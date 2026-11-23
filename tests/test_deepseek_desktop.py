import asyncio
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from aiohttp.test_utils import TestClient, TestServer

from persona_runtime.config import ConfigStore
from persona_runtime.corpus_files import CorpusError
from persona_runtime.deepseek_browser_bridge import ORIGIN, deliver, serve
from persona_runtime.deepseek_export import inspect
from persona_runtime.deepseek_intake import DeepSeekInbox
from persona_runtime.desktop_activation import DesktopActivation
from persona_runtime.import_library import ImportLibrary
from persona_runtime.import_markdown import parse
from persona_runtime.paths import RuntimePaths
from persona_runtime.server import create_app


def write_export(path, head=3, hazardous=False):
    sample = "\u74f6\n\n## 2\n\n> 3\n\n" + chr(96)*3 + "\n4"
    def message(number, parent, role, fragment, body, status="FINISHED"):
        return {"id": number, "parent_id": parent, "role": role, "status": status,
                "inserted_at": "2026-11-21T12:30:01.123456789Z",
                "fragments": [{"type": fragment, "content": body}]}
    source = {"id": "scope-1", "title": "007", "current_message_id": head, "messages": [
        message(1, None, "USER", "REQUEST", "01"),
        message(2, 1, "ASSISTANT", "RESPONSE", "02"),
        message(3, 1, "ASSISTANT", "RESPONSE", sample),
        message(4, 3, "ASSISTANT", "THINK", "03", "INCOMPLETE")
    ]}
    with ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"export_format_version": 3}))
        archive.writestr("chat_session_index.json", json.dumps([{"id":"scope-1","title":"007"}]))
        archive.writestr("chat_sessions/scope-1.json", json.dumps(source))
        if hazardous:
            archive.writestr("../01", b"01")
    return sample


class DeepSeekArchiveTest(unittest.TestCase):
    def setUp(self):
        base = tempfile.TemporaryDirectory()
        self.addCleanup(base.cleanup)
        self.paths = RuntimePaths(Path(base.name))
        self.file = Path(base.name) / "deepseek.zip"

    def test_official_chain_and_markdown_boundaries(self):
        body = write_export(self.file)
        self.assertEqual(inspect(self.file)["conversations"][0]["id"], "scope-1")
        self.assertTrue(inspect(self.file, "scope-2")["missing"])
        data = inspect(self.file, "scope-1")
        parsed = parse(data["markdown"])
        self.assertEqual([row["original"] for row in parsed["rows"]], ["01", body])
        self.assertEqual(parsed["rows"][1]["date"], "2026-11-21T12:30:01.123456+00:00")
        self.assertIn(b'INCOMPLETE', data["raw"])
        with self.assertRaises(CorpusError):
            parse(data["markdown"][:-1])
        library = ImportLibrary(self.paths)
        first = library.add(data["markdown"], deepseek_source=(data["raw"], data["conversation"]))
        second = library.add(data["markdown"], deepseek_source=(data["raw"], data["conversation"]))
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["mapping"], {"用户": "A", "DeepSeek": "B"})
        self.assertEqual(first["deepseek"]["branch"], 3)
        self.assertEqual(len(library.list()), 1)

    def test_choose_branches_and_reject_path_escape(self):
        write_export(self.file, head=None)
        self.assertEqual({row["id"] for row in inspect(self.file, "scope-1")["branches"]}, {2, 4})
        self.assertEqual(inspect(self.file, "scope-1", 2)["conversation"]["branch"], 2)
        write_export(self.file, hazardous=True)
        with self.assertRaises(CorpusError):
            inspect(self.file)

    def test_bridge_replay_leaves_user_file_in_place(self):
        write_export(self.file)
        inbox = DeepSeekInbox(self.paths)
        job = "a"*32
        self.assertEqual(inbox.enqueue(self.file, "scope-1", job=job), "pending")
        self.assertEqual(inbox.pending()["target"], "scope-1")
        inbox.finish(job, "imported")
        self.assertEqual(inbox.enqueue(self.file, "scope-1", job=job), "imported")
        self.assertIsNone(inbox.pending())
        self.assertTrue(self.file.is_file())

    def test_native_handoff_gates_and_remembers_target(self):
        write_export(self.file)
        config = ConfigStore(self.paths)
        config.initialize()
        settings = {"edition": "development", "home": str(self.paths.home)}
        request = {"version": 1, "action": "remember", "id": "a"*32, "target": "scope-1"}
        inbox = DeepSeekInbox(self.paths)
        with patch("persona_runtime.deepseek_browser_bridge._launch"):
            self.assertEqual(deliver(settings, request)["state"], "key_required")
            self.assertIsNone(inbox.preferred())
            with patch.object(DesktopActivation, "status", return_value={"state":"verified"}):
                self.assertEqual(deliver(settings, request)["state"], "remembered")
                self.assertEqual(inbox.preferred()["target"], "scope-1")
                file_request = request | {"action": "deliver", "path": str(self.file)}
                self.assertEqual(deliver(settings, file_request)["state"], "pending")
                inbox.finish(request["id"], "imported")
                self.assertIsNone(inbox.preferred())
                self.assertEqual(deliver(settings, file_request)["state"], "imported")

    def test_native_bridge_imports_in_a_process_without_console_streams(self):
        import subprocess
        import sys

        subprocess.run(
            [sys.executable, "-B", "-c",
             "import sys; sys.stdin = sys.stdout = None; "
             "from persona_runtime.deepseek_browser_bridge import ORIGIN, serve; "
             "assert serve('community', ORIGIN) == 1"],
            check=True, timeout=15,
        )

    def test_browser_download_watcher_queues_only_new_completed_exports(self):
        from persona_runtime.deepseek_download import DeepSeekDownloadWatch

        folder = self.paths.home / "Downloads"
        folder.mkdir()
        before = folder / "deepseek_data-2026-11-20.zip"
        write_export(before)
        guard = []
        watcher = DeepSeekDownloadWatch(self.paths, folder, lambda: guard.append(True), lambda: None)
        write_export(folder / "unrelated.zip")
        temporary = folder / "deepseek_data-2026-11-23.zip.crdownload"
        write_export(temporary)
        self.assertFalse(watcher.poll())
        self.assertFalse(watcher.poll())
        export = temporary.with_suffix("")
        temporary.rename(export)
        self.assertFalse(watcher.poll())
        self.assertTrue(watcher.poll())
        self.assertEqual(guard, [True])
        self.assertEqual(Path(watcher.inbox.pending()["path"]), export.resolve())
        self.assertEqual(watcher.inbox.pending()["target"], None)
        watcher.inbox.finish(watcher.inbox.pending()["id"], "imported")
        self.assertTrue(export.is_file())

    def test_markdown_review_edits_only_the_selected_text(self):
        from persona_runtime.deepseek_export import revise_markdown

        original_body = write_export(self.file)
        result = inspect(self.file, "scope-1")
        rendered = revise_markdown(result["markdown"], "scope-1", 3, {"2": "## 10\n20"})
        self.assertEqual([row["original"] for row in parse(rendered)["rows"]], ["01", "## 10\n20"])
        self.assertEqual(parse(result["markdown"])["rows"][1]["original"], original_body)
        self.assertEqual(parse(rendered)["rows"][1]["date"], parse(result["markdown"])["rows"][1]["date"])
        with self.assertRaises(CorpusError):
            revise_markdown(result["markdown"], "scope-1", 3, {"1": ""})
        with self.assertRaises(CorpusError):
            revise_markdown(result["markdown"], "scope-1", 3, {"3": "ignored"})

    def test_native_message_framing(self):
        payload = json.dumps({"version": 1, "action": "probe"}).encode()
        output = io.BytesIO()
        with patch("persona_runtime.deepseek_browser_bridge._folder", return_value=self.paths.home):
            (self.paths.home / "instance.json").write_text('{"edition":"development"}')
            self.assertEqual(serve("development", ORIGIN, io.BytesIO(struct.pack("<I", len(payload)) + payload), output), 0)
        self.assertEqual(json.loads(output.getvalue()[4:]), {"state": "ready", "edition": "development"})
        self.assertEqual(serve("development", "https://chat.deepseek.com/", io.BytesIO(), io.BytesIO()), 1)


class DeepSeekActivationTest(unittest.IsolatedAsyncioTestCase):
    async def test_lock_validate_reject_clear_and_restart(self):
        async def verify(key):
            return {"state": "verified", "balance_available": False} if key == "0001" else {"state": "invalid_key"}

        with tempfile.TemporaryDirectory() as directory:
            paths = RuntimePaths(Path(directory))
            config = ConfigStore(paths)
            config.initialize()
            gate = DesktopActivation(config, verify)
            app = create_app(paths, activation=gate)
            async with TestClient(TestServer(app)) as client:
                headers = {"Authorization": "Bearer " + config.token()}
                async def manage(action, **args):
                    async with client.post("/v1/manage", json={"action":action, "args":args}, headers=headers) as response:
                        return response.status, await response.json()
                self.assertEqual((await manage("persona.list"))[0], 423)
                self.assertEqual((await manage("desktop.activation.verify",key="0000"))[1]["state"], "invalid_key")
                self.assertFalse(config.load()["model"]["encrypted_key"])
                self.assertEqual((await manage("desktop.activation.verify",key="0001"))[1], {"state":"verified","balance_available":False})
                self.assertEqual(DesktopActivation(config).status()["state"], "verified")
                self.assertEqual((await manage("persona.list"))[0], 200)
                self.assertEqual((await manage("desktop.activation.verify",key="0002"))[1]["state"], "invalid_key")
                self.assertEqual(gate.status()["state"], "verified")
                gate.invalidate(config.load()["model"] | {"encrypted_key":"other"})
                self.assertEqual(gate.status()["state"], "verified")
                gate.invalidate(config.load()["model"])
                self.assertEqual((await manage("persona.list"))[0], 423)
                self.assertEqual((await manage("desktop.activation.verify"))[1]["state"], "verified")
                self.assertEqual((await manage("desktop.activation.clear"))[1]["state"], "missing")
                self.assertEqual((await manage("persona.list"))[0], 423)


class DeepSeekBrowserTests(unittest.TestCase):
    def test_verification_has_separate_resource_policy(self):
        from persona_runtime.deepseek_webview import ASSET_HOSTS, CHAT_HOST, VERIFICATION_HOSTS, approved, approved_resource

        host = next(iter(VERIFICATION_HOSTS))
        origin = "https://" + host
        self.assertTrue(approved_resource(origin + "/site/version/challenge.js", "Script", "GET"))
        self.assertTrue(approved_resource(origin + "/site/version/inputs", "XmlHttpRequest", "GET"))
        self.assertTrue(approved_resource(origin + "/site/version/mp_verify", "XmlHttpRequest", "POST"))
        self.assertFalse(approved_resource(origin + "/site/version/challenge.js", "Document", "GET"))
        self.assertFalse(approved_resource(origin + "/site/version/mp_verify", "XmlHttpRequest", "GET"))
        self.assertFalse(approved_resource(origin + "/site/version/other.js", "Script", "GET"))
        self.assertFalse(approved_resource("https://another.token.awswaf.com/challenge.js", "Script", "GET"))
        self.assertFalse(approved_resource(origin + ".example.org/challenge.js", "Script", "GET"))
        self.assertFalse(approved_resource(origin + ":444/challenge.js", "Script", "GET"))
        self.assertFalse(approved(origin, ASSET_HOSTS))
        self.assertFalse(approved(origin, {CHAT_HOST}))

    def test_hcaptcha_loads_only_as_verified_embedded_resources(self):
        from persona_runtime.deepseek_webview import ASSET_HOSTS, CHAT_HOST, approved, approved_frame, approved_resource

        sdk = "https://js.hcaptcha.com/1/api.js"
        widget = "https://newassets.hcaptcha.com/captcha/v1/example/static/hcaptcha.html"
        api = "https://api.hcaptcha.com"
        self.assertTrue(approved_resource(sdk + "?onload=loaded", "Script", "GET"))
        self.assertTrue(approved_resource(widget, "Document", "GET"))
        self.assertTrue(approved_frame(widget))
        self.assertTrue(approved_frame("about:blank"))
        self.assertTrue(approved_frame("about:srcdoc"))
        self.assertTrue(approved_resource("https://newassets.hcaptcha.com/c/example/hsw.js", "Script", "GET"))
        self.assertTrue(approved_resource(api + "/checksiteconfig", "XmlHttpRequest", "POST"))
        self.assertTrue(approved_resource(api + "/getcaptcha/example", "XmlHttpRequest", "POST"))
        self.assertTrue(approved_resource(api + "/checkcaptcha/example", "XmlHttpRequest", "POST"))
        self.assertTrue(approved_resource("https://imgs.hcaptcha.com/example.jpg", "Image", "GET"))
        self.assertTrue(approved_resource("https://imgs3.hcaptcha.com/tip/example.webm", "Media", "GET"))
        self.assertTrue(approved_resource("https://api2.hcaptcha.com/getcaptcha/example", "XmlHttpRequest", "OPTIONS"))
        self.assertTrue(approved_resource("https://hcaptcha.com/getcaptcha/example", "XmlHttpRequest", "POST"))
        self.assertFalse(approved_resource("https://hcaptcha.com/", "Document", "GET"))
        self.assertFalse(approved(sdk, ASSET_HOSTS))
        self.assertFalse(approved(widget, {CHAT_HOST}))
        self.assertFalse(approved_frame("https://newassets.hcaptcha.com/unrelated.html"))
        self.assertFalse(approved_frame(widget.replace("https:", "http:")))
        self.assertFalse(approved_frame(widget.replace(".com/", ".com.example.org/")))
        self.assertFalse(approved_resource(api + "/siteverify", "XmlHttpRequest", "POST"))
        self.assertFalse(approved_resource(api + "/getcaptcha/example", "Document", "GET"))
        self.assertFalse(approved_resource("https://someone.w.hcaptcha.com/logo.png", "Image", "GET"))

    def test_turnstile_is_allowed_in_frames_but_not_as_a_page_or_download(self):
        from persona_runtime.deepseek_webview import ASSET_HOSTS, CHAT_HOST, approved, approved_frame, approved_resource

        sdk = "https://challenges.cloudflare.com/turnstile/v0/api.js?onload=loaded"
        widget = "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/g/turnstile/f/av0/example"
        api = "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/g/flow/example"
        self.assertTrue(approved_resource(sdk, "Script", "GET"))
        self.assertTrue(approved_resource("https://challenges.cloudflare.com/turnstile/v0/b/version/api.js", "Script", "GET"))
        self.assertTrue(approved_frame(widget))
        self.assertTrue(approved_resource(widget, "Document", "GET"))
        self.assertTrue(approved_resource(api, "XmlHttpRequest", "POST"))
        self.assertFalse(approved(widget, {CHAT_HOST}))
        self.assertFalse(approved(widget, ASSET_HOSTS))
        self.assertFalse(approved_frame(api))
        self.assertFalse(approved_frame(widget.replace("https:", "http:")))
        self.assertFalse(approved_frame(widget.replace(".com/", ".com.example.org/")))
        self.assertFalse(approved_resource(sdk, "Document", "GET"))
        self.assertFalse(approved_resource(api, "Document", "GET"))
        self.assertFalse(approved_resource("https://challenges.cloudflare.com/unrelated.js", "Script", "GET"))

    def test_challenge_pending_and_timeout_have_visible_status(self):
        from types import SimpleNamespace
        from persona_runtime.deepseek_webview import ExportBrowser

        timer = SimpleNamespace(Enabled=True)
        timer.Stop = lambda: setattr(timer, "Enabled", False)
        browser = ExportBrowser.__new__(ExportBrowser)
        browser.page_timer = timer
        browser.navigation_id = 1
        browser.status = SimpleNamespace(Text="")
        browser.document_loaded = False
        browser.load_deadline = 0

        browser._navigation_completed(None, SimpleNamespace(NavigationId=1, HttpStatusCode=202, IsSuccess=True))
        self.assertIn("访问验证", browser.status.Text)
        self.assertTrue(timer.Enabled)
        self.assertFalse(browser.document_loaded)
        browser._check_page(None, None)
        self.assertFalse(timer.Enabled)
        self.assertIn("系统浏览器", browser.status.Text)

    def test_failed_navigation_reports_error(self):
        from types import SimpleNamespace
        from persona_runtime.deepseek_webview import ExportBrowser

        timer = SimpleNamespace(Enabled=True)
        timer.Stop = lambda: setattr(timer, "Enabled", False)
        browser = ExportBrowser.__new__(ExportBrowser)
        browser.page_timer = timer
        browser.navigation_id = 3
        browser.status = SimpleNamespace(Text="")
        result = SimpleNamespace(NavigationId=3, HttpStatusCode=403, IsSuccess=False)
        browser._navigation_completed(None, result)
        self.assertFalse(timer.Enabled)
        self.assertIn("HTTP 403", browser.status.Text)
