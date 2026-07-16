# FINDINGS: Merging upstream PR #89 (WiFi/LAN/TCP) onto current main

**Date:** 2026-07-06
**Branch under work:** `feature/wifi` (at `742adad` == `upstream/main`)
**Subject:** upstream PR #89 — "initial wifi/lan/tcp implementation" — commit `6e00434`,
branch `upstream/feat/tcp`, author Jonas Niesner
**Merge-base:** `9617757` (pre-dates the entire hardening series, PRs #93–#118)
**Firmware reference:** `/home/davelee/opendisplay/Firmware` (WiFi/TCP code is on `main`)

---

## 1. Architecture: how WiFi image sending works

### 1.1 Client–server model

**The display device is the TCP server; py-opendisplay is the client.**

- With `system_config.communication_modes` bit 2 (`COMM_MODE_WIFI`) set **and** a `wifi_config`
  TLV (type `0x26`: SSID, password, encryption type, optional port) written, the ESP32 firmware
  joins the configured WiFi network, listens on TCP port **2446** (default; overridable via the
  TLV), and advertises itself via mDNS as service **`_opendisplay._tcp`** with hostname
  `OD<chipid>.local`.
- The host (CLI, Home Assistant, any script using this library) discovers the device via mDNS
  (`discover_devices` LAN variant / `scan --lan`) or addresses it directly by `host:port`, opens
  a TCP socket (`LANConnection`), and speaks the **identical command protocol as BLE** — the
  firmware feeds LAN bytes into the same command dispatcher (`imageDataWritten`) BLE writes use.
- BLE and WiFi run **concurrently** on the device; enabling WiFi does not disable BLE.

### 1.2 Wire framing

TCP is a byte stream, so frames must be self-delimiting (BLE gets framing for free from GATT
packets):

```
[len: 2 bytes, little-endian][payload: len bytes]
payload = [command opcode: 2 bytes big-endian][command data ...]
```

- Both directions (commands *and* responses) use the same framing.
- Max payload: 4096 bytes (`WIFI_LAN_MAX_PAYLOAD`); oversized frames close the connection.
- Both sides must reassemble partial/coalesced TCP segments. The firmware does
  (`wifi_service.cpp:229-246`); the library's `LANConnection.read_response` does via
  `readexactly(2)` + `readexactly(len)`.

### 1.3 Image upload flow (unchanged vs BLE)

1. Connect → optionally authenticate (`0x0050` challenge-response) → read config (`0x0040`)
   to learn panel size/color scheme.
2. Prepare the image locally: rotate, fit, dither, encode to bitplanes, zlib-compress.
3. `0x0070` DIRECT_WRITE_START (uncompressed size + first payload bytes) → ACK.
4. Repeated `0x0071` DIRECT_WRITE_DATA chunks, each ACKed by the device.
5. `0x0072` DIRECT_WRITE_END with refresh mode → panel refreshes.

### 1.4 What LAN buys

| | BLE | LAN/WiFi |
|---|---|---|
| Data chunk size (`0x0071`) | 230 B (154 B encrypted) | **4094 B** (154 B encrypted) |
| ~100 KB compressed image | ~435 ACK round-trips | ~25 ACK round-trips |
| Reach | radio proximity + host BT adapter | anywhere on the LAN |
| Containers/VMs | needs Bluetooth passthrough | plain TCP |

### 1.5 How the code decides BLE vs WiFi

Transport selection is **explicit, made once at device construction — no automatic fallback,
sniffing, or racing of transports**.

`OpenDisplayDevice.__init__` gains `lan_address: tuple[str, int] | None`, mutually exclusive
with the BLE identifiers (`mac_address` / `device_name` / `ble_device`); combining them raises
`ValueError`.

- `lan_address=("192.168.1.50", 2446)` → `__aenter__` takes the LAN branch: builds a
  `LANConnection`, connects over TCP, sets `mac_address = "lan:host:port"`, then performs the
  same optional authenticate/interrogate steps. Every subsequent command — including the whole
  image-upload flow — goes over that socket.
- Any BLE identifier → the existing bleak scan/connect branch, unchanged.

Once connected, upload code is transport-blind: it talks to `self._conn` (typed
`BLEConnection | LANConnection`; both expose the same `connect`/`disconnect`/`write_command`/
`read_response` surface) and only sizes chunks via `_data_chunk_limit()`:

```python
def _data_chunk_limit(self) -> int:
    if self._session_key is not None:   # encrypted session wins — AES-CCM budget
        return ENCRYPTED_CHUNK_SIZE     # 154
    if self._lan_address is not None:
        return LAN_CHUNK_SIZE           # 4094
    return CHUNK_SIZE                   # 230
```

In the **CLI**, the single `--device ADDR` argument decides
(`_device_kwargs` → `_parse_lan_address` → `_is_ble_device_id`):

1. ADDR is a BLE MAC (6 colon-separated hex pairs) or 36-char macOS BLE UUID → **BLE**.
2. Else ADDR parses as `HOST:PORT` with port 1–65535 (e.g. `192.168.1.50:2446`,
   `OD1a2b3c.local:2446`) → **WiFi/LAN**.
3. Else → treated as a BLE device *name* to scan for.

---

## 2. PR #89 inventory (`git show 6e00434`, 11 files, +649/−44)

| File | Change | Merge result vs `742adad` |
|---|---|---|
| `src/opendisplay/transport/lan.py` | **New**: `LANConnection` (asyncio TCP, length-prefixed framing, BLE-shaped exception mapping, `LAN_MAX_FRAME_PAYLOAD = 4096`) | clean (new file) |
| `src/opendisplay/protocol/commands.py` | Adds `LAN_CHUNK_SIZE = 4094`; `build_direct_write_data_command(..., max_data_len=None)` | auto-merged |
| `src/opendisplay/device.py` | `lan_address` kwarg + validation, LAN branch in `__aenter__`, `_conn` type widened, `_data_chunk_limit()`, both `0x0071` loops use it | **2 conflicts** (§4.3) |
| `src/opendisplay/discovery.py` | `discover_lan_devices()` (zeroconf browse of `_opendisplay._tcp.local.`), `msd_bytes_from_mdns_txt_properties()` (28-hex-char `msd` TXT → 14-byte MSD) | auto-merged |
| `src/opendisplay/cli.py` | `--device HOST:PORT` routing, `scan --lan`, `listen --lan` | auto-merged |
| `src/opendisplay/__init__.py` | Exports `discover_lan_devices`, `LAN_CHUNK_SIZE` | **1 conflict** (§4.1) |
| `src/opendisplay/protocol/__init__.py` | Re-export `LAN_CHUNK_SIZE` | auto-merged |
| `src/opendisplay/transport/__init__.py` | Export `LANConnection`, `LAN_MAX_FRAME_PAYLOAD` | auto-merged |
| `tests/unit/test_protocol_commands.py` | 2 LAN chunk tests + import | **1 conflict** (§4.2) |
| `pyproject.toml` / `uv.lock` | Optional dep group `lan = ["zeroconf>=0.132.0"]` | auto-merged |

---

## 3. Why the conflicts exist: what main gained since PR #89's base

Between merge-base `9617757` and `742adad`, main absorbed the hardening series (PRs #93–#118).
The parts that intersect PR #89:

- **Command serialization / reentrancy**: `@_serialized` decorator + reentrant `_transaction()`
  (`asyncio.Lock` + owner tracking) on all public command methods. PR #89's LAN `__aenter__`
  calls `authenticate()`/`interrogate()` which are now `@_serialized` — safe as-is (the
  reentrancy guard prevents deadlock), but the merge must keep both method blocks → conflict.
- **Session hygiene**: `_clear_session()` on disconnect + `_on_ble_disconnect` callback so an
  unexpected BLE drop forgets the dead AES session. Inserted at the same anchor PR #89 inserts
  `_data_chunk_limit()` → conflict.
- **Partial-upload fallback**: `_send_partial_chunks` now returns `"success"`/`"fallback_full"`
  instead of `None` (NACK mid-stream → fall back to full upload). PR #89 edits the same method's
  first body line (chunk-size source) → conflict.
- **START-payload plumbing** (`max_start_payload` parameter, C4): call sites that did not exist
  at PR #89's base — source of latent gap G1 (§5).
- **BLE notification-queue hardening** (#98/C6): `drain_notifications()` before each write.
  No textual conflict, but the LAN analogue is missing — latent gap G3 (§5).
- CRC-16/CCITT config CRC (#117), `IntegrityCheckError` 0xFF frame (#118), ZIPXL/streaming
  compression gating (#114), lazy compression (#95/#113): all transport-neutral; no LAN
  interaction beyond the `__init__.py` export-line collision.

---

## 4. Conflict resolution (3 files, verified via `git merge-tree --write-tree 742adad 6e00434`)

### 4.1 `src/opendisplay/__init__.py` — keep both sides

```
<<<<<<< 742adad (main)
from .partial import PartialState
from .protocol import MANUFACTURER_ID, SERVICE_UUID
=======
from .protocol import LAN_CHUNK_SIZE, MANUFACTURER_ID, SERVICE_UUID
>>>>>>> 6e00434 (PR 89)
```

Resolution:

```python
from .partial import PartialState
from .protocol import LAN_CHUNK_SIZE, MANUFACTURER_ID, SERVICE_UUID
```

All `__all__` additions (`"discover_lan_devices"`, `"LAN_CHUNK_SIZE"`, `"PartialState"`,
`"IntegrityCheckError"`, `"DataExtended"`) auto-merge.

### 4.2 `tests/unit/test_protocol_commands.py` — keep both imports

```
    CHUNK_SIZE,
<<<<<<< 742adad
    CONFIG_CHUNK_SIZE,
=======
    LAN_CHUNK_SIZE,
>>>>>>> 6e00434
    CommandCode,
```

Resolution: keep `CONFIG_CHUNK_SIZE` **and** `LAN_CHUNK_SIZE`. Both sides' test bodies
(main's `TestWriteConfigChunking`, PR 89's `test_build_direct_write_data_command_lan_*`)
auto-merge.

### 4.3 `src/opendisplay/device.py` — two hunks

**Hunk A** (method block after the `_conn` property, which itself auto-merges to
`-> BLEConnection | LANConnection`): main contributes `_transaction()`, `_clear_session()`,
`_on_ble_disconnect()`; PR 89 contributes `_data_chunk_limit()`. Disjoint methods inserted at
the same anchor. Resolution: **keep all four**.

**Hunk B** (`_send_partial_chunks` signature + first body line):

```
<<<<<<< 742adad
    ) -> str:
        """... Returns "success", or "fallback_full" if the device NACKed ..."""
        chunk_size = ENCRYPTED_CHUNK_SIZE if self._session_key is not None else CHUNK_SIZE
=======
    ) -> None:
        """Send remaining 0x71 chunks and update upload progress."""
        chunk_size = self._data_chunk_limit()
>>>>>>> 6e00434
```

Resolution: **main's signature and docstring** (`-> str`, "success"/"fallback_full" fallback
semantics) + **PR 89's chunk-size line** (`chunk_size = self._data_chunk_limit()`). The body
below the marker auto-merges correctly, combining main's NACK→`return "fallback_full"` logic
with PR 89's `max_data_len=chunk_size`.

Note: PR 89's *second* chunk-size edit — the full-upload `_send_data_chunks` loop — auto-merges
on its own (main left that line untouched), so **both** `0x0071` loops end up LAN-aware.

These resolutions are identical to reflog commit `b22bc0d` (a prior cherry-pick of `6e00434`
onto `742adad`), which was independently verified correct and complete for the mechanical
conflicts.

---

## 5. Firmware protocol validation

Every library-side assumption checked against the ESP32 firmware
(`/home/davelee/opendisplay/Firmware`, on `main`).

| # | Library assumption (PR 89) | Firmware ground truth | Verdict |
|---|---|---|---|
| V1 | TCP port 2446 default | `uint16_t wifiServerPort = 2446` (`src/main.h:152`); overridable via `wifi_config` TLV bytes [64..65] (`src/config_parser.cpp:487-490`) | ✅ match |
| V2 | 2-byte LE length-prefixed frames, both directions | RX parse `flen = buf[0] \| (buf[1]<<8)` (`src/wifi_service.cpp:230`); TX `send_wifi_lan_frame` writes same header (`src/communication.cpp:73-81`) | ✅ match |
| V3 | `LAN_CHUNK_SIZE = 4094` data bytes per `0x0071` | `WIFI_LAN_MAX_PAYLOAD = 4096` inner budget, frames `flen > 4096` rejected + connection closed (`src/wifi_service.cpp:16-18,231`); 4096 − 2 opcode bytes = 4094 | ✅ correct, 0 headroom wasted |
| V4 | `LAN_MAX_FRAME_PAYLOAD = 4096` accepted on RX | Responses are ≤ 4096 payload by the same constant | ✅ match |
| V5 | Host must frame-reassemble RX (TCP coalescing/splitting) | Firmware reassembles RX per-frame with `memmove` buffer (`src/wifi_service.cpp:229-246`); `LANConnection.read_response` uses `readexactly` | ✅ match |
| V6 | Encryption works over TCP | Transport-agnostic: LAN bytes enter the same `imageDataWritten` dispatcher (`src/wifi_service.cpp:239`); auth `0x0050` + fw-version `0x0043` allowed clear, rest NACKed `0xFE` when unauthenticated (`src/communication.cpp:479-497`); encrypted frames `[cmd:2][nonce:16][ct][tag:12]` (`src/communication.cpp:499-541`) | ✅ `_data_chunk_limit()` correctly prefers 154-byte encrypted budget over 4094 |
| V7 | mDNS discovery: `_opendisplay._tcp`, TXT `msd` (28 hex chars) | `MDNS.addService("opendisplay","tcp",port)` + `addServiceTxt(...,"msd",hex)`; hostname `OD<chipid>` (`src/wifi_service.cpp:51-83`) | ✅ matches `discover_lan_devices` + `msd_bytes_from_mdns_txt_properties` |
| V8 | Persistent connection, request/response | Single client; new connection **evicts** the old and **clears the encryption session** (`src/wifi_service.cpp:163,187-199,204-206`); 30 s socket read timeout; no keepalive, nothing unsolicited | ⚠️ client must re-auth after any (re)connect — PR 89 does auth in `__aenter__`, so per-context-manager use is fine |
| V9 | Config read over LAN | `0x0040` reply still arrives as multiple ~100-byte chunk frames (`src/communication.cpp:319-358`) — not one big frame | ✅ existing multi-chunk config parser handles it |
| V10 | Responses only to LAN | Responses are dual-delivered to LAN **and** BLE notify queue when a central is connected (`src/communication.cpp:136-137,202-203`) | ℹ️ harmless for a LAN-only client |
| V11 | WiFi enablement | `communication_modes` bit 2 (`COMM_MODE_WIFI`, `src/main.h:64`) **and** parsed `wifi_config` TLV type `0x26` with non-empty SSID (`src/config_parser.cpp:425-524`, `src/wifi_service.cpp:88-104`); BLE + WiFi concurrent (`src/main.cpp:146-169`) | ✅ documented |

`CONFIG_CHUNK_SIZE = 200` (write-config path) is firmware-fixed (`ceil(total/200)` accounting)
and transport-independent — PR 89 correctly leaves it alone.

---

## 6. Gaps: PR 89 vs. main's hardening (not merge conflicts — latent issues)

### G1 — START-payload sites not LAN-aware (severity: low; throughput only)

`device.py:1615` (`_maybe_upload_partial`, partial START `0x76`) and `device.py:1701`
(`_execute_upload`, compressed START `0x70`) still cap the initial payload at
`ENCRYPTED_CHUNK_SIZE if session else MAX_START_PAYLOAD` (200 bytes). These call sites didn't
exist at PR 89's merge-base. Not a correctness bug (200 ≤ 4096 is always a valid LAN frame;
the remainder streams as LAN-sized `0x0071` chunks) — purely extra round-trips.

**Proposed fix:** add `_start_payload_limit()` beside `_data_chunk_limit()`:
session → `ENCRYPTED_CHUNK_SIZE`; LAN → LAN frame budget **minus the fixed START header**
(size field / 17-byte partial header live inside the same 4096 budget — plain `LAN_CHUNK_SIZE`
is *not* reusable here); else `MAX_START_PAYLOAD`. Thread through the builders' existing
`max_start_payload` parameter.

### G2 — no session-clear on unexpected TCP drop (severity: medium)

BLE registers `disconnected_callback=self._on_ble_disconnect` so a dropped link forgets the
dead AES session. `LANConnection` has no such hook, while the firmware clears its session
server-side on client eviction/disconnect (`wifi_service.cpp:163,191,204,206`). A long-lived
device object whose socket drops mid-session would keep encrypting against a session the
firmware no longer has.

**Proposed fix:** surface EOF/`ConnectionError` from `LANConnection` reads/writes and clear the
session at the device layer (e.g. a `disconnected_callback` on `LANConnection` wired to an
`_on_lan_disconnect` mirroring `_on_ble_disconnect`).

### G3 — no stale-frame drain after read timeout (severity: medium)

If `LANConnection.read_response` times out mid-reply, the late bytes remain in the TCP stream
and the *next* command's `read_response` parses them as its own reply — a frame-shift desync,
the exact LAN analogue of the BLE notification-queue bug fixed on main (#98/C6, BLE's
`drain_notifications()` before each write).

**Proposed fix (preferred):** force reconnect after a response timeout — the firmware evicts
and session-clears on reconnect anyway, so this is the simplest firmware-aligned recovery.
Alternative: mark the stream dirty on timeout and drain buffered bytes before the next
`write_command`.

---

## 7. Merge procedure (for execution when approved)

1. On `feature/wifi`, clean at `742adad`:
   ```
   git cherry-pick 6e00434
   # resolve the 3 conflicts exactly per §4
   git add -A && git cherry-pick --continue
   ```
   Preserves Jonas Niesner's authorship. (Reflog commit `b22bc0d` is exactly this resolution on
   the same parent and could be restored directly with `git reset --hard b22bc0d`.)
2. Optional follow-up commits for gaps G1–G3 (§6).
3. Verification:
   - `uv sync --group dev && uv run pytest -q` — full suite green, including PR 89's
     `test_build_direct_write_data_command_lan_*` and main's `TestWriteConfigChunking`.
   - Import smoke check: `python -c "import opendisplay; opendisplay.LAN_CHUNK_SIZE; opendisplay.discover_lan_devices"`.
   - `uv run ruff check` / `uv run mypy` per repo dev tooling.
   - Optional live test with a WiFi-enabled device: `opendisplay scan --lan` (expect the mDNS
     `_opendisplay._tcp` entry), then `--device HOST:2446` firmware-version read and an image
     upload.
