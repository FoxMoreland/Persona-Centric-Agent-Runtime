"""Local display preferences and imported images for the desktop workbench."""
import base64
from io import BytesIO
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile

from .character_embedding import private_directory, safe_path
from .config import replace_closed
from .models import RuntimeErrorBase


APPEARANCE_FILE = Path("config", "workbench-appearance.json")
APPEARANCE_MODES = ("system", "light", "dark")
ACCENT = re.compile(r"#[0-9a-fA-F]{6}")
IMAGE_TYPES = {"avatar": ("png", "image/png"), "background": ("webp", "image/webp")}
MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_PIXELS = 40_000_000


def appearance_settings(paths):
    try:
        saved = json.loads((paths.home / APPEARANCE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    if not isinstance(saved, dict):
        saved = {}
    mode = saved.get("mode")
    accent = saved.get("accent")
    return {
        "mode": mode if mode in APPEARANCE_MODES else "system",
        "accent": accent.lower() if isinstance(accent, str) and ACCENT.fullmatch(accent) else None,
    }


def image_path(paths, pid, kind):
    from .persona_management import active
    from .storage import Store

    if not isinstance(kind, str) or kind not in IMAGE_TYPES or not isinstance(pid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", pid):
        raise RuntimeErrorBase("人物外观参数无效。")
    with Store(paths).connection() as db:
        if not db.execute("SELECT 1 FROM personas WHERE id=?", (pid,)).fetchone():
            raise RuntimeErrorBase("人物不存在。")
        active(db, pid)
    suffix, _ = IMAGE_TYPES[kind]
    path = paths.home / "private" / "workbench-appearance" / pid / f"{kind}.{suffix}"
    return safe_path(paths.home, path)


def image_url(path, kind):
    try:
        payload = path.read_bytes()
    except OSError:
        return None
    _, mime = IMAGE_TYPES[kind]
    return f"data:{mime};base64," + base64.b64encode(payload).decode("ascii")


def prepare_image(source, kind):
    from PIL import Image, ImageOps

    try:
        with Path(source).open("rb") as stream:
            data = stream.read(MAX_SOURCE_BYTES + 1)
        if len(data) > MAX_SOURCE_BYTES:
            raise RuntimeErrorBase("请选择小于 32 MiB 的图片。")
        with Image.open(BytesIO(data), formats=("PNG", "JPEG", "WEBP")) as original:
            if original.width * original.height > MAX_PIXELS:
                raise RuntimeErrorBase("图片像素过多，请缩小后再选择。")
            image = ImageOps.exif_transpose(original).convert("RGBA")
        image.info.clear()
        output = BytesIO()
        if kind == "avatar":
            image = ImageOps.fit(image, (192, 192), Image.Resampling.LANCZOS)
            image.save(output, format="PNG", optimize=True)
        else:
            image.thumbnail((2560, 1600), Image.Resampling.LANCZOS)
            image.save(output, format="WEBP", quality=88)
        return output.getvalue()
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        raise RuntimeErrorBase("无法读取图片，请选择有效的 PNG、JPEG 或 WebP 文件。") from error


def store_image(paths, path, image):
    private_directory(paths.home, path.parent)
    safe_path(paths.home, path)
    with NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(image)
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            replace_closed(temporary, path)
        finally:
            stream.close()
            temporary.unlink(missing_ok=True)
