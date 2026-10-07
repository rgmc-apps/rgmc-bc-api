"""Custom Connector API — Ship-to Address read endpoints (Pag51014).

Same separate AL app as custom_connector_item_attributes_routes.py (github.com/Aaron-Alvarez-RGMC/
custom-connector-AL), own APIPublisher/APIGroup/object ID range - not part of the shared rgmc/
rgmccustom namespace.

Endpoints:
  GET /bc/custom-connector/ship-to-addresses         — list
  GET /bc/custom-connector/ship-to-addresses/{id}    — get single record by SystemId

Fields: id, customerNo, code, name, name2, address, address2, city, postCode, county, countryRegionCode, contact, phoneNo, email, locationCode, salespersonCode, shippingAgentCode, shipmentMethodCode, companyName, lastModifiedDateTime.
customerNo + code is the natural key. Same table as the shared extension's /bc/custom/v2/ship-to-addresses (Pag50350) but
flat, with the incremental cursor and paging Airbyte needs, and without the hand-entered Lookup Code (see the pending
Pag51018 in the AL repo).
"""
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, status

from src import config
from src.services.bc_functions import (
    call_custom_connector_table,
    custom_connector_get_record,
)

logger = logging.getLogger("bc_routes.custom_connector_ship_to_address")

custom_connector_ship_to_address_router = APIRouter(
    prefix="/bc/custom-connector/ship-to-addresses",
    tags=["Custom Connector — Ship-to Address"],
)

_TABLE = "shipToAddresses"


def _unwrap_list(http_status: int, data: Any) -> List[Dict[str, Any]]:
    if http_status != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Business Central returned {http_status}: {data}",
        )
    return data.get("value", data)


@custom_connector_ship_to_address_router.get("", summary="List Ship-to Addresses")
def list_ship_to_addresss(
    filter: Optional[str] = Query(None, description="OData $filter expression (e.g. customerNo eq 'WA062')"),
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
    modified_from: Optional[str] = Query(
        None,
        description="Only records with lastModifiedDateTime on/after this ISO 8601 datetime (e.g. from Airbyte's incremental cursor). Combined with `filter` via AND if both are given.",
    ),
    limit: Optional[int] = Query(None, ge=1, le=5000, description="Max records to return in this page (BC $top). Omit for all matching records."),
    offset: Optional[int] = Query(None, ge=0, description="Records to skip before this page (BC $skip)."),
):
    """Return Ship-to Address records from BC (Pag51014)."""
    try:
        company_name = company or config.BC_COMPANY
        odata_filter = filter
        if modified_from:
            clause = f"lastModifiedDateTime ge {modified_from}"
            odata_filter = f"({odata_filter}) and {clause}" if odata_filter else clause
        http_status, data = call_custom_connector_table(
            _TABLE, company_name=company_name, odata_filter=odata_filter, top=limit, skip=offset
        )
        return {"data": _unwrap_list(http_status, data)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing ship-to addresss: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@custom_connector_ship_to_address_router.get("/{record_id}", summary="Get Ship-to Address by ID")
def get_ship_to_address(
    record_id: str,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    """Fetch a single Ship-to Address record by SystemId from BC."""
    try:
        http_status, data = custom_connector_get_record(
            table_endpoint=_TABLE,
            record_id=record_id,
            company_name=company or config.BC_COMPANY,
        )
        if http_status == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Ship-to Address record '{record_id}' not found",
            )
        if http_status != 200:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Business Central returned {http_status}: {data}",
            )
        return data
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching ship-to address {record_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
