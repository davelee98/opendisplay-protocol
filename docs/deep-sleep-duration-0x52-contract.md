# Protocol & API Contract — `CMD_DEEP_SLEEP` (0x0052) with Duration Payload

*Normative contract for the 0x0052 deep-sleep command and its optional wake-timer payload.
2026-07-17. Reflects the behavior **shipped** on `Firmware/main` (PR #97, `d974f9d`). Companion
plan: [deep-sleep-duration-0x52-plan.md](deep-sleep-duration-0x52-plan.md).*

> This document describes the wire contract and the `py-opendisplay` API contract. Where the current
> canonical header (`opendisplay-protocol/src/opendisplay_protocol.h`) or `py-opendisplay` disagrees with
> this document, they are **out of date** — this contract states the shipped firmware behavior that both
> must be brought to match (see the plan).

---

## 1. Summary

`CMD_DEEP_SLEEP` (opcode `0x0052`, host → device) commands the device to enter deep sleep **now**. It
carries an **optional `uint16` wake-timer payload, in seconds, big-endian**, that overrides the
device's configured sleep duration for **exactly one sleep cycle**.

- **Empty payload** → sleep now; wake after the configured `deep_sleep_time_seconds`.
- **Payload `0x0000`** → identical to empty (explicit "no override").
- **Payload `X` (1…65535)** → sleep now; wake after `X` seconds, **this cycle only**. On the next wake the
  device reverts to its configured cadence — the override is never persisted.

Ceiling is the wire width: `uint16` → max `65535` s ≈ **18.2 h**. This doubles as the safety cap; it can
never exceed the range already reachable through the persistent config field.

**Acknowledgement model (normative).** `0x0052` is a **fire-and-forget** command. The device **MAY** emit
an ACK or a NACK (§2.4), and a sender that receives one **SHOULD** use it — but a sender **MUST NOT**
*expect* one, block on one, or treat its **absence** as either success or failure. There is deliberately
**no wire condition that a sender may rely on to confirm the device slept**: on the primary path the
device tears down BLE without a frame, and even the latch-path ACK may never leave the buffer (§2.4).
Senders treat a successfully-transmitted command as "delivered; the device will act per its capability,"
and use a received NACK only as an opportunistic, non-guaranteed correction.

---

## 2. Wire format

### 2.1 Framing (per universal protocol rules)
- Opcode: **2 bytes, big-endian** → `0x00 0x52`.
- Payload multi-byte integers are little-endian by default **UNLESS the block marks "BE"**. This command
  **marks BE** — the seconds field is big-endian. (This is an explicit, deliberate exception; do not apply
  the little-endian default.)
- Under an active security session the payload rides inside the AES-CCM envelope like any other command
  payload; nothing here is special-cased for encryption.

### 2.2 Request (host → device)
| Bytes | Meaning |
|---|---|
| `00 52` | Sleep now, configured `deep_sleep_time_seconds`. Unchanged legacy behavior. |
| `00 52 HH LL` | Sleep now; wake timer = `(HH << 8) \| LL` seconds (**big-endian uint16**), one cycle only. |

Parsing rules as implemented (`Firmware/src/device_control.cpp:709-717`):
- `len >= 2`: seconds = big-endian first two payload bytes; **any bytes beyond 2 are ignored** (forward
  compatibility).
- `len == 1`: malformed; logged and treated as **no override** (does not reject).
- `len == 0`: no override.
- seconds `== 0x0000`: treated as **no override** (configured cadence), *not* as an error.

**Examples**
| Intent | Frame |
|---|---|
| Sleep now, config cadence | `00 52` |
| Sleep 60 s | `00 52 00 3C` |
| Sleep 3600 s (1 h) | `00 52 0E 10` |
| Sleep 65535 s (max, ≈18.2 h) | `00 52 FF FF` |

### 2.3 Response (device → host)
General response shape is `[status][cmd_echo][data…]`; `status` `0x00`=ACK, `0xFF`=NACK, `0xFE`=auth
required. `cmd_echo` for this command is `0x52`.

| Frame | Status | Meaning | Device outcome |
|---|---|---|---|
| *(no frame)* | — | Battery, no power latch: enters deep sleep synchronously and tears down BLE. | **Sleeps.** Link drops; treat disconnect as success. |
| `00 52 00 00` | ACK | Power-latch (D-FF) hardware: acknowledges, then hard-powers-off after ~100 ms. **Any duration payload is ignored** (no timer exists after power-off). | **Powers off.** Wakes only on physical button. |
| `FF 52 01 00` | NACK | Deep sleep **disabled in device config** (`deep_sleep_time_seconds == 0`). | **Stays awake.** Reachable; retry only after fixing config. |
| `FF 52 02 00` | NACK | Device is **mains-powered** (`power_mode != 1`). | **Stays awake.** Reachable. |
| `FF 52 00 00` / *(no frame, nRF)* | NACK / none | Command **not supported** on this target (nRF logs only, sends nothing). | No sleep. |
| `FE 52` | AUTH | Security enabled and no live session. | Request dropped; authenticate first. |

**Error-code namespace (scoped to opcode `0x52`)** — per the protocol's per-opcode NACK scoping rule,
`data[0]` is interpreted only in the scope of the echoed opcode:

| `data[0]` | Name (proposed) | Meaning | Client action |
|---|---|---|---|
| `0x00` | `OD_ERR_DEEP_SLEEP_UNSUPPORTED` | Target does not implement deep sleep (nRF). | Report unsupported. Do **not** retry. |
| `0x01` | `OD_ERR_DEEP_SLEEP_DISABLED` | `deep_sleep_time_seconds == 0` in config. | Rejected; device awake. Fix config to enable. |
| `0x02` | `OD_ERR_DEEP_SLEEP_NOT_BATTERY` | `power_mode != 1` (mains-powered). | Rejected; device awake. Not applicable to this device. |

> **Critical for clients:** `0x01` and `0x02` mean **"rejected, device is still awake and reachable"** —
> they are *not* "unsupported." A client must not conflate them with `0x00`. (The current
> `py-opendisplay` treats every `0xFF52` as "not supported" — that is a bug against this contract; see the
> plan §2.2.)

### 2.4 Acknowledgement model — ACK/NACK are OPTIONAL; senders MUST NOT expect them

The two rules are independent and both normative:

- **Device side — ACK/NACK are *allowed* (best-effort).** The firmware **MAY** emit the ACK (`00 52 00 00`)
  or a NACK (`FF 52 xx`) as documented in §2.3. These frames are *permitted and meaningful when they
  arrive*, but the protocol makes **no delivery guarantee** for them. In particular:
  - The **primary success path** (battery, no latch) sends **no frame at all** — it enters deep sleep and
    tears down BLE synchronously.
  - The **latch-path ACK is queued but may never transmit**: `sendResponse()` only enqueues
    (`communication.cpp:89,102-105`); the queue is drained on a *later* `loop()` pass
    (`main.cpp:238-247`), but the handler busy-waits `delay(100)` and then powers the rail off, so the ACK
    typically dies in the buffer. This is *acceptable under this model* — it is exactly why senders must
    not expect it, and it is **not** a firmware bug that needs fixing for correctness.
  - The **NACKs (`FF 52 01/02`) do transmit reliably** today (the handler returns, the device stays awake,
    the loop drains them), but a sender still must not *depend* on receiving them — proxy latency,
    encryption-decrypt timing, or a short read window can drop them.

- **Sender side — MUST NOT expect ACK/NACK.** A conformant sender is **fire-and-forget**:
  1. Transmit the command. Treat a successful transmit as **"delivered"** — do not await confirmation as a
     success gate.
  2. Treat a write failure, read timeout, or disconnect as the **normal, expected** outcome (the device
     slept or dropped the link). **Never** treat absence of a response as failure, and **never** manufacture
     a stronger claim than "delivered" from silence.
  3. **MAY** perform a single **bounded, best-effort** read for a NACK. If one arrives, surface it (§4.2):
     `FF 52 01/02` ⇒ the device is *awake and refused* (actionable); `FF 52 00` ⇒ unsupported. If none
     arrives before the short window elapses, proceed as "delivered" — this is not an error and must not be
     retried on that basis.
  4. **MUST NOT** block, spin, or retry waiting for an ACK, and MUST NOT escalate a missing ACK to the
     caller.

This resolves the ambiguity described in the plan: because the sender never claims "confirmed slept," a
lost NACK can no longer produce a *false success* — the sender only ever claims "command delivered," which
is true regardless of whether the device slept or opportunistically refused.

### 2.5 One-shot semantics
The override is a per-command parameter consumed at sleep entry
(`enterDeepSleep(force, overrideSleepSeconds)`, `Firmware/src/main.cpp:468`, arm at `:515-519`). It is
**never stored** (no flash, no RTC-retained memory). Consequences:
- The next wake (and every wake after) uses the configured `deep_sleep_time_seconds`.
- An aborted sleep (e.g. a client reconnects before entry) discards the override.
- "Sleep until time T" is therefore an **iterative, host-driven** pattern (re-arm each wake), not a single
  durable command.

### 2.6 Backward compatibility (both directions clean)
- **Old firmware + new payload:** pre-PR#97 firmware dispatched 0x0052 with no arguments, so a payload is
  silently ignored → the device sleeps at its configured cadence. Benign, but **undetectable in-band** on
  ESP32-without-latch (that path sends no frame either way). Gate on firmware version.
- **New firmware + no payload:** `len == 0` → exactly the legacy behavior. No client breaks.

### 2.7 Per-target behavior
| Target | Duration honored? | Notes |
|---|---|---|
| ESP32 (battery, no latch) | **Yes** | Happy path; no ACK; link drops. Timer wake armed for the override or config seconds. |
| ESP32 (power-latch D-FF) | **No** | ACKs `00 52 00 00`, then powers off. Payload ignored. Button wake only. |
| ESP32 (mains, `power_mode != 1`) | n/a | `FF 52 02`; stays awake. |
| Silabs Flex | **No (today)** | ACKs and ignores payload; enters EM4 (button/NFC wake), no timer armed. BURTC-timed EM4 is a plausible future enhancement — the wire format already supports it. |
| nRF | **No** | Unsupported; logs only / `FF 52 00`. |

### 2.8 Bounds & clamps
| Bound | Value | Enforced by |
|---|---|---|
| Ceiling | `65535` s (≈18.2 h) | Wire width (`uint16`). |
| Floor (recommended) | ≥ `10` s | Sending library today; **not** yet in firmware (see plan §5). `X < 10` risks a wake storm. |
| `0x0000` | = configured cadence | Firmware treats as "no override," not an error. |

---

## 3. Canonical header block (drop-in replacement for `src/opendisplay_protocol.h`)

Replace the existing `CMD_DEEP_SLEEP` block (lines 350-362) with the following. Edit the **canonical**
file only, then `tools/sync_protocol_header.py --push && --check`, and regenerate the Python mirror with
`tools/gen_python_protocol.py`. Keep the new error-code macros as simple literals so the generator parses.

```c
/* --------------------------------------------------------------------------
 * @opcode: 0x0052   @name: CMD_DEEP_SLEEP   @dir: host->device
 * @request:  [0x00][0x52]                 -> sleep now, configured cadence.
 *            [0x00][0x52][seconds:2 BE]    -> sleep now; wake timer = seconds,
 *                                             ONE cycle only (never persisted).
 *              - seconds is BIG-ENDIAN uint16 (explicit exception to the LE
 *                payload default). Range 1..65535 (<= ~18.2 h ceiling = wire cap).
 *              - seconds == 0 or empty payload == "no override" (configured
 *                cadence); NOT an error. Bytes beyond 2 ignored. len==1 ignored.
 *              - Override is one-shot: reverts to deep_sleep_time_seconds on the
 *                next wake. Recommended client floor >= 10 s (wake-storm guard).
 * @response: FIRE-AND-FORGET. ACK/NACK are OPTIONAL/best-effort; the sender MUST
 *            NOT expect, block on, or infer success from any frame. No frame is
 *            guaranteed, and absence is the normal success signal:
 *              ESP32 battery, no latch : NO frame; sleeps immediately, link drops.
 *              ESP32 w/ D-FF latch     : [0x00][0x52][0x00][0x00] ACK is QUEUED but
 *                                        usually not sent before power-off (~100 ms);
 *                                        duration IGNORED (no timer). Loss is allowed.
 *              Silabs Flex             : ACK, closes link, enters EM4 (payload ignored).
 *              nRF                     : not supported (logs; may send FF 52 00 00).
 *            A received NACK (below) is an opportunistic, non-guaranteed signal that
 *            the device stayed AWAKE; a sender uses it if seen, never waits for it.
 * @errors:   NACK data[0], scoped to opcode 0x52 (device stayed AWAKE for 01/02):
 *              0x00 OD_ERR_DEEP_SLEEP_UNSUPPORTED   target has no deep sleep (nRF).
 *              0x01 OD_ERR_DEEP_SLEEP_DISABLED      deep_sleep_time_seconds == 0.
 *              0x02 OD_ERR_DEEP_SLEEP_NOT_BATTERY   power_mode != 1 (mains-powered).
 * @state:    session required when security enabled. Timer wake is the only wake
 *            source on ESP32 (plus button wake if armed); the armed duration is
 *            also the worst-case unreachability window.
 * @limits:   optional [seconds:2 BE]; ceiling 65535 s. Client-side floor >= 10 s.
 * @targets:  Firmware (ESP32) | Silabs (payload ignored)   (nRF: no-op)
 * @since:    payload + one-shot override shipped Firmware PR #97.
 * -------------------------------------------------------------------------- */
#define CMD_DEEP_SLEEP                     0x0052u
#define OD_ERR_DEEP_SLEEP_UNSUPPORTED      0x00u
#define OD_ERR_DEEP_SLEEP_DISABLED         0x01u
#define OD_ERR_DEEP_SLEEP_NOT_BATTERY      0x02u
```

Changelog: add under `Unreleased (since 2.0)` (or roll into a `2.1` heading — MINOR bump, backward-
compatible addition of an optional payload + a scoped NACK namespace), set `LAST CHANGED`.

---

## 4. `py-opendisplay` API contract

### 4.1 `build_deep_sleep_command(duration_s: int | None = None) -> bytes`
| Input | Output |
|---|---|
| `None` | `b"\x00\x52"` |
| `duration_s` in `[10, 65535]` | `b"\x00\x52" + struct.pack(">H", duration_s)` — **big-endian** |
| `duration_s` out of range | `ValueError` |

### 4.2 `async deep_sleep(self, duration_s: int | None = None) -> None`

**Fire-and-forget (per §2.4).** The method transmits the command and returns without requiring a response.
It **MAY** do a single **bounded, best-effort** read for a NACK, but it **MUST NOT** block waiting for an
ACK or treat a missing response as failure. Its normal return means **"command delivered,"** never a
verified "device slept."

Behavior matrix:

| What the method observes | Result | Claim |
|---|---|---|
| Write succeeds, then disconnect / read timeout / no frame | **return** (success) | "delivered" — do **not** claim confirmed-slept |
| Write fails as the link tears down (`BLEConnectionError`) | **return** (success) | "delivered" — device sleeps as it drops BLE |
| `00 52 00 00` (ACK) received opportunistically | **return** (success) | "delivered / acknowledged" |
| `FF 52 01` received in the best-effort window | raise `DeepSleepRejectedError(reason="disabled")` | device **awake**, refused — actionable |
| `FF 52 02` received in the best-effort window | raise `DeepSleepRejectedError(reason="not_battery")` | device **awake**, refused — actionable |
| `FF 52 00` received | raise `DeepSleepUnsupportedError` | target lacks deep sleep |

Contract obligations:
- Pack the duration **big-endian** (`struct.pack(">H", …)`).
- **Do not expect a response.** Absence of an ACK is success ("delivered"), not a failure and not a reason
  to retry, block, or extend the wait. The best-effort read is an optimization to *catch* a NACK, never a
  gate the command depends on.
- When a NACK *does* arrive, distinguish `FF 52 01/02` (rejected, **awake**, do-not-blind-retry) from
  `FF 52 00` (unsupported). The current implementation's blanket "any `0xFF52` = not supported" is
  **non-conformant** — it both over-claims (calls a mains/disabled refusal "unsupported") and, by racing a
  short timeout against a real NACK, can mask a refusal as a false "slept."
- Gate `duration_s` on firmware capability (ESP32 ≥ the PR#97 version); on incapable targets, strip with a
  warning or raise per a `strict` flag — the duration is otherwise silently dropped by firmware/Silabs.
- Because the method never claims "confirmed slept," the §2 false-success race disappears: a dropped NACK
  degrades to a correct "delivered," not an incorrect "slept."

### 4.3 CLI
`opendisplay sleep <device> [--for SECONDS]` — `--for` omitted sends the bare command; `--for` packs a
big-endian duration and applies the same range validation.

---

## 5. Conformance checklist
- [ ] **Fire-and-forget:** sender never blocks on / expects an ACK; absence of a response = "delivered",
      not success-confirmed and not failure. ACK/NACK are optional/best-effort on the device side.
- [ ] Sender does at most one **bounded** best-effort read; a missing NACK is never a retry trigger.
- [ ] Seconds field is **big-endian** on the wire (firmware ✓, header ✓, py-opendisplay ✗ → fix).
- [ ] Empty / `0x0000` payload = configured cadence, one-shot, never persisted.
- [ ] `FF 52 01` / `FF 52 02` decoded as **rejected & awake**, not unsupported.
- [ ] `FF 52 00` (or silent nRF) decoded as unsupported.
- [ ] Latch hardware: duration ignored, device powers off — clients must not expect a timed wake.
- [ ] Ceiling `65535` s enforced by width; client floor ≥ 10 s (firmware clamp recommended).
- [ ] Header edited canonically, `--push`/`--check` run, Python mirror regenerated, MINOR bump recorded.
