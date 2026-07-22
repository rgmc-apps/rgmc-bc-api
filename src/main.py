import threading
import time
import src.config as config
from src.services.bc_functions import ServiceWarmingError
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
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
    rgmc_item_price_v2_router,
    rgmc_item_price_v3_router,
    rgmc_sales_order_router,
    rgmc_company_v2_router,
    rgmc_customer_v2_router,
    rgmc_retail_customer_v2_router,
    rgmc_contact_v2_router,
    rgmc_item_v2_router,
    rgmc_item_family_v2_router,
    rgmc_sales_return_order_v2_router,
    rgmc_sales_order_v2_router,
    task_router,
    item_price_firestore_router,
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
        "name": "BC RGMC Item Prices v2",
        "description": "RGMC custom API v2.0 — Item Price CRUD endpoints (Pag50210, api/rgmc/rgmccustom/v2.0).",
    },
    {
        "name": "BC RGMC Item Prices v3",
        "description": "RGMC custom API v3.0 — Item Price read endpoints (Pag50318, api/rgmc/rgmccustom/v3.0). Returns one current price per product.",
    },
    {
        "name": "BC RGMC Sales Orders",
        "description": "RGMC custom API — Sales Order and Lines CRUD endpoints (Pag50216/Pag50217).",
    },
    {
        "name": "BC RGMC Company Settings v2",
        "description": "RGMC custom API v2.0 — Company Settings read and update endpoints (Pag50492, api/rgmc/rgmccustom/v2.0/companies({id})/companySettings).",
    },
    {
        "name": "BC RGMC Customers v2",
        "description": "RGMC custom API v2.0 — Customer CRUD endpoints (api/rgmc/rgmccustom/v2.0/companies({id})/customers).",
    },
    {
        "name": "BC RGMC Retail Customers v2",
        "description": "RGMC custom API v2.0 — Retail Customer CRUD endpoints (Pag50307, api/rgmc/rgmccustom/v2.0).",
    },
    {
        "name": "BC RGMC Contacts v2",
        "description": "RGMC custom API v2.0 — Contact CRUD endpoints (Pag50308), including picture (Pag50309) and brand tags (Pag50312).",
    },
    {
        "name": "BC RGMC Items v2",
        "description": "RGMC custom API v2.0 — Item read endpoints (Pag50310).",
    },
    {
        "name": "BC RGMC Item Families v2",
        "description": "RGMC custom API v2.0 — Item Family read endpoints (Pag50311).",
    },
    {
        "name": "BC RGMC Sales Return Orders v2",
        "description": "RGMC custom API v2.0 — Sales Return Order and Lines CRUD endpoints (Pag50313/Pag50314).",
    },
    {
        "name": "BC RGMC Sales Orders v2",
        "description": "RGMC custom API v2.0 — Sales Order and Lines CRUD endpoints (Pag50315/Pag50316).",
    },
]

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


try:
    revision = config.revision_code
    api = FastAPI(
        title=f"RGMC BC API (Release - {revision})",
        docs_url="/swagger",
        version=config.__version__,
        openapi_tags=tags_metadata,
        lifespan=lifespan,
    )
    api.add_middleware(
        CORSMiddleware,
        allow_origins=config.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @api.exception_handler(ServiceWarmingError)
    async def service_warming_handler(request: Request, exc: ServiceWarmingError):
        return JSONResponse(
            status_code=503,
            content={"detail": str(exc)},
            headers={"Retry-After": "15"},
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
    api.include_router(rgmc_item_price_v2_router)
    api.include_router(rgmc_item_price_v3_router)
    api.include_router(rgmc_sales_order_router)
    api.include_router(rgmc_company_v2_router)
    api.include_router(rgmc_customer_v2_router)
    api.include_router(rgmc_retail_customer_v2_router)
    api.include_router(rgmc_contact_v2_router)
    api.include_router(rgmc_item_v2_router)
    api.include_router(rgmc_item_family_v2_router)
    api.include_router(rgmc_sales_return_order_v2_router)
    api.include_router(rgmc_sales_order_v2_router)
    api.include_router(task_router)
    api.include_router(item_price_firestore_router)


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


# Added last → outermost middleware, so compression happens AFTER error_email_middleware
# has read the (plain-text) body. Catalog responses (thousands of price records)
# compress ~10x — reps on mobile networks were downloading multi-MB JSON uncompressed.
api.add_middleware(GZipMiddleware, minimum_size=1024)



@api.get("/index")
def index():
    return {"status": "BC API is running"}
