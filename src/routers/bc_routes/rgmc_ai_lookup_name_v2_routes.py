"""RGMC custom API v2.0 — AI Lookup Name read endpoints (Pag50302, read-only).

Fields: id, customerNumber, code, name. `name` holds the customer name as it
appears on uploaded PO documents, used to match the PO to a BC customer.
"""
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, status

from src import config
from src.services.bc_functions import call_rgmc_v2_table, odata_escape, rgmc_v2_get_record

logger = logging.getLogger("bc_routes.rgmc_ai_lookup_name_v2")

rgmc_ai_lookup_name_v2_router = APIRouter(
    prefix="/bc/custom/v2/ai-lookup-names",
    tags=["BC RGMC AI Lookup Names v2"],
)

_TABLE = "aiLookupNames"


def _unwrap_list(http_status: int, data: Any) -> List[Dict[str, Any]]:
    if http_status != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Business Central returned {http_status}: {data}",
        )
    return data.get("value", data)


def _unwrap_single(http_status: int, data: Any) -> Dict[str, Any]:
    if http_status == 404:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="AI Lookup Name not found")
    if http_status != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Business Central returned {http_status}: {data}",
        )
    return data


@rgmc_ai_lookup_name_v2_router.get("", summary="List/search RGMC AI Lookup Names v2")
def list_rgmc_ai_lookup_names_v2(
    customer_no: Optional[str] = Query(None, description="Filter to lookup names belonging to this BC customer number"),
    search: Optional[str] = Query(None, description="Substring match against name"),
    filter: Optional[str] = Query(None, description="Additional raw OData $filter expression"),
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    try:
        filters = []
        if customer_no:
            filters.append(f"customerNumber eq '{odata_escape(customer_no)}'")
        if search:
            filters.append(f"contains(name,'{odata_escape(search)}')")
        if filter:
            filters.append(filter)
        combined_filter = " and ".join(filters) if filters else None
        http_status, data = call_rgmc_v2_table(
            _TABLE, company_name=company or config.BC_COMPANY, odata_filter=combined_filter
        )
        return {"data": _unwrap_list(http_status, data)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing RGMC AI lookup names v2: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_ai_lookup_name_v2_router.get("/{record_id}", summary="Get RGMC AI Lookup Name v2 by ID")
def get_rgmc_ai_lookup_name_v2(
    record_id: str,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    try:
        http_status, data = rgmc_v2_get_record(_TABLE, record_id, company_name=company or config.BC_COMPANY)
        return _unwrap_single(http_status, data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching RGMC AI lookup name v2 {record_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
