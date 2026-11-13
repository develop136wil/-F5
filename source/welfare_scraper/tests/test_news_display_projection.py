"""Readable news integration, offline fixtures only; run with isolated runner."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from services import news_display_projection as display
from services import news_gallery, notion_service


BODY = ('교육비 지원 안내\n개인이 신청합니다.\n'
        '지원 내용: 청소년에게 교재비 5만원을 지원합니다.\n'
        '신청 방법: 센터 방문 접수\n'
        '유의 사항: 다른 사업과 중복 신청할 수 없습니다.\n\n'
        'COPYRIGHT 2026 EXAMPLE ALL RIGHTS RESERVED.\n개인정보처리방침')
SECTIONS = [
    {'section': 'notes', 'quote': '교육비 지원 안내'},
    {'section': 'eligibility', 'quote': '개인이 신청합니다.'},
    {'section': 'support', 'quote': '지원 내용: 청소년에게 교재비 5만원을 지원합니다.'},
    {'section': 'application', 'quote': '신청 방법: 센터 방문 접수'},
    {'section': 'notes', 'quote': '유의 사항: 다른 사업과 중복 신청할 수 없습니다.'},
]


def metadata():
    return {'version': 1, 'source_sha256': hashlib.sha256(BODY.encode()).hexdigest(),
            'sections': deepcopy(SECTIONS)}


def stored_item():
    return {'id': 1, 'title': '교육비 지원', 'site_name': '합성 기관',
            'link': 'https://example.invalid/news/1', 'support_detail': BODY,
            'ai_data': {'support_detail': BODY, 'news_import_review': False,
                        'news_readable': metadata(), 'source_support_selection': {
                            'version': 3, 'source_sha256': metadata()['source_sha256'],
                            'unit_ids': [1], 'part_count': 1}}}


class NewsDisplayProjection(unittest.TestCase):
    def test_policy_prose_is_legacy_in_every_read_and_export_boundary(self):
        body='--- [게시글 본문] --- 담당자가 관리하는 정책\n### 지원 내용\n- 정책 지원 10만원'
        for mode in ('top-level','dict','json'):
            with self.subTest(mode=mode):
                metadata=dict(item_type='policy',support_detail=body,news_readable={'invalid':True})
                item=dict(id=1,support_detail=body,ai_data=metadata)
                if mode == 'json': item['ai_data']=json.dumps(metadata)
                if mode == 'top-level':
                    item['item_type']='policy'
                    item['ai_data']=dict(support_detail=body,news_readable={'invalid':True})
                conn=Mock()
                conn.execute.side_effect=AssertionError('Policy presentation cannot query receipts')
                expected=dict(support_detail=body,presentation_kind='legacy',source_note='')
                self.assertEqual(display.item_presentation(item,conn=conn),expected)
                self.assertEqual(news_gallery._details(item)[0][1],body)
                self.assertIn(body,news_gallery._copy_text(item))
                self.assertIn('정책 지원 10만원',json.dumps(notion_service.build_children(item,''),ensure_ascii=False))
                self.assertEqual(item['support_detail'],body)
                conn.execute.assert_not_called()
                self.assertEqual(display.item_presentation(dict(item,support_detail=''))['support_detail'],'')

    def test_projection_never_rewrites_saved_body_or_adds_raw_source_field(self):
        item = stored_item()
        original = deepcopy(item)
        result = display.item_presentation(item)
        self.assertEqual(item, original)
        self.assertEqual(result['presentation_kind'], 'structured')
        self.assertIn('교재비 5만원', result['support_detail'])
        self.assertIn('중복 신청할 수 없습니다.', result['support_detail'])
        self.assertNotIn('COPYRIGHT', result['support_detail'])
        self.assertNotIn('개인정보처리방침', result['support_detail'])
        self.assertNotIn('source_text', result)

    def test_json_metadata_and_dict_metadata_have_same_result(self):
        item = stored_item()
        self.assertEqual(display.item_presentation(item),
            display.item_presentation(dict(item, ai_data=json.dumps(item['ai_data']))))

    def test_explicit_empty_body_cannot_restore_old_ai_body(self):
        item = stored_item()
        item['support_detail'] = ''
        self.assertEqual(display.item_presentation(item)['support_detail'], '')

    def test_html_copy_and_notion_use_same_verified_body_and_keep_source_link(self):
        item = stored_item()
        expected = display.item_presentation(item)['support_detail']
        self.assertEqual(news_gallery._details(item)[0][1], expected)
        copied = news_gallery._copy_text(item)
        self.assertIn(expected, copied)
        self.assertIn(item['link'], copied)
        children = json.dumps(notion_service.build_children(item, ''), ensure_ascii=False)
        self.assertIn('교재비 5만원', children)
        self.assertIn('중복 신청할 수 없습니다.', children)
        self.assertIn(item['link'], children)
        self.assertNotIn('COPYRIGHT', children)
        self.assertNotIn('개인정보처리방침', copied)
        path = Path(news_gallery.generate_html([item]))
        try:
            document = path.read_text(encoding='utf-8')
            self.assertIn('교재비 5만원', document)
            self.assertNotIn('COPYRIGHT', document)
            self.assertIn('<strong>지원 내용</strong>', document)
        finally:
            path.unlink(missing_ok=True)

    def test_plain_legacy_markdown_is_unchanged(self):
        body = '### 지원 내용\n- 독서 프로그램\n### 신청 방법\n- 센터 방문'
        result = display.item_presentation({'support_detail': body})
        self.assertEqual(result['support_detail'], body)
        self.assertEqual(result['presentation_kind'], 'legacy')

    def test_import_review_false_does_not_hide_legacy_markdown(self):
        body = '### 지원 내용\n- 독서 프로그램\n### 신청 방법\n- 센터 방문'
        for flag in (False, True):
            item = dict(id=1, link='https://example.invalid/news/1', support_detail=body,
                        ai_data=dict(support_detail=body, news_import_review=flag))
            result = display.item_presentation(item)
            self.assertEqual(result['support_detail'], body)
            self.assertEqual(result['presentation_kind'], 'legacy')

    def test_current_metadata_does_not_query_receipts(self):
        conn = Mock()
        conn.execute.side_effect = AssertionError('Current imports must not query receipts')
        original = stored_item()
        copied = display.with_stored_provenance(original, conn)
        self.assertIsNot(copied, original)
        self.assertEqual(copied, original)
        self.assertEqual(display.item_presentation(original, conn=conn)['presentation_kind'], 'structured')
        conn.execute.assert_not_called()


class HistoricalImportProvenance(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.addCleanup(self.conn.close)
        self.conn.executescript('''
            CREATE TABLE welfare_items (id INTEGER PRIMARY KEY,link TEXT,support_detail TEXT);
            CREATE TABLE news_analysis_jobs (job_id TEXT PRIMARY KEY,state TEXT,recipe TEXT,
                input_json TEXT,result_json TEXT);
            CREATE TABLE news_result_imports (job_id TEXT,item_id INTEGER,link TEXT,outcome TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        ''')
        self.body = ('청년 지원 안내\n지원 내용: 교육비 10만원을 지원합니다.\n'
                     '신청 대상: 도봉구 거주 청년입니다.\n신청 방법: 센터 방문 신청\n'
                     '유의 사항: 같은 사업의 지원금은 중복 신청할 수 없습니다.\n'
                     'COPYRIGHT SYNTHETIC CENTER.')
        self.item = dict(id=7, link='https://example.invalid/old', support_detail=self.body,
                         ai_data=dict(support_detail=self.body, news_import_review=False))
        self.facts = dict(support_detail=self.body, fact_evidence=dict(support_detail=self.body),
            source_support_selection=dict(version=3, unit_ids=[1], part_count=1,
                source_sha256=hashlib.sha256(self.body.encode()).hexdigest()))
        self.conn.execute('INSERT INTO welfare_items VALUES (?,?,?)',
                          (self.item['id'], self.item['link'], self.body))
        self.conn.execute('INSERT INTO news_analysis_jobs VALUES (?,?,?,?,?)',
            ('old', 'complete', 'news-custom-v19-counted-complete-source',
             json.dumps(dict(link=self.item['link'], content=self.body)),
             json.dumps(dict(analysis=self.facts))))
        self.conn.execute('INSERT INTO news_result_imports(job_id,item_id,link,outcome) VALUES (?,?,?,?)',
                          ('old', self.item['id'], self.item['link'], 'created'))
        self.conn.commit()

    def test_old_whole_source_inherits_only_valid_receipt_metadata_without_writes(self):
        before = deepcopy(self.item)
        changes = self.conn.total_changes
        trace = []
        self.conn.set_trace_callback(trace.append)
        copied = display.with_stored_provenance(self.item, self.conn)
        self.assertEqual(self.item, before)
        self.assertEqual(copied['support_detail'], self.body)
        self.assertEqual(copied['ai_data']['source_support_selection'], self.facts['source_support_selection'])
        self.assertEqual(copied['ai_data']['news_import_review'], False)
        result = display.item_presentation(copied)
        self.assertEqual(result['presentation_kind'], 'source_excerpt')
        self.assertIn('교육비 10만원', result['support_detail'])
        self.assertNotIn('COPYRIGHT', result['support_detail'])
        self.assertEqual(result, display.item_presentation(self.item, conn=self.conn))
        self.assertEqual(self.conn.total_changes, changes)
        self.assertTrue(all(statement.lstrip().upper().startswith('SELECT') for statement in trace))
        self.assertEqual(set(result), {'support_detail', 'presentation_kind', 'source_note'})

    def test_user_edit_is_preserved_as_legacy(self):
        edited = '### 지원 내용\n담당자가 확인해 직접 작성한 안내입니다.'
        self.conn.execute('UPDATE welfare_items SET support_detail=? WHERE id=?', (edited, self.item['id']))
        item = dict(self.item, support_detail=edited)
        result = display.item_presentation(item, conn=self.conn)
        self.assertEqual(result['support_detail'], edited)
        self.assertEqual(result['presentation_kind'], 'legacy')
        self.assertNotIn('source_support_selection', display.with_stored_provenance(item, self.conn)['ai_data'])

    def test_empty_current_body_does_not_revive_receipt_body(self):
        item = dict(self.item, support_detail='')
        result = display.item_presentation(item, conn=self.conn)
        self.assertEqual(result['support_detail'], '')

    def test_historical_v13_markdown_with_import_flag_remains_legacy(self):
        body = '### 지원 내용\n- 지역 독서 프로그램'
        self.conn.execute('UPDATE welfare_items SET support_detail=?', (body,))
        self.conn.execute('UPDATE news_analysis_jobs SET recipe=?,input_json=?,result_json=?',
            ('news-custom-v13-source-spans-page6000',
             json.dumps(dict(link=self.item['link'], content=body)),
             json.dumps(dict(analysis=dict(support_detail=body)))))
        item = dict(self.item, support_detail=body, ai_data=dict(news_import_review=False, support_detail=body))
        result = display.item_presentation(item, conn=self.conn)
        self.assertEqual(result['support_detail'], body)
        self.assertEqual(result['presentation_kind'], 'legacy')

    def test_receipt_must_be_created_for_same_saved_item_link_and_complete_job(self):
        for statement, values in (
            ('UPDATE news_result_imports SET outcome=?', ('existing',)),
            ('UPDATE news_result_imports SET link=?', ('https://example.invalid/other',)),
            ('UPDATE news_result_imports SET item_id=?', (8,)),
            ('UPDATE news_analysis_jobs SET state=?', ('waiting',)),
            ('UPDATE welfare_items SET support_detail=?', ('user edited',)),
        ):
            with self.subTest(statement=statement):
                self.conn.execute('SAVEPOINT changed')
                self.conn.execute(statement, values)
                result = display.item_presentation(self.item, conn=self.conn)
                self.assertEqual(result['presentation_kind'], 'legacy')
                self.assertEqual(result['support_detail'], self.body)
                self.conn.execute('ROLLBACK TO changed')
                self.conn.execute('RELEASE changed')

    def test_input_source_and_analysis_source_and_hash_must_all_match(self):
        for change in ('input_link', 'input_body', 'analysis_body', 'source_hash', 'evidence_body'):
            with self.subTest(change=change):
                facts = deepcopy(self.facts)
                payload = dict(link=self.item['link'], content=self.body)
                if change == 'input_link':
                    payload['link'] += '?other'
                elif change == 'input_body':
                    payload['content'] += '\n추가 조건'
                elif change == 'analysis_body':
                    facts['support_detail'] += '\n추가 조건'
                elif change == 'source_hash':
                    facts['source_support_selection']['source_sha256'] = '0' * 64
                else:
                    facts['fact_evidence']['support_detail'] = 'different source'
                self.conn.execute('UPDATE news_analysis_jobs SET input_json=?,result_json=?',
                                  (json.dumps(payload), json.dumps(dict(analysis=facts))))
                self.assertEqual(display.item_presentation(self.item, conn=self.conn)['presentation_kind'], 'legacy')

    def test_ordinary_legacy_policy_and_invalid_ids_do_not_query(self):
        conn = Mock()
        conn.execute.side_effect = AssertionError('No receipt lookup expected')
        for item in (
            dict(self.item, ai_data={}), dict(self.item, item_type='policy'),
            dict(self.item, id=True), dict(self.item, id='7'), dict(self.item, link=''),
            dict(self.item, support_detail=''),
        ):
            display.item_presentation(item, conn=conn)
        conn.execute.assert_not_called()

    def test_missing_tables_preserve_legacy_without_migrations(self):
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        result = display.item_presentation(self.item, conn=conn)
        self.assertEqual(result['presentation_kind'], 'legacy')
        self.assertEqual(result['support_detail'], self.body)
        self.assertEqual(conn.execute('SELECT name FROM sqlite_master').fetchall(), [])

    def test_no_connection_means_no_receipt_resolution(self):
        result = display.item_presentation(self.item)
        self.assertEqual(result['presentation_kind'], 'legacy')
        self.assertEqual(result['support_detail'], self.body)


# The fixture sets its own disposable DB. The outer runner also isolates the
# API import, configuration, startup DB and keys before this module is loaded.
from tests import test_news_result_import as import_fixtures
from tests.test_news_source_selection import response
import database
from services import news_profile_store as profiles, news_analysis_store as jobs
from services import news_custom_analysis as custom, news_source_selection as selection
from services import page_coverage, news_result_import as imports, news_unified_feed as feed


class NewsReadableRoundTrip(unittest.TestCase):
    setUp = import_fixtures.TestNewsResultImport.setUp
    tearDown = import_fixtures.TestNewsResultImport.tearDown
    new_job = import_fixtures.TestNewsResultImport.new_job

    def test_new_result_saved_item_and_exports_keep_one_readable_body_without_reanalysis(self):
        raw = response()
        raw.update(applicant_type='individual', applicant_evidence='개인이 신청합니다.',
                   applicant_detail='개인', beneficiary_detail='청소년', target='청소년')
        facts = selection.materialize(raw, BODY, extended=True, complete_document=True)
        facts.update(news_readable=metadata(), source_page=page_coverage.make(),
                     source_input={'status': 'complete'})
        payload = dict(title='교육비 지원', site_name='합성 기관', content=BODY,
                       link='https://example.invalid/readable', source_page=page_coverage.make())
        with database.get_connection() as conn:
            run = profiles.prepare_run(conn, 'p', expected_revision=1,
                sites=[dict(id=1, name='합성 기관', url='https://example.invalid')])
            job = jobs.enqueue(conn, run, payload=payload, model='offline', recipe=custom.COMPLETE_RECIPE)
            jobs.complete(conn, job, jobs.claim(conn, job, now=0),
                          {'analysis': facts, 'target_status': 'review'})
            original = conn.execute('SELECT result_json,input_json FROM news_analysis_jobs WHERE job_id=?', (job,)).fetchone()
            new = jobs.get_profile_result_details(conn, 'p', 'job:' + job)
            self.assertEqual(new['presentation_kind'], 'structured')
            self.assertNotIn('COPYRIGHT', new['details']['support_detail'])
            self.assertFalse(new['ai_called'])
            preview = imports.preview(conn, 'p', job)
            receipt = imports.import_result(conn, 'p', job, fingerprint=preview['fingerprint'], confirmed=True)
            saved = dict(conn.execute('SELECT * FROM welfare_items WHERE id=?', (receipt['item_id'],)).fetchone())
            self.assertEqual(saved['support_detail'], BODY)
            self.assertEqual(json.loads(saved['ai_data'])['news_readable'], metadata())
            visible = next(row for row in feed.read_feed(conn, 'p')['items'] if row['item_id'] == receipt['item_id'])
            self.assertEqual(visible['support_detail'], new['details']['support_detail'])
            self.assertEqual(conn.execute('SELECT result_json,input_json FROM news_analysis_jobs WHERE job_id=?', (job,)).fetchone(), original)
        detail = self.client.get('/api/data/' + str(receipt['item_id']))
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()['news_presentation']['support_detail'], new['details']['support_detail'])
        self.assertEqual(news_gallery._details(saved)[0][1], new['details']['support_detail'])

    def test_historical_import_read_and_export_boundaries_recover_provenance_without_writing(self):
        from services import notion_upload_state
        old_body = BODY.replace('개인이 신청합니다.', '신청 대상: 개인이 신청합니다.')
        raw = response()
        raw.update(applicant_type='individual', applicant_evidence='개인이 신청합니다.',
                   applicant_detail='개인', beneficiary_detail='청소년', target='청소년')
        facts = selection.materialize(raw, old_body, extended=True, complete_document=True)
        facts.update(source_page=page_coverage.make(), source_input={'status': 'complete'})
        payload = dict(title='이전 교육비 지원', site_name='합성 기관', content=old_body,
                       link='https://example.invalid/historical-readable', source_page=page_coverage.make())
        with database.get_connection() as conn:
            run = profiles.prepare_run(conn, 'p', expected_revision=1,
                sites=[dict(id=1, name='합성 기관', url='https://example.invalid')])
            job = jobs.enqueue(conn, run, payload=payload, model='offline', recipe=custom.COMPLETE_RECIPE)
            jobs.complete(conn, job, jobs.claim(conn, job, now=0),
                          {'analysis': facts, 'target_status': 'review'})
            preview = imports.preview(conn, 'p', job)
            receipt = imports.import_result(conn, 'p', job, fingerprint=preview['fingerprint'], confirmed=True)
            item_id = receipt['item_id']
            saved = dict(conn.execute('SELECT * FROM welfare_items WHERE id=?', (item_id,)).fetchone())
            historical_ai = json.loads(saved['ai_data'])
            historical_ai.pop('source_support_selection', None)
            historical_ai.pop('news_readable', None)
            conn.execute('UPDATE welfare_items SET ai_data=? WHERE id=?',
                         (json.dumps(historical_ai, ensure_ascii=False), item_id))
            conn.commit()
            saved = dict(conn.execute('SELECT * FROM welfare_items WHERE id=?', (item_id,)).fetchone())
            before_item = tuple(conn.execute('SELECT * FROM welfare_items WHERE id=?', (item_id,)).fetchone())
            before_job = tuple(conn.execute('SELECT input_json,result_json FROM news_analysis_jobs WHERE job_id=?', (job,)).fetchone())
            before_receipt = tuple(conn.execute('SELECT * FROM news_result_imports WHERE job_id=?', (job,)).fetchone())
            expected = display.item_presentation(saved, conn=conn)
            self.assertEqual(expected['presentation_kind'], 'source_excerpt')
            self.assertIn('교재비 5만원', expected['support_detail'])
            self.assertNotEqual(expected['support_detail'], old_body)
            visible = next(row for row in feed.read_feed(conn, 'p')['items'] if row['item_id'] == item_id)
            self.assertEqual(visible['support_detail'], expected['support_detail'])
            self.assertNotIn('COPYRIGHT', visible['support_detail'])
        detail = self.client.get('/api/data/' + str(item_id))
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()['news_presentation'], expected)
        self.assertNotIn('source_text', detail.json()['news_presentation'])

        # Exercise each production _load query. Stub only outbound/browser and
        # upload-receipt mutations, keeping actual HTML/Notion body construction.
        fake_engine = SimpleNamespace(close=AsyncMock())
        create_page = AsyncMock(return_value={'ok': True, 'page_id': 'offline-only'})
        with patch('api_server._poster_for_news_item', new=AsyncMock(return_value=('', True))), \
                patch('scraper_engine.ScraperEngine', return_value=fake_engine), \
                patch.object(notion_service, 'is_configured', return_value=True), \
                patch.object(notion_service, 'create_event_page', new=create_page), \
                patch.object(notion_upload_state, 'claim', return_value={'state': 'claimed', 'link': payload['link']}), \
                patch.object(notion_upload_state, 'complete', return_value=True), \
                patch('ai_classifier._call_gemini_json', new=AsyncMock(side_effect=AssertionError('No AI during projection'))):
            html = self.client.post('/api/news/gallery-html', json={'ids': [item_id]})
            self.assertEqual(html.status_code, 200, html.text)
            self.assertEqual(html.headers['X-WelfareF5-AI-Calls'], '0')
            self.assertIn('교재비 5만원', html.text)
            self.assertNotIn('COPYRIGHT', html.text)
            self.assertNotIn('개인정보처리방침', html.text)
            upload = self.client.post('/api/notion/upload', json={'ids': [item_id]})
            self.assertEqual(upload.status_code, 200, upload.text)
            self.assertEqual(upload.json()['uploaded'], 1)
        create_page.assert_awaited_once()
        selected_item = create_page.await_args.args[0]
        self.assertIn('source_support_selection', selected_item['ai_data'])
        self.assertEqual(display.item_presentation(selected_item), expected)
        children = json.dumps(notion_service.build_children(selected_item, ''), ensure_ascii=False)
        self.assertIn('교재비 5만원', children)
        self.assertNotIn('COPYRIGHT', children)
        with database.get_connection() as conn:
            self.assertEqual(tuple(conn.execute('SELECT * FROM welfare_items WHERE id=?', (item_id,)).fetchone()), before_item)
            self.assertEqual(tuple(conn.execute('SELECT input_json,result_json FROM news_analysis_jobs WHERE job_id=?', (job,)).fetchone()), before_job)
            self.assertEqual(tuple(conn.execute('SELECT * FROM news_result_imports WHERE job_id=?', (job,)).fetchone()), before_receipt)
