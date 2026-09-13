"""Best-effort request telemetry for the game museum server."""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


_WRITE_LOCK = threading.Lock()
_INTEGER_PATH = re.compile(r"/\d+(?=/|$)")


def normalize_route(target: str) -> str:
    """Return a stable route label without query strings or resource IDs."""
    path = urlsplit(target).path or "/"
    if path.startswith("/api/game/"):
        return "/api/game/:path"
    if path.startswith("/api/tools/"):
        return "/api/tools/:path"
    if path.startswith("/game/"):
        return "/game/:path"
    if path.startswith("/media/"):
        return "/media/:path"
    if path.startswith("/assets/"):
        return "/assets/:path"
    return _INTEGER_PATH.sub("/:id", path)


def should_log(route: str) -> bool:
    """Exclude static resources and browser metadata from business telemetry."""
    return not (
        route == "/favicon.ico"
        or route.startswith(("/assets/", "/media/", "/static/"))
    )


def request_bytes(headers: Any) -> int:
    try:
        return max(0, int(headers.get("Content-Length", "0")))
    except (TypeError, ValueError):
        return 0


class CountingWriter:
    """Delegate a response stream while counting body bytes written."""

    def __init__(self, wrapped: Any) -> None:
        self._wrapped = wrapped
        self.bytes_written = 0

    def write(self, data: bytes) -> int:
        self.bytes_written += len(data)
        return self._wrapped.write(data)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)


class RequestLogger:
    def __init__(self, root: Path, app_id: str) -> None:
        self.directory = root / "server_logs" / app_id / "raw"

    def record(
        self,
        *,
        method: str,
        target: str,
        status: int,
        request_size: int,
        response_size: int,
        started_at: float,
    ) -> None:
        route = normalize_route(target)
        if not should_log(route):
            return
        event = {
            "timestamp": datetime.fromtimestamp(started_at).astimezone().isoformat(),
            "method": method,
            "route": route,
            "status": int(status),
            "request_bytes": int(request_size),
            "response_bytes": int(response_size),
            "latency_ms": round(max(0.0, (time.time() - started_at) * 1000), 3),
        }
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.directory / f"{datetime.fromtimestamp(started_at).date().isoformat()}.jsonl"
            line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
            with _WRITE_LOCK:
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(line)
        except Exception:
            # Telemetry must never break a business request.
            return
