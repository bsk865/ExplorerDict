"""Native image inspection and normalized region selection."""
from math import isfinite
from PySide6.QtCore import Qt, QRectF, QPointF, Signal
from PySide6.QtGui import QPixmap, QPen, QColor, QBrush
from PySide6.QtWidgets import (QVBoxLayout, QGraphicsView, QGraphicsScene, QLabel)
from .surfaces import QDialog
from .widgets import button, FlowLayout


class ImageView(QGraphicsView):
    selected = Signal(object)

    def __init__(self, raw, region=None, parent=None):
        super().__init__(parent)
        self.setObjectName('imageView')
        self.setScene(QGraphicsScene(self))
        pixmap = QPixmap()
        pixmap.loadFromData(raw)
        self.image = self.scene().addPixmap(pixmap)
        self.bounds = self.image.boundingRect()
        self.scene().setSceneRect(self.bounds)
        self.origin = None
        self.region = None
        self.auto_fit = True
        self.fit_scale = 1.0
        self.panning = False
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.outline = self.scene().addRect(QRectF(), QPen(QColor('#c35f36'), 2), QBrush(QColor(230, 160, 60, 40)))
        pen = self.outline.pen()
        pen.setCosmetic(True)
        self.outline.setPen(pen)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        if (not self.bounds.isEmpty() and isinstance(region, (list, tuple)) and len(region) == 4
                and all(type(value) in (int, float) and isfinite(value) and 0 <= value <= 1 for value in region)
                and region[2] > 0 and region[3] > 0
                and region[0] + region[2] <= 1 and region[1] + region[3] <= 1):
            self.region = list(region)
            x, y, w, h = region
            self.outline.setRect(x*self.bounds.width(), y*self.bounds.height(), w*self.bounds.width(), h*self.bounds.height())

    def fit(self):
        self.auto_fit = True
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        if not self.bounds.isEmpty():
            self.fitInView(self.bounds, Qt.AspectRatioMode.KeepAspectRatio)
            self.fit_scale = self.transform().m11()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.auto_fit:
            self.fit()

    def zoom(self, factor):
        if self.bounds.isEmpty() or not isfinite(factor) or factor <= 0:
            return
        current = self.transform().m11()
        target = max(min(.02, self.fit_scale), min(max(20, self.fit_scale), current * factor))
        if target != current:
            self.auto_fit = False
            self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            self.scale(target / current, target / current)

    def set_panning(self, enabled):
        self.origin = None
        # Discard an unfinished outline, retaining the last committed selection.
        if self.region:
            x, y, w, h = self.region
            self.outline.setRect(x*self.bounds.width(), y*self.bounds.height(), w*self.bounds.width(), h*self.bounds.height())
        else:
            self.outline.setRect(QRectF())
        self.panning = enabled
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag if enabled else QGraphicsView.DragMode.NoDrag)
        self.setCursor(Qt.CursorShape.OpenHandCursor if enabled else Qt.CursorShape.CrossCursor)

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom(1.25 ** (event.angleDelta().y() / 120))
            event.accept()
        else:
            super().wheelEvent(event)

    def clamp(self, point):
        return QPointF(max(0, min(self.bounds.width(), point.x())), max(0, min(self.bounds.height(), point.y())))

    def mousePressEvent(self, event):
        point = self.mapToScene(event.position().toPoint())
        if not self.panning and event.button() == Qt.MouseButton.LeftButton and self.bounds.contains(point):
            self.origin = self.clamp(point)
            self.outline.setRect(QRectF(self.origin, self.origin))
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.origin is not None:
            self.outline.setRect(QRectF(self.origin, self.clamp(self.mapToScene(event.position().toPoint()))).normalized())
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.origin is not None and event.button() == Qt.MouseButton.LeftButton:
            rect = QRectF(self.origin, self.clamp(self.mapToScene(event.position().toPoint()))).normalized()
            self.origin = None
            if rect.width() >= 1 and rect.height() >= 1:
                self.region = [rect.x()/self.bounds.width(), rect.y()/self.bounds.height(),
                               rect.width()/self.bounds.width(), rect.height()/self.bounds.height()]
                self.outline.setRect(rect)
                self.selected.emit(self.region)
            else:
                self.clear_region()
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def clear_region(self):
        self.origin = None
        self.region = None
        self.outline.setRect(QRectF())
        self.selected.emit(None)


class ImageDialog(QDialog):
    def __init__(self, host, article, obj, raw):
        super().__init__(host)
        self.host, self.article, self.obj = host, article, obj
        self.setWindowTitle(obj['title'])
        self.resize(860, 650)
        self.setMinimumSize(500, 400)
        layout = QVBoxLayout(self)
        self.view = ImageView(raw, obj.get('region'))
        layout.addWidget(self.view, 1)
        controls = FlowLayout(spacing=8)
        self.pan = button('拖动浏览', self.toggle_pan)
        self.pan.setCheckable(True)
        view_controls = [button('−', lambda: self.view.zoom(0.8), tip='缩小'),
                         button('+', lambda: self.view.zoom(1.25), tip='放大'),
                         button('适应窗口', self.view.fit), self.pan,
                         button('整张图', self.view.clear_region)]
        for control in view_controls:
            controls.addWidget(control)
        self.use = button('理解选区' if self.view.region else '理解整张图', self.choose, primary=True)
        controls.addWidget(self.use)
        controls.addWidget(button('关闭', self.close))
        layout.addLayout(controls)
        self.error = QLabel()
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        self.error.hide()
        self.hint = QLabel('拖动框选图片区域；放大后可切换拖动浏览。Ctrl＋滚轮缩放。')
        self.hint.setObjectName('muted')
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        if self.view.bounds.isEmpty():
            self.error.setText('图片无法读取，请关闭后重新导入原图。')
            self.error.show()
            self.hint.hide()
            for control in [*view_controls, self.use]:
                control.setEnabled(False)
        self.view.selected.connect(lambda region: self.use.setText('理解选区' if region else '理解整张图'))

    def toggle_pan(self):
        self.view.set_panning(self.pan.isChecked())
        self.pan.setText('返回框选' if self.pan.isChecked() else '拖动浏览')

    def choose(self):
        if self.view.bounds.isEmpty():
            return
        region = self.view.region
        body = {'type': 'region' if region else 'image', 'title': self.obj['title'].removesuffix(' · 局部') + (' · 局部' if region else ''),
                'block': self.obj['block'], 'asset': self.obj['asset'], 'region': region}
        try:
            obj = self.host.service.store.object(self.article['id'], body)
        except ValueError as exc:
            self.error.setText(str(exc))
            self.error.show()
            return
        self.host.open_article(self.article['id'], remember=False, object_id=obj['id'])
        self.accept()
