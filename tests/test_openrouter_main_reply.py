"""Offline tests for a separately reviewed desktop writer."""
import json
import unittest
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from persona_runtime import main_reply, session_privacy
from persona_runtime.chat import ChatCompletionsClient, ModelError
from persona_runtime.chat_stream import ChatStream
from persona_runtime.provider_compat import build_request, endpoint
from persona_runtime.provider_policy import require_generation_provider
from persona_runtime.models import RuntimeErrorBase, identifier
from tests import test_private_reply_stage2 as ordinary


def event(delta=None, *, finish=None, model=main_reply.MODEL, usage=None):
    value = {'model':model, 'provider':'701', 'id':'gen-701', 'choices':[] if delta is None else [
        {'index':0, 'delta':delta, 'finish_reason':finish}]}
    if usage is not None:
        value['usage']=usage
    return ('data: '+json.dumps(value)+'\n\n').encode()


class Wire(unittest.TestCase):
    def setUp(self):
        self.model = dict(base_url=main_reply.URL,name=main_reply.MODEL,max_tokens=8192,timeout=180,
                          _openrouter_stream=True)

    def test_wire_is_body_only_in_both_modes(self):
        with self.assertRaises(ValueError):
            require_generation_provider(self.model)
        require_generation_provider(self.model,allow_openrouter_writer=True)
        for stream in (True,False):
            messages = [{'role':'user','content':'701'}]
            payload, _ = build_request(dict(self.model,_openrouter_stream=stream),messages)
            self.assertEqual(payload,{'model':main_reply.MODEL,'messages':messages,'max_tokens':8192,
                                      'stream':stream,'reasoning':{'effort':'low','exclude':True}})
            self.assertEqual(endpoint(self.model),'https://openrouter.ai/api/v1/chat/completions')

    def test_reasoning_does_not_count_as_first_content_or_enter_receipts(self):
        stream = ChatStream(self.model)
        for chunk in (b': 700\r\n\r\n',event({'reasoning_details':[{'text':'991'}]})):
            stream.feed(chunk)
        self.assertIsNone(stream.first_content_delta_ms)
        body = chr(0x4E00)+chr(0x4E8C)
        frame=event({'content':body},finish='stop').replace(b'\\u4e00\\u4e8c',body.encode())
        for byte in frame:
            stream.feed(bytes([byte]))
        stream.feed(event(usage={'completion_tokens_details':{'reasoning_tokens':7},'cost':0.001})+b'data: [DONE]\n\n')
        text,usage,details=stream.finish()
        self.assertEqual(text,body)
        self.assertEqual(usage['openrouter_cost'],0.001)
        self.assertNotIn('"991"',json.dumps([text,usage,details]))

    def test_missing_done_truncated_filtered_and_wrong_model_have_no_body(self):
        for frame,code in (
                (event({'content':'701'},finish='stop'),'provider_network_error'),
                (event({'content':'701'},finish='length')+b'data: [DONE]\n\n','provider_output_truncated'),
                (event({'content':'701'},finish='content_filter')+b'data: [DONE]\n\n','provider_content_filtered'),
                (event({'reasoning':'991'},finish='stop')+b'data: [DONE]\n\n','provider_empty_response'),
                (event({'content':'701'},finish='stop',model='other'),'provider_unexpected_response_model')):
            with self.subTest(code=code),self.assertRaisesRegex(ModelError,code):
                parser=ChatStream(self.model)
                parser.feed(frame)
                parser.finish()

    def test_openrouter_accounting_frame_repeats_stop_but_still_requires_done(self):
        final = event({'content':'701'},finish='stop')
        accounting = event({'content':'','role':'assistant'},finish='stop',usage={'total_tokens':7})
        parser = ChatStream(self.model)
        parser.feed(final+accounting+b'data: [DONE]\n\n')
        body, usage, details = parser.finish()
        self.assertEqual(body,'701')
        self.assertEqual(usage,{'total_tokens':7})
        self.assertEqual(details['openrouter_generation_id'],'gen-701')
        for tail in (accounting,accounting+accounting,
                     event({'content':'702'},finish='stop',usage={'total_tokens':7}),
                     event({'content':''},finish='length',usage={'total_tokens':7}),
                     event({'content':''},finish='stop')):
            with self.subTest(tail=tail), self.assertRaises(ModelError):
                parser = ChatStream(self.model)
                parser.feed(final+tail)
                parser.finish()


class LocalFlow(unittest.IsolatedAsyncioTestCase):
    setUp = ordinary.OrdinaryTests.setUp
    reopen = ordinary.OrdinaryTests.reopen
    submit = ordinary.OrdinaryTests.submit
    consent = ordinary.OrdinaryTests.consent

    def register(self):
        cfg=self.config.load()
        mark=self.paths.home/'private/ordinary-reply/registration.json'
        mark.parent.mkdir(parents=True,exist_ok=True)
        body=json.dumps({'main':{'name':'deepseek-v4-pro','base_url':'https://api.deepseek.com'},
                         'notice':{'name':'deepseek-flash'},'scope':{'instance':cfg['instance_id']}}).encode()
        digest=sha256(body).hexdigest()
        self.providers.version=digest
        mark.with_name(digest+'.json').write_bytes(body)
        mark.write_text(json.dumps({'instance':cfg['instance_id'],'reviewed_sha256':digest}),encoding='utf-8')
        return digest

    async def test_disconnected_registration_and_switch_only_change_new_bodies(self):
        digest=self.register()
        self.consent(True)
        cfg=self.config.load()
        prior=session_privacy.recipients(self.config,cfg)
        with self.assertRaises(RuntimeErrorBase):
            self.config.update({'main_reply':{'active':main_reply.HYDROGEN}})
        self.config.update({'main_reply':{'api_key':'881702','enabled':True}})
        self.assertEqual(self.config.public()['main_reply']['active'],main_reply.DS)
        self.assertNotIn('encrypted_key',self.config.public()['main_reply'])
        self.assertEqual(session_privacy.recipients(self.config,self.config.load()),prior)
        cfg=self.config.load()
        resolved=SimpleNamespace(version=digest,notice_model=lambda: {'name':'deepseek-flash'},
                                 bundle={'main':{k:cfg['model'][k] for k in ('base_url','name')},
                                         'scope':{'instance':cfg['instance_id']}})
        with patch('persona_runtime.ordinary_providers.registered',return_value=resolved):
            self.config.update({'main_reply':{'reviewed_bundle':digest}})
            self.config.update({'main_reply':{'active':main_reply.HYDROGEN}})
        self.reopen()
        self.assertEqual(self.config.public()['main_reply']['active'],main_reply.HYDROGEN)
        with self.store.connection() as db:
            cfg=self.config.load()
            state=session_privacy.writer_consent(db,self.config,cfg,self.pid,self.sid)
            self.assertFalse(state['enabled'])
        session_privacy.update_writer_consent(self.store,self.config,self.pid,self.sid,True,state['revision'],state['recipient_hash'])
        write=AsyncMock(return_value=('708',{}))
        with patch.object(self.providers,'bind_hydrogen_reply',return_value=write,create=True) as bound:
            with patch.object(self.chat,'_schedule'):
                accepted=await self.chat.submit('desktop','707',identifier(),self.sid,persona_id=self.pid)
            tid=accepted['id']
            self.config.update({'main_reply':{'active':main_reply.DS}})
            await self.chat._run(tid)
            self.assertEqual(write.await_count,1)
            frozen=bound.call_args.args[0]
            with self.store.connection() as db:
                model=json.loads(db.execute("SELECT model_snapshot FROM turns WHERE id=?",(tid,)).fetchone()[0])
            self.assertEqual(model['_main_reply'],frozen)
            self.assertEqual(model['name'],'deepseek-v4-pro')
            self.assertEqual(main_reply.resolve(self.config,self.config.load(),frozen)['name'],main_reply.MODEL)
            await self.submit()
            self.assertEqual(write.await_count,1)
        self.config.update({'main_reply':{'api_key':'881703'}})
        with self.assertRaises(RuntimeErrorBase):
            main_reply.resolve(self.config,self.config.load(),frozen)


class NonStreaming(unittest.IsolatedAsyncioTestCase):
    async def test_real_client_validates_json_final_state_on_fixed_route(self):
        client = ChatCompletionsClient(allow_openrouter_writer=True)
        model = dict(base_url=main_reply.URL,name=main_reply.MODEL,max_tokens=8192,timeout=180,
                     encrypted_key='701',_openrouter_stream=False)
        class Response:
            status=200
            headers={}
            async def iter_chunked(self,size):
                yield json.dumps(data).encode()
            content=property(lambda self:self)
            async def __aenter__(self):return self
            async def __aexit__(self,*_):pass
        class Session:
            def __init__(self,**_):pass
            async def __aenter__(self):return self
            async def __aexit__(self,*_):pass
            def post(self,url,**params):
                self_checked.assertEqual(url,main_reply.URL+'/chat/completions')
                self_checked.assertFalse(params['allow_redirects'])
                return Response()
        self_checked=self
        data={'model':main_reply.MODEL,'provider':'701','choices':[{'finish_reason':'stop',
              'message':{'content':'702','reasoning':'991','reasoning_details':[{'text':'992'}]}}]}
        with (patch('persona_runtime.chat.aiohttp.ClientSession',Session),
              patch('persona_runtime.chat.unprotect',return_value='703'),
              patch('persona_runtime.network_proxy.outbound_proxy',return_value='http://127.0.0.1:702')):
            completion=await client.complete(model,[{'role':'user','content':'701'}])
            self.assertEqual(completion[0],'702')
            self.assertNotIn('991',json.dumps(completion.diagnostics))
            for finish,code in (('length','provider_output_truncated'),('tool_calls','provider_tool_call_not_supported'),
                                (None,'provider_invalid_response')):
                data['choices'][0]['finish_reason']=finish
                with self.assertRaisesRegex(ModelError,code):
                    await client.complete(model,[{'role':'user','content':'701'}])
