"""Upward writes: shadows, complete PUTs, required-field filling, readings."""
from protocol_proxy.protocol.ieee2030_5.models import sep


async def test_settings_written_as_one_complete_put(client, server):
    results, errors = await client.write({'der/DERSettings/setMaxW': 5000.0, 'der/DERSettings/setMaxVar': 2500})
    assert errors == {}
    assert results['der/DERSettings/setMaxW'] == {'status': 204, 'resource': 'DERSettings', 'value': 5000.0}
    puts = [r for r in server.requests if r.method == 'PUT' and r.path == '/edev/1/der/1/derg']
    assert len(puts) == 1
    settings = puts[0].body
    assert isinstance(settings, sep.DERSettings)
    assert settings.setMaxW == sep.ActivePower(multiplier=0, value=5000)
    assert settings.setMaxVar == sep.ReactivePower(multiplier=1, value=250)      # row multiplier 1
    assert settings.setGradW == 0 and settings.updatedTime == client.session.now()   # required, filled
    # The shadow persists: a later single-attribute write still carries the earlier values.
    await client.write({'der/DERSettings/setMaxW': 6000})
    again = [r for r in server.requests if r.method == 'PUT' and r.path == '/edev/1/der/1/derg'][-1].body
    assert again.setMaxW.value == 6000 and again.setMaxVar.value == 250
    results, _ = await client.read(['der/DERSettings/setMaxW', 'der/DERSettings/setMaxVar'])
    assert results == {'der/DERSettings/setMaxW': 6000, 'der/DERSettings/setMaxVar': 2500}


async def test_status_structs_and_aliases(client, server):
    results, errors = await client.write({'der/DERStatus/operationalModeStatus': 2, 'der/DERStatus/connectStatus': 1})
    assert errors == {}
    status = server.puts['/edev/1/der/1/ders']
    assert status.operationalModeStatus.value == 2 and status.operationalModeStatus.dateTime == client.session.now()
    assert status.genConnectStatus.value == b'\x01' and status.readingTime == client.session.now()   # a bitmap
    results, _ = await client.read(['der/DERStatus/connectStatus'])
    assert results == {'der/DERStatus/connectStatus': '01'}


async def test_capability_seeded_from_starting_values(client, server):
    results, _ = await client.read(['der/DERCapability/rtgMaxW', 'der/DERCapability/type'])
    assert results == {'der/DERCapability/rtgMaxW': 8000, 'der/DERCapability/type': 83}
    await client.write({'der/DERCapability/rtgMaxW': 9000})
    cap = server.puts['/edev/1/der/1/dercap']
    assert cap.rtgMaxW.value == 9000 and cap.type == 83 and cap.modesSupported == bytes(4)


async def test_shadow_seeded_from_the_server_copy(server, clock):
    from tests.conftest import make_client
    server.resources['/edev/1/der/1/derg'] = sep.DERSettings(href='/edev/1/der/1/derg', setGradW=55,
                                                           setMaxW=sep.ActivePower(multiplier=0, value=1234), updatedTime=1)
    client = make_client(server, clock)
    client.start()
    assert await client.wait_ready(5.0), client.start_error
    results, _ = await client.read(['der/DERSettings/setMaxW'])
    assert results == {'der/DERSettings/setMaxW': 1234}
    await client.write({'der/DERSettings/setMaxVar': 10})
    assert server.puts['/edev/1/der/1/derg'].setGradW == 55 and server.puts['/edev/1/der/1/derg'].setMaxW.value == 1234
    await client.close()


async def test_readings_posted_to_the_mirror_usage_point(client, server):
    results, errors = await client.write({'der/MirrorMeterReading/W': 4321.6, 'der/MirrorMeterReading/V/PhaseA': 240.1})
    assert errors == {}
    assert results['der/MirrorMeterReading/W']['status'] == 'posted' and results['der/MirrorMeterReading/W']['href'] == '/mup/1/mr/1'
    assert [r.Reading.value for r in server.readings] == [4322, 2401]        # scaling 0.1 undone for the voltage
    assert server.readings[0].ReadingType.uom == 38 and server.readings[1].ReadingType.phase == 128
    assert server.readings[0].Reading.timePeriod.start == client.session.now()
    results, _ = await client.read(['der/MirrorMeterReading/W'])
    assert results == {'der/MirrorMeterReading/W': 4321.6}


async def test_write_errors(client, server):
    results, errors = await client.write({'der/DERControl/opModMaxLimW': 1, 'nope': 2, 'der/DERSettings/setMaxW': 'abc'})
    assert results == {}
    assert 'cannot be written' in errors['der/DERControl/opModMaxLimW'] and errors['nope'] == 'unregistered topic'
    assert 'invalid value' in errors['der/DERSettings/setMaxW']
    server.fail_next['/edev/1/der/1/derg'] = 403
    results, errors = await client.write({'der/DERSettings/setMaxW': 1})
    assert results == {} and 'HTTP 403' in errors['der/DERSettings/setMaxW']
