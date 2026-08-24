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
import time

from google.api_core import retry as api_retry
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from src import config

# retry=None causes "NoneType has no attribute _predicate" when gRPC raises internally.
# A Retry with a False predicate satisfies the interface but never retries, and also
# avoids the grpcio>=1.67 "no attribute _retry" path that DEFAULT triggers.
_NO_RETRY = api_retry.Retry(predicate=lambda e: False)

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
) -> list:
    """Return item prices from Firestore for the given company and current GCP_ENV.

    Lookup strategy (most to least specific):
    - product_no alone  → direct document get by ID (O(1), no index needed)
    - product_nos list  → batch document get by IDs (one RPC, no index needed)
    - family_code       → Firestore query with familyCode filter (composite index required)
    - no specific key   → full company scan (slow; avoid without a narrowing filter)

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

    # Fast path 1: single product — O(1) document ID lookup, no query needed.
    if product_no and not product_nos:
        doc = db.collection(collection).document(f"{company}_{product_no}").get(retry=_NO_RETRY)
        if not doc.exists:
            return []
        data = doc.to_dict()
        if family_code and data.get("familyCode") != family_code:
            return []
        return [data] if _passes(data) else []

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

    # Filtered query: push family_code to Firestore when provided to avoid a full scan.
    # Requires composite index: (company ASC, familyCode ASC) on item_prices_{env}.
    query = db.collection(collection).where(filter=FieldFilter("company", "==", company))
    if family_code:
        query = query.where(filter=FieldFilter("familyCode", "==", family_code))

    return [data for doc in query.stream(retry=_NO_RETRY) if _passes(data := doc.to_dict())]


def get_price_overrides_from_price_list_items(
    company: str,
    price_list_codes: list[str],
) -> dict[str, dict]:
    """Return a map of productNo → {unitPriceIncVAT, priceListCode} from price_list_items_{env}.

    Reads all line items for the given price list codes and returns one price entry
    per product (first matching code wins, so pass codes in preference order).
    Only "Item" asset type lines are included.

    Used to overlay date-accurate prices and priceListCodes onto item_prices records,
    which store only the price effective on the last sync date.
    """
    if not price_list_codes:
        return {}

    collection = _price_list_items_collection()
    db = _firestore()
    docs = db.collection(collection).where(filter=FieldFilter("company", "==", company)).stream(retry=_NO_RETRY)

    plc_set = set(price_list_codes)
    result: dict[str, dict] = {}
    for doc in docs:
        data = doc.to_dict()
        if data.get("priceListCode") not in plc_set:
            continue
        if data.get("assetType", "Item") != "Item":
            continue
        asset_no = data.get("assetNo") or ""
        if not asset_no or asset_no in result:
            continue
        unit_price = data.get("unitPrice") or data.get("unitAmount") or data.get("unitPriceIncVAT")
        if unit_price is None:
            continue
        result[asset_no] = {
            "unitPriceIncVAT": unit_price,
            "priceListCode": data.get("priceListCode"),
        }
    return result


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
    codes: list[str] = []
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
            codes.append(code)
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
    return written


def get_price_list_headers_from_firestore(
    company: str,
    status: str | None = None,
    item_family_code: str | None = None,
    price_type: str | None = None,
) -> list:
    """Return price list headers from Firestore for the given company and current GCP_ENV.

    Filters are applied in Python after a single company-scoped query.
    """
    collection = _price_list_headers_collection()
    db = _firestore()
    docs = db.collection(collection).where(filter=FieldFilter("company", "==", company)).stream(retry=_NO_RETRY)
    results = []
    all_docs = []
    for doc in docs:
        data = doc.to_dict()
        all_docs.append(data)
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
            f"price_list_headers: collection {collection!r} has no docs for company={company!r}"
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
