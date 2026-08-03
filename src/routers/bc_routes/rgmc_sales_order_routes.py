"""RGMC custom API — Sales Order endpoints (Pag50216 / Pag50217)."""
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException, Query, status
from src.services.bc_functions import (
    call_rgmc_table,
    rgmc_get_record,
    rgmc_create_record,
    rgmc_update_record,
    rgmc_delete_record,
)
from src.models.bc_models.rgmc_sales_order_models import (
    RgmcSalesOrderCreate,
    RgmcSalesOrderUpdate,
    RgmcSalesOrderLineCreate,
    RgmcSalesOrderLineUpdate,
)
from src.services.task_service import enqueue_order
from src import config

logger = logging.getLogger("bc_routes.rgmc_sales_orders")

rgmc_sales_order_router = APIRouter(prefix="/bc/custom/sales-orders", tags=["BC RGMC Sales Orders"])

_TABLE = "salesOrders"
_LINES_TABLE = "salesOrderLines"


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


@rgmc_sales_order_router.get("", summary="List RGMC Sales Orders")
def list_rgmc_sales_orders(
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
    filter: Optional[str] = Query(None, description="OData $filter expression"),
    expand: Optional[str] = Query(None, description="OData $expand (e.g. salesOrderLines)"),
    select: Optional[str] = Query(None, description="OData $select"),
):
    try:
        result = call_rgmc_table(_TABLE, company_name=company or config.BC_COMPANY, odata_filter=filter, expand=expand, select=select)
        return {"data": _unwrap_list(result)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing RGMC sales orders: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_sales_order_router.get("/{order_id}", summary="Get RGMC Sales Order by ID")
def get_rgmc_sales_order(
    order_id: str,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
    expand: Optional[str] = Query(None, description="OData $expand (e.g. salesOrderLines)"),
):
    try:
        http_status, data = rgmc_get_record(_TABLE, order_id, company_name=company or config.BC_COMPANY)
        return _unwrap_single(http_status, data, "Sales order")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching RGMC sales order {order_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


def _map_line_payload(line: dict) -> dict:
    mapped: dict = {"lineType": line.get("lineType", "Item")}
    for field in ("number", "description", "description2", "unitOfMeasureCode",
                  "quantity", "unitPrice", "lineDiscountPercent", "lineAmount",
                  "shipmentDate", "locationCode", "shortcutDimension1Code", "shortcutDimension2Code"):
        if field in line:
            mapped[field] = line[field]
    return mapped


@rgmc_sales_order_router.post("/submit", summary="Submit RGMC Sales Order (async via Cloud Tasks)", status_code=status.HTTP_202_ACCEPTED)
def submit_rgmc_sales_order_async(
    body: RgmcSalesOrderCreate,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    payload = body.model_dump(mode="json", exclude_none=True)
    lines = payload.pop("lines", [])
    mapped_lines = []
    for i, line in enumerate(lines, start=1):
        lp = _map_line_payload(line)
        lp["lineNo"] = i * 10000
        mapped_lines.append(lp)
    task_id = enqueue_order("sales", "v1", payload, mapped_lines, company or config.BC_COMPANY)
    return {"taskId": task_id, "status": "queued"}


@rgmc_sales_order_router.post("", summary="Create RGMC Sales Order", status_code=status.HTTP_201_CREATED)
def create_rgmc_sales_order(
    body: RgmcSalesOrderCreate,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    try:
        payload = body.model_dump(mode="json", exclude_none=True)
        lines = payload.pop("lines", [])

        http_status, data = rgmc_create_record(_TABLE, payload, company_name=company or config.BC_COMPANY)
        order = _unwrap_single(http_status, data, "Sales order")

        if lines:
            order_id = order.get("id")
            company_name = company or config.BC_COMPANY

            def _create_line(index_and_line):
                i, line = index_and_line
                lp = _map_line_payload(line)
                lp["lineNo"] = i * 10000
                for attempt in range(4):
                    lh, ld = rgmc_create_record(
                        f"{_TABLE}({order_id})/{_LINES_TABLE}",
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
                logger.error(f"Failed to create {len(errors)} line(s) for RGMC sales order {order_id}: {first_err}")
                try:
                    rgmc_delete_record(_TABLE, order_id, company_name=company_name)
                except Exception as del_err:
                    logger.error(f"Rollback failed for RGMC sales order {order_id}: {del_err}")
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"Line {first_idx} creation failed: {first_err}. Order rolled back.",
                )

        return order
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating RGMC sales order: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_sales_order_router.patch("/{order_id}", summary="Update RGMC Sales Order")
def update_rgmc_sales_order(
    order_id: str,
    body: RgmcSalesOrderUpdate,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    try:
        payload = body.model_dump(mode="json", exclude_none=True)
        if not payload:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields provided for update")
        http_status, data = rgmc_update_record(_TABLE, order_id, payload, company_name=company or config.BC_COMPANY)
        return _unwrap_single(http_status, data, "Sales order")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating RGMC sales order {order_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_sales_order_router.delete("/{order_id}", summary="Delete RGMC Sales Order", status_code=status.HTTP_204_NO_CONTENT)
def delete_rgmc_sales_order(
    order_id: str,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    try:
        http_status = rgmc_delete_record(_TABLE, order_id, company_name=company or config.BC_COMPANY)
        if http_status == 404:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sales order not found")
        if http_status not in (204, 200):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Business Central returned {http_status}",
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting RGMC sales order {order_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_sales_order_router.get("/{order_id}/lines", summary="List Lines for a RGMC Sales Order")
def list_rgmc_sales_order_lines(
    order_id: str,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
    filter: Optional[str] = Query(None, description="OData $filter expression"),
    select: Optional[str] = Query(None, description="OData $select"),
):
    try:
        nested = f"{_TABLE}({order_id})/{_LINES_TABLE}"
        result = call_rgmc_table(nested, company_name=company or config.BC_COMPANY, odata_filter=filter, select=select)
        return {"data": _unwrap_list(result)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing lines for RGMC sales order {order_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_sales_order_router.get("/{order_id}/lines/{line_id}", summary="Get a RGMC Sales Order Line by ID")
def get_rgmc_sales_order_line(
    order_id: str,
    line_id: str,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    try:
        nested = f"{_TABLE}({order_id})/{_LINES_TABLE}"
        http_status, data = rgmc_get_record(nested, line_id, company_name=company or config.BC_COMPANY)
        return _unwrap_single(http_status, data, "Sales order line")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching line {line_id} for RGMC sales order {order_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_sales_order_router.post("/{order_id}/lines", summary="Create a RGMC Sales Order Line", status_code=status.HTTP_201_CREATED)
def create_rgmc_sales_order_line(
    order_id: str,
    body: RgmcSalesOrderLineCreate,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    try:
        nested = f"{_TABLE}({order_id})/{_LINES_TABLE}"
        payload = body.model_dump(exclude_none=True)
        http_status, data = rgmc_create_record(nested, payload, company_name=company or config.BC_COMPANY)
        return _unwrap_single(http_status, data, "Sales order line")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating line for RGMC sales order {order_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_sales_order_router.patch("/{order_id}/lines/{line_id}", summary="Update a RGMC Sales Order Line")
def update_rgmc_sales_order_line(
    order_id: str,
    line_id: str,
    body: RgmcSalesOrderLineUpdate,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    try:
        payload = body.model_dump(exclude_none=True)
        if not payload:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields provided for update")
        nested = f"{_TABLE}({order_id})/{_LINES_TABLE}"
        http_status, data = rgmc_update_record(nested, line_id, payload, company_name=company or config.BC_COMPANY)
        return _unwrap_single(http_status, data, "Sales order line")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating line {line_id} for RGMC sales order {order_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_sales_order_router.delete("/{order_id}/lines/{line_id}", summary="Delete a RGMC Sales Order Line", status_code=status.HTTP_204_NO_CONTENT)
def delete_rgmc_sales_order_line(
    order_id: str,
    line_id: str,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    try:
        nested = f"{_TABLE}({order_id})/{_LINES_TABLE}"
        http_status = rgmc_delete_record(nested, line_id, company_name=company or config.BC_COMPANY)
        if http_status == 404:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sales order line not found")
        if http_status not in (204, 200):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Business Central returned {http_status}",
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting line {line_id} for RGMC sales order {order_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
