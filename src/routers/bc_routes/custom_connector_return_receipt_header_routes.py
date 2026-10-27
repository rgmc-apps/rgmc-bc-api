"""Custom Connector API — Return Receipt Header read endpoints (Pag51034).

Same separate AL app as custom_connector_sales_invoice_header_routes.py (github.com/Aaron-Alvarez-RGMC/
custom-connector-AL), own APIPublisher/APIGroup/object ID range - not part of the shared rgmc/
rgmccustom namespace.

Endpoints:
  GET /bc/custom-connector/return-receipt-headers         — list
  GET /bc/custom-connector/return-receipt-headers/{id}    — get single record by SystemId

Fields: id, no, returnOrderNo, externalDocumentNo, yourReference, packageTrackingNo, sellToCustomerNo, billToCustomerNo, shipToCode, salespersonCode, locationCode, postingDate, documentDate, shipmentDate, reasonCode, userId, companyName, lastModifiedDateTime.
Posted return receipts (Return Receipt Header, table 6660). The lines are already served by
custom_connector_return_receipt_line_routes.py (Pag51001) and Pag50338. no is the natural key and equals
documentNo on the return receipt lines. Join to the return order on returnOrderNo.
"""
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, status

from src import config
from src.services.bc_functions import (
    call_custom_connector_table,
    custom_connector_get_record,
)

logger = logging.getLogger("bc_routes.custom_connector_return_receipt_header")

custom_connector_return_receipt_header_router = APIRouter(
    prefix="/bc/custom-connector/return-receipt-headers",
    tags=["Custom Connector — Return Receipt Header"],
)

_TABLE = "returnReceiptHeaders"


def _unwrap_list(http_status: int, data: Any) -> List[Dict[str, Any]]:
    if http_status != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Business Central returned {http_status}: {data}",
        )
    return data.get("value", data)


@custom_connector_return_receipt_header_router.get("", summary="List Return Receipt Headers")
def list_return_receipt_headers(
    filter: Optional[str] = Query(None, description="OData $filter expression (e.g. returnOrderNo eq 'SRO-0001')"),
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
    modified_from: Optional[str] = Query(
        None,
        description="Only records with lastModifiedDateTime on/after this ISO 8601 datetime (e.g. from Airbyte's incremental cursor). Combined with `filter` via AND if both are given.",
    ),
    limit: Optional[int] = Query(None, ge=1, le=5000, description="Max records to return in this page (BC $top). Omit for all matching records."),
    offset: Optional[int] = Query(None, ge=0, description="Records to skip before this page (BC $skip)."),
):
    """Return Return Receipt Headers records from BC (Pag51034). Unfiltered, unpaginated requests are cached for 5 minutes."""
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
        logger.error(f"Error listing return receipt headers: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@custom_connector_return_receipt_header_router.get("/{record_id}", summary="Get Return Receipt Header by ID")
def get_return_receipt_header(
    record_id: str,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    """Fetch a single Return Receipt Header record by SystemId from BC."""
    try:
        http_status, data = custom_connector_get_record(
            table_endpoint=_TABLE,
            record_id=record_id,
            company_name=company or config.BC_COMPANY,
        )
        if http_status == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Return receipt header record '{record_id}' not found",
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
        logger.error(f"Error fetching return receipt header {record_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
