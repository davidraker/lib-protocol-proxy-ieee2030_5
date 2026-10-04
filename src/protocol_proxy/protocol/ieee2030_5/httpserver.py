"""A small HTTP/1.1 server for IEEE 2030.5 resources, the counterpart of :mod:`notify`'s receiver.

It speaks just enough HTTP for CSIP clients: GET, PUT, POST and DELETE with ``Content-Length`` bodies, keep-alive,
``application/sep+xml`` responses and a ``Location`` header for created resources. Chunked request bodies are refused
with 411, as the notification receiver does. The handler decides everything else.
"""
from __future__ import annotations

import asyncio
import logging
import ssl
from collections.abc import Awaitable, Callable
from urllib.parse import parse_qs, urlsplit

from .models.xml import SEP_XML

_log = logging.getLogger(__name__)

MAX_HEADER_BYTES = 16 * 1024
MAX_BODY_BYTES = 4 * 1024 * 1024
REASONS = {200: 'OK', 201: 'Created', 204: 'No Content', 400: 'Bad Request', 403: 'Forbidden', 404: 'Not Found',
           405: 'Method Not Allowed', 411: 'Length Required', 413: 'Payload Too Large', 500: 'Internal Server Error'}

#: ``handler(method, path, query, headers, body) -> (status, body or None, extra headers)``
Handler = Callable[[str, str, dict, dict, bytes], Awaitable[tuple[int, bytes | None, dict]]]


class Sep2HttpServer:
    def __init__(self, handler: Handler, *, host: str = '0.0.0.0', port: int = 0, ssl_context: ssl.SSLContext | None = None):
        self.handler = handler
        self.host, self.port = host, int(port)
        self.ssl_context = ssl_context
        self.server: asyncio.base_events.Server | None = None
        self.requests = 0

    @property
    def bound_port(self) -> int:
        if self.server is not None and self.server.sockets:
            return self.server.sockets[0].getsockname()[1]
        return self.port

    @property
    def is_serving(self) -> bool:
        return self.server is not None and self.server.is_serving()

    async def start(self):
        self.server = await asyncio.start_server(self._serve, self.host, self.port, ssl=self.ssl_context)
        _log.info(f"2030.5 server listening on {'https' if self.ssl_context else 'http'}://{self.host}:{self.bound_port}")

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
                keep_alive = headers.get('connection', '').lower() != 'close'
                if 'chunked' in headers.get('transfer-encoding', '').lower():
                    status, payload, extra, keep_alive = 411, None, {}, False
                else:
                    parts = urlsplit(target)
                    query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
                    try:
                        status, payload, extra = await self.handler(method, parts.path, query, headers, body)
                    except Exception:
                        _log.exception(f'Error handling {method} {parts.path}')
                        status, payload, extra = 500, None, {}
                self.requests += 1
                writer.write(self._response(status, payload, extra, keep_alive))
                await writer.drain()
                if not keep_alive:
                    break
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError, ValueError) as e:
            _log.debug(f'Connection closed: {e}')
        except Exception:
            _log.exception('Error while serving a 2030.5 request')
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

    @staticmethod
    def _response(status: int, payload: bytes | None, extra: dict, keep_alive: bool) -> bytes:
        lines = [f'HTTP/1.1 {status} {REASONS.get(status, "")}']
        if payload:
            lines += [f'Content-Type: {SEP_XML}', f'Content-Length: {len(payload)}']
        elif status != 204:
            lines.append('Content-Length: 0')
        lines += [f'{k}: {v}' for k, v in extra.items()]
        lines.append(f'Connection: {"keep-alive" if keep_alive else "close"}')
        return ('\r\n'.join(lines) + '\r\n\r\n').encode('latin1') + (payload or b'')
