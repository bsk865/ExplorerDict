"""Small renderer probe; no user documents or model requests."""
import json
import sys
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from PySide6.QtWebEngineWidgets import QWebEngineView

app = QApplication([])
view = QWebEngineView()
view.page().renderProcessTerminated.connect(
    lambda status, code: print('renderer_exit', status, code, flush=True))
view.page().loadingChanged.connect(
    lambda info: print('load', info.status(), info.errorCode(), info.errorString(), flush=True))

def loaded(ok):
    print('loaded', ok, flush=True)
    view.page().runJavaScript('document.body.innerText',
                             lambda text: (print(json.dumps(text), flush=True), app.quit()))

view.loadFinished.connect(loaded)
view.setHtml('<html><body>Renderer ready</body></html>')
view.show()
QTimer.singleShot(15000, app.quit)
sys.exit(app.exec())
