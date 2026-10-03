"""An in-process CSIP server behind ``httpx.MockTransport``: enough of IEEE 2030.5 to exercise the client.

Resources live in a dict keyed by href. Lists page with the ``s``/``l`` query parameters. POSTs create EndDevices,
MirrorUsagePoints, readings, responses and subscriptions; everything received is recorded so tests can assert on it.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, fields
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from protocol_proxy.protocol.ieee2030_5.models import convert, from_xml, sep, to_xml

LFDI = '0123456789ABCDEF0123456789ABCDEF01234567'
PIN = 111115
ROOT_TAG = re.compile(rb'<([A-Za-z_][\w.]*)[\s>/]')


def parse_any(body: bytes):
    """Parse a document as the sep class named by its root element."""
    match = ROOT_TAG.search(body.split(b'?>')[-1])
    if match is None:
        return None
    name = match.group(1).decode().split(':')[-1]
    cls = getattr(sep, name, None)
    return from_xml(body, cls) if cls is not None else None


def list_field(cls) -> str:
    for f in fields(cls):
        if f.name not in ('all', 'results') and convert.field_type(cls, f.name)[1]:
            return f.name
    raise ValueError(f'{cls.__name__} has no list field')


def make_list(cls, href: str, items: list, start: int = 0, limit: int = 255, **extra):
    page = items[start:start + limit]
    return cls(href=href, all=len(items), results=len(page), **{list_field(cls): page}, **extra)


@dataclass
class Recorded:
    method: str
    path: str
    body: Any = None


@dataclass
class FakeSep2Server:
    subscriptions_supported: bool = True
    poll_rate: int | None = 10
    pin: int = PIN
    existing_lfdi: str | None = None            # pre-register an EndDevice with this LFDI
    time_offset: float = 0.0
    with_der: bool = True
    der_put_creates: bool = False          # the server lets a client declare its DER by PUT
    advertise_sub_link: bool = True        # False: /edev/N/sub exists but the EndDevice carries no SubscriptionListLink
    with_mup: bool = True
    clock: Any = time.time
    resources: dict[str, Any] = field(default_factory=dict)
    lists: dict[str, tuple[type, list]] = field(default_factory=dict)
    requests: list[Recorded] = field(default_factory=list)
    responses: list[sep.DERControlResponse] = field(default_factory=list)
    readings: list[sep.MirrorMeterReading] = field(default_factory=list)
    puts: dict[str, Any] = field(default_factory=dict)
    fail_next: dict[str, int] = field(default_factory=dict)       # path -> status to answer once
    device_counter: int = 0
    control_counter: int = 0
    sub_counter: int = 0

    def __post_init__(self):
        r, lists = self.resources, self.lists
        r['/dcap'] = sep.DeviceCapability(href='/dcap', pollRate=self.poll_rate, TimeLink=sep.TimeLink(href='/tm'),
                                          EndDeviceListLink=sep.EndDeviceListLink(href='/edev', all=0),
                                          MirrorUsagePointListLink=sep.MirrorUsagePointListLink(href='/mup', all=0) if self.with_mup else None)
        lists['/edev'] = (sep.EndDeviceList, [])
        lists['/mup'] = (sep.MirrorUsagePointList, [])
        lists['/derp'] = (sep.DERProgramList, [])
        if self.existing_lfdi:
            self.add_end_device(self.existing_lfdi)

    # ---- seeding --------------------------------------------------------------------------------------------------
    def now(self) -> int:
        return int(self.clock() + self.time_offset)

    def add_end_device(self, lfdi: str) -> str:
        self.device_counter += 1
        n = self.device_counter
        base = f'/edev/{n}'
        from protocol_proxy.protocol.ieee2030_5.identity import sfdi_from_lfdi
        device = sep.EndDevice(href=base, lFDI=bytes.fromhex(lfdi), sFDI=sfdi_from_lfdi(lfdi), changedTime=self.now(),
                               enabled=True, RegistrationLink=sep.RegistrationLink(href=f'{base}/rg'),
                               FunctionSetAssignmentsListLink=sep.FunctionSetAssignmentsListLink(href=f'{base}/fsa', all=1),
                               DERListLink=sep.DERListLink(href=f'{base}/der', all=1 if self.with_der else 0),
                               SubscriptionListLink=sep.SubscriptionListLink(href=f'{base}/sub', all=0) if self.subscriptions_supported and self.advertise_sub_link else None,
                               DeviceInformationLink=sep.DeviceInformationLink(href=f'{base}/di'))
        self.resources[base] = device
        self.lists['/edev'][1].append(device)
        self.resources[f'{base}/rg'] = sep.Registration(href=f'{base}/rg', dateTimeRegistered=self.now(), pIN=self.pin)
        fsa = sep.FunctionSetAssignments(href=f'{base}/fsa/1', mRID=bytes(16), description='fsa',
                                         DERProgramListLink=sep.DERProgramListLink(href=f'{base}/fsa/1/derp', all=1))
        self.lists[f'{base}/fsa'] = (sep.FunctionSetAssignmentsList, [fsa])
        self.lists[f'{base}/fsa/1/derp'] = (sep.DERProgramList, [])
        if not self.with_der:
            self.lists[f'{base}/der'] = (sep.DERList, [])
        if self.with_der:
            der = sep.DER(href=f'{base}/der/1', DERCapabilityLink=sep.DERCapabilityLink(href=f'{base}/der/1/dercap'),
                          DERSettingsLink=sep.DERSettingsLink(href=f'{base}/der/1/derg'),
                          DERStatusLink=sep.DERStatusLink(href=f'{base}/der/1/ders'),
                          DERAvailabilityLink=sep.DERAvailabilityLink(href=f'{base}/der/1/dera'))
            self.lists[f'{base}/der'] = (sep.DERList, [der])
        self.lists[f'{base}/sub'] = (sep.SubscriptionList, [])
        self.device_base = base
        return base

    def add_program(self, primacy: int = 1, name: str | None = None, default_base: dict | None = None,
                    device_base: str | None = None) -> str:
        device_base = device_base or getattr(self, 'device_base', None)
        n = len(self.lists['/derp'][1]) + 1
        href = name or f'/derp/{n}'
        program = sep.DERProgram(href=href, mRID=bytes([n]) * 16, description=f'program {n}', primacy=primacy,
                                 DefaultDERControlLink=sep.DefaultDERControlLink(href=f'{href}/dderc'),
                                 DERControlListLink=sep.DERControlListLink(href=f'{href}/derc', all=0),
                                 DERCurveListLink=sep.DERCurveListLink(href=f'{href}/dc', all=0))
        self.lists['/derp'][1].append(program)
        if device_base:
            self.lists[f'{device_base}/fsa/1/derp'][1].append(program)
        self.resources[f'{href}/dderc'] = sep.DefaultDERControl(
            href=f'{href}/dderc', mRID=bytes([0xDD, n]) * 8, setGradW=100,
            DERControlBase=convert.to_object(sep.DERControlBase, default_base or {'opModConnect': True, 'opModMaxLimW': 10000}))
        self.lists[f'{href}/derc'] = (sep.DERControlList, [])
        self.lists[f'{href}/dc'] = (sep.DERCurveList, [])
        return href

    def add_curve(self, program: str, points: list[tuple[int, int]], curve_type: int = 11, **attrs) -> str:
        items = self.lists[f'{program}/dc'][1]
        href = f'{program}/dc/{len(items) + 1}'
        curve = sep.DERCurve(href=href, mRID=bytes([0xCC, len(items) + 1]) * 8, creationTime=self.now(), curveType=curve_type,
                             xMultiplier=0, yMultiplier=0, yRefType=3,
                             CurveData=[sep.CurveData(xvalue=x, yvalue=y) for x, y in points], **attrs)
        items.append(curve)
        self.resources[href] = curve
        return href

    def add_control(self, program: str, start: int, duration: int, base: dict, *, mrid: bytes | None = None,
                    status: int = 0, randomize_start: int | None = None, randomize_duration: int | None = None,
                    response_required: int = 0x07, reply_to: str | None = '/rsps/1/rsp', creation_time: int | None = None) -> sep.DERControl:
        items = self.lists[f'{program}/derc'][1]
        n = len(items) + 1
        self.control_counter += 1
        control = sep.DERControl(
            href=f'{program}/derc/{n}', mRID=mrid or bytes([0xEE, self.control_counter]) * 8, creationTime=creation_time or self.now(),
            replyTo=reply_to, responseRequired=bytes([response_required]),
            EventStatus=sep.EventStatus(currentStatus=status, dateTime=self.now(), potentiallySuperseded=False),
            interval=sep.DateTimeInterval(start=start, duration=duration), randomizeStart=randomize_start,
            randomizeDuration=randomize_duration, DERControlBase=convert.to_object(sep.DERControlBase, base))
        items.append(control)
        return control

    def set_status(self, control: sep.DERControl, status: int):
        control.EventStatus = sep.EventStatus(currentStatus=status, dateTime=self.now(), potentiallySuperseded=False)

    def remove_control(self, control: sep.DERControl):
        for _, items in self.lists.values():
            if control in items:
                items.remove(control)

    # ---- transport ------------------------------------------------------------------------------------------------
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        parts = urlsplit(str(request.url))
        path, query = parts.path, parse_qs(parts.query)
        method = request.method.upper()
        body = parse_any(request.content) if request.content else None
        self.requests.append(Recorded(method, path, body))
        if path in self.fail_next:
            return httpx.Response(self.fail_next.pop(path))
        if method == 'GET':
            return self._get(path, query)
        if method == 'PUT':
            self.puts[path] = body
            self.resources[path] = body
            if isinstance(body, sep.DER) and self.der_put_creates:
                parent = path.rsplit('/', 1)[0]
                items = self.lists.setdefault(parent, (sep.DERList, []))[1]
                if not any(d.href == path for d in items):
                    body.href = path
                    items.append(body)
            return httpx.Response(204)
        if method == 'POST':
            return self._post(path, body)
        if method == 'DELETE':
            for lst in self.lists.values():
                lst[1][:] = [i for i in lst[1] if getattr(i, 'href', None) != path]
            return httpx.Response(204 if self.resources.pop(path, None) is not None else 404)
        return httpx.Response(405)

    def _get(self, path: str, query: dict) -> httpx.Response:
        if path == '/tm':
            now = self.now()
            return self._xml(sep.Time(href='/tm', currentTime=now, dstEndTime=0, dstOffset=0, dstStartTime=0, localTime=now,
                                      quality=4, tzOffset=0))
        if path in self.lists:
            cls, items = self.lists[path]
            start, limit = int(query.get('s', [0])[0]), int(query.get('l', [255])[0])
            extra = {'pollRate': self.poll_rate} if 'pollRate' in {f.name for f in fields(cls)} else {}
            return self._xml(make_list(cls, path, items, start, limit, **extra))
        if path in self.resources:
            return self._xml(self.resources[path])
        return httpx.Response(404)

    def _post(self, path: str, body) -> httpx.Response:
        if path == '/edev' and isinstance(body, sep.EndDevice):
            href = self.add_end_device(body.lFDI.hex().upper())
            return httpx.Response(201, headers={'Location': href})
        if path == '/mup' and isinstance(body, sep.MirrorUsagePoint):
            existing = [m for m in self.lists['/mup'][1] if m.mRID == body.mRID]
            href = existing[0].href if existing else f'/mup/{len(self.lists["/mup"][1]) + 1}'
            body.href = href
            if existing:
                self.lists['/mup'][1][self.lists['/mup'][1].index(existing[0])] = body
            else:
                self.lists['/mup'][1].append(body)
            self.resources[href] = body
            return httpx.Response(201, headers={'Location': href})
        if path.startswith('/mup/') and isinstance(body, sep.MirrorMeterReading):
            self.readings.append(body)
            return httpx.Response(201, headers={'Location': f'{path}/mr/{len(self.readings)}'})
        if isinstance(body, sep.DERControlResponse):
            self.responses.append(body)
            return httpx.Response(201, headers={'Location': f'{path}/{len(self.responses)}'})
        if path.endswith('/sub') and isinstance(body, sep.Subscription):
            if not self.subscriptions_supported:
                return httpx.Response(405)
            self.sub_counter += 1
            href = f'{path}/{self.sub_counter}'
            body.href = href
            self.lists.setdefault(path, (sep.SubscriptionList, []))[1].append(body)
            self.resources[href] = body
            return httpx.Response(201, headers={'Location': href})
        return httpx.Response(404)

    @staticmethod
    def _xml(obj) -> httpx.Response:
        return httpx.Response(200, content=to_xml(obj), headers={'Content-Type': 'application/sep+xml'})

    # ---- views for assertions -------------------------------------------------------------------------------------
    def subscriptions_for(self, device_base: str | None = None) -> list[sep.Subscription]:
        base = device_base or self.device_base
        return list(self.lists.get(f'{base}/sub', (None, []))[1])

    def paths(self, method: str | None = None) -> list[str]:
        return [r.path for r in self.requests if method is None or r.method == method]
