"""Offline tests for dahua.nanddump: telnet framing, marker/rc handling,
base64 stream decoding, /proc/mtd parsing, and a fake busybox end-to-end.

The fake server speaks just enough of a busybox telnet session to exercise
the real client: login prompt(s), an optional "Last login: ..." banner,
marker-framed commands with `dd | base64` payloads, and Telnet control
sequences deliberately split across writes.
"""
import base64
import os
import re
import socket
import tempfile
import threading

import pytest

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


# -- parsers -------------------------------------------------------------------

def test_parse_proc_mtd_tags_ubi_volumes():
    parts = parse_proc_mtd(MTD_TEXT)
    assert [p["idx"] for p in parts] == list(range(13))
    assert [p["idx"] for p in parts if p["ubi"]] == [10, 11, 12]
    assert parts[3]["size"] == 0x2A00000
    assert [p["idx"] for p in parts if not p["ubi"]] == list(range(10))


def test_marker_split_normal_and_rc():
    m = _MarkerSplit("MK")
    emit = m.feed("noise\r\nMKCMD\n") + m.feed("PAYLOAD\nMK") + m.feed("END0\r\n/ # ")
    # "MKEND" straddled the last two feeds: the held-back bytes came back in order
    assert emit == "\nPAYLOAD\n"
    assert m.finished
    assert m.rc_digits()[1] == 0


def test_marker_split_cmd_and_end_straddle_chunks():
    m = _MarkerSplit("MK")
    emit = ""
    for chunk in ["echo $__m\"CMD\"; dd ...\r\n",   # echoed line: no marker
                  "M", "KCMD\nDATA",                # CMD marker split
                  "M", "KEND7\n"]:                  # END marker split
        emit += m.feed(chunk)
    assert emit == "\nDATA"
    assert m.finished
    assert m.rc_digits()[1] == 7


def test_base64_sink_decodes_stream_with_noise_and_crs():
    blob = bytes(range(256)) * 41
    armor = base64.b64encode(blob).decode()
    # wrap in CRs and prompt noise, feed in awkward small chunks
    stream = "\r\n/ # \r\n" + armor + "\r\n"
    with tempfile.NamedTemporaryFile(suffix=".part", delete=False) as tf:
        path = tf.name
    sink = _Base64FileSink(path)
    try:
        for i in range(0, len(stream), 7):          # lines straddle feeds
            sink.feed(stream[i:i + 7])
        assert sink.finish() == len(blob)
        with open(path, "rb") as f:
            assert f.read() == blob
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_base64_sink_rejects_corrupted_payload():
    with tempfile.NamedTemporaryFile(suffix=".part", delete=False) as tf:
        path = tf.name
    sink = _Base64FileSink(path)
    try:
        with pytest.raises(Exception):
            sink.feed("QUJD\n")                     # a fine line
            sink.feed("ABCDE\n")                     # passes the charset regex,
            sink.finish()                           # but is not valid base64
    finally:
        os.unlink(path)


# -- negotiation ----------------------------------------------------------------

def _bare_shell(sock):
    sh = TelnetShell.__new__(TelnetShell)
    sh.sock = sock
    sh._carry = b""
    return sh


def test_negotiate_handles_iac_straddling_reads():
    a, b = socket.socketpair()
    sh = _bare_shell(a)
    # IAC DO TTYPE split as [IAC] then [DO TTYPE]; then data
    assert sh._negotiate(b"\xff") == b""
    assert sh._negotiate(b"\xfd\x18hello") == b"hello"
    # escaped 0xff in data
    assert sh._negotiate(b"\xff\xffend") == b"\xffend"
    a.close()
    b.close()


def test_negotiate_sb_frame_split_across_reads():
    a, b = socket.socketpair()
    sh = _bare_shell(a)
    frame = b"\xff\xfa\x24\x00\x01\x02\xff\xf0tail"
    assert sh._negotiate(frame[:5]) == b""
    assert sh._negotiate(frame[5:]) == b"tail"
    a.close()
    b.close()


# -- fake busybox telnet server ---------------------------------------------------

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
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

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
                conn.sendall(b"\r\nLast login: Sun Nov 22 21:00:00 UTC 2026 on pts/0\r\n")
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
                        self.blobs.get(idx, b"") if isinstance(self.blobs, dict)
                        else self.blobs)
                    # a prompt-noise line before the armor, then wrapped payload
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


def test_end_to_end_dump_byte_exact(tmp_path):
    blobs = {0: os.urandom(4), 1: os.urandom(16), 2: os.urandom(0x1000)}
    srv = FakeBusybox(blobs=blobs)
    try:
        sh = _shell(srv, password="ivideo")
        try:
            rc, mtd = sh.run("cat /proc/mtd")
            assert rc == 0 and "mtd0" in mtd
            parts = parse_proc_mtd(mtd)
            assert [p["idx"] for p in parts if not p["ubi"]] == [0, 1, 2]
            for p in parts:
                if p["ubi"]:
                    continue
                path = str(tmp_path / f"mtd{p['idx']}.bin")
                ok, n = dump_partition(sh, p, path)
                assert ok and n == p["size"] == len(blobs[p["idx"]])
                with open(path, "rb") as f:
                    assert f.read() == blobs[p["idx"]]
        finally:
            sh.close()
    finally:
        srv.sock.close()


def test_logout_failed():
    srv = FakeBusybox()
    try:
        with pytest.raises(ShellError):
            _shell(srv, password="wrong")
    finally:
        srv.sock.close()


def test_last_login_banner_is_not_a_login_failure():
    srv = FakeBusybox(last_login=True)
    try:
        sh = _shell(srv, password="ivideo")       # must not raise
        try:
            assert sh.run("false")[0] == 1        # rc propagates through the shell
        finally:
            sh.close()
    finally:
        srv.sock.close()


def test_dump_failure_never_truncates_a_previous_backup(tmp_path):
    good = tmp_path / "mtd1-defcfg.bin"
    good.write_bytes(b"PREVIOUS GOOD BACKUP" + b"\x00" * 4)
    srv = FakeBusybox(blobs={1: os.urandom(0x10 - 1)})   # short by one byte
    try:
        sh = _shell(srv, password="ivideo")
        try:
            rc, mtd = sh.run("cat /proc/mtd")
            part = [p for p in parse_proc_mtd(mtd) if p["idx"] == 1][0]
            with pytest.raises(ShellError):
                dump_partition(sh, part, str(good))
            assert good.read_bytes().startswith(b"PREVIOUS GOOD BACKUP")
            assert not list(tmp_path.glob("*.part"))
        finally:
            sh.close()
    finally:
        srv.sock.close()


def test_backup_default_skips_ubi_but_explicit_selects_it(tmp_path):
    blobs = {0: os.urandom(4), 1: os.urandom(16), 2: os.urandom(0x1000)}
    srv = FakeBusybox(blobs=blobs)
    try:
        res = backup("127.0.0.1", str(tmp_path), port=srv.port,
                    user="root", password="ivideo", verbose=False)
        assert [r["idx"] for r in res] == [0, 1, 2]     # 10-12 skipped
        assert not (tmp_path / "mtd12-config.bin").exists()
    finally:
        srv.sock.close()

    ubi = {12: os.urandom(0x20)}
    srv2 = FakeBusybox(blobs=ubi)
    try:
        out = str(tmp_path / "ubi")
        res = backup("127.0.0.1", out, port=srv2.port, user="root",
                     password="ivideo", parts=[12], verbose=False)
        assert len(res) == 1 and res[0]["idx"] == 12 and res[0]["ubi"]
        with open(f"{out}/mtd12-config.bin", "rb") as f:
            assert f.read() == ubi[12]
    finally:
        srv2.sock.close()
