import asyncio
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from services import attachment_cache as cache_module
from services import news_attachments as attachments


URL = 'https://example.org/download?id=1'
FINAL_URL = 'https://files.example.org/guide.hwpx'


def hwpx(text='지원 내용'):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('Contents/section0.xml', f'<section><t>{text}</t></section>')
    return output.getvalue()


def downloaded(text='지원 내용', **extra):
    data = hwpx(text)
    return dict(url=URL, final_url=FINAL_URL, filename='guide.hwpx', kind='hwpx', data=data,
                sha256=hashlib.sha256(data).hexdigest(), etag='"v1"', last_modified='', **extra)


def record(text='지원 내용'):
    return {**downloaded(text), 'text': text, 'status': 'text_extracted'}


class CacheFixture:
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = os.path.join(self.directory.name, 'text.sqlite3')
        self.cache = cache_module.AttachmentTextCache(self.path)


class TestPersistentCache(CacheFixture, unittest.TestCase):
    def test_reopened_cache_restores_text_without_storing_raw_urls(self):
        value = record()
        self.assertTrue(self.cache.store(URL, 'r1', value, now=1000))
        restored = cache_module.AttachmentTextCache(self.path).get_resource(URL, 'r1', now=1001)
        self.assertEqual(restored['text'], value['text'])
        self.assertTrue(restored['conditional_allowed'])
        with closing(sqlite3.connect(self.path)) as conn:
            row = conn.execute('SELECT * FROM resources').fetchone()
        self.assertNotIn(URL, row)
        self.assertNotIn(FINAL_URL, row)

    def test_recipe_expiry_and_clock_rollback_do_not_reuse_stale_results(self):
        self.cache.store(URL, 'r1', record(), now=100000)
        self.assertIsNone(self.cache.get_resource(URL, 'r2', now=100001))
        self.assertIsNone(self.cache.get_resource(URL, 'r1', now=99999))
        self.assertIsNone(self.cache.get_resource(URL, 'r1', now=100000 + 31 * 86400))
        stale = self.cache.get_resource(URL, 'r1', now=100000 + 86401)
        self.assertFalse(stale['conditional_allowed'])

    def test_conditional_touch_does_not_postpone_full_download_forever(self):
        value = record()
        self.cache.store(URL, 'r1', value, now=100000)
        self.cache.store(URL, 'r1', value, now=100000 + 86399, full_checked_at=100000)
        self.assertFalse(self.cache.get_resource(URL, 'r1', now=100000 + 86401)['conditional_allowed'])

    def test_incomplete_or_oversized_text_is_not_cached(self):
        value = record()
        self.assertFalse(self.cache.store(URL, 'r1', {**value, 'status': 'read_failed'}))
        self.assertFalse(self.cache.store(URL, 'r1', {**value, 'text': 'a' * 30001}))
        self.assertIsNone(self.cache.get_resource(URL, 'r1'))

    def test_hwp_complete_text_can_be_cached_but_partial_and_image_text_cannot(self):
        complete = {**record(), 'kind': 'hwp'}
        self.assertTrue(self.cache.store(URL, 'hwp-recipe', complete))
        self.assertEqual(self.cache.get_resource(URL, 'hwp-recipe')['text'], complete['text'])
        for kind, status in (('hwp', 'partial_extracted'), ('png', 'partial_extracted'),
                             ('png', 'text_extracted'), ('image', 'text_extracted'),
                             ('pdf', 'partial_extracted')):
            with self.subTest(kind=kind, status=status):
                recipe = f'{kind}:{status}'
                self.assertFalse(self.cache.store(URL, recipe, {**record(), 'kind': kind, 'status': status}))
                self.assertIsNone(self.cache.get_resource(URL, recipe))

    def test_capacity_prunes_old_url_and_unreferenced_text(self):
        with patch.object(cache_module, 'MAX_ENTRIES', 3):
            for index in range(5):
                self.assertTrue(self.cache.store(f'{URL}&n={index}', 'r1', record(str(index)), now=1000 + index))
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM resources').fetchone()[0], 3)
            self.assertEqual(conn.execute('SELECT count(*) FROM documents').fetchone()[0], 3)
        self.assertIsNone(self.cache.get_resource(f'{URL}&n=0', 'r1', now=1005))

    def test_old_schema_is_migrated_without_losing_existing_text(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute('''CREATE TABLE documents (
                sha256 TEXT NOT NULL, recipe TEXT NOT NULL, kind TEXT NOT NULL,
                status TEXT NOT NULL, text TEXT NOT NULL, stored_at REAL NOT NULL,
                PRIMARY KEY(sha256, recipe))''')
            value = record()
            conn.execute('INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?)',
                         (value['sha256'], 'r1', 'hwpx', 'text_extracted', value['text'], 1000))
        restored = self.cache.get_content(value['sha256'], 'r1', now=1001)
        self.assertEqual(restored['text'], value['text'])
        self.assertEqual(restored['coverage'], {})
        self.assertTrue(self.cache.store(URL, 'r1', value, now=1002))

    def test_only_complete_ocr_with_safe_coverage_is_cached(self):
        coverage = {'reasons': ['ocr_verification_required'], 'units_total': 1,
                    'units_examined': 1, 'max_chars': 30000}
        value = {**record(), 'kind': 'png', 'status': 'ocr_text_extracted', 'coverage': coverage}
        for invalid in ({}, {'units_total': 1, 'units_examined': 1},
                        {**coverage, 'reasons': ['ocr_low_confidence']},
                        {**coverage, 'reasons': [{'invalid': 'reason'}]},
                        {**coverage, 'units_examined': 0}, {**coverage, 'units_total': True}):
            with self.subTest(coverage=invalid):
                self.assertFalse(self.cache.store(URL, 'ocr', {**value, 'coverage': invalid}))
        self.assertFalse(self.cache.store(URL, 'ocr', {**value, 'no_store': True}))
        self.assertTrue(self.cache.store(URL, 'ocr', value))
        restored = self.cache.get_resource(URL, 'ocr')
        self.assertEqual(restored['coverage'], coverage)
        self.assertEqual(restored['status'], 'ocr_text_extracted')

    def test_damaged_cached_ocr_coverage_is_not_reused(self):
        value = {**record(), 'kind': 'png', 'status': 'ocr_text_extracted',
                 'coverage': {'reasons': ['ocr_verification_required'],
                              'units_total': 1, 'units_examined': 1}}
        self.assertTrue(self.cache.store(URL, 'ocr', value))
        for raw in ('not JSON', json.dumps({'reasons': [], 'units_total': 2, 'units_examined': 1})):
            with closing(sqlite3.connect(self.path)) as conn, conn:
                conn.execute('UPDATE documents SET coverage_json=?', (raw,))
            self.assertIsNone(self.cache.get_resource(URL, 'ocr'))
            self.assertIsNone(self.cache.get_content(value['sha256'], 'ocr'))

    def test_sqlite_capacity_failure_rolls_back_without_breaking_cache_reads(self):
        self.cache.store(URL, 'r1', record(), now=1000)
        with patch.object(cache_module, 'MAX_CACHE_BYTES', 16384):
            self.assertFalse(self.cache.store(URL, 'r1', record('가' * 30000), now=1001))
        self.assertEqual(self.cache.get_resource(URL, 'r1', now=1002)['text'], '지원 내용')


class Response:
    def __init__(self, status=200, headers=None, data=b'', url=FINAL_URL):
        self.status, self.headers, self.url = status, headers or {}, url
        self.content = SimpleNamespace(read=AsyncMock(side_effect=[data, b'']))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


class TestConditionalDownload(unittest.IsolatedAsyncioTestCase):
    def entry(self, **extra):
        return {**record(), 'final_key': cache_module.url_key(FINAL_URL),
                'conditional_allowed': True, **extra}

    async def fetch(self, responses, *, entry=None, cookie_jar=None):
        session = SimpleNamespace(get=Mock(side_effect=responses), cookie_jar=cookie_jar)
        result = await attachments.download_attachment(session, {'url': URL},
            revalidation=entry, url_validator=AsyncMock(side_effect=lambda url: url))
        return result, session

    async def test_validators_follow_only_exact_final_redirect_target(self):
        response = Response(status=304, headers={'ETag': '"v1"'})
        result, session = await self.fetch([
            Response(302, {'Location': FINAL_URL}, url=URL), response,
        ], entry=self.entry())
        self.assertTrue(result['not_modified'])
        self.assertEqual(session.get.call_args_list[0].kwargs['headers'], {})
        self.assertEqual(session.get.call_args_list[1].kwargs['headers']['If-None-Match'], '"v1"')
        response.content.read.assert_not_awaited()

    async def test_changed_redirect_destination_does_not_get_old_validators(self):
        new_url = 'https://other.example.org/new.hwpx'
        result, session = await self.fetch([
            Response(302, {'Location': new_url}, url=URL), Response(data=hwpx('변경'), url=new_url),
        ], entry=self.entry())
        self.assertNotIn('not_modified', result)
        self.assertEqual(session.get.call_args_list[1].kwargs['headers'], {})

    async def test_full_refresh_ignores_validators_and_reads_body(self):
        result, session = await self.fetch([Response(data=hwpx(), url=URL)],
            entry=self.entry(final_key=cache_module.url_key(URL), conditional_allowed=False))
        self.assertEqual(session.get.call_args.kwargs['headers'], {})
        self.assertTrue(result['data'])

    async def test_unrequested_304_retries_without_validators(self):
        result, session = await self.fetch([Response(304, url=URL), Response(data=hwpx(), url=URL)])
        self.assertEqual(session.get.call_count, 2)
        self.assertTrue(result['data'])

    async def test_contradictory_304_etag_forces_unconditional_download(self):
        result, session = await self.fetch([
            Response(304, {'ETag': '"v2"'}, url=URL), Response(data=hwpx('새 내용'), url=URL),
        ], entry=self.entry(final_key=cache_module.url_key(URL)))
        self.assertEqual(session.get.call_args_list[1].kwargs['headers'], {})
        self.assertNotIn('not_modified', result)

    def test_summary_counts_only_successfully_reused_extractions(self):
        summary = attachments.summarize_attachment_reports([
            {'status': 'text_extracted', 'extraction_cache_hit': True},
            {'status': 'read_failed', 'extraction_cache_hit': True},
            {'status': 'text_extracted', 'extraction_cache_hit': False},
            {'status': 'ocr_text_extracted', 'extraction_cache_hit': True},
        ])
        self.assertEqual(summary['cache_hits'], 2)
        self.assertEqual(summary['analyzed'], 3)

    async def test_private_cookie_and_vary_responses_are_not_storable(self):
        for headers in ({'Cache-Control': 'no-store'}, {'Cache-Control': 'private, max-age=60'},
                        {'Set-Cookie': 'x=1'}, {'Vary': 'Accept-Language'}):
            with self.subTest(headers=headers):
                result, _ = await self.fetch([Response(headers=headers, data=hwpx(), url=URL)])
                self.assertTrue(result['no_store'])
        jar = SimpleNamespace(filter_cookies=Mock(return_value={'session': 'secret'}))
        result, session = await self.fetch([Response(data=hwpx(), url=URL)],
            entry=self.entry(final_key=cache_module.url_key(URL)), cookie_jar=jar)
        self.assertTrue(result['no_store'])
        self.assertEqual(session.get.call_args.kwargs['headers'], {})


class TestCachedExtraction(CacheFixture, unittest.IsolatedAsyncioTestCase):
    async def load(self, downloader):
        return await attachments.load_local_attachment(None, {'url': URL},
                                                       cache=self.cache, downloader=downloader)

    async def test_revalidation_reuses_text_after_restart_without_parser(self):
        await self.load(AsyncMock(return_value=downloaded()))
        self.cache = cache_module.AttachmentTextCache(self.path)

        async def not_modified(_session, _item, *, revalidation):
            self.assertIsNotNone(revalidation)
            return {**revalidation, 'not_modified': True, 'final_url': FINAL_URL}

        with patch.object(attachments, 'extract_hwpx_document', side_effect=AssertionError('must not parse')):
            result = await self.load(not_modified)
        self.assertTrue(result['extraction_cache_hit'])
        self.assertEqual(result['text'], '지원 내용')

    async def test_complete_hwp_reuses_hash_confirmed_local_text_without_second_worker(self):
        fetched = {**downloaded(), 'kind': 'hwp', 'filename': '안내.hwp', 'data': b'synthetic HWP'}
        extracted = attachments.DocumentText('한글 지원 내용', [], 1, 1)
        with patch.object(attachments, 'extract_document_isolated', new=AsyncMock(return_value=extracted)) as worker:
            first = await self.load(AsyncMock(return_value=fetched))
            second = await self.load(AsyncMock(return_value=fetched))
        self.assertEqual(first['status'], 'text_extracted')
        self.assertEqual(second['text'], extracted.text)
        self.assertTrue(second['extraction_cache_hit'])
        worker.assert_awaited_once_with(b'synthetic HWP', 'hwp')

    async def test_partial_ocr_and_partial_hwp_never_reuse_success_cache(self):
        for kind, reasons in (('png', ['ocr_verification_required', 'ocr_low_confidence']),
                              ('hwp', ['embedded_content_review'])):
            with self.subTest(kind=kind):
                fetched = {**downloaded(), 'kind': kind, 'data': b'synthetic local file',
                           'sha256': ('b' if kind == 'png' else 'c') * 64}
                extracted = attachments.DocumentText('원문 검토가 필요한 텍스트', reasons, 1, 1)
                with patch.object(attachments, 'extract_document_isolated', new=AsyncMock(return_value=extracted)) as worker:
                    first = await self.load(AsyncMock(return_value=fetched))
                    second = await self.load(AsyncMock(return_value=fetched))
                self.assertEqual(first['status'], 'partial_extracted')
                self.assertEqual(second['coverage']['reasons'], reasons)
                self.assertFalse(second['extraction_cache_hit'])
                self.assertEqual(worker.await_count, 2)
                self.assertIsNone(self.cache.get_resource(URL, attachments.local_extraction_recipe()))

    async def test_complete_image_ocr_reuses_confirmed_bytes_and_preserves_coverage(self):
        fetched = {**downloaded(), 'kind': 'png', 'data': b'image first', 'sha256': 'e' * 64}
        extracted = attachments.DocumentText('지원 대상과 신청 조건', ['ocr_verification_required'], 1, 1)
        with patch.object(attachments, 'extract_document_isolated', new=AsyncMock(return_value=extracted)) as worker:
            first = await self.load(AsyncMock(return_value=fetched))
            self.cache = cache_module.AttachmentTextCache(self.path)
            downloader = AsyncMock(return_value=fetched)
            second = await self.load(downloader)
        self.assertEqual(first['status'], 'ocr_text_extracted')
        self.assertEqual(second['status'], 'ocr_text_extracted')
        self.assertEqual(second['coverage'], extracted.coverage())
        self.assertTrue(second['extraction_cache_hit'])
        self.assertIsNotNone(downloader.call_args.kwargs['revalidation'])
        worker.assert_awaited_once_with(b'image first', 'image')

    async def test_complete_scanned_pdf_reuses_304_without_either_worker(self):
        fetched = {**downloaded(), 'kind': 'pdf', 'data': b'scanned PDF', 'sha256': 'f' * 64}
        extracted = attachments.DocumentText('신청 기간과 연락처', ['ocr_verification_required'], 2, 2)

        async def not_modified(_session, _item, *, revalidation):
            self.assertEqual(revalidation['status'], 'ocr_text_extracted')
            return {**revalidation, 'not_modified': True, 'final_url': FINAL_URL}

        with patch.object(attachments, 'extract_document_isolated', new=AsyncMock(
                side_effect=[attachments.DocumentText('', ['no_text_page'], 2, 2), extracted])) as worker:
            await self.load(AsyncMock(return_value=fetched))
            result = await self.load(not_modified)
        self.assertEqual(worker.await_count, 2)
        self.assertTrue(result['extraction_cache_hit'])
        self.assertEqual(result['coverage'], extracted.coverage())
        self.assertEqual(result['status'], 'ocr_text_extracted')

    async def test_changed_image_is_reprocessed_and_failure_never_uses_old_ocr(self):
        original = {**downloaded(), 'kind': 'png', 'data': b'first', 'sha256': 'd' * 64}
        changed = {**original, 'data': b'changed', 'sha256': 'e' * 64}
        texts = [attachments.DocumentText(value, ['ocr_verification_required'], 1, 1)
                 for value in ('원래 조건', '변경 조건')]
        with patch.object(attachments, 'extract_document_isolated', new=AsyncMock(side_effect=texts)) as worker:
            await self.load(AsyncMock(return_value=original))
            result = await self.load(AsyncMock(return_value=changed))
            with self.assertRaises(TimeoutError):
                await self.load(AsyncMock(side_effect=TimeoutError()))
        self.assertEqual(worker.await_count, 2)
        self.assertFalse(result['extraction_cache_hit'])
        self.assertEqual(result['text'], '변경 조건')

    async def test_private_image_does_not_reuse_or_persist_ocr(self):
        original = {**downloaded(), 'kind': 'png', 'data': b'image', 'sha256': 'd' * 64}
        extracted = attachments.DocumentText('읽은 글자', ['ocr_verification_required'], 1, 1)
        with patch.object(attachments, 'extract_document_isolated', new=AsyncMock(return_value=extracted)) as worker:
            await self.load(AsyncMock(return_value=original))
            result = await self.load(AsyncMock(return_value={**original, 'no_store': True}))
        self.assertEqual(worker.await_count, 2)
        self.assertFalse(result['extraction_cache_hit'])
        self.assertIsNone(self.cache.get_resource(URL, attachments.local_extraction_recipe()))

    async def test_no_validator_server_reuses_identical_bytes_not_old_url_contents(self):
        same_bytes = downloaded()
        await self.load(AsyncMock(return_value=same_bytes))
        with patch.object(attachments, 'extract_hwpx_document', wraps=attachments.extract_hwpx_document) as parse:
            same = await self.load(AsyncMock(return_value=same_bytes))
            changed = await self.load(AsyncMock(return_value=downloaded('새 지원 조건')))
        self.assertTrue(same['extraction_cache_hit'])
        self.assertFalse(changed['extraction_cache_hit'])
        self.assertEqual(changed['text'], '새 지원 조건')
        self.assertEqual(parse.call_count, 1)

    async def test_parser_recipe_change_reextracts_same_file(self):
        value = downloaded()
        await self.load(AsyncMock(return_value=value))
        with patch.object(attachments, 'local_extraction_recipe', return_value='changed-parser'):
            result = await self.load(AsyncMock(return_value=value))
        self.assertFalse(result['extraction_cache_hit'])

    async def test_server_failure_never_returns_old_text(self):
        await self.load(AsyncMock(return_value=downloaded()))
        with self.assertRaises(TimeoutError):
            await self.load(AsyncMock(side_effect=TimeoutError()))

    async def test_cancellation_is_propagated(self):
        with self.assertRaises(asyncio.CancelledError):
            await self.load(AsyncMock(side_effect=asyncio.CancelledError()))

    async def test_private_response_invalidates_url_and_is_not_persisted(self):
        await self.load(AsyncMock(return_value=downloaded()))
        result = await self.load(AsyncMock(return_value=downloaded('비공개 조건', no_store=True)))
        self.assertEqual(result['text'], '비공개 조건')
        recipe = attachments.local_extraction_recipe()
        self.assertIsNone(self.cache.get_resource(URL, recipe))
        self.assertIsNone(self.cache.get_content(result['sha256'], recipe))

    async def test_corrupt_file_cannot_leave_old_url_binding(self):
        await self.load(AsyncMock(return_value=downloaded()))
        result = await self.load(AsyncMock(return_value={**downloaded(), 'data': b'corrupt', 'sha256': 'a' * 64}))
        self.assertEqual(result['status'], 'read_failed')
        self.assertIsNone(self.cache.get_resource(URL, attachments.local_extraction_recipe()))

    async def test_broken_cache_does_not_fail_extraction(self):
        # A directory in place of the cache file simulates inaccessible/corrupt storage.
        os.mkdir(self.path)
        result = await self.load(AsyncMock(return_value=downloaded()))
        self.assertEqual(result['text'], '지원 내용')
        self.assertFalse(result['extraction_cache_hit'])

    async def test_parallel_writes_leave_each_url_bound_to_its_own_text(self):
        recipe = attachments.local_extraction_recipe()
        records = [record(f'지원 {index}') for index in range(12)]
        results = await asyncio.gather(*[
            asyncio.to_thread(self.cache.store, f'{URL}&n={index}', recipe, value)
            for index, value in enumerate(records)
        ])
        self.assertTrue(all(results))
        for index, value in enumerate(records):
            restored = self.cache.get_resource(f'{URL}&n={index}', recipe)
            self.assertEqual(restored['text'], value['text'])
            self.assertEqual(restored['sha256'], value['sha256'])

    async def test_real_http_200_304_and_changed_file_pipeline(self):
        import aiohttp
        from aiohttp import web
        state = {'version': 1, 'statuses': []}

        async def serve(request):
            etag = f'"version-{state["version"]}"'
            if request.headers.get('If-None-Match') == etag:
                state['statuses'].append(304)
                return web.Response(status=304, headers={'ETag': etag})
            state['statuses'].append(200)
            return web.Response(body=hwpx(f'지원 조건 {state["version"]}'),
                                headers={'ETag': etag, 'Content-Type': 'application/octet-stream'})

        app = web.Application()
        app.router.add_get('/download', serve)
        runner = web.AppRunner(app)
        await runner.setup()
        try:
            site = web.TCPSite(runner, '127.0.0.1', 0)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]
            local_url = f'http://127.0.0.1:{port}/download'
            # Loopback is allowed only by this explicit test validator, never in production.
            downloader = partial(attachments.download_attachment,
                                 url_validator=AsyncMock(side_effect=lambda url: url))
            async with aiohttp.ClientSession() as session:
                with patch.object(attachments, 'extract_hwpx_document', wraps=attachments.extract_hwpx_document) as parse:
                    first = await attachments.load_local_attachment(session, {'url': local_url},
                        cache=self.cache, downloader=downloader)
                    second = await attachments.load_local_attachment(session, {'url': local_url},
                        cache=self.cache, downloader=downloader)
                    state['version'] = 2
                    third = await attachments.load_local_attachment(session, {'url': local_url},
                        cache=self.cache, downloader=downloader)
                self.assertEqual(parse.call_count, 2)
            self.assertEqual(state['statuses'], [200, 304, 200])
            self.assertEqual(first['text'], second['text'])
            self.assertNotEqual(first['sha256'], third['sha256'])
            self.assertEqual(third['text'], '지원 조건 2')
            self.assertTrue(second['extraction_cache_hit'])
        finally:
            await runner.cleanup()
