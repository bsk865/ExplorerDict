import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from app.native.service import NativeService
from app.reading_export import reading_markdown, write_reading_notes, article_versions


class ReadingExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = NativeService(Path(self.temp.name))
        self.service.workspace.map_preloader.close()
        self.article = self.service.store.import_article('阅读与实验', [
            {'type':'text', 'text':'测量温度为83摄氏度。', 'page':2},
            {'type':'text', 'html':'<table><tr><th>A</th><td>B</td></tr></table><p><math><mfrac><mi>x</mi><mn>2</mn></mfrac></math></p>'}])

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_rich_source_marks_and_archived_citations_export_without_internal_ids(self):
        article = self.article
        self.service.annotations.save({'article_id':article['id'], 'version':article['version'], 'style':'yellow',
            'segments':[{'block':'b0','start':0,'end':11,'quote':'测量温度为83摄氏度。'}]})
        obj = self.service.store.object(article['id'], {'type':'term','title':'测量温度','block':'b0'})
        self.service.store.update_object(obj['id'], {'note':'对象心得'})
        hit = dict(article_id=article['id'], article_title=article['title'], block='b0', page=2, quote='测量温度为83摄氏度。', citation='S1')
        self.service.store.put('sessions', article['id']+':'+article['version']+':'+obj['id'], {
            'note':'会话心得', 'archives':[{'messages':[{'role':'assistant','content':'旧回答[S1]', 'evidence':[hit]}], 'handoff':'internal-only'}],
            'messages':[{'role':'user','content':'温度是多少？'}, {'role':'assistant','content':'新回答[S1]','evidence':[dict(hit, quote='新的已保存依据')]}],
            'draft':'继续核对误差'})
        exported = reading_markdown(self.service, article)
        for value in ('<table>', '<mfrac>', '黄色荧光', '第2页', '> 测量温度为83摄氏度。', '对象心得',
                      '会话心得', '旧回答[S1]', '新回答[S1]', '> 新的已保存依据', '继续核对误差'):
            self.assertIn(value, exported)
        self.assertLess(exported.index('旧回答'), exported.index('新回答'))
        for value in (article['id'], article['version'], obj['id'], '来源：b0', 'internal-only'):
            self.assertNotIn(value, exported)

    def test_article_drafts_and_partial_translation_stay_with_their_own_article(self):
        article = self.article
        other = self.service.import_text('其他资料', 'Other article.')
        saved = self.service.hub.save('notes', {'article_id':article['id'], 'title':'心得', 'content':'原保存内容'})
        self.service.store.put('settings', 'native-note-drafts:'+article['id'], [
            dict(saved, content='尚未落入笔记表的修改'), {'article_id':article['id'], 'draft_id':'new', 'content':'新写下的心得'},
            {'article_id':other['id'], 'content':'不属于本文的草稿'}])
        self.service.store.put('settings', 'native-translation:article:'+article['id'],
            dict(source='en', target='zh', text='Selected paragraph.', result='当前文章的部分译文', complete=False))
        self.service.store.put('settings', 'native-translation:article:'+other['id'], dict(result='其他文章的译文'))
        exported = reading_markdown(self.service, article)
        for value in ('尚未落入笔记表的修改', '新写下的心得', '（草稿）', '译文（未完成）', '英语 → 简体中文',
                      'Selected paragraph.', '当前文章的部分译文'):
            self.assertIn(value, exported)
        for value in ('原保存内容', '不属于本文的草稿', '其他文章的译文'):
            self.assertNotIn(value, exported)

    def test_image_is_self_contained_and_empty_object_sections_are_omitted(self):
        raw = io.BytesIO()
        Image.new('RGB', (20,10), 'red').save(raw, 'PNG')
        asset, _ = self.service.store.image(raw.getvalue())
        article = self.service.store.import_article('图片阅读', [{'type':'image', 'asset':asset, 'caption':'原始图像'}])
        exported = reading_markdown(self.service, article)
        self.assertIn('![原始图像](data:image/png;base64,', exported)
        self.assertNotIn('## 原始图像', exported)
        self.assertNotIn(asset, exported)

    def test_failed_replacement_preserves_existing_export_and_cleans_temporary_file(self):
        target = Path(self.temp.name)/'阅读笔记.md'
        target.write_text('上一次完整导出', encoding='utf-8')
        with patch('app.reading_export.os.replace', side_effect=OSError('disk error')):
            with self.assertRaises(OSError):
                write_reading_notes(self.service, self.article, target)
        self.assertEqual('上一次完整导出', target.read_text(encoding='utf-8'))
        self.assertEqual([], list(Path(self.temp.name).glob('.*.tmp')))
        write_reading_notes(self.service, self.article, target)
        self.assertIn('# 阅读与实验', target.read_text(encoding='utf-8'))

    def test_version_order_follows_creation_and_rename_is_reflected_in_export(self):
        old = self.article
        new = self.service.append(old['id'], [{'type':'text', 'text':'新增段落'}])
        self.assertEqual([old['id'], new['id']], [item['id'] for item in article_versions(self.service, new)])
        self.service.rename(old['id'], '更正后的标题')
        self.assertTrue(reading_markdown(self.service, old).startswith('# 更正后的标题\n'))


if __name__ == '__main__':
    unittest.main()
