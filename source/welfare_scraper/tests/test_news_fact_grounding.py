import unittest
from services.news_fact_grounding import grounded_facts


def entry(value, quote, basis='explicit'):
    return dict(value=value, quote=quote, basis=basis)


class TestGrounding(unittest.TestCase):
    def test_scoped_context_preserves_separate_duplicate_constraint(self):
        region = '서울특별시 도봉구'
        for source in ('도봉구 거주자 대상, 중복 신청 불가.',
                       '도봉구 거주자 대상, 중복 수급자 제외.',
                       '도봉구 거주자 대상， 중복 참여 금지.'):
            for quote in ('도봉구 거주자', source):
                with self.subTest(source=source, quote=quote):
                    facts = self.facts([], [region], [], [entry(region, quote)])
                    result, note = grounded_facts(facts, source, scoped_context=True)
                    self.assertEqual(result, facts)
                    self.assertEqual(note, '')

    def test_scoped_context_matches_exact_multiline_evidence(self):
        region = '서울특별시 도봉구'
        for quote in ('지원 대상:\n도봉구 거주자', '지원 대상:\r\n도봉구 거주자'):
            facts = self.facts([], [region], [], [entry(region, quote)])
            result, note = grounded_facts(facts, quote, scoped_context=True)
            self.assertEqual(result, facts)
            self.assertEqual(note, '')

    def test_scoped_context_preserves_true_exclusions(self):
        region = '서울특별시 도봉구'
        for source in ('제외 대상: 도봉구 거주자',
                       '제외 대상:\n도봉구 거주자',
                       '제외 대상\n도봉구 거주자',
                       '다른 안내.\n  제외 대상:\n도봉구 거주자',
                       '도봉구 거주자는 지원 대상이 아닙니다.',
                       '도봉구 거주자는\n지원 대상이 아닙니다.',
                       '도봉구 거주자는\n신청할 수 없습니다.',
                       '도봉구 거주자 대상, 지원 대상이 아닙니다.',
                       '도봉구 거주자가 아닌 다른 지역 주민 대상',
                       '도봉구 거주자 제외, 중복 신청 불가.'):
            with self.subTest(source=source):
                facts = self.facts([], [region], [], [entry(region, '도봉구 거주자')])
                result, note = grounded_facts(facts, source, scoped_context=True)
                self.assertEqual(result['residence'], [])
                self.assertTrue(note)

    def test_scoped_context_does_not_assume_every_comma_is_independent(self):
        for source in ('제외 대상: 청년, 노년', '청년, 노년은 지원 대상이 아닙니다.'):
            with self.subTest(source=source):
                facts = self.facts(['노년'], [], [entry('노년', '노년')])
                result, note = grounded_facts(facts, source, scoped_context=True)
                self.assertEqual(result['life_cycle'], [])
                self.assertTrue(note)

    def test_scoped_context_preserves_separate_exclusion_line(self):
        facts = self.facts(['청년'], [], [entry('청년', '청년 대상')])
        result, note = grounded_facts(facts, '청년 대상\n중복 수급자 제외', scoped_context=True)
        self.assertEqual(result, facts)
        self.assertEqual(note, '')

    def test_v9_default_keeps_released_context_behavior(self):
        region = '서울특별시 도봉구'
        for quote, source in (
            ('도봉구 거주자', '도봉구 거주자 대상, 중복 신청 불가.'),
            ('지원 대상:\n도봉구 거주자', '지원 대상:\n도봉구 거주자'),
        ):
            facts = self.facts([], [region], [], [entry(region, quote)])
            result, note = grounded_facts(facts, source)
            self.assertEqual(result['residence'], [])
            self.assertTrue(note)

    def test_negative_context_cannot_be_removed_from_quote(self):
        cases = [
            ('도봉구 거주자', '제외 대상: 도봉구 거주자', '서울특별시 도봉구'),
            ('도봉구 거주자', '도봉구 거주자는 지원 대상이 아닙니다.', '서울특별시 도봉구'),
            ('거주 무관', '거주 무관 아님', '거주무관'),
            ('전국 대상', '전국 대상이 아닙니다.', '전국'),
        ]
        for quote, source, region in cases:
            with self.subTest(source=source):
                facts = self.facts([], [region], [], [entry(region, quote)])
                result, note = grounded_facts(facts, source)
                self.assertEqual(result['residence'], [])
                self.assertTrue(note)

    def test_lifecycle_exclusion_context(self):
        for source in ('제외 대상: 청년', '청년은 지원 대상이 아닙니다.', '청년은 신청할 수 없습니다. 지원하지 않습니다.'):
            facts = self.facts(['청년'], [], [entry('청년', '청년')])
            result, note = grounded_facts(facts, source)
            self.assertEqual(result['life_cycle'], [])
            self.assertTrue(note)

    def test_separate_exclusion_line_preserves_positive_evidence(self):
        facts = self.facts([], ['서울특별시 도봉구'], [], [entry('서울특별시 도봉구', '도봉구 거주자')])
        for source in ('지원 대상: 도봉구 거주자\n중복 수급자 제외',
                       '도봉구 거주자 신청 가능. 중복 수급자 제외.'):
            result, note = grounded_facts(facts, source)
            self.assertEqual(result, facts)
            self.assertEqual(note, '')

    def test_v8_compatibility_mode_preserves_old_result(self):
        facts = self.facts([], ['서울특별시 도봉구'], [], [entry('서울특별시 도봉구', '도봉구 거주자')])
        result, note = grounded_facts(facts, '제외 대상: 도봉구 거주자', strict_context=False)
        self.assertEqual(result, facts)
        self.assertEqual(note, '')

    def test_shortened_quote_cannot_hide_exclusion(self):
        facts = self.facts([], ['서울특별시 도봉구'], [],
            [entry('서울특별시 도봉구', '도봉구 거주자')])
        for source in ('도봉구 거주자 제외', '도봉구 거주자가 아닌 다른 지역 주민 대상'):
            result, note = grounded_facts(facts, source)
            self.assertEqual(result['residence'], [])
            self.assertTrue(note)
    def facts(self, cycles=(), regions=(), cycle_evidence=(), region_evidence=()):
        return dict(life_cycle=list(cycles), residence=list(regions),
                    life_cycle_evidence=list(cycle_evidence), residence_evidence=list(region_evidence))

    def test_parent_does_not_prove_senior_even_if_model_says_explicit(self):
        facts = self.facts(['학령기', '노년'], [],
            [entry('학령기', '신체활동이 가능한 아동'), entry('노년', '리더 부모님 및 보호자')])
        result, note = grounded_facts(facts, '리더 부모님 및 보호자. 신체활동이 가능한 아동')
        self.assertEqual(result['life_cycle'], ['학령기'])
        self.assertIn('생애주기', note)
        self.assertEqual(facts['life_cycle'], ['학령기', '노년'])

    def test_office_location_does_not_prove_residence(self):
        for basis in ('explicit', 'publisher', 'inferred'):
            facts = self.facts([], ['서울특별시 도봉구'], [],
                [entry('서울특별시 도봉구', '서울특별시 도봉구 보건소 방문 신청', basis)])
            result, note = grounded_facts(facts, '서울특별시 도봉구 보건소 방문 신청')
            self.assertEqual(result['residence'], [])
            self.assertIn('거주 조건', note)

    def test_explicit_residents_and_senior_alias(self):
        quote = '서울특별시 도봉구 거주 어르신 대상'
        facts = self.facts(['노년'], ['서울특별시 도봉구'],
            [entry('노년', quote)], [entry('서울특별시 도봉구', quote)])
        result, note = grounded_facts(facts, quote)
        self.assertEqual(result, facts)
        self.assertEqual(note, '')

    def test_fabrication_exclusion_and_age_inference_stay_unconfirmed(self):
        for quote, source in [('노년 대상', '청년 대상'), ('노년 제외', '노년 제외'), ('만 65세 이상', '만 65세 이상')]:
            result, note = grounded_facts(self.facts(['노년'], [], [entry('노년', quote)]), source)
            self.assertEqual(result['life_cycle'], [])
            self.assertTrue(note)

    def test_no_restrictions_does_not_invent_tags(self):
        result, note = grounded_facts(self.facts(), '지역·나이 안내 없음')
        self.assertEqual(result['life_cycle'], [])
        self.assertEqual(note, '')

    def test_malformed_grounding_fails(self):
        facts = self.facts()
        facts['residence_evidence'] = 'not a list'
        with self.assertRaises(ValueError):
            grounded_facts(facts, '')
