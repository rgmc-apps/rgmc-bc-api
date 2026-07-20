"""Cloud Tasks HTTP callback and task-status polling endpoints."""
import logging
import time

from fastapi import APIRouter, HTTPException, Request, status

from src import config
from src.services.bc_functions import (
    rgmc_create_record,
    rgmc_delete_record,
    rgmc_v2_create_record,
    rgmc_v2_delete_record,
)
from src.services.task_service import get_task, update_task

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

    body = await request.json()
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
        _TABLE = "salesOrders"
        _LINES_TABLE = "salesOrderLines"
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
