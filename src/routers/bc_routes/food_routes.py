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
import base64
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import Response
from src.services.bc_functions import (
    rgmc_v2_list_table_live,
    rgmc_v2_search_table_live,
    rgmc_v2_create_record,
    rgmc_v2_update_record,
    rgmc_v2_delete_record,
    rgmc_v2_get_contact_picture,
    odata_escape,
)
from src.models.bc_models.food_models import FoodSalesOrderCreate, FoodOrderHistoryRecord
from src.services import food_order_history_service
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


# Only these profile-editable fields (plus passwordHash) may ever be written —
# never number/id, which identify the BC record, not describe the person.
_CONTACT_PATCH_FIELD_MAP = {
    "displayName": "name",
    "email": "email",
    "phoneNumber": "phoneNo",
    "username": "username",
    "passwordHash": "passwordHash",
}


@food_router.patch("/contacts/{contact_id}", summary="Update a contact (profile fields and/or password hash)")
def update_contact(
    contact_id: str,
    body: Dict[str, Any],
    company: Optional[str] = Query(None),
):
    try:
        payload = {
            _CONTACT_PATCH_FIELD_MAP[k]: v
            for k, v in body.items()
            if k in _CONTACT_PATCH_FIELD_MAP
        }
        if not payload:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No updatable fields provided")
        http_status, data = rgmc_v2_update_record("contacts", contact_id, payload, company_name=_company(company))
        if http_status not in (200, 201):
            _bc_error(http_status, data)
        return {
            "id": data.get("id"),
            "number": data.get("number"),
            "displayName": data.get("name"),
            "email": data.get("email"),
            "phoneNumber": data.get("phoneNo"),
            "username": data.get("username"),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating contact {contact_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


# ---------------------------------------------------------------------------
# Contact picture — same contactPictures entity/page the garments app's
# rgmc_contact_v2_routes.py uses (RGMC Contact Picture API v2, page 50309),
# read live via the shared bc_functions helper. Kept self-contained here
# (own MIME-sniffing) rather than importing from that garments-specific
# router, per this file's file-level "intentionally separate" convention.
# ---------------------------------------------------------------------------

_IMAGE_SIGNATURES = [
    (b'\xff\xd8\xff', "image/jpeg"),
    (b'\x89PNG\r\n\x1a\n', "image/png"),
    (b'GIF87a', "image/gif"),
    (b'GIF89a', "image/gif"),
    (b'BM', "image/bmp"),
]
_MIN_IMAGE_BYTES = 64


def _detect_media_type(image_bytes: bytes) -> Optional[str]:
    for sig, mime in _IMAGE_SIGNATURES:
        if image_bytes[:len(sig)] == sig:
            return mime
    if image_bytes[:4] == b'RIFF' and image_bytes[8:12] == b'WEBP':
        return "image/webp"
    return None


@food_router.get("/contacts/{contact_id}/picture", summary="Get a contact's picture (live, read-only)")
def get_contact_picture(
    contact_id: str,
    company: Optional[str] = Query(None),
):
    try:
        http_status, data = rgmc_v2_get_contact_picture(contact_id, company_name=_company(company))
        if http_status == 404:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Contact picture not found")
        if http_status != 200:
            _bc_error(http_status, data)
        if not isinstance(data, dict):
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Unexpected response shape from Business Central")
        picture_b64 = data.get("picture") or ""
        if not picture_b64:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No picture data on this contact record")
        image_bytes = base64.b64decode(picture_b64)
        if len(image_bytes) < _MIN_IMAGE_BYTES:
            logger.error(f"Picture for food contact {contact_id} decoded to only {len(image_bytes)} bytes — field may be truncated.")
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="BC picture field appears truncated")
        media_type = _detect_media_type(image_bytes) or "image/jpeg"
        return Response(content=image_bytes, media_type=media_type)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching picture for food contact {contact_id}: {e}")
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
        safe = odata_escape(search.strip()) if search else ""
        http_status, records, total = rgmc_v2_search_table_live(
            "customers",
            company_name=_company(company),
            search_fields=["name", "customerNo"],
            search_term=safe,
            extra_filter="chain eq true",
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
                # Months of shelf life this customer requires — used, when the
                # user's "Include Item Shelf Life" setting is on, to extend a
                # picked lot's real BC expiration date for display/entry on
                # the Scan screen. A customer-level term, not an item one.
                "prodShelfLife": r.get("prodShelfLife"),
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
        safe = odata_escape(search.strip()) if search else ""
        http_status, records, total = rgmc_v2_search_table_live(
            "items",
            company_name=_company(company),
            search_fields=["number", "description"],
            search_term=safe,
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
# Item prices — bulk, live lookup for the "Display Item Prices" setting.
# A separate endpoint (not just a field on GET /items) because the item list
# shown to the user can come from the item catalog cache instead of a live
# search — price must still never be cached, so this is the one live call
# made for whatever page of items is currently on screen.
# ---------------------------------------------------------------------------

_MAX_PRICE_LOOKUP_ITEMS = 50

@food_router.get("/items/prices", summary="Live unit price for a set of items, keyed by item number")
def get_item_prices(
    numbers: str = Query(..., description="Comma-separated item numbers"),
    company: Optional[str] = Query(None),
):
    try:
        nums = [n.strip() for n in numbers.split(",") if n.strip()][:_MAX_PRICE_LOOKUP_ITEMS]
        if not nums:
            return {}
        # BC's OData does support an OR filter across multiple values of the
        # SAME field (unlike an OR across two different fields — see
        # rgmc_v2_search_table_live's own docstring) — one round trip for the
        # whole visible page of items rather than one call per item.
        filter_expr = " or ".join(f"number eq '{odata_escape(n)}'" for n in nums)
        http_status, records, _ = rgmc_v2_list_table_live(
            "items",
            company_name=_company(company),
            odata_filter=filter_expr,
            top=len(nums),
        )
        if http_status != 200:
            _bc_error(http_status, records)
        return {r.get("number"): r.get("unitPrice") for r in records if r.get("number")}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching item prices: {e}")
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
        # itemAvailableLots (RGMC Item Available Lot API v2) is sourced from
        # Item Ledger Entry — one row per inbound/consumption transaction, not
        # one row per lot. The same Lot No. can span several entries (e.g. two
        # separate receipts, or a receipt plus a correction), each carrying
        # only its OWN partial Remaining Quantity — showing that raw per-entry
        # number as "available" undercounts what's actually still available
        # for that lot. So every open entry for this item is fetched in one
        # live call (bounded — a real item has a handful of open batches, not
        # hundreds) and summed here by (Lot No., Location Code) — grouping by
        # location too, since a lot's available quantity must stay tied to
        # where that specific stock physically sits (the Sales Line's own
        # Location Code requirement) — before this endpoint's own limit/offset
        # is applied to the aggregated result.
        http_status, records, _ = rgmc_v2_list_table_live(
            "itemAvailableLots",
            company_name=_company(company),
            odata_filter=f"itemNo eq '{safe}' and remainingQuantity gt 0",
            orderby="expirationDate asc",
            top=500,
        )
        if http_status != 200:
            _bc_error(http_status, records)

        aggregated: Dict[Any, Dict[str, Any]] = {}
        for r in records:
            key = (r.get("lotNo"), r.get("locationCode"))
            bucket = aggregated.get(key)
            remaining = r.get("remainingQuantity") or 0
            if bucket is None:
                aggregated[key] = {
                    "itemNo": r.get("itemNo"),
                    "lotNo": r.get("lotNo"),
                    "expirationDate": r.get("expirationDate"),
                    "remainingQuantity": remaining,
                    "locationCode": r.get("locationCode"),
                }
            else:
                bucket["remainingQuantity"] += remaining
                # A lot should carry one consistent expiration date across all
                # its entries — if data entry ever left them differing, keep
                # the earliest so FEFO ordering stays conservative.
                other_date = r.get("expirationDate")
                if other_date and (not bucket["expirationDate"] or other_date < bucket["expirationDate"]):
                    bucket["expirationDate"] = other_date

        # "Remaining Quantity" alone overstates what's actually free to
        # promise — it only drops as stock ships/is invoiced, not when a
        # sales line already has an outstanding tracking-line demand tied to
        # this specific lot. Confirmed directly against real data (debug
        # output) that every such demand line DOES carry its own Lot No. —
        # e.g. one entry with lotNo="TEST3", another with lotNo="TEST5",
        # each qty 1 — genuinely per-lot, not a shared/unassigned pool as
        # first assumed (that theory came from Reservation Entries' "Reserved
        # From" column being blank for a Surplus-status entry, which only
        # means "not linked to a specific Item Ledger Entry," not "no lot").
        # BC's Lot No. List lookup showing the same "Total Requested
        # Quantity"=1 for both lots was coincidental: each lot's OWN demand
        # happened to be exactly 1. So the correct fix sums demand PER
        # (Lot No., Location Code) and subtracts each lot's own total from
        # itself only.
        http_status, tracking_records, _ = rgmc_v2_list_table_live(
            "salesLineTrackingLines",
            company_name=_company(company),
            odata_filter=f"itemNo eq '{safe}'",
            top=500,
        )
        demand_by_lot: Dict[Any, float] = {}
        if http_status == 200:
            for t in tracking_records:
                key = (t.get("lotNo"), t.get("locationCode"))
                demand_by_lot[key] = demand_by_lot.get(key, 0) + abs(t.get("quantityBase") or 0)
        else:
            logger.warning(f"Could not fetch tracking-line demand for item {item_no}: BC returned {http_status}")

        mapped = []
        for key, bucket in aggregated.items():
            demand = demand_by_lot.get(key, 0)
            bucket["remainingQuantity"] = max(bucket["remainingQuantity"] - demand, 0)
            mapped.append(bucket)
        mapped.sort(key=lambda x: (x["expirationDate"] or "", x["lotNo"] or ""))

        total = len(mapped)
        page = mapped[offset:offset + limit]
        return _page_envelope(page, total, limit, offset)
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

_SALES_LINE_TRACKING_TABLE = "salesLineTrackingLines"


def _create_tracking_line(item_no: str, document_no: str, line_no: int, quantity_base: float, lot_no: str, expiration_date: Optional[str], company_name: str, location_code: Optional[str] = None) -> None:
    payload: Dict[str, Any] = {
        "itemNo": item_no,
        "documentNo": document_no,
        "lineNo": line_no,
        "lotNo": lot_no,
        "quantityBase": quantity_base,
    }
    if expiration_date:
        payload["expirationDate"] = expiration_date
    # Item tracking is matched to a document line by Location Code as well as
    # Source Type/ID/Ref No. — leaving this blank while the Sales Line itself
    # has a location silently orphans the entry: it inserts fine (no error)
    # but never surfaces as an assignment in BC's Item Tracking Lines page.
    if location_code:
        payload["locationCode"] = location_code
    for attempt in range(4):
        th, td = rgmc_v2_create_record(_SALES_LINE_TRACKING_TABLE, payload, company_name=company_name)
        if th in (200, 201):
            return
        if th == 409 and attempt < 3:
            time.sleep(0.5 * (attempt + 1))
            continue
        raise ValueError(f"BC returned {th}: {td}")


def _create_line(order_id: str, company_name: str, index: int, line) -> Optional[str]:
    """Creates the sales line, then best-effort writes its lot tracking.
    Returns a non-fatal tracking warning message, or None. Raises only for a
    failure to create the sales line itself — that's the one failure serious
    enough to roll back the whole order."""
    payload = {
        "lineType": "Item",
        "number": line.itemNumber,
        "description": line.description,
        "quantity": line.quantity,
        "unitOfMeasureCode": line.unitOfMeasureCode,
        # Explicit, pre-computed Line No. (same 10000-step convention BC's own
        # auto-numbering uses) — lines are created with up to 2 concurrent
        # workers below, and leaving Line No. to BC's own "highest existing +
        # 10000" auto-assignment is a race under concurrent inserts against
        # the same document: two lines can read the same "current max" before
        # either commits, collide, and get silently renumbered. A tracking
        # line created afterward against the Line No. this response *claimed*
        # then points at a Source Ref. No. that no longer matches reality,
        # which is what produces BC's "The Reservation Entry does not exist"
        # error later (a stale Entry No. left behind by the collision).
        # Assigning it ourselves up front removes the race entirely.
        "lineNo": index * 10000,
    }
    if line.locationCode:
        # Required at posting time for a lot-tracked item's Sales Line, even
        # for a plain tracking assignment with no separate reservation — and
        # must match the physical location the chosen lot's stock sits in.
        payload["locationCode"] = line.locationCode
    line_data: Optional[Dict[str, Any]] = None
    for attempt in range(4):
        lh, ld = rgmc_v2_create_record(
            f"{_SALES_ORDER_TABLE}({order_id})/{_SALES_ORDER_LINES_TABLE}",
            payload,
            company_name=company_name,
        )
        if lh in (200, 201):
            line_data = ld
            break
        if lh == 409 and attempt < 3:
            time.sleep(0.5 * (attempt + 1))
            continue
        raise ValueError(f"BC returned {lh}: {ld}")

    # Lot tracking is a follow-up write against the just-created line — a
    # sales line can be created without one (item isn't lot-tracked), but if
    # the user picked a lot in the Add Items modal, carry it onto the BC
    # Item Tracking Line so the physical batch sold stays traceable. This is
    # intentionally non-fatal: the sales line/order is already valid and
    # postable without it (e.g. before the RGMC Sales Ln Tracking API v2 AL
    # page has been published to a given BC environment), so a tracking
    # failure is surfaced as a warning rather than rolling back the order.
    if line.lotNo and line_data:
        try:
            _create_tracking_line(
                item_no=line.itemNumber,
                document_no=line_data.get("documentNo"),
                line_no=line_data.get("lineNo"),
                quantity_base=line.quantity * (line.qtyPerUnitOfMeasure or 1),
                lot_no=line.lotNo,
                expiration_date=line.expirationDate,
                company_name=company_name,
                location_code=line.locationCode or line_data.get("locationCode"),
            )
        except Exception as e:
            logger.error(f"Item tracking write failed for {line.itemNumber} on order {order_id}: {e}")
            return f"Line {index}: lot {line.lotNo} was not recorded on the BC Item Tracking Line ({e})"
    return None


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
        tracking_warnings: List[str] = []
        with ThreadPoolExecutor(max_workers=2) as executor:
            future_to_idx = {
                executor.submit(_create_line, order_id, company_name, i, line): i
                for i, line in enumerate(body.lines, start=1)
            }
            for future in as_completed(future_to_idx):
                exc = future.exception()
                if exc:
                    errors.append((future_to_idx[future], exc))
                else:
                    warning = future.result()
                    if warning:
                        tracking_warnings.append(warning)

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

        result = {"documentNumber": document_number, "externalDocumentNo": body.orderNumber}
        if tracking_warnings:
            result["trackingWarnings"] = tracking_warnings
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error submitting food sales order: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


# ---------------------------------------------------------------------------
# Order submission history — a dedicated Firestore collection
# (food_order_history_{env}, see services/food_order_history_service.py),
# entirely separate from the garments app's session_history_{env} collection.
# One record per submission ATTEMPT (success or failure), written by the
# frontend right after each /sales-orders call resolves either way, so a
# failed attempt is just as visible in history as a successful one.
# ---------------------------------------------------------------------------

@food_router.post("/order-history", summary="Record an order submission attempt (success or failure)", status_code=status.HTTP_201_CREATED)
def create_order_history(record: FoodOrderHistoryRecord):
    try:
        doc_id = food_order_history_service.save_order_history(record.model_dump())
        return {"id": doc_id}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"Error saving food order history: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@food_router.get("/order-history", summary="List order submission history for one user, most recent first")
def list_order_history(
    username: str = Query(..., min_length=1),
    company: Optional[str] = Query(None, description="Optional company code filter"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    try:
        records, total = food_order_history_service.get_order_history(
            username=username, company_code=company, limit=limit, offset=offset,
        )
        return _page_envelope(records, total, limit, offset)
    except Exception as e:
        logger.error(f"Error listing food order history: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
