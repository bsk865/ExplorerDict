"""Native, source-linked article maps and full-scene exports."""
import json
from pathlib import Path
from PySide6.QtCore import Qt, QObject, Signal, QTimer, QRectF, QSize
from PySide6.QtGui import QColor, QPen, QBrush, QPainterPath, QPainter, QImage, QTextDocument, QPixmap
from PySide6.QtSvg import QSvgGenerator
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QGraphicsView, QGraphicsScene,
                              QGraphicsItem, QGraphicsRectItem, QComboBox, QListWidget, QListWidgetItem, QLabel,
                              QTextBrowser, QFileDialog, QStyle, QDialog, QTabWidget)
from .widgets import PageComboBox as QComboBox
from .widgets import button, heading
from ..reading_preferences import colors


class MapView(QGraphicsView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)

    def zoom(self, factor):
        if .02 <= self.transform().m11()*factor <= 8:
            self.scale(factor, factor)

    def focusInEvent(self, event):
        super().focusInEvent(event)
        nodes = list(getattr(self.scene(), 'nodes', {}).values())
        if nodes and event.reason() in (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason):
            node = nodes[0] if event.reason() == Qt.FocusReason.TabFocusReason else nodes[-1]
            node.setFocus(event.reason())
            self.ensureVisible(node)

    def focusNextPrevChild(self, forward):
        nodes = list(getattr(self.scene(), 'nodes', {}).values())
        current = self.scene().focusItem() if self.scene() else None
        if nodes:
            index = nodes.index(current) if current in nodes else (-1 if forward else len(nodes))
            target = index + (1 if forward else -1)
            if 0 <= target < len(nodes):
                node = nodes[target]
                node.setFocus(Qt.FocusReason.TabFocusReason if forward else Qt.FocusReason.BacktabFocusReason)
                self.ensureVisible(node)
                return True
        return super().focusNextPrevChild(forward)

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom(1.0015 ** max(-480, min(480, event.angleDelta().y())))
            event.accept()
        else:
            super().wheelEvent(event)


class MapNode(QGraphicsRectItem):
    def paint(self, painter, option, widget=None):
        pen = QPen(self.pen())
        if self.isSelected() or self.hasFocus():
            pen.setWidthF(2.5)
            pen.setColor(QColor(self.data(2)))
        painter.setPen(pen)
        painter.setBrush(self.brush())
        painter.drawRoundedRect(self.rect(), 8, 8)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.scene().clearSelection()
            self.setSelected(True)
            event.accept()
        else:
            super().keyPressEvent(event)


class MapScene(QGraphicsScene):
    def __init__(self, graph, palette, depth=9, parent=None, include_relations=False, objects=()):
        super().__init__(parent)
        self.graph = graph
        self.nodes = {}
        self.setBackgroundBrush(QColor(palette.get('bg', '#ffffff')))
        ink = QColor(palette.get('ink', '#302d29'))
        border = QColor(palette.get('line', '#d6c8b8'))
        accent = QColor(palette.get('accent', '#a65e42'))
        rows = {n['id']: n for n in graph.get('nodes', [])}
        saved = {key for binding in graph.get('bindings', []) for key in binding.get('node_ids', [])}
        for node in rows.values():
            if any(obj.get('saved') and obj.get('type') == 'term' and obj.get('title')
                   and obj['title'] in node.get('quote', '') for obj in objects):
                saved.add(node['id'])
        children = {}
        for node in rows.values():
            children.setdefault(node.get('parent'), []).append(node['id'])
        sizes, spans, levels = {}, {}, {}
        visited = set()

        def measure(key, level):
            if key in visited or key not in rows or level > depth:
                return 0
            visited.add(key)
            levels[key] = level
            doc = QTextDocument()
            doc.setPlainText(rows[key]['label'])
            doc.setTextWidth(184)
            height = max(54, doc.size().height()+20)
            sizes[key] = height
            sub = sum(measure(k, level+1) for k in children.get(key, []))
            spans[key] = max(height+28, sub)
            return spans[key]
        root = graph.get('root')
        measure(root, 0)

        branch_colors = ['#aa7658', '#6b8c77', '#8583a6', '#b39a58', '#6b9aab']

        def draw(key, top, branch=None):
            if key not in spans:
                return
            x, y = levels[key]*270, top+(spans[key]-sizes[key])/2
            rect = MapNode(QRectF(x, y, 208, sizes[key]))
            rect.setPen(QPen(accent if key == root else border, 1.3))
            rect.setBrush(QBrush(QColor(palette.get('soft', '#f2e8dc'))))
            rect.setData(2, accent.name())
            self.addItem(rect)
            rect.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
            rect.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsFocusable)
            rect.setData(0, rows[key])
            text = self.addText(rows[key]['label'])
            text.setTextWidth(184)
            text.setDefaultTextColor(ink)
            text.setParentItem(rect)
            text.setPos(x+12, y+8)
            text.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            rect.setToolTip(rows[key].get('quote', ''))
            stripe = self.addRect(QRectF(x+2, y+12, 3, sizes[key]-24), QPen(Qt.PenStyle.NoPen),
                                  QBrush(QColor(branch) if branch else accent))
            stripe.setParentItem(rect)
            stripe.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            if key in saved:
                rect.setPen(QPen(accent, 2.2))
                rect.setToolTip('已关联探索对象\n' + rows[key].get('quote', ''))
            rect.setData(1, key in saved)
            self.nodes[key] = rect
            cursor = top
            for index, child in enumerate(children.get(key, [])):
                if child not in spans:
                    continue
                child_color = branch_colors[index % len(branch_colors)] if key == root else branch
                draw(child, cursor, child_color)
                target = self.nodes[child].rect()
                path = QPainterPath()
                path.moveTo(x+208, y+sizes[key]/2)
                path.cubicTo(x+240, y+sizes[key]/2, x+240, target.center().y(), target.left(), target.center().y())
                edge = self.addPath(path, QPen(QColor(child_color) if child_color else border, 1.2))
                edge.setZValue(-1)
                cursor += spans[child]
        if root in spans:
            draw(root, 0)
        if include_relations and graph.get('relations'):
            top = self.itemsBoundingRect().bottom()+50
            title = self.addText('原文关系')
            title.setDefaultTextColor(ink)
            title.setPos(0, top)
            top += 40
            for relation in graph['relations']:
                label = rows.get(relation['src'], {}).get('label', '') + ' → ' + relation['label'] + ' → ' + rows.get(relation['dst'], {}).get('label', '')
                text = self.addText(label)
                text.setDefaultTextColor(ink)
                text.setTextWidth(max(640, self.itemsBoundingRect().width()))
                text.setPos(0, top)
                top += text.boundingRect().height()+16
        self.setSceneRect(self.itemsBoundingRect().adjusted(-24, -24, 24, 24))

    def export(self, filename):
        bounds = self.sceneRect()
        width, height = max(1, int(bounds.width())), max(1, int(bounds.height()))
        if Path(filename).suffix.lower() == '.svg':
            device = QSvgGenerator()
            device.setFileName(str(filename))
            device.setSize(QSize(width, height))
            device.setViewBox(QRectF(0, 0, width, height))
            device.setTitle('探索词典 · 思维导图')
        else:
            scale = min(2, (24_000_000/max(1, width*height))**.5)
            width, height = max(1, int(width*scale)), max(1, int(height*scale))
            device = QImage(width, height, QImage.Format.Format_ARGB32)
            device.fill(self.backgroundBrush().color())
        painter = QPainter(device)
        if not painter.isActive():
            raise ValueError('无法写入导出文件，请选择其他位置。')
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            self.render(painter, QRectF(0, 0, width, height), bounds)
        finally:
            painter.end()
        if isinstance(device, QImage) and not device.save(str(filename), 'PNG'):
            raise ValueError('导出未完成，请检查保存位置。')


class MapController(QObject):
    changed = Signal()

    def __init__(self, host, article):
        super().__init__(host)
        self.host, self.article = host, article
        self.job = None
        self.message = ''
        self.timer = QTimer(self)
        self.timer.setInterval(300)
        self.timer.timeout.connect(self.poll)

    def generate(self, force=False):
        if self.job:
            return
        try:
            result = self.host.service.workspace.start('map', {'article_id': self.article['id'], 'force': force})
        except ValueError as exc:
            self.message = str(exc)
        else:
            if result.get('cached'):
                self.article = self.host.service.store.article(self.article['id'])
                self.message = '导图已就绪'
            else:
                self.job = result.get('job')
                self.message = '小探正在整理导图…'
                self.timer.start()
        self.changed.emit()

    def poll(self):
        with self.host.service.workspace.lock:
            job = dict(self.host.service.workspace.jobs.get(self.job, {}))
        if job.get('status') == 'running':
            return
        self.timer.stop()
        self.job = None
        if job.get('status') == 'done':
            self.article = self.host.service.store.article(self.article['id'])
            self.message = '导图已保存'
        else:
            self.message = '已停止' if job.get('status') == 'cancelled' else job.get('message', '生成暂未完成，请重试。')
        self.changed.emit()

    def stop(self):
        with self.host.service.workspace.lock:
            job = self.host.service.workspace.jobs.get(self.job)
            if job:
                job['cancel'].set()
        self.message = '正在停止…'
        self.changed.emit()


class MapPage(QWidget):
    def __init__(self, host, bundle):
        super().__init__()
        self.host = host
        self.assets = bundle.get('assets', {})
        article = bundle['article']
        if article['id'] not in host.maps:
            host.maps[article['id']] = MapController(host, article)
        self.controller = host.maps[article['id']]
        if not self.controller.job:
            self.controller.article = article
        self.palette = colors(host.snapshot['preferences']['palette'], host.snapshot['preferences']['mode'])
        self.selected = None
        self.graph_key = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(heading(article['title']))
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        tools = QHBoxLayout()
        self.generate = button('生成导图', self.controller.generate, primary=True)
        self.rebuild = button('重新整理', lambda: self.controller.generate(True))
        self.stop = button('停止', self.controller.stop)
        tools.addWidget(self.generate)
        tools.addWidget(self.rebuild)
        tools.addWidget(self.stop)
        self.depth = QComboBox()
        for label, depth in [('主题', 1), ('要点', 2), ('细节', 3), ('完整导图', 9)]:
            self.depth.addItem(label, depth)
        self.depth.setCurrentIndex(3)
        tools.addWidget(self.depth)
        tools.addStretch()
        tools.addWidget(button('−', lambda: self.view.zoom(.8), tip='缩小'))
        tools.addWidget(button('+', lambda: self.view.zoom(1.25), tip='放大'))
        tools.addWidget(button('', self.fit, icon=self.style().standardIcon(QStyle.StandardPixmap.SP_TitleBarNormalButton), tip='适应窗口'))
        tools.addWidget(button('导出', self.export))
        tools.addWidget(button('草稿记录', self.export_drafts))
        layout.addLayout(tools)
        split = QSplitter()
        self.view = MapView()
        split.addWidget(self.view)
        detail = QWidget()
        aside = QVBoxLayout(detail)
        self.quote = QTextBrowser()
        self.quote.setOpenLinks(False)
        aside.addWidget(self.quote, 1)
        self.relations = QListWidget()
        self.relations.setWordWrap(True)
        self.relations.itemClicked.connect(self.show_relation)
        aside.addWidget(self.relations, 1)
        self.locate = button('查看原文依据', self.open_source)
        aside.addWidget(self.locate)
        self.images = button('查看原图依据', self.open_images)
        self.images.setVisible(False)
        aside.addWidget(self.images)
        split.addWidget(detail)
        split.setSizes([750, 290])
        split.setChildrenCollapsible(False)
        layout.addWidget(split, 1)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.depth.currentIndexChanged.connect(self.draw)
        self.controller.changed.connect(self.render)
        self.render()
        QTimer.singleShot(0, self.fit)

    def render(self):
        self.generate.setEnabled(not self.controller.job)
        self.rebuild.setEnabled(not self.controller.job)
        self.stop.setEnabled(bool(self.controller.job))
        graph = self.controller.article.get('map')
        details = []
        if (self.controller.article.get('quality') or {}).get('demo'):
            details.append('示例导图')
        if graph and graph.get('segments'):
            details.append('全文分 ' + str(graph['segments']) + ' 段研读')
        if graph and graph.get('visual_sources'):
            details.append('含原图依据')
        self.summary.setText(' · '.join(details))
        self.summary.setVisible(bool(details))
        self.status.setText(self.controller.message or ('' if graph else '尚未生成导图'))
        key = json.dumps(graph, ensure_ascii=False)
        if key != self.graph_key:
            self.graph_key = key
            self.draw()

    def draw(self, *_):
        graph = self.controller.article.get('map') or {}
        old = self.view.scene()
        scene = MapScene(graph, self.palette, self.depth.currentData(), self,
                         objects=self.controller.article.get('objects', []))
        self.view.setScene(scene)
        scene.selectionChanged.connect(self.select_node)
        if old:
            old.deleteLater()
        self.quote.setPlainText('选择节点查看原文依据')
        self.selected = None
        self.locate.setEnabled(False)
        self.images.setVisible(False)
        self.show_relations()
        self.fit()

    def fit(self):
        if self.view.scene() and self.view.scene().items():
            self.view.fitInView(self.view.scene().sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def select_node(self):
        selected = self.view.scene().selectedItems()
        self.selected = selected[0].data(0) if selected else None
        if self.selected:
            self.quote.setPlainText(self.selected['label'] + '\n\n' + self.selected.get('quote', ''))
        self.locate.setEnabled(bool(self.selected))
        self.images.setVisible(bool(self.image_evidence()))
        self.show_relations(self.selected.get('id') if self.selected else None)

    def show_relations(self, node=None):
        self.relations.clear()
        graph = self.controller.article.get('map') or {}
        labels = {n['id']: n['label'] for n in graph.get('nodes', [])}
        for relation in graph.get('relations', []):
            if node and node not in (relation['src'], relation['dst']):
                continue
            label = labels.get(relation['src'], '') + ' → ' + relation['label'] + ' → ' + labels.get(relation['dst'], '')
            item = QListWidgetItem(label, self.relations)
            item.setData(Qt.ItemDataRole.UserRole, relation)

    def show_relation(self, item):
        self.selected = item.data(Qt.ItemDataRole.UserRole)
        self.quote.setPlainText(self.selected['label'] + '\n\n' + self.selected.get('quote', ''))
        self.locate.setEnabled(True)
        self.images.setVisible(bool(self.image_evidence()))

    def image_evidence(self):
        return [e for e in (self.selected or {}).get('evidence', []) if e.get('kind') == 'image' and e.get('asset')]

    def open_images(self):
        evidence = self.image_evidence()
        if not evidence:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle('原图依据')
        dialog.resize(860, 650)
        layout = QVBoxLayout(dialog)
        quote = QLabel(self.selected.get('quote', ''))
        quote.setWordWrap(True)
        layout.addWidget(quote)
        tabs = QTabWidget()
        layout.addWidget(tabs, 1)
        views = []
        for index, item in enumerate(evidence):
            view = MapView()
            scene = QGraphicsScene(view)
            pixmap = QPixmap()
            pixmap.loadFromData(self.assets.get(item['asset'], b''))
            if pixmap.isNull():
                scene.addText('暂时无法读取这张图片')
            else:
                scene.addPixmap(pixmap)
            view.setScene(scene)
            tabs.addTab(view, '第 ' + str(item['page']) + ' 页' if item.get('page') else '图片 ' + str(index+1))
            views.append(view)
        def fit(index):
            view = views[index]
            view.fitInView(view.scene().itemsBoundingRect(), Qt.AspectRatioMode.KeepAspectRatio)
        tabs.currentChanged.connect(fit)
        controls = QHBoxLayout()
        controls.addWidget(button('−', lambda: views[tabs.currentIndex()].zoom(.8), tip='缩小'))
        controls.addWidget(button('+', lambda: views[tabs.currentIndex()].zoom(1.25), tip='放大'))
        controls.addWidget(button('适应窗口', lambda: fit(tabs.currentIndex())))
        controls.addStretch()
        def locate():
            block = evidence[tabs.currentIndex()].get('block')
            if block:
                dialog.accept()
                self.host.open_article(self.controller.article['id'], block=block)
        controls.addWidget(button('在原文中定位', locate))
        controls.addWidget(button('关闭', dialog.close))
        layout.addLayout(controls)
        QTimer.singleShot(0, lambda: fit(0))
        dialog.exec()

    def open_source(self):
        if not self.selected:
            return
        article = self.controller.article
        evidence = self.selected.get('evidence', [])
        block = next((e.get('block') for e in evidence if e.get('block')), None)
        quote = self.selected.get('quote', '')
        if not block and quote:
            block = next((b['id'] for b in article['blocks'] if quote in (b.get('text') or b.get('caption', ''))), None)
        if block:
            self.host.open_article(article['id'], block=block)
        else:
            self.host.notice('该依据跨越多个段落，请在原文中核对。')

    def export(self):
        graph = self.controller.article.get('map')
        if not graph:
            self.host.notice('请先生成导图')
            return
        filename, selected = QFileDialog.getSaveFileName(self, '导出完整导图', '完整导图.png', 'PNG 图片 (*.png);;SVG 矢量图 (*.svg)')
        if filename:
            if not Path(filename).suffix:
                filename += '.svg' if selected.startswith('SVG') else '.png'
            scene = MapScene(graph, self.palette, include_relations=True,
                             objects=self.controller.article.get('objects', []))
            try:
                scene.export(filename)
                self.host.notice('完整导图已导出')
            except ValueError as exc:
                self.host.notice(str(exc))

    def export_drafts(self):
        filename, _ = QFileDialog.getSaveFileName(self, '导出草稿与复核记录', '导图草稿.json', 'JSON (*.json)')
        if filename:
            with self.host.service.store.connect() as db:
                rows = [json.loads(row[0]) for row in db.execute('SELECT payload FROM attempts WHERE article_id=?', (self.controller.article['id'],))]
            try:
                Path(filename).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
                self.host.notice('草稿记录已导出')
            except OSError:
                self.host.notice('无法保存，请选择其他位置。')
