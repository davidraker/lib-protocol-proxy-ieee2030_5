"""IEEE 2030.5 (Smart Energy Profile 2.0, CSIP) client plugin for protocol_proxy.

Proxies are launched with ``python -m protocol_proxy.proxy <module>:<class>`` (see protocol_proxy.proxy.launch), so
this package may import its proxy class directly. Ieee2030_5Proxy declares no LAUNCHER: everything about a server
arrives via REGISTER_SERVER.
"""
from .client import ServerClient
from .ieee2030_5_proxy import Ieee2030_5Proxy
from .points import PointSpec, ReadingSpec

__all__ = ['Ieee2030_5Proxy', 'PointSpec', 'ReadingSpec', 'ServerClient', 'PROXY_CLASS']

PROXY_CLASS = Ieee2030_5Proxy
