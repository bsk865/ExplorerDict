"""Inline structure forms for article maps; never opens a second window."""
from collections import Counter
from PySide6.QtCore import Qt, QTimer, QPoint
from PySide6.QtWidgets import QWidget, QVBoxLayout, QFormLayout, QLabel, QLineEdit, QPlainTextEdit, QCompleter, QSizePolicy
from .widgets import PageComboBox, FlowLayout, button
from .. import map_customization as custom


class NodePicker(PageComboBox):
    def __init__(self, nodes, excluded=(), selected=None):
        super().__init__()
        self.setEditable(True)
        self.setInsertPolicy(PageComboBox.InsertPolicy.NoInsert)
        self.setMinimumContentsLength(10)
        self.setSizeAdjustPolicy(PageComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        rows = {node['id']: node for node in nodes}
        counts = Counter()
        for node in nodes:
            if node['id'] in excluded:
                continue
            parent = rows.get(node.get('parent'))
            caption = node['label'] + (' · '+parent['label'] if parent else '')
            counts[caption] += 1
            self.addItem(caption + (f'（{counts[caption]}）' if counts[caption] > 1 else ''), node['id'])
            self.setItemData(self.count()-1, caption, Qt.ItemDataRole.ToolTipRole)
        completer = self.completer()
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.lineEdit().setPlaceholderText('输入文字查找节点')
        self.currentIndexChanged.connect(lambda _: self.lineEdit().setCursorPosition(0))
        self.setCurrentIndex(max(0, self.findData(selected)))
        self.lineEdit().setCursorPosition(0)

    def chosen(self):
        index = self.findText(self.currentText(), Qt.MatchFlag.MatchExactly)
        if index < 0:
            raise ValueError('请从列表中选择一个节点。')
        return self.itemData(index)


class MapStructureEditor(QWidget):
    def __init__(self, page):
        super().__init__()
        self.setObjectName('page')
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.page = page
        self.action = None
        self.record = None
        self.form = None
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.footer = QWidget()
        self.footer.setObjectName('page')
        controls = FlowLayout(self.footer, spacing=6)
        self.save_button = button('保存', self.save, primary=True)
        controls.addWidget(self.save_button)
        controls.addWidget(button('取消', self.hide))
        self.footer.hide()
        self.hide()

    def hide(self):
        self.footer.hide()
        super().hide()

    def open(self, action):
        graph = custom.display_graph(self.page.controller.article)
        if not graph or self.page.controller.job:
            return
        self.page.editor.hide()
        self.action = action
        self.record = dict(self.page.selected or {})
        if self.form:
            self.form.hide()
            self.layout.removeWidget(self.form)
            self.form.deleteLater()
        self.form = QWidget()
        self.form.setObjectName('page')
        layout = QVBoxLayout(self.form)
        layout.setContentsMargins(0, 0, 0, 0)
        titles = {'add_node':'新增子节点', 'reparent':'调整上级节点', 'remove_node':'删除这个分支',
                  'add_relation':'新增关系', 'edit_relation':'编辑关系', 'remove_relation':'删除关系', 'reset':'恢复原图'}
        layout.addWidget(QLabel(titles[action]))
        fields = QFormLayout()
        fields.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        layout.addLayout(fields)
        nodes = graph['nodes']
        rows = {n['id']: n for n in nodes}
        selected = self.record.get('id')
        self.parent_picker = None
        self.src = self.dst = None
        self.label = self.note = None
        if action in {'add_node', 'reparent'}:
            excluded = custom.descendants(rows, {selected}) if action == 'reparent' else set()
            current = self.record.get('parent') if action == 'reparent' else selected or graph['root']
            self.parent_picker = NodePicker(nodes, excluded, current)
            self.parent_picker.setAccessibleName('上级节点')
            fields.addRow('上级节点', self.parent_picker)
        if action in {'add_node', 'add_relation', 'edit_relation'}:
            if action != 'add_node':
                current = self.record.get('src') if action == 'edit_relation' else selected or graph['root']
                if current not in rows:
                    current = self.record.get('src', graph['root'])
                target = self.record.get('dst') if action == 'edit_relation' else next((n['id'] for n in nodes if n['id'] != current), current)
                self.src, self.dst = NodePicker(nodes, selected=current), NodePicker(nodes, selected=target)
                self.src.setAccessibleName('关系起点')
                self.dst.setAccessibleName('关系终点')
                fields.addRow('起点', self.src)
                fields.addRow('终点', self.dst)
            self.label = QLineEdit()
            self.label.setMaxLength(96)
            self.label.setAccessibleName('节点文字' if action == 'add_node' else '关系文字')
            fields.addRow('文字', self.label)
            if action == 'edit_relation':
                self.label.setText(self.record['label'])
            if action != 'add_node':
                self.note = QPlainTextEdit()
                self.note.setPlaceholderText('可选：写下你的理解')
                self.note.setAccessibleName('我的说明')
                self.note.setFixedHeight(72)
                self.note.setPlainText(self.record.get('note', '') if action == 'edit_relation' else '')
                fields.addRow('我的说明', self.note)
            self.label.returnPressed.connect(self.save)
        description = ''
        if action == 'remove_node':
            count = len(custom.descendants(rows, {selected}))
            description = f'将从当前导图移除“{self.record.get("label", "")}”及其下级，共 {count} 个节点，同时移除与它们相连的关系。可撤销或恢复原图。'
        elif action == 'remove_relation':
            description = '将移除所选关系，保留两个节点。可撤销或恢复原图。'
        elif action == 'reset':
            description = '将恢复生成时的原图，清除本图的文字、节点、关系和布局调整。原文章不受影响，可撤销本次恢复。'
        elif action in {'add_node', 'add_relation', 'edit_relation'}:
            description = '内容按你的理解保存，原文仍可随时核对。'
        if description:
            text = QLabel(description)
            text.setWordWrap(True)
            layout.addWidget(text)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.hide()
        layout.addWidget(self.error)
        self.save_button.setText('确认删除' if action.startswith('remove_') else '确认恢复' if action == 'reset' else '保存')
        self.layout.addWidget(self.form)
        self.show()
        self.footer.show()
        if self.label:
            self.label.setFocus()
        QTimer.singleShot(0, self.scroll_to_form)

    def scroll_to_form(self):
        self.page.detail_scroll.verticalScrollBar().setValue(self.mapTo(self.page.detail_scroll.widget(), QPoint(0, 0)).y())

    def save(self):
        action = self.action
        data = {'id': self.record.get('id')}
        try:
            if self.parent_picker:
                data['parent'] = self.parent_picker.chosen()
            if self.label:
                data['label'] = self.label.text()
            if self.src:
                data.update(src=self.src.chosen(), dst=self.dst.chosen())
            if self.note:
                data['note'] = self.note.toPlainText()
                if len(data['note']) > 2000:
                    raise ValueError('说明请控制在2000字以内。')
        except ValueError as exc:
            self.error.setText(str(exc))
            self.error.show()
            self.page.detail_scroll.ensureWidgetVisible(self.error)
            return
        if not self.page.controller.customize(action=action, data=data):
            self.error.setText(self.page.controller.message)
            self.error.show()
            self.page.detail_scroll.ensureWidgetVisible(self.error)
            return
        self.hide()
        if action == 'add_node':
            added = custom.customization(self.page.controller.article).get('added_nodes', [])
            if added:
                self.page.reveal_node(added[-1]['id'])
        elif action == 'reparent':
            self.page.reveal_node(data['id'])
