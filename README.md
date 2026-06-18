# python-dhip

A pure-stdlib Python library for **Dahua DHIP** (binary RPC2) IP cameras — the
`CDVRIPRPCClient` / `DHIPHeader` transport spoken by the `hunter` daemon on
Zenointel / Rostelecom (and other Dahua-derived) cameras on TCP **5000**.

It is the Dahua counterpart to [`python-dvr`](https://github.com/NeiroNx/python-dvr)
(XiongMai/Sofia cameras).

> This is **not** the XiongMai/Sofia "NetSurveillance" protocol (TCP 34567,
> `0xFF` header, `sofia_hash`). Different port, framing, auth hash and command
> model. Note also that legacy Dahua devices expose a *different* framing on TCP
> 37777 — DHIP here is **port 5000**.

No third-party dependencies. Sync and async clients, a CLI, and an event
listener are all included.

## Install

```bash
pip install -e .          # from a checkout
dhip --help               # console entry point
```

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

Some Dahua cameras serve live video over the binary DHIP transport; many serve
it over the **HTTP RPC2 + `RPC_Loadfile`** channel instead. `HttpMediaClient`
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

See the status note below — this flow is validated against a mock but the test
camera available has its HTTP RPC2 endpoint locked, so it could not be confirmed
on hardware.

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
| `ptz_status(ch)` / `ptz_position(ch)` | `ptz.getStatus` | live `[pan, tilt(, zoom)]` |
| `ptz_caps(ch)` | `ptz.getCurrentProtocolCaps` | pan/tilt speed ranges |
| `ptz_is_moving(ch)` | `ptz.isMoving` | bool in `result` |
| `ptz_start/stop/move(code, ...)` | `ptz.start` / `ptz.stop` | code API; codes in `dahua.const.PTZ_CODES` |
| `ptz_up/down/left/right(...)` | `ptz.start`+`stop` | timed directional nudge |
| `ptz_zoom/focus/iris(direction, ...)` | `ptz.start`+`stop` | `in`/`out`, `near`/`far`, `open`/`close` |
| `ptz_move_absolutely(pan, tilt, zoom)` | `ptz.moveAbsolutely` | slew to an absolute position |
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
| `HttpMediaClient.record(out, channel, subtype, duration)` | HTTP `streamReader.*` + `RPC_Loadfile` | live video → DHAV file (separate HTTP transport) |

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

## Status: verified vs reconstructed

Verified against a live **SD-2N-4G** (Rostelecom-branded PTZ cam, firmware
`30.13...R`):

- ✅ login, keep-alive, logout; all `magicBox.*` getters; `configManager.getConfig`
  (General/Encode/Network/Snap); users & groups; `get_time`/`set_time`; PTZ
  start/stop/move; user add+delete; snapshot via HTTP CGI; `eventManager.attach`
  handshake (returns a `SID`); sync **and** async clients.
- ⚠️ **Event delivery**: the attach handshake is confirmed, but the lab camera
  was idle so no events were pushed. Push parsing follows the documented
  `client.notifyEventStream` / `eventList` format and is not yet exercised
  against a live trigger.
- ⚠️ **Multi-fragment binary reassembly** (`packageIndex`) is handled
  frame-by-frame and covered by the offline test, but not observed on a real
  large-payload response (snapshots use HTTP, not the binary RPC channel here).
- ⚠️ **Live video (`HttpMediaClient`, `RPC_Loadfile`)**: the `streamReader.create`
  + `RPC_Loadfile` flow is validated end-to-end against a fake HTTP server
  (`tests/test_media.py`) but **not** confirmed on hardware. The available test
  camera is a carrier-locked SD-2N-4G where *no* live transport is reachable:
  the binary DHIP media subsystem (`streamReader.*`, `devVideoEncode.*`,
  `media.*`) returns `400`; HTTP `/RPC2` returns `301 → /` for every method
  except `/RPC2_Login`; the MJPEG/realmonitor CGIs return `401`; RTSP
  `/cam/realmonitor` returns `404`; and there is no SD card for recorded
  playback. Only `snapshot.cgi` (single JPEGs) works. On a non-locked Dahua
  device `HttpMediaClient.record()` is expected to work; the `RPC_Loadfile`
  request line lives in one place (`dahua/media.py::_loadfile_request`) for easy
  adjustment if a given firmware differs.

## Tests

```bash
python -m unittest tests.test_dahua      # offline, scriptable fake server
python tests/test_loopback.py            # original framing/login self-test
```

## Authorization

Intended for testing and recovery of **devices you own or are authorized to
assess**. Use responsibly.
