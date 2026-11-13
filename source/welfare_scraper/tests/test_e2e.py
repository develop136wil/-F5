import unittest
import os
import sys
import asyncio
import shutil
import sqlite3
from unittest.mock import MagicMock, patch

# Add parent directory to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.scraper_service import ScraperService
from tests.mock_server import MockServer
import database
import history_manager

class TestE2EScraper(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        # Start Mock Server
        cls.mock_server = MockServer(port=8888)
        cls.mock_server.start()
        cls.mock_url = "http://localhost:8888/board"
        
    @classmethod
    def tearDownClass(cls):
        cls.mock_server.stop()

    def setUp(self):
        # Setup Test DB
        self.test_db = 'test_welfare.db'

        # (테스트 격리 수정 2026-07-24) 파일을 지우기 '전에' 남은 연결을 먼저 닫아야 한다.
        # 이전 테스트가 남긴 SQLite 연결이 살아 있으면 Windows에서 파일이 잠겨
        # PermissionError(WinError 32)가 난다. WAL 부산물(-wal/-shm)도 함께 정리한다.
        database.close_all_connections()
        for _suffix in ('', '-wal', '-shm'):
            _p = self.test_db + _suffix
            if os.path.exists(_p):
                try:
                    os.remove(_p)
                except OSError:
                    pass
            
        # (테스트 격리 수정 2026-07-27, GPT Codex 리뷰로 발견) scraper.lock은 AppData의
        # 실제 운영 lock 파일을 OS 배타 락으로 잡는 교차프로세스 가드다(services/scraper_service.py
        # _acquire_scrape_lock). 이 앱이 실제로 수집 중인 순간과 e2e 실행이 겹치면 테스트가
        # 락 획득에 실패해 "다른 프로세스에서 스크래핑이 이미 실행 중" 경로로 빠져 빈 리스트를
        # 반환한다(실측 재현 확인됨: is_relevant/전체 파이프라인과 무관한 비결정적 실패).
        # e2e는 운영 앱의 실행 여부와 완전히 독립적으로 결정론적이어야 하므로, 이 락 자체를
        # 패치해 항상 즉시 획득 가능한 더미로 대체한다(파일 I/O 없음 → 실제 lock 파일과 무관).
        self.lock_acquire_patcher = patch(
            'services.scraper_service._acquire_scrape_lock', return_value=object())
        self.lock_acquire_patcher.start()
        self.lock_release_patcher = patch(
            'services.scraper_service._release_scrape_lock', return_value=None)
        self.lock_release_patcher.start()

        # Patch Database Module to use test DB
        self.db_patcher = patch('database.DB_FILE', self.test_db)
        self.db_patcher.start()

        # (테스트 격리 수정 2026-07-24) DB_FILE만 패치하면 부족하다 — database 모듈은
        # 임포트 시점에 이미 '진짜' DB로 스레드 로컬 연결을 만들어 캐싱해두기 때문에,
        # init_db()가 그 캐시 연결을 재사용해 스키마를 진짜 DB에 만들어버린다. 반면
        # 수집은 run_db(to_thread)로 워커 스레드에서 돌아 패치된 경로(빈 테스트 DB)에
        # 새로 연결하므로 "no such table: welfare_items"로 깨졌다.
        # 캐시 연결을 끊어 이후 모든 연결이 패치된 테스트 DB를 보게 한다.
        database.close_all_connections()

        # Initialize Schema
        database.init_db()
        
        # (테스트 격리 수정 2026-07-28) 의미 게이트(_semantic_gate)와 항목별 AI 분석 단계의
        # 의미중복 제거 둘 다 실제 임베딩 API를 호출하는데 mocking이 안 돼 있었다 — 환경에
        # 실제 GOOGLE_API_KEY가 있으면 라이브 네트워크 호출이 나갔다. 오늘 실제 일일 임베딩
        # 한도 소진과 겹쳐 이 e2e 테스트가 embedding_service의 "일일 소진" 전역 상태를 세팅해
        # 버려, 이후 실행되는 다른 테스트 파일의 목(mock) 기반 임베딩 테스트를 오염시키는 게
        # 실측 확인됐다. 호출 경로가 여러 곳이라 기능별 스위치를 하나씩 끄는 대신,
        # embedding_service 자체를 네트워크 계층에서 차단해 e2e를 API 키·한도와
        # 완전히 무관하게 결정론적으로 만든다.
        self.semantic_gate_patcher = patch.dict(os.environ, {'RELEVANCE_GATE_SEMANTIC': '0'})
        self.semantic_gate_patcher.start()
        self.embed_patcher = patch('services.embedding_service.embed_text', return_value=None)
        self.embed_patcher.start()
        self.embed_batch_patcher = patch(
            'services.embedding_service.embed_texts_batch',
            side_effect=lambda texts, *a, **k: [None] * len(texts))
        self.embed_batch_patcher.start()
        # 짧은 본문이면 실제 Gemini Vision으로 보완하는 경로가 열릴 수 있다. E2E가
        # 개발자 API 키·무료 한도를 소비하거나 네트워크에서 멈추지 않도록 함께 격리한다.
        self.vision_patcher = patch('scraper_engine.ScraperEngine.capture_page_vision', return_value='')
        self.vision_patcher.start()
        # 수집 finally의 자동 유지보수는 운영 config.json에 실행일을 기록한다. E2E는
        # 테스트 DB의 수집 파이프라인만 검증하므로 운영 설정 파일 I/O도 격리한다.
        self.maintenance_patcher = patch(
            'history_manager.run_maintenance',
            return_value={"archived": 0, "cleaned": 0},
        )
        self.maintenance_patcher.start()

        # Patch AI Classifier directly
        self.ai_classify_patcher = patch('ai_classifier.classify_post_async')
        self.mock_classify = self.ai_classify_patcher.start()
        
        # Setup Mock AI Response
        self.mock_classify.return_value = {
            "category": "테스트", 
            "target": "전체", 
            "prediction": "mock",
            "is_relevant": True
        }
        
        # Patch Sites Configuration
        self.sites_patcher = patch('utils.load_sites')
        self.mock_load_sites = self.sites_patcher.start()
        self.mock_load_sites.return_value = [{
            "name": "Test Site",
            "url": self.mock_url,
            "selectors": {
                "list_item": "table tbody tr",
                "title": "td.title a",
                "date": "td.date",
                "link": "td.title a"
            },
            "base_url": "http://localhost:8888"
        }]

    def tearDown(self):
        # (테스트 격리 수정 2026-07-24) 열린 SQLite 연결을 먼저 닫아야 Windows에서
        # 파일을 지울 수 있다. 안 닫으면 다음 테스트의 setUp이 test_welfare.db를
        # 지우려다 PermissionError(WinError 32)로 깨진다.
        database.close_all_connections()

        self.db_patcher.stop()
        self.ai_classify_patcher.stop()
        self.sites_patcher.stop()
        self.lock_acquire_patcher.stop()
        self.lock_release_patcher.stop()
        self.semantic_gate_patcher.stop()
        self.embed_patcher.stop()
        self.embed_batch_patcher.stop()
        self.vision_patcher.stop()
        self.maintenance_patcher.stop()

        for _suffix in ('', '-wal', '-shm'):
            _p = self.test_db + _suffix
            if os.path.exists(_p):
                try:
                    os.remove(_p)
                except OSError:
                    pass  # 다음 테스트 setUp이 닫고 다시 지운다

    async def test_full_pipeline(self):
        """
        E2E Test: Scraping -> Filtering -> AI -> DB Storage -> History Check
        """
        service = ScraperService(headless=True)
        
        # 1. Run Scraper
        with patch('notifier.send_notification'), \
             patch('notifier.send_discord_notification'), \
             patch('notifier.send_google_chat_notification'):
            new_items = await service.run_scraper_async()
        
        # 2. Verify Scraped Items
        self.assertTrue(len(new_items) > 0, "Should have scraped items")
        item = new_items[0]
        self.assertEqual(item['site_name'], "Test Site")
        self.assertIn("New Welfare Program for 영유아", item['title'])
        self.assertEqual(item['link'], "http://localhost:8888/notice/1")
        self.assertEqual(item.get('category'), "테스트")
        
        # 3. Verify DB Storage
        conn = sqlite3.connect(self.test_db)
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM welfare_items WHERE link=?", (item['link'],))
        row = cursor.fetchone()
        conn.close()
        
        self.assertIsNotNone(row, "Item should be in database")
        
        # 4. Run Again (Duplicate Check)
        with patch('notifier.send_notification'), \
             patch('notifier.send_discord_notification'), \
             patch('notifier.send_google_chat_notification'):
            second_run_items = await service.run_scraper_async()
            
        self.assertEqual(len(second_run_items), 0, "Should not return duplicates")
        
    async def test_stop_signal(self):
        """Test if stop signal aborts operation"""
        service = ScraperService(headless=True)
        stop_event = asyncio.Event()
        
        # Set stop event immediately
        stop_event.set()
        
        with patch('notifier.send_notification'):
            items = await service.run_scraper_async(stop_event=stop_event)
            
        self.assertEqual(items, [], "Should return empty list on stop")

if __name__ == '__main__':
    unittest.main()
