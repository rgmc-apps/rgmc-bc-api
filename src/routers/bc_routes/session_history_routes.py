"""Session history endpoints — persistent consignment session records in Firestore.

POST /session-history             — save a completed session (called fire-and-forget from the app)
GET  /session-history             — list sessions for a company, optionally filtered by user
POST /session-history/verify-all  — publishes a verify-session-history job to the worker pool
POST /session-history/{id}/verify — re-verify one record against BC synchronously (cheap)
"""
import logging
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Header, HTTPException, Query, status
from pydantic import BaseModel

from src import config
from src.services.session_history_service import save_session, get_sessions, verify_one
from src.services.pubsub_publisher import publish_sync_message

logger = logging.getLogger("session_history_routes")

session_history_router = APIRouter(
    prefix="/session-history",
    tags=["Session History"],
)


class OrderLineRecord(BaseModel):
    id: str
    itemNumber: str
    itemName: str
    description: str
    categoryCode: Optional[str] = None
    srp: float
    priceListCode: Optional[str] = None
    quantity: int
    discountType: str
    discountValue: float
    totalAmount: float


class SessionRecord(BaseModel):
    id: str
    companyCode: Optional[str] = None
    userId: Optional[str] = None
    userDisplayName: str
    userEmail: Optional[str] = None
    userNumber: Optional[str] = None
    brandCode: str
    brandDisplayName: str
    customerNumber: Optional[str] = None
    customerDisplayName: Optional[str] = None
    postingDate: Optional[str] = None
    noSales: Optional[bool] = False
    salesOrders: List[Dict[str, Any]] = []
    returnOrders: List[Dict[str, Any]] = []
    status: str
    salesOrderSeries: Optional[str] = None
    returnOrderSeries: Optional[str] = None
    errorMessage: Optional[str] = None
    createdAt: str
    submittedAt: Optional[str] = None
    updatedAt: Optional[str] = None


@session_history_router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Save Session Record",
)
def post_session(record: SessionRecord):
    """Persist a completed consignment session to Firestore.

    Called fire-and-forget from the frontend after every submission (success or failure).
    Idempotent — re-posting the same session id overwrites the existing record.
    """
    try:
        doc_id = save_session(record.model_dump())
        return {"id": doc_id}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"session_history POST failed: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@session_history_router.get(
    "",
    summary="List Session History",
)
def list_sessions(
    company_code: str = Query(..., description="Company code (e.g. RGMC)"),
    user_id: Optional[str] = Query(None, description="Filter to a specific user by their Contact ID"),
    user_number: Optional[str] = Query(None, description="Contact number fallback for sessions where userId was not stored"),
    limit: int = Query(100, ge=1, le=500, description="Max records to return"),
    offset: int = Query(0, ge=0, description="Records to skip"),
):
    """Return session history from Firestore for the given company.

    Pass user_id to restrict results to a single user's sessions.
    Pass user_number as a fallback to catch sessions saved before userId was tracked.
    Results are sorted newest-first by submittedAt.
    """
    try:
        sessions, total = get_sessions(
            company_code=company_code,
            user_id=user_id or None,
            user_number=user_number or None,
            limit=limit,
            offset=offset,
        )
        return {"data": sessions, "total": total, "limit": limit, "offset": offset}
    except Exception as e:
        logger.error(f"session_history GET failed: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@session_history_router.post(
    "/verify-all",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Publish a session-history verification job to the worker pool",
    tags=["Internal"],
)
def verify_all_sessions(
    force: bool = Query(False, description="Re-verify records that already have a status, not just unchecked/pending ones"),
    limit: Optional[int] = Query(None, ge=1, description="Cap how many records the worker processes in this run"),
    x_task_secret: str = Header("", alias="X-Task-Secret", description="Required — must match TASK_SECRET env var"),
):
    """Publish a verify-session-history message to the worker pool via Pub/Sub.

    The collection (20k+ docs and growing) is too large to validate against BC inline
    in this request — the worker pool runs it in the background instead, with its own
    BC request budget fully decoupled from this API's live traffic. By default only
    unchecked/pending records are processed, so repeated runs converge on full
    coverage without re-verifying everything each time. Returns 202 immediately.
    Requires X-Task-Secret header.
    """
    if x_task_secret != config.TASK_SECRET:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    payload: dict = {"type": "verify-session-history", "force": force}
    if limit is not None:
        payload["limit"] = limit
    try:
        msg_id = publish_sync_message(payload)
        return {"status": "published", "message_id": msg_id, "topic": config.PUBSUB_SYNC_TOPIC, "payload": payload}
    except Exception as e:
        logger.error(f"session_history verify-all publish failed: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@session_history_router.post(
    "/{doc_id}/verify",
    summary="Re-verify a single session history record against BC",
)
def verify_single_session(doc_id: str):
    """Re-check one session record's in_bc flag against Business Central on demand."""
    try:
        return verify_one(doc_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.error(f"session_history verify failed for {doc_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
