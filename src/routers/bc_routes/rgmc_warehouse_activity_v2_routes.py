"""RGMC custom API v2.0 — Warehouse Activity Header/Line endpoints (Pag50351 / Pag50352)."""
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException, Query, status
from src.services.bc_functions import (
    call_rgmc_v2_table,
    rgmc_v2_get_record,
    rgmc_v2_create_record,
    rgmc_v2_update_record,
    rgmc_v2_delete_record,
)
from src.models.bc_models.rgmc_warehouse_activity_models import (
    RgmcWarehouseActivityHeaderCreate,
    RgmcWarehouseActivityHeaderUpdate,
    RgmcWarehouseActivityLineCreate,
    RgmcWarehouseActivityLineUpdate,
)
from src import config

logger = logging.getLogger("bc_routes.rgmc_warehouse_activities_v2")

rgmc_warehouse_activity_v2_router = APIRouter(
    prefix="/bc/custom/v2/warehouse-activities",
    tags=["BC RGMC Warehouse Activities v2"],
)

_TABLE = "warehouseActivityHeaders"
_LINES_TABLE = "warehouseActivityLines"


def _unwrap_list(bc_result: tuple) -> List[Dict[str, Any]]:
    http_status, data = bc_result
    if http_status != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Business Central returned {http_status}: {data}",
        )
    return data.get("value", data)


def _unwrap_single(http_status: int, data: Any, label: str = "Record") -> Dict[str, Any]:
    if http_status == 404:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{label} not found")
    if http_status not in (200, 201):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Business Central returned {http_status}: {data}",
        )
    return data


@rgmc_warehouse_activity_v2_router.get("", summary="List Warehouse Activity Headers v2")
def list_warehouse_activity_headers_v2(
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
    filter: Optional[str] = Query(None, description="OData $filter expression"),
    expand: Optional[str] = Query(None, description="OData $expand (e.g. warehouseActivityLines)"),
    select: Optional[str] = Query(None, description="OData $select"),
):
    try:
        result = call_rgmc_v2_table(_TABLE, company_name=company or config.BC_COMPANY, odata_filter=filter, expand=expand, select=select)
        return {"data": _unwrap_list(result)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing warehouse activity headers v2: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_warehouse_activity_v2_router.get("/{header_id}", summary="Get Warehouse Activity Header v2 by ID")
def get_warehouse_activity_header_v2(
    header_id: str,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
    expand: Optional[str] = Query(None, description="OData $expand (e.g. warehouseActivityLines)"),
):
    try:
        http_status, data = rgmc_v2_get_record(_TABLE, header_id, company_name=company or config.BC_COMPANY)
        return _unwrap_single(http_status, data, "Warehouse activity header")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching warehouse activity header v2 {header_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_warehouse_activity_v2_router.post("", summary="Create Warehouse Activity Header v2", status_code=status.HTTP_201_CREATED)
def create_warehouse_activity_header_v2(
    body: RgmcWarehouseActivityHeaderCreate,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    try:
        payload = body.model_dump(mode="json", exclude_none=True)
        lines = payload.pop("lines", [])

        http_status, data = rgmc_v2_create_record(_TABLE, payload, company_name=company or config.BC_COMPANY)
        header = _unwrap_single(http_status, data, "Warehouse activity header")

        if lines:
            header_id = header.get("id")
            company_name = company or config.BC_COMPANY

            def _create_line(index_and_line):
                _, line = index_and_line
                lp = {k: v for k, v in line.items() if v is not None}
                for attempt in range(4):
                    lh, ld = rgmc_v2_create_record(
                        f"{_TABLE}({header_id})/{_LINES_TABLE}",
                        lp,
                        company_name=company_name,
                    )
                    if lh in (200, 201):
                        return
                    if lh == 409 and attempt < 3:
                        time.sleep(0.5 * (attempt + 1))
                        continue
                    raise ValueError(f"BC returned {lh}: {ld}")

            errors: List[tuple] = []
            with ThreadPoolExecutor(max_workers=2) as executor:
                future_to_idx = {
                    executor.submit(_create_line, (i, line)): i
                    for i, line in enumerate(lines, start=1)
                }
                for future in as_completed(future_to_idx):
                    exc = future.exception()
                    if exc:
                        errors.append((future_to_idx[future], exc))

            if errors:
                first_idx, first_err = min(errors, key=lambda x: x[0])
                logger.error(f"Failed to create {len(errors)} line(s) for warehouse activity header v2 {header_id}: {first_err}")
                try:
                    rgmc_v2_delete_record(_TABLE, header_id, company_name=company_name)
                except Exception as del_err:
                    logger.error(f"Rollback failed for warehouse activity header v2 {header_id}: {del_err}")
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"Line {first_idx} creation failed: {first_err}. Header rolled back.",
                )

        return header
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating warehouse activity header v2: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_warehouse_activity_v2_router.patch("/{header_id}", summary="Update Warehouse Activity Header v2")
def update_warehouse_activity_header_v2(
    header_id: str,
    body: RgmcWarehouseActivityHeaderUpdate,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    try:
        payload = body.model_dump(mode="json", exclude_none=True)
        if not payload:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields provided for update")
        http_status, data = rgmc_v2_update_record(_TABLE, header_id, payload, company_name=company or config.BC_COMPANY)
        return _unwrap_single(http_status, data, "Warehouse activity header")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating warehouse activity header v2 {header_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_warehouse_activity_v2_router.delete("/{header_id}", summary="Delete Warehouse Activity Header v2", status_code=status.HTTP_204_NO_CONTENT)
def delete_warehouse_activity_header_v2(
    header_id: str,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    try:
        http_status = rgmc_v2_delete_record(_TABLE, header_id, company_name=company or config.BC_COMPANY)
        if http_status == 404:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Warehouse activity header not found")
        if http_status not in (204, 200):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Business Central returned {http_status}",
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting warehouse activity header v2 {header_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_warehouse_activity_v2_router.get("/{header_id}/lines", summary="List Lines for a Warehouse Activity Header v2")
def list_warehouse_activity_lines_v2(
    header_id: str,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
    filter: Optional[str] = Query(None, description="OData $filter expression"),
    select: Optional[str] = Query(None, description="OData $select"),
):
    try:
        nested = f"{_TABLE}({header_id})/{_LINES_TABLE}"
        result = call_rgmc_v2_table(nested, company_name=company or config.BC_COMPANY, odata_filter=filter, select=select)
        return {"data": _unwrap_list(result)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing lines for warehouse activity header v2 {header_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_warehouse_activity_v2_router.get("/{header_id}/lines/{line_id}", summary="Get a Warehouse Activity Line v2 by ID")
def get_warehouse_activity_line_v2(
    header_id: str,
    line_id: str,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    try:
        nested = f"{_TABLE}({header_id})/{_LINES_TABLE}"
        http_status, data = rgmc_v2_get_record(nested, line_id, company_name=company or config.BC_COMPANY)
        return _unwrap_single(http_status, data, "Warehouse activity line")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching line v2 {line_id} for warehouse activity header {header_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_warehouse_activity_v2_router.post("/{header_id}/lines", summary="Create a Warehouse Activity Line v2", status_code=status.HTTP_201_CREATED)
def create_warehouse_activity_line_v2(
    header_id: str,
    body: RgmcWarehouseActivityLineCreate,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    try:
        nested = f"{_TABLE}({header_id})/{_LINES_TABLE}"
        payload = body.model_dump(exclude_none=True)
        http_status, data = rgmc_v2_create_record(nested, payload, company_name=company or config.BC_COMPANY)
        return _unwrap_single(http_status, data, "Warehouse activity line")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating line v2 for warehouse activity header {header_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_warehouse_activity_v2_router.patch("/{header_id}/lines/{line_id}", summary="Update a Warehouse Activity Line v2")
def update_warehouse_activity_line_v2(
    header_id: str,
    line_id: str,
    body: RgmcWarehouseActivityLineUpdate,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    try:
        payload = body.model_dump(exclude_none=True)
        if not payload:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields provided for update")
        nested = f"{_TABLE}({header_id})/{_LINES_TABLE}"
        http_status, data = rgmc_v2_update_record(nested, line_id, payload, company_name=company or config.BC_COMPANY)
        return _unwrap_single(http_status, data, "Warehouse activity line")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating line v2 {line_id} for warehouse activity header {header_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_warehouse_activity_v2_router.delete("/{header_id}/lines/{line_id}", summary="Delete a Warehouse Activity Line v2", status_code=status.HTTP_204_NO_CONTENT)
def delete_warehouse_activity_line_v2(
    header_id: str,
    line_id: str,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    try:
        nested = f"{_TABLE}({header_id})/{_LINES_TABLE}"
        http_status = rgmc_v2_delete_record(nested, line_id, company_name=company or config.BC_COMPANY)
        if http_status == 404:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Warehouse activity line not found")
        if http_status not in (204, 200):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Business Central returned {http_status}",
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting line v2 {line_id} for warehouse activity header {header_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
