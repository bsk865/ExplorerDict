"""全文关系标签的整字框净空与绘制一致性。"""
import unittest
import xml.etree.ElementTree as ET
from app import article_map as am, map_export
from app.map_service import MapGraph
from app.ui import concept_map as cm
from tests import test_full_article_map as fixtures
from tests.test_article_map_navigation import article_window


class ArticleLabelTests(unittest.TestCase):
    def assert_clear(self,layout):
        m=cm._article_metrics()
        paths=[*layout.topic_links,*(e.points for e in layout.edges)]
        occupied=[]
        for edge in layout.edges:
            self.assertIsNotNone(edge.label_pos,edge.label)
            box=cm.article_label_box(edge.label_pos,edge.label,m)
            for path in paths:
                self.assertTrue(cm.route_is_clear(path,[cm._inflate_box(box,7.99)]),edge.label)
            self.assertFalse(any(cm.rects_intersect(box,cm.node_box(n,0)) for n in layout.nodes),edge.label)
            self.assertFalse(any(cm.rects_intersect(box,b) for b in occupied),edge.label)
            occupied.append(box)

    def test_vertical_word_uses_half_width_not_half_height(self):
        m=cm._article_metrics()
        path=((100,0),(100,120))
        label="能够提升"
        pos=cm._article_label_position(path,label,m,[],[],[path])
        box=cm.article_label_box(pos,label,m)
        self.assertGreaterEqual(box[0]-100,8)
        self.assertEqual(pos[1],60)

    def test_horizontal_labels_stay_above_line_in_either_direction(self):
        m=cm._article_metrics()
        for path in (((0,100),(300,100)),((300,100),(0,100))):
            pos=cm._article_label_position(path,"可以通过",m,[],[],[path])
            self.assertLessEqual(cm.article_label_box(pos,"可以通过",m)[3],92)

    def test_vertical_label_avoids_organization_line_on_right(self):
        m=cm._article_metrics()
        path=((100,0),(100,120))
        organization=((125,0),(125,120))
        pos=cm._article_label_position(path,"能够提升",m,[],[],[path,organization])
        self.assertLessEqual(cm.article_label_box(pos,"能够提升",m)[2],92)

    def test_long_label_uses_full_width(self):
        m=cm._article_metrics()
        path=((100,0),(100,150))
        label="只有完成充分准备才能开始下一阶段"
        pos=cm._article_label_position(path,label,m,[],[],[path])
        box=cm.article_label_box(pos,label,m)
        self.assertGreaterEqual(box[0]-100,8)
        self.assertGreater(box[2]-box[0],150)

    def test_all_fixture_labels_clear_every_semantic_and_tree_line(self):
        for fixture in fixtures.FIXTURES:
            saved=fixtures.saved_nodes(fixture)
            data=am.validate_draft(fixture["draft"],fixture["text"],fixture["selected_words"])
            nodes,rels,_=am.projection(data,saved)
            graph=MapGraph(1,"test",nodes=nodes,relations=rels,article_map=data)
            layout=cm.layout_article_graph(graph,saved,width=900,height=700,depth=8,relations=rels)
            self.assert_clear(layout)
            root=ET.fromstring(map_export.svg_document(layout=layout,meta={"article_map":data}))
            texts=root.findall(".//{http://www.w3.org/2000/svg}text")
            for edge in layout.edges:
                matches=[e for e in texts if "".join(e.itertext())==edge.label]
                self.assertTrue(any(abs(float(e.attrib["x"])-edge.label_pos[0])<0.02 and
                                    abs(float(e.attrib["y"])-edge.label_pos[1]-9.5*0.34)<0.02 for e in matches))

    def test_drag_moves_label_with_its_relation_and_preserves_clearance(self):
        with article_window() as (win,_):
            win._pick_article_relation(0)
            edge=win._layout.edges[0]
            eid=edge.rel.src_entry_id
            node=win._layout.find(eid)
            label_item=win._edge_label_items[id(edge.rel)]
            before=tuple(win.canvas.coords(label_item))
            win._follow_edges(eid,node.x+40,node.y+30)
            preview=win._article_drag_preview
            self.assert_clear(preview)
            moved=preview.edges[0]
            after=tuple(win.canvas.coords(label_item))
            self.assertNotEqual(before,after)
            self.assertAlmostEqual(after[0],moved.label_pos[0]*win._zoom)
            self.assertAlmostEqual(after[1],moved.label_pos[1]*win._zoom)


if __name__=="__main__":
    unittest.main()
