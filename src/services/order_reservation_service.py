"""Firestore-backed reservation preventing two concurrent order submissions
from using the same external document number before either has reached BC.

_check_duplicate_external_doc_no (in the sales order / sales return order
route modules) queries BC directly, but that read-then-decide check has a
race window: two submissions with the same reference number, enqueued within
milliseconds of each other (e.g. two reps, or a double-tap), can both query
BC before either order has been created there, see no conflict, and both
pass. Firestore document creation is atomic (fails if the document already
exists), so using it as a lock closes that gap.
"""
import datetime
import hashlib
import logging

from google.cloud import firestore

from src import config

logger = logging.getLogger("order_reservation_service")

_db: firestore.Client | None = None

# Same placeholder convention as _EXTERNAL_DOC_NO_PLACEHOLDER in the route
# modules — "no sales" sessions share this literal by the hundreds and must
# never be reserved against each other.
_PLACEHOLDER = "no sales"


def _firestore() -> firestore.Client:
    global _db
    if _db is None:
        _db = firestore.Client(project=config.GCP_PROJECT_ID)
    return _db


def _collection() -> str:
    env = (config.GCP_ENV or "staging").lower().replace(" ", "_")
    return f"order_reservations_{env}"


def _doc_id(company: str, external_doc_no: str) -> str:
    # Firestore document IDs can't contain "/", and reference numbers are
    # free text, so hash the natural key instead of sanitizing it.
    key = f"{(company or '').strip().upper()}|{external_doc_no.strip().lower()}"
    return hashlib.sha256(key.encode()).hexdigest()


def reserve(company: str, external_doc_no: str, task_id: str) -> bool:
    """Atomically claim (company, external_doc_no) for this submission.

    Returns True if the reservation was acquired (or the value is blank/the
    "no sales" placeholder, which is exempt — same scope as the BC-side
    duplicate check). Returns False if another submission already holds it.
    """
    value = (external_doc_no or "").strip()
    if not value or value.lower() == _PLACEHOLDER:
        return True
    doc_ref = _firestore().collection(_collection()).document(_doc_id(company, value))
    try:
        doc_ref.create({
            "company": company,
            "externalDocumentNo": value,
            "taskId": task_id,
            "reservedAt": datetime.datetime.utcnow(),
        })
        return True
    except Exception as e:
        if type(e).__name__ == "AlreadyExists":
            return False
        # An unexpected Firestore error shouldn't silently let a race through —
        # treat it as "could not confirm uniqueness" and block the submission.
        logger.error(f"Reservation check failed for {company}/{value}: {e}")
        return False


def release(company: str, external_doc_no: str) -> None:
    """Free a reservation so the reference number can be reused. Called when
    the reserving task ultimately fails, so a rep who corrects and resubmits
    isn't blocked by their own failed attempt. Never called on success — a
    successfully created order's reference number must never be reused, and
    leaving the reservation in place is a cheap extra guard alongside the
    BC-side duplicate check."""
    value = (external_doc_no or "").strip()
    if not value or value.lower() == _PLACEHOLDER:
        return
    try:
        _firestore().collection(_collection()).document(_doc_id(company, value)).delete()
    except Exception as e:
        logger.warning(f"Failed to release reservation for {company}/{value}: {e}")
