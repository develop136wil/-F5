"""Local synthetic strings only: no configuration, database, SDK or network."""
import unittest
from unittest.mock import patch

from services.ai_input_safety import (
    AiInputReviewRequired, REVIEW_MESSAGE, ensure_ai_text_safe, inspect_ai_text,
)


class TestAiInputSafety(unittest.TestCase):
    def test_ordinary_policy_terms_dates_amounts_and_official_phones_are_allowed(self):
        examples = (
            '장애인·한부모·조손·다문화 가구, 중위소득 180% 이하, 만 0~9세',
            '신청기간 2026-09-01~2026-10-31. 생후 14일~71개월.',
            '지원금 1,000,000원, 9001011234567원, 예산 2026090912345원',
            '문의 02-123-4567 / 031-1234-5678 / 070-1234-5678',
            '대표전화 120, 129, 1588-0000, 1644-0000, 1800-0000',
            '기관 문의 info@center.or.kr / welfare@district.go.kr / contact@school.ac.kr',
            'API key 만드는 방법. API_KEY=YOUR_API_KEY_HERE. Bearer YOUR_TOKEN_HERE',
            '주민등록번호를 입력하지 마세요. 연락처 010-****-**** / 이름 홍*동',
            '',
        )
        for text in examples:
            with self.subTest(text=text):
                self.assertTrue(inspect_ai_text(text).allowed)
                self.assertIsNone(ensure_ai_text_safe(text))

    def test_identity_format_requires_calendar_date_but_not_checksum(self):
        for text in ('900101-1234567', '9001011234567', '주민번호 000229 3234567',
                     '외국인등록번호 900101-5234567', '주민등록번호: 9001011234567원'):
            with self.subTest(text=text):
                self.assertIn('identity_number', inspect_ai_text(text).reasons)
        for text in ('991332-1234567', '010229-1234567', '2026-09-09', '12345678901234567'):
            with self.subTest(text=text):
                self.assertNotIn('identity_number', inspect_ai_text(text).reasons)

    def test_mobile_formats_are_held_without_rewriting(self):
        for text in ('010-1234-5678', '01012345678', '011 123 4567', '+82-10-1234-5678',
                     '+82 (0)10 1234 5678', '문의 담당자: ０１０－１２３４－５６７８',
                     '010\u200b–1234–5678'):
            with self.subTest(text=text):
                original = text
                self.assertIn('mobile_number', inspect_ai_text(text).reasons)
                self.assertEqual(text, original)
        self.assertTrue(inspect_ai_text('수치 10101234567890').allowed)

    def test_private_or_ambiguous_email_is_not_assumed_public(self):
        for text in ('synthetic.person@example.com', 'synthetic.person@gmail.com',
                     'info@gmail.com', 'named.staff@district.go.kr',
                     'contact@district.go.kr.example.com'):
            with self.subTest(text=text):
                self.assertIn('email_address', inspect_ai_text(text).reasons)

    def test_known_credentials_and_explicit_secret_assignments_are_held(self):
        examples = (
            'AIza' + 'a' * 35, 'ntn_' + 'b' * 32, 'secret_' + 'c' * 32,
            'ghp_' + 'd' * 36, 'github_pat_' + 'e' * 32, 'AKIA' + 'A' * 16,
            'sk-proj-' + 'a1' * 20, '-----BEGIN PRIVATE KEY-----',
            'Authorization: Bearer synthetic-token-123456789',
            'GOOGLE_API_KEY="synthetic-secret-123456789"',
            'https://example.com?key=synthetic-secret-123456789',
            'https://example.com?access_token=synthetic%2Fsecret12345',
            'https://synthetic-user:synthetic-password@example.com',
            'https://discord.com/api/webhooks/123456789012345678/' + 'a' * 30,
            'token=your_real_secret_12345',
        )
        for text in examples:
            with self.subTest(kind=text[:12]):
                self.assertIn('credential', inspect_ai_text(text).reasons)

    def test_errors_and_result_repr_do_not_retain_matches(self):
        private = '900101-1234567 / 010-1234-5678 / synthetic.person@example.com / ntn_' + 'b' * 32
        result = inspect_ai_text(private)
        self.assertEqual(result.reasons, ('identity_number', 'mobile_number', 'email_address', 'credential'))
        with self.assertRaises(AiInputReviewRequired) as caught:
            ensure_ai_text_safe(private)
        error = caught.exception
        self.assertEqual(error.code, 'AI_INPUT_REVIEW_REQUIRED')
        self.assertEqual(str(error), REVIEW_MESSAGE)
        for value in ('900101', '010-1234', 'synthetic.person', 'ntn_'):
            self.assertNotIn(value, repr(result))
            self.assertNotIn(value, str(error))
            self.assertNotIn(value, repr(error.__dict__))

    def test_non_text_is_not_claimed_to_be_inspected(self):
        class NoStringify:
            def __str__(self):
                raise AssertionError('Must not stringify opaque content')
        for value in (b'%PDF-synthetic', b'PNG', None, {}, [], 42, NoStringify()):
            with self.subTest(kind=type(value).__name__):
                self.assertEqual(inspect_ai_text(value).reasons, ('uninspected_input',))
                with self.assertRaises(AiInputReviewRequired):
                    ensure_ai_text_safe(value)
        with self.assertRaises(AiInputReviewRequired):
            ensure_ai_text_safe()

    def test_checks_all_payloads_and_never_reads_configuration_or_files(self):
        with patch('builtins.open', side_effect=AssertionError('No file access')):
            with self.assertRaises(AiInputReviewRequired):
                ensure_ai_text_safe('일반 정책', '010-1234-5678', '다른 일반 원문')
            self.assertIsNone(ensure_ai_text_safe('첫 문단', '둘째 문단'))

    def test_long_non_sensitive_text_and_sensitive_tail_are_not_truncated(self):
        text = '일반 지원내용 2026년. ' * 20000
        self.assertTrue(inspect_ai_text(text).allowed)
        self.assertIn('mobile_number', inspect_ai_text(text + '010-1234-5678').reasons)


if __name__ == '__main__':
    unittest.main()
