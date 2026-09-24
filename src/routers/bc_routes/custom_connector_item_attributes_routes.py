"""Custom Connector API — Item Attributes read endpoints (Pag51000).

Separate AL app (github.com/Aaron-Alvarez-RGMC/custom-connector-AL), own APIPublisher/
APIGroup/object ID range - not part of Erwin's rgmc/rgmccustom namespace, on purpose.

Endpoints:
  GET /bc/custom-connector/item-attributes         — list item attributes from BC (cached 5 min)
  GET /bc/custom-connector/item-attributes/{id}    — get single record by SystemId from BC

Fields: id, number, lineUpSeason, attrib1Code..attrib5Code, vendorItemNo, no2,
lastModifiedDateTime. Does NOT include a product-group-code equivalent - the only
confirmed field for that ("LSC Retail Product Code") lives on Item Ledger Entry, not
Item, and still needs resolving before it can be added anywhere.
"""
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, status

from src import config
from src.services.bc_functions import (
    call_custom_connector_table,
    custom_connector_get_record,
)

logger = logging.getLogger("bc_routes.custom_connector_item_attributes")

custom_connector_item_attributes_router = APIRouter(
    prefix="/bc/custom-connector/item-attributes",
    tags=["Custom Connector — Item Attributes"],
)

_TABLE = "itemAttributes"


def _unwrap_list(http_status: int, data: Any) -> List[Dict[str, Any]]:
    if http_status != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Business Central returned {http_status}: {data}",
        )
    return data.get("value", data)


@custom_connector_item_attributes_router.get("", summary="List Item Attributes")
def list_item_attributes(
    filter: Optional[str] = Query(None, description="OData $filter expression (e.g. number eq 'M013-0001S')"),
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    """Return item attribute records from BC (Pag51000). Unfiltered requests are cached for 5 minutes."""
    try:
        company_name = company or config.BC_COMPANY
        http_status, data = call_custom_connector_table(_TABLE, company_name=company_name, odata_filter=filter)
        return {"data": _unwrap_list(http_status, data)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing item attributes: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@custom_connector_item_attributes_router.get("/{record_id}", summary="Get Item Attributes by ID")
def get_item_attributes(
    record_id: str,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    """Fetch a single item attributes record by SystemId from BC."""
    try:
        http_status, data = custom_connector_get_record(
            table_endpoint=_TABLE,
            record_id=record_id,
            company_name=company or config.BC_COMPANY,
        )
        if http_status == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Item attributes record '{record_id}' not found",
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
        logger.error(f"Error fetching item attributes {record_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
