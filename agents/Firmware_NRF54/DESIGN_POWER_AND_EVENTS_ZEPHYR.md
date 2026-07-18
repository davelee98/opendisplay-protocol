# Power Management & Event Servicing — Zephyr Design for the Unified OpenDisplay Firmware

> Design study, 2026-07-14. No code changed. Targets: nRF54L15 (primary), nRF52840, ESP32 (Zephyr).
> Citations: `Firmware_NRF54/…` = Zephyr port; `Firmware/…` = Arduino reference.
> Companion docs: [ARCHITECTURE_NRF54.md](ARCHITECTURE_NRF54.md), [ARCHITECTURE_FIRMWARE_SISTER.md](ARCHITECTURE_FIRMWARE_SISTER.md), [RTOS_COMPARISON.md](RTOS_COMPARISON.md), [FEATURE_PARITY_VS_FIRMWARE.md](FEATURE_PARITY_VS_FIRMWARE.md)

---

## 0. Executive summary of the two decisions that drive everything else

**Decision 1 — the sleep model is a *reboot cycle*, not a resume.** Zephyr's system-off (`sys_poweroff()` / `PM_STATE_SOFT_OFF`) resets the CPU on wake. Every piece of state the ESP32 keeps in `RTC_DATA_ATTR` (`woke_from_deep_sleep`, `deep_sleep_count`, `displayed_etag`, `mloopcounter`, `s_ext0WakePin`) must move into a **checksummed retained-RAM area** (`CONFIG_RETENTION` over `CONFIG_RETAINED_MEM`), and the "did I wake from sleep?" branch must be **double-gated** on retained state *and* `hwinfo_get_reset_cause()`. The ESP32 hazard ("RTC RAM does NOT survive panic/WDT/brownout", `Firmware/src/main.cpp:86-90`) **inverts** on Zephyr: nRF RAM keeps power across a soft/WDT/panic reset, so a stale "I slept" cookie *does* survive a crash and would make the firmware wrongly skip the boot screen. See §1.2.

**Decision 2 — kill the superloop; the blocking EPD refresh moves off the thread that services everything else.** Today `opendisplay_pipe_process()` → display → `wait_for_refresh()` (60 s cap, 50 ms poll, `Firmware_NRF54/src/opendisplay_display.cpp:127-142`) runs on `main` (prio 0), and `main` is also the only servicer of LED, buzzer, buttons, touch, and advertising (`src/opendisplay_ble.c:707-723`). Moving pipe dispatch to a dedicated **command thread** and the UI/input state machines to a dedicated **UI workqueue** makes the refresh's blockingness a non-event, and simultaneously fixes hazard #4 in `ARCHITECTURE_NRF54.md §2.5` (display state shared between `main` and the boot workqueue) *by construction* — one thread owns `s_epd`.

---

# PART 1 — Power management in Zephyr

## 1.1 What each Zephyr PM mechanism actually does, and which ones this product needs

| Mechanism | Kconfig | What it actually does | Needed here? |
|---|---|---|---|
| **Tickless idle** | `CONFIG_TICKLESS_KERNEL` (**already `y` by default on nRF**) | The idle thread programs the next GRTC/RTC compare and executes `WFI`. This is why the current firmware is already µA-class between advertising events despite having no PM code. | **Already have it.** It is *not* system-off and gives no benefit when the radio is idle for hours. |
| **System PM** | `CONFIG_PM` | Adds `pm_system_suspend()` to the idle path: on each idle entry, the *policy* (`CONFIG_PM_POLICY_DEFAULT`, residency-based) picks the deepest `power-states` node from DTS whose `min-residency-us` fits the next timeout, and calls `pm_state_set()`. Gives you `pm_policy_state_lock_get/put()` to veto states, and `pm_state_force()` to force one on next idle. | **Marginal on nRF.** On nRF54L/nRF52 the SoC's System-ON idle substates are largely what WFI+tickless already produce; the residency policy mostly buys you HFCLK/constant-latency choices. Enable it only if you also want `pm_policy_state_lock_get()` as a *sleep veto* primitive. **Do not** rely on it to reach system-off. |
| **Explicit power-off** | `CONFIG_POWEROFF` | Provides `sys_poweroff()`: locks IRQs and calls the SoC's `z_sys_poweroff()` (nRF: enter System OFF; ESP32: `esp_deep_sleep_start()`). Wake sources must already be armed. **Wake = reset.** Does *not* by itself suspend devices or drop your GPIO rails — that is your job before calling it. | **YES. This is the product's deep-sleep primitive.** It is independent of `CONFIG_PM`. |
| **Device PM** | `CONFIG_PM_DEVICE` | Adds `PM_DEVICE_ACTION_SUSPEND/RESUME/TURN_OFF` hooks to drivers that implement them; `pm_device_action_run()`. | **Partly.** Useful for the SPI/I2C/ADC/PWM peripherals we *do* instantiate (SAADC, PWM). Useless for the EPD panel — it isn't a Zephyr `device` (it's bit-banged over `nrf54_gpio.c`). |
| **Device runtime PM** | `CONFIG_PM_DEVICE_RUNTIME` | Per-device usage refcount: `pm_device_runtime_get()/put()`; `pm_device_runtime_put_async(dev, K_MSEC(n))` gives a *deferred* suspend — the closest kernel analogue of the EPD keep-alive window. | **Only for real devices** (a PWM instance for the buzzer/LED is a good fit). Not for the panel — see §1.7. |
| **Retained memory** | `CONFIG_RETAINED_MEM` (+ driver: `zephyr,retained-ram` / `nordic,nrf-gpregret`) | Exposes a RAM region (or GPREGRET registers) as a "retained memory" device. | **YES** — the substrate for the wake-state cookie. |
| **Retention** | `CONFIG_RETENTION` | Layers a magic header + CRC and named sub-areas on top of retained memory. `retention_read/write/is_valid()`. **Uses a mutex ⇒ not ISR-callable.** | **YES.** The checksum is exactly the guard the ESP32 code lacks. |
| **Reset cause** | `CONFIG_HWINFO` (already `y`, `zephyr/prj.conf:26`) | `hwinfo_get_reset_cause(&flags)` / `hwinfo_clear_reset_cause()`. Flags accumulate until cleared — **you must clear it every boot** or you can never tell the last cause. | **YES** — the second half of the wake gate. |

**Bottom line:** the minimum viable Kconfig delta is

```
CONFIG_POWEROFF=y
CONFIG_RETAINED_MEM=y
CONFIG_RETAINED_MEM_ZEPHYR_RAM=y     # or the nRF GPREGRET driver on nRF52840
CONFIG_RETENTION=y
CONFIG_RETENTION_MUTEXES=y
CONFIG_HWINFO=y                      # already set
# optional, only if you want policy locks / device suspend on idle:
# CONFIG_PM=y
# CONFIG_PM_DEVICE=y
# CONFIG_PM_DEVICE_RUNTIME=y
```

`CONFIG_PM` is **not** required for `sys_poweroff()`. Say so loudly in the commit message, because the natural assumption ("we need CONFIG_PM to sleep") sends people down the policy/DTS `power-states` rabbit hole for no gain.

## 1.2 The reset-not-resume problem, and the inverted RTC-memory hazard

### The branch we must rebuild

`Firmware/src/main.cpp:103-113`:
```
if (!is_deep_sleep_wake) { rebootFlag = 1; initDisplay(); }   // ← full boot screen + full EPD refresh
```
Skipping `initDisplay()` on a wake **is the wake path's main energy saving** (the panel keeps its image; a full refresh is 1–5 s of rail-on time plus the ~900 ms `pwrmgm(true)` settle). On Zephyr the equivalent gate sits in front of `schedule_boot_display_apply()` (`Firmware_NRF54/src/opendisplay_ble.c:704`) and `opendisplay_display_boot_apply()` (`src/opendisplay_display.cpp:677-708`).

### What survives what

| Store | nRF54L15 | nRF52840 | ESP32 (Zephyr) |
|---|---|---|---|
| **GPREGRET** (`nordic,nrf-gpregret`) | **Does not exist** — the nRF54L has no POWER→GPREGRET. Do not plan on it. | 2 × 8-bit regs, survives soft reset + System-OFF wake, cleared by POR/brownout. Already spoken for by DFU on the sister repo (`Firmware/src/device_control.cpp:679-680` sets `0xB1`). Too small anyway. | n/a |
| **Retained RAM** (`zephyr,retained-ram` over a `zephyr,memory-region`) | **Yes** — what the upstream `samples/boards/nordic/system_off` sample uses with `CONFIG_APP_USE_RETAINED_MEM=y`; the nRF54L15 DK is a listed target. | Yes (RAM retention in System OFF). | Maps to RTC slow RAM; ESP-IDF's own reload behaviour applies. |
| **RESETREAS via `hwinfo`** | Yes. Look for `RESET_LOW_POWER_WAKE` (System-OFF wake) plus `RESET_PIN`, `RESET_WATCHDOG`, `RESET_SOFTWARE`, `RESET_BROWNOUT`, `RESET_POR`. | Yes. | Partially. |

### The hazard, inverted

ESP32: RTC segments are **reloaded from the app image on every reset except a deep-sleep wake**, so `RTC_DATA_ATTR` silently resets to its initializer after a panic/WDT/brownout — a hidden crash looks like a first boot (`Firmware/src/main.cpp:86-90`).

nRF/Zephyr: retained RAM keeps power across a **soft reset, WDT reset, panic-induced reset and System-OFF wake**. It is only lost on POR/brownout/pin-reset (and even then, contents are *undefined*, not zeroed — hence the CRC). So the failure mode flips:

> **A firmware crash mid-cycle leaves a valid, checksum-correct "I went to sleep" cookie behind. Without a second gate, the device reboots after a WDT and *skips the boot screen*, silently hiding the crash and leaving `displayed_etag` claiming an image the panel may no longer hold.**

### The gate (both halves required)

```c
/* retained area, CONFIG_RETENTION with 4-byte checksum + magic */
struct od_retained {
    uint32_t sleep_intent_magic;  /* written ONLY immediately before sys_poweroff() */
    uint32_t sleep_seq;           /* ++ on each sleep entry — the "deep_sleep_count" analogue */
    uint32_t displayed_etag;      /* today: opendisplay_display.cpp:41 s_displayed_etag (plain static) */
    uint8_t  panel_image_valid;   /* set after a successful refresh; cleared on any failure/abort */
    uint8_t  woke_by_button;      /* which class of wake armed the min-wake hold */
    uint8_t  msd_loop_counter;    /* keeps advertisements distinguishable across sleeps */
    uint8_t  reserved;
};
```

Boot decision:

```c
uint32_t cause = 0;
(void)hwinfo_get_reset_cause(&cause);
(void)hwinfo_clear_reset_cause();          /* MANDATORY: flags accumulate otherwise */

bool retained_ok   = (retention_is_valid(od_retention) == 1);
bool cookie_ok     = retained_ok && r.sleep_intent_magic == OD_SLEEP_MAGIC;
bool cause_is_wake = (cause & RESET_LOW_POWER_WAKE) != 0;
bool cause_is_bad  = (cause & (RESET_WATCHDOG | RESET_BROWNOUT | RESET_DEBUG | RESET_PIN)) != 0;

bool woke_from_sleep = cookie_ok && cause_is_wake && !cause_is_bad;

/* Consume the intent immediately, whatever we decided — a cookie must never be
 * read twice. This is the one write that must happen before anything can crash. */
if (cookie_ok) { r.sleep_intent_magic = 0; retention_write(...); }

if (!woke_from_sleep) {
    r.panel_image_valid = 0;     /* we cannot prove what is on the glass */
    r.displayed_etag    = 0;     /* forces the host to a full upload, not a bad diff */
    reboot_flag         = 1;
    schedule_boot_display_apply();
}
```

Notes that matter:
- **`RESET_LOW_POWER_WAKE` support on nRF54L must be confirmed on your NCS version.** If `hwinfo_get_supported_reset_cause()` does not report it, the cookie alone plus the *absence* of `RESET_WATCHDOG|RESET_BROWNOUT|RESET_POR|RESET_PIN` is the fallback gate. Worth a build-time assertion.
- **Write-then-sleep ordering is the whole ballgame.** `sleep_intent_magic` is written in the final instructions before `sys_poweroff()`. A crash anywhere else therefore cannot forge it.
- `retention_*()` takes a mutex (`CONFIG_RETENTION_MUTEXES`) → **never call it from an ISR or a `k_timer` callback.**
- Zephyr will not zero the retained section only if it is declared `zephyr,memory-region` and excluded from `.bss`. Getting this wrong is silent: the data is simply zeroed on every boot and the device never takes the wake path.

## 1.3 Wake sources

### GPIO (nRF)

Zephyr path, no vendor calls needed:
```c
gpio_pin_configure(dev, pin, GPIO_INPUT | (pull_up ? GPIO_PULL_UP : GPIO_PULL_DOWN));
gpio_pin_interrupt_configure(dev, pin, active_low ? GPIO_INT_LEVEL_LOW : GPIO_INT_LEVEL_HIGH);
sys_poweroff();
```
`GPIO_INT_LEVEL_*` makes the nRF GPIO driver program `PIN_CNF.SENSE`, and **`PIN_CNF` is a retained register** — it holds through System OFF and is what raises the wake. This is the whole mechanism; there is **no `gpio_hold_en()` to call and none is needed** (§1.5).

Hard nRF54L15 constraints to encode in config validation:
- **Only P0 and P1 pins have SENSE / can wake. P2 has no sense, no GPIOTE, no wake capability.** OpenDisplay's pin byte is `(port<<4)|pin` (`src/nrf54_gpio.c:30-31`), and the current board puts the display on P2. **A button configured on a P2 pin can never be a wake source.** Log it and fall back to timer-only, exactly as `wake_button.cpp:133-135` does for non-wake-capable ESP32 pads.
- Edge-triggered wake requires the `sense-edge-mask` DTS property. **Use LEVEL interrupts for wake pins** and avoid the question. (`nrf54_gpio_configure_interrupt()` hard-codes `GPIO_INT_EDGE_BOTH`, `src/nrf54_gpio.c:127` — it needs a level-mode variant.)

### Porting the sister repo's careful wake-pin logic

Most of `wake_button.cpp` **evaporates** on nRF, and that is a feature:

| ESP32 concern | nRF54L/nRF52 status |
|---|---|
| ext0/ext1 single-slot limitation, "larger polarity group takes ext1" (`wake_button.cpp:195-221`) | **Gone.** Every SENSE-capable pin is independently armable. Mixed polarity is free. |
| `retainWakePull()` through RTC IO registers (`wake_button.cpp:58-69`) | **Gone.** `PIN_CNF` (including the pull) is retained. |
| `s_ext0WakePin` in RTC memory because "there is no ext0 status register" (`wake_button.cpp:26-28`) | **Gone, and better:** on wake, re-read every armed pin's level in `main()` and report which is at its wake level. |
| `esp_sleep_is_valid_wakeup_gpio()` (`wake_button.cpp:133`) | **Keep**, reimplemented as: `port == 0 \|\| port == 1` on nRF54L; all ports on nRF52840. |
| **Skip a pin already at its wake level at sleep entry** (`wake_button.cpp:137-141`) | **KEEP THIS — the single most important line in that file.** With `GPIO_INT_LEVEL_LOW` on an already-low pin, `sys_poweroff()` returns *immediately as a reset*: infinite sleep/wake ping-pong at full boot cost. Zephyr does not protect you. |
| `SLEEP_FLAG_BUTTON_WAKE_DISABLE` opt-out (`wake_button.cpp:83`) | Keep verbatim. |
| Exclude `pwr_pin_2` (latch output) and, on D-FF boards, `pwr_pin_3` (the CP clock — arming a pull could clock the latch off and cut power) (`wake_button.cpp:121-132`) | Keep verbatim. Same electrical reasoning applies. |

### Timer wake — and a portability landmine

| SoC | Timer wake from system-off? |
|---|---|
| **nRF54L15** | **Yes**, via the GRTC, but *not* through the portable `counter` API. The mechanism is `z_nrf_grtc_wakeup_prepare()` (`<zephyr/drivers/timer/nrf_grtc_timer.h>`, NCS-internal), called before `sys_poweroff()`. **Known trap:** the argument already accounts for the current SYSCOUNTER value — adding "now" to it is the standard bug and yields a device that never wakes. |
| **nRF52840** | **NO.** nRF52 System OFF has no running RTC. Wake sources are pin DETECT/sense, NFC field, LPCOMP, VBUS, reset. **`deep_sleep_time_seconds` is therefore unimplementable via system-off on nRF52840.** |
| **ESP32 (Zephyr)** | Yes — `sys_poweroff()` maps to `esp_deep_sleep_start()` with the RTC timer wake source. |

**Design consequence — two sleep tiers, not one:**

- **Tier "OFF"** (`sys_poweroff()`): whenever the wake set is GPIO-only, or on SoCs with a system-off timer (nRF54L, ESP32). Cost: full reboot (~100–300 ms of init, plus config load from NVS), but sub-µA.
- **Tier "DOZE"** (nRF52840 with `deep_sleep_time_seconds > 0`, and as a universal fallback): **stop advertising**, park all peripheral pins, force the panel rail off, and `k_sleep(K_SECONDS(n))` on a supervisor thread. Tickless idle + RAM retention on nRF52840 idles in the low-single-digit µA. Not 0.4 µA System-OFF, but within a couple of µA — and it costs no reboot, no retained-memory machinery, and **keeps the panel image and the entire RAM state trivially** (no reset-not-resume problem at all). For a display that advertises and sleeps, DOZE is very often the *right* answer even on nRF54L. **Measure both before committing.** This is the most likely place an "obvious port" over-engineers.

## 1.4 Preserving the panel image and the ETag across sleep

Today: `s_displayed_etag` is a plain file-scope `static` (`src/opendisplay_display.cpp:41`) — it does not survive anything.

Rules to enforce (lifted from the reference's hard-won invariants, `ARCHITECTURE_FIRMWARE_SISTER.md §8.33-34`):

1. `displayed_etag` lives in the retained struct. It is meaningful **only** when `panel_image_valid == 1` **and** we took the wake path.
2. **Any non-wake boot clears both.** A crash reset may have interrupted a refresh, leaving the glass in an unknown state. A partial write that diffs against a wrong base produces visible garbage no host retry can detect.
3. A successful full refresh with **no** etag *clears* `displayed_etag` (never leaves it stale).
4. Every partial-START NACK clears `displayed_etag` so the host falls back to a full upload.
5. On the wake path, **do not** `schedule_boot_display_apply()` — but **do** still create the `BBEPAPER` object with the right panel type/rotation so the first incoming `0x0070`/`0x0076` finds a sane object (as ESP32 does in `fullSetupAfterConnection()`, `Firmware/src/main.cpp:458-464`). The panel controller is power-cycled by the rail during sleep, so a warm re-acquire across sleep is impossible: the *image* survives (e-paper is bistable), the *controller state* does not. Same trap as `seeed_gfx_hw_initialized` deliberately **not** being `RTC_DATA_ATTR` (`ARCHITECTURE_FIRMWARE_SISTER.md §8.14`) — **do not put any "controller is initialised" flag in retained memory.**

## 1.5 The power latch, and the Zephyr equivalent of `gpio_hold_en()`

**There is no `gpio_hold_en()` in Zephyr, and on nRF you do not need one.** `PIN_CNF` (direction, drive, pull, sense) is retained: an output driven high before `sys_poweroff()` stays driven high through System OFF and holds through the wake until the CPU reconfigures it.

| ESP32 call | nRF equivalent |
|---|---|
| `gpio_hold_en(latch)` / `gpio_deep_sleep_hold_en()` (`power_latch.cpp:96-98,164-167`) | **Nothing.** `gpio_pin_configure(dev,pin, GPIO_OUTPUT_HIGH)` before `sys_poweroff()` suffices. |
| `esp_sleep_config_gpio_isolate()` (`power_latch.cpp:74,94`) | **Nothing automatic.** Do it explicitly: iterate every configured pin and `gpio_pin_configure(..., GPIO_DISCONNECTED)` — the port already has `nrf54_gpio_park()` (`src/nrf54_gpio.c:157-166`) and `opendisplay_display_park_pins()` (`src/opendisplay_display.cpp:89-102`). Extend to *all* config-declared pins (buzzer drive/enable, LED R/G/B, sensor bus, charger, battery-sense enable) minus wake pins and the latch pin. **A pin left as a driven output into an unpowered peripheral is a leakage path and is the #1 cause of "why is my sleep current 80 µA".** |
| `gpio_hold_dis()` at boot (`powerLatchBegin`, `power_latch.cpp:118`) | **Nothing** — but do re-assert the latch pin as `GPIO_OUTPUT_HIGH` early in `board_*_early_init()`, which the port already effectively does for the LM20's nPM1300 SHPHLD (`src/board_nrf54.c:11-15`). |

**MOSFET self-hold latch** (`DEVICE_FLAG_BATTERY_LATCH`, `pwr_pin_2` = latch, `pwr_pin_3` = shutdown button):
- Boot: drive `pwr_pin_2` high in `board_*_early_init()` (before anything can fault); configure `pwr_pin_3` as `GPIO_INPUT | GPIO_PULL_UP`.
- Power-off: drive `pwr_pin_2` low → the rail collapses. Nothing further executes; no need to call `sys_poweroff()` at all.
- **`powerOff()`'s unbounded busy-wait for button release (`power_latch.cpp:87-90`) must not be ported as a spin.** Make it a `k_work_delayable` re-polling at 20 ms, or gate power-off on the button's *release* edge, which the new ISR-driven button model (§2.4) gives you for free.

**74AHC1G79 D-flip-flop latch** (`DEVICE_FLAG_PWR_LATCH_DFF`, D=`pwr_pin_2`, CP=`pwr_pin_3`): engage/release (`power_latch.cpp:44-81`) is pure GPIO + 50 µs pulses → `gpio_pin_set()` + `k_busy_wait(50)`. Release is a **hard, immediate cut**; command `0x0052` on such a board ACKs then cuts, ignoring the duration payload (`Firmware/src/device_control.cpp:717-725`). Preserve that: send the notify, `k_msleep(50)` for the ATT to drain, then clock the latch off. **Never arm CP as a wake pin.**

## 1.6 The activity / idle-hold model, Zephyr-idiomatic

`pollActivity()` (`Firmware/src/main.cpp:187-230`) exists because the ESP32 has no central place to stamp activity — it *samples* state other tasks mutate and infers activity from changes. Once we have real kernel objects, **producers can stamp directly**, and the sampler's cleverness (including the `bleRestartAdvertisingPending` trick at `main.cpp:217` — "the only trace of a connect+drop entirely between two passes") becomes unnecessary: a `k_work` submission *is* the trace.

```c
static atomic_t s_last_activity_ms;      /* k_uptime_get_32() */
static struct k_work_delayable s_sleep_candidate_work;

static inline void od_activity(void)     /* ISR-safe, callable from anywhere */
{
    atomic_set(&s_last_activity_ms, (atomic_val_t)k_uptime_get_32());
    (void)k_work_reschedule(&s_sleep_candidate_work, K_MSEC(idle_hold_ms()));
}
```

Call `od_activity()` from: the GATT write path (`opendisplay_pipe_on_write`, `src/opendisplay_pipe.c:1381`), `connected()`/`disconnected()` (`src/opendisplay_ble.c:377,402`), the button ISR, the touch work item, the buzzer/LED activate handlers, and the display command handlers. **Six call sites replace 40 lines of sampling.**

The **sleep-candidate work** (system workqueue is fine — it never blocks) is the sole sleep decider, mirroring `enterDeepSleep()`'s load-bearing ordering (`Firmware/src/main.cpp:468-530`):

```c
static void sleep_candidate_handler(struct k_work *w)
{
    const struct PowerOption *p = &cfg->power_option;

    if (p->power_mode != 1)                    return;   /* not battery */
    if (p->deep_sleep_time_seconds == 0)       return;   /* sleep disabled */
    if (opendisplay_ble_is_connected())        { od_activity(); return; }  /* raced a connect */
    if (min_wake_hold_active())                { k_work_reschedule(w, K_MSEC(remaining_hold())); return; }
    if (od_work_in_flight())                   { k_work_reschedule(w, K_MSEC(250)); return; }
    uint32_t idle = k_uptime_get_32() - atomic_get(&s_last_activity_ms);
    if (idle < idle_hold_ms())                 { k_work_reschedule(w, K_MSEC(idle_hold_ms() - idle)); return; }

    od_enter_deep_sleep(false, 0);
}
```

- `idle_hold_ms()` = `power_option.sleep_timeout_ms` (default `DEFAULT_IDLE_HOLD_MS = 10000`, `Firmware/src/main.h:340`).
- `min_wake_hold_active()` = the floor after **first boot** or a **button wake**, sized by `min_wake_time_seconds` (default 120 s). Armed from the retained `woke_by_button` flag. **Must be checked ahead of any teardown** — everything past the advertising stop commits to the poweroff (`Firmware/src/main.cpp:484-491`).
- `od_work_in_flight()` = the `workInFlight` term (`Firmware/src/main.cpp:372-377`): pipe msgq non-empty ∨ display session ≠ PWR_OFF ∨ direct/partial write context active ∨ buzzer/LED running. Every term is transient, so — exactly as the reference warns — **it must never be the sole gate**; `lastActivityMs` supplies the quiet window.
- This `k_work_delayable` **replaces the `idle_delay_ms()` chunked superloop entirely** (`Firmware_NRF54/src/main.c:10-22`). No polling, no 100 ms wake train.

`od_enter_deep_sleep(force, override_seconds)` — the ordering is load-bearing and must be preserved literally:

```
1. re-check power_mode / deep_sleep_time_seconds / connection / min-wake-hold   (bail cheaply)
2. epd_session_force_off()          — below every early return (Firmware/src/main.cpp:492-500)
3. buzzer_stop(); led_stop();       — a PWM left running is a mA-class leak
4. touch suspend (GT911 into sleep or held in reset)
5. bt_le_adv_stop(); bt_disable();  — the sys_poweroff()-safe equivalent of BLEDevice::deinit(true)
6. flash deep power-down (0xB9)     — already exists: board_nrf54_flash_powerdown(), board_nrf54.c:48-70
7. park every non-wake, non-latch pin (GPIO_DISCONNECTED)
8. arm wake sources: GPIO SENSE (level), then GRTC (nRF54L only)
9. latch pin re-asserted as GPIO_OUTPUT_HIGH — LAST, so it cannot disturb freshly configured pins
                                     (mirrors Firmware/src/main.cpp:520-528)
10. retention_write(sleep_intent_magic = OD_SLEEP_MAGIC, ++sleep_seq, displayed_etag, …)
11. LOG_PANIC()/log flush;  sys_poweroff();
```

Step 10 is deliberately the **last** thing before `sys_poweroff()` — see §1.2.

## 1.7 Device PM for the EPD panel session: keep the FSM

The panel has no Zephyr `device` — `bb_epaper` is bit-banged through `nrf54_gpio.c`, and the SPI/I2C DTS nodes are explicitly *disabled* (`zephyr/app.overlay`). `pm_device_runtime_get()/put_async()` needs a `struct device` with a `pm_device` object, so using it would mean **inventing a fake driver just to borrow a refcount**.

`pm_device_runtime_put_async(dev, K_MSEC(window))` is genuinely a beautiful fit for the keep-alive semantics (`epdSessionRelease()` → `PWR_WARM` with a deadline, `Firmware/src/display_service.cpp:281-296`). But the FSM has three states, not two — `PWR_OFF / PWR_WARM / PWR_ACTIVE` — and WARM≠suspended: the controller is *awake*, the rail is *up*, only the deadline is armed. Device runtime PM has no third state, and its suspend hook would have to re-derive "was this a success or a failure release?" You would end up with a hand-rolled FSM *plus* a device wrapper.

**Recommendation: keep the hand-rolled FSM, port it faithfully, and fix its two Zephyr-specific weaknesses.**

1. **`pwrmgmLock` → `k_mutex`.** The reference had to hand-roll a yielding spinlock and document the priority-inversion livelock (`display_service.cpp:204-211`: "MUST yield while waiting… a bare busy-spin starves the lower-priority holder forever"). **`k_mutex` has priority inheritance built in** — the entire hazard disappears. This would be the **first `k_mutex` in the nRF54 tree** (there are currently zero, `ARCHITECTURE_NRF54.md §2.5.8`).
2. **`epdSessionTick()` (polled every loop pass, try-lock, skip-if-held) → `k_work_delayable`.** `epdSessionRelease(success)` with `window > 0` does `k_work_reschedule(&s_epd_keepalive_work, K_MSEC(window))`; `epdSessionAcquire()` does `k_work_cancel_delayable()`. Put the keep-alive work on the **UI workqueue**, never the display/command thread, or you self-deadlock on `k_work_cancel_delayable_sync()`.
3. **Keep the invariants:** `epdKeepAliveWindowMs()` = `min(screen_timeout_seconds, 30) × 1000`, 0 = power off immediately (the default, and what old blobs read); a WARM panel **survives a BLE disconnect** and keeps its window — only a disconnect *mid-transfer* (ACTIVE) tears it down; the deep-sleep path forces it off, so effective keep-alive on battery = `min(window, idle-hold)`.
4. If you *do* enable `CONFIG_PM`, wrap the session in `pm_policy_state_lock_get(PM_STATE_SOFT_OFF, PM_ALL_SUBSTATES)` on OFF→ACTIVE and `..._put()` on →OFF. That is the correct use of the policy API here: **a veto, not a driver.**

## 1.8 Config fields that must be added

The nRF54 structs **drop four features into `reserved[]`**. All are wire-compatible restorations — packet sizes unchanged, so the TLV parser's implied-size table (`src/opendisplay_config_parser.c:120-140`) and every serialized blob in the field stay valid. **A pure struct-field rename.**

| Packet | nRF54 today | Must become (matches `Firmware/src/structs.h`) |
|---|---|---|
| `0x01` **SystemConfig** (`src/opendisplay_structs.h:7-13`) | `… uint8_t pwr_pin; uint8_t reserved[17];` | `… uint8_t pwr_pin; uint8_t reserved[15]; uint8_t pwr_pin_2; uint8_t pwr_pin_3;` (`structs.h:30-32`). Needed for **both** latch types. Also add `DEVICE_FLAG_BATTERY_LATCH (1<<3)` and `DEVICE_FLAG_PWR_LATCH_DFF (1<<4)` to `src/opendisplay_device_flags.h`. |
| `0x04` **PowerOption** (`src/opendisplay_structs.h:26-43`) | `… uint8_t charger_flags; uint8_t reserved[7];` | `… uint8_t charger_flags; uint16_t min_wake_time_seconds; uint8_t screen_timeout_seconds; uint8_t reserved[4];` (`structs.h:60-64`). Needed for the **min-wake hold** and the **EPD keep-alive window**. |
| `0x25` **BinaryInputs** (`src/opendisplay_structs.h:123-141`) | `… uint8_t button_data_byte_index; uint8_t reserved[14];` | `… uint8_t button_data_byte_index; uint8_t power_off_flags; uint8_t power_off_hold_sec; uint8_t reserved[12];` (`structs.h:272-275`). Needed for **per-button long-press power-off**. |
| `0x04` `sleep_flags` | field exists, **never read** | Define and honour `SLEEP_FLAG_BUTTON_WAKE_DISABLE (1<<0)` (`structs.h:72`). |
| `0x04` `power_mode`, `deep_sleep_time_seconds` | fields exist, **never read** | Both gate every sleep decision. |

Add the compile-time guards the tree already uses elsewhere (`_Static_assert(sizeof(struct PowerOption) == 24, …)`, cf. `src/opendisplay_buzzer.c:29-30`) — struct-size drift silently desyncs the whole TLV parse (`ARCHITECTURE_FIRMWARE_SISTER.md §8.21`).

---

# PART 2 — Servicing async events without a superloop

## 2.0 The target concurrency map

| Context | Prio | Stack | Owns | Never does |
|---|---|---|---|---|
| **`main`** | 0 | 2048 (down from 4096) | init only; then `return` (or `k_sleep(K_FOREVER)`). The Zephyr main thread exiting is legal and frees its stack. | anything periodic |
| **`od_cmd` thread** (new) | **6** | **8192** | drains `s_pipe_msgq`; CCM crypto; config read/write to NVS; **all display work**: `s_epd`, direct-write, partial-write, refresh + BUSY wait, `boot_apply`. Replaces `main`'s command role **and** the boot-display workqueue. | touch I²C, LED/buzzer timing |
| **`od_ui` workqueue** (new) | **10** | **2048** | LED phase machine, buzzer melody machine, button work + long-press, touch poll/resume, EPD keep-alive expiry | any BT call that can block >ms |
| **sysworkq** | −1 (coop) | 4096 | advertising restart, MSD publish, sleep-candidate evaluator | anything blocking (**it already hosts a 10 s `k_sem_take`**) |
| **`od_cs` workqueue** (new) | 12 | 2048 | Channel-Sounding setup (the 10 s `k_sem_take`, `src/opendisplay_cs.c:146`) | — |
| BT RX | 8 | 8192 | GATT callbacks → `k_msgq_put` only (unchanged) | processing |

**Why prio 6 for `od_cmd`:** in Zephyr **lower number = higher priority**, so 6 is *higher* priority than BT RX's 8. Deliberate: the command thread must drain the msgq faster than BT RX fills it (8 slots, `src/opendisplay_pipe.c:82`), and MPSL/SDC radio timing is protected in *hardware* by IRQ priority (`RTOS_COMPARISON.md §1.4`), so no application thread priority can drop a connection. If you prefer BT-first, use 9 — but then verify the msgq doesn't overflow during a 60 s refresh (it will: today it drops frames after a 100 ms `k_msgq_put` timeout, `src/opendisplay_pipe.c:1387-1389`).

**Why not the system workqueue for UI work:** `cs_setup_work_handler` blocks the sysWQ for **up to 10 seconds** on `k_sem_take(&s_cs_config_sem, K_SECONDS(10))` (`src/opendisplay_cs.c:146`), and `s_adv_restart_work` is a delayable on that same queue. Putting the buzzer step or button debounce there would make a CS timeout silence the buzzer for 10 s. **Move CS to its own queue as part of this work** — a two-line change, and the correct fix regardless.

**Locking, introduced with this refactor** (the tree currently has **zero** `k_mutex`):

| Object | Guard | Fixes |
|---|---|---|
| `s_conn` | `k_mutex s_conn_lock` around every `bt_gatt_notify()` and the `disconnected()` unref | use-after-unref (`ARCHITECTURE_NRF54.md §2.5.1`), now *worse* because `od_cmd` notifies from a third thread |
| `msd_payload[16]` | `k_mutex s_msd_lock` (written by button/touch/sensor work, read by `adv_work_handler`) | torn read (`§2.5.2`) |
| `s_od_global_config` | `k_mutex s_cfg_lock`; `loadGlobalConfig()` `memset`s + re-parses the live struct | readers on 3 threads (`§2.5.3`) |
| `s_epd` + `pwrmgmState` | `k_mutex s_epd_lock` | `§2.5.4` — **and** single-thread-ownership makes it nearly moot |
| button edge counters | `atomic_t` per button | ISR ↔ work |
| `s_notify_enabled` | `atomic_t` | `§2.5.7` |
| `led->reserved` const-cast | **delete it** — copy the 12-byte pattern into the LED runtime struct | `§2.5.5` (an LED command currently mutates the parsed config in place) |

---

## 2.1 Buzzer — 400 Hz…12 kHz, hardware PWM, runtime pins

### The current design is worse than it looks

`buzzer_tone_timer_cb` (`src/opendisplay_buzzer.c:256-271`) toggles a GPIO from a **`k_timer` ISR** and re-arms itself each half-period. Two independent problems:

1. **Interrupt load.** At 12 kHz that is **24 000 ISRs/sec**, each doing a `nrf54_pin_decode()` (switch + `device_is_ready()`), a `gpio_pin_set()`, and a `k_timer_start()` (which reprograms a GRTC compare). Comfortably >10 % CPU and a continuous stream of high-priority interrupts contending with MPSL for the whole 5 s cap.
2. **The frequency is wrong.** `k_timer` resolution is the **kernel tick**, not microseconds. `K_USEC(41)` (a 12 kHz half-period) with the nRF default `CONFIG_SYS_CLOCK_TICKS_PER_SEC=32768` (30.5 µs/tick) rounds to 1–2 ticks → the emitted tone is 16 kHz or 8 kHz, not 12 kHz. **Across the top of the range the pitch error is tens of percent, and the "quarter-tone table anchored at A-1" semantics of the reference are simply not reproducible.** Raising the tick rate to 1 MHz fixes the resolution and makes problem #1 catastrophic.

**Treat this as a bug, not a design choice.** The reference uses hardware PWM on both platforms (nRF `HwPWM3`, ESP32 LEDC).

### Reconciling a runtime pin with Zephyr's DTS-bound PWM

**(A) — `nrfx_pwm` directly. Recommended.**
Disable the PWM node in the overlay (`&pwm20 { status = "disabled"; };`), enable `CONFIG_NRFX_PWM20=y`, and drive the peripheral yourself:

```c
nrfx_pwm_t inst = NRFX_PWM_INSTANCE(20);
nrfx_pwm_config_t cfg = NRFX_PWM_DEFAULT_CONFIG(
        NRF_GPIO_PIN_MAP(port, pin),        /* ← from the runtime config byte */
        NRFX_PWM_PIN_NOT_USED, NRFX_PWM_PIN_NOT_USED, NRFX_PWM_PIN_NOT_USED);
cfg.base_clock = NRF_PWM_CLK_1MHz;          /* 1 MHz → 12 kHz needs top=83, 400 Hz needs top=2500 */
cfg.top_value  = 1000000u / hz;
cfg.load_mode  = NRF_PWM_LOAD_COMMON;
nrfx_pwm_init(&inst, &cfg, NULL, NULL);
static uint16_t duty = ...;                 /* top * duty_percent / 100, in a static (EasyDMA) */
nrfx_pwm_simple_playback(&inst, &seq, 1, NRFX_PWM_FLAG_LOOP);
```
This is *exactly the pattern the codebase already uses* for SPI and I²C: disable the DTS node, drive the pads at runtime (`zephyr/app.overlay` disables `spi00` and `i2c22` for precisely this reason; `RTOS_COMPARISON.md §4.6` calls the overlay "the referee" between DTS and the runtime-config model). The honest, consistent answer. **Zero interrupts, exact frequency, hardware duty cycle.**

**(B) — Runtime pinctrl.** Construct a `pinctrl_soc_pin_t` (an `NRF_PSEL()`-encoded `uint32_t`) at runtime and call `pinctrl_configure_pins(&pin, 1, (uintptr_t)NRF_PWM20)` — on nRF, pinctrl writes the peripheral's own `PSEL.*` registers, so this really does re-route a live PWM instance. Keeps the Zephyr `pwm_set_cycles()` API. Fiddlier but avoids nrfx. Reasonable second choice; the upstream *Dynamic Pin Control (nRF)* sample is the precedent.

**(C) — Enumerate candidate pins in DTS** with N alternate pinctrl states. Doesn't scale to an arbitrary config blob. Reject.

### Hard constraints to encode

- **nRF54L15: PWM peripherals are only available on P1 pins.** P2 is the high-speed domain (no PWM, no GPIOTE, no sense); P0 is the LP domain. A `drive_pin` on P0 or P2 **cannot** be hardware-PWM'd.
- **`CONFIG_PWM_NRF_SW` (GPIOTE+PPI software PWM) is not supported on nRF54L** — and couldn't work on P2 anyway. It remains available on **nRF52840** as a fallback for non-PWM-capable pads.
- **Fallback ladder:** P1 → `nrfx_pwm`. nRF52840, any pin → `nrfx_pwm` (no port restriction). Non-PWM-capable pin → keep the `k_timer` toggle **but** raise `CONFIG_SYS_CLOCK_TICKS_PER_SEC` and *clamp the frequency ceiling* (e.g. 4 kHz) so the ISR rate stays sane, and log the degradation. Best of all: **validate at config-parse time and reject a buzzer pin that can't do PWM**, the way `initButtons()` already rejects GT911 INT pins (`src/opendisplay_button.c:71-74`).
- **ESP32/Zephyr:** the LEDC driver plus the GPIO matrix means any pad can be routed; option (B) is easy there.

### Where the melody state machine lives

| | |
|---|---|
| **Context** | `k_work_delayable s_buzzer_work` on the **`od_ui` workqueue** (prio 10, 2 KB) |
| **Triggered by** | `0x0077` handler (on `od_cmd`) validates the payload, copies it into `s_bz.payload[]` under `s_bz_lock`, and `k_work_reschedule(&s_buzzer_work, K_NO_WAIT)` |
| **Each run** | advance the FSM one step (`BZ_PATTERN_ENTER / BZ_STEP / BZ_PATTERN_END`, unchanged from `src/opendisplay_buzzer.c:172-248`); start/stop the PWM; `k_work_reschedule(self, K_MSEC(step_ms))`. The 5 s cap and the 20 ms inter-pattern gap are unchanged. |
| **Must NOT** | be on the sysWQ (CS can block it 10 s); call `retention_*`; hold `s_epd_lock` |
| **Stop** | from `od_cmd` → `k_work_cancel_delayable_sync(&s_buzzer_work, &sync)` then PWM off. **`_sync` is essential**: without it, a cancel racing a running handler leaves the PWM playing forever. |
| **Shared state** | `s_bz` written by `od_cmd` (activate/stop) and `od_ui` (step) → `k_mutex s_bz_lock`. Today it is *unlocked* across main + two ISRs (`ARCHITECTURE_NRF54.md §2.5.6`: `tone_running` is a plain `bool` written from ISR and main). |
| **Net effect** | **Both `k_timer`s are deleted.** ISR count for a 5 s melody drops from ~120 000 to 0. |

---

## 2.2 LED — get the 11 ms busy-wait off the critical path

`od_flash_led()` (`src/opendisplay_led.c:73-114`) does 7 × `k_busy_wait(100 µs)` per brightness step, up to 16 steps ⇒ **~11 ms of `k_busy_wait` inline on `main` (prio 0) per flash**, during which nothing at or below prio 0 runs — including the pipe drain.

**Primary fix — hardware PWM, same machinery as the buzzer.** An nRF PWM instance has **4 channels**; R/G/B map to channels 0/1/2 of one `nrfx_pwm` instance with `NRF_PWM_LOAD_INDIVIDUAL`. The 3-bit R/G and 2-bit B intensities become duty values; `LED_FLAG_INVERT_*` becomes the `NRFX_PWM_PIN_INVERTED` flag. "Flash colour X at brightness B" collapses to **one `nrfx_pwm_simple_playback()` call and zero CPU** for the duration. Same P1-only constraint on nRF54L; same config-time validation.

**Fallback (pins not PWM-capable):** keep the 7-slice software PWM but move it to a **dedicated `od_led` thread, prio 10, 512 B stack**, spinning `k_busy_wait()`. It still burns CPU, but it is preemptible by everything that matters. Do **not** put a `k_busy_wait` loop on a workqueue shared with anything else.

**Phase machine** (11 phases, `src/opendisplay_led.c:18-30`): becomes a `k_work_delayable` on `od_ui`, identical structure to the buzzer. The `s_led_timer` `k_timer` and the `s_timer_due` flag are deleted. **Also delete the const-cast** that writes `led->reserved` in the live global config (`src/opendisplay_led.c:340-348`, `:372`) — copy the 12-byte pattern into `s_run` instead; with three threads reading `s_od_global_config` it is now a genuine data race, not just ugly.

---

## 2.3 Touch (GT911)

| | |
|---|---|
| **Context** | `k_work_delayable s_touch_work` on **`od_ui`** (prio 10). Bit-bang I²C with a bounded 1000 µs clock-stretch spin (`src/opendisplay_touch.c:91-98`) is fine at prio 10. |
| **Triggered by** | (a) **INT pin GPIO ISR** → `k_work_submit_to_queue(&od_ui_wq, &s_touch_work)` — ISR-safe, nothing else. (b) A self-rescheduling 100 ms tick as the fallback poll, preserving today's rate limit (`src/opendisplay_touch.c:586-589`). |
| **Must NOT** | do I²C in the ISR; call `bt_*` from the ISR; run while the panel is refreshing |
| **Suspend/resume around refresh** | `od_cmd` (which owns the refresh) sets `atomic_set(&s_touch_suspended, 1)` and then calls **`k_work_cancel_delayable_sync(&s_touch_work, &sync)`** — the crucial part: it *waits for an in-flight poll to finish* before the panel rail moves. A plain flag would let a poll straddle the rail cut. After the refresh, `od_cmd` clears the flag and submits the resume work. **`opendisplay_touch_resume_after_refresh()` costs 200 ms–2 s** (`src/opendisplay_touch.c:552-575`) — running it on `od_ui` rather than inline in the refresh path is a direct latency win for the `0x73` ack. |
| **Failure backoff** | unchanged: `i2c_fail_streak`, disable after 5 failures (`src/opendisplay_touch.c:613-620`). On disable, stop rescheduling and `gpio_pin_interrupt_configure(..., GPIO_INT_DISABLE)`. |
| **Shared state** | touch results → `msd_payload` under `s_msd_lock`; `s_touch_suspended` is an `atomic_t`. |

---

## 2.4 Buttons — the polling model has a real bug

`opendisplay_button_process()` (`src/opendisplay_button.c:102-137`) is called from the ~100 ms idle tick and **re-polls the level**; the ISR only sets a flag that the poll then discards (`:106`). **A press-and-release inside one tick is completely invisible** — no `press_count` increment, no MSD update, no advertising boost. The reference does the edge accounting *in the ISR* precisely to avoid this (`Firmware/src/device_control.cpp:517-532`).

```c
/* ISR — one gpio_callback per pin, GPIO_INT_EDGE_BOTH (or LEVEL when armed for wake) */
static void button_isr(const struct device *d, struct gpio_callback *cb, uint32_t pins)
{
    ButtonState *b = CONTAINER_OF(cb, ButtonState, cb);
    uint32_t now = k_uptime_get_32();                 /* ISR-safe */
    if ((now - b->last_edge_ms) < BTN_DEBOUNCE_MS) return;   /* 20 ms lockout */
    b->last_edge_ms = now;

    int lvl = gpio_pin_get(d, b->pin);                /* ISR-safe */
    bool pressed = b->inverted ? !lvl : lvl;

    if (pressed) {
        atomic_inc(&b->press_edges);
        if (b->power_off_flags_set)                   /* per-button long-press power-off */
            k_work_schedule_for_queue(&od_ui_wq, &b->long_press_work,
                                      K_SECONDS(b->power_off_hold_sec ?: 3));
    } else {
        (void)k_work_cancel_delayable(&b->long_press_work);   /* ISR-safe */
    }
    atomic_set(&b->level, pressed);
    od_activity();                                    /* ISR-safe, §1.6 */
    k_work_submit_to_queue(&od_ui_wq, &s_button_work);
}
```

| | |
|---|---|
| **Work item** (`od_ui`, prio 10) | reads `atomic_get(&b->press_edges)` / `&b->level`, packs `id \| press_count<<3 \| state<<7`, takes `s_msd_lock`, `opendisplay_ble_set_dynamic_byte()`, `opendisplay_ble_update_msd(true)`, `opendisplay_ble_boost_advertising()` (→ sysWQ). |
| **ISR must NOT** | call `bt_*`, `printf`, `retention_*`, or any I²C |
| **Long-press power-off** | `k_work_delayable` armed on press, cancelled on release. Handler: buzzer power-off alert then `powerLatchTriggerOff()`. Exact, no polling; replaces `powerButtonPoll()`'s `millis()` arithmetic (`Firmware/src/power_latch.cpp:124-145`). Keep its "require a release since boot" guard — a device that boots with the button held must not immediately power off. |
| **Init dance to keep** | the reference's detach → 50 ms settle → re-read → re-attach (`Firmware/src/device_control.cpp:624-662`) discards the spurious edges generated *by pin configuration itself*. Port it, or the first `k_work` submission happens before `bt_enable()` has run. |
| **Wake mode** | when arming for sleep, reconfigure the same pins from `GPIO_INT_EDGE_BOTH` to `GPIO_INT_LEVEL_LOW/HIGH` (§1.3). `nrf54_gpio_configure_interrupt()` needs a `flags` parameter (it hard-codes `GPIO_INT_EDGE_BOTH`, `src/nrf54_gpio.c:127`). |

---

## 2.5 The blocking EPD refresh

**Two independent changes; do both.**

### (a) Move the refresh off the servicing thread (the change that actually matters)

Today, `0x0072` → `opendisplay_display_direct_write_end_refresh()` → `wait_for_refresh(60000)` (`src/opendisplay_display.cpp:127-142`, `:955`) runs on **`main`**, which is also the only servicer of the buzzer, LED, buttons, touch and advertising fallback. A 5 s refresh silences the buzzer for 5 s — the exact ESP32 pathology.

Once **all** pipe dispatch runs on **`od_cmd`** and the UI machines run on **`od_ui`**, a 60 s blocking wait on `od_cmd` costs *nothing else*: `od_ui` (prio 10) is ready and preempts nothing it shouldn't; BT RX keeps enqueuing; sysWQ keeps advertising alive. **This alone converts the single worst structural problem in both repos into a non-issue, and it does so without touching the busy-wait at all.**

It also **eliminates the boot-display workqueue** (`src/opendisplay_ble.c:146-147,161-170`): `boot_apply` becomes a work item submitted to `od_cmd`, so hazard §2.5.4 ("a client connects immediately and runs `direct_write_start()` on `main` against the same `s_epd` the boot render is using") disappears — one thread, one owner, one object.

### (b) Interrupt-driven BUSY, replacing the 50 ms poll

```c
static K_SEM_DEFINE(s_busy_sem, 0, 1);

static void busy_isr(...) { k_sem_give(&s_busy_sem); }        /* ISR-safe */

static bool wait_for_refresh(uint32_t timeout_ms)
{
    k_sem_reset(&s_busy_sem);
    gpio_pin_interrupt_configure(dev, busy_pin, GPIO_INT_EDGE_TO_INACTIVE);
    /* arm AFTER issuing refresh(); also do one level check to close the race
       where BUSY deasserted before we armed */
    if (!s_epd.isBusy()) return true;
    int rc = k_sem_take(&s_busy_sem, K_MSEC(timeout_ms));
    gpio_pin_interrupt_configure(dev, busy_pin, GPIO_INT_DISABLE);
    return rc == 0;
}
```

**What it buys:** the CPU **actually sleeps** for the 1–5 s (up to 60 s) of the refresh instead of a 20 Hz wake train; refresh-complete latency drops from up to 50 ms to microseconds, shortening the panel's rail-on window per refresh; and the `saw_busy` heuristic (`:130-138`) — which returns `true` only if it *observed* BUSY assert, silently masking panels that assert BUSY for <50 ms — goes away.

**What it complicates:**
- **The arm/check race.** Some panels deassert BUSY in well under 50 µs. The `isBusy()` re-check after arming is mandatory.
- **BUSY must be on a sense/interrupt-capable pin.** On nRF54L15, **P2 has no sense and no GPIOTE.** A config that puts BUSY on P2 **cannot** use the interrupt path. **Keep the 50 ms poll as a runtime fallback**, selected by port number at init.
- **`bbepWaitBusy()` inside `bb_epaper`** (`third_party/bb_epaper/src/nrf54_bbep_busy.inl:20-50`) does its own 20 ms poll and is called from `bbepSendCMDSequence`'s `BUSY_WAIT` opcodes during panel init. Convert it too, or accept it — panel init is 100s of ms, not seconds.
- **Protocol ordering is unchanged and safe:** `0x72` ack → `k_msleep(20)` → refresh → `0x73`/`0x74` (`src/opendisplay_pipe.c:796-806`), all emitted by **`od_cmd`, in order, from one thread**. But `bt_gatt_notify()` is now called from a *third* thread → **`s_conn` must go under `s_conn_lock`** (§2.0), and the 200 × `k_msleep(1)` notify-backpressure retry (`src/opendisplay_pipe.c:542-550`) now blocks `od_cmd` rather than `main`, which is correct.

---

## 2.6 Advertising tick + MSD refresh

| | |
|---|---|
| **Advertising boost expiry** (`opendisplay_ble_advertising_tick()`, `src/opendisplay_ble.c:632-649`) | Replace the poll: `opendisplay_ble_boost_advertising()` does `k_work_reschedule(&s_adv_boost_work, K_MSEC(OD_ADV_BOOST_MS))`; the handler restores the interval and restarts advertising. **sysWQ.** Exact expiry instead of ≤100 ms late. |
| **MSD refresh** (today: once per `sleep_timeout_ms` idle cycle, `src/main.c:49-51`) | `k_work_delayable s_msd_work` on **sysWQ**, self-rescheduling at 60 s (matching `Firmware/src/main.cpp:402-406`). Handler: `update_msd_payload()` (SHT40 + BQ27220 + battery, each with a 30 s cache) under `s_msd_lock`, then `schedule_msd_publish()`. **Keep the "skip the advertising rewrite if the payload is unchanged" early-exit** (`src/opendisplay_ble.c:346-348`) — without it, nRF tears down and restarts advertising on every pass (`ARCHITECTURE_FIRMWARE_SISTER.md §8.35`). |
| **Advertising-restart fallback** (`src/opendisplay_ble.c:719-722`) | Keep it, but as a `k_work_delayable` self-heal at 500 ms rather than a superloop poll. Note the existing bug it papers over: it re-submits to the **same sysWQ that CS may have blocked for 10 s**. Moving CS to `od_cs_wq` is what actually fixes it. |

**Result: `main()` has no loop.** After `opendisplay_ble_init()` it arms `s_msd_work` and `s_sleep_candidate_work` and returns. Every wakeup from that point is a real event — a GPIO edge, a radio event, a BUSY deassert, or a scheduled work expiry — which is precisely the property that makes the `sys_poweroff()` idle-hold measurement meaningful.

---

## 3. Open questions, flagged rather than guessed

1. **`RESET_LOW_POWER_WAKE` on nRF54L15 in your NCS version.** The hwinfo API defines the flag; whether the nRF54L driver maps RESETREAS's off-wake bit to it needs a one-line check on hardware (`hwinfo_get_supported_reset_cause()`). If absent, the fallback gate in §1.2 is sound but strictly weaker.
2. **Whether `sys_poweroff()` runs `PM_DEVICE_ACTION_TURN_OFF` on your devices.** Zephyr's `poweroff.c` locks IRQs and calls the SoC hook; do not rely on device suspend happening for you. Explicitly power down the panel rail, PWM, and flash before calling it (steps 2–7 in §1.6) regardless.
3. **Real measured deltas: System-OFF + GRTC wake + reboot vs. DOZE (advertising stopped, `k_sleep`, RAM retained).** Every architectural cost in Part 1 — retained memory, checksums, the reset-cause gate, the "skip the boot screen" branch and its crash hazard — exists *only* to make System-OFF safe. **If DOZE measures within a couple of µA on your board, the correct engineering decision is to not build any of it** on nRF, and reserve `sys_poweroff()` for the ESP32 target and for the latched hard-off. Measure first.
4. **nRF54L15 PWM instance count and pin domain in your board's DTS.** PWM being P1-only is the binding constraint on both the buzzer and the LED; validate against real OpenDisplay config blobs in the field before the PWM path becomes the default rather than a fallback.

---

## Sources

[Zephyr Power off](https://docs.zephyrproject.org/latest/services/poweroff.html) · [Zephyr System Off sample (nRF)](https://docs.zephyrproject.org/latest/samples/boards/nordic/system_off/README.html) · [Zephyr Retention System](https://docs.zephyrproject.org/latest/services/storage/retention/index.html) · [Zephyr Retained Memory](https://docs.zephyrproject.org/latest/hardware/peripherals/retained_mem.html) · [Zephyr hwinfo](https://docs.zephyrproject.org/latest/hardware/peripherals/hwinfo.html) · [Zephyr Device Runtime PM](https://docs.zephyrproject.org/latest/services/pm/device_runtime.html) · [Zephyr System PM](https://docs.zephyrproject.org/latest/services/pm/system.html) · [Nordic: Essential pin planning for the nRF54L Series](https://devzone.nordicsemi.com/nordic/nordic-blog/b/blog/posts/essential-pin-planning-guidelines-for-the-nrf54l-series) · [DevZone: nRF54L15 GRTC wake after sys_poweroff()](https://devzone.nordicsemi.com/f/nordic-q-a/120655/nrf54l15-dk-does-not-wake-up-from-grtc-after-sys_poweroff) · [DevZone: nRF54L15 with SW PWM](https://devzone.nordicsemi.com/f/nordic-q-a/121736/nrf54l15-with-sw-pwm) · [Zephyr Dynamic Pin Control (nRF)](https://docs.zephyrproject.org/latest/samples/boards/nordic/dynamic_pinctrl/README.html) · [Zephyr ESP32 Deep Sleep sample](https://docs.zephyrproject.org/latest/samples/boards/espressif/deep_sleep/README.html)
