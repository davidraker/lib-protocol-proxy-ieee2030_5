"""The registered points of one server: a topic, its convention path, direction, and reading metadata."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from .models import convert

#: ReadingType defaults by unit name of the ``MirrorMeterReading.<unit>`` convention (IEEE 2030.5 UomType).
UOM_BY_UNIT = {'W': 38, 'var': 63, 'VAr': 63, 'VA': 61, 'Hz': 33, 'A': 5, 'V': 29, 'PF': 65, 'Wh': 72, 'varh': 73,
               'VAh': 71, 'degC': 23}
#: PhaseCode by the phase names of the convention.
PHASE_CODES = {'PhaseA': 128, 'PhaseB': 64, 'PhaseC': 32, 'PhaseAB': 132, 'PhaseBC': 66, 'PhaseCA': 40, 'PhaseN': 16,
               'PhaseAN': 129, 'PhaseBN': 65, 'PhaseCN': 33, 'PhaseABC': 224}
READING_KEYS = ('uom', 'phase', 'kind', 'flowDirection', 'dataQualifier', 'accumulationBehaviour', 'commodity',
                'powerOfTenMultiplier', 'intervalLength')


def parse_path(path: str) -> tuple[str, ...]:
    """``DERSettings::setMaxW``, ``DERSettings.setMaxW`` or ``DERSettings/setMaxW`` to a tuple of names."""
    text = str(path).strip().replace('::', '.').replace('/', '.')
    parts = tuple(p for p in text.split('.') if p)
    if not parts:
        raise ValueError(f'Empty 2030.5 path {path!r}')
    return convert.resolve_alias(parts)


def reading_mrid(lfdi: str, path: tuple[str, ...]) -> str:
    """A stable 128-bit mRID for a reading row, derived from the device LFDI and the path."""
    return hashlib.sha256(f'{lfdi}:{".".join(path)}'.encode()).hexdigest()[:32].upper()


@dataclass(frozen=True)
class ReadingSpec:
    """The ReadingType of a ``MirrorMeterReading`` row and the mRID of its mirror."""
    mrid: str
    uom: int = 0
    phase: int = 0
    kind: int = 37                      # power
    flowDirection: int = 1              # forward
    dataQualifier: int = 0
    accumulationBehaviour: int = 12     # instantaneous
    commodity: int = 1                  # electricity secondary metered
    powerOfTenMultiplier: int = 0
    intervalLength: int | None = None
    description: str = ''

    @classmethod
    def from_path(cls, path: tuple[str, ...], lfdi: str, given: dict | None = None) -> 'ReadingSpec':
        given = dict(given or {})
        unit = path[1] if len(path) > 1 else ''
        phase_name = path[2] if len(path) > 2 else ''
        values: dict[str, Any] = {'uom': UOM_BY_UNIT.get(unit, 0), 'phase': PHASE_CODES.get(phase_name, 0)}
        if unit in ('Wh', 'varh', 'VAh'):
            values.update(kind=12, accumulationBehaviour=9)      # energy, cumulative
        for key in READING_KEYS:
            if given.get(key) not in (None, ''):
                values[key] = int(given[key])
        mrid = given.get('mrid') or given.get('mRID') or reading_mrid(lfdi, path)
        return cls(mrid=str(mrid).replace('-', '').upper(), description=str(given.get('description') or '.'.join(path)),
                   **values)

    def reading_type(self):
        from .models import sep
        return sep.ReadingType(accumulationBehaviour=self.accumulationBehaviour, commodity=self.commodity,
                               dataQualifier=self.dataQualifier, flowDirection=self.flowDirection,
                               intervalLength=self.intervalLength, kind=self.kind, phase=self.phase,
                               powerOfTenMultiplier=self.powerOfTenMultiplier, uom=self.uom)


@dataclass(frozen=True)
class PointSpec:
    topic: str
    path: tuple[str, ...]
    writable: bool
    multiplier: int = 0
    scaling: float = 1.0
    starting_value: Any = None
    reading: ReadingSpec | None = None
    extra: dict = field(default_factory=dict, compare=False)

    @property
    def resource(self) -> str:
        return self.path[0]

    @property
    def attribute(self) -> tuple[str, ...]:
        return self.path[1:]

    @property
    def dotted(self) -> str:
        return '.'.join(self.path)

    @classmethod
    def from_dict(cls, spec: dict, lfdi: str, served: bool = False) -> 'PointSpec':
        """``served``: the point belongs to a server this proxy serves, so the directions are the client's mirrored:
        the platform writes the controls (downward resources) and the DER client writes the upward ones."""
        path = parse_path(spec['path'])
        resource = path[0]
        if served:
            writable = bool(spec.get('writable', resource in convert.DOWNWARD_RESOURCES))
            if writable and resource not in convert.DOWNWARD_RESOURCES:
                raise ValueError(f'{spec.get("topic")}: {resource} is reported by the DER client and cannot be written here')
            if resource not in convert.UPWARD_RESOURCES and resource not in convert.DOWNWARD_RESOURCES:
                raise ValueError(f'{spec.get("topic")}: {resource} is not a 2030.5 resource this proxy serves')
        else:
            writable = bool(spec.get('writable', resource in convert.UPWARD_RESOURCES))
            if writable and resource not in convert.UPWARD_RESOURCES:
                raise ValueError(f'{spec.get("topic")}: {resource} is received from the server and cannot be written')
            if not writable and resource not in convert.DOWNWARD_RESOURCES:
                raise ValueError(f'{spec.get("topic")}: {resource} is sent to the server; mark the row writable')
        reading = None
        if resource == 'MirrorMeterReading':
            if len(path) < 2:
                raise ValueError(f'{spec.get("topic")}: a MirrorMeterReading row names its unit (MirrorMeterReading.W)')
            reading = ReadingSpec.from_path(path, lfdi, spec.get('reading'))
        else:
            validate_path(path)
        multiplier = spec.get('multiplier')
        scaling = spec.get('scaling')
        return cls(topic=str(spec['topic']), path=path, writable=writable,
                   multiplier=0 if multiplier in (None, '') else int(multiplier),
                   scaling=1.0 if scaling in (None, '') else float(scaling), starting_value=spec.get('starting_value'),
                   reading=reading, extra={k: v for k, v in spec.items() if k not in ('topic', 'path')})

    def engineering(self, value):
        """Scale a convention value for the driver (readings of controls)."""
        if isinstance(value, (int, float)) and not isinstance(value, bool) and self.scaling not in (0, 1.0):
            return value * self.scaling
        return value

    def raw(self, value):
        """Undo the driver's scaling before a value goes on the wire."""
        if isinstance(value, (int, float)) and not isinstance(value, bool) and self.scaling not in (0, 1.0):
            return value / self.scaling
        return value


def validate_path(path: tuple[str, ...]) -> None:
    """Check a convention path against the schema (``DERControlList`` and bare resources always pass)."""
    from .models import sep
    resource, attrs = path[0], path[1:]
    if resource == 'DERControlList' or not attrs:
        return
    if resource in ('DERControl', 'DefaultDERControl'):
        # The convention flattens DERControlBase into the control: DERControl.opModMaxLimW.
        if attrs[0] in convert.field_names(sep.DERControlBase):
            convert.validate_path('DERControlBase', attrs)
        else:
            convert.validate_path(resource, attrs)
    elif resource == 'DERCurve':
        if attrs[0] not in convert.field_names(sep.DERControlBase):
            raise ValueError(f'DERCurve.{attrs[0]}: curves are keyed by the DERControlBase curve attribute')
        if attrs[1:]:
            convert.validate_path('DERCurve', attrs[1:])
    else:
        convert.validate_path(resource, attrs)
