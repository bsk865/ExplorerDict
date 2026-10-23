"""Exercise install/update/uninstall in an isolated directory, preserving evidence."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
import tempfile
import winreg

ROOT = Path(__file__).resolve().parents[1]
APP_KEY = r'Software\Microsoft\Windows\CurrentVersion\Uninstall\{4EF8E5E1-C970-4BE0-9FA5-627AD8E30E66}_is1'


def registered():
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APP_KEY, 0, winreg.KEY_READ | view):
                return True
        except FileNotFoundError:
            pass
    return False


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('installer', type=Path)
    parser.add_argument('--isolated', action='store_true', help='Skip OS registration and shortcuts; verify file lifecycle only')
    args = parser.parse_args()
    if registered():
        raise RuntimeError('An existing native installation is registered; isolated verification will not replace it.')
    installer = args.installer.resolve()
    parent = ROOT / 'artifacts/native-install-checks'
    parent.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='install-', dir=parent))
    target = directory / 'application'
    temp = directory / 'temp'
    temp.mkdir()
    environment = dict(os.environ, TEMP=str(temp), TMP=str(temp))
    report = {'directory': str(directory), 'installer_sha256': digest(installer), 'passed': False,
              'os_registration_tested': not args.isolated}
    def execute(command, log):
        with (directory / log).open('w', encoding='utf-8') as output:
            result = subprocess.run(command, cwd=directory, env=environment, stdout=output, stderr=output, timeout=180)
        if result.returncode:
            raise RuntimeError(f'{log}: process exit {result.returncode}')
    def install(stage):
        execute([str(installer), '/SP-', '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CURRENTUSER',
                 '/NOICONS', '/TASKS=', '/ISOLATEDCHECK='+('1' if args.isolated else '0'),
                 '/DIR='+str(target), '/LOG='+str(directory/(stage+'.log'))], stage+'-process.log')
        if not (target / '探索词典-原生版.exe').is_file() or (not args.isolated and not registered()):
            raise RuntimeError('Installed application or registration missing')
    try:
        install('install')
        report['installed'] = True
        data = target / 'data'
        data.mkdir(exist_ok=True)
        sentinel = data / 'retained-user-content.txt'
        sentinel.write_text('Installer verification: user content must survive update and uninstall.', encoding='utf-8')
        database = data / 'installer-fixture.sqlite3'
        with sqlite3.connect(database) as connection:
            connection.execute('CREATE TABLE retained (id INTEGER PRIMARY KEY, content TEXT)')
            connection.execute('INSERT INTO retained(content) VALUES (?)', ('User note fixture',))
        config = target / 'storage-location.json'
        config.write_text(json.dumps({'active': str(data)}), encoding='utf-8')
        retained = {str(path): digest(path) for path in (sentinel, database, config)}
        execute([sys.executable, str(ROOT/'tools/check_native_desktop.py'), str(target/'探索词典-原生版.exe')], 'installed-workflows.log')
        report['installed_workflows'] = True
        install('update')
        if any(digest(Path(path)) != value for path, value in retained.items()):
            raise RuntimeError('Update changed retained user content')
        report['update_preserved_data'] = True
        uninstaller = target / 'unins000.exe'
        if not uninstaller.is_file():
            raise RuntimeError('Uninstaller missing')
        execute([str(uninstaller), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
                 '/LOG='+str(directory/'uninstall.log')], 'uninstall-process.log')
        if (target/'探索词典-原生版.exe').exists() or registered():
            raise RuntimeError('Uninstall left the executable or registration')
        if any(not Path(path).is_file() or digest(Path(path)) != value for path, value in retained.items()):
            raise RuntimeError('Uninstall removed retained user content')
        report['uninstall_preserved_data'] = True
        report['passed'] = True
    except Exception as exc:
        report['error'] = str(exc)
    (directory/'installer-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
