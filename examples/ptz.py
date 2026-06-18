#!/usr/bin/env python3
"""Drive PTZ and read presets.

Usage:  python examples/ptz.py <host> [user] [password] [code] [duration]
        code defaults to "Left"; see dahua.const.PTZ_CODES for the full list.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dahua import DahuaClient  # noqa: E402

host = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.10"
user = sys.argv[2] if len(sys.argv) > 2 else "admin"
pw = sys.argv[3] if len(sys.argv) > 3 else "admin54321"
code = sys.argv[4] if len(sys.argv) > 4 else "Left"
duration = float(sys.argv[5]) if len(sys.argv) > 5 else 0.5

with DahuaClient(host) as cam:
    cam.login(user, pw)
    print("presets:", cam.ptz_get_presets())
    print(f"moving {code} for {duration}s ...")
    cam.ptz_move(code, duration=duration)
    print("done")
