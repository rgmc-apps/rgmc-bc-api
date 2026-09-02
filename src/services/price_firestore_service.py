"""Firestore read access for item prices, price list headers, and price list items.

Collection naming:
  item_prices_{env}           e.g. item_prices_production
  price_list_headers_{env}    e.g. price_list_headers_production
  price_list_items_{env}      e.g. price_list_items_production

Document IDs:
  item_prices          → {company}_{productNo}
  price_list_headers   → {company}_{code}
  price_list_items     → {company}_{priceListCode}_{lineNo}
"""
import logging
import threading
import time

from google.api_core import retry as api_retry
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from src import config

# Hard client-side timeout for price list Firestore queries.
# Firestore's server-side execution limit is 60 s; this ensures we fail fast
# and serve cached/GCS data rather than blocking for the full server timeout.
# Set to 30 s — the (company, priceListCode IN) compound query uses a composite
# index but may still need a few seconds on first execution after index creation.
_FAST_TIMEOUT = 30.0

# retry=None causes "NoneType has no attribute _predicate" when gRPC raises internally.
# A Retry with a False predicate satisfies the interface but never retries, and also
# avoids the grpcio>=1.67 "no attribute _retry" path that DEFAULT triggers.
_NO_RETRY = api_retry.Retry(predicate=lambda e: False, deadline=None)

logger = logging.getLogger("price_firestore_service")

_db: firestore.Client | None = None
_BATCH_SIZE = 500


def _firestore() -> firestore.Client:
    global _db
    if _db is None:
        _db = firestore.Client(project=config.GCP_PROJECT_ID)
    return _db


def _collection_name() -> str:
    env = (config.GCP_ENV or "staging").lower().replace(" ", "_")
    return f"item_prices_{env}"


def sync_prices_to_firestore(records: list, company: str, on_date: str) -> int:
    """Upsert item price records into Firestore. Returns the number of records written.

    Writes in batches of 500 (Firestore limit). Existing documents for the same
    company+productNo are overwritten. Records for products that no longer exist
    in BC are left in place — they can be identified by their stale syncedAt value.
    """
    collection = _collection_name()
    db = _firestore()
    synced_at = time.time()
    written = 0
    batch = db.batch()
    count_in_batch = 0

    for record in records:
        product_no = record.get("productNo") or ""
        if not product_no:
            continue
        doc_id = f"{company}_{product_no}"
        ref = db.collection(collection).document(doc_id)
        batch.set(ref, {
            **record,
            "company": company,
            "onDate": on_date,
            "syncedAt": synced_at,
            "env": config.GCP_ENV,
            "familyCode": record.get("familyCode") or "",
        })
        count_in_batch += 1
        written += 1
        if count_in_batch >= _BATCH_SIZE:
            batch.commit()
            batch = db.batch()
            count_in_batch = 0

    if count_in_batch > 0:
        batch.commit()

    logger.info(
        f"Synced {written} item prices to Firestore {collection!r} "
        f"(company={company!r}, onDate={on_date!r})"
    )
    return written


def backfill_family_codes(records: list, company: str) -> dict:
    """Patch the familyCode field on existing Firestore item price documents.

    Uses set(merge=True) so only familyCode is touched on existing docs.
    Company name is uppercased to match the convention used by sync_prices_to_firestore
    (which receives company from BC_COMPANY env var, always uppercase).
    Returns {"patched": int, "skipped_missing_product_no": int}.
    """
    collection = _collection_name()
    db = _firestore()
    doc_company = company.upper()
    patched = 0
    skipped = 0
    batch = db.batch()
    count_in_batch = 0

    for record in records:
        product_no = record.get("productNo") or ""
        if not product_no:
            skipped += 1
            continue
        ref = db.collection(collection).document(f"{doc_company}_{product_no}")
        batch.set(ref, {"familyCode": record.get("familyCode") or ""}, merge=True)
        count_in_batch += 1
        patched += 1
        if count_in_batch >= _BATCH_SIZE:
            batch.commit()
            batch = db.batch()
            count_in_batch = 0

    if count_in_batch > 0:
        batch.commit()

    logger.info(
        f"Backfilled familyCode on {patched} documents in {collection!r} (company={company!r})"
    )
    return {"patched": patched, "skipped_missing_product_no": skipped}


def check_prices_exist(company: str) -> bool:
    """Return True if any price records exist for this company (limit-1 probe, no full scan)."""
    collection = _collection_name()
    db = _firestore()
    probe = list(
        db.collection(collection)
        .where(filter=FieldFilter("company", "==", company))
        .limit(1)
        .stream(retry=_NO_RETRY)
    )
    return len(probe) > 0


def get_prices_from_firestore(
    company: str,
    family_code: str | None = None,
    product_no: str | None = None,
    product_nos: list | None = None,
    price_list_code: str | None = None,
    include_blocked: bool = False,
    exact_only: bool = False,
) -> list:
    """Return item prices from Firestore for the given company and current GCP_ENV.

    Lookup strategy (most to least specific):
    - product_no alone  → direct document get by ID (O(1), no index needed)
    - product_nos list  → batch document get by IDs (one RPC, no index needed)
    - family_code       → Firestore query with familyCode filter (composite index required)
    - no specific key   → full company scan (slow; avoid without a narrowing filter)

    exact_only=True stops at fast path 1 and returns [] on miss, skipping the
    slow scan/query paths. Use this when the caller has already exhausted a
    fast source (e.g. GCS) and only wants a cheap Firestore check for a single
    exact item number — not a prefix/startswith search.

    Composite index required for the family_code path:
      Collection item_prices_{env}: company ASC, familyCode ASC
    """
    collection = _collection_name()
    db = _firestore()

    def _passes(data: dict) -> bool:
        if not include_blocked and data.get("blocked") is True:
            return False
        if price_list_code and data.get("priceListCode") != price_list_code:
            return False
        return True

    # Fast path 1: try exact document ID lookup (O(1), no query needed).
    # On hit, return immediately. On miss, fall through — the query/scan paths below
    # will do startswith matching so partial search queries (e.g. "A0934") work correctly.
    # When exact_only=True, return [] immediately on miss to avoid the slow scan.
    if product_no and not product_nos:
        doc = db.collection(collection).document(f"{company}_{product_no}").get(retry=_NO_RETRY)
        if doc.exists:
            data = doc.to_dict()
            # No family_code filter — productNo is an exact key; a stale or missing
            # familyCode field shouldn't hide an item the caller explicitly asked for.
            return [data] if _passes(data) else []
        if exact_only:
            return []
        # Exact doc not found — fall through to scan with startswith filter.

    # Fast path 2: explicit product list — batch document gets by ID (one RPC).
    if product_nos:
        refs = [db.collection(collection).document(f"{company}_{no}") for no in product_nos]
        results = []
        for doc in db.get_all(refs, retry=_NO_RETRY):
            if not doc.exists:
                continue
            data = doc.to_dict()
            if family_code and data.get("familyCode") != family_code:
                continue
            if _passes(data):
                results.append(data)
        return results

    # Filtered query path.
    #
    # When family_code is provided, filter by familyCode in Firestore (single-field
    # auto-index, no composite index required) then Python-filter by company.
    # familyCode is a narrow selector (hundreds of docs vs thousands for company alone),
    # so this avoids the full-scan timeout that company-first querying causes.
    #
    # When no family_code is given, filter by company in Firestore and stream all.
    # That path is intentionally left slow — callers should avoid it on large catalogs.
    if family_code:
        query = db.collection(collection).where(filter=FieldFilter("familyCode", "==", family_code))
        results = []
        for doc in query.stream(retry=_NO_RETRY):
            data = doc.to_dict()
            if data.get("company") != company:
                continue
            if product_no and not data.get("productNo", "").startswith(product_no):
                continue
            if _passes(data):
                results.append(data)
        return results

    query = db.collection(collection).where(filter=FieldFilter("company", "==", company))
    results = []
    for doc in query.stream(retry=_NO_RETRY):
        data = doc.to_dict()
        if product_no and not data.get("productNo", "").startswith(product_no):
            continue
        if _passes(data):
            results.append(data)
    return results


def _price_list_items_to_override_map(items: list, price_list_codes: list[str], company: str | None = None) -> dict[str, dict]:
    """Convert a list of price_list_item dicts to a productNo→override map.

    Groups items by assetNo, then picks the entry whose line-level startingDate is most
    recent (latest-start wins). Header priority order (price_list_codes index) is used as
    a tiebreaker when two lines share the same startingDate. This correctly handles items
    that move from an older to a newer price list mid-year: the newer line's startingDate
    takes precedence regardless of the order Firestore documents are returned.
    """
    _BC_NULL_DATE = "0001-01-01"
    plc_set = set(price_list_codes)
    # priority_index: lower index = higher header priority (most-recently-started header first)
    priority_index = {code: i for i, code in enumerate(price_list_codes)}

    # Group items by assetNo → {priceListCode → data}
    by_asset: dict[str, dict] = {}
    for data in items:
        if company and data.get("company") != company:
            continue
        code = data.get("priceListCode")
        if code not in plc_set:
            continue
        if data.get("assetType", "Item") != "Item":
            continue
        asset_no = data.get("assetNo") or ""
        if not asset_no:
            continue
        by_asset.setdefault(asset_no, {})[code] = data

    result: dict[str, dict] = {}
    for asset_no, code_map in by_asset.items():
        best_code = None
        best_line_date = ""
        best_priority = len(price_list_codes)
        best_data = None

        for code, data in code_map.items():
            unit_price_incl = data.get("unitPriceIncVAT") or data.get("unitPrice") or data.get("unitAmount")
            if unit_price_incl is None:
                continue

            # Normalise the line-level startingDate for ISO string comparison.
            line_date = (data.get("startingDate") or "").strip()[:10]
            if line_date == _BC_NULL_DATE:
                line_date = ""
            priority = priority_index.get(code, len(price_list_codes))

            # Prefer: (1) later line startingDate, (2) lower priority index (newer header).
            if (
                best_code is None
                or line_date > best_line_date
                or (line_date == best_line_date and priority < best_priority)
            ):
                best_code = code
                best_line_date = line_date
                best_priority = priority
                best_data = data

        if best_code and best_data:
            unit_price_incl = best_data.get("unitPriceIncVAT") or best_data.get("unitPrice") or best_data.get("unitAmount")
            unit_price_excl = best_data.get("unitPrice") or best_data.get("unitAmount") or unit_price_incl
            result[asset_no] = {
                "unitPrice": unit_price_excl,
                "unitPriceIncVAT": unit_price_incl,
                "priceListCode": best_code,
            }

    return result


def get_price_overrides_from_price_list_items(
    company: str,
    price_list_codes: list[str],
    product_nos: list[str] | None = None,
) -> dict[str, dict]:
    """Return a map of productNo → {unitPrice, unitPriceIncVAT, priceListCode} for the given products.

    Queries price_list_items_{env} by assetNo IN [product_nos] (30 at a time), then
    Python-filters to price_list_codes. This targeted query is fast regardless of
    collection size — only entries for the specific products being served are fetched.

    Returns empty when product_nos is None or empty.
    Requires composite index on (company, assetNo) for price_list_items_{env}.
    """
    if not price_list_codes or not product_nos:
        return {}

    collection = _price_list_items_collection()
    db = _firestore()
    _IN_LIMIT = 30
    raw_items: list = []
    try:
        for i in range(0, len(product_nos), _IN_LIMIT):
            chunk = product_nos[i : i + _IN_LIMIT]
            q = (
                db.collection(collection)
                .where(filter=FieldFilter("company", "==", company))
                .where(filter=FieldFilter("assetNo", "in", chunk))
            )
            for doc in q.stream(retry=_NO_RETRY, timeout=_FAST_TIMEOUT):
                raw_items.append(doc.to_dict())
    except Exception as e:
        logger.warning(f"price_list_items Firestore fetch failed (non-fatal): {e}")
        return {}

    return _price_list_items_to_override_map(raw_items, price_list_codes)


def warmup_price_list_cache(company: str) -> None:
    """Pre-populate the GCS price list headers blob for cold-start performance.

    Fetches price list headers for the company from Firestore and writes them to GCS.
    Price list items are no longer pre-cached — they are queried on-demand by assetNo
    so only the exact products being served are fetched per request.

    Called at startup in a background daemon thread and from the
    POST /internal/firestore/warmup-price-lists endpoint.
    Skips headers that are already cached (GCS blob exists + memory warm).
    """
    from src.services import gcs_catalog as _gcs

    db = _firestore()

    if _gcs.load_pl_headers_cached(company) is None:
        col = _price_list_headers_collection()
        try:
            headers = [
                doc.to_dict()
                for doc in db.collection(col)
                .where(filter=FieldFilter("company", "==", company))
                .stream(retry=_NO_RETRY)
            ]
            _gcs.save_pl_headers(company, headers)
            logger.info(f"Price list headers warmed: {len(headers)} (company={company!r})")
        except Exception as e:
            logger.warning(f"warmup_price_list_cache headers failed (company={company!r}): {e}")


def get_active_price_list_codes_for_date(
    company: str,
    on_date: str,
    family_code: str | None = None,
) -> list[str]:
    """Return price list codes that are active on on_date for the given company.

    Reads from price_list_headers_{env}. A header is considered active when:
      - status == "Active"
      - priceType == "Sale"
      - startingDate <= on_date (or startingDate is empty)
      - endingDate >= on_date (or endingDate is empty)

    Returns an empty list when no headers are synced — callers should treat
    an empty result as "no filter" (fall back to all prices) rather than "no prices".
    """
    headers = get_price_list_headers_from_firestore(
        company=company,
        status="Active",
        price_type="Sale",
        item_family_code=family_code,
    )
    _BC_NULL_DATE = "0001-01-01"  # BC stores "no date" as year 0001
    code_date_pairs: list[tuple[str, str]] = []
    for h in headers:
        starting = (h.get("startingDate") or "").strip()
        ending = (h.get("endingDate") or "").strip()
        if starting == _BC_NULL_DATE:
            starting = ""
        if ending == _BC_NULL_DATE:
            ending = ""
        if starting and starting > on_date:
            continue
        if ending and ending < on_date:
            continue
        code = h.get("code")
        if code:
            code_upper = code.upper()
            parts = code_upper.split("_")
            is_ic = code_upper.startswith("IC") or (len(parts) >= 2 and parts[1].startswith("IC"))
            if not is_ic:
                code_date_pairs.append((code, starting))
    # Sort descending by startingDate so the most recently effective price list comes first.
    # _price_list_items_to_override_map picks the first code with a valid price, so this
    # ensures latest-start wins when multiple price lists cover the same product.
    code_date_pairs.sort(key=lambda x: x[1], reverse=True)
    codes = [pair[0] for pair in code_date_pairs]
    logger.info(
        f"get_active_price_list_codes_for_date: {len(headers)} headers → {len(codes)} active codes={codes!r} "
        f"(company={company!r}, on_date={on_date!r}, family_code={family_code!r})"
    )
    return codes


# ---------------------------------------------------------------------------
# Price List Headers
# ---------------------------------------------------------------------------

def _price_list_headers_collection() -> str:
    env = (config.GCP_ENV or "staging").lower().replace(" ", "_")
    return f"price_list_headers_{env}"


def _price_list_items_collection() -> str:
    env = (config.GCP_ENV or "staging").lower().replace(" ", "_")
    return f"price_list_items_{env}"


def sync_price_list_headers_to_firestore(records: list, company: str) -> int:
    """Upsert price list header records into Firestore. Returns count written.

    Document ID: {company}_{code} — code is the price list Code field.
    """
    collection = _price_list_headers_collection()
    db = _firestore()
    synced_at = time.time()
    written = 0
    batch = db.batch()
    count_in_batch = 0

    for record in records:
        code = record.get("code") or ""
        if not code:
            continue
        doc_id = f"{company}_{code}"
        ref = db.collection(collection).document(doc_id)
        batch.set(ref, {
            **record,
            "company": company,
            "syncedAt": synced_at,
            "env": config.GCP_ENV,
        })
        count_in_batch += 1
        written += 1
        if count_in_batch >= _BATCH_SIZE:
            batch.commit()
            batch = db.batch()
            count_in_batch = 0

    if count_in_batch > 0:
        batch.commit()

    logger.info(
        f"Synced {written} price list headers to Firestore {collection!r} "
        f"(company={company!r})"
    )

    # Evict the GCS/memory cache so the next read picks up fresh Firestore data.
    # Also write the new headers directly to GCS so cold-start instances don't
    # need to round-trip Firestore.
    try:
        from src.services import gcs_catalog as _gcs
        normalized = [{**r, "company": company} for r in records if r.get("code")]
        _gcs.save_pl_headers(company, normalized)
    except Exception as _e:
        logger.warning(f"GCS price list headers post-sync write failed: {_e}")

    return written


def get_price_list_headers_from_firestore(
    company: str,
    status: str | None = None,
    item_family_code: str | None = None,
    price_type: str | None = None,
) -> list:
    """Return price list headers for the given company.

    Load order: process memory cache → GCS blob → Firestore (5 s timeout).
    On a successful Firestore fetch the result is persisted to GCS in a background
    thread so the next cold-start instance reads from GCS instead of Firestore.
    Filters are applied in Python after loading all headers for the company.
    """
    from src.services import gcs_catalog as _gcs

    all_docs = _gcs.load_pl_headers_cached(company)

    if all_docs is None:
        collection = _price_list_headers_collection()
        db = _firestore()
        try:
            docs = (
                db.collection(collection)
                .where(filter=FieldFilter("company", "==", company))
                .stream(retry=_NO_RETRY, timeout=_FAST_TIMEOUT)
            )
            all_docs = [doc.to_dict() for doc in docs]
            threading.Thread(
                target=_gcs.save_pl_headers, args=(company, all_docs), daemon=True
            ).start()
        except Exception as e:
            logger.warning(f"price_list_headers Firestore fetch failed (non-fatal): {e}")
            all_docs = []

    results = []
    for data in all_docs:
        if status and data.get("status") != status:
            continue
        if item_family_code:
            header_family = (data.get("itemFamilyCode") or "").strip()
            if header_family and header_family != item_family_code:
                continue
        if price_type and data.get("priceType") != price_type:
            continue
        results.append(data)

    if not results and all_docs:
        sample = [{k: v for k, v in d.items() if k in ("code", "status", "priceType", "itemFamilyCode", "startingDate", "endingDate")} for d in all_docs[:5]]
        logger.info(
            f"price_list_headers: {len(all_docs)} docs found but all filtered out "
            f"(company={company!r}, status={status!r}, price_type={price_type!r}, "
            f"item_family_code={item_family_code!r}) — sample fields: {sample}"
        )
    elif not all_docs:
        logger.info(
            f"price_list_headers: no docs for company={company!r} (cache miss + Firestore empty/timeout)"
        )

    return results


def _state_collection_name() -> str:
    env = (config.GCP_ENV or "staging").lower().replace(" ", "_")
    return f"sync_state_{env}"


def get_sync_state(company: str, collection_type: str) -> str | None:
    """Return the UTC ISO timestamp of the last successful sync for (company, collection_type), or None."""
    db = _firestore()
    doc = db.collection(_state_collection_name()).document(f"{company}_{collection_type}").get(retry=_NO_RETRY)
    if not doc.exists:
        return None
    return doc.to_dict().get("lastSyncAt")


def _ile_collection_name() -> str:
    env = (config.GCP_ENV or "staging").lower().replace(" ", "_")
    return f"item_ledger_entries_{env}"


def get_item_ledger_entries_from_firestore(
    company: str,
    item_no: str | None = None,
    entry_type: str | None = None,
    location_code: str | None = None,
    modified_from: str | None = None,
    modified_to: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> tuple[list, int]:
    """Return item ledger entries from Firestore for the given company and current GCP_ENV.

    All filters are applied in Python after a single company-scoped query.
    modified_from / modified_to accept ISO 8601 datetime strings compared against
    the lastModifiedDateTime (SystemModifiedAt) field stored in Firestore.

    Returns (page, total) where total is the count of all matching records before
    pagination and page is the slice [offset : offset+limit].
    """
    collection = _ile_collection_name()
    db = _firestore()
    query = db.collection(collection).where(filter=FieldFilter("company", "==", company))
    if modified_from:
        query = query.where(filter=FieldFilter("lastModifiedDateTime", ">=", modified_from))
    if modified_to:
        query = query.where(filter=FieldFilter("lastModifiedDateTime", "<=", modified_to))
    docs = query.stream(retry=_NO_RETRY)
    results = []
    for doc in docs:
        data = doc.to_dict()
        if item_no and data.get("itemNo") != item_no:
            continue
        if entry_type and data.get("entryType") != entry_type:
            continue
        if location_code and data.get("locationCode") != location_code:
            continue
        results.append(data)
    total = len(results)
    start = offset or 0
    page = results[start : start + limit] if limit is not None else results[start:]
    return page, total


def get_price_list_items_from_firestore(
    company: str,
    price_list_code: str | None = None,
) -> list:
    """Return price list items from Firestore for the given company and current GCP_ENV.

    Filter by price_list_code to get items for a specific price list.
    """
    collection = _price_list_items_collection()
    db = _firestore()
    docs = db.collection(collection).where(filter=FieldFilter("company", "==", company)).stream(retry=_NO_RETRY)
    results = []
    for doc in docs:
        data = doc.to_dict()
        if price_list_code and data.get("priceListCode") != price_list_code:
            continue
        results.append(data)
    return results
