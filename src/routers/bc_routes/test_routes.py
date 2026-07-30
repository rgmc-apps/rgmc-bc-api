"""Internal connectivity test endpoints.

POST /internal/test/worker-ping       — publish a ping to the Pub/Sub sync topic.
GET  /internal/test/catalog-status    — report Firestore catalog record counts.
"""
import datetime
import logging
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query, status

from src import config
from src.services.pubsub_publisher import publish_sync_message
from src.services.price_firestore_service import (
    _collection_name,
    _price_list_headers_collection,
    _price_list_items_collection,
    get_prices_from_firestore,
    get_price_list_headers_from_firestore,
    get_price_list_items_from_firestore,
)

logger = logging.getLogger("bc_routes.test")

test_router = APIRouter(tags=["Internal"])


@test_router.post(
    "/internal/test/worker-ping",
    summary="Ping Worker Pool via Pub/Sub",
    status_code=status.HTTP_202_ACCEPTED,
)
async def worker_ping(
    note: Optional[str] = Query(None, description="Optional note to include in the ping payload"),
    x_task_secret: str = Header("", alias="X-Task-Secret"),
):
    """Publish a ping message to the Pub/Sub sync topic.

    The worker pool receives the message and sends a confirmation email to
    DEVELOPER_EMAIL. Use this to verify end-to-end bc-api → Pub/Sub → worker pool
    connectivity. Requires X-Task-Secret header.
    """
    if x_task_secret != config.TASK_SECRET:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    payload: dict = {
        "type": "ping",
        "sent_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "sent_by": "bc-api",
    }
    if note:
        payload["note"] = note

    try:
        msg_id = publish_sync_message(payload)
        return {
            "status": "published",
            "message_id": msg_id,
            "topic": config.PUBSUB_SYNC_TOPIC,
            "payload": payload,
        }
    except Exception as e:
        logger.error(f"worker-ping: failed to publish: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to publish to Pub/Sub: {e}",
        )


@test_router.get(
    "/internal/test/catalog-status",
    summary="Catalog Firestore Status",
    tags=["Internal"],
)
async def catalog_status(
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
    x_task_secret: str = Header("", alias="X-Task-Secret"),
):
    """Report how many records are in each Firestore catalog collection for a company.

    Use this to diagnose empty-catalog errors — shows exact collection names and record
    counts so any company-name or env-slug mismatch is immediately visible.
    Requires X-Task-Secret header.
    """
    if x_task_secret != config.TASK_SECRET:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    company_name = company or config.BC_COMPANY
    prices_col     = _collection_name()
    headers_col    = _price_list_headers_collection()
    items_col      = _price_list_items_collection()

    try:
        prices_count  = len(get_prices_from_firestore(company=company_name, include_blocked=True))
        headers_count = len(get_price_list_headers_from_firestore(company=company_name))
        items_count   = len(get_price_list_items_from_firestore(company=company_name))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Firestore read failed: {e}")

    return {
        "company":      company_name,
        "gcp_env":      config.GCP_ENV,
        "gcp_project":  config.GCP_PROJECT_ID,
        "collections": {
            "item_prices":        {"name": prices_col,  "records_for_company": prices_count},
            "price_list_headers": {"name": headers_col, "records_for_company": headers_count},
            "price_list_items":   {"name": items_col,   "records_for_company": items_count},
        },
    }
