"""Application chrome, also used by prompts and non-native file pickers.

Window flags are set during construction, before Qt can create a visible frame.
The header is installed before setVisible/exec, avoiding a system-frame flash.
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog as QtDialog, QMessageBox as QtMessageBox,
                              QInputDialog as QtInputDialog, QFileDialog as QtFileDialog,
                              QWidget, QLabel, QPushButton, QHBoxLayout, QVBoxLayout,
                              QBoxLayout, QGridLayout, QStyleFactory)


class Header(QWidget):
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self.window().windowHandle()
            if handle and handle.startSystemMove():
                return
            self.origin = event.globalPosition().toPoint() - self.window().pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton and hasattr(self, 'origin'):
            self.window().move(event.globalPosition().toPoint() - self.origin)
        super().mouseMoveEvent(event)


class Chrome:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setSizeGripEnabled(True)
        self._header = None
        # Fusion avoids platform-drawn controls inside the application surface.
        self._surface_style = QStyleFactory.create('Fusion')
        self.setStyle(self._surface_style)

    def prepare_surface(self):
        if self._header is not None:
            return
        self._header = Header(self)
        self._header.setObjectName('dialogHeader')
        row = QHBoxLayout(self._header)
        row.setContentsMargins(4, 0, 0, 10)
        title = QLabel(self.windowTitle())
        title.setObjectName('dialogTitle')
        title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.windowTitleChanged.connect(title.setText)
        row.addWidget(title, 1)
        close = QPushButton('×')
        close.setObjectName('dialogClose')
        close.setFixedSize(32, 32)
        close.setAccessibleName('关闭')
        close.setToolTip('关闭')
        close.clicked.connect(self.reject)
        row.addWidget(close)
        layout = self.layout()
        if isinstance(layout, QBoxLayout):
            layout.insertWidget(0, self._header)
        elif isinstance(layout, QGridLayout):
            entries = []
            while layout.count():
                position = layout.getItemPosition(0)
                entries.append((layout.takeAt(0), position))
            columns = max((c + cs for _, (_, c, _, cs) in entries), default=1)
            for item, (r, c, rs, cs) in entries:
                layout.addItem(item, r + 1, c, rs, cs)
            layout.addWidget(self._header, 0, 0, 1, columns)
            self._header.setMinimumWidth(340)
        elif layout is None:
            layout = QVBoxLayout(self)
            layout.addWidget(self._header)
        self.setMinimumWidth(max(340, self.minimumWidth()))
        screen = self.screen().availableGeometry()
        self.resize(min(self.width(), screen.width() - 32), min(self.height(), screen.height() - 48))

    def setVisible(self, visible):
        if visible:
            self.prepare_surface()
        super().setVisible(visible)

    def exec(self):
        self.prepare_surface()
        return super().exec()


class QDialog(Chrome, QtDialog):
    pass


class QMessageBox(Chrome, QtMessageBox):
    @classmethod
    def question(cls, parent, title, text, buttons=QtMessageBox.StandardButton.Yes | QtMessageBox.StandardButton.No,
                 defaultButton=QtMessageBox.StandardButton.No):
        dialog = cls(parent)
        dialog.setWindowTitle(title)
        dialog.setText(text)
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setStandardButtons(buttons)
        dialog.setDefaultButton(defaultButton)
        for value, label in ((cls.StandardButton.Yes, '确认'), (cls.StandardButton.No, '取消'),
                             (cls.StandardButton.Ok, '知道了'), (cls.StandardButton.Cancel, '取消')):
            if dialog.button(value):
                dialog.button(value).setText(label)
        return cls.StandardButton(dialog.exec())

    @classmethod
    def information(cls, parent, title, text, buttons=QtMessageBox.StandardButton.Ok,
                    defaultButton=QtMessageBox.StandardButton.Ok):
        return cls.question(parent, title, text, buttons, defaultButton)

    warning = information
    critical = information


class QInputDialog:
    @classmethod
    def getText(cls, parent, title, label, mode=None, text='', **kwargs):
        from PySide6.QtWidgets import QLineEdit
        dialog = QDialog(parent)
        dialog.setWindowTitle(title)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(22, 18, 22, 20)
        layout.setSpacing(14)
        caption = QLabel(label)
        caption.setWordWrap(True)
        layout.addWidget(caption)
        entry = QLineEdit(text)
        entry.setAccessibleName(label)
        if mode is not None:
            entry.setEchoMode(mode)
        layout.addWidget(entry)
        cls._actions(dialog, layout)
        entry.returnPressed.connect(dialog.accept)
        dialog.resize(480, 190)
        accepted = dialog.exec() == QtDialog.DialogCode.Accepted
        return entry.text(), accepted

    @staticmethod
    def _actions(dialog, layout):
        from .widgets import button
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(button('取消', dialog.reject))
        row.addWidget(button('确认', dialog.accept, primary=True))
        layout.addLayout(row)

    @classmethod
    def getItem(cls, parent, title, label, items, current=0, editable=True, **kwargs):
        from .widgets import PageComboBox
        dialog = QDialog(parent)
        dialog.setWindowTitle(title)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(22, 18, 22, 20)
        layout.setSpacing(14)
        layout.addWidget(QLabel(label))
        entry = PageComboBox()
        entry.addItems(items)
        entry.setEditable(editable)
        entry.setAccessibleName(label)
        if items:
            entry.setCurrentIndex(current)
        layout.addWidget(entry)
        cls._actions(dialog, layout)
        dialog.resize(480, 190)
        accepted = dialog.exec() == QtDialog.DialogCode.Accepted
        return entry.currentText(), accepted


class QFileDialog(QDialog):
    """A small application file browser, with no shell dialog or shell icon chrome."""
    FileMode = QtFileDialog.FileMode
    AcceptMode = QtFileDialog.AcceptMode
    DialogLabel = QtFileDialog.DialogLabel
    Option = QtFileDialog.Option

    def __init__(self, parent=None, caption='', directory='', filter=''):
        from pathlib import Path
        from PySide6.QtCore import QDir
        from PySide6.QtWidgets import QFileSystemModel, QTreeView, QLineEdit, QAbstractItemView
        from .widgets import PageComboBox, button
        super().__init__(parent)
        self._mode = self.FileMode.ExistingFile
        self._accept_mode = self.AcceptMode.AcceptOpen
        self._selection = []
        self.setWindowTitle(caption)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(22, 18, 22, 20)
        outer.setSpacing(14)
        nav = QHBoxLayout()
        nav.addWidget(button('上一级', self.up))
        self.address = QLineEdit()
        self.address.setAccessibleName('当前文件夹')
        self.address.returnPressed.connect(lambda: self.setDirectory(self.address.text()))
        nav.addWidget(self.address, 1)
        nav.addWidget(button('新建文件夹', self.new_folder))
        outer.addLayout(nav)
        class Files(QFileSystemModel):
            def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
                if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
                    return ('名称', '大小', '类型', '修改时间')[section] if section < 4 else ''
                return super().headerData(section, orientation, role)

            def data(self, index, role=Qt.ItemDataRole.DisplayRole):
                if role == Qt.ItemDataRole.DecorationRole:
                    return None
                if role == Qt.ItemDataRole.DisplayRole and index.column() == 2:
                    return '文件夹' if self.isDir(index) else Path(self.filePath(index)).suffix.lstrip('.').upper() or '文件'
                return super().data(index, role)
        self.files = Files(self)
        self.files.setReadOnly(True)
        self.files.setFilter(QDir.Filter.AllDirs | QDir.Filter.Files | QDir.Filter.NoDotAndDotDot | QDir.Filter.Drives)
        self.files.setNameFilterDisables(False)
        self.view = QTreeView()
        self.view.setModel(self.files)
        self.view.setRootIsDecorated(False)
        self.view.setItemsExpandable(False)
        self.view.setAlternatingRowColors(True)
        self.view.setSortingEnabled(True)
        self.view.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.view.setColumnWidth(0, 330)
        self.view.setColumnWidth(1, 90)
        self.view.setColumnWidth(2, 90)
        self.view.header().setStretchLastSection(True)
        self.view.doubleClicked.connect(self.open_index)
        self.view.selectionModel().selectionChanged.connect(self.selected)
        outer.addWidget(self.view, 1)
        self.error = QLabel()
        self.error.setWordWrap(True)
        outer.addWidget(self.error)
        row = QHBoxLayout()
        self.filename = QLineEdit()
        self.filename.setAccessibleName('文件名')
        self.filename.setPlaceholderText('选择文件或输入文件名')
        self.filename.textEdited.connect(lambda _: self._selection.clear())
        self.filename.returnPressed.connect(self.accept)
        row.addWidget(self.filename, 1)
        self.filters = PageComboBox()
        self.filters.setAccessibleName('文件类型')
        self.filters.currentTextChanged.connect(self.apply_filter)
        row.addWidget(self.filters)
        outer.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(button('此电脑', self.computer))
        row.addWidget(button('用户文件夹', lambda: self.setDirectory(str(Path.home()))))
        row.addStretch()
        row.addWidget(button('取消', self.reject))
        self.choose = button('选择', self.accept, primary=True)
        row.addWidget(self.choose)
        outer.addLayout(row)
        self.directory = Path.cwd()
        self.setDirectory(str(self.directory))
        if directory:
            path = Path(directory)
            self.setDirectory(str(path if path.is_dir() else path.parent))
            if not path.is_dir():
                self.selectFile(path.name)
        self.setNameFilter(filter or '所有文件 (*)')
        self.resize(840, 560)

    def testOption(self, option):
        return option == self.Option.DontUseNativeDialog

    def setDirectory(self, directory):
        from pathlib import Path
        path = Path(directory).expanduser().resolve()
        if not path.is_dir():
            self.error.setText('此文件夹不存在，请检查路径。')
            return
        self.directory = path
        self.files.setRootPath(str(path))
        self.view.setRootIndex(self.files.index(str(path)))
        self.address.setText(str(path))
        self._selection = []
        if self._mode == self.FileMode.Directory:
            self.filename.clear()
        self.error.clear()

    def computer(self):
        self.files.setRootPath('')
        self.view.setRootIndex(self.files.index(''))
        self.address.setText('此电脑')
        self._selection = []

    def up(self):
        self.setDirectory(str(self.directory.parent))

    def new_folder(self):
        name, ok = QInputDialog.getText(self, '新建文件夹', '文件夹名称')
        if not ok or not name.strip():
            return
        if any(char in name for char in '/\\:*?"<>|') or name.strip() in ('.', '..'):
            self.error.setText('请使用有效的文件夹名称。')
            return
        try:
            path = self.directory / name.strip()
            path.mkdir()
            self.setDirectory(str(path))
        except OSError:
            self.error.setText('未能创建文件夹，请检查名称和文件夹权限。')

    def setFileMode(self, mode):
        from PySide6.QtWidgets import QAbstractItemView
        self._mode = mode
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection if mode == self.FileMode.ExistingFiles else QAbstractItemView.SelectionMode.SingleSelection)
        self.filename.setPlaceholderText('选择文件夹，或直接使用当前文件夹' if mode == self.FileMode.Directory else '选择文件或输入文件名')
        self.apply_filter()

    def setAcceptMode(self, mode):
        self._accept_mode = mode
        self.choose.setText('保存' if mode == self.AcceptMode.AcceptSave else '选择')

    def acceptMode(self):
        return self._accept_mode

    def setNameFilter(self, value):
        self.filters.clear()
        self.filters.addItems(value.split(';;'))

    def selectedNameFilter(self):
        return self.filters.currentText()

    def apply_filter(self, *_):
        import re
        from PySide6.QtCore import QDir
        match = re.search(r'\(([^()]*)\)', self.selectedNameFilter())
        self.files.setNameFilters(match[1].split() if match else ['*'])
        flags = QDir.Filter.AllDirs | QDir.Filter.NoDotAndDotDot | QDir.Filter.Drives
        if self._mode != self.FileMode.Directory:
            flags |= QDir.Filter.Files
        self.files.setFilter(flags)

    def selectFile(self, filename):
        self.filename.setText(filename)
        self._selection = []

    def selected(self, *_):
        from pathlib import Path
        self._selection = [self.files.filePath(index) for index in self.view.selectionModel().selectedRows()]
        self.filename.setText('、'.join(Path(path).name for path in self._selection))

    def open_index(self, index):
        if self.files.isDir(index):
            self.filename.clear()
            self.setDirectory(self.files.filePath(index))
        else:
            self.accept()

    def selectedFiles(self):
        from pathlib import Path
        import re
        paths = list(self._selection)
        if not paths and self.filename.text().strip():
            path = Path(self.filename.text().strip())
            paths = [str((self.directory / path).resolve())]
        if not paths and self._mode == self.FileMode.Directory:
            paths = [str(self.directory)]
        if self._accept_mode == self.AcceptMode.AcceptSave and paths and not Path(paths[0]).suffix:
            match = re.search(r'\*\.(\w+)', self.selectedNameFilter())
            if match:
                paths[0] += '.' + match[1]
        return paths

    def accept(self):
        from pathlib import Path
        paths = self.selectedFiles()
        if not paths:
            self.error.setText('请先选择文件。')
            return
        if self._mode == self.FileMode.Directory and not all(Path(path).is_dir() for path in paths):
            self.error.setText('请选择文件夹。')
            return
        if self._mode in (self.FileMode.ExistingFile, self.FileMode.ExistingFiles) and not all(Path(path).is_file() for path in paths):
            if len(paths) == 1 and Path(paths[0]).is_dir():
                self.filename.clear()
                self.setDirectory(paths[0])
            else:
                self.error.setText('文件不存在，请重新选择。')
            return
        if self._accept_mode == self.AcceptMode.AcceptSave and (not Path(paths[0]).parent.is_dir() or Path(paths[0]).is_dir()):
            self.error.setText('请选择有效的文件名和保存文件夹。')
            return
        if self.acceptMode() == self.AcceptMode.AcceptSave and paths and Path(paths[0]).is_file():
            if QMessageBox.question(self, '替换文件', '此文件已存在，是否替换？') != QMessageBox.StandardButton.Yes:
                return
        super().accept()

    @classmethod
    def _pick(cls, parent, caption, directory, filter, mode, save=False):
        dialog = cls(parent, caption, directory, filter)
        dialog.setFileMode(mode)
        if save:
            dialog.setAcceptMode(cls.AcceptMode.AcceptSave)
        accepted = dialog.exec() == QtDialog.DialogCode.Accepted
        return (dialog.selectedFiles() if accepted else []), dialog.selectedNameFilter()

    @classmethod
    def getOpenFileNames(cls, parent=None, caption='', dir='', filter='', **kwargs):
        return cls._pick(parent, caption, dir, filter, cls.FileMode.ExistingFiles)

    @classmethod
    def getOpenFileName(cls, parent=None, caption='', dir='', filter='', **kwargs):
        paths, selected = cls._pick(parent, caption, dir, filter, cls.FileMode.ExistingFile)
        return (paths[0] if paths else ''), selected

    @classmethod
    def getSaveFileName(cls, parent=None, caption='', dir='', filter='', **kwargs):
        paths, selected = cls._pick(parent, caption, dir, filter, cls.FileMode.AnyFile, True)
        return (paths[0] if paths else ''), selected

    @classmethod
    def getExistingDirectory(cls, parent=None, caption='', dir='', **kwargs):
        paths, _ = cls._pick(parent, caption, dir, '', cls.FileMode.Directory)
        return paths[0] if paths else ''
