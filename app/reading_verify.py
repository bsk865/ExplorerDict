"""Opt-in packaged window check; never displays a window or reads external text."""
import json
import sys
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QPushButton


def verify_window(window, directory):
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    report = {'frameless':bool(window.windowFlags() & Qt.WindowType.FramelessWindowHint)}
    target = directory / 'window-verification.json'
    def loading(info):
        if info.errorCode() < 0:
            report['load_detail'] = {'code':info.errorCode(), 'message':info.errorString()}
    window.page.loadingChanged.connect(loading)
    def finish():
        window.findChild(QPushButton, 'close').click()
        report['close_button'] = window.closed
        target.write_text(json.dumps(report), encoding='utf-8')
    if '--verify-immediate-close' in sys.argv:
        report['immediate_close'] = True
        QTimer.singleShot(0, finish)
        return
    def loaded(ok):
        if not ok:
            report['load_error'] = True
            QTimer.singleShot(100, finish)
            return
        def inspect():
            def checked(value):
                report['rendered'] = bool(value and '资料书架' in value)
                window.grab().save(str(directory / 'packaged-window.png'))
                window.findChild(QPushButton, 'maximize').click()
                report['maximize'] = window.isMaximized()
                window.findChild(QPushButton, 'maximize').click()
                report['restore'] = not window.isMaximized()
                window.findChild(QPushButton, 'minimize').click()
                report['minimize'] = window.isMinimized()
                finish()
            window.page.runJavaScript('document.body.innerText', checked)
        QTimer.singleShot(2000, inspect)
    window.page.loadFinished.connect(loaded)
    def timeout():
        if not window.closed:
            report['timeout'] = True
            finish()
    QTimer.singleShot(30000, timeout)
