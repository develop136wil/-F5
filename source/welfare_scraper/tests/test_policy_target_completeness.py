"""Offline recipient-tag omission checks, not measurements of model accuracy."""
import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from services.policy_target_completeness import target_evidence_candidates, missing_target_groups


ALL = {'target_characteristics': ['일반', '장애/발달지연', '장애인', '저소득',
                                 '다자녀', '한부모', '한부모·조손']}


class PolicyTargetCompleteness(unittest.TestCase):
    def test_frozen_goesan_source_order_and_original_response_missing_disability(self):
        root = Path(__file__).resolve().parents[2] / 'artifacts'
        source_bytes = (root / 'ai-holdout-prep-20260913/sources/policy-goesan-diapers.txt').read_bytes()
        ledger_bytes = (root / 'ai-holdout-live-20260913/fresh-live-01/ledger.jsonl').read_bytes()
        self.assertEqual(hashlib.sha256(source_bytes).hexdigest(),
                         '03473b6a889c4185d9f7d92f49514eb4e36006203a6c8530884212cfe47ebb48')
        self.assertEqual(hashlib.sha256(ledger_bytes).hexdigest(),
                         '91e4f35f18f6d29c5cdab391e6e4cc53d485f2ccdae4eb3ce8ebc725e6e1b9a8')
        records = [json.loads(line) for line in ledger_bytes.decode('utf-8').splitlines()]
        response = next(row for row in records
                        if row.get('case_id') == 'policy-goesan-diapers' and row.get('event') == 'response')
        raw = json.loads(response['raw']['text'])
        before = copy.deepcopy(raw)
        candidates = target_evidence_candidates(source_bytes.decode('utf-8'), ALL)
        disability = next(item for item in candidates if '장애/발달지연' in item['labels'])
        self.assertEqual(disability['line'], 5)
        self.assertEqual(disability['quote'], source_bytes.decode('utf-8').splitlines()[4])
        self.assertIn(disability, missing_target_groups(raw, candidates))
        self.assertEqual(raw, before)  # a detected omission is not a repaired/live response

    def test_explicit_household_or_branches_both_get_unchanged_line(self):
        quote = '만 2세 미만의 영아를 둔 기준중위소득 80%이하의 다자녀가구(2인 이상) 또는 장애인 가구'
        source = '기저귀 지원대상\n' + quote + '\n※ 지원기준 변경은 별도로 확인합니다.'
        candidates = target_evidence_candidates(source, ALL)
        self.assertEqual(len(candidates), 2)
        self.assertEqual({label for item in candidates for label in item['labels']},
                         {'다자녀', '장애인', '장애/발달지연'})
        self.assertTrue(all(item['quote'] == quote and item['line'] == 2 for item in candidates))
        missing = missing_target_groups({'target_characteristics': ['다자녀']}, candidates)
        self.assertEqual(len(missing), 1)
        self.assertEqual(set(missing[0]['labels']), {'장애인', '장애/발달지연'})

    def test_equivalent_connected_labels_need_only_one_not_every_alias(self):
        source = '지원 대상\n장애인 가구\n지원내용\n교통비 지원'
        candidates = target_evidence_candidates(source, ALL)
        self.assertEqual(len(candidates), 1)
        for label in ('장애인', '장애/발달지연'):
            with self.subTest(label=label):
                self.assertEqual(missing_target_groups({'target_characteristics': [label]}, candidates), [])

    def test_only_exact_current_connected_labels_are_candidates(self):
        source = '지원 대상\n장애인 가구'
        for labels in ([], ['일반'], ['기관 전용 분류'], ['장애인 가구용 새 선택지']):
            with self.subTest(labels=labels):
                self.assertEqual(target_evidence_candidates(source, {'target_characteristics': labels}), [])
        candidates = target_evidence_candidates(source, {'target_characteristics': ['장애/발달지연']})
        self.assertEqual(candidates[0]['labels'], ['장애/발달지연'])

    def test_literal_low_income_household_but_not_income_percentage_alone(self):
        for body, expected in [('저소득 가구', {'저소득'}),
                               ('기준중위소득 80% 이하 가구', set()),
                               ('저소득 가구 또는 다자녀 가구', {'저소득', '다자녀'})]:
            with self.subTest(body=body):
                candidates = target_evidence_candidates('지원대상\n' + body, ALL)
                self.assertEqual({label for item in candidates for label in item['labels']}, expected)

    def test_single_parent_is_not_mistaken_for_proxy_parent(self):
        for source in ('지원대상\n한부모 가구', '신청자격\n한부모가족 수급대상 가구'):
            with self.subTest(source=source):
                candidates = target_evidence_candidates(source, ALL)
                self.assertEqual(len(candidates), 1)
                self.assertEqual(set(candidates[0]['labels']), {'한부모', '한부모·조손'})
                self.assertEqual(missing_target_groups({'target_characteristics': ['한부모']}, candidates), [])

    def test_positive_recipient_statement_outside_heading(self):
        candidates = target_evidence_candidates('장애인 가구에게 교통비를 지원합니다.', ALL)
        self.assertEqual(len(candidates), 1)
        self.assertIn('장애인', candidates[0]['labels'])

    def test_bare_title_mention_is_not_a_requirement(self):
        for source in ('장애인 가구 사업 안내\n지원내용\n교통비 지원',
                       '저소득 가구 지원사업\n지원내용\n교통비 지원',
                       '장애인 가구', '다자녀 가구 또는 장애인 가구'):
            with self.subTest(source=source):
                self.assertEqual(target_evidence_candidates(source, ALL), [])

    def test_parent_applicant_provider_and_other_roles_are_not_required(self):
        examples = (
            '장애인 부모의 자녀', '부모가 장애인인 가구', '장애인인 보호자와 아동 가구',
            '장애인 신청자가 있는 가구', '장애인 대리인을 둔 가구',
            '장애인 가구를 지원하는 단체', '장애인 가구를 돌보는 종사자',
            '한부모 가구 대상 서비스 제공기관', '장애인 가구 대상 강사 모집',
            '장애인복지센터', '한부모가족지원법에 따른 가구',
            '장애인 가구 확인 서류', '장애인 가구의 아동을 위탁받은 가구',
        )
        for body in examples:
            with self.subTest(body=body):
                self.assertEqual(target_evidence_candidates('지원대상\n' + body, ALL), [])

    def test_nonrecipient_parent_and_subordinate_target_label_stay_masked(self):
        for heading in ('지원 제외 대상', '제공기관', '제출서류', '관련법령'):
            for child in ('대상', '지원대상', '신청자격'):
                with self.subTest(heading=heading, child=child):
                    self.assertEqual(target_evidence_candidates(
                        heading + '\n' + child + '\n장애인 가구\n지원내용\n교통비 지원', ALL), [])

    def test_negative_comparative_and_mixed_or_lines_are_not_requirements(self):
        examples = ('장애인 가구 제외', '장애인 가구가 아닌 가구', '비장애인 가구',
                    '장애인 가구보다 소득이 낮은 가구', '장애인 여부와 관계없는 가구',
                    '장애인 가구 외의 다자녀 가구', '장애인 가구는 제외 또는 다자녀 가구',
                    '다자녀 가구 및 장애인 부모를 둔 가구',
                    '장애인 가구\n지원 대상이 아닙니다.')
        for body in examples:
            with self.subTest(body=body):
                self.assertEqual(target_evidence_candidates('지원대상\n' + body, ALL), [])

    def test_detached_negative_on_either_side_survives_blank_lines(self):
        for negative in ('미지원', '미지급', '미수급', '지원하지 않음', '대상 없음', '제외'):
            for body in (f'장애인 가구\n{negative}', f'{negative}\n장애인 가구',
                         f'장애인 가구\n\n{negative}', f'{negative}\n\n장애인 가구'):
                with self.subTest(body=body):
                    self.assertEqual(target_evidence_candidates(
                        '지원대상\n' + body + '\n지원내용\n교육비 지원', ALL), [])
        self.assertEqual(len(target_evidence_candidates(
            '지원대상\n장애인 가구\n지원내용\n교육비 지원', ALL)), 1)

    def test_sibling_mention_cannot_supply_this_quotes_evidence(self):
        source = '지원대상\n장애인 가구를 지원하는 단체\n지원내용\n장애인 가구 지원 안내'
        self.assertEqual(target_evidence_candidates(source, ALL), [])

    def test_existing_guard_is_required_not_bypassed(self):
        with patch('services.policy_evidence.structured_policy_issues', return_value=['target_characteristics']):
            self.assertEqual(target_evidence_candidates('지원대상\n장애인 가구', ALL), [])

    def test_full_quote_line_and_line_numbers_keep_crlf_blank_boundaries(self):
        quote = '  다자녀 가구 또는 장애인 가구  '
        source = '지원대상\r\n\r\n' + quote + '\r\n'
        candidates = target_evidence_candidates(source, ALL)
        self.assertEqual(len(candidates), 2)
        self.assertTrue(all(item['quote'] == quote and item['line'] == 3 for item in candidates))

    def test_bounds_unknown_options_and_unsafe_input_do_not_create_requirements(self):
        for source, options in [(None, ALL), ('지원대상\n장애인 가구' + ' ' * 6000, ALL),
                                ('지원대상\n장애인 가구\x00', ALL),
                                ('\n' * 512 + '지원대상\n장애인 가구', ALL),
                                ('지원대상\n장애인 가구', None),
                                ('지원대상\n장애인 가구', {'target_characteristics': ['장애인'] * 101}),
                                ('지원대상\n장애인 가구', {'target_characteristics': [True]})]:
            with self.subTest(options=options):
                self.assertEqual(target_evidence_candidates(source, options), [])

    def test_missing_check_never_injects_or_changes_input_or_candidates(self):
        source = '지원대상\n다자녀 가구 또는 장애인 가구'
        result = {'target_characteristics': ['다자녀'], 'eligibility_detail': source}
        options = copy.deepcopy(ALL)
        candidates = target_evidence_candidates(source, options)
        before = copy.deepcopy((result, options, candidates))
        missing = missing_target_groups(result, candidates)
        self.assertEqual(len(missing), 1)
        self.assertEqual((result, options, candidates), before)
        missing[0]['labels'].append('임의 분류')
        self.assertEqual((result, options, candidates), before)
        self.assertEqual(missing_target_groups(result, []), [])


if __name__ == '__main__':
    unittest.main()
