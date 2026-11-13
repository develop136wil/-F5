"""Opt-in real loopback allocation probe; run through run_isolated_tests.py."""
import asyncio
import gc
import json
import os
import socket
import tracemalloc
import unittest
import urllib.request


@unittest.skipUnless(os.environ.get('WELFARE_ALLOCATION_PROBE') == '1', 'opt-in diagnostic')
class HttpAllocationProbe(unittest.IsolatedAsyncioTestCase):
    async def test_loopback_allocations(self):
        import api_server
        import uvicorn
        from services.windows_transport import install_reset_safe_close
        install_reset_safe_close()
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(api_server.app, log_config=None, access_log=False))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def request():
            with opener.open(f'http://127.0.0.1:{port}/api/status', timeout=3) as response:
                self.assertEqual(response.status, 200)
                response.read()
        try:
            async with asyncio.timeout(30):
                while not server.started:
                    if task.done():
                        await task
                        self.fail('Server exited before readiness')
                    await asyncio.sleep(.05)
            for _ in range(20):
                await asyncio.to_thread(request)
            tracemalloc.start(10)
            before = tracemalloc.take_snapshot()
            for _ in range(300):
                await asyncio.to_thread(request)
            await asyncio.sleep(.2)
            after = tracemalloc.take_snapshot()
            print('HTTP_ALLOCATION_BEFORE_GC', json.dumps([
                dict(trace=str(item.traceback), bytes=item.size_diff, count=item.count_diff)
                for item in after.compare_to(before, 'lineno')[:12]]), flush=True)
            gc.collect()
            after_gc = tracemalloc.take_snapshot()
            print('HTTP_ALLOCATION_AFTER_GC', json.dumps([
                dict(trace=str(item.traceback), bytes=item.size_diff, count=item.count_diff)
                for item in after_gc.compare_to(before, 'lineno')[:12]]), flush=True)
        finally:
            tracemalloc.stop()
            server.should_exit = True
            await asyncio.wait_for(task, 30)
            listener.close()
