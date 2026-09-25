"""RGMC custom API v3.0 — Item Price read endpoints (Pag50318) and count endpoint (Pag50319).

Item records are served from the GCS blobs the worker pool publishes every sync:
  families/{FAMILY}.json  — one family in the exact response shape of this endpoint,
                            price-list overlay already applied for the sync date
  search_index.json       — [productNo, description, family] for every item; resolves
                            barcode/substring searches and product_nos batches to blobs
  catalog.json            — full catalog (legacy fallback only)
All are cached in-process by GCS generation (see gcs_catalog.py).

Request shapes, fastest first:
  family_code + on_date == sync date, no other params  → stored gzip bytes returned as-is
  family_code (+ modified_since / paging / other date)  → parsed family blob, filtered
  product_no / product_nos                              → search index → family blob(s)
  anything else                                         → full catalog blob (legacy)

A request for a posting date other than the sync date re-applies the overlay from the
compact price_overrides.json index. Firestore is consulted only for single-product
lookups (the /sync endpoint writes there first) and as a fallback when no blob exists.

Single-record lookup by SystemId (/bc/custom/v3/item-prices/{id}) still reads from BC
because GCS/Firestore are keyed by company+productNo, not SystemId.
"""
import datetime
import logging
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import ORJSONResponse
from pydantic import BaseModel
from src.services import gcs_catalog as _gcs_catalog
from src.services.bc_functions import (
    rgmc_v3_get_item_price,
    rgmc_v3_list_item_prices,
    rgmc_v3_invalidate_cache,
)
from src.services.pubsub_publisher import publish_sync_message
from src.services.price_firestore_service import (
    check_prices_exist,
    get_prices_from_firestore,
    get_active_price_list_codes_for_date,
    get_price_overrides_from_price_list_items,
)
from src import config

logger = logging.getLogger("bc_routes.rgmc_item_prices_v3")

# Cap on substring-search hits — matches BC's own contains() page size and keeps a
# one-character query from returning the whole catalog.
_SEARCH_MAX = 500


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


def _modified_key(rec: dict) -> str:
    """Latest of BC's lastModifiedDateTime and the worker's priceChangedAt.

    BC does not bump lastModifiedDateTime when only a price list changes, so the worker
    stamps priceChangedAt on records whose overlaid price moved; delta syncs must see both.
    """
    return max(rec.get("lastModifiedDateTime") or "", rec.get("priceChangedAt") or "")


def _active_codes(company_name: str, effective_date: str, family_code: Optional[str]) -> list:
    """Price lists active on effective_date; a Firestore/GCS hiccup degrades to no overlay."""
    try:
        return get_active_price_list_codes_for_date(
            company=company_name, on_date=effective_date, family_code=family_code,
        )
    except Exception as e:
        logger.warning(f"price_list_headers lookup failed (non-fatal): {e}")
        return []


def _apply_overrides(records: list, company_name: str, active_codes: list) -> tuple[list, int]:
    """Overlay date-specific price list prices onto records. Returns (records, applied_count)."""
    if not active_codes or not records:
        return records, 0
    try:
        overrides = get_price_overrides_from_price_list_items(
            company=company_name,
            price_list_codes=active_codes,
            product_nos=[rec.get("productNo") for rec in records if rec.get("productNo")],
        )
    except Exception as e:
        logger.warning(f"price overrides lookup failed (non-fatal): {e}")
        return records, 0
    if not overrides:
        return records, 0
    merged = []
    applied = 0
    for rec in records:
        ov = overrides.get(rec.get("productNo") or "")
        if ov:
            rec = {**rec, **ov}
            applied += 1
        merged.append(rec)
    return merged, applied


def _page(records: list, skip: int, limit: int) -> list:
    return records[skip:skip + limit] if limit > 0 else records[skip:]


def _search_via_index(
    company_name: str,
    sidx: dict,
    product_no: Optional[str],
    nos_list: Optional[list],
    family_code: Optional[str],
    modified_since: Optional[str],
) -> Optional[list]:
    """Resolve a product_no substring search or a product_nos batch through the search
    index and the family blobs it points at. Returns None when the request is a full
    company listing (caller falls back to the legacy catalog path)."""
    wanted: dict[str, set] = {}
    if product_no:
        # BC product numbers are upper-case; descriptions are stored lower-cased in the index.
        q_upper, q_lower = product_no.upper(), product_no.lower()
        hits = 0
        for pno, desc, fam in sidx.get("items") or []:
            if q_upper in pno or q_lower in desc:
                wanted.setdefault(fam, set()).add(pno)
                hits += 1
                if hits >= _SEARCH_MAX:
                    break
    elif nos_list:
        by_pno = sidx.get("by_pno") or {}
        for no in nos_list:
            fam = by_pno.get(no)
            if fam is not None and (not family_code or fam == family_code):
                wanted.setdefault(fam, set()).add(no)
    else:
        return None

    out: list = []
    for fam, nos in wanted.items():
        for rec in _gcs_catalog.family_records(company_name, fam) or []:
            if rec.get("productNo") in nos and (not modified_since or _modified_key(rec) > modified_since):
                out.append(rec)
    return out


@rgmc_item_price_v3_router.get("", summary="List Item Prices (v3)")
def list_item_prices(
    request: Request,
    product_no: Optional[str] = Query(None, description="Filter by a single item No. (productNo)"),
    product_nos: Optional[str] = Query(None, description="Comma-separated list of item numbers to filter"),
    family_code: Optional[str] = Query(None, description="Filter by familyCode (exact match, applied in Python)."),
    price_list_code: Optional[str] = Query(None, description="Filter by priceListCode (exact match, applied in Python)."),
    on_date: Optional[str] = Query(None, description="Price-effective date (YYYY-MM-DD). When provided, only price lists active on this date are returned (via price_list_headers lookup). Defaults to today when omitted."),
    modified_since: Optional[str] = Query(None, description="ISO 8601 datetime — when set, only records with lastModifiedDateTime (or priceChangedAt) > this value are returned. Used by the webapp for incremental syncs; clients merge the result into their existing cache."),
    filter: Optional[str] = Query(None, description="OData $filter — not supported when reading from the catalog blobs."),
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
    skip: int = Query(0, ge=0, description="Records to skip after fetching (Python-level)"),
    limit: int = Query(0, ge=0, description="Max records to return; 0 = all (Python-level)"),
    bc_limit: Optional[int] = Query(None, ge=0, description="Alias for limit (kept for backwards compatibility)."),
    bc_offset: Optional[int] = Query(None, ge=0, description="Alias for skip (kept for backwards compatibility)."),
):
    """Return item prices from the published catalog blobs.

    All filtering (family_code, product_no, product_nos, price_list_code) is applied in
    Python. OData $filter is not supported.

    Returns 503 when the catalog has not been synced yet — run
    POST /internal/firestore/routine-sync to populate it.
    """
    if filter:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OData $filter is not supported — the catalog is served from GCS. "
                   "Use the query params (family_code, product_no, price_list_code) instead.",
        )

    try:
        nos_list = [n.strip() for n in product_nos.split(",") if n.strip()] if product_nos else None
        company_name = company or config.BC_COMPANY
        effective_date = on_date or datetime.date.today().isoformat()

        py_skip = bc_offset if bc_offset is not None else skip
        py_limit = bc_limit if bc_limit is not None else limit
        using_bc_params = bc_limit is not None or bc_offset is not None

        def _respond(records: list, source: str, active_codes: list, applied: int) -> ORJSONResponse:
            if price_list_code:
                records = [rec for rec in records if rec.get("priceListCode") == price_list_code]
            resp = {
                "data": _page(records, py_skip, py_limit), "total": len(records),
                "onDate": effective_date, "activePriceLists": active_codes,
                "priceOverridesApplied": applied, "source": source,
            }
            if using_bc_params:
                resp.update({"bc_limit": bc_limit, "bc_offset": bc_offset})
            else:
                resp.update({"skip": py_skip, "limit": py_limit})
            # Returning the response object directly skips FastAPI's jsonable_encoder walk
            # over every record — for a 5 000-item family that walk cost more than the query.
            return ORJSONResponse(resp)

        family_index = _gcs_catalog.load_family_index(company_name)
        families = (family_index or {}).get("families") or {}
        blob_overlay_date = (family_index or {}).get("overlay_on_date")

        # ── Fast path: one family from its own blob ────────────────────────────
        # This is the call every device makes on sync; it must never touch the full
        # catalog, price list lines, or Firestore.
        if family_code and not product_no and not nos_list:
            plain = (
                not modified_since and not price_list_code and not using_bc_params
                and py_skip == 0 and py_limit == 0
            )
            if (
                plain and family_code in families and blob_overlay_date == effective_date
                and "gzip" in request.headers.get("accept-encoding", "").lower()
            ):
                raw = _gcs_catalog.family_response_gzip(company_name, family_code)
                if raw is not None:
                    # The blob is already this endpoint's response body, gzip-encoded.
                    return Response(
                        content=raw,
                        media_type="application/json",
                        headers={"Content-Encoding": "gzip", "Vary": "Accept-Encoding", "X-Source": "gcs_family_raw"},
                    )
            fam = _gcs_catalog.load_family_catalog(company_name, family_code)
            if fam is not None:
                records = fam.get("data") or fam.get("records") or []
                if modified_since:
                    records = [rec for rec in records if _modified_key(rec) > modified_since]
                active_codes: list = []
                applied = 0
                if fam.get("overlay_on_date") != effective_date:
                    active_codes = _active_codes(company_name, effective_date, family_code)
                    records, applied = _apply_overrides(records, company_name, active_codes)
                return _respond(records, "gcs_family", active_codes, applied)
            if family_index is not None and family_code not in families:
                # Index exists but this family has no blob → the family genuinely has no items.
                return _respond([], "gcs_family", [], 0)

        # ── Product lookups via the search index ───────────────────────────────
        sidx = _gcs_catalog.load_search_index(company_name) if (product_no or nos_list) else None
        have_index = sidx is not None

        # ── Legacy full-catalog path (only when the index can't answer) ────────
        gcs_data = None if have_index else _gcs_catalog.load_catalog_cached(company_name)
        gcs_has_catalog = bool(gcs_data and gcs_data.get("records"))
        if not have_index and gcs_data:
            blob_overlay_date = gcs_data.get("overlay_on_date")
        catalog_available = have_index or gcs_has_catalog

        _pno_lower = product_no.lower() if product_no else None

        records = []
        source = "gcs"

        # Single-item lookups read Firestore first: the /sync endpoint writes corrected
        # prices there but not to the GCS blob, so Firestore is authoritative per item.
        if product_no and catalog_available:
            records = get_prices_from_firestore(
                company=company_name,
                product_no=product_no,
                exact_only=True,
            )
            if records:
                source = "firestore"

        if not records:
            if have_index:
                found = _search_via_index(company_name, sidx, product_no, nos_list, family_code, modified_since)
                if found is None:
                    gcs_data = _gcs_catalog.load_catalog_cached(company_name)
                    gcs_has_catalog = bool(gcs_data and gcs_data.get("records"))
                    if gcs_data:
                        blob_overlay_date = gcs_data.get("overlay_on_date")
                else:
                    records = found
            if not records and gcs_has_catalog:
                nos_set = set(nos_list) if nos_list else None
                for rec in gcs_data["records"]:
                    if rec.get("blocked") is True:
                        continue
                    # Skip family_code filter for direct item lookups — productNo is already
                    # a precise key; a stale or missing familyCode shouldn't hide the item.
                    if family_code and not product_no and rec.get("familyCode") != family_code:
                        continue
                    # Substring (contains) match — case-insensitive so "green" finds "DARK GREEN",
                    # and "41400" finds "A093414000102". Checks productNo and description.
                    if _pno_lower:
                        pno = rec.get("productNo", "").lower()
                        desc = rec.get("description", "").lower()
                        if _pno_lower not in pno and _pno_lower not in desc:
                            continue
                    if nos_set is not None and rec.get("productNo") not in nos_set:
                        continue
                    if modified_since and _modified_key(rec) <= modified_since:
                        continue
                    records.append(rec)
                source = "gcs"
            elif not records and not catalog_available:
                records = get_prices_from_firestore(
                    company=company_name,
                    family_code=family_code,
                    product_no=product_no,
                    product_nos=nos_list,
                    price_list_code=price_list_code,
                )
                source = "firestore"

        if not records:
            # Item wasn't in the blob (added after last sync) — prefix range query on Firestore.
            if catalog_available and product_no:
                records = get_prices_from_firestore(
                    company=company_name,
                    product_no=product_no,
                )
                if records:
                    source = "firestore"

        # Live BC contains-search fallback so items added after the last catalog sync
        # are still findable by substring.
        if not records and product_no:
            try:
                pno_esc = product_no.replace("'", "''")
                odata = f"contains(productNo,'{pno_esc}') or contains(description,'{pno_esc}')"
                _bc_status, _bc_data = rgmc_v3_list_item_prices(
                    company_name,
                    odata_filter=odata,
                    on_date=effective_date,
                )
                if _bc_status == 200:
                    bc_live = _bc_data.get("value", [])
                    if family_code:
                        bc_live = [r for r in bc_live if r.get("familyCode") == family_code]
                    if bc_live:
                        records = bc_live
                        source = "bc_live"
            except Exception as _e_live:
                logger.warning(f"Live BC contains search failed for {product_no!r}: {_e_live}")

        if not records:
            if catalog_available or check_prices_exist(company_name):
                return _respond([], source, [], 0)
            from src.services.price_firestore_service import _collection_name
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    f"Item price catalog is empty — run POST /internal/firestore/sync-item-prices first. "
                    f"[company={company_name!r}, collection={_collection_name()!r}]"
                ),
                headers={"Retry-After": "60"},
            )

        # Overlay date-accurate prices unless the blobs were already overlaid for this date.
        # Skipped for Firestore single-item hits: the /sync endpoint stored BC's authoritative,
        # already date-adjusted price there.
        overlay_done = source == "gcs" and blob_overlay_date == effective_date
        active_codes = []
        applied = 0
        if not overlay_done and not (source == "firestore" and product_no):
            active_codes = _active_codes(company_name, effective_date, family_code)
            records, applied = _apply_overrides(records, company_name, active_codes)

        return _respond(records, source, active_codes, applied)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing item prices (v3): {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_item_price_v3_router.get("/count", summary="Count Distinct Active Products (v3)")
def get_item_price_count(
    on_date: Optional[str] = Query(None, description="Accepted for compatibility — ignored when reading from the catalog."),
    family_code: Optional[str] = Query(None, description="Restrict count to a single item family."),
    product_no: Optional[str] = Query(None, description="Restrict count to a single product number."),
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    """Return the count of distinct products in the published catalog.

    Returns 503 when the catalog has not been synced yet.
    """
    effective_date = on_date or datetime.date.today().isoformat()
    company_name = company or config.BC_COMPANY

    try:
        index = _gcs_catalog.load_family_index(company_name)
        families = (index or {}).get("families") or {}
        if index is not None and not product_no:
            count = families.get(family_code, 0) if family_code else sum(families.values())
            return {"totalCount": count, "onDate": effective_date, "familyCode": family_code, "source": "gcs_index"}
        if index is not None and product_no:
            sidx = _gcs_catalog.load_search_index(company_name)
            if sidx is not None:
                fam = (sidx.get("by_pno") or {}).get(product_no.upper())
                count = 1 if fam is not None and (not family_code or fam == family_code) else 0
                return {"totalCount": count, "onDate": effective_date, "familyCode": family_code, "source": "gcs_index"}

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
    """Drop all in-process caches for the company (BC catalog + every GCS blob) and ask the
    worker pool to rebuild the published blobs.

    To repopulate Firestore as well, use POST /internal/firestore/routine-sync instead.
    """
    company_name = company or config.BC_COMPANY
    rgmc_v3_invalidate_cache(company_name)
    _gcs_catalog.evict_company(company_name)
    # The worker pool rebuilds the blobs; the API no longer pulls the whole catalog itself.
    msg_id = publish_sync_message({
        "type": "sync-item-prices",
        "company": company_name,
        "on_date": datetime.date.today().isoformat(),
    })
    return {"status": "refresh triggered", "company": company_name, "published": msg_id,
            "evicted": ["catalog", "families", "search_index", "price_list_headers", "price_overrides"]}


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
