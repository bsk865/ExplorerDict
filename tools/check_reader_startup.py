"""Run the packaged window verification against disposable data."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('executable')
    parser.add_argument('--software', action='store_true')
    args = parser.parse_args()
    artifacts = Path(__file__).resolve().parents[1] / 'artifacts' / 'startup-checks'
    artifacts.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='reader-', dir=artifacts))
    env = dict(os.environ, EXPLORER_DICT_DATA_DIR=str(directory),
               EXPLORER_DICT_STORAGE_CONFIG=str(directory / 'storage.json'))
    if args.software:
        env['QTWEBENGINE_CHROMIUM_FLAGS'] = '--disable-gpu'
    env['QT_LOGGING_RULES'] = 'qt.webenginecontext.debug=true'
    with (directory / 'process.log').open('w', encoding='utf-8') as log:
        process = subprocess.Popen([str(Path(args.executable).resolve()), '--verify-window'],
                                   env=env, stdout=log, stderr=log)
        try:
            code = process.wait(timeout=50)
        except subprocess.TimeoutExpired:
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                           capture_output=True)
            code = process.wait(timeout=10)
    report = directory / 'window-verification.json'
    print(json.dumps({'directory': str(directory), 'exit_code': code,
                      'report': json.loads(report.read_text()) if report.exists() else None},
                     ensure_ascii=False))


if __name__ == '__main__':
    main()
