"""RGMC custom API v3.0 — Item Price read endpoints (Pag50318) and count endpoint (Pag50319).

All list and count requests are served exclusively from Firestore (item_prices_{env}).
Run POST /internal/firestore/routine-sync to populate or refresh the catalog.

Single-record lookup by SystemId (/bc/custom/v3/item-prices/{id}) still reads from BC
because Firestore is keyed by company+productNo, not SystemId.
"""
import datetime
import logging
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from src.services.bc_functions import (
    rgmc_v3_get_item_price,
    rgmc_v3_warmup,
    rgmc_v3_invalidate_cache,
)
from src.services.price_firestore_service import (
    get_prices_from_firestore,
    get_active_price_list_codes_for_date,
    get_price_overrides_from_price_list_items,
)
from src import config

logger = logging.getLogger("bc_routes.rgmc_item_prices_v3")


class ItemPricePage(BaseModel):
    data: List[Dict[str, Any]]
    total: int
    skip: int
    limit: int


rgmc_item_price_v3_router = APIRouter(
    prefix="/bc/custom/v3/item-prices",
    tags=["BC RGMC Item Prices v3"],
)


def _unwrap(http_status: int, data: Any) -> List[Dict[str, Any]]:
    if http_status != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Business Central returned {http_status}: {data}",
        )
    return data.get("value", data)


@rgmc_item_price_v3_router.get("", summary="List Item Prices (v3)")
def list_item_prices(
    product_no: Optional[str] = Query(None, description="Filter by a single item No. (productNo)"),
    product_nos: Optional[str] = Query(None, description="Comma-separated list of item numbers to filter"),
    family_code: Optional[str] = Query(None, description="Filter by familyCode (exact match, applied in Python)."),
    price_list_code: Optional[str] = Query(None, description="Filter by priceListCode (exact match, applied in Python)."),
    on_date: Optional[str] = Query(None, description="Price-effective date (YYYY-MM-DD). When provided, only price lists active on this date are returned (via price_list_headers lookup). Defaults to today when omitted."),
    filter: Optional[str] = Query(None, description="OData $filter — not supported when reading from Firestore."),
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
    skip: int = Query(0, ge=0, description="Records to skip after fetching (Python-level)"),
    limit: int = Query(0, ge=0, description="Max records to return; 0 = all (Python-level)"),
    bc_limit: Optional[int] = Query(None, ge=0, description="Alias for limit (kept for backwards compatibility)."),
    bc_offset: Optional[int] = Query(None, ge=0, description="Alias for skip (kept for backwards compatibility)."),
):
    """Return item prices from the Firestore catalog (item_prices_{env}).

    All filtering (family_code, product_no, product_nos, price_list_code) is applied in
    Python after a single company-scoped Firestore query. OData $filter is not supported.

    Returns 503 when the catalog has not been synced yet — run
    POST /internal/firestore/routine-sync to populate it.
    """
    if filter:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OData $filter is not supported — the catalog is served from Firestore. "
                   "Use the query params (family_code, product_no, price_list_code) instead.",
        )

    try:
        nos_list = [n.strip() for n in product_nos.split(",") if n.strip()] if product_nos else None
        company_name = company or config.BC_COMPANY
        effective_date = on_date or datetime.date.today().isoformat()

        py_skip = bc_offset if bc_offset is not None else skip
        py_limit = bc_limit if bc_limit is not None else limit
        using_bc_params = bc_limit is not None or bc_offset is not None

        # Step 1: resolve which price lists are active on effective_date.
        # An empty result means headers haven't been synced yet — we still serve
        # item_prices data without a price override in that case.
        active_codes = get_active_price_list_codes_for_date(
            company=company_name,
            on_date=effective_date,
            family_code=family_code,
        )

        # Step 2: fetch base item records from item_prices_{env} (full item details).
        records = get_prices_from_firestore(
            company=company_name,
            family_code=family_code,
            product_no=product_no,
            product_nos=nos_list,
            price_list_code=price_list_code,
        )

        if not records:
            # Check with include_blocked=True to distinguish "all filtered out / all blocked"
            # from "catalog not yet synced" — avoids a false 503 when records exist but are blocked.
            any_exist = get_prices_from_firestore(company=company_name, include_blocked=True)
            if any_exist:
                resp = {"data": [], "total": 0, "source": "firestore"}
                if using_bc_params:
                    resp.update({"bc_limit": bc_limit, "bc_offset": bc_offset})
                else:
                    resp.update({"skip": py_skip, "limit": py_limit})
                return resp
            from src.services.price_firestore_service import _collection_name
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    f"Item price catalog is empty — run POST /internal/firestore/routine-sync first. "
                    f"[company={company_name!r}, collection={_collection_name()!r}]"
                ),
                headers={"Retry-After": "60"},
            )

        # Step 3: overlay date-accurate prices from price_list_items_{env}.
        # price_list_items stores ALL price list lines for ALL codes; filtering to
        # active_codes gives us the prices effective on effective_date.
        if active_codes:
            overrides = get_price_overrides_from_price_list_items(
                company=company_name,
                price_list_codes=active_codes,
            )
            if overrides:
                merged = []
                for rec in records:
                    pno = rec.get("productNo") or ""
                    if pno in overrides:
                        rec = {**rec, **overrides[pno]}
                    merged.append(rec)
                records = merged

        total = len(records)
        page = records[py_skip:py_skip + py_limit] if py_limit > 0 else records[py_skip:]
        resp = {"data": page, "total": total, "onDate": effective_date, "activePriceLists": active_codes, "source": "firestore"}
        if using_bc_params:
            resp.update({"bc_limit": bc_limit, "bc_offset": bc_offset})
        else:
            resp.update({"skip": py_skip, "limit": py_limit})
        return resp

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing item prices (v3): {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_item_price_v3_router.get("/count", summary="Count Distinct Active Products (v3)")
def get_item_price_count(
    on_date: Optional[str] = Query(None, description="Accepted for compatibility — ignored when reading from Firestore."),
    family_code: Optional[str] = Query(None, description="Restrict count to a single item family."),
    product_no: Optional[str] = Query(None, description="Restrict count to a single product number."),
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    """Return the count of distinct products in the Firestore catalog (item_prices_{env}).

    Returns 503 when the catalog has not been synced yet.
    """
    import datetime
    effective_date = on_date or datetime.date.today().isoformat()
    company_name = company or config.BC_COMPANY

    try:
        records = get_prices_from_firestore(
            company=company_name,
            family_code=family_code,
            product_no=product_no,
        )

        if not records:
            any_exist = get_prices_from_firestore(company=company_name, include_blocked=True)
            if not any_exist:
                from src.services.price_firestore_service import _collection_name
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=(
                        f"Item price catalog is empty — run POST /internal/firestore/routine-sync first. "
                        f"[company={company_name!r}, collection={_collection_name()!r}]"
                    ),
                    headers={"Retry-After": "60"},
                )

        return {"totalCount": len(records), "onDate": effective_date, "familyCode": family_code, "source": "firestore"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching item price count (v3): {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_item_price_v3_router.post("/refresh", summary="Refresh Item Price Cache (v3)", status_code=status.HTTP_202_ACCEPTED)
def refresh_cache(
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    """Invalidate the in-process v3 cache and trigger a background refresh.
    To repopulate Firestore, use POST /internal/firestore/routine-sync instead."""
    company_name = company or config.BC_COMPANY
    rgmc_v3_invalidate_cache(company_name)
    rgmc_v3_warmup(company_name)
    return {"status": "refresh triggered", "company": company_name}


@rgmc_item_price_v3_router.get("/{item_price_id}", summary="Get Item Price by ID (v3)")
def get_item_price(
    item_price_id: str,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    """Fetch a single price record by SystemId directly from BC.

    Firestore is keyed by company+productNo — lookup by SystemId requires a BC call.
    """
    import requests as _requests
    try:
        http_status, data = rgmc_v3_get_item_price(item_price_id, company or config.BC_COMPANY)
        if http_status == 404:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=data)
        if http_status != 200:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"BC returned {http_status}: {data}")
        return data
    except HTTPException:
        raise
    except _requests.exceptions.Timeout as e:
        logger.error(f"BC API timed out fetching item price {item_price_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Business Central API timed out — retry in a few seconds.",
        )
    except Exception as e:
        logger.error(f"Error fetching item price {item_price_id} (v3): {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
