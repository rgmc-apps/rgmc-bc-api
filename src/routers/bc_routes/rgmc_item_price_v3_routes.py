"""RGMC custom API v3.0 — Item Price read endpoints (Pag50318)."""
import logging
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException, Query, status
from src.services.bc_functions import (
    rgmc_v3_list_item_prices,
    rgmc_v3_get_item_price,
    rgmc_v3_warmup,
    rgmc_v3_invalidate_cache,
)
from src import config

logger = logging.getLogger("bc_routes.rgmc_item_prices_v3")

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
):
    """Returns one record per product — the price with the highest Starting Date on or before
    on_date (BC WorkDate if omitted), excluding IC price lists. Fields include unitPriceIncVAT
    and assignToNo (not in v2)."""
    try:
        nos_list = [n.strip() for n in product_nos.split(",") if n.strip()] if product_nos else None
        http_status, data = rgmc_v3_list_item_prices(
            company_name=company or config.BC_COMPANY,
            product_no=product_no,
            product_nos=nos_list,
            family_code=family_code,
            on_date=on_date,
            odata_filter=filter,
        )
        return {"data": _unwrap(http_status, data)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing item prices (v3): {e}")
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
