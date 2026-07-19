# Protocol & API Contract — `CMD_DEEP_SLEEP` (0x0053) + `CMD_POWER_OFF` (0x0052)

*Normative contract for the deep-sleep command `CMD_DEEP_SLEEP` (0x0053, with its optional
wake-timer payload) and the power-off command `CMD_POWER_OFF` (0x0052). 2026-07-17; **revised
2026-07-18** to split "sleep" from "power off": deep sleep now performs a timer-wake deep sleep on
**all** battery targets — including power-latch (D-FF) hardware — and the hard rail-cut moves to the
dedicated `CMD_POWER_OFF`; **revised 2026-07-19** for the merged protocol-2.1 opcode swap
(0x0052 = power-off, 0x0053 = deep sleep — see §1). Companion plan:
[deep-sleep-duration-0x53-plan.md](deep-sleep-duration-0x53-plan.md).*

> **Shipped vs. target.** The deep-sleep *duration payload* is shipped (Firmware PR #97, `d974f9d`) —
> on the **pre-swap opcode 0x0052** (the shipped firmware predates the 2.1 opcode swap; see §1). The
> deep-sleep *latch behavior change* (latch → timer-wake sleep instead of power-off), the entire
> `CMD_POWER_OFF` command, and the firmware side of the opcode swap are a **target design, not yet
> shipped** — they require firmware work (see the plan).
>
> **Hardware feasibility — RESOLVED (2026-07-18 schematic review).** Holding the rail powered across
> `esp_deep_sleep_start()` for a timer wake is **feasible** (§2.7). On the Seeed **reTerminal E1001/E1002/
> E1003** it is *inherent*: those boards have **no D-FF latch** — a mechanical slide switch feeds an SY6974B
> power-path and an always-on TPS631000 rail (EN tied high), so **no GPIO gates the rail** and the D-FF
> power-off branch never fires there. On actual D-FF hardware the firmware already holds the flip-flop's D
> input high across deep sleep (`powerLatchHoldForSleep()`), and a D flip-flop retains its Q (power-enable)
> through sleep by nature. The **only** residual rail-drop risk is an *I²C-commanded SY6974B ship mode*
> (battery disconnect, button-only revive) — plain deep sleep never triggers it.

> This document describes the wire contract and the `py-opendisplay` API contract. The canonical
> header (`opendisplay-protocol/src/opendisplay_protocol.h`) **implements this contract as of
> protocol 2.1** (see §3); where `py-opendisplay` or firmware disagrees with this document, they
> must be brought to match this contract (see the plan).

---

## 1. Summary

> **Opcode assignment (updated — merged protocol 2.1).** The current mapping is
> **`0x0052` = `CMD_POWER_OFF`** (`RESP_POWER_OFF` `0x52`) and **`0x0053` = `CMD_DEEP_SLEEP`**
> (`RESP_DEEP_SLEEP` `0x53`). `CMD_DEEP_SLEEP` **moved `0x0052` → `0x0053`** — swapped with
> power-off so that power-off takes the lower number. This is **BREAKING vs. shipped firmware**:
> `0x0052` was `CMD_DEEP_SLEEP` in Firmware PR #97, so a peer still on the old mapping now hits the
> wrong command. The swap was amended in place within the (unreleased) 2.1 line, not a MAJOR bump.
> Everything else about each command — payloads, response model, error codes and their meanings,
> per-target behavior — is unchanged; only the opcode numbers moved.

`CMD_DEEP_SLEEP` (opcode `0x0053`, host → device) commands the device to enter deep sleep **now**. It
carries an **optional `uint16` wake-timer payload, in seconds, big-endian**, that overrides the
device's configured sleep duration for **exactly one sleep cycle**.

- **Empty payload** → sleep now; wake after the configured `deep_sleep_time_seconds`.
- **Payload `0x0000`** → identical to empty (explicit "no override").
- **Payload `X` (1…65535)** → sleep now; wake after `X` seconds, **this cycle only**. On the next wake the
  device reverts to its configured cadence — the override is never persisted.

Ceiling is the wire width: `uint16` → max `65535` s ≈ **18.2 h**. This doubles as the safety cap; it can
never exceed the range already reachable through the persistent config field.

**Acknowledgement model (normative).** `0x0053` is a **fire-and-forget** command. The device **MAY** emit
an ACK or a NACK (§2.4), and a sender that receives one **SHOULD** use it — but a sender **MUST NOT**
*expect* one, block on one, or treat its **absence** as either success or failure. There is deliberately
**no wire condition that a sender may rely on to confirm the device slept**: on **every** battery path —
latch or non-latch — the device now enters deep sleep and tears down BLE without a frame (§2.4). (The old
latch-path ACK belonged to the *power-off* behavior, which has moved to `CMD_POWER_OFF`, `0x0052`.)
Senders treat a successfully-transmitted command as "delivered; the device will act per its capability,"
and use a received NACK only as an opportunistic, non-guaranteed correction.

---

## 2. Wire format

### 2.1 Framing (per universal protocol rules)
- Opcode: **2 bytes, big-endian** → `0x00 0x53`.
- Payload multi-byte integers are little-endian by default **UNLESS the block marks "BE"**. This command
  **marks BE** — the seconds field is big-endian. (This is an explicit, deliberate exception; do not apply
  the little-endian default.)
- Under an active security session the payload rides inside the AES-CCM envelope like any other command
  payload; nothing here is special-cased for encryption.

### 2.2 Request (host → device)
| Bytes | Meaning |
|---|---|
| `00 53` | Sleep now, configured `deep_sleep_time_seconds`. Unchanged legacy behavior. |
| `00 53 HH LL` | Sleep now; wake timer = `(HH << 8) \| LL` seconds (**big-endian uint16**), one cycle only. |

Parsing rules as implemented (`Firmware/src/device_control.cpp:711-716`):
- `len >= 2`: seconds = big-endian first two payload bytes; **any bytes beyond 2 are ignored** (forward
  compatibility).
- `len == 1`: malformed; logged and treated as **no override** (does not reject).
- `len == 0`: no override.
- seconds `== 0x0000`: treated as **no override** (configured cadence), *not* as an error.

**Examples**
| Intent | Frame |
|---|---|
| Sleep now, config cadence | `00 53` |
| Sleep 60 s | `00 53 00 3C` |
| Sleep 3600 s (1 h) | `00 53 0E 10` |
| Sleep 65535 s (max, ≈18.2 h) | `00 53 FF FF` |

### 2.3 Response (device → host)
General response shape is `[status][cmd_echo][data…]`; `status` `0x00`=ACK, `0xFF`=NACK, `0xFE`=auth
required. `cmd_echo` for this command is `0x53`.

| Frame | Status | Meaning | Device outcome |
|---|---|---|---|
| *(no frame)* | — | Battery, **latch or non-latch**: enters deep sleep synchronously, arms timer/button wake, and tears down BLE. On D-FF latch HW the latch is **held**, not cut (feasible — §2.7); reTerminal E-series have no latch to hold (always-on rail). | **Sleeps.** Link drops; treat disconnect as success. |
| `FF 53 01 00` | NACK | Deep sleep **disabled in device config** (`deep_sleep_time_seconds == 0`). | **Stays awake.** Reachable; retry only after fixing config. |
| `FF 53 02 00` | NACK | Device is **mains-powered** (`power_mode != 1`). | **Stays awake.** Reachable. |
| `FF 53 00 00` / *(no frame, nRF)* | NACK / none | Command **not supported** on this target (nRF logs only, sends nothing). | No sleep. |
| `FE 53` | AUTH | Security enabled and no live session. | Request dropped; authenticate first. |

**Error-code namespace (scoped to opcode `0x53`; header SECTION 4c)** — per the protocol's per-opcode
NACK scoping rule, `data[0]` is interpreted only in the scope of the echoed opcode:

| `data[0]` | Name (header SECTION 4c) | Meaning | Client action |
|---|---|---|---|
| `0x00` | `OD_ERR_DEEP_SLEEP_UNSUPPORTED` | Target does not implement deep sleep (nRF). | Report unsupported. Do **not** retry. |
| `0x01` | `OD_ERR_DEEP_SLEEP_DISABLED` | `deep_sleep_time_seconds == 0` in config. | Rejected; device awake. Fix config to enable. |
| `0x02` | `OD_ERR_DEEP_SLEEP_NOT_BATTERY` | `power_mode != 1` (mains-powered). | Rejected; device awake. Not applicable to this device. |

> **Critical for clients:** `0x01` and `0x02` mean **"rejected, device is still awake and reachable"** —
> they are *not* "unsupported." A client must not conflate them with `0x00`. (The current
> `py-opendisplay` treats every deep-sleep NACK as "not supported" — that is a bug against this
> contract; see the plan §2.2.)

### 2.4 Acknowledgement model — ACK/NACK are OPTIONAL; senders MUST NOT expect them

The two rules are independent and both normative:

- **Device side — ACK/NACK are *allowed* (best-effort).** The firmware **MAY** emit the ACK (`00 53 00 00`)
  or a NACK (`FF 53 xx`) as documented in §2.3. These frames are *permitted and meaningful when they
  arrive*, but the protocol makes **no delivery guarantee** for them. In particular:
  - The **primary success path** (battery, **latch or non-latch**) sends **no frame at all** — it enters
    deep sleep and tears down BLE synchronously.
  - The **power-off ACK is queued but may never transmit** — this now applies to **`0x0052 CMD_POWER_OFF`**
    on latch hardware (the deep-sleep command, `0x0053`, no longer powers off): `sendResponse()` only
    enqueues; the queue is drained on a *later* `loop()` pass, but the handler busy-waits `delay(100)` and
    then powers the rail off, so the ACK typically dies in the buffer. This is *acceptable under this
    model* — it is exactly why senders must not expect it, and it is **not** a firmware bug that needs
    fixing for correctness.
  - The **NACKs (`FF 53 01/02`) do transmit reliably** today (the handler returns, the device stays awake,
    the loop drains them), but a sender still must not *depend* on receiving them — proxy latency,
    encryption-decrypt timing, or a short read window can drop them.

- **Sender side — MUST NOT expect ACK/NACK.** A conformant sender is **fire-and-forget**:
  1. Transmit the command. Treat a successful transmit as **"delivered"** — do not await confirmation as a
     success gate.
  2. Treat a write failure, read timeout, or disconnect as the **normal, expected** outcome (the device
     slept or dropped the link). **Never** treat absence of a response as failure, and **never** manufacture
     a stronger claim than "delivered" from silence.
  3. **MAY** perform a single **bounded, best-effort** read for a NACK. If one arrives, surface it (§4.2):
     `FF 53 01/02` ⇒ the device is *awake and refused* (actionable); `FF 53 00` ⇒ unsupported. If none
     arrives before the short window elapses, proceed as "delivered" — this is not an error and must not be
     retried on that basis.
  4. **MUST NOT** block, spin, or retry waiting for an ACK, and MUST NOT escalate a missing ACK to the
     caller.

This resolves the ambiguity described in the plan: because the sender never claims "confirmed slept," a
lost NACK can no longer produce a *false success* — the sender only ever claims "command delivered," which
is true regardless of whether the device slept or opportunistically refused.

### 2.5 One-shot semantics
The override is a per-command parameter consumed at sleep entry
(`enterDeepSleep(force, overrideSleepSeconds)`, `Firmware/src/main.cpp:510`, arm at `:557-565`). It is
**never stored** (no flash, no RTC-retained memory). Consequences:
- The next wake (and every wake after) uses the configured `deep_sleep_time_seconds`.
- An aborted sleep (e.g. a client reconnects before entry) discards the override.
- "Sleep until time T" is therefore an **iterative, host-driven** pattern (re-arm each wake), not a single
  durable command.

### 2.6 Backward compatibility (payload extension clean both directions; the 2.1 opcode swap is separately BREAKING — see §1)
- **Old firmware + new payload:** pre-PR#97 firmware dispatched the deep-sleep opcode with no arguments, so
  a payload is silently ignored → the device sleeps at its configured cadence. Benign, but **undetectable
  in-band** on ESP32-without-latch (that path sends no frame either way). Gate on firmware version.
- **New firmware + no payload:** `len == 0` → exactly the legacy behavior. No client breaks.
- **Opcode swap (protocol 2.1) vs. shipped firmware — NOT clean:** shipped firmware (PR #97) maps
  `0x0052` → deep sleep; under the 2.1 mapping `0x0052` is `CMD_POWER_OFF`. A new-mapping sender talking to
  old-mapping firmware (or vice versa) hits the **wrong command**. Gate on firmware version.

### 2.7 Per-target behavior
| Target | Duration honored? | Notes |
|---|---|---|
| ESP32 (battery, no latch) | **Yes** | Happy path; no ACK; link drops. Timer wake armed for the override or config seconds. |
| ESP32 (power-latch D-FF) | **Yes (target)** | **Changed:** now identical to non-latch — holds the latch, arms timer/override wake, sleeps; no frame. **Feasible (verified 2026-07-18):** the firmware already holds the flip-flop's D input across deep sleep (`powerLatchHoldForSleep()` → `gpio_deep_sleep_hold_en()` / `gpio_hold_en()`), and a D flip-flop retains Q through sleep. A caller wanting a hard off uses `0x0052` (`CMD_POWER_OFF`). |
| ESP32 (reTerminal E1001/2/3) | **Yes (already)** | **No D-FF latch** — mechanical slide switch + SY6974B power-path + always-on TPS631000 rail (EN tied high); no GPIO gates the rail. The latch branch never fires; the deep-sleep command is a plain timer-sleep here today. Rail stays up through deep sleep inherently. |
| ESP32 (mains, `power_mode != 1`) | n/a | `FF 53 02`; stays awake. |
| Silabs Flex | **No (today)** | ACKs and ignores payload; enters EM4 (button/NFC wake), no timer armed. BURTC-timed EM4 is a plausible future enhancement — the wire format already supports it. |
| nRF | **No** | Unsupported; logs only / `FF 53 00`. |

### 2.8 Bounds & clamps
| Bound | Value | Enforced by |
|---|---|---|
| Ceiling | `65535` s (≈18.2 h) | Wire width (`uint16`). |
| Floor (recommended) | ≥ `10` s | Sending library today; **not** yet in firmware (see plan §5). `X < 10` risks a wake storm. |
| `0x0000` | = configured cadence | Firmware treats as "no override," not an error. |

### 2.9 Companion command — `CMD_POWER_OFF` (0x0052)

Introduced by this revision to carry the **hard power-off** semantics that used to live on the deep-sleep
command's latch path. `CMD_POWER_OFF` (opcode `0x0052`, host → device) cuts the device's power rail via
the D-FF latch; the device then wakes **only on a physical button press** (a fresh cold boot). There is no
timer and no wake interval — power-off is absolute.

**Payload.** Optional and **reserved**: firmware ignores any payload today (bare `00 52`). The field is held
open for a possible future variant; senders SHOULD send no payload. Bytes beyond the opcode are ignored.

**Targets & responses.** Only power-latch hardware can honor it; every other target refuses.

| Frame | Status | Meaning | Device outcome |
|---|---|---|---|
| `00 52 00 00` *(queued)* | ACK | Power-latch (D-FF) HW: acknowledges, then `powerLatchPowerOff()` after ~100 ms. ACK is best-effort (§2.4) — usually dies in the buffer before the rail is cut. | **Powers off.** Button wake only. |
| `FF 52 00 00` | NACK | **No power latch on this target** (battery non-latch, mains, Silabs, nRF): there is no rail to cut. | **Stays awake / unchanged.** Reachable. |
| `FE 52` | AUTH | Security enabled, no live session. | Dropped; authenticate first. |

**Error namespace (scoped to opcode `0x52`; header SECTION 4d).**

| `data[0]` | Name (header SECTION 4d) | Meaning | Client action |
|---|---|---|---|
| `0x00` | `OD_ERR_POWER_OFF_UNSUPPORTED` | Target has no power latch (cannot cut its own rail). | Report unsupported. If the intent was low power (not a hard off), use `0x0053` instead. |

**Acknowledgement model.** Identical to `0x0053` (§2.4): **fire-and-forget**. The power-off ACK is queued
and usually not transmitted before the rail is cut; senders MUST NOT expect it. Treat a successful transmit
as "delivered."

**Why split it out.** Overloading a single deep-sleep opcode forced latch hardware to power off when a
caller only wanted low-power sleep, and left latch devices unable to do a timed wake at all. Separating the
two gives *every* battery target a uniform "sleep, wake on timer" via `0x0053`, and a caller that
specifically wants the device fully off (button-only revival) an explicit, capability-gated `0x0052`.

---

## 3. Canonical header block — IMPLEMENTED in protocol 2.1 (see `src/opendisplay_protocol.h`)

This section was originally a drop-in **proposal**; it is now **implemented** in the merged canonical
header (protocol **2.1**), with two differences from the snippet as first drafted:

1. **Opcodes swapped** (see §1): `CMD_DEEP_SLEEP` is **`0x0053`** and `CMD_POWER_OFF` is **`0x0052`**
   (the draft had them the other way around; the swap gives power-off the lower number).
2. **Error macros are not inlined under the opcode `#define`s** — the `OD_ERR_*` codes live in the
   header's **SECTION 4** error-namespace area: SECTION **4c** (deep sleep, scoped to opcode `0x0053`)
   and SECTION **4d** (power-off, scoped to opcode `0x0052`).

The implemented values (macro names and values unchanged from the draft; only the opcode numbers and the
macro placement moved):

```c
/* Command opcodes (see the header's opcode section for the full tagged blocks) */
#define CMD_POWER_OFF                      0x0052u  /* hard rail-cut; RESP_POWER_OFF 0x52       */
#define CMD_DEEP_SLEEP                     0x0053u  /* timer-wake sleep; RESP_DEEP_SLEEP 0x53;
                                                       optional [seconds:2 BE] one-shot payload */

/* SECTION 4c — NACK error codes scoped to opcode 0x0053 (CMD_DEEP_SLEEP) */
#define OD_ERR_DEEP_SLEEP_UNSUPPORTED      0x00u
#define OD_ERR_DEEP_SLEEP_DISABLED         0x01u
#define OD_ERR_DEEP_SLEEP_NOT_BATTERY      0x02u

/* SECTION 4d — NACK error codes scoped to opcode 0x0052 (CMD_POWER_OFF) */
#define OD_ERR_POWER_OFF_UNSUPPORTED       0x00u
```

The semantic content of the drafted tagged blocks — BE seconds field, one-shot override, `0`/empty =
no-override, fire-and-forget response model, per-target behavior, latch-hold feasibility notes, and the
reserved power-off payload — is carried in the merged header's `@opcode` blocks unchanged apart from the
opcode numbers; consult `src/opendisplay_protocol.h` for the authoritative text. After any further edit to
the canonical header: `tools/sync_protocol_header.py --push && --check`, and regenerate the Python mirror
with `tools/gen_python_protocol.py` (the error-code macros are simple literals so the generator parses).

Changelog: recorded under the (unreleased) **2.1** line — a MINOR bump for the backward-compatible
additions (optional deep-sleep payload + scoped NACK namespaces + the new power-off opcode), with the
deep-sleep latch *behavior* change called out explicitly. The subsequent `0x0052`↔`0x0053` **opcode swap**
was amended in place within the unreleased 2.1 line rather than taking a MAJOR bump; the changelog calls
out that it is breaking vs. shipped PR #97 firmware (see §1).

---

## 4. `py-opendisplay` API contract

### 4.1 `build_deep_sleep_command(duration_s: int | None = None) -> bytes`
| Input | Output |
|---|---|
| `None` | `b"\x00\x53"` |
| `duration_s` in `[10, 65535]` | `b"\x00\x53" + struct.pack(">H", duration_s)` — **big-endian** |
| `duration_s` out of range | `ValueError` |

### 4.2 `async deep_sleep(self, duration_s: int | None = None) -> None`

**Fire-and-forget (per §2.4).** The method transmits the command (opcode `0x0053`) and returns without
requiring a response. It **MAY** do a single **bounded, best-effort** read for a NACK, but it **MUST NOT**
block waiting for an ACK or treat a missing response as failure. Its normal return means **"command
delivered,"** never a verified "device slept."

Behavior matrix:

| What the method observes | Result | Claim |
|---|---|---|
| Write succeeds, then disconnect / read timeout / no frame | **return** (success) | "delivered" — do **not** claim confirmed-slept |
| Write fails as the link tears down (`BLEConnectionError`) | **return** (success) | "delivered" — device sleeps as it drops BLE |
| `00 53 00 00` (ACK) received opportunistically | **return** (success) | "delivered / acknowledged" |
| `FF 53 01` received in the best-effort window | raise `DeepSleepRejectedError(reason="disabled")` | device **awake**, refused — actionable |
| `FF 53 02` received in the best-effort window | raise `DeepSleepRejectedError(reason="not_battery")` | device **awake**, refused — actionable |
| `FF 53 00` received | raise `DeepSleepUnsupportedError` | target lacks deep sleep |

Contract obligations:
- Pack the duration **big-endian** (`struct.pack(">H", …)`).
- **Do not expect a response.** Absence of an ACK is success ("delivered"), not a failure and not a reason
  to retry, block, or extend the wait. The best-effort read is an optimization to *catch* a NACK, never a
  gate the command depends on.
- When a NACK *does* arrive, distinguish `FF 53 01/02` (rejected, **awake**, do-not-blind-retry) from
  `FF 53 00` (unsupported). The current implementation's blanket "any deep-sleep NACK = not supported" is
  **non-conformant** — it both over-claims (calls a mains/disabled refusal "unsupported") and, by racing a
  short timeout against a real NACK, can mask a refusal as a false "slept."
- Gate `duration_s` on firmware capability (ESP32 ≥ the PR#97 version); on incapable targets, strip with a
  warning or raise per a `strict` flag — the duration is otherwise silently dropped by firmware/Silabs.
- Because the method never claims "confirmed slept," the §2 false-success race disappears: a dropped NACK
  degrades to a correct "delivered," not an incorrect "slept."

### 4.3 CLI
`opendisplay sleep <device> [--for SECONDS]` — `--for` omitted sends the bare command; `--for` packs a
big-endian duration and applies the same range validation. A separate `opendisplay power-off <device>`
maps to `power_off()` (§4.4) — deliberately a distinct subcommand so "sleep" and "hard off" can never be
confused at the CLI.

### 4.4 `build_power_off_command() -> bytes` and `async power_off(self) -> None`

`build_power_off_command()` returns `b"\x00\x52"` (no payload — the field is reserved). `power_off()` is
**fire-and-forget** exactly like `deep_sleep()` (§4.2): transmit, then treat disconnect / read timeout /
opportunistic ACK as **"delivered."** A `FF 52 00` seen in the bounded best-effort window raises
`PowerOffUnsupportedError` — the target has no power latch. Callers must not conflate "power-off
unsupported" with "cannot sleep": an unsupported target may still accept `0x0053`. Because power-off is
absolute (no wake timer), `power_off()` takes **no** `duration_s` parameter.

| What the method observes | Result | Claim |
|---|---|---|
| Write succeeds, then disconnect / timeout / no frame | **return** (success) | "delivered" — device is powering off |
| Write fails as the link tears down (`BLEConnectionError`) | **return** (success) | "delivered" |
| `00 52 00 00` (ACK) received opportunistically | **return** (success) | "delivered / acknowledged" |
| `FF 52 00` received in the best-effort window | raise `PowerOffUnsupportedError` | target has **no latch**; still reachable |

---

## 5. Conformance checklist
- [ ] **Fire-and-forget:** sender never blocks on / expects an ACK; absence of a response = "delivered",
      not success-confirmed and not failure. ACK/NACK are optional/best-effort on the device side.
- [ ] Sender does at most one **bounded** best-effort read; a missing NACK is never a retry trigger.
- [ ] Seconds field is **big-endian** on the wire (firmware ✓, header ✓, py-opendisplay ✗ → fix).
- [ ] Empty / `0x0000` payload = configured cadence, one-shot, never persisted.
- [ ] **Opcode swap adopted:** senders and firmware use `0x0053` = `CMD_DEEP_SLEEP`, `0x0052` =
      `CMD_POWER_OFF` (protocol 2.1) — breaking vs. PR #97 firmware, which had deep sleep on `0x0052` (§1).
- [ ] `FF 53 01` / `FF 53 02` decoded as **rejected & awake**, not unsupported.
- [ ] `FF 53 00` (or silent nRF) decoded as unsupported.
- [ ] **Latch HW deep sleep (`0x0053`) = timer-wake sleep** (no longer power-off) — feasibility **resolved**
      (2026-07-18): D-FF hardware holds the FF's D across deep sleep (fw already does this) and a D-FF
      retains Q; reTerminal E1001/2/3 have no latch at all (always-on rail). Sole residual risk:
      I²C-commanded SY6974B ship mode.
- [ ] **`0x0052 CMD_POWER_OFF`**: latch HW → ACK then rail cut (button-only wake); all other targets →
      `FF 52 00` unsupported. Fire-and-forget; optional payload reserved/ignored.
- [ ] `power_off()` is a distinct method/CLI subcommand (no `duration_s`); `FF 52 00` → `PowerOffUnsupportedError`.
- [ ] Ceiling `65535` s enforced by width; client floor ≥ 10 s (firmware clamp recommended).
- [x] Header edited canonically — **done in protocol 2.1** (deep-sleep latch note, power-off block, and
      the `0x0052`↔`0x0053` opcode swap; `OD_ERR_*` macros in SECTION 4c/4d), MINOR bump recorded with
      the latch *behavior* change and the swap called out explicitly. Propagation (`--push`/`--check`,
      Python-mirror regeneration) follows the standard workflow.
