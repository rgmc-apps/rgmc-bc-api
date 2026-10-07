"""Custom Connector API — Sales Order Line read endpoints (Pag51013).

Same separate AL app as custom_connector_item_attributes_routes.py (github.com/Aaron-Alvarez-RGMC/
custom-connector-AL), own APIPublisher/APIGroup/object ID range - not part of the shared rgmc/
rgmccustom namespace.

Endpoints:
  GET /bc/custom-connector/sales-order-lines         — list
  GET /bc/custom-connector/sales-order-lines/{id}    — get single record by SystemId

Fields: id, documentNo, lineNo, lineType, no, description, variantCode, locationCode, unitOfMeasureCode, qtyPerUnitOfMeasure, quantity, quantityBase, quantityShipped, quantityInvoiced, outstandingQuantity, qtyToShip, unitPrice, lineDiscountPercent, lineAmount, amount, amountIncludingVat, requestedDeliveryDate, promisedDeliveryDate, shipmentDate, companyName, lastModifiedDateTime.
Flat, unlike the nested lines of the shared order page. quantity is the ordered (requested) quantity and quantityShipped
the shipped quantity while the order is open. documentNo + lineNo is the natural key; downstream keeps the maximum
quantity ever seen per line because the order is deleted once fully invoiced.
"""
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, status

from src import config
from src.services.bc_functions import (
    call_custom_connector_table,
    custom_connector_get_record,
)

logger = logging.getLogger("bc_routes.custom_connector_sales_order_line")

custom_connector_sales_order_line_router = APIRouter(
    prefix="/bc/custom-connector/sales-order-lines",
    tags=["Custom Connector — Sales Order Line"],
)

_TABLE = "salesOrderLines"


def _unwrap_list(http_status: int, data: Any) -> List[Dict[str, Any]]:
    if http_status != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Business Central returned {http_status}: {data}",
        )
    return data.get("value", data)


@custom_connector_sales_order_line_router.get("", summary="List Sales Order Lines")
def list_sales_order_lines(
    filter: Optional[str] = Query(None, description="OData $filter expression (e.g. documentNo eq 'SBSO260175')"),
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
    modified_from: Optional[str] = Query(
        None,
        description="Only records with lastModifiedDateTime on/after this ISO 8601 datetime (e.g. from Airbyte's incremental cursor). Combined with `filter` via AND if both are given.",
    ),
    limit: Optional[int] = Query(None, ge=1, le=5000, description="Max records to return in this page (BC $top). Omit for all matching records."),
    offset: Optional[int] = Query(None, ge=0, description="Records to skip before this page (BC $skip)."),
):
    """Return Sales Order Line records from BC (Pag51013)."""
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
        logger.error(f"Error listing order lines: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@custom_connector_sales_order_line_router.get("/{record_id}", summary="Get Sales Order Line by ID")
def get_sales_order_line(
    record_id: str,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    """Fetch a single Sales Order Line record by SystemId from BC."""
    try:
        http_status, data = custom_connector_get_record(
            table_endpoint=_TABLE,
            record_id=record_id,
            company_name=company or config.BC_COMPANY,
        )
        if http_status == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Sales Order Line record '{record_id}' not found",
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
        logger.error(f"Error fetching order line {record_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
