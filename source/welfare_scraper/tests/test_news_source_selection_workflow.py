"""Synthetic source-selection workflow; fake SDK only, never live AI quality."""
import copy
import hashlib
import json
import sqlite3
import unittest
from unittest.mock import AsyncMock, patch

from services.news_profile import NewsProfile
from services import news_profile_store as profiles, news_analysis_store as jobs
from services import news_custom_analysis as custom, news_result_import as imports
from services import news_source_selection as selection, page_coverage
from services.news_analysis_worker import process_job


class NewsSourceSelectionWorkflow(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.execute('PRAGMA foreign_keys=ON')
        profiles.ensure_schema(self.conn)
        jobs.ensure_schema(self.conn)
        # Only the local import destination schema needed by this workflow.
        self.conn.execute('''CREATE TABLE welfare_items (
            id INTEGER PRIMARY KEY, title TEXT, link TEXT UNIQUE, site_name TEXT,
            category TEXT, target TEXT, residence TEXT, life_cycle TEXT,
            target_characteristics TEXT, start_age_months INTEGER, end_age_months INTEGER,
            support_detail TEXT, eligibility_detail TEXT, contact TEXT, deadline TEXT, ai_data TEXT,
            ai_skip_reason TEXT,is_archived INTEGER DEFAULT 0,
            notion_dismissed INTEGER DEFAULT 0,notion_page_id TEXT)''')
        imports.ensure_schema(self.conn)
        self.profile = NewsProfile('p', 1, '합성 청소년 소식', '전국', life_cycles=('청소년',), site_ids=('1',))
        profiles.create_profile(self.conn, self.profile)
        self.source = ('청소년 대상 교육비 지원\n개인이 신청합니다.\n'
                       '신청 방법: 센터 방문 접수\n중복 수급자는 제외합니다.')
        self.payload = {'title': '합성 교육비 안내', 'link': 'https://example.invalid/selection',
                        'site_name': '합성센터', 'content': self.source,
                        'source_page': page_coverage.make()}

    def tearDown(self):
        self.conn.close()

    def run_snapshot(self):
        return profiles.prepare_run(self.conn, 'p', expected_revision=1,
                                    sites=[dict(id=1, name='합성센터', url='https://example.invalid')])

    def enqueue(self, *, recipe=None, payload=None):
        return jobs.enqueue(self.conn, self.run_snapshot(), payload=payload or self.payload,
                            model='offline-source-selection', recipe=recipe or custom.SHORT_SOURCE_RECIPE)

    async def call_db(self, function, *args, **kwargs):
        return function(self.conn, *args, **kwargs)

    def selected_reply(self, ids=None):
        return dict(
            is_relevant=True, category='교육', target='청소년', residence=[],
            life_cycle=['청소년'], target_characteristics=[], interest_topics=[],
            start_age_months=None, end_age_months=None,
            evidence_snippet='청소년 대상 교육비 지원',
            applicant_type='individual', applicant_evidence='개인이 신청합니다.',
            applicant_detail='개인 신청', beneficiary_detail='청소년',
            life_cycle_evidence=[dict(value='청소년', quote='청소년 대상 교육비 지원', basis='explicit')],
            residence_evidence=[], eligibility_detail='', contact='', deadline='',
            fact_evidence={'eligibility_detail':'', 'contact':'', 'deadline':'',
                           'is_relevant':'청소년 대상 교육비 지원'},
            _support_source_units=[1] if ids is None else ids)

    def prose_reply(self):
        value = self.selected_reply()
        value.pop('_support_source_units')
        value['support_detail'] = self.source
        value['fact_evidence']['support_detail'] = self.source
        return value

    async def execute(self, job, reply):
        original = copy.deepcopy(reply)
        with patch('ai_classifier._call_gemini_json', new=AsyncMock(return_value=copy.deepcopy(reply))) as call:
            status = await process_job(job, object(), call_db=self.call_db, clock=lambda: 0)
        self.assertEqual(call.await_count, 1)
        self.assertEqual(call.call_args.kwargs['request_attempts'], 1)
        self.assertEqual(reply, original)
        return status, call.call_args

    def result(self, job):
        return json.loads(jobs.get_job(self.conn, job)['result_json'])

    async def complete_selection(self, *, ids=None):
        job = self.enqueue()
        status, call = await self.execute(job, self.selected_reply(ids))
        self.assertEqual(status, 'complete')
        return job, self.result(job), call

    async def test_pinned_v14_preserves_source_in_store_and_import_not_display_body(self):
        job, result, call = await self.complete_selection()
        schema = call.kwargs['response_json_schema']
        self.assertIn('_support_source_units', schema['required'])
        self.assertNotIn('support_detail', schema['properties'])
        self.assertNotIn('support_detail', schema['properties']['fact_evidence']['properties'])
        self.assertIn('[Whole source selection:', call.args[1])
        self.assertTrue(result['accepted'], result)
        self.assertEqual(result['target_status'], 'match')
        analysis = result['analysis']
        self.assertEqual(analysis['support_detail'], self.source)
        self.assertEqual(analysis['fact_evidence']['support_detail'], self.source)
        self.assertEqual(analysis[selection.META_KEY], dict(version=1,
            source_sha256=hashlib.sha256(self.source.encode()).hexdigest(), unit_ids=[1]))
        self.assertNotIn('_support_source_units', analysis)
        stored = self.conn.execute('SELECT input_json,result_json FROM news_analysis_jobs WHERE job_id=?', (job,)).fetchone()
        for _ in range(2):
            self.assertEqual(self.result(job), result)
        visible = jobs.list_profile_results(self.conn, 'p')['items'][0]
        # Source remains exact internally; the user-facing body is a separate
        # conservative projection, never the whole captured page under a title.
        self.assertNotEqual(visible['details'].get('support_detail'), self.source)
        self.assertEqual(visible['presentation_kind'], 'source_excerpt')
        self.assertNotIn('source_text', visible)
        self.assertEqual(visible['import_eligibility'], {'allowed':True, 'reason':'confirmation_required'})
        self.assertNotIn(selection.META_KEY, visible['details'])
        self.assertIsNone(visible['review_evidence'])
        preview = imports.preview(self.conn, 'p', job)
        self.assertFalse(preview['ai_called'])
        with self.assertRaisesRegex(ValueError, 'Explicit confirmation'):
            imports.import_result(self.conn, 'p', job, fingerprint=preview['fingerprint'], confirmed=False)
        added = imports.import_result(self.conn, 'p', job, fingerprint=preview['fingerprint'], confirmed=True)
        self.assertEqual(added['status'], 'created')
        saved = self.conn.execute('SELECT support_detail,ai_data FROM welfare_items').fetchone()
        self.assertEqual(saved[0], self.source)
        self.assertEqual(json.loads(saved[1])['support_detail'], self.source)
        self.assertEqual(json.loads(saved[1])[selection.META_KEY], analysis[selection.META_KEY])
        self.assertFalse(json.loads(saved[1])['news_import_review'])
        self.assertEqual(self.conn.execute('SELECT input_json,result_json FROM news_analysis_jobs WHERE job_id=?', (job,)).fetchone(), stored)
        again = imports.import_result(self.conn, 'p', job, fingerprint=preview['fingerprint'], confirmed=True)
        self.assertEqual(again['status'], 'already_imported')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM welfare_items').fetchone()[0], 1)

    async def test_empty_selection_stays_review_after_cache_read_and_explicit_import(self):
        job, result, _ = await self.complete_selection(ids=[])
        self.assertEqual(result['target_status'], 'review')
        self.assertFalse(result['accepted'])
        self.assertEqual(result['analysis']['support_detail'], '')
        self.assertEqual(result['analysis'][selection.META_KEY]['unit_ids'], [])
        for _ in range(2):
            self.assertEqual(self.result(job)['analysis']['support_detail'], '')
            self.assertEqual(self.result(job)['target_status'], 'review')
        copied = self.enqueue()
        with patch('ai_classifier._call_gemini_json', new=AsyncMock()) as call:
            self.assertEqual(await process_job(copied, object(), call_db=self.call_db, clock=lambda: 0), 'not_claimed')
        call.assert_not_called()
        self.assertEqual(jobs.get_job(self.conn, copied)['attempts'], 0)
        self.assertEqual(self.result(copied)['target_status'], 'review')
        visible = next(row for row in jobs.list_profile_results(self.conn, 'p')['items'] if row['job_id'] == copied)
        self.assertNotIn('support_detail', visible['details'])
        self.assertIsNotNone(visible['review_evidence'])
        # Existing manual-review confirmation remains possible, never an
        # automatic approval or reconstruction of withheld descriptive text.
        preview = imports.preview(self.conn, 'p', copied)
        self.assertIn('원문', preview['note'])
        added = imports.import_result(self.conn, 'p', copied, fingerprint=preview['fingerprint'], confirmed=True)
        self.assertEqual(added['status'], 'created')
        row = self.conn.execute('SELECT support_detail,ai_data FROM welfare_items').fetchone()
        self.assertEqual(row[0], '')
        self.assertTrue(json.loads(row[1])['news_import_review'])

    async def test_saved_selection_tampering_cannot_restore_prose_or_keep_match(self):
        mutations = [
            lambda analysis: analysis.pop(selection.META_KEY),
            lambda analysis: analysis[selection.META_KEY].update(source_sha256='0'*64),
            lambda analysis: analysis[selection.META_KEY].update(unit_ids=[]),
            lambda analysis: analysis[selection.META_KEY].update(unit_ids=[True]),
            lambda analysis: analysis[selection.META_KEY].update(unit_ids=[1, 1]),
            lambda analysis: analysis.update(support_detail='청소년 대상 교육비 지원'),
            lambda analysis: analysis['fact_evidence'].update(support_detail='청소년 대상 교육비 지원'),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                job, output, _ = await self.complete_selection()
                preview = imports.preview(self.conn, 'p', job)
                tampered = copy.deepcopy(output)
                mutate(tampered['analysis'])
                tampered['import_eligibility'] = {'allowed':True, 'reason':'confirmation_required'}
                self.conn.execute('UPDATE news_analysis_jobs SET result_json=? WHERE job_id=?',
                                  (jobs._encode(tampered), job))
                self.conn.commit()
                for _ in range(2):
                    current = self.result(job)
                    self.assertFalse(current['accepted'])
                    self.assertEqual(current['target_status'], 'review')
                    self.assertEqual(current['analysis']['support_detail'], '')
                with self.assertRaisesRegex(ValueError, 'Preview changed'):
                    imports.import_result(self.conn, 'p', job, fingerprint=preview['fingerprint'], confirmed=True)
                self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM welfare_items').fetchone()[0], 0)
                self.assertEqual(json.loads(self.conn.execute('SELECT result_json FROM news_analysis_jobs WHERE job_id=?', (job,)).fetchone()[0]), tampered)
                self.conn.execute('DELETE FROM news_analysis_cache')
                self.conn.commit()

    async def test_raw_schema_errors_fail_one_attempt_without_legacy_repair_or_cache(self):
        replies = []
        for ids in ([True], [2], [1, 1], '1', None):
            reply = self.selected_reply()
            reply['_support_source_units'] = ids
            replies.append(reply)
        supplied_prose = self.selected_reply()
        supplied_prose['support_detail'] = '모델 요약'
        replies.append(supplied_prose)
        supplied_meta = self.selected_reply()
        supplied_meta[selection.META_KEY] = {'version':1}
        replies.append(supplied_meta)
        missing_field = self.selected_reply()
        missing_field.pop('interest_topics')
        replies.append(missing_field)
        for reply in replies:
            with self.subTest(reply=reply):
                job = self.enqueue()
                status, _ = await self.execute(job, reply)
                self.assertEqual(status, 'failed')
                saved = jobs.get_job(self.conn, job)
                self.assertEqual(saved['state'], 'failed')
                self.assertEqual(saved['reason'], 'invalid_analysis')
                self.assertIsNone(saved['result_json'])
                self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_analysis_cache').fetchone()[0], 0)
                self.assertFalse(imports.eligibility(self.conn, 'p', job)['allowed'])

    async def test_v13_short_job_and_v14_fallback_keep_the_prose_contract(self):
        legacy = self.enqueue(recipe=custom.SOURCE_SPAN_RECIPE)
        status, call = await self.execute(legacy, self.prose_reply())
        self.assertEqual(status, 'complete')
        self.assertIn('support_detail', call.kwargs['response_json_schema']['required'])
        self.assertNotIn('[Whole source selection:', call.args[1])
        self.assertNotIn(selection.META_KEY, self.result(legacy)['analysis'])
        newest = self.enqueue()
        self.assertNotEqual(jobs.get_job(self.conn, legacy)['cache_key'], jobs.get_job(self.conn, newest)['cache_key'])
        for source, page in [
            (self.source + '\n' + '추가 안내 ' * 300, page_coverage.make()),
            (self.source, page_coverage.make('attachment_review')),
            (self.source + '\n--- [첨부파일: 시험.pdf]\n첨부 내용', page_coverage.make()),
        ]:
            with self.subTest(source=source, page=page):
                payload = self.payload | {'content':source, 'source_page':page}
                job = self.enqueue(payload=payload)
                status, call = await self.execute(job, self.prose_reply())
                self.assertEqual(status, 'complete')
                self.assertNotIn('[Whole source selection:', call.args[1])
                self.assertIn('support_detail', call.kwargs['response_json_schema']['properties'])
                self.assertNotIn(selection.META_KEY, self.result(job)['analysis'])
                if page['status'] == 'review_required':
                    visible = next(row for row in jobs.list_profile_results(self.conn, 'p')['items'] if row['job_id'] == job)
                    self.assertEqual(visible['source_review']['status'], 'partial')

    async def test_valid_selection_never_overrides_explicit_non_news_or_bad_age(self):
        job = self.enqueue(payload=self.payload | {'content':self.source+'\n직원 채용 공고입니다.'})
        reply = self.selected_reply()
        reply['is_relevant'] = False
        reply['fact_evidence']['is_relevant'] = '직원 채용 공고입니다.'
        status, _ = await self.execute(job, reply)
        self.assertEqual(status, 'complete')
        result = self.result(job)
        self.assertEqual(result['target_status'], 'excluded')
        self.assertFalse(imports.eligibility(self.conn, 'p', job)['allowed'])
        with self.assertRaises(ValueError):
            imports.preview(self.conn, 'p', job)
        other = self.enqueue()
        reply = self.selected_reply()
        reply['start_age_months'] = 780
        status, _ = await self.execute(other, reply)
        self.assertEqual(status, 'complete')
        result = self.result(other)
        self.assertEqual(result['target_status'], 'review')
        self.assertIsNone(result['analysis']['start_age_months'])
        self.assertEqual(result['analysis']['support_detail'], self.source)

    async def test_whole_source_selection_never_collapses_disjoint_age_ranges(self):
        source = self.source + '\n지원 대상: 만 13~15세 또는 만 17~18세'
        job = self.enqueue(payload=self.payload | {'content':source})
        reply = self.selected_reply()
        reply.update(start_age_months=156, end_age_months=227)
        status, _ = await self.execute(job, reply)
        self.assertEqual(status, 'complete')
        for _ in range(2):
            result = self.result(job)
            self.assertEqual(result['target_status'], 'review')
            self.assertFalse(result['accepted'])
            self.assertIsNone(result['analysis']['start_age_months'])
            self.assertIsNone(result['analysis']['end_age_months'])
            self.assertEqual(result['analysis']['support_detail'], source)
            self.assertEqual(len(result['analysis']['unverified_facts']['age_ranges']), 2)
        preview = imports.preview(self.conn, 'p', job)
        imported = imports.import_result(self.conn, 'p', job, fingerprint=preview['fingerprint'], confirmed=True)
        self.assertEqual(imported['status'], 'created')
        row = self.conn.execute('SELECT start_age_months,end_age_months,support_detail,ai_data FROM welfare_items').fetchone()
        self.assertIsNone(row[0])
        self.assertIsNone(row[1])
        self.assertEqual(row[2], source)
        self.assertTrue(json.loads(row[3])['news_import_review'])

    def test_import_keeps_recipe_specific_interest_topic_requirement(self):
        facts = self.prose_reply()
        facts.pop('interest_topics')
        snapshot = {'profile':self.profile.snapshot()}
        for recipe in custom.SUPPORTED_RECIPES:
            job = {'state':'complete', 'recipe':recipe, 'input_json':jobs._encode(self.payload),
                   'result_json':jobs._encode({'analysis':facts, 'target_status':'review'})}
            with self.subTest(recipe=recipe):
                required = custom.recipe_options(recipe)['require_interest_topics']
                if required:
                    with self.assertRaisesRegex(ValueError, 'Invalid custom fact tags'):
                        imports._read(self.conn, 'p', 'mock-reviewed', reviewed_job=job, run_snapshot=snapshot)
                else:
                    _, copied, _, revision = imports._read(self.conn, 'p', 'mock-reviewed',
                                                          reviewed_job=job, run_snapshot=snapshot)
                    self.assertEqual(copied['interest_topics'], [])
                    self.assertEqual(revision, 1)
