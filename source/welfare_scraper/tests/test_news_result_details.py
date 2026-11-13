"""Synthetic-only bounded news display. No HTTP providers, live DB or AI calls."""
import json
import sqlite3
import unittest
from unittest.mock import AsyncMock, patch

from services import news_analysis_store as jobs, news_profile_store as profiles
from services import news_custom_analysis as custom, news_official_results as official
from services.news_profile import NewsProfile


class NewsResultDetails(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        profiles.ensure_schema(self.conn)
        jobs.ensure_schema(self.conn)
        official.ensure_schema(self.conn)
        for profile_id in ('p', 'other'):
            profiles.create_profile(self.conn, NewsProfile(profile_id, 1, '합성 조건', '전국', site_ids=('1',)))
        self.run = profiles.prepare_run(self.conn, 'p', expected_revision=1,
            sites=[dict(id=1, name='합성 기관', url='https://example.invalid')])

    def tearDown(self):
        self.conn.close()

    def source(self, length):
        head, tail = '교육 지원 안내\n개인이 신청합니다.\n', '\n끝 조건: 중복 지원은 제외합니다.'
        return head + '가' * (length - len(head) - len(tail)) + tail

    def make_job(self, length=4100, *, recipe=None, state='complete'):
        source = self.source(length)
        payload = dict(title=f'합성 자료 {length}', site_name='합성 기관', content=source,
                       link=f'https://example.invalid/{length}')
        job = jobs.enqueue(self.conn, self.run, payload=payload, model='offline', recipe=recipe or custom.SOURCE_SPAN_RECIPE)
        if state == 'complete':
            analysis = dict(is_relevant=True, category='교육', target='이용자', residence=[], life_cycle=[],
                target_characteristics=[], interest_topics=[], start_age_months=None, end_age_months=None,
                evidence_snippet='교육 지원 안내', applicant_type='individual', applicant_evidence='개인이 신청합니다.',
                applicant_detail='개인', beneficiary_detail='이용자', life_cycle_evidence=[], residence_evidence=[],
                support_detail=source, eligibility_detail='', contact='', deadline='',
                fact_evidence=dict(support_detail=source, eligibility_detail='', contact='', deadline='', is_relevant='교육 지원 안내'),
                secret='provider-private', unverified_facts={'contact':'withheld-private'})
            jobs.complete(self.conn, job, jobs.claim(self.conn, job, now=0),
                dict(analysis=analysis, target_status='review', raw_response='raw-private'))
        return job, source

    def test_long_tail_roundtrip_without_rewriting_or_private_projection(self):
        job, source = self.make_job()
        before = self.conn.execute('SELECT input_json,result_json FROM news_analysis_jobs WHERE job_id=?', (job,)).fetchone()
        listing = jobs.list_profile_results(self.conn, 'p')['items'][0]
        self.assertNotIn('support_detail', listing['details'])
        self.assertEqual(listing['detail_omissions']['support_detail'], dict(length=4100, available=True, reason='deferred'))
        with patch.object(jobs, 'get_job', wraps=jobs.get_job) as read:
            full = jobs.get_profile_result_details(self.conn, 'p', f'job:{job}')
        self.assertEqual(read.call_count, 1)
        self.assertEqual(full['details']['support_detail'], source)
        self.assertTrue(full['details']['support_detail'].endswith('중복 지원은 제외합니다.'))
        self.assertFalse(full['ai_called'])
        for private in ('provider-private', 'withheld-private', 'raw-private', 'input_json', 'fact_evidence', 'unverified_facts'):
            self.assertNotIn(private, json.dumps(full, ensure_ascii=False))
        self.assertEqual(before, self.conn.execute('SELECT input_json,result_json FROM news_analysis_jobs WHERE job_id=?', (job,)).fetchone())

    def test_full_projection_exact_boundaries_and_no_cutoff(self):
        for length in (4000, 4001, 16000, 16001, 30000, 30001):
            with self.subTest(length=length):
                value = self.source(length)
                full, omitted = jobs._project_details({'support_detail':value, 'raw_response':'private'})
                if length <= 30000:
                    self.assertEqual(full, {'support_detail':value})
                    self.assertEqual(omitted, {})
                else:
                    self.assertEqual(full, {})
                    self.assertEqual(omitted['support_detail'], dict(length=length, available=False, reason='display_limit'))
        full, omitted = jobs._project_details({key:'\x00' * 30000 for key in jobs.DETAIL_FIELDS})
        self.assertLess(len(json.dumps(full).encode()), 2 * 1024 * 1024)
        self.assertEqual(omitted, {})

    def test_verified_long_support_survives_actual_detail_reader(self):
        for length in (16001, 30000):
            with self.subTest(length=length):
                job, source = self.make_job(length)
                full = jobs.get_profile_result_details(self.conn, 'p', f'job:{job}')
                self.assertEqual(full['details']['support_detail'], source)
                self.assertNotIn('support_detail', jobs.list_profile_results(self.conn, 'p')['items'][0]['details'])

    def test_whole_fields_and_byte_budget_do_not_change_pagination(self):
        # Synthetic stored legacy fields isolate projection cost from grounding.
        for index in range(101):
            job = jobs.enqueue(self.conn, self.run, payload=dict(title=str(index), content='private-source',
                link=f'https://example.invalid/{index}', site_name='합성'), model='offline', recipe='historical')
            jobs.complete(self.conn, job, jobs.claim(self.conn, job, now=0),
                {'analysis':{key:'가' * 2000 for key in jobs.DETAIL_FIELDS}, 'target_status':'review'})
        first = jobs.list_profile_results(self.conn, 'p', limit=100)
        self.assertEqual(len(first['items']), 100)
        self.assertTrue(first['has_more'])
        inline_bytes = sum(len(value.encode()) for item in first['items'] for value in item['details'].values())
        self.assertLessEqual(inline_bytes, jobs.DETAIL_LIST_PAGE_BYTES)
        self.assertGreater(inline_bytes, jobs.DETAIL_LIST_PAGE_BYTES - 6000)
        for item in first['items']:
            self.assertLessEqual(sum(len(v.encode()) for v in item['details'].values()), jobs.DETAIL_LIST_ROW_BYTES)
            self.assertTrue(all(value == '가' * 2000 for value in item['details'].values()))
            self.assertTrue(item['detail_omissions'])
        self.assertLess(len(json.dumps(first, ensure_ascii=False).encode()), 2 * 1024 * 1024)
        second = jobs.list_profile_results(self.conn, 'p', limit=100, offset=100)
        self.assertEqual(len(second['items']), 1)
        self.assertFalse(second['has_more'])
        self.assertFalse({x['record_id'] for x in first['items']} & {x['record_id'] for x in second['items']})

    def test_profile_scope_precedes_grounding_and_unknown_records_fail(self):
        job, _ = self.make_job()
        with patch.object(jobs, 'get_job', wraps=jobs.get_job) as read:
            with self.assertRaises(KeyError):
                jobs.get_profile_result_details(self.conn, 'other', f'job:{job}')
            read.assert_not_called()
        for record in ('job:missing', 'official:missing', 'candidate:any', 'bad', 'job:' + 'x' * 501):
            with self.subTest(record=record), self.assertRaises((KeyError, ValueError)):
                jobs.get_profile_result_details(self.conn, 'p', record)

    def test_waiting_unsupported_and_failed_revalidation_cannot_expand(self):
        for options in ({'state':'queued'}, {'recipe':'unknown'}):
            job, _ = self.make_job(**options)
            with self.assertRaises(ValueError):
                jobs.get_profile_result_details(self.conn, 'p', f'job:{job}')
        job, _ = self.make_job(4200)
        with patch('services.news_analysis_safety.assess', side_effect=ValueError('provider-private')):
            with self.assertRaises(ValueError):
                jobs.get_profile_result_details(self.conn, 'p', f'job:{job}')

    def test_source_mutation_is_rechecked_and_withheld_body_not_restored(self):
        job, source = self.make_job()
        row = json.loads(self.conn.execute('SELECT input_json FROM news_analysis_jobs WHERE job_id=?', (job,)).fetchone()[0])
        row['content'] = '교육 지원 안내\n개인이 신청합니다. 원문이 달라졌습니다.'
        self.conn.execute('UPDATE news_analysis_jobs SET input_json=? WHERE job_id=?', (json.dumps(row), job))
        full = jobs.get_profile_result_details(self.conn, 'p', f'job:{job}')
        self.assertNotIn('support_detail', full['details'])
        self.assertNotIn(source, json.dumps(full, ensure_ascii=False))

    def test_invalid_saved_analysis_never_displays_small_unchecked_prose(self):
        job, _ = self.make_job(100)
        saved = json.loads(self.conn.execute('SELECT result_json FROM news_analysis_jobs WHERE job_id=?', (job,)).fetchone()[0])
        saved['analysis'].update(support_detail='원문에 없는 허위 지원 문장')
        self.conn.execute('UPDATE news_analysis_jobs SET result_json=? WHERE job_id=?', (json.dumps(saved), job))
        payload = json.loads(self.conn.execute('SELECT input_json FROM news_analysis_jobs WHERE job_id=?', (job,)).fetchone()[0])
        payload.pop('content')  # damaged historical input makes the current recheck fail
        self.conn.execute('UPDATE news_analysis_jobs SET input_json=? WHERE job_id=?', (json.dumps(payload), job))
        item = jobs.list_profile_results(self.conn, 'p')['items'][0]
        self.assertEqual(item['details'], {})
        self.assertEqual(item['detail_omissions'], {})
        self.assertNotIn('허위 지원', json.dumps(item, ensure_ascii=False))
        with self.assertRaises(ValueError):
            jobs.get_profile_result_details(self.conn, 'p', f'job:{job}')

    def test_official_record_identity_and_current_projection_ignore_tampered_result(self):
        from services.bokjiro_collector import normalize_official_service
        profile = NewsProfile('official', 1, '주거', '전국', sources=('bokjiro',),
            life_cycles=('노년',), interest_topics=('주거',), bokjiro_mode='official_tags_v1')
        profiles.create_profile(self.conn, profile)
        run = profiles.prepare_run(self.conn, 'official', expected_revision=1, sites=[])
        item = normalize_official_service(dict(WLFARE_INFO_ID='svc-1', WLFARE_INFO_NM='주거 지원',
            WLFARE_INFO_OUTL_CN='주거비 지원', BIZ_CHR_INST_NM='공식 기관', ADDR='',
            RETURN_STR='BKJR_LFTM_CYC_CD:노년;INTRS_THEMA_CD:주거'))
        official.register_official_results(self.conn, run, [item])
        record = f'official:{run}:bokjiro:svc-1'
        self.conn.execute('UPDATE news_official_results SET result_json=?',
                          (json.dumps({'details':{'support_detail':'tampered-private'}}),))
        full = jobs.get_profile_result_details(self.conn, 'official', record)
        self.assertEqual(full['details']['support_detail'], '주거비 지원')
        self.assertNotIn('tampered-private', json.dumps(full))
        with self.assertRaises(KeyError):
            jobs.get_profile_result_details(self.conn, 'p', record)

    def test_detail_endpoint_only_dispatches_read_and_maps_fixed_import_error(self):
        import asyncio
        from fastapi import HTTPException
        from services import news_jobs_api as api
        from services.news_result_import import ImportUnavailable
        job, source = self.make_job()
        with patch.object(api.database, 'run_db', new=AsyncMock(side_effect=lambda fn:fn())), \
                patch.object(api.database, 'get_connection', return_value=self.conn):
            result = asyncio.run(api.profile_result_details('p', f'job:{job}'))
            self.assertEqual(result['details']['support_detail'], source)
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(api._execute(lambda conn: (_ for _ in ()).throw(ImportUnavailable('import_text_too_long'))))
            self.assertEqual(caught.exception.status_code, 409)
            self.assertEqual(caught.exception.detail, '본문이 길어 새 소식 후보로 가져오기를 보류했습니다. 분석 기록과 원문은 유지됩니다.')


if __name__ == '__main__':
    unittest.main()
