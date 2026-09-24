"""RGMC custom API v2.0 — Ship-To Address read endpoints (Pag50350).

Read-only in BC (InsertAllowed/ModifyAllowed are both false on the AL page) — this is a
lookup/search endpoint only, for the SO-import buffer reconciliation UI to find an
existing ship-to address to link a buffered order's customer branch to.

`lookupCode` (added 2026-09-24 via tableextension 50458) holds the matching
customerLookupCode from SBIC's own CustomerBranch table on Cloud SQL — populated
manually in BC for now, searchable here so a human reconciling a buffered order can
find the right ship-to by whichever value they recognize.
"""
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, status

from src import config
from src.services.bc_functions import call_rgmc_v2_table

logger = logging.getLogger("bc_routes.rgmc_ship_to_v2")

rgmc_ship_to_v2_router = APIRouter(
    prefix="/bc/custom/v2/ship-to-addresses",
    tags=["BC RGMC Ship-To Addresses v2"],
)


def _unwrap_list(http_status: int, data: Any) -> List[Dict[str, Any]]:
    if http_status != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Business Central returned {http_status}: {data}",
        )
    return data.get("value", data)


def _is_unknown_property_error(http_status: int, data: Any, property_name: str) -> bool:
    """True if BC rejected the request because `property_name` doesn't exist on the entity.

    Used to fall back gracefully if the lookupCode tableextension/page field hasn't
    been published to this BC environment yet, rather than breaking search entirely.
    """
    if http_status != 400:
        return False
    message = str(data)
    return f"'{property_name}'" in message and "property" in message.lower()


def _build_filter(
    search: Optional[str], customer_no: Optional[str], extra_filter: Optional[str], include_lookup_code: bool
) -> Optional[str]:
    parts = []
    if search:
        esc = search.replace("'", "''")
        search_parts = [f"contains(name,'{esc}')", f"contains(code,'{esc}')"]
        if include_lookup_code:
            search_parts.append(f"contains(lookupCode,'{esc}')")
        parts.append("(" + " or ".join(search_parts) + ")")
    if customer_no:
        esc = customer_no.replace("'", "''")
        parts.append(f"customerNumber eq '{esc}'")
    if extra_filter:
        parts.append(extra_filter)
    return " and ".join(parts) if parts else None


@rgmc_ship_to_v2_router.get("", summary="List/search Ship-To Addresses (v2)")
def list_ship_to_addresses(
    search: Optional[str] = Query(
        None, description="Substring match against name, code, or lookupCode (BC contains(), OR'd across all three)"
    ),
    customer_no: Optional[str] = Query(None, description="Filter to ship-tos belonging to this BC customer number"),
    filter: Optional[str] = Query(None, description="Additional raw OData $filter expression"),
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    company_name = company or config.BC_COMPANY
    try:
        odata_filter = _build_filter(search, customer_no, filter, include_lookup_code=True)
        http_status, data = call_rgmc_v2_table(
            "shipToAddresses", company_name=company_name, odata_filter=odata_filter,
        )

        if search and _is_unknown_property_error(http_status, data, "lookupCode"):
            # lookupCode isn't published to BC yet — retry without it instead of
            # breaking search entirely. Remove this fallback once it's confirmed live.
            logger.warning("shipToAddresses search: 'lookupCode' not found on BC yet — retrying without it")
            odata_filter = _build_filter(search, customer_no, filter, include_lookup_code=False)
            http_status, data = call_rgmc_v2_table(
                "shipToAddresses", company_name=company_name, odata_filter=odata_filter,
            )

        return {"data": _unwrap_list(http_status, data)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing ship-to addresses (v2): {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
