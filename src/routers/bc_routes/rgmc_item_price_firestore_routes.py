"""Firestore-backed item price endpoints.

POST /internal/firestore/sync-item-prices        — publishes sync-item-prices to worker pool.
POST /internal/firestore/sync-price-list-headers — publishes sync-price-list-headers to worker pool.
POST /internal/firestore/routine-sync            — publishes routine-sync to worker pool.
GET  /bc/custom/v3/item-prices/catalog           — reads from Firestore (for consignment app).
"""
import datetime
import logging
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query, status

from src import config
from src.services.price_firestore_service import get_prices_from_firestore
from src.services.pubsub_publisher import publish_sync_message

logger = logging.getLogger("bc_routes.item_price_firestore")

item_price_firestore_router = APIRouter()


@item_price_firestore_router.post(
    "/internal/firestore/sync-item-prices",
    summary="Sync Item Price Catalog to Firestore",
    tags=["Internal"],
    status_code=status.HTTP_202_ACCEPTED,
)
async def sync_item_prices(
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
    on_date: Optional[str] = Query(None, description="Price date YYYY-MM-DD (defaults to today)"),
    page_size: int = Query(500, ge=1, le=500, description="Records per Firestore write chunk (max 500 — Firestore batch limit)."),
    x_task_secret: str = Header("", alias="X-Task-Secret", description="Required — must match TASK_SECRET env var"),
):
    """Publish a sync-item-prices message to the worker pool via Pub/Sub.

    The worker pool fetches the full v3 catalog from BC and writes records to Firestore.
    Returns 202 immediately — sync runs asynchronously in the worker pool.
    Requires X-Task-Secret header.
    """
    if x_task_secret != config.TASK_SECRET:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    payload = {
        "type": "sync-item-prices",
        "company": company or config.BC_COMPANY,
        "on_date": on_date or datetime.date.today().isoformat(),
        "page_size": page_size,
    }
    msg_id = publish_sync_message(payload)
    return {"status": "published", "message_id": msg_id, "topic": config.PUBSUB_SYNC_TOPIC, "payload": payload}


@item_price_firestore_router.post(
    "/internal/firestore/sync-price-list-headers",
    summary="Sync Price List Headers to Firestore",
    tags=["Internal"],
    status_code=status.HTTP_202_ACCEPTED,
)
async def sync_price_list_headers(
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
    x_task_secret: str = Header("", alias="X-Task-Secret", description="Required — must match TASK_SECRET env var"),
):
    """Publish a sync-price-list-headers message to the worker pool via Pub/Sub.

    The worker pool fetches price list headers from BC (Pag50320) and writes them to Firestore.
    Returns 202 immediately — sync runs asynchronously in the worker pool.
    Requires X-Task-Secret header.
    """
    if x_task_secret != config.TASK_SECRET:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    payload = {
        "type": "sync-price-list-headers",
        "company": company or config.BC_COMPANY,
    }
    msg_id = publish_sync_message(payload)
    return {"status": "published", "message_id": msg_id, "topic": config.PUBSUB_SYNC_TOPIC, "payload": payload}


@item_price_firestore_router.post(
    "/internal/firestore/routine-sync",
    summary="Routine Multi-Company Firestore Sync",
    tags=["Internal"],
    status_code=status.HTTP_202_ACCEPTED,
)
async def routine_firestore_sync(
    on_date: Optional[str] = Query(None, description="Price date YYYY-MM-DD (defaults to today)"),
    x_task_secret: str = Header("", alias="X-Task-Secret", description="Required — must match TASK_SECRET env var"),
):
    """Publish a routine-sync message to the worker pool via Pub/Sub.

    The worker pool syncs price list headers and item prices for all configured companies
    (BC_COMPANIES / BC_COMPANY on the worker pool). Returns 202 immediately.
    Requires X-Task-Secret header.
    """
    if x_task_secret != config.TASK_SECRET:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    payload = {
        "type": "routine-sync",
        "on_date": on_date or datetime.date.today().isoformat(),
    }
    msg_id = publish_sync_message(payload)
    return {"status": "published", "message_id": msg_id, "topic": config.PUBSUB_SYNC_TOPIC, "payload": payload}


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
