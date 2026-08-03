"""GET /deferred/{key} — retrieve a deferred (slow-request) result.

When an endpoint defers its response (returns 202 with a key), the client polls
this endpoint until status changes from "pending" to "ready" or "error".

Response shapes:
  202  {"status": "pending",  "endpoint": "...", "created_at": <epoch float>}
  200  {"status": "ready",    "endpoint": "...", "completed_at": <float>, "data": <any>}
  500  {"status": "error",    "endpoint": "...", "error": "..."}
  404  {"detail": "Deferred result not found or expired"}
"""
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from src.services.deferred_result_service import get_meta, read_data

logger = logging.getLogger("deferred_routes")

deferred_router = APIRouter(tags=["Deferred Results"])


@deferred_router.get("/deferred/{key}", summary="Poll a deferred result")
def get_deferred_result(key: str):
    meta = get_meta(key)
    if meta is None:
        raise HTTPException(status_code=404, detail="Deferred result not found or expired")

    status = meta.get("status", "pending")
    endpoint = meta.get("endpoint", "")
    created_at = meta.get("created_at")
    completed_at = meta.get("completed_at")

    if status == "pending":
        return JSONResponse(
            status_code=202,
            content={
                "status": "pending",
                "endpoint": endpoint,
                "created_at": created_at,
                "message": "Still processing — check back in a few seconds.",
            },
        )

    if status == "error":
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "endpoint": endpoint,
                "error": meta.get("error"),
                "created_at": created_at,
                "completed_at": completed_at,
            },
        )

    # status == "ready" — fetch payload from GCS
    try:
        data = read_data(meta)
    except Exception as exc:
        logger.error(f"Failed to read deferred data key={key!r}: {exc}")
        raise HTTPException(status_code=500, detail=f"Failed to retrieve result: {exc}")

    if data is None:
        raise HTTPException(status_code=500, detail="Result data unavailable — GCS read failed")

    return {
        "status": "ready",
        "endpoint": endpoint,
        "params": meta.get("params"),
        "created_at": created_at,
        "completed_at": completed_at,
        "data": data,
    }
