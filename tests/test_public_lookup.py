"""Public work orders, independently supported passages and real desktop presentation receipts."""
import hashlib
import json
import unittest
from unittest.mock import AsyncMock, patch

from persona_runtime import private_lookup, public_lookup, public_lookup_study, semantic_knowledge, session_privacy
from persona_runtime.models import RuntimeErrorBase, identifier
from persona_runtime.private_reply_output import FACT_REVIEW_VERSION
from tests import test_lookup_target as target

UNUSED_LOOKUP_NOTE='fixture-unused-lookup-note-799'


class LookupCallEvidence(unittest.TestCase):
    def test_writer_reconciles_only_received_guidance_fingerprints(self):
        from scripts.pcar_desktop_turn_evidence import private_reply_evidence

        trace={"applied":True,"request_sha256":"a"*64,"note_sha256":"b"*64}
        row={"id":"701","state":"received","usage_json":None}
        usage={"evidence_guidance":trace|{"raw_text":"702"},"credentials":"703"}
        report=private_reply_evidence(row|{"usage_json":json.dumps(usage)})
        self.assertEqual(report,{"id":"701","state":"received","evidence_guidance":trace})
        self.assertEqual(private_reply_evidence(row),{"id":"701","state":"received"})
        self.assertEqual(private_reply_evidence(row|{"usage_json":"{"}),{"id":"701","state":"received"})
        unknown=private_reply_evidence(row|{"state":"unknown","usage_json":json.dumps(usage)})
        self.assertNotIn("evidence_guidance",unknown)
        usage["evidence_guidance"]["note_sha256"]="704"
        self.assertNotIn("evidence_guidance",private_reply_evidence(row|{"usage_json":json.dumps(usage)}))
        usage["evidence_guidance"]["applied"]=False
        inactive=private_reply_evidence(row|{"usage_json":json.dumps(usage)})
        self.assertEqual(inactive["evidence_guidance"],{"applied":False,"request_sha256":"a"*64})

    def test_native_receipt_preserves_timing_and_tool_counts_only(self):
        from scripts.pcar_desktop_turn_evidence import lookup_call_evidence

        row={"step":"item.2.native","attempt_id":"701","binding":"702","state":"received",
             "result_json":json.dumps({"sources":[{"text":"903"}]}),
             "usage_json":json.dumps({
                 "transport":{"elapsed_ms":152000,"first_event_ms":1063,"first_content_delta_ms":146719,"http_status":200},
                 "usage":{"prompt_tokens":822975,"completion_tokens":17047,
                          "completion_tokens_details":{"reasoning_tokens":13364},
                          "server_tool_use":{"tool_search":36,"tool_fetch":9,"tool_image_search":0}},
                 "native":{"assistant":{"content":"904"}},
             })}
        report=lookup_call_evidence(row)
        self.assertEqual(report["timing"],{"elapsed_ms":152000,"first_event_ms":1063,"first_content_delta_ms":146719})
        self.assertEqual(report["provider_tool_counts"],{"tool_search":36,"tool_fetch":9,"tool_image_search":0})
        self.assertEqual(report["tokens"]["reasoning_tokens"],13364)
        self.assertNotIn("903",json.dumps(report))
        self.assertNotIn("904",json.dumps(report))

    def test_failed_and_historical_attempts_do_not_invent_measurements(self):
        from scripts.pcar_desktop_turn_evidence import lookup_call_evidence

        row={"step":"item.1.fetch","attempt_id":"701","binding":"702","state":"failed","usage_json":None}
        failed=lookup_call_evidence(row|{"result_json":json.dumps({"diagnostics":{"http_status":404,"elapsed_ms":98}})})
        self.assertEqual((failed["http_status"],failed["timing"]), (404,{"elapsed_ms":98}))
        old=lookup_call_evidence(row|{"result_json":json.dumps({"diagnostics":{"http_status":404}})})
        self.assertNotIn("timing",old)
        self.assertNotIn("provider_tool_counts",old)


class PublicLookup(unittest.IsolatedAsyncioTestCase):
    reopen=target.TargetRoute.reopen
    submit=target.TargetRoute.submit
    consent=target.TargetRoute.consent
    show=target.TargetRoute.show
    paint=target.TargetRoute.paint
    continue_turn=target.TargetRoute.continue_turn
    send_span=target.TargetRoute.send_span

    def setUp(self):
        target.TargetRoute.setUp(self)
        self.providers.public_lookup_version=public_lookup.VERSION
        self.providers.structured_study=True
        self.providers.knowledge_revisions=True
        self.providers.output_contract_version=FACT_REVIEW_VERSION
        self.sources=[{'url':'https://example.org/701','title':'701','content':'701 702 703',
                       'qualification':'710','kind':'page_content','date':'2025-01-01'}]
        self.accept=True
        self.native=[]
        self.failed_step=None
        choose=self.providers.choose
        async def select(node,aid,state,options):
            if node=='J5' and 'source' in state:
                self.assertEqual(options,('complete','source/0','insufficient'))
                return ('complete' if self.accept else 'insufficient'),{}
            return await choose(node,aid,state,options)
        self.providers.choose=select
        async def retrieve(step,aid,query,model):
            self.providers.lookups.append((step,aid,query))
            if self.failed_step==step:raise TimeoutError()
            sources=self.native if step=='native' else self.sources
            return {'sources':sources,'tool_calls':int(bool(sources)),'notes':UNUSED_LOOKUP_NOTE},{}
        self.providers.retrieve=retrieve
        rank=patch.object(semantic_knowledge,'rank',side_effect=lambda paths,q,rows,**kw:
                          [semantic_knowledge.public_material(r) for r in rows][:6])
        rank.start()
        self.addCleanup(rank.stop)

    def grant(self,enabled=True,op=None):
        s=session_privacy.status(self.store,self.config,self.pid,self.sid)['public_lookup']
        return session_privacy.update(self.store,self.config,self.pid,self.sid,enabled,s['revision'],
                                      s['recipient_hash'],op or identifier(),s['protocol'])

    async def test_world_backfill_and_publication_scope(self):
        from persona_runtime import persona_world as world
        instance=self.config.load()['instance_id']
        self.sources[0]['kind']='search_content'
        self.consent(True)
        tid=await self.cold()
        self.show(tid,'main')
        with self.store.connection(write=True) as db:
            self.assertEqual(world.attestations(db,instance,self.pid),[])
            entries=world.inspect_turn(db,instance,self.pid,tid)
            self.assertEqual(len(entries),1)
            self.assertEqual(world.index_turn(db,instance,self.pid,tid),1)
            self.assertEqual(world.index_turn(db,instance,self.pid,tid),0)
            self.assertFalse(world.inspect_turn(db,identifier(),self.pid,tid))
            self.assertFalse(world.inspect_turn(db,instance,identifier(),tid))
            item=entries[0]
            world.withdraw_source(db,instance,self.pid,item['url'],item['sha256'])
        self.reopen()
        with self.store.connection() as db:
            self.assertEqual(world.inspect_turn(db,instance,self.pid,tid),[])
            self.assertEqual(world.attestations(db,instance,self.pid),entries)

    async def test_world_rechecks_origin_correction_and_recipient(self):
        from persona_runtime import persona_world as world
        from persona_runtime.knowledge_service import KnowledgeService
        instance=self.config.load()['instance_id']
        self.enable_world()
        await self.check_local_clock(new_window=True)
        consent=session_privacy.status(self.store,self.config,self.pid,self.sid)
        base={'instance':instance,'consent':consent}
        with self.store.connection() as db:
            turn=dict(db.execute("SELECT * FROM turns WHERE session_id=? ORDER BY rowid DESC LIMIT 1",(self.sid,)).fetchone())
            entries=world.attestations(db,instance,self.pid)
            self.assertEqual(len(entries),1)
            entry=entries[0]
            expected={entry['fact_id']:entry['session_id']}
            self.assertEqual(world.eligible_fact_ids(db,turn,base,list(expected)),expected)
            self.assertFalse(world.eligible_fact_ids(db,turn,{**base,'consent':{**consent,'recipient_hash':identifier()}}))
            witness=session_privacy._witness(self.config,self.config.load(),entry['session_id'])
            original=witness.read_bytes()
            witness.write_bytes(b'{}')
            try:
                self.assertFalse(world.eligible_fact_ids(db,turn,base,list(expected)))
                self.assertTrue(world.inspect_turn(db,instance,self.pid,entry['origin']))
            finally:
                witness.write_bytes(original)
            self.assertEqual(world.eligible_fact_ids(db,turn,base,list(expected)),expected)
        private_lookup.edit_source(KnowledgeService(self.store),self.pid,entry['source_id'],'corrected',0,identifier(),'704')
        with self.store.connection() as db:
            self.assertFalse(world.eligible_fact_ids(db,turn,base,list(expected)))

    def test_world_public_grant_requires_matching_witness(self):
        from persona_runtime import persona_world as world
        self.consent(True)
        self.grant()
        cfg=self.config.load()
        instance=cfg['instance_id']
        consent=session_privacy.status(self.store,self.config,self.pid,self.sid)
        path=session_privacy._witness(self.config,cfg,self.sid).with_suffix('.public.json')
        with self.store.connection() as db:
            scope=session_privacy.public_scope(db,self.config,instance,self.pid,self.sid,consent)
            args=(db,world._live_privacy(db,instance),self.pid,self.sid,consent['revision'],consent['recipient_hash'],scope)
            self.assertTrue(world._live_grant(*args))
            path.write_bytes(b'{}')
            self.assertFalse(world._live_grant(*args))

    async def test_new_recipient_approval_in_b_cannot_use_a_previous_account(self):
        from persona_runtime import persona_world as world
        from persona_runtime.config import atomic_json
        self.enable_world()
        await self.check_local_clock(new_window=True)
        cfg=self.config.load()
        cfg['model']['base_url']='https://example.org/v1'
        atomic_json(self.config.file,cfg)
        self.consent(True)
        consent=session_privacy.status(self.store,self.config,self.pid,self.sid)
        self.assertTrue(consent['enabled'])
        instance=cfg['instance_id']
        with self.store.connection() as db:
            turn=dict(db.execute("SELECT * FROM turns WHERE session_id=? ORDER BY rowid DESC LIMIT 1",(self.sid,)).fetchone())
            entry=world.attestations(db,instance,self.pid)[0]
            self.assertTrue(world.inspect_turn(db,instance,self.pid,entry['origin']))
            self.assertFalse(world.eligible_fact_ids(db,turn,{'instance':instance,'consent':consent}))

    async def test_world_origin_consent_revocation_preserves_other_acquisitions(self):
        from persona_runtime import persona_world as world
        instance=self.config.load()['instance_id']
        self.enable_world()
        self.consent(True)
        first=await self.cold()
        self.show(first,'main')
        first_sid=self.sid
        self.sid=self.chat.session(fresh=True)['session_id']
        self.consent(True)
        second=await self.cold()
        self.show(second,'main')
        with self.store.connection() as db:
            original,=world.inspect_turn(db,instance,self.pid,first)
            duplicate,=world.inspect_turn(db,instance,self.pid,second)
            self.assertNotEqual(original['fact_id'],duplicate['fact_id'])
            self.assertEqual(world.passage_id(instance,self.pid,original),world.passage_id(instance,self.pid,duplicate))
        self.sid=first_sid
        self.consent(False)
        with self.store.connection() as db:
            self.assertFalse(world.inspect_turn(db,instance,self.pid,first))
            self.assertTrue(world.inspect_turn(db,instance,self.pid,second))

    async def cold(self,body='701 709',span='701'):
        tid=await self.send_span(body,span)
        self.show(tid,'search_notice')
        await self.continue_turn(tid)
        return tid

    def trace(self,tid):
        with self.store.connection() as db:
            return public_lookup.trace(db,tid)

    def select_topic(self,origin,*,end=3):
        def resolve(aid,packet):
            previous=next(f for f in packet['fields'] if f['ref']==f'r/{origin}/query')
            current=next(f for f in packet['fields'] if f['kind']=='current')
            return {'parts':[{'ref':previous['ref'],'start':0,'end':3},
                             {'ref':current['ref'],'start':0,'end':end}]},{}
        self.resolve.side_effect=resolve

    async def test_uncertain_main_is_inline_and_one_new_render_restores_history(self):
        from persona_runtime.workbench_timeline import page
        from persona_runtime.ordinary_materials import _confirmed
        self.providers.route='none'
        self.consent(True)
        tid=await self.submit('701')
        uid=self.out.list(self.pid,self.sid,tid)[0]['id']
        plan=self.out.plan(self.pid,self.sid,uid)
        original=self.paint(uid,plan,0,'unknown')
        card=page(self.store,self.pid,self.sid)['turns'][-1]
        reply=card['inline_reply']
        self.assertEqual(''.join(m['text'] for m in reply['items']),original)
        self.assertTrue(reply['confirmable'])
        args=(self.pid,self.sid,tid,reply['publication'],reply['revision'])
        with self.assertRaises(RuntimeErrorBase):
            self.out.confirm_inline_main(*args,[])
        fragments=[[m['id'],m['body_hash']] for m in reply['items']]
        for _ in range(2):
            self.assertTrue(self.out.confirm_inline_main(*args,fragments)['confirmed'])
        confirmed=page(self.store,self.pid,self.sid)['turns'][-1]
        self.assertEqual(confirmed['status'],'completed')
        self.assertNotIn('inline_reply',confirmed)
        self.assertEqual([p['text'] for p in confirmed['desktop_publications'][0]['visible']],[original])
        with self.store.connection() as db:
            self.assertTrue(_confirmed(db,tid,{'id':confirmed['reply_event_id'],'text':original}))
            self.assertEqual([r[0] for r in db.execute("SELECT state FROM expression_speech_receipts WHERE fragment_id=? ORDER BY rowid",
                                                      (fragments[0][0],))],['unknown','presented'])
            self.assertEqual(db.execute("SELECT count(*) FROM private_reply_attempts WHERE turn_id=?",(tid,)).fetchone()[0],1)

    async def test_inline_recovery_rechecks_privacy_after_the_page_read(self):
        from persona_runtime.workbench_timeline import page
        self.providers.route='none'
        self.consent(True)
        tid=await self.submit('702')
        uid=self.out.list(self.pid,self.sid,tid)[0]['id']
        plan=self.out.plan(self.pid,self.sid,uid)
        self.paint(uid,plan,0,'unknown')
        reply=page(self.store,self.pid,self.sid)['turns'][-1]['inline_reply']
        self.consent(False)
        self.assertNotIn('inline_reply',page(self.store,self.pid,self.sid)['turns'][-1])
        with self.assertRaises(RuntimeErrorBase):
            self.out.confirm_inline_main(self.pid,self.sid,tid,reply['publication'],reply['revision'],
                                         [[i['id'],i['body_hash']] for i in reply['items']])
        with self.store.connection() as db:
            self.assertIsNone(db.execute('SELECT reply_event_id FROM turns WHERE id=?',(tid,)).fetchone()[0])

    async def test_unknown_notice_cannot_be_reconciled_as_a_main_reply(self):
        from persona_runtime.workbench_timeline import page
        self.consent(True)
        tid=await self.send_span('701 709','701')
        uid=self.out.list(self.pid,self.sid,tid)[0]['id']
        plan=self.out.plan(self.pid,self.sid,uid)
        self.paint(uid,plan,0,'unknown')
        self.assertNotIn('inline_reply',page(self.store,self.pid,self.sid)['turns'][-1])
        with self.assertRaises(RuntimeErrorBase):
            self.out.confirm_inline_main(self.pid,self.sid,tid,plan['id'],plan['revision'],[])
        self.assertFalse(self.providers.lookups)

    async def test_shared_order_and_delivery_bindings(self):
        tid=await self.send_span('701 709','701')
        before=self.trace(tid)
        self.assertEqual((before['intent'],before['decision'],before['ready'],before['notice_event'],before['calls']),
                         ('button','retrieve',True,None,[]))
        await self.continue_turn(tid)
        self.assertFalse(self.providers.lookups)
        self.show(tid,'search_notice')
        self.reopen()
        await self.continue_turn(tid)
        pending=self.trace(tid)
        self.assertEqual(pending['outcome'],'supported')
        self.assertFalse(pending['delivery']['fragments'])
        body=self.show(tid,'main')
        t=self.trace(tid)
        self.assertIn('703',body)
        self.assertEqual(t['facts'],t['delivery']['facts'])
        self.assertTrue(t['delivery']['fragments'])
        self.assertEqual(t['delivery']['state'],'complete')
        self.assertTrue(t['notice_event'])
        self.assertEqual([a['step'] for a in t['calls']],['native','search'])
        self.assertEqual({a['order_hash'] for a in t['calls']},{before['order_hash']})
        self.assertEqual(len({a['query_hash'] for a in t['calls']}),1)
        self.assertEqual(t['delivery']['body_sha256'],hashlib.sha256(body.encode()).hexdigest())
        self.assertEqual(self.providers.bodies[-1][1]['research']['public_result'],{'question':'701','outcome':'supported'})
        self.assertNotIn(UNUSED_LOOKUP_NOTE,json.dumps(self.providers.bodies[-1][1]['research']))
        detail=private_lookup.details(self.store,self.pid,self.sid,tid,'research')
        self.assertEqual(detail['sources'][0]['qualification'],'710')
        self.assertEqual(self.providers.lookups[0][2]['question'],self.providers.lookups[1][2]['query'])

    async def test_eight_hits_without_support_never_enter_recall_or_details(self):
        self.accept=False
        self.sources=[dict(self.sources[0],url=f'https://example.org/{i}') for i in range(8)]
        tid=await self.cold()
        self.show(tid,'main')
        self.assertEqual(self.trace(tid)['calls'][-1]['results'],8)
        self.assertEqual(self.trace(tid)['outcome'],'no_relevant_evidence')
        self.assertIsNone(self.trace(tid)['revision'])
        self.assertEqual(self.providers.bodies[-1][1]['research']['facts'],[])
        self.assertEqual(private_lookup.details(self.store,self.pid,self.sid,tid,'research')['sources'],[])
        with self.store.connection(write=True) as db:
            self.assertIsNone(private_lookup.evidence(db,self.pid,self.sid,tid))
            scope={'instance':self.config.load()['instance_id'],'consent':{'enabled':False,'revision':0}}
            next_turn={'id':'','persona_id':self.pid,'session_id':self.sid}
            semantic_knowledge.import_prior_sources(db,next_turn,scope)
            self.assertFalse(semantic_knowledge.materials(db,next_turn,scope,'701'))
            self.assertEqual(db.execute('SELECT count(*) FROM local_knowledge_documents').fetchone()[0],0)

    async def test_separate_scope_resume_revoke_and_single_use_confirmation(self):
        self.consent(True)
        first=await self.cold()
        self.show(first,'main')
        self.select_topic(first)
        tid=await self.send_span('702','702')
        with self.store.connection() as db:
            it=private_lookup.intent(db,tid)
            self.assertEqual(it['state'],'waiting')
        self.assertFalse(self.trace(tid)['ready'])
        original_calls=len(self.providers.lookups)
        self.grant()
        self.reopen()
        await self.continue_turn(tid)
        self.show(tid,'search_notice')
        self.assertTrue(self.trace(tid)['ready'])
        self.grant(False)
        await self.continue_turn(tid)
        self.assertEqual(len(self.providers.lookups),original_calls)
        self.assertEqual(self.trace(tid)['calls'],[])
        self.grant()
        await self.continue_turn(tid)
        self.assertEqual(len(self.providers.lookups),original_calls)

    async def test_ordinary_followup_and_version_chain(self):
        self.consent(True)
        self.grant()
        first=await self.cold()
        self.show(first,'main')
        self.select_topic(first)
        self.sources=[dict(self.sources[0],content='701 702 705',date='2025-02-01')]
        # A local passage is related but cannot answer this follow-up.
        choose=self.providers.choose
        async def fresh(node,aid,state,options):
            if node=='J5' and state.get('source',{}).get('text')=='701 702 703':
                return 'insufficient',{}
            return await choose(node,aid,state,options)
        self.providers.choose=fresh
        tid=await self.submit('702')
        self.show(tid,'search_notice')
        await self.continue_turn(tid)
        self.assertIn('705',self.show(tid,'main'))
        t=self.trace(tid)
        self.assertEqual((t['intent'],t['revision']['previous']),('conversation',first))
        self.assertTrue(t['revision']['previous_sources'])
        detail=private_lookup.details(self.store,self.pid,self.sid,first,'research')
        self.assertIn('705',detail['notes'])
        self.assertEqual(detail['original']['sources'][0]['text'],'701 702 703')
        n=len(self.providers.lookups)
        nxt=await self.submit('702')
        self.assertIn('705',self.show(nxt,'main'))
        self.assertEqual(len(self.providers.lookups),n)

    async def test_unconfirmed_fetch_preserves_transport_without_admitting_partial_results(self):
        self.native=[dict(self.sources[0],content=None,summary='701')]
        self.failed_step='fetch'
        tid=await self.cold()
        self.show(tid,'main')
        t=self.trace(tid)
        self.assertEqual(t['outcome'],'unconfirmed')
        self.assertEqual([a['state'] for a in t['calls']],['received','unknown'])
        self.assertIsNone(t['revision'])
        self.assertEqual(t['delivery']['facts'],[])
        self.assertEqual(self.providers.bodies[-1][1]['research']['public_result']['outcome'],'unconfirmed')

    async def test_private_fragment_cannot_be_exported_without_per_turn_confirmation(self):
        self.consent(True)
        self.grant()
        first=await self.cold()
        self.show(first,'main')
        self.select_topic(first)
        self.accept=False
        tid=await self.submit('711')
        self.assertFalse(self.trace(tid)['ready'])
        with self.store.connection() as db:
            binding=private_lookup.intent(db,tid)['binding']
        op=identifier()
        for _ in range(2):
            private_lookup.confirm(self.store,self.pid,self.sid,tid,binding,op,'713')
        await self.continue_turn(tid)
        self.show(tid,'search_notice')
        await self.continue_turn(tid)
        self.show(tid,'main')
        self.assertEqual(self.providers.lookups[-1][2],{'query':'713'})
        self.assertEqual(self.providers.bodies[-1][1]['research']['public_result']['question'],'713')

    async def test_long_selection_is_never_truncated_for_fallback(self):
        q='7'*129
        tid=await self.cold(q+' 8',q)
        self.show(tid,'main')
        self.assertEqual([call[0] for call in self.providers.lookups],['native'])
        self.assertEqual(self.trace(tid)['fallback'],'query_exceeds_search_limit')
        self.assertEqual(self.providers.lookups[0][2]['question'],q)

    def test_attribution_requires_the_exact_statement_and_offered_source(self):
        from persona_runtime.reply_provenance import references,unpack
        facts=[{'id':'1','text':'710 711'}]
        item={'field':'detail','claim':'711','fact':'1','quote':'710 711'}
        raw=json.dumps({'body':'7','detail':'711','claims':[item]})
        slots,ann=unpack(raw,True)
        self.assertEqual(references(slots['body'],slots['detail'],ann,facts)[0]['passage'],[0,7])
        for change in ({'fact':'2'},{'quote':'720'},{'field':'body'},{'claim':'712'}):
            self.assertIsNone(references('7','711',{'claims':[item|change]},facts))
        self.assertIsNone(references('7','111',{'claims':[item|{'claim':'11'}]},facts))
        self.assertIn('error',unpack('{"body":"7","claims":[],"claims":[]}',False)[1])

    def test_literal_quote_uses_unique_coverage_witness_without_inventing_claims(self):
        from persona_runtime import reply_provenance,research_contract
        from persona_runtime.archive import fingerprint
        fact={'id':'701','text':'702 703 702','title':'704','url':'https://example.org/704'}
        alternate=dict(fact,id='705')
        with self.store.connection(write=True) as db:
            phase={'witnesses':[{'id':'701','revision':fingerprint(fact),'span':[0,7]}]}
            public_lookup.freeze(db,research_contract.PREFIX+'799.readback',phase)
            research={'lookup_outcome':'studied','facts':[fact,alternate]}
            self.assertEqual(reply_provenance.literal_citations(db,'799','“702”',None,research),
                             [{'claim':'702','quote':'702 703','title':'704','url':'https://example.org/704'}])
            self.assertFalse(reply_provenance.literal_citations(db,'799','「706」',None,research))
            self.assertFalse(reply_provenance.literal_citations(db,'799','“702”',None,research,[{'claim':'702 703'}]))

    def test_redundant_reply_action_cannot_override_planned_action(self):
        from persona_runtime.reply_provenance import unpack
        raw=json.dumps({'body':'701','action':'respond','claims':[]})
        self.assertEqual(unpack(raw,False,action='respond'),('701',{'claims':[]}))
        self.assertIn('error',unpack(raw,False,action='clarify')[1])
        wrong=json.dumps({'body':'701','action':'respond','claims':[],'unknown':None})
        self.assertIn('error',unpack(wrong,False,action='respond')[1])
        empty_detail=json.dumps({'body':'701','detail':'','claims':[]})
        self.assertEqual(unpack(empty_detail,False),('701',{'claims':[]}))
        self.assertIn('error',unpack(empty_detail,True)[1])
        self.assertIn('error',unpack(empty_detail.replace('""','"702"'),False)[1])
        echo=json.dumps({'body':'701','mode':'chat','claims':[]})
        self.assertEqual(unpack(echo,False,mode='chat'),('701',{'claims':[]}))
        self.assertIn('error',unpack(echo,False,mode='short_answer')[1])
        long_echo=json.dumps({'body':'701','detail':'702','mode':'long_detail','claims':[]})
        self.assertEqual(unpack(long_echo,True,mode='long_detail'),({'body':'701','detail':'702'},{'claims':[]}))

    def test_quoted_roles_recover_a_single_current_span_from_a_public_title(self):
        from persona_runtime.lookup_target import quoted_selection
        ref='r/701/702'
        catalogue={'fields':[{'ref':ref,'text':'710 711','revision':'720'},
                             {'ref':'703','text':'712 713','revision':'721'}]}
        aliases={'S1':'703','S2':ref}
        parts=[{'ref':'S2','quote':'710','role':'subject'},
               {'ref':'S2','quote':'711','role':'attribute'},
               {'ref':'S1','quote':'712','role':'qualifier'}]
        question,pins=quoted_selection({'parts':parts},catalogue,aliases,{'703'})
        self.assertEqual(question,'710 712')
        self.assertEqual([(pin['ref'],pin['role']) for pin in pins],[(ref,'subject'),('703','attribute')])
        with self.assertRaises(RuntimeErrorBase):
            quoted_selection({'parts':parts+[{'ref':'S1','quote':'713','role':'qualifier'}]},catalogue,aliases,{'703'})

    async def test_ambiguous_followup_recovers_a_public_subject_from_preceding_acquisition(self):
        from persona_runtime import lookup_target
        from persona_runtime.config import atomic_json
        self.providers.lookup_target_version=lookup_target.QUOTED_TARGET_VERSION
        marker=self.config.paths.home/'private/ordinary-reply/registration.json'
        atomic_json(marker,{'instance':self.config.load()['instance_id'],'current_public_attribute':True})
        self.consent(True)
        self.grant()
        first=await self.cold()
        self.show(first,'main')
        sent=len(self.providers.lookups)
        self.accept=False
        def resolve(aid,packet):
            fields=packet['fields']
            current=next(f for f in fields if f['kind']=='current')
            if current['ref'].startswith('S'):
                return {'parts':[]},{}
            self.assertEqual({f['kind'] for f in fields},{'current','query','title'})
            title=next(f for f in fields if f['kind']=='title')
            return {'parts':[{'ref':title['ref'],'role':'subject','quote':'701'},
                             {'ref':current['ref'],'role':'attribute','quote':'778'}]},{}
        self.resolve.side_effect=resolve
        tid=await self.submit('778')
        self.assertEqual(self.resolve.await_count,2)
        with self.store.connection() as db:
            proof=lookup_target.saved(db,tid)
            work=public_lookup.saved(db,public_lookup.key(tid,'order'))
            self.assertTrue(proof['recovery'])
            self.assertEqual(work['authority']['topics'],[first])
            self.assertEqual(work['query']['question'],'701 778')
        self.assertEqual(len(self.providers.lookups),sent)
        self.show(tid,'search_notice')
        await self.continue_turn(tid)
        self.show(tid,'main')
        self.assertEqual(self.providers.lookups[sent][2]['question'],'701 778')

    async def test_unresolved_lookup_shows_no_search_status_after_rejected_drafts(self):
        from persona_runtime import lookup_target, reply_provenance
        from persona_runtime.workbench_timeline import page
        self.providers.lookup_target_version=lookup_target.QUOTED_TARGET_VERSION
        self.providers.provenance_version=reply_provenance.VERSION
        self.consent(True)
        self.grant()
        self.resolve.return_value=({'parts':[]},{})
        original_choose=self.providers.choose
        async def choose(node,aid,state,options):
            if node==reply_provenance.NODE:
                return 'unbound',{}
            return await original_choose(node,aid,state,options)
        self.providers.choose=choose
        async def write(aid,packet):
            return '701',{'lookup_attribution':{'claims':[]}}
        self.providers.reply=write
        tid=await self.submit('778')
        card=page(self.store,self.pid,self.sid)['turns'][-1]
        self.assertTrue(card['lookup_scope_unresolved'])
        self.assertIsNone(card['reply_event_id'])
        self.assertFalse(self.trace(tid)['calls'])
        self.assertFalse(self.out.list(self.pid,self.sid,tid))

    def enable_research_contract(self,assessment):
        from persona_runtime import lookup_target, research_contract
        from persona_runtime.config import atomic_json
        self.providers.research_contract_version=research_contract.VERSION
        self.providers.lookup_target_version=lookup_target.QUOTED_TARGET_VERSION
        self.providers.route='retrieve'
        self.providers.bind_coverage=lambda base,state: (lambda aid:assessment(aid,state),{'model':'801'})
        atomic_json(self.config.paths.home/'private/ordinary-reply/registration.json',
                    {'instance':self.config.load()['instance_id'],'current_public_attribute':True})
        if not session_privacy.status(self.store,self.config,self.pid,self.sid)['enabled']:
            self.consent(True)
        self.grant()
        def resolve(aid,packet):
            current=next(f for f in packet['fields'] if f['kind']=='current')
            return {'precision':'clock_time','parts':[
                {'ref':current['ref'],'role':'subject','quote':'701'},
                {'ref':current['ref'],'role':'attribute','quote':'778'}]},{}
        self.resolve.side_effect=resolve

    async def seed_research_contract(self,payload):
        self.consent(True)
        self.sources[0]['content']=payload
        tid=await self.cold()
        self.show(tid,'main')

    def enable_world(self):
        from persona_runtime import persona_world
        self.sources[0]['kind']='search_content'
        with self.store.connection(write=True) as db:
            persona_world.set_enabled(db,self.config.load()['instance_id'],self.pid,True)

    async def test_original_local_first_route_shares_a_public_acquisition(self):
        from persona_runtime import persona_world

        self.enable_world()
        self.providers.target_resolution=False
        self.providers.public_lookup_version=None
        self.consent(True)
        origin=await self.submit('701')
        self.show(origin,'search_notice')
        await self.continue_turn(origin)
        self.show(origin,'main')
        with self.store.connection() as db:
            self.assertTrue(persona_world.inspect_turn(db,self.config.load()['instance_id'],self.pid,origin))
        self.sid=self.chat.session(fresh=True)['session_id']
        self.consent(True)
        sent=len(self.providers.lookups)
        followup=await self.submit('701')
        self.show(followup,'main')
        self.assertEqual(len(self.providers.lookups),sent)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT outcome FROM local_knowledge_reads WHERE turn_id=?',
                                        (followup,)).fetchone()[0],'hit')
            self.assertFalse(db.execute("""SELECT 1 FROM private_reply_nodes WHERE turn_id=?
                AND (node LIKE 'lookup.target%' OR node LIKE 'lookup.coverage%' OR node='lookup.attribution')""",
                (followup,)).fetchone())

    async def test_date_only_can_be_retained_while_missing_clock_time_triggers_one_lookup(self):
        await self.check_missing_clock(new_window=False)

    async def test_persona_world_adds_missing_clock_from_new_session(self):
        self.enable_world()
        await self.check_missing_clock(new_window=True)

    async def test_world_does_not_share_passages_rejected_on_readback(self):
        from persona_runtime import persona_world, research_contract
        self.enable_world()
        judge=AsyncMock(return_value=({'outcome':'insufficient','gap':'information','witnesses':[]},{}))
        self.enable_research_contract(judge)
        tid=await self.submit('701 778')
        self.show(tid,'search_notice')
        await self.continue_turn(tid)
        self.show(tid,'main')
        judge.assert_awaited_once()
        with self.store.connection() as db:
            self.assertEqual(research_contract.coverage(db,tid,'readback')['outcome'],'insufficient')
            self.assertEqual(db.execute('SELECT status FROM knowledge_study_commits WHERE turn_id=?',
                                        (tid,)).fetchone()[0],'committed')
            self.assertFalse(persona_world.attestations(db,self.config.load()['instance_id'],self.pid))

    async def check_missing_clock(self,*,new_window):
        from persona_runtime import research_contract
        date='701 778 2026-12-24'
        clock=date+' 08:15'
        await self.seed_research_contract(date)
        if new_window:
            self.sid=self.chat.session(fresh=True)['session_id']
        offered=[]
        async def assess(aid,state):
            offered.append(state)
            self.assertEqual(state['precision'],'clock_time')
            src=next((p for p in state['sources'] if p['text']==clock),state['sources'][0])
            witness={'id':src['id'],'quote':src['text']}
            if src['text']==clock:witness['clock']='08:15'
            return {'outcome':'complete','gap':'none','witnesses':[witness]},{}
        self.enable_research_contract(assess)
        self.sources=[dict(self.sources[0],url='https://example.org/778',content=clock)]
        sent=len(self.providers.lookups)
        tid=await self.submit('701 778')
        with self.store.connection() as db:
            local=research_contract.coverage(db,tid,'local')
            need=research_contract.request(db,tid)
            order=public_lookup.saved(db,public_lookup.key(tid,'order'))
            self.assertEqual((local['outcome'],local['gap']),('partial','precision'))
            self.assertEqual(local['reason'],'clock_witness_missing')
            self.assertEqual(order['research_request'],research_contract.fingerprint(need))
        self.assertEqual(len(self.providers.lookups),sent)
        self.show(tid,'search_notice')
        await self.continue_turn(tid)
        self.show(tid,'main')
        self.assertEqual(len(offered),2)
        self.assertEqual({p['text'] for p in offered[1]['sources']},{date,clock})
        self.assertEqual(self.providers.lookups[sent][2]['question'],'701 778')
        self.assertEqual(self.trace(tid)['outcome'],'supported')
        actual=self.providers.bodies[-1][1]['research']
        self.assertEqual({f['text'] for f in actual['facts']},{clock})
        self.assertEqual(actual['assessment']['coverage'],'complete')

    async def test_exact_local_witness_stops_search_without_confusing_publication_time(self):
        await self.check_local_clock(new_window=False)

    async def test_standalone_reference_retries_resolution_before_any_public_call(self):
        from persona_runtime import lookup_target
        await self.seed_research_contract('701 778 2026-12-24 08:15')
        self.enable_research_contract(AsyncMock(return_value=(
            {'outcome':'insufficient','gap':'information','witnesses':[]},{})))
        self.resolve.reset_mock()

        def unresolved(aid,packet):
            current=next(f for f in packet['fields'] if f['kind']=='current')
            if current['ref'].startswith('R'):
                self.assertNotIn('这两位',current['text'])
                self.assertIn('778',current['text'])
                return {'precision':'unspecified','parts':[]},{}
            return {'precision':'unspecified','parts':[
                {'ref':current['ref'],'role':'subject','quote':'这两位'},
                {'ref':current['ref'],'role':'attribute','quote':'778'}]},{}

        self.resolve.side_effect=unresolved
        sent=len(self.providers.lookups)
        tid=await self.submit('这两位 778')
        self.assertEqual(self.resolve.await_count,2)
        self.assertEqual(len(self.providers.lookups),sent)
        self.assertFalse(any(u['phase']=='search_notice' for u in self.out.list(self.pid,self.sid,tid)))
        with self.store.connection() as db:
            proof=lookup_target.saved(db,tid)
            self.assertEqual((proof['status'],proof['unbound_subject']),('invalid',True))
            self.assertIsNone(public_lookup.saved(db,public_lookup.key(tid,'order')))

    async def test_reference_recovery_keeps_requested_attribute_and_uses_public_subject(self):
        from persona_runtime import lookup_target
        from persona_runtime.config import atomic_json
        self.providers.lookup_target_version=lookup_target.QUOTED_TARGET_VERSION
        atomic_json(self.config.paths.home/'private/ordinary-reply/registration.json',
                    {'instance':self.config.load()['instance_id'],'current_public_attribute':True})
        self.consent(True)
        self.grant()
        self.sources[0]['title']='701和702'
        origin=await self.cold()
        self.show(origin,'main')
        self.accept=False
        recovered_subject='701'

        def resolve(aid,packet):
            current=next(f for f in packet['fields'] if f['kind']=='current')
            if current['ref'].startswith('S'):
                ref,quote=current['ref'],'这两位'
            else:
                self.assertNotIn('这两位',current['text'])
                title=next(f for f in packet['fields'] if f['kind']=='title')
                ref,quote=title['ref'],recovered_subject
            return {'parts':[{'ref':ref,'role':'subject','quote':quote},
                             {'ref':current['ref'],'role':'attribute','quote':'778'}]},{}

        self.resolve.side_effect=resolve
        before=len(self.providers.lookups)
        single=await self.submit('这两位 778')
        with self.store.connection() as db:
            self.assertIsNone(public_lookup.saved(db,public_lookup.key(single,'order')))
        self.assertEqual(len(self.providers.lookups),before)
        self.show(single,'main')
        recovered_subject='701和702'
        tid=await self.submit('这两位 778')
        with self.store.connection() as db:
            work=public_lookup.saved(db,public_lookup.key(tid,'order'))
            self.assertEqual(work['authority']['topics'],[origin])
            self.assertEqual(work['query']['question'],'701和702 778')
        self.assertEqual(len(self.providers.lookups),before)
        self.show(tid,'search_notice')
        await self.continue_turn(tid)
        self.show(tid,'main')
        self.assertEqual(self.providers.lookups[before][2]['question'],'701和702 778')

    async def test_persona_world_delivers_old_passage_without_network(self):
        self.enable_world()
        await self.check_local_clock(new_window=True)

    async def test_persona_world_rechecks_cross_window_revision_for_pinned_read(self):
        from persona_runtime import persona_world
        self.enable_world()
        await self.seed_research_contract('701 702')
        self.sid=self.chat.session(fresh=True)['session_id']
        self.sources[0]['date']='2026-04-01'
        await self.seed_research_contract('701 703')
        self.sid=self.chat.session(fresh=True)['session_id']
        self.enable_research_contract(None)
        self.providers.route='none'
        tid=await self.submit('778')
        self.show(tid,'main')
        instance=self.config.load()['instance_id']
        base={'instance':instance,'consent':session_privacy.status(self.store,self.config,self.pid,self.sid)}
        with self.store.connection() as db:
            turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone())
            self.assertEqual(len(persona_world.attestations(db,instance,self.pid)),2)
            latest=semantic_knowledge.materials(db,turn,base,'701')
            self.assertEqual([f['text'] for f in latest],['701 703'])
            historical=semantic_knowledge.materials(db,turn,base,'截至2026年2月1日甲属性乙')
            self.assertEqual([f['text'] for f in historical],['701 702'])
            self.assertEqual(semantic_knowledge.materials(db,turn,base,'701',ids=[historical[0]['id']]),[])

    async def test_persona_world_j4_none_continues_explicit_public_reference_only(self):
        from persona_runtime import research_contract
        self.enable_world()
        text=self.sources[0]['content']
        await self.seed_research_contract(text)
        self.sid=self.chat.session(fresh=True)['session_id']
        seen=[]
        async def assess(aid,state):
            seen.append(aid)
            fact=state['sources'][0]
            return {'outcome':'complete','gap':'none','witnesses':[{'id':fact['id'],'quote':text}]},{}
        self.enable_research_contract(assess)
        self.providers.route='none'
        before=len(self.providers.lookups)
        ordinary=await self.submit('778')
        self.show(ordinary,'main')
        self.assertFalse(seen)
        tid=await self.submit(self.sources[0]['url'])
        self.show(tid,'main')
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT outcome FROM local_knowledge_reads WHERE turn_id=?',(tid,)).fetchone()[0],
                             'continued')
            self.assertIs(research_contract.request(db,tid)['world_reference'],True)
            self.assertFalse(public_lookup.saved(db,public_lookup.key(tid,'order')))
        self.assertEqual(len(seen),1)
        self.assertEqual(len(self.providers.lookups),before)
        self.assertEqual(private_lookup.details(self.store,self.pid,self.sid,tid,'research')['sources'][0]['content'],text)

    async def test_persona_world_uses_public_title_for_a_missing_attribute(self):
        from persona_runtime import persona_world
        self.enable_world()
        await self.seed_research_contract('701 702 703')
        self.sid=self.chat.session(fresh=True)['session_id']

        async def assess(aid,state):
            return {'outcome':'insufficient','gap':'information','witnesses':[]},{}
        self.enable_research_contract(assess)
        def resolve(aid,packet):
            self.assertEqual([f['kind'] for f in packet['fields']],['current','title'])
            current,title=packet['fields']
            return {'precision':'unspecified','parts':[
                {'ref':title['ref'],'role':'subject','quote':title['text']},
                {'ref':current['ref'],'role':'attribute','quote':current['text']}]},{}
        self.resolve.side_effect=resolve
        before=len(self.providers.lookups)
        tid=await self.submit('778')
        self.show(tid,'search_notice')
        with self.store.connection(write=True) as db:
            order=public_lookup.saved(db,public_lookup.key(tid,'order'))
            self.assertEqual(order['query'],{'question':'701 778'})
            self.assertIn('public_title',order['authority']['witnesses'][0])
            persona_world.withdraw_source(db,self.config.load()['instance_id'],self.pid,self.sources[0]['url'])
        # A revocation between the notice and the web call invalidates the frozen subject.
        await self.continue_turn(tid)
        self.assertEqual(len(self.providers.lookups),before)

    async def test_independent_acquisitions_keep_one_copy_of_public_body(self):
        from persona_runtime import local_knowledge
        self.enable_world()
        turns=[]
        for index in range(2):
            if index:
                self.sid=self.chat.session(fresh=True)['session_id']
            self.consent(True)
            tid=await self.cold()
            self.show(tid,'main')
            turns.append(tid)
        with self.store.connection() as db:
            docs=db.execute("""SELECT * FROM local_knowledge_documents
                WHERE receipt IN (?,?) ORDER BY rowid""",turns).fetchall()
            self.assertEqual(len({doc['session_id'] for doc in docs}),2)
            self.assertEqual([doc['body'] for doc in docs],[self.sources[0]['content'],''])
            self.assertEqual(local_knowledge.document_body(db,docs[1]),docs[0]['body'])

    async def check_local_clock(self,*,new_window):
        from persona_runtime import research_contract
        clock='701 778 2026-12-24 08:15'
        await self.seed_research_contract(clock)
        if new_window:
            self.sid=self.chat.session(fresh=True)['session_id']
        async def assess(aid,state):
            src=state['sources'][0]
            return {'outcome':'complete','gap':'none','witnesses':[
                {'id':src['id'],'quote':clock,'clock':'08:15'}]},{}
        self.enable_research_contract(assess)
        calls=len(self.providers.lookups)
        tid=await self.submit('701 778')
        self.show(tid,'main')
        self.assertEqual(len(self.providers.lookups),calls)
        with self.store.connection() as db:
            self.assertEqual(research_contract.coverage(db,tid,'local')['outcome'],'complete')
        summary=self.providers.bodies[-1][1]['research']['assessment']
        self.assertEqual((summary['coverage'],summary['tool']),('complete','not_dispatched'))
        if new_window:
            detail=private_lookup.details(self.store,self.pid,self.sid,tid,'research')
            self.assertEqual([(r['url'],r['content']) for r in detail['sources']],
                             [(self.sources[0]['url'],clock)])

    async def test_partial_readback_stays_partial_and_preserves_local_material(self):
        date='701 778 2026-12-24'
        await self.seed_research_contract(date)
        async def assess(aid,state):
            src=next(f for f in state['sources'] if f['text']==date)
            return {'outcome':'partial','gap':'precision','witnesses':[{'id':src['id'],'quote':date}]},{}
        self.enable_research_contract(assess)
        self.sources=[dict(self.sources[0],url='https://example.org/778',content=date+' 702')]
        tid=await self.submit('701 778')
        self.show(tid,'search_notice')
        await self.continue_turn(tid)
        self.show(tid,'main')
        result=self.providers.bodies[-1][1]['research']
        self.assertEqual((result['assessment']['coverage'],result['assessment']['tool']),('partial','acquired'))
        self.assertEqual(result['facts'][0]['text'],date)
        self.assertEqual(self.trace(tid)['outcome'],'partial_evidence')

    async def test_invalid_assessment_cannot_dispatch_or_claim_coverage(self):
        await self.seed_research_contract('701 778 2026-12-24')
        async def assess(aid,state):
            return {'outcome':'complete','gap':'none','witnesses':[{'id':'999','quote':'2026-12-24'}]},{}
        self.enable_research_contract(assess)
        sent=len(self.providers.lookups)
        tid=await self.submit('701 778')
        self.show(tid,'main')
        actual=self.providers.bodies[-1][1]['research']
        self.assertEqual(actual['lookup_outcome'],'coverage_unconfirmed')
        self.assertEqual(actual['assessment']['coverage'],'unconfirmed')
        self.assertEqual(len(self.providers.lookups),sent)
        self.assertEqual(actual['facts'],[])

    def test_readback_preserves_distinct_quotes_from_the_same_passage(self):
        from persona_runtime import research_contract

        passages=[{'id':'701','text':'702 703'}]
        answer={'outcome':'partial','gap':'information','witnesses':[
            {'id':'701','quote':'702'},{'id':'701','quote':'703'}]}
        result=research_contract.interpret(answer,{'precision':'unspecified'},passages)
        self.assertEqual(result['outcome'],'partial')
        self.assertEqual([witness['span'] for witness in result['witnesses']],[[0,3],[4,7]])
        self.assertEqual(research_contract.selected_facts(result,passages),passages)
        answer['witnesses'][1]['quote']='704'
        self.assertEqual(research_contract.interpret(answer,{'precision':'unspecified'},passages)['outcome'],
                         'unconfirmed')

    async def test_partial_without_public_grant_preserves_readable_evidence(self):
        await self.seed_research_contract('701 778 2026-12-24')
        async def assess(aid,state):
            src=state['sources'][0]
            return {'outcome':'partial','gap':'precision','witnesses':[{'id':src['id'],'quote':src['text']}]},{}
        self.enable_research_contract(assess)
        self.grant(False)
        sent=len(self.providers.lookups)
        tid=await self.submit('701 778')
        self.show(tid,'main')
        actual=self.providers.bodies[-1][1]['research']
        self.assertEqual(actual['assessment']['coverage'],'partial')
        self.assertFalse(actual['assessment']['public_bound'])
        self.assertEqual(len(self.providers.lookups),sent)
        self.assertEqual(len(actual['facts']),1)

    async def test_reply_format_failure_keeps_verified_lookup_material_readable(self):
        from persona_runtime import reply_provenance
        from persona_runtime.workbench_timeline import page
        clock='701 778 2026-12-24 08:15'
        await self.seed_research_contract(clock)
        async def assess(aid,state):
            return {'outcome':'complete','gap':'none','witnesses':[
                {'id':state['sources'][0]['id'],'quote':clock,'clock':'08:15'}]},{}
        self.enable_research_contract(assess)
        self.providers.provenance_version=reply_provenance.VERSION
        repairs=[]
        async def produce(aid,packet):
            repairs.append(packet.get('repair_observations'))
            return '701',{'lookup_attribution':{'error':'format'}}
        self.providers.reply=produce
        tid=await self.submit('701 778')
        self.assertEqual(self.chat.get(tid)['error_code'],'lookup_reply_format_invalid')
        self.assertEqual(repairs[1]['cause'],'lookup_reply_format_invalid')
        self.assertTrue(page(self.store,self.pid,self.sid)['turns'][-1]['research_detail'])
        details=private_lookup.details(self.store,self.pid,self.sid,tid,'research')
        self.assertEqual(details['sources'][0]['content'],clock)
        self.consent(False)
        with self.assertRaises(RuntimeErrorBase):
            private_lookup.details(self.store,self.pid,self.sid,tid,'research')

    def test_frozen_coverage_offer_can_resume_with_exact_revisions(self):
        from persona_runtime import research_contract
        from persona_runtime.archive import fingerprint
        fact={'id':'701','text':'702'}
        need={'precision':'clock_time'}
        prefix=research_contract.PREFIX+'703.local.offer'
        with self.store.connection(write=True) as db:
            public_lookup.freeze(db,prefix,{'request':fingerprint(need),
                                            'candidates':[['701',fingerprint(fact)]]})
            new={'id':'704','text':'705'}
            def read(ids):
                self.assertEqual(ids,['701'])
                return [fact]
            self.assertEqual(research_contract.restore_offer(db,'703',need,'local',[new],read),[fact])
            with self.assertRaises(RuntimeErrorBase):
                research_contract.restore_offer(db,'703',need,'local',[new],lambda _:[])
            with self.assertRaises(RuntimeErrorBase):
                research_contract.restore_offer(db,'703',need|{'precision':'unspecified'},'local',[new],read)

    async def switched_main_with_readback(self):
        from persona_runtime import knowledge_study
        clock='701 778 2026-12-24 08:15'
        self.config.update({'private_research_account':'native_web',
                            'native_web_model':{'name':'deepseek-flash','base_url':'https://api.deepseek.com','api_key':'820'}})
        self.sources[0]['content']=clock
        async def assess(aid,state):
            src=state['sources'][0]
            return {'outcome':'complete','gap':'none','witnesses':[
                {'id':src['id'],'quote':clock,'clock':'08:15'}]},{}
        self.enable_research_contract(assess)
        source=await self.submit('701 778')
        self.show(source,'search_notice')
        finish=knowledge_study.finish
        async def changing_main(*args,**kwargs):
            result=await finish(*args,**kwargs)
            self.config.update({'model':{'api_key':'821'}})
            return result
        with patch.object(knowledge_study,'finish',side_effect=changing_main):
            await self.continue_turn(source)
        self.assertEqual(self.chat.get(source)['error_code'],'private_reply_model_changed')
        self.assertEqual(self.trace(source)['outcome'],'supported')
        self.consent(True)
        self.assertFalse(session_privacy.status(self.store,self.config,self.pid,self.sid)['public_lookup']['enabled'])
        return source,clock

    async def test_persona_withdrawal_applies_to_legacy_retained_readback(self):
        from persona_runtime import lookup_continuation,persona_world
        source,_=await self.switched_main_with_readback()
        instance=self.config.load()['instance_id']
        with self.store.connection(write=True) as db:
            turn=db.execute('SELECT * FROM turns WHERE id=?',(source,)).fetchone()
            self.assertTrue(lookup_continuation.retained_detail(db,self.store,turn))
            persona_world.withdraw_source(db,instance,self.pid,self.sources[0]['url'])
            self.assertIsNone(lookup_continuation.retained_detail(db,self.store,turn))

    async def test_readback_continues_under_new_main_grant_without_another_search(self):
        from persona_runtime import reply_provenance
        from persona_runtime.workbench_timeline import page
        source,clock=await self.switched_main_with_readback()
        calls=len(self.providers.lookups)
        self.providers.route='none'
        self.providers.provenance_version=reply_provenance.VERSION
        choose=self.providers.choose
        async def decide(node,aid,state,options):
            if node=='J2.task':return 'open',{}
            if node==reply_provenance.NODE:
                self.assertEqual(state['resolved_question'],'701 778')
                self.assertFalse(state['fresh_fact_ids'])
                return 'grounded',{}
            return await choose(node,aid,state,options)
        self.providers.choose=decide
        async def write(aid,packet):
            self.providers.bodies.append((aid,packet))
            fact=packet['research']['facts'][0]
            return '08:15',{'lookup_attribution':{'claims':[
                {'field':'body','claim':'08:15','fact':fact['id'],'quote':clock}]}}
        self.providers.reply=write
        tid=await self.submit('799')
        self.assertEqual(self.show(tid,'main'),'08:15')
        read=self.providers.bodies[-1][1]
        self.assertEqual(read['research']['lookup_outcome'],'continued')
        self.assertEqual(read['research']['facts'][0]['text'],clock)
        self.assertNotIn('retained_read',read['research'])
        self.assertEqual(read['provenance']['fresh_fact_ids'],[])
        self.assertEqual(read['research']['assessment']['coverage'],'complete')
        self.assertEqual(read['research']['assessment']['tool'],'not_dispatched')
        another=await self.submit('798')
        self.assertEqual(self.show(another,'main'),'08:15')
        self.assertEqual(self.providers.bodies[-1][1]['research']['facts'],read['research']['facts'])
        self.assertEqual(len(self.providers.lookups),calls)
        cards={c['id']:c for c in page(self.store,self.pid,self.sid)['turns']}
        self.assertTrue(cards[source]['research_detail'])
        self.assertTrue(cards[tid]['retained_lookup'])
        self.assertTrue(cards[another]['retained_lookup'])
        for turn in (source,tid,another):
            self.assertEqual(private_lookup.details(self.store,self.pid,self.sid,turn,'research')['sources'][0]['content'],clock)
        with self.store.connection(write=True) as db:
            db.execute('UPDATE local_knowledge_facts SET withdrawn=1 WHERE id=?',(read['research']['facts'][0]['id'],))
        for turn in (source,tid):
            with self.assertRaises(RuntimeErrorBase):
                private_lookup.details(self.store,self.pid,self.sid,turn,'research')

    async def test_visible_citation_can_anchor_a_followup_search_and_preserve_local_partial(self):
        from persona_runtime import reply_provenance,research_contract
        source,clock=await self.switched_main_with_readback()
        self.providers.route='none'
        self.providers.provenance_version=reply_provenance.VERSION
        choose=self.providers.choose
        original_writer=self.providers.reply
        async def decide(node,aid,state,options):
            if node=='J2.task':return 'open',{}
            if node==reply_provenance.NODE:return 'grounded',{}
            return await choose(node,aid,state,options)
        self.providers.choose=decide
        async def write(aid,packet):
            if packet['phase']=='search_notice':
                return await original_writer(aid,packet)
            self.providers.bodies.append((aid,packet))
            fact=packet['research']['facts'][0]
            return '701',{'lookup_attribution':{'claims':[
                {'field':'body','claim':'701','fact':fact['id'],'quote':fact['text']}]}}
        self.providers.reply=write
        prior=await self.submit('798')
        self.assertEqual(self.show(prior,'main'),'701')
        with self.store.connection() as db:
            usage=json.loads(db.execute("SELECT usage_json FROM private_reply_nodes WHERE turn_id=? AND node='J6.ending'",
                                        (prior,)).fetchone()[0])
            self.assertEqual(usage['origin'],'local_single_choice')

        self.grant()
        self.providers.route='retrieve'
        def resolve(aid,packet):
            now=next(f for f in packet['fields'] if f['kind']=='current')
            old=next(f for f in packet['fields'] if f['kind']=='recent' and f['text']=='701')
            return {'precision':'clock_time','parts':[
                {'ref':old['ref'],'role':'subject','quote':'701'},
                {'ref':now['ref'],'role':'attribute','quote':'779'}]},{}
        self.resolve.side_effect=resolve
        fresh='701 779 2026-12-24 19:20'
        self.sources=[dict(self.sources[0],url='https://example.org/779',content=fresh)]
        async def assess(aid,state):
            src=next((f for f in state['sources'] if f['text']==fresh),state['sources'][0])
            witness={'id':src['id'],'quote':src['text']}
            if src['text']==fresh:
                return {'outcome':'complete','gap':'none','witnesses':[witness|{'clock':'19:20'}]},{}
            self.assertEqual(src['text'],clock)
            return {'outcome':'partial','gap':'precision','witnesses':[witness]},{}
        self.providers.bind_coverage=lambda base,state: (lambda aid:assess(aid,state),{'model':'801'})
        sent=len(self.providers.lookups)
        tid=await self.submit('779')
        with self.store.connection() as db:
            work=public_lookup.saved(db,public_lookup.key(tid,'order'))
            self.assertEqual(work['authority']['topics'],[source])
            self.assertTrue(work['authority']['witnesses'][0]['prior_attribution'])
            self.assertEqual(research_contract.coverage(db,tid,'local')['outcome'],'partial')
        self.assertEqual(len(self.providers.lookups),sent)
        self.show(tid,'search_notice')
        await self.continue_turn(tid)
        self.show(tid,'main')
        self.assertEqual(self.providers.lookups[sent][2]['question'],'701 779')
        self.assertEqual(self.trace(tid)['outcome'],'supported')

    async def test_topic_change_does_not_pull_retained_public_evidence_into_ordinary_chat(self):
        from persona_runtime import reply_provenance
        await self.switched_main_with_readback()
        calls=len(self.providers.lookups)
        self.providers.route='none'
        self.providers.provenance_version=reply_provenance.VERSION
        choose=self.providers.choose
        async def decide(node,aid,state,options):
            if node=='J2.action':return 'change_topic',{}
            if node=='J2.task':return 'open',{}
            return await choose(node,aid,state,options)
        self.providers.choose=decide
        tid=await self.submit('799')
        self.show(tid,'main')
        packet=self.providers.bodies[-1][1]
        self.assertEqual(packet['research']['status'],'ordinary')
        self.assertFalse(packet['research']['facts'])
        self.assertNotIn('provenance',packet)
        self.assertEqual(len(self.providers.lookups),calls)

    async def test_continued_readback_requires_unbroken_private_grant(self):
        source,clock=await self.switched_main_with_readback()
        self.assertEqual(private_lookup.details(self.store,self.pid,self.sid,source,'research')['notes'],clock)
        self.consent(False)
        self.consent(True)
        with self.assertRaises(RuntimeErrorBase):
            private_lookup.details(self.store,self.pid,self.sid,source,'research')

    async def test_continued_readback_stops_on_change_to_other_recipients(self):
        source,clock=await self.switched_main_with_readback()
        self.assertEqual(private_lookup.details(self.store,self.pid,self.sid,source,'research')['notes'],clock)
        self.config.update({'native_web_model':{'api_key':'822'}})
        self.consent(True)
        with self.assertRaises(RuntimeErrorBase):
            private_lookup.details(self.store,self.pid,self.sid,source,'research')

    async def test_citations_are_bound_to_the_published_text_and_survive_reopen(self):
        from persona_runtime import reply_provenance
        self.consent(True)
        self.providers.provenance_version=reply_provenance.VERSION
        choose=self.providers.choose
        checks=[]
        async def verify(node,aid,state,options):
            if node==reply_provenance.NODE:
                checks.append(state)
                return ('grounded' if len(checks)>1 else 'unbound'),{}
            return await choose(node,aid,state,options)
        self.providers.choose=verify
        reply=self.providers.reply
        async def write(aid,packet):
            if 'provenance' not in packet:
                return await reply(aid,packet)
            fact=packet['research']['facts'][0]
            return '703',{'lookup_attribution':{'claims':[
                {'field':'body','claim':'703','fact':fact['id'],'quote':'702 703'}]}}
        self.providers.reply=write
        tid=await self.cold()
        self.assertEqual(self.show(tid,'main'),'703')
        self.assertEqual(len(checks),2)
        self.assertEqual(checks[-1]['fresh_fact_ids'],[checks[-1]['sources'][0]['id']])
        self.assertEqual(len(self.providers.lookups),2)
        self.reopen()
        self.assertEqual(private_lookup.details(self.store,self.pid,self.sid,tid,'citations'),
                         [{'claim':'703','quote':'702 703','title':'701','url':'https://example.org/701'}])
        self.providers.route='none'
        ordinary=await self.submit('90000')
        self.show(ordinary,'main')
        self.assertEqual(len(checks),2)
        self.consent(False)
        with self.assertRaises(RuntimeErrorBase):
            private_lookup.details(self.store,self.pid,self.sid,tid,'citations')

    async def test_scope_recovery_keeps_subject_and_attribute_after_change_of_topic(self):
        from persona_runtime import lookup_target
        self.providers.lookup_target_version=lookup_target.QUOTED_TARGET_VERSION
        self.consent(True)
        self.grant()
        first=await self.cold()
        self.show(first,'main')
        self.providers.route='none'
        self.show(await self.submit('919'),'main')
        self.providers.route='retrieve'
        self.accept=False
        def choose(aid,packet):
            fields=packet['fields']
            current=next(f for f in fields if f['kind']=='current')
            if current['ref'].startswith('R'):
                self.assertFalse(any(f['kind']=='query' and '701' in f['text'] for f in fields))
                return {'parts':[{'ref':current['ref'],'role':'subject','quote':'702'}]},{}
            subject=next(f for f in fields if f['kind']=='recent' and f['text']=='919')
            return {'parts':[{'ref':subject['ref'],'role':'subject','quote':'919'},
                             {'ref':current['ref'],'role':'attribute','quote':'702'}]},{}
        self.resolve.side_effect=choose
        calls=len(self.providers.lookups)
        tid=await self.submit('702')
        self.show(tid,'main')
        with self.store.connection() as db:
            proof=lookup_target.for_query(db,tid)
            self.assertEqual(proof['task']['question'],'919 702')
            self.assertEqual(proof['recovery_result']['status'],'different_query')
        self.assertEqual(len(self.providers.lookups),calls)

    async def test_partial_local_passage_survives_empty_public_lookup(self):
        self.consent(True)
        self.grant()
        first=await self.cold()
        self.show(first,'main')
        self.sources=[]
        choose=self.providers.choose
        async def partial(node,aid,state,options):
            if node=='J5' and 'source' in state:
                return 'source/0',{}
            return await choose(node,aid,state,options)
        self.providers.choose=partial
        self.resolve.return_value=({'parts':[]},{})
        # Exercise a local-first read on an ordinary follow-up, with an authorized public target.
        self.select_topic(first)
        tid=await self.submit('702')
        self.show(tid,'search_notice')
        await self.continue_turn(tid)
        self.show(tid,'main')
        context=self.providers.bodies[-1][1]['research']
        self.assertEqual(context['lookup_outcome'],'local_partial')
        self.assertEqual(context['facts'][0]['text'],'701 702 703')

    def test_after_timestamp_checks_publication_precision(self):
        values=('2025-10-01','2025-10-02','2025年10月3日','2025-10-02T15:00:02+00:00',None,'2026-10-03')
        self.assertEqual([public_lookup_study._within_window(d,'2025-10-02T15:00:01+00:00',
                             '2025-10-04T20:00:00+00:00') for d in values],[False,False,True,True,False,False])

    def test_cited_partial_source_and_independent_complete_source_are_delivered(self):
        rows=[{'id':str(i),'url':'https://example.org/'+str(i)} for i in range(3)]
        citation={'type':'url_citation','url_citation':{'start_index':0,'end_index':1,'url':rows[1]['url']}}
        native={'notes':'701','sources':rows,'annotations':[citation]}
        self.assertEqual(public_lookup_study.cited_selection(native,'701',rows),[rows[1],rows[0],rows[2]])
        self.assertEqual(public_lookup_study.cited_selection(native,'702',rows),rows)
        citation['url_citation']['end_index']=99
        self.assertEqual(public_lookup_study.cited_selection(native,'701',rows),rows)

    def test_scope_witness_replay_and_recipient_change(self):
        self.consent(True)
        s=session_privacy.status(self.store,self.config,self.pid,self.sid)['public_lookup']
        args=(self.store,self.config,self.pid,self.sid,True,s['revision'],s['recipient_hash'],identifier(),s['protocol'])
        first=session_privacy.update(*args)
        self.assertEqual(session_privacy.update(*args),first)
        self.consent(False)
        self.consent(True)
        self.assertFalse(session_privacy.status(self.store,self.config,self.pid,self.sid)['public_lookup']['enabled'])

    async def test_proven_unsent_reuses_exact_tool_payload_after_restart(self):
        from persona_runtime.chat import ModelError
        from persona_runtime.ingress import IngressService
        retrieve=self.providers.retrieve
        searches=[]
        async def once(step,aid,query,model):
            if step=='search':
                searches.append(query)
                if len(searches)==1:
                    raise ModelError('provider_network_error',diagnostics={'request_headers_sent':False})
            return await retrieve(step,aid,query,model)
        self.providers.retrieve=once
        tid=await self.cold()
        self.assertEqual(self.trace(tid)['calls'][-1]['state'],'unsent')
        self.reopen()
        self.chat.recover()
        ingress=IngressService(self.chat)
        ingress.recover()
        ingress.network_wake()
        ingress.schedule_committed()
        await self.chat.wait()
        self.show(tid,'main')
        calls=self.trace(tid)['calls']
        self.assertEqual([c['state'] for c in calls],['received','unsent','received'])
        self.assertEqual(searches,[{'query':'701'}]*2)
        self.assertEqual(calls[-1]['payload_hash'],calls[-2]['payload_hash'])
        self.assertNotEqual(calls[-1]['attempt'],calls[-2]['attempt'])

    async def test_native_effect_unknown_never_starts_fallback(self):
        self.failed_step='native'
        tid=await self.cold()
        self.show(tid,'main')
        self.reopen()
        self.chat.recover()
        self.assertEqual([c['step'] for c in self.trace(tid)['calls']],['native'])
        self.assertEqual(self.trace(tid)['outcome'],'unconfirmed')

    async def test_conflicting_sources_keep_conditions_and_reverification_reuses_document(self):
        self.sources.append(dict(self.sources[0],url='https://example.org/707',content='701 702 711',qualification='712'))
        first=await self.cold()
        self.show(first,'main')
        second=await self.cold()
        self.show(second,'main')
        a,b=(self.trace(t)['revision'] for t in (first,second))
        self.assertEqual(b['previous'],first)
        self.assertEqual(a['documents'],b['documents'])
        self.assertNotEqual(a['facts'],b['facts'])
        detail=private_lookup.details(self.store,self.pid,self.sid,second,'research')
        self.assertEqual({s['qualification'] for s in detail['sources']},{'710','712'})
        self.assertEqual({s['content'] for s in detail['sources']},{'701 702 703','701 702 711'})
        from persona_runtime import local_knowledge
        with self.store.connection(write=True) as db:
            local_knowledge.withdraw_document(db,next(s['id'] for s in detail['sources'] if s['qualification']=='710'))
        self.assertEqual([s['qualification'] for s in private_lookup.details(self.store,self.pid,self.sid,second,'research')['sources']],['712'])

    async def test_rejection_of_one_passage_quarantines_the_rest_of_the_page(self):
        self.providers.consume=False
        self.sources[0]['content']='7'*432+'8'*48+'9'*400
        choose=self.providers.choose
        async def selected(node,aid,state,options):
            if node=='J5' and '9' in state.get('source',{}).get('text',''):
                return 'insufficient',{}
            return await choose(node,aid,state,options)
        self.providers.choose=selected
        tid=await self.cold()
        self.show(tid,'main')
        with self.store.connection() as db:
            r=db.execute('select spec_json from local_knowledge_facts').fetchall()
            self.assertEqual(len(r),1)
            self.assertEqual(json.loads(r[0][0])['end'],480)

    async def test_confirmed_query_with_no_tool_results_settles_explicitly(self):
        self.sources=[]
        tid=await self.cold()
        self.show(tid,'main')
        research=self.providers.bodies[-1][1]['research']
        self.assertEqual((research['status'],research['public_result']['outcome']),('insufficient','no_relevant_evidence'))
        self.assertEqual(self.trace(tid)['calls'][-1]['results'],0)

    async def test_partial_source_keeps_its_scope_in_the_main_packet(self):
        choose=self.providers.choose
        async def partial(node,aid,state,options):
            if node=='J5' and 'source' in state:
                return 'source/0',{}
            return await choose(node,aid,state,options)
        self.providers.choose=partial
        tid=await self.cold()
        self.show(tid,'main')
        self.assertEqual(self.trace(tid)['outcome'],'partial_evidence')
        research=self.providers.bodies[-1][1]['research']
        self.assertEqual(research['public_result']['outcome'],'partial_evidence')
        self.assertEqual(research['facts'][0]['qualification'],'710')

    async def test_workbench_card_exposes_counts_and_visible_receipt(self):
        from persona_runtime.ingress import IngressService
        from persona_runtime.workbench_timeline import page
        from scripts.pcar_desktop_turn_evidence import collect
        ingress=IngressService(self.chat)
        rid=identifier()
        payload={'persona_id':self.pid,'session_id':self.sid,'channel':'desktop','text':'701 754',
                 'public_task':{'question':'701','request_search':True},'request_id':rid}
        receipt=ingress.accept(payload)
        self.assertEqual(ingress.accept(payload)['batch_id'],receipt['batch_id'])
        with self.store.connection() as db:
            batch=db.execute('SELECT revision,state FROM input_batches WHERE id=?',(receipt['batch_id'],)).fetchone()
        if batch['state']=='collecting':
            ingress.action(receipt['batch_id'],'seal',batch['revision'],identifier(),self.pid,self.sid)
        await self.chat.wait()
        tid=ingress.lookup('desktop',rid)['turn_id']
        self.show(tid,'search_notice')
        await self.continue_turn(tid)
        self.show(tid,'main')
        card=next(t for t in page(self.store,self.pid,self.sid)['turns'] if t['id']==tid)
        self.assertTrue(card['research_detail'])
        self.assertEqual(card['public_lookup'],{'outcome':'supported','calls':[
            {'step':'native','state':'received','results':0},{'step':'search','state':'received','results':1}]})
        trace=collect(self.store.paths.home,rid)['public_lookup']
        self.assertEqual(trace['delivery'],self.trace(tid)['delivery'])
        self.assertTrue(trace['j4'])
        self.assertNotIn('https://example.org',json.dumps(trace))

    async def test_timed_public_lookup_reopens_with_the_same_sources_and_delivery(self):
        from persona_runtime import message_time
        from persona_runtime.ingress import IngressService
        from persona_runtime.recall_runtime import render
        from persona_runtime.workbench_timeline import page

        ingress=IngressService(self.chat)
        policy=ingress.current_policy(self.pid,'desktop')
        ingress.update_policy(self.pid,'desktop',policy['mode'],policy['id'],identifier(),
                              message_time_version=message_time.VERSION)
        self.consent(True)
        self.grant()
        scoped=[]
        async def source(node,aid,state,options):
            self.assertEqual((node,set(state),options),
                             ('J5',{'question','source'},('complete','source/0','insufficient')))
            scoped.append(aid)
            return 'complete',{}
        self.providers.bind_public_source=lambda:source
        rid=identifier()
        first=ingress.accept({'persona_id':self.pid,'session_id':self.sid,'channel':'desktop','request_id':rid,
                              'text':'701 709','lookup_span':'701'})
        with self.store.connection() as db:
            batch=db.execute('SELECT revision,state FROM input_batches WHERE id=?',(first['batch_id'],)).fetchone()
        if batch['state']=='collecting':
            ingress.action(first['batch_id'],'seal',batch['revision'],identifier(),self.pid,self.sid)
        await self.chat.wait()
        tid=ingress.lookup('desktop',rid)['turn_id']
        self.show(tid,'search_notice')
        await self.continue_turn(tid)
        self.show(tid,'main')
        before=self.trace(tid)
        self.reopen()
        card=next(t for t in page(self.store,self.pid,self.sid)['turns'] if t['id']==tid)
        self.assertEqual(card['input_members'][0]['timestamp']['source'],'received')
        self.assertTrue(scoped)
        ids=[card['search_notice']['event_id'],card['reply_event_id']]
        with self.store.connection() as db:
            times=message_time.event_times(db,ids)
            for eid in ids:
                body=db.execute('SELECT text FROM events WHERE id=?',(eid,)).fetchone()[0]
                raw=render([{'kind':'dialogue','source_type':'assistant_statement','source_id':eid,'text':body}],
                           db,delivery_times=True)
                self.assertEqual(json.loads(raw.splitlines()[-1])['message_times'],message_time.wire_times(times[eid]))
            self.assertEqual([r['receipt_id'] for r in times[ids[1]]],
                             [r['receipt_id'] for r in before['delivery']['fragments']])
            self.assertEqual(times[ids[0]][0]['recorded_at'],card['search_notice']['timestamp']['recorded_at'])
            self.assertEqual(before['revision']['facts'],before['delivery']['facts'])
        count=len(self.providers.lookups)
        self.select_topic(tid)
        follow=await self.submit('702')
        self.assertIn('703',self.show(follow,'main'))
        self.assertEqual(len(self.providers.lookups),count)
        self.assertEqual(self.trace(tid),before)

    async def test_ambiguous_two_public_topics_and_local_only_request_do_not_dispatch(self):
        self.consent(True)
        self.grant()
        first=await self.cold()
        self.show(first,'main')
        self.sources=[dict(self.sources[0],title='706',url='https://example.org/706',content='706 702 712')]
        second=await self.cold('706 709','706')
        self.show(second,'main')
        count=len(self.providers.lookups)
        def ambiguous(aid,packet):
            refs={f['ref'] for f in packet['fields']}
            self.assertTrue({f'r/{first}/query',f'r/{second}/query'}<=refs)
            return {'parts':[]},{}
        self.resolve.side_effect=ambiguous
        tid=await self.submit('702')
        self.assertEqual(self.trace(tid)['reason'],'ambiguous')
        self.assertEqual(self.trace(tid)['calls'],[])
        with self.store.connection() as db:
            binding=private_lookup.intent(db,tid)['binding']
        private_lookup.confirm(self.store,self.pid,self.sid,tid,binding,identifier(),None)
        await self.continue_turn(tid)
        self.show(tid,'main')
        self.accept=False
        local=await self.submit('只查本地资料，未知属性')
        self.show(local,'main')
        self.resolve.assert_awaited_once()
        self.assertEqual(self.trace(local)['calls'],[])
        self.assertEqual(len(self.providers.lookups),count)

    async def test_unknown_compact_reference_requires_explicit_query_or_skip(self):
        from persona_runtime import lookup_target
        from persona_runtime.workbench_timeline import page

        self.providers.lookup_target_version=lookup_target.SHORT_REFS_VERSION
        self.consent(True)
        self.grant()
        self.resolve.return_value=({'parts':[{'ref':'S999','start':0,'end':2}]},{})
        tid=await self.submit('702')
        card=next(t for t in page(self.store,self.pid,self.sid)['turns'] if t['id']==tid)
        self.assertEqual(card['lookup_target_status'],'invalid')
        self.assertNotIn('lookup_proposal',card)
        self.assertFalse(self.providers.lookups)
        op=identifier()
        for _ in range(2):
            private_lookup.confirm(self.store,self.pid,self.sid,tid,card['lookup_confirmation'],op,'701')
        await self.continue_turn(tid)
        self.show(tid,'search_notice')
        self.assertFalse(self.providers.lookups)
        await self.continue_turn(tid)
        self.show(tid,'main')
        self.assertEqual([q.get('question') or q.get('query') for _,_,q in self.providers.lookups],['701','701'])
        self.resolve.assert_awaited_once()

    async def test_recent_context_recovers_in_background_and_preserves_the_public_scope(self):
        from persona_runtime import lookup_target

        self.providers.lookup_target_version=lookup_target.RECENT_CONTEXT_VERSION
        self.providers.lookup_target_budget=lambda:20000
        self.consent(True)
        self.grant()
        first=await self.cold()
        self.show(first,'main')
        self.providers.route='none'
        for i in range(26):
            self.show(await self.submit(str(9000+i)),'main')
        self.providers.route='retrieve'
        attempts=[]
        def choose(aid,packet):
            attempts.append(aid)
            fields=packet['fields']
            history={f['text'] for f in fields if f['kind']=='recent'}
            self.assertIn('9001',history)
            self.assertIn('9025',history)
            self.assertNotIn('9000',history)
            if len(attempts)==1:
                return {'parts':[{'ref':'S999','start':0,'end':3}]},{}
            self.assertFalse(any(f['kind'] in ('title','correction') for f in fields))
            origin=next(f for f in fields if f['kind']=='query' and f['text']=='701')
            prop=next(f for f in fields if f['kind']=='current')
            self.assertTrue(origin['ref'].startswith('R'))
            return {'parts':[{'ref':origin['ref'],'start':0,'end':3},
                             {'ref':prop['ref'],'start':0,'end':3}]},{}
        self.resolve.side_effect=choose
        self.accept=False
        tid=await self.submit('702')
        self.show(tid,'search_notice')
        self.reopen()
        await self.continue_turn(tid)
        self.show(tid,'main')
        self.assertEqual(len(attempts),2)
        with self.store.connection() as db:
            proof=lookup_target.saved(db,tid)
            self.assertEqual([proof['previous_attempt_id'],proof['attempt_id']],attempts)
            self.assertTrue(proof['recovery'])
        self.assertEqual(self.providers.lookups[-1][2]['query'],'701 702')

    async def test_recent_context_exhaustion_completes_without_a_manual_lookup(self):
        from persona_runtime import lookup_target
        from persona_runtime.workbench_timeline import page

        self.providers.lookup_target_version=lookup_target.RECENT_CONTEXT_VERSION
        self.consent(True)
        self.grant()
        self.resolve.return_value=({'parts':[{'ref':'S999','start':0,'end':2}]},{})
        tid=await self.submit('702')
        self.show(tid,'main')
        card=next(t for t in page(self.store,self.pid,self.sid)['turns'] if t['id']==tid)
        self.assertNotIn('lookup_confirmation',card)
        self.assertFalse(self.providers.lookups)
        self.assertEqual(self.resolve.await_count,2)
        research=self.providers.bodies[-1][1]['research']
        self.assertEqual((research['lookup_outcome'],research['facts']),('target_unresolved',[]))
        self.assertEqual((self.trace(tid)['outcome'],self.trace(tid)['reason']),('target_unresolved','invalid'))
        self.providers.lookup_target_budget=lambda:1
        over_budget=await self.submit('703')
        self.show(over_budget,'main')
        self.assertEqual(self.trace(over_budget)['reason'],'input_budget')
        self.assertEqual(self.resolve.await_count,2)
        self.assertFalse(self.providers.lookups)

    async def test_plain_desktop_send_starts_and_extends_a_public_topic_when_the_model_decides_to_retrieve(self):
        from persona_runtime.ingress import IngressService
        from persona_runtime import local_knowledge, lookup_target
        from persona_runtime.config import atomic_json

        self.providers.lookup_target_version=lookup_target.QUOTED_TARGET_VERSION
        self.consent(True)
        self.grant()
        marker=self.config.paths.home/'private/ordinary-reply/registration.json'
        atomic_json(marker,{'instance':self.config.load()['instance_id'],'current_public_attribute':True})
        self.assertFalse(session_privacy.status(self.store,self.config,self.pid,self.sid)['public_lookup']['enabled'])
        self.assertEqual(self.grant()['protocol'],session_privacy.PUBLIC_QUESTION_SCOPE)
        self.providers.route='none'
        ordinary=await self.submit('790')
        self.show(ordinary,'main')
        self.resolve.assert_not_awaited()
        self.assertFalse(self.providers.lookups)

        self.providers.route='retrieve'
        vetoed=await self.submit('只查本地资料，未知属性')
        self.show(vetoed,'main')
        self.assertEqual(self.trace(vetoed)['decision'],'retrieve')
        self.assertFalse(self.trace(vetoed)['ready'])
        self.resolve.assert_not_awaited()
        self.assertFalse(self.providers.lookups)

        def first_selection(aid,packet):
            current=next(f for f in packet['fields'] if f['kind']=='current')
            return {'parts':[{'ref':current['ref'],'role':'subject','quote':'701'},
                             {'ref':current['ref'],'role':'attribute','quote':'702'}]},{}

        self.resolve.side_effect=first_selection
        self.providers.route='retrieve'
        ingress=IngressService(self.chat)
        rid=identifier()
        receipt=ingress.accept({'persona_id':self.pid,'session_id':self.sid,'channel':'desktop',
                                'request_id':rid,'text':'701 702 709'})
        with self.store.connection() as db:
            batch=db.execute('SELECT revision,state FROM input_batches WHERE id=?',(receipt['batch_id'],)).fetchone()
        if batch['state']=='collecting':
            ingress.action(receipt['batch_id'],'seal',batch['revision'],identifier(),self.pid,self.sid)
        await self.chat.wait()
        first=ingress.lookup('desktop',rid)['turn_id']
        self.assertFalse(self.providers.lookups)
        self.assertEqual(self.trace(first)['intent'],'conversation')
        self.assertEqual(self.trace(first)['decision'],'retrieve')
        with self.store.connection() as db:
            order=public_lookup.saved(db,public_lookup.key(first,'order'))
            self.assertEqual(order['query']['question'],'701 702')
            self.assertEqual(order['authority']['kind'],'current_query')
        self.show(first,'search_notice')
        await self.continue_turn(first)
        self.show(first,'main')
        earlier_calls=len(self.providers.lookups)
        with patch.object(local_knowledge,'_REFRESH') as refresh_hint:
            refresh_hint.search.return_value=True
            cached=await self.submit('701 702 709')
        self.show(cached,'main')
        self.assertEqual(self.providers.bodies[-1][1]['research']['status'],'local_hit')
        self.assertEqual(len(self.providers.lookups),earlier_calls)
        self.sources.append(dict(self.sources[0],url='https://example.org/777',title='777',content='701 777 778'))
        choose=self.providers.choose
        async def coverage(node,aid,state,options):
            if node=='J5' and 'source' in state and state['question']=='701 777':
                if len(self.providers.lookups)==earlier_calls:
                    return 'insufficient',{}
                return ('complete' if state['source']['title']=='777' else 'source/0'),{}
            return await choose(node,aid,state,options)
        self.providers.choose=coverage
        def select(aid,packet):
            prev=next(f for f in packet['fields'] if f['kind']=='query')
            current=next(f for f in packet['fields'] if f['kind']=='current')
            return {'parts':[{'ref':prev['ref'],'role':'subject','quote':'701'},
                             {'ref':current['ref'],'role':'attribute','quote':'777'}]},{}
        self.resolve.side_effect=select
        tid=await self.submit('777')
        self.show(tid,'search_notice')
        self.reopen()
        await self.continue_turn(tid)
        self.show(tid,'main')
        with self.store.connection() as db:
            order=public_lookup.saved(db,public_lookup.key(tid,'order'))
            self.assertEqual(order['authority']['topics'],[first])
            self.assertEqual(order['query']['question'],'701 777')
            previous=self.trace(first)['revision']
            updated=self.trace(tid)['revision']
            self.assertEqual(updated['previous'],first)
            self.assertEqual(updated['topic'],previous['topic'])
            self.assertIn(previous['documents'][0]['id'],[d['id'] for d in updated['documents']])
            self.assertEqual(db.execute('SELECT count(*) FROM local_knowledge_documents WHERE session_id=?',
                                        (self.sid,)).fetchone()[0],2)
        self.assertEqual(self.resolve.await_count,3)

    async def test_existing_context_grant_does_not_authorize_a_new_public_question(self):
        from persona_runtime import lookup_target
        from persona_runtime.config import atomic_json

        self.providers.lookup_target_version=lookup_target.QUOTED_TARGET_VERSION
        self.consent(True)
        cfg=self.config.load()
        marker=self.config.paths.home/'private/ordinary-reply/registration.json'
        atomic_json(marker,{'instance':cfg['instance_id'],'current_public_attribute':True,
                            'public_lookup_version':public_lookup.VERSION})
        state=session_privacy.status(self.store,self.config,self.pid,self.sid)
        with self.store.connection() as db:
            previous=session_privacy.public_scope(db,self.config,cfg['instance_id'],self.pid,self.sid,state,
                                                  protocol=session_privacy.PUBLIC_ATTRIBUTE_SCOPE)
        session_privacy.update(self.store,self.config,self.pid,self.sid,True,previous['revision'],
                               previous['recipient_hash'],identifier(),previous['protocol'])
        state=session_privacy.status(self.store,self.config,self.pid,self.sid)['public_lookup']
        self.assertTrue(state['enabled'])
        self.assertEqual(state['protocol'],session_privacy.PUBLIC_ATTRIBUTE_SCOPE)
        self.assertFalse(state['upgrade']['enabled'])
        self.assertEqual(state['upgrade']['protocol'],session_privacy.PUBLIC_QUESTION_SCOPE)

        def select(aid,packet):
            current=next(f for f in packet['fields'] if f['kind']=='current')
            return {'parts':[{'ref':current['ref'],'role':'subject','quote':'701'}]},{}
        self.resolve.side_effect=select
        tid=await self.submit('701')
        self.show(tid,'main')
        self.assertEqual(self.trace(tid)['reason'],'no_public_scope')
        self.assertFalse(self.providers.lookups)

    async def test_local_only_scope_applies_to_the_whole_desktop_batch(self):
        from persona_runtime import lookup_target
        from persona_runtime.config import atomic_json
        from persona_runtime.ingress import IngressService

        self.providers.lookup_target_version=lookup_target.QUOTED_TARGET_VERSION
        self.consent(True)
        atomic_json(self.config.paths.home/'private/ordinary-reply/registration.json',
                    {'instance':self.config.load()['instance_id'],'current_public_attribute':True})
        self.grant()
        choose=self.providers.choose
        async def choose_last(node,aid,state,options):
            if node=='J1':
                return state['current'][-1]['id'],{}
            return await choose(node,aid,state,options)
        self.providers.choose=choose_last
        ingress=IngressService(self.chat)
        policy=ingress.current_policy(self.pid,'desktop')
        ingress.update_policy(self.pid,'desktop','natural_batch',policy['id'],identifier())
        rid=identifier()
        payload={'persona_id':self.pid,'session_id':self.sid,'channel':'desktop'}
        first=ingress.accept(dict(payload,request_id=identifier(),text='只查本地资料，未知属性'))
        last=ingress.accept(dict(payload,request_id=rid,text='701'))
        self.assertEqual(first['batch_id'],last['batch_id'])
        with self.store.connection() as db:
            batch=db.execute('SELECT revision FROM input_batches WHERE id=?',(first['batch_id'],)).fetchone()
        ingress.action(first['batch_id'],'seal',batch['revision'],identifier(),self.pid,self.sid)
        await self.chat.wait()
        tid=ingress.lookup('desktop',rid)['turn_id']
        self.show(tid,'main')
        self.assertEqual(self.trace(tid)['decision'],'retrieve')
        self.assertFalse(self.trace(tid)['ready'])
        self.resolve.assert_not_awaited()
        self.assertFalse(self.providers.lookups)

    async def test_plain_web_link_respects_the_automatic_public_lookup_grant(self):
        from persona_runtime import lookup_target
        from persona_runtime.config import atomic_json

        self.providers.lookup_target_version=lookup_target.QUOTED_TARGET_VERSION
        self.consent(True)
        atomic_json(self.config.paths.home/'private/ordinary-reply/registration.json',
                    {'instance':self.config.load()['instance_id'],'current_public_attribute':True})
        url=self.sources[0]['url']
        blocked=await self.submit(url)
        self.show(blocked,'main')
        self.assertEqual(self.trace(blocked)['reason'],'no_public_scope')
        self.assertFalse(self.providers.lookups)

        self.grant()
        allowed=await self.submit(url)
        self.show(allowed,'search_notice')
        self.assertFalse(self.providers.lookups)
        await self.continue_turn(allowed)
        self.show(allowed,'main')
        self.assertEqual([call[0] for call in self.providers.lookups],['native','fetch'])
        self.assertEqual(self.providers.lookups[-1][2],{'url':url})
        self.resolve.assert_not_awaited()

    async def test_reviewed_notice_receives_bounded_current_context_before_dispatch(self):
        from persona_runtime.profile import snapshot
        from persona_runtime.search_notice_context import SITUATION, valid_packet
        from persona_runtime.archive import fingerprint

        self.providers.phase_scoped_search=True
        with self.store.connection() as db:
            profile=snapshot(db,self.pid)
        self.providers.notice_context_profiles={self.pid:{
            'core_revision':profile['core']['id'],'expression_revision':profile['expression']['id'],
            'trait_indices':[0],'style_range':[0,1]}}
        self.consent(True)
        tid=await self.send_span('701 709','701')
        packet=self.providers.bodies[0][1]
        self.assertTrue(valid_packet(packet))
        self.assertEqual(packet['context'],{'current':[{'role':'user','text':'701 709'}],
                                           'reference':None,'public_query':'701'})
        self.assertEqual(packet['situation'],SITUATION)
        self.assertEqual(packet['persona']['core_traits'],profile['core']['data']['core_traits'][:1])
        with self.store.connection() as db:
            saved=db.execute("""SELECT d.contract_json FROM private_reply_attempts a
                JOIN private_reply_decisions d ON d.id=a.decision_id
                WHERE a.turn_id=? AND a.phase='search_notice'""",(tid,)).fetchone()
            proof=json.loads(saved[0])['ordinary']['notice_context']
            self.assertEqual(proof['packet_hash'],fingerprint(packet))
        await self.continue_turn(tid)
        self.assertEqual(len(self.providers.bodies),1)
        self.assertFalse(self.providers.lookups)
        self.show(tid,'search_notice')
        self.reopen()
        await self.continue_turn(tid)
        self.show(tid,'main')
        self.assertTrue(self.providers.lookups)
        self.assertEqual(len(self.providers.bodies),2)
        self.assertTrue(self.providers.bodies[-1][1]['search_notice_presented'])
        self.assertNotIn('situation',self.providers.bodies[-1][1])
