import copy
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from app import map_customization as custom
from app.map_input import markdown
from app.native.map_editor import NodePicker
from tests import test_map_customization as fixtures


class MapStructureTests(unittest.TestCase):
    setUpClass = fixtures.MapCustomizationTests.__dict__['setUpClass']
    setUp = fixtures.MapCustomizationTests.setUp
    tearDown = fixtures.MapCustomizationTests.tearDown
    page = fixtures.MapCustomizationTests.page

    def change(self, action, **data):
        self.article, before, after = custom.save(self.store, self.article, action=action, data=data)
        return before, after

    def nodes(self):
        return {n['id']: n for n in custom.display_graph(self.article)['nodes']}

    def test_add_move_remove_restore_and_reopen_preserve_source(self):
        original = copy.deepcopy(self.article)
        self.change('add_node', parent='b', label='我的阅读习惯', note='在第二遍补充问题')
        added = next(key for key in self.nodes() if key.startswith('user:'))
        self.assertTrue(self.nodes()[added]['manual_node'])
        self.assertNotIn('quote', self.nodes()[added])
        self.change('reparent', id='c', parent=added)
        moved = self.nodes()['c']
        self.assertEqual(added, moved['parent'])
        self.assertEqual('b', moved['original_parent'])
        self.assertEqual('核对原文', moved['quote'])
        self.article, _, _ = custom.save(self.store, self.article, node_id=added, label='个人理解', position=[20, 33])
        self.article = type(self.store)(self.store.directory).article(self.article['id'])
        self.assertEqual('个人理解', self.nodes()[added]['label'])
        self.assertEqual({added: [20, 33]}, custom.positions(self.article))
        self.change('remove_node', id='b')
        self.assertEqual({'root', 'a'}, set(self.nodes()))
        self.assertEqual([], custom.display_graph(self.article)['relations'])
        self.assertEqual({}, custom.positions(self.article))
        self.assertEqual(original['map'], self.article['map'])
        self.assertEqual(original['quality'], self.article['quality'])
        before, _ = self.change('reset')
        self.assertEqual(original['map'], custom.display_graph(self.article))
        self.article, _, _ = custom.save(self.store, self.article, replacement=before)
        self.assertEqual({'root', 'a'}, set(self.nodes()))

    def test_reparent_rejects_cycles_root_changes_and_excessive_depth(self):
        for action, data in [('reparent', {'id':'b','parent':'c'}), ('reparent', {'id':'a','parent':'a'}),
                             ('remove_node', {'id':'root'}), ('reparent', {'id':'root','parent':'a'})]:
            with self.assertRaises(ValueError):
                self.change(action, **data)
        previous = 'c'
        for index in range(7):
            self.change('add_node', parent=previous, label=f'阅读层级{index}')
            previous = custom.customization(self.article)['added_nodes'][-1]['id']
        with self.assertRaises(ValueError):
            self.change('add_node', parent=previous, label='超过显示深度')
        # Moving a deep branch up must permit a later child and survive normalization.
        self.change('reparent', id=previous, parent='root')
        self.change('add_node', parent=previous, label='新的补充')
        self.assertEqual(12, len(self.nodes()))
        latest = self.store.article(self.article['id'])
        self.assertEqual(12, len(custom.display_graph(latest)['nodes']))
        self.assertEqual('root', self.nodes()[previous]['parent'])

    def test_relations_keep_original_evidence_separate_from_manual_content(self):
        original = copy.deepcopy(self.article['map'])
        self.change('add_relation', src='b', dst='a', label='阅读前先回顾', note='我的安排')
        added = custom.customization(self.article)['added_relations'][-1]['id']
        self.change('edit_relation', id='r', src='root', dst='b', label='包含', note='我对结构的整理')
        relations = {r['id']: r for r in custom.display_graph(self.article)['relations']}
        self.assertTrue(relations[added]['manual_relation'])
        self.assertNotIn('quote', relations[added])
        self.assertEqual(original['relations'][0], relations['r']['original_relation'])
        self.assertEqual(original['relations'][0]['quote'], relations['r']['quote'])
        self.change('remove_node', id='a')
        relations = custom.display_graph(self.article)['relations']
        self.assertEqual(['r'], [r['id'] for r in relations])
        exported = markdown(self.article)
        self.assertIn('原关系：阅读全文 → 之后可以 → 核对原文', exported)
        self.assertIn('原关系依据：阅读全文后可以核对原文', exported)
        self.assertIn('我的说明：我对结构的整理', exported)
        self.change('remove_relation', id='r')
        self.assertEqual([], custom.display_graph(self.article)['relations'])
        self.assertEqual(original, self.article['map'])
        with self.assertRaises(ValueError):
            self.change('add_relation', src='root', dst='root', label='同一个节点')
        with self.assertRaises(ValueError):
            self.change('edit_relation', id='r', src='root', dst='b', label='已删除关系')

    def test_stale_structure_form_does_not_overwrite_a_newer_edit(self):
        stale = copy.deepcopy(self.article)
        self.change('add_node', parent='root', label='另一页已保存')
        with self.assertRaises(ValueError):
            custom.save(self.store, stale, action='remove_node', data={'id':'b'})
        self.assertEqual(5, len(custom.display_graph(self.store.article(self.article['id']))['nodes']))

    def test_inline_node_forms_depth_reveal_delete_and_undo(self):
        host, page = self.page()
        self.addCleanup(host.close)
        self.addCleanup(page.close)
        page.depth.setCurrentIndex(0)
        page.view.scene().nodes['b'].setSelected(True)
        page.add_node.click()
        page.structure.label.setText('阅读后的新问题')
        page.structure.save_button.click()
        added = page.selected['id']
        self.assertEqual('阅读后的新问题', page.selected['label'])
        self.assertIn(added, page.view.scene().nodes)
        self.assertFalse(page.locate.isEnabled())
        page.reparent.click()
        previous_form = page.structure.form
        page.structure.open('reparent')
        self.assertTrue(previous_form.isHidden())
        picker = page.structure.parent_picker
        self.assertEqual(-1, picker.findData(added))
        picker.setCurrentIndex(picker.findData('root'))
        page.structure.save_button.click()
        self.assertEqual('root', page.selected['parent'])
        page.remove_node.click()
        self.assertIsNone(QApplication.activeModalWidget())
        page.structure.hide()
        self.assertIn(added, {n['id'] for n in custom.display_graph(page.controller.article)['nodes']})
        page.remove_node.click()
        page.structure.save_button.click()
        self.assertNotIn(added, page.view.scene().nodes)
        page.undo.click()
        page.reveal_node(added)
        self.assertEqual(added, page.selected['id'])
        page.restore_graph.click()
        page.structure.save_button.click()
        self.assertEqual({}, custom.customization(page.controller.article))
        page.undo.click()
        self.assertIn(added, {n['id'] for n in custom.display_graph(page.controller.article)['nodes']})

    def test_inline_relation_editing_and_full_export(self):
        host, page = self.page()
        self.addCleanup(host.close)
        self.addCleanup(page.close)
        page.add_relation.click()
        page.structure.src.setCurrentIndex(page.structure.src.findData('a'))
        page.structure.dst.setCurrentIndex(page.structure.dst.findData('b'))
        page.structure.label.setText('帮助理解')
        page.structure.note.setPlainText('记录自己的想法')
        page.structure.save_button.click()
        added = custom.customization(page.controller.article)['added_relations'][-1]['id']
        item = next(page.relations.item(i) for i in range(page.relations.count())
                    if page.relations.item(i).data(Qt.ItemDataRole.UserRole)['id'] == added)
        page.show_relation(item)
        self.assertIn('我的补充', page.quote.toPlainText())
        self.assertFalse(page.locate.isEnabled())
        page.edit_relation.click()
        page.structure.label.setText('支持理解')
        page.structure.save_button.click()
        target = Path(self.temp.name)/'manual.svg'
        with patch('app.native.maps.QFileDialog.getSaveFileName', return_value=(str(target), 'SVG')):
            page.export()
        contents = ''.join(ET.parse(target).getroot().itertext())
        self.assertIn('支持理解', contents)
        self.assertIn('手动修改', contents)
        item = next(page.relations.item(i) for i in range(page.relations.count())
                    if page.relations.item(i).data(Qt.ItemDataRole.UserRole)['id'] == added)
        page.show_relation(item)
        page.remove_relation.click()
        page.structure.save_button.click()
        self.assertNotIn(added, {r['id'] for r in custom.display_graph(page.controller.article)['relations']})
        self.assertIsNone(QApplication.activeModalWidget())

    def test_node_picker_search_requires_a_real_choice(self):
        picker = NodePicker(list(self.nodes().values()), selected='b')
        self.assertEqual('b', picker.chosen())
        picker.setEditText('没有这个节点')
        with self.assertRaises(ValueError):
            picker.chosen()

    def test_long_node_choices_keep_narrow_form_and_fixed_actions_usable(self):
        for index in range(100):
            self.article['map']['nodes'].append({'id':f'long{index}', 'parent':'root', 'label':f'阅读要点{index}'+('理解与核对'*14)})
        self.store.update_article(self.article)
        host, page = self.page()
        self.addCleanup(host.close)
        self.addCleanup(page.close)
        page.resize(700, 420)
        page.add_relation.click()
        self.app.processEvents()
        self.assertEqual(104, page.structure.src.count())
        self.assertEqual(700, page.width())
        self.assertTrue(page.structure.save_button.isVisible())
        center = page.structure.save_button.mapTo(page, page.structure.save_button.rect().center())
        self.assertTrue(page.rect().contains(center))
        self.assertGreater(page.detail_scroll.verticalScrollBar().maximum(), 0)
        page.detail_scroll.verticalScrollBar().setValue(page.detail_scroll.verticalScrollBar().maximum())
        self.assertTrue(page.structure.save_button.isVisible())
        page.controller.job = 'running'
        page.render()
        self.assertFalse(page.structure.save_button.isEnabled())
        page.controller.job = None

    def test_corrupt_cycles_and_orphans_never_reach_the_canvas(self):
        graph = self.article['map']
        value = {'added_nodes':[{'id':'user:orphan','parent':'gone','label':'无上级'},
                                {'id':'user:cycle','parent':'user:cycle','label':'循环'}],
                 'parents':{'b':'c','root':'c'},'removed_nodes':['root','missing']}
        cleaned = custom.clean(graph, value)
        effective = custom.apply_display(graph, cleaned)
        self.assertEqual(graph, effective)
        self.assertEqual({}, custom.display_graph({}))


if __name__ == '__main__':
    unittest.main()
