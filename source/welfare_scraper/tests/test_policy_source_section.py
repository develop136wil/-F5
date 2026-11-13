import os
import sys
import unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services.policy_source_section import select_policy_section


class SectionTests(unittest.TestCase):
    def test_sibling_policy_is_excluded(self):
        text = '# 복지 안내\n## 공영주차장 감면\n50% 할인\n### 신청 조건\n등록 차량\n## 고궁 관람\n무료 입장'
        result = select_policy_section(text, '공영주차장 감면')
        self.assertIn('등록 차량', result)
        self.assertNotIn('무료 입장', result)
        self.assertNotIn('# 복지 안내', result)

    def test_long_conditions_are_not_cut_at_400_characters(self):
        text = '## 주차 감면\n' + '상세 조건입니다. ' * 100 + '\n마지막 조건: 본인 탑승\n## 다른 제도\n별도 내용'
        self.assertIn('본인 탑승', select_policy_section(text, '주차 감면'))
        self.assertNotIn('별도 내용', select_policy_section(text, '주차 감면'))

    def test_keyword_in_unstructured_text_does_not_select_wrong_window(self):
        text = '주차 감면 관련 메뉴\n' + '다른 설명 ' * 100 + '\n주차 감면 조건: 본인 탑승'
        self.assertEqual(select_policy_section(text, '주차 감면'), text)

    def test_duplicate_headings_are_not_guessed(self):
        text = '## 주차 감면\n서울\n## 주차 감면\n부산'
        self.assertEqual(select_policy_section(text, '주차 감면'), text)

    def test_multiline_title_uses_specific_heading(self):
        text = '## 베이비 마사지 교실\n신청 조건\n## 부모 교실\n별도'
        self.assertNotIn('별도', select_policy_section(text, '건강 교실\n베이비 마사지 교실'))
