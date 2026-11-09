"""Custom Connector API — Bank Account Ledger Entry read endpoints (Pag51028).

Same separate AL app as custom_connector_sales_invoice_header_routes.py (github.com/Aaron-Alvarez-RGMC/
custom-connector-AL), own APIPublisher/APIGroup/object ID range - not part of the shared rgmc/
rgmccustom namespace.

Endpoints:
  GET /bc/custom-connector/bank-account-ledger-entries         — list
  GET /bc/custom-connector/bank-account-ledger-entries/{id}    — get single record by SystemId

Fields: id, entryNo, transactionNo, documentType, documentNo, externalDocumentNo, description, bankAccountNo, bankAccPostingGroup, postingDate, documentDate, currencyCode, amount, amountLcy, remainingAmount, debitAmount, creditAmount, debitAmountLcy, creditAmountLcy, open, statementStatus, statementNo, statementLineNo, closedByEntryNo, reversed, balAccountType, balAccountNo, journalTemplName, journalBatchName, sourceCode, reasonCode, globalDimension1Code, globalDimension2Code, userId, companyName, lastModifiedDateTime.
Every posted bank movement (Bank Account Ledger Entry, table 271). Bank reconciliation changes
statementStatus / statementNo / open on the row, so the incremental sync picks those changes up. Natural key:
entryNo. Join to G/L and customer ledger entries on transactionNo.
"""
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, status

from src import config
from src.services.bc_functions import (
    call_custom_connector_table,
    custom_connector_get_record,
)

logger = logging.getLogger("bc_routes.custom_connector_bank_account_ledger_entry")

custom_connector_bank_account_ledger_entry_router = APIRouter(
    prefix="/bc/custom-connector/bank-account-ledger-entries",
    tags=["Custom Connector — Bank Account Ledger Entry"],
)

_TABLE = "bankAccountLedgerEntries"


def _unwrap_list(http_status: int, data: Any) -> List[Dict[str, Any]]:
    if http_status != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Business Central returned {http_status}: {data}",
        )
    return data.get("value", data)


@custom_connector_bank_account_ledger_entry_router.get("", summary="List Bank Account Ledger Entries")
def list_bank_account_ledger_entries(
    filter: Optional[str] = Query(None, description="OData $filter expression (e.g. bankAccountNo eq 'BANK01')"),
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
    modified_from: Optional[str] = Query(
        None,
        description="Only records with lastModifiedDateTime on/after this ISO 8601 datetime (e.g. from Airbyte's incremental cursor). Combined with `filter` via AND if both are given.",
    ),
    limit: Optional[int] = Query(None, ge=1, le=5000, description="Max records to return in this page (BC $top). Omit for all matching records."),
    offset: Optional[int] = Query(None, ge=0, description="Records to skip before this page (BC $skip)."),
):
    """Return Bank Account Ledger Entries records from BC (Pag51028). Unfiltered, unpaginated requests are cached for 5 minutes."""
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
        logger.error(f"Error listing bank account ledger entries: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@custom_connector_bank_account_ledger_entry_router.get("/{record_id}", summary="Get Bank Account Ledger Entry by ID")
def get_bank_account_ledger_entry(
    record_id: str,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    """Fetch a single Bank Account Ledger Entry record by SystemId from BC."""
    try:
        http_status, data = custom_connector_get_record(
            table_endpoint=_TABLE,
            record_id=record_id,
            company_name=company or config.BC_COMPANY,
        )
        if http_status == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Bank account ledger entry record '{record_id}' not found",
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
        logger.error(f"Error fetching bank account ledger entry {record_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
