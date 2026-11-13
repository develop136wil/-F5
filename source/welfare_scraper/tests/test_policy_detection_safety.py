"""Offline regression coverage for policy detection and acquisition boundaries."""
import asyncio
import copy
import hashlib
import json
import ssl
import sqlite3
import unittest
from unittest.mock import AsyncMock, patch

import ai_classifier
from services import policy_sync as ps, policy_watch as pw

URL = 'https://example.invalid/policy/details/1001'


class PolicyDetectionFacts(unittest.TestCase):
    def support_diff(self, old, new, cosine=None):
        vectors = {(old[:2000], new[:2000]): cosine} if cosine is not None else {}
        diffs = ps._diff_fields({'support_detail': old}, {'support_detail': new}, None, vectors)
        return next(d for d in ps._visible_diffs(diffs) if d['field'] == ps._PROP_LABEL['support_detail'])

    def test_service_change_survives_identical_amount(self):
        for old, new in [('병원 진료와 재활치료 지원', '심리 상담과 방문 돌봄 지원'),
                         ('진료비 월 10만원 지원', '방문돌봄비 월 10만원 지원')]:
            with self.subTest(old=old):
                self.assertIn(self.support_diff(old, new, .999)['level'], ('high', 'info'))

    def test_shared_date_never_hides_changed_amount(self):
        diff = self.support_diff('월 10만원 지원, 마감 2026-12-31', '월 20만원 지원, 마감 2026-12-31', .999)
        self.assertEqual(diff['level'], 'high')

    def test_shared_amount_never_hides_changed_duration(self):
        diff = self.support_diff('월 10만원 최대 12개월 지원', '월 10만원 최대 6개월 지원', .999)
        self.assertEqual(diff['level'], 'high')
        self.assertIn('12개월', ps._money_date_tokens('월 10만원 최대 12개월 지원'))

    def test_decimal_amount_and_discount_changes_remain_visible(self):
        for old, new in [('연 1.5만원 지원', '연 15만원 지원'), ('50% 할인', '30% 할인')]:
            with self.subTest(old=old):
                self.assertEqual(self.support_diff(old, new, .999)['level'], 'high')

    def test_eligibility_negation_is_not_hidden_by_similarity(self):
        old, new = '중복 지원 가능합니다.', '중복 지원 불가능합니다.'
        diffs = ps._diff_fields({'eligibility_detail': old}, {'eligibility_detail': new}, None, {(old, new): .999})
        self.assertIn(diffs[0]['level'], ('high', 'info'))

    def test_markdown_only_support_difference_is_not_substantive(self):
        diffs = ps._diff_fields({'support_detail': '지원 내용\n월 10만원'},
                               {'support_detail': '### 지원 내용\n**월 10만원**'}, None, {})
        self.assertFalse(ps._visible_diffs(diffs))

    def test_seconds_deadlines_preserved_but_explicit_live_clock_ignored(self):
        self.assertNotEqual(pw._normalize_text('마감: 2026-09-20 18:00:00'),
                            pw._normalize_text('마감: 2026-10-20 18:00:00'))
        self.assertEqual(pw._normalize_text('안내 현재 시각: 2026-09-20 18:00:00 조회 100'),
                         pw._normalize_text('안내 현재 시각: 2026-09-20 19:00:00 조회 200'))

    def test_date_formats_still_detected(self):
        for before, after in [('2026-09-20', '2026-10-20'), ('2026-09-20 18:00', '2026-10-20 18:00'),
                              ('2026년 9월 20일 오후 6시', '2026년 10월 20일 오후 6시')]:
            self.assertNotEqual(pw._normalize_text(before), pw._normalize_text(after))

    def test_verified_tls_is_required(self):
        context = ps._relaxed_ssl_context()
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_watch_error_migration_is_idempotent_and_keeps_baseline(self):
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('CREATE TABLE policy_watch (id INTEGER PRIMARY KEY, enabled INTEGER, last_hash TEXT)')
            connection.execute("INSERT INTO policy_watch VALUES (1, 1, 'saved-baseline')")
            connection.commit()
            with patch.object(pw.database, 'get_connection', return_value=connection):
                pw.database.init_db()
                pw.database.init_db()
            self.assertIn('last_error', {row[1] for row in connection.execute('PRAGMA table_info(policy_watch)')})
            self.assertEqual(tuple(connection.execute('SELECT last_hash, last_error FROM policy_watch').fetchone()),
                             ('saved-baseline', None))
        finally:
            connection.close()


class PolicySourceAcquisition(unittest.IsolatedAsyncioTestCase):
    async def fetch(self, payload, scraper=None):
        with patch.object(ai_classifier, 'fetch_page_content_async', new=AsyncMock(return_value=payload)):
            return await ps._fetch_source(None, scraper, URL, asyncio.Semaphore(1))

    async def test_attachment_manifest_changes_hash_not_source_evidence(self):
        body = '지원 정책 본문 ' * 80
        outputs = []
        for suffix in ('v1', 'v2'):
            parsed = ai_classifier._parse_page_html(f'<p>{body}</p><a href="/terms-{suffix}.pdf">상세 안내</a>', URL)
            outputs.append(await self.fetch(parsed))
        self.assertEqual(len(outputs[0]), 3)
        self.assertNotEqual(outputs[0][0], outputs[1][0])
        self.assertEqual(outputs[0][1], outputs[1][1])
        self.assertEqual(outputs[1].attachment_manifest, ('https://example.invalid/terms-v2.pdf',))
        self.assertIn('attachment_review', outputs[1].source_review['reasons'])

    async def test_attachment_order_and_duplicates_do_not_trigger_change(self):
        a, b = 'https://example.invalid/a.pdf', 'https://example.invalid/b.hwp'
        first = await self.fetch({'text': '본문 ' * 120, 'attachments': [a, b, a]})
        second = await self.fetch({'text': '본문 ' * 120, 'attachments': [b, a]})
        self.assertEqual(first[0], second[0])

    async def test_partial_html_cannot_become_complete_source(self):
        result = await self.fetch({'policy_text': '일부 본문 ' * 70,
                                  'source_page': {'reasons': ['html_byte_limit']}})
        self.assertEqual(result.source_review['code'], 'source_read_incomplete')
        self.assertIn('html_byte_limit', result.source_review['reasons'])

    async def test_failed_verified_fetch_does_not_use_browser_bypass(self):
        scraper = type('FakeScraper', (), {'fetch_rendered_text': AsyncMock(return_value='본문 ' * 200)})()
        result = await self.fetch({'text': '', 'fetch_failure_kind': 'tls_error'}, scraper)
        scraper.fetch_rendered_text.assert_not_awaited()
        self.assertEqual(result.fetch_failure_kind, 'tls_error')
        self.assertIn('fetch_failed', result.source_review['reasons'])

    async def test_rendered_fallback_retains_incomplete_manifest_warning(self):
        scraper = type('FakeScraper', (), {'fetch_rendered_text': AsyncMock(return_value='렌더된 본문 ' * 90)})()
        result = await self.fetch({'text': '짧은 본문'}, scraper)
        self.assertIn('rendered_capture_partial', result.source_review['reasons'])

    async def test_complete_html_has_no_hold(self):
        result = await self.fetch({'policy_text': '완전한 본문 ' * 70, 'source_page': {'reasons': []}})
        self.assertFalse(result.source_review)
        self.assertFalse(result[2])

    async def test_timeout_is_not_a_dead_link_or_second_unguarded_request(self):
        session = type('FakeSession', (), {'get': unittest.mock.Mock(side_effect=AssertionError('extra GET'))})()
        with patch.object(ai_classifier, 'fetch_page_content_async', new=AsyncMock(return_value={
                'text': '', 'fetch_failure_kind': 'timeout'})) as fetch:
            result = await ps._fetch_source(session, None, URL, asyncio.Semaphore(1))
        self.assertFalse(result[2])
        session.get.assert_not_called()
        self.assertIn('url_validator', fetch.await_args.kwargs)

    async def test_not_found_remains_a_broken_link(self):
        result = await self.fetch({'text': '', 'fetch_failure_kind': 'not_found'})
        self.assertTrue(result[2])


class PolicyWatchFailureResults(unittest.IsolatedAsyncioTestCase):
    def row(self, wid=1):
        return {'id': wid, 'name': '합성 정책', 'url': URL, 'last_hash': 'old', 'last_text': '기존 본문'}

    async def run_watch(self, rows, outcomes):
        with patch.object(pw.database, 'execute_query', return_value=rows), \
             patch.object(pw.database, 'execute_write') as writes, \
             patch.object(ps, '_fetch_source', new=AsyncMock(side_effect=outcomes)):
            result = await pw.run_watch_check(scraper=object())
        return result, writes

    async def test_all_failed_is_error_and_baseline_untouched(self):
        result, writes = await self.run_watch([self.row()], [ps.SourceFetchResult('', '', True)])
        self.assertEqual(result['status'], 'error')
        self.assertEqual((result['succeeded'], result['failed'], result['changed']), (0, 1, 0))
        sql, params = writes.call_args.args
        self.assertIn('last_error', sql)
        self.assertNotIn('last_hash', sql)

    async def test_partial_is_not_success_zero(self):
        row2 = self.row(2)
        row2['last_hash'] = hashlib.sha256('동일'.encode()).hexdigest()
        result, _ = await self.run_watch([self.row(), row2], [ps.SourceFetchResult('', '', True), ps.SourceFetchResult('동일', '동일', False)])
        self.assertEqual(result['status'], 'partial')
        self.assertEqual((result['succeeded'], result['failed'], result['changed']), (1, 1, 0))

    async def test_partial_source_never_advances_watch_baseline(self):
        held = ps.SourceFetchResult('일부', '일부', False, source_review={
            'code': 'source_read_incomplete', 'reasons': ['page_char_limit']})
        result, writes = await self.run_watch([self.row()], [held])
        self.assertEqual(result['failed'], 1)
        self.assertNotIn('last_hash', writes.call_args.args[0])

    async def test_recovered_watch_clears_saved_error(self):
        row = self.row()
        row['last_hash'] = hashlib.sha256('동일'.encode()).hexdigest()
        result, writes = await self.run_watch([row], [ps.SourceFetchResult('동일', '동일', False)])
        self.assertEqual(result['status'], 'completed')
        self.assertIn('last_error = NULL', writes.call_args.args[0])

    async def test_browser_start_failure_records_all_unprocessed_errors(self):
        scraper = type('FakeScraper', (), {'start': AsyncMock(side_effect=RuntimeError('synthetic')),
                                         'close': AsyncMock()})()
        with patch.object(pw.database, 'execute_query', return_value=[self.row(), self.row(2)]), \
             patch.object(pw.database, 'execute_write') as writes, \
             patch('scraper_engine.ScraperEngine', return_value=scraper):
            result = await pw.run_watch_check()
        self.assertEqual((result['status'], result['failed']), ('error', 2))
        self.assertEqual(writes.call_count, 2)
        scraper.close.assert_awaited_once()


class PolicyCheckDetectionIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_substantive_support_changes_persist_a_visible_proposal(self):
        for old_value, new_value in [
            ('병원 진료와 재활치료 지원', '심리 상담과 방문 돌봄 지원'),
            ('진료비 월 10만원 지원', '방문돌봄비 월 10만원 지원'),
            ('월 10만원, 마감 2026-12-31', '월 20만원, 마감 2026-12-31'),
            ('월 10만원 최대 12개월 지원', '월 10만원 최대 6개월 지원'),
            ('마감 2026-09-20 18:00:00', '마감 2026-10-20 18:00:00'),
            ('연 1.5만원 지원', '연 15만원 지원'),
        ]:
            with self.subTest(old=old_value):
                old, new = {'support_detail': old_value}, {'support_detail': new_value}
                row = dict(id=101, page_id='synthetic-policy', title='합성 정책', db_key='synthetic',
                    notion_fields=json.dumps(old), source_urls=json.dumps([URL]),
                    source_hashes=json.dumps({URL: hashlib.sha256(pw._normalize_text(old_value).encode()).hexdigest()}),
                    source_texts=json.dumps({URL: old_value}), last_extracted_fields=json.dumps(old),
                    pending_diff=None, change_status='none')
                async def diff_no_network(before, after, client):
                    return ps._diff_fields(before, after, None, {})
                with patch.object(ps, '_active_configured_keys', return_value=['synthetic']), \
                     patch.object(ps, '_db_query_strict', new=AsyncMock(return_value=[row])), \
                     patch.object(ps, '_db_write_strict', new=AsyncMock()) as save, \
                     patch.object(ps, '_persist_policy_review', new=AsyncMock()), \
                     patch.object(ps, '_fetch_source', new=AsyncMock(return_value=(pw._normalize_text(new_value), new_value, False))), \
                     patch.object(ai_classifier, 'get_client', return_value=object()), \
                     patch.object(ai_classifier, 'extract_policy_fields_async', new=AsyncMock(return_value=copy.deepcopy(new))), \
                     patch.object(ps, '_get_db_schema', return_value={}), \
                     patch.object(ps, '_normalize_extracted_fields'), \
                     patch.object(ps, '_diff_fields_with_embeddings', side_effect=diff_no_network), \
                     patch.object(ps, '_extraction_meta', return_value={'prompt_version': ps._EXTRACTION_PROMPT_VERSION}):
                    result = await ps.check_policies(['synthetic'], scraper=object())
                self.assertEqual(result['changed'], 1)
                self.assertIn('pending_diff=?', save.await_args.args[0])

    async def test_partial_or_missing_source_preserves_all_baselines_without_ai(self):
        other = URL + '?part=2'
        for held_result in [
            ps.SourceFetchResult('일부', '일부', False, source_review={
                'code': 'source_read_incomplete', 'reasons': ['html_byte_limit']}),
            ps.SourceFetchResult('본문 [첨부 변경]', '본문', False, source_review={
                'code': 'source_read_incomplete', 'reasons': ['attachment_review']},
                attachment_manifest=['https://example.invalid/new.pdf']),
            ps.SourceFetchResult('', '', False, source_review={'code': 'source_missing'}),
        ]:
            with self.subTest(review=held_result.source_review):
                old_hashes, old_texts = {URL: 'previous1', other: 'previous2'}, {URL: '옛 원문1', other: '옛 원문2'}
                row = dict(id=101, page_id='synthetic-policy', title='합성 정책', db_key='synthetic',
                    notion_fields=json.dumps({'support_detail': '기존 내용'}), source_urls=json.dumps([URL, other]),
                    source_hashes=json.dumps(old_hashes), source_texts=json.dumps(old_texts),
                    last_extracted_fields='{}', pending_diff=None, change_status='none')
                async def source_by_url(session, scraper, url, sem):
                    return held_result if url == URL else ps.SourceFetchResult('수정 본문', '수정 본문', False)
                with patch.object(ps, '_active_configured_keys', return_value=['synthetic']), \
                     patch.object(ps, '_db_query_strict', new=AsyncMock(return_value=[row])), \
                     patch.object(ps, '_db_write_strict', new=AsyncMock()) as save, \
                     patch.object(ps, '_persist_policy_review', new=AsyncMock()) as reviews, \
                     patch.object(ps, '_fetch_source', side_effect=source_by_url), \
                     patch.object(ai_classifier, 'get_client', return_value=object()), \
                     patch.object(ai_classifier, 'extract_policy_fields_async', new=AsyncMock()) as extract:
                    result = await ps.check_policies(['synthetic'], scraper=object())
                extract.assert_not_awaited()
                self.assertEqual(result['ai_required'], 0)
                self.assertGreater(result['review_count'], 0)
                params = save.await_args.args[1]
                self.assertEqual(json.loads(params[0]), old_hashes)
                self.assertEqual(json.loads(params[1]), old_texts)
                self.assertTrue(any(call.args[1] == 'record_hold' for call in reviews.await_args_list))
                self.assertFalse(any(call.args[1] == 'resolve_after_success' for call in reviews.await_args_list))


class PolicyScheduledWatch(unittest.IsolatedAsyncioTestCase):
    async def run_watch(self, source, *, startup_failure=False):
        row = dict(id=101, title='합성 정책', source_urls=json.dumps([URL]), source_hashes=json.dumps({URL: 'oldhash'}))
        scraper = type('FakeScraper', (), {'start': AsyncMock(side_effect=RuntimeError('synthetic') if startup_failure else None),
                                         'close': AsyncMock()})()
        with patch.object(ps, '_active_configured_keys', return_value=['synthetic']), \
             patch.object(ps, '_db_query_strict', new=AsyncMock(return_value=[row])), \
             patch.object(ps, '_db_write_strict', new=AsyncMock()) as writes, \
             patch.object(ps, '_fetch_source', new=AsyncMock(return_value=source)), \
             patch('scraper_engine.ScraperEngine', return_value=scraper):
            result = await ps.watch_stage1_only(['synthetic'])
        writes.assert_not_awaited()
        scraper.close.assert_awaited_once()
        return result

    async def test_partial_source_is_error_not_unchanged(self):
        result = await self.run_watch(ps.SourceFetchResult('일부', '일부', False,
            source_review={'code': 'source_read_incomplete', 'reasons': ['page_char_limit']}))
        self.assertEqual((result['status'], result['failed'], result['changed']), ('error', 1, 0))

    async def test_attachment_link_change_can_be_monitored_without_ai(self):
        result = await self.run_watch(ps.SourceFetchResult('본문 + 첨부 manifest', '본문', False,
            source_review={'code': 'source_read_incomplete', 'reasons': ['attachment_review']}))
        self.assertEqual((result['status'], result['failed'], result['changed']), ('completed', 0, 1))

    async def test_start_failure_closes_and_reports_error(self):
        result = await self.run_watch(None, startup_failure=True)
        self.assertEqual((result['status'], result['failed']), ('error', 1))


class InitialAuditAcquisitionHolds(unittest.IsolatedAsyncioTestCase):
    async def run_initial(self, texts):
        from services import policy_review_store
        other = URL + '?part=2'
        row = dict(id=101, page_id='synthetic-policy', title='합성 정책', db_key='synthetic',
                   notion_fields=json.dumps({'support_detail': '지원 내용'}),
                   source_urls=json.dumps([URL, other]), source_texts=json.dumps(texts))
        with patch.object(ps, '_active_configured_keys', return_value=['synthetic']), \
             patch.object(ps, '_db_query_strict', new=AsyncMock(return_value=[row])), \
             patch.object(ps, '_db_write_strict', new=AsyncMock()) as save, \
             patch.object(ps, '_persist_policy_review', new=AsyncMock()) as reviews, \
             patch.object(policy_review_store, 'unresolved_by_policy', return_value={}), \
             patch.object(ai_classifier, 'get_client', return_value=object()), \
             patch.object(ai_classifier, 'extract_policy_fields_async', new=AsyncMock(return_value={'support_detail': '지원 내용'})) as extract, \
             patch.object(ps, '_normalize_extracted_fields'), \
             patch.object(ps, '_filter_low_quality_extracted_fields', side_effect=lambda fields, old: (fields, [])), \
             patch.object(ps, '_diff_fields_with_embeddings', new=AsyncMock(return_value=[])):
            result = await ps.run_initial_audit(['synthetic'])
        return result, extract, save, reviews

    async def test_all_cached_sources_reach_one_extraction(self):
        other = URL + '?part=2'
        _, extract, _, reviews = await self.run_initial({URL: '주요 지원 내용 ' * 50, other: '짧은 별도 제외 조건은 반드시 확인'})
        extract.assert_awaited_once()
        self.assertIn('주요 지원 내용', extract.await_args.args[4])
        self.assertIn('짧은 별도 제외 조건', extract.await_args.args[4])
        resolved = [call for call in reviews.await_args_list if call.args[1] == 'resolve_after_success']
        self.assertEqual(set(resolved[0].args[3]), {URL, other})

    async def test_missing_companion_source_is_held_without_marking_audited(self):
        result, extract, save, reviews = await self.run_initial({URL: '충분히 긴 주요 지원 내용 ' * 50})
        extract.assert_not_awaited()
        save.assert_not_awaited()
        self.assertEqual(result['review_count'], 1)
        self.assertEqual(reviews.await_args.args[-1], 'fetch')
        self.assertEqual(reviews.await_args.args[1], 'record_hold')

    async def test_cached_body_cannot_bypass_current_acquisition_hold(self):
        from services import policy_review_store
        for code, role in [('source_missing', 'fetch'), ('source_read_incomplete', 'fetch'),
                           ('source_read_incomplete', 'current')]:
            with self.subTest(code=code, role=role):
                row = dict(id=101, page_id='synthetic-policy', title='합성 정책', db_key='synthetic',
                           notion_fields='{}', source_texts=json.dumps({URL: '예전에 저장한 충분한 정책 본문'}))
                with patch.object(ps, '_active_configured_keys', return_value=['synthetic']), \
                     patch.object(ps, '_db_query_strict', new=AsyncMock(return_value=[row])), \
                     patch.object(ps, '_db_write_strict', new=AsyncMock()) as save, \
                     patch.object(ps, '_persist_policy_review', new=AsyncMock()) as reviews, \
                     patch.object(policy_review_store, 'unresolved_by_policy', return_value={101: [{'code': code, 'source_role': role}]}), \
                     patch.object(ai_classifier, 'get_client', return_value=object()), \
                     patch.object(ai_classifier, 'extract_policy_fields_async', new=AsyncMock()) as extract:
                    result = await ps.run_initial_audit(['synthetic'])
                extract.assert_not_awaited()
                save.assert_not_awaited()
                reviews.assert_not_awaited()
                self.assertEqual(result['changed'], 0)
                self.assertGreater(result['review_count'], 0)
                self.assertFalse(ps.AUDIT_STATUS['running'])
