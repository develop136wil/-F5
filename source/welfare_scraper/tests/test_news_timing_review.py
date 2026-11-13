"""Offline timing holds through read-only review and explicit local import.

Synthetic disposable DB/API fixtures and one immutable public response replay;
no model, source fetch, Notion write, or new model-accuracy claim is involved.
"""
import asyncio
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import ai_classifier
import database
from services import news_gallery as gallery, news_result_import as imports
from services import news_timing as timing, notion_service as notion
from services.news_analysis_safety import assess
from services.news_profile import NewsProfile
from tests import test_news_result_import as import_fixture


def item(**values):
    return {'title': '2000년 소식', 'site_name': '시험 기관',
            'link': 'https://example.invalid/timing', 'support_detail': '본문 보존',
            **values}


class TestNewsTimingPure(unittest.TestCase):
    def assert_held(self, value, note):
        before = copy.deepcopy(value)
        self.assertEqual(timing.timing_review_note(value), note)
        self.assertEqual(notion.compute_period(value), ('', ''))
        self.assertEqual(notion.compute_status(value), '기간 확인 필요')
        self.assertEqual(gallery._display_date(value), '일정 확인 필요')
        self.assertIn(note, gallery._copy_text(value))
        self.assertEqual(value, before)

    def test_explicit_code_wins_over_any_old_title_or_other_valid_date(self):
        for deadline in (None, '2000-01-01', '2099-12-31'):
            for serialized in (False, True):
                with self.subTest(deadline=deadline, serialized=serialized):
                    metadata = {'news_timing_review': 'deadline_unverified',
                                'deadline': 'REJECTED VALUE DO NOT DISPLAY',
                                'note': 'UNTRUSTED PROVIDER MESSAGE'}
                    value = item(deadline=deadline, event_date='2099-01-01',
                                 ai_data=json.dumps(metadata) if serialized else metadata)
                    self.assert_held(value, timing.TIMING_NOTE)
                    text = gallery._copy_text(value)
                    self.assertNotIn('REJECTED VALUE', text)
                    self.assertNotIn('UNTRUSTED PROVIDER', text)

    def test_manual_or_import_markers_keep_missing_dates_unknown_not_ended(self):
        for marker in ({'manual_news': True}, {'manual_news': 1},
                       {'ai_data': {'news_import_review': True}},
                       {'ai_data': {'news_import_review': False}},
                       {'ai_data': '{"news_import_review":false}'},
                       {'manual_news': True, 'ai_data': '{invalid-json'}):
            with self.subTest(marker=marker):
                self.assert_held(item(deadline=None, event_date='', event_end_date=None,
                                      **marker), timing.UNKNOWN_NOTE)

    def test_strict_date_validation_rejects_bad_calendar_clock_and_multiple_dates(self):
        bad = ('2026-02-30', '2026-13-01', '2026-09-14 24:00',
               '2026-09-14 18:60', '2026-09-14 18:00:00',
               '2026-09-14T18:00+09:00', '2026-9-14',
               '2026/09/14', '2026-09-14 extra',
               '1차 2000-01-01 / 2차 2099-01-01', True, 20260914, [], {})
        for field in ('deadline', 'event_end_date', 'event_date'):
            for date in bad:
                with self.subTest(field=field, date=date):
                    value = item(deadline='2099-01-01', ai_data={'news_import_review': False})
                    value[field] = date
                    self.assert_held(value, timing.UNKNOWN_NOTE)

    def test_unrelated_review_does_not_erase_a_strict_valid_date_or_change_status(self):
        today = dt.date.today()
        for review in (True, False):
            for date, status in ((today.isoformat(), '진행 중'),
                                 ((today - dt.timedelta(days=1)).isoformat(), '종료'),
                                 ('2099-12-31 18:30', '진행 중'),
                                 ('2099-12-31T18:30', '진행 중')):
                with self.subTest(review=review, date=date):
                    value = item(deadline=date, ai_data={
                        'news_import_review': review,
                        'source_attachments': {'status': 'review_required'},
                        'unverified_facts': {'target_characteristics': ['다른 검토']}})
                    self.assertEqual(timing.timing_review_note(value), '')
                    self.assertEqual(notion.compute_period(value), (date[:10], ''))
                    self.assertEqual(notion.compute_status(value), status)
                    self.assertEqual(gallery._display_date(value), date)
                    payload = notion.build_payload(value, '', 'test-db')
                    self.assertEqual(payload['properties']['기간']['date']['start'], date[:10])
                    self.assertEqual(payload['properties']['선택']['select']['name'], status)

    def test_valid_date_fields_keep_existing_deadline_priority_and_future_start(self):
        value = item(deadline='2099-12-31', event_date='2099-12-01',
                     event_end_date='2099-12-10', manual_news=1)
        self.assertEqual(timing.timing_review_note(value), '')
        self.assertEqual(notion.compute_period(value), ('2099-12-31', ''))
        self.assertEqual(notion.compute_status(value), '진행 예정')

    def test_non_boolean_and_forged_markers_do_not_expand_manual_news_scope(self):
        for marker in ({}, {'manual_news': 'true'}, {'manual_news': '1'},
                       {'ai_data': {'manual_news': True}},
                       {'ai_data': {'news_import_review': 'false'}},
                       {'ai_data': {'news_import_review': 1}},
                       {'ai_data': {'news_timing_review': 'arbitrary reason'}},
                       {'ai_data': '{invalid-json'}):
            with self.subTest(marker=marker):
                value = item(**marker)
                self.assertEqual(timing.timing_review_note(value), '')
                self.assertEqual(notion.compute_status(value), '종료')

    def test_notion_omits_unknown_select_and_period_but_keeps_warning_with_poster(self):
        for metadata, note in (({'news_timing_review': 'deadline_unverified'}, timing.TIMING_NOTE),
                               ({'news_import_review': False}, timing.UNKNOWN_NOTE)):
            for poster in ('', 'https://example.invalid/poster.jpg'):
                with self.subTest(metadata=metadata, poster=bool(poster)):
                    value = item(ai_data=metadata)
                    before = copy.deepcopy(value)
                    payload = notion.build_payload(value, poster, 'test-db')
                    self.assertNotIn('선택', payload['properties'])
                    self.assertNotIn('기간', payload['properties'])
                    self.assertIn(note, json.dumps(payload['children'], ensure_ascii=False))
                    self.assertEqual(value, before)

    def test_import_end_check_receives_timing_metadata_not_title_alone(self):
        self.assertFalse(imports._ended(item(), {
            'deadline': '', 'news_import_review': True,
            'news_timing_review': 'deadline_unverified'}))
        self.assertFalse(imports._ended(item(), {'deadline': '', 'news_import_review': False}))
        self.assertTrue(imports._ended(item(), {'deadline': '2000-01-01', 'news_import_review': True}))


class TestNewsTimingImport(unittest.TestCase):
    # Reuse fixtures, not the TestCase base: do not collect all old tests twice.
    setUp = import_fixture.TestNewsResultImport.setUp
    tearDown = import_fixture.TestNewsResultImport.tearDown
    new_job = import_fixture.TestNewsResultImport.new_job
    preview = import_fixture.TestNewsResultImport.preview
    commit = import_fixture.TestNewsResultImport.commit

    def snapshot(self):
        with database.get_connection() as conn:
            return {table: [tuple(row) for row in conn.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]
                    for table in ('news_analysis_jobs', 'news_analysis_cache', 'news_result_imports')}

    def test_multisession_assess_import_candidate_notion_and_gallery_keep_hold(self):
        raw = self.facts | {'deadline': '2000-01-01'}
        raw_before = copy.deepcopy(raw)
        payload = self.payload | {'title': '2000년 회차별 돌봄 소식',
            'content': self.payload['content'] + '\n1차 접수 마감: 2000-01-01\n2차 접수 마감: 2099-01-01'}
        assessed = assess(NewsProfile('p', 1, '노년', '전국'), raw, payload['content'])
        self.assertEqual(raw, raw_before)
        self.assertEqual(assessed['analysis']['deadline'], '')
        self.assertEqual(assessed['analysis']['unverified_facts']['deadline'], raw['deadline'])
        self.job = self.new_job(facts=assessed['analysis'], payload=payload)
        self.url = f'/api/news/profiles/p/jobs/{self.job}'
        before = self.snapshot()
        with patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(
                side_effect=AssertionError('No AI during local timing review'))) as ai, \
             patch.object(notion, 'is_configured', return_value=False):
            for _ in range(2):
                preview = self.preview()
                self.assertEqual(preview['status'], 'ready')
                self.assertIn(timing.TIMING_NOTE, preview['note'])
                self.assertFalse(preview['ai_called'])
                self.assertEqual(self.snapshot(), before)
            response = self.commit(preview['fingerprint'])
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['status'], 'created')
            item_id = response.json()['item_id']
            saved = database.execute_query('SELECT * FROM welfare_items WHERE id=?',
                                           (item_id,), fetch_one=True)
            metadata = json.loads(saved['ai_data'])
            self.assertEqual(metadata['news_timing_review'], 'deadline_unverified')
            self.assertNotIn('unverified_facts', metadata)
            self.assertNotIn(raw['deadline'], json.dumps(metadata))
            self.assertIsNone(saved['deadline'])
            after_import = self.snapshot()
            self.assertEqual(after_import['news_analysis_jobs'], before['news_analysis_jobs'])
            self.assertEqual(after_import['news_analysis_cache'], before['news_analysis_cache'])
            self.assertEqual(len(after_import['news_result_imports']), 1)
            candidates = self.client.get('/api/notion/candidates')
            self.assertEqual(candidates.status_code, 200, candidates.text)
            candidate = next(value for value in candidates.json()['items'] if value['id'] == item_id)
            self.assertEqual(candidate['status'], '기간 확인 필요')
            self.assertEqual((candidate['start'], candidate['end']), ('', ''))
            self.assertTrue(candidate['manual_news'])
            self.assertIn(timing.TIMING_NOTE, candidate['input_note'])
            notion_payload = notion.build_payload(saved, 'https://example.invalid/poster.jpg', 'test-db')
            self.assertNotIn('기간', notion_payload['properties'])
            self.assertNotIn('선택', notion_payload['properties'])
            self.assertIn(timing.TIMING_NOTE, json.dumps(notion_payload, ensure_ascii=False))
            with patch('api_server._poster_for_news_item', new=AsyncMock(return_value=('', True))):
                html = self.client.post('/api/news/gallery-html', json={'ids': [item_id]})
            self.assertEqual(html.status_code, 200, html.text)
            self.assertIn('일정 확인 필요', html.text)
            self.assertIn('기간 확인 필요', html.text)
            self.assertIn(timing.TIMING_NOTE, html.text)
            self.assertIn(timing.TIMING_NOTE, gallery._copy_text(saved))
            self.assertEqual(self.commit().json()['status'], 'already_imported')
            self.assertEqual(self.snapshot(), after_import)
            self.assertEqual(database.execute_query('SELECT * FROM welfare_items WHERE id=?',
                                                   (item_id,), fetch_one=True), saved)
        ai.assert_not_awaited()

    def test_valid_past_deadline_still_ends_despite_unrelated_review(self):
        self.job = self.new_job(facts=self.facts | {'deadline': '2000-01-01'},
            payload=self.payload | {'content': self.payload['content'] + '\n접수 마감: 2000-01-01'})
        self.url = f'/api/news/profiles/p/jobs/{self.job}'
        before = self.snapshot()
        self.assertEqual(self.preview()['status'], 'ended')
        self.assertEqual(self.commit().status_code, 409)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(database.execute_query('SELECT COUNT(*) AS n FROM welfare_items', fetch_one=True)['n'], 0)

    def test_arbitrary_saved_marker_does_not_become_an_import_timing_reason(self):
        self.job = self.new_job(facts=self.facts | {
            'deadline': '2099-01-01', 'news_timing_review': 'RAW MODEL NOTE'},
            payload=self.payload | {'content': self.payload['content'] + '\n접수 마감: 2099-01-01'})
        self.url = f'/api/news/profiles/p/jobs/{self.job}'
        response = self.commit()
        self.assertEqual(response.status_code, 200, response.text)
        saved = database.execute_query('SELECT * FROM welfare_items', fetch_one=True)
        metadata = json.loads(saved['ai_data'])
        self.assertNotIn('news_timing_review', metadata)
        self.assertNotIn('RAW MODEL NOTE', json.dumps(metadata))
        self.assertEqual(notion.compute_period(saved), ('2099-01-01', ''))

    def test_receipt_marker_reaches_direct_upload_when_old_metadata_is_missing(self):
        result = self.commit()
        self.assertEqual(result.status_code, 200, result.text)
        item_id = result.json()['item_id']
        for metadata in ('{}', '{invalid-json'):
            with self.subTest(metadata=metadata):
                database.execute_write('UPDATE welfare_items SET ai_data=? WHERE id=?', (metadata, item_id))
                before = self.snapshot()
                saved = database.execute_query('SELECT * FROM welfare_items WHERE id=?',
                                               (item_id,), fetch_one=True)
                observed = []

                async def create(value, poster):
                    observed.append((copy.deepcopy(value), notion.build_payload(value, poster, 'test-db')))
                    # Synthetic definitive local rejection releases only the
                    # fixture claim. No real page creation or success is faked.
                    return {'ok': False, 'code': 'NOTION_PAYLOAD_INVALID',
                            'error': 'synthetic unsent result',
                            'request_sent': False, 'uncertain': False}

                engine = MagicMock()
                engine.close = AsyncMock()
                with patch('scraper_engine.ScraperEngine', return_value=engine), \
                     patch.object(notion, 'is_configured', return_value=True), \
                     patch.object(notion, 'create_event_page', new=AsyncMock(side_effect=create)) as send, \
                     patch.object(import_fixture.api_server, '_poster_for_news_item',
                                  new=AsyncMock(return_value=('', True))):
                    response = asyncio.run(import_fixture.api_server._notion_upload_locked(
                        import_fixture.api_server.NotionUploadRequest(ids=[item_id])))
                send.assert_awaited_once()
                engine.start.assert_not_called()
                engine.close.assert_awaited_once()
                self.assertEqual(response['uploaded'], 0)
                self.assertEqual(len(observed), 1)
                value, payload = observed[0]
                self.assertEqual(value['manual_news'], 1)
                self.assertNotIn('기간', payload['properties'])
                self.assertNotIn('선택', payload['properties'])
                self.assertIn(timing.UNKNOWN_NOTE, json.dumps(payload['children'], ensure_ascii=False))
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(database.execute_query('SELECT * FROM welfare_items WHERE id=?',
                                                       (item_id,), fetch_one=True), saved)


class TestFrozenMemorialTimingReplay(unittest.TestCase):
    def test_existing_raw_response_is_withheld_without_cutting_or_rewriting_evidence(self):
        from services.news_source_selection import materialize
        root = Path(__file__).resolve().parents[2] / 'artifacts'
        ledger_path = root / 'ai-retest-20260914/fresh-live-01/ledger.jsonl'
        source_path = root / 'ai-holdout-prep-20260913/sources/news-memorial-school.txt'
        ledger_bytes, source_bytes = ledger_path.read_bytes(), source_path.read_bytes()
        self.assertEqual(hashlib.sha256(ledger_bytes).hexdigest(),
                         'ea955497e7521b89a092350b91b3094a477a3e336f34a88491af9e5a5e68809b')
        self.assertEqual(hashlib.sha256(source_bytes).hexdigest(),
                         'fabf744aa848e01ab473c4eb341c85367fb1dd89219f929bb4fd397d95e1506e')
        events = [json.loads(line) for line in ledger_bytes.decode('utf-8').splitlines()]
        response = next(value for value in events if value.get('case_id') == 'news-memorial-school'
                        and value['event'] == 'response')
        previous = next(value for value in events if value.get('case_id') == 'news-memorial-school'
                        and value['event'] == 'case_result')
        raw = json.loads(response['raw']['text'])
        raw_before = copy.deepcopy(raw)
        self.assertEqual(raw['deadline'], '2026-11-24 17:00')
        source = source_bytes.decode('utf-8')
        page = copy.deepcopy(previous['decision']['analysis']['source_page'])
        facts = materialize(raw, source, extended=True)
        facts['source_page'] = page
        profile = NewsProfile('offline-replay', 1, '고정 원문 검증', '전국')
        payload = {'content': source, 'source_page': copy.deepcopy(previous['source_page'])}
        for _ in range(2):
            before = copy.deepcopy(facts)
            reviewed = assess(profile, facts, source, require_source_selection=True,
                              whole_document_rules=True, source_payload=payload)
            self.assertEqual(facts, before)
            facts = reviewed['analysis']
            self.assertEqual(facts['deadline'], '')
            self.assertEqual(facts['unverified_facts']['deadline'], raw['deadline'])
            self.assertEqual(facts['support_detail'], source)
            # Preserve the frozen evidence and reason codes, but use today's
            # code-owned explanatory copy rather than a historical UI sentence.
            from services.page_coverage import normalize
            self.assertEqual(facts['source_page'], normalize(page))
            self.assertFalse(reviewed['accepted'])
            self.assertTrue(reviewed['review_required'])
        self.assertEqual(raw, raw_before)
        self.assertEqual(ledger_path.read_bytes(), ledger_bytes)
        self.assertEqual(source_path.read_bytes(), source_bytes)
