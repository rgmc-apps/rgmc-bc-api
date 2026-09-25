"""Manual SO-import buffer reconciliation.

Reads the Firestore buffer rgmc-worker-pool owns (so_buffer_{env}) and lets a human save
a resolved BC link for a raw SKU code / customer branch name / customer name shared by
one or more buffered orders — for the sbic-manual-trigger-page reconciliation UI.

IMPORTANT: saved overrides are NOT YET consumed by rgmc-worker-pool's reprocess logic.
so_import_worker.py always re-derives customer/ship-to/item from each buffered order's
raw text fields — this endpoint only persists what the user chose so it isn't lost, and
so a future worker-pool change can consult it. Reprocessing today still uses
rgmc-gcp-api's existing POST /customerpoul/reprocess-buffer, unchanged by this feature.
"""
import logging
from typing import List, Optional

from fastapi import APIRouter, Body, HTTPException, Query, status

from src.services.so_buffer_service import (
    VALID_OVERRIDE_TYPES,
    apply_resolution_to_buffer,
    delete_override,
    get_reprocess_run,
    list_buffered_orders,
    list_overrides,
    list_reference,
    save_override,
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


@so_buffer_router.get("/overrides", summary="List saved manual reconciliation links")
def get_overrides(
    type: Optional[str] = Query(None, description="Filter by override type: sku, branch, or customer."),
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
    type: str = Body(..., embed=True, description="sku | branch | customer"),
    key: str = Body(..., embed=True, description="Raw SKU code / branch name / customer name being resolved"),
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
    type: Optional[str] = Query(None, description="Filter by override type: sku, branch, or customer."),
    key: Optional[str] = Query(None, description="Filter to one exact raw key (SKU code / branch name / customer name)."),
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
