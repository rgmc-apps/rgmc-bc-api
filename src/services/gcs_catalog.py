"""Cloud Storage-backed persistence for the v3 item price catalog.

Blob layout: {GCP_ENV}/{COMPANY}/catalog.json
  e.g.  Production/LGAP/catalog.json
        Staging/RGMC/catalog.json

All public functions are non-fatal: any GCS error is logged and swallowed so
the BC fetch path continues normally when GCS is unavailable.
"""
import json
import logging
import threading
import time

from src.config import GCS_CATALOG_BUCKET, GCP_ENV

logger = logging.getLogger("gcs_catalog")

_client = None

# Process-level in-memory cache so repeated requests within the same instance
# don't pay the ~200ms GCS download cost on every call.
_mem_cache: dict[str, tuple[float, dict]] = {}  # company → (expires_at, data)
_mem_cache_lock = threading.Lock()
_MEM_CACHE_TTL = 300  # 5 minutes


def _gcs():
    global _client
    if _client is None:
        from google.cloud import storage  # lazy import — only paid when GCS is configured
        _client = storage.Client()
    return _client


def _blob_path(company_name: str) -> str:
    env = (GCP_ENV or "Staging").strip()
    return f"{env}/{company_name.upper()}/catalog.json"


def _mem_get(company_name: str) -> dict | None:
    with _mem_cache_lock:
        entry = _mem_cache.get(company_name)
    if not entry:
        return None
    expires_at, data = entry
    return data if time.time() < expires_at else None


def _mem_set(company_name: str, data: dict) -> None:
    with _mem_cache_lock:
        _mem_cache[company_name] = (time.time() + _MEM_CACHE_TTL, data)


def _mem_evict(company_name: str) -> None:
    with _mem_cache_lock:
        _mem_cache.pop(company_name, None)


def load_catalog(company_name: str) -> dict | None:
    """Load the persisted catalog from GCS.

    Returns {"records": list, "on_date": str, "saved_at": float} or None if the
    bucket is not configured, the object does not exist, or any error occurs.
    """
    if not GCS_CATALOG_BUCKET:
        logger.warning("GCS_CATALOG_BUCKET not configured — skipping catalog load")
        return None
    try:
        blob = _gcs().bucket(GCS_CATALOG_BUCKET).blob(_blob_path(company_name))
        if not blob.exists():
            return None
        data = json.loads(blob.download_as_text(encoding="utf-8"))
        count = len(data.get("records", []))
        logger.info(
            f"GCS catalog loaded: {count} records "
            f"(env={GCP_ENV}, company={company_name}, date={data.get('on_date')})"
        )
        return data
    except Exception as e:
        logger.warning(f"GCS catalog load failed (company={company_name}): {e}")
        return None


def load_catalog_cached(company_name: str) -> dict | None:
    """Like load_catalog but serves from a 5-minute process-level memory cache.

    First call per instance pays the GCS download cost (~200ms). All subsequent
    calls within the TTL are free. Cache is evicted when save_catalog writes new data.
    """
    cached = _mem_get(company_name)
    if cached is not None:
        return cached
    data = load_catalog(company_name)
    if data:
        _mem_set(company_name, data)
    return data


def save_catalog(company_name: str, on_date: str, records: list) -> None:
    """Persist the catalog to GCS after every successful full BC fetch.

    Called from a background thread in bc_functions.py — never blocks request handling.
    Evicts the in-memory cache so the next request picks up fresh data.
    """
    if not GCS_CATALOG_BUCKET:
        logger.warning("GCS_CATALOG_BUCKET not configured — skipping catalog save")
        return
    try:
        payload = json.dumps({
            "records": records,
            "on_date": on_date,
            "saved_at": time.time(),
        })
        _gcs().bucket(GCS_CATALOG_BUCKET).blob(_blob_path(company_name)).upload_from_string(
            payload, content_type="application/json"
        )
        _mem_evict(company_name)
        logger.info(
            f"GCS catalog saved: {len(records)} records "
            f"(env={GCP_ENV}, company={company_name}, date={on_date})"
        )
    except Exception as e:
        logger.warning(f"GCS catalog save failed (company={company_name}): {e}")
