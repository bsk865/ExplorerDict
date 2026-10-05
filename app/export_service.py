"""导出词条：**七种格式选一种**（CSV / Markdown / JSON / JSONL / Anki / PDF / HTML / TXT）。

定位
----
**只提供数据出口**，不做闪卡、不做复习算法：用户把词表导出去，接进 Anki /
Obsidian / Excel 自己用。导出的内容就是用户自己的数据（词语、上下文、来源、
释义、例子、标签、时间），一条不加工。

三种格式各自为谁服务（界面上的选择器按这个写文案）
--------------------------------------------------
* **CSV** → Excel / 表格（``utf-8-sig``，Excel 双击不乱码）；
* **Markdown** → Obsidian / 笔记软件（表格）；
* **JSON / JSONL** → 自己写脚本吃（忠实字段名 + 标签数组）；
* **Anki 导入文本** → 直接导进 Anki（制表符分隔、**无表头**、末列 ``#标签``）；
* **PDF** → 要「发出去 / 打印」的成品（手写 PDF + 嵌入中文字体，见
  :mod:`app.pdf_writer`）；
* **HTML 单页** → 浏览器里看 / 转存（自包含，无外部依赖）；
* **TXT** → 纯文字（粘进任何地方都不带格式）。

三个硬约束
----------
* **不弹「另存为」**：冻结运行时没有 ``tkinter.filedialog``，目标目录来自
  :func:`resolve_directory`（设置里的 ``export.directory``，空 = ``<data>/exports``）；
* **不按勾选筛**：导的是**当前列表范围**（勾选只决定哪些词进导图）—— 这条口径
  是用户确认过的（K1），调用方负责把范围查好再传进来；
* **同名绝不覆盖**：走 :func:`unique_path` 加 ``-2`` / ``-3`` 后缀。
"""
from __future__ import annotations

import csv
import html
import io
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import paths
from .logging_setup import get_logger

log = get_logger("export")

#: 文件名前缀（后面接范围与时间戳）
FILE_PREFIX = "探索词典"
#: 范围标签最多保留多少字（它来自用户输入的主题名 / 搜索词）
MAX_STEM_CHARS = 30
#: Windows 文件名不允许的字符（外加控制字符）
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

#: 导出列：(数据库列, 表头)。顺序就是 CSV / Markdown / TXT / 表格类输出的列顺序。
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

#: JSON / JSONL 里的字段名（英文，便于脚本处理）+ 中文标题
JSON_FIELDS: tuple[tuple[str, str], ...] = (
    ("term", "词语"),
    ("context", "上下文"),
    ("source_title", "来源标题"),
    ("source_url", "来源链接"),
    ("source_app", "来源应用"),
    ("one_line", "释义"),
    ("examples", "例子"),
    ("captured_at", "捕获时间"),
    ("repeat_count", "重复次数"),
)


@dataclass(frozen=True)
class ExportFormat:
    """一种导出格式（选择器里的一行）。"""

    key: str
    label: str
    suffix: str
    note: str
    #: 目标软件（界面上小字提示「给谁用」）
    target: str = ""


#: 界面上的格式清单 —— 顺序就是按钮顺序（用户要求把两种导出分类，这里是词表那一类）
FORMATS: tuple[ExportFormat, ...] = (
    ExportFormat("csv", "CSV", ".csv",
                 "表格；Excel 双击不乱码（带 BOM），也能导进 Anki", "Excel / 表格"),
    ExportFormat("markdown", "Markdown", ".md",
                 "表格；竖线会转义，适合粘贴进笔记", "Obsidian / 笔记"),
    ExportFormat("json", "JSON", ".json",
                 "完整结构（含标签数组与例子数组），适合写脚本读", "脚本 / 程序"),
    ExportFormat("jsonl", "JSONL", ".jsonl",
                 "一行一条 JSON，适合逐行处理 / 喂给大模型", "脚本 / 程序"),
    ExportFormat("anki", "Anki 导入文本", ".txt",
                 "制表符分隔、无表头、末列是 #标签 —— Anki「导入文件」直接认",
                 "Anki"),
    ExportFormat("pdf", "PDF", ".pdf",
                 "排好版的成品（嵌入中文字体，文字可选中可搜索）", "打印 / 分享"),
    ExportFormat("html", "HTML 单页", ".html",
                 "自包含单页，双击用浏览器打开就能看 / 转存", "浏览器"),
    ExportFormat("txt", "纯文本 TXT", ".txt",
                 "纯文字，粘到哪里都不带格式", "任何地方"),
)
#: 按 key 找格式
FORMATS_BY_KEY: dict[str, ExportFormat] = {item.key: item for item in FORMATS}
#: 默认格式（工具栏「导出」不选就是个 CSV —— 与老行为一致）
DEFAULT_FORMAT = "csv"
#: Anki 那一列用的制表符（Anki 的「导入文件」按制表符分列）
ANKI_SEPARATOR = "\t"


def format_for(key: str) -> ExportFormat:
    """按 key 取格式；不认识就退回 CSV（绝不因为参数脏而崩）。"""
    return FORMATS_BY_KEY.get(str(key or "").strip().lower(),
                              FORMATS_BY_KEY[DEFAULT_FORMAT])


def resolve_directory(configured="", *, fallback=None) -> Path:
    """导出目录：设置里写了就用它，空 / 写坏就用默认（``<data>/exports``）。

    设置里的值允许写 ``%USERPROFILE%\\Desktop`` 这类带环境变量的路径，会展开。
    """
    text = str(configured or "").strip().strip('"')
    if text:
        try:
            expanded = Path(os.path.expandvars(text)).expanduser()
            return expanded
        except (ValueError, OSError):                         # pragma: no cover
            log.warning("导出目录设置值看不懂，退回默认目录：%r", configured)
    return Path(fallback) if fallback else paths.exports_dir()


@dataclass(frozen=True)
class ExportResult:
    """一次导出的结果：文件路径 + 条数 + 格式（界面据此显示反馈）。"""

    path: Path
    count: int
    scope_label: str
    format_key: str = DEFAULT_FORMAT

    @property
    def directory(self) -> Path:
        return self.path.parent

    @property
    def format(self) -> ExportFormat:
        return format_for(self.format_key)

    @property
    def csv_path(self) -> Path:
        """兼容旧调用（老代码/老测试按这个名字取路径）。"""
        return self.path

    @property
    def md_path(self) -> Path:
        """兼容旧调用：只有 Markdown 导出才有意义，其它格式返回同一个文件。"""
        return self.path

    def summary(self) -> str:
        return f"已导出 {self.count} 条到 {self.path.name}"


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


def _examples_list(raw) -> list[str]:
    """``examples`` 列的**数组**形态（JSON 导出用；脏数据退化成单元素列表）。"""
    text = str(raw or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except Exception:
        return [text]
    if isinstance(data, list):
        return [str(x).strip() for x in data if str(x).strip()]
    return [text]


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
    """返回 ``(表头, 数据行)`` —— CSV / Markdown / TXT / 表格共用同一份内容。"""
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


def records(rows, tags_map: dict | None = None) -> list[dict]:
    """JSON / JSONL 的记录：字段名用英文，标签与例子是**数组**（不是拼接串）。"""
    mapping = tags_map or {}
    out: list[dict] = []
    for row in rows or ():
        try:
            eid = int(row["id"])
        except Exception:
            eid = 0
        tags = [str(t) for t in (mapping.get(eid, ()) or ())]
        item: dict[str, object] = {"id": eid}
        for key, _label in JSON_FIELDS:
            if key == "examples":
                item[key] = _examples_list(row["examples"])
            elif key == "repeat_count":
                item[key] = int(row["repeat_count"] or 0)
            else:
                try:
                    value = row[key]
                except (KeyError, IndexError):
                    value = ""
                item[key] = "" if value is None else str(value)
        item["tags"] = tags
        out.append(item)
    return out


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


def anki_rows(rows, tags_map: dict | None = None) -> list[list[str]]:
    """Anki 导入用的行：**词语 / 释义 / 上下文 / 来源** + 末列 ``#标签``。

    与 CSV 的列不同 —— Anki 的卡片是「正面 → 背面」，所以列顺序按卡片来：
    正面 = 词语，背面 = 释义、上下文、来源。格式是「制表符分隔、无表头」，
    这正是 Anki「导入文件」认的形态（备注/标签列放在最后）。
    """
    mapping = tags_map or {}
    out: list[list[str]] = []
    for row in rows or ():
        try:
            eid = int(row["id"])
        except Exception:
            eid = 0
        tags = " ".join(f"#{t}" for t in (mapping.get(eid, ()) or ()))
        out.append([
            _cell(row, "term"),
            _cell(row, "one_line"),
            _cell(row, "context"),
            _cell(row, "source_title"),
            tags,
        ])
    return out


def _cell(row, key: str) -> str:
    """取一列并按「一行一格」清洗：制表符 / 换行会把 Anki 的分列搞乱。"""
    try:
        value = row[key]
    except (KeyError, IndexError):
        value = ""
    text = "" if value is None else str(value)
    return " ".join(text.replace("\t", " ").split())


def anki_text(rows, tags_map: dict | None = None) -> str:
    """Anki 导入文本（无表头，制表符分隔；``\\n`` 行尾）。"""
    lines = [ANKI_SEPARATOR.join(cells) for cells in anki_rows(rows, tags_map)]
    return "\n".join(lines) + ("\n" if lines else "")


def json_text(rows, tags_map: dict | None = None, *, scope_label: str = "全部词语",
              created_at: datetime | None = None) -> str:
    """JSON：一个对象（元信息 + ``entries`` 数组），缩进 2（人能读、diff 也好看）。"""
    stamp = (created_at or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    payload = {
        "app": FILE_PREFIX,
        "kind": "entries",
        "scope": scope_label,
        "count": len(list(rows or ())),
        "exported_at": stamp,
        "entries": records(rows, tags_map),
    }
    payload["count"] = len(payload["entries"])
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def jsonl_text(rows, tags_map: dict | None = None) -> str:
    """JSONL：一行一条记录（没有外层元信息；要元信息看 JSON）。"""
    lines = [json.dumps(item, ensure_ascii=False) for item in records(rows, tags_map)]
    return "\n".join(lines) + ("\n" if lines else "")


def txt_text(header, body, *, scope_label: str = "全部词语",
             created_at: datetime | None = None) -> str:
    """纯文本：元信息头 + 每条一段「标题 / 字段：值」。

    刻意**不**画表格框线（等宽字体下中文对不齐，反而更难读）。
    """
    stamp = (created_at or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"{FILE_PREFIX} · 词条导出",
        "=" * 24,
        f"范围：{scope_label}",
        f"条数：{len(body)}",
        f"导出时间：{stamp}",
        "",
    ]
    for index, cells in enumerate(body, start=1):
        term = cells[0] if cells else ""
        lines.append(f"[{index}] {term}")
        for label, value in zip(header[1:], cells[1:]):
            if str(value or "").strip():
                lines.append(f"    {label}：{value}")
        lines.append("")
    return "\n".join(lines)


def html_text(header, body, *, scope_label: str = "全部词语",
              created_at: datetime | None = None) -> str:
    """HTML 单页（自包含、无外部依赖；行内 CSS 保证双击打开就好看）。"""
    stamp = (created_at or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    css = """
:root { color-scheme: light; }
body { margin: 0 auto; max-width: 900px; padding: 32px 20px 64px;
       background: #F9F8F6; color: #1C1C1C;
       font-family: "Microsoft YaHei", "PingFang SC", system-ui, sans-serif;
       line-height: 1.65; }
h1 { font-size: 22px; margin: 0 0 6px; }
.meta { color: #6E6C67; font-size: 13px; margin-bottom: 4px; }
.note { color: #9B9993; font-size: 12px; margin-bottom: 26px; }
article { background: #FCFBF8; border: 1px solid #E3E1DC; border-radius: 10px;
          padding: 14px 18px; margin: 0 0 14px; }
article h2 { font-size: 16px; margin: 0 0 8px; }
article dl { margin: 0; }
article dt { color: #6E6C67; font-size: 12px; margin-top: 8px; }
article dd { margin: 2px 0 0; white-space: pre-wrap; word-break: break-word; }
a { color: #1C1C1C; }
.tag { display: inline-block; background: #EDEBE6; border-radius: 999px;
       padding: 1px 8px; margin-right: 6px; font-size: 12px; color: #3A3936; }
@media print { body { background: #fff; } article { break-inside: avoid; } }
""".strip()
    parts = [
        "<!DOCTYPE html>",
        '<html lang="zh-CN">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{html.escape(FILE_PREFIX)} · {html.escape(str(scope_label))}</title>",
        f"<style>\n{css}\n</style>",
        "</head>",
        "<body>",
        f"<h1>{html.escape(FILE_PREFIX)} · 词条导出</h1>",
        f'<p class="meta">范围：{html.escape(str(scope_label))} ｜ 条数：{len(body)}'
        f" ｜ 导出时间：{html.escape(stamp)}</p>",
        '<p class="note">上下文是划词当时原文里的那句话；来源是当时的窗口标题'
        "（未做后台采集，也没有再联网）。</p>",
    ]
    tag_index = header.index("标签") if "标签" in header else -1
    for index, cells in enumerate(body, start=1):
        term = cells[0] if cells else ""
        parts.append("<article>")
        parts.append(f"<h2>{index}. {html.escape(str(term))}</h2>")
        if tag_index >= 0 and str(cells[tag_index] or "").strip():
            chips = " ".join(
                f'<span class="tag">{html.escape(tag)}</span>'
                for tag in str(cells[tag_index]).split("#") if tag.strip())
            parts.append(f"<p>{chips}</p>")
        parts.append("<dl>")
        for position, (label, value) in enumerate(zip(header, cells)):
            if position == 0 or position == tag_index:
                continue
            text = str(value or "").strip()
            if not text:
                continue
            if label == "来源链接":
                shown = f'<a href="{html.escape(text, quote=True)}">{html.escape(text)}</a>'
            else:
                shown = html.escape(text)
            parts.append(f"<dt>{html.escape(label)}</dt><dd>{shown}</dd>")
        parts.append("</dl>")
        parts.append("</article>")
    parts += ["</body>", "</html>", ""]
    return "\n".join(parts)


def pdf_bytes(header, body, *, scope_label: str = "全部词语",
              created_at: datetime | None = None) -> bytes:
    """PDF 字节（手写 PDF + 嵌入中文字体子集，见 :mod:`app.pdf_writer`）。

    排版走 :class:`app.pdf_writer.PdfDoc`（自动折行 + 自动分页），build() 时
    才把用到的字形收齐、嵌一份子集字体，所以整份文档只排一遍。
    """
    from . import pdf_writer                                 # 延迟导入：省启动时间

    stamp = (created_at or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    doc = pdf_writer.PdfDoc(f"{FILE_PREFIX} · 词条导出")
    doc.new_page()
    doc.title(f"{FILE_PREFIX} · 词条导出")
    doc.paragraph(f"范围：{scope_label}    条数：{len(body)}    导出时间：{stamp}",
                  size=pdf_writer.SIZE_FOOT, color=pdf_writer.COLOR_MUTED)
    doc.paragraph("上下文是划词当时原文里的那句话；来源是当时的窗口标题"
                  "（未做后台采集，也没有再联网）。",
                  size=pdf_writer.SIZE_FOOT, color=pdf_writer.COLOR_FAINT)
    tag_index = header.index("标签") if "标签" in header else -1
    meta_labels = {"来源链接", "来源应用", "捕获时间", "重复次数"}
    for index, cells in enumerate(body, start=1):
        term = str(cells[0] if cells else "")
        doc.heading(f"{index}. {term}")
        for position, (label, value) in enumerate(zip(header, cells)):
            if position == 0 or position == tag_index:
                continue
            text = str(value or "").strip()
            if not text:
                continue
            if label in meta_labels:
                doc.paragraph(f"{label}：{text}", size=pdf_writer.SIZE_FOOT,
                              color=pdf_writer.COLOR_MUTED, gap_after=2.0)
            else:
                doc.paragraph(f"{label}：{text}")
        if tag_index >= 0 and str(cells[tag_index] or "").strip():
            doc.paragraph(str(cells[tag_index]).strip(),
                          size=pdf_writer.SIZE_FOOT, color=pdf_writer.COLOR_MUTED,
                          gap_after=2.0)
        doc.space(pdf_writer.GAP_AFTER_BLOCK)
    return doc.build()


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


def build_text(fmt: ExportFormat, header, body, rows, tags_map, *,
               scope_label: str, created_at: datetime) -> str | bytes:
    """按格式生成**文件内容**（PDF 是 bytes，其余是 str）。"""
    if fmt.key == "csv":
        return csv_text(header, body)
    if fmt.key == "markdown":
        return markdown_text(header, body, scope_label=scope_label,
                             created_at=created_at)
    if fmt.key == "json":
        return json_text(rows, tags_map, scope_label=scope_label,
                         created_at=created_at)
    if fmt.key == "jsonl":
        return jsonl_text(rows, tags_map)
    if fmt.key == "anki":
        return anki_text(rows, tags_map)
    if fmt.key == "html":
        return html_text(header, body, scope_label=scope_label, created_at=created_at)
    if fmt.key == "txt":
        return txt_text(header, body, scope_label=scope_label, created_at=created_at)
    if fmt.key == "pdf":
        return pdf_bytes(header, body, scope_label=scope_label, created_at=created_at)
    raise ValueError(f"未知导出格式：{fmt.key}")            # pragma: no cover


def export_entries(rows, *, scope_label: str = "全部词语", directory=None,
                   tags_map: dict | None = None, now: datetime | None = None,
                   fmt: str = DEFAULT_FORMAT) -> ExportResult:
    """把 ``rows`` 写成**一个**文件（格式由 ``fmt`` 决定），返回 :class:`ExportResult`。

    ``directory`` 默认 :func:`app.paths.exports_dir`；测试传临时目录。
    """
    stamp = now or datetime.now()
    target = Path(directory) if directory else paths.exports_dir()
    target.mkdir(parents=True, exist_ok=True)
    header, body = table(rows, tags_map)
    chosen = format_for(fmt)
    base = f"{FILE_PREFIX}-{safe_stem(scope_label)}-{stamp.strftime('%Y%m%d-%H%M%S')}"
    path = unique_path(target / f"{base}{chosen.suffix}")

    content = build_text(chosen, header, body, rows, tags_map,
                         scope_label=scope_label, created_at=stamp)
    if isinstance(content, bytes):
        path.write_bytes(content)
    elif chosen.key == "csv":
        path.write_text(content, encoding="utf-8-sig", newline="")
    else:
        path.write_text(content, encoding="utf-8", newline="")
    log.info("导出 %d 条 → %s（%s）", len(body), path.name, chosen.key)
    return ExportResult(path=path, count=len(body), scope_label=scope_label,
                        format_key=chosen.key)
