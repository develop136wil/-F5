import unittest
from services.news_attachments import select_ocr_images


class TestOcrSelection(unittest.TestCase):
    def image(self, name, status='ocr_required'):
        return {'url': f'https://example.org/{name}.jpg', 'kind': 'jpg', 'status': status}

    def test_attachment_is_read_despite_long_html(self):
        r = self.image('poster')
        self.assertEqual(select_ocr_images([r], [], has_enough_text=True), [r['url']])

    def test_pdf_leaves_one_image_slot(self):
        rows = [self.image('one'), self.image('two')]
        self.assertEqual(select_ocr_images(rows, [], has_enough_text=True, pdf_selected=True), [rows[0]['url']])

    def test_skipped_and_complete_files_do_not_reenter_via_images(self):
        rows = [self.image('skip', 'skipped_limit'), self.image('done', 'ocr_complete')]
        self.assertEqual(select_ocr_images(rows, [r['url'] for r in rows], has_enough_text=False), [])

    def test_explicit_attachment_beats_decoration_and_duplicates(self):
        r = self.image('poster')
        self.assertEqual(select_ocr_images([r], ['https://example.org/logo.jpg', r['url']], has_enough_text=False),
                         [r['url'], 'https://example.org/logo.jpg'])

    def test_total_cap_invalid_url_and_long_html_decoration(self):
        self.assertEqual(len(select_ocr_images([self.image(str(i)) for i in range(5)], [], has_enough_text=False)), 2)
        self.assertEqual(select_ocr_images([], ['javascript:alert(1)', None], has_enough_text=False), [])
        self.assertEqual(select_ocr_images([], ['https://example.org/logo.jpg'], has_enough_text=True), [])
