"""Synthetic quota reservations only; no SDK clients or operational profiles.

The --quota-probe entry point is a reviewed child-process fixture. It only uses
the temporary directory supplied by its parent test and does not call an API.
"""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from services import ai_coordinator as coordinator
from services import ai_rate_policy as rate

GEN, EMBED = rate.MODELS
INPUT_TOKENS = rate.estimate_input_tokens('synthetic quota test')


def observed_config(root):
    """Synthetic supported-model limits, only for isolated concurrency tests."""
    key = 'synthetic-ledger-key-not-a-credential'
    value = rate.observe({GEN: {'rpm': 15, 'tpm': 100000, 'rpd': 1000},
                          EMBED: {'rpm': 100, 'tpm': 100000, 'rpd': 1000}}, key=key)
    (Path(root) / 'config.json').write_text(json.dumps({'GOOGLE_API_KEY': key,
        'AI_DAILY_REQUEST_LIMIT': 20, rate.POLICY_KEY: value}), encoding='utf-8')


def independent(state, name='independent_quota_fixture'):
    spec = importlib.util.spec_from_file_location(name, Path(coordinator.__file__))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._state_path = lambda: str(state)
    return module


class TestSharedQuotaLedger(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='quota-ledger-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        observed_config(self.root)
        self.state = self.root / 'state.json'
        self.path_patch = patch.object(coordinator, '_state_path', return_value=str(self.state))
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        coordinator._reset_for_tests()
        self.addCleanup(coordinator._reset_for_tests)

    def read(self):
        return json.loads(self.state.read_text(encoding='utf-8'))

    async def test_stale_reader_cannot_erase_peer_exhaustion(self):
        coordinator._load_state()
        peer = independent(self.state)
        peer.mark_daily_exhausted(GEN)
        before = self.state.read_bytes()
        with self.assertRaises(coordinator.AiDailyQuotaExhausted):
            await coordinator.acquire_model_slot(GEN, input_tokens=INPUT_TOKENS)
        self.assertEqual(self.state.read_bytes(), before)
        self.assertTrue(coordinator.status()['models'][GEN]['daily_exhausted'])

    async def test_stale_counts_are_max_snapshots_not_summed_or_overwritten(self):
        peer = independent(self.state)
        coordinator._load_state()
        peer._load_state()
        for _ in range(3):
            await peer.acquire_model_slot(GEN, input_tokens=INPUT_TOKENS)
        await coordinator.acquire_model_slot(GEN, input_tokens=INPUT_TOKENS)
        await peer.acquire_model_slot(GEN, input_tokens=INPUT_TOKENS)
        self.assertEqual(self.read()['calls'][GEN], 5)
        self.assertEqual(coordinator.status()['recorded_requests_today'], 5)
        self.assertEqual(peer.status()['recorded_requests_today'], 5)

    async def test_other_model_and_wildcard_holds_survive_merging(self):
        coordinator._load_state()
        peer = independent(self.state)
        peer.mark_daily_exhausted(EMBED)
        await coordinator.acquire_model_slot(GEN, input_tokens=INPUT_TOKENS)
        self.assertIn(EMBED, self.read()['exhausted'])
        peer.mark_daily_exhausted('*')
        for model in (GEN, EMBED, 'new-model'):
            with self.subTest(model=model), self.assertRaises(coordinator.AiDailyQuotaExhausted):
                await coordinator.acquire_model_slot(model, input_tokens=INPUT_TOKENS)
        self.assertEqual(set(self.read()['exhausted']), {EMBED, '*'})
        self.assertEqual(self.read()['calls'], {GEN: 1})

    async def test_midnight_drops_only_previous_day_not_peers_new_day(self):
        peer = independent(self.state)
        with patch.object(coordinator, '_pacific_today', return_value='2026-09-07'):
            await coordinator.acquire_model_slot(GEN, input_tokens=INPUT_TOKENS)
            coordinator.mark_daily_exhausted('old-model')
        with patch.object(peer, '_pacific_today', return_value='2026-09-08'):
            for _ in range(3):
                await peer.acquire_model_slot(GEN, input_tokens=INPUT_TOKENS)
            peer.mark_daily_exhausted('new-model')
        with patch.object(coordinator, '_pacific_today', return_value='2026-09-08'):
            await coordinator.acquire_model_slot(GEN, input_tokens=INPUT_TOKENS)
            self.assertEqual(coordinator.status()['recorded_requests_today'], 4)
        self.assertEqual(self.read()['date_pacific'], '2026-09-08')
        self.assertEqual(self.read()['exhausted'], {'new-model': '2026-09-08'})

    async def test_backward_clock_cannot_replace_future_ledger(self):
        peer = independent(self.state)
        with patch.object(peer, '_pacific_today', return_value='2026-09-09'):
            await peer.acquire_model_slot(GEN, input_tokens=INPUT_TOKENS)
        before = self.state.read_bytes()
        with patch.object(coordinator, '_pacific_today', return_value='2026-09-08'):
            with self.assertRaises(coordinator.AiQuotaStateUnavailable):
                await coordinator.acquire_model_slot(GEN, input_tokens=INPUT_TOKENS)
        self.assertEqual(self.state.read_bytes(), before)

    async def test_corrupt_file_is_preserved_and_shared_hold_is_durable(self):
        damaged = b'{synthetic broken json'
        self.state.write_bytes(damaged)
        with self.assertRaises(coordinator.AiDailyQuotaExhausted):
            await coordinator.acquire_model_slot(GEN, input_tokens=INPUT_TOKENS)
        self.assertEqual(next(self.root.glob('state.json.corrupt.*')).read_bytes(), damaged)
        peer = independent(self.state)
        with self.assertRaises(peer.AiDailyQuotaExhausted):
            await peer.acquire_model_slot(EMBED, input_tokens=INPUT_TOKENS)
        self.assertIn('*', self.read()['exhausted'])

    async def test_write_failure_does_not_reserve_or_admit_and_can_recover(self):
        coordinator._load_state()
        before = self.state.read_bytes()
        with patch.object(coordinator.os, 'replace', side_effect=OSError('synthetic write failure')):
            with self.assertRaises(coordinator.AiQuotaStateUnavailable):
                await coordinator.acquire_model_slot(GEN, input_tokens=INPUT_TOKENS)
            self.assertTrue(coordinator._STATE_WARNING)
        self.assertEqual(self.state.read_bytes(), before)
        self.assertFalse(coordinator._MODEL_CALLS[GEN])
        self.assertEqual(coordinator._DAILY_CALLS[GEN], 0)
        self.assertEqual(list(self.root.glob('ai_quota_*.tmp')), [])
        await coordinator.acquire_model_slot(GEN, input_tokens=INPUT_TOKENS)
        self.assertEqual(self.read()['calls'][GEN], 1)
        self.assertIsNone(coordinator.status()['state_warning'])

    async def test_read_or_fsync_failure_does_not_admit(self):
        coordinator._load_state()
        before = self.state.read_bytes()
        for target in ('_refresh_state_locked', 'fsync'):
            with self.subTest(target=target):
                owner = coordinator if target == '_refresh_state_locked' else coordinator.os
                with patch.object(owner, target, side_effect=OSError('synthetic I/O failure')):
                    with self.assertRaises(coordinator.AiQuotaStateUnavailable):
                        await coordinator.acquire_model_slot(GEN, input_tokens=INPUT_TOKENS)
                self.assertEqual(self.state.read_bytes(), before)
                self.assertFalse(coordinator._MODEL_CALLS[GEN])

    async def test_sync_error_registration_retains_pending_hold_on_write_failure(self):
        coordinator._load_state()
        before = self.state.read_bytes()
        with patch.object(coordinator.os, 'replace', side_effect=OSError('synthetic write failure')):
            info = coordinator.register_api_error(GEN, RuntimeError('429 requests per day exceeded'))
            self.assertTrue(info['daily_exhausted'])
            self.assertIn(GEN, coordinator._PENDING_EXHAUSTED)
            self.assertTrue(coordinator._STATE_WARNING)
            with self.assertRaises(coordinator.AiQuotaStateUnavailable):
                await coordinator.acquire_model_slot(GEN, input_tokens=INPUT_TOKENS)
        self.assertEqual(self.state.read_bytes(), before)
        with self.assertRaises(coordinator.AiDailyQuotaExhausted):
            await coordinator.acquire_model_slot(GEN, input_tokens=INPUT_TOKENS)
        self.assertFalse(coordinator._PENDING_EXHAUSTED)
        self.assertIn(GEN, self.read()['exhausted'])

    async def test_sync_contention_is_finite_and_pending_hold_is_not_lost(self):
        with coordinator._quota_locked():
            start = time.monotonic()
            coordinator.mark_daily_exhausted(GEN)
            self.assertLess(time.monotonic() - start, 0.75)
            self.assertIn(GEN, coordinator._PENDING_EXHAUSTED)
        with self.assertRaises(coordinator.AiDailyQuotaExhausted):
            await coordinator.acquire_model_slot(GEN, input_tokens=INPUT_TOKENS)
        self.assertIn(GEN, self.read()['exhausted'])

    async def test_contention_intent_survives_recording_instance_disappearance(self):
        peer = independent(self.state)
        with peer._quota_locked():
            coordinator.mark_daily_exhausted(GEN)
            self.assertEqual(len(list(self.root.glob('state.json.exhausted.*.json'))), 1)
        coordinator._reset_for_tests()  # Discard all recording-instance memory.
        replacement = independent(self.state, 'replacement_quota_fixture')
        with self.assertRaises(replacement.AiDailyQuotaExhausted):
            await replacement.acquire_model_slot(GEN, input_tokens=INPUT_TOKENS)
        self.assertIn(GEN, self.read()['exhausted'])
        self.assertEqual(list(self.root.glob('state.json.exhausted.*.json')), [])

    async def test_intent_publish_during_cleanup_preserves_same_and_other_model(self):
        for published in (GEN, EMBED):
            with self.subTest(published=published):
                state = self.root / f'{published}.json'
                owner = independent(state, f'owner_{published}')
                peer = independent(state, f'peer_{published}')
                day = owner._pacific_today()
                owner._persist_exhaustion_intent(GEN, day)
                remove = os.remove
                injected = []
                def publish_before_remove(path):
                    if '.exhausted.' in str(path) and not injected:
                        injected.append(True)
                        peer._persist_exhaustion_intent(published, day)
                    return remove(path)
                with patch.object(owner.os, 'remove', side_effect=publish_before_remove):
                    owner._load_state()
                self.assertTrue(injected)
                self.assertTrue(list(self.root.glob(f'{published}.json.exhausted.*.json')))
                replacement = independent(state, f'replacement_{published}')
                for model in {GEN, published}:
                    with self.assertRaises(replacement.AiDailyQuotaExhausted):
                        await replacement.acquire_model_slot(model, input_tokens=INPUT_TOKENS)
                self.assertEqual(set(json.loads(state.read_text())['exhausted']), {GEN, published})

    async def test_intent_filenames_never_include_model_or_date_text(self):
        with coordinator._quota_locked():
            coordinator.mark_daily_exhausted('../synthetic-model/with:separators')
        paths = list(self.root.glob('state.json.exhausted.*.json'))
        self.assertEqual(len(paths), 1)
        self.assertRegex(paths[0].name, r'^state\.json\.exhausted\.[0-9a-f]{32}\.json$')
        self.assertEqual(json.loads(paths[0].read_text())['model'], '../synthetic-model/with:separators')

    async def test_marker_read_race_or_corruption_fails_closed(self):
        coordinator._persist_exhaustion_intent(GEN, coordinator._pacific_today())
        before = list(self.root.glob('state.json.exhausted.*.json'))[0]
        with patch.object(coordinator, '_exhaustion_intents', side_effect=FileNotFoundError('synthetic read race')):
            with self.assertRaises(coordinator.AiQuotaStateUnavailable):
                await coordinator.acquire_model_slot(GEN, input_tokens=INPUT_TOKENS)
        self.assertTrue(before.exists())
        before.write_text('invalid-json', encoding='utf-8')
        with self.assertRaises(coordinator.AiDailyQuotaExhausted):
            await coordinator.acquire_model_slot(EMBED, input_tokens=INPUT_TOKENS)
        self.assertEqual(before.read_text(), 'invalid-json')
        self.assertIn('*', self.read()['exhausted'])

    def test_failed_file_close_still_releases_thread_lock(self):
        import errno
        from unittest.mock import Mock
        handle = Mock()
        handle.fileno.return_value = 0
        handle.close.side_effect = OSError('synthetic close failure')
        operation = 'msvcrt.locking' if os.name == 'nt' else 'fcntl.flock'
        with patch.object(coordinator, 'open', return_value=handle, create=True), \
             patch(operation, side_effect=OSError(errno.EACCES, 'synthetic lock busy')):
            with self.assertRaises(coordinator._QuotaStateBusy):
                with coordinator._quota_locked():
                    self.fail('Lock must not be admitted')
        self.assertFalse(coordinator._QUOTA_THREAD_LOCK.locked())

    async def test_cancel_while_lock_busy_does_not_reserve_or_leak_waiter(self):
        peer = independent(self.state)
        with peer._quota_locked():
            task = asyncio.create_task(coordinator.acquire_model_slot(GEN, input_tokens=INPUT_TOKENS))
            await asyncio.sleep(0.02)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(coordinator._MODEL_WAITERS[GEN], 0)
        self.assertFalse(coordinator._MODEL_CALLS[GEN])
        await coordinator.acquire_model_slot(GEN, input_tokens=INPUT_TOKENS)
        self.assertEqual(self.read()['calls'][GEN], 1)

    async def test_rpm_wait_does_not_hold_cross_process_ledger_lock(self):
        peer = independent(self.state)
        with patch.dict(os.environ, {'AI_GLOBAL_GENERATE_RPM': '1', 'AI_GLOBAL_RATE_PERIOD_SEC': '0.1'}):
            await coordinator.acquire_model_slot(GEN, input_tokens=INPUT_TOKENS)
            waiting = asyncio.create_task(coordinator.acquire_model_slot(GEN, input_tokens=INPUT_TOKENS))
            await asyncio.sleep(0.01)
            peer.mark_daily_exhausted(GEN)
            self.assertFalse(peer._PENDING_EXHAUSTED)
            with self.assertRaises(coordinator.AiDailyQuotaExhausted):
                await waiting
        self.assertEqual(self.read()['calls'][GEN], 1)


class TestQuotaLedgerProcesses(unittest.TestCase):
    def test_simultaneous_process_reservations_have_no_lost_increments(self):
        with tempfile.TemporaryDirectory(prefix='quota-process-test-') as temporary:
            root = Path(temporary)
            observed_config(root)
            children = []
            try:
                for name in ('first', 'second'):
                    command = [sys.executable, '-B', '-m', 'tests.test_ai_quota_ledger',
                               '--quota-probe', str(root), 'reserve', '5', name]
                    children.append(subprocess.Popen(command, cwd=Path(__file__).resolve().parents[1],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
                deadline = time.monotonic() + 15
                while not all((root / ('ready-' + name)).exists() for name in ('first', 'second')):
                    if time.monotonic() > deadline or any(child.poll() is not None for child in children):
                        self.fail('Synthetic children did not become ready')
                    time.sleep(0.01)
                (root / 'start').touch()
                for child in children:
                    stdout, stderr = child.communicate(timeout=15)
                    self.assertEqual(child.returncode, 0, stderr)
                    self.assertEqual(json.loads(stdout)['reserved'], 5)
                data = json.loads((root / 'state.json').read_text())
                self.assertEqual(data['calls'], {GEN: 10})
            finally:
                for child in children:
                    if child.poll() is None:
                        child.kill()
                        child.communicate(timeout=5)

    def test_already_open_parent_observes_child_exhaustion(self):
        with tempfile.TemporaryDirectory(prefix='quota-process-test-') as temporary:
            root = Path(temporary)
            observed_config(root)
            parent = independent(root / 'state.json')
            parent._load_state()
            command = [sys.executable, '-B', '-m', 'tests.test_ai_quota_ledger',
                       '--quota-probe', str(root), 'exhaust', '0', 'immediate']
            completed = subprocess.run(command, cwd=Path(__file__).resolve().parents[1],
                capture_output=True, text=True, timeout=15)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            before = (root / 'state.json').read_bytes()
            with self.assertRaises(parent.AiDailyQuotaExhausted):
                asyncio.run(parent.acquire_model_slot(GEN, input_tokens=INPUT_TOKENS))
            self.assertEqual((root / 'state.json').read_bytes(), before)

    def test_child_exit_after_lock_contention_keeps_durable_hold(self):
        with tempfile.TemporaryDirectory(prefix='quota-process-test-') as temporary:
            root = Path(temporary)
            observed_config(root)
            holder = independent(root / 'state.json')
            command = [sys.executable, '-B', '-m', 'tests.test_ai_quota_ledger',
                       '--quota-probe', str(root), 'exhaust', '0', 'immediate']
            with holder._quota_locked():
                completed = subprocess.run(command, cwd=Path(__file__).resolve().parents[1],
                    capture_output=True, text=True, timeout=15)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertTrue(list(root.glob('state.json.exhausted.*.json')))
            fresh = independent(root / 'state.json')
            with self.assertRaises(fresh.AiDailyQuotaExhausted):
                asyncio.run(fresh.acquire_model_slot(GEN, input_tokens=INPUT_TOKENS))
            self.assertIn(GEN, json.loads((root / 'state.json').read_text())['exhausted'])


def probe(root, mode, count, name):
    root = Path(root).resolve(strict=True)
    coordinator._state_path = lambda: str(root / 'state.json')
    coordinator._load_state()
    if name != 'immediate':
        (root / ('ready-' + name)).touch()
        deadline = time.monotonic() + 10
        while not (root / 'start').exists():
            if time.monotonic() > deadline:
                raise TimeoutError('Synthetic barrier timed out')
            time.sleep(0.01)
    async def reserve():
        for _ in range(count):
            await coordinator.acquire_model_slot(GEN, input_tokens=INPUT_TOKENS)
    if mode == 'reserve' and 0 <= count <= 5:
        asyncio.run(reserve())
    elif mode == 'exhaust':
        coordinator.mark_daily_exhausted(GEN)
    else:
        raise ValueError('Unknown synthetic probe')
    print(json.dumps({'mode': mode, 'reserved': count, 'real_api_calls': 0}))


if __name__ == '__main__':
    if len(sys.argv) == 6 and sys.argv[1] == '--quota-probe':
        probe(sys.argv[2], sys.argv[3], int(sys.argv[4]), sys.argv[5])
    else:
        unittest.main()
