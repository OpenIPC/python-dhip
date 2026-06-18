#!/usr/bin/env python3
"""Record live video over RTSP to a file (needs ffmpeg).

Usage:  python examples/record_rtsp.py <host> [user] [password] [out.mp4] [seconds]

The RTSP path is firmware-specific; this example uses the Zenointel OEM path
verified on the SD-2N-4G. For standard Dahua cameras, drop the `rtsp_template`
line (the default `/cam/realmonitor` path is used).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dahua import DahuaClient, rtsp  # noqa: E402

host = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.10"
user = sys.argv[2] if len(sys.argv) > 2 else "admin"
pw = sys.argv[3] if len(sys.argv) > 3 else "admin54321"
out = sys.argv[4] if len(sys.argv) > 4 else "rtsp.mp4"
seconds = float(sys.argv[5]) if len(sys.argv) > 5 else 10.0

with DahuaClient(host) as cam:
    cam.login(user, pw)
    cam.rtsp_template = rtsp.ZN_RTSP_TEMPLATE   # OEM path; remove for std Dahua
    print("url:", cam.rtsp_url(channel=1, subtype=0))
    cam.record_rtsp(out, channel=1, subtype=0, duration=seconds)
    print(f"wrote {os.path.getsize(out)} bytes to {out}")
