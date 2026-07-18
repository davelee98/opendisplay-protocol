# Findings: can loop() keep servicing non-screen work during a panel refresh?

> 2026-07-13, branch `feat/less-latency`. Investigation only — no code changed.
> Question: is it possible to avoid blocking calls so that non-screen items
> (buzzer, buttons, touch, BLE) still get serviced while the display is busy?

**Short answer: yes, and the cheap version is much cheaper than a full async
rewrite.** The dominant stall is a single pure poll loop (`waitforrefresh()`)
with no state, so it can be turned into a service pump in ~50 lines. A true
non-blocking state machine is achievable for the bb_epaper family, but *not* for
the Seeed/IT8951 panel without patching the vendor library.

## 1. The two platforms fail differently

| | ESP32 | nRF52 |
|---|---|---|
| Where command handlers run | **`loop()` task** (BLE `onWrite` only memcpys into a ring; `main.cpp:315-333` drains it) | **Bluefruit callback task** (`imageDataWritten` is registered directly as the write callback, `ble_init.cpp:167`) |
| During a refresh | `loop()` is *inside* the refresh. **Nothing else runs at all** for its full duration | `delay()` is `vTaskDelay`, so it yields — `loop()` **does** keep running |
| Buzzer service period during a refresh | **never called** → the sounding note drones for 1–5 s | 100 ms (via `idleDelay`) → notes quantised to 100 ms |

This is why `pwrmgmLock` (`display_service.cpp:204-217`) exists at all: on nRF
the panel session is genuinely touched from two tasks.

Consequence: **on neither platform does the display have to become truly async to
fix the audible bug.** ESP32 needs the blocking wait to pump services; nRF needs
a finer service period. Full async is a *latency/throughput* win (BLE commands
keep flowing during a refresh), not a prerequisite for correctness.

## 2. Where the time actually goes

Per user-visible push, worst case:

| Blocking site | file:line | Mechanism | Cost |
|---|---|---|---|
| `waitforrefresh()` | `display_service.cpp:509-535` | `delay(10)` + `bbepIsBusy()` poll | **1–5 s typical** |
| `pwrmgm(true)` rail settle | `main.cpp:613,641,647` | fixed `delay(800)` + `delay(100/200)` | **~0.9–1.0 s per cold acquire** |
| Seeed `tconWaitForDisplayReady()` | `lib/Seeed_GFX/Extensions/Tcon.cpp:521-524` | `while(tconReadReg(LUTAFSR));` | **2–5 s, UNBOUNDED** |
| `seeed_gfx_direct_refresh()` | `display_seeed_gfx.cpp:179-184` | two full GC16 passes | **4–10 s** |
| `bbepSendCMDSequence` init | `bb_ep.inl:4202-4227` | `BUSY_WAIT` opcodes | N × up to 5 s |

`bbepRefresh()` (`bb_ep.inl:4365`) **already returns immediately** after
`MASTER_ACTIVATE` — the panel refreshes asynchronously in hardware. The firmware
is blocking purely by *choice*, in its own poll loop. That is the whole
opportunity.

## 3. Recommended: Tier A — service pump inside the wait loops (small, safe)

Replace the bare `delay()` in the wait/settle paths with a `pumpServices(ms)`
cooperative delay that runs the non-display services between polls.

Safe to pump (verified re-entrancy-free w.r.t. the display path):
- `buzzerService()` — `buzzer_control.cpp:239` — early-outs on `!active`, touches only PWM + `millis()`.
- `processLedFlash()` — `device_control.cpp:364` — millis()-polled, no display calls.
- `flushResponseQueueToBle()` — `main.cpp:237` — notify-only.
- `epdSessionTick()` — `display_service.cpp:304` — **a no-op during a refresh**: it returns immediately unless `pwrmgmState == PWR_WARM`, and a refresh is `PWR_ACTIVE`. It cannot cut the rail out from under itself.

Must **not** be pumped:
- The BLE command drain (`main.cpp:315-333`) — re-enters the display path. This is the hard boundary that keeps Tier A safe and small.
- `processTouchInput()` — already skipped while `directWriteActive` (`touch_input.cpp:580`), and `touchResumeAfterEpdRefresh()` can take ~200 ms–2 s.
- `processButtonEvents()` — it is not cheap: it calls `updatemsdata()` → SHT40/BQ27220 I2C + `readBatteryVoltage()` (**~50–100 ms**, `device_control.cpp:452`), and `flashLed()` busy-waits up to **~200 ms** (`device_control.cpp:485-513`). Running I2C mid-SPI-refresh is a robustness risk. Button presses survive anyway — the ISR latches them — so latency, not loss.

Achievable granularity is set by the poll cadence, and here there is a catch:

> **`bbepIsBusy()` internally does `delay(10) + delay(1)`** (`bb_ep.inl:3984,3986`),
> on *every call*. So `waitforrefresh()`'s loop iteration is **~21 ms, not the
> 10 ms it claims**, and its effective timeout is **~2× nominal (≈120 s, not 60 s)**.
> Commit f39b199 ("tighten waitforrefresh poll from 100 ms to 10 ms") therefore
> only ever got to ~21 ms.

`bbepIsBusy()` is trivial — a `digitalRead()` plus a chip-type polarity check.
Reimplementing it locally without the two delays drops the pump period to the
buzzer's 5 ms quantum (`kBuzzerDurationUnitMs`, `buzzer_control.cpp:15`) and
incidentally makes the 60 s timeout mean 60 s.

Also in Tier A: `idleDelay()`'s `CHECK_INTERVAL_MS = 100` (`main.cpp:429`) is the
**nRF buzzer's** real problem — it is the only frequent `buzzerService()` call
site on that platform, so *every* note is quantised to 100 ms even with the
display completely idle. Make the chunk adaptive (5 ms while the buzzer is
active).

Result: the buzzer plays correctly through a refresh on both platforms. BLE
commands still stall for the refresh duration.

## 4. Tier B — true async (bb_epaper only)

`waitforrefresh()` becomes a `REFRESH_WAIT` deadline state polled from `loop()`;
`pwrmgm(true)`'s `delay(800)` becomes a rail-settle deadline state. `epdSession`
is already the single choke point for both the full-frame and partial paths, so
it is the natural home for the state machine.

The real cost is not the state machine, it is the **ACK contract**: handlers like
`handleDirectWriteEnd()` currently refresh and *then* respond. Async means the
completion ACK must be deferred. ESP32 already has the machinery (the response
ring + `flushResponseQueueToBle`). nRF does not — `sendResponse()` is called
directly from the BLE task — so nRF would want routing through a command queue
like ESP32's, which is a real architectural change (and the stale
`feat/esp32-freertos-command-queue` branch is *not* a usable starting point; it
predates the current atomics/bounded-drain work and would block the BLE host task
up to 100 ms on a full queue).

Payoff: BLE keeps flowing during a refresh — pipe-window ACKs are not delayed,
which matters because the 10-slot response ring **drops the newest entry when
full** (`main.h:381`), so a long stall plus a chatty host can collapse throughput.

## 5. Tier C — Seeed/IT8951: not fixable without a vendor patch

`EPaper::update()` is monolithic (push framebuffer + `tconDisplayArea1bpp` +
`tconWaitForDisplayReady` + sleep), and the wait is an **unbounded**
`while(tconReadReg(LUTAFSR))` with SPI transactions inside it. It would have to
be split into `pushImage()` / `startRefresh()` / `pollRefreshDone()`. Until then,
Tier A (pump inside a patched wait) is the ceiling for this panel.

## 6. Bugs found along the way (independent of any redesign)

1. **`waitforrefresh()` timeout is ~2× what it says** (~120 s, not 60 s) — the hidden `delay(11)` inside `bbepIsBusy()`. Poll cadence is 21 ms, not 10 ms.
2. **Seeed `tconWaitForDisplayReady()` has no timeout** (`Tcon.cpp:521`) — a wedged panel hangs the firmware forever.
3. **`enterDeepSleep()`'s two `delay(2000)` early-returns are dead code** (`main.cpp:471,476`). They are vestigial log-spam throttles from the initial-commit era, sitting on the two *static config* checks (not-battery / sleep-disabled) while the two *dynamic* checks below them (BLE connected, min-wake hold) correctly have no delay. Every caller now pre-checks those same two conditions: `main.cpp:396` is guarded by `deep_sleep_time_seconds > 0 && power_mode == 1` (the exact negation), and the `0x0052` handler rejects both with an error response before calling (`device_control.cpp:727-737`). The only reachable path is `main.cpp:297` (post-wake advertising window), which requires a self-contradictory config state and clears `advertising_timeout_active` before the call, so it can fire **at most once**, not repeatedly. Safe to delete.
4. **nRF buzzer is quantised to 100 ms** regardless of display activity (§3) — sub-100 ms notes cannot play correctly.
5. `kBuzzerMaxTotalMs` is 30000 but the comments at `buzzer_control.cpp:144,167` say "5 s cap" — stale.
6. **`powerLatch powerOff()` busy-waits unbounded** for button release (`power_latch.cpp:86-90`), reached from `processButtonEvents()`.
7. Latent: `CHECK_BUSY()` in `ED103TC2_Defines.h:55-62` expands to `do { tconWaitForReady(); } while (true);` — an infinite loop. Not currently reachable (`EPD_INIT()` is never invoked for this panel).

## 7. Recommendation

Do **Tier A** now — it is contained, it fixes the audible drone (the only
*correctness* symptom), and it fixes bug #1 as a side effect. Treat Tier B as a
follow-up justified by BLE throughput, not by the buzzer. Tier C only if the
10.3" Seeed panel's multi-second unbounded stall becomes a problem in its own
right (bug #2 arguably makes it one already).
