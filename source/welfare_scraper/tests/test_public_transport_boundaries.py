"""Offline regressions: connection targets and complete single HTTP exchanges."""
import asyncio
import socket
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from services import public_transport as net
from services.public_browser_proxy import PublicBrowserProxy


class Writer:
    def __init__(self):
        self.data = bytearray()
        self.closed = False
    def write(self, data): self.data.extend(data)
    async def drain(self): pass
    def close(self): self.closed = True
    def is_closing(self): return self.closed


class AddressBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def check_address(self, ip, allowed):
        url = f'http://[{ip}]/' if ':' in ip else f'http://{ip}/'
        rows = [dict(hostname='source.example', host=ip, port=80, family=socket.AF_INET6 if ':' in ip else socket.AF_INET, proto=0, flags=0)]
        delegate = SimpleNamespace(resolve=AsyncMock(return_value=rows))
        row = (rows[0]['family'], socket.SOCK_STREAM, 6, '', (ip, 80))
        proxy = PublicBrowserProxy()
        with patch.object(asyncio.get_running_loop(), 'getaddrinfo', AsyncMock(return_value=[row])), patch.object(proxy, '_open_address', AsyncMock(return_value=('r', 'w'))) as connect:
            if allowed:
                self.assertEqual(net.public_url(url), url)
                self.assertEqual(await net.PublicResolver(delegate).resolve('source.example'), rows)
                self.assertEqual(await proxy._connect('source.example', 80), ('r', 'w'))
                connect.assert_awaited_once_with(row)
            else:
                with self.assertRaises(ValueError): net.public_url(url)
                with self.assertRaises(OSError): await net.PublicResolver(delegate).resolve('source.example')
                with self.assertRaises(ValueError): await proxy._connect('source.example', 80)
                connect.assert_not_called()

    async def test_nat64_nonpublic_embedded_addresses_rejected_everywhere(self):
        for ip in ('64:ff9b::127.0.0.1', '64:ff9b::10.0.0.1', '64:ff9b::169.254.169.254',
                   '64:ff9b::192.168.1.1', '64:ff9b::172.16.0.1', '64:ff9b::100.64.0.1',
                   '64:ff9b::0.0.0.0', '64:ff9b::192.0.2.1'):
            with self.subTest(ip=ip): await self.check_address(ip, False)

    async def test_normal_public_and_nat64_public_addresses_remain_usable(self):
        for ip in ('8.8.8.8', '2606:4700:4700::1111', '64:ff9b::8.8.8.8', '64:ff9b::1.1.1.1', '::ffff:8.8.8.8'):
            with self.subTest(ip=ip): await self.check_address(ip, True)

    async def test_existing_internal_mapped_and_local_translation_blocks_remain(self):
        for ip in ('127.0.0.1', '10.0.0.1', '::1', '::ffff:127.0.0.1', '64:ff9b:1::808:808'):
            with self.subTest(ip=ip): await self.check_address(ip, False)

    async def test_mixed_nat64_dns_rejects_whole_answer(self):
        delegate = SimpleNamespace(resolve=AsyncMock(return_value=[{'host':'8.8.8.8'}, {'host':'64:ff9b::127.0.0.1'}]))
        with self.assertRaises(OSError): await net.PublicResolver(delegate).resolve('source.example')


class HTTPBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def exchange(self, body=b'', headers=b'', method=b'GET', response=None, pipeline=b'', response_eof=False):
        proxy = PublicBrowserProxy()
        request = method + b' http://source.example/a?q=1 HTTP/1.1\r\nHost: source.example\r\nProxy-Authorization: ' + proxy.authorization + b'\r\n' + headers + b'\r\n' + body + pipeline
        reader = asyncio.StreamReader(); reader.feed_data(request)
        # Remain open as browsers normally do; a correct implementation needs
        # neither the browser nor the origin to close to find message boundaries.
        remote = asyncio.StreamReader()
        remote.feed_data(response if response is not None else b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok')
        if response_eof: remote.feed_eof()
        downstream = Writer(); upstream = Writer()
        with patch.object(proxy, '_connect', AsyncMock(return_value=(remote, upstream))) as connect:
            await asyncio.wait_for(proxy._handle(reader, downstream), 5)
            calls = connect.await_count
        self.assertTrue(downstream.closed)
        self.assertTrue(upstream.closed or not calls)
        return bytes(upstream.data), bytes(downstream.data), calls

    async def test_pipelined_second_request_never_leaves_proxy(self):
        second = b'GET http://second.example/UNEXPECTED HTTP/1.1\r\nHost: second.example\r\n\r\n'
        up, down, calls = await self.exchange(pipeline=second)
        self.assertNotIn(b'UNEXPECTED', up)
        self.assertEqual(calls, 1)
        self.assertIn(b'GET /a?q=1 HTTP/1.1', up)
        self.assertNotIn(b'proxy-authorization', up.lower())
        self.assertTrue(down.endswith(b'ok'))

    async def test_content_length_post_body_preserved_without_next_request(self):
        payload = b'GET /looks-like-request HTTP/1.1\r\n\r\nDATA'
        up, down, _ = await self.exchange(method=b'POST', headers=b'Content-Length: '+str(len(payload)).encode()+b'\r\n', body=payload,
                                         pipeline=b'GET http://second.example/EXTRA HTTP/1.1\r\nHost: second.example\r\n\r\n')
        self.assertTrue(up.endswith(payload)); self.assertNotIn(b'EXTRA', up)
        self.assertTrue(down.endswith(b'ok'))

    async def test_chunked_post_trailers_and_binary_body_preserved(self):
        up, down, _ = await self.exchange(method=b'POST', headers=b'Transfer-Encoding: chunked\r\n',
            body=b'4\r\n\x00\xffab\r\n0\r\nX-Checksum: yes\r\n\r\n', pipeline=b'UNEXPECTED')
        self.assertIn(b'\x00\xffab', up); self.assertIn(b'X-Checksum: yes'.lower(), up.lower())
        self.assertNotIn(b'UNEXPECTED', up); self.assertTrue(down.endswith(b'ok'))

    async def test_fixed_length_response_ends_without_origin_eof(self):
        _, down, _ = await self.exchange(response=b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: keep-alive\r\n\r\nokHTTP/1.1 200 UNEXPECTED\r\n\r\n')
        self.assertNotIn(b'UNEXPECTED', down); self.assertIn(b'connection: close', down.lower())

    async def test_chunked_download_and_trailers_end_without_origin_eof(self):
        _, down, _ = await self.exchange(response=b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n4\r\ndata\r\n0\r\nX-Checksum: yes\r\n\r\nUNEXPECTED')
        self.assertIn(b'data', down); self.assertIn(b'x-checksum: yes', down.lower()); self.assertNotIn(b'UNEXPECTED', down)

    async def test_close_delimited_response_preserves_body(self):
        _, down, _ = await self.exchange(response=b'HTTP/1.0 200 OK\r\n\r\nbody', response_eof=True)
        self.assertIn(b'body', down)

    async def test_head_and_bodyless_statuses_finish(self):
        for method, response in ((b'HEAD', b'HTTP/1.1 200 OK\r\nContent-Length: 900\r\n\r\n'),
                                 (b'GET', b'HTTP/1.1 204 No Content\r\n\r\n'),
                                 (b'GET', b'HTTP/1.1 304 Not Modified\r\n\r\n')):
            with self.subTest(method=method,response=response):
                _, down, _ = await self.exchange(method=method,response=response)
                self.assertIn(response.split(b'\r\n')[0], down)

    async def test_redirect_response_preserved_for_browser_to_follow(self):
        _, down, _ = await self.exchange(response=b'HTTP/1.1 302 Found\r\nLocation: https://next.example/\r\nContent-Length: 0\r\n\r\n')
        self.assertIn(b'location: https://next.example/', down.lower())

    async def test_large_download_stream_not_truncated(self):
        payload = bytes(range(256))*8192
        _, down, _ = await self.exchange(response=b'HTTP/1.1 200 OK\r\nContent-Length: '+str(len(payload)).encode()+b'\r\n\r\n'+payload)
        self.assertEqual(down.split(b'\r\n\r\n',1)[1],payload)

    async def test_expect_continue_and_informational_response(self):
        up, down, _ = await self.exchange(method=b'POST',headers=b'Expect: 100-continue\r\nContent-Length: 4\r\n', body=b'data',
            response=b'HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 103 Early Hints\r\nLink: </x>\r\n\r\nHTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok')
        self.assertEqual(down.count(b'HTTP/1.1 100 '),1); self.assertIn(b'103 Early Hints',down)
        self.assertTrue(up.endswith(b'data')); self.assertTrue(down.endswith(b'ok'))

    async def test_ambiguous_request_and_upgrade_rejected_before_connect(self):
        for headers in (b'Content-Length: 2\r\nTransfer-Encoding: chunked\r\n',
                        b'Content-Length: 2\r\nContent-Length: 3\r\n',
                        b'Upgrade: websocket\r\nConnection: upgrade\r\n',
                        b'Connection: content-length\r\nContent-Length: 2\r\n'):
            with self.subTest(headers=headers):
                up, down, calls = await self.exchange(headers=headers)
                self.assertEqual(calls,0); self.assertEqual(up,b''); self.assertIn(b'502',down)

    async def test_incomplete_response_is_not_reported_complete(self):
        _, down, _ = await self.exchange(response=b'HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\nx', response_eof=True)
        self.assertEqual(down.split(b'\r\n\r\n',1)[1],b'x')
        self.assertIn(b'content-length: 10',down.lower())  # Client detects truncation.

    async def test_early_final_response_does_not_wait_for_upload(self):
        _, down, _ = await self.exchange(method=b'POST',headers=b'Content-Length: 1000\r\n',
            response=b'HTTP/1.1 413 Too Large\r\nContent-Length: 0\r\n\r\n')
        self.assertIn(b'413 Too Large',down)

    async def test_connect_tunnel_remains_byte_transparent(self):
        proxy=PublicBrowserProxy(); reader=asyncio.StreamReader(); remote=asyncio.StreamReader()
        reader.feed_data(b'CONNECT source.example:443 HTTP/1.1\r\nProxy-Authorization: '+proxy.authorization+b'\r\n\r\n\x16\x03\x01tls');reader.feed_eof()
        remote.feed_data(b'\x16\x03\x03reply')
        upstream=Writer(); downstream=Writer()
        with patch.object(proxy,'_connect',AsyncMock(return_value=(remote,upstream))) as connect:
            await asyncio.wait_for(proxy._handle(reader,downstream),5)
        connect.assert_awaited_once_with('source.example',443)
        self.assertEqual(bytes(upstream.data),b'\x16\x03\x01tls')
        self.assertIn(b'200 Connection Established',downstream.data)

    async def test_browser_disconnect_cancels_slow_origin_after_upload(self):
        proxy=PublicBrowserProxy(); reader=asyncio.StreamReader(); remote=asyncio.StreamReader()
        reader.feed_data(b'GET http://source.example/ HTTP/1.1\r\nHost: source.example\r\nProxy-Authorization: '+proxy.authorization+b'\r\n\r\n')
        upstream=Writer();downstream=Writer()
        with patch.object(proxy,'_connect',AsyncMock(return_value=(remote,upstream))):
            task=asyncio.create_task(proxy._handle(reader,downstream))
            for _ in range(100):
                if upstream.data:break
                await asyncio.sleep(0)
            self.assertTrue(upstream.data)
            reader.feed_eof()
            await asyncio.wait_for(task,5)
        self.assertTrue(upstream.closed);self.assertTrue(downstream.closed)
        self.assertFalse(proxy.tasks);self.assertFalse(proxy.writers)
