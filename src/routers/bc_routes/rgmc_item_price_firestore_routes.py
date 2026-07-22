"""Firestore-backed item price endpoints.

POST /internal/firestore/sync-item-prices  — loads full v3 catalog and writes to Firestore.
GET  /bc/custom/v3/item-prices/catalog     — reads from Firestore (for consignment app).
"""
import datetime
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, status

from src import config
from src.services.bc_functions import ServiceWarmingError, rgmc_v3_list_item_prices
from src.services.price_firestore_service import (
    get_prices_from_firestore,
    sync_prices_to_firestore,
)

logger = logging.getLogger("bc_routes.item_price_firestore")

item_price_firestore_router = APIRouter(tags=["BC RGMC Item Prices v3"])


@item_price_firestore_router.post(
    "/internal/firestore/sync-item-prices",
    include_in_schema=False,
    status_code=status.HTTP_200_OK,
)
async def sync_item_prices(request: Request):
    """Load the full v3 item price catalog from BC and write it to Firestore.

    Query params:
      company  — BC company name (defaults to BC_COMPANY env var)
      on_date  — Price date YYYY-MM-DD (defaults to today)

    Protected with X-Task-Secret header. Idempotent — safe to call repeatedly.
    Intended to be triggered by Cloud Scheduler or Cloud Tasks.
    """
    if request.headers.get("X-Task-Secret", "") != config.TASK_SECRET:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    params = dict(request.query_params)
    company_name = params.get("company") or config.BC_COMPANY
    on_date = params.get("on_date") or datetime.date.today().isoformat()

    try:
        http_status, data = rgmc_v3_list_item_prices(
            company_name=company_name, on_date=on_date
        )
        if http_status != 200:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Business Central returned {http_status}: {data}",
            )
        records = data.get("value", [])
        written = sync_prices_to_firestore(records, company_name, on_date)
        return {
            "written": written,
            "company": company_name,
            "onDate": on_date,
            "env": config.GCP_ENV,
        }
    except HTTPException:
        raise
    except ServiceWarmingError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(e),
            headers={"Retry-After": "15"},
        )
    except Exception as e:
        logger.error(f"Error syncing item prices to Firestore: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        )


@item_price_firestore_router.get(
    "/bc/custom/v3/item-prices/catalog",
    summary="Get Item Price Catalog from Firestore",
)
async def get_item_price_catalog(request: Request):
    """Return item prices from the Firestore catalog for the current GCP_ENV.

    Reads pre-synced data — does NOT call Business Central. Use
    POST /internal/firestore/sync-item-prices to populate or refresh.

    Query params:
      company         — BC company name (defaults to BC_COMPANY env var)
      family_code     — Filter by familyCode (exact match)
      product_no      — Filter by productNo (exact match)
      include_blocked — Include blocked items (default: false)
    """
    params = dict(request.query_params)
    company_name = params.get("company") or config.BC_COMPANY
    family_code: Optional[str] = params.get("family_code") or None
    product_no: Optional[str] = params.get("product_no") or None
    include_blocked: bool = params.get("include_blocked", "false").lower() == "true"

    try:
        records = get_prices_from_firestore(
            company=company_name,
            family_code=family_code,
            product_no=product_no,
            include_blocked=include_blocked,
        )
        return {
            "data": records,
            "total": len(records),
            "company": company_name,
            "env": config.GCP_ENV,
        }
    except Exception as e:
        logger.error(f"Error reading item prices from Firestore: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        )
