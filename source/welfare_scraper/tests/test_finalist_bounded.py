"""Offline only: fake transport; never load real credentials or send requests."""
import asyncio
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from scripts import evaluate_finalist_bounded as runner


def embedding(vector=None):
    return SimpleNamespace(embeddings=[SimpleNamespace(values=vector if vector is not None else [1.0] + [0.0] * 767)])


def good_response(case):
    source = case['source']
    if case['kind'] == 'policy':
        result = dict(support_detail=source, eligibility_detail='', age_detail='', target_income='',
                      start_age_months=None, end_age_months=None, residence=[], life_cycle=[],
                      _source_evidence={'support_detail': source})
        result.update(case['expected']['equals'])
        return result
    result = dict(is_relevant=True, category='교육', target='미상', residence=[], life_cycle=[],
                  target_characteristics=[], interest_topics=[], start_age_months=None, end_age_months=None,
                  evidence_snippet=source, applicant_type='unknown', applicant_evidence='',
                  applicant_detail='미상', beneficiary_detail='미상', support_detail=source,
                  eligibility_detail='', contact='', deadline='', life_cycle_evidence=[], residence_evidence=[],
                  fact_evidence=dict(support_detail=source, eligibility_detail='', contact='', deadline='', is_relevant=source))
    result.update(case['expected']['equals'])
    if result['applicant_type'] != 'unknown':
        result['applicant_evidence'] = source.split('. ')[-1]
    return result


class FakeClock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class LedgerAndGateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def test_dry_run_and_missing_gates_never_read_config_or_construct_client(self):
        choices = [([], 0), (['--config', 'unused.json'], 0),
                   (['--execute'], 2), (['--execute', '--config', 'unused.json'], 2),
                   (['--execute', '--free-tier-confirmed'], 2)]
        for args, expected in choices:
            with self.subTest(args=args), patch.object(Path, 'open', side_effect=AssertionError('Unexpected read')), \
                    patch.object(runner, 'execute_with_client', new_callable=AsyncMock) as execute, \
                    redirect_stdout(io.StringIO()) as output:
                self.assertEqual(runner.main(args), expected)
                self.assertEqual(json.loads(output.getvalue())['requests'], 0)
                execute.assert_not_awaited()

    def test_missing_key_and_existing_output_send_zero_requests(self):
        config = self.root / 'config.json'
        output_dir = self.root / 'already-used'
        output_dir.mkdir()
        for value in ({}, {'GOOGLE_API_KEY': 'synthetic-key-not-real'}):
            config.write_text(json.dumps(value), encoding='utf-8')
            with patch.object(runner, 'LOCK_PATH', self.root / 'lock'), \
                    patch.object(runner, 'single_run_lock', lambda: runner_lock(self.root / 'lock')), \
                    patch.object(runner, 'execute_with_client', new_callable=AsyncMock) as execute, \
                    redirect_stdout(io.StringIO()) as output:
                self.assertEqual(runner.main(['--execute', '--free-tier-confirmed', '--config', str(config),
                                              '--output-dir', str(output_dir)]), 2)
                execute.assert_not_awaited()
                self.assertNotIn('synthetic-key-not-real', output.getvalue())
            self.assertEqual(json.loads(config.read_text(encoding='utf-8')), value)

    def test_plan_is_exactly_ten_fixed_models_and_explicit_limitations(self):
        plan = runner.plan()
        self.assertEqual(len(plan['cases']), 10)
        self.assertEqual([sum(case['kind'] == k for case in plan['cases']) for k in ('policy', 'news', 'embedding')], [4, 4, 2])
        self.assertEqual(plan['model'], 'gemini-3.5-flash-lite')
        self.assertEqual(plan['sdk_attempts'], 1)
        self.assertIn('OCR/images', plan['not_evaluated'])
        self.assertTrue(all(len(case['source']) < 16000 and case['expected'] for case in plan['cases']))

    def test_ledger_exclusive_and_secret_redacted(self):
        path = self.root / 'ledger.jsonl'
        ledger = runner.Ledger(path, 'synthetic-secret')
        try:
            ledger.append({'raw': 'synthetic-secret'})
            with self.assertRaises(FileExistsError):
                runner.Ledger(path)
        finally:
            ledger.close()
        self.assertEqual(json.loads(path.read_text())['raw'], '[REDACTED]')

    def test_os_lock_rejects_concurrent_output_then_releases(self):
        path = self.root / 'lock'
        with runner.single_run_lock(path):
            with self.assertRaises(OSError), runner.single_run_lock(path):
                self.fail('Concurrent lock entered')
        with runner.single_run_lock(path):
            pass

    def test_safe_error_never_includes_exception_message_or_string_code(self):
        error = RuntimeError('synthetic-secret')
        error.code = 'synthetic-secret'
        error.status_code = 429
        self.assertEqual(runner.safe_error(error), {'type': 'RuntimeError', 'status_code': 429})

    def test_optional_response_usage_metadata_is_recorded_without_invented_counts(self):
        response = SimpleNamespace(text='{}', model_version='synthetic-model', response_id='synthetic-id',
                                   usage_metadata=SimpleNamespace(prompt_token_count=12, total_token_count=20))
        record = runner.generation_record(response)
        self.assertEqual(record['usage_metadata']['prompt_token_count'], 12)
        self.assertEqual(record['usage_metadata']['total_token_count'], 20)
        self.assertIsNone(record['usage_metadata']['candidates_token_count'])
        self.assertEqual(record['response_id'], 'synthetic-id')
        self.assertIsNone(runner.generation_record(SimpleNamespace(text='{}'))['model_version'])

    def test_guard_blocks_external_writes_databases_dotenv_services_and_children(self):
        guard = runner.IsolationGuard(self.root)
        forbidden = [('open', (str(self.root.parent / '.env'), 'r', 0)),
                     ('open', (str(self.root.parent / 'user.json'), 'w', os.O_WRONLY)),
                     ('sqlite3.connect', (str(self.root.parent / 'user.db'),)),
                     ('os.remove', (str(self.root.parent / 'user.json'), -1)),
                     ('os.rename', (str(self.root / 'own'), str(self.root.parent / 'user'), -1, -1)),
                     ('os.symlink', (str(self.root.parent / 'user'), str(self.root / 'link'), -1)),
                     ('socket.getaddrinfo', ('api.notion.com', 443)),
                     ('socket.connect', (None, ('127.0.0.1', 8000))),
                     ('socket.connect', (None, ('198.51.100.1', 443))),
                     ('socket.bind', (None, ('0.0.0.0', 0))),
                     ('subprocess.Popen', ('unused', [], None, None))]
        for event, args in forbidden:
            with self.subTest(event=event, args=args), self.assertRaises(PermissionError):
                guard(event, args)
        guard('open', (str(self.root / 'own.json'), 'w', os.O_WRONLY))
        guard('sqlite3.connect', (str(self.root / 'own.db'),))
        guard('socket.getaddrinfo', ('generativelanguage.googleapis.com', 443))
        guard.google_addresses.add('198.51.100.1')
        guard('socket.connect', (None, ('198.51.100.1', 443)))

    def test_runtime_refuses_preexisting_application_state(self):
        with patch.dict('sys.modules', {'database': Mock()}), self.assertRaisesRegex(RuntimeError, 'StandaloneProcessRequired'):
            with runner.isolated_runtime(self.root):
                self.fail('Entered active application state')
        self.assertEqual(list(self.root.iterdir()), [])


runner_lock = runner.single_run_lock


class BoundedTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'ledger.jsonl'
        self.ledger = runner.Ledger(self.path, secret='synthetic-secret')
        self.clock = FakeClock()
        self.budget = runner.RequestBudget(self.ledger, clock=self.clock, sleep=self.clock.sleep)

    def tearDown(self):
        self.ledger.close()
        self.directory.cleanup()

    def events(self):
        return [json.loads(line) for line in self.path.read_text(encoding='utf-8').splitlines()]

    async def test_ten_cap_and_sequential_fifteen_second_spacing(self):
        invoke = AsyncMock(return_value=SimpleNamespace(text='{}'))
        for i in range(10):
            await self.budget.dispatch(str(i), 'generate', 'synthetic', invoke, lambda value: value.text)
        with self.assertRaises(runner.EvaluationStopped):
            await self.budget.dispatch('extra', 'generate', 'synthetic', invoke, lambda value: value.text)
        self.assertEqual(invoke.await_count, 10)
        self.assertEqual(self.clock.sleeps, [15] * 9)
        self.assertEqual(len([e for e in self.events() if e['event'] == 'attempt_reserved']), 10)

    async def test_reservation_is_durable_before_dispatch(self):
        async def invoke():
            self.assertEqual(self.events()[-1]['event'], 'attempt_reserved')
            self.assertEqual(self.events()[-1]['attempt'], 1)
            return '{}'
        with patch.object(runner.os, 'fsync', wraps=os.fsync) as sync:
            await self.budget.dispatch('one', 'generate', 'synthetic', invoke, lambda value: value)
            self.assertEqual(sync.call_count, 2)

    async def test_failed_reservation_fsync_prevents_all_dispatch(self):
        invoke = AsyncMock()
        with patch.object(runner.os, 'fsync', side_effect=OSError('synthetic-secret')):
            with self.assertRaises(runner.EvaluationStopped):
                await self.budget.dispatch('one', 'generate', 'synthetic', invoke, lambda value: value)
        with self.assertRaises(runner.EvaluationStopped):
            await self.budget.dispatch('two', 'generate', 'synthetic', invoke, lambda value: value)
        invoke.assert_not_awaited()

    async def test_input_limit_rejects_without_dispatch(self):
        invoke = AsyncMock()
        with self.assertRaises(runner.EvaluationStopped):
            await self.budget.dispatch('one', 'generate', 'a' * 16001, invoke, lambda value: value)
        self.assertEqual(self.budget.attempts, 0)
        invoke.assert_not_awaited()

    async def test_error_timeout_and_cancellation_are_consumed_and_never_retried(self):
        for error in (RuntimeError('synthetic-secret'), TimeoutError('synthetic-secret'), asyncio.CancelledError()):
            budget = runner.RequestBudget(self.ledger, clock=self.clock, sleep=self.clock.sleep)
            invoke = AsyncMock(side_effect=error)
            with self.subTest(error=type(error).__name__), self.assertRaises(runner.EvaluationStopped):
                await budget.dispatch('one', 'generate', 'synthetic', invoke, lambda value: value)
            with self.assertRaises(runner.EvaluationStopped):
                await budget.dispatch('two', 'generate', 'synthetic', invoke, lambda value: value)
            self.assertEqual(invoke.await_count, 1)
            self.assertEqual(budget.attempts, 1)
        self.assertNotIn('synthetic-secret', self.path.read_text())

    async def test_concurrent_dispatch_is_rejected(self):
        entered, finish = asyncio.Event(), asyncio.Event()
        async def invoke():
            entered.set()
            await finish.wait()
            return '{}'
        first = asyncio.create_task(self.budget.dispatch('one', 'generate', 'synthetic', invoke, lambda value: value))
        await entered.wait()
        with self.assertRaises(runner.EvaluationStopped):
            await self.budget.dispatch('two', 'generate', 'synthetic', AsyncMock(), lambda value: value)
        finish.set()
        await first
        self.assertEqual(self.budget.attempts, 1)

    async def test_sdk_has_exactly_one_attempt_and_45_second_timeout(self):
        from google import genai
        client = SimpleNamespace(aio=SimpleNamespace(aclose=AsyncMock()), close=Mock())
        with patch.object(genai, 'Client', return_value=client) as create, \
                patch.object(runner, 'evaluate', new_callable=AsyncMock, return_value={}) as evaluate:
            await runner.execute_with_client('synthetic-secret', self.ledger)
        options = create.call_args.kwargs['http_options']
        self.assertEqual(options.timeout, 45000)
        self.assertEqual(options.retry_options.attempts, 1)
        self.assertEqual(options.base_url, 'https://generativelanguage.googleapis.com')
        self.assertIs(create.call_args.kwargs['vertexai'], False)
        evaluate.assert_awaited_once()
        client.aio.aclose.assert_awaited_once()
        client.close.assert_called_once()

    def client(self, outputs):
        generate = AsyncMock(side_effect=[SimpleNamespace(text=json.dumps(row, ensure_ascii=False)) for row in outputs])
        embed = AsyncMock(return_value=embedding())
        return SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate, embed_content=embed)))

    async def test_full_ten_fake_requests_use_production_prompts_and_validators(self):
        client = self.client([good_response(case) for case in runner.cases()[:8]])
        result = await runner.evaluate(client, self.ledger, budget=self.budget)
        self.assertEqual(result, {'status': 'complete', 'attempts': 10, 'completed': 10}, self.events())
        self.assertEqual(client.aio.models.generate_content.await_count, 8)
        self.assertEqual(client.aio.models.embed_content.await_count, 2)
        calls = client.aio.models.generate_content.call_args_list
        self.assertIn('Extract ONE welfare policy', calls[0].kwargs['contents'])
        self.assertIn('_source_units', calls[0].kwargs['contents'])
        self.assertIn('[Source units JSON: id and complete original text]', calls[0].kwargs['contents'])
        self.assertIn('Extract welfare announcement facts', calls[4].kwargs['contents'])
        self.assertIn('fact_evidence', calls[4].kwargs['contents'])
        for call in calls:
            self.assertEqual(call.kwargs['model'], runner.MODEL)
            self.assertEqual(call.kwargs['config'].max_output_tokens, 3072)
            self.assertLessEqual(len(call.kwargs['contents']), 16000)
        results = [e for e in self.events() if e['event'] == 'case_result']
        self.assertEqual(results[6]['decision']['target_status'], 'excluded')
        self.assertEqual(results[7]['decision']['target_status'], 'review')

    async def test_quality_failure_stops_after_first_without_next_call(self):
        client = self.client([{}])
        result = await runner.evaluate(client, self.ledger, budget=self.budget)
        self.assertEqual(result['attempts'], 1)
        self.assertEqual(result['status'], 'stopped')
        client.aio.models.generate_content.assert_awaited_once()
        client.aio.models.embed_content.assert_not_awaited()

    async def test_invalid_json_and_transport_failure_stop_batch(self):
        for payload in ('not json', RuntimeError('synthetic-secret')):
            client = self.client([])
            client.aio.models.generate_content.side_effect = payload if isinstance(payload, Exception) else None
            client.aio.models.generate_content.return_value = SimpleNamespace(text=payload)
            result = await runner.evaluate(client, self.ledger, budget=runner.RequestBudget(self.ledger))
            self.assertEqual(result['attempts'], 1)
            self.assertEqual(result['status'], 'stopped')
            client.aio.models.generate_content.assert_awaited_once()
        self.assertNotIn('synthetic-secret', self.path.read_text())

    async def test_invalid_embedding_uses_production_vector_validation_and_stops(self):
        client = self.client([])
        client.aio.models.embed_content.return_value = embedding([0.0] * 768)
        with patch.object(runner, 'cases', lambda: runner_cases()[-2:]):
            result = await runner.evaluate(client, self.ledger, budget=self.budget)
        self.assertEqual(result['status'], 'stopped')
        self.assertEqual(result['attempts'], 1)
        client.aio.models.embed_content.assert_awaited_once()


runner_cases = runner.cases
