"""RGMC custom API v2.0 — Ship-To Address read endpoints (Pag50350).

Read-only in BC (InsertAllowed/ModifyAllowed are both false on the AL page) — this is a
lookup/search endpoint only, for the SO-import buffer reconciliation UI to find an
existing ship-to address to link a buffered order's customer branch to.
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


@rgmc_ship_to_v2_router.get("", summary="List/search Ship-To Addresses (v2)")
def list_ship_to_addresses(
    search: Optional[str] = Query(None, description="Substring match on the ship-to name (BC contains())"),
    customer_no: Optional[str] = Query(None, description="Filter to ship-tos belonging to this BC customer number"),
    filter: Optional[str] = Query(None, description="Additional raw OData $filter expression"),
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    try:
        parts = []
        if search:
            esc = search.replace("'", "''")
            parts.append(f"contains(name,'{esc}')")
        if customer_no:
            esc = customer_no.replace("'", "''")
            parts.append(f"customerNumber eq '{esc}'")
        if filter:
            parts.append(filter)
        odata_filter = " and ".join(parts) if parts else None
        http_status, data = call_rgmc_v2_table(
            "shipToAddresses",
            company_name=company or config.BC_COMPANY,
            odata_filter=odata_filter,
        )
        return {"data": _unwrap_list(http_status, data)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing ship-to addresses (v2): {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
