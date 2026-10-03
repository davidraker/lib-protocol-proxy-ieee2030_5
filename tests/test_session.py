"""The start-up phases against the fake server."""
import pytest

from protocol_proxy.protocol.ieee2030_5.models import sep
from protocol_proxy.protocol.ieee2030_5.session import SessionError

from tests.conftest import make_client
from tests.fake_server import LFDI, FakeSep2Server


async def test_phases_find_existing_device_and_links(client, server):
    s = client.session
    assert s.ready and not s.registered_now
    assert s.end_device.href == '/edev/1' and s.registration.pIN == server.pin
    assert [p.href for p in s.programs] == ['/derp/1'] and s.der.href == '/edev/1/der/1'
    assert s.upward_hrefs() == {'DERCapability': '/edev/1/der/1/dercap', 'DERSettings': '/edev/1/der/1/derg',
                                'DERStatus': '/edev/1/der/1/ders', 'DERAvailability': '/edev/1/der/1/dera',
                                'DeviceInformation': '/edev/1/di'}
    # Every href came from a link; the only configured path is /dcap, and it was fetched first.
    assert server.paths('GET')[0] == '/dcap' and '/tm' in server.paths('GET')
    assert s.mup_href == '/mup/1'
    mup = server.resources['/mup/1']
    assert mup.deviceLFDI.hex().upper() == LFDI and len(mup.MirrorMeterReading) == 2
    units = {r.ReadingType.uom for r in mup.MirrorMeterReading}
    assert units == {38, 29}


async def test_registers_when_missing(clock):
    server = FakeSep2Server(clock=clock)
    server.add_program()         # a program with no device yet; it is attached through the dcap list
    server.lists['/derp'][1][0].DERControlListLink.all = 0
    client = make_client(server, clock, pin=None)
    client.start()
    assert await client.wait_ready(5.0), client.start_error
    assert client.session.registered_now and client.session.end_device.href == '/edev/1'
    posted = [r for r in server.requests if r.method == 'POST' and r.path == '/edev'][0].body
    assert isinstance(posted, sep.EndDevice) and posted.lFDI.hex().upper() == LFDI and posted.sFDI == client.session.sfdi
    assert posted.deviceCategory == bytes.fromhex('00400000')
    await client.close()


async def test_missing_device_without_registration_is_an_error(clock):
    server = FakeSep2Server(clock=clock)
    client = make_client(server, clock, pin=None, register_if_missing=False)
    client.start()
    assert not await client.wait_ready(5.0)
    assert 'registration is disabled' in client.start_error
    await client.close()


async def test_pin_mismatch_stops_the_session(server, clock):
    client = make_client(server, clock, pin=999990)
    client.start()
    assert not await client.wait_ready(5.0)
    assert 'PIN mismatch' in client.start_error
    results, errors = await client.read(['der/DERControl/opModMaxLimW'])
    assert results == {} and 'PIN mismatch' in errors['der/DERControl/opModMaxLimW']
    await client.close()


async def test_time_offset_uses_server_time(clock):
    server = FakeSep2Server(existing_lfdi=LFDI, clock=clock, time_offset=120)
    server.add_program()
    client = make_client(server, clock)
    client.start()
    assert await client.wait_ready(5.0), client.start_error
    assert client.session.time_offset == pytest.approx(120, abs=1)
    assert client.session.now() == pytest.approx(clock() + 120, abs=1)
    await client.close()


async def test_poll_rate_bounds(clock):
    server = FakeSep2Server(existing_lfdi=LFDI, clock=clock, poll_rate=900)
    server.add_program()
    client = make_client(server, clock, poll_rate_floor=10)
    client.start()
    assert await client.wait_ready(5.0), client.start_error
    assert client.engine.poll_rate == 900                       # the server's value, above the floor
    assert client.session.poll_rate(3) == 10                    # floored
    client.session.config.poll_rate_ceiling = 30
    assert client.session.poll_rate() == 30                     # capped for a test harness
    client.session.config.poll_rate_ceiling = 1
    assert client.session.poll_rate() == 10                     # the ceiling never undercuts the floor
    await client.close()


async def test_programs_sorted_by_primacy_across_fsa_and_dcap(clock):
    server = FakeSep2Server(existing_lfdi=LFDI, clock=clock)
    server.add_program(primacy=5)
    server.add_program(primacy=2)
    server.resources['/dcap'].DERProgramListLink = sep.DERProgramListLink(href='/derp', all=2)
    client = make_client(server, clock)
    client.start()
    assert await client.wait_ready(5.0), client.start_error
    assert [(p.href, p.primacy) for p in client.session.programs] == [('/derp/2', 2), ('/derp/1', 5)]
    await client.close()


async def test_empty_der_list_declares_a_der_when_the_server_allows(clock):
    server = FakeSep2Server(existing_lfdi=LFDI, clock=clock, with_der=False, der_put_creates=True)
    server.add_program()
    client = make_client(server, clock)
    client.start()
    assert await client.wait_ready(5.0), client.start_error
    assert client.session.der.href == '/edev/1/der/1' and '/edev/1/der/1' in server.puts
    assert client.session.upward_hrefs()['DERSettings'] == '/edev/1/der/1/derg'
    results, errors = await client.write({'der/DERSettings/setMaxW': 5000})
    assert errors == {} and server.puts['/edev/1/der/1/derg'].setMaxW.value == 5000
    await client.close()


async def test_no_der_list_is_tolerated(clock):
    server = FakeSep2Server(existing_lfdi=LFDI, clock=clock, with_der=False, with_mup=False)
    server.add_program()
    server.resources['/edev/1'].DERListLink = None
    client = make_client(server, clock)
    client.start()
    assert await client.wait_ready(5.0), client.start_error
    assert client.session.der is None and client.session.upward_hrefs() == {'DeviceInformation': '/edev/1/di'}
    results, errors = await client.write({'der/DERSettings/setMaxW': 5000})
    assert results == {} and 'no DERSettings link' in errors['der/DERSettings/setMaxW']
    await client.close()


async def test_session_error_type_is_reported(clock):
    server = FakeSep2Server(existing_lfdi=LFDI, clock=clock)
    server.resources['/dcap'].EndDeviceListLink = None
    client = make_client(server, clock)
    with pytest.raises(SessionError):
        await client.session.start()
    await client.close()
