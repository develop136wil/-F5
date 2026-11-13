from dataclasses import replace
import sqlite3
import unittest
from unittest.mock import AsyncMock, patch

from services.news_profile import capture_legacy_profile
from services.news_analysis_context import NewsAnalysisContext
from services import news_profile_store as store


class TestNewsAnalysisContext(unittest.IsolatedAsyncioTestCase):
    def test_body_hint_linebreaks_negatives_and_section_boundaries(self):
        from services.news_profile import NewsProfile
        context = NewsAnalysisContext(NewsProfile('body', 1, '대상', '전국',
            characteristics=('장애인',)))
        positives = ['신청 대상:\n장애인', '지원 대상： 장애인 및 가족',
                     '참여 대상: 등록 장애인', '모집 대상: 장애인\n문의: 담당자']
        negatives = ['신청 대상: 장애인 대상 아님', '지원 대상: 장애인은 대상이 아닙니다',
                     '신청 대상: 장애인 미해당', '신청 대상:\n\n장애인',
                     '신청 대상:\n문의: 장애인 담당자', '문의: 장애인\n신청 대상: 일반',
                     '지원 대상: 장애인이 아닌 사람']
        for text in positives:
            with self.subTest(text=text):
                self.assertEqual(context.body_target_hint(text), ('장애인',))
        for text in negatives:
            with self.subTest(text=text):
                self.assertEqual(context.body_target_hint(text), ())

    def test_body_hint_requires_target_heading_and_is_not_acceptance(self):
        from services.news_profile import NewsProfile
        context = NewsAnalysisContext(NewsProfile('body', 1, '대상', '전국',
            characteristics=('일반', '장애인')))
        self.assertEqual(context.body_target_hint('신청 대상: 장애인 및 가족'), ('장애인',))
        for text in ('메뉴: 장애인 지원', '신청 대상: 장애인은 제외',
                     '신청 대상: 비장애인', '신청 대상: 장애인복지관 직원',
                     '일반 공지', None):
            self.assertEqual(context.body_target_hint(text), ())
        self.assertEqual(context.route_title('이동 서비스 안내')[0], 'semantic_candidate')
        self.assertEqual(context.body_target_hint('x'*12000+'\n신청 대상: 장애인'), ())

    def test_custom_categories_reach_candidate_search_without_general_noise(self):
        from services.news_profile import NewsProfile
        context = NewsAnalysisContext(NewsProfile('p', 1, '대상', '전국',
            life_cycles=('노년',), characteristics=('일반', '장애인')))
        self.assertEqual(context.route_title('장애인 이동 지원'), ('matched', ('장애인',)))
        self.assertEqual(context.route_title('일반 공지'), ('semantic_candidate', ()))
        self.assertIn('찾을 대상: 노년, 장애인', context.semantic_text)
        self.assertEqual(context.profile.characteristics, ('일반', '장애인'))
        # A title without the exact tag is still eligible for semantic review.
        self.assertEqual(context.route_title('이동 편의 지원 안내')[0], 'semantic_candidate')

    def setUp(self):
        self.profile = capture_legacy_profile(
            region='도봉', max_age_months=108, keywords=['영유아'],
            excluded_keywords=['채용'], site_ids=['1'], ai_prompt='고정된 원래 지침',
            semantic_text='고정된 의미 기준', relevance_enabled=True)
        self.context = NewsAnalysisContext(self.profile)

    def test_title_uses_snapshot_not_global_keywords(self):
        with patch('content_filter.load_keywords', side_effect=AssertionError('global read')), \
             patch('content_filter.load_excluded_keywords', side_effect=AssertionError('global read')):
            self.assertEqual(self.context.route_title('영유아 지원'), ('matched', ('영유아',)))
            self.assertEqual(self.context.route_title('영유아 채용'), ('excluded', ()))
            self.assertEqual(self.context.route_title('노년 지원'), ('semantic_candidate', ()))
            empty = NewsAnalysisContext(replace(self.profile, keywords=(), excluded_keywords=()))
            self.assertEqual(empty.route_title('영유아 채용'), ('semantic_candidate', ()))

    def test_interest_topics_are_search_and_semantic_clues_without_mutation(self):
        context = NewsAnalysisContext(replace(self.profile, interest_topics=('생활지원', '주거')))
        self.assertEqual(context.route_title('주거 지원 안내'), ('matched', ('주거',)))
        self.assertIn('찾을 지원 분야: 생활지원, 주거', context.semantic_text)
        self.assertEqual(context.profile.keywords, ('영유아',))
        self.assertEqual(context.profile.interest_topics, ('생활지원', '주거'))

    def test_post_filter_uses_original_age_and_does_not_mutate_input(self):
        item = {'target': '아동', 'start_age_months': 120}
        with patch('services.relevance_filter.load_config', side_effect=AssertionError('global read')):
            result, passed, _ = self.context.post_filter(item)
        self.assertFalse(passed)
        self.assertFalse(result['age_match'])
        self.assertNotIn('age_match', item)

    async def test_ai_uses_explicit_prompt_even_if_global_changes(self):
        import ai_classifier
        for prompt in ('원래 지침', ''):
            context = NewsAnalysisContext(replace(self.profile, ai_prompt=prompt))
            with patch.object(ai_classifier.utils, 'get_env_value', side_effect=AssertionError('global read')), \
                 patch.object(ai_classifier, '_call_gemini_json', new_callable=AsyncMock,
                              return_value={'is_relevant': True}) as call:
                result = await context.classify(object(), title='테스트', link='https://example.invalid')
                self.assertTrue(result['is_relevant'])
                call.assert_awaited_once()
                request = call.call_args.args[1]
                if prompt:
                    self.assertIn(prompt, request)
                else:
                    self.assertNotIn('[User Custom Instructions', request)

    async def test_legacy_caller_still_reads_global_prompt(self):
        import ai_classifier
        with patch.object(ai_classifier.utils, 'get_env_value', return_value='기존 환경 지침'), \
             patch.object(ai_classifier, '_call_gemini_json', new_callable=AsyncMock,
                          return_value={}) as call:
            await ai_classifier.classify_post_async(object(), '제목', 'https://example.invalid')
            self.assertIn('기존 환경 지침', call.call_args.args[1])

    async def test_custom_does_not_fall_back_to_infant_rules(self):
        for changes in ({'general_mode': 'custom_v1', 'bokjiro_mode': 'unsupported'},
                        {'characteristics': ('한부모',)}, {'min_age_months': 120}):
            # Keep numeric interval valid so the guard, rather than construction, is tested.
            context = NewsAnalysisContext(replace(self.profile, max_age_months=240, **changes))
            with self.assertRaises(ValueError):
                context.post_filter({})
            with self.assertRaises(ValueError):
                await context.classify(object(), title='테스트', link='https://example.invalid')

    def test_reload_context_after_profile_edit_preserves_run_version(self):
        conn = sqlite3.connect(':memory:')
        try:
            store.ensure_schema(conn)
            original = replace(self.profile, profile_id='copy')
            store.create_profile(conn, original)
            run_id = store.prepare_run(conn, 'copy', expected_revision=1,
                                       sites=[{'id': 1, 'name': '테스트', 'url': 'https://example.invalid'}])
            store.save_revision(conn, replace(original, revision=2, keywords=['노년']), expected_revision=1)
            context = NewsAnalysisContext.from_run(conn, run_id)
            self.assertEqual(context.profile, original)
            self.assertEqual(context.semantic_text, '고정된 의미 기준')
            self.assertEqual(context.route_title('영유아 지원')[0], 'matched')
        finally:
            conn.close()
