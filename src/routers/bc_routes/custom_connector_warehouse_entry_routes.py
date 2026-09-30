"""Custom Connector API — Warehouse Entry read endpoints (Pag51004).

Same separate AL app as custom_connector_item_attributes_routes.py (github.com/Aaron-Alvarez-RGMC/
custom-connector-AL), own APIPublisher/APIGroup/object ID range - not part of Erwin's rgmc/
rgmccustom namespace.

Endpoints:
  GET /bc/custom-connector/warehouse-entries         — list
  GET /bc/custom-connector/warehouse-entries/{id}    — get single record by SystemId

Bin-level movement ledger (~56k rows on CGI as of 2026-09-29). entryNo is the natural key. Source
table is real (not a temp table like Pag50339), so SystemId is stable across syncs.

Transfer tracing: a Pick (referenceDocument 'Pick', sourceNo = transfer order, sourceLineNo) takes
stock from the source bin and places it in DISPATCH; the Posted T. Shipment entry then takes it out
of DISPATCH with referenceNo = posted shipment no. Enum fields (entryType, sourceDocument,
referenceDocument, whseDocumentType) arrive OData-escaped (e.g. 'Outb_x002E__x0020_Transfer') and
are decoded in dbt staging.
"""
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, status

from src import config
from src.services.bc_functions import (
    call_custom_connector_table,
    custom_connector_get_record,
)

logger = logging.getLogger("bc_routes.custom_connector_warehouse_entry")

custom_connector_warehouse_entry_router = APIRouter(
    prefix="/bc/custom-connector/warehouse-entries",
    tags=["Custom Connector — Warehouse Entry"],
)

_TABLE = "warehouseEntries"


def _unwrap_list(http_status: int, data: Any) -> List[Dict[str, Any]]:
    if http_status != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Business Central returned {http_status}: {data}",
        )
    return data.get("value", data)


@custom_connector_warehouse_entry_router.get("", summary="List Warehouse Entries")
def list_warehouse_entries(
    filter: Optional[str] = Query(None, description="OData $filter expression (e.g. locationCode eq 'WHC001PCWH')"),
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
    modified_from: Optional[str] = Query(
        None,
        description="Only records with lastModifiedDateTime on/after this ISO 8601 datetime (e.g. from Airbyte's incremental cursor). Combined with `filter` via AND if both are given.",
    ),
    limit: Optional[int] = Query(None, ge=1, le=5000, description="Max records to return in this page (BC $top). Omit for all matching records."),
    offset: Optional[int] = Query(None, ge=0, description="Records to skip before this page (BC $skip)."),
):
    """Return Warehouse Entry records from BC (Pag51004)."""
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
        logger.error(f"Error listing warehouse entries: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@custom_connector_warehouse_entry_router.get("/{record_id}", summary="Get Warehouse Entry by ID")
def get_warehouse_entry(
    record_id: str,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    """Fetch a single Warehouse Entry record by SystemId from BC."""
    try:
        http_status, data = custom_connector_get_record(
            table_endpoint=_TABLE,
            record_id=record_id,
            company_name=company or config.BC_COMPANY,
        )
        if http_status == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Warehouse Entry record '{record_id}' not found",
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
        logger.error(f"Error fetching warehouse entry {record_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
