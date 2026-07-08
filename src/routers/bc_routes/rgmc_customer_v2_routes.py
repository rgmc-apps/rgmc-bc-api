"""RGMC custom API v2.0 — Customer CRUD endpoints."""
import logging
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Body, HTTPException, Query, status
from src.services.bc_functions import (
    rgmc_v2_list_customers,
    rgmc_v2_get_customer,
    rgmc_v2_create_customer,
    rgmc_v2_update_customer,
    rgmc_v2_delete_customer,
)
from src.models.bc_models import RgmcCustomerV2Response, RgmcCustomerV2Create, RgmcCustomerV2Update
from src import config

logger = logging.getLogger("bc_routes.rgmc_customers_v2")

rgmc_customer_v2_router = APIRouter(
    prefix="/bc/custom/v2/customers",
    tags=["BC RGMC Customers v2"],
)


def _unwrap_list(http_status: int, data: Any) -> List[Dict[str, Any]]:
    if http_status != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Business Central returned {http_status}: {data}",
        )
    return data.get("value", data)


def _unwrap_single(http_status: int, data: Any, customer_id: str = "") -> Dict[str, Any]:
    if http_status == 404:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Customer '{customer_id}' not found")
    if http_status not in (200, 201):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Business Central returned {http_status}: {data}",
        )
    return data


@rgmc_customer_v2_router.get("", summary="List Customers (v2)")
def list_customers(
    filter: Optional[str] = Query(None, description="OData $filter expression"),
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    try:
        http_status, data = rgmc_v2_list_customers(
            company_name=company or config.BC_COMPANY,
            odata_filter=filter,
        )
        return {"data": _unwrap_list(http_status, data)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing customers (v2): {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_customer_v2_router.get(
    "/{customer_id}",
    summary="Get Customer by ID (v2)",
    response_model=RgmcCustomerV2Response,
)
def get_customer(
    customer_id: str,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    try:
        http_status, data = rgmc_v2_get_customer(
            customer_id=customer_id,
            company_name=company or config.BC_COMPANY,
        )
        return _unwrap_single(http_status, data, customer_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching customer {customer_id} (v2): {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_customer_v2_router.post(
    "",
    summary="Create Customer (v2)",
    status_code=status.HTTP_201_CREATED,
    response_model=RgmcCustomerV2Response,
)
def create_customer(
    body: RgmcCustomerV2Create = Body(...),
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    try:
        http_status, data = rgmc_v2_create_customer(
            payload=body.model_dump(exclude_none=True),
            company_name=company or config.BC_COMPANY,
        )
        if http_status not in (200, 201):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Business Central returned {http_status}: {data}",
            )
        return data
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating customer (v2): {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_customer_v2_router.patch(
    "/{customer_id}",
    summary="Update Customer (v2)",
    response_model=RgmcCustomerV2Response,
)
def update_customer(
    customer_id: str,
    body: RgmcCustomerV2Update = Body(...),
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    try:
        payload = body.model_dump(exclude_none=True)
        if not payload:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No fields provided for update",
            )
        http_status, data = rgmc_v2_update_customer(
            customer_id=customer_id,
            payload=payload,
            company_name=company or config.BC_COMPANY,
        )
        return _unwrap_single(http_status, data, customer_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating customer {customer_id} (v2): {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_customer_v2_router.delete(
    "/{customer_id}",
    summary="Delete Customer (v2)",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_customer(
    customer_id: str,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    try:
        http_status = rgmc_v2_delete_customer(
            customer_id=customer_id,
            company_name=company or config.BC_COMPANY,
        )
        if http_status == 404:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Customer '{customer_id}' not found")
        if http_status not in (200, 204):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Business Central returned {http_status}",
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting customer {customer_id} (v2): {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
