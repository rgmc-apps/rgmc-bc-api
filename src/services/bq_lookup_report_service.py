"""Firestore + GCS persistence for shareable BigQuery-Lookup alignment reports.

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

Also carries the original search's raw `headers`/`details` (not just the computed
`rows` summary) so the report page's own "Quick Align" buttons can call
/api/bigquery/quick-align directly from the shared link, without needing the
generating browser's live search state. A stale MSSQL/BC flag on a replayed header is
safe to act on regardless — /api/bigquery/quick-align always re-checks BC itself
before doing anything, and the MSSQL insert it may replay is itself a no-op for a row
already promoted (duplicate-key rows are dropped, never re-inserted).

`rows`/`headers`/`details` are stored together as one JSON blob in GCS rather than
inline on the Firestore document — a wide date-range search can return enough PO
headers/detail lines to blow past Firestore's 1 MiB per-document limit (seen live:
a single report hit 1,315,170 bytes), and GCS has no such ceiling. The Firestore doc
itself stays small: just criteria/generated_by/generated_at plus the GCS blob path,
mirroring deferred_result_service.py's Firestore+GCS split for the same reason.

get_report falls back to reading rows/headers/details straight off the Firestore doc
when no gcs_path is present, so reports saved before this split still read back fine.
"""
import json
import logging
from typing import Any, Dict, List, Optional

from google.cloud import firestore

from src import config

logger = logging.getLogger("bq_lookup_report_service")

_db: Optional[firestore.Client] = None
_gcs_client = None


def _firestore() -> firestore.Client:
    global _db
    if _db is None:
        _db = firestore.Client(project=config.GCP_PROJECT_ID)
    return _db


def _gcs():
    global _gcs_client
    if _gcs_client is None:
        from google.cloud import storage
        _gcs_client = storage.Client(project=config.GCP_PROJECT_ID)
    return _gcs_client


def _collection_name() -> str:
    env = (config.GCP_ENV or "staging").lower().replace(" ", "_")
    return f"bq_lookup_reports_{env}"


def _gcs_blob_path(report_id: str) -> str:
    env = (config.GCP_ENV or "staging").lower().replace(" ", "_")
    return f"bq_lookup_reports/{env}/{report_id}.json"


def create_report(
    criteria: Dict[str, Any],
    rows: List[Dict[str, Any]],
    generated_by: Dict[str, Any],
    headers: Optional[List[Dict[str, Any]]] = None,
    details: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Save one report snapshot. Returns the new doc ID — used as-is in the shareable
    link (/report/{id} on the manual-trigger page)."""
    if not config.GCS_CATALOG_BUCKET:
        raise RuntimeError("GCS_CATALOG_BUCKET is not configured — cannot save a report")

    doc_ref = _firestore().collection(_collection_name()).document()
    blob_path = _gcs_blob_path(doc_ref.id)
    payload = {"rows": rows, "headers": headers or [], "details": details or []}
    _gcs().bucket(config.GCS_CATALOG_BUCKET).blob(blob_path).upload_from_string(
        json.dumps(payload, default=str), content_type="application/json",
    )

    doc_ref.set({
        "criteria": criteria,
        "generated_by": generated_by,
        "generated_at": firestore.SERVER_TIMESTAMP,
        "gcs_path": blob_path,
        "row_count": len(rows),
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

    blob_path = data.pop("gcs_path", None)
    if blob_path:
        if not config.GCS_CATALOG_BUCKET:
            logger.error(f"bq_lookup_reports: GCS_CATALOG_BUCKET not configured — cannot read {report_id!r}")
            data.setdefault("rows", [])
            data.setdefault("headers", [])
            data.setdefault("details", [])
        else:
            try:
                blob = _gcs().bucket(config.GCS_CATALOG_BUCKET).blob(blob_path)
                payload = json.loads(blob.download_as_text(encoding="utf-8"))
                data.update(payload)
            except Exception as e:
                logger.error(f"bq_lookup_reports: GCS read failed id={report_id!r} path={blob_path!r}: {e}")
                data.setdefault("rows", [])
                data.setdefault("headers", [])
                data.setdefault("details", [])

    return {"id": report_id, **data}
