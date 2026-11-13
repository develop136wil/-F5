"""Offline deterministic collection-work counters, not end-to-end speed claims.

Use the isolated test runner. Only synthetic in-memory receipts are read/written;
no scraper, provider, operational profile, or external service is invoked here.
"""
from dataclasses import replace
import json
import sqlite3
import unittest

from services import news_analysis_store as jobs, news_profile_store as profiles
from services.news_candidate_intake import register_candidates
from services.news_custom_analysis import RECIPE
from services.news_profile import NewsProfile
from services.news_site_collector import _previous_links, _registered_progress


class MeasuredConnection:
    """Count only string bytes returned by the candidate-receipt SELECTs."""
    def __init__(self, conn):
        self.conn = conn
        self.read_bytes = 0

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def execute(self, sql, parameters=()):
        cursor = self.conn.execute(sql, parameters)
        if sql.lstrip().upper().startswith('SELECT') and 'FROM news_run_candidates' in sql:
            def rows():
                for row in cursor:
                    self.read_bytes += sum(len(value.encode('utf-8')) for value in row if isinstance(value, str))
                    yield row
            return rows()
        return cursor


class TestNewsCollectionPerformance(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.execute('PRAGMA foreign_keys=ON')
        profiles.ensure_schema(self.conn)
        jobs.ensure_schema(self.conn)
        self.profile = NewsProfile('perf', 1, '검증 조건', '전국', keywords=('지원',), site_ids=('1',))
        profiles.create_profile(self.conn, self.profile)
        self.run = self.make_run()

    def tearDown(self):
        self.conn.close()

    def make_run(self, profile_id='perf', revision=1):
        return profiles.prepare_run(self.conn, profile_id, expected_revision=revision,
            sites=[dict(id=1, name='고정 출처', url='https://example.invalid')])

    def candidates(self, count=20):
        return [dict(title=f'지원 {index}', link=f'https://example.invalid/posts/{index}',
                     content=('원문 조건을 빠짐없이 보존합니다.\n' * 500) + f'마지막 조건 {index}')
                for index in range(count)]

    @staticmethod
    def profile_reads(statements):
        selects = [sql.lower() for sql in statements if sql.lstrip().lower().startswith('select')]
        return (sum('from news_run_snapshots where run_id=' in sql for sql in selects),
                sum('from news_profile_versions where profile_id=' in sql for sql in selects))

    def trace(self, action):
        statements = []
        self.conn.set_trace_callback(statements.append)
        try:
            result = action()
        finally:
            self.conn.set_trace_callback(None)
        return result, self.profile_reads(statements)

    def test_twenty_candidate_batch_validates_run_once_with_identical_inputs_and_cache_keys(self):
        candidates = self.candidates()
        control_run = self.make_run()

        def enqueue_individually():
            with profiles._atomic(self.conn):
                return [jobs.enqueue(self.conn, control_run,
                    payload=dict(candidate, site_name='고정 출처'), model='fixed-model', recipe=RECIPE)
                    for candidate in candidates]

        _, reference_reads = self.trace(enqueue_individually)
        records, batch_reads = self.trace(lambda: register_candidates(
            self.conn, self.run, '1', candidates, model='fixed-model'))
        self.assertEqual(reference_reads, (20, 40))
        self.assertEqual(batch_reads, (1, 1))
        self.assertEqual(len(records), 20)
        self.assertTrue(all(record['created'] and record['route'] == 'matched' for record in records))
        columns = 'cache_key,input_json,model,recipe'
        read = lambda run: self.conn.execute(
            f'SELECT {columns} FROM news_analysis_jobs WHERE run_id=? ORDER BY cache_key', (run,)).fetchall()
        self.assertEqual(read(self.run), read(control_run))
        stored = [json.loads(row[1]) for row in read(self.run)]
        self.assertEqual(sorted(stored, key=lambda item: item['link']),
                         sorted([dict(item, site_name='고정 출처') for item in candidates], key=lambda item: item['link']))
        again = register_candidates(self.conn, self.run, '1', candidates, model='different-model')
        self.assertEqual([item['job_id'] for item in again], [item['job_id'] for item in records])
        self.assertFalse(any(item['created'] for item in again))
        print(f'NEWS_COLLECTION_WORK_COUNTERS candidates=20 reference_reads={reference_reads} batch_reads={batch_reads}')

    def test_changed_current_profile_keeps_original_route_and_cache_fingerprint(self):
        profiles.save_revision(self.conn, replace(self.profile, revision=2, keywords=('의료',)), expected_revision=1)
        candidate = self.candidates(1)[0]
        record = register_candidates(self.conn, self.run, '1', [candidate], model='fixed-model')[0]
        expected = jobs._hash({'profile': self.profile.fingerprint,
            'input': dict(candidate, site_name='고정 출처'), 'model': 'fixed-model', 'recipe': RECIPE})
        self.assertEqual(record['route'], 'matched')
        self.assertEqual(self.conn.execute('SELECT cache_key FROM news_analysis_jobs WHERE job_id=?',
                                          (record['job_id'],)).fetchone()[0], expected)

    def test_corrupt_run_or_profile_is_rejected_before_intake_writes(self):
        for table, key in (('news_run_snapshots', 'run_id'), ('news_profile_versions', 'profile_id')):
            with self.subTest(table=table):
                value = self.run if key == 'run_id' else self.profile.profile_id
                original = self.conn.execute(f'SELECT fingerprint FROM {table} WHERE {key}=?', (value,)).fetchone()[0]
                self.conn.execute(f'UPDATE {table} SET fingerprint=? WHERE {key}=?', ('corrupt', value))
                with self.assertRaisesRegex(ValueError, 'integrity mismatch'):
                    register_candidates(self.conn, self.run, '1', self.candidates(1), model='fixed-model')
                self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_analysis_jobs').fetchone()[0], 0)
                self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_run_candidates').fetchone()[0], 0)
                self.conn.execute(f'UPDATE {table} SET fingerprint=? WHERE {key}=?', (original, value))

    def test_bound_enqueue_cannot_switch_run_or_fingerprint_and_expires_after_batch(self):
        payload = dict(self.candidates(1)[0], site_name='고정 출처')
        with jobs._candidate_batch(self.conn, self.run) as (snapshot, profile, enqueue):
            snapshot['profile']['profile_id'] = 'not-another-run'
            with self.assertRaises(TypeError):
                enqueue(payload=payload, model='fixed-model', recipe=RECIPE, run_id='other')
            with self.assertRaises(TypeError):
                enqueue(payload=payload, model='fixed-model', recipe=RECIPE, fingerprint='other')
            with self.assertRaises(ValueError):
                enqueue(payload=dict(payload, unexpected='not allowed'), model='fixed-model', recipe=RECIPE)
            job_id = enqueue(payload=payload, model='fixed-model', recipe=RECIPE)
        with self.assertRaisesRegex(RuntimeError, 'no longer active'):
            enqueue(payload=payload, model='fixed-model', recipe=RECIPE)
        row = self.conn.execute('SELECT run_id,cache_key FROM news_analysis_jobs WHERE job_id=?', (job_id,)).fetchone()
        self.assertEqual(row, (self.run, jobs._hash({'profile': profile.fingerprint,
            'input': payload, 'model': 'fixed-model', 'recipe': RECIPE})))

    def test_invalid_late_candidate_rolls_back_jobs_and_receipts(self):
        candidates = self.candidates(2)
        candidates[1]['content'] = None
        with self.assertRaises(ValueError):
            register_candidates(self.conn, self.run, '1', candidates, model='fixed-model')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_analysis_jobs').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_run_candidates').fetchone()[0], 0)

    def test_successful_batch_does_not_commit_callers_transaction(self):
        self.conn.commit()
        self.conn.execute('BEGIN')
        register_candidates(self.conn, self.run, '1', self.candidates(1), model='fixed-model')
        self.conn.rollback()
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_analysis_jobs').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_run_candidates').fetchone()[0], 0)

    def insert_receipts(self, run_id, count, *, body='원문' * 5000, prefix='post'):
        rows = [(run_id, f'{prefix}-{index}', '1', json.dumps(dict(
            title=f'  지원 {prefix} {index}  ', link=f'https://example.invalid/{prefix}/{index}', content=body),
            ensure_ascii=False), 'semantic_candidate', None) for index in range(count)]
        self.conn.executemany('INSERT INTO news_run_candidates VALUES (?,?,?,?,?,?)', rows)

    def test_resume_and_previous_url_reads_do_not_transfer_source_bodies(self):
        previous = self.make_run()
        self.insert_receipts(self.run, 100, prefix='current')
        self.insert_receipts(previous, 100, prefix='prior')
        before = self.conn.execute('SELECT run_id,candidate_key,payload_json FROM news_run_candidates ORDER BY rowid').fetchall()
        current_rows = self.conn.execute('SELECT site_id,payload_json FROM news_run_candidates WHERE run_id=?', (self.run,)).fetchall()
        reference_bytes = sum(len(value.encode('utf-8')) for row in current_rows for value in row)
        expected = {'1': {json.loads(row[1])['link'] for row in current_rows}}
        measured = MeasuredConnection(self.conn)
        identities, count, skipped, vision_review, issues = _registered_progress(measured, self.run)
        self.assertEqual((identities, count, skipped, vision_review, issues), (expected, 100, 0, 0, {}))
        self.assertLess(measured.read_bytes, reference_bytes // 100)
        current_bytes = measured.read_bytes
        measured.read_bytes = 0
        links = _previous_links(measured, self.run)
        self.assertEqual(links, {f'https://example.invalid/prior/{index}' for index in range(100)})
        self.assertLess(measured.read_bytes, reference_bytes // 100)
        self.assertEqual(before, self.conn.execute(
            'SELECT run_id,candidate_key,payload_json FROM news_run_candidates ORDER BY rowid').fetchall())
        print(f'NEWS_RECEIPT_TRANSFER_COUNTERS rows=100 reference_bytes={reference_bytes} '
              f'checkpoint_bytes={current_bytes} previous_link_bytes={measured.read_bytes}')

    def test_previous_url_limit_and_profile_scope_are_unchanged(self):
        previous = self.make_run()
        self.insert_receipts(previous, 5002, body='', prefix='bounded')
        self.insert_receipts(self.run, 1, body='', prefix='current')
        profiles.create_profile(self.conn, replace(self.profile, profile_id='other'))
        unrelated = self.make_run('other')
        self.insert_receipts(unrelated, 1, body='', prefix='other')
        links = _previous_links(self.conn, self.run)
        self.assertEqual(len(links), 5000)
        self.assertEqual(links, {f'https://example.invalid/bounded/{index}' for index in range(2, 5002)})

    def test_malformed_or_nontext_receipts_still_fail_closed(self):
        previous = self.make_run()
        cases = ('{', '{}', '{"title":{},"link":{}}', '{"title":"지원","link":7}')
        for encoded in cases:
            with self.subTest(encoded=encoded):
                for run in (self.run, previous):
                    self.conn.execute('INSERT INTO news_run_candidates VALUES (?,?,?,?,?,?)',
                        (run, 'bad', '1', encoded, 'semantic_candidate', None))
                with self.assertRaises((ValueError, sqlite3.OperationalError)):
                    _registered_progress(self.conn, self.run)
                with self.assertRaises((ValueError, sqlite3.OperationalError)):
                    _previous_links(self.conn, self.run)
                self.conn.execute("DELETE FROM news_run_candidates WHERE candidate_key='bad'")
