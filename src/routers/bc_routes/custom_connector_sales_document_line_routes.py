"""Custom Connector API — Sales Document Line read endpoints (Pag51032).

Same separate AL app as custom_connector_sales_invoice_header_routes.py (github.com/Aaron-Alvarez-RGMC/
custom-connector-AL), own APIPublisher/APIGroup/object ID range - not part of the shared rgmc/
rgmccustom namespace.

Endpoints:
  GET /bc/custom-connector/sales-document-lines         — list
  GET /bc/custom-connector/sales-document-lines/{id}    — get single record by SystemId

Fields: id, documentType, documentNo, lineNo, sellToCustomerNo, lineType, no, description, variantCode, locationCode, returnReasonCode, unitOfMeasureCode, qtyPerUnitOfMeasure, quantity, quantityBase, quantityInvoiced, returnQtyReceived, returnQtyToReceive, unitPrice, lineDiscountPercent, lineDiscountAmount, lineAmount, amount, amountIncludingVat, invAmountDiscount, vatPercent, shipmentNo, returnReceiptNo, applFromItemEntry, companyName, lastModifiedDateTime.
Open Sales Line for Invoice, Credit Memo and Return Order (the lines of Pag51031). Same money fields as the
order lines. returnQtyReceived / returnQtyToReceive follow a return order through receiving. Natural key:
documentType + documentNo + lineNo. Join to sales-document-headers on documentType + documentNo.
"""
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, status

from src import config
from src.services.bc_functions import (
    call_custom_connector_table,
    custom_connector_get_record,
)

logger = logging.getLogger("bc_routes.custom_connector_sales_document_line")

custom_connector_sales_document_line_router = APIRouter(
    prefix="/bc/custom-connector/sales-document-lines",
    tags=["Custom Connector — Sales Document Line"],
)

_TABLE = "salesDocumentLines"


def _unwrap_list(http_status: int, data: Any) -> List[Dict[str, Any]]:
    if http_status != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Business Central returned {http_status}: {data}",
        )
    return data.get("value", data)


@custom_connector_sales_document_line_router.get("", summary="List Sales Document Lines")
def list_sales_document_lines(
    filter: Optional[str] = Query(None, description="OData $filter expression (e.g. documentType eq 'Return Order')"),
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
    modified_from: Optional[str] = Query(
        None,
        description="Only records with lastModifiedDateTime on/after this ISO 8601 datetime (e.g. from Airbyte's incremental cursor). Combined with `filter` via AND if both are given.",
    ),
    limit: Optional[int] = Query(None, ge=1, le=5000, description="Max records to return in this page (BC $top). Omit for all matching records."),
    offset: Optional[int] = Query(None, ge=0, description="Records to skip before this page (BC $skip)."),
):
    """Return Sales Document Lines records from BC (Pag51032). Unfiltered, unpaginated requests are cached for 5 minutes."""
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
        logger.error(f"Error listing sales document lines: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@custom_connector_sales_document_line_router.get("/{record_id}", summary="Get Sales Document Line by ID")
def get_sales_document_line(
    record_id: str,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    """Fetch a single Sales Document Line record by SystemId from BC."""
    try:
        http_status, data = custom_connector_get_record(
            table_endpoint=_TABLE,
            record_id=record_id,
            company_name=company or config.BC_COMPANY,
        )
        if http_status == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Sales document line record '{record_id}' not found",
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
        logger.error(f"Error fetching sales document line {record_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
