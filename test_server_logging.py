import json
import tempfile
import unittest
from pathlib import Path

from server_logging import RequestLogger, normalize_route, should_log


class ServerLoggingTests(unittest.TestCase):
    def test_normalizes_business_routes_and_removes_query(self):
        self.assertEqual(normalize_route("/api/game/SNES/Mario?q=1"), "/api/game/:path")
        self.assertEqual(normalize_route("/game/SNES/Mario"), "/game/:path")
        self.assertEqual(normalize_route("/items/123"), "/items/:id")

    def test_excludes_static_resources(self):
        self.assertFalse(should_log("/assets/app.js"))
        self.assertFalse(should_log("/media/cover.png"))
        self.assertFalse(should_log("/favicon.ico"))
        self.assertTrue(should_log("/api/games"))

    def test_writes_expected_event(self):
        with tempfile.TemporaryDirectory() as directory:
            logger = RequestLogger(Path(directory), "game_museum")
            logger.record(
                method="POST",
                target="/api/games?source=test",
                status=201,
                request_size=42,
                response_size=128,
                started_at=1_800_000_000.0,
            )
            files = list((Path(directory) / "server_logs/game_museum/raw").glob("*.jsonl"))
            self.assertEqual(len(files), 1)
            event = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(
                {key: event[key] for key in ("method", "route", "status", "request_bytes", "response_bytes")},
                {
                    "method": "POST",
                    "route": "/api/games",
                    "status": 201,
                    "request_bytes": 42,
                    "response_bytes": 128,
                },
            )
            self.assertIn("latency_ms", event)


if __name__ == "__main__":
    unittest.main()
