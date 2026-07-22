# WiFi Transport Readiness — Firmware Audit

**Date:** 2026-07-22
**Scope:** `Firmware/` (PlatformIO/Arduino, nRF52840 + ESP32-S3/C6/C3), with a
confirming pass over `Firmware_NRF54`, `Firmware_Silabs`, `Firmware_NRF`.
**Type:** Report-only catalog + gap analysis. No source was modified.

---

## Executive summary

**WiFi is not a green-field feature in this repo — a working ESP32 WiFi transport
already exists and runs alongside BLE today.** `Firmware/src/wifi_service.cpp`
brings up a WiFi STA, advertises `_opendisplay._tcp` over mDNS, and accepts a
single TCP client whose length-prefixed frames are fed into the *same*
`imageDataWritten()` opcode dispatcher that BLE uses. Responses are mirrored to
both transports. The requirement "BLE and WiFi simultaneously active on one
device" is therefore *already met in principle* — both radios are up, both feed
one protocol layer, and the ESP32 build uses **NimBLE** (not Bluedroid), which is
the coexistence-friendly choice.

What is missing is not the transport skeleton but the surrounding robustness:
a **single shared global encryption session** that BLE and WiFi clients stomp on
each other, **no transport-level security** (plaintext TCP; app-layer AEAD is
optional and off when `encryption_enabled==0`), **no WiFi transport on the Home
Assistant side at all** (HA still speaks only BLE), no capability/IP beacon beyond
the mDNS SRV record, no WiFi OTA, and a power model that assumes WiFi is a
mains-only feature (WiFi is implicitly torn down by deep sleep and there is no
modem-sleep tuning).

The other three firmware repos (`Firmware_NRF54`, `Firmware_Silabs`,
`Firmware_NRF`) have **no WiFi-capable hardware and no WiFi code** — they only
*parse-and-skip* the `wifi_config` (0x26) TLV. Confirmed below.

---

## What exists today

### Build environments and WiFi capability (`Firmware/platformio.ini`)

| Env | Chip | WiFi-capable? | Notes |
|-----|------|--------------|-------|
| `nrf52840custom` | nRF52840 | **No** (no WiFi radio) | `TARGET_NRF`, Adafruit Bluefruit; `lib_ignore = NimBLE-Arduino` |
| `esp32-s3-N16R8` / `N8R8` / `N32R8` | ESP32-S3 (+OPI PSRAM) | **Yes** | `TARGET_ESP32`, NimBLE-Arduino |
| `esp32-s3-N32R8-extuart` / `E1004` | ESP32-S3 (Seeed reTerminal) | **Yes** | PSRAM present; log over UART |
| `esp32-c3-N4` / `esp32-c3-N16` | ESP32-C3 | **Yes** | **No PSRAM** — RAM-tight for WiFi+BLE |
| `esp32-c6-N4` | ESP32-C6 | **Yes** | WiFi 6; also 802.15.4 |

All ESP32 targets compile `wifi_service.cpp` (guarded `#ifdef TARGET_ESP32`). The
nRF target excludes it and the WiFi globals via the same guard.

### The WiFi transport (`Firmware/src/wifi_service.cpp`, `.h`)

- **Association** — `initWiFi(bool waitForConnection)` (line 85). Gated by
  `communication_modes & COMM_MODE_WIFI` (bit 2, line 88) *and* `wifiConfigured`
  (the 0x26 TLV was parsed, line 93) *and* a non-empty SSID (line 100).
  `WiFi.begin(ssid, password)`, `setAutoReconnect(true)`, `WIFI_POWER_15dBm`.
  Non-blocking mode (`waitForConnection=false`) is used at boot; a blocking
  3×10 s retry path also exists (lines 120-146).
- **mDNS discovery** — `restartLanService()` (line 73) publishes
  `OD<chipid>.local` and `MDNS.addService("opendisplay","tcp", wifiServerPort)`
  → `_opendisplay._tcp`. `opendisplay_mdns_update_msd_txt()` (line 51) mirrors the
  14 dynamic MSD bytes into an mDNS TXT key `msd` (throttled to 400 ms), so a LAN
  scanner sees the same telemetry a BLE scanner gets in the manufacturer record.
- **TCP server** — `WiFiServer wifiServer` on `wifiServerPort` (default **2446**,
  the same 0x2446 used for the BLE service UUID / company id). `handleWiFiServer()`
  (line 170) accepts **one** client at a time (a new connection replaces the old,
  line 189), reads into a static `tcpReceiveBuffer[8192]`.
- **LAN framing** — inbound: `[len:2 little-endian][payload:len]` where `len ≤
  WIFI_LAN_MAX_PAYLOAD (4096)` (lines 229-246). Each de-framed payload is handed
  straight to `imageDataWritten(NULL, NULL, payload, len)` — i.e. the payload is a
  raw opcode frame identical to a BLE GATT write. Outbound framing is the mirror:
  `send_wifi_lan_frame()` in `communication.cpp:78` prepends the 2-byte LE length.
- **Session lifecycle** — on client connect/replace/drop it calls
  `clearEncryptionSession()` (lines 163, 191, 205) and resets the RX buffer.

### Provisioning / credential storage (already over BLE)

WiFi credentials are **already delivered over BLE** via the normal config-write
path — there is no separate provisioning protocol needed:

- `wifi_config` TLV **0x26** → `struct WifiConfig` (160 bytes, canonical layout in
  `opendisplay-protocol/src/opendisplay_protocol.h` / `include/opendisplay_structs.h:887`):
  `ssid[32]`, `password[32]`, `encryption_type`, `server_host[64]`,
  `server_port` (the one **big-endian** field), `reserved[29]`.
- Parsed in `Firmware/src/config_parser.cpp:457` → populates globals
  `wifiSsid/wifiPassword/wifiEncryptionType/wifiServerUrl/wifiServerPort`
  (defined in `Firmware/src/main.h:138-153`).
- Written to the device by the host with `CMD_CONFIG_WRITE (0x0041)` /
  `CMD_CONFIG_CHUNK (0x0042)` (dispatched in `communication.cpp`), persisted, and
  `reloadConfigAfterSave()` (`communication.cpp:27`) calls `initWiFi()` so WiFi
  comes up immediately after a config write.
- Capability advertisement: `SystemConfig.communication_modes` bit 2
  `OD_COMM_MODE_WIFI` (`include/opendisplay_structs.h:409`) declares that the
  device *supports* WiFi transfer.

### Confirmation: no WiFi in the other three firmware repos

- **`Firmware_NRF54`** — `opendisplay_structs.h:222` explicitly notes "The nRF54
  radio has no Wi-Fi"; 0x26 is defined (`opendisplay_constants.h:12`) and stored
  but never acted on.
- **`Firmware_Silabs`** — EFR32BG22 has no WiFi; `opendisplay_config_parser.c:326`
  is `case CONFIG_PKT_WIFI: // wifi_config - skip this as requested`. (The
  `_WIFI_301` hit under `simplicity_sdk_.../sl_hal_system.h` is an unrelated
  Series-3 part-family enum, not our code.)
- **`Firmware_NRF`** — `config_parser.c:319` likewise skips 0x26.

All three correctly treat 0x26 as an opaque skipped TLV. **Nothing to add there.**

---

## BLE transport architecture relevant to adding WiFi

The single most important architectural fact: **the opcode dispatcher is already
transport-agnostic.** WiFi did not have to reinvent it — it just calls into it.

- **One dispatch entry point:** `imageDataWritten(conn_hdl, chr, data, len)` in
  `communication.cpp:534`. Despite the name it services *every* command. It:
  1. reads the 2-byte big-endian opcode (`data[0]<<8 | data[1]`),
  2. handles the `CMD_AUTHENTICATE (0x0050)` / `CMD_FIRMWARE_VERSION (0x0043)`
     handshake before the encryption gate,
  3. if `isEncryptionEnabled()`, requires an authenticated session and
     decrypts the AEAD envelope (nonce + tag) in place,
  4. `switch(command)` dispatches to the per-opcode handlers (config r/w/chunk,
     direct-write 0x0070-0x0072, partial-write 0x0076, pipe-write 0x0080-0x0082,
     LED, buzzer, power-off 0x0052, deep-sleep 0x0053, reboot, MSD read).
  The `conn_hdl`/`chr` arguments are already `(void)`-cast unused — WiFi passes
  `NULL, NULL`.
- **BLE plumbing feeds the same entry point:**
  - nRF: `imageCharacteristic.setWriteCallback(imageDataWritten)` directly
    (`ble_init.cpp:160`), single GATT service/characteristic **0x2446**.
  - ESP32: NimBLE write callback enqueues into a lock-free `commandQueue`
    (`main.h:384`); `loop()` drains it and calls `imageDataWritten(...)`
    (`main.cpp:357`). WiFi frames enter via `handleWiFiServer()` later in the same
    `loop()` pass (`main.cpp:388`) → the exact same call.
- **One response fan-out:** `sendResponse()` / `sendResponseUnencrypted()`
  (`communication.cpp:128, 164`) encrypt (when authed) and then, on ESP32, call
  **both** `send_wifi_lan_frame()` and `esp32_queue_ble_notify_copy()`
  (lines 141-142, 228-229). So a response is delivered to whichever transports are
  live. BLE notifies are queued and drained in `loop()`
  (`flushResponseQueueToBle`, `main.cpp:237`).
- **Streaming/image upload path:** identical for both transports — the image
  handlers (`handleDirectWriteData`, `handlePipeWriteData`, etc.) consume opcode
  frames one at a time regardless of origin. Note the **PIPE_WRITE sliding-window
  + SACK** machinery (0x0080-0x0082) exists to make BLE's lossy/unordered notify
  path reliable; over TCP that reliability is redundant (TCP already guarantees
  order/delivery), but the pipe opcodes still *work* over TCP because they are
  just frames. A WiFi-optimized host would more naturally use direct-write.

**Implication for a "parallel WiFi transport":** the hard part (a transport-neutral
protocol core with a single dispatch + single response fan-out) is already done.
Any new transport only needs to (a) deframe into opcode buffers and call
`imageDataWritten`, and (b) implement a `send_*` for `sendResponse` to fan out to.

---

## Power & coexistence

- **BLE stack choice = NimBLE (ESP32).** `platformio.ini` pulls
  `h2zero/NimBLE-Arduino`; init in `ble_init.cpp:250` (`ble_init_esp32`). NimBLE's
  much smaller RAM/flash footprint vs Bluedroid is exactly what leaves headroom to
  run the WiFi stack concurrently. There is **no** Bluedroid path on ESP32 here.
- **Simultaneous operation is real and already scheduled cooperatively.** `loop()`
  (`main.cpp:294`) runs, in order: `pollActivity()` → drain BLE command queue →
  flush BLE responses → `handleWiFiServer()` (line 388) → deep-sleep decision.
  `pollActivity()` (line 187) treats a live LAN session
  (`wifiInitialized && wifiServerConnected && wifiClient.connected()`) as activity
  that keeps the device awake, exactly like a BLE connection. WiFi servicing was
  deliberately moved *after* BLE processing "to avoid blocking BLE command
  responses" (comment, line 386).
- **Coexistence is left to the IDF's software coexistence arbiter** (default). No
  explicit `esp_coex_*` / `esp_wifi_set_ps()` tuning appears anywhere in `src/`
  (grep for `esp_wifi`, `WIFI_PS`, `setSleep` returns nothing). On single-antenna
  parts (C3/C6) BLE and WiFi time-share one 2.4 GHz radio, so heavy concurrent use
  degrades both — acceptable but untuned.
- **Power model = WiFi is effectively mains-only.** Deep sleep
  (`enterDeepSleep()`, `main.cpp:513`) only runs when `power_mode == 1` (battery)
  and `deep_sleep_time_seconds > 0`; it `BLEDevice::deinit(true)` +
  `esp_deep_sleep_start()`, which also kills WiFi. There is no WiFi-aware wake or
  modem-sleep duty-cycling — an association draws ~80-100 mA continuously, which is
  incompatible with the battery deep-sleep tags. WiFi today is a
  wired/always-powered-panel feature. `initWiFi(false)` is called non-blocking at
  boot (`main.cpp:124`) and again in `fullSetupAfterConnection()` (line 495) so it
  never stalls the wake path.
- **RAM.** Static `tcpReceiveBuffer[8192]` (`main.h:152`) + the WiFi stack
  (~40-50 KB) is comfortable on the S3 PSRAM parts but tight on `esp32-c3-N4`
  (no PSRAM, 400 KB SRAM shared with NimBLE + framebuffers).

Background docs consulted: `opendisplay-protocol/agents/Firmware/FINDINGS_NONBLOCKING_LOOP_2026-07-13.md`,
`Firmware/docs/architecture-deep-sleep-power-buttons.md`,
`Firmware/docs/FIRMWARE_NIMBLE_PORT_CODE_REVIEW_2026-07-17.md`. None of these
address WiFi coexistence tuning specifically — a gap in the design record.

---

## Gap analysis

Ordered roughly by how blocking each is for "WiFi as a full HA transport."

1. **[Blocking] No WiFi transport on the Home Assistant side.** The integration
   (`Home_Assistant_Integration/custom_components/opendisplay/`) speaks only BLE;
   the sole WiFi reference is an aspirational comment in `delivery.py:16`. There is
   no zeroconf/`_opendisplay._tcp` discovery, no TCP client, no LAN framing on the
   host. The firmware advertises and listens, but nothing connects. This is the
   biggest single gap and it lives outside the firmware repo.

2. **[Blocking] Shared global encryption session — BLE/WiFi mutual clobber.**
   There is one process-global session (`clearEncryptionSession()` is called on
   every LAN connect/disconnect, `wifi_service.cpp:163/191/205`). A WiFi client
   connecting mid-BLE-session wipes the BLE session's keys/nonce state (and vice
   versa). Two clients cannot be authenticated at once. For true simultaneous
   BLE+WiFi use this must become **per-transport session state** (or an explicit
   single-active-transport election).

3. **[Security] Plaintext TCP; security is opt-in.** The LAN carries raw opcode
   frames. Confidentiality/integrity depend entirely on the *optional* app-layer
   AEAD (`SecurityConfig` 0x27, `encryption_enabled`). If encryption is disabled
   (the default for many tags), **anyone on the LAN can push images / read config /
   trigger reboot/DFU** over TCP with no auth. Recommend: require encryption when
   `COMM_MODE_WIFI` is set, and/or add TLS.

4. **[Security] Credentials at rest.** SSID + password live in the stored config
   (`WifiConfig`, plaintext in the config blob) and are echoed to the serial log
   (`config_parser.cpp:544-546`, `wifi_service.cpp:105`). Fine for a lab, risky for
   production.

5. **[Robustness] Single client, no backpressure beyond buffer-full drop.**
   `handleWiFiServer()` drops the connection on a full 8 KB buffer or a bad frame
   length (lines 220, 232). One client only. No keep-alive/idle timeout beyond the
   30 s socket timeout.

6. **[Discovery/capability] No IP/version/capability beacon beyond mDNS SRV.** HA
   can find `_opendisplay._tcp` and the `msd` TXT (14 dynamic bytes), but there is
   no advertised firmware version, `communication_modes`, or auth-required flag in
   the TXT record — HA would have to connect + `CMD_FIRMWARE_VERSION` to learn
   them. The mDNS instance name is `OD<chipid>` so identity mapping to a BLE MAC is
   indirect.

7. **[Feature] No WiFi OTA.** Firmware update is still BLE DFU / `CMD_ENTER_DFU`
   (0x0051) / USB. No `ArduinoOTA`, no HTTP pull. WiFi could carry OTA but doesn't.

8. **[Power] No battery-compatible WiFi.** No modem-sleep (`esp_wifi_set_ps`), no
   WiFi-preserving light sleep, no coexistence tuning. WiFi + deep-sleep are
   mutually exclusive today.

9. **[Framing] Raw TCP, not HTTP/WebSocket.** The 2-byte-LE-length wrapper is a
   private framing. That is *fine and efficient* for a dedicated HA client, but it
   is not browser/standard-tooling reachable and carries no version/type field for
   future evolution. The same opcode stream *does* run over TCP unchanged (proven
   by the working path) — WebSocket/HTTP would only be needed for interop with
   generic clients, not for HA.

10. **[Minor] DHCP only** (no static IP / IPv6), reconnect handling is a 10 s poll
    in `loop()` (`main.cpp:389-403`), and `WifiConfig.encryption_type` /
    `server_host`/`server_port` (an *outbound push* server) are parsed but the
    push-to-server client is not implemented — only the inbound TCP server is. So
    the header/struct is ahead of the firmware on the "device dials out to a
    server" model.

---

## Firmware implementation sketch

The firmware is ~70% there. The recommended work is hardening + a few additions,
not a rewrite. Touchpoints are concrete file paths.

### 1. Make the transport abstraction explicit (fixes gap #2)

- Introduce a small `Transport` notion carrying **its own** auth/encryption
  session, replacing the single process-global session. Minimal version: an
  `enum ActiveTransport { NONE, BLE, LAN }` plus per-transport `EncryptionState`
  in `encryption_state.h`, and have `imageDataWritten` take an origin tag instead
  of the ignored `conn_hdl`.
- Touch: `encryption.cpp` / `encryption_state.h` (session becomes per-transport),
  `communication.cpp:534` (`imageDataWritten` origin param; `sendResponse` fan-out
  already exists at lines 141/228 — route the response to the *originating*
  transport instead of unconditionally both, while still allowing broadcast for
  MSD/telemetry), `wifi_service.cpp:239` and `main.cpp:357` (pass origin).

### 2. Security hardening (gaps #3, #4)

- In `initWiFi()` (`wifi_service.cpp:85`), refuse to open the TCP server unless
  `SecurityConfig.encryption_enabled` (or a new `require_auth_on_wifi` flag),
  logging a clear reason. Cheapest correct default.
- Optionally add mbedTLS (ESP-IDF ships it) for `WiFiServerSecure`; keep AEAD as
  the interop baseline so non-TLS clients still work.
- Stop logging SSID/password (`config_parser.cpp:544-546`, `wifi_service.cpp:105`).

### 3. Capability + identity beacon (gap #6)

- Extend `opendisplay_mdns_update_msd_txt()` (`wifi_service.cpp:51`) to add TXT
  keys: `fw` (version), `cm` (`communication_modes`), `auth` (0/1), and the BLE MAC
  so HA can correlate the LAN device with the BLE-discovered tag. This is a pure
  additive change to the mDNS record; no protocol-header change required.

### 4. Coexistence & power posture (gap #8)

- Add an explicit `esp_wifi_set_ps(WIFI_PS_MIN_MODEM)` after association for the
  mains case, and gate WiFi bring-up on `power_mode != 1` (or a dedicated
  "wifi-while-battery" opt-in flag) so the deep-sleep tags never associate. Small
  addition in `initWiFi()`; document the single-antenna time-share caveat for
  C3/C6 in `docs/`.

### 5. Optional: WiFi OTA (gap #7)

- Add `ArduinoOTA` or an HTTP-pull updater behind a new opcode or the existing
  `CMD_ENTER_DFU` path, guarded by auth. New module `src/wifi_ota.cpp`, hooked from
  `handleWiFiServer()` / `loop()`.

### 6. Host side (gap #1 — out of this repo but the true blocker)

- `Home_Assistant_Integration`: add a `wifi_transport.py` (async TCP client with
  the 2-byte-LE framing that mirrors `send_wifi_lan_frame`/`handleWiFiServer`),
  plus zeroconf discovery of `_opendisplay._tcp`, and let the coordinator prefer
  LAN when a tag exposes both. The wire payloads are byte-identical to BLE GATT
  writes, so the existing opcode/AEAD encoders are reused verbatim.

### Files that already implement the transport (reference map)

| Concern | File:line |
|---|---|
| WiFi STA + TCP server + mDNS + LAN deframing | `Firmware/src/wifi_service.cpp` (whole file) |
| WiFi service header / API | `Firmware/src/wifi_service.h` |
| Transport-neutral opcode dispatch | `Firmware/src/communication.cpp:534` (`imageDataWritten`) |
| Response fan-out to BLE + LAN | `Firmware/src/communication.cpp:78,128,164` |
| WiFi globals (server/client/buffers/creds) | `Firmware/src/main.h:138-153` |
| Loop scheduling / coexistence / deep sleep | `Firmware/src/main.cpp:294,388,513` |
| `wifi_config` (0x26) parse | `Firmware/src/config_parser.cpp:457` |
| `WifiConfig` struct (canonical) | `opendisplay-protocol/src/opendisplay_protocol.h` / `include/opendisplay_structs.h:887` |
| `COMM_MODE_WIFI` capability bit | `include/opendisplay_structs.h:409` |
| MSD advertisement (mirrored to mDNS TXT) | `Firmware/src/display_service.cpp:1707` (`updatemsdata`) |

---

*End of report.*
