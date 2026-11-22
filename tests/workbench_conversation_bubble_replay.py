"""Exercise the ordinary workbench with a saved reply and loopback-only providers."""
import ipaddress
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
from unittest.mock import patch

from persona_runtime.conversation_bubbles import frames
from persona_runtime.paths import RuntimePaths
from persona_runtime.private_reply_output import FACT_REVIEW_VERSION
from persona_runtime.storage import Store
from tests.test_private_reply_stage3 import ResearchProviders
from tests.workbench_import_v2_smoke import Screen
from tests.workbench_private_daily_cycle import setup


def service(module):
    from persona_runtime import server
    paths = RuntimePaths.from_environment()
    body = (paths.home/'body.txt').read_bytes().decode('utf-8')
    def local(event, args):
        if event=='socket.connect' and isinstance(args[1],tuple):
            if args[1][0]!='localhost' and not ipaddress.ip_address(args[1][0]).is_loopback:
                raise RuntimeError('unexpected_external_connection')
    sys.addaudithook(local)
    class Provider(ResearchProviders):
        output_contract_version = FACT_REVIEW_VERSION
        conversation_systems = {'reply_system':'701'}
        async def choose(self,node,aid,state,options):
            if node=='J6.mode':
                return 'short_answer',{}
            self.route='none'
            return await super().choose(node,aid,state,options)
        async def reply(self,aid,packet):
            assert set(packet['output_constraints'])=={'phase','mode','output_fields'}
            return body,{}
    create=server.create_app
    with patch.object(server,'create_app',lambda *a,**kw:create(*a,private_chat_providers=Provider(),**kw)):
        runpy.run_module(module,run_name='__main__')


def desktop(phase):
    from persona_runtime import workbench_webview as gui
    from persona_runtime.app_processes import AppProcesses
    paths=RuntimePaths.from_environment()
    body=(paths.home/'body.txt').read_bytes().decode('utf-8')
    pieces=[body[f['start']:f['end']] for f in frames(body)]
    sid=json.loads((paths.home/'cycle.json').read_bytes())['session']
    errors=[]
    def exercise(host):
        try:
            screen=Screen(host.window)
            screen.wait('window.pcarLive?.active && !pcarLive.offline',timeout=55)
            if phase=='reopen':
                screen.wait('Boolean(currentPerson()) && !pcarLive.selecting')
                screen.eval('window.pcarLive.select(currentPerson(),false,true,'+json.dumps(sid)+');true')
            screen.wait('currentSession()?.id==='+json.dumps(sid),timeout=45)
            if phase=='submit':
                screen.wait('!message.disabled')
                screen.eval("message.value='701';message.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#composer').requestSubmit();true")
            bubbles="[...document.querySelectorAll('#timeline .bubble.reply')].map(b=>b.textContent)"
            screen.wait('JSON.stringify('+bubbles+')==='+json.dumps(json.dumps(pieces,ensure_ascii=False,separators=(',',':'))),timeout=60)
            from tests.workbench_daily_cycle import wait
            store=Store(paths)
            def committed():
                with store.connection() as db:
                    return db.execute("SELECT 1 FROM turns WHERE session_id=? AND status='completed'",(sid,)).fetchone()
            wait(committed)
            with store.connection() as db:
                rows=list(db.execute("""SELECT v.text,v.receipt_id,v.presented_at,e.text
                    FROM expression_speech_visible v JOIN expression_speech_publications p ON p.id=v.publication_id
                    JOIN events e ON e.id=p.canonical_event_id ORDER BY v.ordinal"""))
                assert [r[0] for r in rows]==pieces and all(r[3]==body for r in rows)
                assert db.execute("SELECT count(*) FROM private_lookup_calls").fetchone()[0]==0
            result={'lengths':list(map(len,pieces)),'receipts':[r[1] for r in rows],'times':[r[2] for r in rows]}
            (paths.home/(phase+'.json')).write_text(json.dumps(result),encoding='utf-8')
        except BaseException as exc:
            errors.append(exc)
        finally:
            host.controls.exit_workbench()
    class Host(gui.WebViewHost):
        def run(self,web,html):
            start=web.start
            with patch.object(web,'start',lambda **opts:start(lambda:exercise(self),**opts)):
                super().run(web,html)
    start=AppProcesses.start
    def start_service(owner,role,args,**kw):
        assert role=='service'
        code=f'from tests.workbench_conversation_bubble_replay import service;service({args[3]!r})'
        return start(owner,role,[args[0],'-B','-c',code,*args[4:]],**kw)
    with patch.object(AppProcesses,'start',start_service),patch.object(gui,'WebViewHost',Host):
        gui.run(paths)
    if errors:
        raise errors[0]


if __name__=='__main__':
    home=Path(sys.argv[1]).resolve()
    body=json.loads(Path(sys.argv[2]).read_bytes())['body']
    setup(home)
    (home/'body.txt').write_text(body,encoding='utf-8',newline='')
    env=os.environ|{'PERSONA_RUNTIME_HOME':str(home),'PCAR_PYTHON':sys.executable,'PYTHONUTF8':'1'}
    for phase in ('submit','reopen'):
        code=f'from tests.workbench_conversation_bubble_replay import desktop;desktop({phase!r})'
        subprocess.run([sys.executable,'-B','-c',code],env=env,check=True,timeout=145)
    assert (home/'submit.json').read_bytes()==(home/'reopen.json').read_bytes()
    print((home/'reopen.json').read_text())
