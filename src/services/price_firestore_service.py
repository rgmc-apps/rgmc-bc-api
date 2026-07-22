"""Firestore persistence layer for the v3 item price catalog.

Collection naming: item_prices_{env} where env = GCP_ENV.lower() with spaces
replaced by underscores (e.g. "item_prices_production", "item_prices_staging").

Document ID: {company}_{productNo} — supports multi-company in one collection.
"""
import logging
import time

from google.cloud import firestore

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
    include_blocked: bool = False,
) -> list:
    """Return item prices from Firestore for the given company and current GCP_ENV.

    All filters (family_code, product_no, product_nos, blocked) are applied in Python
    after a single company-scoped query — avoids composite index requirements.
    Returns [] when the collection is empty (not yet synced) OR when filters match nothing.
    """
    collection = _collection_name()
    db = _firestore()
    docs = db.collection(collection).where("company", "==", company).stream()
    nos_set = set(product_nos) if product_nos else None
    results = []
    for doc in docs:
        data = doc.to_dict()
        if not include_blocked and data.get("blocked"):
            continue
        if family_code and data.get("familyCode") != family_code:
            continue
        if product_no and data.get("productNo") != product_no:
            continue
        if nos_set is not None and data.get("productNo") not in nos_set:
            continue
        results.append(data)
    return results
