"""Offline test for the HTTP RPC2 + RPC_Loadfile live-capture flow.

A fake HTTP server implements the documented Dahua media handshake
(/RPC2_Login, /RPC2 streamReader.*, /RPC_Loadfile) and streams a synthetic
DHAV payload, so the full client flow is validated end-to-end without a device.

Run:  python -m unittest tests.test_media
"""

import hashlib
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dahua import HttpMediaClient  # noqa: E402
from dahua.media import DHAV_MAGIC  # noqa: E402

USER, PASS = "admin", "admin54321"
REALM, RANDOM, SESSION = "Login to FAKE", "13572468", 0x0BADBEEF
# 3 fragments of a fake DHAV container.
PAYLOAD = DHAV_MAGIC + b"\x00" * 60 + b"FRAME-A" + b"FRAME-B" + b"dhav-tail"


def md5u(s):
    return hashlib.md5(s.encode()).hexdigest().upper()


def expected_digest():
    return md5u(f"{USER}:{RANDOM}:{md5u(f'{USER}:{REALM}:{PASS}')}")


class Handler(BaseHTTPRequestHandler):
    server_version = "FakeDahua/1.0"

    def log_message(self, *a):  # silence
        pass

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n)) if n else {}
        method = req.get("method")
        rid = req.get("id", 0)
        self.server.calls.append(method)

        if self.path == "/RPC2_Login":
            if not req["params"].get("password"):
                return self._json({"id": rid, "session": SESSION, "result": False,
                                   "error": {"code": 268632079, "message": "challenge"},
                                   "params": {"realm": REALM, "random": RANDOM,
                                              "encryption": "Default"}})
            ok = req["params"]["password"] == expected_digest()
            return self._json({"id": rid, "session": SESSION, "result": ok})

        if self.path == "/RPC2":
            if method == "streamReader.create":
                self.server.last_create = req["params"]
                return self._json({"id": rid, "result": 0xABCDEF, "params": None})
            if method in ("streamReader.start", "streamReader.stop",
                          "streamReader.destroy"):
                return self._json({"id": rid, "result": True, "params": None})
            return self._json({"id": rid, "result": False,
                               "error": {"code": 405, "message": "Method not allowed"}})
        self.send_error(404)

    def do_GET(self):
        if self.path == "/RPC_Loadfile/mnt/sd/clip.dav":
            body = b"DAV-RECORDED-FILE-BYTES" * 10
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == f"/RPC_Loadfile/{0xABCDEF}":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(PAYLOAD)))
            self.end_headers()
            # stream in chunks to exercise the read loop
            for i in range(0, len(PAYLOAD), 16):
                self.wfile.write(PAYLOAD[i:i + 16])
            return
        self.send_error(404)


class TestHttpMediaCapture(unittest.TestCase):
    def setUp(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.srv.calls = []
        self.srv.last_create = None
        self.port = self.srv.server_address[1]
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.t.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def test_record_saves_dhav_stream(self):
        out = os.path.join(os.path.dirname(__file__), "_capture.dhav")
        try:
            with HttpMediaClient("127.0.0.1", self.port) as cam:
                cam.login(USER, PASS)
                written = cam.record(out, channel=0, subtype=1, duration=5.0)
            self.assertEqual(written, len(PAYLOAD))
            with open(out, "rb") as fh:
                data = fh.read()
            self.assertEqual(data, PAYLOAD)
            self.assertTrue(data.startswith(DHAV_MAGIC))
            # the create carried the requested channel/subtype
            self.assertEqual(self.srv.last_create, {"channel": 0, "subtype": 1})
            # the stream was torn down
            self.assertIn("streamReader.stop", self.srv.calls)
            self.assertIn("streamReader.destroy", self.srv.calls)
        finally:
            if os.path.exists(out):
                os.remove(out)

    def test_download_recorded_file(self):
        out = os.path.join(os.path.dirname(__file__), "_clip.dav")
        try:
            with HttpMediaClient("127.0.0.1", self.port) as cam:
                cam.login(USER, PASS)
                n = cam.download_file("/mnt/sd/clip.dav", out)
            self.assertEqual(n, len(b"DAV-RECORDED-FILE-BYTES" * 10))
            with open(out, "rb") as fh:
                self.assertTrue(fh.read().startswith(b"DAV-RECORDED-FILE-BYTES"))
        finally:
            if os.path.exists(out):
                os.remove(out)

    def test_login_bad_password_raises(self):
        from dahua.exceptions import LoginError
        with HttpMediaClient("127.0.0.1", self.port) as cam:
            with self.assertRaises(LoginError):
                cam.login(USER, "wrong")


if __name__ == "__main__":
    unittest.main()
