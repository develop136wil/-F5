"""Source-line candidate regressions, not an AI semantic-accuracy evaluation."""
import copy
import hashlib
import json
from pathlib import Path
import unittest

from services.policy_source_coverage import COVERAGE_KINDS, coverage_candidates, coverage_sections


def numbers(content):
    return {item['line_number'] for item in coverage_candidates(content)}


def missing(content, ranges):
    # Test-only integration model: a range is inclusive and one-based. Production
    # range validation is tested separately; this helper grants no approval.
    selected = {line for start, end in ranges for line in range(start, end + 1)}
    return numbers(content) - selected


class PolicySourceCoverageTests(unittest.TestCase):
    def test_section_payloads_are_not_satisfied_by_only_the_heading(self):
        cases = (
            ('지원 제외 대상', '주택 소유자(분양권, 입주권 포함)', 'exclusion'),
            ('지원 제외 대상', '부모 소유 주택에 임차하는 경우(배우자의 부모 소유 주택 포함)', 'exclusion'),
            ('제출서류', '임대차계약서 사본 1부', 'documents'),
            ('신청방법', '부산청년플랫폼 온라인 접수', 'application'),
            ('신청기간', '2026.8.18.(화) 10:00 ~ 8.24.(월) 18:00', 'application'),
            ('지원내용', '교통비 월 3만원', 'benefit'),
            ('대상자별 추가지원금', '전월 출석 12일 충족 시 월 20만원', 'benefit'),
            ('가입 및 유지 기준', '차상위 이하\n5,025,353', 'continuation'),
        )
        for heading, payload, kind in cases:
            with self.subTest(heading=heading, payload=payload):
                content = heading + '\n' + payload
                expected = set(range(1, len(content.splitlines()) + 1))
                self.assertEqual(numbers(content), expected)
                self.assertEqual({item['kind'] for item in coverage_candidates(content)}, {kind})
                self.assertEqual(missing(content, [(1, 1)]), expected - {1})
                self.assertEqual(missing(content, [(1, max(expected))]), set())

    def test_exclusions_keep_exception_and_wrapped_cap_together(self):
        content = ('지원 제외 대상\n유사사업 수혜자\n'
                   '※ 타 유사사업에서 중개보수만 지원받은 경우 이사비만 지원 가능\n'
                   '(반대도 동일. 단, 합산 최대 수혜 금액 40만원 한도)\n'
                   '지원내용\n실제 비용 최대 40만원, 생애 1회')
        self.assertEqual(numbers(content), set(range(1, 7)))
        self.assertEqual(missing(content, [(1, 3), (5, 6)]), {4})
        self.assertEqual([item['kind'] for item in coverage_candidates(content)],
                         ['exclusion'] * 4 + ['benefit'] * 2)

    def test_required_documents_preserve_direct_submit_and_public_lookup_labels(self):
        content = ('제출서류\n직접제출\n임대차계약서 사본 1부\n'
                   '① 입실확인서 ② 임대사업자등록증 사본\n'
                   '공공마이데이터(조회)\n주민등록 등·초본 1부\n배우자 포함')
        self.assertEqual(numbers(content), set(range(1, 8)))
        self.assertEqual(missing(content, [(1, 4), (6, 7)]), {5})

    def test_each_application_route_and_its_period_remain_required(self):
        content = ('신청기간\n2026. 5. 4. ~ 2026. 5. 20.\n신청방법\n'
                   '(온라인 신청) 복지로\n(방문 신청) 거주지 관할 행정복지센터')
        self.assertEqual(numbers(content), {1, 2, 3, 4, 5})
        self.assertEqual(missing(content, [(1, 4)]), {5})

    def test_unknown_subheading_does_not_discard_later_conditions(self):
        content = '제출서류\n특별한 상황에 해당하는 분\n입실확인서\n사업자등록증 사본'
        self.assertEqual(numbers(content), {1, 2, 3, 4})

    def test_contact_and_known_navigation_stop_section_inheritance(self):
        for boundary in ('문의처', '문 의', '담당기관', '알림ㆍ자료', '개인정보처리방침'):
            with self.subTest(boundary=boundary):
                content = '지원내용\n교통비 월 3만원\n' + boundary + '\n복지정책과\n02-1234-5678'
                self.assertEqual(numbers(content), {1, 2})
                self.assertEqual(missing(content, [(1, 2)]), set())

    def test_contact_boundary_does_not_suppress_later_real_fact(self):
        content = '지원내용\n교통비 월 3만원\n문의처\n복지정책과\n방문 신청 필요'
        self.assertEqual(numbers(content), {1, 2, 5})
        self.assertEqual(coverage_candidates(content)[-1]['kind'], 'application')

    def test_public_page_department_footer_and_ornament_are_not_policy_payload(self):
        for boundary in ('자료담당부서', '자료담당자', '민원상담',
                         '민원상담(지침 및 서류 관련 문의) : 전담 콜센터'):
            with self.subTest(boundary=boundary):
                source = '◈ 지원내용\n교통비 월 3만원\n◈\n' + boundary + '\n복지정책과\n02-1234-5678'
                self.assertEqual(numbers(source), {1, 2})
                self.assertEqual(coverage_sections(source),
                                 [{'start_line': 1, 'end_line': 2, 'kind': 'benefit'}])
        self.assertEqual(numbers('자료담당부서: 복지과. 방문 신청만 가능'), {1})
        # An arbitrary parenthetical constraint must not be stripped as contact metadata.
        self.assertIn(1, numbers('민원상담(방문 신청만 가능): 복지과'))

    def test_neutral_inline_label_does_not_swallow_real_instruction(self):
        for content, kind in (
            ('문의처: 주민센터. 방문 신청만 가능', 'application'),
            ('사업개요: 교통비 월 3만원', 'benefit'),
            ('연락처：복지정책과. 임대차계약서 사본 필수', 'documents'),
        ):
            with self.subTest(content=content):
                self.assertEqual(coverage_candidates(content), [{'line_number': 1, 'kind': kind}])

    def test_known_numbered_or_bold_headings_keep_bare_children(self):
        for heading in ('1. 지원 제외 대상', '2) 지원 제외 대상', '가. 지원 제외 대상',
                        '① 지원 제외 대상', '**지원 제외 대상**', '### 2. 지원 제외 대상'):
            with self.subTest(heading=heading):
                self.assertEqual(numbers(heading + '\n주택 소유자'), {1, 2})

    def test_exclusion_heading_and_bare_child_are_one_context_group(self):
        content = '지원 제외 대상\n주택 소유자\n부모 소유 주택 거주자\n지원내용\n이사비 최대 40만원'
        self.assertEqual(coverage_sections(content), [
            {'start_line': 1, 'end_line': 3, 'kind': 'exclusion'},
            {'start_line': 4, 'end_line': 5, 'kind': 'benefit'},
        ])
        section = coverage_sections(content)[0]
        # Covering all lines in separate fields is not context preservation.
        self.assertEqual(missing(content, [(1, 1), (2, 3), (4, 5)]), set())
        self.assertFalse(any(start <= section['start_line'] and end >= section['end_line']
                             for start, end in [(1, 1), (2, 3)]))

    def test_exclusion_parent_cannot_detach_from_subordinate_age_income_or_residence(self):
        content = ('지원 제외 대상\n연령\n만 65세 이상\n소득기준: 중위소득 100% 이상\n'
                   '거주조건\n자가 주택 거주자\n지원내용\n월 10만원')
        self.assertEqual(coverage_sections(content), [
            {'start_line': 1, 'end_line': 6, 'kind': 'exclusion'},
            {'start_line': 7, 'end_line': 8, 'kind': 'benefit'},
        ])
        self.assertEqual([item['kind'] for item in coverage_candidates(content)][:6],
                         ['exclusion'] * 6)

    def test_eligibility_table_role_headers_stay_with_positive_parent(self):
        for next_heading, next_kind in (('지원내용', 'benefit'), ('신청방법', 'application'),
                                        ('제출서류', 'documents')):
            with self.subTest(next_heading=next_heading):
                content = ('지원대상\n세 가지 조건을 모두 충족한 청년\n가구소득\n50% 이하\n'
                           '연령: 만 15세 이상\n근로기준\n월 10만원\n' + next_heading + '\n상세 내용')
                self.assertEqual(coverage_sections(content), [
                    {'start_line': 1, 'end_line': 7, 'kind': 'eligibility'},
                    {'start_line': 8, 'end_line': 9, 'kind': next_kind},
                ])

    def test_document_role_rows_and_table_captions_remain_in_one_section(self):
        content = ('제출서류\n구분, 제출서류, 확인내용에 관한 표\n직접제출\n'
                   '임대차계약서 사본\n공공마이데이터(조회)\n주민등록 초본\n문의처\n복지정책과')
        self.assertEqual(coverage_sections(content), [
            {'start_line': 1, 'end_line': 6, 'kind': 'documents'},
        ])
        self.assertEqual(numbers(content), {1, 3, 4, 5, 6})

    def test_sections_stop_at_next_known_heading_or_neutral_not_blank_or_unknown(self):
        content = ('지원 제외 대상\n주택 소유자\n\n특별한 상황\n부모 소유 주택 거주자\n'
                   '신청하기\n문의처\n복지정책과\n신청방법\n기관 포털\n\n바로가기')
        self.assertEqual(coverage_sections(content), [
            {'start_line': 1, 'end_line': 5, 'kind': 'exclusion'},
            {'start_line': 9, 'end_line': 10, 'kind': 'application'},
        ])

    def test_sections_use_same_numbered_inline_and_neutral_boundary_rules(self):
        content = ('## 1. 지원 제외 대상\n주택 소유자\n'
                   '문의처: 주민센터. 방문 신청만 가능\n2) 제출서류\n계약서 사본')
        self.assertEqual(coverage_sections(content), [
            {'start_line': 1, 'end_line': 2, 'kind': 'exclusion'},
            {'start_line': 4, 'end_line': 5, 'kind': 'documents'},
        ])
        self.assertIn(3, numbers(content))

    def test_standalone_facts_do_not_create_guessed_sections(self):
        self.assertEqual(coverage_sections('방문 신청 필요\n임대차계약서 사본'), [])
        self.assertEqual(coverage_sections('지원내용\n\n신청하기'), [
            {'start_line': 1, 'end_line': 1, 'kind': 'benefit'},
        ])
        for value in ('', None, [], {}):
            with self.subTest(value=value):
                self.assertEqual(coverage_sections(value), [])

    def test_exact_navigation_and_accessibility_rows_are_skipped_not_arbitrary_rows(self):
        content = ('제출서류\n←→ 표를 가로로 스크롤 해주세요\n'
                   '구분, 제출서류, 확인내용에 관한 표\n구분\n확인 내용\n'
                   '주민등록 등본\n신청하기\n임대차계약서')
        self.assertEqual(numbers(content), {1, 4, 5, 6, 8})
        self.assertEqual(missing(content, [(1, 1), (4, 6), (8, 8)]), set())
        self.assertIn(8, numbers(content))

    def test_inline_labels_mark_payload_and_propagate_section_context(self):
        content = ('### 신청 방법： 온라인 접수\n기관 포털\n'
                   '※ 지원 금액 : 월 3만원\n교통비 2만원\n문의처: 복지정책과\n02-1234-5678')
        self.assertEqual(numbers(content), {1, 2, 3, 4})
        self.assertEqual([item['kind'] for item in coverage_candidates(content)],
                         ['application', 'application', 'benefit', 'benefit'])

    def test_standalone_critical_facts_detected_without_headings(self):
        cases = (
            ('기초생활수급자 제외', 'exclusion'),
            ('임대차계약서 사본 1부', 'documents'),
            ('매년 재신청', 'continuation'),
            ('복지로 온라인 신청 또는 방문 접수', 'application'),
            ('중위소득 120% 이하', 'eligibility'),
            ('교통비 월 3만원', 'benefit'),
            ('지원금 1,000원 지급', 'benefit'),
        )
        for content, kind in cases:
            with self.subTest(content=content):
                self.assertEqual(coverage_candidates(content), [{'line_number': 1, 'kind': kind}])

    def test_unrelated_text_not_blindly_required_and_source_indexes_not_compacted(self):
        content = '기관 소개\n\n환영합니다\n신청기간\n\n2026. 5. 4. ~ 2026. 5. 20.\n문의처\n1234-5678'
        self.assertEqual(numbers(content), {4, 6})
        self.assertEqual(coverage_candidates('기관 소개\n환영합니다\n02-1234-5678'), [])

    def test_metadata_contains_no_source_text_is_stable_and_does_not_truncate(self):
        content = '제출서류\n' + '\n'.join('서류 항목 ' + str(i) for i in range(300))
        before = copy.deepcopy(content)
        result = coverage_candidates(content)
        self.assertEqual(content, before)
        self.assertEqual(result, coverage_candidates(content))
        self.assertEqual(len(result), 301)
        self.assertEqual(result[-1]['line_number'], 301)
        for item in result:
            self.assertEqual(set(item), {'line_number', 'kind'})
            self.assertIs(type(item['line_number']), int)
            self.assertIn(item['kind'], COVERAGE_KINDS)
        self.assertNotIn('서류 항목', json.dumps(result, ensure_ascii=False))

    def test_crlf_empty_and_invalid_inputs(self):
        self.assertEqual(coverage_candidates('지원내용\r\n\r\n월 3만원'),
                         [{'line_number': 1, 'kind': 'benefit'}, {'line_number': 3, 'kind': 'benefit'}])
        for value in ('', '\n\n', None, [], {}):
            with self.subTest(value=value):
                self.assertEqual(coverage_candidates(value), [])

    def test_frozen_official_sources_keep_bare_rows_without_mutating_fixtures(self):
        root = Path(__file__).resolve().parents[2] / 'artifacts/ai-fresh-eval-20260910/fixtures'
        cases = (
            ('policy-busan-moving.txt', '6c1888e46322d628107823fcbd9769f7c729a6bc5afce667cd7321f316444621', (
                '주택 소유자(분양권, 입주권 포함)',
                '부모 소유 주택에 임차하는 경우(배우자의 부모 소유 주택 포함)',
                '① 입실확인서 ② 임대사업자등록증 사본',
                '공공마이데이터(조회)',
                '부산청년플랫폼 온라인 접수',
            )),
            ('policy-busan-saving.txt', '6933628ac88edec28167f99b2d299fda25a06a1da8b90496bff90bb14338fba2', (
                '2026. 5. 4.(월) ~ 2026. 5. 20.(수)',
                '(온라인 신청) 복지로',
                '(방문 신청) 거주지 관할 구·군 내 읍면동 행정복지센터',
                '(지자체지원금) 지자체 자체사업 보조금 등 추가 매칭 기능',
                '(기타 지원금) 민간 등과 협약을 통해 추가지원 금액',
            )),
        )
        for name, digest, expected in cases:
            with self.subTest(name=name):
                path = root / name
                data = path.read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(), digest)
                source = data.decode('utf-8')
                lines = source.splitlines()
                required = numbers(source)
                for line in expected:
                    self.assertIn(lines.index(line) + 1, required)
                self.assertEqual(missing(source, [(1, len(lines))]), set())
                self.assertEqual(path.read_bytes(), data)
