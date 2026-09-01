"""Firestore-backed item price and item ledger endpoints.

POST /internal/firestore/sync-item-prices           — publishes sync-item-prices to worker pool.
POST /internal/firestore/sync-price-list-headers    — publishes sync-price-list-headers to worker pool.
POST /internal/firestore/sync-price-list-items      — publishes sync-price-list-items to worker pool.
POST /internal/firestore/sync-item-ledger-entries   — publishes sync-item-ledger-entries to worker pool.
POST /internal/firestore/routine-sync               — publishes routine-sync to worker pool.
POST /internal/firestore/warmup-price-lists         — reads price list data from Firestore → writes GCS blobs.
GET  /bc/custom/v3/item-prices/catalog              — reads item prices from Firestore.
GET  /bc/custom/v2/price-list-items                 — reads price list items from Firestore.
"""
import datetime
import logging
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query, status

import datetime

import threading

from src import config
from src.services.price_firestore_service import (
    backfill_family_codes,
    get_price_list_items_from_firestore,
    get_prices_from_firestore,
    sync_prices_to_firestore,
    warmup_price_list_cache,
)
from src.services.pubsub_publisher import publish_sync_message
from src.services.bc_functions import rgmc_v3_fetch_catalog_direct

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
    "/internal/firestore/sync-price-list-items",
    summary="Sync Price List Items to Firestore",
    tags=["Internal"],
    status_code=status.HTTP_202_ACCEPTED,
)
async def sync_price_list_items(
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
    price_list_code: Optional[str] = Query(None, description="Sync only this price list code (omit to sync all codes for the company)"),
    x_task_secret: str = Header("", alias="X-Task-Secret", description="Required — must match TASK_SECRET env var"),
):
    """Publish a sync-price-list-items message to the worker pool via Pub/Sub.

    The worker pool fetches priceListHeaders with expanded priceListLines from BC
    and writes each line to the price_list_items_{env} Firestore collection.
    Returns 202 immediately — sync runs asynchronously in the worker pool.
    Requires X-Task-Secret header.
    """
    if x_task_secret != config.TASK_SECRET:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    payload: dict = {
        "type": "sync-price-list-items",
        "company": company or config.BC_COMPANY,
    }
    if price_list_code:
        payload["price_list_code"] = price_list_code
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

    The worker pool syncs price list headers, item prices, and item ledger entries
    for all configured companies (BC_COMPANIES / BC_COMPANY on the worker pool).
    Returns 202 immediately.
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


@item_price_firestore_router.post(
    "/internal/firestore/sync-item-ledger-entries",
    summary="Sync Item Ledger Entries to Firestore",
    tags=["Internal"],
    status_code=status.HTTP_202_ACCEPTED,
)
async def sync_item_ledger_entries(
    company: Optional[str] = Query(None, description="BC company name, or 'ALL' to sync all companies (default). Worker pool expands 'ALL' from BC_COMPANIES env var or BC's company list."),
    since_date: Optional[str] = Query(None, description="Only fetch records modified on or after this date (YYYY-MM-DD). Omit for full sync."),
    x_task_secret: str = Header("", alias="X-Task-Secret", description="Required — must match TASK_SECRET env var"),
):
    """Publish a sync-item-ledger-entries message to the worker pool via Pub/Sub.

    The worker pool fetches item ledger entries from BC (Pag50339) using limit/offset
    pagination (5,000 records per page) and writes records to Firestore.
    When company is 'ALL' (default), the worker pool expands the list from the
    BC_COMPANIES env var, falling back to fetching all companies from BC if not set.
    Returns 202 immediately — sync runs asynchronously in the worker pool.
    Requires X-Task-Secret header.
    """
    if x_task_secret != config.TASK_SECRET:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    payload: dict = {
        "type": "sync-item-ledger-entries",
        "company": company or "ALL",
    }
    if since_date:
        payload["since_date"] = since_date
    msg_id = publish_sync_message(payload)
    return {"status": "published", "message_id": msg_id, "topic": config.PUBSUB_SYNC_TOPIC, "payload": payload}


@item_price_firestore_router.post(
    "/internal/firestore/sync-item-prices-direct",
    summary="Direct BC→Firestore Item Price Sync (bypasses worker pool)",
    tags=["Internal"],
    status_code=status.HTTP_200_OK,
)
async def sync_item_prices_direct(
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
    on_date: Optional[str] = Query(None, description="Price date YYYY-MM-DD (defaults to today)"),
    x_task_secret: str = Header("", alias="X-Task-Secret", description="Required — must match TASK_SECRET env var"),
):
    """Fetch the v3 item price catalog directly from BC and write it to Firestore.

    Bypasses the worker pool entirely — useful for diagnosing worker pool issues or
    for a one-off manual sync. Uses the same 4-range parallel BC fetch as the worker pool.
    Returns synchronously with the count of records written.
    Requires X-Task-Secret header.
    """
    if x_task_secret != config.TASK_SECRET:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    company_name = company or config.BC_COMPANY
    effective_date = on_date or datetime.date.today().isoformat()

    try:
        records = rgmc_v3_fetch_catalog_direct(company_name, effective_date)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.error(f"Direct sync BC fetch failed (company={company_name!r}): {e}")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"BC fetch failed: {e}")

    if not records:
        return {
            "status": "ok",
            "company": company_name,
            "on_date": effective_date,
            "bc_records": 0,
            "written": 0,
            "warning": "BC returned 0 prices for this company and date — Firestore unchanged.",
        }

    try:
        written = sync_prices_to_firestore(records, company_name, effective_date)
    except Exception as e:
        logger.error(f"Direct sync Firestore write failed (company={company_name!r}): {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Firestore write failed: {e}")

    return {
        "status": "ok",
        "company": company_name,
        "on_date": effective_date,
        "bc_records": len(records),
        "written": written,
    }


@item_price_firestore_router.post(
    "/internal/firestore/backfill-family-codes",
    summary="Patch familyCode on existing Firestore item price documents",
    tags=["Internal"],
    status_code=status.HTTP_200_OK,
)
async def backfill_family_codes_endpoint(
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
    on_date: Optional[str] = Query(None, description="Price date YYYY-MM-DD (defaults to today)"),
    x_task_secret: str = Header("", alias="X-Task-Secret", description="Required — must match TASK_SECRET env var"),
):
    """Fetch the full item catalog from BC and patch only the familyCode field on
    existing Firestore documents. All other fields (price, description, syncedAt, etc.)
    are left untouched. Documents that do not yet exist in Firestore are skipped.

    Use this to fix documents written before familyCode was explicitly persisted by
    the worker pool, without triggering a full re-sync of all price data.
    Requires X-Task-Secret header.
    """
    if x_task_secret != config.TASK_SECRET:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    company_name = company or config.BC_COMPANY
    effective_date = on_date or datetime.date.today().isoformat()

    try:
        records = rgmc_v3_fetch_catalog_direct(company_name, effective_date)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.error(f"Backfill BC fetch failed (company={company_name!r}): {e}")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"BC fetch failed: {e}")

    if not records:
        return {
            "status": "ok",
            "company": company_name,
            "on_date": effective_date,
            "bc_records": 0,
            "patched": 0,
            "warning": "BC returned 0 prices for this company and date — Firestore unchanged.",
        }

    try:
        result = backfill_family_codes(records, company_name)
    except Exception as e:
        logger.error(f"Backfill Firestore update failed (company={company_name!r}): {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Firestore update failed: {e}")

    return {
        "status": "ok",
        "company": company_name,
        "on_date": effective_date,
        "bc_records": len(records),
        **result,
    }


@item_price_firestore_router.post(
    "/internal/firestore/warmup-price-lists",
    summary="Pre-populate GCS Price List Cache from Firestore",
    tags=["Internal"],
    status_code=status.HTTP_202_ACCEPTED,
)
async def warmup_price_lists(
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
    x_task_secret: str = Header("", alias="X-Task-Secret", description="Required — must match TASK_SECRET env var"),
):
    """Read price list headers and items from Firestore and write them to GCS blobs.

    This seeds the GCS cache used by GET /bc/custom/v3/item-prices so that cold-start
    instances serve correct date-accurate price overrides without Firestore timeouts.
    Runs asynchronously in a daemon thread — returns 202 immediately.
    Call this after a routine-sync or whenever the price list data changes.
    Requires X-Task-Secret header.
    """
    if x_task_secret != config.TASK_SECRET:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    company_name = company or config.BC_COMPANY
    threading.Thread(
        target=warmup_price_list_cache, args=(company_name,), daemon=True,
        name=f"pl-warmup-manual-{company_name}"
    ).start()
    return {"status": "warmup triggered", "company": company_name}


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


@item_price_firestore_router.get(
    "/bc/custom/v2/price-list-items",
    summary="Get Price List Items from Firestore",
    tags=["BC RGMC Price List Headers v2"],
)
async def get_price_list_items(
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
    price_list_code: Optional[str] = Query(None, description="Filter by priceListCode (exact match)"),
):
    """Return price list line items from the Firestore catalog for the current GCP_ENV.

    Reads pre-synced data — does **not** call Business Central. Use
    `POST /internal/firestore/sync-price-list-items` to populate or refresh.
    Filter by price_list_code to get items for a specific price list.
    """
    company_name = company or config.BC_COMPANY

    try:
        records = get_price_list_items_from_firestore(
            company=company_name,
            price_list_code=price_list_code,
        )
        return {
            "data": records,
            "total": len(records),
            "company": company_name,
            "price_list_code": price_list_code,
            "env": config.GCP_ENV,
        }
    except Exception as e:
        logger.error(f"Error reading price list items from Firestore: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        )
