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


class TestPTZ(unittest.TestCase):
    def _server(self):
        self.seen = []

        def record(req):
            self.seen.append((req["method"], req.get("params")))
            return {"result": True}

        handlers = {
            "ptz.start": record,
            "ptz.stop": record,
            "ptz.moveAbsolutely": record,
            "ptz.getStatus": lambda r: {"result": True,
                                        "params": {"status": {"Location": [4096, 2048]}}},
            "ptz.isMoving": lambda r: {"result": False, "params": None},
            "ptz.getCurrentProtocolCaps": lambda r: {
                "result": True, "params": {"caps": {"PanSpeedMax": 8}}},
            "ptz.getPresets": lambda r: {"result": True, "params": {"presets": None}},
        }
        return FakeDHIPServer(handlers)

    def test_position_and_caps(self):
        with self._server() as srv:
            with DahuaClient("127.0.0.1", srv.port) as cam:
                cam.login(USER, PASS, keep_alive=False)
                self.assertEqual(cam.ptz_position(), [4096, 2048])
                self.assertEqual(cam.ptz_caps()["PanSpeedMax"], 8)
                self.assertFalse(cam.ptz_is_moving())  # result:false != error

    def test_position_degrees_conversion(self):
        with self._server() as srv:
            with DahuaClient("127.0.0.1", srv.port) as cam:
                cam.login(USER, PASS, keep_alive=False)
                # raw [4096, 2048] with default 8192 full-scale, 90 deg tilt span
                pan, tilt = cam.ptz_position_degrees()
                self.assertEqual(pan, 180.0)   # 4096/8192*360
                self.assertEqual(tilt, 22.5)   # 2048/8192*90
                cam.ptz_location_fullscale = 4096  # override scale
                self.assertEqual(cam.ptz_position_degrees()[0], 360.0)

    def test_directional_sends_code(self):
        with self._server() as srv:
            with DahuaClient("127.0.0.1", srv.port) as cam:
                cam.login(USER, PASS, keep_alive=False)
                cam.ptz_left(speed=6, duration=0)
                starts = [p for m, p in self.seen if m == "ptz.start"]
                self.assertEqual(starts[0]["code"], "Left")
                self.assertEqual(starts[0]["arg2"], 6)
                self.assertTrue(any(m == "ptz.stop" for m, _ in self.seen))

    def test_zoom_maps_to_code(self):
        with self._server() as srv:
            with DahuaClient("127.0.0.1", srv.port) as cam:
                cam.login(USER, PASS, keep_alive=False)
                cam.ptz_zoom("in", duration=0)
                cam.ptz_zoom("out", duration=0)
                codes = [p["code"] for m, p in self.seen if m == "ptz.start"]
                self.assertIn("ZoomTele", codes)
                self.assertIn("ZoomWide", codes)

    def test_absolute_and_presets(self):
        with self._server() as srv:
            with DahuaClient("127.0.0.1", srv.port) as cam:
                cam.login(USER, PASS, keep_alive=False)
                self.assertTrue(cam.ptz_move_absolutely(100, 200, 0))
                abs_calls = [p for m, p in self.seen if m == "ptz.moveAbsolutely"]
                self.assertEqual(abs_calls[0]["Position"], [100, 200, 0])
                self.assertTrue(cam.ptz_goto_preset(3))
                self.assertTrue(cam.ptz_set_preset(3))
                presets = [p for m, p in self.seen
                           if m == "ptz.start" and p["code"] in ("GotoPreset", "SetPreset")]
                self.assertEqual(presets[0]["arg2"], 3)


class TestTelnet(unittest.TestCase):
    def test_set_telnet_read_modify_write(self):
        store = {"table": {"Telnet": {"Enable": False}, "SSH": {"Enable": True}}}

        def get_cfg(req):
            return {"result": True, "params": {"table": store["table"]}}

        def set_cfg(req):
            store["table"] = req["params"]["table"]
            return {"result": True, "params": {"options": None}}

        with FakeDHIPServer({"configManager.getConfig": get_cfg,
                             "configManager.setConfig": set_cfg}) as srv:
            with DahuaClient("127.0.0.1", srv.port) as cam:
                cam.login(USER, PASS, keep_alive=False)
                self.assertFalse(cam.telnet_enabled())
                cam.set_telnet(True)
                self.assertTrue(store["table"]["Telnet"]["Enable"])
                self.assertTrue(store["table"]["SSH"]["Enable"])  # preserved
                cam.set_telnet(False)
                self.assertFalse(store["table"]["Telnet"]["Enable"])
                self.assertTrue(store["table"]["SSH"]["Enable"])


class TestOSD(unittest.TestCase):
    def test_channel_title_round_trip(self):
        store = {"table": [{"Name": "cam0"}]}

        def get_cfg(req):
            return {"result": True, "params": {"table": store["table"]}}

        def set_cfg(req):
            store["table"] = req["params"]["table"]
            return {"result": True, "params": {"options": None}}

        with FakeDHIPServer({"configManager.getConfig": get_cfg,
                             "configManager.setConfig": set_cfg}) as srv:
            with DahuaClient("127.0.0.1", srv.port) as cam:
                cam.login(USER, PASS, keep_alive=False)
                self.assertEqual(cam.get_channel_titles(), ["cam0"])
                cam.set_channel_title("Lobby", 0)
                self.assertEqual(store["table"][0]["Name"], "Lobby")
                self.assertEqual(cam.get_channel_titles(), ["Lobby"])


class TestFindFiles(unittest.TestCase):
    def test_mediafilefind_flow(self):
        state = {"calls": 0}

        def next_file(req):
            state["calls"] += 1
            if state["calls"] == 1:
                return {"result": True, "params": {"found": 2, "infos": [
                    {"FilePath": "/mnt/sd/a.dav", "Length": 100},
                    {"FilePath": "/mnt/sd/b.dav", "Length": 200}]}}
            return {"result": True, "params": {"found": 0, "infos": []}}

        handlers = {
            "mediaFileFind.factory.create": lambda r: {"result": 77},
            "mediaFileFind.findFile": lambda r: {"result": True},
            "mediaFileFind.findNextFile": next_file,
            "mediaFileFind.close": lambda r: {"result": True},
            "mediaFileFind.destroy": lambda r: {"result": True},
        }
        with FakeDHIPServer(handlers) as srv:
            with DahuaClient("127.0.0.1", srv.port) as cam:
                cam.login(USER, PASS, keep_alive=False)
                files = cam.find_files("2026-06-01 00:00:00", "2026-06-18 23:59:59",
                                       channel=0, batch=2)
                self.assertEqual([f["FilePath"] for f in files],
                                 ["/mnt/sd/a.dav", "/mnt/sd/b.dav"])
                self.assertIn("mediaFileFind.destroy", srv.received)


class TestFirmware(unittest.TestCase):
    def test_upgrade_requires_confirm(self):
        with FakeDHIPServer({}) as srv:
            with DahuaClient("127.0.0.1", srv.port) as cam:
                cam.login(USER, PASS, keep_alive=False)
                with self.assertRaises(ValueError):
                    cam.upgrade_firmware(__file__)  # no confirm=True

    def test_upgrade_streams_file_in_chunks(self):
        append_params = []

        def send(req):
            # The device rejects appendData unless params is exactly
            # {"length": <payload length>}. Record params + the actual binary
            # payload length so a regression to {"Offset","Length"} is caught.
            append_params.append((req.get("params"), len(req.get("__data__", b""))))
            return {"result": True}

        handlers = {
            "upgrader.prepare": lambda r: {"result": True},
            "upgrader.appendData": send,
            "upgrader.execute": lambda r: {"result": True},
        }
        with FakeDHIPServer(handlers) as srv:
            with DahuaClient("127.0.0.1", srv.port) as cam:
                cam.login(USER, PASS, keep_alive=False)
                blob = os.path.join(os.path.dirname(__file__), "_fw.bin")
                with open(blob, "wb") as fh:
                    fh.write(b"X" * 10000)
                try:
                    seen = []
                    cam.upgrade_firmware(blob, confirm=True, chunk_size=4096,
                                         progress=lambda s, t: seen.append((s, t)))
                finally:
                    os.remove(blob)
                self.assertEqual(srv.received.count("upgrader.appendData"), 3)  # 4096*3 covers 10000
                self.assertIn("upgrader.prepare", srv.received)
                self.assertIn("upgrader.execute", srv.received)
                self.assertEqual(seen[-1], (10000, 10000))
                # params must be exactly {"length": N} with N == the real payload
                # length for every chunk (4096, 4096, 1808) — nothing else.
                self.assertEqual([p for p, _ in append_params],
                                 [{"length": 4096}, {"length": 4096}, {"length": 1808}])
                self.assertEqual([(p["length"], n) for p, n in append_params],
                                 [(4096, 4096), (4096, 4096), (1808, 1808)])


class TestDiscovery(unittest.TestCase):
    def test_probe_frame_and_parse_roundtrip(self):
        from dahua import discovery
        frame = discovery._frame(
            {"method": "DHDiscover.search", "params": {"mac": "", "uni": 1}})
        # well-formed DHIP frame
        import struct
        from dahua import const
        magic = struct.unpack_from(const.HEADER_FMT, frame)[1]
        self.assertEqual(magic, const.DHIP_MAGIC)
        # a reply frame parses back to the device info
        reply = discovery._frame({"method": "client.notifyDevInfo",
                                  "params": {"deviceInfo": {"SerialNo": "ABC",
                                                            "IPv4Address": {"IPAddress": "10.0.0.9"}}}})
        info = discovery._parse(reply)
        self.assertEqual(info["SerialNo"], "ABC")


class TestRtspUrl(unittest.TestCase):
    def test_default_and_oem_templates(self):
        from dahua import rtsp
        std = rtsp.build_rtsp_url("cam.lan", "admin", "secret", channel=1, subtype=0)
        self.assertEqual(
            std, "rtsp://admin:secret@cam.lan:554/cam/realmonitor?channel=1&subtype=0")
        oem = rtsp.build_rtsp_url("cam.lan", "admin", "secret", channel=1, subtype=1,
                                  template=rtsp.ZN_RTSP_TEMPLATE)
        self.assertEqual(oem, "rtsp://admin:secret@cam.lan:554/H264?ch=1&subtype=1")

    def test_credentials_are_url_escaped(self):
        from dahua import rtsp
        u = rtsp.build_rtsp_url("h", "ad@min", "p:w/d", channel=1)
        self.assertIn("ad%40min:p%3Aw%2Fd@h", u)

    def test_client_builds_url_from_login_creds(self):
        with FakeDHIPServer({}) as srv:
            with DahuaClient("127.0.0.1", srv.port) as cam:
                cam.login(USER, PASS, keep_alive=False)
                url = cam.rtsp_url(channel=1, subtype=0)
                self.assertIn(f"{USER}:{PASS}@127.0.0.1", url)
                self.assertIn("/cam/realmonitor?channel=1&subtype=0", url)


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
