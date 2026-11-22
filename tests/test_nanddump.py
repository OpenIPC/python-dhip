"""Offline tests for dahua.nanddump: telnet framing, marker/rc handling,
base64 stream decoding, /proc/mtd parsing, and a fake busybox end-to-end.

stdlib unittest only (the CI runs `python -m unittest discover` without
pytest). The fake server speaks just enough of a busybox telnetd session to
exercise the real client: login prompt(s), an optional "Last login: ..."
banner, marker-framed commands with `dd | base64` payloads, and Telnet
control sequences deliberately split across writes.
"""
import base64
import os
import re
import socket
import tempfile
import threading
import unittest

from dahua.nanddump import (
    TelnetShell, ShellError, _MarkerSplit, _Base64FileSink, parse_proc_mtd,
    dump_partition, backup,
)

MTD_TEXT = """dev:    size   erasesize  name
mtd0: 00200000 00020000 "boot"
mtd1: 00040000 00020000 "defcfg"
mtd2: 003c0000 00020000 "kernel"
mtd3: 02a00000 00020000 "root"
mtd4: 01000000 00020000 "custom"
mtd5: 00c00000 00020000 "config"
mtd6: 02600000 00020000 "database"
mtd7: 00800000 00020000 "appdata1"
mtd8: 00300000 00020000 "appdata2"
mtd9: 00300000 00020000 "encrypt"
mtd10: 01b20000 0001f000 "rootfs"
mtd11: 00c1c000 0001f000 "custom"
mtd12: 00ae6000 0001f000 "config"
"""

SMALL_MTD = """dev:    size   erasesize  name
mtd0: 00000004 00020000 "boot"
mtd1: 00000010 00020000 "defcfg"
mtd2: 00001000 00020000 "kernel"
mtd10: 00000020 0001f000 "rootfs"
mtd11: 00000020 0001f000 "custom"
mtd12: 00000020 0001f000 "config"
"""

IAC_CTRL = re.compile(rb"\xff[\xfa-\xfe].")


class TestParsers(unittest.TestCase):

    def test_parse_proc_mtd_tags_ubi_volumes(self):
        parts = parse_proc_mtd(MTD_TEXT)
        self.assertEqual([p["idx"] for p in parts], list(range(13)))
        self.assertEqual([p["idx"] for p in parts if p["ubi"]], [10, 11, 12])
        self.assertEqual(parts[3]["size"], 0x2A00000)
        self.assertEqual([p["idx"] for p in parts if not p["ubi"]],
                         list(range(10)))

    def test_marker_split_normal_and_rc(self):
        m = _MarkerSplit("MK")
        emit = m.feed("noise\r\nMKCMD\n") + m.feed("PAYLOAD\nMK") \
            + m.feed("END0\r\n/ # ")
        # "MKEND" straddled the last two feeds: held-back bytes came back in order
        self.assertEqual(emit, "\nPAYLOAD\n")
        self.assertTrue(m.finished)
        self.assertEqual(m.rc_digits()[1], 0)

    def test_marker_split_cmd_and_end_straddle_chunks(self):
        m = _MarkerSplit("MK")
        emit = ""
        for chunk in ["echo $__m\"CMD\"; dd ...\r\n",   # echoed line: no marker
                      "M", "KCMD\nDATA",                # CMD marker split
                      "M", "KEND7\n"]:                  # END marker split
            emit += m.feed(chunk)
        self.assertEqual(emit, "\nDATA")
        self.assertTrue(m.finished)
        self.assertEqual(m.rc_digits()[1], 7)

    def test_base64_sink_decodes_stream_with_noise_and_crs(self):
        blob = bytes(range(256)) * 41
        armor = base64.b64encode(blob).decode()
        stream = "\r\n/ # \r\n" + armor + "\r\n"
        with tempfile.NamedTemporaryFile(suffix=".part", delete=False) as tf:
            path = tf.name
        try:
            sink = _Base64FileSink(path)
            for i in range(0, len(stream), 7):        # lines straddle feeds
                sink.feed(stream[i:i + 7])
            self.assertEqual(sink.finish(), len(blob))
            with open(path, "rb") as f:
                self.assertEqual(f.read(), blob)
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_base64_sink_rejects_corrupted_payload(self):
        with tempfile.NamedTemporaryFile(suffix=".part", delete=False) as tf:
            path = tf.name
        sink = _Base64FileSink(path)
        try:
            with self.assertRaises(Exception):
                sink.feed("QUJD\n")                    # a fine line
                sink.feed("ABCDE\n")                   # passes the charset
                sink.finish()                          # regex, not valid base64
        finally:
            os.unlink(path)


def _bare_shell(sock):
    sh = TelnetShell.__new__(TelnetShell)
    sh.sock = sock
    sh._carry = b""
    return sh


class TestNegotiate(unittest.TestCase):

    def test_iac_straddling_reads(self):
        a, b = socket.socketpair()
        try:
            sh = _bare_shell(a)
            self.assertEqual(sh._negotiate(b"\xff"), b"")
            self.assertEqual(sh._negotiate(b"\xfd\x18hello"), b"hello")
            self.assertEqual(sh._negotiate(b"\xff\xffend"), b"\xffend")
        finally:
            a.close()
            b.close()

    def test_sb_frame_split_across_reads(self):
        a, b = socket.socketpair()
        try:
            sh = _bare_shell(a)
            frame = b"\xff\xfa\x24\x00\x01\x02\xff\xf0tail"
            self.assertEqual(sh._negotiate(frame[:5]), b"")
            self.assertEqual(sh._negotiate(frame[5:]), b"tail")
        finally:
            a.close()
            b.close()


class FakeBusybox(threading.Thread):
    """Speaks a minimal busybox telnetd session over a real socket."""

    BANNER = b"\r\nBusyBox v1.26.1 built-in shell\r\n"

    def __init__(self, mtd=SMALL_MTD, blobs=b"", good_pass="ivideo",
                 last_login=False):
        super().__init__(daemon=True)
        self.good_pass, self.last_login = good_pass, last_login
        self.mtd, self.blobs = mtd, blobs
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.start()

    def run(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,),
                             daemon=True).start()

    @staticmethod
    def _clean(line):
        return IAC_CTRL.sub(b"", line).strip()

    def _recv_line(self, conn, rest):
        buf = bytearray(rest)
        while b"\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                raise ConnectionError
            buf += chunk
        line, _, rest = bytes(buf).partition(b"\n")
        return self._clean(line), rest

    def _serve(self, conn):
        try:
            conn.sendall(b"\xff\xfd\x01\r\nlogin: ")
            line, rest = self._recv_line(conn, b"")
            if line != b"root":
                conn.sendall(b"Login incorrect\r\n\r\nlogin: ")
                return
            conn.sendall(b"Password: ")
            line, rest = self._recv_line(conn, rest)
            if line != self.good_pass.encode():
                conn.sendall(b"Login incorrect\r\n\r\nlogin: ")
                return
            if self.last_login:
                conn.sendall(b"\r\nLast login: Sun Nov 22 21:00:00 UTC 2026"
                             b" on pts/0\r\n")
            conn.sendall(self.BANNER + b"/ # ")
            while True:
                line, rest = self._recv_line(conn, rest)
                m = re.search(rb"__m=(\w+);", line)
                if not m:
                    continue
                marker = m.group(1).decode()
                cmd = line.decode()
                out = b""
                if "cat /proc/mtd" in cmd:
                    out, rc = self.mtd.encode(), 0
                elif "dd if=" in cmd:
                    idx = int(re.search(r"mtd(\d+)ro", cmd).group(1))
                    armor = base64.b64encode(
                        self.blobs.get(idx, b"")
                        if isinstance(self.blobs, dict) else self.blobs)
                    # a prompt-noise line, then the armored payload
                    out = b"\r\n/ # \r\n" + armor + b"\r\n"
                    rc = 0
                elif "false" in cmd:
                    rc = 1
                else:
                    rc = 127
                payload = (f"{marker}CMD\n".encode() + out
                           + f"{marker}END{rc}\n".encode())
                if "dd if=" in cmd:
                    # fragment the stream deliberately, carrying a complete
                    # SB subnegotiation frame across the split read boundary
                    mid = len(payload) // 3
                    conn.sendall(payload[:mid] + b"\xff\xfa\x18")
                    conn.sendall(b"\x01\xff\xf0" + payload[mid:])
                else:
                    conn.sendall(payload)
                conn.sendall(b"/ # ")
        except (ConnectionError, OSError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


def _shell(srv, **kw):
    return TelnetShell("127.0.0.1", srv.port, user="root", **kw)


class TestEndToEnd(unittest.TestCase):

    def setUp(self):
        self.servers = []

    def tearDown(self):
        for srv in self.servers:
            srv.sock.close()

    def _server(self, **kw):
        srv = FakeBusybox(**kw)
        self.servers.append(srv)
        return srv

    def test_dump_byte_exact(self):
        import shutil
        tmp = tempfile.mkdtemp()
        try:
            blobs = {0: os.urandom(4), 1: os.urandom(16), 2: os.urandom(0x1000)}
            srv = self._server(blobs=blobs)
            sh = _shell(srv, password="ivideo")
            try:
                rc, mtd = sh.run("cat /proc/mtd")
                self.assertEqual(rc, 0)
                self.assertIn("mtd0", mtd)
                parts = parse_proc_mtd(mtd)
                self.assertEqual([p["idx"] for p in parts if not p["ubi"]],
                                 [0, 1, 2])
                for p in parts:
                    if p["ubi"]:
                        continue
                    path = os.path.join(tmp, f"mtd{p['idx']}.bin")
                    ok, n = dump_partition(sh, p, path)
                    self.assertTrue(ok)
                    self.assertEqual(n, p["size"])
                    self.assertEqual(n, len(blobs[p["idx"]]))
                    with open(path, "rb") as f:
                        self.assertEqual(f.read(), blobs[p["idx"]])
            finally:
                sh.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_logout_failed(self):
        srv = self._server()
        with self.assertRaises(ShellError):
            _shell(srv, password="wrong")

    def test_last_login_banner_is_not_a_login_failure(self):
        srv = self._server(last_login=True)
        sh = _shell(srv, password="ivideo")        # must not raise
        try:
            self.assertEqual(sh.run("false")[0], 1)
        finally:
            sh.close()

    def test_dump_failure_never_truncates_a_previous_backup(self):
        import shutil
        tmp = tempfile.mkdtemp()
        try:
            good = os.path.join(tmp, "mtd1-defcfg.bin")
            with open(good, "wb") as f:
                f.write(b"PREVIOUS GOOD BACKUP" + b"\x00" * 4)
            srv = self._server(blobs={1: os.urandom(0x10 - 1)})   # short by one
            sh = _shell(srv, password="ivideo")
            try:
                rc, mtd = sh.run("cat /proc/mtd")
                part = [p for p in parse_proc_mtd(mtd) if p["idx"] == 1][0]
                with self.assertRaises(ShellError):
                    dump_partition(sh, part, good)
                with open(good, "rb") as f:
                    self.assertTrue(f.read().startswith(b"PREVIOUS GOOD BACKUP"))
                self.assertEqual([], [n for n in os.listdir(tmp)
                                      if n.endswith(".part")])
            finally:
                sh.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_backup_default_skips_ubi_but_explicit_selects_it(self):
        import shutil
        tmp = tempfile.mkdtemp()
        try:
            blobs = {0: os.urandom(4), 1: os.urandom(16), 2: os.urandom(0x1000)}
            srv = self._server(blobs=blobs)
            res = backup("127.0.0.1", tmp, port=srv.port, user="root",
                         password="ivideo", verbose=False)
            self.assertEqual([r["idx"] for r in res], [0, 1, 2])   # 10-12 skip
            self.assertFalse(os.path.exists(
                os.path.join(tmp, "mtd12-config.bin")))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        tmp2 = tempfile.mkdtemp()
        try:
            ubi = {12: os.urandom(0x20)}
            srv2 = self._server(blobs=ubi)
            res = backup("127.0.0.1", tmp2, port=srv2.port, user="root",
                         password="ivideo", parts=[12], verbose=False)
            self.assertEqual(len(res), 1)
            self.assertEqual(res[0]["idx"], 12)
            self.assertTrue(res[0]["ubi"])
            with open(os.path.join(tmp2, "mtd12-config.bin"), "rb") as f:
                self.assertEqual(f.read(), ubi[12])
        finally:
            shutil.rmtree(tmp2, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
