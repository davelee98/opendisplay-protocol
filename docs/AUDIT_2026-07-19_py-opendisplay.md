# AUDIT — py-opendisplay (BLE upload library)

- **Repo:** `/home/davelee/opendisplay/py-opendisplay`
- **Branch:** `fix/ble-notification-watcher-leak`
- **Commit audited:** `d99313a` ("fix(transport): close all BLE notification-watcher leak paths")
- **Method:** Static read-only analysis. No build/test/install run. Canonical wire spec: `opendisplay-protocol/src/opendisplay_protocol.h` v2.1.
- **Scope of this pass (gap-fill):** PIPE_WRITE sliding-window algorithm, asyncio lifecycle/races, session/auth layer. Serialization/constants findings from the prior verified pass (staging PART B) are incorporated below with original severity, credited **"verified by prior audit pass."**

---

## Architecture overview

`OpenDisplayDevice` (device.py, 2763 lines) is the public API; `BLEConnection` (transport/connection.py) wraps `bleak` + `bleak-retry-connector`. Wire framing lives in `protocol/commands.py` (builders) and `protocol/responses.py` (parsers); AES-128-CCM/CMAC in `crypto.py`. py-opendisplay uses **hand-written** protocol constants (`protocol/commands.py`) and does **not** import the generated `opendisplay_protocol.py` mirror — this is where constant drift lives (PART B).

Command flow is serialized by a **reentrant per-task lock** (`_serialized` decorator → `_transaction` CM, device.py:383, 631). All I/O funnels through `_write` (encrypt + re-auth check) and `_read` (decrypt + 0xFE/0xFF handling). A single unbounded `asyncio.Queue` (connection.py:66) buffers inbound notifications; reads are stop-and-wait with a pre-command `drain_notifications()` (connection.py:343) to prevent response/command desync.

The image path has three transports, tried in order with graceful fallback: **PIPE_WRITE sliding window** (0x0080–0x0082, `_negotiate_pipe*`/`_run_pipe_upload`/`_send_pipe_chunks`), **legacy compressed/uncompressed 0x70–0x74**, and **legacy partial 0x76**. Only ESP32 `Firmware` implements PIPE_WRITE (per canonical `@targets`); all other targets fall through to legacy — silence on the 0x0080 probe returns `None` and the caller degrades cleanly.

---

## PIPE_WRITE sliding-window deep-dive — RESULT: no correctness defects found

This was the highest-value gap. The QUIC-style selective-repeat sender (`_send_pipe_chunks`, device.py:2525–2697) and the SACK decoder (`unpack_ack_ranges`, responses.py:404–437) were traced against the canonical spec (header lines 587–594). **Conclusion: the algorithm is sound and well-bounded.** Details, because absence-of-bug here is load-bearing:

- **Mask semantics match canonical exactly.** Header: "mask bit i (LSB first) = chunk (highest_seen-1-i) received; highest_seen is implicitly acked." `unpack_ack_ranges` implements precisely this (responses.py:431–436). The 33-slot reorder queue = `highest_seen` + 32 mask bits; with window ≤ 32 all in-flight frames are always representable (h_abs .. h_abs-32 spans 33 indices). No mask overflow. **Confirmed.**
- **Seq-wrap resolution is unambiguous.** `highest_seen` is mod-256; resolved against `window_base` via `delta=(highest_seen-base_mod)%256` with the `>128 → −256` fold (responses.py:423–427). In-flight span ≤ window ≤ 32 ≪ 256, so resolution (including stale ACKs that sit just below `window_base`) is exact. No wrap/off-by-one. **Confirmed.**
- **Window arithmetic** is span-based: `(next_to_send - window_base) < window` (device.py:2586). `w_eff = max(1, min(req, dev_max, 32))`, `n_eff = max(1, min(req, dev_max, w_eff))` (device.py:2326–2327) — min-rule, clamped to 1..32, cannot yield 0. **Confirmed.**
- **No over-advance from stale/reordered ACKs.** Selective mode: `next_to_send` monotonic, so a stale ACK's `h_abs ≤ next_to_send`; `window_base` cannot pass sent frames. Non-selective (rewind) mode: only cumulative bits are set by a non-buffering receiver, so advancing `window_base` to `highest_seen` is delivery-safe. Traced both branches. **Confirmed.**
- **Liveness is triple-bounded** — no infinite loop is reachable: `pto_count ≥ MAX_PTO (3)` (silent timeouts), `retx_count > max_retx (3·window)` (retransmits), and `stall_acks > max_retx` (ACKs without cumulative progress or exposed hole, device.py:2662–2673). Every non-progress path increments a bounded counter that aborts with `ProtocolError`. **Confirmed.**
- **Chunk-capacity math is exact** (`_pipe_data_size`, device.py:2458–2466). Encrypted: `frame − 31 − 1`; recomputing the CCM envelope (`cmd2+nonce16+innerlen1+tag12=31`, plus a 1-byte inner `seq`) gives `frame − 32` — matches. Plaintext: `frame − 3` (`cmd2+seq1`). No off-by-one. **Confirmed.**
- **Tail-flush / auto-complete vs explicit-END split** (device.py:2568, 2596–2617, 2699–2742) is coherent: uncompressed-full auto-completes (unsolicited `{00,82}`) and the sender keeps reading; compressed/partial use explicit END; a sub-N_eff tail that can never earn a cadence ACK is dup-probed under a short timeout rather than stalling. Partial+auto-complete is explicitly rejected as a contract violation (device.py:2502–2506).

---

## Findings by severity

### HIGH

**H1 — DEEP_SLEEP emitted on 0x0052; canonical v2.1 reassigned 0x0052=CMD_POWER_OFF, 0x0053=CMD_DEEP_SLEEP**
`protocol/commands.py:40` (`DEEP_SLEEP = 0x0052`), used at `:154`. Against any firmware that adopted v2.1 (the ESP32 `Firmware` branch already has), `deep_sleep()` invokes **CMD_POWER_OFF — a hard rail-cut, button-only wake** — instead of deep sleep. Client + NRF54/Silabs/NRF52811 all still agree on the *old* 0x0052 numbering, so it works there, but the client is on a collision course with the canonical/ESP32 target. *Verified by prior audit pass (B1a). Confirmed.*

**H2 — `deep_sleep()` conflates every 0xFF52 refusal as "unsupported"**
`device.py:1147-1148`; the NACK sub-status `data[0]` is never read. Per header §4c, `0x01 DISABLED` / `0x02 NOT_BATTERY` are *rejected-while-awake*; only `0x00` means unsupported. Capability reporting to HA is therefore wrong for two of three refusal reasons. *Verified by prior audit pass (B4a). Confirmed.*

### MEDIUM

**M1 — Mid-stream re-authentication can be injected into a live legacy 0x71 image transfer (NEW this pass)**
`_write` calls `_reauthenticate_if_needed` on every encrypted write (device.py:696-698). The PIPE_WRITE path deliberately bypasses this via `_write_pipe_frame` (device.py:702-710, "never mid-stream"), **but the legacy data-chunk senders do not** — both `_send_data_chunks` (device.py:2244) and `_send_partial_chunks` (device.py:1860) send each 0x71 chunk through `_write`. If a large/slow upload crosses 90% of `session_timeout_seconds` (device.py:712-728), a full 3-frame AUTHENTICATE handshake (`build_authenticate_step1/step2`) is transmitted *between two image-data chunks*, and `_nonce_counter` is reset to 0 under a new `session_id`. `authenticate()` additionally `drain_stale=True`-drains the queue mid-stream (connection.py:394). Firmware reassembling an in-progress direct-write stream would see auth opcodes interleaved with data → transfer desync/abort. **Failure scenario:** slow BLE proxy + large panel + short configured session timeout. Confidence: **Confirmed** code path (re-auth reachable from the legacy chunk loop); **Plausible** firmware-facing impact. Nonce reuse is *not* a risk (new session_id), so this is a protocol-desync bug, not a crypto bug.

**M2 — Auth-required 0xFE frame shape: py matches firmware but contradicts the header**
`device.py:748-751` detects `[cmd_high, cmd_low, 0xFE]` (3 bytes); header lines 186-187 specify `[0xFE][cmd_echo]` (status-first, 2 bytes). Firmware ground truth agrees with py (`Firmware/src/communication.cpp:400`). A header-conformant `[0xFE][echo]` device would surface as generic `InvalidResponseError`, never `AuthenticationRequiredError`. Spec/impl divergence. *Verified by prior audit pass (B1c/B3d). Confirmed.*

**M3 — Whole-frame `[status][echo]`-as-BE-opcode model + fictional `RESPONSE_HIGH_BIT_FLAG 0x8000`**
`responses.py:44` (`struct.unpack(">H")`) + `commands.py:54` (`0x8000` "high bit = ACK", no header basis, no firmware sets it). Works only because every CMD high byte is `0x00 == RESP_ACK`. A NACK `[FF][echo]` parses as opcode `0xFFxx` and is treated as ACK-flagged before failing `CommandCode` lookup — a latent conflation engine. *Verified by prior audit pass (B1d). Confirmed.*

**M4 — `validate_ack_response` swallows NACK sub-status everywhere it is used**
`responses.py:111-132` accepts only `{cmd, cmd|0x8000}`; any `[FF][echo]` collapses to a generic `InvalidResponseError` with no sub-status decode, at config-write/LED/buzzer/direct-write call sites (device.py:2151, 2061, 1873…). For 0x71/0x72 inside a partial session the NACK byte is a meaningful `OD_ERR_PARTIAL_*`, but is discarded; `ERR_PARTIAL_UNSUPPORTED` is never cached, so a doomed partial is retried on every upload. *Verified by prior audit pass (B4b). Confirmed.*

**M5 — Constant drift & wrong retry diagnosis on pipe-start err 0x02**
`_negotiate_pipe` retries UNCOMPRESSED on `PIPE_START_NACK_COMPRESSION == 0x02` (device.py:2318), but canonical 0x02 for the 0x80 namespace is `OD_ERR_PIPE_START_UNKNOWN_FLAG`, not "compression unsupported" (header lines 566-575). Also missing constants (CMD_POWER_OFF 0x0052, READ_MSD 0x0044, CONFIG_CLEAR 0x0045, LED_STOP 0x0075) and apocryphal ones (`PIPE_START_NACK_BUSY 0x04`, `ERR_MIXED_DATA 0x02`). The pipe-partial path (device.py:2408-2415) layers a second heuristic on the same overloaded 0x02, inferring "partial flag unknown" from a repeated 0x02 — brittle. *Verified by prior audit pass (B3). Confirmed drift; Plausible mis-retry.*

### LOW

**L1 — Encrypted frame already queued can be misparsed as plaintext after a disconnect clears the session (NEW)**
`_on_ble_disconnect` (device.py:661-669) synchronously nulls `_session_key`. If an encrypted notification is enqueued and the disconnect callback runs before the awaiting `_read` (device.py:737-745) wakes, `_read` sees `_session_key is None`, skips `decrypt_response`, and returns the raw ≥31-byte ciphertext as if plaintext → downstream misparse (garbage `InvalidResponseError`). Narrow ordering window; not memory-unsafe. Confidence: **Plausible.**

**L2 — Selective-repeat "1 RTT spacing" never actually spaces retransmits (NEW)**
`_send_pipe_chunks` (device.py:2678-2686): on a newly-detected hole it retransmits and sets `pending_retx[m]=0`; the very next ACK still showing the hole hits `pending_retx[m] += 1 → 1`, and `do_retx = pending_retx[m] >= 1` is immediately true — so a persistent hole is retransmitted on *every* ACK, not once per RTT as the comment claims. Bounded by `max_retx = 3·window` (so it aborts rather than storms), but it wastes the retx budget and BLE airtime. Confidence: **Confirmed.** Efficiency/comment-accuracy, not a hang.

**L3 — Unbounded notification queue (NEW)**
`connection.py:66` creates `asyncio.Queue()` with no `maxsize`; `_notification_callback` uses `put_nowait` (connection.py:341). A misbehaving/flooding peer grows it without backpressure. Stop-and-wait + pre-command drain keeps it near-empty in practice, so **Low**. Confidence: **Confirmed.**

**L4 — Deep-sleep `[seconds:2 BE]` payload not implemented; L5 — storage-init `{FF,40,00,00}` mapped to "no stored configuration"**
Capability gaps / benign mappings. *Verified by prior audit pass (B1b, B4/3e). Confirmed.*

---

## Notes on the watcher-leak fix (this branch)

The fix (device.py:583-608, connection.py:197-251) is **correct and complete** for the three enumerated paths:
1. `__aenter__` now wraps post-connect steps in `try/except BaseException` (catching `CancelledError`, which `except Exception` would miss) and tears the link down before re-raising, so a keyless-probe `AuthenticationRequiredError` or an outer `asyncio.timeout()` cancellation no longer leaks a subscribed connection.
2/3. `_stop_notifications()` is invoked *before* every disconnect (`disconnect`, `_clear_cache_and_drop`), and `_notification_characteristic` is nulled alongside `_client`, so a flaky ACL disconnect can't strand the BlueZ watcher. `read_response` already re-checks `get_nowait()` after a `wait_for` timeout (connection.py:426-434) to avoid dropping a response delivered during cancellation. No remaining leak path found among these lifecycle routines.

**Residual asyncio risk not covered by the fix:** M1 (mid-stream re-auth) and L1 (session-clear vs queued frame) above. No task leaks, no `K_FOREVER`-style unbounded awaits, and no reentrancy deadlock (the lock is per-task reentrant, device.py:640) were found.

---

## Session / auth layer

- **No nonce reuse.** `nonce = session_id(8) || counter_be(8)` (crypto.py:95-97); `_encrypt_frame` increments `_nonce_counter` on every transmission incl. retransmits (device.py:671-685). Re-auth derives a fresh `session_id` before resetting the counter to 0 (device.py:811-813), so the counter reset cannot collide. 64-bit counter — no practical wrap.
- **Mutual auth is real and constant-time.** `authenticate()` recomputes the device's server proof and compares with `hmac.compare_digest` (device.py:807-809); a peer returning status-OK without the master key is rejected.
- **Step-1 retry** on `AuthenticationSessionExistsError` is bounded to one retry (device.py:782-791); rate-limit/wrong-key/encryption-not-configured statuses are decoded (responses.py:167-215).
- Auth frames correctly bypass encrypt/decrypt (use `write_command`/`read_response` directly) — they are plaintext by protocol.

---

## Unimplemented / partial features

| Feature | State in py-opendisplay | Ref |
|---|---|---|
| CMD_POWER_OFF (0x0052 canonical) | Absent; 0x0052 is (mis)used as DEEP_SLEEP | commands.py:40 |
| Deep-sleep `[seconds:2 BE]` arg | Not implemented (bare opcode only) | commands.py:135-154 |
| CMD_READ_MSD 0x0044 / CONFIG_CLEAR 0x0045 / LED_STOP 0x0075 | Absent from CommandCode | commands.py (B3) |
| NFC read (status 0x80 READ_DATA) | Unimplemented; write path present (7.13.0) | responses.py:462-512 (B3) |
| Header-conformant 0xFE `[status][echo]` auth frame | Not detected (only 3-byte firmware shape) | device.py:748 |
| PIPE_WRITE against non-ESP32 targets | N/A by design — negotiation silence → legacy fallback | device.py:2304-2308 |

---

## Cross-repo observations

- **Deep-sleep opcode split is the dominant ecosystem hazard.** py (0x52) + NRF54 (0x52) + Silabs (0x52) + NRF52811 (0x52) agree with each other and with *shipped* firmware, but disagree with canonical v2.1 and the ESP32 `Firmware` branch (0x53 deep-sleep, 0x52 power-off). py→ESP32 `deep_sleep()` = hard power-off. *(PART C.)*
- **PIPE_WRITE has exactly one firmware peer** (ESP32 `Firmware`, per canonical `@targets`). py's substantial sliding-window implementation is therefore validated against a single target; NRF54/Silabs/NRF have no PIPE and rely on py's silence-timeout fallback. The mask/window/seq logic matches the canonical spec (this pass), so the contract is internally consistent — but only one interoperability partner exists.
- **The drift-gate is unwired.** `sync_protocol_header.py --check` is not invoked by any py-opendisplay CI or pre-commit hook (PART C), so `commands.py`'s divergence from the generated mirror is undetected. Recommend adding a check that diffs py's hand-written CommandCode/PIPE/NFC constants against the generated `opendisplay_protocol.py`.
- **NFC endpoint opcode (0x0083)** in py matches canonical; note NRF54 firmware still dispatches NFC on the stale 0x0082 (PART A), so py's NFC write is unreachable on NRF54 regardless of py correctness.
