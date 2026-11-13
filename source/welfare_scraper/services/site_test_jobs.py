"""Small, read-only browser diagnostics independent of an HTTP connection.

The existing workflow lease also makes desktop shutdown and database restore
wait for diagnostics/cleanup. No new AI permission or durable user data is added.
"""
import asyncio
import copy
import json
import logging
import uuid
from collections import OrderedDict
from datetime import datetime, timezone

from services import ai_coordinator

logger = logging.getLogger(__name__)
TEST_TIMEOUT_SECONDS = 120
MAX_RESULTS = 64


def identity(site):
    # Verification itself updates registration_status; it is not site identity.
    return json.dumps({key: site.get(key) for key in
                      ('id', 'name', 'url', 'base_url', 'selectors', 'max_pages')},
                     sort_keys=True, ensure_ascii=False, default=str)


def _now():
    return datetime.now(timezone.utc).isoformat()


class SiteTestBusy(RuntimeError):
    pass


class SiteTestJobs:
    def __init__(self):
        self._records = OrderedDict()
        self._active = None
        self._task = None
        self._cancel_requested = False

    @staticmethod
    def _public(record):
        return copy.deepcopy({key: value for key, value in record.items()
                              if not key.startswith('_')})

    def is_current(self, site):
        record = self._records.get(site['id'])
        return bool(record and record['_identity'] == identity(site))

    def status(self, site):
        if self.is_current(site):
            return self._public(self._records[site['id']])
        return dict(site_id=site['id'], task_id=None, status='idle', running=False,
                    result=None, message='', started_at=None, finished_at=None)

    def invalidate(self, site_id):
        # A deleted/re-registered id must not inherit a prior site's result.
        self._records.pop(site_id, None)

    async def start(self, site, runner):
        if self._active is not None:
            if (self._active['site_id'] == site['id']
                    and self.is_current(site)):
                return self._public(self._active)
            raise SiteTestBusy('진행 중인 사이트 점검이 끝난 뒤 다시 시도해주세요.')
        # The coordinator's atomic lease prevents concurrent diagnostics and
        # restore/other process work, without consuming an AI request.
        if not await ai_coordinator.try_start_job('site_test', '사이트 점검'):
            raise SiteTestBusy(ai_coordinator.busy_message() or '다른 작업이 진행 중입니다.')
        try:
            record = dict(site_id=site['id'], task_id=uuid.uuid4().hex,
                          status='running', running=True, result=None,
                          message='게시판을 확인하고 있습니다.', started_at=_now(),
                          finished_at=None, _identity=identity(site))
            self._records[site['id']] = record
            self._records.move_to_end(site['id'])
            while len(self._records) > MAX_RESULTS:
                self._records.popitem(last=False)
            self._active = record
            self._cancel_requested = False
            self._task = asyncio.create_task(self._run(record, copy.deepcopy(site), runner))
            # Even cancellation before _run enters must release the lease.
            self._task.add_done_callback(lambda task: self._finish(record, task))
            return self._public(record)
        except BaseException:
            self._active = None
            ai_coordinator.finish_job('site_test')
            raise

    def _finish(self, record, task):
        if record['running']:
            record.update(status='cancelled', message='사이트 점검이 중지되었습니다.')
        record.update(running=False, finished_at=record['finished_at'] or _now())
        if self._active is record:
            self._active = None
            ai_coordinator.finish_job('site_test')
        if not task.cancelled():
            task.exception()  # Retrieve unexpected errors, never leak task warnings.

    async def _run(self, record, site, runner):
        try:
            result = await asyncio.wait_for(runner(site), timeout=TEST_TIMEOUT_SECONDS)
            record.update(status='completed', result=result, message='사이트 점검을 마쳤습니다.')
        except TimeoutError:
            record.update(status='timed_out', message='사이트 응답이 늦어 점검을 마치지 못했습니다. 잠시 후 다시 시도해주세요.')
        except asyncio.CancelledError:
            record.update(status='cancelled', message='사이트 점검이 중지되었습니다.')
            raise
        except Exception as error:
            logger.warning('사이트 점검 실패 (%s)', type(error).__name__)
            record.update(status='failed', message='사이트 점검에 실패했습니다. 게시판 주소와 연결 상태를 확인해주세요.')
        finally:
            record.update(running=False, finished_at=_now())

    def request_shutdown(self):
        """Cancel only diagnostics; never send repeated cancellation into cleanup."""
        if self._task is not None and not self._task.done() and not self._cancel_requested:
            self._cancel_requested = True
            self._task.cancel()

    async def shutdown(self):
        task = self._task
        if task is not None and not task.done():
            self.request_shutdown()
            # Keep the existing workflow lease until the runner's cleanup and
            # pending DB write settle. Never close its DB underneath it.
            try:
                await task
            except asyncio.CancelledError:
                pass
        if task is not None and self._active is not None:
            self._finish(self._active, task)


jobs = SiteTestJobs()
