import time
from datetime import datetime

from fastapi import APIRouter

from src.config import BC_ENVIRONMENT, __version__
from src.logger import logger
from src.types_py import BCHealthcheckResponse, HealthcheckResponse

healthrouter = APIRouter()


@healthrouter.get("/healthcheck", response_model=HealthcheckResponse, tags=["health"])
def healthcheck() -> HealthcheckResponse:
    message = "We're on the air."
    now = datetime.now()
    logger.info(msg=message, extra={"version": __version__, "time": now})
    return HealthcheckResponse(message=message, version=__version__, time=now)


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
