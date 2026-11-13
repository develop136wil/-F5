"""Task Scheduler contracts; every subprocess and registration query is fake."""
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import api_server as api
from fastapi import HTTPException


def response(code=0, stdout=''):
    return SimpleNamespace(returncode=code, stdout=stdout, stderr='')


class TaskStatusSafety(unittest.TestCase):
    def test_present_task_is_known(self):
        with patch.object(api.sys, 'platform', 'win32'), patch('subprocess.run', return_value=response()) as run:
            self.assertEqual(api.os_task_status(), {'supported': True, 'registered': True})
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.kwargs['timeout'], 10)

    def test_query_failure_is_not_proof_of_absence(self):
        for outputs in ([response(1), response(1)],
                        [response(1), response(0, 'localized error prose')]):
            with self.subTest(outputs=outputs), patch.object(api.sys, 'platform', 'win32'), patch('subprocess.run', side_effect=outputs):
                value = api.os_task_status()
                self.assertIsNone(value['registered'])
                self.assertIn('error', value)

    def test_inventory_proves_absence_or_exact_root_task_presence(self):
        for tasks, expected in [('', False), ('"\\AnotherTask","N/A","Ready"', False),
                                ('"\\Folder\\SearchMaster_AutoScrape","N/A","Ready"', False),
                                ('"\\SearchMaster_AutoScrape","N/A","Ready"', True)]:
            with self.subTest(expected=expected), patch.object(api.sys, 'platform', 'win32'), patch('subprocess.run', side_effect=[response(1), response(0, tasks)]):
                self.assertIs(api.os_task_status()['registered'], expected)

    def test_timeout_is_unknown_without_exception_text(self):
        with patch.object(api.sys, 'platform', 'win32'), patch('subprocess.run', side_effect=subprocess.TimeoutExpired('private', 10)):
            value = api.os_task_status()
            self.assertIsNone(value['registered'])
            self.assertNotIn('private', str(value))


class TaskMutationSafety(unittest.IsolatedAsyncioTestCase):
    async def test_delete_only_succeeds_when_absence_is_confirmed(self):
        for code in (0, 1):
            with self.subTest(code=code), patch.object(api.sys, 'platform', 'win32'), patch('subprocess.run', return_value=response(code)) as run, patch.object(api.database, 'run_db', new=AsyncMock(return_value={'registered': False})):
                self.assertEqual(await api.delete_os_task(), {'status': 'success', 'registered': False})
                self.assertEqual(run.call_args.args[0], ['schtasks', '/Delete', '/TN', api._OS_TASK_NAME, '/F'])

    async def test_delete_does_not_claim_success_if_present_or_unknown(self):
        for state, code in [({'registered': True}, 409), ({'registered': None, 'error': 'unknown'}, 502),
                            ({'registered': False, 'error': 'denied'}, 502), ({}, 502)]:
            with self.subTest(state=state), patch.object(api.sys, 'platform', 'win32'), patch('subprocess.run', return_value=response(1)), patch.object(api.database, 'run_db', new=AsyncMock(return_value=state)):
                with self.assertRaises(HTTPException) as caught:
                    await api.delete_os_task()
                self.assertEqual(caught.exception.status_code, code)

    async def test_register_validates_input_before_sending(self):
        for params in ({'mode': 'weekly'}, {'time': '99:00'}, {'time': '09:60'}, {'mode': 'interval', 'interval_hours': 0}):
            with self.subTest(params=params), patch.object(api.sys, 'platform', 'win32'), patch('subprocess.run') as run:
                with self.assertRaises(HTTPException) as caught:
                    await api.register_os_task(api.OsTaskRequest(**params))
                self.assertEqual(caught.exception.status_code, 400)
                run.assert_not_called()

    async def test_register_checks_observed_state(self):
        for current, succeeds in [({'registered': True}, True), ({'registered': False}, False),
                                  ({'registered': None, 'error': 'unknown'}, False)]:
            with self.subTest(current=current), patch.object(api.sys, 'platform', 'win32'), patch('subprocess.run', return_value=response()), patch.object(api.database, 'run_db', new=AsyncMock(return_value=current)):
                if succeeds:
                    self.assertTrue((await api.register_os_task(api.OsTaskRequest()))['registered'])
                else:
                    with self.assertRaises(HTTPException) as caught:
                        await api.register_os_task(api.OsTaskRequest())
                    self.assertEqual(caught.exception.status_code, 502)
