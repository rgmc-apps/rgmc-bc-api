"""Custom Connector API — Sales Document Header read endpoints (Pag51031).

Same separate AL app as custom_connector_sales_invoice_header_routes.py (github.com/Aaron-Alvarez-RGMC/
custom-connector-AL), own APIPublisher/APIGroup/object ID range - not part of the shared rgmc/
rgmccustom namespace.

Endpoints:
  GET /bc/custom-connector/sales-document-headers         — list
  GET /bc/custom-connector/sales-document-headers/{id}    — get single record by SystemId

Fields: id, documentType, no, externalDocumentNo, yourReference, status, appliesToDocType, appliesToDocNo, sellToCustomerNo, billToCustomerNo, shipToCode, salespersonCode, locationCode, orderDate, postingDate, documentDate, dueDate, paymentTermsCode, reasonCode, currencyCode, pricesIncludingVat, amount, amountIncludingVat, returnReceiptNo, assignedUserId, companyName, lastModifiedDateTime.
Open (unposted) Sales Header for the document types the order header page (Pag51012) does not cover:
Invoice, Credit Memo and Return Order, told apart by documentType. A document leaves this table when it is posted,
so the stream captures each state while it is open and staging keeps the history. amount and amountIncludingVat are
FlowFields calculated by the AL page. Natural key: documentType + no. Join to sales-document-lines on
documentType + no = documentType + documentNo.
"""
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, status

from src import config
from src.services.bc_functions import (
    call_custom_connector_table,
    custom_connector_get_record,
)

logger = logging.getLogger("bc_routes.custom_connector_sales_document_header")

custom_connector_sales_document_header_router = APIRouter(
    prefix="/bc/custom-connector/sales-document-headers",
    tags=["Custom Connector — Sales Document Header"],
)

_TABLE = "salesDocumentHeaders"


def _unwrap_list(http_status: int, data: Any) -> List[Dict[str, Any]]:
    if http_status != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Business Central returned {http_status}: {data}",
        )
    return data.get("value", data)


@custom_connector_sales_document_header_router.get("", summary="List Sales Document Headers")
def list_sales_document_headers(
    filter: Optional[str] = Query(None, description="OData $filter expression (e.g. documentType eq 'Return Order')"),
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
    modified_from: Optional[str] = Query(
        None,
        description="Only records with lastModifiedDateTime on/after this ISO 8601 datetime (e.g. from Airbyte's incremental cursor). Combined with `filter` via AND if both are given.",
    ),
    limit: Optional[int] = Query(None, ge=1, le=5000, description="Max records to return in this page (BC $top). Omit for all matching records."),
    offset: Optional[int] = Query(None, ge=0, description="Records to skip before this page (BC $skip)."),
):
    """Return Sales Document Headers records from BC (Pag51031). Unfiltered, unpaginated requests are cached for 5 minutes."""
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
        logger.error(f"Error listing sales document headers: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@custom_connector_sales_document_header_router.get("/{record_id}", summary="Get Sales Document Header by ID")
def get_sales_document_header(
    record_id: str,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    """Fetch a single Sales Document Header record by SystemId from BC."""
    try:
        http_status, data = custom_connector_get_record(
            table_endpoint=_TABLE,
            record_id=record_id,
            company_name=company or config.BC_COMPANY,
        )
        if http_status == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Sales document header record '{record_id}' not found",
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
        logger.error(f"Error fetching sales document header {record_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
