"""RGMC custom API v2.0 — Item Reference read endpoints (Pag50349, read-only).

Fields: id, itemNo, referenceNo, referenceType, referenceTypeNo, unitOfMeasure, description.
`referenceType` is an option (e.g. Item, Variant, Bar Code, Customer, Vendor) — pass the
caption text as-is (e.g. `reference_type=Bar Code`).
"""
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, status

from src import config
from src.services.bc_functions import call_rgmc_v2_table, odata_escape, rgmc_v2_get_record

logger = logging.getLogger("bc_routes.rgmc_item_reference_v2")

rgmc_item_reference_v2_router = APIRouter(
    prefix="/bc/custom/v2/item-references",
    tags=["BC RGMC Item References v2"],
)

_TABLE = "itemReferences"


def _unwrap_list(http_status: int, data: Any) -> List[Dict[str, Any]]:
    if http_status != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Business Central returned {http_status}: {data}",
        )
    return data.get("value", data)


def _unwrap_single(http_status: int, data: Any) -> Dict[str, Any]:
    if http_status == 404:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item Reference not found")
    if http_status != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Business Central returned {http_status}: {data}",
        )
    return data


@rgmc_item_reference_v2_router.get("", summary="List RGMC Item References v2")
def list_rgmc_item_references_v2(
    item_no: Optional[str] = Query(None, description="Filter by itemNo (item code)"),
    reference_type: Optional[str] = Query(None, description="Filter by referenceType (e.g. Item, Variant, Bar Code, Customer, Vendor)"),
    filter: Optional[str] = Query(None, description="Additional raw OData $filter expression"),
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    try:
        filters = []
        if item_no:
            filters.append(f"itemNo eq '{odata_escape(item_no)}'")
        if reference_type:
            filters.append(f"referenceType eq '{odata_escape(reference_type)}'")
        if filter:
            filters.append(filter)
        combined_filter = " and ".join(filters) if filters else None
        http_status, data = call_rgmc_v2_table(
            _TABLE, company_name=company or config.BC_COMPANY, odata_filter=combined_filter
        )
        return {"data": _unwrap_list(http_status, data)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing RGMC item references v2: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_item_reference_v2_router.get("/{record_id}", summary="Get RGMC Item Reference v2 by ID")
def get_rgmc_item_reference_v2(
    record_id: str,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    try:
        http_status, data = rgmc_v2_get_record(_TABLE, record_id, company_name=company or config.BC_COMPANY)
        return _unwrap_single(http_status, data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching RGMC item reference v2 {record_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
