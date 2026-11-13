"""Synthetic safety cases. Passing tests do not measure live model accuracy."""
import copy
import json
import sqlite3
import unittest
from unittest.mock import AsyncMock, patch

from services.news_analysis_safety import assess
from services.news_profile import NewsProfile
from services import news_custom_analysis as custom, news_analysis_store as jobs
from services import news_profile_store as profiles
from services.news_analysis_worker import process_job
from services import ocr_service, ai_coordinator
from tests.test_ocr_evidence import candidate as ocr_candidate


def facts(**changes):
    value = dict(is_relevant=True, category='복지', target='개인', residence=[], life_cycle=[],
        target_characteristics=[], interest_topics=[], start_age_months=None, end_age_months=None,
        evidence_snippet='', residence_evidence=[], life_cycle_evidence=[],
        applicant_type='individual', applicant_evidence='개인이 신청합니다.',
        applicant_detail='개인', beneficiary_detail='개인', support_detail='',
        eligibility_detail='', contact='', deadline='')
    return value | changes


class TestCompleteGrounding(unittest.TestCase):
    profile = NewsProfile('p', 1, '시험', '전국')

    def test_missing_contact_and_deadline_markers_are_not_verified_facts(self):
        source = '개인이 신청합니다. 교통비 월 2만원 지원.'
        baseline = facts(support_detail='교통비 월 2만원 지원.',
                         fact_evidence={'support_detail': '교통비 월 2만원 지원.',
                                        'contact': '', 'deadline': ''})
        self.assertEqual(assess(self.profile, baseline, source)['target_status'], 'match')
        marked = dict(baseline, contact='미상', deadline='미상')
        result = assess(self.profile, marked, source)
        self.assertEqual(result['target_status'], 'review')
        self.assertEqual(set(result['analysis']['unverified_facts']), {'contact', 'deadline'})
        # A prompt correction must not weaken actual unsupported-fact rejection.
        invented = dict(baseline, contact='02-000-0000', deadline='2099-01-01')
        self.assertEqual(assess(self.profile, invented, source)['target_status'], 'review')

    def test_invented_age_household_money_and_deadline_are_review_and_removed(self):
        source = '개인이 신청합니다. 일반 주민에게 교육 서비스를 제공합니다.'
        original = facts(start_age_months=0, end_age_months=12, target_characteristics=['장애인'],
                         support_detail='매월 100만원을 지급합니다.', deadline='2099-01-01')
        before = copy.deepcopy(original)
        result = assess(self.profile, original, source)
        self.assertEqual(original, before)
        self.assertEqual(result['target_status'], 'review')
        for key in ('start_age_months', 'end_age_months'):
            self.assertIsNone(result['analysis'][key])
        for key in ('support_detail', 'deadline'):
            self.assertEqual(result['analysis'][key], '')
        self.assertEqual(result['analysis']['target_characteristics'], [])
        self.assertEqual(assess(self.profile, result['analysis'], source)['target_status'], 'review')

    def test_negative_applicant_quote_cannot_match(self):
        source = '모집 대상은 운영기관입니다. 개인은 신청할 수 없습니다.'
        result = assess(self.profile, facts(applicant_evidence='개인은 신청할 수 없습니다.'), source)
        self.assertEqual(result['target_status'], 'review')
        self.assertEqual(result['target_checks']['applicant'], 'review')

    def test_organization_exclusion_needs_application_not_incidental_address(self):
        for source, expected in [('개인이 신청합니다. 방문 장소는 운영기관입니다.', 'review'),
                                 ('프로그램 운영기관을 모집합니다.', 'excluded')]:
            with self.subTest(source=source):
                result = assess(self.profile, facts(applicant_type='organization',
                    applicant_evidence=source), source)
                self.assertEqual(result['target_status'], expected)

    def test_model_non_relevance_alone_cannot_exclude(self):
        source = '개인이 신청합니다. 교육 서비스를 제공합니다.'
        self.assertEqual(assess(self.profile, facts(is_relevant=False), source)['target_status'], 'review')
        source = '직원 채용 공고입니다.'
        result = assess(self.profile, facts(is_relevant=False, evidence_snippet=source), source)
        self.assertEqual(result['target_status'], 'excluded')

    def test_negated_non_news_phrase_cannot_exclude(self):
        source = '직원 채용 공고가 아니라 주민 대상 교육입니다. 개인이 신청합니다.'
        for quote in ('직원 채용 공고', '직원 채용 공고가 아니라 주민 대상 교육입니다.'):
            with self.subTest(quote=quote):
                result = assess(self.profile, facts(is_relevant=False, evidence_snippet=quote), source)
                self.assertEqual(result['target_status'], 'review')

    def test_unsupported_topic_cannot_exclude(self):
        profile = NewsProfile('p', 1, '주거', '전국', interest_topics=('주거',))
        source = '개인이 신청합니다. 주거 비용을 지원합니다.'
        result = assess(profile, facts(interest_topics=['교육']), source)
        self.assertEqual(result['target_status'], 'review')
        self.assertEqual(result['analysis']['interest_topics'], [])

    def test_explicit_age_bounds_are_inclusive_and_grounded(self):
        cases = [('생후 0~12개월 대상', 0, 12), ('만 3~5세 대상', 36, 71),
                 ('만 65세 이상 대상', 780, None), ('만 6세 미만 대상', None, 71),
                 ('생후 6개월 초과 대상', 7, None), ('만 5세 이하 대상', None, 71)]
        for phrase, start, end in cases:
            with self.subTest(phrase=phrase):
                source = phrase + '. 개인이 신청합니다.'
                result = assess(self.profile, facts(start_age_months=start, end_age_months=end), source)
                self.assertEqual(result['target_status'], 'match')
                wrong = assess(self.profile, facts(start_age_months=1 if start is None else start + 1,
                                                  end_age_months=end), source)
                self.assertEqual(wrong['target_status'], 'review')

    def test_normal_households_and_literal_benefits_remain_matches(self):
        tags = ('장애인', '한부모', '다자녀', '다문화', '탈북민', '보훈', '여성', '1인 가구')
        for tag in tags:
            with self.subTest(tag=tag):
                source = f'{tag} 가구 대상. 교육비 월 10만원 지원. 개인이 신청합니다. 신청 마감 2026-10-31.'
                result = assess(self.profile, facts(target_characteristics=[tag], interest_topics=['교육'],
                    support_detail='교육비 월 100000원 지원', deadline='2026-10-31',
                    fact_evidence={'support_detail':'교육비 월 10만원 지원'}), source)
                self.assertEqual(result['target_status'], 'match')
                self.assertEqual(result['analysis']['deadline'], '2026-10-31')

    def test_excluded_household_and_incidental_numbers_do_not_ground_facts(self):
        source = '장애인 제외 대상입니다. 지원 기간은 6개월입니다. 개인이 신청합니다.'
        result = assess(self.profile, facts(target_characteristics=['장애인'], end_age_months=6), source)
        self.assertEqual(result['target_status'], 'review')
        self.assertEqual(result['analysis']['target_characteristics'], [])
        self.assertIsNone(result['analysis']['end_age_months'])

    def test_guardian_age_cannot_exclude_child_recipient(self):
        source = '아동 만 3~5세 대상. 보호자는 만 19세 이상이어야 합니다. 개인이 신청합니다.'
        profile = NewsProfile('p', 1, '아동', '전국', min_age_months=36, max_age_months=71)
        wrong = assess(profile, facts(start_age_months=228), source)
        self.assertEqual(wrong['target_status'], 'review')
        self.assertIsNone(wrong['analysis']['start_age_months'])
        correct = assess(profile, facts(start_age_months=36, end_age_months=71,
            eligibility_detail='보호자는 만 19세 이상이어야 합니다.'), source)
        self.assertEqual(correct['target_status'], 'match')

    def test_missing_restriction_is_review_but_complete_literal_summary_matches(self):
        for restriction in ('기존 수급자는 신청할 수 없습니다.', '중복 지원 불가.',
                            '방문 신청 필요.', '신청 시 소득증명서를 제출해야 합니다.'):
            with self.subTest(restriction=restriction):
                source = '개인이 신청합니다. 월 10만원을 지급합니다. ' + restriction
                partial = facts(support_detail='월 10만원을 지급합니다.')
                held = assess(self.profile, partial, source)
                self.assertEqual(held['target_status'], 'review')
                self.assertTrue(held['analysis']['unverified_facts']['omitted_conditions'])
                complete = assess(self.profile, partial | {'eligibility_detail': restriction}, source)
                self.assertEqual(complete['target_status'], 'match')

    def test_official_household_aliases_match_without_sibling_inference(self):
        from services.news_profile_taxonomy import _HOUSEHOLD_ALIASES
        for alias, canonical in _HOUSEHOLD_ALIASES.items():
            with self.subTest(alias=alias):
                source = f'{alias} 가구를 지원합니다. 개인이 신청합니다.'
                result = assess(self.profile, facts(target_characteristics=[canonical]), source)
                self.assertEqual(result['target_status'], 'match')
        result = assess(self.profile, facts(target_characteristics=['다문화']),
                        '탈북민 가구를 지원합니다. 개인이 신청합니다.')
        self.assertEqual(result['target_status'], 'review')

    def test_event_date_does_not_prove_application_deadline(self):
        source = '개인이 신청합니다. 신청은 2026-10-01부터이며 행사일은 2026-10-31입니다.'
        result = assess(self.profile, facts(deadline='2026-10-31'), source)
        self.assertEqual(result['target_status'], 'review')
        self.assertEqual(result['analysis']['deadline'], '')


class TestStoredSafety(unittest.IsolatedAsyncioTestCase):
    async def test_old_false_match_is_review_in_worker_cache_and_list_without_mutating_user_data(self):
        with sqlite3.connect(':memory:') as conn:
            profiles.ensure_schema(conn)
            jobs.ensure_schema(conn)
            profile = NewsProfile('p',1,'장애인','전국',characteristics=('장애인',),site_ids=('1',))
            profiles.create_profile(conn, profile)
            source = '개인이 신청합니다. 일반 주민 교육입니다.'
            run = profiles.prepare_run(conn,'p',expected_revision=1,
                sites=[dict(id=1,name='시험',url='https://example.invalid')])
            job_id = jobs.enqueue(conn,run,payload=dict(title='시험',link='https://example.invalid/post',
                site_name='시험',content=source),model='mock',recipe=custom.SCOPED_RECIPE)
            token = jobs.claim(conn,job_id,now=0)
            raw = dict(analysis=facts(target_characteristics=['장애인']),accepted=True,
                       target_status='match',review_required=False)
            jobs.complete(conn,job_id,token,raw)
            before = conn.execute('SELECT result_json FROM news_analysis_jobs').fetchone()[0]
            view = json.loads(jobs.get_job(conn,job_id)['result_json'])
            self.assertEqual(view['target_status'],'review')
            self.assertEqual(jobs.list_profile_results(conn,'p')['items'][0]['target_status'],'review')
            self.assertEqual(conn.execute('SELECT result_json FROM news_analysis_jobs').fetchone()[0],before)
            async def call_db(fn,*args,**kwargs):
                return fn(conn,*args,**kwargs)
            second_run = profiles.prepare_run(conn,'p',expected_revision=1,
                sites=[dict(id=1,name='시험',url='https://example.invalid')])
            payload = json.loads(jobs.get_job(conn,job_id)['input_json'])
            second = jobs.enqueue(conn,second_run,payload=payload,model='mock',recipe=custom.SCOPED_RECIPE)
            with patch.object(custom,'classify',new=AsyncMock()) as model:
                self.assertEqual(await process_job(second,object(),call_db=call_db),'not_claimed')
                model.assert_not_awaited()
            self.assertEqual(json.loads(jobs.get_job(conn,second)['result_json'])['target_status'],'review')


class TestOcrShape(unittest.IsolatedAsyncioTestCase):
    def test_local_validator_rejects_malformed_shapes_and_ranges(self):
        cases = [[], [{}], None, {'is_relevant':'false'}, {'is_relevant':1},
                 {'is_relevant':True,'residence':'전국'},
                 {'is_relevant':True,'start_age_months':True},
                 {'is_relevant':True,'start_age_months':12,'end_age_months':0},
                 {'is_relevant':True,'deadline':20260230}]
        for value in cases:
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                ocr_service.validate_ocr_result(value)
        # Missing transcription is now a review hold, even for an irrelevant
        # model classification. It cannot silently become a verified exclusion.
        irrelevant = ocr_service.validate_ocr_result({'is_relevant':False})
        self.assertFalse(irrelevant['is_relevant'])
        self.assertEqual(irrelevant['error_code'], 'OCR_SOURCE_REVIEW_REQUIRED')
        # Invalid calendar text cannot rescue a missing transcription. With a
        # valid envelope it is held per field (test_ocr_date_review), not parsed
        # into a corrected date or used to discard the complete source.
        missing_source = ocr_service.validate_ocr_result(
            {'is_relevant': True, 'deadline': '2026-02-30'})
        self.assertEqual(missing_source['error_code'], 'OCR_SOURCE_REVIEW_REQUIRED')
        valid = dict(is_relevant=True,title='복지 안내',support_detail='보육료 월 10만원 지원',residence=['전국'],start_age_months=0,end_age_months=12)
        # Old inferred region/age fields have no transcription or quotations.
        held = ocr_service.validate_ocr_result(valid)
        self.assertEqual(held['error_code'], 'OCR_SOURCE_REVIEW_REQUIRED')
        self.assertEqual(held['_ocr_review']['unverified_fields']['residence'], ['전국'])
        actual = ocr_service.validate_ocr_result(ocr_candidate('보육료 월 10만원 지원'))
        self.assertTrue(actual['is_relevant'])
        self.assertEqual(actual['support_detail'], '보육료 월 10만원 지원')
        self.assertTrue(actual['needs_source_review'])

    def test_ocr_schema_requires_all_fields_and_rejects_date_only_success(self):
        schema = ocr_service.response_schema()
        self.assertEqual(set(schema['required']), set(schema['properties']))
        for support in (None, '', '미상', '   ', '...'):
            with self.subTest(support=support):
                raw = dict(is_relevant=True, start_age_months=780, deadline='2026-10-31', support_detail=support)
                if support is None:  # Existing strict text-type rejection stays.
                    with self.assertRaises(ValueError):
                        ocr_service.validate_ocr_result(raw)
                else:
                    actual = ocr_service.validate_ocr_result(raw)
                    self.assertFalse(actual['is_relevant'])
                    self.assertEqual(actual['error_code'], 'OCR_SOURCE_REVIEW_REQUIRED')

    async def test_malformed_model_json_returns_safe_error_not_truthy_success(self):
        from types import SimpleNamespace
        from services import ai_verified_input
        ai_verified_input._reset_for_tests()
        self.addCleanup(ai_verified_input._reset_for_tests)
        for raw in ('[]','[{"is_relevant":true}]','{"is_relevant":"false"}'):
            client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(
                count_tokens=AsyncMock(return_value=SimpleNamespace(total_tokens=100)),
                generate_content=AsyncMock(return_value=SimpleNamespace(text=raw)))))
            with patch.object(ai_coordinator,'acquire_model_slot',new=AsyncMock()), \
                 patch.object(ai_coordinator,'input_token_capacity',return_value=6000):
                result = await ocr_service._analyze_document_bytes(client,b'%PDF-1.7\nmock',mime_type='application/pdf',privacy_reviewed=True)
            client.aio.models.generate_content.assert_awaited_once()
            self.assertIs(result['is_relevant'],False)
            self.assertEqual(result['error_code'],'OCR_SOURCE_REVIEW_REQUIRED')
            self.assertTrue(result['needs_source_review'])
