"""SQLite 持久层。

设计要点：
* 单文件库，默认 data/explorer_dict.sqlite3；测试时传入临时路径。
* 所有时间由 Python 生成 ISO8601 字符串（不用 sqlite3 的 datetime 适配器，3.12+ 已弃用）。
* API key 只以 DPAPI 密文进 secrets 表；settings 表只放 `api.key_saved` 之类标志。
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
from pathlib import Path

from . import crypto_dpapi
from .article_source import Article
from .models import now_iso

SCHEMA_VERSION = 10


def _as_topic(topic_id: object) -> int:
    """``topic_id`` 的容错转换：``None`` / 非法值一律当 0（"未知主题"）。"""
    try:
        return int(topic_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


#: 一个标签最多多少字符、一条词条最多几个标签（**轻量**标签：不做重型笔记）
MAX_TAG_CHARS = 24
MAX_TAGS_PER_ENTRY = 12
#: 追加上下文（B4）后 context 的总长度上限：上下文本身很短，超了只截断新片段
MAX_CONTEXT_CHARS = 1200
#: 追加上下文时拼接用的分隔符（正常化之后仍与旧片段逐段比对，重复追加不会变长）
CONTEXT_JOINER = " ／ "

#: 增量迁移：只**新增**表/索引/列，绝不重建、绝不删除既有数据。
#: v1 → v2 加入「词条级追问对话」表（每个词独立历史）。
#: v2 → v3 由 :meth:`Database._ensure_column` 增列：``batches.name_source``
#: （主题名的来源：auto / manual / ''=旧数据未知）与 ``explain_cache.topic``。
#: v3 → v4 加入 ``map_graphs``：AI 参考关系图的**结果缓存**（按主题 + 内容指纹
#: 隔离）。它只缓存「模型给的候选关系」这一派生数据，**绝不写** ``relations``
#: 表 —— 那张表里的旧记录仍然是用户手动建立的，不会被当成已验证的 AI 关系。
#: v4 → v5 给 ``map_graphs`` 增列 ``validation_version``：缓存里必须记录**校验
#: 版本**，只有「候选 + 独立核对」这一版校验产出的图才允许直接展示；旧的
#: 「只做字串证据校验」缓存默认 0，读缓存时一律不命中（当没有缓存重新生成）。
#: v5 → v6 给 ``map_graphs`` 再加两列：``dropped``（筛除计数）与 ``verdicts``
#: （**每条候选的核对判定**：保留 / 不确定 / 不成立 + 模型给的理由）。它们是
#: 「这个词为什么变成了孤立词」的唯一答案 —— 以前只活在内存里，重开窗口既看不到
#: 筛除计数、也说不清孤立原因。
#: v6 → v7 加入 ``tags`` / ``entry_tags``：**轻量标签**（一个词条可打多个标签，
#: 标签跨主题检索）。它只回答「这条词属于我关心的哪几类」，刻意不做笔记 / 层级 /
#: 颜色；条目与标签都随词条删除自动清理（``ON DELETE CASCADE``）。
#: v7 → v8 给 ``relations`` 补两列：``topic_id``（这条**人工关系**属于哪个主题，
#: 0 = 未知/全局）与 ``note``（用户给这条关系写的一句话）。这两列不新开表 ——
#: 参考关系图里手工加的关系本来就存在 ``relations``，只是以前没地方记主题与备注。
#: v8 → v9 加入 ``node_pins``（**词卡位置固定**：用户把卡片拖到哪儿就记在哪儿，
#: 重新生成只换 AI 关系、不动他摆过的位置）与 ``map_edge_blocks``（**AI 关系
#: 黑名单**：用户判定「这条 AI 关系不对」后，同一对端点的候选边不再画出来，
#: 重新生成也不会回来）。两者都随词条删除自动清理（``ON DELETE CASCADE``），
#: 都只记「位置」与「哪一对词条」，**不缓存任何模型输出**。
#: v9 → v10 加入 ``articles``：**原文留档**（以 ``doc_key`` 为主键，一个文档一行）。
#: 导图的目标是「按这一篇文章的脉络归纳」，而归纳必须看得到原文 —— 只拿每个词的
#: 240 字上下文，模型给得出「词与词的关系」，给不出「这篇文章在讲什么」。正文在
#: 划词那一刻抓（页面内容随时会变，事后补抓到的往往已经是另一版），抓不到就如实写
#: ``status='failed'`` + 原因，**绝不**拿导航文字或上一次的缓存冒充原文。
_MIGRATIONS = """
CREATE TABLE IF NOT EXISTS chat_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    request_id INTEGER NOT NULL DEFAULT 0,
    role TEXT NOT NULL DEFAULT 'user',
    content TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'ok',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_turns_entry ON chat_turns(entry_id, id);
CREATE INDEX IF NOT EXISTS idx_chat_turns_request ON chat_turns(request_id);

CREATE TABLE IF NOT EXISTS articles (
    doc_key TEXT PRIMARY KEY,
    url TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    text_chars INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'failed',
    note TEXT NOT NULL DEFAULT '',
    fetched_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS map_graphs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    base_url TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT '[]',
    raw TEXT NOT NULL DEFAULT '',
    validation_version INTEGER NOT NULL DEFAULT 0,
    dropped TEXT NOT NULL DEFAULT '',
    verdicts TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(topic_id, fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_map_graphs_topic ON map_graphs(topic_id);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entry_tags (
    entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (entry_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_entry_tags_tag ON entry_tags(tag_id);

CREATE TABLE IF NOT EXISTS node_pins (
    topic_id INTEGER NOT NULL,
    entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    x REAL NOT NULL,
    y REAL NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (topic_id, entry_id)
);
CREATE INDEX IF NOT EXISTS idx_node_pins_topic ON node_pins(topic_id);

CREATE TABLE IF NOT EXISTS map_edge_blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id INTEGER NOT NULL DEFAULT 0,
    src_entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    dst_entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    label TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(topic_id, src_entry_id, dst_entry_id)
);
CREATE INDEX IF NOT EXISTS idx_map_edge_blocks_topic ON map_edge_blocks(topic_id);
"""

#: v2 → v3 的**加列**迁移：``(表, 列, 列定义)``。
#: ``batches.name_source`` 默认空串 —— 老库里的名字来源未知，属于「用户自己的
#: 名字」，自动改名**不得**覆盖它（见 :meth:`Database.rename_batch_auto`）。
#: ``map_graphs.validation_version`` 默认 0 —— 老缓存按「旧校验」处理，不命中。
#: ``map_graphs.dropped`` / ``map_graphs.verdicts`` 默认空串 —— 老缓存没有判定
#: 记录，界面按「这份缓存生成时还没记录判定」处理（不编造筛除计数）。
#: ``relations.topic_id`` 默认 0、``relations.note`` 默认空串 —— 老库里那些手工
#: 关系没有主题归属也没有备注，一律当「未知主题 / 没写备注」，绝不猜。
_COLUMN_MIGRATIONS = (
    ("batches", "name_source", "TEXT NOT NULL DEFAULT ''"),
    ("explain_cache", "topic", "TEXT NOT NULL DEFAULT ''"),
    ("map_graphs", "validation_version", "INTEGER NOT NULL DEFAULT 0"),
    ("map_graphs", "dropped", "TEXT NOT NULL DEFAULT ''"),
    ("map_graphs", "verdicts", "TEXT NOT NULL DEFAULT ''"),
    ("relations", "topic_id", "INTEGER NOT NULL DEFAULT 0"),
    ("relations", "note", "TEXT NOT NULL DEFAULT ''"),
)

#: v7 → v8 的**索引**迁移，必须在 :data:`_COLUMN_MIGRATIONS` 补完列**之后**执行。
#: 它**不能**放进 ``_DDL``：``_DDL`` 在加列迁移之前跑，老库那时还没有
#: ``relations.topic_id``，``CREATE INDEX`` 会以 ``no such column: topic_id``
#: 直接把「打开库」炸掉。新库（``_DDL`` 已建全列）与老库（刚 ``ALTER TABLE``
#: 补上列）在这里都能建出来；``IF NOT EXISTS`` 保证重复执行安全。
_INDEX_MIGRATIONS = """
CREATE INDEX IF NOT EXISTS idx_relations_topic ON relations(topic_id);
"""

_DDL = """
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    source_key TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL DEFAULT '',
    pinned INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_batches_source ON batches(source_key);

CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    term TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '',
    doc_key TEXT NOT NULL DEFAULT '',
    source_app TEXT NOT NULL DEFAULT '',
    source_title TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    source_confidence TEXT NOT NULL DEFAULT 'window_title_only',
    source_note TEXT NOT NULL DEFAULT '',
    capture_method TEXT NOT NULL DEFAULT 'manual_input',
    captured_at TEXT NOT NULL,
    repeat_count INTEGER NOT NULL DEFAULT 1,
    explain_status TEXT NOT NULL DEFAULT 'none',
    one_line TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    examples TEXT NOT NULL DEFAULT '[]',
    model_config TEXT NOT NULL DEFAULT '',
    explain_error TEXT NOT NULL DEFAULT '',
    explained_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_entries_batch ON entries(batch_id);
CREATE INDEX IF NOT EXISTS idx_entries_term ON entries(term);

CREATE TABLE IF NOT EXISTS relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    src_entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    dst_entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    label TEXT NOT NULL DEFAULT '相关',
    origin TEXT NOT NULL DEFAULT 'user',
    topic_id INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(src_entry_id, dst_entry_id, label)
);

CREATE TABLE IF NOT EXISTS explain_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cache_key TEXT NOT NULL UNIQUE,
    term TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '',
    base_url TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    one_line TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    examples TEXT NOT NULL DEFAULT '[]',
    raw TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS secrets (
    name TEXT PRIMARY KEY,
    blob BLOB NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
"""


#: ``entries`` 允许被更新的字段（update_entry 与 apply_explanation 共用同一份白名单）
_ENTRY_UPDATE_FIELDS = frozenset({
    "term",
    "context",
    "source_title",
    "source_url",
    "source_confidence",
    "source_note",
    "batch_id",
    "explain_status",
    "one_line",
    "detail",
    "examples",
    "model_config",
    "explain_error",
    "explained_at",
})


def _entry_update_sql(fields: dict) -> tuple[list[str], list]:
    """把 ``{字段: 值}`` 翻译成 ``SET`` 片段与参数（字段校验共用一份白名单）。"""
    sets, params = [], []
    for k, v in (fields or {}).items():
        if k not in _ENTRY_UPDATE_FIELDS:
            raise ValueError(f"不允许更新字段: {k}")
        sets.append(f"{k}=?")
        params.append(v)
    return sets, params


def normalize_tags(value) -> list[str]:
    """把用户输入的一串标签**规范化**成去重后的列表（顺序保留）。

    * 输入可以是字符串（用空格 / 逗号 / 顿号 / 分号 / 斜杠分隔）或任意可迭代对象；
    * 每条标签去掉首尾空白、把内部连续空白压成一个空格，并截到
      :data:`MAX_TAG_CHARS` 字；
    * 去重**大小写不敏感**（``经济学`` 与 ``经济学 `` 只留一条），条目数上限
      :data:`MAX_TAGS_PER_ENTRY`。

    纯函数：不碰数据库，界面上「输入框里的字 → 真正要写的标签」与写库前的
    校验共用它，因此界面显示与库里存的一定一致。
    """
    if value is None:
        return []
    if isinstance(value, str):
        raw = value
        for sep in (",", "，", "、", ";", "；", "/", "／", "\n", "\t"):
            raw = raw.replace(sep, " ")
        items = raw.split(" ")
    else:
        try:
            items = list(value)
        except TypeError:
            items = [value]
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = " ".join(str(item or "").split()).strip()
        if not text:
            continue
        text = text[:MAX_TAG_CHARS]
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= MAX_TAGS_PER_ENTRY:
            break
    return out


class Database:
    """一个库连接的封装。UI 用长连接；测试用临时库。"""

    def __init__(self, path: str | os.PathLike[str], *, read_only: bool = False):
        self.path = str(path)
        self.read_only = read_only
        self._lock = threading.RLock()
        self._closed = False
        if read_only:
            uri = Path(self.path).absolute().as_uri() + "?mode=ro"
            self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        else:
            parent = Path(self.path).parent
            parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(self.path, check_same_thread=False)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        if not read_only:
            with self._lock:
                self.conn.executescript(_DDL)
                # 增量迁移（v1 库 → v2）：只建新表，既有数据原样保留
                self._migrate()
                self.conn.execute(
                    "INSERT INTO settings(k, v) VALUES('schema_version', ?) "
                    "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                    (str(SCHEMA_VERSION),),
                )
                self.conn.commit()

    def _current_schema_version(self) -> int:
        try:
            row = self.conn.execute(
                "SELECT v FROM settings WHERE k='schema_version'").fetchone()
        except sqlite3.Error:  # pragma: no cover - settings 表刚建好，理论上不会失败
            return 1
        if row is None:
            return 1
        value = row["v"] if isinstance(row, sqlite3.Row) else row[0]
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return 1

    def _migrate(self) -> None:
        """增量迁移：只**新增**表 / 索引 / 列，既有用户数据原样保留。

        v1 → v2：新增 ``chat_turns``（词条级追问对话）。
        v2 → v3：给 ``batches`` 加 ``name_source``、给 ``explain_cache`` 加 ``topic``。
        v7 → v8：给 ``relations`` 加 ``topic_id`` / ``note``，并建
        ``idx_relations_topic``（补列之后才建，见 :data:`_INDEX_MIGRATIONS`）。
        v8 → v9：新增 ``node_pins``（词卡固定位置）与 ``map_edge_blocks``
        （AI 关系黑名单）。

        迁移脚本全部是 ``CREATE ... IF NOT EXISTS`` / ``ALTER TABLE ADD COLUMN``，
        因此对已经迁移过的库（或者被人手工删过表的库）重复执行也安全 ——
        这里不做版本分支上的「跳过」，避免出现「版本号新但表真的缺了」的半损状态。
        """
        self.conn.executescript(_MIGRATIONS)
        for table, column, ddl in _COLUMN_MIGRATIONS:
            self._ensure_column(table, column, ddl)
        # 索引迁移放在补列**之后**（``idx_relations_topic`` 依赖 v8 才有的列）
        self.conn.executescript(_INDEX_MIGRATIONS)

    def _ensure_column(self, table: str, column: str, ddl: str) -> bool:
        """幂等加列：已经存在就什么都不做（**绝不**丢数据）。"""
        try:
            rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        except sqlite3.Error:  # pragma: no cover - 表刚建好，理论上不会失败
            return False
        names = {str(r["name"] if isinstance(r, sqlite3.Row) else r[1]) for r in rows}
        if column in names:
            return False
        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        return True

    # ------------------------------------------------------------------ 基础
    def closed(self) -> bool:
        """连接是否已经关闭（退出流程 / 测试 tearDown 之后不要再摸库）。"""
        return bool(getattr(self, "_closed", False))

    def _ensure_open(self) -> None:
        """关闭之后再用连接：抛一个**可捕获的**错误，而不是让 sqlite3 段错误。

        （后台追问/解释线程可能在 UI 已经退出的瞬间还在写库；直接对已关闭的
        连接调 ``execute`` 会让解释器 access violation 崩溃，测试里也会
        随机把整个进程带走。）
        """
        if self.closed():
            raise sqlite3.ProgrammingError("数据库连接已关闭")

    def close(self) -> None:
        with self._lock:
            self._closed = True
            try:
                self.conn.close()
            except sqlite3.Error:
                pass

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            self._ensure_open()
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            self._ensure_open()
            return list(self.conn.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # -------------------------------------------------------------- settings
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.query_one("SELECT v FROM settings WHERE k=?", (key,))
        return row["v"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO settings(k, v) VALUES(?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, str(value)),
        )

    def all_settings(self) -> dict[str, str]:
        return {r["k"]: r["v"] for r in self.query("SELECT k, v FROM settings")}

    def get_bool(self, key: str, default: bool = False) -> bool:
        v = self.get_setting(key)
        if v is None:
            return default
        return v.strip() in ("1", "true", "True", "yes", "on")

    def set_bool(self, key: str, value: bool) -> None:
        self.set_setting(key, "1" if value else "0")

    def get_int(self, key: str, default: int) -> int:
        v = self.get_setting(key)
        try:
            return int(str(v).strip())
        except (TypeError, ValueError):
            return default

    # --------------------------------------------------------------- secrets
    def set_secret(self, name: str, plaintext: str) -> None:
        """DPAPI 加密后落库；空串表示清除。"""
        if plaintext:
            blob = crypto_dpapi.protect(plaintext)
            self.execute(
                "INSERT INTO secrets(name, blob, updated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET blob=excluded.blob, updated_at=excluded.updated_at",
                (name, sqlite3.Binary(blob), now_iso()),
            )
            if name == "api_key":
                self.set_bool("api.key_saved", True)
        else:
            self.clear_secret(name)

    def get_secret(self, name: str) -> str | None:
        row = self.query_one("SELECT blob FROM secrets WHERE name=?", (name,))
        if not row:
            return None
        try:
            return crypto_dpapi.unprotect(bytes(row["blob"]))
        except crypto_dpapi.DpapiError:
            return None

    def clear_secret(self, name: str) -> None:
        self.execute("DELETE FROM secrets WHERE name=?", (name,))
        if name == "api_key":
            self.set_bool("api.key_saved", False)

    def has_secret(self, name: str) -> bool:
        return self.query_one("SELECT 1 FROM secrets WHERE name=?", (name,)) is not None

    # -------------------------------------------------------------- batches
    def create_batch(self, name: str, source_key: str = "", source_kind: str = "",
                     *, name_source: str = "auto") -> int:
        """新建主题（旧称「批次」）。

        ``name_source`` 记录这个名字是谁起的：``auto``（程序按首个关键词自动命名）
        或 ``manual``（用户改名）。**旧数据**（v2 及更早）迁移后是空串，被视为
        「用户自己的名字」，自动改名永远不会覆盖它。
        """
        ts = now_iso()
        cur = self.execute(
            "INSERT INTO batches(name, source_key, source_kind, pinned, name_source, "
            "created_at, updated_at) VALUES(?, ?, ?, 0, ?, ?, ?)",
            (name, source_key, source_kind, str(name_source or ""), ts, ts),
        )
        return int(cur.lastrowid)

    def get_batch(self, batch_id: int) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM batches WHERE id=?", (batch_id,))

    def list_batches(self) -> list[sqlite3.Row]:
        return self.query(
            "SELECT b.*, (SELECT COUNT(*) FROM entries e WHERE e.batch_id=b.id) AS entry_count "
            "FROM batches b ORDER BY b.pinned DESC, b.updated_at DESC, b.id DESC"
        )

    def rename_batch(self, batch_id: int, name: str, *, name_source: str = "manual") -> None:
        """用户**显式改名**：同时把这个名字标记成 manual（自动改名从此不会碰它）。"""
        self.execute(
            "UPDATE batches SET name=?, name_source=?, updated_at=? WHERE id=?",
            (name, str(name_source or "manual"), now_iso(), batch_id),
        )

    def rename_batch_auto(self, batch_id: int, name: str) -> bool:
        """自动命名：**一条 SQL 里**完成「判断 + 改名」（同锁同事务，无竞态窗口）。

        ``WHERE name_source='auto'`` 是硬条件：

        * 用户改过名（``manual``）→ 0 行受影响，返回 False，用户的名字原样保留；
        * 旧库里的名字（``''``，来源未知）→ 同样不动，绝不覆盖用户的历史命名。

        返回是否真的改了名。因为判断与写入在同一个 SQL 语句里，自动改名与
        手动改名并发时也不会互相覆盖（SQLite 串行化 + ``self._lock``）。
        """
        cur = self.execute(
            "UPDATE batches SET name=?, updated_at=? WHERE id=? AND name_source='auto'",
            (str(name), now_iso(), int(batch_id)),
        )
        return int(cur.rowcount or 0) > 0

    def batch_name_source(self, batch_id: int) -> str:
        row = self.query_one("SELECT name_source FROM batches WHERE id=?", (batch_id,))
        return str(row["name_source"] or "") if row is not None else ""

    def delete_batch(self, batch_id: int) -> None:
        self.execute("DELETE FROM batches WHERE id=?", (batch_id,))

    def touch_batch(self, batch_id: int) -> None:
        self.execute("UPDATE batches SET updated_at=? WHERE id=?", (now_iso(), batch_id))

    def set_pinned(self, batch_id: int | None) -> None:
        """全局只允许一个固定批次；None 表示取消固定。"""
        with self._lock:
            self.conn.execute("UPDATE batches SET pinned=0")
            if batch_id is not None:
                self.conn.execute("UPDATE batches SET pinned=1 WHERE id=?", (batch_id,))
            self.conn.commit()

    def pinned_batch(self) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM batches WHERE pinned=1 LIMIT 1")

    def find_recent_batch_by_source(self, source_key: str, since_iso: str) -> sqlite3.Row | None:
        """按来源键找最近批次（带时间下界）。

        注意：新逻辑**不再按分钟窗口**切批次，此方法仅为兼容保留。
        """
        if not source_key:
            return None
        return self.query_one(
            "SELECT * FROM batches WHERE source_key=? AND updated_at>=? "
            "ORDER BY updated_at DESC LIMIT 1",
            (source_key, since_iso),
        )

    def find_batch_by_source(self, source_key: str) -> sqlite3.Row | None:
        """按来源键找批次，**不限时间**：同一可靠来源跨天也复用同一批次。"""
        if not source_key:
            return None
        return self.query_one(
            "SELECT * FROM batches WHERE source_key=? ORDER BY updated_at DESC, id DESC LIMIT 1",
            (source_key,),
        )

    # ------------------------------------------------- 主题合并 / 词条搬家
    def merge_batches(self, target_id: int, source_ids) -> int:
        """把若干主题**合并进** ``target_id``：词条搬家、源主题删除。

        合并语义（单一事务，绝不半途而废）：

        * ``source_ids`` 里属于 ``target_id`` 的 id 自动忽略（自己并不合并自己）；
        * 词条**原样搬家**（``batch_id`` 改成目标主题）—— 上下文、来源、解释、
          标签、追问历史全部跟着词条走，一条都不删；
        * 源主题的关系图缓存**删掉**（``map_graphs.topic_id`` 指向已消失的主题，
          留着就是孤儿；合并后的目标主题会按新内容重新生成）；
        * 源主题本身删除（``batches`` 行没了，但词条已经先搬走了，不会被级联删掉）。

        返回真正搬过去的词条数。
        """
        target = int(target_id)
        sources = sorted({int(i) for i in (source_ids or []) if int(i) and int(i) != target})
        if not sources:
            return 0
        placeholders = ", ".join("?" for _ in sources)
        with self._lock:
            self._ensure_open()
            try:
                cur = self.conn.execute(
                    f"UPDATE entries SET batch_id=? WHERE batch_id IN ({placeholders})",
                    (target, *sources),
                )
                moved = int(cur.rowcount or 0)
                self.conn.execute(
                    f"DELETE FROM map_graphs WHERE topic_id IN ({placeholders})",
                    tuple(sources),
                )
                self.conn.execute(
                    f"DELETE FROM batches WHERE id IN ({placeholders})",
                    tuple(sources),
                )
                self.conn.execute(
                    "UPDATE batches SET updated_at=? WHERE id=?", (now_iso(), target))
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        log_event = getattr(self, "log_event", None)
        if callable(log_event):
            log_event("merge_batches", f"target=#{target} sources={sources} moved={moved}")
        return moved

    def move_entries(self, entry_ids, target_batch_id: int) -> int:
        """把选中的词条**搬进**另一个主题（主题拆分用：新主题先建好再搬过来）。

        只改 ``batch_id``：词条内容、解释、标签、追问历史全部跟着走。两个主题的
        ``updated_at`` 都刷新（源主题可能因此变空，但它**不会被删除** —— 是否删掉
        由用户决定）。返回搬动的条数。
        """
        ids = sorted({int(i) for i in (entry_ids or []) if int(i)})
        target = int(target_batch_id)
        if not ids or not target:
            return 0
        placeholders = ", ".join("?" for _ in ids)
        with self._lock:
            self._ensure_open()
            try:
                rows = self.conn.execute(
                    f"SELECT DISTINCT batch_id FROM entries WHERE id IN ({placeholders})",
                    tuple(ids),
                ).fetchall()
                cur = self.conn.execute(
                    f"UPDATE entries SET batch_id=? WHERE id IN ({placeholders})",
                    (target, *ids),
                )
                moved = int(cur.rowcount or 0)
                ts = now_iso()
                self.conn.execute("UPDATE batches SET updated_at=? WHERE id=?", (ts, target))
                for row in rows:
                    old = int(row["batch_id"] or 0)
                    if old and old != target:
                        self.conn.execute(
                            "UPDATE batches SET updated_at=? WHERE id=?", (ts, old))
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return moved

    def set_batch_source_key(self, batch_id: int, source_key: str, source_kind: str = "") -> None:
        if not source_key:
            return
        if source_kind:
            self.execute(
                "UPDATE batches SET source_key=?, source_kind=?, updated_at=? WHERE id=?",
                (source_key, source_kind, now_iso(), batch_id),
            )
        else:
            self.execute(
                "UPDATE batches SET source_key=?, updated_at=? WHERE id=?",
                (source_key, now_iso(), batch_id),
            )

    # -------------------------------------------------------------- articles
    def put_article(self, doc_key: str, article: Article) -> None:
        """写入 / 覆盖一篇原文留档（``doc_key`` 是主键，一个文档一行）。

        ``status`` 也照实存：抓失败的那次会留着原因（``note``），界面据此显示
        「未取到原文」。**失败也要存** —— 否则同一页每划一个词都要重抓一遍，
        既慢又像在反复捶别人服务器。
        """
        key = (doc_key or "").strip()
        if not key:
            return
        self.execute(
            "INSERT INTO articles(doc_key, url, title, text, text_chars, status, note, "
            "fetched_at) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(doc_key) DO UPDATE SET url=excluded.url, title=excluded.title, "
            "text=excluded.text, text_chars=excluded.text_chars, status=excluded.status, "
            "note=excluded.note, fetched_at=excluded.fetched_at",
            (
                key,
                article.url or "",
                article.title or "",
                article.text or "",
                int(article.chars),
                article.status or "",
                article.note or "",
                now_iso(),
            ),
        )

    def get_article(self, doc_key: str) -> sqlite3.Row | None:
        """按 ``doc_key`` 取原文留档；没有（或从没抓过）返回 ``None``。"""
        key = (doc_key or "").strip()
        if not key:
            return None
        return self.query_one("SELECT * FROM articles WHERE doc_key=?", (key,))

    def has_article(self, doc_key: str) -> bool:
        """这一页是不是**已经处理过**（抓到过或明确抓失败过）。

        判定必须包含失败 —— 否则每划一个词都会重抓一遍同一页。
        """
        return self.get_article(doc_key) is not None

    def article_text(self, doc_key: str) -> str:
        """取原文正文；没有 / 抓失败过时返回空串（调用方据此如实说明）。"""
        row = self.get_article(doc_key)
        if row is None:
            return ""
        return str(row["text"] or "") if str(row["status"] or "") == "ok" else ""

    def delete_article(self, doc_key: str) -> None:
        key = (doc_key or "").strip()
        if key:
            self.execute("DELETE FROM articles WHERE doc_key=?", (key,))

    # -------------------------------------------------------------- entries
    def add_entry(
        self,
        *,
        batch_id: int,
        term: str,
        context: str = "",
        doc_key: str = "",
        source_app: str = "",
        source_title: str = "",
        source_url: str = "",
        source_confidence: str = "window_title_only",
        source_note: str = "",
        capture_method: str = "manual_input",
        captured_at: str | None = None,
    ) -> int:
        cur = self.execute(
            "INSERT INTO entries(batch_id, term, context, doc_key, source_app, source_title, "
            "source_url, source_confidence, source_note, capture_method, captured_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                batch_id,
                term,
                context,
                doc_key,
                source_app,
                source_title,
                source_url,
                source_confidence,
                source_note,
                capture_method,
                captured_at or now_iso(),
            ),
        )
        self.touch_batch(batch_id)
        return int(cur.lastrowid)

    def find_entry(self, batch_id: int, term: str, doc_key: str, context: str = "") -> sqlite3.Row | None:
        """按「批次 + 术语 + 文档键 + **规范化上下文**」查找同一条目。

        同一文档里同一个词可能出现在不同语境（不同含义），只按 term+doc_key
        去重会把它们合成一条、丢掉语境，因此 context 必须参与匹配。
        """
        return self.query_one(
            "SELECT * FROM entries WHERE batch_id=? AND term=? AND doc_key=? AND context=? LIMIT 1",
            (batch_id, term, doc_key, context),
        )

    def find_entry_by_term(self, batch_id: int, term: str, doc_key: str) -> sqlite3.Row | None:
        """按「批次 + 术语 + 文档键」找**任意上下文**的同名词条（最近捕获的一条）。

        :meth:`find_entry` 要求上下文也一致；这里刻意放开上下文 —— 它是「同一个
        概念再次被划到」的判据（B4 追加上下文），返回最新那条（``captured_at``
        新、``id`` 大者优先），因为追加要追加到用户最近看到的那条上。
        """
        return self.query_one(
            "SELECT * FROM entries WHERE batch_id=? AND term=? AND doc_key=? "
            "ORDER BY captured_at DESC, id DESC LIMIT 1",
            (batch_id, term, doc_key),
        )

    def append_entry_context(self, entry_id: int, snippet: str, *,
                             captured_at: str | None = None,
                             invalidate_explanation: bool = True) -> str:
        """把一段新上下文**追加**到已有词条（B4），返回追加后的完整上下文。

        * 片段先做空白规范化；**已经在上下文里出现过**（按 ``／`` 分段逐段比对）
          就不再重复追加 —— 只是词频 +1、时间戳刷新（重复划到同一个词不会把
          上下文越堆越长）；
        * 上下文总长封顶 :data:`MAX_CONTEXT_CHARS`：先按剩余额度截断新片段，
          再拼上 ``CONTEXT_JOINER``；
        * ``invalidate_explanation=True``（默认）把解释标记成 **stale**
          （旧解释只对应旧语境，留着不删 —— 界面显示「需要重新解释」，
          用户重新解释后自然覆盖），这比静默保留一个过时解释诚实。

        返回写入后的 context 原文（调用方据此刷新界面）。
        """
        row = self.query_one("SELECT context FROM entries WHERE id=?", (int(entry_id),))
        if row is None:
            return ""
        current = str(row["context"] or "")
        piece = " ".join(str(snippet or "").split()).strip()
        parts = [p.strip() for p in current.split(CONTEXT_JOINER) if p.strip()]
        if not piece or piece in parts or any(piece in p for p in parts):
            # 同一句话（或它更短的一截）又划到一次：内容不变，只加词频、刷时间。
            self.execute(
                "UPDATE entries SET repeat_count=repeat_count+1, captured_at=? WHERE id=?",
                (captured_at or now_iso(), int(entry_id)),
            )
            return current

        # 这次划到的句子**包含**已有的一段：那是同一句话的更完整版本，
        # 就地升级，不再并排堆一遍（否则用户会看到半句 + 整句重复两次）。
        target = None
        for i, prev in enumerate(parts):
            if prev in piece:
                target = i
                break
        if target is None:
            parts.append(piece)
            target = len(parts) - 1
        else:
            parts[target] = piece

        prefix = CONTEXT_JOINER.join(parts[:target])
        room = MAX_CONTEXT_CHARS - (len(prefix) + len(CONTEXT_JOINER) if prefix else 0)
        parts[target] = parts[target][: max(0, room)]
        if not parts[target]:
            # 额度已经用满：新句子一个字都放不下 —— 当作重复处理，不写半截内容
            self.execute(
                "UPDATE entries SET repeat_count=repeat_count+1, captured_at=? WHERE id=?",
                (captured_at or now_iso(), int(entry_id)),
            )
            return current
        merged = CONTEXT_JOINER.join(parts)
        if invalidate_explanation:
            self.execute(
                "UPDATE entries SET context=?, repeat_count=repeat_count+1, captured_at=?, "
                "explain_status=CASE WHEN explain_status='ok' THEN 'stale' "
                "ELSE explain_status END WHERE id=?",
                (merged, captured_at or now_iso(), int(entry_id)),
            )
        else:
            self.execute(
                "UPDATE entries SET context=?, repeat_count=repeat_count+1, captured_at=? WHERE id=?",
                (merged, captured_at or now_iso(), int(entry_id)),
            )
        return merged

    def bump_entry(self, entry_id: int, captured_at: str | None = None) -> None:
        row = self.query_one("SELECT batch_id FROM entries WHERE id=?", (entry_id,))
        self.execute(
            "UPDATE entries SET repeat_count=repeat_count+1, captured_at=? WHERE id=?",
            (captured_at or now_iso(), entry_id),
        )
        if row:
            self.touch_batch(int(row["batch_id"]))

    def get_entry(self, entry_id: int) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM entries WHERE id=?", (entry_id,))

    def update_entry(self, entry_id: int, **fields) -> None:
        sets, params = _entry_update_sql(fields)
        if not sets:
            return
        params.append(entry_id)
        self.execute(f"UPDATE entries SET {', '.join(sets)} WHERE id=?", tuple(params))

    def apply_explanation(self, entry_id: int, *, expected_term: str, expected_context: str,
                          expected_batch_id: int = 0, fields: dict | None = None,
                          topic: str = "") -> bool:
        """解释结果写回：**同一个锁 / 同一个事务 / 同一组条件**里完成校验与写入。

        条件（任一不满足就 0 行改动、返回 False —— 迟到的旧结果直接作废）：

        * 词条必须仍存在，且 ``term`` / ``context`` 与发起解释时的快照一致
          （被编辑 / 删除的旧结果绝不覆盖新内容）；
        * ``expected_batch_id`` 非 0 时必须仍等于词条当前 ``batch_id``
          （被移动到别的主题的旧结果绝不给**新主题**改名，也不写回）；
        * 可选的 ``topic`` 只在 ``batches.name_source='auto'`` 时改名
          （用户手工改名 / 旧库历史的空来源名字**原样保留**），
          改名与写回在同一次 ``commit`` 内，自动命名与手工改名因此没有竞态窗口。

        ``expected_batch_id == 0`` 表示旧构造 / 内存选区快照：跳过主题归属校验与
        改名，只做词条内容校验（向后兼容）。
        """
        sets, params = _entry_update_sql(fields or {})
        if not sets:
            return False
        with self._lock:
            self._ensure_open()
            try:
                row = self.conn.execute(
                    "SELECT term, context, batch_id FROM entries WHERE id=?",
                    (int(entry_id),),
                ).fetchone()
                if row is None:
                    return False
                if (row["term"] or "") != (expected_term or ""):
                    return False
                if (row["context"] or "") != (expected_context or ""):
                    return False
                batch_id = int(row["batch_id"] or 0)
                if expected_batch_id and batch_id != int(expected_batch_id):
                    return False
                params.append(int(entry_id))
                self.conn.execute(
                    f"UPDATE entries SET {', '.join(sets)} WHERE id=?", tuple(params))
                if topic and expected_batch_id:
                    self.conn.execute(
                        "UPDATE batches SET name=?, updated_at=? "
                        "WHERE id=? AND name_source='auto'",
                        (str(topic), now_iso(), int(expected_batch_id)),
                    )
                self.conn.commit()
                return True
            except Exception:
                self.conn.rollback()
                raise

    def delete_entry(self, entry_id: int) -> None:
        self.execute("DELETE FROM entries WHERE id=?", (entry_id,))

    def list_entries(
        self, batch_id: int | None = None, query: str = "", limit: int = 2000, tag: str = ""
    ) -> list[sqlite3.Row]:
        """列出词条。``query`` 搜**所有文字字段**（含解释、来源、例子与标签名）。

        ``tag`` 非空时只留打了该标签的词条（大小写不敏感，与 ``batch_id`` 是
        「且」关系 —— 这样「某个主题里带某标签的词」也能直接查）。
        """
        where, params = self._entries_where(batch_id, query, tag)
        sql = "SELECT * FROM entries" + where
        sql += " ORDER BY captured_at DESC, id DESC LIMIT ?"
        params.append(limit)
        return self.query(sql, tuple(params))

    def list_entry_ids(self, batch_id: int | None = None, query: str = "",
                       tag: str = "") -> list[int]:
        """当前范围（主题 / 搜索词 / 标签）里**全部**词条 id（按显示顺序，不受分页上限影响）。

        只取 id：主界面要给整个范围默认打勾（新主题一进来就是「全勾」），
        而屏幕上只画得下 ``MAX_CARDS`` 张卡片 —— 拿卡片算范围会漏掉没画出来的词。
        """
        where, params = self._entries_where(batch_id, query, tag)
        rows = self.query("SELECT id FROM entries" + where
                          + " ORDER BY captured_at DESC, id DESC", tuple(params))
        return [int(r["id"]) for r in rows]

    @staticmethod
    def _entries_where(batch_id: int | None, query: str, tag: str = "") -> tuple[str, list]:
        """``list_entries`` / ``count_entries`` 共用的 WHERE 片段与参数。"""
        where, params = [], []
        if batch_id is not None:
            where.append("batch_id=?")
            params.append(int(batch_id))
        if query:
            like = f"%{query}%"
            where.append(
                "(term LIKE ? OR context LIKE ? OR one_line LIKE ? OR detail LIKE ? "
                "OR source_title LIKE ? OR source_url LIKE ? OR examples LIKE ? "
                "OR EXISTS(SELECT 1 FROM entry_tags et JOIN tags t ON t.id=et.tag_id "
                "WHERE et.entry_id=entries.id AND t.name LIKE ?))"
            )
            params.extend([like] * 8)
        text = " ".join(str(tag or "").split()).strip()
        if text:
            where.append(
                "EXISTS(SELECT 1 FROM entry_tags et JOIN tags t ON t.id=et.tag_id "
                "WHERE et.entry_id=entries.id AND t.name = ? COLLATE NOCASE)"
            )
            params.append(text)
        return ((" WHERE " + " AND ".join(where)) if where else ""), params

    def count_entries(self, batch_id: int | None = None, query: str = "", tag: str = "") -> int:
        where, params = self._entries_where(batch_id, query, tag)
        row = self.query_one("SELECT COUNT(*) AS c FROM entries" + where, tuple(params))
        return int(row["c"]) if row else 0

    # ------------------------------------------------------------------ tags
    def list_tags(self) -> list[sqlite3.Row]:
        """所有标签 + 各自动用了多少条词条（空标签不会出现在这里 —— 会顺手清掉）。"""
        self.prune_tags()
        return self.query(
            "SELECT t.name AS name, COUNT(et.entry_id) AS entry_count "
            "FROM tags t LEFT JOIN entry_tags et ON et.tag_id = t.id "
            "GROUP BY t.id ORDER BY t.name COLLATE NOCASE"
        )

    def tags_for(self, entry_id: int) -> list[str]:
        rows = self.query(
            "SELECT t.name AS name FROM entry_tags et JOIN tags t ON t.id=et.tag_id "
            "WHERE et.entry_id=? ORDER BY t.name COLLATE NOCASE",
            (int(entry_id),),
        )
        return [str(r["name"]) for r in rows]

    def tags_for_entries(self, entry_ids) -> dict[int, list[str]]:
        """批量取标签（卡片列表一次查完，避免每条词条一次查询）。"""
        ids = sorted({int(i) for i in (entry_ids or []) if int(i)})
        if not ids:
            return {}
        out: dict[int, list[str]] = {i: [] for i in ids}
        placeholders = ", ".join("?" for _ in ids)
        rows = self.query(
            "SELECT et.entry_id AS entry_id, t.name AS name FROM entry_tags et "
            "JOIN tags t ON t.id=et.tag_id WHERE et.entry_id IN (" + placeholders + ") "
            "ORDER BY t.name COLLATE NOCASE",
            tuple(ids),
        )
        for row in rows:
            out.setdefault(int(row["entry_id"]), []).append(str(row["name"]))
        return out

    def set_entry_tags(self, entry_id: int, names) -> list[str]:
        """把一条词条的标签**整体替换**成 ``names``，返回规范化后的最终结果。

        输入先过 :func:`normalize_tags`（去空白、大小写去重、限量）；写入时
        新标签按需建（``tags.name`` 唯一），旧链接删掉，之后再
        :meth:`prune_tags` 清理没人用的标签行 —— 所以标签列表永远没有孤儿。
        """
        clean = normalize_tags(names)
        eid = int(entry_id)
        with self._lock:
            self._ensure_open()
            try:
                self.conn.execute("DELETE FROM entry_tags WHERE entry_id=?", (eid,))
                ts = now_iso()
                for name in clean:
                    self.conn.execute(
                        "INSERT OR IGNORE INTO tags(name, created_at) VALUES(?, ?)", (name, ts))
                    self.conn.execute(
                        "INSERT OR IGNORE INTO entry_tags(entry_id, tag_id) "
                        "SELECT ?, id FROM tags WHERE name=?", (eid, name))
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        self.prune_tags()
        return clean

    def rename_tag(self, old: str, new: str) -> str:
        """改名；``new`` 已存在就**并过去**（两条标签合成一条）。返回最终名字。"""
        source = " ".join(str(old or "").split()).strip()
        target = normalize_tags([new])
        if not source or not target:
            return ""
        name = target[0]
        with self._lock:
            self._ensure_open()
            try:
                row = self.conn.execute(
                    "SELECT id FROM tags WHERE name=? COLLATE NOCASE", (source,)).fetchone()
                if row is None:
                    return ""
                old_id = int(row["id"])
                self.conn.execute(
                    "INSERT OR IGNORE INTO tags(name, created_at) VALUES(?, ?)",
                    (name, now_iso()))
                new_row = self.conn.execute(
                    "SELECT id FROM tags WHERE name=? COLLATE NOCASE", (name,)).fetchone()
                new_id = int(new_row["id"])
                if new_id != old_id:
                    self.conn.execute(
                        "UPDATE OR IGNORE entry_tags SET tag_id=? WHERE tag_id=?",
                        (new_id, old_id))
                    self.conn.execute("DELETE FROM entry_tags WHERE tag_id=?", (old_id,))
                    self.conn.execute("DELETE FROM tags WHERE id=?", (old_id,))
                else:
                    self.conn.execute("UPDATE tags SET name=? WHERE id=?", (name, old_id))
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return name

    def delete_tag(self, name: str) -> None:
        text = " ".join(str(name or "").split()).strip()
        if not text:
            return
        self.execute("DELETE FROM tags WHERE name=? COLLATE NOCASE", (text,))

    def prune_tags(self) -> int:
        """删掉没有任何词条使用的标签行，返回删掉几条（幂等清理）。"""
        cur = self.execute(
            "DELETE FROM tags WHERE id NOT IN (SELECT DISTINCT tag_id FROM entry_tags)")
        return int(cur.rowcount or 0)

    # ------------------------------------------------------------ relations
    def add_relation(self, src_entry_id: int, dst_entry_id: int, label: str = "相关") -> int:
        cur = self.execute(
            "INSERT OR IGNORE INTO relations(src_entry_id, dst_entry_id, label, origin, created_at) "
            "VALUES(?,?,?, 'user', ?)",
            (src_entry_id, dst_entry_id, label, now_iso()),
        )
        return int(cur.lastrowid or 0)

    def list_relations(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM relations ORDER BY id")

    def delete_relation(self, relation_id: int) -> None:
        self.execute("DELETE FROM relations WHERE id=?", (relation_id,))

    # ------------------------------------------------------- 人工关系（v8）
    def set_manual_relation(self, topic_id: int, src_entry_id: int, dst_entry_id: int,
                            rel_type: str, note: str = "") -> int:
        """写一条**人工关系**（用户在参考关系图里手工加/改的那条），返回新行 id。

        语义要点：

        * 自环（两端同一个词条）直接 :class:`ValueError` —— 图里画不出这种边；
        * ``rel_type`` 去首尾空白，空/全空白一律落到 ``"相关"``；
        * ``topic_id`` 走 ``int()`` 容错：``None`` / 非法值当 0（"未知主题"）；
        * **同一对 ``(src, dst)`` 在同一主题下的旧人工行先删掉再插** —— 用户改类型
          就是「换一行」，这样既不会撞 ``UNIQUE(src_entry_id, dst_entry_id, label)``
          （改类型后旧 label 不同）也不会留下一行旧类型的残留；
        * ``origin`` 固定 ``'user'``（AI 给的候选关系只进 ``map_graphs`` 缓存）。
        """
        if int(src_entry_id) == int(dst_entry_id):
            raise ValueError("关系两端不能是同一个词条")
        try:
            topic = int(topic_id)
        except (TypeError, ValueError):
            topic = 0
        label = " ".join(str(rel_type or "").split()).strip() or "相关"
        src, dst = int(src_entry_id), int(dst_entry_id)
        text = str(note or "")
        with self._lock:
            self._ensure_open()
            try:
                self.conn.execute(
                    "DELETE FROM relations WHERE topic_id=? AND src_entry_id=? AND dst_entry_id=?",
                    (topic, src, dst),
                )
                cur = self.conn.execute(
                    "INSERT INTO relations(src_entry_id, dst_entry_id, label, origin, topic_id, "
                    "note, created_at) VALUES(?,?,?, 'user', ?,?,?)",
                    (src, dst, label, topic, text, now_iso()),
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return int(cur.lastrowid or 0)

    def list_manual_relations(self, topic_id: int | None = None) -> list[sqlite3.Row]:
        """列出人工关系；``topic_id=None`` = 全部主题，否则只看该主题。"""
        if topic_id is None:
            return self.query("SELECT * FROM relations ORDER BY id")
        try:
            topic = int(topic_id)
        except (TypeError, ValueError):
            topic = 0
        return self.query("SELECT * FROM relations WHERE topic_id=? ORDER BY id", (topic,))

    def delete_manual_relation(self, relation_id: int) -> bool:
        """删一条人工关系，返回**是否真的删到了**（没这行就返回 False）。"""
        cur = self.execute("DELETE FROM relations WHERE id=?", (int(relation_id),))
        return int(cur.rowcount or 0) > 0

    # ------------------------------------------------- 词卡位置固定（v9）
    def set_node_pin(self, topic_id: int, entry_id: int, x: float, y: float) -> None:
        """记住一张词卡被拖到哪儿（同一主题 + 同一词条只留一行）。

        坐标为**画布坐标**里的卡片中心点。非有限数字（NaN / inf）直接
        :class:`ValueError` —— 那种坐标会把整张图的外框算成 NaN，宁可当场报错。
        ``topic_id`` 走 ``int()`` 容错：非法值当 0（与人工关系同口径）。
        """
        px, py = float(x), float(y)
        if not (math.isfinite(px) and math.isfinite(py)):
            raise ValueError("卡片坐标必须是有限数字")
        self.execute(
            "INSERT INTO node_pins(topic_id, entry_id, x, y, updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(topic_id, entry_id) DO UPDATE SET x=excluded.x, y=excluded.y, "
            "updated_at=excluded.updated_at",
            (_as_topic(topic_id), int(entry_id), px, py, now_iso()),
        )

    def list_node_pins(self, topic_id: int | None = None) -> dict[int, tuple[float, float]]:
        """该主题下所有被固定过位置的词卡：``{entry_id: (x, y)}``。

        只按主题取 —— 位置对别的主题（别的画布）没有意义；``topic_id=None``
        按 0 处理（"未知主题"），不做跨主题合并。
        """
        rows = self.query("SELECT entry_id, x, y FROM node_pins WHERE topic_id=?",
                          (_as_topic(topic_id),))
        out: dict[int, tuple[float, float]] = {}
        for row in rows:
            try:
                out[int(row["entry_id"])] = (float(row["x"]), float(row["y"]))
            except (TypeError, ValueError):  # pragma: no cover - 列是 REAL，读不出才会到这里
                continue
        return out

    def delete_node_pin(self, topic_id: int, entry_id: int) -> bool:
        """取消固定一张词卡，返回**是否真的取消到了**。"""
        cur = self.execute("DELETE FROM node_pins WHERE topic_id=? AND entry_id=?",
                           (_as_topic(topic_id), int(entry_id)))
        return int(cur.rowcount or 0) > 0

    def clear_node_pins(self, topic_id: int | None = None) -> int:
        """清掉某主题的全部固定位置（「恢复自动布局」），返回取消了几张卡。"""
        cur = self.execute("DELETE FROM node_pins WHERE topic_id=?", (_as_topic(topic_id),))
        return int(cur.rowcount or 0)

    # ------------------------------------------- AI 关系黑名单（v9，配合 F3）
    def block_map_edge(self, topic_id: int, src_entry_id: int,
                       dst_entry_id: int, *, label: str = "", reason: str = "") -> int:
        """记下「这对词之间的 AI 关系是错的」，返回行 id。

        同一对端点在**同一主题**下只留一行（``UNIQUE`` + 先删后插），
        ``label`` / ``reason`` 只是给界面显示用的备注，不参与判定 ——
        判定永远是「这一对端点的候选边不再画」。
        """
        if int(src_entry_id) == int(dst_entry_id):
            raise ValueError("关系两端不能是同一个词条")
        topic = _as_topic(topic_id)
        src, dst = int(src_entry_id), int(dst_entry_id)
        text = " ".join(str(label or "").split()).strip()
        note = str(reason or "")
        with self._lock:
            self._ensure_open()
            try:
                self.conn.execute(
                    "DELETE FROM map_edge_blocks WHERE topic_id=? AND src_entry_id=? "
                    "AND dst_entry_id=?",
                    (topic, src, dst),
                )
                cur = self.conn.execute(
                    "INSERT INTO map_edge_blocks(topic_id, src_entry_id, dst_entry_id, label, "
                    "reason, created_at) VALUES(?,?,?,?,?,?)",
                    (topic, src, dst, text, note, now_iso()),
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return int(cur.lastrowid or 0)

    def list_map_edge_blocks(self, topic_id: int | None = None) -> list[sqlite3.Row]:
        """列出 AI 关系黑名单；``topic_id=None`` = 全部主题，否则只看该主题。"""
        if topic_id is None:
            return self.query("SELECT * FROM map_edge_blocks ORDER BY id")
        return self.query("SELECT * FROM map_edge_blocks WHERE topic_id=? ORDER BY id",
                          (_as_topic(topic_id),))

    def map_edge_block_pairs(self, topic_id: int | None = None) -> set[tuple[int, int]]:
        """黑名单端点的**两个方向**：``{(src, dst), (dst, src)}``，供画图时过滤。"""
        pairs: set[tuple[int, int]] = set()
        for row in self.list_map_edge_blocks(topic_id):
            try:
                src, dst = int(row["src_entry_id"]), int(row["dst_entry_id"])
            except (TypeError, ValueError):  # pragma: no cover - 列是 INTEGER
                continue
            pairs.add((src, dst))
            pairs.add((dst, src))
        return pairs

    def clear_map_edge_blocks(self, topic_id: int, src_entry_id: int, dst_entry_id: int) -> int:
        """删掉这一对端点（**两个方向**）的黑名单记录，返回删了几行。

        用户手工给同一对词加了关系时调用：他显然想要这条边，黑名单不该再拦着。
        """
        src, dst = int(src_entry_id), int(dst_entry_id)
        cur = self.execute(
            "DELETE FROM map_edge_blocks WHERE topic_id=? AND "
            "((src_entry_id=? AND dst_entry_id=?) OR (src_entry_id=? AND dst_entry_id=?))",
            (_as_topic(topic_id), src, dst, dst, src),
        )
        return int(cur.rowcount or 0)

    def delete_map_edge_block(self, block_id: int) -> bool:
        """删一条黑名单记录（界面上的「恢复这条关系」），返回是否真的删到了。"""
        cur = self.execute("DELETE FROM map_edge_blocks WHERE id=?", (int(block_id),))
        return int(cur.rowcount or 0) > 0

    # ---------------------------------------------------------------- cache
    def get_cache(self, cache_key: str) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM explain_cache WHERE cache_key=?", (cache_key,))

    def put_cache(
        self,
        *,
        cache_key: str,
        term: str,
        context: str,
        base_url: str,
        model: str,
        one_line: str,
        detail: str,
        examples: list[str],
        raw: str = "",
        topic: str = "",
    ) -> None:
        self.execute(
            "INSERT INTO explain_cache(cache_key, term, context, base_url, model, one_line, "
            "detail, examples, raw, topic, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(cache_key) DO UPDATE SET one_line=excluded.one_line, detail=excluded.detail, "
            "examples=excluded.examples, raw=excluded.raw, topic=excluded.topic, "
            "created_at=excluded.created_at",
            (
                cache_key,
                term,
                context,
                base_url,
                model,
                one_line,
                detail,
                json.dumps(examples, ensure_ascii=False),
                raw,
                str(topic or ""),
                now_iso(),
            ),
        )

    def count_cache(self) -> int:
        row = self.query_one("SELECT COUNT(*) AS c FROM explain_cache")
        return int(row["c"]) if row else 0

    # --------------------------------------------------------- map graphs
    def put_map_graph(self, *, topic_id: int, fingerprint: str, base_url: str = "",
                      model: str = "", relations: list | None = None,
                      raw: str = "", validation_version: int = 0,
                      dropped: dict | None = None,
                      verdicts: list | None = None) -> None:
        """写一份 AI 参考关系图缓存（键 = 主题 + 内容指纹）。

        ``relations`` 是**已经过证据校验 + 独立核对**的关系列表（``src`` / ``dst``
        都是真实 ``entries.id``）：未通过校验的候选一律不写进来，因此缓存里不会
        出现无依据的连线。``validation_version`` 记录产出这批关系时的校验版本，
        读缓存时版本不一致就当没有缓存（旧的字串校验结果不得直接展示）。
        ``dropped`` 是各道闸门的筛除计数、``verdicts`` 是**每条候选的核对判定**
        （保留 / 不确定 / 不成立 + 模型给的理由）：它们是「这个词为什么成了孤立词」
        的唯一依据，重开窗口必须能原样读回来。计数里为 0 的项不写（没被筛掉就不算
        筛除理由）；两者不传时写空 JSON（``{}`` / ``[]``），读回来口径一致。
        同一 ``(topic_id, fingerprint)`` 重写时整体替换（不做增量合并）。
        """
        counts = {str(key): int(value) for key, value in dict(dropped or {}).items()
                  if int(value or 0)}
        self.execute(
            "INSERT INTO map_graphs(topic_id, fingerprint, base_url, model, payload, raw, "
            "validation_version, dropped, verdicts, created_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(topic_id, fingerprint) DO UPDATE SET base_url=excluded.base_url, "
            "model=excluded.model, payload=excluded.payload, raw=excluded.raw, "
            "validation_version=excluded.validation_version, dropped=excluded.dropped, "
            "verdicts=excluded.verdicts, "
            "created_at=excluded.created_at",
            (
                int(topic_id),
                str(fingerprint),
                str(base_url or ""),
                str(model or ""),
                json.dumps(list(relations or []), ensure_ascii=False),
                str(raw or ""),
                int(validation_version or 0),
                json.dumps(counts, ensure_ascii=False),
                json.dumps(list(verdicts or []), ensure_ascii=False),
                now_iso(),
            ),
        )

    def get_map_graph(self, topic_id: int, fingerprint: str, *, base_url: str | None = None,
                      model: str | None = None) -> sqlite3.Row | None:
        """按「主题 + 内容指纹」取缓存；模型配置给了就一并要求匹配。

        指纹已经包含模型配置，这里的 ``base_url`` / ``model`` 是第二道显式闸门：
        调用方传了就必须完全一致，换模型不会误命中旧图。
        """
        row = self.query_one(
            "SELECT * FROM map_graphs WHERE topic_id=? AND fingerprint=?",
            (int(topic_id), str(fingerprint)),
        )
        if row is None:
            return None
        if base_url is not None and str(row["base_url"] or "") != str(base_url or ""):
            return None
        if model is not None and str(row["model"] or "") != str(model or ""):
            return None
        return row

    def count_map_graphs(self, topic_id: int | None = None) -> int:
        if topic_id is None:
            row = self.query_one("SELECT COUNT(*) AS c FROM map_graphs")
        else:
            row = self.query_one(
                "SELECT COUNT(*) AS c FROM map_graphs WHERE topic_id=?", (int(topic_id),))
        return int(row["c"]) if row else 0

    # ---------------------------------------------------------- chat turns
    def add_chat_turn(self, *, entry_id: int, role: str, content: str,
                      request_id: int = 0, status: str = "ok",
                      created_at: str | None = None) -> int:
        """写入一轮追问对话（用户问题或助手回答）。"""
        cur = self.execute(
            "INSERT INTO chat_turns(entry_id, request_id, role, content, status, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (int(entry_id), int(request_id or 0), str(role), str(content or ""),
             str(status or "ok"), created_at or now_iso()),
        )
        return int(cur.lastrowid)

    def list_chat_turns(self, entry_id: int, limit: int | None = None) -> list[sqlite3.Row]:
        """按时间正序取某词条的对话历史（``limit`` 取**最近** N 条，仍按正序返回）。"""
        if limit is None:
            return self.query(
                "SELECT * FROM chat_turns WHERE entry_id=? ORDER BY id", (int(entry_id),))
        rows = self.query(
            "SELECT * FROM chat_turns WHERE entry_id=? ORDER BY id DESC LIMIT ?",
            (int(entry_id), int(limit)),
        )
        return list(reversed(rows))

    def count_chat_turns(self, entry_id: int | None = None) -> int:
        if entry_id is None:
            row = self.query_one("SELECT COUNT(*) AS c FROM chat_turns")
        else:
            row = self.query_one(
                "SELECT COUNT(*) AS c FROM chat_turns WHERE entry_id=?", (int(entry_id),))
        return int(row["c"]) if row else 0

    def update_chat_turn(self, turn_id: int, *, content: str | None = None,
                         status: str | None = None) -> None:
        sets, params = [], []
        if content is not None:
            sets.append("content=?")
            params.append(str(content))
        if status is not None:
            sets.append("status=?")
            params.append(str(status))
        if not sets:
            return
        params.append(int(turn_id))
        self.execute(f"UPDATE chat_turns SET {', '.join(sets)} WHERE id=?", tuple(params))

    def chat_request_exists(self, entry_id: int, request_id: int) -> bool:
        """该请求是否已经有落库的助手回答（防止重试 / 重复投递写两遍）。"""
        return self.query_one(
            "SELECT 1 FROM chat_turns WHERE entry_id=? AND request_id=? AND role='assistant' "
            "AND status IN ('ok','error') LIMIT 1",
            (int(entry_id), int(request_id)),
        ) is not None

    def delete_chat_turns(self, entry_id: int) -> None:
        self.execute("DELETE FROM chat_turns WHERE entry_id=?", (int(entry_id),))

    # ------------------------------------------------------------ event log
    def log_event(self, kind: str, detail: str = "") -> None:
        try:
            from .logging_setup import redact

            self.execute(
                "INSERT INTO event_log(at, kind, detail) VALUES(?,?,?)",
                (now_iso(), kind, redact(detail)[:2000]),
            )
        except sqlite3.Error:
            pass

    def recent_events(self, limit: int = 200) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM event_log ORDER BY id DESC LIMIT ?", (limit,))
