"""Render plan week breakpoints offscreen with synthetic content only."""
import json
import os
from pathlib import Path
import sys
import tempfile
from datetime import date, timedelta
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PySide6.QtWidgets import QApplication, QWidget
from PySide6.QtGui import QFont, QFontDatabase
from app.native.study import StudyPage
from app.native.plans import WeekPlans
from app.native.widgets import stylesheet


def main():
    app = QApplication([])
    font_id = QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
    app.setFont(QFont(QFontDatabase.applicationFontFamilies(font_id)[0], 10))
    host = QWidget()
    for name in ('edit_task', 'show_advisor', 'plan_history', 'refresh'):
        setattr(host, name, Mock())
    host.plan_view, host.week_offset = 'week', 0
    start = date.today() - timedelta(days=date.today().weekday())
    tasks = [{'id':str(i), 'title':title, 'date':(start + timedelta(days=i % 4)).isoformat(),
              'time':'09:00' if i < 4 else '14:30', 'minutes':25, 'status':status}
             for i, (title, status) in enumerate([
                 ('阅读文章并整理主要观点', 'todo'), ('回顾笔记', 'done'),
                 ('继续阅读并核对原文中的相关论据', 'doing'), ('整理心得', 'todo'),
                 ('复习本周记录，整理需要进一步确认的问题与资料来源', 'todo')])]
    directory = Path(tempfile.mkdtemp(prefix='week-layout-', dir=ROOT/'artifacts/native-checks'))
    results = []
    for mode in ('light', 'dark'):
        preferences = {'palette':'clay', 'mode':mode}
        app.setStyleSheet(stylesheet(preferences))
        page = StudyPage(host, {'study':{'tasks':tasks}, 'preferences':preferences}, 'plans')
        page.setObjectName('page')
        page.show()
        week = page.findChild(WeekPlans)
        for width, columns in ((1250, 7), (850, 3), (600, 1)):
            page.resize(width, 760)
            for _ in range(6):
                app.processEvents()
            assert page.width() == width, (width, page.width())
            assert week.columns == columns, (width, week.columns)
            assert week.horizontalScrollBar().maximum() == 0
            page.grab().save(str(directory/f'{mode}-{width}.png'))
            results.append({'mode':mode, 'width':width, 'columns':columns,
                            'vertical_scroll':week.verticalScrollBar().maximum()})
        page.close()
    report = {'passed':True, 'cases':results, 'directory':str(directory)}
    (directory/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
