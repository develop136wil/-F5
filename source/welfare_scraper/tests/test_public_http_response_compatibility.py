"""Ordinary response advertisements must not become collection failures."""
import unittest
from tests import test_public_transport_boundaries as helpers


class ResponseCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    exchange = helpers.HTTPBoundaryTests.exchange

    async def test_upgrade_advertisement_preserves_200_body(self):
        for extra in (b'Upgrade: h2c\r\n', b'Upgrade: h2c\r\nConnection: Upgrade\r\n',
                      b'uPgRaDe: h2c\r\nConnection: uPgRaDe, X-Hop\r\nX-Hop: remove\r\n'):
            with self.subTest(headers=extra):
                _, down, _ = await self.exchange(response=b'HTTP/1.1 200 OK\r\n'+extra+b'Content-Length: 4\r\nContent-Type: text/plain\r\n\r\ndata')
                headers,body=down.split(b'\r\n\r\n',1)
                self.assertEqual(body,b'data')
                self.assertNotIn(b'upgrade',headers.lower())
                self.assertNotIn(b'x-hop',headers.lower())
                self.assertIn(b'content-type: text/plain',headers.lower())

    async def test_upgrade_advertisement_preserves_redirect(self):
        _,down,_=await self.exchange(response=b'HTTP/1.1 301 Moved Permanently\r\nUpgrade: h2c\r\nConnection: Upgrade\r\nLocation: https://next.example/\r\nContent-Length: 0\r\n\r\n')
        self.assertIn(b'301 Moved Permanently',down)
        self.assertIn(b'location: https://next.example/',down.lower())
        self.assertNotIn(b'upgrade',down.lower())

    async def test_upgrade_advertisement_in_early_hints_preserves_final_response(self):
        _,down,_=await self.exchange(response=b'HTTP/1.1 103 Early Hints\r\nUpgrade: h2c\r\nConnection: Upgrade, X-Hop\r\nX-Hop: remove\r\nLink: </style.css>; rel=preload\r\n\r\nHTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok')
        self.assertIn(b'103 Early Hints',down)
        self.assertIn(b'link: </style.css>; rel=preload',down.lower())
        self.assertNotIn(b'upgrade',down.lower()); self.assertNotIn(b'x-hop',down.lower())
        self.assertTrue(down.endswith(b'ok'))

    async def test_transfer_encoding_wins_without_forwarding_content_length_or_tail(self):
        for length in (b'0',b'4',b'99999'):
            with self.subTest(content_length=length):
                _,down,_=await self.exchange(response=b'HTTP/1.1 200 OK\r\nContent-Length: '+length+b'\r\nTransfer-Encoding: chunked\r\nUpgrade: h2c\r\n\r\n4\r\ndata\r\n0\r\nX-End: yes\r\n\r\nHTTP/1.1 200 UNEXPECTED\r\n\r\n')
                headers,body=down.split(b'\r\n\r\n',1)
                self.assertNotIn(b'content-length',headers.lower())
                self.assertIn(b'transfer-encoding: chunked',headers.lower())
                self.assertIn(b'data',body);self.assertIn(b'x-end: yes',body.lower())
                self.assertNotIn(b'UNEXPECTED',down)

    async def test_truncated_chunk_is_not_completed_using_content_length(self):
        _,down,_=await self.exchange(response=b'HTTP/1.1 200 OK\r\nContent-Length: 1\r\nTransfer-Encoding: chunked\r\n\r\n4\r\nx',response_eof=True)
        headers,body=down.split(b'\r\n\r\n',1)
        self.assertNotIn(b'content-length',headers.lower())
        self.assertIn(b'transfer-encoding: chunked',headers.lower())
        self.assertFalse(body.endswith(b'0\r\n\r\n'))

    async def test_protocol_switch_and_unparseable_framing_remain_rejected(self):
        for response in (b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: h2c\r\nConnection: Upgrade\r\n\r\nUNEXPECTED',
                         b'HTTP/1.1 200 OK\r\nTransfer-Encoding: unsupported\r\n\r\nUNEXPECTED',
                         b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Length: 3\r\n\r\nUNEXPECTED'):
            with self.subTest(response=response):
                _,down,_=await self.exchange(response=response,response_eof=True)
                self.assertNotIn(b'UNEXPECTED',down)
                self.assertNotIn(b'101 Switching',down)
                self.assertNotIn(b'200 OK',down)
