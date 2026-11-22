"""Window acceptance with numeric messages and an isolated model substitute."""
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from PIL import Image

from persona_runtime.models import PersonaSpec
from persona_runtime.paths import RuntimePaths
from persona_runtime.service import RuntimeService
from persona_runtime.sessions import select
from persona_runtime.storage import Store
from tests.workbench_daily_cycle import wait
from tests.workbench_private_daily_cycle import desktop, setup


def open_person(screen, name):
    row = "[...document.querySelectorAll('#people button')].find(b=>b.textContent===" + json.dumps(name) + ")"
    screen.wait("Boolean(" + row + ")")
    screen.eval("(" + row + ").click();true")
    screen.wait("document.getElementById('current-person').textContent===" + json.dumps(name) + " && !pcarLive.selecting")


def open_profile(screen):
    screen.eval("document.getElementById('profile-button').click();true")
    screen.wait("document.querySelector('dialog').open")


def close_profile(screen):
    screen.eval("document.querySelector('dialog').close();true")
    screen.wait("!document.querySelector('dialog').open")


def save_mode(screen, mode):
    screen.eval("document.getElementById('appearance-mode').value=" + json.dumps(mode) +
                ";document.getElementById('appearance-mode').dispatchEvent(new Event('change'));true")
    screen.wait("document.documentElement.dataset.appearance===" + json.dumps(mode))


def test_window(home, phase):
    paths = RuntimePaths(home)
    store = Store(paths)

    def check(screen, host):
        if phase == "appearance-reopen":
            screen.wait("document.documentElement.dataset.accent==='#a73887' && document.documentElement.dataset.appearance==='dark'")
            for name in ("731", "751"):
                open_person(screen, name)
                screen.wait("Boolean(document.querySelector('#chat-avatar img')?.naturalWidth) && document.getElementById('timeline').classList.contains('has-wallpaper')")
            old_session = screen.eval("currentSession().id")
            background = screen.eval("timeline.style.getPropertyValue('--chat-wallpaper')")
            screen.eval("document.getElementById('new-session').click();true")
            screen.wait("currentSession().id!==" + json.dumps(old_session) + " && !pcarLive.selecting")
            assert screen.eval("timeline.style.getPropertyValue('--chat-wallpaper')") == background
            screen.eval("document.querySelector('#sessions button').click();true")
            screen.wait("currentSession().id===" + json.dumps(old_session) + " && !pcarLive.selecting")
            assert screen.eval("timeline.style.getPropertyValue('--chat-wallpaper')") == background
            open_profile(screen)
            screen.click("恢复默认头像")
            screen.wait("!document.querySelector('#chat-avatar img')")
            screen.click("恢复默认聊天背景")
            screen.wait("!document.getElementById('timeline').classList.contains('has-wallpaper')")
            close_profile(screen)
            screen.eval("document.getElementById('appearance-reset').click();true")
            screen.wait("!document.documentElement.hasAttribute('data-accent')")
            wait(lambda: not json.loads((home/'config/workbench-appearance.json').read_bytes())['accent'])
            open_person(screen, "731")
            screen.wait("Boolean(document.querySelector('#chat-avatar img')?.naturalWidth) && timeline.classList.contains('has-wallpaper')")
            return {"reopened_and_isolated": True, "reset_verified": True}

        paths_to_pick = []
        def choose(*args, **kwargs):
            return paths_to_pick.pop(0)

        def upload(kind, file):
            paths_to_pick.append([str(home/file)] if file else None)
            screen.click("选择" + kind)

        with patch.object(host.window, 'create_file_dialog', side_effect=choose):
            open_profile(screen)
            upload("头像", "first-avatar.png")
            screen.wait("Boolean(document.querySelector('#chat-avatar img')?.naturalWidth)")
            first_avatar = screen.eval("document.querySelector('#chat-avatar img').src")
            upload("头像", "invalid.png")
            screen.wait("document.getElementById('toast').textContent.length>0")
            assert screen.eval("document.querySelector('#chat-avatar img').src") == first_avatar
            upload("聊天背景", None)
            assert screen.eval("!timeline.classList.contains('has-wallpaper')")
            upload("聊天背景", "first-background.jpg")
            screen.wait("document.getElementById('timeline').classList.contains('has-wallpaper')")
            first_background = screen.eval("timeline.style.getPropertyValue('--chat-wallpaper')")
            close_profile(screen)
            open_person(screen, "751")
            screen.wait("!document.querySelector('#chat-avatar img') && !timeline.classList.contains('has-wallpaper')")
            open_profile(screen)
            upload("头像", "second-avatar.webp")
            screen.wait("Boolean(document.querySelector('#chat-avatar img')?.naturalWidth)")
            upload("聊天背景", "second-background.png")
            screen.wait("timeline.classList.contains('has-wallpaper')")
            assert screen.eval("document.querySelector('#chat-avatar img').src") != first_avatar
            assert screen.eval("timeline.style.getPropertyValue('--chat-wallpaper')") != first_background
            close_profile(screen)
            open_person(screen, "731")
            screen.wait("document.querySelector('#chat-avatar img')?.src===" + json.dumps(first_avatar))
            assert screen.eval("timeline.style.getPropertyValue('--chat-wallpaper')") == first_background

        screen.eval("document.getElementById('appearance-accent').value='#a73887';"
                    "document.getElementById('appearance-accent').dispatchEvent(new Event('input'));true")
        screen.wait("document.documentElement.style.getPropertyValue('--accent')!==''")
        screen.eval("document.getElementById('appearance-accent').dispatchEvent(new Event('change'));true")
        for mode in ("light", "system", "dark"):
            save_mode(screen, mode)
        wait(lambda: json.loads((home/'config/workbench-appearance.json').read_bytes()) ==
             {"mode": "dark", "accent": "#a73887"})

        screen.eval("document.getElementById('session-privacy').click();true")
        screen.click("同意并开启本会话全流程授权")
        screen.wait("!document.querySelector('dialog').open")
        screen.eval("document.getElementById('input-buffer-settings').click();true")
        screen.wait("document.querySelector('dialog select')")
        screen.eval("document.querySelector('dialog select').value='manual';"
                    "document.querySelector('dialog select').dispatchEvent(new Event('change'));true")
        screen.click("保存")
        screen.wait("!document.querySelector('dialog').open")
        screen.eval("""(() => {
          const api=window.pywebview.api, original=api.live_submissions;
          api.live_submissions=async (...args)=>{
            const value=await original(...args);
            api.live_submissions=original;
            await new Promise(resolve=>{window.releaseOldBufferSnapshot=resolve;});
            return value;
          };
          pcarLive.wake();
          return true;
        })()""")
        screen.wait("typeof window.releaseOldBufferSnapshot==='function'")

        def key(flags=""):
            return screen.eval("message.focus();!message.dispatchEvent(new KeyboardEvent('keydown',"
                               "{key:'Enter',bubbles:true,cancelable:true"+flags+"}))")

        for count, value in enumerate(("941", "942"), 1):
            screen.eval(f"message.value='{value}';message.dispatchEvent(new Event('input',{{bubbles:true}}));true")
            assert key(",isComposing:true") is False
            assert key(",shiftKey:true") is False
            assert key() is True
            screen.wait("message.value==='' && message.placeholder==='再次按下回车即可发送' && "
                        f"currentSession().inputBuffer.batch.members.length==={count}")
            if count == 1:
                screen.eval("window.releaseOldBufferSnapshot();true")
                assert screen.eval("currentSession().inputBuffer.batch.members.length") == 1
        assert key(",repeat:true") is True
        assert screen.eval("message.placeholder") == "再次按下回车即可发送"
        assert key() is True
        screen.wait("!currentSession().inputBuffer.batch && message.placeholder==='输入消息'")
        def delivered():
            with store.connection() as db:
                return db.execute("SELECT count(*) FROM turns WHERE status='completed'").fetchone()[0] == 1
        wait(delivered, timeout=65)
        with store.connection() as db:
            assert db.execute("SELECT count(*) FROM turns").fetchone()[0] == 1
            members = [row[0] for row in db.execute("""SELECT e.text FROM input_batch_members m
                JOIN events e ON e.id=m.event_id ORDER BY m.member_order""")]
            assert members == ["941", "942"], members
            assert db.execute("SELECT count(*) FROM ingress_receipts").fetchone()[0] == 2
        return {"buffered_members": members, "completed_turns": 1, "theme_saved": True, "two_personas": True}

    desktop(home, phase, check)


def run(home):
    setup(home)
    store = Store(RuntimePaths(home))
    service = RuntimeService(store)
    second = service.create_persona(PersonaSpec(1, "751", "752", ["753"], "754", ["755"], "756"))
    with store.connection(write=True) as db:
        select(db, second["id"], "desktop")
    files = {
        "first-avatar.png": ((200, 100), (120, 12, 54)),
        "first-background.jpg": ((460, 190), (233, 185, 53)),
        "second-avatar.webp": ((90, 140), (30, 71, 242)),
        "second-background.png": ((140, 350), (32, 145, 60)),
    }
    for name, (size, color) in files.items():
        Image.new("RGB", size, color).save(home/name)
    (home/"invalid.png").write_bytes(b"invalid")
    (home/"appearance-case.json").write_text("{}")
    env=os.environ|{'PERSONA_RUNTIME_HOME':str(home),'PCAR_PYTHON':sys.executable}
    for phase in ("appearance", "appearance-reopen"):
        subprocess.run([sys.executable, "-B", "-m", "tests.workbench_appearance_cycle", str(home), phase],
                       check=True, env=env, timeout=130)
        print((home/(phase+".json")).read_text(), flush=True)
        if phase == "appearance":
            for name in files:
                (home/name).rename(home/(name+".original"))


if __name__ == "__main__":
    root=Path(sys.argv[1]).resolve()
    if len(sys.argv) == 2:
        run(root)
    else:
        test_window(root, sys.argv[2])
