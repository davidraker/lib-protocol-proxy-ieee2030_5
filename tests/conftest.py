"""Shared fixtures: a fake CSIP server, a controllable clock and a client wired to both."""
from __future__ import annotations

import pytest

from protocol_proxy.protocol.ieee2030_5.client import ServerClient

from tests.fake_server import LFDI, PIN, FakeSep2Server


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0):
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float):
        self.t += seconds


POINTS = [
    {'topic': 'der/DERSettings/setMaxW', 'path': 'DERSettings.setMaxW', 'writable': True},
    {'topic': 'der/DERSettings/setMaxVar', 'path': 'DERSettings::setMaxVar', 'writable': True, 'multiplier': 1},
    {'topic': 'der/DERStatus/operationalModeStatus', 'path': 'DERStatus.operationalModeStatus', 'writable': True},
    {'topic': 'der/DERStatus/connectStatus', 'path': 'DERStatus.connectStatus', 'writable': True},
    {'topic': 'der/DERCapability/rtgMaxW', 'path': 'DERCapability.rtgMaxW', 'writable': True, 'starting_value': 8000},
    {'topic': 'der/DERCapability/type', 'path': 'DERCapability.type', 'writable': True, 'starting_value': 83},
    {'topic': 'der/MirrorMeterReading/W', 'path': 'MirrorMeterReading.W', 'writable': True},
    {'topic': 'der/MirrorMeterReading/V/PhaseA', 'path': 'MirrorMeterReading.V.PhaseA', 'writable': True, 'scaling': 0.1},
    {'topic': 'der/DERControl/opModMaxLimW', 'path': 'DERControl.opModMaxLimW'},
    {'topic': 'der/DERControl/opModConnect', 'path': 'DERControl.opModConnect'},
    {'topic': 'der/DERControl/opModFixedPFInjectW/displacement', 'path': 'DERControl.opModFixedPFInjectW.displacement'},
    {'topic': 'der/DERControl/opModTargetW', 'path': 'DERControl.opModTargetW', 'scaling': 0.001},
    {'topic': 'der/DERControl/mRID', 'path': 'DERControl.mRID'},
    {'topic': 'der/DefaultDERControl/opModMaxLimW', 'path': 'DefaultDERControl.opModMaxLimW'},
    {'topic': 'der/DefaultDERControl/setGradW', 'path': 'DefaultDERControl.setGradW'},
    {'topic': 'der/DERCurve/opModVoltVar/CurveData', 'path': 'DERCurve.opModVoltVar.CurveData'},
    {'topic': 'der/DERControlList', 'path': 'DERControlList'},
    {'topic': 'der/DERProgram/primacy', 'path': 'DERProgram.primacy'},
]


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def server(clock):
    s = FakeSep2Server(existing_lfdi=LFDI, clock=clock)
    s.add_program(primacy=1)
    return s


def make_client(server: FakeSep2Server, clock, *, points=POINTS, lfdi=LFDI, pushed: list | None = None, **settings) -> ServerClient:
    async def push(values):
        if pushed is not None:
            pushed.append(values)

    settings.setdefault('pin', PIN)
    settings.setdefault('subscribe', False)
    settings.setdefault('poll_rate_floor', 1)
    client = ServerClient('https://sep2.test:8443', lfdi=lfdi, transport=server.transport(), clock=clock, push=push,
                          retries=1, **settings)
    client.configure_points(points)
    return client


@pytest.fixture
def pushed():
    return []


@pytest.fixture
async def client(server, clock, pushed):
    c = make_client(server, clock, pushed=pushed)
    c.start()
    assert await c.wait_ready(5.0), c.start_error
    yield c
    await c.close()
