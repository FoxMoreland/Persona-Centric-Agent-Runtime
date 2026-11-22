"""Initialize an installed Community edition with its private Python and local model."""

import argparse
import ctypes
import os
from pathlib import Path
import socket
import sys

from . import community
from .config import ConfigStore, atomic_json
from .installation import activated, prepare_model, read_json, validate_home, _install_lock, _frontend_probe, _frontend_packages
from .models import RuntimeErrorBase
from .paths import RuntimePaths
from .storage import Store


def prepare(source, home):
    paths = RuntimePaths(validate_home(home, source_root=source))
    marker = paths.home / 'runtime/community-edition.json'
    with _install_lock(paths):
        if marker.exists():
            if read_json(marker) != {'edition': 'community', 'version': 1}:
                raise ValueError('社区版数据目录标记不匹配。')
        else:
            entries = {path.name for path in paths.home.iterdir()}
            runtime = marker.parent
            if entries != {'runtime'} or any(path.name != 'webview-install.lock' for path in runtime.iterdir()):
                raise ValueError('请选择社区版的专用数据目录。')
            atomic_json(marker, {'edition': 'community', 'version': 1})
        config = ConfigStore(paths)
        new = not config.file.exists()
        config.initialize()
        if new:
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                config.update({'service_port': listener.getsockname()[1]})
        Store(paths).initialize()
        prepare_model(paths, local_source=source / 'public-model', source_root=source)
    return paths


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--home', type=Path)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    report = None
    stage = '启动入口'
    outcome = None
    failure = None
    try:
        if not community.enabled():
            raise ValueError('请从社区版程序目录启动。')
        source = Path(__file__).resolve().parents[1]
        local = os.environ.get('LOCALAPPDATA')
        if not local and not args.home:
            raise ValueError('无法定位当前用户的数据目录。')
        field_test = community.field_test_enabled()
        data_name = 'PCAR Community Test Data' if field_test else 'PCAR Community Data'
        home = args.home if args.home else Path(local) / data_name
        if field_test and not args.check:
            from .community_field_report import FieldReport
            report = FieldReport(home.parent / 'PCAR Community Test Reports', source, home)
        stage = '初始化数据与本地模型'
        paths = prepare(source, home)
        os.environ['PCAR_PYTHON'] = str(Path(sys.executable).with_name('python.exe'))
        with activated(paths):
            if args.check:
                from .installation import doctor
                import json
                report = doctor(paths.home, source_root=source)
                report['checks'].append({
                    'name': 'webview_dependencies',
                    'ready': _frontend_probe(os.environ['PCAR_PYTHON'], _frontend_packages(source)[1]),
                })
                report['ready'] = all(item['ready'] for item in report['checks'] if item['name'] != 'system_proxy')
                print(json.dumps(report, ensure_ascii=True))
                return 0 if report['ready'] else 2
            from .workbench_webview import run
            stage = '工作台启动与运行'
            outcome = run(paths)
        return 0
    except Exception as error:
        if report is None and not isinstance(error, (OSError, RuntimeErrorBase, ValueError)):
            raise
        failure = type(error).__name__
        if args.check:
            print(str(error), file=sys.stderr)
        else:
            ctypes.windll.user32.MessageBoxW(None, str(error), 'PCAR 社区版', 0x10)
        return 1
    finally:
        if report is not None and (outcome is not None or failure is not None):
            try:
                report.finish(outcome, stage, failure)
            except Exception:
                ctypes.windll.user32.MessageBoxW(
                    None, '无法写入本次验收日志。请检查测试报告目录的权限和剩余空间。',
                    'PCAR 社区测试版', 0x10)


if __name__ == '__main__':
    sys.exit(main())
