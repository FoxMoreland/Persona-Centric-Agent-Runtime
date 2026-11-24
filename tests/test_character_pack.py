"""Local interchange and consumption with opaque, offline-only records."""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from persona_runtime import import_history, scene_policy
from persona_runtime.archive import binding, encoded, scope
from persona_runtime.automatic_memory_schedule import scan_sessions
from persona_runtime.character_pack import CharacterPacks, stage_input, stage_path
from persona_runtime.config import ConfigStore
from persona_runtime.context_builders import memory_usable, summary_usable
from persona_runtime.import_history_service import must_prepare
from persona_runtime.models import LOCAL_USER_ID, RuntimeErrorBase, generic_persona, identifier, timestamp
from persona_runtime.organization import latest_summary
from persona_runtime.paths import RuntimePaths
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store
from persona_runtime.workbench_timeline import save_session_title, selection


class CharacterPackTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.sender, self.receiver = self.instance(root / 'source'), self.instance(root / 'target')
        self.packs, self.inbox = CharacterPacks(self.sender), CharacterPacks(self.receiver)
        service = RuntimeService(self.sender)
        self.pid = service.create_persona(generic_persona())['id']
        service.use_persona(self.pid)
        service.set_relationship('10')

    @staticmethod
    def instance(root):
        paths = RuntimePaths(root)
        config = ConfigStore(paths)
        config.initialize()
        config.update({'model': {'base_url': 'https://api.deepseek.com', 'name': 'deepseek-v4-pro', 'api_key': '1'}})
        store = Store(paths)
        store.initialize()
        return store

    def transcript(self, db, first, count=2):
        from persona_runtime.import_markdown import assign
        from persona_runtime.import_plan import parse_source
        raw = '\n'.join(f'## {"A" if i % 2 == 0 else "B"} [2020-01-02 03:04+03:00]\n{first + i}'
                        for i in range(count))
        rows = assign(parse_source(raw.encode()), {'A': 'A', 'B': 'B'}, {})
        providers = import_history.configured_providers(self.sender.paths)
        db.execute('DELETE FROM session_heads WHERE persona_id=?', (self.pid,))
        sid = import_history.restore(db, self.pid, identifier(), 'B', rows, meta={
            'version': 2, 'revision': 1, 'withdrawn': False, 'organize': True, 'self_role': 'A',
            'timezone_offset': 180, 'providers': providers, 'prepare_before_chat': True,
        })
        save_session_title(db, self.pid, sid, str(first))
        turns = [dict(row) for row in db.execute('SELECT * FROM turns WHERE session_id=? ORDER BY rowid', (sid,))]
        return sid, turns

    def use(self, db, store, sid):
        meta = scene_policy.read(db, import_history.META + sid)
        scene_policy.save(db, import_history.META + sid,
                          dict(meta, providers=import_history.configured_providers(store.paths)))

    def memory(self, db, event, text, sources=(), previous=None, status='active'):
        mid, now = identifier(), timestamp()
        root = previous or mid
        db.execute('INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                   (mid, self.pid, LOCAL_USER_ID, 'user_fact', text, event, root, previous,
                    2 if previous else 1, status, now, now))
        for evidence, quote in sources:
            db.execute('INSERT INTO memory_sources VALUES(?,?,?)', (mid, evidence, quote))
        return mid

    def send(self, sessions, main):
        preview = self.packs.export(self.pid, sessions, main)
        result = self.packs.export(self.pid, sessions, main, expected=preview['digest'])
        handle = stage_input(self.receiver.paths, stage_path(self.sender.paths, result['handle']))
        return handle, self.inbox.preview(handle)

    def test_restore_coverage_summary_relationship_revisions_and_usable_memory(self):
        with self.sender.connection(write=True) as db:
            sid, turns = self.transcript(db, 101)
            turn = turns[0]
            source = turn['user_event_id']
            old = self.memory(db, source, '901', [(source, '101')], status='superseded')
            current = self.memory(db, source, '902', [(source, '101')], previous=old)
            now = timestamp()
            selected = scope(self.pid, 'desktop')
            proof = binding(import_history.group(db, turn, selected, {'kind': 'turn', 'id': turn['id']}, purpose='evidence'))
            db.execute("INSERT INTO memory_coverage VALUES (?,?,?,?,?,NULL,'processed','available',?)",
                       (self.pid, LOCAL_USER_ID, turn['id'], encoded(selected), proof['hash'], now))
            bid, task = identifier(), identifier()
            db.execute("INSERT INTO memory_batches VALUES(?,?,?,?,?,?,?,'completed',NULL,NULL,?,?,?)",
                       (bid, self.pid, LOCAL_USER_ID, sid, encoded(selected), bid, 'extract', '{}', now, now))
            db.execute("INSERT INTO memory_facts VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (current, self.pid, LOCAL_USER_ID, encoded(selected), '1', '2', '2020-01-01T00:00:00Z',
                        '2099-01-01T00:00:00Z', bid, '{}'))
            db.execute("""INSERT INTO organization_tasks(id,request_id,persona_id,user_id,session_id,channel,
                       mode,fingerprint,payload_json,model_snapshot,status,created_at,updated_at)
                       VALUES(?,?,?,?,?,'desktop','both',?,'{}','{}','completed',?,?)""",
                       (task, task, self.pid, LOCAL_USER_ID, sid, task, now, now))
            item = {'text': '551', 'sources': [{'event_id': source, 'quote': '101'}]}
            db.execute("""INSERT INTO summaries(id,persona_id,user_id,session_id,channel,task_id,previous_id,revision,
                       start_row,end_row,items_json,sources_json,covered_json,status,reason,created_at)
                       VALUES(?,?,?,?,'desktop',?,NULL,1,1,1,?,?,?,'active','automatic_memory',?)""",
                       (identifier(), self.pid, LOCAL_USER_ID, sid, task, encoded([item]), encoded([source]), '[1]', now))
            db.execute("""INSERT INTO memory_fragments VALUES(?,?,?,?,?,NULL,'active',?,?)""",
                       (identifier(), self.pid, LOCAL_USER_ID, sid, bid,
                        encoded({'items': [{'text': '551', 'sources': [{'source_id': source, 'quote': '101'}]}]}), now))
        handle, preview = self.send([sid], sid)
        self.assertEqual(preview['sessions'][0]['remaining'], 0)
        self.assertEqual(preview['sessions'][0]['memories'], 1)
        result = self.inbox.restore(handle, preview['digest'], '01')
        self.assertEqual(self.inbox.restore(handle, preview['digest'], '01'), result)
        pid, sid = result['id'], result['session_id']
        self.assertEqual(selection(self.receiver, pid)['sessions'][0]['title'], '101')
        history = import_history.page(self.receiver, pid, sid)['rows']
        self.assertEqual([x['text'] for x in history], ['101', '102'])
        self.assertEqual(history[0]['created_at'], '2020-01-02T03:04:00+03:00')
        self.assertFalse(history[0]['referenceable'])
        with self.receiver.connection(write=True) as db:
            self.assertFalse(must_prepare(db, sid))
            self.assertEqual(db.execute('SELECT count(*) FROM organization_attempts').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT description FROM relationships WHERE persona_id=?', (pid,)).fetchone()[0], '10')
            memory, = db.execute("SELECT * FROM memories WHERE persona_id=? AND status='active'", (pid,))
            previous, = db.execute("SELECT * FROM memories WHERE id=?", (memory['previous_id'],))
            self.assertEqual((previous['status'], previous['text'], memory['text']), ('superseded', '901', '902'))
            turn = dict(db.execute('SELECT * FROM turns WHERE session_id=?', (sid,)).fetchone())
            self.assertFalse(memory_usable(db, turn, dict(memory)))
            self.use(db, self.receiver, sid)
            self.assertTrue(memory_usable(db, turn, dict(memory)))
            self.assertTrue(summary_usable(db, turn, latest_summary(db, sid)))
            receipt = db.execute('SELECT source_hash,disposition FROM memory_coverage WHERE turn_id=?', (turn['id'],)).fetchone()
            replay = binding(import_history.group(db, turn, scope(pid, 'desktop'), {'kind': 'turn', 'id': turn['id']}))
            self.assertEqual(tuple(receipt), (replay['hash'], 'processed'))
            with patch('persona_runtime.ordinary_materials.full_revision', return_value=1):
                self.assertFalse(scan_sessions(db, pid, {'memory_backfill': True},
                                               {'encrypted_key': '1'}, self.receiver)[0][1])
        self.assertTrue(import_history.page(self.receiver, pid, sid, query='102')['rows'][0]['referenceable'])

    def test_cross_session_previews_recheck_and_authorization(self):
        with self.sender.connection(write=True) as db:
            first, a = self.transcript(db, 201)
            second, b = self.transcript(db, 301)
            a, b = a[0]['user_event_id'], b[0]['user_event_id']
            self.memory(db, b, '987', [(a, '201'), (b, '301')])
        preview = self.packs.export(self.pid, [first], first)
        self.assertEqual([(s['id'], s['memories']) for s in preview['dependencies']], [(second, 1)])
        self.assertEqual(preview['omitted']['unselected_source'], 1)
        with self.sender.connection(write=True) as db:
            save_session_title(db, self.pid, first, '401')
        with self.assertRaisesRegex(RuntimeErrorBase, 'package_changed'):
            self.packs.export(self.pid, [first], first, expected=preview['digest'])
        handle, preview = self.send([first, second], first)
        self.assertEqual([(r['title'], r['memories'], r['remaining']) for r in preview['sessions']],
                         [('401', 1, 1), ('301', 1, 1)])
        result = self.inbox.restore(handle, preview['digest'], '02')
        pid, sid = result['id'], result['session_id']
        with self.receiver.connection(write=True) as db:
            other = db.execute('SELECT id FROM sessions WHERE persona_id=? AND id!=?', (pid, sid)).fetchone()[0]
            turn = dict(db.execute('SELECT * FROM turns WHERE session_id=?', (sid,)).fetchone())
            memory = dict(db.execute('SELECT * FROM memories WHERE persona_id=?', (pid,)).fetchone())
            self.use(db, self.receiver, sid)
            self.assertFalse(memory_usable(db, turn, memory))
            self.use(db, self.receiver, other)
            self.assertTrue(memory_usable(db, turn, memory))
            self.assertFalse(memory_usable(db, turn | {'session_id': identifier()}, memory))
            from persona_runtime.memory_policy import exclude_sources
            exclude_sources(db, pid, [memory['source_event_id']], 'history_withdrawn')
            self.assertFalse(memory_usable(db, turn, memory))

    def test_corrupt_large_path_and_transaction_rollback(self):
        with self.sender.connection(write=True) as db:
            sid, _ = self.transcript(db, 401)
        path = self.sender.paths.home / 'bad.pcarpack'
        with ZipFile(path, 'w') as archive:
            archive.writestr('../manifest.json', '{}')
        handle = stage_input(self.receiver.paths, path)
        with self.assertRaisesRegex(RuntimeErrorBase, 'invalid_character_history_package'):
            self.inbox.preview(handle)
        handle, preview = self.send([sid], sid)
        with patch('persona_runtime.character_pack_restore._summaries', side_effect=RuntimeErrorBase('injected')):
            with self.assertRaisesRegex(RuntimeErrorBase, 'injected'):
                self.inbox.restore(handle, preview['digest'], '04')
        with self.receiver.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM personas').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM sessions').fetchone()[0], 0)
        self.assertEqual(self.inbox.restore(handle, preview['digest'], '04')['packaged_history'], True)

    def test_ten_thousand_visible_messages(self):
        with self.sender.connection(write=True) as db:
            from persona_runtime.sessions import select
            sid = select(db, self.pid, 'desktop')
            now = timestamp()
            db.executemany('INSERT INTO events(id,session_id,persona_id,user_id,role,text,created_at) VALUES (?,?,?,?,?,?,?)',
                           ((identifier(), sid, self.pid, LOCAL_USER_ID, 'user', str(n), now) for n in range(10000)))
        handle, preview = self.send([sid], sid)
        self.assertEqual(preview['sessions'][0]['messages'], 10000)
        copied = self.inbox.restore(handle, preview['digest'], 'large')
        self.assertEqual(import_history.page(self.receiver, copied['id'], copied['session_id'], query='9999')['rows'][0]['text'], '9999')

    def test_first_submitted_turn_uses_published_memory_and_exact_citation(self):
        from persona_runtime.chat import ChatEngine
        from persona_runtime.ingress import IngressService
        from persona_runtime.tk_notice import CONTRACT
        from tests.test_history_continuation import ReplyDouble

        with self.sender.connection(write=True) as db:
            sid, turns = self.transcript(db, 601)
            self.memory(db, turns[0]['user_event_id'], '877')
        handle, preview = self.send([sid], sid)
        copied = self.inbox.restore(handle, preview['digest'], 'first-turn')
        pid, sid = copied['id'], copied['session_id']
        RuntimeService(self.receiver).use_persona(pid)
        with self.receiver.connection() as db:
            mid = db.execute('SELECT id FROM memories WHERE persona_id=?', (pid,)).fetchone()[0]

        async def run():
            client = ReplyDouble()
            engine = ChatEngine(self.receiver, ConfigStore(self.receiver.paths), client)
            ingress = IngressService(engine)
            try:
                with self.receiver.connection() as db:
                    policy = ingress.policy(db, pid, 'desktop')
                ingress.update_policy(pid, 'desktop', 'legacy_per_message', policy['id'], identifier(),
                                      companion_mode='minimal_im', reply_mode='context_full',
                                      context_mode='working_set', expression_policy_version=3)

                def acknowledge(tid):
                    view = engine.get(tid)
                    speech = view.get('scene_speech')
                    if speech:
                        publisher = engine.scene_publication.publisher
                        for item in speech['items']:
                            offer = publisher.offer(pid, sid, speech['utterance_id'], speech['publication_id'],
                                                    speech['revision'], item['ordinal'], '101')
                            publisher.acknowledge(pid, sid, speech['utterance_id'], offer['id'], offer['lease_id'],
                                                  '101', identifier(), offer['body_hash'])
                    return engine.get(tid)

                original = ingress.accept({'channel':'desktop','persona_id':pid,'session_id':sid,
                                            'desktop_presentation':CONTRACT,'request_id':identifier(),'text':'877'})
                await asyncio.wait_for(engine.wait(), 10)
                first = acknowledge(original['turn_id'])
                self.assertEqual(first['status'], 'completed', first['error_code'])
                with self.receiver.connection(write=True) as db:
                    from persona_runtime.archive_query import Budget
                    from persona_runtime.archive_corpus import capture_connection
                    spec = {'scope':scope(pid,'desktop'),'session_id':sid,'window':'recent_50'}
                    def history():
                        return [doc['text'] for doc in capture_connection(db,spec,Budget(5))['documents']
                                if doc['coverage'] == 'imported_transcript']
                    self.assertEqual(history(), [])
                    self.use(db, self.receiver, sid)
                    self.assertEqual(sorted(history()), ['601', '602'])
                event = import_history.page(self.receiver, pid, sid, query='601')['rows'][0]

                item = ingress.accept({'channel': 'desktop', 'persona_id': pid, 'session_id': sid,
                                       'desktop_presentation': CONTRACT, 'request_id': identifier(), 'text': '877'})
                await asyncio.wait_for(engine.wait(), 10)
                saved = acknowledge(item['turn_id'])
                self.assertEqual(saved['status'], 'completed', saved['error_code'])
                with self.receiver.connection() as db:
                    for plan in db.execute("SELECT manifest_json FROM context_manifests WHERE turn_id=?", (item['turn_id'],)):
                        if mid in plan[0]:
                            break
                    else:
                        self.fail('portable memory was not selected by the ordinary desktop entry')
                    self.assertEqual(db.execute('SELECT count(*) FROM organization_attempts').fetchone()[0], 0)
                quote = ingress.accept({'channel': 'desktop', 'persona_id': pid, 'session_id': sid,
                                        'desktop_presentation': CONTRACT, 'request_id': identifier(), 'text': '878',
                                        'reference': {'kind': 'desktop_message', 'id': event['id'], 'index': 0}})
                await asyncio.wait_for(engine.wait(), 10)
                saved = acknowledge(quote['turn_id'])
                self.assertEqual(saved['status'], 'completed', saved['error_code'])
                self.assertIn('601', json.dumps(client.calls[-1], ensure_ascii=False))
                with self.receiver.connection(write=True) as db:
                    key = import_history.META + sid
                    scene_policy.save(db,key,dict(scene_policy.read(db,key),providers={},withdrawn=True))
                    self.assertEqual(history(), [])
                after = ingress.accept({'channel':'desktop','persona_id':pid,'session_id':sid,
                                         'desktop_presentation':CONTRACT,'request_id':identifier(),'text':'879'})
                await asyncio.wait_for(engine.wait(), 10)
                last = acknowledge(after['turn_id'])
                self.assertEqual(last['status'], 'completed', last['error_code'])
            finally:
                await engine.scene.close()
                engine.cognitive.characters.close()
        asyncio.run(run())

    def test_purge_removes_live_role_and_preserves_other_copy(self):
        from persona_runtime.persona_management import PersonaManagement
        from persona_runtime.workbench_appearance import image_path
        with self.sender.connection(write=True) as db:
            sid, turns = self.transcript(db, 701)
            self.memory(db,turns[0]['user_event_id'],'966')
        handle, preview = self.send([sid],sid)
        a = self.inbox.restore(handle,preview['digest'],'purge-a')
        b = self.inbox.restore(handle,preview['digest'],'purge-b')
        RuntimeService(self.receiver).use_persona(a['id'])
        avatar = image_path(self.receiver.paths,a['id'],'avatar')
        avatar.parent.mkdir(parents=True,exist_ok=True)
        avatar.write_bytes(b'1')
        manager = PersonaManagement(self.receiver)
        rev=manager.inspect(a['id'])['revision']
        with self.receiver.connection(write=True) as db:
            db.execute("UPDATE turns SET status='queued' WHERE session_id=?",(a['session_id'],))
        with self.assertRaisesRegex(RuntimeErrorBase,'persona_has_pending_work'):
            manager.purge(a['id'],rev,a['id'],'delete')
        with self.receiver.connection(write=True) as db:
            db.execute("UPDATE turns SET status='completed' WHERE session_id=?",(a['session_id'],))
        result=manager.purge(a['id'],rev,a['id'],'delete')
        self.assertEqual(result['current_persona_id'],b['id'])
        self.assertTrue(result['storage_reclaimed'])
        self.assertFalse(avatar.exists())
        self.assertTrue(manager.purge(a['id'],rev,a['id'],'delete')['already_applied'])
        with self.receiver.connection() as db:
            self.assertIsNone(db.execute('SELECT 1 FROM personas WHERE id=?',(a['id'],)).fetchone())
            self.assertIsNone(db.execute('SELECT 1 FROM memories WHERE persona_id=?',(a['id'],)).fetchone())
            self.assertIsNone(db.execute('PRAGMA foreign_key_check').fetchone())
        history=import_history.page(self.receiver,b['id'],b['session_id'])['rows']
        self.assertEqual([row['text'] for row in history],['701','702'])
        self.assertEqual(self.inbox.preview(handle)['digest'],preview['digest'])
