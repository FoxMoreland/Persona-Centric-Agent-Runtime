"""Release announcement tests with local files and an offline feed double."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from persona_runtime.release_notices import ASSETS, Notices, entries, release_metadata, version_parts


class AnnouncementTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.home = Path(root.name)
        metadata = release_metadata()
        bundled = entries(json.loads((ASSETS / "announcements.json").read_bytes()))
        self.version = max((row["version"] for row in bundled.values() if row["kind"] == "release"),
                           key=version_parts)
        # Exercise the published snapshot while the next candidate is still under review.
        version = patch("persona_runtime.release_notices.release_metadata",
                        return_value=metadata | {"version": self.version})
        version.start()
        self.addCleanup(version.stop)
        self.current = "release-" + self.version
        self.today = datetime.now(timezone(timedelta(hours=8))).date()
        self.notices = Notices(self.home, "windows")

    def entry(self, **properties):
        return {"id":"notice-001", "kind":"notice", "date":self.today.isoformat(), "platforms":["windows"],
                "title":"001", "items":["002"]} | properties

    def test_current_release_shows_once_offline_and_history_is_retained(self):
        initial = self.notices.view()
        self.assertEqual(initial["startup"], [self.current])
        self.assertIn("release-1.1.1", [row["id"] for row in initial["entries"]])
        self.notices.acknowledge(self.current)
        self.notices.acknowledge(self.current)
        self.assertEqual(Notices(self.home,"windows").view()["startup"], [])
        self.assertEqual(Notices(self.home,"android").view()["startup"], [])
        self.assertNotIn("release-1.0.0", [row["id"] for row in Notices(self.home,"android").view()["entries"]])

    def test_ordinary_and_expiring_author_notices(self):
        ordinary = self.entry()
        important = self.entry(id="notice-002", versions=[self.version], important_until=self.today.isoformat())
        expired = self.entry(id="notice-003", date=(self.today-timedelta(days=3)).isoformat(),
                             versions=[self.version], important_until=(self.today-timedelta(days=2)).isoformat())
        with patch("persona_runtime.release_notices.fetch", return_value={"schema":1,"entries":[ordinary,important,expired]}):
            view = self.notices.view(True)
        self.assertEqual(view["freshness"], "updated")
        self.assertEqual(view["startup"], [self.current,"notice-002"])
        with patch("persona_runtime.release_notices.fetch", side_effect=OSError):
            offline = self.notices.view(True)
        self.assertEqual(offline["freshness"], "unavailable")
        self.assertEqual(offline["entries"], view["entries"])
        self.notices.acknowledge("notice-002")
        with patch("persona_runtime.release_notices.fetch", return_value={"schema":1,"entries":[important | {"items":["003"]}]}):
            corrected = self.notices.view(True)
        self.assertNotIn("notice-002", corrected["startup"])

    def test_invalid_feed_keeps_previous_cache(self):
        good = {"schema":1,"entries":[self.entry()]}
        with patch("persona_runtime.release_notices.fetch", return_value=good):
            before = self.notices.view(True)
        with patch("persona_runtime.release_notices.fetch", return_value={"schema":1,"entries":[self.entry(items=[""])]}):
            after = self.notices.view(True)
        self.assertEqual(before["entries"], after["entries"])
        with self.assertRaises(ValueError):
            entries({"schema":1,"entries":[self.entry(important_until="2030-01-01")]})
        with self.assertRaises(ValueError):
            self.notices.acknowledge("notice-does-not-exist")

    def test_unannounced_candidate_keeps_published_history(self):
        major, minor, patch_number = version_parts(self.version)
        candidate = f"{major}.{minor}.{patch_number + 1}"
        with patch("persona_runtime.release_notices.release_metadata", return_value={"version": candidate}):
            view = self.notices.view()
        self.assertEqual(view["version"], candidate)
        self.assertEqual(view["startup"], [])
        self.assertIn(self.current, [row["id"] for row in view["entries"]])
