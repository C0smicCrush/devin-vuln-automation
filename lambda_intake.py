from __future__ import annotations

from aws_runtime import (
    enqueue_work_item,
    load_runtime_settings,
    parse_incoming_event,
)


def handler(event, context):  # noqa: ANN001
    settings = load_runtime_settings()
    raw_work_item = parse_incoming_event(event, settings)
    if raw_work_item.get("ignored"):
        return {"statusCode": 202, "body": raw_work_item["reason"]}
    queued = enqueue_work_item(settings, raw_work_item)
    return {
        "statusCode": 200,
        "body": {
            "message_id": queued["message_id"],
            "event_phase": raw_work_item.get("event_phase", "raw"),
            "source_type": raw_work_item["source"]["type"],
        },
    }
