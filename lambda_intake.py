from __future__ import annotations

import json

from aws_runtime import (
    enqueue_work_item,
    load_runtime_settings,
    parse_incoming_event,
)


def _request_path(event: dict) -> str:
    return (
        event.get("rawPath")
        or event.get("requestContext", {}).get("http", {}).get("path")
        or ""
    )


def _parse_trigger_body(event: dict) -> dict:
    raw = event.get("body")
    if raw is None or raw == "":
        return {}
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _handle_vuln_trigger(event: dict, context) -> dict:  # noqa: ANN001
    from lambda_discovery import handler as discovery_handler

    body = _parse_trigger_body(event)
    discovery_event = {}
    if "max_findings" in body:
        discovery_event["max_findings"] = body["max_findings"]
    result = discovery_handler(discovery_event, context)
    return {"statusCode": 200, "body": result}


def handler(event, context):  # noqa: ANN001
    path = _request_path(event)
    if path.rstrip("/").rsplit("/", 1)[-1] == "vuln-trigger":
        return _handle_vuln_trigger(event, context)

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
