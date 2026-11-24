"""Create a shareable local diagnostic bundle from bounded metadata."""
from contextlib import closing
from datetime import datetime
import json
import re
from pathlib import Path
import sqlite3
import stat
import sys
from tempfile import NamedTemporaryFile
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from . import __version__
from .character_embedding import safe_path
from .models import RuntimeErrorBase, timestamp

_LOG_SOURCES = ("service", "astrbot", "napcat")
_TAIL_BYTES = 256 * 1024
_LIMIT = 100
_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_COMPONENTS = {"api", "host", "delivery", "aiohttp.server", "aiohttp.access", "asyncio", "root"}
_STATUSES = {"queued", "running", "completed", "failed", "interrupted", "cancelled",
             "waiting_quota", "blocked_unknown", "pending", "received", "sending"}
_CODES = {
    "library_event", "operation_failed", "host_failure", "delivery_reconciliation_waiting",
    "model_not_configured", "provider_timeout", "provider_unreachable", "provider_invalid_response",
    "provider_http_400", "provider_http_401", "provider_http_402", "provider_http_403",
    "provider_http_404", "provider_http_422", "provider_http_429", "provider_http_500",
    "provider_http_502", "provider_http_503", "provider_http_504",
    "desktop_network_wait", "desktop_storage_wait", "desktop_connection_changed",
    "desktop_settlement_pending", "desktop_receipt_requires_review", "generation_retry_sources_changed",
    "organization_network_wait", "organization_model_changed", "organization_policy_changed",
    "organization_model_not_configured", "service_restarted", "service_draining", "cancelled_by_user",
    "request_failed", "request_interrupted", "reply_output_truncated", "reply_schema_invalid",
    "provider_content_filtered", "deepseek_activation_required", "roleplay_prompts_review_required",
    "roleplay_official_deepseek_required", "roleplay_story_memory_required", "roleplay_input_too_long",
}
_ERRORS = {"RuntimeError", "RuntimeErrorBase", "ModelError", "ValueError", "TypeError", "KeyError",
           "TimeoutError", "ConnectionError", "ClientConnectionError", "ClientConnectorError",
           "ClientConnectorCertificateError", "ClientResponseError", "PermissionError", "FileNotFoundError",
           "OSError", "OperationalError", "DatabaseError", "CancelledError", "JSONDecodeError"}


def _label(value, choices, default="other"):
    return value if isinstance(value, str) and value in choices else default


def _date(value):
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        result = datetime.fromisoformat(value)
        return result.isoformat(timespec="seconds") if result.tzinfo else None
    except ValueError:
        return None


def _regular(home, path):
    path = safe_path(home, path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise OSError("diagnostic_source_not_regular")
    return path


def _log_tail(path, source):
    with path.open("rb") as stream:
        offset = max(0, stream.seek(0, 2) - _TAIL_BYTES)
        stream.seek(offset)
        lines = stream.read(_TAIL_BYTES).split(b"\n")
    for line in reversed(lines[bool(offset):-1]):
        try:
            data = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(data, dict):
            continue
        at = _date(data.get("at"))
        level = _label(data.get("level"), _LEVELS, None)
        if at and level:
            yield {"at": at, "source": source, "level": level,
                   "component": _label(data.get("component"), _COMPONENTS, "service_library"),
                   "event": _label(data.get("event"), _CODES),
                   "error_type": _label(data.get("error_type"), _ERRORS, None)}


def _events(paths):
    events, source_count = [], 0
    for source in _LOG_SOURCES:
        recent = []
        for suffix in ("", ".1", ".2", ".3"):
            try:
                path = _regular(paths.home, paths.directories()["logs"] / (source + ".jsonl" + suffix))
                recent.extend(_log_tail(path, source))
                source_count += 1
            except (OSError, RuntimeErrorBase):
                continue
            if len(recent) >= _LIMIT:
                break
        events.extend(sorted(recent, key=lambda event: event["at"])[-_LIMIT:])
    return sorted(events, key=lambda event: event["at"]), source_count


def _operations(paths):
    result = {"database": "unavailable", "schema": None, "chat": [], "background": [], "public_search": []}
    try:
        path = _regular(paths.home, paths.directories()["database"] / "runtime.sqlite3")
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.2)) as db:
            result["schema"] = db.execute("PRAGMA user_version").fetchone()[0]
            for key, table in (("chat", "turns"), ("background", "organization_tasks")):
                rows = db.execute(f"SELECT updated_at, status, error_code FROM {table} ORDER BY rowid DESC LIMIT ?",
                                  (_LIMIT,))
                result[key] = [{"at": _date(at), "status": _label(status, _STATUSES),
                                "error": _label(error, _CODES) if error is not None else None}
                               for at, status, error in rows]
            result["database"] = "readable"
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='knowledge_control'").fetchone():
                from .community_lookup import outcome
                rows = db.execute("""SELECT t.updated_at, json_extract(k.value,'$.data.retrieval')
                    FROM knowledge_control k JOIN turns t ON k.key='knowledge.conversation.record.'||t.id
                    WHERE json_extract(k.value,'$.data.retrieval.executor')='community-explicit-search-v1'
                    ORDER BY t.rowid DESC LIMIT ?""", (_LIMIT,))
                for at, raw in rows:
                    retrieval = json.loads(raw)
                    error = next(iter(retrieval.get("errors", [])), None)
                    status = int(error[9:]) if error and re.fullmatch(r"web_http_[1-5][0-9]{2}",error) else None
                    result["public_search"].append({"at":_date(at), "outcome":outcome(retrieval),
                        "http_status":status, "error":_label(error,_CODES | {
                            "community_search_consent_required","native_public_task_scope_changed",
                            "provider_network_error","web_transport_unconfirmed","organization_effect_unknown"
                        }) if error and status is None else None})
    except (OSError, RuntimeErrorBase, sqlite3.Error):
        pass
    return result


def _write(folder, filename, contents):
    temp = None
    try:
        with NamedTemporaryFile(prefix=".pcar-log-", suffix=".tmp", dir=folder, delete=False) as handle:
            temp = Path(handle.name)
        with ZipFile(temp, "w", compression=ZIP_DEFLATED) as bundle:
            for name, value in contents.items():
                bundle.writestr(name, value)
        target = folder / filename
        temp.replace(target)
        return target
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def export_logs(paths, service_state=None, key_state=None):
    from .deepseek_download import windows_downloads

    events, sources = _events(paths)
    operations = _operations(paths)
    version = sys.getwindowsversion() if sys.platform == "win32" else None
    edition = "community" if Path(__file__).with_name("community-edition").is_file() else "development"
    manifest = {"format": "pcar-support-logs-v1", "created_at": timestamp(), "version": __version__,
                "edition": edition, "os": sys.platform, "os_build": version.build if version else None,
                "python": ".".join(map(str, sys.version_info[:3])), "log_files_read": sources,
                "service_state": _label(service_state, {"starting", "online", "disconnected",
                                                        "wrong_instance", "draining", "repairing"}, "unknown"),
                "key_state": _label(key_state, {"verified", "unverified", "missing", "invalid_key",
                                                "proxy_required", "network_error"}, "unknown"),
                "events": len(events), "operation_schema": operations["schema"]}
    contents = {"manifest.json": json.dumps(manifest, indent=2) + "\n",
                "events.jsonl": "".join(json.dumps(event) + "\n" for event in events),
                "recent-operations.json": json.dumps(operations, indent=2) + "\n"}
    filename = "PCAR-logs-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:10] + ".zip"
    try:
        downloads = windows_downloads()
        if downloads.is_absolute() and not str(downloads).startswith(chr(92)*2):
            return _write(downloads, filename, contents)
    except (OSError, AttributeError, RuntimeErrorBase):
        pass
    try:
        exports = safe_path(paths.home, paths.directories()["exports"])
        exports.mkdir(parents=True, exist_ok=True)
        return _write(exports, filename, contents)
    except (OSError, RuntimeErrorBase):
        raise RuntimeErrorBase("support_log_export_failed") from None
