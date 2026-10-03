"""The IEEE 2030.5 protocol proxy: a process acting as the 2030.5 client (EndDevice) towards any number of servers.

Everything about a server arrives in REGISTER_SERVER, so the proxy takes no launch options. Reads and writes are
keyed by the full point topics the caller registered; control changes (polled, notified or scheduled) are pushed to
the manager as RECEIVE_CONTROLS, keyed the same way.
"""
import asyncio
import json
import logging
from typing import Any

from protocol_proxy.ipc import callback, ProtocolProxyMessage
from protocol_proxy.proxy.asyncio import AsyncioProtocolProxy

from .client import SERVER_FIELDS, ServerClient
from .json import serialize

_log = logging.getLogger(__name__)


class Ieee2030_5Proxy(AsyncioProtocolProxy):
    """One proxy process serves many servers; each (server, certificate) pair has its own session."""

    DEFAULT_CALLBACK_TIMEOUT = 120.0

    def __init__(self, callback_timeout: float = DEFAULT_CALLBACK_TIMEOUT, **kwargs):
        super(Ieee2030_5Proxy, self).__init__(**kwargs)
        self.clients: dict[str, ServerClient] = {}
        self.loop = asyncio.get_event_loop()
        self.register_callback(self.register_server_endpoint, 'REGISTER_SERVER', provides_response=True,
                               timeout=callback_timeout)
        self.register_callback(self.read_resources_endpoint, 'READ_RESOURCES', provides_response=True,
                               timeout=callback_timeout)
        self.register_callback(self.write_resources_endpoint, 'WRITE_RESOURCES', provides_response=True,
                               timeout=callback_timeout)
        self.register_callback(self.close_server_endpoint, 'CLOSE_SERVER', provides_response=True, timeout=callback_timeout)
        self.register_callback(self.describe_server_endpoint, 'DESCRIBE_SERVER', provides_response=True,
                               timeout=callback_timeout)

    # ---- helpers ------------------------------------------------------------------------------------------------
    @staticmethod
    def client_key(message: dict) -> str:
        return f"{message.get('server_url')}|{message.get('lfdi') or message.get('cert_path')}"

    def _get_client(self, message: dict, action: str) -> ServerClient | None:
        client = self.clients.get(self.client_key(message))
        if client is None:
            _log.warning(f'Failed to {action}: server {self.client_key(message)} is not registered.')
        return client

    @staticmethod
    def _decode(raw_message: bytes) -> dict | None:
        try:
            message = json.loads(raw_message.decode('utf8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            _log.warning(f'Received undecodable request: {e}')
            return None
        return message if isinstance(message, dict) else None

    async def _push(self, values: dict[str, Any]):
        """Send changed control values to the manager, fire-and-forget, keyed by the registered topics."""
        peer = self.peers.get(self.manager)
        if peer is None:    # pragma: no cover - the manager peer is created by the base constructor
            return
        await self.send(peer, ProtocolProxyMessage(method_name='RECEIVE_CONTROLS', payload=serialize(values)))

    # ---- endpoints ----------------------------------------------------------------------------------------------
    @callback
    async def register_server_endpoint(self, _, raw_message: bytes):
        """Create or re-register the client for a server and replace its point table.

        Payload: the server fields (``server_url``, ``cert_path``, ``key_path``, ``ca_path``, ``lfdi``, ``pin``,
        ``subscribe``, ``notify_host``, ...) and ``points``: a list of ``{topic, path, writable, multiplier, scaling,
        starting_value, reading}``. Reply: ``{'client': key, 'points': n, 'lfdi': ..., 'sfdi': ...}``. The session
        phases run in the background; ``wait`` (seconds) makes the reply wait for them.
        """
        message = self._decode(raw_message)
        if message is None or not message.get('server_url'):
            return serialize({}, {'server': 'REGISTER_SERVER needs at least a server_url.'})
        key = self.client_key(message)
        try:
            client = self.clients.get(key)
            fresh = client is None
            if fresh:
                settings = {k: message[k] for k in SERVER_FIELDS if k in message and k != 'server_url'}
                client = ServerClient(message['server_url'], push=self._push, **settings)
            count = client.configure_points(message.get('points') or [])
            self.clients[key] = client
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

    @callback
    async def read_resources_endpoint(self, _, raw_message: bytes):
        """Reply with the values of ``topics`` (controls from the engine's view, upward rows from the shadows).
        ``refresh`` fetches the controls first."""
        message = self._decode(raw_message)
        if message is None or (client := self._get_client(message, 'read resources')) is None:
            return serialize({}, {'server': 'Server not registered.'})
        results, errors = await client.read(message.get('topics'), refresh=bool(message.get('refresh')))
        return serialize(results, errors)

    @callback
    async def write_resources_endpoint(self, _, raw_message: bytes):
        """PUT or POST the upward resources touched by ``values`` (``{topic: value}``)."""
        message = self._decode(raw_message)
        if message is None or (client := self._get_client(message, 'write resources')) is None:
            return serialize({}, {'server': 'Server not registered.'})
        results, errors = await client.write(message.get('values') or {})
        return serialize(results, errors)

    @callback
    async def describe_server_endpoint(self, _, raw_message: bytes):
        message = self._decode(raw_message)
        if message is None or (client := self._get_client(message, 'describe server')) is None:
            return serialize({}, {'server': 'Server not registered.'})
        return serialize(client.describe())

    @callback
    async def close_server_endpoint(self, _, raw_message: bytes):
        message = self._decode(raw_message)
        if message is None:
            return serialize({}, {'server': 'Undecodable request.'})
        client = self.clients.pop(self.client_key(message), None)
        if client is not None:
            await client.close()
        return serialize({'closed': client is not None})

    @classmethod
    def get_unique_remote_id(cls, unique_remote_id: tuple) -> tuple:
        """Identify the proxy process a caller wants; all servers share one unless the caller groups them."""
        return tuple(unique_remote_id)
