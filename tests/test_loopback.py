"""Offline self-test: a tiny in-process DHIP server validates framing + login.

Run:  python tests/test_loopback.py
"""
import hashlib
import json
import os
import socket
import struct
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dhip import DHIPClient, DHIP_MAGIC, HEADER_FMT, HEADER_SIZE  # noqa: E402

USER, PASS = "admin", "admin54321"
REALM, RANDOM, SESSION = "Login to NC-IPTC2200", "123456789", 0x12345678


def _md5u(s):
    return hashlib.md5(s.encode()).hexdigest().upper()


def _recv(sock, n):
    buf = b""
    while len(buf) < n:
        c = sock.recv(n - len(buf))
        if not c:
            raise EOFError
        buf += c
    return buf


def _read_frame(sock):
    hdr = _recv(sock, HEADER_SIZE)
    size, magic, sess, rid, pkg, idx, mlen, dlen = struct.unpack(HEADER_FMT, hdr)
    assert size == HEADER_SIZE and magic == DHIP_MAGIC, "client sent bad header"
    body = _recv(sock, pkg) if pkg else b""
    return json.loads(body[:mlen].decode()), sess


def _send_frame(sock, obj, session):
    body = json.dumps(obj, separators=(",", ":")).encode()
    hdr = struct.pack(HEADER_FMT, HEADER_SIZE, DHIP_MAGIC, session,
                      obj.get("id", 0), len(body), 0, len(body), 0)
    sock.sendall(hdr + body)


def fake_server(srv):
    conn, _ = srv.accept()
    with conn:
        # stage 1: challenge
        req, _ = _read_frame(conn)
        assert req["method"] == "global.login"
        _send_frame(conn, {"id": req["id"], "session": SESSION, "result": False,
                           "error": {"code": 268632079, "message": "login challenge!"},
                           "params": {"realm": REALM, "random": RANDOM,
                                      "encryption": "Default"}}, SESSION)
        # stage 2: verify digest exactly as the client computes it
        req, sess = _read_frame(conn)
        assert sess == SESSION, "client must reuse challenge session in header"
        expect = _md5u(f"{USER}:{RANDOM}:{_md5u(f'{USER}:{REALM}:{PASS}')}")
        ok = req["params"]["password"] == expect
        _send_frame(conn, {"id": req["id"], "session": SESSION, "result": ok}, SESSION)
        # one method call
        req, _ = _read_frame(conn)
        assert req["method"] == "magicBox.getSystemInfo"
        _send_frame(conn, {"id": req["id"], "result": True,
                           "params": {"deviceType": "NC-IPTC2200_DL_4G-4"}}, SESSION)


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    t = threading.Thread(target=fake_server, args=(srv,), daemon=True)
    t.start()

    with DHIPClient("127.0.0.1", port) as c:
        r = c.login(USER, PASS)
        assert r["result"] is True, "login should succeed"
        assert c.session == SESSION, "session id should be captured"
        resp, _ = c.request("magicBox.getSystemInfo")
        assert resp["params"]["deviceType"] == "NC-IPTC2200_DL_4G-4"
    t.join(timeout=2)
    print("OK: header pack/unpack + two-stage login + method call all passed")


if __name__ == "__main__":
    main()
