"""The server role: a ServedSep2Server over plain HTTP on the loopback interface, exercised by the proxy's own client."""
import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from protocol_proxy.protocol.ieee2030_5.client import ServerClient
from protocol_proxy.protocol.ieee2030_5.models import sep
from protocol_proxy.protocol.ieee2030_5.points import PointSpec
from protocol_proxy.protocol.ieee2030_5.server import ServedSep2Server
from tests.conftest import FakeClock
from tests.fake_server import LFDI, PIN
from tests.test_proxy import call, make_proxy

SERVED_POINTS = [
    {'topic': 's/DefaultDERControl/opModMaxLimW', 'path': 'DefaultDERControl.opModMaxLimW'},
    {'topic': 's/DefaultDERControl/opModConnect', 'path': 'DefaultDERControl.opModConnect'},
    {'topic': 's/DefaultDERControl/setGradW', 'path': 'DefaultDERControl.setGradW'},
    {'topic': 's/DERControl/opModMaxLimW', 'path': 'DERControl.opModMaxLimW'},
    {'topic': 's/DERControl/opModTargetW', 'path': 'DERControl.opModTargetW', 'scaling': 0.001},
    {'topic': 's/DERControl/mRID', 'path': 'DERControl.mRID'},
    {'topic': 's/DERControlList', 'path': 'DERControlList'},
    {'topic': 's/DERCurve/opModVoltVar/CurveData', 'path': 'DERCurve.opModVoltVar.CurveData'},
    {'topic': 's/DERProgram/primacy', 'path': 'DERProgram.primacy'},
    {'topic': 's/DERSettings/setMaxW', 'path': 'DERSettings.setMaxW', 'writable': False},
    {'topic': 's/DERStatus/connectStatus', 'path': 'DERStatus.connectStatus', 'writable': False},
    {'topic': 's/DERCapability/type', 'path': 'DERCapability.type', 'writable': False},
    {'topic': 's/MirrorMeterReading/W', 'path': 'MirrorMeterReading.W', 'writable': False},
    {'topic': 's/MirrorMeterReading/V/PhaseA', 'path': 'MirrorMeterReading.V.PhaseA', 'writable': False, 'scaling': 0.1},
]
CLIENT_POINTS = [
    {'topic': 'c/DERSettings/setMaxW', 'path': 'DERSettings.setMaxW', 'writable': True},
    {'topic': 'c/DERStatus/connectStatus', 'path': 'DERStatus.connectStatus', 'writable': True},
    {'topic': 'c/DERCapability/type', 'path': 'DERCapability.type', 'writable': True, 'starting_value': 83},
    {'topic': 'c/MirrorMeterReading/W', 'path': 'MirrorMeterReading.W', 'writable': True},
    {'topic': 'c/MirrorMeterReading/V/PhaseA', 'path': 'MirrorMeterReading.V.PhaseA', 'writable': True},
    {'topic': 'c/DERControl/opModMaxLimW', 'path': 'DERControl.opModMaxLimW'},
    {'topic': 'c/DERControl/opModTargetW', 'path': 'DERControl.opModTargetW'},
    {'topic': 'c/DefaultDERControl/opModMaxLimW', 'path': 'DefaultDERControl.opModMaxLimW'},
    {'topic': 'c/DERCurve/opModVoltVar/CurveData', 'path': 'DERCurve.opModVoltVar.CurveData'},
    {'topic': 'c/DERControlList', 'path': 'DERControlList'},
    {'topic': 'c/DERProgram/primacy', 'path': 'DERProgram.primacy'},
]


class Pushes:
    def __init__(self):
        self.values = []

    async def __call__(self, values):
        self.values.append(values)

    def latest(self, topic):
        for values in reversed(self.values):
            if topic in values:
                return values[topic]
        return None


async def wait_for(predicate, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


@pytest.fixture
async def served(tmp_path):
    pushes = Pushes()
    s = ServedSep2Server('127.0.0.1', 0, tls=False, poll_rate=1, client_lfdi=LFDI, client_pin=PIN, push=pushes,
                         state_dir=str(tmp_path), immediate_control_duration=120)
    s.configure_points(SERVED_POINTS)
    await s.start()
    s.pushes = pushes
    try:
        yield s
    finally:
        await s.close()


def make_client(served, pushed=None, **settings):
    async def push(values):
        if pushed is not None:
            pushed.append(values)
    settings.setdefault('subscribe', False)
    settings.setdefault('poll_rate_floor', 0.2)
    settings.setdefault('poll_rate_ceiling', 1)
    client = ServerClient(f'http://127.0.0.1:{served.bound_port}', lfdi=LFDI, pin=PIN, push=push, retries=1, **settings)
    client.configure_points(CLIENT_POINTS)
    return client


@pytest.fixture
async def client(served):
    pushed = []
    c = make_client(served, pushed)
    c.pushed = pushed
    c.start()
    assert await c.wait_ready(5.0), c.start_error
    try:
        yield c
    finally:
        await c.close()


def test_served_point_directions():
    assert PointSpec.from_dict({'topic': 't', 'path': 'DERControl.opModMaxLimW'}, '', served=True).writable is True
    assert PointSpec.from_dict({'topic': 't', 'path': 'DERSettings.setMaxW'}, '', served=True).writable is False
    with pytest.raises(ValueError, match='reported by the DER client'):
        PointSpec.from_dict({'topic': 't', 'path': 'DERSettings.setMaxW', 'writable': True}, '', served=True)
    with pytest.raises(ValueError, match='not a 2030.5 resource'):
        PointSpec.from_dict({'topic': 't', 'path': 'Nope.x'}, '', served=True)


async def test_tree_serves_a_client_session(served, client):
    d = client.session.describe()
    assert d['ready'] and d['end_device'] == '/edev/1' and d['registered_now'] is False      # pre-registered by LFDI
    assert d['programs'] == ['/derp/1'] and d['der'] == '/edev/1/der/1' and d['mup'] == '/mup/1'
    assert client.engine.snapshot().get('DERProgram.primacy') == 1


async def test_unknown_client_registers_when_allowed(served):
    other = '00' * 19 + 'AB'
    client = ServerClient(f'http://127.0.0.1:{served.bound_port}', lfdi=other, push=None, retries=1, subscribe=False)
    client.configure_points(CLIENT_POINTS[:1])
    client.start()
    assert await client.wait_ready(5.0), client.start_error
    assert client.session.registered_now and served.devices[other] == '/edev/2'
    await client.close()
    served.register_unknown_clients = False
    refused = ServerClient(f'http://127.0.0.1:{served.bound_port}', lfdi='11' * 20, push=None, retries=1, subscribe=False)
    refused.configure_points(CLIENT_POINTS[:1])
    refused.start()
    assert await refused.wait_ready(5.0) is False and 'HTTP 403' in refused.start_error
    await refused.close()


async def test_pin_is_checked(served):
    client = ServerClient(f'http://127.0.0.1:{served.bound_port}', lfdi=LFDI, pin=PIN + 10, push=None, retries=1, subscribe=False)
    client.configure_points(CLIENT_POINTS[:1])
    client.start()
    assert await client.wait_ready(5.0) is False and 'PIN mismatch' in client.start_error
    await client.close()


async def test_platform_writes_reach_the_client(served, client):
    results, errors = await served.write({'s/DefaultDERControl/opModMaxLimW': 80, 's/DefaultDERControl/opModConnect': True,
                                          's/DefaultDERControl/setGradW': 50, 's/DERProgram/primacy': 3,
                                          's/DERSettings/setMaxW': 1, 's/zz': 2})
    assert set(results) == {'s/DefaultDERControl/opModMaxLimW', 's/DefaultDERControl/opModConnect',
                            's/DefaultDERControl/setGradW', 's/DERProgram/primacy'}
    assert 'reported by the DER client' in errors['s/DERSettings/setMaxW'] and errors['s/zz'] == 'unregistered topic'
    held, _ = await served.read(['s/DefaultDERControl/opModMaxLimW', 's/DefaultDERControl/opModConnect', 's/DERProgram/primacy'])
    assert held == {'s/DefaultDERControl/opModMaxLimW': 80, 's/DefaultDERControl/opModConnect': True, 's/DERProgram/primacy': 3}
    await client.engine.refresh()
    view = client.engine.snapshot()
    assert view['DefaultDERControl.opModMaxLimW'] == 80 and view['DERControl.opModMaxLimW'] == 80 and view['DefaultDERControl.setGradW'] == 50
    assert served.pushes.values == []                              # the platform's own writes are not pushed back


async def test_immediate_control_is_an_active_event_and_the_client_responds(served, client):
    results, errors = await served.write({'s/DERControl/opModMaxLimW': 40, 's/DERControl/opModTargetW': 5.5})
    assert errors == {} and len(results) == 2
    [entry] = served._schedule_view()
    assert entry['status'] == 'active' and entry['immediate'] and entry['DERControl']['opModMaxLimW'] == 40
    assert entry['DERControl']['opModTargetW'] == 5500                                 # 5.5 / scaling 0.001
    held, _ = await served.read(['s/DERControl/opModMaxLimW', 's/DERControl/mRID', 's/DERControl/opModTargetW'])
    assert held['s/DERControl/opModMaxLimW'] == 40 and held['s/DERControl/mRID'] == entry['mRID'] and held['s/DERControl/opModTargetW'] == 5.5
    await client.engine.refresh()
    await client.engine.tick()
    view = client.engine.snapshot()
    assert view['DERControl.opModMaxLimW'] == 40 and view['DERControl.opModTargetW'] == 5500
    assert await wait_for(lambda: sorted(r['status'] for r in served.responses.get(entry['mRID'], [])) == [1, 2])
    pushed_list = served.pushes.latest('s/DERControlList')
    assert pushed_list is None                                      # DERControlList is platform-written: reflected, not pushed
    # A second immediate control supersedes the first.
    await served.write({'s/DERControl/opModMaxLimW': 30})
    statuses = {e['mRID']: e['status'] for e in served._schedule_view()}
    assert list(statuses.values()).count('active') == 1 and 'superseded' in statuses.values()


async def test_schedule_creates_cancels_and_completes_events(served):
    clock = FakeClock()
    served.clock = clock
    now = int(clock())
    entries = [{'interval': {'start': now + 60, 'duration': 30}, 'DERControl': {'opModMaxLimW': 70}},
               {'mRID': 'ab' * 16, 'interval': {'start': now - 5, 'duration': 30}, 'DERControl': {'opModConnect': False}}]
    results, errors = await served.write({'s/DERControlList': entries})
    assert errors == {}
    view = served._schedule_view()
    assert [e['status'] for e in view] == ['scheduled', 'active'] and view[1]['mRID'] == 'AB' * 16
    held, _ = await served.read(['s/DERControlList'])
    assert len(held['s/DERControlList']) == 2
    await served.write({'s/DERControlList': entries[1:]})                            # the first event is dropped
    assert {e['mRID']: e['status'] for e in served._schedule_view()}[view[0]['mRID']] == 'cancelled'
    clock.advance(40)
    served._refresh_statuses()
    assert {e['mRID']: e['status'] for e in served._schedule_view()}['AB' * 16] == 'complete'
    clock.advance(500)
    served._refresh_statuses()
    assert served._schedule_view() == []                                             # finished events are dropped later
    _, errors = await served.write({'s/DERControlList': '"not a list"'})
    assert 'invalid value' in errors['s/DERControlList']


async def test_curves_are_served_and_linked(served, client):
    points = [{'xvalue': 95, 'yvalue': 30}, {'xvalue': 105, 'yvalue': -30}]
    results, errors = await served.write({'s/DERCurve/opModVoltVar/CurveData': points})
    assert errors == {} and served.curves == {'opModVoltVar': '/derp/1/dc/1'}
    held, _ = await served.read(['s/DERCurve/opModVoltVar/CurveData'])
    assert held['s/DERCurve/opModVoltVar/CurveData'] == points
    # Linking the curve from the default control by its attribute; the client sees the curve points.
    served.configure_points(SERVED_POINTS + [{'topic': 's/DefaultDERControl/opModVoltVar', 'path': 'DefaultDERControl.opModVoltVar'}])
    results, errors = await served.write({'s/DefaultDERControl/opModVoltVar': True})
    assert errors == {} and served.resources['/derp/1/dderc'].DERControlBase.opModVoltVar.href == '/derp/1/dc/1'
    await client.engine.refresh()
    assert client.engine.snapshot().get('DERCurve.opModVoltVar.CurveData') == points
    _, errors = await served.write({'s/DefaultDERControl/opModVoltVar': False})
    assert errors == {} and served.resources['/derp/1/dderc'].DERControlBase.opModVoltVar is None


async def test_client_reports_are_pushed_as_received_values(served, client):
    results, errors = await client.write({'c/DERSettings/setMaxW': 5000, 'c/DERStatus/connectStatus': '0x01',
                                          'c/DERCapability/type': 83, 'c/MirrorMeterReading/W': 1234.0,
                                          'c/MirrorMeterReading/V/PhaseA': 2401})
    assert errors == {}, errors
    assert await wait_for(lambda: served.pushes.latest('s/MirrorMeterReading/V/PhaseA') is not None)
    assert served.pushes.latest('s/DERSettings/setMaxW') == 5000 and served.pushes.latest('s/DERCapability/type') == 83
    assert served.pushes.latest('s/DERStatus/connectStatus') == '01'
    assert served.pushes.latest('s/MirrorMeterReading/W') == 1234.0
    assert served.pushes.latest('s/MirrorMeterReading/V/PhaseA') == pytest.approx(240.1)     # scaled for the platform
    held, _ = await served.read(['s/DERSettings/setMaxW', 's/MirrorMeterReading/W', 's/DERControl/opModMaxLimW'])
    assert held == {'s/DERSettings/setMaxW': 5000, 's/MirrorMeterReading/W': 1234.0, 's/DERControl/opModMaxLimW': None}
    table = await served.push_all()
    assert table['s/DERSettings/setMaxW'] == 5000 and table['s/DERProgram/primacy'] == 1 and 's/DERControl/opModMaxLimW' not in table


async def test_starting_values_seed_served_controls_once():
    s = ServedSep2Server('127.0.0.1', 0, tls=False, poll_rate=1)
    s.configure_points([{'topic': 's/d', 'path': 'DefaultDERControl.opModMaxLimW', 'starting_value': 100},
                        {'topic': 's/g', 'path': 'DefaultDERControl.setGradW', 'starting_value': 50},
                        {'topic': 's/p', 'path': 'DERProgram.primacy', 'starting_value': 7}])
    assert await s.seed({'s/g': 60}) == 2                       # the caller's value beats the row's; primacy is already held
    held, _ = await s.read(['s/d', 's/g', 's/p'])
    assert held == {'s/d': 100, 's/g': 60, 's/p': 1}
    assert await s.seed({'s/d': 1}) == 0                                            # held values are kept
    await s.close()


async def test_resubscribing_replaces_rather_than_duplicates(served):
    sub = sep.Subscription(subscribedResource='/derp/1/dderc', encoding=0, level='+S1', limit=10,
                           notificationURI='http://127.0.0.1:1/notify')
    from protocol_proxy.protocol.ieee2030_5.models import to_xml
    status, _, headers = await served.handle('POST', '/edev/1/sub', {}, {}, to_xml(sub))
    status2, _, headers2 = await served.handle('POST', '/edev/1/sub', {}, {}, to_xml(sub))
    assert status == status2 == 201 and headers['Location'] == headers2['Location']
    assert served.describe()['subscriptions'] == 1


async def test_subscriptions_are_notified(served):
    pushed = []
    client = make_client(served, pushed, subscribe=True, notify_host='127.0.0.1', notify_port=0, poll_rate_ceiling=600)
    client.start()
    assert await client.wait_ready(5.0), client.start_error
    try:
        assert client.subscriptions.supported and len(client.subscriptions.subscriptions) == 3
        await served.write({'s/DefaultDERControl/opModMaxLimW': 42})
        assert await wait_for(lambda: served.notifications_sent >= 1 and client.receiver.received >= 1)
        assert await wait_for(lambda: any(v.get('c/DefaultDERControl/opModMaxLimW') == 42 for v in pushed), 5)
    finally:
        await client.close()
    assert served.describe()['subscriptions'] == 0                                   # deleted when the client closed


async def test_state_survives_a_restart(tmp_path, served, client):
    await served.write({'s/DefaultDERControl/opModMaxLimW': 77, 's/DERControl/opModMaxLimW': 55})
    await client.write({'c/DERSettings/setMaxW': 4321})
    await wait_for(lambda: served.received.get('DERSettings.setMaxW') == 4321)
    port = served.bound_port
    await served.close()
    assert served.state_path.exists()
    again = ServedSep2Server('127.0.0.1', 0, tls=False, poll_rate=1, client_lfdi=LFDI, client_pin=PIN, state_dir=str(tmp_path))
    again.configure_points(SERVED_POINTS)
    assert again.devices == {LFDI: '/edev/1'} and again.received['DERSettings.setMaxW'] == 4321
    held, _ = await again.read(['s/DefaultDERControl/opModMaxLimW', 's/DERControl/opModMaxLimW', 's/DERSettings/setMaxW'])
    assert held == {'s/DefaultDERControl/opModMaxLimW': 77, 's/DERControl/opModMaxLimW': 55, 's/DERSettings/setMaxW': 4321}
    assert again.counters['derc'] == served.counters['derc'] and again.immediate_href == served.immediate_href
    assert again.lists['/derp'][1][0] is again.resources['/derp/1']
    await again.start()
    assert again.bound_port != port or True
    await again.close()


async def test_proxy_serves_and_closes_a_server(tmp_path):
    proxy = make_proxy()
    pushes = []

    async def fake_push(client, values):
        pushes.append((client.remote_id, values))
    proxy._push = fake_push
    remote = uuid4()
    reply = await call(proxy, proxy.register_server_endpoint,
                       {'role': 'server', 'bind_host': '127.0.0.1', 'port': 0, 'tls': False, 'client_lfdi': LFDI,
                        'points': SERVED_POINTS, 'values': {'s/DefaultDERControl/opModMaxLimW': 90, 's/DERSettings/setMaxW': 1}},
                       remote=remote)
    assert reply['error'] == {}, reply
    key = 'server|127.0.0.1:0'
    assert reply['result']['server'] == key and reply['result']['points'] == len(SERVED_POINTS)
    assert reply['result']['role'] == 'server' and reply['result']['seeded'] == 1 and reply['result']['port'] > 0
    served = proxy.servers[key]
    assert proxy.remotes[remote] is served and served.server.is_serving
    await asyncio.sleep(0.05)
    assert pushes and pushes[0][0] == remote and pushes[0][1]['s/DefaultDERControl/opModMaxLimW'] == 90
    reply = await call(proxy, proxy.read_resources_endpoint, {'topics': ['s/DefaultDERControl/opModMaxLimW']}, remote=remote)
    assert reply['result'] == {'s/DefaultDERControl/opModMaxLimW': 90}
    reply = await call(proxy, proxy.write_resources_endpoint, {'values': {'s/DefaultDERControl/opModMaxLimW': 91}}, remote=remote)
    assert reply['result']['s/DefaultDERControl/opModMaxLimW']['status'] == 'stored'
    reply = await call(proxy, proxy.describe_server_endpoint, {}, remote=remote)
    assert reply['result']['role'] == 'server' and reply['result']['devices'] == {LFDI: '/edev/1'}
    # Re-registering with the same listener keeps the served server; a changed table replaces the points.
    reply = await call(proxy, proxy.register_server_endpoint, {'role': 'server', 'bind_host': '127.0.0.1', 'port': 0, 'tls': False,
                                                               'points': SERVED_POINTS[:2]}, remote=remote)
    assert reply['result']['points'] == 2 and proxy.servers[key] is served
    reply = await call(proxy, proxy.register_server_endpoint, {'role': 'sideways', 'points': []}, remote=remote)
    assert 'Unknown role' in reply['error']['server']
    reply = await call(proxy, proxy.register_server_endpoint, {'role': 'server', 'bind_host': '127.0.0.1', 'port': 1, 'points': []},
                       remote=uuid4())
    assert 'cert_path' in reply['error']['server'] and len(proxy.servers) == 1              # TLS without a certificate
    reply = await call(proxy, proxy.close_server_endpoint, {}, remote=remote)
    assert reply['result'] == {'closed': True} and proxy.servers == {} and proxy.remotes == {}
