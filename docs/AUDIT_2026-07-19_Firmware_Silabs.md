# AUDIT — Firmware_Silabs (EFR32BG22, Simplicity SDK)

**Audited:** `/home/davelee/opendisplay/Firmware_Silabs`
**Branch/commit:** `main` @ `7efd0635d2877611fd2f6308eadc889c9f551a92` (tag `2.2`, "fix streaming compression flag", 2026-07-16)
**Date:** 2026-07-19 · Static analysis only, no build/run.
**Canonical ground truth:** `opendisplay-protocol/src/opendisplay_protocol.h` v2.1 (Part D staging extract).
**Method:** traced the full BLE→pipe→handler application code (`opendisplay_pipe.c`, `opendisplay_ble.c`, `opendisplay_config_parser.c`, `opendisplay_config_storage.c`, `opendisplay_display.cpp`, `app.c`, `main.c`). Prior-pass cross-repo/opcode facts (Part C/D) incorporated and credited.

---

## Architecture overview

Single-threaded Silicon Labs Bluetooth (BGAPI) super-loop. `main.c` → `sl_main_process_action()` + `app_process_action()`; `app.c:sl_bt_on_event()` dispatches every stack event to `opendisplay_ble_on_event()`, which forwards **all** events to `opendisplay_pipe_handle_gatt_event()` (ble.c:1834) before its own connection bookkeeping. GATT writes to the pipe characteristic arrive as `sl_bt_evt_gatt_server_attribute_value_id` → `on_pipe_write()` → `dispatch()` (pipe.c:1067/1082). **No cross-thread races**: stack callbacks and app processing are serialized on one context (contrast Firmware_NRF54's K_MSGQ hand-off). Long operations (EPD refresh up to 60 s, reboot spin) run *inside* the event handler and block stack servicing while active.

Deferred actions (DFU, deep sleep) are set as flags in the pipe handler and actioned later in `opendisplay_ble_process()` (ble.c:1880), which also enforces a 300 s max-connection timeout. `advertiser_stop` is called on `connection_opened` ("single-client mode", ble.c:1844), so in practice only one connection is live at a time even though `SL_BT_CONFIG_MAX_CONNECTIONS=4`.

Security: per-session AES-128-CCM envelope (hand-rolled CCM over PSA AES-ECB, pipe.c:236-355), AES-CMAC challenge/response auth (pipe.c:581-680), 64-entry ±32 replay window (pipe.c:362-405). Session state (`s_session`) and connection id (`g_connection`) are **single globals**.

Vendored `opendisplay_protocol.h` is a hand-written ~50-line stub (old opcode numbering), NOT the 861-line canonical — `sync_protocol_header.py --check` would FAIL (verified by prior audit pass).

Endianness spot-checked against Part D — **all correct**: opcodes BE (pipe.c:1224); NFC `len`/`total_len` BE (pipe.c:949, 980) and response `len` BE (pipe.c:935-936); config chunk#/total-len LE (pipe.c:760-765 read, 807 write); direct-write compressed size LE 4B (display.cpp:783-787); CCM nonce=`nonce[3..15]`, AAD=2 opcode bytes BE (pipe.c:438-440); auth CMAC input order `server_nonce‖client_nonce‖device_id` (pipe.c:636-638); config wire CRC-16/CCITT-FALSE, init 0xFFFF, poly 0x1021, first 2 bytes zeroed (config_parser.c:17-48).

---

## Findings by severity

### CRITICAL

**C1 — NFC single-shot write: uint16 length-guard overflow → remote global-buffer OVERWRITE (~64 KB).** *Confirmed (traced end-to-end).*
`opendisplay_pipe.c:950` guards the single-write (`sub==0x01`) path with:
```c
text_len = (payload[2] << 8) | payload[3];      /* 0..0xFFFF, attacker-controlled */
if ((uint16_t)(4u + text_len) > payload_len) { error; }   /* SUM TRUNCATED TO uint16 */
```
`text_len = 0xFFFF` makes `(uint16_t)(4+0xFFFF) = 3`, which is `<= payload_len`, so the guard **passes** and `opendisplay_ble_nfc_write(rec_type, &payload[4], 0xFFFF)` is called (pipe.c:960). On Silabs the NFC backend is **real** (unlike NRF54's stub). In `od_nfc_write_record_raw` (ble.c:1252):
- `rec_type = OD_NFC_REC_TEXT` (0): `payload_len = (uint16_t)(1+2+0xFFFF) = 2`, passes the `>255 / >508` checks, then **`memcpy(&blocks[7], data, data_len)` with `data_len = 0xFFFF`** (ble.c:1282).
- `rec_type = OD_NFC_REC_URI` (1): `payload_len = (uint16_t)(1+0xFFFF) = 0`, passes, then `memcpy(&blocks[5], data, 0xFFFF)` (ble.c:1294).

`blocks` is the 512-byte global `s_od_nfc_write_blocks` (ble.c:129), immediately followed in `.bss` by `s_od_nfc_read_data[128]`, `s_od_nfc_read_throwaway[16]`, and further globals. The copy writes up to 65 535 bytes → massive out-of-bounds **write** (memory corruption / likely code-exec primitive), plus an OOB **read** of the same span from the ≤244/512-byte frame buffer.
- **Reachability:** requires NFC enabled in the device config. If encryption is enabled, requires a live authenticated session (the NFC single-write frame is >31 bytes so it goes through the CCM path); if encryption is disabled, unauthenticated.
- This is the *same bug class* the prior pass flagged as NRF54 **A3**, but there it was read-only and defanged by a stub backend. On Silabs it is **live and a write-overflow.** Systemic pattern in `handle_nfc_endpoint`'s single-write guard.
- The chunked-write path is **safe**: `total_len` is capped to `≤512` (pipe.c:986) and per-chunk accumulation is bounds-checked (pipe.c:1016). `WELL_KNOWN_RAW`/`MIME`/`RAW_NDEF` rec types are safe (their internal `>255`/`>512` checks reject the wrapped lengths). Only TEXT and URI single-write are exploitable.
- **Fix direction:** validate `text_len` against `payload_len` in 32-bit arithmetic (`(uint32_t)4 + text_len > payload_len`), and have `od_nfc_write_record_raw` bound `data_len` before any memcpy.

---

### HIGH

**H1 — Deep-sleep / power-off opcode drift from canonical v2.1 (stale vendored header).** *Confirmed (verified by prior audit pass, corroborated here).*
Vendored `opendisplay_protocol.h:24` defines `CMD_DEEP_SLEEP 0x0052` (old numbering); no `CMD_POWER_OFF`, no `0x0053`. `dispatch()` maps `0x0052` → ACK `{00,0x52}` then schedules EM4 (pipe.c:1111-1117). Canonical v2.1 **swapped** these: `0x0052 = CMD_POWER_OFF`, `0x0053 = CMD_DEEP_SLEEP`. Consequences for a canonical/v2.1 host:
- Sends `POWER_OFF (0x0052)` expecting Silabs to NACK `[FF][52][00]` (Part D) → instead the device **ACKs and enters EM4 deep sleep.**
- Sends `DEEP_SLEEP (0x0053)` → falls to `default:` "unknown cmd" (pipe.c:1176), **no response** → client hangs waiting for the ACK.
NFC is correctly on `0x0083` (matches canonical, unlike NRF54's stale `0x0082`). `sync_protocol_header.py --check` would FAIL for this repo and no CI wires it (prior pass, Part C).

---

### MEDIUM

**M2 — Encryption/replay bypass for short commands via frame-length discriminator.** *Plausible.*
`on_pipe_write()` routes a frame through decrypt + replay-window only when `sec_enabled() && cmd != AUTH && frame_len >= 31` (pipe.c:1225). Any frame `< 31` bytes skips decryption and is dispatched as **plaintext** (pipe.c:1238). The `dispatch()` gate (pipe.c:1076) only checks `session_alive()`, not whether the frame was actually encrypted. So while a session is alive, 2-byte plaintext commands — `REBOOT 0x0F`, `DEEP_SLEEP 0x52`, `ENTER_DFU 0x51`, `CONFIG_READ 0x40` — execute with **no CCM integrity and no replay protection**. On an unpaired link (no BLE link-layer encryption — no bonding requirement observed on the pipe characteristic), an injected/replayed plaintext short frame would run without possession of the session key. The canonical spec itself lacks an in-band plaintext/ciphertext discriminator (Part D "SPEC GAP"); `frame_len>=31` is Silabs' heuristic and it under-covers the short sensitive opcodes.

**M3 — Config wire CRC-16 computed but NOT enforced.** *Confirmed.*
`parseConfigBytes()` computes the toolbox outer CRC-16 and, on mismatch, only `printf`s "CRC mismatch" then proceeds to `globalConfig->loaded = true; return true;` (config_parser.c:490-503). A corrupt or truncated config body — including a malformed `SecurityConfig` (0x27) that becomes the live `s_od_security_parsed` — is accepted and applied. The NVM storage layer's own CRC-32 (`loadConfig`, config_storage.c:133) is enforced, but it only protects flash integrity, not the wire-supplied config. Config writes are auth-gated only when security is already enabled; pre-provisioning they are open.

**M4 — Config-write 201-byte single frame stalls / drops a byte.** *Confirmed.*
`handle_config_write()` with `len==201` (`> CONFIG_CHUNK_SIZE`(200) but `< CONFIG_CHUNK_SIZE_WITH_PREFIX`(202)) takes the no-prefix else branch (pipe.c:824): `total_size=201`, copies `min(201,200)=200`, `received_size=200 < 201`, then waits for a `0x42` continuation to deliver the "missing" byte instead of NACKing a malformed frame. Canonical requires a chunked first frame to be a full 202 bytes. Same class as NRF54 **A8** (data loss instead of rejection). Low impact but wrong handling.

---

### LOW

**L1 — Deep-sleep ACK vs EM4 flush.** *Plausible.* `dispatch()` queues the ACK notification via `pipe_send` then `schedule_deep_sleep()` (pipe.c:1113-1116). `opendisplay_ble_process()` first closes the connection (returns) and only enters `EMU_EnterEM4()` on a later tick after the close completes (ble.c:1910-1927). This ordering gives the ACK a flush window (better than immediate EM4), but there is no explicit tx-confirmed wait; if the stack discards the queued notification on `connection_close`, the client never sees the ACK. Wake sources (button + NFC field-detect EM4 interrupts) are armed before EM4 (ble.c:1924-1925) — correct.

**L2 — Global session/connection state with MAX_CONNECTIONS=4.** *Plausible.* `s_session` and `g_connection` are single globals; `session_alive()` is not per-connection. Mitigated by `sl_bt_advertiser_stop` on `connection_opened` (ble.c:1844) enforcing single-client mode, and by `opendisplay_pipe_on_connection_closed()` clearing the session on any disconnect. Residual: a second central connecting in the window before `advertiser_stop` takes effect would share the first client's authenticated `session_alive()` and could dispatch commands. Latent design smell rather than a demonstrated break given the single-client guard.

**L3 — FW_VERSION (0x43) / READ_MSD (0x44) auth-gated pre-session.** *Confirmed.* When security is enabled and no session exists, the `dispatch()` gate (pipe.c:1076) returns AUTH_REQUIRED for everything except AUTH — including the "always-plaintext" capability reads `0x43`/`0x44`. A client probing firmware version before authenticating gets `[00][43][0xFE]` instead of the version. Responses are correctly forced plaintext when they do run (pipe.c:532-533). Minor divergence from canonical "always-plaintext, capability" intent.

**L4 — AUTH_STATUS_ALREADY (0x02) never emitted.** *Confirmed.* Re-issuing the challenge step while authenticated silently `clear_session()`s (pipe.c:620-622) rather than returning `AUTH_STATUS_ALREADY`. Cosmetic protocol divergence.

**L5 — Single-slot config-chunk state not connection-guarded at start.** *Confirmed.* `handle_config_write()` unconditionally `cfg_chunk_reset()`s and claims `s_cfg_chunk` for the caller; only `handle_config_chunk()` (the 0x42 continuation) checks `connection` (pipe.c:872). A new 0x41 clobbers an in-progress transfer. Low impact under single-client mode.

**L6 — Event-loop stalls.** *Confirmed.* `CMD_REBOOT` busy-waits 800 000 iterations before `NVIC_SystemReset()` (pipe.c:1100); direct-write END blocks up to 60 s in `wait_for_refresh` inside the event handler (display.cpp:882). Both block BLE stack servicing while active. Acceptable for this device class but noted.

---

## Unimplemented-or-partial features

| Feature / opcode | State on Silabs | Notes |
|---|---|---|
| `CMD_POWER_OFF 0x52` (canonical v2.1) | **Not implemented** | 0x52 is consumed as deep-sleep (H1). Canonical expects NACK `[FF][52][00]`. |
| `CMD_DEEP_SLEEP 0x53` (canonical v2.1) | **Not implemented** | Falls to `default:` → silent unknown-cmd; client hangs (H1). |
| `[seconds:2 BE]` deep-sleep duration | Not implemented | Payload ignored; bare EM4 entry. |
| `CMD_CONFIG_CLEAR 0x45` | Not implemented | Matches @targets legend (Part D); contradicts canonical 0x45 block — documented spec inconsistency, not a firmware bug. |
| `0x76` PARTIAL_START / `0x77` BUZZER | Explicit NACK `{FF, cmd, 0x07, 00}` | Intentional fast-fail; matches client `parse_nack()` (pipe.c:1164-1173). Header stub still `#define`s them (protocol.h:17-18) only to NACK. |
| PIPE_WRITE `0x80-0x82` | Not implemented | Expected — Silabs has no pipe/sliding-window. |
| NFC `0x83` (read / single-write / chunked write) | **Fully implemented — the only real NFC in the ecosystem** | Read + write drive a TNB132M over bit-banged GPIO. Single-write path carries the C1 overflow. |
| Direct-write `0x70/0x71/0x72` (raw + zlib stream) | Implemented, bounds-clean | Data streamed straight to EPD; compressed path validated `decompressed==expected` (display.cpp:788) and `written<=total` (display.cpp:666); no RAM overflow found. |
| Config `0x40/0x41/0x42`, `0x43`, `0x44`, AUTH `0x50`, DFU `0x51`, LED `0x73/0x75`, REBOOT `0x0F` | Implemented | See findings for gate/edge issues. |

---

## Cross-repo observations

- **Vendored header drift (systemic):** `Firmware_Silabs/opendisplay_protocol.h` is a ~50-line hand-written stub on the pre-swap numbering. Byte-for-byte sync with canonical fails; no repo wires `sync_protocol_header.py --check` into CI (verified prior pass, Part C). Silabs is on the **old** deep-sleep/power-off numbering (`0x52`=deep-sleep) but the **new** NFC numbering (`0x83`) — a mixed state that differs from both canonical (0x53/0x83) and NRF54 (0x52/0x82).
- **NFC uint16 overflow is a shared pattern:** the identical length-guard-truncation bug appears in NRF54 (`A3`, read-only, stub-defanged) and Silabs (`C1`, write, live). The canonical NFC sub-protocol spec (`0x01` = `[rec_type][len:2 BE][payload]`) invites this because `len` is a full uint16 and reference handlers add a small constant before comparing to `payload_len`. Worth a spec note / shared reference-handler fix.
- **Client interop:** py-opendisplay sends deep-sleep on `0x0052` (prior pass B1a) — which *matches* this Silabs firmware and works today, but both diverge from canonical v2.1 and from the ESP32 `Firmware` branch that adopted the swap. The `0x0076`/`0x0077` explicit-NACK-`0x07` contract is deliberately aligned with the client's `parse_nack()` fallback.
- **Endianness & crypto envelope:** verified conformant to Part D field-for-field (opcodes/NFC lengths BE, config lengths LE, CCM nonce/AAD, CMAC input order, config CRC-16 parameters). No endianness defects found.
