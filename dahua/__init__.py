"""python-dhip: a pure-stdlib client for Dahua DHIP (RPC2) IP cameras.

The DHIP transport (``CDVRIPRPCClient`` / ``DHIPHeader``) is spoken on TCP 5000
by Dahua-derived cameras. This is *not* the XiongMai/Sofia "NetSurveillance"
protocol (TCP 34567) that python-dvr implements.
"""

from __future__ import annotations

from . import const
from .const import (
    DHIP_MAGIC,
    HEADER_FMT,
    HEADER_SIZE,
    DEFAULT_PORT,
)
from .exceptions import DHIPError, DahuaError, LoginError
from .transport import DHIPTransport, md5_upper, login_digest
from .client import DahuaClient, DHIPClient
from .events import EventListener

__version__ = "0.1.0"

# Private alias kept for the original single-file module's API.
_md5_upper = md5_upper

try:  # async client is optional — only import if asyncio is importable
    from .aio import AsyncDahuaClient  # noqa: F401
except Exception:  # noqa: BLE001
    AsyncDahuaClient = None  # type: ignore

__all__ = [
    "DahuaClient",
    "DHIPClient",
    "AsyncDahuaClient",
    "DHIPTransport",
    "EventListener",
    "DHIPError",
    "DahuaError",
    "LoginError",
    "DHIP_MAGIC",
    "HEADER_FMT",
    "HEADER_SIZE",
    "DEFAULT_PORT",
    "md5_upper",
    "login_digest",
    "const",
    "__version__",
]
