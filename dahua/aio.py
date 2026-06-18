"""Asyncio Dahua DHIP client — mirrors the synchronous :class:`DahuaClient`.

Shares the protocol constants, digest, and payload-extraction helpers with the
synchronous implementation so the two cannot drift on the wire format.
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
from datetime import datetime
from typing import Any, AsyncIterator, Callable

from . import const
from .exceptions import DahuaError, DHIPError, LoginError
from .transport import extract, login_digest

_TIME_FMT = "%Y-%m-%d %H:%M:%S"


class AsyncDahuaClient:
    """Async client for a Dahua IP camera over DHIP (RPC2).

    Example::

        async with AsyncDahuaClient("10.0.0.10") as cam:
            await cam.login("admin", "admin54321")
            print(await cam.get_system_info())
    """

    def __init__(self, host: str, port: int = const.DEFAULT_PORT,
                 timeout: float = const.DEFAULT_TIMEOUT):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.username = ""
        self.password = ""
        self.session = 0
        self.encryption: str | None = None
        self.device_info: dict = {}
        self.logger = logging.getLogger(__name__)
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._id = 0
        self._lock = asyncio.Lock()
        self._keepalive_interval = const.DEFAULT_KEEPALIVE
        self._keepalive_task: asyncio.Task | None = None

    # -- connection ---------------------------------------------------------
    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), self.timeout)

    async def close(self) -> None:
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            self._keepalive_task = None
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            self._writer = None
            self._reader = None

    async def __aenter__(self) -> "AsyncDahuaClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # -- framing ------------------------------------------------------------
    async def _recv_exact(self, n: int) -> bytes:
        assert self._reader is not None, "not connected"
        return await asyncio.wait_for(self._reader.readexactly(n), self.timeout)

    async def _send_frame(self, payload: dict, data: bytes = b"") -> None:
        assert self._writer is not None, "not connected"
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        msg_len = len(body)
        header = struct.pack(
            const.HEADER_FMT, const.HEADER_SIZE, const.DHIP_MAGIC,
            self.session, payload.get("id", 0),
            msg_len + len(data), 0, msg_len, len(data))
        self._writer.write(header + body + data)
        await self._writer.drain()

    async def recv_frame(self) -> tuple[dict, bytes, dict]:
        hdr = await self._recv_exact(const.HEADER_SIZE)
        (size, magic, session, req_id, pkg_len,
         pkg_idx, msg_len, data_len) = struct.unpack(const.HEADER_FMT, hdr)
        if magic != const.DHIP_MAGIC:
            raise DHIPError(f"bad magic 0x{magic:08x} (expected DHIP)")
        body = await self._recv_exact(pkg_len) if pkg_len else b""
        msg = body[:msg_len]
        data = body[msg_len:msg_len + data_len]
        try:
            obj = json.loads(msg.decode("utf-8")) if msg else {}
        except json.JSONDecodeError as e:
            raise DHIPError(f"invalid JSON in response: {e}") from e
        meta = {"session": session, "request_id": req_id,
                "package_index": pkg_idx, "data_length": data_len}
        return obj, data, meta

    # -- rpc ----------------------------------------------------------------
    async def request(self, method: str, params=None, *, data: bytes = b"",
                      extra: dict | None = None) -> tuple[dict, bytes]:
        async with self._lock:
            self._id += 1
            payload: dict = {"method": method, "id": self._id,
                             "params": {} if params is None else params}
            if self.session:
                payload["session"] = self.session
            if extra:
                payload.update(extra)
            await self._send_frame(payload, data)
            obj, blob, _ = await self.recv_frame()
            return obj, blob

    async def call(self, method: str, params=None, *, data: bytes = b"") -> Any:
        resp, _ = await self.request(method, params, data=data)
        if not resp.get("result"):
            err = resp.get("error") or {}
            raise DahuaError(err.get("message") or const.error_message(
                err.get("code"), "RPC call failed"),
                code=err.get("code"), method=method)
        return extract(resp)

    # -- login --------------------------------------------------------------
    async def login(self, username: str, password: str,
                    client_type: str = "Web3.0", *, keep_alive: bool = True) -> dict:
        if self._writer is None:
            await self.connect()
        self.username, self.password = username, password
        resp, _ = await self.request(const.LOGIN, {
            "userName": username, "password": "",
            "clientType": client_type, "loginType": "Direct"})
        self.session = resp.get("session", self.session) or 0
        params = resp.get("params", {}) or {}
        realm, random = params.get("realm"), params.get("random")
        self.encryption = params.get("encryption")
        if self.encryption and self.encryption not in ("Default", "OldDigest", ""):
            raise DHIPError(f"unsupported login encryption {self.encryption!r}")
        if resp.get("result") and realm is None:
            return resp
        if not realm or not random:
            raise LoginError(f"login challenge missing realm/random: {resp}")
        resp2, _ = await self.request(const.LOGIN, {
            "userName": username, "password": login_digest(username, password, realm, random),
            "clientType": client_type, "loginType": "Direct",
            "authorityType": "Default", "passwordType": "Default",
            "realm": realm, "random": random})
        if not resp2.get("result"):
            err = resp2.get("error") or {}
            raise LoginError(err.get("message") or "login failed",
                             code=err.get("code"), method=const.LOGIN)
        self.session = resp2.get("session", self.session) or self.session
        self._keepalive_interval = int(
            (resp2.get("params") or {}).get("keepAliveInterval", const.DEFAULT_KEEPALIVE)
            or const.DEFAULT_KEEPALIVE)
        if keep_alive:
            self._keepalive_task = asyncio.ensure_future(self._keepalive_loop())
        return resp2

    async def _keepalive_loop(self) -> None:
        iv = self._keepalive_interval
        delay = iv - 2 if iv > 5 else max(1, iv)
        try:
            while True:
                await asyncio.sleep(delay)
                await self.keep_alive(self._keepalive_interval)
                self.logger.debug("keep-alive ok")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("keep-alive stopped: %s", exc)

    async def keep_alive(self, timeout: int = const.DEFAULT_KEEPALIVE) -> tuple[dict, bytes]:
        return await self.request(const.KEEPALIVE, {"timeout": timeout, "active": True})

    async def logout(self) -> tuple[dict, bytes]:
        return await self.request(const.LOGOUT)

    # -- device / system ----------------------------------------------------
    async def get_system_info(self) -> dict:
        info = await self.call("magicBox.getSystemInfo")
        if isinstance(info, dict):
            self.device_info = info
        return info

    async def get_device_type(self) -> str:
        return (await self.call("magicBox.getDeviceType")).get("type", "")

    async def get_software_version(self) -> dict:
        return (await self.call("magicBox.getSoftwareVersion")).get("version", {})

    async def get_hardware_version(self) -> str:
        return (await self.call("magicBox.getHardwareVersion")).get("version", "")

    async def get_serial_number(self) -> str:
        return (await self.call("magicBox.getSerialNo")).get("sn", "")

    async def get_memory_info(self) -> dict:
        return await self.call("magicBox.getMemoryInfo")

    async def get_vendor(self) -> str:
        return (await self.call("magicBox.getVendor")).get("vendor", "")

    # -- config -------------------------------------------------------------
    async def get_config(self, name: str) -> Any:
        params = await self.call(const.GET_CONFIG, {"name": name})
        if isinstance(params, dict) and "table" in params:
            return params["table"]
        return params

    async def set_config(self, name: str, table: Any) -> dict:
        return await self.call(const.SET_CONFIG, {"name": name, "table": table})

    async def get_inner_server_config(self) -> Any:
        """Return the ``InnerServer`` config table (Telnet, SSH, FTP, ...)."""
        return await self.get_config("InnerServer")

    async def set_telnet(self, enable: bool = True) -> dict:
        """Enable or disable the built-in telnet server (``InnerServer.Telnet``).

        Read-modify-write so other ``InnerServer`` subkeys (SSH, FTP, …) are
        preserved.
        """
        table = await self.get_inner_server_config()
        if isinstance(table, dict):
            sub = table.get("Telnet")
            if not isinstance(sub, dict):
                sub = {}
                table["Telnet"] = sub
            sub["Enable"] = enable
        else:
            table = {"Telnet": {"Enable": enable}}
        return await self.set_config("InnerServer", table)

    async def telnet_enabled(self) -> bool:
        """Return ``True`` if the built-in telnet server is enabled."""
        table = await self.get_inner_server_config()
        if isinstance(table, dict):
            return bool(table.get("Telnet", {}).get("Enable", False))
        return False

    # -- users --------------------------------------------------------------
    async def get_users(self) -> list:
        params = await self.call(const.GET_USERS)
        return params.get("users", []) if isinstance(params, dict) else (params or [])

    async def get_groups(self) -> list:
        params = await self.call(const.GET_GROUPS)
        return params.get("groups", []) if isinstance(params, dict) else (params or [])

    async def add_user(self, name: str, password: str, group: str = "admin",
                       memo: str = "", authorities: list | None = None,
                       sharable: bool = True) -> dict:
        return await self.call(const.ADD_USER, {"user": {
            "Name": name, "Password": password, "Group": group, "Memo": memo,
            "Sharable": sharable, "AuthorityList": authorities or []}})

    async def delete_user(self, name: str) -> dict:
        return await self.call(const.DELETE_USER, {"name": name})

    # -- PTZ ----------------------------------------------------------------
    async def ptz_get_presets(self, channel: int = 0) -> list:
        params = await self.call(const.PTZ_GET_PRESETS, {"channel": channel})
        return params.get("presets") or [] if isinstance(params, dict) else (params or [])

    async def ptz_start(self, code: str, channel: int = 0,
                        arg1: int = 0, arg2: int = 0, arg3: int = 0) -> dict:
        return await self.call(const.PTZ_START, {
            "channel": channel, "code": code, "arg1": arg1, "arg2": arg2, "arg3": arg3})

    async def ptz_stop(self, code: str, channel: int = 0,
                       arg1: int = 0, arg2: int = 0, arg3: int = 0) -> dict:
        return await self.call(const.PTZ_STOP, {
            "channel": channel, "code": code, "arg1": arg1, "arg2": arg2, "arg3": arg3})

    async def ptz_move(self, code: str, channel: int = 0, speed: int = 4,
                       duration: float = 0.5) -> None:
        await self.ptz_start(code, channel=channel, arg2=speed)
        await asyncio.sleep(duration)
        await self.ptz_stop(code, channel=channel, arg2=speed)

    # -- time ---------------------------------------------------------------
    async def get_time(self) -> datetime:
        value = await self.call(const.GET_TIME)
        if isinstance(value, dict):
            value = value.get("time", "")
        return datetime.strptime(value, _TIME_FMT)

    async def set_time(self, when: datetime | None = None) -> dict:
        when = when or datetime.now()
        return await self.call(const.SET_TIME, {"time": when.strftime(_TIME_FMT)})

    # -- maintenance --------------------------------------------------------
    async def reboot(self) -> None:
        try:
            await self.call(const.REBOOT)
        finally:
            await self.close()

    # -- snapshot -----------------------------------------------------------
    async def snapshot(self, channel: int = 0, http_port: int = 80) -> bytes:
        """Capture a JPEG still via the HTTP CGI endpoint (digest auth)."""
        import base64
        import hashlib

        # Minimal async HTTP digest against /cgi-bin/snapshot.cgi.
        path = f"/cgi-bin/snapshot.cgi?channel={channel}"
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, http_port), self.timeout)
        try:
            challenge = await self._http_get(reader, writer, path, auth=None)
            auth = self._build_digest(challenge, path)
            reader2, writer2 = await asyncio.wait_for(
                asyncio.open_connection(self.host, http_port), self.timeout)
            try:
                body = await self._http_get(reader2, writer2, path, auth=auth, want_body=True)
                return body
            finally:
                writer2.close()
        finally:
            writer.close()

    async def _http_get(self, reader, writer, path, auth, want_body=False) -> Any:
        req = f"GET {path} HTTP/1.1\r\nHost: {self.host}\r\nConnection: close\r\n"
        if auth:
            req += f"Authorization: {auth}\r\n"
        req += "\r\n"
        writer.write(req.encode())
        await writer.drain()
        raw = await reader.read()
        head, _, body = raw.partition(b"\r\n\r\n")
        if want_body:
            return body
        return head.decode("latin1")

    def _build_digest(self, challenge_head: str, path: str) -> str:
        import hashlib
        import re
        m = re.search(r'WWW-Authenticate:\s*Digest\s*(.*)', challenge_head, re.I)
        if not m:
            return ""
        fields = dict(re.findall(r'(\w+)="?([^",]+)"?', m.group(1)))
        realm, nonce = fields.get("realm", ""), fields.get("nonce", "")
        qop = fields.get("qop")
        ha1 = hashlib.md5(f"{self.username}:{realm}:{self.password}".encode()).hexdigest()
        ha2 = hashlib.md5(f"GET:{path}".encode()).hexdigest()
        if qop:
            cnonce, nc = "0a4f113b", "00000001"
            resp = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode()).hexdigest()
            return (f'Digest username="{self.username}", realm="{realm}", nonce="{nonce}", '
                    f'uri="{path}", qop={qop}, nc={nc}, cnonce="{cnonce}", response="{resp}"')
        resp = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
        return (f'Digest username="{self.username}", realm="{realm}", nonce="{nonce}", '
                f'uri="{path}", response="{resp}"')

    # -- events -------------------------------------------------------------
    async def iter_events(self, codes: list[str] | None = None
                          ) -> AsyncIterator[dict]:
        """Yield device events as they are pushed.

        Opens its own login on *this* connection's credentials but a fresh
        socket, so it never blocks control RPCs.

        Usage::

            async for event in cam.iter_events(["VideoMotion"]):
                print(event)
        """
        listener = AsyncDahuaClient(self.host, self.port, self.timeout)
        await listener.connect()
        await listener.login(self.username, self.password, keep_alive=False)
        resp, _ = await listener.request(const.EVENT_ATTACH, {"codes": codes or ["All"]})
        if not resp.get("result"):
            await listener.close()
            raise DahuaError("event attach failed", method=const.EVENT_ATTACH)
        try:
            while True:
                obj, _, _ = await listener.recv_frame()
                params = obj.get("params") or {}
                for event in params.get("eventList", [params] if params else []):
                    if event:
                        yield event
        finally:
            await listener.close()
