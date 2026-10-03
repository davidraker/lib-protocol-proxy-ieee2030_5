"""Upward data: DERStatus, DERSettings, DERCapability, DERAvailability and DeviceInformation PUTs, and
MirrorMeterReading POSTs.

The writer keeps a shadow copy of every upward resource, seeded from the server's current copy when it has one and
from the registry rows' starting values, so each PUT carries a complete resource even when the driver writes one
attribute at a time. Required fields the driver never wrote are filled with schema zero values and reported once.
"""
from __future__ import annotations

import logging
from typing import Any

from .models import convert, sep
from .points import PointSpec
from .session import DeviceSession
from .transport import Sep2HttpError

_log = logging.getLogger(__name__)

RESOURCE_CLASSES = {'DERCapability': sep.DERCapability, 'DERSettings': sep.DERSettings, 'DERStatus': sep.DERStatus,
                    'DERAvailability': sep.DERAvailability, 'DeviceInformation': sep.DeviceInformation}
#: Fields stamped with the current server time on every PUT.
TIMESTAMPS = {'DERStatus': 'readingTime', 'DERSettings': 'updatedTime', 'DERAvailability': 'readingTime'}


class UpwardWriter:
    def __init__(self, session: DeviceSession):
        self.session = session
        self.http = session.http
        self.shadows: dict[str, Any] = {}
        self.seeded: set[str] = set()
        self.warned_filled: set[str] = set()
        self.last_readings: dict[str, Any] = {}
        self.reading_hrefs: dict[str, str] = {}

    # ---- shadows --------------------------------------------------------------------------------------------------
    async def seed(self, specs: list[PointSpec]):
        """Start the shadows from the server's copies, then apply starting values the rows carry."""
        hrefs = self.session.upward_hrefs()
        for name, cls in RESOURCE_CLASSES.items():
            current = None
            if name in hrefs:
                try:
                    current = await self.http.get_optional(hrefs[name], cls)
                except Sep2HttpError as e:
                    _log.debug(f'No current {name} to seed from: {e}')
            self.shadows[name] = current if current is not None else cls()
            self.seeded.add(name)
        for spec in specs:
            if spec.writable and spec.starting_value not in (None, '') and spec.resource in RESOURCE_CLASSES:
                try:
                    self._apply(spec, spec.starting_value)
                except (convert.ConversionError, ValueError, TypeError) as e:
                    _log.warning(f'Starting value for {spec.topic} ignored: {e}')

    def shadow(self, name: str):
        if name not in self.shadows:
            self.shadows[name] = RESOURCE_CLASSES[name]()
        return self.shadows[name]

    def _apply(self, spec: PointSpec, value):
        convert.set_path(self.shadow(spec.resource), spec.attribute, spec.raw(value), multiplier=spec.multiplier,
                         now=self.session.now())

    def current_value(self, spec: PointSpec):
        """What the driver would read back for an upward row: the shadow's value, or the last reading posted."""
        if spec.resource == 'MirrorMeterReading':
            return self.last_readings.get(spec.topic)
        obj = self.shadows.get(spec.resource)
        if obj is None:
            return None
        value = convert.get_path(obj, spec.attribute)
        if value is None:
            return None
        if convert.is_quantity(type(value)):
            return spec.engineering(convert.quantity_value(value))
        if convert.is_status_struct(type(value)):
            return convert.hex_text(value.value) if isinstance(value.value, (bytes, bytearray)) else value.value
        if isinstance(value, (bytes, bytearray)):
            return convert.hex_text(value)
        if hasattr(value, '__dataclass_fields__'):
            return convert.flatten(value)
        return spec.engineering(value)

    # ---- writes ---------------------------------------------------------------------------------------------------
    async def write(self, values: dict[str, Any], specs: dict[str, PointSpec]) -> tuple[dict, dict]:
        """Apply ``{topic: value}`` and send one PUT per touched resource plus one POST per reading.

        Returns ``(results, errors)`` keyed by topic.
        """
        results: dict[str, Any] = {}
        errors: dict[str, str] = {}
        touched: dict[str, list[str]] = {}
        readings: list[tuple[PointSpec, Any]] = []
        for topic, value in values.items():
            spec = specs.get(topic)
            if spec is None:
                errors[topic] = 'unregistered topic'
                continue
            if not spec.writable:
                errors[topic] = f'{spec.dotted} is received from the server and cannot be written'
                continue
            if spec.resource == 'MirrorMeterReading':
                readings.append((spec, value))
                continue
            try:
                self._apply(spec, value)
            except (convert.ConversionError, ValueError, TypeError) as e:
                errors[topic] = f'invalid value: {e}'
                continue
            touched.setdefault(spec.resource, []).append(topic)
        hrefs = self.session.upward_hrefs()
        for resource, topics in touched.items():
            href = hrefs.get(resource)
            if href is None:
                for topic in topics:
                    errors[topic] = f'the server publishes no {resource} link for this DER'
                continue
            try:
                status = await self._put(resource, href)
            except Sep2HttpError as e:
                for topic in topics:
                    errors[topic] = str(e)
                continue
            for topic in topics:
                results[topic] = {'status': status, 'resource': resource, 'value': values[topic]}
        for spec, value in readings:
            try:
                results[spec.topic] = await self._post_reading(spec, value)
            except (Sep2HttpError, convert.ConversionError, ValueError, TypeError) as e:
                errors[spec.topic] = str(e)
        return results, errors

    async def _put(self, resource: str, href: str) -> int:
        obj = self.shadow(resource)
        now = self.session.now()
        stamp = TIMESTAMPS.get(resource)
        if stamp:
            setattr(obj, stamp, now)
        filled = convert.fill_required(obj, now=now)
        new = set(filled) - self.warned_filled
        if new:
            self.warned_filled |= new
            _log.warning(f'{resource}: required attributes {sorted(new)} were never written; sending zero values. '
                         'Give them registry rows with starting values.')
        if resource == 'DeviceInformation' and obj.lFDI is None:
            from .identity import lfdi_bytes
            obj.lFDI = lfdi_bytes(self.session.lfdi)
        obj.href = href
        return await self.http.put(href, obj)

    async def _post_reading(self, spec: PointSpec, value) -> dict:
        if self.session.mup_href is None:
            raise Sep2HttpError('POST', 'MirrorMeterReading', None, 'no MirrorUsagePoint was created for this device')
        reading_spec = spec.reading
        raw = spec.raw(value)
        wire_value = int(round(float(raw) / (10 ** reading_spec.powerOfTenMultiplier)))
        now = self.session.now()
        reading = sep.MirrorMeterReading(mRID=convert.hex_bytes(reading_spec.mrid, 16), description=reading_spec.description[:32],
                                         lastUpdateTime=now, ReadingType=reading_spec.reading_type(),
                                         Reading=sep.Reading(value=wire_value, qualityFlags=convert.hex_bytes(0, 2),
                                                             timePeriod=sep.DateTimeInterval(start=now, duration=0)))
        location = await self.http.post(self.session.mup_href, reading)
        if location:
            self.reading_hrefs[spec.topic] = location
        self.last_readings[spec.topic] = value
        return {'status': 'posted', 'resource': 'MirrorMeterReading', 'value': value, 'href': location}
