from __future__ import annotations

import json
import os
import time

from lambda_poller import handler as poller_handler


def main() -> None:
    interval = float(os.getenv("LOCAL_POLLER_INTERVAL_SECONDS", "30"))
    print("local poller started", flush=True)
    while True:
        try:
            result = poller_handler({}, None)
            print(json.dumps({"action": "poll", "result": result}, sort_keys=True), flush=True)
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"action": "poll_failed", "error": str(exc)}, sort_keys=True), flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    main()
