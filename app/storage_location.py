"""Shared data location; switch only after both app processes have stopped."""
from contextlib import closing
import json
import os
import shutil
import sqlite3
from pathlib import Path
from .paths import project_root


def config_path():
    return Path(os.environ.get('EXPLORER_DICT_STORAGE_CONFIG') or project_root() / 'storage-location.json')


def read():
    path = config_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding='utf-8'))


def write(value):
    path = config_path()
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def validate(source, destination):
    source = Path(source).resolve()
    target = Path(destination).expanduser()
    if not target.is_absolute():
        raise ValueError('请输入完整的文件夹路径。')
    target = target.resolve()
    if target == source:
        return target
    if target in source.parents or source in target.parents:
        raise ValueError('新目录不能包含当前目录，也不能位于当前目录内。')
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ValueError('请选择空文件夹，避免覆盖已有资料。')
    return target


def schedule(source, destination):
    source = Path(source).resolve()
    target = validate(source, destination)
    config = read()
    config['active'] = str(source)
    config.pop('error', None)
    if target == source:
        config.pop('pending', None)
    else:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / '.explorerdict-write-test'
        probe.write_bytes(b'')
        probe.unlink()
        config['pending'] = str(target)
    write(config)
    return config


def apply_pending(source, *, components_stopped=True):
    """Caller must stop capture and backend first. Original data is retained."""
    config = read()
    if not config.get('pending'):
        return False
    source = Path(source).resolve()
    if Path(config.get('active', '')).resolve() != source:
        return False
    if not components_stopped:
        config['error'] = '尚未确认悬浮取词组件退出，本次未迁移资料。请完全退出后重试。'
        write(config)
        return False
    try:
        target = validate(source, config['pending'])
        if target == source:
            config.pop('pending', None); write(config); return False
        if any(p.is_symlink() or (hasattr(p,'is_junction') and p.is_junction()) for p in source.rglob('*')):
            raise ValueError('目录包含链接，请先移除链接或选择其他目录。')
        target.mkdir(parents=True, exist_ok=True)
        ignored = {'workspace-session-v7.json','capture-status.json', '.reader-instance.json', '.reader-instance.tmp'}
        for item in source.iterdir():
            if item.name in ignored or item.name.endswith(('-wal','-shm')):
                continue
            dest = target / item.name
            if item.is_file() and item.suffix == '.sqlite3':
                with closing(sqlite3.connect(item)) as old, closing(sqlite3.connect(dest)) as new:
                    old.backup(new)
                    if new.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise ValueError('数据库校验未通过。')
            elif item.is_dir():
                shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)
        config.update(active=str(target)); config.pop('pending', None); config.pop('error', None)
        write(config)
        return True
    except Exception:
        config['error'] = '迁移未完成，继续使用原目录。请检查空间和权限，并重新选择空目录。'
        write(config)
        return False
