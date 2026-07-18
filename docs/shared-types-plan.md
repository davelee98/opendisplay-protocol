# Plan: canonical shared enums + structs via codegen from annotated C

Status: **proposal** (not yet implemented). Captures the design for extending this
repo from the wire-*message* contract (`src/opendisplay_protocol.h`, opcodes/
responses/errors) to the wire-*payload* contract (the packed config structs and
their enums), shared across **all firmware variants, py-opendisplay, the OD App,
and opendisplay.org**.

The existing `docs/implementation-plan.md` / `docs/rollout-plan.md` cover the v2.0
opcode header and its rollout. This doc is the next layer: one canonical source for
the **structs and enums** those opcodes carry.

---

## 1. Why — the problem

The same data model is hand-maintained in **five** places, in four languages, and is
**already visibly drifting**:

| Surface | "Canonical" today | Hand-duplicated in |
|---|---|---|
| Opcodes / responses / errors / framing | `src/opendisplay_protocol.h` (this repo) — but macro-only C, so only C can `#include` it | Swift `OD.Cmd` (still `nfc = 0x0082`, **stale** — v2.0 moved NFC to 0x0083), JS `ble-common.js` (scattered inline hex, no enum), Python `CommandCode` |
| Packed config structs (15) | `opendisplay.org` `httpdocs/firmware/toolbox/config.yaml` `ble_proto.packet_types` — a real byte-level schema; drives the website toolbox and the OD App (via JSCore) at runtime | C `structs.h` ×4 firmware repos (**6 silently diverged**, see §7), Python dataclasses + manual `struct.pack` (sizes already drifted and were reactively corrected: `POWER 30 # was 32`, `DISPLAY 46 # was 66`) |
| Enums / bitfields | `config.yaml` (`enum` / `bits` / `conditional_enum`) | C `#define` groups ×4 (drift: `TRANSMISSION_MODE` bit0, `CONFIG_PKT 0x29` naming), Python `IntEnum` (behind: `ICType` stops at 6, missing 7/8), Swift `ColorScheme`, **and `epaper_dithering` (a third home for the color enum)** |
| MSD advertisement framing | — (nowhere) | Swift `AdvertisementData.swift` + JS both hand-parse the fixed 16-byte layout |

Evidence the hand-mirroring fails in practice:
- **6 packed structs have silently diverged** across firmware repos — same total byte
  size, different carve-up of `reserved[]`, so they compile and mis-parse on the wire
  (§7 lists the exact offsets).
- Python struct sizes drifted from firmware and were corrected reactively (annotations
  literally read `# Fixed: was 32`).
- `config.yaml` is at `minor_version: 4` on the website but `minor_version: 3` in the
  app's bundled copy; Python `ICType` is behind both.
- The Swift opcode for NFC is still `0x0082`, two protocol versions stale.

`py-opendisplay` is the single Python source (the Home Assistant integration consumes
it, holding no copy of its own) — so the duplication to kill is **cross-language**, not
Python-vs-Python.

Root cause: the ecosystem already has the right idea **twice** — a macro C header here
and a YAML schema on the website — each covering half the surface, in a format the other
languages can't natively consume, so each leaks hand-maintained copies.

## 2. Decision

**The canonical source is annotated C, in this repo.** Codegen reads it and emits the
other four languages plus a regenerated `config.yaml`. Chosen because:

- Firmware `#include`s it directly — zero build step, identical ergonomics to the
  existing vendored `opendisplay_protocol.h`.
- **The C compiles, so the layout is validated by the same compiler that builds the
  firmware.** Codegen reads that same C via a real compiler front-end, so generated
  bindings can never disagree with the firmware's actual bytes.
- Engineers already read/write these structs in C; the model matches the repo's proven
  "one canonical header, vendored byte-for-byte, `--check` drift gate" pattern.

The one thing C's type system cannot express — enum-binding of raw ints, bitfield
semantics, per-field endianness, units, defaults, docs — is carried in **structured
comments**, extending the `@tag` convention this repo already uses for opcodes.

## 3. Core idea — C type is the layout oracle, the comment is the semantics

A packed C struct unambiguously encodes everything about *bytes*: field name, width,
array length, order, and (with `packed`) exact offsets. It cannot encode the *meaning*
layer. So:

- **C declaration → layout truth**, read by libclang (the compiler computes packed
  offsets and sizes for you).
- **Structured trailing comment → semantic truth**, parsed for `@tags`.

Both merge into one neutral in-memory IR; emitters fan out from the IR. Crucially,
**everything downstream of the IR is identical regardless of source format** — choosing
C-canonical only changes the front end (parse annotated C instead of authoring YAML).
The IR, emitters, drift gate, and cross-language self-test are unchanged. This is a
front-end choice, not a redesign.

The codegen can also *emit* `config.yaml`, so the website + app runtime keep working —
generated from C now instead of hand-authored.

## 4. The annotated header format

Use Doxygen **trailing-member** comments (`/**< … */`); libclang attaches those to the
field cursor automatically. The C compiler ignores them, so the header still compiles
clean and firmware is unaffected.

```c
/** @struct DisplayConfig  @packet 0x20  @doc "Per-display panel configuration" */
struct DisplayConfig {
    uint8_t  instance_number;      /**< @doc "0-based display index" */
    uint8_t  display_technology;   /**< @enum DisplayTechnology */
    uint16_t panel_ic_type;        /**< @enum PanelIC @endian le */
    uint16_t pixel_width;          /**< @unit px @endian le */
    uint16_t pixel_height;         /**< @unit px @endian le */
    /* … */
    uint8_t  color_scheme;         /**< @enum ColorScheme */
    uint8_t  transmission_modes;   /**< @bits TransmissionMode */
    uint8_t  reserved_pins[7];     /**< @reserved */
    uint16_t full_update_mC;       /**< @unit mC @endian le @since 2.1 */
    uint8_t  reserved[13];         /**< @reserved */
} __attribute__((packed));
_Static_assert(sizeof(struct DisplayConfig) == 46, "DisplayConfig wire size");

/** @enum ColorScheme @width 1 */
enum ColorScheme {
    COLOR_SCHEME_MONO = 0,  /**< @doc "1bpp black/white" */
    COLOR_SCHEME_BWR  = 1,  /**< @doc "black/white/red" */
    /* … */
};
```

The `_Static_assert` makes the **firmware's own compile** assert every wire size,
independently of codegen — a third check alongside "C compiles" and "codegen agrees".

### Tag vocabulary (the columns C can't express — i.e. what `config.yaml` already carries)

| Tag | On | Purpose |
|---|---|---|
| `@packet 0xNN` | struct | config-packet-type (TLV tag) |
| `@enum Name` | field | bind a raw int field to a named enum → typed codegen |
| `@bits Name` | field | field is a bitfield; bit names defined once on the enum/group |
| `@endian le\|be` | field / struct | **per-field byte order** — handles the 0x76-BE vs 0x80-LE geometry split |
| `@reserved` | field | padding; do not surface downstream |
| `@unit`, `@scale`, `@min`, `@max`, `@default` | field | UI / validation hints the website toolbox needs |
| `@since x.y` | field / struct | added-in protocol version |
| `@enum_when F {v: EnumA, …}` | field | conditional enum: meaning depends on another field's value (§8) |
| `@doc "..."` | anything | human + agent description |

## 5. Codegen pipeline

libclang does the heavy lifting — no offset or size is ever hand-maintained again:

1. `Index.parse("src/opendisplay_types.h", args=["-Xclang", "-fparse-all-comments", "-std=c11"])`
   — `-fparse-all-comments` makes clang attach non-Doxygen comments too.
2. Walk the AST. For each packed `STRUCT_DECL`, per field capture: `spelling`,
   `type.spelling`, `type.get_array_size()`, offset via `record_type.get_offset(name)`
   (returns **bits**; ÷8), `type.get_size()`, and `field.raw_comment` → parse `@tags`.
3. For each `ENUM_DECL`: constants, `enum_value`, each constant's `raw_comment`.
4. Build a neutral **IR** (structs → fields; enums → members). Same IR a YAML source
   would have produced.
5. Emit each target from the IR via small template functions. **Every emitted target
   must be human-readable, documented output — not a values-only dump.** The IR
   carries the canonical header's `@doc` prose + enum/value descriptions, and each
   emitter re-renders it as idiomatic doc comments in that language (Swift `///`,
   Python docstrings/field comments, JSDoc `/** */`), producing a documented module
   that reads as clearly as `opendisplay_structs.h` does in C. The generated struct/enum
   (or the language's natural equivalent — dataclass, `OptionSet`, `IntEnum`, const
   object) must be self-explaining on its own. (See `docs/opendisplay-structs-draft-plan.md`
   §4 "Authoring principle".)
   - **C**: (optional) a checked re-emit / the `_Static_assert`s; the header itself is
     the source, vendored as-is.
   - **Python** (`py-opendisplay`): `CommandCode` + every `IntEnum`/`IntFlag` + per-struct
     field tables (offset, width, endian, enum-binding) driving generic pack/unpack —
     replacing the hand `struct.pack` format strings and the duplicated `PACKET_TYPE_*`.
   - **Swift** (OD App): opcode enum, config enums, `OptionSet` bitfields, MSD offsets
     (kills the stale `0x0082`). The struct codec stays JS-runtime-driven.
   - **JS**: opcode + enum constants for `ble-common.js` (replacing scattered inline hex).
   - **`config.yaml`**: regenerated so website + app keep their runtime codec, now sourced
     from C.
6. `--check` mode: emit to temp, diff against committed outputs, exit 1 on drift — reuse
   the `tools/sync_protocol_header.py` pattern exactly.

## 6. Parser choice

| Option | Verdict |
|---|---|
| **libclang** (`clang.cindex`) | **Recommended.** Real compiler → exact packed offsets/sizes (`get_offset`/`get_size`), native enum values, automatic comment attachment. Cost: a dev-time libclang dependency — this breaks the repo's strict stdlib-only rule for *tooling*, but codegen is a build tool, not shipped, so it is acceptable. |
| pycparser | Rejected: **discards comments entirely** (kills the approach), computes no offsets, and chokes on `__attribute__((packed))` without fake headers. |
| Restricted regex / subset parser | Stdlib-pure fallback (~300 lines; these headers are a very regular subset). But it re-derives offsets by hand — the exact thing that already drifted — so it must be backstopped by the `_Static_assert`s and a size cross-check. |

Recommendation: libclang for extraction **plus** `_Static_assert(sizeof == N)` per struct
in the header so the firmware compile is an independent layout check.

## 7. One-time reconciliation before locking (the real work)

Codegen is the easy part; generation *freezes* a single layout, so the divergences must
be resolved first. The canonical layout for each struct is the **superset**: every field
any repo promoted out of `reserved[]` gets a name; `reserved[]` shrinks to match. Total
sizes already agree, so this is renaming reserved bytes, not resizing.

**The 6 divergent packed structs** (same total size, different carve-up — exact offsets
from the firmware survey):

- **`SystemConfig` (22B)** — `Firmware` names offsets 20–21 as `pwr_pin_2` / `pwr_pin_3`;
  NRF/N54/SIL treat them as `reserved[17]`.
- **`ManufacturerData` (22B)** — N54/SIL define `simple_config_driver_index` (4),
  `_display_index` (6), `_power_index` (8), `_configured_at[6]` (10–15); FW/NRF see
  `reserved[18]`.
- **`PowerOption` (30B)** — three tail interpretations at offsets 20–29: `charge_*` pins
  (FW+N54) vs `reserved` (NRF+SIL); `min_wake_time_seconds`/`screen_timeout_seconds`
  (FW only) vs `reserved` (N54).
- **`DisplayConfig` (46B)** — FW reads `full_update_mC` (u16) at offsets 31–32; others
  see `reserved[15]`.
- **`SensorData` (30B)** — FW/N54 define `i2c_addr_7bit` (4), `msd_data_start_byte` (5);
  NRF/SIL see `reserved[26]`.
- **`BinaryInputs` (30B)** — FW defines `power_off_flags` (16), `power_off_hold_sec` (17);
  others see `reserved[14]`.

Already byte-identical everywhere (canonicalize as-is): `LedConfig`, `DataBus`,
`SecurityConfig`, `TouchController`, `BuzzerConfig` (canonical name; firmware still
`PassiveBuzzerConfig` until migrated), `FlashConfig`, `DataExtended`,
`WifiConfig`, `NfcConfig`.

**Enum / constant naming drift to settle:**
- `TRANSMISSION_MODE` bit0 — `ZIPXL` (FW/SIL) vs `STREAMING_DECOMPRESSION` (N54): same
  bit, conflicting name. Pick one.
- `CONFIG_PKT 0x29` — `PASSIVE_BUZZER` (N54) vs `BUZZER` (SIL).
- `ICType` — add `NRF54L15 = 7`, `NRF54LM20 = 8` (present on website, missing in Python).
- **Color / dither** — decide whether the canonical color enum lives here or is a
  delegated import from `epaper_dithering` (a legitimate third owner). This is a real
  ownership question, not just a copy.

**Sharp edges the tags must model** (all real, from the surveys):
- **Per-field endianness**: opcode headers are BE, struct bodies LE, and the same region
  geometry is packed **BE under opcode 0x76 but LE under 0x80**. → `@endian`.
- **Direction-scoped opcode value reuse**: `0x0073` = `LED_ACTIVATE` (host→device) *and*
  `DIRECT_WRITE_REFRESH_COMPLETE` (device→host). A naive unique-value enum won't
  round-trip — key on direction (the existing `@dir` tag already does this).
- **Bitfields stored as raw `uint8`** (`transmission_modes`, `communication_modes`,
  `security_flags`) → `@bits` so they become typed `OptionSet`/`IntFlag`/bit-`#define`s.

## 8. Two things genuinely awkward in C

1. **`conditional_enum`** — `manufacturer_data.board_type` means different things per
   `manufacturer_id`. C has no native form; encode it in the comment via
   `@enum_when manufacturer_id {0: DIYBoard, 1: SeeedBoard, …}` and teach the parser this
   one tag. This is the one place a comment does structural, not just annotative, work.
2. **Opcodes are `#define` macros**, and macros are not in the clang AST. Two options:
   (a) parse them with `TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD` (surfaces
   `MACRO_DEFINITION` cursors + tokens); or (b) keep the existing macro-only
   `opendisplay_protocol.h` untouched for the wire-*message* layer, and put the new
   codegen-canonical **enums + structs** in a separate `opendisplay_types.h` that uses
   real `enum`s (trivially codegen-able). Recommended: **(b)** — leave
   `opendisplay_protocol.h` as-is; add `opendisplay_types.h`.

## 9. Resulting repo structure

```
opendisplay-protocol/
  src/opendisplay_protocol.h    # UNCHANGED — opcodes/responses/errors (macro-only, wire messages)
  src/opendisplay_types.h       # NEW canonical — annotated packed structs + real enums (config payload)
  gen/codegen.py                # libclang -> IR -> {Python, Swift, JS, config.yaml}; --check drift mode
  tools/sync_protocol_header.py # extend to vendor + drift-check opendisplay_types.h and all generated outputs
```

`opendisplay_types.h` is vendored into firmware byte-for-byte (firmware just `#include`s
it, as today). Vendored destinations mirror `opendisplay_protocol.h`'s (siblings under
`~/Documents/OD/`); firmware repos delete their local `structs.h` / `opendisplay_structs.h`
struct + enum duplicates in favor of the shared header (keeping strictly repo-specific
values — GPIO pins, buffer sizes, in-memory-only structs like `GlobalConfig`/`ImageData`/
`PipeReorderSlot`, and the `#ifdef PIPE_SMALL_DRAM_WINDOW` tuning knobs — which must NOT
be canonicalized).

## 10. Verification

- **Drift gate**: CI regenerates all outputs, diffs against committed, fails on mismatch
  (identical to `sync_protocol_header.py --check`).
- **Firmware compile**: `_Static_assert(sizeof(...)==N)` per struct + the existing
  `cc -std=c99 -fsyntax-only` / `c++ -fsyntax-only` passes.
- **Cross-language offset self-test**: each language emits every struct's size + field
  offsets; assert equal across C / Swift / Python / JS. This directly catches the
  "compiles but mis-parses" class that already occurred.

## 11. Phased rollout (each phase independently shippable)

0. **Reconcile** the 6 divergent structs + the naming drifts to one agreed layout each
   (§7). This is the hard human work; do it once. Land `src/opendisplay_types.h`
   annotated, with `_Static_assert`s.
1. **`gen/codegen.py`** with the libclang front end + IR + a first emitter. Prove the
   `get_offset` oracle and comment parsing end-to-end on one real struct
   (`DisplayConfig`) before generalizing.
2. **Firmware C** first consumer (lowest risk — you already have the vendoring + drift
   machinery): vendor `opendisplay_types.h`, delete local struct/enum duplicates per repo
   (branch + PR each, like `rollout-plan.md`).
3. **Python** (`py-opendisplay`): generate `CommandCode` + enums + struct tables; retire
   the hand dataclasses/format strings and the duplicated `PACKET_TYPE_*`; the offset
   self-test guards it.
4. **Swift** enums + MSD offsets (`ODConstants.swift`, `AdvertisementData.swift`); config
   struct codec stays JS-runtime-driven but now sourced from generated `config.yaml`.
5. **JS**: generate opcode/enum constants for `ble-common.js`; fold `config.yaml`
   generation so website + app share one generated source.
6. Wire `--check` drift gates into every repo's CI (same pattern + precedent as
   `rollout-plan.md` Task E).

## 12. Open decisions (need a human call before generation lands)

- **libclang dependency** for `codegen.py` — accept the dev-time dep (recommended), or
  build the stdlib-only subset parser + `_Static_assert` backstop instead.
- **Color/dither enum ownership** — canonical here vs delegated import from
  `epaper_dithering`.
- Resolutions for the naming drifts in §7 (`TRANSMISSION_MODE` bit0, `CONFIG_PKT 0x29`).
- Whether opcodes eventually move into the codegen (via detailed preprocessing record) or
  stay hand-authored macros in `opendisplay_protocol.h` (recommended: stay).
