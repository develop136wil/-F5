"""Exactly one framed HTTP exchange per browser proxy connection.

Use h11 (also used by our API server), not a second handwritten HTTP parser.
Do not forward pipelined bytes or wait for the origin to honor Connection: close.
HTTPS CONNECT remains a byte tunnel to its already-validated destination.
"""
import asyncio
import h11


def _headers(headers, *, host=None, response=False):
    names = {name for name, _ in headers}
    if not response and b'content-length' in names and b'transfer-encoding' in names:
        raise ValueError('Ambiguous HTTP body framing')
    nominated = {token.strip().lower() for name, value in headers
                 if name == b'connection' for token in value.split(b',')}
    if not response and (b'upgrade' in names or b'upgrade' in nominated):
        raise ValueError('HTTP protocol upgrade is not a source fetch')
    if nominated & {b'content-length', b'transfer-encoding', b'host'}:
        raise ValueError('Connection header conflicts with message framing')
    removed = nominated | {b'connection', b'proxy-connection', b'proxy-authorization', b'proxy-authenticate'}
    if response:
        # RFC 9110 7.8: advertising Upgrade is not a 101 protocol switch.
        removed.add(b'upgrade')
        if b'transfer-encoding' in names:
            # h11 reads Transfer-Encoding first. RFC 9112 6.3 requires a
            # forwarding intermediary to remove Content-Length in this case.
            removed.add(b'content-length')
    if host is not None:
        removed |= {b'host'}
    result = [(name, value) for name, value in headers if name not in removed]
    if host is not None:
        result.append((b'host', host.encode('ascii')))
    result.append((b'connection', b'close'))
    return result


async def _event(connection, reader):
    while True:
        event = connection.next_event()
        if event is h11.NEED_DATA:
            connection.receive_data(await reader.read(65536))
        elif event is h11.PAUSED:
            raise ValueError('Unexpected second HTTP exchange')
        else:
            return event


class PublicHTTPExchange:
    def __init__(self, raw_headers, url):
        self.client = h11.Connection(h11.SERVER, max_incomplete_event_size=65536)
        self.origin = h11.Connection(h11.CLIENT, max_incomplete_event_size=65536)
        self.client.receive_data(raw_headers)
        self.request = self.client.next_event()
        if not isinstance(self.request, h11.Request):
            raise ValueError('Incomplete HTTP request')
        headers = _headers(self.request.headers, host=url.netloc)
        target = (url.path or '/') + ('?' + url.query if url.query else '')
        self.outgoing = self.origin.send(h11.Request(
            method=self.request.method, target=target.encode('ascii'), headers=headers))

    async def run(self, reader, writer, remote, upstream):
        upstream.write(self.outgoing)
        await upstream.drain()
        continued = self.client.they_are_waiting_for_100_continue
        if continued:
            writer.write(self.client.send(h11.InformationalResponse(status_code=100, headers=[])))
            await writer.drain()

        async def upload():
            while True:
                event = await _event(self.client, reader)
                if not isinstance(event, (h11.Data, h11.EndOfMessage)):
                    raise ValueError('Incomplete HTTP request body')
                upstream.write(self.origin.send(event))
                await upstream.drain()
                if isinstance(event, h11.EndOfMessage):
                    return  # Never consume or forward a second request.

        async def download():
            while True:
                event = await _event(self.origin, remote)
                if isinstance(event, h11.InformationalResponse):
                    if event.status_code == 101:
                        raise ValueError('Unexpected HTTP upgrade')
                    if event.status_code == 100 and continued:
                        continue
                    event = h11.InformationalResponse(status_code=event.status_code,
                        reason=event.reason, headers=_headers(event.headers, response=True))
                elif isinstance(event, h11.Response):
                    event = h11.Response(status_code=event.status_code,
                        reason=event.reason, headers=_headers(event.headers, response=True))
                elif not isinstance(event, (h11.Data, h11.EndOfMessage)):
                    raise ValueError('Incomplete HTTP response')
                writer.write(self.client.send(event))
                await writer.drain()
                if isinstance(event, h11.EndOfMessage):
                    return  # Close even when the origin keeps its socket open.

        sending = asyncio.create_task(upload())
        receiving = asyncio.create_task(download())
        watching = None
        try:
            done, _ = await asyncio.wait({sending, receiving}, return_when=asyncio.FIRST_COMPLETED)
            if sending in done:
                await sending  # Do not hide framing errors in request bodies.
                # Request framing is finished, so no upload reader competes here.
                # Discard subsequent bytes; never relay them to the first origin.
                # Observe browser EOF while a slow origin has not replied yet.
                async def disconnected():
                    while await reader.read(65536):
                        pass
                watching = asyncio.create_task(disconnected())
                done, _ = await asyncio.wait({receiving, watching}, return_when=asyncio.FIRST_COMPLETED)
                if receiving not in done:
                    return
            await receiving
        finally:
            sending.cancel(); receiving.cancel()
            if watching is not None:
                watching.cancel()
            await asyncio.gather(sending, receiving, *([watching] if watching else []), return_exceptions=True)
