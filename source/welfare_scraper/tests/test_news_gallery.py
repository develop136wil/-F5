import datetime as dt
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch
from html.parser import HTMLParser

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import news_gallery


class TestNewsMediaCache(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row

    def tearDown(self):
        self.conn.close()

    def test_positive_and_negative_results_have_separate_ttl(self):
        base = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
        news_gallery.store_poster(self.conn, "https://example.com/with", "https://img.example/a.jpg", base)
        news_gallery.store_poster(self.conn, "https://example.com/none", "", base)
        after_eight_days = (base + dt.timedelta(days=8)).replace(tzinfo=None)
        self.assertTrue(news_gallery.get_cached_poster(
            self.conn, "https://example.com/with", after_eight_days)["hit"])
        self.assertFalse(news_gallery.get_cached_poster(
            self.conn, "https://example.com/none", after_eight_days)["hit"])

    def test_upsert_refreshes_cached_poster(self):
        base = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
        news_gallery.store_poster(self.conn, "https://example.com/item", "", base)
        news_gallery.store_poster(self.conn, "https://example.com/item", "https://img.example/new.png", base)
        result = news_gallery.get_cached_poster(self.conn, "https://example.com/item", base.replace(tzinfo=None))
        self.assertTrue(result["hit"])
        self.assertEqual(result["poster_url"], "https://img.example/new.png")


class TestNewsGalleryHtml(unittest.TestCase):
    def test_legacy_markdown_is_readable_without_changing_saved_or_copy_text(self):
        body = ('### ✅ 지원 내용\n- **교재비 5만원(50,000원)** 지원.\n'
                '- 중복 신청은 불가합니다.\n\n### 📝 신청 방법\n'
                '1. [신청 안내](https://example.invalid/apply) 확인\n2. 서류 제출\n'
                '기간: 2026.09.18. ~ 2026.09.30.\n문의 내용은 `원문` 확인')
        item = {'support_detail':body}
        output = news_gallery._detail_html(item)
        self.assertIn('<strong>✅ 지원 내용</strong>',output)
        self.assertIn('<li><strong>교재비 5만원(50,000원)</strong> 지원.</li>',output)
        self.assertIn('<li>중복 신청은 불가합니다.</li>',output)
        self.assertIn('<ol start="1">',output)
        self.assertIn('rel="noopener noreferrer">신청 안내</a>',output)
        self.assertIn('기간: 2026.09.18. ~ 2026.09.30.',output)
        self.assertIn('<code>원문</code>',output)
        self.assertNotIn('###',output)
        self.assertNotIn('**',output)
        self.assertEqual(item['support_detail'],body)
        self.assertIn(body,news_gallery._copy_text(item))

    def test_legacy_markdown_never_executes_html_or_unsafe_links(self):
        body = ('### 안내 <img src=x onerror=alert(1)>\n'
                '- <script>alert(1)</script>\n'
                '[실행](javascript:alert(1)) [파일](file:///local)\n'
                '[안전](https://example.invalid/\"onclick=\"bad)\n'
                '```\n### 코드의 제목은 그대로\n<script>code</script>\n```')
        output = news_gallery._legacy_markdown_html(body)
        self.assertNotIn('<img',output)
        self.assertNotIn('<script>',output)
        self.assertNotIn('href="javascript:',output)
        self.assertNotIn('href="file:',output)
        self.assertIn('&lt;img',output)
        self.assertIn('&lt;script&gt;',output)
        self.assertIn('href="https://example.invalid/&quot;onclick=&quot;bad"',output)
        self.assertIn('<pre>### 코드의 제목은 그대로\n&lt;script&gt;code&lt;/script&gt;</pre>',output)

    def test_legacy_markdown_retains_multiline_restrictions_and_unclosed_code(self):
        output = news_gallery._legacy_markdown_html('- 지원 대상은 아동입니다.\n  다만 중복 신청은\n  허용하지 않습니다.\n\n```\n**문자 그대로**')
        self.assertIn('<li>지원 대상은 아동입니다.\n다만 중복 신청은\n허용하지 않습니다.</li>',output)
        self.assertIn('<pre>**문자 그대로**</pre>',output)
        self.assertEqual(news_gallery._legacy_markdown_html('원문 첫줄\n\n두 번째 줄\n'),'원문 첫줄\n\n두 번째 줄\n')

    def test_long_content_is_complete_escaped_foldable_and_copyable(self):
        item = {
            'title': '조건 확인', 'support_detail': '본문 안내\n' * 500 + '끝 조건: 지원 대상이 아닙니다.',
            'eligibility_detail': '<script>태그는 문자</script>\n추가 자격 요건',
            'contact': '문의: 02-000-0000', 'poster_url': 'https://example.invalid/poster.jpg',
        }
        class Content(HTMLParser):
            def __init__(self):
                super().__init__()
                self.copy, self.fields, self.detail_attrs = '', [], []
                self.in_dd = False
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == 'article': self.copy = attrs.get('data-copy', '')
                if tag == 'details' and attrs.get('class') == 'full-content': self.detail_attrs.append(attrs)
                if tag == 'dd': self.in_dd = True; self.fields.append('')
            def handle_data(self, data):
                if self.in_dd: self.fields[-1] += data
            def handle_endtag(self, tag):
                if tag == 'dd': self.in_dd = False
        path = news_gallery.generate_html([item])
        try:
            output = Path(path).read_text(encoding='utf-8')
            parsed = Content(); parsed.feed(output)
            self.assertEqual(parsed.fields, [item[k] for k, _ in news_gallery.DETAIL_FIELDS])
            self.assertEqual(parsed.copy, news_gallery._copy_text(item))
            self.assertNotIn('open', parsed.detail_attrs[0])
            for _, value in news_gallery._details(item): self.assertIn(value, parsed.copy)
            self.assertNotIn('<script>태그는 문자</script>', output)
            self.assertNotIn('-webkit-line-clamp', output)
            self.assertIn("window.addEventListener('beforeprint'", output)
            self.assertIn('break-inside:auto', output)
        finally:
            os.unlink(path)

    def test_complete_short_content_is_initially_visible(self):
        path = news_gallery.generate_html([{'support_detail': '선착순 아님. 추첨으로 선발합니다.'}])
        try:
            output = Path(path).read_text(encoding='utf-8')
            self.assertIn('<details class="full-content" open>', output)
            self.assertIn('<dd>선착순 아님. 추첨으로 선발합니다.</dd>', output)
        finally:
            os.unlink(path)

    def test_oversized_document_never_creates_a_partial_file(self):
        for limit in (1, 900):
            with self.subTest(limit=limit), patch.object(news_gallery, 'MAX_GALLERY_BYTES', limit), \
                    patch.object(news_gallery, 'create_temp_file') as create:
                with self.assertRaisesRegex(news_gallery.GalleryTooLargeError, '나누어 선택'):
                    news_gallery.generate_html([{'support_detail': '보존할 내용'}])
                create.assert_not_called()

    def test_compact_layout_and_manual_copy_accessibility(self):
        path = news_gallery.generate_html([{'title': '긴 제목' * 80}])
        try:
            output = Path(path).read_text(encoding='utf-8')
            self.assertIn('repeat(auto-fill,minmax(min(100%,280px),1fr))', output)
            self.assertIn('max-width:420px', output)
            self.assertIn('.media.no-image,.media.missing{aspect-ratio:auto;height:64px}', output)
            self.assertIn('object-fit:contain', output)
            self.assertIn('aria-labelledby="manual-copy-title"', output)
            self.assertIn('aria-label="직접 복사할 내용" readonly', output)
            self.assertIn('role="status" aria-live="polite"></div>', output)
        finally:
            os.unlink(path)

    def test_empty_gallery_disables_copy(self):
        path = news_gallery.generate_html([])
        try:
            output = Path(path).read_text(encoding='utf-8')
            self.assertIn('data-copy-action disabled onclick="copyAll()"', output)
            self.assertIn('내보낼 새소식이 없습니다.', output)
        finally:
            os.unlink(path)

    def test_generated_copy_script_runtime(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Generated HTML runtime regression needs Node.js')
        path = news_gallery.generate_html([{'title': '시험 내용'}])
        try:
            result = subprocess.run(
                [node, '--test-reporter=tap', str(Path(__file__).with_name('news_gallery_runtime.cjs')), path],
                capture_output=True, text=True, encoding='utf-8', timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('# fail 0', result.stdout)
        finally:
            os.unlink(path)

    def test_partial_input_warning_survives_html_and_copy_with_escaping(self):
        import json
        item = {'title': '안내', 'ai_data': json.dumps({'source_input': {
            'status': 'partial', 'note': '내용 일부만 전달 <script>주의</script>'}})}
        path = news_gallery.generate_html([item])
        try:
            with open(path, encoding='utf-8') as handle:
                output = handle.read()
            self.assertIn('원문 확인 필요', news_gallery._copy_text(item))
            self.assertIn('class="review"', output)
            self.assertIn('&lt;script&gt;주의&lt;/script&gt;', output)
            self.assertNotIn('<script>주의</script>', output)
        finally:
            os.unlink(path)

    def test_gallery_is_self_contained_safe_and_copyable(self):
        path = news_gallery.generate_html([{
            "title": "<script>alert(1)</script> 돌봄 행사",
            "org": "도봉구",
            "status": "진행 중",
            "deadline": "2026-09-30",
            "support_detail": "신청 내용을 확인하세요.",
            "link": "https://example.com/news?a=1&b=2",
            "poster_url": "https://img.example/poster.jpg",
        }])
        try:
            with open(path, encoding="utf-8") as handle:
                output = handle.read()
            self.assertIn("복지F5 새소식", output)
            self.assertIn("전체 내용 복사", output)
            self.assertIn("내용 복사", output)
            self.assertIn("https://img.example/poster.jpg", output)
            self.assertNotIn("<script>alert(1)</script>", output)
            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", output)
            self.assertNotIn("cdn.", output)
        finally:
            os.unlink(path)

    def test_non_http_links_and_images_are_not_emitted(self):
        path = news_gallery.generate_html([{
            "title": "안전한 제목",
            "link": "javascript:alert(1)",
            "poster_url": "file:///secret.png",
        }])
        try:
            with open(path, encoding="utf-8") as handle:
                output = handle.read()
            self.assertNotIn("javascript:alert", output)
            self.assertNotIn("file:///", output)
            self.assertIn("no-image", output)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
