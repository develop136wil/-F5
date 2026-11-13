"""Pure enquiry departments must not hide any substantive policy condition."""
import copy
import hashlib
import json
from pathlib import Path
import unittest

from services.policy_evidence import diagnose_policy_evidence
from services.policy_source_coverage import contact_destination_lines, contact_condition_clauses
from services.policy_source_units import source_units, hydrate_policy_source_units


def candidate(source, contact):
    return hydrate_policy_source_units(dict(_source_units=dict(
        support_detail=[unit['id'] for unit in source_units(source) if unit['required']],
        eligibility_detail=[], age_detail=[], target_income=[]), contact=contact), source)


class DepartmentContact(unittest.TestCase):
    def test_exact_department_and_phone_under_enquiry_can_live_in_contact(self):
        for line in ('장애인복지과 481-2562', '한부모가족지원팀 02-1234-5678',
                     '장애인복지과(031-481-2562)', '장애인복지과: 481-2562'):
            for separator in ('\n', ': '):
                with self.subTest(line=line, separator=separator):
                    source = '지원내용\n교육비 3만원 지원\n문의처' + separator + line
                    result = candidate(source, line)
                    before = copy.deepcopy(result)
                    self.assertTrue(diagnose_policy_evidence(result, source)['valid'])
                    self.assertEqual(result, before)
                    self.assertFalse(diagnose_policy_evidence(dict(result, contact=''), source)['valid'])
                    self.assertFalse(diagnose_policy_evidence(dict(result, contact='481-2562'), source)['valid'])

    def test_full_multiline_contact_is_not_required_to_equal_one_destination(self):
        contact = '거주지 동주민센터\n장애인복지과 481-2562'
        source = '지원내용\n교육비 3만원 지원\n문의처\n' + contact
        self.assertTrue(diagnose_policy_evidence(candidate(source, contact), source)['valid'])
        partial = candidate(source, '장애인복지과 481-2562')
        self.assertFalse(diagnose_policy_evidence(partial, source)['valid'])

    def test_no_heading_or_non_enquiry_heading_never_grants_contact_exception(self):
        for heading in ('', '지원대상', '신청방법', '담당기관', '사업목적'):
            source = (heading + '\n' if heading else '') + '장애인복지과 481-2562'
            with self.subTest(heading=heading):
                self.assertEqual(contact_destination_lines(source), {})

    def test_real_restriction_or_application_is_never_a_contact_destination(self):
        clauses = ('방문 신청만 가능', '서울 거주자만 지원 대상', '중복 지원 불가',
                   '소급 지급 불가', '증빙서류 필요', '만 2세 미만', '매년 재신청 필요',
                   '현금 지급하지 않음', '지원 중단', '접수 기간 6개월')
        for clause in clauses:
            for separator in ('. ', '\n'):
                with self.subTest(clause=clause, separator=separator):
                    line = '장애인복지과 481-2562' + separator + clause
                    source = '지원내용\n교육비 3만원 지원\n문의처\n' + line
                    proposal = dict(support_detail='교육비 3만원 지원', contact=line,
                        _source_evidence={'support_detail': '교육비 3만원 지원'})
                    self.assertFalse(diagnose_policy_evidence(proposal, source)['valid'])
                    destinations = contact_destination_lines(source)
                    self.assertNotIn(line.replace(' ', '').replace('\n', ''), destinations)

    def test_frozen_ansan_contact_false_positive_is_distinct_from_real_partial_omission(self):
        root = Path(__file__).resolve().parents[2] / 'artifacts'
        source_path = root / 'ai-holdout-prep-20260913/sources/policy-ansan-birth.txt'
        ledger_path = root / 'ai-holdout-live-20260913/fresh-live-01/ledger.jsonl'
        source_bytes, ledger_bytes = source_path.read_bytes(), ledger_path.read_bytes()
        self.assertEqual(hashlib.sha256(source_bytes).hexdigest(),
            '6bbc1bf61cec526c6167e28423555048219ab25f22c7f5c923aaaddee3068c5a')
        self.assertEqual(hashlib.sha256(ledger_bytes).hexdigest(),
            '91e4f35f18f6d29c5cdab391e6e4cc53d485f2ccdae4eb3ce8ebc725e6e1b9a8')
        rows = [json.loads(line) for line in ledger_bytes.decode('utf-8').splitlines()]
        raw = json.loads(next(row for row in rows if row['event'] == 'response'
            and row['case_id'] == 'policy-ansan-birth')['raw']['text'])
        original = copy.deepcopy(raw)
        result = hydrate_policy_source_units(raw, source_bytes.decode('utf-8'))
        diagnostic = diagnose_policy_evidence(result, source_bytes.decode('utf-8'))
        self.assertEqual(diagnostic['condition_candidates'], [])
        # This narrowly fixes diagnosis, not the known extraction omissions.
        self.assertEqual(result['contact'], '장애인복지과 481-2562')
        self.assertNotIn('각 동 행정복지센터', result['contact'])
        self.assertEqual(result['target_income'], '')
        self.assertEqual(raw, original)
        self.assertEqual(source_path.read_bytes(), source_bytes)
        self.assertEqual(ledger_path.read_bytes(), ledger_bytes)

    def test_enquiry_adjacent_age_and_application_period_require_actual_prose(self):
        for clause, kind in (('만 2세 미만', 'eligibility'), ('접수 기간 6개월', 'application')):
            with self.subTest(clause=clause):
                source = '지원내용\n교육비 3만원 지원\n문의처\n장애인복지과 481-2562\n' + clause
                classified = contact_condition_clauses(source)
                self.assertEqual(classified, {clause.replace(' ', ''): kind})
                result = dict(support_detail='지원내용\n교육비 3만원 지원',
                    eligibility_detail=clause, contact='장애인복지과 481-2562',
                    _source_evidence={'support_detail': '지원내용\n교육비 3만원 지원',
                                      'eligibility_detail': clause})
                self.assertTrue(diagnose_policy_evidence(result, source)['valid'])
                missing = copy.deepcopy(result)
                missing['eligibility_detail'] = ''
                missing['_source_evidence'].pop('eligibility_detail')
                missing['contact'] += '\n' + clause
                diagnostic = diagnose_policy_evidence(missing, source)
                self.assertFalse(diagnostic['valid'])
                self.assertIn({'code': 'source_condition_not_preserved'}, diagnostic['issues'])
