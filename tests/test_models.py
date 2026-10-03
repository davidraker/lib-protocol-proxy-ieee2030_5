"""XML round trips, identifiers and the convention conversions."""
import pytest

from protocol_proxy.protocol.ieee2030_5 import identity
from protocol_proxy.protocol.ieee2030_5.models import convert, from_xml, list_items, sep, to_xml
from protocol_proxy.protocol.ieee2030_5.points import PointSpec, ReadingSpec, parse_path


def test_xml_round_trip_quantities_hex_and_attributes():
    settings = sep.DERSettings(href='/derg', setMaxW=sep.ActivePower(multiplier=0, value=5000), setGradW=100,
                               updatedTime=1700000000, modesEnabled=bytes.fromhex('00000003'))
    xml = to_xml(settings)
    assert xml.startswith(b'<?xml') and b'xmlns="urn:ieee:std:2030.5:ns"' in xml and b'href="/derg"' in xml
    back = from_xml(xml, sep.DERSettings)
    assert back == settings


def test_xml_lenient_on_unknown_content_and_lists():
    doc = (b'<DERControlList xmlns="urn:ieee:std:2030.5:ns" href="/derc" all="2" results="1" vendor="x">'
           b'<DERControl replyTo="/rsp" responseRequired="07"><mRID>EE01EE01EE01EE01EE01EE01EE01EE01</mRID>'
           b'<creationTime>1</creationTime><EventStatus><currentStatus>0</currentStatus><dateTime>1</dateTime>'
           b'<potentiallySuperseded>false</potentiallySuperseded></EventStatus><interval><duration>60</duration>'
           b'<start>5</start></interval><randomizeStart>10</randomizeStart><DERControlBase><opModConnect>true</opModConnect>'
           b'<opModMaxLimW>5000</opModMaxLimW><Extension>1</Extension></DERControlBase></DERControl></DERControlList>')
    page = from_xml(doc, sep.DERControlList)
    assert page.all == 2 and page.results == 1
    [control] = list_items(page)
    assert control.replyTo == '/rsp' and control.responseRequired == b'\x07' and control.randomizeStart == 10
    assert control.DERControlBase.opModMaxLimW == 5000 and control.DERControlBase.opModConnect is True
    assert list_items(None) == [] and list_items(sep.EndDeviceList(all=0, results=0)) == []


def test_identity_vectors():
    assert identity.sfdi_is_valid('167261211391') and not identity.sfdi_is_valid('167261211390')
    assert identity.sfdi_is_valid('000000000000') and not identity.sfdi_is_valid('12345')
    assert identity.format_sfdi(167261211391) == '167-261-211-391'
    lfdi = '3E4F45AB31EDFE5B67E343E5E4562E31984E23E5'
    sfdi = identity.sfdi_from_lfdi(lfdi)
    assert sfdi == int(f'{int(lfdi[:9], 16):011d}' + str(identity.check_digit(f'{int(lfdi[:9], 16):011d}')))
    assert identity.sfdi_is_valid(sfdi) and len(str(sfdi)) == 12
    assert identity.lfdi_from_der(b'cert') == __import__('hashlib').sha256(b'cert').hexdigest()[:40].upper()
    assert identity.normalize_lfdi('3e:4f:45:ab:31:ed:fe:5b:67:e3:43:e5:e4:56:2e:31:98:4e:23:e5') == lfdi
    assert identity.lfdi_bytes(lfdi) == bytes.fromhex(lfdi)
    with pytest.raises(ValueError):
        identity.normalize_lfdi('abc')
    assert identity.pin_is_valid(111115) and not identity.pin_is_valid(111111)


def test_convert_paths_and_flatten():
    status = sep.DERStatus()
    convert.set_path(status, ('operationalModeStatus',), 2, now=9)
    convert.set_path(status, ('alarmStatus',), '0x11')
    convert.set_path(status, ('stateOfChargeStatus',), {'value': 55, 'dateTime': 3})
    assert status.operationalModeStatus == sep.OperationalModeStatusType(dateTime=9, value=2)
    assert status.alarmStatus == bytes.fromhex('00000011') and status.stateOfChargeStatus.value == 55
    assert convert.flatten(status) == {'alarmStatus': '00000011', 'operationalModeStatus': 2, 'stateOfChargeStatus': 55}
    assert convert.resolve_alias(('DERStatus', 'connectStatus')) == ('DERStatus', 'genConnectStatus')
    assert convert.quantity_value(sep.ActivePower(value=12, multiplier=-2)) == pytest.approx(0.12)
    assert convert.quantity_from(sep.ActivePower, 1234.9, 1) == sep.ActivePower(multiplier=1, value=123)
    with pytest.raises(convert.ConversionError):
        convert.validate_path('DERSettings', ('nope',))
    with pytest.raises(convert.ConversionError):
        convert.set_path(sep.DERSettings(), ('setMaxW', 'deeper'), 1)
    curve = convert.to_object(sep.DERCurve, {'CurveData': [{'xvalue': 1, 'yvalue': 2}], 'curveType': 11})
    assert curve.CurveData == [sep.CurveData(xvalue=1, yvalue=2)]
    assert convert.fill_required(sep.DERCapability(), now=5) == ['modesSupported', 'rtgMaxW', 'type']
    assert convert.hex_bytes(9, 2) == b'\x00\x09' and convert.hex_bytes('', 2) == b'\x00\x00'
    # PowerFactor is a quantity too, with displacement in place of value.
    cap = sep.DERCapability()
    convert.set_path(cap, ('rtgOverExcitedPF',), 0.95, multiplier=-2)
    assert cap.rtgOverExcitedPF == sep.PowerFactor(displacement=95, multiplier=-2)
    assert convert.flatten(cap) == {'rtgOverExcitedPF': pytest.approx(0.95)}


def test_point_specs_and_readings():
    assert parse_path('DERSettings::setMaxW') == parse_path('DERSettings/setMaxW') == ('DERSettings', 'setMaxW')
    lfdi = '0' * 40
    spec = PointSpec.from_dict({'topic': 't', 'path': 'MirrorMeterReading.V.PhaseB', 'writable': True,
                                'reading': {'kind': 37, 'mrid': 'ab' * 16}}, lfdi)
    assert spec.reading.uom == 29 and spec.reading.phase == 64 and spec.reading.mrid == 'AB' * 16
    energy = ReadingSpec.from_path(('MirrorMeterReading', 'Wh'), lfdi)
    assert energy.uom == 72 and energy.kind == 12 and energy.accumulationBehaviour == 9
    assert energy.mrid == ReadingSpec.from_path(('MirrorMeterReading', 'Wh'), lfdi).mrid          # deterministic
    assert PointSpec.from_dict({'topic': 't', 'path': 'DERControlList'}, lfdi).writable is False
    with pytest.raises(ValueError):
        PointSpec.from_dict({'topic': 't', 'path': 'MirrorMeterReading', 'writable': True}, lfdi)
    assert PointSpec.from_dict({'topic': 't', 'path': 'DERSettings.setMaxW'}, lfdi).writable is True   # direction by resource
    with pytest.raises(ValueError):
        PointSpec.from_dict({'topic': 't', 'path': 'DERSettings.setMaxW', 'writable': False}, lfdi)
    with pytest.raises(ValueError):
        PointSpec.from_dict({'topic': 't', 'path': 'DERCurve.opModVoltVar.nope'}, lfdi)
    scaled = PointSpec.from_dict({'topic': 't', 'path': 'DERControl.opModTargetW', 'scaling': 0.001}, lfdi)
    assert scaled.engineering(2500) == pytest.approx(2.5) and scaled.raw(2.5) == pytest.approx(2500)
    assert scaled.engineering(True) is True and scaled.engineering([1]) == [1]
