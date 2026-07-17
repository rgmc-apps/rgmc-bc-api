"""RGMC custom API v3.0 — Item Price read endpoints (Pag50318) and count endpoint (Pag50319)."""
import logging
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from src.services.bc_functions import (
    rgmc_v3_list_item_prices,
    rgmc_v3_get_item_price,
    rgmc_v3_warmup,
    rgmc_v3_invalidate_cache,
    rgmc_v3_get_item_price_count,
    ServiceWarmingError,
)
from src import config

logger = logging.getLogger("bc_routes.rgmc_item_prices_v3")


class ItemPricePage(BaseModel):
    data: List[Dict[str, Any]]
    total: int
    skip: int
    limit: int


rgmc_item_price_v3_router = APIRouter(
    prefix="/bc/custom/v3/item-prices",
    tags=["BC RGMC Item Prices v3"],
)


def _unwrap(http_status: int, data: Any) -> List[Dict[str, Any]]:
    if http_status != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Business Central returned {http_status}: {data}",
        )
    return data.get("value", data)


@rgmc_item_price_v3_router.get("", summary="List Item Prices (v3)")
def list_item_prices(
    product_no: Optional[str] = Query(None, description="Filter by a single item No. (productNo)"),
    product_nos: Optional[str] = Query(None, description="Comma-separated list of item numbers to filter"),
    family_code: Optional[str] = Query(None, description="Filter by familyCode (Pag50318 field). Takes priority over product_nos when no product_no is set."),
    on_date: Optional[str] = Query(None, description="Return the active price as of this date (YYYY-MM-DD). Defaults to BC WorkDate when omitted."),
    filter: Optional[str] = Query(None, description="Additional OData $filter expression"),
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
    skip: int = Query(0, ge=0, description="Python-level records to skip after fetching from BC (use bc_offset for BC-native pagination)"),
    limit: int = Query(0, ge=0, description="Python-level max records to return; 0 = all (use bc_limit for BC-native pagination)"),
    bc_limit: Optional[int] = Query(None, ge=0, description="Pass limit directly to BC's OnOpenPage — BC returns only this many records. 0 = all."),
    bc_offset: Optional[int] = Query(None, ge=0, description="Pass offset directly to BC's OnOpenPage — BC skips this many products before returning."),
):
    """Returns one record per product — the price with the highest Starting Date on or before
    on_date (BC WorkDate if omitted), excluding IC price lists. Fields include unitPriceIncVAT
    and assignToNo (not in v2).

    Pagination modes:
    - bc_limit / bc_offset: BC-native — only the requested page is processed by BC (most efficient).
    - skip / limit: Python-level — full result fetched from BC then sliced in Python (legacy).
    """
    try:
        nos_list = [n.strip() for n in product_nos.split(",") if n.strip()] if product_nos else None
        company_name = company or config.BC_COMPANY
        http_status, data = rgmc_v3_list_item_prices(
            company_name=company_name,
            product_no=product_no,
            product_nos=nos_list,
            family_code=family_code,
            on_date=on_date,
            odata_filter=filter,
            bc_limit=bc_limit,
            bc_offset=bc_offset,
        )
        records = _unwrap(http_status, data)
        if bc_limit is not None or bc_offset is not None:
            # BC already applied the page — return as-is with total from count cache if available.
            return {"data": records, "total": None, "bc_limit": bc_limit, "bc_offset": bc_offset}
        total = len(records)
        if limit > 0:
            records = records[skip : skip + limit]
        return {"data": records, "total": total, "skip": skip, "limit": limit}
    except HTTPException:
        raise
    except ServiceWarmingError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(e),
            headers={"Retry-After": "15"},
        )
    except Exception as e:
        logger.error(f"Error listing item prices (v3): {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_item_price_v3_router.get("/count", summary="Count Distinct Active Products (v3)")
def get_item_price_count(
    on_date: Optional[str] = Query(None, description="Count prices active on this date (YYYY-MM-DD). Defaults to BC WorkDate."),
    family_code: Optional[str] = Query(None, description="Restrict count to a single item family."),
    product_no: Optional[str] = Query(None, description="Restrict count to a single product number."),
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    """Return the total number of distinct products with an active price on on_date.

    Uses Pag50319 (itemPriceCounts) — a single BC call that counts the same product set
    as the itemPrices endpoint. Result is cached for 1 hour. Use alongside bc_limit/bc_offset
    on the itemPrices endpoint for proper server-side pagination.
    """
    import datetime
    effective_date = on_date or datetime.date.today().isoformat()
    try:
        count = rgmc_v3_get_item_price_count(
            company_name=company or config.BC_COMPANY,
            on_date=effective_date,
            family_code=family_code,
            product_no=product_no,
        )
        return {"totalCount": count, "onDate": effective_date, "familyCode": family_code}
    except Exception as e:
        logger.error(f"Error fetching item price count (v3): {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_item_price_v3_router.post("/refresh", summary="Refresh Item Price Cache (v3)", status_code=status.HTTP_202_ACCEPTED)
def refresh_cache(
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    """Invalidate the in-process v3 cache for the given company and trigger a background
    refresh. Returns 202 immediately — the refresh runs asynchronously."""
    company_name = company or config.BC_COMPANY
    rgmc_v3_invalidate_cache(company_name)
    rgmc_v3_warmup(company_name)
    return {"status": "refresh triggered", "company": company_name}


@rgmc_item_price_v3_router.get("/{item_price_id}", summary="Get Item Price by ID (v3)")
def get_item_price(
    item_price_id: str,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    try:
        http_status, data = rgmc_v3_get_item_price(item_price_id, company or config.BC_COMPANY)
        if http_status == 404:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=data)
        if http_status != 200:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"BC returned {http_status}: {data}")
        return data
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching item price {item_price_id} (v3): {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
