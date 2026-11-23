"""Package the shared workbench and its Android presentation layer."""

from pathlib import Path
import re
import shutil
import sys


def render(source, mobile, output):
    destination = output / "workbench"
    destination.mkdir(parents=True, exist_ok=True)
    page = (source / "index.html").read_text(encoding="utf-8")
    scripts = re.findall(r"<script(?: [^>]*)?>(.*?)</script>", page, flags=re.S)
    if len(scripts) != 2:
        raise ValueError("Unexpected shared workbench script layout")
    for filename, script in zip(("appearance.js", "base.js"), scripts, strict=True):
        (destination / filename).write_text(script, encoding="utf-8")
    names = iter(("appearance.js", "base.js"))
    page = re.sub(r"<script(?: [^>]*)?>.*?</script>",
                  lambda _: f'<script src="{next(names)}"></script>', page, flags=re.S)
    page = page.replace('data-appearance="system">', 'data-appearance="system" data-native-workbench data-pcar-android>', 1)
    policy = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
              "img-src 'self' data:; connect-src 'none'; object-src 'none'; frame-src 'none'; "
              "form-action 'none'; base-uri 'none'")
    page, updated = re.subn(r'<meta http-equiv="Content-Security-Policy" content="[^"]*">',
                            f'<meta http-equiv="Content-Security-Policy" content="{policy}">', page)
    if updated != 1:
        raise ValueError("Expected one shared workbench content security policy")
    page = page.replace('<script src="appearance.js"></script>',
        '  <script src="android-transport.js"></script>\n  <script src="appearance.js"></script>', 1)
    page = page.replace("</head>", '  <link rel="stylesheet" href="android.css">\n</head>', 1)
    page = page.replace("</body>", '  <script src="persona_import.js"></script>\n'
                        '  <script src="android-ui.js"></script>\n</body>', 1)
    (destination / "index.html").write_text(page, encoding="utf-8")
    shutil.copyfile(source / "persona_import.js", destination / "persona_import.js")
    for name in ("android-transport.js", "android-ui.js", "android.css"):
        shutil.copyfile(mobile / name, destination / name)


if __name__ == "__main__":
    render(*(Path(argument) for argument in sys.argv[1:]))
