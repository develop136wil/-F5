"""DB 저장 결과가 수집 완료 상태·리포트·복지로 집계에 정확히 반영되는지 검증한다."""
import os
import sqlite3
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api_server
import database
import history_manager
import notifier
from services.scraper_service import ScraperService
from services import ai_coordinator


class SaveResultCallerTests(unittest.IsolatedAsyncioTestCase):
    async def test_save_failure_stops_report_and_notification(self):
        service = ScraperService(headless=True)
        item = {'title': '저장 실패 항목', 'link': 'https://x/save-fail'}
        report = AsyncMock()
        notify = AsyncMock()
        with patch.object(database, 'run_db', AsyncMock(
                side_effect=sqlite3.OperationalError('disk full'))), \
             patch.object(notifier, 'generate_report', report), \
             patch.object(notifier, 'send_discord_notification', notify):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'disk full'):
                await service._save_results([item], [])
        report.assert_not_called()
        notify.assert_not_awaited()

    async def test_only_actually_saved_items_reach_report_and_notification(self):
        service = ScraperService(headless=True)
        saved = {'title': '저장됨', 'link': 'https://x/saved'}
        skipped = {'title': '중복', 'link': 'https://x/skipped'}
        report_items = []
        notification_counts = []

        async def fake_run_db(func, *args, **kwargs):
            if func is history_manager.save_data:
                return {
                    'requested': 2, 'saved': 1, 'processed': 1, 'skipped': 1,
                    'saved_links': ['https://x/saved'],
                    'skipped_links': ['https://x/skipped'],
                }
            if func is notifier.generate_report:
                report_items.extend(args[0])
                return 'report.html'
            if func is notifier.send_notification:
                notification_counts.append(args[0])
                return None
            raise AssertionError(f'예상하지 못한 DB 작업: {func}')

        discord = AsyncMock()
        chat = AsyncMock()
        expiring = AsyncMock()
        with patch.object(database, 'run_db', side_effect=fake_run_db), \
             patch('services.embedding_service.get_unavailable_reason', return_value='missing_api_key'), \
             patch.object(notifier, 'send_discord_notification', discord), \
             patch.object(notifier, 'send_google_chat_notification', chat), \
             patch.object(notifier, 'notify_expiring_items', expiring):
            result = await service._save_results([saved, skipped], [])

        self.assertEqual(result, [saved])
        self.assertEqual(report_items, [saved])
        self.assertEqual(notification_counts, [1])
        self.assertEqual(discord.await_args.args[0], [saved])
        self.assertEqual(chat.await_args.args[0], [saved])
        expiring.assert_awaited_once()

    async def test_retired_bokjiro_collector_cannot_save_or_start_background_work(self):
        from fastapi import BackgroundTasks, HTTPException
        from services import bokjiro_collector
        tasks = BackgroundTasks()
        collect, save = AsyncMock(), AsyncMock()
        with patch.object(bokjiro_collector, 'collect_infant_services', collect), \
             patch.object(database, 'run_db', save):
            with self.assertRaises(HTTPException) as caught:
                await api_server.bokjiro_collect_infant(tasks)
        self.assertEqual(caught.exception.status_code, 410)
        self.assertEqual(tasks.tasks, [])
        collect.assert_not_awaited()
        save.assert_not_awaited()

    async def test_background_scrape_reports_storage_failure_as_error(self):
        api_server.STOP_EVENT.clear()
        api_server.SCRAPER_STATUS.update({
            'is_running': True, 'status_code': 'running', 'message': '저장 중',
            'save_result': None,
        })
        with patch.object(
                ScraperService, 'run_scraper_async',
                AsyncMock(side_effect=sqlite3.OperationalError('disk full'))), \
             patch.object(ai_coordinator, 'finish_job'):
            await api_server.run_scraper_task(headless=True)

        self.assertFalse(api_server.SCRAPER_STATUS['is_running'])
        self.assertEqual(api_server.SCRAPER_STATUS['status_code'], 'error')
        self.assertIn('disk full', api_server.SCRAPER_STATUS['message'])
        self.assertIsNone(api_server.SCRAPER_STATUS['save_result'])

    async def test_background_scrape_keeps_actual_save_counts_in_completion_message(self):
        async def fake_run(service, **_kwargs):
            service.last_save_result = {
                'requested': 4, 'saved': 3, 'processed': 3, 'skipped': 1,
            }
            return [object()] * 3

        api_server.STOP_EVENT.clear()
        api_server.SCRAPER_STATUS.update({
            'is_running': True, 'status_code': 'running', 'message': '저장 중',
            'save_result': None,
        })
        with patch.object(ScraperService, 'run_scraper_async', new=fake_run), \
             patch.object(ai_coordinator, 'finish_job'):
            await api_server.run_scraper_task(headless=True)

        self.assertEqual(api_server.SCRAPER_STATUS['status_code'], 'completed')
        self.assertEqual(api_server.SCRAPER_STATUS['save_result']['saved'], 3)
        self.assertEqual(
            api_server.SCRAPER_STATUS['message'],
            '완료 — 3건 저장/갱신 · 1건 중복 제외',
        )


if __name__ == '__main__':
    unittest.main()
