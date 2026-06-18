#!/usr/bin/env python3
"""Login and dump basic device info over DHIP.

Usage:  python examples/get_info.py <host> [user] [password]
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dahua import DahuaClient  # noqa: E402

host = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.10"
user = sys.argv[2] if len(sys.argv) > 2 else "admin"
pw = sys.argv[3] if len(sys.argv) > 3 else "admin54321"

with DahuaClient(host) as cam:
    cam.login(user, pw)
    print(f"# session = 0x{cam.session:08x}")
    print("device_type     :", cam.get_device_type())
    print("vendor          :", cam.get_vendor())
    print("serial_number   :", cam.get_serial_number())
    print("hardware_version:", cam.get_hardware_version())
    print("software_version:", json.dumps(cam.get_software_version()))
    print("memory          :", cam.get_memory_info())
    print("time            :", cam.get_time())
    print("users           :", [u.get("Name") for u in cam.get_users()])
