"""T5 持久化与数据完整性。"""
from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

from tests.support import temp_db, tmp_dir
from app.db import Database, SCHEMA_VERSION


class TestPersistence(unittest.TestCase):
    def test_data_survives_reopen(self):
        """T5：写入后重开连接数据仍在。"""
        with tmp_dir("persist_") as tmp:
            path = Path(tmp) / "p.sqlite3"
            db1 = Database(path)
            bid = db1.create_batch("批次一", "url:https://a.example.com/x", "url_document")
            eid = db1.add_entry(batch_id=bid, term="alpha", context="ctx", doc_key="k")
            db1.set_setting("ui.float_bar.x", "123")
            db1.close()

            db2 = Database(path)
            try:
                self.assertEqual(db2.get_batch(bid)["name"], "批次一")
                self.assertEqual(db2.get_entry(eid)["term"], "alpha")
                self.assertEqual(db2.get_setting("ui.float_bar.x"), "123")
                self.assertEqual(db2.count_entries(), 1)
            finally:
                db2.close()

    def test_batch_delete_cascades_to_entries(self):
        with temp_db() as db:
            bid = db.create_batch("b")
            db.add_entry(batch_id=bid, term="t1")
            db.add_entry(batch_id=bid, term="t2")
            self.assertEqual(db.count_entries(), 2)
            db.delete_batch(bid)
            self.assertEqual(db.count_entries(), 0, "删除批次应级联删除词条")

    def test_pinned_is_unique(self):
        with temp_db() as db:
            a = db.create_batch("a")
            b = db.create_batch("b")
            db.set_pinned(a)
            db.set_pinned(b)
            rows = db.query("SELECT id FROM batches WHERE pinned=1")
            self.assertEqual(len(rows), 1)
            self.assertEqual(int(rows[0]["id"]), b)
            db.set_pinned(None)
            self.assertIsNone(db.pinned_batch())

    def test_search_matches_multiple_fields(self):
        with temp_db() as db:
            bid = db.create_batch("b")
            db.add_entry(batch_id=bid, term="卷积神经网络", context="深度学习中的卷积",
                         source_title="论文 A")
            db.add_entry(batch_id=bid, term="attention", context="自注意力机制",
                         source_title="论文 B")
            self.assertEqual(len(db.list_entries(query="卷积")), 1)
            self.assertEqual(len(db.list_entries(query="论文")), 2)
            self.assertEqual(len(db.list_entries(query="注意力")), 1)
            self.assertEqual(len(db.list_entries(query="不存在的词")), 0)

    def test_entry_update_whitelist(self):
        with temp_db() as db:
            bid = db.create_batch("b")
            eid = db.add_entry(batch_id=bid, term="t")
            db.update_entry(eid, term="t2", source_confidence="manual")
            self.assertEqual(db.get_entry(eid)["term"], "t2")
            self.assertEqual(db.get_entry(eid)["source_confidence"], "manual")
            with self.assertRaises(ValueError):
                db.update_entry(eid, id=999)  # 不允许改主键

    def test_dedupe_and_repeat_count(self):
        with temp_db() as db:
            bid = db.create_batch("b")
            eid = db.add_entry(batch_id=bid, term="t", doc_key="k", context="ctx")
            self.assertIsNotNone(db.find_entry(bid, "t", "k", "ctx"))
            db.bump_entry(eid)
            row = db.get_entry(eid)
            self.assertEqual(row["repeat_count"], 2)
            self.assertEqual(db.count_entries(), 1)

    def test_find_entry_distinguishes_context(self):
        """Codex 审阅：同文档不同语境的同一个词不能合并成一条。"""
        with temp_db() as db:
            bid = db.create_batch("b")
            db.add_entry(batch_id=bid, term="bank", doc_key="k", context="river bank near the sea")
            db.add_entry(batch_id=bid, term="bank", doc_key="k", context="bank account interest")
            self.assertEqual(db.count_entries(), 2)
            self.assertIsNone(db.find_entry(bid, "bank", "k", "third context"))
            self.assertIsNotNone(db.find_entry(bid, "bank", "k", "bank account interest"))

    def test_json_fields_are_valid(self):
        with temp_db() as db:
            bid = db.create_batch("b")
            eid = db.add_entry(batch_id=bid, term="t")
            db.update_entry(eid, examples=json.dumps(["e1", "e2"], ensure_ascii=False))
            self.assertEqual(json.loads(db.get_entry(eid)["examples"]), ["e1", "e2"])

    def test_foreign_keys_enabled(self):
        with temp_db() as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(
                    "INSERT INTO entries(batch_id, term, captured_at) VALUES(?,?,?)",
                    (999999, "x", "2026-01-01T00:00:00+08:00"),
                )

    def test_relations_are_user_origin_only(self):
        with temp_db() as db:
            bid = db.create_batch("b")
            a = db.add_entry(batch_id=bid, term="A")
            b = db.add_entry(batch_id=bid, term="B")
            db.add_relation(a, b, "导致")
            rel = db.list_relations()[0]
            self.assertEqual(rel["origin"], "user", "关系必须标记为用户手动建立")
            self.assertEqual(rel["label"], "导致")
            db.delete_relation(int(rel["id"]))
            self.assertEqual(len(db.list_relations()), 0)


class TestReadOnlyInterface(unittest.TestCase):
    def test_agent_api_is_read_only(self):
        """FR-13：只读接口不能写入。"""
        from app.agent_api import list_batches, search_entries

        with temp_db() as db:
            bid = db.create_batch("只读批次", "url:https://x.example.com/", "url_document")
            db.add_entry(batch_id=bid, term="只读词", doc_key="k")

            batches = list_batches(db.path)
            self.assertEqual(len(batches), 1)
            self.assertEqual(batches[0]["entry_count"], 1)

            entries = search_entries("只读", db_file=db.path)
            self.assertEqual(entries[0]["term"], "只读词")

            ro = Database(db.path, read_only=True)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    ro.execute("INSERT INTO settings(k, v) VALUES('x', 'y')")
            finally:
                ro.close()


class TestSchemaMigration(unittest.TestCase):
    """本轮 v2 → v3 迁移的**数据保留**验证（放在白名单模块里，旧库是临时文件）。

    v3 由 ``Database._ensure_column`` 增列：``batches.name_source`` 与
    ``explain_cache.topic``。迁移只新增，绝不动既有行；老库里的名字来源是空串
    （= 用户自己的名字），自动命名不得覆盖。
    """

    OLD_V2_DDL = """
    CREATE TABLE batches (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
        source_key TEXT NOT NULL DEFAULT '', source_kind TEXT NOT NULL DEFAULT '',
        pinned INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
        term TEXT NOT NULL, context TEXT NOT NULL DEFAULT '', doc_key TEXT NOT NULL DEFAULT '',
        source_app TEXT NOT NULL DEFAULT '', source_title TEXT NOT NULL DEFAULT '',
        source_url TEXT NOT NULL DEFAULT '',
        source_confidence TEXT NOT NULL DEFAULT 'window_title_only',
        source_note TEXT NOT NULL DEFAULT '', capture_method TEXT NOT NULL DEFAULT 'manual_input',
        captured_at TEXT NOT NULL, repeat_count INTEGER NOT NULL DEFAULT 1,
        explain_status TEXT NOT NULL DEFAULT 'none', one_line TEXT NOT NULL DEFAULT '',
        detail TEXT NOT NULL DEFAULT '', examples TEXT NOT NULL DEFAULT '[]',
        model_config TEXT NOT NULL DEFAULT '', explain_error TEXT NOT NULL DEFAULT '',
        explained_at TEXT);
    CREATE TABLE explain_cache (
        cache_key TEXT PRIMARY KEY, term TEXT NOT NULL, context TEXT NOT NULL DEFAULT '',
        base_url TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
        one_line TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '',
        examples TEXT NOT NULL DEFAULT '[]', raw TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL);
    CREATE TABLE settings (k TEXT PRIMARY KEY, v TEXT NOT NULL);
    INSERT INTO settings(k, v) VALUES('schema_version', '2');
    INSERT INTO settings(k, v) VALUES('ui.topmost', '1');
    """

    def _make_old_db(self, path: Path) -> tuple[int, int]:
        conn = sqlite3.connect(str(path))
        conn.executescript(self.OLD_V2_DDL)
        cur = conn.execute(
            "INSERT INTO batches(name, source_key, created_at, updated_at) VALUES(?,?,?,?)",
            ("旧主题", "url:https://old.example.com/", "2026-01-01T00:00:00+08:00",
             "2026-01-01T00:00:00+08:00"))
        bid = int(cur.lastrowid)
        cur = conn.execute(
            "INSERT INTO entries(batch_id, term, context, captured_at, one_line) "
            "VALUES(?,?,?,?,?)",
            (bid, "旧词", "旧语境", "2026-01-01T00:00:00+08:00", "旧的解释"))
        eid = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO explain_cache(cache_key, term, context, base_url, model, "
            "one_line, created_at) VALUES(?,?,?,?,?,?,?)",
            ("old-cache-key", "旧词", "旧语境", "https://api.example.com/v1", "model-A",
             "缓存里的旧解释", "2026-01-01T00:00:00+08:00"))
        conn.commit()
        conn.close()
        return bid, eid

    def _columns(self, db: Database, table: str) -> set[str]:
        return {str(r["name"]) for r in db.query(f"PRAGMA table_info({table})")}

    def test_old_v2_db_keeps_data_and_gains_the_new_columns(self):
        with tmp_dir("dbmig_") as tmp:
            path = Path(tmp) / "old.sqlite3"
            bid, eid = self._make_old_db(path)

            db = Database(path)                     # 打开 = 增量迁移
            try:
                self.assertEqual(db.get_setting("schema_version"), str(SCHEMA_VERSION),
                                 "版本号必须跟随 app.db.SCHEMA_VERSION")
                self.assertGreaterEqual(int(SCHEMA_VERSION), 3)
                self.assertIn("name_source", self._columns(db, "batches"),
                              "v2 → v3：batches 必须补上 name_source")
                self.assertIn("topic", self._columns(db, "explain_cache"),
                              "v2 → v3：explain_cache 必须补上 topic")

                # ---- 既有数据一条不少 ----
                self.assertEqual(db.get_setting("ui.topmost"), "1", "旧设置必须保留")
                row = db.get_entry(eid)
                self.assertEqual(row["term"], "旧词")
                self.assertEqual(row["context"], "旧语境")
                self.assertEqual(row["one_line"], "旧的解释")
                cached = db.get_cache("old-cache-key")
                self.assertEqual(cached["one_line"], "缓存里的旧解释")
                self.assertEqual(db.count_entries(), 1)

                # ---- 老库的名字来源是空串：属于用户的名字，自动命名不许覆盖 ----
                self.assertEqual(db.batch_name_source(bid), "")
                self.assertFalse(db.rename_batch_auto(bid, "自动起的名字"),
                                 "老库主题名不得被自动命名覆盖")
                self.assertEqual(db.get_batch(bid)["name"], "旧主题")

                # v1 → v2 的对话表也要在（迁移脚本全部幂等执行）
                self.assertIn("chat_turns", {str(r["name"]) for r in db.query(
                    "SELECT name FROM sqlite_master WHERE type='table'")})
                turn_id = db.add_chat_turn(entry_id=eid, role="user", content="迁移后的问题")
                self.assertTrue(turn_id)

                # v3 → v4 的导图缓存表同样只新增：老库打开后即可用，旧数据一条不少
                self.assertIn("map_graphs", {str(r["name"]) for r in db.query(
                    "SELECT name FROM sqlite_master WHERE type='table'")})
                db.put_map_graph(topic_id=bid, fingerprint="fp-1", base_url="https://api.example.com/v1",
                                 model="model-A",
                                 relations=[{"src": eid, "dst": eid, "type": "包含"}])
                self.assertEqual(db.count_map_graphs(bid), 1)
            finally:
                db.close()

            db2 = Database(path)                    # 再开一次：迁移必须幂等
            try:
                self.assertEqual(db2.count_entries(), 1)
                self.assertEqual(db2.count_chat_turns(), 1)
                self.assertEqual(db2.get_setting("schema_version"), str(SCHEMA_VERSION))
                self.assertEqual(db2.get_entry(eid)["term"], "旧词")
                self.assertEqual(db2.count_map_graphs(), 1, "导图缓存不得在重开时丢失")
            finally:
                db2.close()

    def test_map_graph_cache_is_isolated_by_topic_and_fingerprint(self):
        """导图缓存：按 (主题, 内容指纹) 隔离；换模型不误命中；旧 relations 不受影响。"""
        with temp_db() as db:
            bid = db.create_batch("主题")
            other = db.create_batch("别的主题")
            first = db.add_entry(batch_id=bid, term="A")
            second = db.add_entry(batch_id=bid, term="B")
            db.put_map_graph(topic_id=bid, fingerprint="fp-1", base_url="https://x/v1",
                             model="model-A",
                             relations=[{"src": first, "dst": second, "type": "因果",
                                         "reason": "依据", "evidence": "片段"}])
            row = db.get_map_graph(bid, "fp-1", base_url="https://x/v1", model="model-A")
            self.assertIsNotNone(row)
            self.assertIn("因果", row["payload"])
            self.assertIsNone(db.get_map_graph(bid, "fp-2"), "换了指纹就是另一份内容")
            self.assertIsNone(db.get_map_graph(other, "fp-1"), "缓存按主题隔离")
            self.assertIsNone(db.get_map_graph(bid, "fp-1", model="model-B"),
                              "换模型不得误命中旧图")
            # 重写同一键 = 整体替换（不做增量合并）
            db.put_map_graph(topic_id=bid, fingerprint="fp-1", base_url="https://x/v1",
                             model="model-A", relations=[])
            self.assertEqual(db.get_map_graph(bid, "fp-1")["payload"], "[]")
            # AI 参考关系**只**进 map_graphs：旧的 relations 表仍然是用户手动数据
            self.assertEqual(db.list_relations(), [])

    def test_relations_are_user_origin_only_and_never_written_by_the_map(self):
        """AI 关系绝不写进 relations 表：旧的手动关系数据保持 user 来源。"""
        with temp_db() as db:
            bid = db.create_batch("b")
            first = db.add_entry(batch_id=bid, term="A")
            second = db.add_entry(batch_id=bid, term="B")
            db.add_relation(first, second, "导致")
            db.put_map_graph(topic_id=bid, fingerprint="fp", relations=[
                {"src": second, "dst": first, "type": "因果", "reason": "r", "evidence": "e"}])
            rows = db.list_relations()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["origin"], "user")
            self.assertEqual(int(rows[0]["src_entry_id"]), first)
            self.assertEqual(db.count_map_graphs(bid), 1)


class TestMapVerdictsArePersisted(unittest.TestCase):
    """v6：核对判定（dropped 计数 + 每条候选的 verdict/note）必须随图一起落库。

    这是「这个词为什么成了孤立词」的唯一依据：不落库，用户重开窗口就只能
    看到一句「孤立词」，说不出原因（2026-10 的真实缺陷）。
    """

    RECORD = {
        "index": 0, "src": 1, "dst": 2, "type": "依赖",
        "reason": "ML 算法部署在真实环境中", "evidence": "deployed in real-world environments",
        "verdict": "contradicted", "note": "材料只是场景描述，并未表达依赖。", "kept": False,
    }

    def test_dropped_and_verdicts_survive_a_round_trip(self):
        with temp_db() as db:
            bid = db.create_batch("主题")
            db.put_map_graph(topic_id=bid, fingerprint="fp-1", base_url="https://x/v1",
                             model="model-A", relations=[],
                             dropped={"verify_contradicted": 3, "verify_supported": 1,
                                      "evidence": 0},
                             verdicts=[self.RECORD])
            row = db.get_map_graph(bid, "fp-1", base_url="https://x/v1", model="model-A")
            self.assertEqual(json.loads(row["dropped"]),
                             {"verify_contradicted": 3, "verify_supported": 1},
                             "计数里只留真正筛掉的项，0 不写")
            self.assertEqual(json.loads(row["verdicts"]), [self.RECORD])
            self.assertEqual(db.count_map_graphs(bid), 1)

    def test_rewriting_the_same_key_replaces_the_old_verdicts(self):
        """重写同一 (主题, 指纹) = 整体替换：旧判定不许残留。"""
        with temp_db() as db:
            bid = db.create_batch("主题")
            db.put_map_graph(topic_id=bid, fingerprint="fp", dropped={"verify_bad": 2},
                             verdicts=[self.RECORD])
            db.put_map_graph(topic_id=bid, fingerprint="fp", relations=[],
                             dropped={}, verdicts=[])
            row = db.get_map_graph(bid, "fp")
            self.assertEqual(json.loads(row["dropped"]), {})
            self.assertEqual(json.loads(row["verdicts"]), [])

    def test_old_call_style_still_works(self):
        """不传新参数照旧可写：两个字段落空 JSON，读回来口径一致（老代码不受影响）。"""
        with temp_db() as db:
            bid = db.create_batch("主题")
            db.put_map_graph(topic_id=bid, fingerprint="fp", relations=[])
            row = db.get_map_graph(bid, "fp")
            self.assertEqual(json.loads(row["dropped"]), {})
            self.assertEqual(json.loads(row["verdicts"]), [])

    def test_a_v5_database_gains_the_two_columns_without_losing_rows(self):
        with tmp_dir("dbv6_") as tmp:
            path = Path(tmp) / "v5.sqlite3"
            conn = sqlite3.connect(str(path))
            try:
                conn.executescript(
                    "CREATE TABLE settings(k TEXT PRIMARY KEY, v TEXT NOT NULL);"
                    "CREATE TABLE map_graphs(id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    " topic_id INTEGER NOT NULL, fingerprint TEXT NOT NULL,"
                    " base_url TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',"
                    " payload TEXT NOT NULL DEFAULT '', raw TEXT NOT NULL DEFAULT '',"
                    " validation_version INTEGER NOT NULL DEFAULT 0,"
                    " created_at TEXT NOT NULL, UNIQUE(topic_id, fingerprint));")
                conn.execute("INSERT INTO settings(k, v) VALUES('schema_version', '5')")
                conn.execute(
                    "INSERT INTO map_graphs(topic_id, fingerprint, base_url, model, payload,"
                    " validation_version, created_at) VALUES(7, 'old-fp', 'https://x/v1',"
                    " 'model-A', '[{\"src\":1,\"dst\":2,\"type\":\"因果\"}]', 2,"
                    " '2026-01-01T00:00:00+08:00')")
                conn.commit()
            finally:
                conn.close()

            db = Database(path)
            try:
                columns = {str(r["name"]) for r in db.query("PRAGMA table_info(map_graphs)")}
                self.assertIn("dropped", columns, "v5 → v6：map_graphs 必须补上 dropped")
                self.assertIn("verdicts", columns, "v5 → v6：map_graphs 必须补上 verdicts")
                self.assertEqual(db.get_setting("schema_version"), str(SCHEMA_VERSION))
                row = db.get_map_graph(7, "old-fp")
                self.assertIn("因果", row["payload"], "老缓存行一条不少")
                self.assertEqual(row["dropped"], "", "老行的新列 = 默认空串")
                self.assertEqual(row["verdicts"], "")
            finally:
                db.close()

            again = Database(path)                  # 迁移幂等：再开一次不炸
            try:
                self.assertEqual(again.count_map_graphs(), 1)
            finally:
                again.close()


class TestTagsMergeAndContext(unittest.TestCase):
    """改进清单 B 批的数据层：标签（B1）、主题合并/拆分（B2）、追加上下文（B4）。

    这一批的新接口都是「用户的整理动作」，所以重点在**不破坏已有数据**：
    合并只能是搬家（词条一条不少）、追加只能加内容（重复的语境不再堆一遍）、
    旧解释在上下文变化后必须变成「需重新解释」而不是悄悄过期。
    """

    def test_schema_gains_the_tag_tables(self):
        from app.db import SCHEMA_VERSION as version

        with temp_db() as db:
            tables = {str(r["name"]) for r in db.query(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("tags", tables, "v7 必须建 tags 表")
            self.assertIn("entry_tags", tables, "v7 必须建 entry_tags 关联表")
            self.assertEqual(db.get_setting("schema_version"), str(version))

    def test_normalize_tags_splits_and_dedupes(self):
        from app.db import normalize_tags

        self.assertEqual(normalize_tags(" 经济学, 博弈论、经济学 ／ 微观 "),
                         ["经济学", "博弈论", "微观"])
        self.assertEqual(normalize_tags(["A", "a", " A "]), ["A"], "去重不区分大小写")
        self.assertEqual(normalize_tags(""), [])
        self.assertEqual(normalize_tags(None), [])
        long_tag = "x" * 40
        self.assertEqual(len(normalize_tags(long_tag)[0]), 24, "单个标签最多 24 字")
        many = normalize_tags(" ".join(f"t{i}" for i in range(20)))
        self.assertEqual(len(many), 12, "一条词条最多 12 个标签")

    def test_set_and_read_entry_tags(self):
        with temp_db() as db:
            bid = db.create_batch("b")
            eid = db.add_entry(batch_id=bid, term="边际效用")
            self.assertEqual(db.set_entry_tags(eid, ["经济学", "微观", "经济学"]),
                             ["经济学", "微观"])
            self.assertEqual(db.tags_for(eid), ["微观", "经济学"])
            names = {str(r["name"]): int(r["entry_count"]) for r in db.list_tags()}
            self.assertEqual(names, {"微观": 1, "经济学": 1})

    def test_replace_tags_drops_orphans(self):
        with temp_db() as db:
            bid = db.create_batch("b")
            eid = db.add_entry(batch_id=bid, term="t")
            db.set_entry_tags(eid, ["旧标签"])
            db.set_entry_tags(eid, ["新标签"])
            self.assertEqual([str(r["name"]) for r in db.list_tags()], ["新标签"],
                             "不再被任何词条引用的标签要清掉，免得筛选列表越用越长")

    def test_rename_tag_merges_when_target_exists(self):
        with temp_db() as db:
            bid = db.create_batch("b")
            a = db.add_entry(batch_id=bid, term="a")
            b = db.add_entry(batch_id=bid, term="b")
            db.set_entry_tags(a, ["微观"])
            db.set_entry_tags(b, ["经济学"])
            self.assertEqual(db.rename_tag("微观", "经济学"), "经济学")
            self.assertEqual(db.tags_for(a), ["经济学"])
            self.assertEqual(db.tags_for(b), ["经济学"])
            self.assertEqual(len(db.list_tags()), 1, "改名撞车 = 合并成一条")
            self.assertEqual(db.rename_tag("不存在", "x"), "")

    def test_delete_tag_keeps_entries(self):
        with temp_db() as db:
            bid = db.create_batch("b")
            eid = db.add_entry(batch_id=bid, term="t")
            db.set_entry_tags(eid, ["经济学"])
            db.delete_tag("经济学")
            self.assertEqual(db.tags_for(eid), [])
            self.assertIsNotNone(db.get_entry(eid), "删标签不能连词条一起删")

    def test_list_entries_can_filter_by_tag(self):
        with temp_db() as db:
            b1 = db.create_batch("主题一")
            b2 = db.create_batch("主题二")
            a = db.add_entry(batch_id=b1, term="供给")
            c = db.add_entry(batch_id=b2, term="需求")
            db.set_entry_tags(a, ["经济学"])
            db.set_entry_tags(c, ["经济学"])
            self.assertEqual(len(db.list_entries(tag="经济学")), 2, "标签筛选跨主题")
            self.assertEqual(len(db.list_entries(batch_id=b1, tag="经济学")), 1)
            self.assertEqual(len(db.list_entries(tag="经济")), 0, "标签是精确匹配")
            self.assertEqual(db.count_entries(tag="经济学"), 2)

    def test_search_also_matches_tags_and_examples(self):
        with temp_db() as db:
            bid = db.create_batch("b")
            eid = db.add_entry(batch_id=bid, term="供给")
            db.set_entry_tags(eid, ["经济学"])
            db.update_entry(eid, examples=json.dumps(["稀缺性决定价格"], ensure_ascii=False))
            self.assertEqual(len(db.list_entries(query="经济学")), 1)
            self.assertEqual(len(db.list_entries(query="稀缺性")), 1)
            self.assertEqual(db.count_entries(query="稀缺性"), 1)

    def test_tags_for_entries_is_a_batch_read(self):
        with temp_db() as db:
            bid = db.create_batch("b")
            a = db.add_entry(batch_id=bid, term="a")
            b = db.add_entry(batch_id=bid, term="b")
            db.set_entry_tags(a, ["x", "y"])
            self.assertEqual(db.tags_for_entries([a, b]), {a: ["x", "y"], b: []})
            self.assertEqual(db.tags_for_entries([]), {})

    def test_merge_batches_moves_every_entry(self):
        with temp_db() as db:
            keep = db.create_batch("长文档")
            split = db.create_batch("被拆出来的一半")
            e1 = db.add_entry(batch_id=keep, term="A")
            e2 = db.add_entry(batch_id=split, term="B")
            self.assertEqual(db.merge_batches(keep, [split]), 1)
            self.assertEqual(int(db.get_entry(e2)["batch_id"]), keep)
            self.assertIsNone(db.get_batch(split), "源主题合并后不再存在")
            self.assertIsNotNone(db.get_entry(e1))
            names = [str(r["name"]) for r in db.list_batches()]
            self.assertEqual(names.count("长文档"), 1)

    def test_merge_batches_ignores_self_and_empty(self):
        with temp_db() as db:
            keep = db.create_batch("a")
            db.add_entry(batch_id=keep, term="A")
            self.assertEqual(db.merge_batches(keep, [keep]), 0)
            self.assertEqual(db.merge_batches(keep, []), 0)
            self.assertIsNotNone(db.get_batch(keep))

    def test_merge_batches_drops_the_source_map_graph(self):
        """源主题的图谱缓存必须跟着走，否则合并后还能看到已经不存在的主题的图。"""
        with temp_db() as db:
            keep = db.create_batch("a")
            gone = db.create_batch("b")
            db.put_map_graph(topic_id=gone, fingerprint="fp", relations=[])
            db.merge_batches(keep, [gone])
            self.assertEqual(db.count_map_graphs(gone), 0)

    def test_move_entries_keeps_both_topics(self):
        with temp_db() as db:
            src = db.create_batch("源")
            dst = db.create_batch("目标")
            eid = db.add_entry(batch_id=src, term="A")
            self.assertEqual(db.move_entries([eid], dst), 1)
            self.assertEqual(int(db.get_entry(eid)["batch_id"]), dst)
            self.assertIsNotNone(db.get_batch(src), "拆分 = 搬家，源主题要留着")
            self.assertEqual(db.count_entries(src), 0)
            self.assertEqual(db.count_entries(dst), 1)

    def test_move_entries_ignores_unknown_ids(self):
        with temp_db() as db:
            dst = db.create_batch("目标")
            self.assertEqual(db.move_entries([999999], dst), 0)
            self.assertEqual(db.move_entries([], dst), 0)

    def test_find_entry_by_term_ignores_context(self):
        with temp_db() as db:
            bid = db.create_batch("b")
            eid = db.add_entry(batch_id=bid, term="bank", doc_key="k", context="river bank")
            found = db.find_entry_by_term(bid, "bank", "k")
            self.assertIsNotNone(found, "B4 要按「词 + 文档」找，不看上下文")
            self.assertEqual(int(found["id"]), eid)
            self.assertIsNone(db.find_entry_by_term(bid, "bank", "其它文档"))

    def test_append_entry_context_appends_and_dedupes(self):
        from app.db import CONTEXT_JOINER

        with temp_db() as db:
            bid = db.create_batch("b")
            eid = db.add_entry(batch_id=bid, term="t", context="第一处语境")
            merged = db.append_entry_context(eid, "第二处语境")
            self.assertEqual(merged, f"第一处语境{CONTEXT_JOINER}第二处语境")
            again = db.append_entry_context(eid, "第二处语境")
            self.assertEqual(again, merged, "同一句话再划到不该重复堆进去")
            partial = db.append_entry_context(eid, "第二处语境的后半句")
            self.assertEqual(partial, f"第一处语境{CONTEXT_JOINER}第二处语境的后半句",
                             "更完整的同一句话应就地升级，而不是并排堆两段")
            self.assertEqual(db.append_entry_context(eid, "第二处语境"), partial,
                             "再划到被包含的短句也是重复，不再变长")
            self.assertEqual(db.get_entry(eid)["repeat_count"], 5,
                             "四次重复划选都要计数（1 次原始 + 4 次追加）")

    def test_append_entry_context_marks_the_old_explanation_stale(self):
        with temp_db() as db:
            bid = db.create_batch("b")
            eid = db.add_entry(batch_id=bid, term="t", context="旧语境")
            db.update_entry(eid, one_line="旧解释", explain_status="ok")
            self.assertEqual(db.get_entry(eid)["explain_status"], "ok")
            db.append_entry_context(eid, "新语境")
            self.assertEqual(db.get_entry(eid)["explain_status"], "stale",
                             "上下文变了，旧解释必须变成「需重新解释」")

    def test_append_entry_context_can_skip_invalidation(self):
        with temp_db() as db:
            bid = db.create_batch("b")
            eid = db.add_entry(batch_id=bid, term="t", context="旧语境")
            db.update_entry(eid, one_line="旧解释", explain_status="ok")
            db.append_entry_context(eid, "新语境", invalidate_explanation=False)
            self.assertEqual(db.get_entry(eid)["explain_status"], "ok")

    def test_append_entry_context_respects_the_length_budget(self):
        from app.db import MAX_CONTEXT_CHARS

        with temp_db() as db:
            bid = db.create_batch("b")
            eid = db.add_entry(batch_id=bid, term="t", context="起点")
            db.append_entry_context(eid, "长" * 5000)
            self.assertLessEqual(len(db.get_entry(eid)["context"]), MAX_CONTEXT_CHARS)


class TestManualRelations(unittest.TestCase):
    """v8 的人工关系存储：``relations`` 补 ``topic_id`` / ``note`` + 三个新方法。

    这里全是**纯数据库**用例：不建任何 Tk 窗口、不联网。
    """

    #: v7 时代的 ``relations``：**没有** ``topic_id`` / ``note`` 两列。
    #: 手写一份老结构（而不是复用 ``app.db._DDL``）才能真正验证「老库升上来」。
    OLD_V7_DDL = """
    CREATE TABLE batches (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
        source_key TEXT NOT NULL DEFAULT '', source_kind TEXT NOT NULL DEFAULT '',
        pinned INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        name_source TEXT NOT NULL DEFAULT '');
    CREATE TABLE entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
        term TEXT NOT NULL, context TEXT NOT NULL DEFAULT '', doc_key TEXT NOT NULL DEFAULT '',
        source_app TEXT NOT NULL DEFAULT '', source_title TEXT NOT NULL DEFAULT '',
        source_url TEXT NOT NULL DEFAULT '',
        source_confidence TEXT NOT NULL DEFAULT 'window_title_only',
        source_note TEXT NOT NULL DEFAULT '', capture_method TEXT NOT NULL DEFAULT 'manual_input',
        captured_at TEXT NOT NULL, repeat_count INTEGER NOT NULL DEFAULT 1,
        explain_status TEXT NOT NULL DEFAULT 'none', one_line TEXT NOT NULL DEFAULT '',
        detail TEXT NOT NULL DEFAULT '', examples TEXT NOT NULL DEFAULT '[]',
        model_config TEXT NOT NULL DEFAULT '', explain_error TEXT NOT NULL DEFAULT '',
        explained_at TEXT);
    CREATE TABLE explain_cache (
        cache_key TEXT PRIMARY KEY, term TEXT NOT NULL, context TEXT NOT NULL DEFAULT '',
        base_url TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
        one_line TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '',
        examples TEXT NOT NULL DEFAULT '[]', raw TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL, topic TEXT NOT NULL DEFAULT '');
    CREATE TABLE relations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        src_entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
        dst_entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
        label TEXT NOT NULL DEFAULT '相关', origin TEXT NOT NULL DEFAULT 'user',
        created_at TEXT NOT NULL, UNIQUE(src_entry_id, dst_entry_id, label));
    CREATE TABLE settings (k TEXT PRIMARY KEY, v TEXT NOT NULL);
    INSERT INTO settings(k, v) VALUES('schema_version', '7');
    INSERT INTO settings(k, v) VALUES('ui.topmost', '1');
    """

    def _columns(self, db: Database, table: str) -> set[str]:
        return {str(r["name"]) for r in db.query(f"PRAGMA table_info({table})")}

    def test_schema_version_is_9(self):
        self.assertEqual(SCHEMA_VERSION, 9)

    def test_new_db_has_topic_and_note_columns(self):
        with temp_db() as db:
            cols = self._columns(db, "relations")
            self.assertIn("topic_id", cols)
            self.assertIn("note", cols)
            self.assertEqual(db.get_setting("schema_version"), str(SCHEMA_VERSION))

    def test_topic_index_exists(self):
        """``idx_relations_topic`` 必须真的建出来（``IF NOT EXISTS`` 便于老库重复执行）。"""
        with temp_db() as db:
            row = db.query_one(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_relations_topic'")
            self.assertIsNotNone(row, "v8 需要 relations(topic_id) 索引")

    def _make_old_v7_db(self, path: Path) -> tuple[int, int, int]:
        """建一个 v7 结构的老库（含一条老人工关系），返回 ``(topic_id, e1, e2, rel_id)``。"""
        conn = sqlite3.connect(str(path))
        try:
            conn.executescript(self.OLD_V7_DDL)
            ts = "2026-01-01T00:00:00+08:00"
            cur = conn.execute(
                "INSERT INTO batches(name, source_key, created_at, updated_at, name_source) "
                "VALUES(?,?,?,?,?)", ("旧主题", "url:https://old.example.com/", ts, ts, "manual"))
            bid = int(cur.lastrowid)
            cur = conn.execute(
                "INSERT INTO entries(batch_id, term, context, captured_at) VALUES(?,?,?,?)",
                (bid, "旧词 A", "旧语境 A", ts))
            e1 = int(cur.lastrowid)
            cur = conn.execute(
                "INSERT INTO entries(batch_id, term, context, captured_at) VALUES(?,?,?,?)",
                (bid, "旧词 B", "旧语境 B", ts))
            e2 = int(cur.lastrowid)
            cur = conn.execute(
                "INSERT INTO relations(src_entry_id, dst_entry_id, label, origin, created_at) "
                "VALUES(?,?,?,?,?)", (e1, e2, "导致", "user", ts))
            rel_id = int(cur.lastrowid)
            conn.commit()
        finally:
            conn.close()
        return bid, e1, e2, rel_id

    def test_old_v7_db_gains_columns_keeps_rows_and_is_idempotent(self):
        """老库升级：补两列 + 两张新表 + 版本号变 9 + **旧关系一条不少**，重开也不重复迁移。"""
        with tmp_dir("dbmig_rel_") as tmp:
            path = Path(tmp) / "old_v7.sqlite3"
            bid, e1, e2, rel_id = self._make_old_v7_db(path)

            db = Database(path)                     # 打开 = 增量迁移
            try:
                cols = self._columns(db, "relations")
                self.assertIn("topic_id", cols, "v7 → v8：relations 必须补上 topic_id")
                self.assertIn("note", cols, "v7 → v8：relations 必须补上 note")
                self.assertEqual(db.get_setting("schema_version"), "9")

                # ---- v8 → v9：两张新表也在（老库同样要有）----
                for table in ("node_pins", "map_edge_blocks"):
                    self.assertIsNotNone(db.query_one(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)),
                        f"v9 需要 {table} 表")
                # 老库里两张新表都是空的（不凭空造数据）
                self.assertEqual(db.list_node_pins(0), {})
                self.assertEqual(db.map_edge_block_pairs(0), set())

                # ---- 老数据一条都没丢，且新列取到「未知」默认值 ----
                self.assertEqual(db.count_entries(), 2)
                self.assertEqual(db.get_entry(e1)["term"], "旧词 A")
                self.assertEqual(db.get_setting("ui.topmost"), "1", "旧设置必须保留")
                rows = db.list_manual_relations()
                self.assertEqual(len(rows), 1)
                self.assertEqual(int(rows[0]["id"]), rel_id)
                self.assertEqual(rows[0]["label"], "导致")
                self.assertEqual(rows[0]["origin"], "user")
                self.assertEqual(int(rows[0]["topic_id"]), 0, "老关系没有主题归属 → 0")
                self.assertEqual(rows[0]["note"], "", "老关系没有备注 → 空串")

                # 老关系按主题过滤时只落在「未知主题」桶里
                self.assertEqual(len(db.list_manual_relations(0)), 1)
                self.assertEqual(len(db.list_manual_relations(bid)), 0)

                # 老库升级后立刻就能用 v9 的两张新表
                db.set_node_pin(0, e1, 12.5, -3.0)
                db.block_map_edge(0, e1, e2, label="导致", reason="模型给的理由")
            finally:
                db.close()

            db2 = Database(path)                    # 再开一次：迁移必须幂等
            try:
                self.assertEqual(db2.get_setting("schema_version"), "9")
                self.assertEqual(len(db2.list_manual_relations()), 1, "重开不得重复插入或删行")
                self.assertEqual(int(db2.list_manual_relations()[0]["id"]), rel_id)
                self.assertEqual(db2.get_entry(e1)["term"], "旧词 A")
                self.assertEqual(db2.list_node_pins(0), {e1: (12.5, -3.0)})
                self.assertEqual(db2.map_edge_block_pairs(0), {(e1, e2), (e2, e1)})
            finally:
                db2.close()

    def test_set_and_list_round_trip(self):
        with temp_db() as db:
            topic = db.create_batch("主题")
            other = db.create_batch("别的主题")
            a = db.add_entry(batch_id=topic, term="A")
            b = db.add_entry(batch_id=topic, term="B")

            rid = db.set_manual_relation(topic, a, b, "导致", note="因为 A 所以 B")
            self.assertTrue(rid)

            rows = db.list_manual_relations(topic)
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(int(row["id"]), rid)
            self.assertEqual(int(row["topic_id"]), topic)
            self.assertEqual(int(row["src_entry_id"]), a)
            self.assertEqual(int(row["dst_entry_id"]), b)
            self.assertEqual(row["label"], "导致")
            self.assertEqual(row["note"], "因为 A 所以 B")
            self.assertEqual(row["origin"], "user")
            self.assertTrue(str(row["created_at"]).strip(), "created_at 必须写上时间")
            self.assertEqual(db.list_manual_relations(other), [], "不同 topic 互相隔离")

    def test_self_loop_is_rejected(self):
        with temp_db() as db:
            topic = db.create_batch("主题")
            a = db.add_entry(batch_id=topic, term="A")
            with self.assertRaises(ValueError):
                db.set_manual_relation(topic, a, a, "相关")
            self.assertEqual(db.list_manual_relations(), [], "自环不得落库")

    def test_changing_type_replaces_the_row(self):
        with temp_db() as db:
            topic = db.create_batch("主题")
            a = db.add_entry(batch_id=topic, term="A")
            b = db.add_entry(batch_id=topic, term="B")
            first = db.set_manual_relation(topic, a, b, "导致")
            second = db.set_manual_relation(topic, a, b, "包含")
            self.assertNotEqual(first, second, "改类型 = 换一行（新 id）")
            rows = db.list_manual_relations(topic)
            self.assertEqual(len(rows), 1, "同一对 (src, dst) 只能留一行")
            self.assertEqual(rows[0]["label"], "包含")
            self.assertEqual(int(rows[0]["id"]), second)

    def test_same_pair_can_live_in_different_topics(self):
        with temp_db() as db:
            t1 = db.create_batch("主题一")
            t2 = db.create_batch("主题二")
            a = db.add_entry(batch_id=t1, term="A")
            b = db.add_entry(batch_id=t1, term="B")
            r1 = db.set_manual_relation(t1, a, b, "导致")
            r2 = db.set_manual_relation(t2, a, b, "包含")
            self.assertNotEqual(r1, r2)
            self.assertEqual(len(db.list_manual_relations(t1)), 1)
            self.assertEqual(len(db.list_manual_relations(t2)), 1)
            self.assertEqual(db.list_manual_relations(t1)[0]["label"], "导致")
            self.assertEqual(db.list_manual_relations(t2)[0]["label"], "包含")

    def test_list_without_topic_returns_everything(self):
        with temp_db() as db:
            t1 = db.create_batch("主题一")
            t2 = db.create_batch("主题二")
            a = db.add_entry(batch_id=t1, term="A")
            b = db.add_entry(batch_id=t1, term="B")
            db.set_manual_relation(t1, a, b, "导致")
            db.set_manual_relation(t2, b, a, "包含")
            self.assertEqual(len(db.list_manual_relations()), 2, "topic_id=None = 全部")
            ids = [int(r["id"]) for r in db.list_manual_relations()]
            self.assertEqual(ids, sorted(ids), "按 id 排序")

    def test_blank_type_falls_back_to_default_label(self):
        with temp_db() as db:
            topic = db.create_batch("主题")
            a = db.add_entry(batch_id=topic, term="A")
            b = db.add_entry(batch_id=topic, term="B")
            db.set_manual_relation(topic, a, b, "")
            self.assertEqual(db.list_manual_relations()[0]["label"], "相关")
            c = db.add_entry(batch_id=topic, term="C")
            db.set_manual_relation(topic, a, c, "   ")
            self.assertEqual(len(db.list_manual_relations()), 2)
            rows = {int(r["dst_entry_id"]): r for r in db.list_manual_relations()}
            self.assertEqual(rows[c]["label"], "相关", "全空白也必须落到默认类型")

    def test_none_topic_is_stored_as_zero(self):
        with temp_db() as db:
            topic = db.create_batch("主题")
            a = db.add_entry(batch_id=topic, term="A")
            b = db.add_entry(batch_id=topic, term="B")
            rid = db.set_manual_relation(None, a, b, "导致")
            row = db.query_one("SELECT * FROM relations WHERE id=?", (rid,))
            self.assertEqual(int(row["topic_id"]), 0, "topic_id=None ⇒ 0")
            self.assertEqual(len(db.list_manual_relations(0)), 1)
            self.assertEqual(db.list_manual_relations(topic), [], "没有被塞进某个主题")

    def test_note_defaults_to_empty(self):
        with temp_db() as db:
            topic = db.create_batch("主题")
            a = db.add_entry(batch_id=topic, term="A")
            b = db.add_entry(batch_id=topic, term="B")
            db.set_manual_relation(topic, a, b, "导致")
            self.assertEqual(db.list_manual_relations()[0]["note"], "")

    def test_rewriting_the_same_pair_updates_the_note(self):
        with temp_db() as db:
            topic = db.create_batch("主题")
            a = db.add_entry(batch_id=topic, term="A")
            b = db.add_entry(batch_id=topic, term="B")
            db.set_manual_relation(topic, a, b, "导致", note="旧备注")
            db.set_manual_relation(topic, a, b, "导致", note="新备注")
            rows = db.list_manual_relations(topic)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["note"], "新备注")

    def test_delete_manual_relation_reports_hit_and_miss(self):
        with temp_db() as db:
            topic = db.create_batch("主题")
            a = db.add_entry(batch_id=topic, term="A")
            b = db.add_entry(batch_id=topic, term="B")
            rid = db.set_manual_relation(topic, a, b, "导致")
            self.assertTrue(db.delete_manual_relation(rid), "删到行要返回 True")
            self.assertFalse(db.delete_manual_relation(rid), "同一行再删一次返回 False")
            self.assertFalse(db.delete_manual_relation(999999), "不存在的 id 返回 False")
            self.assertEqual(db.list_manual_relations(), [])

    def test_deleting_an_entry_takes_its_manual_relations_with_it(self):
        """词条被删 ⇒ 它的人工关系行必须跟着消失（CASCADE）。

        ``Database.__init__`` 里执行了 ``PRAGMA foreign_keys=ON``，所以这里断言的是
        真实级联；先读一次 pragma 确认前提成立（测试不去改连接的外键策略）。
        """
        with temp_db() as db:
            fk = db.query_one("PRAGMA foreign_keys")
            self.assertEqual(int(fk[0]), 1, "前提：连接必须开着外键约束")
            topic = db.create_batch("主题")
            a = db.add_entry(batch_id=topic, term="A")
            b = db.add_entry(batch_id=topic, term="B")
            db.set_manual_relation(topic, a, b, "导致")
            self.assertEqual(len(db.list_manual_relations()), 1)
            db.delete_entry(b)
            self.assertEqual(db.list_manual_relations(), [], "被删词条的关系行必须级联消失")
            self.assertEqual(db.list_relations(), [])
            self.assertEqual(
                len(db.query("SELECT id FROM relations WHERE src_entry_id=? OR dst_entry_id=?",
                             (b, b))), 0)

    def test_deleting_a_topic_leaves_the_other_topics_relations(self):
        """删主题级联删词条时，只带走该主题的人工关系（别的关系不受影响）。"""
        with temp_db() as db:
            t1 = db.create_batch("主题一")
            t2 = db.create_batch("主题二")
            a = db.add_entry(batch_id=t1, term="A")
            b = db.add_entry(batch_id=t1, term="B")
            c = db.add_entry(batch_id=t2, term="C")
            d = db.add_entry(batch_id=t2, term="D")
            db.set_manual_relation(t1, a, b, "导致")
            keep = db.set_manual_relation(t2, c, d, "包含")
            db.delete_batch(t1)
            rows = db.list_manual_relations()
            self.assertEqual(len(rows), 1)
            self.assertEqual(int(rows[0]["id"]), keep)
            self.assertEqual(int(rows[0]["topic_id"]), t2)

    def test_unknown_entry_ids_are_rejected_by_foreign_keys(self):
        """不存在的词条 id 不许静默写进关系表（外键真的在起作用）。"""
        with temp_db() as db:
            topic = db.create_batch("主题")
            a = db.add_entry(batch_id=topic, term="A")
            with self.assertRaises(sqlite3.IntegrityError):
                db.set_manual_relation(topic, a, 999999, "导致")
            self.assertEqual(db.list_manual_relations(), [], "失败后不得留下半行")

    def test_legacy_helpers_still_work_and_share_the_table(self):
        """老的 ``add_relation`` / ``list_relations`` / ``delete_relation`` 语义不变。"""
        with temp_db() as db:
            topic = db.create_batch("主题")
            a = db.add_entry(batch_id=topic, term="A")
            b = db.add_entry(batch_id=topic, term="B")
            rid = db.add_relation(a, b, "导致")
            self.assertTrue(rid)
            # 老语义：重复插入被 IGNORE（**不新增行**）。
            # 注意 ``add_relation`` 返回的是 ``cur.lastrowid``，而 IGNORE 掉的那次
            # INSERT 不会重置 lastrowid —— 老实现本来就可能回上一次的值，
            # 所以这里只断言「行数没有变多」，不去锁返回值。
            before = len(db.list_relations())
            db.add_relation(a, b, "导致")
            self.assertEqual(len(db.list_relations()), before, "重复插入必须被 IGNORE")
            rel = db.list_relations()[0]
            self.assertEqual(rel["origin"], "user")
            self.assertEqual(int(rel["topic_id"]), 0, "老方法不写主题 ⇒ 默认 0")
            # 老方法写出来的行概念上属于「未知主题（0）」；在同一主题桶里改用
            # set_manual_relation 改类型 ⇒ 旧行被换掉，表里只剩新那一行。
            new_id = db.set_manual_relation(0, a, b, "包含")
            self.assertEqual(len(db.list_relations()), 1)
            self.assertEqual(db.list_relations()[0]["label"], "包含")
            self.assertEqual(int(db.list_relations()[0]["id"]), new_id)
            # 两套接口操作的是同一张表、同一批行
            db.delete_relation(new_id)
            self.assertEqual(db.list_relations(), [])
            self.assertFalse(db.delete_manual_relation(new_id))


class TestNodePins(unittest.TestCase):
    """v9：词卡固定位置（改进清单 F2 —— 拖过的卡片位置存在本机库里）。"""

    def test_set_and_list_round_trip(self):
        with temp_db() as db:
            topic = db.create_batch("主题")
            a = db.add_entry(batch_id=topic, term="A")
            b = db.add_entry(batch_id=topic, term="B")
            db.set_node_pin(topic, a, 12.5, 30.25)
            db.set_node_pin(topic, b, -4.0, 0.0)
            self.assertEqual(db.list_node_pins(topic), {a: (12.5, 30.25), b: (-4.0, 0.0)})

    def test_the_same_card_is_updated_not_duplicated(self):
        with temp_db() as db:
            topic = db.create_batch("主题")
            a = db.add_entry(batch_id=topic, term="A")
            db.set_node_pin(topic, a, 1.0, 2.0)
            db.set_node_pin(topic, a, 5.0, 6.0)
            rows = db.query("SELECT * FROM node_pins WHERE topic_id=?", (topic,))
            self.assertEqual(len(rows), 1, "同一张卡只能有一行")
            self.assertEqual(db.list_node_pins(topic), {a: (5.0, 6.0)}, "后写的位置覆盖先写的")
            self.assertTrue(str(rows[0]["updated_at"]), "必须记下更新时间")

    def test_positions_are_scoped_to_a_topic(self):
        with temp_db() as db:
            t1 = db.create_batch("主题一")
            t2 = db.create_batch("主题二")
            a = db.add_entry(batch_id=t1, term="A")
            b = db.add_entry(batch_id=t2, term="B")
            db.set_node_pin(t1, a, 1.0, 1.0)
            db.set_node_pin(t2, b, 2.0, 2.0)
            self.assertEqual(db.list_node_pins(t1), {a: (1.0, 1.0)})
            self.assertEqual(db.list_node_pins(t2), {b: (2.0, 2.0)})
            self.assertEqual(db.list_node_pins(), {},
                             "不带主题 = 只认「未知主题」(0)：坐标不能跨主题合并")

    def test_none_topic_is_stored_as_the_unknown_topic(self):
        with temp_db() as db:
            topic = db.create_batch("主题")
            a = db.add_entry(batch_id=topic, term="A")
            db.set_node_pin(None, a, 3.0, 4.0)
            self.assertEqual(db.list_node_pins(0), {a: (3.0, 4.0)})
            self.assertEqual(db.list_node_pins(topic), {})

    def test_non_finite_coordinates_are_rejected(self):
        with temp_db() as db:
            topic = db.create_batch("主题")
            a = db.add_entry(batch_id=topic, term="A")
            for bad in (float("nan"), float("inf"), float("-inf")):
                with self.assertRaises(ValueError):
                    db.set_node_pin(topic, a, bad, 0.0)
                with self.assertRaises(ValueError):
                    db.set_node_pin(topic, a, 0.0, bad)
            self.assertEqual(db.list_node_pins(topic), {}, "被拒之后不得留下半行")

    def test_delete_and_clear_report_what_they_removed(self):
        with temp_db() as db:
            topic = db.create_batch("主题")
            a = db.add_entry(batch_id=topic, term="A")
            b = db.add_entry(batch_id=topic, term="B")
            db.set_node_pin(topic, a, 1.0, 1.0)
            db.set_node_pin(topic, b, 2.0, 2.0)
            self.assertTrue(db.delete_node_pin(topic, a))
            self.assertFalse(db.delete_node_pin(topic, a), "删第二次应当报「没有」")
            self.assertEqual(db.clear_node_pins(topic), 1)
            self.assertEqual(db.clear_node_pins(topic), 0)
            self.assertEqual(db.list_node_pins(topic), {})

    def test_unknown_entry_ids_are_rejected_by_foreign_keys(self):
        with temp_db() as db:
            topic = db.create_batch("主题")
            with self.assertRaises(sqlite3.IntegrityError):
                db.set_node_pin(topic, 999999, 1.0, 1.0)

    def test_deleting_an_entry_takes_its_pin_with_it(self):
        with temp_db() as db:
            topic = db.create_batch("主题")
            a = db.add_entry(batch_id=topic, term="A")
            b = db.add_entry(batch_id=topic, term="B")
            db.set_node_pin(topic, a, 1.0, 1.0)
            db.set_node_pin(topic, b, 2.0, 2.0)
            db.delete_entry(a)
            self.assertEqual(db.list_node_pins(topic), {b: (2.0, 2.0)})


class TestMapEdgeBlocks(unittest.TestCase):
    """v9：AI 关系黑名单（改进清单 F3 —— 「这条不对」以后不再出现）。"""

    def test_block_and_read_back_both_directions(self):
        with temp_db() as db:
            topic = db.create_batch("主题")
            a = db.add_entry(batch_id=topic, term="A")
            b = db.add_entry(batch_id=topic, term="B")
            rid = db.block_map_edge(topic, a, b, label="导致", reason="模型说 A 导致 B")
            self.assertTrue(rid)
            rows = db.list_map_edge_blocks(topic)
            self.assertEqual(len(rows), 1)
            self.assertEqual(int(rows[0]["src_entry_id"]), a)
            self.assertEqual(int(rows[0]["dst_entry_id"]), b)
            self.assertEqual(rows[0]["label"], "导致")
            self.assertEqual(rows[0]["reason"], "模型说 A 导致 B")
            self.assertTrue(str(rows[0]["created_at"]))
            # 两个方向都算被屏蔽（AI 边反向提出来同样不该画）
            self.assertEqual(db.map_edge_block_pairs(topic), {(a, b), (b, a)})

    def test_blocking_the_same_pair_twice_keeps_one_row(self):
        with temp_db() as db:
            topic = db.create_batch("主题")
            a = db.add_entry(batch_id=topic, term="A")
            b = db.add_entry(batch_id=topic, term="B")
            db.block_map_edge(topic, a, b, label="导致")
            db.block_map_edge(topic, a, b, label="包含", reason="改主意了")
            rows = db.list_map_edge_blocks(topic)
            self.assertEqual(len(rows), 1, "同一对端点只留一行")
            self.assertEqual(rows[0]["label"], "包含")

    def test_self_loop_is_rejected(self):
        with temp_db() as db:
            topic = db.create_batch("主题")
            a = db.add_entry(batch_id=topic, term="A")
            with self.assertRaises(ValueError):
                db.block_map_edge(topic, a, a)
            self.assertEqual(db.list_map_edge_blocks(topic), [])

    def test_blocks_are_scoped_to_a_topic(self):
        with temp_db() as db:
            t1 = db.create_batch("主题一")
            t2 = db.create_batch("主题二")
            a = db.add_entry(batch_id=t1, term="A")
            b = db.add_entry(batch_id=t1, term="B")
            c = db.add_entry(batch_id=t2, term="C")
            db.block_map_edge(t1, a, b)
            db.block_map_edge(t2, a, c)
            self.assertEqual(len(db.list_map_edge_blocks(t1)), 1)
            self.assertEqual(len(db.list_map_edge_blocks(t2)), 1)
            self.assertEqual(db.map_edge_block_pairs(t1), {(a, b), (b, a)})
            self.assertEqual(db.map_edge_block_pairs(t2), {(a, c), (c, a)})

    def test_clear_removes_both_directions(self):
        with temp_db() as db:
            topic = db.create_batch("主题")
            a = db.add_entry(batch_id=topic, term="A")
            b = db.add_entry(batch_id=topic, term="B")
            db.block_map_edge(topic, a, b)
            self.assertEqual(db.clear_map_edge_blocks(topic, b, a), 1,
                             "反向恢复同样要命中那一行")
            self.assertEqual(db.list_map_edge_blocks(topic), [])

    def test_delete_by_id_reports_hit_and_miss(self):
        with temp_db() as db:
            topic = db.create_batch("主题")
            a = db.add_entry(batch_id=topic, term="A")
            b = db.add_entry(batch_id=topic, term="B")
            rid = db.block_map_edge(topic, a, b)
            self.assertTrue(db.delete_map_edge_block(rid))
            self.assertFalse(db.delete_map_edge_block(rid))

    def test_deleting_an_entry_takes_its_blocks_with_it(self):
        with temp_db() as db:
            topic = db.create_batch("主题")
            a = db.add_entry(batch_id=topic, term="A")
            b = db.add_entry(batch_id=topic, term="B")
            c = db.add_entry(batch_id=topic, term="C")
            db.block_map_edge(topic, a, b)
            db.block_map_edge(topic, b, c)
            db.delete_entry(a)
            self.assertEqual(db.map_edge_block_pairs(topic), {(b, c), (c, b)})

    def test_unknown_entry_ids_are_rejected_by_foreign_keys(self):
        with temp_db() as db:
            topic = db.create_batch("主题")
            a = db.add_entry(batch_id=topic, term="A")
            with self.assertRaises(sqlite3.IntegrityError):
                db.block_map_edge(topic, a, 999999)
            self.assertEqual(db.list_map_edge_blocks(topic), [])


if __name__ == "__main__":
    unittest.main()
