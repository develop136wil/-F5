"""Offline release/security regressions; use scripts/run_isolated_tests.py."""
import ast
import asyncio
import io
import logging
import os
import stat
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import ContentTypeError, RequestInfo
from fastapi import BackgroundTasks
from fastapi.testclient import TestClient
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

import api_server
import notifier
from services import temp_files, news_gallery
from services.logger import MEMORY_LOGS, setup_logger
from services.secret_redaction import redact_secrets


class TestOwnedTemporaryFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='security-test-owned-profile-')
        self.env = patch.dict(os.environ, {'LOCALAPPDATA': self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def create(self, **kwargs):
        fd, path = temp_files.create_temp_file(**kwargs)
        os.close(fd)
        return Path(path)

    def age(self, path):
        old = time.time() - 7200
        os.utime(path, (old, old))

    def test_cleanup_only_old_owned_direct_regular_files(self):
        old = self.create(prefix='report_', suffix='.html')
        recent = self.create(prefix='recent_', suffix='.html')
        self.age(old)
        root = temp_files.get_temp_dir()
        unknown = root / 'unknown-user-report.pdf'
        unknown.write_text('synthetic')
        self.age(unknown)
        nested = root / 'WelfareF5_user_folder'
        nested.mkdir()
        nested_file = nested / 'WelfareF5_nested.html'
        nested_file.write_text('synthetic')
        self.age(nested_file)
        shared = Path(self.tmp.name) / 'shared-temp'
        shared.mkdir()
        others = []
        for name in ('export_budget.xlsx', 'Report_user.pdf', 'welfare_other_app.dat',
                     'NotionReport_other_app.md', 'WelfareF5_shared.html'):
            path = shared / name
            path.write_text('synthetic-other-application')
            self.age(path)
            others.append(path)
        with patch('tempfile.gettempdir', return_value=str(shared)):
            self.assertEqual(temp_files.cleanup_stale_temp_files(), 1)
        self.assertFalse(old.exists())
        for path in [recent, unknown, nested_file, *others]:
            self.assertTrue(path.exists(), path.name)
            if path != recent:
                self.assertFalse(temp_files.delete_temp_file(path))

    def test_cleanup_refuses_reparse_directory(self):
        owned = self.create(suffix='.html')
        self.age(owned)
        with patch.object(temp_files, '_plain_directory', return_value=False):
            with self.assertRaises(OSError):
                temp_files.cleanup_stale_temp_files()
            with self.assertRaises(OSError):
                self.create(suffix='.html')
            self.assertFalse(temp_files.delete_temp_file(owned))
        self.assertTrue(owned.exists())

    def test_cleanup_refuses_reparse_file(self):
        owned = self.create(suffix='.html')
        self.age(owned)
        original_lstat = Path.lstat
        def lstat(path):
            if path == owned:
                return SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400)
            return original_lstat(path)
        with patch.object(Path, 'lstat', lstat):
            self.assertFalse(temp_files.delete_temp_file(owned))
            self.assertEqual(temp_files.cleanup_stale_temp_files(), 0)
        self.assertTrue(owned.exists())

    def test_invalid_names_cannot_escape_directory(self):
        for prefix, suffix in [('../outside', '.html'), ('safe', '/../escape'),
                               ('C:\\outside\\', '.txt')]:
            with self.assertRaises(ValueError):
                self.create(prefix=prefix, suffix=suffix)

    def test_all_report_generators_use_owned_directory(self):
        rows = [{'title':'Synthetic title','link':'https://example.invalid/never-requested',
                 'site_name':'Synthetic agency'}]
        for generate in (notifier.generate_report, notifier.generate_notion_report,
                         notifier.generate_notion_markdown_report, news_gallery.generate_html):
            path = Path(generate(rows))
            self.assertEqual(path.parent, temp_files.get_temp_dir())
            self.assertTrue(path.name.startswith('WelfareF5_'))
            self.assertTrue(api_server._delete_temp_file(path))
            self.assertFalse(path.exists())

    def test_startup_no_longer_enumerates_shared_temp(self):
        tree = ast.parse(Path(api_server.__file__).read_text(encoding='utf-8'))
        startup = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                       and node.name == 'lifespan')
        code = ast.unparse(startup)
        self.assertIn('cleanup_stale_temp_files()', code)
        self.assertNotIn('gettempdir', code)
        self.assertNotIn('os.unlink', code)

    def test_csv_template_is_inline_without_temporary_files(self):
        from services.site_import import HEADERS

        async def run():
            tasks = BackgroundTasks()
            with patch.object(api_server, 'create_temp_file') as create, \
                 patch.object(api_server, '_delete_temp_file') as delete:
                result = await api_server.download_site_template(tasks, format='csv')
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.media_type, 'text/csv')
                self.assertEqual(result.headers['content-disposition'],
                                 'attachment; filename="site_import_template.csv"')
                self.assertEqual(result.body, ('\ufeff' + ','.join(HEADERS) + '\r\n').encode('utf-8'))
                self.assertFalse(tasks.tasks)
                await tasks()
                create.assert_not_called()
                delete.assert_not_called()
        asyncio.run(run())

    def test_excel_template_preserves_bundled_asset_after_download(self):
        async def run():
            asset = Path(api_server.__file__).resolve().parent / 'assets/site_import_template.xlsx'
            original = asset.read_bytes()
            tasks = BackgroundTasks()
            with patch.object(api_server, 'create_temp_file') as create, \
                 patch.object(api_server, '_delete_temp_file') as delete:
                result = await api_server.download_site_template(tasks)
                self.assertEqual(Path(result.path).resolve(), asset)
                self.assertEqual(result.filename, 'site_import_template.xlsx')
                self.assertEqual(result.media_type,
                                 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
                sent = []

                async def send(message):
                    sent.append(message)

                # Actually stream the response, including any attached cleanup callback.
                await result({'type': 'http', 'method': 'GET', 'headers': []}, None, send)
                self.assertEqual(sent[0]['status'], 200)
                self.assertEqual(b''.join(m.get('body', b'') for m in sent[1:]), original)
                self.assertFalse(tasks.tasks)
                await tasks()
                self.assertEqual(asset.read_bytes(), original)
                create.assert_not_called()
                delete.assert_not_called()
        asyncio.run(run())

    def test_download_backup_cleanup_does_not_touch_original_database(self):
        original = Path(self.tmp.name) / 'synthetic-original.db'
        original.write_bytes(b'synthetic original preserved')
        async def fake_backup(function, destination):
            Path(destination).write_bytes(b'synthetic download copy')
        async def run():
            tasks = BackgroundTasks()
            with patch.object(api_server.database,'DB_FILE',str(original)), \
                 patch.object(api_server.database,'run_db',new=AsyncMock(side_effect=fake_backup)):
                result = await api_server.backup_database(tasks)
            path = Path(result.path)
            self.assertEqual(path.parent, temp_files.get_temp_dir())
            self.assertTrue(path.is_file())
            await tasks()
            self.assertFalse(path.exists())
            self.assertEqual(original.read_bytes(),b'synthetic original preserved')
        asyncio.run(run())


class TestSecretDiagnostics(unittest.TestCase):
    google = 'AIza' + 'A' * 35
    notion = 'ntn_' + 'B' * 32
    webhook = 'https://discord.com/api/webhooks/12345/TEST_DISCORD_CREDENTIAL'

    def test_common_header_query_path_webhook_and_userinfo_formats(self):
        samples = [
            ('https://example.invalid/?key=TEST_KEY&token=TEST_TOKEN&count=3', ['TEST_KEY','TEST_TOKEN']),
            ("{'Authorization': 'Bearer TEST_BEARER', 'x-goog-api-key': 'TEST_HEADER'}", ['TEST_BEARER','TEST_HEADER']),
            ('C:/synthetic/' + self.google + '/trace.log', [self.google]),
            ('https://example.invalid/' + self.notion, [self.notion]),
            (self.webhook, ['TEST_DISCORD_CREDENTIAL']),
            ('https://user:TEST_PASSWORD@example.invalid/path', ['TEST_PASSWORD']),
        ]
        for sample, secrets in samples:
            clean = redact_secrets(sample, secrets=())
            for secret in secrets:
                self.assertNotIn(secret, clean)
            self.assertIn('[REDACTED]', clean)
            self.assertEqual(redact_secrets(clean, secrets=()), clean)
        self.assertIn('count=3', redact_secrets(samples[0][0], secrets=()))

    def test_configured_unknown_format_is_masked_even_when_percent_encoded(self):
        secret = 'synthetic/credential+with=symbols'
        with patch.dict(os.environ, {'GOOGLE_API_KEY':secret}):
            clean = redact_secrets('failure ' + secret + ' encoded synthetic%2Fcredential%2Bwith%3Dsymbols')
        self.assertNotIn(secret, clean)
        self.assertNotIn('synthetic%2Fcredential', clean)

    def test_file_console_memory_and_parent_handlers_receive_redacted_records(self):
        with tempfile.TemporaryDirectory(prefix='security-test-logs-') as profile:
            output, parent_output = io.StringIO(), io.StringIO()
            parent_handler = logging.StreamHandler(parent_output)
            root = logging.getLogger()
            root.addHandler(parent_handler)
            logger = None
            try:
                with patch.dict(os.environ, {'LOCALAPPDATA':profile, 'GOOGLE_API_KEY':self.google,
                                              'NOTION_TOKEN':self.notion}), patch('sys.stdout', output):
                    logger = setup_logger('security-regression-' + uuid.uuid4().hex)
                    logger.warning('header Authorization: Bearer %s', self.notion)
                    try:
                        raise ValueError('fake failure ?key=' + self.google)
                    except ValueError:
                        logger.exception('Synthetic exception')
                    logging.getLogger(logger.name + '.child').warning('webhook %s', self.webhook)
                    for handler in logger.handlers:
                        handler.flush()
                file_output = (Path(profile)/'welfare-scraper'/'logs'/'scraper.log').read_text(encoding='utf-8')
                for rendered in (output.getvalue(), parent_output.getvalue(), file_output,
                                 '\n'.join(list(MEMORY_LOGS)[-3:])):
                    self.assertIn('[REDACTED]', rendered)
                    for secret in (self.google, self.notion, 'TEST_DISCORD_CREDENTIAL'):
                        self.assertNotIn(secret, rendered)
                    self.assertIn('Synthetic exception', rendered)
            finally:
                root.removeHandler(parent_handler)
                if logger:
                    for handler in logger.handlers[:]:
                        handler.close()
                        logger.removeHandler(handler)

    def test_raw_packaged_stderr_redacts_a_credential_url(self):
        with tempfile.TemporaryDirectory(prefix='security-test-stderr-') as directory:
            path = Path(directory)/'synthetic.log'
            stream = api_server._RotatingStderr(str(path))
            try:
                stream.write('fake ?key=' + self.google + '\n')
            finally:
                stream.close()
            self.assertNotIn(self.google, path.read_text(encoding='utf-8'))


class TestGoogleVerificationAndCors(unittest.IsolatedAsyncioTestCase):
    async def verify(self, status=200, body=None, failure=None):
        key = 'SYNTHETIC_TEST_KEY_NEVER_A_REAL_CREDENTIAL'
        calls = []
        class Response:
            async def __aenter__(self): return self
            async def __aexit__(self,*args): return False
            async def json(self):
                if failure:
                    raise failure
                return body if body is not None else {'name':'synthetic-model'}
        Response.status = status
        class Session:
            def __init__(self,*args,**kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self,*args): return False
            def get(self,url,**kwargs):
                calls.append((url, kwargs))
                return Response()
        values = {'GOOGLE_API_KEY':key,'CLASSIFY_MODEL':'synthetic-model'}
        with patch.object(api_server.utils,'get_env_value',side_effect=lambda name:values.get(name,'')), \
             patch.object(aiohttp,'ClientSession',Session):
            result = await api_server._google_api_key_verify()
        self.assertNotIn(key, str(result))
        self.assertEqual(len(calls), 1)
        self.assertNotIn(key, calls[0][0])
        self.assertNotIn('params', calls[0][1])
        self.assertEqual(calls[0][1]['headers'], {'x-goog-api-key':key})
        self.assertFalse(calls[0][1]['allow_redirects'])
        return result

    async def test_success_uses_header_and_never_follows_redirects(self):
        self.assertTrue((await self.verify())['ok'])

    async def test_server_error_payload_is_not_reflected_to_user(self):
        result = await self.verify(status=403,body={'error':{'message':'SYNTHETIC_TEST_KEY_NEVER_A_REAL_CREDENTIAL'}})
        self.assertEqual(result['status'],'invalid_key')

    async def test_exception_with_legacy_key_url_is_not_reflected(self):
        url = URL('https://example.invalid/model?key=SYNTHETIC_TEST_KEY_NEVER_A_REAL_CREDENTIAL')
        request = RequestInfo(url,'GET',CIMultiDictProxy(CIMultiDict()),url)
        result = await self.verify(failure=ContentTypeError(request,(),status=200,message='Synthetic HTML response'))
        self.assertEqual(result['status'],'network_error')
        self.assertNotIn('example.invalid',result['error'])

    def test_dev_preflight_allows_put_and_session_header_but_rejects_other_origin(self):
        client = TestClient(api_server.app)
        headers = {'Origin':'http://127.0.0.1:1420', 'Access-Control-Request-Method':'PUT',
                   'Access-Control-Request-Headers':'Content-Type,X-Welfare-Session'}
        result = client.options('/api/news/profiles/synthetic',headers=headers)
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.headers['access-control-allow-origin'], headers['Origin'])
        self.assertIn('PUT', result.headers['access-control-allow-methods'])
        result = client.options('/api/news/profiles/synthetic',headers={**headers,'Origin':'https://untrusted.example'})
        self.assertEqual(result.status_code,400)
        self.assertNotIn('access-control-allow-origin',result.headers)


if __name__ == '__main__':
    unittest.main()
