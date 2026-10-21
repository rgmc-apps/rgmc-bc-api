"""Firestore persistence for consignment app session history records.

Collection: session_history_{env}
Document ID: {companyCode}_{sessionId}

Stores the full session payload (orders, user, customer) so history survives
localStorage clears and is accessible across devices per user.
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from google.api_core import retry as api_retry
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from src import config
from src.services.bc_functions import call_rgmc_table, call_rgmc_v2_table, odata_escape

logger = logging.getLogger("session_history_service")

_db: firestore.Client | None = None
_NO_RETRY = api_retry.Retry(predicate=lambda e: False, deadline=None)
_FAST_TIMEOUT = 15.0
_BATCH_SIZE = 500
_VERIFY_WORKERS = 24


def _firestore() -> firestore.Client:
    global _db
    if _db is None:
        _db = firestore.Client(project=config.GCP_PROJECT_ID)
    return _db


def _collection_name() -> str:
    env = (config.GCP_ENV or "staging").lower().replace(" ", "_")
    return f"session_history_{env}"


def _initial_in_bc(record: dict, company_code: str) -> dict:
    """Best-effort in_bc flag computed at save time, without a live BC call.

    'submitted' is trusted immediately (BC just confirmed creation in this same
    request/task). 'failed' is left pending for verify_record/backfill_validate to
    resolve — a failed submission might still have landed in BC (timeout after
    success), so it must not be assumed not_found without actually checking.
    """
    if record.get("noSales"):
        return {"inBc": True, "inBcStatus": "not_applicable", "inBcCheckedAt": time.time()}
    if not company_code or company_code == "UNKNOWN":
        return {"inBc": None, "inBcStatus": "unknown_company", "inBcCheckedAt": None}
    if record.get("status") == "submitted":
        return {"inBc": True, "inBcStatus": "confirmed", "inBcCheckedAt": time.time()}
    return {"inBc": None, "inBcStatus": "pending", "inBcCheckedAt": None}


def save_session(record: dict) -> str:
    """Upsert a session record into Firestore. Returns the document ID."""
    db = _firestore()
    collection = _collection_name()

    session_id = (record.get("id") or "").strip()
    company_code = (record.get("companyCode") or "unknown").strip().upper()
    if not session_id:
        raise ValueError("Session record must include a non-empty 'id' field")

    doc_id = f"{company_code}_{session_id}"
    ref = db.collection(collection).document(doc_id)
    ref.set({
        **record,
        **_initial_in_bc(record, company_code),
        "companyCode": company_code,
        "savedAt": time.time(),
        "env": config.GCP_ENV,
    })
    logger.info(
        f"Session history saved: {doc_id} "
        f"(user={record.get('userId')!r}, status={record.get('status')!r})"
    )
    return doc_id


def get_sessions(
    company_code: str,
    user_id: Optional[str] = None,
    user_number: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """Return session history records for the given company, optionally filtered by user.

    Sorted by submittedAt descending (most recent first). Sorting and pagination are
    done in Python to avoid requiring composite Firestore indexes.

    Two known data-quality gaps are handled here:
    - Sessions submitted before companyCode was tracked are stored as "UNKNOWN".
      The query uses an `in` filter to catch both the real code and "UNKNOWN".
    - Sessions submitted before userId was tracked have userId == null. A second
      query by userNumber catches those so they still appear for the correct user.
    """
    db = _firestore()
    collection = _collection_name()
    normalized = company_code.strip().upper()

    seen_ids: set[str] = set()
    all_records: list[dict] = []

    def _run_query(q) -> list[dict]:
        try:
            return [doc.to_dict() for doc in q.stream(retry=_NO_RETRY, timeout=_FAST_TIMEOUT)]
        except Exception as e:
            logger.warning(f"session_history Firestore fetch failed: {e}")
            return []

    def _merge(records: list[dict]) -> None:
        for r in records:
            rid = r.get("id") or ""
            if rid and rid not in seen_ids:
                seen_ids.add(rid)
                all_records.append(r)

    # Primary query: sessions where companyCode matches (or was saved as "UNKNOWN")
    primary = db.collection(collection).where(
        filter=FieldFilter("companyCode", "in", [normalized, "UNKNOWN"])
    )
    if user_id:
        primary = primary.where(filter=FieldFilter("userId", "==", user_id))
    _merge(_run_query(primary))

    # Fallback query: sessions with userId == null (saved before userId was tracked),
    # identified by userNumber instead.
    if user_id and user_number:
        fallback = db.collection(collection).where(
            filter=FieldFilter("companyCode", "in", [normalized, "UNKNOWN"])
        ).where(
            filter=FieldFilter("userId", "==", None)
        ).where(
            filter=FieldFilter("userNumber", "==", user_number)
        )
        _merge(_run_query(fallback))

    all_records.sort(key=lambda r: r.get("submittedAt") or r.get("createdAt") or "", reverse=True)

    total = len(all_records)
    page = all_records[offset:offset + limit] if limit else all_records[offset:]
    return page, total


def _lookup_by_number(table_fn, table: str, company_code: str, number: Optional[str]) -> Optional[dict]:
    """Exact lookup of a BC document by its own document number. None if not found."""
    if not number:
        return None
    status_code, data = table_fn(table, company_name=company_code, odata_filter=f"number eq '{odata_escape(number)}'")
    if status_code != 200:
        return None
    records = data.get("value", data)
    return records[0] if records else None


def _lookup_candidates_by_customer_date(
    table_fn, table: str, company_code: str, posting_date: Optional[str], customer_number: Optional[str]
) -> list:
    """Exact customer+date match — the same fallback the History page's manual 'Fetch
    BC Order Number' button uses, but never auto-picks among multiple candidates (see
    the Thread C history-mismatch bug, caused by exactly that)."""
    if not posting_date or not customer_number:
        return []
    odata_filter = f"postingDate eq {posting_date} and sellToCustomerNo eq '{odata_escape(customer_number)}'"
    status_code, data = table_fn(table, company_name=company_code, odata_filter=odata_filter)
    if status_code != 200:
        return []
    return data.get("value", data) or []


def _verify_side(
    table_fn, table: str, company_code: str, series: Optional[str], posting_date: Optional[str], customer_number: Optional[str]
) -> tuple[str, Optional[str]]:
    """Returns (status, recovered_number) for one side (sales or returns) of a session.

    status is one of 'confirmed' | 'not_found' | 'needs_review'.
    """
    if series and _lookup_by_number(table_fn, table, company_code, series):
        return "confirmed", None

    candidates = _lookup_candidates_by_customer_date(table_fn, table, company_code, posting_date, customer_number)
    if len(candidates) == 1:
        return "confirmed", candidates[0].get("number")
    if len(candidates) > 1:
        return "needs_review", None
    return "not_found", None


def verify_record(record: dict) -> dict:
    """Determine whether a session history record's order(s) really exist in BC.

    Returns a patch dict (inBc, inBcStatus, inBcCheckedAt, and any recovered series
    numbers) meant to be merged back onto the Firestore doc — never a full overwrite,
    since recovered series numbers should be kept even if other fields are stale.
    """
    if record.get("noSales"):
        return {"inBc": True, "inBcStatus": "not_applicable", "inBcCheckedAt": time.time()}

    company_code = (record.get("companyCode") or "").strip().upper()
    if not company_code or company_code == "UNKNOWN":
        return {"inBc": None, "inBcStatus": "unknown_company", "inBcCheckedAt": time.time()}

    posting_date = record.get("postingDate")
    customer_number = record.get("customerNumber")
    sales_lines = record.get("salesOrders") or []
    return_lines = record.get("returnOrders") or []

    statuses: list[str] = []
    patch: dict = {}

    if sales_lines:
        side_status, recovered = _verify_side(
            call_rgmc_table, "salesOrders", company_code,
            record.get("salesOrderSeries"), posting_date, customer_number,
        )
        statuses.append(side_status)
        if recovered:
            patch["salesOrderSeries"] = recovered

    if return_lines:
        side_status, recovered = _verify_side(
            call_rgmc_v2_table, "salesReturnOrders", company_code,
            record.get("returnOrderSeries"), posting_date, customer_number,
        )
        statuses.append(side_status)
        if recovered:
            patch["returnOrderSeries"] = recovered

    if not statuses:
        overall = "not_applicable"
    elif "not_found" in statuses:
        overall = "not_found"
    elif "needs_review" in statuses:
        overall = "needs_review"
    else:
        overall = "confirmed"

    patch["inBcStatus"] = overall
    patch["inBc"] = {"confirmed": True, "not_found": False, "not_applicable": True}.get(overall)
    patch["inBcCheckedAt"] = time.time()
    return patch


def verify_one(doc_id: str) -> dict:
    """Re-verify a single session history record on demand and persist the result."""
    db = _firestore()
    collection = _collection_name()
    ref = db.collection(collection).document(doc_id)
    doc = ref.get(retry=_NO_RETRY, timeout=_FAST_TIMEOUT)
    if not doc.exists:
        raise ValueError(f"Session history record {doc_id!r} not found")
    patch = verify_record(doc.to_dict())
    ref.set(patch, merge=True)
    return {"id": doc_id, **patch}


def backfill_validate(force: bool = False, limit: Optional[int] = None) -> dict:
    """Validate stored session history records against BC and patch their in_bc flag
    in place. Scans the whole collection (all companies) since this is a maintenance
    pass, not a user-scoped read.

    By default, only records that have never been checked (no inBcStatus) or were
    left `pending` are processed — already-`confirmed`/`not_found`/`needs_review`/
    `not_applicable`/`unknown_company` records are skipped. The collection can be
    large enough that one pass doesn't finish inside a single request, so this makes
    repeated calls converge incrementally instead of re-verifying everything each
    time. Pass force=True to re-verify records regardless of their current status.

    limit caps how many records this call processes (oldest-unprocessed-first isn't
    guaranteed — Firestore's default stream order is used), letting a huge backlog
    be worked through in bounded chunks across multiple calls instead of one very
    long request.

    Safe to re-run at any time — each record is independently re-evaluated and
    patched via set(merge=True), so nothing outside the verified fields is ever
    touched, and partial progress (already-committed batches) persists even if this
    call itself doesn't finish.
    """
    db = _firestore()
    collection = _collection_name()
    all_docs = list(db.collection(collection).stream(retry=_NO_RETRY, timeout=300.0))

    if force:
        pending_docs = all_docs
    else:
        pending_docs = [d for d in all_docs if d.to_dict().get("inBcStatus") in (None, "pending")]
    skipped = len(all_docs) - len(pending_docs)

    if limit is not None:
        pending_docs = pending_docs[:limit]

    def _evaluate(doc) -> tuple[str, dict]:
        record = doc.to_dict()
        try:
            patch = verify_record(record)
        except Exception as e:
            logger.warning(f"verify_record failed for {doc.id}: {e}")
            patch = {"inBcStatus": "pending"}
        return doc.id, patch

    counts: dict[str, int] = {}
    batch = db.batch()
    count_in_batch = 0

    with ThreadPoolExecutor(max_workers=_VERIFY_WORKERS) as executor:
        futures = [executor.submit(_evaluate, doc) for doc in pending_docs]
        for future in as_completed(futures):
            doc_id, patch = future.result()
            label = patch.get("inBcStatus", "pending")
            counts[label] = counts.get(label, 0) + 1
            ref = db.collection(collection).document(doc_id)
            batch.set(ref, patch, merge=True)
            count_in_batch += 1
            if count_in_batch >= _BATCH_SIZE:
                batch.commit()
                batch = db.batch()
                count_in_batch = 0

    if count_in_batch > 0:
        batch.commit()

    logger.info(
        f"session_history backfill_validate: {counts} "
        f"(total={len(all_docs)}, skipped_already_verified={skipped}, processed={len(pending_docs)})"
    )
    return {
        "total": len(all_docs),
        "skipped_already_verified": skipped,
        "processed": len(pending_docs),
        "counts": counts,
    }
