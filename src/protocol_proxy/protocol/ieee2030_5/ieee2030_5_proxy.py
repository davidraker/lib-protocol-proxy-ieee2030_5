"""The IEEE 2030.5 protocol proxy: a process acting as the 2030.5 client (EndDevice) towards any number of servers,
and serving 2030.5 servers to DER clients, in either role as the registration says.

Everything about a remote arrives in REGISTER_REMOTE (or its older name REGISTER_SERVER), so the proxy takes no launch
options. ``role`` is ``client`` (the default) to reach a server, or ``server`` to serve one. The registering caller's
remote id (header version 2) identifies the remote in every later message; the caller and the proxy are always
installed together, so there is no payload fallback. Reads and writes are keyed by the full point topics the caller
registered and mean the same in both roles (what the proxy holds for the point; store this value). Values the other
side produced (control changes from a server, a DER client's reports and responses to a served server) are pushed to
the manager as RECEIVE_CONTROLS, tagged with the remote id and keyed the same way; a served server also pushes
everything it holds when it is (re)registered.
"""
import asyncio
import json
import logging
from functools import partial
from typing import Any
from uuid import UUID

from protocol_proxy.ipc import callback, ProtocolProxyMessage
from protocol_proxy.proxy.asyncio import AsyncioProtocolProxy

from .client import SERVER_FIELDS, ServerClient
from .json import serialize
from .server import ServedSep2Server

_log = logging.getLogger(__name__)

ROLES = {'client': 'client', 'master': 'client', 'server': 'server', 'outstation': 'server'}
SERVED_FIELDS = ('cert_path', 'key_path', 'ca_path', 'tls', 'client_auth', 'tls12_only', 'ciphers', 'poll_rate', 'primacy',
                 'client_lfdi', 'client_pin', 'register_unknown_clients', 'immediate_control_duration', 'state_dir',
                 'notify_tls_verify', 'response_timeout', 'description')
Remote = ServerClient | ServedSep2Server


class Ieee2030_5Proxy(AsyncioProtocolProxy):
    """One proxy process serves many servers; each (server, certificate) pair has its own session."""

    DEFAULT_CALLBACK_TIMEOUT = 120.0

    def __init__(self, callback_timeout: float = DEFAULT_CALLBACK_TIMEOUT, **kwargs):
        super(Ieee2030_5Proxy, self).__init__(**kwargs)
        self.clients: dict[str, ServerClient] = {}            # servers reached, by (url, identity)
        self.servers: dict[str, ServedSep2Server] = {}        # servers served, by listener
        self.remotes: dict[UUID, Remote] = {}                 # either, by the caller's remote id (header version 2)
        self.loop = asyncio.get_event_loop()
        for name in ('REGISTER_REMOTE', 'REGISTER_SERVER'):
            self.register_callback(self.register_server_endpoint, name, provides_response=True, timeout=callback_timeout)
        self.register_callback(self.read_resources_endpoint, 'READ_RESOURCES', provides_response=True,
                               timeout=callback_timeout)
        self.register_callback(self.write_resources_endpoint, 'WRITE_RESOURCES', provides_response=True,
                               timeout=callback_timeout)
        for name in ('CLOSE_REMOTE', 'CLOSE_SERVER'):
            self.register_callback(self.close_server_endpoint, name, provides_response=True, timeout=callback_timeout)
        self.register_callback(self.describe_server_endpoint, 'DESCRIBE_SERVER', provides_response=True,
                               timeout=callback_timeout)

    # ---- helpers ------------------------------------------------------------------------------------------------
    @staticmethod
    def client_key(message: dict) -> str:
        return f"{message.get('server_url')}|{message.get('lfdi') or message.get('cert_path')}"

    @staticmethod
    def server_key(message: dict) -> str:
        return f"server|{message.get('bind_host', '0.0.0.0')}:{message.get('port', 8443)}"

    def _get_client(self, headers, message: dict, action: str) -> Remote | None:
        """The remote (a server reached, or a server served) a request concerns, by the remote id in its header."""
        remote_id = getattr(headers, 'remote_id', None)
        client = self.remotes.get(remote_id) if remote_id else None
        if client is None:
            _log.warning(f'Failed to {action}: remote {remote_id} has no registered server.')
        return client

    @staticmethod
    def _decode(raw_message: bytes) -> dict | None:
        try:
            message = json.loads(raw_message.decode('utf8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            _log.warning(f'Received undecodable request: {e}')
            return None
        return message if isinstance(message, dict) else None

    async def _push(self, client: Remote, values: dict[str, Any]):
        """Send values the other side produced to the manager, fire-and-forget, keyed by the registered topics and
        tagged with the remote's id so the manager routes them to the caller that registered it."""
        peer = self.peers.get(self.manager)
        if peer is None:    # pragma: no cover - the manager peer is created by the base constructor
            return
        await self.send(peer, ProtocolProxyMessage(method_name='RECEIVE_CONTROLS', payload=serialize(values),
                                                   remote_id=client.remote_id))

    @staticmethod
    def _remote_id(headers) -> UUID | None:
        """The caller's id for the server, from the header."""
        return getattr(headers, 'remote_id', None)

    # ---- endpoints ----------------------------------------------------------------------------------------------
    @callback
    async def register_server_endpoint(self, headers, raw_message: bytes):
        """Create or re-register the client for a server and replace its point table.

        Payload: the server fields (``server_url``, ``cert_path``, ``key_path``, ``ca_path``, ``lfdi``, ``pin``,
        ``subscribe``, ``notify_host``, ...) and ``points``: a list of ``{topic, path, writable, multiplier, scaling,
        starting_value, reading}``. Reply: ``{'client': key, 'points': n, 'lfdi': ..., 'sfdi': ...}``. The session
        phases run in the background; ``wait`` (seconds) makes the reply wait for them.
        """
        message = self._decode(raw_message)
        if message is None:
            return serialize({}, {'server': 'Undecodable registration.'})
        role = ROLES.get(str(message.get('role') or 'client').lower())
        if role is None:
            return serialize({}, {'server': f"Unknown role {message.get('role')!r}; expected client or server."})
        remote_id = self._remote_id(headers)
        if remote_id is None:
            return serialize({}, {'server': 'Registration needs the remote id in its header (version 2).'})
        if role == 'server':
            return await self._register_served(remote_id, message)
        if not message.get('server_url'):
            return serialize({}, {'server': 'Registering a server to reach needs at least a server_url.'})
        key = self.client_key(message)
        try:
            client = self.clients.get(key)
            fresh = client is None
            if fresh:
                settings = {k: message[k] for k in SERVER_FIELDS if k in message and k != 'server_url'}
                client = ServerClient(message['server_url'], **settings)
                client.push = partial(self._push, client)
            client.remote_id = remote_id
            count = client.configure_points(message.get('points') or [])
            self.clients[key] = client
            self.remotes[remote_id] = client
            if fresh:
                client.start()
            else:
                await client.restart()
            wait = message.get('wait')
            if wait:
                await client.wait_ready(float(wait))
            return serialize({'client': key, 'points': count, 'lfdi': client.lfdi, 'sfdi': client.session.sfdi,
                              'ready': client.session.ready, 'start_error': client.start_error})
        except (KeyError, TypeError, ValueError) as e:
            _log.warning(f'Failed to register server {key}: {e}')
            return serialize({}, {'server': str(e)})

    async def _register_served(self, remote_id: UUID, message: dict):
        """Serve a 2030.5 server on ``bind_host:port`` (role ``server``). Payload: the listener fields (``cert_path``,
        ``key_path``, ``ca_path``, ``tls``, ``client_auth``, ``poll_rate``, ``primacy``, ``client_lfdi``, ``client_pin``,
        ``register_unknown_clients``, ``immediate_control_duration``, ``state_dir``...), ``points`` and optionally
        ``values`` (the caller's current values, applied to served points that hold nothing). Reply: ``{'server': key,
        'points': n, 'role': 'server', 'seeded': m, 'port': bound port}``; everything held is pushed as well."""
        key = self.server_key(message)
        served = self.servers.get(key)
        try:
            if served is None:
                settings = {k: message[k] for k in SERVED_FIELDS if k in message and message[k] is not None}
                served = ServedSep2Server(message.get('bind_host', '0.0.0.0'), int(message.get('port', 8443)), **settings)
                served.push = partial(self._push, served)
            served.remote_id = remote_id
            count = served.configure_points(message.get('points') or [])
            await served.start()
            await served.seed(message.get('values') or {})
        except Exception as e:      # a bad row, a bad certificate, or whatever the listener's bind raised
            _log.warning(f'Failed to serve 2030.5 server {key}: {e}')
            if served is not None and key not in self.servers:
                await served.close()
            return serialize({}, {'server': str(e)})
        self.servers[key] = served
        self.remotes[remote_id] = served
        self.loop.create_task(served.push_all())
        return serialize({'server': key, 'points': count, 'role': 'server', 'seeded': served.seeded, 'port': served.bound_port})

    @callback
    async def read_resources_endpoint(self, headers, raw_message: bytes):
        """Reply with the values of ``topics`` (controls from the engine's view, upward rows from the shadows).
        ``refresh`` fetches the controls first."""
        message = self._decode(raw_message)
        if message is None or (client := self._get_client(headers, message, 'read resources')) is None:
            return serialize({}, {'server': 'Server not registered.'})
        results, errors = await client.read(message.get('topics'), refresh=bool(message.get('refresh')))
        return serialize(results, errors)

    @callback
    async def write_resources_endpoint(self, headers, raw_message: bytes):
        """PUT or POST the upward resources touched by ``values`` (``{topic: value}``)."""
        message = self._decode(raw_message)
        if message is None or (client := self._get_client(headers, message, 'write resources')) is None:
            return serialize({}, {'server': 'Server not registered.'})
        results, errors = await client.write(message.get('values') or {})
        return serialize(results, errors)

    @callback
    async def describe_server_endpoint(self, headers, raw_message: bytes):
        message = self._decode(raw_message)
        if message is None or (client := self._get_client(headers, message, 'describe server')) is None:
            return serialize({}, {'server': 'Server not registered.'})
        return serialize(client.describe())

    @callback
    async def close_server_endpoint(self, headers, raw_message: bytes):
        message = self._decode(raw_message)
        if message is None:
            return serialize({}, {'server': 'Undecodable request.'})
        client = self._get_client(headers, message, 'close server')
        if client is not None:
            self.clients = {k: c for k, c in self.clients.items() if c is not client}
            self.servers = {k: c for k, c in self.servers.items() if c is not client}
            self.remotes = {k: c for k, c in self.remotes.items() if c is not client}
            await client.close()
        return serialize({'closed': client is not None})

    @classmethod
    def get_unique_remote_id(cls, unique_remote_id: tuple) -> tuple:
        """Identify the proxy process a caller wants; all servers share one unless the caller groups them."""
        return tuple(unique_remote_id)
