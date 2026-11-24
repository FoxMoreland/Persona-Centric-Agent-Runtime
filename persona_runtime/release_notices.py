"""Packaged release announcements and the public announcement feed."""
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import re
from threading import RLock
import urllib.request

from .config import atomic_json

ASSETS = Path(__file__).with_name("release_assets")
FEED = "https://raw.githubusercontent.com/FoxMoreland/Persona-Centric-Agent-Runtime/main/docs/public/announcements.json"
LIMIT = 256 * 1024
LOCK = RLock()


def version_parts(value):
    if not isinstance(value, str) or not re.fullmatch(r"(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})", value):
        raise ValueError("invalid_release_version")
    return tuple(int(item) for item in value.split("."))


def release_metadata():
    data = json.loads((ASSETS / "version.json").read_bytes())
    version_parts(data["version"])
    if type(data["android_version_code"]) is not int or data["android_version_code"] < 1:
        raise ValueError("invalid_android_version_code")
    return data


def iso_date(value):
    if not isinstance(value, str):
        raise ValueError("invalid_announcement_date")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("invalid_announcement_date")
    return parsed


def entries(data):
    if not isinstance(data, dict) or type(data.get("schema")) is not int or data["schema"] != 1:
        raise ValueError("invalid_announcement_feed")
    rows = data.get("entries")
    if not isinstance(rows, list) or len(rows) > 256:
        raise ValueError("invalid_announcement_feed")
    result = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) - {
                "id", "kind", "version", "date", "title", "platforms", "items", "important_until", "versions", "updated_at"}:
            raise ValueError("invalid_announcement")
        ident = row.get("id")
        if not isinstance(ident, str) or not re.fullmatch("[a-z0-9][a-z0-9.-]{0,87}", ident) or ident in result:
            raise ValueError("invalid_announcement_id")
        iso_date(row.get("date"))
        if "updated_at" in row:
            if iso_date(row["updated_at"]) < iso_date(row["date"]):
                raise ValueError("invalid_announcement_date")
        title, items, platforms = row.get("title"), row.get("items"), row.get("platforms")
        if (not isinstance(title, str) or not 1 <= len(title.strip()) <= 160
                or not isinstance(items, list) or not 1 <= len(items) <= 12
                or any(not isinstance(item, str) or not 1 <= len(item.strip()) <= 800 for item in items)
                or not isinstance(platforms, list) or not platforms
                or any(platform not in ("windows", "android") for platform in platforms)):
            raise ValueError("invalid_announcement_content")
        if row.get("kind") == "release":
            version_parts(row.get("version"))
            if ident != "release-" + row["version"] or {"important_until", "versions"} & row.keys():
                raise ValueError("invalid_release_announcement")
        elif row.get("kind") == "notice":
            if not ident.startswith("notice-") or "version" in row:
                raise ValueError("invalid_author_notice")
            versions = row.get("versions", [])
            if not isinstance(versions, list) or len(versions) > 32:
                raise ValueError("invalid_announcement_versions")
            for version in versions:
                version_parts(version)
            if "important_until" in row:
                if iso_date(row["important_until"]) < iso_date(row["date"]) or not versions:
                    raise ValueError("invalid_announcement_deadline")
        else:
            raise ValueError("invalid_announcement_kind")
        result[ident] = row
    return result


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def fetch():
    from .chat import model_tls_context
    from .network_proxy import _SystemProxyHandler

    opener = urllib.request.build_opener(_SystemProxyHandler(), _NoRedirect(),
                                         urllib.request.HTTPSHandler(context=model_tls_context()))
    request = urllib.request.Request(FEED, headers={"Accept": "application/json"})
    with opener.open(request, timeout=7) as response:
        raw = response.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError("announcement_feed_too_large")
    return json.loads(raw)


class Notices:
    def __init__(self, home, platform):
        self.cache = home / "runtime" / "announcement-cache.json"
        self.receipt = home / "runtime" / "announcement-read.json"
        self.platform = platform

    def _local(self):
        local = entries(json.loads((ASSETS / "announcements.json").read_bytes()))
        try:
            cached = entries(json.loads(self.cache.read_bytes()))
        except (OSError, ValueError):
            cached = {}
        return local | cached

    def _seen(self):
        try:
            data = json.loads(self.receipt.read_bytes())
        except (OSError, ValueError):
            return set()
        if not isinstance(data, list) or any(not isinstance(item, str) for item in data):
            return set()
        return set(data)

    def view(self, refresh=False):
        from .models import RuntimeErrorBase

        freshness = "local"
        if refresh is True:
            try:
                data = fetch()
                entries(data)
                with LOCK:
                    atomic_json(self.cache, data, durable=True)
                freshness = "updated"
            except (OSError, ValueError, RuntimeErrorBase):
                freshness = "unavailable"
        version = release_metadata()["version"]
        today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        with LOCK:
            seen = self._seen()
            visible = [row for row in self._local().values() if self.platform in row["platforms"]
                       and row["date"] <= today
                       and (row["kind"] == "release" or not row.get("versions") or version in row["versions"])]
        visible.sort(key=lambda row: (row["date"], row["id"]), reverse=True)
        current = "release-" + version
        saved_versions = (ident.removeprefix("release-") for ident in seen
                          if re.fullmatch(r"release-(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})", ident))
        newer_read = any(version_parts(saved) > version_parts(version) for saved in saved_versions)
        startup = [row["id"] for row in visible if row["id"] not in seen and
                   ((row["id"] == current and not newer_read) or
                    row["kind"] == "notice" and row.get("important_until", "") >= today)]
        startup.sort(key=lambda ident: ident != current)
        return {"version": version, "entries": visible, "startup": startup, "freshness": freshness}

    def acknowledge(self, ident):
        if not isinstance(ident, str):
            raise ValueError("invalid_announcement_id")
        with LOCK:
            if ident not in self._local():
                raise ValueError("invalid_announcement_id")
            seen = self._seen()
            if ident not in seen:
                atomic_json(self.receipt, sorted(seen | {ident}), durable=True)
        return True
