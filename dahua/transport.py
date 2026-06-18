"""DHIP binary transport: framing, two-stage login, request/response.

The framing here is lifted verbatim from the original single-file ``dhip``
module (verified against a live device) and extended with:

* a socket lock, so a background keep-alive timer cannot interleave bytes with
  a caller's request,
* :meth:`DHIPTransport.call`, a raising/unwrapping primitive that the
  high-level helpers build on, while :meth:`request` keeps its original
  no-raise ``(dict, bytes)`` contract,
* :meth:`recv_frame` exposed for the event/stream readers.
"""

from __future__ import annotations

import hashlib
import json
import socket
import struct
import threading
from typing import Any

from . import const
from .exceptions import DahuaError, DHIPError, LoginError


def md5_upper(text: str) -> str:
    """MD5 hex digest, upper-cased — the Dahua digest primitive."""
    return hashlib.md5(text.encode("utf-8")).hexdigest().upper()


# Backwards-compatible private alias (the old module exported ``_md5_upper``).
_md5_upper = md5_upper


def login_digest(username: str, password: str, realm: str, random: str) -> str:
    """Compute the stage-2 password digest for a DHIP login challenge."""
    pwd_hash = md5_upper(f"{username}:{realm}:{password}")
    return md5_upper(f"{username}:{random}:{pwd_hash}")


def extract(resp: dict) -> Any:
    """Pull the meaningful payload out of a (successful) RPC envelope.

    The device is not consistent about where it puts data:

    * most getters return it under ``params`` (a dict or a list),
    * ``global.getCurrentTime`` returns the value directly in ``result``,
    * a bare ``result: true`` carries no payload.

    Prefer ``params`` when it holds something; otherwise fall back to a
    non-boolean ``result``; otherwise return the whole envelope.
    """
    params = resp.get("params")
    if isinstance(params, (dict, list)) and params:
        return params
    result = resp.get("result")
    if not isinstance(result, bool) and result is not None:
        return result
    if params is not None:
        return params
    return resp


class DHIPTransport:
    """A single DHIP connection: TCP socket + RPC2 framing."""

    def __init__(self, host: str, port: int = const.DEFAULT_PORT,
                 timeout: float = const.DEFAULT_TIMEOUT):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.session = 0
        self.encryption: str | None = None
        self._id = 0
        self._lock = threading.RLock()

    # -- connection ---------------------------------------------------------
    def connect(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), self.timeout)

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def __enter__(self):
        if self.sock is None:
            self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- framing ------------------------------------------------------------
    def _recv_exact(self, n: int) -> bytes:
        assert self.sock is not None, "not connected"
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise DHIPError("socket closed by peer")
            buf.extend(chunk)
        return bytes(buf)

    def _send_frame(self, payload: dict, data: bytes = b"") -> None:
        assert self.sock is not None, "not connected"
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        msg_len = len(body)
        pkg_len = msg_len + len(data)
        header = struct.pack(
            const.HEADER_FMT,
            const.HEADER_SIZE,      # size / headFlag
            const.DHIP_MAGIC,       # magic "DHIP"
            self.session,           # sessionID
            payload.get("id", 0),   # requestID mirrors JSON id
            pkg_len,                # packageLength
            0,                      # packageIndex
            msg_len,                # messageLength
            len(data),              # dataLength
        )
        self.sock.sendall(header + body + data)

    def recv_frame(self) -> tuple[dict, bytes, dict]:
        """Read one DHIP frame.

        Returns ``(json_obj, binary_data, header)`` where *header* exposes the
        raw fields (``session``, ``request_id``, ``package_index``) needed by
        the multi-fragment and event readers.
        """
        hdr = self._recv_exact(const.HEADER_SIZE)
        (size, magic, session, req_id, pkg_len,
         pkg_idx, msg_len, data_len) = struct.unpack(const.HEADER_FMT, hdr)
        if magic != const.DHIP_MAGIC:
            raise DHIPError(f"bad magic 0x{magic:08x} (expected DHIP)")
        body = self._recv_exact(pkg_len) if pkg_len else b""
        msg = body[:msg_len]
        data = body[msg_len:msg_len + data_len]
        try:
            obj = json.loads(msg.decode("utf-8")) if msg else {}
        except json.JSONDecodeError as e:
            raise DHIPError(f"invalid JSON in response: {e}") from e
        meta = {"session": session, "request_id": req_id,
                "package_index": pkg_idx, "data_length": data_len}
        return obj, data, meta

    # Compat: the original module's private two-tuple frame reader.
    def _recv_frame(self) -> tuple[dict, bytes]:
        obj, data, _ = self.recv_frame()
        return obj, data

    # -- rpc ----------------------------------------------------------------
    def request(self, method: str, params=None, *, data: bytes = b"",
                extra: dict | None = None) -> tuple[dict, bytes]:
        """Send one RPC2 call and return ``(response_object, binary_data)``.

        Never raises on ``result: false`` — preserves the original contract.
        """
        with self._lock:
            self._id += 1
            payload: dict = {"method": method, "id": self._id}
            payload["params"] = {} if params is None else params
            if self.session:
                payload["session"] = self.session
            if extra:
                payload.update(extra)
            self._send_frame(payload, data)
            return self._recv_frame()

    def call(self, method: str, params=None, *, data: bytes = b"") -> Any:
        """Send an RPC2 call, raise :class:`DahuaError` on failure, unwrap payload.

        This is the primitive every high-level helper is built on.
        """
        resp, _ = self.request(method, params, data=data)
        self._check(resp, method)
        return extract(resp)

    def call_raw(self, method: str, params=None, *, data: bytes = b"") -> tuple[dict, bytes]:
        """Like :meth:`call` but return the full ``(envelope, binary)`` after the check."""
        resp, blob = self.request(method, params, data=data)
        self._check(resp, method)
        return resp, blob

    @staticmethod
    def _check(resp: dict, method: str) -> None:
        if resp.get("result"):
            return
        err = resp.get("error") or {}
        code = err.get("code")
        message = err.get("message") or const.error_message(code, "RPC call failed")
        raise DahuaError(message, code=code, method=method)

    # -- login --------------------------------------------------------------
    def login(self, username: str, password: str,
              client_type: str = "Web3.0") -> dict:
        """Two-stage Dahua DHIP digest login. Returns the final response dict."""
        # Stage 1: blank-password probe to obtain realm + random (challenge).
        resp, _ = self.request(
            const.LOGIN,
            {
                "userName": username,
                "password": "",
                "clientType": client_type,
                "loginType": "Direct",
            },
        )
        # The challenge carries the session id to reuse for stage 2.
        self.session = resp.get("session", self.session) or 0
        params = resp.get("params", {}) or {}
        realm = params.get("realm")
        random = params.get("random")
        self.encryption = params.get("encryption")
        if self.encryption and self.encryption not in ("Default", "OldDigest", ""):
            raise DHIPError(
                f"unsupported login encryption {self.encryption!r}; "
                "only plaintext/Default RPC is implemented")
        if resp.get("result") and realm is None:
            return resp  # already authenticated (no challenge required)
        if not realm or not random:
            raise LoginError(
                f"login challenge missing realm/random: {resp}",
                code=(resp.get("error") or {}).get("code"))

        response_hash = login_digest(username, password, realm, random)

        # Stage 2: answer the challenge.
        resp2, _ = self.request(
            const.LOGIN,
            {
                "userName": username,
                "password": response_hash,
                "clientType": client_type,
                "loginType": "Direct",
                "authorityType": "Default",
                "passwordType": "Default",
                "realm": realm,
                "random": random,
            },
        )
        if not resp2.get("result"):
            err = resp2.get("error") or {}
            raise LoginError(err.get("message") or "login failed",
                             code=err.get("code"), method=const.LOGIN)
        self.session = resp2.get("session", self.session) or self.session
        return resp2

    def keep_alive(self, timeout: int = const.DEFAULT_KEEPALIVE) -> tuple[dict, bytes]:
        return self.request(const.KEEPALIVE, {"timeout": timeout, "active": True})

    def logout(self) -> tuple[dict, bytes]:
        return self.request(const.LOGOUT)
