# Implementation Plan — 0x0052 Deep-Sleep Duration Extension

*Report-only planning document. 2026-07-17. Scope restricted to four repos/branches:
`Firmware/main`, `opendisplay-protocol/main`, `Home_Assistant_Integration/feat/clean-port`,
`py-opendisplay/main`. Companion contract:
[deep-sleep-duration-0x52-contract.md](deep-sleep-duration-0x52-contract.md). Prior design
background (dated, LE-leaning — superseded on endianness by shipped firmware):
[../agents/Home_Assistant_Integration/DEEP_SLEEP_EXTENSIONS_FINDINGS_2026-07-07.md](../agents/Home_Assistant_Integration/DEEP_SLEEP_EXTENSIONS_FINDINGS_2026-07-07.md)
Part 3.*

---

## 0. TL;DR — the extension is half-shipped, and a 2026-07-18 revision adds a behavior split

The original feature is *"extend `CMD_DEEP_SLEEP` (0x0052) with an optional `uint16` seconds payload;
empty payload or `0` = sleep now at the configured cadence."*

**Revision 2026-07-18 — split "sleep" from "power off":**
- **0x0052 on power-latch (D-FF) hardware** changes from *hard power-off* to *timer-wake deep sleep*,
  making it identical to non-latch battery. **Hardware feasibility RESOLVED** (2026-07-18 schematic review):
  the firmware already holds the FF's D across deep sleep and a D-FF retains Q; the Seeed reTerminal E1001/2/3
  have no D-FF at all (always-on rail) so 0x0052 is already timer-sleep there. See §1A.
- **New `0x0053 CMD_POWER_OFF`** carries the hard rail-cut: latch HW → ACK then power off (button-only
  wake); every non-latch target → `FF 53 00` unsupported. Optional payload reserved/ignored.

This makes the firmware stage **no longer "done"** — see the table and §1A.

| Stage | Repo / branch | State | Work required |
|---|---|---|---|
| **Firmware** | `Firmware/main` | ⚠ **New work (rev 2026-07-18)** — 0x0052 *duration payload* shipped in PR #97 (`d974f9d`), but this revision **changes 0x0052 latch behavior** (power-off → timer-wake sleep) and **adds 0x0053 `CMD_POWER_OFF`**; neither is shipped | Add 0x0053 handler; change the latch branch of `handleDeepSleepCommand`; **verify the D-FF latch holds across deep sleep** (§1A). Plus optional floor clamp (§5). |
| **Protocol contract** | `opendisplay-protocol/main` | ⚠️ **Partial** — header documents `[seconds:2 BE]` but `@errors: none` and omits the NACK codes / one-shot / clamp semantics the firmware actually emits | Update the 0x0052 block; changelog + version decision (§3) |
| **Sending lib** | `py-opendisplay/main` | ❌ **Not done** — `build_deep_sleep_command()` sends bare `0x0052` only; no `duration_s`; and it **mis-maps** the new `FF 52 01/02` NACKs to "unsupported" | Add duration param; fix NACK interpretation (§2) |
| **Consumer** | `Home_Assistant_Integration/feat/clean-port` | ❌ **Not done** — has the deep-sleep *availability* model (`sleep.py`, `delivery.py`) but never *sends* 0x0052 and has no duration hint | Optional: `sleep_after` hint + availability-horizon fix (§4) |

**The single most important correctness fact for the sending side:** the shipped firmware parses the
payload **big-endian** (`payload[0] << 8 | payload[1]`, `Firmware/src/device_control.cpp:713`), and
the canonical header agrees (`[seconds:2 BE]`). The 2026-07-07 findings doc argued for little-endian;
**that recommendation was not followed and is now wrong.** All sending-side code must pack big-endian.

---

## 1. Ground truth — what the firmware actually does (verified on `Firmware/main`)

Handler: `handleDeepSleepCommand(const uint8_t* payload, uint16_t payloadLen)`
(`src/device_control.cpp:705`), dispatched with the payload sliced off the opcode
(`src/communication.cpp:660-661`: `handleDeepSleepCommand(data + 2, len - 2)`).
Override consumed in `enterDeepSleep(bool force, uint16_t overrideSleepSeconds)`
(`src/main.cpp:468`, arm at `:515-519`).

### 1.1 Payload parse (ESP32 only)
```
payloadLen >= 2 : overrideSeconds = (payload[0] << 8) | payload[1]   // BIG-ENDIAN; extra bytes ignored
payloadLen == 1 : warn "malformed", treat as NO override
payloadLen == 0 : NO override
overrideSeconds == 0x0000 : explicit "no override" -> falls back to configured cadence
```
The override is a **plain function argument**, never persisted — one-shot by construction. On the next
wake the device reverts to `globalConfig.power_option.deep_sleep_time_seconds`
(`src/main.cpp:515-517`). No `RTC_DATA_ATTR`, no flash write.

### 1.2 Response / rejection matrix (as shipped)
| Precondition (checked in order) | Wire response | Then |
|---|---|---|
| Power-latch D-FF configured (`powerLatchDffConfigured()`) | `00 52 00 00` (ACK) | Duration **ignored** (logged); `powerLatchPowerOff()` after ~100 ms. Physical button is the only wake. |
| Not battery powered (`power_mode != 1`) | `FF 52 02 00` (NACK) | Device **stays awake**. |
| Deep sleep disabled (`deep_sleep_time_seconds == 0`) | `FF 52 01 00` (NACK) | Device **stays awake**. |
| Otherwise (battery, no latch) | **No frame** | `enterDeepSleep(true, overrideSeconds)`; BLE torn down, link drops. |
| nRF / non-ESP32 target | **No frame** | Logged "not supported" only. |

Notes that shape the contract and the sending side:
- **The happy path sends no ACK.** A battery, non-latch device sleeps synchronously; the client sees
  a disconnect. This is fine — the library already treats a dropped link as success — but it means the
  duration is **not in-band confirmable** on this target. Discovery is via firmware version only.
- **The NACK codes are semantically "rejected, still awake / retryable", not "unsupported".**
  `FF 52 01` (config-disabled) and `FF 52 02` (mains-powered) both mean the device is reachable and did
  *not* sleep. This is the crux of the py-opendisplay fix in §2.
- **No floor clamp.** A duration of `1`–`9` s is honored verbatim → a wake storm. Firmware relies on the
  caller for a sane floor today (§5 proposes adding one).
- **Ceiling is the wire width:** `uint16` → max `65535` s ≈ **18.2 h**, identical to the range already
  reachable via the config field, so a buggy caller can never exceed what config already permits.

### 1.3 Wording reconciliation (flag for the requester)
The request says *"an empty payload, or a payload of 0 should indicate immediate deep sleep."* The shipped
firmware reads that as: **enter deep sleep immediately, waking at the configured `deep_sleep_time_seconds`
cadence** — i.e. "immediate" refers to *entering* sleep now, and the `uint16` is the *wake timer*; empty/0
means "use the configured duration." This is the natural reading and is already live. If the intent was
instead "0 = sleep indefinitely / until button," the contract changes materially — **confirm before any
sending-side code treats 0 specially.** This plan assumes the shipped semantics.

---

## 1A. Firmware work required by the 2026-07-18 revision (0x0053 + 0x0052 latch change)

§1 above is the **shipped** ground truth. This revision changes it. All work is on `Firmware/main`,
`src/device_control.cpp` (`handleDeepSleepCommand`) and `src/main.cpp` (`enterDeepSleep`).

### 1A.1 Change the 0x0052 latch branch: power-off → timer-wake sleep
Today `handleDeepSleepCommand` checks `powerLatchDffConfigured()` first and, if set, ACKs `00 52 00 00`
and calls `powerLatchPowerOff()` — the duration is ignored and the device wakes only on a button. **Remove
that early power-off branch from 0x0052.** After the change, latch hardware falls through to the same path
as non-latch: `enterDeepSleep(true, overrideSeconds)`, which arms the timer (and button) wake and calls
`esp_deep_sleep_start()`. Net: latch HW now honors the duration and wakes on the timer, identical to
non-latch.

**Hardware feasibility — RESOLVED (2026-07-18 schematic review).** The existing sleep path already calls
`powerLatchHoldForSleep()` and `armButtonWakeSources()` before sleeping:
1. `powerLatchHoldForSleep()` sets the FF's D pin high and latches it through deep sleep via
   `gpio_deep_sleep_hold_en()` + `gpio_hold_en()`, so the rail stays powered and the RTC timer fires; and
2. a **74AHC1G79 D flip-flop retains Q** (the power-enable) through deep sleep by nature — nothing clocks it
   low while asleep — so the timer wake brings the device back with the latch still engaged (not a cold boot).

Independent confirmation from the Seeed **reTerminal E1001/E1002/E1003** schematics: those boards have **no
D-FF latch** at all — a mechanical slide switch (MK-22D18G3) feeds an SY6974B power-path and an always-on
TPS631000 rail (EN tied high via R125), so **no GPIO gates the rail** and 0x0052 is already a plain
timer-sleep there (the `powerLatchDffConfigured()` branch never fires). All three are ESP32-S3, so the
ESP32-C6 `gpio_deep_sleep_hold_en()` skip is N/A.

**Sole residual risk:** if firmware ever commands **SY6974B I²C ship mode** (battery disconnect, QON-button
revive) the rail drops for real — but plain `esp_deep_sleep_start()` never does this. A human should still
open **sheet 4 "03 Power"** of the E-series schematics to confirm /QON and /CE routing before relying on any
ship-mode behavior. The reTerminal **Sticky** power path is **Unclear** — no public schematic exists yet
(product still being announced); re-check when Seeed publishes it.

### 1A.2 Add the 0x0053 `CMD_POWER_OFF` handler
New opcode dispatch in `communication.cpp` (`case CMD_POWER_OFF: handlePowerOffCommand(data + 2, len - 2)`)
and a handler that mirrors the *old* 0x0052 latch branch:
- `powerLatchDffConfigured()` → ACK `00 53 00 00`, `delay(100)`, `powerLatchPowerOff()`. (Same best-effort
  ACK caveat as before — it usually dies in the buffer; that's fine under fire-and-forget.)
- otherwise (non-latch battery, mains, and the non-ESP32 `#else`) → NACK `FF 53 00 00`
  (`OD_ERR_POWER_OFF_UNSUPPORTED`); device stays awake.
- Payload is reserved/ignored (`(void)payload;`).

### 1A.3 Response matrix — target (after the revision)
| Command / precondition | Wire response | Then |
|---|---|---|
| **0x0052**, power-latch D-FF (after 1A.1) | **No frame** | `enterDeepSleep(true, overrideSeconds)`; timer/button wake; latch **held** (feasibility resolved, §1A.1). |
| **0x0052**, battery no latch | **No frame** | unchanged — timer-wake sleep. |
| **0x0052**, mains / disabled | `FF 52 02` / `FF 52 01` | unchanged — stays awake. |
| **0x0053**, power-latch D-FF | `00 53 00 00` (ACK) | `powerLatchPowerOff()` ~100 ms; button-only wake. |
| **0x0053**, any non-latch target | `FF 53 00 00` (NACK) | stays awake (`OD_ERR_POWER_OFF_UNSUPPORTED`). |

---

## 2. Sending side — `py-opendisplay/main` (the real work)

Files: `src/opendisplay/protocol/commands.py`, `src/opendisplay/device.py`.

### 2.1 `build_deep_sleep_command(duration_s: int | None = None)` (commands.py:122)
Extend the existing builder (currently `CommandCode.DEEP_SLEEP.to_bytes(2, "big")`, no payload):
```python
def build_deep_sleep_command(duration_s: int | None = None) -> bytes:
    cmd = CommandCode.DEEP_SLEEP.to_bytes(2, "big")          # 0x0052 opcode, BE (unchanged)
    if duration_s is None:
        return cmd                                           # bare command == today's behavior
    if not (MIN_SLEEP_S <= duration_s <= 0xFFFF):            # MIN_SLEEP_S: see §5 (recommend 10)
        raise ValueError(f"duration_s out of range: {duration_s} ({MIN_SLEEP_S}-65535)")
    return cmd + struct.pack(">H", duration_s)               # BIG-ENDIAN — matches firmware + header
```
**`>H`, not `<H`.** This is the one line that the stale findings doc would get wrong.

### 2.2 `deep_sleep(duration_s: int | None = None)` (device.py:1068)
Add the parameter, keep the method **fire-and-forget**, and **fix the NACK interpretation.**

**Acknowledgement model (contract §2.4):** ACK/NACK are optional/best-effort — the sender **MUST NOT**
expect or block on them. A normal return means **"delivered,"** never a verified "slept." This is what
removes the false-success race: because the method never claims "confirmed slept," a dropped NACK degrades
to a correct "delivered" instead of an incorrect "slept."

Today the method treats *any* `0xFF52` frame as "not supported" (`device.py:1122-1125`) and raises
`ProtocolError`. That is wrong on two counts — it mislabels a refusal as "unsupported," and by racing a
short timeout against a real NACK it can mask a refusal as false success. With the new firmware:
- `FF 52 01` → deep sleep disabled in device config → `DeepSleepRejectedError(reason="disabled")`.
- `FF 52 02` → device is mains-powered (`power_mode != 1`) → `DeepSleepRejectedError(reason="not_battery")`.
- `FF 52 00` (or bare `FF 52`) → `DeepSleepUnsupportedError` (nRF-style).

Map 01/02 to a distinct, **non-retry** typed error carrying the reason and the fact that the link is still
up — callers must not blindly retry expecting success. Do at most **one bounded best-effort read** to catch
a NACK; treat write/read failure, timeout, or clean disconnect as the normal "delivered" outcome (the
battery non-latch path sends no frame by design). Never extend the wait or retry because an ACK is missing.

### 2.3 Capability gating
`duration_s` is honored only on ESP32 firmware at/after the version that shipped PR #97. Silabs ACKs and
ignores the payload (EM4 has no timer wake armed); nRF is unsupported. Gate on the already-cached firmware
version (`read_firmware_version()` / `device.py`), and when a duration is requested against a target that
cannot honor it, either strip it with a warning or raise, controlled by a `strict` flag. This is the only
in-band way to know the duration took effect, because the happy path is silent.

### 2.4 New `0x0053 CMD_POWER_OFF` support (revision)
Add alongside the deep-sleep work:
- `commands.py`: `CommandCode.POWER_OFF = 0x0053` and `build_power_off_command() -> bytes` returning
  `CommandCode.POWER_OFF.to_bytes(2, "big")` (no payload — reserved).
- `device.py`: `async def power_off(self) -> None` — **fire-and-forget**, structurally a copy of
  `deep_sleep()` (write, tolerate disconnect/timeout as "delivered"), but with **no** `duration_s`. In the
  bounded best-effort read, decode `FF 53 00` → raise a new `PowerOffUnsupportedError` (target has no
  latch); disconnect/timeout/ACK → return "delivered".
- `exceptions.py`: add `PowerOffUnsupportedError`. Also add the `DeepSleepRejectedError` /
  `DeepSleepUnsupportedError` types from §2.2.
- Capability gating: `power_off()` is meaningful only on ESP32 power-latch hardware; on other targets it
  returns the `FF 53 00` NACK by design, so no version gate is needed — the device self-reports.

### 2.5 CLI
`opendisplay sleep <device> [--for SECONDS]` (extras `[cli]`). `--for` omitted → bare command.
`opendisplay power-off <device>` → `power_off()` — a **separate subcommand**, never a flag on `sleep`, so
"low power" and "hard off" cannot be confused.

### 2.6 Tests
Unit: builder emits `00 52` (no arg) and `00 52 HH LL` **big-endian** for a duration; `ValueError` below
floor / above `0xFFFF`. `build_power_off_command()` emits `00 53`. Method: `FF 52 01`/`FF 52 02` raise the
new rejected-error (link stays up), `FF 52 00` raises unsupported, disconnect/timeout = success;
`FF 53 00` → `PowerOffUnsupportedError`, disconnect/timeout on `power_off()` = success. Add to
`tests/unit/test_models_new_packets.py` / a `test_device_*` companion.

---

## 3. Protocol contract — `opendisplay-protocol/main`

Canonical header `src/opendisplay_protocol.h`, `CMD_DEEP_SLEEP` block at lines **350-362**.
Today it documents the optional `[seconds:2 BE]` payload in `@request` but says `@errors: none` and does
not state the one-shot semantics, the `0`/empty fallback, the clamp, or the `FF 52 01/02` NACK namespace.

**Do NOT hand-edit the vendored firmware copies.** Edit the canonical file only, then propagate:
```
cd opendisplay-protocol
tools/sync_protocol_header.py --push     # canonical -> all firmware copies
tools/sync_protocol_header.py --check    # CI/pre-commit drift gate
```
The full replacement `@request`/`@response`/`@errors`/`@state` text is in the contract doc §"Canonical
header block" — drop it into the 0x0052 block verbatim, **and add the new 0x0053 `CMD_POWER_OFF` block**
(also in the contract §3). Update the 0x0052 `@response` latch line to the timer-wake-sleep / pending-HW
wording, not the old power-off wording.

**Also mirror into the Python constant source** `src/opendisplay_protocol.py` via
`tools/gen_python_protocol.py` (the header is macro-only precisely so this generator works — keep any new
error-code `#define`s to simple literals).

### Version-policy decision (needs an owner's call)
Per the header's own VERSIONING POLICY, documenting an optional length-discriminated payload, a new
per-opcode NACK error namespace, **and a brand-new opcode (0x0053)** is a **backward-compatible addition →
MINOR bump (2.0 → 2.1)**. The one non-additive wrinkle is the **0x0052 latch *behavior* change**
(power-off → sleep) — the wire framing is unchanged, but the on-device effect on latch HW differs; call it
out explicitly in the changelog so integrators with latch hardware notice. Caveat: the
`[seconds:2 BE]` text is *already* present in the 2.0 header without a recorded bump, so part of this is
"catch the changelog up to shipped reality." Recommended: land the full 0x0052 contract (payload + errors +
semantics) as the **2.1** entry, set `LAST CHANGED`, and record it under a new `2.1 (YYYY-MM-DD)` heading.
Confirm with the protocol owner rather than bumping unilaterally.

---

## 4. Consumer — `Home_Assistant_Integration/feat/clean-port` (optional, follow-on)

The clean-port branch already models deep-sleep *availability* (`sleep.py` `SleepProfile`,
`delivery.py` `DeliveryManager`) but **never sends 0x0052** and has no duration hint. Wiring the duration
in is a genuine battery lever (adaptive cadence ≈ 2× life for hourly-fresh content — findings Part 3 §Use
cases) but is **not required** for the extension itself. If pursued, keep it minimal and opt-in:

1. **`sleep_after` / `next_update_in` hint** on the upload services only. Store it with the pending
   upload; after a successful drain, over the still-open connection, call
   `device.deep_sleep(min(hint, cap))`. Never automatic, never when work is queued.
2. **Availability-horizon fix (required if #1 ships).** The fallback availability interval is derived from
   the *configured* cadence (`sleep.py:84`, `deep_sleep_time_seconds * missed_cycles`). A commanded longer
   sleep would flap entities unavailable — when a duration is commanded, raise the horizon to
   `max(configured, commanded) * missed_cycles` for that cycle.
3. **Latch hardware:** after the revision (§1A), 0x0052 *sleeps* on latch HW rather than powering off (HW
   feasibility resolved, §1A.1), so a `sleep_after` hint is safe there behind the usual firmware-version
   gate. reTerminal E1001/2/3 have no latch and already timer-sleep. Never send when a re-auth is pending.
   A deliberate hard-off from HA (if ever wanted) uses `power_off()` (0x0053), not the sleep hint.
4. **Caps:** for automatic sends, `X <= min(configured_cadence * 8, 12 h)`; a manual service call may use
   the full range with a confirmation-worthy `services.yaml` description.

Defer unless there is demand; §2 (py-opendisplay) is the load-bearing deliverable.

---

## 5. Recommended firmware hardening (optional, separate small PR)

The **required** firmware work is in §1A (0x0053 handler + 0x0052 latch change; HW feasibility resolved). The
items here are optional hardening on top of that:
- **No deferred-ACK fix needed (explicit non-goal).** Under the fire-and-forget acknowledgement model
  (contract §2.4), the sender never expects an ACK, so the **power-off ACK** that is queued-but-not-sent
  before the rail is cut — now on **0x0053** (`sendResponse` enqueues; drained only on a later `loop()`
  pass; power is cut first) — is **acceptable, not a bug**. Do **not** adopt the
  Silabs deferred-sleep pattern on ESP32 to "make the ACK reliable" — it would add complexity to deliver a
  frame the contract says no one may depend on. The NACK paths already transmit reliably because the
  handler returns to the loop; that is sufficient.
- **Floor clamp.** `handleDeepSleepCommand` honors any `overrideSeconds >= 1`. Clamp
  `0 < X < MIN_SLEEP_S` up to `MIN_SLEEP_S` (≈10 s) to prevent a wake storm from a buggy client
  (`X = 1 s` → ~86 400 wakes/day). Keep the sending-side floor (§2.1) as defense in depth.
- **Button wake as a safety net.** PR #97 already added button wake (`armButtonWakeSources()`,
  `main.cpp:520`), so a mistakenly-long commanded sleep degrades to "press the button" rather than a hard
  blackout — the residual-risk mitigation the findings doc asked for is effectively in place. Verify it is
  armed on the override path.
- **Silabs parity (separate repo, out of the 4-repo scope but note it):** Silabs ACKs and ignores the
  payload (EM4, no BURTC timer). A one-line `FF 52 02` on a duration payload would stop it silently
  misleading a duration-aware client. Track separately; not part of this plan's scope.

---

## 6. Sequencing

0. **HW feasibility — DONE (§1A.1).** Resolved via 2026-07-18 schematic review + firmware analysis: D-FF
   holds across deep sleep (fw already does it; a D-FF retains Q), and reTerminal E1001/2/3 have no latch at
   all. No bench gate remains for the latch-sleep change. (Optional: a human confirms SY6974B /QON routing on
   the E-series power sheet before relying on ship mode.)
1. **Firmware revision (§1A)** — 0x0053 handler + 0x0052 latch branch change. This is now real firmware
   work, not "done."
2. **Protocol contract (§3)** — document the target behavior (incl. 0x0053 + the latch change) so the spec
   is the source of truth the library is written against. MINOR bump decision resolved with the owner.
3. **py-opendisplay (§2)** — builder + method + NACK-interpretation fix + `build_power_off_command()` /
   `power_off()` + capability gating + tests. The critical path for making the firmware usable.
4. **Firmware floor clamp (§5)** — optional, parallel, independent.
5. **HA hint (§4)** — optional follow-on once #3 is released; depends on the availability-horizon fix.

## 7. Cross-repo invariants / gotchas checklist
- [ ] **Big-endian everywhere** (`>H`). The findings doc's LE recommendation is superseded.
- [ ] **Fire-and-forget:** sender never expects/blocks on ACK/NACK; a normal return = "delivered", not
      "confirmed slept". ACK/NACK are optional/best-effort; a missing response is never failure or a retry.
- [ ] py-opendisplay `FF 52 01/02` = *rejected & awake* (surfaced if opportunistically read), **not**
      *unsupported*; caller must not blind-retry.
- [ ] `0x0000` / empty payload = configured cadence, one-shot (never persisted).
- [ ] Happy path (battery, no latch) is **silent** — disconnect = "delivered"; whether the device *slept*
      is not wire-confirmable (duration takes effect confirmable only via firmware version).
- [ ] **Latch HW 0x0052 = timer-wake sleep** (revision) — honors the duration, wakes on timer/button. HW
      feasibility **resolved** (§1A.1): D-FF holds Q across deep sleep; reTerminal E1001/2/3 have no latch
      (always-on rail). Residual: don't command SY6974B I²C ship mode expecting a timer wake.
- [ ] **0x0053 `CMD_POWER_OFF`** added: latch HW → ACK + rail cut (button-only wake); every non-latch
      target → `FF 53 00` unsupported. Fire-and-forget; payload reserved/ignored; `power_off()` has no
      `duration_s`; `FF 53 00` → `PowerOffUnsupportedError`; distinct CLI subcommand.
- [ ] `uint16` ceiling (18.2 h) is the safety cap; add a ≥10 s floor in lib (and ideally firmware).
- [ ] Edit only the canonical protocol header (0x0052 latch note **and** new 0x0053 block); `--push` then
      `--check`; regenerate the `.py` mirror.
