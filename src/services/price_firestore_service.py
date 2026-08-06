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
import traceback

from google.cloud import firestore

from src import config

logger = logging.getLogger("price_firestore_service")

_db: firestore.Client | None = None
_BATCH_SIZE = 500


def _firestore() -> firestore.Client:
    global _db
    if _db is None:
        logger.info(f"Initializing Firestore client for project={config.GCP_PROJECT_ID!r}")
        try:
            _db = firestore.Client(project=config.GCP_PROJECT_ID)
            logger.info("Firestore client initialized OK")
        except Exception:
            logger.error(f"Firestore client init failed:\n{traceback.format_exc()}")
            raise
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

    All filters (family_code, product_no, product_nos, price_list_code, blocked) are
    applied in Python after a single company-scoped query — avoids composite index
    requirements. Returns [] when the collection is empty or filters match nothing.
    """
    collection = _collection_name()
    db = _firestore()
    docs = db.collection(collection).where("company", "==", company).stream()
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
    docs = db.collection(collection).where("company", "==", company).stream()
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
    try:
        doc = db.collection(_state_collection_name()).document(f"{company}_{collection_type}").get()
    except Exception:
        logger.error(f"get_sync_state({company!r}, {collection_type!r}) failed:\n{traceback.format_exc()}")
        raise
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
    docs = db.collection(collection).where("company", "==", company).stream()
    results = []
    for doc in docs:
        data = doc.to_dict()
        if item_no and data.get("itemNo") != item_no:
            continue
        if entry_type and data.get("entryType") != entry_type:
            continue
        if location_code and data.get("locationCode") != location_code:
            continue
        lmdt = data.get("lastModifiedDateTime") or ""
        if modified_from and lmdt < modified_from:
            continue
        if modified_to and lmdt > modified_to:
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
    docs = db.collection(collection).where("company", "==", company).stream()
    results = []
    for doc in docs:
        data = doc.to_dict()
        if price_list_code and data.get("priceListCode") != price_list_code:
            continue
        results.append(data)
    return results
