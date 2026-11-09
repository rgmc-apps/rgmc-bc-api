"""Custom Connector API — Sales Credit Memo Header read endpoints (Pag51029).

Same separate AL app as custom_connector_sales_invoice_header_routes.py (github.com/Aaron-Alvarez-RGMC/
custom-connector-AL), own APIPublisher/APIGroup/object ID range - not part of the shared rgmc/
rgmccustom namespace.

Endpoints:
  GET /bc/custom-connector/sales-cr-memo-headers         — list
  GET /bc/custom-connector/sales-cr-memo-headers/{id}    — get single record by SystemId

Fields: id, no, preAssignedNo, returnOrderNo, externalDocumentNo, yourReference, appliesToDocType, appliesToDocNo, custLedgerEntryNo, sellToCustomerNo, billToCustomerNo, shipToCode, salespersonCode, locationCode, postingDate, documentDate, dueDate, paymentTermsCode, reasonCode, currencyCode, pricesIncludingVat, amount, amountIncludingVat, invoiceDiscountAmount, cancelled, corrective, paid, remainingAmount, userId, companyName, lastModifiedDateTime.
Posted credit memos only (Sales Cr.Memo Header, table 114). no is the natural key and equals documentNo on
the Sales Credit Memo Lines stream. Join to the return order on returnOrderNo, to the invoice it corrects on
appliesToDocNo. amount, amountIncludingVat, invoiceDiscountAmount, cancelled, corrective, paid and remainingAmount
are FlowFields calculated by the AL page (paid / remainingAmount can lag, as on the invoice header).
"""
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, status

from src import config
from src.services.bc_functions import (
    call_custom_connector_table,
    custom_connector_get_record,
)

logger = logging.getLogger("bc_routes.custom_connector_sales_cr_memo_header")

custom_connector_sales_cr_memo_header_router = APIRouter(
    prefix="/bc/custom-connector/sales-cr-memo-headers",
    tags=["Custom Connector — Sales Credit Memo Header"],
)

_TABLE = "salesCrMemoHeaders"


def _unwrap_list(http_status: int, data: Any) -> List[Dict[str, Any]]:
    if http_status != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Business Central returned {http_status}: {data}",
        )
    return data.get("value", data)


@custom_connector_sales_cr_memo_header_router.get("", summary="List Sales Credit Memo Headers")
def list_sales_cr_memo_headers(
    filter: Optional[str] = Query(None, description="OData $filter expression (e.g. sellToCustomerNo eq 'C00001')"),
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
    modified_from: Optional[str] = Query(
        None,
        description="Only records with lastModifiedDateTime on/after this ISO 8601 datetime (e.g. from Airbyte's incremental cursor). Combined with `filter` via AND if both are given.",
    ),
    limit: Optional[int] = Query(None, ge=1, le=5000, description="Max records to return in this page (BC $top). Omit for all matching records."),
    offset: Optional[int] = Query(None, ge=0, description="Records to skip before this page (BC $skip)."),
):
    """Return Sales Credit Memo Headers records from BC (Pag51029). Unfiltered, unpaginated requests are cached for 5 minutes."""
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
        logger.error(f"Error listing sales credit memo headers: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@custom_connector_sales_cr_memo_header_router.get("/{record_id}", summary="Get Sales Credit Memo Header by ID")
def get_sales_cr_memo_header(
    record_id: str,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    """Fetch a single Sales Credit Memo Header record by SystemId from BC."""
    try:
        http_status, data = custom_connector_get_record(
            table_endpoint=_TABLE,
            record_id=record_id,
            company_name=company or config.BC_COMPANY,
        )
        if http_status == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Sales credit memo header record '{record_id}' not found",
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
        logger.error(f"Error fetching sales credit memo header {record_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
