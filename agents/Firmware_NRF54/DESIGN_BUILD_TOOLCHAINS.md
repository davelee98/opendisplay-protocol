# Building One Shared C/C++ Tree Across Four Embedded Toolchains

> Research + recommendation, 2026-07-14. Documentation only; no code changed.
> Companion to [DESIGN_REPO_STRUCTURE_FOUR_TARGETS.md](DESIGN_REPO_STRUCTURE_FOUR_TARGETS.md) (structure + the lane diagram) and [DESIGN_UNIFIED_ARCHITECTURE.md](DESIGN_UNIFIED_ARCHITECTURE.md) (the core-library proposal).

## Executive summary

**A. Can PlatformIO do this? No.** Not "awkwardly" — it is not possible. PlatformIO has **zero nRF54L15 support** (no `platform-nordicnrf54` exists; `platform-nordicnrf52` ships 45 boards, all nRF52), and its Silicon Labs platform has **no EFR32BG22 and no Gecko/Simplicity SDK framework** (one release ever, 2023-07-28; the BG22 request has been open since 2022, the Gecko SDK request since 2017). PlatformIO is dead as a unifier.

**B. But the real finding is that you don't need a unifier, because three of your four targets are already CMake — including Silabs, which has *already silently abandoned SLC*.** The `.slcp` is a fossil. And `third_party/bb_epaper` is already a working, in-repo, four-way-portable library using exactly the pattern recommended below. The architecture you want is not a migration; it's a promotion of something you've already built by accident.

**E. Recommendation: library-as-package.** One `core/` source tree, four thin native build-entry files. Do **not** try to make all four CMake-native (the ESP32 Arduino→IDF port is a large, low-value lift). Do **not** adopt Bazel/Meson.

---

## 0. Ground truth: what these four builds actually are

| Target | Nominal build system | What it *actually* runs |
|---|---|---|
| nRF54L15 / LM20 | NCS + west + CMake + Kconfig | CMake. `west build … -- -D<args>` (`build.sh:34`) |
| EFR32BG22 | "Simplicity SDK + SLC" | **Plain CMake + Ninja + stock `arm-none-eabi-gcc`.** SLC is not in the loop. |
| nRF52840 | PlatformIO + Arduino | SCons |
| ESP32 ×4 | PlatformIO + Arduino | SCons |

That table is the whole report. Two of four are already CMake; the other two are one build system (PlatformIO), used in Arduino mode, where it is genuinely good.

---

## 1. The Silabs port has already left SLC. This is the load-bearing discovery.

`Firmware_Silabs/cmake_gcc/opendisplay-bg22.cmake` says `# Automatically-generated file. Do not edit!` at line 1. **It has been edited.** Compare the `.slcp` source list against the generated CMake:

`opendisplay-bg22.slcp:21-29` declares 8 sources. `cmake_gcc/opendisplay-bg22.cmake:174-192` compiles **17**. These nine exist *only* in the hand-edited "generated" file and would be **silently dropped by any `slc generate`**:

```
opendisplay_display_color.c   opendisplay_display.cpp   opendisplay_epd_map.c
qr/qrcode.c                   app_bm.c                  main.c
third_party/bb_epaper/src/bb_epaper.cpp
third_party/bb_epaper/src/Group5.cpp
third_party/uzlib/src/od_zlib_stream.c
```

That is *every C++ file and every vendored dependency in the project*. Also hand-added: `__SILABS_BG22__=1` (`cmake_gcc/opendisplay-bg22.cmake:269`) and the include dirs at lines 200-202.

Three further nails:
- **The vendored SDK contains no component metadata.** `Firmware_Silabs/simplicity_sdk_2025.12.2/` is 658 tracked files / 57 MB: 429 `.h`, 157 `.c`, 66 `.a`, **0 `.slcc`**. SLC cannot regenerate against this tree even if you wanted it to.
- **`.slcp` has no path outside the project dir** — no `../` anywhere. SLC's `source:`/`include:` keys are project-relative by construction, so **SLC structurally cannot reference an external `libopendisplay/`**. This alone kills "keep SLC as the Silabs entry point."
- **CI proves SLC is unnecessary.** `.github/workflows/ci.yml` installs `cmake ninja-build gcc-arm-none-eabi` from **apt** and runs `./build-and-flash.sh --no-flash`. No Simplicity Studio, no `slt`, no SLC. `toolchain.cmake:16-26` *prefers* `slt where …` but falls back cleanly to `ARM_GCC_DIR=/usr`.

**Conclusion: the question "can a `.slcp` include external sources / is there a `.slcc` mechanism?" is moot. You already bypassed SLC and hand-wrote CMake against `autogen/`. The correct move is to stop pretending otherwise, rename the file, and delete the `.slcp` from the build path.**

*(Redistributability: emlib/platform sources are `SPDX-License-Identifier: Zlib`. The BLE stack is 66 prebuilt `.a` blobs under Silabs MSLA, already committed to a GPL-3 repo. That is a licensing question for a lawyer, not a build-system blocker — CI already works because the blobs are in git.)*

---

## 2. How bad is the duplication? (measured)

**~19,800 LOC maintained as three divergent copies.** Per-file drift, nRF54 `src/` vs Silabs root:

| File | nRF54 | Silabs | Line-identical |
|---|---|---|---|
| `opendisplay_pipe.c` | 1425 | 1204 | **79%** |
| `opendisplay_epd_map.c` | 94 | 75 | 79% |
| `opendisplay_structs.h` | 315 | 220 | 69% |
| `opendisplay_config_parser.c` | 696 | 475 | **66%** |
| `opendisplay_display.cpp` | 982 | 892 | **54%** |
| `opendisplay_display_color.h`, `opendisplay_epd_map.h`, `opendisplay_led.h` | — | — | **100% (identical)** |

The PlatformIO copy is the same code again under different names (`communication.cpp` = pipe, `config_parser.cpp`, `structs.h`). Silabs has **no `boot_screen`** and **no `factory_config`** at all — pure feature drift.

Meanwhile, the things that *are* shared are already byte-identical, which tells you the model works:
- `tools/config_packet.py` — **identical** across Firmware and Firmware_NRF54.
- `third_party/uzlib/` — **identical** nRF54 ↔ Silabs; `od_zlib_stream.c` is **byte-identical across all three repos**.
- `qr/qrcode.c` — identical nRF54 ↔ Silabs.

**The 20–46% drift is not inherent complexity. It is the cost of `cp -r`.**

---

## 3. `bb_epaper` already solves this problem, four ways, today

This is the pattern. `Firmware_NRF54/third_party/bb_epaper/src/bb_epaper.cpp:34-60`:

```c
#ifdef ESPHOME_LOG_LEVEL
#include "esphome_io.inl"
#elif defined(__SILABS_BG22__)
#include "silabs_efr32_io.inl"
#elif defined(TARGET_NRF54) || defined(CONFIG_ZEPHYR)
#include "nrf54_zephyr_io.inl"
#elif defined( ARDUINO )
#include "arduino_io.inl"
#elif !defined(__MACH__)
#include "../esp_idf/esp_generic.inl"     // ESP-IDF
#endif
#include "bb_ep.inl"
#if defined(__SILABS_BG22__)
#include "silabs_bbep_busy.inl"
#elif defined(TARGET_NRF54) || defined(CONFIG_ZEPHYR)
#include "nrf54_bbep_busy.inl"
#endif
```

One source tree. Port backends selected by a single `-D`. `TARGET_NRF54` is set in `zephyr/CMakeLists.txt:63`; `__SILABS_BG22__=1` in `cmake_gcc/opendisplay-bg22.cmake:269`; `ARDUINO` by PlatformIO. It is consumed by **CMake `target_sources`** (twice) and by **PlatformIO `lib_deps`** (once) — and the nRF54 and Silabs vendored copies **differ by only 4 lines**, versus 115 lines from upstream `bitbank2`.

**You have already built and validated the exact library-as-package model this report recommends. It's sitting in `third_party/`.** The only thing wrong with it is that there are three copies of it and the ESP32 build pulls an *unpatched* upstream from git (`platformio.ini:7`), so ESP32 silently runs different display code than nRF54/Silabs.

---

## 4. Question A, answered decisively: **PlatformIO — No**

Verified live against the registry/GitHub, July 2026.

| Claim | Verdict | Evidence |
|---|---|---|
| PIO supports nRF54L15 | **NO — does not exist** | No `platform-nordicnrf54` in the 308-platform registry. `platform-nordicnrf52` boards dir = 45 boards, all nRF52. |
| PIO supports EFR32BG22 | **NO** | `platform-siliconlabsefm32` ships **5 boards**; only one is EFR32 (MG12). [Issue #7 "EFR32BG22"](https://github.com/platformio/platform-siliconlabsefm32/issues/7) open since 2022-07-24. |
| PIO supports the Silabs BLE stack | **NO** | Frameworks are `mbed` + `zephyr` only. [Issue #2 "Add support for Gecko SDK"](https://github.com/platformio/platform-siliconlabsefm32/issues/2) **open since 2017-07-02**. Orphan `framework-gecko-sdk v4.0.0 (2021)` + `tool-slc-cli (2022)` exist in the registry, referenced by **no platform**. |
| `framework = zephyr` works for NCS | **NO** | `platform-nordicnrf52` pins `framework-zephyr ~2.20701.0` = **Zephyr 2.7.1 (Dec 2021)**. [Issue #141](https://github.com/platformio/platform-nordicnrf52/issues/141) open since 2022-02-28. Sysbuild (Zephyr 3.4+) cannot exist there. Docs state **"Trusted Firmware M module is not supported"** — mandatory for nRF54L15. MCUboot is broken even on 2.7.1 ([#150](https://github.com/platformio/platform-nordicnrf52/issues/150), open since 2022). |
| PIO can drive a CMake SDK | **NO official mechanism** | Core is SCons. The Zephyr builder is a bespoke per-framework adapter. Only escape hatch is `extra_scripts` shelling out — PIO won't understand the result. |
| Is `platform-siliconlabsefm32` maintained? | **NO** | v10.0.0, 2023-07-28 — **its only release ever.** |
| Is PlatformIO Core maintained? | **Yes, but Arduino-shaped** | v6.1.19 (2026-02-04). nordicnrf52's last four releases mention **only** Arduino-core and J-Link bumps — zero Zephyr work. |

**A telling in-repo corroboration:** `platformio.ini` already routes around PlatformIO's official platforms. **7 of 10 envs depend on community forks** — `maxgerhardt/platform-nordicnrf52` (line 27) and `pioarduino/platform-espressif32` (lines 38, 113, 196, 222…). Only the three C3/C6 envs use official `espressif32`. You are already paying the maintenance tax of PlatformIO's decay.

**Verdict: PlatformIO is excellent at exactly one job — Arduino on ESP32/nRF52 — and incapable of the other two targets. Keep it for what it's good at. Never make it the unifier.**

---

## 5. Question B: what actually works

### Option 1 — "All four CMake-native" (drop PlatformIO) — **Rejected, on cost**

Tempting: Zephyr is CMake, Silabs is *already* CMake, ESP-IDF is CMake. Three of four for free.

What breaks is the **ESP32 Arduino→ESP-IDF port**, and it is not small:
- **20 of ~22 files in `Firmware/src/` include Arduino/WiFi/NimBLE/Wire/SPI headers.**
- `wifi_service.cpp` is `WiFi.h`/`WiFiServer`/`MDNS` — ESP-IDF equivalents are a rewrite, and WiFi is the one ESP32-only feature you cannot drop.
- Seeed_GFX is a 7,659-LOC TFT_eSPI fork.

**The one genuinely encouraging data point:** `Firmware/lib/Seeed_GFX/CMakeLists.txt:1-5` already *is* an ESP-IDF component:
```cmake
idf_component_register(SRCS "TFT_eSPI.cpp"
        INCLUDE_DIRS "." "Extensions"
        REQUIRES driver arduino-esp32)
```
So the IDF path exists via `arduino-esp32`-as-a-component, and Seeed_GFX survives it. **This is a viable future, not a viable now.** It buys uniformity, not capability — and it is weeks of risk on your highest-volume, best-tested target. **Defer it.**

### Option 2 — **Library-as-package. This is the answer.**

One `core/` source tree; each toolchain consumes it natively via a ~20-line build-entry file. Pressure-tested:

- **"Can Silabs consume an external dir?"** SLC: no (no `../` paths). **Hand-written CMake: trivially yes** — and you're already using hand-written CMake. *The design's weakest assumption turns out to be its safest.*
- **"Can PIO consume a lib with nested `third_party/` and mixed C/C++?"** Yes — documented: `srcDir: "."` "will instruct PlatformIO to recursively build all source files located in the root of the library **and its subfolders**." Set `lib_ldf_mode = off` so LDF never wanders into `third_party/`.
- **"Does the port-shim seam actually work?"** Yes — `bb_epaper` (§3). Empirically, in your repo, across all four toolchains.
- **"Does it need Zephyr's module system?"** No — `ZEPHYR_EXTRA_MODULES` is a `-D` and `build.sh:34` already forwards `-D` args. One-line change.

### Option 3 — Bazel / Meson / custom generator — **Dismissed**

- **Bazel:** no Zephyr support (Kconfig+devicetree+`zephyr_library` is deeply CMake-native); no NCS support; you'd maintain rules for three vendor SDKs forever.
- **Meson:** same objection, less ecosystem. Zephyr will never be Meson.
- **Custom generator** (emit CMake + `platformio.ini` from one manifest): this is a real pattern, and it's what SLC *is* — and you can watch it failing in `cmake_gcc/` right now. Generated build files get hand-edited under deadline pressure, and then the generator is a liability. **Do not build a second SLC.**

### Distribution: monorepo vs submodule vs subtree vs registry

[DESIGN_UNIFIED_ARCHITECTURE.md](DESIGN_UNIFIED_ARCHITECTURE.md) §1.5 picks **git submodule**. **This report pushes back.** There are three repos, one git user, ~20k LOC of triplicated code that has already drifted 20–46%, and a *shared wire protocol* that couples the release trains whether you admit it or not. Submodules let each app repo pin a different core SHA — which is drift with a version number on it. That's the disease, not the cure.

| | Verdict |
|---|---|
| **Monorepo** | **Recommended.** One `git grep`, one atomic protocol change across four targets, one CI matrix, no SHA-bump ceremony. The four release trains stay independent — they're separate *tags/workflows*, not separate *repos*. |
| **Submodule** | Acceptable fallback if the repos must stay separate for external reasons. Cost: every protocol fix is N+1 PRs, and "forgot to bump" reintroduces exactly the drift you're fixing. |
| **Subtree** | Worst of both — vendored copies that *look* like a monorepo but silently fork. **This is what you have now, unmanaged.** |
| **Package registry** | No. Four incompatible registries (west/PIO/none/IDF). Publishing overhead per change, zero benefit for a single-team private core. |

> **Note — this is the one open disagreement in the doc set.** DESIGN_UNIFIED_ARCHITECTURE argues submodule (independent release trains); this report argues monorepo (drift is the disease). Decide before Stage 1; everything else is compatible with either.

---

## 6. Question C: how each toolchain consumes the core (concrete)

Assume `core/` with `include/opendisplay/`, `src/`, `third_party/{bb_epaper,uzlib}/`, and `port/<target>/od_port_config.h`.

### Zephyr / NCS
Two files in `core/zephyr/`. There is **no `west.yml` in `Firmware_NRF54`** — it's a freestanding app built against `$ZEPHYR_BASE` — so use `ZEPHYR_EXTRA_MODULES`, *not* a manifest entry.

`core/zephyr/module.yml`:
```yaml
name: opendisplay
build:
  cmake: zephyr
  kconfig: zephyr/Kconfig
```
`core/zephyr/CMakeLists.txt`:
```cmake
if(CONFIG_OPENDISPLAY)
  zephyr_library()
  zephyr_library_sources(
    ${OD_CORE}/src/od_pipe.c ${OD_CORE}/src/od_config.c
    ${OD_CORE}/src/od_display.cpp ${OD_CORE}/src/boot_screen.cpp
    ${OD_CORE}/third_party/bb_epaper/src/bb_epaper.cpp
    ${OD_CORE}/third_party/uzlib/src/od_zlib_stream.c)
  zephyr_include_directories(${OD_CORE}/include ${OD_CORE}/port/zephyr)
  zephyr_library_compile_definitions(TARGET_NRF54)
endif()
```
Wire it in `build.sh:34` — one line, since `-D` args already pass through:
```bash
west build … -- -DZEPHYR_EXTRA_MODULES="${SCRIPT_DIR}/core" "${CMAKE_ARGS[@]}"
```

### PlatformIO
`core/library.json`:
```json
{
  "name": "libopendisplay",
  "version": "1.0.0",
  "build": {
    "srcDir": ".",
    "includeDir": "include",
    "srcFilter": "+<src/*> +<third_party/bb_epaper/src/*> +<third_party/uzlib/src/*> -<tests>",
    "flags": ["-Iinclude", "-Iport/arduino", "-Ithird_party/bb_epaper/src", "-Ithird_party/uzlib/src"],
    "libArchive": false
  }
}
```
`platformio.ini`:
```ini
[env]
lib_deps = symlink://./core     ; symlink:// → edits in core/ take effect immediately
lib_ldf_mode = off              ; never let LDF wander into third_party/
```
`libArchive: false` matters if the core ever overrides a weak symbol. `symlink://` (vs `file://`) is the documented choice for local dev — `file://` *copies* and your edits won't take.

**This also fixes a live bug:** `platformio.ini:7` pulls `bb_epaper` from upstream `bitbank2` git, i.e. **unpatched** — 115 lines behind the fork nRF54/Silabs use. Vendoring it in `core/third_party/` makes all four builds agree for the first time.

### Silicon Labs — bypass SLC, hand-write CMake (you already did)
Delete the pretence. Rename `cmake_gcc/opendisplay-bg22.cmake` → `sdk_sources.cmake` (drop the "do not edit" banner; **it is a lie**), keep it as the vendored-SDK source list, and in `cmake_gcc/CMakeLists.txt` add:
```cmake
add_subdirectory(${CMAKE_CURRENT_LIST_DIR}/../core core_build)
target_link_libraries(slc PUBLIC opendisplay_core)
```
with `core/CMakeLists.txt` exporting a plain `add_library(opendisplay_core STATIC …)` + `target_include_directories(… PUBLIC include port/silabs)` + `target_compile_definitions(… PUBLIC __SILABS_BG22__=1)`. The `.slcp` stays in the repo only as a record of which SDK components were originally selected — **it is documentation, not a build input.**

### ESP-IDF (if/when ESP32 moves)
`core/CMakeLists.txt` gains an IDF branch:
```cmake
if(ESP_PLATFORM)
  idf_component_register(SRCS ${OD_CORE_SRCS}
                         INCLUDE_DIRS include port/espidf
                         REQUIRES driver arduino-esp32)
  return()
endif()
```
One file, two build systems, because `idf_component_register` is just CMake.

---

## 7. Question D: the hard parts

### Per-target config — **solve it with a header, not four config systems**
Do **not** try to bridge Kconfig ↔ `build_flags` ↔ `.slcp` config ↔ `sdkconfig`. That's a four-way impedance mismatch and it's where generator-based schemes go to die.

**Every toolchain can add an include directory. That is the only capability you need.** Ship `core/port/<target>/od_port_config.h`:
```c
/* core/port/nrf54/od_port_config.h */
#define OD_PIPE_DEPTH                    33
#define OD_CMD_SLOT_SIZE                 512
#define OPENDISPLAY_ZLIB_USE_HEAP_WINDOW 0     /* static window */
#define OPENDISPLAY_ZLIB_WINDOW_BITS     15
```
`core/include/opendisplay/od_config.h` does `#include "od_port_config.h"` with `#ifndef` defaults. Each build system contributes **one include path** and **one `-DTARGET_*`**. That's it.

This is already how `od_zlib_stream.c` works (`third_party/uzlib/src/od_zlib_stream.c:11-16`) and how `PIPE_SMALL_DRAM_WINDOW` works for the classic-ESP32 DRAM squeeze (`platformio.ini:229`). Promote the pattern; delete the ad-hoc `-D` sprawl. Kconfig then carries **one** symbol on the Zephyr side — `CONFIG_OPENDISPLAY=y` — and nothing else.

### C/C++ mixing
- Every public header in `include/opendisplay/` wraps in `#ifdef __cplusplus extern "C" {`.
- Zephyr **needs** `CONFIG_CPP=y` + `CONFIG_REQUIRES_FULL_LIBCPP=y` — already set (`zephyr/prj.conf:18-19`).
- Silabs/Arduino need nothing (`project(… LANGUAGES C CXX ASM)` at `cmake_gcc/CMakeLists.txt:5-9`).
- **Constraint to enforce:** the core must not require RTTI/exceptions/`<iostream>`, or the Zephyr libcpp cost balloons. It currently doesn't — keep it that way with `-fno-exceptions -fno-rtti` in the core's own `target_compile_options`.

### Vendored `third_party/` — one copy, four consumers
`uzlib` is *already* byte-identical across all three repos — move it and stop thinking about it. Keep the **bb_epaper fork** (it has your `nrf54_*.inl` / `silabs_*.inl` backends that upstream lacks), vendor it once, consume it from all four via the existing `-D` seam. Track the upstream delta (115 lines) in a `PATCHES.md` and upstream the `_io.inl` backends to bitbank2 when convenient.

### CI — one workflow, four jobs, all cacheable
| Job | Runner cost | Notes |
|---|---|---|
| **host tests** | seconds | plain CMake + CTest on `ubuntu-latest`. Gate everything on this. |
| **Silabs** | ~1 min | **already free** — `apt install gcc-arm-none-eabi ninja-build`, SDK vendored in git. No Studio, no SLC, no license server. |
| **nRF54** | ~10 min cold, ~1 min warm | `nrfutil sdk-manager install --ncs-version v3.3.1` + `actions/cache` on `~/ncs`. |
| **PIO (nRF52 + ESP32)** | ~3 min | `pip install platformio`; cache `~/.platformio`. |

**All four already run in CI today.** Merging them is a matter of one `matrix:` and a shared checkout — and the hard part (is the Silabs SDK CI-installable?) is already solved by vendoring. It's the cheapest job of the four.

### Host-built unit tests — **CMake + CTest**
Three of four targets are CMake; the core's own `CMakeLists.txt` already exists for them. Add `core/tests/` with `add_executable` + `enable_testing()` and a `port/host/` backend (`od_hal.h` against stdlib + a fake clock). PlatformIO's `pio test` is a distraction — don't split the test story across two runners.

**This is the highest-value item in the whole plan and it is currently zero.** The pipe protocol, AES-CCM, CMAC challenge, config TLV parser and CRC are all pure C with no platform deps (`opendisplay_config_parser.c` — 696 LOC, **zero** `zephyr/` includes) and have never once been tested on x86 with golden vectors. Seven files in `Firmware_NRF54/src/` already have **no Zephyr includes at all** and could be under test this week.

### Reproducibility / toolchain pinning
| Target | Pin | Status |
|---|---|---|
| nRF54 | `--ncs-version v3.3.1` | pinned in CI, but `ncs-env.sh:9` **globs** `~/ncs/v3.*` locally → **local ≠ CI**. Fix: honor an `NCS_VERSION` file. |
| Silabs | `simplicity_sdk_2025.12.2/` **in git** | perfectly reproducible. Compiler is *not* pinned (apt `gcc-arm-none-eabi` in CI vs `slt` Conan GCC locally, `toolchain.cmake:29-39`) → **pin the ARM GCC version.** |
| PIO | forks pinned by URL, but `pioarduino/…/stable/…zip` is a **moving target** | pin to a release tag, not `stable`. |

The Silabs approach — vendor the SDK, use a stock compiler — is by far the most reproducible of the three. **The target everyone assumes is the most locked-down vendor-tool nightmare has, in practice, the cleanest and most hermetic build in the repo.**

---

## 8. Recommendation

**Monorepo. Library-as-package. Four native build-entry files. Keep PlatformIO for Arduino only.**

```
opendisplay/
├── core/                              ← the shared tree, single source of truth
│   ├── include/opendisplay/           od_core.h od_hal.h od_pipe.h od_protocol.h od_structs.h
│   ├── src/                           od_pipe.c od_config.c od_display.cpp boot_screen.cpp
│   │                                  od_led.c od_buzzer.c od_touch.c od_button.c
│   │                                  od_epd_map.c od_display_color.c qr/qrcode.c
│   ├── third_party/
│   │   ├── bb_epaper/                 THE FORK (nrf54_*.inl + silabs_*.inl + arduino_io.inl)
│   │   └── uzlib/                     already byte-identical everywhere — just move it
│   ├── port/
│   │   ├── nrf54/od_port_config.h     ← per-target config lives HERE, not in Kconfig/.slcp/ini
│   │   ├── silabs/od_port_config.h
│   │   ├── arduino/od_port_config.h
│   │   └── host/od_port_config.h  + od_hal_host.c
│   ├── tests/                         ← CTest, x86, golden wire vectors. The point of all this.
│   ├── CMakeLists.txt                 ← serves Silabs + ESP-IDF + host tests
│   ├── zephyr/{module.yml,CMakeLists.txt,Kconfig}   ← serves nRF54
│   └── library.json                   ← serves PlatformIO (nRF52 + ESP32)
│
├── apps/
│   ├── nrf54/      main.c board_nrf54.c nrf54_gpio.c od_hal_zephyr.c zephyr/prj*.conf app.overlay
│   ├── silabs/     main.c app.c od_hal_silabs.c config/ autogen/ simplicity_sdk_2025.12.2/ cmake_gcc/
│   └── arduino/    main.cpp od_hal_arduino.cpp wifi_service.cpp wake_button.cpp power_latch.cpp
│                   lib/Seeed_GFX/ variants/ platformio.ini
├── tools/          config_packet.py factory_config_gen.py   ← ONE copy (already identical!)
└── .github/workflows/ci.yml            ← 4 jobs + host tests
```

**The four build-entry files, and nothing more:**

| Toolchain | Entry file | Mechanism |
|---|---|---|
| Zephyr/NCS | `core/zephyr/module.yml` + `CMakeLists.txt` | `-DZEPHYR_EXTRA_MODULES=…/core` (one line in `build.sh:34`) |
| Silabs | `core/CMakeLists.txt` | `add_subdirectory(../core)` + `target_link_libraries(slc PUBLIC opendisplay_core)` |
| PlatformIO | `core/library.json` | `lib_deps = symlink://./core`, `lib_ldf_mode = off` |
| ESP-IDF *(future)* | `core/CMakeLists.txt` | `idf_component_register(...)` behind `if(ESP_PLATFORM)` |

**Sequencing** (each step independently shippable, in strict value order):

1. **Kill the SLC fiction.** Rename the generated CMake, drop the false banner, note in `AGENTS.md` that `.slcp` is not a build input. Zero risk; stops the next `slc generate` from silently deleting every C++ file in the project.
2. **Hoist `third_party/` and `tools/` into `core/`.** `uzlib`, `qrcode.c`, `config_packet.py` are *already identical* — a pure `git mv` with no semantic change, and it makes ESP32 use the patched `bb_epaper` for the first time.
3. **Stand up `core/tests/` + CTest** on the modules that already have zero platform deps (`config_parser`, pipe framing, CCM/CMAC, CRC). **This is the payoff.** Do it before any risky code motion, so the un-drifting in step 4 has a safety net.
4. **Un-drift and hoist the core modules**, one at a time, nRF54's copy as the baseline (it is the most complete: it alone has `boot_screen` and `factory_config`). Each move is gated by step 3's vectors.
5. **Leave the ESP32 Arduino→IDF port alone** until 1–4 are done and paying rent. Revisit only if you want to delete PlatformIO entirely — Seeed_GFX's `idf_component_register` says the door is open.

**The one-sentence version:** you don't have a four-build-system problem, you have a three-copies-of-the-source problem — Silabs is already CMake, the port-shim pattern already works four ways in `bb_epaper`, and the only thing PlatformIO must keep doing is the one thing it's still good at.

---

## Sources

[platform-nordicnrf52](https://github.com/platformio/platform-nordicnrf52) · [issue #141 (Zephyr 2.7.1, open since 2022)](https://github.com/platformio/platform-nordicnrf52/issues/141) · [issue #150 (MCUboot broken)](https://github.com/platformio/platform-nordicnrf52/issues/150) · [platform-siliconlabsefm32](https://github.com/platformio/platform-siliconlabsefm32) · [issue #7 (EFR32BG22)](https://github.com/platformio/platform-siliconlabsefm32/issues/7) · [issue #2 (Gecko SDK, open since 2017)](https://github.com/platformio/platform-siliconlabsefm32/issues/2) · [siliconlabsefm32 docs](https://docs.platformio.org/en/latest/platforms/siliconlabsefm32.html) · [nordicnrf52 docs](https://docs.platformio.org/en/latest/platforms/nordicnrf52.html) · [library.json build fields](https://docs.platformio.org/en/latest/manifests/library-json/fields/build/index.html) · [LDF modes](https://docs.platformio.org/en/latest/librarymanager/ldf.html) · [lib_deps symlink://](https://docs.platformio.org/en/latest/core/userguide/pkg/cmd_install.html)
