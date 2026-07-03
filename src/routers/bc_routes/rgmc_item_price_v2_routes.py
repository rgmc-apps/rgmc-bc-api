"""RGMC custom API v2.0 — Item Price CRUD endpoints (Pag50210)."""
import logging
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Body, HTTPException, Query, status
from src.services.bc_functions import (
    rgmc_v2_list_item_prices,
    rgmc_v2_get_item_price,
    rgmc_v2_create_item_price,
    rgmc_v2_update_item_price,
    rgmc_v2_delete_item_price,
)
from src.models.bc_models import ItemPriceCreate, ItemPriceUpdate

logger = logging.getLogger("bc_routes.rgmc_item_prices_v2")

rgmc_item_price_v2_router = APIRouter(
    prefix="/bc/custom/v2/item-prices",
    tags=["BC RGMC Item Prices v2"],
)


def _unwrap(http_status: int, data: Any) -> List[Dict[str, Any]]:
    if http_status != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Business Central returned {http_status}: {data}",
        )
    return data.get("value", data)


@rgmc_item_price_v2_router.get("", summary="List Item Prices (v2)")
def list_item_prices(
    product_no: Optional[str] = Query(None, description="Filter by a single item No. (productNo)"),
    product_nos: Optional[str] = Query(None, description="Comma-separated list of item numbers to filter"),
    on_date: Optional[str] = Query(None, description="Filter to prices active on this date (YYYY-MM-DD)"),
    filter: Optional[str] = Query(None, description="Additional OData $filter expression"),
    company: str = Query(..., description="BC company name"),
):
    try:
        nos_list = [n.strip() for n in product_nos.split(",") if n.strip()] if product_nos else None
        http_status, data = rgmc_v2_list_item_prices(
            company_name=company,
            product_no=product_no,
            product_nos=nos_list,
            on_date=on_date,
            odata_filter=filter,
        )
        return {"data": _unwrap(http_status, data)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing item prices (v2): {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_item_price_v2_router.get("/active", summary="Get Active Price for Item on Date (v2)")
def get_active_item_price(
    product_no: str = Query(..., description="Item No."),
    on_date: str = Query(..., description="Date in YYYY-MM-DD format"),
    company: str = Query(..., description="BC company name"),
):
    """Returns the single most-recently-effective price for an item on the given date.
    A blank endingDate (stored as 0001-01-01 by BC) means open-ended — always included."""
    try:
        http_status, data = rgmc_v2_list_item_prices(
            company_name=company,
            product_no=product_no,
            on_date=on_date,
            top=1,
        )
        records = _unwrap(http_status, data)
        if not records:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No active price found for item '{product_no}' on {on_date}",
            )
        return records[0]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching active price (v2) for {product_no} on {on_date}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_item_price_v2_router.get("/{item_price_id}", summary="Get Item Price by ID (v2)")
def get_item_price(
    item_price_id: str,
    company: str = Query(..., description="BC company name"),
):
    try:
        http_status, data = rgmc_v2_get_item_price(item_price_id, company)
        if http_status == 404:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=data)
        if http_status != 200:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"BC returned {http_status}: {data}")
        return data
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching item price {item_price_id} (v2): {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_item_price_v2_router.post("", summary="Create Item Price (v2)", status_code=status.HTTP_201_CREATED)
def create_item_price(
    company: str = Query(..., description="BC company name"),
    payload: ItemPriceCreate = Body(...),
):
    try:
        http_status, data = rgmc_v2_create_item_price(payload.model_dump(exclude_none=True), company)
        if http_status not in (200, 201):
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"BC returned {http_status}: {data}")
        return data
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating item price (v2): {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_item_price_v2_router.patch("/{item_price_id}", summary="Update Item Price (v2)")
def update_item_price(
    item_price_id: str,
    company: str = Query(..., description="BC company name"),
    payload: ItemPriceUpdate = Body(...),
):
    updated_fields = payload.model_dump(exclude_none=True)
    if not updated_fields:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Request body must include at least one field to update.",
        )
    try:
        http_status, data = rgmc_v2_update_item_price(item_price_id, updated_fields, company)
        if http_status == 404:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=data)
        if http_status not in (200, 204):
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"BC returned {http_status}: {data}")
        return data
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating item price {item_price_id} (v2): {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_item_price_v2_router.delete("/{item_price_id}", summary="Delete Item Price (v2)", status_code=status.HTTP_204_NO_CONTENT)
def delete_item_price(
    item_price_id: str,
    company: str = Query(..., description="BC company name"),
):
    try:
        http_status = rgmc_v2_delete_item_price(item_price_id, company)
        if http_status == 404:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Item price '{item_price_id}' not found.")
        if http_status not in (200, 204):
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"BC returned {http_status}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting item price {item_price_id} (v2): {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
