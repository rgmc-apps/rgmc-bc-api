"""RGMC custom API v2.0 — Item endpoints (Pag50310, read-only)."""
import logging
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException, Query, status
from src.services.bc_functions import call_rgmc_v2_table, rgmc_v2_get_record, rgmc_v2_list_table_live, odata_escape
from src import config

logger = logging.getLogger("bc_routes.rgmc_items_v2")

rgmc_item_v2_router = APIRouter(prefix="/bc/custom/v2/items", tags=["BC RGMC Items v2"])

_TABLE = "items"


def _unwrap_list(bc_result: tuple) -> List[Dict[str, Any]]:
    http_status, data = bc_result
    if http_status != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Business Central returned {http_status}: {data}",
        )
    return data.get("value", data)


def _unwrap_single(http_status: int, data: Any) -> Dict[str, Any]:
    if http_status == 404:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
    if http_status != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Business Central returned {http_status}: {data}",
        )
    return data


@rgmc_item_v2_router.get("", summary="List RGMC Items v2")
def list_rgmc_items_v2(
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
    filter: Optional[str] = Query(None, description="OData $filter expression"),
    select: Optional[str] = Query(None, description="OData $select"),
    category_code: Optional[str] = Query(None, description="Filter by itemCategoryCode"),
    family_code: Optional[str] = Query(None, description="Filter by familyCode"),
):
    try:
        filters = []
        if category_code:
            filters.append(f"itemCategoryCode eq '{category_code}'")
        if family_code:
            filters.append(f"familyCode eq '{family_code}'")
        if filter:
            filters.append(filter)
        combined_filter = " and ".join(filters) if filters else None
        result = call_rgmc_v2_table(_TABLE, company_name=company or config.BC_COMPANY, odata_filter=combined_filter, select=select)
        return {"data": _unwrap_list(result)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing RGMC items v2: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_item_v2_router.get("/{item_id}", summary="Get RGMC Item v2 by ID")
def get_rgmc_item_v2(
    item_id: str,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    try:
        http_status, data = rgmc_v2_get_record(_TABLE, item_id, company_name=company or config.BC_COMPANY)
        return _unwrap_single(http_status, data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching RGMC item v2 {item_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_item_v2_router.get(
    "/{item_no}/unit-of-measure",
    summary="Valid units of measure for one item (BC's Item Unit of Measure table)",
)
def get_item_unit_of_measure(
    item_no: str,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    """Reads itemUnitsOfMeasure (Pag50353) filtered to one item — the exact table
    BC's own sales-line rejection points at when a line's Unit of Measure Code isn't
    configured for that item ("...cannot be found in the related table (Item Unit of
    Measure)"). Live, single-page read (not the cached item catalog) since this is a
    one-off lookup for the /reconcile page's Unit of Measure tab, not a bulk listing.
    """
    try:
        safe = odata_escape(item_no.strip())
        http_status, records, total = rgmc_v2_list_table_live(
            "itemUnitsOfMeasure",
            company_name=company or config.BC_COMPANY,
            odata_filter=f"itemNo eq '{safe}'",
            top=50,
        )
        if http_status != 200:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Business Central returned {http_status}: {records}",
            )
        mapped = [
            {
                "itemNo": r.get("itemNo"),
                "code": r.get("code"),
                "description": r.get("description"),
                "qtyPerUnitOfMeasure": r.get("qtyPerUnitOfMeasure"),
            }
            for r in records
        ]
        return {"data": mapped, "total": total}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing unit of measure for item {item_no}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
