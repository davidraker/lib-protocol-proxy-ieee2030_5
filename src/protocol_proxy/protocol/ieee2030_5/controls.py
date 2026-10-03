"""DER controls as the client sees them: polling, the event state machine, randomisation and responses.

A port of the GridAPPS-D Go client's ``dercontrol_poll``, ``scheduler``, ``statemachine`` and ``response_retry``.
Each DERProgram's DefaultDERControl and DERControlList are fetched at the program list's poll rate or when a
notification arrives; every DERControl is an event with ``EventStatus.currentStatus`` 0 scheduled, 1 active, 2 or 3
cancelled, 4 superseded, 5 completed. The engine keeps its own view of each event (scheduled, active, complete,
cancelled, superseded), moves it on the randomised start and end times, resolves overlaps by program primacy, and
posts a DERControlResponse for every transition the server asked to hear about.

The public view is ``snapshot()``: the interoperability service's flat ``2030.5`` convention (``DERControl.<attr>``
for the controls in force, ``DefaultDERControl.<attr>``, ``DERCurve.<curve attribute>.<attr>`` for the curves those
controls link, ``DERControlList`` as the list of scheduled and active events, ``DERProgram.<attr>`` for the program
in charge).
"""
from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .identity import lfdi_bytes
from .models import convert, sep
from .session import DeviceSession, ProgramInfo
from .transport import Sep2HttpError

_log = logging.getLogger(__name__)

#: EventStatus.currentStatus values (IEEE 2030.5 Table 44).
SCHEDULED, ACTIVE, CANCELLED, CANCELLED_WITH_RANDOMIZATION, SUPERSEDED, COMPLETE = 0, 1, 2, 3, 4, 5
#: Response status codes (IEEE 2030.5 Table 27) the client sends.
RESPONSE_RECEIVED, RESPONSE_STARTED, RESPONSE_COMPLETED = 1, 2, 3
RESPONSE_CANCELLED, RESPONSE_SUPERSEDED = 6, 7
RESPONSE_REJECTED_INVALID, RESPONSE_REJECTED_EXPIRED = 253, 254
#: ResponseRequired bits.
RR_MESSAGE_RECEIVED, RR_SPECIFIC_RESPONSE, RR_RESPONSE_REQUIRED = 0x01, 0x02, 0x04

CURVE_ATTRIBUTES = tuple(name for name in convert.field_names(sep.DERControlBase)
                         if convert.field_type(sep.DERControlBase, name)[0] is sep.DERCurveLink)
RESPONSE_ATTEMPTS = 3

ChangeCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


def mrid_hex(control) -> str:
    return convert.hex_text(control.mRID) or ''


def response_required_bits(control) -> int:
    rr = getattr(control, 'responseRequired', None)
    return int.from_bytes(rr, 'big') if rr else 0


@dataclass
class EventState:
    control: sep.DERControl
    program: ProgramInfo
    status: str                   # scheduled | active | complete | cancelled | superseded
    start: int
    end: int
    responded: list[int] = field(default_factory=list)

    @property
    def mrid(self) -> str:
        return mrid_hex(self.control)

    @property
    def base(self) -> sep.DERControlBase:
        return self.control.DERControlBase or sep.DERControlBase()

    @property
    def primacy(self) -> int:
        return self.program.primacy

    @property
    def creation_time(self) -> int:
        return self.control.creationTime or 0

    def fields_set(self) -> set[str]:
        return {name for name in convert.field_names(sep.DERControlBase) if getattr(self.base, name) is not None}

    def overlaps(self, other: 'EventState') -> bool:
        return self.start < other.end and other.start < self.end

    def outranks(self, other: 'EventState') -> bool:
        """Lower primacy wins; among equals the later creation time wins."""
        if self.primacy != other.primacy:
            return self.primacy < other.primacy
        return self.creation_time > other.creation_time


class ControlEngine:
    def __init__(self, session: DeviceSession, *, on_change: ChangeCallback | None = None,
                 rng: random.Random | None = None):
        self.session = session
        self.http = session.http
        self.on_change = on_change
        self.rng = rng or random.Random()
        self.defaults: dict[str, sep.DefaultDERControl] = {}
        self.events: dict[str, EventState] = {}
        self.curves: dict[str, sep.DERCurve] = {}
        self.responses_sent: list[tuple[str, int]] = []
        self.last_refresh: float | None = None
        self.poll_rate: float = session.poll_rate()
        self.last_snapshot: dict[str, Any] = {}
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._refresh_lock = asyncio.Lock()
        self._reload_programs = False

    # ---- fetching -------------------------------------------------------------------------------------------------
    async def refresh(self) -> dict[str, Any]:
        """Fetch every program's defaults and controls, reconcile the events and return the changed snapshot keys."""
        async with self._refresh_lock:
            if self._reload_programs:
                self._reload_programs = False
                await self.session.load_programs()
            seen: set[str] = set()
            for info in self.session.programs:
                program = info.program
                if program.DefaultDERControlLink and program.DefaultDERControlLink.href:
                    default = await self.http.get_optional(program.DefaultDERControlLink.href, sep.DefaultDERControl)
                    if default is not None:
                        self.defaults[info.href] = default
                        await self._fetch_curves(default.DERControlBase)
                if program.DERControlListLink and program.DERControlListLink.href:
                    controls = await self.http.get_all(program.DERControlListLink.href, sep.DERControlList)
                    for control in controls:
                        seen.add(mrid_hex(control))
                        await self._ingest(control, info)
            for mrid, state in list(self.events.items()):
                if mrid not in seen and state.status in ('scheduled', 'active'):
                    _log.info(f'DERControl {mrid} disappeared from the server; treating it as cancelled.')
                    state.status = 'cancelled'
            self.poll_rate = self.session.poll_rate()
            self.last_refresh = self.session.clock()
            return await self.tick()

    async def _fetch_curves(self, base: sep.DERControlBase | None):
        if base is None:
            return
        for name in CURVE_ATTRIBUTES:
            link = getattr(base, name)
            if link is not None and link.href and link.href not in self.curves:
                curve = await self.http.get_optional(link.href, sep.DERCurve)
                if curve is not None:
                    self.curves[link.href] = curve

    def _randomized(self, control: sep.DERControl) -> tuple[int, int]:
        interval = control.interval or sep.DateTimeInterval(start=self.session.now(), duration=0)
        start, duration = int(interval.start or 0), int(interval.duration or 0)
        r_start, r_duration = int(control.randomizeStart or 0), int(control.randomizeDuration or 0)
        if r_start:
            start += self.rng.randint(min(0, r_start), max(0, r_start))
        if r_duration:
            duration = max(0, duration + self.rng.randint(min(0, r_duration), max(0, r_duration)))
        return start, start + duration

    async def _ingest(self, control: sep.DERControl, info: ProgramInfo):
        mrid = mrid_hex(control)
        status = control.EventStatus.currentStatus if control.EventStatus else SCHEDULED
        state = self.events.get(mrid)
        if state is None:
            if status in (CANCELLED, CANCELLED_WITH_RANDOMIZATION, SUPERSEDED, COMPLETE):
                return                                      # over before we ever saw it
            if control.DERControlBase is None or control.interval is None:
                await self._respond(EventState(control, info, 'cancelled', 0, 0), RESPONSE_REJECTED_INVALID)
                return
            start, end = self._randomized(control)
            if end <= self.session.now():
                await self._respond(EventState(control, info, 'complete', start, end), RESPONSE_REJECTED_EXPIRED)
                return
            state = EventState(control, info, 'scheduled', start, end)
            self.events[mrid] = state
            await self._fetch_curves(control.DERControlBase)
            await self._respond(state, RESPONSE_RECEIVED)
            return
        state.control, state.program = control, info
        if state.status in ('scheduled', 'active'):
            if status in (CANCELLED, CANCELLED_WITH_RANDOMIZATION):
                state.status = 'cancelled'
                await self._respond(state, RESPONSE_CANCELLED)
            elif status == SUPERSEDED:
                state.status = 'superseded'
                await self._respond(state, RESPONSE_SUPERSEDED)

    # ---- state machine --------------------------------------------------------------------------------------------
    async def tick(self, now: int | None = None) -> dict[str, Any]:
        """Advance events to ``now`` and publish the snapshot if it changed; returns the changed keys."""
        now = self.session.now() if now is None else now
        for state in sorted(self.events.values(), key=lambda s: s.start):
            if state.status == 'active' and state.end <= now:
                state.status = 'complete'
                await self._respond(state, RESPONSE_COMPLETED)
        for state in sorted(self.events.values(), key=lambda s: s.start):
            if state.status == 'scheduled' and state.start <= now:
                if state.end <= now:
                    state.status = 'complete'
                    await self._respond(state, RESPONSE_COMPLETED)
                    continue
                state.status = 'active'
                await self._respond(state, RESPONSE_STARTED)
        await self._resolve_overlaps()
        return await self._publish()

    async def _resolve_overlaps(self):
        """Among active events that set the same control attribute and overlap in time, only the top-ranked stays."""
        active = [s for s in self.events.values() if s.status == 'active']
        for state in active:
            for other in active:
                if other is state or state.status != 'active' or other.status != 'active':
                    continue
                if state.overlaps(other) and state.fields_set() & other.fields_set() and other.outranks(state):
                    state.status = 'superseded'
                    await self._respond(state, RESPONSE_SUPERSEDED)
                    break

    async def _respond(self, state: EventState, status: int):
        """POST a DERControlResponse when the event asks for one (``responseRequired`` bits), with retries."""
        bits = response_required_bits(state.control)
        wanted = bits & RR_MESSAGE_RECEIVED if status == RESPONSE_RECEIVED else bits & (RR_SPECIFIC_RESPONSE | RR_RESPONSE_REQUIRED)
        if not wanted or not state.control.replyTo:
            return
        if status in state.responded and status != RESPONSE_RECEIVED:
            return
        response = sep.DERControlResponse(createdDateTime=self.session.now(), endDeviceLFDI=lfdi_bytes(self.session.lfdi),
                                          status=status, subject=state.control.mRID)
        for attempt in range(RESPONSE_ATTEMPTS):
            try:
                await self.http.post(state.control.replyTo, response)
                state.responded.append(status)
                self.responses_sent.append((state.mrid, status))
                return
            except Sep2HttpError as e:
                _log.warning(f'DERControlResponse {status} for {state.mrid} failed (attempt {attempt + 1}): {e}')
                if e.status is not None and 400 <= e.status < 500:
                    return
                await asyncio.sleep(0.5 * 2 ** attempt)

    # ---- views ----------------------------------------------------------------------------------------------------
    def active_events(self) -> list[EventState]:
        """Active events, lowest priority first so that overlaying them leaves the top-ranked values in place."""
        active = [s for s in self.events.values() if s.status == 'active']
        active.sort(key=lambda s: (-s.primacy, s.creation_time))
        return active

    def program_in_charge(self) -> ProgramInfo | None:
        active = self.active_events()
        if active:
            return active[-1].program
        return self.session.programs[0] if self.session.programs else None

    def snapshot(self) -> dict[str, Any]:
        """The convention view of the controls now in force (see the module docstring)."""
        view: dict[str, Any] = {}
        program = self.program_in_charge()
        default = self.defaults.get(program.href) if program else None
        if program is not None:
            for key, value in convert.flatten(program.program, skip=('href', 'subscribable')).items():
                if not key.endswith('Link'):
                    view[f'DERProgram.{key}'] = value
        effective = sep.DERControlBase()
        if default is not None:
            for key, value in convert.flatten(default, skip=('href', 'subscribable')).items():
                view[f'DefaultDERControl.{key.removeprefix("DERControlBase.")}'] = value
            self._overlay(effective, default.DERControlBase)
        for state in self.active_events():
            self._overlay(effective, state.base)
        for key, value in convert.flatten(effective).items():
            view[f'DERControl.{key}'] = value
        active = self.active_events()
        if active:
            top = active[-1]
            view['DERControl.mRID'] = top.mrid
            view['DERControl.interval.start'], view['DERControl.interval.duration'] = top.start, top.end - top.start
            view['DERControl.EventStatus.currentStatus'] = ACTIVE
        for name in CURVE_ATTRIBUTES:
            link = getattr(effective, name)
            curve = self.curves.get(link.href) if link is not None and link.href else None
            if curve is not None:
                for key, value in convert.flatten(curve, skip=('href', 'subscribable')).items():
                    view[f'DERCurve.{name}.{key}'] = value
        view['DERControlList'] = [self._entry(s) for s in sorted(self.events.values(), key=lambda s: s.start)
                                  if s.status in ('scheduled', 'active')]
        return view

    def _entry(self, state: EventState) -> dict[str, Any]:
        entry: dict[str, Any] = {'mRID': state.mrid, 'status': state.status,
                                 'interval': {'start': state.start, 'duration': state.end - state.start},
                                 'primacy': state.primacy, 'DERControl': convert.flatten(state.base), 'DERCurve': {}}
        for name in CURVE_ATTRIBUTES:
            link = getattr(state.base, name)
            curve = self.curves.get(link.href) if link is not None and link.href else None
            if curve is not None:
                entry['DERCurve'][name] = convert.flatten(curve, skip=('href', 'subscribable'))
        return entry

    @staticmethod
    def _overlay(target: sep.DERControlBase, source: sep.DERControlBase | None):
        if source is None:
            return
        for name in convert.field_names(sep.DERControlBase):
            value = getattr(source, name)
            if value is not None:
                setattr(target, name, value)

    async def _publish(self) -> dict[str, Any]:
        view = self.snapshot()
        changed = {k: v for k, v in view.items() if k not in self.last_snapshot or self.last_snapshot[k] != v}
        changed.update({k: None for k in self.last_snapshot if k not in view})
        self.last_snapshot = view
        if changed and self.on_change is not None:
            result = self.on_change(changed)
            if asyncio.iscoroutine(result):
                await result
        return changed

    # ---- scheduling -----------------------------------------------------------------------------------------------
    def next_deadline(self) -> int | None:
        times = [s.start for s in self.events.values() if s.status == 'scheduled']
        times += [s.end for s in self.events.values() if s.status == 'active']
        return min(times) if times else None

    def request_refresh(self, reload_programs: bool = False):
        """Wake the run loop to fetch now (a notification arrived, or the program list changed)."""
        self._reload_programs = self._reload_programs or reload_programs
        self.last_refresh = None
        self._wake.set()

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self.run())

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def run(self):
        while True:
            try:
                now = self.session.clock()
                due = self.last_refresh is None or now - self.last_refresh >= self.poll_rate
                if due:
                    await self.refresh()
                else:
                    await self.tick()
            except Sep2HttpError as e:
                _log.warning(f'Fetching DER controls failed: {e}')
                self.last_refresh = self.session.clock()
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception('Unexpected error while handling DER controls')
                self.last_refresh = self.session.clock()
            now = self.session.clock()
            wait = max(0.0, (self.last_refresh or now) + self.poll_rate - now)
            deadline = self.next_deadline()
            if deadline is not None:
                wait = min(wait, max(0.0, deadline - self.session.now()))
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=max(0.05, wait))
            except asyncio.TimeoutError:
                pass
