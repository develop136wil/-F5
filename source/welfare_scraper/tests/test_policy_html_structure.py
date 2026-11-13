import os
import sys
import unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ai_classifier
from services.policy_source_section import select_policy_section


class HtmlStructureTests(unittest.TestCase):
    def test_html_headings_survive_policy_parse_without_changing_news_text(self):
        html = '<html><title>안내</title><body><h2>공영주차장 감면</h2><p>50% 할인</p><h3>신청 조건</h3><p>본인 탑승</p><h2>고궁 관람</h2><p>무료 입장</p></body></html>'
        result = ai_classifier._parse_page_html(html, 'https://example.invalid')
        self.assertNotIn('##', result['text'])
        self.assertEqual(result['title'], '안내')
        section = select_policy_section(result['policy_text'], '공영주차장 감면')
        self.assertIn('본인 탑승', section)
        self.assertNotIn('무료 입장', section)

    def test_heading_free_page_retains_text(self):
        result = ai_classifier._parse_page_html('<p>공영주차장 50% 할인</p>', 'https://example.invalid')
        self.assertEqual(result['policy_text'], '공영주차장 50% 할인')

    def test_sibling_catalogue_items_exclude_other_benefits(self):
        html = '''<ul><li>고궁 감면<ul><li>무료 입장</li></ul></li>
        <li>공영주차장 주차요금 감면<ul><li>등록 차량</li><li>환승주차장 80% 할인</li></ul></li>
        <li>고속도로 할인<br>별도 조건<ul><li>50% 할인</li></ul></li></ul>'''
        parsed = ai_classifier._parse_page_html(html, 'https://example.invalid')
        section = select_policy_section(parsed['policy_text'], '공영주차장 주차 요금 감면')
        self.assertIn('등록 차량', section)
        self.assertIn('환승주차장 80%', section)
        self.assertNotIn('고궁', section)
        self.assertNotIn('고속도로', section)
        self.assertNotIn('######', parsed['text'])

    def test_duplicate_catalogue_titles_are_not_arbitrarily_selected(self):
        html = '<ul><li>주차 감면<ul><li>서울</li></ul></li><li>주차 감면<ul><li>부산</li></ul></li></ul>'
        parsed = ai_classifier._parse_page_html(html, 'https://example.invalid')
        self.assertEqual(select_policy_section(parsed['policy_text'], '주차 감면'), parsed['policy_text'])
