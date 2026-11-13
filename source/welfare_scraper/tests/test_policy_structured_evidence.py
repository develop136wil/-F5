"""Synthetic full-source policy guards; no actual AI, Notion or operational DB."""
import json
import hashlib
import os
import unittest
from unittest.mock import AsyncMock, patch

import ai_classifier
from services import policy_sync as ps
from services.policy_evidence import validate_policy_evidence, structured_policy_issues
from services.policy_source_section import select_policy_section


def extraction(source, **facts):
    return {'support_detail': source, '_source_evidence': {'support_detail': source}, **facts}


class StructuredEvidence(unittest.TestCase):
    def test_verified_complete_child_range_supports_only_intersecting_child_bands(self):
        for start, end, tags in ((0, 107, ['영아', '유아', '학령기']),
                                 (0, 83, ['영아', '유아']),
                                 (35, 36, ['영아', '유아']),
                                 (83, 84, ['유아', '학령기'])):
            source = f'지원대상: 생후 {start}~{end}개월 아동. 교육비 지원.'
            facts = {'start_age_months': start, 'end_age_months': end, 'life_cycle': tags}
            with self.subTest(start=start, end=end):
                self.assertTrue(validate_policy_evidence(extraction(source, **facts), source))
                self.assertEqual(facts['life_cycle'], tags)

    def test_child_overlap_does_not_infer_adult_all_age_or_nonoverlapping_stages(self):
        for end, invalid in ((35, ['유아', '학령기']), (83, ['학령기']),
                             (107, ['전생애', '청소년', '청년', '중장년', '노년', '임신/출산'])):
            source = f'지원대상: 생후 0~{end}개월 아동. 교육비 지원.'
            for tag in invalid:
                with self.subTest(end=end, tag=tag):
                    self.assertIn('life_cycle', structured_policy_issues(extraction(source,
                        start_age_months=0, end_age_months=end, life_cycle=[tag]), source))

    def test_child_overlap_requires_exact_complete_numeric_bounds(self):
        source = '지원대상: 9세 미만(0~107개월)의 아동. 교육비 지원.'
        for start, end in ((None, None), (0, None), (None, 107), (0, 108), (1, 107),
                           (0, 35), (36, 107), (True, 107)):
            with self.subTest(start=start, end=end):
                self.assertIn('life_cycle', structured_policy_issues(extraction(source,
                    start_age_months=start, end_age_months=end,
                    life_cycle=['영아', '유아', '학령기']), source))

    def test_child_overlap_cannot_borrow_guardian_excluded_disjoint_or_usage_ages(self):
        cases = [
            ('아동 교육비 지원. 신청 보호자는 0~107개월이어야 합니다.', 0, 107),
            ('교육비 지원. 제외 대상: 0~107개월 아동.', 0, 107),
            ('교육비 지원. 0~107개월 아동은 지원 대상이 아닙니다.', 0, 107),
            ('지원 대상은 0~35개월 또는 84~107개월 아동.', 0, 107),
            ('아동에게 포인트 지급. 사용기간은 출생일부터 2년.', 0, 24),
        ]
        for source, start, end in cases:
            with self.subTest(source=source):
                self.assertFalse(validate_policy_evidence(extraction(source,
                    start_age_months=start, end_age_months=end,
                    life_cycle=['영아', '유아', '학령기']), source))

    def test_unmentioned_structured_facts_cannot_borrow_a_valid_benefit_quote(self):
        source = '교육비 월 5만원 지원.'
        for facts in ({'residence': ['전국']}, {'life_cycle': ['전생애']},
                {'target_characteristics': ['한부모']}, {'start_age_months': 0},
                {'end_age_months': 1800}, {'contact': '02-0000-0000'}):
            with self.subTest(facts=facts):
                result = extraction(source, **facts)
                self.assertFalse(validate_policy_evidence(result, source))
                self.assertTrue(structured_policy_issues(result, source))
                self.assertEqual({key: result[key] for key in facts}, facts)

    def test_ordinary_explicit_policies_are_not_all_rejected(self):
        cases = [
            ('도봉구 주민에게 교통비 지원.', {'residence': ['도봉구']}),
            ('서울시 거주자에게 교통비 지원.', {'residence': ['서울특별시']}),
            ('부산광역시 해운대구 주민에게 지원.', {'residence': ['부산광역시 해운대구']}),
            ('전국민 대상 문화비 지원.', {'residence': ['전국']}),
            ('전국 지원 대상자에게 문화비 지원.', {'residence': ['전국']}),
            ('전국 어디서나 신청할 수 있습니다.', {'residence': ['전국']}),
            ('전국에 거주하는 주민 누구나 신청 가능합니다.', {'residence': ['전국']}),
            ('거주지와 관계없이 문화비 신청 가능합니다.', {'residence': ['전국']}),
            ('전국민 대상 문화비 지원. 중복 수급자는 제외합니다.', {'residence': ['전국']}),
            ('모든 연령을 대상으로 문화비 지원.', {'life_cycle': ['전생애']}),
            ('대상은 청년이며 문화비 지원.', {'life_cycle': ['청년']}),
            ('한부모 가구에 교육비 지원.', {'target_characteristics': ['한부모']}),
            ('북한이탈주민 가구에 주거비 지원.', {'target_characteristics': ['북한이탈주민']}),
            ('만 65세 이상에게 교통비 지원.', {'start_age_months': 780, 'life_cycle': ['노년']}),
            ('생후 2개월부터 6개월까지 영아에게 지원.',
                {'start_age_months': 2, 'end_age_months': 6, 'life_cycle': ['영아']}),
            ('만 6세 미만 아동에게 지원.', {'end_age_months': 71, 'life_cycle': []}),
            ('교육비 지원. 문의 02-1234-5678', {'contact': '02-1234-5678'}),
            ('교육비 지원.', {'residence': [], 'life_cycle': [], 'start_age_months': None,
                              'end_age_months': None, 'contact': ''}),
        ]
        for source, facts in cases:
            with self.subTest(source=source):
                self.assertTrue(validate_policy_evidence(extraction(source, **facts), source))

    def test_publisher_exclusion_and_proxy_do_not_prove_recipient_conditions(self):
        cases = [
            ('교육비 지원. 방문 장소: 도봉구 주민센터', {'residence': ['도봉구']}),
            ('교육비 지원. 제외 대상: 도봉구 거주자', {'residence': ['도봉구']}),
            ('교육비 지원. 청년은 지원 대상이 아닙니다.', {'life_cycle': ['청년']}),
            ('교육비 지원. 한부모 가구 제외.', {'target_characteristics': ['한부모']}),
            ('한부모가족지원센터에서 교육비 지원.', {'target_characteristics': ['한부모']}),
            ('노년복지관에서 아동 교육비 지원.', {'life_cycle': ['노년']}),
            ('아동 지원 대상자의 보호자는 노년이다.', {'life_cycle': ['노년']}),
            ('비장애 아동에게 교육비 지원. 장애인인 보호자가 자녀를 대신하여 신청한다.',
                {'target_characteristics': ['장애/발달지연']}),
            ('비장애 아동에게 교육비 지원. 장애가 있는 보호자가 신청한다.',
                {'target_characteristics': ['장애/발달지연']}),
            ('전국민 대상 문화비 지원. 단 도봉구 주민은 제외합니다.', {'residence': ['전국']}),
            ('전국민 대상 문화비 지원. 제외 대상:\n도봉구 거주자', {'residence': ['전국']}),
            ('교육비 지원. 기관 주소: 서울시 도봉구 주민센터.', {'residence': ['도봉구']}),
            ('만 6세 미만 아동에게 지원.', {'life_cycle': ['학령기']}),
            ('아동 교육비 지원. 신청자 보호자는 만 65세 이상이어야 한다.', {'start_age_months': 780}),
            ('지원 대상은 만 3~5세 또는 만 10~12세 아동.', {'start_age_months': 36, 'end_age_months': 155}),
        ]
        for source, facts in cases:
            with self.subTest(source=source):
                self.assertFalse(validate_policy_evidence(extraction(source, **facts), source))

    def test_malformed_structured_values_fail_closed(self):
        source = '교육비 지원.'
        for facts in ({'residence': '전국'}, {'life_cycle': [False]},
                      {'target_characteristics': [None]}, {'start_age_months': True},
                      {'end_age_months': '12'}, {'contact': []}):
            with self.subTest(facts=facts):
                self.assertFalse(validate_policy_evidence(extraction(source, **facts), source))


class ExtractionBoundary(unittest.IsolatedAsyncioTestCase):
    async def test_sensitive_input_hold_does_not_log_the_source_or_title(self):
        from services.ai_input_safety import AiInputReviewRequired, REVIEW_MESSAGE
        title = 'sensitive-title-marker'
        review = {}
        with patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(
                side_effect=AiInputReviewRequired(('email_address',)))), \
             patch.object(ai_classifier, 'logger') as logger:
            actual = await ai_classifier.extract_policy_fields_async(object(), title,
                'https://example.invalid', content='교육비 지원.', review=review)
        self.assertIsNone(actual)
        self.assertEqual(review['code'], 'ai_input_review_required')
        self.assertEqual(review['note'], REVIEW_MESSAGE)
        self.assertNotIn(title, str(logger.mock_calls))
        logger.error.assert_not_called()

    async def test_long_source_is_held_before_any_model_call(self):
        source = '교육비 월 5만원 지원.\n' + '안내문입니다.\n' * 1000 + '다른 교육비와 중복 지원 불가.'
        review = {}
        with patch.dict(os.environ, {'AI_CONTENT_MAX_CHARS': '6000'}), \
             patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock()) as call:
            actual = await ai_classifier.extract_policy_fields_async(object(), '교육비',
                'https://example.invalid', content=source, review=review)
        self.assertIsNone(actual)
        call.assert_not_awaited()
        self.assertEqual(review['code'], 'source_too_long')
        self.assertIn('기존 내용은 유지', review['note'])

    async def test_explicit_short_section_of_long_page_still_works(self):
        source = '교육비 월 5만원 지원.'
        page = '## 다른 정책\n' + '기타 설명\n' * 2000 + '\n## 교육비\n' + source
        selected = select_policy_section(page, '교육비')
        result = extraction(source)
        with patch.dict(os.environ, {'AI_CONTENT_MAX_CHARS': '6000'}), \
             patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(return_value=result)) as call:
            actual = await ai_classifier.extract_policy_fields_async(object(), '교육비',
                'https://example.invalid', content=selected)
        self.assertEqual(actual, result)
        self.assertEqual(call.await_count, 1)

    async def test_short_bad_structured_result_is_a_hold_not_empty_update(self):
        source = '교육비 월 5만원 지원.'
        review = {}
        with patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(
                return_value=extraction(source, residence=['전국']))) as call:
            actual = await ai_classifier.extract_policy_fields_async(object(), '교육비',
                'https://example.invalid', content=source, review=review)
        self.assertIsNone(actual)
        self.assertEqual(call.await_count, 1)
        self.assertEqual(review['fields'], ['residence'])

    async def test_input_limit_boundary_and_invalid_setting(self):
        source = '교육비 지원.'
        for limit, expected_calls in ((str(len(source)), 1), (str(len(source)-1), 0), ('bad', 0), ('0', 0)):
            with self.subTest(limit=limit), patch.dict(os.environ, {'POLICY_SOURCE_MAX_CHARS': limit}), \
                    patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(return_value=extraction(source))) as call:
                actual = await ai_classifier.extract_policy_fields_async(object(), '교육비',
                    'https://example.invalid', content=source)
                self.assertEqual(call.await_count, expected_calls)
                self.assertEqual(actual is not None, bool(expected_calls))


class PolicyHoldPersistence(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_check_revalidates_only_old_pending_on_unchanged_source(self):
        source = '교육비 월 5만원 지원.'
        url = 'https://example.invalid/policy'
        for version in ('policy-extract-v2-literal-conditions', 'policy-extract-v3-full-source-structured',
                        None, ps._EXTRACTION_PROMPT_VERSION):
            for proposal in (True, False):
                with self.subTest(version=version, proposal=proposal):
                    pending = {'fields': [{'field': '지원 내용', 'old_display': '기존', 'new_display': source, 'snapshot_current': True}],
                               'proposal_schema_version': 2,
                               'extraction_meta': {'prompt_version': version}} if proposal else {}
                    row = dict(id=1, page_id='fake-page', title='교육비', db_key='medical',
                        notion_fields=json.dumps({'support_detail': '교육비 월 3만원 지원.'}),
                        pending_diff=json.dumps(pending), change_status='detected', source_urls=json.dumps([url]),
                        source_hashes=json.dumps({url: hashlib.sha256(source.encode()).hexdigest()}),
                        source_texts=json.dumps({url: source}), last_extracted_fields=json.dumps(extraction(source)))
                    with patch.object(ps, '_active_configured_keys', return_value=['medical']), \
                         patch.object(ps, '_get_db_schema', return_value={}), \
                         patch.object(ps, '_db_query_strict', new=AsyncMock(return_value=[row])), \
                         patch.object(ps, '_db_write_strict', new=AsyncMock()) as write, \
                         patch.object(ps, '_fetch_source', new=AsyncMock(return_value=(source, source, False))), \
                         patch.object(ai_classifier, 'get_client', return_value=object()), \
                         patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(return_value=extraction(source))) as call, \
                         patch.object(ps, '_diff_fields_with_embeddings', new=AsyncMock(return_value=[
                             {'field': ps._PROP_LABEL['support_detail'], 'old': '교육비 월 3만원 지원.',
                              'new': source, 'level': 'high'}])) as diff, \
                         patch.object(ps, '_intensity_of', return_value='high'), \
                         patch.object(ps, '_visible_diffs', side_effect=lambda value: value):
                        actual = await ps.check_policies(['medical'], scraper=object())
                    needs = proposal and version != ps._EXTRACTION_PROMPT_VERSION
                    self.assertEqual(call.await_count, int(needs))
                    self.assertEqual(actual['changed'], int(needs))
                    sql, params = write.await_args.args
                    self.assertNotIn('notion_fields=', sql)
                    if needs:
                        self.assertEqual(diff.await_args.args[0]['support_detail'], '교육비 월 3만원 지원.')
                        self.assertIn('pending_diff=', sql)
                        updated = next(json.loads(v) for v in params if isinstance(v, str) and 'extraction_meta' in v)
                        self.assertEqual(updated['extraction_meta']['prompt_version'], ps._EXTRACTION_PROMPT_VERSION)
                    else:
                        self.assertNotIn('pending_diff=', sql)

    async def test_old_pending_revalidation_failure_preserves_existing_data(self):
        url = 'https://example.invalid/policy'
        source = '교육비 지원.'
        row = dict(id=1, page_id='fake-page', title='교육비', db_key='medical',
            notion_fields=json.dumps({'support_detail': '사용자 내용'}),
            pending_diff=json.dumps({'fields': [{'field': 'old'}], 'extraction_meta': {'prompt_version': 'v2'}}), change_status='detected',
            source_urls=json.dumps([url]), source_hashes=json.dumps({url: hashlib.sha256(source.encode()).hexdigest()}),
            source_texts=json.dumps({url: source}), last_extracted_fields=json.dumps({'support_detail': '기존 기준'}))
        for dead in (False, True):
            with self.subTest(dead=dead), patch.object(ps, '_active_configured_keys', return_value=['medical']), \
                 patch.object(ps, '_get_db_schema', return_value={}), \
                 patch.object(ps, '_db_query_strict', new=AsyncMock(return_value=[row])), \
                 patch.object(ps, '_db_write_strict', new=AsyncMock()) as write, \
                 patch.object(ps, '_fetch_source', new=AsyncMock(return_value=('', '', True) if dead else (source, source, False))), \
                 patch.object(ai_classifier, 'get_client', return_value=object()), \
                 patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(return_value=extraction(source, residence=['전국']))) as call:
                actual = await ps.check_policies(['medical'], scraper=object())
            self.assertEqual(actual['review_count'], 1)
            self.assertEqual(call.await_count, 0 if dead else 1)
            sql, params = write.await_args.args
            for column in ('pending_diff=', 'last_extracted_fields=', 'notion_fields='):
                self.assertNotIn(column, sql)
            self.assertEqual(json.loads(params[0]), json.loads(row['source_hashes']))
            self.assertEqual(json.loads(params[1]), json.loads(row['source_texts']))

    async def test_successful_revalidation_can_retire_stale_proposal_without_changing_notion(self):
        source = '교육비 월 5만원 지원.'
        url = 'https://example.invalid/policy'
        row = dict(id=1, page_id='fake-page', title='교육비', db_key='medical',
            notion_fields=json.dumps({'support_detail': source}),
            pending_diff=json.dumps({'fields': [{'field': 'old'}]}), change_status='detected', source_urls=json.dumps([url]),
            source_hashes=json.dumps({url: hashlib.sha256(source.encode()).hexdigest()}),
            source_texts=json.dumps({url: source}), last_extracted_fields='{}')
        with patch.object(ps, '_active_configured_keys', return_value=['medical']), \
             patch.object(ps, '_get_db_schema', return_value={}), \
             patch.object(ps, '_db_query_strict', new=AsyncMock(return_value=[row])), \
             patch.object(ps, '_db_write_strict', new=AsyncMock()) as write, \
             patch.object(ps, '_fetch_source', new=AsyncMock(return_value=(source, source, False))), \
             patch.object(ai_classifier, 'get_client', return_value=object()), \
             patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(return_value=extraction(source))) as call, \
             patch.object(ps, '_diff_fields_with_embeddings', new=AsyncMock(return_value=[])):
            actual = await ps.check_policies(['medical'], scraper=object())
        self.assertEqual(call.await_count, 1)
        self.assertEqual(actual['changed'], 0)
        self.assertEqual(actual['review_count'], 0)
        sql, params = write.await_args.args
        self.assertNotIn('notion_fields=', sql)
        self.assertIn('pending_diff=', sql)
        self.assertEqual(params[-3:], ('none', None, 1))

    async def test_new_source_hold_preserves_baseline_and_pending_proposal(self):
        url = 'https://example.invalid/policy'
        old = '기존 원문'
        source = '교육비 지원.\n' + '설명\n' * 3000 + '중복 지원 불가.'
        row = dict(id=1, page_id='fake-page', title='교육비', db_key='medical',
                   notion_fields=json.dumps({'support_detail': '사용자 내용'}),
                   source_urls=json.dumps([url]), source_hashes=json.dumps({url: 'old-hash'}),
                   source_texts=json.dumps({url: old}), last_extracted_fields=json.dumps({'support_detail': '기존 기준'}))
        with patch.object(ps, '_active_configured_keys', return_value=['medical']), \
             patch.object(ps, '_get_db_schema', return_value={}), \
             patch.object(ps, '_db_query_strict', new=AsyncMock(return_value=[row])), \
             patch.object(ps, '_db_write_strict', new=AsyncMock()) as write, \
             patch.object(ps, '_fetch_source', new=AsyncMock(return_value=(source, source, False))), \
             patch.object(ai_classifier, 'get_client', return_value=object()), \
             patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock()) as call, \
             patch.dict(os.environ, {'AI_CONTENT_MAX_CHARS': '6000'}):
            actual = await ps.check_policies(['medical'], scraper=object())
        self.assertEqual(actual['changed'], 0)
        self.assertEqual(actual['review_notes'][0]['code'], 'source_too_long')
        self.assertIn('긴 원문', ps.POLICY_STATUS['message'])
        call.assert_not_awaited()
        sql, values = write.await_args.args
        self.assertNotIn('pending_diff=', sql)
        self.assertNotIn('last_extracted_fields=', sql)
        self.assertNotIn('notion_fields=', sql)
        self.assertEqual(json.loads(values[0]), {url: 'old-hash'})
        self.assertEqual(json.loads(values[1]), {url: old})

    async def test_initial_audit_hold_does_not_mark_audited_or_overwrite_data(self):
        source = '교육비 지원.'
        row = dict(id=1, page_id='fake-page', title='교육비', db_key='medical',
                   notion_fields='{}', source_texts=json.dumps({'https://example.invalid/policy': source}))
        with patch.object(ps, '_active_configured_keys', return_value=['medical']), \
             patch.object(ps, '_get_db_schema', return_value={}), \
             patch.object(ps, '_db_query_strict', new=AsyncMock(return_value=[row])), \
             patch.object(ps, '_db_write_strict', new=AsyncMock()) as write, \
             patch.object(ai_classifier, 'get_client', return_value=object()), \
             patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(
                 return_value=extraction(source, residence=['전국']))):
            actual = await ps.run_initial_audit(['medical'])
        self.assertEqual(actual['review_count'], 1)
        self.assertEqual(actual['changed'], 0)
        write.assert_not_awaited()
        self.assertIn('기존 내용 유지', ps.AUDIT_STATUS['message'])

    def test_review_metadata_is_bounded_and_new_provenance_is_distinct(self):
        status = {}
        review = {'code': 'source_too_long', 'note': '원문 확인', 'fields': []}
        for _ in range(100):
            ps._record_extraction_review(status, '제목' * 100, review)
        self.assertEqual(status['review_count'], 100)
        self.assertEqual(len(status['review_notes']), 20)
        self.assertLessEqual(len(status['review_notes'][0]['title']), 120)
        privacy_status = {'review_count': 1, 'review_notes': [{'code': 'ai_input_review_required'}]}
        self.assertIn('민감정보 전송 전 확인 필요', ps._extraction_review_summary(privacy_status))
        self.assertNotEqual(ps._EXTRACTION_PROMPT_VERSION, 'policy-extract-v2-literal-conditions')
        self.assertEqual(ps._EXTRACTION_PROMPT_VERSION, 'policy-extract-v6-payment-predicate')
        for key in ('residence', 'life_cycle', 'target_characteristics'):
            self.assertTrue(ps.requires_individual_review(ps._PROP_LABEL[key]))
