#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
from typing import Any, Dict

HOST = "0.0.0.0"
PORT = 8081
DATA_DIR = Path("/data")
EVENTS_FILE = DATA_DIR / "events.jsonl"

SENSITIVE_KEY_RE = re.compile(r"(key|token|secret|password|authorization)", re.IGNORECASE)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            if SENSITIVE_KEY_RE.search(k):
                out[k] = "[REDACTED]"
            else:
                out[k] = redact(v)
        return out
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str) and ("sk-" in value or "Bearer " in value):
        return "[REDACTED]"
    return value


def append_event(event: Dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    envelope = {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "event": redact(event),
    }
    with EVENTS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(envelope, ensure_ascii=True) + "\n")


class Handler(BaseHTTPRequestHandler):
    server_version = "audit-server/1.0"

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(200, {"ok": True})
            return
        if self.path == "/summary":
            count = 0
            if EVENTS_FILE.exists():
                with EVENTS_FILE.open("r", encoding="utf-8") as f:
                    for _ in f:
                        count += 1
            self._json(200, {"ok": True, "events": count})
            return
        self._json(404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:
        if self.path != "/ingest":
            self._json(404, {"ok": False, "error": "not_found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self._json(400, {"ok": False, "error": "invalid_json"})
            return
        append_event(payload)
        self._json(200, {"ok": True})

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _json(self, code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"audit server listening on {HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
