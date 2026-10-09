"""Manual SO-import buffer reconciliation.

Reads the Firestore buffer rgmc-worker-pool owns (so_buffer_{env}) and lets a human save
a resolved BC link for a raw SKU code / customer branch name / customer name shared by
one or more buffered orders — for the sbic-manual-trigger-page reconciliation UI.

Saved overrides ARE consumed by rgmc-worker-pool's reprocess logic: apply_resolution_to_buffer
(below) patches the resolved link directly onto the affected buffer doc(s)
(header.resolvedShipTo/resolvedCustomer, line.resolvedItem), and so_import_worker.py's
_create_order/_resolve_valid_lines read those fields first, ahead of their own automatic
ship-to/item matching. Reprocessing itself still goes through rgmc-gcp-api's existing
POST /customerpoul/reprocess-buffer, unchanged by this feature.
"""
import logging
from typing import List, Optional

from fastapi import APIRouter, Body, HTTPException, Query, status

from src.services.so_buffer_service import (
    VALID_OVERRIDE_TYPES,
    add_missing_lines_to_buffer,
    apply_resolution_to_buffer,
    create_buffer_entry,
    delete_override,
    get_reprocess_run,
    list_buffer_history,
    list_buffered_orders,
    list_inactive_skus,
    list_overrides,
    list_reference,
    mark_sku_inactive,
    record_history_entry,
    save_override,
    unmark_sku_inactive,
)

logger = logging.getLogger("bc_routes.so_buffer")

so_buffer_router = APIRouter(
    prefix="/bc/custom/v2/so-buffer",
    tags=["BC Custom Extended — SO Buffer Reconciliation"],
)


@so_buffer_router.get("", summary="List buffered/failed POUL SO-import orders")
def get_buffered_orders(
    company: Optional[str] = Query(None, description="BC company code (e.g. SBIC, MTC). Omit for all companies."),
):
    """Read-only mirror of rgmc-worker-pool's Firestore so_buffer_{env} collection."""
    try:
        orders = list_buffered_orders(company=company)
        return {"data": orders, "total": len(orders)}
    except Exception as e:
        logger.error(f"Error listing buffered orders: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@so_buffer_router.post(
    "/manual-entry",
    summary="Manually create a new SO-import buffer entry (e.g. from a BigQuery lookup)",
    status_code=status.HTTP_201_CREATED,
)
def post_manual_buffer_entry(
    header: dict = Body(..., embed=True, description="Buffer doc header — poRefNumber, customerName, customerBranchName, poDate, etc."),
    lines: List[dict] = Body([], embed=True, description="Buffer doc lines — customerSKUCode, poQty, etc."),
    company: str = Body(..., embed=True, description="BC company code this order belongs to (e.g. SBIC, MTC)"),
    src_company: str = Body("", embed=True, description="Descriptive source label, e.g. 'manual-bq-lookup:SBIC'"),
    created_by: str = Body("", embed=True, description="Who added this (free text, shown in the buffer doc's last_error)"),
):
    if not header.get("poRefNumber"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="header.poRefNumber is required")
    if not company:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="company is required")
    try:
        return create_buffer_entry(header, lines, company, src_company, created_by)
    except Exception as e:
        logger.error(f"Error creating manual buffer entry: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@so_buffer_router.post(
    "/merge-lines",
    summary="Add missing detail lines to an order ALREADY sitting in the buffer (e.g. a BigQuery lookup found extra lines)",
)
def post_merge_buffer_lines(
    po_ref_number: str = Body(..., embed=True, description="The already-buffered order's poRefNumber"),
    lines: List[dict] = Body(..., embed=True, description="Candidate lines — only ones not already present (matched by customerSKUCode, falling back to customerSKUDesc) are appended"),
):
    if not po_ref_number:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="po_ref_number is required")
    try:
        result = add_missing_lines_to_buffer(po_ref_number, lines)
    except Exception as e:
        logger.error(f"Error merging lines into buffer entry {po_ref_number}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No existing buffer entry for PO ref {po_ref_number!r} — create one instead",
        )
    return result


@so_buffer_router.get("/overrides", summary="List saved manual reconciliation links")
def get_overrides(
    type: Optional[str] = Query(None, description="Filter by override type: sku, branch, customer, or uom."),
):
    if type and type not in VALID_OVERRIDE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"type must be one of {VALID_OVERRIDE_TYPES}",
        )
    try:
        overrides = list_overrides(override_type=type)
        return {"data": overrides, "total": len(overrides)}
    except Exception as e:
        logger.error(f"Error listing overrides: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@so_buffer_router.post("/overrides", summary="Save a manual reconciliation link", status_code=status.HTTP_201_CREATED)
def post_override(
    type: str = Body(..., embed=True, description="sku | branch | customer | uom"),
    key: str = Body(..., embed=True, description="Raw SKU code / branch name / customer name being resolved, or \"{itemNo}::{rawUom}\" for uom"),
    resolved: dict = Body(..., embed=True, description="The chosen BC record's fields"),
    resolved_by: str = Body("", embed=True, description="Who resolved this link (free text)"),
    buffer_ids: List[str] = Body(
        [],
        embed=True,
        description="Buffered order doc IDs this resolution applies to — also patched "
                    "directly onto each doc's header/lines, not just the overrides collection.",
    ),
):
    if type not in VALID_OVERRIDE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"type must be one of {VALID_OVERRIDE_TYPES}",
        )
    if not key.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="key must not be blank")
    if not resolved:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="resolved must not be empty")
    try:
        result = save_override(type, key, resolved, resolved_by, buffer_ids=buffer_ids)
        if buffer_ids:
            patched = apply_resolution_to_buffer(type, key, resolved, buffer_ids)
            result["buffer_docs_patched"] = patched
        return result
    except Exception as e:
        logger.error(f"Error saving override: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@so_buffer_router.get(
    "/reference",
    summary="List the full resolution history (append-only, every save ever made)",
)
def get_reference(
    type: Optional[str] = Query(None, description="Filter by override type: sku, branch, customer, or uom."),
    key: Optional[str] = Query(None, description="Filter to one exact raw key (SKU code / branch name / customer name / \"{itemNo}::{rawUom}\")."),
):
    if type and type not in VALID_OVERRIDE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"type must be one of {VALID_OVERRIDE_TYPES}",
        )
    try:
        history = list_reference(override_type=type, key=key)
        return {"data": history, "total": len(history)}
    except Exception as e:
        logger.error(f"Error listing reference history: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@so_buffer_router.delete(
    "/overrides/{override_id}",
    summary="Remove a saved manual link",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_override_route(override_id: str):
    try:
        delete_override(override_id)
    except Exception as e:
        logger.error(f"Error deleting override {override_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@so_buffer_router.get(
    "/reprocess-status/{run_id}",
    summary="Status of one manual reprocess-buffer run (ongoing / done / error)",
)
def get_reprocess_status(run_id: str):
    """Read rgmc-worker-pool's Firestore reprocess_runs_{env}/{run_id}, written as it
    processes the Pub/Sub message rgmc-gcp-api's POST /customerpoul/reprocess-buffer
    published (that response includes this same run_id)."""
    try:
        return get_reprocess_run(run_id)
    except Exception as e:
        logger.error(f"Error reading reprocess run {run_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@so_buffer_router.get(
    "/inactive-skus",
    summary="List SKU codes marked inactive (excluded from the Items (SKU) tab/counts)",
)
def get_inactive_skus():
    try:
        rows = list_inactive_skus()
        return {"data": rows, "total": len(rows)}
    except Exception as e:
        logger.error(f"Error listing inactive SKUs: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@so_buffer_router.post(
    "/inactive-skus",
    summary="Mark a SKU code (or description) inactive",
    status_code=status.HTTP_201_CREATED,
)
def post_inactive_sku(
    key: str = Body(..., embed=True, description="Raw SKU code (or description, for a blank-SKU group) to exclude"),
    marked_by: str = Body("", embed=True, description="Who marked this inactive (free text)"),
):
    if not key.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="key must not be blank")
    try:
        return mark_sku_inactive(key, marked_by)
    except Exception as e:
        logger.error(f"Error marking SKU inactive: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@so_buffer_router.delete(
    "/inactive-skus/{doc_id}",
    summary="Reactivate a SKU (undo mark-inactive)",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_inactive_sku(doc_id: str):
    try:
        unmark_sku_inactive(doc_id)
    except Exception as e:
        logger.error(f"Error reactivating SKU {doc_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@so_buffer_router.get(
    "/history",
    summary="List buffer-reconciliation history (every PO a reprocess-buffer run touched)",
)
def get_buffer_history(
    company: Optional[str] = Query(None, description="BC company code (e.g. SBIC, MTC)."),
    po_ref: Optional[str] = Query(None, description="Filter to one exact poRefNumber."),
    outcome: Optional[str] = Query(None, description="Filter by outcome: resolved, still_buffered, or failed."),
    run_id: Optional[str] = Query(None, description="Filter to one reprocess-buffer run."),
):
    """Read-only mirror of rgmc-worker-pool's Firestore so_buffer_history_{env}
    collection — one record per PO per manual reprocess-buffer attempt, including the
    header/lines snapshot at that attempt, the outcome, and who triggered it."""
    try:
        history = list_buffer_history(company=company, po_ref=po_ref, outcome=outcome, run_id=run_id)
        return {"data": history, "total": len(history)}
    except Exception as e:
        logger.error(f"Error listing buffer history: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@so_buffer_router.post(
    "/history/manual-entry",
    summary="Manually record a buffer-history entry (e.g. a BigQuery-lookup insert blocked by an existing BC order)",
    status_code=status.HTTP_201_CREATED,
)
def post_manual_history_entry(
    header: dict = Body(..., embed=True, description="Header snapshot at the time of this attempt — poRefNumber, customerName, etc."),
    lines: List[dict] = Body([], embed=True, description="Line snapshot at the time of this attempt"),
    company: str = Body(..., embed=True, description="BC company code (e.g. SBIC, MTC)"),
    outcome: str = Body(..., embed=True, description="resolved | still_buffered | failed"),
    triggered_by: Optional[dict] = Body(None, embed=True, description="{name, company, department, email} of who triggered this"),
    so_number: Optional[str] = Body(None, embed=True, description="The existing BC Sales Order's Document No., if known"),
    detail: Optional[str] = Body(None, embed=True, description="Free-text note, e.g. why this was blocked"),
):
    if not header.get("poRefNumber"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="header.poRefNumber is required")
    if outcome not in ("resolved", "still_buffered", "failed"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="outcome must be one of: resolved, still_buffered, failed")
    try:
        return record_history_entry(header, lines, company, outcome, triggered_by, so_number=so_number, detail=detail)
    except Exception as e:
        logger.error(f"Error recording manual history entry: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
