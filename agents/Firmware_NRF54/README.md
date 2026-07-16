# OpenDisplay nRF54 — Documentation Index

Audits and design studies performed 2026-07-13/14 against `Firmware_NRF54` @ `635d7d2` and `../Firmware` @ `main`. **Documentation only — no source was modified.**

## Audits — what exists today

| Doc | What it answers |
|---|---|
| [ARCHITECTURE_NRF54.md](ARCHITECTURE_NRF54.md) | How **this** firmware works: boot sequence, the complete concurrency inventory (every thread, work item, timer, semaphore, msgq, ISR and BLE callback), the BLE pipe protocol, the display pipeline, config storage, peripherals, build profiles, and 31 gaps/hazards. |
| [ARCHITECTURE_FIRMWARE_SISTER.md](ARCHITECTURE_FIRMWARE_SISTER.md) | How the **reference** `../Firmware` works: `setup()`/`loop()` semantics on nRF52840 and ESP32, the full 71-item feature inventory, the PIPE_WRITE sliding-window protocol, power/sleep state machines, and 35 "a fresh port would miss this" gotchas. |
| [FEATURE_PARITY_VS_FIRMWARE.md](FEATURE_PARITY_VS_FIRMWARE.md) | **Do they have parity? No — roughly 70%.** Exhaustive parity table, prioritized gap list with effort estimates, and nine divergences where both sides implement a feature *differently*. |
| [RTOS_COMPARISON.md](RTOS_COMPARISON.md) | The underlying RTOS: what Zephyr/NCS offers vs what this firmware actually uses (every `CONFIG_*` explained), how Arduino's `setup()`/`loop()` is really a FreeRTOS task, and the head-to-head on scheduling, memory, power, drivers, and debuggability. |

## Design studies — where to go next

| Doc | What it proposes |
|---|---|
| **[DESIGN_UNIFIED_ARCHITECTURE.md](DESIGN_UNIFIED_ARCHITECTURE.md)** | **The proposal.** Extract a `libopendisplay` core library (~10 kLOC, single-source) with a ~30-function HAL; the core **owns no threads** — it defines a CMD lane (may block 60 s) and a UI lane (≤50 ms handlers), which Zephyr fulfils with a thread + workqueue and Arduino fulfils inside `loop()`. Portable `od_work_t` deferred-work primitive; core-owned command/response rings; canonical resolutions for all nine protocol divergences with a new `0x0046` GET_CAPABILITIES for rollout; DFU strategy per target; a 7-stage migration (~20–31 weeks) whose **Stage 0 ships protocol fixes in 1 week with no refactor**. |
| [DESIGN_ZEPHYR_UNIFICATION_FEASIBILITY.md](DESIGN_ZEPHYR_UNIFICATION_FEASIBILITY.md) | **Can all three targets run on Zephyr? Verdict: don't.** 35–62 engineer-weeks vs 15–24 for extracting a shared `libopendisplay` core library instead. Full Zephyr collapses exactly one row of the platform table (BLE host); sleep, power latch, RTC retention and WiFi stay platform-specific *under Zephyr too*. Killer risk is `Seeed_GFX`/IT8951, not WiFi. |
| **[DESIGN_REPO_STRUCTURE_FOUR_TARGETS.md](DESIGN_REPO_STRUCTURE_FOUR_TARGETS.md)** | **The source tree, including Silabs BG22.** Full directory layout for one core + four platform apps, and **the layered diagram**: lanes → core → `od_hal` seam → four backends, showing how each platform fulfils the CMD/UI lane contract. Pressure-tests the lane model against bare-metal Silabs (it survives, with three wording amendments). |
| **[DESIGN_BUILD_TOOLCHAINS.md](DESIGN_BUILD_TOOLCHAINS.md)** | **Can PlatformIO unify four toolchains? No — decisively.** No nRF54 support, no BG22 support, Zephyr pinned at 2.7.1. But you don't need a unifier: **Silabs has already silently abandoned SLC** (its "auto-generated, do not edit" CMake is hand-edited), and `bb_epaper` already proves the library-as-package pattern works four ways. Recommends a monorepo + four thin native build-entry files. |
| [DESIGN_POWER_AND_EVENTS_ZEPHYR.md](DESIGN_POWER_AND_EVENTS_ZEPHYR.md) | How to build deep sleep and async event servicing properly on Zephyr: `sys_poweroff()` + checksummed retained memory + a reset-cause double-gate; the DOZE-vs-OFF sleep tiering; killing the superloop in favour of an `od_cmd` thread + `od_ui` workqueue; hardware PWM for buzzer/LED; ISR-latched buttons; interrupt-driven BUSY. |

## The short version

**No feature parity.** The nRF54 port has the core solid — BLE pipe `0x2446`, the full config system, the entire AES-CCM/CMAC encryption stack (byte-compatible), direct write, partial write, streaming zlib, boot screen, GT911 touch, SHT40, BQ27220, LED, buzzer. It also adds something the reference doesn't have: **BLE Channel Sounding / RAS ranging**.

But four protocol-level bugs will misbehave silently rather than fail cleanly:

1. **Opcode `0x0082` collision** — the reference uses it for PIPE_WRITE END; nRF54 repurposed it for an NFC endpoint that is itself a stub. A pipe transfer's final frame gets answered with a nonsense NFC error and the image never refreshes.
2. **Buzzer frequency mapping** — the reference uses a quarter-tone musical table (index 120 = A4 = 440 Hz); nRF54 uses a linear ramp (index 120 ≈ 5834 Hz). Every melody plays as noise. *Separately*, the `k_timer`-ISR square-wave generator can't hit its frequencies anyway: `k_timer` resolution is the kernel tick (30.5 µs), so a 12 kHz half-period rounds to 8 or 16 kHz.
3. **GRAY16 bits-per-pixel** — 1 on the reference, 4 on nRF54. A 16-gray image is sized 4× differently; one of them is wrong.
4. **`0x0051` ENTER_DFU acks success and does nothing.** There is no DFU/OTA path at all (no MCUboot). Clients will push firmware into a void.

And three whole subsystems are missing: **PIPE_WRITE** (the fast sliding-window image path — every push falls back to slow stop-and-wait), **all power management** (no deep sleep, no wake-on-button, no power latch, no EPD keep-alive — battery devices never sleep), and **OTA/DFU**.

The runtime model is a cooperative superloop on Zephyr's main thread, with GATT writes deferred through a `k_msgq` — architecturally cleaner than the reference's nRF52 build, which runs the entire command pipeline on the BLE callback task. But there is **no `k_mutex` anywhere in the tree**, and the boot-screen workqueue can interleave with a client's first image write against the same panel object.

**On unification:** the two design studies agree that the prize is not "one RTOS" but "one protocol implementation." Every one of the four dangerous divergences above is a *core-library* bug, not a platform bug — which is the argument for extracting `libopendisplay` (nRF54's `src/` is already ~90% of it) rather than porting ESP32 to Zephyr.
