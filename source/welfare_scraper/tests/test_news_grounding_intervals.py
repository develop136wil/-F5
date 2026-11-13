"""Offline regressions for recipient gaps and omitted negative benefit conditions.

Synthetic model responses measure the local safety boundary, not model accuracy.
All integration data is in SQLite memory and all model calls are AsyncMock.
"""
import copy
import json
import sqlite3
import unittest
from unittest.mock import AsyncMock, patch

from services import news_analysis_store as jobs, news_custom_analysis as custom
from services import news_profile_store as profiles, news_result_import as imports
from services.news_analysis_safety import VERSION, assess
from services.news_analysis_worker import process_job
from services.news_profile import NewsProfile


def facts(**changes):
    return dict(is_relevant=True, category='복지', target='개인', residence=[], life_cycle=[],
        target_characteristics=[], interest_topics=[], start_age_months=None, end_age_months=None,
        evidence_snippet='', residence_evidence=[], life_cycle_evidence=[],
        applicant_type='individual', applicant_evidence='개인이 신청합니다.',
        applicant_detail='개인', beneficiary_detail='개인', support_detail='',
        eligibility_detail='', contact='', deadline='',
        fact_evidence=dict.fromkeys(('support_detail', 'eligibility_detail', 'contact',
                                     'deadline', 'is_relevant'), '')) | changes


GAP_SOURCE = '만 3~5세 또는 만 19~24세 대상. 개인이 신청합니다.'
BENEFIT = '월 10만원을 지급합니다.'
RESTRICTION = '소득이 있는 사람에게는 지급하지 않습니다.'
PAYMENT_SOURCE = f'개인이 신청합니다. {BENEFIT} {RESTRICTION}'


class TestIntervalGrounding(unittest.TestCase):
    profile = NewsProfile('p', 1, '전국 조건', '전국')

    def test_disjoint_recipient_intervals_cannot_fill_gaps_or_auto_exclude(self):
        cases = [
            (GAP_SOURCE, 36, 299),
            ('만 3~5세 대상. 만 19~24세 대상. 개인이 신청합니다.', 36, 299),
            ('생후 0~6개월 또는 생후 12~18개월 대상. 개인이 신청합니다.', 0, 18),
            ('만 5세 이하 또는 만 19세 이상 대상. 개인이 신청합니다.', None, None),
            ('만 3세 이상 만 5세 이하 또는 만 19세 이상 대상. 개인이 신청합니다.', 36, None),
        ]
        # Matching an allowed branch, falling in the gap, or having no age
        # filter cannot make a multi-interval source fit the one-interval schema.
        for source, start, end in cases:
            for lower, upper in ((None, None), (36, 71), (120, 131), (780, 791)):
                with self.subTest(source=source, profile=(lower, upper)):
                    profile = NewsProfile('p', 1, '나이 조건', '전국',
                                          min_age_months=lower, max_age_months=upper)
                    original = facts(start_age_months=start, end_age_months=end)
                    before = copy.deepcopy(original)
                    result = assess(profile, original, source)
                    self.assertEqual(original, before)
                    self.assertEqual(result['target_status'], 'review')
                    self.assertFalse(result['accepted'])
                    self.assertIsNone(result['analysis']['start_age_months'])
                    self.assertIsNone(result['analysis']['end_age_months'])
                    self.assertEqual(len(result['analysis']['unverified_facts']['age_ranges']), 2)
                    self.assertIn('분리된 나이 구간', result['analysis']['grounding_note'])
                    self.assertEqual(assess(profile, result['analysis'], source)['target_status'], 'review')

    def test_single_repeated_overlapping_adjacent_and_unrestricted_ages_still_match(self):
        cases = [
            ('생후 0~12개월 대상.', 0, 12, ''),
            ('만 3~5세 대상.', 36, 71, ''),
            ('만 3세 이상 만 5세 이하 대상.', 36, 71, ''),
            ('만 65세 이상 대상.', 780, None, ''),
            ('만 6세 미만 대상.', None, 71, ''),
            ('만 3~5세 대상. 지원 대상 연령은 만 3~5세입니다.', 36, 71, ''),
            ('만 3~5세 또는 만 5~7세 대상.', 36, 95, ''),
            ('만 3~5세 또는 만 6~7세 대상.', 36, 95, ''),
            ('생후 0~6개월 또는 생후 7~12개월 대상.', 0, 12, ''),
            ('만 5세 이하 또는 만 6세 이상 대상.', None, None, ''),
            ('모든 나이가 대상이며 나이 제한이 없습니다.', None, None, ''),
            ('개인정보를 공개하지 않습니다.', None, None, ''),
            ('아동 만 3~5세 대상. 보호자는 만 19세 이상이어야 합니다.', 36, 71,
             '보호자는 만 19세 이상이어야 합니다.'),
        ]
        for phrase, start, end, eligibility in cases:
            with self.subTest(phrase=phrase):
                original = facts(start_age_months=start, end_age_months=end,
                    eligibility_detail=eligibility,
                    fact_evidence=facts()['fact_evidence'] | {'eligibility_detail': eligibility})
                result = assess(self.profile, original, phrase + ' 개인이 신청합니다.')
                self.assertEqual(result['target_status'], 'match')
                self.assertEqual(result['analysis']['unverified_facts'], {})
                self.assertEqual((result['analysis']['start_age_months'], result['analysis']['end_age_months']),
                                 (start, end))

    def test_negative_benefit_restriction_is_review_only_when_omitted(self):
        restrictions = [RESTRICTION, '기존 수급자에게는 지급되지 않습니다.',
            '고소득 가구는 지원하지 않습니다.', '소득이 있는 사람은 지원받지 않습니다.',
            '기존 수급자는 지급받을 수 없습니다.', '기존 수급자는 지원을 받지 않습니다.']
        for restriction in restrictions:
            with self.subTest(restriction=restriction):
                source = f'개인이 신청합니다. {BENEFIT} {restriction}'
                original = facts(support_detail=BENEFIT,
                    fact_evidence=facts()['fact_evidence'] | {'support_detail': BENEFIT})
                missing = assess(self.profile, original, source)
                self.assertEqual(missing['target_status'], 'review')
                self.assertEqual(missing['analysis']['support_detail'], BENEFIT)
                self.assertEqual(missing['analysis']['unverified_facts']['omitted_conditions'],
                                 [restriction.rstrip('.')])
                for field in ('eligibility_detail', 'support_detail'):
                    retained = restriction if field == 'eligibility_detail' else BENEFIT + ' ' + restriction
                    complete = original | {field: retained,
                        'fact_evidence': original['fact_evidence'] | {field: retained}}
                    result = assess(self.profile, complete, source)
                    self.assertEqual(result['target_status'], 'match')
                    self.assertEqual(result['analysis']['unverified_facts'], {})

    def test_unrelated_negative_guidance_does_not_become_a_benefit_exclusion(self):
        for note in ('개인정보를 공개하지 않습니다.', '참여 비용을 부담하지 않습니다.',
                     '비밀번호를 요구하지 않습니다.', '나이 제한이 없습니다.'):
            with self.subTest(note=note):
                source = f'개인이 신청합니다. {BENEFIT} {note}'
                result = assess(self.profile, facts(support_detail=BENEFIT,
                    fact_evidence=facts()['fact_evidence'] | {'support_detail': BENEFIT}), source)
                self.assertEqual(result['target_status'], 'match')
                self.assertFalse(result['review_required'])


class TestIntervalImportSafety(unittest.IsolatedAsyncioTestCase):
    async def _exercise(self, source, model_facts, expected, *, historical, recipe):
        with sqlite3.connect(':memory:') as conn:
            conn.row_factory = sqlite3.Row
            profiles.ensure_schema(conn)
            jobs.ensure_schema(conn)
            conn.execute('''CREATE TABLE welfare_items (
                id INTEGER PRIMARY KEY, title TEXT, link TEXT UNIQUE, site_name TEXT,
                category TEXT, target TEXT, residence TEXT, life_cycle TEXT,
                target_characteristics TEXT, start_age_months INTEGER, end_age_months INTEGER,
                support_detail TEXT, eligibility_detail TEXT, contact TEXT, deadline TEXT,
                ai_data TEXT, ai_skip_reason TEXT, is_archived INTEGER DEFAULT 0,
                notion_dismissed INTEGER DEFAULT 0, notion_page_id TEXT)''')
            conn.execute("INSERT INTO welfare_items(title,link,ai_data) VALUES ('사용자 작성 내용', 'https://example.invalid/user', '{}')")
            user_before = tuple(conn.execute('SELECT * FROM welfare_items').fetchone())
            imports.ensure_schema(conn)
            profile = NewsProfile('p', 1, '합성 조건', '전국', site_ids=('1',))
            profiles.create_profile(conn, profile)
            payload = dict(title='합성 소식', site_name='합성 사이트',
                           link='https://example.invalid/notice', content=source)
            sites = [dict(id=1, name='합성 사이트', url='https://example.invalid')]
            run = profiles.prepare_run(conn, 'p', expected_revision=1, sites=sites)
            job_id = jobs.enqueue(conn, run, payload=payload, model='mock-only', recipe=recipe)
            original = copy.deepcopy(model_facts)
            custom.validate_facts(model_facts)

            async def call_db(fn, *args, **kwargs):
                return fn(conn, *args, **kwargs)

            mock_model = AsyncMock(return_value=copy.deepcopy(model_facts))
            with patch.object(custom, 'classify', new=mock_model):
                if historical:
                    token = jobs.claim(conn, job_id, now=0)
                    jobs.complete(conn, job_id, token, dict(analysis=copy.deepcopy(model_facts),
                        target_status='match', accepted=True, review_required=False))
                else:
                    self.assertEqual(await process_job(job_id, object(), call_db=call_db), 'complete')
                if historical:
                    mock_model.assert_not_awaited()
                else:
                    mock_model.assert_awaited_once()
                raw_before = conn.execute('SELECT result_json FROM news_analysis_jobs WHERE job_id=?', (job_id,)).fetchone()[0]
                cache_before = conn.execute('SELECT result_json FROM news_analysis_cache').fetchone()[0]
                self._assert_reviewed(json.loads(jobs.get_job(conn, job_id)['result_json']), expected, source)
                self.assertEqual(jobs.list_profile_results(conn, 'p')['items'][0]['target_status'], expected)

                # Same recipe/input must use the cache and revalidate, never call
                # the model again or rewrite the historical response/cache.
                next_run = profiles.prepare_run(conn, 'p', expected_revision=1, sites=sites)
                next_id = jobs.enqueue(conn, next_run, payload=payload, model='mock-only', recipe=recipe)
                count = mock_model.await_count
                self.assertEqual(await process_job(next_id, object(), call_db=call_db), 'not_claimed')
                self.assertEqual(mock_model.await_count, count)
                self._assert_reviewed(json.loads(jobs.get_job(conn, next_id)['result_json']), expected, source)

            with patch.object(imports, '_ended', return_value=False):
                for candidate in (job_id, next_id):
                    preview = imports.preview(conn, 'p', candidate)
                    # All these fixtures are dateless. Timing uncertainty is
                    # independent of the target/condition review asserted here.
                    from services.news_timing import UNKNOWN_NOTE
                    self.assertTrue(preview['note'].endswith(UNKNOWN_NOTE))
                    self.assertEqual(preview['note'].count(UNKNOWN_NOTE), 1)
                    source_note = preview['note'].removesuffix(UNKNOWN_NOTE).strip()
                    self.assertEqual(bool(source_note), expected == 'review')
                    self.assertFalse(preview['ai_called'])
                    before_count = conn.execute('SELECT count(*) FROM welfare_items').fetchone()[0]
                    with self.assertRaisesRegex(ValueError, 'Explicit confirmation'):
                        imports.import_result(conn, 'p', candidate,
                            fingerprint=preview['fingerprint'], confirmed=False)
                    self.assertEqual(conn.execute('SELECT count(*) FROM welfare_items').fetchone()[0], before_count)
                    receipt = imports.import_result(conn, 'p', candidate,
                        fingerprint=preview['fingerprint'], confirmed=True)
                    self.assertIn(receipt['status'], ('created', 'existing'))
            saved = conn.execute('SELECT * FROM welfare_items WHERE link=?', (payload['link'],)).fetchone()
            saved_facts = json.loads(saved['ai_data'])
            self.assertEqual(saved_facts['news_import_review'], expected == 'review')
            if source == GAP_SOURCE:
                for key in ('start_age_months', 'end_age_months'):
                    self.assertIsNone(saved[key])
                    self.assertIsNone(saved_facts[key])
            else:
                self.assertEqual(saved['eligibility_detail'], model_facts['eligibility_detail'])
            self.assertEqual(conn.execute('SELECT result_json FROM news_analysis_jobs WHERE job_id=?', (job_id,)).fetchone()[0], raw_before)
            self.assertEqual(conn.execute('SELECT result_json FROM news_analysis_cache').fetchone()[0], cache_before)
            self.assertEqual(tuple(conn.execute('SELECT * FROM welfare_items WHERE id=1').fetchone()), user_before)
            self.assertEqual(model_facts, original)

    def _assert_reviewed(self, output, expected, source):
        self.assertEqual(output['target_status'], expected)
        self.assertEqual(output['accepted'], expected == 'match')
        self.assertEqual(output['review_required'], expected == 'review')
        self.assertEqual(output['analysis']['validation_version'], VERSION)
        if source == GAP_SOURCE:
            self.assertIsNone(output['analysis']['start_age_months'])
            self.assertIsNone(output['analysis']['end_age_months'])
            self.assertEqual(output['analysis']['unverified_facts']['start_age_months'], 36)
            self.assertEqual(output['analysis']['unverified_facts']['end_age_months'], 299)
        elif expected == 'review':
            self.assertEqual(output['analysis']['unverified_facts']['omitted_conditions'],
                             [RESTRICTION.rstrip('.')])

    async def test_worker_historical_cache_preview_and_confirmed_import(self):
        partial = facts(support_detail=BENEFIT,
            fact_evidence=facts()['fact_evidence'] | {'support_detail': BENEFIT})
        complete = partial | {'eligibility_detail': RESTRICTION,
            'fact_evidence': partial['fact_evidence'] | {'eligibility_detail': RESTRICTION}}
        cases = [(GAP_SOURCE, facts(start_age_months=36, end_age_months=299), 'review'),
                 (PAYMENT_SOURCE, partial, 'review'), (PAYMENT_SOURCE, complete, 'match'),
                 ('만 3~5세 대상. 개인이 신청합니다.', facts(start_age_months=36, end_age_months=71), 'match')]
        for source, model_facts, expected in cases:
            for historical in (False, True):
                # These saved/raw prose fixtures predate source-unit selection.
                for recipe in (custom.SOURCE_SPAN_RECIPE, custom.SCOPED_RECIPE):
                    with self.subTest(source=source, expected=expected, historical=historical, recipe=recipe):
                        await self._exercise(source, model_facts, expected, historical=historical, recipe=recipe)
