#!/usr/bin/env python3
"""Record live video to a DHAV file over HTTP RPC2 + RPC_Loadfile.

Usage:  python examples/stream.py <host> [user] [password] [out.dhav] [seconds]

Note: requires a device whose HTTP RPC2 endpoint serves the streamReader /
RPC_Loadfile media flow. Not all firmwares do — prefer RTSP (examples/record_rtsp.py)
where available. See the README "Device support & status" section.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dahua import HttpMediaClient  # noqa: E402

host = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.10"
user = sys.argv[2] if len(sys.argv) > 2 else "admin"
pw = sys.argv[3] if len(sys.argv) > 3 else "admin54321"
out = sys.argv[4] if len(sys.argv) > 4 else "stream.dhav"
seconds = float(sys.argv[5]) if len(sys.argv) > 5 else 10.0

with HttpMediaClient(host) as cam:
    cam.login(user, pw)
    n = cam.record(out, channel=0, subtype=0, duration=seconds)
    print(f"wrote {n} bytes to {out}")
