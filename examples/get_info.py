#!/usr/bin/env python3
"""Login and dump basic device info over DHIP.

Usage:  python examples/get_info.py <host> [user] [password]
"""
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dhip import DHIPClient  # noqa: E402

host = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.10"
user = sys.argv[2] if len(sys.argv) > 2 else "admin"
pw = sys.argv[3] if len(sys.argv) > 3 else "admin54321"

with DHIPClient(host) as c:
    c.login(user, pw)
    print(f"# session = 0x{c.session:08x}")
    for method in (
        "magicBox.getDeviceType",
        "magicBox.getSoftwareVersion",
        "magicBox.getHardwareVersion",
        "magicBox.getSerialNo",
        "magicBox.getSystemInfo",
        "Security.getUserInfoAll",   # accounts (requires authority)
    ):
        try:
            resp, _ = c.request(method)
            print(f"\n## {method}")
            print(json.dumps(resp.get("params", resp), indent=2, ensure_ascii=False))
        except Exception as e:  # noqa: BLE001
            print(f"\n## {method}  -> error: {e}")
