"""Event / alarm streaming over a dedicated DHIP connection.

``eventManager.attach`` is a long-lived call: the device returns one
``result: true`` acknowledgement and then *pushes* event frames sharing the
same request id indefinitely. To avoid that read loop blocking ordinary
control RPCs, an :class:`EventListener` owns its **own** socket and login —
fully independent of the :class:`~dahua.client.DahuaClient` that spawned it.
"""

from __future__ import annotations

import threading
from typing import Callable

from . import const
from .transport import DHIPTransport


class EventListener:
    """Subscribe to device events and dispatch them to a callback."""

    def __init__(self, host: str, port: int = const.DEFAULT_PORT,
                 timeout: float = const.DEFAULT_TIMEOUT,
                 codes: list[str] | None = None, channel: int = 0):
        self.codes = codes or ["All"]
        self.channel = channel
        self._transport = DHIPTransport(host, port, timeout)
        self._thread: threading.Thread | None = None
        self._running = False
        self._attach_id = 0
        self.sid = 0

    # -- lifecycle ----------------------------------------------------------
    def attach(self, username: str, password: str) -> None:
        """Connect, log in, and register the event subscription."""
        self._transport.connect()
        self._transport.login(username, password)
        resp, _ = self._transport.request(
            const.EVENT_ATTACH,
            {"codes": self.codes},
            extra={} if self.channel == 0 else None,
        )
        if not resp.get("result"):
            self._transport.close()
            self._transport._check(resp, const.EVENT_ATTACH)  # raises DahuaError
        self._attach_id = resp.get("id", 0)
        self.sid = (resp.get("params") or {}).get("SID", 0)

    def start(self, callback: Callable[[dict], None],
              username: str, password: str) -> None:
        """Attach (if needed) and dispatch events to *callback* in a daemon thread."""
        if self._attach_id == 0:
            self.attach(username, password)
        self._running = True
        self._thread = threading.Thread(target=self._loop, args=(callback,), daemon=True)
        self._thread.start()

    def _loop(self, callback: Callable[[dict], None]) -> None:
        while self._running:
            try:
                obj, _, _ = self._transport.recv_frame()
            except Exception:  # noqa: BLE001 - socket closed on stop()
                break
            if not self._running:
                break
            # Pushed events arrive as `client.notifyEventStream` frames whose
            # params carry an `eventList`; fall back to the params dict itself
            # for firmwares that push a single bare event.
            params = obj.get("params") or {}
            event_list = params.get("eventList")
            if event_list is None:
                event_list = [params] if params else []
            for event in event_list:
                if event:
                    callback(event)

    def stop(self) -> None:
        """Stop dispatching and tear down the connection."""
        self._running = False
        try:
            if self._transport.session:
                self._transport.request(const.EVENT_DETACH, {"id": self._attach_id})
        except Exception:  # noqa: BLE001
            pass
        self._transport.close()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def listen(self, callback: Callable[[dict], None],
               username: str, password: str) -> None:
        """Blocking variant: attach and dispatch on the current thread."""
        if self._attach_id == 0:
            self.attach(username, password)
        self._running = True
        self._loop(callback)

    # -- context manager ----------------------------------------------------
    def __enter__(self) -> "EventListener":
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
