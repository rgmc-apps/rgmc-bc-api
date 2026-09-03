"""Internal connectivity test endpoints.

POST /internal/test/worker-ping       — publish a ping to the Pub/Sub sync topic.
GET  /internal/test/catalog-status    — report Firestore catalog record counts and last sync timestamps.
POST /internal/sync/bq-ile            — trigger BigQuery ILE sync via Pub/Sub (full MERGE, overwrites all columns).
POST /internal/sync/backfill-ile-columns — trigger ILE column backfill via Pub/Sub (COALESCE MERGE, fills NULLs only).
"""
import datetime
import logging
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query, status
from google.api_core import retry as api_retry
from google.cloud import firestore as _firestore_module

from src import config
from src.services.pubsub_publisher import publish_sync_message
from src.services.price_firestore_service import (
    _collection_name,
    _price_list_headers_collection,
    _price_list_items_collection,
    _ile_collection_name,
    _state_collection_name,
    _firestore,
    get_prices_from_firestore,
    get_price_list_headers_from_firestore,
    get_price_list_items_from_firestore,
    get_sync_state,
)

logger = logging.getLogger("bc_routes.test")

_NO_RETRY = api_retry.Retry(predicate=lambda e: False, deadline=None)

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
    """Report how many records are in each Firestore catalog collection for a company,
    plus the last sync timestamp from sync_state_{env}.

    Checks both the env this bc-api instance is configured for AND the alternate env
    (staging/production), so a worker-pool GCP_ENV mismatch shows up immediately.
    Requires X-Task-Secret header.
    """
    if x_task_secret != config.TASK_SECRET:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    company_name = company or config.BC_COMPANY
    prices_col  = _collection_name()
    headers_col = _price_list_headers_collection()
    items_col   = _price_list_items_collection()
    ile_col     = _ile_collection_name()
    state_col   = _state_collection_name()

    def _count(collection: str, comp: str) -> int:
        db = _firestore()
        from google.cloud.firestore_v1.base_query import FieldFilter
        return sum(1 for _ in db.collection(collection).where(filter=FieldFilter("company", "==", comp)).stream(retry=_NO_RETRY))

    # Also check the alternate env collection so a worker GCP_ENV mismatch is visible
    env_slug = (config.GCP_ENV or "staging").lower().replace(" ", "_")
    alt_slug = "staging" if env_slug == "production" else "production"

    try:
        result = {
            "company":      company_name,
            "gcp_env":      config.GCP_ENV,
            "gcp_project":  config.GCP_PROJECT_ID,
            "bc_company":   config.BC_COMPANY,
            "sync_state": {
                "collection":            state_col,
                "item_prices":           get_sync_state(company_name, "item_prices"),
                "price_list_headers":    get_sync_state(company_name, "price_list_headers"),
                "item_ledger_entries":   get_sync_state(company_name, "item_ledger_entries"),
            },
            "this_env": {
                "item_prices":           {"collection": prices_col,  "records": _count(prices_col,  company_name)},
                "price_list_headers":    {"collection": headers_col, "records": _count(headers_col, company_name)},
                "price_list_items":      {"collection": items_col,   "records": _count(items_col,   company_name)},
                "item_ledger_entries":   {"collection": ile_col,     "records": _count(ile_col,     company_name)},
            },
            "alt_env": {
                "item_prices":           {"collection": f"item_prices_{alt_slug}",           "records": _count(f"item_prices_{alt_slug}",           company_name)},
                "price_list_headers":    {"collection": f"price_list_headers_{alt_slug}",    "records": _count(f"price_list_headers_{alt_slug}",    company_name)},
                "price_list_items":      {"collection": f"price_list_items_{alt_slug}",      "records": _count(f"price_list_items_{alt_slug}",      company_name)},
                "item_ledger_entries":   {"collection": f"item_ledger_entries_{alt_slug}",   "records": _count(f"item_ledger_entries_{alt_slug}",   company_name)},
            },
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Firestore read failed: {e}")

    return result


@test_router.post(
    "/internal/sync/bq-ile",
    summary="Trigger BigQuery ILE Sync",
    status_code=status.HTTP_202_ACCEPTED,
    tags=["Internal"],
)
async def trigger_bq_ile_sync(
    company: Optional[str] = Query(
        "ALL",
        description="BC company code or 'ALL' (default) to sync all configured companies.",
    ),
    since_date: Optional[str] = Query(
        None,
        description=(
            "Fetch ILE records modified on or after this date (YYYY-MM-DD). "
            "Omit to auto-detect from the BigQuery table watermark. "
            "Pass an empty string to force a full sync."
        ),
    ),
    x_task_secret: str = Header("", alias="X-Task-Secret"),
):
    """Publish a bq-sync-ile message to the Pub/Sub sync topic.

    The worker pool queries each company's BigQuery itemLedgerEntries table for the
    latest lastModifiedDateTime, fetches newer records from Business Central via the
    paginated ILE endpoint, and streams them into BigQuery. A result email is sent to
    DEVELOPER_EMAIL on completion or failure. Requires X-Task-Secret header.
    """
    if x_task_secret != config.TASK_SECRET:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    payload: dict = {
        "type": "bq-sync-ile",
        "company": company or "ALL",
        "triggered_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    if since_date is not None:
        payload["since_date"] = since_date

    try:
        msg_id = publish_sync_message(payload)
        return {
            "status": "published",
            "message_id": msg_id,
            "topic": config.PUBSUB_SYNC_TOPIC,
            "payload": payload,
        }
    except Exception as e:
        logger.error(f"trigger_bq_ile_sync: failed to publish: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to publish to Pub/Sub: {e}",
        )


@test_router.post(
    "/internal/sync/backfill-ile-columns",
    summary="Trigger ILE Column Backfill (Firestore + BigQuery)",
    status_code=status.HTTP_202_ACCEPTED,
    tags=["Internal"],
)
async def trigger_backfill_ile_columns(
    company: Optional[str] = Query(
        "ALL",
        description="BC company code or 'ALL' (default) to backfill all configured companies.",
    ),
    since_date: Optional[str] = Query(
        None,
        description=(
            "Only fetch ILE records modified on or after this date (YYYY-MM-DD). "
            "Omit for a full backfill of all records."
        ),
    ),
    x_task_secret: str = Header("", alias="X-Task-Secret"),
):
    """Publish a backfill-ile-columns message to the Pub/Sub sync topic.

    The worker pool re-fetches ILE records from BC and patches the target columns
    (quantity, entryType, itemNo, sourceType, description, entryNo, postingDate,
    documentType, sourceNo, documentNo, costAmountActual, salesAmountActual,
    lastModifiedDateTime) on existing records:
      - Firestore: set-with-merge — only the target fields are written.
      - BigQuery: COALESCE MERGE — only currently-NULL target columns are filled;
        existing non-null values are never overwritten.
    Requires X-Task-Secret header.
    """
    if x_task_secret != config.TASK_SECRET:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    payload: dict = {
        "type": "backfill-ile-columns",
        "company": company or "ALL",
    }
    if since_date:
        payload["since_date"] = since_date

    try:
        msg_id = publish_sync_message(payload)
        return {
            "status": "published",
            "message_id": msg_id,
            "topic": config.PUBSUB_SYNC_TOPIC,
            "payload": payload,
        }
    except Exception as e:
        logger.error(f"trigger_backfill_ile_columns: failed to publish: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to publish to Pub/Sub: {e}",
        )
