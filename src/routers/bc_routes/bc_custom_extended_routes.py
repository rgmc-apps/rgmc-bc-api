"""RGMC custom API v2.0 — New extended BC endpoints (Pages 50322–50341, GET only).

Exposed on a dedicated Swagger page at /swagger-extended.
All routes forward to BC's api/rgmc/rgmccustom/v2.0 namespace.
"""
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, status

from src import config
from src.services.bc_functions import (
    call_rgmc_v2_table,
    get_all_companies_cached,
    rgmc_v2_get_record,
)
from src.services.price_firestore_service import get_item_ledger_entries_from_firestore

logger = logging.getLogger("bc_routes.custom_extended")

bc_custom_extended_router = APIRouter(prefix="/bc/custom/v2")

# ---------------------------------------------------------------------------
# Shared Query parameter defaults
# ---------------------------------------------------------------------------

_COMPANY_Q = Query("ALL", description="BC company name. Use 'ALL' (default) to fetch from all companies.")
_FILTER_Q = Query(None, description="OData $filter expression (applied in addition to any date filters).")
_MODIFIED_FROM_Q = Query(None, description="lastModifiedDateTime on or after this value (ISO 8601, e.g. 2024-01-01T00:00:00Z). Pass 'NOW' to use the start of today (UTC).")
_MODIFIED_TO_Q = Query(None, description="lastModifiedDateTime on or before this value (ISO 8601, e.g. 2024-12-31T23:59:59Z).")
_MODIFIED_AS_OF_DATE_Q = Query(None, description="lastModifiedDateTime on this exact calendar date (YYYY-MM-DD).")
_MODIFIED_MONTH_Q = Query(None, description="lastModifiedDateTime within this month (YYYY-MM, e.g. 2024-01).")
_MODIFIED_YEAR_Q = Query(None, description="lastModifiedDateTime within this year (integer, e.g. 2024).")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _all_company_names() -> List[str]:
    http_status, data = get_all_companies_cached()
    if http_status != 200:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Failed to fetch BC companies: {data}")
    return [c["name"] for c in data.get("value", [])]


def _combine_filter(
    raw_filter: Optional[str],
    modified_from: Optional[str],
    modified_to: Optional[str],
    modified_as_of_date: Optional[str],
    modified_month: Optional[str],
    modified_year: Optional[int],
) -> Optional[str]:
    """Merge raw OData filter + all lastModifiedDateTime parameters into one $filter string."""
    parts: List[str] = []
    if raw_filter:
        parts.append(raw_filter)
    if modified_from and modified_from.upper() == "NOW":
        modified_from = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    if modified_from:
        parts.append(f"lastModifiedDateTime ge {modified_from}")
    if modified_to:
        parts.append(f"lastModifiedDateTime le {modified_to}")
    if modified_as_of_date:
        try:
            d = date.fromisoformat(modified_as_of_date)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Invalid modified_as_of_date '{modified_as_of_date}'. Use YYYY-MM-DD.")
        next_d = d + timedelta(days=1)
        parts.append(f"lastModifiedDateTime ge {d.isoformat()}T00:00:00Z")
        parts.append(f"lastModifiedDateTime lt {next_d.isoformat()}T00:00:00Z")
    if modified_month:
        try:
            year, month = map(int, modified_month.split("-"))
            first_day = date(year, month, 1)
            next_month = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        except (ValueError, TypeError):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Invalid modified_month '{modified_month}'. Use YYYY-MM.")
        parts.append(f"lastModifiedDateTime ge {first_day.isoformat()}T00:00:00Z")
        parts.append(f"lastModifiedDateTime lt {next_month.isoformat()}T00:00:00Z")
    if modified_year is not None:
        parts.append(f"lastModifiedDateTime ge {modified_year}-01-01T00:00:00Z")
        parts.append(f"lastModifiedDateTime lt {modified_year + 1}-01-01T00:00:00Z")
    return " and ".join(parts) if parts else None


def _list_response(table_endpoint: str, company: str, odata_filter: Optional[str]) -> Dict[str, Any]:
    return {"data": _list(table_endpoint, company, odata_filter)}


def _get_response(table_endpoint: str, record_id: str, company: str, entity: str) -> Dict[str, Any]:
    return _get(table_endpoint, record_id, company, entity)


def _list(table_endpoint: str, company: str, odata_filter: Optional[str]) -> List[Dict[str, Any]]:
    """Fetch list records from one company or all companies when company == 'ALL'."""
    if company.upper() == "ALL":
        results: List[Dict[str, Any]] = []
        for name in _all_company_names():
            http_status, data = call_rgmc_v2_table(table_endpoint, company_name=name, odata_filter=odata_filter)
            if http_status == 200:
                records = data.get("value", data) if isinstance(data, dict) else data
                if isinstance(records, list):
                    results.extend(records)
        return results
    http_status, data = call_rgmc_v2_table(table_endpoint, company_name=company, odata_filter=odata_filter)
    if http_status != 200:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Business Central returned {http_status}: {data}")
    return data.get("value", data) if isinstance(data, dict) else data


def _get(table_endpoint: str, record_id: str, company: str, entity: str) -> Dict[str, Any]:
    """Fetch a single record, searching all companies when company == 'ALL'."""
    if company.upper() == "ALL":
        for name in _all_company_names():
            http_status, data = rgmc_v2_get_record(table_endpoint, record_id, name)
            if http_status == 200:
                return data
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{entity} not found in any company")
    http_status, data = rgmc_v2_get_record(table_endpoint, record_id, company)
    if http_status == 404:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{entity} not found")
    if http_status != 200:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Business Central returned {http_status}: {data}")
    return data


# ---------------------------------------------------------------------------
# LS Central Transactions — Pages 50322–50324
# ---------------------------------------------------------------------------

_TAG_TX = "BC Custom Extended — LS Central Transactions"


@bc_custom_extended_router.get("/transaction-headers", tags=[_TAG_TX], summary="List Transaction Headers (Pag50322)")
def list_transaction_headers(
    company: Optional[str] = _COMPANY_Q,
    filter: Optional[str] = _FILTER_Q,
    modified_from: Optional[str] = _MODIFIED_FROM_Q,
    modified_to: Optional[str] = _MODIFIED_TO_Q,
    modified_as_of_date: Optional[str] = _MODIFIED_AS_OF_DATE_Q,
    modified_month: Optional[str] = _MODIFIED_MONTH_Q,
    modified_year: Optional[int] = _MODIFIED_YEAR_Q,
):
    """List all LSC Transaction Headers (Pag50322, source table: `LSC Transaction Header` 99001472).

    Fields: `id`, `storeNo`, `posTerminalNo`, `transactionNo`, `receiptNo`,
    `transactionDate`, `transactionTime`, `memberCardNo`, `customerNo`, `staffId`,
    `netAmount`, `grossAmount`, `discountAmount`, `noOfItems`, `statementNo`,
    `companyName`, `lastModifiedDateTime`.

    BC column names: `Store No.`, `POS Terminal No.`, `Transaction No.`, `Receipt No.`,
    `Transaction Date`, `Transaction Time`, `Member Card No.`, `Customer No.`,
    `Staff ID`, `Net Amount`, `Gross Amount`, `Discount Amount`, `No. of Items`,
    `Statement No.`
    """
    try:
        combined = _combine_filter(filter, modified_from, modified_to, modified_as_of_date, modified_month, modified_year)
        return _list_response("transactionHeaders", company, combined)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing transaction headers: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@bc_custom_extended_router.get("/transaction-headers/{record_id}", tags=[_TAG_TX], summary="Get Transaction Header by ID (Pag50322)")
def get_transaction_header(record_id: str, company: Optional[str] = _COMPANY_Q):
    """Fetch a single LSC Transaction Header by SystemId from BC."""
    try:
        return _get_response("transactionHeaders", record_id, company, "Transaction Header")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching transaction header {record_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@bc_custom_extended_router.get("/transaction-headers/{header_id}/transaction-sales-entries", tags=[_TAG_TX], summary="List Transaction Sales Entries under a Header (Pag50323 nested)")
def list_transaction_sales_entries_nested(
    header_id: str,
    company: Optional[str] = _COMPANY_Q,
    filter: Optional[str] = _FILTER_Q,
    modified_from: Optional[str] = _MODIFIED_FROM_Q,
    modified_to: Optional[str] = _MODIFIED_TO_Q,
    modified_as_of_date: Optional[str] = _MODIFIED_AS_OF_DATE_Q,
    modified_month: Optional[str] = _MODIFIED_MONTH_Q,
    modified_year: Optional[int] = _MODIFIED_YEAR_Q,
):
    """List LSC Transaction Sales Entries nested under a specific Transaction Header (Pag50323)."""
    try:
        combined = _combine_filter(filter, modified_from, modified_to, modified_as_of_date, modified_month, modified_year)
        return _list_response(f"transactionHeaders({header_id})/transactionSalesEntries", company, combined)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing nested transaction sales entries for header {header_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@bc_custom_extended_router.get("/transaction-headers/{header_id}/trans-payment-entries", tags=[_TAG_TX], summary="List Transaction Payment Entries under a Header (Pag50324 nested)")
def list_trans_payment_entries_nested(
    header_id: str,
    company: Optional[str] = _COMPANY_Q,
    filter: Optional[str] = _FILTER_Q,
    modified_from: Optional[str] = _MODIFIED_FROM_Q,
    modified_to: Optional[str] = _MODIFIED_TO_Q,
    modified_as_of_date: Optional[str] = _MODIFIED_AS_OF_DATE_Q,
    modified_month: Optional[str] = _MODIFIED_MONTH_Q,
    modified_year: Optional[int] = _MODIFIED_YEAR_Q,
):
    """List LSC Transaction Payment Entries nested under a specific Transaction Header (Pag50324)."""
    try:
        combined = _combine_filter(filter, modified_from, modified_to, modified_as_of_date, modified_month, modified_year)
        return _list_response(f"transactionHeaders({header_id})/transPaymentEntries", company, combined)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing nested payment entries for header {header_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@bc_custom_extended_router.get("/transaction-sales-entries", tags=[_TAG_TX], summary="List Transaction Sales Entries (Pag50323)")
def list_transaction_sales_entries(
    company: Optional[str] = _COMPANY_Q,
    filter: Optional[str] = _FILTER_Q,
    modified_from: Optional[str] = _MODIFIED_FROM_Q,
    modified_to: Optional[str] = _MODIFIED_TO_Q,
    modified_as_of_date: Optional[str] = _MODIFIED_AS_OF_DATE_Q,
    modified_month: Optional[str] = _MODIFIED_MONTH_Q,
    modified_year: Optional[int] = _MODIFIED_YEAR_Q,
):
    """List all LSC Transaction Sales Entries (Pag50323, source table: `LSC Trans. Sales Entry` 99001473).

    Fields: `id`, `storeNo`, `posTerminalNo`, `transactionNo`, `lineNo`, `receiptNo`,
    `transactionDate`, `transactionTime`, `itemNo`, `barcodeNo`, `variantCode`,
    `unitOfMeasure`, `quantity`, `price`, `netPrice`, `netAmount`, `discountAmount`,
    `discountPercent`, `costAmount`, `staffId`, `companyName`, `lastModifiedDateTime`.

    BC column names: `Store No.`, `POS Terminal No.`, `Transaction No.`, `Line No.`,
    `Receipt No.`, `Transaction Date`, `Transaction Time`, `Item No.`, `Barcode No.`,
    `Variant Code`, `Unit of Measure`, `Quantity`, `Price`, `Net Price`, `Net Amount`,
    `Discount Amount`, `Discount %`, `Cost Amount`, `Staff ID`.
    """
    try:
        combined = _combine_filter(filter, modified_from, modified_to, modified_as_of_date, modified_month, modified_year)
        return _list_response("transactionSalesEntries", company, combined)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing transaction sales entries: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@bc_custom_extended_router.get("/transaction-sales-entries/{record_id}", tags=[_TAG_TX], summary="Get Transaction Sales Entry by ID (Pag50323)")
def get_transaction_sales_entry(record_id: str, company: Optional[str] = _COMPANY_Q):
    """Fetch a single LSC Transaction Sales Entry by SystemId from BC."""
    try:
        return _get_response("transactionSalesEntries", record_id, company, "Transaction Sales Entry")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching transaction sales entry {record_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@bc_custom_extended_router.get("/trans-payment-entries", tags=[_TAG_TX], summary="List Transaction Payment Entries (Pag50324)")
def list_trans_payment_entries(
    company: Optional[str] = _COMPANY_Q,
    filter: Optional[str] = _FILTER_Q,
    modified_from: Optional[str] = _MODIFIED_FROM_Q,
    modified_to: Optional[str] = _MODIFIED_TO_Q,
    modified_as_of_date: Optional[str] = _MODIFIED_AS_OF_DATE_Q,
    modified_month: Optional[str] = _MODIFIED_MONTH_Q,
    modified_year: Optional[int] = _MODIFIED_YEAR_Q,
):
    """List all LSC Transaction Payment Entries (Pag50324, source table: `LSC Trans. Payment Entry` 99001474).

    Fields: `id`, `storeNo`, `posTerminalNo`, `transactionNo`, `lineNo`, `receiptNo`,
    `transactionDate`, `transactionTime`, `tenderType`, `amountTendered`,
    `currencyCode`, `staffId`, `companyName`, `lastModifiedDateTime`.

    BC column names: `Store No.`, `POS Terminal No.`, `Transaction No.`, `Line No.`,
    `Receipt No.`, `Transaction Date`, `Transaction Time`, `Tender Type`,
    `Amount Tendered`, `Currency Code`, `Staff ID`.
    """
    try:
        combined = _combine_filter(filter, modified_from, modified_to, modified_as_of_date, modified_month, modified_year)
        return _list_response("transPaymentEntries", company, combined)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing trans payment entries: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@bc_custom_extended_router.get("/trans-payment-entries/{record_id}", tags=[_TAG_TX], summary="Get Transaction Payment Entry by ID (Pag50324)")
def get_trans_payment_entry(record_id: str, company: Optional[str] = _COMPANY_Q):
    """Fetch a single LSC Transaction Payment Entry by SystemId from BC."""
    try:
        return _get_response("transPaymentEntries", record_id, company, "Transaction Payment Entry")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching trans payment entry {record_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


# ---------------------------------------------------------------------------
# LS Central Retail Setup — Pages 50325–50326, 50335–50336
# ---------------------------------------------------------------------------

_TAG_RETAIL = "BC Custom Extended — LS Central Retail Setup"


@bc_custom_extended_router.get("/tender-type-setups", tags=[_TAG_RETAIL], summary="List Tender Type Setups (Pag50325)")
def list_tender_type_setups(
    company: Optional[str] = _COMPANY_Q,
    filter: Optional[str] = _FILTER_Q,
    modified_from: Optional[str] = _MODIFIED_FROM_Q,
    modified_to: Optional[str] = _MODIFIED_TO_Q,
    modified_as_of_date: Optional[str] = _MODIFIED_AS_OF_DATE_Q,
    modified_month: Optional[str] = _MODIFIED_MONTH_Q,
    modified_year: Optional[int] = _MODIFIED_YEAR_Q,
):
    """List all LSC Tender Type Setups (Pag50325, source table: `LSC Tender Type Setup` 99001466).

    Fields: `id`, `code`, `description`, `companyName`, `lastModifiedDateTime`.

    BC column names: `Code`, `Description`.
    """
    try:
        combined = _combine_filter(filter, modified_from, modified_to, modified_as_of_date, modified_month, modified_year)
        return _list_response("tenderTypeSetups", company, combined)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing tender type setups: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@bc_custom_extended_router.get("/tender-type-setups/{record_id}", tags=[_TAG_RETAIL], summary="Get Tender Type Setup by ID (Pag50325)")
def get_tender_type_setup(record_id: str, company: Optional[str] = _COMPANY_Q):
    """Fetch a single LSC Tender Type Setup by SystemId from BC."""
    try:
        return _get_response("tenderTypeSetups", record_id, company, "Tender Type Setup")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching tender type setup {record_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@bc_custom_extended_router.get("/stores", tags=[_TAG_RETAIL], summary="List Stores (Pag50326)")
def list_stores(
    company: Optional[str] = _COMPANY_Q,
    filter: Optional[str] = _FILTER_Q,
    modified_from: Optional[str] = _MODIFIED_FROM_Q,
    modified_to: Optional[str] = _MODIFIED_TO_Q,
    modified_as_of_date: Optional[str] = _MODIFIED_AS_OF_DATE_Q,
    modified_month: Optional[str] = _MODIFIED_MONTH_Q,
    modified_year: Optional[int] = _MODIFIED_YEAR_Q,
):
    """List all LSC Stores (Pag50326, source table: `LSC Store` 99001470).

    Fields: `id`, `no`, `name`, `address`, `address2`, `city`, `county`, `postCode`,
    `phoneNo`, `email`, `currencyCode`, `locationCode`, `responsibilityCenter`,
    `companyName`, `lastModifiedDateTime`.

    BC column names: `No.`, `Name`, `Address`, `Address 2`, `City`, `County`,
    `Post Code`, `Phone No.`, `E-Mail`, `Currency Code`, `Location Code`,
    `Responsibility Center`.
    """
    try:
        combined = _combine_filter(filter, modified_from, modified_to, modified_as_of_date, modified_month, modified_year)
        return _list_response("stores", company, combined)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing stores: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@bc_custom_extended_router.get("/stores/{record_id}", tags=[_TAG_RETAIL], summary="Get Store by ID (Pag50326)")
def get_store(record_id: str, company: Optional[str] = _COMPANY_Q):
    """Fetch a single LSC Store by SystemId from BC."""
    try:
        return _get_response("stores", record_id, company, "Store")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching store {record_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@bc_custom_extended_router.get("/retail-product-groups", tags=[_TAG_RETAIL], summary="List Retail Product Groups (Pag50335)")
def list_retail_product_groups(
    company: Optional[str] = _COMPANY_Q,
    filter: Optional[str] = _FILTER_Q,
    modified_from: Optional[str] = _MODIFIED_FROM_Q,
    modified_to: Optional[str] = _MODIFIED_TO_Q,
    modified_as_of_date: Optional[str] = _MODIFIED_AS_OF_DATE_Q,
    modified_month: Optional[str] = _MODIFIED_MONTH_Q,
    modified_year: Optional[int] = _MODIFIED_YEAR_Q,
):
    """List all LSC Retail Product Groups (Pag50335, source table: `LSC Retail Product Group` 10000705).

    Fields: `id`, `itemCategoryCode`, `code`, `description`, `companyName`, `lastModifiedDateTime`.

    BC column names: `Item Category Code`, `Code`, `Description`.
    """
    try:
        combined = _combine_filter(filter, modified_from, modified_to, modified_as_of_date, modified_month, modified_year)
        return _list_response("retailProductGroups", company, combined)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing retail product groups: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@bc_custom_extended_router.get("/retail-product-groups/{record_id}", tags=[_TAG_RETAIL], summary="Get Retail Product Group by ID (Pag50335)")
def get_retail_product_group(record_id: str, company: Optional[str] = _COMPANY_Q):
    """Fetch a single LSC Retail Product Group by SystemId from BC."""
    try:
        return _get_response("retailProductGroups", record_id, company, "Retail Product Group")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching retail product group {record_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@bc_custom_extended_router.get("/tender-types", tags=[_TAG_RETAIL], summary="List Tender Types (Pag50336)")
def list_tender_types(
    company: Optional[str] = _COMPANY_Q,
    filter: Optional[str] = _FILTER_Q,
    modified_from: Optional[str] = _MODIFIED_FROM_Q,
    modified_to: Optional[str] = _MODIFIED_TO_Q,
    modified_as_of_date: Optional[str] = _MODIFIED_AS_OF_DATE_Q,
    modified_month: Optional[str] = _MODIFIED_MONTH_Q,
    modified_year: Optional[int] = _MODIFIED_YEAR_Q,
):
    """List all LSC Tender Types (Pag50336, source table: `LSC Tender Type` 99001462).

    Distinct from Tender Type Setups (Pag50325, table 99001466).
    Fields: `id`, `code`, `description`, `companyName`, `lastModifiedDateTime`.

    BC column names: `Code`, `Description`.
    """
    try:
        combined = _combine_filter(filter, modified_from, modified_to, modified_as_of_date, modified_month, modified_year)
        return _list_response("tenderTypes", company, combined)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing tender types: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@bc_custom_extended_router.get("/tender-types/{record_id}", tags=[_TAG_RETAIL], summary="Get Tender Type by ID (Pag50336)")
def get_tender_type(record_id: str, company: Optional[str] = _COMPANY_Q):
    """Fetch a single LSC Tender Type by SystemId from BC."""
    try:
        return _get_response("tenderTypes", record_id, company, "Tender Type")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching tender type {record_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


# ---------------------------------------------------------------------------
# Transfer Orders — Pages 50327–50332
# ---------------------------------------------------------------------------

_TAG_TRANSFER = "BC Custom Extended — Transfer Orders"


@bc_custom_extended_router.get("/transfer-headers", tags=[_TAG_TRANSFER], summary="List Transfer Headers (Pag50327)")
def list_transfer_headers(
    company: Optional[str] = _COMPANY_Q,
    filter: Optional[str] = _FILTER_Q,
    modified_from: Optional[str] = _MODIFIED_FROM_Q,
    modified_to: Optional[str] = _MODIFIED_TO_Q,
    modified_as_of_date: Optional[str] = _MODIFIED_AS_OF_DATE_Q,
    modified_month: Optional[str] = _MODIFIED_MONTH_Q,
    modified_year: Optional[int] = _MODIFIED_YEAR_Q,
):
    """List all Transfer Headers (Pag50327, source table: `Transfer Header` 5740).

    Fields: `id`, `no`, `transferFromCode`, `transferFromName`, `transferToCode`,
    `transferToName`, `inTransitCode`, `postingDate`, `shipmentDate`, `receiptDate`,
    `status`, `directTransfer`, `externalDocumentNo`, `shortcutDimension1Code`,
    `shortcutDimension2Code`, `completelyShipped`, `completelyReceived`,
    `companyName`, `lastModifiedDateTime`.

    BC column names: `No.`, `Transfer-from Code`, `Transfer-from Name`,
    `Transfer-to Code`, `Transfer-to Name`, `In-Transit Code`, `Posting Date`,
    `Shipment Date`, `Receipt Date`, `Status`, `Direct Transfer`,
    `External Document No.`, `Shortcut Dimension 1 Code`, `Shortcut Dimension 2 Code`,
    `Completely Shipped`, `Completely Received`.

    Note: modify and delete are blocked in BC when `status = Released`.
    """
    try:
        combined = _combine_filter(filter, modified_from, modified_to, modified_as_of_date, modified_month, modified_year)
        return _list_response("transferHeaders", company, combined)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing transfer headers: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@bc_custom_extended_router.get("/transfer-headers/{record_id}", tags=[_TAG_TRANSFER], summary="Get Transfer Header by ID (Pag50327)")
def get_transfer_header(record_id: str, company: Optional[str] = _COMPANY_Q):
    """Fetch a single Transfer Header by SystemId from BC."""
    try:
        return _get_response("transferHeaders", record_id, company, "Transfer Header")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching transfer header {record_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@bc_custom_extended_router.get("/transfer-headers/{header_id}/transfer-lines", tags=[_TAG_TRANSFER], summary="List Transfer Lines under a Header (Pag50328 nested)")
def list_transfer_lines_nested(
    header_id: str,
    company: Optional[str] = _COMPANY_Q,
    filter: Optional[str] = _FILTER_Q,
    modified_from: Optional[str] = _MODIFIED_FROM_Q,
    modified_to: Optional[str] = _MODIFIED_TO_Q,
    modified_as_of_date: Optional[str] = _MODIFIED_AS_OF_DATE_Q,
    modified_month: Optional[str] = _MODIFIED_MONTH_Q,
    modified_year: Optional[int] = _MODIFIED_YEAR_Q,
):
    """List Transfer Lines nested under a specific Transfer Header (Pag50328)."""
    try:
        combined = _combine_filter(filter, modified_from, modified_to, modified_as_of_date, modified_month, modified_year)
        return _list_response(f"transferHeaders({header_id})/transferLines", company, combined)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing nested transfer lines for header {header_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@bc_custom_extended_router.get("/transfer-lines", tags=[_TAG_TRANSFER], summary="List Transfer Lines (Pag50328)")
def list_transfer_lines(
    company: Optional[str] = _COMPANY_Q,
    filter: Optional[str] = _FILTER_Q,
    modified_from: Optional[str] = _MODIFIED_FROM_Q,
    modified_to: Optional[str] = _MODIFIED_TO_Q,
    modified_as_of_date: Optional[str] = _MODIFIED_AS_OF_DATE_Q,
    modified_month: Optional[str] = _MODIFIED_MONTH_Q,
    modified_year: Optional[int] = _MODIFIED_YEAR_Q,
):
    """List all Transfer Lines (Pag50328, source table: `Transfer Line` 5741).

    Fields: `id`, `documentNo`, `lineNo`, `itemNo`, `description`, `description2`,
    `variantCode`, `unitOfMeasureCode`, `qtyPerUnitOfMeasure`, `quantity`,
    `qtyToShip`, `quantityShipped`, `qtyToReceive`, `quantityReceived`,
    `outstandingQuantity`, `shipmentDate`, `receiptDate`, `transferFromCode`,
    `transferToCode`, `inTransitCode`, `shortcutDimension1Code`,
    `shortcutDimension2Code`, `companyName`, `lastModifiedDateTime`.

    BC column names: `Document No.`, `Line No.`, `Item No.`, `Description`,
    `Description 2`, `Variant Code`, `Unit of Measure Code`,
    `Qty. per Unit of Measure`, `Quantity`, `Qty. to Ship`, `Quantity Shipped`,
    `Qty. to Receive`, `Quantity Received`, `Outstanding Quantity`,
    `Shipment Date`, `Receipt Date`, `Transfer-from Code`, `Transfer-to Code`,
    `In-Transit Code`, `Shortcut Dimension 1 Code`, `Shortcut Dimension 2 Code`.
    """
    try:
        combined = _combine_filter(filter, modified_from, modified_to, modified_as_of_date, modified_month, modified_year)
        return _list_response("transferLines", company, combined)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing transfer lines: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@bc_custom_extended_router.get("/transfer-lines/{record_id}", tags=[_TAG_TRANSFER], summary="Get Transfer Line by ID (Pag50328)")
def get_transfer_line(record_id: str, company: Optional[str] = _COMPANY_Q):
    """Fetch a single Transfer Line by SystemId from BC."""
    try:
        return _get_response("transferLines", record_id, company, "Transfer Line")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching transfer line {record_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@bc_custom_extended_router.get("/transfer-shipment-headers", tags=[_TAG_TRANSFER], summary="List Transfer Shipment Headers (Pag50329)")
def list_transfer_shipment_headers(
    company: Optional[str] = _COMPANY_Q,
    filter: Optional[str] = _FILTER_Q,
    modified_from: Optional[str] = _MODIFIED_FROM_Q,
    modified_to: Optional[str] = _MODIFIED_TO_Q,
    modified_as_of_date: Optional[str] = _MODIFIED_AS_OF_DATE_Q,
    modified_month: Optional[str] = _MODIFIED_MONTH_Q,
    modified_year: Optional[int] = _MODIFIED_YEAR_Q,
):
    """List all Transfer Shipment Headers (Pag50329, source table: `Transfer Shipment Header` 5744).

    Fields: `id`, `no`, `transferFromCode`, `transferFromName`, `transferToCode`,
    `transferToName`, `inTransitCode`, `postingDate`, `shipmentDate`,
    `transferOrderNo`, `externalDocumentNo`, `shortcutDimension1Code`,
    `shortcutDimension2Code`, `companyName`, `lastModifiedDateTime`.

    BC column names: `No.`, `Transfer-from Code`, `Transfer-from Name`,
    `Transfer-to Code`, `Transfer-to Name`, `In-Transit Code`, `Posting Date`,
    `Shipment Date`, `Transfer Order No.`, `External Document No.`,
    `Shortcut Dimension 1 Code`, `Shortcut Dimension 2 Code`.
    """
    try:
        combined = _combine_filter(filter, modified_from, modified_to, modified_as_of_date, modified_month, modified_year)
        return _list_response("transferShipmentHeaders", company, combined)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing transfer shipment headers: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@bc_custom_extended_router.get("/transfer-shipment-headers/{record_id}", tags=[_TAG_TRANSFER], summary="Get Transfer Shipment Header by ID (Pag50329)")
def get_transfer_shipment_header(record_id: str, company: Optional[str] = _COMPANY_Q):
    """Fetch a single Transfer Shipment Header by SystemId from BC."""
    try:
        return _get_response("transferShipmentHeaders", record_id, company, "Transfer Shipment Header")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching transfer shipment header {record_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@bc_custom_extended_router.get("/transfer-shipment-headers/{header_id}/transfer-shipment-lines", tags=[_TAG_TRANSFER], summary="List Transfer Shipment Lines under a Header (Pag50330 nested)")
def list_transfer_shipment_lines_nested(
    header_id: str,
    company: Optional[str] = _COMPANY_Q,
    filter: Optional[str] = _FILTER_Q,
    modified_from: Optional[str] = _MODIFIED_FROM_Q,
    modified_to: Optional[str] = _MODIFIED_TO_Q,
    modified_as_of_date: Optional[str] = _MODIFIED_AS_OF_DATE_Q,
    modified_month: Optional[str] = _MODIFIED_MONTH_Q,
    modified_year: Optional[int] = _MODIFIED_YEAR_Q,
):
    """List Transfer Shipment Lines nested under a specific Transfer Shipment Header (Pag50330)."""
    try:
        combined = _combine_filter(filter, modified_from, modified_to, modified_as_of_date, modified_month, modified_year)
        return _list_response(f"transferShipmentHeaders({header_id})/transferShipmentLines", company, combined)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing nested transfer shipment lines for header {header_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@bc_custom_extended_router.get("/transfer-shipment-lines", tags=[_TAG_TRANSFER], summary="List Transfer Shipment Lines (Pag50330)")
def list_transfer_shipment_lines(
    company: Optional[str] = _COMPANY_Q,
    filter: Optional[str] = _FILTER_Q,
    modified_from: Optional[str] = _MODIFIED_FROM_Q,
    modified_to: Optional[str] = _MODIFIED_TO_Q,
    modified_as_of_date: Optional[str] = _MODIFIED_AS_OF_DATE_Q,
    modified_month: Optional[str] = _MODIFIED_MONTH_Q,
    modified_year: Optional[int] = _MODIFIED_YEAR_Q,
):
    """List all Transfer Shipment Lines (Pag50330, source table: `Transfer Shipment Line` 5745).

    Fields: `id`, `documentNo`, `lineNo`, `itemNo`, `description`, `description2`,
    `variantCode`, `unitOfMeasureCode`, `qtyPerUnitOfMeasure`, `quantity`,
    `transferOrderNo`, `transferFromCode`, `transferToCode`, `shortcutDimension1Code`,
    `shortcutDimension2Code`, `companyName`, `lastModifiedDateTime`.

    BC column names: `Document No.`, `Line No.`, `Item No.`, `Description`,
    `Description 2`, `Variant Code`, `Unit of Measure Code`,
    `Qty. per Unit of Measure`, `Quantity`, `Transfer Order No.`,
    `Transfer-from Code`, `Transfer-to Code`, `Shortcut Dimension 1 Code`,
    `Shortcut Dimension 2 Code`.
    """
    try:
        combined = _combine_filter(filter, modified_from, modified_to, modified_as_of_date, modified_month, modified_year)
        return _list_response("transferShipmentLines", company, combined)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing transfer shipment lines: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@bc_custom_extended_router.get("/transfer-shipment-lines/{record_id}", tags=[_TAG_TRANSFER], summary="Get Transfer Shipment Line by ID (Pag50330)")
def get_transfer_shipment_line(record_id: str, company: Optional[str] = _COMPANY_Q):
    """Fetch a single Transfer Shipment Line by SystemId from BC."""
    try:
        return _get_response("transferShipmentLines", record_id, company, "Transfer Shipment Line")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching transfer shipment line {record_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@bc_custom_extended_router.get("/transfer-receipt-headers", tags=[_TAG_TRANSFER], summary="List Transfer Receipt Headers (Pag50331)")
def list_transfer_receipt_headers(
    company: Optional[str] = _COMPANY_Q,
    filter: Optional[str] = _FILTER_Q,
    modified_from: Optional[str] = _MODIFIED_FROM_Q,
    modified_to: Optional[str] = _MODIFIED_TO_Q,
    modified_as_of_date: Optional[str] = _MODIFIED_AS_OF_DATE_Q,
    modified_month: Optional[str] = _MODIFIED_MONTH_Q,
    modified_year: Optional[int] = _MODIFIED_YEAR_Q,
):
    """List all Transfer Receipt Headers (Pag50331, source table: `Transfer Receipt Header` 5746).

    Fields: `id`, `no`, `transferFromCode`, `transferFromName`, `transferToCode`,
    `transferToName`, `inTransitCode`, `postingDate`, `transferOrderNo`,
    `externalDocumentNo`, `shortcutDimension1Code`, `shortcutDimension2Code`,
    `companyName`, `lastModifiedDateTime`.

    BC column names: `No.`, `Transfer-from Code`, `Transfer-from Name`,
    `Transfer-to Code`, `Transfer-to Name`, `In-Transit Code`, `Posting Date`,
    `Transfer Order No.`, `External Document No.`, `Shortcut Dimension 1 Code`,
    `Shortcut Dimension 2 Code`.
    """
    try:
        combined = _combine_filter(filter, modified_from, modified_to, modified_as_of_date, modified_month, modified_year)
        return _list_response("transferReceiptHeaders", company, combined)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing transfer receipt headers: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@bc_custom_extended_router.get("/transfer-receipt-headers/{record_id}", tags=[_TAG_TRANSFER], summary="Get Transfer Receipt Header by ID (Pag50331)")
def get_transfer_receipt_header(record_id: str, company: Optional[str] = _COMPANY_Q):
    """Fetch a single Transfer Receipt Header by SystemId from BC."""
    try:
        return _get_response("transferReceiptHeaders", record_id, company, "Transfer Receipt Header")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching transfer receipt header {record_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@bc_custom_extended_router.get("/transfer-receipt-headers/{header_id}/transfer-receipt-lines", tags=[_TAG_TRANSFER], summary="List Transfer Receipt Lines under a Header (Pag50332 nested)")
def list_transfer_receipt_lines_nested(
    header_id: str,
    company: Optional[str] = _COMPANY_Q,
    filter: Optional[str] = _FILTER_Q,
    modified_from: Optional[str] = _MODIFIED_FROM_Q,
    modified_to: Optional[str] = _MODIFIED_TO_Q,
    modified_as_of_date: Optional[str] = _MODIFIED_AS_OF_DATE_Q,
    modified_month: Optional[str] = _MODIFIED_MONTH_Q,
    modified_year: Optional[int] = _MODIFIED_YEAR_Q,
):
    """List Transfer Receipt Lines nested under a specific Transfer Receipt Header (Pag50332)."""
    try:
        combined = _combine_filter(filter, modified_from, modified_to, modified_as_of_date, modified_month, modified_year)
        return _list_response(f"transferReceiptHeaders({header_id})/transferReceiptLines", company, combined)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing nested transfer receipt lines for header {header_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@bc_custom_extended_router.get("/transfer-receipt-lines", tags=[_TAG_TRANSFER], summary="List Transfer Receipt Lines (Pag50332)")
def list_transfer_receipt_lines(
    company: Optional[str] = _COMPANY_Q,
    filter: Optional[str] = _FILTER_Q,
    modified_from: Optional[str] = _MODIFIED_FROM_Q,
    modified_to: Optional[str] = _MODIFIED_TO_Q,
    modified_as_of_date: Optional[str] = _MODIFIED_AS_OF_DATE_Q,
    modified_month: Optional[str] = _MODIFIED_MONTH_Q,
    modified_year: Optional[int] = _MODIFIED_YEAR_Q,
):
    """List all Transfer Receipt Lines (Pag50332, source table: `Transfer Receipt Line` 5747).

    Fields: `id`, `documentNo`, `lineNo`, `itemNo`, `description`, `description2`,
    `variantCode`, `unitOfMeasureCode`, `qtyPerUnitOfMeasure`, `quantity`,
    `transferOrderNo`, `transferFromCode`, `transferToCode`, `shortcutDimension1Code`,
    `shortcutDimension2Code`, `companyName`, `lastModifiedDateTime`.

    BC column names: `Document No.`, `Line No.`, `Item No.`, `Description`,
    `Description 2`, `Variant Code`, `Unit of Measure Code`,
    `Qty. per Unit of Measure`, `Quantity`, `Transfer Order No.`,
    `Transfer-from Code`, `Transfer-to Code`, `Shortcut Dimension 1 Code`,
    `Shortcut Dimension 2 Code`.
    """
    try:
        combined = _combine_filter(filter, modified_from, modified_to, modified_as_of_date, modified_month, modified_year)
        return _list_response("transferReceiptLines", company, combined)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing transfer receipt lines: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@bc_custom_extended_router.get("/transfer-receipt-lines/{record_id}", tags=[_TAG_TRANSFER], summary="Get Transfer Receipt Line by ID (Pag50332)")
def get_transfer_receipt_line(record_id: str, company: Optional[str] = _COMPANY_Q):
    """Fetch a single Transfer Receipt Line by SystemId from BC."""
    try:
        return _get_response("transferReceiptLines", record_id, company, "Transfer Receipt Line")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching transfer receipt line {record_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


# ---------------------------------------------------------------------------
# Sales Archives — Pages 50333–50334
# ---------------------------------------------------------------------------

_TAG_ARCHIVE = "BC Custom Extended — Sales Archives"


@bc_custom_extended_router.get("/sales-header-archives", tags=[_TAG_ARCHIVE], summary="List Sales Header Archives (Pag50333)")
def list_sales_header_archives(
    company: Optional[str] = _COMPANY_Q,
    filter: Optional[str] = _FILTER_Q,
    modified_from: Optional[str] = _MODIFIED_FROM_Q,
    modified_to: Optional[str] = _MODIFIED_TO_Q,
    modified_as_of_date: Optional[str] = _MODIFIED_AS_OF_DATE_Q,
    modified_month: Optional[str] = _MODIFIED_MONTH_Q,
    modified_year: Optional[int] = _MODIFIED_YEAR_Q,
):
    """List all Sales Header Archives (Pag50333, source table: `Sales Header Archive` 5107).

    Fields: `id`, `documentType`, `no`, `docNoOccurrence`, `versionNo`,
    `sellToCustomerNo`, `sellToCustomerName`, `sellToContactNo`, `postingDate`,
    `documentDate`, `orderDate`, `status`, `salespersonCode`, `currencyCode`,
    `externalDocumentNo`, `locationCode`, `shortcutDimension1Code`,
    `shortcutDimension2Code`, `archivedBy`, `dateArchived`, `timeArchived`,
    `companyName`, `lastModifiedDateTime`.

    BC column names: `Document Type`, `No.`, `Doc. No. Occurrence`, `Version No.`,
    `Sell-to Customer No.`, `Sell-to Customer Name`, `Sell-to Contact No.`,
    `Posting Date`, `Document Date`, `Order Date`, `Status`, `Salesperson Code`,
    `Currency Code`, `External Document No.`, `Location Code`,
    `Shortcut Dimension 1 Code`, `Shortcut Dimension 2 Code`,
    `Archived By`, `Date Archived`, `Time Archived`.
    """
    try:
        combined = _combine_filter(filter, modified_from, modified_to, modified_as_of_date, modified_month, modified_year)
        return _list_response("salesHeaderArchives", company, combined)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing sales header archives: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@bc_custom_extended_router.get("/sales-header-archives/{record_id}", tags=[_TAG_ARCHIVE], summary="Get Sales Header Archive by ID (Pag50333)")
def get_sales_header_archive(record_id: str, company: Optional[str] = _COMPANY_Q):
    """Fetch a single Sales Header Archive by SystemId from BC."""
    try:
        return _get_response("salesHeaderArchives", record_id, company, "Sales Header Archive")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching sales header archive {record_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@bc_custom_extended_router.get("/sales-header-archives/{header_id}/sales-line-archives", tags=[_TAG_ARCHIVE], summary="List Sales Line Archives under a Header (Pag50334 nested)")
def list_sales_line_archives_nested(
    header_id: str,
    company: Optional[str] = _COMPANY_Q,
    filter: Optional[str] = _FILTER_Q,
    modified_from: Optional[str] = _MODIFIED_FROM_Q,
    modified_to: Optional[str] = _MODIFIED_TO_Q,
    modified_as_of_date: Optional[str] = _MODIFIED_AS_OF_DATE_Q,
    modified_month: Optional[str] = _MODIFIED_MONTH_Q,
    modified_year: Optional[int] = _MODIFIED_YEAR_Q,
):
    """List Sales Line Archives nested under a specific Sales Header Archive (Pag50334)."""
    try:
        combined = _combine_filter(filter, modified_from, modified_to, modified_as_of_date, modified_month, modified_year)
        return _list_response(f"salesHeaderArchives({header_id})/salesLineArchives", company, combined)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing nested sales line archives for header {header_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@bc_custom_extended_router.get("/sales-line-archives", tags=[_TAG_ARCHIVE], summary="List Sales Line Archives (Pag50334)")
def list_sales_line_archives(
    company: Optional[str] = _COMPANY_Q,
    filter: Optional[str] = _FILTER_Q,
    modified_from: Optional[str] = _MODIFIED_FROM_Q,
    modified_to: Optional[str] = _MODIFIED_TO_Q,
    modified_as_of_date: Optional[str] = _MODIFIED_AS_OF_DATE_Q,
    modified_month: Optional[str] = _MODIFIED_MONTH_Q,
    modified_year: Optional[int] = _MODIFIED_YEAR_Q,
):
    """List all Sales Line Archives (Pag50334, source table: `Sales Line Archive` 5108).

    Fields: `id`, `documentType`, `documentNo`, `docNoOccurrence`, `versionNo`,
    `lineNo`, `lineType`, `no`, `description`, `description2`, `variantCode`,
    `locationCode`, `unitOfMeasureCode`, `quantity`, `unitPrice`,
    `lineDiscountPercent`, `lineAmount`, `amount`, `amountIncludingVat`,
    `shipmentDate`, `shortcutDimension1Code`, `shortcutDimension2Code`,
    `companyName`, `lastModifiedDateTime`.

    BC column names: `Document Type`, `Document No.`, `Doc. No. Occurrence`,
    `Version No.`, `Line No.`, `Type`, `No.`, `Description`, `Description 2`,
    `Variant Code`, `Location Code`, `Unit of Measure Code`, `Quantity`,
    `Unit Price`, `Line Discount %`, `Line Amount`, `Amount`,
    `Amount Including VAT`, `Shipment Date`, `Shortcut Dimension 1 Code`,
    `Shortcut Dimension 2 Code`.
    """
    try:
        combined = _combine_filter(filter, modified_from, modified_to, modified_as_of_date, modified_month, modified_year)
        return _list_response("salesLineArchives", company, combined)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing sales line archives: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@bc_custom_extended_router.get("/sales-line-archives/{record_id}", tags=[_TAG_ARCHIVE], summary="Get Sales Line Archive by ID (Pag50334)")
def get_sales_line_archive(record_id: str, company: Optional[str] = _COMPANY_Q):
    """Fetch a single Sales Line Archive by SystemId from BC."""
    try:
        return _get_response("salesLineArchives", record_id, company, "Sales Line Archive")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching sales line archive {record_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


# ---------------------------------------------------------------------------
# Returns — Pages 50337–50338
# ---------------------------------------------------------------------------

_TAG_RETURNS = "BC Custom Extended — Returns"


@bc_custom_extended_router.get("/return-shipment-lines", tags=[_TAG_RETURNS], summary="List Return Shipment Lines (Pag50337)")
def list_return_shipment_lines(
    company: Optional[str] = _COMPANY_Q,
    filter: Optional[str] = _FILTER_Q,
    modified_from: Optional[str] = _MODIFIED_FROM_Q,
    modified_to: Optional[str] = _MODIFIED_TO_Q,
    modified_as_of_date: Optional[str] = _MODIFIED_AS_OF_DATE_Q,
    modified_month: Optional[str] = _MODIFIED_MONTH_Q,
    modified_year: Optional[int] = _MODIFIED_YEAR_Q,
):
    """List all Return Shipment Lines (Pag50337, source table: `Return Shipment Line` 6651).

    Fields: `id`, `documentNo`, `lineNo`, `type`, `no`, `description`, `description2`,
    `variantCode`, `unitOfMeasureCode`, `qtyPerUnitOfMeasure`, `quantity`,
    `returnReasonCode`, `returnOrderNo`, `returnOrderLineNo`,
    `shortcutDimension1Code`, `shortcutDimension2Code`, `companyName`, `lastModifiedDateTime`.

    BC column names: `Document No.`, `Line No.`, `Type`, `No.`, `Description`,
    `Description 2`, `Variant Code`, `Unit of Measure Code`,
    `Qty. per Unit of Measure`, `Quantity`, `Return Reason Code`,
    `Return Order No.`, `Return Order Line No.`, `Shortcut Dimension 1 Code`,
    `Shortcut Dimension 2 Code`.

    Note: `unitPrice`, `amount`, `amountIncludingVat` do not exist on
    `Return Shipment Line` (table 6651) in this BC27 installation.
    """
    try:
        combined = _combine_filter(filter, modified_from, modified_to, modified_as_of_date, modified_month, modified_year)
        return _list_response("returnShipmentLines", company, combined)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing return shipment lines: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@bc_custom_extended_router.get("/return-shipment-lines/{record_id}", tags=[_TAG_RETURNS], summary="Get Return Shipment Line by ID (Pag50337)")
def get_return_shipment_line(record_id: str, company: Optional[str] = _COMPANY_Q):
    """Fetch a single Return Shipment Line by SystemId from BC."""
    try:
        return _get_response("returnShipmentLines", record_id, company, "Return Shipment Line")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching return shipment line {record_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@bc_custom_extended_router.get("/return-receipt-lines", tags=[_TAG_RETURNS], summary="List Return Receipt Lines (Pag50338)")
def list_return_receipt_lines(
    company: Optional[str] = _COMPANY_Q,
    filter: Optional[str] = _FILTER_Q,
    modified_from: Optional[str] = _MODIFIED_FROM_Q,
    modified_to: Optional[str] = _MODIFIED_TO_Q,
    modified_as_of_date: Optional[str] = _MODIFIED_AS_OF_DATE_Q,
    modified_month: Optional[str] = _MODIFIED_MONTH_Q,
    modified_year: Optional[int] = _MODIFIED_YEAR_Q,
):
    """List all Return Receipt Lines (Pag50338, source table: `Return Receipt Line` 6661).

    Fields: `id`, `documentNo`, `lineNo`, `type`, `no`, `description`, `description2`,
    `variantCode`, `unitOfMeasureCode`, `qtyPerUnitOfMeasure`, `quantity`, `unitPrice`,
    `returnReasonCode`, `returnOrderNo`, `returnOrderLineNo`,
    `shortcutDimension1Code`, `shortcutDimension2Code`, `companyName`, `lastModifiedDateTime`.

    BC column names: `Document No.`, `Line No.`, `Type`, `No.`, `Description`,
    `Description 2`, `Variant Code`, `Unit of Measure Code`,
    `Qty. per Unit of Measure`, `Quantity`, `Unit Price`, `Return Reason Code`,
    `Return Order No.`, `Return Order Line No.`, `Shortcut Dimension 1 Code`,
    `Shortcut Dimension 2 Code`.

    Note: `amount` and `amountIncludingVat` do not exist on `Return Receipt Line`
    (table 6661) in this BC27 installation.
    """
    try:
        combined = _combine_filter(filter, modified_from, modified_to, modified_as_of_date, modified_month, modified_year)
        return _list_response("returnReceiptLines", company, combined)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing return receipt lines: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@bc_custom_extended_router.get("/return-receipt-lines/{record_id}", tags=[_TAG_RETURNS], summary="Get Return Receipt Line by ID (Pag50338)")
def get_return_receipt_line(record_id: str, company: Optional[str] = _COMPANY_Q):
    """Fetch a single Return Receipt Line by SystemId from BC."""
    try:
        return _get_response("returnReceiptLines", record_id, company, "Return Receipt Line")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching return receipt line {record_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


# ---------------------------------------------------------------------------
# Inventory — Page 50339
# ---------------------------------------------------------------------------

_TAG_INVENTORY = "BC Custom Extended — Inventory"


@bc_custom_extended_router.get("/item-ledger-entries", tags=[_TAG_INVENTORY], summary="List Item Ledger Entries from Firestore (Pag50339)")
def list_item_ledger_entries(
    company: Optional[str] = _COMPANY_Q,
    item_no: Optional[str] = Query(None, description="Filter by itemNo (exact match)."),
    entry_type: Optional[str] = Query(None, description="Filter by entryType (e.g. Sale, Purchase, Transfer, Positive Adjmt., Negative Adjmt.)."),
    location_code: Optional[str] = Query(None, description="Filter by locationCode (exact match)."),
    modified_from: Optional[str] = Query(None, description="lastModifiedDateTime on or after this ISO 8601 datetime (e.g. 2024-01-01T00:00:00Z). Pass 'NOW' to use start of today (UTC)."),
    modified_to: Optional[str] = Query(None, description="lastModifiedDateTime on or before this ISO 8601 datetime."),
    limit: Optional[int] = Query(None, ge=1, description="Maximum number of records to return. Omit for all matching records."),
    offset: Optional[int] = Query(None, ge=0, description="Number of matching records to skip before returning results (for pagination)."),
):
    """List Item Ledger Entries from the Firestore cache (Pag50339, source table: `Item Ledger Entry` 32).

    Reads pre-synced Firestore data — does **not** call Business Central directly.
    Use `POST /internal/firestore/sync-item-ledger-entries` to populate or refresh the cache,
    or `POST /internal/firestore/routine-sync` to include it in the scheduled sync.

    Filters (item_no, entry_type, location_code, modified_from/to) are applied first,
    then offset and limit are applied to the filtered result set. `total` in the response
    is the count of all matching records before pagination, so clients can compute page counts.

    For single-record lookup by SystemId use `GET /bc/custom/v2/item-ledger-entries/{record_id}`
    (that endpoint still fetches live from BC).

    **API property → BC column name differences:**
    - `intrastatArea` → BC column **`Area`** — renamed because `area` is a reserved AL keyword

    Extended by `RGMC Item Ledger Entry Ext` (TableExt 50456):
    `transferType`, `batchNo`, `offerNo`, `promotionNo`, `statementNo`, `biTimestamp`
    (blank until populated via POST/PATCH to BC).
    """
    try:
        if modified_from and modified_from.upper() == "NOW":
            modified_from = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
        if company.upper() == "ALL":
            http_status, companies_data = get_all_companies_cached()
            if http_status != 200:
                raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to fetch BC companies")
            all_results = []
            for c in companies_data.get("value", []):
                # Fetch without pagination per-company; paginate the merged set below.
                page, _ = get_item_ledger_entries_from_firestore(
                    company=c["name"],
                    item_no=item_no,
                    entry_type=entry_type,
                    location_code=location_code,
                    modified_from=modified_from,
                    modified_to=modified_to,
                )
                all_results.extend(page)
            total = len(all_results)
            start = offset or 0
            data = all_results[start : start + limit] if limit is not None else all_results[start:]
            return {"data": data, "total": total, "limit": limit, "offset": offset, "env": config.GCP_ENV}

        page, total = get_item_ledger_entries_from_firestore(
            company=company,
            item_no=item_no,
            entry_type=entry_type,
            location_code=location_code,
            modified_from=modified_from,
            modified_to=modified_to,
            limit=limit,
            offset=offset,
        )
        return {"data": page, "total": total, "limit": limit, "offset": offset, "company": company, "env": config.GCP_ENV}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing item ledger entries from Firestore: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@bc_custom_extended_router.get("/item-ledger-entries/{record_id}", tags=[_TAG_INVENTORY], summary="Get Item Ledger Entry by ID (Pag50339)")
def get_item_ledger_entry(record_id: str, company: Optional[str] = _COMPANY_Q):
    """Fetch a single Item Ledger Entry by SystemId from BC.

    Note: `intrastatArea` maps to the BC column **`Area`** — renamed because `area`
    is a reserved keyword in AL (used in page layouts as `area(Content)`).
    """
    try:
        return _get_response("itemLedgerEntries", record_id, company, "Item Ledger Entry")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching item ledger entry {record_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


# ---------------------------------------------------------------------------
# Sales Shipments — Page 50340
# ---------------------------------------------------------------------------

_TAG_SHIPMENTS = "BC Custom Extended — Sales Shipments"


@bc_custom_extended_router.get("/sales-shipment-lines", tags=[_TAG_SHIPMENTS], summary="List Sales Shipment Lines (Pag50340)")
def list_sales_shipment_lines(
    company: Optional[str] = _COMPANY_Q,
    filter: Optional[str] = _FILTER_Q,
    modified_from: Optional[str] = _MODIFIED_FROM_Q,
    modified_to: Optional[str] = _MODIFIED_TO_Q,
    modified_as_of_date: Optional[str] = _MODIFIED_AS_OF_DATE_Q,
    modified_month: Optional[str] = _MODIFIED_MONTH_Q,
    modified_year: Optional[int] = _MODIFIED_YEAR_Q,
    limit: Optional[int] = Query(None, ge=1, description="Maximum records to return per company. When set, the AL page loads only the execution company; omit for all records across all companies."),
    offset: Optional[int] = Query(None, ge=0, description="Records to skip before returning results. Only meaningful when limit is also set."),
):
    """List all Sales Shipment Lines (Pag50340, source table: `Sales Shipment Line` 111).

    **Identity & document:** `id`, `documentNo`, `lineNo`, `type`, `no`, `description`,
    `description2`, `postingDate`, `correction`.

    **Customer:** `sellToCustomerNo`, `billToCustomerNo`, `customerPriceGroup`,
    `customerDiscGroup`, `responsibilityCenter`.

    **Item & location:** `locationCode`, `binCode`, `variantCode`, `itemCategoryCode`,
    `productGroupCode`, `nonstock`, `postingGroup`, `purchasingCode`.

    **Quantities & UoM:** `quantity`, `quantityBase`, `quantityInvoiced`, `qtyInvoicedBase`,
    `qtyShippedNotInvoiced`, `qtyPerUnitOfMeasure`, `unitOfMeasure`, `unitOfMeasureCode`,
    `unitOfMeasureCrossRef`, `unitsPerParcel`, `netWeight`, `grossWeight`, `unitVolume`.

    **Pricing & discounts:** `unitPrice`, `unitCost`, `unitCostLcy`, `vatPercent`,
    `vatCalculationType`, `vatBusinessPostingGroup`, `vatProductPostingGroup`,
    `generalBusinessPostingGroup`, `generalProductPostingGroup`,
    `taxLiable`, `taxAreaCode`, `taxGroupCode`, `vatBaseAmount`,
    `lineDiscountPercent`, `allowLineDisc`, `allowInvoiceDisc`, `itemChargeBaseAmount`.

    **Item application:** `itemShipmentEntryNo`, `appliesToItemEntry`, `appliesFromItemEntry`,
    `attachedToLineNo`, `returnReasonCode`.

    **Order references:** `orderNo`, `orderLineNo`, `blanketOrderNo`, `blanketOrderLineNo`,
    `purchaseOrderNo`, `purchOrderLineNo`, `dropShipment`, `vendorNo`.

    **Job:** `jobNo`, `jobTaskNo`, `jobContractEntryNo`, `workTypeCode`.

    **Dimensions:** `shortcutDimension1Code`, `shortcutDimension2Code`, `dimensionSetId`.

    **FA / Depreciation:** `faPostingDate`, `depreciationBookCode`, `deprUntilFaPostingDate`,
    `duplicateInDepreciationBook`, `useDuplicationList`.

    **Intrastat:** `transactionType`, `transportMethod`, `transactionSpecification`,
    `exitPoint`, `intrastatArea`.

    **Dates & shipping:** `shippingTime`, `outboundWarehouseHandlingTime`,
    `plannedShipmentDate`, `plannedDeliveryDate`, `requestedDeliveryDate`,
    `promisedDeliveryDate`, `estimatedDeliveryDate`.

    **LSC retail / delivery:** `sourcing`, `deliverFrom`, `returnPolicy`, `deliveringMethod`,
    `itemTrackingNo`, `configurationId`, `deliveryUserId`, `optionValueText`,
    `spoWhseLocation`, `deliveryDateTime`, `noLaterThanDate`, `vendorDeliversTo`,
    `spoDocumentMethod`, `retailSpecialOrder`, `storeSalesLocation`,
    `deliveryReferenceNo`, `deliveryLocationCode`, `authorizedForCreditCard`.

    **Metadata:** `companyName`, `lastModifiedDateTime`.

    When `limit` is provided the AL page (Pag50340) restricts its load to the
    execution company only (`CompanyName()`), enabling reliable per-company
    pagination. Combine with `offset` to walk pages: if the returned `data`
    array is shorter than `limit`, there are no more records for that company.
    """
    try:
        combined = _combine_filter(filter, modified_from, modified_to, modified_as_of_date, modified_month, modified_year)
        pagination_parts: List[str] = []
        if limit is not None:
            pagination_parts.append(f"limit eq {limit}")
        if offset is not None and offset > 0:
            pagination_parts.append(f"offset eq {offset}")
        if pagination_parts:
            pagination_str = " and ".join(pagination_parts)
            combined = f"{combined} and {pagination_str}" if combined else pagination_str
        data = _list("salesShipmentLines", company, combined)
        if limit is not None or offset is not None:
            return {"data": data, "total": len(data), "limit": limit, "offset": offset or 0}
        return {"data": data}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing sales shipment lines: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@bc_custom_extended_router.get("/sales-shipment-lines/{record_id}", tags=[_TAG_SHIPMENTS], summary="Get Sales Shipment Line by ID (Pag50340)")
def get_sales_shipment_line(record_id: str, company: Optional[str] = _COMPANY_Q):
    """Fetch a single Sales Shipment Line by SystemId from BC."""
    try:
        return _get_response("salesShipmentLines", record_id, company, "Sales Shipment Line")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching sales shipment line {record_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


# ---------------------------------------------------------------------------
# Sales Prices — Page 50341
# ---------------------------------------------------------------------------

_TAG_PRICES = "BC Custom Extended — Sales Prices"


def _sales_price_filter(
    raw_filter: Optional[str],
    modified_from: Optional[str],
    modified_to: Optional[str],
    modified_as_of_date: Optional[str],
    modified_month: Optional[str],
    modified_year: Optional[int],
    limit: Optional[int],
    offset: Optional[int],
) -> Optional[str]:
    """Build the OData $filter string for the Sales Price AL page (Pag50341).

    Unlike other endpoints, the AL page reads Date-typed custom fields
    (RGMC Modified From/To/etc.) via GetFilter rather than filtering on
    SystemModifiedAt directly, so we pass date strings and integers
    instead of datetime ISO strings.
    """
    parts: List[str] = []
    if raw_filter:
        parts.append(raw_filter)
    if modified_from and modified_from.upper() == "NOW":
        modified_from = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if modified_from:
        parts.append(f"modifiedFrom eq {modified_from.split('T')[0]}")
    if modified_to:
        parts.append(f"modifiedTo eq {modified_to.split('T')[0]}")
    if modified_as_of_date:
        try:
            date.fromisoformat(modified_as_of_date)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid modified_as_of_date '{modified_as_of_date}'. Use YYYY-MM-DD.",
            )
        parts.append(f"modifiedAsOfDate eq {modified_as_of_date}")
    if modified_month:
        try:
            year, month = map(int, modified_month.split("-"))
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid modified_month '{modified_month}'. Use YYYY-MM.",
            )
        parts.append(f"modifiedYear eq {year}")
        parts.append(f"modifiedMonth eq {month}")
    if modified_year is not None:
        parts.append(f"modifiedYear eq {modified_year}")
    if limit is not None:
        parts.append(f"limit eq {limit}")
    if offset is not None and offset > 0:
        parts.append(f"offset eq {offset}")
    return " and ".join(parts) if parts else None


@bc_custom_extended_router.get("/sales-prices", tags=[_TAG_PRICES], summary="List Sales Price List Lines (Pag50341)")
def list_sales_prices(
    company: Optional[str] = _COMPANY_Q,
    filter: Optional[str] = _FILTER_Q,
    modified_from: Optional[str] = _MODIFIED_FROM_Q,
    modified_to: Optional[str] = _MODIFIED_TO_Q,
    modified_as_of_date: Optional[str] = _MODIFIED_AS_OF_DATE_Q,
    modified_month: Optional[str] = _MODIFIED_MONTH_Q,
    modified_year: Optional[int] = _MODIFIED_YEAR_Q,
    limit: Optional[int] = Query(None, ge=1, description="Maximum records to return per company. When set, the AL page loads only the execution company; omit for all records across all companies."),
    offset: Optional[int] = Query(None, ge=0, description="Records to skip before returning results. Only meaningful when limit is also set."),
):
    """List all Sales Price List Lines (Pag50341, source table: `Price List Line` 7023).

    **Identity:** `id`, `priceListCode`, `lineNo`.

    **Price type & assignment:** `status`, `priceType`, `assignToNo`.

    **Asset:** `assetType`, `assetNo`, `variantCode`, `unitOfMeasureCode`.

    **Dates:** `startingDate`, `endingDate`.

    **Pricing:** `currencyCode`, `minimumQuantity`, `amountType`, `unitPrice`,
    `unitPriceIncVat`, `lineDiscountPercent`, `allowLineDisc`, `allowInvoiceDisc`,
    `description`.

    **Metadata:** `companyName`, `lastModifiedDateTime`.

    **Date filters** (`modified_from`, `modified_to`, `modified_as_of_date`, `modified_month`,
    `modified_year`) are forwarded to BC as Date-typed OData filter fields on the AL page.
    Accept YYYY-MM-DD or ISO datetime strings (the date part is extracted automatically).
    Pass `modified_from=NOW` to use the start of today (UTC).

    When `limit` is provided the AL page restricts its load to the execution company only
    (`CompanyName()`), enabling reliable per-company pagination. Combine with `offset` to
    walk pages; if the returned `data` array is shorter than `limit`, there are no more
    records for that company.
    """
    try:
        odata_filter = _sales_price_filter(
            filter, modified_from, modified_to, modified_as_of_date,
            modified_month, modified_year, limit, offset,
        )
        data = _list("salesPrices", company, odata_filter)
        if limit is not None or offset is not None:
            return {"data": data, "total": len(data), "limit": limit, "offset": offset or 0}
        return {"data": data}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing sales prices: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@bc_custom_extended_router.get("/sales-prices/{record_id}", tags=[_TAG_PRICES], summary="Get Sales Price List Line by ID (Pag50341)")
def get_sales_price(record_id: str, company: Optional[str] = _COMPANY_Q):
    """Fetch a single Sales Price List Line by SystemId from BC."""
    try:
        return _get_response("salesPrices", record_id, company, "Sales Price")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching sales price {record_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
