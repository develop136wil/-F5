import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import embedding_service, relevance_filter
from services.scraper_service import ScraperService


class TestDynamicRelevanceProfile(unittest.TestCase):
    def test_default_text_is_byte_compatible(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                relevance_filter.build_profile_text(),
                '도봉구 영유아 및 아동(0~9세, 초등 저학년 포함) 대상 복지·돌봄·보육·교육·'
                '양육 지원, 가족지원, 건강·의료, 바우처, 수당, 프로그램 및 행사 신청 안내',
            )

    def test_region_and_age_changes_are_read_each_time(self):
        with patch.dict(os.environ, {
            'TARGET_REGION': '서울특별시',
            'TARGET_AGE_MAX_MONTHS': '120',
        }, clear=True):
            first = relevance_filter.build_profile_text()
            os.environ['TARGET_REGION'] = '부산광역시'
            os.environ['TARGET_AGE_MAX_MONTHS'] = '72'
            second = relevance_filter.build_profile_text()

        self.assertIn('서울특별시', first)
        self.assertIn('0~10세', first)
        self.assertIn('부산광역시', second)
        self.assertIn('0~6세', second)
        self.assertNotEqual(first, second)

    def test_explicit_profile_override_is_read_each_time(self):
        with patch.dict(os.environ, {'RELEVANCE_PROFILE': '첫 번째 기준'}, clear=True):
            self.assertEqual(relevance_filter.build_profile_text(), '첫 번째 기준')
            os.environ['RELEVANCE_PROFILE'] = '바뀐 기준'
            self.assertEqual(relevance_filter.build_profile_text(), '바뀐 기준')


class TestSemanticProfileUse(unittest.IsolatedAsyncioTestCase):
    async def test_semantic_gate_uses_current_settings(self):
        service = ScraperService(headless=True)
        profile_embed = AsyncMock(return_value=[1.0, 0.0])
        item_embed = AsyncMock(return_value=[1.0, 0.0])
        with patch.dict(os.environ, {
            'TARGET_REGION': '부산광역시',
            'TARGET_AGE_MAX_MONTHS': '72',
        }, clear=True), patch.object(
            embedding_service, 'embed_semantic_profile_async', profile_embed
        ), patch.object(embedding_service, 'embed_text_async', item_embed):
            result = await service._semantic_gate([{'title': '돌봄 지원'}], client=object())

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['title'], '돌봄 지원')
        self.assertEqual(result[0]['_gate_score'], 1.0)
        profile_text = profile_embed.await_args.args[0]
        self.assertIn('부산광역시', profile_text)
        self.assertIn('0~6세', profile_text)

    async def test_profile_embedding_is_cached_by_text(self):
        embedding_service._semantic_profile_cache.clear()
        mocked = AsyncMock(return_value=[0.1, 0.2])
        try:
            with patch.object(embedding_service, 'embed_text_async', mocked):
                first = await embedding_service.embed_semantic_profile_async('같은 기준')
                second = await embedding_service.embed_semantic_profile_async('같은 기준')
                third = await embedding_service.embed_semantic_profile_async('다른 기준')
        finally:
            embedding_service._semantic_profile_cache.clear()

        self.assertEqual(first, second)
        self.assertEqual(third, [0.1, 0.2])
        self.assertEqual(mocked.await_count, 2)


if __name__ == '__main__':
    unittest.main()
