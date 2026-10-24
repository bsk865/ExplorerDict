"""Shared native controls and the existing visual palette."""
import logging
from PySide6.QtCore import QObject, Signal, QRunnable, QThreadPool, Qt, QRect, QSize
from PySide6.QtGui import QPixmap, QPainter, QColor, QFont
from PySide6.QtWidgets import QPushButton, QLabel, QWidget, QVBoxLayout, QHBoxLayout, QSizePolicy, QLayout, QPlainTextEdit, QComboBox, QListView, QSlider, QAbstractScrollArea, QApplication, QSpinBox, QDateEdit, QTimeEdit
from ..paths import resource_root
from .. import reading_preferences


class PageComboBox(QComboBox):
    """A closed selector leaves wheel scrolling to its parent page."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setView(QListView())
        self.setMaxVisibleItems(9)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.view().setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.view().setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def wheelEvent(self, event):
        if self.view().isVisible():
            super().wheelEvent(event)
        else:
            scroll_page(self, event)


class PageSlider(QSlider):
    def wheelEvent(self, event):
        scroll_page(self, event)


class PageSpinBox(QSpinBox):
    def wheelEvent(self, event):
        scroll_page(self, event)


class PageDateEdit(QDateEdit):
    def wheelEvent(self, event):
        scroll_page(self, event)


class PageTimeEdit(QTimeEdit):
    def wheelEvent(self, event):
        scroll_page(self, event)


def scroll_page(control, event):
    parent = control.parentWidget()
    while parent is not None:
        if isinstance(parent, QAbstractScrollArea):
            bar = parent.verticalScrollBar()
            delta = event.angleDelta().y() or event.pixelDelta().y()
            if (delta < 0 and bar.value() < bar.maximum()) or (delta > 0 and bar.value() > bar.minimum()):
                QApplication.sendEvent(parent.viewport(), event)
                return
        parent = parent.parentWidget()
    event.ignore()


class QuestionInput(QPlainTextEdit):
    submitted = Signal()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and event.modifiers() & (
                Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier):
            event.accept()
            if self.isEnabled() and not event.isAutoRepeat():
                self.submitted.emit()
            return
        super().keyPressEvent(event)


class FlowLayout(QLayout):
    """Keep variable-width controls in rows that follow the available width."""
    def __init__(self, parent=None, spacing=8):
        super().__init__(parent)
        self.items = []
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(spacing)

    def addItem(self, item):
        self.items.append(item)

    def count(self):
        return len(self.items)

    def itemAt(self, index):
        return self.items[index] if 0 <= index < len(self.items) else None

    def takeAt(self, index):
        return self.items.pop(index) if 0 <= index < len(self.items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self.arrange(QRect(0, 0, width, 0), False)

    def minimumSize(self):
        size = QSize()
        for item in self.items:
            if item.isEmpty():
                continue
            size = size.expandedTo(item.minimumSize())
        return size

    def sizeHint(self):
        return self.minimumSize()

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self.arrange(rect, True)

    def arrange(self, rect, apply):
        x, y, row_height = rect.x(), rect.y(), 0
        for item in self.items:
            if item.isEmpty():
                continue
            size = item.sizeHint()
            size.setWidth(min(size.width(), rect.width()))
            if row_height and x + size.width() > rect.right() + 1:
                x = rect.x()
                y += row_height + self.spacing()
                row_height = 0
            if apply:
                item.setGeometry(QRect(x, y, size.width(), size.height()))
            x += size.width() + self.spacing()
            row_height = max(row_height, size.height())
        return y + row_height - rect.y()


class Result(QObject):
    done = Signal(object)
    error = Signal(str)
    finished = Signal()


class Work(QRunnable):
    def __init__(self, action):
        super().__init__()
        self.action = action
        self.signals = Result()

    def run(self):
        try:
            self.signals.done.emit(self.action())
        except Exception as exc:
            logging.getLogger(__name__).exception('Native workspace action failed')
            self.signals.error.emit(str(exc) if isinstance(exc, ValueError)
                                    else '操作暂未完成，请稍后重试。')
        finally:
            self.signals.finished.emit()


class Tasks(QObject):
    error = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(2)
        self.pending = set()

    def submit(self, action, done, error=None):
        work = Work(action)
        self.pending.add(work)
        work.signals.done.connect(done)
        work.signals.error.connect(error or self.error)
        work.signals.finished.connect(lambda: self.pending.discard(work))
        self.pool.start(work)


def button(text, action=None, *, primary=False, icon=None, tip=None):
    value = QPushButton(text)
    value.setCursor(Qt.CursorShape.PointingHandCursor)
    value.setMinimumHeight(34)
    if primary:
        value.setObjectName('primary')
    if icon:
        value.setIcon(icon)
    if tip:
        value.setToolTip(tip)
    if action:
        value.clicked.connect(action)
    return value


def heading(text, subtitle=''):
    box = QWidget()
    box.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
    layout = QVBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 12)
    title = QLabel(text)
    title.setObjectName('heading')
    title.setWordWrap(True)
    layout.addWidget(title)
    if subtitle:
        label = QLabel(subtitle)
        label.setObjectName('muted')
        label.setWordWrap(True)
        layout.addWidget(label)
    return box


class ReadingArt(QWidget):
    def __init__(self, palette='clay', parent=None):
        super().__init__(parent)
        self.setMinimumHeight(160)
        self.setMaximumHeight(230)
        self.set_palette(palette)

    def set_palette(self, palette):
        self.pixmap = QPixmap(str(resource_root() / 'web/assets/generated' / (palette + '-reading-scene.webp')))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        if not self.pixmap.isNull():
            scaled = self.pixmap.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                        Qt.TransformationMode.SmoothTransformation)
            painter.drawPixmap((self.width()-scaled.width())//2, (self.height()-scaled.height())//2, scaled)


def stylesheet(preferences):
    c = reading_preferences.colors(preferences['palette'], preferences['mode'])
    bg, ink, soft, accent = [c.get(k, d) for k, d in
                            [('bg', '#f7f2ea'), ('ink', '#40342d'), ('soft', '#efe5d9'), ('accent', '#9d5c43')]]
    border = c.get('line', '#d6c8b8')
    return f'''
        QWidget {{ color:{ink}; font-family:"Microsoft YaHei UI"; font-size:13px; }}
        QMainWindow, QDialog, QWidget#page {{ background:{bg}; }}
        QWidget#dialogHeader {{ background:{bg}; border-bottom:1px solid {border}; }}
        QLabel#dialogTitle {{ font-size:17px; font-weight:600; }}
        QPushButton#dialogClose {{ padding:0; border:0; font-size:23px; border-radius:5px; }}
        QWidget#infoCard {{ background:{soft}; border:1px solid {border}; border-radius:8px; }}
        QWidget#infoCard QLabel {{ border:0; background:transparent; }}
        QMenu {{ background:{bg}; color:{ink}; border:1px solid {border}; padding:5px; }}
        QMenu::item {{ padding:8px 22px; border-radius:4px; }}
        QMenu::item:selected {{ background:{soft}; }}
        QTreeView, QListView {{ background:{bg}; color:{ink}; border:1px solid {border}; selection-background-color:{soft}; selection-color:{ink}; }}
        QTreeView::item {{ min-height:32px; padding:2px 6px; }}
        QWidget#sidebar {{ background:{c.get('quiet', soft)}; border-right:1px solid {border}; }}
        QLabel#heading {{ font-size:23px; font-weight:600; }}
        QLabel#brand {{ font-size:21px; font-weight:600; }}
        QLabel#muted {{ color:{c.get('muted', '#81766b')}; }}
        QPushButton {{ padding:5px 12px; border:1px solid {border}; border-radius:6px; background:transparent; }}
        QPushButton:hover {{ background:{soft}; border-color:{accent}; }}
        QPushButton:disabled {{ color:#99958f; }}
        QPushButton#primary, QPushButton:checked {{ color:white; background:{accent}; border-color:{accent}; }}
        QPushButton#primary:disabled {{ color:#99958f; background:{soft}; border-color:{border}; }}
        QPushButton#nav {{ text-align:left; border:0; padding:5px 10px; }}
        QPushButton#nav:checked {{ background:{c.get('accent-soft', soft)}; color:{c.get('accent-text', ink)}; font-weight:600; }}
        QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox, QDateEdit, QTimeEdit {{
          padding:7px; background:{bg}; border:1px solid {border}; border-radius:5px; selection-background-color:{accent}; }}
        QComboBox {{ padding-right:30px; }}
        QComboBox:hover, QComboBox:focus {{ border-color:{accent}; }}
        QComboBox::drop-down {{ subcontrol-origin:padding; subcontrol-position:top right; width:28px; border:0; background:transparent; }}
        QComboBox::down-arrow {{ image:url("{(resource_root() / 'assets/native-chevron.svg').as_posix()}"); width:12px; height:8px; }}
        QComboBox QAbstractItemView {{ background:{bg}; color:{ink}; border:1px solid {border}; border-radius:6px; padding:5px; outline:0; selection-background-color:{soft}; selection-color:{ink}; }}
        QComboBox QAbstractItemView::item {{ min-height:34px; padding:3px 10px; border:0; border-radius:4px; }}
        QComboBox QAbstractItemView::item:selected {{ background:{soft}; color:{ink}; }}
        QSpinBox, QTimeEdit, QDateEdit {{ padding-right:28px; }}
        QSpinBox::up-button, QTimeEdit::up-button {{ subcontrol-origin:border; subcontrol-position:top right; width:24px; border:0; border-top-right-radius:5px; background:{soft}; }}
        QSpinBox::down-button, QTimeEdit::down-button {{ subcontrol-origin:border; subcontrol-position:bottom right; width:24px; border:0; border-bottom-right-radius:5px; background:{soft}; }}
        QSpinBox::up-arrow, QTimeEdit::up-arrow {{ image:url("{(resource_root() / 'assets/native-chevron-up.svg').as_posix()}"); width:10px; height:6px; }}
        QSpinBox::down-arrow, QTimeEdit::down-arrow, QDateEdit::down-arrow {{ image:url("{(resource_root() / 'assets/native-chevron.svg').as_posix()}"); width:10px; height:6px; }}
        QDateEdit::drop-down {{ subcontrol-origin:border; subcontrol-position:top right; width:26px; border:0; border-top-right-radius:5px; border-bottom-right-radius:5px; background:{soft}; }}
        QSpinBox::up-button:hover, QSpinBox::down-button:hover, QTimeEdit::up-button:hover, QTimeEdit::down-button:hover, QDateEdit::drop-down:hover {{ background:{border}; }}
        QCalendarWidget QWidget#qt_calendar_navigationbar, QCalendarWidget QToolButton {{ background:{soft}; color:{ink}; border:0; }}
        QCalendarWidget QToolButton {{ padding:6px; }}
        QCalendarWidget QAbstractItemView {{ background:{bg}; color:{ink}; selection-background-color:{accent}; selection-color:{bg}; outline:0; }}
        QScrollBar:vertical {{ background:{bg}; width:10px; margin:0; }}
        QScrollBar::handle:vertical {{ background:{border}; min-height:24px; border-radius:5px; }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background:transparent; }}
        QScrollBar:horizontal {{ background:{bg}; height:10px; margin:0; }}
        QScrollBar::handle:horizontal {{ background:{border}; min-width:24px; border-radius:5px; }}
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width:0; }}
        QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background:transparent; }}
        QAbstractScrollArea::corner {{ background:{bg}; border:0; }}
        QListWidget, QTreeWidget, QTableWidget {{ background:{bg}; border:0; alternate-background-color:{soft}; outline:0; }}
        QListWidget::item {{ padding:12px 10px; border-bottom:1px solid {border}; }}
        QListWidget::item:selected, QTreeWidget::item:selected {{ background:{soft}; color:{ink}; }}
        QHeaderView::section {{ background:{soft}; border:0; padding:9px; text-align:left; }}
        QProgressBar {{ border:0; background:{soft}; border-radius:4px; min-height:8px; max-height:8px; }}
        QProgressBar::chunk {{ background:{accent}; border-radius:4px; }}
        QSplitter::handle {{ background:{border}; width:1px; }}
        QTabWidget::pane {{ border:0; background:{bg}; }}
        QTabBar::tab {{ background:{soft}; padding:7px 10px; border:0; }}
        QTabBar::tab:selected {{ background:{bg}; border-bottom:2px solid {accent}; }}
        QScrollArea {{ border:0; background:transparent; }}
        QGraphicsView#imageView {{ background:{bg}; border:1px solid {border}; border-radius:6px; }}
        QToolTip {{ background:{bg}; color:{ink}; border:1px solid {border}; }}
    '''
