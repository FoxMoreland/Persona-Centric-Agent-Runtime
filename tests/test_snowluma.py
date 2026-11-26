"""External SnowLuma selection, ownership, identity and text-only transport."""
import asyncio
import copy
import gc
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from persona_runtime.app_runtime import AppRuntime
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.deployment import prepare, snowluma_settings, transport_token, write_component_config
from persona_runtime.desktop_runtime import connection_status_text
from persona_runtime.lifecycle import ServiceController
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.paths import RuntimePaths
from persona_runtime.qq_client import probe_client, require_snowluma, send_private_text
from persona_runtime.qq_bridge import QQBridge, allowed_message, event_type


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        base = RuntimePaths.from_environment().home / 'tmp'
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='snowluma-', dir=base)
        self.addCleanup(self.temp.cleanup)
        self.paths = RuntimePaths(Path(self.temp.name))
        self.config = ConfigStore(self.paths)
        self.config.initialize()
        self.config.update({'qq_backend': 'snowluma', 'bot_qq': '456', 'owner_qq': '123'})
        self.cfg = self.config.load()
        self.controller = ServiceController(self.paths)

    def test_scoped_source_and_self_messages_are_local_context_without_sdk_dispatch(self):
        from persona_runtime.qq_context_journal import configured_journal
        from persona_runtime.sdk_reference_guard import install_context_capture
        profile = {'context_sources': {'groups': ['777'], 'private': ['789','987'],
                                      'incoming_private': ['987']}}
        atomic_json(self.paths.home/'private/qq-ingress-observation.json',profile)
        journal = configured_journal(self.paths,self.cfg)

        class Adapter:
            def __init__(self):
                self.downstream = []

            async def _handle_event(self, payload):
                self.downstream.append(payload.get('message_id'))

        install_context_capture(journal,Adapter)
        adapter = Adapter()
        base = {'self_id':456,'user_id':456,'post_type':'message_sent','message_type':'group',
                'group_id':777,'message_id':11,'time':12345,
                'message':[{'type':'text','data':{'text':'fixture-59126'}}]}
        for value in (base, base, base|{'group_id':888,'message_id':12},
                      base|{'message_type':'private','sub_type':'friend','target_id':789,'message_id':13},
                      base|{'message_type':'private','sub_type':'friend','target_id':123,'message_id':14},
                      base|{'message_type':'private','sub_type':'friend','post_type':'message','user_id':987,'message_id':15},
                      base|{'message_type':'group','post_type':'message','user_id':987,'message_id':16}):
            asyncio.run(adapter._handle_event(value))
        self.assertEqual(adapter.downstream,[16])
        group, = journal.recent('group','777')
        self.assertEqual((group['author'],group['direction'],group['text']),('456','account_self','fixture-59126'))
        self.assertEqual(journal.recent('private','789')[0]['direction'],'account_self')
        peer, = journal.recent('private','987')
        self.assertEqual((peer['author'],peer['direction']),('987','incoming'))
        self.assertNotIn(b'fixture-59126',journal.path.read_bytes())
        with self.assertRaisesRegex(RuntimeErrorBase,'qq_context_scope_invalid'):
            journal.recent('private','123')

    def test_startup_quota_ignores_offline_messages_and_resets_without_duplication(self):
        from persona_runtime.qq_context_journal import QQContextJournal

        def open_journal(floor):
            return QQContextJournal(self.paths,'456',['777'],['789'],[],core=['123'],
                                    startup_limit=50,timestamp_floor=floor)

        journal = open_journal(100)
        raw = {'post_type':'message','self_id':456,'user_id':998,'message_type':'group',
               'group_id':777,'time':100,'message':[{'type':'text','data':{'text':'0901'}}]}
        self.assertEqual(journal.capture(raw|{'message_id':1,'time':99}),'offline_ignored')
        self.assertEqual(journal.capture(raw|{'message_id':2}),'incoming')
        self.assertEqual(journal.capture(raw|{'message_id':2}),'duplicate')
        for mid in range(3,52):
            self.assertEqual(journal.capture(raw|{'message_id':mid}),'incoming')
        self.assertEqual(journal.capture(raw|{'message_id':52}),'startup_limit')
        own = raw|{'user_id':456,'post_type':'message_sent','message_type':'private',
                   'sub_type':'friend','target_id':789,'message_id':52}
        self.assertEqual(journal.capture(own),'account_self')
        core = own|{'post_type':'message','user_id':123,'time':90}
        for mid in range(60,112):
            self.assertEqual(journal.capture(core|{'message_id':mid}),'incoming')
        self.assertEqual(len(journal.recent('private','123',limit=100)),52)
        restarted = open_journal(500)
        self.assertEqual(restarted.capture(raw|{'message_id':53,'time':400}),'offline_ignored')
        self.assertEqual(restarted.capture(raw|{'message_id':54,'time':500}),'incoming')
        self.assertEqual(len(restarted.recent('group','777',limit=100)),51)

    def test_core_history_has_no_50_message_cap_and_resumes_at_previous_watermark(self):
        from persona_runtime.qq_context_journal import QQContextJournal
        from persona_runtime.qq_core_sync import synchronize
        journal = QQContextJournal(self.paths,'456',[],[],[],core=['123'])
        rows = [{'message_id':n,'time':n,'user_id':123,
                 'message':[{'type':'text','data':{'text':'0901'}}]} for n in range(90,0,-1)]
        calls = []

        async def fetch(peer, anchor, count):
            calls.append((peer,anchor))
            return [r for r in rows if not anchor or r['message_id'] <= anchor][:count]

        self.assertEqual(asyncio.run(synchronize(journal,'123',fetch)),90)
        self.assertEqual(calls,[('123',0),('123',41)])
        self.assertEqual(journal.watermark('123'),'90')
        rows.insert(0,rows[0]|{'message_id':91,'time':91})
        self.assertEqual(asyncio.run(synchronize(journal,'123',fetch)),1)
        self.assertEqual(journal.watermark('123'),'91')
        self.assertEqual(len(journal.recent('private','123',limit=100)),91)

    def test_core_sync_does_not_treat_an_empty_initial_page_as_a_checkpoint(self):
        from persona_runtime.qq_context_journal import QQContextJournal
        from persona_runtime.qq_core_sync import synchronize
        journal = QQContextJournal(self.paths,'456',[],[],[],core=['123'])

        async def empty(peer, anchor, count):
            return []

        with self.assertRaisesRegex(RuntimeErrorBase,'qq_history_empty_unverified'):
            asyncio.run(synchronize(journal,'123',empty))
        self.assertIsNone(journal.watermark('123'))

    def test_mixed_qq_events_are_filtered_before_content_and_counted_without_payloads(self):
        def event(**changes):
            raw = {'post_type': 'message', 'message_type': 'private', 'sub_type': 'friend',
                   'self_id': '456', 'user_id': '123', 'message_id': '11',
                   'message': [{'type': 'text', 'data': {'text': 'synthetic-local-fixture'}}]}
            raw.update(changes)
            return SimpleNamespace(platform_meta=SimpleNamespace(id='persona-onebot', name='aiocqhttp'),
                                   message_obj=SimpleNamespace(raw_message=raw, message_id='11'),
                                   is_private_chat=lambda: raw['message_type'] == 'private',
                                   get_self_id=lambda: raw['self_id'], get_sender_id=lambda: raw['user_id'])

        atomic_json(self.paths.home/'private/qq-ingress-observation.json', {'group': '777', 'other_private': '789'})
        bridge = QQBridge(self.paths)
        valid = allowed_message(event(), self.cfg)
        self.assertEqual(valid['request_id'], allowed_message(event(), self.cfg)['request_id'])
        for item, category in ((event(message_type='group', group_id='777', message=None), 'group'),
                               (event(message_type='group', group_id='888', message=None), 'group'),
                               (event(user_id='789', message=None), 'other_private'),
                               (event(post_type='message_sent'), 'self_or_echo'),
                               (event(user_id='456'), 'self_or_echo'),
                               (event(self_id='111'), 'foreign_or_invalid_envelope'),
                               (event(post_type='notice'), 'non_message')):
            with self.subTest(category=category):
                self.assertEqual(event_type(item, self.cfg), category)
                self.assertIsNone(allowed_message(item, self.cfg))
                bridge.note_ingress(category, item)
        bridge.publish_ingress_counts()
        data = json.loads(bridge.ingress_counts_path.read_text())
        self.assertEqual(data, {'group': 2, 'observed_group': 1, 'other_private': 1,
                                'observed_other_private': 1, 'self_or_echo': 2,
                                'foreign_or_invalid_envelope': 1, 'non_message': 1})

    def test_old_config_defaults_to_napcat_without_rewriting_it(self):
        raw = self.config.load()
        raw.pop('qq_backend')
        raw.pop('snowluma_port')
        atomic_json(self.config.file, raw)
        original = self.config.file.read_bytes()
        value = self.config.load()
        self.assertEqual(value['qq_backend'], 'napcat')
        self.assertEqual(value['snowluma_port'], 5099)
        self.assertEqual(self.config.file.read_bytes(), original)

    def test_invalid_selection_and_conflicting_port_rejected(self):
        for update in ({'qq_backend': 'auto'}, {'snowluma_port': True},
                       {'snowluma_port': 80}, {'snowluma_port': self.cfg['onebot_port']}):
            with self.subTest(update=update), self.assertRaises(RuntimeErrorBase):
                self.config.update(update)
        self.assertEqual(self.config.load(), self.cfg)

    def test_transport_reused_on_restart_and_rotated_for_each_identity_change(self):
        token = transport_token(self.paths, self.cfg)
        self.assertEqual(token, transport_token(self.paths, self.cfg))
        self.assertNotIn(token, (self.paths.home / 'runtime/onebot-transport.dpapi.json').read_text())
        for update in ({'qq_backend': 'napcat'}, {'qq_backend': 'snowluma'},
                       {'bot_qq': '789'}, {'owner_qq': '111'}, {'onebot_port': 16301}):
            self.config.update(update)
            current = transport_token(self.paths, self.config.load())
            self.assertNotEqual(current, token)
            token = current

    def test_sl_parameters_require_current_generated_identity(self):
        with self.assertRaises(RuntimeErrorBase):
            snowluma_settings(self.paths)
        token = transport_token(self.paths, self.cfg)
        value = snowluma_settings(self.paths)
        self.assertEqual(value['url'], 'ws://127.0.0.1:6299/ws')
        self.assertEqual(value['token'], token)
        self.assertNotIn(token, json.dumps(self.config.public()))
        self.config.update({'owner_qq': '789'})
        with self.assertRaises(RuntimeErrorBase):
            snowluma_settings(self.paths)

    def test_configure_sl_preserves_napcat_and_binds_owned_loopback_listener(self):
        old = self.paths.home / 'components/napcat/config/onebot11_456.json'
        atomic_json(old, {'sentinel': 'preserve'})
        original = old.read_bytes()
        default = {'provider_settings': {}, 'platform_settings': {}, 'dashboard': {}}
        with patch.dict('sys.modules', {'astrbot.core.config.default': SimpleNamespace(DEFAULT_CONFIG=default)}), \
                patch('persona_runtime.deployment.subprocess.run'):
            write_component_config(self.paths)
        data = json.loads((self.paths.home / 'components/astrbot/bot/data/cmd_config.json').read_text())
        platform = data['platform'][0]
        self.assertEqual(platform['ws_reverse_host'], '127.0.0.1')
        self.assertEqual(platform['ws_reverse_token'], snowluma_settings(self.paths)['token'])
        self.assertFalse(data['provider_settings']['enable'])
        self.assertEqual(old.read_bytes(), original)
        self.assertFalse((old.parent / 'webui.json').exists())

    def test_prepare_sl_requires_only_astrbot_assets(self):
        python = self.paths.home / 'components/astrbot/.venv/Scripts/python.exe'
        python.parent.mkdir(parents=True)
        python.touch()
        result = prepare(self.paths, install=False)
        self.assertTrue(result['prepared'])
        self.assertFalse((self.paths.home / 'components/napcat').exists())
        self.assertTrue((self.paths.home / 'components/astrbot/bot/data/plugins/astrbot_plugin_persona_runtime/main.py').exists())

    def test_start_sl_starts_only_our_message_connection(self):
        with patch('persona_runtime.lifecycle.require_console', return_value={'token': 'console'}), \
                patch.object(self.controller, 'start') as start, \
                patch.object(self.controller, 'health', return_value={'state': '等待外部连接'}), \
                patch('persona_runtime.lifecycle.component_python') as python:
            result = self.controller._start('napcat')
        self.assertEqual(result['state'], '等待外部连接')
        start.assert_called_once_with('astrbot')
        python.assert_not_called()

    def test_stop_sl_disables_connection_and_stops_only_our_bridge(self):
        with patch.object(self.controller, 'owned', return_value=True), \
                patch.object(self.controller, '_stop', wraps=self.controller._stop) as stop:
            def stop_owned(role):
                if role == 'astrbot':
                    return None
                return ServiceController._stop(self.controller, role)
            stop.side_effect = stop_owned
            self.controller.stop('napcat')
        self.assertEqual([call.args[0] for call in stop.call_args_list], ['napcat', 'astrbot'])
        self.assertFalse(self.config.load()['runtime']['components']['napcat'])
        self.assertFalse(self.config.load()['runtime']['components']['astrbot'])

    def test_external_connection_is_not_automatically_started_or_retried(self):
        cfg = copy.deepcopy(self.cfg)
        cfg['runtime']['components'] = {'napcat': True}
        controller = SimpleNamespace(paths=self.paths, config=SimpleNamespace(load=lambda: cfg),
            ensure_binding=lambda: None, closing=threading.Event(),
            health=lambda role: {'state': '等待外部连接', 'owned': False})
        runtime = AppRuntime(controller)
        runtime.last_maintenance = time.time()
        with patch.object(runtime, 'launch_operation') as launch:
            for _ in range(8):
                runtime.tick()
            launch.assert_not_called()
        self.assertFalse(runtime.restarts)

    def test_external_shutdown_releases_our_jobs_and_records_connection_scope(self):
        self.controller.console_lease = {'token': 'current'}
        with patch.object(self.controller, 'owned', return_value=False), \
                patch.object(self.controller, 'release_role') as release, \
                patch.object(self.controller, 'end_console'), \
                patch.object(self.controller, 'has_resident_processes', return_value=False), \
                patch('persona_runtime.qq_session.logout_verified') as logout:
            report = self.controller.shutdown()
        logout.assert_not_called()
        release.assert_called_once_with('astrbot')
        self.assertTrue(report['external_qq_session'])
        self.assertTrue(report['processes_released'])
        self.assertFalse(report['logout_confirmed'])
        self.assertEqual(report['logout_reason'], 'external_session_connection_only')

    def test_health_requires_fresh_current_sl_proof(self):
        self.controller.set_desired('napcat',True)
        identity = {'ProcessId': 1234, 'Created': 'current', 'ExecutablePath': 'python.exe'}
        proof = {'at': time.time(), 'pid': 1234, 'instance_id': self.cfg['instance_id'],
                 'verified': True, 'online': True, 'connected': True, 'backend': 'snowluma',
                 'backend_matches': True, 'account_matches': True}
        with patch.object(self.controller, 'owned', side_effect=lambda role: role == 'astrbot'), \
                patch.object(self.controller, 'record', return_value={'host': identity}), \
                patch('persona_runtime.lifecycle.port_open', return_value=True):
            for change, expected in (({}, '运行'), ({'at': time.time()-20}, '等待外部连接'),
                                     ({'backend': 'napcat'}, '等待外部连接')):
                atomic_json(self.paths.home / 'runtime/astrbot-health.json', proof | change)
                state = self.controller.health('napcat')
                self.assertEqual(state['state'], expected)
                self.assertFalse(state['owned'])

    def test_external_management_page_uses_explicit_loopback_config(self):
        self.config.update({'snowluma_port': 15099})
        self.assertEqual(self.controller.management_url('napcat'), 'http://127.0.0.1:15099')

    def test_sl_probe_failure_keeps_healthy_bridge_ready_without_claiming_login(self):
        identity = {'ProcessId': 1234, 'Created': 'current', 'ExecutablePath': 'python.exe'}
        record = {'instance_id': self.cfg['instance_id'], 'host': identity}
        proof = {'at': time.time(), 'pid': 1234, 'instance_id': self.cfg['instance_id'],
                 'verified': False, 'online': False, 'error_type': 'TimeoutError', 'error_stage': 'probe'}
        atomic_json(self.paths.home / 'runtime/astrbot-health.json', proof)
        with patch.object(self.controller, 'owned', return_value=True), \
                patch.object(self.controller, 'record', return_value=record), \
                patch('persona_runtime.lifecycle.listener', return_value=identity), \
                patch('persona_runtime.lifecycle.port_open', return_value=True):
            state = self.controller.health('astrbot')
            self.assertIsNone(state['login'])
            self.assertTrue(self.controller._start_ready('astrbot', state))


class DesktopConnectionTests(unittest.TestCase):
    from tests.test_desktop import DesktopTests as _base
    setUp = _base.setUp
    window = _base.window
    def doCleanups(self):
        for window in self.windows:
            window.hub.pool.shutdown(wait=True, cancel_futures=True)
            window.hub.drain()
        result = super().doCleanups()
        self.windows.clear()
        gc.collect()
        return result

    def test_selection_save_and_external_prepare_use_saved_config(self):
        window = self.window()
        window.qq_vars['qq_backend'].set('snowluma')
        with patch.object(window, 'run_task') as task, patch.object(window.controller, 'owned', return_value=False):
            window.save_connection()
            task.call_args.args[2](None)
        self.assertEqual(window.config.load()['qq_backend'], 'snowluma')
        with patch.object(window, 'run_task') as task, patch('persona_runtime.desktop.filedialog.askdirectory') as choose:
            window.prepare_components()
        choose.assert_not_called()
        self.assertEqual(task.call_args.args[0], '准备独立组件')

    def test_connection_controls_and_parameter_dialog_use_normal_entry(self):
        window = self.window()
        window.config.update({'qq_backend': 'snowluma'})
        window.load_config_fields()
        from tests.test_desktop import DesktopTests
        combos = [widget for widget in DesktopTests.widgets(window) if widget.winfo_class() == 'TCombobox']
        self.assertTrue(any(tuple(widget.cget('values')) == ('napcat', 'snowluma') for widget in combos))
        transport_token(window.paths, window.config.load())
        with patch.object(window, 'run_task') as task, patch.object(window, 'show_json') as show:
            window.show_snowluma_settings()
            value = task.call_args.args[2](None)
            task.call_args.args[3](value)
        self.assertEqual(value['backend'], 'snowluma')
        show.assert_called_once()


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cfg = {'qq_backend': 'snowluma', 'bot_qq': '456', 'owner_qq': '123'}
        self.client = SimpleNamespace(get_login_info=AsyncMock(return_value={'user_id': 456}),
            get_status=AsyncMock(return_value={'online': True}),
            get_version_info=AsyncMock(return_value={'app_name': 'SnowLuma'}),
            send_msg=AsyncMock(return_value={'message_id': 789}), send_private_msg=AsyncMock())

    async def test_send_uses_sl_generic_action_and_expected_bot_routing(self):
        result = await send_private_text(self.client, self.cfg, '测试文本')
        self.assertEqual(result, {'message_id': 789})
        self.client.send_msg.assert_awaited_once_with(message_type='private', user_id=123, self_id=456,
            message=[{'type': 'text', 'data': {'text': '测试文本'}}])
        self.client.send_private_msg.assert_not_called()
        self.client.get_version_info.assert_awaited_once_with(self_id=456)

    async def test_mismatched_backend_account_offline_or_disabled_never_sends(self):
        for kind in ('backend', 'account', 'offline', 'disabled'):
            with self.subTest(kind=kind):
                self.setUp()
                if kind == 'backend':
                    self.client.get_version_info.return_value = {'app_name': 'NapCat'}
                elif kind == 'account':
                    self.client.get_login_info.return_value = {'user_id': 999}
                elif kind == 'offline':
                    self.client.get_status.return_value = {'online': False}
                else:
                    self.cfg['runtime'] = {'components': {'napcat': False}}
                with self.assertRaises(RuntimeErrorBase):
                    await send_private_text(self.client, self.cfg, 'test')
                self.client.send_msg.assert_not_called()

    async def test_timeout_and_missing_version_are_visible(self):
        self.client.get_version_info.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            await require_snowluma(self.client, self.cfg)
        self.client.send_msg.assert_not_called()
        self.client.get_version_info.side_effect = None
        self.client.get_version_info.return_value = {}
        proof = await probe_client(self.client, self.cfg)
        self.assertFalse(proof['verified'])
        self.assertTrue(proof['connected'])

    async def test_napcat_keeps_existing_send_action_and_probe_contract(self):
        self.cfg['qq_backend'] = 'napcat'
        await probe_client(self.client, self.cfg)
        await send_private_text(self.client, self.cfg, 'test')
        self.client.get_version_info.assert_not_called()
        self.client.send_private_msg.assert_awaited_once()
        self.client.send_msg.assert_not_called()

    async def test_sl_heartbeat_publishes_type_mismatch_without_restart_error(self):
        from persona_runtime.bridge_health import heartbeat_loop
        self.client.get_version_info.return_value = {'app_name': 'NapCat'}
        platform = SimpleNamespace(meta=lambda: SimpleNamespace(name='aiocqhttp', id='persona-onebot'),
                                   get_client=lambda: self.client)
        published = []
        async def stop(_):
            raise asyncio.CancelledError()
        cfg = self.cfg | {'instance_id': 'test'}
        with self.assertRaises(asyncio.CancelledError):
            await heartbeat_loop(SimpleNamespace(home=Path('unused')), SimpleNamespace(load=lambda: cfg),
                lambda: [platform], publish=lambda _, value: published.append(value), sleep=stop)
        self.assertFalse(published[0]['verified'])
        self.assertFalse(published[0]['backend_matches'])
        self.assertTrue(published[0]['connected'])
        self.assertIsNone(published[0]['error_type'])

    def test_visible_status_names_external_sl_and_mismatch(self):
        text = connection_status_text({'napcat': {'backend': 'snowluma', 'account_logged_in': False,
                                                  'login_reason': 'backend_mismatch'}})
        self.assertIn('SnowLuma（独立运行）', text)
        self.assertIn('客户端类型不符', text)


if __name__ == '__main__':
    unittest.main()
