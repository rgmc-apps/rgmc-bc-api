"""Firestore persistence for consignment app session history records.

Collection: session_history_{env}
Document ID: {companyCode}_{sessionId}

Stores the full session payload (orders, user, customer) so history survives
localStorage clears and is accessible across devices per user.
"""
import logging
import time
from typing import Optional

from google.api_core import retry as api_retry
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from src import config

logger = logging.getLogger("session_history_service")

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
    return f"session_history_{env}"


def save_session(record: dict) -> str:
    """Upsert a session record into Firestore. Returns the document ID."""
    db = _firestore()
    collection = _collection_name()

    session_id = (record.get("id") or "").strip()
    company_code = (record.get("companyCode") or "unknown").strip().upper()
    if not session_id:
        raise ValueError("Session record must include a non-empty 'id' field")

    doc_id = f"{company_code}_{session_id}"
    ref = db.collection(collection).document(doc_id)
    ref.set({
        **record,
        "companyCode": company_code,
        "savedAt": time.time(),
        "env": config.GCP_ENV,
    })
    logger.info(
        f"Session history saved: {doc_id} "
        f"(user={record.get('userId')!r}, status={record.get('status')!r})"
    )
    return doc_id


def get_sessions(
    company_code: str,
    user_id: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """Return session history records for the given company, optionally filtered by user.

    Sorted by submittedAt descending (most recent first). Sorting and pagination are
    done in Python to avoid requiring composite Firestore indexes.
    """
    db = _firestore()
    collection = _collection_name()

    query = db.collection(collection).where(
        filter=FieldFilter("companyCode", "==", company_code.strip().upper())
    )
    if user_id:
        query = query.where(filter=FieldFilter("userId", "==", user_id))

    try:
        docs = list(query.stream(retry=_NO_RETRY, timeout=_FAST_TIMEOUT))
    except Exception as e:
        logger.warning(f"session_history Firestore fetch failed: {e}")
        return [], 0

    records = [doc.to_dict() for doc in docs]
    records.sort(key=lambda r: r.get("submittedAt") or r.get("createdAt") or "", reverse=True)

    total = len(records)
    page = records[offset:offset + limit] if limit else records[offset:]
    return page, total
