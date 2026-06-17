"""Minimal Dahua DHIP (DVRIP/RPC2) client.

Pure-stdlib client for the Dahua "DHIP" binary RPC2 protocol as implemented by
the `hunter` daemon on Zenointel/Rostelecom (and Dahua-derived) IP cameras.

Wire format (reconstructed from firmware, all little-endian):

    32-byte header, then `messageLength` bytes of JSON, then `dataLength` bytes
    of optional binary payload.

    off  size  field
      0   u32  size/headFlag = 0x00000020  (constant: header length)
      4   u32  magic = 0x50494844 == b"DHIP"
      8   u32  sessionID      (0 before login)
     12   u32  requestID      (mirrors the JSON "id")
     16   u32  packageLength  (= messageLength + dataLength)
     20   u32  packageIndex
     24   u32  messageLength  (length of the JSON text)
     28   u32  dataLength     (length of trailing binary; 0 for pure-JSON)

Login is the standard Dahua two-stage digest challenge/response:

    first global.login (blank password)  -> server returns realm + random
    pwd  = MD5(f"{user}:{realm}:{pass}").hexdigest().upper()
    resp = MD5(f"{user}:{random}:{pwd}").hexdigest().upper()
    second global.login with password=resp

Default port: 37777.
"""

from __future__ import annotations

import hashlib
import json
import socket
import struct

DHIP_MAGIC = 0x50494844            # b"DHIP"
HEADER_SIZE = 32
HEADER_FMT = "<IIIIIIII"           # 8 x u32, little-endian


def _md5_upper(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest().upper()


class DHIPError(Exception):
    pass


class DHIPClient:
    def __init__(self, host: str, port: int = 37777, timeout: float = 10.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.session = 0
        self._id = 0

    # -- connection ---------------------------------------------------------
    def connect(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), self.timeout)

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def __enter__(self) -> "DHIPClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- framing ------------------------------------------------------------
    def _recv_exact(self, n: int) -> bytes:
        assert self.sock is not None
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
            HEADER_FMT,
            HEADER_SIZE,            # size / headFlag
            DHIP_MAGIC,             # magic "DHIP"
            self.session,           # sessionID
            payload.get("id", 0),   # requestID mirrors JSON id
            pkg_len,                # packageLength
            0,                      # packageIndex
            msg_len,                # messageLength
            len(data),              # dataLength
        )
        self.sock.sendall(header + body + data)

    def _recv_frame(self) -> tuple[dict, bytes]:
        hdr = self._recv_exact(HEADER_SIZE)
        (size, magic, session, req_id, pkg_len,
         pkg_idx, msg_len, data_len) = struct.unpack(HEADER_FMT, hdr)
        if magic != DHIP_MAGIC:
            raise DHIPError(f"bad magic 0x{magic:08x} (expected DHIP)")
        body = self._recv_exact(pkg_len) if pkg_len else b""
        msg = body[:msg_len]
        data = body[msg_len:msg_len + data_len]
        try:
            obj = json.loads(msg.decode("utf-8")) if msg else {}
        except json.JSONDecodeError as e:
            raise DHIPError(f"invalid JSON in response: {e}") from e
        return obj, data

    # -- rpc ----------------------------------------------------------------
    def request(self, method: str, params=None, *, data: bytes = b"",
                extra: dict | None = None) -> tuple[dict, bytes]:
        """Send one RPC2 call and return (response_object, binary_data)."""
        self._id += 1
        payload: dict = {"method": method, "id": self._id}
        payload["params"] = {} if params is None else params
        if self.session:
            payload["session"] = self.session
        if extra:
            payload.update(extra)
        self._send_frame(payload, data)
        return self._recv_frame()

    # -- login --------------------------------------------------------------
    def login(self, username: str, password: str,
              client_type: str = "Web3.0") -> dict:
        """Two-stage Dahua DHIP digest login. Returns the final response."""
        # Stage 1: blank-password probe to obtain realm + random (challenge).
        resp, _ = self.request(
            "global.login",
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
        if resp.get("result") and realm is None:
            return resp  # already authenticated (no challenge required)
        if not realm or not random:
            raise DHIPError(f"login challenge missing realm/random: {resp}")

        pwd_hash = _md5_upper(f"{username}:{realm}:{password}")
        response_hash = _md5_upper(f"{username}:{random}:{pwd_hash}")

        # Stage 2: answer the challenge.
        resp2, _ = self.request(
            "global.login",
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
            raise DHIPError(f"login failed: {resp2}")
        self.session = resp2.get("session", self.session) or self.session
        return resp2

    def keep_alive(self, timeout: int = 60) -> tuple[dict, bytes]:
        return self.request("global.keepAlive",
                            {"timeout": timeout, "active": True})

    def logout(self) -> tuple[dict, bytes]:
        return self.request("global.logout")


def _main() -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="Minimal Dahua DHIP RPC2 client")
    ap.add_argument("host")
    ap.add_argument("-p", "--port", type=int, default=37777)
    ap.add_argument("-u", "--user", default="admin")
    ap.add_argument("-P", "--password", default="")
    ap.add_argument("-m", "--method", default="magicBox.getSystemInfo",
                    help="RPC2 method to call after login")
    ap.add_argument("--params", default=None,
                    help="JSON object for method params")
    args = ap.parse_args()

    params = json.loads(args.params) if args.params else None
    with DHIPClient(args.host, args.port) as c:
        login = c.login(args.user, args.password)
        print(f"[+] logged in (session=0x{c.session:08x})", file=sys.stderr)
        resp, data = c.request(args.method, params)
        print(json.dumps(resp, indent=2, ensure_ascii=False))
        if data:
            print(f"[+] +{len(data)} bytes binary payload", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
