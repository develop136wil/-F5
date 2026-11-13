import unittest
from ai_classifier import _parse_page_html


class TestBulletinExtraction(unittest.TestCase):
    def test_verified_layout_preserves_conditions_and_removes_menu(self):
        html = '''<title>공지사항(상세보기)</title><meta property="og:title" content="기관 홈페이지">
        <div>메뉴잡음 채용 공고</div><div class="bbsView">
        <table class="boardView"><caption>상세보기 설명</caption>
        <tr><th>제목</th><td class="title">참여매장 모집</td></tr>
        <tr><th>등록일</th><td>2026.07.31</td></tr>
        <tr><td><a href="/download.pdf">신청 안내.pdf</a></td></tr></table>
        <div class="bbsCont">신청대상: 식품 판매 업소 영업자
        <table><tr><td>제외: 미등록 업소</td></tr></table>
        신청기간: 2026. 7. 31.까지<script>잡음()</script></div>
        <div class="bbsBtn">이전글 어르신 일자리</div></div>'''
        result = _parse_page_html(html, 'https://example.org/post')
        self.assertEqual(result['title'], '참여매장 모집')
        for value in ('업소 영업자', '제외: 미등록 업소', '2026. 7. 31.', '등록일'):
            self.assertIn(value, result['text'])
        for value in ('메뉴잡음', '이전글', '잡음()', '상세보기 설명'):
            self.assertNotIn(value, result['text'])
        self.assertIn('https://example.org/download.pdf', result['attachments'])
        self.assertIn('메뉴잡음', result['policy_text'])

    def test_incomplete_or_ambiguous_layout_falls_back(self):
        view = '<div class="bbsView"><table class="boardView"><td class="title">제목</td></table><div class="bbsCont">본문</div></div>'
        for content in (view.replace('bbsCont', 'other'), view + view):
            result = _parse_page_html('<title>원래 제목</title><p>보존할 내용</p>' + content, 'https://example.org')
            self.assertEqual(result['title'], '원래 제목')
            self.assertIn('보존할 내용', result['text'])

    def test_image_only_body_does_not_discard_surrounding_evidence(self):
        result = _parse_page_html('<title>제목</title><p>포스터 설명</p><div class="bbsView"><table class="boardView"><td class="title">공지</td></table><div class="bbsCont"><img src="poster.png"></div></div>', 'https://example.org')
        self.assertIn('포스터 설명', result['text'])
