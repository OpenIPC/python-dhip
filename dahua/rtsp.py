"""RTSP live-stream helpers.

Dahua devices serve live H.264/H.265 over RTSP on TCP 554. The stream path is
firmware-specific — the standard Dahua path is ``/cam/realmonitor`` but OEM
builds differ (the Zenointel test camera uses ``/H264?ch=N&subtype=M``), so the
path is a configurable template.

``record_rtsp`` and ``iter_rtsp`` shell out to ``ffmpeg`` (the only dependency,
and only for video capture); everything else in the library is pure stdlib.
"""

from __future__ import annotations

import shutil
import subprocess
from urllib.parse import quote

# Standard Dahua path (channel is 1-based here); override per-device.
DEFAULT_RTSP_TEMPLATE = "/cam/realmonitor?channel={channel}&subtype={subtype}"
# The Zenointel/Rostelecom OEM variant verified on the SD-2N-4G test camera.
ZN_RTSP_TEMPLATE = "/H264?ch={channel}&subtype={subtype}"


def build_rtsp_url(host: str, username: str = "", password: str = "",
                   channel: int = 1, subtype: int = 0, port: int = 554,
                   template: str = DEFAULT_RTSP_TEMPLATE) -> str:
    """Construct an RTSP URL. *channel*/*subtype* are substituted into *template*."""
    path = template.format(channel=channel, subtype=subtype)
    creds = ""
    if username:
        creds = f"{quote(username, safe='')}:{quote(password, safe='')}@"
    return f"rtsp://{creds}{host}:{port}{path}"


def _require_ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if exe is None:
        raise RuntimeError(
            "ffmpeg not found on PATH; required for RTSP capture")
    return exe


def record_rtsp(url: str, out_path: str, duration: float = 10.0,
                transport: str = "tcp", reencode: bool = False,
                timeout: float | None = None) -> str:
    """Record *url* to *out_path* for *duration* seconds via ffmpeg.

    Stream-copies by default (no re-encode). Returns *out_path*.
    """
    exe = _require_ffmpeg()
    codec = ["-c", "copy"] if not reencode else []
    cmd = [exe, "-hide_banner", "-loglevel", "error",
           "-rtsp_transport", transport, "-i", url,
           "-t", str(duration), *codec, "-y", out_path]
    subprocess.run(cmd, check=True, timeout=timeout or (duration + 30))
    return out_path


def iter_rtsp(url: str, transport: str = "tcp", chunk_size: int = 65536):
    """Yield raw MPEG-TS chunks of the live stream (via ffmpeg stdout).

    Useful for piping the stream elsewhere without writing a file. Caller must
    consume the generator and close it to terminate ffmpeg::

        for chunk in cam.iter_rtsp():
            sink.write(chunk)
    """
    exe = _require_ffmpeg()
    cmd = [exe, "-hide_banner", "-loglevel", "error",
           "-rtsp_transport", transport, "-i", url,
           "-c", "copy", "-f", "mpegts", "pipe:1"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    try:
        while True:
            chunk = proc.stdout.read(chunk_size)
            if not chunk:
                break
            yield chunk
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
