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

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from src import config

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


def get_prices_from_firestore(
    company: str,
    family_code: str | None = None,
    product_no: str | None = None,
    product_nos: list | None = None,
    price_list_code: str | None = None,
    include_blocked: bool = False,
) -> list:
    """Return item prices from Firestore for the given company and current GCP_ENV.

    Reads from item_prices_{env} — one record per product, storing the price that was
    effective on the last sync date. All filters are applied in Python after a single
    company-scoped query. Returns [] when the collection is empty or filters match nothing.
    """
    collection = _collection_name()
    db = _firestore()
    docs = db.collection(collection).where(filter=FieldFilter("company", "==", company)).stream()
    nos_set = set(product_nos) if product_nos else None
    results = []
    for doc in docs:
        data = doc.to_dict()
        if not include_blocked and data.get("blocked") is True:
            continue
        if family_code and data.get("familyCode") != family_code:
            continue
        if product_no and data.get("productNo") != product_no:
            continue
        if nos_set is not None and data.get("productNo") not in nos_set:
            continue
        if price_list_code and data.get("priceListCode") != price_list_code:
            continue
        results.append(data)
    return results


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
    docs = db.collection(collection).where(filter=FieldFilter("company", "==", company)).stream()

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
    codes: list[str] = []
    for h in headers:
        starting = (h.get("startingDate") or "").strip()
        ending = (h.get("endingDate") or "").strip()
        if starting and starting > on_date:
            continue
        if ending and ending < on_date:
            continue
        code = h.get("code")
        if code:
            codes.append(code)
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
    docs = db.collection(collection).where(filter=FieldFilter("company", "==", company)).stream()
    results = []
    for doc in docs:
        data = doc.to_dict()
        if status and data.get("status") != status:
            continue
        if item_family_code and data.get("itemFamilyCode") != item_family_code:
            continue
        if price_type and data.get("priceType") != price_type:
            continue
        results.append(data)
    return results


def _state_collection_name() -> str:
    env = (config.GCP_ENV or "staging").lower().replace(" ", "_")
    return f"sync_state_{env}"


def get_sync_state(company: str, collection_type: str) -> str | None:
    """Return the UTC ISO timestamp of the last successful sync for (company, collection_type), or None."""
    db = _firestore()
    doc = db.collection(_state_collection_name()).document(f"{company}_{collection_type}").get()
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
    docs = query.stream()
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
    docs = db.collection(collection).where(filter=FieldFilter("company", "==", company)).stream()
    results = []
    for doc in docs:
        data = doc.to_dict()
        if price_list_code and data.get("priceListCode") != price_list_code:
            continue
        results.append(data)
    return results
