# py-opendisplay WiFi Transport Readiness Audit

**Date:** 2026-07-22
**Repo audited:** `/home/davelee/opendisplay/py-opendisplay` (version 7.13.0)
**Scope:** Assess readiness to add **WiFi/IP as a transport between Home Assistant and the display**, with **BLE and WiFi allowed to be simultaneously active on one device**. Report-only — no source changes.
**Canonical protocol:** `/home/davelee/opendisplay/opendisplay-protocol/src/opendisplay_protocol.h`

---

## Executive summary

py-opendisplay is today a **BLE-only** library. There is **zero IP/TCP/HTTP/WebSocket/socket/mqtt/zeroconf/mDNS transport code** — the only "wifi" in the tree is `WifiConfig` (TLV packet `0x26`), a *provisioning* payload that ships an SSID/password/server host+port **to the tag over BLE**. It is a data model, not a transport.

The good news: the byte-pipe is already isolated in a single small class, `BLEConnection` (`src/opendisplay/transport/connection.py`, 439 lines), and the wire framing (`src/opendisplay/protocol/commands.py`) and per-frame encryption (`src/opendisplay/crypto.py`) are **pure byte builders with no BLE dependency**. The PIPE_WRITE sliding-window engine, stop-and-wait command loop, and AES-GCM session all operate on `bytes` in and `bytes` out. A TCP or WebSocket byte-pipe could in principle be substituted **underneath the same framing** with a modest refactor.

The bad news: there is **no transport abstraction**. `OpenDisplayDevice` (`src/opendisplay/device.py`, 2763 lines) is hard-wired to a concrete `BLEConnection` via a `self._conn` property (line 624), constructs it directly in `__aenter__` (line 573), and threads BLE-specific concepts throughout: MAC address as the sole identity, an injected `BLEDevice`, GATT-cache clearing, Write-Without-Response semantics for bulk chunks, and a hardcoded 244-byte frame ceiling (`DEFAULT_MAX_FRAME`) that is explicitly the *HA GATT write ceiling*. Discovery is `BleakScanner`-only. Identity, capability detection, and the HA integration's per-MAC serialization lock all assume "one device == one BLE MAC == one radio link."

**Headline recommendation:** Introduce a `Transport` Protocol (async byte-pipe: `connect/disconnect/write/read/is_connected` + a `max_frame`/`supports_write_without_response` capability surface), rename/rehome `BLEConnection` behind it as `BleTransport`, add a `TcpTransport` (or `WsTransport`), and make `OpenDisplayDevice` accept an injected transport (or a transport-selection policy) instead of building `BLEConnection` itself. Keep the existing `mac_address=`/`ble_device=` constructor kwargs working (delegating to a `BleTransport`) so the HA integration's call sites stay source-compatible. This is a **medium refactor** concentrated in ~4 files, not a rewrite — the framing/crypto/PIPE layers are already transport-agnostic in all but a few hardcoded constants.

---

## What exists today

### No IP/network transport of any kind
A full-tree search for `wifi|tcp|http|socket|aiohttp|mqtt|mdns|zeroconf|transport|backend|inet|websocket` in `src/` returns:
- **`transport/`** — a package containing only `BLEConnection` (BLE). `src/opendisplay/transport/__init__.py` exports exactly `BLEConnection`.
- **`WifiConfig`** — provisioning data model (see below). Not a transport.
- **`landing.py`** — builds `https://opendisplay.org/l/?...` deep-link URLs (a QR/NFC convenience, unrelated to a HA↔device transport).

No `aiohttp`, `websockets`, `zeroconf`, `paho-mqtt`, or `asyncio` socket code. `pyproject.toml` dependencies are `bleak`, `bleak-retry-connector`, `pillow`, `numpy`, `epaper-dithering==5.0.9`, `cryptography` — **no networking libraries**.

### The one WiFi artifact: `WifiConfig` provisioning (BLE-delivered)
- **Model:** `src/opendisplay/models/config.py:673` — `WifiConfig` dataclass, 160-byte fixed layout: `ssid` (32B C-string), `password` (32B), `encryption_type` (uint8), `server_url` (64B C-string), `server_port` (uint16 **big-endian**), `reserved` (29B). Constructors `from_bytes` / `from_strings` (default `server_port=2446`, i.e. `0x2446` = the OpenDisplay manufacturer id) / `to_bytes`.
- **Enum:** `src/opendisplay/models/enums.py:194` — `WifiEncryption`.
- **Serialize/parse:** `src/opendisplay/protocol/config_serializer.py:541` (`serialize_wifi_config`, packet type `0x26`) and `src/opendisplay/protocol/config_parser.py:525` (`_parse_wifi_config`, handles legacy 65-byte and current 160-byte). Header confirms `0x26 wifi` TLV at `opendisplay_protocol.h:242`.
- **Exposure:** part of `GlobalConfig` (`config.py`), so it round-trips through `write_config()` / `interrogate()` / JSON export (`models/config_json.py:283`) and is rendered by the CLI `info` command (`cli.py:299-384`, "WiFi" tree with SSID + `server:port`).

**Interpretation:** the firmware/config schema *already anticipates* a WiFi-connected tag that phones home to a `server_url:server_port`. But: (a) the library only knows how to *write these credentials over BLE*, (b) there is **no convenience API** to push just WiFi credentials — the caller must build a full `GlobalConfig` and call `write_config()`, and (c) nothing in py-opendisplay ever *opens an IP connection* to that server or acts as that server. The `server_url`/`server_port` field implies a **device-initiated (tag→server) or server-hosted** model, which is architecturally different from HA↔BLE (host-initiated). This matters for the design (see Gap 6).

---

## Current transport architecture

### Layering (bottom to top)
```
bleak / bleak-retry-connector            (3rd-party BLE stack)
  └─ BLEConnection            transport/connection.py   — byte-pipe over one GATT char
       └─ OpenDisplayDevice   device.py                 — command loop, PIPE engine, session
            ├─ protocol/commands.py                     — pure byte framing builders
            ├─ crypto.py                                — per-frame AES-GCM (transport-agnostic)
            └─ partial.py, encoding/, models/           — payload prep (transport-agnostic)
```

### `BLEConnection` — the byte-pipe (`transport/connection.py`)
This is the **de-facto transport interface**, and it is already close to a clean async byte-pipe. Its surface:
- `async connect()` (line 87) / `async disconnect()` (237) / `is_connected` property (436)
- `async __aenter__`/`__aexit__` (73/78)
- `async write_command(data: bytes, response: bool = True, drain_stale: bool = True)` (364)
- `async read_response(timeout: float = 5.0) -> bytes` (409)
- `drain_notifications() -> int` (343)
- `async clear_cache() -> bool` (253) — ESPHome-proxy GATT-cache clear (BLE-specific)

**BLE-specific internals** that would *not* carry over to IP, and mark the coupling seam:
- Construction from `mac_address` + optional `BLEDevice` (line 38-46); resolves MAC→`BLEDevice` via `BleakScanner.find_device_by_address` (172).
- Connects with `bleak-retry-connector.establish_connection` + `BleakClientWithServiceCache` (182), stale-GATT-cache retry loop (107-137), `_is_stale_cache_error` heuristics keyed on the GATT `SERVICE_UUID` (139).
- Response delivery is a **GATT notification queue**: `_notification_queue: asyncio.Queue[bytes]` (66), fed by `_notification_callback` (333); `read_response` awaits the queue. There is no length-prefix or framing on the pipe — **each GATT notification is one logical frame**. A stream transport (TCP) has no such message boundaries and would need a framing/length-delimiter shim to preserve the "one read == one frame" contract every caller in `device.py` relies on.
- Write-Without-Response: `_write_no_response_supported` sniffed from characteristic properties (305); `write_command(response=False)` maps to a GATT Write Command for bulk `0x71`/`0x81` chunks (398-405). This is a BLE flow-control optimization with **no TCP analogue** (TCP is already reliable/ordered), but the `response` flag is threaded up into `device.py` and would need a no-op mapping.

### `OpenDisplayDevice` — command loop + PIPE engine (`device.py`)
- **Coupling to the pipe:** the `_conn` property (line 624) returns a concrete `BLEConnection` and raises if unset; `__aenter__` (544) *constructs* `BLEConnection(...)` directly at line 573, passing MAC, `BLEDevice`, timeout, `max_attempts`, `use_services_cache`, and a `disconnected_callback`. Every command goes through `self._conn.write_command(...)` / `self._conn.read_response(...)` — there are **~40+ direct `self._conn` call sites**. Substituting a transport means either (a) making `_conn` a `Transport` Protocol, or (b) injecting the transport in `__init__`/`__aenter__`.
- **Command serialization:** `self._command_lock = asyncio.Lock()` + `_lock_owner` (530-531), reentrant per-task via `_transaction()` (631). This is **per-device-object**, not per-MAC and not process-global. It has no BLE assumption and carries over unchanged.
- **Encryption:** `_encrypt_frame`/`_write`/`_read`/`_write_pipe_frame` (671-763) wrap AES-GCM (`crypto.py`) around `bytes` frames. Session nonce/reauth is timer-based. **Fully transport-agnostic** — encrypts the same bytes regardless of pipe. Note: over IP, TLS would arguably supersede this app-layer AES-GCM, but it can coexist.
- **Framing:** `protocol/commands.py` builds every opcode as pure `bytes` (`build_pipe_write_start_command`, chunk builders, etc.). `CHUNK_SIZE=230`, `ENCRYPTED_CHUNK_SIZE=154`, `CONFIG_CHUNK_SIZE=200`, `NFC_CHUNK_SIZE=120` (`commands.py:57-89`). **No BLE dependency** — these are protocol constants.
- **The 244 ceiling:** `DEFAULT_MAX_FRAME = 244` (`commands.py:71`), commented *"HA native GATT write ceiling (client_max_frame request)."* Used verbatim as the negotiated `req_frame` in `_negotiate_pipe` (`device.py:2296`) and `_negotiate_pipe_partial` (2368). Over WiFi this ceiling is artificial — TCP/WS could carry far larger frames — but the **firmware** side of the 0x0080 negotiation also has a `dev_max_frame`, and the min-rule (`frame_eff = min(req_frame, dev_max_frame)`, line 2328) means a WiFi transport could request a larger `max_frame` and let firmware cap it. This is the single most transport-shaped constant and belongs on the transport, not on the protocol module.
- **PIPE_WRITE (0x0080–0x0082):** sliding-window engine in `_negotiate_pipe` (2280), `_run_pipe_upload` (2468), `_send_pipe_chunks` (2525), `_await_pipe_end_ack` (2699). Opcodes/flags in `commands.py:42-71` (`PIPE_WRITE_START/DATA/END`, `PIPE_FLAG_COMPRESSED=0x01`, `PIPE_FLAG_PARTIAL=0x02`, `PIPE_FRAME_OVERHEAD=3`, `PIPE_VERSION=1`, `TIMEOUT_PIPE_START=30.0`). The window/ACK-cadence machinery (`blocks_per_ack`, `max_queue_size`, min-rule at 2324-2330, selective-ACK bit) is a **reliability/flow-control protocol layered on top of an unreliable, unordered BLE notification pipe**. Over TCP much of this is redundant (TCP already gives reliable ordered delivery), but it would still *function* — a WiFi transport can keep PIPE_WRITE as-is, or negotiate a trivial window; either works, so this is not a blocker.
- **Retry/timeout/deadline:** per-operation timeouts as class constants (`TIMEOUT_FIRST_CHUNK`, `TIMEOUT_ACK`, `TIMEOUT_PIPE_DATA_*`, etc., 433-453) tuned to **BLE/e-paper SPI stalls** (up to 90s). These are conservative but not BLE-*coupled*; they'd be reused (possibly relaxed) over WiFi.
- **Disconnect handling:** `_on_ble_disconnect` (661) clears session + PIPE state; wired via `disconnected_callback`. Naming is BLE-flavored but the mechanism (link-drop → drop session) is transport-neutral.

### Discovery / identity
- `discovery.py` — `discover_devices()` / `discover_devices_with_adv()` are **`BleakScanner`-only**, filtered by `MANUFACTURER_ID = 0x2446` (`commands.py:53`), returning `{name: mac}` or `{name: (mac, AdvertisementData)}`. Passive advert parsing (battery/temp/loop counter, button/touch events) lives in `models/advertisement.py`. **All identity is BLE MAC.** There is no device-id/serial abstraction that survives across transports.

### The per-MAC lock is NOT in the library
The recent "process-global per-MAC connection lock" work (git `07d4976`) lives in the **HA integration**, not py-opendisplay: `custom_components/opendisplay/ble_lock.py` — a `WeakValueDictionary[str, asyncio.Lock]` keyed by `format_mac(address)` (`ble_lock.py:35`), with a contention WARNING (`ble_connection()`, line 55). It serializes *all host-side connects to a given MAC* because "the device exposes a single BLE link and the library holds no per-address lock." **For simultaneous BLE+WiFi this lock's premise changes**: a tag reachable on *both* radios has two independent links, and a MAC-keyed lock (a) may not have the WiFi peer's MAC and (b) would wrongly serialize a BLE op against a WiFi op that could run concurrently. This is an integration-side gap, but the library should expose enough identity/transport info for the integration to key the lock correctly (see Gap 8).

---

## Public API surface (what the HA integration consumes)

From grepping `custom_components/opendisplay/`, the integration depends on this surface. **Anything here is a compatibility constraint on a transport refactor.**

**Constructor (services.py:410, delivery.py:326, __init__.py:242, update.py:262, config_flow.py):**
```python
OpenDisplayDevice(
    mac_address=address,          # BLE MAC — primary identity
    ble_device=ble_device,        # BLEDevice from HA bluetooth (async_ble_device_from_address)
    config=entry.runtime_data.device_config,
    capabilities=..., encryption_key=..., use_measured_palettes=...,
    timeout=..., max_attempts=..., use_services_cache=...,
    blocks_per_ack=..., max_queue_size=...,
) as device:                      # used as async context manager
```
The integration obtains `ble_device` via `homeassistant.components.bluetooth.async_ble_device_from_address(hass, address, connectable=True)` (`services.py:343`, `delivery.py:312`) — **a BLE-only HA helper**. A WiFi transport has no `BLEDevice` equivalent from HA.

**Methods called by HA:** `upload_image` / `upload_prepared_image` (services `_upload`), `interrogate`, `activate_led` (`_led`), `activate_buzzer` (`_buzz`), `write_config`, `deep_sleep` (`sleep.py`), `reboot`, `read_firmware_version`, `clear_gatt_cache`, `trigger_dfu_bootloader`, `authenticate`, and read-only properties (`config`, `capabilities`, `width`, `height`, `color_scheme`, `device_name`, `is_flex`, `landing_url`). `perform_silabs_ota` (`ota.py`) for updates.

**Module-level:** `MANUFACTURER_ID`, `AdvertisementTracker`, `parse_advertisement`, `decode_button_event` (coordinator/event passive BLE advert path); `discover_devices` (config flow); the exception hierarchy (`BLEConnectionError`, `BLETimeoutError`, `AuthenticationFailedError`, `AuthenticationRequiredError`, `OTAError`, …) — **note these are BLE-named** (`exceptions.py:12` `BLEConnectionError`), which will read awkwardly for IP failures unless generalized or aliased.

**Stability implication:** a transport abstraction can be introduced **without breaking any of this** if `mac_address=`/`ble_device=` continue to select a `BleTransport` by default. New WiFi usage would add an alternative selector (e.g. `host=`/`transport=`) without disturbing existing call sites.

---

## Gap analysis

1. **No transport abstraction layer.** `OpenDisplayDevice` builds and calls a concrete `BLEConnection`. There is no `Transport` interface, no dependency injection point, no factory. *This is the foundational gap — everything else depends on closing it.*

2. **No IP transport implementation.** No TCP/WebSocket byte-pipe. Would need: connect to `host:port`, a **framing shim** (length-prefix or delimiter) to reconstruct the "one read == one logical frame" contract that BLE gets for free from GATT notifications, a `response`-flag no-op mapping, and a `max_frame` capability far larger than 244.

3. **Discovery is BLE-only.** `BleakScanner` + manufacturer id `0x2446`. No mDNS/zeroconf discovery of WiFi tags. No `zeroconf` dependency. A WiFi tag on the LAN cannot be found by the library today. HA has its own zeroconf, but the library offers nothing to enumerate or resolve IP peers.

4. **Identity is MAC-only.** `mac_address` is the primary key throughout (constructor, discovery return type `{name: mac}`, HA lock, config entry `unique_id`). There is no stable transport-independent device id. A tag reachable by both BLE MAC and an IP address needs a **single logical identity** that both transports map to, or HA will treat it as two devices. Firmware likely exposes a serial/`device_id` (worth confirming in the header), but py-opendisplay does not surface one as identity.

5. **Provisioning helper is minimal.** `WifiConfig` exists and rides `write_config()`, but there is **no first-class "send WiFi credentials over BLE" convenience** (e.g. `device.provision_wifi(ssid, password, server_url, server_port)` that patches only the `0x26` TLV). Callers must construct/merge a whole `GlobalConfig`. The natural bring-up flow (connect over BLE → push WiFi creds → device joins WiFi → reconnect over IP) is entirely manual.

6. **Server/role model is unclear and possibly inverted.** `WifiConfig.server_url`/`server_port` (default `2446`) implies the **tag connects out to a server**, or a server hosts an endpoint the tag polls — not the HA-initiated, host-drives-the-link model BLE uses. If WiFi tags are *outbound/poll* clients, py-opendisplay's entire "open a connection and push frames" shape doesn't map, and the library (and/or HA) would instead need to **run a listener/server** that tags connect to. This is a **protocol-design question that must be resolved before the transport layer is designed**, and it is not answerable from py-opendisplay alone — it needs the firmware/protocol-header WiFi contract (which does not yet define a TCP/WS opcode framing — the header only has the `0x26` provisioning TLV).

7. **No security/auth story for IP.** App-layer AES-GCM (`crypto.py`) is available and transport-agnostic, but there is no TLS, no cert/PSK handling, and no notion that an IP peer is un-authenticated by default (BLE has physical-proximity as weak auth; a LAN socket does not). `SecurityConfig` (`config.py:773`) carries an AES-128 master key that could be reused, but nothing wires it to an IP transport.

8. **Capability detection for WiFi is absent.** Capability bits exist (`transmission_modes` bitfield with `supports_pipe_write` = bit `0x10`, `config.py:355`; `device_flags`, `config.py:62`) but **none advertise "this device supports a WiFi/IP transport" or "is currently on WiFi."** The library cannot know from an interrogation whether to prefer IP. A new capability bit (firmware + `models/config.py` property) is needed, plus a way to learn the device's current IP (adverts carry battery/temp today, not an IP).

9. **Simultaneous BLE+WiFi is unmodeled.** `OpenDisplayDevice` == one link. There is no transport-selection policy, no preference/failover ("try WiFi, fall back to BLE"), no way to hold both open. The HA per-MAC lock (`ble_lock.py`) would incorrectly serialize cross-transport ops. Nothing coordinates two concurrent links to one logical device.

10. **BLE-named public API.** `BLEConnectionError`/`BLETimeoutError`, `clear_gatt_cache`, `use_services_cache`, `ble_device=`, `MANUFACTURER_ID`, `discover_devices→{name: mac}`. Usable over IP only with awkward semantics; ideally generalized (with back-compat aliases).

11. **CLI is BLE-only.** `cli.py` subcommands (`scan`, `info`, `upload`, `reboot`, `sleep`, `export-config`, `write-config`) all resolve a MAC and go over BLE. No `--host`, no `provision-wifi`, no IP discovery.

---

## Library implementation sketch

*Proposal only — no code was written. Concentrated in ~4 files plus one new subpackage.*

### 1. Transport Protocol (new: `src/opendisplay/transport/base.py`)
A structural `typing.Protocol` capturing exactly what `device.py` uses today:
```python
class Transport(Protocol):
    max_frame: int                     # replaces DEFAULT_MAX_FRAME=244 (per-transport)
    supports_write_without_response: bool
    is_connected: bool
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def write(self, data: bytes, *, response: bool = True, drain_stale: bool = True) -> None: ...
    async def read(self, timeout: float = 5.0) -> bytes: ...   # returns ONE logical frame
    def drain(self) -> int: ...
    # optional/BLE-only, default no-op on IP:
    async def clear_cache(self) -> bool: ...
```
Key contract: **`read()` returns exactly one logical protocol frame.** BLE satisfies this natively (one notification). IP transports satisfy it with an internal length-prefix framer.

### 2. `BleTransport` (rehome existing code)
Rename/wrap `BLEConnection` (`transport/connection.py`) as `BleTransport` implementing `Transport`. Set `max_frame = 244`, `supports_write_without_response` from characteristic sniffing. Keep the stale-GATT-cache retry, notification queue, and `clear_cache`. **Preserve `BLEConnection` as an alias** for external back-compat (it is exported).

### 3. `TcpTransport` / `WsTransport` (new: `src/opendisplay/transport/ip.py`)
`asyncio.open_connection(host, port)` (or `websockets`). Internal framer: length-prefix each write, buffer+reassemble reads into whole frames, feed an internal `asyncio.Queue[bytes]` so `read()` mirrors the BLE queue semantics. `max_frame` large (e.g. 1024+, still min-ruled against firmware's `dev_max_frame` in 0x0080). `response`/`drain_stale` flags accepted and effectively no-ops (TCP is reliable/ordered). `supports_write_without_response=False`. Adds a networking dep (`aiohttp` or `websockets`) as an **optional extra** `[wifi]` to keep the BLE-only install lean. **Blocked on resolving Gap 6** (client-connects-out vs. host-connects-in) before this can be finalized.

### 4. Wire it into `OpenDisplayDevice` (`device.py`)
- Change `self._conn` (property line 624) to type `Transport`.
- In `__aenter__` (line 573), instead of `BLEConnection(...)`, call a **transport factory / accept an injected transport**:
  - Add `transport: Transport | None = None` and/or `host: str | None = None` to `__init__`.
  - Selection policy: explicit `transport=` wins; else `host=` → `TcpTransport`; else `mac_address=`/`ble_device=` → `BleTransport` (**default, preserves every current HA call site**).
- Replace `DEFAULT_MAX_FRAME` usages in `_negotiate_pipe`/`_negotiate_pipe_partial` with `self._conn.max_frame`.
- Gate the WWR optimization on `self._conn.supports_write_without_response`.
- Generalize `_on_ble_disconnect` → `_on_disconnect` (behavior unchanged).

### 5. Discovery module for IP (new: `src/opendisplay/discovery_ip.py`, optional `[wifi]`)
`zeroconf`/`AsyncZeroconf` browse for an OpenDisplay service type (`_opendisplay._tcp.local.`), returning `{name: (host, port, device_id)}`. Keep BLE `discovery.py` untouched. Longer-term: a unified `discover()` returning transport-tagged endpoints keyed by a **stable device id** (Gap 4).

### 6. Provisioning API (small addition to `device.py`)
`async def provision_wifi(self, *, ssid, password, encryption_type=0, server_url="", server_port=2446) -> None` — reads current config (or uses cached), sets only the `0x26` `WifiConfig` TLV via `WifiConfig.from_strings`, writes back via the existing `write_config()`. Over BLE. This is the BLE→WiFi bring-up bridge and is **implementable today** with zero new transport work (it's pure `WifiConfig` + `write_config`).

### 7. Capability + identity
- Add a `supports_wifi` property to `DisplayConfig`/`SystemConfig` in `models/config.py` (new `transmission_modes` or `device_flags` bit) **once firmware defines it** in the canonical header — mirror the `supports_pipe_write` pattern (`config.py:355`).
- Introduce a transport-independent `device_id` (serial) as logical identity; map both MAC and IP to it. Requires a firmware-exposed id; surface it on `OpenDisplayDevice` and in discovery return types.

### 8. Simultaneous BLE+WiFi (policy layer)
- Short term: two `OpenDisplayDevice` instances (one per transport) coordinated by the caller. The library's per-object `_command_lock` already prevents intra-object concurrency.
- Medium term: a transport **preference/failover** policy (`prefer="wifi", fallback="ble"`) selecting at `__aenter__`.
- HA-side (out of library scope but library must enable): re-key `ble_lock.py` from raw MAC to the **logical `device_id`**, and make the lock transport-aware so a WiFi op and a BLE op to the *same* tag are serialized only if the *tag* can't handle both — surface a `transport.exclusive_with(other)` hint or a per-transport link token.

### Versioning / pinning implications
- `Home_Assistant_Integration/custom_components/opendisplay/manifest.json` pins an **exact** `py-opendisplay==` (per workspace CLAUDE.md). Any transport refactor that changes the constructor signature is a **minor/major bump** and requires a coordinated manifest pin update. Keeping `mac_address=`/`ble_device=` working makes it a **non-breaking minor** for HA — recommended.
- Networking deps go in an **optional `[wifi]` extra**, so the default HA install stays BLE-only until the integration opts in.

### Testing strategy
- Tests are `asyncio_mode=auto` (pytest-asyncio). A `Transport` Protocol makes `OpenDisplayDevice` **trivially unit-testable with a `FakeTransport`** (a scripted `asyncio.Queue` of frames) — the entire PIPE_WRITE / command / crypto suite could then run with **no BLE mocking at all**, a strict improvement over today's `bleak`-mocking. Add: (a) `FakeTransport` fixture, (b) `TcpTransport` framer round-trip tests (partial reads, coalesced frames, large frames), (c) transport-selection tests (mac vs host vs injected), (d) `provision_wifi` writes-only-`0x26` test against a config fixture. HA-side tests must confirm the per-MAC lock re-keying doesn't regress the existing serialization guarantees.

---

## Bottom line

The framing, crypto, PIPE_WRITE, and payload layers are **already transport-agnostic in substance** — they move `bytes`. The work is not rewriting the protocol; it is (1) extracting a `Transport` Protocol from the concrete `BLEConnection`, (2) inverting `OpenDisplayDevice`'s dependency so the transport is injected/selected rather than built, (3) writing an IP transport with a framing shim, and (4) answering the **open protocol question** of the WiFi role model (host-connects-in vs. tag-connects-out — Gap 6), which py-opendisplay cannot answer alone and which gates the IP transport's shape. The BLE-side per-MAC serialization and MAC-only identity are the two assumptions that most need generalizing before "BLE and WiFi simultaneously active on one device" is coherent.
