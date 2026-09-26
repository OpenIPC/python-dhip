"""A scriptable in-process DHIP server for offline tests.

Speaks the same 32-byte-header framing as a real device and runs a two-stage
login. Handlers for individual RPC methods are registered per-test; unknown
methods get a 405 error envelope, matching real-device behaviour.
"""

import hashlib
import json
import socket
import struct
import threading

HEADER_FMT = "<IIIIIIII"
HEADER_SIZE = 32
DHIP_MAGIC = 0x50494844

USER, PASS = "admin", "admin54321"
REALM, RANDOM, SESSION = "Login to FAKE", "987654321", 0x0BADF00D


def md5u(s):
    return hashlib.md5(s.encode()).hexdigest().upper()


def expected_digest():
    return md5u(f"{USER}:{RANDOM}:{md5u(f'{USER}:{REALM}:{PASS}')}")


class FakeDHIPServer:
    """Threaded loopback DHIP server. Use as a context manager."""

    def __init__(self, handlers=None):
        # handlers: method-name -> callable(req_dict) -> response dict (sans id)
        self.handlers = handlers or {}
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.received = []          # every request method seen
        self._thread = None

    # -- framing ------------------------------------------------------------
    @staticmethod
    def _recv(sock, n):
        buf = b""
        while len(buf) < n:
            c = sock.recv(n - len(buf))
            if not c:
                raise EOFError
            buf += c
        return buf

    def _read_frame(self, sock):
        hdr = self._recv(sock, HEADER_SIZE)
        _, magic, sess, rid, pkg, idx, mlen, dlen = struct.unpack(HEADER_FMT, hdr)
        assert magic == DHIP_MAGIC
        body = self._recv(sock, pkg) if pkg else b""
        obj = json.loads(body[:mlen].decode())
        if dlen:
            # expose the trailing binary payload so handlers can verify a request
            # whose params must describe it (e.g. upgrader.appendData length).
            obj["__data__"] = body[mlen:mlen + dlen]
        return obj, sess

    def _send(self, sock, obj, data=b"", index=0):
        body = json.dumps(obj, separators=(",", ":")).encode()
        hdr = struct.pack(HEADER_FMT, HEADER_SIZE, DHIP_MAGIC, SESSION,
                          obj.get("id", 0), len(body) + len(data), index,
                          len(body), len(data))
        sock.sendall(hdr + body + data)

    # -- lifecycle ----------------------------------------------------------
    def __enter__(self):
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        try:
            self.srv.close()
        except Exception:
            pass

    def _serve(self):
        try:
            conn, _ = self.srv.accept()
        except OSError:
            return
        with conn:
            self._handle(conn)

    def _handle(self, conn):
        # stage 1: challenge
        req, _ = self._read_frame(conn)
        self.received.append(req["method"])
        self._send(conn, {"id": req["id"], "session": SESSION, "result": False,
                          "error": {"code": 268632079, "message": "login challenge!"},
                          "params": {"realm": REALM, "random": RANDOM,
                                     "encryption": "Default"}})
        # stage 2: verify digest
        req, _ = self._read_frame(conn)
        ok = req["params"]["password"] == expected_digest()
        self._send(conn, {"id": req["id"], "session": SESSION, "result": ok,
                          "params": {"keepAliveInterval": 1}})
        if not ok:
            return
        # serve method calls until the client disconnects
        while True:
            try:
                req, _ = self._read_frame(conn)
            except (EOFError, OSError):
                break
            self.received.append(req["method"])
            handler = self.handlers.get(req["method"])
            if handler is None:
                self._send(conn, {"id": req["id"], "result": False,
                                  "error": {"code": 405, "message": "Method not allowed"}})
                continue
            try:
                result = handler(req)
            except StopIteration:
                break
            if result is None:
                continue
            data = result.pop("__data__", b"")
            frames = result.pop("__frames__", None)
            result["id"] = req["id"]
            if frames:
                # emit a binary payload split across multiple fragments
                for i, chunk in enumerate(frames):
                    self._send(conn, result if i == 0 else {"id": req["id"]},
                               data=chunk, index=i)
            else:
                self._send(conn, result, data=data)
