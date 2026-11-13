"""Offline residence grammar/request regressions, not a new AI quality score.

Run through the isolated audit runner. The frozen public source is read-only;
no actual response is repaired and no SDK, operating DB or network is used.
"""
import copy
import hashlib
import json
from pathlib import Path
import unittest

from services.news_region_rules import residence_quote_matches
from services.policy_evidence import structured_policy_issues
from services.policy_source_plan import (
    build_policy_extraction_request, policy_response_matches_schema,
)


JEONGEUP = '신청일 기준 정읍시에 1년 이상 주민등록상 거주한 입영예정자'
DOBONG = '신청일 기준 도봉구에 6개월 이상 주민등록을 두고 거주하는 청년'


def request(source, labels):
    options = {'category': ['생활지원'], 'target_characteristics': ['일반'],
               'residence': list(labels), 'life_cycle': []}
    before = copy.deepcopy(options)
    prompt, schema = build_policy_extraction_request(
        source, '거주 조건 확인', 'https://example.invalid/policy', '공공기관', options)
    assert options == before
    return prompt, schema['properties']['residence']


class RegisteredResidenceGrammar(unittest.TestCase):
    def test_explicit_duration_registration_predicates(self):
        for value, quote in (
                ('정읍시', JEONGEUP), ('도봉구', DOBONG),
                ('서울특별시 도봉구', DOBONG),
                ('서울특별시 도봉구', DOBONG.replace('도봉구', '서울시 도봉구')),
                ('정읍시', JEONGEUP.replace('1년', '12개월')),
                ('정읍시', JEONGEUP.replace('거주한 입영예정자', '거주하는 한부모 가구')),
                ('도봉구', DOBONG.replace('거주하는', '거주하시는')),
                ('정읍시', JEONGEUP.replace('1년 이상', '1 년\t이상'))):
            with self.subTest(value=value, quote=quote):
                self.assertTrue(residence_quote_matches(value, quote))

    def test_no_new_geographic_hierarchy_or_substring_matches(self):
        for value, quote in (
                ('전북특별자치도', JEONGEUP), ('서울특별시', DOBONG),
                ('서울특별시 도봉구', DOBONG.replace('도봉구', '부산 도봉구')),
                ('서울특별시 중구', DOBONG.replace('도봉구', '중구')),
                ('서울특별시 중구', DOBONG.replace('도봉구', '부산 중구')),
                ('인천광역시 중구', DOBONG.replace('도봉구', '서울시 중구')),
                ('광주광역시', JEONGEUP.replace('정읍시', '경기도 광주시')),
                ('정읍시', JEONGEUP.replace('정읍시', '신정읍시')),
                ('정읍시', JEONGEUP.replace('정읍시', 'A정읍시')),
                ('정읍시', JEONGEUP.replace('정읍시', '1정읍시')),
                ('정읍시', JEONGEUP.replace('정읍시', '정읍시청'))):
            with self.subTest(value=value, quote=quote):
                self.assertFalse(residence_quote_matches(value, quote))
        self.assertTrue(residence_quote_matches('서울특별시 중구',
            DOBONG.replace('도봉구', '서울시 중구')))

    def test_new_path_does_not_promote_places_statistics_proxies_or_negatives(self):
        for quote in (
                '기관 주소: ' + JEONGEUP, '행사 장소: ' + JEONGEUP,
                '통계 조사: ' + JEONGEUP, '현황: ' + JEONGEUP,
                '안내 예시: ' + JEONGEUP, '제공기관: ' + JEONGEUP,
                '신청 보호자는 ' + JEONGEUP, JEONGEUP + '의 보호자가 신청합니다',
                '부모는 ' + JEONGEUP, '대리인은 ' + JEONGEUP,
                JEONGEUP + '는 제외', JEONGEUP + '는 지원 불가',
                JEONGEUP + '가 아닌 시민', JEONGEUP + '가 아닙니다',
                JEONGEUP + '는 신청하지 않습니다', JEONGEUP + '만 있는 것은 아니며',
                JEONGEUP + ' 또는 비거주자', JEONGEUP + ' 또는 거주 무관',
                JEONGEUP + '는 미지원', JEONGEUP + '는 신청 금지',
                JEONGEUP + '는 신청 안됩니다',
                JEONGEUP.replace('입영예정자', '후 부산으로 이사한 입영예정자'),
                JEONGEUP.replace('입영예정자', '후 타지역으로 전출한 입영예정자'),
                JEONGEUP + ' 우선 지원', JEONGEUP + ' 예외',
                JEONGEUP.replace('거주한', '비거주한'),
                JEONGEUP.replace('거주한', '거주하지 않은')):
            with self.subTest(quote=quote):
                self.assertFalse(residence_quote_matches('정읍시', quote))

    def test_registration_path_has_no_arbitrary_connector_or_line_join(self):
        for quote in (
                JEONGEUP.replace('1년', '0년'), JEONGEUP.replace('1년', '1000년'),
                JEONGEUP.replace('1년', '-1년'), JEONGEUP.replace('1년', '1.5년'),
                JEONGEUP.replace('이상', '미만'), JEONGEUP.replace('이상', '이하'),
                JEONGEUP.replace('주민등록상', '사업자등록상'),
                JEONGEUP.replace('주민등록상', '주소지를 두었으나 현재 타지역에'),
                JEONGEUP.replace('1년 이상', '1년 이상 근무한 뒤'),
                JEONGEUP.replace('이상 주민등록상', '이상\n주민등록상'),
                JEONGEUP.replace('거주한 입영예정자', '거주한다면 지원을 검토'),
                JEONGEUP + (' ' * 2000)):
            with self.subTest(quote=quote[:160]):
                self.assertFalse(residence_quote_matches('정읍시', quote))

    def test_old_direct_resident_forms_still_work(self):
        for quote in ('도봉구에 거주하는 가족', '도봉구민 만 참여가능',
                      '서울시 도봉구 거주자', '서울 도봉구 주민'):
            self.assertTrue(residence_quote_matches('서울특별시 도봉구', quote), quote)


class PolicyResidenceRequest(unittest.TestCase):
    def test_explicit_source_restores_exact_options_without_forcing_a_value(self):
        for source, labels, expected in (
                (JEONGEUP, ['정읍시', '전북특별자치도', '전국'], ['정읍시']),
                (DOBONG, ['도봉구', '서울특별시 도봉구', '서울특별시', '전국'],
                 ['도봉구', '서울특별시 도봉구'])):
            with self.subTest(source=source):
                prompt, spec = request(source, labels)
                self.assertEqual(spec, {'type': 'array', 'items':
                    {'type': 'string', 'enum': expected}, 'maxItems': len(expected)})
                self.assertIn('"residence":' + json.dumps(expected, ensure_ascii=False,
                    separators=(',', ':')), prompt)
                self.assertNotIn('residence', structured_policy_issues(
                    {'residence': expected}, source))
                # A permitted option is not a model prediction. Unknown []
                # still remains unknown; no response/cache is filled in.
                self.assertTrue(policy_response_matches_schema([], spec))
                self.assertTrue(policy_response_matches_schema(expected, spec))
                self.assertFalse(policy_response_matches_schema(['전국'], spec))

    def test_existing_source_context_guards_still_remove_the_option(self):
        for source in (
                '지원 제외 대상:\n' + JEONGEUP,
                JEONGEUP + '\n지원 대상이 아닙니다',
                '기관 주소: ' + JEONGEUP,
                '신청 보호자는 ' + JEONGEUP,
                '통계: ' + JEONGEUP,
                JEONGEUP + '는 제외합니다'):
            with self.subTest(source=source):
                _, spec = request(source, ['정읍시'])
                self.assertEqual(spec, {'type': 'array', 'items': {'type': 'string'},
                                       'maxItems': 0})
                self.assertIn('residence', structured_policy_issues(
                    {'residence': ['정읍시']}, source))

    def test_frozen_public_source_request_is_fixed_not_the_actual_model_response(self):
        path = (Path(__file__).resolve().parents[2] / 'artifacts' /
                'ai-acceptance-live-20260914/sources-agent/policy-jeongeup-enlistment.txt')
        raw = path.read_bytes()
        expected_sha = '0fa98a36616fa73910ac52fbabae5ae00e6fcb2a10e708dc82d4bb1a1fa7f521'
        self.assertEqual(hashlib.sha256(raw).hexdigest(), expected_sha)
        source = raw.decode('utf-8')
        self.assertIn(JEONGEUP, source)
        labels = ['전국', '서울시', '도봉구', '부산광역시', '기장군', '정읍시', '서대문구']
        _, spec = request(source, labels)
        self.assertEqual(spec, {'type': 'array', 'items':
            {'type': 'string', 'enum': ['정읍시']}, 'maxItems': 1})
        self.assertEqual(hashlib.sha256(source.encode('utf-8')).hexdigest(), expected_sha)
        self.assertEqual(path.read_bytes(), raw)
