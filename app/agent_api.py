"""预留的**只读**本地接口（不是 Agent）。

只暴露两类能力：
  * list_batches()          读取批次
  * search_entries(q)       搜索词条

约束（SPEC.md FR-13）：
* 使用 `mode=ro` 的 SQLite 连接，物理上无法写入；
* 不联网、不自动执行任何动作、不做决策；
* 命令行仅供人工/脚本查询。

用法：
    python -m app.agent_api batches
    python -m app.agent_api search 关键词 --limit 20
    python -m app.agent_api entry 12
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import paths
from .db import Database


def open_readonly(db_file: str | Path | None = None) -> Database:
    path = Path(db_file) if db_file else paths.db_path()
    if not Path(path).exists():
        raise FileNotFoundError(f"数据库不存在: {path}")
    return Database(path, read_only=True)


def _batch_dict(row) -> dict:
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "source_key": row["source_key"],
        "source_kind": row["source_kind"],
        "pinned": bool(row["pinned"]),
        "entry_count": int(row["entry_count"]) if "entry_count" in row.keys() else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _entry_dict(row) -> dict:
    try:
        examples = json.loads(row["examples"] or "[]")
    except json.JSONDecodeError:
        examples = []
    return {
        "id": int(row["id"]),
        "batch_id": int(row["batch_id"]),
        "term": row["term"],
        "context": row["context"],
        "source_app": row["source_app"],
        "source_title": row["source_title"],
        "source_url": row["source_url"],
        "source_confidence": row["source_confidence"],
        "capture_method": row["capture_method"],
        "captured_at": row["captured_at"],
        "repeat_count": int(row["repeat_count"]),
        "explain_status": row["explain_status"],
        "one_line": row["one_line"],
        "detail": row["detail"],
        "examples": examples,
    }


def list_batches(db_file: str | Path | None = None) -> list[dict]:
    db = open_readonly(db_file)
    try:
        return [_batch_dict(r) for r in db.list_batches()]
    finally:
        db.close()


def search_entries(
    query: str = "", limit: int = 50, batch_id: int | None = None,
    db_file: str | Path | None = None,
) -> list[dict]:
    db = open_readonly(db_file)
    try:
        return [_entry_dict(r) for r in db.list_entries(batch_id=batch_id, query=query, limit=limit)]
    finally:
        db.close()


def get_entry(entry_id: int, db_file: str | Path | None = None) -> dict | None:
    db = open_readonly(db_file)
    try:
        row = db.get_entry(entry_id)
        return _entry_dict(row) if row else None
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="app.agent_api", description="探索词典只读本地查询接口（不是自主 Agent）"
    )
    parser.add_argument("--db", default=None, help="数据库路径（默认项目内 data/）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("batches", help="列出所有批次")

    p_search = sub.add_parser("search", help="搜索词条")
    p_search.add_argument("query", nargs="?", default="")
    p_search.add_argument("--limit", type=int, default=50)
    p_search.add_argument("--batch", type=int, default=None)

    p_entry = sub.add_parser("entry", help="按 id 读取词条")
    p_entry.add_argument("entry_id", type=int)

    args = parser.parse_args(argv)
    try:
        if args.cmd == "batches":
            data = list_batches(args.db)
        elif args.cmd == "search":
            data = search_entries(args.query, args.limit, args.batch, args.db)
        else:
            data = get_entry(args.entry_id, args.db)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        if isinstance(data, list):
            for item in data:
                if "term" in item:
                    print(f"[{item['id']}] {item['term']}  <- {item['source_title']}  "
                          f"({item['captured_at']})")
                else:
                    pin = "★" if item["pinned"] else " "
                    print(f"{pin} [{item['id']}] {item['name']}  共 {item['entry_count']} 条")
        elif data:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print("(未找到)", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
