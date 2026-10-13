"""用当前配置模型验证认可的全文材料，生产库只读，输出写入独立目录。"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
import sys
import time
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import article_map, map_export
from app.api_client import DeepSeekClient, ApiError
from app.config import Config
from app.db import Database
from app.map_service import MapGraph, MapNode
from app.ui.concept_map import layout_article_graph


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def render(folder, source, result, *, index=1, model=""):
    data = article_map.validate_draft(result["article_map"], source["text"], source["selected_words"])
    saved = [MapNode(i+1, word) for i, word in enumerate(source["selected_words"])]
    nodes, relations, _ = article_map.projection(data, saved)
    graph = MapGraph(index, "isolated-live-check", nodes=nodes, relations=relations, article_map=data)
    layout = layout_article_graph(graph, saved, width=1000, height=700, depth=8, relations=relations)
    title = source.get("title") or next(n["label"] for n in data["nodes"] if n["id"]==data["root"])
    svg = map_export.svg_document(layout=layout, meta=dict(article_map=data, topic_name=title, model_config=model))
    (folder/"full-map.svg").write_text(svg, encoding="utf-8")
    return len(nodes), len(relations)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true", help="调用用户当前配置的模型")
    mode.add_argument("--render-only", action="store_true", help="只重绘 --output 下已保存的模型结果，不调用模型")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--fixtures", type=Path, default=ROOT/"tests/fixtures/article-maps.json",
                        help="测试材料与划选词；核查标准不会发给模型")
    parser.add_argument("--cases", nargs="+", help="仅运行指定材料 id，每次使用新的输出目录")
    args = parser.parse_args()
    if args.render_only:
        if args.output is None:
            parser.error("重绘需用 --output 指定已有验证目录")
        settings = json.loads((args.output/"configuration.json").read_text(encoding="utf-8"))
        folders = sorted((p for p in args.output.iterdir() if p.is_dir() and (p/"approved.json").exists()),
                         key=lambda p: int(p.name) if p.name.isdigit() else float("inf"))
        if not folders:
            parser.error("结果目录中没有合格模型图可重绘")
        for index, folder in enumerate(folders, 1):
            result = json.loads((folder/"approved.json").read_text(encoding="utf-8"))
            source = json.loads((folder/"source.json").read_text(encoding="utf-8"))
            render(folder, source, result, index=index, model=settings["model"])
        return 0
    if args.output is None:
        args.output = ROOT/"artifacts"/datetime.now().strftime("article-map-%Y%m%d-%H%M%S")
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("实测输出目录已有结果，请使用新的目录以保留每次验证证据")
    fixture_bytes = args.fixtures.read_bytes()
    fixtures = json.loads(fixture_bytes.decode("utf-8-sig"))["fixtures"]
    if args.cases:
        wanted = set(args.cases)
        fixtures = [f for f in fixtures if f.get("id") in wanted]
        if {f.get("id") for f in fixtures} != wanted:
            parser.error("指定材料 id 不存在")
    for fixture in fixtures:
        if any(word not in fixture["text"] for word in fixture["selected_words"]):
            parser.error("模拟划选词必须逐字出现在测试材料中")
        if any(item["quote"] not in fixture["text"] for item in fixture.get("expected",[])):
            parser.error("冻结核查点的依据必须逐字出现在测试材料中")
    db = Database(ROOT/"data/explorer_dict.sqlite3", read_only=True)
    try:
        config = Config.__new__(Config)
        config.db = db
        key = config.api_key()
        settings = dict(base_url=config.base_url, model=config.model,
                        timeout=config.article_map_timeout, reasoning_effort=config.reasoning_effort,
                        thinking=config.article_map_thinking)
    finally:
        db.close()
    args.output.mkdir(parents=True, exist_ok=True)
    save(args.output/"configuration.json", settings)
    save(args.output/"test-manifest.json", dict(fixtures_sha256=hashlib.sha256(fixture_bytes).hexdigest(),
         cases=[dict(id=f.get("id"),title=f["file"],expected=f.get("expected",[])) for f in fixtures],
         generation_source_sha256={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
              for name in ("app/article_map.py","app/api_client.py","app/config.py")},
         checks_frozen_before_generation=True,expected_sent_to_model=False))
    source_snapshot = args.output/"generation-source"
    source_snapshot.mkdir()
    for name in ("app/article_map.py","app/api_client.py","app/config.py"):
        shutil.copy2(ROOT/name,source_snapshot/Path(name).name)
    print(json.dumps(settings, ensure_ascii=False), flush=True)
    client = DeepSeekClient(api_key=key, **settings)
    results = []
    for index, fixture in enumerate(fixtures, 1):
        folder = args.output/str(index)
        folder.mkdir(exist_ok=True)
        calls = []
        class RecordingClient:
            def full_article_map(self, **kwargs):
                sequence = len(calls)+1
                stage = kwargs["stage"]
                print(f"材料{index} 调用{sequence} {stage}", flush=True)
                start = time.monotonic()
                save(folder/f"request-{sequence}-{stage}.json", kwargs)
                try:
                    result = client.full_article_map(**kwargs)
                except ApiError as exc:
                    calls.append(dict(stage=stage,seconds=round(time.monotonic()-start,2),error=exc.display()))
                    save(folder/"calls.json",calls)
                    raise
                calls.append(dict(stage=stage, seconds=round(time.monotonic()-start, 2)))
                save(folder/f"call-{sequence}-{stage}.json", result)
                save(folder/"calls.json", calls)
                return result
        def attempt(round_no, draft, issues, review):
            save(folder/f"attempt-{round_no}.json", dict(draft=draft, issues=issues, review=review))
            print(f"材料{index} 第{round_no}轮 问题{len(issues)}项", flush=True)
        source = dict(text=fixture["text"], selected_words=fixture["selected_words"], title=fixture["file"],
                      case_id=fixture.get("id"),text_sha256=hashlib.sha256(fixture["text"].encode("utf-8")).hexdigest(),
                      simulated_selections=[dict(word=word,start=fixture["text"].index(word),
                        end=fixture["text"].index(word)+len(word),
                        context=fixture["text"][max(0,fixture["text"].index(word)-25):fixture["text"].index(word)+len(word)+25])
                        for word in fixture["selected_words"]])
        save(folder/"source.json", source)
        try:
            data, quality = article_map.run(RecordingClient(), text=fixture["text"],
                                           selected_words=fixture["selected_words"], on_attempt=attempt)
            result = dict(article_map=data, quality=quality)
            save(folder/"approved.json", result)
            nodes, relations = render(folder, source, result, index=index, model=settings["model"])
            results.append(dict(case_id=fixture.get("id"),file=fixture["file"], approved=True, rounds=quality["rounds"], nodes=nodes, relations=relations))
        except ApiError as exc:
            results.append(dict(case_id=fixture.get("id"),file=fixture["file"], approved=False, error=exc.display()))
        save(args.output/"results.json", results)
        print(json.dumps(results[-1], ensure_ascii=False), flush=True)
    return 0 if all(r["approved"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
