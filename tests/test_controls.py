"""The DER control engine: events, randomisation, responses, supersession, curves and the convention snapshot."""
import random

import pytest

from protocol_proxy.protocol.ieee2030_5 import controls
from protocol_proxy.protocol.ieee2030_5.controls import (RESPONSE_CANCELLED, RESPONSE_COMPLETED, RESPONSE_RECEIVED,
                                                        RESPONSE_REJECTED_EXPIRED, RESPONSE_STARTED, RESPONSE_SUPERSEDED)
from protocol_proxy.protocol.ieee2030_5.models import sep

from tests.conftest import make_client
from tests.fake_server import LFDI, FakeSep2Server


def responses(server):
    return [(r.subject.hex().upper(), r.status) for r in server.responses]


async def test_defaults_appear_as_the_effective_control(client, server, pushed):
    view = client.engine.last_snapshot
    assert view['DERControl.opModMaxLimW'] == 10000 and view['DERControl.opModConnect'] is True
    assert view['DefaultDERControl.opModMaxLimW'] == 10000 and view['DefaultDERControl.setGradW'] == 100
    assert view['DERControlList'] == [] and view['DERProgram.primacy'] == 1
    # The first snapshot was pushed, keyed by topic, scaled.
    first = pushed[0]
    assert first['der/DERControl/opModMaxLimW'] == 10000 and first['der/DERProgram/primacy'] == 1
    assert first['der/DERControlList'] == [] and 'der/DERSettings/setMaxW' not in first


async def test_event_lifecycle_with_responses(client, server, clock, pushed):
    now = client.session.now()
    control = server.add_control('/derp/1', now + 100, 60, {'opModMaxLimW': 5000, 'opModTargetW': {'value': 25, 'multiplier': 2}})
    pushed.clear()
    await client.engine.refresh()
    mrid = control.mRID.hex().upper()
    assert responses(server) == [(mrid, RESPONSE_RECEIVED)]
    state = client.engine.events[mrid]
    assert state.status == 'scheduled' and (state.start, state.end) == (now + 100, now + 160)
    entry = pushed[-1]['der/DERControlList'][0]
    assert entry['mRID'] == mrid and entry['interval'] == {'start': now + 100, 'duration': 60}
    assert entry['DERControl']['opModMaxLimW'] == 5000 and entry['status'] == 'scheduled'
    assert 'der/DERControl/opModMaxLimW' not in pushed[-1]          # still the default, so unchanged

    clock.advance(100)
    changed = await client.engine.tick()
    assert state.status == 'active' and responses(server)[-1] == (mrid, RESPONSE_STARTED)
    assert changed['DERControl.opModMaxLimW'] == 5000 and changed['DERControl.opModTargetW'] == 2500
    assert changed['DERControl.mRID'] == mrid and changed['DERControl.opModConnect' if False else 'DERControl.interval.duration'] == 60
    assert pushed[-1]['der/DERControl/opModTargetW'] == pytest.approx(2.5)    # scaling 0.001
    assert pushed[-1]['der/DERControl/opModMaxLimW'] == 5000

    clock.advance(60)
    changed = await client.engine.tick()
    assert state.status == 'complete' and responses(server)[-1] == (mrid, RESPONSE_COMPLETED)
    assert changed['DERControl.opModMaxLimW'] == 10000 and changed['DERControl.mRID'] is None
    assert pushed[-1]['der/DERControl/opModMaxLimW'] == 10000 and pushed[-1]['der/DERControl/mRID'] is None
    assert client.engine.last_snapshot['DERControlList'] == []


async def test_cancel_and_supersede_from_the_server(client, server, clock):
    now = client.session.now()
    a = server.add_control('/derp/1', now + 10, 60, {'opModMaxLimW': 1})
    b = server.add_control('/derp/1', now + 500, 60, {'opModMaxLimW': 2})
    await client.engine.refresh()
    server.set_status(a, controls.CANCELLED)
    server.set_status(b, controls.SUPERSEDED)
    await client.engine.refresh()
    assert client.engine.events[a.mRID.hex().upper()].status == 'cancelled'
    assert client.engine.events[b.mRID.hex().upper()].status == 'superseded'
    assert responses(server)[-2:] == [(a.mRID.hex().upper(), RESPONSE_CANCELLED), (b.mRID.hex().upper(), RESPONSE_SUPERSEDED)]
    assert client.engine.last_snapshot['DERControlList'] == []
    # An event that vanishes from the list while scheduled is dropped as cancelled, without a response.
    c = server.add_control('/derp/1', now + 900, 60, {'opModMaxLimW': 3})
    await client.engine.refresh()
    server.remove_control(c)
    count = len(server.responses)
    await client.engine.refresh()
    assert client.engine.events[c.mRID.hex().upper()].status == 'cancelled' and len(server.responses) == count


async def test_already_active_and_expired_events(client, server, clock):
    now = client.session.now()
    active = server.add_control('/derp/1', now - 30, 60, {'opModMaxLimW': 4000}, status=controls.ACTIVE)
    expired = server.add_control('/derp/1', now - 600, 60, {'opModMaxLimW': 1})
    cancelled_before = server.add_control('/derp/1', now + 60, 60, {'opModMaxLimW': 1}, status=controls.CANCELLED)
    await client.engine.refresh()
    assert client.engine.events[active.mRID.hex().upper()].status == 'active'
    assert client.engine.last_snapshot['DERControl.opModMaxLimW'] == 4000
    assert (expired.mRID.hex().upper(), RESPONSE_REJECTED_EXPIRED) in responses(server)
    assert expired.mRID.hex().upper() not in client.engine.events
    assert cancelled_before.mRID.hex().upper() not in client.engine.events
    mine = [s for m, s in responses(server) if m == active.mRID.hex().upper()]
    assert mine == [RESPONSE_RECEIVED, RESPONSE_STARTED]


async def test_randomization_is_drawn_once_within_bounds(client, server, clock):
    client.engine.rng = random.Random(7)
    now = client.session.now()
    control = server.add_control('/derp/1', now + 100, 100, {'opModMaxLimW': 1}, randomize_start=30, randomize_duration=-20)
    await client.engine.refresh()
    state = client.engine.events[control.mRID.hex().upper()]
    assert now + 100 <= state.start <= now + 130 and 80 <= state.end - state.start <= 100
    start, end = state.start, state.end
    await client.engine.refresh()
    assert (state.start, state.end) == (start, end)


async def test_overlapping_events_resolved_by_primacy_then_creation_time(clock):
    server = FakeSep2Server(existing_lfdi=LFDI, clock=clock)
    low = server.add_program(primacy=5)
    high = server.add_program(primacy=1)
    client = make_client(server, clock)
    client.start()
    assert await client.wait_ready(5.0), client.start_error
    now = client.session.now()
    weak = server.add_control(low, now + 10, 100, {'opModMaxLimW': 1000}, creation_time=now)
    strong = server.add_control(high, now + 10, 100, {'opModMaxLimW': 2000}, creation_time=now - 50)
    unrelated = server.add_control(low, now + 10, 100, {'opModFixedW': 5000}, creation_time=now)
    older = server.add_control(high, now + 10, 100, {'opModMaxLimW': 3000}, creation_time=now - 100)
    await client.engine.refresh()
    clock.advance(10)
    await client.engine.tick()
    statuses = {c.mRID.hex().upper(): client.engine.events[c.mRID.hex().upper()].status for c in (weak, strong, unrelated, older)}
    assert statuses == {weak.mRID.hex().upper(): 'superseded', strong.mRID.hex().upper(): 'active',
                        unrelated.mRID.hex().upper(): 'active', older.mRID.hex().upper(): 'superseded'}
    view = client.engine.last_snapshot
    assert view['DERControl.opModMaxLimW'] == 2000 and view['DERControl.opModFixedW'] == 5000
    assert view['DERProgram.primacy'] == 1 and view['DERControl.mRID'] == strong.mRID.hex().upper()
    assert (weak.mRID.hex().upper(), RESPONSE_SUPERSEDED) in responses(server)
    await client.close()


async def test_curves_resolved_from_links(client, server, clock):
    href = server.add_curve('/derp/1', [(9500, 2500), (9800, 0), (10200, 0), (10500, -2500)], vRef=10000, openLoopTms=50)
    now = client.session.now()
    control = server.add_control('/derp/1', now - 1, 600, {'opModVoltVar': href}, status=controls.ACTIVE)
    await client.engine.refresh()
    view = client.engine.last_snapshot
    assert view['DERCurve.opModVoltVar.CurveData'] == [{'xvalue': 9500, 'yvalue': 2500}, {'xvalue': 9800, 'yvalue': 0},
                                                       {'xvalue': 10200, 'yvalue': 0}, {'xvalue': 10500, 'yvalue': -2500}]
    assert view['DERCurve.opModVoltVar.vRef'] == 10000 and view['DERCurve.opModVoltVar.curveType'] == 11
    assert view['DERControl.opModVoltVar'] == href
    entry = view['DERControlList'][0]
    assert entry['DERCurve']['opModVoltVar']['openLoopTms'] == 50 and entry['mRID'] == control.mRID.hex().upper()
    results, errors = await client.read(['der/DERCurve/opModVoltVar/CurveData', 'der/DERControlList'])
    assert errors == {} and len(results['der/DERCurve/opModVoltVar/CurveData']) == 4
    assert results['der/DERControlList'][0]['interval']['duration'] == 600


async def test_responses_follow_response_required_bits_and_reply_to(client, server, clock):
    now = client.session.now()
    silent = server.add_control('/derp/1', now + 5, 10, {'opModMaxLimW': 1}, response_required=0)
    received_only = server.add_control('/derp/1', now + 5, 10, {'opModMaxLimW': 2}, response_required=0x01)
    no_reply_to = server.add_control('/derp/1', now + 5, 10, {'opModMaxLimW': 3}, reply_to=None)
    await client.engine.refresh()
    clock.advance(5)
    await client.engine.tick()
    sent = responses(server)
    assert all(m != silent.mRID.hex().upper() for m, _ in sent)
    assert [s for m, s in sent if m == received_only.mRID.hex().upper()] == [RESPONSE_RECEIVED]
    assert all(m != no_reply_to.mRID.hex().upper() for m, _ in sent)


async def test_response_post_failure_does_not_break_the_engine(client, server, clock):
    now = client.session.now()
    control = server.add_control('/derp/1', now + 5, 10, {'opModMaxLimW': 1})
    server.fail_next['/rsps/1/rsp'] = 404
    await client.engine.refresh()
    assert client.engine.events[control.mRID.hex().upper()].status == 'scheduled'
    assert responses(server) == []


async def test_run_loop_polls_and_wakes_on_request(clock):
    server = FakeSep2Server(existing_lfdi=LFDI, clock=clock, poll_rate=5)
    server.add_program()
    pushed = []
    client = make_client(server, clock, pushed=pushed)
    client.start()
    assert await client.wait_ready(5.0), client.start_error
    assert client.engine.poll_rate == 5
    gets = len(server.paths('GET'))
    now = client.session.now()
    server.add_control('/derp/1', now - 1, 600, {'opModMaxLimW': 777}, status=controls.ACTIVE)
    client.engine.request_refresh()
    import asyncio
    for _ in range(50):
        await asyncio.sleep(0.02)
        if pushed and pushed[-1].get('der/DERControl/opModMaxLimW') == 777:
            break
    assert pushed[-1]['der/DERControl/opModMaxLimW'] == 777 and len(server.paths('GET')) > gets
    await client.close()


def test_response_required_bits_helper():
    assert controls.response_required_bits(sep.DERControl(responseRequired=bytes([0x07]))) == 7
    assert controls.response_required_bits(sep.DERControl(responseRequired=None)) == 0
