"""Offline transmission-copy tests; run with an isolated app-data environment.

All strings and SDK replies are synthetic. Provider dispatch and admission are
mocked; source-selection checks run against the unmodified local original.
"""
import copy
from dataclasses import replace
import json
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from google.genai import types

import ai_classifier
from services import ai_coordinator, news_contact_input as contacts
from services import news_custom_analysis as custom, news_source_selection as selection
from services import news_analysis_store as jobs, news_profile_store as profiles, page_coverage
from services.ai_input_safety import AiInputReviewRequired, ensure_ai_text_safe
from services.news_analysis_safety import assess
from services.news_analysis_worker import process_job
from services.news_profile import NewsProfile
from tests.test_news_source_selection import response as source_response


PHONE = '010-1234-5678'
EMAIL = 'named.staff@example.invalid'
BODY = '개인 대상 교육 안내\n개인이 신청합니다.\n중복 신청 불가.\n접수 마감: 2026-10-01 18:00까지'
SOURCE = BODY + f'\n문의: 복지과 담당자 {PHONE}, {EMAIL}\n지원 금액: 10만원'
PROFILE = NewsProfile('contact-test', 1, '연락처 시험', '전국', general_mode='custom_v1', site_ids=('1',))


def payload(content=SOURCE):
    return dict(title='합성 지원 공고', link='https://example.invalid/news',
                site_name='합성기관', content=content, source_page=page_coverage.make())


def reply():
    output = source_response()
    output.update(applicant_type='individual', applicant_evidence='개인이 신청합니다.',
                  applicant_detail='개인', beneficiary_detail='개인', contact='010-9999-9999')
    output['fact_evidence'].update(is_relevant='개인 대상 교육 안내', contact='010-9999-9999')
    return output


class ContactInput(unittest.TestCase):
    def test_only_explicit_contact_values_are_removed_and_original_is_unchanged(self):
        source = payload()
        frozen = copy.deepcopy(source)
        transmitted, metadata = contacts.ensure_news_payload_safe(source, '전국 주민 지원')
        self.assertEqual(source, frozen)
        self.assertEqual(transmitted['content'], SOURCE.replace(PHONE, contacts.MARKER).replace(EMAIL, contacts.MARKER))
        self.assertEqual(metadata['removed_counts'], {'mobile_number': 1, 'email_address': 1})
        self.assertNotIn(PHONE, repr(metadata))
        self.assertNotIn(EMAIL, repr(metadata))
        self.assertIn('중복 신청 불가', transmitted['content'])
        self.assertIn('2026-10-01 18:00', transmitted['content'])
        self.assertIn('10만원', transmitted['content'])
        ensure_ai_text_safe(transmitted['content'])
        with self.assertRaises(AiInputReviewRequired):
            ensure_ai_text_safe(SOURCE)  # The global/policy rule is unchanged.

    def test_contact_labels_and_newlines_remain_exact(self):
        for label in ('문의처:', '담당자:', '연락처:', '접수처:', '접수 이메일:',
                      '문의/접수처:', '※ 신청 문의:', '1. 담당부서:',
                      '문의전화 ', '문의 담당자:', '문의 및 접수:', '접수 ', '담당 '):
            with self.subTest(label=label):
                original = f'안내\r\n{label} 지원센터 {EMAIL}\r\n중복 수급 불가\r\n'
                text, metadata = contacts.prepare_content(original)
                self.assertEqual(text, original.replace(EMAIL, contacts.MARKER))
                self.assertEqual(metadata['status'], 'minimized')

    def test_per_person_support_terms_are_not_mistaken_for_a_contact_roster(self):
        original = '개인별 1회 신청. 대상자별 지원금이 다릅니다.\n문의전화 ' + PHONE
        self.assertEqual(contacts.prepare_content(original)[0], original.replace(PHONE, contacts.MARKER))

    def test_existing_allowed_business_contacts_are_untouched(self):
        text = '문의: 02-123-4567 / 031-1234-5678 / 070-1234-5678 / 1588-0000 / welfare@district.go.kr'
        self.assertEqual(contacts.prepare_content(text)[0], text)
        self.assertEqual(contacts.prepare_content(text)[1]['status'], 'unchanged')

    def test_ambiguous_contacts_rosters_and_embedded_urls_stay_held(self):
        for source in (
            f'일반 본문 {PHONE}', f'개인 이메일 {EMAIL}',
            f'문의\n합성이름 {PHONE}', f'신청자 명단\n문의: {PHONE}',
            f'성명 | 연락처\n문의: {PHONE}',
            f'성명: 합성이름\n연락처: {PHONE}',
            ('담당자: 합성이름 ' + PHONE + '\n') * 4,
            f'문의: https://example.invalid/person/{EMAIL}',
            f'문의: mailto:{EMAIL}',
            '문의: ０１０－１２３４－５６７８',
            '문의: a@example.invalid b@example.invalid c@example.invalid d@example.invalid',
        ):
            with self.subTest(source=source), self.assertRaises(AiInputReviewRequired):
                contacts.prepare_content(source)

    def test_identity_credentials_and_sensitive_tail_cannot_be_hidden(self):
        for source in (
            SOURCE + '\n주민등록번호: 900101-1234567',
            '문의: 900101-1234567@example.invalid',
            '문의: ghp_' + 'a'*36 + '@example.invalid',
            SOURCE + '\n문의: https://example.invalid/?key=synthetic-secret-12345',
            '안내\n' * 5000 + '\n개인 연락번호: ' + PHONE,
        ):
            with self.subTest(source=source[-60:]), self.assertRaises(AiInputReviewRequired):
                contacts.prepare_content(source)


class ContactSdkIntegration(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.provider = AsyncMock()
        self.client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=self.provider)))
        self.slot = AsyncMock()
        patcher = patch.object(ai_coordinator, 'acquire_model_slot', self.slot)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.set_reply(reply())

    def set_reply(self, value):
        self.provider.return_value = types.GenerateContentResponse(candidates=[types.Candidate(
            content=types.Content(parts=[types.Part(text=json.dumps(value, ensure_ascii=False))]),
            finish_reason='STOP')])

    async def classify(self, source=None, profile=PROFILE):
        return await custom.classify(self.client, profile, source or payload(), 'synthetic-only',
            source_unit_rules=True, whole_document_rules=True, request_attempts=1)

    async def test_actual_sdk_prompt_excludes_contacts_but_local_full_source_stays_grounded(self):
        source = payload()
        frozen = copy.deepcopy(source)
        result = await self.classify(source)
        self.provider.assert_awaited_once()
        self.slot.assert_awaited_once()
        prompt = self.provider.call_args.kwargs['contents']
        self.assertNotIn(PHONE, prompt)
        self.assertNotIn(EMAIL, prompt)
        transmitted = json.loads(prompt.split('\nSOURCE: ')[1])
        self.assertIn(BODY, transmitted['content'])
        self.assertIn('지원 금액: 10만원', transmitted['content'])
        self.assertEqual(source, frozen)
        self.assertEqual(result['support_detail'], SOURCE)
        self.assertEqual(selection.selection_issue(result, SOURCE, required=True, extended=True), '')
        self.assertEqual(result['contact'], '')
        self.assertEqual(result['fact_evidence']['contact'], '')
        output = assess(PROFILE, result, SOURCE, require_source_selection=True,
                        whole_document_rules=True, source_payload=source)
        self.assertTrue(output['accepted'], output)
        self.assertEqual(output['analysis']['support_detail'], SOURCE)
        self.assertEqual(output['analysis']['source_input']['contact_minimization']['status'], 'minimized')
        self.assertIn(contacts.NOTE, output['analysis']['source_input']['note'])

    async def test_legacy_news_sdk_receives_copy_and_never_uses_invented_contact(self):
        self.set_reply({'is_relevant': True, 'category': '교육', 'target': '개인', 'contact': '010-9999-9999'})
        result = await ai_classifier.classify_post_async(self.client, **payload(),
            custom_prompt='', strict=True, request_attempts=1)
        prompt = self.provider.call_args.kwargs['contents']
        self.assertNotIn(PHONE, prompt)
        self.assertNotIn(EMAIL, prompt)
        # The budget-fitting legacy prompt JSON-escapes source line breaks.
        transmitted = json.loads(prompt.rsplit('\nSOURCE: ', 1)[1]) if '\nSOURCE: ' in prompt else prompt
        self.assertIn(BODY, transmitted)
        self.assertEqual(result['contact'], '')
        self.assertEqual(result['source_input']['contact_minimization']['status'], 'minimized')

    async def test_title_url_site_preferences_and_criteria_are_not_minimized(self):
        cases = [(payload() | {key: value}, PROFILE) for key, value in (
            ('title', '문의: ' + PHONE), ('site_name', '담당: ' + EMAIL),
            ('link', 'https://example.invalid/?key=synthetic-secret-12345'))]
        cases.extend([(payload(), replace(PROFILE, ai_prompt='문의: ' + PHONE)),
                      (payload(), replace(PROFILE, interest_topics=('문의: ' + EMAIL,)))])
        for source, profile in cases:
            with self.subTest(source=source['title'], prompt=profile.ai_prompt):
                with self.assertRaises(AiInputReviewRequired):
                    await self.classify(source, profile)
        self.provider.assert_not_awaited()
        self.slot.assert_not_awaited()

    async def test_noncontact_tail_is_checked_before_excerpt_selection_and_sdk(self):
        for source in (payload('안내\n'*5000 + PHONE), payload(SOURCE + '\n900101-1234567')):
            with self.assertRaises(AiInputReviewRequired):
                await self.classify(source)
        self.provider.assert_not_awaited()
        self.slot.assert_not_awaited()

    async def test_shared_policy_dispatch_keeps_its_strict_raw_input_check(self):
        with self.assertRaises(AiInputReviewRequired):
            await ai_classifier._call_gemini_json(self.client, SOURCE, '합성 정책', request_attempts=1)
        self.provider.assert_not_awaited()
        self.slot.assert_not_awaited()

    async def test_worker_keeps_raw_database_input_and_separates_new_recipe(self):
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        profiles.ensure_schema(conn)
        jobs.ensure_schema(conn)
        profiles.create_profile(conn, PROFILE)
        run = profiles.prepare_run(conn, PROFILE.profile_id, expected_revision=1,
            sites=[dict(id=1, name='시험', url='https://example.invalid')])
        old = jobs.enqueue(conn, run, payload=payload(), model='synthetic-only', recipe=custom.BOUNDED_SOURCE_RECIPE)
        # Pin the v15 -> v16 contact-minimization identity this fixture covers.
        new = jobs.enqueue(conn, run, payload=payload(), model='synthetic-only', recipe=custom.CONTACT_RECIPE)
        self.assertNotEqual(jobs.get_job(conn, old)['cache_key'], jobs.get_job(conn, new)['cache_key'])
        async def call_db(fn, *args, **kwargs):
            return fn(conn, *args, **kwargs)
        self.assertEqual(await process_job(new, self.client, call_db=call_db), 'complete')
        saved = jobs.get_job(conn, new)
        self.assertEqual(json.loads(saved['input_json'])['content'], SOURCE)
        self.assertEqual(json.loads(saved['result_json'])['analysis']['support_detail'], SOURCE)
        visible = next(item for item in jobs.list_profile_results(conn, PROFILE.profile_id)['items']
                       if item['job_id'] == new)
        self.assertIn(contacts.NOTE, visible['input_note'])
