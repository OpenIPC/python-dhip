"""Live video capture over Dahua's HTTP RPC2 + ``RPC_Loadfile`` transport.

The binary DHIP transport (:mod:`dahua.transport`) does not serve the live-media
subsystem on every device, and many Dahua cameras deliver real-time video over
the HTTP RPC2 channel instead:

1. ``POST /RPC2_Login`` — the same two-stage MD5 digest login as DHIP.
2. ``POST /RPC2`` ``streamReader.create {channel, subtype}`` -> a stream *object id*.
3. ``POST /RPC2`` ``streamReader.start {}`` with ``object`` = that id.
4. ``GET /RPC_Loadfile/<object id>`` on a second connection — the device then
   streams the raw **DHAV** ("DAHUA") media container as the HTTP response body.
5. ``streamReader.stop`` / ``streamReader.destroy`` to tear the stream down.

.. note::
   The ``RPC_Loadfile`` request line and the exact stream object are
   *reconstructed* from the documented Dahua web-client flow and validated
   end-to-end against the fake server in ``tests/test_media.py``. Not every
   firmware exposes the HTTP RPC2 media endpoint (prefer RTSP where available).
   The wire details live in one place (:meth:`HttpMediaClient._loadfile_request`)
   so they are easy to adjust for a given firmware.
"""

from __future__ import annotations

import http.client
import json
import logging
import time

from . import const
from .exceptions import DahuaError, LoginError
from .transport import login_digest

LOGIN_URL = "/RPC2_Login"
RPC_URL = "/RPC2"
LOADFILE_URL = "/RPC_Loadfile"
DHAV_MAGIC = b"DHAV"            # Dahua AV container magic


class HttpMediaClient:
    """Dahua RPC2-over-HTTP control + media client (default port 80)."""

    def __init__(self, host: str, port: int = 80,
                 timeout: float = const.DEFAULT_TIMEOUT):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.username = ""
        self.password = ""
        self.session = 0
        self._id = 0
        self.logger = logging.getLogger(__name__)
        self._conn = http.client.HTTPConnection(host, port, timeout=timeout)

    # -- low-level RPC ------------------------------------------------------
    @property
    def cookie(self) -> str:
        return f"DhWebClientSessionID={self.session}"

    def _post(self, url: str, payload: dict) -> dict:
        self._id += 1
        payload.setdefault("id", self._id)
        if self.session:
            payload.setdefault("session", self.session)
        headers = {"Content-Type": "application/json"}
        if self.session:
            headers["Cookie"] = self.cookie
        self._conn.request("POST", url, json.dumps(payload), headers)
        resp = self._conn.getresponse()
        body = resp.read()
        if resp.status != 200:
            raise DahuaError(f"HTTP {resp.status} for {payload.get('method')}",
                             code=resp.status, method=payload.get("method"))
        return json.loads(body.decode("utf-8")) if body else {}

    def _rpc(self, method: str, params=None, obj=None):
        payload: dict = {"method": method}
        if params is not None:
            payload["params"] = params
        if obj is not None:
            payload["object"] = obj
        resp = self._post(RPC_URL, payload)
        if not resp.get("result"):
            err = resp.get("error") or {}
            raise DahuaError(err.get("message") or const.error_message(
                err.get("code"), "RPC call failed"),
                code=err.get("code"), method=method)
        # streamReader.create returns the object id in `result`.
        result = resp.get("result")
        params_out = resp.get("params")
        if isinstance(params_out, dict) and params_out:
            return params_out
        return result

    # -- login --------------------------------------------------------------
    def login(self, username: str, password: str,
              client_type: str = "Web3.0") -> None:
        self.username, self.password = username, password
        first = self._post(LOGIN_URL, {
            "method": const.LOGIN,
            "params": {"userName": username, "password": "",
                       "clientType": client_type, "loginType": "Direct"}})
        self.session = first.get("session", 0) or 0
        params = first.get("params") or {}
        realm, random = params.get("realm"), params.get("random")
        if not realm or not random:
            raise LoginError(f"login challenge missing realm/random: {first}")
        resp = self._post(LOGIN_URL, {
            "method": const.LOGIN,
            "params": {
                "userName": username,
                "password": login_digest(username, password, realm, random),
                "clientType": client_type, "loginType": "Direct",
                "authorityType": "Default", "passwordType": "Default",
                "realm": realm, "random": random}})
        if not resp.get("result"):
            err = resp.get("error") or {}
            raise LoginError(err.get("message") or "login failed",
                             code=err.get("code"), method=const.LOGIN)
        self.session = resp.get("session", self.session) or self.session
        self.logger.debug("http rpc login ok, session=%s", self.session)

    # -- realtime stream ----------------------------------------------------
    def _loadfile_request(self, conn: http.client.HTTPConnection, obj) -> http.client.HTTPResponse:
        """Issue the RPC_Loadfile GET that begins the media byte stream."""
        conn.request("GET", f"{LOADFILE_URL}/{obj}", headers={"Cookie": self.cookie})
        return conn.getresponse()

    def record(self, out_path: str, channel: int = 0, subtype: int = 0,
               duration: float = 10.0, chunk_size: int = 65536) -> int:
        """Capture the live stream to *out_path*; return bytes written.

        Records for *duration* seconds (or until the device closes the stream).
        """
        obj = self._rpc("streamReader.create",
                        {"channel": channel, "subtype": subtype})
        if isinstance(obj, dict):
            obj = obj.get("instanceID") or obj.get("object") or obj
        try:
            self._rpc("streamReader.start", {}, obj=obj)
        except DahuaError as exc:
            # Some firmwares stream immediately on create; start is optional.
            self.logger.debug("streamReader.start not accepted: %s", exc)

        data_conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        written = 0
        try:
            resp = self._loadfile_request(data_conn, obj)
            if resp.status != 200:
                raise DahuaError(f"RPC_Loadfile HTTP {resp.status}",
                                 code=resp.status, method="RPC_Loadfile")
            deadline = None  # set on first byte to time the *stream*, not setup
            with open(out_path, "wb") as fh:
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    if deadline is None:
                        deadline = time.monotonic() + duration
                    fh.write(chunk)
                    written += len(chunk)
                    if deadline is not None and time.monotonic() >= deadline:
                        break
        finally:
            data_conn.close()
            self._safe_rpc("streamReader.stop", obj)
            self._safe_rpc("streamReader.destroy", obj)
        return written

    def download_file(self, remote_path: str, out_path: str,
                      chunk_size: int = 65536) -> int:
        """Download a recorded file (a ``FilePath`` from ``find_files``) to disk.

        Uses ``GET /RPC_Loadfile<remote_path>`` — the documented Dahua file
        export mechanism. Returns bytes written.
        """
        data_conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        written = 0
        try:
            data_conn.request("GET", f"{LOADFILE_URL}{remote_path}",
                              headers={"Cookie": self.cookie})
            resp = data_conn.getresponse()
            if resp.status != 200:
                raise DahuaError(f"RPC_Loadfile HTTP {resp.status}",
                                 code=resp.status, method="RPC_Loadfile")
            with open(out_path, "wb") as fh:
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    fh.write(chunk)
                    written += len(chunk)
        finally:
            data_conn.close()
        return written

    def _safe_rpc(self, method: str, obj) -> None:
        try:
            self._rpc(method, {}, obj=obj)
        except Exception as exc:  # noqa: BLE001 - teardown must not mask errors
            self.logger.debug("%s failed: %s", method, exc)

    # -- lifecycle ----------------------------------------------------------
    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "HttpMediaClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
