"""Cloud Tasks enqueue and Firestore task-result store for async order processing."""
import json
import logging
import time
import uuid

from google.cloud import firestore
from google.cloud import tasks_v2

from src import config

logger = logging.getLogger("task_service")

_tasks_client: tasks_v2.CloudTasksClient | None = None
_db: firestore.Client | None = None
_COLLECTION = "order_tasks"


def _tasks() -> tasks_v2.CloudTasksClient:
    global _tasks_client
    if _tasks_client is None:
        _tasks_client = tasks_v2.CloudTasksClient()
    return _tasks_client


def _firestore() -> firestore.Client:
    global _db
    if _db is None:
        _db = firestore.Client(project=config.GCP_PROJECT_ID)
    return _db


def enqueue_order(
    order_type: str,
    api_version: str,
    header: dict,
    lines: list,
    company: str,
) -> str:
    """Write a task document to Firestore and enqueue a Cloud Task to process the order.

    order_type  : "sales" or "returns"
    api_version : "v1" (RGMC v1 API) or "v2" (RGMC v2 API)
    header      : order header dict, already field-mapped and ready for BC
    lines       : list of line dicts, already field-mapped (lineNo pre-set) and ready for BC
    company     : BC company name

    Returns the task_id UUID string — the frontend polls GET /tasks/{task_id}.
    """
    task_id = str(uuid.uuid4())

    _firestore().collection(_COLLECTION).document(task_id).set({
        "status": "queued",
        "order_type": order_type,
        "created_at": time.time(),
        "result": None,
        "error": None,
    })

    queue_path = _tasks().queue_path(
        config.GCP_PROJECT_ID,
        config.CLOUD_TASKS_LOCATION,
        config.CLOUD_TASKS_ORDER_QUEUE,
    )

    body = json.dumps({
        "task_id": task_id,
        "order_type": order_type,
        "api_version": api_version,
        "header": header,
        "lines": lines,
        "company": company,
    }).encode()

    _tasks().create_task(
        parent=queue_path,
        task={
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": f"{config.BC_API_URL}/internal/tasks/process-order/{task_id}",
                "headers": {
                    "Content-Type": "application/json",
                    "X-Task-Secret": config.TASK_SECRET,
                },
                "body": body,
            }
        },
    )
    logger.info(f"Enqueued {order_type}/{api_version} order task {task_id}")
    return task_id


def enqueue_catalog_sync(company: str) -> str:
    """Enqueue a catalog sync task to bc-sync-queue.

    Called by /internal/sync/trigger (Cloud Scheduler target). The task runs
    /internal/tasks/sync-catalog/{task_id} which warms the v3 price cache for
    the given company and persists it to GCS.

    Returns the task_id UUID string.
    """
    task_id = str(uuid.uuid4())
    queue_path = _tasks().queue_path(
        config.GCP_PROJECT_ID,
        config.CLOUD_TASKS_LOCATION,
        config.CLOUD_TASKS_SYNC_QUEUE,
    )
    body = json.dumps({"task_id": task_id, "company": company}).encode()
    _tasks().create_task(
        parent=queue_path,
        task={
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": f"{config.BC_API_URL}/internal/tasks/sync-catalog/{task_id}",
                "headers": {
                    "Content-Type": "application/json",
                    "X-Task-Secret": config.TASK_SECRET,
                },
                "body": body,
            }
        },
    )
    logger.info(f"Enqueued catalog sync task {task_id} for {company}")
    return task_id


def get_task(task_id: str) -> dict | None:
    doc = _firestore().collection(_COLLECTION).document(task_id).get()
    return doc.to_dict() if doc.exists else None


def update_task(task_id: str, **fields):
    _firestore().collection(_COLLECTION).document(task_id).update(fields)
