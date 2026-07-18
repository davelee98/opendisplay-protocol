# Repository & Source-Tree Structure — One Core, Four Targets

> Structure proposal, 2026-07-14. Documentation only; no code changed.
> Extends [DESIGN_UNIFIED_ARCHITECTURE.md](DESIGN_UNIFIED_ARCHITECTURE.md) §1.5 from three targets to four, adding **Silicon Labs EFR32BG22** (`Firmware_Silabs`, Simplicity SDK + SLC + bare-metal superloop).
> Accepted inputs: the `libopendisplay` core extraction (DESIGN_UNIFIED_ARCHITECTURE), the no-full-Zephyr verdict ([DESIGN_ZEPHYR_UNIFICATION_FEASIBILITY.md](DESIGN_ZEPHYR_UNIFICATION_FEASIBILITY.md)), the Zephyr `od_cmd`/`od_ui` topology ([DESIGN_POWER_AND_EVENTS_ZEPHYR.md](DESIGN_POWER_AND_EVENTS_ZEPHYR.md)).
> **Build mechanics** (PlatformIO capabilities, Zephyr modules, SLC components, CI) are covered separately in [DESIGN_BUILD_TOOLCHAINS.md](DESIGN_BUILD_TOOLCHAINS.md); this document specifies *structure*.

---

## 0. What the Silabs spot-check found (facts the earlier docs didn't have)

The BG22 tree is a flat repo-root layout, ~5.4 kLOC of app code, and it is exactly what was suspected: **a third hand-maintained copy of the nRF54 core files, drifted.**

| File | Silabs LOC | nRF54 LOC | diff lines | Nature of drift |
|---|---:|---:|---:|---|
| `opendisplay_pipe.c` | 1204 | 1425 | 423 | Silabs is an older subset (no touch/buzzer/sensor opcodes, no CS) **plus** unique additions: NFC endpoint incl. chunked write, BGAPI long-write reassembly (`opendisplay_pipe.c:36-38,1115-1124`), `sl_bt` response paths |
| `opendisplay_config_parser.c` | 475 | 696 | 239 | missing newer TLV fields |
| `opendisplay_structs.h` | 220 | 315 | 99 | struct drift — the exact D8 class of bug |
| `opendisplay_display_color.c` | 64 | 73 | 51 | diverged |
| `opendisplay_epd_map.c` | 75 | 94 | 19 | near-identical |

Structural facts that matter:

1. **Main loop is the SLC bare-metal superloop**: `main.c:68-80` — `while(1){ sl_main_process_action(); app_process_action(); sl_power_manager_sleep(); }`. No RTOS, no threads. `app_bm.c:37-68` is a critical-section counter (`app_proceed()`/`app_is_process_required()`) — a hand-rolled ISR-safe event flag, i.e. **`od_event_signal()` already exists there under another name.**
2. **BLE is event-driven through the same loop**: BGAPI events pumped by `sl_main_process_action()` → `sl_bt_on_event()` (`app.c:68-105`) → `opendisplay_pipe_handle_gatt_event()` (`opendisplay_pipe.c:1176-1204`). **Commands dispatch synchronously inside the stack event handler** (`opendisplay_pipe.c:1153-1156`) — there is no command queue.
3. **The blocking refresh blocks everything**: `wait_for_refresh()` polls BUSY with `sl_sleeptimer_delay_millisecond(50)` for up to 60 s (`opendisplay_display.cpp:558-573`, `:880`). During that time `opendisplay_ble_process()` (`opendisplay_ble.c:1880-1930` — LED stepping, MSD refresh, adv boost, connection-timeout close, pending DFU/EM4) is starved and BGAPI events queue in the stack. Same pathology class as the Arduino reference's "buzzer drones through refresh".
4. **Deferred dangerous actions already use the pending-flag-at-top-of-loop pattern**: `s_pending_dfu`/`s_pending_deep_sleep` set in handlers, executed from `opendisplay_ble_process()` after the connection closes (`opendisplay_ble.c:1871-1927`, ending in `EMU_EnterEM4()`). This is precisely the core's power-policy/executor split — **the BG22 port independently invented it.**
5. **Silabs has REAL NFC at `0x0082`** — `CMD_NFC_ENDPOINT 0x0082` (`opendisplay_protocol.h:17`) with a working TNB132M NDEF driver (`opendisplay_ble.c:50-63`, `:1943-1975`). **This invalidates DESIGN_UNIFIED §3-D3's "rollout risk: zero" claim**, which rested on NFC-at-0x0082 being an unconditional-NACK stub. It's a stub on nRF54; on BG22 it works. See §5.
6. **Pin encoding**: same `(port<<4)|pin` nibble scheme as nRF54, decoded to `GPIO_Port_TypeDef` (`opendisplay_display.cpp:58-66`) — the backend-decoder model holds unchanged.
7. **bb_epaper is vendored a third time**, with two Silabs-only IO shims `silabs_efr32_io.inl` + `silabs_bbep_busy.inl` selected by `__SILABS_BG22__`.
8. **Build**: SLC generates a CMake target `slc` (`cmake_gcc/opendisplay-bg22.cmake`); the hand-owned wrapper `cmake_gcc/CMakeLists.txt` already demonstrates the extension mechanism we need — `target_sources(slc PRIVATE …)` / `target_include_directories(slc PUBLIC …)` for vendored code (`cmake_gcc/CMakeLists.txt:42-46`). C++ is already enabled (`LANGUAGES C CXX ASM`, `:7`).
9. **Hard RAM wall: 32 KB.** `autogen/linkerfile.ld:35`: `RAM LENGTH = 0x7ffc`. The core's default command ring (33×512 B ≈ 17 KB) is **physically impossible** on BG22. `od_port.h` per-target sizing is not a nicety here; it is existential.
10. **Silabs already has real DFU** (Gecko bootloader, `.gbl` OTA artifact generated post-link) **and real deep sleep** (EM4 + button/NFC-field wake). It is *ahead* of nRF54 on both.
11. **Feature subset**: no buzzer, touch, sensors, battery gauge, or boot screen (it has `qr/`). The core must be consumable à la carte.

---

## 1. The directory layout

### 1.1 Decision: one new core repo, three app repos, submodule pins — not a monorepo

DESIGN_UNIFIED §1.5's decision extends cleanly and gets *stronger* with a fourth target: `Firmware_Silabs` vendors an entire Simplicity SDK in-tree (`simplicity_sdk_2025.12.2/`), `Firmware` vendors Seeed_GFX (29.4 kLOC), `Firmware_NRF54` rides an NCS workspace. A monorepo would weld three unrelated multi-hundred-MB SDK footprints together and force lock-step releases on repos that today ship independently. **The core is the only thing all four targets share, so the core is the only thing that gets a new repo.** Each app repo pins it as a git submodule; the pinned SHA is the compatibility record, and `0x0043`'s build-id string should embed it.

### 1.2 `libopendisplay` — the core repo

```
libopendisplay/                          # NEW repo. The ONLY home of protocol + product logic.
├── include/opendisplay/                 # public API — everything extern "C"-guarded
│   ├── od_core.h                        #   init/lifecycle, od_core_cmd_pump(budget)
│   ├── od_hal.h                         #   the ~30-function platform contract (DESIGN_UNIFIED §1.2)
│   ├── od_work.h                        #   od_work_t + lanes
│   ├── od_port.h                        #   per-target knobs: OD_PORT_CMD_DEPTH/SLOT, feature gates,
│   │                                    #   static-vs-heap zlib window  ← BG22's 32 KB lives or dies here
│   ├── od_protocol.h                    #   canonical opcode space — single source of D3/D5–D9 truth
│   ├── od_structs.h                     #   wire structs + _Static_assert size guards (kills D8)
│   └── od_panel.h                       #   od_panel_ops seam (bb_epaper in-core; Seeed_GFX registers from ESP32 app)
├── src/                                 # C99 core + two C++ islands; no String/std::/heap
│   ├── od_pipe.c                        #   ← nRF54 pipe ∪ Firmware PIPE_WRITE ∪ Silabs NFC-endpoint logic
│   ├── od_config_parser.c  od_factory_config.c
│   ├── od_display.cpp  od_display_color.c  od_epd_map.c        # C++ island 1
│   ├── boot_screen.cpp  logo_bitmap.h  qr/                     # C++ island 2 — feature-gated
│   ├── od_buzzer.c  od_led.c  od_button.c  od_touch.c          # FSMs on od_work_t only
│   ├── od_i2c_soft.c  od_sensor_sht40.c  od_sensor_bq27220.c  od_battery.c
│   ├── od_msd.c                         #   MSD payload build + suppress-if-unchanged (submission = HAL)
│   ├── od_nfc.c                         #   NFC ENDPOINT wire logic, hoisted from Firmware_Silabs
│   └── od_power_policy.c                #   decides when; never executes sleep
├── ports/                               # ONLY the lane machinery — one shim per scheduler family,
│   │                                    # kept in-repo so the contract tests cover every shim
│   ├── zephyr/    od_lanes_zephyr.c     #   ~120 LOC: od_cmd thread + od_ui workqueue + k_sem event
│   ├── arduino/   od_lanes_arduino.cpp  #   ~120 LOC: sorted-poll lists + loop() pump order + sliced yield
│   ├── silabs_bm/ od_lanes_silabs.c     #   ~150 LOC: sorted-poll + sl_sleeptimer wake + app_proceed bridge (§3)
│   └── posix/     od_lanes_posix.c      #   host-test backend, fake clock
├── third_party/
│   ├── bb_epaper/                       # ONE canonical copy; IO shim retargeted to od_hal —
│   │                                    #   silabs_efr32_io.inl / nrf54_zephyr_io.inl / arduino_io.inl collapse
│   └── uzlib/
├── zephyr/                              # module.yml + CMakeLists.txt + Kconfig  → consumed by west/NCS
├── arduino/library.json                 # PlatformIO package
├── cmake/opendisplay.cmake              # plain-CMake source+include list → Silabs wrapper AND host tests
├── tests/                               # HOST-BUILT: golden wire vectors (auth/CCM/pipe/config/CRC),
│                                        #   od_work contract suite run against posix + each shim
├── docs/PROTOCOL.md                     # the canonical wire spec; caps bits incl. NFC
└── .github/workflows/ci.yml             # host tests + Zephyr + PlatformIO + arm-gcc compile per PR
```

Two deliberate deviations from DESIGN_UNIFIED §1.5's sketch:

- **`ports/` moves into the library.** The lane/`od_work` shims are the part of the platform code whose *semantics* the core relies on (idempotent submit, reschedule re-arm, cancel_sync guarantees). They are tiny (~120–150 LOC each), and with four of them the odds of one drifting out of contract are too high to leave untested. **Everything else platform (pins, buses, storage, crypto glue, sleep executors, BLE transports) stays in the app repos** — that code depends on full SDKs the library CI shouldn't drag in.
- **`cmake/opendisplay.cmake`** is the third packaging face: nothing but `set(OPENDISPLAY_SOURCES …)` + include dirs — the lowest-tech consumption mechanism, used by both the Silabs wrapper and the host test build.

### 1.3 The three app repos after migration

```
Firmware_NRF54/                          # nRF54L15 / LM20 — NCS + west + sysbuild
├── external/libopendisplay/             # submodule (west.yml or ZEPHYR_EXTRA_MODULES)
├── src/
│   ├── main.c                           # init, arm od_work items, start lanes, return
│   ├── board_nrf54.c                    # board/product glue
│   ├── od_hal_zephyr/                   # gpio (nibble decode), bit-bang SPI (→ crossbar HW SPI later,
│   │                                    #   zero core change), i2c, settings/NVS, PSA glue,
│   │                                    #   sys_poweroff executor, retained_mem gate
│   ├── ble_transport_zephyr.c           # GATT define, adv, conn cbs; onWrite → od_pipe_rx()
│   └── opendisplay_cs.c                 # Channel Sounding — stays platform-only forever
├── zephyr/                              # CMakeLists.txt, app.overlay, prj*.conf
└── docs/  scripts/  build.sh  flash.sh

Firmware/                                # nRF52840 + ESP32-{S3,C3,C6,classic} — PlatformIO + Arduino
├── external/libopendisplay/             # submodule; lib_deps = symlink://external/libopendisplay
├── platformio.ini                       # env matrix unchanged
├── src/
│   ├── main.cpp                         # loop(): od_core_cmd_pump(); od_work_poll(UI); od_work_poll(CMD); platform_service()
│   ├── od_hal_arduino/                  # common.cpp + esp32.cpp + nrf52.cpp (flat 0-47 pin decode)
│   ├── ble_transport_bluefruit.cpp      # nRF52: callbacks → od_pipe_rx() (gains the queue it never had)
│   ├── ble_transport_esp32.cpp          # Bluedroid/NimBLE glue → od_pipe_rx()
│   ├── wifi_service.cpp  wake_button.cpp  power_latch.cpp     # ESP32-only, stay forever
│   └── panel_seeed_gfx.cpp              # registers od_panel_ops for panel_ic_type 3000/3001
├── lib/Seeed_GFX/                       # stays vendored HERE; never enters the library
└── variants/  boards/  scripts/  bin/

Firmware_Silabs/                         # EFR32BG22 — Simplicity SDK + SLC + CMake wrapper
├── external/libopendisplay/             # submodule
├── opendisplay-bg22.slcp  .pintool  .slps   # SLC project unchanged — SLC never learns the library exists
├── cmake_gcc/
│   ├── CMakeLists.txt                   # + include(external/libopendisplay/cmake/opendisplay.cmake)
│   │                                    #   + target_sources(slc PRIVATE ${OPENDISPLAY_SOURCES}) —
│   │                                    #   the exact pattern already proven at cmake_gcc/CMakeLists.txt:42-46
│   └── opendisplay-bg22.cmake           # SLC-generated, untouched
├── app/                                 # (today: repo root — gains a folder, see §4)
│   ├── main.c  app.c  app_bm.c          # superloop + BT event fan-out, ~as today
│   ├── od_hal_efr32/                    # gpio (nibble→GPIO_Port decode), sleeptimer time, NVM3 store,
│   │                                    #   PSA glue, EM4 executor, GBL-DFU executor
│   ├── ble_transport_bgapi.c            # adv build, BGAPI long-write reassembly, sl_bt events →
│   │                                    #   od_pipe_rx() ENQUEUE-ONLY (§3.2)
│   └── nfc_tnb132m.c                    # NFC hardware driver behind core's od_nfc HAL ops
├── config/  autogen/  simplicity_sdk_2025.12.2/  bootloader-artifact/
└── build-and-flash.sh
```

What gets **deleted** is the point: nRF54 loses ~10 kLOC of `src/opendisplay_*` + `third_party/` + `qr/`; `Firmware` loses ~9 kLOC; Silabs loses its eight `opendisplay_*` copies + `third_party/{bb_epaper,uzlib}` + `qr/` (~4 kLOC of its 5.4k).

---

## 2. THE DIAGRAM — lanes, core, seam, and how each platform fulfils the contract

```
╔═══════════════════════════════════════════════════════════════════════════════════════════╗
║                      THE LANE CONTRACT  (defined by core — core owns NO threads)           ║
║                                                                                            ║
║   ┌── CMD lane ─────────────────────────────┐   ┌── UI lane ──────────────────────────┐    ║
║   │ dispatch → auth/CCM → config → display  │   │ buzzer / LED / button / touch poll  │    ║
║   │ pipeline → BLOCKING refresh (≤ 60 s)    │   │ EPD keep-alive / MSD tick / policy  │    ║
║   │ single consumer, never re-entered       │   │ every handler ≤ 50 ms, never blocks │    ║
║   └───────────────▲─────────────────────────┘   └──────────────▲──────────────────────┘    ║
║        od_core_cmd_pump(budget)                    od_work_t  (submit/reschedule/cancel)   ║
╚════════╤══════════════════════════════════════════════════════╤════════════════════════════╝
         │                                                      │
╔════════▼══════════════════════════════════════════════════════▼════════════════════════════╗
║                            l i b o p e n d i s p l a y   (core, ~10 kLOC)                  ║
║                                                                                            ║
║   od_pipe.c ── framing · auth/CMAC · CCM · replay · PIPE_WRITE · SACK · NFC endpoint       ║
║   od_config_parser.c · od_factory_config.c · od_structs.h (canonical, _Static_assert'd)    ║
║   od_display.cpp ── plane split · zlib inflate · quirks · ETag · PWR session FSM           ║
║        └─► od_panel_ops ──► bb_epaper (in core) ▪ Seeed_GFX (registered by ESP32 app only) ║
║   od_buzzer/od_led/od_button/od_touch/od_sensors/od_battery/od_msd ── FSMs on od_work_t    ║
║   od_power_policy.c ── decides WHEN to sleep; NEVER executes it                            ║
║                                                                                            ║
║   core-owned SPSC cmd ring (depth/slot from od_port.h) ◄── od_pipe_rx()  [any RX context]  ║
║   core-owned response ring ──► od_notify()             [flushed between commands]          ║
╚═════════════════════════════════╤══════════════════════════════════════════════════════════╝
                                  │
      ═══════════════════════ od_hal.h  +  od_work.h  (~30 fns, the ONLY seam) ═══════════════
        gpio/caps/irq/wait_level · spi · i2c · pwm · time · store · aes-ecb/cmac/rng ·
        od_notify/od_link_connected/od_adv_set_msd · od_event_signal/wait · od_cmd_yield · log
                                  │
   ┌──────────────────────────────┼───────────────────────────────┬───────────────────────────┐
   ▼                              ▼                               ▼                           ▼
╔═════════════════════════╗ ╔═════════════════════════╗ ╔═════════════════════════╗ ╔═════════════════════════╗
║ ZEPHYR (nRF54L15,       ║ ║ ARDUINO-nRF52 (nRF52840 ║ ║ ARDUINO-ESP32           ║ ║ SILABS BARE-METAL       ║
║  nRF52840 after Stage 4)║ ║  until Stage 4)         ║ ║  (S3/C3/C6/classic)     ║ ║  (EFR32BG22)            ║
╟─────────────────────────╢ ╟─────────────────────────╢ ╟─────────────────────────╢ ╟─────────────────────────╢
║ CMD lane =              ║ ║ CMD lane =              ║ ║ CMD lane =              ║ ║ CMD lane =              ║
║  od_cmd THREAD (prio 6) ║ ║  loop() pass, phase 1:  ║ ║  loop() pass, phase 1:  ║ ║  app_process_action()   ║
║  while(1){              ║ ║  od_core_cmd_pump()     ║ ║  od_core_cmd_pump()     ║ ║  phase 1:               ║
║   od_event_wait();      ║ ║                         ║ ║                         ║ ║  od_core_cmd_pump()     ║
║   od_core_cmd_pump(); } ║ ║ UI lane =               ║ ║ UI lane =               ║ ║                         ║
║                         ║ ║  loop() pass, phase 2:  ║ ║  loop() pass, phase 2:  ║ ║ UI lane =               ║
║ UI lane =               ║ ║  od_work_poll(UI)       ║ ║  od_work_poll(UI)       ║ ║  phase 2:               ║
║  od_ui WORKQUEUE        ║ ║                         ║ ║                         ║ ║  od_work_poll(UI)       ║
║  (prio 10) — preempts   ║ ║ 60 s refresh:           ║ ║ 60 s refresh:           ║ ║  + one-shot sl_sleep-   ║
║  the blocked refresh    ║ ║  od_cmd_yield() slices  ║ ║  od_cmd_yield() slices  ║ ║  timer armed at next    ║
║                         ║ ║  ≤10 ms delay +         ║ ║  ≤10 ms delay +         ║ ║  od_work deadline →     ║
║ 60 s refresh:           ║ ║  od_work_poll(UI)       ║ ║  od_work_poll(UI)       ║ ║  app_proceed()          ║
║  BUSY = IRQ + k_sem,    ║ ║  → buzzer keeps playing ║ ║  → buzzer keeps playing ║ ║                         ║
║  CPU sleeps through it  ║ ║                         ║ ║                         ║ ║ 60 s refresh:           ║
║                         ║ ║ RX producer =           ║ ║ RX producers =          ║ ║  od_cmd_yield() =       ║
║ RX producer =           ║ ║  Bluefruit cb task →    ║ ║  BLE host task +        ║ ║  sl_main_process_       ║
║  BT RX thread (p8) →    ║ ║  od_pipe_rx() (finally  ║ ║  WiFi TCP reader →      ║ ║  action() +             ║
║  od_pipe_rx()           ║ ║  a real queue; retires  ║ ║  od_pipe_rx()           ║ ║  od_work_poll(UI)       ║
║                         ║ ║  pwrmgmLock class)      ║ ║                         ║ ║  → BLE + LED live       ║
║ od_event_signal =       ║ ║                         ║ ║ od_event_signal =       ║ ║  through the refresh    ║
║  k_sem_give             ║ ║ od_event_wait = no-op   ║ ║  no-op wait; loop polls ║ ║                         ║
║                         ║ ║  (poll each pass)       ║ ║                         ║ ║ RX producer =           ║
║ sysWQ: adv restart,     ║ ║                         ║ ║                         ║ ║  sl_bt_on_event →       ║
║ MSD publish             ║ ║                         ║ ║ platform_service():     ║ ║  ble_transport_bgapi →  ║
║ od_cs WQ: Channel       ║ ║                         ║ ║  wifi reconnect,        ║ ║  od_pipe_rx() ENQUEUE-  ║
║ Sounding (nRF54 only)   ║ ║                         ║ ║  power-latch poll       ║ ║  ONLY, NEVER dispatch   ║
║                         ║ ║                         ║ ║                         ║ ║  (§3.2 — the law)       ║
╟── stays platform-only ──╢ ╟── stays platform-only ──╢ ╟── stays platform-only ──╢ ╟── stays platform-only ──╢
║ Zephyr BT host · GATT   ║ ║ Bluefruit + S140 · UF2  ║ ║ Bluedroid/NimBLE · WiFi ║ ║ BGAPI stack · adv build ║
║ adv · settings/NVS ·    ║ ║ bootloader DFU · flat   ║ ║ +TCP+mDNS · esp_sleep_* ║ ║ · long-write reassembly ║
║ PSA · sys_poweroff +    ║ ║ pin decode · LittleFS   ║ ║ ext0/ext1 wake · POWER  ║ ║ · NVM3 · PSA · EM4 +    ║
║ retained-mem gate ·     ║ ║                         ║ ║ LATCH · RTC retention · ║ ║ button/NFC-field wake · ║
║ MCUboot+SMP · CS        ║ ║                         ║ ║ SEEED_GFX/IT8951 (13.3")║ ║ Gecko bootloader GBL ·  ║
║                         ║ ║                         ║ ║ OTA partitions          ║ ║ TNB132M NFC hardware    ║
╚═════════════════════════╝ ╚═════════════════════════╝ ╚═════════════════════════╝ ╚═════════════════════════╝
```

The one-line reading: **Zephyr gives each lane its own context; the other three platforms run both lanes on one context and honour the contract by pump order plus `od_cmd_yield` slicing.** Silabs is the third column's twin, with two twists — sleep is cooperative (`sl_power_manager`), so the UI lane needs a wake timer; and the RX producer is a stack callback *on the same thread as the lanes*, so enqueue-only RX changes from good practice into a hard law (§3.2).

### 2.1 Build / dependency graph (who consumes the core, via what)

```
                              ┌────────────────────────────────┐
                              │      libopendisplay.git        │
                              │  src/ ports/ third_party/      │
                              │  tests/  docs/PROTOCOL.md      │
                              └───┬────────┬─────────┬────────┬┘
              packaging faces:    │        │         │        │
               zephyr/module.yml ─┘        │         │        └─ cmake/opendisplay.cmake
               (+Kconfig)          arduino/library.json                 │        │
                    │                      │                            │        │
        west/NCS module            PlatformIO lib_deps          include()+      ctest (x86)
        (submodule +               (submodule +                 target_sources( │
         ZEPHYR_EXTRA_MODULES       symlink://)                  slc PRIVATE …) │
         or west.yml entry)                │                            │        │
                    │                      │                            │        │
   ┌────────────────▼───┐   ┌──────────────▼─────────────┐   ┌──────────▼──┐  ┌──▼─────────┐
   │  Firmware_NRF54    │   │  Firmware                  │   │ Firmware_   │  │ library CI │
   │  west build        │   │  pio run                   │   │ Silabs      │  │ host tests │
   │  (sysbuild+MCUboot │   │  env: nrf52840custom,      │   │ SLC gen →   │  │ golden     │
   │   at Stage 5)      │   │  esp32s3/c3/c6/classic     │   │ cmake_gcc + │  │ vectors +  │
   │  pins SHA ────────►│   │  pins SHA ────────────────►│   │ ninja       │  │ od_work    │
   └────────────────────┘   └────────────────────────────┘   │ pins SHA ──►│  │ contract   │
                                                             └─────────────┘  └────────────┘
        one core SHA per app release · CI builds all four faces on every library PR
```

---

## 3. Silabs integration — pressure-testing the lane contract against bare metal

**Verdict up front: the lane contract survives.** Silabs does not need a different model — it needs the Arduino model plus two amendments, both of which improve the contract's wording for everyone. The BG22 is "Arduino-shaped with a cooperative sleep manager": one thread, a superloop, ISRs, and a BLE stack that delivers events on the loop thread rather than a separate task.

### 3.1 Mapping

- **CMD lane = phase 1 of `app_process_action()`.** Today commands dispatch synchronously inside `sl_bt_on_event` (`opendisplay_pipe.c:1153-1156` via `app.c:103`). Under the core, `app_process_action()` calls `od_core_cmd_pump(budget)` at top level instead. This is not a compromise forced by the core; it **fixes the current architecture's worst behaviour** (a 60 s refresh freezing LED, adv-refresh, connection-timeout handling and the BGAPI pump — §0.3).
- **UI lane = phase 2**, `od_work_poll(OD_LANE_UI)`, exactly the Arduino sorted-list backend. `opendisplay_ble_process()`'s hand-rolled `now_ms` arithmetic (`opendisplay_ble.c:1882-1898`) is precisely the workload `od_work_t` was designed to absorb — **the BG22 port re-implemented `od_work` informally, which is the strongest available evidence the primitive is at the right altitude.**
- **`od_event_signal()` = `app_proceed()`** (`app_bm.c:46-54`) — already ISR-safe, already counting. The bridge is a one-liner.
- **Dangerous executors stay top-of-loop**: core's `od_power_policy` decides; the platform's pending-flag pattern (`opendisplay_ble.c:1910-1927`) executes EM4/DFU at the loop's safe point. **The existing code *is* the design.**

### 3.2 Amendment 1 — "transport RX is enqueue-only" becomes a law, not a convention

On Zephyr and Arduino, the RX producer is a *different context* from the lanes, so `od_pipe_rx()`-then-return is natural. On Silabs, the producer (`sl_bt_on_event`) runs **on the same context as the lanes**. Two consequences:

1. If a transport handler ever dispatched instead of enqueuing, the CMD lane would be re-entered the moment `od_cmd_yield()` pumps the stack — violating the single-consumer guarantee. So the contract gains an explicit universal rule: **transport event handlers may only call `od_pipe_rx()` and `od_event_signal()`; they never call into dispatch.** (BGAPI prepare/execute long-write reassembly, `opendisplay_pipe.c:36-38,1099-1156`, stays in `ble_transport_bgapi.c` and feeds *complete frames* to `od_pipe_rx()`.)
2. With that rule in place, the Silabs `od_cmd_yield()` is allowed to do what the platform needs:

```c
/* ports/silabs_bm — od_cmd_yield(ms): the CMD lane is blocked in a refresh */
void od_cmd_yield(uint32_t ms) {
    sl_main_process_action();          /* keep BGAPI events flowing: writes arriving
                                          during the 60 s refresh land in the core
                                          cmd ring instead of overflowing the stack
                                          event buffer. Legal: we are at loop level,
                                          not inside sl_bt_on_event, and RX handlers
                                          are enqueue-only by law. */
    od_work_poll(OD_LANE_UI);          /* LED keeps stepping, adv keeps refreshing  */
    sl_sleeptimer_delay_millisecond(min(ms, 10));
}
```

The contract text for `od_cmd_yield` therefore broadens from "pump the UI lane if it shares this context" to **"pump every co-resident consumer that must not starve: the UI lane, and on single-thread platforms, the transport stack's event pump — under the enqueue-only RX law."**

### 3.3 Amendment 2 — `od_event_wait()` is a wakeup hint, not a blocking guarantee

On Zephyr the CMD thread parks in `od_event_wait()`. A superloop cannot park there — it must return to `main.c:78`'s `sl_power_manager_sleep()` so the SoC reaches EM2 between events. So the Silabs (and Arduino) backend implements `od_event_wait(t)` as *return immediately* (or a bounded ≤10 ms nap), and the loop calls `od_core_cmd_pump()` every pass. The contract wording changes to: **"the core guarantees only that it calls `od_event_wait()` when the cmd ring is empty; a backend may satisfy it with any wait from 0 ms up to `timeout_ms`."** Core correctness must never depend on the wait actually blocking.

The corollary is Silabs-specific but pays for the whole `ports/` decision: **with EM2 sleep between loop passes, who wakes the loop for a UI deadline 40 ms out?** Arduino never sleeps, so its backend needs no answer. The Silabs `od_work` backend must arm a one-shot `sl_sleeptimer` at the earliest pending deadline whose callback is just `app_proceed()` — otherwise the power manager sleeps through buzzer steps and button long-press windows. (~30 extra LOC over the Arduino backend; the entire reason `ports/silabs_bm/` is 150 LOC instead of 120.) The same mechanism keeps `sl_power_manager` honest: the backend adds/removes an EM1 requirement around bit-banged SPI bursts and refresh polling.

### 3.4 What Silabs does NOT change

- `cancel_sync` degenerates to `cancel` exactly as on Arduino — single context, "not running concurrently" is free.
- The SPSC ring, response-ring flush, connection-generation counter: unchanged; `sl_bt_evt_connection_closed` bumps the generation.
- The 50 ms UI-handler budget: comfortably holds.
- Feature subsetting is a **link-time** matter, not `#ifdef` creep: BG22 simply doesn't reference `od_buzzer`/`od_touch`/`od_sensor_*`/`boot_screen`, and `--gc-sections` (already standard in the SLC toolchain) drops them. **The rule for core authors: feature knobs live in `od_port.h` or the linker, never as `#if` inside shared `.c` files.**
- The ring sizing law meets its hardest case: BG22 has 32 KB RAM total (`autogen/linkerfile.ld:35`). `OD_PORT_CMD_SLOT=512, DEPTH=W+1` with **W=2–4** (≈1.5–2.5 KB) instead of Nordic's 33×512. The W+1 law holds; W itself is negotiated per-device in PIPE START, so a small window is protocol-legal, just slower — exactly what a 32 KB part should be.

**Conclusion of the falsification test:** the platform that most violates the contract's assumptions required **zero structural change to the core**, one broadened function contract (`od_cmd_yield`), one weakened one (`od_event_wait`), and one promoted invariant (enqueue-only RX). All three amendments are *also* correct on Zephyr and Arduino. **The lane abstraction is at the right altitude.**

---

## 4. Migration path — per repo, while everything keeps shipping

Stage numbering extends DESIGN_UNIFIED §5. Every stage leaves all three app repos releasable.

| Stage | Weeks | What happens | Who ships what |
|---|---:|---|---|
| **0. Protocol hygiene — now includes Silabs, and is more urgent than previously assessed** | 1–2 | As DESIGN_UNIFIED Stage 0, **plus**: BG22 moves NFC `0x0082 → 0x0090` (`opendisplay_protocol.h:17` + dispatch) and gains `0x0046` GET_CAPS with the `NFC` bit. The old "zero-risk" claim is void — BG22's NFC is real (§0.5); every BG22 unit shipped meanwhile deepens the collision with canonical PIPE_WRITE_END. Do this in the next BG22 release (it has working GBL OTA, so the fleet is updatable — the one target where late is recoverable, but don't test that). | All four targets release from current trees, one small diff each |
| **1. Extract `libopendisplay`** (nRF54 = first consumer) | 3–5 | As DESIGN_UNIFIED Stage 1, plus **three-way diff triage**: the 423 pipe / 239 parser / 99 struct diff-lines (§0) are classified feature-vs-rot *before* extraction; Silabs-unique logic worth keeping (NFC endpoint incl. chunked write → `od_nfc.c`; connection-scoped long-write handling → transport pattern) is folded into the core now. `ports/{zephyr,arduino,silabs_bm,posix}` land with the contract test suite. | nRF54 ships from core SHA; Firmware + Silabs untouched |
| **2. PIPE_WRITE into core** | 2–3 | Port from `Firmware/src/display_service.cpp:2114-2600`. | nRF54 gains pipe |
| **3a. Silabs becomes the second consumer** | 2–3 | **The smallest and most alien consumer goes *before* the big Arduino rewire** — if the contract survives bare metal, Arduino is downhill; if it doesn't, only 5 kLOC of blast radius. Add submodule + `include(opendisplay.cmake)` + `target_sources(slc …)`; delete the eight `opendisplay_*` copies, `third_party/`, `qr/`; add `od_hal_efr32/` + `ble_transport_bgapi.c` + `nfc_tnb132m.c`; move loose root files under `app/` (pure `git mv`, separate commit). **Carrot: BG22 gets PIPE_WRITE for free** at the moment of the swap. | BG22 ships from core SHA; carries `DEEP_SLEEP`+`DFU_BOOTLOADER`+`NFC` caps honestly |
| **3b. `od_hal_arduino` + rewire `Firmware/`** | 4–6 | ~9 kLOC deleted; nRF52 gains the cmd ring; ESP32 buzzer survives refreshes. | Both PlatformIO targets ship from core SHA |
| **4. nRF52840 → Zephyr** | 3–5 | UF2/sdv7 layout; backend swap. | |
| **5. nRF54 power + DFU (MCUboot/SMP)** | 5–8 | Note **BG22 is the reference implementation** for the retention/wake gate — its button/NFC-field EM4 wake path predates nRF54's. | |
| **6. ESP32 OTA (`0x006x` over the pipe engine)** | 2–3 | | |

**Total ≈ 22–35 weeks** (DESIGN_UNIFIED's 20–31 + ~2–4 for Silabs triage and Stage 3a). Each app repo's release branch never depends on unmerged library work; the submodule bump commit is the only coupling point, and reverting it is the rollback plan.

---

## 5. Costs, and what could go wrong

| # | Risk / cost | Severity | Notes & mitigation |
|---|---|---|---|
| 1 | **Four packaging faces on one library** (Zephyr module, PlatformIO package, plain CMake, host ctest). | Medium, chronic | Contained because the Silabs face is the dumbest possible mechanism (a source list); SLC never learns the library exists — the wrapper CMake owns the integration, a pattern already proven in-tree (`cmake_gcc/CMakeLists.txt:42-46`). CI builds all four faces per library PR; if that CI isn't stood up in Stage 1, drift is guaranteed. |
| 2 | **Silabs NFC `0x0082` collision is live, not theoretical.** DESIGN_UNIFIED's D3 "rollout risk: zero" was written against nRF54's NACK stub; BG22 has a working NFC client surface on the colliding opcode. | **High until Stage 0 ships** | Move to `0x0090` in the next BG22 release; add the `NFC` caps bit; count fielded BG22 units before assuming client impact is nil. Every week of delay converts this from a rename into a compatibility negotiation. |
| 3 | **BG22 32 KB RAM** (`autogen/linkerfile.ld:35`). Core defaults (33×512 ring ≈ 17 KB, zlib window, plane row buffers) exceed the entire SoC RAM. | High if ignored, low once sized | `od_port.h` is load-bearing: W=2–4 ring, static zlib window sized to the largest BG22 panel, no boot_screen link. Add a CI link-and-check of the Silabs face with `--print-memory-usage` budget assertions, or the first oversized core change bricks the port silently. |
| 4 | **Third scheduling shim drifts out of contract** (the Silabs `od_work` + sleeptimer-wake backend has real logic: deadline re-arm vs EM2, `app_proceed` coalescing). | Medium | This is *why* `ports/` lives in the library with the contract test suite. Without it, "UI handler ran 400 ms late after EM2" becomes an unreproducible field bug. |
| 5 | **Drift-triage underestimate at Stage 1.** The three-way merge is where hidden semantics live — Silabs's BGAPI long-write offset reassembly, connection-timeout close, response paths. Treating nRF54 as "the" source and bulk-deleting the other two copies loses fixes that only exist there. | Medium | Budgeted explicitly. Golden vectors captured from *all three* current firmwares before extraction, not just nRF54. |
| 6 | **Enqueue-only RX law violated by future transport code** (worst on Silabs, where violation = same-thread re-entry into dispatch during `od_cmd_yield`). | Medium | Debug assert (`od_in_cmd_pump && caller==transport → panic`), documented in `od_hal.h`, exercised by a host test that pumps a fake stack inside yield. |
| 7 | **Power regression on BG22 during long refreshes** — `od_cmd_yield` keeps the loop spinning at ≤10 ms granularity. | Low | Net wash or better; but measure EM-level residency before/after Stage 3a — BG22 is the battery-critical target and currently the only one with working deep sleep. |
| 8 | **Submodule discipline** across three app repos. | Low, perennial | CI check: submodule SHA must be an ancestor of `libopendisplay/main`; `0x0043` build-id embeds the SHA so field units are attributable. |
| 9 | **SLC regeneration friction**: re-running SLC rewrites `opendisplay-bg22.cmake`/`autogen/`. | Low | The library hook lives only in the hand-owned wrapper `CMakeLists.txt`, which SLC does not regenerate — the same reason the existing injections survive regen today. |
| 10 | **Seeed_GFX** | Contained (unchanged) | Stays vendored in `Firmware/lib/`, registers `od_panel_ops` from the ESP32 app layer, never enters the library. |

**Bottom line:** adding the Silabs target to the convergence costs ~2–4 weeks and one more packaging face, and in exchange it (a) deletes the fastest-rotting of the three core copies, (b) supplies the reference implementations for deep-sleep gating and working DFU that nRF54 still lacks, and (c) — by surviving the bare-metal falsification test with only wording amendments — **proves the lane contract is a real abstraction rather than a Zephyr-and-Arduino coincidence.**
