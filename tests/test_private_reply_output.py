import json
import unittest
from hashlib import sha256

from persona_runtime import private_lookup
from persona_runtime.models import RuntimeErrorBase, identifier
from persona_runtime.ordinary_providers import ReviewedPrivateChatProviders
from persona_runtime.private_reply_output import (
    ADVISORY_VERSION, FACT_REVIEW_VERSION, SEARCH_SPEECH_VERSION, SEMANTIC_REVIEW_VERSION, VERSION, assess, limits,
)
from persona_runtime.workbench_timeline import turn_card
from tests import test_private_reply_stage3 as stage3


class FormatChecks(unittest.TestCase):
    def test_one_bubble_and_sentence_limits_are_independent(self):
        contract = limits('main', 'short_answer')
        self.assertEqual(assess('7'*74+'。', None, contract)['failed_constraints'], [])
        self.assertEqual(assess('7'*75+'。', None, contract)['failed_constraints'], ['max_bubble_chars'])
        self.assertEqual(assess('701。702。', None, contract)['failed_constraints'], ['sentences_per_bubble'])
        self.assertEqual(assess('701。702。', None, limits('search_notice', 'short_answer'))['failed_constraints'], [])
        self.assertEqual(assess('701。', None, limits('main', 'long_detail'))['failed_constraints'], ['detail_required'])
        self.assertEqual(assess('701。', '701'*80, limits('main', 'long_detail'))['failed_constraints'], [])


class OutputHandoff(unittest.IsolatedAsyncioTestCase):
    reopen = stage3.ResearchTests.reopen
    submit = stage3.ResearchTests.submit
    paint = stage3.ResearchTests.paint
    show = stage3.ResearchTests.show
    consent = stage3.ResearchTests.consent
    continue_turn = stage3.ResearchTests.continue_turn

    def setUp(self):
        stage3.ResearchTests.setUp(self)
        self.providers.output_contract_version = VERSION

    async def test_conversation_keeps_full_reply_across_fragment_restart(self):
        self.consent(True)
        self.providers.output_contract_version = FACT_REVIEW_VERSION
        self.providers.conversation_systems = {'reply_system': '701'}
        self.providers.route = 'none'
        self.providers.consume = False
        self.providers.body = ('。'.join(['甲'*30]*7))+'。'
        choose = self.providers.choose
        async def choose_short(node, attempt, state, options):
            if node == 'J6.mode':
                return 'short_answer', {}
            return await choose(node, attempt, state, options)
        self.providers.choose = choose_short
        tid = await self.submit()
        unit = self.out.list(self.pid,self.sid,tid)[0]
        uid = unit['id']
        plan = self.out.plan(self.pid,self.sid,uid)
        self.assertGreater(len(plan['items']), 1)
        constraints = self.providers.bodies[-1][1]['output_constraints']
        self.assertEqual(constraints, {'phase':'main','mode':'short_answer','output_fields':['body']})
        prefix = self.paint(uid,plan,0)
        self.reopen()
        restored = self.out.plan(self.pid,self.sid,uid)
        self.assertEqual((restored['id'],restored['revision']), (plan['id'],plan['revision']))
        self.assertEqual(restored['items'][0]['state'],'presented')
        pieces = [prefix]+[self.paint(uid,restored,i) for i in range(1,len(restored['items']))]
        self.assertEqual(''.join(pieces), self.providers.body)
        self.assertEqual(self.chat.get(tid)['status'],'completed')
        self.assertFalse(self.providers.lookups)

    def paint_full_bubble(self, tid, phase, expected):
        unit = next(u for u in self.out.list(self.pid,self.sid,tid) if u['phase']==phase)
        plan = self.out.plan(self.pid,self.sid,unit['id'])
        self.assertEqual(len(plan['items']),1)
        receipt = identifier()
        presenter = 'webview-v2.'+receipt
        part = self.out.offer(self.pid,self.sid,unit['id'],plan['id'],plan['revision'],0,presenter)
        self.assertEqual(part['body'],expected)
        self.out.acknowledge(self.pid,self.sid,unit['id'],part['id'],part['lease_id'],presenter,receipt,
                             part['body_hash'],webview=True,publication_id=plan['id'],revision=plan['revision'],ordinal=0)

    async def test_advisory_length_preserves_original_notice_and_main(self):
        self.consent(True)
        self.providers.output_contract_version = ADVISORY_VERSION
        self.providers.consume = False
        self.providers.notice_body = '7'*76+'。701。'
        self.providers.body = '8'*76+'。702。'
        tid = await self.submit()
        self.assertFalse(self.providers.lookups)
        self.paint_full_bubble(tid,'search_notice',self.providers.notice_body)
        await self.continue_turn(tid)
        self.paint_full_bubble(tid,'main',self.providers.body)
        self.assertEqual(len(self.providers.lookups),1)
        self.assertEqual(self.chat.get(tid)['status'],'completed')
        self.assertEqual(len(self.providers.bodies),2)
        with self.store.connection() as db:
            status=db.execute("""SELECT r.approved,r.reason FROM private_reply_reviews r
                JOIN private_reply_attempts a ON a.id=r.attempt_id
                WHERE a.turn_id=? AND a.phase='main'""",(tid,)).fetchone()
            self.assertEqual(tuple(status),(1,'local_publication_ready'))
            confirmed=db.execute("""SELECT v.text FROM expression_speech_visible v JOIN expression_speech_units u
                ON u.id=v.utterance_id WHERE u.turn_id=? AND u.phase='main'""",(tid,)).fetchone()
            self.assertEqual(confirmed[0],self.providers.body)

    async def test_advisory_format_receipt_presents_body_without_cloud_review(self):
        self.consent(True)
        self.providers.output_contract_version = SEMANTIC_REVIEW_VERSION
        self.providers.consume = False
        self.providers.notice_body = '7'*32 + '。'
        self.providers.body = '8'*76 + '。702。'
        tid = await self.submit()
        self.paint_full_bubble(tid, 'search_notice', self.providers.notice_body)
        await self.continue_turn(tid)
        self.paint_full_bubble(tid, 'main', self.providers.body)
        self.assertNotIn('J7',[node for node,*_ in self.providers.calls])
        self.assertEqual(len(self.providers.lookups), 1)
        self.assertEqual(self.chat.get(tid)['status'], 'completed')
        with self.store.connection() as db:
            result = db.execute("""SELECT a.id,r.approved,r.reason FROM private_reply_reviews r
                JOIN private_reply_attempts a ON a.id=r.attempt_id WHERE a.turn_id=? AND a.phase='main'""",
                (tid,)).fetchone()
            self.assertEqual(tuple(result)[1:], (1, 'local_publication_ready'))
            raw = db.execute('SELECT value FROM knowledge_control WHERE key=?',
                             ('private.reply.format.' + result['id'],)).fetchone()[0]
        self.assertEqual(set(json.loads(raw)['failed_constraints']), {'max_bubble_chars', 'sentences_per_bubble'})

    async def test_legacy_long_reply_still_requires_details(self):
        self.providers.output_contract_version = SEMANTIC_REVIEW_VERSION
        self.providers.route = 'none'
        self.providers.long = True
        complete = self.providers.reply

        async def reply(attempt, packet):
            if not packet['repair_reason']:
                return '810。', {}
            return await complete(attempt, packet)

        self.providers.reply = reply
        turn = await self.chat.submit('desktop', '828', identifier(), self.sid, persona_id=self.pid, long_requested=True)
        await self.chat.wait()
        tid = turn['id']
        self.paint_full_bubble(tid, 'main', '810。')
        self.assertEqual(private_lookup.details(self.store, self.pid, self.sid, tid, 'long')['body'], '811。'*75)
        self.assertNotIn('J7',[node for node,*_ in self.providers.calls])
        with self.store.connection() as db:
            first = db.execute("SELECT id FROM private_reply_attempts WHERE turn_id=? AND draft=0", (tid,)).fetchone()[0]
            raw = db.execute('SELECT value FROM knowledge_control WHERE key=?', ('private.reply.format.' + first,)).fetchone()[0]
        self.assertIn('detail_required', json.loads(raw)['failed_constraints'])

    async def test_approved_short_answer_system_is_used_on_writer_wire(self):
        from persona_runtime.archive import fingerprint
        from persona_runtime.provider_compat import build_request

        nodes=('J1','J2.action','J2.stance','J2.task','J3','J4','J5','J6.mode','J6.coverage','J6.ending','J7')
        model = {k:self.config.load()['model'][k] for k in ('base_url','name')}
        bundle = {
            'protocol':'maibot-private-v3', 'output_contract_version':VERSION,
            'scope':{'instance':self.config.load()['instance_id'],'personas':[self.pid]},
            'main':model, 'search':model|{'name':'deepseek-flash'},
            'reply_system':'700', 'notice_system':'701', 'long_system':'702', 'search_system':'703',
            'short_answer_system':'704',
            'nodes':{node:{'instructions':'705','criteria':{}} for node in nodes},
        }
        path=self.paths.home/'output-v1.json'
        path.write_text(json.dumps(bundle),encoding='utf-8')
        received=[]
        requests=[]

        class Writer:
            async def complete(self, actual, messages):
                received.append(messages)
                requests.append(build_request(actual,messages)[0])
                if messages[0]['content'].splitlines()[0]=='702':
                    return '{"body":"706","detail":"707"}', {}
                return '706。', {}

        original_digest=sha256(path.read_bytes()).hexdigest()
        adapter=ReviewedPrivateChatProviders(self.config,path,original_digest,writer=Writer())
        with self.assertRaisesRegex(RuntimeErrorBase,'private_reply_output_contract_missing'):
            await adapter.reply('707',{'phase':'main','mode':'short_answer'})
        for phase in ('main','search_notice'):
            _,usage=await adapter.reply('707',{'phase':phase,'mode':'short_answer',
                                                'output_constraints':limits(phase,'short_answer')})
            self.assertNotIn('evidence_guidance',usage)
        self.assertEqual([m[0]['content'] for m in received],['704','701'])
        self.assertEqual(json.loads(received[0][1]['content'])['output_constraints'],limits('main','short_answer'))

        path.write_text(json.dumps(bundle|{'insufficient_evidence_system':None}),encoding='utf-8')
        with self.assertRaisesRegex(RuntimeErrorBase,'private_chat_review_incomplete'):
            ReviewedPrivateChatProviders(self.config,path,sha256(path.read_bytes()).hexdigest(),writer=Writer())
        path.write_text(json.dumps(bundle|{'insufficient_evidence_system':'708'}),encoding='utf-8')
        with self.assertRaisesRegex(RuntimeErrorBase,'private_chat_review_version_changed'):
            ReviewedPrivateChatProviders(self.config,path,original_digest,writer=Writer())

        candidate=ReviewedPrivateChatProviders(self.config,path,sha256(path.read_bytes()).hexdigest(),writer=Writer())
        for phase,mode,status,system in (
            ('main','short_answer','insufficient','704\n708'),
            ('main','long_detail','insufficient','702\n708'),
            ('main','chat','ordinary','700'),
            ('main','short_answer','local_hit','704'),
            ('search_notice','short_answer','insufficient','701'),
        ):
            with self.subTest(phase=phase,mode=mode,status=status):
                packet={'phase':phase,'mode':mode,'output_constraints':limits(phase,mode),
                        'research':{'status':status,'facts':[]}}
                _,usage=await candidate.reply('707',packet)
                self.assertEqual(received[-1][0]['content'],system)
                self.assertEqual(json.loads(received[-1][1]['content']),packet)
                if phase=='main':
                    expected={'applied':status=='insufficient','request_sha256':fingerprint(requests[-1])}
                    if expected['applied']:
                        expected['note_sha256']=sha256(b'708').hexdigest()
                    self.assertEqual(usage['evidence_guidance'],expected)
                else:
                    self.assertNotIn('evidence_guidance',usage)

    async def test_ordinary_writer_receives_phase_for_advisory_limits(self):
        self.providers.research=False
        self.providers.output_contract_version=ADVISORY_VERSION
        self.reopen()

        async def respond(attempt,packet):
            self.assertEqual(packet['phase'],'main')
            self.assertEqual(packet['output_constraints'],limits('main',packet['mode']))
            self.assertNotIn('research',packet)
            return self.providers.body,{}

        self.providers.reply=respond
        tid=await self.submit()
        self.assertEqual(self.show(tid,'main'),self.providers.body)
        self.assertEqual(self.chat.get(tid)['status'],'completed')

    async def test_scoped_notice_uses_no_dialogue_and_main_requires_visible_receipt(self):
        self.providers.phase_scoped_search = True
        self.providers.output_contract_version = FACT_REVIEW_VERSION
        self.consent(True)
        tid = await self.submit()
        self.assertEqual(self.providers.bodies[-1][1], {
            'phase':'search_notice', 'mode':'short_answer',
            'output_constraints':limits('search_notice','short_answer',compact_notice=True),
        })
        self.assertFalse(self.providers.lookups)
        self.assertIsNone(self.providers.bodies[-1][1].get('search_notice_presented'))
        self.show(tid,'search_notice')
        await self.continue_turn(tid)
        self.assertEqual(len(self.providers.lookups),1)
        main=self.providers.bodies[-1][1]
        self.assertTrue(main['search_notice_presented'])
        self.assertTrue(main['current'])
        self.assertTrue(main['research']['facts'])
        self.show(tid,'main')

        notice_text=self.providers.notice_body
        self.providers.route='none'
        await self.submit()
        following=self.providers.bodies[-1][1]
        self.assertNotIn('search_notice_presented',following)
        self.assertNotIn(notice_text,{row['text'] for row in following['dialogue']+following['evidence']})

    async def test_scoped_search_templates_and_notice_packet_are_enforced_on_wire(self):
        nodes=('J1','J2.action','J2.stance','J2.task','J3','J4','J5','J6.mode','J6.coverage','J6.ending','J7')
        model={k:self.config.load()['model'][k] for k in ('base_url','name')}
        bundle={
            'protocol':'maibot-private-v3', 'research_context_version':'search-evidence-v2',
            'local_lookup_version':'local-first-v2', 'output_contract_version':FACT_REVIEW_VERSION,
            'search_speech_version':SEARCH_SPEECH_VERSION,
            'scope':{'instance':self.config.load()['instance_id'],'personas':[self.pid]},
            'main':model, 'search':model|{'name':'deepseek-flash'}, 'notice':model|{'name':'deepseek-flash'},
            'reply_system':'700', 'notice_system':'701', 'long_system':'702', 'search_system':'703',
            'short_answer_system':'704', 'post_search_system':'711', 'post_search_long_system':'712',
            'insufficient_evidence_system':'725',
            'nodes':{node:{'instructions':'705','criteria':{}} for node in nodes},
        }
        bundle['knowledge_study']={'version':'source-facts-v1','model':bundle['search'],'instructions':'JSON'}
        bundle['nodes']['J4']['criteria']={'retrieve':'721','none':'722'}
        bundle['nodes']['J7']['criteria']={'accept':'723','fact':'724'}
        path=self.paths.home/'scoped-search.json'
        path.write_text(json.dumps(bundle),encoding='utf-8')
        received=[]
        requests=[]

        class Writer:
            async def complete(self, actual, messages):
                received.append((actual['name'],messages))
                from persona_runtime.provider_compat import build_request
                requests.append(build_request(actual,messages)[0])
                return ('{"body":"706","detail":"707"}' if messages[0]['content']=='712' else '706。'), {}

        adapter=ReviewedPrivateChatProviders(self.config,path,sha256(path.read_bytes()).hexdigest(),writer=Writer())
        self.assertNotIn('J7',adapter.bundle['nodes'])
        with self.assertRaisesRegex(RuntimeErrorBase,'private_reply_decision_unavailable'):
            await adapter._choose(model,'707','J7',{},('accept',))
        notice={'phase':'search_notice','mode':'short_answer',
                'output_constraints':limits('search_notice','short_answer',compact_notice=True)}
        with self.assertRaisesRegex(RuntimeErrorBase,'private_search_notice_input_invalid'):
            await adapter.reply('707',notice|{'current':['709']})
        await adapter.reply('707',notice)
        main={'phase':'main','mode':'short_answer','output_constraints':limits('main','short_answer')}
        await adapter.reply('707',main)
        await adapter.reply('707',main|{'search_notice_presented':True})
        await adapter.reply('707',main|{'search_notice_presented':True,'mode':'long_detail',
                                      'output_constraints':limits('main','long_detail')})
        self.assertEqual(received[0][0],'deepseek-flash')
        self.assertEqual([m[1][0]['content'] for m in received],['701','704','711','712'])
        notice_wire, ordinary_wire, post_wire, long_wire = [json.loads(m[1][1]['content']) for m in received]
        self.assertEqual(set(notice_wire),set(notice))
        self.assertNotIn('output_fields',notice_wire['output_constraints'])
        self.assertNotIn('output_fields',post_wire['output_constraints'])
        self.assertNotIn('output_fields',ordinary_wire['output_constraints'])
        self.assertEqual(long_wire['output_constraints']['output_fields'],['body','detail'])

        for tool in ('acquired','empty','failed','not_dispatched'):
            await adapter.reply('707',main|{'research':{'retrieval':{'tool':tool}}})
        self.assertEqual([m[1][0]['content'] for m in received[-4:]],['711','711','711','704'])
        self.assertTrue(all('search_notice_presented' not in json.loads(m[1][1]['content'])
                            for m in received[-4:]))
        await adapter.reply('707',main|{'research':{'status':'insufficient','retrieval':{'tool':'acquired'}}})
        self.assertEqual(received[-1][1][0]['content'],'711\n725')

        from persona_runtime.provider_policy import PRIVATE_COMPATIBLE_WRITER, require_generation_provider
        from persona_runtime.session_privacy import recipients
        gateway, model_name = PRIVATE_COMPATIBLE_WRITER
        self.config.update({
            'private_research_account':'organization',
            'model':{'base_url':gateway,'name':model_name},
            'organization':{'reuse_chat':False,'model':bundle['search']|{'api_key':'801'}},
        })
        bundle.update(main={'base_url':gateway,'name':model_name},
                      writer_transport='openai-chat-completions-v1',search_account='organization')
        path.write_text(json.dumps(bundle),encoding='utf-8')
        adapter=ReviewedPrivateChatProviders(self.config,path,sha256(path.read_bytes()).hexdigest(),writer=Writer())
        with self.assertRaisesRegex(ValueError,'provider_official_deepseek_required'):
            require_generation_provider(adapter._model('main'))
        require_generation_provider(adapter._model('main'),allow_private_compatible_writer=True)
        with self.assertRaisesRegex(ValueError,'provider_official_deepseek_required'):
            require_generation_provider(adapter._model('main')|{'base_url':'https://example.invalid/v1'},
                                        allow_private_compatible_writer=True)
        dest=recipients(self.config,self.config.load())
        self.assertEqual(dest['main']['endpoint'],gateway)
        self.assertEqual(dest['search_fetch']['account'],dest['organization']['account'])
        self.assertEqual(adapter.notice_model()['base_url'],bundle['search']['base_url'])
        await adapter.reply('710',main|{'search_notice_presented':True})
        self.assertEqual(received[-1][0],model_name)
        self.assertTrue(requests[-1]['stream'])
        self.assertEqual(requests[-1]['max_tokens'],self.config.load()['model']['max_tokens'])
        self.assertFalse(requests[0]['stream'])

        official_flash=bundle['search']
        relay_flash={'base_url':gateway,'name':'deepseek-flash'}
        relay_pro={'base_url':gateway,'name':'deepseek-v4-pro'}
        self.config.update({
            'private_research_account':'native_web',
            'native_web_model':official_flash|{'api_key':'801'},
            'model':relay_pro|{'api_key':'802'},
            'organization':{'reuse_chat':False,'model':relay_flash|{'api_key':'802'}},
        })
        bundle.update(main=relay_pro,notice=relay_flash,search_account='native_web',
                      text_flash_account='organization')
        bundle['knowledge_study']['model']=relay_flash
        path.write_text(json.dumps(bundle),encoding='utf-8')
        adapter=ReviewedPrivateChatProviders(self.config,path,sha256(path.read_bytes()).hexdigest(),
                                             writer=Writer(),organizer=Writer())
        dest=recipients(self.config,self.config.load())
        self.assertEqual(dest['main'],dest['organization'])
        self.assertEqual(dest['search_fetch']['account'],dest['native_web']['account'])
        self.assertNotEqual(dest['main']['account'],dest['native_web']['account'])
        self.assertEqual(adapter.notice_model()['base_url'],gateway)
        self.assertEqual(adapter.search_model()['base_url'],official_flash['base_url'])
        self.assertNotIn('encrypted_key',self.config.public()['native_web_model'])
        await adapter.reply('730',notice)
        await adapter.organize('731',{},adapter.study_model())
        await adapter.reply('732',main|{'search_notice_presented':True})
        self.assertEqual([p['model'] for p in requests[-3:]],['deepseek-flash','deepseek-flash','deepseek-v4-pro'])
        self.assertTrue(all(not p.get('tools') for p in requests[-3:]))
        self.assertEqual(requests[-2]['response_format'],{'type':'json_object'})

        from unittest.mock import AsyncMock
        from persona_runtime.provider_compat import build_request
        from persona_runtime import lookup_target, session_privacy
        from persona_runtime.knowledge_routing import configured_model
        dedicated={'name':'deepseek-v4-flash','base_url':'https://api.deepseek.com'}
        prev=session_privacy.recipients(self.config,self.config.load())
        self.config.update({'lookup_target_model':dedicated|{'api_key':'811'}})
        self.assertNotIn('lookup_target',prev)
        self.assertNotIn('encrypted_key',self.config.public()['lookup_target_model'])
        grant=self.consent(True)
        self.assertEqual(grant['recipients']['lookup_target'],dedicated['base_url'])
        bundle['jev']={k:configured_model(self.config.paths)[k] for k in ('base_url','name')}
        bundle['lookup_target']={'version':lookup_target.VERSION,'model':dedicated,'system':bundle['knowledge_study']['instructions']}
        path.write_text(json.dumps(bundle),encoding='utf-8')
        resolver=AsyncMock()
        resolver.complete.return_value=('{"parts":[]}',{'total_tokens':4})
        adapter=ReviewedPrivateChatProviders(self.config,path,sha256(path.read_bytes()).hexdigest(),target_client=resolver)
        state={'fields':[{'ref':'817','text':'818'}]}
        with self.assertRaisesRegex(RuntimeErrorBase,'not_authorized'):
            adapter.bind_target({'consent':grant|{'enabled':False}},state)
        invoke,audit=adapter.bind_target({'consent':grant},state)
        answer,usage=await invoke('819')
        self.assertEqual((answer,usage['usage']['total_tokens']),({'parts':[]},4))
        sent,messages=resolver.complete.await_args.args
        self.assertEqual((sent['name'],audit['endpoint'],messages[0]['content']),
                         (dedicated['name'],dedicated['base_url'],bundle['lookup_target']['system']))
        self.assertEqual(json.loads(messages[1]['content']),state)
        self.assertNotIn('tools',build_request(sent,messages)[0])
        from unittest.mock import patch
        from persona_runtime import knowledge_coverage, public_lookup
        from persona_runtime.chat import ModelCompletion
        from persona_runtime.archive import fingerprint
        bundle['public_lookup_version']=public_lookup.VERSION
        path.write_text(json.dumps(bundle),encoding='utf-8')
        with self.assertRaisesRegex(RuntimeErrorBase,'review_incomplete'):
            ReviewedPrivateChatProviders(self.config,path,sha256(path.read_bytes()).hexdigest())
        bundle['local_lookup_version']=knowledge_coverage.VERSION
        bundle['knowledge_study']['version']=knowledge_coverage.STUDY_VERSION
        selector=bundle['nodes']['J5']
        selector.update(criteria=dict.fromkeys(('complete','insufficient'),selector['instructions']),
                        source_criterion=selector['instructions'])
        path.write_text(json.dumps(bundle),encoding='utf-8')
        with self.assertRaisesRegex(RuntimeErrorBase,'review_incomplete'):
            ReviewedPrivateChatProviders(self.config,path,sha256(path.read_bytes()).hexdigest())
        bundle['public_lookup_j5']={'instructions':'810','criteria':{'complete':'811','source/0':'812','insufficient':'813'}}
        path.write_text(json.dumps(bundle),encoding='utf-8')
        signed=ReviewedPrivateChatProviders(self.config,path,sha256(path.read_bytes()).hexdigest())
        from persona_runtime.public_lookup_study import SOURCE_OPTIONS
        transport=AsyncMock()
        transport.complete.return_value=(200,'{}')
        signed.jev_factory=lambda model:transport
        _,audit=await signed.bind_public_source()('J5','809',{'question':'807','source':{}},SOURCE_OPTIONS)
        wire=transport.complete.await_args.args[0]['questions']['J5']
        self.assertEqual(wire,dict(type='choice',**bundle['public_lookup_j5']))
        self.assertEqual(audit['purpose'],'public_lookup_j5')
        self.assertEqual(audit['request_sha256'],fingerprint(transport.complete.await_args.args[0]))
        await signed.bind_choose()('J5','808',{'facts':[]},('drop/0','insufficient'))
        old=transport.complete.await_args.args[0]['questions']['J5']
        self.assertEqual(old['criteria']['drop/0'],selector['source_criterion'])
        from persona_runtime.ordinary_providers import register
        self.assertFalse(session_privacy.status(self.store,self.config,self.pid,self.sid)['public_lookup']['available'])
        register(self.config,path,sha256(path.read_bytes()).hexdigest())
        self.assertTrue(session_privacy.status(self.store,self.config,self.pid,self.sid)['public_lookup']['available'])
        search=signed.search_model()
        reply=ModelCompletion('',{}, {},native={'outcome':'completed','assistant':{}})
        with patch('persona_runtime.deepseek_native_transport.DeepSeekNativeClient.complete',
                   new=AsyncMock(return_value=reply)) as capture:
            send,endpoint,payload=signed.bind_retrieve('native','820',{'question':'821','as_of':'2026-11-21'},search)
            await send()
            actual,messages=capture.await_args.args
            self.assertEqual(fingerprint(payload),fingerprint(build_request(actual,messages)[0]))
            self.assertEqual(endpoint,'https://api.deepseek.com/chat/completions')
            self.assertEqual([m['role'] for m in messages],['system','user'])
            self.assertTrue(messages[-1]['content'].endswith('821'))
        with patch('persona_runtime.knowledge_web_transport.request',new=AsyncMock(return_value=('{}',{}))) as capture:
            send,endpoint,payload=signed.bind_retrieve('search','822',{'query':'821'},search)
            await send()
            self.assertEqual(capture.await_args.args[1],payload)
            self.assertEqual(payload,{'query':'821','count':8,'need_content':True})
            self.assertEqual(endpoint,'https://api.deepseek.com/v1/tools/search')
        from persona_runtime import reply_provenance
        bundle['lookup_target']['version']=lookup_target.QUOTED_TARGET_VERSION
        bundle['provenance_version']=reply_provenance.VERSION
        bundle['lookup_delivery_system']='817 JSON'
        bundle['nodes'][reply_provenance.NODE]={'instructions':'818','criteria':{'grounded':'819','unbound':'820'}}
        path.write_text(json.dumps(bundle),encoding='utf-8')
        writer=AsyncMock()
        writer.complete.return_value=('{"body":"801","claims":[]}',{})
        attributed=ReviewedPrivateChatProviders(self.config,path,sha256(path.read_bytes()).hexdigest(),writer=writer)
        body,usage=await attributed.reply('821',main|{'provenance':{'version':reply_provenance.VERSION,'fresh_fact_ids':[]}})
        sent,messages=writer.complete.await_args.args
        self.assertEqual(messages[0]['content'],bundle['lookup_delivery_system'])
        self.assertTrue(sent['_json_output'])
        self.assertEqual((body,usage['lookup_attribution']),('801',{'claims':[]}))
        self.config.update({'lookup_target_model':dedicated|{'api_key':'812'}})
        with self.assertRaisesRegex(RuntimeErrorBase,'not_authorized'):
            adapter.bind_target({'consent':grant},state)

        official_pro={'name':'deepseek-v4-pro','base_url':official_flash['base_url']}
        self.config.update({'model':official_pro|{'api_key':'801'}})
        bundle['main']=official_pro
        path.write_text(json.dumps(bundle),encoding='utf-8')
        direct=ReviewedPrivateChatProviders(self.config,path,sha256(path.read_bytes()).hexdigest(),writer=writer)
        writer.complete.return_value=(json.dumps({'body':'801','mode':main['mode'],'claims':[]}),{})
        body,usage=await direct.reply('821',main|{'provenance':{'version':reply_provenance.VERSION,'fresh_fact_ids':[]}})
        self.assertEqual((body,usage['lookup_attribution']),('801',{'claims':[]}))
        sent,messages=writer.complete.await_args.args
        self.assertEqual(sent['base_url'],official_pro['base_url'])
        self.assertEqual(messages[0]['content'],bundle['lookup_delivery_system'])
        self.assertEqual(direct.notice_model()['base_url'],gateway)
        self.assertEqual(direct.search_model()['base_url'],official_flash['base_url'])
        from persona_runtime.ordinary_providers import OFFICIAL_DEEPSEEK_ROUTE
        from persona_runtime.config import atomic_json
        bundle['generation_route']=OFFICIAL_DEEPSEEK_ROUTE
        path.write_text(json.dumps(bundle),encoding='utf-8')
        with self.assertRaisesRegex(RuntimeErrorBase,'recipient_changed'):
            ReviewedPrivateChatProviders(self.config,path,sha256(path.read_bytes()).hexdigest())
        bundle['notice']=bundle['knowledge_study']['model']=official_flash
        self.config.update({'organization':{'reuse_chat':False,'model':official_flash|{'api_key':'801'}}})
        router=self.paths.home/'private/knowledge-router/config.json'
        bundle['jev']={'base_url':gateway,'name':'jev-1.14'}
        atomic_json(router,json.loads(router.read_bytes())|bundle['jev'])
        from persona_runtime import research_contract
        bundle['research_coverage']={'version':research_contract.VERSION,'model':official_flash,
                                     'system':bundle['knowledge_study']['instructions']}
        path.write_text(json.dumps(bundle),encoding='utf-8')
        organizer=AsyncMock()
        organizer.complete.return_value=('{}',{})
        official=ReviewedPrivateChatProviders(self.config,path,sha256(path.read_bytes()).hexdigest(),organizer=organizer)
        self.assertEqual(official._model('jev')['base_url'],gateway)
        self.assertEqual([m['base_url'] for m in (official._model('main'),official.notice_model(),
                         official.study_model(),official.target_model(),official.search_model())],
                         [official_flash['base_url']]*5)
        await official.organize('824',{},official.study_model())
        self.assertEqual(organizer.complete.await_args.args[0]['base_url'],official_flash['base_url'])
        granted=self.consent(True)
        check,audit=official.bind_coverage({'consent':granted},{'version':research_contract.VERSION})
        await check('825')
        self.assertEqual(audit['endpoint'],official_flash['base_url'])
        self.config.update({'organization':{'reuse_chat':False,'model':relay_flash|{'api_key':'802'}}})
        with self.assertRaisesRegex(RuntimeErrorBase,'recipient_changed'):
            official.bind_phase_reply('search_notice')
        self.config.update({'organization':{'reuse_chat':False,'model':official_flash|{'api_key':'801'}}})
        bundle['main']=relay_pro
        self.config.update({'model':relay_pro|{'api_key':'802'}})
        path.write_text(json.dumps(bundle),encoding='utf-8')
        with self.assertRaisesRegex(RuntimeErrorBase,'recipient_changed'):
            ReviewedPrivateChatProviders(self.config,path,sha256(path.read_bytes()).hexdigest())
        bundle['main']=official_pro|{'base_url':'https://example.invalid'}
        path.write_text(json.dumps(bundle),encoding='utf-8')
        with self.assertRaisesRegex(RuntimeErrorBase,'recipient_changed'):
            ReviewedPrivateChatProviders(self.config,path,sha256(path.read_bytes()).hexdigest(),writer=writer)

    async def test_format_failure_reuses_decisions_and_search_then_displays_original(self):
        self.consent(True)
        complete = self.providers.reply

        async def respond(attempt, packet):
            if packet['phase'] == 'main' and packet['repair_reason'] is None:
                self.providers.bodies.append((attempt,packet))
                return '7'*31+'。'+'7'*30+'。', {}
            return await complete(attempt,packet)

        self.providers.reply = respond
        tid = await self.submit()
        self.assertEqual(self.providers.bodies[0][1]['output_constraints'], limits('search_notice','short_answer'))
        self.show(tid, 'search_notice')
        await self.continue_turn(tid)
        packets = [packet for _,packet in self.providers.bodies if packet['phase']=='main']
        self.assertEqual(len(packets),2)
        self.assertTrue(all(p['output_constraints']==limits('main','short_answer') for p in packets))
        self.assertEqual(packets[0]['research']['facts'],packets[1]['research']['facts'])
        observations = packets[1]['repair_observations']
        self.assertEqual(observations['failed_constraints'], ['sentences_per_bubble'])
        self.assertEqual(observations['observed_counts']['body_characters'],63)
        self.assertEqual(len(self.providers.lookups),1)
        with self.store.connection() as db:
            source = db.execute("SELECT receipt_id FROM private_reply_reviews r JOIN private_reply_attempts a ON a.id=r.attempt_id WHERE a.phase='main' AND a.draft=0").fetchone()[0]
            self.assertTrue(source.startswith('format/'))
            saved = db.execute("SELECT value FROM knowledge_control WHERE key=?",('private.reply.format.'+source.removeprefix('format/'),)).fetchone()
            self.assertEqual(json.loads(saved[0])['semantic_review'],'skipped_for_format')
            self.assertEqual(db.execute("SELECT count(*) FROM private_reply_nodes WHERE turn_id=? AND node LIKE '%J7'",(tid,)).fetchone()[0],0)
        self.assertIn('903',self.show(tid,'main'))

    async def test_saved_research_remains_readable_after_format_rejection(self):
        self.consent(True)
        self.providers.consume = False
        self.providers.body = '7'*38+'。'+'8'*36+'。'
        tid = await self.submit()
        self.show(tid,'search_notice')
        await self.continue_turn(tid)
        self.assertEqual(self.chat.get(tid)['error_code'],'private_reply_review_rejected')
        self.assertEqual(private_lookup.details(self.store,self.pid,self.sid,tid,'research')['notes'],'901。902。')
        with self.store.connection() as db:
            row=db.execute('SELECT *,rowid AS position FROM turns WHERE id=?',(tid,)).fetchone()
            self.assertTrue(turn_card(db,row)['research_detail'])
        self.consent(False)
        with self.assertRaisesRegex(RuntimeErrorBase,'retrieved_notes_unavailable'):
            private_lookup.details(self.store,self.pid,self.sid,tid,'research')
