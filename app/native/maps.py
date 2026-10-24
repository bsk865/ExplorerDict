"""Native, source-linked article maps and full-scene exports."""
import json
import time
from pathlib import Path
from PySide6.QtCore import Qt, QObject, Signal, QTimer, QRectF, QSize, QPointF
from PySide6.QtGui import QColor, QPen, QBrush, QPainterPath, QPainter, QImage, QTextDocument, QPixmap
from PySide6.QtSvg import QSvgGenerator
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QGraphicsView, QGraphicsScene, QGraphicsItem, QGraphicsRectItem, QComboBox, QListWidget, QListWidgetItem, QLabel, QTextBrowser, QStyle, QTabWidget, QProgressBar, QLineEdit, QScrollArea, QFrame)
from .surfaces import QFileDialog, QDialog
from .widgets import PageComboBox as QComboBox
from .widgets import button, heading, FlowLayout
from ..map_input import resolve_mode, markdown
from ..reading_preferences import colors
from .. import map_customization


class MapView(QGraphicsView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.auto_fit = True
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setToolTip('拖动画布查看，Ctrl + 滚轮缩放；自适应可显示完整导图')

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.auto_fit and self.scene() and self.scene().items():
            self.fit_content()

    def fit_content(self):
        self.fitInView(self.scene().sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
        nodes = getattr(self.scene(), 'nodes', {})
        root = nodes.get(getattr(self.scene(), 'root', None))
        if root is not None and self.transform().m11() < .7:
            self.resetTransform()
            self.scale(.7, .7)
            center = root.sceneBoundingRect().center()
            center.setX(center.x()+self.viewport().width()/(4*.7))
            self.centerOn(center)

    def zoom(self, factor):
        if .02 <= self.transform().m11()*factor <= 8:
            self.auto_fit = False
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
    def itemChange(self, change, value):
        result = super().itemChange(change, value)
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged and self.scene():
            self.scene().refresh_edges()
        return result

    def mousePressEvent(self, event):
        self.drag_start = QPointF(self.pos())
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if self.pos() != getattr(self, 'drag_start', self.pos()):
            self.scene().nodeMoved.emit(self.data(0)['id'], self.pos())

    def paint(self, painter, option, widget=None):
        pen = QPen(self.pen())
        if self.isSelected() or self.hasFocus():
            pen.setWidthF(2.5)
            pen.setColor(QColor(self.data(2)))
        painter.setPen(pen)
        painter.setBrush(self.brush())
        painter.drawRoundedRect(self.rect(), 8, 8)

    def keyPressEvent(self, event):
        arrows = {Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_Right: (1, 0),
                  Qt.Key.Key_Up: (0, -1), Qt.Key.Key_Down: (0, 1)}
        if event.key() in arrows and self.flags() & QGraphicsItem.GraphicsItemFlag.ItemIsMovable:
            x, y = arrows[event.key()]
            step = 20 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 5
            self.moveBy(x*step, y*step)
            self.scene().nodeMoved.emit(self.data(0)['id'], self.pos())
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.scene().clearSelection()
            self.setSelected(True)
            event.accept()
        else:
            super().keyPressEvent(event)


class MapScene(QGraphicsScene):
    nodeMoved = Signal(str, object)

    def __init__(self, graph, palette, depth=9, parent=None, include_relations=False, objects=(), focus_root=None, positions=None, movable=False):
        super().__init__(parent)
        self.graph = graph
        self.nodes = {}
        self.edges = []
        self.constructing = True
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
            doc.setPlainText(rows[key]['label']+('\n〔手动修改〕' if rows[key].get('manual_label') else ''))
            doc.setTextWidth(184)
            height = max(54, doc.size().height()+20)
            sizes[key] = height
            sub = sum(measure(k, level+1) for k in children.get(key, []))
            spans[key] = max(height+28, sub)
            return spans[key]
        root = focus_root if focus_root in rows else graph.get('root')
        self.root = root
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
            rect.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)
            rect.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, movable)
            rect.setData(0, rows[key])
            text = self.addText(rows[key]['label']+('\n〔手动修改〕' if rows[key].get('manual_label') else ''))
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
                self.edges.append((key, child, edge))
                cursor += spans[child]
        if root in spans:
            draw(root, 0)
        for key, offset in (positions or {}).items():
            if key in self.nodes and map_customization.valid_position(offset):
                self.nodes[key].setPos(QPointF(*offset))
        self.constructing = False
        self.refresh_edges()
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

    def refresh_edges(self):
        if self.constructing:
            return
        for src, dst, edge in self.edges:
            left = self.nodes[src].mapRectToScene(self.nodes[src].rect())
            right = self.nodes[dst].mapRectToScene(self.nodes[dst].rect())
            path = QPainterPath()
            path.moveTo(left.right(), left.center().y())
            middle = (left.right()+right.left())/2
            path.cubicTo(middle, left.center().y(), middle, right.center().y(), right.left(), right.center().y())
            edge.setPath(path)
        self.setSceneRect(self.itemsBoundingRect().adjusted(-24, -24, 24, 24))

    def set_movable(self, enabled):
        for node in self.nodes.values():
            node.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, enabled)

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
        self.started = None
        self.elapsed = 0
        self.undo_stack = []
        self.mode = (article.get('quality') or {}).get('input_mode') or resolve_mode(article)
        self.timer = QTimer(self)
        self.timer.setInterval(300)
        self.timer.timeout.connect(self.poll)

    def generate(self, force=False):
        if self.job:
            return
        try:
            result = self.host.service.workspace.start('map', {'article_id': self.article['id'], 'force': force, 'mode': self.mode})
        except ValueError as exc:
            self.message = str(exc)
        else:
            if result.get('cached'):
                self.article = self.host.service.store.article(self.article['id'])
                self.message = '导图已就绪'
            else:
                self.job = result.get('job')
                self.started = time.monotonic()
                self.elapsed = 0
                self.message = '小探正在整理导图…'
                self.timer.start()
        self.changed.emit()

    def poll(self):
        with self.host.service.workspace.lock:
            job = dict(self.host.service.workspace.jobs.get(self.job, {}))
        if job.get('status') == 'running':
            elapsed = int(time.monotonic() - self.started) if self.started is not None else 0
            message = '正在停止…' if job.get('cancel') and job['cancel'].is_set() else job.get('message') or self.message
            if message != self.message or elapsed != self.elapsed:
                self.message, self.elapsed = message, elapsed
                self.changed.emit()
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

    def customize(self, **changes):
        if self.job:
            return False
        try:
            article, before, after = map_customization.save(self.host.service.store, self.article, **changes)
        except ValueError as exc:
            self.message = str(exc)
            self.changed.emit()
            return False
        self.article = article
        if before != after:
            self.undo_stack.append((map_customization.fingerprint(article['map']), before, after))
            self.undo_stack = self.undo_stack[-20:]
        self.message = '已保存在本文导图中'
        self.changed.emit()
        return True

    def can_undo(self):
        return bool(self.undo_stack and self.undo_stack[-1][0] == map_customization.fingerprint(self.article.get('map'))
                    and self.undo_stack[-1][2] == map_customization.customization(self.article))

    def undo(self):
        if self.job or not self.can_undo():
            return
        _, before, _ = self.undo_stack[-1]
        try:
            article, _, _ = map_customization.save(self.host.service.store, self.article, replacement=before)
        except ValueError as exc:
            self.message = str(exc)
        else:
            self.article = article
            self.undo_stack.pop()
            self.message = '已撤销上一步调整'
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
        self.source_key = None
        self.position_key = None
        self.focus_path = []
        self.depth_user_selected = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(heading(article['title']))
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        tools = FlowLayout(spacing=8)
        self.mode = QComboBox()
        self.mode.setAccessibleName('导图生成依据')
        self.mode.addItem('正文导图', 'text')
        self.mode.addItem('图文导图', 'multimodal')
        self.mode.setCurrentIndex(max(0, self.mode.findData(self.controller.mode)))
        self.mode.currentIndexChanged.connect(lambda _: setattr(self.controller, 'mode', self.mode.currentData()))
        tools.addWidget(self.mode)
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
        self.branch = button('展开分支', self.open_branch)
        self.branch.setEnabled(False)
        self.back_branch = button('返回上层', self.close_branch)
        self.back_branch.setVisible(False)
        tools.addWidget(self.branch)
        tools.addWidget(self.back_branch)
        tools.addWidget(button('−', lambda: self.view.zoom(.8), tip='缩小'))
        tools.addWidget(button('+', lambda: self.view.zoom(1.25), tip='放大'))
        tools.addWidget(button('', self.fit, icon=self.style().standardIcon(QStyle.StandardPixmap.SP_TitleBarNormalButton), tip='适应窗口'))
        tools.addWidget(button('导出', self.export))
        layout.addLayout(tools)
        arrange = QHBoxLayout()
        self.arrange = button('调整布局')
        self.arrange.setCheckable(True)
        self.arrange.toggled.connect(self.toggle_arrange)
        self.arrange.setToolTip('开启后可拖动节点；方向键微调，Shift + 方向键加快移动')
        self.reset_layout = button('恢复布局', lambda: self.controller.customize(reset_layout=True))
        self.undo = button('撤销', self.controller.undo)
        arrange.addWidget(self.arrange)
        arrange.addWidget(self.reset_layout)
        arrange.addWidget(self.undo)
        arrange.addStretch()
        layout.addLayout(arrange)
        scope = QLabel('正文导图只研读正文；图文导图同时识别并核对图片，需要模型支持图片。扫描页需使用图文导图。')
        scope.setWordWrap(True)
        scope.setObjectName('muted')
        layout.addWidget(scope)
        split = QSplitter()
        self.view = MapView()
        split.addWidget(self.view)
        detail = QWidget()
        aside = QVBoxLayout(detail)
        self.quote = QTextBrowser()
        self.quote.setOpenLinks(False)
        self.quote.setMinimumHeight(110)
        self.edit_label = button('修改节点文字', self.begin_edit)
        aside.addWidget(self.edit_label)
        self.editor = QWidget()
        edit_layout = QVBoxLayout(self.editor)
        edit_layout.setContentsMargins(0, 0, 0, 0)
        self.label_input = QLineEdit()
        self.label_input.setMaxLength(96)
        self.label_input.setAccessibleName('节点文字')
        self.label_input.returnPressed.connect(self.save_label)
        edit_layout.addWidget(self.label_input)
        edit_controls = FlowLayout(spacing=6)
        edit_controls.addWidget(button('保存', self.save_label, primary=True))
        edit_controls.addWidget(button('取消', self.editor.hide))
        self.restore_label = button('恢复原文字', self.reset_label)
        edit_controls.addWidget(self.restore_label)
        edit_layout.addLayout(edit_controls)
        self.editor.hide()
        aside.addWidget(self.editor)
        aside.addWidget(self.quote, 1)
        self.relations = QListWidget()
        self.relations.setMinimumHeight(90)
        self.relations.setWordWrap(True)
        self.relations.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.relations.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.relations.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.relations.itemClicked.connect(self.show_relation)
        aside.addWidget(self.relations, 1)
        self.locate = button('查看原文依据', self.open_source)
        aside.addWidget(self.locate)
        self.images = button('查看原图依据', self.open_images)
        self.images.setVisible(False)
        aside.addWidget(self.images)
        self.detail_scroll = QScrollArea()
        self.detail_scroll.setWidgetResizable(True)
        self.detail_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.detail_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.detail_scroll.setMinimumWidth(210)
        self.detail_scroll.setWidget(detail)
        split.addWidget(self.detail_scroll)
        split.setSizes([750, 290])
        split.setChildrenCollapsible(False)
        layout.addWidget(split, 1)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.busy = QProgressBar()
        self.busy.setRange(0, 0)
        self.busy.setTextVisible(False)
        self.busy.setFixedHeight(4)
        layout.addWidget(self.busy)
        layout.addWidget(self.status)
        self.depth.currentIndexChanged.connect(self.change_depth)
        self.controller.changed.connect(self.render)
        self.render()
        QTimer.singleShot(0, self.fit)

    def render(self):
        self.generate.setEnabled(not self.controller.job)
        self.rebuild.setEnabled(not self.controller.job)
        self.stop.setEnabled(bool(self.controller.job))
        self.mode.setEnabled(not self.controller.job)
        article = self.controller.article
        graph = map_customization.display_graph(article) if article.get('map') else None
        editable = bool(graph and not self.controller.job and not article.get('superseded_by'))
        self.arrange.setEnabled(editable)
        self.reset_layout.setEnabled(editable and bool(map_customization.positions(article)))
        self.undo.setEnabled(editable and self.controller.can_undo())
        self.edit_label.setEnabled(editable and bool(self.selected and 'parent' in self.selected))
        self.editor.setEnabled(editable)
        if self.controller.job and self.arrange.isChecked():
            self.arrange.setChecked(False)
        details = []
        if graph:
            details.append('正文导图 · 未分析图片' if (self.controller.article.get('quality') or {}).get('input_mode') == 'text' else '图文导图' if graph.get('visual_sources') else '正文导图')
        if (self.controller.article.get('quality') or {}).get('demo'):
            details.append('示例导图')
        if graph and graph.get('segments'):
            details.append('全文分 ' + str(graph['segments']) + ' 段研读')
        if graph and graph.get('visual_sources'):
            details.append('含原图依据')
        if map_customization.customization(article):
            details.append('已手动整理')
        self.summary.setText(' · '.join(details))
        self.summary.setVisible(bool(details))
        status = self.controller.message or ('' if graph else '尚未生成导图')
        if graph and len(graph.get('nodes', [])) > 80:
            status = (status+' · ' if status else '')+'拖动画布或展开分支查看细节'
        if self.controller.job:
            minutes, seconds = divmod(self.controller.elapsed, 60)
            status += f' · 已用 {minutes} 分 {seconds:02d} 秒'
        elif self.arrange.isChecked():
            status = (status+' · ' if status else '')+'拖动节点调整位置，松开后自动保存'
        self.status.setText(status)
        self.busy.setVisible(bool(self.controller.job))
        key = json.dumps(graph, ensure_ascii=False)
        source_key = map_customization.fingerprint(article.get('map'))
        if key != self.graph_key:
            same_source = source_key == self.source_key
            self.source_key = source_key
            self.graph_key = key
            if graph and not same_source and not self.depth_user_selected:
                self.depth.blockSignals(True)
                self.depth.setCurrentIndex(0 if len(graph.get('nodes', [])) > 80 else 3)
                self.depth.blockSignals(False)
            ids = {node['id'] for node in (graph or {}).get('nodes', [])}
            self.focus_path = [key for key in self.focus_path if key in ids]
            self.draw(preserve=same_source)
        position_key = map_customization.positions(article)
        if position_key != self.position_key:
            self.position_key = position_key
            scene = self.view.scene()
            if scene:
                for key, item in scene.nodes.items():
                    item.setPos(QPointF(*position_key.get(key, [0, 0])))

    def toggle_arrange(self, enabled):
        self.arrange.setText('结束调整' if enabled else '调整布局')
        if self.view.scene():
            self.view.scene().set_movable(enabled)
        if enabled:
            self.view.auto_fit = False
        self.render()

    def move_node(self, key, position):
        if not self.controller.customize(node_id=key, position=[position.x(), position.y()]):
            self.view.scene().nodes[key].setPos(QPointF(*map_customization.positions(self.controller.article).get(key, [0, 0])))

    def begin_edit(self):
        if self.selected and 'parent' in self.selected and not self.controller.job:
            self.label_input.setText(self.selected['label'])
            self.restore_label.setEnabled(bool(self.selected.get('manual_label')))
            self.editor.show()
            self.label_input.setFocus()
            self.label_input.selectAll()
            QTimer.singleShot(0, lambda: self.detail_scroll.ensureWidgetVisible(self.editor))

    def save_label(self):
        if self.selected and 'parent' in self.selected:
            if self.controller.customize(node_id=self.selected['id'], label=self.label_input.text()):
                self.editor.hide()

    def reset_label(self):
        if self.selected and self.selected.get('original_label'):
            if self.controller.customize(node_id=self.selected['id'], label=self.selected['original_label']):
                self.editor.hide()

    def change_depth(self, *_):
        self.depth_user_selected = True
        self.draw()

    def open_branch(self):
        if not self.selected or not self.branch.isEnabled():
            return
        self.focus_path.append(self.selected['id'])
        self.draw()

    def close_branch(self):
        if self.focus_path:
            self.focus_path.pop()
            self.draw()

    def draw(self, *_, preserve=False):
        graph = map_customization.display_graph(self.controller.article)
        old = self.view.scene()
        transform = self.view.transform()
        center = self.view.mapToScene(self.view.viewport().rect().center())
        auto_fit = self.view.auto_fit
        selected = self.selected.get('id') if preserve and self.selected and 'parent' in self.selected else None
        scene = MapScene(graph, self.palette, self.depth.currentData(), self,
                         objects=self.controller.article.get('objects', []),
                         focus_root=self.focus_path[-1] if self.focus_path else None,
                         positions=map_customization.positions(self.controller.article), movable=self.arrange.isChecked())
        self.view.setScene(scene)
        scene.selectionChanged.connect(self.select_node)
        scene.nodeMoved.connect(self.move_node)
        if old:
            old.deleteLater()
        self.quote.setPlainText('选择节点查看原文依据')
        self.selected = None
        self.editor.hide()
        self.edit_label.setEnabled(False)
        self.branch.setEnabled(False)
        self.back_branch.setVisible(bool(self.focus_path))
        self.locate.setEnabled(False)
        self.images.setVisible(False)
        self.show_relations()
        if preserve:
            self.view.setTransform(transform)
            self.view.centerOn(center)
            self.view.auto_fit = auto_fit
        else:
            self.fit()
        if selected in scene.nodes:
            scene.nodes[selected].setSelected(True)

    def fit(self):
        if self.view.scene() and self.view.scene().items():
            self.view.auto_fit = True
            self.view.fit_content()

    def select_node(self):
        selected = self.view.scene().selectedItems()
        self.selected = selected[0].data(0) if selected else None
        self.editor.hide()
        self.edit_label.setEnabled(bool(self.selected) and not self.controller.job
                                   and not self.controller.article.get('superseded_by'))
        if self.selected:
            original = '\n手动修改 · 原文字：'+self.selected['original_label'] if self.selected.get('manual_label') else ''
            self.quote.setPlainText(self.selected['label'] + original + '\n\n' + self.selected.get('quote', ''))
        self.locate.setEnabled(bool(self.selected))
        graph = map_customization.display_graph(self.controller.article)
        current_root = self.focus_path[-1] if self.focus_path else graph.get('root')
        self.branch.setEnabled(bool(self.selected and self.selected['id'] != current_root and
            any(node.get('parent') == self.selected['id'] for node in graph.get('nodes', []))))
        self.images.setVisible(bool(self.image_evidence()))
        self.show_relations(self.selected.get('id') if self.selected else None)

    def show_relations(self, node=None):
        self.relations.clear()
        graph = map_customization.display_graph(self.controller.article)
        labels = {n['id']: n['label'] for n in graph.get('nodes', [])}
        branch_ids = None
        if self.focus_path and not node:
            branch_ids = {self.focus_path[-1]}
            while True:
                expanded = branch_ids | {n['id'] for n in graph.get('nodes', []) if n.get('parent') in branch_ids}
                if expanded == branch_ids:
                    break
                branch_ids = expanded
        for relation in graph.get('relations', []):
            if node and node not in (relation['src'], relation['dst']):
                continue
            if branch_ids is not None and not branch_ids.intersection((relation['src'], relation['dst'])):
                continue
            label = labels.get(relation['src'], '') + ' → ' + relation['label'] + ' → ' + labels.get(relation['dst'], '')
            item = QListWidgetItem(label, self.relations)
            item.setData(Qt.ItemDataRole.UserRole, relation)

    def show_relation(self, item):
        self.selected = item.data(Qt.ItemDataRole.UserRole)
        self.editor.hide()
        self.edit_label.setEnabled(False)
        self.branch.setEnabled(False)
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
        graph = map_customization.display_graph(self.controller.article)
        if not graph:
            self.host.notice('请先生成导图')
            return
        filename, selected = QFileDialog.getSaveFileName(self, '导出完整导图', '完整导图.png', 'PNG 图片 (*.png);;SVG 矢量图 (*.svg);;Markdown (*.md)')
        if filename:
            if not Path(filename).suffix:
                filename += '.svg' if selected.startswith('SVG') else '.md' if selected.startswith('Markdown') else '.png'
            if Path(filename).suffix.lower() == '.md':
                try:
                    Path(filename).write_text(markdown(self.controller.article), encoding='utf-8')
                    self.host.notice('导图结构与原文关系已导出')
                except OSError:
                    self.host.notice('无法保存，请选择其他位置。')
                return
            scene = MapScene(graph, self.palette, include_relations=True,
                             objects=self.controller.article.get('objects', []),
                             positions=map_customization.positions(self.controller.article))
            try:
                scene.export(filename)
                self.host.notice('完整导图已导出')
            except ValueError as exc:
                self.host.notice(str(exc))
