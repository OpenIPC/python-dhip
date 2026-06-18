"""Exception hierarchy for the Dahua DHIP client."""

from __future__ import annotations


class DHIPError(Exception):
    """Base error for all transport/protocol failures.

    Kept as the public base name for backward compatibility with the original
    single-file ``dhip`` module.
    """


class DahuaError(DHIPError):
    """An RPC call returned ``result: false``.

    Carries the device ``error.code`` and ``error.message`` when present so
    callers can branch on the numeric code (see :data:`dahua.const.DAHUA_ERRORS`).
    """

    def __init__(self, message: str, code: int | None = None, method: str | None = None):
        self.code = code
        self.method = method
        prefix = f"{method}: " if method else ""
        suffix = f" (code {code})" if code is not None else ""
        super().__init__(f"{prefix}{message}{suffix}")


class LoginError(DahuaError):
    """Authentication failed (bad credentials, locked account, etc.)."""
