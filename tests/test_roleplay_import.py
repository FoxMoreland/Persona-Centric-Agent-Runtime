"""Desktop roleplay and import bounds, using only local numeric fixtures."""
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from persona_runtime.chat import ChatEngine, ModelCompletion
from persona_runtime.config import ConfigStore
from persona_runtime.corpus_files import digest
from persona_runtime.deepseek_export import revise_markdown
from persona_runtime.import_card_publish import CardPublisher
from persona_runtime.import_library import ImportLibrary
from persona_runtime.import_markdown import parse
from persona_runtime.import_personas import PersonaImports
from persona_runtime.import_plan import parse_source
from persona_runtime.import_scope import selected
from persona_runtime.ingress import IngressService
from persona_runtime.models import PRIVATE_CHAT, ROLEPLAY, PersonaSpec, RuntimeErrorBase, identifier
from persona_runtime.organization import OrganizationEngine
from persona_runtime.paths import RuntimePaths
from persona_runtime.roleplay_prompts import APPROVAL_REQUIRED, OFFICIAL, PURPOSES, VERSION as PROMPTS_VERSION
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store
from persona_runtime.workbench_import import WorkbenchData
from tests.test_import_card_publish import Materials
from tests.test_import_prompts import LocalModel

BODY = "（" + "901。" * 32 + "）\n\n**902**"


def make_zip(path, count):
    nodes = []
    for i in range(1, count + 1):
        user = i % 2
        fragment = {"type": "REQUEST" if user else "RESPONSE", "content": f"（{i}）"}
        nodes.append({"id": i, "parent_id": i - 1 or None, "role": "USER" if user else "ASSISTANT",
                      "status": "FINISHED",
                      "fragments": [fragment] if user else [fragment, {"type": "THINK", "content": "999999"}]})
    with ZipFile(path, "w") as bundle:
        bundle.writestr("manifest.json", json.dumps({"export_format_version": 3}))
        bundle.writestr("chat_session_index.json", json.dumps([{"id": "1", "title": "120"}]))
        bundle.writestr("chat_sessions/1.json", json.dumps({"id": "1", "current_message_id": count, "messages": nodes}))


class ScopeTests(unittest.TestCase):
    def test_md_last_messages_mode_and_readback(self):
        raw = ("# 01\r\n由 Moect 导出\r\n\r\n" + "\r\n".join(
            f"[2026-09-21T12:00:00] {i%2}\r\n（{i}）\r\n" for i in range(1, 10001))).encode("utf-16")
        source = {"raw": raw, "parsed": parse(raw)}
        with self.assertRaisesRegex(RuntimeErrorBase, "5000"):
            selected(source, None, ROLEPLAY, False)
        md, _ = selected(source, 5000, ROLEPLAY, False)
        parsed = parse_source(md)
        self.assertEqual(parsed["chat_mode"], ROLEPLAY)
        self.assertEqual(parsed["format"], "Moect Markdown")
        self.assertEqual(len(parsed["rows"]), 5000)
        self.assertEqual(parsed["rows"][0]["text"], "（5001）")
        self.assertEqual(parsed["rows"][-1]["text"], "（10000）")
        first = parsed["rows"][0]
        self.assertEqual(md.decode()[first["start"]:first["end"]], first["original"])
        md, _ = selected(source, 5001, PRIVATE_CHAT, True)
        self.assertEqual(len(parse(md)["rows"]), 5001)
        for count in (False, 0, -1, 10001, 1.5):
            with self.assertRaisesRegex(RuntimeErrorBase, "条数"):
                selected(source, count, PRIVATE_CHAT, True)

    def test_zip_stage_does_not_insert_until_confirmed(self):
        with TemporaryDirectory() as folder:
            paths = RuntimePaths(Path(folder))
            workbench = WorkbenchData(paths, lambda: None)
            archive = paths.home / "data.zip"
            make_zip(archive, 5002)
            with patch.object(workbench, "_require_open"):
                workbench._set_deepseek_file(archive)
                scope = workbench.import_deepseek_session("1")["scope"]
                self.assertEqual(scope["total"], 5002)
                self.assertEqual(workbench._library.list(), [])
                self.assertRaisesRegex(RuntimeErrorBase, "5000", workbench.commit_import_scope,
                                       scope["token"], None, ROLEPLAY)
                self.assertEqual(workbench._library.list(), [])
                data = workbench.commit_import_scope(scope["token"], 100, ROLEPLAY)
                md = workbench._library.row(data["id"])["raw"]
                self.assertEqual(len(parse(md)["rows"]), 100)
                self.assertEqual(parse(md)["rows"][0]["original"], "（4903）")
                self.assertNotIn(b"999999", md)
                original, _ = workbench._library.deepseek_source(data["id"])
                self.assertEqual(original, b"")
                edited = revise_markdown(md, "1", 5002, {"1": "（101）\n> 102"})
                self.assertEqual(parse_source(edited)["rows"][0]["text"], "（101）\n> 102")
                self.assertEqual(parse(edited)["chat_mode"], ROLEPLAY)

    def test_modes_stored_per_persona_and_old_spec_keeps_wire_format(self):
        with TemporaryDirectory() as folder:
            paths = RuntimePaths(Path(folder))
            config = ConfigStore(paths)
            config.initialize()
            store = Store(paths)
            store.initialize()
            library = ImportLibrary(paths)
            source = b"## X\n101\n## Y\n201"
            md, _ = selected({"raw": source, "parsed": parse(source)}, None, ROLEPLAY, False)
            doc = library.add(md)
            plan_id = "1"*64
            jobs = Materials(doc["id"], plan_id)
            publisher = CardPublisher(store, library, jobs)
            pid, = publisher.create(doc["id"], 1, plan_id, 3, ["A"], "101", confirmed=True)
            spec = RuntimeService(store).show_persona(pid)["spec"]
            self.assertEqual(spec["chat_mode"], ROLEPLAY)
            self.assertNotIn("chat_mode", replace(PersonaSpec.from_dict(spec), chat_mode=PRIVATE_CHAT).to_dict())
            imports = PersonaImports(store, config)
            planned = imports.profiles.plan(doc["id"], 1, doc["mapping"], {})
            self.assertFalse(planned["plan"]["configured"])
            with self.assertRaisesRegex(RuntimeErrorBase, "待审阅"):
                imports.analyze(doc["id"], planned["revision"], True, planned["plan"]["id"])


class Model:
    def __init__(self):
        self.calls = []

    async def complete(self, model, messages):
        self.calls.append(messages)
        return ModelCompletion(BODY, {"total_tokens": 1}, {"finish_reason": "stop"})


class MemoryModel(Model):
    async def complete(self, model, messages):
        self.calls.append(messages)
        data = json.loads(messages[-1]["content"])
        event = next(e for e in data["events"] if e["role"] != "user" and "901" in e["text"])
        sources = [{"source_id": event["id"], "quote": "901"}]
        if "proposals" in data:
            result = {"format_version": 3, "decisions": [
                {"id": "101", "action": "publish", "reason": "101", "subject": "901", "attribute": "01",
                 "target_memory_id": None, "effective_at": None, "valid_until": None, "sources": sources}]}
        else:
            result = {"format_version": 3, "summary": [], "candidates": [
                {"id": "101", "category": "world_knowledge", "text": "901", "subject": "901", "attribute": "01",
                 "sources": sources}]}
        return ModelCompletion(json.dumps(result), {"total_tokens": 1}, {"finish_reason": "stop"})


class RoleplayRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        root = TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.paths = RuntimePaths(Path(root.name))
        self.config = ConfigStore(self.paths)
        self.config.initialize()
        self.config.update({"model": {"name": "deepseek-v4-pro", "base_url": OFFICIAL, "api_key": "100"},
                            "organization": {"reuse_chat": True}})
        self.store = Store(self.paths)
        self.store.initialize()
        self.service = RuntimeService(self.store)
        self.pid = self.service.create_persona(PersonaSpec(1, "101", "102", ["103"], "104", ["105"], "106",
                                                            chat_mode=ROLEPLAY))["id"]
        self.service.use_persona(self.pid)
        self.transport = Model()
        self.chat = ChatEngine(self.store, self.config, self.transport)
        self.ingress = IngressService(self.chat)
        self.sid = self.chat.session()["session_id"]

    async def asyncTearDown(self):
        await self.chat.wait()
        await self.chat.scene.close()
        self.chat.cognitive.characters.close()

    def approve_fixture(self):
        root = ImportLibrary(self.paths).root
        root.mkdir(parents=True, exist_ok=True)
        bundle = {"version": PROMPTS_VERSION} | {p: f"JSON {i}" for i, p in enumerate(PURPOSES, 1001)}
        raw = json.dumps(bundle).encode()
        (root/(PROMPTS_VERSION + ".json")).write_bytes(raw)
        (root/"roleplay-approval.json").write_text(json.dumps({"decision": "approved", "sha256": digest(raw),
            "version": PROMPTS_VERSION, "base_url": OFFICIAL, "purposes": list(PURPOSES)}), encoding="utf-8")
        return bundle

    def request(self, text):
        return {"channel": "desktop", "persona_id": self.pid, "session_id": self.sid,
                "text": text, "request_id": identifier()}

    async def send(self, text):
        receipt = self.ingress.accept(self.request(text))
        await self.chat.wait()
        return self.chat.get(receipt["turn_id"])

    async def test_pending_stays_local_and_approved_story_preserves_body(self):
        with self.assertRaisesRegex(RuntimeErrorBase, APPROVAL_REQUIRED):
            self.ingress.accept(self.request("（110）"))
        self.assertEqual(self.transport.calls, [])
        bundle = self.approve_fixture()
        first = await self.send("（110）")
        self.assertEqual(first["status"], "completed", first.get("error_code"))
        self.assertEqual(first["display_reply"], BODY)
        self.assertNotIn("display_bubbles", first)
        self.assertTrue(self.transport.calls[0][0]["content"].startswith(bundle["reply"] + "\n"))
        self.assertEqual(self.transport.calls[0][-1]["content"], "（110）")
        second = await self.send("（112）")
        self.assertEqual(second["status"], "completed", second.get("error_code"))
        self.assertIn({"role": "assistant", "content": BODY}, self.transport.calls[-1])
        self.assertEqual(self.transport.calls[-1][-1]["content"], "（112）")

    async def test_approved_import_uses_roleplay_extraction_and_card_contracts(self):
        bundle = self.approve_fixture()
        transport = LocalModel()
        imports = PersonaImports(self.store, self.config, transport)
        raw = b"## X\n101\n## Y\n201"
        scoped, _ = selected({"raw": raw, "parsed": parse(raw)}, None, ROLEPLAY, False)
        doc = imports.library.add(scoped)
        planned = imports.profiles.plan(doc["id"], doc["revision"], doc["mapping"], {})
        self.assertTrue(planned["plan"]["configured"])
        imports.analyze(doc["id"], planned["revision"], True, planned["plan"]["id"], compose_card=True)
        await imports._card_chains[doc["id"]]
        systems = [request["messages"][0]["content"] for request in transport.requests]
        self.assertEqual(systems, [bundle["profile_extract"], bundle["profile_extract"], bundle["card_compose"]])

    async def test_story_facts_admit_assistant_text_and_recall_inside_persona(self):
        bundle = self.approve_fixture()
        self.assertEqual((await self.send("（110）"))["status"], "completed")
        transport = MemoryModel()
        org = OrganizationEngine(self.store, self.config, transport, self.chat)
        policy = org.managed.current_policy()
        org.managed.update_policy(self.pid, dict(policy["data"], memory_automation=True),
                                  policy["id"], self.pid, identifier())
        for _ in range(8):
            org.automatic.tick(force=True)
            if not await org.run_next():
                break
        self.assertEqual([m[0]["content"] for m in transport.calls],
                         [bundle["memory_extract"], bundle["memory_verify"]])
        stored = self.service.search_memories()
        self.assertEqual([(r["text"], r["category"]) for r in stored], [("901", "world_knowledge")])
        followup = await self.send("901")
        self.assertEqual(followup["status"], "completed", followup.get("error_code"))
        self.assertEqual(followup["context"]["memories"][0]["id"], stored[0]["id"])
        private = self.service.create_persona(PersonaSpec(1, "201", "202", ["203"], "204", ["205"], "206"))
        self.service.use_persona(private["id"])
        self.assertEqual(self.service.search_memories(), [])
