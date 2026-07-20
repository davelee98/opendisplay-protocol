# Audit — Firmware_NRF54 (XIAO nRF54L15, Zephyr / nRF Connect SDK)

- Repo: `/home/davelee/opendisplay/Firmware_NRF54`
- Branch / commit audited: `main` @ `635d7d2` ("add chanel sounding and switch to ncs")
- Scope: REPORT-ONLY static analysis. Gap-fill pass over main-loop/power/idle, display decompression-to-panel path, battery/ADC, config parse/storage/TLV bounds, and Channel Sounding (`device_flags` bit 5). BLE + pipe subsystem was fully audited in a prior pass (PART A of the staging doc) and is incorporated below by reference, credited "verified by prior audit pass".
- Method: traced code paths in `main.c`, `opendisplay_display.cpp`, `opendisplay_battery.c`, `opendisplay_config_parser.c`, `opendisplay_config_storage.c`, `opendisplay_cs.c`, `opendisplay_device_flags.h`, and the CS wiring in `opendisplay_ble.c`.

---

## Architecture overview

Single-image Zephyr/NCS application. `main()` (src/main.c) does early board init, prepares the EPD power rail, calls `opendisplay_ble_init()`, then spins a cooperative super-loop:
- **Connected:** calls `opendisplay_ble_process()` every ~10 ms (drains the K_MSGQ that the BT RX thread feeds; command handling runs on the main thread — see PART A).
- **Idle (no connection):** `idle_delay_ms()` sleeps in 100 ms chunks while pumping `opendisplay_ble_process()`, then refreshes the manufacturer-specific advertising data (MSD) once per `sleep_timeout_ms` cycle.

There is **no Zephyr PM system-off / deep-sleep path anywhere in the application** — the loop always returns to `k_msleep`, relying only on the SoC's automatic idle. The BLE GATT service (single 0x2446 char, WRITE + WRITE_NO_RSP + NOTIFY) is the sole command channel. Config is TLV-parsed (`opendisplay_config_parser.c`) into a large static `GlobalConfig` and persisted through the Zephyr `settings` subsystem (`opendisplay_config_storage.c`, key `od/config`, magic+version+CRC32 record). Display output (`opendisplay_display.cpp`) drives `bb_epaper` with a full-frame direct-write path (0x70/0x71/0x72, optional zlib streaming) and a 1bpp partial-update path (0x76). Channel Sounding (`opendisplay_cs.c`) is a compile-gated (`CONFIG_BT_CHANNEL_SOUNDING`) reflector-role feature enabled at runtime by `device_flags` bit 5 (0x20).

Confirmed: no `FEATURE_PARITY_VS_FIRMWARE.md` exists anywhere in the repo (only `docs/LM20_NCS.md`). The gap task's item 5 is therefore vacuous — there are no parity claims to check. Parity is instead asserted in inline comments (`main.c:46-48`, `opendisplay_battery.c:22-32`, `opendisplay_config_parser.c:108-118`); those are consistent with the code as written.

---

## Findings by severity

### HIGH

#### H1. Vendored protocol header drift — three wire-opcode divergences (Confirmed) — *verified by prior audit pass (A2)*
`src/opendisplay_protocol.h` is a 59-line stub vs the 861-line canonical; `sync_protocol_header.py --check` FAILS. NFC dispatched on retired **0x0082** (canonical 0x0083); deep-sleep on **0x0052** (canonical v2.1 = CMD_POWER_OFF; deep-sleep moved to 0x0053). A canonical/py host cannot reach NFC or deep-sleep on this target, and a host sending PIPE_WRITE_END 0x0082 is misrouted into NFC handling. See A2 for full detail. Reconfirmed against my gap files: `opendisplay_display.cpp` includes this same drifted header and consumes `OD_ERR_*`/`REFRESH_*`/`TRANSMISSION_MODE_*` constants from it, so any future header re-sync must be validated against the display path too.

### MEDIUM

#### M1. Channel Sounding setup: unsynchronized `s_cs_conn` use-after-free / NULL-deref across threads (Plausible)
`opendisplay_cs.c:60,129-167` vs `:184-210` and `opendisplay_ble.c:388-389,406`.

`opendisplay_cs_on_connected()` (BT RX/connected callback thread) does `s_cs_conn = bt_conn_ref(conn)` then `k_work_submit(&s_cs_setup_work)`. The work handler `cs_setup_work_handler()` runs on the **system workqueue thread**, reads `s_cs_conn` once for a NULL check (`:129`), then dereferences it at `:140` (`bt_le_cs_set_default_settings`) and again at `:167` — with a **10-second blocking `k_sem_take` in between** (`:146`). Meanwhile `opendisplay_cs_on_disconnected()` (BT thread) runs `bt_conn_unref(s_cs_conn); s_cs_conn = NULL` (`:202-205`) with no lock, atomic, or work-cancel.

Failure scenario: peer connects with CS enabled, then disconnects (or link drops) during the ≤10 s config-creation window. `on_disconnected` drops the last ref and nulls the pointer while the work handler is parked in `k_sem_take` still holding the raw `s_cs_conn`; on wake it calls `bt_le_cs_set_procedure_parameters(s_cs_conn, …)` on a freed/NULL conn (UB / NULL deref). Rapid reconnect makes it worse: `on_connected` unrefs the old conn and submits the work item again while the prior invocation may still be queued/parked, and `k_sem_reset` (`:206`) races the pending `k_sem_take`. This is the CS analogue of PART A's A7 (`s_conn` cross-thread), but higher-impact because the handler holds the pointer across a 10 s blocking wait rather than a single notify call. In practice the HCI ops likely fail with `-ENOTCONN` rather than crashing, hence Plausible not Confirmed, but the freed-pointer deref is genuine UB.

#### M2. CS setup blocks the system workqueue for up to 10 s (Confirmed)
`opendisplay_cs.c:146` — `cs_setup_work_handler` runs on the shared system workqueue (submitted via `k_work_submit`) and parks in `k_sem_take(&s_cs_config_sem, K_SECONDS(10))` waiting for the remote initiator to create the CS config. For the entire window (up to 10 s, or the timeout on failure) **every other `k_work_submit` item is starved**, including anything the BLE/adv path defers to the system workqueue. Failure scenario: a central that advertises interest in ranging but never completes config creation stalls all system-workqueue work for 10 s per connection. Should run on a dedicated workqueue or use an async/non-blocking completion. Confirmed by trace; severity Medium because it only triggers when CS is enabled (bit 5) and a peer initiates.

#### M3. Deep-sleep is a no-op, and the device has no low-power state at all (Confirmed)
`main.c:34-59` + deep-sleep stub (`opendisplay_ble.c` `schedule_deep_sleep` is log-only — PART A A5). The super-loop only ever `k_msleep`s and keeps advertising; there is no `pm_state_force` / system-off / EM-style entry anywhere. Combined with the 0x0052 "deep sleep" handler being purely a log line, a host that issues deep-sleep (even on the old opcode this firmware still uses) gets a silent no-response and the tag stays fully awake, advertising, and periodically refreshing MSD. Failure scenario: battery-powered tag commanded to sleep never reduces current draw. This is the concrete power-subsystem implication of the documented stub (task item 2). Also note `main.c:51` `opendisplay_ble_update_msd(true)` fires once per `sleep_timeout_ms` idle cycle purely to keep the MSD fresh — there is no actual sleep during that timeout, just a chunked busy-idle. Severity Medium (functional/battery), not High, because it is a known-stub area rather than a corruption bug.

#### M4. `CMD_ENTER_DFU` sends a success ACK but never enters DFU (Confirmed) — *verified by prior audit pass (A4)*
`opendisplay_pipe.c:1241-1247` replies `[00][51]` then `schedule_dfu()` only prints "not implemented". Client sees ACK, waits for a bootloader/disconnect that never comes → hung/bricked OTA flow. Carried forward from PART A; still the most user-visible dishonest response on this target.

### LOW

#### L1. NFC single-shot length check bypassable via uint16 overflow (Confirmed, latent) — *verified by prior audit pass (A3)*
`opendisplay_pipe.c:1060-1065`: `(uint16_t)(4u + text_len)` truncation lets `text_len=0xFFFF` pass the guard. Defanged only because `opendisplay_ble_nfc_write` is a stub returning false; becomes Critical OOB read the moment a real NFC backend lands. Config-side note: `nfc_config` (0x2A) IS fully parsed/stored (`opendisplay_config_parser.c:533-560`, up to 2 entries), so the configuration half of NFC is real while both runtime backends are stubs — the elaborate front end / dead backend asymmetry from A5 extends into config.

#### L2. Auth MAC compare not timing-safe; `s_conn`/`s_notify` cross-thread reads (Confirmed/Plausible) — *verified by prior audit pass (A6, A7)*. BLE-latency masks the timing side channel; the pointer races are formally unsynchronized but practically bounded by the host stack holding the conn ref. See M1 for the CS-specific instance, which is the sharper version of A7.

#### L3. Config-write single-frame length 201 silently drops one byte (Confirmed) — *verified by prior audit pass (A8)*.

#### L4. Battery `ENABLE_INVERTED` flag ignored; 30 s cache can outlive a config change (Confirmed, minor)
`opendisplay_battery.c:133-138` drives the sense-enable pin HIGH unconditionally (documented as matching the Arduino reference `readBatteryVoltageUncached`); a board wired active-low would misread. `opendisplay_battery_read_voltage_volts` caches for 30 s (`:34,189`) keyed only on time — a config write that changes `battery_sense_pin`/`voltage_scaling_factor` mid-window returns a stale reading until TTL expiry. Both low impact. The 9-bit clamp in `opendisplay_battery_get_10mv` (`:207-209`, `>511`) correctly matches the advertisement battery field width.

#### L5. Config parser: unknown-type packet drops all later packets; skip-size table must stay in lockstep with structs (Confirmed, by-design but fragile)
`opendisplay_config_parser.c:616-643` — a type ID absent from `config_packet_data_size()` forces skip-to-CRC, silently discarding every subsequent packet (the TLV format carries no per-packet length, so this is unrecoverable and documented). More fragile: `config_packet_data_size()` hard-codes each packet's on-wire size (`:120-140`) independently of the `sizeof(struct …)` used by the real parse cases. If a struct changes size without updating the table (or vice-versa), the default-skip path desyncs the whole rest of the blob — exactly the class of bug the comment says the old wifi-162-vs-160 error caused (`:110-118`). No automated cross-check enforces table == struct sizes. TLV bounds checks themselves are sound: every case guards `offset + sizeof(...) <= configLen - 2` before `memcpy`, `configLen >= 3` is enforced up front (`:152`) so `configLen - 2` never underflows, and per-type array caps (`< 4` / `< 2`) prevent `GlobalConfig` array overflow.

#### L6. `rescan_security_packet` scans attacker-controlled blob for a security struct (Confirmed, low risk)
`opendisplay_config_parser.c:46-65` — if the main parse loop bailed early (unknown packet), this fallback linearly scans the raw config bytes for a `CONFIG_PKT_SECURITY` marker and adopts the first candidate whose `encryption_enabled` or key bytes look set. Bounds are correct (`i + 2u + sizeof(SecurityConfig) <= configLen - 2u`, no underflow given `configLen >= 3`). Risk is low because config write already requires the pipe/auth path, but it means a malformed/partially-understood config can still activate encryption from bytes that were never validated as a real security packet. Worth noting for the "config apply" threat surface.

---

## Unimplemented / partial features

| Feature | Opcode / trigger | State on NRF54 @635d7d2 | Evidence |
|---|---|---|---|
| Deep sleep | 0x0052 (old numbering) | Log-only stub; no PM entry; no response frame | pipe.c:1248-1254, ble.c schedule_deep_sleep; M3 |
| Power-off (canonical 0x0052) | 0x0052 | Consumed by deep-sleep stub; no true power-off | A2/A10 |
| Enter DFU / OTA | 0x0051 | Dishonest ACK, never enters bootloader | pipe.c:1241-1247; M4 |
| NFC read | 0x0082 read | Front end real, backend `nfc_read` stub → NACK 0x02 | A3/A5, L1 |
| NFC write | 0x0082 write | Front end real, backend `nfc_write` stub → NACK 0x03; length guard has latent uint16 overflow | A3/L1 |
| NFC config | 0x2A config packet | Parsed & stored (≤2 entries), but no runtime consumer (backends stubbed) | config_parser.c:533-560 |
| Channel Sounding | device_flags bit5 (0x20) | Compile-gated reflector role; setup present but blocks system WQ ≤10 s and has cross-thread conn race | opendisplay_cs.c; M1/M2 |
| Wi-Fi config | 0x26 config packet | Parsed & stored only (no radio); read-back preserved | config_parser.c:481-506 |
| Low-power / system-off | — | Absent; super-loop only k_msleep | main.c; M3 |
| Partial update (0x76) | 0x76 | Implemented, 1bpp only, etag-gated, bounds-checked | display.cpp:444-565 |
| Direct write + zlib stream (0x70-0x72) | 0x70/71/72 | Implemented, streaming decompression gated on transmission_modes | display.cpp:710-982 |

---

## Cross-repo observations

- **Opcode numbering:** NRF54 remains on the *old* numbering (deep-sleep 0x0052, NFC 0x0082) shared with py-opendisplay, Firmware_Silabs and Firmware_NRF, and disagrees with canonical v2.1 + the ESP32 `Firmware` branch (which adopted 0x0053 deep-sleep / 0x0052 POWER_OFF / 0x0083 NFC). This matches PART C's cross-repo table. A py client issuing deep-sleep (0x0052) still works against this firmware but would hit CMD_POWER_OFF on the new ESP32 firmware.
- **Drift gate unwired:** consistent with PART C, this repo's `.github/workflows/` runs no `sync_protocol_header.py --check`, so the 59-line vendored header drifted undetected. Display and config code both `#include "opendisplay_protocol.h"`, so a future re-sync is a wider change than the pipe layer alone.
- **Config schema coupling:** the hand-maintained `config_packet_data_size()` table (L5) mirrors sizes that the comment says are cross-checked against `Firmware/src/structs.h` and `py-opendisplay/protocol/config_serializer.py`. That three-way agreement is asserted, not enforced — the same latent drift risk as the protocol header, one layer up (the config TLV schema). Recommend a generated/checked size table.
- **Display path parity** with the nRF52840 `Firmware` reference is asserted in comments (response-ordering `direct_write_end_prepare`/`_refresh` split at display.cpp:867-982, MSD-refresh cadence in main.c). Those match the code; no automated parity harness exists.

---

*Prepared 2026-07-19. Static analysis only; no build, no execution. "Confirmed" = traced code path; "Plausible" = reasoned failure mode not fully traced to a crash.*
