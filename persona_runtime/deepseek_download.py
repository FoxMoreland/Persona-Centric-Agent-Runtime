"""Receive completed DeepSeek browser downloads without accessing browser data."""
import ctypes
import os
from ctypes import wintypes
from pathlib import Path
import re
import stat
from threading import Event, Thread
from uuid import UUID

from .corpus_files import CorpusError
from .deepseek_export import inspect
from .deepseek_intake import DeepSeekInbox, archive_identity
from .models import RuntimeErrorBase


EXPORT_NAME = re.compile(r"deepseek_data(?:-[^/\\]+)?\.zip", re.IGNORECASE)
DOWNLOADS_ID = UUID("374DE290-123F-4565-9164-39C4925E467B")


def windows_downloads():
    function = ctypes.windll.shell32.SHGetKnownFolderPath
    function.argtypes = (ctypes.c_void_p, wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p))
    function.restype = ctypes.c_long
    folder_id = (ctypes.c_ubyte * 16).from_buffer_copy(DOWNLOADS_ID.bytes_le)
    value = ctypes.c_wchar_p()
    if function(ctypes.byref(folder_id), 0, None, ctypes.byref(value)) != 0 or not value.value:
        raise RuntimeErrorBase("找不到 Windows 下载文件夹。请选择下载位置。")
    free = ctypes.windll.ole32.CoTaskMemFree
    free.argtypes = (ctypes.c_void_p,)
    free.restype = None
    try:
        return Path(value.value)
    finally:
        free(ctypes.cast(value, ctypes.c_void_p))


def snapshot(folder):
    result = {}
    with os.scandir(folder) as entries:
        for entry in entries:
            if not EXPORT_NAME.fullmatch(entry.name):
                continue
            try:
                info = os.stat(entry.path, follow_symlinks=False)
            except OSError:
                continue
            if (stat.S_ISREG(info.st_mode)
                    and not (getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)):
                result[Path(entry.path)] = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]
    return result


class DeepSeekDownloadWatch:
    def __init__(self, paths, folder, guard, notify):
        self.folder = Path(folder).resolve(strict=True)
        if not self.folder.is_dir() or self.folder.drive.startswith("\\\\"):
            raise RuntimeErrorBase("请选择本机浏览器的下载文件夹。")
        self.base = snapshot(self.folder)
        self.previous = {}
        self.rejected = {}
        self.inbox = DeepSeekInbox(paths)
        self.guard = guard
        self.notify = notify
        self.stop_event = Event()
        self.thread = Thread(target=self._run, name="deepseek-download", daemon=True)

    def start(self):
        self.thread.start()

    def active(self):
        return not self.stop_event.is_set() and self.thread.is_alive()

    def stop(self):
        self.stop_event.set()

    def _run(self):
        while not self.stop_event.wait(0.8):
            try:
                if self.poll():
                    self.notify()
                    return
            except (OSError, RuntimeErrorBase):
                self.stop()
                return

    def poll(self):
        files = snapshot(self.folder)
        for path, identity in sorted(files.items(), key=lambda item: item[1][3], reverse=True):
            if (self.base.get(path) == identity or self.previous.get(path) != identity
                    or self.rejected.get(path) == identity or self.stop_event.is_set()):
                continue
            try:
                if archive_identity(path) != identity:
                    continue
                inspect(path)
            except (OSError, CorpusError, RuntimeErrorBase):
                self.rejected[path] = identity
                continue
            if self.stop_event.is_set() or archive_identity(path) != identity:
                continue
            self.guard()
            if self.stop_event.is_set():
                return False
            self.inbox.enqueue(path)
            self.stop()
            return True
        self.previous = files
        return False
