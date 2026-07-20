# Firmware (ESP32/nRF52840) toolchain audit — 2026-07-19

**Repo:** `Firmware` (PlatformIO, ESP32-S3/C3/C6 + nRF52840 via bb_epaper/Seeed_GFX)
**Branch / commit:** `feat/incorporate-structs2` @ `56019e7`
**Scope:** static, read-only. No build/flash/run. Two effort tiers below:
prior-pass findings (power / device-control / peripherals, **verified by prior
audit pass**) plus this pass's gap-fill on the BLE layer, config parser,
display/main-loop, and zlib window flags.

---

## Architecture overview

Single-threaded cooperative model on ESP32; a two-task model on nRF.

- **ESP32 BLE ingest is queue-decoupled.** NimBLE `onWrite`
  (`esp32_ble_callbacks.h:80`) only `memcpy`s the frame into a 33-slot SPSC ring
  (`commandQueue`, ACQUIRE/RELEASE atomics) and returns. `loop()`
  (`main.cpp:344-362`) drains up to `COMMAND_QUEUE_SIZE` frames/pass into
  `imageDataWritten()`, which is the single command dispatcher
  (`communication.cpp:503`). Responses are produced synchronously by handlers,
  pushed into a **10-slot** `responseQueue` ring
  (`esp32_queue_ble_notify_copy`, `communication.cpp:89`), and later notified by
  `flushResponseQueueToBle()` (`main.cpp:237`) on the loop task. This is the
  crux of finding **H1** below.
- **nRF has no such queue:** `imageDataWritten` is registered directly as the
  Bluefruit write callback (`ble_init.cpp:160`) and runs on the SoftDevice
  task, so handlers mutate shared state concurrently with `loop()` (the prior
  pass's buzzer/LED cross-task race, staging finding 17).
- **The ESP32 build is now fully NimBLE-Arduino.** No Bluedroid code path
  remains anywhere in `src/` (grep for `bluedroid|esp_ble_*|getNative` returns
  nothing). See finding **R1** — the historical Bluedroid config-TX gate is
  moot on this branch.
- **Config** is a length-prefixed TLV container parsed by `loadGlobalConfig()`
  (`config_parser.cpp:263`) into `globalConfig`; persisted to LittleFS/InternalFS
  with a magic + CRC32 wrapper; factory-embed fallback on first boot.
- **Display** paths: legacy direct-write `0x70/0x71/0x72`, partial `0x76`, and
  sliding-window pipe `0x80/0x81/0x82`, all funnelling through one panel session
  in `display_service.cpp`. Bytes are streamed to controller RAM
  (`bbepWriteData`) rather than a heap framebuffer, or through a 2048-byte
  `decompressionChunk` for zlib streams.
- **Power/sleep/peripherals**: see the prior-pass section, incorporated below.

---

## Findings by severity

### HIGH

#### H1 — Config-read (0x0040) truncates on ESP32 BLE: 44 chunks into a 10-slot response ring
**File:** `communication.cpp:351-393` (`handleReadConfig`) +
`communication.cpp:89-107` (`esp32_queue_ble_notify_copy`) + `main.h:363`
(`RESPONSE_QUEUE_SIZE 10`). **Confidence: Confirmed (traced).**

`handleReadConfig()` runs synchronously inside the `loop()` command drain. It
emits config data in a tight `while` loop, one `sendResponse()` per chunk, up to
`maxChunks = (MAX_CONFIG_SIZE + 93)/94 = 44` chunks (payload ≤ `MAX_RESPONSE_DATA_SIZE`
100 minus header). On ESP32 each `sendResponse()` merely enqueues into the
**10-slot** `responseQueue`; the ring is only drained by
`flushResponseQueueToBle()`, which runs on the *same* loop task and therefore
**cannot run until `handleReadConfig()` returns**. The inter-chunk `delay(1)`
(`communication.cpp:384`) blocks that same task, so it drains nothing.

Result: `esp32_queue_ble_notify_copy` hits `nextHead == responseQueueTail`
after ~9 queued chunks and logs `"Response queue full, dropping response"` for
every chunk thereafter. Any stored config larger than ~9×96 ≈ **864 bytes** is
silently truncated on read-back over BLE. A realistically provisioned device
(displays + LEDs + sensors + wifi_config + security_config + `data_extended`,
which alone is 9×32 = 288 bytes) easily exceeds that. The LAN transport is
unaffected (`send_wifi_lan_frame` writes synchronously), so this is BLE-only.

This is a **regression introduced by the NimBLE queue-based response model**:
the pre-NimBLE direct-notify path drained each chunk before producing the next.
Fixes would be to drain the response ring between chunks inside
`handleReadConfig` (as the command drain already does between commands), grow
the ring to cover `maxChunks`, or notify config-read chunks directly.

**Failure scenario:** host issues `CMD_CONFIG_READ`; py-opendisplay/HA
reassembles chunks 0..8, then never receives chunk 9+; readback CRC/parse fails
or returns a truncated config. Intermittent-looking because small configs pass.

---

### MEDIUM

#### M1 — Seeed/IT8951 `tconWaitForDisplayReady()` has no timeout (unbounded hang)
**File:** `lib/Seeed_GFX/Extensions/Tcon.cpp:521-524`
(`while(tconReadReg(LUTAFSR));`). **Confidence: Confirmed (traced), still-live.**

Reached on the Seeed 10.3" ED103/E1004 path via `waitforrefresh()` →
`seeed_gfx_wait_refresh()` → `EPaper::update()`. A wedged panel (SPI glitch, no
LUTAFSR clear) hangs the firmware forever with no watchdog escape on that code
path. Carried over from `FINDINGS_NONBLOCKING_LOOP_2026-07-13.md` §6.2; verified
still present at this commit. The bb_epaper path is bounded (`waitforrefresh`,
finding L1); only the Seeed path is unbounded.

#### M2 — WiFi-LAN is an unauthenticated full control plane when security is off
**Verified by prior audit pass** (staging finding 21). `wifi_service.cpp:239`
feeds length-prefixed LAN frames straight into `imageDataWritten()` — the entire
BLE command surface (config write, reboot, power-off, DFU) plus mDNS-broadcast
sensor data. If `security_config` encryption is not enabled, this is an
unauthenticated LAN control/exfil plane. Gated only by `communication_modes`
bit 2 + a `wifi_config` TLV. Needs an explicit security note in docs.
`initWiFi(waitForConnection=true)` can block ~34 s but all call sites pass
`false` (dead-but-dangerous). Framing/overflow math is correct.

#### M3 — 60 s firmware floor on 0x0053 deep-sleep overrides, undocumented
**Verified by prior audit pass** (staging finding 8). `device_control.cpp:725-730`
clamps host sleep overrides below 60 s up to 60 s, silently, no NACK. Canonical
header says range 1..65535 with a *recommended* client floor ≥ 10 s. Either the
header/@limits + contract doc must record the clamp, or the clamp should drop to
10 s.

#### M4 — nRF buzzer/LED cross-task race → possible OOB read
**Verified by prior audit pass** (staging finding 17). On nRF, BLE-task handlers
mutate `s_buzzer`/`s_led` while `buzzerService()`/`processLedFlash()` run on
`loop()`; no lock, `active` not atomic. Desynced `nsteps`/`pattern_count` can
walk the `poff` cursor past `melody[256]`. ESP32 immune (drained on loop task).

*(Additional prior-pass Medium items — power_latch MOSFET-hold at wake
[staging 1], C6 LP-pad latch loss [staging 5], D-FF boot race [staging 6],
idle-sleep gate excludes buzzer/LED [staging 11], SHT40 poll stall [staging 19]
— are incorporated by reference from the staging document with their original
labels.)*

---

### LOW

#### L1 — `waitforrefresh()` effective timeout is ~2× nominal
**File:** `display_service.cpp:734` (`for i < timeout*100`, `delay(10)`) +
`bbepIsBusy()` internal hidden `delay(11)`. **Confidence: Confirmed, still-live**
(`FINDINGS_NONBLOCKING_LOOP` §6.1). Each poll iteration is ~21 ms not 10 ms, so
`waitforrefresh(60)` (`display_service.cpp:520`) actually bounds at ~120 s. Bounded
(not a hang), only a mislabeled timeout / slower failure detection.

#### L2 — Stale/misleading default-case log in the command dispatcher
**File:** `communication.cpp:668`. The unknown-command branch prints
`"Expected: 0x0011 (read config), 0x0064 (image info), 0x0065 (block data),
0x0003 (finalize)"` — opcodes from a long-dead protocol revision. Cosmetic, but
actively misleading during protocol debugging.

#### L3 — CMD_REBOOT / power-off ACKs are never transmitted (benign)
**File:** `communication.cpp:602-606` (`CMD_REBOOT`: `delay(100); reboot();`);
staging finding 9 (`device_control.cpp:764-771` power-off ACK). Responses are
queued but the device reboots/cuts rail before `flushResponseQueueToBle()` runs,
so the ACK never leaves. Protocol is fire-and-forget so this is acceptable, but
the `delay(100)` accomplishes nothing on ESP32.

#### L4 — CONFIG_WRITE single-frame of exactly 201 bytes loses one byte and stalls
**File:** `communication.cpp:406-428`. For `CONFIG_CHUNK_SIZE < len <
CONFIG_CHUNK_SIZE_WITH_PREFIX` (i.e. len == 201, since the two constants are 200
and 202), the `else` branch (`:418`) treats the whole frame as data, copies
`min(len,200) = 200` bytes (dropping the 201st), sets `totalSize = 201`,
`expectedChunks = 1`, and waits for a follow-up `0x42` chunk that a
single-frame writer never sends. Narrow window (only len exactly 201), but a
silent truncate + stalled `chunkedWriteState.active`. **Confidence: Confirmed.**

#### L5 — Prior-pass Low items (incorporated by reference)
power_latch isolate/hold ordering [staging 3,4]; ext0 arm return unchecked
[staging 13]; button-held-at-sleep-entry skip [staging 14]; buzzer stale "5 s
cap" comments [staging 16, = FINDINGS §6.5]; passiveBuzzerPowerOffAlert pin
inconsistency [staging 18]; non-ESP32 0x0053 silent [staging 10]; button ISR
last-writer-wins [staging 12]. All retain their staging labels.

---

### Resolved / not-a-finding (verification results)

#### R1 — Bluedroid config-TX gate: RESOLVED on this branch
Memory recorded a bug where config-read TX was gated on a NimBLE-only flag so
Bluedroid builds always dropped config responses (commit `77750a6` era, marked
UNFIXED). **On `feat/incorporate-structs2` this is moot and effectively fixed:**
(a) the ESP32 build is now **entirely NimBLE** — no Bluedroid path exists; and
(b) commit `77750a6` ("gate config-response TX on CCCD subscription, not
getConnectedCount()") makes `esp32_ble_notify_enabled()`
(`ble_init.cpp:222-228`) return the CCCD-subscription flag
`esp32BleNotifySubscribed`, set by `onSubscribe`
(`esp32_ble_callbacks.h:74-79`). `flushResponseQueueToBle()` correctly holds
responses queued while connected-but-not-subscribed (`main.cpp:259-261`) rather
than dropping them. This is correct behaviour. *(Note: the true remaining
config-read defect is H1, a different mechanism — ring overflow, not the CCCD
gate.)*

#### R2 — NimBLE callback-race fix (PR #108): verified sound
The onWrite→ring→loop-drain decoupling (`esp32_ble_callbacks.h:80-122`,
`main.cpp:344-362`) uses proper ACQUIRE/RELEASE atomics on head/tail, publishes
the payload before the head store, and defers all heavyweight/state-mutating
callback work (disconnect teardown, msd update, advertising restart) to
`loop()` via flags (`serviceBleDisconnectCleanup`, `msdUpdatePending`,
`bleRestartAdvertisingPending`). No BLE-task/loop() shared-state mutation of the
display/pipe path remains on ESP32. The race the PR targeted is closed.

#### R3 — Config TLV parser bounds: clean
`loadGlobalConfig()` (`config_parser.cpp:263-626`) guards every TLV: the loop
condition and `if (offset + 2 > configLen - 2) break` bound the length+id read;
each `case` checks `offset + sizeof(struct) <= configLen - 2` before `memcpy`;
per-array counts are capped (`< 4`, `< 2` for flash); unknown IDs skip to CRC;
`data_extended` strings are force-NUL-terminated. `configLen < 3` is rejected
before the `configLen - 2` arithmetic, so no unsigned underflow. `saveConfig`
/`loadConfig` bound on `MAX_CONFIG_SIZE` and verify magic + CRC32. Advisory
CRC-16 mismatch is warn-only (matches nRF/Silabs). No memory-safety findings.

#### R4 — Chunked config-write assembly & direct/partial write bounds: clean
`handleWriteConfigChunk` (`communication.cpp:453-493`) bounds every append
(`receivedSize + len > 4096`, `receivedChunks >= MAX_CONFIG_CHUNKS`, `len >
CONFIG_CHUNK_SIZE`) before `memcpy` into the 4096-byte buffer. Direct-write data
clamps to `remainingBytes` (`display_service.cpp:2222-2223`); `streamGray4Bytes`
bounds writes to `2*planeBytes` (`:1924,1932-1934`); zlib output paths check
`directWriteBytesWritten > directWriteDecompressedTotal` after each 2048-byte
chunk and abort (`:3054-3056`). `handlePartialWriteStart` validates header
length, flag mask, etag, rect-in-bounds (`rectX+rectW>dispW`), 8px alignment,
and plane geometry before activating (`:2097-2168`). Raw sink writes go to panel
controller RAM (address-window bounded), not a heap buffer. No MCU-side overflow
found.

---

## Zlib window flags (per-env)

**Default is a 512-byte window; only one env accepts legacy 32 KB streams.**

- `lib/uzlib/src/uzlib.h:21-23` — `OPENDISPLAY_ZLIB_WINDOW_BITS` defaults to
  **9** (512 B) when the build flag is absent; `:25-27` hard-errors outside 9..15.
- `lib/uzlib/src/od_zlib_stream.c:641-644` — the zlib header parser **rejects**
  any stream whose advertised window `((cmf>>4)+8) > OPENDISPLAY_ZLIB_WINDOW_BITS`
  with `"zlib stream window exceeds firmware limit"`. So a 512 B-window build
  refuses any standard 32 KB-window zlib stream.
- `platformio.ini`: **only `env:esp32-s3-E1004`** (line 148) sets
  `-DOPENDISPLAY_ZLIB_WINDOW_BITS=15` (accepts up to 32 KB). Every other env
  leaves it commented (`#-D...WINDOW_BITS=15`) → default 9 → **512 B window,
  rejects legacy 32 KB streams**. `-DOPENDISPLAY_ZLIB_USE_HEAP_WINDOW` is an
  orthogonal allocation flag (heap vs static window buffer), set independently
  per env and does not change the accept/reject boundary.

**Cross-repo consequence (Medium interop):** this Firmware repo is the odd one
out. The workspace CLAUDE.md notes "existing targets pin 32 KB windows for
legacy-client compatibility" — that holds for the *other* firmwares, but here
**all ESP32 envs except `esp32-s3-E1004` require the host to compress with a
≤512 B window.** If py-opendisplay/HA emits a standard 32 KB-window zlib stream
for a compressed `0x70`/`0x76`/`0x80` transfer, every env but E1004 fails the
whole transfer at the header with `"window exceeds firmware limit"`. The host
side must select window size per target, or the firmware default must be raised.
Confidence: Confirmed (traced parser + build flags).

---

## Unimplemented / partial features

| Feature | State on this branch | Evidence |
|---|---|---|
| NFC | Not implemented (matches @targets) | no `0x0083`/`0x0082` NFC opcode in dispatcher; staging cross-repo note |
| Config read-back > ~864 B over BLE | **Broken** (silent truncation) | H1, `handleReadConfig` + 10-slot ring |
| Legacy 32 KB-window zlib streams | Rejected on all envs except `esp32-s3-E1004` | zlib section above |
| Async display refresh (BLE keeps flowing during refresh) | Not implemented; blocking poll by choice | `FINDINGS_NONBLOCKING_LOOP` Tier B/C; `waitforrefresh` still blocks loop |
| Seeed/IT8951 non-blocking / bounded refresh | Not implemented | M1, monolithic `EPaper::update()` |
| `OD_COLOR_SCHEME_SEVEN_COLOR` | Stub (flagged in commit `7b9c005`) | boot-screen doc commit |
| 0x76 partial-write RESP mirror opcode | Missing upstream; raw `0x76` literal echoed | `display_service.cpp:2148-2151` TODO |
| CONFIG_WRITE single-frame len == 201 | Buggy (1-byte loss + stall) | L4 |

---

## Cross-repo observations

1. **H1 is a host-visible interop break.** py-opendisplay's config read-back /
   verify flow will observe truncated configs from ESP32 tags over BLE whenever
   the stored config exceeds ~864 bytes. This is not visible in the header or in
   any other firmware repo — it is specific to this branch's NimBLE response-ring
   model. Worth a coordinated note in py-opendisplay's config-read reassembly.
2. **Zlib window divergence** (above): the host must not assume a 32 KB window is
   universally accepted. This Firmware repo's default (512 B) is stricter than
   the other firmwares; only `esp32-s3-E1004` matches the "32 KB legacy" posture.
3. **Opcode numbering**: this repo is the ONLY one on the v2.1 swapped numbering
   (`0x0052=POWER_OFF`, `0x0053=DEEP_SLEEP`), dispatched at
   `communication.cpp:660-664`; its vendored `include/opendisplay_protocol.h` is
   the only in-sync firmware copy. NRF54/Silabs/NRF-legacy/py-opendisplay remain
   on the old numbering (deep sleep = `0x0052`). Verified by prior audit pass.
4. **60 s deep-sleep clamp** (M3) is a silent host-invisible behaviour the
   contract doc still records as "not yet in firmware" — stale.
5. **No repo wires `sync_protocol_header.py --check` into CI**; this branch's
   header happens to be current, but nothing enforces it.

---

*Prior-pass power/device-control/peripherals findings (staging document,
2026-07-19) are incorporated above by reference with their original
severity/confidence labels and are credited "verified by prior audit pass".
This pass added H1, L2, L4, the zlib per-env analysis, and the R1–R4
verification results.*
