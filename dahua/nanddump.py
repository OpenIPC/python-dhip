"""NAND backup over the vendor telnet shell -- no UART, no TFTP, no NFS.

Dumps every raw MTD partition of a vendor camera that offers a busybox shell
on telnet (enabled first via ``DahuaClient.set_telnet``). Works with the
minimal applet set found on such firmwares: only ``dd`` and ``base64`` are
used, the telnet stream is ASCII-armored (immune to telnet/pty mangling),
decoded strictly on the host, and every partition is verified against its
declared size from ``/proc/mtd``.

Designed for Zenointel/XMedia (GK7205) cameras whose stock busybox lacks
gzip/md5sum/tftp, but works on any Linux box exposing /dev/mtd* over telnet.
"""
from __future__ import annotations

import base64
import re
import socket
import sys
import time

IAC, DONT, DO, WONT, WILL = 255, 254, 253, 252, 251


class ShellError(RuntimeError):
    pass


class TelnetShell:
    """Minimal raw telnet client (stdlib sockets; telnetlib is gone in 3.13)."""

    def __init__(self, host: str, port: int = 23, user: str = "root",
                 password: str = "", login_timeout: int = 15):
        self.sock = socket.create_connection((host, port), timeout=10)
        self._read_until([b"login:", b"ogin:"], login_timeout)
        self._send(user + "\n")
        out = self._read_until([b"assword:"], login_timeout)
        self._send(password + "\n")
        out += self._read_until([b"# ", b"$ ", b"~#", b"/ #"], login_timeout)
        if b"incorrect" in out or b"ogin:" in out:
            raise ShellError("telnet login failed: " + out.decode(errors="replace"))
        # unique marker prefix so echoed command lines never match real output
        self.marker = "RZ7Q%d" % (int(time.time()) % 100000)

    def _negotiate(self, data: bytes) -> bytes:
        out = bytearray()
        i = 0
        while i < len(data):
            b = data[i]
            if b == IAC and i + 2 < len(data):
                cmd, opt = data[i + 1], data[i + 2]
                resp = {DO: WONT, DONT: WONT, WILL: DONT, WONT: DONT}.get(cmd)
                if resp is not None:
                    self.sock.sendall(bytes([IAC, resp, opt]))
                i += 3
            else:
                out.append(b)
                i += 1
        return bytes(out)

    def _read_until(self, needles: list[bytes], timeout: int) -> bytes:
        buf = bytearray()
        end = time.time() + timeout
        self.sock.settimeout(0.5)
        while time.time() < end:
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                if any(n in buf for n in needles):
                    break
                continue
            if not chunk:
                break
            buf += self._negotiate(chunk)
            if any(n in buf for n in needles):
                break
        return bytes(buf)

    def _send(self, text: str) -> None:
        self.sock.sendall(text.encode())

    def run(self, cmd: str, timeout: int = 60) -> tuple[int, str]:
        """Run one command, return (rc, output). `timeout` is an IDLE timeout:
        the clock resets on every received chunk (streaming-safe)."""
        m = self.marker
        self._send(f'__m={m}; echo $__m"CMD"; {cmd}; echo $__m"END$?"\n')
        buf = bytearray()
        end_c = f"{m}END".encode()
        cmd_c = f"{m}CMD".encode()
        idle = time.time() + timeout
        self.sock.settimeout(0.5)
        while time.time() < idle:
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                if end_c in buf:
                    break
                continue
            if not chunk:
                break
            buf += self._negotiate(chunk)
            if end_c in buf:
                # the rc digits may straddle a packet boundary -- wait briefly
                # for at least one digit to land after the end marker.
                tail_start = buf.find(end_c) + len(end_c)
                deadline = time.time() + 2
                while time.time() < deadline:
                    if re.search(rb"\d", bytes(buf[tail_start:])):
                        break
                    try:
                        buf += self._negotiate(self.sock.recv(4096))
                    except socket.timeout:
                        continue
                break
            idle = time.time() + timeout     # got data: reset the idle clock
        text = buf.decode(errors="replace")
        end_s, cmd_s = f"{m}END", f"{m}CMD"
        if end_s not in text:
            raise ShellError("timeout while running command: " + cmd[:60])
        head, tail = text.split(end_s, 1)
        rc = -1
        digits = re.match(r"\D*(\d+)", tail.replace("\r", ""))
        if digits:
            rc = int(digits.group(1))
        body = head.split(cmd_s, 1)[1] if cmd_s in head else head
        # drop the echoed command line (marker makes it recognizable)
        first_nl = body.find("\n")
        if first_nl != -1 and m in body[:first_nl + 1]:
            body = body[first_nl + 1:]
        return rc, body

    def close(self) -> None:
        try:
            self._send("exit\n")
            self.sock.close()
        except OSError:
            pass


MTD_RE = re.compile(r"^mtd(\d+):\s+([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\s+\"([^\"]*)\"")


def parse_proc_mtd(text: str) -> list[dict]:
    """Return the raw (non-UBI-volume) partitions in table order."""
    parts, raw_erase = [], None
    for line in text.splitlines():
        m = MTD_RE.match(line)
        if not m:
            continue
        idx, size, erase, name = int(m.group(1)), int(m.group(2), 16), int(m.group(3), 16), m.group(4)
        if raw_erase is None:
            raw_erase = erase
        if erase != raw_erase:      # UBI volume devices (erase 0x1f000 etc.) -- skip
            continue
        parts.append({"idx": idx, "size": size, "erase": erase, "name": name})
    return parts


def dump_partition(shell: TelnetShell, part: dict, out_path: str,
                   idle_timeout: int = 180) -> tuple[bool, int]:
    """dd | base64 one /dev/mtdNro into out_path. Returns (ok, bytes_written)."""
    dev = f"/dev/mtd{part['idx']}ro"
    cmd = f"dd if={dev} 2>/dev/null | base64"
    rc, body = shell.run(cmd, timeout=idle_timeout)
    if rc != 0:
        raise ShellError(f"dd|base64 on {dev} returned {rc}")
    # strip CRs and any echo/prompt noise, keep only the base64 payload lines
    payload = []
    for line in body.replace("\r", "").splitlines():
        line = line.strip()
        if not line:
            continue
        if not re.fullmatch(r"[A-Za-z0-9+/]+=*", line):
            continue  # echo line / prompt -- payload lines are pure base64
        payload.append(line)
    data = base64.b64decode("".join(payload), validate=True)
    ok = len(data) == part["size"]
    with open(out_path, "wb") as f:
        f.write(data)
    return ok, len(data)


def backup(host: str, outdir: str, port: int = 23, user: str = "root",
           password: str = "", parts: list[int] | None = None,
           verbose: bool = True) -> list[dict]:
    """Dump raw MTD partitions over telnet. `parts` selects partition indices;
    None = every raw partition (the contiguous vendor map, concat == whole
    data-only NAND image)."""
    shell = TelnetShell(host, port, user, password)
    try:
        rc, mtd_text = shell.run("cat /proc/mtd")
        if rc != 0 or "mtd" not in mtd_text:
            raise ShellError("cannot read /proc/mtd over telnet")
        avail = parse_proc_mtd(mtd_text)
        if not avail:
            raise ShellError("no raw MTD partitions found in /proc/mtd")
        todo = [p for p in avail if parts is None or p["idx"] in parts]
        if not todo:
            raise ShellError("no partition matched -- indices: " +
                             ",".join(str(p["idx"]) for p in avail))
        import os
        os.makedirs(outdir, exist_ok=True)
        result = []
        for p in todo:
            name = f"mtd{p['idx']}-{p['name']}.bin"
            path = f"{outdir}/{name}"
            t0 = time.time()
            ok, n = dump_partition(shell, p, path)
            dt = time.time() - t0
            result.append({**p, "file": path, "ok": ok, "bytes": n})
            if verbose:
                state = "OK " if ok else "SIZE MISMATCH"
                print(f"[{state}] {path}  {n}/{p['size']} bytes  "
                      f"{n / max(dt, 0.001) / 1024:.0f} KiB/s", file=sys.stderr)
        return result
    finally:
        shell.close()
