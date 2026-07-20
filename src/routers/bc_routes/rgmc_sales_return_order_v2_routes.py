"""RGMC custom API v2.0 — Sales Return Order endpoints (Pag50313 / Pag50314)."""
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
from src.models.bc_models.sales_return_order_models import (
    SalesReturnOrderCreate,
    SalesReturnOrderUpdate,
    SalesReturnOrderLineCreate,
    SalesReturnOrderLineUpdate,
)
from src.services.task_service import enqueue_order
from src import config

logger = logging.getLogger("bc_routes.rgmc_sales_return_orders_v2")

rgmc_sales_return_order_v2_router = APIRouter(prefix="/bc/custom/v2/sales-return-orders", tags=["BC RGMC Sales Return Orders v2"])

_TABLE = "salesReturnOrders"
_LINES_TABLE = "salesReturnOrderLines"


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


@rgmc_sales_return_order_v2_router.get("", summary="List Sales Return Orders v2")
def list_sales_return_orders_v2(
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
    filter: Optional[str] = Query(None, description="OData $filter expression"),
    expand: Optional[str] = Query(None, description="OData $expand (e.g. salesReturnOrderLines)"),
    select: Optional[str] = Query(None, description="OData $select"),
):
    try:
        result = call_rgmc_v2_table(_TABLE, company_name=company or config.BC_COMPANY, odata_filter=filter, expand=expand, select=select)
        return {"data": _unwrap_list(result)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing sales return orders v2: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_sales_return_order_v2_router.get("/{order_id}", summary="Get Sales Return Order v2 by ID")
def get_sales_return_order_v2(
    order_id: str,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
    expand: Optional[str] = Query(None, description="OData $expand (e.g. salesReturnOrderLines)"),
):
    try:
        http_status, data = rgmc_v2_get_record(_TABLE, order_id, company_name=company or config.BC_COMPANY)
        return _unwrap_single(http_status, data, "Sales return order")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching sales return order v2 {order_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


def _map_line_payload(line: dict) -> dict:
    mapped: dict = {"lineType": "Item"}
    if "itemNumber" in line:
        mapped["number"] = line["itemNumber"]
    if "description" in line:
        mapped["description"] = line["description"]
    if "quantity" in line:
        mapped["quantity"] = line["quantity"]
    if "unitPrice" in line:
        mapped["unitPrice"] = line["unitPrice"]
    if "discountPercent" in line:
        mapped["lineDiscountPercent"] = line["discountPercent"]
    elif "lineDiscountAmount" in line:
        qty = line.get("quantity") or 1
        unit_price = line.get("unitPrice") or 0
        base = unit_price * qty
        if base > 0:
            mapped["lineDiscountPercent"] = round((line["lineDiscountAmount"] / base) * 100, 5)
    return mapped


@rgmc_sales_return_order_v2_router.post("/submit", summary="Submit Sales Return Order (async via Cloud Tasks)", status_code=status.HTTP_202_ACCEPTED)
def submit_sales_return_order_async(
    body: SalesReturnOrderCreate,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    payload = body.model_dump(mode="json", exclude_none=True)
    if "customerNumber" in payload:
        payload["sellToCustomerNo"] = payload.pop("customerNumber")
    lines = payload.pop("lines", [])
    mapped_lines = []
    for i, line in enumerate(lines, start=1):
        lp = _map_line_payload(line)
        lp["lineNo"] = i * 10000
        mapped_lines.append(lp)
    task_id = enqueue_order("returns", "v2", payload, mapped_lines, company or config.BC_COMPANY)
    return {"taskId": task_id, "status": "queued"}


@rgmc_sales_return_order_v2_router.post("", summary="Create Sales Return Order v2", status_code=status.HTTP_201_CREATED)
def create_sales_return_order_v2(
    body: SalesReturnOrderCreate,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    try:
        payload = body.model_dump(mode='json', exclude_none=True)

        if 'customerNumber' in payload:
            payload['sellToCustomerNo'] = payload.pop('customerNumber')

        lines = payload.pop('lines', [])

        http_status, data = rgmc_v2_create_record(_TABLE, payload, company_name=company or config.BC_COMPANY)
        order = _unwrap_single(http_status, data, "Sales return order")

        if lines:
            order_id = order.get('id')
            company_name = company or config.BC_COMPANY

            def _create_line(index_and_line):
                i, line = index_and_line
                lp = _map_line_payload(line)
                lp["lineNo"] = i * 10000
                for attempt in range(4):
                    lh, ld = rgmc_v2_create_record(
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
                logger.error(f"Failed to create {len(errors)} line(s) for return order v2 {order_id}: {first_err}")
                try:
                    rgmc_v2_delete_record(_TABLE, order_id, company_name=company_name)
                except Exception as del_err:
                    logger.error(f"Rollback failed for return order v2 {order_id}: {del_err}")
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"Line {first_idx} creation failed: {first_err}. Order rolled back.",
                )

        return order
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating sales return order v2: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_sales_return_order_v2_router.patch("/{order_id}", summary="Update Sales Return Order v2")
def update_sales_return_order_v2(
    order_id: str,
    body: SalesReturnOrderUpdate,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    try:
        payload = body.model_dump(mode='json', exclude_none=True)
        if not payload:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields provided for update")
        http_status, data = rgmc_v2_update_record(_TABLE, order_id, payload, company_name=company or config.BC_COMPANY)
        return _unwrap_single(http_status, data, "Sales return order")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating sales return order v2 {order_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_sales_return_order_v2_router.delete("/{order_id}", summary="Delete Sales Return Order v2", status_code=status.HTTP_204_NO_CONTENT)
def delete_sales_return_order_v2(
    order_id: str,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    try:
        http_status = rgmc_v2_delete_record(_TABLE, order_id, company_name=company or config.BC_COMPANY)
        if http_status == 404:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sales return order not found")
        if http_status not in (204, 200):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Business Central returned {http_status}",
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting sales return order v2 {order_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_sales_return_order_v2_router.get("/{order_id}/lines", summary="List Lines for a Sales Return Order v2")
def list_sales_return_order_lines_v2(
    order_id: str,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
    filter: Optional[str] = Query(None, description="OData $filter expression"),
    select: Optional[str] = Query(None, description="OData $select"),
):
    try:
        nested = f"{_TABLE}({order_id})/{_LINES_TABLE}"
        result = call_rgmc_v2_table(nested, company_name=company or config.BC_COMPANY, odata_filter=filter, select=select)
        return {"data": _unwrap_list(result)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing lines for sales return order v2 {order_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_sales_return_order_v2_router.get("/{order_id}/lines/{line_id}", summary="Get a Sales Return Order Line v2 by ID")
def get_sales_return_order_line_v2(
    order_id: str,
    line_id: str,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    try:
        nested = f"{_TABLE}({order_id})/{_LINES_TABLE}"
        http_status, data = rgmc_v2_get_record(nested, line_id, company_name=company or config.BC_COMPANY)
        return _unwrap_single(http_status, data, "Sales return order line")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching line v2 {line_id} for sales return order {order_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_sales_return_order_v2_router.post("/{order_id}/lines", summary="Create a Sales Return Order Line v2", status_code=status.HTTP_201_CREATED)
def create_sales_return_order_line_v2(
    order_id: str,
    body: SalesReturnOrderLineCreate,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    try:
        nested = f"{_TABLE}({order_id})/{_LINES_TABLE}"
        payload = body.model_dump(exclude_none=True)
        http_status, data = rgmc_v2_create_record(nested, payload, company_name=company or config.BC_COMPANY)
        return _unwrap_single(http_status, data, "Sales return order line")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating line v2 for sales return order {order_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_sales_return_order_v2_router.patch("/{order_id}/lines/{line_id}", summary="Update a Sales Return Order Line v2")
def update_sales_return_order_line_v2(
    order_id: str,
    line_id: str,
    body: SalesReturnOrderLineUpdate,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    try:
        payload = body.model_dump(exclude_none=True)
        if not payload:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields provided for update")
        nested = f"{_TABLE}({order_id})/{_LINES_TABLE}"
        http_status, data = rgmc_v2_update_record(nested, line_id, payload, company_name=company or config.BC_COMPANY)
        return _unwrap_single(http_status, data, "Sales return order line")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating line v2 {line_id} for sales return order {order_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_sales_return_order_v2_router.delete("/{order_id}/lines/{line_id}", summary="Delete a Sales Return Order Line v2", status_code=status.HTTP_204_NO_CONTENT)
def delete_sales_return_order_line_v2(
    order_id: str,
    line_id: str,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    try:
        nested = f"{_TABLE}({order_id})/{_LINES_TABLE}"
        http_status = rgmc_v2_delete_record(nested, line_id, company_name=company or config.BC_COMPANY)
        if http_status == 404:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sales return order line not found")
        if http_status not in (204, 200):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Business Central returned {http_status}",
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting line v2 {line_id} for sales return order {order_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
