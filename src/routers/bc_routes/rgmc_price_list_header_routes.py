"""RGMC custom API v2.0 — Price List Header read endpoints (Pag50320).

Endpoints:
  GET /bc/custom/v2/price-list-headers           — list headers from BC (cached 5 min)
  GET /bc/custom/v2/price-list-headers/catalog   — read from Firestore (consignment app)
  GET /bc/custom/v2/price-list-headers/{id}      — get single header by SystemId from BC

NOTE: /catalog is defined before /{id} so FastAPI matches it as a literal path,
not as an ID parameter.
"""
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, status

from src import config
from src.services.bc_functions import (
    rgmc_v2_get_price_list_header,
    rgmc_v2_list_price_list_headers,
)
from src.services.price_firestore_service import get_price_list_headers_from_firestore

logger = logging.getLogger("bc_routes.rgmc_price_list_headers")

rgmc_price_list_header_router = APIRouter(
    prefix="/bc/custom/v2/price-list-headers",
    tags=["BC RGMC Price List Headers v2"],
)


def _unwrap_list(http_status: int, data: Any) -> List[Dict[str, Any]]:
    if http_status != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Business Central returned {http_status}: {data}",
        )
    return data.get("value", data)


@rgmc_price_list_header_router.get("", summary="List Price List Headers (v2)")
def list_price_list_headers(
    filter: Optional[str] = Query(None, description="OData $filter expression (e.g. status eq 'Active')"),
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by status (Draft, Active, Inactive)"),
    item_family_code: Optional[str] = Query(None, description="Filter by itemFamilyCode (custom field, exact match)"),
    price_type: Optional[str] = Query(None, description="Filter by priceType (e.g. Sale, Purchase)"),
    expand: Optional[str] = Query(None, description="OData $expand (e.g. priceListLines)"),
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    """Return all price list headers from BC (Pag50320).

    Includes the custom `itemFamilyCode` field (TableExt 50455). Unfiltered requests
    are cached for 5 minutes. Pass `expand=priceListLines` to include line items.
    """
    try:
        company_name = company or config.BC_COMPANY
        odata_parts = []
        if filter:
            odata_parts.append(filter)
        if status_filter:
            odata_parts.append(f"status eq '{status_filter}'")
        if item_family_code:
            odata_parts.append(f"itemFamilyCode eq '{item_family_code}'")
        if price_type:
            odata_parts.append(f"priceType eq '{price_type}'")
        odata_filter = " and ".join(odata_parts) if odata_parts else None

        http_status, data = rgmc_v2_list_price_list_headers(
            company_name=company_name,
            odata_filter=odata_filter,
            expand=expand,
        )
        return {"data": _unwrap_list(http_status, data)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing price list headers (v2): {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_price_list_header_router.get("/catalog", summary="Get Price List Headers from Firestore")
def get_price_list_header_catalog(
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by status (Draft, Active, Inactive)"),
    item_family_code: Optional[str] = Query(None, description="Filter by itemFamilyCode (exact match)"),
    price_type: Optional[str] = Query(None, description="Filter by priceType (exact match)"),
):
    """Return price list headers from the Firestore catalog for the current GCP_ENV.

    Reads pre-synced data — does **not** call Business Central. Use
    `POST /internal/firestore/sync-price-list-headers` to populate or refresh.
    Intended for the consignment app.
    """
    try:
        company_name = company or config.BC_COMPANY
        records = get_price_list_headers_from_firestore(
            company=company_name,
            status=status_filter,
            item_family_code=item_family_code,
            price_type=price_type,
        )
        return {
            "data": records,
            "total": len(records),
            "company": company_name,
            "env": config.GCP_ENV,
        }
    except Exception as e:
        logger.error(f"Error reading price list headers from Firestore: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_price_list_header_router.get("/{header_id}", summary="Get Price List Header by ID (v2)")
def get_price_list_header(
    header_id: str,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    """Fetch a single price list header by SystemId from BC."""
    try:
        http_status, data = rgmc_v2_get_price_list_header(
            header_id=header_id,
            company_name=company or config.BC_COMPANY,
        )
        if http_status == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Price list header '{header_id}' not found",
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
        logger.error(f"Error fetching price list header {header_id} (v2): {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
