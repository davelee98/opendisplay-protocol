# OpenDisplay nRF54 — Architecture & Runtime Map

> Generated 2026-07-13. Documentation-only audit of `Firmware_NRF54` @ `635d7d2`.
> Companion docs: [RTOS_COMPARISON.md](RTOS_COMPARISON.md), [FEATURE_PARITY_VS_FIRMWARE.md](FEATURE_PARITY_VS_FIRMWARE.md), [ARCHITECTURE_FIRMWARE_SISTER.md](ARCHITECTURE_FIRMWARE_SISTER.md)

# OpenDisplay nRF54 Firmware — Architectural Map

All claims cite `file:line` in `/home/davelee/opendisplay/Firmware_NRF54`. Target: Seeed XIAO nRF54L15 (`xiao_nrf54l15/nrf54l15/cpuapp`), nRF Connect SDK v3.3.1 + Zephyr, SoftDevice Controller.

---

## 1. Boot & init sequence

`main()` is Zephyr's main thread (`src/main.c:24`). Everything before it is Zephyr/NCS init (SoC, clocks, GPIO drivers, `SYS_INIT` hooks). One application `SYS_INIT` exists: `cs_setup_init()` at `APPLICATION` priority, which only does `k_sem_init` + `k_work_init` for Channel Sounding (`src/opendisplay_cs.c:173-180`).

Ordered trace from `main()`:

| # | Call | File:line | What it does | Failure behavior |
|---|------|-----------|--------------|------------------|
| 1 | `printf("OpenDisplay nRF54 starting")` | `src/main.c:29` | First console output (RTT by default, UART in `uart` profile) | n/a |
| 2 | `board_nrf54_early_init()` | `src/main.c:30` → `src/board_nrf54.c:28-35` (L15) / `:11-15` (LM20) | **L15**: drives RF-switch power P2.3 high, RF-switch select P2.5 low (onboard ceramic antenna), BS pin P2.10 low, then `k_msleep(10)`. **LM20**: drives nPM1300 SHPHLD (P1.13) high to hold power, `k_msleep(10)`. | Cannot fail (`nrf54_gpio_configure_output` silently returns on bad pin, `src/nrf54_gpio.c:43-54`) |
| 3 | `board_nrf54_prepare_epd_rail()` | `src/main.c:31` → `src/board_nrf54.c:37-41` | L15: BS pin low + `k_msleep(50)` settle. LM20: `k_msleep(50)` only. | Cannot fail |
| 4 | `opendisplay_ble_init()` | `src/main.c:32` → `src/opendisplay_ble.c:651` | The real init sequence — see below | See below |

### `opendisplay_ble_init()` internals (`src/opendisplay_ble.c:651-705`)

1. `initConfigStorage()` (`:655`) → `settings_subsys_init()` (`src/opendisplay_config_storage.c:31-34`). Return value ignored (cast to void); a failure here surfaces later as "no config".
2. Optional `FACTORY_CLEAR_CONFIG_ON_BOOT` build → `clearStoredConfig()` (`:656-660`).
3. `loadGlobalConfig(&s_od_global_config)` (`:661`) → `src/opendisplay_config_parser.c:673-696`: `initConfigStorage()` → `loadConfig()` (NVS/settings read, magic + CRC32 validated, `src/opendisplay_config_storage.c:64-98`) → `parseConfigBytes()`.
4. If no valid stored config: `tryProvisionFactoryEmbed()` (`:662`) → `src/factory_config.c:63-79`: validates the embedded packet's declared length and CRC-16/CCITT (`:34-48`), `saveConfig()`s it, then re-loads. Compiled out unless `FACTORY_HAS_EMBED` (set by CMake when `FACTORY_CONFIG_HEX` is given, `zephyr/CMakeLists.txt:81-83`).
5. **Failure path**: no config → `printf("[OD] config: defaults")` (`:670`) and `s_od_global_config` stays zeroed. The firmware still boots and advertises; display/LED/touch/buzzer/button all no-op because their init guards on `cfg->loaded` and their `*_count`.
6. `flash_powerdown_from_config()` (`:672`, impl `:585-604`) — bit-bangs SPI-NOR `0xB9` deep power-down for the first enabled `FlashConfig` (`src/board_nrf54.c:48-70`).
7. `opendisplay_sensor_bq27220_init()` (`:674`, impl `src/opendisplay_sensor_bq27220.c:94-126`) — configures charge enable/state GPIOs, probes gauge over bit-bang I2C. Failure just logs `BQ27220: not found`.
8. `opendisplay_sensor_sht40_init()` (`:675`, impl `src/opendisplay_sensor_sht40.c:160-186`) — soft-reset (`0x94`) at configured addr, falling back to 0x44/0x45. Failures ignored.
9. `opendisplay_led_init()` (`:677`, `src/opendisplay_led.c:316-336`) — `k_timer_init(&s_led_timer)` **always**, then drives each configured LED pin to its "off" level.
10. `opendisplay_buzzer_init()` (`:678`, `src/opendisplay_buzzer.c:273-302`) — `k_timer_init` for both step and tone timers, configures drive/enable pins.
11. `bt_enable(NULL)` (`:681`). **Only hard-failure gate**: on error it prints and **returns from `opendisplay_ble_init()` entirely** (`:682-685`) — no advertising, no button/touch init, no boot screen. `main()` then spins forever in the idle path with `cfg` loaded but no BLE.
12. `settings_load()` (`:686-688`) — loads BT bonds (`CONFIG_BT_SETTINGS=y`, `zephyr/prj_ncs.conf:5`).
13. `read_chip_temperature_once()` (`:689`, impl `:224-235`) — one-shot die-temp sample; deliberately after `bt_enable` because with MPSL the temp driver routes through `mpsl_temperature_get()` (comment `:219-223`). Failure leaves `s_chip_temperature = -999.0f` (`:217`).
14. `opendisplay_button_init()` (`:691`, `src/opendisplay_button.c:42-100`) — binds up to 8 buttons from `BinaryInputs` packets, configures pull-ups/downs, attaches **both-edges GPIO interrupts**. Interrupt-attach failure degrades to polling (`:91-94`). GT911 INT pins are skipped (`:71-74`).
15. `opendisplay_touch_init()` (`:692`, `src/opendisplay_touch.c:490-534`) — GT911 probe + hardware reset per touch controller. Failure logs and leaves that controller inactive.
16. `k_work_init_delayable(&s_adv_restart_work, adv_work_handler)` (`:693`) and `k_work_init(&s_boot_display_work, ...)` (`:694`).
17. `update_msd_payload()` (`:695`) — polls SHT40 + BQ27220 + battery, packs the 16-byte manufacturer-specific data (`:237-276`).
18. `start_advertising()` (`:696`, impl `:551-581`) — builds name `OD<chipid6>`, scan-response, applies interval, `bt_le_adv_stop()` then `bt_le_adv_start()`. On failure: logs and `schedule_adv_restart(0)` (`:697-700`). On success: `apply_tx_power(ADV, 0)` via HCI VS Write_Tx_Power_Level (`:500-535`).
19. `schedule_boot_display_apply()` (`:704`, impl `:161-170`) — **lazily creates a dedicated 8 KB work queue at priority 14** and submits the boot-screen work item. This is the only place the display work queue is started.

**Net result of boot:** BLE advertising up, boot screen being painted asynchronously on the display work queue, `main()` entering the superloop.

---

## 2. The main loop / concurrency & control-flow model

### 2.1 Shape of the model

It is a **cooperative superloop on the Zephyr main thread**, with a small number of deferred/asynchronous helpers:

- **Superloop**: `main()` (`src/main.c:34-59`). All command dispatch, display I/O, sensor polling, LED/buzzer/button/touch state machines execute here.
- **Message queue**: BLE GATT writes are *not* processed in the BLE callback; they are copied into a `k_msgq` and drained on main (`src/opendisplay_pipe.c:82`, `:1376-1390`, `:1407-1425`).
- **Work queues**: system workqueue for advertising restart/MSD publish; one dedicated workqueue for the boot screen; system workqueue for CS setup.
- **k_timers**: LED delay, buzzer step, buzzer square-wave toggle. Timer callbacks only set flags (except the buzzer tone timer, which toggles GPIO directly).
- **ISRs**: GPIO both-edge interrupts for buttons (flag-only).
- **No RTOS threads are created by the app** other than the display work queue thread. There is **no idle/sleep power-management path** (`CONFIG_PM` is absent from all `prj*.conf`); "sleep" is just Zephyr tickless idle between `k_msleep()` calls.

### 2.2 Complete inventory of concurrency objects

| Object | Definition | Trigger | Context / priority | Work done | Hands off to |
|---|---|---|---|---|---|
| **main thread** | `src/main.c:24` | reset | Zephyr `main`, prio 0 (default; `CONFIG_MAIN_STACK_SIZE=4096`, `zephyr/prj.conf:22`) | superloop: `opendisplay_ble_process()`, MSD refresh, `k_msleep` | everything |
| **`s_pipe_msgq`** (`K_MSGQ_DEFINE(s_pipe_msgq, sizeof(struct od_pipe_msg), 8, 4)`) | `src/opendisplay_pipe.c:82` | GATT write | produced on BT RX thread, consumed on main | 8 slots × 512-byte msgs (`OD_PIPE_MSG_DATA_MAX = 509`, `:75`) | `on_pipe_write()` on main |
| **`s_conn_gen` / `s_close_pending`** (`atomic_t`) | `src/opendisplay_pipe.c:83-84` | disconnect | written on BT RX (`:1399-1404`), read/CAS on main (`:1411`) | generation-stamps queued frames so stale frames from a closed link are dropped (`:1420-1422`) | deferred cleanup on main |
| **`s_adv_restart_work`** (`k_work_delayable`) | `src/opendisplay_ble.c:144`, init `:693` | `schedule_adv_restart()` (`:191`), `schedule_msd_publish()` (`:198`), `recycled()` (`:420`), `connected()` err path (`:381`), `disconnected()` (`:413`), config reload (`:616`), adv boost (`:628`), 500 ms fallback in `opendisplay_ble_process()` (`:719-722`) | **system workqueue** (Zephyr default prio −1, cooperative; stack 4096 from `zephyr/prj.conf:23`) | `adv_work_handler` (`:172-189`): either publish new MSD into adv data, or `start_advertising()`; on error reschedules itself at +200 ms | BT host |
| **`s_boot_display_work`** (`k_work`) | `src/opendisplay_ble.c:145`, init `:694`, submitted `:704` | end of `opendisplay_ble_init()` | **`s_display_work_q`**, own 8 KB stack, **priority 14** (`:146-147`, `:164-167`) | `opendisplay_display_boot_apply()` (`src/opendisplay_display.cpp:677-708`): power panel, init, render boot screen + QR, full refresh (blocking up to 60 s), deep-sleep panel, power off | nothing |
| **`s_cs_setup_work`** (`k_work`) | `src/opendisplay_cs.c:61`, init `:176` | `opendisplay_cs_on_connected()` (`:192`) from the BLE `connected` callback | **system workqueue** | `cs_setup_work_handler` (`:124-171`): `bt_le_cs_set_default_settings()` (reflector-only), **blocks on `k_sem_take(&s_cs_config_sem, K_SECONDS(10))`**, then `bt_le_cs_set_procedure_parameters()` | SDC controller |
| **`s_cs_config_sem`** (`k_sem`, max 1) | `src/opendisplay_cs.c:62`, init `:175` | given by `cs_config_create_cb` (`:84`) from BT callback context | — | gates CS procedure-param write on config-complete | `cs_setup_work_handler` |
| **`s_led_timer`** (`k_timer`) | `src/opendisplay_led.c:48`, init `:320` | `led_schedule_delay_ms()` (`:129-140`) | **timer ISR / sysclock** | callback sets `s_timer_due = true` **only** (`:116-120`) | `led_run_step()` on main via `opendisplay_led_process()` |
| **`s_step_timer`** (`k_timer`) | `src/opendisplay_buzzer.c:70`, init `:277` | `buzzer_schedule_wait_ms()` (`:161-170`) | timer ISR | sets `s_step_due = true` (`:250-254`) | `buzzer_run_step()` on main |
| **`s_tone_timer`** (`k_timer`) | `src/opendisplay_buzzer.c:71`, init `:278` | `buzzer_start_tone()` (`:143`) | **timer ISR — does real work**: toggles the drive GPIO and re-arms itself (`:256-271`) | generates the square wave (400 Hz–12 kHz, so this ISR can fire up to 24 k/s) | none |
| **GPIO button ISR** | `src/nrf54_gpio.c:85-133` (8 slots, `GPIO_INT_EDGE_BOTH`) | button edge | GPIO ISR | trampoline → `button_irq_handler()` sets `s_button_irq_pending = true` (`src/opendisplay_button.c:31-34`) | `opendisplay_button_process()` on main (which actually re-polls the level, `:102-137`) |
| **BLE `connected`** | `src/opendisplay_ble.c:377-400` (`BT_CONN_CB_DEFINE`, `:424`) | link up | BT RX thread | cancels adv-restart work, `bt_conn_ref`, clears reboot flag, submits CS work, applies conn TX power via `bt_hci_cmd_send_sync` | CS workqueue |
| **BLE `disconnected`** | `:402-414` | link down | BT RX thread | `opendisplay_cs_on_disconnected()`, `opendisplay_pipe_on_connection_closed()` (flag only), unref conn, `schedule_adv_restart(150)` | main (deferred cleanup) + sysWQ |
| **BLE `recycled`** | `:416-422` | conn object freed | ISR-like | only `k_work_schedule(&s_adv_restart_work, K_NO_WAIT)` (explicitly documented as ISR-safe, `:418`) | sysWQ |
| **GATT write cb `od_gatt_write`** | `:287-298` | client write / write-cmd on `0x2446` | BT RX thread | `memcpy` into msgq via `opendisplay_pipe_on_write()`; **no processing** | main |
| **CCC changed `od_ccc_cfg_changed`** | `:300-305` | client subscribes | BT RX thread | sets `s_notify_enabled`, `s_notify` | — |
| **CS callbacks** (`le_cs_read_remote_capabilities_complete`, `le_cs_config_complete`, `le_cs_security_enable_complete`, `le_cs_procedure_enable_complete`) | `src/opendisplay_cs.c:117-122` | controller events | BT context | log; `cs_config_create_cb` gives the semaphore | CS workqueue |

### 2.3 Steady-state behavior

**Idle (no connection)** — `src/main.c:45-58`:
- If a config with `power_option.sleep_timeout_ms > 0` is loaded: `idle_delay_ms(sleep_timeout_ms)` (`:50`) → loops in 100 ms chunks, calling `opendisplay_ble_process()` then `k_msleep(100)` (`src/main.c:10-22`). At the end of each full timeout, `opendisplay_ble_update_msd(true)` (`:51`) re-reads sensors and republishes the advertisement.
- Otherwise: `idle_delay_ms(500)` (`:53`) — housekeeping every 100 ms, no periodic MSD refresh.
- Every 10 idle cycles: an "alive uptime" printf (`:56-58`).

**`opendisplay_ble_process()`** (`src/opendisplay_ble.c:707-723`) is the housekeeping tick, run every ~100 ms:
1. `opendisplay_pipe_process()` — deferred disconnect cleanup + drain the write msgq.
2. `opendisplay_led_process()` — advance LED state machine.
3. `opendisplay_buzzer_process()` — advance buzzer state machine.
4. `opendisplay_button_process()` — poll button levels, detect edges.
5. `opendisplay_touch_process()` — GT911 poll, rate-limited to ≥100 ms (`src/opendisplay_touch.c:586-589`).
6. `opendisplay_ble_advertising_tick()` — ends the 3 s advertising boost and restores the slow interval (`:632-649`).
7. Fallback: if disconnected and not advertising and ≥500 ms since last try → `schedule_adv_restart(0)` (`:719-722`).

**Connected** — `src/main.c:37-44`: `opendisplay_ble_process()` then `k_msleep(10)` — a 10 ms tick, so pipe commands are drained ~100×/s.

**What wakes the system:** nothing wakes it in a PM sense — the CPU is simply in Zephyr idle between `k_msleep()` expiries. Real event sources are (a) the 10/100 ms tick, (b) BT RX thread activity from the controller, (c) GPIO button IRQ (which only sets a flag; the main loop still discovers it on its next tick), (d) `k_timer` expiries.

**Low-power path:** essentially none at the app level. `board_nrf54_flash_powerdown()` puts external SPI NOR to sleep (`src/board_nrf54.c:48`); `s_epd.sleep(DEEP_SLEEP)` + `display_power_set(false)` park the panel after every refresh (`src/opendisplay_display.cpp:957-961`, `:706-707`, `:433-435`), and `opendisplay_display_park_pins()` disconnects display GPIOs (`:89-102`, `src/nrf54_gpio.c:157-166`). `CMD_DEEP_SLEEP` (0x0052) is a **stub**: `opendisplay_ble_schedule_deep_sleep()` only prints "deep sleep not implemented on nRF54 yet" (`src/opendisplay_ble.c:730-733`).

### 2.4 Blocking calls, timeouts, deferral

| Where | Blocking call | Timeout | Context |
|---|---|---|---|
| `src/main.c:19,42` | `k_msleep(100/10)` | — | main |
| `src/opendisplay_pipe.c:1387` | `k_msgq_put(..., K_MSEC(100))` | 100 ms; on expiry the frame is **dropped** with a printf (`:1388`) | BT RX thread |
| `src/opendisplay_pipe.c:542-550` | notify retry loop, up to 200 × `k_msleep(1)` | ~200 ms | main |
| `src/opendisplay_pipe.c:799` | `k_msleep(20)` between the 0x72 ack and the refresh | — | main |
| `src/opendisplay_pipe.c:1237-1239` | busy-wait 800 000 iterations before `NVIC_SystemReset()` | — | main |
| `src/opendisplay_display.cpp:127-142` | `wait_for_refresh()` — polls `isBusy()` every 50 ms | **60 000 ms** (`:388`, `:703-704`, `:955`) | main (or display WQ for boot) |
| `third_party/bb_epaper/src/nrf54_bbep_busy.inl:20-50` | `bbepWaitBusy()` — 20 ms poll | 5 000 ms, 30 000 ms for 3/4/7-color | main / display WQ |
| `src/opendisplay_ble.c:510` | `bt_hci_cmd_alloc(K_FOREVER)` | **unbounded** | BT RX thread (from `connected`) and main (config reload) |
| `src/opendisplay_ble.c:520` | `bt_hci_cmd_send_sync()` | host default | same |
| `src/opendisplay_cs.c:146` | `k_sem_take(&s_cs_config_sem, K_SECONDS(10))` | 10 s | system workqueue |
| `src/opendisplay_touch.c:91-98`, `src/opendisplay_i2c.c:35-48` | I²C clock-stretch spin | 1000 µs bounded | main |
| `src/opendisplay_led.c:91-111` | `k_busy_wait(100 µs)` × 7 per brightness step, up to 16 steps | ≈11 ms per flash, **inline on main** | main |

**Deferred vs inline:**
- Deferred: GATT writes (BT RX → msgq → main, `src/opendisplay_pipe.c:70-74` explains why: EPD/CCM work on the BT RX thread starves ATT and breaks service discovery on reconnect); disconnect cleanup (`:1398-1404` → `:1411-1418`); advertising restart/MSD publish (sysWQ); boot screen (dedicated WQ); CS setup (sysWQ).
- Inline on main: **all** command handling, including AES-CCM crypto, NVS config writes, full/partial EPD refresh (seconds), config reads that emit up to 44 notifications back-to-back (`src/opendisplay_pipe.c:832`), and the LED software PWM.

### 2.5 Re-entrancy / shared-state / race hazards

1. **`s_conn` use-after-unref (main vs BT RX).** `opendisplay_ble_pipe_notify()` checks `s_conn == NULL` then calls `bt_gatt_notify(s_conn, ...)` on the main thread (`src/opendisplay_ble.c:455-461`), while `disconnected()` does `bt_conn_unref(s_conn); s_conn = NULL;` on the BT RX thread (`:408-411`). No lock. A disconnect landing between the check and the notify passes a just-unreffed pointer.
2. **`msd_payload` torn read.** Written byte-wise by `update_msd_payload()` on main (`:268-275`) while `adv_work_handler` → `publish_msd_to_advertising()` → `bt_le_adv_update_data(ad, ...)` reads it from the system workqueue (`:341-362`; `ad[]` points directly at `msd_payload`, `:317`). No lock.
3. **`s_od_global_config` mutated under readers.** `opendisplay_ble_reload_config_from_nvm()` runs `loadGlobalConfig()` (which `memset`s and re-parses the whole struct, `src/opendisplay_config_parser.c:679`) on the main thread (`src/opendisplay_ble.c:606-617`), while the system workqueue's `sd_prepare()`/`opendisplay_cs_config_enabled()` read it (`:333`, `src/opendisplay_cs.c:25-28`). No lock.
4. **Display state (`s_epd`, `s_active`, `s_partial`) is shared between main and the boot-display workqueue.** `opendisplay_display_boot_apply()` runs on `s_display_work_q` (prio 14) and can take tens of seconds (`src/opendisplay_display.cpp:677-708`); it is submitted at the *end of init* (`src/opendisplay_ble.c:704`) so a client that connects immediately and sends `0x0070` will run `opendisplay_display_direct_write_start()` on main (prio 0, which preempts prio 14) against the *same* `s_epd` object and the same power rail. `s_boot_applied` (`:681-684`) only guards double-boot-apply, not this interleaving. No mutex anywhere in the display module.
5. **`GlobalConfig` written through a const-cast.** `opendisplay_led_activate()` casts away `const` and writes `led->reserved` in the global config (`src/opendisplay_led.c:340-348`, and clears it at `:372`), so an LED command mutates the parsed config in place.
6. **Buzzer tone ISR vs main GPIO writes.** `buzzer_tone_timer_cb` writes `s_bz.drive_pin` from ISR context (`src/opendisplay_buzzer.c:256-271`) while `buzzer_stop_tone()`/`buzzer_finish()` write the same pin from main (`:97-105`, `:146-159`). `s_bz.tone_running` is a plain `bool`, not atomic.
7. **`s_notify` / `s_notify_enabled`** written on BT RX (`src/opendisplay_ble.c:303-304`, `src/opendisplay_pipe.c:1392-1396`), read on main (`:529`, `:546`) — plain `bool`.
8. **Explicit locking that does exist:** only the msgq + the two `atomic_t`s in the pipe (`src/opendisplay_pipe.c:82-84`) and the CS semaphore (`src/opendisplay_cs.c:62`). **There is not a single `k_mutex` in the tree** (grep: none).
9. **Queue-overflow data loss.** During a 60 s blocking EPD refresh on main, the BT RX thread keeps enqueuing; the 8-deep msgq fills and further frames are dropped after a 100 ms `k_msgq_put` timeout (`src/opendisplay_pipe.c:1387-1389`). Mitigated in practice by the request/ack protocol.
10. **`s_long_write_buf` / `s_long_write_len` / `s_long_write_conn`** (`src/opendisplay_pipe.c:51-53`) are written only in the reset path (`:1414-1415`) — dead state (see §9).

### 2.6 ASCII runtime diagram

```
 RESET
   │
   ├─ Zephyr init (SoC, GPIO, settings/NVS, SYS_INIT: cs_setup_init)
   │
   ▼
 main() ──► board_early_init ──► prepare_epd_rail ──► opendisplay_ble_init()
                                                          │
   ┌──────────────────────────────────────────────────────┘
   │ settings ▸ loadGlobalConfig ▸ [factory embed?] ▸ flash powerdown
   │ ▸ bq27220/sht40 init ▸ led/buzzer init ▸ bt_enable ▸ settings_load
   │ ▸ chip temp ▸ button/touch init ▸ work init ▸ MSD ▸ start_advertising
   │ ▸ submit boot-screen work ───────────────────► [display WQ prio14]
   │                                                 boot_apply(): power,
   │                                                 init, QR render,
   │                                                 REFRESH_FULL (≤60 s),
   │                                                 sleep, power off
   ▼
 ┌───────────────── SUPERLOOP (main, prio 0) ──────────────────┐
 │  connected?                                                  │
 │   ├─yes─► ble_process(); k_msleep(10)                        │
 │   └─no ──► idle_delay_ms(sleep_timeout_ms | 500)             │
 │             └─ per 100 ms: ble_process(); k_msleep(100)      │
 │             └─ at end (if sleep_timeout>0): update_msd(true) │
 └──────────────────────────────────────────────────────────────┘
        │
        └─ ble_process() =
             pipe_process ▸ led_process ▸ buzzer_process
             ▸ button_process ▸ touch_process ▸ adv_tick ▸ adv fallback
```

Event flow for a GATT write (the hot path):

```
 phone                BT RX thread                main thread              panel
   │  ATT Write          │                            │                      │
   ├────────────────────►│ od_gatt_write()            │                      │
   │                     │  └► pipe_on_write()        │                      │
   │                     │      k_msgq_put(100ms) ───►│ (queued, gen-stamped)│
   │                     │                            │                      │
   │                     │        ...≤10ms later...   │ pipe_process()       │
   │                     │                            │  k_msgq_get(NO_WAIT) │
   │                     │                            │  on_pipe_write()     │
   │                     │                            │   ├ sec? CCM decrypt │
   │                     │                            │   └ dispatch()       │
   │                     │                            │      0x0070 → EPD init
   │                     │                            │      0x0071 → uzlib→writeData ──►│
   │                     │                            │      0x0072 → ack, then
   │                     │                            │              refresh + busy-poll
   │◄────── notify ──────┤◄── bt_gatt_notify() ───────┤              (≤60 s BLOCKS main)
   │        0x73/0x74    │   (retry ≤200×1ms)         │                      │
```

Advertising state machine:

```
        ┌──────────── s_adv_active=false ────────────┐
        │                                            │
  disconnect / adv-fail / config-reload / boost      │
        │                                            │
        ▼                                            │
  schedule_adv_restart(delay) ──► [sysWQ] adv_work_handler
        │                              ├ s_conn != NULL → drop
        │                              ├ msd_publish flag → bt_le_adv_update_data
        │                              └ else start_advertising()
        │                                    ok → s_adv_active=true
        │                                    err → reschedule +200 ms
        └── main-loop fallback: !adv_active && 500 ms elapsed → reschedule
```

---

## 3. Module inventory (`src/`)

**`main.c` (61 lines).** Superloop only. `idle_delay_ms()` chunks a long idle into 100 ms slices so housekeeping keeps running (`:10-22`). Chooses between a connected 10 ms tick and an idle tick sized by `power_option.sleep_timeout_ms` (`:37-54`). Deps: `board_nrf54.h`, `opendisplay_ble.h`, `opendisplay_config_parser.h`, `opendisplay_display.h`.

**`board_nrf54.c` (70 lines).** Board-specific power/RF bring-up, compile-time split on `NRF54_BOARD_LM20` (`:6`) vs L15 (`:22`). Public: `board_nrf54_early_init()`, `board_nrf54_prepare_epd_rail()`, `board_nrf54_flash_powerdown()` (bit-bangs SPI-NOR `0xB9`, `:48-70`). Deps: `nrf54_gpio.h`, Zephyr kernel.

**`nrf54_gpio.c` (171 lines).** The single GPIO abstraction. Decodes OpenDisplay's compact `(port<<4)|pin` byte (`:25-41`; port ≤3, pin ≤15, `0xFF` = unused). Public: `nrf54_pin_decode`, `nrf54_gpio_configure_output/_input`, `nrf54_gpio_configure_interrupt` (8 slots, `GPIO_INT_EDGE_BOTH`, `:98-133`), `nrf54_gpio_write/read`, `nrf54_gpio_park` (`GPIO_DISCONNECTED`), `od_hwinfo_get_device_id`. All errors are silent no-ops.

**`nrf54_zephyr_time.c` (18 lines) + `nrf54_zephyr_compat.h`.** `od_msleep` / `od_uptime_get_32` / `od_busy_wait` shims so C++/vendored code doesn't include `kernel.h`.

**`newlib_stubs.c` (5 lines).** Empty `_fini()` — newlib link stub.

**`opendisplay_ble.c` (756 lines).** BLE lifecycle, GATT service, advertising, MSD, TX power, config reload, boot-display scheduling, and the process tick. Public API in `opendisplay_ble.h`. Also owns `s_od_global_config` — `opendisplay_get_global_config()` (`:430-433`) is the accessor every other module uses. Deps: nearly everything (config parser/storage, cs, display, button, led, touch, buzzer, pipe, factory_config, battery, sensors, board).

**`opendisplay_pipe.c` (1425 lines).** The OpenDisplay pipe protocol: framing, encryption session (AES-CMAC challenge/response + AES-CCM with 13-byte nonce/12-byte tag hand-rolled over PSA AES-ECB, `:286-405`), 64-entry replay window (`:407-438`), chunked config write, chunked config read, NFC endpoint, LED/buzzer/reboot/DFU/deep-sleep, direct- and partial-write dispatch. Entry points: `opendisplay_pipe_on_write` (BT RX), `opendisplay_pipe_on_notify_changed`, `opendisplay_pipe_on_connection_closed`, `opendisplay_pipe_process` (main).

**`opendisplay_display.cpp` (982 lines).** The display pipeline: boot screen apply, direct-write (0x0070/71/72), partial-write (0x0076) with ETag gating, plane splitting, streaming zlib, per-panel RAM-window quirks, refresh + busy wait, panel deep-sleep and rail power-down. Deps: `bb_epaper`, `uzlib`, `opendisplay_epd_map`, `opendisplay_display_color`, `boot_screen`, `nrf54_gpio`, `opendisplay_touch` (for post-refresh GT911 recovery, `:936`, `:980`).

**`boot_screen.cpp` (1039 lines) + `logo_bitmap.h` + `qr/qrcode.c`.** Renders the boot screen (manufacturer/model header, domain/name/firmware/key lines, QR code, color swatches, logo) directly into panel RAM row-by-row via `bbep_write_data`, with a three-zone layout on ≥400×300 panels (`:625`), rotation mapping (`:975-981`), 4-gray plane de-interleaving (`:569`, `:646-652`), and BWR/BWY second-plane pass (`:1020-1027`). Row buffer is a fixed 680 B (`:28`); gray4 scratch is `(2720+7)/8` (`:29`).

**`opendisplay_config_parser.c` (696 lines).** TLV parser for the config blob: 2-byte length + version byte, then `[packetNum][packetId][data...]`, trailing CRC-16 (`:142-671`). Knows the on-wire size of every packet (`:120-140`) so unknown-but-sized packets are skipped rather than aborting the parse. Contains a security-packet rescan fallback (`:46-65`). Public: `parseConfigBytes`, `loadGlobalConfig`, `od_get_parsed_security`, `od_security_key_set`.

**`opendisplay_config_storage.c` (106 lines).** Zephyr `settings`/NVS persistence of the raw config blob under key `od/config` (`:7`), wrapped in `{magic 0xDEADBEEF, version, crc32, data_len, data[4096]}` (`opendisplay_config_storage.h:20-26`), with a static RAM cache. Public: `initConfigStorage`, `saveConfig`, `loadConfig`, `clearStoredConfig`, `calculateConfigCRC`.

**`factory_config.c` (79 lines).** Optional build-time embedded config: validates the packet (declared length + CRC-16/CCITT-FALSE with the two length bytes zeroed, `:15-48`) and `saveConfig()`s it when nothing valid is stored (`:63-79`). Entire body compiles out without `FACTORY_HAS_EMBED`.

**`opendisplay_epd_map.c` (94 lines).** Pure mapping from OpenDisplay `panel_ic_type` (0x0000–0x004C) to `bb_epaper` panel enums (`:4-94`). 0x0047/0x0048/0x004A/0x004C → `EP_PANEL_UNDEFINED` (not vendored); 0x0049 and 0x004B are documented substitutions.

**`opendisplay_display_color.c` (73 lines).** Color-scheme math: `bits_per_pixel` (`:27-39`), `is_bitplanes` (schemes 1/2/5, `:41-46`), `start_plane` (`:48-61`), `bitplane_plane_bytes` and `direct_write_total_bytes` — all **row-padded** (`ceil(w*bpp/8) * h`, `:63-73`).

**`opendisplay_i2c.c` (219 lines) + `opendisplay_sensor_common.h`.** Bit-banged open-drain I²C master over `nrf54_gpio` (a line is "released" by reconfiguring it as pull-up input). Bounded 1000 µs clock-stretch timeout (`:7`, `:35-48`). `od_sensor_bus_for()` resolves a `DataBus` (0x24) instance to a bus (`opendisplay_sensor_common.h:15-39`).

**`opendisplay_sensor_sht40.c` (233 lines).** SHT40 temp/RH over the bit-bang bus: `0xFD` high-precision measure, 12 ms wait, CRC-8 checked (`:51-83`); tries the configured address then 0x44/0x45 (`:93-111`). Packs a 3-byte MSD block (`rh_deci | temp<<10`, `:124-148`), 30 s TTL (`:12`, `:201`).

**`opendisplay_sensor_bq27220.c` (183 lines).** BQ27220 fuel gauge: voltage (`0x08`) and SOC (`0x2C`) via write-no-stop + read (`:49-63`); charger enable/state GPIOs from `PowerOption` (`:101-112`); packs SOC + charging bit into one MSD byte (`:169-182`), 30 s TTL.

**`opendisplay_battery.c` (211 lines).** SAADC battery read. Maps `battery_sense_pin` → AIN index (only P1.00–P1.07 are analog, `:55-67`), reads 10 samples, converts pin mV back into the reference's 10-bit/3.6 V count space so existing `voltage_scaling_factor` calibrations still work (`:22-31`, `:159-165`). Source priority: BQ27220 first, else SAADC (`:170-179`). 30 s cache (`:181-196`). `opendisplay_battery_get_10mv()` clamps to 9 bits (`:198-211`).

**`opendisplay_button.c` (137 lines).** Up to 8 buttons from `BinaryInputs` (0x25). Both-edge IRQ sets a flag; the actual edge detection is a level poll in `opendisplay_button_process()` (`:102-137`). On change: packs `id | press_count<<3 | state<<7` into a dynamic MSD byte, updates MSD, and boosts advertising (`:129-133`).

**`opendisplay_touch.c` (703 lines).** GT911 driver with its **own** bit-bang I²C implementation (separate from `opendisplay_i2c.c`, `:70-174`), 16-bit register access with LE/BE auto-detect (`:266-279`), address auto-resolve 0x5D/0x14 via INT-during-reset (`:333-401`), 5-byte MSD block encoding (`:675-694`), disable-after-5-I²C-failures (`:39`, `:616-620`), and `opendisplay_touch_resume_after_refresh()` (light PID probe, else full reinit) called after every EPD refresh (`:552-575`).

**`opendisplay_led.c` (389 lines).** Non-blocking, phase-based LED sequencer (11 phases, `:18-30`) replicating the reference's 3-color/3-loop/group-repeat pattern; brightness via 7-slice software PWM with `k_busy_wait(100 µs)` (`:73-114`). Delays use `s_led_timer`; the state machine advances from `opendisplay_led_process()`.

**`opendisplay_buzzer.c` (400 lines).** Passive-buzzer pattern player. Validates the whole `[outer][pattern_count]([nsteps][fidx,dunit]…)` payload before playing (`:335-355`), then plays non-blockingly: `s_step_timer` for step/gap durations, `s_tone_timer` for the square wave. Frequency index 1–255 → 400 Hz–12 kHz linear (`:74-82`); 5 s total cap (`:36`, `:182-184`).

**`opendisplay_cs.c` (210 lines).** Channel Sounding reflector. All CS code is `#if defined(CONFIG_BT_CHANNEL_SOUNDING)`. Runtime-gated on `device_flags` bit 5 (`:19-30`). Adds the RAS UUID to the scan response (`:37-56`), and on connect kicks a workqueue that sets reflector-only default settings and procedure parameters (`:124-171`).

**`qr/qrcode.c`** — vendored QR generator used only by the boot screen.

---

## 4. BLE stack usage

**Stack config** (`zephyr/prj.conf:36-57`, `zephyr/prj_ncs.conf`): `CONFIG_BT_PERIPHERAL`, `CONFIG_BT_MAX_CONN=1`, `CONFIG_BT_HCI_VS=y` (for TX power), ATT MTU 512 (`BT_L2CAP_TX_MTU=512`, ACL RX/TX 512), BT RX stack 8192, and TX pools raised to 12 (`BT_L2CAP_TX_BUF_COUNT`, `BT_ATT_TX_COUNT`, `BT_CONN_TX_MAX`) so a multi-chunk config read doesn't stall. NCS overlay adds `BT_SMP=y`, `BT_BONDABLE=y`, `BT_SETTINGS=y`, `BT_CTLR_DATA_LENGTH_MAX=251`, `BT_CTLR_PHY_2M=y`, `BT_AUTO_PHY_UPDATE=n`, `BT_GAP_AUTO_UPDATE_CONN_PARAMS=n`.

**Service & characteristic** (`src/opendisplay_ble.c:307-313`):
- Primary service UUID **`0x2446`** (16-bit).
- One characteristic, UUID **`0x2446`**, properties `WRITE | WRITE_WITHOUT_RESP | NOTIFY`, permission `BT_GATT_PERM_WRITE` (**no encryption/authentication permission required** — link-layer security is not enforced; the app-layer session encryption is the gate).
- A CCC descriptor (`BT_GATT_CCC`, perms READ|WRITE). Notifications target `od_svc.attrs[2]` (`:460`).

**Advertising** (`:315-321`, `:551-581`):
- AD: Flags (`GENERAL | NO_BREDR`) + 16-byte Manufacturer Specific Data.
- Scan response: complete local name `OD<6-hex-of-chip-id>` (`:560-561`, name buffer 16 B), 16-bit UUID list `0x2446`, and — when CS is enabled — the RAS service UUID (`src/opendisplay_cs.c:42-50`).
- Params: `BT_LE_ADV_OPT_CONN`, interval 160 ms–1000 ms (`OD_ADV_INTERVAL_MIN=256`, `MAX=BT_GAP_ADV_SLOW_INT_MIN`, `:119-120`). A 3 s "boost" drops it to 20–30 ms after button/touch events (`:121-123`, `:624-630`).
- `start_advertising()` always `bt_le_adv_stop()` first (`:567`).

**MSD payload (16 bytes)** (`:237-276`):
```
[0..1]  company ID 0x2446 (LE)
[2..12] dynamic_return[11]  ← sensors/buttons/touch write here via
                              opendisplay_ble_set_dynamic_byte()
[13]    temperature  = clamp((chip_temp + 40) * 2, 0..255)
[14]    battery low byte (10 mV units)
[15]    status: bit0 battery high bit | bit1 rebootFlag | bit2 connectionRequested
                | bits4-7 loop counter
```
MSD is only pushed into the advertisement when it actually changed (`:346-348`, `:371-374`).

**TX power** (`:500-535`): HCI VS `Write_Tx_Power_Level` with `power_option.tx_power` (dBm, int8), applied for the ADV handle at init (`:701`), on every config reload (`:613`), and for the CONN handle on connect (`:391-399`). The controller-selected value is logged.

**Connection params:** not set by the app; auto-update is disabled (`prj_ncs.conf:7`). No `bt_le_conn_param_update` call exists.

**Pairing / bonding:** `BT_SMP` + `BT_BONDABLE` + `BT_SETTINGS` are enabled, and `settings_load()` restores bonds (`:686-688`), but **no `bt_conn_auth_cb` / `bt_conn_auth_info_cb` is registered anywhere** — pairing is Just-Works with the host defaults, and no characteristic requires an encrypted link. Bonding exists to satisfy Android's CS/ranging requirement (README: "Pair and bond with the phone when prompted").

### The OpenDisplay pipe protocol

Frame (client → device): `[cmd_hi][cmd_lo][payload…]`, max 244 B (`OD_PIPE_MAX_PAYLOAD`, `src/opendisplay_protocol.h:57`); frames >244 B get an immediate `{0xFF, cmd_lo, 0xFE}` error (`src/opendisplay_pipe.c:1333-1337`). Responses (device → client, via notify): `[status][resp_code][data…]`, status `0x00` = ok, `0xFF` = error.

Commands (`src/opendisplay_protocol.h:6-31`):

| Cmd | Name | Handler |
|---|---|---|
| `0x000F` | REBOOT | busy-wait then `NVIC_SystemReset()` (`pipe.c:1235-1240`) |
| `0x0040` | CONFIG_READ | `handle_config_read` — chunked, ≤100 B/response, up to `(MAX_CONFIG_SIZE+93)/94 = 44` chunks (`:822-887`) |
| `0x0041` | CONFIG_WRITE | `handle_config_write` — single-shot (≤200 B) or start of a chunked write with a 2-byte total-size prefix (`:889-965`) |
| `0x0042` | CONFIG_CHUNK | `handle_config_chunk` — continuation, ≤200 B, ≤20 chunks (`:967-1021`) |
| `0x0043` | FIRMWARE_VERSION | major/minor + SHA string; **always plaintext-readable** (`:587-608`, `:571-572`) |
| `0x0044` | READ_MSD | returns the 16 MSD bytes (`:610-618`) |
| `0x0045` | CONFIG_CLEAR | `clearStoredConfig()` + reload (`:809-820`) |
| `0x0050` | AUTHENTICATE | challenge/response, see below (`:620-725`) |
| `0x0051` | ENTER_DFU | acks `0x00 0x51`, then `opendisplay_ble_schedule_dfu()` which **only prints "not implemented"** (`:1241-1247`, `ble.c:725-728`) |
| `0x0052` | DEEP_SLEEP | **no response sent by design** (`:1248-1254`); handler is a stub (`ble.c:730-733`) |
| `0x0070/71/72` | DIRECT_WRITE START/DATA/END | display pipeline (§5) |
| `0x0073` | LED_ACTIVATE | `opendisplay_led_activate` (`:1255-1271`) |
| `0x0075` | LED_STOP | `opendisplay_led_stop` (`:1272-1288`) |
| `0x0076` | PARTIAL_WRITE_START | `opendisplay_display_partial_write_start` (`:1313-1315`) |
| `0x0077` | BUZZER | `opendisplay_buzzer_activate` (`:1289-1300`) |
| `0x0082` | NFC_ENDPOINT | sub-commands 0x00 read / 0x01 write / 0x10-0x12 chunked write — but the backend is a stub (§9) (`:1023-1176`) |

**Security / session** (all in `opendisplay_pipe.c`):
- Enabled when `SecurityConfig.encryption_enabled != 0` (`:143-147`).
- **Auth**: client sends `0x0050` with payload `{0x00}` → device replies `{status, 0x50, AUTH_STATUS_CHALLENGE, server_nonce[16], device_id[4]}` (`:664-677`). Client then sends 32 bytes `{client_nonce[16], cmac[16]}`; the device recomputes `AES-CMAC(master_key, server_nonce || client_nonce || device_id)` and compares (`:681-691`). On success it derives the session key (`CMAC("OpenDisplay session"‖0‖device_id‖cnonce‖snonce‖0‖0x80)` then AES-ECB, `:218-249`) and an 8-byte session ID (`CMAC(session_key, cnonce‖snonce)[0:8]`, `:251-265`), and returns a server proof. Rate limit: 10 attempts/60 s (`:639-648`); challenge valid 30 s (`:678`).
- **Transport**: for frames ≥31 B, payload is `nonce[16] ‖ ciphertext ‖ tag[12]`; the CCM nonce is `nonce[3..15]` (13 B), AAD is the 2-byte command, tag 12 B, L=2 (`:440-483`, B_0 flags `0x69` documented at `:281-285`). Plaintext is length-prefixed (`:473-479`).
- **Replay**: nonce must carry the session ID in bytes 0-7; counter must be within ±32 of `last_seen_counter` and not in the 64-entry window (`:407-438`).
- **Integrity failures**: 3 strikes (replay or tag failure) clear the session (`:135-141`).
- **Session lifetime**: absolute, from `session_start_ms`, `SecurityConfig.session_timeout_seconds` (`:149-172`).
- **Unauthenticated policy** (`:1187-1215`): with security on and no live session, only `CMD_FIRMWARE_VERSION` passes; `CONFIG_WRITE`/`CONFIG_CHUNK` pass **only** if `SecurityConfig.flags` bit 0 (rewrite-allowed) is set, and only after `clearStoredConfig()` erases the old key. With a *live* session, sub-31-byte (i.e. unencrypted) frames other than firmware-version are rejected (`:1367-1370`).
- Responses are encrypted when a session is alive, except `RESP_AUTH_REQUIRED`, status `0xFF`, `RESP_AUTHENTICATE`, `RESP_FIRMWARE_VERSION` (`:571-576`).
- Crypto is PSA (`psa_crypto_init` lazily at first auth, `:114-122`); AES-CMAC and AES-ECB are PSA calls, CCM is hand-rolled on top of ECB (`:174-216`, `:286-405`). Kconfig: `CONFIG_MBEDTLS_PSA_CRYPTO_C`, `PSA_WANT_KEY_TYPE_AES`, `PSA_WANT_ALG_CMAC`, `PSA_WANT_ALG_ECB_NO_PADDING` (`zephyr/prj.conf:67-74`).

**Channel Sounding** (`src/opendisplay_cs.c`, `zephyr/prj_cs.conf`): compiled in unconditionally (CMake always appends `prj_cs.conf`, `zephyr/CMakeLists.txt:9`) but **runtime-gated** on `system_config.device_flags` bit 5 (`DEVICE_FLAG_CHANNEL_SOUNDING = 0x20`, `src/opendisplay_device_flags.h:11`). Config: `CONFIG_BT_CHANNEL_SOUNDING`, `CONFIG_BT_RAS` + `CONFIG_BT_RAS_RRSP` (Ranging Responder), reflector-only (`CONFIG_BT_CTLR_SDC_CS_ROLE_REFLECTOR_ONLY=y`), mode-3 off, 1 antenna. On connect: scan response advertises the RAS UUID, and the workqueue sets `enable_initiator_role=false / enable_reflector_role=true`, waits for the peer-driven config-complete, then writes procedure parameters (2M PHY, A1/B1 antenna config, `max_procedure_len=1000`, `min/max_subevent_len=10000/75000`) (`:133-170`).

---

## 5. Display pipeline

### Direct write (full frame): `0x0070 → 0x0071* → 0x0072`

1. **`0x0070` `handle_direct_write_start`** (`src/opendisplay_pipe.c:741-751`) → `opendisplay_display_direct_write_start()` (`src/opendisplay_display.cpp:710-810`):
   - Resolve `displays[0]` (`:75-82` — **only display instance 0 is ever used**), map `panel_ic_type` → bb_epaper panel (`opendisplay_map_epd`, `:726`).
   - `opendisplay_display_abort()` (cleans any prior stream), `display_power_set(true)` — drives `system_config.pwr_pin` high (`:104-120`).
   - Construct a fresh `BBEPAPER`, `setPanelType`, `setRotation(rotation * 90)`, `initIO(dc, reset, busy, cs, data, clk, 0)`, `wake()`, `sendPanelInitFull()`, `setAddrWindow(0,0,w,h)` (`:735-751`). Each step is timestamped by `dw_init_mark()` (`:69-73`).
   - Compute `s_total_bytes` from the color scheme (`opendisplay_color_direct_write_total_bytes`, `:759-760`) and choose the starting plane (`:761-764`).
   - **Compression detection is implicit**: a non-empty payload of ≥4 bytes means zlib, and those 4 bytes are the little-endian decompressed size (`:771`, `:782-786`). It must equal `s_total_bytes` (`:787-792`), and `transmission_modes` bit 0 (`STREAMING_DECOMPRESSION`) must be set (`:776-781`). Then `od_zlib_stream_reset(total)` and any remaining bytes are fed immediately (`:793-799`).
2. **`0x0071` `handle_direct_write_data`** (`pipe.c:753-772`) → `opendisplay_display_direct_write_data()` (`display.cpp:812-865`):
   - Compressed: `zlib_stream_to_direct_write()` (`:640-675`) pushes into `od_zlib_stream_push()` and polls 256 B at a time (`OPENDISPLAY_DECOMPRESSION_CHUNK_SIZE`, `:24`, buffer `:40`) into `dw_stream_raw_bytes()`.
   - Raw: `dw_stream_raw_bytes()` (`:600-638`) SPI-writes straight into panel RAM (`s_epd.writeData`), switching to `PLANE_1` exactly at the plane boundary for bitplane schemes (`:628-631`). **There is no framebuffer in RAM** — bytes go from BLE (or from the 256 B inflate window) directly to the controller.
   - Trailing bytes past `s_total_bytes` are ignored (up to 4 logged, `:843-854`). Progress is logged every 25 % (`:586-598`).
   - Each chunk is acked `{0x00,0x71}` (`pipe.c:771`).
3. **`0x0072` `handle_direct_write_end`** (`pipe.c:774-807`), a deliberate two-stage split:
   - `..._end_prepare()` (`display.cpp:869-909`): finalize the zlib stream, verify `written == total`.
   - Ack `{0x00,0x72}` is sent **before** the blocking refresh, then `k_msleep(20)` (`pipe.c:798-799`).
   - `..._end_refresh()` (`display.cpp:912-982`): `refresh(REFRESH_FULL)` — or `REFRESH_FAST` if `payload[0]==1` (`:948-951`) — `wait_for_refresh(60000)`, `sleep(DEEP_SLEEP)`, `display_power_set(false)`, ETag update from `payload[1..4]` (`:963-974`), then `opendisplay_touch_resume_after_refresh()` (`:980`).
   - Final response: `{0x00,0x73}` on refresh success, `{0x00,0x74}` on timeout (`pipe.c:805-806`).

### Partial write: `0x0076 → 0x0071* → 0x0072`

`opendisplay_display_partial_write_start()` (`display.cpp:444-565`). Payload (≥17 B): `[flags][old_etag BE32][new_etag BE32][x BE16][y BE16][w BE16][h BE16][data…]`. Validation, each with a distinct error code (`opendisplay_protocol.h:17-22`):
- unknown flags → `OD_ERR_PARTIAL_FLAGS` (only bit 0 = compressed is allowed, `:43-44`, `:475-480`)
- `partial_update_support == 0` → `OD_ERR_PARTIAL_UNSUPPORTED` (`:481-486`)
- `old_etag == 0 || old_etag != s_displayed_etag || new_etag == 0` → `OD_ERR_ETAG_MISMATCH` (`:487-492`)
- non-1bpp scheme → `OD_ERR_PARTIAL_UNSUPPORTED` (`:493-498`)
- rect out of bounds → `OD_ERR_RECT_OOB`; `x`/`w` not 8-aligned → `OD_ERR_RECT_ALIGN` (`:499-511`)

Stream size is `2 × ceil(w/8) × h` — two 1-bit controller planes (`:150-153`, `:513-514`). `partial_prepare_panel_ram()` powers the panel, uses `pInitPart` if present else `pInitFull`, and fills both planes white (`:394-424`). Bytes stream into `PLANE_1` first then `PLANE_0` (`:306`), with the address window re-set at each plane switch (`:298-327`). Refresh: `partial_trigger_refresh()` (`:374-392`) — `REFRESH_PARTIAL` by default, or FULL/FAST from `payload[0]` at end (`:915-922`). ETag is committed only on success (`:924-929`).

### Panel/driver abstraction

- **`opendisplay_epd_map.c`**: config `panel_ic_type` → `bb_epaper` panel enum (§3).
- **`bb_epaper` I/O glue**: `third_party/bb_epaper/src/nrf54_zephyr_io.inl` — selected by `TARGET_NRF54 || CONFIG_ZEPHYR` (`bb_epaper.cpp:46`). Provides Arduino-shaped shims (`digitalWrite`, `pinMode`, `delay`, `millis`) over `nrf54_gpio`, and **software bit-bang SPI only** (`bb_spi_bitbang`, `:30-42`) — no Zephyr SPI driver, no DMA, no hardware SPIM. This is why `app.overlay` disables `spi00` and `i2c22` (`zephyr/app.overlay:27-45`) so those peripherals don't contend with the bit-banged pins.
- **Busy handling**: `third_party/bb_epaper/src/nrf54_bbep_busy.inl` — idle level is HIGH for UC81xx chips, LOW otherwise (`:15-18`); `bbepWaitBusy()` polls every 20 ms with a 5 s (30 s for 3/4/7-color) timeout (`:20-50`). The app's own `wait_for_refresh()` (`display.cpp:127-142`) waits for a busy→idle *transition* with a 60 s cap.
- **Per-panel quirks** in `display.cpp:168-296`: EP397/EP426 800×480 panels skip `bbep_setAddrWindow` and get hand-written `SSD1608_SET_RAMX/YPOS/COUNT` sequences (EP397 uses Y-decrement, EP426 uses X-decrement), and skip re-init on partial refresh (`:190-193`).

### Color handling (`opendisplay_display_color.c`)

| scheme | meaning | bpp | planes | start plane |
|---|---|---|---|---|
| 0 | MONO | 1 | single | PLANE_0 |
| 1 / 2 | BWR / BWY | 1 | **two 1-bit planes** concatenated | PLANE_0 |
| 3 | BWRY | 2 | single packed | PLANE_1 |
| 4 | BWGBRY | 4 | single packed | PLANE_1 |
| 5 | GRAY4 | 2 | **two 1-bit planes** | PLANE_0 |
| 6 | GRAY16 | 4 | single packed | PLANE_0 |

All sizes are row-padded (`:63-73`). Note the deliberate divergence from the Arduino reference: scheme 6 is treated as 4 bpp here (`:15-18`), and GRAY4 starts at PLANE_0 (`:50-54`).

### Decompression (`third_party/uzlib/src/od_zlib_stream.c`)

A hand-written **incremental** zlib/DEFLATE inflater (state machine over `ST_ZLIB_CMF … ST_TRAILER`, `:68-80`) with a **512-byte** history window (`OPENDISPLAY_ZLIB_WINDOW_BITS = 9`, `uzlib.h:21-29`) held in static BSS (`OPENDISPLAY_ZLIB_USE_HEAP_WINDOW=0`, `zephyr/CMakeLists.txt:66`). It rejects streams whose header declares a bigger window (`:641-644`), enforces the expected output size (`:325-328`, `:551-554`), and verifies adler32 (`:547-550`). One global stream instance (`static od_zlib_stream_state_t s`, `:172`) — so only one decompressing transfer can be in flight, which is consistent with the single-connection design.

**Boot screen path**: `opendisplay_display_boot_apply()` (`:677-708`) runs once, on the display work queue; it skips rendering entirely if `transmission_modes` bit 7 (`CLEAR_ON_BOOT`) is set, and falls back to `fillScreen(BBEP_WHITE)` if `writeBootScreenWithQr()` returns false (`:699-705`).

---

## 6. Config & persistence

**Where it lives:** Zephyr `settings` backed by NVS on nRF54L RRAM (`CONFIG_SETTINGS`/`CONFIG_NVS`/`CONFIG_SETTINGS_NVS`, `zephyr/prj.conf:59-65`), single key **`od/config`** (`src/opendisplay_config_storage.c:7`).

**Record layout** (`src/opendisplay_config_storage.h:20-26`):
```
{ uint32 magic=0xDEADBEEF, uint32 version=1, uint32 crc (CRC-32/zlib), uint32 data_len, uint8 data[4096] }
```
Only `offsetof(data) + len` bytes are written (`opendisplay_config_storage.c:55-56`). `MAX_CONFIG_SIZE = 4096`, but the BLE write paths cap an inbound config at `MAX_CONFIG_CHUNKS(20) × CONFIG_CHUNK_SIZE(200) = 4000` B (`opendisplay_constants.h:20-22`), sized so `16 + 4000 < 4064` (the NVS sector limit, documented at `opendisplay_config_storage.h:7-17`).

**Read/write:**
- `loadConfig()` (`:64-98`): RAM cache first; otherwise `settings_load_one`, then magic + `data_len` + CRC-32 checks. Any failure → `false` (treated as "no config").
- `saveConfig()` (`:36-62`): builds the record in a **`static`** scratch (4 KB would blow the 4 KB main stack, `:38-41`), `settings_save_one()`, and only commits the RAM cache on success.
- `clearStoredConfig()` (`:100-106`): `settings_delete` + clear cache; **always returns true**.
- CRC-32 is a bit-wise reflected 0xEDB88320 implementation (`:14-29`).

**Blob (packet) format** — parsed by `parseConfigBytes()` (`src/opendisplay_config_parser.c:142`):
```
[len_lo][len_hi][version]  ( offset starts at 2, version at [2] :161-163 )
  then repeated TLV: [packet_number][packet_id][fixed-size data ...]
[crc16_lo][crc16_hi]       ( last 2 bytes )
```
- The trailing CRC is **CRC-16/CCITT-FALSE over the body with the two length bytes forced to 0** (`config_toolbox_outer_crc16`, `:83-101`; the same function is duplicated in `src/factory_config.c:15-32`). A mismatch is **logged but not fatal** (`:660-662`) — the config is still accepted.
- There is **no per-packet length field**; sizes come from a static table (`config_packet_data_size`, `:120-140`). A packet ID with a parse case is parsed; one with only a size entry is skipped by that size; a truly unknown ID forces a jump to the CRC, silently dropping every later packet (`:616-643`).
- Packet IDs (`src/opendisplay_constants.h:4-18`): 0x01 system(22B), 0x02 manufacturer(22B), 0x04 power(30B), 0x20 display(46B, ≤4), 0x21 led(22B, ≤4), 0x23 sensor(30B, ≤4), 0x24 data_bus(30B, ≤4), 0x25 binary_input(30B, ≤4), 0x26 wifi(160B), 0x27 security(64B), 0x28 touch(32B, ≤4), 0x29 passive_buzzer(32B, ≤4), 0x2A nfc(32B, ≤2), 0x2B flash(32B, ≤2), 0x2C data_extended(288B).
- `wifi_config` is parsed and stored (never used) purely so a config read-back round-trips (`:481-506`).
- `security_config` is extracted into a module-static `s_od_security_parsed` (`:8`, `:508-531`), **not** into `GlobalConfig`, plus a brute-force rescan fallback if the main pass missed it (`:46-65`, called at `:649`).

**Wire-level config write** (`src/opendisplay_pipe.c:889-1021`): a payload ≤200 B is saved directly; a payload >200 B begins a chunked write whose first 2 bytes are the total size (when ≥202 B), followed by `0x0042` chunks. On completion: `saveConfig` → `opendisplay_ble_reload_config_from_nvm()` → `clear_session()` (the key may have changed).

**Config read** (`:822-887`): chunk 0 = `[00][40][chunk#lo][chunk#hi][total_lo][total_hi][data…]`, later chunks omit the total. Max response 100 B (`MAX_RESPONSE_DATA_SIZE`).

**Factory config** (`src/factory_config.c`, `scripts/factory_config_gen.py`, `zephyr/CMakeLists.txt:42-53`): a CMake custom command always regenerates `src/generated/factory_config_data.c` — a stub by default (current tree contains only the "no factory embed" comment). With `-DFACTORY_CONFIG_HEX=…`, it emits `g_factory_embed` and defines `FACTORY_HAS_EMBED`; with `-DFACTORY_CLEAR_CONFIG_ON_BOOT=ON` it defines `FACTORY_CLEAR_CONFIG_ON_BOOT` so the device wipes NVS on every boot (`src/opendisplay_ble.c:656-660`).

**`device_flags`** (`src/opendisplay_device_flags.h`): bit0 PWR_PIN, bit1 XIAOINIT, bit2 WS_PP_INIT, bit3 BATTERY_LATCH, bit4 PWR_LATCH_DFF, bit5 **CHANNEL_SOUNDING**. Only bit 5 is actually consumed in this firmware (`src/opendisplay_cs.c:28`, logged at `src/opendisplay_config_parser.c:206-208`).

---

## 7. Peripherals

**GPIO abstraction (`nrf54_gpio.c`).** Everything uses the compact `(port<<4)|pin` config byte (README "Pin encoding"; decode at `:25-41`). Ports 0–3, pins 0–15, `0xFF` = unused. Interrupts: 8 static slots, each with its own `gpio_callback` recovered via `CONTAINER_OF` (`:73-96`), always `GPIO_INT_EDGE_BOTH` (`:127`). `nrf54_gpio_park()` sets `GPIO_DISCONNECTED`.

**I²C.** Two independent bit-bang implementations (see §9):
- `opendisplay_i2c.c` — used by SHT40 and BQ27220, driven from `DataBus` (0x24) config: `pin_1 = SCL`, `pin_2 = SDA`, `bus_speed_hz` (default 100 kHz, half-period clamped to ≥1 µs → ≤500 kHz, `:157-164`). Bounded clock stretch (`:7`, `:35-48`).
- `opendisplay_touch.c:70-174` — a separate GT911-specific bit-bang stack with a fixed 5 µs half-bit (`:43`) and 3-retry wrappers (`:237-259`).

The Zephyr I²C driver is **not** used by any app path; `i2c22` is explicitly disabled in the overlay because it would hold the display BUSY pin (P1.11/D5) high (`zephyr/app.overlay:36-45`).

**Battery (`opendisplay_battery.c`).** SAADC via `/zephyr,user` io-channels 0–7 exposed in the overlay (`zephyr/app.overlay:14-17`), one `adc_dt_spec` per AIN (`:39-48`). Only P1.00–P1.07 are analog-capable (`:55-67`). Enable pin (if any) is driven high, 10 ms settle, 10 samples averaged, then the enable pin is written low and parked (`:135-154`). 30 s cache; result in volts, and `get_10mv()` for the MSD. **BQ27220 wins over SAADC** when configured (`:170-179`).

**BQ27220 (`opendisplay_sensor_bq27220.c`).** Default address 0x55. Voltage `0x08` (2 B LE, mV), SOC `0x2C` (1 B, clamped 0–100). Charging state read from `power_option.charge_state_pin` with `CHARGER_FLAG_STATE_ACTIVE_LOW` polarity (`:65-82`). MSD byte = `soc & 0x7F | 0x80 if charging`, or `0xFF` if invalid (`:169-182`). Charge-enable pin driven at init per `CHARGER_FLAG_ENABLE_ACTIVE_LOW` (`:101-107`).

**SHT40 (`opendisplay_sensor_sht40.c`).** Default 0x44 (falls back through 0x44/0x45). `0xFD` measure, 12 ms delay, 6-byte read, CRC-8 (poly 0x31) on both words. Default MSD start byte 7 (`:41-49`). Invalid reads write `0xFF FF FF` (`:150-158`).

**Touch (`opendisplay_touch.c`).** GT911 only (`TOUCH_IC_GT911 = 1`; anything else is logged and skipped, `:509-513`). Registers 0x8140 (product ID), 0x814E (status), 0x814F (point 1). Address auto-resolve: INT high before RST release → 0x14, low → 0x5D (`:333-401`). Register byte-order (LE/BE) auto-detected by probing both (`:266-279`). Poll interval = `poll_interval_ms` or 100 ms floor (`:38`, `:607`). Coordinate mapping honors `TOUCH_FLAG_INVERT_X/Y/SWAP_XY` and clips to `displays[display_instance]` (`:459-486`). 5 consecutive I²C failures permanently disable that controller (`:616-620`). Post-EPD-refresh recovery via `opendisplay_touch_resume_after_refresh()`.

**Button (`opendisplay_button.c`).** From `BinaryInputs` (0x25) with `input_type == 1` and `button_data_byte_index ≤ 10` (`:59-61`); `input_flags` masks which of the 8 pins are active; `invert`/`pullups`/`pulldowns` are per-pin bitmasks (`:63-86`). GT911 INT pins are excluded (`:71-74`). A press bumps a 4-bit counter and triggers an MSD update + advertising boost (`:125-133`).

**Buzzer (`opendisplay_buzzer.c`).** `PassiveBuzzerConfig` (0x29) — `drive_pin`, optional `enable_pin` with `BUZZER_FLAG_ENABLE_ACTIVE_HIGH`, `duty_percent` (0 → 50). Square wave synthesized entirely in a self-rescheduling `k_timer` ISR (`:256-271`). Freq index → Hz: `400 + (11600 × (idx−1)) / 254` (`:74-82`), index 0 = rest. Step duration = `dunit × 5 ms`; 20 ms inter-pattern gap; 5 s hard cap. `_Static_assert(sizeof(PassiveBuzzerConfig) == 32)` (`:29-30`).

**LED (`opendisplay_led.c`).** `LedConfig` (0x21) — `led_1_r`, `led_2_g`, `led_3_b` (+ unused `led_4`), `led_flags` inversion bits. The animation program is packed into `led->reserved[0..11]`, written by the `0x0073` payload (`:347-349`): `reserved[0]` = `brightness<<4 | mode`, then three (color, loopdelay|loopcnt, interloop-delay) triplets and a group-repeat count (`:151-174`). Color byte = `RRR GGG BB` (3/3/2 bits, `:75-77`). Brightness is 7-slice software PWM at 100 µs/slice (`:87-113`), run **inline on the main thread**.

---

## 8. Build system & profiles

**`build.sh` (55 lines).** Sources `ncs-env.sh` (auto-detects `~/ncs/v3.*`, activates the matching toolchain via `print_toolchain_checksum.sh`), then:
```
west build -p ${PURGE:-always} -d build -b ${BOARD:-xiao_nrf54l15/nrf54l15/cpuapp} zephyr/ -- <cmake args>
```
Env knobs (`build.sh:6-32`): `APP_DIR`, `BUILD_DIR`, `BOARD`, `PROFILE` (`battery` default | `uart`), `PURGE`, `BUILD_VERSION`, `SHA` (→ `-DGIT_SHA`), `FACTORY_CONFIG_HEX`, `OPENDISPLAY_FACTORY_CLEAR_CONFIG`. `PROFILE` is exported so CMake can see it (`:12`, consumed at `zephyr/CMakeLists.txt:15`). Output hex is `build/merged.hex` (fallbacks at `:38-42`). `flash.sh` runs `west flash` then a `pyocd reset -t nrf54l` / `nrfjprog --reset`.

**`zephyr/CMakeLists.txt`.** Always appends `prj_ncs.conf` **and** `prj_cs.conf` to `EXTRA_CONF_FILE` (`:8-9`) — so **Channel Sounding is always compiled in**. Appends `prj_lm20_extra.conf` when `BOARD` matches `*lm20*` (`:11-13`), and `prj_uart.conf` for the uart profile (`:15-19`). Compile definitions (`:62-73`): `TARGET_NRF54`, `OD_APP_VERSION=0x0100`, `OPENDISPLAY_BUILD_ID="nrf54"`, `OPENDISPLAY_ZLIB_USE_HEAP_WINDOW=0`, plus `NRF54_BOARD_LM20` or `NRF54_BOARD_L15`. Sources list at `:88-117` (app + `bb_epaper.cpp` + `Group5.cpp` + `od_zlib_stream.c` + `qr/qrcode.c` + generated factory data).

**Profiles / conf files:**

| File | Purpose | Key settings |
|---|---|---|
| `prj.conf` (77 lines) | base, always applied | GPIO/SPI/I2C drivers on; **`CONFIG_SERIAL=n`, RTT console** (battery-safe: an unpowered USB-UART bridge would be back-fed via TX, `:5-14`); C++ + full libc++ + newlib; main stack 4096, sysWQ stack 4096, heap 8192 (`:22-24`); HWINFO; SENSOR + `TEMP_NRF5`; ADC/SAADC; BT peripheral + HCI VS + MTU 512 + BT RX stack 8192 + TX pools of 12 (`:36-57`); settings/NVS/flash-map; MbedTLS PSA (AES/CMAC/ECB) + entropy; LOG level 3 |
| `prj_ncs.conf` (11 lines) | **always merged** (SoftDevice Controller) | SMP + bondable + BT settings; dynamic conn callbacks; auto conn-param update **off**; DLE 251; 2M PHY; auto PHY update **off**; `BT_MAX_CONN=1` |
| `prj_cs.conf` (14 lines) | **always merged** | `BT_CHANNEL_SOUNDING`, `BT_RAS` + `BT_RAS_RRSP`, mode-3 off, reflector-only, 1 antenna / 2 antenna paths, `BT_TRANSMIT_POWER_CONTROL=y` |
| `prj_uart.conf` (13 lines) | `PROFILE=uart ./build.sh` | `CONFIG_SERIAL=y`, UART console, **RTT off**, `LOG_BACKEND_UART=y`, `LOG_MODE_IMMEDIATE=y` |
| `prj_lm20_extra.conf` (2 lines) | `BOARD` matches `*lm20*` | comment only — it *documents* disabling `CONFIG_BT_CTLR_ASSERT_OPTIMIZE_FOR_SIZE` (gcc 8.2 can't build the size-optimized LL_ASSERT asm), and the line is a Kconfig "is not set" comment |

**`app.overlay`** (`zephyr/app.overlay`): console → `uart20`; all 8 SAADC channels exposed via `/zephyr,user`; **`spi00` disabled** (would contend with the bit-banged P2.1 CLK / P2.2 DATA); **`lsm6ds3tr_c` disabled** (unused IMU, failing boot probe); **`i2c22` disabled** (its idle-high on P1.11 would pin the display BUSY line high and time out every `bbepWaitBusy()`).

**Boards / LM20 status.** Default and only CI-built target is `xiao_nrf54l15/nrf54l15/cpuapp` (`build.sh:8`, `.github/workflows/main.yaml`, `release.yml`). **LM20 is not buildable out of the box**: NCS v3.3.1 ships no `seeed-xiao-nrf54lm20a` board (`README.md` "nRF54LM20A", `docs/LM20_NCS.md`). The code paths exist (`NRF54_BOARD_LM20` in `board_nrf54.c:6`, `prj_lm20_extra.conf`, the CMake `*lm20*` match) but the board DTS must be vendored first. Releases publish `nrf54l15-<tag>.hex` from `build/merged.hex` (`release.yml:44-47`).

---

## 9. Gaps, TODOs, stubs, dead code

**Stubs / unimplemented features**
1. **DFU (`0x0051`)**: acks success then does nothing — `opendisplay_ble_schedule_dfu()` prints "DFU not implemented on nRF54 yet" (`src/opendisplay_ble.c:725-728`). The ack is sent *before* the stub (`src/opendisplay_pipe.c:1241-1247`), so a client sees success. (`opendisplay_pipe.c:1250-1252` even calls this out as "the separate DFU-honesty question for 0x0051".)
2. **Deep sleep (`0x0052`)**: stub (`src/opendisplay_ble.c:730-733`); deliberately sends no response so clients don't treat it as supported (`src/opendisplay_pipe.c:1248-1254`).
3. **NFC**: `opendisplay_ble_nfc_read()` / `opendisplay_ble_nfc_write()` unconditionally return `false` (`src/opendisplay_ble.c:735-751`). The entire `0x0082` endpoint — read, single write, and the 3-stage chunked write with a 512 B staging buffer (`src/opendisplay_pipe.c:59-68`, `:1023-1176`) — is fully implemented on the pipe side but always errors out (`0xFF … 0x02` / `0x03`). `NfcConfig` (0x2A) is parsed and stored but never consumed.
4. **`opendisplay_ble_set_connection_requested()`** (MSD status bit 2) has **no producer** — explicitly documented as a hook for a future feature (`src/opendisplay_ble.h:29-34`).
5. **Wi-Fi config (0x26)** is parsed and stored only so a read-back round-trips; the nRF54 has no Wi-Fi (`src/opendisplay_config_parser.c:481-486`).
6. **No power management.** `CONFIG_PM` / `CONFIG_PM_DEVICE` appear in no conf file; `power_option.power_mode`, `sleep_flags`, `deep_sleep_current_ua`, `deep_sleep_time_seconds`, `capacity_estimator`, `battery_capacity_mah` are parsed and unused. Only `sleep_timeout_ms` and `tx_power` and the battery-sense fields are consumed.
7. **`battery_sense_flags` / `BATTERY_SENSE_FLAG_ENABLE_INVERTED`** is defined (`src/opendisplay_structs.h:48`) but deliberately **not honored** (`src/opendisplay_battery.c:133-135`).
8. **G5 compression** (`TRANSMISSION_MODE_G5`) — flag defined, "not implemented" (`src/opendisplay_constants.h:49`, and `Group5.cpp` is compiled in but unused by any app path).
9. **`TRANSMISSION_MODE_ZIP`** (full-window zip) — flag defined, not implemented (only the 512-byte streaming inflater exists).
10. **Missing EPD panels**: protocol IDs `0x0047`, `0x0048`, `0x004A`, `0x004C` map to `EP_PANEL_UNDEFINED`, and `0x0049`/`0x004B` are **wrong-panel substitutions** that "may render incorrectly" (`src/opendisplay_epd_map.c:78-92`).
11. **Touch: only GT911.** Any other `touch_ic_type` is skipped with a log (`src/opendisplay_touch.c:509-513`).
12. **Only `displays[0]` is ever used** by the display pipeline (`src/opendisplay_display.cpp:75-82`), and only `boot_cfg()->displays[0]` by the boot screen (`src/boot_screen.cpp:611-621`) — despite the config supporting 4.
13. **Channel Sounding: RAS responder connection allocation is absent.** `opendisplay_cs.c` includes `<bluetooth/services/ras.h>` (`:13`) and enables `CONFIG_BT_RAS_RRSP` (`prj_cs.conf:5`) but never calls `bt_ras_rrsp_alloc()`/`_free()` on connect/disconnect — unlike the NCS `ras_reflector` sample the README points at. Only `bt_le_cs_set_default_settings()` and `bt_le_cs_set_procedure_parameters()` are called (`:140`, `:167`).
14. **LM20 board target does not exist** in NCS v3.3.1; LM20 is not built in CI (`docs/LM20_NCS.md`, `README.md`).

**Dead / vestigial code**
15. `s_long_write_buf[244]`, `s_long_write_len`, `s_long_write_conn` (`src/opendisplay_pipe.c:51-53`) are only ever zeroed in the disconnect-cleanup path (`:1414-1415`) — never written or read otherwise. Leftover from a long-write reassembly design; wastes 244 B.
16. `AUTH_STATUS_ALREADY` (`src/opendisplay_protocol.h:36`) and `RESP_DEEP_SLEEP` (`:55`) are defined but never used.
17. `opendisplay_cs_scan_response_count()` (`src/opendisplay_cs.c:32-35`) is exported but never called (`sd_prepare` uses the `count_out` from `fill_scan_response` instead).
18. `opendisplay_ble_restart_advertising()` (`src/opendisplay_ble.c:619-622`), `opendisplay_display_power_off()` (`src/opendisplay_display.cpp:122-125`), `opendisplay_display_partial_active()` (`:439-442`), `opendisplay_led_is_active()` (`src/opendisplay_led.c:386-389`), `od_zlib_stream_output_count()` (`third_party/uzlib/src/od_zlib_stream.c:721-723`) — public, no in-tree callers.
19. `uzlib_adler32` / `uzlib_crc32` are declared in `uzlib.h:44-45` but **not defined** in `od_zlib_stream.c` (they'd be a link error if called; nothing calls them).
20. **Two independent bit-bang I²C stacks** — `opendisplay_i2c.c` (sensors) and the private one inside `opendisplay_touch.c:70-174`. The touch file's header comment explains the split, but it is duplicated logic with different timing (5 µs fixed vs. config-derived) and different stretch handling.
21. **`config_toolbox_outer_crc16` is duplicated** in `src/opendisplay_config_parser.c:83-101` and `src/factory_config.c:15-32` (the latter's comment says "Kept local so this file has no dependency on the parser's static helper").
22. `TRANSMISSION_MODE_CLEAR_ON_BOOT` is `#define`d twice — in `src/opendisplay_constants.h:51` and again in `src/opendisplay_config_parser.c:68`.
23. `LED_FLAG_INVERT_LED4` (`src/opendisplay_led.c:14`) and `LedConfig.led_4` are never used.
24. `nrf54_gpio_configure_interrupt()` has no un-register path; slots leak if init is ever re-run (it isn't today).
25. `src/newlib_stubs.c` — an empty `_fini()` purely to satisfy the linker.
26. `tools/__pycache__/config_packet.cpython-314.pyc` is committed to the repo.

**Correctness/robustness observations**
27. The outer **CRC-16 mismatch is logged but the config is still accepted** (`src/opendisplay_config_parser.c:657-665`) — only the *storage-layer* CRC-32 is enforcing.
28. `clearStoredConfig()` always returns `true` even if `settings_delete` failed (`src/opendisplay_config_storage.c:100-106`), so `handle_config_clear` can report success on a failed erase.
29. A genuinely unknown config packet ID still silently drops every subsequent packet, because the TLV format carries no per-packet length (acknowledged in-code at `src/opendisplay_config_parser.c:617-624`, `:637-641`).
30. `bt_hci_cmd_alloc(K_FOREVER)` in `apply_tx_power()` (`src/opendisplay_ble.c:510`) is an unbounded block, reachable from the BT `connected` callback.
31. The boot-screen work item is submitted while BLE is already advertising (`src/opendisplay_ble.c:696` then `:704`), so a fast client can start a direct write on the main thread while the boot screen is still refreshing on the display workqueue — see §2.5 item 4.
