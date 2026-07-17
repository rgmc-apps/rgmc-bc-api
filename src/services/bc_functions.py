"""All Business Central API related functions."""
import datetime
import logging
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
import requests
from requests.adapters import HTTPAdapter
from src.config import BC_CLIENT_ID, BC_TENANT_ID, BC_CLIENT_SECRET, BC_SCOPE, BC_AUTH_URL, BC_ENVIRONMENT

logger = logging.getLogger("bc_functions")

_BC_BASE = "https://api.businesscentral.dynamics.com/v2.0"

_token_lock = threading.Lock()
_token_cache: dict = {"token": None, "expires_at": 0.0}
_company_id_cache: dict = {}
_companies_lock = threading.Lock()
_companies_cache: dict = {"value": None, "expires_at": 0.0}
_COMPANIES_TTL = 600  # 10 minutes — companies list rarely changes
_item_price_cache: dict = {}
_list_cache: dict = {}
_LIST_CACHE_TTL = 300  # 5 minutes for general entity lists; dimension values use 3600s
_list_refresh_lock = threading.Lock()
_list_refreshing: set = set()
_LIST_WARMUP_WAIT_S = 40  # max seconds to block waiting for a background list refresh
# v1/v2 item price caches — entries are error-fallback only; cap size and TTL to prevent OOM.
_ITEM_PRICE_CACHE_TTL = 300
_ITEM_PRICE_CACHE_MAX_SIZE = 200

# Shared HTTP session with connection pooling — reuses TCP+TLS connections across all BC calls.
# pool_maxsize=20: up to 20 simultaneous keep-alive connections to api.businesscentral.dynamics.com.
_session = requests.Session()
_http_adapter = HTTPAdapter(pool_connections=4, pool_maxsize=20)
_session.mount("https://", _http_adapter)
_session.mount("http://", _http_adapter)

# Active BC request counter — incremented inside _fetch_all_pages so we can report
# live concurrency to the frontend status endpoint.
_active_bc_requests: int = 0
_active_bc_lock = threading.Lock()
# Hard limit one below BC's 5-concurrent-request cap.  Every outgoing BC call —
# reads, writes, and background warmup threads — must acquire a slot before hitting BC.
# In-process queuing here is cheaper than a BC 429 retry round-trip.
_bc_semaphore = threading.Semaphore(4)


class ServiceWarmingError(Exception):
    """Last-resort 503 raised only when the catalog fetch times out or fails completely.

    Normal cold-start requests block in _block_until_v3_catalog_ready() for up to
    _V3_WARMUP_WAIT_S seconds while the background fetch runs.  With 3 parallel
    ranges and Prefer: odata.maxpagesize=500 the catalog loads in 6-20 s, so this
    exception is rarely reached in practice.
    """
    pass


def get_access_token() -> str:
    with _token_lock:
        if time.time() < _token_cache["expires_at"] - 60:
            return _token_cache["token"]
        payload = {
            "grant_type": "client_credentials",
            "client_id": BC_CLIENT_ID,
            "client_secret": BC_CLIENT_SECRET,
            "scope": BC_SCOPE
        }
        response = _session.post(
            BC_AUTH_URL,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()
        data = response.json()
        _token_cache["token"] = data["access_token"]
        _token_cache["expires_at"] = time.time() + data.get("expires_in", 3600)
        return _token_cache["token"]


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {get_access_token()}", "Accept": "application/json"}


def _bc_request(method: str, url: str, max_retries: int = 3, **kwargs) -> requests.Response:
    """Gate a single BC HTTP call through _bc_semaphore with active-request tracking and 429/5xx retry.

    Semaphore is acquired only while the HTTP request is in flight — released before each
    sleep so other BC requests are not starved while waiting on a rate-limit hold-off.
    429 honours the Retry-After header; 502/503 use exponential backoff capped at 16 s.
    After max_retries the last response is returned — callers should call raise_for_status()
    or check status_code as needed.
    """
    global _active_bc_requests
    response = None
    for attempt in range(max_retries + 1):
        with _bc_semaphore:
            with _active_bc_lock:
                _active_bc_requests += 1
            try:
                response = getattr(_session, method)(url, **kwargs)
            finally:
                with _active_bc_lock:
                    _active_bc_requests -= 1
        # Semaphore released — evaluate status before sleeping.
        if response.status_code not in (429, 502, 503):
            return response
        if attempt == max_retries:
            break
        if response.status_code == 429:
            wait = int(response.headers.get("Retry-After", min(2 ** attempt, 30)))
            logger.warning(f"BC 429 on {method.upper()} (attempt {attempt + 1}/{max_retries}). Waiting {wait}s.")
        else:
            wait = min(2 ** attempt, 16)
            logger.warning(f"BC {response.status_code} on {method.upper()} (attempt {attempt + 1}/{max_retries}). Waiting {wait}s.")
        time.sleep(wait)  # Semaphore NOT held during sleep
    return response


def call_business_central_api(endpoint: str):
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/api/v2.0/{endpoint}"
    response = _bc_request("get", url, headers=_auth_headers())
    return response.status_code, response.json()


def _fetch_companies_cached() -> list:
    """Return the raw companies list with a 10-minute TTL cache.

    Uses double-checked locking so only one BC call is made on cache miss,
    and also populates _company_id_cache for free.
    """
    now = time.time()
    if _companies_cache["value"] is not None and now < _companies_cache["expires_at"]:
        return _companies_cache["value"]
    with _companies_lock:
        if _companies_cache["value"] is not None and time.time() < _companies_cache["expires_at"]:
            return _companies_cache["value"]
        status, data = call_business_central_api("companies")
        if status != 200:
            raise RuntimeError(f"BC companies call failed ({status}): {data}")
        companies = data.get("value", [])
        for c in companies:
            _company_id_cache[c.get("name", "").upper()] = c["id"]
        _companies_cache["value"] = companies
        _companies_cache["expires_at"] = time.time() + _COMPANIES_TTL
        return companies


def get_company_id(company_name: str) -> str:
    """Return the BC company GUID for the given company name (uses companies cache)."""
    name = company_name.upper()
    if name in _company_id_cache:
        return _company_id_cache[name]
    _fetch_companies_cached()
    if name in _company_id_cache:
        return _company_id_cache[name]
    raise ValueError(f"Company '{name}' not found in Business Central")


def get_all_companies_cached() -> tuple:
    """Return (200, {value: [...]}) for the companies list, served from cache after first load."""
    try:
        companies = _fetch_companies_cached()
        return 200, {"value": companies}
    except RuntimeError as e:
        return 502, {"error": str(e)}


def warmup_company_id():
    """Pre-populate the companies cache. Called at startup in a background thread."""
    try:
        companies = _fetch_companies_cached()
        logger.info(f"Companies cache warmed up: {len(companies)} companies")
    except Exception as e:
        logger.warning(f"Companies warmup failed: {e}")


def _fetch_all_pages(url: str, max_retries: int = 6, extra_headers: dict | None = None) -> list:
    """Follow @odata.nextLink pages and return the combined value list.

    Semaphore is acquired per HTTP request only — released immediately after each response
    is received so sleep periods (429 hold-off / 5xx backoff) do not starve other BC callers.
    429 honours the Retry-After header; 502/503 use exponential backoff capped at 16 s.
    extra_headers are merged onto every request in the chain (e.g. Prefer: odata.maxpagesize).
    """
    global _active_bc_requests
    all_records = []
    next_url = url
    while next_url:
        response = None
        for attempt in range(max_retries + 1):
            with _bc_semaphore:
                with _active_bc_lock:
                    _active_bc_requests += 1
                try:
                    headers = {**_auth_headers(), **(extra_headers or {})}
                    response = _session.get(next_url, headers=headers, timeout=120)
                finally:
                    with _active_bc_lock:
                        _active_bc_requests -= 1
            # Semaphore released — evaluate status before sleeping.
            if response.status_code not in (429, 502, 503):
                break
            if attempt == max_retries:
                logger.warning(f"BC {response.status_code} after {max_retries} retries on {next_url}")
                break
            if response.status_code == 429:
                wait = int(response.headers.get("Retry-After", min(2 ** attempt, 60)))
                logger.warning(f"BC rate-limited (429). Waiting {wait}s (attempt {attempt + 1}/{max_retries}).")
            else:
                wait = min(2 ** attempt, 16)
                logger.warning(f"BC transient error ({response.status_code}). Waiting {wait}s (attempt {attempt + 1}/{max_retries}).")
            time.sleep(wait)  # Semaphore NOT held during sleep
        response.raise_for_status()
        data = response.json()
        all_records.extend(data.get("value", []))
        next_url = data.get("@odata.nextLink")
    return all_records


def _list_fetch_and_cache(cache_key: tuple, url: str, ttl: float):
    """Fetch a paginated BC list and update _list_cache. Runs in a background thread."""
    try:
        records = _fetch_all_pages(url)
        _list_cache[cache_key] = {"data": {"value": records}, "expires_at": time.time() + ttl}
        logger.info(f"List cache refreshed: {cache_key}")
    except Exception as e:
        logger.warning(f"Background list refresh failed {cache_key}: {e}")
    finally:
        with _list_refresh_lock:
            _list_refreshing.discard(cache_key)


def _trigger_list_refresh(cache_key: tuple, url: str, ttl: float):
    """Start a background list-cache refresh if one is not already running."""
    with _list_refresh_lock:
        if cache_key in _list_refreshing:
            return
        _list_refreshing.add(cache_key)
    threading.Thread(target=_list_fetch_and_cache, args=(cache_key, url, ttl), daemon=True).start()


def _block_until_list_ready(cache_key: tuple, url: str, ttl: float, timeout: float = _LIST_WARMUP_WAIT_S) -> dict | None:
    """Ensure a background list refresh is running and poll until it finishes.

    Returns the cache entry dict on success, None if timed out or the refresh failed.
    Keeps cold-start requests alive rather than doing a duplicate synchronous BC call
    when the startup warmup thread is already fetching the same data.
    """
    _trigger_list_refresh(cache_key, url, ttl)
    deadline = time.time() + timeout
    while time.time() < deadline:
        entry = _list_cache.get(cache_key)
        if entry:
            return entry
        with _list_refresh_lock:
            still_running = cache_key in _list_refreshing
        if not still_running:
            break
        time.sleep(0.3)
    return _list_cache.get(cache_key)


def _dim_fetch_and_cache(cache_key: tuple, dimension_code: str, company_name: str):
    """Re-fetch dimension values from BC and update _list_cache. Runs in a background thread."""
    try:
        company_id = get_company_id(company_name)
        base = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/api/v2.0/companies({company_id})"
        dims = _fetch_all_pages(f"{base}/dimensions?$filter=code eq '{dimension_code.upper()}'")
        if not dims:
            raise ValueError(f"Dimension '{dimension_code}' not found")
        dimension_id = dims[0]["id"]
        records = _fetch_all_pages(f"{base}/dimensionValues?$filter=dimensionId eq {dimension_id}")
        _list_cache[cache_key] = {"data": {"value": records}, "expires_at": time.time() + 3600}
        logger.info(f"Dimension cache refreshed: {dimension_code} (company={company_name})")
    except Exception as e:
        logger.warning(f"Background dimension refresh failed {dimension_code}: {e}")
    finally:
        with _list_refresh_lock:
            _list_refreshing.discard(cache_key)


def call_bc_table(table_endpoint: str, company_name: str, odata_filter: str = None, expand: str = None, select: str = None):
    """Call a company-scoped BC table endpoint and return (status, value_list).

    Unfiltered requests (no filter/expand/select) are served from a 5-minute TTL cache.
    """
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/api/v2.0/companies({company_id})/{table_endpoint}"
    params = []
    if odata_filter:
        params.append(f"$filter={odata_filter}")
    if expand:
        params.append(f"$expand={expand}")
    if select:
        params.append(f"$select={select}")
    if params:
        url += "?" + "&".join(params)

    cache_key = ("bc_v2", table_endpoint, company_name.upper()) if not odata_filter and not expand and not select else None
    if cache_key:
        entry = _list_cache.get(cache_key)
        if entry:
            if time.time() < entry["expires_at"]:
                return 200, entry["data"]
            # Stale: return immediately and refresh in background
            _trigger_list_refresh(cache_key, url, _LIST_CACHE_TTL)
            return 200, entry["data"]
        # Cold cache: wait for the background warmup rather than blocking the request thread
        entry = _block_until_list_ready(cache_key, url, _LIST_CACHE_TTL)
        if entry:
            return 200, entry["data"]
        # Warmup timed out — fall through to synchronous fetch for this fast table

    try:
        records = _fetch_all_pages(url)
        data = {"value": records}
        if cache_key:
            _list_cache[cache_key] = {"data": data, "expires_at": time.time() + _LIST_CACHE_TTL}
        return 200, data
    except requests.HTTPError as e:
        if cache_key:
            entry = _list_cache.get(cache_key)
            if entry:
                return 200, entry["data"]
        return e.response.status_code, e.response.json()


def bc_get_record(table_endpoint: str, record_id: str, company_name: str):
    """GET a single record by GUID from a company-scoped BC table."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/api/v2.0/companies({company_id})/{table_endpoint}({record_id})"
    response = _bc_request("get", url, headers=_auth_headers())
    return response.status_code, response.json()


def bc_create_record(table_endpoint: str, payload: dict, company_name: str):
    """POST a new record to a company-scoped BC table."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/api/v2.0/companies({company_id})/{table_endpoint}"
    headers = {**_auth_headers(), "Content-Type": "application/json"}
    response = _bc_request("post", url, json=payload, headers=headers)
    return response.status_code, response.json()


def bc_update_record(table_endpoint: str, record_id: str, payload: dict, company_name: str):
    """PATCH an existing record in a company-scoped BC table."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/api/v2.0/companies({company_id})/{table_endpoint}({record_id})"
    headers = {**_auth_headers(), "Content-Type": "application/json", "If-Match": "*"}
    response = _bc_request("patch", url, json=payload, headers=headers)
    return response.status_code, response.json() if response.content else {}


def bc_delete_record(table_endpoint: str, record_id: str, company_name: str):
    """DELETE a record from a company-scoped BC table."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/api/v2.0/companies({company_id})/{table_endpoint}({record_id})"
    response = _bc_request("delete", url, headers=_auth_headers())
    return response.status_code


_RGMC_CUSTOM_API = "api/rgmc/rgmccustom/v1.0"
_RGMC_CUSTOM_API_V2 = "api/rgmc/rgmccustom/v2.0"
_RGMC_CUSTOM_API_V3 = "api/rgmc/rgmccustom/v3.0"

_item_price_v2_cache: dict = {}
_item_price_v3_cache: dict = {}


def call_rgmc_table(table_endpoint: str, company_name: str, odata_filter: str = None, expand: str = None, select: str = None):
    """Call a company-scoped RGMC custom API table and return (status, value_list).

    Unfiltered requests are served from a 5-minute TTL cache.
    """
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API}/companies({company_id})/{table_endpoint}"
    params = []
    if odata_filter:
        params.append(f"$filter={odata_filter}")
    if expand:
        params.append(f"$expand={expand}")
    if select:
        params.append(f"$select={select}")
    if params:
        url += "?" + "&".join(params)

    cache_key = ("rgmc_v1", table_endpoint, company_name.upper()) if not odata_filter and not expand and not select else None
    if cache_key:
        entry = _list_cache.get(cache_key)
        if entry:
            if time.time() < entry["expires_at"]:
                return 200, entry["data"]
            # Stale: return immediately and refresh in background
            _trigger_list_refresh(cache_key, url, _LIST_CACHE_TTL)
            return 200, entry["data"]
        # Cold cache: wait for the background warmup rather than blocking the request thread
        entry = _block_until_list_ready(cache_key, url, _LIST_CACHE_TTL)
        if entry:
            return 200, entry["data"]
        # Warmup timed out — fall through to synchronous fetch for this fast table

    try:
        records = _fetch_all_pages(url)
        data = {"value": records}
        if cache_key:
            _list_cache[cache_key] = {"data": data, "expires_at": time.time() + _LIST_CACHE_TTL}
        return 200, data
    except requests.HTTPError as e:
        if cache_key:
            entry = _list_cache.get(cache_key)
            if entry:
                return 200, entry["data"]
        return e.response.status_code, e.response.json()


def rgmc_get_record(table_endpoint: str, record_id: str, company_name: str):
    """GET a single record by GUID from a RGMC custom API table."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API}/companies({company_id})/{table_endpoint}({record_id})"
    response = _bc_request("get", url, headers=_auth_headers())
    return response.status_code, response.json()


def rgmc_create_record(table_endpoint: str, payload: dict, company_name: str):
    """POST a new record to a RGMC custom API table."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API}/companies({company_id})/{table_endpoint}"
    headers = {**_auth_headers(), "Content-Type": "application/json"}
    response = _bc_request("post", url, json=payload, headers=headers)
    return response.status_code, _safe_json(response)


def rgmc_update_record(table_endpoint: str, record_id: str, payload: dict, company_name: str):
    """PATCH an existing record in a RGMC custom API table."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API}/companies({company_id})/{table_endpoint}({record_id})"
    headers = {**_auth_headers(), "Content-Type": "application/json", "If-Match": "*"}
    response = _bc_request("patch", url, json=payload, headers=headers)
    return response.status_code, response.json() if response.content else {}


def rgmc_delete_record(table_endpoint: str, record_id: str, company_name: str):
    """DELETE a record from a RGMC custom API table."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API}/companies({company_id})/{table_endpoint}({record_id})"
    response = _bc_request("delete", url, headers=_auth_headers())
    return response.status_code


def _safe_json(response) -> Any:
    """Parse response body as JSON; fall back to raw text on decode failure."""
    if not response.content:
        return {}
    try:
        return response.json()
    except Exception:
        return response.text


def rgmc_get_contact_picture(contact_id: str, company_name: str):
    """GET contactPictures({contact_id}) — returns {id, contactNo, picture} where picture is base64."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API}/companies({company_id})/contactPictures({contact_id})"
    response = _bc_request("get", url, headers=_auth_headers())
    return response.status_code, _safe_json(response)


def rgmc_update_contact_picture(contact_id: str, picture_base64: str, company_name: str):
    """PATCH contactPictures({contact_id}) with a base64-encoded image string. Insert/Delete not allowed by AL."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API}/companies({company_id})/contactPictures({contact_id})"
    headers = {**_auth_headers(), "Content-Type": "application/json", "If-Match": "*"}
    response = _bc_request("patch", url, json={"picture": picture_base64}, headers=headers)
    return response.status_code, _safe_json(response)


def rgmc_list_contact_brand_tags(contact_id: str, company_name: str):
    """GET contacts({contact_id})/contactBrandTags — all brand tags for a contact (Pag50209)."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API}/companies({company_id})/contacts({contact_id})/contactBrandTags"
    response = _bc_request("get", url, headers=_auth_headers())
    return response.status_code, _safe_json(response)


def rgmc_add_contact_brand_tag(contact_id: str, brand_code: str, company_name: str):
    """POST contacts({contact_id})/contactBrandTags — add a brand tag to a contact (Pag50209)."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API}/companies({company_id})/contacts({contact_id})/contactBrandTags"
    headers = {**_auth_headers(), "Content-Type": "application/json"}
    response = _bc_request("post", url, json={"brandCode": brand_code}, headers=headers)
    return response.status_code, _safe_json(response)


def rgmc_delete_contact_brand_tag(contact_id: str, tag_id: str, company_name: str):
    """DELETE contacts({contact_id})/contactBrandTags({tag_id}) — remove a brand tag (Pag50209)."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API}/companies({company_id})/contacts({contact_id})/contactBrandTags({tag_id})"
    response = _bc_request("delete", url, headers=_auth_headers())
    return response.status_code


def rgmc_list_item_prices(
    company_name: str,
    product_no: str = None,
    product_nos: list = None,
    on_date: str = None,
    odata_filter: str = None,
    top: int = None,
):
    """GET itemPrices filtered to the price effectivity window (Pag50210).

    When on_date is provided the filter enforces:
        startingDate <= on_date <= endingDate
    A blank endingDate is stored by BC as 0001-01-01 (meaning "open-ended"),
    so records with endingDate eq 0001-01-01 are always included.
    Results are ordered startingDate desc so the most-recent effective price
    comes first when the caller uses $top=1.

    product_nos accepts a list of product numbers and builds an OData OR filter,
    scoping the fetch to only those items instead of the full price list.
    Pagination is followed automatically unless top=1 (single active-price lookup).
    """
    cache_key = (company_name, product_no, tuple(product_nos) if product_nos else None, on_date, odata_filter, top)
    try:
        company_id = get_company_id(company_name)
        url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API}/companies({company_id})/itemPrices"
        params = []
        filters = []
        if product_no:
            filters.append(f"productNo eq '{product_no}'")
        elif product_nos:
            nos_filter = " or ".join(f"productNo eq '{n}'" for n in product_nos)
            filters.append(f"({nos_filter})")
        if on_date:
            filters.append(f"startingDate le {on_date}")
            filters.append(f"(endingDate ge {on_date} or endingDate eq 0001-01-01)")
        if odata_filter:
            filters.append(odata_filter)
        if filters:
            params.append(f"$filter={' and '.join(filters)}")
        params.append("$orderby=startingDate desc")
        if top:
            params.append(f"$top={top}")
        url += "?" + "&".join(params)
        if top == 1:
            response = _bc_request("get", url, headers=_auth_headers())
            data = _safe_json(response)
            if response.ok:
                _item_price_cache[cache_key] = {"data": data, "expires_at": time.time() + _ITEM_PRICE_CACHE_TTL}
            return response.status_code, data
        else:
            records = _fetch_all_pages(url)
            data = {"value": records}
            _item_price_cache[cache_key] = {"data": data, "expires_at": time.time() + _ITEM_PRICE_CACHE_TTL}
            return 200, data
    except Exception:
        entry = _item_price_cache.get(cache_key)
        if entry is not None:
            return 200, entry["data"]
        raise


def update_cached_item_price(
    product_no: str,
    updated_fields: dict,
    company_name: str,
    on_date: str = None,
) -> int:
    """Merge updated_fields into every cached price record that matches product_no."""
    target_company = company_name.upper()
    count = 0
    for cache_key, entry in list(_item_price_cache.items()):
        key_company, key_product_no, key_on_date = cache_key[0], cache_key[1], cache_key[3]
        if key_company.upper() != target_company:
            continue
        if key_product_no != product_no:
            continue
        if on_date and key_on_date != on_date:
            continue
        cached_data = entry["data"] if isinstance(entry, dict) and "data" in entry else entry
        for record in cached_data.get("value", []):
            record.update(updated_fields)
        count += 1
    return count


def get_dimension_values_by_code(dimension_code: str, company_name: str):
    """Return all dimension values for the given dimension code (e.g. 'BRAND').

    Results are cached for 1 hour — dimension codes and values rarely change.
    """
    cache_key = ("dim_values", dimension_code.upper(), company_name.upper())
    entry = _list_cache.get(cache_key)
    if entry:
        if time.time() < entry["expires_at"]:
            return 200, entry["data"]
        # Stale: return immediately and refresh in background
        with _list_refresh_lock:
            if cache_key not in _list_refreshing:
                _list_refreshing.add(cache_key)
                threading.Thread(target=_dim_fetch_and_cache, args=(cache_key, dimension_code, company_name), daemon=True).start()
        return 200, entry["data"]

    company_id = get_company_id(company_name)
    base = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/api/v2.0/companies({company_id})"

    dims = _fetch_all_pages(f"{base}/dimensions?$filter=code eq '{dimension_code.upper()}'")
    if not dims:
        raise ValueError(f"Dimension '{dimension_code}' not found")
    dimension_id = dims[0]["id"]

    records = _fetch_all_pages(f"{base}/dimensionValues?$filter=dimensionId eq {dimension_id}")
    data = {"value": records}
    _list_cache[cache_key] = {"data": data, "expires_at": time.time() + 3600}
    return 200, data


# ---------------------------------------------------------------------------
# RGMC Custom API v2.0 — Item Prices (Pag50210)
# ---------------------------------------------------------------------------

def rgmc_v2_list_item_prices(
    company_name: str,
    product_no: str = None,
    product_nos: list = None,
    on_date: str = None,
    odata_filter: str = None,
    top: int = None,
    family_code: str = None,
):
    """GET itemPrices from the v2.0 RGMC custom API with optional filtering.

    When family_code is provided (and no explicit product_no/product_nos), the function
    first resolves item numbers for that family code server-side, then filters prices by
    those numbers. This avoids the client needing to send a large product_nos list which
    would cause a 413 from GCP's load balancer.
    """
    if family_code and not product_no and not product_nos:
        try:
            _, items_data = call_rgmc_v2_table(
                "items",
                company_name,
                odata_filter=f"familyCode eq '{family_code}'",
                select="number",
            )
            resolved = [i.get("number") for i in items_data.get("value", []) if i.get("number")]
            if resolved:
                product_nos = resolved
        except Exception:
            pass  # Fall through to unfiltered price fetch if item lookup fails

    cache_key = (company_name, product_no, tuple(product_nos) if product_nos else None, on_date, odata_filter, top)
    try:
        company_id = get_company_id(company_name)
        url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API_V2}/companies({company_id})/itemPrices"
        params = []
        filters = []
        if product_no:
            filters.append(f"productNo eq '{product_no}'")
        elif product_nos:
            nos_filter = " or ".join(f"productNo eq '{n}'" for n in product_nos)
            filters.append(f"({nos_filter})")
        if on_date:
            filters.append(f"startingDate le {on_date}")
            filters.append(f"(endingDate ge {on_date} or endingDate eq 0001-01-01)")
        if odata_filter:
            filters.append(odata_filter)
        if filters:
            params.append(f"$filter={' and '.join(filters)}")
        params.append("$orderby=startingDate desc")
        if top:
            params.append(f"$top={top}")
        url += "?" + "&".join(params)
        if top == 1:
            response = _bc_request("get", url, headers=_auth_headers())
            data = _safe_json(response)
            if response.ok:
                _item_price_v2_cache[cache_key] = {"data": data, "expires_at": time.time() + _ITEM_PRICE_CACHE_TTL}
            return response.status_code, data
        else:
            records = _fetch_all_pages(url)
            data = {"value": records}
            _item_price_v2_cache[cache_key] = {"data": data, "expires_at": time.time() + _ITEM_PRICE_CACHE_TTL}
            return 200, data
    except Exception:
        entry = _item_price_v2_cache.get(cache_key)
        if entry is not None:
            return 200, entry["data"]
        raise


def rgmc_v2_get_item_price(record_id: str, company_name: str):
    """GET a single itemPrice record by GUID from the v2.0 RGMC custom API."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API_V2}/companies({company_id})/itemPrices({record_id})"
    response = _bc_request("get", url, headers=_auth_headers())
    return response.status_code, _safe_json(response)


def rgmc_v2_create_item_price(payload: dict, company_name: str):
    """POST a new itemPrice record to the v2.0 RGMC custom API."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API_V2}/companies({company_id})/itemPrices"
    headers = {**_auth_headers(), "Content-Type": "application/json"}
    response = _bc_request("post", url, json=payload, headers=headers)
    return response.status_code, _safe_json(response)


def rgmc_v2_update_item_price(record_id: str, payload: dict, company_name: str):
    """PATCH an existing itemPrice record in the v2.0 RGMC custom API."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API_V2}/companies({company_id})/itemPrices({record_id})"
    headers = {**_auth_headers(), "Content-Type": "application/json", "If-Match": "*"}
    response = _bc_request("patch", url, json=payload, headers=headers)
    return response.status_code, _safe_json(response)


def rgmc_v2_delete_item_price(record_id: str, company_name: str):
    """DELETE an itemPrice record from the v2.0 RGMC custom API."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API_V2}/companies({company_id})/itemPrices({record_id})"
    response = _bc_request("delete", url, headers=_auth_headers())
    return response.status_code


# ---------------------------------------------------------------------------
# RGMC Custom API v3.0 — Item Prices (Pag50318, read-only, one record per product)
# ---------------------------------------------------------------------------

_V3_CACHE_TTL = 86400  # 24 hours — prices change rarely; /refresh endpoint handles manual invalidation
_v3_refresh_lock = threading.Lock()
_v3_refreshing: set = set()

# Fields returned by Pag50318 that the Python cache and frontend actually use.
# BC returns 22 fields by default; we request only 11 via OData $select, cutting
# the JSON payload by ~50% (7-12 MB per full-catalog fetch).
# 'id' (SystemId) is the OData key and is always returned regardless of $select.
_V3_SELECT_FIELDS = (
    "productNo,description,unitPriceIncVAT,unitPrice,familyCode,"
    "itemId,itemCategoryCode,baseUnitOfMeasure,priceListCode,"
    "lastModifiedDateTime,blocked"
)

# Prefer header value sent on every v3 OData request.
# BC's default page size is 100; requesting 500 cuts round-trips 5× for a
# 5 000-product catalog (10-20 pages vs. 50-100).
_V3_PREFER_HEADER = {"Prefer": "odata.maxpagesize=500"}

# Three non-overlapping product-number ranges covering all BC product numbers.
# Pag50318's OnOpenPage pushes these down to SQL (via PriceListLine.SetFilter),
# so each range request scans only its share of the Price List Line table.
# Three parallel ranges cut per-range sequential page count by a further 33%
# while holding 3 of 4 semaphore slots, leaving 1 free for user traffic.
# Ranges are expressed as (min_inclusive, max_exclusive); None means open-ended.
_V3_CATALOG_RANGES = [
    (None, 'H'),   # product numbers < 'H'  (digits + A..G)
    ('H',  'Q'),   # product numbers 'H'..'P'
    ('Q',  None),  # product numbers >= 'Q' (Q..Z)
]


def _find_any_full_catalog_cache(company_name: str) -> dict | None:
    """Return the freshest full-catalog v3 cache entry for this company (any on_date).

    Used as stale fallback when the exact date key is missing — avoids a synchronous
    BC call for the common case where the warmup already populated a different date.
    Full-catalog key shape: (company, None, None, None, any_date, None).
    """
    best = None
    for key, entry in list(_item_price_v3_cache.items()):
        if key[0] == company_name and key[1] is None and key[2] is None and key[3] is None and key[5] is None:
            if best is None or entry.get("expires_at", 0) > best.get("expires_at", 0):
                best = entry
    return best


def _any_full_catalog_warming(company_name: str) -> bool:
    """True if a background thread is already fetching the full catalog for this company."""
    with _v3_refresh_lock:
        return any(
            k[0] == company_name and k[1] is None and k[2] is None and k[3] is None and k[5] is None
            for k in _v3_refreshing
        )


def _rgmc_v3_build_url(
    company_id: str,
    product_no: str,
    product_nos: list,
    family_code: str,
    on_date: str,
    odata_filter: str,
    bc_limit: int = None,
    bc_offset: int = None,
) -> str:
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API_V3}/companies({company_id})/itemPrices"
    filters = []
    if on_date:
        filters.append(f"onDate eq {on_date}")
    if product_no:
        filters.append(f"productNo eq '{product_no}'")
    elif product_nos:
        nos_filter = " or ".join(f"productNo eq '{n}'" for n in product_nos)
        filters.append(f"({nos_filter})")
    elif family_code:
        filters.append(f"familyCode eq '{family_code}'")
    if odata_filter:
        filters.append(odata_filter)
    # BC-native pagination: BC's OnOpenPage reads these from the OData filter and
    # applies them before inserting into the temp buffer — only the requested page
    # is ever in the response, so no Python-level slicing is needed.
    if bc_limit is not None:
        filters.append(f"limit eq {bc_limit}")
    if bc_offset is not None:
        filters.append(f"offset eq {bc_offset}")
    params = []
    if filters:
        params.append(f"$filter={' and '.join(filters)}")
    params.append(f"$select={_V3_SELECT_FIELDS}")
    url += f"?{'&'.join(params)}"
    return url


def _fetch_v3_catalog_parallel(company_id: str, on_date: str) -> list:
    """Fetch the full v3 item price catalog using parallel range requests.

    Each range is a separate BC OData call with a productNo range filter that
    Pag50318's OnOpenPage pushes down to SQL — so BC scans only its share of
    the Price List Line table.  Three ranges cut per-range sequential page count
    by 33% vs two ranges while holding 3 of 4 semaphore slots.
    Prefer: odata.maxpagesize=500 reduces round-trips from ~50 to ~10 per range.
    Partial success: if some ranges succeed and others fail, the successful records
    are returned. Only raises if every range failed (zero records collected).
    """
    def fetch_range(rng: tuple) -> list:
        min_no, max_no = rng
        parts = []
        if min_no:
            parts.append(f"productNo ge '{min_no}'")
        if max_no:
            parts.append(f"productNo lt '{max_no}'")
        range_filter = ' and '.join(parts) if parts else None
        url = _rgmc_v3_build_url(company_id, None, None, None, on_date, range_filter)
        logger.info(f"v3 parallel range fetch: {range_filter or 'all'} (company={company_id})")
        return _fetch_all_pages(url, extra_headers=_V3_PREFER_HEADER)

    all_records: list = []
    errors: list = []
    with ThreadPoolExecutor(max_workers=len(_V3_CATALOG_RANGES)) as executor:
        futures = {executor.submit(fetch_range, rng): rng for rng in _V3_CATALOG_RANGES}
        for future in as_completed(futures):
            try:
                all_records.extend(future.result())
            except Exception as exc:
                errors.append(exc)
                logger.warning(f"v3 range fetch failed (company={company_id}): {exc}")
    if errors and not all_records:
        # Every range failed — propagate so the caller retries.
        raise errors[0]
    if errors:
        # Partial success — some ranges failed but we have data. Log and continue.
        logger.warning(
            f"v3 catalog partial fetch: {len(errors)}/{len(_V3_CATALOG_RANGES)} ranges failed, "
            f"{len(all_records)} records recovered (company={company_id})"
        )
    return all_records


def _rgmc_v3_fetch_and_cache(cache_key: tuple, company_name: str, product_no: str, product_nos: list, family_code: str, on_date: str, odata_filter: str):
    """Fetch v3 item prices from BC and populate the cache. Runs in a background thread —
    no HTTP request timeout applies, so even slow BC OnOpenPage calls complete here."""
    try:
        company_id = get_company_id(company_name)
        # Full-catalog requests use parallel range fetching — each range acquires its own
        # semaphore slot so BC scans a subset of Price List Lines per request.
        if not product_no and not product_nos and not family_code and not odata_filter:
            records = _fetch_v3_catalog_parallel(company_id, on_date)
        else:
            url = _rgmc_v3_build_url(company_id, product_no, product_nos, family_code, on_date, odata_filter)
            records = _fetch_all_pages(url, extra_headers=_V3_PREFER_HEADER)
        data = {"value": records}
        # Evict expired v3 entries before writing so old full-catalog datasets (e.g. yesterday's
        # date key) are released immediately rather than waiting for the cleanup thread.
        _purge_expired_v3_cache()
        _item_price_v3_cache[cache_key] = {"data": data, "expires_at": time.time() + _V3_CACHE_TTL}
        logger.info(f"v3 item prices cache refreshed: {len(records)} records (company={company_name})")
    except Exception as e:
        logger.warning(f"v3 item prices background refresh failed: {e}")
    finally:
        with _v3_refresh_lock:
            _v3_refreshing.discard(cache_key)


def _trigger_v3_refresh(cache_key: tuple, company_name: str, product_no: str, product_nos: list, family_code: str, on_date: str, odata_filter: str):
    """Start a background refresh for the given cache key if one isn't already running."""
    with _v3_refresh_lock:
        if cache_key in _v3_refreshing:
            return
        _v3_refreshing.add(cache_key)
    threading.Thread(
        target=_rgmc_v3_fetch_and_cache,
        args=(cache_key, company_name, product_no, product_nos, family_code, on_date, odata_filter),
        daemon=True,
    ).start()


# Seconds to block waiting for the background catalog fetch before giving up with 503.
# Set to 55 s — just under nginx's 60 s proxy_read_timeout — so multiple retry
# attempts fit within the window (each attempt typically takes 6-20 s).
_V3_WARMUP_WAIT_S = 55


def _block_until_v3_catalog_ready(company_name: str, on_date: str, timeout: float = _V3_WARMUP_WAIT_S) -> dict | None:
    """Block until the full-catalog warmup finishes or the timeout expires.

    Polls every 300 ms. Crucially, if the background fetch fails (exception clears
    _v3_refreshing without populating the cache), this function automatically re-triggers
    the warmup instead of breaking out of the loop — allowing multiple retry attempts
    within the timeout window.  Returns the cache entry on success, None only after
    the deadline passes without data.
    """
    full_key = (company_name, None, None, None, on_date, None)
    _trigger_v3_refresh(full_key, company_name, None, None, None, on_date, None)
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.3)
        entry = _item_price_v3_cache.get(full_key) or _find_any_full_catalog_cache(company_name)
        if entry:
            return entry
        # If warming stopped but no data, the fetch failed — re-trigger if time allows.
        if not _any_full_catalog_warming(company_name):
            remaining = deadline - time.time()
            if remaining > 8:
                logger.warning(
                    f"v3 catalog fetch failed for {company_name!r}, "
                    f"re-triggering ({remaining:.0f}s remaining)"
                )
                _trigger_v3_refresh(full_key, company_name, None, None, None, on_date, None)
    return _item_price_v3_cache.get(full_key) or _find_any_full_catalog_cache(company_name)


def rgmc_v3_list_item_prices(
    company_name: str,
    product_no: str = None,
    product_nos: list = None,
    on_date: str = None,
    odata_filter: str = None,
    family_code: str = None,
    bc_limit: int = None,
    bc_offset: int = None,
):
    """GET itemPrices from the v3.0 RGMC custom API (Pag50318).

    Cache strategy (best-first):
    1. Exact cache key, fresh  → return immediately.
    2. Exact cache key, stale  → return immediately + background refresh.
    3. Full-catalog path only  → look for any cached full catalog (any date) as stale
       fallback; avoids a BC call when posting date ≠ warmup date.
    4. Full-catalog / family   → if no cache exists, block up to _V3_WARMUP_WAIT_S s for
       the background fetch to complete (6-20 s with current optimizations) — no 503.
    5. product_no path         → check full-catalog cache first; a single-item BC call
       is only made when no catalog data exists at all.
    6. Synchronous BC fetch    → last resort for filtered requests; result cached.

    BC's OnOpenPage returns one price per product: the highest Starting Date ≤ on_date
    (defaults to WorkDate). on_date is forwarded as $filter=onDate eq YYYY-MM-DD.
    familyCode is never sent to BC (it's a temp-buffer field BC rejects in OData);
    filtering is done in Python against the full-catalog cache.
    """
    # ── BC-native pagination short-circuit ──────────────────────────────────
    # When bc_limit or bc_offset is provided the caller wants a specific page
    # directly from BC. Skip all caching logic and return only the requested slice.
    if bc_limit is not None or bc_offset is not None:
        company_id = get_company_id(company_name)
        url = _rgmc_v3_build_url(
            company_id, product_no, product_nos, family_code, on_date, odata_filter,
            bc_limit=bc_limit, bc_offset=bc_offset,
        )
        records = _fetch_all_pages(url, extra_headers=_V3_PREFER_HEADER)
        return 200, {"value": records}

    cache_key = (company_name, product_no, tuple(product_nos) if product_nos else None, family_code, on_date, odata_filter)
    cached = _item_price_v3_cache.get(cache_key)

    # ── Step 1/2: exact key hit ──────────────────────────────────────────────
    if cached:
        if time.time() < cached["expires_at"]:
            return 200, cached["data"]
        _trigger_v3_refresh(cache_key, company_name, product_no, product_nos, family_code, on_date, odata_filter)
        return 200, cached["data"]

    # ── family_code path ─────────────────────────────────────────────────────
    # familyCode is a temp-buffer field; BC rejects it in OData. Always resolve
    # against the full-catalog cache and filter in Python.
    if family_code and not product_no and not product_nos and not odata_filter:
        full_key = (company_name, None, None, None, on_date, None)

        # Step 3: exact date in cache?
        full_cached = _item_price_v3_cache.get(full_key)
        if not full_cached:
            # Step 3b: any date's full catalog?
            full_cached = _find_any_full_catalog_cache(company_name)
            if full_cached:
                # Stale cross-date hit — trigger refresh for the requested date
                _trigger_v3_refresh(full_key, company_name, None, None, None, on_date, None)

        if full_cached:
            all_records = full_cached["data"].get("value", [])
            if time.time() >= full_cached.get("expires_at", 0):
                _trigger_v3_refresh(full_key, company_name, None, None, None, on_date, None)
            return 200, {"value": [r for r in all_records if r.get("familyCode") == family_code]}

        # No catalog cache — fetch directly from BC.
        company_id = get_company_id(company_name)
        records = _fetch_v3_catalog_parallel(company_id, on_date)
        data = {"value": records}
        _purge_expired_v3_cache()
        _item_price_v3_cache[full_key] = {"data": data, "expires_at": time.time() + _V3_CACHE_TTL}
        return 200, {"value": [r for r in records if r.get("familyCode") == family_code]}

    # ── product_no path ──────────────────────────────────────────────────────
    # Step 5: check full-catalog cache before going to BC for a single item.
    if product_no and not product_nos and not family_code and not odata_filter:
        any_catalog = _find_any_full_catalog_cache(company_name)
        if any_catalog:
            all_records = any_catalog["data"].get("value", [])
            match = next((r for r in all_records if r.get("productNo") == product_no), None)
            if match:
                return 200, {"value": [match]}

    # ── full-catalog path (no product / family filter) ───────────────────────
    if not product_no and not product_nos and not family_code and not odata_filter:
        # Step 3: any date's full catalog as stale fallback?
        any_catalog = _find_any_full_catalog_cache(company_name)
        if any_catalog:
            _trigger_v3_refresh(cache_key, company_name, None, None, None, on_date, None)
            return 200, any_catalog["data"]

    # ── Step 5b: product_nos path — serve from full catalog cache before BC ──
    # The per-batch cache key (unique to each product_nos tuple) is almost always
    # a miss, so without this step every date-change watcher call goes to BC.
    # Using the exact-date catalog avoids a BC call and returns the correct prices.
    if product_nos and not product_no and not family_code and not odata_filter:
        full_key = (company_name, None, None, None, on_date, None)
        exact_catalog = _item_price_v3_cache.get(full_key)
        if exact_catalog:
            nos_set = set(product_nos)
            return 200, {"value": [r for r in exact_catalog["data"].get("value", []) if r.get("productNo") in nos_set]}

    # ── Step 6: synchronous BC fetch ─────────────────────────────────────────
    # Full-catalog path: if no stale cache existed and no warmup was running at Step 4
    # (rare race), block here until the fetch finishes rather than returning 503.
    try:
        company_id = get_company_id(company_name)
        if not product_no and not product_nos and not family_code and not odata_filter:
            records = _fetch_v3_catalog_parallel(company_id, on_date)
        else:
            url = _rgmc_v3_build_url(company_id, product_no, product_nos, family_code, on_date, odata_filter)
            records = _fetch_all_pages(url, extra_headers=_V3_PREFER_HEADER)
        data = {"value": records}
        _purge_expired_v3_cache()
        _item_price_v3_cache[cache_key] = {"data": data, "expires_at": time.time() + _V3_CACHE_TTL}
        return 200, data
    except Exception:
        if cached is not None:
            return 200, cached["data"]
        # For product_nos requests, fall back to the exact-date full catalog only.
        # Using any-date catalog (_find_any_full_catalog_cache) would silently return
        # prices computed for a different effective date, which is incorrect.
        if product_nos and not product_no and not family_code and not odata_filter:
            full_key = (company_name, None, None, None, on_date, None)
            fallback = _item_price_v3_cache.get(full_key)
            if fallback:
                nos_set = set(product_nos)
                return 200, {"value": [r for r in fallback["data"].get("value", []) if r.get("productNo") in nos_set]}
        raise


def _purge_expired_v3_cache() -> None:
    """Remove expired entries from _item_price_v3_cache. Called before writing new entries."""
    now = time.time()
    for k in [k for k, v in list(_item_price_v3_cache.items()) if now >= v.get("expires_at", 0)]:
        _item_price_v3_cache.pop(k, None)


def _purge_all_expired_caches() -> None:
    """Remove expired entries from every TTL-based cache to prevent unbounded memory growth."""
    now = time.time()
    for cache in (_list_cache, _company_settings_cache, _item_price_v3_cache):
        for k in [k for k, v in list(cache.items()) if now >= v.get("expires_at", 0)]:
            cache.pop(k, None)
    for cache in (_item_price_cache, _item_price_v2_cache):
        expired = [k for k, v in list(cache.items()) if now >= v.get("expires_at", 0)]
        for k in expired:
            cache.pop(k, None)
        if len(cache) > _ITEM_PRICE_CACHE_MAX_SIZE:
            oldest = sorted(cache, key=lambda k: cache[k].get("expires_at", 0))
            for k in oldest[:len(cache) - _ITEM_PRICE_CACHE_MAX_SIZE]:
                cache.pop(k, None)


def _start_cache_cleanup() -> None:
    """Start a daemon thread that evicts expired cache entries every 5 minutes."""
    def _loop():
        while True:
            time.sleep(300)
            try:
                _purge_all_expired_caches()
            except Exception as exc:
                logger.warning(f"Cache cleanup error: {exc}")
    threading.Thread(target=_loop, daemon=True).start()


_start_cache_cleanup()


def rgmc_v3_warmup(company_name: str):
    """Trigger a background cache warm-up for the full v3 price list of company_name.
    Uses today's date so the cached key matches what the frontend actually requests.
    Called at startup and hourly so the cache is always warm."""
    today = datetime.date.today().isoformat()
    cache_key = (company_name, None, None, None, today, None)
    entry = _item_price_v3_cache.get(cache_key)
    if entry and time.time() < entry["expires_at"]:
        return  # Already warm for today
    _trigger_v3_refresh(cache_key, company_name, None, None, None, today, None)


def rgmc_v3_invalidate_cache(company_name: str = None):
    """Remove v3 cache entries. If company_name is given, only that company is cleared."""
    keys = [k for k in list(_item_price_v3_cache) if company_name is None or k[0] == company_name]
    for k in keys:
        _item_price_v3_cache.pop(k, None)


# ---------------------------------------------------------------------------
# v3 item-price count — Pag50319 (itemPriceCounts)
# Returns the number of distinct active products for a given date/family.
# Cached for 1 hour; invalidated when the v3 price cache is invalidated.
# ---------------------------------------------------------------------------
_v3_count_cache: dict = {}
_V3_COUNT_CACHE_TTL = 3600


def rgmc_v3_get_item_price_count(
    company_name: str,
    on_date: str,
    family_code: str = None,
    product_no: str = None,
) -> int:
    """Return the count of distinct active products from the itemPriceCounts endpoint (Pag50319)."""
    company_id = get_company_id(company_name)
    cache_key = (company_id, on_date, family_code, product_no)
    entry = _v3_count_cache.get(cache_key)
    if entry and time.time() < entry["expires_at"]:
        return entry["count"]
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API_V3}/companies({company_id})/itemPriceCounts"
    filters = [f"onDate eq {on_date}"]
    if family_code:
        filters.append(f"familyCode eq '{family_code}'")
    if product_no:
        filters.append(f"productNo eq '{product_no}'")
    url += f"?$filter={' and '.join(filters)}"
    records = _fetch_all_pages(url)
    count = int(records[0].get("totalCount", 0)) if records else 0
    _v3_count_cache[cache_key] = {"count": count, "expires_at": time.time() + _V3_COUNT_CACHE_TTL}
    logger.info(f"v3 count: {count} products (company={company_name}, date={on_date}, family={family_code})")
    return count


def get_api_status(company_name: str) -> dict:
    """Return live server status for the /bc/status endpoint.

    warming_up  — the full-catalog v3 price cache is currently being refreshed
                  from BC (happens at startup and every hour).
    active_bc_requests — how many BC HTTP fetches are in flight right now.
    busy        — active_bc_requests is high enough that new requests will likely
                  queue or time out (threshold: gunicorn has 3 workers × 2 threads = 6
                  slots; ≥ 4 active BC requests leaves little headroom).
    """
    today = datetime.date.today().isoformat()
    full_key = (company_name, None, None, None, today, None)
    with _v3_refresh_lock:
        v3_warming = full_key in _v3_refreshing
    with _cs_refresh_lock:
        cs_warming = bool(_cs_refreshing)
    with _list_refresh_lock:
        list_warming = bool(_list_refreshing)
    with _active_bc_lock:
        active = _active_bc_requests
    return {
        "warming_up": v3_warming or cs_warming or list_warming,
        "active_bc_requests": active,
        "busy": active >= 4,
    }


def rgmc_v3_get_item_price(record_id: str, company_name: str):
    """GET a single itemPrice record by SystemId from the v3.0 RGMC custom API."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API_V3}/companies({company_id})/itemPrices({record_id})"
    response = _bc_request("get", url, headers=_auth_headers())
    return response.status_code, _safe_json(response)


# ---------------------------------------------------------------------------
# RGMC Custom API v2.0 — Company Settings (Pag50492, EntitySet: companySettings)
# ---------------------------------------------------------------------------
# Pag50492's OnOpenPage iterates ALL BC companies and runs INSERT/MODIFY for each
# on every request — making every uncached GET expensive. The stale-while-revalidate
# cache below means BC is only hit when the cache is cold or expired, not on every call.

_company_settings_cache: dict = {}
_COMPANY_SETTINGS_TTL = 600  # 10 minutes; cleared immediately on any PATCH
_cs_refresh_lock = threading.Lock()
_cs_refreshing: set = set()


def _cs_fetch_and_cache(cache_key: tuple, company_name: str, odata_filter: str):
    """Fetch company settings from BC and populate the cache. Runs in a background thread."""
    try:
        company_id = get_company_id(company_name)
        url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API_V2}/companies({company_id})/companySettings"
        if odata_filter:
            url += f"?$filter={odata_filter}"
        records = _fetch_all_pages(url)
        data = {"value": records}
        _company_settings_cache[cache_key] = {"data": data, "expires_at": time.time() + _COMPANY_SETTINGS_TTL}
        logger.info(f"company settings cache refreshed: {len(records)} records (company={company_name})")
    except Exception as e:
        logger.warning(f"company settings background refresh failed: {e}")
    finally:
        with _cs_refresh_lock:
            _cs_refreshing.discard(cache_key)


def _trigger_cs_refresh(cache_key: tuple, company_name: str, odata_filter: str):
    with _cs_refresh_lock:
        if cache_key in _cs_refreshing:
            return
        _cs_refreshing.add(cache_key)
    threading.Thread(
        target=_cs_fetch_and_cache,
        args=(cache_key, company_name, odata_filter),
        daemon=True,
    ).start()


_CS_WARMUP_WAIT_S = 40  # max seconds to block waiting for company settings background fetch


def _block_until_cs_ready(cache_key: tuple, company_name: str, odata_filter: str, timeout: float = _CS_WARMUP_WAIT_S) -> dict | None:
    """Trigger a company settings background refresh if needed and poll until it finishes.

    Returns the cache entry dict on success, None if timed out or the refresh failed.
    Pag50492's OnOpenPage is slow (iterates all BC companies); never run it synchronously
    in the request path — always delegate to a background thread and block here instead.
    """
    _trigger_cs_refresh(cache_key, company_name, odata_filter)
    deadline = time.time() + timeout
    while time.time() < deadline:
        entry = _company_settings_cache.get(cache_key)
        if entry:
            return entry
        with _cs_refresh_lock:
            still_running = cache_key in _cs_refreshing
        if not still_running:
            break
        time.sleep(0.3)
    return _company_settings_cache.get(cache_key)


def rgmc_v2_list_company_settings(company_name: str, odata_filter: str = None):
    """GET companySettings for a company (Pag50492) with stale-while-revalidate caching.

    Pag50492's OnOpenPage iterates ALL BC companies on every request, making it too slow
    for synchronous use in the request path. Cold-start requests block here for up to
    _CS_WARMUP_WAIT_S seconds while the background thread fetches the data, then return 503
    if it is still not ready (client should retry with back-off).
    """
    cache_key = (company_name, odata_filter)
    cached = _company_settings_cache.get(cache_key)

    if cached:
        if time.time() < cached["expires_at"]:
            return 200, cached["data"]
        _trigger_cs_refresh(cache_key, company_name, odata_filter)
        return 200, cached["data"]

    # Cold cache: wait for background fetch rather than blocking the request thread with
    # a synchronous Pag50492 call (which can exceed nginx's proxy_read_timeout).
    entry = _block_until_cs_ready(cache_key, company_name, odata_filter)
    if entry:
        return 200, entry["data"]
    raise ServiceWarmingError("Company settings are loading — please retry shortly.")


def rgmc_v2_get_company_setting(setting_id: str, company_name: str):
    """GET a single companySettings record by key (Pag50492)."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API_V2}/companies({company_id})/companySettings({setting_id})"
    response = _bc_request("get", url, headers=_auth_headers())
    return response.status_code, _safe_json(response)


def rgmc_v2_update_company_setting(setting_id: str, payload: dict, company_name: str):
    """PATCH a companySettings record (Pag50492). Clears the cache on success."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API_V2}/companies({company_id})/companySettings({setting_id})"
    headers = {**_auth_headers(), "Content-Type": "application/json", "If-Match": "*"}
    response = _bc_request("patch", url, json=payload, headers=headers)
    if response.ok:
        _company_settings_cache.clear()
    return response.status_code, _safe_json(response)


def rgmc_v2_warmup_company_settings(company_name: str):
    """Trigger a background warm-up for company settings. Called at startup."""
    cache_key = (company_name, None)
    entry = _company_settings_cache.get(cache_key)
    if entry and time.time() < entry["expires_at"]:
        return
    _trigger_cs_refresh(cache_key, company_name, None)


def rgmc_v2_invalidate_company_settings_cache():
    """Clear the entire company settings cache (used by the /refresh endpoint)."""
    _company_settings_cache.clear()


# ---------------------------------------------------------------------------
# RGMC Custom API v2.0 — Customers (EntitySet: customers)
# ---------------------------------------------------------------------------

def rgmc_v2_list_customers(company_name: str, odata_filter: str = None):
    """GET all customers from the v2.0 RGMC custom API."""
    return call_rgmc_v2_table("customers", company_name, odata_filter=odata_filter)


def rgmc_v2_get_customer(customer_id: str, company_name: str):
    """GET a single customer by GUID from the v2.0 RGMC custom API."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API_V2}/companies({company_id})/customers({customer_id})"
    response = _bc_request("get", url, headers=_auth_headers())
    return response.status_code, _safe_json(response)


def rgmc_v2_create_customer(payload: dict, company_name: str):
    """POST a new customer to the v2.0 RGMC custom API."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API_V2}/companies({company_id})/customers"
    headers = {**_auth_headers(), "Content-Type": "application/json"}
    response = _bc_request("post", url, json=payload, headers=headers)
    return response.status_code, _safe_json(response)


def rgmc_v2_update_customer(customer_id: str, payload: dict, company_name: str):
    """PATCH an existing customer in the v2.0 RGMC custom API."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API_V2}/companies({company_id})/customers({customer_id})"
    headers = {**_auth_headers(), "Content-Type": "application/json", "If-Match": "*"}
    response = _bc_request("patch", url, json=payload, headers=headers)
    return response.status_code, _safe_json(response)


def rgmc_v2_delete_customer(customer_id: str, company_name: str):
    """DELETE a customer from the v2.0 RGMC custom API."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API_V2}/companies({company_id})/customers({customer_id})"
    response = _bc_request("delete", url, headers=_auth_headers())
    return response.status_code


# ---------------------------------------------------------------------------
# RGMC Custom API v2.0 — Generic CRUD helpers
# ---------------------------------------------------------------------------

def call_rgmc_v2_table(table_endpoint: str, company_name: str, odata_filter: str = None, expand: str = None, select: str = None):
    """LIST records from any v2.0 RGMC custom API entity set.

    Unfiltered requests are served from a 5-minute TTL cache.
    """
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API_V2}/companies({company_id})/{table_endpoint}"
    params = []
    if odata_filter:
        params.append(f"$filter={odata_filter}")
    if expand:
        params.append(f"$expand={expand}")
    if select:
        params.append(f"$select={select}")
    if params:
        url += "?" + "&".join(params)

    cache_key = ("rgmc_v2", table_endpoint, company_name.upper()) if not odata_filter and not expand and not select else None
    if cache_key:
        entry = _list_cache.get(cache_key)
        if entry:
            if time.time() < entry["expires_at"]:
                return 200, entry["data"]
            # Stale: return immediately and refresh in background
            _trigger_list_refresh(cache_key, url, _LIST_CACHE_TTL)
            return 200, entry["data"]
        # Cold cache: wait for the background warmup rather than blocking the request thread
        entry = _block_until_list_ready(cache_key, url, _LIST_CACHE_TTL)
        if entry:
            return 200, entry["data"]
        # Warmup timed out — fall through to synchronous fetch for this fast table

    try:
        records = _fetch_all_pages(url)
        data = {"value": records}
        if cache_key:
            _list_cache[cache_key] = {"data": data, "expires_at": time.time() + _LIST_CACHE_TTL}
        return 200, data
    except requests.HTTPError as e:
        if cache_key:
            entry = _list_cache.get(cache_key)
            if entry:
                return 200, entry["data"]
        return e.response.status_code, _safe_json(e.response)


def rgmc_v2_get_record(table_endpoint: str, record_id: str, company_name: str):
    """GET a single record by GUID from any v2.0 RGMC custom API entity set."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API_V2}/companies({company_id})/{table_endpoint}({record_id})"
    response = _bc_request("get", url, headers=_auth_headers())
    return response.status_code, _safe_json(response)


def rgmc_v2_create_record(table_endpoint: str, payload: dict, company_name: str):
    """POST a new record to any v2.0 RGMC custom API entity set."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API_V2}/companies({company_id})/{table_endpoint}"
    headers = {**_auth_headers(), "Content-Type": "application/json"}
    response = _bc_request("post", url, json=payload, headers=headers)
    return response.status_code, _safe_json(response)


def rgmc_v2_update_record(table_endpoint: str, record_id: str, payload: dict, company_name: str):
    """PATCH an existing record in any v2.0 RGMC custom API entity set."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API_V2}/companies({company_id})/{table_endpoint}({record_id})"
    headers = {**_auth_headers(), "Content-Type": "application/json", "If-Match": "*"}
    response = _bc_request("patch", url, json=payload, headers=headers)
    return response.status_code, _safe_json(response)


def rgmc_v2_delete_record(table_endpoint: str, record_id: str, company_name: str):
    """DELETE a record from any v2.0 RGMC custom API entity set."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API_V2}/companies({company_id})/{table_endpoint}({record_id})"
    response = _bc_request("delete", url, headers=_auth_headers())
    return response.status_code


# ---------------------------------------------------------------------------
# RGMC Custom API v2.0 — Contact Picture (Pag50309)
# ---------------------------------------------------------------------------

def rgmc_v2_get_contact_picture(contact_id: str, company_name: str):
    """GET contactPictures({contact_id}) from v2.0 — returns {id, contactNo, picture} where picture is base64."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API_V2}/companies({company_id})/contactPictures({contact_id})"
    response = _bc_request("get", url, headers=_auth_headers())
    return response.status_code, _safe_json(response)


def rgmc_v2_update_contact_picture(contact_id: str, picture_base64: str, company_name: str):
    """PATCH contactPictures({contact_id}) in v2.0 with a base64-encoded image string."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API_V2}/companies({company_id})/contactPictures({contact_id})"
    headers = {**_auth_headers(), "Content-Type": "application/json", "If-Match": "*"}
    response = _bc_request("patch", url, json={"picture": picture_base64}, headers=headers)
    return response.status_code, _safe_json(response)


# ---------------------------------------------------------------------------
# RGMC Custom API v2.0 — Contact Brand Tags (Pag50312 sub-resource)
# ---------------------------------------------------------------------------

def rgmc_v2_list_contact_brand_tags(contact_id: str, company_name: str):
    """GET contacts({contact_id})/contactBrandTags from v2.0."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API_V2}/companies({company_id})/contacts({contact_id})/contactBrandTags"
    response = _bc_request("get", url, headers=_auth_headers())
    return response.status_code, _safe_json(response)


def rgmc_v2_add_contact_brand_tag(contact_id: str, brand_code: str, company_name: str):
    """POST contacts({contact_id})/contactBrandTags in v2.0 — add a brand tag to a contact."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API_V2}/companies({company_id})/contacts({contact_id})/contactBrandTags"
    headers = {**_auth_headers(), "Content-Type": "application/json"}
    response = _bc_request("post", url, json={"brandCode": brand_code}, headers=headers)
    return response.status_code, _safe_json(response)


def rgmc_v2_delete_contact_brand_tag(contact_id: str, tag_id: str, company_name: str):
    """DELETE contacts({contact_id})/contactBrandTags({tag_id}) in v2.0."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API_V2}/companies({company_id})/contacts({contact_id})/contactBrandTags({tag_id})"
    response = _bc_request("delete", url, headers=_auth_headers())
    return response.status_code


# ---------------------------------------------------------------------------
# Startup warmup helpers — called at server start to pre-populate list caches
# ---------------------------------------------------------------------------

def warmup_bc_lists(company_name: str):
    """Pre-fetch BC v2.0 entity lists (contacts, customers, items, itemCategories) into the list cache."""
    for table in ("contacts", "customers", "items", "itemCategories"):
        try:
            call_bc_table(table, company_name)
            logger.info(f"List cache warmed: bc/{table} (company={company_name})")
        except Exception as e:
            logger.warning(f"List warmup failed bc/{table}: {e}")


def warmup_rgmc_lists(company_name: str):
    """Pre-fetch RGMC v1 entity lists (retailCustomers, contacts, items, itemFamilies) into the list cache."""
    for table in ("retailCustomers", "contacts", "items", "itemFamilies"):
        try:
            call_rgmc_table(table, company_name)
            logger.info(f"List cache warmed: rgmc/{table} (company={company_name})")
        except Exception as e:
            logger.warning(f"List warmup failed rgmc/{table}: {e}")


def warmup_rgmc_v2_lists(company_name: str):
    """Pre-fetch RGMC v2 entity lists (customers, retailCustomers, contacts, items, itemFamilies) into the list cache."""
    for table in ("customers", "retailCustomers", "contacts", "items", "itemFamilies"):
        try:
            call_rgmc_v2_table(table, company_name)
            logger.info(f"List cache warmed: rgmc_v2/{table} (company={company_name})")
        except Exception as e:
            logger.warning(f"List warmup failed rgmc_v2/{table}: {e}")


def warmup_dimension_lists(company_name: str):
    """Pre-fetch BRAND and DEPARTMENT dimension values into the list cache."""
    for code in ("BRAND", "DEPARTMENT"):
        try:
            get_dimension_values_by_code(code, company_name)
            logger.info(f"List cache warmed: dimension/{code} (company={company_name})")
        except Exception as e:
            logger.warning(f"List warmup failed dimension/{code}: {e}")
