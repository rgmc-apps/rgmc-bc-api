"""Internal connectivity test endpoints.

POST /internal/test/worker-ping  — publish a ping to the Pub/Sub sync topic;
                                   the worker pool responds by sending a confirmation email.
"""
import datetime
import logging
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query, status

from src import config
from src.services.pubsub_publisher import publish_sync_message

logger = logging.getLogger("bc_routes.test")

test_router = APIRouter(tags=["Internal"])


@test_router.post(
    "/internal/test/worker-ping",
    summary="Ping Worker Pool via Pub/Sub",
    status_code=status.HTTP_202_ACCEPTED,
)
async def worker_ping(
    note: Optional[str] = Query(None, description="Optional note to include in the ping payload"),
    x_task_secret: str = Header("", alias="X-Task-Secret"),
):
    """Publish a ping message to the Pub/Sub sync topic.

    The worker pool receives the message and sends a confirmation email to
    DEVELOPER_EMAIL. Use this to verify end-to-end bc-api → Pub/Sub → worker pool
    connectivity. Requires X-Task-Secret header.
    """
    if x_task_secret != config.TASK_SECRET:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    payload: dict = {
        "type": "ping",
        "sent_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "sent_by": "bc-api",
    }
    if note:
        payload["note"] = note

    try:
        msg_id = publish_sync_message(payload)
        return {
            "status": "published",
            "message_id": msg_id,
            "topic": config.PUBSUB_SYNC_TOPIC,
            "payload": payload,
        }
    except Exception as e:
        logger.error(f"worker-ping: failed to publish: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to publish to Pub/Sub: {e}",
        )
