import asyncio
import errno
import os
import socket
import unittest
from unittest.mock import Mock

from services.windows_transport import _ClosingSocket, install_reset_safe_close


class SocketCloseTests(unittest.TestCase):
    def test_only_known_reset_is_suppressed(self):
        raw = Mock()
        wrapped = _ClosingSocket(raw)
        raw.shutdown.side_effect = ConnectionResetError(errno.ECONNRESET, 'reset')
        wrapped.shutdown(socket.SHUT_RDWR)
        for error in (OSError(5, 'denied'), ConnectionResetError(12345, 'unknown')):
            raw.shutdown.side_effect = error
            with self.assertRaises(type(error)):
                wrapped.shutdown(socket.SHUT_RDWR)
        raw.close.side_effect = OSError(5, 'close failed')
        with self.assertRaises(OSError):
            wrapped.close()


@unittest.skipUnless(os.name == 'nt', 'Windows Proactor regression')
class ProactorCloseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from asyncio.proactor_events import _ProactorBasePipeTransport
        self.transport_class = _ProactorBasePipeTransport
        self.original = self.transport_class._call_connection_lost

    async def asyncTearDown(self):
        self.transport_class._call_connection_lost = self.original

    async def scenario(self, fixed, reset=True, protocol_error=False, pending_read=False):
        from asyncio.base_events import Server
        class ClosingSocket(socket.socket):
            def shutdown(self, how):
                if reset:
                    raise ConnectionResetError(10054, 'injected reset')
        if fixed:
            install_reset_safe_close()
        server = Server(asyncio.get_running_loop(), [], None, None, 100, None)
        transport = object.__new__(self.transport_class)
        transport._called_connection_lost = False
        transport._protocol = Mock()
        if protocol_error:
            transport._protocol.connection_lost.side_effect = ValueError('protocol error')
        raw = ClosingSocket()
        transport._sock = raw
        transport._server = server
        transport._closing = True
        transport._read_fut = transport._write_fut = None
        if pending_read:
            transport._read_fut = Mock()
        transport._data = bytearray(65536)
        exported_buffer = memoryview(transport._data)
        server._attach(transport)
        server.close()
        try:
            if protocol_error:
                with self.assertRaises(ValueError):
                    transport._call_connection_lost(None)
            elif not fixed and reset:
                with self.assertRaises(ConnectionResetError):
                    transport._call_connection_lost(None)
            else:
                transport._call_connection_lost(None)
                transport._call_connection_lost(None)  # callback remains idempotent
            if fixed:
                await asyncio.wait_for(server.wait_closed(), 0.5)
                self.assertEqual(raw.fileno(), -1)
                self.assertEqual(len(transport._data), 65536 if pending_read else 0)
                self.assertEqual(len(exported_buffer), 65536)
                transport._protocol.connection_lost.assert_called_once_with(None)
            else:
                with self.assertRaises(TimeoutError):
                    await asyncio.wait_for(server.wait_closed(), 0.05)
        finally:
            exported_buffer.release()
            raw.close()
            transport._sock = None
            if transport._server is not None:
                server._detach(transport)
            transport._server = None

    async def test_original_reset_reproduces_endless_wait(self):
        await self.scenario(False)

    async def test_reset_cleanup_releases_server(self):
        await self.scenario(True)

    async def test_normal_close_unchanged(self):
        await self.scenario(True, reset=False)

    async def test_pending_read_buffer_is_preserved(self):
        await self.scenario(True, pending_read=True)

    async def test_protocol_error_still_propagates_after_cleanup(self):
        await self.scenario(True, protocol_error=True)

    async def test_install_idempotent(self):
        install_reset_safe_close()
        once = self.transport_class._call_connection_lost
        install_reset_safe_close()
        self.assertIs(once, self.transport_class._call_connection_lost)

    async def test_real_transfer_then_abort_releases_closed_buffers(self):
        install_reset_safe_close()
        loop = asyncio.get_running_loop()
        accepted = loop.create_future()

        class Echo(asyncio.Protocol):
            def connection_made(self, transport):
                self.transport = transport
                self.closed = loop.create_future()
                accepted.set_result(self)

            def data_received(self, data):
                self.transport.write(data)

            def connection_lost(self, exc):
                self.closed.set_result(exc)

        server = await loop.create_server(Echo, '127.0.0.1', 0)
        writer = None
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(
                '127.0.0.1', server.sockets[0].getsockname()[1]), 5)
            peer = await asyncio.wait_for(accepted, 5)
            payload = bytes(range(256)) * 1024
            writer.write(payload)
            await asyncio.wait_for(writer.drain(), 5)
            self.assertEqual(await asyncio.wait_for(reader.readexactly(len(payload)), 5), payload)
            pending = asyncio.create_task(reader.read(1))
            await asyncio.sleep(0)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            writer.transport.abort()
            await asyncio.wait_for(writer.wait_closed(), 5)
            await asyncio.wait_for(peer.closed, 5)
            await asyncio.sleep(0)
            self.assertEqual(writer.transport._data, b'')
            self.assertEqual(peer.transport._data, b'')
        finally:
            if writer is not None:
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), 5)
            if accepted.done() and not accepted.cancelled():
                accepted.result().transport.close()
            server.close()
            await asyncio.wait_for(server.wait_closed(), 5)
