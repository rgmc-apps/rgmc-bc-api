import threading
import time
import src.config as config
from fastapi import FastAPI, Request
from fastapi.responses import Response
from typing import Any, Callable
from src.logger import logger
from src.routers import (
    healthrouter,
    bc_router,
    sales_order_router,
    item_router,
    customer_router,
    sales_credit_memo_router,
    retail_customer_router,
    sales_return_order_router,
    rgmc_contact_router,
    item_category_router,
    rgmc_item_router,
    rgmc_item_family_router,
    rgmc_item_price_router,
    rgmc_sales_order_router,
)
from src.services.send_mail import notify_error

tags_metadata = [
    {
        "name": "Business Central",
        "description": "Business Central data endpoints — brands, contacts, dimensions, and companies.",
    },
    {
        "name": "BC Sales Orders",
        "description": "Business Central Sales Order CRUD endpoints (api/v2.0).",
    },
    {
        "name": "BC Items",
        "description": "Business Central Item CRUD endpoints.",
    },
    {
        "name": "BC Customers",
        "description": "Business Central Customer CRUD endpoints.",
    },
    {
        "name": "BC Sales Credit Memos",
        "description": "Business Central Sales Credit Memo CRUD endpoints.",
    },
    {
        "name": "BC RGMC Retail Customers",
        "description": "RGMC custom API — Retail Customer CRUD endpoints (Pag50200, api/rgmc/rgmccustom/v1.0).",
    },
    {
        "name": "BC RGMC Sales Return Orders",
        "description": "RGMC custom API — Sales Return Order and Lines CRUD endpoints (Pag50201/Pag50202).",
    },
    {
        "name": "BC RGMC Contacts",
        "description": "RGMC custom API — Contact CRUD endpoints (Pag50203), including picture and brand tags.",
    },
    {
        "name": "BC Item Categories",
        "description": "Business Central Item Category CRUD endpoints.",
    },
    {
        "name": "BC RGMC Items",
        "description": "RGMC custom API — Item read endpoints (Pag50205).",
    },
    {
        "name": "BC RGMC Item Families",
        "description": "RGMC custom API — Item Family read endpoints (Pag50206).",
    },
    {
        "name": "BC RGMC Item Prices",
        "description": "RGMC custom API — Item Price read endpoints (Pag50210).",
    },
    {
        "name": "BC RGMC Sales Orders",
        "description": "RGMC custom API — Sales Order and Lines CRUD endpoints (Pag50216/Pag50217).",
    },
]

try:
    revision = config.revision_code
    api = FastAPI(
        title=f"RGMC BC API (Release - {revision})",
        docs_url="/swagger",
        version=config.__version__,
        openapi_tags=tags_metadata,
    )
    api.include_router(healthrouter)
    api.include_router(bc_router)
    api.include_router(sales_order_router)
    api.include_router(item_router)
    api.include_router(customer_router)
    api.include_router(sales_credit_memo_router)
    api.include_router(retail_customer_router)
    api.include_router(sales_return_order_router)
    api.include_router(rgmc_contact_router)
    api.include_router(item_category_router)
    api.include_router(rgmc_item_router)
    api.include_router(rgmc_item_family_router)
    api.include_router(rgmc_item_price_router)
    api.include_router(rgmc_sales_order_router)
except Exception as e:
    logger.error(f"Error initializing FastAPI: {e}")
    raise e


@api.middleware("http")
async def add_process_time_header(request: Request, call_next: Callable) -> Any:
    start_time = time.time()
    response = await call_next(request)
    process_time = time.time() - start_time
    response.headers["X-Process-Time"] = str(process_time)
    return response


@api.middleware("http")
async def error_email_middleware(request: Request, call_next: Callable) -> Any:
    response = await call_next(request)
    if response.status_code in (500, 502):
        body = b""
        async for chunk in response.body_iterator:
            body += chunk
        client_ip = request.headers.get("X-Forwarded-For") or (
            request.client.host if request.client else ""
        )
        threading.Thread(
            target=notify_error,
            args=(request.method, str(request.url), response.status_code, body.decode("utf-8", errors="replace"), client_ip),
            daemon=True,
        ).start()
        headers = dict(response.headers)
        headers.pop("content-length", None)
        return Response(
            content=body,
            status_code=response.status_code,
            headers=headers,
            media_type=response.media_type,
        )
    return response


@api.get("/index")
def index():
    return {"status": "BC API is running"}
