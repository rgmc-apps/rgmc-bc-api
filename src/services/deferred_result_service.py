"""Firestore + GCS storage for deferred (slow-request) results.

When a route handler is about to time out, it writes a pending record here and
returns 202. A background thread completes the work and calls save_result() to
mark it ready. Clients poll GET /deferred/{key} to check status and retrieve data.

Firestore collection  : deferred_results_{env}   (e.g. deferred_results_production)
  Document fields     : key, status, endpoint, params, created_at, completed_at,
                        gcs_path, error, expires_at
  status values       : "pending" | "ready" | "error"

GCS blob layout       : deferred/{key}.json  (in GCS_CATALOG_BUCKET)
  Content             : the serialised response body that would have been returned
"""
import json
import logging
import time
import uuid

from src import config

logger = logging.getLogger("deferred_result_service")

_db = None
_gcs_client = None
_EXPIRES_SECONDS = 24 * 3600  # blobs + Firestore docs live for 24 h


# ── Lazy singletons ─────────────────────────────────────────────────────────

def _firestore():
    global _db
    if _db is None:
        from google.cloud import firestore
        _db = firestore.Client(project=config.GCP_PROJECT_ID)
    return _db


def _gcs():
    global _gcs_client
    if _gcs_client is None:
        from google.cloud import storage
        _gcs_client = storage.Client(project=config.GCP_PROJECT_ID)
    return _gcs_client


def _collection():
    env = (config.GCP_ENV or "staging").lower().replace(" ", "_")
    return _firestore().collection(f"deferred_results_{env}")


def _gcs_blob_path(key: str) -> str:
    env = (config.GCP_ENV or "Staging").strip()
    return f"deferred/{env}/{key}.json"


# ── Public API ───────────────────────────────────────────────────────────────

def create_pending(endpoint: str, params: dict) -> str:
    """Create a pending record in Firestore and return a unique key."""
    key = str(uuid.uuid4())
    now = time.time()
    _collection().document(key).set({
        "status": "pending",
        "endpoint": endpoint,
        "params": params,
        "created_at": now,
        "completed_at": None,
        "gcs_path": None,
        "error": None,
        "expires_at": now + _EXPIRES_SECONDS,
    })
    logger.info(f"Deferred record created: key={key!r} endpoint={endpoint!r}")
    return key


def save_result(key: str, data) -> None:
    """Serialise data to GCS then mark the Firestore record as ready."""
    if not config.GCS_CATALOG_BUCKET:
        logger.error(f"GCS_CATALOG_BUCKET not configured — cannot save deferred result key={key!r}")
        fail_result(key, "GCS_CATALOG_BUCKET not configured")
        return
    blob_path = _gcs_blob_path(key)
    try:
        blob = _gcs().bucket(config.GCS_CATALOG_BUCKET).blob(blob_path)
        blob.upload_from_string(
            json.dumps(data, default=str),
            content_type="application/json",
        )
    except Exception as e:
        logger.error(f"Deferred GCS save failed key={key!r}: {e}")
        fail_result(key, f"GCS save failed: {e}")
        return

    _collection().document(key).update({
        "status": "ready",
        "gcs_path": blob_path,
        "completed_at": time.time(),
    })
    logger.info(f"Deferred result saved: key={key!r} path={blob_path!r}")


def fail_result(key: str, error: str) -> None:
    """Mark a deferred record as failed."""
    try:
        _collection().document(key).update({
            "status": "error",
            "error": error,
            "completed_at": time.time(),
        })
    except Exception as e:
        logger.error(f"Deferred fail_result update failed key={key!r}: {e}")


def get_meta(key: str) -> dict | None:
    """Return the Firestore metadata doc, or None if the key doesn't exist."""
    doc = _collection().document(key).get(retry=None)
    return doc.to_dict() if doc.exists else None


def read_data(meta: dict):
    """Download and deserialise the result data from GCS using a metadata dict."""
    blob_path = meta.get("gcs_path")
    if not blob_path or not config.GCS_CATALOG_BUCKET:
        return None
    try:
        blob = _gcs().bucket(config.GCS_CATALOG_BUCKET).blob(blob_path)
        return json.loads(blob.download_as_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"Deferred GCS read failed path={blob_path!r}: {e}")
        return None
