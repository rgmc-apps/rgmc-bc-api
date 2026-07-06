"""RGMC custom API v2.0 — Company endpoints."""
import logging
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Body, HTTPException, Query, status
from src.services.bc_functions import (
    rgmc_v2_list_companies,
    rgmc_v2_get_company,
    rgmc_v2_update_company,
)
from src.models.bc_models import RgmcCompanyV2Response, RgmcCompanyV2Update

logger = logging.getLogger("bc_routes.rgmc_companies_v2")

rgmc_company_v2_router = APIRouter(
    prefix="/bc/custom/v2/companies",
    tags=["BC RGMC Companies v2"],
)


def _unwrap_list(http_status: int, data: Any) -> List[Dict[str, Any]]:
    if http_status != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Business Central returned {http_status}: {data}",
        )
    return data.get("value", data)


def _unwrap_single(http_status: int, data: Any) -> Dict[str, Any]:
    if http_status == 404:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")
    if http_status not in (200, 201):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Business Central returned {http_status}: {data}",
        )
    return data


@rgmc_company_v2_router.get("", summary="List RGMC Companies (v2)")
def list_rgmc_companies(
    filter: Optional[str] = Query(None, description="OData $filter expression"),
):
    try:
        http_status, data = rgmc_v2_list_companies(odata_filter=filter)
        return {"data": _unwrap_list(http_status, data)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing RGMC companies (v2): {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_company_v2_router.get(
    "/{company_id}",
    summary="Get RGMC Company by ID (v2)",
    response_model=RgmcCompanyV2Response,
)
def get_rgmc_company(company_id: str):
    try:
        http_status, data = rgmc_v2_get_company(company_id)
        return _unwrap_single(http_status, data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching RGMC company {company_id} (v2): {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_company_v2_router.patch(
    "/{company_id}",
    summary="Update RGMC Company (v2)",
    response_model=RgmcCompanyV2Response,
)
def update_rgmc_company(
    company_id: str,
    body: RgmcCompanyV2Update = Body(...),
):
    """Toggle consignmentAppVisible or update other company-level fields in the RGMC v2 API."""
    try:
        payload = body.model_dump(exclude_none=True)
        if not payload:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No fields provided for update",
            )
        http_status, data = rgmc_v2_update_company(company_id, payload)
        return _unwrap_single(http_status, data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating RGMC company {company_id} (v2): {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
