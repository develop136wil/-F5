"""Offline Notion export limits, lossless text and one-write safety."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from services import notion_service as ns


def rich_text(block):
    return ''.join(run['text']['content'] for run in block[block['type']].get('rich_text', []))


def body_blocks(payload):
    return [block for callout in payload['children'] for block in callout['callout']['children']]


def item(**overrides):
    return {'title': '합성 소식', 'site_name': '합성 기관',
            'link': 'https://example.invalid/news', 'support_detail': '본문', **overrides}


class TestEventTextPreservation(unittest.TestCase):
    def test_long_cjk_and_astral_lines_preserve_the_final_condition(self):
        text = '가' * 1900 + '😀' * 1901 + ' 단, 신청자는 서류를 제출해야 합니다.'
        runs = ns._rt(text)
        self.assertEqual(''.join(run['text']['content'] for run in runs), text)
        self.assertTrue(all(len(run['text']['content'].encode('utf-16-le')) // 2 <= 1900 for run in runs))
        payload = ns.build_payload(item(support_detail=text), '', 'fake-db')
        self.assertEqual(rich_text(body_blocks(payload)[-1]), text)

    def test_a_line_longer_than_one_rich_text_array_is_split_into_blocks(self):
        text = 'a' * 190_001 + ' TAIL CONDITION'
        blocks = ns._text_blocks(text)
        self.assertEqual(len(blocks), 2)
        self.assertEqual(''.join(rich_text(block) for block in blocks), text)
        self.assertTrue(all(len(block[block['type']]['rich_text']) <= 100 for block in blocks))
        ns.build_payload(item(support_detail=text), '', 'fake-db')

    def test_130_lines_and_section_context_are_preserved_in_one_payload(self):
        lines = [f'condition {index:03}' for index in range(130)]
        payload = ns.build_payload(item(support_detail='\n'.join(lines),
            eligibility_detail='동일 조건\n신청 서류 필수', contact='동일 조건\n문의는 담당 기관'), '', 'fake-db')
        self.assertGreater(len(payload['children']), 1)
        self.assertTrue(all(len(block['callout']['children']) <= 100 for block in payload['children']))
        texts = [rich_text(block) for block in body_blocks(payload)]
        self.assertEqual([text for text in texts if text.startswith('condition ')], lines)
        self.assertEqual(texts.count('동일 조건'), 2)
        self.assertIn('참여 조건', texts)
        self.assertIn('신청 서류 필수', texts)
        self.assertIn('문의처', texts)
        self.assertIn('문의는 담당 기관', texts)

    def test_blank_lines_and_indentation_are_not_silently_removed(self):
        blocks = ns._text_blocks('  들여쓰기\n\n후속 조건')
        self.assertEqual([rich_text(block) for block in blocks], ['  들여쓰기', '', '후속 조건'])

    def test_markdown_blocks_preserve_content_without_freeform_ai_metadata(self):
        payload = ns.build_payload(item(support_detail='## 안내\n- 단, 예외 있음\n* 원문 확인',
            ai_data={'eligibility_detail': 'DO NOT EXPORT THIS METADATA'}), '', 'fake-db')
        blocks = body_blocks(payload)
        self.assertEqual([block['type'] for block in blocks[-3:]],
                         ['heading_3', 'bulleted_list_item', 'bulleted_list_item'])
        self.assertEqual([rich_text(block) for block in blocks[-3:]], ['안내', '단, 예외 있음', '원문 확인'])
        self.assertNotIn('DO NOT EXPORT', json.dumps(payload))

    def test_poster_only_existing_rule_is_unchanged(self):
        payload = ns.build_payload(item(support_detail='NO BODY', eligibility_detail='NO ELIGIBILITY',
            contact='NO CONTACT'), 'https://example.invalid/poster.png', 'fake-db')
        self.assertEqual(len(payload['children']), 1)
        self.assertEqual(body_blocks(payload)[-1]['type'], 'image')
        content = '\n'.join(rich_text(block) for block in body_blocks(payload))
        self.assertNotIn('NO BODY', content)
        self.assertNotIn('NO ELIGIBILITY', content)
        self.assertNotIn('NO CONTACT', content)


class TestEventPayloadLimits(unittest.TestCase):
    def assert_large(self, payload):
        with self.assertRaises(ns.EventPayloadError) as error:
            ns.validate_event_payload(payload)
        self.assertEqual(error.exception.code, 'NOTION_PAYLOAD_TOO_LARGE')

    def test_each_array_and_text_object_is_bounded(self):
        for payload in ({'children': [{}] * 101}, {'rich_text': [{}] * 101},
                        {'title': [{}] * 101}, {'text': {'content': '😀' * 1001}}):
            with self.subTest(payload_kind=next(iter(payload))):
                self.assert_large(payload)

    def test_total_block_limit_counts_nested_blocks(self):
        paragraph = {'type': 'paragraph', 'paragraph': {'rich_text': ns._rt('a')}}
        self.assert_large({'children': [{'type': 'callout', 'callout': {
            'rich_text': ns._rt(''), 'children': [paragraph] * 90}} for _ in range(11)]})

    def test_500kb_uses_actual_default_json_bytes_not_unicode_length(self):
        payload = {'text': {'content': 'abc'}}
        exact = len(json.dumps(payload).encode('utf-8'))
        with patch.object(ns, '_EVENT_PAYLOAD_BYTE_LIMIT', exact):
            ns.validate_event_payload(payload)
        with patch.object(ns, '_EVENT_PAYLOAD_BYTE_LIMIT', exact - 1):
            self.assert_large(payload)
        with self.assertRaises(ns.EventPayloadError) as error:
            ns.build_payload(item(support_detail='가' * 100_000), '', 'fake-db')
        self.assertEqual(error.exception.code, 'NOTION_PAYLOAD_TOO_LARGE')

    def test_more_than_the_whole_block_budget_fails_instead_of_truncating(self):
        with self.assertRaises(ns.EventPayloadError):
            ns.build_payload(item(support_detail='condition\n' * 1001), '', 'fake-db')

    def test_invalid_unicode_is_a_local_error(self):
        with self.assertRaises(ns.EventPayloadError) as error:
            ns._rt('broken\ud800')
        self.assertEqual(error.exception.code, 'NOTION_PAYLOAD_INVALID')

    def test_urls_are_http_or_https_strings_within_2000_units(self):
        valid = 'https://example.invalid/'
        ns.validate_event_payload({'url': valid + 'a' * (2000 - len(valid))})
        self.assert_large({'url': valid + 'a' * (2001 - len(valid))})
        for value in (None, [], 5, '', 'file:///private', 'https://',
                      'http://example.invalid:99999/', 'https://example.invalid/a\nb',
                      'https://user:password@example.invalid/', 'https://user@example.invalid/'):
            with self.subTest(value=value), self.assertRaises(ns.EventPayloadError) as error:
                ns.validate_event_payload({'url': value})
            self.assertEqual(error.exception.code, 'NOTION_PAYLOAD_INVALID')

    def test_invalid_body_type_is_local_instead_of_silent_loss(self):
        with self.assertRaises(ns.EventPayloadError) as error:
            ns.build_payload(item(support_detail={'unsupported': 'value'}), '', 'fake-db')
        self.assertEqual(error.exception.code, 'NOTION_PAYLOAD_INVALID')


class TestCreateEventOneWrite(unittest.IsolatedAsyncioTestCase):
    async def create(self, *, status=200, data=None, error=None, text='본문', poster='https://example.invalid/poster.png'):
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        post = AsyncMock(return_value=(status, {'id': 'fake-page'} if data is None else data), side_effect=error)
        resolve = AsyncMock(return_value={'ok': True, 'kind': 'data_source', 'id': 'fake-source', 'version': ns.DS_VERSION})
        with patch.object(ns, 'get_credentials', return_value=('fake-token', 'fake-db')), \
             patch.object(ns.aiohttp, 'ClientSession', return_value=session) as factory, \
             patch.object(ns, 'resolve_target', resolve), patch.object(ns, '_post_page', post):
            result = await ns.create_event_page(item(support_detail=text), poster)
        return result, post, resolve, factory

    async def test_success_requires_page_id_and_posts_once(self):
        result, post, _, _ = await self.create()
        self.assertEqual(result, {'ok': True, 'page_id': 'fake-page'})
        post.assert_awaited_once()
        ns.validate_event_payload(post.call_args.args[2])

    async def test_image_rejection_never_retries_without_the_poster(self):
        for status in (400, 429, 500, 503):
            with self.subTest(status=status):
                result, post, _, _ = await self.create(status=status, data={'message': 'image url failed'})
                self.assertFalse(result['ok'])
                self.assertEqual(result['uncertain'], status >= 500)
                self.assertTrue(result['request_sent'])
                post.assert_awaited_once()

    async def test_empty_or_malformed_success_and_timeout_are_uncertain(self):
        for data in ({}, {'id': ''}, {'id': ' '}, {'id': 5}, []):
            with self.subTest(data=data):
                result, post, _, _ = await self.create(data=data)
                self.assertEqual(result['code'], 'NOTION_CREATE_UNCERTAIN')
                self.assertTrue(result['uncertain'])
                post.assert_awaited_once()
        for error in (TimeoutError('private'), ValueError('private')):
            result, post, _, _ = await self.create(error=error)
            self.assertTrue(result['uncertain'])
            self.assertNotIn('private', str(result))
            post.assert_awaited_once()
        result, _, _, _ = await self.create(status=408, data={})
        self.assertTrue(result['uncertain'])

    async def test_local_overflow_sends_neither_lookup_nor_create(self):
        result, post, resolve, factory = await self.create(text='가' * 100_000, poster='')
        self.assertEqual(result['code'], 'NOTION_PAYLOAD_TOO_LARGE')
        self.assertIs(result['request_sent'], False)
        self.assertIs(result['uncertain'], False)
        post.assert_not_called()
        resolve.assert_not_called()
        factory.assert_not_called()


class TestLocalPreflightClaimSafety(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import database
        from services import notion_upload_state
        self.db, self.state = database, notion_upload_state
        self.tmp = tempfile.TemporaryDirectory()
        self.db.close_all_connections()
        self.db_patch = patch.object(self.db, 'DB_FILE', str(Path(self.tmp.name) / 'synthetic.db'))
        self.db_patch.start()
        self.db.init_db()
        self.db.execute_write("INSERT INTO welfare_items(title,link) VALUES ('fixture','https://example.invalid/fixture')")

    def tearDown(self):
        self.db.close_all_connections()
        self.db_patch.stop()
        self.tmp.cleanup()

    def test_only_exact_fresh_claim_can_be_released(self):
        old = self.state.claim(1)
        self.assertFalse(self.state.release_unsubmitted(self.state.claim(1)))
        self.assertTrue(self.state.release_unsubmitted(old))
        current = self.state.claim(1)
        self.assertFalse(self.state.release_unsubmitted(old))
        self.state.complete(current['link'], 'remote-page')
        self.assertFalse(self.state.release_unsubmitted(current))
        self.assertEqual(self.state.claim(1)['state'], 'completed')

    async def upload_twice(self, response):
        import api_server
        engine = MagicMock()
        engine.close = AsyncMock()
        create = AsyncMock(return_value=response)
        with patch('scraper_engine.ScraperEngine', return_value=engine), \
             patch.object(ns, 'is_configured', return_value=True), \
             patch.object(ns, 'create_event_page', create), \
             patch.object(api_server, '_poster_for_news_item', new=AsyncMock(return_value=('', True))):
            first = await api_server._notion_upload_locked(api_server.NotionUploadRequest(ids=[1]))
            second = await api_server._notion_upload_locked(api_server.NotionUploadRequest(ids=[1]))
        return first, second, create

    async def test_local_preflight_failure_does_not_leave_uncertain_attempt(self):
        response = {'ok': False, 'code': 'NOTION_PAYLOAD_TOO_LARGE', 'error': 'local limit',
                    'request_sent': False, 'uncertain': False}
        first, second, create = await self.upload_twice(response)
        self.assertEqual(create.await_count, 2)
        self.assertEqual(first['uploaded'], 0)
        self.assertEqual(second['results'][0]['error'], 'local limit')
        self.assertEqual(self.state.uncertain_links(), set())

    async def test_sent_or_unproven_failures_keep_retry_blocked(self):
        responses = [
            {'ok': False, 'code': 'NOTION_PAYLOAD_TOO_LARGE', 'request_sent': True, 'uncertain': True},
            {'ok': False, 'code': 'NOTION_CREATE_REJECTED', 'request_sent': True, 'uncertain': False},
            {'ok': False, 'code': 'NOTION_PAYLOAD_TOO_LARGE', 'request_sent': 0, 'uncertain': 0},
            {'ok': False, 'error': 'unknown'},
        ]
        for response in responses:
            with self.subTest(response=response):
                self.state.allow_retry([1])
                first, second, create = await self.upload_twice(response)
                self.assertEqual(create.await_count, 1)
                self.assertEqual(first['uploaded'], 0)
                self.assertEqual(second['uploaded'], 0)
                self.assertIn('https://example.invalid/fixture', self.state.uncertain_links())


if __name__ == '__main__':
    unittest.main()
