"""Offline integration of the standard Community search route."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aiohttp.test_utils import TestServer

from persona_runtime.chat import ModelCompletion, ModelError
from persona_runtime.community import BASE, PRO
from persona_runtime.community_lookup import AUTOMATIC, PROTOCOL
from persona_runtime.config import ConfigStore
from persona_runtime.diagnostic_export import _operations
from persona_runtime.models import generic_persona, identifier, RuntimeErrorBase
from persona_runtime.paths import RuntimePaths
from persona_runtime.server import create_app
from persona_runtime.service import RuntimeService
from persona_runtime.session_privacy import status, update
from persona_runtime.sessions import select


class CommunitySearchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        edition = patch.dict("os.environ", {"PCAR_EDITION": "community"})
        edition.start()
        self.addCleanup(edition.stop)
        paths = self.paths = RuntimePaths(Path(directory.name))
        self.config = ConfigStore(paths)
        self.config.initialize()
        self.config.update({"model": {"name":PRO, "base_url":BASE, "api_key":"001", "timeout":4}})
        self.calls = []
        self.reply_inputs = []
        self.result = "found"
        self.decisions = []
        self.choice = "none"
        self.revoke_during_decision = False
        flash = patch("persona_runtime.community_decision.Transport", self.flash_transport)
        flash.start()
        self.addCleanup(flash.stop)
        app = create_app(paths, client=self, foreground_only=True, interactive_maintenance=True)
        self.chat = app["engine"]
        self.store = app["store"]
        self.pid = RuntimeService(self.store).create_persona(generic_persona())["id"]
        with self.store.connection(write=True) as db:
            self.sid = select(db, self.pid, "desktop")
        app["organization"].processing.tools.transport = self.tools
        self.server = TestServer(app)
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)

    async def complete(self, model, messages):
        self.reply_inputs.append(messages)
        return ModelCompletion("405", {"total_tokens": 1}, {"finish_reason": "stop"})

    async def tools(self, stage, values, model):
        self.calls.append((stage, values))
        if self.result == "failed":
            raise ModelError("web_http_403", diagnostics={"request_headers_sent": True, "http_status":403})
        if self.result == "network":
            raise ModelError("provider_network_error", diagnostics={"request_headers_sent": False})
        rows = [{"url":"https://example.com/001","title":"103","content":"904"}] if self.result == "found" else []
        return ModelCompletion(json.dumps({"results": rows}), {"http_status":200},
                               {"request_headers_sent": True, "http_status":200})

    def flash_transport(self, model):
        test = self

        class Flash:
            async def complete(self, payload, *, on_dispatch, on_receipt, diagnostics):
                from persona_runtime.community_decision import PROFILE

                test.decisions.append(payload["state"])
                on_dispatch(1)
                if test.revoke_during_decision:
                    test.grant(False, AUTOMATIC)
                on_receipt(2)
                return 200, json.dumps({"decision_source": PROFILE, "answers": {"route": {"choice": test.choice}}})

            async def close(self):
                pass

        return Flash()

    def grant(self, enabled=True, protocol=PROTOCOL):
        field = "community_auto_search" if protocol == AUTOMATIC else "community_search"
        current = status(self.store, self.config, self.pid, self.sid)[field]
        self.assertTrue(current["available"])
        return update(self.store, self.config, self.pid, self.sid, enabled, current["revision"],
                      current["recipient_hash"], identifier(), protocol)

    async def submit(self, grant=None):
        task = ({"question":"102", "request_search":True, "community_consent":{
            "revision":grant["revision"], "recipient_hash":grant["recipient_hash"]}} if grant else None)
        turn = await self.chat.submit("desktop", "101 102 103", identifier(),
                                      session_id=self.sid, persona_id=self.pid, public_task=task)
        await asyncio.wait_for(self.chat.wait(), 14)
        return self.chat.get(turn["id"])

    async def test_plain_chat_and_explicit_search_are_separate(self):
        grant = self.grant()
        self.assertEqual((await self.submit())["status"], "completed")
        self.assertEqual(self.calls, [])
        turn = await self.submit(grant)
        self.assertEqual(turn["status"], "completed", turn.get("error_code"))
        self.assertEqual(self.calls, [("search", {"query":"102","count":8,"need_content":True})])
        self.assertIn("904", self.reply_inputs[-1][0]["content"])
        receipts = _operations(self.paths)["public_search"]
        self.assertEqual([row["outcome"] for row in receipts], ["found"])
        self.assertNotIn("904", json.dumps(receipts))
        self.assertEqual(self.decisions, [])

    async def test_automatic_search_requires_a_positive_decision_and_live_consent(self):
        self.choice = "web"
        self.assertEqual((await self.submit())["status"], "completed")
        self.assertEqual(self.decisions, [])
        self.grant(protocol=AUTOMATIC)
        self.choice = "none"
        self.assertEqual((await self.submit())["status"], "completed")
        self.assertEqual(self.calls, [])
        self.choice = "web"
        turn = await self.submit()
        self.assertEqual(turn["status"], "completed", turn.get("error_code"))
        self.assertEqual(self.calls, [("search", {"query": "101 102 103", "count": 8, "need_content": True})])
        self.assertIn("904", self.reply_inputs[-1][0]["content"])
        self.assertEqual(len(self.decisions), 2)
        self.assertEqual(self.decisions[-1]["recent_context"], [])
        self.assertFalse(self.decisions[-1]["explicit_public_query_requested"])
        self.assertEqual(_operations(self.paths)["public_search"][0]["outcome"], "found")

    async def test_revoke_while_automatic_decision_is_in_flight(self):
        self.grant(protocol=AUTOMATIC)
        self.choice = "web"
        self.revoke_during_decision = True
        self.assertEqual((await self.submit())["status"], "completed")
        self.assertEqual(len(self.decisions), 1)
        self.assertEqual(self.calls, [])

    async def test_automatic_search_uses_authorized_jev_and_preserves_unknown_dispatch(self):
        from persona_runtime.community_jev import Settings
        from tests.test_community_jev import Endpoint

        jev_calls, replies = [], []
        def endpoint(model):
            return Endpoint(model, jev_calls, replies)

        self.chat.decision_providers.transport = endpoint
        settings = Settings(self.store, self.config, transport=endpoint)
        checked = await settings.check("https://example.test/api", "jev-1.14", "202", 0)
        self.assertTrue(checked["choice_verified"])
        settings.activate(checked["token"], 0)
        grant = settings.status(self.pid, self.sid)["session"]
        self.assertTrue(settings.grant(self.pid, self.sid, True, grant["revision"], grant["recipient"])["authorized"])
        self.grant(protocol=AUTOMATIC)
        jev_calls.clear()
        self.assertEqual((await self.submit())["status"], "completed")
        self.assertEqual(len(self.calls), 1)
        replies.append("unknown")
        await self.submit()
        self.assertEqual(len(jev_calls), 2)
        self.assertEqual(self.decisions, [])
        self.assertEqual(len(self.calls), 1)

    async def test_workbench_normal_send_routes_to_automatic_search(self):
        from persona_runtime.workbench_import import WorkbenchData

        self.config.update({"service_port": self.server.port})
        self.grant(protocol=AUTOMATIC)
        self.choice = "web"
        workbench = WorkbenchData(self.paths, lambda: None)
        accepted = await asyncio.to_thread(workbench.live_submit, self.pid, self.sid, "101 102 103", identifier())
        self.assertEqual(accepted["state"], "accepted", accepted)

        async def wait_for_search():
            while not _operations(self.paths)["public_search"]:
                await asyncio.sleep(.15)
        await asyncio.wait_for(wait_for_search(), 15)
        self.assertEqual(self.calls, [("search", {"query": "101 102 103", "count": 8, "need_content": True})])

    async def test_empty_and_http_failure_are_distinct(self):
        grant = self.grant()
        for result in ("empty", "failed", "network"):
            self.result = result
            await self.submit(grant)
        self.assertEqual(len(self.calls),3)
        receipts = _operations(self.paths)["public_search"]
        self.assertEqual([(r["outcome"],r["http_status"]) for r in receipts],
                         [("not_dispatched",None),("failed",403),("empty",None)])

    async def test_revocation_and_key_change_invalidate_queued_permission(self):
        grant = self.grant()
        self.grant(False)
        with self.assertRaisesRegex(RuntimeErrorBase,"community_search_consent_required"):
            await self.submit(grant)
        grant = self.grant()
        self.config.update({"model":{"api_key":"002"}})
        with self.assertRaisesRegex(RuntimeErrorBase,"community_search_consent_required"):
            await self.submit(grant)
        self.assertEqual(self.calls, [])

    async def test_workbench_submission_uses_bound_public_excerpt(self):
        from persona_runtime.workbench_import import WorkbenchData

        self.config.update({"service_port": self.server.port})
        grant = self.grant()
        workbench = WorkbenchData(self.paths, lambda: None)
        accepted = await asyncio.to_thread(workbench.live_submit, self.pid, self.sid, "101 102 103", identifier(),
            None, None, {"question":"102", "community_consent":{
                "revision":grant["revision"], "recipient_hash":grant["recipient_hash"]}})
        self.assertEqual(accepted["state"], "accepted", accepted)
        async def result():
            while not _operations(self.paths)["public_search"]:
                await asyncio.sleep(.15)
        await asyncio.wait_for(result(), 15)
        self.assertEqual(self.calls, [("search", {"query":"102","count":8,"need_content":True})])
