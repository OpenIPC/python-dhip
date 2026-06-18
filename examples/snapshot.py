#!/usr/bin/env python3
"""Grab a JPEG snapshot.

Usage:  python examples/snapshot.py <host> [user] [password] [out.jpg]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dahua import DahuaClient  # noqa: E402

host = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.10"
user = sys.argv[2] if len(sys.argv) > 2 else "admin"
pw = sys.argv[3] if len(sys.argv) > 3 else "admin54321"
out = sys.argv[4] if len(sys.argv) > 4 else "snapshot.jpg"

with DahuaClient(host) as cam:
    cam.login(user, pw)
    data = cam.snapshot()
    with open(out, "wb") as fh:
        fh.write(data)
    is_jpeg = data[:2] == b"\xff\xd8"
    print(f"wrote {len(data)} bytes to {out} (JPEG: {is_jpeg})")
