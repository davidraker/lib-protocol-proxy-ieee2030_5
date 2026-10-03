"""Every endpoint through the real IPC base class, with the proxy constructed but never registered."""
import json
from uuid import uuid4

import protocol_proxy.protocol.ieee2030_5 as package
from protocol_proxy.protocol.ieee2030_5.ieee2030_5_proxy import Ieee2030_5Proxy
from protocol_proxy.protocol.ieee2030_5.json import serialize
from protocol_proxy.proxy.launch import resolve_launcher

from tests.conftest import POINTS
from tests.fake_server import LFDI, PIN


def make_proxy() -> Ieee2030_5Proxy:
    return Ieee2030_5Proxy(proxy_id=uuid4(), token=uuid4(), manager_address='127.0.0.1', manager_port=1, manager_id=uuid4(),
                           manager_token=uuid4(), registration_retry_delay=0, registration_timeout=0.05)


async def call(proxy, endpoint, message: dict) -> dict:
    reply = await endpoint.__wrapped__(proxy, None, json.dumps(message).encode('utf8'))
    return json.loads(reply)


IDENTITY = {'server_url': 'https://sep2.test:8443', 'lfdi': LFDI}


def test_serialize_shapes():
    body = json.loads(serialize({'a': {'value': 1.5, 'timestamp': None}}, {'b': 'bad'}))
    assert body == {'result': {'a': {'value': 1.5, 'timestamp': None}}, 'error': {'b': 'bad'}}
    assert json.loads(serialize({}))['error'] == {}


async def test_callbacks_registered_and_launch_shape():
    proxy = make_proxy()
    assert {'REGISTER_SERVER', 'READ_RESOURCES', 'WRITE_RESOURCES', 'CLOSE_SERVER', 'DESCRIBE_SERVER'} <= set(proxy.callbacks)
    assert all(len(name) <= 32 for name in proxy.callbacks)
    assert Ieee2030_5Proxy.LAUNCHER is None and Ieee2030_5Proxy.PATCH_GEVENT is False and package.PROXY_CLASS is Ieee2030_5Proxy
    assert Ieee2030_5Proxy.get_unique_remote_id(['ieee2030_5', 'site']) == ('ieee2030_5', 'site')
    assert resolve_launcher('protocol_proxy.protocol.ieee2030_5.ieee2030_5_proxy:Ieee2030_5Proxy') is not None


async def test_register_read_write_close(server, clock, monkeypatch):
    proxy = make_proxy()
    # Route the client's HTTP into the fake server and freeze its clock.
    from protocol_proxy.protocol.ieee2030_5 import client as client_module
    original = client_module.ServerClient.__init__

    def patched(self, *args, **kwargs):
        kwargs.setdefault('transport', server.transport())
        kwargs['clock'] = clock
        kwargs['retries'] = 1
        original(self, *args, **kwargs)
    monkeypatch.setattr(client_module.ServerClient, '__init__', patched)
    pushed = []

    async def push(values):
        pushed.append(values)
    monkeypatch.setattr(proxy, '_push', push)           # no manager process in a unit test

    reply = await call(proxy, proxy.register_server_endpoint, {**IDENTITY, 'pin': PIN, 'subscribe': False,
                                                               'poll_rate_floor': 1, 'points': POINTS, 'wait': 5})
    key = f'{IDENTITY["server_url"]}|{LFDI}'
    assert reply['error'] == {}
    assert reply['result'] == {'client': key, 'points': len(POINTS), 'lfdi': LFDI, 'sfdi': proxy.clients[key].session.sfdi,
                               'ready': True, 'start_error': None}
    now = proxy.clients[key].session.now()
    server.add_control('/derp/1', now - 1, 600, {'opModMaxLimW': 1234}, status=1)
    reply = await call(proxy, proxy.read_resources_endpoint, {**IDENTITY, 'refresh': True,
                                                              'topics': ['der/DERControl/opModMaxLimW', 'der/DERControlList', 'bogus']})
    assert reply["result"]["der/DERControl/opModMaxLimW"] == 1234 and len(reply['result']['der/DERControlList']) == 1
    assert reply['error'] == {'bogus': 'unregistered topic'}

    reply = await call(proxy, proxy.write_resources_endpoint, {**IDENTITY, 'values': {'der/DERSettings/setMaxW': 5000}})
    assert reply == {'result': {'der/DERSettings/setMaxW': {'status': 204, 'resource': 'DERSettings', 'value': 5000}}, 'error': {}}
    assert server.puts['/edev/1/der/1/derg'].setMaxW.value == 5000

    assert pushed and pushed[-1]['der/DERControl/opModMaxLimW'] == 1234      # the refresh pushed RECEIVE_CONTROLS
    reply = await call(proxy, proxy.describe_server_endpoint, IDENTITY)
    assert reply['result']['end_device'] == '/edev/1' and reply['result']['points'] == len(POINTS)

    # Re-registering replaces the point table and restarts the session.
    reply = await call(proxy, proxy.register_server_endpoint, {**IDENTITY, 'points': POINTS[:2], 'wait': 5})
    assert reply['result']['points'] == 2 and reply['result']['ready'] is True

    reply = await call(proxy, proxy.close_server_endpoint, IDENTITY)
    assert reply == {'result': {'closed': True}, 'error': {}} and proxy.clients == {}
    reply = await call(proxy, proxy.read_resources_endpoint, {**IDENTITY, 'topics': ['x']})
    assert reply['error'] == {'server': 'Server not registered.'}


async def test_bad_registrations():
    proxy = make_proxy()
    assert (await call(proxy, proxy.register_server_endpoint, {}))['error'] == {'server': 'REGISTER_SERVER needs at least a server_url.'}
    reply = await call(proxy, proxy.register_server_endpoint, {'server_url': 'https://x'})
    assert 'LFDI or a client certificate' in reply['error']['server']
    reply = await call(proxy, proxy.register_server_endpoint, {**IDENTITY, 'points': [{'topic': 't', 'path': 'DERSettings.nope', 'writable': True}]})
    assert 'no attribute' in reply['error']['server'] and proxy.clients == {}
    reply = await proxy.read_resources_endpoint.__wrapped__(proxy, None, b'not json')
    assert json.loads(reply)['error'] == {'server': 'Server not registered.'}
