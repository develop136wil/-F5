"""Narrow CPython Proactor socket-close compatibility for Windows.

Some runtimes let an already-reset socket's shutdown() abort transport cleanup
before Server._detach(), leaving Server.wait_closed() waiting forever. Keep the
runtime's original callback and suppress only reset from socket.shutdown itself;
protocol callbacks, socket.close errors, and all other errors still propagate.
Remove this private-API shim when the bundled runtime fixes this path and passes
the same regression tests. No active requests or database workers are cancelled.
"""
import errno
import functools
import os
import socket


class _ClosingSocket:
    def __init__(self, sock):
        self.sock = sock

    def __getattr__(self, name):
        return getattr(self.sock, name)

    def shutdown(self, how):
        try:
            return self.sock.shutdown(how)
        except ConnectionResetError as error:
            if error.errno not in (errno.ECONNRESET, 10054) and getattr(error, 'winerror', None) != 10054:
                raise
            # The peer is already gone. Still let the original callback close
            # the socket and detach this one transport from its owning server.


def install_reset_safe_close():
    if os.name != 'nt':
        return False
    from asyncio.proactor_events import _ProactorBasePipeTransport
    original = _ProactorBasePipeTransport._call_connection_lost
    if getattr(original, '_welfare_reset_safe', False):
        return True

    @functools.wraps(original)
    def reset_safe_close(self, exc):
        sock = self._sock
        if not isinstance(sock, socket.socket):
            return original(self, exc)
        proxy = _ClosingSocket(sock)
        self._sock = proxy
        try:
            return original(self, exc)
        finally:
            if self._sock is proxy:
                self._sock = sock
            # Closed transports can remain in protocol reference cycles until GC.
            # Drop only our receive-buffer reference after successful cleanup and
            # with no registered read. Never resize memory exported to overlapped I/O.
            if (self._sock is None and self._called_connection_lost
                    and getattr(self, '_read_fut', None) is None
                    and isinstance(getattr(self, '_data', None), bytearray)):
                self._data = b''

    reset_safe_close._welfare_reset_safe = True
    _ProactorBasePipeTransport._call_connection_lost = reset_safe_close
    return True
