"""Cloud Tasks enqueue and Firestore task-result store for async order processing."""
import datetime
import json
import logging
import uuid

from google.api_core import retry as api_retry
from google.cloud import firestore
from google.cloud import tasks_v2

from src import config

_NO_RETRY = api_retry.Retry(predicate=lambda e: False, deadline=None)

# How long a task can sit in "processing" before a retry delivery is allowed
# to take it over — covers the case where a prior attempt crashed between
# claiming the task and reaching a terminal status, which would otherwise
# wedge the task forever (every retry sees "processing" and backs off).
_STALE_PROCESSING_SECONDS = 180

logger = logging.getLogger("task_service")

_tasks_client: tasks_v2.CloudTasksClient | None = None
_db: firestore.Client | None = None


def _collection() -> str:
    env = (config.GCP_ENV or "staging").lower().replace(" ", "_")
    return f"order_tasks_{env}"


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
    task_id: str | None = None,
) -> str:
    """Write a task document to Firestore and enqueue a Cloud Task to process the order.

    order_type  : "sales" or "returns"
    api_version : "v1" (RGMC v1 API) or "v2" (RGMC v2 API)
    header      : order header dict, already field-mapped and ready for BC
    lines       : list of line dicts, already field-mapped (lineNo pre-set) and ready for BC
    company     : BC company name
    task_id     : pre-generated id, for callers that need to reserve against it
                  (e.g. an external-document-number lock) before enqueueing.
                  Generated here if not supplied.

    Returns the task_id UUID string — the frontend polls GET /tasks/{task_id}.
    """
    task_id = task_id or str(uuid.uuid4())

    _firestore().collection(_collection()).document(task_id).set({
        "status": "queued",
        "order_type": order_type,
        "created_at": datetime.datetime.utcnow(),
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
    logger.info(f"Enqueued {order_type}/{api_version} order task {task_id} → {_collection()}")
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
    doc = _firestore().collection(_collection()).document(task_id).get(retry=_NO_RETRY)
    return doc.to_dict() if doc.exists else None


def claim_task_for_processing(task_id: str) -> str:
    """Atomically claim a queued (or stale-processing) task for execution.

    Cloud Tasks guarantees at-least-once delivery, so the same task_id can be
    POSTed to /internal/tasks/process-order more than once — most commonly a
    retry after the response to a successful attempt was lost (dropped
    connection, container recycle), occasionally a genuine duplicate
    delivery. Without this claim, a retry would call BC's create endpoint
    again and create a second, duplicate order. The Firestore transaction
    makes the read-then-write atomic so two concurrent deliveries can't both
    pass the check and both proceed.

    Returns:
      "claimed"       - caller should proceed to call BC and process the order.
      "already_done"  - a prior attempt already finished successfully; caller
                         should return success without touching BC again.
      "in_progress"   - another delivery of this task is actively being
                         worked right now; caller should ask Cloud Tasks to
                         retry later rather than race it.
    """
    db = _firestore()
    doc_ref = db.collection(_collection()).document(task_id)

    @firestore.transactional
    def _txn(transaction):
        snapshot = doc_ref.get(transaction=transaction, retry=_NO_RETRY)
        data = snapshot.to_dict() or {}
        current_status = data.get("status")
        if current_status == "done":
            return "already_done"
        if current_status == "processing":
            started = data.get("processing_started_at")
            if started is not None:
                age = (datetime.datetime.utcnow() - started.replace(tzinfo=None)).total_seconds()
                if age < _STALE_PROCESSING_SECONDS:
                    return "in_progress"
        transaction.set(doc_ref, {
            "status": "processing",
            "processing_started_at": datetime.datetime.utcnow(),
        }, merge=True)
        return "claimed"

    return _txn(db.transaction())


def update_task(task_id: str, **fields):
    _firestore().collection(_collection()).document(task_id).set(fields, merge=True)
