"""Offline adversarial examples: queued candidate != verified eligible result."""
import unittest
from services.news_profile import NewsProfile
from services.news_analysis_context import NewsAnalysisContext


class BodyRoutingTests(unittest.TestCase):
    def setUp(self):
        self.context=NewsAnalysisContext(NewsProfile('p',1,'시험','전국',
            life_cycles=('아동',),characteristics=('장애인',)))

    def route(self, body):
        return self.context.route_candidate('참여자 모집',body)[0]

    def test_same_statement_required_for_cross_group_match(self):
        self.assertEqual(self.route('모집대상: 장애인 아동'), 'matched')
        self.assertEqual(self.route('모집대상: 아동\n다른 프로그램\n모집대상: 장애인'), 'semantic_candidate')

    def test_alternative_audiences_are_not_a_combined_target(self):
        for body in ('모집대상: 아동 또는 장애인','신청대상: 장애인 혹은 아동'):
            self.assertEqual(self.route(body),'semantic_candidate')

    def test_truncated_exclusion_is_not_used(self):
        prefix='x\n' * 5990
        self.assertEqual(self.route(prefix+'모집대상: 장애인 아동'+ ' '*30+'제외'), 'semantic_candidate')

    def test_offline_candidate_growth_is_bounded_to_clear_example(self):
        bodies=['모집대상: 장애인 아동','모집대상: 아동','메뉴: 장애인 아동',
                '모집대상: 장애인 아동 제외','모집대상: 장애인\n모집대상: 아동',
                '모집대상: 장애인 또는 아동','모집대상: 노년',
                '신청대상:\n\n장애인 아동']
        routes=[self.route(body) for body in bodies]
        self.assertEqual(routes.count('matched'),1)
        self.assertEqual(routes.count('semantic_candidate'),7)
