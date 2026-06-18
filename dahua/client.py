"""High-level synchronous Dahua camera client."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any, Callable

from . import const
from .transport import DHIPTransport
from .events import EventListener

_TIME_FMT = "%Y-%m-%d %H:%M:%S"


class DahuaClient(DHIPTransport):
    """A Dahua IP camera over the DHIP (RPC2) protocol.

    Example::

        with DahuaClient("10.0.0.10") as cam:
            cam.login("admin", "admin54321")
            print(cam.get_system_info())
    """

    def __init__(self, host: str, port: int = const.DEFAULT_PORT,
                 timeout: float = const.DEFAULT_TIMEOUT):
        super().__init__(host, port, timeout)
        self.logger = logging.getLogger(__name__)
        self.username = ""
        self.password = ""
        self.device_info: dict = {}
        self._keepalive_interval = const.DEFAULT_KEEPALIVE
        self._keepalive_timer: threading.Timer | None = None
        self._keepalive_on = False
        # PTZ readout scale: ptz.getStatus reports raw encoder units (~0..8191
        # per axis) while ptz.moveAbsolutely is commanded in *degrees*. These
        # convert the raw readout to degrees; defaults follow the common Dahua
        # encoder convention and can be overridden per-device.
        self.ptz_location_fullscale = 8192   # raw units for a full pan revolution
        self.ptz_tilt_span_deg = 90.0        # physical tilt span in degrees

    # -- logging ------------------------------------------------------------
    def debug(self, fmt: str | None = None) -> None:
        """Enable DEBUG logging to stderr (opt-in, mirrors python-dvr)."""
        self.logger.setLevel(logging.DEBUG)
        handler = logging.StreamHandler()
        if fmt:
            handler.setFormatter(logging.Formatter(fmt))
        self.logger.addHandler(handler)

    # -- session lifecycle --------------------------------------------------
    def connect(self) -> None:
        super().connect()
        self.logger.debug("connected to %s:%s", self.host, self.port)

    def login(self, username: str, password: str,
              client_type: str = "Web3.0", *, keep_alive: bool = True) -> dict:
        """Authenticate; optionally start a background keep-alive timer."""
        if self.sock is None:
            self.connect()
        self.username = username
        self.password = password
        resp = super().login(username, password, client_type)
        self.logger.debug("logged in, session=0x%08x", self.session)
        params = resp.get("params") or {}
        self._keepalive_interval = int(
            params.get("keepAliveInterval", const.DEFAULT_KEEPALIVE) or const.DEFAULT_KEEPALIVE)
        if keep_alive:
            self._start_keepalive()
        return resp

    def _start_keepalive(self) -> None:
        self._keepalive_on = True
        self._arm_keepalive()

    def _arm_keepalive(self) -> None:
        if not self._keepalive_on:
            return
        # Re-arm a little before the device's interval to stay ahead of it.
        iv = self._keepalive_interval
        delay = iv - 2 if iv > 5 else max(1, iv)
        self._keepalive_timer = threading.Timer(delay, self._keepalive_tick)
        self._keepalive_timer.daemon = True
        self._keepalive_timer.start()

    def _keepalive_tick(self) -> None:
        try:
            self.keep_alive(self._keepalive_interval)
            self.logger.debug("keep-alive ok")
        except Exception as exc:  # noqa: BLE001 - timer thread must not die loudly
            self.logger.debug("keep-alive failed: %s", exc)
            self._keepalive_on = False
            return
        self._arm_keepalive()

    def close(self) -> None:
        self._keepalive_on = False
        if self._keepalive_timer is not None:
            self._keepalive_timer.cancel()
            self._keepalive_timer = None
        super().close()

    # -- device / system info ----------------------------------------------
    def get_system_info(self) -> dict:
        info = self.call("magicBox.getSystemInfo")
        if isinstance(info, dict):
            self.device_info = info
        return info

    def get_device_type(self) -> str:
        return self.call("magicBox.getDeviceType").get("type", "")

    def get_software_version(self) -> dict:
        return self.call("magicBox.getSoftwareVersion").get("version", {})

    def get_hardware_version(self) -> str:
        return self.call("magicBox.getHardwareVersion").get("version", "")

    def get_serial_number(self) -> str:
        return self.call("magicBox.getSerialNo").get("sn", "")

    def get_memory_info(self) -> dict:
        return self.call("magicBox.getMemoryInfo")

    def get_vendor(self) -> str:
        return self.call("magicBox.getVendor").get("vendor", "")

    # -- generic config -----------------------------------------------------
    def get_config(self, name: str) -> Any:
        """Return ``configManager.getConfig`` table for *name* (str or list)."""
        params = self.call(const.GET_CONFIG, {"name": name})
        if isinstance(params, dict) and "table" in params:
            return params["table"]
        return params

    def set_config(self, name: str, table: Any) -> dict:
        """Write a config table back via ``configManager.setConfig``."""
        return self.call(const.SET_CONFIG, {"name": name, "table": table})

    def get_general_config(self) -> Any:
        return self.get_config("General")

    def get_encode_config(self) -> Any:
        return self.get_config("Encode")

    def get_network_config(self) -> Any:
        return self.get_config("Network")

    def get_snap_config(self) -> Any:
        return self.get_config("Snap")

    # -- users & groups -----------------------------------------------------
    def get_users(self) -> list:
        params = self.call(const.GET_USERS)
        if isinstance(params, dict):
            return params.get("users", [])
        return params or []

    def get_groups(self) -> list:
        params = self.call(const.GET_GROUPS)
        if isinstance(params, dict):
            return params.get("groups", [])
        return params or []

    def get_active_users(self) -> Any:
        return self.call(const.GET_ACTIVE_USERS)

    def add_user(self, name: str, password: str, group: str = "admin",
                 memo: str = "", authorities: list | None = None,
                 sharable: bool = True) -> dict:
        user = {
            "Name": name,
            "Password": password,
            "Group": group,
            "Memo": memo,
            "Sharable": sharable,
            "AuthorityList": authorities or [],
        }
        return self.call(const.ADD_USER, {"user": user})

    def modify_user(self, name: str, **changes) -> dict:
        """Fetch the user, merge *changes*, and write it back."""
        users = self.get_users()
        current = next((u for u in users if u.get("Name") == name), None)
        if current is None:
            from .exceptions import DahuaError
            raise DahuaError(f"no such user: {name}", method=const.MODIFY_USER)
        merged = {**current, **changes}
        return self.call(const.MODIFY_USER, {"user": merged})

    def delete_user(self, name: str) -> dict:
        return self.call(const.DELETE_USER, {"name": name})

    def modify_password(self, name: str, old_password: str, new_password: str) -> dict:
        return self.call(const.MODIFY_PASSWORD, {
            "name": name,
            "oldPassword": old_password,
            "password": new_password,
        })

    # -- PTZ ----------------------------------------------------------------
    # PTZ actions return a bool: a `result:false` here is usually benign
    # (axis at its limit, "not moving", an op the firmware doesn't implement)
    # rather than an exception-worthy error, so they go through request().
    def _ptz_bool(self, method: str, params: dict) -> bool:
        resp, _ = self.request(method, params)
        return bool(resp.get("result"))

    # -- PTZ: status & capabilities ----------------------------------------
    def ptz_status(self, channel: int = 0) -> dict:
        """Full PTZ status, incl. ``Location`` ([pan, tilt(, zoom)])."""
        return self.call(const.PTZ_GET_STATUS, {"channel": channel}).get("status", {})

    def ptz_position(self, channel: int = 0) -> list:
        """Current raw ``[pan, tilt(, zoom)]`` position (encoder units, ~0..8191)."""
        return self.ptz_status(channel).get("Location", [])

    def ptz_position_degrees(self, channel: int = 0) -> tuple:
        """Current ``(pan_deg, tilt_deg)`` — the raw readout converted to degrees.

        Uses :attr:`ptz_location_fullscale` and :attr:`ptz_tilt_span_deg`; set
        those for your device if its encoder scale differs from the default.
        """
        loc = self.ptz_position(channel)
        if len(loc) < 2:
            return ()
        pan = loc[0] / self.ptz_location_fullscale * 360.0
        tilt = loc[1] / self.ptz_location_fullscale * self.ptz_tilt_span_deg
        return (round(pan, 1), round(tilt, 1))

    def ptz_caps(self, channel: int = 0) -> dict:
        """Protocol capabilities (pan/tilt speed ranges, etc.)."""
        return self.call(const.PTZ_GET_CAPS, {"channel": channel}).get("caps", {})

    def ptz_is_moving(self, channel: int = 0) -> bool:
        return self._ptz_bool(const.PTZ_IS_MOVING, {"channel": channel})

    # -- PTZ: low-level code API (the widely-supported interface) -----------
    def ptz_start(self, code: str, channel: int = 0,
                  arg1: int = 0, arg2: int = 0, arg3: int = 0) -> bool:
        return self._ptz_bool(const.PTZ_START, {
            "channel": channel, "code": code,
            "arg1": arg1, "arg2": arg2, "arg3": arg3})

    def ptz_stop(self, code: str, channel: int = 0,
                 arg1: int = 0, arg2: int = 0, arg3: int = 0) -> bool:
        return self._ptz_bool(const.PTZ_STOP, {
            "channel": channel, "code": code,
            "arg1": arg1, "arg2": arg2, "arg3": arg3})

    def ptz_move(self, code: str, channel: int = 0, speed: int = 4,
                 duration: float = 0.5) -> None:
        """Move with *code* (e.g. ``"Left"``) for *duration* seconds, then stop."""
        self.ptz_start(code, channel=channel, arg2=speed)
        time.sleep(duration)
        self.ptz_stop(code, channel=channel, arg2=speed)

    # -- PTZ: directional convenience --------------------------------------
    def ptz_up(self, channel: int = 0, speed: int = 4, duration: float = 0.5):
        self.ptz_move("Up", channel, speed, duration)

    def ptz_down(self, channel: int = 0, speed: int = 4, duration: float = 0.5):
        self.ptz_move("Down", channel, speed, duration)

    def ptz_left(self, channel: int = 0, speed: int = 4, duration: float = 0.5):
        self.ptz_move("Left", channel, speed, duration)

    def ptz_right(self, channel: int = 0, speed: int = 4, duration: float = 0.5):
        self.ptz_move("Right", channel, speed, duration)

    def ptz_zoom(self, direction: str, channel: int = 0, speed: int = 1,
                 duration: float = 0.5) -> None:
        """Zoom ``"in"``/``"tele"`` or ``"out"``/``"wide"`` for *duration* seconds."""
        code = "ZoomTele" if direction.lower() in ("in", "tele") else "ZoomWide"
        self.ptz_move(code, channel, speed, duration)

    def ptz_focus(self, direction: str, channel: int = 0, speed: int = 1,
                  duration: float = 0.5) -> None:
        """Focus ``"near"`` or ``"far"``."""
        code = "FocusNear" if direction.lower() == "near" else "FocusFar"
        self.ptz_move(code, channel, speed, duration)

    def ptz_iris(self, direction: str, channel: int = 0, speed: int = 1,
                 duration: float = 0.5) -> None:
        """Iris ``"open"``/``"large"`` or ``"close"``/``"small"``."""
        code = "IrisLarge" if direction.lower() in ("open", "large") else "IrisSmall"
        self.ptz_move(code, channel, speed, duration)

    # -- PTZ: absolute / relative / continuous -----------------------------
    def ptz_move_absolutely(self, pan: float, tilt: float, zoom: float = 0,
                            channel: int = 0) -> bool:
        """Slew to an absolute position, in **degrees**.

        *pan* is 0–360, *tilt* 0–90 (values out of range are rejected by the
        device). *zoom* is a magnification step. Read the resulting position
        back with :meth:`ptz_position_degrees`.
        """
        return self._ptz_bool(const.PTZ_MOVE_ABSOLUTELY,
                              {"channel": channel, "Position": [pan, tilt, zoom]})

    # Friendlier alias — same degree-based absolute positioning.
    def ptz_goto(self, pan: float, tilt: float, zoom: float = 0,
                 channel: int = 0) -> bool:
        return self.ptz_move_absolutely(pan, tilt, zoom, channel)

    def ptz_move_relatively(self, pan: float, tilt: float, zoom: float = 0,
                            channel: int = 0) -> bool:
        return self._ptz_bool(const.PTZ_MOVE_RELATIVELY,
                              {"channel": channel, "Position": [pan, tilt, zoom]})

    def ptz_move_continuously(self, x: float, y: float, z: float = 0,
                              channel: int = 0) -> bool:
        """Continuous move at velocities x (pan), y (tilt), z (zoom).

        Stop with :meth:`ptz_stop_move`. Not implemented on every firmware
        (the legacy :meth:`ptz_start`/:meth:`ptz_stop` code API is broader).
        """
        return self._ptz_bool(const.PTZ_MOVE_CONTINUOUSLY,
                              {"channel": channel, "x": x, "y": y, "z": z})

    def ptz_stop_move(self, channel: int = 0) -> bool:
        return self._ptz_bool(const.PTZ_STOP_MOVE, {"channel": channel})

    # -- PTZ: presets (legacy code API; works where ptz.setPreset doesn't) --
    def ptz_get_presets(self, channel: int = 0) -> list:
        params = self.call(const.PTZ_GET_PRESETS, {"channel": channel})
        if isinstance(params, dict):
            return params.get("presets") or []
        return params or []

    def ptz_goto_preset(self, index: int, channel: int = 0) -> bool:
        return self.ptz_start("GotoPreset", channel=channel, arg2=index)

    def ptz_set_preset(self, index: int, channel: int = 0) -> bool:
        return self.ptz_start("SetPreset", channel=channel, arg2=index)

    def ptz_clear_preset(self, index: int, channel: int = 0) -> bool:
        return self.ptz_start("ClearPreset", channel=channel, arg2=index)

    # -- PTZ: tours --------------------------------------------------------
    def ptz_get_tours(self, channel: int = 0) -> list:
        params = self.call(const.PTZ_GET_TOURS, {"channel": channel})
        if isinstance(params, dict):
            return params.get("tours") or []
        return params or []

    def ptz_start_tour(self, index: int, channel: int = 0) -> bool:
        return self.ptz_start("StartTour", channel=channel, arg2=index)

    def ptz_stop_tour(self, index: int = 0, channel: int = 0) -> bool:
        return self.ptz_stop("StopTour", channel=channel, arg2=index)

    # -- PTZ: home / reset -------------------------------------------------
    def ptz_goto_home(self, channel: int = 0) -> bool:
        """Go to the configured home position (falls back to the code API)."""
        if self._ptz_bool(const.PTZ_GOTO_HOME, {"channel": channel}):
            return True
        return self.ptz_start("GotoHome", channel=channel)

    def ptz_reset(self, channel: int = 0) -> bool:
        return self._ptz_bool(const.PTZ_RESET, {"channel": channel})

    # -- time ---------------------------------------------------------------
    def get_time(self) -> datetime:
        value = self.call(const.GET_TIME)
        if isinstance(value, dict):
            value = value.get("time", "")
        return datetime.strptime(value, _TIME_FMT)

    def set_time(self, when: datetime | None = None) -> dict:
        when = when or datetime.now()
        return self.call(const.SET_TIME, {"time": when.strftime(_TIME_FMT)})

    # -- maintenance --------------------------------------------------------
    def reboot(self) -> None:
        try:
            self.call(const.REBOOT)
        finally:
            self.close()

    def shutdown(self) -> None:
        try:
            self.call(const.SHUTDOWN)
        finally:
            self.close()

    def factory_reset(self, names: list | None = None) -> dict:
        return self.call(const.RESTORE_CONFIG, {"names": names or []})

    # -- snapshot -----------------------------------------------------------
    def snapshot(self, channel: int = 0, http_port: int = 80) -> bytes:
        """Capture a JPEG still from *channel*.

        Dahua DHIP devices serve snapshots over the HTTP CGI endpoint
        ``/cgi-bin/snapshot.cgi`` with digest auth, not over the RPC channel
        (the ``snapManager.attach`` RPC is not implemented on these cameras).
        Uses the credentials supplied to :meth:`login`.
        """
        import urllib.request

        url = (f"http://{self.host}:{http_port}/cgi-bin/snapshot.cgi"
               f"?channel={channel}")
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, url, self.username, self.password)
        opener = urllib.request.build_opener(
            urllib.request.HTTPDigestAuthHandler(mgr),
            urllib.request.HTTPBasicAuthHandler(mgr),
        )
        with opener.open(url, timeout=self.timeout) as resp:
            return resp.read()

    # -- events -------------------------------------------------------------
    def events(self, codes: list[str] | None = None, channel: int = 0) -> EventListener:
        """Return an :class:`EventListener` bound to this device's credentials.

        The listener opens its own connection so event streaming never blocks
        control RPCs on this client.
        """
        return EventListener(self.host, self.port, self.timeout,
                             codes=codes, channel=channel)

    def listen_events(self, callback: Callable[[dict], None],
                      username: str, password: str,
                      codes: list[str] | None = None, channel: int = 0) -> EventListener:
        """Convenience: open an event listener, log in, and start dispatching."""
        listener = self.events(codes=codes, channel=channel)
        listener.start(callback, username, password)
        return listener


# Backwards-compatible public name.
DHIPClient = DahuaClient
