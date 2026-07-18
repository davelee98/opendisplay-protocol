# Plan — first draft of canonical `src/opendisplay_structs.h`

Agent-executable plan for authoring the **first draft** of the canonical shared
enums/constants/structs header. This is the wire-*payload* counterpart to
`src/opendisplay_protocol.h` (which owns opcodes / responses / errors / framing and
stays macro-only, untouched). Execute this plan to produce the header; do not start
codegen or downstream rollout from this document (see `docs/shared-types-plan.md`
§5–§11 for those later phases).

**Naming reconciliation:** `docs/shared-types-plan.md` used the working name
`opendisplay_types.h`. The decided file name is **`src/opendisplay_structs.h`** with
include guard **`OPENDISPLAY_STRUCTS_H`**. This is deliberate: `Firmware_Silabs/`
and `Firmware_NRF54/src/` already carry a local `opendisplay_structs.h` with that
exact guard, so the vendored canonical file becomes a **drop-in replacement by the
same mechanism as `opendisplay_protocol.h`** — same name, same guard, byte-identical
copies checked by `tools/sync_protocol_header.py`. Everywhere shared-types-plan says
`opendisplay_types.h`, read `opendisplay_structs.h`. Everything else in that doc
(annotated-C-canonical, libclang codegen, `@tag` vocabulary, IR/emitters, phased
rollout) is unchanged and this plan aligns with it.

**Consequence to record in the header banner:** the existing per-repo
`opendisplay_structs.h` files also contain in-memory-only structs (`GlobalConfig`,
`EncryptionSession`) that must NOT be canonicalized. Adopting repos must first move
those to a repo-local header (suggested: `app_structs.h` or keep in their existing
`structs.h`) before vendoring — this goes in the eventual rollout brief, not this
draft, but the banner must state the rule (mirror of protocol.h's "no typedefs here"
rule, inverted: "no in-memory/RAM-only types here").

---

## Sources of truth surveyed (byte layouts verified against all of these)

| Source | Path | Role |
|---|---|---|
| Design doc | `opendisplay-protocol/docs/shared-types-plan.md` | approach, `@tag` vocabulary, reconciliation list |
| Opcode header | `opendisplay-protocol/src/opendisplay_protocol.h` | conventions to mirror; SECTION 1 comment table of config-packet types |
| Arduino reference FW | `Firmware/src/structs.h` | richest superset (charge pins, min_wake, full_update_mC, power_off_*) |
| Silabs FW | `Firmware_Silabs/opendisplay_structs.h`, `opendisplay_constants.h` | simple_config_* fields, CONFIG_PKT_* table, skip-size constants |
| nRF52811 FW | `Firmware_NRF/structs.h` | most-behind variant (baseline reserved[] carve-ups) |
| nRF54 FW | `Firmware_NRF54/src/opendisplay_structs.h`, `opendisplay_constants.h` | NfcConfig, WifiConfig notes, TRANSMISSION_MODE bit names, bb_epaper map |
| Website schema | `opendisplay.org/httpdocs/firmware/toolbox/config.yaml` (`ble_proto`, version 1, minor_version 4) | field names, enums, bits, conditional_enum, outer-packet framing + CRC |
| External: bb_epaper | vendored in `Firmware_Silabs/third_party/bb_epaper`, used by Firmware + NRF54 (`opendisplay_epd_map.c`) | `EP*` panel identifiers, ColorScheme heritage |
| External: epaper-dithering | `epaper-dithering/packages/rust/core/src/palettes.rs`, `enums.rs` | `ColorScheme` (mirrors firmware values), `DitherMode` |

---

## 1. Inventory — everything that goes in the draft header

All multi-byte fields little-endian unless tagged `@endian be`. All wire structs
`__attribute__((packed))` with an `OD_STATIC_ASSERT(sizeof == N)` (see §4 for the
C99/C++ portability macro). "Canonical layout" below always means the reconciled
superset of §3.

### 1a. Config transfer framing (from `config.yaml packet_structure`)

| Item | Kind | Wire size | Notes |
|---|---|---|---|
| `OuterPacketHeader` | packed struct | 3 B | `length:u16 LE` (total incl. CRC), `version:u8` (= `OD_CONFIG_VERSION`). Followed by variable `single_packet` sequence, then `crc:u16 LE`. **FOLLOWUP `@doc`:** the `length` field is inconsistently populated across encoders — the website toolbox patches it to the real total, but py-opendisplay appears to leave the leading `u16` as zero pad (`[2B pad][version]…`). The CRC is *always* computed as if `length` were `0x0000` regardless. Verify whether any receiver relies on `length`, and document the contract (patched vs zero) before firmware adopts. |
| CRC spec constants | `#define`-style enum/consts | — | CRC16-CCITT poly `0x1021`, init `0xFFFF`, computed with the length field zeroed, CRC field excluded. `OD_CONFIG_CRC_POLY`, `OD_CONFIG_CRC_INIT` + `@doc` of the zero-length quirk. |
| `SinglePacketHeader` | packed struct | 2 B | `number:u8` (0-based sequential), `id:u8` (`@enum ConfigPacketType`). Payload size is fixed per id (= sizeof the struct). |
| `ConfigPacketType` | enum | u8 | 15 values, §1b table. Replaces per-repo `CONFIG_PKT_*` and protocol.h's SECTION-1 comment-only table (leave the comment there, add a cross-reference). |
| `OD_CONFIG_VERSION` / `OD_CONFIG_MINOR_VERSION` | macros | on-wire | `1` / `4` — unlike `OD_PROTOCOL_VERSION` these ARE transmitted (outer packet `version` byte) / negotiated. See §6 Q2. |
| `OD_PIN_UNUSED` | macro `0xFFu` | — | the pervasive "0xFF = pin not present" wire convention (today `GPIO_PIN_UNUSED` in Silabs + NRF54 constants). Wire-meaningful → canonical. |

### 1b. Config TLV payload structs (the 15 packet types)

Wire size = payload size after the 2-byte `SinglePacketHeader`. All packed.

| ID | yaml name | Canonical struct | Size | Repeatable (max) | Canonical layout source |
|---|---|---|---|---|---|
| 0x01 | system_config | `SystemConfig` | 22 | no | superset: Firmware + yaml (`pwr_pin_2/3` named) — §3.1 |
| 0x02 | manufacturer_data | `ManufacturerData` | 22 | no | superset: Silabs/NRF54 + yaml (`simple_config_*`) — §3.2 |
| 0x04 | power_option | `PowerOption` | 30 | no | superset: Firmware + yaml (charge pins, min_wake, screen_timeout) — §3.3 |
| 0x20 | display | `DisplayConfig` | 46 | yes (4) | superset: Firmware + yaml (`full_update_mC`, `cs_pin_2` naming) — §3.4 |
| 0x21 | led | `LedConfig` | 22 | yes (4) | identical everywhere |
| 0x23 | sensor_data | `SensorData` | 30 | yes (4) | superset: Firmware/NRF54 + yaml (`i2c_addr_7bit`, `msd_data_start_byte`) — §3.5 |
| 0x24 | data_bus | `DataBus` | 30 | yes (4) | identical everywhere |
| 0x25 | binary_inputs | `BinaryInputs` | 30 | yes (4) | superset: Firmware + yaml (`power_off_flags/_hold_sec`); pin-field naming — §3.6 |
| 0x26 | wifi_config | `WifiConfig` | 160 | no | identical (Firmware + NRF54; yaml). **§6 Q6 resolved — server fields promoted out of `reserved[]`:** `ssid[32]`, `password[32]`, `encryption_type`, `server_host[64]` @65, `server_port:u16 **@endian be**` @129, `reserved[29]` @131. Header is ahead of current firmware (was comment-only); backward-compatible carve of former reserved bytes. |
| 0x27 | security_config | `SecurityConfig` | 64 | no | identical everywhere |
| 0x28 | touch_controller | `TouchController` | 32 | yes (4) | identical (Firmware, NRF54, yaml; Silabs skips by size) |
| 0x29 | passive_buzzer (yaml) → buzzer | `BuzzerConfig` | 32 | yes (4) | identical layout; canonical name resolved to `OD_PKT_BUZZER`/`BuzzerConfig` (BUZZER) — §3.7 |
| 0x2A | nfc_config | `NfcConfig` | 32 | yes (2) | identical (Silabs, NRF54, yaml) |
| 0x2B | flash_config | `FlashConfig` | 32 | yes (2) | identical everywhere present |
| 0x2C | data_extended | `DataExtended` | 288 | no | identical (9 × 32-byte NUL-terminated zero-padded UTF-8 strings; tag `@doc "text"` per field) |

Silabs' skip-size constants (`CONFIG_PKT_TOUCH_SIZE 32`, `CONFIG_PKT_BUZZER_SIZE 32`,
`CONFIG_PKT_DATA_EXTENDED_SIZE 288`) become redundant — `sizeof(struct ...)` of the
canonical structs replaces them; note this for the rollout brief.

### 1c. Value enums (real C `enum`s, explicit values, `@enum`-bound from fields)

| Enum | Width | Values | Bound from | Notes |
|---|---|---|---|---|
| `ICType` | u16 | 1 NRF52840, 2 ESP32S3, 3 ESP32C3, 4 ESP32C6, 5 NRF52811, 6 EFR32BG22C222F352GM40, 7 NRF54L15, 8 NRF54LM20 | `SystemConfig.ic_type` | fixes py-opendisplay lag (stops at 6) |
| `ManufacturerId` | u16 | 0 DIY, 1 SEEED, 2 WAVESHARE, 3 SOL, 4 OPENDISPLAY | `ManufacturerData.manufacturer_id` | kept (small, C-safe, not a board name — §6 Q4) |
| ~~`BoardType*`~~ | — | — | — | **OUT entirely (§6 Q4).** Board name tables stay in `config.yaml` only; `board_type` remains a raw `u8` field, unenumerated here. |
| `PowerMode` | u8 | 1 BATTERY, 2 USB, 3 SOLAR | `PowerOption.power_mode` | |
| `CapacityEstimator` | u8 | 1 LI_ION, 2 LIFEPO4, 3 SUPERCAP, 4 LITHIUM_PRIMARY, 5 SEEED_LI_ION | `PowerOption.capacity_estimator` | |
| `DisplayTechnology` | u8 | 0 UNDEFINED, 1 E_PAPER, 2 LCD, 3 LED_MATRIX | `DisplayConfig.display_technology` | |
| `PanelIC` | u16 | ~113 values: 0–76 (with 65–66 contested, §3.9), 1000–1030 (M3 / EPD-nRF5 driver line), 3000–3001 (Seeed_GFX/ED103TC2) | `DisplayConfig.panel_ic_type` | external-related, §2 |
| `Rotation` | u8 | 0 ROT_0, 1 ROT_90, 2 ROT_180, 3 ROT_270 | `DisplayConfig.rotation` | |
| `PartialUpdateSupport` | u8 | 0 NONE, 1 SUPPORTED, 2 FULL_FRAME | `DisplayConfig.partial_update_support` | |
| `ColorScheme` | u8 | 0 MONO, 1 BWR, 2 BWY, 3 BWRY, 4 BWGBRY, 5 GRAY4, 6 GRAY16, **7 SEVEN_COLOR** (§3.8; no gray8 — was a mistake), 8 BWGBRY_SPLIT, 100 RGB565, 101 RGB888, 102 RGB16BPC | `DisplayConfig.color_scheme` | external-related, §2 |
| `LedType` | u8 | 0 RGB, 1 SINGLE, 2 RY, 3 FOUR_SEPARATE | `LedConfig.led_type` | |
| `SensorType` | u16 | 1 TEMPERATURE, 2 HUMIDITY, 3 AXP2101, 4 SHT40, 5 BQ27220 | `SensorData.sensor_type` | today `SENSOR_TYPE_*` macros in Firmware + NRF54 |
| `BusType` | u8 | 1 I2C, 2 SPI | `DataBus.bus_type` | replaces `OD_BUS_TYPE_I2C` |
| `InputType` | u8 | 1 BUTTON, 2 SWITCH | `BinaryInputs.input_type` | |
| `InputDisplayAs` | u8 | 1 AS_BUTTON, 2 AS_SWITCH | `BinaryInputs.display_as` | |
| `WifiEncryptionType` | u8 | 0 NONE, 1 WEP, 2 WPA, 3 WPA2, 4 WPA3 | `WifiConfig.encryption_type` | |
| `TouchIcType` | u16 | 0 NONE, 1 GT911 | `TouchController.touch_ic_type` | today `TOUCH_IC_*` macros |
| `NfcIcType` | u8 | 0 AUTO, 1 TNB132M | `NfcConfig.nfc_ic_type` | **Not redefined here.** structs.h `#include`s protocol.h (§6 Q5) and `@enum`-binds this field to protocol.h's existing `OD_NFC_IC_*` macros — declaring an `OD_NFC_IC_*` enumerator here would collide with the macro and fail to compile. |
| `NfcFieldDetectMode` | u8 | 0 DISABLED, 1 GPIO_LEVEL, 2 IRQ_LATCHED | `NfcConfig.field_detect_mode` | |
| `ActiveLevel` | u8 | 0 ACTIVE_LOW, 1 ACTIVE_HIGH | `NfcConfig.field_detect_active`, `.power_active`, `FlashConfig.power_active` | shared tiny enum |
| `FlashIcType` | u8 | 0 AUTO | `FlashConfig.flash_ic_type` | |

Do **not** enum-ify `button_data_byte_index` (yaml models it as an 11-entry enum
`byte_0..byte_10`; it is really a bounded integer — tag `@min 0 @max 10` instead).

### 1d. Bitfield groups (`@bits`-bound; emitted as bit-position `#define`s or a
tagged enum, matching whichever form codegen settles on — draft uses `#define OD_..._BIT`)

| Group | Bound from | Bits (LSB first) |
|---|---|---|
| `CommunicationModes` | `SystemConfig.communication_modes` | 0 BLE, 1 OEPL, 2 WIFI, 3–7 reserved |
| `DeviceFlags` | `SystemConfig.device_flags` | 0 PWR_PIN, 1 XIAO_INIT, 2 WS_PP_INIT, 3 PWR_LATCH, 4 PWR_LATCH_DFF, 5–7 reserved |
| `SleepFlags` | `PowerOption.sleep_flags` | 0 BUTTON_WAKE_DISABLE (Firmware; yaml says reserved — §3.10), 1–7 reserved |
| `BatterySenseFlags` | `PowerOption.battery_sense_flags` | 0 ENABLE_INVERTED (Firmware/NRF54; yaml says reserved — §3.10), 1–7 reserved |
| `ChargerFlags` | `PowerOption.charger_flags` | 0 CHARGE_ENABLE_ACTIVE_LOW, 1 CHARGE_STATE_ACTIVE_LOW, 2–7 reserved |
| `TransmissionModes` | `DisplayConfig.transmission_modes` | 0 STREAMING_DECOMPRESSION (naming clash — §3.7), 1 ZIP, 2 G5, 3 DIRECT_WRITE, 4 PIPE_WRITE, 5–6 reserved, 7 CLEAR_ON_BOOT (`@doc` synonym: yaml `no_boot_text` — §6 Q3) |
| `LedFlags` | `LedConfig.led_flags` | 0–3 LED1..LED4_INVERT, 4–7 reserved |
| `BusFlags` | `DataBus.bus_flags` | all reserved today |
| `PinBitmap` (shared shape) | `DataBus.pullups/.pulldowns`, `BinaryInputs.isused/.invert/.pullups/.pulldowns/.power_off_flags` | bit N = pin N+1 |
| `SecurityFlags` | `SecurityConfig.flags` | 0 REWRITE_ALLOWED, 1 SHOW_KEY_ON_SCREEN, 2 RESET_PIN_ENABLED, 3 RESET_PIN_POLARITY, 4 RESET_PIN_PULLUP, 5 RESET_PIN_PULLDOWN, 6–7 reserved |
| `TouchFlags` | `TouchController.flags` | 0 INVERT_X, 1 INVERT_Y, 2 SWAP_XY, 3–7 reserved |
| `NfcFlags` | `NfcConfig.flags` | 0 ENABLED, 1–7 reserved |
| `FlashFlags` | `FlashConfig.flags` | 0 ENABLED, 1–7 reserved |
| `BuzzerFlags` | `BuzzerConfig.flags` | 0 ENABLE_ACTIVE_HIGH, 1–7 reserved |

### 1e. Non-TLV wire payload structs (message payloads + advertisement)

These carry `@message CMD_*` (a small extension of `@packet` for opcode-carried
payloads; add to the tag table) instead of `@packet 0xNN`. protocol.h's `@request`
comment blocks remain the framing narrative; these structs are the machine-readable
layout for the fixed-size bodies. Fixed-size only — variable-tail messages stay
comment-documented in protocol.h.

| Struct | Message | Size | Fields (offsets after opcode/status framing) |
|---|---|---|---|
| `LedFlashPattern` | CMD_LED_ACTIVATE 0x0073 payload byte 1.. | 12 | b0: mode(low nibble; 1 = flash) + brightness-1(high nibble); b1/b4/b7: color1/2/3; b2/b5/b8: loop delay(high nibble)+loop count(low nibble); b3/b6/b9: inter-loop delay; b10: group repeats-1; b11: reserved. Extracted from `Firmware/src/device_control.cpp led_load_config()`; firmware persists it into `LedConfig.reserved[0..11]`. OEPL-heritage layout — `@doc` that. |
| `MsdAdvertisement` | BLE manufacturer-specific data + CMD_READ_MSD 0x0044 response | 16 | `company_id:u16 LE`, `dynamic[11]` (button/touch/SHT40/BQ27220 slots at config-driven indices), `chip_temperature:u8`, `battery_voltage_low:u8`, `status:u8`. Verified against `OD App/Models/AdvertisementData.swift` — this kills the "MSD framing lives nowhere" gap from shared-types-plan §1. |
| `MsdStatusBits` | `MsdAdvertisement.status` `@bits` | — | 0 BATTERY_VOLTAGE_BIT8 (9th bit of 10 mV voltage), 1 REBOOT_FLAG, 2 CONNECTION_REQUESTED, 3 reserved, 4–7 MAIN_LOOP_COUNTER (`@doc` nibble counter) |
| `PipeStartRequest` | CMD_PIPE_WRITE_START 0x0080 | 10 | `version:u8`, `flags:u8 @bits PipeFlags`, `req_window:u8`, `req_ack_every:u8`, `client_max_frame:u16 le`, `total_size:u32 le` (six fields sum to 10; protocol.h's 0x0080 block confirms "10-byte header, 22 when partial" = 10 + 12-byte `PipePartialExt`. Earlier "8" was an arithmetic slip.) |
| `PipePartialExt` | 0x0080 iff flags bit1 | 12 | `old_etag:u32 le`, `x:u16 le`, `y:u16 le`, `w:u16 le`, `h:u16 le` — the LE twin of the 0x76 BE geometry |
| `PipeStartResponse` | 0x0080 ACK data | 6 | `version:u8`, `max_window:u8`, `max_ack_every:u8`, `max_frame:u16 le`, `resp_flags:u8` |
| `PipeSack` | 0x0081 ACK data | 5 | `highest_seen:u8`, `ack_mask:u32 le` |
| `PartialWriteStartHeader` | CMD_PARTIAL_WRITE_START 0x0076 | 17 | `flags:u8`, `old_etag:u32 be`, `new_etag:u32 be`, `x:u16 be`, `y:u16 be`, `width:u16 be`, `height:u16 be` — **struct-level `@endian be`**; the showcase for the per-message endianness split (`@doc` cross-ref to `PipePartialExt`) |
| `AuthChallenge` | CMD_AUTHENTICATE 0x0050 step-1 response data | 21 | `status:u8`, `server_nonce[16]`, `device_id:u32 le` |
| `AuthProof` | 0x0050 step-2 request payload | 32 | `client_nonce[16]`, `mac[16]` (AES-CMAC) |

The buzzer 0x0077 pattern payload is variable-length (`[instance][outer_repeats]
[n_patterns]` then per-pattern `n_steps` + n_steps × (`freq:u8`,`duration:u8`)) —
stays comment-only in protocol.h; add a `@doc` cross-reference from
`BuzzerConfig`.

**Inventory totals:** 15 config TLV structs + 2 framing structs + 9 message/adv
structs = **26 packed structs** (`MsdStatusBits` is a `@bits` group, not a struct —
earlier "27" miscounted it); **17 value enums** (the 4 conditional `BoardType`
sets are OUT — §6 Q4; board names stay in `config.yaml`) + **15 bitfield groups**;
plus ~8 loose wire constants (`OD_CONFIG_VERSION`,
CRC pair, `OD_PIN_UNUSED`, config version pair, etc.).

---

## 2. Local vs external classification

### OpenDisplay-local (canonically defined in this header, no outside owner)

Everything in §1 **except** the four rows below: all 15 TLV structs, framing,
`ICType`, `ManufacturerId`, `PowerMode`, `CapacityEstimator`,
`DisplayTechnology`, `Rotation`, `PartialUpdateSupport`, `LedType`, `SensorType`,
`BusType`, `InputType`, `InputDisplayAs`, `WifiEncryptionType`, `TouchIcType`,
`NfcFieldDetectMode`, `ActiveLevel`, `FlashIcType`, all bitfield
groups, all message payload structs, `MsdAdvertisement`. (Note: `NfcIcType` is NOT
defined here — the `nfc_ic_type` field binds to protocol.h's `OD_NFC_IC_*` macros;
see §6 Q5.)

### External-related (values that mirror or must stay compatible with an outside component)

| Item | External component | Relationship found in survey | Recommendation |
|---|---|---|---|
| `PanelIC` (wire values 0–76, 1000–1030, 3000+) | **bb_epaper** (vendored in Firmware_Silabs `third_party/`, `#include <bb_epaper.h>` in Firmware, mapped in NRF54) | Wire values are **OpenDisplay-owned**; each firmware converts wire id → bb_epaper `EP*` constant through a repo-local `opendisplay_epd_map.c` (NRF54's map covers 0x00–0x4C with explicit UNDEFINED/substitution fallbacks for panels not vendored into its bb_epaper copy). Names of the 0–76 range deliberately mirror bb_epaper `EP*` spellings; 1000–1030 come from the M3/EPD-nRF5 driver line; 3000+ from Seeed_GFX runtime. | **Canonical copy here** with `@doc` per value and a section-level `@external bb_epaper` note: "names track bb_epaper panel identifiers; the value→`EP*` map is per-repo (`opendisplay_epd_map.c`) and NOT part of this header; bb_epaper never dictates wire values." No ownership problem — OD owns the numbers. |
| `ColorScheme` (0–8, 100–102) | **bb_epaper** (comment in `Firmware/src/structs.h`: "add more entries to match the ColorScheme enum from bb_epaper") **and** **epaper-dithering** (`palettes.rs`: "Integer discriminants match OpenDisplay firmware", "firmware API contracts — never change them") | Three homes today. The Rust crate explicitly declares itself a **mirror of firmware**, i.e. it already concedes ownership to OpenDisplay. bb_epaper informs which schemes exist but its enum is not the wire contract. | **Canonical here** (`@external mirror: epaper-dithering palettes.rs ColorScheme; informed-by: bb_epaper`). After landing, epaper-dithering should add `BwgbrySplit = 8` (currently missing) and the value-7 resolution (§3.8) must be pushed to all three homes. This resolves shared-types-plan §12's ownership open question in favor of **this repo owns; the crate mirrors**. |
| `DitherMode` (0–8) | **epaper-dithering** (`enums.rs`) | Not a BLE wire value: appears in no config struct, no opcode payload — it is a host-side image-preparation parameter shared by website/app/python. | **Stays OUT** of this header. Document as a one-line reference in the banner ("dither algorithm ids are owned by epaper-dithering; they are not wire values"). If it ever lands in a config packet, it enters here with `@external`. |
| `LedFlashPattern` layout; `legacy_tagtype`; `communication_modes` bit1 `OEPL` | **OEPL (OpenEPaperLink)** heritage | The 12-byte LED flash layout and the legacy tag-type field reproduce OEPL conventions for compatibility. | Canonical here (OD owns its wire), with `@doc "OEPL-compatible layout"` so nobody 'fixes' the odd nibble packing. |

Also intentionally external-and-out: silicon-specific pin *encodings* (e.g. NRF54's
`(port<<4)|pin` GPIO convention noted on `BuzzerConfig.drive_pin`, Silabs
`0xPN`) — the wire field is a raw u8; the encoding is target-defined. Add a single
banner sentence, not per-field tags.

---

## 3. Divergence reconciliation (resolve BEFORE writing the header)

Rule (from shared-types-plan §7): canonical layout = the **superset** — every field
any repo or the yaml promoted out of `reserved[]` gets its name; `reserved[]`
shrinks; total sizes never change. Offsets below are payload-relative (after the
2-byte single-packet header).

1. **`SystemConfig` (22 B)** — offsets 20–21: Firmware + yaml name them
   `pwr_pin_2` / `pwr_pin_3`; NRF/NRF54/Silabs have `reserved[17]` (5–21).
   → Canonical: `reserved[15]` (5–19), `pwr_pin_2` (20), `pwr_pin_3` (21).

2. **`ManufacturerData` (22 B)** — NRF54/Silabs + yaml define
   `simple_config_driver_index:u16` (4), `simple_config_display_index:u16` (6),
   `simple_config_power_index:u16` (8), `simple_config_configured_at[6]` (10–15,
   48-bit LE Unix seconds), `reserved[6]` (16–21); Firmware/NRF see `reserved[18]`.
   → Canonical: the named layout.

3. **`PowerOption` (30 B)** — common prefix through `deep_sleep_time_seconds`
   (0–19). Tail 20–29 has three interpretations: Firmware + yaml:
   `charge_enable_pin` (20), `charge_state_pin` (21), `charger_flags` (22),
   `min_wake_time_seconds:u16` (23–24), `screen_timeout_seconds` (25),
   `reserved[4]` (26–29); NRF54 stops after `charger_flags` (`reserved[7]`);
   NRF/Silabs: `reserved[10]`.
   → Canonical: the full Firmware/yaml layout (it is the superset; yaml confirms
   every offset).

4. **`DisplayConfig` (46 B)** — two deltas at the tail:
   - offset 24 (after `clk_pin` at 23): yaml names it **`cs_pin_2`** ("second CS
     for displays split over 2 drivers", real for dual-controller Spectra panels);
     all four firmwares call it `reserved_pin_2`.
     → Canonical: **`cs_pin_2`** (semantic wins; firmware renames on adoption).
   - offsets 31–32: Firmware + yaml define `full_update_mC:u16` (energy per full
     refresh); others fold into `reserved[15]`.
     → Canonical: `full_update_mC` + `reserved[13]` (33–45).
   - field-name normalization: yaml `legacy_tagtype` vs firmware `tag_type` at
     12–13. → Canonical: **`legacy_tag_type`** (keeps yaml's semantic "legacy",
     snake-cased; regenerated yaml then matches exactly).
   - `dc_pin` (17) doc: Firmware repurposes it as "SPI MISO for Seeed ED103/IT8951".
     Layout identical; write the `@doc` as "data/command select; MISO on
     OpenDisplay-runtime IT8951 panels" — doc merge, no bytes.

5. **`SensorData` (30 B)** — Firmware/NRF54 + yaml define `i2c_addr_7bit` (4) and
   `msd_data_start_byte` (5) with `reserved[24]`; NRF/Silabs see `reserved[26]`.
   → Canonical: the named layout.

6. **`BinaryInputs` (30 B)** — two reconciliations:
   - offsets 16–17: Firmware + yaml define `power_off_flags` (16),
     `power_off_hold_sec` (17), `reserved[12]`; others `reserved[14]`.
     → Canonical: named layout.
   - naming: pins 3–10 are `reserved_pin_1..8` in every firmware but
     **`inputpin1..8`** in yaml; offset 11 is `input_flags` in firmware but
     **`isused`** (pin-used bitmap) in yaml.
     → Canonical: **`input_pin_1..8`** and **`pins_used`** (semantic, normalized
     snake_case). Both firmware and regenerated yaml rename; bytes unchanged.

7. **Constant-name drift** (same values, conflicting names — pick one):
   - `TRANSMISSION_MODE` bit0: `ZIPXL` (Firmware, Silabs) vs
     `STREAMING_DECOMPRESSION` (NRF54, yaml).
     → Canonical: **`OD_TXM_STREAMING_DECOMPRESSION`** — matches the yaml/UI name
     and describes the actual capability (512-byte-window streaming inflate).
   - `TRANSMISSION_MODE` bit7: `CLEAR_ON_BOOT` (Firmware, NRF54) vs yaml
     `no_boot_text`. → **RESOLVED per §6 Q3 (2026-07-18): canonical name is
     `OD_TRANSMISSION_MODE_CLEAR_ON_BOOT`.** The `@doc` must note it is **synonymous
     with yaml's `no_boot_text` / "NO_BOOT_TEXT"** (same bit, same behavior — clearing
     the screen on boot is why no boot text appears) so the two names are never read as
     different flags.
   - `CONFIG_PKT 0x29`: `BUZZER` (Silabs) vs `PASSIVE_BUZZER` (NRF54, yaml).
     → **RESOLVED — canonical is `OD_PKT_BUZZER` / `struct BuzzerConfig` (BUZZER,
     matching Silabs).** config.yaml's packet name `passive_buzzer` is regenerated to
     `buzzer`; the `@doc` records the former split. (The device is still a passive
     piezo buzzer — noted in prose — but the wire name is BUZZER.)
8. **`ColorScheme` value 7 — RESOLVED (2026-07-18): 7 = `SEVEN_COLOR` ("7color")**.
   config.yaml's `7color` is canonical (it is shipped in the website UI). The
   firmware `COLOR_SCHEME_GRAY8 = 7` and epaper-dithering `Grayscale8 = 7` were a
   **mistake — "gray8" does not exist as a color scheme** and is removed outright
   (not renumbered). Draft ships `OD_COLOR_SCHEME_SEVEN_COLOR = 7`; the firmware
   macro is deleted and the crate replaces `Grayscale8` with `SevenColor = 7`
   (see §6 Q1 / Q8).
9. **`PanelIC` 65/66 gap**: yaml skips 65–66, but `Firmware/src/structs.h` defines
   `PANEL_IC_EP133A_SPECTRA_1200X1600 0x0042u` (= 66, reTerminal E1004 13.3"
   dual-controller), and NRF54's map has `0x41`/`0x42 → EP_PANEL_UNDEFINED`.
   → Canonical: define 66 = `EP133A_SPECTRA_1200X1600`; leave 65 reserved;
   regenerated yaml gains the entry (website catches up).
10. **Flag bits yaml is behind on**: `sleep_flags` bit0
    (`SLEEP_FLAG_BUTTON_WAKE_DISABLE`, Firmware) and `battery_sense_flags` bit0
    (`BATTERY_SENSE_FLAG_ENABLE_INVERTED`, Firmware + NRF54) are `reserved_0` in
    yaml. Firmware behavior is shipped → canonical names the bits; regenerated
    yaml inherits them.
11. **Already identical everywhere** (canonicalize as-is, zero risk): `LedConfig`,
    `DataBus`, `SecurityConfig`, `TouchController`, `BuzzerConfig`,
    `NfcConfig`, `FlashConfig`, `DataExtended`.
12. **`WifiConfig`** — identical across all sources today (all `reserved[95]`), but
    the canonical draft **intentionally extends** it: per §6 Q6 the server
    `server_host[64]` + `server_port:u16 @endian be` are promoted out of `reserved[]`
    (backward-compatible carve of former must-be-zero bytes). So the header is ahead
    of current firmware here — not "as-is". Firmware picks up the names on vendor.

---

## 4. Proposed header organization

Mirror `opendisplay_protocol.h`'s shape exactly: banner (purpose → versioning
policy → changelog → agent instructions → canonical location/vendoring →
language rule → tag convention), then numbered sections. Differences from
protocol.h's banner to state explicitly:

- **Language rule (inverted from protocol.h):** this header CONTAINS real `enum`s
  and packed `struct`s (that is its purpose — they are the codegen source). It
  must compile clean as C99 **and** C++ (`cc -std=c99 -fsyntax-only` /
  `c++ -fsyntax-only`, same gate as protocol.h). No functions, no in-memory/
  RAM-only types, no repo-specific values. Enums are plain (not `enum class`),
  every enumerator has an explicit value.
- **Static asserts:** define once at top:
  ```c
  #if defined(__cplusplus)
    #define OD_STATIC_ASSERT(expr, msg) static_assert(expr, msg)
  #elif defined(__STDC_VERSION__) && __STDC_VERSION__ >= 201112L
    #define OD_STATIC_ASSERT(expr, msg) _Static_assert(expr, msg)
  #else
    #define OD_STATIC_ASSERT(expr, msg) typedef char od_static_assert_[(expr) ? 1 : -1]
  #endif
  ```
  (C99 fallback needed because the syntax gate runs `-std=c99`.) Every packed
  struct is followed by `OD_STATIC_ASSERT(sizeof(struct X) == N, "X wire size");`.
- **Versioning:** same MAJOR.MINOR policy + append-only changelog + AGENT
  INSTRUCTIONS block as protocol.h, but scoped to *payload* layout changes
  (renaming a reserved byte = MINOR doc-level or no bump; moving/resizing a field
  = MAJOR). Plus the on-wire `OD_CONFIG_VERSION`/`OD_CONFIG_MINOR_VERSION` pair
  (see §6 Q2 for how the two version schemes relate).
- **Blob-wide conventions (from `config.yaml` `meta`):** the banner MUST state the
  four global rules that govern every struct here, since they are not in protocol.h
  (that is the message layer): (1) **byte order is little-endian by default** —
  multi-byte fields are LE unless an `@endian be` tag says otherwise; (2) all wire
  structs are **`packed`** (no implicit padding); (3) **reserved fields MUST be 0**
  and are ignored by older parsers (forward-compat); (4) **bitfields number from bit 0
  = LSB**, and unused bits MUST be 0. These are the contract a reader assumes when
  decoding, so they belong up top, once, not repeated per field.

### Section layout

| § | Content | Primary tags used |
|---|---|---|
| 1 | Config transfer framing: `OuterPacketHeader`, `SinglePacketHeader`, CRC constants, `OD_CONFIG_VERSION` | `@packet` (framing), `@doc`, `@endian` |
| 2 | `ConfigPacketType` enum (15 ids). Per-packet `required`/`repeatable(max)` are carried as `@required`/`@repeatable` tags on each struct in §4, not a separate comment table | `@enum`, `@doc` |
| 3 | Shared scalar constants: `OD_PIN_UNUSED`, `ActiveLevel` | `@doc` |
| 4 | Per-packet blocks, in packet-id order. Each block = its enums, then its `@bits` groups, then the packed struct + size assert (system → manufacturer → power → display → led → sensor → data_bus → binary_inputs → wifi → security → touch → buzzer → nfc → flash → data_extended) | `@packet`, `@required`, `@repeatable`, `@enum`, `@bits`, `@endian`, `@reserved`, `@unit`, `@min/@max/@default`, `@since`, `@doc` |
| 5 | External-related enum annex: `PanelIC`, `ColorScheme` (kept adjacent to §4 display block or inline there — draft choice; inline preferred, with `@external` notes) | `@enum`, `@external`, `@doc` |
| 6 | Message payload structs (pipe, partial, auth, LED flash) | `@message`, `@endian` (incl. struct-level `be` on `PartialWriteStartHeader`) |
| 7 | MSD advertisement: `MsdAdvertisement` + `MsdStatusBits` + dynamic-area index conventions | `@bits`, `@doc` |

**Authoring principle — the config payload CONTENTS must read as sequential,
human-readable documentation.** The per-packet blocks (§ section 4) are not just a
codegen source; `opendisplay_structs.h` is intended to be the place an engineer or
agent reads **top-to-bottom, in wire order, to understand the entire config blob
without opening `config.yaml` or any firmware parser.** So every field, in the order
it sits on the wire, carries a **detailed human comment** alongside its machine tags:
what the field is, its valid values / enum meanings, units, default, and any wire
nuance (endianness, "0xFF = unused", reserved-must-be-zero). Reserved bytes are
labelled with what space they hold and why. The `@tag` annotations and the prose live
in the **same trailing comment** — tags for codegen, prose for humans — and the prose
is the primary deliverable of this header, not an afterthought. Match the density and
clarity of `opendisplay_protocol.h`'s per-opcode blocks. A reader should be able to
hand-decode a captured config blob byte-by-byte from this file alone.

**Codegen must carry that documentation through to every target language.** The
generated output for each language is not bare structs/enums — it must reproduce the
human-readable documentation as **idiomatic doc comments in that language** (Swift
`///` doc comments on each `enum` case / struct property, Python docstrings or field
comments on each `IntEnum`/dataclass member, JSDoc `/** */` on each constant/field,
and the equivalent for any future target). Codegen therefore extracts the `@doc` prose
(and the enum/value descriptions) from the canonical header's comments and re-emits it,
so each generated file reads as a documented, human-usable module in its own right —
never a values-only dump. The generated structs (or the language's natural equivalent
— dataclass, `OptionSet`, `IntEnum`, object of consts) must be as readable and
self-explaining in Swift/Python/JS as `opendisplay_structs.h` is in C. (Record this as
an emitter requirement in `docs/shared-types-plan.md` §5 alongside the per-target
outputs.)

New tags this header introduces (add rows to shared-types-plan's tag table when
implementing codegen):
- **`@message CMD_X`** (struct) — opcode-carried payload, direction from protocol.h's `@dir`.
- **`@external <component>`** (enum/struct) — mirrored/related outside component; emit a provenance note downstream.
- **`@required`** (struct, config packet) — this packet MUST be present in a valid config blob (from yaml `required: true`; e.g. `system_config`, `manufacturer_data`). Codegen surfaces it for validators.
- **`@repeatable [max=N]`** (struct, config packet) — this packet MAY appear more than once in the blob, up to `N` instances (from yaml `repeatable: true` + the max; e.g. `display` max 4). Absent ⇒ exactly one. Replaces the "repeatability/max-instances table in comments" that §4 §2 previously proposed — it now lives as a per-struct tag.

### Template blocks (illustrative — the author copies this shape)

Annotated struct (showing `@packet`, `@enum`, `@bits`, `@unit`, `@since`,
`@reserved`, and the reconciled §3.4 layout):

```c
/** @struct DisplayConfig  @packet 0x20  @repeatable max=4  @doc "Per-panel configuration" */
struct DisplayConfig {
    uint8_t  instance_number;        /**< @doc "0-based display index" */
    uint8_t  display_technology;     /**< @enum DisplayTechnology */
    uint16_t panel_ic_type;          /**< @enum PanelIC @endian le */
    uint16_t pixel_width;            /**< @unit px @endian le */
    uint16_t pixel_height;           /**< @unit px @endian le */
    uint16_t active_width_mm;        /**< @unit mm @endian le */
    uint16_t active_height_mm;       /**< @unit mm @endian le */
    uint16_t legacy_tag_type;        /**< @endian le @doc "OEPL legacy tag type (optional)" */
    uint8_t  rotation;               /**< @enum Rotation */
    uint8_t  reset_pin;              /**< @doc "0xFF = none" @default 0xFF */
    uint8_t  busy_pin;               /**< @doc "0xFF = none" @default 0xFF */
    uint8_t  dc_pin;                 /**< @doc "data/command; MISO on IT8951 runtime panels" */
    uint8_t  cs_pin;                 /**< @doc "SPI chip select; 0xFF = none" */
    uint8_t  data_pin;               /**< @doc "MOSI / data line" */
    uint8_t  partial_update_support; /**< @enum PartialUpdateSupport */
    uint8_t  color_scheme;           /**< @enum ColorScheme */
    uint8_t  transmission_modes;     /**< @bits TransmissionModes */
    uint8_t  clk_pin;                /**< @doc "SPI SCLK" */
    uint8_t  cs_pin_2;               /**< @doc "second CS for dual-driver panels; 0xFF = none" @since 1.4 */
    uint8_t  reserved_pin_3;         /**< @reserved */
    uint8_t  reserved_pin_4;         /**< @reserved */
    uint8_t  reserved_pin_5;         /**< @reserved */
    uint8_t  reserved_pin_6;         /**< @reserved */
    uint8_t  reserved_pin_7;         /**< @reserved */
    uint8_t  reserved_pin_8;         /**< @reserved */
    uint16_t full_update_mC;         /**< @unit mC @endian le @doc "energy per full refresh; 0 = unknown" */
    uint8_t  reserved[13];           /**< @reserved */
} __attribute__((packed));
OD_STATIC_ASSERT(sizeof(struct DisplayConfig) == 46, "DisplayConfig wire size");
```

Annotated enum (showing `@width`, `@external`, per-value `@doc`):

```c
/** @enum ColorScheme  @width 1
 *  @external mirror: epaper-dithering packages/rust/core/src/palettes.rs (values MUST match)
 *  @doc "display color/plane scheme; drives plane count and packing" */
enum ColorScheme {
    OD_COLOR_SCHEME_MONO         = 0,   /**< @doc "1bpp black/white" */
    OD_COLOR_SCHEME_BWR          = 1,   /**< @doc "black/white/red" */
    OD_COLOR_SCHEME_BWY          = 2,   /**< @doc "black/white/yellow" */
    OD_COLOR_SCHEME_BWRY         = 3,   /**< @doc "black/white/red/yellow" */
    OD_COLOR_SCHEME_BWGBRY       = 4,   /**< @doc "Spectra 6-color" */
    OD_COLOR_SCHEME_GRAY4        = 5,   /**< @doc "2bpp 4-gray" */
    OD_COLOR_SCHEME_GRAY16       = 6,   /**< @doc "4bpp 16-gray" */
    OD_COLOR_SCHEME_SEVEN_COLOR  = 7,   /**< @doc "7-color (Spectra/ACeP 7)"  @changed "2.0: value 7 is 7color; former COLOR_SCHEME_GRAY8=7 was a mistake, removed" */
    OD_COLOR_SCHEME_BWGBRY_SPLIT = 8,   /**< @doc "Spectra 6 nibbles, left plane then right (dual-CS, no FB)" */
    OD_COLOR_SCHEME_RGB565       = 100, /**< @doc "RGB565 (non-epaper)" */
    OD_COLOR_SCHEME_RGB888       = 101, /**< @doc "RGB888 (non-epaper)" */
    OD_COLOR_SCHEME_RGB16BPC     = 102  /**< @doc "16 bits per channel (non-epaper)" */
};
```

Naming convention decision embedded above: struct and field names stay **exactly**
as the firmware repos spell them today (`DisplayConfig`, `instance_number`) so the
vendored header is a drop-in for existing code; **enumerators and bit macros get an
`OD_` prefix** because unprefixed spellings collide with per-repo macros during
migration (`COLOR_SCHEME_MONO` is a live macro in `Firmware/src/structs.h` — a
same-name enumerator would shadow/clash; the `OD_` prefix lets both coexist during
a repo's transition, then the local macros are deleted per the established
macro-dedup rule from `docs/rollout-plan.md`).

---

## 5. What stays OUT (must NOT be canonicalized)

Per repo, verified present in the surveyed files — the adopting-repo briefs will
need this list verbatim:

- **In-memory-only structs**: `GlobalConfig` (all four repos — includes
  RAM-layout choices like instance array sizes and `*_count` fields),
  `ImageData` (Firmware, NRF), `EncryptionSession` (Silabs, NRF54, NRF),
  `PipeReorderSlot` / `PipeWriteState` (Firmware), `ButtonState` (Firmware).
- **RAM/buffer tuning**: `PIPE_REORDER_SLOTS` / `PIPE_MAX_W` / `PIPE_MAX_N` /
  `PIPE_REORDER_SLOT_SIZE` and the `#ifdef PIPE_SMALL_DRAM_WINDOW` knob
  (Firmware), `BOOT_ROW_BUFFER_SIZE` (Firmware), `MAX_BUTTONS` (Firmware),
  Silabs `MAX_ENCRYPTED_*`-class buffer sizes.
- **GPIO pin values / board wiring** and per-target pin *encodings*
  (`(port<<4)|pin` etc.) — the wire carries raw u8; meaning is per-target.
- **Board / manufacturer name tables** (`board_type` `conditional_enum` values, per-
  manufacturer board names) — §6 Q4: OUT entirely. `config.yaml` is the sole owner;
  firmware never named them. `board_type` stays a raw `u8` field, `manufacturer_id`
  keeps only its small flat `ManufacturerId` enum.
- **Repo-local driver maps**: `opendisplay_epd_map.c/.h` (NRF54, Silabs) — the
  PanelIC→bb_epaper conversion is per-repo by design (different vendored panel
  sets, different fallbacks).
- **External components' own enums**: bb_epaper `EP*` / `BBEP_*` constants;
  epaper-dithering `DitherMode`, `ToneCompression`, `GamutCompression`.
- **Obsoleted by this header** (delete downstream, don't port): Silabs
  `CONFIG_PKT_*_SIZE` skip constants (use `sizeof`), per-repo `CONFIG_PKT_*`,
  `SENSOR_TYPE_*`, `TOUCH_IC_*`, `TRANSMISSION_MODE_*`, `SECURITY_FLAG_*`,
  `CHARGER_FLAG_*`, `BUZZER_FLAG_*`, `FLASH_CONFIG_FLAG_*`, `GPIO_PIN_UNUSED`,
  `OD_BUS_TYPE_I2C` macro groups.
- Anything already owned by `opendisplay_protocol.h` (opcodes, RESP_*, error
  namespaces, NFC sub-protocol bytes incl. `OD_NFC_IC_*`, chunk budgets, encryption
  envelope sizes) — **never redefine.** structs.h `#include`s protocol.h (§6 Q5
  resolved) and references these; redeclaring any of them collides with the macro and
  fails to compile.

---

## 6. Open questions / decisions needing a human call

1. **`ColorScheme` value 7** (§3.8): **RESOLVED 2026-07-18 — 7 = `SEVEN_COLOR`
   ("7color") is canonical** (matches config.yaml, which is shipped in the website
   UI). **`gray8` does not exist — it was a mistake** and is dropped entirely
   (no value 9, no renumber). Consequences to carry through, tracked in Q8:
   delete the Firmware `COLOR_SCHEME_GRAY8` macro, and remove epaper-dithering's
   erroneous `Grayscale8 = 7` (replace with `SevenColor = 7`). The draft ships
   `OD_COLOR_SCHEME_SEVEN_COLOR = 7` with an `@changed` note; there is no gray8
   entry at any value. No longer blocking.
2. **Version scheme coupling**: **RESOLVED 2026-07-18 — carry both.**
   `opendisplay_structs.h` carries (a) its own spec `MAJOR.MINOR` à la protocol.h
   **and** (b) the on-wire `OD_CONFIG_VERSION 1` / `OD_CONFIG_MINOR_VERSION 4`. The
   banner must spell out that (b) is what the outer packet transmits and (a)
   documents this file. Note: the app's bundled yaml is at minor 3 — **landing this
   header freezes minor 4 as canonical** (the app copy must catch up).
3. **`transmission_modes` bit7 semantics** (§3.7): **RESOLVED 2026-07-18 — canonical
   name `OD_TRANSMISSION_MODE_CLEAR_ON_BOOT`.** The `@doc` notes it is **synonymous
   with yaml's `no_boot_text` / `NO_BOOT_TEXT`** — same bit, same behavior (clearing
   the screen on boot is why no boot text is shown). e.g.
   `@doc "Clear screen on boot. Synonymous with yaml's no_boot_text / NO_BOOT_TEXT."`
   No wire value changes; regenerated yaml keeps its `no_boot_text` display name and
   maps to this bit.
4. **Board / manufacturer name tables**: **RESOLVED 2026-07-18 — left OUT of this
   header entirely.** `board_type` is the only `conditional_enum` in the schema; the
   board tables are large, display-name-heavy, and were never named in firmware C
   (only `uint8_t board_type;`), so this header does NOT define `BoardType*` enums.
   `config.yaml` remains the sole owner of the board/manufacturer value tables;
   codegen does not generate them from here. The `ManufacturerData.board_type` and
   `manufacturer_id` **fields stay** (raw wire bytes, part of the packed layout) —
   only their *value enumerations* are omitted. Consequence: **the `@enum_when` tag
   now has no consumer and is retired from this header** (its sole use was
   `board_type`); reintroduce it only if a future conditional field needs it.
   `ManufacturerId` (a clean 5-value, C-safe flat enum — not a board name) is kept;
   flag if you want it out too.
5. **Include relationship with protocol.h**: **RESOLVED 2026-07-18 —
   `opendisplay_structs.h` `#include`s `opendisplay_protocol.h`.** So `@message`
   structs reference `CMD_*` directly and nothing is duplicated. Two rules this
   forces (both wanted anyway):
   - **structs.h must NEVER redeclare a name protocol.h already `#define`s.** This is
     not optional: protocol.h defines `OD_NFC_IC_*` as **macros**; if structs.h also
     declared an `enum` member `OD_NFC_IC_AUTO`, the macro would textually rewrite the
     enumerator (`enum { 0 = 0 }`) → **compile error**. This bites in *every* firmware
     TU (both headers are included together regardless), so it is a correctness rule,
     not a style choice.
   - Therefore **do not define an `OD_NFC_IC_*` enum in structs.h.** The `NfcConfig`
     `nfc_ic_type` field is `@enum`-bound to protocol.h's existing `OD_NFC_IC_*`
     macros. (If a typed enum name is needed for codegen, give it distinct enumerator
     spellings that do not collide with any protocol.h macro, or — preferred — bind
     the field to the macros via a tag and let codegen synthesize the per-language
     enum.) The banner states protocol.h is a prerequisite include.
6. **`WifiConfig.reserved` carve-up**: **RESOLVED 2026-07-18 — promote the server
   fields into named struct members** (pulled into scope). NRF54's comment records
   that the server host/port ride inside `reserved[95]`, with `server_port` **big-
   endian** at reserved indices 64–65. The canonical struct names them:
   - `server_host[64]` — server URL/hostname, null-terminated/zero-padded (like
     `ssid`/`password`), at reserved indices 0–63 → **struct offsets 65–128**.
   - `server_port` — `u16` **`@endian be`** at reserved indices 64–65 → **struct
     offsets 129–130** (the single big-endian field in an otherwise little-endian
     struct — the `@endian be` tag is mandatory here).
   - `reserved[29]` — remaining, at struct offsets 131–159.
   New layout: `ssid[32]` @0, `password[32]` @32, `encryption_type` @64,
   `server_host[64]` @65, `server_port:u16 be` @129, `reserved[29]` @131 = 160B
   (unchanged total). This is a **backward-compatible** carve (the bytes were
   `reserved`-must-be-zero, so old peers that zeroed them still interop; only a peer
   that actually writes a port must honor BE). The canonical header is thus AHEAD of
   all current firmware (none name these today — it was comment-only); firmware
   adopts on vendor. Add a `@since`/`@doc` noting the promotion.
7. **Message payload structs in-or-out** (§1e): **RESOLVED 2026-07-18 — INCLUDE
   them.** The fixed-layout opcode payloads (`LedFlashPattern`, `MsdAdvertisement`,
   `PipeStartRequest`/`Response`, `PipePartialExt`, `PipeSack`,
   `PartialWriteStartHeader`, `AuthChallenge`, `AuthProof`) live in this header as
   the single machine-readable layout source — they are exactly the layouts
   hand-parsed and drifting in Swift/JS today. Two guardrails:
   - **Boundary — fixed-layout only.** Variable/streaming payloads (pipe DATA chunks,
     the buzzer 0x0077 pattern list) are NOT modeled as structs; they stay prose in
     protocol.h with a `@doc` cross-reference (already handled in §1e).
   - **One home per layout (anti-drift).** Byte offsets live in the struct, narrative
     lives in protocol.h. Where a struct exists, protocol.h's `@request` block **points
     to it** (`@request see struct PipeStartRequest`) rather than re-listing offsets, so
     the two files cannot disagree. This is a doc-only edit to protocol.h (no version
     bump) — fold into the execution-checklist step that adds the SECTION-1
     cross-reference.
8. **`ColorScheme` propagation to epaper-dithering + Firmware** (Q1 now resolved):
   who files the coordinated PRs? Required changes: (a) epaper-dithering — replace
   `Grayscale8 = 7` with `SevenColor = 7` and add `BwgbrySplit = 8` (currently
   missing); (b) Firmware — delete the erroneous `COLOR_SCHEME_GRAY8 = 7` macro.
   The crate's comment says "never change them", so both need a coordinated PR
   referencing this header once landed. **Owner: TBD.**
9. **`required` / `repeatable` packet attributes**: **RESOLVED 2026-07-18 — added as
   `@required` / `@repeatable [max=N]` struct-level tags** (from yaml `required:` /
   `repeatable:` + max-instances). These config-blob constraints are not expressible
   in a plain C struct, so they ride in the struct's `@tag` comment; codegen surfaces
   them for validators. See the tag-list note in §4. The former plan to keep them as a
   comment-only table is superseded.

---

## Execution checklist for the drafting agent

1. Decisions §6 Q1–Q7, Q9 are **resolved**; Q8 is a downstream owner assignment only
   (not draft-blocking). Nothing remains blocking — proceed.
2. Write `src/opendisplay_structs.h` per §4's section layout, §1's inventory,
   §3's reconciled layouts, using the §4 template blocks; every struct packed +
   `OD_STATIC_ASSERT`ed; every constant's value cross-checked against the §
   "Sources of truth" table (yaml for names/enums, Firmware superset for layout).
   Honor the §4 authoring principle: config payload fields documented sequentially,
   human-readable, with detailed prose in each field's comment.
3. Gate: `cc -std=c99 -fsyntax-only src/opendisplay_structs.h` and
   `c++ -fsyntax-only src/opendisplay_structs.h` both clean.
4. Do NOT touch `src/opendisplay_protocol.h` except (separately, no version bump —
   doc-only): (a) a SECTION-1 cross-reference "packet-type bytes and payload structs
   now live canonically in opendisplay_structs.h"; (b) per §6 Q7's one-home rule,
   repoint each `@request` block that has a matching struct to "see struct X" instead
   of re-listing byte offsets.
5. Do NOT vendor into firmware repos yet — that is shared-types-plan phase 2
   (extend `tools/sync_protocol_header.py` first, and write per-repo migration
   briefs in the style of `docs/rollout-plan.md`, including the "relocate
   GlobalConfig/EncryptionSession first" step for Silabs/NRF54).
