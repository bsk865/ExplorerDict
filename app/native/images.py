"""Native image inspection and normalized region selection."""
from PySide6.QtCore import Qt, QRectF, QPointF, Signal
from PySide6.QtGui import QPixmap, QPen, QColor, QBrush
from PySide6.QtWidgets import (QVBoxLayout, QHBoxLayout, QGraphicsView, QGraphicsScene, QLabel, QStyle)
from .surfaces import QDialog
from .widgets import button


class ImageView(QGraphicsView):
    selected = Signal(object)

    def __init__(self, raw, region=None, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        pixmap = QPixmap()
        pixmap.loadFromData(raw)
        self.image = self.scene().addPixmap(pixmap)
        self.bounds = self.image.boundingRect()
        self.scene().setSceneRect(self.bounds)
        self.origin = None
        self.region = region
        self.fitted = False
        self.outline = self.scene().addRect(QRectF(), QPen(QColor('#c35f36'), 2), QBrush(QColor(230, 160, 60, 40)))
        pen = self.outline.pen()
        pen.setCosmetic(True)
        self.outline.setPen(pen)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        if region:
            x, y, w, h = region
            self.outline.setRect(x*self.bounds.width(), y*self.bounds.height(), w*self.bounds.width(), h*self.bounds.height())

    def fit(self):
        if not self.bounds.isEmpty():
            self.fitInView(self.bounds, Qt.AspectRatioMode.KeepAspectRatio)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not self.fitted:
            self.fit()
            self.fitted = True

    def zoom(self, factor):
        if 0.02 <= self.transform().m11()*factor <= 20:
            self.scale(factor, factor)

    def clamp(self, point):
        return QPointF(max(0, min(self.bounds.width(), point.x())), max(0, min(self.bounds.height(), point.y())))

    def mousePressEvent(self, event):
        point = self.mapToScene(event.position().toPoint())
        if event.button() == Qt.MouseButton.LeftButton and self.bounds.contains(point):
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
        controls = QHBoxLayout()
        controls.addWidget(button('−', lambda: self.view.zoom(0.8), tip='缩小'))
        controls.addWidget(button('+', lambda: self.view.zoom(1.25), tip='放大'))
        controls.addWidget(button('', self.view.fit, icon=self.style().standardIcon(QStyle.StandardPixmap.SP_TitleBarNormalButton), tip='适应窗口'))
        controls.addWidget(button('整张图', self.view.clear_region))
        controls.addStretch()
        self.use = button('理解选区' if self.view.region else '理解整张图', self.choose, primary=True)
        controls.addWidget(self.use)
        controls.addWidget(button('关闭', self.close))
        layout.addLayout(controls)
        self.error = QLabel()
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        self.view.selected.connect(lambda region: self.use.setText('理解选区' if region else '理解整张图'))

    def choose(self):
        region = self.view.region
        body = {'type': 'region' if region else 'image', 'title': self.obj['title'].removesuffix(' · 局部') + (' · 局部' if region else ''),
                'block': self.obj['block'], 'asset': self.obj['asset'], 'region': region}
        try:
            obj = self.host.service.store.object(self.article['id'], body)
        except ValueError as exc:
            self.error.setText(str(exc))
            return
        self.host.open_article(self.article['id'], remember=False, object_id=obj['id'])
        self.accept()
