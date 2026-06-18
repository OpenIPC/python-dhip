# Contributing to python-dhip

A guide for contributors (human or AI) working on this codebase. Read it before
sending a patch.

## What this is

A pure-stdlib client for the **Dahua DHIP** binary RPC2 protocol (TCP 5000): a
32-byte little-endian header, a JSON RPC2 body, and an optional trailing binary
payload. There is no official protocol spec; everything here is reconstructed
from firmware and device behaviour. Keep that in mind — when in doubt, verify
against a real device and document what you observed.

## Layout

```
dahua/
  const.py        wire constants, RPC method names, PTZ codes, error-code map
  exceptions.py   DHIPError (base) → DahuaError(code) → LoginError
  transport.py    DHIPTransport: framing, two-stage login, request()/call()
  client.py       DahuaClient: high-level sync API (built on transport)
  aio.py          AsyncDahuaClient: asyncio mirror of the sync API
  events.py       EventListener: eventManager.attach over a dedicated connection
  media.py        HttpMediaClient: HTTP RPC2 + RPC_Loadfile (live + file download)
  rtsp.py         RTSP URL builder + ffmpeg-based record/iter helpers
  discovery.py    DHDiscover multicast LAN discovery
  cli.py          argparse CLI → the `dhip` console entry point
dhip.py           backward-compat shim (re-exports the old single-file API)
tests/            unittest + scriptable fake servers (no device needed)
examples/         runnable scripts
```

## Core conventions

- **Pure stdlib only.** No runtime third-party dependencies. The one exception
  is `rtsp.py`, which shells out to `ffmpeg` (an external binary, only for video
  capture) — never import a Python package that isn't in the standard library.
- **`request()` vs `call()`.**
  - `request(method, params)` → `(envelope, binary)`, **never raises** on
    `result: false`. This is the low-level primitive and the backward-compatible
    contract; don't change its signature or raising behaviour.
  - `call(method, params)` → the unwrapped payload, **raises `DahuaError`** on
    failure. High-level helpers are built on `call`.
- **The response envelope is not uniform.** Use `transport.extract()` (already
  applied by `call`): most getters put data in `params`, but some return it in
  `result` (e.g. `global.getCurrentTime`) or as a bare list (e.g.
  `userManager.getGroupInfoAll`). New getters should tolerate all three.
- **Boolean-result methods.** Some methods use `result` as the actual boolean
  answer (`ptz.isMoving`) or return a benign `result: false` (PTZ axis at a
  limit, an op a firmware doesn't implement). For these, go through `request()`
  and return `bool(resp.get("result"))` — do **not** use `call()`, which would
  raise. See `DahuaClient._ptz_bool`.
- **Errors** are typed: raise `DahuaError(message, code=..., method=...)`; map
  known codes via `const.DAHUA_ERRORS` / `const.error_message`.
- **Channels** are 0-based in the RPC API (RTSP templates are the exception and
  are firmware-specific — see `rtsp.py`).
- **Method names** live in `const.py`, not inline string literals, where they
  are reused.

## Adding a new RPC method

1. Add the method-name constant to `const.py` if it'll be reused.
2. Add the helper to `DahuaClient` (in `client.py`), built on `self.call(...)`
   (or `request()` for boolean/benign-false methods). Match the docstring and
   naming style of the surrounding code.
3. If it's part of the async surface, mirror it in `AsyncDahuaClient`
   (`aio.py`). **Keep sync and async in sync** — they share `const.py`,
   `exceptions.py`, and the digest/extract helpers, but the method bodies are
   parallel implementations.
4. Add a CLI subcommand in `cli.py` if it's user-facing.
5. Add a test (see below) and, if user-facing, an example.

## Verifying

- **Offline (required):** add a handler to the relevant fake server and a test.
  - `tests/fake_server.py` — `FakeDHIPServer`, a scriptable DHIP server
    (`handlers` is a `{method: callable(req) -> response_dict}` map; unknown
    methods return a 405 envelope, matching real devices).
  - `tests/test_media.py` — a fake HTTP server for the `RPC_Loadfile` flow.
  - Run everything: `python -m unittest discover -s tests`
    (plus `python tests/test_loopback.py` for the legacy self-test).
- **On hardware (encouraged):** verify against a real device and note what you
  saw in the PR. The `dhip` CLI and `system.listMethod` / `system.listService`
  are useful for probing what a given firmware supports. Devices vary — guard
  destructive operations (e.g. `upgrade_firmware` requires `confirm=True`) and
  prefer reversible checks (set a value, then restore it).

## Backward compatibility

`dhip.py` is a compat shim: `DHIPClient` is an alias of `DahuaClient`, and the
old names (`DHIP_MAGIC`, `HEADER_FMT`, `HEADER_SIZE`, `_md5_upper`, …) are
re-exported. `from dhip import DHIPClient, DHIP_MAGIC, ...` must keep working,
and `request()`/`login()`/`keep_alive()`/`logout()` must keep their signatures
and return types. There's a test for this; don't break it.

## Style

- Type hints on public methods; concise docstrings explaining intent, units, and
  any device-specific behaviour.
- Match the comment density and idiom of the file you're editing.
- Document the *why* for anything reconstructed or firmware-specific, and isolate
  wire details that may vary by firmware in one place (as `rtsp.py` and
  `media.py::_loadfile_request` do).

## PR checklist

- [ ] `python -m unittest discover -s tests` passes; `tests/test_loopback.py` passes.
- [ ] New/changed public API has a test and (if user-facing) an example + CLI flag.
- [ ] Sync and async surfaces stay consistent.
- [ ] README API table updated if you added a public method.
- [ ] No new third-party dependencies; no secrets or device-specific hosts in code.
- [ ] If verified on hardware, say which model/firmware and what you observed.
