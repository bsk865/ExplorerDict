"""离线审核已保存的全文导图；不建窗口、不调用模型。默认只写审核报告。

示例：python tools/audit_article_maps.py artifacts/目录一 artifacts/目录二
确认后可加 --render 更新完整图 SVG。未获批、输入失败与几何失败均记录，最终返回非零。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import article_map as am, map_export
from app.map_service import MapGraph, MapNode
from app.ui import concept_map as cm


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def segment_gap(box,a,b):
    dx=max(box[0]-max(a[0],b[0]),min(a[0],b[0])-box[2],0)
    dy=max(box[1]-max(a[1],b[1]),min(a[1],b[1])-box[3],0)
    return math.hypot(dx,dy)


def placement(edge,box):
    for a,b in zip(edge.points,edge.points[1:]):
        if a==b:
            continue
        if a[1]==b[1] and box[3]<=a[1]-7.99 and min(a[0],b[0])<=box[0] and box[2]<=max(a[0],b[0]):
            return "horizontal_above"
        if a[0]==b[0] and min(a[1],b[1])<=box[1] and box[3]<=max(a[1],b[1]):
            if box[0]>=a[0]+7.99:
                return "vertical_right"
            if box[2]<=a[0]-7.99:
                return "vertical_left"
    return "unassociated"


def check_layout(layout):
    paths=[*layout.topic_links,*(e.points for e in layout.edges)]
    segments=[(a,b) for path in paths for a,b in zip(path,path[1:]) if a!=b]
    boxes=[];details=[];errors=[]
    nonorthogonal=sum(a[0]!=b[0] and a[1]!=b[1] for a,b in segments)
    if nonorthogonal:
        errors.append(f"存在 {nonorthogonal} 段非正交线")
    for i,edge in enumerate(layout.edges):
        row={"index":i,"label":edge.label,"source":edge.rel.src_entry_id,"target":edge.rel.dst_entry_id}
        details.append(row)
        if len(edge.points)<2 or edge.label_pos is None:
            row.update(status="missing_route_or_label")
            errors.append(f"关系 {i} 缺少走线或标签：{edge.label}")
            continue
        box=cm.article_label_box(edge.label_pos,edge.label,cm._article_metrics())
        gaps=[segment_gap(box,a,b) for a,b in segments]
        minimum=min(gaps,default=None)
        node_hits=[n.entry_id for n in layout.nodes if cm.rects_intersect(box,cm.node_box(n))]
        label_hits=[j for j,b in boxes if cm.rects_intersect(box,b)]
        side=placement(edge,box)
        row.update(status="ok",box=list(box),position=list(edge.label_pos),placement=side,
                   minimum_line_gap_px=minimum,line_collisions=sum(g==0 for g in gaps),
                   node_collisions=node_hits,label_collisions=label_hits)
        if minimum is not None and minimum<7.99 or node_hits or label_hits or side=="unassociated":
            row["status"]="failed"
            errors.append(f"关系 {i} 净空或归属检查失败：{edge.label}")
        boxes.append((i,box))
    return {"ok":not errors,"nodes":len(layout.nodes),"relations":len(layout.edges),
            "nonorthogonal_segments":nonorthogonal,"labels":details,"errors":errors}


def audit_case(folder,config,render=False):
    result={"directory":str(folder),"status":"not_approved","errors":[]}
    if not (folder/"approved.json").exists():
        result["errors"]=["尚无 approved.json；该案例未获批或仍在处理"]
        result["saved_attempts"]=sorted(p.name for p in folder.glob("attempt-*.json"))
        return result
    try:
        source=read_json(folder/"source.json")
        approved=read_json(folder/"approved.json")
        data=approved["article_map"]
        saved=[MapNode(i+1,w) for i,w in enumerate(source["selected_words"])]
        nodes,rels,ids=am.projection(data,saved)
        graph=MapGraph(1,"offline-layout-audit",nodes=nodes,relations=rels,article_map=data)
        layout=cm.layout_article_graph(graph,saved,width=1000,height=700,depth=8,relations=rels)
        title=source.get("title") or source.get("file") or next(n["label"] for n in data["nodes"] if n["id"]==data["root"])
        result.update(title=title,case_id=source.get("case_id"),model_config=config,
                      initial=check_layout(layout),drag_checks=[])
        pairs=[(ids[n["parent"]],ids[n["id"]]) for n in data["nodes"] if n["parent"] is not None]
        probes=[(layout.nodes[0].entry_id,40,30),(layout.nodes[-1].entry_id,-28,20)]
        if rels:
            probes.append((rels[0].src_entry_id,32,-24))
        for eid,dx,dy in probes:
            node=layout.find(eid)
            try:
                preview=cm.article_drag_layout(layout,eid,node.x+dx,node.y+dy,pairs)
                check=check_layout(preview)
                check.update(entry_id=eid,offset=[dx,dy])
            except Exception as exc:
                check={"ok":False,"entry_id":eid,"offset":[dx,dy],"errors":[str(exc)]}
            result["drag_checks"].append(check)
        if not result["initial"]["ok"]:
            result["errors"].extend(result["initial"]["errors"])
        for check in result["drag_checks"]:
            if not check["ok"]:
                result["errors"].extend(f"拖动节点 {check['entry_id']}：{e}" for e in check["errors"])
        result["status"]="failed" if result["errors"] else "passed"
        if render:
            model=str(config.get("model", ""))
            options=[f"{k}={config[k]}" for k in ("thinking","reasoning_effort") if config.get(k)]
            model_note=" · ".join([model,*options])
            meta={"article_map":data,"topic_name":title,"model_config":model_note,"template_name":"全文主题树"}
            path=folder/"full-map.svg"
            map_export.write_svg(path,map_export.svg_document(layout=layout,meta=meta))
            result["rendered_svg"]=str(path)
    except Exception as exc:
        result["status"]="input_error"
        result["errors"].append(f"{type(exc).__name__}: {exc}")
    return result


def audit_directory(root,render=False):
    report={"schema":"article_layout_audit_v1","created_utc":datetime.now(timezone.utc).isoformat(),
            "directory":str(root),"minimum_gap_px":8,"render_requested":render,
            "desktop_gui_tested":False,"model_called":False,"cases":[],"errors":[]}
    config={}
    try:
        path=root/"configuration.json"
        if path.exists():
            raw=read_json(path)
            config={k:raw[k] for k in ("model","thinking","reasoning_effort") if k in raw}
    except Exception as exc:
        report["errors"].append(f"读取配置失败：{exc}")
    folders=sorted({p.parent for pattern in ("source.json","approved.json") for p in root.rglob(pattern)})
    for folder in folders:
        report["cases"].append(audit_case(folder,config,render))
    if not folders:
        report["errors"].append("未找到 source.json 或 approved.json")
    report["ok"]=not report["errors"] and all(c["status"]=="passed" for c in report["cases"])
    report["counts"]={status:sum(c["status"]==status for c in report["cases"])
                      for status in ("passed","failed","not_approved","input_error")}
    root.mkdir(parents=True,exist_ok=True)
    with (root/"layout-audit-history.jsonl").open("a",encoding="utf-8") as history:
        history.write(json.dumps(report,ensure_ascii=False)+"\n")
    (root/"layout-audit.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directories",nargs="+",type=Path)
    parser.add_argument("--render",action="store_true",help="同时重绘已获批图的完整 SVG，保留模型与文章标题")
    args=parser.parse_args(argv)
    failed=False
    for root in args.directories:
        try:
            report=audit_directory(root,args.render)
            failed=failed or not report["ok"]
            print(json.dumps({"directory":str(root),"ok":report["ok"],"counts":report["counts"]},ensure_ascii=False))
        except Exception as exc:
            failed=True
            print(json.dumps({"directory":str(root),"error":str(exc)},ensure_ascii=False))
    return int(failed)


if __name__=="__main__":
    raise SystemExit(main())
