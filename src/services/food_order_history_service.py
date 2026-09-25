"""Firestore persistence for the SBIC Food & Beverages consignment app's order
submission history — a dedicated collection, separate from the garments app's
session_history_{env} collection (own schema, own document shape), per this
app's "own identity, not a reskin/shared state" design principle.

Collection: food_order_history_{env}
Document ID: the record's own `id` (a UUID minted by the frontend at submit
time, shared between the success and — if it ever needs one — a later record;
in practice each submission attempt writes exactly one record here).

One record per submission ATTEMPT, success or failure — status distinguishes
them. Unlike sales-order data itself, nothing here is ever re-fetched from
Business Central; it's the food app's own append-only log of what its users
did.
"""
import logging
import time
from typing import Optional

from google.api_core import retry as api_retry
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from src import config

logger = logging.getLogger("food_order_history_service")

_db: firestore.Client | None = None
_NO_RETRY = api_retry.Retry(predicate=lambda e: False, deadline=None)
_FAST_TIMEOUT = 15.0


def _firestore() -> firestore.Client:
    global _db
    if _db is None:
        _db = firestore.Client(project=config.GCP_PROJECT_ID)
    return _db


def _collection_name() -> str:
    env = (config.GCP_ENV or "staging").lower().replace(" ", "_")
    return f"food_order_history_{env}"


def save_order_history(record: dict) -> str:
    """Insert one order-submission history record. Returns the document ID."""
    db = _firestore()
    collection = _collection_name()

    record_id = (record.get("id") or "").strip()
    if not record_id:
        raise ValueError("Order history record must include a non-empty 'id' field")

    ref = db.collection(collection).document(record_id)
    ref.set({
        **record,
        "savedAt": time.time(),
        "env": config.GCP_ENV,
    })
    logger.info(
        f"Food order history saved: {record_id} "
        f"(username={record.get('username')!r}, status={record.get('status')!r})"
    )
    return record_id


def get_order_history(
    username: str,
    company_code: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """Return order history for one username, most recent first.

    Sorting and pagination happen in Python (not Firestore) to avoid requiring
    a composite index for username + companyCode + orderBy — the same
    trade-off session_history_service.py makes, and for the same reason: this
    is a per-user history list, never a full-table scan.
    """
    db = _firestore()
    collection = _collection_name()

    query = db.collection(collection).where(filter=FieldFilter("username", "==", username))
    if company_code:
        query = query.where(filter=FieldFilter("companyCode", "==", company_code.strip().upper()))

    try:
        records = [doc.to_dict() for doc in query.stream(retry=_NO_RETRY, timeout=_FAST_TIMEOUT)]
    except Exception as e:
        logger.warning(f"food_order_history Firestore fetch failed: {e}")
        records = []

    records.sort(key=lambda r: r.get("createdAt") or "", reverse=True)
    total = len(records)
    page = records[offset:offset + limit] if limit else records[offset:]
    return page, total
