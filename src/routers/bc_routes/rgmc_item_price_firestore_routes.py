"""Firestore-backed item price endpoints.

POST /internal/firestore/sync-item-prices  — loads full v3 catalog and writes to Firestore.
GET  /bc/custom/v3/item-prices/catalog     — reads from Firestore (for consignment app).
"""
import datetime
import logging
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query, status

from src import config
from src.services.bc_functions import ServiceWarmingError, rgmc_v3_list_item_prices
from src.services.price_firestore_service import (
    get_prices_from_firestore,
    sync_prices_to_firestore,
)

logger = logging.getLogger("bc_routes.item_price_firestore")

item_price_firestore_router = APIRouter()


@item_price_firestore_router.post(
    "/internal/firestore/sync-item-prices",
    summary="Sync Item Price Catalog to Firestore",
    tags=["Internal"],
    status_code=status.HTTP_200_OK,
)
async def sync_item_prices(
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
    on_date: Optional[str] = Query(None, description="Price date YYYY-MM-DD (defaults to today)"),
    x_task_secret: str = Header("", alias="X-Task-Secret", description="Required — must match TASK_SECRET env var"),
):
    """Load the full v3 item price catalog from BC and write every record to Firestore.

    Reads from the in-memory v3 cache (populated by the daily GCS sync). If the cache
    is cold, waits up to 55 s for a live BC fetch before returning 503.

    Idempotent — safe to call repeatedly. Intended to be triggered by Cloud Scheduler.
    Requires the `X-Task-Secret` header to match the `TASK_SECRET` environment variable.
    """
    if x_task_secret != config.TASK_SECRET:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    company_name = company or config.BC_COMPANY
    effective_date = on_date or datetime.date.today().isoformat()

    try:
        http_status, data = rgmc_v3_list_item_prices(
            company_name=company_name, on_date=effective_date
        )
        if http_status != 200:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Business Central returned {http_status}: {data}",
            )
        records = data.get("value", [])
        written = sync_prices_to_firestore(records, company_name, effective_date)
        return {
            "written": written,
            "company": company_name,
            "onDate": effective_date,
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
    tags=["BC RGMC Item Prices v3"],
)
async def get_item_price_catalog(
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
    family_code: Optional[str] = Query(None, description="Filter by familyCode (exact match)"),
    product_no: Optional[str] = Query(None, description="Filter by productNo (exact match)"),
    include_blocked: bool = Query(False, description="Include blocked items (default: false)"),
):
    """Return item prices from the Firestore catalog for the current GCP_ENV.

    Reads pre-synced Firestore data — does **not** call Business Central. Use
    `POST /internal/firestore/sync-item-prices` to populate or refresh the catalog.

    Blocked items are excluded by default. Filters are applied in Python after a
    single company-scoped Firestore query.
    """
    company_name = company or config.BC_COMPANY

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
