"""轻量数据结构。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


def now_iso() -> str:
    """本地时区 ISO8601（秒精度）。"""
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


@dataclass
class SourceInfo:
    """捕获时的来源信息。"""

    hwnd: int = 0
    pid: int = 0
    exe: str = ""
    app: str = ""
    title: str = ""
    url: str = ""
    confidence: str = "window_title_only"  # url_document | window_title_only | manual
    doc_key: str = ""
    note: str = ""

    def display(self) -> str:
        if self.url:
            return f"{self.title} — {self.url}"
        return self.title or self.app or "(未知来源)"


@dataclass
class CapturedSelection:
    """一次取词结果（内存快照，**不落库**）。

    最终交互：鼠标选词只产生这样一份内存快照，用来在选区旁弹出带唯一按钮
    「解释并记录」的小浮条；点别处只**作废**这份快照、什么都不保存（窗口显隐由用户
    与硬阻断决定）。只有点那个按钮
    才会把它写进 SQLite（见 CaptureService.record_snapshot），随后异步解释
    并把释义写回**同一个 entry_id**。
    """

    term: str
    context: str = ""
    method: str = "manual_input"  # ui_textpattern | manual_input | clipboard_import
    source: SourceInfo = field(default_factory=SourceInfo)
    captured_at: str = field(default_factory=now_iso)
    #: 选区矩形（屏幕坐标 ``(left, top, right, bottom)``）；UIA 未给出时为空。
    rect: tuple[int, int, int, int] | None = None

    def signature(self) -> tuple:
        """用于「是否还是同一份快照」的判等键。"""
        return (self.term, self.context, self.method,
                self.source.hwnd, self.source.pid, self.source.url, self.source.title)


#: 选区快照的别名：新代码优先用这个名字，语义更明确（内存态、未落库）。
SelectionSnapshot = CapturedSelection



@dataclass
class ExplainResult:
    one_line: str
    detail: str = ""
    examples: list[str] = field(default_factory=list)
    raw: str = ""
    from_cache: bool = False
    model_config: str = ""
    #: 可选的**主题名**（模型若能判断就给出，用于给自动分组起更好的名字）。
    #: 默认空串：旧接口 / 旧缓存 / 模型没给 topic 时都退回「首个关键词」命名，
    #: 绝不因为缺这个字段影响解释本身的可用性。
    topic: str = ""

    def as_text(self) -> str:
        """给用户看的多行文本（解释结果小窗 / 词条详情都用它，保证一致）。"""
        lines = [self.one_line or ""]
        if self.detail:
            lines.append("")
            lines.append(self.detail)
        if self.examples:
            lines.append("")
            lines.append("例子：")
            lines.extend(f"  · {ex}" for ex in self.examples)
        return "\n".join(lines).strip()
