# Sister Repo Map — `../Firmware` (nRF52840 + ESP32, PlatformIO/Arduino)

> Generated 2026-07-13. Documentation-only audit of the **reference implementation**.
> Companion docs: [ARCHITECTURE_NRF54.md](ARCHITECTURE_NRF54.md), [RTOS_COMPARISON.md](RTOS_COMPARISON.md), [FEATURE_PARITY_VS_FIRMWARE.md](FEATURE_PARITY_VS_FIRMWARE.md)
>
> All `file:line` citations are relative to `/home/davelee/opendisplay/Firmware`.

Target: nRF52840 (Adafruit/Bluefruit + SoftDevice) and ESP32-S3 / -C3 / -C6 / classic ESP32, PlatformIO + Arduino.

> **Read-me-first caveat:** two docs in that repo describe *problems*, not shipped behaviour. `docs/FINDINGS_NONBLOCKING_LOOP_2026-07-13.md` is an **investigation** — the "Tier A service pump" it recommends is **not implemented**; the loop still blocks fully during a panel refresh. `FINDINGS.md` (2026-07-05) is an older audit and many items are now fixed. Trust the source, cited below, over both.

---

## 1. Boot & init sequence — `setup()` traced call by call

`src/main.cpp:49-158`.

| # | Call | Line | Notes |
|---|---|---|---|
| 1 | `LogSerialPort.begin(115200, SERIAL_8N1, RX=44, TX=43)` **or** `Serial.begin(115200)` | `main.cpp:50-56` | Chosen by `OPENDISPLAY_LOG_UART` (external CH343P UART) vs native USB-CDC. `DISABLE_USB_SERIAL` suppresses both. |
| 2 | Print firmware version + git SHA | `main.cpp:57-68` | `BUILD_VERSION` / `SHA` injected by CI (`.github/workflows/release.yml:29`). Strips surrounding quotes. |
| 3 | **ESP32 only:** `esp_reset_reason()` + `esp_sleep_get_wakeup_cause()` | `main.cpp:73-93` | `is_deep_sleep_wake = (cause != ESP_SLEEP_WAKEUP_UNDEFINED)`. On wake: `woke_from_deep_sleep=true`, `deep_sleep_count++`, `detectButtonWake()`. **Critical subtlety** (`main.cpp:86-90`): RTC memory segments are *reloaded from the app image* on every reset except a deep-sleep wake, so `RTC_DATA_ATTR` does **not** survive PANIC/WDT/SW/brownout — a hidden mid-cycle reset is indistinguishable from first boot. |
| 4 | `full_config_init()` | `main.cpp:96` → `config_parser.cpp:819-863` | Mount LittleFS → load `/config.bin` → on failure `tryProvisionFactoryEmbed()` → `printConfigSummary()` → `clearEncryptionSession()` → `encryptionInitialized=true` → `checkResetPin()` → (nRF) `powerDownExternalFlashFromConfig()` + `xiaoinit()` if `DEVICE_FLAG_XIAOINIT` → `ws_pp_init()` if `DEVICE_FLAG_WS_PP_INIT` → `powerLatchBegin()`. |
| 5 | `initio()` | `main.cpp:98` → `display_service.cpp:700-755` | LED pins + boot RGB flash sequence (`flashLed(0xE0/0x1C/0x03/0xFF, 15)`, gated on VBUS present on nRF, `display_service.cpp:726-737`) → `initPassiveBuzzers()` → `pwr_pin` OUTPUT LOW → `initDataBuses()` → `initSensors()`. |
| 6 | **nRF only:** `ble_nrf_stack_init()` | `main.cpp:101` → `ble_init.cpp:157-193` | SoftDevice **must** start before display/SPI. |
| 7 | If **not** a deep-sleep wake: `rebootFlag = 1`; `initDisplay()` | `main.cpp:103-113` | This is the boot-screen redraw path. A wake skips it entirely — that skip *is* the wake path's main energy saving. |
| 8 | **ESP32:** `ble_init()` (after display) / **nRF:** `ble_nrf_advertising_start()` | `main.cpp:114-121` | |
| 9 | **ESP32, non-wake only:** `initWiFi(false)` | `main.cpp:122-126` | On a wake, WiFi is deferred to `fullSetupAfterConnection()`. |
| 10 | `updatemsdata()` | `main.cpp:127` | Builds + publishes the 16-byte manufacturer-specific advertising payload. |
| 11 | `initButtons()` | `main.cpp:129` → `device_control.cpp:556-663` | |
| 12 | `initTouchInput()` | `main.cpp:131` → `touch_input.cpp:471` | |
| 13 | **ESP32, on wake:** arm the advertising window **LAST** | `main.cpp:132-146` | `advertising_timeout_active=true`, `advertising_start_time=millis()`. If `woke_by_button`, also arm `minWakeWindowActive`. Ordering is deliberate: buttons/GT911 bring-up must not eat the host's connection window. |
| 14 | **ESP32, first boot (`deep_sleep_count==0`):** arm min-wake hold | `main.cpp:147-153` | |
| 15 | **ESP32:** `lastActivityMs = millis()` | `main.cpp:155` | Both sleep paths measure quiet time from here, not from power-on. |

`ble_nrf_stack_init()` detail (`ble_init.cpp:157-193`): `configCentralBandwidth/configPrphBandwidth(BANDWIDTH_MAX)`, `autoConnLed(false)`, `setTxPower(power_option.tx_power)`, `Bluefruit.begin(1,0)`, `imageService.begin()`, `imageCharacteristic.setWriteCallback(imageDataWritten)`, `imageCharacteristic.begin()`, **then** `bledfu.begin()` *only if encryption is disabled*. The DFU-last ordering is load-bearing: GATT handles are assigned in `begin()` order, so registering the conditional DFU service last keeps `imageCharacteristic`'s handles + CCCD stable across encryption on/off (`ble_init.cpp:171-176`). Then device name `"OD" + getChipIdHex()`, `sd_power_mode_set(NRF_POWER_MODE_LOWPWR)`, `sd_power_dcdc_mode_set(NRF_POWER_DCDC_ENABLE)`.

---

## 2. THE MAIN LOOP — `loop()` semantics

`src/main.cpp:262-424`. This is the most important section for the port; read it alongside the diagram at the end.

### 2.1 Unconditional prologue (every pass, both platforms)

```
processLedFlash();     // main.cpp:263  — millis()-polled LED pattern state machine
epdSessionTick();      // main.cpp:264  — millis()-polled EPD keep-alive expiry
buzzerService();       // main.cpp:265  — millis()-polled non-blocking melody step
#ifdef TARGET_ESP32
pollActivity();        // main.cpp:267  — single point of activity detection
#endif
```

### 2.2 `pollActivity()` — the activity sampler (ESP32 only)

`main.cpp:187-230`. Rather than have every producer (BLE host task, buttons, touch, LAN) stamp a timestamp, it **samples state they already mutate** and treats any change since the previous pass as activity, stamping `lastActivityMs`. Sampled signals:
- `commandQueueHead` / `responseQueueHead` (producer-side, so a command that arrived *and drained* within one pass still registers) — `main.cpp:199-200`
- `pServer->getConnectedCount()` (covers connect **and** disconnect edges) — `main.cpp:203`
- `wifiInitialized && wifiServerConnected && wifiClient.connected()` — `main.cpp:204`
- `memcmp` of the whole `dynamicreturndata[11]` (button presses + touch events land here) — `main.cpp:213`
- `bleRestartAdvertisingPending` — the only trace of a connect+drop entirely between two passes — `main.cpp:217`
- Live link / unfinished queue work is activity *in itself*, not just its edges — `main.cpp:219-221`

### 2.3 Branch A — deep-sleep wake advertising window (ESP32)

`main.cpp:271-307`, active while `woke_from_deep_sleep && advertising_timeout_active`:
1. Client connected → `advertising_timeout_active=false`, `fullSetupAfterConnection()`, `woke_from_deep_sleep=false`, `return` (next pass falls into the normal loop). `main.cpp:272-278`
2. `bleRestartAdvertisingPending` → `esp32_restart_ble_advertising()` — a connect+drop entirely inside one poll gap would otherwise leave the radio dark for the rest of the window. `main.cpp:281-283`
3. Timeout: `idle_duration = millis() - lastActivityMs` (measured from **last activity**, not window start, so a connect-then-drop re-arms the full window). If `idle_duration >= sleep_timeout_ms` (default `DEFAULT_IDLE_HOLD_MS = 10000`, `main.h:340`) **and** `!minWakeHoldActive()` → `enterDeepSleep()`. `main.cpp:284-299`
4. Otherwise `idleDelay(50)` and `return` — `idleDelay` services buttons/touch/LED/EPD-tick/buzzer while it waits, so a wake-time touch is polled during the window. `main.cpp:305`

### 2.4 Branch B — normal work (ESP32)

1. **Bounded command drain** (`main.cpp:315-333`): drains up to `COMMAND_QUEUE_SIZE` (33) commands per pass. Acquire/release atomics on head/tail. Each is dispatched via `imageDataWritten(NULL, NULL, data, len)` (misleadingly named — it services *every* BLE command). **`flushResponseQueueToBle()` is called BETWEEN commands** (`main.cpp:331`) — critical, because at small negotiated `ack_every` a 33-command drain can emit ~32 pipe ACKs, which would overflow the 10-slot response ring (which **drops the newest entry**) and collapse throughput.
2. `flushResponseQueueToBle()` again (`main.cpp:334`) — drains up to 16 notifications per call (`main.cpp:241`). If connected but CCCD not enabled, responses stay queued; if disconnected, they're discarded (`main.cpp:251-258`).
3. `bleRestartAdvertisingPending` → `esp32_restart_ble_advertising()` (`main.cpp:335-337`).
4. **Direct-write watchdog:** `directWriteActive && millis()-directWriteStartTime > 900000` (15 min) → `cleanupDirectWriteState(true)` (`main.cpp:338-344`).
5. `checkPartialWriteTimeout()` — same 15-min ceiling for `partialCtx` (`main.cpp:345`, `display_service.cpp:357-366`).
6. `handleWiFiServer()` (`main.cpp:348`) — deliberately **after** BLE queue processing so it can't block BLE responses.
7. **WiFi link watchdog every 10 s** (`main.cpp:349-363`): detects loss → `disconnectWiFiServer()`; detects reconnect → `restartWiFiLanAfterReconnect()`.
8. **`workInFlight`** (`main.cpp:372-377`) = queues non-empty ∨ BLE connected ∨ `bleRestartAdvertisingPending` ∨ `epdRefreshInProgress` ∨ LAN session. Every term is transient, so this must **never** be the sole sleep gate — `lastActivityMs` supplies the quiet window.
   - **`workInFlight` true** → `processButtonEvents()`, `processTouchInput()`, `buzzerService()`, `delay(1)` — a tight 1 ms-cadence loop.
   - **`workInFlight` false** →
     - If `deep_sleep_time_seconds > 0 && power_mode == 1`: `idleMs = millis()-lastActivityMs`. If `idleMs < idleHoldMs` (or `minWakeHoldActive()`) → `idleDelay(5)`, else `enterDeepSleep()` (`main.cpp:384-398`).
     - Else (mains/no-sleep): `idleDelay(2000)` (`main.cpp:400`).
     - Then a **60 s** `updatemsdata()` refresh timer (`main.cpp:402-406`), then buttons/touch/buzzer.

### 2.5 Branch C — nRF loop tail

`main.cpp:411-423`. Much simpler — **no deep sleep, no command queue, no response queue**:
```
if (power_option.sleep_timeout_ms > 0) { idleDelay(sleep_timeout_ms); updatemsdata(); }
else                                     idleDelay(500);
ble_nrf_advertising_tick();
processButtonEvents(); processTouchInput(); buzzerService();
```
Commands are **not** queued on nRF — `imageDataWritten` is registered directly as the Bluefruit write callback (`ble_init.cpp:167`) and therefore runs **on the Bluefruit callback task**, not in `loop()`.

### 2.6 `idleDelay()` — the cooperative delay

`main.cpp:428-444`. Chunks the delay into 100 ms slices (`CHECK_INTERVAL_MS = 100`), and between slices runs: `ble_nrf_advertising_tick()` (nRF), `processButtonEvents()`, `processTouchInput()`, `processLedFlash()`, `epdSessionTick()`, `buzzerService()`. This 100 ms granularity is **the nRF buzzer's quantisation floor** (`docs/FINDINGS_NONBLOCKING_LOOP_2026-07-13.md:75-79`).

### 2.7 Blocking work (what the loop does NOT service)

| Blocking site | file:line | Cost |
|---|---|---|
| `waitforrefresh()` — `delay(10)` + `bbepIsBusy()` poll | `display_service.cpp:509-535` | **1–5 s typical**. Note `bbepIsBusy()` itself does `delay(10)+delay(1)` internally, so the real poll period is ~21 ms and the "60 s" timeout is really ~120 s (`FINDINGS_NONBLOCKING_LOOP:63-68`). |
| `pwrmgm(true)` rail settle — fixed `delay(800)` | `main.cpp:611` | ~0.9–1.0 s per **cold** acquire |
| Seeed/IT8951 `update()` — 2 GC16 passes, unbounded internal busy-wait | `display_seeed_gfx.cpp:179-184` | **4–10 s** |
| `bbepSendCMDSequence` panel init `BUSY_WAIT` opcodes | — | N × up to 5 s |
| `flashLed()` busy-waits with `delayMicroseconds` | `device_control.cpp:485-513` | up to ~200 ms |
| `powerOff()` unbounded wait for button release | `power_latch.cpp:86-90` | unbounded |

**Platform consequence** (`FINDINGS_NONBLOCKING_LOOP:15-20`): on **ESP32**, `loop()` is *inside* the refresh, so **nothing else runs at all** for its duration — the sounding buzzer note drones for 1–5 s. On **nRF**, `delay()` is `vTaskDelay` so it yields, and `loop()` keeps running (this is why `pwrmgmLock` exists at all — the nRF panel session is genuinely touched from two tasks).

### 2.8 Deep-sleep entry/exit — `enterDeepSleep(bool force, uint16_t overrideSleepSeconds)`

`main.cpp:468-530`. Order is load-bearing:
1. `power_mode != 1` → skip (not battery). `main.cpp:469-472`
2. `deep_sleep_time_seconds == 0` → skip. `main.cpp:473-476`
3. `!force && connectedCount > 0` → skip, re-stamp `lastActivityMs` (a central can connect in the gap between the caller sampling idle and getting here). `main.cpp:479-483`
4. `!force && minWakeHoldActive()` → skip. **Must stay ahead of the advertising stop**: everything past that commits to `esp_deep_sleep_start()`. `force` (host 0x0052) bypasses. `main.cpp:488-491`
5. `epdSessionForceOff()` — must sit **below** every early return. Net effect on battery ESP32: effective keep-alive = `min(configured window, idle-hold)`. `main.cpp:500`
6. `woke_from_deep_sleep = true` (for the next boot), stop advertising, `delay(200)`, `BLEDevice::deinit(true)`, `esp32_ble_clear_handles()`, `delay(100)`. `main.cpp:501-513`
7. `esp_sleep_enable_timer_wakeup(sleepSeconds × 1e6)` — `overrideSleepSeconds` (from 0x0052) applies to **this one cycle only**, never stored. `main.cpp:516-519`
8. `armButtonWakeSources()` — **after** the timer arm, **before** `powerLatchHoldForSleep()`, so the latch-hold `gpio_hold_en()` can't disturb freshly configured RTC pulls. `main.cpp:522`
9. `flushLog()`, `delay(100)`, `powerLatchHoldForSleep()`, `esp_deep_sleep_start()`. `main.cpp:526-529`

### 2.9 Every task / callback / ISR and how it meets `loop()`

| Context | Entry point | file:line | Interaction with `loop()` |
|---|---|---|---|
| **ESP32 BLE host task** | `MyBLECharacteristicCallbacks::onWrite` | `esp32_ble_callbacks.h:87-124` | **Only memcpys** into a lock-free SPSC ring (`commandQueue[33]`, `MAX_COMMAND_SIZE=256`) with `__atomic` acquire/release. Drops the command if the ring is full. Never touches the display. |
| ESP32 BLE host task | `MyBLEServerCallbacks::onConnect` | `esp32_ble_callbacks.h:48-54` | `rebootFlag=0`, `esp32BleNotifySubscribed=false`, `updatemsdata()` |
| ESP32 BLE host task | `MyBLEServerCallbacks::onDisconnect` | `esp32_ble_callbacks.h:55-74` | If `epdRefreshInProgress` → defers cleanup to loop; else `cleanupDirectWriteState(true)` + `cleanupPartialWriteOnDisconnect()`. Always `resetPipeWriteState()` and sets `bleRestartAdvertisingPending = true` (consumed by loop). |
| ESP32 BLE host task | `onSubscribe` (NimBLE only) | `esp32_ble_callbacks.h:80-85` | Sets `esp32BleNotifySubscribed` |
| **nRF Bluefruit callback task** | `imageDataWritten` (the write callback itself) | `ble_init.cpp:167` | **Runs the entire command handler, including the display path and `sendResponse()`, off the loop task.** This is the single biggest architectural divergence from ESP32. |
| nRF Bluefruit task | `connect_callback` | `device_control.cpp:84-94` | `rebootFlag=0`, `updatemsdata()`, `ble_nrf_log_link_params()`, `ble_nrf_request_fast_link()` (2M PHY + 251-octet DLE), `ble_nrf_arm_link_diag()` |
| nRF Bluefruit task | `disconnect_callback` | `device_control.cpp:96-109` | `cleanupDirectWriteState(true)`, `cleanupPartialWriteOnDisconnect()`, `resetPipeWriteState()` |
| **nRF FreeRTOS timer task** | `ble_nrf_link_diag_cb` (one-shot `SoftwareTimer`) | `ble_init.cpp:111-118, 146-155` | Re-logs PHY/MTU/DLE ~2.5 s after connect, once negotiation settles. Armed only in `connect_callback` — no per-loop polling. |
| **Button GPIO ISR** | ESP32: `buttonISR(void*)` `IRAM_ATTR`, attached per-pin with `attachInterruptArg(pin, buttonISR, index, CHANGE)`; nRF: `buttonISRGeneric()` scanning all buttons | `device_control.cpp:517-554`, attach at `:616-618` | Both funnel into `handleButtonISR()` (`device_control.cpp:517-532`): debounce-free edge check, `press_count = (press_count+1) & 0x0F` on press, sets `volatile buttonEventPending` + `lastChangedButtonIndex`. Consumed by `processButtonEvents()` in loop (`device_control.cpp:425-457`) under `noInterrupts()/interrupts()`. |
| **Touch GT911 INT ISR** | `touch_isr_0..3` (`IRAM_ATTR` on ESP32) | `touch_input.cpp:120-133` | Sets a bit in `s_touch_irq_mask`; consumed by `processTouchInput()` in loop (`touch_input.cpp:575`). |
| **Cross-task lock** | `pwrmgmLock` (`volatile uint8_t`, `__atomic_exchange_n`) | `main.h:195`, `display_service.cpp:204-217` | `epdSessionAcquire/Release/ForceOff` **take** it (`pwrmgmLockTake` uses `delay(1)` = `vTaskDelay` while spinning, to avoid priority-inversion livelock with the higher-priority Bluefruit task); `epdSessionTick` **try-locks** and skips its pass if held, so it can never rail-cut mid-init. |

### 2.10 ASCII runtime diagram

```
                         ESP32                                        nRF52840
   ┌────────────────────────────┐                     ┌──────────────────────────────┐
   │  BLE host task (Bluedroid) │                     │  Bluefruit callback task     │
   │  onWrite(): memcpy only    │                     │  imageDataWritten():         │
   │    ↓ SPSC ring (33×256)    │                     │    FULL command handling,    │
   │  onConnect/onDisconnect    │                     │    display SPI, sendResponse │
   │    → bleRestartAdvPending  │                     │    → takes pwrmgmLock        │
   └──────────┬─────────────────┘                     └───────────┬──────────────────┘
              │ commandQueueHead (RELEASE)                        │  (no queue at all)
   ┌──────────▼──────────────────────────────────────────────────▼──────────────────┐
   │                                   loop()                                        │
   │  processLedFlash()  epdSessionTick()  buzzerService()   [+ pollActivity() ESP32]│
   │                                                                                 │
   │  ESP32 ──┬── woke_from_deep_sleep && advertising_timeout_active ?               │
   │          │      ├─ connected      → fullSetupAfterConnection(); clear flags     │
   │          │      ├─ idle >= T && !minWakeHold → enterDeepSleep()                 │
   │          │      └─ else idleDelay(50); return   ◄── touch/buttons still polled  │
   │          │                                                                      │
   │          └── normal:                                                            │
   │               drain ≤33 cmds ─┬─► imageDataWritten() ─► display/pipe/config …   │
   │                               └─► flushResponseQueueToBle()  (BETWEEN each cmd) │
   │               flushResponseQueueToBle(); restartAdvIfPending()                  │
   │               directWrite 15-min watchdog; checkPartialWriteTimeout()           │
   │               handleWiFiServer();  WiFi link check (10 s)                       │
   │               workInFlight? ─ yes ─► buttons+touch+buzzer, delay(1)             │
   │                            └─ no  ─► idleHold? idleDelay(5) : enterDeepSleep()  │
   │                                      (mains: idleDelay(2000) + 60 s MSD refresh)│
   │                                                                                 │
   │  nRF  ──► idleDelay(sleep_timeout_ms|500) ─► updatemsdata()                     │
   │           ble_nrf_advertising_tick(); buttons; touch; buzzer                    │
   └─────────┬──────────────────────────┬──────────────────────┬────────────────────┘
             │ epdSessionTick (trylock) │ processButtonEvents  │ processTouchInput
             ▼                          ▼                      ▼
   ┌───────────────────────┐   ┌──────────────────┐   ┌─────────────────────┐
   │ EPD power FSM         │   │ button ISR flags │   │ GT911 INT ISR mask  │
   │ PWR_OFF/WARM/ACTIVE   │   │ (volatile)       │   │ (volatile)          │
   │ guarded by pwrmgmLock │   └──────────────────┘   └─────────────────────┘
   └───────────────────────┘

  BLOCKING (nothing else runs on ESP32): waitforrefresh() 1-5 s │ pwrmgm(true) delay(800)
                                          Seeed update() 4-10 s │ flashLed() ≤200 ms

  EPD POWER STATE MACHINE (display_service.cpp:241-317):
        pwrmgm(true)                 epdSessionRelease(success) & window>0
   OFF ─────────────► ACTIVE ──────────────────────────────────────► WARM
    ▲                   │  ▲                                            │
    │                   │  └──── epdSessionAcquire() [warm re-acquire]──┘
    │  epdSessionForceOff / Release(fail) / Release(window==0)          │
    └───────────────────┴──────────── epdSessionTick(): deadline hit ◄──┘
```

---

## 3. Full feature inventory

### 3.1 BLE / LAN command opcodes (16-bit big-endian; dispatcher `communication.cpp:503-669`)

| Opcode | Name | Handler | Payload | Response |
|---|---|---|---|---|
| `0x000F` | REBOOT | `communication.cpp:602-606` → `device_control.cpp:111-120` | — | none (`NVIC_SystemReset` / `esp_restart`) |
| `0x0040` | READ CONFIG | `communication.cpp:351-393` | — | chunked `00 40 chunk:2LE [totalLen:2LE on chunk 0] data…`, ≤100 B/frame |
| `0x0041` | WRITE CONFIG | `communication.cpp:395-437` | full blob, or `[totalSize:2LE][chunk0]` if >200 B | `00 41 00 00` / `FF 41 00 00` / `00 41 FE` (auth) |
| `0x0042` | WRITE CONFIG CHUNK | `communication.cpp:453-493` | ≤200 B | `00 42 00 00` / `FF 42 00 00` |
| `0x0043` | FIRMWARE VERSION | `communication.cpp:316-349` | — | `00 43 major minor shaLen sha[≤40]` — **bypasses the encryption gate** |
| `0x0044` | READ MSD | `communication.cpp:258-268` | — | `00 44` + 16-byte `msd_payload` |
| `0x0045` | CLEAR CONFIG | `communication.cpp:439-451` | — | `00 45 00 00` / `FF 45 00 00` |
| `0x0050` | AUTHENTICATE | `encryption.cpp:512-634` | `00` (challenge req) or `client_nonce[16]‖cmac_resp[16]` | see §4.4 — **bypasses the encryption gate** |
| `0x0051` | ENTER DFU | `device_control.cpp:665-703` | — | nRF: sets GPREGRET `0xB1`, disables SoftDevice, jumps to bootloader. ESP32: `esp_restart()` |
| `0x0052` | DEEP SLEEP | `device_control.cpp:705-746` | optional `seconds:2 BE` (0 = no override) | `00 52 00 00` / `FF 52 01` (sleep disabled) / `FF 52 02` (not battery). D-FF latch boards: ACK then hard power-off |
| `0x0070` | DIRECT WRITE START | `display_service.cpp:1801-1839` | `len>=4` → compressed: `decompressedTotal:4 LE` (+ optional inline zlib bytes); `len<4` → uncompressed | `00 70` / `FF 70` |
| `0x0071` | DIRECT WRITE DATA | `display_service.cpp:1932-1988` | raw controller bytes or zlib stream | `00 71` / `FF 71` |
| `0x0072` | DIRECT WRITE END | `display_service.cpp:1990-2112` | `[refresh:1][etag:4 BE]` | `00 72` then `00 73` (refresh OK) / `00 74` (refresh timeout) |
| `0x0073` | LED ACTIVATE | `device_control.cpp:377-412` | `[instance][12-byte pattern]` | `00 73 00 00` / `FF 73 01` (short) / `FF 73 02` (bad instance) |
| `0x0075` | LED STOP | `device_control.cpp:414-423` | `[instance]` | `00 75 00 00` / `FF 75 02` |
| `0x0076` | PARTIAL WRITE START | `display_service.cpp:1841-1930` | `[flags][old_etag:4 BE][new_etag:4 BE][x:2 BE][y:2 BE][w:2 BE][h:2 BE]` (≥17 B) | `FF 76 <err>` on failure; errs `0x01` ETAG_MISMATCH, `0x03` RECT_OOB, `0x04` RECT_ALIGN, `0x05` PARTIAL_FLAGS, `0x06` PARTIAL_STREAM, `0x07` PARTIAL_UNSUPPORTED (`display_service.cpp:66-74`) |
| `0x0077` | BUZZER ACTIVATE | `buzzer_control.cpp:269-349` | `[instance][outer_repeat][pattern_count]{[nsteps]{[freq_idx][dur_units]}×n}×p` | `00 77 00 00` / `FF 77 01..06` |
| `0x0080` | PIPE WRITE START | `display_service.cpp:2276-2432` | see §4.3 | `00 80 ver maxW maxN frame:2LE flags` / `FF 80 err 00` |
| `0x0081` | PIPE WRITE DATA | `display_service.cpp:2434-…` | `[seq:1][payload ≤243]` | 7-byte SACK `00 81 highest_seen mask:4LE` / `FF 81 err hs mask:4` |
| `0x0082` | PIPE WRITE END | `display_service.cpp:2520-2604` | full: `[refresh:1][etag:4 BE]`; partial: `[refresh:1][new_etag:4 BE]` | `00 82` then `00 73`/`00 74`; `FF 82` on incomplete |

### 3.2 Config packets (TLV IDs)

`0x01` system_config · `0x02` manufacturer_data · `0x04` power_option · `0x20` display (×4) · `0x21` led (×4) · `0x23` sensor_data (×4) · `0x24` data_bus (×4) · `0x25` binary_inputs (×4) · `0x26` wifi_config · `0x27` security_config · `0x28` touch_controller (×4) · `0x29` passive_buzzer (×4) · `0x2B` flash_config (×2) · `0x2C` data_extended. Structs at `structs.h:25-398`; parser at `config_parser.cpp:288-610`.

### 3.3 Feature list (granular, for parity checking)

**Image transfer**
1. DIRECT_WRITE uncompressed (0x70/71/72), bufferless, streams straight to controller RAM
2. DIRECT_WRITE zlib-compressed (`decompressedTotal` header + streaming inflate)
3. DIRECT_WRITE auto-complete: finalizes without an END once `bytesWritten >= totalBytes` (`display_service.cpp:1982-1983`)
4. PIPE_WRITE sliding-window (0x80/81/82) with QUIC-style SACK, W≤32, reorder queue
5. PIPE_WRITE compressed (`PIPE_FLAG_COMPRESSED`)
6. PIPE_WRITE partial-region (`PIPE_FLAG_PARTIAL`, `display_service.cpp:2298-2331`)
7. PARTIAL_WRITE region refresh (0x76), two 1bpp controller planes (old + new)
8. ETag mechanism: `displayed_etag` (RTC-persistent on ESP32, `main.h:312`); partial writes require `old_etag == displayed_etag` and a nonzero `new_etag`
9. 15-minute stuck-transfer watchdog for both direct-write and partial
10. BWR/BWY bitplane transfer (2 planes) — `directWriteBitplanes`
11. GRAY4 plane-split streaming (`streamGray4Bytes`, `display_service.cpp:1693-1711`)
12. 5%-granularity image transfer progress meter + per-frame log suppression (`imageWriteLogQuietCmd/Ack/Frame`, `display_service.cpp:1600-1657`)

**Display**
13. 77 bb_epaper panel IDs (`mapEpd`, `display_service.cpp:411-494`) — full table in §5
14. Seeed ED103TC2 10.3″ 1872×1404 via Seeed_GFX/TFT_eSPI + IT8951 TCON: 1bpp (`panel_ic 3000`) and 4bpp 16-gray (`3001`)
15. 8 color schemes: MONO, BWR, BWY, BWRY, BWGBRY (6-color), GRAY4, GRAY8, GRAY16 (`structs.h:86-93`)
16. Refresh modes FULL(0)/FAST(1)/PARTIAL(2)
17. Rotation 0/90/180/270 (`rotation * 90`, `main.cpp:463`)
18. `TRANSMISSION_MODE_CLEAR_ON_BOOT` — suppresses the boot render
19. Boot screen with QR code, logo, chip ID, FW version, encryption-key lines, battery voltage, chip temp, color-scheme name, resolution string, color swatch band
20. Battery-fail retry: a failed boot refresh on battery re-powers the panel and retries once (`display_service.cpp:1367-1375`)

**Power**
21. ESP32 timer deep sleep (`deep_sleep_time_seconds`)
22. Wake-on-button from deep sleep (ext0/ext1/LP-GPIO, chip-dependent) — `wake_button.cpp`
23. `SLEEP_FLAG_BUTTON_WAKE_DISABLE` opt-out (`structs.h:72`)
24. Minimum wake window (`min_wake_time_seconds`, default 120 s) on first boot / button wake
25. Idle-hold quiet window before sleep (`sleep_timeout_ms`, default 10 000 ms)
26. Host-commanded deep sleep with per-cycle duration override (0x0052)
27. EPD panel power session with configurable keep-alive (`screen_timeout_seconds`, 0–30 s, clamped; forced 0 on AXP2101)
28. MOSFET self-holding battery latch (`DEVICE_FLAG_BATTERY_LATCH`) + long-press power-off button
29. 74AHC1G79 D-FF hard power latch (`DEVICE_FLAG_PWR_LATCH_DFF`), D=`pwr_pin_2`, CP=`pwr_pin_3`
30. Per-button configurable long-press power-off (`power_off_flags`, `power_off_hold_sec`, default 3 s)
31. External QSPI flash deep power-down (0xB9) — bit-banged (`main.cpp:785-877`, `powerDownExternalFlashFromConfig` `main.cpp:732-780`)
32. AXP2101 PMIC power up/down (`initAXP2101` / `powerDownAXP2101`)
33. nRF DCDC + `NRF_POWER_MODE_LOWPWR` (`ble_init.cpp:190-191`)
34. `configureDisplayPinsLowPower()` — drives panel control lines LOW before rail cut (`main.cpp:534-560`)

**Sensors / telemetry**
35. SHT40 temp + humidity over I2C (`sensor_sht40.cpp`), CRC-8 verified, 30 s poll TTL, addr auto-probe 0x44/0x45, 3-byte packed MSD encoding
36. BQ27220 fuel gauge (voltage 0x08, SoC 0x2C), 30 s TTL, 1 packed MSD byte with charging bit 0x80
37. BQ25616 charger GPIO (enable + state pins, configurable polarity)
38. ADC battery voltage with enable pin, scaling factor, inverted-enable flag; 30 s cache
39. Chip temperature (ESP32 `temperatureRead()`; nRF `sd_temp_get()` × 0.25)
40. AXP2101 battery/VBUS/SYS voltage + percent
41. I2C bus scanner (`scanI2CDevices`, `display_service.cpp:757-…`)

**Input**
42. Up to 32 buttons (4 instances × 8 pins), ISR-driven, per-pin invert/pullup/pulldown
43. Button press count (4-bit rolling) + state packed into `dynamicreturndata`
44. GT911 capacitive touch (up to 4 controllers), IRQ + polled, addr auto-probe 0x5D/0x14, LE/BE register-order probe, X/Y invert + XY swap flags, I2C-failure backoff and auto-disable after 5 failures, suspend/resume around EPD refresh
45. Touch release event encoding (low nibble 6, last x/y retained)

**Output**
46. RGB/RY LED with per-channel invert, software 3-bit-per-channel PWM (`flashLed`)
47. LED flash pattern engine: 3 nested color loops with per-loop repeat counts, inter-loop delays, group repeats (255 = infinite) — non-blocking state machine (`device_control.cpp:125-375`)
48. Passive buzzer, up to 4 instances, hardware PWM (nRF `HwPWM3` / ESP32 LEDC), quarter-tone 256-entry frequency table anchored at A-1=13.75 Hz, octave-folding of out-of-range notes, non-blocking playback, 30 s total cap, preemption, power-off two-beep alert

**Connectivity**
49. BLE peripheral, service+char UUID `0x2446`
50. BLE MSD advertising (16 bytes) with rolling sequence nibble, reboot flag, battery, temp, sensor + touch + button data
51. nRF: 2M PHY + 251-octet DLE request, ATT MTU up to 247, advertising-interval boost on button press, link diagnostics
52. ESP32: MTU 512, CCCD-aware notify gating
53. nRF BLE DFU service (only when encryption disabled)
54. WiFi STA (ESP32) — non-blocking connect, auto-reconnect, 15 dBm TX
55. TCP LAN server on port 2446 (default) with 2-byte LE length framing, 8 KB RX buffer
56. mDNS: `OD<chipid>.local`, service `_opendisplay._tcp`, TXT record `msd=<28 hex>` refreshed on MSD change (≥400 ms throttle)

**Security**
57. AES-128-CCM encryption of commands and responses (mbedTLS on ESP32, CC310 hardware on nRF)
58. AES-CMAC challenge-response authentication (0x0050)
59. Session key derivation: CMAC(master, server_nonce‖client_nonce‖device_id) → AES-ECB
60. Session ID derivation, 64-entry replay window, ±32 counter window, integrity-failure counter (3 → session cleared)
61. Session timeout (`session_timeout_seconds`, 0 = none), auth rate-limit (10 attempts/60 s), server-nonce 30 s expiry
62. `SECURITY_FLAG_REWRITE_ALLOWED` — unauthenticated config write allowed, but only after `secureEraseConfig()`
63. `SECURITY_FLAG_SHOW_KEY_ON_SCREEN` — key on boot screen + in QR payload
64. Hardware reset pin (configurable polarity/pullup/pulldown) → `secureEraseConfig()` + reboot
65. Secure erase: zero-overwrite of `/config.bin` before removal

**Provisioning / build**
66. LittleFS config storage `/config.bin` with `0xDEADBEEF` magic + CRC-32
67. Factory-embedded config (`OPENDISPLAY_FACTORY_CONFIG_HEX`) — fallback only, never overrides a valid stored config
68. `OPENDISPLAY_FACTORY_CLEAR_CONFIG=1` build → erase on boot
69. `DEVICE_FLAG_XIAOINIT` / `DEVICE_FLAG_WS_PP_INIT` board hooks
70. Git SHA + version baked into the binary and reported via 0x0043
71. nRF UF2 post-build script (`scripts/nrf_uf2_post.py`)

---

## 4. BLE protocol details

### 4.1 GATT

- **Service UUID:** `0x2446` (nRF `main.h:375`) / `00002446-0000-1000-8000-00805F9B34FB` (ESP32, `ble_init.cpp:283`)
- **Characteristic UUID:** same value (`main.h:376`, `ble_init.cpp:290`) — a **single** characteristic used for both directions.
- Properties: nRF `BLEWrite | BLEWriteWithoutResponse | BLENotify`, max 512 B (`main.h:376`). ESP32: `READ | NOTIFY | WRITE | WRITE_NR` (`ble_init.cpp:291-297`) + CCCD `0x2902` (Bluedroid, `ble_init.cpp:304-305`). `pRxCharacteristic = pTxCharacteristic` (`ble_init.cpp:309`).
- Device name: `"OD" + getChipIdHex()` — chip ID is the low 3 bytes of `NRF_FICR->DEVICEID[1]` (nRF) or bits 24-47 of `ESP.getEfuseMac()` (ESP32), 6 uppercase hex chars (`encryption.cpp:725-752`).
- ESP32 MTU 512 (`ble_init.cpp:274`); nRF requests 2M PHY + 251-octet DLE at connect (`ble_init.cpp:123-144`).

### 4.2 Advertising / Manufacturer Specific Data (16 bytes)

`updatemsdata()`, `display_service.cpp:1475-…`:
```
[0..1]  0x2446 (company ID, little-endian memcpy)
[2..12] dynamicreturndata[11]   — buttons / touch / SHT40 / BQ27220, per-config byte offsets
[13]    temperatureByte  = clamp((chipTemp + 40) * 2, 0..255)
[14]    batteryVoltageLow = (voltage_10mv & 0xFF)      [voltage_10mv = mV/10, capped 511]
[15]    statusByte = (voltage_10mv >> 8 & 0x01)
                   | (rebootFlag & 1) << 1
                   | (connectionRequested & 1) << 2
                   | (mloopcounter & 0x0F) << 4        [rolling sequence nibble]
```
Both platforms skip the advertising rewrite when the payload is unchanged (`display_service.cpp:1506-1512` nRF, `:1524-1529` ESP32) and just bump `mloopcounter`. nRF tears down and restarts advertising on change; ESP32 stops/rebuilds `BLEAdvertisementData` when disconnected, or updates in place when connected. `mloopcounter` is `RTC_DATA_ATTR` so advertisements stay distinguishable across sleep/wake (`main.h:129`).

**Sub-field encodings inside `dynamicreturndata`:**
- Button byte (`device_control.cpp:445-447`): `(button_id & 0x07) | ((press_count & 0x0F) << 3) | ((state & 1) << 7)`
- Touch 5-byte block (`touch_input.cpp:699-714`): `byte0 = (contacts & 0x0F) | (track_id << 4)` where contacts 1–5 = down, **6 = released** (last x/y retained), 0 = never touched; `bytes 1-4` = x:2 LE, y:2 LE
- SHT40 3-byte block (`sensor_sht40.cpp:191-212`): `v = rh_deci | (tu << 10)` LE, `tu = temp_deci + 400`; `FF FF FF` = invalid
- BQ27220 1 byte (`sensor_bq27220.cpp:183-187`): `soc & 0x7F | 0x80 if charging`; `0xFF` = invalid

### 4.3 The "pipe" write protocol (0x0080–0x0082)

Fully documented in `docs/pipe-write-protocol.md`. Constants at `structs.h:116-136`:

| Constant | Default | `PIPE_SMALL_DRAM_WINDOW` (env:esp32-N4 only) |
|---|---|---|
| `PIPE_VERSION` | `0x01` | `0x01` |
| `PIPE_MAX_W` | 32 | 16 |
| `PIPE_MAX_N` | 32 | 16 |
| `PIPE_MAX_FRAME` | 244 (HA ATT write ceiling) | 244 |
| `PIPE_ACK_MASK_BITS` | 32 | 32 |
| `PIPE_REORDER_SLOTS` | 33 | 17 |
| `PIPE_REORDER_SLOT_SIZE` | 248 | 248 |

**START (0x0080) request**, post-opcode, little-endian (`display_service.cpp:2280-2310`):
```
[0]     ver         (must == 0x01)
[1]     flags       bit0 = COMPRESSED (zlib), bit1 = PARTIAL
[2]     req_w
[3]     req_n       (ack every N)
[4..5]  client_max_frame  (LE)
[6..9]  total_size        (LE, decompressed panel byte total)
--- if PARTIAL, 12-byte LE extension (len 22, not 10) ---
[10..13] old_etag (LE!)   [14..15] x   [16..17] y   [18..19] w   [20..21] h
```
Note the **endianness trap**: the pipe-partial extension is little-endian, but the legacy 0x76 partial header is **big-endian** (`display_service.cpp:1852-1857`).

**Negotiation (min-rule)** `display_service.cpp:2349-2356`: `W_eff = clamp(min(req_w, PIPE_MAX_W), 1..)` (further capped to 32 when authenticated, mask width); `N_eff = min(clamp(min(req_n, PIPE_MAX_N),1..), W_eff)`; `frame_eff = min(client_max_frame, 244)`.

**START response** (8 B, `display_service.cpp:2405-2408`) — returns device **maxima**, not effective values; the client applies the same min-rule:
```
00 80 | VER | PIPE_MAX_W | PIPE_MAX_N | FRAME_LO FRAME_HI | flags(bit0=selective-repeat, bit1=partial accepted)
```
It is sent **before** panel bring-up (`display_service.cpp:2394-2404`) so slow panel init can't blow the client's probe timeout.

**START NACK** `FF 80 <err> 00`: `0x01` bad len/version · `0x02` unknown flag bit · `0x03` size/geometry mismatch · `0x05` ETAG_MISMATCH (partial) · `0x06` PARTIAL_UNSUPPORTED (bpp≠1 or Seeed) · `0x07` RECT invalid.

**DATA (0x0081):** `[seq:1][payload ≤243]`. `fwd = (uint8_t)(seq - expected_seq)`, `back = (uint8_t)(expected_seq - seq)`.
- `fwd == 0` → stream to controller, `expected_seq++`, then **drain** the contiguous run of queued successors; cadence SACK every `N_eff` accepts.
- `0 < fwd < W` → memcpy into `pipeReorder[seq % PIPE_REORDER_SLOTS]`; if this **opens** a gap, SACK **immediately** (fast retransmit); further OOO arrivals rate-limited to one SACK per `N_eff`.
- `back <= W` → duplicate/stale, discard + rate-limited SACK.
- Outside window both ways → NACK `0x04`.
- Indexing by `seq % 33` is collision-free because a live window spans ≤W < 33 seqs.

**SACK (7 bytes):** `00 81 | highest_seen | mask[0..3]`. Mask is a **32-bit little-endian** bitmask; bit *i* (LSB first) = "chunk `highest_seen − 1 − i` was received". `highest_seen` is implicitly acked. "Received" = accepted in the in-order prefix **or** currently in the reorder queue.

**DATA NACK (8 bytes):** `FF 81 <err> highest_seen mask[0..3]`. Every 0x81 NACK is **fatal**: sets `pipeState.error`, runs `cleanupDirectWriteState(true)`, and then **silently discards** all further 0x0081 until the next START or a disconnect. Errors: `0x02` zlib consume fail · `0x03` raw consume fail / over-size / reorder overflow · `0x04` out-of-window.

**END (0x0082):** full-frame `[refresh_mode:1][etag:4 BE]`; partial `[refresh:1][new_etag:4 BE]` where refresh 0=FULL, 1=FAST, 2/absent=PARTIAL. Flow (`display_service.cpp:2520-2604`): tail-flush SACK → completeness check (`queued_count > 0`, or short byte count for uncompressed) → `FF 82` if incomplete, else `00 82` + refresh + `00 73`/`00 74`. **Auto-complete (uncompressed full-frame only):** when an in-order accept pushes `bytesWritten >= total_size`, finalize without waiting for END (`display_service.cpp:2177-2182`). **Partial transfers never auto-complete.**

### 4.4 Encryption / auth on the wire

**Encrypted command frame** (`communication.cpp:544-580`):
```
[0..1]  opcode (BE, plaintext — used as CCM associated data)
[2..17] nonce_full[16]
[18..]  ciphertext        (inner: [payload_len:1][plaintext…])
[last 12] auth_tag
```
- Cipher: **AES-128-CCM**, 13-byte nonce = `nonce_full[3..15]`, AD = the 2 opcode bytes, tag = 12 bytes (`encryption.cpp:652-670`).
- Inner length prefix is a single byte → responses must stay ≤255 B.
- `0x0050` (auth) and `0x0043` (fw version) are **exempt** from the gate (`communication.cpp:517-527`).
- Unauthenticated command when encryption is on → `{0x00, opcode_lo, 0xFE}` unencrypted. Decrypt failure → `{0x00, opcode_lo, 0xFF}`.
- Response encryption skip heuristic (`communication.cpp:175-209`): skip for `0x0050`/`0x0043` and when `response[2] == 0xFE/0xFF` — **explicitly excluding** the 7-byte `00 81` SACK shape, because a `highest_seen` of 0xFE/0xFF would otherwise trip it.

**Auth (0x0050)** (`encryption.cpp:512-634`):
1. Client sends `00` → device returns `00 50 00 | server_nonce[16] | device_id[4]` (device_id = 4 bytes of chip ID, `encryption.cpp:45-59`). Server nonce expires in 30 s.
2. Client sends `client_nonce[16] ‖ CMAC(master_key, server_nonce‖client_nonce‖device_id)[16]` (32 B).
3. Device recomputes with `aes_cmac`, **constant-time compares**, derives `session_key = AES-ECB(master_key, CMAC(master_key, …))` (`encryption.cpp:61-93`), derives `session_id`, and returns `00 50 00 | CMAC(session_key, server_nonce‖client_nonce‖device_id)[16]`.
- Status codes: `0x01` wrong key · `0x03` encryption not enabled · `0x04` rate-limited (10 attempts / 60 s) · `0xFF` generic failure.
- Replay: 64-entry window, ±32 counter tolerance (`encryption.cpp:113-156`). 3 integrity failures → session cleared. Note `communication.cpp:631-634`: the replay counter advances at *decrypt* time for **every** 0x0081 frame including queued/discarded ones, so drops/dupes never desync it (in-flight ≤ W ≤ 32 ≤ the ±32 window).

### 4.5 `device_flags` bits (`main.h:68-72`, `SystemConfig.device_flags`)

| Bit | Name | Meaning |
|---|---|---|
| 0 | `DEVICE_FLAG_PWR_PIN` | Device has an external power-management pin |
| 1 | `DEVICE_FLAG_XIAOINIT` | Call `xiaoinit()` after config load (**nRF52840 only**) — powers down XIAO's external QSPI flash, drives GPIO 13 LOW (`main.cpp:689-696`) |
| 2 | `DEVICE_FLAG_WS_PP_INIT` | Call `ws_pp_init()` — Waveshare Photo Printer pin setup (`main.cpp:698-729`) |
| 3 | `DEVICE_FLAG_BATTERY_LATCH` | Self-holding MOSFET battery latch on `pwr_pin_2`; optional active-low long-press shutdown button on `pwr_pin_3` |
| 4 | `DEVICE_FLAG_PWR_LATCH_DFF` | 74AHC1G79 D-flip-flop latch: `pwr_pin_2` = D (PWR_HOLD), `pwr_pin_3` = CP (PWR_LOCK); released via command 0x0052 |

Other bitfields: `communication_modes` — bit0 BLE, bit1 OEPL, bit2 WIFI (`main.h:63-65`). `transmission_modes` — bit0 ZIPXL, bit1 ZIP, bit2 G5, bit3 DIRECT_WRITE, bit4 PIPE_WRITE, bit7 CLEAR_ON_BOOT (`structs.h:96-101`).

---

## 5. Display pipeline

### 5.1 Bytes → panel

```
BLE/LAN frame → [ESP32: command ring] → imageDataWritten()
   → handleDirectWriteData / handlePipeWriteData
      → compressed?  od_zlib_stream_push() + od_zlib_stream_poll()
                        into decompressionChunk[2048]      (main.h:124, display_service.h:7)
      → gray4/bitplanes?  streamGray4Bytes()               (display_service.cpp:1693-1711)
                        splits at planeBytes boundary; bbepSetAddrWindow + bbepStartWrite(PLANE_0/PLANE_1)
      → else            bbepWriteData(&bbep, data, len)     [straight to controller RAM]
   → END → bbepRefresh(&bbep, mode) → waitforrefresh(60)
   → cleanupDirectWriteState(false) → epdSessionRelease(true) → PWR_WARM or PWR_OFF
```
**There is no framebuffer on the bb_epaper path** — bytes stream straight to controller RAM as they arrive. The **Seeed/IT8951 path is the exception**: `seeed_gfx_direct_write_chunk()` memcpys into the TFT_eSPI sprite buffer (`display_seeed_gfx.cpp:166-177`).

### 5.2 Decompression

- `lib/uzlib/src/od_zlib_stream.c` — a custom streaming inflate. **Window bits default to 9** (512 B), configurable 9–15 via `OPENDISPLAY_ZLIB_WINDOW_BITS` (`lib/uzlib/src/uzlib.h:21-26`). A stream whose header declares a larger window is **rejected**: `"zlib stream window exceeds firmware limit"` (`od_zlib_stream.c:641-644`).
- `OPENDISPLAY_ZLIB_USE_HEAP_WINDOW`: **0 on nRF** (static storage, deterministic allocation — `platformio.ini:26`), **1 on all ESP32 envs**.
- Existing targets pin 32 KB windows (`-DOPENDISPLAY_ZLIB_WINDOW_BITS=15`) only for legacy-client back-compat; it is commented out in every current env (`platformio.ini:22-24`).
- Output chunk: `OPENDISPLAY_DECOMPRESSION_CHUNK_SIZE = 2048` (`display_service.h:7`).

### 5.3 Panel map (`mapEpd`, `display_service.cpp:411-494`) — all 77 IDs

`0x0000` UNDEFINED · `01` EP42_400x300 · `02` EP42B_400x300 · `03` EP213_122x250 · `04` EP213B_122x250 · `05` EP293_128x296 · `06` EP294_128x296 · `07` EP295_128x296 · `08` EP295_128x296_4GRAY · `09` EP266_152x296 · `0A` EP102_80x128 · `0B` EP27B_176x264 · `0C` EP29R_128x296 · `0D` EP122_192x176 · `0E` EP154R_152x152 · `0F` EP42R_400x300 · `10` EP42R2_400x300 · `11` EP37_240x416 · `12` EP37B_240x416 · `13` EP213_104x212 · `14` EP75_800x480 · `15` EP75_800x480_4GRAY · `16` EP75_800x480_4GRAY_V2 · `17` EP29_128x296 · `18` EP29_128x296_4GRAY · `19` EP213R_122x250 · `1A` EP154_200x200 · `1B` EP154B_200x200 · `1C` EP266YR_184x360 · `1D` EP29YR_128x296 · `1E` EP29YR_168x384 · `1F` EP583_648x480 · `20` EP296_128x296 · `21` EP26R_152x296 · `22` EP73_800x480 · `23` EP73_SPECTRA_800x480 · `24` EP74R_640x384 · `25` EP583R_600x448 · `26` EP75R_800x480 · `27` EP426_800x480 · `28` EP426_800x480_4GRAY · `29` EP29R2_128x296 · `2A` EP41_640x400 · `2B` EP81_SPECTRA_1024x576 · `2C` EP7_960x640 · `2D` EP213R2_122x250 · `2E` EP29Z_128x296 · `2F` EP29Z_128x296_4GRAY · `30` EP213Z_122x250 · `31` EP213Z_122x250_4GRAY · `32` EP154Z_152x152 · `33` EP579_792x272 · `34` EP213YR_122x250 · `35` EP37YR_240x416 · `36` EP35YR_184x384 · `37` EP397YR_800x480 · `38` EP154YR_200x200 · `39` EP266YR2_184x360 · `3A` EP42YR_400x300 · `3B` EP75_800x480_GEN2 · `3C` EP75_800x480_4GRAY_GEN2 · `3D` EP215YR_160x296 · `3E` EP1085_1360x480 · `3F` EP31_240x320 · `40` EP75YR_800x480 · `41` **UNDEFINED** · `42` **UNDEFINED** (13.3″ EP133 not in bb_epaper 2.1.9) · `43` EP154_200x200_4GRAY · `44` EP42B_400x300_4GRAY · `45` EP397_800x480 · `46` EP397_800x480_4GRAY · `47` EP368_792x528 · `48` EP368_792x528_4GRAY · `49` EP213ZZ_122x250 · `4A` EP40_SPECTRA_400x600 · `4B` EP27_176x264 · `4C` EP27_176x264_4GRAY. Default → UNDEFINED.

Plus, out-of-band from bb_epaper: `3000` = `PANEL_IC_SEEED_ED103TC2_1872X1404` (1bpp), `3001` = `..._4GRAY` (4bpp) (`structs.h:79-80`). Decimal 3000–3999 is reserved for the Seeed_GFX / OpenDisplay runtime epaper family.

### 5.4 Bits per pixel / planes

`getBitsPerPixel()` (`display_service.cpp:1404-1415`): Seeed 3001 → 4; BWGBRY → 4; BWRY → 2; GRAY4 → 2; else **1**.
`getplane()` (`display_service.cpp:1396-1402`): MONO/GRAY16/BWR/BWY → `PLANE_0`; GRAY4 and everything else → `PLANE_1`.
Refresh constants: `REFRESH_FULL 0`, `REFRESH_FAST 1`, `REFRESH_PARTIAL 2`, `REFRESH_PARTIAL2 3` (bb_epaper `bb_epaper.h:90-93`).

Geometry (`directWriteComputeGeometry`, `display_service.cpp:1746-1771`): rows are **padded to whole bytes** — 4bpp → `ceil(w/2)*h`; 2bpp → `ceil(w/4)*h`; 1bpp → `ceil(w/8)*h`. Bitplanes (BWR/BWY) and GRAY4 → `2 × ceil(w/8) × h` (two concatenated 1bpp planes). The comment at `display_service.cpp:1755-1758` is a warning worth heeding: sizing *flat* instead of row-padded under-counts on width-not-divisible-by-8 panels (e.g. 122-wide EP213) and auto-completes before the bottom rows are written.

**Panels that skip `bbepSetAddrWindow` on partial** (`display_service.cpp:2613-2617`): EP397_800x480(_4GRAY), EP426_800x480(_4GRAY).

---

## 6. Per-platform differences

### 6.1 `platformio.ini` environments

| Env | Board | Flash/PSRAM | Key flags |
|---|---|---|---|
| `nrf52840custom` | `xiaoblesense_adafruit` + custom variant | — | `TARGET_NRF`, `OPENDISPLAY_ZLIB_USE_HEAP_WINDOW=0`, `lib_ignore=Seeed_GFX`, post-script `nrf_uf2_post.py` |
| `esp32-s3-N16R8` | esp32-s3-devkitc-1 | 16 MB / 8 MB OPI PSRAM | `TARGET_ESP32`, `BOARD_HAS_PSRAM`, `OPENDISPLAY_SEEED_GFX`, USB-CDC on boot |
| `esp32-s3-N8R8` | " | 8 MB / PSRAM | same |
| `esp32-s3-N32R8` | " | 32 MB / PSRAM | same |
| `esp32-s3-N16R8-extuart` | " | 16 MB / PSRAM | + `OPENDISPLAY_LOG_UART` (RX 44 / TX 43) |
| `esp32-s3-N32R8-extuart` | " (Seeed reTerminal Sticky) | 32 MB / 8 MB | `ARDUINO_USB_MODE=0`, `OPENDISPLAY_LOG_UART`, `lib_ignore=Seeed_GFX` (bb_epaper path) |
| `esp32-c3-N4` | esp32-c3-devkitm-1 | 4 MB, no PSRAM | `huge_app.csv`, `lib_ignore=Seeed_GFX` |
| `esp32-c3-N16` | esp32-c3-devkitm-1 | 16 MB, no PSRAM | DIO flash mode (frees GPIO12/13 for a battery-latch MOSFET) |
| `esp32-c6-N4` | esp32-c6-devkitm-1 | 4 MB | `lib_ignore=Seeed_GFX` |
| `esp32-N4` | esp32dev (classic) | 320 KB RAM | **`PIPE_SMALL_DRAM_WINDOW`** — W=16/17 slots; the full 33×248 queue (~8.3 KB .bss) overflows `dram0_0_seg` by ~672 B at link |

All ESP32 envs set `CONFIG_FREERTOS_WATCHDOG_TIMEOUT_S=120` and `OPENDISPLAY_ZLIB_USE_HEAP_WINDOW=1`. `extra_scripts = pre:scripts/factory_config_gen.py` is global (`platformio.ini:8-9`).

### 6.2 What is ESP32-only

- Deep sleep, wake-on-button, RTC memory, `enterDeepSleep()`, `pollActivity()`, `minWakeHold` — all `#ifdef TARGET_ESP32` (`main.cpp:160-260, 447-531`)
- Power latch (MOSFET + D-FF) — `power_latch.cpp:3` (`#if defined(TARGET_ESP32)`), with no-op stubs at `:193-211`
- `wake_button.cpp` — same pattern, stubs at `:263-272`
- WiFi + TCP LAN server + mDNS — the entire `wifi_service.cpp` is wrapped in `#ifdef TARGET_ESP32` (`:1`, `:259`)
- BLE command ring + response ring (`main.h:379-414`, `esp32_ble_callbacks.h`)
- PSRAM (`BOARD_HAS_PSRAM` on S3 envs only)
- Seeed_GFX / IT8951 path (`OPENDISPLAY_SEEED_GFX`, S3 only) — `display_seeed_gfx.cpp:1`
- `OPENDISPLAY_LOG_UART` external UART logging (`main.cpp:13-22`)
- `INPUT_PULLDOWN` for buttons (`device_control.cpp:605-607` — nRF only gets pullup)

### 6.3 What is nRF-only

- BLE DFU service via `BLEDfu` (`main.h:374`, `ble_init.cpp:177-182`) — ESP32 0x0051 just reboots
- `enterDFUMode()` real bootloader jump: GPREGRET `0xB1`, `sd_softdevice_disable()`, NVIC clear, `bootloader_util_app_start(NRF_UICR->NRFFW[0])` (`device_control.cpp:668-696`)
- SoftDevice temperature `sd_temp_get()` (`display_service.cpp:1845-1848`)
- CC310 hardware crypto (`CRYS_AESCCM`, `nRFCrypto.Random`) vs mbedTLS on ESP32 (`encryption.cpp:7-13, 440-508`)
- `powerDownExternalFlash()` (bit-banged QSPI 0xB9) — explicitly "not implemented for ESP32" (`main.cpp:872-874`)
- `powerDownExternalFlashFromConfig()` (uses the 0x2B flash_config packet) — no-op on ESP32 (`main.cpp:782`)
- Advertising interval boost on button press (`ble_init.cpp:46-84`)
- `nrfVbusPresent()` gating of the boot LED flash + the boot-refresh battery retry (`display_service.cpp:120-126`)
- `Adafruit_LittleFS` / `InternalFS` vs `LittleFS` (`config_parser.cpp:11-16`)

---

## 7. Power management & sleep

### 7.1 EPD panel power session (`display_service.cpp:160-317`)

Three states (`display_service.h:15`): `PWR_OFF = 0` (BSS-zero after boot/wake == rail off), `PWR_WARM = 1`, `PWR_ACTIVE = 2`.

- `pwrmgm(bool)` (`main.cpp:562-662`) is the **sole rail actuator** and owns OFF↔ACTIVE. Idempotency guard keyed on `pwrmgmState` (`main.cpp:572-573`). ON: AXP2101 init if configured → `pwr_pin` HIGH → **`delay(800)`** → panel control pins → `Wire` restore. OFF: `SPI.end()` → `Wire.end()` (only if data_bus[0] isn't a configured I2C bus) → `configureDisplayPinsLowPower()` → `pwr_pin` LOW.
- `epdSessionAcquire(partialInit)` (`:241-279`) owns WARM→ACTIVE. **COLD**: `pwrmgm(true)`, `bbepInitIO(dc,rst,busy,cs,mosi,sck, 8 MHz)`, `bbepWakeUp`, `bbepSendCMDSequence(pInitPart or pInitFull)`. **WARM re-acquire**: skips the ~900 ms rail bring-up and `bbepInitIO`, but still does `bbepWakeUp` + full re-init sequence (Phase 1; Phase 2a is planned to skip these).
- `epdSessionRelease(refreshSuccess)` (`:281-296`): on success **and** window > 0 → `PWR_WARM` + `pwrmgmOffDeadlineMs = millis() + window`, controller stays awake, rail/SPI stay up. Otherwise → power fully down.
- `epdSessionForceOff()` (`:298-302`) / `epdSessionForceOffLocked()` (`:221-239`): `bbepSleep(&bbep,1)` + `delay(50)` (or `seeed_gfx_direct_sleep()` + `seeed_gfx_mark_hw_deinitialized()`), then `pwrmgm(false)`.
- `epdSessionTick()` (`:304-313`): fast pre-check `pwrmgmState != PWR_WARM` → return; then **try-lock** (never blocks); re-check under the lock; expire.
- **Keep-alive window** (`epdKeepAliveWindowMs`, `:186-197`): `min(power_option.screen_timeout_seconds, EPD_KEEPALIVE_MAX_S=30) × 1000`. **0 is the default** (and what old blobs read) → panel powers off immediately after refresh. **Forced to 0 on any AXP2101 board**, regardless of config, with a one-shot log line. Live-disable: a config write that sets it to 0 while the panel is WARM triggers `epdSessionForceOff()` immediately (`communication.cpp:36-38`).
- **ACTIVE-only-teardown invariant** (`device_control.cpp:101-108`, `esp32_ble_callbacks.h:61-65`): a WARM panel **survives a BLE disconnect** and keeps its keep-alive window — a reconnect within the window pays only a warm re-acquire. Only a disconnect *mid-transfer* (still ACTIVE) tears the panel down.

### 7.2 Power latch (`power_latch.cpp`)

- **MOSFET latch** (`DEVICE_FLAG_BATTERY_LATCH`, `pwr_pin_2` = latch, `pwr_pin_3` = button): `powerLatchBegin()` releases the GPIO hold; `powerButtonPoll()` (called from `processButtonEvents`, `device_control.cpp:426`) requires a release-since-boot, then a 3 s hold (`POWER_OFF_HOLD_MS`, `:21`) → `powerOff()` → **unbounded busy-wait for button release** (`:86-90`) → latch LOW + `gpio_hold_en` + `esp_sleep_config_gpio_isolate()` + arm LP-GPIO wake on the button → `esp_deep_sleep_start()`.
- **D-FF latch** (`DEVICE_FLAG_PWR_LATCH_DFF`, `pwr_pin_2` = D, `pwr_pin_3` = CP): `dffLatchEngage()` at boot (D=HIGH, clock pulse); `dffLatchRelease()` (D=LOW, clock pulse, hold, isolate) is a **hard power cut** with no timer — command 0x0052 on such a board ACKs and then cuts power, ignoring any duration payload (`device_control.cpp:717-725`).
- `powerLatchHoldForSleep()` (`:147-168`): before `esp_deep_sleep_start()`, drives the latch/D pin HIGH and `gpio_hold_en`s it so the rail survives the sleep. Note `gpio_deep_sleep_hold_en()` is skipped on C6 (`#if !defined(CONFIG_IDF_TARGET_ESP32C6)`).
- **Per-button power-off**: `pollConfiguredPowerOffButtons()` (`device_control.cpp:50-81`) — any button with `power_off_flags` bit set, held for `power_off_hold_sec` (default 3 s), fires `passiveBuzzerPowerOffAlert()` then `powerLatchTriggerOff()`.

### 7.3 Wake sources (`wake_button.cpp:82-231`)

- Opt-out: `SLEEP_FLAG_BUTTON_WAKE_DISABLE` (bit 0 of `sleep_flags`).
- Candidates = every initialized button + the MOSFET-latch power button. **Excluded:** `pwr_pin_2` (it's an output), `pwr_pin_3` on D-FF boards (arming the CP clock could latch power off), non-wake-capable pins, and any pin **already at its wake level at sleep entry** (would wake instantly and ping-pong).
- **C3/C6** (`SOC_GPIO_SUPPORT_DEEPSLEEP_WAKEUP`): `esp_deep_sleep_enable_gpio_wakeup()` per polarity mask; IDF auto-enables the opposite pull.
- **S2/S3** (`SOC_PM_SUPPORT_EXT1_WAKEUP`): only **one** ext1 call is possible, so the **larger polarity group takes ext1** and the other group's lowest pin takes ext0; the rest are warned as timer-only.
- **Classic ESP32**: ext1 has no ANY_LOW mode → HIGH group on ext1 ANY_HIGH, one LOW pin on ext0.
- `retainWakePull()` re-asserts configured pulls through the **RTC IO registers** (digital-domain `pinMode` pulls do not survive RTC_PERIPH power-down). A pad with no internal pull is warned about explicitly.
- `s_ext0WakePin` is `RTC_DATA_ATTR` because **there is no ext0 status register** — it's the only way `detectButtonWake()` can name the pin.

---

## 8. Things a fresh port would likely miss

1. **nRF runs command handlers on the BLE callback task, not `loop()`.** `imageDataWritten` *is* the Bluefruit write callback (`ble_init.cpp:167`). Everything the handler touches — the display SPI, `sendResponse`, `partialCtx`, `pipeState` — is therefore cross-task with respect to `loop()`. This is the sole reason `pwrmgmLock` exists.
2. **`pwrmgmLockTake()` must yield, not spin** (`display_service.cpp:204-211`). The Bluefruit callback task outranks the loop task; a bare busy-spin starves the lower-priority holder forever on a single core. `delay(1)` is `vTaskDelay`, which blocks the spinner.
3. **`epdSessionTick()` try-locks and skips.** If it blocked, it could cut the rail mid-init.
4. **ESP32 RTC memory does NOT survive panic/WDT/SW/brownout resets** — only a deep-sleep wake. A hidden mid-cycle reset lands on the NORMAL BOOT path with `deep_sleep_count == 0`, indistinguishable from first boot (`main.cpp:86-90`, captured on hardware in `docs/FINDINGS_DEEP_SLEEP_WAKE_BOOT_SCREEN_2026-07-07.md`).
5. **`flushResponseQueueToBle()` must run BETWEEN commands in the drain, not just after it** (`main.cpp:329-331`). At small `ack_every`, a 33-command drain emits ~32 SACKs; the 10-slot response ring **drops the NEWEST entry** when full, leaving only stale ACKs and collapsing pipe throughput.
6. **DFU service must be registered LAST on nRF** (`ble_init.cpp:171-176`) — GATT handles are assigned in `begin()` order, and the DFU service is conditional on encryption. Registering it first would shift `imageCharacteristic`'s CCCD handle when encryption flips, invalidating cached client handles.
7. **The pipe SACK must stay encrypted.** A `highest_seen` of 0xFE/0xFF would otherwise trip the "unencrypted status response" heuristic; the check is explicitly scoped to exclude the 7-byte `00 81` shape (`communication.cpp:173-184`).
8. **The replay counter advances at decrypt time for every 0x0081 frame, including ones the handler queues or discards** (`communication.cpp:631-634`) — so drops/dupes never desync it.
9. **PIPE START must respond BEFORE panel bring-up** (`display_service.cpp:2394-2404`). Spectra/ACeP-class init takes seconds; a client's probe timeout would fire first.
10. **Panel power-down in `enterDeepSleep()` must sit below every early return** (`main.cpp:492-500`). On mains it never runs (so the keep-alive tick expires the panel normally); on battery it means effective keep-alive = `min(configured window, idle-hold)`.
11. **`armButtonWakeSources()` must run after the timer arm but before `powerLatchHoldForSleep()`** (`main.cpp:520-528`) — otherwise the latch's `gpio_hold_en()` disturbs freshly configured RTC pulls.
12. **A wake pin already at its wake level at sleep entry must be skipped** (`wake_button.cpp:137-141`) — arming it produces an instant wake / sleep ping-pong.
13. **RTC pulls must be re-asserted through `rtc_gpio_*`** on ext0/ext1 pads (`wake_button.cpp:58-69`) — `pinMode` pulls are in the digital domain and do not survive RTC_PERIPH power-down.
14. **Warm re-acquire ≠ cold acquire for the IT8951 TCON.** `seeed_gfx_hw_initialized` is deliberately **NOT** `RTC_DATA_ATTR` (`display_seeed_gfx.cpp:77-80`): it is false after every reset including a deep-sleep wake, which is exactly when the power-cycled TCON needs a full `begin()`/`hostTconInit()` (VCOM, dimensions, I80 packed-pixel mode) rather than a cheap `wake()`. `seeed_gfx_mark_hw_deinitialized()` must be called on every rail cut, or the next refresh comes up garbled.
15. **The boot screen is not redrawn on a deep-sleep wake** (`main.cpp:103-113`) — the panel keeps its image, and skipping `initDisplay()` is the wake path's main energy saving.
16. **`rebootFlag` is armed on the boot-screen path, not at declaration** (`main.cpp:104-108`) — so a wake never advertises as a reboot, but every real reset does.
17. **`dynamicreturndata` byte offsets are config-driven and can collide.** Buttons use `button_data_byte_index`, touch uses a 5-byte block at `touch_data_start_byte`, SHT40 a 3-byte block at `msd_data_start_byte` (default 7), BQ27220 one byte. Nothing validates non-overlap.
18. **`pollActivity()` samples `bleRestartAdvertisingPending`** (`main.cpp:217`) because it is the **only** trace of a connect+drop that lands entirely between two loop passes — `connCount` reads 0 on both sides of such a blip.
19. **`imageCharacteristic.notify()` retry on nRF is bounded and only on backpressure** (`communication.cpp:242-249`) — replaces an unconditional `delay(20)`, which cost ~20 ms/chunk on the legacy path.
20. **The config wire-format CRC-16 is effectively dead code** for well-formed packets (guard `if (offset < configLen - 2)` at `config_parser.cpp:611` is false when the parse consumed the whole stream). Integrity relies entirely on the storage-layer CRC-32.
21. **The TLV parser ignores the per-packet length field** (`config_parser.cpp:290-291`: `offset++` skips it) — lengths are implied by `sizeof(struct)`. A struct-size mismatch between toolbox and firmware silently desyncs the entire parse.
22. **An unknown packet ID aborts the rest of the parse but still marks the config loaded** (`config_parser.cpp:605-608`).
23. **The factory embed is a fallback, not an override** (`config_parser.cpp:836`) — a valid stored `/config.bin` always wins.
24. **`PIPE_SMALL_DRAM_WINDOW`** exists solely because classic-ESP32's static DRAM can't hold the 33-slot queue. Any RAM-constrained nRF54 variant will hit the same wall.
25. **`waitforrefresh`'s timeout is really ~2× its nominal value** because `bbepIsBusy()` internally does `delay(10) + delay(1)` on every call (`docs/FINDINGS_NONBLOCKING_LOOP_2026-07-13.md:63-68`).
26. **The Seeed `tconWaitForDisplayReady()` has no timeout** — a wedged panel hangs the firmware forever (same doc, §6 bug 2).
27. **Buzzer melody must be copied out of the incoming buffer** (`buzzer_control.cpp:328-330`) — ESP32 recycles the command buffer the moment the handler returns.
28. **Buzzer notes are octave-folded, not clamped** (`buzzer_control.cpp:82-97`) — an out-of-range index preserves its pitch class by shifting ±24 (one octave) at a time into the playable window (idx 117–234, 403.48 Hz–11 839.82 Hz).
29. **GT911 must be suspended around EPD refreshes and resumed afterwards** (`touchSuspendForEpdRefresh` / `touchResumeAfterEpdRefresh`, `touch_input.cpp:114`, `:416`); resume can cost 200 ms–2 s.
30. **`initButtons()` skips any pin that is a GT911 INT pin** (`device_control.cpp:586-589`).
31. **`initButtons()` does a detach → settle(50 ms) → re-read → re-attach dance** (`device_control.cpp:624-662`) to discard the spurious edges generated during pin configuration.
32. **A stray legacy 0x71/0x72 during an active pipe transfer must be silently discarded**, not honoured (`display_service.cpp:1937`, `:1993`) — otherwise it feeds the panel out-of-band from the sliding-window seq accounting.
33. **Every partial-START NACK clears `displayed_etag`** (`display_service.cpp:2318-2330`, `send_direct_write_nack` `:2565`) so a later partial falls back cleanly to a full upload instead of diffing against a stale base.
34. **A successful full-frame refresh with no etag *clears* `displayed_etag`** rather than leaving it stale (`display_service.cpp:2103-2104`).
35. **nRF `updatemsdata()` early-exits when the payload is unchanged** (`display_service.cpp:1506-1512`) — without this it would tear down and restart advertising on every single loop pass.
