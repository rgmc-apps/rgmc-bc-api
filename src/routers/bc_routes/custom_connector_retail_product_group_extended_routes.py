"""Custom Connector API — Retail Product Group (extended fields) read endpoints (Pag51008).

Same separate AL app as custom_connector_item_attributes_routes.py (github.com/Aaron-Alvarez-RGMC/
custom-connector-AL), own APIPublisher/APIGroup/object ID range - not part of the rgmc/
rgmccustom namespace.

Endpoints:
  GET /bc/custom-connector/retail-product-groups-extended         — list
  GET /bc/custom-connector/retail-product-groups-extended/{id}    — get single record by SystemId

Fields: id, code, description, itemCategoryCode, buyerId, buyerGroupCode, divisionCode, barcodeMask,
useEanStandardBarc, outboundCode, allocationRuleCode, posMenuLink, posInventoryLookup, suggestedQtyOnPos,
dispensePrinterGroup, disableDispensePrinting, shelfLabelDescription, profitGoalPercent,
defaultProfitPercent, notDiscountable, defaultBaseUom, qtyNotInDecimal, itemTemplCode,
itemErrorCheckCode, variantFrameworkCode, minLocProfInventory, replenDataProfile,
replenTransferRuleCode, defItemDistrType, defItemDistrCode, lastDateModified, companyName,
lastModifiedDateTime.
id is the same SystemId as the shared /retail-product-groups route (Pag50335), which publishes only
code, description and itemCategoryCode of the same LSC Retail Product Group table; code is unique per company.
defItemDistrType is an Option, so its value can carry OData _xHHHH_ escapes.
"""
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, status

from src import config
from src.services.bc_functions import (
    call_custom_connector_table,
    custom_connector_get_record,
)

logger = logging.getLogger("bc_routes.custom_connector_retail_product_group_extended")

custom_connector_retail_product_group_extended_router = APIRouter(
    prefix="/bc/custom-connector/retail-product-groups-extended",
    tags=["Custom Connector — Retail Product Group Extended"],
)

_TABLE = "retailProductGroupsExtended"


def _unwrap_list(http_status: int, data: Any) -> List[Dict[str, Any]]:
    if http_status != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Business Central returned {http_status}: {data}",
        )
    return data.get("value", data)


@custom_connector_retail_product_group_extended_router.get("", summary="List Retail Product Groups (extended fields)")
def list_retail_product_groups_extended(
    filter: Optional[str] = Query(None, description="OData $filter expression (e.g. itemCategoryCode eq 'XYZ')"),
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
    modified_from: Optional[str] = Query(
        None,
        description="Only records with lastModifiedDateTime on/after this ISO 8601 datetime (e.g. from Airbyte's incremental cursor). Combined with `filter` via AND if both are given.",
    ),
    limit: Optional[int] = Query(None, ge=1, le=5000, description="Max records to return in this page (BC $top). Omit for all matching records."),
    offset: Optional[int] = Query(None, ge=0, description="Records to skip before this page (BC $skip)."),
):
    """Return Retail Product Group records with the extended fields from BC (Pag51008)."""
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
        logger.error(f"Error listing retail product groups (extended): {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@custom_connector_retail_product_group_extended_router.get("/{record_id}", summary="Get Retail Product Group (extended) by ID")
def get_retail_product_group_extended(
    record_id: str,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    """Fetch a single Retail Product Group (extended) record by SystemId from BC."""
    try:
        http_status, data = custom_connector_get_record(
            table_endpoint=_TABLE,
            record_id=record_id,
            company_name=company or config.BC_COMPANY,
        )
        if http_status == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Retail Product Group record '{record_id}' not found",
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
        logger.error(f"Error fetching retail product group {record_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
