"""RGMC custom API v3.0 — Item Price read endpoints (Pag50318) and count endpoint (Pag50319).

Item records are served from GCS (single JSON blob, ~200ms) with a 5-minute process-level
memory cache. Firestore is used only for price list headers/items (small collections) and as
a fallback when GCS is cold. Run POST /internal/firestore/routine-sync to populate Firestore;
the GCS catalog is written automatically on every full BC sync.

Single-record lookup by SystemId (/bc/custom/v3/item-prices/{id}) still reads from BC
because GCS/Firestore are keyed by company+productNo, not SystemId.
"""
import datetime
import logging
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from src.services import gcs_catalog as _gcs_catalog
from src.services.bc_functions import (
    rgmc_v3_get_item_price,
    rgmc_v3_warmup,
    rgmc_v3_invalidate_cache,
)
from src.services.price_firestore_service import (
    check_prices_exist,
    get_prices_from_firestore,
    get_active_price_list_codes_for_date,
    get_price_overrides_from_price_list_items,
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
    family_code: Optional[str] = Query(None, description="Filter by familyCode (exact match, applied in Python)."),
    price_list_code: Optional[str] = Query(None, description="Filter by priceListCode (exact match, applied in Python)."),
    on_date: Optional[str] = Query(None, description="Price-effective date (YYYY-MM-DD). When provided, only price lists active on this date are returned (via price_list_headers lookup). Defaults to today when omitted."),
    filter: Optional[str] = Query(None, description="OData $filter — not supported when reading from Firestore."),
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
    skip: int = Query(0, ge=0, description="Records to skip after fetching (Python-level)"),
    limit: int = Query(0, ge=0, description="Max records to return; 0 = all (Python-level)"),
    bc_limit: Optional[int] = Query(None, ge=0, description="Alias for limit (kept for backwards compatibility)."),
    bc_offset: Optional[int] = Query(None, ge=0, description="Alias for skip (kept for backwards compatibility)."),
):
    """Return item prices from the Firestore catalog (item_prices_{env}).

    All filtering (family_code, product_no, product_nos, price_list_code) is applied in
    Python after a single company-scoped Firestore query. OData $filter is not supported.

    Returns 503 when the catalog has not been synced yet — run
    POST /internal/firestore/routine-sync to populate it.
    """
    if filter:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OData $filter is not supported — the catalog is served from Firestore. "
                   "Use the query params (family_code, product_no, price_list_code) instead.",
        )

    try:
        nos_list = [n.strip() for n in product_nos.split(",") if n.strip()] if product_nos else None
        company_name = company or config.BC_COMPANY
        effective_date = on_date or datetime.date.today().isoformat()

        py_skip = bc_offset if bc_offset is not None else skip
        py_limit = bc_limit if bc_limit is not None else limit
        using_bc_params = bc_limit is not None or bc_offset is not None

        # Step 1: resolve which price lists are active on effective_date.
        # Wrapped in try/except so a Firestore timeout degrades gracefully —
        # item records from GCS are still served without a price override.
        try:
            active_codes = get_active_price_list_codes_for_date(
                company=company_name,
                on_date=effective_date,
                family_code=family_code,
            )
        except Exception as _e1:
            logger.warning(f"price_list_headers lookup failed (non-fatal): {_e1}")
            active_codes = []

        # Step 2: fetch base item records.
        # Primary source: GCS catalog (single blob download, ~200ms, process-cached 5 min).
        # Fallback: Firestore (used when GCS is cold or bucket not configured).
        gcs_data = _gcs_catalog.load_catalog_cached(company_name)
        gcs_has_catalog = bool(gcs_data and gcs_data.get("records"))

        if gcs_has_catalog:
            nos_set = set(nos_list) if nos_list else None
            records = []
            for rec in gcs_data["records"]:
                if rec.get("blocked") is True:
                    continue
                # Skip family_code filter for direct item lookups — productNo is already
                # a precise key; a stale or missing familyCode shouldn't hide the item.
                if family_code and not product_no and rec.get("familyCode") != family_code:
                    continue
                if product_no and not rec.get("productNo", "").startswith(product_no):
                    continue
                if nos_set is not None and rec.get("productNo") not in nos_set:
                    continue
                # price_list_code filter deferred to after overrides are applied (Step 3)
                # so stale GCS priceListCode values don't cause items to be wrongly excluded.
                records.append(rec)
            source = "gcs"
        else:
            records = get_prices_from_firestore(
                company=company_name,
                family_code=family_code,
                product_no=product_no,
                product_nos=nos_list,
                price_list_code=price_list_code,
            )
            source = "firestore"

        if not records:
            # When GCS has the catalog but a specific product_no wasn't in the blob
            # (item added after last sync, or missing familyCode at build time), fall
            # back to a direct Firestore document lookup before giving up.
            # exact_only=True prevents the slow full-company scan on a prefix miss —
            # the GCS path is the right place for prefix/search queries; Firestore
            # is only checked here for exact barcodes not yet in the GCS rebuild.
            if gcs_has_catalog and product_no:
                records = get_prices_from_firestore(
                    company=company_name,
                    product_no=product_no,
                    exact_only=True,
                )
                if records:
                    source = "firestore"

        if not records:
            if gcs_has_catalog or check_prices_exist(company_name):
                resp = {"data": [], "total": 0, "source": source}
                if using_bc_params:
                    resp.update({"bc_limit": bc_limit, "bc_offset": bc_offset})
                else:
                    resp.update({"skip": py_skip, "limit": py_limit})
                return resp
            from src.services.price_firestore_service import _collection_name
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    f"Item price catalog is empty — run POST /internal/firestore/sync-item-prices first. "
                    f"[company={company_name!r}, collection={_collection_name()!r}]"
                ),
                headers={"Retry-After": "60"},
            )

        # Step 3: overlay date-accurate prices from price_list_items_{env}.
        # price_list_items stores ALL price list lines for ALL codes; filtering to
        # active_codes gives us the prices effective on effective_date.
        # Wrapped in try/except so a Firestore timeout degrades gracefully —
        # GCS catalog prices are served as-is without the overlay.
        price_overrides_applied = 0
        if active_codes:
            try:
                product_nos_for_override = [rec.get("productNo") for rec in records if rec.get("productNo")]
                overrides = get_price_overrides_from_price_list_items(
                    company=company_name,
                    price_list_codes=active_codes,
                    product_nos=product_nos_for_override,
                )
            except Exception as _e3:
                logger.warning(f"price_list_items lookup failed (non-fatal): {_e3}")
                overrides = {}
            if overrides:
                merged = []
                for rec in records:
                    pno = rec.get("productNo") or ""
                    if pno in overrides:
                        rec = {**rec, **overrides[pno]}
                        price_overrides_applied += 1
                    merged.append(rec)
                records = merged

                # Write corrected priceListCode values back into the GCS in-memory cache
                # so subsequent reads (and the initial item list load) serve correct codes
                # without waiting for the next full BC catalog rebuild.
                if source == "gcs" and gcs_data and not product_no and not nos_list and not family_code:
                    corrected_map = {r.get("productNo"): r for r in records if r.get("productNo")}
                    updated_records = [
                        corrected_map.get(r.get("productNo"), r) for r in gcs_data["records"]
                    ]
                    _gcs_catalog.patch_catalog_records(company_name, updated_records)

        # Apply deferred price_list_code filter after overrides so stale GCS values
        # don't cause items to be wrongly excluded before correction.
        if price_list_code:
            records = [rec for rec in records if rec.get("priceListCode") == price_list_code]

        total = len(records)
        page = records[py_skip:py_skip + py_limit] if py_limit > 0 else records[py_skip:]
        resp = {
            "data": page, "total": total,
            "onDate": effective_date, "activePriceLists": active_codes,
            "priceOverridesApplied": price_overrides_applied,
            "source": source,
        }
        if using_bc_params:
            resp.update({"bc_limit": bc_limit, "bc_offset": bc_offset})
        else:
            resp.update({"skip": py_skip, "limit": py_limit})
        return resp

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing item prices (v3): {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_item_price_v3_router.get("/count", summary="Count Distinct Active Products (v3)")
def get_item_price_count(
    on_date: Optional[str] = Query(None, description="Accepted for compatibility — ignored when reading from Firestore."),
    family_code: Optional[str] = Query(None, description="Restrict count to a single item family."),
    product_no: Optional[str] = Query(None, description="Restrict count to a single product number."),
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    """Return the count of distinct products in the Firestore catalog (item_prices_{env}).

    Returns 503 when the catalog has not been synced yet.
    """
    import datetime
    effective_date = on_date or datetime.date.today().isoformat()
    company_name = company or config.BC_COMPANY

    try:
        gcs_data = _gcs_catalog.load_catalog_cached(company_name)
        gcs_has_catalog = bool(gcs_data and gcs_data.get("records"))

        if gcs_has_catalog:
            records = [
                rec for rec in gcs_data["records"]
                if rec.get("blocked") is not True
                and (not family_code or rec.get("familyCode") == family_code)
                and (not product_no or rec.get("productNo") == product_no)
            ]
            source = "gcs"
        else:
            records = get_prices_from_firestore(
                company=company_name,
                family_code=family_code,
                product_no=product_no,
            )
            source = "firestore"

        if not records:
            if not gcs_has_catalog and not check_prices_exist(company_name):
                from src.services.price_firestore_service import _collection_name
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=(
                        f"Item price catalog is empty — run POST /internal/firestore/sync-item-prices first. "
                        f"[company={company_name!r}, collection={_collection_name()!r}]"
                    ),
                    headers={"Retry-After": "60"},
                )

        return {"totalCount": len(records), "onDate": effective_date, "familyCode": family_code, "source": source}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching item price count (v3): {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_item_price_v3_router.post("/refresh", summary="Refresh Item Price Cache (v3)", status_code=status.HTTP_202_ACCEPTED)
def refresh_cache(
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    """Invalidate all in-process and GCS caches (catalog + price lists) and trigger a background refresh.

    Call this after a routine-sync so the next request picks up fresh price list data.
    To repopulate Firestore, use POST /internal/firestore/routine-sync instead.
    """
    company_name = company or config.BC_COMPANY
    rgmc_v3_invalidate_cache(company_name)
    _gcs_catalog.evict_pl_headers(company_name)
    _gcs_catalog.evict_pl_items(company_name)
    rgmc_v3_warmup(company_name)
    return {"status": "refresh triggered", "company": company_name, "evicted": ["catalog", "price_list_headers", "price_list_items"]}


@rgmc_item_price_v3_router.get("/{item_price_id}", summary="Get Item Price by ID (v3)")
def get_item_price(
    item_price_id: str,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    """Fetch a single price record by SystemId directly from BC.

    Firestore is keyed by company+productNo — lookup by SystemId requires a BC call.
    """
    import requests as _requests
    try:
        http_status, data = rgmc_v3_get_item_price(item_price_id, company or config.BC_COMPANY)
        if http_status == 404:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=data)
        if http_status != 200:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"BC returned {http_status}: {data}")
        return data
    except HTTPException:
        raise
    except _requests.exceptions.Timeout as e:
        logger.error(f"BC API timed out fetching item price {item_price_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Business Central API timed out — retry in a few seconds.",
        )
    except Exception as e:
        logger.error(f"Error fetching item price {item_price_id} (v3): {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
