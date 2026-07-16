# RTOS & Runtime Model — Zephyr/NCS (nRF54) vs Arduino/FreeRTOS (sister repo)

> Generated 2026-07-13. Documentation-only audit.
> Companion docs: [ARCHITECTURE_NRF54.md](ARCHITECTURE_NRF54.md), [ARCHITECTURE_FIRMWARE_SISTER.md](ARCHITECTURE_FIRMWARE_SISTER.md), [FEATURE_PARITY_VS_FIRMWARE.md](FEATURE_PARITY_VS_FIRMWARE.md)

> Scope note: throughout, **"Zephyr offers"** describes kernel capability; **"this firmware uses"** describes what the OpenDisplay nRF54 code actually calls. The two are deliberately kept apart — the nRF54 port uses a *small* slice of Zephyr.

---

## 1. Zephyr on nRF54L15, concretely as used here

### 1.1 Kernel model

Zephyr is a single-address-space, statically-configured RTOS with a **priority-based preemptive scheduler**. Priorities are integers where **lower = higher priority**. Negative priorities (`-CONFIG_NUM_COOP_PRIORITIES .. -1`) are **cooperative** (never preempted by another thread; only yield voluntarily). Non-negative priorities (`0 .. CONFIG_NUM_PREEMPT_PRIORITIES-1`) are **preemptive**. There is no time-slicing unless `CONFIG_TIMESLICING` is enabled — neither `prj*.conf` sets it, so **two ready threads at the same preemptive priority will not round-robin**; the running one keeps the CPU until it blocks.

Threads present in this image (none of the priorities are overridden in `prj*.conf`, so Zephyr/NCS defaults apply):

| Thread | Priority | Stack | Origin |
|---|---|---|---|
| `main` | 0 (preemptible) | `CONFIG_MAIN_STACK_SIZE=4096` (`zephyr/prj.conf:22`) | runs `main()` at `src/main.c:24` |
| `idle` | lowest | — | kernel; enters WFI/WFE and drives tickless idle |
| `sysworkq` (system workqueue) | `-1` (**cooperative**, NCS default) | `CONFIG_SYSTEM_WORKQUEUE_STACK_SIZE=4096` (`prj.conf:23`) | kernel |
| BT RX thread | `CONFIG_BT_RX_PRIO` (default 8) | `CONFIG_BT_RX_STACK_SIZE=8192` (`prj.conf:50`) | Zephyr BT host |
| BT TX / HCI driver threads | host defaults | host defaults | Zephyr BT host + SoftDevice Controller |
| MPSL / SDC low-latency signal handlers | run in **IRQ context** (MPSL owns RADIO/TIMER0/RTC0 and the high-prio IRQs) | — | NCS SoftDevice Controller |
| **app display workqueue** | **14** (preemptible, *lower* than `main`) | **8192**, statically allocated | `src/opendisplay_ble.c:146-147`, started at `:164-167` |

The nRF54 firmware creates **exactly one** thread of its own — the display workqueue — and otherwise lives on `main` plus the system workqueue.

### 1.2 The actual scheduling shape of this firmware

`main()` is a **superloop that yields** (`src/main.c:34-59`):

```c
while (1) {
    if (opendisplay_ble_is_connected()) {
        opendisplay_ble_process();
        k_msleep(10);           // main.c:42
        continue;
    }
    ...
    idle_delay_ms(cfg->power_option.sleep_timeout_ms);   // main.c:50
    opendisplay_ble_update_msd(true);
}
```

and `idle_delay_ms()` (`src/main.c:10-22`) is a 100 ms-chunked cooperative delay that calls `opendisplay_ble_process()` between chunks — a **direct structural port of `idleDelay()` in the Arduino repo** (`Firmware/src/main.cpp:428-444`). `opendisplay_ble_process()` (`src/opendisplay_ble.c:707-723`) is the service pump: pipe drain, LED, buzzer, buttons, touch, advertising tick.

So the design is: **Arduino superloop semantics re-implemented on top of Zephyr threads**, with three deviations where Zephyr primitives were genuinely needed:

1. **GATT writes are not processed on the BT thread.** `od_gatt_write()` (`opendisplay_ble.c:287-298`) → `opendisplay_pipe_on_write()` (`opendisplay_pipe.c:1381-1390`) only `memcpy`s into a `k_msgq` and returns. `opendisplay_pipe_process()` (`:1410-1425`) drains it on `main`. The rationale is in the comment at `opendisplay_pipe.c:67-71`: EPD refresh waits and CCM crypto on the BT RX thread starve ATT and break service discovery on reconnect. This is exactly the `commandQueue` ring in the ESP32 firmware, but with a real kernel object instead of a hand-rolled SPSC ring.
2. **Advertising restart is deferred to work items** (`k_work_delayable s_adv_restart_work`, `opendisplay_ble.c:144`), because `recycled()` (`:416-422`) runs in a context where BT APIs are illegal — the comment says "Runs in ISR-like context: only queue work, no BT API calls here."
3. **The boot screen render runs on its own 8 KB workqueue** (`opendisplay_ble.c:161-170`) so a multi-second panel init/refresh cannot block `main` or the system workqueue.

### 1.3 Kernel objects: semantics, and which ones this firmware uses

| Object | Zephyr semantics | Used here? |
|---|---|---|
| `k_thread` / `K_THREAD_STACK_DEFINE` | Static stack, explicit priority. Stack is a *fixed array*; overflow → `CONFIG_HW_STACK_PROTECTION` fault (on by default for nRF54 w/ MPU) | Only via `K_THREAD_STACK_DEFINE(s_display_wq_stack, 8192)` — `opendisplay_ble.c:147` |
| `k_work` | One-shot item submitted to a queue; **runs on a thread**, so it may block. A pending item re-submitted is coalesced (submit is idempotent while pending) | `s_boot_display_work` (`opendisplay_ble.c:145`), `s_cs_setup_work` (`opendisplay_cs.c:61`) |
| `k_work_delayable` | Same, plus a timeout. `k_work_schedule()` is a **no-op if already scheduled**; `k_work_reschedule()` re-arms | `s_adv_restart_work` (`opendisplay_ble.c:144`), armed at `:187,195,202,420` |
| `k_work_q` | A private workqueue thread. Isolates long work from `sysworkq` | `s_display_work_q` at priority 14 (`opendisplay_ble.c:146,164-167`) |
| `k_timer` | Expiry callback runs **in ISR context** (system clock ISR). Must not block. May call ISR-safe APIs (`k_sem_give`, `k_work_submit`, `gpio_pin_set`, restart another `k_timer`) | LED (`opendisplay_led.c:48,116-119,139`), buzzer step + tone timers (`opendisplay_buzzer.c:70-71,250-270`) |
| `k_sem` | Counting semaphore. `k_sem_give()` ISR-safe; `k_sem_take(K_FOREVER/timeout)` blocks — **illegal in ISR with a nonzero timeout** | `s_cs_config_sem` (`opendisplay_cs.c:62`); given from the CS config-complete callback (`:84`), taken with a 10 s timeout in the work handler (`:146`) |
| `k_msgq` | Fixed-size-message FIFO with a **statically allocated ring**. `k_msgq_put` from ISR requires `K_NO_WAIT` | `K_MSGQ_DEFINE(s_pipe_msgq, sizeof(struct od_pipe_msg), 8, 4)` — `opendisplay_pipe.c:87`. Message is 509 B payload + header → ~4 KB of static BSS |
| `k_mutex` | Priority-inheritance mutex; thread-only | **Not used.** The pipe/display path is single-threaded on `main` by construction; the RAM caches in `opendisplay_config_storage.c:42,68` rely on that ("Config writes are serialized on the main thread, so a single scratch is safe") |
| `k_fifo`/`k_lifo`/`k_queue`/`k_heap`/`k_poll`/`k_condvar`/`k_pipe` | offered | **Not used** |
| `atomic_t` | lock-free ops, ISR-safe | `s_conn_gen`, `s_close_pending` (`opendisplay_pipe.c:88-89`) — the connection-generation counter that lets `main` drop stale queued frames from a closed link (`:1420-1422`) |

**ISR-context rules Zephyr enforces**, and how this code observes them:
- No blocking in ISR. The GPIO button IRQ handler (`opendisplay_button.c:31-34`) does *nothing* but set `volatile bool s_button_irq_pending`; the actual I2C/BLE work happens in `opendisplay_button_process()` on `main` (`:102-137`). The comment at `:26-28` says so explicitly.
- `k_timer` callbacks stay ISR-legal: `buzzer_tone_timer_cb` (`opendisplay_buzzer.c:256-270`) only does `nrf54_gpio_write()` + `k_timer_start()`; `led_timer_cb` (`opendisplay_led.c:116-119`) only sets a flag consumed by the polled `opendisplay_led_process()`.
- `bt_le_adv_start()` is never called from a callback; it's always routed through `adv_work_handler` (`opendisplay_ble.c:172-189`).

### 1.4 Every `CONFIG_*` actually set, and why

#### `zephyr/prj.conf` (base, all builds)

| Line | Option | Effect / why it matters here |
|---|---|---|
| 1-3 | `GPIO`, `SPI`, `I2C` | Enables the driver subsystems. **Note the irony**: `app.overlay:27-29,43-45` *disables* `spi00` and `i2c22` because the firmware bit-bangs both (`opendisplay_i2c.c`, `third_party/bb_epaper/src/nrf54_zephyr_io.inl`). Only the GPIO driver is truly used for the panel/touch buses |
| 8-9 | `SERIAL=n`, `UART_CONSOLE=n` | Battery-safe default: an unpowered USB-UART bridge would be back-fed through TX. Removes the UART driver + console thread entirely |
| 10-14 | `CONSOLE=y`, `PRINTK=y`, `USE_SEGGER_RTT=y`, `RTT_CONSOLE=y`, `LOG_BACKEND_RTT=y` | All `printf()`/`printk()` output goes out **SEGGER RTT** (a RAM ring buffer polled over SWD). Zero pin cost, near-zero timing cost. This is the primary debug channel |
| 16-20 | `STD_C99`, `CPP`, `REQUIRES_FULL_LIBCPP`, `NEWLIB_LIBC` | C++ is needed for `opendisplay_display.cpp`, `boot_screen.cpp` and `bb_epaper.cpp`. Newlib (not minimal libc) gives real `snprintf`/float formatting — and forces `src/newlib_stubs.c:3-5` to provide the `_fini()` symbol newlib's C++ init expects |
| 22 | `MAIN_STACK_SIZE=4096` | `main` runs the whole command pipeline (config parse, zlib inflate, EPD writes). 4 KB is why the ~4 KB config records in `opendisplay_config_storage.c:42,68` are `static`, not stack locals — the comments call this out |
| 23 | `SYSTEM_WORKQUEUE_STACK_SIZE=4096` | The system workqueue runs `adv_work_handler` and, notably, `cs_setup_work_handler` |
| 24 | `HEAP_MEM_POOL_SIZE=8192` | The kernel heap backing `k_malloc`/`bt_buf` allocations. The app itself never calls `k_malloc`; this exists for the BT host and PSA/mbedTLS |
| 26 | `HWINFO=y` | `hwinfo_get_device_id()` → the 6-hex-digit device name `OD%06X` (`opendisplay_ble.c:205-215,561`) |
| 28-29 | `SENSOR=y`, `TEMP_NRF5=y` | Die temperature for the MSD payload. Comment at `:219-223`: with `CONFIG_TEMP_NRF5_MPSL` (auto-selected on SoftDevice builds) `sample_fetch` goes through `mpsl_temperature_get()`, which is safe **only after `bt_enable()`** — hence `read_chip_temperature_once()` is called at `:689`, after `:681` |
| 33-34 | `ADC=y`, `ADC_NRFX_SAADC=y` | Battery sense. `app.overlay:14-17` exposes all 8 SAADC channels under `zephyr,user` so `opendisplay_battery.c` can pick one **at runtime** by index — a devicetree workaround for a config-driven pin |
| 36-37 | `BT=y`, `BT_PERIPHERAL=y` | Zephyr BT **host** + peripheral role |
| 40 | `BT_HCI_VS=y` | Vendor-specific HCI. Required because there is no stable `bt_le_*` runtime TX-power API; `apply_tx_power()` (`opendisplay_ble.c:500-535`) issues `BT_HCI_OP_VS_WRITE_TX_POWER_LEVEL` directly and logs the value the controller actually selected |
| 45-47 | `BT_L2CAP_TX_MTU=512`, `BT_BUF_ACL_RX_SIZE=512`, `BT_BUF_ACL_TX_SIZE=512` | 512-byte ATT MTU for the image pipe |
| 50 | `BT_RX_STACK_SIZE=8192` | Headroom on the BT RX thread for GATT/ATT **plus** the `k_msgq_put` enqueue path |
| 55-57 | `BT_L2CAP_TX_BUF_COUNT=12`, `BT_ATT_TX_COUNT=12`, `BT_CONN_TX_MAX=12` | A config read notifies up to 10 chunks back-to-back. With the default 3 buffers, completions are handled on the *same* thread that is emitting, so the pool can't refill mid-loop → deadlock/drop. All three pools independently cap in-flight notifications, so all three must be raised |
| 59-65 | `SETTINGS`, `SETTINGS_RUNTIME`, `NVS`, `SETTINGS_NVS`, `FLASH`, `FLASH_MAP`, `FLASH_PAGE_LAYOUT` | Persistent config storage. `opendisplay_config_storage.c` stores the whole blob under one key `"od/config"` via `settings_save_one`/`settings_load_one` (`:55,83`), backed by NVS in the `storage_partition` |
| 67-74 | `MBEDTLS`, `MBEDTLS_BUILTIN`, `MBEDTLS_PSA_CRYPTO_C`, `ENTROPY_*`, `PSA_WANT_KEY_TYPE_AES`, `PSA_WANT_ALG_CMAC`, `PSA_WANT_ALG_ECB_NO_PADDING` | PSA Crypto for the AES-CCM session (`opendisplay_pipe.c:14` includes `<psa/crypto.h>`; `psa_crypto_init()` at `:117`) |
| 76-77 | `LOG=y`, `LOG_DEFAULT_LEVEL=3` | Deferred logging. Only `opendisplay_cs.c:17` registers a `LOG_MODULE`; everything else uses raw `printf` |

#### `zephyr/prj_ncs.conf` (merged on **every** build — `CMakeLists.txt:8`)

| Line | Option | Effect |
|---|---|---|
| 3-5 | `BT_SMP`, `BT_BONDABLE`, `BT_SETTINGS` | Pairing/bonding, keys persisted through the settings subsystem. `settings_load()` at `opendisplay_ble.c:687` is what restores them |
| 6 | `BT_CONN_DYNAMIC_CALLBACKS` | Runtime `bt_conn_cb` registration (used by RAS) |
| 7 | `BT_GAP_AUTO_UPDATE_CONN_PARAMS=n` | Suppresses the host's automatic LE param-update request (the `err -128` noise described in `prj.conf:42-44`) |
| 8-10 | `BT_CTLR_DATA_LENGTH_MAX=251`, `BT_CTLR_PHY_2M=y`, `BT_AUTO_PHY_UPDATE=n` | Controller supports DLE 251 and 2M PHY, but does **not** auto-negotiate — the central drives it |
| 11 | `BT_MAX_CONN=1` | Single-peripheral link. Cuts per-connection RAM |

The `BT_CTLR_*` prefix and the `BT_CTLR_SDC_*` options in `prj_cs.conf` confirm the link layer is the **NCS SoftDevice Controller** (`CONFIG_BT_LL_SOFTDEVICE`, the NCS default), running on **MPSL** — a closed-source, timing-guaranteed radio scheduler that owns RADIO/TIMER0/RTC0 and the top IRQ priorities. Practical consequence: **radio timing is protected from the application by hardware priority**, not by RTOS scheduling. No amount of `main`-thread stalling can cause a connection drop.

#### `zephyr/prj_cs.conf` (also merged on every build — `CMakeLists.txt:9`)

Channel Sounding, compiled in but **runtime-gated** by `DEVICE_FLAG_CHANNEL_SOUNDING` (`opendisplay_cs.c:19-30`). `BT_CHANNEL_SOUNDING=y`, `BT_RAS`/`BT_RAS_RRSP=y` (Ranging Service, reflector responder), 1 antenna / 2 paths, `BT_CTLR_SDC_CS_ROLE_REFLECTOR_ONLY=y`, `BT_TRANSMIT_POWER_CONTROL=y`. This is an nRF54L-only feature (BT 6.0 CS) with no analogue in the nRF52/ESP32 repo.

#### `zephyr/prj_uart.conf` (opt-in via `PROFILE=uart` — `CMakeLists.txt:15-19`, `build.sh:18-20`)

Flips the console: `SERIAL=y`, `UART_CONSOLE=y`, `USE_SEGGER_RTT=n`, `LOG_BACKEND_UART=y`, and crucially **`LOG_MODE_IMMEDIATE=y`** — logs are emitted synchronously from the calling context rather than deferred to the logging thread. Slower and it perturbs timing, but nothing is lost on a crash.

#### `zephyr/prj_lm20_extra.conf`

A single commented-out line disabling `BT_CTLR_ASSERT_OPTIMIZE_FOR_SIZE` — a gcc 8.2 toolchain workaround for the LM20 board. Applied only when `BOARD MATCHES ".*lm20.*"` (`CMakeLists.txt:11-13`).

#### Notably **absent** (defaults apply)

- **`CONFIG_PM` / `CONFIG_PM_DEVICE`: not set.** No Zephyr power-management subsystem, no device PM, no system-off state. Idle is *just* the idle thread's WFI/WFE.
- **`CONFIG_TICKLESS_KERNEL`: not set, but defaults to `y`** on nRF (the nRF GRTC/RTC timer driver is tickless-capable). So the system tick does not fire at a fixed rate; the kernel programs the next wake for the nearest timeout. This is what makes `k_msleep(10)` in the connected loop (`main.c:42`) cheap-ish and `idle_delay_ms(500)`/`k_msleep(100)` genuinely low-power.
- `CONFIG_BOOTLOADER_MCUBOOT`: **not set**. No sysbuild, no MCUboot, no DFU — confirmed by `opendisplay_ble_schedule_dfu()` at `opendisplay_ble.c:725-728`, which just prints "DFU not implemented on nRF54 yet".
- No `CONFIG_TIMESLICING`, no `CONFIG_THREAD_ANALYZER`, no shell.

---

## 2. The Arduino/FreeRTOS model in the sister repo

### 2.1 `setup()` / `loop()` **are** a FreeRTOS task — on both platforms

**nRF52840 (Adafruit nRF52 core, Seeed fork):**
`framework-arduinoadafruitnrf52-seeed/cores/nRF5/main.cpp:88` —
```c
xTaskCreate(loop_task, "loop", LOOP_STACK_SZ /* 256*4 words = 4 KB */, NULL, TASK_PRIO_LOW, &_loopHandle);
...
vTaskStartScheduler();                                  // main.cpp:94
```
and `loop_task` is `while(1) { loop(); yield(); }` (`main.cpp:66-74`). Priorities (`cores/nRF5/rtos.h:57-61`):

```
TASK_PRIO_LOWEST  = 0   idle
TASK_PRIO_LOW     = 1   ← loop()  (i.e. ALL of setup()/loop() runs here)
TASK_PRIO_NORMAL  = 2   FreeRTOS timer task, Adafruit callback task
TASK_PRIO_HIGH    = 3   ← Bluefruit "BLE" task and "SOC" task
```
The BLE task and SOC task are created in `libraries/Bluefruit52Lib/src/bluefruit.cpp:473,480` at `TASK_PRIO_HIGH`. FreeRTOS here is **preemptive** (`configUSE_PREEMPTION=1`, `FreeRTOSConfig.h:50`), 1024 Hz tick (`:55`), 5 priority levels (`:56`), tickless idle **on** (`configUSE_TICKLESS_IDLE=1`, `:52`), idle hook on (`:76`).

**The consequence is the single most important structural fact about the nRF52 build**: `imageCharacteristic.setWriteCallback(imageDataWritten)` (`Firmware/src/ble_init.cpp:167`) registers the command handler **directly** on the Bluefruit callback path. So on nRF52, **the entire OpenDisplay command pipeline — config parsing, crypto, EPD SPI writes, multi-second refresh waits — executes on a priority-3 task, above `loop()`.** `Firmware/docs/FINDINGS_NONBLOCKING_LOOP_2026-07-13.md` states this explicitly, and it's the reason `pwrmgmLock` exists at all (`Firmware/src/display_service.cpp:204-217`): the panel session is genuinely touched from two tasks. That file also documents the resulting **priority-inversion livelock** and its fix:

> "MUST yield while waiting: on nRF this runs on the Bluefruit callback task, which outranks the loop task holding the lock… A bare busy-spin starves the lower-priority holder forever on the single core; `delay(1)` is `vTaskDelay`, which blocks the spinner so the holder can finish and release." — `display_service.cpp:205-211`

**ESP32 (Arduino-ESP32 on ESP-IDF):**
`framework-arduinoespressif32/cores/esp32/main.cpp:113` —
```c
xTaskCreateUniversal(loopTask, "loopTask", ARDUINO_LOOP_STACK_SIZE /* 8192 */, NULL, 1, &loopTaskHandle, ARDUINO_RUNNING_CORE);
```
`loop()` runs at **priority 1** on the `loopTask`, with `esp_task_wdt_reset()` called each pass (`main.cpp:79-81`) — hence `-DCONFIG_FREERTOS_WATCHDOG_TIMEOUT_S=120` in every ESP32 env (`platformio.ini:47,71,95,121,146,163,183,204,232`): the firmware knowingly blocks `loopTask` for many seconds inside an EPD refresh and would otherwise trip the task WDT.

Hidden ESP-IDF tasks the Arduino app never sees: `IDLE0`/`IDLE1`, `esp_timer`, `Tmr Svc` (FreeRTOS timer service), `ipc0`/`ipc1`, `btController` + `BTC_TASK`/`BTU_TASK` (Bluedroid) or `nimble_host`, `wifi` + `tiT` (LwIP TCP/IP), and `sys_evt`. The BLE `onWrite` callback in `Firmware/src/esp32_ble_callbacks.h:75-125` therefore runs on a **Bluedroid/NimBLE host task**, not on `loopTask`.

### 2.2 What `delay()` and `yield()` actually do

- **nRF52**: `delay(ms)` → `vTaskDelay(ms2tick(ms))` (`cores/nRF5/delay.c:33-48`). It **blocks the calling task and lets equal-or-lower priority tasks run**; higher-priority tasks (BLE at 3) run regardless. `yield()` → `taskYIELD()` (`cores/nRF5/rtos.cpp:75-82`). `millis()` → `tick2ms(xTaskGetTickCount())` (`delay.c:28-31`) — i.e. **`millis()` is the FreeRTOS tick count**, at 1024 Hz.
- **ESP32**: `delay(ms)` → `vTaskDelay(ms / portTICK_PERIOD_MS)` (`esp32-hal-misc.c:212-213`). Default IDF tick is 100 Hz (`portTICK_PERIOD_MS == 10`), so **`delay(1)` .. `delay(9)` all round to 0 or 1 ticks** — `delay(1)` is *not* a 1 ms sleep, it's "yield until the next tick". `millis()` is `esp_timer_get_time()/1000` — a real 64-bit microsecond timer, independent of the tick.

### 2.3 The cooperative assumption this creates

The design contract that emerges is: **`loop()` must be entered often enough** that everything polled inside it (`processLedFlash`, `epdSessionTick`, `buzzerService`, `processButtonEvents`, `processTouchInput`, `handleWiFiServer`) stays responsive. `idleDelay()` (`Firmware/src/main.cpp:428-444`) is the codification of that contract: it never calls bare `delay(N)` for large N, it chops the wait into 100 ms slices and pumps the services between them.

This assumption is violated in exactly one place, and it is documented: `waitforrefresh()` (`Firmware/src/display_service.cpp:509-535`) polls `bbepIsBusy()` with `delay(10)` for up to `timeout*100` iterations. On **ESP32** the command handler runs *on* `loopTask`, so during a 1–5 s refresh **`loop()` is inside the refresh and nothing else in the superloop runs at all** — the buzzer note drones. On **nRF52** the handler runs on the higher-priority Bluefruit task, so `delay(10)` = `vTaskDelay` lets `loop()` keep running at 100 ms granularity. Same source file, two completely different runtime behaviours — the clearest possible demonstration that "where your callback runs" is the whole ballgame.

### 2.4 What the ESP32 build does about it

It re-invents `k_msgq` by hand. `esp32_ble_callbacks.h:105-118` is an SPSC ring with explicit `__atomic_load_n(..., __ATOMIC_ACQUIRE)` / `__atomic_store_n(..., __ATOMIC_RELEASE)` publishing, 33 slots × 256 B (`main.h:387-388`); `loop()` drains it with a matching acquire/release pair and a **bounded** drain of `COMMAND_QUEUE_SIZE` commands per pass (`main.cpp:315-333`). The response side is a second 10-slot ring flushed *between* commands inside the drain (`main.cpp:237-259,331`) because the ACK burst from a 33-command drain would otherwise overflow it. `-DPIPE_SMALL_DRAM_WINDOW` on the classic-ESP32 env (`platformio.ini:229`) exists purely because 33 × 256 B of static DRAM doesn't fit that part.

The nRF52 build has **no such queue** — it calls `imageDataWritten` straight from the BLE task.

---

## 3. Head-to-head

### Scheduling determinism & latency

| | Zephyr / nRF54 | Arduino / FreeRTOS |
|---|---|---|
| Where BLE commands execute | `main`, drained from `k_msgq` (`pipe.c:1410-1425`) | **ESP32**: `loopTask` via a hand-rolled ring. **nRF52**: *directly on the priority-3 BLE task* |
| Radio timing guarantee | **Hardware**: MPSL + SDC own the top IRQ priorities. Application stalls cannot break a connection | **nRF52**: SoftDevice, same guarantee. **ESP32**: BT controller task is a *software* priority — a long `loopTask` block is tolerable only because the controller task outranks it |
| Worst-case service latency | `k_msleep(10)` when connected → ~10 ms; `idle_delay_ms` → 100 ms when idle (`main.c:19,42`) | `idleDelay` 100 ms slices (`main.cpp:440`); **but on ESP32, unbounded (1–5 s) during an EPD refresh** |
| Preemption of app work | `main` (prio 0) preempts the display workqueue (prio 14) and BT RX (prio 8) whenever it becomes ready. No time-slicing → no fairness between equal priorities | ESP32 `loopTask` at prio 1 is preempted by everything; nRF52 `loop` at prio 1 is preempted by BLE(3), timers(2), callbacks(2) |
| Known hazard **actually present** | `cs_setup_work_handler` blocks on `k_sem_take(..., K_SECONDS(10))` (`opendisplay_cs.c:146`) **on the system workqueue** — and `s_adv_restart_work` is a delayable on that same queue. If CS config never completes, advertising restart is stalled for up to 10 s. The `opendisplay_ble_process()` fallback at `opendisplay_ble.c:719-722` re-submits, but to the same blocked queue | Priority inversion on `pwrmgmLock`, fixed with `delay(1)` (`display_service.cpp:205-211`) |

### Power / idle

| | Zephyr / nRF54 | Arduino / FreeRTOS |
|---|---|---|
| Idle mechanism | Tickless kernel (default-on for nRF) + the idle thread's WFI. `k_msleep()` programs a single GRTC compare and the core sleeps until then | **nRF52**: FreeRTOS `configUSE_TICKLESS_IDLE=1` (`FreeRTOSConfig.h:52`) — `vTaskDelay` also suppresses ticks. **ESP32**: IDF automatic light-sleep is **off** by default; `delay()` just parks `loopTask` and the idle task spins into WFI |
| Explicit low-power calls | **None**. No `CONFIG_PM`, no `pm_state_force`, no system-off | nRF52 `ble_init.cpp:190-191`: `sd_power_mode_set(NRF_POWER_MODE_LOWPWR)` + `sd_power_dcdc_mode_set(NRF_POWER_DCDC_ENABLE)` |
| Deep sleep | **Not implemented** — `opendisplay_ble_schedule_deep_sleep()` prints "not implemented on nRF54 yet" (`opendisplay_ble.c:730-733`) | ESP32: full `esp_deep_sleep_start()` path (see §5) |

### Memory model

- **Zephyr**: every kernel object is a static, statically-sized C struct. `K_MSGQ_DEFINE` (`pipe.c:87`) reserves its 8 × ~513 B ring in BSS at link time; `K_THREAD_STACK_DEFINE` (`ble.c:147`) reserves 8 KB. The kernel heap is a hard 8 KB (`prj.conf:24`); the app never calls `k_malloc`. Big buffers are file-scope statics: `s_cfg_chunk` (4 KB), `s_pipe_enc_buf[544]`, `s_plain_buf[512]` (`pipe.c:41,52-54`). `-DOPENDISPLAY_ZLIB_USE_HEAP_WINDOW=0` (`CMakeLists.txt:66`) pins the inflate window in static storage too. **Result: no heap fragmentation, and link-time failure instead of runtime OOM.**
- **Arduino**: `String` is used everywhere in logging (`main.cpp:58,74,137,…`), which is heap churn on every log line; ESP32 uses `-DOPENDISPLAY_ZLIB_USE_HEAP_WINDOW=1` (`platformio.ini:43`) and PSRAM. nRF52 pins `=0` (`platformio.ini:26`) for the same static-allocation reason. FreeRTOS's own `configTOTAL_HEAP_SIZE=4096` is annotated "not used since we use malloc" (`FreeRTOSConfig.h:58`) — the Adafruit core routes FreeRTOS allocation to newlib `malloc`.

### Stack sizing

Zephyr sizes are explicit and per-thread (`MAIN_STACK_SIZE=4096`, `SYSTEM_WORKQUEUE_STACK_SIZE=4096`, `BT_RX_STACK_SIZE=8192`, display WQ 8192) and are *the* reason two big records are `static` (`config_storage.c:38-42,66-68`). Arduino sizes are buried in the core: 4 KB for `loop` on nRF52 (`main.cpp:42`), 8 KB on ESP32 (`main.cpp:17`) — and there's no equivalent of Zephyr's `CONFIG_HW_STACK_PROTECTION`; nRF52 only gets a `vApplicationStackOverflowHook` that prints and spins (`rtos.cpp:84-88`).

### Driver models

- **Zephyr**: devicetree. Pins are `DEVICE_DT_GET(DT_NODELABEL(gpio0..3))` + a port/pin pair (`nrf54_gpio.c:7-23`). Because OpenDisplay's wire config carries an 8-bit pin byte, the port lives in the high nibble and the pin in the low nibble: `port = cfg >> 4; pin = cfg & 0x0F` (`nrf54_gpio.c:30-31`) — so `P2.1` is `0x21`, `P1.10` is `0x1A`. `app.overlay` then has to *disable* `spi00` and `i2c22` (`:27-29,43-45`) because the board DTS claims the same pads the firmware bit-bangs. **DTS is not optional infrastructure here — it actively fights the runtime-config model, and the overlay is the referee.**
- **Arduino**: `pinMode(pin, OUTPUT)` / `digitalWrite(pin, LOW)` with a flat integer pin space defined by `variants/nrf52840custom/variant.cpp`. The same config byte is used directly as an Arduino pin number (`main.cpp:755-758`).

### Build config

`Kconfig` (`prj*.conf`, merged by `CMakeLists.txt:8-19`) + DTS overlay + CMake `target_compile_definitions` (`CMakeLists.txt:62-86`) + `west build` driven by `build.sh:34`, with an NCS toolchain activated by parsing `environment.json` (`ncs-env.sh:45-64`). Versus: one `platformio.ini` with per-env `build_flags` `-D`s and `board_build.*` keys, plus pre/post extra_scripts (`platformio.ini:8-9,17-19`).

### Debuggability

Zephyr wins decisively: SEGGER RTT console + `CONFIG_LOG` with per-module levels (`opendisplay_cs.c:17`), a UART profile switchable by one env var (`PROFILE=uart ./build.sh`, `CMakeLists.txt:15-19`) with `LOG_MODE_IMMEDIATE` for crash-adjacent logs. The Arduino side does `Serial.println(String(...))` and has to hand-roll `flushLog()` (`main.cpp:678-687`) because the IDF panic handler only drains the *console* UART, not the secondary log UART.

### OTA / DFU

- **nRF54: none.** No MCUboot, no sysbuild, `opendisplay_ble_schedule_dfu()` is a stub (`opendisplay_ble.c:725-728`). Flashing is `west flash` over SWD (`flash.sh:10`). *This is the single biggest functional gap versus the sister repo.*
- **nRF52**: Adafruit `BLEDfu` service (`main.h:375`, registered at `ble_init.cpp:176-180` only when encryption is off), plus a manual jump: set GPREGRET to `0xB1`, disable the SoftDevice, mask all NVIC, and `bootloader_util_app_start(NRF_UICR->NRFFW[0])` (`device_control.cpp:679-694`). UF2 output via `scripts/nrf_uf2_post.py` (`platformio.ini:19`).
- **ESP32**: `esp_restart()` and "OTA typically handled via WiFi" (`device_control.cpp:702-705`).

### BLE stack

| | Host | Controller | GATT registration |
|---|---|---|---|
| nRF54 | Zephyr BT host | **SoftDevice Controller** (NCS, on MPSL) | `BT_GATT_SERVICE_DEFINE` — **compile-time, static** (`opendisplay_ble.c:307-313`); handle order is fixed at link |
| nRF52 | Adafruit Bluefruit52Lib | Nordic **SoftDevice S140 7.3.0** (`boards/nrf52840custom.json`) | Runtime `imageService.begin()` / `imageCharacteristic.begin()` — order matters, hence the comment at `ble_init.cpp:169-175` about registering `bledfu` **last** so conditional DFU presence doesn't shift the app characteristic's CCCD handle |
| ESP32 | Bluedroid (default) or NimBLE — both branches present (`esp32_ble_callbacks.h:78`, `ble_init.cpp:27-29`) | ESP32 BT controller | Runtime `BLEDevice::createServer()` (`ble_init.cpp:273-276`) |

---

## 4. Porting gotchas (A→B and B→A)

1. **"Where does my callback run?" changes on every port.** B/nRF52 runs command handlers on the BLE task; B/ESP32 runs them on `loopTask` via a ring; A runs them on `main` via `k_msgq`. Any code that assumes "my handler and `loop()` never overlap" is correct on ESP32, wrong on nRF52, and correct-by-construction on nRF54.
2. **Blocking in an ISR-ish context.** Zephyr will *fault or assert*, not merely misbehave. `recycled()` (`ble.c:416-422`) and `button_irq_handler()` (`button.c:31-34`) both had to be reduced to flag-set/work-submit. Arduino's `attachInterrupt` (`device_control.cpp:660`) tolerates far more (though the code correctly doesn't rely on it).
3. **Workqueue starvation.** Porting an Arduino "just call it from the callback" handler into a `k_work` handler is *not* free: `k_work_submit()` targets the **system workqueue**, shared with the BT host and (here) the CS setup handler that blocks for up to 10 s (`opendisplay_cs.c:146`). The nRF54 port already had to spin up a *separate* 8 KB workqueue for the boot-screen render (`ble.c:161-170`) for exactly this reason. Rule of thumb: **anything that can block > a few ms needs its own `k_work_q`.**
4. **`k_work_schedule` vs `k_work_reschedule`.** `k_work_schedule()` is a **no-op if the item is already scheduled**. `schedule_adv_restart()` (`ble.c:191-196`) has to `k_work_cancel_delayable()` first to actually change the delay — a subtle difference from `SoftwareTimer.reset()` in Bluefruit (`ble_init.cpp:154`).
5. **Stack overflow.** Zephyr stacks are fixed and small (`main` 4096). Two ~4 KB config records had to become `static` (`config_storage.c:38-42,66-68`). Any Arduino code with a `uint8_t buf[4096]` local will hard-fault on the way in.
6. **Devicetree pin encodings.** Arduino: a flat integer. Zephyr: `(port, pin)` on a `struct device *`. The nRF54 port smuggles the port through the config byte's high nibble (`nrf54_gpio.c:30-31`) — and then must **disable the board's own `spi00`/`i2c22` nodes** (`app.overlay:27-29,43-45`) or the peripherals contend with the bit-banged pads. There is no Arduino analogue of this failure mode.
7. **Storage.** A: `settings` + NVS, one key (`settings_save_one("od/config", ...)`, `config_storage.c:55`). B: a **file** on Adafruit LittleFS (`InternalFS`) or ESP32 LittleFS (`encryption.cpp:760-791`). Porting means swapping a filesystem API for a key/value API — and NVS records are size-capped per key, so a 4 KB blob is near the practical limit.
8. **Timing APIs.** `millis()` → `k_uptime_get_32()`; `delay()` → `k_msleep()`; `delayMicroseconds()` → `k_busy_wait()`. The port abstracts all three behind `od_msleep` / `od_uptime_get_32` / `od_busy_wait` (`nrf54_zephyr_time.c:5-18`, declared in `nrf54_zephyr_compat.h:12-14`) so `bb_epaper`'s Arduino-shaped `delay()`/`delayMicroseconds()` can be shimmed in `third_party/bb_epaper/src/nrf54_zephyr_io.inl:86-103`. Semantic trap: **`millis()` on nRF52 is the FreeRTOS tick** (1024 Hz), while `k_uptime_get_32()` is true milliseconds — code doing tight `millis()` arithmetic can shift by ~2.4%.
9. **C vs C++ linkage.** A is C-dominant with a C++ island (`opendisplay_display.cpp`, `boot_screen.cpp`, `bb_epaper.cpp`), so every shim header needs `extern "C"` guards (`nrf54_zephyr_compat.h:8-10,17-19`) and `CONFIG_CPP=y`/`CONFIG_REQUIRES_FULL_LIBCPP=y` (`prj.conf:18-19`) — which in turn drags in a newlib symbol the C-only link doesn't provide, hence `src/newlib_stubs.c:3-5` defining an empty `_fini()`. B is C++-dominant (`.cpp` everywhere) with `String`, lambdas (`main.cpp:787`), and range-for — none of which survive a move into a `.c` file.
10. **No `String`, no `Serial`.** Every `writeSerial(String(...))` becomes `printf(...)` with explicit format specifiers (`ble.c:577-578`). This is where most of the mechanical porting time goes.
11. **`bt_hci_cmd_alloc(K_FOREVER)`** (`ble.c:510`) is a blocking allocation — legal on `main`/workqueue, fatal in an ISR. It's called from `connected()` (`:396`), which runs on the BT RX thread. Fine today, but it's a latent constraint any refactor must preserve.

---

## 5. Power / sleep — what each repo actually does

### A) `Firmware_NRF54`

**It does not sleep.** Concretely:

- No `CONFIG_PM`, no `CONFIG_PM_DEVICE`, no `pm_state_force()`, no `sys_poweroff()`, no RAM-retention configuration anywhere in `zephyr/prj*.conf` or `src/`.
- `opendisplay_ble_schedule_deep_sleep()` is a stub that prints "deep sleep not implemented on nRF54 yet" (`opendisplay_ble.c:730-733`).
- The only power measures taken are **peripheral-level, not system-level**:
  - External SPI NOR flash is put into deep power-down (`0xB9`) by bit-bang after every config load — `board_nrf54_flash_powerdown()` (`board_nrf54.c:48-70`), called from `flash_powerdown_from_config()` (`ble.c:585-604,672,611`).
  - The EPD rail is prepared/parked via GPIO (`board_nrf54_prepare_epd_rail()`, `board_nrf54.c:37-41`).
  - On the LM20 board, the nPM1300 SHPHLD pin is asserted at boot (`board_nrf54.c:11-15`) — the hold, not a ship-mode entry.
  - `CONFIG_SERIAL=n` (`prj.conf:8`) removes UART current draw and USB back-feed.
- The *actual* low-power behaviour is therefore whatever **tickless idle** gives you: `main` spends its time in `k_msleep(100)` (`main.c:19`) or `k_msleep(10)` (`main.c:42`); the idle thread WFIs between GRTC compares; MPSL/SDC wakes the core for radio events. That is genuinely low-power for an advertising peripheral (µA-class between adv events) but it is **not** system-off, and there is **no RAM retention / wake-on-GPIO / timer-wake reboot cycle**.

### B) `Firmware`

**nRF52840:** also no system-off. It sets the SoC to `NRF_POWER_MODE_LOWPWR` with the DC/DC regulator enabled (`ble_init.cpp:190-191`) and relies on FreeRTOS tickless idle + `vTaskDelay`. There is **no `sd_power_system_off()`** call in the repo. `main.cpp:69` says it outright: *"NRF has no deep-sleep wake path."* External flash is power-downed the same way (`main.cpp:731-780`). GPREGRET is used, but for **DFU entry**, not sleep (`device_control.cpp:679-680`).

**ESP32:** a complete deep-sleep state machine, and it is the most elaborate power logic in either repo — `enterDeepSleep()` (`main.cpp:468-530`):
1. Gated on `power_mode == 1` (battery) **and** `deep_sleep_time_seconds > 0`.
2. Re-checks for a live BLE link, and for the **min-wake hold** (`minWakeHoldActive()`, `main.cpp:171-179`) — a floor that keeps the device awake after a button wake or first boot.
3. `epdSessionForceOff()` — drops the panel rail.
4. Stops advertising, `BLEDevice::deinit(true)`, clears handles.
5. `esp_sleep_enable_timer_wakeup()`, then `armButtonWakeSources()` (`main.cpp:523`) — EXT0/EXT1 GPIO wake.
6. `powerLatchHoldForSleep()` (`gpio_hold_en()` on the latch pin), then `esp_deep_sleep_start()`.

On the way back up, `setup()` reads `esp_sleep_get_wakeup_cause()` and `esp_reset_reason()` (`main.cpp:73-93`) and takes a **short wake path**: it *skips* `initDisplay()` entirely (keeping the panel image — "the wake path's main energy saving", `main.cpp:109-111`), defers WiFi, and arms an advertising window sized by `sleep_timeout_ms` (`main.cpp:137-146`). The `RTC_DATA_ATTR` `deep_sleep_count` is documented as **not surviving panic/WDT/SW/brownout resets** (`main.cpp:86-91`) — a hardware-confirmed finding recorded in `docs/FINDINGS_DEEP_SLEEP_WAKE_BOOT_SCREEN_2026-07-07.md`. There's also a hard power-off path via a D-flip-flop latch (`powerLatchPowerOff()`, `device_control.cpp:~720`) for boards with `DEVICE_FLAG_PWR_LATCH_DFF`.

### The bottom line on power

The nRF54 firmware today gets its efficiency **for free from Zephyr's tickless idle**, and does not use any of the mechanisms (Zephyr `CONFIG_PM`, `sys_poweroff()`, nRF54L RAM retention + GRTC/GPIO wake, MCUboot-less reboot cycling) that would let it match the ESP32's duty-cycled behaviour. Achieving parity would mean adding `CONFIG_PM` + `CONFIG_PM_DEVICE`, a `PM_STATE_SOFT_OFF` transition with `gpio_pin_interrupt_configure(..., GPIO_INT_LEVEL_ACTIVE)` as the wake source, and — because Zephyr system-off is a *reset*, not a resume — the same "did I wake from sleep?" branch that `setup()` already has on ESP32, keyed off `hwinfo_get_reset_cause()` instead of `esp_sleep_get_wakeup_cause()`.
