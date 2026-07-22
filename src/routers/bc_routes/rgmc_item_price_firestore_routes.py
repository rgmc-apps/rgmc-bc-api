"""Firestore-backed item price endpoints.

POST /internal/firestore/sync-item-prices        — loads full v3 catalog and writes to Firestore.
POST /internal/firestore/sync-price-list-headers — loads price list headers and writes to Firestore.
POST /internal/firestore/routine-sync            — background multi-company sync (fire-and-forget).
GET  /bc/custom/v3/item-prices/catalog           — reads from Firestore (for consignment app).
"""
import datetime
import logging
import threading
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query, status

from src import config
from src.services.bc_functions import (
    ServiceWarmingError,
    rgmc_v2_list_price_list_headers,
    rgmc_v3_list_item_prices,
)
from src.services.price_firestore_service import (
    get_prices_from_firestore,
    sync_price_list_headers_to_firestore,
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
    bc_limit: Optional[int] = Query(None, ge=1, description="Fetch only this many records from BC (BC-native limit). Omit to sync the full catalog."),
    bc_offset: Optional[int] = Query(None, ge=0, description="Skip this many BC records before fetching (BC-native offset). Use with bc_limit for paged syncs."),
    x_task_secret: str = Header("", alias="X-Task-Secret", description="Required — must match TASK_SECRET env var"),
):
    """Load the v3 item price catalog from BC and write the records to Firestore.

    Without `bc_limit`/`bc_offset`: fetches the full catalog from the in-memory cache
    (populated by the GCS sync). If the cache is cold, waits up to 55 s for a live BC
    fetch. Use this for the daily scheduled full sync.

    With `bc_limit`/`bc_offset`: fetches exactly that page directly from BC (bypasses
    cache). Use for incremental / paged syncs (e.g. bc_limit=500&bc_offset=0, then
    bc_offset=500, etc.).

    Idempotent — safe to call repeatedly. Requires `X-Task-Secret` header.
    """
    if x_task_secret != config.TASK_SECRET:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    company_name = company or config.BC_COMPANY
    effective_date = on_date or datetime.date.today().isoformat()

    try:
        http_status, data = rgmc_v3_list_item_prices(
            company_name=company_name,
            on_date=effective_date,
            bc_limit=bc_limit,
            bc_offset=bc_offset,
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
            "bc_limit": bc_limit,
            "bc_offset": bc_offset,
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


@item_price_firestore_router.post(
    "/internal/firestore/sync-price-list-headers",
    summary="Sync Price List Headers to Firestore",
    tags=["Internal"],
    status_code=status.HTTP_200_OK,
)
async def sync_price_list_headers(
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
    x_task_secret: str = Header("", alias="X-Task-Secret", description="Required — must match TASK_SECRET env var"),
):
    """Load all price list headers from BC (Pag50320) and write them to Firestore.

    Fetches from BC each time — price list headers are a small dataset (< 100 records)
    and don't have an in-memory cache. Idempotent. Requires `X-Task-Secret` header.
    """
    if x_task_secret != config.TASK_SECRET:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    company_name = company or config.BC_COMPANY

    try:
        http_status, data = rgmc_v2_list_price_list_headers(company_name=company_name)
        if http_status != 200:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Business Central returned {http_status}: {data}",
            )
        records = data.get("value", [])
        written = sync_price_list_headers_to_firestore(records, company_name)
        return {
            "written": written,
            "company": company_name,
            "env": config.GCP_ENV,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error syncing price list headers to Firestore: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        )


def _routine_sync_task(companies: list, page_size: int, on_date: str) -> None:
    """Background sync: for each company, writes price list headers then item prices to Firestore.

    Item prices are fetched as a single full-catalog call (in-memory cache / GCS / live BC).
    BC-native offset pagination is intentionally avoided: Pag50318's OnOpenPage must iterate
    ALL rows up to the offset, causing 120 s timeouts at large offsets on a 3 000+ item catalog.
    Firestore writes are committed in chunks of page_size (≤ 500 per Firestore batch limit).
    """
    for company in companies:
        logger.info(f"Routine sync started for company={company!r} on_date={on_date!r}")

        # ── Step 1: price list headers ───────────────────────────────────────
        try:
            http_status, data = rgmc_v2_list_price_list_headers(company_name=company)
            if http_status == 200:
                headers = data.get("value", [])
                written = sync_price_list_headers_to_firestore(headers, company)
                logger.info(f"Routine sync [{company}]: {written} price list headers written")
            else:
                logger.error(f"Routine sync [{company}]: BC returned {http_status} for price list headers")
        except Exception as e:
            logger.error(f"Routine sync [{company}]: price list headers failed — {e}")

        # ── Step 2: item prices (full catalog → chunked Firestore writes) ────
        try:
            http_status, data = rgmc_v3_list_item_prices(
                company_name=company,
                on_date=on_date,
                # No bc_limit / bc_offset — fetches full catalog from in-memory cache.
                # BC-native offset is O(n²) on Pag50318 and causes timeouts at large offsets.
            )
            if http_status != 200:
                logger.error(f"Routine sync [{company}]: BC returned {http_status} for item prices")
                continue

            all_records = data.get("value", [])
            total_written = 0
            for i in range(0, len(all_records), page_size):
                chunk = all_records[i : i + page_size]
                total_written += sync_prices_to_firestore(chunk, company, on_date)
                logger.info(
                    f"Routine sync [{company}]: item prices page {i // page_size + 1} "
                    f"({len(chunk)} records, offset={i})"
                )
            logger.info(f"Routine sync [{company}]: {total_written} item prices written total")
        except Exception as e:
            logger.error(f"Routine sync [{company}]: item prices failed — {e}")

        logger.info(f"Routine sync complete for company={company!r}")


@item_price_firestore_router.post(
    "/internal/firestore/routine-sync",
    summary="Routine Multi-Company Firestore Sync",
    tags=["Internal"],
    status_code=status.HTTP_202_ACCEPTED,
)
async def routine_firestore_sync(
    page_size: int = Query(500, ge=1, le=500, description="Records per Firestore write chunk (max 500 — Firestore batch limit)."),
    on_date: Optional[str] = Query(None, description="Price date YYYY-MM-DD (defaults to today)"),
    x_task_secret: str = Header("", alias="X-Task-Secret", description="Required — must match TASK_SECRET env var"),
):
    """Fire-and-forget sync of price list headers and item prices to Firestore for all companies.

    Reads `BC_COMPANIES` (falls back to `BC_COMPANY`) to determine which companies to sync.
    Returns **202 immediately** — the sync runs in a background daemon thread.

    Per company the sync runs in order:
    1. Fetch all price list headers from BC → write to `price_list_headers_{env}`
    2. Fetch full item price catalog (in-memory cache → GCS → live BC) → write to
       `item_prices_{env}` in chunks of `page_size`

    Progress is logged to Cloud Logging. Requires `X-Task-Secret` header.
    """
    if x_task_secret != config.TASK_SECRET:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    companies = [
        c.strip()
        for c in (config.BC_COMPANIES or config.BC_COMPANY or "").split(",")
        if c.strip()
    ]
    if not companies:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No companies configured. Set BC_COMPANIES or BC_COMPANY env var.",
        )

    effective_date = on_date or datetime.date.today().isoformat()

    threading.Thread(
        target=_routine_sync_task,
        args=(companies, page_size, effective_date),
        daemon=True,
    ).start()

    return {
        "status": "started",
        "companies": companies,
        "page_size": page_size,
        "on_date": effective_date,
        "env": config.GCP_ENV,
    }


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
