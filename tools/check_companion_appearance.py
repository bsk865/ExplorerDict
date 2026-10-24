"""Run appearance checks in an isolated source or packaged companion process."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parent = ROOT/'artifacts/appearance-checks'
    parent.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='appearance-', dir=parent))
    (directory/'.appearance-check').touch()
    executable = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else None
    command = [str(executable)] if executable else [sys.executable, '-m', 'app.main']
    environment = dict(os.environ, EXPLORER_DICT_DATA_DIR=str(directory),
                       EXPLORER_DICT_STORAGE_CONFIG=str(directory/'location.json'), PYTHONIOENCODING='utf-8')
    with (directory/'process.log').open('w', encoding='utf-8') as output:
        result = subprocess.run(command+['--check-appearance', str(directory)],
                                cwd=executable.parent if executable else ROOT, env=environment,
                                stdout=output, stderr=output, timeout=40)
    path = directory/'appearance-verification.json'
    report = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {'passed': False}
    if report.get('passed'):
        sys.path.insert(0, str(ROOT))
        from app.reading_preferences import colors
        for case in report['cases']:
            expected = colors(case['palette'], case['mode'])
            if any(case['colors'][key] != expected[token].lower() for key, token in (
                    ('bg', 'bg'), ('input_bg', 'bg'), ('ink', 'ink'), ('accent', 'accent'))):
                report.update(passed=False, error='Packaged palette differs from current source')
        report['matches_source_colors'] = report['passed']
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(dict(directory=str(directory), exit_code=result.returncode, result=report), ensure_ascii=False))
    return 0 if result.returncode == 0 and report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
