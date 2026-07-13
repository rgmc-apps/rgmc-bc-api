"""RGMC custom API v2.0 — Company Settings endpoints (Pag50492)."""
import logging
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Body, HTTPException, Query, status
from src.services.bc_functions import (
    rgmc_v2_list_company_settings,
    rgmc_v2_get_company_setting,
    rgmc_v2_update_company_setting,
    rgmc_v2_warmup_company_settings,
    rgmc_v2_invalidate_company_settings_cache,
)
from src.models.bc_models import RgmcCompanySettingResponse, RgmcCompanySettingUpdate
from src import config

logger = logging.getLogger("bc_routes.rgmc_company_settings_v2")

rgmc_company_v2_router = APIRouter(
    prefix="/bc/custom/v2/company-settings",
    tags=["BC RGMC Company Settings v2"],
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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company setting not found")
    if http_status not in (200, 201):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Business Central returned {http_status}: {data}",
        )
    return data


@rgmc_company_v2_router.post("/refresh", summary="Refresh Company Settings Cache (v2)", status_code=status.HTTP_202_ACCEPTED)
def refresh_cache(
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    """Invalidate the in-process company settings cache and trigger a background refresh.
    Returns 202 immediately — the refresh runs asynchronously."""
    company_name = company or config.BC_COMPANY
    rgmc_v2_invalidate_company_settings_cache()
    rgmc_v2_warmup_company_settings(company_name)
    return {"status": "refresh triggered", "company": company_name}


@rgmc_company_v2_router.get("", summary="List Company Settings (v2)")
def list_company_settings(
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
    filter: Optional[str] = Query(None, description="OData $filter expression"),
):
    try:
        http_status, data = rgmc_v2_list_company_settings(
            company_name=company or config.BC_COMPANY,
            odata_filter=filter,
        )
        return {"data": _unwrap_list(http_status, data)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing company settings (v2): {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_company_v2_router.get(
    "/{setting_id}",
    summary="Get Company Setting by ID (v2)",
    response_model=RgmcCompanySettingResponse,
)
def get_company_setting(
    setting_id: str,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    try:
        http_status, data = rgmc_v2_get_company_setting(
            setting_id=setting_id,
            company_name=company or config.BC_COMPANY,
        )
        return _unwrap_single(http_status, data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching company setting {setting_id} (v2): {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_company_v2_router.patch(
    "/{setting_id}",
    summary="Update Company Setting (v2)",
    response_model=RgmcCompanySettingResponse,
)
def update_company_setting(
    setting_id: str,
    body: RgmcCompanySettingUpdate = Body(...),
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    """Toggle consignmentAppVisible. Insert and Delete are not allowed by Pag50492."""
    try:
        payload = body.model_dump(exclude_none=True)
        if not payload:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No fields provided for update",
            )
        http_status, data = rgmc_v2_update_company_setting(
            setting_id=setting_id,
            payload=payload,
            company_name=company or config.BC_COMPANY,
        )
        return _unwrap_single(http_status, data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating company setting {setting_id} (v2): {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
