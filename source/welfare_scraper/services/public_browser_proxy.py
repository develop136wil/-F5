"""A loopback-only authenticated CONNECT proxy for public-source browsers.

DNS rows are checked and numeric sockets are opened from those same rows.
The browser retains the original hostname and performs TLS verification itself.
Redirects, fetch/XHR and subresources therefore share the connection boundary.
"""
import asyncio
import base64
import hmac
import secrets
import socket
from urllib.parse import urlsplit
from services.public_address import is_public_address
from services.public_http_exchange import PublicHTTPExchange


class PublicBrowserProxy:
    def __init__(self):
        self.server = None
        self.tasks = set()
        self.writers = set()
        self.password = secrets.token_urlsafe(32)
        self.authorization = ('Basic ' + base64.b64encode(('welfare:' + self.password).encode()).decode()).encode()

    async def start(self):
        try:
            self.server = await asyncio.start_server(self._handle, '127.0.0.1', 0, limit=65536)
            return self
        except BaseException:
            self.close()
            raise

    @property
    def options(self):
        port = self.server.sockets[0].getsockname()[1]
        # Chromium normally bypasses proxies for loopback. Remove that implicit
        # bypass so redirects cannot reach the local machine outside this proxy.
        return dict(server=f'http://127.0.0.1:{port}', username='welfare',
                    password=self.password, bypass='<-loopback>')

    def close(self):
        if self.server is not None:
            self.server.close()
        for writer in tuple(self.writers):
            writer.close()
        for task in tuple(self.tasks):
            task.cancel()

    async def _addresses(self, host, port):
        from services.public_transport import public_url
        authority = f'[{host}]' if ':' in host else host
        public_url(f'http://{authority}:{port}/')
        rows = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        if not rows or any(not is_public_address(row[4][0]) for row in rows):
            raise ValueError('Non-public connection')
        return rows

    async def _open_address(self, row):
        family, kind, proto, _, address = row
        sock = socket.socket(family, kind, proto)
        sock.setblocking(False)
        try:
            # address[0] is the validated numeric IP, never the original name.
            await asyncio.get_running_loop().sock_connect(sock, address)
            return await asyncio.open_connection(sock=sock)
        except BaseException:
            sock.close()
            raise

    async def _connect(self, host, port):
        rows = await self._addresses(host, port)
        for row in rows:
            try:
                return await self._open_address(row)
            except OSError:
                continue
        raise OSError('Public connection failed')

    @staticmethod
    async def _copy(reader, writer):
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()

    async def _handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task); self.writers.add(writer)
        upstream = None
        established = False
        try:
            raw = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 15)
            lines = raw[:-4].split(b'\r\n')
            method, target, version = lines[0].decode('ascii').split(' ')
            headers = []
            for line in lines[1:]:
                name, value = line.split(b':', 1)
                headers.append((name.lower(), value.strip(), line))
            credentials = [v for k, v, _ in headers if k == b'proxy-authorization']
            if len(credentials) != 1 or not hmac.compare_digest(credentials[0], self.authorization):
                writer.write(b'HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm="public-source"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
                await writer.drain()
                return
            if method == 'CONNECT':
                exchange = None
                url = urlsplit('https://' + target)
                if url.path or url.query or url.fragment or url.username or url.password:
                    raise ValueError('Invalid tunnel target')
                host, port = url.hostname, url.port or 443
            else:
                from services.public_transport import public_url
                url = urlsplit(public_url(target))
                if url.scheme != 'http':
                    raise ValueError('TLS requires a CONNECT tunnel')
                host, port = url.hostname, url.port or 80
                exchange = PublicHTTPExchange(raw, url)
            if not host or not 1 <= port <= 65535:
                raise ValueError('Invalid host/port')
            remote, upstream = await asyncio.wait_for(self._connect(host, port), 20)
            self.writers.add(upstream)
            if method == 'CONNECT':
                writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                await writer.drain()
            else:
                established = True
                await exchange.run(reader, writer, remote, upstream)
                return
            established = True
            a = asyncio.create_task(self._copy(reader, upstream))
            b = asyncio.create_task(self._copy(remote, writer))
            try:
                await asyncio.wait({a, b}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                a.cancel(); b.cancel()
                await asyncio.gather(a, b, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            if not established and not writer.is_closing():
                writer.write(b'HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
                try: await writer.drain()
                except Exception: pass
        finally:
            for current in (upstream, writer):
                if current is not None:
                    self.writers.discard(current)
                    current.close()
            self.tasks.discard(task)
