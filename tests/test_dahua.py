"""Offline unit tests for the dahua package (no real device required).

Run:  python -m pytest tests/test_dahua.py
  or:  python -m unittest tests.test_dahua
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.fake_server import FakeDHIPServer, USER, PASS  # noqa: E402

from dahua import DahuaClient, DHIPClient, DahuaError, DHIPError  # noqa: E402
from dahua import DHIP_MAGIC, HEADER_FMT, HEADER_SIZE  # noqa: E402
import dhip as dhip_shim  # noqa: E402


class TestBackwardCompat(unittest.TestCase):
    def test_shim_exports(self):
        # The original single-file imports must keep working.
        self.assertIs(dhip_shim.DHIPClient, DahuaClient)
        self.assertEqual(dhip_shim.DHIP_MAGIC, DHIP_MAGIC)
        self.assertEqual(dhip_shim.HEADER_SIZE, HEADER_SIZE)
        self.assertEqual(dhip_shim.HEADER_FMT, HEADER_FMT)
        self.assertTrue(callable(dhip_shim._md5_upper))

    def test_alias(self):
        self.assertIs(DHIPClient, DahuaClient)


class TestLoginAndRpc(unittest.TestCase):
    def test_login_and_request_no_raise(self):
        handlers = {
            "magicBox.getSystemInfo": lambda r: {
                "result": True, "params": {"deviceType": "FAKE-CAM"}},
        }
        with FakeDHIPServer(handlers) as srv:
            with DahuaClient("127.0.0.1", srv.port) as cam:
                resp = cam.login(USER, PASS, keep_alive=False)
                self.assertTrue(resp["result"])
                # request() keeps its no-raise (dict, bytes) contract
                obj, data = cam.request("magicBox.getSystemInfo")
                self.assertEqual(obj["params"]["deviceType"], "FAKE-CAM")
                self.assertEqual(data, b"")

    def test_call_unwraps_and_caches(self):
        handlers = {
            "magicBox.getSystemInfo": lambda r: {
                "result": True, "params": {"deviceType": "FAKE-CAM"}},
        }
        with FakeDHIPServer(handlers) as srv:
            with DahuaClient("127.0.0.1", srv.port) as cam:
                cam.login(USER, PASS, keep_alive=False)
                info = cam.get_system_info()
                self.assertEqual(info["deviceType"], "FAKE-CAM")
                self.assertEqual(cam.device_info, info)

    def test_bad_password_raises_login_error(self):
        with FakeDHIPServer({}) as srv:
            with DahuaClient("127.0.0.1", srv.port) as cam:
                with self.assertRaises(DHIPError):
                    cam.login(USER, "wrong-password", keep_alive=False)


class TestErrorEnvelope(unittest.TestCase):
    def test_call_raises_dahua_error_on_405(self):
        with FakeDHIPServer({}) as srv:
            with DahuaClient("127.0.0.1", srv.port) as cam:
                cam.login(USER, PASS, keep_alive=False)
                with self.assertRaises(DahuaError) as ctx:
                    cam.call("deviceInfo.getVendor")
                self.assertEqual(ctx.exception.code, 405)

    def test_request_does_not_raise_on_405(self):
        with FakeDHIPServer({}) as srv:
            with DahuaClient("127.0.0.1", srv.port) as cam:
                cam.login(USER, PASS, keep_alive=False)
                obj, _ = cam.request("deviceInfo.getVendor")
                self.assertFalse(obj["result"])
                self.assertEqual(obj["error"]["code"], 405)


class TestConfig(unittest.TestCase):
    def test_get_config_unwraps_table(self):
        handlers = {
            "configManager.getConfig": lambda r: {
                "result": True, "params": {"table": {"MachineName": "cam1"}}},
        }
        with FakeDHIPServer(handlers) as srv:
            with DahuaClient("127.0.0.1", srv.port) as cam:
                cam.login(USER, PASS, keep_alive=False)
                self.assertEqual(cam.get_config("General"), {"MachineName": "cam1"})

    def test_set_config_sends_table(self):
        seen = {}

        def setter(req):
            seen.update(req["params"])
            return {"result": True}

        with FakeDHIPServer({"configManager.setConfig": setter}) as srv:
            with DahuaClient("127.0.0.1", srv.port) as cam:
                cam.login(USER, PASS, keep_alive=False)
                cam.set_config("General", {"MachineName": "cam2"})
                self.assertEqual(seen["name"], "General")
                self.assertEqual(seen["table"], {"MachineName": "cam2"})


class TestUsers(unittest.TestCase):
    def test_get_users_from_params_users(self):
        handlers = {
            "userManager.getUserInfoAll": lambda r: {
                "result": True, "params": {"users": [{"Name": "admin"}]}},
            # groups come back as a bare list under params (real-device quirk)
            "userManager.getGroupInfoAll": lambda r: {
                "result": True, "params": [{"Name": "admin"}, {"Name": "user"}]},
        }
        with FakeDHIPServer(handlers) as srv:
            with DahuaClient("127.0.0.1", srv.port) as cam:
                cam.login(USER, PASS, keep_alive=False)
                self.assertEqual([u["Name"] for u in cam.get_users()], ["admin"])
                self.assertEqual([g["Name"] for g in cam.get_groups()],
                                 ["admin", "user"])


class TestTime(unittest.TestCase):
    def test_get_time_from_result_field(self):
        # global.getCurrentTime returns the value in `result`, not `params`.
        handlers = {
            "global.getCurrentTime": lambda r: {
                "result": "2026-06-18 11:46:19", "params": None},
        }
        with FakeDHIPServer(handlers) as srv:
            with DahuaClient("127.0.0.1", srv.port) as cam:
                cam.login(USER, PASS, keep_alive=False)
                dt = cam.get_time()
                self.assertEqual((dt.year, dt.month, dt.day), (2026, 6, 18))


class TestMultiFragment(unittest.TestCase):
    def test_reassemble_split_binary(self):
        # Snapshot-style multi-frame binary is read frame-by-frame.
        chunks = [b"AAAA", b"BBBB", b"CCCC"]

        def snap(req):
            return {"result": True, "params": {"SID": 1}, "__frames__": chunks}

        with FakeDHIPServer({"snapManager.attach": snap}) as srv:
            with DahuaClient("127.0.0.1", srv.port) as cam:
                cam.login(USER, PASS, keep_alive=False)
                resp, first = cam.call_raw("snapManager.attach", {"Type": "All"})
                acc = bytearray(first)
                for _ in range(len(chunks) - 1):
                    _, data, _ = cam.recv_frame()
                    acc += data
                self.assertEqual(bytes(acc), b"".join(chunks))


class TestKeepAlive(unittest.TestCase):
    def test_keepalive_timer_fires_and_cancels(self):
        hits = {"n": 0}

        def ka(req):
            hits["n"] += 1
            return {"result": True}

        with FakeDHIPServer({"global.keepAlive": ka}) as srv:
            cam = DahuaClient("127.0.0.1", srv.port)
            cam.connect()
            cam.login(USER, PASS, keep_alive=True)  # interval=1 from server
            time.sleep(1.5)
            cam.close()
            self.assertGreaterEqual(hits["n"], 1)
            # after close, the timer is cancelled — count stays put
            settled = hits["n"]
            time.sleep(1.5)
            self.assertEqual(hits["n"], settled)


if __name__ == "__main__":
    unittest.main()
