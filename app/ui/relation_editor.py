"""人工关系编辑对话框（改进清单 C5 / C6）。

用户要的能力
------------
* **手动新增关系**：模型没提出来、但用户自己认定成立的那条边；
* **改类型 / 改依据**：同一对端点再存一次就是「换一条」（存储层先删后插）；
* **删除错误连线**：只删人工关系 —— AI 关系不在这里改（它们只能靠重新生成变化）；
* **人工最高优先级**：这些边重新生成图时**绝不会**被覆盖（存储层与分析层分开）。

三条硬约束（与全程序一致）
--------------------------
* **纯 tk**：冻结运行时没有 ``tkinter.ttk``（导入它会直接让程序起不来），
  所以类型用 ``tk.Radiobutton``、列表用 ``tk.Listbox``，没有下拉框；
* **不发网络请求**：这里只写本地库，一张图也不重新生成；
* **不重复实现存储逻辑**：增删都走 :class:`app.ui.concept_map.ConceptMapWindow`
  的 ``save_manual_relation`` / ``delete_manual_relation``（它们负责落库 + 重画）。
"""
from __future__ import annotations

import tkinter as tk

from ..map_service import REL_TYPES
from . import theme, widgets

#: 人工关系的类型白名单：与 AI 关系的 6 种类型一致，另加一个「相关」
#: （用户认定有关但说不清是哪种）。「相关」不进 AI 白名单 —— 模型永远不许默认它。
MANUAL_TYPES: tuple[str, ...] = tuple(REL_TYPES) + ("相关",)

#: 对话框里两栏词条列表的宽度（字符）与高度（行）
LIST_WIDTH = 16
LIST_HEIGHT = 11


def _as_entry_id(value) -> int | None:
    """把 ``src_id`` / ``dst_id`` / ``relation_id`` 洗成整数 id（``None`` / 非法 → ``None``）。"""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _label(parent, text: str, *, muted: bool = False, size: int = 9, wrap: int = 560):
    return tk.Label(parent, text=text, bg=theme.BG,
                    fg=theme.TEXT_MUTED if muted else theme.TEXT,
                    font=theme.font(size), justify="left", anchor="w",
                    wraplength=theme.px(wrap))


def _listbox(parent, height: int = LIST_HEIGHT, width: int = LIST_WIDTH) -> tk.Listbox:
    widget = tk.Listbox(
        parent, height=height, width=width, activestyle="none",
        exportselection=False, selectmode="browse", bg=theme.PANEL, fg=theme.TEXT,
        font=theme.font(9), highlightthickness=1, highlightbackground=theme.BORDER,
        borderwidth=0, selectbackground=theme.ACCENT, selectforeground=theme.BG,
    )
    return widget


class RelationEditor:
    """「人工关系」对话框：左选起点、右选终点、中间选类型，下面列已加的边。

    三种打开方式（都在 :meth:`app.ui.concept_map.ConceptMapWindow.open_relation_editor`）：

    * 不带参数 —— 空表单（按钮「人工关系…」）；
    * ``src_id`` / ``dst_id`` —— **F1**：从卡片连线柄拖到另一张卡松手，两端已经选好；
    * ``relation`` —— **F3**：双击一条人工连线，打开就是「改这一条」。
    """

    def __init__(self, master, owner, *, src_id=None, dst_id=None, relation=None):
        self.owner = owner
        self.db = owner.db
        #: ``(entry_id, 显示名)``：本主题**全部**词条（不随分析子集变化 —— 子集少几个
        #: 词不该让人没法给它们加关系）
        self.options: list[tuple[int, str]] = list(owner.manual_relation_options())
        self.names = {int(eid): str(name) for eid, name in self.options}
        #: 当前库里的人工关系行（与下面那个列表一一对应）
        self.rows: list = []
        #: 打开时带进来的两端（F1 拖拽 / F3 双击）：``None`` = 让用户自己选
        self.initial_src = _as_entry_id(src_id)
        self.initial_dst = _as_entry_id(dst_id)
        #: 打开时带进来的那条人工关系（F3 双击）：改完按「添加 / 更新」就是覆盖它
        self.initial_relation = relation

        self.win = tk.Toplevel(master)
        self.win.title("人工关系 — 探索词典")
        self.win.configure(bg=theme.BG)
        try:
            self.win.transient(master)
        except tk.TclError:  # pragma: no cover - 极简替身
            pass
        self.chrome = widgets.BorderlessChrome(self.win, title="人工关系",
                                               on_close=self.close, resizable=True,
                                               min_w=560, min_h=420, bg=theme.PANEL)

        wrap = tk.Frame(self.win, bg=theme.BG)
        wrap.pack(fill="both", expand=True, padx=theme.px(16), pady=theme.px(12))
        _label(wrap, "这里加的关系是你自己的判断：线上和 AI 的线长得一样（同一个颜色、"
                     "同一个粗细，只写关系类型），认它的办法是点一下线 —— 依据区里会写着"
                     "「人工添加的关系」。重新生成参考关系时**不会**被覆盖。"
                     "AI 关系不在这里改 —— 要让模型重新判断，回上一页点「重新生成」。",
               muted=True, size=8).pack(anchor="w", pady=(0, theme.px(8)))

        columns = tk.Frame(wrap, bg=theme.BG)
        columns.pack(fill="x")

        left = tk.Frame(columns, bg=theme.BG)
        left.pack(side="left", fill="y")
        _label(left, "起点（选一个词）", muted=True, size=7).pack(anchor="w")
        self.src_list = _listbox(left)
        self.src_list.pack(fill="y")

        mid = tk.Frame(columns, bg=theme.BG)
        mid.pack(side="left", fill="both", expand=True, padx=theme.px(12))
        _label(mid, "关系类型（起点 → 终点）", muted=True, size=7).pack(anchor="w")
        self.var_type = tk.StringVar(value=str(MANUAL_TYPES[0]))
        self.type_buttons: list = []
        for name in MANUAL_TYPES:
            button = tk.Radiobutton(
                mid, text=name, variable=self.var_type, value=str(name),
                bg=theme.BG, fg=theme.TEXT, font=theme.font(9), anchor="w",
                activebackground=theme.BG, activeforeground=theme.TEXT,
                selectcolor=theme.PANEL, highlightthickness=0, bd=0,
            )
            button.pack(fill="x", anchor="w")
            self.type_buttons.append(button)
        _label(mid, "依据 / 备注（可留空）", muted=True, size=7).pack(
            anchor="w", pady=(theme.px(8), theme.px(2)))
        self.var_note = tk.StringVar(value="")
        self.note_entry = tk.Entry(mid, textvariable=self.var_note, width=26,
                                   bg=theme.CARD_BG, fg=theme.TEXT, font=theme.font(9),
                                   relief="flat", highlightthickness=1,
                                   highlightbackground=theme.BORDER, insertbackground=theme.TEXT)
        self.note_entry.pack(fill="x")

        right = tk.Frame(columns, bg=theme.BG)
        right.pack(side="left", fill="y")
        _label(right, "终点（选一个词）", muted=True, size=7).pack(anchor="w")
        self.dst_list = _listbox(right)
        self.dst_list.pack(fill="y")

        _label(wrap, "已经加过的人工关系（点一条再按「删除选中的关系」）",
               muted=True, size=7).pack(anchor="w", pady=(theme.px(10), theme.px(2)))
        self.rel_list = _listbox(wrap, height=4, width=LIST_WIDTH * 3)
        self.rel_list.pack(fill="x")

        self.status = tk.Label(wrap, text="", bg=theme.BG, fg=theme.TEXT_MUTED,
                               font=theme.font(8), anchor="w", justify="left",
                               wraplength=theme.px(560))
        self.status.pack(fill="x", pady=(theme.px(4), 0))

        buttons = tk.Frame(wrap, bg=theme.BG)
        buttons.pack(fill="x", pady=(theme.px(6), 0))
        self.btn_add = widgets.FlatButton(buttons, "添加 / 更新这条关系", self.add_current,
                                          primary=True, font_size=8, padx=12, pady=5)
        self.btn_add.pack(side="left")
        self.btn_delete = widgets.FlatButton(buttons, "删除选中的关系", self.delete_current,
                                             font_size=8, padx=10, pady=4)
        self.btn_delete.pack(side="left", padx=(theme.px(6), 0))
        self.btn_close = widgets.FlatButton(buttons, "关闭", self.close, font_size=8,
                                            padx=10, pady=4)
        self.btn_close.pack(side="right")

        self._fill_entries()
        self.refresh()
        if not self._apply_initial():
            self._set_status("左边 / 右边各选一个词，选好类型，再按「添加 / 更新这条关系」。")

    # ------------------------------------------------------- 带进来的选中项
    def _apply_initial(self) -> bool:
        """把带进来的两端 / 关系填进表单（F1 / F3）；什么都没带 → ``False``。"""
        rel = self.initial_relation
        if rel is not None:
            src = _as_entry_id(getattr(rel, "src_entry_id", None))
            dst = _as_entry_id(getattr(rel, "dst_entry_id", None))
            if src is not None:
                self.initial_src = src
            if dst is not None:
                self.initial_dst = dst
            kind = str(getattr(rel, "rel_type", "") or getattr(rel, "label", "") or "").strip()
            if kind in MANUAL_TYPES:
                try:
                    self.var_type.set(kind)
                except (AttributeError, tk.TclError):  # pragma: no cover - 极简替身
                    pass
            note = str(getattr(rel, "note", "") or "")
            if note:
                try:
                    self.var_note.set(note)
                except (AttributeError, tk.TclError):  # pragma: no cover
                    pass
        picked = 0
        if self.initial_src is not None and self._select_entry(self.src_list, self.initial_src):
            picked += 1
        if self.initial_dst is not None and self._select_entry(self.dst_list, self.initial_dst):
            picked += 1
        if rel is not None:
            self._select_relation(getattr(rel, "relation_id", 0))
            self._set_status("这就是那条人工关系：改完按「添加 / 更新这条关系」= 覆盖它，"
                             "按「删除选中的关系」= 删掉它。")
            return True
        if picked:
            self._set_status("两端已经替你选好了：选个类型（想写依据就写一句），"
                             "再按「添加 / 更新这条关系」。")
            return True
        return False

    def _select_entry(self, widget, entry_id: int) -> bool:
        """在词条列表里选中 ``entry_id``（找到并选中 → ``True``）。

        只有 ``selection_clear`` / ``selection_set`` 是必须的；``activate`` / ``see``
        只是让这一行看起来「亮着」和滚进视野，缺了也照样算选中成功。
        """
        for index, (eid, _label) in enumerate(self.options):
            if int(eid) != int(entry_id):
                continue
            try:
                widget.selection_clear(0, tk.END)
                widget.selection_set(index)
            except (AttributeError, tk.TclError):  # pragma: no cover - 极简替身
                return False
            for name in ("activate", "see"):
                method = getattr(widget, name, None)
                if not callable(method):
                    continue
                try:
                    method(index)
                except (tk.TclError, TypeError):  # pragma: no cover - 极简替身
                    pass
            return True
        return False

    def _select_relation(self, relation_id) -> bool:
        """在下面那个列表里选中 ``id = relation_id`` 的那一行（F3 双击进来的那条）。"""
        want = _as_entry_id(relation_id)
        if want is None:
            return False
        for index, row in enumerate(self.rows):
            try:
                row_id = int(row["id"])
            except (KeyError, IndexError, TypeError, ValueError):  # pragma: no cover
                continue
            if row_id != want:
                continue
            try:
                self.rel_list.selection_clear(0, tk.END)
                self.rel_list.selection_set(index)
            except (AttributeError, tk.TclError):  # pragma: no cover - 极简替身
                return False
            see = getattr(self.rel_list, "see", None)
            if callable(see):
                try:
                    see(index)
                except (tk.TclError, TypeError):  # pragma: no cover
                    pass
            return True
        return False

    # ------------------------------------------------------------- 数据
    def _fill_entries(self) -> None:
        for widget in (self.src_list, self.dst_list):
            try:
                widget.delete(0, tk.END)
                for _eid, label in self.options:
                    widget.insert(tk.END, label)
            except (AttributeError, tk.TclError):  # pragma: no cover - 极简替身
                return

    def _picked_id(self, widget) -> int | None:
        """列表里选中的那一个词条 id（没选 / 选不到 → ``None``）。"""
        try:
            picks = widget.curselection()
        except (AttributeError, tk.TclError):  # pragma: no cover
            return None
        if not picks:
            return None
        index = int(picks[0])
        if not 0 <= index < len(self.options):
            return None
        return int(self.options[index][0])

    def _picked_row(self):
        try:
            picks = self.rel_list.curselection()
        except (AttributeError, tk.TclError):  # pragma: no cover
            return None
        if not picks:
            return None
        index = int(picks[0])
        if not 0 <= index < len(self.rows):
            return None
        return self.rows[index]

    def _row_text(self, row) -> str:
        src = self.names.get(int(row["src_entry_id"]), "（不在本主题）")
        dst = self.names.get(int(row["dst_entry_id"]), "（不在本主题）")
        note = str(row["note"] or "").strip() if "note" in row.keys() else ""
        text = f"{src} --{str(row['label'])}--> {dst}"
        return f"{text}（依据：{note}）" if note else text

    def refresh(self) -> None:
        """重读人工关系并刷新下面的列表（**只读本地库**）。"""
        self.rows = list(self.owner.manual_relation_rows())
        try:
            self.rel_list.delete(0, tk.END)
            for row in self.rows:
                self.rel_list.insert(tk.END, self._row_text(row))
        except (AttributeError, tk.TclError):  # pragma: no cover
            return

    # ------------------------------------------------------------- 动作
    def _set_status(self, text: str, *, error: bool = False) -> None:
        try:
            self.status.configure(text=str(text),
                                  fg=theme.ERR if error else theme.TEXT_MUTED)
        except (AttributeError, tk.TclError):  # pragma: no cover
            return

    def add_current(self) -> None:
        """把「起点 + 类型 + 终点 + 依据」写成一条人工关系。"""
        src = self._picked_id(self.src_list)
        dst = self._picked_id(self.dst_list)
        if src is None or dst is None:
            self._set_status("先在左边选「起点」、右边选「终点」（各选一个词）。", error=True)
            return
        ok, message = self.owner.save_manual_relation(src, dst, self.var_type.get(),
                                                     self.var_note.get())
        self._set_status(message, error=not ok)
        if ok:
            try:
                self.var_note.set("")
            except (AttributeError, tk.TclError):  # pragma: no cover
                pass
            self.refresh()

    def delete_current(self) -> None:
        """删掉下面列表里选中的那条人工关系。"""
        row = self._picked_row()
        if row is None:
            self._set_status("先在下面点一条人工关系，再按「删除选中的关系」。", error=True)
            return
        ok, message = self.owner.delete_manual_relation(row["id"])
        self._set_status(message, error=not ok)
        if ok:
            self.refresh()

    def close(self) -> None:
        try:
            self.win.destroy()
        except (AttributeError, tk.TclError):  # pragma: no cover
            return
