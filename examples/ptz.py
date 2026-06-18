#!/usr/bin/env python3
"""Exercise the PTZ API: read position, nudge, zoom, absolute move, presets.

Usage:  python examples/ptz.py <host> [user] [password]
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dahua import DahuaClient  # noqa: E402

host = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.10"
user = sys.argv[2] if len(sys.argv) > 2 else "admin"
pw = sys.argv[3] if len(sys.argv) > 3 else "admin54321"

with DahuaClient(host) as cam:
    cam.login(user, pw)
    print("caps     :", cam.ptz_caps())
    print("position :", cam.ptz_position())
    print("moving?  :", cam.ptz_is_moving())
    print("presets  :", cam.ptz_get_presets())

    print("nudge left, then right ...")
    cam.ptz_left(duration=0.4)
    cam.ptz_right(duration=0.4)

    print("zoom in / focus far ...")
    cam.ptz_zoom("in", duration=0.4)
    cam.ptz_focus("far", duration=0.3)

    print("absolute move to pan=3000 tilt=2000 ...")
    cam.ptz_move_absolutely(3000, 2000)
    time.sleep(2)
    print("position :", cam.ptz_position())

    print("store preset 7, go to it, clear it ...")
    cam.ptz_set_preset(7)
    cam.ptz_goto_preset(7)
    cam.ptz_clear_preset(7)
