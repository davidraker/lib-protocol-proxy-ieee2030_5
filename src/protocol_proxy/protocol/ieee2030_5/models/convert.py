"""The boundary between the interoperability service's flat ``Resource.attribute`` convention and sep objects.

In the convention a quantity such as ``DERSettings.setMaxW`` is one number in engineering units; on the wire it is a
``{value, multiplier}`` pair. Status structs (``DERStatus.operationalModeStatus``) are one enumeration value in the
convention and ``{dateTime, value}`` on the wire. Hex-binary fields are hex strings (or integers) in the convention
and ``bytes`` on the wire. Curve points are a list of ``{xvalue, yvalue}`` dicts in both.
"""
from __future__ import annotations

import dataclasses
import typing
from functools import lru_cache
from typing import Any

from . import sep

#: Convention names that differ from the 2018 schema field names.
ALIASES: dict[tuple[str, ...], tuple[str, ...]] = {
    ('DERStatus', 'connectStatus'): ('DERStatus', 'genConnectStatus'),
}
#: Resources the client publishes upward (writable rows) and the ones it receives (read-only rows).
UPWARD_RESOURCES = ('DERCapability', 'DERSettings', 'DERStatus', 'DERAvailability', 'DeviceInformation',
                    'MirrorMeterReading')
DOWNWARD_RESOURCES = ('DERControl', 'DefaultDERControl', 'DERCurve', 'DERControlList', 'DERProgram')


class ConversionError(ValueError):
    pass


# ---- type introspection ---------------------------------------------------------------------------------------------
@lru_cache(maxsize=None)
def field_types(cls) -> dict[str, Any]:
    # localns must be empty: a field named like its class (DERControl.DERControlBase) would otherwise shadow the type
    return typing.get_type_hints(cls, globalns=vars(sep), localns={})


@lru_cache(maxsize=None)
def field_type(cls, name: str) -> tuple[Any, bool]:
    """``(inner type, is_list)`` of a field, with ``Optional`` and ``List`` stripped."""
    try:
        hint = field_types(cls)[name]
    except KeyError:
        raise ConversionError(f'{cls.__name__} has no attribute {name!r}') from None
    is_list = False
    origin = typing.get_origin(hint)
    if origin is typing.Union:
        args = [a for a in typing.get_args(hint) if a is not type(None)]
        hint = args[0] if len(args) == 1 else hint
        origin = typing.get_origin(hint)
    if origin is list:
        is_list = True
        hint = typing.get_args(hint)[0]
    return hint, is_list


def field_meta(cls, name: str) -> dict:
    return dict(cls.__dataclass_fields__[name].metadata)


def is_dataclass_type(t) -> bool:
    return isinstance(t, type) and dataclasses.is_dataclass(t)


def field_names(cls) -> frozenset:
    return frozenset(f.name for f in dataclasses.fields(cls))


def is_quantity(t) -> bool:
    return is_dataclass_type(t) and field_names(t) == frozenset({'multiplier', 'value'})


def is_status_struct(t) -> bool:
    return is_dataclass_type(t) and field_names(t) == frozenset({'dateTime', 'value'})


def is_link(t) -> bool:
    return is_dataclass_type(t) and 'href' in field_names(t) and field_names(t) <= {'href', 'all'}


def is_hex(cls, name: str) -> bool:
    return field_meta(cls, name).get('format') == 'base16'


# ---- scalar conversions -------------------------------------------------------------------------------------------
def hex_bytes(value, length: int | None) -> bytes:
    """Hex-binary from an int, a hex string or bytes, left-padded to the schema length."""
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
    elif isinstance(value, bool):
        raw = bytes([int(value)])
    elif isinstance(value, int):
        raw = value.to_bytes(max(1, (value.bit_length() + 7) // 8), 'big')
    elif isinstance(value, str):
        text = value.strip().lower()
        text = text[2:] if text.startswith('0x') else text
        if not text:
            raw = b''
        elif all(c in '0123456789abcdef' for c in text):
            raw = bytes.fromhex(text if len(text) % 2 == 0 else '0' + text)
        elif text.isdigit():
            return hex_bytes(int(text), length)
        else:
            raise ConversionError(f'{value!r} is not a hex value')
    else:
        raise ConversionError(f'{value!r} is not a hex value')
    if length is not None:
        if len(raw) > length:
            raw = raw[-length:]
        raw = raw.rjust(length, b'\x00')
    return raw


def hex_text(value: bytes | None) -> str | None:
    return None if value is None else bytes(value).hex().upper()


def quantity_from(cls, value, multiplier: int = 0):
    """A ``{value, multiplier}`` object for an engineering value, scaled by ``10**-multiplier``."""
    if isinstance(value, dict):
        return cls(value=int(value.get('value', 0)), multiplier=int(value.get('multiplier', multiplier)))
    if value is None:
        return None
    return cls(value=int(round(float(value) / (10 ** multiplier))), multiplier=int(multiplier))


def quantity_value(obj) -> float | int | None:
    """Engineering value of a ``{value, multiplier}`` object; integers stay integers when the multiplier is >= 0."""
    if obj is None or obj.value is None:
        return None
    multiplier = obj.multiplier or 0
    return obj.value * 10 ** multiplier if multiplier >= 0 else obj.value / 10 ** -multiplier


def scalar_to_wire(cls, name: str, value, *, multiplier: int = 0, now: int | None = None):
    """Convert one convention value for field ``name`` of ``cls`` to the type the dataclass expects."""
    t, is_list = field_type(cls, name)
    if value is None:
        return None
    if is_list:
        if not isinstance(value, (list, tuple)):
            raise ConversionError(f'{cls.__name__}.{name} takes a list')
        return [to_object(t, item) if is_dataclass_type(t) else item for item in value]
    if is_quantity(t):
        return quantity_from(t, value, multiplier)
    if is_status_struct(t):
        # The struct's value is an enumeration (int) for most statuses and a bitmap (hex) for connect statuses.
        if isinstance(value, dict):
            return t(dateTime=int(value.get('dateTime', now or 0)), value=scalar_to_wire(t, 'value', value['value']))
        return t(dateTime=int(now or 0), value=scalar_to_wire(t, 'value', value))
    if is_dataclass_type(t):
        if isinstance(value, dict):
            return to_object(t, value, now=now)
        if is_link(t) and isinstance(value, str):
            return t(href=value)
        raise ConversionError(f'{cls.__name__}.{name} takes a mapping of its attributes')
    if t is bytes:
        return hex_bytes(value, field_meta(cls, name).get('max_length'))
    if t is bool:
        return value if isinstance(value, bool) else str(value).strip().lower() in ('1', 'true', 'yes', 'on')
    if t is int:
        return int(round(float(value))) if not isinstance(value, int) else value
    if t is float:
        return float(value)
    return value


def to_object(cls, values: dict, *, now: int | None = None):
    """Build a sep object from a mapping of convention values (nested mappings for nested types)."""
    obj = cls()
    for key, value in values.items():
        set_path(obj, key.split('.'), value, now=now)
    return obj


# ---- paths --------------------------------------------------------------------------------------------------------
def resolve_alias(parts: tuple[str, ...]) -> tuple[str, ...]:
    for alias, real in ALIASES.items():
        if parts[:len(alias)] == alias:
            return real + parts[len(alias):]
    return parts


def set_path(obj, parts, value, *, multiplier: int = 0, now: int | None = None):
    """Set a dotted attribute path on a sep object, creating intermediate objects."""
    parts = list(parts)
    cls = type(obj)
    for i, name in enumerate(parts[:-1]):
        t, is_list = field_type(cls, name)
        if is_list or not is_dataclass_type(t):
            raise ConversionError(f'{cls.__name__}.{name} is not a nested resource')
        child = getattr(obj, name)
        if child is None:
            child = t()
            setattr(obj, name, child)
        obj, cls = child, t
    name = parts[-1]
    setattr(obj, name, scalar_to_wire(cls, name, value, multiplier=multiplier, now=now))


def get_path(obj, parts):
    for name in parts:
        if obj is None:
            return None
        obj = getattr(obj, name)
    return obj


def validate_path(resource: str, attribute_parts: tuple[str, ...]) -> type:
    """Check a convention path against the schema; returns the leaf field's declared type."""
    cls = getattr(sep, resource, None)
    if cls is None or not is_dataclass_type(cls):
        raise ConversionError(f'{resource} is not a 2030.5 resource')
    t: Any = cls
    for name in attribute_parts:
        if not is_dataclass_type(t):
            raise ConversionError(f'{resource}.{".".join(attribute_parts)}: {name} is below a scalar')
        t, _ = field_type(t, name)
    return t


# ---- flattening ---------------------------------------------------------------------------------------------------
def flatten(obj, prefix: str = '', *, skip=('href', 'subscribable', 'replyTo', 'responseRequired')) -> dict[str, Any]:
    """Convention view of a sep object: dotted paths to engineering values, hex strings and curve-point lists."""
    out: dict[str, Any] = {}
    if obj is None:
        return out
    cls = type(obj)
    for f in dataclasses.fields(obj):
        name = f.name
        if name in skip:
            continue
        value = getattr(obj, name)
        if value is None or (isinstance(value, list) and not value):
            continue
        path = f'{prefix}.{name}' if prefix else name
        t, is_list = field_type(cls, name)
        if is_list:
            out[path] = [flatten(item) if dataclasses.is_dataclass(item) else item for item in value]
        elif is_quantity(t):
            out[path] = quantity_value(value)
        elif is_status_struct(t):
            out[path] = hex_text(value.value) if isinstance(value.value, (bytes, bytearray)) else value.value
        elif is_link(t):
            out[path] = value.href
        elif is_dataclass_type(t):
            out.update(flatten(value, path))
        elif isinstance(value, (bytes, bytearray)):
            out[path] = hex_text(value)
        else:
            out[path] = value
    return out


def required_fields(obj) -> list[str]:
    return [f.name for f in dataclasses.fields(obj) if f.metadata.get('required') and getattr(obj, f.name) is None]


def fill_required(obj, *, now: int) -> list[str]:
    """Give every required-but-missing field a schema zero value; returns the names filled."""
    filled = []
    cls = type(obj)
    for name in required_fields(obj):
        t, is_list = field_type(cls, name)
        if is_list:
            value: Any = []
        elif is_quantity(t):
            value = t(value=0, multiplier=0)
        elif is_status_struct(t):
            value = t(dateTime=now, value=0)
        elif is_dataclass_type(t):
            value = t()
            fill_required(value, now=now)
        elif t is bytes:
            value = bytes(field_meta(cls, name).get('max_length') or 1)
        elif t is bool:
            value = False
        elif t is str:
            value = ''
        else:
            value = now if name.endswith('Time') else 0
        setattr(obj, name, value)
        filled.append(name)
    return filled
