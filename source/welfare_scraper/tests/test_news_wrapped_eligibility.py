"""Offline literal-source display for line-wrapped eligibility, not paraphrases."""
import copy
import hashlib
import json
from pathlib import Path
import unittest

from services.news_analysis_safety import assess
from services.news_field_presentation import verified_labeled_display
from services.news_profile import NewsProfile


class WrappedEligibilityTests(unittest.TestCase):
    profile = NewsProfile('wrapped-offline', 1, '표시 검증', '전국')
    quote = '본 수업은 교과연계수업으로,\n초등학교 3-6학년 학급단체만 신청가능\n합니다.'
    value = '본 수업은 교과연계수업으로, 초등학교 3-6학년 학급단체만 신청가능합니다.'

    def display(self, quote, value, content=None, field='eligibility_detail'):
        return verified_labeled_display(field, value, quote, quote if content is None else content)

    def analyze(self, quote, value, extra='', **changes):
        source = '개인이 신청합니다.\n' + quote + extra
        data = dict(is_relevant=True, category='복지', target='개인', residence=[], life_cycle=[],
            target_characteristics=[], interest_topics=[], start_age_months=None, end_age_months=None,
            life_cycle_evidence=[], residence_evidence=[], evidence_snippet='',
            applicant_type='individual', applicant_evidence='개인이 신청합니다.', applicant_detail='개인',
            beneficiary_detail='개인', support_detail='', eligibility_detail=value, contact='', deadline='',
            fact_evidence={'eligibility_detail': quote})
        data.update(changes)
        before = copy.deepcopy(data)
        decision = assess(self.profile, data, source)
        self.assertEqual(data, before)
        return decision, source

    def test_exact_source_is_returned_with_all_original_line_breaks(self):
        for quote, value in (
                (self.quote, self.value),
                (self.quote.replace('\n', '\r\n'), self.value),
                ('참여자는 성인\n개인입니다.', '참여자는 성인 개인입니다.'),
                ('신청가능\n합니다.', '신청가능합니다.'),
                ('신청가능\n합니다', '신청가능합니다'),
                ('접수가능\n합니다。', '접수가능합니다。'),
                ('신청가능\n됩니다.', '신청가능됩니다.'),
                ('신청대상\n입니다.', '신청대상입니다.')):
            with self.subTest(quote=quote):
                self.assertEqual(self.display(quote, value), quote)

    def test_altered_numbers_words_order_or_exceptions_are_not_repaired(self):
        for value in (self.value.replace('3-6', '1-6'), self.value.replace('학급단체만', '개인도'),
                      self.value.replace('학급단체만 ', ''), self.value.replace('수업은', '교육은'),
                      '학급단체만 초등학교 3-6학년 신청가능합니다.', self.value + ' 무료입니다.'):
            with self.subTest(value=value):
                self.assertIsNone(self.display(self.quote, value))
        self.assertIsNone(self.display(self.quote + '\n중복 신청 불가', self.value))

    def test_only_single_newline_and_exact_sentence_ending_seam_are_permitted(self):
        for quote, value in (
                ('신청가능\n\n합니다.', '신청가능합니다.'),
                ('신청가능 \n합니다.', '신청가능합니다.'),
                ('신청가능\v합니다.', '신청가능합니다.'),
                ('신청가능\f합니다.', '신청가능합니다.'),
                ('신청가능\u2028합니다.', '신청가능합니다.'),
                ('신청가능\u2029합니다.', '신청가능합니다.'),
                ('신청가능\n합니다만', '신청가능합니다만'),
                ('신청가능\n됩니다만', '신청가능됩니다만'),
                ('신청대상\n입니다만', '신청대상입니다만'),
                ('신청가능\n한다.', '신청가능한다.'),
                ('신청가능\n됩니다라고', '신청가능됩니다라고'),
                ('모집 인원 1\n2명', '모집 인원 12명'),
                ('신청\n불가', '신청불가')):
            with self.subTest(quote=quote):
                self.assertIsNone(self.display(quote, value))

    def test_reconstructed_or_nonliteral_evidence_does_not_use_wrap_path(self):
        self.assertIsNone(self.display(self.quote, self.value, self.quote.replace('\n', ' ')))
        self.assertIsNone(self.display(self.quote, self.value, '참가대상: 성인'))

    def test_adjacent_negative_context_still_blocks_display(self):
        quote, value = '성인 개인만\n신청가능합니다.', '성인 개인만 신청가능합니다.'
        for source in ('제외 대상:\n' + quote, quote + '\n아님', quote + '\n신청 불가'):
            with self.subTest(source=source):
                self.assertIsNone(self.display(quote, value, source))

    def test_other_fields_do_not_gain_wrapped_prose_equivalence(self):
        for field in ('support_detail', 'contact', 'deadline'):
            with self.subTest(field=field):
                self.assertIsNone(self.display(self.quote, self.value, field=field))

    def test_assessment_is_idempotent_and_does_not_infer_age(self):
        quote, value = '성인 개인이\n신청가능합니다.', '성인 개인이 신청가능합니다.'
        result, source = self.analyze(quote, value)
        self.assertEqual(result['analysis']['eligibility_detail'], quote)
        self.assertNotIn('eligibility_detail', result['analysis']['unverified_facts'])
        self.assertIsNone(result['analysis']['start_age_months'])
        self.assertIsNone(result['analysis']['end_age_months'])
        self.assertEqual(assess(self.profile, result['analysis'], source), result)

    def test_prior_withheld_empty_value_is_never_restored(self):
        quote, value = '성인 개인이\n신청가능합니다.', '성인 개인이 신청가능합니다.'
        result, _ = self.analyze(quote, '', unverified_facts={'eligibility_detail': value})
        self.assertEqual(result['analysis']['eligibility_detail'], '')
        self.assertEqual(result['target_status'], 'review')
        self.assertEqual(result['analysis']['unverified_facts']['eligibility_detail'], value)

    def test_condition_omitted_outside_quote_still_prevents_approval(self):
        quote, value = '성인 개인이\n신청가능합니다.', '성인 개인이 신청가능합니다.'
        result, _ = self.analyze(quote, value, '\n안내\n중복 지원 불가')
        self.assertFalse(result['accepted'])
        self.assertIn('omitted_conditions', result['analysis']['unverified_facts'])

    def test_actual_frozen_memorial_quote_replays_without_changing_old_ledger(self):
        root = Path(__file__).resolve().parents[2] / 'artifacts'
        source = (root / 'ai-holdout-prep-20260913/sources/news-memorial-school.txt').read_bytes()
        ledger = (root / 'ai-retest-20260914/fresh-live-01/ledger.jsonl').read_bytes()
        self.assertEqual(hashlib.sha256(source).hexdigest(),
            'fabf744aa848e01ab473c4eb341c85367fb1dd89219f929bb4fd397d95e1506e')
        self.assertEqual(hashlib.sha256(ledger).hexdigest(),
            'ea955497e7521b89a092350b91b3094a477a3e336f34a88491af9e5a5e68809b')
        rows = [json.loads(line) for line in ledger.decode('utf-8').splitlines()]
        raw = json.loads(next(row for row in rows if row.get('event') == 'response'
            and row.get('case_id') == 'news-memorial-school')['raw']['text'])
        old = next(row for row in rows if row.get('event') == 'case_result'
            and row.get('case_id') == 'news-memorial-school')
        before = copy.deepcopy(raw)
        quote = raw['fact_evidence']['eligibility_detail']
        self.assertEqual(self.display(quote, raw['eligibility_detail'], source.decode('utf-8')), quote)
        from services.news_source_selection import materialize
        content = source.decode('utf-8')
        captured = dict(content=content, source_page=copy.deepcopy(old['source_page']))
        result = assess(self.profile, materialize(raw, content, extended=True), content,
            whole_document_rules=True, source_payload=captured)
        self.assertEqual(result['analysis']['eligibility_detail'], quote)
        self.assertNotIn('eligibility_detail', result['analysis']['validation_diagnostics']['field_holds'])
        self.assertEqual(result['target_status'], 'review')
        self.assertEqual(result['target_checks']['applicant'], 'review')
        self.assertFalse(result['accepted'])
        self.assertEqual(result['analysis']['source_page']['status'], 'review_required')
        self.assertEqual(result['analysis']['support_detail'], content)
        reassessed = assess(self.profile, result['analysis'], content,
            whole_document_rules=True, source_payload=captured)
        # Existing lifecycle grounding emits a first-pass sentence as well as
        # the aggregate hold note. Once its tag is blank, only that redundant
        # sentence disappears. All data, diagnostics and decisions stay exact.
        redundant = ('원문에서 명시적 근거를 확인하지 못한 생애주기 분류는 판정에서 제외했습니다. '
                     '원문을 확인해주세요. ')
        self.assertEqual(result['analysis']['grounding_note'].count(redundant), 1)
        expected = copy.deepcopy(result)
        expected['analysis']['grounding_note'] = expected['analysis']['grounding_note'].replace(redundant, '', 1)
        self.assertEqual(reassessed, expected)
        self.assertEqual(assess(self.profile, reassessed['analysis'], content,
            whole_document_rules=True, source_payload=captured), reassessed)
        # Revalidating an old held result still may not resurrect its blank.
        stored = copy.deepcopy(old['decision']['analysis'])
        self.assertEqual(stored['eligibility_detail'], '')
        repeated = assess(self.profile, stored, content,
            whole_document_rules=True, source_payload=captured)
        self.assertEqual(repeated['analysis']['eligibility_detail'], '')
        self.assertEqual(repeated['target_status'], 'review')
        self.assertEqual(raw, before)


if __name__ == '__main__':
    unittest.main()
