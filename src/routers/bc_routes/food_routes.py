"""SBIC Consignment Webapp — Food & Beverages.

Every endpoint here is intentionally separate from the garments app's routers
above: it never reads the in-process/GCS/Firestore caches (bc_functions'
_list_cache, gcs_catalog, price_firestore_service) and never fetches a whole
table via _fetch_all_pages. Each call hits Business Central fresh through
rgmc_v2_list_table_live, bounded to exactly one page via BC-native
$top/$skip/$count. See services/bc_functions.py's "live, single-page reads"
section for the shared helper this router builds on.

Field names below are taken directly from the RGMC custom API v2.0 AL pages
(companySettings/Pag50492, contacts/Pag50308, customers/Pag50306,
items/Pag50310, itemAvailableLots/Pag50354, itemUnitsOfMeasure/Pag50353,
salesOrders+salesOrderLines/Pag50315-50316) — this router's job is purely to
translate that shape into the flatter, paginated shape the food app expects.
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException, Query, status
from src.services.bc_functions import (
    rgmc_v2_list_table_live,
    rgmc_v2_create_record,
    rgmc_v2_update_record,
    rgmc_v2_delete_record,
    odata_escape,
)
from src.models.bc_models.food_models import FoodSalesOrderCreate
from src import config

logger = logging.getLogger("bc_routes.food")

food_router = APIRouter(prefix="/food", tags=["SBIC Food & Beverages Consignment"])

_SALES_ORDER_TABLE = "salesOrders"
_SALES_ORDER_LINES_TABLE = "salesOrderLines"


def _company(company: Optional[str]) -> str:
    return company or config.BC_COMPANY


def _page_envelope(records: List[Dict[str, Any]], total: int, limit: int, offset: int) -> Dict[str, Any]:
    return {"value": records, "total": total, "limit": limit, "offset": offset}


def _bc_error(http_status: int, records: Any) -> None:
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"Business Central returned {http_status}: {records}",
    )


# ---------------------------------------------------------------------------
# Companies — filtered by foodConsignmentVisible instead of consignmentAppVisible
# ---------------------------------------------------------------------------

@food_router.get("/companies", summary="List food-consignment-visible companies")
def list_companies(
    company: Optional[str] = Query(None, description="Any BC company name — company-settings is a shared, not per-company, table"),
):
    try:
        http_status, records, total = rgmc_v2_list_table_live(
            "companySettings",
            company_name=_company(company),
            odata_filter="foodConsignmentVisible eq true",
            top=200,
        )
        if http_status != 200:
            _bc_error(http_status, records)
        mapped = [
            {
                "id": r.get("id"),
                "code": r.get("companyName"),
                "displayName": r.get("displayName") or r.get("companyName"),
                "foodConsignmentVisible": r.get("foodConsignmentVisible"),
            }
            for r in records
        ]
        return _page_envelope(mapped, total, 200, 0)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing food companies: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


# ---------------------------------------------------------------------------
# Contacts — single live lookup by username, for login. No full-list prefetch.
# ---------------------------------------------------------------------------

@food_router.get("/contacts", summary="Look up a contact by username (live, for login)")
def get_contact_by_username(
    username: str = Query(..., min_length=1),
    company: Optional[str] = Query(None),
):
    try:
        safe = odata_escape(username.strip())
        http_status, records, total = rgmc_v2_list_table_live(
            "contacts",
            company_name=_company(company),
            odata_filter=f"tolower(username) eq tolower('{safe}')",
            top=1,
        )
        if http_status != 200:
            _bc_error(http_status, records)
        mapped = [
            {
                "id": r.get("id"),
                "number": r.get("number"),
                "displayName": r.get("name"),
                "email": r.get("email"),
                "phoneNumber": r.get("phoneNo"),
                "username": r.get("username"),
                "passwordHash": r.get("passwordHash"),
            }
            for r in records
        ]
        return _page_envelope(mapped, total, 1, 0)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error looking up contact by username: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@food_router.patch("/contacts/{contact_id}", summary="Update a contact (password hash / setup)")
def update_contact(
    contact_id: str,
    body: Dict[str, Any],
    company: Optional[str] = Query(None),
):
    try:
        allowed = {k: v for k, v in body.items() if k in ("passwordHash",)}
        if not allowed:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No updatable fields provided")
        http_status, data = rgmc_v2_update_record("contacts", contact_id, allowed, company_name=_company(company))
        if http_status not in (200, 201):
            _bc_error(http_status, data)
        return {"id": data.get("id"), "username": data.get("username")}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating contact {contact_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


# ---------------------------------------------------------------------------
# Customers — chain=true is always enforced server-side, live + paginated
# ---------------------------------------------------------------------------

@food_router.get("/customers", summary="Search chain=true customers (live, paginated)")
def list_customers(
    search: Optional[str] = Query(None),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    company: Optional[str] = Query(None),
):
    try:
        clauses = ["chain eq true"]
        if search:
            safe = odata_escape(search.strip())
            clauses.append(f"(contains(name,'{safe}') or contains(customerNo,'{safe}'))")
        http_status, records, total = rgmc_v2_list_table_live(
            "customers",
            company_name=_company(company),
            odata_filter=" and ".join(clauses),
            orderby="name asc",
            top=limit,
            skip=offset,
        )
        if http_status != 200:
            _bc_error(http_status, records)
        mapped = [
            {
                "id": r.get("id"),
                "number": r.get("customerNo"),
                "displayName": r.get("name"),
                "address": r.get("address"),
                "city": r.get("city"),
                "county": r.get("county"),
                "postCode": r.get("postCode"),
                "countryRegionCode": r.get("countryRegionCode"),
                "phoneNumber": r.get("phoneNo"),
                "email": r.get("email"),
                "chain": r.get("chain"),
            }
            for r in records
        ]
        return _page_envelope(mapped, total, limit, offset)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing food customers: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


# ---------------------------------------------------------------------------
# Items — number/description search, no brand/family filter, live + paginated
# ---------------------------------------------------------------------------

@food_router.get("/items", summary="Search items by number or description (live, paginated)")
def list_items(
    search: Optional[str] = Query(None),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    company: Optional[str] = Query(None),
):
    try:
        odata_filter = None
        if search:
            safe = odata_escape(search.strip())
            odata_filter = f"(contains(number,'{safe}') or contains(description,'{safe}'))"
        http_status, records, total = rgmc_v2_list_table_live(
            "items",
            company_name=_company(company),
            odata_filter=odata_filter,
            orderby="number asc",
            top=limit,
            skip=offset,
        )
        if http_status != 200:
            _bc_error(http_status, records)
        mapped = [
            {
                "id": r.get("id"),
                "number": r.get("number"),
                "description": r.get("description"),
                "baseUnitOfMeasureCode": r.get("baseUnitOfMeasure"),
            }
            for r in records
        ]
        return _page_envelope(mapped, total, limit, offset)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing food items: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


# ---------------------------------------------------------------------------
# Item lots — open lots for one item, oldest expiration first (live, paginated)
# ---------------------------------------------------------------------------

@food_router.get("/items/{item_no}/lots", summary="Open lots for an item, oldest expiration first")
def list_item_lots(
    item_no: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    company: Optional[str] = Query(None),
):
    try:
        safe = odata_escape(item_no.strip())
        http_status, records, total = rgmc_v2_list_table_live(
            "itemAvailableLots",
            company_name=_company(company),
            odata_filter=f"itemNo eq '{safe}' and remainingQuantity gt 0",
            orderby="expirationDate asc",
            top=limit,
            skip=offset,
        )
        if http_status != 200:
            _bc_error(http_status, records)
        mapped = [
            {
                "itemNo": r.get("itemNo"),
                "lotNo": r.get("lotNo"),
                "expirationDate": r.get("expirationDate"),
                "remainingQuantity": r.get("remainingQuantity"),
            }
            for r in records
        ]
        return _page_envelope(mapped, total, limit, offset)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing lots for item {item_no}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


# ---------------------------------------------------------------------------
# Item units of measure (live — small per-item list, no pagination needed)
# ---------------------------------------------------------------------------

@food_router.get("/items/{item_no}/uom", summary="Valid units of measure for an item")
def list_item_uom(
    item_no: str,
    company: Optional[str] = Query(None),
):
    try:
        safe = odata_escape(item_no.strip())
        http_status, records, total = rgmc_v2_list_table_live(
            "itemUnitsOfMeasure",
            company_name=_company(company),
            odata_filter=f"itemNo eq '{safe}'",
            top=50,
        )
        if http_status != 200:
            _bc_error(http_status, records)
        mapped = [
            {
                "itemNo": r.get("itemNo"),
                "code": r.get("code"),
                "description": r.get("description"),
                "qtyPerUnitOfMeasure": r.get("qtyPerUnitOfMeasure"),
            }
            for r in records
        ]
        return _page_envelope(mapped, total, 50, 0)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing units of measure for item {item_no}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


# ---------------------------------------------------------------------------
# Sales order submission — direct, synchronous create. No Cloud Tasks queue.
# The Order No. the user enters maps to Sales Header's External Document No.
# so the BC document stays traceable back to the online order (per spec).
# Posting/shipping is intentionally out of scope — that happens as its own
# Business Central transaction once the order reaches BC.
# ---------------------------------------------------------------------------

def _create_line(order_id: str, company_name: str, index: int, line) -> None:
    payload = {
        "lineType": "Item",
        "number": line.itemNumber,
        "description": line.description,
        "quantity": line.quantity,
        "unitOfMeasureCode": line.unitOfMeasureCode,
    }
    for attempt in range(4):
        lh, ld = rgmc_v2_create_record(
            f"{_SALES_ORDER_TABLE}({order_id})/{_SALES_ORDER_LINES_TABLE}",
            payload,
            company_name=company_name,
        )
        if lh in (200, 201):
            return
        if lh == 409 and attempt < 3:
            time.sleep(0.5 * (attempt + 1))
            continue
        raise ValueError(f"BC returned {lh}: {ld}")


@food_router.post("/sales-orders", summary="Submit a food consignment sales order (synchronous, direct to BC)", status_code=status.HTTP_201_CREATED)
def submit_sales_order(
    body: FoodSalesOrderCreate,
    company: Optional[str] = Query(None),
):
    company_name = _company(company)
    try:
        if not body.lines:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="At least one line is required")

        header_payload = {
            "sellToCustomerNo": body.customerNumber,
            "postingDate": body.postingDate,
            "orderDate": body.postingDate,
            "externalDocumentNo": body.orderNumber,
        }
        http_status, data = rgmc_v2_create_record(_SALES_ORDER_TABLE, header_payload, company_name=company_name)
        if http_status not in (200, 201):
            _bc_error(http_status, data)
        order_id = data.get("id")
        document_number = data.get("number")

        errors: List[tuple] = []
        with ThreadPoolExecutor(max_workers=2) as executor:
            future_to_idx = {
                executor.submit(_create_line, order_id, company_name, i, line): i
                for i, line in enumerate(body.lines, start=1)
            }
            for future in as_completed(future_to_idx):
                exc = future.exception()
                if exc:
                    errors.append((future_to_idx[future], exc))

        if errors:
            first_idx, first_err = min(errors, key=lambda x: x[0])
            logger.error(f"Failed to create {len(errors)} line(s) for food sales order {order_id}: {first_err}")
            try:
                rgmc_v2_delete_record(_SALES_ORDER_TABLE, order_id, company_name=company_name)
            except Exception as del_err:
                logger.error(f"Rollback failed for food sales order {order_id}: {del_err}")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Line {first_idx} creation failed: {first_err}. Nothing was posted — order rolled back.",
            )

        return {"documentNumber": document_number, "externalDocumentNo": body.orderNumber}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error submitting food sales order: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
