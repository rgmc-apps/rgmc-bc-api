"""Cloud Storage-backed persistence for the v3 item price catalog.

Blob layout: {GCP_ENV}/{COMPANY}/catalog.json
  e.g.  Production/LGAP/catalog.json
        Staging/RGMC/catalog.json

All public functions are non-fatal: any GCS error is logged and swallowed so
the BC fetch path continues normally when GCS is unavailable.
"""
import json
import logging
import time

from src.config import GCS_CATALOG_BUCKET, GCP_ENV

logger = logging.getLogger("gcs_catalog")

_client = None


def _gcs():
    global _client
    if _client is None:
        from google.cloud import storage  # lazy import — only paid when GCS is configured
        _client = storage.Client()
    return _client


def _blob_path(company_name: str) -> str:
    env = (GCP_ENV or "Staging").strip()
    return f"{env}/{company_name.upper()}/catalog.json"


def load_catalog(company_name: str) -> dict | None:
    """Load the persisted catalog from GCS.

    Returns {"records": list, "on_date": str, "saved_at": float} or None if the
    bucket is not configured, the object does not exist, or any error occurs.
    """
    if not GCS_CATALOG_BUCKET:
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


def save_catalog(company_name: str, on_date: str, records: list) -> None:
    """Persist the catalog to GCS after every successful full BC fetch.

    Called from a background thread in bc_functions.py — never blocks request handling.
    """
    if not GCS_CATALOG_BUCKET:
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
        logger.info(
            f"GCS catalog saved: {len(records)} records "
            f"(env={GCP_ENV}, company={company_name}, date={on_date})"
        )
    except Exception as e:
        logger.warning(f"GCS catalog save failed (company={company_name}): {e}")
