import unittest
from unittest.mock import patch

from persona_runtime import desktop_messages, quote_context
from persona_runtime.context_m4 import collect_context_materials
from persona_runtime.import_history import page as history_page
from persona_runtime.models import LOCAL_USER_ID, RuntimeErrorBase
from persona_runtime.references import prepare, ReferenceError
from persona_runtime.sessions import SessionService
from persona_runtime.workbench_import import WorkbenchData
from tests import test_desktop_recovery as fixture, test_workbench_continuity as workbench
from tests import test_private_reply_stage3 as research


class DesktopQuoteBranch(unittest.IsolatedAsyncioTestCase):
    setUp = fixture.DesktopRecoveryTests.setUp
    asyncTearDown = fixture.DesktopRecoveryTests.asyncTearDown
    seed = workbench.ContinuityTests.seed
    reopen = fixture.DesktopRecoveryTests.reopen

    @staticmethod
    def anchor(n, index=0):
        return {'kind': 'desktop_message', 'id': f'event-{80000+n}', 'index': index}

    def accept(self, ref, sid=None, request='901'):
        from persona_runtime.ingress import IngressService
        from persona_runtime.desktop_recovery import issue_intent
        ingress = IngressService(self.chat)
        payload = {'channel': 'desktop', 'request_id': request, 'text': '901',
                   'session_id': sid or self.sid, 'persona_id': self.pid, 'reference': ref}
        ticket = issue_intent(self.store, self.config, payload, activate=True)
        with patch.object(self.chat, '_schedule'):
            receipt = ingress.accept(payload, desktop_ticket=ticket)
            if not receipt['turn_id']:
                with self.store.connection() as db:
                    revision = db.execute('SELECT revision FROM input_batches WHERE id=?', (receipt['batch_id'],)).fetchone()[0]
                receipt = ingress.action(receipt['batch_id'], 'seal', revision, request + '-seal', self.pid)
        with self.store.connection() as db:
            return dict(db.execute('SELECT * FROM turns WHERE id=?', (receipt['turn_id'],)).fetchone())

    def test_quote_snapshot_has_51_visible_messages_and_no_semantic_context(self):
        self.seed(90)
        turn = self.accept(self.anchor(30))
        with self.store.connection() as db:
            window = quote_context.read(db, turn)
            self.assertEqual(window['anchor']['id'], 'event-80030')
            for key, start, end in [('before', 10, 30), ('after', 31, 51), ('latest', 80, 90)]:
                self.assertEqual([r['id'] for r in window[key]], [f'event-{80000+n}' for n in range(start, end)])
            with patch('persona_runtime.context_memory.rank_memories', side_effect=AssertionError), \
                    patch('persona_runtime.recall_runtime.selection', side_effect=AssertionError):
                material = collect_context_materials(self.chat, db, turn)
            self.assertEqual(material['history'], [])
            self.assertEqual(material['reference_manifest'], quote_context.manifest(window))
            fixed, audit = quote_context.fit_packet({'evidence': []}, window, '821', 350)
            self.assertEqual(fixed['evidence'][0]['id'], 'event-80030')
            self.assertLess(len(audit['before']) + len(audit['after']), 40)
        with self.store.connection(write=True) as db:
            db.execute('INSERT INTO source_exclusions VALUES(?,?,?,?,?)',
                       ('event-80032', self.pid, LOCAL_USER_ID, '822', '2026-01-01'))
        with self.store.connection() as db, self.assertRaisesRegex(ReferenceError, 'reference_source_changed'):
            prepare(db, turn)

    def test_quote_promotes_only_eligible_automatically_organized_events_near_anchor(self):
        self.seed(4)
        base = '2026-01-01T02:00:00+00:00'
        with self.store.connection(write=True) as db:
            stamps = ['2026-01-01T01:25:00+00:00',base,'2026-01-01T04:00:00+00:00',base]
            for i,stamp in enumerate(stamps):
                db.execute('UPDATE events SET created_at=? WHERE id=?',(stamp,f'event-{80000+i}'))
            db.execute("INSERT INTO memory_batches VALUES(?,?,?,?,?,?,?,?,NULL,NULL,?,?,?)",
                       ('91',self.pid,LOCAL_USER_ID,self.sid,'{}','92','extract','completed','{}',base,base))
            for i in range(4):
                ident=f'93{i}'
                db.execute('INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                           (ident,self.pid,LOCAL_USER_ID,'preference',ident,f'event-{80000+i}',ident,None,1,'active',base,base))
                db.execute('INSERT INTO memory_facts VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (ident,self.pid,LOCAL_USER_ID,'{}','94','95',None,None,'91' if i!=3 else None,'{}'))
        turn=self.accept(self.anchor(1))
        with self.store.connection() as db:
            from persona_runtime.context_m4 import build_context
            messages,details=build_context(self.chat,db,turn)
            self.assertEqual([row['id'] for row in details['memories']],['931','930'])
            self.assertTrue(any('931' in m['content'] and '930' in m['content'] for m in messages))
        with self.store.connection(write=True) as db:
            db.execute("UPDATE memories SET status='retracted' WHERE id='931'")
        with self.store.connection() as db:
            material=collect_context_materials(self.chat,db,turn)
            self.assertEqual([row[2] for row in material['memories']],['930'])

    def test_new_and_existing_workbench_sessions_support_scoped_titles(self):
        from persona_runtime.workbench_timeline import save_session_title,selection
        self.assertIsNone(selection(self.store,self.pid)['sessions'][0]['title'])
        fresh=self.chat.session(fresh=True,persona_id=self.pid,title=' 812 ')['session_id']
        self.assertEqual(selection(self.store,self.pid)['sessions'][-1]['title'],'812')
        with self.store.connection(write=True) as db:
            self.assertEqual(save_session_title(db,self.pid,self.sid,'813'),'813')
            self.assertEqual(save_session_title(db,self.pid,fresh,''),'')
            with self.assertRaisesRegex(RuntimeErrorBase,"session_unavailable"):
                save_session_title(db,self.pid,'foreign','814')
        self.assertEqual({s['id']:s['title'] for s in selection(self.store,self.pid)['sessions']},
                         {self.sid:'813',fresh:None})

    def test_anchor_overlap_is_deduplicated_and_draft_keeps_target(self):
        self.seed(35)
        reference = self.anchor(28)
        bridge = WorkbenchData(self.paths, lambda: None)
        saved = bridge.live_draft(self.pid, self.sid, {'previous': None, 'revision': '823', 'text': '824',
                                                      'reference': reference | {'text': '825'}})
        self.assertEqual(saved['reference'], reference | {'text': '80028'})
        turn = self.accept(reference)
        with self.store.connection() as db:
            window = quote_context.read(db, turn)
            self.assertEqual(window['latest'], [])
            self.assertEqual(len(window['after']), 6)
        with self.store.connection(write=True) as db:
            db.execute('INSERT INTO source_exclusions VALUES(?,?,?,?,?)',
                       ('event-80028', self.pid, LOCAL_USER_ID, '826', '2026-01-01'))
        self.assertTrue(bridge.live_draft(self.pid, self.sid)['reference']['unavailable'])

    def test_branch_stops_at_anchor_can_be_resumed_and_never_accepts_future_quote(self):
        self.seed(25)
        sessions = SessionService(self.store)
        anchor = self.anchor(17)
        result = sessions.branch(self.sid, self.pid, anchor, '830')
        child = result['session_id']
        self.assertEqual(result, sessions.branch(self.sid, self.pid, anchor, '830'))
        self.assertEqual(sessions.current(self.pid)['session_id'], child)
        self.assertEqual(sessions.show(self.sid, self.pid)['state'], 'active')
        rows = history_page(self.store, self.pid, child, limit=12)
        self.assertEqual([m['text'] for m in rows['rows']], [str(80000+n) for n in range(6,18)])
        earlier = history_page(self.store, self.pid, child, before=rows['before'], limit=12)
        self.assertEqual([m['text'] for m in earlier['rows']], [str(80000+n) for n in range(6)])
        with self.assertRaisesRegex(RuntimeErrorBase, 'reference_unavailable'):
            self.accept(self.anchor(18), child, '831')
        sessions.activate_desktop(self.sid, self.pid, '832')
        self.assertEqual(sessions.current(self.pid)['session_id'], self.sid)
        sessions.activate_desktop(child, self.pid, '833')
        turn = self.accept(anchor, child, '834')
        with self.store.connection() as db:
            quote = quote_context.read(db, turn)
            self.assertEqual(quote['after'], [])
            self.assertEqual(quote['anchor']['text'], '80017')
            self.assertEqual(desktop_messages.branch(db, child)['parent_session_id'], self.sid)

    def test_branch_cut_uses_exact_fragment_not_remainder_of_reply(self):
        self.seed(2)
        from persona_runtime.models import identifier
        with self.store.connection(write=True) as db:
            reply = identifier()
            db.execute("INSERT INTO events(id,session_id,persona_id,user_id,role,text,created_at) VALUES(?,?,?,?,'persona',?,?)",
                       (reply,self.sid,self.pid,LOCAL_USER_ID,'856\n\n857','2026-01-01'))
            db.execute("UPDATE turns SET reply_event_id=? WHERE id='80001'", (reply,))
        # The real reference resolver uses the same reply projection as the workbench.
        with patch('persona_runtime.desktop_messages.visible_legacy_reply', return_value=('856\n\n857', ['856','857'])):
            result = SessionService(self.store).branch(self.sid, self.pid,
                {'kind':'desktop_message','id':reply,'index':0}, '858')
            rows = history_page(self.store,self.pid,result['session_id'])['rows']
        self.assertEqual([r['text'] for r in rows], ['80000','80001','856'])


class PrivateQuoteBudget(unittest.IsolatedAsyncioTestCase):
    setUp = research.ResearchTests.setUp
    reopen = research.ResearchTests.reopen
    consent = research.ResearchTests.consent
    seed = workbench.ContinuityTests.seed
    accept = DesktopQuoteBranch.accept

    async def test_quote_preflight_fails_before_writer_and_skips_research(self):
        self.seed(60)
        self.consent(True)

        def too_large(packet, writer):
            self.assertIn('_accepted_quote_window', packet)
            raise RuntimeErrorBase('reference_budget_exceeded')

        self.providers.preflight_quote = too_large
        turn = self.accept(DesktopQuoteBranch.anchor(20))
        self.chat._schedule(turn['id'])
        await self.chat.wait()
        result = self.chat.get(turn['id'])
        self.assertEqual(result['error_code'], 'reference_budget_exceeded')
        self.assertEqual(self.providers.lookups, [])
        self.assertEqual(self.providers.bodies, [])
        with self.store.connection() as db:
            self.assertFalse(db.execute('SELECT 1 FROM private_reply_attempts WHERE turn_id=?', (turn['id'],)).fetchone())
