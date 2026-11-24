"""Configuration and real dispatch boundaries via opaque, offline transport doubles."""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from persona_runtime import community, community_decisions, community_jev
from persona_runtime.archive import encoded
from persona_runtime.chat import ModelError
from persona_runtime.config import ConfigStore
from persona_runtime.models import RuntimeErrorBase, generic_persona, identifier, timestamp
from persona_runtime.paths import RuntimePaths
from persona_runtime.scene_policy import read, save
from persona_runtime.security import unprotect
from persona_runtime.service import RuntimeService
from persona_runtime.sessions import select
from persona_runtime.storage import Store


class Endpoint:
    def __init__(self, model, calls, replies):
        self.model, self.calls, self.replies = model, calls, replies

    async def complete(self, body, *, on_dispatch, on_receipt, diagnostics):
        self.calls.append((self.model['base_url'], body))
        action = self.replies.pop(0) if self.replies else None
        if action == 'before':
            raise ModelError('provider_network_error', diagnostics={'request_headers_sent': False, 'effect_unknown': False})
        on_dispatch(1)
        if action == 'unknown':
            raise ModelError('jev_transport_unconfirmed', diagnostics={'request_headers_sent': True, 'effect_unknown': True})
        on_receipt(2)
        diagnostics({'total_ms': 1})
        if isinstance(action, int):
            return action, '{}'
        _, expected = community_jev.approved_probe()
        answers = {}
        for key, question in body['questions'].items():
            options = list(question['criteria'])
            choice = expected.get(key, options[0]) if action != 'wrong' else 'B'
            answers[key] = {'type': 'choice', 'choice': choice,
                            'probabilities': {value: float(value == choice) for value in options}}
        name = 'other-model' if action == 'wrong-model' else body['model']
        return 200, encoded({'model': name, 'answers': answers, 'usage': {'total_tokens': 11}})

    async def close(self):
        return None


class OptionalJevTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        env = patch.dict(os.environ, {'PCAR_EDITION': 'community'})
        env.start()
        self.addCleanup(env.stop)
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        paths = RuntimePaths(Path(temp.name))
        self.config = ConfigStore(paths)
        self.config.initialize()
        self.config.update({'model': {'base_url': community.BASE, 'name': community.PRO, 'api_key': '101'}})
        self.store = Store(paths)
        self.store.initialize()
        runtime = RuntimeService(self.store)
        self.pid = runtime.create_persona(generic_persona())['id']
        runtime.use_persona(self.pid)
        with self.store.connection(write=True) as db:
            self.sid = select(db, self.pid, 'desktop')
        self.calls, self.replies, self.flash = [], [], []
        self.settings = community_jev.Settings(self.store, self.config, transport=self.endpoint)
        self.router = community_decisions.TurnDecisions(self.store, self.config, transport=self.endpoint)
        self.addAsyncCleanup(self.router.close)

    def endpoint(self, model):
        return Endpoint(model, self.calls, self.replies)

    async def check(self, key='202', name='jev-1.14', url='https://example.test/api/systemone/'):
        return await self.settings.check(url, name, key, self.settings.status()['revision'])

    async def enable(self, key='202'):
        checked = await self.check(key)
        value = self.settings.activate(checked['token'], self.settings.status()['revision'])
        return value

    def grant(self, enabled=True):
        active = self.settings.status(self.pid, self.sid)['session']
        return self.settings.grant(self.pid, self.sid, enabled, active['revision'], active['recipient'])

    def turn(self):
        tid, now = identifier(), timestamp()
        with self.store.connection(write=True) as db:
            db.execute("""INSERT INTO turns(id,source_key,persona_id,session_id,channel,status,model_snapshot,created_at,updated_at)
                VALUES(?,?,?,?,'desktop','queued','{}',?,?)""", (tid, tid, self.pid, self.sid, now, now))
            community_decisions.freeze(db, self.config, tid, self.pid, self.sid)
        return tid

    def view(self, tid):
        with self.store.connection() as db:
            return community_decisions.turn_view(db, tid)

    async def invoke(self, tid, node='one', check_fallback=None):
        async def flash(body, *, on_dispatch, on_receipt, diagnostics):
            self.flash.append((tid, node, body))
            on_dispatch(1)
            on_receipt(2)
            return 200, '{"answers":{}}'

        body = {'model': community.FLASH, **community_jev.approved_probe()[0]}
        audit = {}
        status, raw = await self.router.complete(tid, node, body, flash, check_fallback=check_fallback,
                                                 on_dispatch=lambda _: None, on_receipt=lambda _: None, diagnostics=audit.update)
        return status, json.loads(raw), audit

    async def test_probe_verifies_both_choices_and_keeps_single_key(self):
        self.assertEqual(self.settings.status()['source'], 'flash')
        for action, error in [('wrong', 'probe_failed'), ('wrong-model', 'probe_failed'), (401, 'http_401')]:
            self.replies.append(action)
            with self.assertRaisesRegex(RuntimeErrorBase, error):
                await self.check()
            self.assertFalse(self.settings.status()['key_configured'])
        checked = await self.check()
        self.assertEqual(checked['base_url'], 'https://example.test/api')
        self.assertTrue(checked['connected'] and checked['choice_verified'])
        self.assertEqual(checked['usage']['total_tokens'], 11)
        self.assertEqual({k: v for k, v in self.calls[-1][1].items() if k != 'model'}, community_jev.approved_probe()[0])
        self.assertEqual(self.settings.status()['source'], 'flash')
        with self.assertRaisesRegex(RuntimeErrorBase, 'check_required'):
            self.settings.activate('incorrect', 0)
        self.settings.activate(checked['token'], 0)
        post = community_jev.Settings(self.store, self.config).status()
        self.assertEqual((post['source'], post['choice_verified']), ('jev', True))
        self.assertNotIn('encrypted_key', post)
        self.assertEqual(unprotect(self.config.load()['model']['encrypted_key']), '101')
        self.assertEqual(unprotect(community_jev.settings(self.config)['encrypted_key']), '202')
        for value in ('http://example.test', 'https://user@example.test', 'https://example.test/?key=202'):
            with self.assertRaisesRegex(RuntimeErrorBase, 'invalid_url'):
                community_jev.endpoint(value)

    async def test_configuration_does_not_change_accepted_turns_or_inherit_grants(self):
        old = self.turn()
        await self.enable()
        waiting = self.turn()
        self.assertTrue(self.view(waiting)['unapproved'])
        self.assertTrue(self.grant()['authorized'])
        active = self.turn()
        await self.invoke(old)
        await self.invoke(waiting)
        status, raw, audit = await self.invoke(active)
        self.assertEqual((status, raw['decision_source'], audit['decision_provider']), (200, 'jev', 'jev'))
        self.assertEqual([turn for turn, _, _ in self.flash], [old, waiting])
        await self.enable(key='203')
        self.assertFalse(self.settings.status(self.pid, self.sid)['session']['authorized'])
        with self.assertRaisesRegex(ModelError, 'grant_changed'):
            await self.invoke(active, 'two')
        self.settings.disable(self.settings.status()['revision'])
        now = self.turn()
        await self.invoke(now)
        self.assertEqual(self.view(now)['actual'], 'flash')

    async def test_before_send_falls_back_once_with_current_flash_permission(self):
        await self.enable()
        self.grant()
        before = self.turn()
        self.replies.append('before')
        _, _, first = await self.invoke(before, 'first')
        _, _, second = await self.invoke(before, 'second')
        self.assertEqual(first['fallback_reason'], 'provider_network_error')
        self.assertNotIn('fallback_reason', second)
        self.assertEqual(self.view(before)['actual'], 'flash')
        self.assertEqual([turn for turn, _, _ in self.flash], [before, before])
        denied = self.turn()

        def no_flash():
            raise RuntimeErrorBase('private_cloud_provider_not_authorized')
        self.replies.append('before')
        with self.assertRaisesRegex(ModelError, 'provider_network_error'):
            await self.invoke(denied, check_fallback=no_flash)
        self.assertNotIn(denied, [turn for turn, _, _ in self.flash])

    async def test_after_any_jev_dispatch_never_falls_back(self):
        await self.enable()
        self.grant()
        prior = self.turn()
        await self.invoke(prior, 'first')
        self.replies.append('before')
        with self.assertRaisesRegex(ModelError, 'provider_network_error'):
            await self.invoke(prior, 'second')
        unknown = self.turn()
        self.replies.append('unknown')
        with self.assertRaisesRegex(ModelError, 'transport_unconfirmed'):
            await self.invoke(unknown)
        reached = len(self.calls)
        with self.assertRaisesRegex(ModelError, 'transport_unconfirmed'):
            await self.invoke(unknown, 'next')
        self.assertEqual(reached, len(self.calls))
        self.assertEqual(self.view(unknown)['outcome'], 'unknown')
        rejected = self.turn()
        self.replies.append(401)
        self.assertEqual((await self.invoke(rejected))[0], 401)
        self.assertEqual(self.view(rejected)['outcome'], 'http_error')
        self.assertEqual(self.flash, [])

    async def test_restart_with_pending_dispatch_preserves_unknown(self):
        await self.enable()
        self.grant()
        turn = self.turn()
        with self.store.connection(write=True) as db:
            key = community_decisions.TURN + turn
            save(db, key, dict(read(db, key), pending=identifier()))
        before = len(self.calls)
        with self.assertRaisesRegex(ModelError, 'transport_unconfirmed'):
            await self.invoke(turn)
        self.assertEqual(before, len(self.calls))
        self.assertEqual(self.flash, [])

    async def test_ordinary_choice_and_web_switch_share_the_same_frozen_jev(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from persona_runtime.knowledge_routing import KnowledgeRouting
        from persona_runtime.knowledge_web_decision import parse
        from persona_runtime.ordinary_providers import ReviewedPrivateChatProviders

        await self.enable()
        self.grant()
        tid = self.turn()
        provider = object.__new__(ReviewedPrivateChatProviders)
        config_model = community.decision_model(self.config.load())
        provider._model = lambda _: config_model
        probe = community_jev.approved_probe()[0]
        preset = probe['questions']['check_first']
        provider.bundle = {'nodes': {'check_first': preset}}
        provider.knowledge_revisions = False
        provider.community_route = True
        provider.version = '0'
        provider.connections = {}
        from persona_runtime.community_decision import Transport
        provider.jev_factory = Transport
        routing = KnowledgeRouting(SimpleNamespace(chat=SimpleNamespace(decision_providers=self.router)))
        with patch.object(Transport, 'complete', AsyncMock(side_effect=AssertionError('Flash dispatched'))):
            choose = provider.bind_community_choice(self.router, tid, 'one', public_source=False,
                                                    check_fallback=lambda: None)
            choice, audit = await choose('check_first', '01', probe['state'], ('A', 'B'))
            result = await routing.execute(config_model, {'persona_id': self.pid,
                'knowledge_processing': {'root_turn_id': tid, 'input': {}}})
        self.assertEqual((choice, audit['decision_source'], audit['usage']['total_tokens']), ('A', 'jev', 11))
        self.assertEqual(parse(result[0])['decision_source'], 'jev')
        self.assertEqual(self.calls[-1][1]['model'], 'jev-1.14')
