"""Run isolated settings, process-exit migration, and fresh-process reopening."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from native_relocation_fixture import prepare, check, ROOT


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('executable', type=Path, nargs='?')
    parser.add_argument('--with-companion', action='store_true')
    args = parser.parse_args()
    root = prepare()
    report = {'directory': str(root), 'passed': False, 'capture_lifecycle_tested': args.with_companion}
    environment = dict(os.environ, QT_QPA_PLATFORM='offscreen')
    environment.pop('EXPLORER_DICT_DATA_DIR', None)
    environment.pop('EXPLORER_DICT_STORAGE_CONFIG', None)
    command = [sys.executable, str(ROOT / 'native_desktop.py')]
    cwd = ROOT
    if args.executable:
        executable = args.executable.resolve()
        command = [str(executable)]
        cwd = executable.parent
        environment.pop('PYTHONPATH', None)
        environment.pop('PYTHONHOME', None)
        system = Path(environment.get('SystemRoot', 'C:/Windows'))
        environment['PATH'] = str(system / 'System32') + os.pathsep + str(system)
    try:
        for stage in ('schedule', 'reopen'):
            with (root / (stage + '-process.log')).open('w', encoding='utf-8') as log:
                options = ['--verify-relocation', str(root), '--relocation-stage', stage]
                if args.with_companion:
                    options += ['--relocation-capture']
                process = subprocess.Popen(command + options, cwd=cwd, env=environment, stdout=log, stderr=log)
                try:
                    code = process.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True)
                    process.wait(timeout=10)
                    raise
            runtime = json.loads((root / (stage + '-runtime.json')).read_text(encoding='utf-8'))
            assert code == 0 and runtime['passed'], runtime
            assert runtime['frozen'] == bool(args.executable)
            report[stage] = runtime
            if args.with_companion:
                companion = json.loads((root/(stage+'-companion.json')).read_text(encoding='utf-8'))
                assert companion['passed'] and companion['database_closed'] and companion['normal_exit'], companion
                assert companion['frozen'] == bool(args.executable)
                report[stage+'_companion'] = companion
            if stage == 'schedule':
                assert check(root) == 0, 'Exit migration changed fixture contents'
                report['exit_migration_verified'] = True
        report['passed'] = True
    except Exception as exc:
        report['error'] = type(exc).__name__ + ': ' + str(exc)
    (root / 'workflow-verification.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
