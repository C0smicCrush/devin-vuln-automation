from __future__ import annotations

import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from lambda_intake import handler as intake_handler


class IntakeHandler(BaseHTTPRequestHandler):
    server_version = "devin-vuln-automation-local"

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") in {"", "/health"}:
            self._write_json(HTTPStatus.OK, {"status": "ok"})
            return
        self._write_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def do_POST(self) -> None:  # noqa: N802
        raw_body = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode("utf-8")
        event = {
            "rawPath": self.path,
            "headers": {key: value for key, value in self.headers.items()},
            "body": raw_body,
            "isBase64Encoded": False,
        }
        try:
            response = intake_handler(event, None)
        except SystemExit as exc:
            self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        status_code = int(response.get("statusCode", HTTPStatus.OK))
        body = response.get("body", {})
        self._write_json(status_code, body)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return

    def _write_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    host = os.getenv("LOCAL_INTAKE_HOST", "0.0.0.0")
    port = int(os.getenv("LOCAL_INTAKE_PORT", "8000"))
    server = ThreadingHTTPServer((host, port), IntakeHandler)
    print(f"local intake listening on http://{host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
