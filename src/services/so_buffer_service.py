"""Read/write access to rgmc-worker-pool's Firestore SO-import buffer, plus two sibling
collections for manual reconciliation links.

Collections (named per config.GCP_ENV, matching rgmc-worker-pool's src/services/so_buffer.py):
  so_buffer_{env}            — buffered/failed POUL SO-import orders. The doc itself
                                (header/lines/company/src_company/last_error/
                                attempt_count) is owned and written by rgmc-worker-pool
                                (so_buffer.save_failed_order/delete_buffered_order).
                                This module reads it, and (via apply_resolution_to_buffer/
                                clear_resolution_from_buffer) additively patches
                                header.resolvedShipTo/resolvedCustomer and per-line
                                resolvedItem fields directly onto the affected record —
                                every other field is left untouched.
  so_buffer_overrides_{env}  — the CURRENT manual link for a raw SKU code / customer
                                branch name / customer name — one doc per (type, key),
                                upserted. This is what the reconciliation UI matches
                                against to mark a group "resolved"; the buffer-doc patch
                                above is a denormalized copy of the same resolution,
                                written to the exact record(s) it applies to.
  so_buffer_reference_{env}  — an APPEND-ONLY history of every resolution ever saved
                                (never overwritten, one doc per save). Kept so a
                                previously-seen SKU/branch/customer can be looked up
                                for reference on a future upload even after its "current"
                                override has since been changed or superseded, and so a
                                future automated consumer (e.g. rgmc-worker-pool, once it
                                honors overrides) has a durable log to audit against.

IMPORTANT: none of this — not the overrides collection, not the resolved* fields now
written directly onto the buffer doc — is consumed by rgmc-worker-pool's reprocess
logic yet (so_import_worker.py always re-derives customer/ship-to/item from the raw
text fields on each buffered order). This is groundwork for that follow-up, not a
complete fix — the reconciliation UI is explicit about this to whoever uses it.
"""
import logging
import time
from typing import Any, Dict, List, Optional

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from src import config

logger = logging.getLogger("so_buffer_service")

_db: Optional[firestore.Client] = None

VALID_OVERRIDE_TYPES = ("sku", "branch", "customer")


def _firestore() -> firestore.Client:
    global _db
    if _db is None:
        _db = firestore.Client(project=config.GCP_PROJECT_ID)
    return _db


def _env_slug() -> str:
    return (config.GCP_ENV or "staging").lower().replace(" ", "_")


def _buffer_collection() -> str:
    return f"so_buffer_{_env_slug()}"


def _overrides_collection() -> str:
    return f"so_buffer_overrides_{_env_slug()}"


def _reference_collection() -> str:
    return f"so_buffer_reference_{_env_slug()}"


def list_buffered_orders(company: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return every buffered order as {"id": doc_id, **doc_data}.

    doc_data shape (written by rgmc-worker-pool's so_buffer.save_failed_order):
      header: dict (poRefNumber, customerName, customerBranchName,
              customerBranchLookUpCode, poDate, deliveryDate, ...)
      lines: list[dict] (customerSKUCode, customerSKUDesc, poQty, poQtyPcs, ...)
      company: str (BC company code, e.g. "SBIC")
      src_company: str
      last_error: str
      failed_at: timestamp
      attempt_count: int

    Read-only mirror — this collection is small (tens of documents), so no pagination.
    """
    db = _firestore()
    query = db.collection(_buffer_collection())
    if company:
        query = query.where(filter=FieldFilter("company", "==", company))
    return [{"id": doc.id, **(doc.to_dict() or {})} for doc in query.stream()]


def _override_doc_id(override_type: str, key: str) -> str:
    safe_key = "".join(c if c.isalnum() else "_" for c in key.strip().upper())
    return f"{override_type}_{safe_key}"[:1500]  # Firestore document ID length limit is 1500 bytes


def list_overrides(override_type: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return every saved manual link as {"id": doc_id, **doc_data}."""
    db = _firestore()
    query = db.collection(_overrides_collection())
    if override_type:
        query = query.where(filter=FieldFilter("type", "==", override_type))
    return [{"id": doc.id, **(doc.to_dict() or {})} for doc in query.stream()]


def save_override(
    override_type: str,
    key: str,
    resolved: Dict[str, Any],
    resolved_by: str,
) -> Dict[str, Any]:
    """Upsert a manual link for one raw SKU code / branch name / customer name.

    override_type: one of VALID_OVERRIDE_TYPES ("sku", "branch", "customer").
    key: the exact raw text value shared by every buffered order this override applies
         to (e.g. the line's customerSKUCode, or the header's customerBranchName /
         customerName) — matched case-insensitively via the document ID.
    resolved: the chosen BC record's fields, e.g. {"itemNo": "...", "description": "..."}
              for sku; {"customerNo": "...", "shipToCode": "...", "name": "..."} for
              branch; {"customerNo": "...", "displayName": "..."} for customer.
    resolved_by: free-text identifying who made the choice (shown back in the UI).
    """
    doc_id = _override_doc_id(override_type, key)
    doc_ref = _firestore().collection(_overrides_collection()).document(doc_id)
    payload = {
        "type": override_type,
        "key": key,
        "resolved": resolved,
        "resolved_by": resolved_by,
        "resolved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    doc_ref.set(payload)
    logger.info(f"so_buffer_overrides: saved {doc_id!r} -> {resolved!r} (by {resolved_by!r})")

    # Append-only reference log — every save gets its own new doc, never overwritten.
    try:
        _firestore().collection(_reference_collection()).add(payload)
    except Exception as exc:
        logger.warning(f"so_buffer_reference: failed to log {doc_id!r} (non-fatal): {exc}")

    return {"id": doc_id, **payload}


def apply_resolution_to_buffer(
    override_type: str,
    key: str,
    resolved: Dict[str, Any],
    buffer_ids: List[str],
) -> int:
    """Write a resolved link directly onto the affected buffered order doc(s) in
    so_buffer_{env}, in addition to the (type, key) upsert in so_buffer_overrides_{env}.

    This means the resolution travels with the order record itself rather than only
    living in a side lookup table keyed by raw text — if/when rgmc-worker-pool's
    reprocess logic is updated to consult it, it's already sitting right there.

    branch   -> header.resolvedShipTo = resolved    ({customerNo, shipToCode, name})
    customer -> header.resolvedCustomer = resolved  ({customerNo, displayName})
    sku      -> every line whose customerSKUCode (or, if blank, customerSKUDesc) matches
                `key` case-insensitively gets line.resolvedItem = resolved

    Only touches the `header` or `lines` field on each doc (via .update(), not .set())
    — company/last_error/attempt_count/etc. are left untouched. Silently skips any
    buffer_id that no longer exists (e.g. already reprocessed and cleared). Returns the
    number of docs actually patched.
    """
    db = _firestore()
    collection = _buffer_collection()
    key_upper = key.strip().upper()
    patched = 0

    for buffer_id in buffer_ids:
        doc_ref = db.collection(collection).document(buffer_id)
        snap = doc_ref.get()
        if not snap.exists:
            continue
        data = snap.to_dict() or {}

        if override_type in ("branch", "customer"):
            header = data.get("header") or {}
            header["resolvedShipTo" if override_type == "branch" else "resolvedCustomer"] = resolved
            doc_ref.update({"header": header})
            patched += 1
        elif override_type == "sku":
            lines = data.get("lines") or []
            changed = False
            for line in lines:
                sku = (line.get("customerSKUCode") or "").strip()
                match_key = sku or (line.get("customerSKUDesc") or "").strip() or "(no SKU code, no description)"
                if match_key.upper() == key_upper:
                    line["resolvedItem"] = resolved
                    changed = True
            if changed:
                doc_ref.update({"lines": lines})
                patched += 1

    logger.info(
        f"so_buffer: applied {override_type}/{key!r} resolution to {patched}/{len(buffer_ids)} buffer doc(s)"
    )
    return patched


def clear_resolution_from_buffer(override_type: str, key: str) -> int:
    """Remove a resolved link from every buffered order doc that currently has it.

    Scans so_buffer_{env} (small — tens of docs) for headers/lines matching `key` the
    same way apply_resolution_to_buffer found them, and deletes the corresponding
    resolved* field. Called when a saved override is undone, so a buffer doc never
    keeps claiming a link its override no longer confirms.
    """
    db = _firestore()
    key_upper = key.strip().upper()
    field_name = {"branch": "resolvedShipTo", "customer": "resolvedCustomer"}.get(override_type)
    cleared = 0

    for doc in db.collection(_buffer_collection()).stream():
        data = doc.to_dict() or {}
        if override_type in ("branch", "customer"):
            header = data.get("header") or {}
            name_field = "customerBranchName" if override_type == "branch" else "customerName"
            if (header.get(name_field) or "").strip().upper() == key_upper and field_name in header:
                del header[field_name]
                doc.reference.update({"header": header})
                cleared += 1
        elif override_type == "sku":
            lines = data.get("lines") or []
            changed = False
            for line in lines:
                sku = (line.get("customerSKUCode") or "").strip()
                match_key = sku or (line.get("customerSKUDesc") or "").strip() or "(no SKU code, no description)"
                if match_key.upper() == key_upper and "resolvedItem" in line:
                    del line["resolvedItem"]
                    changed = True
            if changed:
                doc.reference.update({"lines": lines})
                cleared += 1

    return cleared


def delete_override(doc_id: str) -> int:
    """Delete a saved override and clear the matching resolved* field from any buffer
    doc that has it. Returns how many buffer docs were cleared."""
    doc_ref = _firestore().collection(_overrides_collection()).document(doc_id)
    snap = doc_ref.get()
    cleared = 0
    if snap.exists:
        data = snap.to_dict() or {}
        override_type, key = data.get("type"), data.get("key")
        if override_type and key:
            cleared = clear_resolution_from_buffer(override_type, key)
    doc_ref.delete()
    logger.info(f"so_buffer_overrides: deleted {doc_id!r}, cleared from {cleared} buffer doc(s)")
    return cleared


def list_reference(override_type: Optional[str] = None, key: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return the resolution history, most recent first.

    Unlike list_overrides (current state, one row per key), this can return multiple
    rows for the same (type, key) if it was resolved more than once over time.
    """
    db = _firestore()
    query = db.collection(_reference_collection())
    if override_type:
        query = query.where(filter=FieldFilter("type", "==", override_type))
    if key:
        query = query.where(filter=FieldFilter("key", "==", key))
    docs = [{"id": doc.id, **(doc.to_dict() or {})} for doc in query.stream()]
    docs.sort(key=lambda d: d.get("resolved_at") or "", reverse=True)
    return docs
