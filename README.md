# python-dhip

A pure-stdlib Python library for the **Dahua DHIP** (binary RPC2) protocol on
TCP **5000**, as spoken by Dahua and Dahua-derived OEM IP cameras (e.g.
Zenointel).

It is the Dahua counterpart to [`python-dvr`](https://github.com/OpenIPC/python-dvr)
(XiongMai/Sofia cameras).

> This is **not** the XiongMai/Sofia "NetSurveillance" protocol (TCP 34567,
> `0xFF` header, `sofia_hash`). Different port, framing, auth hash and command
> model. Note also that legacy Dahua devices expose a *different* framing on TCP
> 37777 — DHIP here is **port 5000**.

No third-party dependencies. Sync and async clients, a CLI, and an event
listener are all included.

## Install

```bash
pip install git+https://github.com/OpenIPC/python-dhip      # latest
# or, from a checkout:
git clone https://github.com/OpenIPC/python-dhip && cd python-dhip && pip install -e .

dhip --help               # console entry point
```

Pure stdlib — no third-party dependencies. `ffmpeg` is only needed for RTSP video
capture (`record_rtsp` / `iter_rtsp`).

## Quick start

```python
from dahua import DahuaClient

with DahuaClient("10.0.0.10") as cam:
    cam.login("admin", "admin54321")        # starts a keep-alive timer
    print(cam.get_system_info())            # {'deviceType': 'SD-2N-4G', ...}
    print(cam.get_time())                   # datetime(...)
    cam.ptz_move("Left", duration=0.5)      # nudge left, then stop
    with open("snap.jpg", "wb") as f:
        f.write(cam.snapshot())             # JPEG via HTTP CGI
```

Async:

```python
import asyncio
from dahua import AsyncDahuaClient

async def main():
    async with AsyncDahuaClient("10.0.0.10") as cam:
        await cam.login("admin", "admin54321")
        print(await cam.get_device_type())
        async for event in cam.iter_events(["VideoMotion"]):
            print(event)

asyncio.run(main())
```

## Live video capture

The most reliable live-video path is **RTSP** (TCP 554). The stream path is
firmware-specific, so it's a configurable template:

```python
from dahua import DahuaClient, rtsp

with DahuaClient("10.0.0.10") as cam:
    cam.login("admin", "admin54321")
    # standard Dahua path is the default; OEM builds differ:
    cam.rtsp_template = rtsp.ZN_RTSP_TEMPLATE          # "/H264?ch={channel}&subtype={subtype}"
    print(cam.rtsp_url(channel=1, subtype=0))
    cam.record_rtsp("clip.mp4", channel=1, subtype=0, duration=10)   # needs ffmpeg
    for chunk in cam.iter_rtsp(channel=1):             # or pipe raw MPEG-TS
        ...
```

```bash
dhip 10.0.0.10 -u admin -P admin54321 rtsp clip.mp4 --subtype 1 --duration 10 \
    --template '/H264?ch={channel}&subtype={subtype}'
```

Alternatively, some Dahua cameras serve live video over the **HTTP RPC2 +
`RPC_Loadfile`** channel. `HttpMediaClient`
implements the latter: login → `streamReader.create {channel, subtype}` →
`streamReader.start` → stream `GET /RPC_Loadfile/<object>` (the raw **DHAV**
container) to a file → `streamReader.stop`/`destroy`.

```python
from dahua import HttpMediaClient

with HttpMediaClient("10.0.0.10") as cam:
    cam.login("admin", "admin54321")
    n = cam.record("clip.dhav", channel=0, subtype=0, duration=10.0)
    print(f"wrote {n} bytes")   # play/convert with: ffmpeg -i clip.dhav out.mp4
```

```bash
dhip 10.0.0.10 -u admin -P admin54321 stream clip.dhav --subtype 1 --duration 10
```

Not every firmware exposes the HTTP RPC2 media endpoint; prefer RTSP where it is
available. See [Device support & status](#device-support--status).

## CLI

```bash
# high-level subcommands
dhip 10.0.0.10 -u admin -P admin54321 info
dhip 10.0.0.10 -u admin -P admin54321 config General
dhip 10.0.0.10 -u admin -P admin54321 config Encode --set '{...}'
dhip 10.0.0.10 -u admin -P admin54321 users
dhip 10.0.0.10 -u admin -P admin54321 ptz Left --duration 0.5
dhip 10.0.0.10 -u admin -P admin54321 snapshot out.jpg
dhip 10.0.0.10 -u admin -P admin54321 events --codes VideoMotion

# raw one-shot RPC (backward compatible with the original tool)
dhip 10.0.0.10 -u admin -P admin54321 -m magicBox.getSystemInfo
dhip 10.0.0.10 -u admin -P admin54321 -m configManager.getConfig --params '{"name":"General"}'
```

## API ↔ RPC2 mapping

| Method | RPC2 call | Notes |
|---|---|---|
| `get_system_info()` | `magicBox.getSystemInfo` | |
| `get_device_type()` | `magicBox.getDeviceType` | |
| `get_software_version()` | `magicBox.getSoftwareVersion` | |
| `get_hardware_version()` | `magicBox.getHardwareVersion` | |
| `get_serial_number()` | `magicBox.getSerialNo` | |
| `get_memory_info()` | `magicBox.getMemoryInfo` | |
| `get_vendor()` | `magicBox.getVendor` | |
| `get_config(name)` / `set_config(name, table)` | `configManager.getConfig` / `setConfig` | returns/sends `params.table` |
| `get_users()` / `get_groups()` | `userManager.getUserInfoAll` / `getGroupInfoAll` | groups come back as a bare list |
| `add_user(...)` / `modify_user(...)` / `delete_user(name)` | `userManager.addUser` / `modifyUser` / `deleteUser` | |
| `ptz_status(ch)` / `ptz_position(ch)` | `ptz.getStatus` | live raw `[pan, tilt(, zoom)]` (encoder units) |
| `ptz_position_degrees(ch)` | `ptz.getStatus` | raw readout converted to `(pan°, tilt°)` |
| `ptz_caps(ch)` | `ptz.getCurrentProtocolCaps` | pan/tilt speed ranges |
| `ptz_is_moving(ch)` | `ptz.isMoving` | bool in `result` |
| `ptz_start/stop/move(code, ...)` | `ptz.start` / `ptz.stop` | code API; codes in `dahua.const.PTZ_CODES` |
| `ptz_up/down/left/right(...)` | `ptz.start`+`stop` | timed directional nudge |
| `ptz_zoom/focus/iris(direction, ...)` | `ptz.start`+`stop` | `in`/`out`, `near`/`far`, `open`/`close` |
| `ptz_move_absolutely(pan, tilt, zoom)` / `ptz_goto(...)` | `ptz.moveAbsolutely` | absolute slew in **degrees** (pan 0–360, tilt 0–90) |
| `ptz_move_relatively/continuously` + `ptz_stop_move` | `ptz.move*` | modern API (not on every firmware) |
| `ptz_get_presets` / `ptz_set/goto/clear_preset(i)` | `ptz.getPresets`, `ptz.start` (`Set/Goto/ClearPreset`) | presets via the code API |
| `ptz_get_tours` / `ptz_start_tour(i)` / `ptz_stop_tour` | `ptz.getTours`, `ptz.start` (`Start/StopTour`) | |
| `ptz_goto_home(ch)` / `ptz_reset(ch)` | `ptz.gotoHomePosition` (fallback `GotoHome`) / `ptz.reset` | |
| `get_time()` / `set_time(dt)` | `global.getCurrentTime` / `setCurrentTime` | time value is in `result` |
| `reboot()` / `shutdown()` | `magicBox.reboot` / `shutdown` | |
| `snapshot(channel)` | HTTP CGI `/cgi-bin/snapshot.cgi` | RPC `snapManager.attach` is unsupported on these cameras |
| `events(...)` / `iter_events(...)` | `eventManager.attach` | long-lived push stream over a dedicated connection |
| `call(method, params)` | *any* | raises `DahuaError`, returns the unwrapped payload |
| `request(method, params)` | *any* | low-level, returns `(envelope, binary)`, never raises |
| `rtsp_url` / `record_rtsp` / `iter_rtsp` | RTSP (554) | live video; configurable path template; `record_rtsp`/`iter_rtsp` need ffmpeg |
| `get_channel_titles` / `set_channel_title(text, ch)` | `configManager` `ChannelTitle` | OSD title overlay |
| `get_osd(ch)` / `set_osd(data, ch)` | `configManager` `VideoWidget` | OSD overlay layout/covers |
| `find_files(start, end, ch)` | `mediaFileFind.*` | list recordings (returns `FilePath`…) |
| `firmware_state()` / `upgrade_firmware(path, confirm=True)` | `upgrader.*` | ⚠️ upgrade is reconstructed + can brick; mock-validated only |
| `dahua.discover(timeout)` | `DHDiscover.search` (multicast) | LAN device discovery (needs L2 adjacency) |
| `HttpMediaClient.record(...)` / `download_file(path, out)` | HTTP `streamReader.*` / `RPC_Loadfile` | live video → DHAV / recorded-file download (HTTP transport) |

## Protocol

### Wire format (little-endian)

```
32-byte header | JSON (messageLength bytes) | binary (dataLength bytes)

off size field           value / meaning
  0  u32 size/headFlag    0x00000020  (constant = header length)
  4  u32 magic            0x50494844 == "DHIP"
  8  u32 sessionID        0 before login
 12  u32 requestID        mirrors the JSON "id"
 16  u32 packageLength    messageLength + dataLength
 20  u32 packageIndex     fragment index
 24  u32 messageLength    length of the JSON text
 28  u32 dataLength       length of trailing binary (0 for pure JSON)
```

### Login (two-stage digest challenge/response)

```
1. global.login {userName, password:"", clientType, loginType:"Direct"}
   -> server replies with params.realm and params.random (+ session id)
2. pwd  = MD5(f"{user}:{realm}:{password}").hexdigest().upper()
   resp = MD5(f"{user}:{random}:{pwd}").hexdigest().upper()
   global.login {userName, password:resp, realm, random, ...}
```

`realm` is the full `"Login to <name>"` string read from the challenge, so the
digest matches the on-device account hash without hardcoding anything.

### Response envelope (not uniform!)

* Most getters: `{"result": true, "params": {...}}` — data is in `params`.
* `global.getCurrentTime`: `{"result": "2026-06-18 11:46:19", "params": null}` —
  the value is in `result`.
* `userManager.getGroupInfoAll`: `params` is a **list**, not `{"groups": [...]}`.
* Errors: `{"result": false, "error": {"code": 405, "message": "Method not allowed"}}`.

`call()` smooths these over (see `dahua.transport.extract`); `request()` hands
back the raw envelope.

## Device support & status

The protocol is reconstructed from firmware and behaviour, and Dahua exposes no
official spec — so feature support varies by model and firmware. The library was
developed and verified against a **Zenointel SD-2N-4G** PTZ dome (a
Dahua-derived OEM camera; HiSilicon GK7205V510) that speaks the DHIP protocol. The list below marks what was confirmed on real hardware versus
what is reconstructed and covered only by the offline test suite.

**Verified on hardware**

- Login / keep-alive / logout; sync and async clients.
- `magicBox.*` device info; `configManager` get/set (General, Encode, Network,
  Snap, ChannelTitle, VideoWidget); users & groups (incl. add/delete);
  `get_time`/`set_time`; reboot.
- PTZ: directional, zoom/focus/iris, presets, status/position, and absolute
  positioning. `moveAbsolutely` is commanded in **degrees** (pan ≥360 is
  rejected); `getStatus` reports raw encoder units, which `ptz_position_degrees`
  converts via the configurable `ptz_location_fullscale` / `ptz_tilt_span_deg`.
- Snapshot (HTTP CGI) and **RTSP** live capture (`record_rtsp`).
- `eventManager.attach` handshake.

**Reconstructed / covered by the offline tests only**

- Event *delivery* parsing (`client.notifyEventStream`), multi-fragment binary
  reassembly, and `HttpMediaClient` (`RPC_Loadfile`) — exercised against the
  fake servers in `tests/`, since not all firmwares expose these paths.
- `find_files` / `download_file`, `discover` (needs L2 adjacency), and
  `upgrade_firmware` — the last is **destructive**, requires `confirm=True`, and
  is intentionally never run against a device.

Feature-specific wire details that may vary between firmwares (the RTSP path
template, the `RPC_Loadfile` request) are kept in one place each so they are easy
to adjust for your device.

## Tests

```bash
python -m unittest discover -s tests     # offline, scriptable fake servers
python tests/test_loopback.py            # original framing/login self-test
```

## Contributing

Patches welcome. See [CLAUDE.md](CLAUDE.md) for the architecture, conventions,
and how to add and verify new RPC methods. Please run the test suite before
submitting.

## License

MIT — see [LICENSE](LICENSE).

## Authorization

Intended for testing and recovery of **devices you own or are authorized to
assess**. Use responsibly.
