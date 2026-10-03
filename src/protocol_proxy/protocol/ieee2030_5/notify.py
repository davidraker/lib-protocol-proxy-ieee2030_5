"""A minimal HTTPS endpoint for IEEE 2030.5 notifications (``POST /notify`` with ``application/sep+xml``).

The server connects to us, so this is the one place the client listens. The receiver speaks just enough HTTP/1.1 for
notifications: one request per connection (or several with keep-alive), ``Content-Length`` bodies, 204 on success.
Chunked bodies are refused with 411, other paths with 404 and other methods with 405, as the Go receiver does.
"""
from __future__ import annotations

import asyncio
import logging
import ssl
from collections.abc import Awaitable, Callable

from .models import from_xml, sep
from .transport import make_ssl_context

_log = logging.getLogger(__name__)

MAX_HEADER_BYTES = 16 * 1024
MAX_BODY_BYTES = 1024 * 1024
NotificationHandler = Callable[[sep.Notification], Awaitable[None] | None]


class NotifyReceiver:
    def __init__(self, handler: NotificationHandler, *, host: str = '0.0.0.0', port: int = 8443,
                 cert_path: str | None = None, key_path: str | None = None, ca_path: str | None = None,
                 client_auth: bool = False, path: str = '/notify', advertised_host: str | None = None,
                 tls: bool = True, ssl_context: ssl.SSLContext | None = None):
        self.handler = handler
        self.host, self.port, self.path = host, int(port), path
        self.cert_path, self.key_path, self.ca_path, self.client_auth = cert_path, key_path, ca_path, client_auth
        self.advertised_host = advertised_host or (host if host not in ('0.0.0.0', '::') else '127.0.0.1')
        self.tls = tls
        self._ssl_context = ssl_context
        self.server: asyncio.base_events.Server | None = None
        self.received = 0

    @property
    def bound_port(self) -> int:
        if self.server is not None and self.server.sockets:
            return self.server.sockets[0].getsockname()[1]
        return self.port

    @property
    def url(self) -> str:
        scheme = 'https' if self.tls else 'http'
        return f'{scheme}://{self.advertised_host}:{self.bound_port}{self.path}'

    async def start(self):
        ctx = None
        if self.tls:
            ctx = self._ssl_context or make_ssl_context(self.cert_path, self.key_path, self.ca_path, server_side=True,
                                                        client_auth=self.client_auth, tls12_only=False)
        self.server = await asyncio.start_server(self._serve, self.host, self.port, ssl=ctx)
        _log.info(f'Notification receiver listening at {self.url}')

    async def stop(self):
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            while True:
                request = await self._read_request(reader)
                if request is None:
                    break
                method, target, headers, body = request
                status, keep_alive = await self._dispatch(method, target, headers, body)
                writer.write(self._response(status, keep_alive))
                await writer.drain()
                if not keep_alive:
                    break
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError) as e:
            _log.debug(f'Notification connection closed: {e}')
        except Exception:
            _log.exception('Error while serving a notification')
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _read_request(self, reader: asyncio.StreamReader):
        head = await reader.readuntil(b'\r\n\r\n') if not reader.at_eof() else b''
        if not head:
            return None
        if len(head) > MAX_HEADER_BYTES:
            raise ValueError('request header too large')
        lines = head.decode('latin1').split('\r\n')
        parts = lines[0].split(' ')
        if len(parts) < 2:
            raise ValueError(f'bad request line {lines[0]!r}')
        headers = {}
        for line in lines[1:]:
            if ':' in line:
                name, value = line.split(':', 1)
                headers[name.strip().lower()] = value.strip()
        body = b''
        if 'content-length' in headers:
            length = int(headers['content-length'])
            if length > MAX_BODY_BYTES:
                raise ValueError('request body too large')
            body = await reader.readexactly(length)
        return parts[0].upper(), parts[1], headers, body

    async def _dispatch(self, method: str, target: str, headers: dict, body: bytes) -> tuple[int, bool]:
        keep_alive = headers.get('connection', '').lower() != 'close'
        path = target.split('?', 1)[0]
        if path.rstrip('/') != self.path.rstrip('/'):
            return 404, keep_alive
        if method != 'POST':
            return 405, keep_alive
        if 'chunked' in headers.get('transfer-encoding', '').lower():
            return 411, False
        try:
            notification = from_xml(body, sep.Notification)
        except Exception as e:
            _log.warning(f'Undecodable notification: {e}')
            return 400, keep_alive
        self.received += 1
        try:
            result = self.handler(notification)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            _log.exception('Notification handler failed')
            return 500, keep_alive
        return 204, keep_alive

    @staticmethod
    def _response(status: int, keep_alive: bool) -> bytes:
        reasons = {204: 'No Content', 400: 'Bad Request', 404: 'Not Found', 405: 'Method Not Allowed',
                   411: 'Length Required', 500: 'Internal Server Error'}
        connection = 'keep-alive' if keep_alive else 'close'
        extra = '' if status == 204 else 'Content-Length: 0\r\n'
        return f'HTTP/1.1 {status} {reasons.get(status, "")}\r\n{extra}Connection: {connection}\r\n\r\n'.encode('latin1')
