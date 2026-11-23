"""Settings log export uses a fixed set of shareable fields."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from persona_runtime.paths import RuntimePaths
from persona_runtime.workbench_import import WorkbenchData


class DiagnosticExportTests(unittest.TestCase):
    def test_known_folder_contains_filtered_logs_and_operation_codes(self):
        with TemporaryDirectory() as home:
            paths = RuntimePaths(Path(home))
            paths.initialize()
            download = paths.home / "下载"
            download.mkdir()
            sensitive = "fixture-private-123"
            stamp = "2026-11-23T00:00:00+00:00"
            log = paths.directories()["logs"]
            row = {"at": stamp, "level": "ERROR", "component": sensitive, "event": sensitive,
                   "error_type": sensitive, "body": sensitive}
            (log / "service.jsonl").write_text(json.dumps(row) + "\n" + json.dumps(row | {
                "component": "api", "event": "operation_failed", "error_type": "ValueError",
            }) + "\n{", encoding="utf-8")
            (log / "environment-prepare.log").write_text(sensitive, encoding="utf-8")
            (paths.directories()["config"] / "private.json").write_text(sensitive, encoding="utf-8")
            database = paths.directories()["database"] / "runtime.sqlite3"
            with closing(sqlite3.connect(database)) as db:
                for table in ("turns", "organization_tasks"):
                    db.execute(f"CREATE TABLE {table}(updated_at, status, error_code, body)")
                    db.execute(f"INSERT INTO {table} VALUES(?,?,?,?)",
                               (stamp, "failed", "provider_http_429", sensitive))
                db.commit()
            workbench = WorkbenchData(paths, lambda: None)
            with patch("persona_runtime.deepseek_download.windows_downloads", return_value=download):
                result = workbench.export_support_logs()
            saved = Path(result["path"])
            self.assertEqual(saved.parent, download)
            self.assertEqual(saved.name, result["name"])
            with ZipFile(saved) as bundle:
                self.assertEqual(set(bundle.namelist()), {"manifest.json", "events.jsonl", "recent-operations.json"})
                for name in bundle.namelist():
                    self.assertNotIn(sensitive.encode(), bundle.read(name))
                events = [json.loads(line) for line in bundle.read("events.jsonl").splitlines()]
                self.assertEqual([event["event"] for event in events], ["operation_failed", "other"])
                self.assertEqual(json.loads(bundle.read("recent-operations.json"))["chat"][0]["error"],
                                 "provider_http_429")

    def test_missing_downloads_and_database_still_produce_distinct_bundles(self):
        with TemporaryDirectory() as home:
            paths = RuntimePaths(Path(home))
            workbench = WorkbenchData(paths, lambda: None)
            with patch("persona_runtime.deepseek_download.windows_downloads",
                       side_effect=PermissionError("fixture")), ThreadPoolExecutor(2) as workers:
                pending = [workers.submit(workbench.export_support_logs) for _ in range(2)]
                files = [Path(task.result()["path"]) for task in pending]
            self.assertNotEqual(*files)
            self.assertEqual(set(paths.directories()["exports"].iterdir()), set(files))
            self.assertFalse(paths.directories()["database"].exists())
            for saved in files:
                with ZipFile(saved) as bundle:
                    self.assertEqual(bundle.read("events.jsonl"), b"")
                    self.assertEqual(json.loads(bundle.read("recent-operations.json"))["database"], "unavailable")
