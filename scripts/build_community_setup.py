"""Build the Windows Community candidate from a locked Python environment."""

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
from urllib.parse import urlsplit
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


def signing_command(parser, args):
    if not any((args.signtool, args.sign_thumbprint, args.timestamp_url, args.sign_machine_store)):
        return []
    if not args.signtool or not args.timestamp_url or not re.fullmatch(r"[0-9a-fA-F]{40}", args.sign_thumbprint or ""):
        parser.error("Signing requires --signtool, a 40-digit --sign-thumbprint and --timestamp-url.")
    if not args.signtool.is_file():
        parser.error("--signtool must point to the Windows SDK SignTool executable.")
    try:
        endpoint = urlsplit(args.timestamp_url)
    except ValueError:
        parser.error("--timestamp-url must be an RFC 3161 HTTP(S) endpoint.")
    if (endpoint.scheme not in ("https", "http") or not endpoint.hostname or endpoint.username is not None
            or endpoint.fragment or any(ch.isspace() or ch == '"' for ch in args.timestamp_url)):
        parser.error("--timestamp-url must be an RFC 3161 HTTP(S) endpoint.")
    store = ["/sm"] if args.sign_machine_store else []
    return [str(args.signtool.resolve()), "sign", *store, "/s", "My", "/sha1", args.sign_thumbprint,
            "/fd", "SHA256", "/tr", args.timestamp_url, "/td", "SHA256"]


def inno_signing_options(command, cache):
    # ISCC expands $q as a quote and $f as the quoted input filename.
    quoted = " ".join("$q" + arg.replace("$", "$$") + "$q" for arg in command)
    return ["/DAuthenticode=1", f"/DSignedUninstallerCache={cache}",
            f"--signtool=pcar_authenticode={quoted} $f"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', required=True, type=Path)
    parser.add_argument('--public-model', required=True, type=Path)
    parser.add_argument('--webview2', required=True, type=Path)
    parser.add_argument('--vc-runtime', required=True, type=Path)
    parser.add_argument('--iscc', required=True, type=Path)
    parser.add_argument('--field-test', action='store_true')
    parser.add_argument('--signtool', type=Path, help='Path to the Windows SDK SignTool executable.')
    parser.add_argument('--sign-thumbprint', help='Code signing certificate thumbprint in the My store.')
    parser.add_argument('--sign-machine-store', action='store_true', help='Choose LocalMachine instead of CurrentUser.')
    parser.add_argument('--timestamp-url', help='RFC 3161 timestamp endpoint.')
    args = parser.parse_args()
    signer = signing_command(parser, args)
    work = args.work.resolve()
    if work.is_relative_to(SOURCE):
        raise ValueError('The build directory must be outside the checkout.')
    work.mkdir(parents=True)
    kit = work / 'desktop-source.zip'
    payload = work / 'payload'
    payload.mkdir()
    launcher = payload / 'PCAR.exe'
    subprocess.run(['cl.exe', '/nologo', '/std:c++17', '/MT', '/EHsc', '/utf-8', '/W4', '/WX',
                    str(SOURCE / 'scripts/community_launcher.cpp'), '/link', 'user32.lib', 'shell32.lib',
                    '/subsystem:windows', f'/out:{launcher}'], cwd=work, check=True)
    signing_options = []
    if signer:
        verify = [signer[0], 'verify', '/pa', '/all', '/tw']
        subprocess.run([*signer, str(launcher)], check=True)
        subprocess.run([*verify, str(launcher)], check=True)
        uninstaller_cache = work / 'signed-uninstaller'
        uninstaller_cache.mkdir()
        signing_options = inno_signing_options(signer, uninstaller_cache)
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
    test_define = ['/DFieldTest=1'] if args.field_test else []
    version = release_metadata()["version"]
    setup_name = f'PCAR-{version}-Windows-x64' + ('-Test-Setup' if args.field_test else '-Setup')
    subprocess.run([str(args.iscc.resolve()), *test_define, *signing_options, f'/DFormalVersion={version}',
                    f'/DFormalFileVersion={version}.0', f'/DCandidateName={setup_name}', f'/DPayload={payload}',
                    f'/DWebView2={args.webview2.resolve()}', f'/O{work}',
                    str(SOURCE / 'scripts/community_setup.iss')], check=True)
    name = setup_name + '.exe'
    installer = work / name
    signature = {'state': 'unsigned'}
    if signer:
        # Inno Setup caches the x64 uninstall executable with an .e64 extension.
        uninstallers = list(uninstaller_cache.glob('uninst-*.e64'))
        if len(uninstallers) != 1:
            raise RuntimeError('Inno Setup did not produce exactly one signed uninstaller.')
        subprocess.run([*verify, str(installer), str(uninstallers[0])], check=True)
        signature = {'state': 'verified', 'certificate_thumbprint': args.sign_thumbprint.upper()}
    with installer.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    report = {'candidate': installer.name, 'sha256': digest, 'bytes': installer.stat().st_size,
              'python': sys.version.split()[0], 'packages': dict(packages), 'authenticode': signature}
    (work / 'build.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(installer)
    print(digest)


if __name__ == '__main__':
    main()
