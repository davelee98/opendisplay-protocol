# BLE Image-Write Throughput — Findings & Proposals (2026-07-07)

Research into why image uploads ("imagewrite" = the DIRECT_WRITE protocol, `0x0070`/`0x0071`/`0x0072`) are slow over BLE, and how to pipeline data frames with batched ACKs to keep the transport saturated. Covers all three repos plus the protocol spec:

- **py-opendisplay** — Python client (bleak)
- **Firmware** — ESP32 (NimBLE) + nRF52840 (Bluefruit) targets, one tree
- **Firmware_Silabs** — BG22 (Silicon Labs `sl_bt` BGAPI)
- **Home_Assistant_Integration** — delivery path, proxies, deadlines
- Spec: `opendisplay.org/httpdocs/protocol/ble-flow.html`

**Status: research only — no code changes made.** File:line references were verified against source during this research; the most load-bearing ones (`commands.py:51`, `device.py:1792-1803`, `main.cpp:171-177`, `opendisplay_pipe.c:437-447`) were re-read directly.

---

## Executive summary

The bottleneck is exactly as hypothesized: the wire protocol is **strict stop-and-wait**. Every DATA chunk carries at most 230 bytes (154 with session encryption), is sent as a GATT **write-with-response**, and the client then blocks until the firmware sends a 2-byte **ACK notification** before sending the next chunk. Each chunk therefore costs multiple BLE connection intervals — and, when routed through an ESPHome BLE proxy, a WiFi round trip on top. The radio idles ~90–95% of the time; effective throughput is ~1.2–10 KB/s. A 24 KB compressed image takes 8–15+ s to transfer; a 48 KB uncompressed image via proxy can take 26–42 s and blow the HA integration's 30 s delivery deadline.

Two protocol methods are proposed, deliberately sharing one firmware-side wire mechanism so they can ship in stages:

- **Method A — credit/window lockstep with batched ACK**: send W frames via write-without-response, one cumulative ACK per window. ~3–7× faster.
- **Method B — continuous sliding-window streaming (TCP-like)**: same firmware protocol, but the client never drains the pipe — ACKs refresh credit asynchronously. ~8–15× faster; a client-only upgrade once Method A firmware exists.
- **Method C — complementary no-protocol-change optimizations** (connection-interval request, MTU-derived chunks, removing an nRF `delay(20)`, ESP32 loop drain, Silabs notify-retry) worth ~1.5–3× on their own and compounding with A/B.

Recommended path: Stage 0 = Method C quick wins → Stage 1 = Method A → Stage 2 = Method B (client-only). Details in §8.

---

## 1. Current state

### 1.1 Wire protocol (per spec and both firmware implementations)

One GATT service + one characteristic, both UUID `0x2446`, properties READ / NOTIFY / WRITE / WRITE_NR. The client writes commands to the characteristic; the device replies via **notifications** on the same characteristic (no indications anywhere).

Image transfer:

| Frame | Layout | Notes |
|---|---|---|
| START `0x0070` | compressed: `[00 70][uncompressed_size:4 LE][first zlib bytes]`; uncompressed: `[00 70]` | zlib stream must use `window_bits=9` (512 B window) |
| DATA `0x0071` | `[00 71][data ≤230 B]` | **no sequence number, offset, length field, or CRC** |
| END `0x0072` | `[00 72][refresh_mode:1][optional new_etag:4 BE]` | triggers panel refresh |
| Refresh result | notification `{0x00,0x73}` success / `{0x00,0x74}` timeout | async, up to ~60 s later |
| PARTIAL START `0x0076` | 17-byte header (flags, etags, x/y/w/h) | ESP32/nRF only — not implemented on Silabs |

**Every command — including every single DATA chunk — gets a 2-byte notification ACK** `{status, opcode_low}`: `0x00` = OK, `0xFF` = error (partial path adds a 4-byte NACK `{0xFF, opcode, err, 0x00}`). Ordering relies entirely on ATT reliability; end-to-end integrity relies on zlib's Adler-32 (compressed path only) and the AES-CCM tag when a session is active. Uncompressed transfers have no integrity check at all.

### 1.2 Client behavior (py-opendisplay)

- **Stop-and-wait send loop** — `_send_data_chunks` (`device.py:1770-1828`): write one chunk, block for its ACK, repeat:
  ```python
  while bytes_sent < len(image_data):
      chunk_data = image_data[bytes_sent : bytes_sent + chunk_size]
      await self._write(build_direct_write_data_command(chunk_data))   # device.py:1796
      ...
      response = await self._read(timeout)                             # device.py:1803 — blocks for ACK
  ```
- **Every write is write-with-response**: `write_gatt_char(..., response=True)` (`connection.py:306-310`) — the only `write_gatt_char` call in the library. So each chunk pays an ATT round trip *and* an ACK-notification round trip.
- **Fixed chunk sizes, no MTU negotiation**: `CHUNK_SIZE = 230`, `ENCRYPTED_CHUNK_SIZE = 154` (`commands.py:47-48`); `client.mtu_size` is never read.
- **`PIPELINE_CHUNKS = 1  # Wait for ACK after each chunk`** (`commands.py:51`) — the constant exists, is exported, and is never used. The intended fix was already anticipated.
- **No request/response correlation**: ACKs are read off a plain `asyncio.Queue` (`connection.py:314-339`); `drain_notifications()` discards stale frames before each write. Correctness relies purely on stop-and-wait ordering.
- **No per-frame retry**: an ACK timeout (`TIMEOUT_ACK = 5.0`; 90 s variants for slow Spectra/ACeP panels that block SPI ~60 s) aborts the whole upload.
- The whole mechanism is encapsulated below `upload_prepared_image`/`upload_image` in `_execute_upload` → `_send_data_chunks` plus the transport `write_command`/`read_response` — **the HA-facing API would not change** under any proposal here.

### 1.3 Firmware receive paths

**ESP32 (NimBLE, async):** the `onWrite` callback enqueues each frame into a **5-slot × 512 B command ring buffer** (`esp32_ble_callbacks.h:13-26, 93-121`); on overflow the frame is **silently dropped** (`esp32_ble_callbacks.h:106-114`) — invisible even to a write-with-response client, because the stack (not the app) generates the ATT Write Response. The main loop processes **one command per iteration** (`if`, not `while` — `main.cpp:171-177`) with `delay(1)` when BLE-active, and per-chunk `writeSerial` logging on both enqueue and process. ACKs go into a 10-slot response ring flushed by the main loop as notifications, drain capped at 16/loop (`main.cpp:178-189`). MTU 512 is requested (`ble_init.cpp:214-215`). DATA bytes are written straight to the panel (blocking SPI `bbepWriteData`) or through the zlib streamer (2048 B scratch).

**nRF52840 (Bluefruit):** same protocol, processed inline in the BLE task; **`delay(20)` after every notify** (`communication.cpp:217`) — a flat 20 ms tax on every chunk ACK. `BANDWIDTH_MAX` configured.

**Silabs BG22 (`sl_bt`):** fully synchronous — the GATT write event handler decompresses/writes to the panel inline, then sends the ACK inline. `pipe_send_raw` (`opendisplay_pipe.c:437-447`) **drops the ACK with only a log line** if `sl_bt_gatt_server_send_notification` fails (TX buffer full) — a live ACK-loss bug that would abort an upload via client timeout. Max frame `OD_PIPE_MAX_PAYLOAD = 244`; decompression scratch only 256 B; stack buffer `SL_BT_CONFIG_BUFFER_SIZE = 3150` (≈10–12 buffered max-size write events). Static DLE 251 config; no partial-update support.

**Connection parameters:** no firmware requests a connection interval, PHY, or (except Silabs' static DLE) data-length extension. The interval is whatever the central picks.

### 1.4 HA integration context

- All connections resolve through HA's bluetooth stack (`async_ble_device_from_address(..., connectable=True)`), so the link may be an **ESPHome BLE proxy** — every GATT round trip then also crosses WiFi/TCP.
- `DELIVERY_DEADLINE_S = 30.0` wraps the whole queued drain **including connect and transfer** (`delivery.py:59-61, 285`). Large uncompressed transfers via proxy can exceed it today (§2).
- Capability negotiation precedent: the client reads the Flex config (`0x0040`) and adapts to `transmission_modes` bits (`streaming_decompression`, `zip`) plus firmware version (`0x0043`). **There is no protocol-version handshake** — new features must be advertised as capability bits with a fallback.

---

## 2. Throughput model of the current protocol

One DATA chunk = write-with-response → firmware main-loop processing → ACK notification → client resumes. BLE exchanges packets only at connection events (one per connection interval, CI), so every round trip is quantized in CI units:

```
T_chunk(direct HCI)   ≈ n_ce × CI + t_fw
   n_ce ≈ 2–3   (write flush ~1 CI; ATT Write Response same/next event;
                 ACK notification next event after main-loop flush; avg ≈ 2.5)
   t_fw ≈ 2–6 ms (SPI ~0.3 ms; uzlib inflate 1–3 ms; serial logging 2–15 ms on UART builds)

T_chunk(ESPHome proxy) ≈ RTT_wifi(write) + n_ce_p × CI_p + RTT_wifi(notify) + t_fw
   RTT_wifi ≈ 10–50 ms per leg (good WLAN 10–20 ms; congested/mesh 50–150 ms)
   CI_p     ≈ 12–15 ms (typical ESP32 proxy negotiation)
```

Payload per chunk: 230 B plaintext, 154 B encrypted (×0.67 on all figures below). nRF adds a flat +20 ms/chunk from `delay(20)`.

| Scenario | CI | T_chunk | Throughput | 24 KB compressed | 48 KB uncompressed |
|---|---|---|---|---|---|
| Direct, fast CI | 7.5 ms | ~23 ms | ~10.0 KB/s | ~2.5 s | ~4.9 s |
| Direct, common CI | 15 ms | ~42 ms | ~5.5 KB/s | ~4.5 s | ~9.0 s |
| Direct, BlueZ default-ish | 30 ms | ~79 ms | ~2.9 KB/s | ~8.5 s | ~17 s |
| Direct, conservative CI | 50 ms | ~129 ms | ~1.8 KB/s | ~13.7 s | ~27 s |
| Proxy, good WiFi | 15 ms | ~70–90 ms | ~2.6–3.3 KB/s | ~7.5–10 s | ~15–20 s |
| Proxy, mediocre WiFi | 15 ms | ~120–200 ms | ~1.2–1.9 KB/s | ~13–21 s | ~26–42 s |

**Takeaways:** a representative 24 KB compressed image (~107 chunks) takes 8–15 s on typical paths, before connect (1–3 s) and refresh (2–20 s). A 48 KB uncompressed transfer through a proxy on mediocre WiFi **exceeds `DELIVERY_DEADLINE_S = 30` including connect** — the observed failure mode. The protocol is RTT-bound, not bandwidth-bound.

---

## 3. Verified enablers and constraints

These were verified against source during this research and shape the proposals:

1. **All three firmware stacks already accept write-without-response (WNR) on `0x2446`.** ESP32: `PROPERTY_WRITE | PROPERTY_WRITE_NR` (`ble_init.cpp:238-239`), and the write callback fires for both write types on NimBLE and Bluedroid. nRF: `BLEWrite | BLEWriteWithoutResponse | BLENotify` (`main.h:361`). Silabs: `on_pipe_write` explicitly accepts `sl_bt_gatt_write_command` (`opendisplay_pipe.c:1110-1112`). **No GATT-table changes needed anywhere — WNR is purely a client-side switch.**
2. **bleak (BlueZ) `response=False`** issues D-Bus `WriteValue type=command`; backpressure is kernel ACL + link-layer flow control. Per-write overhead is a local D-Bus round trip (~0.2–1 ms) — never the bottleneck. No data can be lost between client and the device's host stack (LL retransmits; the receiver withholds LL ACKs when its buffers fill).
3. **ESPHome proxy WNR is fire-and-forget over TCP** (`aioesphomeapi` sends the write with no round trip). The one real loss point: if the **proxy's own GATT write fails under congestion, the frame is dropped silently** — the API defines no error message for no-response writes. This makes app-layer loss *detection* mandatory (sequence byte + cumulative counts below), even though loss is rare.
4. **Precise loss-point analysis for WNR:** the link layer and ATT cannot lose or reorder data. Losses can only occur at (a) the ESP32 5-slot app ring (silent overflow), (b) the ESPHome proxy under congestion, (c) Silabs stack-buffer exhaustion if too many write events are buffered, and (d) the Silabs ACK TX drop bug (§1.3). All are bounded or fixed by the proposals.
5. **Encryption caps the window at W ≤ 32.** The ESP32/nRF replay check rejects nonce counters more than ±32 from the last seen (`encryption.cpp:128-134`); the client increments its counter once per frame. ATT keeps frames in order, so pipelining is safe as long as W ≤ 32. *(Side finding: the Silabs decrypt path performs no replay-window check at all — `opendisplay_pipe.c:358-394` — worth an independent fix.)*
6. **START `0x0070` cannot carry new negotiation fields compatibly**: old firmware treats any START payload ≥ 4 bytes as compressed and feeds `payload+4` straight into zlib (`display_service.cpp:1489, 1541-1544`). Negotiation must live in a **new opcode**.
7. **Unknown opcodes are ignored with no response on all three firmwares** (`communication.cpp:611-614`; `opendisplay_pipe.c:1093-1095`), so a probe of a new opcode must be capability-gated and use a short timeout.
8. **HA integration needs no change** for any proposal: everything lives below `upload_prepared_image`. The proposals bring transfers an order of magnitude inside `DELIVERY_DEADLINE_S` rather than requiring it to move.
9. **The wire protocol's only backpressure is the per-frame ACK.** Stop-and-wait means the client is structurally blocked from sending frame N+1 until the firmware ACKs frame N, and the firmware defers that ACK while it is busy (blocking panel SPI, zlib inflate). So one 2-byte notification does double duty — receipt confirmation *and* flow control. There is no window, credit, or receive-buffer advertisement in the protocol. Transport layers below it also apply backpressure (ATT write-with-response gates one outstanding write; the link layer withholds LL ACKs when the receiver's controller RX buffers fill; TCP on the HA→proxy hop), but those protect the *radio*, not the *application*. **Critical asymmetry:** on ESP32, `onWrite` only memcpys each frame into the app ring and returns immediately (`esp32_ble_callbacks.h:106-114`), so the controller/mbuf layer drains fast, LL never asserts backpressure, and the app ring overflows *silently* — LL flow control is decoupled from the app's real consumption rate. On nRF, `imageDataWritten` runs **inline and blocking** in the write callback (`ble_init.cpp:104`), so the SoftDevice event queue backs up → RX buffers fill → LL flow control paces the sender automatically. Consequence for pipelining: removing the per-frame ACK deletes the only application-layer backpressure the protocol has; the negotiated **credit window IS its replacement**, and must be clamped to the receiver's real buffer capacity (nRF keeps LL backpressure as a safety net; ESP32 and the proxy path have none, so the window is the sole guard).
10. **HA caps every frame at 244 bytes and offers no long writes** (verified against `../core`, `habluetooth==6.26.5`). Modern HA connects local adapters with habluetooth's native L2CAP client `HaMgmtClient`, which requests `PREFERRED_MTU = 247`, caps the negotiated MTU at 247 (`channels/att.py:93, 372`), exposes max write = MTU − 3 = **244 B** (`client_mgmt.py:332-333`), and **rejects any ATT write > 244 B with `BleakError` — no Prepare/Execute long-write fallback** (`channels/att.py:422-432`). Consequences: (a) `max_frame ≤ 244` for **all** platforms over HA, including the ESP32 whose 512-byte buffer is unreachable through HA; (b) the current ~232-byte frames fit with ~12 B headroom, so nothing breaks; (c) any MTU-derived-chunk optimization is worth little on plaintext (230→242, +5%) but meaningful on encrypted transfers (154→213, +38%, since the AES-CCM envelope adds 31 fixed bytes). The legacy bleak/BlueZ-D-Bus path reaches MTU 517 with long writes, and the ESPHome-proxy path uses the proxy-negotiated MTU (`aioesphomeapi` `BluetoothDeviceConnectionResponse.mtu`), but the 244-byte native ceiling is the one to design against.

---

## 4. Shared wire machinery (used by both Methods A and B)

Both methods use the same negotiation opcode, framing change, and ACK format. This is the single most load-bearing design decision: **one firmware implementation supports both client pacing strategies**, so Method B ships later as a client-only release against Method A firmware — decoupling the risky client-side evolution from firmware rollout across three embedded platforms.

### 4.1 Negotiation — new opcode `0x0078 TRANSFER_PARAMS`

Sent write-with-response (encrypted when a session is active), after capability discovery, before START. Per-connection state, reset on disconnect.

```
Client → device:
  [0x00][0x78][ver:1 = 0x01][requested_window:1][requested_ack_every:1][client_max_frame:2 LE]

Device → client (ACK notification):
  [0x00][0x78][ver:1][granted_window:1][ack_every:1][max_frame:2 LE][flags:1]
    granted_window W : max unacknowledged DATA frames in flight (firmware-clamped)
    ack_every      N : firmware emits one cumulative ACK per N processed frames (N ≤ W)
    max_frame        : largest total 0x0071 packet accepted in windowed mode
    flags            : bit0 = go-back-N resume supported (reserved for v2)

Old firmware: no reply (unknown opcode) → client falls back after a short probe timeout.
NACK {0xFF, 0x78}: windowed mode refused → legacy stop-and-wait.
```

Firmware clamps per platform:

| Platform | Granted W (v1) | Rationale | max_frame |
|---|---|---|---|
| ESP32 | **4 today**, 16 after refactor (32 hard max) | app command ring holds `SIZE−1 = 4` (5×512 B, `esp32_ble_callbacks.h:13-17`) and **silently drops on overflow** (`:114`); main loop drains 1/iter (`main.cpp:171-177`). Depth > 4 requires drain-all-per-loop + a larger ring (shrink slot to 256 B since HA caps at 244 → 32 slots = 8 KB). NimBLE itself flow-controls (mbuf backpressure), so the ring is the only drop point | **244 over HA** (see below); 510 only on a raw non-HA BlueZ link |
| nRF52 | 12 useful (32 hard max) | **no app ring** — `imageDataWritten` runs inline in the SoftDevice write callback (`ble_init.cpp:104`); SoftDevice RX buffers (`configPrphBandwidth(BANDWIDTH_MAX)`, `ble_init.cpp:90`) + LL flow control mean **no silent drop at any depth** — the link self-paces to processing speed. Depth only needs to hide one ACK RTT (~8–12); limiter is inline blocking SPI/zlib, not buffer depth | 244 (ATT MTU 247) |
| Silabs BG22 | 4–6 | stack buffer 3150 B ≈ 10–12 buffered 244 B write events; leave TX headroom | 244 (`OD_PIPE_MAX_PAYLOAD`) |
| Any, encrypted | min(above, **32**) | replay window ±32 | — |

> **HA MTU ceiling (verified against the HA core checkout at `../core`, `habluetooth==6.26.5`):** modern HA connects local adapters with habluetooth's own native L2CAP GATT client (`HaMgmtClient`, `client_mgmt.py`), which requests `PREFERRED_MTU = 247` and caps the negotiated MTU at 247 (`channels/att.py:93, 372`) → **max ATT write = MTU − 3 = 244 bytes** (`client_mgmt.py:332-333`). Crucially it **does not implement ATT long writes** — any value > 244 bytes raises `BleakError` (`channels/att.py:422-432`), so there is no automatic splitting to fall back on. The ESPHome-proxy path uses whatever MTU the proxy negotiates and reports (`aioesphomeapi` `BluetoothDeviceConnectionResponse.mtu`); the legacy bleak/BlueZ-D-Bus path can reach the spec max 517 with long writes, but habluetooth 6.26.5 prefers the native client. **Bottom line: assume a 244-byte per-frame ceiling for any HA-delivered transfer, `max_frame ≤ 244` for all platforms including ESP32.** The current ~232-byte frames already fit, with ~12 bytes of headroom.

### 4.2 Capability discovery

Two cheap gates, both already fetched per session:

1. New Flex `transmission_modes` bit **`0x10` = windowed transfer** (bits 0x01/02/04/08/80 are taken) → `DisplayConfig.supports_windowed_transfer`, following the `streaming_decompression` precedent exactly.
2. Firmware version (`0x0043`) ≥ the introducing release, as belt-and-braces for stored configs predating a firmware flash.

If either gate passes, probe `0x0078` with a **2 s** timeout (not the 5 s `TIMEOUT_ACK`); on timeout/NACK, fall back to legacy stop-and-wait. Worst case against a lying config bit: one 2 s penalty, once per connection.

### 4.3 Windowed DATA frame — 1-byte sequence number

Only after successful negotiation (firmware keys the layout off per-connection state; legacy layout otherwise):

```
[0x00][0x71][seq:1][data ≤ max_frame − 3]
encrypted: seq is the first plaintext byte, inside the AES-CCM envelope (authenticated)
```

`seq` = frame counter mod 256, starting at 0 after START. Overhead ≤ 0.5%. Purpose: **immediate gap detection on the firmware side** — the only way to know the bytes fed to zlib/panel SPI are contiguous if a frame was silently dropped (ESP32 ring overflow, proxy congestion). Without it, a mid-window drop feeds the decompressor a spliced stream: the compressed path fails only at END (Adler-32) and the uncompressed path paints garbage and hangs until the 15-minute watchdog.

### 4.4 Cumulative ACK / NACK

```
ACK  (one per N processed frames, notification):
  [0x00][0x71][next_seq:1][received_total:4 LE]
     received_total = DATA payload bytes accepted since START (incl. bytes inlined in START)

NACK (immediate, on gap / zlib error / write failure):
  [0xFF][0x71][err:1][expected_seq:1][received_total:4 LE]
     err: 0x01 seq gap, 0x02 zlib error, 0x03 write/overflow error, …
```

Both are length-distinguishable from every existing frame (legacy 2-byte ACK, 3-byte status, 4-byte partial NACK). The client cross-checks `received_total` against bytes sent — converting every silent loss into a detected, attributable failure at the next window boundary at the latest. Parsers (both directions) must tolerate trailing bytes so a future `credits` field can be appended without another protocol rev.

**END validation is strengthened in windowed mode:** uncompressed transfers NACK END if `received_total` ≠ expected panel byte count (today only the two-plane paths check totals); compressed transfers already fail at zlib finalize.

### 4.5 Error/abort semantics (v1)

- **Firmware** on gap/error: enter error state, discard further DATA for this transfer, emit one NACK, await a new START (which already resets state) or disconnect.
- **Client** on NACK, `received_total` mismatch, or window-ACK timeout: abort and restart the upload. Window timeout = `max(TIMEOUT_ACK, existing per-path chunk timeout)` — the 90 s slow-panel budget must survive, because a Spectra panel blocking 60 s in SPI stalls window ACKs exactly as it stalls per-chunk ACKs today. The credit mechanism inherently stops the client after W frames, so a blocked device is never flooded.
- **Go-back-N resume** (rewind to `received_total`, resend from `expected_seq`) is wire-compatible with this format and reserved behind the v2 flag — v1 keeps abort-and-restart because losses should be rare once in-flight is bounded.

---

## 5. Method A — Credit/window lockstep with batched ACK

### 5.1 Operation

The client sends up to **W** windowed DATA frames via **write-without-response**, then blocks until the cumulative ACK for the window (`N = W`), verifies `received_total`, and sends the next window. START/END/`0x0078` remain write-with-response. Net effect: one ACK round trip per W frames instead of per frame, and the per-frame ATT write-response round trips disappear entirely.

### 5.2 Expected throughput

```
T_window(direct) ≈ ceil(W/p) × CI + 2 × CI + max(0, W × t_fw − radio time)
   p = ATT packets per connection event ≈ 3–5 with DLE (nRF BANDWIDTH_MAX: 6–7)
T_window(proxy)  ≈ ceil(W/p) × CI_p + RTT_wifi(round trip) + processing residue
```

| Scenario | W | T_window | Payload/window | Throughput | vs today |
|---|---|---|---|---|---|
| Direct CI=30, p=4 | 8 | ~130 ms | 1840 B | ~14 KB/s | ~4.9× |
| Direct CI=30, p=4 | 16 | ~200 ms | 3680 B | ~18 KB/s | ~6.3× |
| Direct CI=15, p=4 | 8 | ~70 ms | 1840 B | ~26 KB/s | ~4.8× |
| Proxy, good WiFi | 8 | ~130 ms | 1840 B | ~14 KB/s | ~4–5× |
| Proxy, good WiFi | 16 | ~170 ms | 3680 B | ~21 KB/s | ~7× |
| Silabs, direct CI=30 | 4 | ~100 ms | 920 B | ~9 KB/s | ~3× |

24 KB compressed image: **~1.5–2.5 s** (from 8–15 s). Encrypted ×0.67. Real-world expectation: 50–70% of table values.

### 5.3 Per-repo change inventory

**py-opendisplay**
- `protocol/commands.py`: `build_transfer_params_command()`, `build_direct_write_data_windowed(seq, data)`; retire the dead `PIPELINE_CHUNKS`.
- `protocol/responses.py`: `parse_transfer_params_ack()`, `parse_window_ack()`/`parse_window_nack()`.
- `transport/connection.py`: `write_command_no_response()` (`response=False`, **no** per-frame `drain_notifications()` — drain once per window boundary); expose `mtu_size`.
- `device.py`: `_negotiate_transfer_params()` (gated per §4.2); `_send_data_chunks_windowed()` alongside the untouched legacy `_send_data_chunks`; routing in `_execute_upload`. Encrypted path: per-frame nonce counter unchanged; refactor `_write` so WNR frames share the encrypt step.
- `models/config.py`: `supports_windowed_transfer` (bit 0x10).
- **HA-facing API (`upload_prepared_image` etc.) unchanged.**

**Firmware (ESP32/nRF tree)**
- `communication.cpp`: `0x0078` handler → per-connection transfer-params state, reset on disconnect.
- `display_service.cpp` (`handleDirectWriteData`): windowed branch — seq check, frame counter, ACK-every-N via existing `sendResponse`, NACK-on-gap, END total validation.
- `esp32_ble_callbacks.h` / `main.cpp`: enlarge `COMMAND_QUEUE_SIZE` 5 → W+2 (17 × 512 B ≈ 8.7 KB static — fine on ESP32; nRF processes inline and doesn't use this ring); change the one-command `if` to a bounded `while` drain; gate per-chunk `writeSerial` logging behind a debug flag.
- nRF `sendResponse`: remove/condition the `delay(20)`.

**Firmware_Silabs**
- `opendisplay_pipe.c`: `0x0078` handler; windowed state; seq check + batched ACK in the DATA handler; **bounded retry (2–3 attempts) when `sl_bt_gatt_server_send_notification` returns `SL_STATUS_NO_MORE_RESOURCE`** instead of today's silent drop — with one ACK per window, TX pressure also drops ~W×.
- `config/sl_bluetooth_config.h`: optionally raise `SL_BT_CONFIG_BUFFER_SIZE` 3150 → ~5000 to grant W=8 (audit RAM headroom first).
- `opendisplay_protocol.h`: new opcode/response constants.

**Home_Assistant_Integration** — no required change; optionally bump the py-opendisplay requirement pin once released.

**Docs** — `ble-flow.html`: new "Windowed transfer (0x0078)" section; Flex docs: bit 0x10.

### 5.4 Compatibility matrix

| Client | Firmware | Behavior |
|---|---|---|
| new | old (no bit, version < X) | gates fail → legacy stop-and-wait; zero probes, zero regression |
| new | old but config bit wrongly set | `0x0078` probe → silence → one 2 s penalty → legacy fallback |
| old | new | never sends `0x0078` → firmware stays in legacy per-frame-ACK mode, byte-identical to today |
| new | new | windowed; W clamped (≤32 encrypted) |
| new | new, via proxy | works (proxy API supports WNR); seq + `received_total` covers proxy-side congestion drops |

### 5.5 Failure modes

| Segment | Can WNR data be lost? | Mechanism | Detection / recovery |
|---|---|---|---|
| Client → BlueZ → controller | No | D-Bus enqueue → kernel ACL flow control | n/a |
| Radio (link layer) | No | LL retransmission; receiver withholds ACKs when full | n/a |
| HA → ESPHome proxy → its controller | **Yes** | proxy-side GATT congestion failure unreported for WNR | seq gap → firmware NACK; `received_total` mismatch at window ACK; exposure bounded ≤ W |
| Device host stack | No (ESP32/nRF) / bounded (Silabs) | LL flow control; Silabs buffer-pool exhaustion prevented by W ≤ 6 clamp | W clamp; NACK path |
| Device app layer (ESP32 ring) | **Yes** (silent today) | ring overflow | prevented by W ≤ ring capacity + drain-all loop; residual gaps → seq NACK |
| ACK notification → client | **Yes** (Silabs today) | TX-buffer-full drop | window timeout → abort/retry; Silabs retry loop added |

### 5.6 Risks

- The ESP32 command ring is a `volatile`-index SPSC queue written from the NimBLE host task; higher burst rates widen exposure of any missing memory-barrier assumptions — needs careful review (or replacement with a FreeRTOS queue) during implementation.
- Slow-panel stalls (Spectra ~60 s in `bbepWriteData`) now stall a whole window rather than one chunk — same 90 s timeout class, and W frames sit buffered in device RAM during the stall (bounded, sized for).
- Encrypted W > 32 must be structurally impossible (clamped in both client and firmware), else replay-window rejections cascade.
- Silabs processes frames inside the BLE event loop; per-frame uzlib runs (256 B scratch → multiple polls) delay subsequent event handling. The W clamp covers this, but BG22 profiling before raising W is mandatory.

---

## 6. Method B — Continuous sliding-window streaming (TCP-like)

### 6.1 Operation

Identical wire format to Method A (`0x0078`, seq byte, cumulative ACK every N) but with **N < W** (e.g. N = W/2), and the client does **not** stop at window boundaries: it keeps sending while `frames_sent − frames_acked < W`, consuming ACK notifications asynchronously as credit refresh. The pipe never drains while waiting for an ACK; ACK latency is fully hidden as long as one ACK arrives per N frames sent.

**Firmware behavior is byte-identical to Method A** — count processed frames, ACK every N. Only the client pacing differs. This is why the §4 unification matters: Method B ships later as a **client-only release** against Method A firmware.

### 6.2 Expected throughput

Sliding-window throughput ≈ `min(link rate, W × frame_size / RTT, 1 / t_fw per frame)`:

| Scenario | Binding constraint | Throughput | vs today |
|---|---|---|---|
| Direct CI=30, p=4 | link rate (4 × 230 B / 30 ms) | ~30 KB/s | ~10× |
| Direct CI=15, p=4 | link rate / fw processing | ~40–60 KB/s | ~10–12× |
| Proxy RTT≈100 ms, W=16 | window (16 × 230 B / 0.1 s), link-capped | ~25–35 KB/s | ~10–15× |
| Silabs W=6, t_fw≈3 ms | fw processing | ~15–20 KB/s | ~5–7× |

24 KB compressed ≈ **0.8–1.5 s**. Method B's edge over A is largest exactly where the pain is worst — high-RTT proxy paths — because A still idles one full RTT per window while B overlaps it.

### 6.3 Client delta over Method A

- `_send_data_chunks_streaming()`: interleave sends with non-blocking ACK consumption (or a reader task); track credit; react to an out-of-band NACK immediately (stop sending, abort) rather than at a window boundary.
- The uncorrelated notification queue remains safe: during a windowed transfer the only expected inbound frames are window ACK/NACKs (length-distinguishable). `drain_notifications` must **not** run mid-stream (it would eat ACKs) — only at stream start.
- Timeout becomes "no ACK progress for T seconds" rather than a per-window timer.

### 6.4 Considered variant: firmware-paced dynamic credit grants

The ACK could carry a `credits:2 LE` field (grant = free buffer space) for true TCP-style firmware throttling. **Deferred**: static W with ACK-every-N achieves the same bound with far less firmware state, and the slow-panel case degrades gracefully anyway (no ACKs → client stops after W). The trailing-byte-tolerant ACK parsing rule (§4.4) leaves room to add credits later without breaking anything.

### 6.5 Everything else

Change inventory, compatibility, and failure modes are identical to Method A (§5.3–5.5) plus the streaming sender. Additional risk over A: burst pressure on the ESP32 ring is *sustained* rather than window-pulsed — the drain-all main-loop change stops being optional and becomes required; Silabs W stays ≤ 6 unless the buffer pool is enlarged.

---

## 7. Method C — Complementary optimizations (no protocol change) and rejected alternatives

Independent of A/B; several are worthwhile even for today's stop-and-wait protocol:

| Optimization | Change | Gain | Notes |
|---|---|---|---|
| Remove nRF `delay(20)` | `communication.cpp:217` conditional | −20 ms/chunk → −2.1 s per 107-chunk upload (~30–50% on nRF stop-and-wait) | trivial; ship immediately |
| MTU-derived chunk size | client uses `min(mtu−3, max_frame)` minus framing overhead | **small over HA**: plaintext 230 → 242 (+5%); encrypted 154 → 213 (+38%) | HA caps ATT writes at **244** (`habluetooth` native client, MTU 247, no long writes — see §4.1) and Silabs/nRF firmware caps at 244; the ESP32's 512-byte capacity is unreachable through HA. A 230→~500 B chunk win only exists on a raw non-HA BlueZ link. Today's client never reads `mtu_size`. Encrypted envelope is `cmd(2)+nonce(16)+len(1)+data+tag(12)` = 31 B fixed, so data ≤ 244−31 = 213 — the encrypted path has the most to gain |
| Connection-parameter request | firmware requests CI 7.5–15 ms during transfer | up to 2–4× on stop-and-wait over 30–50 ms defaults; raises windowed link ceiling | see §7.1; restore relaxed params after END |
| ESP32 drain-all + log gating | `main.cpp:171` `if`→bounded `while`; gate `writeSerial` | t_fw 6 → ~1.5 ms; **required** for Method B | |
| 2M PHY request | firmware-preferred PHY | raises packets/CE ~1.5–2×; matters once windowed (link-bound) | BG22/nRF52840/ESP32 all support 2M |
| Silabs notify retry | bounded retry on `SL_STATUS_NO_MORE_RESOURCE` | removes a live ACK-loss bug | worth doing regardless |

### 7.1 Why a connection-interval request helps (mechanism)

BLE exchanges packets only at connection events — one per connection interval — so every round trip is quantized in CI units. Stop-and-wait pays ~2.5 connection events per 230 B chunk, making throughput almost inversely proportional to CI: 50 ms → ~1.8 KB/s, 30 ms → ~2.9 KB/s, 15 ms → ~5.5 KB/s, 7.5 ms → ~10 KB/s (§2 table). No OpenDisplay firmware currently requests parameters, so direct-BlueZ links often sit at 30–50 ms central defaults. A peripheral parameter-update request (NimBLE `updateConnParams`, Bluefruit `requestConnectionParameter`, Silabs `sl_bt_connection_set_parameters`) asking for 7.5–15 ms yields 2–4× on stop-and-wait with zero protocol change — best-effort, since the central may decline.

Caveats: via an ESPHome proxy the WiFi RTT dominates and is untouched by CI, so the gain there is small; and a short CI raises the tag's radio duty cycle, so the firmware should request fast parameters at transfer start and restore relaxed parameters after END/refresh to protect battery. Under Methods A/B, CI still sets the link-rate ceiling (~3–5 packets per connection event), so this optimization compounds with pipelining rather than being made redundant by it.

**ESPHome-proxy specifics (verified against upstream sources):**

- The proxy manages the interval itself: `esp32_ble_client` sets preferred parameters before connecting (fast, 7.5 ms, for `CONNECT_V3_WITHOUT_CACHE`) via `esp_ble_gap_set_prefer_conn_params`, then after service discovery downshifts to "medium" (8.75–11.25 ms) via `esp_ble_gap_update_conn_params` (`esphome/components/esp32_ble_client/ble_client_base.cpp`). So the proxy↔tag hop already runs at a near-optimal interval — little headroom to gain there.
- Recent ESPHome/aioesphomeapi expose **`BluetoothSetConnectionParamsRequest` (message id 145)** → `bluetooth_proxy::bluetooth_set_connection_params` → `esp_ble_gap_update_conn_params`, valid only while connected, values clamped to uint16. So a client *can* request interval changes through the proxy API — but bleak's abstraction (which py-opendisplay uses) does not surface it, and it requires proxies running a recent ESPHome. Not needed for the proposals here; noted for completeness.
- A **peripheral-initiated** L2CAP parameter-update request (the Stage-0 firmware lever) is auto-accepted by the ESP-IDF Bluedroid host on the proxy — ESPHome's GAP handler doesn't intercept `ESP_GAP_BLE_UPDATE_CONN_PARAMS_EVT` — though ESPHome may later re-issue its own parameters, and single-radio WiFi/BLE coexistence on the proxy practically limits very short intervals.

Net: the conn-interval lever matters chiefly on **direct BlueZ adapters** (where 30–50 ms defaults are common); on proxy paths the interval is already fast and the WiFi RTT is the cost that only pipelining (Methods A/B) removes.

### 7.2 Rejected alternatives

- **L2CAP connection-oriented channels (CoC).** Highest raw throughput (no ATT framing, natively credit-based), but bleak exposes no CoC API on any backend and the ESPHome proxy API has no CoC support at all — only GATT operations exist. HA's habluetooth path is therefore structurally unable to use it; it would require a parallel transport benefiting only direct-adapter users. Rejected as a primary method.
- **ATT long writes (prepare/execute) to enlarge frames.** Rejected on two grounds: (1) **HA's native GATT client doesn't implement long writes at all** — it rejects any write > MTU−3 with `BleakError` (`habluetooth channels/att.py:422-432`), so this path is simply unavailable for HA-delivered transfers; (2) even where supported (legacy BlueZ), long writes cost *extra* round trips per frame (prepare + execute), the opposite of the goal, and ESP32/nRF app paths don't handle offset reassembly anyway.
- **Extending the START ACK / START payload for negotiation.** Breaks old firmware, which feeds any START payload ≥ 4 bytes into zlib (§3.6). Rejected in favor of the new `0x0078` opcode.

---

## 8. Comparison and recommendation

### 8.1 Pros/cons vs current state

| Criterion | Current stop-and-wait | Method A (lockstep window) | Method B (sliding credit) |
|---|---|---|---|
| Throughput, direct | 1.8–10 KB/s | ~9–26 KB/s (3–6×) | ~25–60 KB/s (8–12×) |
| Throughput, proxy | 1.2–3.3 KB/s | ~14–21 KB/s (5–7×) | ~25–37 KB/s (10–15×) |
| 24 KB image, proxy | 8–21 s | ~1.5–2.5 s | ~0.8–1.5 s |
| Fits HA 30 s delivery deadline | marginal / fails (large + proxy) | always, with margin | always, with margin |
| Loss detection | implicit (lockstep ≈ no loss; Adler-32 at END, compressed only) | explicit: seq + cumulative count, exposure ≤ W | same as A |
| Client complexity | baseline | +small (window loop, ~1 new function) | +moderate (async credit tracking, out-of-band NACK) |
| Firmware complexity | baseline | +moderate (negotiation, seq, batched ACK, queue sizing) | **same firmware as A** |
| Protocol surface | — | +1 opcode, +1 DATA variant, +2 ACK forms | same as A |
| Old-client compatibility | — | perfect (opt-in via 0x0078) | same |
| Old-firmware compatibility | — | perfect (gated; worst case one 2 s probe) | same |
| Encryption | works | works, W ≤ 32 | works, W ≤ 32 |
| Slow-panel (60 s SPI) robustness | proven | equivalent (credit stops client; W frames buffered) | equivalent; needs drain-all loop |
| Risk | — | ESP32 ring concurrency; Silabs buffer sizing | A's risks + sustained burst pressure |

### 8.2 Staged recommendation

1. **Stage 0 — ship now, no protocol change:** nRF `delay(20)` removal, ESP32 log gating + drain-all loop, Silabs notify retry, firmware connection-parameter request during transfers. Combined ~1.5–3× on the existing protocol for near-zero risk.
2. **Stage 1 — protocol v1: Method A** with the §4 shared wire machinery (`0x0078`, seq byte, cumulative ACK every N, END-total validation). Deliberately spec `N ≤ W` and trailing-byte-tolerant ACK parsing so Stage 2 needs **no firmware release**. Conservative grants: W=8 ESP32/nRF, W=4 Silabs, W ≤ 32 encrypted.
3. **Stage 2 — client-only: Method B** pacing in py-opendisplay once Stage 1 firmware has field mileage; raise grants (e.g. W=16 ESP32) once telemetry supports it.
4. **Not pursued:** L2CAP CoC (no bleak/proxy support), ATT long writes, START-based negotiation.

### 8.3 Side findings worth independent follow-up

- **Silabs ACK drop bug:** `pipe_send_raw` silently drops notifications on TX-buffer exhaustion (`opendisplay_pipe.c:437-447`) — can abort an otherwise-healthy upload today.
- **Silabs missing replay check:** the decrypt path validates the CCM tag but performs no nonce replay-window check (`opendisplay_pipe.c:358-394`), unlike ESP32/nRF (`encryption.cpp:128-134`).
- **ESP32 silent ring overflow:** a frame dropped at `esp32_ble_callbacks.h:106-114` is invisible even to a write-with-response client (the stack ACKs at ATT level before the app ring is consulted). Stop-and-wait masks it today; any future concurrency makes it live.
- **Uncompressed transfers have no end-to-end integrity check** — no CRC, no length validation on most paths. The §4.4 END-total validation closes part of this even for legacy-size images.

## Appendix: key source locations

| What | Where |
|---|---|
| Client send loop (stop-and-wait) | `py-opendisplay/src/opendisplay/device.py:1770-1828` |
| Client write-with-response | `py-opendisplay/src/opendisplay/transport/connection.py:306-310` |
| Chunk-size constants, `PIPELINE_CHUNKS` | `py-opendisplay/src/opendisplay/protocol/commands.py:42-55` |
| Upload orchestration | `py-opendisplay/src/opendisplay/device.py:1671-1768` (`_execute_upload`) |
| ESP32 GATT setup, MTU 512 | `Firmware/src/ble_init.cpp:214-252` |
| ESP32 command/response rings | `Firmware/src/esp32_ble_callbacks.h:13-26, 93-121`; `Firmware/src/communication.cpp:60-101` |
| ESP32 main-loop drain | `Firmware/src/main.cpp:171-198` |
| ESP32 DATA handler + per-frame ACK | `Firmware/src/display_service.cpp:1645-1683` |
| nRF per-notify `delay(20)` | `Firmware/src/communication.cpp:217` |
| Replay window ±32 | `Firmware/src/encryption.cpp:128-134` |
| Silabs DATA handler + inline ACK | `Firmware_Silabs/opendisplay_pipe.c:629-640` |
| Silabs notify drop | `Firmware_Silabs/opendisplay_pipe.c:437-447` |
| Silabs frame cap / buffers | `Firmware_Silabs/opendisplay_pipe.c:36, 1136-1140`; `config/sl_bluetooth_config.h:69` |
| HA delivery deadline | `custom_components/opendisplay/delivery.py:59-61, 285` |
| HA upload funnel | `custom_components/opendisplay/services.py:408-541` |
| HA native GATT client — MTU 247 / 244 B write / no long writes | `habluetooth==6.26.5` (`../core`): `habluetooth/channels/att.py:92-93, 372, 422-432`; `habluetooth/client_mgmt.py:170-172, 210, 332-333` |
| HA proxy path MTU (proxy-negotiated) | `aioesphomeapi==45.5.2` `model.py:1580` (`BluetoothDeviceConnectionResponse.mtu`) |
| Protocol spec | `opendisplay.org/httpdocs/protocol/ble-flow.html` |
