# Buzzer Melody Notation — Design

Design for a compact, YAML-authorable notation that lets a Home Assistant user
build a **list of musical notes** for the OpenDisplay buzzer, more concisely than
today's single-tone `activate_buzzer` service allows.

Companion documents:
- `BUZZER_MUSIC_PROTOCOL_REFERENCE.md` — the `0x0077` wire protocol and
  quarter-tone frequency model this notation compiles down to.
- `buzzer-hz-index-fix-plan.md` — the (not-yet-landed) `hz_to_index()`
  quarter-tone fix. This notation is independent of that fix (notes and raw
  indices bypass Hz entirely) but should ship in the same py-opendisplay release
  to avoid a second manifest pin-bump.

Status: **design / not yet implemented.** This consolidates a design-exploration
pass plus follow-up decisions on duration modes, tempo, triplets, and the
RTTTL question.

---

## 1. Goals & requirements

A YAML author should be able to write a melody as compactly as possible, with a
single field, supporting **all** of:

- **Raw `index / duration` pairs** — direct firmware `freq_idx` (0–255) so any
  reachable pitch (including quarter-tone microtones and the rest sentinel) is
  addressable.
- **`note-string / duration` pairs** — human note names like `A4`, `C#5`.
- **Two duration styles**, mixable per note:
  - **absolute milliseconds** (`:200`), and
  - **tempo-relative note fractions** (`/4` = quarter note), including
    **dotted** (`/4.`) and **triplet** (`/8t`) durations.

Non-goals: no new firmware opcode, no wire-format change, no host-side octave
folding (the firmware folds out-of-window indices itself to protect the
speaker), no multi-pattern authoring in the compact form.

---

## 2. Key decisions (summary)

| Question | Decision |
|----------|----------|
| New firmware opcode? | **No** — the existing `0x0077` melody structure already carries arbitrary index/duration step sequences. The gap is purely host-side authoring convenience. |
| Service shape | **New sibling service `opendisplay.play_melody`**; `activate_buzzer` stays untouched (single-tone UX, zero automation breakage). |
| YAML shape | **One compact string field** (`text` selector), `TOKEN[duration]` separated by whitespace/commas — not a list of dicts or pairs. |
| Duration | Per-note, unambiguous marker: `:ms` (absolute) **or** `/frac[.|t]` (tempo-relative). Both may appear in one melody. |
| Tempo | A `tempo` (BPM) field; quarter-note ms = `60000 / bpm`. Optional `default_length` (RTTTL `d=`-style) sets the default fraction for unmarked notes; `default_note_ms` is the absolute fallback. |
| Note fractions | **Powers of two** (`1`=whole, `2`=half, `4`=quarter, `8`=eighth, `16`, `32`), dotted `.` (×1.5), triplet `t` (×2/3). |
| Parser location | **py-opendisplay** — `note_to_index()` + `BuzzerActivateConfig.melody()`, beside `hz_to_index()`/`single_tone()`. HA stays a thin pass-through. |
| Multi-pattern | **Not exposed** in compact form; a rest token reproduces the inter-pattern gap. `\|` reserved for a future extension. |
| Format lineage | **Own grammar, RTTTL-*inspired*, not RTTTL-literal.** Optional RTTTL *importer* later as a secondary input dialect. |

---

## 3. Why no new opcode (wire level)

The `0x0077` payload is already `[instance][outer_repeat][pattern_count]` then
`pattern_count` patterns of `[nsteps]([freq_idx][dur_unit])*`
(reference §3.2). Every capability the notation needs — arbitrary note
sequences, rests (`freq_idx 0`), per-step durations, looping — is representable
today; the reference doc's worked melodies (§7.2–7.4) are plain `0x0077` frames.
`build_buzzer_activate_command()` and `device.activate_buzzer()` are already
agnostic to how the `BuzzerActivateConfig` was built.

**Practical length limit:** the payload caps at 256 bytes → `(256-3-1)/2 = 126`
steps, but the BLE-MTU ceiling (≤244 B) is tighter at `(244-4)/2 = 120` steps,
and the firmware's independent 30 s playback cap is reached at ~120 notes of
typical length anyway. So the byte cap and time cap converge; **the builder
validates ≤120 steps** with a clear error. Melodies longer than that are out of
scope by firmware design, not a limitation to engineer around here.

---

## 4. Service shape — new sibling `play_melody`

Options weighed:

| Option | Verdict |
|--------|---------|
| Overload `activate_buzzer` with an optional `notes` field | ✗ `frequency_hz`/`duration_ms` have defaults so they're *always* in `call.data`; the UI form shows contradictory fields; `vol.Exclusive` doesn't render as mutual-exclusion in the HA UI; docs become an if/else essay. |
| **New sibling service `play_melody`** | ✓ Each service single-purpose with a clean form; zero risk to existing automations; simple docs ("beep" vs "tune"); mirrors how `drawcustom` coexists with `upload_image`. |
| Replace `activate_buzzer` | ✗ Breaks every existing automation. |
| New firmware opcode | ✗ Nothing to gain (§3). |

**Service `opendisplay.play_melody`** ("Play buzzer melody"). Fields:

- `device_id` (required)
- `instance` (0–3, default 0)
- `notes` (required string, `text` selector)
- `tempo` (BPM, e.g. 40–400, default 120) — only affects `/frac` durations
- `repeats` (1–255, default 1) → wire `outer_repeat`
- `default_note_ms` (5–1275, default 200) — absolute duration for unmarked tokens
- `default_length` (fraction `1/2/4/8/16/32`, optional/unset) — RTTTL `d=`-style
  default: if set, unmarked tokens use this note-fraction at `tempo` instead of
  `default_note_ms` (e.g. `default_length: 4` → unmarked notes are quarters)

The handler mirrors `_async_activate_buzzer` verbatim (same `_get_entry_for_device`
/ no-buzzers guard / `_raise_if_sleeping` / `_async_connect_and_run` plumbing);
only the config construction differs (`BuzzerActivateConfig.melody(...)` instead
of `single_tone(...)`).

---

## 5. Notation

### 5.1 Why a compact string (not lists/dicts)

| Candidate | Verdict |
|-----------|---------|
| List of 2-elem lists `[["A4",200],[0,50]]` | Needs the `object` selector (raw YAML sub-editor); noisy; bracket/quote typos. |
| List of dicts `[{note:"A4",ms:200}]` | The *least* compact option — contradicts the goal; several dict shapes to document. |
| **Compact string `"A4:200 R:50 A5:200"`** | ✓ Most compact; renders as a plain `text` selector; one line per melody; familiar from ESPHome RTTTL; easy to build via templates and store in `input_text` helpers. |

A melody step has exactly two scalar facets (pitch, duration), which a
`TOKEN[duration]` mini-language expresses better than structured YAML. The
`drawcustom` list-of-dicts precedent fits *heterogeneous* elements with many
fields each — not this.

### 5.2 Grammar

```
melody     := token (separator token)*
separator  := whitespace and/or ","          ; commas treated as whitespace
token      := item [duration]
item       := INDEX | REST | NOTE
INDEX      := integer 0..255                  ; raw firmware freq_idx; 0 = rest
REST       := "R" | "REST"                    ; == index 0
NOTE       := letter accidental? octave quarter?
  letter     := A..G                          ; case-insensitive
  accidental := "#" | "s"  (sharp) | "b" (flat, enharmonic)
  octave     := -1 .. 10
  quarter    := "+" | "p"                      ; +1 quarter-tone (odd index)
duration   := abs_ms | rel_frac               ; optional; omitted => default
  abs_ms     := ":" integer 1..1275           ; absolute milliseconds
  rel_frac   := "/" fraction modifier?
    fraction := 1 | 2 | 4 | 8 | 16 | 32       ; 1=whole, 2=half, 4=quarter, ...
    modifier := "." (dotted, x1.5) | "t" (triplet, x2/3)
```

Notes on the grammar:

- **Case-insensitive** throughout (`a4`, `A4`, `r`, `rest`). Canonical/documented
  form uses uppercase letter, `#`, `+`. But `s`/`p` are accepted aliases, so
  **every firmware enum name minus its `n` prefix parses directly**
  (`note_to_index("As4p") == 123`), keeping firmware docs and YAML mutually
  legible without forcing C-enum spelling on authors.
- **Index formula:** `idx = 6 + 24*octave + 2*semitone + quarter`, semitones from
  C `{C0, D2, E4, F5, G7, A9, B11}` ± accidental. Computed `idx` outside `1..255`
  → error. (`A-1` computes to 0, colliding with the rest sentinel — rejected;
  no reason to author a 13.75 Hz note.)
- **No host-side folding.** The firmware folds any index outside `[117, 234]`
  into range (preserving pitch class) to protect the speaker; the host just
  emits a valid 0–255 index.
- **Raw indices and note names mix freely** in one melody (`"A4/4 144/4 212:80"`).

### 5.3 Duration resolution

A note's duration is chosen per token:

- **Omitted** → the melody default: **`default_length`** at the current `tempo`
  if that field is set (e.g. `default_length: 4` → a quarter note), otherwise
  **`default_note_ms`** (absolute). Each default field is unit-named, so there is
  no bare-number ambiguity about whether "4" means 4 ms or a quarter note.
- **`:ms`** → that many milliseconds.
- **`/frac`** → `whole_note_ms / frac`, where `whole_note_ms = 4 * 60000 / bpm`
  (so `/4` = one quarter = `60000/bpm` ms). `.` multiplies ×1.5; `t` multiplies
  ×2/3.

An explicit per-note marker (`:ms` or `/frac`) always overrides both defaults.
`default_length` accepts a plain fraction only; per-note dotted/triplet modifiers
apply where written, not to the default.

The resulting ms is converted to the firmware's **5 ms units** (`ms_to_units`,
round-to-nearest, min 1 unit) and must fit `1..255` units (≤1275 ms). Durations
beyond 1275 ms are an **error, not a silent clamp** — silently truncating a held
note is a confusing authoring surprise.

**Triplets** (`t`, ×2/3) fill correctly without any grouping syntax: three
`/8t` notes total `3 * (1/8 * 2/3) = 1/4` — exactly one quarter. Per-note factor,
flat token stream, no brackets. `.` and `t` are mutually exclusive on one note.

> **5 ms rounding.** Tempo-relative durations quantize to 5 ms units, so triplets
> don't divide evenly: at 120 BPM a quarter is 500 ms → triplet-eighth = 166.7 ms
> → 165 ms; three of them = 495 ms vs. the 500 ms they nominally fill (5 ms
> drift). Inaudible on a piezo buzzer; not worth compensating for. Document it,
> nothing more.

### 5.4 Malformed input

Tokens are validated at **HA schema-validation time**, before any BLE traffic:
the `notes` schema runs `vol.All(cv.string, _valid_melody)`, where
`_valid_melody` invokes the py-opendisplay parser and converts its `ValueError`
into `vol.Invalid` carrying the offending token's position and text
(e.g. `token 3 'H4:100': unknown note letter 'H'`).

---

## 6. Format lineage — own grammar, not RTTTL

RTTTL (the ESPHome-precedented ringtone format) was considered as the native
format and **rejected** because it structurally cannot express this device:

| Requirement | RTTTL |
|-------------|-------|
| Raw `index` tokens | ✗ note-names only |
| Absolute-ms durations | ✗ tempo-relative only |
| **Quarter-tones** (the buzzer's headline feature) | ✗ 12-TET only — cannot address odd indices |
| Triplets | ✗ no native support |
| Note names, rests, dotted, tempo header | ✓ |

The first three are hard structural gaps, not missing conveniences — adopting
RTTTL literally would make the *compact* path less capable than the raw
`activate_buzzer` path already is. **Extending** RTTTL (adding quarter-tone / raw
tokens) is the worst option: it breaks round-tripping with real RTTTL tools, so
it forfeits the only benefit (pasteability) while keeping the name.

**Decision:** the native format is this own grammar — RTTTL-*inspired* (it
borrows RTTTL's proven ideas: a defaults/tempo header concept via the `tempo` +
`default_note_ms` fields, powers-of-two durations, dotted notes) but a proper
superset. RTTTL's one real advantage — a large library of pasteable tunes — is
recovered **separately and optionally** by a future **RTTTL importer**
(auto-detected by its `…:d=…,o=…,b=…:` header, or a distinct field/helper) that
compiles a standard ringtone string into the same `BuzzerActivateConfig`. The
own grammar stays the source of truth; RTTTL becomes one input dialect that feeds
it. Never make extended-RTTTL the primary format.

---

## 7. Where the code lives — py-opendisplay

Parsing and building go in `models/buzzer_activate.py`, beside the existing
`hz_to_index`/`ms_to_units`/`single_tone`:

```python
def note_to_index(name: str) -> int:
    """'A4'->120, 'C#5'/'Cs5'->128, 'A4+'/'A4p'->121, 'R'->0. Raises ValueError."""

@classmethod
def melody(
    cls,
    notes: str | Sequence[tuple[int | str, int]],
    *,
    tempo: int = 120,
    repeats: int = 1,
    default_ms: int = 200,
) -> BuzzerActivateConfig:
    """Parse a compact melody into a single-pattern config. Raises ValueError."""
```

Rationale (vs. parsing in the HA integration):

- Every py-opendisplay client (CLI, tests, future integrations) gets melodies,
  not just HA. Today `single_tone` is the *only* convenience builder — any melody
  currently requires hand-assembling `BuzzerStep` tuples.
- The grammar gets isolated, hardware-free unit tests in the library, versioned
  with the wire model it feeds (mirrors keeping all pitch math in the library).
- The HA handler stays a two-line pass-through; it re-raises parser `ValueError`
  as `vol.Invalid` at schema time (it already imports py-opendisplay at module
  level, so calling the parser inside a validator is free).
- Also accept a `Sequence[tuple[int|str, int]]` so programmatic callers needn't
  build strings; the HA `notes` field accepts the string form only, keeping the
  schema to one shape.
- Export `note_to_index` from `models/__init__.py` and the top-level
  `__init__.py`, matching the existing `BuzzerActivateConfig`/`BuzzerPattern`/
  `BuzzerStep` exports.

---

## 8. Multi-pattern & repeats — flatten to one pattern

The compact form emits **exactly one pattern**; `repeats` maps to the wire's
`outer_repeat`. Multi-pattern is *not* exposed because:

- The only audible effect of a pattern boundary is a fixed 20 ms silence —
  reproducible exactly with an explicit rest token (`R:20`); during both a gap
  and a rest the drive pin is silent and the enable pin stays asserted.
- Outer repeats insert no gap between repetitions either way, so pattern grouping
  buys the author nothing the string can't express.
- Advanced callers wanting real pattern structure can still hand-build
  `BuzzerPattern` tuples via the unchanged dataclasses.

Scope boundary: **1 pattern, ≤120 steps, 1–1275 ms/step, repeats 1–255**; `|`
reserved for a future multi-pattern extension (parser rejects it today with a
clear "multi-pattern not supported in compact form" message, keeping the
character free).

---

## 9. Worked examples

**Mixed note-name / rest / raw index, absolute ms:**

```yaml
service: opendisplay.play_melody
data:
  device_id: abc123
  notes: "A4:200 R:50 144:200"     # note name, rest, raw index
```
`A4`→idx 120/40u, `R`→0/10u, `144`→idx 144/40u →
payload `01 01 03 78 28 00 0A 90 28`; with the `00 77 00` prefix →
frame **`0077000101037828000A9028`** — bit-identical to reference §7.2 (bench T1,
A4·rest·A5).

**Tempo-relative, "Twinkle" opening** (explicit fractions):

```yaml
data:
  tempo: 120
  notes: "C5/4 C5/4 G5/4 G5/4 A5/4 A5/4 G5/2"
```
At 120 BPM a quarter = 500 ms = 100 units (`0x64`), a half = 1000 ms = 200 units
(`0xC8`). C5=126, G5=140, A5=144 →
payload `01 01 07 7E64 7E64 8C64 8C64 9064 9064 8CC8`.

**Same tune, terser via `default_length`** — unmarked notes default to quarters,
only the final half-note carries a marker:

```yaml
data:
  tempo: 120
  default_length: 4        # unmarked notes are quarters
  notes: "C5 C5 G5 G5 A5 A5 G5/2"
```
Compiles to the identical payload as above.

**Triplet figure:**

```yaml
data:
  tempo: 120
  notes: "C5/8t D5/8t E5/8t"       # three eighths in the time of one quarter
```
Eighth = 250 ms; triplet-eighth = 166.7 ms → 165 ms → 33 units (`0x21`). Three
notes ≈ 495 ms ≈ one quarter (the 5 ms rounding note, §5.3).

---

## 10. Backward compatibility

- `activate_buzzer` (schema, handler, `services.yaml`, translations) is untouched;
  existing automations keep working. Its docs gain one line: "For multi-note
  melodies, see Play buzzer melody."
- py-opendisplay changes are purely additive (new function + classmethod +
  exports); no signature changes → non-breaking. Ship as `feat:` → release-please
  minor release → then bump the `manifest.json` pin
  `py-opendisplay[silabs-ota]==…` (same deferred-pin-bump discipline as the hz
  fix plan).
- New service needs `services.yaml` (follow `drawcustom`'s inline
  `name`/`description`/`example` pattern), `strings.json`, and
  `translations/en.json` entries in lockstep.

---

## 11. Open questions / follow-ups

- **RTTTL importer.** Optional, secondary; auto-detect by header or a separate
  field/helper. Not part of the first implementation.
- **General tuplets** (quintuplets, etc.) via an explicit `/8:3:2`
  (N-in-the-time-of-M) ratio. Not needed now; `t` triplet covers ~99% of use.

---

## 12. Critical files (for the eventual implementation)

- `py-opendisplay/src/opendisplay/models/buzzer_activate.py` — `note_to_index()`
  + `melody()` builder + duration/tempo/triplet parsing.
- `py-opendisplay/tests/unit/test_models_buzzer_activate.py` — grammar,
  duration-mode, triplet, and byte-layout tests.
- `Home_Assistant_Integration/custom_components/opendisplay/services.py` — new
  `SCHEMA_PLAY_MELODY`, `_async_play_melody` handler, registration.
- `Home_Assistant_Integration/custom_components/opendisplay/services.yaml` — new
  service definition, `text` selector, examples/tooltips (`drawcustom` pattern).
- `Home_Assistant_Integration/custom_components/opendisplay/strings.json` and
  `translations/en.json` — service + field text, in lockstep.
- Reference: `opendisplay-protocol/docs/BUZZER_MUSIC_PROTOCOL_REFERENCE.md`
  (§3 wire, §4 note model, §7 examples).
