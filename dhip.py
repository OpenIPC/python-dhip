"""Backward-compatibility shim for the original single-file ``dhip`` module.

The implementation now lives in the :mod:`dahua` package. This module re-exports
the names the original API exposed so existing imports keep working::

    from dhip import DHIPClient, DHIP_MAGIC, HEADER_FMT, HEADER_SIZE

``DHIPClient`` is an alias of :class:`dahua.client.DahuaClient`; its original
methods (``connect``/``close``/``request``/``login``/``keep_alive``/``logout``,
context-manager support) keep identical signatures and return types.
"""

from __future__ import annotations

from dahua import (  # noqa: F401
    DahuaClient,
    DHIPClient,
    DHIPError,
    DahuaError,
    LoginError,
    DHIPTransport,
    EventListener,
    DHIP_MAGIC,
    HEADER_FMT,
    HEADER_SIZE,
    DEFAULT_PORT,
    md5_upper,
    login_digest,
    _md5_upper,
    __version__,
)
from dahua.const import HEADER_SIZE as _HEADER_SIZE  # noqa: F401

__all__ = [
    "DHIPClient",
    "DahuaClient",
    "DHIPError",
    "DahuaError",
    "LoginError",
    "DHIPTransport",
    "EventListener",
    "DHIP_MAGIC",
    "HEADER_FMT",
    "HEADER_SIZE",
    "DEFAULT_PORT",
    "md5_upper",
    "login_digest",
    "_md5_upper",
]


def _main() -> int:
    from dahua.cli import main
    return main()


if __name__ == "__main__":
    raise SystemExit(_main())
