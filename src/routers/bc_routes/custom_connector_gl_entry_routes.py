"""Custom Connector API — G/L Entry read endpoints (Pag51027).

Same separate AL app as custom_connector_sales_invoice_header_routes.py (github.com/Aaron-Alvarez-RGMC/
custom-connector-AL), own APIPublisher/APIGroup/object ID range - not part of the shared rgmc/
rgmccustom namespace.

Endpoints:
  GET /bc/custom-connector/gl-entries         — list
  GET /bc/custom-connector/gl-entries/{id}    — get single record by SystemId

Fields: id, entryNo, transactionNo, documentType, documentNo, externalDocumentNo, description, glAccountNo, glAccountName, postingDate, documentDate, amount, debitAmount, creditAmount, vatAmount, genPostingType, genBusPostingGroup, genProdPostingGroup, balAccountType, balAccountNo, sourceType, sourceNo, sourceCode, journalTemplName, journalBatchName, reasonCode, reversed, globalDimension1Code, globalDimension2Code, dimensionSetId, userId, companyName, lastModifiedDateTime.
Every posted general ledger line (G/L Entry, table 17). The largest table in the app: sync it incrementally
from the start (modified_from) with limit/offset paging, and backfill by date range with filter. glAccountName is a
FlowField calculated by the AL page. Natural key: entryNo. Join to customer and bank account ledger entries on
transactionNo (same posting).
"""
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, status

from src import config
from src.services.bc_functions import (
    call_custom_connector_table,
    custom_connector_get_record,
)

logger = logging.getLogger("bc_routes.custom_connector_gl_entry")

custom_connector_gl_entry_router = APIRouter(
    prefix="/bc/custom-connector/gl-entries",
    tags=["Custom Connector — G/L Entry"],
)

_TABLE = "glEntries"


def _unwrap_list(http_status: int, data: Any) -> List[Dict[str, Any]]:
    if http_status != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Business Central returned {http_status}: {data}",
        )
    return data.get("value", data)


@custom_connector_gl_entry_router.get("", summary="List G/L Entries")
def list_gl_entries(
    filter: Optional[str] = Query(None, description="OData $filter expression (e.g. postingDate ge 2026-10-01 and postingDate le 2026-10-31)"),
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
    modified_from: Optional[str] = Query(
        None,
        description="Only records with lastModifiedDateTime on/after this ISO 8601 datetime (e.g. from Airbyte's incremental cursor). Combined with `filter` via AND if both are given.",
    ),
    limit: Optional[int] = Query(None, ge=1, le=5000, description="Max records to return in this page (BC $top). Omit for all matching records."),
    offset: Optional[int] = Query(None, ge=0, description="Records to skip before this page (BC $skip)."),
):
    """Return G/L Entries records from BC (Pag51027). Unfiltered, unpaginated requests are cached for 5 minutes."""
    try:
        company_name = company or config.BC_COMPANY
        odata_filter = filter
        if modified_from:
            clause = f"lastModifiedDateTime ge {modified_from}"
            odata_filter = f"({odata_filter}) and {clause}" if odata_filter else clause
        http_status, data = call_custom_connector_table(
            _TABLE, company_name=company_name, odata_filter=odata_filter, top=limit, skip=offset
        )
        return {"data": _unwrap_list(http_status, data)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing G/L entries: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@custom_connector_gl_entry_router.get("/{record_id}", summary="Get G/L Entry by ID")
def get_gl_entry(
    record_id: str,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    """Fetch a single G/L Entry record by SystemId from BC."""
    try:
        http_status, data = custom_connector_get_record(
            table_endpoint=_TABLE,
            record_id=record_id,
            company_name=company or config.BC_COMPANY,
        )
        if http_status == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"G/L entry record '{record_id}' not found",
            )
        if http_status != 200:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Business Central returned {http_status}: {data}",
            )
        return data
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching G/L entry {record_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
