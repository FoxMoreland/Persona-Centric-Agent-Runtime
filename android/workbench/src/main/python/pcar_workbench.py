"""The private Android workbench host and its explicit WebView operations."""

import asyncio
import json
import os
from pathlib import Path
import socket
from tempfile import NamedTemporaryFile
from threading import Event, Thread

from aiohttp import web

from persona_runtime import community
from persona_runtime.android_platform import bind
from persona_runtime.android_embedding import prepare_packaged_model
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.instance import InstanceLock
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.paths import RuntimePaths
from persona_runtime.server import create_app
from persona_runtime.workbench_appearance import ACCENT, APPEARANCE_FILE, APPEARANCE_MODES, appearance_settings
from persona_runtime.workbench_import import WorkbenchData


PAGE_METHODS = frozenset("""
    release_announcements acknowledge_announcement
    commit_import_scope import_action import_deepseek_session live_ack_submissions live_activate
    live_batch_action live_branch live_cancel live_confirm_inline_main live_create live_draft live_health
    live_location live_lookup_confirm live_memory live_message_position live_network_wake live_page
    live_people live_persona_image live_privacy live_quota_decide live_quota_settings live_rename_session live_reply_detail
    live_save_package live_package_sessions live_preview_package_export live_discard_package live_jev
    live_select live_set_buffer live_stage_retry live_submissions live_submit presentation_attach
    presentation_begin presentation_decide presentation_reconcile revise_deepseek_markdown template_spec
""".split())
BACKGROUND_WRITES = frozenset(("live_location", "live_draft", "presentation_decide", "acknowledge_announcement"))
_runtime = None


class WorkbenchRuntime:
    def __init__(self, paths, foreground):
        self.paths = paths
        self.foreground = foreground
        self.ready = Event()
        self.failure = None
        self.loop = None
        self.app = None
        self.selected_archive = None
        self.thread = Thread(target=self._run, name="pcar-android-runtime", daemon=True)

    def _run(self):
        try:
            self.paths.initialize()
            lock = InstanceLock(self.paths.home / "runtime/service.lock")
            try:
                prepare_packaged_model(self.paths)
                asyncio.run(self._serve())
            finally:
                lock.close()
        except Exception as error:
            self.failure = error
        finally:
            self.loop = None
            self.app = None
            self.ready.set()

    async def _serve(self):
        config = ConfigStore(self.paths)
        current = config.initialize()
        reserved = {current[k] for k in ("napcat_port", "astrbot_port", "onebot_port", "snowluma_port")}
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            # Reserve the port before recording it. Only the site takes over this socket.
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            if port in reserved:
                raise RuntimeErrorBase("android_runtime_port_conflict")
            config.update({"service_port": port})
            app = create_app(self.paths, foreground_only=True, interactive_maintenance=True)
            self.app = app
            self.loop = asyncio.get_running_loop()
            self._apply_foreground()
            runner = web.AppRunner(app, access_log=None, shutdown_timeout=65)
            await runner.setup()
            try:
                await web.SockSite(runner, listener).start()
                self.data = WorkbenchData(self.paths, None)
                self.ready.set()
                await app["stop"].wait()
            finally:
                await runner.cleanup()

    def _apply_foreground(self):
        app = self.app
        if app is None:
            return
        app["engine"].draining = not self.foreground
        app["disposition_compiler"].draining = not self.foreground
        if self.foreground:
            app["ingress"].network_wake()

    def set_foreground(self, foreground):
        self.foreground = foreground
        loop = self.loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._apply_foreground)

    def require_ready(self):
        if not self.ready.wait(30):
            raise RuntimeErrorBase("android_runtime_starting")
        if self.failure is not None:
            raise RuntimeErrorBase("android_runtime_start_failed") from self.failure
        if self.app is None:
            raise RuntimeErrorBase("android_runtime_disconnected")


def start(host, home, foreground):
    global _runtime
    if _runtime is None:
        bind(host)
        os.environ["PCAR_EDITION"] = "community"
        os.environ["PERSONA_RUNTIME_HOME"] = home
        _runtime = WorkbenchRuntime(RuntimePaths(Path(home)), foreground)
        _runtime.thread.start()
    _runtime.require_ready()
    _runtime.set_foreground(foreground)


def set_foreground(value):
    if _runtime is not None:
        _runtime.set_foreground(value)


def encode(data):
    return json.dumps(data, ensure_ascii=False, allow_nan=False)


def dispatch(method, serialized_args):
    _runtime.require_ready()
    if not _runtime.foreground and method not in BACKGROUND_WRITES:
        raise RuntimeErrorBase("android_workbench_background")
    args = json.loads(serialized_args)
    if not isinstance(args, list):
        raise ValueError("invalid_bridge_arguments")
    if method in PAGE_METHODS:
        return encode(getattr(_runtime.data, method)(*args))
    if method == "appearance" and not args:
        return encode(appearance_settings(_runtime.paths))
    if method in ("save_appearance", "save_accent") and len(args) == 1:
        value = args[0]
        current = appearance_settings(_runtime.paths)
        if method == "save_appearance" and value in APPEARANCE_MODES:
            current["mode"] = value
        elif method == "save_accent" and (value is None or isinstance(value, str) and ACCENT.fullmatch(value)):
            current["accent"] = value.lower() if value else None
        else:
            raise ValueError("invalid_appearance")
        atomic_json(_runtime.paths.home / APPEARANCE_FILE, current, durable=True)
        return encode(True)
    raise ValueError("android_bridge_method_unavailable")


def accept_file(kind, path):
    _runtime.require_ready()
    if kind in ("transcript", "scoped_transcript"):
        return encode(_runtime.data.read_transcript_file(path, kind == "scoped_transcript"))
    if kind == "character":
        return encode(_runtime.data.read_character_file(path))
    if kind == "deepseek":
        selected = encode(_runtime.data._set_deepseek_file(path))
        previous = _runtime.selected_archive
        _runtime.selected_archive = Path(path)
        if previous is not None:
            try:
                previous.unlink(missing_ok=True)
            except OSError:
                pass
        return selected
    raise ValueError("android_document_kind_unavailable")


def make_export(kind, serialized_args, directory):
    _runtime.require_ready()
    args = json.loads(serialized_args)
    if kind == "export_support_logs" and len(args) == 2:
        from persona_runtime.diagnostic_export import export_logs
        return str(export_logs(_runtime.paths, *args, output_folder=directory))
    if kind == "live_export_package" and len(args) in (1, 2):
        from persona_runtime.character_pack import copy_file
        source = _runtime.data._stage_package_export(*args)
        try:
            with NamedTemporaryFile(dir=directory, prefix="pcar-export-", suffix=source.suffix, delete=False) as stream:
                output = Path(stream.name)
                try:
                    copy_file(source, stream)
                    return str(output)
                except BaseException:
                    stream.close()
                    output.unlink(missing_ok=True)
                    raise
        finally:
            source.unlink(missing_ok=True)
    if kind == "save_deepseek_markdown" and len(args) == 1:
        raw = _runtime.data.read_deepseek_markdown(args[0])
        with NamedTemporaryFile(dir=directory, prefix="pcar-export-", suffix=".md", delete=False) as stream:
            try:
                stream.write(raw)
            except OSError:
                stream.close()
                Path(stream.name).unlink(missing_ok=True)
                raise
            return stream.name
    raise ValueError("android_export_kind_unavailable")


def change_key(key, confirmation=None):
    _runtime.require_ready()
    if not isinstance(key, str) or not 1 <= len(key) <= 512 or any(c.isspace() for c in key):
        raise ValueError("invalid_deepseek_key")
    value = _runtime.data.live_connection_change("main", community.BASE, community.PRO, key, confirmation)
    return encode({"confirmation": value["confirmation"], "affected": len(value["affected"])})


def key_configured():
    _runtime.require_ready()
    return ConfigStore(_runtime.paths).public()["model"]["has_key"]
