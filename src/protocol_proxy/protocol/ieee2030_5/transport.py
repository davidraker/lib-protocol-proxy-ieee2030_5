"""HTTPS transport for IEEE 2030.5: mutual TLS, the ``application/sep+xml`` media type, retries and paging.

TLS follows the standard's requirements: TLS 1.2 with ``TLS_ECDHE_ECDSA_WITH_AES_128_CCM_8`` (OpenSSL name
``ECDHE-ECDSA-AES128-CCM8``) first. The GCM suite is offered second so servers that do not implement CCM-8 (most
general-purpose stacks) still connect; ``tls12_only`` pins the version because a TLS 1.3 handshake ignores the
cipher list and some CCM-8-only servers refuse it.
"""
from __future__ import annotations

import asyncio
import logging
import ssl
from typing import Any, TypeVar

import httpx

from .models import SEP_XML, from_xml, list_items, to_xml

_log = logging.getLogger(__name__)

T = TypeVar('T')

DEFAULT_CIPHERS = ('ECDHE-ECDSA-AES128-CCM8', 'ECDHE-ECDSA-AES128-GCM-SHA256')
RETRYABLE_STATUSES = frozenset({500, 502, 503, 504})
PAGE_SIZE = 50


class Sep2HttpError(Exception):
    """A request that the server answered with an error status, or that never got an answer."""

    def __init__(self, method: str, href: str, status: int | None, detail: str = ''):
        self.method, self.href, self.status, self.detail = method, href, status, detail
        where = f'HTTP {status}' if status is not None else 'no response'
        super().__init__(f'{method} {href}: {where}{(": " + detail) if detail else ""}')


def make_ssl_context(cert_path: str | None, key_path: str | None, ca_path: str | None = None, *,
                     tls_verify: bool = True, tls12_only: bool = True, ciphers=DEFAULT_CIPHERS,
                     server_side: bool = False, client_auth: bool = False) -> ssl.SSLContext:
    """An SSL context for the client (default) or for the notification receiver (``server_side``)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER if server_side else ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    if tls12_only:
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    if ciphers:
        # OpenSSL 3 disables the 64-bit-tag CCM-8 suites at security level 1 even when they are listed, so the
        # mandated suite needs level 0; the certificate checks (CA, hostname) are unaffected by the level.
        spec = ':'.join(ciphers)
        if any('CCM8' in c.upper() for c in ciphers) and '@SECLEVEL' not in spec:
            spec += '@SECLEVEL=0'
        ctx.set_ciphers(spec)
    if cert_path:
        ctx.load_cert_chain(cert_path, key_path or None)
    if server_side:
        if client_auth:
            ctx.verify_mode = ssl.CERT_REQUIRED
            if ca_path:
                ctx.load_verify_locations(ca_path)
        else:
            ctx.verify_mode = ssl.CERT_NONE
        return ctx
    if not tls_verify:
        _log.warning('Server certificate verification is disabled; the server identity is not checked.')
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    elif ca_path:
        ctx.load_verify_locations(ca_path)
    else:
        ctx.load_default_certs()
    return ctx


class Sep2Http:
    """GET, PUT, POST and DELETE of sep objects against one server, with retries and list paging."""

    def __init__(self, base_url: str, *, cert_path: str | None = None, key_path: str | None = None,
                 ca_path: str | None = None, tls_verify: bool = True, tls12_only: bool = True,
                 ciphers=DEFAULT_CIPHERS, timeout: float = 10.0, retries: int = 3, retry_base: float = 0.5,
                 retry_cap: float = 30.0, transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url.rstrip('/')
        self.retries, self.retry_base, self.retry_cap = max(1, int(retries)), retry_base, retry_cap
        if transport is None and base_url.lower().startswith('https'):
            verify: Any = make_ssl_context(cert_path, key_path, ca_path, tls_verify=tls_verify, tls12_only=tls12_only,
                                           ciphers=ciphers)
        else:
            verify = False
        self.client = httpx.AsyncClient(base_url=self.base_url, verify=verify, timeout=timeout, transport=transport,
                                        headers={'Accept': SEP_XML}, follow_redirects=False, trust_env=False)
        self.requests_made = 0

    async def aclose(self):
        await self.client.aclose()

    # ---- raw requests -------------------------------------------------------------------------------------------
    async def request(self, method: str, href: str, content: bytes | None = None, *, accept_statuses=(),
                      params: dict | None = None) -> httpx.Response:
        """Send one request, retrying connection failures and 5xx answers with exponential backoff."""
        headers = {'Content-Type': SEP_XML} if content is not None else {}
        delay = self.retry_base
        last: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                self.requests_made += 1
                response = await self.client.request(method, href, content=content, headers=headers, params=params)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last = e
                _log.debug(f'{method} {href} failed on attempt {attempt}: {e}')
            else:
                if response.status_code < 400 or response.status_code in accept_statuses:
                    return response
                if response.status_code not in RETRYABLE_STATUSES:
                    raise Sep2HttpError(method, href, response.status_code, response.text[:200])
                last = Sep2HttpError(method, href, response.status_code, response.text[:200])
                retry_after = response.headers.get('Retry-After')
                if retry_after and retry_after.isdigit():
                    delay = max(delay, float(retry_after))
            if attempt < self.retries:
                await asyncio.sleep(min(delay, self.retry_cap))
                delay *= 2
        if isinstance(last, Sep2HttpError):
            raise last
        raise Sep2HttpError(method, href, None, str(last)) from last

    # ---- typed helpers ------------------------------------------------------------------------------------------
    async def get(self, href: str, cls: type[T], params: dict | None = None) -> T:
        response = await self.request('GET', href, params=params)
        return from_xml(response.content, cls)

    async def get_optional(self, href: str | None, cls: type[T]) -> T | None:
        """GET a resource that may not exist (``None`` for a missing link or a 404)."""
        if not href:
            return None
        try:
            return await self.get(href, cls)
        except Sep2HttpError as e:
            if e.status == 404:
                return None
            raise

    async def get_all(self, href: str, cls: type[T], page_size: int = PAGE_SIZE) -> list:
        """Every item of a ``*List`` resource, following the ``s``/``l`` paging parameters until ``all`` is reached."""
        items: list = []
        start = 0
        while True:
            page = await self.get(href, cls, params={'s': start, 'l': page_size})
            batch = list_items(page)
            items.extend(batch)
            total = getattr(page, 'all', None)
            if not batch or total is None or len(items) >= total:
                return items
            start += len(batch)

    async def put(self, href: str, obj) -> int:
        response = await self.request('PUT', href, to_xml(obj))
        return response.status_code

    async def post(self, href: str, obj) -> str | None:
        """POST and return the ``Location`` of the created or updated resource (``None`` when the server sends none)."""
        response = await self.request('POST', href, to_xml(obj))
        return response.headers.get('Location')

    async def delete(self, href: str) -> int:
        response = await self.request('DELETE', href, accept_statuses=(404,))
        return response.status_code
