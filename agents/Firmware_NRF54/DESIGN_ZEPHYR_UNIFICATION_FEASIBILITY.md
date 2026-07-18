# Feasibility: Unifying OpenDisplay Firmware on Zephyr (nRF54L15 + nRF52840 + ESP32-S3/C3/C6/classic)

> Research study, 2026-07-14. No code changed. All claims cite either `file:line` in the two repos or an upstream URL. Zephyr claims are checked against `zephyrproject-rtos/zephyr@main` source, not marketing pages.
> Companion docs: [DESIGN_POWER_AND_EVENTS_ZEPHYR.md](DESIGN_POWER_AND_EVENTS_ZEPHYR.md), [ARCHITECTURE_NRF54.md](ARCHITECTURE_NRF54.md), [ARCHITECTURE_FIRMWARE_SISTER.md](ARCHITECTURE_FIRMWARE_SISTER.md), [FEATURE_PARITY_VS_FIRMWARE.md](FEATURE_PARITY_VS_FIRMWARE.md), [RTOS_COMPARISON.md](RTOS_COMPARISON.md)

---

## 0. Two premises in the brief are wrong, and they change the answer

Before anything else, the framing needs correcting, because it inverts the risk profile.

**(1) The Arduino firmware does not bit-bang SPI or I²C. It uses hardware peripherals.**

- ePaper SPI is **hardware SPI at 8 MHz**. Both bring-up call sites pass a non-zero clock: `Firmware/src/display_service.cpp:154` and `:249` call `bbepInitIO(..., 8000000)`. `bb_epaper`'s `arduino_io.inl:79-95` selects bit-bang **only** when `u32Speed == 0`; otherwise it calls `SPI.begin()` / `SPI.beginTransaction(SPISettings(u32Speed, MSBFIRST, SPI_MODE0))`. The `digitalWrite`-per-bit loop (`arduino_io.inl:41-55`) is **dead code in this firmware**.
- I²C is **hardware `Wire`** everywhere — GT911 (`Firmware/src/touch_input.cpp:71-93`), SHT40 (`sensor_sht40.cpp:62-89`), BQ27220 (`sensor_bq27220.cpp:57-67`), AXP2101 (`display_service.cpp:844-885`). There is no bit-bang I²C in that repo.

**(2) The bit-banging is the *Zephyr* port's compromise, not the Arduino one's.**

`Firmware_NRF54` bit-bangs **both** buses, and its own devicetree overlay says why:

```
zephyr/app.overlay:20-29   "The display is driven by a GPIO bit-bang SPI on the OpenDisplay config
                            pins (P2.1 CLK, P2.2 DATA)... No firmware path uses the Zephyr SPI API"
zephyr/app.overlay:39-45   &i2c22 { status = "disabled"; }   /* BUSY line collides with I2C pins */
```

So the honest statement of the problem is: **Zephyr's DTS-static pin model already forced a hardware-SPI → bit-bang regression on nRF54, and a full unification would export that regression to ESP32, where it would replace a working 8 MHz hardware SPI + DMA path.** That is the central cost, and it is not what the brief assumed.

Also worth flagging: the config *pins* are already only half-honoured on nRF52 today. `bb_epaper` calls bare `SPI.begin()` on non-ESP32 (`arduino_io.inl:85`), so the nRF52840 uses the **variant's fixed** SPI pins (`Firmware/variants/nrf52840custom/variant.h:97-99` — MISO 46 / MOSI 47 / SCK 45), and `display_service.cpp:679-682` explicitly logs `"NOTE: nRF52840 using default I2C pins (config pins: ...)"`. Runtime pin configurability is genuinely load-bearing **only on ESP32**.

---

## A. Zephyr's ESP32 support maturity

### A.1 Board / SoC coverage — good

Upstream Zephyr has boards for all four SoCs in question, plus more: ESP32-DevKitC, ESP32-S2/S3-DevKitC, ESP32-C3-DevKitC/M, ESP32-C5/C6-DevKitC, ESP32-H2, ESP32-P4, ESP8684 ([Zephyr Espressif boards index](https://docs.zephyrproject.org/latest/boards/espressif/index.html)). The ESP32-S3 board page lists drivers for UART, I2C, SPI, I2S, GPIO, ADC, LEDC PWM, MCPWM, timers, WDT, TWAI, USB-OTG, GDMA, SHA/AES accelerators, TRNG, touch, temp sensor ([esp32s3_devkitc](https://docs.zephyrproject.org/latest/boards/espressif/esp32s3_devkitc/doc/index.html)).

Every peripheral OpenDisplay needs has an upstream Zephyr driver: `espressif,esp32-spi`, `espressif,esp32-i2c`, `espressif,esp32-gpio`, `espressif,esp32-adc`, `espressif,esp32-ledc` (buzzer PWM), plus flash + `flash_map` for LittleFS/NVS.

### A.2 WiFi — the weakest link, and it is the one ESP32-only feature you cannot drop

The driver exists upstream (`drivers/wifi/esp32/`), but read its Kconfig:

```kconfig
# zephyr/drivers/wifi/esp32/Kconfig.esp32
menuconfig WIFI_ESP32
	depends on DT_HAS_ESPRESSIF_ESP32_WIFI_ENABLED
	depends on ZEPHYR_HAL_ESPRESSIF_MODULE_BLOBS || BUILD_ONLY_NO_BLOBS   # ← closed-source blob required
	depends on !SMP                                                        # ← single-core only
	select DYNAMIC_THREAD / DYNAMIC_THREAD_ALLOC
config HEAP_MEM_POOL_ADD_SIZE_ESP_WIFI
	default 51200 if ESP_WIFI_HEAP_SYSTEM                                  # ← +50 KB kernel heap
```
([source](https://raw.githubusercontent.com/zephyrproject-rtos/zephyr/main/drivers/wifi/esp32/Kconfig.esp32))

Concretely:
- **It is a binary blob**, fetched with `west blobs fetch hal_espressif`. Blob version must match the HAL version or you get silent failures.
- **`depends on !SMP`** — WiFi forbids SMP.
- **+50 KB of kernel heap** reserved for the WiFi adapter alone. On classic ESP32 (320 KB DRAM) this is a real squeeze — recall your own build already sets `-DPIPE_SMALL_DRAM_WINDOW` on `esp32-N4` because 33×256 B of static DRAM didn't fit (`Firmware/platformio.ini`).
- **Throughput.** Espressif's own tuning blog reaches, *after* tuning `CONFIG_NET_TCP_MAX_RECV_WINDOW_SIZE=50000` on ESP32-S3: **TCP down 4.07 Mbps, TCP up 4.22 Mbps, UDP down 10.05 Mbps** ([Espressif, *Maximizing Wi-Fi Throughput… with ESP32 SoCs*](https://developer.espressif.com/blog/2024/06/zephyr-max-wifi-throughput/)). Untuned Zephyr WiFi on ESP32 is known-bad: [zephyr#51105](https://github.com/zephyrproject-rtos/zephyr/issues/51105) reports a 1 MB download taking ~30 minutes. For your workload (a TCP image pipe on port 2446 with a 4 KB max LAN payload, `Firmware/src/wifi_service.cpp:17,151`) 4 Mbps is *sufficient* — but only after tuning, and the untuned starting point is a trap.
- **Maturity by SoC.** Espressif calls ESP32-C3 "stable for production" as of Zephyr 4.0; ESP32-C6 WiFi is **experimental**, with "Station mode in progress, but AP mode, WPA3, and advanced power save aren't reliably available" ([Hubble](https://hubble.com/community/guides/zephyr-rtos-on-esp32-c6-what-s-supported-and-what-s-still-missing/); [Espressif Zephyr Support Status](https://developer.espressif.com/software/zephyr-support-status/)). WPA3-SAE on C6 is an open build-failure bug: [zephyr#105733](https://github.com/zephyrproject-rtos/zephyr/issues/105733).
- **mDNS**: Zephyr has `CONFIG_MDNS_RESPONDER` + DNS-SD. Your `MDNS.addService("opendisplay","tcp",port)` with a `msd` TXT record (`Firmware/src/wifi_service.cpp:50-81`) is expressible, but it's a rewrite against a different API, not a port.

**Verdict on WiFi:** *supported* ≠ *production-proven for your product*. C3 is defensible. C6 is not, today. S3 is workable but you will spend real time on net-stack tuning that you get for free from ESP-IDF/Arduino.

### A.3 BLE — actually the strongest part of the ESP32 story

Zephyr does **not** use its own link layer on ESP32. It uses the **ESP-IDF controller blob behind a VHCI**, with Zephyr's BT *host* on top:

- DT node `espressif,esp32-bt-hci` with `bt-hci-vs-ext;` and `chosen { zephyr,bt-hci = &esp32_bt_hci; }` on esp32 / esp32s3 / esp32c3 / esp32c6 `*_common.dtsi`.
- Driver: `drivers/bluetooth/hci/hci_esp32.c`. Kconfig exposes `ESP32_BT_CTLR_LE_MAX_CONN`, SMP legacy + LE Secure Connections, 2M PHY, extended adv.

This means **the Zephyr BT host you already run on nRF54 runs unchanged on ESP32**: `BT_GATT_SERVICE_DEFINE`, 512 MTU, notify, `BT_SMP`/`BT_BONDABLE`/`BT_SETTINGS` bonding — all host-side, all portable.

A very concrete win: your nRF54 firmware's TX-power path (`opendisplay_ble.c:500-535`, HCI VS `Write_Tx_Power_Level`) **works as-is on ESP32**, because `hci_esp32.c` implements that exact opcode, mapping it to `esp_ble_tx_power_set_enhanced()`.

Caveats: BLE also needs the blob, and Hubble reports "advanced pairing modes, throughput-intensive data transfer" as unreliable on C6 — relevant, since your image pipe is exactly throughput-intensive BLE with bonding.

### A.4 WiFi + BLE coexistence — supported, upstream, and auto-enabled

```kconfig
# zephyr/soc/espressif/common/Kconfig
config ESP32_SW_COEXIST_ENABLE
	bool "Software controls Wi-Fi/Bluetooth coexistence"
	default y if (BT_ESP32 && WIFI_ESP32) || ...
	help
	  Both coexistence configuration options are automatically managed, no user intervention required.
```
Same time-division coex the IDF uses. Genuinely supported.

### A.5 Deep sleep / PM / RTC retention — supported, but the API is ESP-IDF's, not Zephyr's

Better than expected. Upstream Zephyr has:

- `soc/espressif/common/poweroff.c`: `z_sys_poweroff()` → `esp_sleep_pd_config(...)` → **`esp_deep_sleep_start()`**. So Zephyr's `sys_poweroff()` *is* ESP32 deep sleep.
- An official [Espressif deep-sleep sample](https://docs.zephyrproject.org/latest/samples/boards/espressif/deep_sleep/README.html) with `CONFIG_POWEROFF=y` + `CONFIG_RETAINED_MEM=y`, demonstrating **timer wake, EXT1 wake, GPIO wake**, and **RTC memory retention via the `retained_mem` driver** — the exact replacement for your `RTC_DATA_ATTR` vars (`Firmware/src/main.h:129,136,312,320-321`).
- `Kconfig.pm` even has **`ESP32_SLEEP_SET_FLASH_DPD`** (SPI flash `B9h` deep power-down in light sleep — you currently do this by hand).

**But** — and this matters for the unification thesis — the sample's wake sources are set with **raw ESP-IDF calls**: `esp_sleep_enable_timer_wakeup()`, `esp_sleep_get_ext1_wakeup_status()`. There is **no portable Zephyr abstraction** for ext0/ext1/RTC-GPIO wake. So even under Zephyr, `wake_button.cpp` (272 LOC) and `power_latch.cpp` (211 LOC, uses `esp_sleep_config_gpio_isolate()`) stay **100% ESP32-specific**. **Unification buys you zero here.**

### A.6 OTA / MCUboot — supported, via sysbuild

Zephyr's Espressif boards default to **"Simple Boot"** (single image, no OTA, no security). MCUboot is opt-in via `CONFIG_BOOTLOADER_MCUBOOT=y` + `west build --sysbuild`. Flash encryption / secure boot are only available through **Espressif's fork** of MCUboot, not Zephyr's; secure boot + flash encryption are unsupported on C6.

Flash-layout implication: MCUboot wants **two slots**. Your ESP32 partition tables today are single-app (`huge_app.csv` ≈ 3 MB on the 4 MB C3/C6/classic envs). Dual-slot leaves ~1.8 MB per slot — plenty for a Zephyr image, but **the partition table changes, which means existing units cannot be field-migrated** (see §E).

This is the one place Zephyr is a clear *upgrade*: ESP32 has **no OTA of any kind today** (no `Update.begin`, no `esp_ota_*`, no `ArduinoOTA` anywhere in `Firmware/src/`) and nRF54 has none either.

### A.7 PSRAM — supported

`CONFIG_ESP_SPIRAM` for ESP32/S2/S3/C5/P4, quad/octal modes. But note where you *actually* use PSRAM: only inside the vendored `lib/Seeed_GFX` (TFT_eSPI fork) — `Sprite.cpp:166-210` `ps_calloc()` framebuffers, `TFT_eSPI.cpp:492` `psramFound()`. `Firmware/src/` never calls `ps_malloc`/`heap_caps_*`. So PSRAM support in Zephyr is only useful **if you port Seeed_GFX**, which is 29,400 LOC of Arduino-coupled TFT_eSPI. **This is the sleeper item — see §E.**

### A.8 Summary matrix

| Capability | Upstream Zephyr on ESP32 | Confidence for *your* product |
|---|---|---|
| Board support esp32 / S3 / C3 / C6 | ✅ all four | High |
| GPIO / SPI / I2C / ADC / LEDC PWM | ✅ | High |
| BLE peripheral, GATT, 512 MTU, notify, bonding | ✅ (IDF controller blob + Zephyr host) | **High** — host code is the same you already ship |
| BLE VS TX-power (`Write_Tx_Power_Level`) | ✅ `hci_esp32.c` maps it to `esp_ble_tx_power_set()` | High |
| WiFi STA | ⚠️ blob, `!SMP`, +50 KB heap; C3 "production", C6 experimental | **Medium-Low** |
| TCP server + mDNS | ✅ BSD sockets + `MDNS_RESPONDER` (full rewrite of `wifi_service.cpp`) | Medium |
| WiFi/BLE coex | ✅ `ESP32_SW_COEXIST_ENABLE` (auto) | Medium |
| Deep sleep + timer/ext1/GPIO wake | ✅ `sys_poweroff()`→`esp_deep_sleep_start()` — but wake sources are raw `esp_sleep_*` | Medium (and **not portable code**) |
| RTC-memory retention | ✅ `retained_mem` driver | Medium |
| MCUboot OTA | ✅ via sysbuild; secure boot/flash-enc only on Espressif's fork | Medium |
| PSRAM | ✅ | High (but only matters for Seeed_GFX) |
| Seeed_GFX / IT8951 13.3" panel | ❌ **nothing** | **Zero** |

---

## B. Zephyr on nRF52840 — genuinely easy, including the field-upgrade problem

**Board/SoC:** first-class. `nrf52840dk` upstream, full NCS support, SoftDevice Controller supported for the nRF52 series.

**BLE controller:** you'd use `CONFIG_BT_LL_SOFTDEVICE` (NCS SDC) — the *same* controller family your nRF52 Arduino build gets from S140 7.3.0, but linked as a library into the app rather than flashed as a separate SoftDevice binary. **There is no S140 anymore** — the app owns the radio.

### B.1 The shipped-bootloader question — the good news

**Yes, the existing Adafruit UF2 bootloader can flash Zephyr apps. No bootloader replacement is needed.** Upstream Zephyr ships purpose-built support for exactly this:

- Board variants like `adafruit_feather_nrf52840/nrf52840/uf2` and `promicro_nrf52840/nrf52840/uf2` exist upstream.
- Their defconfig sets `CONFIG_BUILD_OUTPUT_UF2=y` with the comment *"Build UF2 by default, supported by the Adafruit nRF52 Bootloader"*.
- Their DTS includes an upstream partition layout **for your exact bootloader + SoftDevice version** — `dts/vendor/nordic/nrf52840_partition_uf2_sdv7.dtsi`:

```
 * Default flash layout for nrf52840 using UF2 and SoftDevice s140 v7
 * 0x00000000 SoftDevice s140 v7    (156 kB)   → reserved_partition_0, read-only
 * 0x00027000 Application partition (788 kB)   → code_partition
 * 0x000ec000 Storage partition     (32 kB)    → storage_partition (NVS/LittleFS)
 * 0x000f4000 UF2 boot partition    (48 kB)    → boot_partition, read-only
```

Your shipped units run `bin/opendisplay_nrf52840_bootloader-0.10.6_s140_7.3.0.hex` — i.e. **S140 v7**, precisely this layout. And your build already emits UF2 with family ID `0xADA52840` (`Firmware/scripts/nrf_uf2_post.py:13-41`).

So the field-upgrade path is: existing bootloader → existing BLE-DFU / UF2 mechanism → new Zephyr app image at `0x27000`. The 156 kB SoftDevice region becomes dead flash, leaving 788 kB for the app — ample. **ZMK ships tens of thousands of nice!nano units this way** (Zephyr app on the Adafruit nRF52 bootloader), which is the strongest available production evidence ([ZMK: Adafruit nRF52 Bootloader](https://zmk.dev/docs/development/hardware-integration/bootloader/adafruit-nrf52)).

If instead you want **MCUboot**, that *does* require replacing the bootloader — a genuine field-upgrade problem. **Don't.** Keep the Adafruit bootloader; you keep BLE DFU and USB drag-drop for free.

### B.2 Migration gotchas from Bluefruit + S140

1. **Pin encoding — a real fork in the "shared" GPIO shim.** Arduino nRF52 uses a flat 0-63 identity map (`Firmware/variants/nrf52840custom/variant.cpp:6-84`; pin N = P0.N for N<32, P1.(N-32) for N≥32). The nRF54 Zephyr port uses a **nibble encoding** `(port<<4)|pin` (`Firmware_NRF54/src/nrf54_gpio.c:25-41`) which **only supports pins 0-15 per port** — it cannot express P0.16-P0.31 at all. An nRF52840 Zephyr port must use the flat 0-47 encoding to stay wire-compatible with deployed config blobs, i.e. a **different decoder** from nRF54's.
2. **Callback context flip.** Today `imageDataWritten` runs on the priority-3 Bluefruit task (`Firmware/src/ble_init.cpp:167`); under Zephyr it becomes a `k_msgq` producer on the BT RX thread with drain on `main` — *strictly better*, and already implemented (`opendisplay_pipe.c:1381-1390`). But every assumption about `pwrmgmLock` and the priority-inversion fix at `display_service.cpp:205-211` evaporates and must be re-derived.
3. **Crypto backend swap.** nRF52 currently uses **Adafruit_nRFCrypto + CC310** (`Firmware/src/encryption.cpp:365-506`). Under Zephyr/NCS you'd move to **PSA Crypto** (what `Firmware_NRF54` already does) — and NCS routes PSA to CC310 on nRF52840 anyway. Low risk, ~150 LOC.
4. **LittleFS → settings/NVS.** `Adafruit_LittleFS`/`InternalFS` → `settings_save_one("od/config", ...)`. NVS records are size-capped; your blob is up to 4 KB and the 32 kB storage partition is workable but tight-ish. **Alternatively keep LittleFS on Zephyr** (`CONFIG_FILE_SYSTEM_LITTLEFS`) over the same `storage_partition` — this would also let you **read existing on-device config files**, which the settings/NVS route would not. Worth deciding deliberately.
5. **No `String`, no `Serial`** — where most mechanical time goes.

**Verdict on B: low risk, and the bootloader is a non-issue.** This is the part of the unification that actually pencils out.

---

## C. The pin/bus problem — the crux, not a detail

### C.1 What "bit-bang" actually costs today, on nRF54

`Firmware_NRF54/third_party/bb_epaper/src/nrf54_zephyr_io.inl:30-41` toggles three GPIOs per bit, and `nrf54_gpio_write()` (`src/nrf54_gpio.c:135-144`) is **not** a thin wrapper — `nrf54_pin_decode()` (`:25-41`) runs a `switch` → `DEVICE_DT_GET` → **`device_is_ready()` on every single bit-edge**, then `gpio_pin_set()` does an indirect call through `api->port_set_bits_raw`. That's **three GPIO writes and three full decode+ready-check paths per bit** — order 100+ cycles per bit at 128 MHz. Realistic sustained clock: **a few hundred kHz to low MHz**, i.e. roughly **10-30× slower than the 8 MHz hardware SPI** the ESP32/nRF52 firmware achieves today.

That is a latent, unmeasured performance liability *already shipping* on nRF54. It's also cheap to fix without changing architecture: hoist the decode out of the inner loop, cache `(const struct device*, pin_mask)` per pin, and use `gpio_port_set_bits_raw()`/`clear_bits_raw()` directly. Likely a 3-5× improvement for about a day of work.

### C.2 Can Zephyr's GPIO API keep up on ESP32?

The "ESP32 GPIO is notoriously slow" folklore is about **Arduino's `digitalWrite`** and IDF's `gpio_set_level()`. Zephyr's is a plain register write (`gpio_esp32.c:365-379` → `out_w1ts`), maybe 15-25 cycles ≈ 100 ns. But an 8 MHz SPI bit is **125 ns**, and each bit needs **3** GPIO ops. **Bit-banged SPI cannot reach 8 MHz on any of these SoCs through any GPIO API.** Ceiling is ~1-3 MHz with a hand-tuned cached-pin inner loop.

Concretely, for a 400×300 BWR panel (2 planes × 15 KB = 30 KB): **~30 ms at 8 MHz hardware SPI vs ~500 ms - 1 s bit-banged.** Panel refresh itself is 1-15 s, so it *may* be tolerable — but on the S3's 13.3" IT8951 panels (which run Seeed_GFX at **10-40 MHz**) it is categorically not.

### C.3 Runtime-configurable pins on hardware SPI/I²C under Zephyr — **yes, this is possible**

This is the finding that changes the calculus. Zephyr has `CONFIG_PINCTRL_DYNAMIC`:

```kconfig
# zephyr/drivers/pinctrl/Kconfig
config PINCTRL_DYNAMIC
	bool "Dynamic configuration of pins"
	select PINCTRL_NON_STATIC
	help
	  When this option is enabled pin control configuration can be changed at
	  runtime. This can be useful, for example, to change the pins assigned to a
	  peripheral at early boot stages depending on a certain input.
```
with `int pinctrl_update_states(...)` ([pinctrl.h:440](https://raw.githubusercontent.com/zephyrproject-rtos/zephyr/main/include/zephyr/drivers/pinctrl.h)).

Both SoC families route peripherals through a full crossbar — nRF's SPIM `PSEL.*` registers, ESP32's GPIO matrix — so arbitrary runtime pin assignment is a **hardware capability on both**, and Zephyr exposes it.

**Two viable designs:**

1. **`PINCTRL_DYNAMIC` + deferred device init.** Build `pinctrl_soc_pin_t` arrays at runtime from the config blob, call `pinctrl_update_states()`, then let the SPI/I²C driver init. Awkward: you must load the config *before* the SPI driver's init priority runs, and the config lives in flash/NVS (whose driver initialises in the same phase). Doable via a high-priority `SYS_INIT` or `DEVICE_DEINIT_SUPPORT`, but the Kconfig help text itself says "at early boot stages" — you are outside the intended envelope.
2. **Bypass pinctrl; poke the crossbar directly, keep Zephyr's SPI driver.** ~30 lines per SoC: after the Zephyr `spi` device is up, write `NRF_SPIM->PSEL.SCK/MOSI` (nRF) or call `esp_rom_gpio_connect_out_signal()` (ESP32) to re-route the peripheral to the config-blob pins, then use `spi_transceive()` normally. Ugly, non-upstream, but small, well-understood, and it **preserves hardware SPI + DMA**. **This is what I'd actually do.**

**The answer to C:** the bit-bang is *not* a necessary consequence of Zephyr. It was an expedient choice on nRF54 and it is reversible. But nobody has done option (2) yet, and it is per-SoC platform code — precisely the kind of code the unification was supposed to eliminate.

### C.4 The runtime-ADC precedent already exists

`zephyr/app.overlay:14-17` exposes **all 8 SAADC channels** under `zephyr,user` so `opendisplay_battery.c` can pick one by runtime index. The identical trick works for ESP32 ADC. "Runtime-selected resource from a DTS-static world" is a solved pattern in this codebase — it just doesn't extend to *pins*.

---

## D. The realistic alternative: extract a platform-agnostic core library

### D.1 The library already exists — it's just misnamed

Look at what `Firmware_NRF54/src/` is actually made of:

| Module | LOC | Zephyr dependencies |
|---|---|---|
| `opendisplay_pipe.c` | 1425 | `k_msgq`, `atomic_t`, `psa/crypto.h`, timing |
| `boot_screen.cpp` | 1039 | none (pure render into a row buffer) |
| `opendisplay_display.cpp` | 982 | `bb_epaper` + `nrf54_gpio` + timing shims |
| `opendisplay_touch.c` | 703 | `nrf54_gpio` + timing |
| `opendisplay_config_parser.c` | 696 | **none** |
| `opendisplay_buzzer.c` | 400 | `k_timer` + GPIO |
| `opendisplay_led.c` | 389 | `k_timer` + `k_busy_wait` + GPIO |
| `opendisplay_i2c.c` | 219 | `nrf54_gpio` + `k_busy_wait` |
| `opendisplay_battery.c` | 211 | Zephyr ADC |
| `opendisplay_epd_map.c` / `opendisplay_display_color.c` / `factory_config.c` | 94/73/79 | **none** |
| `opendisplay_config_storage.c` | 106 | `settings`/NVS |

The *entire* platform surface is already isolated into two tiny headers:
- `src/nrf54_gpio.h` — `pin_decode`, `configure_output`, `configure_input`, `configure_interrupt`, `write`, `read`, `park`, `hwinfo_get_device_id` (**8 functions**)
- `src/nrf54_zephyr_compat.h` — `od_msleep`, `od_uptime_get_32`, `od_busy_wait` (**3 functions**)

**The nRF54 port is already ~90% of the platform-agnostic core library.** Doing D is largely a *rename + backend swap*, not a rewrite.

### D.2 Proposed interface (`libopendisplay`)

```c
/* od_hal.h — the ENTIRE platform contract. ~20 functions. */

/* --- pins: one opaque byte from the config blob, decoded per-SoC --- */
bool     od_gpio_configure_output(uint8_t cfg, bool init_high);
bool     od_gpio_configure_input (uint8_t cfg, bool pu, bool pd);
int      od_gpio_configure_irq   (uint8_t cfg, od_gpio_irq_cb_t cb, void *ud);
void     od_gpio_write(uint8_t cfg, bool high);
int      od_gpio_read (uint8_t cfg);
void     od_gpio_park (uint8_t cfg);

/* --- buses: hardware where available, bit-bang as fallback --- */
int      od_spi_configure(uint8_t sck, uint8_t mosi, uint32_t hz);
int      od_spi_write(const uint8_t *p, size_t n);
int      od_i2c_configure(od_bus_id, uint8_t sda, uint8_t scl, uint32_t hz);
int      od_i2c_xfer(od_bus_id, uint8_t addr, const uint8_t *w, size_t wn, uint8_t *r, size_t rn);

/* --- time --- */
void     od_msleep(int32_t ms);
void     od_busy_wait(uint32_t us);
uint32_t od_uptime_get_32(void);

/* --- persistence: opaque blob, key/value semantics --- */
int      od_store_save(const uint8_t *blob, size_t len);
int      od_store_load(uint8_t *out, size_t cap, size_t *out_len);
int      od_store_clear(void);

/* --- crypto: only 3 primitives are actually needed --- */
int      od_aes_ecb_encrypt(const uint8_t key[16], const uint8_t in[16], uint8_t out[16]);
int      od_aes_cmac(const uint8_t key[16], const uint8_t *msg, size_t n, uint8_t mac[16]);
int      od_rng(uint8_t *out, size_t n);
/* CCM is ALREADY hand-rolled on top of ECB in opendisplay_pipe.c:174-405 — portable as-is. */

/* --- transport out --- */
int      od_notify(const uint8_t *p, size_t n);   /* BLE notify, or WiFi TCP write */
bool     od_link_connected(void);

/* --- misc --- */
int      od_device_id(uint8_t *out, size_t n);
void     od_reboot(void);
void     od_adc_read_mv(uint8_t pin_cfg, int32_t *mv);
```

**What lives in the core library** (fully portable C/C++, zero `#ifdef`): the config TLV parser + CRC; the whole pipe protocol (framing, dispatch, auth challenge/CMAC, session-key derivation, AES-CCM, replay window); the display pipeline (plane splitting, colour math, streaming zlib inflate, `bb_epaper` + panel map + panel quirks); the boot screen + QR + logo renderer; the LED sequencer, buzzer pattern player, button debouncer, GT911 driver, SHT40, BQ27220; and the wire structs (`structs.h`, 423 LOC, already **zero** platform ifdefs).

That is roughly **9,000-10,000 LOC of the ~15,000 in `Firmware/src/`** plus the vendored uzlib and bb_epaper — **~65% of the codebase becomes shared, single-source, single-bug-fix.**

**What stays platform-specific** — and would stay platform-specific *even under full Zephyr unification*:

| Concern | Zephyr (nRF54/nRF52) | ESP32 (Arduino/IDF) | Shared under **full Zephyr**? |
|---|---|---|---|
| BLE stack init/GATT registration | Zephyr host | NimBLE/Bluedroid | **Yes** (real win) |
| WiFi STA + TCP + mDNS | n/a | `WiFi.h`/`WiFiServer`/`MDNS` | Only if you accept Zephyr's WiFi |
| Deep sleep + ext0/ext1 wake | `sys_poweroff()` + GRTC | `esp_sleep_*` | **No** — even Zephyr uses raw `esp_sleep_*` |
| Power latch / GPIO isolate | n/a | `esp_sleep_config_gpio_isolate()` | **No** |
| RTC retention | `retained_mem` / `hwinfo_get_reset_cause()` | `RTC_DATA_ATTR` | **No** — different idioms |
| OTA/DFU | Adafruit UF2 bootloader | MCUboot (new) | Partly |
| Seeed_GFX / IT8951 13.3" | n/a | 29,400 LOC TFT_eSPI fork | **No** — no Zephyr port exists |

**Notice what that table says.** Full Zephyr unification collapses exactly **one** row (BLE stack init) that the core-library approach doesn't already collapse. Everything else — sleep, latch, retention, WiFi, Seeed_GFX — remains per-platform *under Zephyr too*.

### D.3 Verdict

**Extract the core library. Do not do a full Zephyr unification.**

In one line: **the thing a full Zephyr port buys you over a shared core library is a single unified BLE-host API — and you would pay for it with a hardware-SPI regression, an experimental WiFi stack, a from-scratch Seeed_GFX rewrite, and a flash-layout change that strands the ESP32 field fleet.**

Counter-argument taken seriously: *"a shared core library across two build systems is a maintenance tax forever."* True. But you **already pay that tax** (two repos, two build systems, today), and the core library **reduces** it by ~65% of the code. Full Zephyr unification would eliminate it entirely — but only after clearing the four blockers above, and only if Zephyr's ESP32 WiFi holds up.

**Recommended sequencing:**
1. **Now:** extract `libopendisplay` from `Firmware_NRF54/src/` (it's already shaped right). Two backends: `od_hal_zephyr.c` and `od_hal_arduino.cpp`. Keep ESP32 on Arduino/ESP-IDF.
2. **Then:** bring **nRF52840 onto Zephyr** using the UF2/`sdv7` partition layout (§B.1) — now a *backend swap*, not a port. Retires the Bluefruit/S140 stack and the priority-inversion hazards.
3. **Then:** fix the nRF54 bit-bang (cache pin decode; or go to hardware SPI via crossbar re-routing, §C.3).
4. **Only then, and only if WiFi maturity improves:** revisit ESP32-on-Zephyr, starting with **C3** (the one Espressif calls production-stable), as a *spike*, not a migration.

---

## E. Effort and risk

### E.1 Rough engineering-weeks (one competent embedded engineer; includes bring-up + on-hardware validation)

**Path 1 — Full Zephyr unification**

| Work item | Weeks | Notes |
|---|---:|---|
| nRF52840 Zephyr port (board, DTS, pin decode, PSA crypto, UF2 layout) | **3-5** | Low risk; §B |
| Close nRF54's existing gaps (deep sleep, PIPE_WRITE, OTA, the 4 protocol divergences) | **5-8** | Required regardless of path |
| First ESP32 SoC on Zephyr (say C3): BLE, config/NVS, display, touch, ADC, LEDC buzzer | **4-6** | BLE host is free; buses are the pain |
| ESP32 WiFi STA + TCP server + mDNS rewrite + throughput tuning | **4-7** | `wifi_service.cpp` is only 259 LOC but the tuning tail is long |
| ESP32 deep sleep + ext0/ext1/LP-GPIO wake + power latch + RTC retention | **3-5** | Still ESP-IDF APIs; **no reuse from nRF** |
| ESP32 MCUboot + sysbuild + new partition tables | **2-3** | |
| Each **additional** ESP32 SoC (S3, C6, classic) | **2-4 each** | ⇒ **6-12** for the other three |
| Hardware SPI with runtime pins (crossbar re-routing, per-SoC) — or accept the bit-bang regression | **2-4** | §C.3 |
| **Seeed_GFX / IT8951 13.3" panel family on Zephyr** | **6-12+** | 29,400 LOC TFT_eSPI fork, PSRAM sprites, raw `GPIO.out_w1ts` macros. **No Zephyr equivalent exists.** |
| **Total** | **≈ 35-62 weeks** | |

**Path 2 — Core library + Zephyr on Nordic only**

| Work item | Weeks |
|---|---:|
| Extract `libopendisplay` core (rename `nrf54_*` → `od_hal_*`, define the ~20-fn contract, split C/C++ cleanly) | **3-5** |
| `od_hal_arduino` backend + rewire `Firmware/src/` to consume the core (delete ~9k LOC of duplicated logic) | **4-6** |
| nRF52840 Zephyr port (as above) | **3-5** |
| Close nRF54's gaps (same work, same cost, **both paths**) | **5-8** |
| ESP32 keeps ESP-IDF/Arduino for WiFi/sleep/latch/OTA/Seeed_GFX | **0** |
| **Total** | **≈ 15-24 weeks** |

**Path 2 is roughly 2-3× cheaper and carries a fraction of the technical risk**, and it converges the *protocol and product logic* — which is where your bugs actually are. Every one of the four "dangerous divergences" in [FEATURE_PARITY_VS_FIRMWARE.md §4](FEATURE_PARITY_VS_FIRMWARE.md) — the `0x0082` opcode collision, the buzzer frequency mapping, the GRAY16 bpp mismatch, the fake `0x0051` DFU ack — **is a core-library bug, not a platform bug.**

### E.2 The single biggest thing that could kill a full Zephyr unification

**The ESP32-S3 13.3" panel path (`Seeed_GFX` / IT8951).**

Not WiFi. WiFi is the *obvious* risk and it's the one everyone would name — but it's bounded: 4 Mbps TCP after tuning is enough for your 4 KB LAN payloads, coex is upstream and auto-enabled, and if C6 WiFi isn't ready you can hold C6 back.

`Seeed_GFX` is unbounded. It is a **29,400-LOC TFT_eSPI fork**, Arduino-native to its bones: `SPI.begin()`, `psramFound()`, `ps_calloc()` sprite framebuffers, raw `GPIO.out_w1tc`/`out_w1ts` DC macros. It is selected at *runtime* by `panel_ic_type` 3000/3001 (`Firmware/src/display_service.cpp:500-506`), so it is not an optional SKU you can quietly drop — it's a shipping panel family behind a config byte. Porting TFT_eSPI to Zephyr is a project in its own right, has no upstream, and nobody has done it. **It is the one line item that can silently triple the estimate.**

**Runners-up, in order:**

2. **The unrecoverable-regression trap.** ESP32 has **no OTA today**. A full Zephyr migration must ship MCUboot *and* change the partition table — meaning existing ESP32 units cannot be field-migrated at all (they'd need a serial reflash), and the first Zephyr build you ship to *new* units is the one that has to work, because there's no rollback until MCUboot is in the field. **That's a one-way door with an experimental WiFi stack behind it.**
3. **Zephyr ESP32 WiFi on C6** — genuinely experimental, WPA3 doesn't build, blob-version-coupled with silent failures.
4. **The hardware-SPI regression** (§C) — recoverable via crossbar re-routing, but the fix is per-SoC platform code, which quietly undermines the whole premise of unifying.

---

## Sources

- [Zephyr — Espressif boards index](https://docs.zephyrproject.org/latest/boards/espressif/index.html) · [ESP32-S3-DevKitC board](https://docs.zephyrproject.org/latest/boards/espressif/esp32s3_devkitc/doc/index.html) · [Espressif Simple Boot / MCUboot / sysbuild](https://raw.githubusercontent.com/zephyrproject-rtos/zephyr/main/boards/espressif/common/building-flashing.rst)
- [`drivers/wifi/esp32/Kconfig.esp32`](https://raw.githubusercontent.com/zephyrproject-rtos/zephyr/main/drivers/wifi/esp32/Kconfig.esp32) · [`drivers/bluetooth/hci/hci_esp32.c`](https://raw.githubusercontent.com/zephyrproject-rtos/zephyr/main/drivers/bluetooth/hci/hci_esp32.c) · [`soc/espressif/common/Kconfig`](https://raw.githubusercontent.com/zephyrproject-rtos/zephyr/main/soc/espressif/common/Kconfig) · [`soc/espressif/common/poweroff.c`](https://raw.githubusercontent.com/zephyrproject-rtos/zephyr/main/soc/espressif/common/poweroff.c) · [`drivers/gpio/gpio_esp32.c`](https://raw.githubusercontent.com/zephyrproject-rtos/zephyr/main/drivers/gpio/gpio_esp32.c) · [`drivers/pinctrl/Kconfig` (`PINCTRL_DYNAMIC`)](https://raw.githubusercontent.com/zephyrproject-rtos/zephyr/main/drivers/pinctrl/Kconfig)
- [`dts/vendor/nordic/nrf52840_partition_uf2_sdv7.dtsi`](https://raw.githubusercontent.com/zephyrproject-rtos/zephyr/main/dts/vendor/nordic/nrf52840_partition_uf2_sdv7.dtsi) · [Adafruit Feather nRF52840 board (UF2 variants)](https://docs.zephyrproject.org/latest/boards/adafruit/feather_nrf52840/doc/index.html) · [nRF52840 DK board](https://docs.zephyrproject.org/latest/boards/nordic/nrf52840dk/doc/index.html)
- [Zephyr Espressif Deep Sleep sample](https://docs.zephyrproject.org/latest/samples/boards/espressif/deep_sleep/README.html) · [mDNS responder sample](https://docs.zephyrproject.org/latest/samples/net/mdns_responder/README.html)
- [Espressif — Zephyr Support Status](https://developer.espressif.com/software/zephyr-support-status/) · [Espressif — Maximizing Wi-Fi Throughput with ESP32 SoCs](https://developer.espressif.com/blog/2024/06/zephyr-max-wifi-throughput/) · [Espressif — RF Coexistence](https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-guides/coexist.html)
- [Hubble — Zephyr RTOS on ESP32-C6: What's Supported and What's Still Missing](https://hubble.com/community/guides/zephyr-rtos-on-esp32-c6-what-s-supported-and-what-s-still-missing/)
- [zephyr#51105 — esp32 wifi: http transmit rate too slow](https://github.com/zephyrproject-rtos/zephyr/issues/51105) · [zephyr#105733 — WPA3-SAE not working on ESP32-C6](https://github.com/zephyrproject-rtos/zephyr/issues/105733)
- [ZMK — Adafruit nRF52 Bootloader (Zephyr app on the UF2 bootloader, in production)](https://zmk.dev/docs/development/hardware-integration/bootloader/adafruit-nrf52)
