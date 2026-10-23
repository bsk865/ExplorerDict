"""Freeze a reviewed native stage without copying user data or overwriting releases."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import zipfile

from audit_native_package import audit

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('destination', type=Path)
    parser.add_argument('--installation-report', type=Path, required=True)
    args = parser.parse_args()
    destination = args.destination.resolve()
    if destination.exists():
        raise ValueError('Stage directory already exists')
    package = ROOT / 'artifacts/native-build/dist/探索词典-原生版'
    installer = ROOT / 'artifacts/native-installer/ExplorerDict-Native-1.0.0-Setup.exe'
    report = audit(package)
    assert report['passed'], report['errors']
    installed = json.loads(args.installation_report.read_text(encoding='utf-8'))
    assert installed['passed'] and installed['installer_sha256'] == digest(installer)
    built = json.loads((installer.parent / 'package-audit.json').read_text(encoding='utf-8'))
    assert built['executables_sha256'] == report['executables_sha256']
    destination.mkdir(parents=True)
    shutil.copytree(package, destination / 'app')
    shutil.copy2(installer, destination / 'ExplorerDict-Native-Stage-Setup.exe')
    handoff = ROOT / 'docs/任务交接-原生迁移-20261023.md'
    shutil.copy2(handoff, destination / 'HANDOFF.md')
    shutil.copy2(ROOT / 'docs/阶段交付说明-20261023.md', destination / 'README.md')
    sources = []
    suffixes = {'.py', '.pyw', '.ps1', '.cs', '.iss', '.txt', '.json', '.js', '.css', '.html',
                '.svg', '.png', '.webp', '.ico', '.woff', '.woff2'}
    for folder in ('app', 'tests', 'tools', 'web', 'assets', 'scripts'):
        sources.extend(p for p in (ROOT / folder).rglob('*') if p.is_file()
                       and p.suffix in suffixes and '__pycache__' not in p.parts and not p.is_symlink())
    sources.extend(p for p in ROOT.iterdir() if p.is_file() and
                   (p.suffix in {'.py', '.pyw', '.spec'} or p.name in
                    {'requirements.txt', 'requirements-reading.txt', 'LICENSE', '.gitignore'}))
    sources.extend([handoff, ROOT / 'docs/原生桌面迁移.md', ROOT / 'docs/原生版构建与安装.md',
                    ROOT / 'docs/阶段交付说明-20261023.md'])
    archive = destination / 'source-snapshot.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as output:
        for path in sorted(set(sources)):
            output.write(path, path.relative_to(ROOT).as_posix())
    copied = audit(destination / 'app')
    assert copied['passed'] and copied['executables_sha256'] == report['executables_sha256']
    assert (copied['file_count'], copied['bytes']) == (report['file_count'], report['bytes'])
    with zipfile.ZipFile(archive) as output:
        assert output.testzip() is None
    assert digest(destination / 'ExplorerDict-Native-Stage-Setup.exe') == digest(installer)
    manifest = {'stage': destination.name, 'git_head': subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'source_is_working_tree_snapshot': True, 'source_files': len(set(sources)),
        'source_sha256': digest(archive), 'installer_sha256': digest(installer),
        'executables_sha256': copied['executables_sha256'],
        'installation_report': str(args.installation_report.resolve()),
        'os_registration_tested': installed['os_registration_tested'],
        'user_data_included': False, 'passed': True}
    (destination / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == '__main__':
    main()
