"""All Business Central API related functions."""
import datetime
import logging
import time
import threading
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

# Shared HTTP session with connection pooling — reuses TCP+TLS connections across all BC calls.
# pool_maxsize=20: up to 20 simultaneous keep-alive connections to api.businesscentral.dynamics.com.
_session = requests.Session()
_http_adapter = HTTPAdapter(pool_connections=4, pool_maxsize=20)
_session.mount("https://", _http_adapter)
_session.mount("http://", _http_adapter)


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


def call_business_central_api(endpoint: str):
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/api/v2.0/{endpoint}"
    response = _session.get(url, headers=_auth_headers())
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


def _fetch_all_pages(url: str, max_retries: int = 4) -> list:
    """Follow @odata.nextLink pages and return the combined value list.

    Retries on 429/502/503 up to max_retries times.  429 honours Retry-After;
    502/503 use exponential backoff (1s, 2s, 4s, 8s).
    """
    all_records = []
    while url:
        print(f"Fetching BC data from: {url}")
        for attempt in range(max_retries + 1):
            response = _session.get(url, headers=_auth_headers(), timeout=120)
            if response.status_code not in (429, 502, 503):
                break
            if response.status_code == 429:
                wait = int(response.headers.get("Retry-After", 2 ** attempt))
                logger.warning(f"BC rate-limited (429). Waiting {wait}s (attempt {attempt + 1}/{max_retries}).")
            else:
                wait = min(2 ** attempt, 16)
                logger.warning(f"BC transient error ({response.status_code}). Waiting {wait}s (attempt {attempt + 1}/{max_retries}).")
            time.sleep(wait)
        response.raise_for_status()
        data = response.json()
        all_records.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
    return all_records


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
        if entry and time.time() < entry["expires_at"]:
            return 200, entry["data"]

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
    response = _session.get(url, headers=_auth_headers())
    return response.status_code, response.json()


def bc_create_record(table_endpoint: str, payload: dict, company_name: str):
    """POST a new record to a company-scoped BC table."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/api/v2.0/companies({company_id})/{table_endpoint}"
    headers = {**_auth_headers(), "Content-Type": "application/json"}
    response = _session.post(url, json=payload, headers=headers)
    return response.status_code, response.json()


def bc_update_record(table_endpoint: str, record_id: str, payload: dict, company_name: str):
    """PATCH an existing record in a company-scoped BC table."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/api/v2.0/companies({company_id})/{table_endpoint}({record_id})"
    headers = {**_auth_headers(), "Content-Type": "application/json", "If-Match": "*"}
    response = _session.patch(url, json=payload, headers=headers)
    return response.status_code, response.json() if response.content else {}


def bc_delete_record(table_endpoint: str, record_id: str, company_name: str):
    """DELETE a record from a company-scoped BC table."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/api/v2.0/companies({company_id})/{table_endpoint}({record_id})"
    response = _session.delete(url, headers=_auth_headers())
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
        if entry and time.time() < entry["expires_at"]:
            return 200, entry["data"]

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
    response = _session.get(url, headers=_auth_headers())
    return response.status_code, response.json()


def rgmc_create_record(table_endpoint: str, payload: dict, company_name: str):
    """POST a new record to a RGMC custom API table."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API}/companies({company_id})/{table_endpoint}"
    headers = {**_auth_headers(), "Content-Type": "application/json"}
    response = _session.post(url, json=payload, headers=headers)
    return response.status_code, _safe_json(response)


def rgmc_update_record(table_endpoint: str, record_id: str, payload: dict, company_name: str):
    """PATCH an existing record in a RGMC custom API table."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API}/companies({company_id})/{table_endpoint}({record_id})"
    headers = {**_auth_headers(), "Content-Type": "application/json", "If-Match": "*"}
    response = _session.patch(url, json=payload, headers=headers)
    return response.status_code, response.json() if response.content else {}


def rgmc_delete_record(table_endpoint: str, record_id: str, company_name: str):
    """DELETE a record from a RGMC custom API table."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API}/companies({company_id})/{table_endpoint}({record_id})"
    response = _session.delete(url, headers=_auth_headers())
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
    response = _session.get(url, headers=_auth_headers())
    return response.status_code, _safe_json(response)


def rgmc_update_contact_picture(contact_id: str, picture_base64: str, company_name: str):
    """PATCH contactPictures({contact_id}) with a base64-encoded image string. Insert/Delete not allowed by AL."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API}/companies({company_id})/contactPictures({contact_id})"
    headers = {**_auth_headers(), "Content-Type": "application/json", "If-Match": "*"}
    response = _session.patch(url, json={"picture": picture_base64}, headers=headers)
    return response.status_code, _safe_json(response)


def rgmc_list_contact_brand_tags(contact_id: str, company_name: str):
    """GET contacts({contact_id})/contactBrandTags — all brand tags for a contact (Pag50209)."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API}/companies({company_id})/contacts({contact_id})/contactBrandTags"
    response = _session.get(url, headers=_auth_headers())
    return response.status_code, _safe_json(response)


def rgmc_add_contact_brand_tag(contact_id: str, brand_code: str, company_name: str):
    """POST contacts({contact_id})/contactBrandTags — add a brand tag to a contact (Pag50209)."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API}/companies({company_id})/contacts({contact_id})/contactBrandTags"
    headers = {**_auth_headers(), "Content-Type": "application/json"}
    response = _session.post(url, json={"brandCode": brand_code}, headers=headers)
    return response.status_code, _safe_json(response)


def rgmc_delete_contact_brand_tag(contact_id: str, tag_id: str, company_name: str):
    """DELETE contacts({contact_id})/contactBrandTags({tag_id}) — remove a brand tag (Pag50209)."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API}/companies({company_id})/contacts({contact_id})/contactBrandTags({tag_id})"
    response = _session.delete(url, headers=_auth_headers())
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
            response = _session.get(url, headers=_auth_headers())
            data = _safe_json(response)
            if response.ok:
                _item_price_cache[cache_key] = data
            return response.status_code, data
        else:
            records = _fetch_all_pages(url)
            data = {"value": records}
            _item_price_cache[cache_key] = data
            return 200, data
    except Exception:
        cached = _item_price_cache.get(cache_key)
        if cached is not None:
            return 200, cached
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
    for cache_key, cached_data in _item_price_cache.items():
        key_company, key_product_no, key_on_date = cache_key[0], cache_key[1], cache_key[3]
        if key_company.upper() != target_company:
            continue
        if key_product_no != product_no:
            continue
        if on_date and key_on_date != on_date:
            continue
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
    if entry and time.time() < entry["expires_at"]:
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
            response = _session.get(url, headers=_auth_headers())
            data = _safe_json(response)
            if response.ok:
                _item_price_v2_cache[cache_key] = data
            return response.status_code, data
        else:
            records = _fetch_all_pages(url)
            data = {"value": records}
            _item_price_v2_cache[cache_key] = data
            return 200, data
    except Exception:
        cached = _item_price_v2_cache.get(cache_key)
        if cached is not None:
            return 200, cached
        raise


def rgmc_v2_get_item_price(record_id: str, company_name: str):
    """GET a single itemPrice record by GUID from the v2.0 RGMC custom API."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API_V2}/companies({company_id})/itemPrices({record_id})"
    response = _session.get(url, headers=_auth_headers())
    return response.status_code, _safe_json(response)


def rgmc_v2_create_item_price(payload: dict, company_name: str):
    """POST a new itemPrice record to the v2.0 RGMC custom API."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API_V2}/companies({company_id})/itemPrices"
    headers = {**_auth_headers(), "Content-Type": "application/json"}
    response = _session.post(url, json=payload, headers=headers)
    return response.status_code, _safe_json(response)


def rgmc_v2_update_item_price(record_id: str, payload: dict, company_name: str):
    """PATCH an existing itemPrice record in the v2.0 RGMC custom API."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API_V2}/companies({company_id})/itemPrices({record_id})"
    headers = {**_auth_headers(), "Content-Type": "application/json", "If-Match": "*"}
    response = _session.patch(url, json=payload, headers=headers)
    return response.status_code, _safe_json(response)


def rgmc_v2_delete_item_price(record_id: str, company_name: str):
    """DELETE an itemPrice record from the v2.0 RGMC custom API."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API_V2}/companies({company_id})/itemPrices({record_id})"
    response = _session.delete(url, headers=_auth_headers())
    return response.status_code


# ---------------------------------------------------------------------------
# RGMC Custom API v3.0 — Item Prices (Pag50318, read-only, one record per product)
# ---------------------------------------------------------------------------

_V3_CACHE_TTL = 86400  # 24 hours — prices change rarely; /refresh endpoint handles manual invalidation
_v3_refresh_lock = threading.Lock()
_v3_refreshing: set = set()


def _rgmc_v3_build_url(company_id: str, product_no: str, product_nos: list, family_code: str, on_date: str, odata_filter: str) -> str:
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
    if filters:
        url += f"?$filter={' and '.join(filters)}"
    return url


def _rgmc_v3_fetch_and_cache(cache_key: tuple, company_name: str, product_no: str, product_nos: list, family_code: str, on_date: str, odata_filter: str):
    """Fetch v3 item prices from BC and populate the cache. Runs in a background thread —
    no HTTP request timeout applies, so even slow BC OnOpenPage calls complete here."""
    try:
        company_id = get_company_id(company_name)
        url = _rgmc_v3_build_url(company_id, product_no, product_nos, family_code, on_date, odata_filter)
        records = _fetch_all_pages(url)
        data = {"value": records}
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


def rgmc_v3_list_item_prices(
    company_name: str,
    product_no: str = None,
    product_nos: list = None,
    on_date: str = None,
    odata_filter: str = None,
    family_code: str = None,
):
    """GET itemPrices from the v3.0 RGMC custom API (Pag50318).

    Uses stale-while-revalidate caching to avoid 504s from BC's expensive OnOpenPage:
    - Fresh cache hit  → returned immediately.
    - Stale cache hit  → stale data returned immediately; background thread refreshes.
    - Cache miss       → synchronous fetch (first call after a cold start may be slow).

    BC's OnOpenPage returns one price per product: the highest Starting Date <= on_date
    (defaults to WorkDate). on_date is forwarded as $filter=onDate eq YYYY-MM-DD, setting
    the Effective Date FlowFilter that the AL trigger reads.
    """
    cache_key = (company_name, product_no, tuple(product_nos) if product_nos else None, family_code, on_date, odata_filter)
    cached = _item_price_v3_cache.get(cache_key)

    # family_code path: always resolve against the full-catalog cache, never send a
    # familyCode OData filter to BC. BC rejects it with 400 because familyCode lives
    # on the temp buffer populated in OnOpenPage, not on the source table.
    if family_code and not product_no and not product_nos and not odata_filter:
        full_key = (company_name, None, None, None, on_date, None)
        full_cached = _item_price_v3_cache.get(full_key)
        if full_cached:
            all_records = full_cached["data"].get("value", [])
            filtered = [r for r in all_records if r.get("familyCode") == family_code]
            if time.time() >= full_cached["expires_at"]:
                _trigger_v3_refresh(full_key, company_name, None, None, None, on_date, None)
            return 200, {"value": filtered}
        # Full catalog not cached yet. If the startup warmup is already fetching it,
        # wait up to 30 s so we can serve from cache instead of launching a second
        # concurrent BC call. Both paths fetch the full catalog without any familyCode
        # OData filter (which BC rejects on a temp-table page like Pag50318).
        with _v3_refresh_lock:
            warmup_active = full_key in _v3_refreshing
        if warmup_active:
            deadline = time.time() + 30
            while time.time() < deadline:
                time.sleep(0.5)
                full_cached = _item_price_v3_cache.get(full_key)
                if full_cached:
                    break
            if full_cached:
                all_records = full_cached["data"].get("value", [])
                filtered = [r for r in all_records if r.get("familyCode") == family_code]
                return 200, {"value": filtered}

        try:
            company_id = get_company_id(company_name)
            url = _rgmc_v3_build_url(company_id, None, None, None, on_date, None)
            records = _fetch_all_pages(url)
            data = {"value": records}
            _item_price_v3_cache[full_key] = {"data": data, "expires_at": time.time() + _V3_CACHE_TTL}
            logger.info(f"v3 item prices (full catalog) cached on demand: {len(records)} records (company={company_name})")
            return 200, {"value": [r for r in records if r.get("familyCode") == family_code]}
        except Exception:
            if cached is not None:
                return 200, cached["data"]
            raise

    if cached:
        if time.time() < cached["expires_at"]:
            return 200, cached["data"]
        # Stale: return existing data immediately and refresh in background
        _trigger_v3_refresh(cache_key, company_name, product_no, product_nos, family_code, on_date, odata_filter)
        return 200, cached["data"]

    # Cache miss: fetch synchronously (product_no / product_nos / odata_filter paths)
    try:
        company_id = get_company_id(company_name)
        url = _rgmc_v3_build_url(company_id, product_no, product_nos, family_code, on_date, odata_filter)
        records = _fetch_all_pages(url)
        data = {"value": records}
        _item_price_v3_cache[cache_key] = {"data": data, "expires_at": time.time() + _V3_CACHE_TTL}
        return 200, data
    except Exception:
        if cached is not None:
            return 200, cached["data"]
        raise


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


def rgmc_v3_get_item_price(record_id: str, company_name: str):
    """GET a single itemPrice record by SystemId from the v3.0 RGMC custom API."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API_V3}/companies({company_id})/itemPrices({record_id})"
    response = _session.get(url, headers=_auth_headers())
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


def rgmc_v2_list_company_settings(company_name: str, odata_filter: str = None):
    """GET companySettings for a company (Pag50492) with stale-while-revalidate caching."""
    cache_key = (company_name, odata_filter)
    cached = _company_settings_cache.get(cache_key)

    if cached:
        if time.time() < cached["expires_at"]:
            return 200, cached["data"]
        _trigger_cs_refresh(cache_key, company_name, odata_filter)
        return 200, cached["data"]

    try:
        company_id = get_company_id(company_name)
        url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API_V2}/companies({company_id})/companySettings"
        if odata_filter:
            url += f"?$filter={odata_filter}"
        records = _fetch_all_pages(url)
        data = {"value": records}
        _company_settings_cache[cache_key] = {"data": data, "expires_at": time.time() + _COMPANY_SETTINGS_TTL}
        return 200, data
    except requests.HTTPError as e:
        if cached:
            return 200, cached["data"]
        return e.response.status_code, _safe_json(e.response)


def rgmc_v2_get_company_setting(setting_id: str, company_name: str):
    """GET a single companySettings record by key (Pag50492)."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API_V2}/companies({company_id})/companySettings({setting_id})"
    response = _session.get(url, headers=_auth_headers())
    return response.status_code, _safe_json(response)


def rgmc_v2_update_company_setting(setting_id: str, payload: dict, company_name: str):
    """PATCH a companySettings record (Pag50492). Clears the cache on success."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API_V2}/companies({company_id})/companySettings({setting_id})"
    headers = {**_auth_headers(), "Content-Type": "application/json", "If-Match": "*"}
    response = _session.patch(url, json=payload, headers=headers)
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
    response = _session.get(url, headers=_auth_headers())
    return response.status_code, _safe_json(response)


def rgmc_v2_create_customer(payload: dict, company_name: str):
    """POST a new customer to the v2.0 RGMC custom API."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API_V2}/companies({company_id})/customers"
    headers = {**_auth_headers(), "Content-Type": "application/json"}
    response = _session.post(url, json=payload, headers=headers)
    return response.status_code, _safe_json(response)


def rgmc_v2_update_customer(customer_id: str, payload: dict, company_name: str):
    """PATCH an existing customer in the v2.0 RGMC custom API."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API_V2}/companies({company_id})/customers({customer_id})"
    headers = {**_auth_headers(), "Content-Type": "application/json", "If-Match": "*"}
    response = _session.patch(url, json=payload, headers=headers)
    return response.status_code, _safe_json(response)


def rgmc_v2_delete_customer(customer_id: str, company_name: str):
    """DELETE a customer from the v2.0 RGMC custom API."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API_V2}/companies({company_id})/customers({customer_id})"
    response = _session.delete(url, headers=_auth_headers())
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
        if entry and time.time() < entry["expires_at"]:
            return 200, entry["data"]

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
    response = _session.get(url, headers=_auth_headers())
    return response.status_code, _safe_json(response)


def rgmc_v2_create_record(table_endpoint: str, payload: dict, company_name: str):
    """POST a new record to any v2.0 RGMC custom API entity set."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API_V2}/companies({company_id})/{table_endpoint}"
    headers = {**_auth_headers(), "Content-Type": "application/json"}
    response = _session.post(url, json=payload, headers=headers)
    return response.status_code, _safe_json(response)


def rgmc_v2_update_record(table_endpoint: str, record_id: str, payload: dict, company_name: str):
    """PATCH an existing record in any v2.0 RGMC custom API entity set."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API_V2}/companies({company_id})/{table_endpoint}({record_id})"
    headers = {**_auth_headers(), "Content-Type": "application/json", "If-Match": "*"}
    response = _session.patch(url, json=payload, headers=headers)
    return response.status_code, _safe_json(response)


def rgmc_v2_delete_record(table_endpoint: str, record_id: str, company_name: str):
    """DELETE a record from any v2.0 RGMC custom API entity set."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API_V2}/companies({company_id})/{table_endpoint}({record_id})"
    response = _session.delete(url, headers=_auth_headers())
    return response.status_code


# ---------------------------------------------------------------------------
# RGMC Custom API v2.0 — Contact Picture (Pag50309)
# ---------------------------------------------------------------------------

def rgmc_v2_get_contact_picture(contact_id: str, company_name: str):
    """GET contactPictures({contact_id}) from v2.0 — returns {id, contactNo, picture} where picture is base64."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API_V2}/companies({company_id})/contactPictures({contact_id})"
    response = _session.get(url, headers=_auth_headers())
    return response.status_code, _safe_json(response)


def rgmc_v2_update_contact_picture(contact_id: str, picture_base64: str, company_name: str):
    """PATCH contactPictures({contact_id}) in v2.0 with a base64-encoded image string."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API_V2}/companies({company_id})/contactPictures({contact_id})"
    headers = {**_auth_headers(), "Content-Type": "application/json", "If-Match": "*"}
    response = _session.patch(url, json={"picture": picture_base64}, headers=headers)
    return response.status_code, _safe_json(response)


# ---------------------------------------------------------------------------
# RGMC Custom API v2.0 — Contact Brand Tags (Pag50312 sub-resource)
# ---------------------------------------------------------------------------

def rgmc_v2_list_contact_brand_tags(contact_id: str, company_name: str):
    """GET contacts({contact_id})/contactBrandTags from v2.0."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API_V2}/companies({company_id})/contacts({contact_id})/contactBrandTags"
    response = _session.get(url, headers=_auth_headers())
    return response.status_code, _safe_json(response)


def rgmc_v2_add_contact_brand_tag(contact_id: str, brand_code: str, company_name: str):
    """POST contacts({contact_id})/contactBrandTags in v2.0 — add a brand tag to a contact."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API_V2}/companies({company_id})/contacts({contact_id})/contactBrandTags"
    headers = {**_auth_headers(), "Content-Type": "application/json"}
    response = _session.post(url, json={"brandCode": brand_code}, headers=headers)
    return response.status_code, _safe_json(response)


def rgmc_v2_delete_contact_brand_tag(contact_id: str, tag_id: str, company_name: str):
    """DELETE contacts({contact_id})/contactBrandTags({tag_id}) in v2.0."""
    company_id = get_company_id(company_name)
    url = f"{_BC_BASE}/{BC_TENANT_ID}/{BC_ENVIRONMENT}/{_RGMC_CUSTOM_API_V2}/companies({company_id})/contacts({contact_id})/contactBrandTags({tag_id})"
    response = _session.delete(url, headers=_auth_headers())
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
