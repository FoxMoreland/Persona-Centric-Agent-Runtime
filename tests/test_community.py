import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from persona_runtime import community, community_decision, session_privacy
from persona_runtime.config import ConfigStore
from persona_runtime.knowledge_routing import configured_model
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.chat import ModelError
from persona_runtime.network_proxy import outbound_proxy, route_name
from persona_runtime.paths import RuntimePaths
from persona_runtime.provider_policy import require_generation_provider
from persona_runtime.storage import Store


class CommunityTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict('os.environ', {'PCAR_EDITION': 'community'})
        environment.start()
        self.addCleanup(environment.stop)
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.paths = RuntimePaths(Path(temporary.name))
        self.config = ConfigStore(self.paths)
        self.config.initialize()
        self.store = Store(self.paths)
        self.store.initialize()

    def test_single_connection_preview_matches_saved_recipient_bindings(self):
        changes = (self.store, self.config, 'main', community.BASE, community.PRO, 'fixture-key')
        preview = session_privacy.connection(*changes)
        session_privacy.connection(*changes, preview['confirmation'])
        cfg = self.config.load()
        models = session_privacy.configured_models(self.config, cfg)
        self.assertEqual({'main', 'organization', 'native_web', 'lookup_target', 'jev'}, set(models))
        self.assertEqual({cfg['model']['encrypted_key']},
                         {model['encrypted_key'] for model in models.values()})
        self.assertEqual(community.FLASH, configured_model(self.paths)['name'])
        self.assertEqual(community.DECISION_BASE, models['jev']['base_url'])
        self.assertEqual(community.PRO, models['main']['name'])
        disk = json.loads(self.config.file.read_bytes())
        self.assertEqual('', disk['organization']['model']['encrypted_key'])
        self.assertNotIn('native_web_model', disk)
        self.assertNotIn('fixture-key', self.config.file.read_text(encoding='utf-8'))
        rotated = (*changes[:-1], 'rotated-fixture-key')
        preview = session_privacy.connection(*rotated)
        session_privacy.connection(*rotated, preview['confirmation'])
        cfg = self.config.load()
        self.assertEqual({cfg['model']['encrypted_key']}, {item['encrypted_key'] for item in
                         session_privacy.configured_models(self.config, cfg).values()})

    def test_rejects_other_receiver_and_requires_key_before_decisions(self):
        with self.assertRaisesRegex(RuntimeErrorBase, 'jev_configuration_required'):
            configured_model(self.paths)
        with self.assertRaisesRegex(RuntimeErrorBase, 'community_official_key_required'):
            self.config.update({'main_reply': {'enabled': True}})
        with self.assertRaisesRegex(RuntimeErrorBase, 'community_official_key_required'):
            self.config.update({'organization': {'model': {'api_key': 'fixture-key'}}})
        with self.assertRaisesRegex(ValueError, 'provider_official_deepseek_required'):
            require_generation_provider({'base_url': 'https://e-flowcode.cc/v1', 'name': community.FLASH})

    def test_only_official_https_endpoints_can_connect_without_proxy(self):
        endpoint = community.DECISION_BASE + '/chat/completions'
        with patch('persona_runtime.network_proxy.configured_proxy', return_value=None):
            self.assertIsNone(outbound_proxy(endpoint))
            self.assertEqual('official_direct', route_name(endpoint, None))
            for url in ('http://api.deepseek.com', 'https://api.deepseek.com:8443',
                        'https://api.deepseek.com.evil.invalid', 'https://example.invalid'):
                with self.subTest(url=url), self.assertRaisesRegex(ModelError, 'system_proxy_required'):
                    outbound_proxy(url)
            with patch.dict('os.environ', {'PCAR_EDITION': ''}):
                with self.assertRaisesRegex(ModelError, 'system_proxy_required'):
                    outbound_proxy(endpoint)
        with patch('persona_runtime.network_proxy.configured_proxy', return_value='http://127.0.0.1:7890'):
            self.assertEqual('http://127.0.0.1:7890', outbound_proxy(endpoint))

    def test_strict_decision_roundtrip_validates_choices_without_probabilities(self):
        body = {'model': community.FLASH, 'state': {'id': 1}, 'questions': {
            'route': {'type': 'choice', 'instructions': '1', 'criteria': {'web': '2', 'none': '3'}}}}
        wire = community_decision.build(body, 8192)
        self.assertEqual({'state': body['state'], 'questions': body['questions']},
                         json.loads(wire['messages'][0]['content']))
        function = wire['tools'][0]['function']
        self.assertTrue(function['strict'])
        self.assertEqual(['web', 'none'], function['parameters']['properties']['route']['enum'])

        def result(arguments):
            packet = {'model': community.FLASH, 'usage': {'total_tokens': 14},
                      'choices': [{'finish_reason': 'tool_calls', 'message': {
                          'role': 'assistant', 'tool_calls': [{'type': 'function', 'function': {
                              'name': function['name'], 'arguments': arguments}}]}}]}
            return community_decision.normalize(json.dumps(packet), body)

        self.assertEqual({'route': {'type': 'choice', 'choice': 'none'}},
                         result('{"route":"none"}')['answers'])
        self.assertEqual({'total_tokens': 14}, result('{"route":"none"}')['usage'])
        self.assertEqual({}, result('{"route":"wrong"}')['answers'])
        self.assertEqual({}, result('{"route":"none","route":"web"}')['answers'])


if __name__ == '__main__':
    unittest.main()
