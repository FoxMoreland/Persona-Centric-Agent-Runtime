"""Build the Windows Community candidate from a locked Python environment."""

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import zipfile

from .build_desktop_bundle import SOURCE, build
from persona_runtime.release_notices import release_metadata


RUNTIME_DLLS = ('msvcp140.dll', 'msvcp140_1.dll', 'msvcp140_2.dll', 'vcruntime140.dll',
                'vcruntime140_1.dll', 'concrt140.dll')


def stage_python(destination, redist):
    base = Path(sys.base_prefix)
    site = Path(sys.prefix) / 'Lib/site-packages'
    if sys.version_info[:2] != (3, 12) or struct.calcsize('P') != 8 or base == Path(sys.prefix):
        raise ValueError('Build with a locked Windows x64 CPython 3.12 virtual environment.')
    destination.mkdir()
    for name in ('python.exe', 'pythonw.exe', 'python312.dll', 'python3.dll', 'LICENSE.txt'):
        shutil.copy2(base / name, destination / name)
    for name in RUNTIME_DLLS:
        shutil.copy2(redist / name, destination / name)
    ignore = shutil.ignore_patterns('__pycache__', '*.pyc', 'site-packages')
    for name in ('DLLs', 'Lib', 'tcl'):
        shutil.copytree(base / name, destination / name, ignore=ignore)
    packages = destination / 'Lib/site-packages'
    shutil.copytree(site, packages, ignore=ignore)
    (destination / 'python312._pth').write_text(
        '.\nLib\nDLLs\nLib/site-packages\n..\nimport site\n', encoding='ascii')
    return sorted((item.metadata['Name'], item.version)
                  for item in importlib.metadata.distributions(path=[str(packages)]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', required=True, type=Path)
    parser.add_argument('--public-model', required=True, type=Path)
    parser.add_argument('--webview2', required=True, type=Path)
    parser.add_argument('--vc-runtime', required=True, type=Path)
    parser.add_argument('--iscc', required=True, type=Path)
    parser.add_argument('--field-test', action='store_true')
    args = parser.parse_args()
    work = args.work.resolve()
    if work.is_relative_to(SOURCE):
        raise ValueError('The build directory must be outside the checkout.')
    work.mkdir(parents=True)
    kit = work / 'desktop-source.zip'
    payload = work / 'payload'
    payload.mkdir()
    build(kit, args.public_model, profile='scene-desktop')
    keep = ('persona_runtime/', 'public-model/', 'docs/licenses/', 'templates/', 'scripts/')
    roots = {'LICENSE', 'pyproject.toml', 'uv.lock', 'model-assets.json', 'desktop-kit.json'}
    with zipfile.ZipFile(kit) as archive:
        for name in archive.namelist():
            if name.startswith(keep) or name in roots:
                archive.extract(name, payload)
    for name in ('社区版.md', '历史聊天接续.md', 'DeepSeek 对话导入.md'):
        shutil.copy2(SOURCE / 'docs/user' / name, payload / name)
    (payload / 'persona_runtime/community-edition').write_text('community\n', encoding='ascii')
    if args.field_test:
        (payload / 'persona_runtime/community-field-test').write_text('field-test\n', encoding='ascii')
    packages = stage_python(payload / 'python', args.vc_runtime.resolve())
    subprocess.run(['cl.exe', '/nologo', '/std:c++17', '/MT', '/EHsc', '/utf-8', '/W4', '/WX',
                    str(SOURCE / 'scripts/community_launcher.cpp'), '/link', 'user32.lib', 'shell32.lib',
                    '/subsystem:windows', f'/out:{payload / "PCAR.exe"}'], cwd=work, check=True)
    test_define = ['/DFieldTest=1'] if args.field_test else []
    version = release_metadata()["version"]
    setup_name = f'PCAR-{version}-Windows-x64' + ('-Test-Setup' if args.field_test else '-Setup')
    subprocess.run([str(args.iscc.resolve()), *test_define, f'/DFormalVersion={version}',
                    f'/DFormalFileVersion={version}.0', f'/DCandidateName={setup_name}', f'/DPayload={payload}',
                    f'/DWebView2={args.webview2.resolve()}', f'/O{work}',
                    str(SOURCE / 'scripts/community_setup.iss')], check=True)
    name = setup_name + '.exe'
    installer = work / name
    with installer.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    report = {'candidate': installer.name, 'sha256': digest, 'bytes': installer.stat().st_size,
              'python': sys.version.split()[0], 'packages': dict(packages)}
    (work / 'build.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(installer)
    print(digest)


if __name__ == '__main__':
    main()
