"""Offline policy transport boundaries; no network or operating profile access."""
from copy import deepcopy
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from services import notion_service as ns


def contents(runs):
    return ''.join(run['text']['content'] for run in runs)


def alternating_sections():
    return '\n'.join(f'# 항목 {index}\n본문 조건 {index}' for index in range(60))


class PolicyPayloadText(unittest.TestCase):
    def test_long_korean_and_astral_lines_keep_tail_with_utf16_chunks(self):
        source = ('가😀' * 14980) + '\n마지막 제외 조건은 그대로 유지합니다.'
        for field in ('support_detail', 'eligibility_detail', 'age_detail', 'target_income'):
            with self.subTest(field=field):
                runs = ns._build_prop_value('rich_text', source, field)['rich_text']
                self.assertEqual(contents(runs), source)
                self.assertLessEqual(len(runs), 100)
                self.assertTrue(all(ns._utf16_units(run['text']['content']) <= 1900 for run in runs))
                ns.validate_event_payload({'properties': {'필드': {'rich_text': runs}}})

    def test_hundreds_of_plain_lines_pack_without_losing_headers_or_tail(self):
        source = '# 지원 안내\n' + '\n'.join(f'본문 {index}: 조건 😀' for index in range(300))
        source += '\n\n📌 제외 조건\n중복 참여자는 신청할 수 없습니다.'
        runs = ns._richtext_from_sections(source)
        self.assertEqual(contents(runs), source.removeprefix('# '))
        self.assertLess(len(runs), 20)
        self.assertEqual(contents([run for run in runs if run.get('annotations', {}).get('bold')]),
                         '지원 안내\n📌 제외 조건\n')
        self.assertEqual(runs[-1]['text']['content'], '중복 참여자는 신청할 수 없습니다.')
        self.assertNotIn('annotations', runs[-1])
        ns.validate_event_payload({'properties': {'필드': {'rich_text': runs}}})

    def test_alternating_formatting_is_preserved_not_flattened_to_fit(self):
        source = alternating_sections()
        runs = ns._richtext_from_sections(source)
        self.assertEqual(len(runs), 120)
        self.assertEqual(contents(runs), source.replace('# ', ''))
        self.assertTrue(all(run.get('annotations', {}).get('bold') is True for run in runs[::2]))
        self.assertTrue(all(not run.get('annotations') for run in runs[1::2]))
        with self.assertRaises(ns.EventPayloadError) as raised:
            ns.validate_event_payload({'properties': {'필드': {'rich_text': runs}}})
        self.assertEqual(raised.exception.code, 'NOTION_PAYLOAD_TOO_LARGE')


class PolicyPayloadWriteSafety(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        credentials = patch.object(ns, 'get_credentials', return_value=('offline-token', ''))
        credentials.start()
        self.addCleanup(credentials.stop)
        mappings = patch.object(ns, '_load_policy_prop_override', return_value={})
        mappings.start()
        self.addCleanup(mappings.stop)

    def session(self):
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        response = MagicMock(status=200)
        response.json = AsyncMock(return_value={'id': 'offline-page'})
        response.__aenter__ = AsyncMock(return_value=response)
        response.__aexit__ = AsyncMock(return_value=False)
        session.patch.return_value = response
        return session

    def assert_local_rejection(self, result, code='NOTION_PAYLOAD_TOO_LARGE'):
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], code)
        self.assertEqual(result['code'], code)
        self.assertIs(result['request_sent'], False)
        self.assertIs(result['uncertain'], False)
        self.assertIn('전송하지 않았습니다', result['message'])

    async def test_create_preflight_rejects_array_or_whole_payload_overflow_without_requests(self):
        fields_list = [
            {'title': '합성 정책', 'support_detail': alternating_sections()},
            {'title': '합성 정책', **{field: '가' * 30000 for field in (
                'support_detail', 'eligibility_detail', 'age_detail', 'target_income')}},
        ]
        for fields in fields_list:
            original = deepcopy(fields)
            with self.subTest(fields=len(fields)), patch.object(ns.aiohttp, 'ClientSession') as session, \
                 patch.object(ns, 'resolve_target', new_callable=AsyncMock) as resolve, \
                 patch.object(ns, '_post_page', new_callable=AsyncMock) as post:
                self.assert_local_rejection(await ns.create_policy_page(fields, 'offline-source'))
            session.assert_not_called()
            resolve.assert_not_awaited()
            post.assert_not_awaited()
            self.assertEqual(fields, original)

    async def test_update_preflight_rejects_oversize_or_invalid_utf16_without_patch(self):
        values = [
            ({'상세 지원 내용': {'rich_text': ns._richtext_from_sections(alternating_sections())}}, 'NOTION_PAYLOAD_TOO_LARGE'),
            ({'문의처': {'rich_text': [{'text': {'content': '가' * 2001}}]}}, 'NOTION_PAYLOAD_TOO_LARGE'),
            ({'문의처': {'rich_text': [{'text': {'content': '\ud800'}}]}}, 'NOTION_PAYLOAD_INVALID'),
        ]
        for properties, code in values:
            original = deepcopy(properties)
            with self.subTest(code=code), patch.object(ns.aiohttp, 'ClientSession') as session:
                self.assert_local_rejection(await ns.update_page_properties('offline-page', properties), code)
            session.assert_not_called()
            self.assertEqual(properties, original)

    async def test_create_and_update_send_one_valid_complete_payload_with_headers(self):
        source = '# 지원 안내\n' + '본문😀\n' * 300 + '마지막 제외 조건'
        fields = {'title': '합성 정책', 'support_detail': source}
        target = {'ok': True, 'kind': 'data_source', 'id': 'canonical-source', 'version': ns.DS_VERSION}
        session = self.session()
        with patch.object(ns.aiohttp, 'ClientSession', return_value=session), \
             patch.object(ns, 'resolve_target', new=AsyncMock(return_value=target)), \
             patch.object(ns, '_post_page', new=AsyncMock(return_value=(200, {'id': 'offline-page'}))) as post:
            self.assertTrue((await ns.create_policy_page(fields, 'original-source'))['ok'])
            post.assert_awaited_once()
            payload = post.await_args.args[2]
            self.assertEqual(payload['parent'], {'type': 'data_source_id', 'data_source_id': 'canonical-source'})
            self.assertEqual(contents(payload['properties']['상세 지원 내용']['rich_text']), source.removeprefix('# '))
            ns.validate_event_payload(payload)
            result = await ns.update_page_properties('offline-page', payload['properties'])
            self.assertTrue(result['ok'])
            session.patch.assert_called_once()
            self.assertEqual(session.patch.call_args.kwargs['json'], {'properties': payload['properties']})

    async def test_create_rechecks_actual_canonical_parent_before_post(self):
        target = {'ok': True, 'kind': 'data_source', 'id': 'x' * 500001, 'version': ns.DS_VERSION}
        with patch.object(ns.aiohttp, 'ClientSession', return_value=self.session()), \
             patch.object(ns, 'resolve_target', new=AsyncMock(return_value=target)), \
             patch.object(ns, '_post_page', new_callable=AsyncMock) as post:
            self.assert_local_rejection(await ns.create_policy_page({'title': '합성 정책'}, 'offline-source'))
            post.assert_not_awaited()

    async def test_apply_local_rejection_marks_receipt_failed_not_uncertain_or_applied(self):
        from services import policy_sync as policy, policy_undo as undo, policy_apply_coverage
        source = alternating_sections()
        target = dict(page_id='offline-page', properties=ns.build_policy_properties({'support_detail': source}),
                      fkey='support_detail', old_display='기존 조건', py_value=source)
        calls = []
        async def call_db(function, *args):
            calls.append((function, args))
            if function is policy._apply_field_sync:
                return target, None, {'fields': []}
            if function is undo.prepare:
                return 'offline-receipt'
            if function is undo.mark:
                return None
            self.fail('Unexpected database action')
        with patch.object(policy.database, 'run_db', new=call_db), \
             patch.object(ns, 'get_page', new=AsyncMock(return_value={'id': 'offline-page'})), \
             patch.object(ns, 'extract_page_fields', return_value={'support_detail': '기존 조건'}), \
             patch.object(policy_apply_coverage, 'standalone_apply_hold', return_value=None), \
             patch.object(ns.aiohttp, 'ClientSession') as session:
            self.assert_local_rejection(await policy._apply_field_to_notion_locked(1, '상세 지원 내용', False, False))
        session.assert_not_called()
        self.assertEqual(calls[-1], (undo.mark, ('offline-receipt', 'failed')))
        self.assertEqual(len(calls), 3)
