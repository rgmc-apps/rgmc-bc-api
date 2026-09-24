"""Custom Connector API — Return Receipt Line Extended read endpoints (Pag51001).

Same separate AL app as custom_connector_item_attributes_routes.py (github.com/Aaron-Alvarez-RGMC/
custom-connector-AL), own APIPublisher/APIGroup/object ID range - not part of Erwin's rgmc/
rgmccustom namespace.

Endpoints:
  GET /bc/custom-connector/return-receipt-lines-extended         — list (cached 5 min unfiltered)
  GET /bc/custom-connector/return-receipt-lines-extended/{id}    — get single record by SystemId

Fields: id, documentNo, lineNo, itemRcptEntryNo, itemChargeBaseAmount, lineDiscountPercent,
lastModifiedDateTime. documentNo/lineNo are the join keys back to the existing
bc_richfield_raw.returnReceiptLines sync - Airbyte can't merge two different API sources into one
table on its own, so this lands as its own table until a staging model joins them together.
"""
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, status

from src import config
from src.services.bc_functions import (
    call_custom_connector_table,
    custom_connector_get_record,
)

logger = logging.getLogger("bc_routes.custom_connector_return_receipt_line")

custom_connector_return_receipt_line_router = APIRouter(
    prefix="/bc/custom-connector/return-receipt-lines-extended",
    tags=["Custom Connector — Return Receipt Line Extended"],
)

_TABLE = "returnReceiptLinesExtended"


def _unwrap_list(http_status: int, data: Any) -> List[Dict[str, Any]]:
    if http_status != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Business Central returned {http_status}: {data}",
        )
    return data.get("value", data)


@custom_connector_return_receipt_line_router.get("", summary="List Return Receipt Lines Extended")
def list_return_receipt_lines_extended(
    filter: Optional[str] = Query(None, description="OData $filter expression (e.g. documentNo eq 'RFPRS+260001')"),
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    """Return Return Receipt Line Extended records from BC (Pag51001). Unfiltered requests are cached for 5 minutes."""
    try:
        company_name = company or config.BC_COMPANY
        http_status, data = call_custom_connector_table(_TABLE, company_name=company_name, odata_filter=filter)
        return {"data": _unwrap_list(http_status, data)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing return receipt lines extended: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@custom_connector_return_receipt_line_router.get("/{record_id}", summary="Get Return Receipt Line Extended by ID")
def get_return_receipt_line_extended(
    record_id: str,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    """Fetch a single Return Receipt Line Extended record by SystemId from BC."""
    try:
        http_status, data = custom_connector_get_record(
            table_endpoint=_TABLE,
            record_id=record_id,
            company_name=company or config.BC_COMPANY,
        )
        if http_status == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Return receipt line extended record '{record_id}' not found",
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
        logger.error(f"Error fetching return receipt line extended {record_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
