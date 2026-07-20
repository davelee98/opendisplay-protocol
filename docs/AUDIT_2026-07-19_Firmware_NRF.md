# Audit — Firmware_NRF (legacy nRF52811, bare C + Nordic SDK/SoftDevice)

**Repo:** `/home/davelee/opendisplay/Firmware_NRF`
**Branch / commit audited:** `main` @ `71b870c12855b2bab4db11fdf1fb213818f3f3a8` (tag `1.4`, "Small Bugfix")
**Scope:** application code only — `EPD/`, `main.c`, `encryption.c`, `config_parser.c`, `config_storage.c`, `led_control.c`, `constants.h`, `structs.h`. Vendored Nordic SDK under `SDK/` audited only for build-time invariants (dispatch model, WDT reload), not as target code.
**Method:** static analysis of the checked-out tree. No build/run.

Some cross-repo/opcode facts below are marked **(verified by prior audit pass)** — incorporated from `staging-nrf54-py-crossrepo.md` PART C rather than re-derived.

---

## Architecture overview

- Single-threaded bare-metal app over a Nordic SoftDevice. Two build flavours share the source: **S112 / SDK 17.1.0** (nRF52811) and a legacy **SDK 12.3.0** non-S112 path. `main()` runs a cooperative super-loop: `app_sched_execute()` + `idle_state_handle()` (WDT feed, MSD advertising refresh, button processing, `nrf_pwr_mgmt_run()`).
- **BLE events run in interrupt context.** `sdk_config.h` sets `NRF_SDH_DISPATCH_MODEL 0` (= `NRF_SDH_DISPATCH_MODEL_INTERRUPT`), so `nrf_sdh_evts_poll()` — and therefore the whole GATT-write → `epd_service_on_write` → `dispatch_command` chain — executes from the SoftDevice event IRQ (SWI), **not** from the main loop or the app_scheduler. The legacy build (`SOFTDEVICE_HANDLER_INIT(..., NULL)`) likewise dispatches from `SD_EVT_IRQHandler`. This single fact underlies the two top findings.
- **Own protocol header** (`EPD/EPD_service.h:67-83`), never vendored from canonical. The `sync_protocol_header.py` copy-map expects `Firmware_NRF/opendisplay_protocol.h`, **which does not exist** → `--check` reports MISSING. No repo wires the sync gate into CI. *(verified by prior audit pass)*
- **Command set** (dispatch `EPD/EPD_service.c:618`, switch `:621`): `0x000F` reboot, `0x0040/41/42` config read/write/chunk, `0x0043` firmware version, `0x0050` authenticate, `0x0051` enter-DFU, `0x0052` deep-sleep, `0x0070/71/72` direct-write start/data/end, `0x0073` LED activate. `0x0044 READ_MSD` is `#define`d (`EPD_service.h:79`) but **not dispatched**. No `0x45 / 0x75 / 0x76 / 0x77 / NFC / PIPE`. *(verified by prior audit pass)*
- **Security:** optional app-layer AES-CCM session (`encryption.c`). Challenge/response over master key → derived session key; per-message CCM with 13-byte nonce (session-id[8] ‖ counter[8], AAD = 2 opcode bytes, 12-byte tag). Replay window ±32, integrity-failure lockout (3), auth rate-limit (10/60 s), 30 s challenge expiry. BLE link itself is **unpaired / `SEC_OPEN`** (no LESC), so all confidentiality/integrity lives in this app layer.
- **Config persistence:** FDS record (`config_storage.c`), CRC32-guarded, `MAX_CONFIG_SIZE 512`.

---

## Findings by severity

### HIGH

#### H1 — Long/unbounded blocking operations run in the BLE interrupt handler; the watchdog feed is defeated by a frozen timestamp → mid-operation reset / hang
**Files:** `EPD/EPD_service.c:518-543` (refresh wait), `EPD/UC81xx.c:64` & `EPD/SSD16xx.c:87` (`WaitBusy(UINT16_MAX)`), `EPD/EPD_driver.c:204-218` (`EPD_WaitBusy` + `app_feed_wdt`), `main.c:186-191` (`app_feed_wdt`), `main.c:277-283` / `:104` (timestamp advanced only from `app_timer` callback), `constants.h:4` (`EPD_REFRESH_TIMEOUT_MS 60000`).
**Confidence:** Confirmed (mechanism); Plausible (field frequency, panel-dependent).

Because `NRF_SDH_DISPATCH_MODEL = 0`, a `CMD_DIRECT_WRITE_END` (`0x72`) is handled inside the SoftDevice event ISR. `handle_direct_write_end` calls `drv->refresh()`, whose e-paper busy-wait is `WaitBusy(UINT16_MAX)` — up to ~65 s of `delay(1 ms)` spinning — and then adds its *own* busy loop `:523-530` up to `EPD_REFRESH_TIMEOUT_MS = 60000 ms`. The whole time the CPU is spinning at SWI priority, so:

1. The main super-loop cannot run, so the normal `app_feed_wdt()` in `idle_state_handle()` never executes.
2. `EPD_WaitBusy` *does* call `app_feed_wdt()` every 100 iterations, **but** `app_feed_wdt()` only actually feeds when `m_timestamp - m_wdt_last_feed_time >= WDT_FEED_INTERVAL_SEC (30)`. `m_timestamp` is incremented **only** by the 1 Hz `clock_timer` callback, which runs at ≤ the SWI dispatch priority (or via the starved app_scheduler) and therefore **cannot preempt the busy-wait**. `m_timestamp` is frozen for the entire refresh → the feed condition never becomes true → the watchdog is never actually kicked.
3. `handle_direct_write_end`'s own post-refresh loop `:523-530` calls neither `app_feed_wdt()` nor `EPD_WaitBusy`, so it never even attempts a feed.

WDT reload is the nrfx default `NRFX_WDT_CONFIG_RELOAD_VALUE = 2000 ms` (`SDK/.../nrfx_wdt.h:85` ← `sdk_config.h:4104`; `NRF_DRV_WDT_DEAFULT_CONFIG → NRFX_WDT_DEAFULT_CONFIG`). A legacy `WDT_CONFIG_RELOAD_VALUE = 60000` also exists but the nrfx path wins. **Failure scenario:** any refresh longer than the reload window (color / large panels — the model table includes 7.5" and BWRY panels that refresh 15-30 s; even a BW refresh can exceed 2 s) trips the watchdog *during* the refresh → device resets before the display finishes and before the success/timeout ACK is sent. The client re-drives the upload → reboot loop. Even below the WDT bound, the entire device (BLE responsiveness, timekeeping, buttons) is frozen for the whole refresh. The same blocking-in-ISR pattern applies to `saveConfig()`'s up-to-1 s FDS wait and to H2 below.

**Note:** this is an architecture-level issue. Correct pattern would defer command execution off the SoftDevice event context (app_scheduler / main-loop worker), exactly as Firmware_NRF54 does via `K_MSGQ` *(per prior NRF54 pass, PART A)*.

#### H2 — `LED_ACTIVATE` (0x73) runs the LED animation synchronously in the ISR; `grouprepeats == 255` is an unbounded loop that never returns
**Files:** `EPD/EPD_service.c:552-605` (`handle_led_activate` sets `flash_active=true`, calls `ledFlashLogic()`, only clears it *after* return), `led_control.c:104-131`.
**Confidence:** Confirmed.

`ledFlashLogic()` loops `while (ledFlashActive)`. The only in-loop exit is `if (ledFlashPosition >= grouprepeats && grouprepeats != 255) break;` (`led_control.c:105`). `grouprepeats = ledcfg[10] + 1`. A command byte `ledcfg[10] == 254` makes `grouprepeats == 255`, which **disables the break** (the "255 = run forever" sentinel), and `ledFlashActive` is not cleared until `ledFlashLogic()` returns — which now never happens. **Failure scenario:** a single `0x73` frame with that byte hangs the device permanently inside the ISR; the watchdog (H1: no feed possible, timestamp frozen) resets it. Even for finite `grouprepeats`, the animation (`hal_delay_ms` up to `15 * 100 ms` per inner step × up to 15 steps × 3 groups × up to 254 repeats — many minutes) blocks the whole device and exceeds the WDT window. When encryption is disabled, any connected peer can send this; when enabled, any authenticated peer (and see M2 — a *short* `0x73` frame bypasses encryption entirely).

---

### MEDIUM

#### M1 — Reentrant `nrf_sdh_evts_poll()` inside `saveConfig`, called from the write ISR
**Files:** `config_storage.c:134-143` (poll loop), reached from `EPD/EPD_service.c:238` / `:327` (`saveConfig` in the config-write/chunk handlers), which run in the event ISR.
**Confidence:** Plausible.

`saveConfig()` blocks for the FDS write completion by calling `nrf_sdh_evts_poll()` (S112) in a spin loop. But `saveConfig()` is itself invoked from `epd_service_on_write`, which the SoftDevice already dispatched *from* `nrf_sdh_evts_poll()`. Re-invoking it re-enters SoftDevice event dispatch from within an event handler: any BLE write that arrived meanwhile is dispatched **reentrantly**, running `epd_service_on_write` again and mutating the shared `static chunkedWriteState` / `directWriteState` (and possibly starting a nested `saveConfig`) underneath the outer call. **Failure scenario:** a client that pipelines a config chunk followed immediately by another write can corrupt the chunk-assembly state machine or nest flash operations. Bounded by BLE timing, hence Plausible rather than Confirmed, but it is an unguarded reentrancy hazard on non-reentrant static state.

#### M2 — Encryption enforcement has a size hole: authenticated sessions dispatch commands < 31 bytes as plaintext
**File:** `EPD/EPD_service.c:705` (`if (length >= 31)` = "treat as encrypted") vs `:748-761` (fall-through: authenticated + short → `dispatch_command` on raw `&p_data[2]`).
**Confidence:** Confirmed (path traced); exploitation constrained.

With encryption enabled the code decides "is this frame encrypted?" purely by length: `>= 31` (2 hdr + 16 nonce + ≥1 ct + 12 tag) is decrypted; anything shorter, if the session is already authenticated, is dispatched **as cleartext** with no CCM, no replay-window check, no per-command integrity. The sensitive control commands are all short: `REBOOT 0x000F`, `ENTER_DFU 0x0051`, `DEEP_SLEEP 0x0052`, `CONFIG_READ 0x0040`, `DIRECT_WRITE_START 0x0070`, short `LED_ACTIVATE 0x0073` (→ H2) are 2-15 bytes and thus execute unencrypted once any session is authenticated. The AEAD/replay guarantees the security layer is supposed to enforce are silently skipped for exactly these commands. **Mitigating reality:** the BLE link is a single connection and unpaired; an off-path attacker cannot inject into an established connection, so the practical actor is the already-authenticated peer (or a peer that can occupy the link). This lowers real-world impact to a defense-in-depth / spec-conformance gap rather than a remote bypass — hence Medium. (Config *responses* are still encrypted regardless, because `send_response` re-encrypts on the authenticated session, so `CONFIG_READ` does not leak stored config in cleartext.)

---

### LOW

#### L1 — Deep-sleep opcode `0x0052` diverges from canonical v2.1; no timed-wake payload
`EPD_service.h:83` `CMD_DEEP_SLEEP 0x0052`; handler `handle_deep_sleep` (`:607`) ACKs `{00,0x52}` then `enter_deep_sleep()` (System OFF, wake only on reset/power). Canonical v2.1 defines `0x0052 = CMD_POWER_OFF`, `0x0053 = CMD_DEEP_SLEEP (+ optional [seconds:2 BE])`. This firmware (and py-opendisplay) still use the pre-2.1 numbering; a canonical/ESP32 host sending `0x0053` hits `default` (no-op) and one sending `0x0052` (power-off) triggers deep-sleep here. No `[seconds]` parse → no timed wake. **Confirmed, cross-repo — *(verified by prior audit pass, PART C)*.** Impact is interop, not local safety.

#### L2 — `CMD_READ_MSD 0x0044` declared but unreachable
`EPD_service.h:79` defines it; `dispatch_command` has no case → falls to `default` (silent debug log). MSD is instead surfaced via advertisement (`updatemsdata`, `main.c:127`). A canonical host issuing `0x44` gets no response. **Confirmed, staged.**

#### L3 — Single-frame config write of exactly 201 bytes silently drops one byte
`EPD_service.c:219-233`: `len == 201` is `> CONFIG_CHUNK_SIZE (200)` but `< CONFIG_CHUNK_SIZE_WITH_PREFIX (202)`, so it takes the no-prefix branch, sets `totalSize = 201` / `expectedChunks = 1`, but `memcpy`s only `min(201,200) = 200` bytes — byte `data[200]` is lost — then ACKs and waits for a `0x42` chunk that a well-formed client won't send. Malformed input per the wire contract, but it loses data instead of NACKing. Directly analogous to Firmware_NRF54 finding A8 *(prior pass)*. **Confirmed.**

#### L4 — Deep-sleep ACK is not TX-confirmed before System OFF
`handle_deep_sleep` (`:610-615`) queues the encrypted ACK via `send_response`, then `nrf_delay_ms(100)`, then `enter_deep_sleep()` which disconnects (`+200 ms`) and calls `sd_power_system_off`. There is no wait on `BLE_GATTS_EVT_HVN_TX_COMPLETE`. The SoftDevice radio does run during the busy-wait and a pending notification overrides slave latency, so with `MAX_CONN_INTERVAL 30 ms` the 100 ms window usually suffices — but delivery is best-effort, not guaranteed. **Plausible, minor.** (`enter_deep_sleep` masks all NVIC IRQs before the SVC — harmless, since `SVCall` is a non-maskable system exception.)

#### L5 — Length-heuristic fragility (informational)
Same `length >= 31` gate (`:705`): a *legitimate plaintext* command ≥ 31 bytes sent while encryption is enabled is misinterpreted as ciphertext and fails decryption → NACK. By design (everything large must be encrypted), but it is a foot-gun coupling frame semantics to size.

---

### Positives verified (no action)
- Constant-time comparisons for the auth MAC and nonce session-id (`encryption.c:356`, `:226` via `ocrypto_constant_time_equal`) — unlike NRF54's `memcmp` (its A6).
- Replay window ±32 with 64-slot dedup, integrity-failure lockout, auth rate-limit, 30 s challenge expiry, nonce-counter wrap invalidation (`encryption.c:204-260`, `:275-290`, `:325`).
- Direct-write RAM assembly is correctly bounded to `total_bytes` per plane with overrun drop and a `≤ EPD_SPI_CHUNK_SIZE (200)` stack chunk buffer (`EPD_service.c:437-495`) — no OOB.
- `config_parser.c` is heavily bounds-guarded (per-packet `offset + sizeof(...) <= configLen - 2` before every `memcpy`, count caps of 4).
- `decryptCommand` validates ciphertext length and the inner declared payload length against buffer size (`encryption.c:455-484`).
- AES-CBC-with-zero-IV correctly emulates single-block ECB in `aes_ecb_encrypt` (`encryption.c:32-39`).
- Session cleared on disconnect (`EPD_service.c:75-78`) and direct-write state torn down + EPD put to sleep on disconnect (`:79-82`).

---

## Unimplemented / partial features vs canonical v2.1

| Canonical command | Opcode | Firmware_NRF state | Notes |
|---|---|---|---|
| CMD_REBOOT | 0x000F | ✅ implemented | `NVIC_SystemReset` |
| CMD_CONFIG_READ / WRITE / CHUNK | 0x40/41/42 | ✅ implemented | chunked assembly; L3 edge bug |
| CMD_FIRMWARE_VERSION | 0x0043 | ✅ implemented | major.minor + SHA |
| CMD_READ_MSD | 0x0044 | ⚠️ declared, **not dispatched** (L2) | MSD only via advertisement |
| CMD_CONFIG_CLEAR | 0x0045 | ❌ absent | erase-only via reset-pin / rewrite flag path |
| CMD_AUTHENTICATE | 0x0050 | ✅ implemented | full CCM handshake, timing-safe |
| CMD_ENTER_DFU | 0x0051 | ✅ implemented | honest: sets GPREGRET + reset (S112 buttonless DFU) |
| CMD_POWER_OFF | 0x0052 (v2.1) | ⚠️ opcode collision — 0x52 is DEEP_SLEEP here (L1) | pre-2.1 numbering |
| CMD_DEEP_SLEEP | 0x0053 (v2.1) | ❌ absent (device uses 0x0052) | no `[seconds]` timed wake |
| CMD_DIRECT_WRITE_START/DATA/END | 0x70/71/72 | ✅ implemented | blocking refresh in ISR (H1) |
| CMD_LED_ACTIVATE | 0x0073 | ✅ implemented | synchronous animation in ISR; unbounded-loop bug (H2) |
| CMD_LED_STOP | 0x0075 | ❌ absent | |
| CMD_DIRECT_WRITE_PARTIAL_* | 0x76 | ❌ absent | |
| CMD_BUZZER_ACTIVATE | 0x0077 | ❌ absent | no buzzer hardware |
| CMD_NFC_ENDPOINT | 0x0083 | ❌ absent | no NFC |
| PIPE_WRITE_* | 0x80-0x82 | ❌ absent | matches @targets "minimal" |

Matches the canonical header's "minimal target" expectation for this legacy chip, except the READ_MSD gap (L2) and the deep-sleep opcode drift (L1).

---

## Cross-repo observations

- **Header drift is total and un-gated.** This repo never vendored `opendisplay_protocol.h`; it carries its own opcode `#define`s. The sync map's expected path `Firmware_NRF/opendisplay_protocol.h` does not exist, so `sync_protocol_header.py --check` reports MISSING — and no repo runs that check in CI. *(verified by prior audit pass, PART C.)*
- **Deep-sleep opcode consensus (old numbering):** Firmware_NRF `0x52`, Firmware_NRF54 `0x52`, Firmware_Silabs `0x52`, py-opendisplay `0x52` all agree with each other; **canonical v2.1 and the shipped ESP32 Firmware use `0x53` (deep-sleep) / `0x52` (power-off)**. A py-opendisplay `deep_sleep()` reaches this firmware correctly today but will hit `CMD_POWER_OFF` on 2.1 targets. *(PART C.)*
- **Same architectural anti-pattern, opposite handling from NRF54:** Firmware_NRF54 defers command processing off the BT RX thread onto a main-thread `K_MSGQ` (prior pass, A) — the correct model. Firmware_NRF instead executes everything, including multi-second blocking display/LED/flash work, directly in the SoftDevice event ISR (H1/H2). Porting the NRF54 deferral model here would resolve both HIGH findings.
- **Auth handshake matches NRF54/canonical field order** (`server_nonce ‖ client_nonce ‖ device_id` for the challenge MAC, `encryption.c:341-344`), and — unlike NRF54's early-exit `memcmp` (its A6) — uses constant-time compares throughout.
