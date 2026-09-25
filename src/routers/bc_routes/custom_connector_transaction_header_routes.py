"""Custom Connector API — Transaction Header Extended read endpoints (Pag51002).

Same separate AL app as custom_connector_item_attributes_routes.py (github.com/Aaron-Alvarez-RGMC/
custom-connector-AL), own APIPublisher/APIGroup/object ID range - not part of Erwin's rgmc/
rgmccustom namespace.

Endpoints:
  GET /bc/custom-connector/transaction-headers-extended         — list (cached 5 min unfiltered)
  GET /bc/custom-connector/transaction-headers-extended/{id}    — get single record by SystemId

Fields: id, storeNo, posTerminalNo, transactionNo, transactionType, payment, vatDifference,
officialReceiptNo, returnExchangeNo, companyName, lastModifiedDateTime. storeNo/posTerminalNo/
transactionNo are the join keys back to the existing transactionHeaders sync (bc_covent_raw /
bc_richfield_raw) - Airbyte can't merge two different API sources into one table on its own, so
this lands as its own table until a staging model joins them together.

transactionType/payment are confirmed real fields on LS Central's own Transaction Header table.
vatDifference/officialReceiptNo/returnExchangeNo are confirmed real fields added by the PHPOS
localization extension - confirmed live via Page Inspector on a real Covent (CGI) transaction,
2026-09-25, not guessed. None of these were exposed by any existing API before this page.
"""
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, status

from src import config
from src.services.bc_functions import (
    call_custom_connector_table,
    custom_connector_get_record,
)

logger = logging.getLogger("bc_routes.custom_connector_transaction_header")

custom_connector_transaction_header_router = APIRouter(
    prefix="/bc/custom-connector/transaction-headers-extended",
    tags=["Custom Connector — Transaction Header Extended"],
)

_TABLE = "transactionHeadersExtended"


def _unwrap_list(http_status: int, data: Any) -> List[Dict[str, Any]]:
    if http_status != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Business Central returned {http_status}: {data}",
        )
    return data.get("value", data)


@custom_connector_transaction_header_router.get("", summary="List Transaction Headers Extended")
def list_transaction_headers_extended(
    filter: Optional[str] = Query(None, description="OData $filter expression (e.g. storeNo eq 'SP003')"),
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
    modified_from: Optional[str] = Query(
        None,
        description="Only records with lastModifiedDateTime on/after this ISO 8601 datetime (e.g. from Airbyte's incremental cursor). Combined with `filter` via AND if both are given.",
    ),
    limit: Optional[int] = Query(None, ge=1, le=5000, description="Max records to return in this page (BC $top). Omit for all matching records."),
    offset: Optional[int] = Query(None, ge=0, description="Records to skip before this page (BC $skip)."),
):
    """Return Transaction Header Extended records from BC (Pag51002). Unfiltered, unpaginated requests are cached for 5 minutes."""
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
        logger.error(f"Error listing transaction headers extended: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@custom_connector_transaction_header_router.get("/{record_id}", summary="Get Transaction Header Extended by ID")
def get_transaction_header_extended(
    record_id: str,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    """Fetch a single Transaction Header Extended record by SystemId from BC."""
    try:
        http_status, data = custom_connector_get_record(
            table_endpoint=_TABLE,
            record_id=record_id,
            company_name=company or config.BC_COMPANY,
        )
        if http_status == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Transaction header extended record '{record_id}' not found",
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
        logger.error(f"Error fetching transaction header extended {record_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
