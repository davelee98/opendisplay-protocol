# Cross-Repo Device-Config Consistency Audit — Power Config (TLV 0x04)

**Date:** 2026-07-13
**Scope:** Firmware power-config fields (`screen_timeout_seconds` et al.) vs py-opendisplay config read/write, HA integration, opendisplay.org docs/tooling.
**Mode:** Read-only audit. All fixes below are suggestions only; nothing was changed.

> **Update 2026-07-14:** At audit time `screen_timeout_seconds` existed only as uncommitted working-tree edits on Firmware `feat/less-latency`. It has since **merged to Firmware `main` as commit `bcee206` ("EPD panel power session … configurable screen_timeout_seconds", #100)**. The wire layout, clamp (30 s), AXP2101 force-off, and live-disable-on-save behaviors were re-verified against the committed code and are unchanged from what this doc describes. Commit-status wording below has been corrected; all byte-layout and semantic findings still stand — and Findings 1–3 are now **more** urgent, since the firmware field is shipped rather than in-flight.

---

## 1. Executive summary

The **wire layout is consistent everywhere** — all four repos agree the `power_option` TLV (packet type 0x04) is a 30-byte packed little-endian blob, and opendisplay.org's `config.yaml` (the single source of truth for the web toolbox/tester, which compute offsets from it) already matches the new firmware struct **byte-for-byte, including `screen_timeout_seconds` at offset 25 and `reserved[4]`**. Command opcodes (0x0040/0x0041/0x0042/0x0045), 200-byte write chunking, CRC-16/CCITT-FALSE, and encrypted-frame overheads (31-byte minimum = cmd 2 + nonce 16 + len 1 + tag 12; 185-byte max encrypted packet; 244-byte HA GATT ceiling) all reconcile.

The problems are all at the **semantic/model layer**, concentrated in py-opendisplay:

1. **py-opendisplay has no named representation for any field at offsets 20–25** (`charge_enable_pin`, `charge_state_pin`, `charger_flags`, `min_wake_time_seconds`, and the branch-new `screen_timeout_seconds`). They are lumped into an opaque 10-byte `reserved` blob. Binary read-modify-write round-trips them safely, but they are **unreadable and unsettable by name** from Python and therefore from HA.
2. **The JSON codec destroys them.** `config_to_json` exports the power `reserved` as `"0x0"` and `config_from_json` rebuilds it as `bytes(10)` — a JSON round-trip **zeroes offsets 20–29**, silently wiping the charger pins, `min_wake_time_seconds`, and `screen_timeout_seconds`. Any tool that exports config to JSON, edits, re-imports, and writes to the device will revert these fields to defaults. (The codebase already solved this exact problem for `binary_inputs` by exporting the raw blob — the power packet was missed.)
3. **Docs semantics drift** for `screen_timeout_seconds`: `config.yaml` says "0–255 s debounce timer", firmware clamps to **30 s max** (`EPD_KEEPALIVE_MAX_S`) and **forces 0 on AXP2101 boards** — neither behavior is documented.
4. The HA integration is a strictly **read-only** config consumer (no write path), so it cannot corrupt device config — but its config-entry **cache** goes through the lossy JSON codec, so diagnostics dumps misreport the new fields.

No byte-offset, endianness, signedness, or opcode mismatches were found. The `screen_timeout_seconds` firmware change is **committed on Firmware `main`** as `bcee206` (#100) (see §2), which the ecosystem (config.yaml) has anticipated but py-opendisplay has not.

---

## 2. Audited branch/commit context

| Repo | Branch | HEAD | State |
|---|---|---|---|
| `/home/davelee/opendisplay/Firmware` | `feat/less-latency` @ audit → `main` now | `74b3f76` at audit; `screen_timeout_seconds` since **merged to `main` as `bcee206` (#100)** | Clean on `main` as of 2026-07-14 — field committed across `src/structs.h`, `src/config_parser.cpp`, `src/display_service.{h,cpp}`, `src/communication.cpp`, `src/main.{h,cpp}` |
| `/home/davelee/opendisplay/py-opendisplay` | `main` | `4bd3ef2` (release 7.12.0) | clean |
| `/home/davelee/opendisplay/Home_Assistant_Integration` | `feat/pipe-partial` | `60b7cf5` (pins py-opendisplay 7.12.0) | untracked docs only |
| `/home/davelee/opendisplay/opendisplay.org` | `update/pipe-changes` | `504aa9c` | clean |

Field introduction history (from `git log -S` on `Firmware/src/structs.h`):

- `charge_enable_pin` / `charge_state_pin` / `charger_flags` — commit `7ffea91` (on `main`, older).
- `min_wake_time_seconds` — commit `d974f9d` (#97, current `main` tip; merged into the branch), shrank `reserved` to 5.
- `screen_timeout_seconds` — introduced on `feat/less-latency`, **now committed on `main` as `bcee206` (#100)**; `src/structs.h` replaces `uint8_t reserved[5]` with `uint8_t screen_timeout_seconds` + `uint8_t reserved[4]` (struct size unchanged at 30 bytes — wire-compatible).

The only strictly branch-new field is `screen_timeout_seconds`; however, py-opendisplay 7.12.0 lacks named support for **all five** fields added after `deep_sleep_time_seconds`, so the audit covers offsets 20–25 as a group.

---

## 3. Authoritative firmware layout — `struct PowerOption` (packed, 30 bytes)

Source: `/home/davelee/opendisplay/Firmware/src/structs.h:44-65` (`__attribute__((packed))`, little-endian targets ESP32/nRF52). TLV framing per packet is `[instance:1][packet_type:1][struct bytes]` (`Firmware/src/config_parser.cpp:288-322`; firmware `memcpy`s `sizeof(struct PowerOption)` at `config_parser.cpp:313-316`). Container: `[len:2][version:1] packets... [CRC16:2]` (`config_parser.cpp:284-288`).

| Offset | Size | Field | structs.h line | Notes |
|---|---|---|---|---|
| 0 | 1 | `power_mode` | 45 | enum 1=battery 2=usb 3=solar |
| 1 | 3 | `battery_capacity_mah` | 46 | 24-bit LE, mAh |
| 4 | 2 | `sleep_timeout_ms` | 47 | u16 LE, ms |
| 6 | 1 | `tx_power` | 48 | u8 |
| 7 | 1 | `sleep_flags` | 49 | bit0 = `SLEEP_FLAG_BUTTON_WAKE_DISABLE` (structs.h:72) |
| 8 | 1 | `battery_sense_pin` | 50 | 0xFF = none |
| 9 | 1 | `battery_sense_enable_pin` | 51 | 0xFF = none |
| 10 | 1 | `battery_sense_flags` | 52 | bit0 = `BATTERY_SENSE_FLAG_ENABLE_INVERTED` (structs.h:75) |
| 11 | 1 | `capacity_estimator` | 53 | enum 1..4 |
| 12 | 2 | `voltage_scaling_factor` | 54 | u16 LE |
| 14 | 4 | `deep_sleep_current_ua` | 55 | u32 LE, µA |
| 18 | 2 | `deep_sleep_time_seconds` | 56 | u16 LE, s; 0 = disabled |
| 20 | 1 | `charge_enable_pin` | 57 | BQ25616 CE; 0/0xFF = unused |
| 21 | 1 | `charge_state_pin` | 58 | 0/0xFF = unused |
| 22 | 1 | `charger_flags` | 59 | bit0/bit1 active-low flags (structs.h:67-68) |
| 23 | 2 | `min_wake_time_seconds` | 60 | u16 LE; 0 → default 120 s (`main.h:330`, `main.cpp:166-168`) |
| 25 | 1 | **`screen_timeout_seconds`** (NEW) | 61-63 | EPD keep-alive; clamped to 30 (`display_service.h:16`, `display_service.cpp:196`); **forced 0 on AXP2101** (`display_service.cpp:187-195`); 0 = power off immediately |
| 26 | 4 | `reserved[4]` | 64 | |

---

## 4. Field-by-field cross-repo mapping

Legend — **py-parse:** `py-opendisplay/src/opendisplay/protocol/config_parser.py` `_parse_power_option` (:309-348); **py-ser:** `config_serializer.py` `serialize_power_option` (:156-208); **py-model:** `models/config.py` `PowerOption` (:190-246); **HA:** `Home_Assistant_Integration/custom_components/opendisplay`; **docs:** `opendisplay.org/httpdocs/firmware/toolbox/config.yaml` (packet id 4, lines 270-402).

| Firmware field (offset) | py-opendisplay | HA integration | opendisplay.org | Verdict |
|---|---|---|---|---|
| `power_mode` (0) | parse :314, ser :181, model :196; `PowerMode` enum 1/2/3 (`models/enums.py:281-286`) | read `sleep.py:151`, `sensor.py:102` | yaml :275 (same enum) | CONSISTENT — fw uses `power_mode == 1` for battery (`main.cpp:381`), matches `PowerMode.BATTERY=1` |
| `battery_capacity_mah` (1, u24 LE) | parse :316-317, ser :183-189 | — | yaml :288 | CONSISTENT |
| `sleep_timeout_ms` (4, u16) | parse/ser `<H`, model :198 | read `sleep.py:152` | yaml :291 | CONSISTENT (ms everywhere; CLI converts to s for display only, `cli.py:287,363`) |
| `tx_power` (6, u8) | `B` with explicit "uint8, not int8" comments (parser :330, ser :191) | — | yaml :294 | CONSISTENT |
| `sleep_flags` (7) | raw int only — **no named bit constants** | — | yaml :297-316 — **documents all 8 bits as `reserved_0..7`** | DRIFT (Finding 5): fw bit0 `SLEEP_FLAG_BUTTON_WAKE_DISABLE` undocumented |
| `battery_sense_pin` (8) | :323/:197 | — | yaml :317 | CONSISTENT |
| `battery_sense_enable_pin` (9) | :324/:198 | — | yaml :320 | CONSISTENT |
| `battery_sense_flags` (10) | raw int only | — | yaml :323-342 — all bits `reserved_N` | DRIFT (Finding 5): fw bit0 `BATTERY_SENSE_FLAG_ENABLE_INVERTED` undocumented |
| `capacity_estimator` (11) | `CapacityEstimator` 1..4 (`models/enums.py:155-161`) | read `sensor.py:103` | yaml :343 (same values) | CONSISTENT |
| `voltage_scaling_factor` (12, u16) | `<H` | — | yaml :359 | CONSISTENT |
| `deep_sleep_current_ua` (14, u32) | `<I` | diagnostics only | yaml :362 (µA) | CONSISTENT |
| `deep_sleep_time_seconds` (18, u16) | `<H`, model :207; `deep_sleep_enabled` predicate :224-230 mirrors `main.cpp:381` | read `sleep.py:153`; predicate `sleep.py:123-124` | yaml :365 (s) | CONSISTENT |
| `charge_enable_pin` (20) | **MISSING** — byte 0 of opaque `reserved` (parse :332, ser :207) | — | yaml :368-370 ✓ | Finding 1/2 |
| `charge_state_pin` (21) | **MISSING** — `reserved[1]` | — | yaml :371-373 ✓ | Finding 1/2 |
| `charger_flags` (22) | **MISSING** — `reserved[2]` | — | yaml :374-393 ✓ (bit names match structs.h:67-68) | Finding 1/2 |
| `min_wake_time_seconds` (23, u16) | **MISSING** — `reserved[3:5]` | — (not consumed by sleep logic) | yaml :394-396 ✓ ("0 = default 120 s" matches `main.h:330`) | Finding 1/2, 8 |
| `screen_timeout_seconds` (25) — **NEW** | **MISSING** — `reserved[5]` | — | yaml :397-399 — offset/size correct, **description stale** | Findings 1/2, 3 |
| `reserved[4]` (26) | subsumed into 10-byte `reserved` (comment says "10 reserved bytes", parser :332; ser docstring :172; model :208) | — | yaml :400-402 (4 bytes ✓) | Finding 4 (stale comments) |

Web tooling note: `opendisplay.org/httpdocs/js/ble-common.js:2861-2897` (`parsePowerOptionPacketFields`) and the toolbox generic serializer (`httpdocs/firmware/toolbox/index.html:1618-1653`) compute offsets from `config.yaml` sizes, so they inherit the correct layout, and the JS parser **does** already read `min_wake_time_seconds` and `screen_timeout_seconds` by name — the website is *ahead* of py-opendisplay here.

---

## 5. Findings

### Finding 1 — py-opendisplay has no named fields for offsets 20–25 of PowerOption (HIGH)
- **Where:** `py-opendisplay/src/opendisplay/models/config.py:190-208` (model ends at `deep_sleep_time_seconds` + `reserved: bytes  # 10 bytes`); `protocol/config_parser.py:309-348` (parses only through offset 19, then `reserved = data[20:30]` at :332); `protocol/config_serializer.py:156-208` (same field set, reserved appended at :206-208).
- **What:** `charge_enable_pin`, `charge_state_pin`, `charger_flags`, `min_wake_time_seconds`, `screen_timeout_seconds` (firmware `structs.h:57-63`) cannot be read or written by name. The new EPD keep-alive feature is unusable from Python/HA without manually poking bytes into `PowerOption.reserved`.
- **Mitigation present:** the binary path is layout-safe — parse stores raw bytes 20–29 and serialize re-emits them (`config_serializer.py:207-208`), so `interrogate()` → mutate other fields → `write_config()` round-trips the new fields intact.
- **Suggested fix:** extend `PowerOption` with the five named fields + `reserved: bytes  # 4 bytes`; parse with `struct.unpack_from("<BBBHB", data, 20)`; serialize symmetrically; keep a back-compat property if `reserved` length is externally observed. Add `SLEEP_FLAG_BUTTON_WAKE_DISABLE`, `BATTERY_SENSE_FLAG_ENABLE_INVERTED`, `CHARGER_FLAG_*` constants mirroring `structs.h:67-75`.

### Finding 2 — JSON codec zeroes the reserved-embedded power fields on round-trip (HIGH, data loss)
- **Where:** `py-opendisplay/src/opendisplay/models/config_json.py:143` — export writes `"reserved": "0x0"` for `power_option`; `config_json.py:509` — import hardcodes `reserved=bytes(10)`.
- **What:** `config_to_json` → `config_from_json` → `serialize_config` → `write_config` **silently zeroes bytes 20–29**: charger pins → 0 ("unused", disabling BQ25616 charger control on boards like reTerminal Sticky), `min_wake_time_seconds` → 0 (reverts to 120 s default), `screen_timeout_seconds` → 0 (keep-alive off). This is the exact bug class already fixed for `binary_inputs` — see the comment at `config_json.py:270-272` ("reserved holds ADC-ladder thresholds + power_off_flags/hold; export the raw blob so a round-trip preserves them").
- **Suggested fix:** until Finding 1 is implemented, export the raw blob (`"reserved": _hex_bytes(pwr.reserved)`) and import with `_parse_hex_bytes(fields.get("reserved", "0x0"), 10)`, mirroring the binary_input treatment. After Finding 1, export the named fields (matching `config.yaml` names so toolbox JSON stays interoperable).

### Finding 3 — opendisplay.org documents stale semantics for `screen_timeout_seconds` (MEDIUM)
- **Where:** `opendisplay.org/httpdocs/firmware/toolbox/config.yaml:397-399` — "debounce timer for display power for rapid update cycles (0-255 s; 0 = disabled, power down immediately)".
- **What:** Firmware (`main`, `bcee206`/#100) clamps to **30 s max** (`Firmware/src/display_service.h:16` `EPD_KEEPALIVE_MAX_S 30`; clamp at `display_service.cpp:196`) and **forces the value to 0 on AXP2101 boards** regardless of config (`display_service.cpp:187-195`). Values 31–255 are accepted on the wire but silently clamped — a user setting 120 s via the toolbox gets 30 s with no feedback. The "debounce timer" framing also diverges from the implemented "EPD keep-alive (WARM window)" concept (`structs.h:61-63`).
- **Suggested fix:** update the yaml description to "EPD keep-alive: seconds panel stays powered after a refresh before shutdown (0–30, values above 30 clamped; 0 = power off immediately; ignored/forced 0 when an AXP2101 PMIC is present)". Consider adding a prose section — "keep-alive" currently appears nowhere on opendisplay.org.

### Finding 4 — Stale "reserved" size comments in py-opendisplay (MEDIUM)
- **Where:** `config_parser.py:332` ("# 10 reserved bytes, not 12"), `config_serializer.py:172` ("- reserved: 10 bytes"), `models/config.py:208` ("reserved: bytes  # 10 bytes"), and the serializer field list at `config_serializer.py:159-172` which presents the packet as ending at `deep_sleep_time_seconds` + 10 reserved.
- **What:** Only 4 bytes are actually reserved; 6 of those 10 bytes are live firmware fields (5 since `7ffea91`/`d974f9d`, 6 with the branch change). The comments actively mislead a maintainer into believing zero-filling is safe (which is exactly what `config_json.py` does — Finding 2).
- **Suggested fix:** subsumed by Finding 1; at minimum rewrite the comments to enumerate the embedded fields, as was done for `binary_inputs`.

### Finding 5 — Defined flag bits documented as "reserved" in config.yaml; no named constants in Python (LOW)
- **Where:** `config.yaml:297-316` (`sleep_flags` bits 0–7 all named `reserved_N`) vs `Firmware/src/structs.h:70-72` (`SLEEP_FLAG_BUTTON_WAKE_DISABLE` = bit0); `config.yaml:323-342` (`battery_sense_flags` all reserved) vs `structs.h:74-75` (`BATTERY_SENSE_FLAG_ENABLE_INVERTED` = bit0). py-opendisplay defines neither constant (repo-wide grep: no `SLEEP_FLAG`/`ENABLE_INVERTED` hits).
- **What:** Toolbox users can't discover/toggle button-wake-disable or inverted battery-sense-enable; Python callers must use magic numbers.
- **Suggested fix:** name bit0 in both yaml bitfields; add the two constants to `py-opendisplay/src/opendisplay/models/enums.py` or the model module.

### Finding 6 — Stale minimum-length guard in web BLE tester power parser (LOW)
- **Where:** `opendisplay.org/httpdocs/js/ble-common.js:2862` — `if (!packetData || packetData.length < 18) return null;`.
- **What:** The 18-byte floor predates the 30-byte struct. Benign today because `min_wake` (:2890-2891) and `screen_timeout` (:2892-2893) are individually length-guarded and degrade to `null`, but the floor no longer reflects the struct and invites future unguarded reads. Note also the guard-style inconsistency: `!== null` for yaml-offset lookups at :2885-2889 vs `!== undefined` at :2890-2893 (missing yaml fields yield `undefined`, so the `!== null` checks would pass and read `NaN` offsets if a field were ever dropped from config.yaml).
- **Suggested fix:** raise the floor to 30 (or 26 to accept the pre-`min_wake` layout) and normalize the offset-presence checks to `!== undefined`.

### Finding 7 — Chunked-write ceiling mismatch: firmware caps at 20 chunks (≈4000 B) but MAX_CONFIG_SIZE is 4096 and Python enforces no ceiling (LOW)
- **Where:** `Firmware/src/communication.cpp:469` — rejects a chunk when `receivedSize + len > 4096 || receivedChunks >= 20`; `Firmware/src/config_parser.h:7` — `MAX_CONFIG_SIZE 4096`; `py-opendisplay/src/opendisplay/protocol/commands.py:496-560` — `build_write_config_command` has no total-size check (`CONFIG_CHUNK_SIZE = 200`, commands.py:57).
- **What:** A config of 4001–4096 bytes needs a 21st chunk (200 first + 19×200 = 4000) and is NAK'd mid-transfer; a >4096-byte config would also pass Python and fail on-device. Storage accepts 4096 (`config_parser.cpp:81`) so the reachable write ceiling (4000) is quietly lower than the read/storage ceiling.
- **Suggested fix:** raise `ValueError` in `build_write_config_command`/`Device.write_config` for `len(config_data) > 4000` (or bump the firmware chunk cap to 21). Document the effective limit; `opendisplay.org/httpdocs/protocol/yaml-config.html:372-374` says only "max 4kB recommended".

### Finding 8 — HA integration cannot see or set any of the new power fields; its cached config is lossy (LOW, informational)
- **Where:** HA is read-only w.r.t. device config — no `write_config`/`PowerOption` construction anywhere in `custom_components/**`. It consumes only `power.power_mode`, `power.sleep_timeout_ms`, `power.deep_sleep_time_seconds` (`custom_components/opendisplay/sleep.py:151-153`), `power_mode_enum`/`capacity_estimator` (`sensor.py:102-103`). Its config cache round-trips through the lossy JSON codec: `__init__.py:109` (`config_from_json`) / `__init__.py:135` (`config_to_json`).
- **What:** (a) Because of Finding 2, the HA-cached copy (and the diagnostics dump via `diagnostics.py:63`) zeroes charger/min-wake/screen-timeout values — misleading but harmless since HA never writes to the device. (b) HA's deep-sleep availability model is unaware of `min_wake_time_seconds` (device stays awake ≥120 s after button wake) and `screen_timeout_seconds`; both are potentially useful inputs to `sleep.py`'s wake-window logic once the library exposes them (cf. staleness gap already noted in `docs/DEEP_SLEEP_EXTENSIONS_FINDINGS_2026-07-07.md:84,115`).
- **Suggested fix:** none required now; after Findings 1–2 land, consider feeding `min_wake_time_seconds` into `SleepProfile` and surfacing `screen_timeout_seconds` in diagnostics.

### Finding 9 — config.yaml shipped `screen_timeout_seconds` ahead of firmware; firmware now merged, docs still stale (INFO)
- **Where:** `Firmware` `main` `bcee206`/#100 (see §2) vs `config.yaml:397-399` already on `update/pipe-changes`.
- **What:** The schema field pre-existed in the website repo with the old "debounce, 0–255" wording, i.e. the docs shipped ahead of the firmware behavior. The firmware is now committed (`bcee206`/#100) but its commit message does not reference the clamp + AXP2101 override, so config.yaml's stale "debounce timer" description (Finding 3) still needs a follow-up doc update — it did not land with the firmware commit.

---

## 6. Read/write (get/set) symmetry analysis

**py-opendisplay binary path — symmetric.** `_parse_power_option` (`config_parser.py:309-348`) and `serialize_power_option` (`config_serializer.py:156-208`) cover the identical field set, both `<`-prefixed (little-endian) with `tx_power` as unsigned `B` on both sides; reserved bytes 20–29 are preserved verbatim in both directions. Container framing matches firmware exactly: Python emits `[00 00][version][instance][type][struct]...` + CRC (`config_serializer.py:562-605`), firmware walks it identically (`config_parser.cpp:284-322`, instance byte skipped at :290). Required-packet validation is symmetric too: parser requires system/manufacturer/power/display (`config_parser.py:210-222`), serializer/`write_config` requires the same (`config_serializer.py:559`, `device.py:1300-1315`).

**py-opendisplay JSON path — asymmetric (the core defect).** Export names 12 power fields + `"reserved": "0x0"` (`config_json.py:129-144`); import rebuilds the same 12 + `reserved=bytes(10)` (`config_json.py:495-510`). Symmetric in shape, but both directions **drop the six live bytes** at offsets 20–25 (Finding 2) — so read(binary)→export(JSON)→import(JSON)→write(binary) is not idempotent.

**Firmware — symmetric with an intentional size asymmetry.** READ (0x0040) streams the raw stored blob back (chunk header `[00 40][chunk#:2LE]` + first-chunk `[totalLen:2LE]`, `communication.cpp:365-381`), which Python reassembles correctly (`device.py:949-1000`). WRITE (0x0041/0x0042) accepts ≤200 B single-shot or `[total:2LE][200B]` + 200 B chunks (`communication.cpp:395-430`), matching `build_write_config_command` (`commands.py:496-560`, including the documented 202-byte edge case). Asymmetry: read chunks are sized to the negotiated MTU (worst case 94 B payload, `communication.cpp:359-362`) while write chunks are a flat 200 B — by design, both ends handle it. The only symmetry gap is the write-side 20-chunk cap (Finding 7): configs of 4001–4096 bytes are readable/storable but not writable.

**Command/constant reconciliation (all consistent):**

| Constant | Firmware | py-opendisplay | opendisplay.org |
|---|---|---|---|
| READ_CONFIG 0x0040 | `communication.cpp:585` | `commands.py:17` | `ble-flow.html:583-588` |
| WRITE_CONFIG 0x0041 | `communication.cpp:591` | `commands.py:18` | `ble-flow.html:667-679` |
| WRITE_CONFIG_CHUNK 0x0042 | `communication.cpp:595` | `commands.py:19` | `ble-flow.html:678-679` |
| CLEAR_CONFIG 0x0045 | `communication.cpp:440` | — (not audited) | `ble-flow.html:883` |
| Write chunk size 200 | `communication.cpp:406,413-419,469` | `commands.py:57` | `ble-flow.html:678-679`, `ble-common.js:2967,3002-3042` |
| Config container max | `config_parser.h:7` (4096) | none (Finding 7) | `yaml-config.html:372-374` ("max 4kB recommended") |
| CRC-16/CCITT-FALSE | `config_toolbox_outer_crc16` (per `config_serializer.py:59`) | `config_serializer.py:46-84` | `yaml-config.html:387-389` |
| Encrypted frame | — | `device.py:419` min 31 = cmd(2)+nonce(16)+len(1)+tag(12) | `ble-flow.html:514-521`; `ble-common.js:1951` (31), `:4092` (`MAX_ENCRYPTED_PACKET_SIZE = 185` = 2+16+155+12) |
| HA GATT write ceiling | — | `commands.py:68` `DEFAULT_MAX_FRAME = 244` | `ble-common.js:4669` (185 auth / 232 plain client_max_frame) |

The remembered "cmd(2)+nonce(16)+len(1)+data(154)+tag(12)=185" framing checks out: 185 − 31 overhead+len = 154 max ciphertext data, and 185 is used for encrypted direct-write/pipe framing (not the 0x0041 config path, which uses flat 200-byte chunks under a large negotiated MTU).

---

## 7. Suggested fix priority

1. **Finding 2** (JSON zero-fill) — fixable today in isolation with the raw-blob technique already used for `binary_inputs`; prevents silent field loss for every toolbox/HA-cache/CLI JSON consumer.
2. **Finding 1** (named fields) — prerequisite for exposing keep-alive to HA and the CLI; do together with Finding 4's comment cleanup and Finding 5's constants.
3. **Finding 3** (config.yaml description) — one-line doc fix; the firmware is already committed (`bcee206`/#100) without the doc update, so this is now an independent follow-up.
4. **Findings 6, 7** — small hardening items, no urgency.
