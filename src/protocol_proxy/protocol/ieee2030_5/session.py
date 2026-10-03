"""The CSIP client session: discovery, registration and the hrefs every later request needs.

A port of the GridAPPS-D Go client's start-up phases: DeviceCapability, time synchronisation, EndDevice lookup or
registration with PIN check, FunctionSetAssignments and DERPrograms by primacy, the DER with its capability,
settings, status and availability links, and the MirrorUsagePoint that mirrors meter readings. Every href comes from
a link in a previous response; only the DeviceCapability path is configured.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .identity import lfdi_bytes, normalize_lfdi, sfdi_from_lfdi
from .models import convert, sep
from .points import ReadingSpec
from .transport import Sep2Http, Sep2HttpError

_log = logging.getLogger(__name__)

TIME_OFFSET_WARNING = 5.0


class SessionError(Exception):
    pass


@dataclass
class SessionConfig:
    lfdi: str
    dcap_path: str = '/dcap'
    pin: int | None = None
    register_if_missing: bool = True
    device_category: int = 0x400000           # DeviceCategoryType bit 22: other generation system
    der_index: int = 0
    poll_rate_floor: float = 60.0
    default_poll_rate: float = 1800.0
    # Caps the server's poll rate (None: honour it). Test harnesses use it to see events without waiting for a long poll.
    poll_rate_ceiling: float | None = None
    mup_role_flags: int = 0x09                    # isMirror | isDER
    mup_service_category: int = 0                 # electricity
    device_description: str = 'VOLTTRON'
    # When the EndDevice's DERList is empty, PUT a DER of our own (some servers let the client declare it).
    create_der_if_missing: bool = True


@dataclass
class ProgramInfo:
    program: sep.DERProgram
    fsa_href: str | None = None

    @property
    def href(self) -> str | None:
        return self.program.href

    @property
    def primacy(self) -> int:
        return self.program.primacy if self.program.primacy is not None else 255


@dataclass
class DeviceSession:
    http: Sep2Http
    config: SessionConfig
    readings: list[ReadingSpec] = field(default_factory=list)
    clock: Callable[[], float] = time.time

    dcap: sep.DeviceCapability | None = None
    time_offset: float = 0.0
    end_device: sep.EndDevice | None = None
    registration: sep.Registration | None = None
    fsas: list[sep.FunctionSetAssignments] = field(default_factory=list)
    programs: list[ProgramInfo] = field(default_factory=list)
    der: sep.DER | None = None
    mup_href: str | None = None
    program_poll_rate: int | None = None
    registered_now: bool = False
    ready: bool = False

    # ---- time -----------------------------------------------------------------------------------------------------
    def now(self) -> int:
        """Server time, in seconds."""
        return int(self.clock() + self.time_offset)

    def poll_rate(self, *candidates) -> float:
        """The first server-provided poll rate among ``candidates`` (then the DERProgramList's, then the
        DeviceCapability's), floored; the default when none is given."""
        candidates = (*candidates, self.program_poll_rate, self.dcap.pollRate if self.dcap else None)
        for rate in candidates:
            if rate:
                return self._bounded(float(rate))
        return self._bounded(self.config.default_poll_rate)

    def _bounded(self, rate: float) -> float:
        rate = max(rate, self.config.poll_rate_floor)
        if self.config.poll_rate_ceiling is not None:
            rate = min(rate, max(self.config.poll_rate_ceiling, self.config.poll_rate_floor))
        return rate

    @property
    def lfdi(self) -> str:
        return normalize_lfdi(self.config.lfdi)

    @property
    def sfdi(self) -> int:
        return sfdi_from_lfdi(self.lfdi)

    # ---- phases ---------------------------------------------------------------------------------------------------
    async def start(self):
        """Run every phase in order. Raises SessionError when the server rejects the device."""
        self.ready = False
        await self.discover()
        await self.sync_time()
        await self.find_or_register()
        await self.check_registration()
        await self.load_programs()
        await self.load_der()
        await self.ensure_mirror_usage_point()
        self.ready = True
        _log.info(f'2030.5 session ready for LFDI {self.lfdi}: {len(self.programs)} DER program(s), '
                  f'DER {self.der.href if self.der else "none"}, MUP {self.mup_href or "none"}')

    async def discover(self):
        self.dcap = await self.http.get(self.config.dcap_path, sep.DeviceCapability)

    async def sync_time(self):
        link = self.dcap.TimeLink if self.dcap else None
        if link is None or not link.href:
            _log.warning('The server publishes no Time resource; using local time.')
            self.time_offset = 0.0
            return
        server_time = await self.http.get(link.href, sep.Time)
        if server_time.currentTime is not None:
            self.time_offset = server_time.currentTime - self.clock()
            if abs(self.time_offset) > TIME_OFFSET_WARNING:
                _log.warning(f'Server time differs from local time by {self.time_offset:.1f} s; server time is used.')

    async def find_or_register(self):
        link = self.dcap.EndDeviceListLink if self.dcap else None
        if link is None or not link.href:
            raise SessionError('The server publishes no EndDeviceList; the client cannot register.')
        devices = await self.http.get_all(link.href, sep.EndDeviceList)
        mine = lfdi_bytes(self.lfdi)
        for device in devices:
            if device.lFDI == mine or (device.lFDI is None and device.sFDI == self.sfdi):
                self.end_device = device
                self.registered_now = False
                return
        if not self.config.register_if_missing:
            raise SessionError(f'EndDevice with LFDI {self.lfdi} is not on the server and registration is disabled.')
        new = sep.EndDevice(lFDI=mine, sFDI=self.sfdi, changedTime=self.now(), enabled=True,
                            deviceCategory=convert.hex_bytes(self.config.device_category, 4))
        location = await self.http.post(link.href, new)
        if not location:
            raise SessionError('The server accepted the EndDevice but returned no Location.')
        self.end_device = await self.http.get(location, sep.EndDevice)
        self.registered_now = True
        _log.info(f'Registered EndDevice {location} for LFDI {self.lfdi}')

    async def check_registration(self):
        link = self.end_device.RegistrationLink if self.end_device else None
        self.registration = await self.http.get_optional(link.href if link else None, sep.Registration)
        if self.config.pin is not None:
            if self.registration is None:
                raise SessionError('A PIN is configured but the server publishes no Registration to check it against.')
            if self.registration.pIN != int(self.config.pin):
                raise SessionError(f'Registration PIN mismatch: the server holds {self.registration.pIN}.')

    async def load_programs(self):
        """DERPrograms from every FunctionSetAssignment (and the DeviceCapability's own list), sorted by primacy."""
        programs: dict[str, ProgramInfo] = {}
        self.fsas = []
        link = self.end_device.FunctionSetAssignmentsListLink if self.end_device else None
        if link and link.href:
            self.fsas = await self.http.get_all(link.href, sep.FunctionSetAssignmentsList)
        self.program_poll_rate = None
        for fsa in self.fsas:
            if fsa.DERProgramListLink and fsa.DERProgramListLink.href:
                first = await self.http.get(fsa.DERProgramListLink.href, sep.DERProgramList, params={'s': 0, 'l': 1})
                self.program_poll_rate = self.program_poll_rate or getattr(first, 'pollRate', None)
                for program in await self.http.get_all(fsa.DERProgramListLink.href, sep.DERProgramList):
                    if program.href:
                        programs.setdefault(program.href, ProgramInfo(program, fsa.href))
        if self.dcap and self.dcap.DERProgramListLink and self.dcap.DERProgramListLink.href:
            for program in await self.http.get_all(self.dcap.DERProgramListLink.href, sep.DERProgramList):
                if program.href:
                    programs.setdefault(program.href, ProgramInfo(program))
        self.programs = sorted(programs.values(), key=lambda p: (p.primacy, p.href or ''))

    async def load_der(self):
        link = self.end_device.DERListLink if self.end_device else None
        self.der = None
        if link is None or not link.href:
            _log.warning('The EndDevice has no DERList; DER status, settings and capability cannot be reported.')
            return
        ders = await self.http.get_all(link.href, sep.DERList)
        if not ders and self.config.create_der_if_missing:
            ders = await self._declare_der(link.href)
        if not ders:
            _log.warning('The EndDevice has an empty DERList; DER status, settings and capability cannot be reported.')
            return
        index = min(self.config.der_index, len(ders) - 1)
        self.der = ders[index]

    async def _declare_der(self, list_href: str) -> list:
        """PUT a DER with the four upward links under the DERList, for servers that let the client declare it."""
        base = f'{list_href.rstrip("/")}/{self.config.der_index + 1}'
        der = sep.DER(href=base, DERCapabilityLink=sep.DERCapabilityLink(href=f'{base}/dercap'),
                      DERSettingsLink=sep.DERSettingsLink(href=f'{base}/derg'), DERStatusLink=sep.DERStatusLink(href=f'{base}/ders'),
                      DERAvailabilityLink=sep.DERAvailabilityLink(href=f'{base}/dera'))
        try:
            await self.http.put(base, der)
        except Sep2HttpError as e:
            _log.info(f'The server does not let the client declare its DER ({e}).')
            return []
        _log.info(f'Declared DER {base} on the server.')
        ders = await self.http.get_all(list_href, sep.DERList)
        return ders or [der]

    async def ensure_mirror_usage_point(self):
        """Create (or update) the MirrorUsagePoint that holds one MirrorMeterReading per configured reading."""
        self.mup_href = None
        if not self.readings:
            return
        link = self.dcap.MirrorUsagePointListLink if self.dcap else None
        if link is None or not link.href:
            _log.warning('The server publishes no MirrorUsagePointList; meter readings cannot be mirrored.')
            return
        mup = sep.MirrorUsagePoint(
            mRID=convert.hex_bytes(self._mup_mrid(), 16), description=self.config.device_description[:32],
            roleFlags=convert.hex_bytes(self.config.mup_role_flags, 2),
            serviceCategoryKind=self.config.mup_service_category, status=1, deviceLFDI=lfdi_bytes(self.lfdi),
            MirrorMeterReading=[sep.MirrorMeterReading(mRID=convert.hex_bytes(r.mrid, 16), description=r.description[:32],
                                                       ReadingType=r.reading_type()) for r in self.readings])
        try:
            location = await self.http.post(link.href, mup)
        except Sep2HttpError as e:
            _log.warning(f'MirrorUsagePoint creation failed ({e}); meter readings will not be mirrored.')
            return
        self.mup_href = location or None
        if self.mup_href is None:
            _log.warning('The server accepted the MirrorUsagePoint without a Location; readings cannot be posted.')

    def _mup_mrid(self) -> str:
        import hashlib
        return hashlib.sha256(f'{self.lfdi}:mup'.encode()).hexdigest()[:32]

    # ---- views ----------------------------------------------------------------------------------------------------
    def upward_hrefs(self) -> dict[str, str]:
        """PUT targets by resource name, from the DER and EndDevice links."""
        hrefs: dict[str, str] = {}
        if self.der is not None:
            for name, link in (('DERCapability', self.der.DERCapabilityLink), ('DERSettings', self.der.DERSettingsLink),
                               ('DERStatus', self.der.DERStatusLink), ('DERAvailability', self.der.DERAvailabilityLink)):
                if link and link.href:
                    hrefs[name] = link.href
        if self.end_device is not None and self.end_device.DeviceInformationLink and self.end_device.DeviceInformationLink.href:
            hrefs['DeviceInformation'] = self.end_device.DeviceInformationLink.href
        return hrefs

    def subscription_list_href(self) -> str | None:
        """The EndDevice's SubscriptionList href; the conventional ``<EndDevice>/sub`` when the server publishes the
        list without advertising the link (the subscription attempt then settles whether it exists)."""
        if self.end_device is None:
            return None
        link = self.end_device.SubscriptionListLink
        if link and link.href:
            return link.href
        return f'{self.end_device.href.rstrip("/")}/sub' if self.end_device.href else None

    def describe(self) -> dict:
        return {'lfdi': self.lfdi, 'sfdi': self.sfdi, 'end_device': self.end_device.href if self.end_device else None,
                'registered_now': self.registered_now, 'programs': [p.href for p in self.programs],
                'der': self.der.href if self.der else None, 'mup': self.mup_href, 'time_offset': round(self.time_offset, 3),
                'ready': self.ready}
