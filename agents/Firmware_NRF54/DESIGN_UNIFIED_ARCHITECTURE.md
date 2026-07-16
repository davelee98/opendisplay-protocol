# OpenDisplay Convergence Architecture — `libopendisplay` + Canonical Protocol

> Architecture proposal, 2026-07-14. Documentation only; no code changed.
> Targets: **nRF54L15** (`Firmware_NRF54`, NCS/Zephyr), **nRF52840** (`Firmware`, Arduino/Bluefruit → Zephyr later), **ESP32-S3/C3/C6/classic** (`Firmware`, Arduino/ESP-IDF — stays there).
> Builds on: [DESIGN_ZEPHYR_UNIFICATION_FEASIBILITY.md](DESIGN_ZEPHYR_UNIFICATION_FEASIBILITY.md) (accepted), [DESIGN_POWER_AND_EVENTS_ZEPHYR.md](DESIGN_POWER_AND_EVENTS_ZEPHYR.md) (accepted), [FEATURE_PARITY_VS_FIRMWARE.md](FEATURE_PARITY_VS_FIRMWARE.md), [ARCHITECTURE_NRF54.md](ARCHITECTURE_NRF54.md), [ARCHITECTURE_FIRMWARE_SISTER.md](ARCHITECTURE_FIRMWARE_SISTER.md), [RTOS_COMPARISON.md](RTOS_COMPARISON.md).

---

## 0. Baseline verdicts, accepted — and the one sentence that drives this document

I accept both prior verdicts without reservation:

1. **No full Zephyr port of ESP32.** The feasibility study's numbers hold: 35–62 eng-weeks vs 15–24, with Seeed_GFX/IT8951 (29.4k LOC, no Zephyr port exists) as an unbounded item and a one-way partition-table door on a fleet with no OTA rollback. Nothing here weakens that case; §6 revisits it only under the "13.3″ dropped" hypothetical.
2. **The Zephyr-side event topology is settled** (`od_cmd` thread prio 6, `od_ui` workqueue prio 10, `od_cs` workqueue, sleep via `sys_poweroff()` + retention + reset-cause double-gate). This document does not re-derive it; it defines the **portable layer underneath it** so the same core code also runs inside an Arduino superloop.

The driving observation, from the feasibility study's own conclusion: **every one of the four dangerous protocol divergences is a core-library bug, not a platform bug** (FEATURE_PARITY §4). The architecture below is organised so that the *protocol and product logic have exactly one implementation*, and the platforms are reduced to an ~30-function HAL plus a scheduling shim.

---

## 1. `libopendisplay` — the core library, concretely

### 1.1 Module map (what goes where)

The nRF54 tree is already ~90% of the core (feasibility §D.1). The split, module by module:

| Module (today: `Firmware_NRF54/src/`) | LOC | Destination | Notes |
|---|---:|---|---|
| `opendisplay_pipe.c` | 1425 | **core** `od_pipe.c` | Framing, dispatch, auth/CMAC, session keys, hand-rolled CCM (`opendisplay_pipe.c:174-405` — already pure-ECB, portable as-is), replay window. Gains PIPE_WRITE (ported once, from `Firmware/src/display_service.cpp:2114-2600`). `k_msgq` → core ring (§2.3); `atomic_t` → `od_atomic` shim. |
| `opendisplay_config_parser.c` + `factory_config.c` | 775 | **core** | Already zero platform deps. Deduplicate the two CRC-16 copies (`ARCHITECTURE_NRF54.md §9.21`). |
| `opendisplay_display.cpp` + `_color.c` + `_epd_map.c` | 1149 | **core** (C++ island) | Plane splitting, geometry, streaming inflate, panel quirk tables, ETag rules, the PWR_OFF/WARM/ACTIVE session FSM (restored per DESIGN_POWER §1.7). Talks only to `od_hal` + the panel backend (§1.4). |
| `boot_screen.cpp` + `logo_bitmap.h` + `qr/` | ~2400 | **core** (C++ island) | Pure render; already identical byte-for-byte to the reference's logo. |
| `opendisplay_buzzer.c`, `opendisplay_led.c` | 789 | **core** | FSMs only. `k_timer` toggling is deleted (DESIGN_POWER §2.1 calls it a bug); output goes through `od_pwm_*` (§1.2), stepping via `od_work_t` (§2.2). |
| `opendisplay_button.c`, `opendisplay_touch.c` | ~840 | **core** | GT911 driver over `od_i2c_xfer`; button edge accounting moves into the ISR path per DESIGN_POWER §2.4, expressed as `od_gpio_configure_irq` callbacks + atomics. |
| `opendisplay_sensor_sht40.c`, `_bq27220.c`, `opendisplay_i2c.c` | ~600 | **core** | Merge the duplicated bit-bang I²C in `opendisplay_touch.c:70-174` into one core soft-I²C used only when `od_i2c_configure()` reports no hardware bus (ARCHITECTURE_NRF54 §9.20). |
| `opendisplay_battery.c`, MSD payload builder (from `opendisplay_ble.c:224-274`) | ~300 | **core** | MSD *content* + suppress-if-unchanged is core; submission is HAL. |
| `structs.h`, protocol/constants/flags headers | ~600 | **core** | Single canonical `od_structs.h` with the reserved-field restorations of DESIGN_POWER §1.8 and the `_Static_assert` size guards. |
| `third_party/bb_epaper`, `uzlib` (Group5 dropped until used) | vendored | **core** `third_party/` | bb_epaper's IO shim retargets to `od_hal` (it already goes through `nrf54_gpio`/compat shims — a rename). |
| Sleep **policy** (activity stamping, idle-hold, `workInFlight`, min-wake) | new (~200) | **core** `od_power_policy.c` | Decides *when*; never executes sleep. |
| `opendisplay_ble.c` (stack init, GATT define, adv submission, conn callbacks) | 751 | **platform** | Zephyr host / Bluefruit / Bluedroid-NimBLE each keep their own ~300-line transport file that funnels into `od_pipe_rx()` / implements `od_notify()`. |
| `opendisplay_cs.c` (Channel Sounding) | 210 | **platform (Zephyr-only)** | nRF54L-only hardware feature. |
| `nrf54_gpio.c`, `nrf54_zephyr_time.c`, `board_nrf54.c`, `opendisplay_config_storage.c` | ~500 | **platform** → `od_hal_zephyr/` | These *are* the backend. |
| `wifi_service.cpp`, `wake_button.cpp`, `power_latch.cpp` execution, `esp_sleep_*`, Seeed_GFX + `display_seeed_gfx.cpp` | — | **platform (ESP32)** | Per feasibility §D.2: these stay platform-specific even under full Zephyr. Latch *sequencing* (the load-bearing ordering) is core; the pin pokes are HAL calls, so most of `power_latch.cpp` actually folds into core too. |

Net: **~10 kLOC single-source core**, matching the feasibility estimate of ~65% of the combined codebase.

### 1.2 The `od_hal.h` contract — refined

The feasibility draft (~20 functions) is close. Three corrections:

**Missing (add):**

```c
/* --- PWM: the buzzer/LED redesign makes this mandatory, not optional.
 *     DESIGN_POWER §2.1/§2.2 kills the k_timer GPIO toggle; the reference
 *     already uses HwPWM3/LEDC. Without this the core FSMs have no output. */
int  od_pwm_start(uint8_t pin_cfg, uint32_t freq_hz, uint16_t duty_permille);
int  od_pwm_update(uint8_t pin_cfg, uint32_t freq_hz, uint16_t duty_permille);
void od_pwm_stop(uint8_t pin_cfg);
/* Backend: nRF54 nrfx_pwm (P1-only — see caps below); nRF52 nrfx_pwm/HwPWM;
 * ESP32 LEDC. Returns -ENOTSUP if the pad can't PWM → core falls back or
 * rejects at config-parse time, per DESIGN_POWER §2.1 fallback ladder. */

/* --- pin capability query: nRF54L15's P0/P1/P2 domain rules (no SENSE/PWM/
 *     GPIOTE on P2, PWM P1-only, ADC P1.00-07) must NOT leak into core as
 *     #ifdefs. Core validates configs through this. */
uint32_t od_gpio_caps(uint8_t pin_cfg);   /* bitmask: OD_PINCAP_WAKE|_IRQ|_PWM|_ADC */

/* --- assisted level wait: lets the Zephyr backend do IRQ+k_sem BUSY waiting
 *     (DESIGN_POWER §2.5b) while Arduino/P2 pins poll. yield is called each
 *     poll slice so shared-thread platforms can pump UI work (§2.4 below). */
int  od_gpio_wait_level(uint8_t pin_cfg, bool level, uint32_t timeout_ms,
                        void (*yield)(void));

/* --- the ONE blocking primitive (cmd-lane wakeup, §2.3) --- */
void od_event_signal(void);                       /* ISR-safe */
void od_event_wait(uint32_t timeout_ms);          /* cmd lane only */

/* --- logging: kills the String-vs-printf fork (RTOS_COMPARISON §4.10) --- */
void od_log(const char *fmt, ...);
```

**Deliberately NOT in the HAL (would be over-abstraction):**

- **BLE advertising/GATT setup.** Only `od_notify()`, `od_link_connected()`, and one new sink `od_adv_set_msd(const uint8_t msd[16], bool boost)` cross the boundary. Stack init, service registration, PHY/DLE requests, bonding are platform files — they differ structurally per stack and abstracting them is where shared-HAL projects go to die.
- **Deep-sleep execution, wake arming, retention, latch pin hold.** Core exposes policy callouts (`od_power_policy_may_sleep()`, `od_power_policy_on_activity()`); the platform's `od_enter_deep_sleep()` implements DESIGN_POWER §1.6's 11-step ordering. Even full-Zephyr couldn't share this (feasibility §A.5).
- **Key/value storage.** Keep the draft's single-blob `od_store_save/load/clear`. The config is one ≤4 KB blob everywhere; a KV abstraction buys nothing. Bond storage stays inside each BLE platform file.
- **Multiple SPI buses / MISO.** One write-only EPD bus is the product. `od_spi_configure(sck, mosi, hz)` as drafted; the backend decides hardware-vs-bit-bang (and the nRF54 backend later upgrades to crossbar-routed hardware SPI per feasibility §C.3 with **zero core change** — that is the proof the abstraction is at the right altitude).

**One correction to the draft:** `od_gpio_*` takes the **opaque config byte**, decoded per-backend — keep that, it's what neutralises divergence D9 (`(port<<4)|pin` on nRF54 vs flat Arduino numbers, FEATURE_PARITY §4-D9). But the nRF52 Zephyr backend must implement the **flat 0-47 decode** to stay wire-compatible with deployed blobs (feasibility §B.2.1); the decoder is backend property, never core.

Final surface: **~30 functions in three headers** — `od_hal.h` (pins/buses/time/store/crypto/transport/misc), `od_work.h` (§2.2), `od_port.h` (compile-time knobs: queue depth/slot size, static-vs-heap zlib window).

### 1.3 C/C++ boundary

Follow the nRF54 model, which is already correct: **core is C99; two C++ islands** (`od_display.cpp`+`boot_screen.cpp`, and vendored `bb_epaper.cpp`) with all public headers `extern "C"`-guarded (the pattern of `nrf54_zephyr_compat.h:8-10`). Rules, enforced by the library's own CI:

- No `String`, no `std::` containers, no exceptions/RTTI, no heap in core (static buffers, as today — RTOS_COMPARISON §3 "Memory model" is a genuine advantage; keep it).
- The Arduino backend is `.cpp` files implementing the C ABI — trivial.
- This direction (C core, C++ consumers) is the only one that works: the reverse (C++ core) would poison the Zephyr C tree with `CONFIG_CPP` coupling beyond the two files that already need it.

### 1.4 Panel backend seam

`od_display.cpp` calls a tiny ops struct, not bb_epaper directly:

```c
struct od_panel_ops {
    int  (*init)(const struct DisplayConfig *);
    int  (*write_cmd_seq)/(*stream_plane)/(*set_window)…;
    int  (*refresh)(int mode);            /* returns immediately; BUSY via od_gpio_wait_level */
    void (*sleep)(void); void (*rail)(bool on);
};
```

Core provides the **bb_epaper backend** (all panel IDs `0x00–0x4C`). The **Seeed_GFX/IT8951 backend registers from the ESP32 platform layer** — selected at runtime by `panel_ic_type` 3000/3001 exactly as today (`Firmware/src/display_service.cpp:500-506`). This is the containment vessel for the 29.4k-LOC risk: Seeed_GFX never enters the library, and no other platform ever links it.

### 1.5 Where the library lives, and how both builds consume it

**Decision: a new repo, `libopendisplay`, consumed as a pinned git submodule by both firmware repos.** Not a monorepo: the two app repos have independent release trains, CI, and contributor bases today, and a monorepo migration is churn that buys nothing the submodule pin doesn't. The submodule SHA in each app repo *is* the compatibility record.

```
libopendisplay/
├── include/opendisplay/        # public: od_core.h od_hal.h od_work.h od_structs.h od_protocol.h
├── src/                        # od_pipe.c od_config.c od_display.cpp od_buzzer.c od_led.c
│                               # od_touch.c od_button.c od_sensors.c od_power_policy.c boot_screen.cpp …
├── third_party/{bb_epaper,uzlib}/
├── zephyr/                     # module.yml + CMakeLists.txt + Kconfig  → west/Zephyr module
├── arduino/library.json        # PlatformIO package (srcFilter over ../src, ../third_party)
├── tests/                      # HOST-BUILT: golden wire vectors for auth/CCM/pipe/config/CRC,
│                               # od_work contract tests against a fake clock
└── .github/workflows/          # host tests + one Zephyr build + one PlatformIO build per PR
```

- **Zephyr side:** a proper Zephyr module — `zephyr/module.yml`, `CONFIG_OPENDISPLAY=y`, pulled via the app's `west.yml` (or `ZEPHYR_EXTRA_MODULES` pointing at the submodule). The app repo keeps only `main.c`, board files, `od_hal_zephyr/`, overlays, `prj*.conf`.
- **PlatformIO side:** `lib_deps = symlink://../libopendisplay` (submodule path) with `arduino/library.json` steering `srcFilter`. The app repo keeps `main.cpp`, `od_hal_arduino/`, `wifi_service.cpp`, `wake_button.cpp`, Seeed_GFX glue.
- **The host-built test suite is half the point.** The pipe protocol, CCM, config parser and CRC logic become testable on x86 with byte-exact golden vectors captured from the field — the first time either repo has had that. Every protocol decision in §3 lands with a vector test.

---

## 2. THE EVENT ARCHITECTURE — the core's concurrency contract

### 2.1 The contract in one paragraph

**The core owns no threads.** It defines two *execution lanes* with hard rules, and the platform supplies contexts for them:

- **CMD lane** — command dispatch, crypto, config I/O, the whole display pipeline including the blocking refresh. *May block up to 60 s.* Exactly one context executes CMD at a time (single-consumer by construction).
- **UI lane** — LED/buzzer/button/touch/EPD-keepalive step functions. Every handler must complete in **≤ 50 ms** and never blocks on the CMD lane.

On **Zephyr**, CMD = the `od_cmd` thread (prio 6) and UI = the `od_ui` workqueue (prio 10) — exactly DESIGN_POWER §2.0, unchanged. On **Arduino**, both lanes run on `loop()`, and the contract is honoured by the pump order plus one crucial trick (§2.4). The core cannot tell the difference, and doesn't try: it never assumes preemption between lanes, only that (a) UI work eventually runs, (b) CMD handlers are never re-entered.

### 2.2 The portable deferred-work primitive: `od_work_t`

```c
/* od_work.h */
typedef struct od_work od_work_t;
typedef void (*od_work_fn)(od_work_t *w);

enum od_lane { OD_LANE_CMD, OD_LANE_UI };

struct od_work {
    od_work_fn  fn;
    uint8_t     lane;
    OD_WORK_PLATFORM;      /* Zephyr: struct k_work_delayable + queue ptr
                              Arduino: {od_work_t *next; uint32_t deadline_ms;
                                        volatile uint8_t pending;} */
};

void od_work_init(od_work_t *w, enum od_lane lane, od_work_fn fn);
void od_work_submit(od_work_t *w);                       /* now; ISR-safe */
void od_work_reschedule(od_work_t *w, uint32_t delay_ms);/* re-arms (k_work_reschedule
                                                            semantics); ISR-safe */
bool od_work_cancel(od_work_t *w);                       /* false if executing */
void od_work_cancel_sync(od_work_t *w);                  /* waits; illegal from w's own lane
                                                            and from ISR */
```

**Semantics are pinned to the stricter platform (Zephyr `k_work`):** submit is idempotent while pending; `reschedule` re-arms (the `k_work_schedule` no-op trap of RTOS_COMPARISON §4.4 is *not* exposed); `cancel_sync` guarantees the handler is not running on return — which is what makes "stop the buzzer then kill the PWM" and "suspend touch before the rail moves" (DESIGN_POWER §2.1, §2.3) expressible portably.

- **Zephyr backend:** ~40 lines. `OD_LANE_UI` → `k_work_reschedule_for_queue(&od_ui_wq, …)`; `cancel_sync` → `k_work_cancel_delayable_sync`.
- **Arduino backend:** ~80 lines. A static intrusive singly-linked list per lane, sorted-insert by `deadline = millis() + delay`; `od_work_poll(lane)` runs every item whose deadline passed. ISR-safety: submit/reschedule from ISR only set `pending`+deadline with `__atomic` store; the poll picks them up (bounded staleness = one loop pass, same as today's Arduino reality). `cancel_sync` degenerates to `cancel` — legal, because on a single-threaded platform "not running concurrently" is free.

Everything periodic in the core is expressed **only** in `od_work_t`: buzzer step, LED phase, touch poll/resume, button long-press, EPD keep-alive expiry, MSD refresh, advertising-boost expiry, sleep-candidate evaluation, partial-write watchdog. This single abstraction replaces: two `k_timer`s + polling on nRF54, and `millis()` arithmetic scattered across `buzzerService`/`processLedFlash`/`epdSessionTick`/`powerButtonPoll` on Arduino.

### 2.3 Command & response queues: core-owned, one model

**Both rings live in the core**, statically allocated, sized by `od_port.h`:

```c
/* command ring: SPSC — producer = transport RX context, consumer = CMD lane */
#define OD_CMD_SLOT_SIZE   OD_PORT_CMD_SLOT   /* Nordic: 512 (MTU); ESP32: 256;
                                                 esp32-N4: 256 w/ reduced depth */
#define OD_CMD_DEPTH       (OD_PIPE_MAX_W + 1)/* the ESP32 33 = 32+1 rationale,
                                                 now written down as a law */
/* response ring: 10 slots, flushed BETWEEN commands by the dispatcher —
   ports the load-bearing flushResponseQueueToBle() pattern
   (Firmware/src/main.cpp:331, ARCHITECTURE_FIRMWARE_SISTER §2) into core. */
```

- `od_pipe_rx(data, len)` is the **only** transport entry point: bounds-check, memcpy, `__atomic` release publish, `od_event_signal()`. Callable from the BT RX thread (Zephyr), the Bluefruit callback task (nRF52 — **which finally gives nRF52 the queue it never had**, retiring the entire `pwrmgmLock` priority-inversion class of `display_service.cpp:204-217` at a stroke), the Bluedroid/NimBLE host task, and the ESP32 TCP reader. On full: **drop + count, never block** — the current nRF54 `k_msgq_put(K_MSEC(100))` on the BT RX thread (`opendisplay_pipe.c:1387-1389`) is deleted; with depth = W+1 a full ring means a misbehaving client, and pipe SACK recovers the loss by design.
- The consumer is a single core function:

```c
/* Runs on the CMD lane. Drains up to `budget` commands, flushing the
   response ring between each. Returns when empty or budget exhausted. */
void od_core_cmd_pump(unsigned budget);
```

- The connection-generation counter (`opendisplay_pipe.c:83,1420-1422`) stays, generalised: transports bump it on disconnect so stale queued frames die in the pump.

### 2.4 The blocking EPD refresh without assuming threads

The refresh blocks the **CMD lane**, which the contract permits. What differs per platform is what happens *around* it:

```c
/* od_hal.h — the second half of the lane contract */
void od_cmd_yield(uint32_t ms);
/* "The CMD lane is waiting. Sleep ~ms — and if this platform runs the UI
   lane on the same context, pump it."
   Zephyr backend : k_msleep(ms)                       (od_ui preempts anyway)
   Arduino backend: slice into ≤10ms delays, calling od_work_poll(OD_LANE_UI)
                    each slice  (this is idleDelay()'s soul, relocated)      */
```

`wait_for_refresh()` in core becomes:

```c
bool od_wait_refresh(uint32_t timeout_ms) {
    return od_gpio_wait_level(busy_pin, /*inactive*/ …, timeout_ms,
                              od_cmd_yield_50ms) == 0;
}
```

- **Zephyr:** the backend implements `od_gpio_wait_level` with the IRQ + `k_sem` pattern of DESIGN_POWER §2.5(b) (with the mandatory post-arm level re-check), falling back to a 50 ms poll for P2 pins. CPU sleeps through the refresh.
- **Arduino:** the backend polls at 10–50 ms, invoking the yield callback each slice — **which means the buzzer keeps playing and buttons keep registering during a 5 s refresh on ESP32 for the first time ever.** The reference's worst documented pathology ("the buzzer note drones", RTOS_COMPARISON §2.3) is fixed *by the portability mechanism itself*, not despite it.

Re-entrancy discipline this imposes (documented, and cheap): UI-lane handlers must not touch CMD-lane state — already true (LED/buzzer/button state is theirs alone), and touch is suspended around refresh anyway (`od_work_cancel_sync(&touch_work)` before the rail moves, per DESIGN_POWER §2.3).

### 2.5 Topology on both platforms

```
════════════════ ZEPHYR (nRF54L15 / nRF52840) ═══════════════════════════════
 ISRs: buttons / touch INT / BUSY          GRTC (od_work timeouts)
   │ atomics + od_work_submit / k_sem_give
   ▼
┌────────────┐  od_pipe_rx(): memcpy+publish  ┌─────────────────────────┐
│ BT RX (p8) │ ─────────────────────────────► │ core cmd ring  (33×512) │
└────────────┘  never blocks, drop-on-full    └───────────┬─────────────┘
                                                          │ od_event_signal
              ┌───────────────────────────────────────────▼───────────────┐
              │ od_cmd thread  (prio 6, 8 KB)   ["CMD lane"]              │
              │   while(1){ od_event_wait(); od_core_cmd_pump(DEPTH); }   │
              │   dispatch → crypto → config/NVS → display → refresh      │
              │   BUSY wait = IRQ + k_sem  (CPU asleep, up to 60 s)       │
              │   responses → core resp ring → od_notify → bt_gatt_notify │
              └───────────────────────────────────────────────────────────┘
┌───────────────────────────┐ ┌────────────────────┐ ┌────────────────────┐
│ od_ui workqueue (p10,2KB) │ │ sysWQ (-1, coop)   │ │ od_cs wq (p12)     │
│  ["UI lane"] all od_work: │ │ adv restart, MSD   │ │ CS setup (10 s     │
│  buzzer/LED/button/touch/ │ │ publish, sleep-    │ │ k_sem_take lives   │
│  EPD-keepalive            │ │ candidate work     │ │ here, off sysWQ)   │
└───────────────────────────┘ └────────────────────┘ └────────────────────┘
 main(): init, arm od_work items, return.        idle: tickless WFI →
                                                 sys_poweroff() per policy

════════════════ ARDUINO (ESP32 / nRF52 until Stage 4) ══════════════════════
┌──────────────────────────────┐   ┌──────────────────────────┐
│ BLE host task                │   │ TCP reader (ESP32 WiFi)  │
│ (Bluedroid/NimBLE/Bluefruit) │   └──────────┬───────────────┘
│  onWrite → od_pipe_rx() ─────┼──────────────▼──────────────┐
└──────────────────────────────┘   │ core cmd ring (33×256)   │
 ISRs: buttons → atomics +         └──────────┬──────────────┘
       od_work_submit (flag)                  │
┌─────────────────────────────────────────────▼──────────────────────────┐
│ loop()  [FreeRTOS task, prio 1]                                        │
│   od_core_cmd_pump(DEPTH);      ← CMD lane (may block inside refresh:  │
│                                    od_cmd_yield() pumps UI each slice) │
│   od_work_poll(OD_LANE_UI);     ← UI lane: buzzer/LED/button/touch     │
│   od_work_poll(OD_LANE_CMD);    ← msd refresh, sleep-candidate, wdogs  │
│   platform_service();           ← wifi reconnect, power latch poll     │
└────────────────────────────────────────────────────────────────────────┘
```

Same core object files, byte-identical protocol behaviour, two ~120-line scheduling shims.

---

## 3. Protocol convergence — the canonical OpenDisplay protocol

**Rollout mechanism first, because every fix uses it.** Add one opcode:

> **`0x0046` GET_CAPABILITIES** → `{00,46, proto_rev:1, caps:4 LE}`. Caps bits: `PIPE_WRITE`, `DFU_BOOTLOADER` (0x51-style reboot-to-bootloader), `DFU_INLINE` (0x60-family, §4), `DEEP_SLEEP`, `NFC`, `BUZZER_QUARTERTONE`, `GRAY16_4BPP`, `AUTOCOMPLETE_0x71`, `CHANNEL_SOUNDING`. No response within timeout ⇒ legacy device, rev 0, assume reference-nRF52 behaviour.

Do **not** graft capabilities onto `0x0043` — its `{00,43,major,minor,shalen,sha…}` shape is fielded on every device and client (`opendisplay_pipe.c:587-608`); trailing bytes are a parser-compat gamble for zero benefit. `0x0043` remains the version oracle; `0x0046` is the behaviour oracle.

### D3 — `0x0082` collision (PIPE_WRITE_END vs NFC). **Canonical: PIPE_WRITE_END.**
- **Who changes: nRF54 only.** Move NFC to **`0x0090`** (not `0x0083` — reserve `0x0083–0x008F` for pipe-family growth). One line in `opendisplay_protocol.h:27` plus the dispatch entry.
- **Rollout risk: zero.** The NFC backend is a stub that unconditionally NACKs (`opendisplay_ble.c:735-751`) — no client on earth depends on `0x0082`-as-NFC working, because it has never worked. Ship before (or with) PIPE_WRITE; it is the prerequisite for gap #1.

### D1 — Buzzer frequency mapping. **Canonical: the quarter-tone table** (`f = 13.75·2^(idx/24)` centi-Hz, idx 120 = A4 = 440 Hz, octave-fold into [117,234]), 30 000 ms cap.
- **Who changes: nRF54 only.** The linear ramp (`opendisplay_buzzer.c:74-82`) is a port of a pre-music firmware; the reference table is what py-opendisplay/HA melodies are authored against, and the `BuzzerNote` enum values *are* wire indices (`buzzer_control.cpp:19-113`, `buzzer_control.h:27-92`). The table + fold + cap move into core `od_buzzer.c`.
- **Rollout:** pure bug fix, no negotiation needed; old nRF54 units simply play correctly after update. `BUZZER_QUARTERTONE` caps bit lets the toolbox warn on stale firmware.

### D2 — GRAY16 bpp. **Canonical: 4 bpp.**
- **Who changes: the reference.** Its `getBitsPerPixel()` has *no case* for scheme 6 — a fallthrough to 1 (`display_service.cpp:1404-1415`), which is self-evidently a bug: 16 grey levels don't fit in 1 bit. The nRF54 side documents that py-opendisplay emits 4 bpp (`opendisplay_display_color.c:16-18`). Add `case 6: return 4;`.
- **Rollout risk: zero.** No client can have been using GRAY16 successfully against the reference — a 4× size mismatch fails the `zlib size != total` check (`display_service.cpp:1819`) or renders garbage. Fixing a feature that never worked cannot regress anyone.

### D4 — `0x0051` ENTER_DFU lying. **Canonical: never ACK a DFU you won't perform.**
- **Immediate (who changes: nRF54):** return `{0xFF, 0x51}` until real DFU exists. One-line honesty fix; py-opendisplay already handles NACKs.
- **Canonical long-term semantics:** `0x0051` = "reboot into the platform's bootloader-resident DFU", advertised by `DFU_BOOTLOADER`; the new inline path (§4) is `DFU_INLINE`. A device may implement either, both, or neither — the caps word tells the client which, ending the guessing.
- Related honesty rule adopted from the port's own good behaviour: unsupported opcodes answer nothing or NACK, never fake-ACK (the `0x0052` precedent, `opendisplay_pipe.c:1248-1254`).

### Pipe-partial endianness (0x80 START `old_etag` **LE** vs 0x76/0x72/0x82 etags **BE**, `ARCHITECTURE_FIRMWARE_SISTER §4.3`).
**Decision: freeze it as a documented wart in PIPE v1; do not burn a wire change on it.** The field is version-gated already — START carries `ver` and the device echoes VER in its response (`display_service.cpp:2405-2408`), so **PIPE v2, whenever it exists for a real reason, normalises every etag to BE** and devices accept both versions during transition. Changing v1 now would break the only deployed pipe clients (reference ESP32 + py-opendisplay) to fix an inconsistency no client actually trips over — each field is read with the correct endianness today; the danger is only to future implementers, which the canonical spec document neutralises.

### D5–D9, resolved in passing by the core extraction
- **D5 (name in scan-response vs ADV):** canonical = **name in ADV packet** (both reference targets do it; passive scanners depend on it). nRF54 platform file changes.
- **D6 (compressed-write gate):** canonical = **reference leniency** — accept `len>=4` as compressed regardless of `transmission_modes` bit0; the bit becomes advisory (advertisement, not enforcement). Removes a field-config landmine; core implements it once.
- **D7 (0x71 auto-complete):** canonical = **auto-complete** (reference behaviour; deployed clients rely on it). Core implements; caps bit for the transition.
- **D8 (reserved-field drift):** resolved by the single canonical `od_structs.h` with DESIGN_POWER §1.8's restorations + `_Static_assert` guards.
- **D9 (pin encoding):** *intentionally not converged* — pin bytes are SoC-namespace values decoded by the backend. The canonical spec states loudly: **config blobs are not portable across SoC families**; the toolbox must key pin fields off `board_type`.

**Deliverable for this section:** a `PROTOCOL.md` in `libopendisplay` — the canonical wire spec (opcodes, endianness per field, error codes, caps bits) with golden vectors in `tests/`. From Stage 1 onward, *the library is the spec*; the repos stop being able to diverge because they share the object code.

---

## 4. OTA / DFU across all three targets

| Target | Bootloader | Strategy |
|---|---|---|
| **nRF52840** | **Keep Adafruit UF2** (feasibility §B.1: upstream `nrf52840_partition_uf2_sdv7.dtsi` matches our shipped `s140_7.3.0` layout exactly; ZMK production precedent). | `0x0051` stays a real bootloader jump: GPREGRET `0xB1` (`Firmware/src/device_control.cpp:679-680`; under Zephyr, via the `nordic,nrf-gpregret` retained-mem driver) + reset → bootloader-resident BLE DFU / USB UF2. Works unchanged across the Arduino→Zephyr swap because **the DFU logic lives in the bootloader, not the app**. Caps: `DFU_BOOTLOADER`. |
| **nRF54L15** | **Adopt MCUboot now, via NCS sysbuild + MCUmgr SMP-over-BLE.** | The standard, production NCS path on nRF54L. Do it **before any volume ships** — the feasibility study's sharpest lesson (§E.2 #2) is that a fleet with no OTA is a one-way door; nRF54 is the only target where the door is still open. Dual slot in the 1.5 MB RRAM; image confirm/rollback on. `0x0051` then ACKs truthfully. Caps: `DFU_INLINE` (via SMP). |
| **ESP32** | Espressif 2nd-stage (unchanged). | **New opcode family `0x0060/0x0061/0x0062` FW_UPDATE START/DATA/END, reusing the PIPE sliding-window engine** (same W/N/SACK negotiation, same auth+CCM session) with the sink being `esp_ota_begin/write/end` instead of the panel. Cost is small precisely because the pipe engine is now a core module. Requires the OTA partition table (`ota_0`/`ota_1`/`otadata`) on **new and factory-reflashed units only**. Existing field units have no OTA today and cannot gain it remotely — that is the status quo, not a regression; document it and move the fleet forward at natural touchpoints. Caps: `DFU_INLINE`. |

Client story: the toolbox reads `0x0046`; `DFU_BOOTLOADER` ⇒ send `0x0051` and switch to the platform DFU tool; `DFU_INLINE` ⇒ stream over the authenticated OpenDisplay session it already has. One decision tree, three targets.

---

## 5. Migration plan — staged, independently shippable

| Stage | Weeks | Delivers | Parity gaps closed (FEATURE_PARITY §3) |
|---|---:|---|---|
| **0. Protocol hygiene** — ship *first*, no refactor needed | **1** | nRF54: NFC→`0x0090`, `0x0051`→NACK, quarter-tone table + 30 s cap, ADV name placement, D6 gate removed. Reference: GRAY16→4 bpp. Both: `0x0046` GET_CAPS. | **#2, #3 (honest), #4, #5**, D5, D6 |
| **1. Extract `libopendisplay`** | **3–5** | New repo; `nrf54_*`→`od_hal_*`; `od_work`/lane contract (§2); host test suite + golden vectors; `PROTOCOL.md`; nRF54 app becomes first consumer **and** simultaneously adopts the Zephyr topology (od_cmd/od_ui/od_cs, mutexes per DESIGN_POWER §2.0) — the refactor and the topology are the same surgery, do them once. | #12, #13, #15, #16, #17, #20, #23 fall out of the lane/ISR redesign |
| **2. PIPE_WRITE in core** | **2–3** | Single pipe implementation ported from `display_service.cpp:2114-2600` into `od_pipe.c`; nRF54 gains pipe; ring depth = W+1; `0x71` auto-complete. | **#1, #6, #10, #17** |
| **3. `od_hal_arduino` + rewire `Firmware/`** | **4–6** | ESP32 + nRF52 consume the core; ~9 kLOC duplicated logic deleted; nRF52 gains the command queue (pwrmgmLock class retired); ESP32 buzzer keeps playing through refreshes (§2.4). Both app repos ship from the same core SHA. | Convergence itself; D7/D2 land on reference via core |
| **4. nRF52840 → Zephyr** | **3–5** | Backend swap on the UF2/sdv7 layout (feasibility §B); Bluefruit/S140 retired; flat pin decode; LittleFS kept for config-file continuity (feasibility §B.2.4). | — (risk retirement) |
| **5. nRF54 power + DFU** | **5–8** | DESIGN_POWER Part 1 in full: retention gate, DOZE-vs-OFF (measure first), latch support, wake buttons, EPD keep-alive session, config field restorations; MCUboot + SMP. | **#3 (real), #7, #8, #9, #14, #19** |
| **6. ESP32 OTA** | **2–3** | `0x006x` inline DFU over the core pipe engine; OTA partition table for new units. | reference OTA gap |
| *(parallel, any time)* panels 0x47–0x4C into bb_epaper; AXP2101 | 2–3 | | #11, #22 |

**Total ≈ 20–31 weeks**, consistent with the feasibility Path-2 estimate plus the OTA additions. **Stage 0 ships first** because the four divergences are live landmines for every client written today, cost a week, and require none of the refactor. Stages 1–2 come before touching `Firmware/` because the library must prove itself on the codebase it was extracted from before a second consumer multiplies the blast radius.

---

## 6. Risks — and the Seeed_GFX counterfactual

| Risk | Severity | Mitigation |
|---|---|---|
| **Two-build-system tax on the library** (the feasibility doc's own counter-argument) | Medium, chronic | The library's CI builds Zephyr + PlatformIO + host tests on every PR; app repos pin by SHA and bump deliberately. The tax exists today across two whole repos; this shrinks it to one library's interface. |
| **`od_work` semantic drift on Arduino** (millis granularity, cancel_sync degeneration, ISR-submit staleness of one loop pass) | Medium | The contract tests in `tests/` run the same scenarios against the fake-clock Arduino backend and (in CI, native_sim) the Zephyr backend. Staleness bound documented: UI latency ≤ one loop pass, same as shipping behaviour. |
| **UI-pump re-entrancy on Arduino** (`od_cmd_yield` runs UI handlers inside a CMD handler) | Medium | Enforced rule: UI handlers touch only their own module state; touch is cancel_sync'd around refresh. One assert (`od_in_cmd_yield`) catches violations in debug builds. |
| **nRF54L pin-domain constraints leaking into core** (P2 no-SENSE/no-PWM, PWM P1-only) | Low | `od_gpio_caps()` keeps it in the backend; config-parse-time validation rejects impossible pin assignments with a log, per DESIGN_POWER §2.1. |
| **Ring RAM on classic ESP32** (`PIPE_SMALL_DRAM_WINDOW` already exists) | Low | `od_port.h` per-target depth/slot; the W+1 law still holds at W=16. |
| **NVS 4 KB blob ceiling on Nordic** | Low | Already at the practical limit; if it pinches, LittleFS-on-Zephyr (feasibility §B.2.4) is the escape hatch behind the unchanged `od_store` API. |
| **Seeed_GFX/IT8951** | Contained | Never enters the library (§1.4); it is an ESP32-platform panel backend behind `od_panel_ops`, selected by `panel_ic_type` at runtime exactly as today. Its 29.4k LOC are quarantined, not solved. |

**If the 13.3″ Seeed_GFX panel family were dropped:** surprisingly little of *this* architecture changes — which is the point of §1.4 — but the strategic horizon moves:

1. **The display path becomes 100% single-source** (bb_epaper covers every remaining panel ID), and the ESP32 PSRAM dependency disappears entirely (feasibility §A.7: PSRAM is used only inside Seeed_GFX).
2. **ESP32-on-Zephyr becomes *thinkable* again** — the full-port estimate loses its 6–12+-week unbounded line item and its single killer risk. But I would **still not do it**: the remaining blockers (blob-based experimental WiFi, +50 KB heap, the no-OTA partition one-way door on the existing fleet, per-SoC crossbar work to keep hardware SPI) survive intact, and with the core library in place the *prize* has shrunk to "one BLE-glue file instead of two". The honest re-evaluation trigger is the feasibility doc's own: Espressif declaring S3/C6 WiFi production-stable **and** the ESP32 fleet having inline OTA (Stage 6) so a Zephyr image could ship with a rollback path. Then run the C3 spike.
3. The nearer-term simplification I *would* take: with Seeed_GFX gone, a later move to **ESP-IDF-native + libopendisplay** (deleting the Arduino layer, keeping the IDF underpinnings) becomes a bounded 3–4-week job per SoC, and it removes the `String`/heap-churn class of problems without any of the Zephyr risks.

---

## Appendix — decisions in one table

| Question | Decision |
|---|---|
| Full Zephyr unification? | **No** — core library; Zephyr on Nordic only (feasibility verdict accepted). |
| Core owns threads? | **No.** Two lanes (CMD may block 60 s; UI ≤50 ms), platform supplies contexts. |
| Deferred work | `od_work_t`, `k_work_reschedule` semantics, Zephyr wq / Arduino sorted-poll backends. |
| Queues | Core-owned SPSC cmd ring (depth = PIPE_MAX_W+1, slot per-port) + 10-slot response ring flushed between commands; producers never block. |
| Blocking refresh | CMD lane blocks; `od_gpio_wait_level` (IRQ+sem on Zephyr, poll elsewhere) + `od_cmd_yield` pumps UI on shared-thread platforms. |
| `0x0082` | PIPE_WRITE_END canonical; NFC → `0x0090` (nRF54 changes, zero risk). |
| Buzzer | Quarter-tone table + 30 s cap canonical (nRF54 changes). |
| GRAY16 | 4 bpp canonical (reference changes; feature never worked there). |
| `0x0051` | NACK until real; caps-advertised DFU flavours; never fake-ACK. |
| Pipe etag endianness | Frozen wart in v1, documented; BE-normalised only in a future PIPE v2. |
| Capability discovery | New `0x0046` GET_CAPS; `0x0043` untouched. |
| DFU | nRF52: keep Adafruit UF2 + `0x0051`. nRF54: MCUboot + SMP **now**. ESP32: `0x006x` inline DFU over the core pipe engine, OTA partitions on new units. |
| Library home | New repo, git submodule pinned by SHA in both app repos; Zephyr module + PlatformIO package; host-built golden-vector CI. |
| First ship | Stage 0 protocol-hygiene release, ~1 week, before any refactor. |
