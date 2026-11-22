"""NAND backup over the vendor telnet shell -- no UART, no TFTP, no NFS.

Dumps every raw MTD partition of a vendor camera that offers a busybox shell
on telnet (enabled first via ``DahuaClient.set_telnet``). Works with the
minimal applet set found on such firmwares: only ``dd`` and ``base64`` are
used, the stream is ASCII-armored (immune to telnet/pty mangling), decoded
strictly on the host, and every partition is verified against its declared
size from ``/proc/mtd`` before it replaces anything on disk.

Designed for Zenointel/XMedia (GK7205) cameras whose stock busybox lacks
gzip/md5sum/tftp, but works on any Linux box exposing /dev/mtd* over telnet.
"""
from __future__ import annotations

import base64
import os
import re
import socket
import sys
import time

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240


class ShellError(RuntimeError):
    pass


class TelnetShell:
    """Minimal raw telnet client (stdlib sockets; telnetlib is gone in 3.13).

    Keeps telnet parser state across socket reads, so a negotiation sequence
    split between two ``recv`` calls can never leak into command output.
    """

    def __init__(self, host: str, port: int = 23, user: str = "root",
                 password: str = "", login_timeout: int = 15):
        self.sock = socket.create_connection((host, port), timeout=10)
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        self._carry = b""
        try:
            self._read_until([b"login:", b"ogin:"], login_timeout)
            self._send(user + "\n")
            # a failed login usually bounces back to a fresh "login:" prompt
            # while we are still waiting for the password prompt, so keep
            # everything since the username went out and check it all.
            mid = self._read_until([b"assword:", b"login:"], login_timeout)
            self._send(password + "\n")
            out = mid + self._read_until([b"# ", b"$ ", b"~#", b"/ #"],
                                         login_timeout)
            # A repeated prompt is a line consisting of (l)ogin: alone; a
            # success banner such as "Last login: Sun Nov 22 ..." has text
            # after the colon and must NOT be taken for a login failure.
            text = out.decode(errors="replace")
            if "incorrect" in text.lower() or re.search(r"(?im)^\s*o?login:?\s*$", text):
                raise ShellError("telnet login failed: " + text.strip()[-200:])
        except ShellError:
            self.close()
            raise
        # unique marker prefix so echoed command lines never match real output
        self.marker = "RZ7Q%d" % (int(time.time()) % 100000)

    # -- telnet wire ----------------------------------------------------------------
    def _negotiate(self, data: bytes) -> bytes:
        """Strip telnet control sequences; keep partial ones for the next read."""
        data = self._carry + data
        self._carry = b""
        out = bytearray()
        i, n = 0, len(data)
        while i < n:
            b = data[i]
            if b != IAC:
                out.append(b)
                i += 1
                continue
            if i + 1 >= n:                       # IAC alone: wait for more
                self._carry = data[i:]
                break
            cmd = data[i + 1]
            if cmd == IAC:                       # escaped 0xff
                out.append(IAC)
                i += 2
            elif cmd in (DO, DONT, WILL, WONT):  # 3-byte option nego
                if i + 2 >= n:
                    self._carry = data[i:]
                    break
                opt = data[i + 2]
                resp = {DO: WONT, DONT: WONT, WILL: DONT, WONT: DONT}.get(cmd)
                if resp is not None:
                    self.sock.sendall(bytes([IAC, resp, opt]))
                i += 3
            elif cmd == SB:                      # subnegotiation: skip to IAC SE
                end = data.find(bytes([IAC, SE]), i + 2)
                if end == -1:
                    self._carry = data[i:]
                    break
                i = end + 2
            else:                                # unsupported 2-byte command
                i += 2
        return bytes(out)

    def _read_until(self, needles: list[bytes], timeout: int) -> bytes:
        buf = bytearray()
        end = time.time() + timeout
        self.sock.settimeout(0.5)
        while time.time() < end:
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                if any(nd in buf for nd in needles):
                    break
                continue
            if not chunk:
                break
            buf += self._negotiate(chunk)
            if any(nd in buf for nd in needles):
                break
        return bytes(buf)

    def _send(self, text: str) -> None:
        self.sock.sendall(text.encode())

    # -- command protocol -------------------------------------------------------------
    def run(self, cmd: str, timeout: int = 60) -> tuple[int, str]:
        """Run one command, return (rc, output). `timeout` is an IDLE timeout:
        the clock resets on every received chunk (streaming-safe)."""
        chunks: list[str] = []
        rc = self.run_stream(cmd, _ListSink(chunks), timeout=timeout)
        return rc, "".join(chunks)

    def run_stream(self, cmd: str, sink, timeout: int = 60) -> int:
        """Run one command, feeding payload text to `sink.feed()` as it
        arrives (memory bounded by the consumer, not the command output).
        Returns the command's exit code, or -1 if it could not be read."""
        m = self.marker
        self._send(f'__m={m}; echo $__m"CMD"; {cmd}; echo $__m"END$?"\n')
        split = _MarkerSplit(m)
        idle = time.time() + timeout
        self.sock.settimeout(0.5)
        while time.time() < idle:
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                if split.finished:
                    break
                continue
            if not chunk:
                break
            payload = split.feed(self._negotiate(chunk).decode(errors="replace"))
            if payload:
                sink.feed(payload)
            if split.finished:
                # rc digits may straddle packets: drain briefly for them
                rc_deadline = time.time() + 2
                while time.time() < rc_deadline and not split.rc_digits():
                    try:
                        split.feed(self._negotiate(self.sock.recv(4096))
                                   .decode(errors="replace"))
                    except socket.timeout:
                        pass
                break
            idle = time.time() + timeout     # got data: reset the idle clock
        return split.rc_digits()[1] if split.rc_digits() else -1

    def close(self) -> None:
        try:
            self._send("exit\n")
            self.sock.close()
        except OSError:
            pass


class _ListSink:
    def __init__(self, chunks: list[str]):
        self.chunks = chunks

    def feed(self, text: str) -> None:
        self.chunks.append(text)


class _MarkerSplit:
    """Split a marker-framed stream into (payload, rc).

    States: wait for the expanded ``<marker>CMD`` output line, then emit
    everything as payload until ``<marker>END`` appears; hold back the tail of
    every chunk so a marker cut across socket reads cannot slip into the
    payload. After END, subsequent text (the ``$?`` digits) accumulates into
    ``rc_text``.
    """

    def __init__(self, marker: str):
        self.cmd_s, self.end_s = marker + "CMD", marker + "END"
        self._carry = ""
        self.state = 0          # 0 = waiting for CMD, 1 = payload, 2 = after END
        self.rc_text = ""
        self.finished = False

    def feed(self, text: str) -> str:
        if self.state == 0:
            text = self._carry + text
            i = text.find(self.cmd_s)
            if i == -1:
                # everything before the CMD marker is the echoed command line;
                # keep only the tail that could still start the marker
                hold = len(self.cmd_s) - 1
                self._carry = text[-hold:] if len(text) > hold else text
                return ""
            text = text[i + len(self.cmd_s):]
            self._carry = ""
            self.state = 1
        if self.state == 1:
            text = self._carry + text
            self._carry = ""
            i = text.find(self.end_s)
            if i == -1:
                hold = len(self.end_s) - 1
                if len(text) <= hold:
                    self._carry = text
                    return ""
                self._carry, emit = text[-hold:], text[:-hold]
                return emit
            self.rc_text = text[i + len(self.end_s):]
            self.state = 2
            self.finished = True
            return text[:i]
        self.rc_text += text     # state 2: draining the exit-code digits
        return ""

    def rc_digits(self) -> tuple[int, int] | None:
        m = re.match(r"\s*\D{0,3}(\d{1,3})", self.rc_text)
        return (0, int(m.group(1))) if m else None


class _Base64FileSink:
    """Incremental strict decoder for `dd | base64` output.

    Writes decoded bytes straight to a file, one base64 line at a time
    (memory bounded by the line length, not the partition size). Lines that
    are not pure base64 -- the echoed command, shell prompts -- are dropped;
    a genuinely corrupt line fails the strict decode and aborts the dump
    loudly rather than silently producing a short file.
    """

    LINE = re.compile(r"[A-Za-z0-9+/]+=*")

    def __init__(self, path: str):
        self.fh = open(path, "wb")
        self.partial = ""
        self.n = 0

    def feed(self, text: str) -> None:
        self.partial += text.replace("\r", "")
        while "\n" in self.partial:
            line, self.partial = self.partial.split("\n", 1)
            self._decode(line.strip())

    def _decode(self, line: str) -> None:
        if not line or not self.LINE.fullmatch(line):
            return                     # echo line / prompt noise, not payload
        data = base64.b64decode(line, validate=True)
        self.fh.write(data)
        self.n += len(data)

    def finish(self) -> int:
        """Flush the final (unterminated) line, close; return bytes written."""
        try:
            self._decode(self.partial.strip())
        finally:
            self.fh.close()
        return self.n

    def abort(self) -> None:
        self.fh.close()


MTD_RE = re.compile(r"^mtd(\d+):\s+([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\s+\"([^\"]*)\"")


def parse_proc_mtd(text: str) -> list[dict]:
    """Parse every /proc/mtd entry in table order.

    Entries whose erasesize is smaller than the raw-device erasesize are UBI
    volume mappings (e.g. 0x1f000 next to the raw 0x20000 on GK7205): they
    alias data already covered by the raw partitions and are tagged
    ``ubi=True`` so the automatic dump skips them. Explicit ``parts`` indices
    still select them, and a raw partition with an unusual erase size is kept
    (tagged, not dropped) -- only the automatic heuristic skips UBI volumes.
    """
    parts, raw_erase = [], None
    for line in text.splitlines():
        m = MTD_RE.match(line)
        if not m:
            continue
        idx, size, erase = int(m.group(1)), int(m.group(2), 16), int(m.group(3), 16)
        if raw_erase is None:
            raw_erase = erase
        parts.append({"idx": idx, "size": size, "erase": erase,
                      "name": m.group(4), "ubi": erase != raw_erase})
    return parts


def dump_partition(shell: TelnetShell, part: dict, out_path: str,
                   idle_timeout: int = 180) -> tuple[bool, int]:
    """dd | base64 one /dev/mtdNro into out_path. Returns (ok, bytes_written).

    Streams into ``out_path + ".part"`` and atomically replaces ``out_path``
    only after the command completed and the byte count matches the partition
    size, so a failed rerun can never truncate a previously complete backup.
    """
    dev = f"/dev/mtd{part['idx']}ro"
    tmp_path = out_path + ".part"
    sink = _Base64FileSink(tmp_path)
    rc = shell.run_stream(f"dd if={dev} 2>/dev/null | base64", sink,
                          timeout=idle_timeout)
    try:
        n = sink.finish()
    except Exception:
        sink.abort()
        os.unlink(tmp_path)
        raise
    if rc != 0:
        os.unlink(tmp_path)
        raise ShellError(f"dd|base64 on {dev} failed (rc={rc})")
    if n != part["size"]:
        os.unlink(tmp_path)
        raise ShellError(f"{dev}: expected {part['size']} bytes, got {n}")
    os.replace(tmp_path, out_path)
    return True, n


def backup(host: str, outdir: str, port: int = 23, user: str = "root",
           password: str = "", parts: list[int] | None = None,
           verbose: bool = True) -> list[dict]:
    """Dump raw MTD partitions over telnet into ``outdir``.

    ``parts=None`` dumps every raw partition in table order (skipping UBI
    volume aliases); explicit indices select any partition, including
    UBI-mapped ones. Raises ``ShellError`` on any failed or size-mismatched
    partition: a partial result file never replaces a good one.
    """
    shell = TelnetShell(host, port, user, password)
    try:
        rc, mtd_text = shell.run("cat /proc/mtd")
        if rc != 0 or "mtd" not in mtd_text:
            raise ShellError("cannot read /proc/mtd over telnet")
        avail = parse_proc_mtd(mtd_text)
        if not avail:
            raise ShellError("no MTD partitions found in /proc/mtd")
        todo = ([p for p in avail if p["idx"] in parts] if parts is not None
                else [p for p in avail if not p["ubi"]])
        if not todo:
            raise ShellError("no partition matched -- indices: " +
                             ",".join(str(p["idx"]) for p in avail))
        os.makedirs(outdir, exist_ok=True)
        result = []
        for p in todo:
            path = f"{outdir}/mtd{p['idx']}-{p['name']}.bin"
            t0 = time.time()
            _, n = dump_partition(shell, p, path)
            dt = time.time() - t0
            result.append({**p, "file": path, "ok": True, "bytes": n})
            if verbose:
                print(f"[OK ] {path}  {n}/{p['size']} bytes  "
                      f"{n / max(dt, 0.001) / 1024:.0f} KiB/s", file=sys.stderr)
        return result
    finally:
        shell.close()
