"""Offline safety regression: all labelled errors must be rejected.

The final tuple field retains the pre-fix decision for before/after comparison.
These synthetic examples do not measure real model accuracy.
"""
import os
import sys
import unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services.policy_evidence import validate_policy_evidence

SOURCE = '등록장애인이 탑승한 차량에 적용. 일반주차장 1시간 면제 후 50% 할인. 환승주차장 3시간 면제 후 80% 할인.'
BENEFITS = '일반주차장 1시간 면제 후 50% 할인. 환승주차장 3시간 면제 후 80% 할인.'
# name, source, proposal, quote, semantically acceptable, current expected decision
CASES = [
    ('원문 유지', SOURCE, SOURCE, SOURCE, True, True),
    ('공백 변경', SOURCE, SOURCE.replace('50%', '50 %'), SOURCE, True, True),
    ('표현 변경', SOURCE, SOURCE.replace('할인', '감면'), SOURCE, True, True),
    ('퍼센트 표기', SOURCE, SOURCE.replace('%', '퍼센트'), SOURCE, True, True),
    ('순서 변경', SOURCE, '환승주차장 3시간 면제 후 80% 할인. 일반주차장 1시간 면제 후 50% 할인. 등록장애인이 탑승한 차량에 적용.', SOURCE, True, True),
    ('금액 단위 환산', '지원금 1만원 지급', '지원금 10000원 지급', '지원금 1만원 지급', True, True),
    ('시간 단위 환산', '주차 1시간 면제', '주차 60분 면제', '주차 1시간 면제', True, True),
    ('할인율 변조', SOURCE, SOURCE.replace('80%', '90%'), SOURCE, False, False),
    ('면제 시간 누락', SOURCE, SOURCE.replace('3시간 면제 후 ', ''), SOURCE, False, False),
    ('단위 변조', SOURCE, SOURCE.replace('50%', '50원'), SOURCE, False, False),
    ('가짜 근거', SOURCE, '박물관 무료 입장', '박물관 무료 입장', False, False),
    ('탑승 조건 누락', SOURCE, BENEFITS, SOURCE, False, True),
    ('할인율 뒤바뀜', SOURCE, SOURCE.replace('50%', 'TEMP').replace('80%', '50%').replace('TEMP', '80%'), SOURCE, False, False),
    ('부분 인용으로 조건 제외', SOURCE, BENEFITS, BENEFITS, False, True),
    ('조건 반전', SOURCE, SOURCE.replace('탑승한', '탑승하지 않은'), SOURCE, False, True),
    ('전체 설명 빈값', SOURCE, '', '', False, False),
]

# Deliberately synthetic, non-parking cases. A passing characterization test
# records limitations; it must never be reported as AI accuracy.
CASES += [
    ('보육 정상', '보육료 월 10만원 지급. 보호자 신청 필요.', '보육료 월 100000원 지급. 보호자 신청 필요.', '보육료 월 10만원 지급. 보호자 신청 필요.', True, True),
    ('보육 신청 조건 누락', '보육료 월 10만원 지급. 보호자 신청 필요.', '보육료 월 10만원 지급.', '보육료 월 10만원 지급.', False, True),
    ('노인 정상', '만 65세 이상에게 교통비 월 2만원 지급.', '만 65세 이상에게 교통비 월 20000원 지급.', '만 65세 이상에게 교통비 월 2만원 지급.', True, True),
    ('노인 연령 변조', '만 65세 이상에게 교통비 월 2만원 지급.', '만 60세 이상에게 교통비 월 2만원 지급.', '만 65세 이상에게 교통비 월 2만원 지급.', False, False),
    ('한부모 정상', '한부모 가구에 교육비 지원. 중복 지원 불가.', '한부모 가구에 교육비 지원. 중복 지원 불가.', '한부모 가구에 교육비 지원. 중복 지원 불가.', True, True),
    ('한부모 제외 조건 반전', '한부모 가구에 교육비 지원. 중복 지원 불가.', '한부모 가구에 교육비 지원. 중복 지원 가능.', '한부모 가구에 교육비 지원. 중복 지원 불가.', False, True),
    ('장애인 정상', '장애인 보조기기 대여. 방문 신청 필요.', '장애인 보조기기 대여. 방문 신청 필요.', '장애인 보조기기 대여. 방문 신청 필요.', True, True),
    ('장애인 신청 방식 변조', '장애인 보조기기 대여. 방문 신청 필요.', '장애인 보조기기 대여. 온라인 신청 필요.', '장애인 보조기기 대여. 방문 신청 필요.', False, True),
]


def audit():
    return [(name, expected, validate_policy_evidence(
        {'support_detail': proposal, '_source_evidence': {'support_detail': quote}}, source))
        for name, source, proposal, quote, expected, _ in CASES]


class EvidenceCharacterization(unittest.TestCase):
    def test_standalone_footer_navigation_is_not_a_policy_condition(self):
        quote = '청년에게 교통비 월 2만원을 지원합니다.'
        source = '청년 교통비 지원\n' + quote + '\n민원 신청\n개인정보처리방침'
        self.assertTrue(validate_policy_evidence({'support_detail': quote,
            '_source_evidence': {'support_detail': quote}}, source))

    def test_unstructured_multiple_programmes_remain_review_not_guessed(self):
        quote = '도봉구 청년에게 교통비 월 2만원을 지원합니다.'
        source = '청년 교통비 지원\n' + quote + '\n어르신 보행보조기 지원\n만 65세 이상 구민이 신청합니다.'
        proposal = {'support_detail': quote, '_source_evidence': {'support_detail': quote}}
        self.assertFalse(validate_policy_evidence(proposal, source))
        from services.policy_source_section import select_policy_section
        structured = '# 청년 교통비 지원\n' + quote + '\n# 어르신 보행보조기 지원\n만 65세 이상 구민이 신청합니다.'
        self.assertTrue(validate_policy_evidence(proposal,
            select_policy_section(structured, '청년 교통비 지원')))

    def test_labelled_safe_behavior(self):
        for name, source, proposal, quote, expected, _previous in CASES:
            with self.subTest(case=name):
                self.assertEqual(validate_policy_evidence({'support_detail': proposal,
                    '_source_evidence': {'support_detail': quote}}, source), expected)


if __name__ == '__main__':
    rows = audit()
    for name, expected, actual in rows:
        print(f'{name}: expected={expected}, accepted={actual}')
    print('normal_accepted', sum(expected and actual for _, expected, actual in rows))
    print('false_rejected', sum(expected and not actual for _, expected, actual in rows))
    print('errors_blocked', sum(not expected and not actual for _, expected, actual in rows))
    print('errors_missed', sum(not expected and actual for _, expected, actual in rows))
