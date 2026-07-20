import time
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Query

from src.config import BC_ENVIRONMENT, BC_COMPANY, __version__
from src.logger import logger
from src.types_py import BCHealthcheckResponse, HealthcheckResponse

healthrouter = APIRouter()


@healthrouter.get("/healthcheck", response_model=HealthcheckResponse, tags=["health"])
def healthcheck() -> HealthcheckResponse:
    message = "We're on the air."
    now = datetime.now()
    logger.info(msg=message, extra={"version": __version__, "time": now})
    return HealthcheckResponse(message=message, version=__version__, time=now)


@healthrouter.get("/bc/status", tags=["health"])
def bc_api_status(company: Optional[str] = Query(default=None)):
    """Return live server status: warmup state and concurrent BC request count.

    Used by the frontend to decide whether to warn the user about slow syncs
    or suggest retrying later when the server is under load.
    """
    from src.services.bc_functions import get_api_status
    return get_api_status(company or BC_COMPANY)


@healthrouter.get("/healthcheck/gcs", tags=["health"])
def gcs_healthcheck():
    """Test GCS connectivity and list all catalog blobs in the bucket."""
    from src import config
    from src.services import gcs_catalog
    import datetime

    bucket = config.GCS_CATALOG_BUCKET
    env = config.GCP_ENV
    if not bucket:
        return {"ok": False, "error": "GCS_CATALOG_BUCKET env var is not set"}

    try:
        client = gcs_catalog._gcs()
        blobs = list(client.bucket(bucket).list_blobs())
        existing = [
            {
                "blob_path": b.name,
                "size_bytes": b.size,
                "updated": b.updated.isoformat() if b.updated else None,
            }
            for b in blobs
        ]
    except Exception as e:
        return {"ok": False, "bucket": bucket, "env": env, "error": str(e)}

    return {
        "ok": True,
        "bucket": bucket,
        "env": env,
        "default_company": config.BC_COMPANY,
        "blobs_found": len(existing),
        "blobs": existing,
    }


@healthrouter.get("/healthcheck/bc", response_model=BCHealthcheckResponse, tags=["health"])
def bc_healthcheck() -> BCHealthcheckResponse:
    from src.services.bc_functions import call_business_central_api

    start = time.perf_counter()
    try:
        status_code, data = call_business_central_api("companies")
        latency_ms = (time.perf_counter() - start) * 1000
        if status_code == 200:
            company_count = len(data.get("value", []))
            return BCHealthcheckResponse(
                status="ok",
                environment=BC_ENVIRONMENT,
                latency_ms=round(latency_ms, 2),
                message=f"Connected to Business Central. {company_count} company(s) accessible.",
            )
        return BCHealthcheckResponse(
            status="error",
            environment=BC_ENVIRONMENT,
            latency_ms=round(latency_ms, 2),
            message="Business Central returned a non-200 response.",
            error=str(data),
        )
    except Exception as exc:
        latency_ms = (time.perf_counter() - start) * 1000
        return BCHealthcheckResponse(
            status="error",
            environment=BC_ENVIRONMENT,
            latency_ms=round(latency_ms, 2),
            message="Failed to connect to Business Central.",
            error=str(exc),
        )
