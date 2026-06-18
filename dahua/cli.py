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

    p_ptz = sub.add_parser("ptz", help="move PTZ")
    p_ptz.add_argument("code", choices=const.PTZ_CODES)
    p_ptz.add_argument("--channel", type=int, default=0)
    p_ptz.add_argument("--speed", type=int, default=4)
    p_ptz.add_argument("--duration", type=float, default=0.5)

    p_snap = sub.add_parser("snapshot", help="grab a JPEG still")
    p_snap.add_argument("output", nargs="?", default="snapshot.jpg")
    p_snap.add_argument("--channel", type=int, default=0)

    p_ev = sub.add_parser("events", help="stream device events")
    p_ev.add_argument("--codes", nargs="*", default=["All"])

    p_call = sub.add_parser("call", help="raw RPC2 method")
    p_call.add_argument("method")
    p_call.add_argument("--params")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
            cam.ptz_move(args.code, channel=args.channel,
                         speed=args.speed, duration=args.duration)
            print(f"[+] PTZ {args.code} for {args.duration}s", file=sys.stderr)
        elif cmd == "snapshot":
            data = cam.snapshot(channel=args.channel)
            with open(args.output, "wb") as fh:
                fh.write(data)
            print(f"[+] wrote {len(data)} bytes to {args.output}", file=sys.stderr)
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
