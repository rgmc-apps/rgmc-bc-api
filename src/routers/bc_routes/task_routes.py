"""Cloud Tasks HTTP callback and task-status polling endpoints."""
import logging
import time

from fastapi import APIRouter, HTTPException, Request, status
from starlette.requests import ClientDisconnect

from src import config
from src.services.bc_functions import (
    rgmc_create_record,
    rgmc_delete_record,
    rgmc_v2_create_record,
    rgmc_v2_delete_record,
    rgmc_v3_warmup,
)
from src.services.task_service import enqueue_catalog_sync, get_task, update_task

logger = logging.getLogger("task_routes")

task_router = APIRouter(tags=["Tasks"])


@task_router.post(
    "/internal/tasks/process-order/{task_id}",
    include_in_schema=False,
)
async def process_order(task_id: str, request: Request):
    """Cloud Tasks HTTP target — not for direct client use.

    Returns 503 on transient failures so Cloud Tasks retries automatically.
    Returns 200 (ok=False) on permanent failures — error stored in Firestore.
    """
    if request.headers.get("X-Task-Secret", "") != config.TASK_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        body = await request.json()
    except ClientDisconnect:
        logger.warning(f"Task {task_id}: client disconnected before body was read — Cloud Tasks will retry")
        raise HTTPException(status_code=503, detail="Client disconnected")

    order_type: str = body.get("order_type", "sales")
    api_version: str = body.get("api_version", "v1")
    header: dict = body.get("header", {})
    lines: list = body.get("lines", [])
    company: str = body.get("company") or config.BC_COMPANY

    update_task(task_id, status="processing")

    if api_version == "v2":
        _TABLE = "salesOrders" if order_type == "sales" else "salesReturnOrders"
        _LINES_TABLE = "salesOrderLines" if order_type == "sales" else "salesReturnOrderLines"
        _create = rgmc_v2_create_record
        _delete = rgmc_v2_delete_record
    else:
        _TABLE = "salesOrders" if order_type == "sales" else "salesReturnOrders"
        _LINES_TABLE = "salesOrderLines" if order_type == "sales" else "salesReturnOrderLines"
        _create = rgmc_create_record
        _delete = rgmc_delete_record

    try:
        http_status, data = _create(_TABLE, header, company_name=company)
        if http_status not in (200, 201):
            raise ValueError(f"Order header failed: BC returned {http_status}: {data}")

        order_id = data.get("id")

        for i, line in enumerate(lines, start=1):
            for attempt in range(4):
                lh, ld = _create(
                    f"{_TABLE}({order_id})/{_LINES_TABLE}",
                    line,
                    company_name=company,
                )
                if lh in (200, 201):
                    break
                if lh == 409 and attempt < 3:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                try:
                    _delete(_TABLE, order_id, company_name=company)
                except Exception as del_err:
                    logger.error(f"Rollback failed for task {task_id}: {del_err}")
                raise ValueError(f"Line {i} failed (BC {lh}): {ld}. Order rolled back.")

        update_task(task_id, status="done", result=data)
        logger.info(f"Task {task_id} done — order {data.get('no') or order_id}")
        return {"ok": True}

    except ValueError as e:
        update_task(task_id, status="failed", error=str(e))
        logger.error(f"Task {task_id} permanent failure: {e}")
        return {"ok": False, "error": str(e)}

    except Exception as e:
        err_str = str(e)
        update_task(task_id, status="failed", error=err_str)
        logger.error(f"Task {task_id} transient failure: {e}")
        if any(code in err_str for code in ("429", "502", "503", "timeout", "ConnectionError")):
            raise HTTPException(status_code=503, detail=err_str)
        return {"ok": False, "error": err_str}


@task_router.post("/internal/sync/trigger", include_in_schema=False)
async def trigger_catalog_sync(request: Request):
    """Cloud Scheduler HTTP target — enqueues sync tasks to bc-sync-queue.

    Body: { "companies": ["RGMC", "CGI"] }   (defaults to BC_COMPANY if omitted)
    Requires X-Task-Secret header.
    """
    if request.headers.get("X-Task-Secret", "") != config.TASK_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    body = await request.json()
    companies: list = body.get("companies") or [config.BC_COMPANY]
    task_ids = []
    for company in companies:
        try:
            task_ids.append(enqueue_catalog_sync(company))
        except Exception as e:
            logger.error(f"Failed to enqueue sync for {company}: {e}")
    return {"enqueued": task_ids}


@task_router.post("/internal/tasks/sync-catalog/{task_id}", include_in_schema=False)
async def sync_catalog(task_id: str, request: Request):
    """Cloud Tasks HTTP target for bc-sync-queue — warms the v3 price cache and saves to GCS.

    Returns 503 on BC errors so Cloud Tasks retries automatically.
    """
    if request.headers.get("X-Task-Secret", "") != config.TASK_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        body = await request.json()
    except ClientDisconnect:
        logger.warning(f"Sync task {task_id}: client disconnected before body was read — Cloud Tasks will retry")
        raise HTTPException(status_code=503, detail="Client disconnected")
    company: str = body.get("company") or config.BC_COMPANY
    try:
        rgmc_v3_warmup(company)
        logger.info(f"Catalog sync task {task_id} triggered warmup for {company}")
        return {"ok": True}
    except Exception as e:
        logger.error(f"Catalog sync task {task_id} failed for {company}: {e}")
        raise HTTPException(status_code=503, detail=str(e))


@task_router.get("/tasks/{task_id}", summary="Poll async order task status")
def get_task_status(task_id: str):
    """Return the current status of an async order submission.

    Poll every 3 seconds. Stop when status is "done" or "failed".

    Response:
      status: "queued" | "processing" | "done" | "failed"
      result: BC order record (when status == "done")
      error:  failure message (when status == "failed")
    """
    task = get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task
