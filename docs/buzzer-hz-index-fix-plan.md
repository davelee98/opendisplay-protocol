# Fix `hz_to_index()` to match the firmware's quarter-tone buzzer scale

## Context

Firmware PR #98 (`0x0077` musical buzzer) replaced the old buzzer's linear pitch
model with a **quarter-tone equal-temperament** scale: the wire byte `freq_idx`
(1–255) selects `Freq(idx) = 13.75 · 2^(idx/24)` Hz, with `idx = 0` meaning
rest/silence. This is fully documented in
`opendisplay-protocol/docs/BUZZER_MUSIC_PROTOCOL_REFERENCE.md` (§4 frequency
model, §7.7 host-side call, §11 item 1 — this exact bug).

The host-side inverse in `py-opendisplay`'s `hz_to_index()`
(`src/opendisplay/models/buzzer_activate.py:12-17`) still implements the
**pre-PR-98 linear** map (`idx = round(1 + (hz-400)*254/(12000-400))`). Every
pitch chosen by Hz — including the Home Assistant `activate_buzzer` service —
plays the wrong note. Concretely: `hz_to_index(440)` today returns 2, which the
firmware folds to A♯4 ≈ 466 Hz, a semitone sharp of the requested A4.

Goal: fix the Hz→index math only. Keep the Hz-based input surface unchanged in
both py-opendisplay and the HA service (per explicit user requirement) — this is
a targeted correctness fix, not an API redesign. No firmware, wire-format, or
octave-folding changes (firmware already folds out-of-window indices; the host
doesn't need to replicate that).

**Verified fact:** `hz_to_index()` has exactly one call site
(`buzzer_activate.py:65`, inside `single_tone()`), is not re-exported from any
`__init__.py`, and the HA handler (`services.py:969-973`) passes `frequency_hz`
straight through to `single_tone()`. So the actual math fix lives entirely in
py-opendisplay; Home_Assistant_Integration only needs a wording touch-up plus a
deferred dependency-pin bump — not a code change to `services.py`/`services.yaml`
(confirmed no other Hz math exists there).

## The new formula

```
idx = clamp(round(24 · log2(hz / 13.75)), 1, 255)   for hz > 0
idx = 0                                              for hz <= 0  (silence/rest)
```

Independently verified against the reference doc's landmark table:
`hz_to_index(440) == 120` (exact, A4), `hz_to_index(880) == 144` (exact, A5,
octave delta = 24), `hz_to_index(6200) == 212`, `hz_to_index(400) == 117`,
`hz_to_index(12000) == 234`.

## Changes — py-opendisplay (the actual fix)

File: `src/opendisplay/models/buzzer_activate.py`

- Add `import math`.
- Remove `_MIN_HZ = 400` / `_MAX_HZ = 12000` (lines 7-8) — used only by the old
  linear formula, private, unreferenced elsewhere (verified by grep).
- Add `_ANCHOR_HZ = 13.75` and `_STEPS_PER_OCTAVE = 24`.
- Rewrite `hz_to_index()`:
  ```python
  def hz_to_index(hz: int) -> int:
      """Convert Hz to firmware quarter-tone index (0-255). 0/negative Hz -> 0 (silence).

      Inverse of the firmware scale Freq(idx) = 13.75 * 2**(idx/24) Hz.

      Examples:
          idx 1   -> ~14.15 Hz    (nAm1p, the bottom note in the table)
          idx 120 -> 440.00 Hz    (nA4, concert pitch A)
          idx 255 -> ~21714.33 Hz (nE10p, the top note in the table)

      Indices outside the firmware's playable window [117, 234] are octave-folded
      by the firmware itself (preserving pitch class) before being driven onto the
      speaker -- this protects the buzzer hardware from being driven outside its
      safe operating range. This helper only produces a valid 0-255 index; it does
      not need to replicate that folding.
      """
      if hz <= 0:
          return 0
      idx = round(_STEPS_PER_OCTAVE * math.log2(hz / _ANCHOR_HZ))
      return max(1, min(255, idx))
  ```
- Fix the stale `BuzzerStep.frequency_index` comment (line 29) — it currently
  says "0=silence, 1–255 → 400–12000 Hz" (describes the old linear map); update
  to describe the quarter-tone formula.

No changes needed to `_DURATION_UNIT_MS`/`ms_to_units()`, `protocol/commands.py`,
`device.py`, or any `__init__.py` re-exports (`hz_to_index` isn't part of the
public API surface).

### Test changes — `tests/unit/test_models_buzzer_activate.py`

`TestHzToIndex` currently asserts the old linear behavior and must be rewritten:

| Case | Old assertion | New assertion |
|---|---|---|
| `hz_to_index(0)` / `(-100)` | `== 0` | unchanged |
| `hz_to_index(400)` | `== 1` | `== 117` |
| `hz_to_index(12000)` | `== 255` | `== 234` |
| `hz_to_index(6200)` | `in 126..130` | `== 212` (rename test — no longer a "midpoint") |
| `hz_to_index(99999)` | `== 255` (clamp) | unchanged (clamp) |
| `hz_to_index(1)` | `== 1` | unchanged |
| `hz_to_index(399)` | `== 1` | `== 117` (399 is no longer "below min") |

Add landmark/round-trip coverage against the reference doc's §4.2 table for
regression protection: `hz_to_index(440) == 120`, `hz_to_index(880) == 144` +
`hz_to_index(880) - hz_to_index(440) == 24` (octave check), `hz_to_index(1000)
== 148`, `hz_to_index(2000) == 172`, `hz_to_index(523) == 126`,
`hz_to_index(11840) == 234`, `hz_to_index(21714) == 255`. Optionally a
parametrized round-trip: `hz_to_index(round(13.75 * 2**(idx/24))) == idx` for
idx in `{117, 120, 126, 144, 148, 172, 212, 234}`.

Optional end-to-end check: `single_tone(frequency_hz=440,
duration_ms=200).to_bytes() == bytes([1, 1, 1, 120, 40])`, matching the
reference doc's §7.1 worked example (`78 28`).

`test_protocol_commands.py` and `test_device_buzzer_activate.py` — **verified,
no changes needed**: their buzzer assertions compare against
`config.to_bytes()` relatively (never a literal `freq_idx` byte), so they're
unaffected by the formula change.

Other test classes in `test_models_buzzer_activate.py`
(`TestMsToUnits`, `TestBuzzerPatternToBytes`,
`TestBuzzerActivateConfigToBytes`, `TestBuzzerActivateConfigRoundtrip`,
including the `frequency_hz=0` silence case) are untouched.

## Changes — Home_Assistant_Integration

- `custom_components/opendisplay/services.py:207-214`
  (`SCHEMA_ACTIVATE_BUZZER`) — **no change**. The `frequency_hz`
  `vol.Range(min=0, max=12000)` bound is a reasonable UI sanity range
  independent of the mapping formula (it brackets the firmware's playable
  window, and any Hz value now maps to *some* valid index). The handler
  (`services.py:959-979`) already forwards `frequency_hz` unchanged.
- `custom_components/opendisplay/services.yaml:209-249` (`activate_buzzer`) —
  **add tooltip documentation**. Most services in this file (`upload_image`,
  `activate_led`) rely on `strings.json`/translations for field text and carry
  no inline `name`/`description`; `drawcustom` is the one exception, adding
  inline `name`/`description` per field. Follow that `drawcustom` pattern here:
  add a top-level `name`/`description` for `activate_buzzer` itself, and a
  `name`/`description` under each field (`instance`, `frequency_hz`,
  `duration_ms`, `repeats`) — this makes the note-mapping and octave-folding
  behavior visible directly in the YAML (useful when reading/editing the raw
  file or in YAML-mode service calls, where the translated `strings.json` text
  isn't shown). The `frequency_hz` description should mirror the corrected
  `strings.json` wording below: mention that Hz maps to the nearest
  quarter-tone note, give the effective range as landmarks (e.g. "~403 Hz to
  ~11840 Hz, expressed in Hz for convenience"), and note that out-of-range
  pitches are octave-folded by the firmware to protect the speaker hardware.
- `custom_components/opendisplay/strings.json` and
  `custom_components/opendisplay/translations/en.json` (identical
  `services.activate_buzzer.fields.frequency_hz.description` text in both —
  `en.json` is the only locale file, edit both in lockstep): replace
  `"Tone frequency in Hz (0 = silence, 400-12000)."` with wording that reflects
  the real behavior, e.g. `"Tone frequency in Hz (0 = silence). Played as the
  nearest quarter-tone note; frequencies outside the buzzer's playable range are
  shifted by octaves to fit."`
- No new HA tests — `tests/` has zero existing references to `activate_buzzer`/
  `frequency_hz` (verified), the handler is a pure pass-through, and the math is
  fully covered by py-opendisplay's unit tests.

## Deferred follow-up (not part of this change)

`custom_components/opendisplay/manifest.json:16` pins
`py-opendisplay[silabs-ota]==7.12.0`. Once the py-opendisplay fix merges and a
new version is released (repo uses conventional commits / release-please — a
`fix:`-prefixed commit will cut a patch release), bump this pin. Don't guess a
version number now; this is the step that actually delivers the fix to HA
users, so track it separately once the release exists.

## Order of operations

1. **py-opendisplay**: rewrite `hz_to_index()`, update constants/comments,
   rewrite `TestHzToIndex` + add landmark tests. Commit as a `fix:` so
   release-please cuts a patch release.
2. **Home_Assistant_Integration**: update `strings.json` +
   `translations/en.json` wording, and add matching `name`/`description`
   tooltips to `services.yaml` for `activate_buzzer` (independent of step 1,
   can happen in parallel).
3. **Follow-up, later**: bump `manifest.json`'s py-opendisplay pin once a
   release containing the fix exists.

## Verification

```bash
# py-opendisplay
cd /home/davelee/opendisplay/py-opendisplay
uv run pytest tests/unit/test_models_buzzer_activate.py tests/unit/test_device_buzzer_activate.py tests/unit/test_protocol_commands.py -v
uv run pytest                       # full suite
uv run mypy src
uv run ruff check .

# Home_Assistant_Integration (strings-only change)
cd /home/davelee/opendisplay/Home_Assistant_Integration
pytest tests/ -v
python3 -c "import json; json.load(open('custom_components/opendisplay/strings.json')); json.load(open('custom_components/opendisplay/translations/en.json')); print('JSON OK')"
```

Hardware-free spot check (landmarks from the reference doc's §4.2 table):

```bash
cd /home/davelee/opendisplay/py-opendisplay
python3 -c "
from opendisplay.models.buzzer_activate import hz_to_index
cases = {0:0, -5:0, 1:1, 400:117, 440:120, 880:144, 1000:148, 2000:172, 523:126, 6200:212, 11840:234, 12000:234, 21714:255, 99999:255}
for hz, want in cases.items():
    got = hz_to_index(hz)
    assert got == want, f'{hz} Hz -> {got}, want {want}'
print('all landmarks OK; octave delta =', hz_to_index(880) - hz_to_index(440))
"
```

Expected: all assertions pass, octave delta == 24. Optional real-hardware
confirmation: call the HA service with `frequency_hz: 440` and verify by ear/
tuner that it now sounds A4 (previously sounded A♯4, a semitone sharp).

### Critical files

- `py-opendisplay/src/opendisplay/models/buzzer_activate.py`
- `py-opendisplay/tests/unit/test_models_buzzer_activate.py`
- `Home_Assistant_Integration/custom_components/opendisplay/strings.json`
- `Home_Assistant_Integration/custom_components/opendisplay/translations/en.json`
- `Home_Assistant_Integration/custom_components/opendisplay/services.yaml`
- Reference: `opendisplay-protocol/docs/BUZZER_MUSIC_PROTOCOL_REFERENCE.md` (§4, §7.7, §10 item 11, §11 item 1)
