"""HTTP contract for the persisted data-health report."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("data_health_http_host", ROOT / "server.py")
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)


class QuietHandler(host.Handler):
    def log_message(self, *_args):
        pass


class DataHealthHttpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="data-health-http-")
        folder = Path(self.temporary.name)
        host.APP_DIR = folder / "app"
        host.APP_DIR.mkdir()
        host.PROJECT_DIR = host.APP_DIR
        host.DATA_DIR = folder / "runtime"
        host.ORIGINAL_DIR = folder / "originals"
        host.ORIGINAL_DIR.mkdir()
        host.EXPORT_DIR = folder / "exports"
        host.EXPORT_DIR.mkdir()
        host.SAMPLE = host.ORIGINAL_DIR / "missing.xlsx"
        host.DESKTOP_MODE = False
        for name in ("HISTORY_STORES", "EXPORT_SERVICES", "MIGRATION_SERVICES",
                     "EXPORT_PREVIEWS", "DATASET_SERVICES", "OPERATIONS_STORES", "WRITEBACK_SERVICES"):
            getattr(host, name).clear()
        self.server = host.ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def test_report_route_reads_saved_snapshot(self):
        snapshot = host.history_store().save({
            "filename": "覆盖样例.xlsx",
            "period": "2026-11",
            "asOf": "2026-11-01",
            "products": [{"id": "P1", "plan": 10, "completed": 8,
                          "stockSmall": 3, "salesQuantity": None}],
            "summary": {"achievement": 0.8, "stockAmount": 30},
            "sources": [{"role": "summary", "sheet": "计划", "records": 1,
                         "startDate": "2026-11-01", "endDate": "2026-11-01"}],
            "issues": [],
        })
        request = Request(self.base + "/api/health/report?snapshotId=" + snapshot["id"])
        with urlopen(request, timeout=10) as response:
            self.assertEqual(response.status, 200)
            payload = json.loads(response.read())
        self.assertEqual(payload["snapshotId"], snapshot["id"])
        self.assertEqual(payload["scope"]["productCount"], 1)
        self.assertEqual(next(item for item in payload["metrics"] if item["key"] == "stockCoverage")["status"], "blocked")


if __name__ == "__main__":
    unittest.main(verbosity=2)
