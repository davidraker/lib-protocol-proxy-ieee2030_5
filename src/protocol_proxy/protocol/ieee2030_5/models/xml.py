"""XML codec for the generated sep dataclasses (xsdata runtime, stdlib parser, no lxml)."""
from __future__ import annotations

from dataclasses import fields
from typing import Any, TypeVar

from xsdata.formats.dataclass.context import XmlContext
from xsdata.formats.dataclass.parsers import XmlParser
from xsdata.formats.dataclass.parsers.config import ParserConfig
from xsdata.formats.dataclass.parsers.handlers import XmlEventHandler
from xsdata.formats.dataclass.serializers import XmlSerializer
from xsdata.formats.dataclass.serializers.config import SerializerConfig

#: XML namespace of the IEEE 2030.5 schema.
NAMESPACE = 'urn:ieee:std:2030.5:ns'
#: Media type of every 2030.5 request and response body.
SEP_XML = 'application/sep+xml'

T = TypeVar('T')

_context = XmlContext()
# Lenient on purpose: servers add vendor elements and attributes, peers may speak an older or newer edition, and
# the models (2023) fold the schema's simple-content wrappers so their anyAttribute slots are not represented.
_parser = XmlParser(config=ParserConfig(fail_on_unknown_properties=False, fail_on_unknown_attributes=False),
                    context=_context, handler=XmlEventHandler)
_serializer = XmlSerializer(config=SerializerConfig(xml_declaration=True, encoding='UTF-8', indent=None), context=_context)
_ns_map = {None: NAMESPACE}


def to_xml(obj: Any) -> bytes:
    """Render a sep dataclass as a UTF-8 document in the 2030.5 default namespace."""
    return _serializer.render(obj, ns_map=_ns_map).encode('utf8')


def from_xml(data: bytes | str, cls: type[T]) -> T:
    """Parse a document as ``cls``. Raises ``xsdata.exceptions.ParserError`` on malformed input."""
    if isinstance(data, str):
        data = data.encode('utf8')
    return _parser.from_bytes(data, cls)


def list_items(list_obj: Any) -> list:
    """The repeated element of a ``*List`` resource (``EndDevice`` of an ``EndDeviceList`` and so on).

    Every list type in the schema carries ``all``, ``results`` and exactly one list-valued field; this returns that
    field's value so paging code need not know each list's element name.
    """
    from .convert import is_structural
    if list_obj is None:
        return []
    for f in fields(list_obj):
        if f.name in ('all', 'results', 'href', 'subscribable', 'pollRate') or not is_structural(f):
            continue
        value = getattr(list_obj, f.name)
        if isinstance(value, list):
            return value
    return []
