"""Command-line interface for python-dhip.

Backward compatible with the original one-shot form::

    dhip HOST -u admin -P secret -m magicBox.getSystemInfo
    dhip HOST -m configManager.getConfig --params '{"name":"General"}'

plus high-level subcommands::

    dhip HOST info
    dhip HOST config General
    dhip HOST users
    dhip HOST ptz Left --duration 0.5
    dhip HOST snapshot out.jpg
    dhip HOST events --codes VideoMotion
"""

from __future__ import annotations

import argparse
import json
import sys

from . import const
from .client import DahuaClient


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="dhip", description="Dahua DHIP RPC2 client")
    ap.add_argument("host")
    ap.add_argument("-p", "--port", type=int, default=const.DEFAULT_PORT)
    ap.add_argument("-u", "--user", default="admin")
    ap.add_argument("-P", "--password", default="")
    ap.add_argument("-m", "--method", help="raw RPC2 method to call (one-shot)")
    ap.add_argument("--params", help="JSON object for raw --method params")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")

    sub = ap.add_subparsers(dest="command")
    sub.add_parser("info", help="dump device/system info")

    p_cfg = sub.add_parser("config", help="get/set a config section")
    p_cfg.add_argument("name")
    p_cfg.add_argument("--set", metavar="JSON", help="JSON table to write back")

    sub.add_parser("users", help="list users and groups")

    p_ptz = sub.add_parser("ptz", help="control PTZ")
    p_ptz.add_argument(
        "action",
        help="a direction code (Up/Down/Left/Right/ZoomTele/...), or one of: "
             "status, position, home, reset, goto-preset, set-preset, "
             "clear-preset, absolute, zoom, focus, iris")
    p_ptz.add_argument("value", nargs="?",
                       help="preset index, or zoom/focus/iris direction")
    p_ptz.add_argument("--channel", type=int, default=0)
    p_ptz.add_argument("--speed", type=int, default=4)
    p_ptz.add_argument("--duration", type=float, default=0.5)
    p_ptz.add_argument("--pan", type=float, default=0.0, help="degrees (0-360)")
    p_ptz.add_argument("--tilt", type=float, default=0.0, help="degrees (0-90)")
    p_ptz.add_argument("--zoom", type=float, default=0.0)
    p_ptz.add_argument("--degrees", action="store_true",
                       help="for 'position': report in degrees")

    p_snap = sub.add_parser("snapshot", help="grab a JPEG still")
    p_snap.add_argument("output", nargs="?", default="snapshot.jpg")
    p_snap.add_argument("--channel", type=int, default=0)

    p_ev = sub.add_parser("events", help="stream device events")
    p_ev.add_argument("--codes", nargs="*", default=["All"])

    p_st = sub.add_parser("stream", help="record live video via HTTP RPC_Loadfile")
    p_st.add_argument("output", nargs="?", default="stream.dhav")
    p_st.add_argument("--channel", type=int, default=0)
    p_st.add_argument("--subtype", type=int, default=0, help="0=main, 1=sub")
    p_st.add_argument("--duration", type=float, default=10.0)
    p_st.add_argument("--http-port", type=int, default=80)

    p_rt = sub.add_parser("rtsp", help="record live video over RTSP (ffmpeg)")
    p_rt.add_argument("output", nargs="?", default="rtsp.mp4")
    p_rt.add_argument("--channel", type=int, default=1)
    p_rt.add_argument("--subtype", type=int, default=0, help="0=main, 1=sub")
    p_rt.add_argument("--duration", type=float, default=10.0)
    p_rt.add_argument("--template", help="RTSP path template (e.g. ZN OEM)")

    p_ti = sub.add_parser("title", help="get/set channel title overlay")
    p_ti.add_argument("text", nargs="?", help="new title; omit to read current")
    p_ti.add_argument("--channel", type=int, default=0)

    p_fl = sub.add_parser("files", help="list recordings (mediaFileFind)")
    p_fl.add_argument("start", help='"YYYY-MM-DD HH:MM:SS"')
    p_fl.add_argument("end", help='"YYYY-MM-DD HH:MM:SS"')
    p_fl.add_argument("--channel", type=int, default=0)

    p_dc = sub.add_parser("discover", help="find Dahua devices on the LAN (multicast)")
    p_dc.add_argument("--timeout", type=float, default=2.0)

    p_call = sub.add_parser("call", help="raw RPC2 method")
    p_call.add_argument("method")
    p_call.add_argument("--params")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "discover":
        from .discovery import discover
        _print(discover(timeout=args.timeout))
        return 0

    cam = DahuaClient(args.host, args.port)
    if args.verbose:
        cam.debug()

    with cam:
        cam.login(args.user, args.password)
        print(f"[+] logged in (session=0x{cam.session:08x})", file=sys.stderr)

        cmd = args.command
        if cmd is None and args.method:
            cmd, raw_method, raw_params = "call", args.method, args.params
        elif cmd == "call":
            raw_method, raw_params = args.method, args.params
        else:
            raw_method = raw_params = None

        if cmd == "call":
            params = json.loads(raw_params) if raw_params else None
            resp, data = cam.request(raw_method, params)
            _print(resp)
            if data:
                print(f"[+] +{len(data)} bytes binary payload", file=sys.stderr)
        elif cmd == "info":
            _print({
                "systemInfo": cam.get_system_info(),
                "softwareVersion": cam.get_software_version(),
                "vendor": cam.get_vendor(),
                "serial": cam.get_serial_number(),
                "memory": cam.get_memory_info(),
                "time": str(cam.get_time()),
            })
        elif cmd == "config":
            if args.set:
                _print(cam.set_config(args.name, json.loads(args.set)))
            else:
                _print(cam.get_config(args.name))
        elif cmd == "users":
            _print({"users": cam.get_users(), "groups": cam.get_groups()})
        elif cmd == "ptz":
            act, ch = args.action, args.channel
            if act == "status":
                _print(cam.ptz_status(ch))
            elif act == "position":
                _print(cam.ptz_position_degrees(ch) if args.degrees
                       else cam.ptz_position(ch))
            elif act == "home":
                print("[+]", cam.ptz_goto_home(ch), file=sys.stderr)
            elif act == "reset":
                print("[+]", cam.ptz_reset(ch), file=sys.stderr)
            elif act in ("goto-preset", "set-preset", "clear-preset"):
                fn = {"goto-preset": cam.ptz_goto_preset,
                      "set-preset": cam.ptz_set_preset,
                      "clear-preset": cam.ptz_clear_preset}[act]
                print("[+]", fn(int(args.value), ch), file=sys.stderr)
            elif act == "absolute":
                print("[+]", cam.ptz_move_absolutely(args.pan, args.tilt,
                                                     args.zoom, ch), file=sys.stderr)
            elif act == "zoom":
                cam.ptz_zoom(args.value or "in", ch, args.speed, args.duration)
            elif act == "focus":
                cam.ptz_focus(args.value or "near", ch, args.speed, args.duration)
            elif act == "iris":
                cam.ptz_iris(args.value or "open", ch, args.speed, args.duration)
            else:  # a raw direction/code
                cam.ptz_move(act, channel=ch, speed=args.speed, duration=args.duration)
                print(f"[+] PTZ {act} for {args.duration}s", file=sys.stderr)
        elif cmd == "snapshot":
            data = cam.snapshot(channel=args.channel)
            with open(args.output, "wb") as fh:
                fh.write(data)
            print(f"[+] wrote {len(data)} bytes to {args.output}", file=sys.stderr)
        elif cmd == "stream":
            from .media import HttpMediaClient
            with HttpMediaClient(args.host, args.http_port) as media:
                media.login(args.user, args.password)
                n = media.record(args.output, channel=args.channel,
                                 subtype=args.subtype, duration=args.duration)
            print(f"[+] wrote {n} bytes to {args.output}", file=sys.stderr)
        elif cmd == "rtsp":
            out = cam.record_rtsp(args.output, channel=args.channel,
                                  subtype=args.subtype, duration=args.duration,
                                  template=args.template)
            print(f"[+] recorded {args.duration}s to {out}", file=sys.stderr)
        elif cmd == "title":
            if args.text is None:
                _print(cam.get_channel_titles())
            else:
                cam.set_channel_title(args.text, args.channel)
                print(f"[+] set channel {args.channel} title -> {args.text!r}",
                      file=sys.stderr)
        elif cmd == "files":
            _print(cam.find_files(args.start, args.end, channel=args.channel))
        elif cmd == "events":
            listener = cam.events(codes=args.codes)
            print(f"[+] listening for events {args.codes} (Ctrl-C to stop)",
                  file=sys.stderr)
            try:
                listener.listen(_print, args.user, args.password)
            except KeyboardInterrupt:
                listener.stop()
        else:
            print("nothing to do; pass -m METHOD or a subcommand "
                  "(info/config/users/ptz/snapshot/events)", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
