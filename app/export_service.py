"""导出词条为 CSV / Markdown 表格（B3）。

定位
----
**只提供数据出口**，不做闪卡、不做复习算法：用户把词表导出去，接进 Anki /
Obsidian / Excel 自己用。导出的内容就是用户自己的数据（词语、上下文、来源、
释义、例子、标签、时间），一条不加工。

两个硬约束
----------
* **不弹「另存为」**：冻结运行时没有 ``tkinter.filedialog``，因此目标目录固定是
  :func:`app.paths.exports_dir`（``<data>/exports``），界面把路径显示出来 +
  ``os.startfile`` 打开文件夹；
* **CSV 用 ``utf-8-sig``**：Excel 双击打开中文不乱码（BOM 只多三个字节）。
"""
from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .logging_setup import get_logger
from . import paths

log = get_logger("export")

#: 文件名前缀（后面接范围与时间戳）
FILE_PREFIX = "探索词典"
#: 范围标签最多保留多少字（它来自用户输入的主题名 / 搜索词）
MAX_STEM_CHARS = 30
#: Windows 文件名不允许的字符（外加控制字符）
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

#: 导出列：(数据库列, 表头)。顺序就是文件里的列顺序。
COLUMNS: tuple[tuple[str, str], ...] = (
    ("term", "词语"),
    ("context", "上下文"),
    ("source_title", "来源标题"),
    ("source_url", "来源链接"),
    ("source_app", "来源应用"),
    ("one_line", "释义"),
    ("examples", "例子"),
    ("tags", "标签"),
    ("captured_at", "捕获时间"),
    ("repeat_count", "重复次数"),
)


@dataclass(frozen=True)
class ExportResult:
    """一次导出的结果：两个文件的路径 + 条数（界面据此显示反馈）。"""

    csv_path: Path
    md_path: Path
    count: int
    scope_label: str

    @property
    def directory(self) -> Path:
        return self.csv_path.parent

    def summary(self) -> str:
        return f"已导出 {self.count} 条到 {self.directory}"


def safe_stem(name: str) -> str:
    """把范围标签变成**安全的文件名片段**（空 / 全是非法字符时退回 ``导出``）。"""
    text = _ILLEGAL.sub(" ", str(name or ""))
    text = " ".join(text.split()).strip(" .")
    text = text[:MAX_STEM_CHARS].strip(" .")
    return text or "导出"


def _examples_text(raw) -> str:
    """``examples`` 列是 JSON 数组；解析失败就原样返回（绝不因为一条脏数据崩掉导出）。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        data = json.loads(text)
    except Exception:
        return text
    if isinstance(data, list):
        return "；".join(str(x).strip() for x in data if str(x).strip())
    return text


def row_values(row, tags=()) -> list[str]:
    """把一行数据库记录翻译成**导出用的字符串列**（与 :data:`COLUMNS` 一一对应）。

    ``tags`` 传这条词条的标签（``db.tags_for`` / ``db.tags_for_entries`` 的结果）：
    数据库列里没有标签，标签在 ``entry_tags`` 关系表里，所以由调用方带进来。
    """
    out: list[str] = []
    for key, _label in COLUMNS:
        if key == "examples":
            out.append(_examples_text(row["examples"]))
        elif key == "tags":
            out.append("  ".join(f"#{t}" for t in (tags or [])))
        elif key == "repeat_count":
            out.append(str(int(row["repeat_count"] or 0)))
        else:
            try:
                value = row[key]
            except (KeyError, IndexError):
                value = ""
            out.append("" if value is None else str(value))
    return out


def table(rows, tags_map: dict | None = None) -> tuple[list[str], list[list[str]]]:
    """返回 ``(表头, 数据行)`` —— CSV 与 Markdown 共用同一份内容。"""
    header = [label for _key, label in COLUMNS]
    mapping = tags_map or {}
    body: list[list[str]] = []
    for row in rows or ():
        try:
            eid = int(row["id"])
        except Exception:
            eid = 0
        body.append(row_values(row, mapping.get(eid, ())))
    return header, body


def csv_cell(value: str) -> str:
    """Excel 公式注入防护：以 ``=`` ``+`` ``-`` ``@`` 开头的单元格前面加一个单引号。

    划词内容来自网页/PDF，可能以 ``=``（公式）或 ``-``（列表符号）开头；直接写进 CSV
    会被 Excel 当公式执行。加一个单引号是最省事又通用的做法（Excel 会把它当文本前缀
    显示，其它工具看到的就是字面量）。
    """
    text = str(value or "")
    if text[:1] in ("=", "+", "-", "@"):
        return "'" + text
    return text


def csv_text(header, body) -> str:
    """CSV 正文（``\\r\\n`` 行尾，Excel 友好）；每个单元格过一遍 :func:`csv_cell`。"""
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow([csv_cell(v) for v in header])
    for line in body:
        writer.writerow([csv_cell(v) for v in line])
    return buf.getvalue()


def markdown_cell(value: str) -> str:
    """Markdown 表格单元格转义：竖线会破坏表格结构，换行会破坏行结构。"""
    text = str(value or "").replace("|", "\\|")
    return " ".join(text.split())


def markdown_text(header, body, *, scope_label: str = "全部词语",
                  created_at: datetime | None = None) -> str:
    """Markdown 表格（带一段元信息头：范围 / 条数 / 时间）。"""
    stamp = (created_at or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "# 探索词典 · 词条导出",
        "",
        f"- 范围：{scope_label}",
        f"- 条数：{len(body)}",
        f"- 导出时间：{stamp}",
        "",
        "> 名词解释：**上下文**是划词当时原文里的那句话；**来源**是当时的窗口标题"
        "（未做后台采集，也没有再联网）。",
        "",
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    for line in body:
        lines.append("| " + " | ".join(markdown_cell(c) for c in line) + " |")
    lines.append("")
    return "\n".join(lines)


def unique_path(path: Path) -> Path:
    """同名文件已存在时追加 ``-2`` / ``-3``…（**绝不覆盖**用户上一次的导出）。"""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(2, 1000):
        candidate = path.with_name(f"{stem}-{i}{suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{stem}-{int(datetime.now().timestamp())}{suffix}")


def export_entries(rows, *, scope_label: str = "全部词语", directory=None,
                   tags_map: dict | None = None, now: datetime | None = None,
                   ) -> ExportResult:
    """把 ``rows`` 写成一对文件（CSV + Markdown），返回 :class:`ExportResult`。

    同一次导出写两份：CSV 给 Excel / Anki 导入，Markdown 给 Obsidian / 笔记软件。
    ``directory`` 默认 :func:`app.paths.exports_dir`；测试传临时目录。
    """
    stamp = now or datetime.now()
    target = Path(directory) if directory else paths.exports_dir()
    target.mkdir(parents=True, exist_ok=True)
    header, body = table(rows, tags_map)
    base = f"{FILE_PREFIX}-{safe_stem(scope_label)}-{stamp.strftime('%Y%m%d-%H%M%S')}"

    csv_path = unique_path(target / f"{base}.csv")
    csv_path.write_text(csv_text(header, body), encoding="utf-8-sig", newline="")
    md_path = unique_path(target / f"{base}.md")
    md_path.write_text(
        markdown_text(header, body, scope_label=scope_label, created_at=stamp),
        encoding="utf-8", newline="")
    log.info("导出 %d 条 → %s / %s", len(body), csv_path.name, md_path.name)
    return ExportResult(csv_path=csv_path, md_path=md_path, count=len(body),
                        scope_label=scope_label)
