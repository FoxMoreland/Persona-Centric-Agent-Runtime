"""Ordinary candidate integration uses opaque numeric source material and isolated transports."""
import asyncio
import json
import hashlib
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from persona_runtime.chat import ChatEngine
from persona_runtime.config import ConfigStore
from persona_runtime.models import PersonaSpec, RuntimeErrorBase, identifier
from persona_runtime.paths import RuntimePaths
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store
from persona_runtime import session_privacy
from persona_runtime.expression_utterance_publication import SpeechPublication
from persona_runtime.import_memory_scope import source_group
from persona_runtime.archive import turn_scope


class LocalProviders:
    version='2'
    def __init__(self):
        self.calls=[]
        self.bodies=[]
        self.selection='recent'
        self.wait=False
        self.target=None
        self.body='801。'*45

    async def choose(self,node,attempt,state,options):
        self.calls.append((node,attempt,state))
        values={'J1':'wait' if self.wait else self.target or 'batch','J2.action':'respond','J2.task':'closed','J2.style':'v2',
                'J2.stance':'0','J3':self.selection,'J6.mode':'chat','J6.coverage':'all','J6.ending':'stop'}
        return values[node],{'total_tokens':1}

    async def reply(self,attempt,packet):
        self.bodies.append((attempt,packet))
        return self.body,{'total_tokens':1}


class OrdinaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.paths=RuntimePaths(Path(temp.name))
        self.config=ConfigStore(self.paths);self.config.initialize()
        self.config.update({'model':{'base_url':'https://api.deepseek.com','name':'deepseek-v4-pro','api_key':'702'}})
        self.store=Store(self.paths);self.store.initialize()
        service=RuntimeService(self.store)
        self.pid=service.create_persona(PersonaSpec(1,'700','701',['702'],'703',['704'],'705'))['id']
        service.use_persona(self.pid)
        args=(self.store,self.config,'jev','https://example.invalid/v1','jev-1.14','706')
        preview=session_privacy.connection(*args)
        session_privacy.connection(*args,confirm=preview['confirmation'])
        self.providers=LocalProviders()
        self.reopen()
        self.sid=self.chat.session()['session_id']

    def reopen(self):
        self.chat=ChatEngine(self.store,self.config,private_chat_providers=self.providers)
        self.out=SpeechPublication(self.chat.expression.outlet)

    async def submit(self,text='707'):
        turn=await self.chat.submit('desktop',text,identifier(),self.sid,persona_id=self.pid)
        await self.chat.wait()
        return turn['id']

    def plan(self,tid):
        units=self.out.list(self.pid,self.sid,tid)
        self.assertEqual(len(units),1,self.chat.get(tid))
        uid=units[0]['id']
        return uid,self.out.plan(self.pid,self.sid,uid)

    def paint(self,uid,plan,index,state='presented'):
        rid=identifier();presenter='webview-v2.'+rid
        item=self.out.offer(self.pid,self.sid,uid,plan['id'],plan['revision'],index,presenter)
        self.assertLessEqual(len(item['body']),75)
        self.out.acknowledge(self.pid,self.sid,uid,item['id'],item['lease_id'],presenter,rid,item['body_hash'],
                             state,webview=True,publication_id=plan['id'],revision=plan['revision'],ordinal=index)
        return item['body']

    def configure_native_qq(self, *, consent=True):
        from persona_runtime.config import atomic_json
        from persona_runtime.ingress import IngressService
        from persona_runtime.sessions import select

        self.config.update({'qq_backend': 'snowluma', 'bot_qq': '456', 'owner_qq': '123'})
        cfg = self.config.load()
        cfg['runtime']['qq_persona_id'] = self.pid
        atomic_json(self.config.file, cfg)
        atomic_json(self.paths.home/'private/qq-native-reply.json',
                    {'instance': cfg['instance_id'], 'persona': self.pid, 'bot': '456', 'peer': '123'})
        with self.store.connection(write=True) as db:
            sid = select(db,self.pid,'qq',origin={'bot': '456', 'sender': '123'})
        if consent:
            state = session_privacy.status(self.store,self.config,self.pid,sid)
            session_privacy.update(self.store,self.config,self.pid,sid,True,state['revision'],
                                   state['recipient_hash'],identifier(),session_privacy.PROTOCOL)
        self.qq_clock = 1_000.0
        self.qq_ingress = IngressService(self.chat,monotonic=lambda: self.qq_clock)
        return sid

    async def submit_native_qq(self, sid):
        request_id = 'qq-'+identifier()
        receipt = self.qq_ingress.accept({'channel': 'qq', 'persona_id': self.pid, 'session_id': sid,
            'bot': '456', 'sender': '123', 'text': '707', 'request_id': request_id,
            'platform_message_id': identifier()})
        self.assertIsNone(receipt['turn_id'])
        self.qq_clock += 6
        self.qq_ingress.tick()
        await self.chat.wait()
        return self.qq_ingress.lookup('qq',request_id)['turn_id']

    async def test_native_qq_delivery_projects_only_platform_receipts_and_reuses_history(self):
        from persona_runtime.archive import resolve, scope
        from persona_runtime.archive_corpus import capture_connection
        from persona_runtime.archive_query import Budget
        from persona_runtime.qq_bridge import QQBridge
        from persona_runtime.qq_native_reply import QQNativeDelivery
        sid = self.configure_native_qq()
        self.providers.body = '801。'
        tid = await self.submit_native_qq(sid)
        self.assertEqual(len(self.providers.bodies), 1, self.chat.get(tid))
        self.assertIsNone(self.chat.get(tid)['reply_event_id'])
        uid = self.out.list(self.pid,sid,tid)[0]['id']
        with self.store.connection() as db:
            plan = db.execute('SELECT id,revision FROM expression_speech_publications WHERE utterance_id=?',(uid,)).fetchone()
        with self.assertRaisesRegex(RuntimeErrorBase,'speech_presentation_scope_mismatch'):
            self.out.offer(self.pid,sid,uid,*plan,0,identifier())

        queue = QQNativeDelivery(self.store,self.config,self.out)
        bridge = QQBridge(self.paths)

        async def request(method,path,value):
            if path == '/v2/qq-native/next':
                return queue.next(value['presenter'])
            return queue.result(value)

        bridge.request = request
        sent = []

        async def send(body):
            sent.append(body)
            return {'data': {'message_id': -701}}

        self.assertTrue(await bridge.deliver_native(send,self.config.load()))
        self.assertFalse(await bridge.deliver_native(send,self.config.load()))
        self.assertEqual(sent,['801。'])
        self.assertEqual(self.chat.get(tid)['status'],'completed')
        with self.store.connection() as db:
            target = {'kind':'platform_message','message_id':'-701'}
            self.assertEqual(resolve(db,scope(self.pid,'qq','456','123'),target)['replies'][0]['text'],'801。')
            self.assertEqual(resolve(db,scope(self.pid,'desktop'),target)['status'],'not_found')
            spec = {'scope':scope(self.pid,'qq','456','123'), 'session_id':sid, 'window':'recent_50'}
            result = capture_connection(db,spec,Budget())
            self.assertEqual([row['text'] for row in result['documents'] if row['source_kind']=='qq_sent_part'],['801。'])
        await self.submit_native_qq(sid)
        self.assertIn('801。',[row['text'] for row in self.providers.bodies[-1][1]['dialogue']])

    async def test_native_qq_lost_offer_is_never_replayed(self):
        from persona_runtime.qq_native_reply import QQNativeDelivery
        sid = self.configure_native_qq()
        self.providers.body = '801。'
        tid = await self.submit_native_qq(sid)
        queue = QQNativeDelivery(self.store,self.config,self.out)
        lease = queue.next('301')
        self.assertEqual(lease['state'],'offered',self.chat.get(tid))
        self.reopen()
        self.chat.recover()
        queue = QQNativeDelivery(self.store,self.config,self.out)
        self.assertEqual(queue.next('302'), {'state':'unsettled'})
        receipt = {k: lease[k] for k in ('utterance_id','id','lease_id','body_hash')}
        receipt.update(presenter='301',state='unknown')
        self.assertEqual(queue.result(receipt)['state'],'unknown')
        self.assertTrue(queue.result(receipt)['already_applied'])
        self.assertIsNone(self.chat.get(tid)['reply_event_id'])
        self.assertEqual(queue.next('302'), {'state':'unsettled'})

    async def test_native_qq_ack_response_loss_replays_only_the_local_receipt(self):
        from persona_runtime.qq_native_reply import QQNativeDelivery
        from persona_runtime.qq_bridge import QQBridge
        sid = self.configure_native_qq()
        self.providers.body = '801。'
        tid = await self.submit_native_qq(sid)
        queue = QQNativeDelivery(self.store,self.config,self.out)
        sends = []
        lost = True

        async def request(method,path,value):
            nonlocal lost
            if path.endswith('/next'):
                return queue.next(value['presenter'])
            result = queue.result(value)
            if lost:
                lost = False
                raise TimeoutError
            return result

        async def send(body):
            sends.append(body)
            return {'message_id': -701}

        bridge = QQBridge(self.paths)
        bridge.request = request
        with self.assertRaises(TimeoutError):
            await bridge.deliver_native(send,self.config.load())
        self.assertTrue(bridge.native_ack_path.is_file())
        restarted = QQBridge(self.paths)
        restarted.request = request
        self.assertFalse(await restarted.deliver_native(send,self.config.load()))
        self.assertFalse(restarted.native_ack_path.exists())
        self.assertEqual(sends, ['801。'])
        self.assertEqual(self.chat.get(tid)['status'], 'completed')

    async def test_native_qq_without_session_consent_never_calls_provider(self):
        from persona_runtime.qq_native_reply import QQNativeDelivery
        sid = self.configure_native_qq(consent=False)
        tid = await self.submit_native_qq(sid)
        self.assertEqual(self.chat.get(tid)['error_code'],'private_cloud_provider_not_authorized')
        self.assertEqual(self.providers.calls,[])
        self.assertEqual(QQNativeDelivery(self.store,self.config,self.out).next('301'), {'state':'empty'})

    async def test_full_local_qq_runs_reviewed_route_and_keeps_knowledge_disabled(self):
        from persona_runtime.config import atomic_json
        from persona_runtime.organization import OrganizationEngine
        from persona_runtime.qq_native_reply import FULL_LOCAL_FILE, QQNativeDelivery
        from persona_runtime.private_reply_entry import RESEARCH
        from persona_runtime import private_lookup
        from tests.test_m8d import Model

        self.providers.research = True
        self.reopen()
        self.config.update({'organization':{'reuse_chat':False,'model':{
            'base_url':'https://api.deepseek.com','name':'deepseek-flash','api_key':'709'}}})
        sid = self.configure_native_qq(consent=False)
        cfg = self.config.load()
        marker = self.paths.home/FULL_LOCAL_FILE
        atomic_json(marker,{'scope':'bound_private_only','web_lookup':False,'allow_pro_only':False})
        payload = {'channel':'qq','persona_id':self.pid,'session_id':sid,'bot':'456','sender':'123',
                   'text':'751','request_id':'qq-'+identifier(),'platform_message_id':identifier()}
        with self.assertRaisesRegex(RuntimeErrorBase,'qq_full_local_route_not_ready'):
            self.qq_ingress.accept(payload)
        atomic_json(marker,{'state':'ready','scope':'bound_private_only','web_lookup':False,'knowledge_sync':False,
                           'allow_pro_only':False,'instance':cfg['instance_id'],'persona':self.pid,
                           'bot':'456','peer':'123','session':sid})
        state=session_privacy.status(self.store,self.config,self.pid,sid)
        state=session_privacy.update(self.store,self.config,self.pid,sid,True,state['revision'],
                                     state['recipient_hash'],identifier(),session_privacy.PROTOCOL)
        self.assertEqual(set(state['recipients']),{'main','jev','organization'})
        memory=Model()
        org=OrganizationEngine(self.store,self.config,memory,self.chat)
        policy=org.managed.current_policy(self.pid)
        org.managed.update_policy(self.pid,policy['data']|{'memory_automation':True},policy['id'],self.pid,identifier())
        with patch.object(private_lookup,'prepare',side_effect=AssertionError('knowledge lookup must stay disabled')):
            self.providers.body='801。'
            tid=await self.submit_native_qq(sid)
        self.assertEqual(len(self.providers.bodies),1)
        self.assertGreater(len(self.providers.calls),0)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT route FROM private_reply_turns WHERE turn_id=?',(tid,)).fetchone()[0],RESEARCH)
            self.assertEqual(db.execute('SELECT count(*) FROM private_lookup_intents WHERE turn_id=?',(tid,)).fetchone()[0],0)
        queue=QQNativeDelivery(self.store,self.config,self.out)
        offer=queue.next('301')
        receipt={key:offer[key] for key in ('utterance_id','id','lease_id','body_hash')}
        receipt.update(presenter='301',state='presented',platform_message_id='801')
        queue.result(receipt)
        for _ in range(5):
            org.automatic.tick(force=True)
            await org.run_next()
            if len(memory.calls)>=2:
                break
        with self.store.connection() as db:
            outcomes=[tuple(row) for row in db.execute('SELECT status,error_code FROM organization_tasks')]
            coverage=[tuple(row) for row in db.execute('SELECT disposition,summary_state FROM memory_coverage')]
            self.assertEqual(len(memory.calls),2,(outcomes,coverage))
            self.assertGreater(db.execute("SELECT count(*) FROM memories WHERE status='active'").fetchone()[0],0)
            for row in db.execute('SELECT payload_json FROM organization_tasks'):
                task=json.loads(row[0])
                self.assertEqual(task['channel'],'qq')
                self.assertEqual(task['automatic_memory']['session_privacy_revision'],state['revision'])

    async def test_native_qq_confirmed_opening_is_only_assistant_history_for_next_input(self):
        from persona_runtime.qq_opening_archive import record_confirmed
        sid = self.configure_native_qq()
        eid = record_confirmed(self.store,self.config,self.pid,sid,'711','901')
        self.assertEqual(record_confirmed(self.store,self.config,self.pid,sid,'711','901'),eid)
        await self.submit_native_qq(sid)
        dialogue = self.providers.bodies[-1][1]['dialogue']
        self.assertEqual([(x['role'],x['text']) for x in dialogue],[('assistant','711')])
        with self.store.connection() as db:
            rows = db.execute('SELECT role FROM events WHERE session_id=? ORDER BY seq',(sid,)).fetchall()
            self.assertEqual([row[0] for row in rows],['persona','user'])

    async def test_native_qq_fixed_window_does_not_slide_after_the_second_message(self):
        from persona_runtime.archive import encoded
        from persona_runtime.qq_native_reply import FIXED_WINDOW_PREFIX
        sid = self.configure_native_qq()
        with self.store.connection(write=True) as db:
            db.execute('INSERT INTO knowledge_control VALUES (?,?)',(FIXED_WINDOW_PREFIX+sid,encoded(8000)))
        base = {'channel':'qq','persona_id':self.pid,'session_id':sid,'bot':'456','sender':'123','text':'707'}
        first=self.qq_ingress.accept(base|{'request_id':'qq-811','platform_message_id':'811'})
        self.qq_clock += 7
        second=self.qq_ingress.accept(base|{'request_id':'qq-812','platform_message_id':'812'})
        self.assertEqual(first['batch_id'],second['batch_id'])
        self.qq_clock += .999
        self.qq_ingress.tick()
        self.assertIsNone(self.qq_ingress.lookup('qq','qq-812')['turn_id'])
        self.qq_clock += .002
        self.qq_ingress.tick()
        await self.chat.wait()
        self.assertEqual(self.qq_ingress.lookup('qq','qq-811')['turn_id'],self.qq_ingress.lookup('qq','qq-812')['turn_id'])
        self.assertEqual(len(self.providers.bodies),1)
        with self.store.connection() as db:
            row=db.execute('SELECT first_epoch,deadline_epoch FROM input_batches WHERE id=?',(first['batch_id'],)).fetchone()
            self.assertAlmostEqual(row['deadline_epoch']-row['first_epoch'],8,places=3)

    async def test_native_qq_pro_test_uses_the_main_provider_only_and_preserves_opening(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from persona_runtime.archive import fingerprint
        from persona_runtime.config import atomic_json
        from persona_runtime.qq_opening_archive import record_confirmed

        sid = self.configure_native_qq(consent=False)
        cfg = self.config.load()
        atomic_json(self.paths.home/'private/qq-pro-test.json',{'instance':cfg['instance_id'],'persona':self.pid,
                    'bot':'456','peer':'123','session':sid,'model':fingerprint(cfg['model'])})
        state = session_privacy.status(self.store,self.config,self.pid,sid)
        session_privacy.update(self.store,self.config,self.pid,sid,True,state['revision'],state['recipient_hash'],
                               identifier(),session_privacy.PROTOCOL)
        record_confirmed(self.store,self.config,self.pid,sid,'711','901')
        complete = AsyncMock(return_value=('801。',{'total_tokens':1}))
        self.chat.client = SimpleNamespace(complete=complete)
        tid = await self.submit_native_qq(sid)
        self.assertEqual(self.chat.get(tid)['status'],'completed',self.chat.get(tid))
        self.assertEqual(self.providers.calls,[])
        complete.assert_awaited_once()
        model,messages = complete.await_args.args
        self.assertEqual(model['name'],'deepseek-v4-pro')
        self.assertIn({'role':'assistant','content':'711'},messages)

    async def test_native_qq_explicit_memory_sharing_is_bidirectional_and_revocable(self):
        from persona_runtime.qq_native_reply import QQNativeDelivery
        from persona_runtime.qq_shared_memory import update
        qq = self.configure_native_qq()
        desktop = session_privacy.status(self.store,self.config,self.pid,self.sid)
        session_privacy.update(self.store,self.config,self.pid,self.sid,True,desktop['revision'],
                               desktop['recipient_hash'],identifier(),session_privacy.PROTOCOL)
        self.providers.body = '801。'
        service = RuntimeService(self.store)
        first = await self.submit('731')
        uid, plan = self.plan(first)
        self.paint(uid,plan,0)
        record = service.add_memory('user_fact','941',self.chat.get(first)['user_event_id'])
        update(self.store,self.config,self.pid,record['id'],qq,True)
        self.providers.selection = 'memory'
        qt = await self.submit_native_qq(qq)
        packet = self.providers.bodies[-1][1]
        self.assertIn('941',[row['text'] for row in packet['evidence']])
        self.assertNotIn('731',[row['text'] for row in packet['dialogue']])
        queue = QQNativeDelivery(self.store,self.config,self.out)
        offer = queue.next('301')
        receipt = {key:offer[key] for key in ('utterance_id','id','lease_id','body_hash')}
        receipt.update(presenter='301',state='presented',platform_message_id='801')
        queue.result(receipt)
        reverse = service.add_memory('user_fact','942',self.chat.get(qt)['user_event_id'])
        update(self.store,self.config,self.pid,reverse['id'],self.sid,True)
        dt = await self.submit('732')
        packet = self.providers.bodies[-1][1]
        self.assertIn('942',[row['text'] for row in packet['evidence']])
        self.assertNotIn('707',[row['text'] for row in packet['dialogue']])
        uid, plan = self.plan(dt)
        self.paint(uid,plan,0)
        update(self.store,self.config,self.pid,reverse['id'],self.sid,False)
        self.providers.selection = 'recent'
        await self.submit('733')
        self.assertNotIn('942',[row['text'] for row in self.providers.bodies[-1][1]['evidence']])

    async def test_native_qq_recall_only_withdraws_own_confirmed_output_and_derivatives(self):
        from persona_runtime.archive import resolve, scope
        from persona_runtime.qq_native_reply import QQNativeDelivery
        from persona_runtime.qq_recall_notice import apply
        qq = self.configure_native_qq()
        self.providers.body = '801。'
        tid = await self.submit_native_qq(qq)
        queue = QQNativeDelivery(self.store,self.config,self.out)
        offer = queue.next('301')
        receipt = {key:offer[key] for key in ('utterance_id','id','lease_id','body_hash')}
        receipt.update(presenter='301',state='presented',platform_message_id='801')
        queue.result(receipt)
        view = self.chat.get(tid)
        service = RuntimeService(self.store)
        dependent = service.add_memory('shared_experience','941',view['reply_event_id'])
        independent = service.add_memory('user_fact','942',view['user_event_id'])
        with self.store.connection() as db:
            incoming = db.execute('SELECT platform_message_id FROM raw_events WHERE event_id=?',
                                  (view['user_event_id'],)).fetchone()[0]
        notice = {'bot':'456','peer':'123','message_id':incoming}
        self.assertEqual(apply(self.store,self.config,notice), {'state':'ignored'})
        self.assertEqual(self.chat.get(tid)['status'],'completed')
        notice['message_id'] = '801'
        self.assertEqual(apply(self.store,self.config,notice),{'state':'withdrawn','already_applied':False})
        self.assertEqual(apply(self.store,self.config,notice),{'state':'withdrawn','already_applied':True})
        with self.store.connection() as db:
            self.assertEqual(resolve(db,scope(self.pid,'qq','456','123'),
                                     {'kind':'platform_message','message_id':'801'})['status'],'unavailable')
            audit = resolve(db,scope(self.pid,'qq','456','123'),
                            {'kind':'platform_message','message_id':'801'},audit=True)
            self.assertEqual(audit['replies'],[])
            self.assertEqual(db.execute('SELECT text FROM events WHERE id=?',(view['reply_event_id'],)).fetchone()[0], '')
            self.assertEqual(db.execute('SELECT text FROM events WHERE id=?',(view['user_event_id'],)).fetchone()[0], '707')
            self.assertEqual(db.execute('SELECT status FROM memories WHERE id=?',(dependent['id'],)).fetchone()[0],'retracted')
            self.assertEqual(db.execute('SELECT status FROM memories WHERE id=?',(independent['id'],)).fetchone()[0],'active')

    async def test_native_qq_http_requires_bridge_and_acknowledges_only_the_leased_fragment(self):
        from aiohttp.test_utils import TestClient, TestServer
        from persona_runtime.server import create_app
        sid = self.configure_native_qq()
        tid = await self.submit_native_qq(sid)
        self.assertGreater(len(self.out.list(self.pid,sid,tid)), 0)
        bridge_headers = {'Authorization': 'Bearer '+self.config.token('bridge')}
        admin_headers = {'Authorization': 'Bearer '+self.config.token()}
        app = create_app(self.paths,engine_factory=lambda *args, **kwargs: self.chat,foreground_only=True)
        async with TestClient(TestServer(app)) as client:
            url = '/v2/qq-native/next'
            response = await client.post(url,json={'presenter':'301'},headers=admin_headers)
            self.assertEqual(response.status, 403)
            first = await client.post(url,json={'presenter':'301'},headers=bridge_headers)
            offered = await first.json()
            self.assertEqual(offered['state'],'offered')
            blocked = await client.post(url,json={'presenter':'302'},headers=bridge_headers)
            self.assertEqual(await blocked.json(),{'state':'unsettled'})
            receipt = {key: offered[key] for key in ('utterance_id','id','lease_id','body_hash')}
            receipt.update(presenter='302',state='presented',platform_message_id='100')
            ack_url = '/v2/qq-native/result'
            denied = await client.post(ack_url,json=receipt,headers=bridge_headers)
            self.assertEqual(denied.status,400)
            receipt['presenter'] = '301'
            accepted = await client.post(ack_url,json=receipt,headers=bridge_headers)
            self.assertEqual((await accepted.json())['state'],'presented')
            replay = await client.post(ack_url,json=receipt,headers=bridge_headers)
            self.assertTrue((await replay.json())['already_applied'])
            receipt['platform_message_id'] = '101'
            conflict = await client.post(ack_url,json=receipt,headers=bridge_headers)
            self.assertEqual(conflict.status,400)
        self.assertIsNone(self.chat.get(tid)['reply_event_id'])

    async def test_delivery_times_follow_each_confirmed_fragment_after_reopen(self):
        from persona_runtime import message_time
        from persona_runtime.ingress import IngressService
        from persona_runtime.workbench_timeline import page
        ingress = IngressService(self.chat)
        policy = ingress.current_policy(self.pid, 'desktop')
        ingress.update_policy(self.pid,'desktop',policy['mode'],policy['id'],identifier(),
                              message_time_version=message_time.VERSION)
        tid = await self.submit()
        uid, plan = self.plan(tid)
        self.assertGreater(len(plan['items']), 1)
        for i in range(len(plan['items'])):
            self.paint(uid,plan,i)
        timeline = page(self.store,self.pid,self.sid)
        bubbles = timeline['turns'][-1]['desktop_publications'][0]['visible']
        expected = [m['presented_at'] for m in bubbles]
        self.reopen()
        await self.submit('799')
        packet = self.providers.bodies[-1][1]
        previous = next(m for m in packet['dialogue'] if m['role']!='user')
        times = previous['message_times']
        self.assertEqual([m['recorded_at'] for m in times], expected)
        self.assertEqual([previous['text'][m['start']:m['end']] for m in times],
                         [m['text'] for m in bubbles])
        with self.store.connection() as db:
            from persona_runtime.recall_runtime import render
            body = render([{'kind':'dialogue','source_type':'assistant_statement','source_id':previous['id'],
                            'text':previous['text']}], db, delivery_times=True)
            self.assertEqual(json.loads(body.splitlines()[-1])['message_times'], times)
            self.assertEqual(message_time.event_times(db,[previous['id']])[previous['id']][0]['receipt_id'],
                             bubbles[0]['receipt_id'])

    async def test_complete_visible_history_and_memory_source(self):
        tid=await self.submit()
        uid,plan=self.plan(tid)
        self.assertIsNone(self.chat.get(tid)['reply_event_id'])
        body=''.join(self.paint(uid,plan,i) for i in range(len(plan['items'])))
        self.assertEqual(body,self.providers.body)
        with self.store.connection() as db:
            turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone())
            data=source_group(db,turn_scope(db,turn),turn)
            self.assertEqual([r['text'] for r in data['replies']],[body])
        second=await self.submit('708')
        self.plan(second)
        packet=self.providers.bodies[-1][1]
        self.assertIn(body,[e['text'] for e in packet['evidence']])
        self.assertNotIn('tools',packet)

    async def test_cold_plan_starts_one_writer_and_never_reviews_body(self):
        tid=await self.submit()
        uid,plan=self.plan(tid)
        self.assertEqual(len(self.providers.bodies),1)
        self.assertIsNone(self.providers.bodies[0][1]['repair_reason'])
        self.assertNotIn('J7',[node for node,*_ in self.providers.calls])
        self.reopen()
        self.chat.recover()
        await self.chat.retry(tid)
        self.assertEqual(len(self.providers.bodies),1)
        self.assertEqual(self.plan(tid)[0],uid)
        for i in range(len(plan['items'])):self.paint(uid,plan,i)
        self.assertEqual(self.chat.get(tid)['status'],'completed')

    async def test_restart_consumes_saved_review_without_calling_again(self):
        from persona_runtime.ingress import IngressService
        with patch.object(self.chat.private_reply.ordinary,'publish',side_effect=asyncio.CancelledError()):
            tid=await self.submit()
        self.assertFalse(self.out.list(self.pid,self.sid,tid))
        before=len(self.providers.calls)
        self.reopen();self.chat.recover()
        IngressService(self.chat).schedule_committed()
        await self.chat.wait()
        uid,plan=self.plan(tid)
        self.assertEqual(len(self.providers.calls),before)
        self.assertEqual(len(self.providers.bodies),1)
        for i in range(len(plan['items'])):self.paint(uid,plan,i)

    async def test_incomplete_proof_cannot_dispatch_and_stale_source_stops_offer(self):
        tid=await self.submit()
        uid,plan=self.plan(tid)
        with self.store.connection(write=True) as db:
            root,base=self.chat.private_reply._binding(db,tid,'main')
            with self.assertRaisesRegex(RuntimeErrorBase,'reply_decision_required'):
                self.chat.private_reply.ordinary.extend_contract(db,root,base,{'draft':0})
            db.execute("INSERT INTO source_exclusions SELECT id,persona_id,user_id,'withdrawn',created_at FROM events WHERE id=?",
                       (root['user_event_id'],))
        offer=self.out.offer(self.pid,self.sid,uid,plan['id'],plan['revision'],0,'908')
        self.assertEqual(offer['state'],'stopped')
        self.assertIsNone(self.chat.get(tid)['reply'])

    async def test_retired_unknown_review_does_not_block_saved_body(self):
        from persona_runtime.ingress import IngressService
        with patch.object(self.chat.private_reply.ordinary,'publish',side_effect=asyncio.CancelledError()):
            tid=await self.submit()
        with self.store.connection(write=True) as db:
            db.execute("INSERT INTO private_reply_nodes VALUES(?,0,'J7',?,?,'unknown',NULL,NULL)",
                       (tid,identifier(),'archived'))
        before=len(self.providers.calls)
        self.reopen();self.chat.recover()
        IngressService(self.chat).schedule_committed()
        await self.chat.wait()
        uid,plan=self.plan(tid)
        self.assertEqual(len(self.providers.calls),before)
        self.assertEqual(len(self.providers.bodies),1)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT state FROM private_reply_nodes WHERE turn_id=? AND node='J7'",
                                        (tid,)).fetchone()[0],'unknown')
        for i in range(len(plan['items'])):self.paint(uid,plan,i)
        self.assertEqual(self.chat.get(tid)['status'],'completed')

    async def test_wait_skips_writer_and_review(self):
        self.providers.wait=True
        tid=await self.submit()
        self.assertEqual(self.chat.get(tid)['status'],'completed')
        self.assertEqual([c[0] for c in self.providers.calls],['J1'])
        self.assertFalse(self.providers.bodies)

    async def test_j2_freezes_a_reviewed_style_before_writing(self):
        self.providers.styles={self.pid:{'v2':'919'}}
        tid=await self.submit()
        self.plan(tid)
        self.assertEqual(self.providers.bodies[0][1]['style'],'919')
        with self.store.connection() as db:
            proof=json.loads(db.execute("SELECT result_json FROM private_reply_nodes WHERE node='handoff'").fetchone()[0])
            self.assertIn('J2.style',proof['node_receipts'])

    async def test_unknown_fragment_keeps_only_confirmed_prefix(self):
        tid=await self.submit()
        uid,plan=self.plan(tid)
        first=self.paint(uid,plan,0)
        self.paint(uid,plan,1,'unknown')
        self.assertEqual(self.chat.get(tid)['reply'],first)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM events WHERE role=\'persona\'').fetchone()[0],1)

    async def test_invalid_judge_and_missing_adapter(self):
        self.providers.selection='invalid'
        tid=await self.submit()
        self.assertFalse(self.providers.bodies)
        self.assertEqual(self.chat.get(tid)['error_code'],'reply_decision_required')
        self.chat=ChatEngine(self.store,self.config)
        await self.chat.retry(tid)
        self.assertEqual(self.chat.get(tid)['error_code'],'private_reply_transport_unavailable')
        self.assertFalse(self.out.list(self.pid,self.sid,tid))

    def consent(self,enable):
        state=session_privacy.status(self.store,self.config,self.pid,self.sid)
        return session_privacy.update(self.store,self.config,self.pid,self.sid,enable,state['revision'],
                                      state['recipient_hash'],identifier(),state['protocol'])

    async def test_imported_explicit_reference_under_session_consent(self):
        from persona_runtime.import_history import restore
        from persona_runtime.ingress import IngressService
        from persona_runtime.desktop_recovery import intent_binding
        self.consent(True)
        with self.store.connection(write=True) as db:
            restore(db,self.pid,'801','A',[{'id':'81','role':'B','text':'811','original':'811','date':'2025-01-01',
                    'speaker':'802','line':1,'start':0,'end':3}],meta={'version':2,'withdrawn':False,
                    'timezone_offset':480,'self_role':'B','providers':{},'organize':False})
            eid=db.execute("SELECT id FROM events WHERE id LIKE 'imported.%'").fetchone()[0]
        self.providers.target=eid
        ingress=IngressService(self.chat)
        ident=identifier()
        payload={'persona_id':self.pid,'session_id':self.sid,'channel':'desktop','text':'808','request_id':ident,
                 'reference':{'kind':'event','id':eid}}
        with self.store.connection() as db:intent_binding(db,self.store,self.config,payload)
        receipt=ingress.accept(payload)
        with self.store.connection() as db:batch=db.execute('SELECT revision,state FROM input_batches WHERE id=?',(receipt['batch_id'],)).fetchone()
        if batch['state']=='collecting':
            ingress.action(receipt['batch_id'],'seal',batch['revision'],identifier(),self.pid,self.sid)
        await self.chat.wait()
        tid=ingress.lookup('desktop',ident)['turn_id']
        uid,plan=self.plan(tid)
        self.assertEqual(self.providers.bodies[0][1]['targets'][0]['text'],'811')
        self.consent(False)
        offer=self.out.offer(self.pid,self.sid,uid,plan['id'],plan['revision'],0,'801')
        self.assertEqual(offer['state'],'stopped')

    async def test_automatic_memory_reviews_visible_source_and_is_used_next_turn(self):
        from tests.test_m8d import Model
        from persona_runtime.organization import OrganizationEngine
        self.config.update({'organization':{'reuse_chat':True}})
        self.consent(True)
        memory=Model()
        org=OrganizationEngine(self.store,self.config,memory,self.chat)
        policy=org.managed.current_policy(self.pid)
        org.managed.update_policy(self.pid,policy['data']|{'memory_automation':True},policy['id'],self.pid,identifier())
        tid=await self.submit('751')
        uid,plan=self.plan(tid)
        org.automatic.tick(force=True)
        self.assertFalse(await org.run_next())
        for i in range(len(plan['items'])):self.paint(uid,plan,i)
        for _ in range(5):
            org.automatic.tick(force=True)
            if not await org.run_next():break
        self.assertEqual(len(memory.calls),2)
        with self.store.connection() as db:
            facts=db.execute("SELECT id,text FROM memories WHERE status='active'").fetchall()
            self.assertEqual([f['text'] for f in facts],['751'])
            for row in db.execute('SELECT payload_json FROM organization_tasks'):
                p=json.loads(row[0])
                self.assertEqual(p['automatic_memory']['session_privacy_revision'],1)
                self.assertEqual([e['text'] for e in p['events'] if e['role']!='user'],[self.providers.body])
        self.providers.selection='memory'
        uid,plan=self.plan(await self.submit('752'))
        self.assertEqual(self.providers.bodies[-1][1]['evidence'][0]['id'],facts[0]['id'])
        for i in range(len(plan['items'])):self.paint(uid,plan,i)
        self.sid=self.chat.session(fresh=True)['session_id']
        self.plan(await self.submit('753'))
        self.assertEqual(self.providers.bodies[-1][1]['evidence'],[])
        self.assertEqual(self.providers.bodies[-1][1]['dialogue'],[])

    async def test_planner_unknown_is_not_replayed(self):
        original=self.providers.choose
        async def choose(node,*args):
            if node=='J3':
                raise TimeoutError()
            return await original(node,*args)
        self.providers.choose=choose
        tid=await self.submit()
        self.reopen();self.chat.recover()
        self.providers.choose=original
        await self.chat.retry(tid)
        self.assertFalse(self.providers.bodies)
        self.assertEqual(self.chat.get(tid)['error_code'],'reply_dispatch_unknown')
        self.assertFalse(self.out.list(self.pid,self.sid,tid))

    async def test_revoke_between_memory_extract_and_verify(self):
        from tests.test_m8d import Model
        from persona_runtime.organization import OrganizationEngine
        self.config.update({'organization':{'reuse_chat':True}})
        self.consent(True)
        memory=Model();org=OrganizationEngine(self.store,self.config,memory,self.chat)
        policy=org.managed.current_policy(self.pid)
        org.managed.update_policy(self.pid,policy['data']|{'memory_automation':True},policy['id'],self.pid,identifier())
        uid,plan=self.plan(await self.submit())
        for i in range(len(plan['items'])):self.paint(uid,plan,i)
        org.automatic.tick(force=True)
        await org.run_next()
        self.consent(False)
        await org.run_next()
        self.assertEqual(len(memory.calls),1)
        with self.store.connection() as db:
            self.assertFalse(db.execute("SELECT 1 FROM memories WHERE status='active'").fetchone())

    async def test_registered_native_adapter_uses_scoped_choices_and_plain_writer(self):
        from persona_runtime.ordinary_providers import ReviewedPrivateChatProviders,register,registered,retire
        nodes=('J1','J2.action','J2.stance','J2.task','J3','J6.mode','J6.coverage','J6.ending','J7')
        bundle={'protocol':'maibot-private-v2','scope':{'instance':self.config.load()['instance_id'],'personas':[self.pid]},
                'main':{'base_url':'https://api.deepseek.com','name':'deepseek-v4-pro'},
                'jev':{'base_url':'https://example.invalid/v1','name':'jev-1.14'},
                'reply_system':'801','nodes':{n:{'instructions':'802','criteria':{},'source_criterion':'803'} for n in nodes}}
        path=self.paths.home/'804.json';path.write_text(json.dumps(bundle),encoding='utf8')
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(RuntimeErrorBase,'review_version_changed'):
            register(self.config,path,'0'*64)
        register(self.config,path,digest)
        self.assertEqual(registered(self.config).version,digest)
        retire(self.config)
        with self.assertRaisesRegex(RuntimeErrorBase,'review_version_changed'):
            register(self.config,path,'0'*64)
        self.assertIsNone(registered(self.config))
        register(self.config,path,digest)
        self.assertEqual(registered(self.config).version,digest)
        native_calls=[]
        class Jev:
            def __init__(self,model):pass
            async def close(self):pass
            async def complete(self,wire,**callbacks):
                native_calls.append(wire)
                node,=wire['questions']
                criteria=wire['questions'][node]['criteria']
                choice=next(iter(criteria))
                return 200,json.dumps({'answers':{node:{'type':'choice','choice':choice,
                    'probabilities':{k:int(k==choice) for k in criteria}}},'usage':{'total_tokens':1}})
        packets=[]
        class Writer:
            async def complete(self,model,messages):
                packets.append(messages)
                return '805',{'total_tokens':1}
        provider=ReviewedPrivateChatProviders(self.config,path,digest,writer=Writer(),jev_factory=Jev)
        self.assertEqual((await provider.choose('J1','806',{'current':['807']},['batch','wait']))[0],'batch')
        self.assertEqual((await provider.reply('809',{'targets':['807']}))[0],'805')
        self.assertEqual(packets[0],[{'role':'system','content':'801'},{'role':'user','content':'{"targets":["807"]}'}])
        self.assertEqual(set(native_calls[0]['questions']),{'J1'})

    async def test_fresh_messages_stay_held_until_manual_confirmation(self):
        from persona_runtime.ingress import IngressService
        ingress=IngressService(self.chat)
        policy=ingress.current_policy(self.pid,'desktop')
        ingress.update_policy(self.pid,'desktop','natural_batch',policy['id'],identifier(),manual_confirmation=True)
        inp={'persona_id':self.pid,'session_id':self.sid,'channel':'desktop','text':'901','request_id':'911'}
        receipt=ingress.accept(inp);ingress.accept(inp|{'text':'902','request_id':'912'})
        self.assertFalse(self.providers.calls)
        with self.store.connection() as db:version=db.execute('SELECT revision FROM input_batches WHERE id=?',(receipt['batch_id'],)).fetchone()[0]
        args=(receipt['batch_id'],'confirm',version,identifier(),self.pid,self.sid)
        self.assertEqual(ingress.action(*args),ingress.action(*args))
        await self.chat.wait()
        self.assertEqual([row['text'] for row in self.providers.bodies[0][1]['targets']],['901','902'])
        self.assertEqual(len(self.providers.bodies),1)
