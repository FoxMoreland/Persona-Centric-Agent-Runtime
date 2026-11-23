"""Local receipt queue for completed browser exports and the workbench file picker."""
from contextlib import closing
import json
import os
from pathlib import Path
import re
import stat

from .config import atomic_json
from .deepseek_export import SESSION_ID
from .instance import InstanceLock
from .models import RuntimeErrorBase
from uuid import uuid4


JOB = re.compile(r"[a-f0-9]{32}")
MAX_ARCHIVE_BYTES = 2 * 1024**3


def archive_identity(path):
    path = Path(path)
    if not path.is_absolute() or path.drive.startswith("\\\\") or path.suffix.lower() != ".zip":
        raise RuntimeErrorBase("请选择本机 DeepSeek 导出 ZIP。")
    with path.open("rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_ARCHIVE_BYTES:
            raise RuntimeErrorBase("导出 ZIP 为空或超过 2 GiB。")
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


class DeepSeekInbox:
    def __init__(self, paths):
        self.root = paths.home / "runtime/deepseek-intake"
        self.cache = paths.home / "cache/deepseek-downloads"
        self.preference = paths.home / "runtime/deepseek-selection.json"

    def remember(self, target, job):
        self._record(job)
        if not isinstance(target, str) or not SESSION_ID.fullmatch(target):
            raise RuntimeErrorBase("目标会话编号无效。")
        with closing(InstanceLock(self.root / "write.lock")):
            atomic_json(self.preference, {"target": target, "job": job}, durable=True)

    def preferred(self):
        try:
            data = json.loads(self.preference.read_bytes())
            return data if isinstance(data, dict) and isinstance(data.get("target"), str) else None
        except (OSError, ValueError):
            return None

    def forget(self, job):
        with closing(InstanceLock(self.root / "write.lock")):
            preference = self.preferred()
            if preference and preference.get("job") == job:
                self.preference.unlink(missing_ok=True)

    def _record(self, job):
        if not isinstance(job, str) or not JOB.fullmatch(job):
            raise RuntimeErrorBase("导入任务编号无效。")
        return self.root / (job + ".json")

    def enqueue(self, path, target=None, *, job=None, owned=False):
        path = Path(path)
        if target is not None and (not isinstance(target, str) or not SESSION_ID.fullmatch(target)):
            raise RuntimeErrorBase("目标会话编号无效。")
        job = job or uuid4().hex
        record = self._record(job)
        identity = archive_identity(path)
        if owned and path.resolve().parent != self.cache.resolve():
            raise RuntimeErrorBase("导出文件位置无效。")
        value = {"id": job, "path": str(path), "identity": identity, "target": target, "owned": owned}
        self.root.mkdir(parents=True, exist_ok=True)
        with closing(InstanceLock(self.root / "write.lock")):
            if record.exists():
                saved = json.loads(record.read_bytes())
                if any(saved.get(key) != field for key, field in value.items()):
                    raise RuntimeErrorBase("导入任务已绑定另一份文件。")
                return saved["state"]
            atomic_json(record, value | {"state": "pending"}, durable=True)
        return "pending"

    def pending(self):
        for file in sorted(self.root.glob("*.json"), key=lambda path: path.stat().st_mtime_ns):
            try:
                job = json.loads(file.read_bytes())
                if job.get("state") == "pending" and self._record(job["id"]) == file:
                    return job
            except (AttributeError, KeyError, OSError, ValueError):
                continue
        return None

    def finish(self, job, state):
        if state not in ("imported", "cancelled"):
            raise RuntimeErrorBase("导入交接状态无效。")
        record = self._record(job)
        with closing(InstanceLock(self.root / "write.lock")):
            value = json.loads(record.read_bytes())
            if value["state"] != "pending":
                return
            atomic_json(record, value | {"state": state}, durable=True)
        self.forget(job)
        if value["owned"]:
            path = Path(value["path"])
            if path.resolve().parent == self.cache.resolve() and path.suffix == ".zip":
                path.unlink(missing_ok=True)
