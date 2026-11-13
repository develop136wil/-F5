"""Public-source transport: validate the actual DNS addresses used to connect.

URLs retain their hostname for Host/SNI/certificate verification. No private
address, ambient proxy, TLS bypass or unvalidated redirect is used. Callers may
add source-specific validators; omitting one cannot disable this boundary.
"""
import asyncio
import ipaddress
import socket
from contextlib import asynccontextmanager
from urllib.parse import urlsplit, urljoin
import aiohttp
from aiohttp.abc import AbstractResolver
from services.public_address import is_public_address

_SESSION_TYPE = aiohttp.ClientSession

def public_url(url):
    value = str(url)
    parsed = urlsplit(value)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or
            parsed.username is not None or parsed.password is not None or
            len(value) > 10000 or any(ord(c) < 33 for c in value) or '\\' in value):
        raise ValueError('공개 HTTP(S) 주소만 사용할 수 있습니다.')
    host = parsed.hostname.rstrip('.').lower()
    _ = parsed.port  # Reject malformed ports before any connection.
    if host == 'localhost' or host.endswith(('.localhost', '.local')):
        raise ValueError('내부 주소는 수집하지 않습니다.')
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return value
    if not is_public_address(ip):
        raise ValueError('내부 주소는 수집하지 않습니다.')
    return value

class PublicResolver(AbstractResolver):
    def __init__(self, delegate=None):
        self.delegate = delegate or aiohttp.resolver.ThreadedResolver()

    async def resolve(self, host, port=0, family=socket.AF_INET):
        rows = await self.delegate.resolve(host, port, family)
        if not rows or any(not is_public_address(r['host']) for r in rows):
            raise OSError('내부망 또는 예약 주소로 연결하지 않습니다.')
        # These very rows are consumed by TCPConnector; no second DNS lookup.
        return rows

    async def close(self):
        await self.delegate.close()

@asynccontextmanager
async def public_get(session, url, **kwargs):
    """Existing session interface, with a guarded connector for real I/O.

    Non-aiohttp adapters remain injectable in offline tests. Real sessions can
    never supply a connector that skips the connection-time address check.
    """
    url = public_url(url)
    method = kwargs.pop('method', 'GET')
    if method not in ('GET', 'HEAD', 'POST'):
        raise ValueError('지원하지 않는 수집 요청입니다.')
    redirect = kwargs.pop('allow_redirects', True)
    if any(k in kwargs for k in ('proxy', 'proxy_auth', 'ssl', 'server_hostname')):
        raise ValueError('공개 연결의 인증·주소 검사를 변경할 수 없습니다.')
    owned = None
    if issubclass(type(session), _SESSION_TYPE):
        owned = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(resolver=PublicResolver(), use_dns_cache=False),
            cookie_jar=session.cookie_jar, headers=session.headers,
            timeout=session.timeout, trust_env=False)
        session = owned
    try:
        for _ in range(6):
            url = public_url(url)
            cm = (session.get(url, allow_redirects=False, **kwargs) if method == 'GET'
                  else session.request(method, url, allow_redirects=False, **kwargs))
            response = await cm.__aenter__()
            try:
                if redirect and response.status in (301,302,303,307,308):
                    location = response.headers.get('Location')
                    if not location:
                        raise ValueError('이동 주소가 없습니다.')
                    url = urljoin(url, location)
                    continue
                yield response
                return
            finally:
                await cm.__aexit__(None, None, None)
        raise ValueError('리다이렉트가 너무 많습니다.')
    finally:
        if owned is not None:
            await owned.close()

async def new_public_context(browser, **options):
    """Bind every browser connection, including native redirects, to the proxy."""
    from services.public_browser_proxy import PublicBrowserProxy
    proxy = await PublicBrowserProxy().start()
    options.update(ignore_https_errors=False, service_workers='block',
                   accept_downloads=False, proxy=proxy.options)
    try:
        context = await browser.new_context(**options)
        context.on('close', lambda *_: proxy.close())
        return context
    except BaseException:
        proxy.close()
        raise
