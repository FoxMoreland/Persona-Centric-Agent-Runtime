"""Desktop intake and evidence handoff using local numeric transport doubles."""
import unittest
from unittest.mock import AsyncMock, patch

from persona_runtime import persona_world, private_lookup, semantic_knowledge, single_retrieval as retrieval
from persona_runtime.config import atomic_json
from tests import test_public_lookup as fixture


class SingleRetrieval(unittest.IsolatedAsyncioTestCase):
    reopen=fixture.PublicLookup.reopen
    submit=fixture.PublicLookup.submit
    consent=fixture.PublicLookup.consent
    show=fixture.PublicLookup.show
    paint=fixture.PublicLookup.paint
    continue_turn=fixture.PublicLookup.continue_turn
    grant=fixture.PublicLookup.grant

    def setUp(self):
        fixture.PublicLookup.setUp(self)
        self.providers.target_resolution=False
        self.providers.public_lookup_version=None
        self.providers.retrieval_spec={'recipient':{'max_input_chars_at_review':24000},
                                       'phases':{phase:{'system':''} for phase in ('translate','assess')}}
        self.requests=[]
        self.queries=['701','702']
        self.answers={}
        async def decide(aid,state,phase):
            self.requests.append((phase,state))
            if phase=='translate':
                return {'status':'ready','queries':[{'text':q,'refs':[state['current'][0]['ref']]} for q in self.queries]},{}
            items=[]
            for q in state['queries']:
                ids=q['candidates']
                items.append({'id':q['id'],'result':'complete' if ids else 'missing','passages':ids[:1]})
            return {'status':'assessed','items':items},{}
        self.providers.bind_retrieval=lambda base,state,phase:(lambda aid:decide(aid,state,phase),{})
        self.tools=[]
        async def retrieve(step,aid,query,model):
            text=query.get('question') or query['query']
            self.tools.append((step,text))
            if self.failed_step==step:
                raise TimeoutError()
            sources=[]
            if step=='search' and text in self.answers:
                sources=[{'url':'https://example.org/'+text,'title':text,'kind':'search_content',
                          'content':self.answers[text]}]
            return {'sources':sources,'tool_calls':len(sources)},{}
        self.providers.retrieve=retrieve
        rank=patch.object(semantic_knowledge,'rank',side_effect=lambda paths,q,rows,**kw:
                    [semantic_knowledge.public_material(row) for row in rows if row['title']==q])
        rank.start()
        self.addCleanup(rank.stop)
        instance=self.config.load()['instance_id']
        atomic_json(self.config.paths.home/'private/ordinary-reply/registration.json',
                    {'instance':instance,'single_retrieval':retrieval.VERSION})
        self.consent(True)
        self.grant()
        with self.store.connection(write=True) as db:
            persona_world.set_enabled(db,instance,self.pid,True)

    async def cold(self):
        tid=await self.submit('701 702')
        self.show(tid,'search_notice')
        self.reopen()
        await self.continue_turn(tid)
        self.show(tid,'main')
        return tid

    async def test_two_queries_and_cross_session_hit(self):
        self.answers={'701':'701 711','702':'702 712'}
        tid=await self.cold()
        self.assertEqual(self.tools,[('native','701'),('search','701'),('native','702'),('search','702')])
        self.assertEqual([k for k,_ in self.requests],['translate','assess'])
        with self.store.connection() as db:
            rows=db.execute('SELECT item_id,step FROM retrieval_item_calls WHERE turn_id=? ORDER BY rowid',(tid,)).fetchall()
            self.assertEqual([tuple(r) for r in rows],[(1,'native'),(1,'search'),(2,'native'),(2,'search')])
            self.assertEqual(len(persona_world.inspect_turn(db,self.config.load()['instance_id'],self.pid,tid)),2)
        detail=private_lookup.details(self.store,self.pid,self.sid,tid,'research')
        self.assertEqual({s['title'] for s in detail['sources']},{'701','702'})
        self.sid=self.chat.session(fresh=True)['session_id']
        self.consent(True)
        self.requests.clear()
        other=await self.submit('701 702')
        self.show(other,'main')
        self.assertEqual([k for k,_ in self.requests],['translate','assess'])
        self.assertEqual(len(self.tools),4)
        self.assertFalse(self.requests[0][1]['history'])
        rows=self.providers.bodies[-1][1]['research']['retrieval']['items']
        self.assertTrue(all(i['tool']=='not_dispatched' and i['result']=='complete' for i in rows))

    def test_citation_must_belong_to_received_public_sources(self):
        one='https://example.org/701'
        def cite(url):
            return {'type':'url_citation','url_citation':{'url':url}}
        receipt={'tool_calls':1,'annotations':[cite('http://localhost/'),cite(one+'#702'),
                 cite('https://example.org/unknown'),cite(one),{'type':'other'},None]}
        self.assertEqual(retrieval.cited_urls(receipt,{one}),[one])
        self.assertFalse(retrieval.cited_urls(receipt|{'tool_calls':0},{one}))

    async def test_fetch_and_assessment_use_cited_passages_of_each_item(self):
        fetched=[]
        async def tools(step,aid,query,model):
            if step=='fetch':
                fetched.append(query['url'])
                return {'sources':[]},{}
            self.assertEqual(step,'native')
            question=query['question']
            sources=[{'url':'https://example.org/'+question+'/'+n,'title':question,
                      'summary':question+' '+n,'kind':'native_search_summary'} for n in ('711','712')]
            return {'sources':sources,'tool_calls':1,'annotations':[
                {'type':'url_citation','url_citation':{'url':sources[1]['url']}}]},{}
        self.providers.retrieve=tools
        def rank(paths,question,rows,**options):
            return [semantic_knowledge.public_material(row) for row in rows
                    if row['title']==question and (options.get('one_per_document') or row['url'].endswith('/711'))]
        with patch.object(semantic_knowledge,'rank',side_effect=rank):
            await self.cold()
            self.show(await self.submit('701 702'),'main')
        self.assertEqual(fetched,['https://example.org/701/712','https://example.org/702/712'])
        for _,packet in self.providers.bodies[-2:]:
            research=packet['research']
            for item in research['retrieval']['items']:
                chosen=[f['url'] for f in research['facts'] if f['id'] in item['facts']]
                self.assertEqual(chosen,['https://example.org/'+item['question']+'/712'])
        self.assertTrue(all(i['tool']=='not_dispatched' for i in research['retrieval']['items']))

    def test_assessment_recognizes_missing_and_rejects_other_items_sources(self):
        packet={'queries':[{'id':1,'candidates':[1]},{'id':2,'candidates':[2]}]}
        missing={'status':'missing','items':[{'id':1,'result':'missing','passages':[]}]}
        docs={1:{'id':'701'},2:{'id':'702'}}
        self.assertEqual([item['result'] for item in retrieval.assessment(missing,packet,docs)['items']],
                         ['missing','unconfirmed'])
        crossed={'status':'assessed','items':[{'id':1,'result':'complete','passages':[2]},
                                             {'id':2,'result':'complete','passages':[2]}]}
        items=retrieval.assessment(crossed,packet,docs)['items']
        self.assertEqual([item['result'] for item in items],['unconfirmed','complete'])
        self.assertEqual(items[1]['facts'][0][0],'702')

    async def test_readback_offers_only_the_items_fresh_sources_when_historical_rank_misses(self):
        self.answers={'701':'701 711','702':'702 712'}
        calls=[]
        def below_cutoff(paths,query,rows,*,minimum_score=None,one_per_document=False):
            calls.append((query,minimum_score,[row['title'] for row in rows]))
            if minimum_score is None:
                return []
            self.assertTrue(one_per_document)
            return [semantic_knowledge.public_material(row) for row in rows]
        with patch.object(semantic_knowledge,'rank',side_effect=below_cutoff):
            tid=await self.cold()
        fallbacks=[(query,titles) for query,threshold,titles in calls if threshold==-1.0]
        self.assertEqual(fallbacks,[('701',['701']),('702',['702'])])
        with self.store.connection() as db:
            fetched=retrieval.saved(db,tid,'readback')
            self.assertEqual([item['result'] for item in fetched['items']],['complete','complete'])
            self.assertEqual([len(item['facts']) for item in fetched['items']],[1,1])
        self.assertEqual([name for name,_ in self.requests],['translate','assess'])

    async def test_only_missing_item_is_dispatched(self):
        self.queries=['701']
        self.answers={'701':'701 711','702':'702 712'}
        await self.cold()
        self.queries.append('702')
        self.requests.clear()
        self.tools.clear()
        await self.cold()
        self.assertEqual(self.tools,[('native','702'),('search','702')])
        self.assertEqual([k for k,_ in self.requests],['translate','assess','assess'])
        self.assertTrue(self.requests[0][1]['history'])
        self.assertEqual([x['tool'] for x in self.providers.bodies[-1][1]['research']['retrieval']['items']],
                         ['not_dispatched','acquired'])

    async def test_pending_notice_reports_wait_and_resumes_once(self):
        from persona_runtime.workbench_timeline import turn_card

        tid = await self.submit('701 702')
        def phase():
            with self.store.connection() as db:
                turn = db.execute('SELECT rowid position,* FROM turns WHERE id=?', (tid,)).fetchone()
                return turn_card(db, turn, self.store)['phase']

        self.assertEqual(phase(), 'knowledge_search_notice')
        self.reopen()
        await self.continue_turn(tid)
        self.assertEqual(phase(), 'knowledge_search_notice')
        self.assertFalse(self.tools)
        self.show(tid, 'search_notice')
        await self.continue_turn(tid)
        self.assertNotEqual(phase(), 'knowledge_search_notice')
        self.assertEqual(self.tools, [('native','701'),('search','701'),('native','702'),('search','702')])

    async def test_background_search_finishes_before_window_returns(self):
        from persona_runtime.workbench_timeline import turn_card
        self.providers.background_lookup = True
        self.answers = {'701':'701 711','702':'702 712'}
        tid = await self.submit('701 702')
        expected = [('native','701'),('search','701'),('native','702'),('search','702')]
        self.assertEqual(self.tools, expected)
        with self.store.connection() as db:
            row = db.execute('SELECT rowid position,* FROM turns WHERE id=?',(tid,)).fetchone()
            card = turn_card(db,row,self.store)
            notice, main = card['desktop_publications']
            self.assertEqual((notice['phase'],main['phase']), ('search_notice','main'))
            self.assertTrue(notice['background_lookup'])
            self.assertTrue(private_lookup.notice(db,tid)['body'])
            self.assertFalse(notice['visible'])
            self.assertFalse(private_lookup.presented_notice(db,tid))
        offered=self.out.offer(self.pid,self.sid,main['utterance_id'],main['publication_id'],main['revision'],0,'desktop')
        self.assertFalse(offered['new_offer'])
        self.reopen()
        await self.continue_turn(tid)
        self.show(tid,'search_notice')
        self.show(tid,'main')
        self.assertEqual(self.chat.get(tid)['status'],'completed')
        self.assertEqual(self.tools, expected)

    async def test_restart_after_saved_notice_continues_without_display(self):
        self.providers.background_lookup = True
        flow = self.chat.private_reply.ordinary
        run = flow._run

        async def stop_after_saved_notice(tid):
            self.assertTrue(await run(tid))
            return False

        with patch.object(flow,'_run',side_effect=stop_after_saved_notice):
            tid = await self.submit('701 702')
        self.assertFalse(self.tools)
        self.reopen()
        await self.continue_turn(tid)
        self.assertEqual(self.tools,[('native','701'),('search','701'),('native','702'),('search','702')])
        with self.store.connection() as db:
            self.assertFalse(private_lookup.presented_notice(db,tid))
            self.assertEqual(db.execute("SELECT count(*) FROM expression_speech_units WHERE turn_id=? AND phase='search_notice'",
                                       (tid,)).fetchone()[0],1)

    async def test_background_search_rechecks_grant_at_dispatch(self):
        self.providers.background_lookup = True
        flow = self.chat.private_reply.ordinary
        run = flow._run

        async def revoke_after_notice(tid):
            continue_lookup = await run(tid)
            if continue_lookup:
                self.grant(False)
            return continue_lookup

        with patch.object(flow,'_run',side_effect=revoke_after_notice):
            tid = await self.submit('701 702')
        self.assertFalse(self.tools)
        self.assertEqual(self.chat.get(tid)['status'],'interrupted')

    async def test_public_first_turn_sends_rewritten_queries_intact(self):
        self.queries=['721','722']
        self.answers={'721':'721 711','722':'722 712'}
        await self.cold()
        self.assertEqual(self.tools,[('native','721'),('search','721'),('native','722'),('search','722')])

    async def test_no_grant_and_ordinary_chat(self):
        self.grant(False)
        tid=await self.submit('701 702')
        self.show(tid,'main')
        self.assertFalse(self.tools)
        self.assertEqual([k for k,_ in self.requests],['translate'])
        self.providers.route='none'
        self.requests.clear()
        self.show(await self.submit('703'),'main')
        self.assertFalse(self.requests)

    async def test_unknown_item_stops_the_batch_across_restart(self):
        self.failed_step='native'
        tid=await self.submit('701 702')
        self.show(tid,'search_notice')
        await self.continue_turn(tid)
        self.reopen()
        await self.continue_turn(tid)
        self.assertEqual(self.tools,[('native','701')])
        self.assertEqual(self.chat.get(tid)['error_code'],'reply_dispatch_unknown')

    async def test_revocation_after_notice_stops_public_dispatch(self):
        tid=await self.submit('701 702')
        self.show(tid,'search_notice')
        self.grant(False)
        await self.continue_turn(tid)
        self.assertFalse(self.tools)
        self.assertEqual(self.chat.get(tid)['status'],'interrupted')

    async def test_empty_supplement_keeps_answerable_item(self):
        self.queries=['701']
        self.answers={'701':'701 711'}
        await self.cold()
        self.queries.append('702')
        self.requests.clear()
        await self.cold()
        self.assertEqual([k for k,_ in self.requests],['translate','assess'])
        items=self.providers.bodies[-1][1]['research']['retrieval']['items']
        self.assertEqual([(q['result'],q['tool']) for q in items],
                         [('complete','not_dispatched'),('missing','empty')])

    async def test_known_unsent_item_reuses_received_sibling(self):
        from persona_runtime.chat import ModelError
        from persona_runtime.ingress import IngressService

        original=self.providers.retrieve
        failed=[]
        async def retrieve(step,aid,query,model):
            if step=='native' and query['question']=='702' and not failed:
                failed.append(aid)
                raise ModelError('provider_network_error',diagnostics={'request_headers_sent':False})
            return await original(step,aid,query,model)
        self.answers={'701':'701 711','702':'702 712'}
        self.providers.retrieve=retrieve
        tid=await self.submit('701 702')
        self.show(tid,'search_notice')
        await self.continue_turn(tid)
        self.assertEqual(self.tools,[('native','701'),('search','701')])
        self.reopen()
        self.chat.recover()
        ingress=IngressService(self.chat)
        ingress.recover()
        ingress.network_wake()
        ingress.schedule_committed()
        await self.chat.wait()
        self.show(tid,'main')
        self.assertEqual(self.tools,[('native','701'),('search','701'),('native','702'),('search','702')])
        with self.store.connection() as db:
            self.assertFalse(db.execute('SELECT 1 FROM retrieval_item_calls WHERE attempt_id=?',failed).fetchone())

    async def test_unresolved_translation_passes_clarification_to_writer(self):
        response=AsyncMock(return_value=({'type':'clarify','queries':[],'refs':[]},{}))
        self.providers.bind_retrieval=lambda base,state,phase:(response,{})
        self.show(await self.submit('701 702'),'main')
        self.assertEqual(response.await_count,1)
        self.assertFalse(self.tools)
        self.assertEqual(self.providers.bodies[-1][1]['research']['retrieval']['status'],'clarify')
        self.assertEqual(self.providers.bodies[-1][1]['action'],'clarify')

    async def test_history_limit_and_private_only_anchor(self):
        self.providers.retrieval_history_turns=2
        self.providers.route='none'
        for text in ('741','742','743'):
            self.show(await self.submit(text),'main')
        self.providers.route='retrieve'
        async def translate(aid,state):
            rows=[m for turn in state['history'] for m in turn['messages']]
            users=[m for m in rows if m['role']=='user']
            self.assertEqual([m['text'] for m in users],['742','743'])
            return {'status':'ready','queries':[{'text':'743','refs':[users[-1]['ref']]}]},{}
        self.providers.bind_retrieval=lambda base,state,phase:(lambda aid:translate(aid,state),{})
        self.show(await self.submit('744'),'main')
        self.assertFalse(self.tools)

    async def test_baseline_reader_keeps_old_and_new_scope_boundaries(self):
        self.answers={'701':'701 711','702':'702 712'}
        first=await self.cold()
        self.providers.retrieval_spec=None
        atomic_json(self.config.paths.home/'private/ordinary-reply/registration.json',
                    {'instance':self.config.load()['instance_id']})
        self.reopen()
        self.assertEqual(len(private_lookup.details(self.store,self.pid,self.sid,first,'research')['sources']),2)
        self.sid=self.chat.session(fresh=True)['session_id']
        self.consent(True)
        again=await self.submit('701')
        self.show(again,'main')
        self.assertEqual(len(self.tools),4)

    def test_bad_evidence_index_does_not_discard_another_items_evidence(self):
        packet={'queries':[{'id':1,'candidates':[1]},{'id':2,'candidates':[]}]}
        raw={'status':'assessed','items':[{'id':1,'result':'partial','passages':[1]},
                                         {'id':2,'result':'complete','passages':[2]}]}
        result=retrieval.assessment(raw,packet,{1:{'id':'701'}})
        self.assertEqual([v['result'] for v in result['items']],['partial','unconfirmed'])

    def enable_egress(self,judge):
        previous=self.providers.bind_retrieval
        self.providers.egress_spec={'system':'','recipient':{'max_input_chars_at_review':24000}}
        self.providers.bind_retrieval=lambda base,state,phase: (
            (lambda aid:judge(aid,state),{}) if phase=='egress' else previous(base,state,phase))

    async def seed_dialogue(self):
        self.providers.route='none'
        self.show(await self.submit('741'),'main')
        self.providers.route='retrieve'

    async def test_semantic_egress_batches_aliases_and_sends_only_the_approved_item(self):
        from persona_runtime import retrieval_egress
        judge=AsyncMock(return_value=({'items':[{'id':1,'result':'allow'},{'id':2,'result':'private'}]},{}))
        self.enable_egress(judge)
        await self.seed_dialogue()
        self.queries=['721','722']
        self.answers={'721':'721 711','722':'722 712'}
        with patch.object(retrieval,'exportable',side_effect=AssertionError('unexpected lexical review')):
            tid=await self.cold()
        self.assertEqual(self.tools,[('native','721'),('search','721')])
        judge.assert_awaited_once()
        payload=judge.await_args.args[1]
        self.assertEqual(set(payload),{'current','queries','basis'})
        self.assertEqual([q['text'] for q in payload['queries']],self.queries)
        self.assertFalse(payload['basis'])
        with self.store.connection() as db:
            proof=retrieval.saved(db,tid,'egress')
            self.assertEqual(proof['version'],retrieval_egress.DIGEST)
            self.assertEqual([r['decision'] for r in proof['items']],['allow','private'])
        items=self.providers.bodies[-1][1]['research']['retrieval']['items']
        self.assertEqual([q['tool'] for q in items],['acquired','not_dispatched'])

    async def test_egress_unknown_settles_without_public_dispatch_or_writer_stop(self):
        judge=AsyncMock(side_effect=TimeoutError())
        self.enable_egress(judge)
        await self.seed_dialogue()
        tid=await self.submit('701 702')
        self.show(tid,'main')
        judge.assert_awaited_once()
        self.assertFalse(self.tools)
        with self.store.connection() as db:
            proof=retrieval.saved(db,tid,'egress')
            self.assertEqual(proof['attempt_state'],'unknown')
            self.assertEqual([r['decision'] for r in proof['items']],['uncertain','uncertain'])

    async def test_egress_direct_and_protected_items_skip_semantic_review(self):
        judge=AsyncMock(side_effect=AssertionError('out-of-scope review'))
        self.enable_egress(judge)
        self.queries=['721','sk-1234567890123456']
        self.answers={'721':'721 711'}
        tid=await self.cold()
        judge.assert_not_awaited()
        self.assertEqual(self.tools,[('native','721'),('search','721')])
        with self.store.connection() as db:
            self.assertEqual([r['decision'] for r in retrieval.saved(db,tid,'egress')['items']],['direct','private'])

    async def test_egress_grant_is_checked_again_after_review_and_notice(self):
        self.enable_egress(AsyncMock(return_value=({'items':[{'id':1,'result':'allow'}]},{})))
        await self.seed_dialogue()
        tid=await self.submit('701 702')
        self.show(tid,'search_notice')
        self.grant(False)
        await self.continue_turn(tid)
        self.assertFalse(self.tools)
        self.assertEqual(self.chat.get(tid)['status'],'interrupted')

    def test_malformed_egress_item_is_withheld_independently(self):
        from persona_runtime.retrieval_egress import answers
        raw={'items':[{'id':1,'result':'allow'},{'id':2,'result':'private'},{'id':2,'result':'allow'},
                      {'id':3,'result':'outside'},{'id':8,'result':'allow'}]}
        self.assertEqual(answers(raw,{1,2,3,4}),{1:'allow',2:'uncertain',3:'outside',4:'uncertain'})

    async def test_quoted_reply_stays_with_its_original_question_in_retrieval_history(self):
        from persona_runtime.ingress import IngressService
        from persona_runtime.models import identifier

        self.providers.route='none'
        prior=await self.submit('741')
        shown=self.show(prior,'main')
        self.show(await self.submit('742'),'main')
        self.grant(False)
        self.providers.route='retrieve'
        with self.store.connection() as db:
            eid=db.execute('SELECT reply_event_id FROM turns WHERE id=?',(prior,)).fetchone()[0]
        ingress=IngressService(self.chat)
        rid=identifier()
        receipt=ingress.accept({'persona_id':self.pid,'session_id':self.sid,'channel':'desktop',
                                'request_id':rid,'text':'743','reference':{'kind':'event','id':eid}})
        batch=ingress.get(receipt['batch_id'])
        if batch['state']=='collecting':
            ingress.action(batch['id'],'seal',batch['revision'],identifier(),self.pid,self.sid)
        await self.chat.wait()
        self.show(ingress.lookup('desktop',rid)['turn_id'],'main')

        packet=self.requests[0][1]
        quote,=packet['references']
        self.assertEqual(quote['text'],shown)
        pair=next(r['messages'] for r in packet['history'] if r['messages'][0]['text']=='741')
        self.assertEqual([m['text'] for m in pair],['741',shown])
        self.assertEqual(pair[1]['ref'],quote['ref'])
        self.assertEqual(retrieval.references(packet)[quote['ref']]['kind'],'message')

    async def test_v2_focus_keeps_bound_topic_and_its_adjacent_clarification(self):
        from persona_runtime.ingress import IngressService
        from persona_runtime.models import identifier

        self.providers.bundle={'translation_manifest':True}
        self.providers.route='none'
        anchor=await self.submit('741')
        self.show(anchor,'main')
        self.show(await self.submit('742'),'main')
        with self.store.connection() as db:
            eid=db.execute('SELECT reply_event_id FROM turns WHERE id=?',(anchor,)).fetchone()[0]
        self.grant(False)
        self.providers.route='retrieve'
        judge=AsyncMock(return_value=({'status':'clarify','queries':[],'refs':[]},{}))
        self.providers.bind_retrieval=lambda base,state,phase:(lambda aid:judge(state),{})
        ingress=IngressService(self.chat)
        rid=identifier()
        receipt=ingress.accept({'persona_id':self.pid,'session_id':self.sid,'channel':'desktop',
                                'request_id':rid,'text':'743','reference':{'kind':'event','id':eid}})
        batch=ingress.get(receipt['batch_id'])
        if batch['state']=='collecting':
            ingress.action(batch['id'],'seal',batch['revision'],identifier(),self.pid,self.sid)
        await self.chat.wait()
        self.show(ingress.lookup('desktop',rid)['turn_id'],'main')
        first=judge.await_args.args[0]
        self.assertEqual([group['messages'][0]['text'] for group in first['history']],['741'])

        self.show(await self.submit('744'),'main')
        follow=judge.await_args.args[0]
        self.assertEqual([group['messages'][0]['text'] for group in follow['history']],['741','743'])
        self.assertEqual(follow['references'],[])

    async def test_egress_uses_public_sources_of_a_quoted_cross_session_hit(self):
        from persona_runtime.retrieval_egress import review_basis

        origin_sid=self.sid
        self.queries=['701']
        self.answers={'701':'701 711'}
        await self.cold()
        self.sid=self.chat.session(fresh=True)['session_id']
        self.consent(True)
        cited=await self.submit('701')
        shown=self.show(cited,'main')
        follow=await self.submit('701')
        self.show(follow,'main')
        with self.store.connection() as db:
            turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(follow,)).fetchone())
            eid=db.execute('SELECT reply_event_id FROM turns WHERE id=?',(cited,)).fetchone()[0]
            cfg=self.config.load()
            grant=fixture.session_privacy.current(db,self.config,cfg,self.pid,self.sid,
                                                  fixture.session_privacy.recipients(self.config,cfg))
            base={'instance':cfg['instance_id'],'consent':grant}
            plan={'anchor_events':{'1':eid}}
            anchor={'ref':1,'text':shown}
            expected={'ref':1,'use':'public_basis','text':'701 711'}
            self.assertEqual(review_basis(db,turn,base,plan,anchor),expected)
            candidates=semantic_knowledge.materials(db,turn,base,'701')
            anchored=retrieval.anchored_answer_rows(db,turn,plan,{'refs':[1]},candidates)
            self.assertEqual([row['text'] for row in anchored],['701 711'])
            self.assertEqual(retrieval.anchored_answer_rows(db,turn,plan,{'refs':[]},candidates),[])
            witness=fixture.session_privacy._witness(self.config,cfg,origin_sid)
            for path in (witness,witness.with_suffix('.public.json')):
                with self.subTest(missing=path.suffixes):
                    original=path.read_bytes()
                    path.unlink()
                    try:
                        private={'ref':1,'use':'context_only','text':shown}
                        self.assertEqual(review_basis(db,turn,base,plan,anchor),private)
                    finally:
                        path.write_bytes(original)
            self.assertEqual(review_basis(db,turn,base,plan,anchor),expected)
