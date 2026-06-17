# python-dhip

A minimal, dependency-free Python client for the **Dahua DHIP** binary RPC2
protocol — the `CDVRIPRPCClient` / `DHIPHeader` transport spoken by the `hunter`
daemon on Zenointel / Rostelecom (and other Dahua-derived) IP cameras on TCP
**37777**.

> This is **not** the XiongMai/Sofia "NetSurveillance" protocol (TCP 34567,
> `0xFF` header, `sofia_hash`) that [`python-dvr`](https://github.com/NeiroNx/python-dvr)
> implements. Different port, framing, auth hash and command model.

## Wire format (little-endian)

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

## Login (two-stage digest challenge/response)

```
1. global.login {userName, password:"", clientType, loginType:"Direct"}
   -> server replies with params.realm and params.random (+ session id)
2. pwd  = MD5(f"{user}:{realm}:{password}").hexdigest().upper()
   resp = MD5(f"{user}:{random}:{pwd}").hexdigest().upper()
   global.login {userName, password:resp, realm, random, ...}
```

`realm` is read from the challenge (it is the full `"Login to <name>"` string),
so the digest matches the on-device account hash without hardcoding anything.

## Usage

```bash
# one-shot CLI
python dhip.py 10.0.0.10 -u admin -P admin54321 -m magicBox.getSystemInfo

# arbitrary method with params
python dhip.py 10.0.0.10 -u admin -P secret \
    -m configManager.getConfig --params '{"name":"General"}'

# example script
python examples/get_info.py 10.0.0.10 admin admin54321
```

```python
from dhip import DHIPClient

with DHIPClient("10.0.0.10") as c:
    c.login("admin", "admin54321")
    resp, _ = c.request("magicBox.getSystemInfo")
    print(resp["params"])
```

## Notes / limitations

- Single-request/response; no async, no event subscription, no multi-fragment
  reassembly beyond a single `packageLength` body.
- Some methods require a session keep-alive: call `c.keep_alive()` periodically.
- Authority-gated methods (account management, **firmware upgrade**) require an
  `admin`-group session — see the project notes on the camera's auth model.

## Authorization

Intended for testing and recovery of **devices you own or are authorized to
assess**. Use responsibly.
