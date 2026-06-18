#!/usr/bin/env python3
"""Async device info + snapshot.

Usage:  python examples/async_info.py <host> [user] [password]
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dahua import AsyncDahuaClient  # noqa: E402

host = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.10"
user = sys.argv[2] if len(sys.argv) > 2 else "admin"
pw = sys.argv[3] if len(sys.argv) > 3 else "admin54321"


async def main():
    async with AsyncDahuaClient(host) as cam:
        await cam.login(user, pw)
        print("device :", await cam.get_device_type())
        print("vendor :", await cam.get_vendor())
        print("time   :", await cam.get_time())
        img = await cam.snapshot()
        print("snapshot bytes:", len(img))


asyncio.run(main())
