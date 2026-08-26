"""Session history endpoints — persistent consignment session records in Firestore.

POST /session-history        — save a completed session (called fire-and-forget from the app)
GET  /session-history        — list sessions for a company, optionally filtered by user
"""
import logging
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from src.services.session_history_service import save_session, get_sessions

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
    limit: int = Query(100, ge=1, le=500, description="Max records to return"),
    offset: int = Query(0, ge=0, description="Records to skip"),
):
    """Return session history from Firestore for the given company.

    Pass user_id to restrict results to a single user's sessions.
    Results are sorted newest-first by submittedAt.
    """
    try:
        sessions, total = get_sessions(
            company_code=company_code,
            user_id=user_id or None,
            limit=limit,
            offset=offset,
        )
        return {"data": sessions, "total": total, "limit": limit, "offset": offset}
    except Exception as e:
        logger.error(f"session_history GET failed: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
