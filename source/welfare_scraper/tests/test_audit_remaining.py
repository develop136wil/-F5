import asyncio
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import BackgroundTasks, HTTPException, UploadFile
import api_server
import database
import utils
from history_manager import items
from services import ai_coordinator, notion_upload_state, notion_service


class TestAuditRemaining(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        database.close_all_connections()
        self.db_patch = patch.object(database, 'DB_FILE', str(Path(self.tmp.name) / 'test.db'))
        self.db_patch.start()
        database.init_db()
        database.execute_write("INSERT INTO welfare_items(title,link) VALUES ('one','https://example.invalid/one')")

    def tearDown(self):
        database.close_all_connections()
        self.db_patch.stop()
        self.tmp.cleanup()

    def test_upload_attempt_survives_connections_and_requires_explicit_retry(self):
        self.assertEqual(notion_upload_state.claim(1)['state'], 'claimed')
        database.close_all_connections()
        self.assertEqual(notion_upload_state.claim(1)['state'], 'uncertain')
        notion_upload_state.allow_retry([1])
        state = notion_upload_state.claim(1)
        self.assertEqual(state['state'], 'claimed')
        notion_upload_state.complete(state['link'], 'remote-page')
        notion_upload_state.allow_retry([1])
        self.assertEqual(notion_upload_state.claim(1)['state'], 'completed')

    async def test_upload_duplicates_and_second_request_create_once(self):
        engine = MagicMock()
        engine.start = AsyncMock(); engine.close = AsyncMock(); engine.extract_poster_url = AsyncMock(return_value='')
        create = AsyncMock(return_value={'ok': True, 'page_id': 'fake'})
        with patch('scraper_engine.ScraperEngine', return_value=engine), \
             patch.object(notion_service, 'is_configured', return_value=True), \
             patch.object(notion_service, 'create_event_page', create):
            first = await api_server._notion_upload_locked(api_server.NotionUploadRequest(ids=[1, 1]))
            second = await api_server._notion_upload_locked(api_server.NotionUploadRequest(ids=[1]))
        self.assertEqual(first['total'], 1)
        self.assertEqual(second['uploaded'], 1)
        self.assertEqual(create.await_count, 1)

    async def test_lost_upload_response_is_not_retried(self):
        engine = MagicMock()
        engine.start = AsyncMock(); engine.close = AsyncMock(); engine.extract_poster_url = AsyncMock(return_value='')
        create = AsyncMock(side_effect=TimeoutError())
        with patch('scraper_engine.ScraperEngine', return_value=engine), \
             patch.object(notion_service, 'is_configured', return_value=True), \
             patch.object(notion_service, 'create_event_page', create):
            await api_server._notion_upload_locked(api_server.NotionUploadRequest(ids=[1]))
            result = await api_server._notion_upload_locked(api_server.NotionUploadRequest(ids=[1]))
        self.assertEqual(create.await_count, 1)
        self.assertEqual(result['uploaded'], 0)

    def test_sites_invalid_batch_rolls_back(self):
        with self.assertRaises(ValueError):
            utils.save_sites([{'name': 'ok', 'url': 'https://example.invalid'}, {'name': 'bad', 'url': 'invalid'}])
        self.assertEqual(utils.load_sites(), [])

    async def test_csv_uppercase_extension_and_empty_cells(self):
        file = UploadFile(filename='sites.CSV', file=io.BytesIO(b'name,url,list_item,title\nExample,https://example.invalid,,\n'))
        start = AsyncMock(return_value={'status': 'started', 'count': 1})
        with patch.object(api_server, '_start_site_batch', start):
            result = await api_server.import_sites(BackgroundTasks(), file)
        self.assertEqual(result['count'], 1)
        self.assertEqual(utils.load_sites(), [])
        self.assertEqual(start.await_args.args[0], ['https://example.invalid'])
        self.assertEqual(start.await_args.args[2], {'https://example.invalid': 'Example'})

    async def test_invalid_csv_batch_returns_error_without_partial_save(self):
        file = UploadFile(filename='sites.csv', file=io.BytesIO(b'name,url\nGood,https://example.invalid\nBad,invalid\n'))
        with self.assertRaises(HTTPException) as error:
            await api_server.import_sites(BackgroundTasks(), file)
        self.assertEqual(error.exception.status_code, 400)
        self.assertEqual(utils.load_sites(), [])

    async def test_oversize_site_file_is_rejected_and_temp_removed(self):
        file = UploadFile(filename='sites.csv', file=io.BytesIO(b'x' * (10 * 1024 * 1024 + 1)))
        with patch('tempfile.tempdir', self.tmp.name):
            with self.assertRaises(HTTPException) as error:
                await api_server.import_sites(BackgroundTasks(), file)
        self.assertEqual(error.exception.status_code, 413)
        self.assertEqual(list(Path(self.tmp.name).glob('*.csv')), [])

    async def test_remote_success_local_save_failure_keeps_retry_blocked(self):
        engine = MagicMock()
        engine.start = AsyncMock(); engine.close = AsyncMock(); engine.extract_poster_url = AsyncMock(return_value='')
        create = AsyncMock(return_value={'ok': True, 'page_id': 'fake'})
        with patch('scraper_engine.ScraperEngine', return_value=engine), \
             patch.object(notion_service, 'is_configured', return_value=True), \
             patch.object(notion_service, 'create_event_page', create), \
             patch.object(notion_upload_state, 'complete', side_effect=OSError('simulated disk failure')):
            result = await api_server._notion_upload_locked(api_server.NotionUploadRequest(ids=[1]))
            await api_server._notion_upload_locked(api_server.NotionUploadRequest(ids=[1]))
        self.assertEqual(result['uploaded'], 0)
        self.assertEqual(create.await_count, 1)
        self.assertEqual(notion_upload_state.claim(1)['state'], 'uncertain')

    async def test_invalid_numeric_and_json_config_are_rejected_before_save(self):
        for key, value in [('TARGET_AGE_MAX_MONTHS', 'abc'), ('TARGET_AGE_MAX_MONTHS', '241'),
                           ('DATA_RETENTION_DAYS', '-1'), ('NOTION_POLICY_DBS', '{}'),
                           ('NOTION_POLICY_DBS', '[{}]')]:
            with self.subTest(key=key, value=value), patch.object(utils, 'set_env_values') as save:
                with self.assertRaises(HTTPException) as error:
                    await api_server.save_config(api_server.ConfigRequest(items={key: value}))
                self.assertEqual(error.exception.status_code, 400)
                save.assert_not_called()

    async def test_bad_legacy_age_does_not_break_config_read(self):
        with patch.object(utils, 'get_env_value', side_effect=lambda k: 'abc' if k == 'TARGET_AGE_MAX_MONTHS' else ''):
            result = await api_server.get_config()
        self.assertEqual(result['TARGET_AGE_MAX_MONTHS'], 108)

    async def test_config_change_invalidates_verification_revision(self):
        previous = api_server.CONFIG_REVISION
        with patch.object(utils, 'set_env_values'):
            result = await api_server.save_config(api_server.ConfigRequest(items={'TARGET_AGE_MAX_MONTHS': '0'}))
        self.assertNotEqual(previous, result['revision'])

    def test_exports_are_unique_and_keep_first_content(self):
        with patch('tempfile.tempdir', self.tmp.name), patch.object(items, 'search_items', side_effect=[
                {'items': [{'title': 'FIRST'}]}, {'items': [{'title': 'SECOND'}]}]):
            first = items.export_data()
            second = items.export_data()
        self.assertNotEqual(first, second)
        self.assertIn('FIRST', Path(first).read_text(encoding='utf-8-sig'))
        self.assertNotIn('SECOND', Path(first).read_text(encoding='utf-8-sig'))

    async def test_backup_requests_use_unique_paths(self):
        first = await api_server.backup_database(BackgroundTasks())
        second = await api_server.backup_database(BackgroundTasks())
        self.assertNotEqual(first.path, second.path)
        self.assertTrue(Path(first.path).exists())

    async def test_only_registered_policy_databases_are_verified(self):
        properties = {name: {'type': kind} for _, name, kind in notion_service.get_policy_props()}
        properties.update({name: {'type': 'url'} for name in notion_service.get_policy_source_props()})
        with patch.object(notion_service, 'get_credentials', return_value=('fake', '')), \
             patch.object(notion_service, 'get_policy_dbs', return_value=[{'key': 'medical', 'name': '의료', 'data_source_id': 'fake'}]), \
             patch.object(notion_service, 'resolve_target', AsyncMock(return_value={'ok': True, 'kind': 'data_source', 'id': 'fake', 'version': 'test'})), \
             patch.object(notion_service, '_get', AsyncMock(return_value=(200, {'properties': properties}))):
            result = await notion_service.verify_policy_databases()
        self.assertTrue(result['ok'], result)
        self.assertEqual(len(result['items']), 1)

    async def test_waiter_checks_daily_quota_again(self):
        ai_coordinator._reset_for_tests()
        model = 'gemini-3.5-flash-lite'
        input_tokens = ai_coordinator.estimate_input_tokens('synthetic quota test')
        entered, resume = asyncio.Event(), asyncio.Event()
        async def sleep(_):
            entered.set()
            await resume.wait()
        with patch.object(ai_coordinator, '_save_state'), \
             patch.object(ai_coordinator, '_state_path', return_value=str(Path(self.tmp.name) / 'ai.json')), \
             patch.object(ai_coordinator, '_rpm_for_model', return_value=1):
            await ai_coordinator.acquire_model_slot(model, input_tokens=input_tokens)
            with patch.object(ai_coordinator.asyncio, 'sleep', sleep):
                task = asyncio.create_task(ai_coordinator.acquire_model_slot(model, input_tokens=input_tokens))
                await asyncio.wait_for(entered.wait(), 2)
                ai_coordinator.mark_daily_exhausted(model)
                resume.set()
                with self.assertRaises(ai_coordinator.AiDailyQuotaExhausted):
                    await asyncio.wait_for(task, 2)
        ai_coordinator._reset_for_tests()
