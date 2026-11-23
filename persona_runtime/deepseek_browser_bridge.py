"""Windows Native Messaging host for the completed, user-requested DeepSeek download."""
import argparse
import json
import os
from pathlib import Path
import struct
import subprocess
import sys

from .config import atomic_json, ConfigStore
from .deepseek_intake import DeepSeekInbox, JOB
from .models import RuntimeErrorBase
from .paths import RuntimePaths
from .workbench_instance import wake_active


ORIGIN = "chrome-extension://inbnheikfofblfnloonicjjgipoggfdi/"
NAMES = {"development": "com.pcar.deepseek.development", "community": "com.pcar.deepseek.community"}
REGISTRY = (r"Software\Google\Chrome\NativeMessagingHosts", r"Software\Microsoft\Edge\NativeMessagingHosts")
MAX_MESSAGE = 16384


def _folder(edition):
    return Path(os.environ["LOCALAPPDATA"]) / "PCAR Browser Bridge" / edition


def _installed_home(edition):
    return Path(os.environ["LOCALAPPDATA"]) / ("PCAR Community Data" if edition == "community" else "PCAR Development Data")


def register(edition, source, interpreter, home=None):
    import winreg
    root = _folder(edition)
    root.mkdir(parents=True, exist_ok=True)
    source, interpreter = Path(source).resolve(strict=True), Path(interpreter).resolve(strict=True)
    home = Path(home).resolve() if home is not None else _installed_home(edition)
    manifest = root / "host.json"
    wrapper = root / "host.bat"
    atomic_json(root / "instance.json", {"edition": edition, "source": str(source),
                                        "python": str(interpreter), "home": str(home)}, durable=True)
    command = ["-E", "-s", "-B", "-m", "persona_runtime.deepseek_browser_bridge", "serve", edition]
    script = ('@echo off\nsetlocal DisableDelayedExpansion\nchcp 65001 >nul\n'
              f'cd /d "{str(source).replace("%", "%%")}" || exit /b 1\n'
              f'"{str(interpreter).replace("%", "%%")}" {" ".join(command)} %*\n')
    wrapper.write_text(script, encoding="utf-8", newline="\r\n")
    atomic_json(manifest, {"name": NAMES[edition], "description": "PCAR DeepSeek import",
                          "path": str(wrapper), "type": "stdio", "allowed_origins": [ORIGIN]}, durable=True)
    for prefix in REGISTRY:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, prefix + "\\" + NAMES[edition]) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(manifest))


def unregister(edition, source):
    import winreg
    root = _folder(edition)
    try:
        settings = json.loads((root / "instance.json").read_bytes())
    except (OSError, ValueError):
        return
    if Path(settings.get("source", "")).resolve() != Path(source).resolve():
        return
    target = str(root / "host.json")
    for prefix in REGISTRY:
        name = prefix + "\\" + NAMES[edition]
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, name) as key:
                same = winreg.QueryValueEx(key, "")[0] == target
            if same:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, name)
        except FileNotFoundError:
            continue
    for name in ("host.json", "host.bat", "instance.json"):
        (root / name).unlink(missing_ok=True)


def _launch(settings, paths):
    if paths and wake_active(paths):
        return
    source = Path(settings["source"])
    interpreter = Path(settings["python"])
    if settings["edition"] == "community":
        args = [str(source / "PCAR.exe")]
    else:
        if paths is None:
            raise RuntimeErrorBase("请先打开 PCAR 开发版并完成初始化。")
        args = [str(interpreter), "-B", "-m", "persona_runtime", "--home", str(paths.home), "gui", "--frontend", "webview"]
    subprocess.Popen(args, cwd=source, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)


def deliver(settings, request):
    if not isinstance(request, dict) or request.get("version") != 1:
        return {"state": "unsupported"}
    if request.get("action") == "probe":
        return {"state": "ready", "edition": settings["edition"]}
    fields = {"version", "action", "id", "target"}
    if not ((request.get("action") == "remember" and set(request) == fields)
            or (request.get("action") == "deliver" and set(request) == fields | {"path"})):
        return {"state": "unsupported"}
    if not isinstance(request["id"], str) or not JOB.fullmatch(request["id"]):
        return {"state": "invalid_file"}
    if (request["action"] == "deliver" and not isinstance(request["path"], str)) or not request["target"]:
        return {"state": "invalid_file"}
    home = Path(settings["home"])
    paths = RuntimePaths(home) if (home / "config/workbench.json").is_file() else None
    if paths is None or not ConfigStore(paths).file.is_file():
        _launch(settings, None)
        return {"state": "setup_required"}
    from .desktop_activation import DesktopActivation
    if DesktopActivation(ConfigStore(paths)).status()["state"] != "verified":
        _launch(settings, paths)
        return {"state": "key_required"}
    inbox = DeepSeekInbox(paths)
    if request["action"] == "remember":
        inbox.remember(request["target"], request["id"])
        return {"state": "remembered"}
    result = inbox.enqueue(request["path"], request["target"], job=request["id"])
    if result == "pending":
        _launch(settings, paths)
    return {"state": result}


def serve(edition, origin, source=None, sink=None):
    if origin != ORIGIN:
        return 1
    # pythonw has no console streams. Registration must remain importable
    # there; only the native host process uses binary standard streams.
    if source is None:
        source = getattr(sys.stdin, "buffer", None)
    if sink is None:
        sink = getattr(sys.stdout, "buffer", None)
    if source is None or sink is None:
        return 1
    result = {"state": "invalid_file"}
    try:
        header = source.read(4)
        if len(header) == 4:
            length, = struct.unpack("<I", header)
            if 0 < length <= MAX_MESSAGE:
                body = source.read(length)
                if len(body) == length:
                    settings = json.loads((_folder(edition) / "instance.json").read_bytes())
                    if settings["edition"] == edition:
                        result = deliver(settings, json.loads(body))
    except (OSError, ValueError, TypeError, KeyError):
        pass
    payload = json.dumps(result).encode()
    sink.write(struct.pack("<I", len(payload)) + payload)
    sink.flush()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("register", "unregister", "serve"))
    parser.add_argument("edition", choices=tuple(NAMES))
    parser.add_argument("origin", nargs="?")
    parser.add_argument("--home", type=Path)
    parser.add_argument("--parent-window")
    args = parser.parse_args(argv)
    if args.command == "serve":
        return serve(args.edition, args.origin)
    source = Path(__file__).resolve().parents[1]
    if args.command == "register":
        register(args.edition, source, sys.executable, args.home)
    else:
        unregister(args.edition, source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
