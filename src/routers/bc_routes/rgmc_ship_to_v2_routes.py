"""RGMC custom API v2.0 — Ship-To Address read endpoints (Pag50350).

Read-only in BC (InsertAllowed/ModifyAllowed are both false on the AL page) — this is a
lookup/search endpoint only, for the SO-import buffer reconciliation UI to find an
existing ship-to address to link a buffered order's customer branch to.

`lookupCode` (added 2026-09-24 via tableextension 50458) holds the matching
customerLookupCode from SBIC's own CustomerBranch table on Cloud SQL — populated
manually in BC for now, searchable here so a human reconciling a buffered order can
find the right ship-to by whichever value they recognize.

Multi-field search (name/code/lookupCode) can't use a single OData $filter with `or`
across distinct fields — this BC environment's OData implementation rejects that with
"BadRequest_MethodNotImplemented: The 'OR' operator is not supported on distinct fields"
(confirmed live, not just a lookupCode-specific issue). Each field is queried separately
in parallel instead and the results merged/deduplicated by id.
"""
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, status

from src import config
from src.services.bc_functions import call_rgmc_v2_table

logger = logging.getLogger("bc_routes.rgmc_ship_to_v2")

rgmc_ship_to_v2_router = APIRouter(
    prefix="/bc/custom/v2/ship-to-addresses",
    tags=["BC RGMC Ship-To Addresses v2"],
)

_SEARCH_FIELDS = ("name", "code", "lookupCode")


def _unwrap_list(http_status: int, data: Any) -> List[Dict[str, Any]]:
    if http_status != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Business Central returned {http_status}: {data}",
        )
    return data.get("value", data)


def _is_unknown_property_error(http_status: int, data: Any, property_name: str) -> bool:
    """True if BC rejected the request because `property_name` doesn't exist on the entity.

    Used to skip the lookupCode query gracefully if that tableextension/page field
    hasn't been published to this BC environment yet, rather than erroring the whole
    search over one field that isn't live yet.
    """
    if http_status != 400:
        return False
    message = str(data)
    return f"'{property_name}'" in message and "property" in message.lower()


def _query_one_field(field: str, search: str, customer_no: Optional[str], extra_filter: Optional[str], company_name: str):
    esc = search.replace("'", "''")
    parts = [f"contains({field},'{esc}')"]
    if customer_no:
        esc_cust = customer_no.replace("'", "''")
        parts.append(f"customerNumber eq '{esc_cust}'")
    if extra_filter:
        parts.append(extra_filter)
    odata_filter = " and ".join(parts)
    return call_rgmc_v2_table("shipToAddresses", company_name=company_name, odata_filter=odata_filter)


@rgmc_ship_to_v2_router.get("", summary="List/search Ship-To Addresses (v2)")
def list_ship_to_addresses(
    search: Optional[str] = Query(
        None, description="Substring match against name, code, or lookupCode — each queried separately and merged"
    ),
    customer_no: Optional[str] = Query(None, description="Filter to ship-tos belonging to this BC customer number"),
    filter: Optional[str] = Query(None, description="Additional raw OData $filter expression"),
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    company_name = company or config.BC_COMPANY
    try:
        if not search:
            odata_filter = None
            if customer_no:
                esc = customer_no.replace("'", "''")
                odata_filter = f"customerNumber eq '{esc}'"
            if filter:
                odata_filter = f"{odata_filter} and {filter}" if odata_filter else filter
            http_status, data = call_rgmc_v2_table(
                "shipToAddresses", company_name=company_name, odata_filter=odata_filter,
            )
            return {"data": _unwrap_list(http_status, data)}

        with ThreadPoolExecutor(max_workers=len(_SEARCH_FIELDS)) as ex:
            futures = {
                field: ex.submit(_query_one_field, field, search, customer_no, filter, company_name)
                for field in _SEARCH_FIELDS
            }
            results = {field: fut.result() for field, fut in futures.items()}

        merged: Dict[str, Dict[str, Any]] = {}
        errors: Dict[str, str] = {}
        for field, (http_status, data) in results.items():
            if http_status == 200:
                for rec in data.get("value", []):
                    rid = rec.get("id")
                    if rid:
                        merged[rid] = rec
            elif field == "lookupCode" and _is_unknown_property_error(http_status, data, "lookupCode"):
                logger.info("shipToAddresses search: 'lookupCode' not published to BC yet — skipping that field")
            else:
                errors[field] = f"{http_status}: {data}"

        if not merged and errors:
            # Every field failed — surface the error instead of silently returning nothing.
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Business Central returned errors on every search field: {errors}",
            )

        return {"data": list(merged.values())}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing ship-to addresses (v2): {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
