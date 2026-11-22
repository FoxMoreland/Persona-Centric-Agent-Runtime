"""Exercise the frozen desktop route with isolated numeric transport fixtures."""
import asyncio
import random
import unittest
from unittest.mock import patch

from persona_runtime import desktop_turn_retry as retry
from persona_runtime.chat import ModelError
from persona_runtime.models import identifier
from persona_runtime.workbench_timeline import page
from tests import test_private_reply_stage2 as ordinary
from tests import test_single_retrieval as retrieval
from tests import test_desktop_recovery as recovery


class OrdinaryDesktopRetry(unittest.IsolatedAsyncioTestCase):
    setUp=recovery.DesktopRecoveryTests.setUp
    reopen=recovery.DesktopRecoveryTests.reopen

    async def test_main_uses_frozen_request_and_retains_failed_attempt(self):
        packets=[]
        original=self.model.complete
        async def controlled(model,messages):
            packets.append(messages)
            if len(packets)==1:
                raise ModelError('provider_http_503',diagnostics={'http_status':503})
            return await original(model,messages)
        with patch.object(self.model,'complete',controlled):
            turn=await self.chat.submit('desktop','907',identifier(),self.sid,persona_id=self.pid)
            tid=turn['id']
            await self.chat.wait()
            card=next(t for t in page(self.store,self.pid,self.sid)['turns'] if t['id']==tid)
            retry.retry(self.store,self.pid,self.sid,tid,card['stage_retry']['version'],identifier())
            self.ingress.schedule_committed()
            await self.chat.wait()
        self.assertEqual(self.chat.get(tid)['status'],'completed',self.chat.get(tid)['error_code'])
        self.assertEqual(len(packets),2)
        self.assertEqual(packets[0],packets[1])
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM attempts WHERE turn_id=?',(tid,)).fetchone()[0],2)


class RetryTests(unittest.IsolatedAsyncioTestCase):
    setUp = ordinary.OrdinaryTests.setUp
    reopen = ordinary.OrdinaryTests.reopen
    submit = ordinary.OrdinaryTests.submit
    plan = ordinary.OrdinaryTests.plan
    paint = ordinary.OrdinaryTests.paint

    def card(self, tid):
        return next(t for t in page(self.store,self.pid,self.sid)['turns'] if t['id']==tid)

    async def test_random_stage_error_resumes_original_request(self):
        rng = random.Random(180)
        stages = ['J1','J2.action','J2.task','J2.stance','J3','J6.mode','writer']
        rng.shuffle(stages)
        for stage in stages:
            with self.subTest(stage=stage):
                choose, reply = self.providers.choose,self.providers.reply
                async def broken(node,*args):
                    if stage == node:
                        raise ModelError('provider_http_503', diagnostics={'http_status':503})
                    return await choose(node,*args)
                async def body(*args):
                    if stage=='writer':
                        raise ModelError('provider_http_503', diagnostics={'http_status':503})
                    return await reply(*args)
                with patch.object(self.providers,'choose',broken), patch.object(self.providers,'reply',body):
                    tid = await self.submit()
                current=self.card(tid)['stage_retry']
                self.assertEqual(current['reason'],'upstream_error')
                op=identifier()
                args=(self.store,self.pid,self.sid,tid,current['version'],op)
                self.assertEqual(retry.retry(*args),{'state':'accepted'})
                self.assertEqual(retry.retry(*args),{'state':'accepted'})
                self.reopen()
                await self.chat.retry(tid)
                uid, plan=self.plan(tid)
                for n in range(len(plan['items'])):
                    self.paint(uid,plan,n)
                self.assertNotIn('stage_retry',self.card(tid))

    async def test_no_response_is_visible_and_unknown_is_never_resent(self):
        release=asyncio.Event()
        entered=asyncio.Event()
        choose=self.providers.choose
        async def stalled(node,*args):
            if node=='J1':
                entered.set()
                await release.wait()
            return await choose(node,*args)
        with patch.object(retry,'FIRST_REPLY_SECONDS',0.02), patch.object(self.providers,'choose',stalled):
            turn=await self.chat.submit('desktop','707',identifier(),self.sid,persona_id=self.pid)
            tid=turn['id']
            await asyncio.wait_for(entered.wait(),2)
            await asyncio.sleep(.08)
            current=self.card(tid)['stage_retry']
            self.assertEqual(current['reason'],'upstream_no_reply')
            self.assertEqual(retry.retry(self.store,self.pid,self.sid,tid,current['version'],identifier()),
                             {'state':'awaiting_receipt'})
            release.set()
            await self.chat.wait()
        self.plan(tid)
        self.assertNotIn('stage_retry',self.card(tid))
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM private_reply_nodes WHERE turn_id=? AND node='J1'",(tid,)).fetchone()[0],1)

    async def test_content_before_deadline_allows_long_generation(self):
        choose=self.providers.choose
        async def slow(node,*args):
            if node=='J1':
                retry.content_received()
                await asyncio.sleep(.05)
            return await choose(node,*args)
        with patch.object(retry,'FIRST_REPLY_SECONDS',.03), patch.object(self.providers,'choose',slow):
            tid=await self.submit()
        self.plan(tid)
        self.assertNotIn('stage_retry',self.card(tid))

    async def test_pending_click_continues_after_definitive_late_error(self):
        entered,release=asyncio.Event(),asyncio.Event()
        async def delayed(*args):
            entered.set()
            await release.wait()
            raise ModelError('provider_http_503',diagnostics={'http_status':503})
        with patch.object(retry,'FIRST_REPLY_SECONDS',.02), patch.object(self.providers,'reply',delayed):
            tid=(await self.chat.submit('desktop','707',identifier(),self.sid,persona_id=self.pid))['id']
            await asyncio.wait_for(entered.wait(),3)
            await asyncio.sleep(.08)
            version=self.card(tid)['stage_retry']['version']
            args=(self.store,self.pid,self.sid,tid,version,identifier())
            self.assertEqual(retry.retry(*args),{'state':'awaiting_receipt'})
            release.set()
            await self.chat.wait()
        self.assertTrue(retry.reconcile_clicks(self.store))
        self.assertFalse(retry.reconcile_clicks(self.store))
        await self.chat.retry(tid)
        self.plan(tid)
        self.assertEqual(retry.retry(*args),{'state':'accepted'})

    async def test_parallel_terminal_errors_share_one_retry(self):
        choose=self.providers.choose
        async def reject(node,*args):
            if node in ('J2.action','J2.task'):
                raise ModelError('provider_http_503',diagnostics={'http_status':503})
            return await choose(node,*args)
        with patch.object(self.providers,'choose',reject):
            tid=await self.submit()
        version=self.card(tid)['stage_retry']['version']
        retry.retry(self.store,self.pid,self.sid,tid,version,identifier())
        await self.chat.retry(tid)
        self.plan(tid)
        with self.store.connection() as db:
            retried=db.execute("""SELECT count(*) FROM desktop_stage_attempts
                WHERE turn_id=? AND archived IS NOT NULL AND replacement IS NOT NULL""",(tid,)).fetchone()[0]
            self.assertEqual(retried,2)

    async def test_unsent_deadline_restart_and_safe_retry(self):
        from persona_runtime.ingress import IngressService
        from persona_runtime.models import RuntimeErrorBase
        with patch.object(self.providers,'choose',side_effect=ModelError('provider_network_error',
                          diagnostics={'request_headers_sent':False})):
            tid=await self.submit()
        with self.store.connection() as db:
            row=db.execute('SELECT * FROM desktop_stage_attempts WHERE turn_id=?',(tid,)).fetchone()
            turn=db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone()
            self.assertIsNone(retry.failure(db,turn,row['started']+180))
            self.assertEqual(retry.failure(db,turn,row['started']+180.001)['reason'],'upstream_no_reply')
        self.reopen()
        with patch.object(retry.time,'time',return_value=row['started']+181):
            retry.expire(self.store)
        state=self.card(tid)['stage_retry']
        with self.assertRaises(RuntimeErrorBase):
            retry.retry(self.store,self.pid,'0',tid,state['version'],identifier())
        retry.retry(self.store,self.pid,self.sid,tid,state['version'],identifier())
        IngressService(self.chat).network_wake()
        await self.chat.retry(tid)
        self.plan(tid)

    async def test_withdrawn_consent_rejects_retry_without_dispatch(self):
        from persona_runtime.models import RuntimeErrorBase
        ordinary.OrdinaryTests.consent(self,True)
        with patch.object(self.providers,'reply',return_value=('',{})):
            tid=await self.submit()
        version=self.card(tid)['stage_retry']['version']
        ordinary.OrdinaryTests.consent(self,False)
        with self.assertRaises(RuntimeErrorBase):
            retry.retry(self.store,self.pid,self.sid,tid,version,identifier())
        with self.store.connection() as db:
            self.assertIsNone(db.execute('SELECT operation FROM desktop_stage_attempts WHERE attempt_id=?',(version,)).fetchone()[0])

    async def test_workbench_service_action_dispatches_original_turn(self):
        from aiohttp.test_utils import TestClient, TestServer
        from persona_runtime.server import create_app
        with patch.object(self.providers,'reply',return_value=('',{})):
            tid=await self.submit()
        version=self.card(tid)['stage_retry']['version']
        app=create_app(self.paths,private_chat_providers=self.providers,foreground_only=True)
        async with TestClient(TestServer(app)) as client:
            request={'action':'desktop.stage.retry','args':{'pid':self.pid,'sid':self.sid,'tid':tid,
                                                            'expected':version,'op':identifier()}}
            forbidden=await client.post('/v1/manage',json=request,
                        headers={'Authorization':'Bearer '+self.config.token('bridge')})
            self.assertEqual(forbidden.status,403)
            accepted=await client.post('/v1/manage',json=request,
                        headers={'Authorization':'Bearer '+self.config.token()})
            self.assertEqual(await accepted.json(),{'state':'accepted'})
            await app['engine'].wait()
            self.plan(tid)

    async def test_batch_retry_preserves_members_and_later_buffer(self):
        from persona_runtime.ingress import IngressService
        ingress=IngressService(self.chat)
        policy=ingress.current_policy(self.pid,'desktop')
        ingress.update_policy(self.pid,'desktop','natural_batch',policy['id'],identifier(),manual_confirmation=True)
        def add(text):
            return ingress.accept({'persona_id':self.pid,'session_id':self.sid,'channel':'desktop',
                                   'text':text,'request_id':text})
        add('941')
        add('942')
        with self.store.connection() as db:
            bid, revision=db.execute("SELECT id,revision FROM input_batches WHERE state='interrupted_buffer'").fetchone()
        with patch.object(self.providers,'reply',return_value=('',{})):
            ingress.action(bid,'confirm',revision,identifier(),self.pid,self.sid)
            ingress.schedule_committed()
            await self.chat.wait()
        tid=ingress.lookup('desktop','941')['turn_id']
        self.assertEqual(ingress.lookup('desktop','942')['turn_id'],tid)
        add('943')
        version=self.card(tid)['stage_retry']['version']
        op=identifier()
        self.assertEqual(retry.retry(self.store,self.pid,self.sid,tid,version,op),{'state':'accepted'})
        self.assertEqual(retry.retry(self.store,self.pid,self.sid,tid,version,identifier()),{'state':'accepted'})
        ingress.schedule_committed()
        await self.chat.wait()
        self.plan(tid)
        self.assertEqual([m['text'] for m in self.card(tid)['input_members']],['941','942'])
        self.assertFalse(ingress.lookup('desktop','943').get('turn_id'))


class RetrievalRetry(unittest.IsolatedAsyncioTestCase):
    setUp=retrieval.SingleRetrieval.setUp
    submit=retrieval.SingleRetrieval.submit
    reopen=retrieval.SingleRetrieval.reopen
    consent=retrieval.SingleRetrieval.consent
    grant=retrieval.SingleRetrieval.grant
    show=retrieval.SingleRetrieval.show
    paint=retrieval.SingleRetrieval.paint
    continue_turn=retrieval.SingleRetrieval.continue_turn
    enable_egress=retrieval.SingleRetrieval.enable_egress
    seed_dialogue=retrieval.SingleRetrieval.seed_dialogue

    async def test_egress_error_blocks_dispatch_then_retries_same_scope(self):
        self.queries=['721']
        self.answers={'721':'721 711'}
        await self.seed_dialogue()
        failed=True
        async def judge(aid,state):
            if failed:
                raise ModelError('provider_http_503',diagnostics={'http_status':503})
            return {'items':[{'id':1,'result':'allow'}]},{}
        self.enable_egress(judge)
        tid=await self.submit('701')
        self.assertFalse(self.tools)
        card=next(t for t in page(self.store,self.pid,self.sid)['turns'] if t['id']==tid)
        failed=False
        retry.retry(self.store,self.pid,self.sid,tid,card['stage_retry']['version'],identifier())
        await self.continue_turn(tid)
        self.show(tid,'search_notice')
        await self.continue_turn(tid)
        self.show(tid,'main')
        self.assertEqual(self.tools.count(('native','721')),1)

    async def test_cold_search_resumes_failed_item_with_saved_notice(self):
        self.answers={'701':'701 711','702':'702 712'}
        original=self.providers.retrieve
        async def fail_second(step,aid,query,model):
            if step=='native' and query['question']=='702':
                raise ModelError('provider_http_503',diagnostics={'http_status':503})
            return await original(step,aid,query,model)
        with patch.object(self.providers,'retrieve',fail_second):
            tid=await self.submit('701 702')
            self.show(tid,'search_notice')
            await self.continue_turn(tid)
        card=next(t for t in page(self.store,self.pid,self.sid)['turns'] if t['id']==tid)
        retry.retry(self.store,self.pid,self.sid,tid,card['stage_retry']['version'],identifier())
        self.reopen()
        await self.continue_turn(tid)
        self.show(tid,'main')
        self.assertEqual(self.tools.count(('native','701')),1)
        self.assertEqual(self.tools.count(('native','702')),1)
        self.assertEqual(self.tools.count(('search','702')),1)

    async def test_search_notice_and_final_body_resume_without_repeating_tools(self):
        self.queries=['701']
        self.answers={'701':'701 711'}
        fail={'phase':'search_notice'}
        original=self.providers.reply
        async def writer(aid,packet):
            if packet['phase']==fail['phase']:
                raise ModelError('provider_http_503',diagnostics={'http_status':503})
            return await original(aid,packet)
        with patch.object(self.providers,'reply',writer):
            tid=await self.submit('701')
            for phase in ('search_notice','main'):
                card=next(t for t in page(self.store,self.pid,self.sid)['turns'] if t['id']==tid)
                self.assertEqual(card['stage_retry']['reason'],'upstream_error')
                fail['phase']='main' if phase=='search_notice' else None
                retry.retry(self.store,self.pid,self.sid,tid,card['stage_retry']['version'],identifier())
                await self.continue_turn(tid)
                self.show(tid,phase)
                if phase=='search_notice':
                    await self.continue_turn(tid)
        self.assertEqual(self.tools,[('native','701'),('search','701')])

    async def test_translation_and_readback_recover_in_original_round(self):
        stages=['translate','assess']
        random.Random(180).shuffle(stages)
        for n,stage in enumerate(stages):
            with self.subTest(stage=stage):
                self.queries=[str(801+n)]
                self.answers={self.queries[0]:self.queries[0]}
                original=self.providers.bind_retrieval
                async def failed(aid):
                    raise ModelError('provider_http_503',diagnostics={'http_status':503})
                def bind(base,state,phase):
                    if phase==stage:
                        return failed,{}
                    return original(base,state,phase)
                with patch.object(self.providers,'bind_retrieval',bind):
                    tid=await self.submit(self.queries[0])
                    if stage=='assess':
                        self.show(tid,'search_notice')
                        await self.continue_turn(tid)
                card=next(t for t in page(self.store,self.pid,self.sid)['turns'] if t['id']==tid)
                retry.retry(self.store,self.pid,self.sid,tid,card['stage_retry']['version'],identifier())
                await self.continue_turn(tid)
                if stage=='translate':
                    self.show(tid,'search_notice')
                    await self.continue_turn(tid)
                self.show(tid,'main')
                self.assertEqual(self.tools.count(('native',self.queries[0])),1)
