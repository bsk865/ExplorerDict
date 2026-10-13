"""全文导图的真实布局与无窗口交互回归。"""
from contextlib import contextmanager
from types import SimpleNamespace
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

from app import article_map as am, map_export
from app.article_source import Article
from app.map_service import MapGraph
from app.ui import concept_map as cm
from tests import support
from tests import test_full_article_map as fixtures


@contextmanager
def article_window():
    with support.temp_db() as db, support._FakeTkEnv():
        fixture = fixtures.FIXTURES[0]
        topic,nodes,service,article,fp = fixtures.TestArticleMapIntegration().prepare(db,fixture)
        db.put_article("fixture",Article(ok=True,text=fixture["text"],title=fixture["file"],status="ok",method="test"))
        service._worker_article(1,topic,fp,nodes,(article,fixture["text"]))
        win = cm.ConceptMapWindow(None,SimpleNamespace(db=db,config=service.config,map_service=service))
        try:
            yield win,nodes
        finally:
            win.close()


class ArticleMapNavigationTests(unittest.TestCase):
    def test_all_fixtures_use_orthogonal_lines_and_readable_gaps(self):
        for fixture in fixtures.FIXTURES:
            data=am.validate_draft(fixture["draft"],fixture["text"],fixture["selected_words"])
            saved=fixtures.saved_nodes(fixture)
            nodes,rels,ids=am.projection(data,saved)
            graph=MapGraph(1,"test",nodes=nodes,relations=rels,article_map=data)
            layout=cm.layout_article_graph(graph,saved,width=900,height=700,depth=8,relations=rels)
            for path in [*layout.topic_links,*(e.points for e in layout.edges)]:
                self.assertGreaterEqual(len(path),2)
                for a,b in zip(path,path[1:]):
                    self.assertTrue(a[0]==b[0] or a[1]==b[1])
            for i,node in enumerate(layout.nodes):
                self.assertTrue(node.source_quote)
                for other in layout.nodes[i+1:]:
                    if node.level==other.level:
                        self.assertGreaterEqual(abs(node.y-other.y)-(node.h+other.h)/2,59)
            xml=ET.fromstring(map_export.svg_document(layout=layout,meta={"article_map":data}))
            titles={e.text for e in xml.findall(".//{http://www.w3.org/2000/svg}title")}
            self.assertTrue({n["quote"] for n in data["nodes"]}.issubset(titles))

    def test_word_selection_expands_and_centers_bound_action(self):
        with article_window() as (win,nodes):
            win._change_article_depth("主干")
            with mock.patch.object(win,"_selected_entry_ids",return_value=[nodes[0].entry_id]):
                win._on_article_word_select()
            target=win._layout.find(win._selected_node)
            self.assertIsNotNone(target)
            self.assertTrue(target.highlighted)
            w,h=win._canvas_size()
            self.assertAlmostEqual(target.x-win._view_left(),w/2,delta=2)
            self.assertAlmostEqual(target.y-win._view_top(),h/2,delta=2)
            self.assertIn(target.source_quote,win.article_evidence._buffer)

    def test_node_relation_navigation_stays_in_scope(self):
        with article_window() as (win,_):
            rel=win._graph.relations[0]
            win._select_article_node(rel.src_entry_id)
            choices=win._article_relation_choices()
            win._step_article_relation(1)
            self.assertIn(win._article_relation_index,choices)
            self.assertIn(rel.src_entry_id,(win._selected.src_entry_id,win._selected.dst_entry_id))
            self.assertEqual(len(win._layout.edges),1)
            self.assertIn(win._selected.evidence,win.article_evidence._buffer)
            win._article_all_relations()
            self.assertIsNone(win._article_scope_node)
            self.assertEqual(len(win._article_relation_choices()),len(win._graph.relations))

    def test_fit_then_zoom_does_not_jump_to_legacy_minimum(self):
        with article_window() as (win,_):
            win._change_article_depth("全部细节")
            win._fit_article_view()
            w,h=win._canvas_size()
            self.assertLessEqual(win._layout.content_w-win._layout.content_x0,w)
            self.assertLessEqual(win._layout.content_h-win._layout.content_y0,h)
            before=win._zoom
            win.zoom_by(1.02)
            self.assertAlmostEqual(win._zoom,before*1.02)
            win._article_actual_size()
            self.assertEqual(win._zoom,1)

    def test_zoom_reuses_article_routing(self):
        with article_window() as (win,_):
            win._draw(force=True)
            with mock.patch.object(cm,"layout_article_graph",side_effect=AssertionError("缩放不应重新走线")):
                win.zoom_by(1.05)

    def test_locating_long_relation_keeps_both_endpoints_visible(self):
        with article_window() as (win,_):
            win._change_article_depth("全部细节")
            index=max(range(len(win._graph.relations)),key=lambda i: abs(
                win._layout.find(win._graph.relations[i].src_entry_id).y-
                win._layout.find(win._graph.relations[i].dst_entry_id).y))
            win._pick_article_relation(index)
            w,h=win._canvas_size()
            for eid in (win._selected.src_entry_id,win._selected.dst_entry_id):
                node=win._layout.find(eid)
                self.assertGreaterEqual(node.x-node.w/2-win._view_left(),-2)
                self.assertLessEqual(node.x+node.w/2-win._view_left(),w+2)
                self.assertGreaterEqual(node.y-node.h/2-win._view_top(),-2)
                self.assertLessEqual(node.y+node.h/2-win._view_top(),h+2)
                win.zoom_by(1.05)

    def test_selecting_node_keeps_it_under_pointer_before_drag(self):
        with article_window() as (win,_):
            target=win._layout.nodes[-1]
            event=SimpleNamespace(x=target.x-win._view_left(),y=target.y-win._view_top(),state=0)
            win._on_canvas_click(event)
            current=win._layout.find(target.entry_id)
            self.assertAlmostEqual(current.x-win._view_left(),event.x,delta=2)
            self.assertAlmostEqual(current.y-win._view_top(),event.y,delta=2)
            win._on_canvas_press(event)
            self.assertEqual(win._drag_node[0],target.entry_id)
            self.assertIsNone(win._link_from)

    def test_drag_updates_organization_line_endpoints(self):
        with article_window() as (win,_):
            parent,child,item=win._article_tree_items[0]
            node=win._layout.find(child)
            win._follow_edges(child,node.x+45,node.y+30)
            coords=win.canvas.coords(item)
            self.assertEqual(tuple(coords[-2:]),(node.x+45-node.w/2,node.y+30))

    def test_relation_expands_endpoints_even_with_multiple_relations(self):
        fixture=fixtures.FIXTURES[0]
        saved=fixtures.saved_nodes(fixture)
        data=am.validate_draft(fixture["draft"],fixture["text"],fixture["selected_words"])
        nodes,rels,ids=am.projection(data,saved)
        graph=MapGraph(1,"test",nodes=nodes,relations=rels,article_map=data)
        layout=cm.layout_article_graph(graph,saved,width=900,height=700,depth=0,relations=rels[:2])
        visible={n.entry_id for n in layout.nodes}
        self.assertTrue({e for r in rels[:2] for e in (r.src_entry_id,r.dst_entry_id)}.issubset(visible))


if __name__=="__main__":
    unittest.main()
