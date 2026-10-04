"""One 2030.5 server as the proxy sees it: the session, the control engine, the writer and the subscriptions."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .controls import ControlEngine
from .identity import lfdi_from_cert, normalize_lfdi
from .models import sep
from .notify import NotifyReceiver
from .points import PointSpec
from .session import DeviceSession, SessionConfig, SessionError
from .subscriptions import SubscriptionManager
from .transport import Sep2Http, Sep2HttpError
from .writer import UpwardWriter

_log = logging.getLogger(__name__)

PushCallback = Callable[[dict[str, Any]], Awaitable[None]]

SERVER_FIELDS = ('server_url', 'dcap_path', 'cert_path', 'key_path', 'ca_path', 'tls_verify', 'tls12_only', 'ciphers',
                 'lfdi', 'pin', 'register_if_missing', 'device_category', 'der_index', 'poll_rate_floor',
                 'default_poll_rate', 'poll_rate_ceiling', 'subscribe', 'notify_host', 'notify_port', 'notify_bind_host',
                 'notify_client_auth', 'notify_cert_path', 'notify_key_path', 'response_timeout', 'description')


class ServerClient:
    """Everything the proxy holds for one (server, device certificate) pair."""

    def __init__(self, server_url: str, *, cert_path: str | None = None, key_path: str | None = None,
                 ca_path: str | None = None, tls_verify: bool = True, tls12_only: bool = True, ciphers=None,
                 lfdi: str | None = None, pin: int | None = None, register_if_missing: bool = True,
                 device_category: int | str = 0x400000, der_index: int = 0, dcap_path: str = '/dcap',
                 poll_rate_floor: float = 60.0, default_poll_rate: float = 1800.0, poll_rate_ceiling: float | None = None,
                 subscribe: bool = True,
                 notify_host: str | None = None, notify_port: int = 8443, notify_bind_host: str = '0.0.0.0',
                 notify_client_auth: bool = False, notify_cert_path: str | None = None, notify_key_path: str | None = None,
                 response_timeout: float = 10.0, description: str = 'VOLTTRON',
                 push: PushCallback | None = None, transport: httpx.AsyncBaseTransport | None = None,
                 clock: Callable[[], float] = time.time, retries: int = 3, remote_id=None):
        if lfdi:
            self.lfdi = normalize_lfdi(lfdi)
        elif cert_path:
            self.lfdi = lfdi_from_cert(cert_path)
        else:
            raise ValueError('Either an LFDI or a client certificate is required.')
        self.server_url = server_url
        self.cert_path, self.key_path, self.ca_path = cert_path, key_path, ca_path
        self.subscribe = bool(subscribe)
        self.notify_host, self.notify_port, self.notify_bind_host = notify_host, int(notify_port), notify_bind_host
        self.notify_client_auth = bool(notify_client_auth)
        # The receiver's TLS identity: a server-style certificate when the device certificate's CSIP extensions make a
        # strict verifier (Go's x509) reject it as a server certificate; the device certificate otherwise.
        self.notify_cert_path, self.notify_key_path = notify_cert_path or cert_path, notify_key_path or key_path
        self.push = push
        # The caller's identifier for this server, carried in pushed messages so the manager routes them to it.
        self.remote_id = remote_id
        if isinstance(device_category, str):
            device_category = int(device_category, 16) if device_category.lower().startswith('0x') else int(device_category)
        from .transport import DEFAULT_CIPHERS
        self.http = Sep2Http(server_url, cert_path=cert_path, key_path=key_path, ca_path=ca_path, tls_verify=tls_verify,
                             tls12_only=tls12_only, ciphers=tuple(ciphers) if ciphers else DEFAULT_CIPHERS,
                             timeout=float(response_timeout), transport=transport, retries=retries)
        self.session = DeviceSession(self.http, SessionConfig(
            lfdi=self.lfdi, dcap_path=dcap_path, pin=None if pin in (None, '') else int(pin),
            register_if_missing=bool(register_if_missing), device_category=int(device_category), der_index=int(der_index),
            poll_rate_floor=float(poll_rate_floor), default_poll_rate=float(default_poll_rate),
            poll_rate_ceiling=None if poll_rate_ceiling in (None, '') else float(poll_rate_ceiling),
            device_description=str(description)), clock=clock)
        self.engine = ControlEngine(self.session, on_change=self._controls_changed)
        self.writer = UpwardWriter(self.session)
        self.receiver: NotifyReceiver | None = None
        self.subscriptions: SubscriptionManager | None = None
        self.points: dict[str, PointSpec] = {}
        self.by_path: dict[str, list[str]] = {}
        self.started = asyncio.Event()
        self.start_error: str | None = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._closing = False

    # ---- configuration ------------------------------------------------------------------------------------------
    def configure_points(self, specs: list[dict]) -> int:
        points = [PointSpec.from_dict(spec, self.lfdi) for spec in specs]
        self.points = {p.topic: p for p in points}
        self.by_path = {}
        for p in points:
            self.by_path.setdefault(p.dotted, []).append(p.topic)
        self.session.readings = [p.reading for p in points if p.reading is not None]
        return len(self.points)

    def describe(self) -> dict:
        return {'server_url': self.server_url, **self.session.describe(), 'points': len(self.points),
                'subscriptions': len(self.subscriptions.subscriptions) if self.subscriptions else 0,
                'notify_url': self.receiver.url if self.receiver else None,
                'notifications': self.receiver.received if self.receiver else None, 'start_error': self.start_error}

    # ---- lifecycle ----------------------------------------------------------------------------------------------
    def start(self):
        """Run the session phases in the background; reads and writes wait for them."""
        if self._task is not None and not self._task.done():
            return
        self.started.clear()
        self.start_error = None
        self._task = asyncio.get_running_loop().create_task(self._start())

    async def _start(self):
        try:
            await self.session.start()
            await self.writer.seed(list(self.points.values()))
            if self.subscribe and self.notify_host:
                await self._start_receiver()
            self.subscriptions = SubscriptionManager(self.session, self.receiver.url if self.receiver else None)
            await self.engine.refresh()
            await self.subscriptions.subscribe_all()
            self.engine.start()
        except (SessionError, Sep2HttpError, OSError) as e:
            self.start_error = str(e)
            _log.error(f'2030.5 session with {self.server_url} failed to start: {e}')
        except Exception as e:                      # pragma: no cover - defensive
            self.start_error = f'{type(e).__name__}: {e}'
            _log.exception(f'2030.5 session with {self.server_url} failed to start')
        finally:
            self.started.set()

    async def _start_receiver(self):
        tls = self.notify_cert_path is not None
        if not tls:
            _log.warning('No certificate: the notification receiver listens over plain HTTP.')
        self.receiver = NotifyReceiver(self._notification, host=self.notify_bind_host, port=self.notify_port,
                                       cert_path=self.notify_cert_path, key_path=self.notify_key_path, ca_path=self.ca_path,
                                       client_auth=self.notify_client_auth, advertised_host=self.notify_host, tls=tls)
        try:
            await self.receiver.start()
        except OSError as e:
            _log.warning(f'Notification receiver could not listen on {self.notify_bind_host}:{self.notify_port} ({e}); '
                         'staying on polling.')
            self.receiver = None

    async def wait_ready(self, timeout: float | None = None) -> bool:
        try:
            await asyncio.wait_for(self.started.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return self.session.ready

    async def restart(self):
        await self.engine.stop()
        if self.subscriptions is not None:
            await self.subscriptions.unsubscribe_all()
        if self.receiver is not None:
            await self.receiver.stop()
            self.receiver = None
        self.engine.last_snapshot = {}
        self.start()

    async def close(self):
        # Stop listening first: deleting the subscriptions makes the server notify us about their removal.
        self._closing = True
        await self.engine.stop()
        if self._task is not None and not self._task.done():
            self._task.cancel()
        if self.receiver is not None:
            await self.receiver.stop()
        if self.subscriptions is not None:
            try:
                await self.subscriptions.unsubscribe_all()
            except Exception as e:
                _log.debug(f'Unsubscribing failed: {e}')
        await self.http.aclose()

    # ---- callbacks ----------------------------------------------------------------------------------------------
    async def _controls_changed(self, changed: dict[str, Any]):
        values = self._topics_for(changed)
        if values and self.push is not None:
            await self.push(values)

    def _topics_for(self, view: dict[str, Any]) -> dict[str, Any]:
        """Convention paths to the topics registered under them, scaled."""
        values: dict[str, Any] = {}
        for path, value in view.items():
            for topic in self.by_path.get(path, ()):
                values[topic] = self.points[topic].engineering(value)
        return values

    async def _notification(self, notification: sep.Notification):
        if self.subscriptions is None or self._closing:
            return
        action, resource = self.subscriptions.classify(notification)
        _log.debug(f'Notification for {resource}: {action}')
        if action == 'resubscribe':
            await self.subscriptions.subscribe_all()
        self.engine.request_refresh(reload_programs=(action == 'reload'))

    # ---- reads and writes ---------------------------------------------------------------------------------------
    async def read(self, topics: list[str] | None, refresh: bool = False) -> tuple[dict, dict]:
        topics = list(topics) if topics is not None else list(self.points)
        results: dict[str, Any] = {}
        errors: dict[str, str] = {}
        if not self.started.is_set() or not self.session.ready:
            reason = self.start_error or 'session not ready'
            return results, {t: reason for t in topics}
        if refresh:
            try:
                await self.engine.refresh()
            except Sep2HttpError as e:
                errors['server'] = str(e)
        view = self.engine.last_snapshot or self.engine.snapshot()
        for topic in topics:
            spec = self.points.get(topic)
            if spec is None:
                errors[topic] = 'unregistered topic'
            elif spec.writable:
                results[topic] = self.writer.current_value(spec)
            elif spec.dotted in view:
                results[topic] = spec.engineering(view[spec.dotted])
            else:
                results[topic] = None
        return results, errors

    async def write(self, values: dict[str, Any]) -> tuple[dict, dict]:
        if not self.started.is_set() or not self.session.ready:
            reason = self.start_error or 'session not ready'
            return {}, {t: reason for t in values}
        async with self._lock:
            return await self.writer.write(values, self.points)
