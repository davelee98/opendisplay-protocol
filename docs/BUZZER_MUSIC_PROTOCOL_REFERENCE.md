# Musical Buzzer Protocol & Firmware Reference (`0x0077`)

Spec-grade, standalone reference for the OpenDisplay **musical buzzer** feature
shipped in Firmware PR #98 (commit `5ed21a9`, "feat: quarter-tone musical buzzer
with hardware PWM + non-blocking playback"). It is detailed enough to recreate
the wire protocol and the firmware implementation from scratch with no access to
the source tree.

This document **consolidates and supersedes**, for anyone recreating the
protocol, the two Firmware-repo docs
`Firmware/docs/buzzer-protocol.md` and `Firmware/docs/buzzer-music-implementation.md`.
Those remain in place for context; where they disagree with the current source
the source wins, and the discrepancies are called out in
[§11](#11-discrepancies-found-vs-existing-docs--models).

All firmware `file:line` references are into the OpenDisplay **Firmware** repo
(`nrf52840custom` / `esp32-s3-*` targets share the code). Host-side references
are into **py-opendisplay** and the **Home_Assistant_Integration**. Canonical
opcode definitions are in **opendisplay-protocol**
(`src/opendisplay_protocol.h`).

---

## 1. Overview

A *passive* piezo buzzer has no internal oscillator: it emits sound only while
driven with an AC / square-wave signal, and its pitch equals the drive
frequency. The firmware therefore synthesizes the waveform itself.

The `0x0077` command carries a **melody**: a nested structure of *patterns*, each
a list of *(frequency, duration)* **steps**. A single call can play one beep or a
multi-pattern tune repeated several times.

PR #98 reworked the feature along three axes versus the prior simple buzzer:

| Axis | Before PR #98 | After PR #98 (this document) |
|------|---------------|------------------------------|
| Pitch model | `freq_idx` 1..255 mapped **linearly** onto 400–12000 Hz (`structs.h:209` comment; still the py-opendisplay behaviour) | `freq_idx` indexes a **quarter-tone equal-temperament** scale, `Freq(idx) = 13.75 · 2^(idx/24)` |
| Waveform generation | GPIO bit-bang square wave (`buzzer_drive_tone_sw`, `delayMicroseconds`) that warbled under SoftDevice/WiFi load | Native **hardware PWM** (nRF52 `HardwarePWM` HwPWM3 / ESP32 LEDC) |
| Playback | Fully **blocking** loop in the BLE write callback; capped at 5 s (commit `83d1ebd`, #58) to bound the stall | **Non-blocking** state machine ticked from the main loop; ACK returns immediately; 30 s cap |

Key capabilities:

- **Quarter-tone resolution** — 24 steps/octave (50 cents/step); every 12-TET
  note lands on an even index; odd indices are the +quarter-tone microtones.
- **Hardware PWM** — rock-steady pitch even while BLE/WiFi stacks preempt the CPU.
- **Non-blocking playback** — the handler validates, starts the first step, and
  ACKs "accepted & started" (not "finished"); a new `0x0077` **preempts** an
  in-progress melody.
- **Octave folding** — out-of-range notes are shifted into the playable window,
  preserving pitch class, rather than clamped or rejected.

Two firmware entry points exist:

| Trigger | Source | Behaviour |
|---------|--------|-----------|
| `0x0077` activate command | host→device over BLE | Non-blocking, caller-defined melody |
| Power-off alert | internal, `device_control.cpp:77` | Fixed **blocking** two-beep chirp before power latch off ([§8](#8-power-off-alert-internal-non-ble)) |

---

## 2. Configuration block — `0x29 passive_buzzer`

A buzzer instance must be provisioned via the device config (TLV block `0x29`)
before `0x0077` can address it. Repeatable, **max 4 instances**. Fixed **32-byte**
record (`structs.h:212-219`; a `static_assert(sizeof(PassiveBuzzerConfig) == 32)`
guards it in `buzzer_control.cpp:10`). Parsed in `config_parser.cpp:409-419`;
instances beyond the 4th are skipped with a warning.

| Offset | Field | Type | Meaning |
|-------:|-------|------|---------|
| 0 | `instance_number` | `uint8` | Instance index (0-based) |
| 1 | `drive_pin` | `uint8` | GPIO driving the buzzer (via transistor). `0xFF` = unconfigured |
| 2 | `enable_pin` | `uint8` | Optional enable/FET gate. `0xFF` = unused |
| 3 | `flags` | `uint8` | Bit 0 = `BUZZER_FLAG_ENABLE_ACTIVE_HIGH` (`structs.h:210`); other bits reserved |
| 4 | `duty_percent` | `uint8` | PWM duty 1–100. `0` or `>100` → firmware substitutes **50** |
| 5–31 | `reserved[27]` | — | Reserved, zero |

Host-side serializer: `py-opendisplay/src/opendisplay/protocol/config_serializer.py:466`
(`serialize_passive_buzzer`, 32 bytes); parser at `config_parser.py:200`.

**Enable-pin polarity** (`buzzer_control.cpp:104-113`, `buzzer_set_enable`): when
`enable_pin != 0xFF`, the pin is asserted for the duration of playback. If
`BUZZER_FLAG_ENABLE_ACTIVE_HIGH` is set the pin is driven HIGH to enable;
otherwise it is active-low. Enable is de-asserted at stop.

**Boot init** (`buzzer_control.cpp:254-267`, `initPassiveBuzzers`, called from
`display_service.cpp:742`): each configured `drive_pin` is set OUTPUT/LOW and
each `enable_pin` is de-asserted.

---

## 3. Wire protocol — activate command `0x0077`

Canonical definition: `opendisplay-protocol/src/opendisplay_protocol.h:441-450`
(`CMD_BUZZER = 0x0077u`, `RESP_BUZZER_ACK = 0x77u` at line 589).

### 3.1 Framing

The 2-byte **big-endian** opcode `0x00 0x77` prefixes the BLE write. The
dispatcher strips it before the handler runs — `communication.cpp:652-654` calls
`handleBuzzerActivate(data + 2, len - 2)`. All byte offsets below are into the
**post-opcode payload**; `data[0]` is the first byte after the opcode. Every
field is a single byte, so endianness does not apply within the payload.

Security/session: like other command opcodes, `0x0077` requires an authenticated
session when device security is enabled and is sent encrypted on secured devices
(`opendisplay_protocol.h:446`).

### 3.2 Payload layout

```
Offset  Field           Meaning
------  --------------  -----------------------------------------------------
  0     instance        buzzer instance index (0-based)
  1     outer_repeat    whole-melody repeat count; 0 is coerced to 1
  2     pattern_count   number of patterns that follow; MUST be >= 1

  then pattern_count patterns, each:
    +0  nsteps          number of steps in this pattern (may be 0)
    then nsteps steps, each 2 bytes:
      +0  freq_idx      0 = rest/silence; 1..255 = tone index (see §4)
      +1  dur_unit      duration in 5 ms units; effective ms = dur_unit * 5
```

Total length of a melody = `3 + Σ_patterns (1 + 2·nsteps_i)`.

- A pattern with `nsteps = 0` is legal; it contributes only its inter-pattern
  gap (`buzzer_control.cpp:225-230`, and the validator allows `need = 0`).
- The payload must terminate **exactly** at the end of the last step; trailing
  bytes are rejected (error `0x06`).
- Maximum copied melody size is **256 bytes** (`s_buzzer.melody[256]`,
  `buzzer_control.cpp:132`); `len > 256` → error `0x05` (`:320-324`). This ceiling
  is unreachable inside a single BLE MTU (≤244 B) but is enforced defensively.

### 3.3 Validation (two-pass, pre-playback)

`handleBuzzerActivate` (`buzzer_control.cpp:269-349`) fully validates before any
sound is produced:

1. `len < 3` → error `0x01` (`:270-274`).
2. `instance >= passive_buzzer_count` → error `0x02` (`:275-280`).
3. selected instance `drive_pin == 0xFF` → error `0x03` (`:281-286`).
4. `outer_repeat == 0` → coerced to `1` (`:288-291`, not an error).
5. `pattern_count == 0` → error `0x04` (`:292-297`).
6. Walk every declared pattern: for each, read `nsteps`, require
   `scan + 2·nsteps <= len`; overrun → error `0x05` (`:299-314`).
7. After the walk, `scan != len` (trailing/short) → error `0x06` (`:315-319`).
8. `len > 256` → error `0x05` (`:320-324`).

Only after all checks pass does the handler preempt any current melody, copy the
payload, start step 0, and ACK.

### 3.4 Response frame

The device replies on the notify channel with a fixed **4-byte** frame
(`sendResponse`, e.g. `buzzer_control.cpp:347-348`):

```
Offset  Field    Value
------  -------  --------------------------------------------
  0     status   0x00 = success, 0xFF = error (NACK)
  1     opcode   0x77 (low byte of the opcode, echoed)
  2     code     error code (0x00 on success)
  3     pad      0x00 (reserved)
```

| status | code | Meaning | Trigger (buzzer_control.cpp) |
|--------|------|---------|------------------------------|
| `0x00` | `0x00` | Success — accepted & playback started | `:347` (sent immediately, before the melody ends) |
| `0xFF` | `0x01` | Truncated header (`len < 3`) | `:271` |
| `0xFF` | `0x02` | Invalid instance (`>= count`) | `:277` |
| `0xFF` | `0x03` | Unconfigured buzzer (`drive_pin == 0xFF`) | `:283` |
| `0xFF` | `0x04` | No patterns (`pattern_count == 0`) | `:294` |
| `0xFF` | `0x05` | Truncated body / oversize (`len > 256`) | `:302`, `:309`, `:321` |
| `0xFF` | `0x06` | Trailing bytes (payload not ending at last step) | `:316` |

The ACK reports acceptance only. Later 30 s-cap truncation or preemption by a
subsequent `0x0077` is **not** an error and produces no further response.

Host-side validation of the ACK
(`py-opendisplay/src/opendisplay/protocol/responses.py:110`,
`validate_ack_response`) only checks the **first two bytes** (opcode echo, high
bit tolerated); it does not inspect `status` byte 0 or the error `code`. Note the
canonical opcode header documents a 2-byte response (`[0x00][0x77]` /
`[0xFF][0x77]`, `opendisplay_protocol.h:444-445`); the firmware actually emits 4
bytes. The extra two bytes carry the granular error code and are read by clients
that inspect them.

---

## 4. Note / frequency model

### 4.1 The quarter-tone scale

`freq_idx` is **not** a raw frequency. It indexes a quarter-tone,
equal-temperament scale anchored at A-1 = 13.75 Hz:

```
Freq(idx) = 13.75 · 2^(idx / 24)          for idx in 1..255
Freq(0)   = silence / rest (nNone)
```

- **24 steps per octave**; one index step = **one quarter-tone = 50 cents**.
  Two indices = one semitone; 24 indices = one octave.
- `idx = 0` is the **rest sentinel** (`nNone`): during its step the drive pin is
  held low but the enable pin stays asserted, so the rest keeps the melody's
  rhythm (`buzzer_control.cpp:191-202`).
- Every standard 12-TET note lands on an **even** index; odd indices are the
  +quarter-tone microtones. Landmarks: `idx 120 = A4 = 440.00 Hz` **exactly**,
  `idx 126 = C5 = 523.25 Hz`, `idx 212 = nG8 = 6271.93 Hz`,
  `idx 255 = 21714.33 Hz`.

### 4.2 The centi-Hz lookup table

The firmware never evaluates `2^x` at runtime (ESP32-C3/C6 are soft-float and
both PWM backends want integer math). Instead a 256-entry `uint32` table in
**centi-Hz** (Hz × 100) is precomputed (`buzzer_control.cpp:27-60`,
`kBuzzerCentiHzTable`):

```
kBuzzerCentiHzTable[idx] = round(100 · 13.75 · 2^(idx/24)),   table[0] = 0
```

Representative entries (centi-Hz → Hz):

| idx | centi-Hz | Hz | note |
|----:|---------:|-----:|------|
| 0 | 0 | — | nNone (rest) |
| 1 | 1415 | 14.15 | nAm1p (lowest) |
| 117 | 40348 | 403.48 | nG4p (min playable) |
| 120 | 44000 | 440.00 | nA4 |
| 126 | 52325 | 523.25 | nC5 |
| 144 | 88000 | 880.00 | nA5 |
| 148 | 98777 | 987.77 | nB5 |
| 172 | 197553 | 1975.53 | nB6 |
| 212 | 627193 | 6271.93 | nG8 |
| 234 | 1183982 | 11839.82 | nFs9 (max playable) |
| 255 | 2171433 | 21714.33 | nE10p (top of table) |

The max entry (2 171 433) fits well within `uint32`.

Regeneration one-liner (from the source comment, `buzzer_control.cpp:25-26`):
```python
print(',\n'.join(', '.join(
    f'{round(100*13.75*2**(i/24)) if i else 0:8d}u' for i in range(r, r+8))
    for r in range(0, 256, 8)))
```

### 4.3 Playable window and octave folding

The buzzer's usable output retains the old hardware limits, now expressed in
centi-Hz (`buzzer_control.cpp:13-14`):

```
kBuzzerFreqMinCentiHz = 40000    (400.00 Hz)
kBuzzerFreqMaxCentiHz = 1200000  (12000.00 Hz)
```

These are converted to **index bounds** at compile time by two recursive
`constexpr` scans of the table (`buzzer_control.cpp:65-78`):

```
kBuzzerMinNoteIdx = first idx with table[idx] >= 40000   → 117  (403.48 Hz)
kBuzzerMaxNoteIdx = last  idx with table[idx] <= 1200000  → 234  (11839.82 Hz)
```

Both values are pinned by `static_assert` (`:77-78`).

An index outside `[117, 234]` is **not clamped or rejected** — it is octave-folded
(`buzzer_control.cpp:82-97`, `buzzer_fold_index`): while `idx < 117` add 24 (up an
octave); while `idx > 234` subtract 24 (down an octave); repeat until inside the
window. This preserves pitch class. `nNone (0)` passes through unchanged. A final
defensive clamp to `kBuzzerMinNoteIdx` guards a degenerate (<1 octave) window.

Examples: `idx 102 (nC4)` folds **up** to `idx 126 (nC5, 523.25 Hz)`;
`idx 255` folds **down** to `idx 231 (10857.16 Hz)`. Clients may therefore author
a melody in any octave and trust each note sounds at its nearest in-range octave.

Folding happens at the single lookup point `buzzer_index_to_centihz`
(`buzzer_control.cpp:99-102`): fold, then read the table.

### 4.4 Note-name enum

`enum BuzzerNote : uint8_t` (`buzzer_control.h:27-92`) names every index so
melodies can be authored by note: `{nA4, 40, nNone, 10, nA5, 40}`. Naming is
`n<Note><s?><octave><p?>`: `n` prefix; note letter `A`–`G`; `s` = sharp; trailing
`p` = +quarter-tone (odd index); octave −1 spelled `m1`. Octaves increment at C
with **C0 = idx 6** and 24 indices/octave, so `Cn` anchor `= 6 + 24·n`; add the
semitone offset ×2 (C 0, C♯ 2, D 4, D♯ 6, E 8, F 10, F♯ 12, G 14, G♯ 16, A 18,
A♯ 20, B 22), +1 for a `p` variant. Full table generated by the Python snippet in
`buzzer_control.h:18-26`.

---

## 5. Timing model

Constants (`buzzer_control.cpp:15-17`):

| Constant | Value | Meaning |
|----------|------:|---------|
| `kBuzzerDurationUnitMs` | 5 ms | One `dur_unit` |
| `kBuzzerInterPatternGapMs` | 20 ms | Silent gap inserted **between** consecutive patterns |
| `kBuzzerMaxTotalMs` | 30000 ms | Hard cap on total playback wall-time |

- **Step duration:** `effective_ms = dur_unit · 5`. Per-step maximum is
  `255 · 5 = 1275 ms` (`dur_unit` is one byte).
- **Inter-pattern gap:** a 20 ms silence is inserted between patterns within a
  repeat — not before the first pattern, not after the last, and **not** between
  outer repeats (`buzzer_control.cpp:207-223`).
- **Playback order:** repeat the whole melody `outer_repeat` times; within a
  repeat play patterns in order (20 ms gap between them); within a pattern play
  steps in order, driving `Freq(freq_idx)` for `dur_unit·5 ms` (rests drive
  silence but hold the enable pin).

### 5.1 Total-duration cap (30 s)

Introduced as a 5 s cap in commit `83d1ebd` (#58) to stop a blocking bit-bang
loop from starving BLE/watchdog on a hostile or buggy payload
(`outer·pattern_count·nsteps` steps × up to 1275 ms each = hours). PR #98's
non-blocking rewrite **raised it to 30 s** (`kBuzzerMaxTotalMs = 30000u`).

Enforcement (`buzzer_control.cpp:167-188`), checked before each step:

- If `now - play_start_ms >= 30000` → playback stops immediately
  (`buzzer_stop_internal`).
- A step that would overrun the cap is **truncated** to the remaining time:
  `ms = min(dur_unit·5, kBuzzerMaxTotalMs - elapsed)` (`:181-185`).
- A step whose (possibly truncated) duration is `0` is skipped as a no-op
  (`:186-188`).

The cap bounds total wall-time regardless of `outer_repeat` or payload size.
Truncation/stop is silent — no additional response is sent.

> **Stale comments:** two in-source comments still say "5 s" cap
> (`buzzer_control.cpp:144` field comment, `:167` inline) even though the constant
> is 30000 ms. The constant is authoritative.

---

## 6. Firmware implementation

### 6.1 Non-blocking playback state machine

State lives in a single static struct `s_buzzer` (`buzzer_control.cpp:129-145`),
modeled on the LED flasher. Because the ESP32 recycles the incoming BLE buffer
the instant the handler returns, the payload is **copied** into
`s_buzzer.melody[256]` before playback (`:329`).

Fields: `active`, `b` (config ptr), `melody[256]` + `melody_len`, `outer`, `rep`
(0-based current repeat), `pattern_count`, `pi` (0-based current pattern), `poff`
(byte cursor into the melody copy), `nsteps` (steps in current pattern), `si`
(0-based current step), `phase`, `tone_on`, `step_until_ms` (wait deadline),
`play_start_ms` (cap anchor).

Two phases (`buzzer_phase_t`, `:124-127`):

- `BZ_PHASE_STEP` — ready to emit step `si` of the current pattern.
- `BZ_PHASE_GAP` — waiting the 20 ms inter-pattern gap.

**Cursor invariant:** on entry to a repeat, `poff = 3` (skip
instance/outer/pattern_count), `nsteps = melody[poff++]` (`:336-338`).

`buzzer_run()` (`:161-237`) is a loop that emits the next scheduled item and
returns as soon as a timed wait is armed (or finishes). Transitions:

```
                 ┌───────────────────────── new 0x0077 accepted ──────────────┐
                 v                                                             │
   (idle) ── handleBuzzerActivate ──► start step 0, ACK ─► [BZ_PHASE_STEP]     │
                                                             │                 │
     si < nsteps:                                            │                 │
        read (freq_idx,dur) at poff; poff+=2; si++           │                 │
        ms = min(dur*5, cap_remaining)                       │                 │
        ms==0 → loop (skip)                                  │                 │
        else assert enable; tone_start OR tone_stop(rest);   │                 │
             step_until_ms = now+ms; RETURN ────────────────►│ (wait in        │
                                                             │  buzzerService) │
     si == nsteps (pattern done):                            │                 │
        pi+1 < pattern_count → [BZ_PHASE_GAP],               │                 │
                               step_until_ms=now+20ms; RETURN│                 │
        else rep++;                                          │                 │
             rep >= outer → stop_internal (idle)             │                 │
             else pi=0; poff=3; nsteps=melody[poff++]; si=0 ─┘ (loop, STEP)

   [BZ_PHASE_GAP] on wait-elapsed: pi++; nsteps=melody[poff++]; si=0;
                  phase=STEP; loop
```

Any-time transitions:
- **30 s cap** (`:168-171`) — checked at the top of every `buzzer_run` iteration
  → `stop_internal`.
- **Preemption** (`:328`) — a newly accepted `0x0077` calls `buzzer_stop_internal`
  then reinitialises `s_buzzer`.
- **Power-off alert** (`:353`) — also preempts via `buzzer_stop_internal`.

`buzzer_stop_internal` (`:147-156`): stop the tone if sounding, de-assert enable,
drive the pin LOW, then `memset(&s_buzzer, 0, ...)` (clears `active`).

### 6.2 Loop integration

`buzzerService()` (`:239-252`) is the tick:

1. `!active` → return.
2. `millis() - step_until_ms < 0` (wraparound-safe signed compare) → the current
   step/gap is still running → return.
3. Wait elapsed → stop the current tone (`buzzer_hw_tone_stop`), clear
   `tone_on`, then call `buzzer_run()` to schedule the next item.

The tone for step *N* is silenced by `buzzerService` at the **start** of the
service pass that advances to step *N+1* (not inside `buzzer_run`), so
`buzzer_run` always begins with the pin already quiet.

`buzzerService()` is called from **every** input-servicing site so steps advance
promptly (`main.cpp`): `loop()` `:265`; the ESP32 deep-sleep first loop
`work-in-flight` branch `:381` and idle branch `:409`; `idleDelay()` `:422`; and
the nRF loop tail `:439`. Step boundaries are tight during normal operation; the
one place timing can drift is a long `idleDelay` chunk (chunked at 100 ms,
`main.cpp` `idleDelay`), the same tolerance the LED flasher accepts.

### 6.3 Hardware PWM layer (`buzzer_hw.cpp` / `.h`)

Interface (`buzzer_hw.h:19-22`): frequency is passed in **centi-Hz** to keep the
math integer.

```
bool buzzer_hw_tone_start(uint8_t pin, uint32_t centihz, uint8_t duty_percent);
void buzzer_hw_tone_stop (uint8_t pin);   // idempotent; leaves pin OUTPUT/LOW
```

`tone_start` returns false on bad frequency or if the PWM resource is owned
elsewhere; calling it again on a playing pin retunes it. Duty of `0` or `>100` is
coerced to 50 in both backends.

**nRF52 (Adafruit core)** — `HardwarePWM` instance **HwPWM3**
(`buzzer_hw.cpp:13-86`). HwPWM0..2 are reserved for the core/other features;
HwPWM3 exists on nRF52840. 16 MHz base clock (`kBuzzerPwmClockHz`), 15-bit
COUNTERTOP (`kBuzzerPwmMaxTop = 32767`). Algorithm:

1. **Prescaler walk** `div_exp = 0..7` (DIV_1..DIV_128): `clk = 16MHz >> div_exp`;
   `top = round(clk·100 / centihz) = (clk·100 + centihz/2) / centihz` (64-bit);
   pick the smallest `div_exp` whose `top <= 32767` (`:31-43`). Yields ≤ ~0.5
   cent pitch error across the range.
2. **Cooperative ownership** — token `kBuzzerPwmToken = 0x425A5A21` ("BZZ!").
   Take ownership only if not already held; abort (return false) if owned by
   something else (`:48-53`). Held only while a tone actually sounds; released in
   `tone_stop`.
3. Stop if enabled, `setClockDiv(div_exp)`, `setMaxValue(top)`, `addPin(pin)`,
   then `writePin(pin, val)` where `val = clamp(top·duty/100, 1, top-1)`
   (`:57-72`).

`tone_stop` (`:75-86`): if owner, remove pin, `stop()`, release ownership; always
leave the pin OUTPUT/LOW. Runs alongside the SoftDevice.

**ESP32 — LEDC**, shimmed across the build matrix (`buzzer_hw.cpp:95-182`).
Resolution 10-bit (`kBuzzerLedcResBits = 10`, duty 0..1023). A module-static
`s_ledc_pin` (0xFF = none) tracks the attached pin so `tone_stop` is idempotent
and never detaches an unattached pin (avoids the core's
`ledcDetach(): pin N is not attached` log).

- **Arduino core 3.x** (`ESP_ARDUINO_VERSION_MAJOR >= 3`, pin-based API,
  `:110-147`): `freq_hz = (centihz + 50)/100` (round to integer Hz; ≤ 4 cents at
  the 403 Hz floor). `ledcAttach(pin, freq_hz, 10)`; `ledcWrite(pin, duty)` where
  `duty = max(1, 1023·duty_percent/100)`. Detaches any prior pin first.
- **Legacy core 2.x** (`:149-182`): reserved channel `kBuzzerLedcChannel = 7`,
  `ledcSetup(7, centihz/100.0, 10)` (full double precision),
  `ledcAttachPin(pin, 7)`, `ledcWrite(7, duty)`.

Unsupported platforms `#error` at `buzzer_hw.cpp:186`.

Duty cycle for playback comes from `PassiveBuzzerConfig.duty_percent` (default
50), passed at `buzzer_control.cpp:196`.

---

## 7. Worked examples

All examples are real command frames drawn from the firmware bench-test doc
(`Firmware/docs/buzzer-music-implementation.md`) and the py-opendisplay unit
tests. Hex strings are the full BLE write = 2-byte big-endian opcode + payload,
no spaces.

### 7.1 Simple — single tone (A4, 200 ms)

Payload built by `BuzzerActivateConfig` for one pattern, one step, tone idx 120,
40 units:

```
00 77                opcode 0x0077
   00                instance = 0
   01                outer_repeat = 1
   01                pattern_count = 1
      01             pattern[0].nsteps = 1
         78 28       step: freq_idx 0x78 = 120 (nA4 = 440.00 Hz), dur 0x28 = 40 units = 200 ms
```
Frame (T5 in the bench doc, "one steady A4"): `00770001010178C8` is the 1 s
variant (`C8` = 200 units = 1000 ms); the 200 ms form is `0077000101017828`.
Nominal time 200 ms. Expected response `00 77 00 00`.

### 7.2 Exact-octave, multi-step (A4 · rest · A5)

Bench test **T1** — the case the old linear map could not reproduce (a true
octave):

```
00 77                opcode
   00                instance 0
   01                outer_repeat 1
   01                pattern_count 1
      03             nsteps = 3
         78 28       idx 120 nA4 440.00 Hz, 40u = 200 ms
         00 0A       idx 0 rest,             10u =  50 ms
         90 28       idx 144 nA5 880.00 Hz, 40u = 200 ms
```
Frame: `0077000101037828000A9028`. The two tones sound exactly one octave apart.

### 7.3 Complex — two patterns, a rest, played twice

Bench test **T8** = the protocol doc's canonical worked example. Instance 0,
whole melody repeated twice, two patterns (a 20 ms gap between them):

- Pattern A: idx 148 (nB5, 987.77 Hz) 100 ms, then rest 50 ms.
- Pattern B: idx 172 (nB6, 1975.53 Hz) 200 ms.

```
00 77                opcode
   00                instance 0
   02                outer_repeat = 2
   02                pattern_count = 2
      02             pattern A: nsteps = 2
         94 14       idx 0x94=148 nB5, 0x14=20u = 100 ms
         00 0A       rest, 0x0A=10u = 50 ms
      01             pattern B: nsteps = 1
         AC 28       idx 0xAC=172 nB6, 0x28=40u = 200 ms
```
Frame: `0077000202029414000A01AC28`. Nominal time
≈ `2 · (100 + 50 + 20_gap + 200) = 740 ms`, far under the 30 s cap. Response
`00 77 00 00`.

### 7.4 Melody — "Twinkle Twinkle" opening

Bench test **T9** (C5 C5 G5 G5 A5 A5 G5, 100 ms each, last note 200 ms), one
pattern of 7 steps:

```
Frame: 0077000101077E147E148C148C14901490148C28
   00 77 · 00 · 01 · 01 · 07 ·
   7E 14 (C5 idx126 100ms) 7E 14 · 8C 14 (G5 idx140 100ms) 8C 14 ·
   90 14 (A5 idx144 100ms) 90 14 · 8C 28 (G5 200ms)
```

### 7.5 Octave folding

Bench **T2** (fold-up): `0077000101016628` sends nC4 (idx 0x66 = 102), below the
window, which folds up to nC5 (523.25 Hz) — audibly identical to
`0077000101017E28` which sends nC5 (idx 126) directly. Bench **T3** (fold-down):
`007700010101FF28` sends idx 255, which folds down to idx 231 → ~10 857 Hz, not
21.7 kHz.

### 7.6 Error-path frames (from the bench doc)

| Frame | Meaning | Response |
|-------|---------|----------|
| `0077000100` | `pattern_count = 0` | `FF 77 04 00` |
| `0077050101017828` | instance 5 (absent) | `FF 77 02 00` |
| `0077000101027828` | declares 2 steps, sends 1 | `FF 77 05 00` |
| `0077000101017828EE` | one trailing byte | `FF 77 06 00` |

### 7.7 Host-side call (py-opendisplay / Home Assistant)

The Python model builds the **payload** (from `instance` onward the device
handler expects, minus the instance byte); the device API prepends opcode +
instance.

`BuzzerActivateConfig.to_bytes()`
(`py-opendisplay/src/opendisplay/models/buzzer_activate.py:74-76`) emits
`[outer_repeats][n_patterns][patterns...]`; each pattern
`[n_steps][freq][dur]...` (`:39-41`).

`build_buzzer_activate_command`
(`py-opendisplay/src/opendisplay/protocol/commands.py:478-493`) prepends the
2-byte big-endian opcode and the instance byte:
```python
cmd = 0x0077.to_bytes(2, "big")            # b"\x00\x77"
return cmd + bytes([buzzer_instance]) + config.to_bytes()
```
So the full wire frame = `00 77 | instance | outer | n_patterns | patterns…`,
matching [§3.2](#32-payload-layout).

`OpenDisplayDevice.activate_buzzer`
(`py-opendisplay/src/opendisplay/device.py:1246-1276`) writes that frame and
awaits the ACK with `validate_ack_response(..., BUZZER_ACTIVATE)`, default
timeout `TIMEOUT_REFRESH = 90.0 s` (`device.py:428`).

Home Assistant service `activate_buzzer`
(`Home_Assistant_Integration/custom_components/opendisplay/services.py:959-979`,
`_async_activate_buzzer`) calls `BuzzerActivateConfig.single_tone(frequency_hz,
duration_ms, repeats)` then `device.activate_buzzer(instance, config)`. Its schema
`SCHEMA_ACTIVATE_BUZZER` (`services.py:209-215`): `instance` 0–3 (default 0),
`frequency_hz` 0–12000 (default 1000), `duration_ms` 5–1275 (default 100),
`repeats` 1–255 (default 1). It is a **single-tone, single-pattern** front end —
it cannot express multi-pattern melodies.

> **Important host-side caveat:** `single_tone`/`hz_to_index` use the **old
> linear** Hz→index map, which does **not** match the firmware's quarter-tone
> scale. See [§11](#11-discrepancies-found-vs-existing-docs--models). The raw
> byte frames in §7.1–7.6 are correct because they specify `freq_idx` directly.

---

## 8. Power-off alert (internal, non-BLE)

`passiveBuzzerPowerOffAlert()` (`buzzer_control.cpp:351-377`) is **not** reachable
over BLE. It is invoked from `device_control.cpp:77` when a button's power-off
hold threshold is met, immediately before the power latch is cut. Behaviour:

1. Preempt any active melody (`buzzer_stop_internal`, `:353`).
2. Pick the first buzzer whose `drive_pin` is neither `0` nor `0xFF`
   (`:355-364`).
3. Play a fixed two-beep chirp at `nG8` (idx 212 ≈ **6271.93 Hz**, the nearest
   quarter-tone to the old ~6200 Hz target): 80 ms on / 80 ms gap / 80 ms on,
   using blocking `delay()` (`:366-374`).
4. De-assert enable and drive the pin LOW (`:375-376`).

Unlike `0x0077`, this path stays **blocking** — it runs synchronously during
shutdown, where there is nothing left to starve.

---

## 9. Test coverage summary

### 9.1 `test_models_buzzer_activate.py`

Unit tests for the host-side model and helpers (all against the **linear** map):

- `TestHzToIndex` — `hz_to_index(0)` and `(-100)` → 0 (silence); `(400)` → 1;
  `(12000)` → 255; `(99999)` clamps to 255; `(1)` and `(399)` → 1 (never 0 for a
  positive Hz); midpoint `(6200)` → 126–130.
- `TestMsToUnits` — `ms_to_units`: `(1)`→1, `(5)`→1, `(10)`→2, `(7)`→1, `(8)`→2
  (round-to-nearest), `(99999)`→255 clamp, `(0)`→1 minimum.
- `TestBuzzerPatternToBytes` — one step `BuzzerStep(10, 20)` →
  `bytes([1,10,20])`; two steps → `bytes([2,5,10,200,50])` (verifies
  `[n_steps][freq][dur]…` layout).
- `TestBuzzerActivateConfigToBytes` — `single_tone(1000 Hz, 100 ms, repeats=3)` →
  `data[0]=3` (outer_repeats), `data[1]=1` (n_patterns), `data[2]=1` (n_steps),
  `len==5`; silence tone (`0 Hz`) → `data[3]==0`; `repeats=0`→coerced to 1;
  default repeats == 1.
- `TestBuzzerActivateConfigRoundtrip` — single-tone frame is exactly 5 bytes
  `[repeats][n_patterns][n_steps][freq][dur]`.

**What is guaranteed:** the model's serialization layout and clamp/round rules
for the *linear* helper. **Not** verified: agreement of the index with firmware
pitch, multi-pattern melodies, or the octave-fold behaviour.

### 9.2 `test_device_buzzer_activate.py`

Async tests of `OpenDisplayDevice.activate_buzzer` against a fake connection:

- `test_activate_buzzer_sends_0077_and_validates_ack` — for
  `single_tone(1000 Hz, 100 ms)`, the exact bytes written are
  `b"\x00\x77\x00" + config.to_bytes()` (opcode + instance 0 + payload); the read
  timeout equals `TIMEOUT_REFRESH`; a `b"\x00\x77\x00\x00"` ACK is accepted and
  returned verbatim.
- `test_activate_buzzer_custom_timeout` — a supplied `timeout=5.0` is honoured on
  the read; a 2-byte `b"\x80\x77"` (high-bit) ACK is accepted.
- `test_activate_buzzer_requires_connection` — no connection → `RuntimeError`
  matching "not connected".

**What is guaranteed:** the on-wire opcode/instance framing, the timeout
plumbing, high-bit ACK tolerance, and the not-connected guard.

There is **no** automated test of the firmware C++ (state machine, table, PWM,
folding, cap). Those are covered only by the manual BLE bench matrix T1–T11 in
`Firmware/docs/buzzer-music-implementation.md` (used as the §7 examples).

---

## 10. Recreation checklist

To reimplement the musical buzzer end-to-end:

1. **Opcode registration** — reserve `CMD_BUZZER = 0x0077` (host→device) and
   `RESP_BUZZER_ACK = 0x77`; wire the dispatcher to strip the 2-byte big-endian
   opcode and call the handler with the remaining payload.
2. **Config block** — define the 32-byte `0x29 passive_buzzer` record
   (`instance_number`, `drive_pin`, `enable_pin`, `flags` with
   `ENABLE_ACTIVE_HIGH` bit 0, `duty_percent`, `reserved[27]`), max 4 instances;
   init pins at boot.
3. **Note table** — precompute `kBuzzerCentiHzTable[256]` as
   `round(100·13.75·2^(idx/24))` centi-Hz, entry 0 = 0 (rest). Derive the playable
   window `[117, 234]` from the 40000/1200000 centi-Hz limits.
4. **Fold + lookup** — `buzzer_fold_index` (±24 into range, pitch class
   preserved, rest passes through) feeding a single `index_to_centihz` lookup.
5. **Payload parser/validator** — two-pass: validate header
   (`len>=3`, valid instance, configured pin, `outer` 0→1, `pattern_count>=1`),
   then walk every `[nsteps][freq,dur]·nsteps]` block requiring an exact fit
   (`scan == len`), reject oversize (`len > 256`). Emit the 4-byte
   `[status][0x77][code][0x00]` response with error codes 0x01–0x06.
6. **PWM driver** — `tone_start(pin, centihz, duty)` / `tone_stop(pin)` on a
   native PWM peripheral: nRF52 HwPWM3 (prescaler walk DIV_1..DIV_128, 15-bit top,
   cooperative ownership token); ESP32 LEDC (10-bit, integer-Hz on core 3.x /
   double on 2.x, tracked attach pin for idempotent stop). Duty from config,
   default 50, clamped to `[1, top-1]`.
7. **State machine** — `s_buzzer` with a private 256-byte copy of the payload;
   phases STEP/GAP; cursor `poff` starting at 3; emit tone-or-rest per step,
   20 ms gap between patterns, no gap between outer repeats; `buzzerService()`
   tick that silences the prior tone then advances; call it from every
   input-servicing site in the main loop.
8. **Duration cap** — `kBuzzerMaxTotalMs = 30000`: stop at/after 30 s, truncate the
   overrunning step, skip zero-length steps.
9. **Preemption + ACK semantics** — a new `0x0077` stops the current melody and
   starts the new one; ACK means "accepted & started," sent immediately after the
   first step; no response on cap/preempt.
10. **Power-off alert** — a separate blocking two-beep at `nG8` (idx 212) on the
    first usable buzzer, preempting any melody.
11. **Host model/serializer** — `BuzzerStep`/`BuzzerPattern`/`BuzzerActivateConfig`
    → `[outer][n_patterns]([nsteps][freq,dur]…)…`; device wrapper prepends
    `[00 77][instance]`. **Fix the Hz→index map to the quarter-tone scale**
    (`idx = clamp(round(24·log2(hz/13.75)), 1, 255)`) — the shipped linear map is
    wrong (see §11).

---

## 11. Discrepancies found (source vs existing docs / models)

1. **py-opendisplay `hz_to_index` is stale — uses the OLD linear map.**
   `models/buzzer_activate.py:12-17` computes
   `idx = round(1 + (hz-400)·254/(12000-400))`, a **linear** 400–12000 Hz →
   index 1–255 mapping. The firmware (PR #98) uses the **quarter-tone
   exponential** table. They disagree badly: `hz_to_index(440)` returns **2**,
   which the firmware octave-folds to idx 122 = **A♯4 ≈ 466 Hz** (a semitone
   sharp), not A4. Any host that selects a pitch via `single_tone`/`hz_to_index`
   (including the Home Assistant `activate_buzzer` service) produces the wrong
   note. The correct inverse is
   `idx = clamp(round(24·log2(hz/13.75)), 1, 255)`. Raw-index frames
   (§7.1–7.6) are unaffected. The model's own unit tests only check the linear
   helper's internal consistency, so they pass despite the mismatch.

2. **`structs.h:209` comment is stale.** It still says
   "`freq_idx` 1–255 maps **linearly** to a firmware-defined Hz range." Post-PR
   #98 the mapping is the quarter-tone exponential table, not linear.

3. **Cap comments say "5 s"; the constant is 30 s.** `buzzer_control.cpp:144` and
   `:167` comments read "5 s total cap," left over from commit `83d1ebd`. The
   actual value is `kBuzzerMaxTotalMs = 30000` (30 s), `:17`.

4. **`AUDIT_FIRMWARE_2026-07-13.md` (L4) and `FINDINGS.md` (H9) describe the
   pre-PR-98 code.** They flag a blocking `buzzer_drive_tone_sw` /
   `delayMicroseconds` bit-bang that busy-waits up to ~5 s and starves the loop.
   That code no longer exists — PR #98 replaced it with the hardware-PWM +
   non-blocking state machine, and raised the cap to 30 s. Those findings are
   **resolved/superseded** by the shipped implementation.

5. **Response length: header says 2 bytes, firmware sends 4.**
   `opendisplay_protocol.h:444-445` documents the response as `[0x00][0x77]` /
   `[0xFF][0x77]`. The firmware always emits a 4-byte frame
   `[status][0x77][code][0x00]` (§3.4). Clients that need the granular error code
   must read all four bytes; `validate_ack_response` only inspects the first two.

6. **Firmware `docs/buzzer-protocol.md` and `docs/buzzer-music-implementation.md`
   are current and accurate** (updated by PR #98). Their content matches the
   source and was used as corroboration here; they simply carry less byte-level
   and cross-repo detail than this reference.

---

*Sources verified against: Firmware `src/buzzer_control.{cpp,h}`,
`src/buzzer_hw.{cpp,h}`, `src/structs.h`, `src/communication.cpp`,
`src/config_parser.cpp`, `src/main.cpp`, `src/device_control.cpp`,
`src/display_service.cpp` (branch `main`, PR #98 / commit `5ed21a9`; shaping
commits `83d1ebd`, `4d5a9a9`); opendisplay-protocol `src/opendisplay_protocol.h`;
py-opendisplay `models/buzzer_activate.py`, `protocol/commands.py`,
`protocol/responses.py`, `protocol/config_serializer.py`, `device.py`, and the
buzzer unit tests; Home_Assistant_Integration `custom_components/opendisplay/services.py`.*
