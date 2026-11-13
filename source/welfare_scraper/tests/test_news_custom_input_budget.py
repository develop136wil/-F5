"""Default profile -> durable job -> real worker/admission, fake SDK boundary only.

Run only through scripts/run_isolated_tests.py; no operational profile or network.
"""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from google.genai import types
from services import ai_coordinator as quota, ai_rate_policy as rate
from services import news_default_condition as defaults, news_profile_store as profiles
from services import news_analysis_store as jobs, news_custom_analysis as custom
from services import news_result_import as imports, news_unified_feed as feed, page_coverage
from services.news_analysis_worker import process_job
from services.news_profile import NewsProfile

SHORT = '청소년 대상 교육비 지원\n개인이 신청합니다.\n신청 방법: 센터 방문 접수'
MODERATE = SHORT + '''
신청 내용이 변경되면 담당 기관에 변경사항을 알려야 하며 처리 결과는 별도로 안내합니다.
방문 신청은 주민센터에서 접수하며 온라인 신청은 해당 기관 누리집에서 진행합니다.
대리 신청을 하는 경우에는 위임장과 대리인의 신분증을 함께 제출해야 합니다.
증빙서류가 빠진 경우에는 담당 기관의 안내에 따라 정해진 기간 안에 보완해야 합니다.
접수한 서류는 심사를 위해 사용되며 사실과 다르게 작성한 경우에는 지원이 취소됩니다.
지원금은 신청인 명의의 계좌로 지급하며 계좌정보를 정확하게 작성해야 합니다.
동일한 목적으로 이미 지원을 받은 경우에는 중복 지급 여부를 확인한 다음 결과를 안내합니다.'''


class CurrentCustomInputBudget(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='custom-budget-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        ledger = patch.object(quota, '_state_path', return_value=str(self.root / 'quota.json'))
        ledger.start()
        self.addCleanup(ledger.stop)
        environment = patch.dict(os.environ, {'AI_DAILY_REQUEST_LIMIT': '20'})
        environment.start()
        self.addCleanup(environment.stop)
        quota._reset_for_tests()
        self.addCleanup(quota._reset_for_tests)
        self.conn = sqlite3.connect(':memory:')
        self.addCleanup(self.conn.close)
        profiles.ensure_schema(self.conn)
        jobs.ensure_schema(self.conn)
        # Explicit local fixture schema: no app initialization or operational DB.
        self.conn.execute('''CREATE TABLE welfare_items (
            id INTEGER PRIMARY KEY,title TEXT,link TEXT UNIQUE,site_name TEXT,
            category TEXT,target TEXT,residence TEXT,life_cycle TEXT,target_characteristics TEXT,
            start_age_months INTEGER,end_age_months INTEGER,support_detail TEXT,
            eligibility_detail TEXT,contact TEXT,deadline TEXT,ai_data TEXT,
            event_date TEXT,event_end_date TEXT,ai_skip_reason TEXT,scraped_at TEXT,
            is_archived INTEGER DEFAULT 0,notion_dismissed INTEGER DEFAULT 0,notion_page_id TEXT)''')
        imports.ensure_schema(self.conn)
        self.model = rate.MODELS[0]
        self.provider = AsyncMock()
        self.client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=self.provider)))

    def enqueue(self, source=SHORT, *, recipe=None, preferences='', page=None):
        if defaults.read(self.conn) is None:
            defaults.save(self.conn, NewsProfile('default-news', 1, '첫 뉴스 조건', '전국',
                life_cycles=('청소년',), site_ids=('1',), ai_prompt=preferences), expected_revision=0)
        profile = defaults.read(self.conn)
        run = profiles.prepare_run(self.conn, profile.profile_id, expected_revision=profile.revision,
            sites=[dict(id=1, name='합성센터', url='https://example.invalid')])
        payload = dict(title='청소년 교육비 지원', link='https://example.invalid/news',
            site_name='합성센터', content=source, source_page=page if page is not None else page_coverage.make())
        # Frozen v18 regression: v19 has separate complete-input/count fixtures.
        return jobs.enqueue(self.conn, run, payload=payload, model=self.model, recipe=recipe or custom.SHARED_SCHEMA_RECIPE)

    async def call_db(self, fn, *args, **kwargs):
        return fn(self.conn, *args, **kwargs)

    def response(self, *, ids=None):
        return dict(is_relevant=True, category='교육', target='청소년', residence=[],
            life_cycle=['청소년'], target_characteristics=[], interest_topics=[],
            start_age_months=None, end_age_months=None, evidence_snippet='청소년 대상 교육비 지원',
            applicant_type='individual', applicant_evidence='개인이 신청합니다.',
            applicant_detail='개인 신청', beneficiary_detail='청소년',
            life_cycle_evidence=[dict(value='청소년', quote='청소년 대상 교육비 지원', basis='explicit')],
            residence_evidence=[], eligibility_detail='', contact='', deadline='',
            fact_evidence=dict(eligibility_detail='', contact='', deadline='', is_relevant='청소년 대상 교육비 지원'),
            _support_source_units=[1] if ids is None else ids)

    async def process(self, job, raw=None):
        raw = self.response() if raw is None else raw
        self.provider.return_value = types.GenerateContentResponse(candidates=[types.Candidate(
            content=types.Content(parts=[types.Part(text=json.dumps(raw, ensure_ascii=False))]), finish_reason='STOP')])
        return await process_job(job, self.client, call_db=self.call_db, clock=lambda: 0)

    def output(self, job):
        return json.loads(jobs.get_job(self.conn, job)['result_json'])

    async def assert_connected_success(self, source):
        self.assertEqual(quota.input_token_budget(self.model), 6000)
        job = self.enqueue(source)
        self.assertEqual(await self.process(job), 'complete', jobs.get_job(self.conn, job))
        self.provider.assert_awaited_once()
        request = self.provider.await_args.kwargs
        size = quota.estimate_generation_input_tokens(request['contents'], request['config'].response_json_schema)
        self.assertLessEqual(size, 6000)
        transmitted = json.loads(request['contents'].split('\nSOURCE: ')[1])
        self.assertEqual(transmitted['content'], source)
        output = self.output(job)
        self.assertTrue(output['accepted'], output)
        self.assertEqual(output['analysis']['support_detail'], source)
        self.assertEqual(output['analysis']['fact_evidence']['support_detail'], source)
        self.assertEqual(output['analysis']['source_input']['status'], 'complete')
        ready = feed.read_feed(self.conn, defaults.PROFILE_ID, view='ready')
        self.assertEqual(ready['total'], 1, ready)
        row = ready['items'][0]
        self.assertEqual(row['job_id'], job)
        self.assertTrue(row['selectable'])
        # Canonical input/evidence stays lossless above and in storage below.
        # A news list is now a readable projection, never a full-source dump.
        self.assertNotEqual(row['support_detail'], source)
        self.assertEqual(row['presentation_kind'], 'source_excerpt')
        self.assertTrue(row['source_note'])
        # This is preparation for a user-selected send, not a Notion upload.
        prepared = feed.prepare(self.conn, defaults.PROFILE_ID,
            [dict(id=row['id'], fingerprint=row['fingerprint'])], action='upload', confirmed=True)
        self.assertFalse(prepared['ai_called'])
        self.assertFalse(prepared['notion_uploaded'])
        self.assertEqual(len(prepared['ids']), 1)
        persisted = self.conn.execute('SELECT support_detail FROM welfare_items WHERE id=?',
                                     (prepared['ids'][0],)).fetchone()[0]
        self.assertEqual(persisted, source)
        self.assertEqual(quota.status()['recorded_requests_today'], 1)
        self.assertEqual(quota.input_token_budget(self.model), 6000)
        self.assertEqual(await self.process(job), 'not_claimed')
        self.provider.assert_awaited_once()
        schema_bytes = len(json.dumps(request['config'].response_json_schema,
            ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        print(f'CURRENT_NEWS_BUDGET chars={len(source)} source_bytes={len(source.encode("utf-8"))} '
              f'prompt_bytes={len(request["contents"].encode("utf-8"))} schema_bytes={schema_bytes} '
              f'full_request={size} allowance=6000 sdk_calls=1')

    async def test_fresh_default_short_source_reaches_sdk_and_completes_once(self):
        await self.assert_connected_success(SHORT)

    async def test_fresh_default_moderate_korean_source_preserved_and_completes_once(self):
        self.assertGreaterEqual(len(MODERATE), 300)
        await self.assert_connected_success(MODERATE)

    async def test_fresh_default_five_hundred_character_source_completes_once(self):
        source = MODERATE + ('\n신청서 작성이 어려운 경우에는 해당 기관에 도움을 요청할 수 있습니다.'
            '\n서류를 제출한 뒤에는 접수 여부를 확인하고 담당 기관의 안내를 따라야 합니다.'
            '\n추가 안내가 필요한 경우에는 신청인이 알 수 있도록 별도 안내문을 게시합니다.'
            '\n방문하기 전에 안내된 준비사항을 확인해주세요.')
        self.assertGreaterEqual(len(source), 500)
        await self.assert_connected_success(source)

    async def test_oversize_source_is_actionably_held_with_original_and_no_quota_use(self):
        source = MODERATE * 5
        job = self.enqueue(source)
        self.assertEqual(await self.process(job), 'failed')
        saved = jobs.get_job(self.conn, job)
        self.assertEqual(saved['reason'], 'ai_input_size_review_required')
        self.assertEqual(json.loads(saved['input_json'])['content'], source)
        self.provider.assert_not_awaited()
        self.assertEqual(quota.status()['recorded_requests_today'], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_analysis_cache').fetchone()[0], 0)
        self.assertEqual(feed.read_feed(self.conn, defaults.PROFILE_ID, view='ready')['total'], 0)
        held = feed.read_feed(self.conn, defaults.PROFILE_ID, view='review')['items'][0]
        self.assertFalse(held['selectable'])
        self.assertEqual(held['review_reason'], 'ai_input_size_review_required')
        self.assertTrue(held['input_note'])
        self.assertEqual(await self.process(job), 'not_claimed')

    async def test_source_over_character_window_is_held_not_hydrated_from_excerpt(self):
        job = self.enqueue(MODERATE * 25)
        self.assertEqual(await self.process(job), 'failed')
        self.assertEqual(jobs.get_job(self.conn, job)['reason'], 'ai_input_size_review_required')
        self.provider.assert_not_awaited()

    async def test_large_custom_preferences_are_not_silently_truncated(self):
        preference = '조건에 맞는 모든 신청 방법과 예외 사항을 확인하세요.' * 150
        job = self.enqueue(preferences=preference)
        self.assertEqual(await self.process(job), 'failed')
        self.assertEqual(jobs.get_job(self.conn, job)['reason'], 'ai_input_size_review_required')
        self.assertEqual(defaults.read(self.conn).ai_prompt, preference)
        self.provider.assert_not_awaited()

    async def test_contacts_stay_withheld_but_locally_selected_source_is_unchanged(self):
        source = SHORT + '\n문의: 담당자 010-1234-5678'
        job = self.enqueue(source)
        self.assertEqual(await self.process(job), 'complete')
        self.provider.assert_awaited_once()
        prompt = self.provider.await_args.kwargs['contents']
        self.assertNotIn('010-1234-5678', prompt)
        self.assertIn('[업무연락처 생략]', prompt)
        self.assertEqual(self.output(job)['analysis']['support_detail'], source)
        self.assertEqual(self.output(job)['analysis']['contact'], '')

    async def test_sensitive_tail_still_blocks_before_planning_or_provider(self):
        job = self.enqueue(MODERATE * 3 + '\n개인 휴대전화: 010-1234-5678')
        self.assertEqual(await self.process(job), 'failed')
        self.assertEqual(jobs.get_job(self.conn, job)['reason'], 'ai_input_review_required')
        self.provider.assert_not_awaited()

    async def test_empty_whole_selection_remains_review(self):
        job = self.enqueue()
        self.assertEqual(await self.process(job, self.response(ids=[])), 'complete')
        self.assertFalse(self.output(job)['accepted'])
        self.assertEqual(self.output(job)['target_status'], 'review')

    async def test_lower_confirmed_budget_still_prevents_provider_dispatch(self):
        key = 'synthetic-budget-key'
        observed = rate.observe({self.model: dict(rpm=10, tpm=5000, rpd=20),
            rate.MODELS[1]: dict(rpm=10, tpm=10000, rpd=20)}, key=key)
        (self.root / 'config.json').write_text(json.dumps({'GOOGLE_API_KEY': key,
            rate.POLICY_KEY: observed}), encoding='utf-8')
        job = self.enqueue()
        self.assertEqual(await self.process(job), 'failed')
        self.assertEqual(quota.input_token_budget(self.model), 3000)
        self.provider.assert_not_awaited()

    async def test_pinned_v16_keeps_its_contract_and_cannot_reuse_v17_cache(self):
        old = self.enqueue(recipe=custom.CONTACT_RECIPE)
        new = self.enqueue()
        self.assertNotEqual(jobs.get_job(self.conn, old)['cache_key'], jobs.get_job(self.conn, new)['cache_key'])
        self.assertFalse(custom.recipe_options(custom.CONTACT_RECIPE)['budgeted_request'])
        self.assertTrue(custom.recipe_options(custom.RECIPE)['budgeted_request'])
        self.assertEqual(await self.process(old), 'failed')
        self.assertEqual(jobs.get_job(self.conn, old)['reason'], 'ai_input_size_review_required')
        self.provider.assert_not_awaited()
        self.assertEqual(await self.process(new), 'complete')
        self.provider.assert_awaited_once()
