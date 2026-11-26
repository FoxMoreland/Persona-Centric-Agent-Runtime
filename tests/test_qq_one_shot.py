"""Exercise the one-shot acceptance against a local OneBot test double."""

import asyncio
from argparse import Namespace
from contextlib import redirect_stdout, suppress
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import web

from persona_runtime import qq_one_shot as acceptance
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.paths import RuntimePaths


class QQOneShotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        sandbox = self.enterContext(tempfile.TemporaryDirectory())
        self.home = Path(sandbox).resolve() / "test-persona"
        self.paths = RuntimePaths(self.home)
        acceptance.initialize(self.home)
        self.sender = "12345"
        self.recipient = "23456"
        self.version = {"app_name": "SnowLuma", "app_version": "2.2.0-node",
                        "protocol_version": "v11"}
        self.auth_enabled = True
        self.online = True
        self.reply = {"message_id": 45678}
        self.retcode = 0
        self.pause_send = False
        self.sends = []
        self.send_entered = asyncio.Event()
        self.release_send = asyncio.Event()
        app = web.Application()
        app.router.add_post("/{action}", self.handle)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        # Release a pending send before runner cleanup waits for its handler.
        self.addAsyncCleanup(self.release_send.set)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        artifact = Path(sandbox) / "fixture-artifact"
        artifact.write_bytes(b"onebot test fixture")
        self.binding = Namespace(
            sender=self.sender, recipient=self.recipient, port=runner.addresses[0][1],
            client_artifact=artifact, client_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
        )

    async def handle(self, request):
        action = request.match_info["action"]
        header = request.headers.get("Authorization")
        if self.auth_enabled and header != "Bearer local-test":
            raise web.HTTPForbidden()
        if action == "get_version_info":
            data = self.version
        elif action == "get_login_info":
            data = {"user_id": int(self.sender)}
        elif action == "get_status":
            data = {"online": self.online}
        elif action == "send_msg":
            attempt = acceptance.load(self.paths)["attempt"]
            self.sends.append((await request.json(), attempt))
            self.send_entered.set()
            if self.pause_send:
                await self.release_send.wait()
            return web.json_response({"status": "failed" if self.retcode else "ok",
                                      "retcode": self.retcode, "data": self.reply})
        else:
            raise web.HTTPNotFound()
        return web.json_response({"status": "ok", "retcode": 0, "data": data})

    def bind(self):
        return acceptance.bind(self.paths, self.binding)

    async def run_operation(self, operation="send", token="local-test"):
        return await acceptance.run_online(self.paths, operation, token)

    async def test_preflight_and_single_dispatch_with_separate_recipient_receipt(self):
        self.bind()
        state_bytes = acceptance.state_path(self.paths).read_bytes()
        proof = await self.run_operation("preflight")
        self.assertEqual(proof["preflight"], "passed")
        self.assertEqual(acceptance.state_path(self.paths).read_bytes(), state_bytes)
        self.assertEqual(self.sends, [])

        state = await self.run_operation()
        preview = acceptance.summary(state)
        self.assertEqual(preview["result"], "awaiting_recipient_confirmation")
        self.assertEqual(preview["attempt"]["platform_message_id"], "45678")
        body, durable = self.sends[0]
        self.assertEqual(durable["dispatch"], "dispatching")
        self.assertEqual(body, {"message_type": "private", "self_id": int(self.sender),
                                "user_id": int(self.recipient), "message": [
            {"type": "text", "data": {"text": f"PCAR QQ {durable['code']}"}},
        ]})
        with self.assertRaisesRegex(acceptance.AcceptanceError, "attempt_already_recorded"):
            await self.run_operation()
        with self.assertRaisesRegex(acceptance.AcceptanceError, "code_mismatch"):
            acceptance.confirm_received(self.paths, "wrong-code")
        confirmed = acceptance.confirm_received(self.paths, durable["code"])
        self.assertEqual(acceptance.summary(confirmed)["result"], "received")
        self.assertEqual(len(self.sends), 1)

    async def test_invalid_and_repeated_binding(self):
        self.binding.recipient = self.sender
        with self.assertRaisesRegex(acceptance.AcceptanceError, "binding_invalid"):
            self.bind()
        self.binding.recipient = self.recipient
        self.binding.client_sha256 = "0" * 64
        with self.assertRaisesRegex(acceptance.AcceptanceError, "client_hash_mismatch"):
            self.bind()
        self.binding.client_sha256 = hashlib.sha256(self.binding.client_artifact.read_bytes()).hexdigest()
        self.bind()
        with self.assertRaisesRegex(acceptance.AcceptanceError, "already_bound"):
            self.bind()
        self.assertEqual(self.sends, [])

    async def test_wrong_account(self):
        self.bind()
        self.sender = "34567"
        with self.assertRaisesRegex(acceptance.AcceptanceError, "sender_mismatch"):
            await self.run_operation()
        self.assertIsNone(acceptance.load(self.paths)["attempt"])
        self.assertEqual(self.sends, [])

    async def test_non_numeric_port_in_local_state_is_rejected(self):
        state = self.bind()
        state["binding"]["port"] = "80@invalid.example"
        acceptance.atomic_json(acceptance.state_path(self.paths), state)
        with self.assertRaisesRegex(acceptance.AcceptanceError, "binding_invalid"):
            await self.run_operation()
        self.assertEqual(self.sends, [])

    async def test_protocol_mismatch(self):
        self.bind()
        self.version["protocol_version"] = "v12"
        with self.assertRaisesRegex(acceptance.AcceptanceError, "client_version_mismatch"):
            await self.run_operation()
        self.assertEqual(self.sends, [])

    async def test_snowluma_version_mismatch(self):
        self.bind()
        self.version["app_version"] = "2.4.2-node"
        with self.assertRaisesRegex(acceptance.AcceptanceError, "client_version_mismatch"):
            await self.run_operation()
        self.assertEqual(self.sends, [])

    async def test_missing_or_wrong_authentication(self):
        self.bind()
        with self.assertRaisesRegex(acceptance.AcceptanceError, "authentication_failed"):
            await self.run_operation(token="wrong")
        self.auth_enabled = False
        with self.assertRaisesRegex(acceptance.AcceptanceError, "authentication_not_enforced"):
            await self.run_operation()
        self.assertEqual(self.sends, [])

    async def test_cli_refuses_echoing_token_input(self):
        self.bind()
        output = io.StringIO()
        args = ["qq_one_shot", "--home", str(self.home), "send"]
        with (patch.object(sys, "argv", args), redirect_stdout(output),
              patch.object(acceptance.getpass, "getpass", side_effect=acceptance.getpass.GetPassWarning)):
            self.assertEqual(acceptance.main(), 1)
        self.assertIn("qq_acceptance_hidden_token_input_required", output.getvalue())
        self.assertEqual(self.sends, [])

    async def test_offline(self):
        self.bind()
        self.online = False
        with self.assertRaisesRegex(acceptance.AcceptanceError, "client_offline"):
            await self.run_operation()
        self.assertEqual(self.sends, [])

    async def test_platform_error_stops_and_preserves_attempt(self):
        self.bind()
        self.retcode = 1200
        state = await self.run_operation()
        self.assertEqual(state["attempt"]["bridge"], "onebot_action_failed:1200")
        with self.assertRaisesRegex(acceptance.AcceptanceError, "attempt_already_recorded"):
            await self.run_operation()
        self.assertEqual(len(self.sends), 1)

    async def test_recipient_can_confirm_when_platform_id_missing(self):
        self.bind()
        self.reply = {}
        state = await self.run_operation()
        self.assertEqual(acceptance.summary(state)["result"], "delivery_uncertain")
        received = acceptance.confirm_received(self.paths, state["attempt"]["code"])
        self.assertEqual(acceptance.summary(received)["result"], "received_without_bridge_receipt")

    async def test_timeout_is_not_retried(self):
        self.bind()
        self.pause_send = True
        with patch.object(acceptance, "SEND_TIMEOUT", 0.3):
            state = await self.run_operation()
        self.assertEqual(state["attempt"]["bridge"], "onebot_timeout")
        with self.assertRaisesRegex(acceptance.AcceptanceError, "attempt_already_recorded"):
            await self.run_operation()
        self.assertEqual(len(self.sends), 1)

    async def test_interrupted_send_is_not_replayed_and_parallel_send_is_locked(self):
        self.bind()
        self.pause_send = True
        task = asyncio.create_task(self.run_operation())
        try:
            await asyncio.wait_for(self.send_entered.wait(), timeout=3)
            with self.assertRaises(RuntimeErrorBase):
                await self.run_operation()
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        state = acceptance.load(RuntimePaths(self.home))
        self.assertEqual(state["attempt"]["dispatch"], "dispatching")
        self.assertEqual(acceptance.summary(state)["result"], "delivery_uncertain")
        with self.assertRaisesRegex(acceptance.AcceptanceError, "attempt_already_recorded"):
            await acceptance.run_online(RuntimePaths(self.home), "send", "local-test")
        self.assertEqual(len(self.sends), 1)

    async def test_existing_directory_cannot_be_initialized(self):
        state_bytes = acceptance.state_path(self.paths).read_bytes()
        with self.assertRaises(FileExistsError):
            acceptance.initialize(self.home)
        self.assertEqual(acceptance.state_path(self.paths).read_bytes(), state_bytes)

    async def test_process_exit_releases_lock_without_replaying_send(self):
        self.bind()
        self.pause_send = True
        runner = (
            "import asyncio, json, sys; from pathlib import Path; "
            "sys.path[:] = json.loads(sys.argv[2]); "
            "from persona_runtime.qq_one_shot import run_online; "
            "from persona_runtime.paths import RuntimePaths; "
            "asyncio.run(run_online(RuntimePaths(Path(sys.argv[1])), 'send', 'local-test'))"
        )
        # On Windows the venv executable can be a redirector with a child
        # interpreter. Kill the actual interpreter which owns the lock.
        process = await asyncio.create_subprocess_exec(
            sys._base_executable, "-B", "-c", runner, str(self.home), json.dumps(sys.path))
        try:
            await asyncio.wait_for(self.send_entered.wait(), timeout=10)
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()
        with self.assertRaisesRegex(acceptance.AcceptanceError, "attempt_already_recorded"):
            await self.run_operation()
        self.assertEqual(acceptance.load(self.paths)["attempt"]["dispatch"], "dispatching")
        self.assertEqual(len(self.sends), 1)
