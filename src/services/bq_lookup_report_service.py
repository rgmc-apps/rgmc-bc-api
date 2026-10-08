"""Firestore persistence for shareable BigQuery-Lookup alignment reports.

Collection: bq_lookup_reports_{env}
Document ID: Firestore auto-ID (doubles as the report's shareable link slug)

A report is a frozen snapshot of one BigQuery Lookup search (criteria + the PO rows
it found, each already carrying a plain-English recommended next step) generated
from the /reconcile page's "BigQuery Lookup" tab — so a person-in-charge who has no
reason to be logged into /reconcile can open one plain link and see exactly what's
misaligned and which button on the manual-trigger page fixes it, without re-running
anything themselves. Never mutated after creation: a stale report just means
generate a fresh one from a new search, same as any other read-only snapshot in this
app (so_buffer_history_{env} follows the same append-only, never-updated pattern).
"""
import logging
from typing import Any, Dict, List, Optional

from google.cloud import firestore

from src import config

logger = logging.getLogger("bq_lookup_report_service")

_db: Optional[firestore.Client] = None


def _firestore() -> firestore.Client:
    global _db
    if _db is None:
        _db = firestore.Client(project=config.GCP_PROJECT_ID)
    return _db


def _collection_name() -> str:
    env = (config.GCP_ENV or "staging").lower().replace(" ", "_")
    return f"bq_lookup_reports_{env}"


def create_report(criteria: Dict[str, Any], rows: List[Dict[str, Any]], generated_by: Dict[str, Any]) -> str:
    """Save one report snapshot. Returns the new doc ID — used as-is in the shareable
    link (/report/{id} on the manual-trigger page)."""
    doc_ref = _firestore().collection(_collection_name()).document()
    doc_ref.set({
        "criteria": criteria,
        "rows": rows,
        "generated_by": generated_by,
        "generated_at": firestore.SERVER_TIMESTAMP,
    })
    logger.info(f"bq_lookup_reports: created {doc_ref.id!r} ({len(rows)} row(s), by {generated_by!r})")
    return doc_ref.id


def get_report(report_id: str) -> Optional[Dict[str, Any]]:
    """Return one report snapshot (with generated_at as an ISO string), or None if it
    doesn't exist — e.g. a mistyped/deleted link."""
    snap = _firestore().collection(_collection_name()).document(report_id).get()
    if not snap.exists:
        return None
    data = snap.to_dict() or {}
    generated_at = data.get("generated_at")
    if generated_at is not None and hasattr(generated_at, "isoformat"):
        data["generated_at"] = generated_at.isoformat()
    return {"id": report_id, **data}
