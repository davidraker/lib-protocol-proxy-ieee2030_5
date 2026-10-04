"""One IEEE 2030.5 (CSIP) server served by the proxy (the ``server`` role): the inverse of :mod:`client`.

The served values are a tree of sep resources held in this process (``resources`` by href, ``lists`` by href), the
same shape the client expects from a utility server: ``/dcap`` with Time, EndDeviceList, DERProgramList and
MirrorUsagePointList links; one DERProgram (``/derp/1``) with a DefaultDERControl, a DERControlList and a DERCurveList;
an EndDevice per DER client (pre-registered from the configuration or created when a client registers) with its
Registration (PIN), FunctionSetAssignments, DER (the four upward PUT targets), DeviceInformation and SubscriptionList.

The platform writes the controls (``DefaultDERControl.*``, ``DERControl.*`` as an immediate event, ``DERControlList``
as the schedule, ``DERCurve.<attribute>.*``, ``DERProgram.*``); the DER client writes DERStatus, DERSettings,
DERCapability, DERAvailability, DeviceInformation and MirrorMeterReadings, which are flattened into the convention and
pushed to the caller as they arrive, along with the DERControlResponses the client posts. Subscribed clients are
notified when a control resource changes.

Everything persists in a JSON state file (``state_dir``): registrations, subscriptions, controls, curves, the last
values received, so the served server survives a proxy restart with its clients still registered and subscribed.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .httpserver import Sep2HttpServer
from .identity import normalize_lfdi, sfdi_from_lfdi
from .models import convert, from_xml, sep, to_xml
from .points import PHASE_CODES, UOM_BY_UNIT, PointSpec
from .transport import DEFAULT_CIPHERS, Sep2Http, Sep2HttpError, make_ssl_context

_log = logging.getLogger(__name__)

PushCallback = Callable[[dict[str, Any]], Awaitable[None]]

SCHEDULED, ACTIVE, CANCELLED, SUPERSEDED, COMPLETE = 0, 1, 2, 4, 5
STATUS_NAMES = {SCHEDULED: 'scheduled', ACTIVE: 'active', CANCELLED: 'cancelled', 3: 'cancelled', SUPERSEDED: 'superseded',
                COMPLETE: 'complete'}
RESPONSE_REQUIRED_ALL = b'\x07'
PROGRAM, DDERC, DERC, CURVES, RESPONSES = '/derp/1', '/derp/1/dderc', '/derp/1/derc', '/derp/1/dc', '/rsps/1/rsp'
UPWARD_LINKS = {'dercap': 'DERCapability', 'derg': 'DERSettings', 'ders': 'DERStatus', 'dera': 'DERAvailability'}
UPWARD_CLASSES = {'DERCapability': sep.DERCapability, 'DERSettings': sep.DERSettings, 'DERStatus': sep.DERStatus,
                  'DERAvailability': sep.DERAvailability, 'DeviceInformation': sep.DeviceInformation}
#: DERCurveType by the DERControlBase attribute that links a curve (IEEE 2030.5 Table 98).
CURVE_TYPES = {'opModFreqWatt': 0, 'opModHFRTMayTrip': 1, 'opModHFRTMustTrip': 2, 'opModHVRTMayTrip': 3,
               'opModHVRTMomentaryCessation': 4, 'opModHVRTMustTrip': 5, 'opModLFRTMayTrip': 6, 'opModLFRTMustTrip': 7,
               'opModLVRTMayTrip': 8, 'opModLVRTMomentaryCessation': 9, 'opModLVRTMustTrip': 10, 'opModVoltVar': 11,
               'opModVoltWatt': 12, 'opModWattPF': 13, 'opModWattVar': 14}
UNIT_BY_UOM = {}
for _unit, _uom in UOM_BY_UNIT.items():
    UNIT_BY_UOM.setdefault(_uom, _unit)
PHASE_BY_CODE = {code: name for name, code in PHASE_CODES.items()}
STATE_VERSION = 1


def _hex(value: bytes | None) -> str:
    return convert.hex_text(value) or ''


def _mrid() -> bytes:
    return uuid.uuid4().bytes


class ServedSep2Server:
    """A CSIP server on ``host:port`` serving one DER program to the DER clients that register with it."""

    def __init__(self, host: str, port: int, *, cert_path: str | None = None, key_path: str | None = None,
                 ca_path: str | None = None, tls: bool = True, client_auth: bool = True, tls12_only: bool = False,
                 ciphers=DEFAULT_CIPHERS, poll_rate: int = 60, primacy: int = 1, client_lfdi: str | None = None,
                 client_pin: int | None = None, register_unknown_clients: bool = True,
                 immediate_control_duration: int = 3600, state_dir: str | None = None, notify_tls_verify: bool = False,
                 response_timeout: float = 10.0, description: str = 'VOLTTRON', push: PushCallback | None = None,
                 clock: Callable[[], float] = time.time, remote_id=None):
        self.host, self.port = host, int(port)
        self.cert_path, self.key_path, self.ca_path = cert_path, key_path, ca_path
        self.tls, self.client_auth, self.tls12_only, self.ciphers = bool(tls), bool(client_auth), bool(tls12_only), tuple(ciphers or ())
        self.poll_rate, self.primacy = int(poll_rate), int(primacy)
        self.client_lfdi = normalize_lfdi(client_lfdi) if client_lfdi else None
        self.client_pin = None if client_pin in (None, '') else int(client_pin)
        self.register_unknown_clients = bool(register_unknown_clients)
        self.immediate_control_duration = int(immediate_control_duration)
        self.notify_tls_verify, self.response_timeout, self.description = bool(notify_tls_verify), float(response_timeout), description
        self.push, self.clock, self.remote_id = push, clock, remote_id
        self.state_path = Path(state_dir) / f'sep2_server_{host}_{port}.json' if state_dir else None

        self.resources: dict[str, Any] = {}
        self.lists: dict[str, tuple[type, list]] = {}
        self.devices: dict[str, str] = {}                  # LFDI -> EndDevice href
        self.pins: dict[str, int] = {}                     # EndDevice href -> PIN
        self.counters = {'edev': 0, 'mup': 0, 'derc': 0, 'dc': 0, 'sub': 0, 'rsp': 0, 'mr': 0}
        self.received: dict[str, Any] = {}                 # convention path -> value the DER client reported
        self.responses: dict[str, list[dict]] = {}         # control mRID -> DERControlResponses
        self.reading_types: dict[str, sep.ReadingType] = {}   # reading mRID -> ReadingType from the MirrorUsagePoint
        self.curves: dict[str, str] = {}                   # curve attribute (opModVoltVar) -> DERCurve href
        self.immediate_href: str | None = None
        self.points: dict[str, PointSpec] = {}
        self.by_path: dict[str, list[str]] = {}
        self.seeded = 0
        self.notifications_sent = 0
        self._notifiers: dict[str, Sep2Http] = {}
        self._save_handle: asyncio.TimerHandle | None = None
        self.server: Sep2HttpServer | None = None
        if self.state_path is not None and self.state_path.exists():
            self._load()
        self._ensure_base()

    # ---- the resource tree --------------------------------------------------------------------------------------
    def now(self) -> int:
        return int(self.clock())

    def _ensure_base(self):
        r, lists = self.resources, self.lists
        r['/dcap'] = sep.DeviceCapability(href='/dcap', pollRate=self.poll_rate, TimeLink=sep.TimeLink(href='/tm'),
                                          EndDeviceListLink=sep.EndDeviceListLink(href='/edev', all=0),
                                          DERProgramListLink=sep.DERProgramListLink(href='/derp', all=1),
                                          MirrorUsagePointListLink=sep.MirrorUsagePointListLink(href='/mup', all=0))
        lists.setdefault('/edev', (sep.EndDeviceList, []))
        lists.setdefault('/mup', (sep.MirrorUsagePointList, []))
        lists.setdefault(RESPONSES, (sep.ResponseList, []))
        if PROGRAM not in r:
            r[PROGRAM] = sep.DERProgram(href=PROGRAM, mRID=hashlib.sha256(b'volttron:derp:1').digest()[:16],
                                        description=self.description[:32], primacy=self.primacy,
                                        DefaultDERControlLink=sep.DefaultDERControlLink(href=DDERC),
                                        DERControlListLink=sep.DERControlListLink(href=DERC, all=0),
                                        DERCurveListLink=sep.DERCurveListLink(href=CURVES, all=0))
        lists.setdefault('/derp', (sep.DERProgramList, [r[PROGRAM]]))
        if DDERC not in r:
            r[DDERC] = sep.DefaultDERControl(href=DDERC, mRID=hashlib.sha256(b'volttron:dderc:1').digest()[:16],
                                             DERControlBase=sep.DERControlBase())
        lists.setdefault(DERC, (sep.DERControlList, []))
        lists.setdefault(CURVES, (sep.DERCurveList, []))
        if self.client_lfdi and self.client_lfdi not in self.devices:
            self.add_end_device(self.client_lfdi, self.client_pin)

    def add_end_device(self, lfdi: str, pin: int | None = None) -> str:
        """Create the EndDevice tree for a DER client; returns its href."""
        lfdi = normalize_lfdi(lfdi)
        if lfdi in self.devices:
            return self.devices[lfdi]
        self.counters['edev'] += 1
        n = self.counters['edev']
        base = f'/edev/{n}'
        now = self.now()
        device = sep.EndDevice(href=base, lFDI=bytes.fromhex(lfdi), sFDI=sfdi_from_lfdi(lfdi), changedTime=now, enabled=True,
                               RegistrationLink=sep.RegistrationLink(href=f'{base}/rg'),
                               FunctionSetAssignmentsListLink=sep.FunctionSetAssignmentsListLink(href=f'{base}/fsa', all=1),
                               DERListLink=sep.DERListLink(href=f'{base}/der', all=1),
                               DeviceInformationLink=sep.DeviceInformationLink(href=f'{base}/di'),
                               SubscriptionListLink=sep.SubscriptionListLink(href=f'{base}/sub', all=0))
        self.resources[base] = device
        self.lists['/edev'][1].append(device)
        pin = self.client_pin if pin is None and lfdi == self.client_lfdi else pin
        if pin is None:
            pin = int(hashlib.sha256(lfdi.encode()).hexdigest()[:5], 16) % 100000 * 10
            pin += _luhn(pin // 10)
        self.pins[base] = int(pin)
        self.resources[f'{base}/rg'] = sep.Registration(href=f'{base}/rg', dateTimeRegistered=now, pIN=int(pin))
        fsa = sep.FunctionSetAssignments(href=f'{base}/fsa/1', mRID=hashlib.sha256(f'{lfdi}:fsa'.encode()).digest()[:16],
                                         description='VOLTTRON', DERProgramListLink=sep.DERProgramListLink(href='/derp', all=1))
        self.lists[f'{base}/fsa'] = (sep.FunctionSetAssignmentsList, [fsa])
        der_base = f'{base}/der/1'
        der = sep.DER(href=der_base, DERCapabilityLink=sep.DERCapabilityLink(href=f'{der_base}/dercap'),
                      DERSettingsLink=sep.DERSettingsLink(href=f'{der_base}/derg'),
                      DERStatusLink=sep.DERStatusLink(href=f'{der_base}/ders'),
                      DERAvailabilityLink=sep.DERAvailabilityLink(href=f'{der_base}/dera'))
        self.lists[f'{base}/der'] = (sep.DERList, [der])
        self.resources[der_base] = der
        self.lists[f'{base}/sub'] = (sep.SubscriptionList, [])
        self.devices[lfdi] = base
        self._schedule_save()
        return base

    def primary_device(self) -> str | None:
        """The EndDevice whose data maps to the registered points: the configured client, else the first registered."""
        if self.client_lfdi and self.client_lfdi in self.devices:
            return self.devices[self.client_lfdi]
        return next(iter(self.devices.values()), None)

    # ---- configuration ------------------------------------------------------------------------------------------
    def configure_points(self, specs: list[dict]) -> int:
        points = [PointSpec.from_dict(spec, self.client_lfdi or '', served=True) for spec in specs]
        self.points = {p.topic: p for p in points}
        self.by_path = {}
        for p in points:
            self.by_path.setdefault(p.dotted, []).append(p.topic)
        return len(self.points)

    def describe(self) -> dict:
        return {'role': 'server', 'host': self.host, 'port': self.bound_port, 'tls': self.tls, 'points': len(self.points),
                'devices': dict(self.devices), 'primary_device': self.primary_device(),
                'subscriptions': sum(len(items) for href, (_, items) in self.lists.items() if href.endswith('/sub')),
                'controls': [self._entry(c) for c in self.lists[DERC][1]], 'curves': dict(self.curves),
                'notifications_sent': self.notifications_sent, 'listening': self.server.is_serving if self.server else False,
                'state_file': str(self.state_path) if self.state_path else None}

    @property
    def bound_port(self) -> int:
        return self.server.bound_port if self.server is not None else self.port

    # ---- lifecycle ----------------------------------------------------------------------------------------------
    async def start(self):
        if self.server is not None and self.server.is_serving:
            return
        ctx = None
        if self.tls:
            if not self.cert_path:
                raise ValueError('A served 2030.5 server needs cert_path and key_path (or tls: false).')
            ctx = make_ssl_context(self.cert_path, self.key_path, self.ca_path, server_side=True, client_auth=self.client_auth,
                                   tls12_only=self.tls12_only, ciphers=self.ciphers)
        self.server = Sep2HttpServer(self.handle, host=self.host, port=self.port, ssl_context=ctx)
        await self.server.start()

    async def close(self):
        if self._save_handle is not None:
            self._save_handle.cancel()
            self._save_handle = None
            self._save()
        if self.server is not None:
            await self.server.stop()
            self.server = None
        for http in self._notifiers.values():
            await http.aclose()
        self._notifiers = {}

    # ---- HTTP -----------------------------------------------------------------------------------------------------
    async def handle(self, method: str, path: str, query: dict, headers: dict, body: bytes):
        path = path.rstrip('/') or '/'
        obj = None
        if body:
            try:
                obj = _parse_any(body)
            except Exception as e:
                _log.warning(f'{method} {path}: undecodable body: {e}')
                return 400, None, {}
        if method == 'GET':
            return self._get(path, query)
        if method == 'PUT':
            return await self._put(path, obj)
        if method == 'POST':
            return await self._post(path, obj)
        if method == 'DELETE':
            return self._delete(path)
        return 405, None, {}

    def _xml(self, obj) -> tuple[int, bytes, dict]:
        return 200, to_xml(obj), {}

    def _get(self, path: str, query: dict):
        if path == '/tm':
            now = self.now()
            return self._xml(sep.Time(href='/tm', currentTime=now, dstEndTime=0, dstOffset=0, dstStartTime=0, localTime=now,
                                      quality=4, tzOffset=0))
        if path == DERC:
            self._refresh_statuses()
        if path in self.lists:
            cls, items = self.lists[path]
            start, limit = int(query.get('s', 0)), int(query.get('l', 255))
            page = items[start:start + limit]
            extra = {'pollRate': self.poll_rate} if 'pollRate' in convert.field_names(cls) else {}
            return self._xml(cls(href=path, all=len(items), results=len(page), **{_list_field(cls): page}, **extra))
        if path in self.resources:
            return self._xml(self.resources[path])
        return 404, None, {}

    async def _put(self, path: str, obj):
        if obj is None:
            return 400, None, {}
        parts = path.split('/')
        # /edev/{n}/der/{k}: a client declaring its DER (ours already exists; accept the declaration anyway).
        if len(parts) == 5 and parts[1] == 'edev' and parts[3] == 'der' and isinstance(obj, sep.DER):
            obj.href = path
            self.resources[path] = obj
            items = self.lists.setdefault(f'/{parts[1]}/{parts[2]}/der', (sep.DERList, []))[1]
            if not any(d.href == path for d in items):
                items.append(obj)
            self._schedule_save()
            return 204, None, {}
        resource = None
        if len(parts) == 6 and parts[1] == 'edev' and parts[3] == 'der' and parts[5] in UPWARD_LINKS:
            resource = UPWARD_LINKS[parts[5]]
        elif len(parts) == 4 and parts[1] == 'edev' and parts[3] == 'di':
            resource = 'DeviceInformation'
        if resource is None or not isinstance(obj, UPWARD_CLASSES[resource]):
            return 404 if resource is None else 400, None, {}
        device = f'/{parts[1]}/{parts[2]}'
        if device not in self.resources:
            return 404, None, {}
        obj.href = path
        self.resources[path] = obj
        if device == self.primary_device():
            flat = {f'{resource}.{k}': v for k, v in convert.flatten(obj).items()}
            self.received.update(flat)
            await self._push_paths(flat)
        self._schedule_save()
        return 204, None, {}

    async def _post(self, path: str, obj):
        if obj is None:
            return 400, None, {}
        if path == '/edev' and isinstance(obj, sep.EndDevice):
            if obj.lFDI is None:
                return 400, None, {}
            lfdi = _hex(obj.lFDI)
            if lfdi in self.devices:
                return 201, None, {'Location': self.devices[lfdi]}
            if not self.register_unknown_clients and lfdi != self.client_lfdi:
                _log.warning(f'Refusing registration of unknown DER client {lfdi}.')
                return 403, None, {}
            href = self.add_end_device(lfdi)
            _log.info(f'DER client {lfdi} registered as {href}.')
            return 201, None, {'Location': href}
        if path == '/mup' and isinstance(obj, sep.MirrorUsagePoint):
            items = self.lists['/mup'][1]
            existing = next((m for m in items if m.mRID == obj.mRID), None)
            if existing is not None:
                obj.href = existing.href
                items[items.index(existing)] = obj
            else:
                self.counters['mup'] += 1
                obj.href = f'/mup/{self.counters["mup"]}'
                items.append(obj)
            self.resources[obj.href] = obj
            for reading in obj.MirrorMeterReading or []:
                if reading.mRID is not None and reading.ReadingType is not None:
                    self.reading_types[_hex(reading.mRID)] = reading.ReadingType
            self._schedule_save()
            return 201, None, {'Location': obj.href}
        if path.startswith('/mup/') and isinstance(obj, sep.MirrorMeterReading):
            mup = self.resources.get(path)
            if mup is None:
                return 404, None, {}
            self.counters['mr'] += 1
            await self._reading_received(mup, obj)
            return 201, None, {'Location': f'{path}/mr/{self.counters["mr"]}'}
        if path.startswith('/rsps') and isinstance(obj, (sep.DERControlResponse, sep.Response)):
            self.counters['rsp'] += 1
            obj.href = f'{RESPONSES}/{self.counters["rsp"]}'
            self.lists[RESPONSES][1].append(obj)
            mrid = _hex(obj.subject)
            self.responses.setdefault(mrid, []).append({'status': obj.status, 'time': obj.createdDateTime,
                                                        'lfdi': _hex(obj.endDeviceLFDI)})
            _log.info(f'DERControlResponse {obj.status} for control {mrid} from {_hex(obj.endDeviceLFDI)}.')
            self._schedule_save()
            await self._push_paths({'DERControlList': self._schedule_view()})
            return 201, None, {'Location': obj.href}
        if path.endswith('/sub') and isinstance(obj, sep.Subscription):
            if path not in self.lists:
                return 404, None, {}
            items = self.lists[path][1]
            # A client that lost track of its subscriptions (a restart on either side) subscribes again: replace rather
            # than duplicate, so each change is notified once.
            same = next((s for s in items if s.subscribedResource == obj.subscribedResource
                         and s.notificationURI == obj.notificationURI), None)
            if same is not None:
                obj.href = same.href
                items[items.index(same)] = obj
            else:
                self.counters['sub'] += 1
                obj.href = f'{path}/{self.counters["sub"]}'
                items.append(obj)
            self.resources[obj.href] = obj
            self._schedule_save()
            _log.info(f'Subscription {obj.href} to {obj.subscribedResource} -> {obj.notificationURI}')
            return 201, None, {'Location': obj.href}
        return 404, None, {}

    def _delete(self, path: str):
        if path not in self.resources:
            return 404, None, {}
        obj = self.resources.pop(path)
        for cls, items in self.lists.values():
            if obj in items:
                items.remove(obj)
        self._schedule_save()
        return 204, None, {}

    # ---- the DER client's data ------------------------------------------------------------------------------------
    async def _reading_received(self, mup: sep.MirrorUsagePoint, reading: sep.MirrorMeterReading):
        mrid = _hex(reading.mRID)
        reading_type = reading.ReadingType or self.reading_types.get(mrid)
        if reading.Reading is None or reading.Reading.value is None:
            return
        multiplier = (reading_type.powerOfTenMultiplier or 0) if reading_type is not None else 0
        value = reading.Reading.value * 10 ** multiplier
        paths = [spec.dotted for spec in self.points.values()
                 if spec.reading is not None and spec.reading.mrid == mrid]
        if not paths and reading_type is not None:
            unit = UNIT_BY_UOM.get(reading_type.uom or 0)
            if unit:
                path = f'MirrorMeterReading.{unit}'
                phase = PHASE_BY_CODE.get(reading_type.phase or 0)
                paths = [f'{path}.{phase}' if phase else path]
        if not paths:
            _log.debug(f'Reading {mrid} has no unit this server maps; ignored.')
            return
        if mup.deviceLFDI is not None and self.primary_device() not in (None, self.devices.get(_hex(mup.deviceLFDI))):
            return                                           # another client's mirror
        flat = {path: value for path in paths}
        self.received.update(flat)
        self._schedule_save()
        await self._push_paths(flat)

    async def _push_paths(self, view: dict[str, Any]):
        values: dict[str, Any] = {}
        for path, value in view.items():
            for topic in self.by_path.get(path, ()):
                spec = self.points[topic]
                if spec.writable:
                    continue                                 # the platform's own served values are reflected by the caller
                values[topic] = spec.engineering(value)
        if values and self.push is not None:
            try:
                await self.push(values)
            except Exception as e:    # pragma: no cover
                _log.warning(f'{self.host}:{self.port}: unable to push received values: {e}')

    async def push_all(self) -> dict[str, Any]:
        """Push every value this server holds for the registered points: served and received."""
        values: dict[str, Any] = {}
        for topic, spec in self.points.items():
            value = self._served_value(spec) if spec.writable else self._received_value(spec)
            if value is not None:
                values[topic] = value
        if values and self.push is not None:
            await self.push(values)
        return values

    # ---- served values: reads and the platform's writes --------------------------------------------------------
    def _control_path(self, attrs: tuple[str, ...]) -> tuple[str, ...]:
        """The convention flattens DERControlBase into the control: DERControl.opModMaxLimW."""
        return ('DERControlBase', *attrs) if attrs and attrs[0] in convert.field_names(sep.DERControlBase) else attrs

    def _served_value(self, spec: PointSpec):
        resource, attrs = spec.resource, spec.attribute
        if resource == 'DERControlList':
            return self._schedule_view()
        if resource == 'DefaultDERControl':
            obj = self.resources[DDERC]
        elif resource == 'DERControl':
            obj = self.resources.get(self.immediate_href) if self.immediate_href else None
        elif resource == 'DERProgram':
            obj = self.resources[PROGRAM]
        elif resource == 'DERCurve':
            href = self.curves.get(attrs[0]) if attrs else None
            obj, attrs = (self.resources.get(href) if href else None), attrs[1:]
        else:
            return None
        if obj is None:
            return None
        if resource in ('DERControl', 'DefaultDERControl'):
            attrs = self._control_path(attrs)
        value = convert.get_path(obj, attrs) if attrs else convert.flatten(obj)
        if value is None:
            return None
        if hasattr(value, '__dataclass_fields__'):
            if convert.is_quantity(type(value)):
                return spec.engineering(convert.quantity_value(value))
            if convert.is_link(type(value)):
                return value.href
            return convert.flatten(value)
        if isinstance(value, (bytes, bytearray)):
            return convert.hex_text(value)
        if isinstance(value, list):
            return [convert.flatten(v) if hasattr(v, '__dataclass_fields__') else v for v in value]
        return spec.engineering(value)

    def _received_value(self, spec: PointSpec):
        value = self.received.get(spec.dotted)
        return None if value is None else spec.engineering(value)

    async def read(self, topics: list[str] | None, refresh: bool = False) -> tuple[dict, dict]:
        self._refresh_statuses()
        topics = list(topics) if topics is not None else list(self.points)
        results, errors = {}, {}
        for topic in topics:
            spec = self.points.get(topic)
            if spec is None:
                errors[topic] = 'unregistered topic'
            else:
                results[topic] = self._served_value(spec) if spec.writable else self._received_value(spec)
        return results, errors

    async def write(self, values: dict[str, Any]) -> tuple[dict, dict]:
        """Store the platform's values: controls, the schedule, curves and the program. One request, one immediate
        control for every ``DERControl.*`` value in it."""
        results, errors = {}, {}
        changed: set[str] = set()
        immediate: dict[tuple[str, ...], tuple[PointSpec, Any]] = {}
        for topic, value in values.items():
            spec = self.points.get(topic)
            if spec is None:
                errors[topic] = 'unregistered topic'
                continue
            if not spec.writable:
                errors[topic] = f'{spec.dotted} is reported by the DER client and cannot be written here'
                continue
            try:
                if spec.resource == 'DefaultDERControl':
                    self._set_control_attr(self.resources[DDERC], spec.attribute, spec.raw(value), spec.multiplier)
                    changed.add(DDERC)
                elif spec.resource == 'DERControl':
                    immediate[spec.attribute] = (spec, value)
                elif spec.resource == 'DERControlList':
                    self._set_schedule(value)
                    changed.add(DERC)
                elif spec.resource == 'DERCurve':
                    self._set_curve(spec.attribute, spec.raw(value), spec.multiplier)
                    changed.add(CURVES)
                elif spec.resource == 'DERProgram':
                    convert.set_path(self.resources[PROGRAM], spec.attribute, spec.raw(value), multiplier=spec.multiplier)
                    changed.add('/derp')
                else:
                    errors[topic] = f'{spec.resource} cannot be written'
                    continue
            except (convert.ConversionError, ValueError, TypeError, KeyError) as e:
                errors[topic] = f'invalid value: {e}'
                continue
            results[topic] = {'status': 'stored', 'resource': spec.resource, 'value': value}
        if immediate:
            try:
                self._set_immediate({attrs: (spec.raw(v), spec.multiplier) for attrs, (spec, v) in immediate.items()})
                changed.add(DERC)
            except (convert.ConversionError, ValueError, TypeError) as e:
                for spec, value in immediate.values():
                    results.pop(spec.topic, None)
                    errors[spec.topic] = f'invalid value: {e}'
        if changed:
            self._bump(changed)
            self._schedule_save()
            self._notify(changed)
        return results, errors

    async def seed(self, values: dict[str, Any]) -> int:
        """Give served points that hold nothing yet a value: the caller's current value when it sent one (the platform's
        memory, after a proxy restart without a state file), else the row's starting value."""
        pending = {}
        for topic, spec in self.points.items():
            if not spec.writable or self._served_value(spec) not in (None, [], {}):
                continue
            if topic in values and values[topic] is not None:
                pending[topic] = values[topic]
            elif spec.starting_value not in (None, ''):
                pending[topic] = spec.starting_value
        if not pending:
            self.seeded = 0
            return 0
        results, errors = await self.write(pending)
        for topic, error in errors.items():
            _log.warning(f'Seed value for {topic} ignored: {error}')
        self.seeded = len(results)
        return self.seeded

    def _set_control_attr(self, control, attrs: tuple[str, ...], value, multiplier: int):
        path = self._control_path(attrs)
        if len(path) == 2 and path[0] == 'DERControlBase':
            t, _ = convert.field_type(sep.DERControlBase, path[1])
            if t is sep.DERCurveLink and not isinstance(value, str):
                if not value:
                    convert.set_path(control, path, None)
                    return
                href = self.curves.get(path[1])
                if href is None:
                    raise ValueError(f'no {path[1]} curve has been written yet (DERCurve.{path[1]}.CurveData)')
                value = href
        convert.set_path(control, path, value, multiplier=multiplier, now=self.now())

    def _set_curve(self, attrs: tuple[str, ...], value, multiplier: int):
        if not attrs or attrs[0] not in CURVE_TYPES:
            raise ValueError(f'DERCurve rows are keyed by the curve attribute of DERControlBase, not {attrs!r}')
        attribute = attrs[0]
        href = self.curves.get(attribute)
        if href is None:
            self.counters['dc'] += 1
            href = f'{CURVES}/{self.counters["dc"]}'
            curve = sep.DERCurve(href=href, mRID=_mrid(), description=attribute[:32], creationTime=self.now(),
                                 curveType=CURVE_TYPES[attribute], xMultiplier=0, yMultiplier=0, yRefType=0, CurveData=[])
            self.resources[href] = curve
            self.lists[CURVES][1].append(curve)
            self.curves[attribute] = href
        curve = self.resources[href]
        if len(attrs) == 1:
            if not isinstance(value, dict):
                raise ValueError('a whole DERCurve is written as a mapping of its attributes')
            for key, item in value.items():
                convert.set_path(curve, key.split('.'), item, multiplier=multiplier, now=self.now())
        else:
            convert.set_path(curve, attrs[1:], value, multiplier=multiplier, now=self.now())
        curve.creationTime = self.now()
        # Controls that name this curve attribute now link to this curve.
        for control in [self.resources[DDERC], *self.lists[DERC][1]]:
            base = control.DERControlBase
            if base is not None and getattr(base, attribute, None) is not None:
                setattr(base, attribute, sep.DERCurveLink(href=href))

    def _set_immediate(self, values: dict[tuple[str, ...], tuple[Any, int]]):
        now = self.now()
        previous = self.resources.get(self.immediate_href) if self.immediate_href else None
        control = sep.DERControl(mRID=_mrid(), description='immediate', creationTime=now, replyTo=RESPONSES,
                                 responseRequired=RESPONSE_REQUIRED_ALL,
                                 EventStatus=sep.EventStatus(currentStatus=ACTIVE, dateTime=now, potentiallySuperseded=False),
                                 interval=sep.DateTimeInterval(start=now, duration=self.immediate_control_duration),
                                 DERControlBase=sep.DERControlBase())
        for attrs, (value, multiplier) in values.items():
            self._set_control_attr(control, attrs, value, multiplier)
        self._add_control(control)
        self.immediate_href = control.href
        if previous is not None:
            self._end_control(previous, SUPERSEDED)

    def _add_control(self, control: sep.DERControl):
        self.counters['derc'] += 1
        control.href = f'{DERC}/{self.counters["derc"]}'
        self.lists[DERC][1].append(control)
        self.resources[control.href] = control

    def _end_control(self, control: sep.DERControl, status: int):
        if control.EventStatus is None or control.EventStatus.currentStatus not in (CANCELLED, 3, SUPERSEDED, COMPLETE):
            control.EventStatus = sep.EventStatus(currentStatus=status, dateTime=self.now(), potentiallySuperseded=False)
            control.description = f'{control.description or ""}'[:32]

    def _set_schedule(self, entries):
        """Replace the scheduled events with ``entries``: ``[{mRID?, interval: {start, duration}, DERControl: {...}}]``.
        Events that stay keep their mRID and status; events that disappear are cancelled."""
        if entries is None:
            entries = []
        if isinstance(entries, str):
            entries = json.loads(entries)
        if not isinstance(entries, list):
            raise ValueError('DERControlList is written as a list of events')
        now = self.now()
        wanted: dict[str, sep.DERControl] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError('each DERControlList entry is a mapping')
            interval = entry.get('interval') or {}
            start, duration = int(interval.get('start', now)), int(interval.get('duration', self.immediate_control_duration))
            base_values = entry.get('DERControl') or {}
            mrid_text = entry.get('mRID') or hashlib.sha256(
                json.dumps([start, duration, sorted(base_values.items())], sort_keys=True, default=str).encode()).hexdigest()[:32]
            mrid = convert.hex_bytes(mrid_text, 16)
            existing = next((c for c in self.lists[DERC][1] if c.mRID == mrid), None)
            if existing is not None and existing.EventStatus.currentStatus in (SCHEDULED, ACTIVE):
                control = existing
            else:
                control = sep.DERControl(mRID=mrid, description=str(entry.get('description', 'scheduled'))[:32], creationTime=now,
                                         replyTo=RESPONSES, responseRequired=RESPONSE_REQUIRED_ALL,
                                         EventStatus=sep.EventStatus(currentStatus=SCHEDULED, dateTime=now, potentiallySuperseded=False),
                                         interval=sep.DateTimeInterval(start=start, duration=duration), DERControlBase=sep.DERControlBase())
                self._add_control(control)
            control.interval = sep.DateTimeInterval(start=start, duration=duration)
            for key in ('randomizeStart', 'randomizeDuration'):
                if key in entry:
                    setattr(control, key, int(entry[key]))
            control.DERControlBase = sep.DERControlBase()
            for key, value in base_values.items():
                self._set_control_attr(control, tuple(key.split('.')), value, 0)
            wanted[_hex(mrid)] = control
        for control in self.lists[DERC][1]:
            if control.href != self.immediate_href and _hex(control.mRID) not in wanted:
                self._end_control(control, CANCELLED)
        self._refresh_statuses()

    def _refresh_statuses(self):
        """Move scheduled events to active and active ones to complete by the clock; drop long-finished ones."""
        now = self.now()
        keep = []
        for control in self.lists[DERC][1]:
            status = control.EventStatus.currentStatus if control.EventStatus else SCHEDULED
            start = control.interval.start if control.interval else now
            end = start + (control.interval.duration if control.interval else 0)
            if status == SCHEDULED and start <= now < end:
                control.EventStatus = sep.EventStatus(currentStatus=ACTIVE, dateTime=now, potentiallySuperseded=False)
            elif status in (SCHEDULED, ACTIVE) and now >= end:
                control.EventStatus = sep.EventStatus(currentStatus=COMPLETE, dateTime=now, potentiallySuperseded=False)
                if control.href == self.immediate_href:
                    self.immediate_href = None
            status = control.EventStatus.currentStatus
            if status in (CANCELLED, 3, SUPERSEDED, COMPLETE) and now - control.EventStatus.dateTime > max(2 * self.poll_rate, 120):
                self.resources.pop(control.href, None)
                if control.href == self.immediate_href:
                    self.immediate_href = None
                continue
            keep.append(control)
        if len(keep) != len(self.lists[DERC][1]):
            self.lists[DERC] = (sep.DERControlList, keep)
            self._schedule_save()

    def _entry(self, control: sep.DERControl) -> dict:
        status = control.EventStatus.currentStatus if control.EventStatus else SCHEDULED
        mrid = _hex(control.mRID)
        return {'mRID': mrid, 'href': control.href, 'status': STATUS_NAMES.get(status, str(status)),
                'interval': {'start': control.interval.start, 'duration': control.interval.duration} if control.interval else None,
                'DERControl': convert.flatten(control.DERControlBase) if control.DERControlBase else {},
                'immediate': control.href == self.immediate_href, 'responses': list(self.responses.get(mrid, []))}

    def _schedule_view(self) -> list[dict]:
        return [self._entry(c) for c in self.lists[DERC][1]]

    def _bump(self, hrefs: set[str]):
        for href in hrefs:
            obj = self.resources.get(href)
            if obj is not None and hasattr(obj, 'version'):
                obj.version = (obj.version or 0) + 1
        self.resources[PROGRAM].DERControlListLink = sep.DERControlListLink(href=DERC, all=len(self.lists[DERC][1]))
        self.resources[PROGRAM].DERCurveListLink = sep.DERCurveListLink(href=CURVES, all=len(self.lists[CURVES][1]))

    # ---- notifications ------------------------------------------------------------------------------------------
    def _notify(self, changed: set[str]):
        for href, (cls, items) in list(self.lists.items()):
            if not href.endswith('/sub'):
                continue
            for subscription in items:
                target = subscription.subscribedResource
                if target in changed or (target == '/derp' and changed & {DDERC, DERC, CURVES}):
                    asyncio.get_running_loop().create_task(self._send_notification(subscription))

    async def _send_notification(self, subscription: sep.Subscription):
        uri = subscription.notificationURI
        parts = urlsplit(uri)
        base = f'{parts.scheme}://{parts.netloc}'
        http = self._notifiers.get(base)
        if http is None:
            http = Sep2Http(base, cert_path=self.cert_path, key_path=self.key_path, ca_path=self.ca_path,
                            tls_verify=self.notify_tls_verify, tls12_only=self.tls12_only, ciphers=self.ciphers or DEFAULT_CIPHERS,
                            timeout=self.response_timeout, retries=2)
            self._notifiers[base] = http
        notification = sep.Notification(subscribedResource=subscription.subscribedResource, status=0,
                                        subscriptionURI=subscription.href)
        try:
            await http.post(parts.path or '/notify', notification)
            self.notifications_sent += 1
        except Sep2HttpError as e:
            _log.warning(f'Notification to {uri} for {subscription.subscribedResource} failed: {e}')

    # ---- persistence --------------------------------------------------------------------------------------------
    def _schedule_save(self):
        if self.state_path is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._save()
            return
        if self._save_handle is None:
            self._save_handle = loop.call_later(0.5, self._save_now)

    def _save_now(self):
        self._save_handle = None
        self._save()

    def _save(self):
        if self.state_path is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix('.tmp')
            tmp.write_text(json.dumps(self.state(), indent=1))
            tmp.replace(self.state_path)
        except Exception as e:
            _log.warning(f'Unable to save the served 2030.5 state to {self.state_path}: {e}')

    def state(self) -> dict:
        def enc(obj):
            return {'cls': type(obj).__name__, 'xml': to_xml(obj).decode('utf8')}
        return {'version': STATE_VERSION, 'devices': self.devices, 'pins': self.pins, 'counters': self.counters,
                'resources': {href: enc(obj) for href, obj in self.resources.items() if href != '/dcap'},
                'lists': {href: {'cls': cls.__name__, 'items': [enc(i) for i in items]} for href, (cls, items) in self.lists.items()},
                'received': self.received, 'responses': self.responses,
                'reading_types': {k: enc(v) for k, v in self.reading_types.items()}, 'curves': self.curves,
                'immediate_href': self.immediate_href}

    def _load(self):
        try:
            data = json.loads(self.state_path.read_text())
        except Exception as e:
            _log.warning(f'Ignoring unreadable served 2030.5 state {self.state_path}: {e}')
            return

        def dec(entry):
            return from_xml(entry['xml'].encode('utf8'), getattr(sep, entry['cls']))
        try:
            resources = {href: dec(entry) for href, entry in data.get('resources', {}).items()}
            lists = {}
            for href, entry in data.get('lists', {}).items():
                cls = getattr(sep, entry['cls'])
                items = [dec(i) for i in entry['items']]
                # Items are the same objects as the resources they are listed under, where both exist.
                lists[href] = (cls, [resources.get(getattr(i, 'href', None), i) for i in items])
            self.resources.update(resources)
            self.lists.update(lists)
            # The program list and the control list must hold the very objects served by href.
            if PROGRAM in self.resources:
                self.lists['/derp'] = (sep.DERProgramList, [self.resources[PROGRAM]])
            self.devices = dict(data.get('devices', {}))
            self.pins = dict(data.get('pins', {}))
            self.counters.update(data.get('counters', {}))
            self.received = dict(data.get('received', {}))
            self.responses = dict(data.get('responses', {}))
            self.reading_types = {k: dec(v) for k, v in data.get('reading_types', {}).items()}
            self.curves = dict(data.get('curves', {}))
            self.immediate_href = data.get('immediate_href')
            _log.info(f'Restored served 2030.5 state from {self.state_path}: {len(self.devices)} device(s), '
                      f'{len(self.lists.get(DERC, (None, []))[1])} control(s).')
        except Exception as e:
            _log.warning(f'Ignoring corrupt served 2030.5 state {self.state_path}: {e}')


def _luhn(number: int) -> int:
    """Luhn check digit of a decimal number (IEEE 2030.5 PIN and SFDI)."""
    digits = [int(d) for d in str(number)]
    total = 0
    for i, d in enumerate(reversed(digits)):
        d = d * 2 if i % 2 == 0 else d
        total += d - 9 if d > 9 else d
    return (10 - total % 10) % 10


def _list_field(cls) -> str:
    import dataclasses
    for f in dataclasses.fields(cls):
        if f.name not in ('all', 'results') and convert.field_type(cls, f.name)[1]:
            return f.name
    raise ValueError(f'{cls.__name__} has no list field')


_ROOT_TAG = __import__('re').compile(rb'<([A-Za-z_][\w.]*)[\s>/]')


def _parse_any(body: bytes):
    """Parse a document as the sep class named by its root element."""
    match = _ROOT_TAG.search(body.split(b'?>')[-1])
    if match is None:
        raise ValueError('no root element')
    name = match.group(1).decode().split(':')[-1]
    cls = getattr(sep, name, None)
    if cls is None:
        raise ValueError(f'unknown resource {name}')
    return from_xml(body, cls)
