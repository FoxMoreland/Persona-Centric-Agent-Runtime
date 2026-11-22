"""Scoped A12 routing through normal desktop intake with numeric transport doubles."""
import json
import unittest
from unittest.mock import AsyncMock, patch

from persona_runtime import lookup_target, private_lookup, semantic_knowledge
from persona_runtime.models import RuntimeErrorBase, identifier
from tests import test_knowledge_study as study


class TargetRoute(unittest.IsolatedAsyncioTestCase):
    reopen = study.Study.reopen
    submit = study.Study.submit
    consent = study.Study.consent
    show = study.Study.show
    paint = study.Study.paint
    continue_turn = study.Study.continue_turn

    def setUp(self):
        study.Study.setUp(self)
        self.providers.semantic_lookup = True
        self.providers.target_resolution = True
        self.providers.bind_target = lambda base,state:(lambda aid:self.resolve(aid,state),{'model':'701'})
        self.resolve = AsyncMock(return_value=({'parts':[]},{'total_tokens':1}))
        self.sources = [{'url':'https://example.org/701','title':'701','content':'701 702','kind':'page_content'}]
        for name,value in [('require_encoder',None),('related_questions',False),('rank',[])]:
            guard = patch.object(semantic_knowledge,name,return_value=value)
            guard.start()
            self.addCleanup(guard.stop)

    async def send_span(self, body, span):
        t = await self.chat.submit('desktop',body,identifier(),self.sid,persona_id=self.pid,
                                   public_task={'question':span,'request_search':True})
        await self.chat.wait()
        return t['id']

    async def seed(self, number='701'):
        tid = await self.send_span(number+' 702',number)
        self.show(tid,'search_notice')
        await self.continue_turn(tid)
        self.show(tid,'main')
        self.resolve.assert_not_awaited()
        return tid

    async def test_lookup_across_chat_and_reopen(self):
        self.consent(True)
        first = await self.seed()
        self.providers.route='none'
        for _ in range(3):
            self.show(await self.submit('703'),'main')
        self.resolve.assert_not_awaited()
        self.providers.route='retrieve'
        def choose(aid, packet):
            subject=next(e for e in packet['fields'] if e['ref']==f'r/{first}/query')
            prop=next(e for e in packet['fields'] if e['kind']=='current')
            return {'parts':[{'ref':subject['ref'],'start':0,'end':3},
                             {'ref':prop['ref'],'start':0,'end':3}]}, {'total_tokens':1}
        self.resolve.side_effect=choose
        tid=await self.send_span('704','704')
        self.show(tid,'search_notice')
        with self.store.connection() as db:
            it=private_lookup.intent(db,tid)
            self.assertEqual(json.loads(it['query_json']),{'question':'701 704'})
            proof=lookup_target.saved(db,tid)
            self.assertEqual(proof['status'],'resolved')
            self.assertEqual(len(proof['pins']),2)
            self.assertEqual(db.execute('SELECT count(*) FROM model_call_meter WHERE category=?',('lookup_target',)).fetchone()[0],1)
            node=db.execute("SELECT node,state FROM private_reply_nodes WHERE attempt_id=?",(proof['attempt_id'],)).fetchone()
            self.assertEqual(tuple(node),(lookup_target.NODE,'received'))
        self.assertTrue(any(node=='J4' and state.get('context',{}).get('fields') for node,_,state in self.providers.calls))
        self.reopen()
        await self.continue_turn(tid)
        self.show(tid,'main')
        self.resolve.assert_awaited_once()
        self.assertTrue(any(q.get('question')=='701 704' for _,_,q in self.providers.lookups))

    async def test_ambiguous_waits_then_uses_explicit_clarification(self):
        self.consent(True)
        tid=await self.submit('704')
        with self.store.connection() as db:
            it=private_lookup.intent(db,tid)
            self.assertEqual(it['state'],'waiting')
            self.assertEqual(lookup_target.saved(db,tid)['status'],'ambiguous')
        self.assertEqual(self.chat.get(tid)['error_code'],'private_lookup_confirmation_required')
        self.assertFalse(self.providers.lookups)
        self.reopen()
        await self.continue_turn(tid)
        self.resolve.assert_awaited_once()
        self.assertFalse(self.providers.lookups)
        op=identifier()
        for _ in range(2):
            self.assertTrue(private_lookup.confirm(self.store,self.pid,self.sid,tid,it['binding'],op,'701 705'))
        await self.continue_turn(tid)
        self.show(tid,'search_notice')
        await self.continue_turn(tid)
        self.show(tid,'main')
        self.assertEqual(self.providers.lookups[0][2]['question'],'701 705')

    async def test_standard_authorization_never_sends_context_to_new_model(self):
        tid=await self.send_span('709','709')
        with self.store.connection() as db:
            self.assertEqual(lookup_target.saved(db,tid)['status'],'needs_consent')
            self.assertEqual(private_lookup.intent(db,tid)['state'],'waiting')
        self.resolve.assert_not_awaited()
        self.assertFalse(self.providers.lookups)

    async def test_direct_link_and_known_unsent_target_recovery(self):
        self.consent(True)
        url='https://example.org/702'
        tid=await self.submit(url)
        with self.store.connection() as db:
            task=json.loads(private_lookup.intent(db,tid)['query_json'])
            self.assertEqual((task['question'],task['url']),(url,url))
        self.resolve.assert_not_awaited()
        self.show(tid,'search_notice')
        await self.continue_turn(tid)
        self.show(tid,'main')
        from persona_runtime.chat import ModelError
        self.resolve.side_effect=ModelError('provider_network_error',diagnostics={
            'request_headers_sent':False,'response_headers_received':False})
        tid=await self.submit('721')
        self.assertEqual(self.chat.get(tid)['error_code'],'desktop_network_wait')
        with self.store.connection() as db:
            self.assertFalse(db.execute("SELECT 1 FROM private_reply_nodes WHERE turn_id=? AND node=?",
                                        (tid,lookup_target.NODE)).fetchone())
        self.resolve.side_effect=None
        self.reopen()
        from persona_runtime.ingress import IngressService
        self.chat.recover()
        ingress=IngressService(self.chat)
        ingress.recover()
        ingress.network_wake()
        ingress.schedule_committed()
        await self.chat.wait()
        self.assertEqual(self.resolve.await_count,2)
        self.assertEqual(self.chat.get(tid)['error_code'],'private_lookup_confirmation_required')

    async def test_lost_resolver_response_is_not_replayed(self):
        self.providers.lookup_target_version=lookup_target.RECENT_CONTEXT_VERSION
        self.consent(True)
        self.resolve.side_effect=TimeoutError()
        tid=await self.submit('719')
        self.assertEqual(self.chat.get(tid)['error_code'],'reply_dispatch_unknown')
        self.reopen()
        self.chat.recover()
        await self.chat.wait()
        self.resolve.assert_awaited_once()
        with self.store.connection() as db:
            node=db.execute('SELECT state,usage_json FROM private_reply_nodes WHERE turn_id=? AND node=?',
                            (tid,lookup_target.NODE)).fetchone()
            self.assertEqual((node[0],json.loads(node[1])['model']),('unknown','701'))
            self.assertFalse(db.execute("SELECT 1 FROM private_reply_nodes WHERE turn_id=? AND node=?",
                                        (tid,lookup_target.RECOVERY_NODE)).fetchone())
        self.assertFalse(self.providers.lookups)

    async def test_new_session_consent_resolves_instead_of_forwarding_the_pending_sentence(self):
        tid=await self.submit('719 720')
        self.resolve.assert_not_awaited()
        self.consent(True)
        self.resolve.side_effect=lambda aid,p:({'parts':[{'ref':p['fields'][0]['ref'],'start':4,'end':7}]},{})
        await self.continue_turn(tid)
        self.show(tid,'search_notice')
        await self.continue_turn(tid)
        self.show(tid,'main')
        self.assertEqual(self.providers.lookups[0][2]['question'],'720')
        self.resolve.assert_awaited_once()

    async def test_requested_update_cannot_be_satisfied_by_original_acquisition(self):
        self.consent(True)
        first=await self.seed()
        def update(aid,p):
            source=next(f for f in p['fields'] if f['ref']==f'r/{first}/query')
            return {'parts':[{'ref':source['ref'],'start':0,'end':3}], 'after':source['ref']},{}
        self.resolve.side_effect=update
        with patch.object(semantic_knowledge,'rank',side_effect=lambda path,q,rows,**kw:
                          [semantic_knowledge.public_material(f) for f in rows[:1]]):
            tid=await self.submit('720')
            self.show(tid,'search_notice')
            with self.store.connection() as db:
                proof=lookup_target.saved(db,tid)
                self.assertIn('after',proof['pins'][-1])
                self.assertNotEqual(db.execute('SELECT outcome FROM local_knowledge_reads WHERE turn_id=?',
                                               (tid,)).fetchone()[0],'hit')
            await self.continue_turn(tid)
            self.show(tid,'main')
            self.assertEqual(self.providers.lookups[-1][2]['question'],proof['task']['question'])

    async def test_rejected_selection_keeps_receipt_without_creating_a_query(self):
        self.consent(True)
        self.resolve.return_value=({'parts':[{'ref':'0','start':0,'end':1}]},{})
        resolve=lookup_target.resolve
        async def legacy(*args):
            outcome=await resolve(*args)
            if outcome['status']=='invalid':
                raise RuntimeErrorBase('lookup_target_invalid')
            return outcome
        with patch.object(lookup_target,'resolve',side_effect=legacy):
            tid=await self.submit('719')
        self.assertEqual(self.chat.get(tid)['error_code'],'lookup_target_invalid')
        self.assertFalse(self.providers.lookups)
        with self.store.connection() as db:
            self.assertIsNone(private_lookup.intent(db,tid))
            old=dict(db.execute('SELECT * FROM private_reply_nodes WHERE turn_id=? AND node=?',
                                (tid,lookup_target.NODE)).fetchone())
        self.assertEqual(old['state'],'received')
        self.reopen()
        await self.continue_turn(tid)
        self.resolve.assert_awaited_once()
        with self.store.connection() as db:
            it=private_lookup.intent(db,tid)
            self.assertEqual((it['state'],it['query_json']),('waiting',None))
            proof=lookup_target.saved(db,tid)
            self.assertEqual((proof['status'],proof['attempt_id']),('invalid',old['attempt_id']))
            self.assertEqual(dict(db.execute('SELECT * FROM private_reply_nodes WHERE attempt_id=?',
                                            (old['attempt_id'],)).fetchone()),old)
        self.assertEqual(self.chat.get(tid)['error_code'],'private_lookup_confirmation_required')
        self.assertFalse(self.providers.lookups)
        private_lookup.confirm(self.store,self.pid,self.sid,tid,it['binding'],identifier(),'721 722')
        await self.continue_turn(tid)
        self.show(tid,'search_notice')
        await self.continue_turn(tid)
        self.show(tid,'main')
        self.assertEqual(self.providers.lookups[0][2]['question'],'721 722')
        self.resolve.assert_awaited_once()

    async def test_short_reference_is_revalidated_against_the_same_source_after_reopen(self):
        self.providers.lookup_target_version=lookup_target.SHORT_REFS_VERSION
        self.sources[0]['title']='701 702 - 701 702'
        self.consent(True)
        first=await self.seed()
        captured={}
        def choose(aid,request):
            fields=request['fields']
            self.assertEqual([f['ref'] for f in fields],[f'S{i}' for i in range(1,len(fields)+1)])
            field=next(f for f in fields if f['kind']=='title')
            self.assertEqual(field['text'],'701 702')
            captured['alias']=field['ref']
            return {'parts':[{'ref':field['ref'],'start':0,'end':3}]},{}
        self.resolve.side_effect=choose
        tid=await self.submit('704')
        with self.store.connection() as db:
            proof=lookup_target.saved(db,tid)
            self.assertEqual(proof['status'],'resolved')
            refs=json.loads(db.execute('SELECT value FROM knowledge_control WHERE key=?',
                                       (lookup_target.CATALOGUE_PREFIX+tid,)).fetchone()[0])['refs']
            self.assertEqual(proof['pins'][0]['ref'],refs[captured['alias']])
            self.assertTrue(proof['pins'][0]['ref'].startswith(f'r/{first}/'))
        calls=len(self.providers.lookups)
        self.show(tid,'search_notice')
        self.reopen()
        await self.continue_turn(tid)
        self.show(tid,'main')
        self.resolve.assert_awaited_once()
        self.assertEqual(len(self.providers.lookups),calls+1)
        self.assertEqual(self.providers.lookups[-1][2]['question'],'701')

    async def test_other_session_topics_stay_out_of_the_catalogue(self):
        self.consent(True)
        first=await self.seed()
        self.sid=self.chat.session(fresh=True,persona_id=self.pid)['session_id']
        self.consent(True)
        tid=await self.submit('710')
        fields=self.resolve.await_args.args[1]['fields']
        self.assertFalse(any(f['ref'].startswith('r/'+first+'/') for f in fields))
        self.assertEqual([f['kind'] for f in fields],['current'])
        self.assertEqual(self.chat.get(tid)['error_code'],'private_lookup_confirmation_required')

    async def test_older_topic_can_be_selected_and_answered_from_eligible_local_material(self):
        self.consent(True)
        first=await self.seed()
        self.sources=[{'url':'https://example.net/721','title':'721','content':'721 722','kind':'page_content'}]
        newer=await self.seed('721')
        calls=len(self.providers.lookups)
        def older(aid, packet):
            ids={f['ref'] for f in packet['fields']}
            self.assertIn(f'r/{newer}/query',ids)
            return {'parts':[{'ref':f'r/{first}/query','start':0,'end':3}]},{}
        self.resolve.side_effect=older
        def rank(path,question,rows,**kw):
            return [semantic_knowledge.public_material(f) for f in rows if f['title']=='701'][:1] if question=='701' else []
        with patch.object(semantic_knowledge,'rank',side_effect=rank):
            tid=await self.submit('729')
            self.show(tid,'main')
            research=self.providers.bodies[-1][1]['research']
            self.assertEqual((research['status'],research['facts'][0]['title']),('local_hit','701'))
        self.resolve.assert_awaited_once()
        self.assertEqual(len(self.providers.lookups),calls)

    async def test_withdrawn_anchor_blocks_dispatch_after_notice(self):
        self.consent(True)
        first=await self.seed()
        self.resolve.side_effect=lambda aid,p:({'parts':[{'ref':f'r/{first}/query','start':0,'end':3}]},{})
        tid=await self.submit('708')
        self.show(tid,'search_notice')
        with self.store.connection(write=True) as db:
            data=private_lookup.evidence(db,self.pid,self.sid,first)
            src=data['sources'][0]
            db.execute("INSERT OR REPLACE INTO knowledge_control VALUES(?,?)",(
                'knowledge.private.lookup.sources.'+self.pid+'.'+self.sid,
                json.dumps({'revision':1,'data':{src['id']:{'url':src['url'],'state':'withdrawn','revision':1}}})))
        calls=len(self.providers.lookups)
        await self.continue_turn(tid)
        self.assertEqual(len(self.providers.lookups),calls)
        self.assertEqual(self.chat.get(tid)['error_code'],'private_reply_source_changed')

    def test_response_must_point_into_this_packet(self):
        packet={'fields':[{'ref':'1','text':'701','revision':'2'}]}
        for part in ({'ref':'3','start':0,'end':2},{'ref':'1','start':True,'end':2},
                     {'ref':'1','start':0,'end':4},{'ref':'1','start':0,'end':2,'query':'703'}):
            with self.subTest(part=part),self.assertRaises(RuntimeErrorBase):
                lookup_target.selection({'parts':[part]},packet)

    def test_quoted_projection_grounds_new_attribute_in_current_message(self):
        fields=[{'ref':'r/1/query','kind':'query','text':'701 702','revision':'11'},
                {'ref':'2','text':'703 704','revision':'12'},{'ref':'3','text':'705 705','revision':'13'}]
        aliases={'S1':'r/1/query','S2':'2','S3':'3'}
        subject={'role':'subject','ref':'S1','quote':'701'}
        attribute={'role':'attribute','ref':'S2','quote':'704'}
        def select(parts):
            return lookup_target.quoted_selection({'parts':parts},{'fields':fields},aliases,['2','3'])
        question,pins=select([subject,attribute])
        self.assertEqual(question,'701 704')
        self.assertEqual([(p['ref'],p['start'],p['end'],p['role']) for p in pins],
                         [('r/1/query',0,3,'subject'),('2',4,7,'attribute')])
        for bad in ([subject],[subject,dict(attribute,ref='S1',quote='702')],
                    [subject,dict(attribute,ref='S3',quote='705')],[subject,dict(attribute,quote='706')]):
            with self.assertRaises(RuntimeErrorBase):
                select(bad)
        self.assertEqual(lookup_target.quoted_selection({'repeat':'S1'},{'fields':fields},aliases,['2'])[0],'701 702')

    def test_connection_change_binds_model_and_account_to_session_consent(self):
        from persona_runtime import session_privacy
        self.consent(True)
        args=(self.store,self.config,'lookup_target','https://api.deepseek.com','deepseek-v4-flash','712')
        pre=session_privacy.connection(*args)
        self.assertEqual(pre['affected'],[self.sid])
        self.assertNotIn('lookup_target_model',self.config.load())
        session_privacy.connection(*args,confirm=pre['confirmation'])
        self.assertTrue(session_privacy.status(self.store,self.config,self.pid,self.sid)['enabled'])
        self.assertNotIn('encrypted_key',self.config.public()['lookup_target_model'])
        self.config.update({'lookup_target_model':{'name':'deepseek-flash'}})
        self.assertFalse(session_privacy.status(self.store,self.config,self.pid,self.sid)['enabled'])

    def test_dedicated_target_hourly_allowance(self):
        from persona_runtime import call_limits
        self.config.update({'lookup_target_model':{'base_url':'https://api.deepseek.com',
                            'name':'deepseek-v4-flash','api_key':'713','hourly_limit':1}})
        with self.store.connection(write=True) as db:
            call_limits.take(db,self.config,'710','lookup_target',now=10000)
            call_limits.take(db,self.config,'711','private_jev',now=10000)
            with self.assertRaises(call_limits.CallLimitPending) as pending:
                call_limits.take(db,self.config,'712','lookup_target',now=10010)
            self.assertEqual(pending.exception.reason,'lookup_target_hourly')
