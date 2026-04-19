from __future__ import annotations

import json
import os
import time

from aws_runtime import dequeue_work_item, load_runtime_settings
from lambda_worker import handler as worker_handler


def main() -> None:
    settings = load_runtime_settings()
    if settings.get("backend") != "local":
        raise SystemExit("local_worker requires RUNTIME_BACKEND=local")

    poll_interval = float(os.getenv("LOCAL_WORKER_POLL_INTERVAL_SECONDS", "2"))
    print("local worker started", flush=True)
    while True:
        message = dequeue_work_item(settings)
        if not message:
            time.sleep(poll_interval)
            continue
        payload = message["body"]
        event = {"Records": [{"body": json.dumps(payload)}]}
        try:
            result = worker_handler(event, None)
            print(json.dumps({"message_id": message["message_id"], "result": result}, sort_keys=True), flush=True)
        except Exception as exc:  # noqa: BLE001
            print(
                json.dumps(
                    {
                        "message_id": message["message_id"],
                        "error": str(exc),
                        "action": "worker_failed",
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            time.sleep(poll_interval)


if __name__ == "__main__":
    main()
