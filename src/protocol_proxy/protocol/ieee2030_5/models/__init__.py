"""IEEE 2030.5 (Smart Energy Profile 2.0) data model and XML codec.

``sep`` holds the generated dataclasses for every type of the ``sep.xsd`` schema (see SOURCE.md), ``enums`` and
``constants`` the enumerations the schema leaves as plain integers, ``xml`` the wire codec and ``convert`` the
boundary between the flat ``Resource.attribute`` convention of the interoperability service and sep objects.
"""
from . import sep
from .xml import NAMESPACE, SEP_XML, from_xml, list_items, to_xml

__all__ = ['NAMESPACE', 'SEP_XML', 'from_xml', 'list_items', 'sep', 'to_xml']
