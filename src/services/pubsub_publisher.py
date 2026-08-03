"""Pub/Sub publishing helpers for outbound messages to the worker pool."""
import json
import logging

from google.cloud import pubsub_v1

from src import config

logger = logging.getLogger("pubsub_publisher")

_publisher: pubsub_v1.PublisherClient | None = None


def _get_publisher() -> pubsub_v1.PublisherClient:
    global _publisher
    if _publisher is None:
        _publisher = pubsub_v1.PublisherClient()
    return _publisher


def publish_sync_message(data: dict) -> str:
    """Publish a JSON message to PUBSUB_SYNC_TOPIC. Returns the published message ID."""
    publisher = _get_publisher()
    topic_path = publisher.topic_path(config.GCP_PROJECT_ID, config.PUBSUB_SYNC_TOPIC)
    payload = json.dumps(data).encode("utf-8")
    future = publisher.publish(topic_path, payload)
    msg_id = future.result()
    logger.info(f"Published to {topic_path!r}: type={data.get('type')!r} msg_id={msg_id}")
    return msg_id
