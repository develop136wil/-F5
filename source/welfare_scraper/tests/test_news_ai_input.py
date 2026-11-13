import copy
import json
import os
import unittest
from unittest.mock import AsyncMock, Mock, patch

from services import news_ai_input as inputs
from services import news_attachments


class TestNewsInput(unittest.TestCase):
    def test_short_and_exact_limit_are_unchanged(self):
        for size in (1, 500, 6000):
            with self.subTest(size=size):
                source = '가' * size
                text, coverage = inputs.prepare(source, 6000)
                self.assertEqual(text, source)
                self.assertEqual(coverage['status'], 'complete')
                self.assertEqual(coverage['selected_chars'], size)
                self.assertEqual(inputs.prompt_notice(coverage), '')

    def test_missing_body_is_not_complete(self):
        text, coverage = inputs.prepare(None)
        self.assertEqual(text, '')
        self.assertEqual(coverage['status'], 'empty')
        self.assertIn('제목', coverage['note'])

    def test_length_boundary_and_many_markers_never_exceed_budget(self):
        sources = ['가' * 9000, ('--- [첨부파일: 자료] ---\n' + '가' * 1000 + '\n') * 80]
        for source in sources:
            for limit in (1, 32, 100, 299, 300, 601, 6000):
                with self.subTest(limit=limit, size=len(source)):
                    text, coverage = inputs.prepare(source, limit)
                    self.assertLessEqual(len(text), limit)
                    self.assertGreater(len(text), 0)
                    self.assertLessEqual(coverage['selected_chars'], len(text))
                    self.assertEqual(coverage['status'], 'partial')

    def test_each_document_and_page_receive_space(self):
        page = {'attachment_text': ('--- [첨부파일: 첫째.pdf] ---\n' + '첫 자료 ' * 3000
                                   + '\n--- [첨부파일: 둘째.hwpx] ---\n둘째 핵심 ' + '나' * 8000),
                'text': '게시글 핵심 ' + '본문 ' * 3000}
        before = copy.deepcopy(page)
        source = news_attachments.merge_page_text(page, include_notices=False)
        text, coverage = inputs.prepare(source, 6000)
        for expected in ('첫째.pdf', '둘째.hwpx', '둘째 핵심', '게시글 핵심'):
            self.assertIn(expected, text)
        self.assertEqual(page, before)
        self.assertEqual(coverage['total_chars'], len(source))

    def test_middle_eligibility_deadline_and_tail_retained_in_source_order(self):
        source = ('머리말 ' + '가' * 7000 + '\n지원 대상: 한부모 가족. 단, 중복 수급자는 제외합니다.\n'
                  + '나' * 5000 + '\n신청 기간: 2026-09-01부터 2026-09-30까지\n'
                  + '다' * 5000 + '\n지원 내용: 교육비 지원\n' + '라' * 5000
                  + '\n문의: 02-123-4567\n' + '마' * 4000 + '끝부분 별도 주의사항')
        text, coverage = inputs.prepare(source, 6000)
        facts = ('머리말', '중복 수급자는 제외합니다', '2026-09-30', '교육비 지원', '02-123-4567', '끝부분 별도 주의사항')
        positions = [text.index(value) for value in facts]
        self.assertEqual(positions, sorted(positions))
        self.assertLessEqual(len(text), 6000)
        self.assertIn('빠진 부분', coverage['note'])
        # Excerpts are verbatim source intervals, never a generated summary.
        for excerpt in text.split(inputs.GAP):
            self.assertIn(excerpt, source)
        self.assertEqual(coverage['selected_chars'], sum(map(len, text.split(inputs.GAP))))

    def test_small_section_returns_spare_to_long_section(self):
        source = '--- [첨부파일: 짧음] ---\n짧은 공고\n--- [게시글 본문] ---\n' + '본문' * 5000
        text, _ = inputs.prepare(source, 6000)
        self.assertIn('짧은 공고', text)
        self.assertGreater(len(text), 5500)

    def test_invalid_limit_uses_safe_default(self):
        for value in ('bad', '0', '-1', True, float('inf')):
            with self.subTest(value=value):
                self.assertEqual(inputs.content_limit(value), 6000)
        self.assertEqual(inputs.content_limit(999999), 60000)
        with patch.dict(os.environ, {'AI_CONTENT_MAX_CHARS': '1200'}):
            self.assertEqual(inputs.prepare('가' * 2000)[1]['max_chars'], 1200)

    def test_legacy_prefix_is_exact_for_queued_jobs(self):
        source = '가' * 7000 + '마지막'
        text, meta = inputs.prepare(source, 6000, strategy='prefix')
        self.assertEqual(text, source[:6000])
        self.assertEqual(meta['recipe'], 'news-prefix-v1')


class TestNewsInputCalls(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # These compatibility cases isolate the configured character recipe.
        # Full-request fitting at the unchanged default allowance is exercised
        # with guarded SDK mocks in test_generation_input_budget instead.
        budget = patch('services.ai_coordinator.input_token_budget', return_value=100000)
        budget.start()
        self.addCleanup(budget.stop)

    async def test_classifier_calls_once_and_overwrites_model_coverage(self):
        import ai_classifier
        source = '가' * 7000 + '\n신청 기간: 2026-09-30\n' + '나' * 6000
        with patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(return_value={
                'is_relevant': True, 'category': '복지', 'target': '일반',
                'source_input': {'status': 'complete', 'note': 'wrong'}})) as api:
            result = await ai_classifier.classify_post_async(object(), '제목', 'url', content=source,
                content_max_chars=6000, custom_prompt='', strict=True)
        api.assert_awaited_once()
        self.assertEqual(result['source_input']['status'], 'partial')
        prompt = api.call_args.args[1]
        self.assertIn('2026-09-30', prompt)
        self.assertIn('[Input coverage]', prompt)
        self.assertNotIn('wrong', result['source_input']['note'])

    async def test_legacy_classifier_keeps_old_prompt_without_new_metadata(self):
        import ai_classifier
        with patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(return_value={
                'is_relevant': True, 'category': '복지', 'target': '일반'})) as api:
            result = await ai_classifier.classify_post_async(object(), '제목', 'url', content='가' * 7000 + 'TAIL',
                content_max_chars=6000, custom_prompt='', strict=True, input_strategy='prefix')
        self.assertNotIn('source_input', result)
        self.assertNotIn('TAIL', api.call_args.args[1])
        self.assertNotIn('[Input coverage]', api.call_args.args[1])

    async def test_main_collection_saves_coverage_and_versions_cache_by_limit(self):
        import ai_classifier
        import history_manager
        from services.scraper_service import ScraperService
        service = ScraperService(headless=True)
        service.ai_rate_limiter = Mock(acquire=AsyncMock())
        page = {'text': '내용' * 5000, 'images': [], 'attachments': []}
        hashes = []
        with patch.object(ai_classifier, 'fetch_page_content_async', new=AsyncMock(return_value=page)), \
             patch.object(history_manager, 'find_cached_ai_data_by_hash', return_value=None), \
             patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(side_effect=[
                 {'is_relevant': True, 'category': '복지', 'target': '일반'} for _ in range(3)])) as api:
            for limit in ('6000', '3000', '3000'):
                with patch.dict(os.environ, {'AI_CONTENT_MAX_CHARS': limit}):
                    result = await service._process_single_item_ai(None, object(),
                        {'title': '제목', 'link': 'https://example.invalid/post'})
                hashes.append(result['content_hash'])
                saved = json.loads(result['ai_data'])['source_input']
                self.assertEqual(saved['status'], 'partial')
                self.assertEqual(saved['max_chars'], int(limit))
        self.assertEqual(api.await_count, 3)
        self.assertNotEqual(hashes[0], hashes[1])
        self.assertEqual(hashes[1], hashes[2])
