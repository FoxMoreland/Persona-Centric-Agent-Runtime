"""Prepare a local, shareable acceptance report for the Community test build."""

from contextlib import closing
from datetime import datetime
import os
from pathlib import Path
import platform
import re
import shutil
import sqlite3
import sys
from tempfile import NamedTemporaryFile
from time import monotonic
from uuid import uuid4

from . import __version__
from .config import ConfigStore
from .deepseek_card_budget import input_limit, input_tokens
from .import_prompts import OUTPUT_BUDGET
from .installation import doctor
from .models import timestamp
from .paths import RuntimePaths


def yes_no(value):
    return '是' if value else '否' if value is False else '未确认'


def external_command(name, source):
    found = shutil.which(name)
    return bool(found and not Path(found).resolve().is_relative_to(source)
                and 'windowsapps' not in [part.lower() for part in Path(found).parts])


def read_database(path, query, params=()):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=.5)) as db:
        return db.execute(query, params).fetchall()


def readable_result(counts, labels):
    return '，'.join(f'{label} {counts.get(code, 0)}' for code, label in labels)


class FieldReport:
    def __init__(self, folder, source, home):
        self.folder = folder
        self.source = source
        self.home = home
        self.started = timestamp()
        self.start_clock = monotonic()

    def _installer(self):
        try:
            state = (self.source / 'field-test-install.txt').read_text(encoding='ascii').strip()
        except (OSError, UnicodeError):
            state = ''
        return {
            'existing': '安装器检测到系统已有 WebView2',
            'bundled': '安装器已运行随包 WebView2 安装程序',
            'restart': '随包 WebView2 安装请求重启',
        }.get(state, '未读到安装回执')

    def _probes(self):
        try:
            checks = doctor(self.home, source_root=self.source)['checks']
        except (OSError, ValueError, RuntimeError, KeyError):
            yield '- 本机依赖预检：未能读取。'
        else:
            labels = {'project_python': '程序解释器', 'character_model': '本地 BGE 资产',
                      'configuration': '配置文件', 'tkinter': 'Tcl/Tk', 'aiohttp': 'aiohttp',
                      'certifi': 'certifi', 'fastembed': 'FastEmbed', 'numpy': 'NumPy',
                      'onnxruntime': 'ONNX Runtime', 'tokenizers': 'Tokenizers'}
            summary = '，'.join(f'{labels[item["name"]]} {"通过" if item["ready"] else "异常"}'
                              for item in checks if item['name'] in labels)
            yield '- 本机依赖预检：' + summary
        try:
            input_tokens(({'content': '0'}, {'content': '1'}))
        except (ValueError, OSError, RuntimeError):
            yield '- 自动建卡分词器：加载失败。'
        else:
            yield f'- 自动建卡分词器：加载通过。输入上限 {input_limit(OUTPUT_BUDGET):,} token。'
        try:
            ready = ConfigStore(RuntimePaths(self.home)).public()['model']['has_key']
        except (OSError, ValueError, RuntimeError, KeyError):
            ready = None
        yield '- 已设置模型密钥：' + yes_no(ready)

    def _activity(self):
        database = self.home / 'data/database/runtime.sqlite3'
        try:
            schema, = read_database(database, 'PRAGMA user_version')[0]
            counts = dict(read_database(database, """SELECT status, count(*) FROM turns
                WHERE channel='desktop' AND created_at>=? GROUP BY status""", (self.started,)))
            labels = [('completed', '完成'), ('failed', '失败'), ('interrupted', '中断'),
                      ('queued', '排队'), ('running', '运行中')]
            yield f'- SQLite 结构版本：{schema}'
            yield '- 本次桌面聊天回合：' + readable_result(counts, labels)
        except (OSError, sqlite3.Error):
            yield '- 本次桌面聊天回合：未能读取。'
        imports = self.home / 'private/persona-import/imports.sqlite3'
        if imports.is_file():
            try:
                rows = read_database(imports, """SELECT status, count(*) FROM calls
                    WHERE created>=? AND status BETWEEN 100 AND 599 GROUP BY status""", (self.started,))
                summary = '，'.join(f'HTTP {status} 共 {count} 次' for status, count in rows)
                yield '- 聊天历史导入模型回执：' + (summary or '本次无 HTTP 回执。')
            except (OSError, sqlite3.Error):
                yield '- 聊天历史导入模型回执：未能读取。'

    def finish(self, outcome, stage, failure):
        shutdown = (outcome or {}).get('shutdown') or {}
        windows = sys.getwindowsversion()
        lines = [
            '# PCAR 社区测试版验收日志', '',
            f'- 程序版本：{__version__} Community Test',
            f'- 运行时间：{self.started} 至 {timestamp()}，约 {round(monotonic()-self.start_clock)} 秒',
            f'- 系统：Windows {windows.major}.{windows.minor}.{windows.build} · {platform.machine()}',
            f'- 随包 Python：{platform.python_version()}',
            f'- PATH 中可用的外部 Python：{yes_no(external_command("python", self.source))}',
            f'- PATH 中可用的 uv：{yes_no(external_command("uv", self.source))}',
            '- WebView2 安装情况：' + self._installer(),
            f'- 工作台已加载：{yes_no(outcome.get("workbench_ready") if outcome else None)}',
            f'- 本机组件已正常释放：{yes_no(shutdown.get("processes_released"))}',
        ]
        if failure:
            lines.append(f'- 中断位置：{stage}，错误类型 `{failure}`。')
        errors = [value for value in shutdown.get('errors', []) if isinstance(value, str)
                  and re.fullmatch(r'(?:drain|checkpoint|stop_ack|console|closing_marker|exit_report)_[a-zA-Z_]{1,64}', value)]
        if errors:
            lines.append('- 退出异常：' + '，'.join(f'`{value}`' for value in errors))
        outstanding = sum(item['count'] for item in shutdown.get('outstanding', {}).values())
        if outstanding:
            lines.append(f'- 退出时待恢复记录：{outstanding} 项。')
        lines.extend(['', '## 本机检查', '', *self._probes(), '', '## 运行记录', '', *self._activity(), ''])
        self.folder.mkdir(parents=True, exist_ok=True)
        name = f'PCAR-验收-{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:8]}.md'
        with NamedTemporaryFile('w', encoding='utf-8', dir=self.folder, delete=False) as stream:
            temp = Path(stream.name)
            stream.write('\n'.join(lines))
        try:
            temp.replace(self.folder / name)
        finally:
            temp.unlink(missing_ok=True)
        os.startfile(self.folder)
