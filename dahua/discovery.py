"""LAN discovery of Dahua devices via the DHIP multicast probe.

Dahua devices answer a ``DHDiscover.search`` probe sent to the multicast group
``239.255.255.251:37810``. The probe and replies use the same 32-byte DHIP
header framing as the RPC2 transport, with a JSON body.

.. note::
   Discovery requires being on the same L2 segment as the devices (multicast is
   not routed), so it could not be exercised against the remote test camera.
   The probe/parse follow the documented DHIP discovery format.
"""

from __future__ import annotations

import json
import socket
import struct

from . import const

DISCOVER_GROUP = "239.255.255.251"
DISCOVER_PORT = 37810
DISCOVER_METHOD = "DHDiscover.search"


def _frame(payload: dict) -> bytes:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    header = struct.pack(
        const.HEADER_FMT, const.HEADER_SIZE, const.DHIP_MAGIC,
        0, 0, len(body), 0, len(body), 0)
    return header + body


def _parse(data: bytes) -> dict | None:
    if len(data) < const.HEADER_SIZE:
        return None
    magic = struct.unpack_from(const.HEADER_FMT, data)[1]
    body = data[const.HEADER_SIZE:] if magic == const.DHIP_MAGIC else data
    try:
        obj = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return obj.get("params", {}).get("deviceInfo", obj.get("params", obj))


def discover(timeout: float = 2.0, interface_ip: str = "0.0.0.0") -> list[dict]:
    """Broadcast a discovery probe and collect device replies for *timeout* secs.

    Returns a list of device-info dicts (IP, serial, type, ... as the device
    reports them). Bind to a specific NIC with *interface_ip* on multi-homed hosts.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                    socket.inet_aton(interface_ip))
    sock.bind((interface_ip, 0))
    sock.settimeout(timeout)

    probe = _frame({"method": DISCOVER_METHOD, "params": {"mac": "", "uni": 1}})
    sock.sendto(probe, (DISCOVER_GROUP, DISCOVER_PORT))

    found: list[dict] = []
    seen: set = set()
    try:
        while True:
            try:
                data, addr = sock.recvfrom(8192)
            except socket.timeout:
                break
            info = _parse(data)
            if not info:
                continue
            key = info.get("SerialNo") or info.get("mac") or addr[0]
            if key in seen:
                continue
            seen.add(key)
            info.setdefault("_source", addr[0])
            found.append(info)
    finally:
        sock.close()
    return found
