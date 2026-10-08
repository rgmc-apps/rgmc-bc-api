"""Shareable BigQuery-Lookup alignment reports.

A frozen snapshot of one BigQuery Lookup search (criteria + per-PO BigQuery/MSSQL/BC
status + a plain-English recommended fix step, built by sbic-manual-trigger-page's
/api/bigquery/report) — served back at a plain link so a person-in-charge can see
exactly what's misaligned without needing /reconcile access themselves. Read/write
only, never patched — rgmc-worker-pool/rgmc-gcp-api have no involvement here.
"""
import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Body, HTTPException, status

from src.services.bq_lookup_report_service import create_report, get_report

logger = logging.getLogger("bc_routes.bq_lookup_report")

bq_lookup_report_router = APIRouter(
    prefix="/bc/custom/v2/bq-lookup-reports",
    tags=["BC Custom Extended — BigQuery Lookup Reports"],
)


@bq_lookup_report_router.post(
    "",
    summary="Save a shareable BigQuery-Lookup alignment report",
    status_code=status.HTTP_201_CREATED,
)
def post_report(
    criteria: Dict[str, Any] = Body(..., embed=True, description="The search's po_ref_number/customer_name/date_from/date_to, as submitted"),
    rows: List[Dict[str, Any]] = Body(..., embed=True, description="One row per PO found (or not found) — status + recommended fix step"),
    generated_by: Dict[str, Any] = Body({}, embed=True, description="{name, company, department, email} of who generated this report"),
    headers: List[Dict[str, Any]] = Body([], embed=True, description="The search's raw headers, so the report page's own Quick Align buttons can replay actions from the shared link"),
    details: List[Dict[str, Any]] = Body([], embed=True, description="The search's raw detail lines, matching `headers`"),
):
    if not rows:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="rows must not be empty")
    try:
        report_id = create_report(criteria, rows, generated_by, headers=headers, details=details)
        return {"id": report_id}
    except Exception as e:
        logger.error(f"Error creating BigQuery-lookup report: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@bq_lookup_report_router.get("/{report_id}", summary="Read a shareable BigQuery-Lookup alignment report")
def get_report_route(report_id: str):
    try:
        report = get_report(report_id)
    except Exception as e:
        logger.error(f"Error reading BigQuery-lookup report {report_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No report {report_id!r}")
    return report
