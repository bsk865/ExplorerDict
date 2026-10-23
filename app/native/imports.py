"""Sequential, cancellable imports with a result for each selected file."""
from pathlib import Path
from threading import Event
from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QProgressBar, QListWidget
from .widgets import button


def import_files(service, paths, progress, cancel):
    results = []
    for path in dict.fromkeys(paths):
        if cancel.is_set():
            break
        try:
            article = service.import_file(path)
            result = {'name': Path(path).name, 'article_id': article['id']}
        except ValueError as exc:
            result = {'name': Path(path).name, 'error': str(exc)}
        except OSError:
            result = {'name': Path(path).name, 'error': '无法读取文件，请确认文件存在且有访问权限。'}
        except Exception:
            result = {'name': Path(path).name, 'error': '未能读取此文件，请检查格式或文件内容。'}
        results.append(result)
        progress(result)
    return results


class BatchImportDialog(QDialog):
    progressed = Signal(object)

    def __init__(self, host, paths):
        super().__init__(host)
        self.setWindowTitle('导入资料')
        self.resize(540, 360)
        self.cancel = Event()
        self.running = True
        self.results = []
        self.total = len(set(paths))
        layout = QVBoxLayout(self)
        self.status = QLabel('正在导入资料…')
        self.status.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setRange(0, self.total)
        self.progress.setTextVisible(False)
        self.rows = QListWidget()
        self.rows.setWordWrap(True)
        self.rows.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.stop = button('停止后续导入', self.reject)
        for widget in (self.status, self.progress, self.rows, self.stop):
            layout.addWidget(widget)
        self.progressed.connect(self.update_result)
        host.tasks.submit(lambda: import_files(host.service, paths, self.progressed.emit, self.cancel),
                          self.finished_import, self.failed)

    def update_result(self, result):
        self.rows.addItem(result['name'] + (' · 导入成功' if 'article_id' in result else ' · ' + result['error']))
        self.rows.item(self.rows.count()-1).setToolTip(self.rows.item(self.rows.count()-1).text())
        self.progress.setValue(self.rows.count())
        self.rows.scrollToBottom()

    def finished_import(self, results):
        self.running = False
        self.results = results
        success = sum('article_id' in row for row in results)
        self.status.setText(f'已导入 {success} 个，失败 {len(results)-success} 个，未处理 {self.total-len(results)} 个。')
        self.stop.setText('完成')
        self.stop.setEnabled(True)

    def failed(self, message):
        self.running = False
        self.status.setText(message)
        self.stop.setText('关闭')
        self.stop.setEnabled(True)

    def reject(self):
        if self.running:
            self.cancel.set()
            self.status.setText('正在完成当前文件，随后停止。已导入的资料会保留。')
            self.stop.setEnabled(False)
        else:
            super().reject()
