"""Custom Connector API — Detailed Customer Ledger Entry read endpoints (Pag51025).

Same separate AL app as custom_connector_sales_invoice_header_routes.py (github.com/Aaron-Alvarez-RGMC/
custom-connector-AL), own APIPublisher/APIGroup/object ID range - not part of the shared rgmc/
rgmccustom namespace.

Endpoints:
  GET /bc/custom-connector/detailed-cust-ledger-entries         — list
  GET /bc/custom-connector/detailed-cust-ledger-entries/{id}    — get single record by SystemId

Fields: id, entryNo, custLedgerEntryNo, entryType, documentType, documentNo, transactionNo, applicationNo, customerNo, postingDate, initialEntryDueDate, initialDocumentType, postingGroup, currencyCode, amount, amountLcy, debitAmount, creditAmount, debitAmountLcy, creditAmountLcy, ledgerEntryAmount, appliedCustLedgerEntryNo, unapplied, unappliedByEntryNo, journalBatchName, sourceCode, reasonCode, userId, companyName, lastModifiedDateTime.
The movements behind each customer ledger entry (Detailed Cust. Ledg. Entry, table 379): initial entry,
applications, discounts, rounding, unapplications. Rows are only inserted (an unapply adds new rows and flags the
old ones), so the incremental sync is complete. Natural key: entryNo. Join to customer-ledger-entries on
custLedgerEntryNo = entryNo. The balance of a ledger entry on any date is the sum of its amounts up to that date.
"""
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, status

from src import config
from src.services.bc_functions import (
    call_custom_connector_table,
    custom_connector_get_record,
)

logger = logging.getLogger("bc_routes.custom_connector_detailed_cust_ledger_entry")

custom_connector_detailed_cust_ledger_entry_router = APIRouter(
    prefix="/bc/custom-connector/detailed-cust-ledger-entries",
    tags=["Custom Connector — Detailed Customer Ledger Entry"],
)

_TABLE = "detailedCustLedgerEntries"


def _unwrap_list(http_status: int, data: Any) -> List[Dict[str, Any]]:
    if http_status != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Business Central returned {http_status}: {data}",
        )
    return data.get("value", data)


@custom_connector_detailed_cust_ledger_entry_router.get("", summary="List Detailed Customer Ledger Entries")
def list_detailed_cust_ledger_entries(
    filter: Optional[str] = Query(None, description="OData $filter expression (e.g. custLedgerEntryNo eq 1234)"),
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
    modified_from: Optional[str] = Query(
        None,
        description="Only records with lastModifiedDateTime on/after this ISO 8601 datetime (e.g. from Airbyte's incremental cursor). Combined with `filter` via AND if both are given.",
    ),
    limit: Optional[int] = Query(None, ge=1, le=5000, description="Max records to return in this page (BC $top). Omit for all matching records."),
    offset: Optional[int] = Query(None, ge=0, description="Records to skip before this page (BC $skip)."),
):
    """Return Detailed Customer Ledger Entries records from BC (Pag51025). Unfiltered, unpaginated requests are cached for 5 minutes."""
    try:
        company_name = company or config.BC_COMPANY
        odata_filter = filter
        if modified_from:
            clause = f"lastModifiedDateTime ge {modified_from}"
            odata_filter = f"({odata_filter}) and {clause}" if odata_filter else clause
        http_status, data = call_custom_connector_table(
            _TABLE, company_name=company_name, odata_filter=odata_filter, top=limit, skip=offset
        )
        return {"data": _unwrap_list(http_status, data)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing detailed customer ledger entries: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@custom_connector_detailed_cust_ledger_entry_router.get("/{record_id}", summary="Get Detailed Customer Ledger Entry by ID")
def get_detailed_cust_ledger_entry(
    record_id: str,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    """Fetch a single Detailed Customer Ledger Entry record by SystemId from BC."""
    try:
        http_status, data = custom_connector_get_record(
            table_endpoint=_TABLE,
            record_id=record_id,
            company_name=company or config.BC_COMPANY,
        )
        if http_status == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Detailed customer ledger entry record '{record_id}' not found",
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
        logger.error(f"Error fetching detailed customer ledger entry {record_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
