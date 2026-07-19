# Implementation Plan — Deep-Sleep Duration Extension (`CMD_DEEP_SLEEP`, 0x0053)

*Report-only planning document. 2026-07-17. Scope restricted to four repos/branches:
`Firmware/main`, `opendisplay-protocol/main`, `Home_Assistant_Integration/feat/clean-port`,
`py-opendisplay/main`. Companion contract:
[deep-sleep-duration-0x53-contract.md](deep-sleep-duration-0x53-contract.md). Prior design
background (dated, LE-leaning — superseded on endianness by shipped firmware):
[../agents/Home_Assistant_Integration/DEEP_SLEEP_EXTENSIONS_FINDINGS_2026-07-07.md](../agents/Home_Assistant_Integration/DEEP_SLEEP_EXTENSIONS_FINDINGS_2026-07-07.md)
Part 3.*

> **Opcode assignment (updated — merged protocol 2.1).** The current mapping is
> **`0x0052` = `CMD_POWER_OFF`** (`RESP_POWER_OFF` `0x52`) and **`0x0053` = `CMD_DEEP_SLEEP`**
> (`RESP_DEEP_SLEEP` `0x53`). `CMD_DEEP_SLEEP` **moved `0x0052` → `0x0053`** — swapped with
> power-off so that power-off takes the lower number. This is **BREAKING vs. shipped firmware**:
> `0x0052` was `CMD_DEEP_SLEEP` in Firmware PR #97, so a peer still on the old mapping now hits the
> wrong command. The swap was amended in place within the (unreleased) 2.1 line, not a MAJOR bump.
> Everything else about each command (payloads, response model, error codes, per-target behavior)
> is unchanged — only the opcode numbers moved. Wire frames in this document are written in the
> **current 2.1 numbering** unless explicitly noted.

---

## 0. TL;DR — the extension is half-shipped, and a 2026-07-18 revision adds a behavior split

The original feature is *"extend `CMD_DEEP_SLEEP` (now opcode 0x0053; 0x0052 at the time of PR #97)
with an optional `uint16` seconds payload; empty payload or `0` = sleep now at the configured
cadence."*

**Revision 2026-07-18 — split "sleep" from "power off":**
- **`CMD_DEEP_SLEEP` (0x0053) on power-latch (D-FF) hardware** changes from *hard power-off* to
  *timer-wake deep sleep*, making it identical to non-latch battery. **Hardware feasibility RESOLVED**
  (2026-07-18 schematic review): the firmware already holds the FF's D across deep sleep and a D-FF retains
  Q; the Seeed reTerminal E1001/2/3 have no D-FF at all (always-on rail) so deep sleep is already
  timer-sleep there. See §1A.
- **New `CMD_POWER_OFF` (0x0052)** carries the hard rail-cut: latch HW → ACK then power off (button-only
  wake); every non-latch target → `FF 52 00` unsupported. Optional payload reserved/ignored.

This makes the firmware stage **no longer "done"** — see the table and §1A.

| Stage | Repo / branch | State | Work required |
|---|---|---|---|
| **Firmware** | `Firmware/main` | ⚠ **New work (rev 2026-07-18)** — deep-sleep *duration payload* shipped in PR #97 (`d974f9d`, on the pre-swap opcode 0x0052), but this revision **changes the deep-sleep latch behavior** (power-off → timer-wake sleep), **adds `CMD_POWER_OFF` (0x0052)**, and requires **migrating to the 2.1 swapped opcodes**; none of that is shipped | Move deep-sleep dispatch to 0x0053; add the 0x0052 power-off handler; change the latch branch of `handleDeepSleepCommand`; **verify the D-FF latch holds across deep sleep** (§1A). Plus optional floor clamp (§5). |
| **Protocol contract** | `opendisplay-protocol/main` | ✅ **Done — merged as protocol 2.1** — full `@request`/`@response`/`@errors` blocks for both opcodes, incl. the `0x0052`↔`0x0053` swap; `OD_ERR_*` macros in header SECTION 4c/4d | None (§3) |
| **Sending lib** | `py-opendisplay/main` | ❌ **Not done** — `build_deep_sleep_command()` sends the bare pre-swap opcode only; no `duration_s`; it **mis-maps** the deep-sleep NACKs to "unsupported"; and it must adopt the 2.1 opcode swap | Add duration param; adopt 0x0053/0x0052 mapping; fix NACK interpretation (§2) |
| **Consumer** | `Home_Assistant_Integration/feat/clean-port` | ❌ **Not done** — has the deep-sleep *availability* model (`sleep.py`, `delivery.py`) but never *sends* the deep-sleep command and has no duration hint | Optional: `sleep_after` hint + availability-horizon fix (§4) |

**The single most important correctness fact for the sending side:** the shipped firmware parses the
payload **big-endian** (`payload[0] << 8 | payload[1]`, `Firmware/src/device_control.cpp:713`), and
the canonical header agrees (`[seconds:2 BE]`). The 2026-07-07 findings doc argued for little-endian;
**that recommendation was not followed and is now wrong.** All sending-side code must pack big-endian.

---

## 1. Ground truth — what the firmware actually does (verified on `Firmware/main`)

> **Numbering note:** the shipped firmware analyzed here predates the 2.1 opcode swap — it dispatches
> deep sleep on **0x0052** and echoes `0x52` in its responses. The frames below are shown in the
> **current 2.1 numbering** (deep-sleep echo `0x53`); on the shipped firmware the echoed byte is `0x52`.
> Migrating the firmware to the swapped opcodes is part of §1A.

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

### 1.2 Response / rejection matrix (as shipped; frames in 2.1 numbering — see the note above)
| Precondition (checked in order) | Wire response | Then |
|---|---|---|
| Power-latch D-FF configured (`powerLatchDffConfigured()`) | `00 53 00 00` (ACK) | Duration **ignored** (logged); `powerLatchPowerOff()` after ~100 ms. Physical button is the only wake. |
| Not battery powered (`power_mode != 1`) | `FF 53 02 00` (NACK) | Device **stays awake**. |
| Deep sleep disabled (`deep_sleep_time_seconds == 0`) | `FF 53 01 00` (NACK) | Device **stays awake**. |
| Otherwise (battery, no latch) | **No frame** | `enterDeepSleep(true, overrideSeconds)`; BLE torn down, link drops. |
| nRF / non-ESP32 target | **No frame** | Logged "not supported" only. |

Notes that shape the contract and the sending side:
- **The happy path sends no ACK.** A battery, non-latch device sleeps synchronously; the client sees
  a disconnect. This is fine — the library already treats a dropped link as success — but it means the
  duration is **not in-band confirmable** on this target. Discovery is via firmware version only.
- **The NACK codes are semantically "rejected, still awake / retryable", not "unsupported".**
  `FF 53 01` (config-disabled) and `FF 53 02` (mains-powered) both mean the device is reachable and did
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

## 1A. Firmware work required by the 2026-07-18 revision (0x0052 power-off + deep-sleep latch change + opcode swap)

§1 above is the **shipped** ground truth. This revision changes it. All work is on `Firmware/main`,
`src/device_control.cpp` (`handleDeepSleepCommand`) and `src/main.cpp` (`enterDeepSleep`).

### 1A.1 Change the deep-sleep (0x0053) latch branch: power-off → timer-wake sleep
Today `handleDeepSleepCommand` checks `powerLatchDffConfigured()` first and, if set, ACKs (`00 53 00 00`
in 2.1 numbering) and calls `powerLatchPowerOff()` — the duration is ignored and the device wakes only on
a button. **Remove that early power-off branch from the deep-sleep handler.** After the change, latch
hardware falls through to the same path as non-latch: `enterDeepSleep(true, overrideSeconds)`, which arms
the timer (and button) wake and calls `esp_deep_sleep_start()`. Net: latch HW now honors the duration and
wakes on the timer, identical to non-latch.

**Hardware feasibility — RESOLVED (2026-07-18 schematic review).** The existing sleep path already calls
`powerLatchHoldForSleep()` and `armButtonWakeSources()` before sleeping:
1. `powerLatchHoldForSleep()` sets the FF's D pin high and latches it through deep sleep via
   `gpio_deep_sleep_hold_en()` + `gpio_hold_en()`, so the rail stays powered and the RTC timer fires; and
2. a **74AHC1G79 D flip-flop retains Q** (the power-enable) through deep sleep by nature — nothing clocks it
   low while asleep — so the timer wake brings the device back with the latch still engaged (not a cold boot).

Independent confirmation from the Seeed **reTerminal E1001/E1002/E1003** schematics: those boards have **no
D-FF latch** at all — a mechanical slide switch (MK-22D18G3) feeds an SY6974B power-path and an always-on
TPS631000 rail (EN tied high via R125), so **no GPIO gates the rail** and deep sleep is already a plain
timer-sleep there (the `powerLatchDffConfigured()` branch never fires). All three are ESP32-S3, so the
ESP32-C6 `gpio_deep_sleep_hold_en()` skip is N/A.

**Sole residual risk:** if firmware ever commands **SY6974B I²C ship mode** (battery disconnect, QON-button
revive) the rail drops for real — but plain `esp_deep_sleep_start()` never does this. A human should still
open **sheet 4 "03 Power"** of the E-series schematics to confirm /QON and /CE routing before relying on any
ship-mode behavior. The reTerminal **Sticky** power path is **Unclear** — no public schematic exists yet
(product still being announced); re-check when Seeed publishes it.

### 1A.2 Add the 0x0052 `CMD_POWER_OFF` handler (and adopt the swapped opcodes)
New opcode dispatch in `communication.cpp` (`case CMD_POWER_OFF: handlePowerOffCommand(data + 2, len - 2)`)
and a handler that mirrors the *old* deep-sleep latch branch:
- `powerLatchDffConfigured()` → ACK `00 52 00 00`, `delay(100)`, `powerLatchPowerOff()`. (Same best-effort
  ACK caveat as before — it usually dies in the buffer; that's fine under fire-and-forget.)
- otherwise (non-latch battery, mains, and the non-ESP32 `#else`) → NACK `FF 52 00 00`
  (`OD_ERR_POWER_OFF_UNSUPPORTED`); device stays awake.
- Payload is reserved/ignored (`(void)payload;`).

Per the 2.1 opcode swap, the **deep-sleep dispatch moves to `0x0053`** in the same change (adopt the
vendored 2.1 header's `CMD_DEEP_SLEEP`/`CMD_POWER_OFF` values rather than local literals) — this is
breaking vs. old-mapping peers; see the opcode-assignment note at the top.

### 1A.3 Response matrix — target (after the revision)
| Command / precondition | Wire response | Then |
|---|---|---|
| **0x0053 deep sleep**, power-latch D-FF (after 1A.1) | **No frame** | `enterDeepSleep(true, overrideSeconds)`; timer/button wake; latch **held** (feasibility resolved, §1A.1). |
| **0x0053 deep sleep**, battery no latch | **No frame** | unchanged — timer-wake sleep. |
| **0x0053 deep sleep**, mains / disabled | `FF 53 02` / `FF 53 01` | unchanged — stays awake. |
| **0x0052 power-off**, power-latch D-FF | `00 52 00 00` (ACK) | `powerLatchPowerOff()` ~100 ms; button-only wake. |
| **0x0052 power-off**, any non-latch target | `FF 52 00 00` (NACK) | stays awake (`OD_ERR_POWER_OFF_UNSUPPORTED`). |

---

## 2. Sending side — `py-opendisplay/main` (the real work)

Files: `src/opendisplay/protocol/commands.py`, `src/opendisplay/device.py`.

### 2.1 `build_deep_sleep_command(duration_s: int | None = None)` (commands.py:122)
Extend the existing builder (currently `CommandCode.DEEP_SLEEP.to_bytes(2, "big")`, no payload).
**`CommandCode.DEEP_SLEEP` must also be updated `0x0052` → `0x0053`** per the 2.1 swap:
```python
def build_deep_sleep_command(duration_s: int | None = None) -> bytes:
    cmd = CommandCode.DEEP_SLEEP.to_bytes(2, "big")          # 0x0053 opcode, BE (2.1 swap: was 0x0052)
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

Today the method treats *any* deep-sleep NACK frame as "not supported" (`device.py:1122-1125`) and raises
`ProtocolError`. That is wrong on two counts — it mislabels a refusal as "unsupported," and by racing a
short timeout against a real NACK it can mask a refusal as false success. With the new firmware:
- `FF 53 01` → deep sleep disabled in device config → `DeepSleepRejectedError(reason="disabled")`.
- `FF 53 02` → device is mains-powered (`power_mode != 1`) → `DeepSleepRejectedError(reason="not_battery")`.
- `FF 53 00` (or bare `FF 53`) → `DeepSleepUnsupportedError` (nRF-style).

Map 01/02 to a distinct, **non-retry** typed error carrying the reason and the fact that the link is still
up — callers must not blindly retry expecting success. Do at most **one bounded best-effort read** to catch
a NACK; treat write/read failure, timeout, or clean disconnect as the normal "delivered" outcome (the
battery non-latch path sends no frame by design). Never extend the wait or retry because an ACK is missing.

### 2.3 Capability gating
`duration_s` is honored only on ESP32 firmware at/after the version that shipped PR #97. Silabs ACKs and
ignores the payload (EM4 has no timer wake armed); nRF is unsupported. Gate on the already-cached firmware
version (`read_firmware_version()` / `device.py`), and when a duration is requested against a target that
cannot honor it, either strip it with a warning or raise, controlled by a `strict` flag. This is the only
in-band way to know the duration took effect, because the happy path is silent. (The same version gate
covers the 2.1 opcode swap — old-mapping firmware listens for deep sleep on `0x0052`, not `0x0053`.)

### 2.4 New `CMD_POWER_OFF` (0x0052) support (revision)
Add alongside the deep-sleep work:
- `commands.py`: `CommandCode.POWER_OFF = 0x0052` and `build_power_off_command() -> bytes` returning
  `CommandCode.POWER_OFF.to_bytes(2, "big")` (no payload — reserved).
- `device.py`: `async def power_off(self) -> None` — **fire-and-forget**, structurally a copy of
  `deep_sleep()` (write, tolerate disconnect/timeout as "delivered"), but with **no** `duration_s`. In the
  bounded best-effort read, decode `FF 52 00` → raise a new `PowerOffUnsupportedError` (target has no
  latch); disconnect/timeout/ACK → return "delivered".
- `exceptions.py`: add `PowerOffUnsupportedError`. Also add the `DeepSleepRejectedError` /
  `DeepSleepUnsupportedError` types from §2.2.
- Capability gating: `power_off()` is meaningful only on ESP32 power-latch hardware; on other targets it
  returns the `FF 52 00` NACK by design, so no version gate is needed — the device self-reports. (Caveat:
  a peer still on the pre-swap mapping interprets `0x0052` as deep sleep — the deep-sleep firmware-version
  gate is the guard.)

### 2.5 CLI
`opendisplay sleep <device> [--for SECONDS]` (extras `[cli]`). `--for` omitted → bare command.
`opendisplay power-off <device>` → `power_off()` — a **separate subcommand**, never a flag on `sleep`, so
"low power" and "hard off" cannot be confused.

### 2.6 Tests
Unit: builder emits `00 53` (no arg) and `00 53 HH LL` **big-endian** for a duration; `ValueError` below
floor / above `0xFFFF`. `build_power_off_command()` emits `00 52`. Method: `FF 53 01`/`FF 53 02` raise the
new rejected-error (link stays up), `FF 53 00` raises unsupported, disconnect/timeout = success;
`FF 52 00` → `PowerOffUnsupportedError`, disconnect/timeout on `power_off()` = success. Add to
`tests/unit/test_models_new_packets.py` / a `test_device_*` companion.

---

## 3. Protocol contract — `opendisplay-protocol/main` — ✅ DONE (merged as protocol 2.1)

This stage is **complete**. The canonical header `src/opendisplay_protocol.h` now implements the
contract: `CMD_POWER_OFF 0x0052` + `CMD_DEEP_SLEEP 0x0053` (opcodes **swapped** from this plan's original
draft so power-off takes the lower number — breaking vs. shipped PR #97 firmware; amended in place within
the unreleased 2.1 line, see the note at the top), full `@request`/`@response`/`@errors`/`@state` tagged
blocks for both opcodes (BE seconds, one-shot override, fire-and-forget model, latch behavior), and the
`OD_ERR_*` NACK codes in the header's **SECTION 4c** (deep sleep, scoped to `0x0053`) / **SECTION 4d**
(power-off, scoped to `0x0052`) — *not* inlined under the opcode `#define`s. See the contract doc §3.

Propagation duties for any **future** header edit are unchanged. **Do NOT hand-edit the vendored firmware
copies.** Edit the canonical file only, then propagate:
```
cd opendisplay-protocol
tools/sync_protocol_header.py --push     # canonical -> all firmware copies
tools/sync_protocol_header.py --check    # CI/pre-commit drift gate
```
**Also mirror into the Python constant source** `src/opendisplay_protocol.py` via
`tools/gen_python_protocol.py` (the header is macro-only precisely so this generator works — the new
error-code `#define`s are simple literals).

### Version-policy decision — RESOLVED
Landed as a **MINOR bump (2.0 → 2.1)**: documenting the optional length-discriminated payload, the
per-opcode NACK error namespaces, and the brand-new power-off opcode is a backward-compatible addition.
The two non-additive wrinkles are called out explicitly in the changelog: the **deep-sleep latch
*behavior* change** (power-off → sleep; wire framing unchanged, on-device effect on latch HW differs) and
the **`0x0052`↔`0x0053` opcode swap** — the swap is breaking vs. shipped PR #97 firmware but was amended
in place within the (unreleased) 2.1 line rather than taking a MAJOR bump, since 2.1 had not shipped.

---

## 4. Consumer — `Home_Assistant_Integration/feat/clean-port` (optional, follow-on)

The clean-port branch already models deep-sleep *availability* (`sleep.py` `SleepProfile`,
`delivery.py` `DeliveryManager`) but **never sends the deep-sleep command (0x0053)** and has no duration
hint. Wiring the duration in is a genuine battery lever (adaptive cadence ≈ 2× life for hourly-fresh
content — findings Part 3 §Use cases) but is **not required** for the extension itself. If pursued, keep
it minimal and opt-in:

1. **`sleep_after` / `next_update_in` hint** on the upload services only. Store it with the pending
   upload; after a successful drain, over the still-open connection, call
   `device.deep_sleep(min(hint, cap))`. Never automatic, never when work is queued.
2. **Availability-horizon fix (required if #1 ships).** The fallback availability interval is derived from
   the *configured* cadence (`sleep.py:84`, `deep_sleep_time_seconds * missed_cycles`). A commanded longer
   sleep would flap entities unavailable — when a duration is commanded, raise the horizon to
   `max(configured, commanded) * missed_cycles` for that cycle.
3. **Latch hardware:** after the revision (§1A), deep sleep (0x0053) *sleeps* on latch HW rather than
   powering off (HW feasibility resolved, §1A.1), so a `sleep_after` hint is safe there behind the usual
   firmware-version gate. reTerminal E1001/2/3 have no latch and already timer-sleep. Never send when a
   re-auth is pending. A deliberate hard-off from HA (if ever wanted) uses `power_off()` (0x0052), not the
   sleep hint.
4. **Caps:** for automatic sends, `X <= min(configured_cadence * 8, 12 h)`; a manual service call may use
   the full range with a confirmation-worthy `services.yaml` description.

Defer unless there is demand; §2 (py-opendisplay) is the load-bearing deliverable.

---

## 5. Recommended firmware hardening (optional, separate small PR)

The **required** firmware work is in §1A (0x0052 power-off handler + deep-sleep latch change + opcode swap;
HW feasibility resolved). The items here are optional hardening on top of that:
- **No deferred-ACK fix needed (explicit non-goal).** Under the fire-and-forget acknowledgement model
  (contract §2.4), the sender never expects an ACK, so the **power-off ACK** that is queued-but-not-sent
  before the rail is cut — now on **0x0052** (`sendResponse` enqueues; drained only on a later `loop()`
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
  payload (EM4, no BURTC timer). A one-line `FF 53 02` on a duration payload would stop it silently
  misleading a duration-aware client. Track separately; not part of this plan's scope.

---

## 6. Sequencing

0. **HW feasibility — DONE (§1A.1).** Resolved via 2026-07-18 schematic review + firmware analysis: D-FF
   holds across deep sleep (fw already does it; a D-FF retains Q), and reTerminal E1001/2/3 have no latch at
   all. No bench gate remains for the latch-sleep change. (Optional: a human confirms SY6974B /QON routing on
   the E-series power sheet before relying on ship mode.)
1. **Firmware revision (§1A)** — 0x0052 power-off handler + deep-sleep latch branch change + migration to
   the 2.1 swapped opcodes. This is now real firmware work, not "done."
2. **Protocol contract (§3) — DONE.** Merged as protocol 2.1 (incl. the opcode swap and the SECTION 4c/4d
   error namespaces); the spec is the source of truth the library is written against. MINOR-bump decision
   resolved (swap amended in place within the unreleased 2.1 line).
3. **py-opendisplay (§2)** — builder + method + opcode-swap adoption + NACK-interpretation fix +
   `build_power_off_command()` / `power_off()` + capability gating + tests. The critical path for making
   the firmware usable.
4. **Firmware floor clamp (§5)** — optional, parallel, independent.
5. **HA hint (§4)** — optional follow-on once #3 is released; depends on the availability-horizon fix.

## 7. Cross-repo invariants / gotchas checklist
- [ ] **Big-endian everywhere** (`>H`). The findings doc's LE recommendation is superseded.
- [ ] **2.1 opcode swap adopted everywhere:** `0x0053` = `CMD_DEEP_SLEEP`, `0x0052` = `CMD_POWER_OFF` —
      breaking vs. PR #97 firmware (deep sleep was `0x0052` there); gate on firmware version.
- [ ] **Fire-and-forget:** sender never expects/blocks on ACK/NACK; a normal return = "delivered", not
      "confirmed slept". ACK/NACK are optional/best-effort; a missing response is never failure or a retry.
- [ ] py-opendisplay `FF 53 01/02` = *rejected & awake* (surfaced if opportunistically read), **not**
      *unsupported*; caller must not blind-retry.
- [ ] `0x0000` / empty payload = configured cadence, one-shot (never persisted).
- [ ] Happy path (battery, no latch) is **silent** — disconnect = "delivered"; whether the device *slept*
      is not wire-confirmable (duration takes effect confirmable only via firmware version).
- [ ] **Latch HW deep sleep (0x0053) = timer-wake sleep** (revision) — honors the duration, wakes on
      timer/button. HW feasibility **resolved** (§1A.1): D-FF holds Q across deep sleep; reTerminal
      E1001/2/3 have no latch (always-on rail). Residual: don't command SY6974B I²C ship mode expecting a
      timer wake.
- [ ] **`CMD_POWER_OFF` (0x0052)** added: latch HW → ACK + rail cut (button-only wake); every non-latch
      target → `FF 52 00` unsupported. Fire-and-forget; payload reserved/ignored; `power_off()` has no
      `duration_s`; `FF 52 00` → `PowerOffUnsupportedError`; distinct CLI subcommand.
- [ ] `uint16` ceiling (18.2 h) is the safety cap; add a ≥10 s floor in lib (and ideally firmware).
- [x] Canonical protocol header — **done (protocol 2.1)**: deep-sleep latch note, power-off block, opcode
      swap, `OD_ERR_*` macros in SECTION 4c/4d. For future edits: canonical file only; `--push` then
      `--check`; regenerate the `.py` mirror.
