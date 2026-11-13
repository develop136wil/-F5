import json
from pathlib import Path
import unittest
from bs4 import BeautifulSoup
from services.news_attachments import summarize_attachment_reports
from services.news_gallery import generate_html, _attachment_links


class TestAttachmentLinks(unittest.TestCase):
    def test_saved_summary_to_html_preserves_file_and_warning(self):
        summary = summarize_attachment_reports([{'url':'https://example.org/file?id=2&download=1',
            'filename':'안내 <공고>.pdf','kind':'pdf','status':'skipped_limit'}])
        item = {'title':'시험', 'ai_data':json.dumps({'source_attachments':summary})}
        path = Path(generate_html([item]))
        try:
            soup = BeautifulSoup(path.read_text(encoding='utf-8'), 'html.parser')
            link = soup.find('a', string='안내 <공고>.pdf')
            self.assertEqual(link['href'], 'https://example.org/file?id=2&download=1')
            self.assertIn('noopener', link['rel'])
            self.assertIn('개수 제한으로 미확인', soup.get_text())
            self.assertIsNone(soup.find('공고'))
        finally:
            path.unlink()

    def test_unsafe_urls_duplicates_and_legacy_records(self):
        files = [{'url':u} for u in ['javascript:alert(1)', 'file:///C:/test.pdf',
                 'https://user:secret@example.org/a', 'https://example.org/\nfile',
                 'https://example.org/a', 'https://example.org/a']]
        item = {'ai_data':{'source_attachments':{'files':files}}}
        self.assertEqual(_attachment_links(item), [('첨부파일', 'https://example.org/a')])
        self.assertEqual(_attachment_links({'ai_data':{'source_attachments':{'files':[{'filename':'old.pdf'}]}}}), [])
        self.assertEqual(_attachment_links({'ai_data':'bad json'}), [])

    def test_summary_does_not_save_executable_url(self):
        result = summarize_attachment_reports([{'url':'javascript:alert(1)', 'status':'ocr_required'}])
        self.assertEqual(result['files'][0]['url'], '')
