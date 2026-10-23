"""Render current native UI with authored demo data, never a user's database."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import sys, tempfile, time
from pathlib import Path
from datetime import date
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFontDatabase, QFont
from app.native.service import NativeService
from app.native.window import NativeWindow
from app.native.settings import SettingsDialog
from app import study_weather

def main():
    output = ROOT / 'docs/screenshots/native-20261023'
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    fid = QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
    app.setFont(QFont(QFontDatabase.applicationFontFamilies(fid)[0], 10))
    with tempfile.TemporaryDirectory(prefix='public-ui-') as temporary:
        os.environ['EXPLORER_DICT_DATA_DIR'] = temporary
        os.environ['EXPLORER_DICT_STORAGE_CONFIG'] = str(Path(temporary)/'location.json')
        service = NativeService(Path(temporary))
        article = service.import_text('把阅读变成自己的理解 · 演示资料',
            '阅读从一个问题开始。遇到陌生概念时，先保留它所在的句子，再尝试用自己的话解释。\n\n'
            '读懂一段材料，不等于记住所有细节。把原文证据、自己的理解和仍待确认的问题分开记录，有助于复习。\n\n'
            '一次阅读可以分成三个小步骤：先浏览结构，再围绕问题精读，最后写下一条心得。\n\n'
            '不同资料之间的联系需要回到具体语句核对。引用让结论可追溯，笔记让学习可积累。')
        service.import_text('本地资料问答 · 演示资料', '资料库问答从本地文章与抓取片段中寻找依据。阅读文章时，可以在侧栏围绕本文提问。')
        service.hub.save('themes', {'title':'阅读与思考'})
        service.hub.save('notes', {'article_id':article['id'], 'title':'把问题留在原文旁边', 'content':'先读原句，再记下自己的理解。'})
        service.hub.save('tasks', {'title':'精读一篇文章，留下一个问题', 'date':date.today().isoformat(), 'minutes':25, 'article_ids':[article['id']]})
        window = NativeWindow(service, discovery_updates=False)
        window.resize(1440, 900)
        window.capture_enabled = False
        window.show()
        def settle():
            end=time.monotonic()+8
            while time.monotonic()<end:
                app.processEvents()
                if not window.navigation_busy and not window.tasks.pending:
                    for _ in range(5): app.processEvents()
                    return
                time.sleep(.02)
            raise RuntimeError('Native screen did not settle')
        settle()
        for route, name in [('today','today'), ('shelf','library'), ('reader','reading'), ('library','rag')]:
            window.navigate(route, article['id'] if route=='reader' else None)
            settle()
            if route=='reader':
                window.stack.currentWidget().set_assistant_visible(True)
                app.processEvents()
            window.grab().save(str(output/(name+'.png')))
        dialog=SettingsDialog(window);dialog.show();app.processEvents()
        dialog.grab().save(str(output/'settings.png'));dialog.close()
        window.close();app.processEvents()
        service.close()
    print(output)

if __name__ == '__main__': main()
