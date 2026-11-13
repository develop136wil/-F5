import unittest
from services.news_profile import NewsProfile
from services.news_region_rules import normalize_region, residence_quote_matches
from services.news_target_rules import evaluate_target


class RegionRulesTests(unittest.TestCase):
    def test_non_eligibility_and_empty_names_are_not_residents(self):
        for value, quote in [('', '거주자'), ('서울특별시 도봉구', '도봉구 주민센터 방문'),
                             ('서울특별시 도봉구', '도봉구 주민자치회 모집'),
                             ('서울특별시 도봉구', '도봉구 거주자 제외'),
                             ('서울특별시 도봉구', '도봉구 거주 불필요'),
                             ('서울특별시 도봉구', '도봉구 거주자 통계')]:
            self.assertFalse(residence_quote_matches(value, quote), (value, quote))
    def test_reviewed_resident_aliases_only(self):
        for quote in ('도봉구에 거주하는 가족', '도봉구민 만 참여가능', '서울시 도봉구 거주자', '서울 도봉구 주민'):
            self.assertTrue(residence_quote_matches('서울특별시 도봉구', quote), quote)
        for quote in ('부산 도봉구 거주자', '신도봉구 거주자', '도봉구보건소 방문 신청'):
            self.assertFalse(residence_quote_matches('서울특별시 도봉구', quote), quote)

    def test_ambiguous_districts_do_not_gain_a_parent(self):
        for value, quote in [('서울특별시 중구', '중구 거주자'), ('인천광역시 서구', '서구 주민'),
                             ('서울특별시 중구', '부산 중구 거주자'), ('광주광역시', '경기도 광주시 거주자')]:
            self.assertFalse(residence_quote_matches(value, quote), (value, quote))
        self.assertTrue(residence_quote_matches('서울특별시 중구', '서울시 중구 거주자'))
    def decision(self, requested, extracted):
        return evaluate_target(NewsProfile('p', 1, '지역', requested),
                               {'residence': [extracted]}).status

    def test_explicit_alias_and_spacing_match(self):
        for value in ('서울 도봉구', '서울시 도봉구', ' 서울특별시   도봉구 '):
            self.assertEqual(self.decision('서울특별시 도봉구', value), 'match')
        self.assertEqual(self.decision('전북특별자치도', '전라북도'), 'match')

    def test_never_infer_district_parent_or_publisher_location(self):
        for value in ('도봉구', '서울', '서울 강북구', '부산 중구', ''):
            self.assertEqual(self.decision('서울 도봉구', value), 'review')
        self.assertEqual(self.decision('서울 중구', '부산 중구'), 'review')
        self.assertEqual(self.decision('광주광역시', '경기도 광주시'), 'review')
        self.assertEqual(self.decision('광주광역시', '광주시'), 'review')

    def test_no_partial_substitution_or_eligibility_inference(self):
        self.assertEqual(normalize_region('서울시립병원'), '서울시립병원')
        self.assertEqual(self.decision('서울 도봉구', '서울 도봉구 거주자 제외'), 'review')
        self.assertEqual(self.decision('서울 도봉구', '전국'), 'match')
