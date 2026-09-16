"""Cloud Storage-backed read cache for everything the worker pool publishes.

Blob layout under {GCP_ENV}/{COMPANY}/ (written by rgmc-worker-pool, gzip-encoded):
  families/_index.json         — {"families": {code: count}, "on_date", "overlay_on_date"}
  families/{FAMILY}.json       — one family in the exact GET /item-prices response shape
                                 ({"data": [...], "total", "onDate", ...}), prices overlaid
  search_index.json            — [[productNo, description_lower, familyBlobName], ...]
  catalog.json                 — full catalog (legacy fallback only)
  price_overrides.json         — compact per-price-list line index for historical-date overlays
  price_list_headers.json      — all price list headers
  customers.json / contacts.json / item_categories.json

Caching: each blob is held in process memory as its stored bytes (gzip, ~1 MB per family)
and keyed by its GCS generation. A cached blob is re-validated with a HEAD request at
most once per _RECHECK_S seconds and only re-downloaded when the generation changed.
Parsing is lazy — a family that is only ever served as-is to clients is never parsed.
A per-blob lock makes concurrent cold-start requests share one download.

All public functions are non-fatal: any GCS error is logged, and the last good copy
(if any) is served.
"""
import gzip
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from google.api_core.exceptions import NotFound

from src.config import GCS_CATALOG_BUCKET, GCP_ENV

logger = logging.getLogger("gcs_catalog")

_client = None

_RECHECK_S = 60         # seconds between generation checks for a cached blob
_HEAD_TIMEOUT = 10      # blob.reload()
_DOWNLOAD_TIMEOUT = 90  # blob.download_as_bytes(); blobs are gzip-encoded and small


@dataclass
class _Entry:
    generation: int | None
    raw: bytes | None       # bytes as stored in GCS (gzip when gzipped=True); None once patched
    gzipped: bool
    data: Any               # parsed JSON, populated lazily
    parsed: bool
    checked_at: float


_cache: dict[str, _Entry] = {}
_cache_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}


def _gcs():
    global _client
    if _client is None:
        from google.cloud import storage  # lazy import — only paid when GCS is configured
        _client = storage.Client()
    return _client


def _prefix(company_name: str) -> str:
    return f"{(GCP_ENV or 'Staging').strip()}/{company_name.upper()}"


def _lock_for(path: str) -> threading.Lock:
    with _cache_guard:
        lock = _locks.get(path)
        if lock is None:
            lock = _locks[path] = threading.Lock()
        return lock


def _fetch(path: str) -> _Entry | None:
    """Return the cache entry for path (a negative entry has generation None), refreshing
    it from GCS when the generation changed. Never raises."""
    if not GCS_CATALOG_BUCKET:
        return None
    entry = _cache.get(path)
    now = time.time()
    if entry and now - entry.checked_at < _RECHECK_S:
        return entry
    with _lock_for(path):
        entry = _cache.get(path)
        now = time.time()
        if entry and now - entry.checked_at < _RECHECK_S:
            return entry
        try:
            blob = _gcs().bucket(GCS_CATALOG_BUCKET).blob(path)
            try:
                blob.reload(timeout=_HEAD_TIMEOUT)
            except NotFound:
                entry = _Entry(None, None, False, None, True, now)
                _cache[path] = entry
                return entry
            if entry and entry.generation == blob.generation:
                entry.checked_at = now
                return entry
            started = time.time()
            gzipped = (blob.content_encoding or "").lower() == "gzip"
            raw = blob.download_as_bytes(timeout=_DOWNLOAD_TIMEOUT, raw_download=True)
            entry = _Entry(blob.generation, raw, gzipped, None, False, time.time())
            _cache[path] = entry
            logger.info(f"GCS blob loaded: {path} (gen={blob.generation}, {len(raw) / 1e6:.1f} MB, {time.time() - started:.2f}s)")
            return entry
        except Exception as e:
            logger.warning(f"GCS blob load failed ({path}): {e}")
            return entry


def _parse(entry: _Entry) -> Any:
    if entry.parsed:
        return entry.data
    with _lock_for("parse:" + str(id(entry))):
        if entry.parsed:
            return entry.data
        raw = entry.raw or b""
        try:
            entry.data = json.loads(gzip.decompress(raw) if entry.gzipped else raw)
        except Exception as e:
            logger.warning(f"GCS blob parse failed: {e}")
            entry.data = None
        entry.parsed = True
        return entry.data


def _load_json(path: str) -> Any:
    entry = _fetch(path)
    if entry is None or entry.generation is None:
        return None
    return _parse(entry)


def _upload_json(path: str, payload: dict) -> None:
    body = gzip.compress(json.dumps(payload, separators=(",", ":")).encode("utf-8"), compresslevel=6)
    blob = _gcs().bucket(GCS_CATALOG_BUCKET).blob(path)
    blob.content_encoding = "gzip"
    blob.cache_control = "no-cache"
    blob.upload_from_string(body, content_type="application/json")
    _cache.pop(path, None)


def evict_company(company_name: str) -> None:
    """Drop every cached blob for a company so the next read re-fetches from GCS."""
    prefix = _prefix(company_name) + "/"
    with _cache_guard:
        for path in [p for p in _cache if p.startswith(prefix)]:
            _cache.pop(path, None)


# ---------------------------------------------------------------------------
# Item catalog
# ---------------------------------------------------------------------------

def load_catalog(company_name: str) -> dict | None:
    """Full catalog (legacy fallback): {"records": list, "on_date", "overlay_on_date"|None}."""
    return _load_json(f"{_prefix(company_name)}/catalog.json")


load_catalog_cached = load_catalog


def load_family_index(company_name: str) -> dict | None:
    return _load_json(f"{_prefix(company_name)}/families/_index.json")


def family_response_gzip(company_name: str, family_code: str) -> bytes | None:
    """The stored gzip bytes of a family blob — already a complete API response body —
    or None when the blob is missing, not gzip-encoded, or was patched in memory."""
    if not family_code:
        return None
    entry = _fetch(f"{_prefix(company_name)}/families/{family_code}.json")
    if entry is None or entry.generation is None or not entry.gzipped:
        return None
    return entry.raw


def load_family_catalog(company_name: str, family_code: str) -> dict | None:
    """One family's parsed blob: {"data": list, "onDate", "overlay_on_date", ...} or None."""
    if not family_code:
        return None
    return _load_json(f"{_prefix(company_name)}/families/{family_code}.json")


def family_records(company_name: str, family_code: str) -> list | None:
    fam = load_family_catalog(company_name, family_code)
    if fam is None:
        return None
    return fam.get("data") or fam.get("records") or []


def load_search_index(company_name: str) -> dict | None:
    """{"items": [[productNo, description_lower, family], ...]} plus a lazily built
    "by_pno" map (productNo → family) attached on first use."""
    idx = _load_json(f"{_prefix(company_name)}/search_index.json")
    if idx is not None and "by_pno" not in idx:
        idx["by_pno"] = {pno: fam for pno, _desc, fam in idx.get("items") or []}
    return idx


def load_price_overrides(company_name: str) -> dict | None:
    """Compact index {"codes": {code: {assetNo: [incl, excl, startingDate]}}, "on_date"}."""
    return _load_json(f"{_prefix(company_name)}/price_overrides.json")


def patch_one_catalog_record(company_name: str, product_no: str, updates: dict) -> None:
    """Patch a product in every cached catalog/family blob of this company.

    Called after a single-item BC sync so bulk reads on this instance serve the corrected
    price without waiting for the next generation check. The patched blob's stored bytes
    are discarded so the as-is fast path cannot serve the stale price.
    """
    prefix = _prefix(company_name) + "/"
    with _cache_guard:
        entries = [e for p, e in _cache.items() if p.startswith(prefix) and e.generation is not None]
    for entry in entries:
        data = _parse(entry)
        if not isinstance(data, dict):
            continue
        for rec in data.get("data") or data.get("records") or []:
            if rec.get("productNo") == product_no:
                rec.update(updates)
                entry.raw = None


def save_catalog(company_name: str, on_date: str, records: list) -> None:
    """Persist a catalog fetched directly from BC by this API (no price overlay applied).

    The worker pool normally owns this blob; kept for the manual direct-sync endpoints.
    """
    if not GCS_CATALOG_BUCKET:
        logger.warning("GCS_CATALOG_BUCKET not configured — skipping catalog save")
        return
    try:
        _upload_json(
            f"{_prefix(company_name)}/catalog.json",
            {"records": records, "on_date": on_date, "saved_at": time.time()},
        )
        logger.info(f"GCS catalog saved: {len(records)} records (env={GCP_ENV}, company={company_name}, date={on_date})")
    except Exception as e:
        logger.warning(f"GCS catalog save failed (company={company_name}): {e}")


def warm_company(company_name: str) -> None:
    """Pre-download (not parse) the family blobs and small indexes so the first sync after
    a cold start is served straight from memory."""
    index = load_family_index(company_name)
    families = list((index or {}).get("families") or {})
    for family in families:
        _fetch(f"{_prefix(company_name)}/families/{family}.json")
    _fetch(f"{_prefix(company_name)}/search_index.json")
    load_pl_headers_cached(company_name)
    logger.info(f"GCS warmup: {len(families)} family blobs cached (company={company_name})")


# ---------------------------------------------------------------------------
# Price list headers
# ---------------------------------------------------------------------------

def load_pl_headers_cached(company_name: str) -> list | None:
    data = _load_json(f"{_prefix(company_name)}/price_list_headers.json")
    return None if data is None else data.get("headers", [])


def save_pl_headers(company_name: str, headers: list) -> None:
    if not GCS_CATALOG_BUCKET:
        return
    try:
        _upload_json(f"{_prefix(company_name)}/price_list_headers.json", {"headers": headers, "saved_at": time.time()})
        logger.info(f"GCS price list headers saved: {len(headers)} (company={company_name})")
    except Exception as e:
        logger.warning(f"GCS price list headers save failed (company={company_name}): {e}")


def evict_pl_headers(company_name: str) -> None:
    _cache.pop(f"{_prefix(company_name)}/price_list_headers.json", None)


# ---------------------------------------------------------------------------
# Supporting datasets
# ---------------------------------------------------------------------------

def load_customers_cached(company_name: str) -> list | None:
    data = _load_json(f"{_prefix(company_name)}/customers.json")
    return None if data is None else data.get("customers", [])


def load_contacts_cached(company_name: str) -> list | None:
    data = _load_json(f"{_prefix(company_name)}/contacts.json")
    return None if data is None else data.get("contacts", [])


def load_item_categories_cached(company_name: str) -> list | None:
    data = _load_json(f"{_prefix(company_name)}/item_categories.json")
    return None if data is None else data.get("item_categories", [])
