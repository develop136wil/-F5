"""AI 분석 백로그(history_manager.count_ai_backlog / get_ai_backlog) 전용 테스트.

(2026-08-06, GPT Codex 리뷰로 발견) 하루 자체 상한(150건) 초과 등으로 AI 분석 없이
저장된 항목이, 링크가 이미 DB에 있다는 이유만으로 다음 수집에서 "이미 수집된 항목"으로
걸러져 사이트에 다시 나타나지 않는 한 영구히 재분석되지 않던 문제(실측: 운영 DB 191건)에
대한 회귀 방지 테스트. 실제 임시 DB를 사용한다(test_policy_sync_embed_batch.py와 동일 패턴).
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
import history_manager


class TestAiBacklog(unittest.TestCase):
    def setUp(self):
        self.test_db = 'test_ai_backlog.db'
        database.close_all_connections()
        for _suffix in ('', '-wal', '-shm'):
            _p = self.test_db + _suffix
            if os.path.exists(_p):
                try:
                    os.remove(_p)
                except OSError:
                    pass
        self.db_patcher = patch('database.DB_FILE', self.test_db)
        self.db_patcher.start()
        database.close_all_connections()
        database.init_db()

    def tearDown(self):
        database.close_all_connections()
        self.db_patcher.stop()
        for _suffix in ('', '-wal', '-shm'):
            _p = self.test_db + _suffix
            if os.path.exists(_p):
                try:
                    os.remove(_p)
                except OSError:
                    pass

    def _insert(self, link, title, ai_data=None, ai_skip_reason=None,
                ai_last_attempt_at=None, scraped_at='2026-08-01T00:00:00',
                is_archived=0):
        with database.get_connection() as conn:
            conn.execute(
                """INSERT INTO welfare_items
                   (site_name, title, link, scraped_at, ai_data, ai_skip_reason,
                    ai_last_attempt_at, is_archived)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ('테스트사이트', title, link, scraped_at, ai_data, ai_skip_reason,
                 ai_last_attempt_at, is_archived))
            conn.commit()

    def test_count_includes_null_and_empty_ai_data(self):
        """ai_data가 NULL이든 빈 문자열이든 '{}' 이든 백로그 대상으로 잡혀야 한다."""
        self._insert('https://x/1', '항목1', ai_data=None)
        self._insert('https://x/2', '항목2', ai_data='')
        self._insert('https://x/3', '항목3', ai_data='{}')
        self._insert('https://x/4', '항목4', ai_data='{"category": "육아"}')  # 정상 분석완료
        self.assertEqual(history_manager.count_ai_backlog(), 3)

    def test_failed_permanent_excluded_from_backlog(self):
        """failed_permanent로 표시된 항목은 무한 재시도 방지를 위해 백로그에서 제외."""
        self._insert('https://x/1', '항목1', ai_data=None, ai_skip_reason='quota')
        self._insert('https://x/2', '항목2', ai_data=None, ai_skip_reason='failed_permanent')
        self.assertEqual(history_manager.count_ai_backlog(), 1)
        rows = history_manager.get_ai_backlog(10)
        self.assertEqual([r['link'] for r in rows], ['https://x/1'])

    def test_count_ai_permanently_excluded(self):
        self._insert('https://x/1', '항목1', ai_data=None, ai_skip_reason='quota')
        self._insert('https://x/2', '항목2', ai_data=None, ai_skip_reason='failed_permanent')
        self._insert('https://x/3', '항목3', ai_data=None, ai_skip_reason='failed_permanent')
        self.assertEqual(history_manager.count_ai_permanently_excluded(), 2)

    def test_archived_items_excluded_from_backlog(self):
        """만료·아카이브된 항목까지 재분석할 필요는 없다."""
        self._insert('https://x/1', '항목1', ai_data=None, is_archived=1)
        self.assertEqual(history_manager.count_ai_backlog(), 0)

    def test_never_attempted_items_prioritized_over_failed_retries(self):
        """(핵심 회귀 방지) 한 번도 시도 안 한 항목이, 이미 실패했던 항목보다 먼저 나와야
        한다 — 안 그러면 최근 실패한 항목이 매 실행마다 큐 앞을 계속 점유해 새 미시도
        항목이 영원히 뒤로 밀리는 편향이 생긴다(GPT Codex 리뷰로 발견)."""
        self._insert('https://x/failed', '실패항목', ai_data=None,
                      ai_last_attempt_at='2026-08-06T00:00:00',
                      scraped_at='2026-08-01T00:00:00')  # 더 오래됐지만 이미 시도함
        self._insert('https://x/never', '미시도항목', ai_data=None,
                      ai_last_attempt_at=None,
                      scraped_at='2026-08-05T00:00:00')  # 더 최근이지만 아직 미시도
        rows = history_manager.get_ai_backlog(10)
        self.assertEqual([r['link'] for r in rows], ['https://x/never', 'https://x/failed'])

    def test_oldest_failed_retried_before_newest_failed(self):
        """미시도 항목이 없다면, 실패한 것들 중에선 가장 오래전에 실패한 것부터."""
        self._insert('https://x/recent_fail', '최근실패', ai_data=None,
                      ai_last_attempt_at='2026-08-06T00:00:00')
        self._insert('https://x/old_fail', '오래된실패', ai_data=None,
                      ai_last_attempt_at='2026-08-01T00:00:00')
        rows = history_manager.get_ai_backlog(10)
        self.assertEqual([r['link'] for r in rows], ['https://x/old_fail', 'https://x/recent_fail'])

    def test_limit_respected(self):
        for i in range(5):
            self._insert(f'https://x/{i}', f'항목{i}', ai_data=None)
        rows = history_manager.get_ai_backlog(2)
        self.assertEqual(len(rows), 2)

    def test_zero_limit_returns_empty(self):
        self._insert('https://x/1', '항목1', ai_data=None)
        self.assertEqual(history_manager.get_ai_backlog(0), [])


class TestDailyAiCountCountsFailedAttempts(unittest.TestCase):
    """(2026-08-06, GPT Codex 리뷰로 발견) get_daily_ai_count가 ai_call_at(성공 시에만
    찍힘) 기준이면 실패한 호출은 자체 하루 상한 집계에서 빠진다 — 실제 Google 쪽 호출은
    소비되므로 반복 실패 중 자체 상한을 과도하게 초과할 위험이 있었다."""

    def setUp(self):
        self.test_db = 'test_ai_backlog_daily_count.db'
        database.close_all_connections()
        for _suffix in ('', '-wal', '-shm'):
            _p = self.test_db + _suffix
            if os.path.exists(_p):
                try:
                    os.remove(_p)
                except OSError:
                    pass
        self.db_patcher = patch('database.DB_FILE', self.test_db)
        self.db_patcher.start()
        database.close_all_connections()
        database.init_db()

    def tearDown(self):
        database.close_all_connections()
        self.db_patcher.stop()
        for _suffix in ('', '-wal', '-shm'):
            _p = self.test_db + _suffix
            if os.path.exists(_p):
                try:
                    os.remove(_p)
                except OSError:
                    pass

    def test_failed_attempt_without_ai_call_at_still_counted(self):
        import datetime
        now_iso = datetime.datetime.now().isoformat()
        with database.get_connection() as conn:
            # 실패: ai_call_at은 없지만(성공 못 했으므로) ai_last_attempt_at은 있음
            conn.execute(
                """INSERT INTO welfare_items (site_name, title, link, scraped_at,
                   ai_data, ai_call_at, ai_last_attempt_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                ('사이트', '실패항목', 'https://x/1', now_iso, None, None, now_iso))
            conn.commit()
        self.assertEqual(history_manager.get_daily_ai_count(), 1)


class TestBacklogItemFailureIsolation(unittest.IsolatedAsyncioTestCase):
    """(2026-08-06, GPT Codex 리뷰) 백로그 항목 하나가 AI 호출 중 예외로 실패해도, 그
    실패 시각이 DB에 남고 이후 항목 처리가 계속돼야 한다 — 실패한 항목이 다음 실행에서도
    큐 맨 앞을 계속 점유해 뒤 항목을 밀어내면 안 된다."""

    def setUp(self):
        self.test_db = 'test_ai_backlog_isolation.db'
        database.close_all_connections()
        for _suffix in ('', '-wal', '-shm'):
            _p = self.test_db + _suffix
            if os.path.exists(_p):
                try:
                    os.remove(_p)
                except OSError:
                    pass
        self.db_patcher = patch('database.DB_FILE', self.test_db)
        self.db_patcher.start()
        database.close_all_connections()
        database.init_db()

    def tearDown(self):
        database.close_all_connections()
        self.db_patcher.stop()
        for _suffix in ('', '-wal', '-shm'):
            _p = self.test_db + _suffix
            if os.path.exists(_p):
                try:
                    os.remove(_p)
                except OSError:
                    pass

    async def test_first_item_exception_records_attempt_and_second_item_still_processed(self):
        import ai_classifier
        from services.scraper_service import ScraperService

        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO welfare_items (site_name, title, link, scraped_at) VALUES (?,?,?,?)",
                ('사이트', '실패항목', 'https://x/fail', '2026-08-01T00:00:00'))
            conn.execute(
                "INSERT INTO welfare_items (site_name, title, link, scraped_at) VALUES (?,?,?,?)",
                ('사이트', '성공항목', 'https://x/ok', '2026-08-01T00:00:00'))
            conn.commit()

        async def fake_fetch(*a, **k):
            return {'text': 'x' * 600, 'images': [], 'attachments': []}

        async def fake_classify(*a, **k):
            # retry_async가 내부적으로 재시도(retries=2)하므로, "몇 번째 호출인지"가 아니라
            # "어느 링크인지"로 실패를 고정해야 재시도로 우연히 성공하는 걸 막을 수 있다.
            if k.get('link') == 'https://x/fail':
                raise RuntimeError('429 simulated — 이 항목은 항상 실패')
            return {'category': '육아', 'target': '영유아', 'is_relevant': True}

        svc = ScraperService(headless=True)
        backlog = history_manager.get_ai_backlog(10)
        self.assertEqual(len(backlog), 2)

        with patch.object(ai_classifier, 'fetch_page_content_async', side_effect=fake_fetch), \
             patch.object(ai_classifier, 'classify_post_async', side_effect=fake_classify):
            for row in backlog:
                processed = await svc._process_single_item_ai(
                    None, object(), dict(row), None, None)
                history_manager.save_data([processed])

        fail_row = database.execute_query(
            "SELECT ai_data, ai_last_attempt_at FROM welfare_items WHERE link=?",
            ('https://x/fail',), fetch_one=True)
        ok_row = database.execute_query(
            "SELECT ai_data, ai_last_attempt_at FROM welfare_items WHERE link=?",
            ('https://x/ok',), fetch_one=True)

        # A failure is stored as diagnostics, never classified facts. Its cooldown
        # prevents the next collection from immediately multiplying API requests.
        from services import legacy_news_analysis
        self.assertEqual(legacy_news_analysis.failure(fail_row['ai_data'])['reason'], 'rate_limited')
        self.assertIsNotNone(fail_row['ai_last_attempt_at'])
        # 성공 항목: 첫 항목 실패와 무관하게 정상 처리됨
        self.assertIsNotNone(ok_row['ai_data'])

        # 남은 백로그는 실패한 1건뿐이어야 한다(성공한 건 빠져야 함)
        self.assertEqual(history_manager.get_ai_backlog(10), [])
        with database.get_connection() as conn:
            conn.execute("UPDATE welfare_items SET ai_data=json_set(ai_data, '$.analysis_failure.next_retry_at', ?) WHERE link=?",
                         ('2000-01-01T00:00:00', 'https://x/fail'))
            conn.commit()
        remaining = history_manager.get_ai_backlog(10)
        self.assertEqual([r['link'] for r in remaining], ['https://x/fail'])


class TestEarlyReturnPathsRecordAttempt(unittest.IsolatedAsyncioTestCase):
    """(2026-08-06, GPT Codex 전수조사로 발견) AI 분류 호출 전에 조기 return하는 경로들
    (로그인 페이지 감지, 캐시+마감경과, fetch 자체 실패)도 ai_last_attempt_at을 남겨야
    한다 — 안 그러면 이런 항목이 "한 번도 시도 안 함"으로 매 백로그 실행마다 큐 맨 앞을
    계속 차지해 뒤 항목들이 영원히 처리 안 되는 새로운 편향이 생긴다."""

    def setUp(self):
        self.svc = None

    async def asyncSetUp(self):
        from services.scraper_service import ScraperService
        self.svc = ScraperService(headless=True)

    async def test_login_page_early_return_records_attempt(self):
        import ai_classifier
        async def fake_fetch(*a, **k):
            return {'text': '로그인이 필요합니다 아이디와 비밀번호를 입력하세요',
                    'images': [], 'attachments': []}
        with patch.object(ai_classifier, 'fetch_page_content_async', side_effect=fake_fetch):
            r = await self.svc._process_single_item_ai(
                None, object(), {'title': 't', 'link': 'https://x/1', 'site_name': 's'}, None, None)
        self.assertIsNone(r.get('ai_data'))
        self.assertIsNotNone(r.get('ai_last_attempt_at'))

    async def test_content_fetch_failure_records_attempt(self):
        import ai_classifier
        async def fake_fetch_fail(*a, **k):
            raise RuntimeError('네트워크 오류')
        with patch.object(ai_classifier, 'fetch_page_content_async', side_effect=fake_fetch_fail):
            r = await self.svc._process_single_item_ai(
                None, object(), {'title': 't', 'link': 'https://x/2', 'site_name': 's'}, None, None)
        from services import legacy_news_analysis
        self.assertEqual(legacy_news_analysis.failure(r.get('ai_data'))['reason'], 'analysis_error')
        self.assertIsNotNone(r.get('ai_last_attempt_at'))

    async def test_cache_hit_with_expired_deadline_records_attempt(self):
        """(2026-08-06, 2차 GPT Codex 리뷰로 동작 변경) 캐시 히트+마감경과는 이제 ai_data를
        저장하고 정상 종료한다(유효한 AI 결과이므로) — 상세 검증은
        TestEarlyReturnPathsLeaveBacklog에서. 여기선 attempt 시각만 확인."""
        import ai_classifier
        import history_manager
        import datetime as _dt
        async def fake_fetch(*a, **k):
            return {'text': 'x' * 600, 'images': [], 'attachments': []}
        yesterday = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
        cached = {'category': '육아', 'deadline': yesterday, 'is_relevant': True}
        with patch.object(ai_classifier, 'fetch_page_content_async', side_effect=fake_fetch), \
             patch.object(history_manager, 'find_cached_ai_data_by_hash', return_value=cached):
            r = await self.svc._process_single_item_ai(
                None, object(), {'title': 't', 'link': 'https://x/3', 'site_name': 's'}, None, None)
        self.assertIsNotNone(r.get('ai_data'))
        self.assertIsNotNone(r.get('ai_last_attempt_at'))


class TestEarlyReturnPathsLeaveBacklog(unittest.IsolatedAsyncioTestCase):
    """(2026-08-06, 2차 GPT Codex 리뷰) ai_last_attempt_at만으로는 "영구 잔류"가 안
    풀린다는 지적 검증 — 마감경과·로그인판정 항목이 실제로 count_ai_backlog()에서
    빠지는지, 같은 배치를 두 번 돌려도 완료 항목에 추가 AI 호출이 없는지까지 확인한다."""

    def setUp(self):
        # (2026-08-06) 테스트 메서드마다 고유 파일명 사용 — Windows에서는 이 클래스의
        # 테스트들이 실제로 database.run_db()(스레드풀)를 거치는 async DB 호출을 하는데,
        # close_all_connections() 이후에도 백그라운드 스레드의 커넥션이 파일 핸들을 즉시
        # 놓지 않는 경우가 있어(WinError 32) 같은 파일명을 재사용하면 os.remove()가 조용히
        # 실패해(except OSError: pass) 이전 테스트의 잔여 행이 다음 테스트로 새는 실제
        # 사례를 재현·확인했다. 메서드별 고유 파일명이면 이 문제 자체가 발생하지 않는다.
        self.test_db = f'test_ai_backlog_early_return_leaves_{self._testMethodName}.db'
        database.close_all_connections()
        for _suffix in ('', '-wal', '-shm'):
            _p = self.test_db + _suffix
            if os.path.exists(_p):
                try:
                    os.remove(_p)
                except OSError:
                    pass
        self.db_patcher = patch('database.DB_FILE', self.test_db)
        self.db_patcher.start()
        database.close_all_connections()
        database.init_db()

    def tearDown(self):
        database.close_all_connections()
        self.db_patcher.stop()
        for _suffix in ('', '-wal', '-shm'):
            _p = self.test_db + _suffix
            if os.path.exists(_p):
                try:
                    os.remove(_p)
                except OSError:
                    pass

    async def test_cache_hit_expired_deadline_leaves_backlog(self):
        import ai_classifier
        import history_manager as hm
        import datetime as _dt
        from services.scraper_service import ScraperService

        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO welfare_items (site_name, title, link, scraped_at) VALUES (?,?,?,?)",
                ('사이트', '마감지난캐시항목', 'https://x/cached_expired', '2026-08-01T00:00:00'))
            conn.commit()

        async def fake_fetch(*a, **k):
            return {'text': 'x' * 600, 'images': [], 'attachments': []}
        yesterday = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
        cached = {'category': '육아', 'deadline': yesterday, 'is_relevant': True}

        svc = ScraperService(headless=True)
        row = hm.get_ai_backlog(10)[0]
        with patch.object(ai_classifier, 'fetch_page_content_async', side_effect=fake_fetch), \
             patch.object(hm, 'find_cached_ai_data_by_hash', return_value=cached):
            processed = await svc._process_single_item_ai(None, None, dict(row), None, None)
        hm.save_data([processed])

        self.assertIsNotNone(processed.get('ai_data'))
        self.assertEqual(hm.count_ai_backlog(), 0)

    async def test_ai_classified_expired_deadline_leaves_backlog(self):
        import ai_classifier
        import history_manager as hm
        import datetime as _dt
        from services.scraper_service import ScraperService

        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO welfare_items (site_name, title, link, scraped_at) VALUES (?,?,?,?)",
                ('사이트', '마감지난AI항목', 'https://x/ai_expired', '2026-08-01T00:00:00'))
            conn.commit()

        async def fake_fetch(*a, **k):
            return {'text': 'x' * 600, 'images': [], 'attachments': []}
        yesterday = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()

        async def fake_classify(*a, **k):
            return {'category': '육아', 'deadline': yesterday, 'is_relevant': True}

        svc = ScraperService(headless=True)
        row = hm.get_ai_backlog(10)[0]
        with patch.object(ai_classifier, 'fetch_page_content_async', side_effect=fake_fetch), \
             patch.object(ai_classifier, 'classify_post_async', side_effect=fake_classify):
            processed = await svc._process_single_item_ai(None, None, dict(row), None, None)
        hm.save_data([processed])

        self.assertIsNotNone(processed.get('ai_data'))
        self.assertEqual(hm.count_ai_backlog(), 0)

    async def test_ai_classified_login_title_with_source_confirmation_marked_failed_permanent(self):
        """(2026-08-06, 3차 GPT Codex 리뷰) AI가 만든 제목만 보고 failed_permanent로 확정하면
        한 번의 AI 오분류로 실제 복지정보가 영구 제외될 위험이 있다 — 원문(text_content)에도
        동일한 로그인 신호가 있어 "구조적으로 막힌 사이트"임이 교차 확인될 때만 확정한다."""
        import ai_classifier
        import history_manager as hm
        from services.scraper_service import ScraperService

        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO welfare_items (site_name, title, link, scraped_at) VALUES (?,?,?,?)",
                ('사이트', '원래제목', 'https://x/login_after_ai', '2026-08-01T00:00:00'))
            conn.commit()

        async def fake_fetch(*a, **k):
            # 2000자 이상이라 AI 호출 전 조기감지(len<2000 조건)는 안 걸리지만, 원문 자체에
            # 로그인 신호가 있어 사후 교차검증은 통과해야 하는 케이스.
            return {'text': ('로그인이 필요합니다 아이디와 비밀번호를 입력하세요. ' + 'x' * 2000),
                    'images': [], 'attachments': []}

        async def fake_classify(*a, **k):
            return {'title': '로그인 후 이용해주세요 회원', 'is_relevant': True}

        svc = ScraperService(headless=True)
        row = hm.get_ai_backlog(10)[0]
        with patch.object(ai_classifier, 'fetch_page_content_async', side_effect=fake_fetch), \
             patch.object(ai_classifier, 'classify_post_async', side_effect=fake_classify):
            processed = await svc._process_single_item_ai(None, None, dict(row), None, None)
        hm.save_data([processed])

        # 로그인 페이지 기반 오분류는 ai_data로 저장하면 안 된다(신뢰 불가) — 원문에서도
        # 로그인 신호가 확인됐으므로 failed_permanent로 표시돼 백로그에서 빠진다.
        self.assertIsNone(processed.get('ai_data'))
        self.assertEqual(processed.get('ai_skip_reason'), 'failed_permanent')
        self.assertEqual(hm.count_ai_backlog(), 0)

    async def test_ai_only_login_misclassification_without_source_signal_stays_retryable(self):
        """원문에는 로그인 신호가 없는데 AI만 제목을 로그인 관련으로 오판했다면, 실제
        복지정보일 수 있으므로 영구 제외하지 말고 백로그에 남겨 재시도 기회를 준다."""
        import ai_classifier
        import history_manager as hm
        from services.scraper_service import ScraperService

        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO welfare_items (site_name, title, link, scraped_at) VALUES (?,?,?,?)",
                ('사이트', '원래제목', 'https://x/ai_only_login_misfire', '2026-08-01T00:00:00'))
            conn.commit()

        async def fake_fetch(*a, **k):
            # 원문엔 로그인 신호가 전혀 없는 정상 본문
            return {'text': '영유아 보육료 지원 안내입니다. ' + 'x' * 600,
                    'images': [], 'attachments': []}

        async def fake_classify(*a, **k):
            # AI가 이유 없이 제목을 로그인 관련 문구로 오분류(현실적으로 드물지만 방어 대상)
            return {'title': '로그인 후 이용해주세요 회원', 'is_relevant': True}

        svc = ScraperService(headless=True)
        row = hm.get_ai_backlog(10)[0]
        with patch.object(ai_classifier, 'fetch_page_content_async', side_effect=fake_fetch), \
             patch.object(ai_classifier, 'classify_post_async', side_effect=fake_classify):
            processed = await svc._process_single_item_ai(None, None, dict(row), None, None)
        hm.save_data([processed])

        self.assertIsNone(processed.get('ai_data'))
        self.assertIsNone(processed.get('ai_skip_reason'))
        # 영구 제외가 아니므로 다음 실행에서 다시 시도될 수 있게 백로그에 남아있어야 한다
        self.assertEqual(hm.count_ai_backlog(), 1)

    async def test_completed_items_not_reprocessed_on_second_batch_run(self):
        """같은 배치를 두 번 실행해도 이미 완료된 항목엔 추가 AI 호출이 없어야 한다."""
        import ai_classifier
        import history_manager as hm
        from services.scraper_service import ScraperService

        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO welfare_items (site_name, title, link, scraped_at) VALUES (?,?,?,?)",
                ('사이트', '정상항목', 'https://x/normal', '2026-08-01T00:00:00'))
            conn.commit()

        call_count = {'n': 0}

        async def fake_fetch(*a, **k):
            return {'text': 'x' * 600, 'images': [], 'attachments': []}

        async def fake_classify(*a, **k):
            call_count['n'] += 1
            return {'category': '육아', 'is_relevant': True}

        svc = ScraperService(headless=True)
        with patch.object(ai_classifier, 'fetch_page_content_async', side_effect=fake_fetch), \
             patch.object(ai_classifier, 'classify_post_async', side_effect=fake_classify):
            # 1차 실행
            row = hm.get_ai_backlog(10)[0]
            processed = await svc._process_single_item_ai(None, None, dict(row), None, None)
            hm.save_data([processed])
            self.assertEqual(call_count['n'], 1)
            self.assertEqual(hm.count_ai_backlog(), 0)

            # 2차 실행: 백로그 조회 자체가 0건을 반환해야 하고, 혹시 재조회하더라도 호출량이 늘면 안 됨
            remaining = hm.get_ai_backlog(10)
            self.assertEqual(remaining, [])
            self.assertEqual(call_count['n'], 1)


if __name__ == '__main__':
    unittest.main()
