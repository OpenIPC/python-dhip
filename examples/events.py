#!/usr/bin/env python3
"""Subscribe to device events and print them as they arrive.

Usage:  python examples/events.py <host> [user] [password] [code ...]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dahua import DahuaClient  # noqa: E402

host = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.10"
user = sys.argv[2] if len(sys.argv) > 2 else "admin"
pw = sys.argv[3] if len(sys.argv) > 3 else "admin54321"
codes = sys.argv[4:] or ["All"]

with DahuaClient(host) as cam:
    cam.login(user, pw)
    listener = cam.events(codes=codes)
    print(f"listening for {codes} (Ctrl-C to stop) ...")
    try:
        listener.listen(lambda e: print("EVENT:", e), user, pw)
    except KeyboardInterrupt:
        listener.stop()
