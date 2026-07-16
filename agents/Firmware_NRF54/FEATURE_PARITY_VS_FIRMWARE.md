# Feature Parity Audit — nRF54L15 (NCS/Zephyr) vs nRF52840/ESP32 (PlatformIO/Arduino)

**Date:** 2026-07-13
**NEW/PORT:** `Firmware_NRF54` (nRF54L15, Seeed XIAO, NCS + Zephyr, C, `third_party/bb_epaper`)
**REFERENCE:** `../Firmware` (nRF52840 + ESP32-S3/C6/C3, PlatformIO + Arduino, C++, `lib/Seeed_GFX`)

> Companion docs: [ARCHITECTURE_NRF54.md](ARCHITECTURE_NRF54.md), [ARCHITECTURE_FIRMWARE_SISTER.md](ARCHITECTURE_FIRMWARE_SISTER.md), [RTOS_COMPARISON.md](RTOS_COMPARISON.md)

---

## 1. Executive verdict

**There is NOT feature parity.** The nRF54 port implements roughly **70% of reference features by count**, but the missing/divergent 30% includes several of the highest-leverage ones (the PIPE_WRITE fast image path, all power management, all OTA/DFU), and — worse — contains **four hard protocol incompatibilities that will silently misbehave rather than fail cleanly.**

What *is* solid: the core BLE pipe (service/char `0x2446`), the full config packet system (parse/store/chunked read+write/clear), the whole encryption/auth stack (AES-CMAC challenge, session-key derivation, AES-CCM, replay window — reimplemented on PSA and byte-compatible), legacy direct-write `0x70/0x71/0x72`, partial-region write `0x76`, streaming zlib inflate, boot screen + QR + logo, GT911 touch, SHT40, BQ27220, LED sequencer, MSD/advertising.

What is **not** there at all: PIPE_WRITE (`0x0080–0x0082`) sliding-window transfer, deep sleep, power latch (MOSFET + D-FF), wake-on-button, EPD keep-alive/panel power sessions, DFU/OTA of any kind (no MCUboot, no bootloader), AXP2101, long-press power-off buttons, the Seeed_GFX/IT8951 13.3" panel family.

**The four dangerous divergences** (details in §4): opcode `0x0082` collision (PIPE END vs NFC), buzzer frequency mapping (linear vs musical — every melody plays at the wrong pitch), GRAY16 bits-per-pixel (1 vs 4 → 4× image-size mismatch), and `0x0051` ENTER_DFU acking success while doing nothing.

---

## 2. Parity table

### 2.1 BLE / GATT / advertising

| Feature | Reference (Firmware) | nRF54 | Status | Evidence | Notes |
|---|---|---|---|---|---|
| Service UUID `0x2446` | `main.h:375` `BLEService imageService("2446")`; ESP32 `ble_init.cpp:283` | `opendisplay_ble.c:307-313` `BT_GATT_SERVICE_DEFINE(od_svc, …DECLARE_16(0x2446))` | ✅ | | |
| Characteristic `0x2446` W/WNR/Notify | `main.h:376` (`BLEWrite\|BLEWriteWithoutResponse\|BLENotify`, 512 B) | `opendisplay_ble.c:309-312` (`WRITE\|WRITE_WITHOUT_RESP\|NOTIFY`) | ✅ | | Reference ESP32 also exposes READ (`ble_init.cpp:293`); nRF54 does not. Harmless. |
| Device name `OD<chipid6>` | `ble_init.cpp:186-187`; ESP32 `ble_init.cpp:271` | `opendisplay_ble.c:560-561` `snprintf("%s%s", "OD", hex)` | ✅ | | |
| Name in **ADV** vs **scan response** | nRF: `ble_init.cpp:199` `Advertising.addName()` → **ADV packet** | `opendisplay_ble.c:320,331` name is in **scan response** (`sd_buf[0]`) | ⚠️ | | **Divergence D5.** Passive scanners / `scan(active=False)` filters on name break on nRF54. |
| Manufacturer data (16 B, CID `0x2446`) | `display_service.cpp:1498-1516` | `opendisplay_ble.c:269-274, 315-318` | ✅ | | Byte layout identical (see MSD row). |
| Service UUID in advertising | nRF52: **not advertised**; ESP32: `ble_init.cpp:316` `addServiceUUID` in ADV | `opendisplay_ble.c:321,332` `0x2446` in **scan response** | ⚠️ | | Three different placements across the three targets. |
| Adv interval 160–1000 ms | `ble_init.cpp:48-49` `NRF_ADV_INTERVAL_MIN=256/MAX=1600` | `opendisplay_ble.c:119-120` `OD_ADV_INTERVAL_MIN=256`, `MAX=BT_GAP_ADV_SLOW_INT_MIN(1600)` | ✅ | | |
| Adv "boost" 20–30 ms for 3 s on button | `ble_init.cpp:50-56,67-84` | `opendisplay_ble.c:121-123, 624-649` | ✅ | | |
| Advertising restart on disconnect | `ble_init.cpp:201` `restartOnDisconnect(true)` | `opendisplay_ble.c:402-414` `disconnected()` → `schedule_adv_restart(150)` + `recycled()` + 500 ms fallback in `opendisplay_ble_process` (`:719-722`) | ✅ | | nRF54's is more robust. |
| MSD suppress-if-unchanged | `display_service.cpp:1506-1512` (nRF), `:1524-1530` (ESP32) | `opendisplay_ble.c:346-349, 371-374` | ✅ | | |
| TX power from `power_option.tx_power` | `ble_init.cpp:161` `Bluefruit.setTxPower()` | `opendisplay_ble.c:500-535` HCI VS `Write_Tx_Power_Level` (adv + conn) | ✅ | | nRF54 also re-applies on config reload (`:613`). |
| MTU 512 / ATT | ESP32 `ble_init.cpp:275` `setMTU(512)` | `zephyr/prj.conf` `CONFIG_BT_L2CAP_TX_MTU=512`, `BT_BUF_ACL_{RX,TX}_SIZE=512` | ✅ | | |
| Explicit 2M PHY + 251-octet DLE request | `ble_init.cpp:123-144` `ble_nrf_request_fast_link()` — **actively requests** on connect (`device_control.cpp:91`) | `zephyr/prj_ncs.conf` `BT_CTLR_PHY_2M=y`, `BT_CTLR_DATA_LENGTH_MAX=251`, but `CONFIG_BT_AUTO_PHY_UPDATE=n` and **no `bt_conn_le_phy_update()` / `bt_conn_le_data_len_update()` call anywhere** | ⚠️ | `opendisplay_ble.c:377-400` `connected()` | **Throughput risk.** If the phone never asks, the nRF54 link stays 1M/27-octet. The reference explicitly upgrades. |
| Connection-parameter update | Bluefruit default | `prj_ncs.conf` `CONFIG_BT_GAP_AUTO_UPDATE_CONN_PARAMS=n` — auto-update **disabled**, no manual `bt_conn_le_param_update()` | ⚠️ | | Device never asks for a faster interval. |
| Link-param diagnostics logging | `ble_init.cpp:91-155` | — | ❌ | | Cosmetic. |
| Pairing / bonding / security | None (Bluefruit, no `requestPairing`) | `prj_ncs.conf` `CONFIG_BT_SMP=y`, `BT_BONDABLE=y`, `BT_SETTINGS=y` | ➕ | | Compiled in, but the char perm is plain `BT_GATT_PERM_WRITE` (`opendisplay_ble.c:312`) — no encryption required, so behaviour matches. App-layer crypto is the real gate on both. |
| Notify with backpressure retry | `communication.cpp:245-249` (4 retries × 5 ms) | `opendisplay_pipe.c:542-551` (200 retries × 1 ms) | ✅ | | |
| Write dispatch off the BT RX thread | ESP32 uses a command queue (`main.h:387`); nRF52 runs in the Bluefruit callback | `opendisplay_pipe.c:76-82` `K_MSGQ_DEFINE(s_pipe_msgq, …, 8, …)`, drained in `opendisplay_pipe_process()` (`:1407`) | ➕ | | nRF54 architecture is cleaner. **But queue depth is only 8** vs ESP32's 33 (`main.h:387`) — under a burst of `write-without-response` frames, `k_msgq_put` with `K_MSEC(100)` on the RX thread can time out and **drop image data** (`:1387-1389`). |
| Disconnect cleanup (abort transfer, clear session) | `device_control.cpp:96-109` | `opendisplay_pipe.c:1399-1418` (deferred to main thread) | ✅ | | |
| Generation counter drops stale frames | — | `opendisplay_pipe.c:83, 1384, 1420-1422` | ➕ | | |

### 2.2 Protocol — every opcode

| Opcode | Reference | nRF54 | Status | Evidence | Notes |
|---|---|---|---|---|---|
| `0x000F` REBOOT | `communication.cpp:602-606` → `reboot()` `device_control.cpp:111` | `opendisplay_pipe.c:1235-1240` `NVIC_SystemReset()` | ✅ | | |
| `0x0040` CONFIG_READ (chunked) | `communication.cpp:351-393` | `opendisplay_pipe.c:822-887` | ✅ | | Same 100-B chunk framing, same `(MAX_CONFIG_SIZE+93)/94` cap. |
| `0x0041` CONFIG_WRITE | `communication.cpp:395-437` | `opendisplay_pipe.c:889-965` | ✅ | | nRF54 adds `total_size` sanity checks the reference lacks. |
| `0x0042` CONFIG_CHUNK | `communication.cpp:453-493` | `opendisplay_pipe.c:967-1021` | ✅ | | |
| `0x0043` FIRMWARE_VERSION | `communication.cpp:316-349` | `opendisplay_pipe.c:587-608` | ✅ | | `{00,43,major,minor,shalen,sha…}` on both; always plaintext on both. |
| `0x0044` READ_MSD | `communication.cpp:258-268` | `opendisplay_pipe.c:610-618` | ✅ | | |
| `0x0045` CONFIG_CLEAR | `communication.cpp:439-451` | `opendisplay_pipe.c:809-820` | ✅ | | |
| `0x0050` AUTHENTICATE | `encryption.cpp:513-…` | `opendisplay_pipe.c:620-725` | ✅ | | Wire-identical: `{00,50,00,server_nonce16,device_id4}` challenge; 32-B response; `{00,50,00,cmac16}` success. Status codes 0x01/0x02/0x03/0x04/0xFF all match. |
| `0x0051` ENTER_DFU | `communication.cpp:656-659` → `enterDFUMode()` `device_control.cpp:665-703` — real GPREGRET `0xB1` + bootloader jump | `opendisplay_pipe.c:1241-1247` sends `{0x00,0x51}` **SUCCESS**, then `opendisplay_ble_schedule_dfu()` → `opendisplay_ble.c:725-728` `printf("DFU not implemented")` | ❌ | | **Divergence D4 — lies to the client.** |
| `0x0052` DEEP_SLEEP | `communication.cpp:660-662` → `handleDeepSleepCommand()` `device_control.cpp:705-746` (ESP32: real; nRF52840: log-only, **no response**) | `opendisplay_pipe.c:1248-1254` no response, `opendisplay_ble.c:730-733` log-only | ✅ (vs nRF52840) / ❌ (vs ESP32) | | nRF54 deliberately matches nRF52840's no-op-with-no-response, so clients correctly infer "unsupported". Honest, unlike 0x0051. |
| `0x0070` DIRECT_WRITE_START | `display_service.cpp:1801-1839` | `opendisplay_pipe.c:741-751` → `opendisplay_display.cpp:710-810` | ⚠️ | | See D2 (GRAY16 sizing) and D6 (`STREAMING_DECOMPRESSION` gate). |
| `0x0071` DIRECT_WRITE_DATA | `display_service.cpp:1932-1988` | `opendisplay_pipe.c:753-772` → `opendisplay_display.cpp:812-865` | ⚠️ | | **Reference auto-completes** the refresh when `bytesWritten >= totalBytes` (`display_service.cpp:1982-1983` calls `handleDirectWriteEnd`). **nRF54 does not** — it ignores trailing bytes (`opendisplay_display.cpp:844-854`) and waits for an explicit `0x72`. A client relying on auto-complete hangs. |
| `0x0072` DIRECT_WRITE_END | `display_service.cpp:1990-2030`, `directWriteFinishAndRefresh` `:2036-2112` | `opendisplay_pipe.c:774-807` (2-stage prepare/refresh) | ✅ | | Same ack ordering: `{00,72}` → refresh → `{00,73}`/`{00,74}`. Same `[refresh:1][new_etag:4 BE]` payload. |
| `0x0073` LED_ACTIVATE | `device_control.cpp:377-412` | `opendisplay_pipe.c:1255-1271` → `opendisplay_led.c` | ✅ | | 12-byte `reserved[]` sequence payload, same error codes 0x01/0x02. |
| `0x0075` LED_STOP | `device_control.cpp:414-423` | `opendisplay_pipe.c:1272-1288` | ✅ | | |
| `0x0076` PARTIAL_WRITE_START | `display_service.cpp:1841-1930` | `opendisplay_pipe.c:727-739` → `opendisplay_display.cpp:444-565` | ✅ | | 17-byte BE header `[flags][old_etag4][new_etag4][x2][y2][w2][h2]`; err codes 0x01/0x03/0x04/0x05/0x06/0x07 identical (`opendisplay_protocol.h:17-22` vs `display_service.cpp:66-71`). |
| `0x0077` BUZZER | `communication.cpp:652-655` → `buzzer_control.cpp` | `opendisplay_pipe.c:1289-1300` → `opendisplay_buzzer.c:304-376` | ⚠️ | | **Divergence D1 — frequency mapping is completely different.** |
| `0x0080` PIPE_WRITE_START | `communication.cpp:626-629`, `display_service.cpp:2272+` | **ABSENT** (`grep -rn "0x0080\|PIPE_WRITE\|pipeState" Firmware_NRF54/src/` → 0 hits) | ❌ | | Falls to `default:` → `printf("unknown cmd")`, **no response at all**. Client times out. |
| `0x0081` PIPE_WRITE_DATA (+SACK) | `communication.cpp:630-636`, `display_service.cpp:2435+` | **ABSENT** | ❌ | | |
| `0x0082` PIPE_WRITE_END | `communication.cpp:637-640`, `display_service.cpp:2526+` | **REPURPOSED as `CMD_NFC_ENDPOINT`** — `opendisplay_protocol.h:27`, `opendisplay_pipe.c:1301-1302, 1023-1176` | ❌ | | **Divergence D3 — opcode collision.** |
| NFC endpoint (read/write/chunked) | — | `opendisplay_pipe.c:1023-1176` | ➕ | | nRF54-only… and squatting on the reference's PIPE END opcode. Backing impl is a stub: `opendisplay_ble.c:735-751` both return `false`. So it always NACKs `{FF,82,FF,02/03}`. |

### 2.3 Encryption / auth

| Feature | Reference | nRF54 | Status | Evidence | Notes |
|---|---|---|---|---|---|
| AES-CMAC (mbedTLS) | `encryption.cpp:31` | PSA `psa_mac_compute(PSA_ALG_CMAC)` `opendisplay_pipe.c:174-194` | ✅ | | |
| AES-ECB | `encryption.cpp:32` | PSA `opendisplay_pipe.c:196-216` | ✅ | | |
| AES-CCM (13-B nonce, 12-B tag, 2-B AAD) | `encryption.cpp:33-40` | Hand-rolled RFC 3610 over ECB, `opendisplay_pipe.c:286-405` (B0 flags `0x69`) | ✅ | | Wire-compatible; nRF54 implements CCM manually rather than via PSA AEAD. |
| Session key derivation | `encryption.cpp:61-93` — label `"OpenDisplay session"` ‖ `0x00` ‖ dev_id4 ‖ cnonce16 ‖ snonce16 ‖ `0x00 0x80`, then ECB(master, `[be64(1)][cmac[0..7]]`) | `opendisplay_pipe.c:218-249` — byte-for-byte identical construction (`final_input[7]=0x01`) | ✅ | | Verified line by line. |
| Session ID = CMAC(sk, cnonce‖snonce)[0..7] | `encryption.cpp:95-108` | `opendisplay_pipe.c:251-265` | ✅ | | |
| Nonce = `session_id8 ‖ be64(counter)`; CCM nonce = nonce[3..15] | `encryption.cpp:158-168` | `opendisplay_pipe.c:460, 500-505` | ✅ | | |
| Replay window ±32, 64-entry history | `encryption.cpp:128-156` | `opendisplay_pipe.c:407-438` | ✅ | | |
| 3-strike integrity failure → session teardown | `encryption.cpp:640-646, 678-683` | `opendisplay_pipe.c:135-141` | ✅ | | |
| Auth rate limit (10 attempts / 60 s) | `encryption.cpp:520-533` | `opendisplay_pipe.c:639-648` | ✅ | | |
| Server nonce 30 s expiry | `encryption.cpp:556` | `opendisplay_pipe.c:678` | ✅ | | |
| Session timeout (absolute) | `encryption.cpp:205-217` | `opendisplay_pipe.c:153-172` | ✅ | | |
| `SECURITY_FLAG_REWRITE_ALLOWED` (bit0) + secure-erase | `communication.cpp:397-405, 459-468` | `opendisplay_pipe.c:1187-1214` | ✅ | | nRF54 erases on **both** the write and first-chunk paths (reference does too). |
| `SECURITY_FLAG_RESET_PIN_*` (bits 2–5) + `checkResetPin()` | `structs.h:403-406`; `main.h:231` declares `checkResetPin()` | Flags **defined** (`opendisplay_structs.h:310-313`) but **no reset-pin logic** | ❌ | | Factory-reset-by-pin is unavailable on nRF54. |
| `SECURITY_FLAG_SHOW_KEY_ON_SCREEN` (bit1) | Defined, unimplemented both sides | Defined | N-A | | Future feature on both. |
| DFU service gated on encryption | `ble_init.cpp:177-182` — `bledfu.begin()` only when encryption disabled | No DFU at all | ❌ | | Moot on nRF54. |

### 2.4 WiFi / OTA (ESP32-only in reference)

| Feature | Reference | nRF54 | Status | Evidence |
|---|---|---|---|---|
| WiFi STA + reconnect | `wifi_service.cpp` (259 lines), ESP32 only | — (nRF54L15 has no WiFi radio) | N-A | `wifi_service.h:4` `#ifdef TARGET_ESP32` |
| LAN TCP server (port 2446) + framing | `communication.cpp:78-86`, `main.h:167-171` | — | N-A | |
| mDNS + MSD TXT record | `wifi_service.h:11` | — | N-A | |
| `COMM_MODE_WIFI` bit (`main.h:65`) | Advertised in `system_config.communication_modes` | Not consumed | N-A | |
| WiFi config packet `0x26` **stored and returned on read-back** | `config_parser.cpp:457` | `opendisplay_config_parser.c:481` + `GlobalConfig.wifi_config` (`opendisplay_structs.h:273`) | ✅ | Deliberate: preserves a client's WiFi block across read/write cycles. Good. |
| OTA (ESP32 HTTP/OTA path) | Reboot only (`device_control.cpp:698-701`) | — | N-A | |

### 2.5 Display

| Feature | Reference | nRF54 | Status | Evidence | Notes |
|---|---|---|---|---|---|
| bb_epaper panel map `0x00–0x46` | `display_service.cpp:412-494` (`mapEpd`) | `opendisplay_epd_map.c:4-93` | ✅ | | IDs 0x00–0x46 identical. |
| Panels `0x0047,0x0048,0x004A,0x004C` | Mapped (EP368, EP368_4GRAY, EP40_SPECTRA, EP27_4GRAY) | `EP_PANEL_UNDEFINED` — not vendored | ❌ | `opendisplay_epd_map.c:86-91` | Configs for these panels **fail with `dw start err bad panel_ic_type`**. |
| Panels `0x0049` (EP213ZZ), `0x004B` (EP27) | Mapped exactly | **Substituted** with EP213Z / EP27B | ⚠️ | `opendisplay_epd_map.c:88, 90` | May render incorrectly — the file's own comment admits it. |
| Seeed ED103TC2 1872×1404 (`panel_ic 3000/3001`, IT8951/Seeed_GFX) | `display_service.cpp:496-507`, `lib/Seeed_GFX/`, `display_seeed_gfx.cpp` | — | ❌ | `structs.h:79-80` | Entire 13.3" family unsupported. |
| Color: MONO(0) | 1 bpp | 1 bpp | ✅ | `display_service.cpp:1414` / `opendisplay_display_color.c:38` | |
| Color: BWR(1) / BWY(2) | 2 planes, 1bpp each | 2 planes | ✅ | `display_service.cpp:1748` / `opendisplay_display_color.c:45` | |
| Color: BWRY(3) | 2 bpp packed | 2 bpp | ✅ | `display_service.cpp:1412` / `:32` | |
| Color: BWGBRY(4) / 6-color | 4 bpp packed | 4 bpp | ✅ | `display_service.cpp:1411` / `:29` | |
| Color: GRAY4(5) | 2 bpp *by `getBitsPerPixel`* but streams as **2×1bpp planes** (`directWriteComputeGeometry` `:1770`, `streamGray4Bytes` `:1694`) | 2×1bpp planes (`opendisplay_display_color.c:45, 65-67`) | ✅ | | Sizes agree. |
| Color: **GRAY16(6)** | `getBitsPerPixel()` returns **1** (`display_service.cpp:1404-1415` — no case for 6) | returns **4** (`opendisplay_display_color.c:35-37`) | ❌ | | **Divergence D2. 4× size mismatch.** |
| Color: GRAY8(7) | Defined (`structs.h:92`), falls through to 1 bpp | Falls through to 1 bpp | ✅ | | Neither really supports it. |
| 7-color (ACeP) | Not in the color-scheme enum on either side | — | N-A | | |
| Full refresh | `bbepRefresh(REFRESH_FULL)` | `s_epd.refresh(REFRESH_FULL)` `opendisplay_display.cpp:954` | ✅ | | |
| Fast refresh (`REFRESH_FAST`) | `display_service.cpp:2058` (`data[0]==1`) | `opendisplay_display.cpp:949-951` | ✅ | | |
| Partial refresh (`REFRESH_PARTIAL`) | `display_service.cpp:2012-2014` | `opendisplay_display.cpp:915-922, 374-392` | ✅ | | Both special-case EP397 (Y-decrement) and EP426 (X-decrement) RAM windows — nRF54 `:180-296` mirrors reference `:2840+`. |
| Rotation (`rotation × 90`) | `display_service.cpp:1361` | `opendisplay_display.cpp:695, 743` | ✅ | | |
| Streaming zlib inflate (512-B window) | `lib/uzlib/src/od_zlib_stream.c`, chunk **2048 B** (`display_service.h:7`) | `third_party/uzlib/src/od_zlib_stream.c`, chunk **256 B** (`opendisplay_display.cpp:24`) | ✅ | | Same streamer; smaller scratch on nRF54. Functionally equivalent. |
| Compressed transfer gated on `transmission_modes` | **Not gated** — reference only *logs* the ZIPXL bit (`config_parser.cpp:713`); `handleDirectWriteStart` accepts any `len>=4` | **Gated** — `opendisplay_display.cpp:776-781` rejects with `-4` unless bit0 set | ⚠️ | | **Divergence D6.** A config that works on the reference can be rejected on nRF54. |
| `TRANSMISSION_MODE` bit meanings | bit0 ZIPXL, bit1 ZIP, bit2 G5, bit3 DIRECT_WRITE, **bit4 PIPE_WRITE**, bit7 CLEAR_ON_BOOT (`structs.h:96-101`) | bit0 STREAMING_DECOMPRESSION, bit1 ZIP, bit2 G5, bit3 DIRECT_WRITE, **no bit4**, bit7 CLEAR_ON_BOOT (`opendisplay_constants.h:47-51`) | ⚠️ | | bit0 renamed but same bit. **bit4 (PIPE_WRITE) is unknown to nRF54** — if a config advertises it, clients will attempt pipe and get nothing. |
| Dithering | Not in firmware (host-side, py-opendisplay) | Same | N-A | | |
| G5 compression | Bit defined, **not implemented** (`config_parser.cpp:715` logs only) | Bit defined, not implemented | ✅ (equally absent) | | `Group5.cpp` is vendored on nRF54 but unused by the write path. |
| Framebuffer memory | Bufferless (direct-to-controller-RAM) on both | Bufferless | ✅ | | |
| **EPD keep-alive / panel power session** (PWR_OFF/WARM/ACTIVE, `screen_timeout_seconds`, cross-task lock) | `display_service.cpp:160-317` (`epdSessionAcquire/Release/ForceOff/Tick`), config field `structs.h:61-63` | **ABSENT.** Panel is powered up per transfer and hard-cut after every refresh: `opendisplay_display.cpp:957-961` `sleep(DEEP_SLEEP); display_power_set(false)` | ❌ | `grep -rn "screen_timeout\|keepalive\|PWR_WARM" Firmware_NRF54/src/` → 0 hits | Every push pays the full cold bring-up. |
| Boot screen + QR + logo | `boot_screen.cpp` (1006 L), `logo_bitmap.h` (1176 L), `src/qr/qrcode.c` | `boot_screen.cpp` (1039 L), `logo_bitmap.h` (**identical**, 1176 L), `src/qr/qrcode.c` | ✅ | `opendisplay_display.cpp:677-708` `opendisplay_display_boot_apply()` | |
| `CLEAR_ON_BOOT` (bit7) | `display_service.cpp:1339, 1365` | `opendisplay_display.cpp:699` | ✅ | | |
| Text rendering / `writelineFont` | `display_service.cpp:1294-1316`, `main.h:36-41` | in `boot_screen.cpp` | ✅ | | |
| ETag tracking (partial-update base image) | `main.h:312-316` `displayed_etag`, RTC-persistent on ESP32 | `opendisplay_display.cpp:41` `s_displayed_etag` (RAM only) | ✅ | | Equivalent to the nRF52840 behaviour (also RAM-only). |
| `full_update_mC` config field | `structs.h:192` | Not in struct (`opendisplay_structs.h:76` `reserved[15]`) | ❌ | | Field is parsed-away; unused by reference logic anyway. |
| Refresh timeout / wait-busy | `waitforrefresh(60)` @10 ms poll (`display_service.cpp:509-535`) | `wait_for_refresh(60000)` @50 ms poll (`opendisplay_display.cpp:127-142`) | ✅ | | |
| Partial-write 15-min watchdog | `display_service.cpp:357-366` `checkPartialWriteTimeout()` | **ABSENT** | ⚠️ | | A stalled partial transfer on nRF54 leaves the panel powered until disconnect. |
| Touch re-probe after EPD refresh | `touch_input.cpp` / `touchResumeAfterEpdRefresh` | `opendisplay_display.cpp:936, 980` | ✅ | | |

### 2.6 Config

| Feature | Reference | nRF54 | Status | Evidence |
|---|---|---|---|---|
| `MAX_CONFIG_SIZE` 4096 | `config_parser.h:7` | `opendisplay_config_storage.h:18` | ✅ |
| Storage backend | LittleFS (nRF)/LittleFS (ESP32), `config_storage_t` w/ CRC32 | Zephyr `settings`/NVS, `[magic][ver][crc32][len][data]` | ✅ | `opendisplay_config_storage.c:33-102` |
| Toolbox outer CRC-16/CCITT (len bytes zeroed) | `config_parser.cpp:228-248` | `opendisplay_config_parser.c:83-100`, `factory_config.c:16-33` | ✅ |
| Packet `0x01` system_config | `config_parser.cpp:293` | `:197` | ⚠️ | **nRF54 struct lacks `pwr_pin_2`/`pwr_pin_3`** (`opendisplay_structs.h:12` `reserved[17]` vs `structs.h:30-32`). Latch pins unreadable. |
| Packet `0x02` manufacturer_data | `structs.h:36-41` (board_type, rev, `reserved[18]`) | `opendisplay_structs.h:15-24` — **adds** `simple_config_*_index`, `configured_at[6]` | ⚠️ | Same 22-B wire size; nRF54 reads extra sub-fields the reference treats as reserved. Read-only, harmless. |
| Packet `0x04` power_option | `structs.h:44-65` | `opendisplay_structs.h:26-43` | ⚠️ | **nRF54 struct is missing `min_wake_time_seconds` (u16) and `screen_timeout_seconds` (u8)** — both live in what nRF54 calls `reserved[7]`. Both features consequently absent. |
| Packet `0x20` display | `config_parser.cpp:323` | `:263` | ✅ | Minus `full_update_mC`. |
| Packet `0x21` led | `:337` | `:307` | ✅ |
| Packet `0x23` sensor_data | `:353` | `:336` | ✅ |
| Packet `0x24` data_bus | `:367` | `:365` | ✅ |
| Packet `0x25` binary_inputs | `:381` | `:394` | ⚠️ | **nRF54 struct lacks `power_off_flags` + `power_off_hold_sec`** (`opendisplay_structs.h:140` `reserved[14]` vs `structs.h:273-275`). |
| Packet `0x26` wifi_config | `:457` | `:481` | ✅ | Stored-only on nRF54 (correct). |
| Packet `0x27` security_config | `:561` | `:508` | ✅ |
| Packet `0x28` touch_controller | `:395` | `:423` | ✅ |
| Packet `0x29` passive_buzzer | `:409` | `:452` | ✅ (struct) |
| Packet `0x2A` nfc_config | **absent** | `:533`, `opendisplay_structs.h:183-201` | ➕ |
| Packet `0x2B` flash_config | `:443` | present (`opendisplay_structs.h:205-219`) | ✅ |
| Packet `0x2C` data_extended | `:423` | `opendisplay_structs.h:237-247` | ✅ |
| Strict per-packet length table | Implicit | `opendisplay_config_parser.c:123-137` explicit | ➕ |
| Factory embed provisioning | `factory_config.cpp`, `scripts/factory_config_gen.py`, `tools/provision_firmware.py` | `factory_config.c:66-79`, `scripts/factory_config_gen.py` | ⚠️ | nRF54 has no `provision_firmware.py` equivalent. |
| `FACTORY_CLEAR_CONFIG_ON_BOOT` | Present | `opendisplay_ble.c:656-660` | ✅ |
| Config reload after save (live re-apply) | `communication.cpp:27-43` | `opendisplay_ble.c:606-617` | ⚠️ | Reference also re-inits WiFi and drops a warm panel. nRF54 reloads + re-applies TX power + restarts adv, but **does not re-init LED/buzzer/touch/button/sensor subsystems** — those are only initialised once in `opendisplay_ble_init()` (`:674-692`). **A config change to pins requires a reboot on nRF54.** |

#### `device_flags` bit-by-bit

| Bit | Name | Reference | nRF54 | Status |
|---|---|---|---|---|
| 0 | `DEVICE_FLAG_PWR_PIN` | `main.h:68` — defined; power pin driven in `initio()` `display_service.cpp:744-747` | `opendisplay_device_flags.h:6` — **defined but never tested**; `display_power_set()` uses `pwr_pin` unconditionally (`opendisplay_display.cpp:104-120`) | ⚠️ |
| 1 | `DEVICE_FLAG_XIAOINIT` | `main.h:69`; acted on `config_parser.cpp:847-848` → `xiaoinit()` | Defined, **no handler** | ❌ |
| 2 | `DEVICE_FLAG_WS_PP_INIT` | `main.h:70`; `config_parser.cpp:853-854` → `ws_pp_init()` | Defined, no handler | ❌ |
| 3 | `DEVICE_FLAG_BATTERY_LATCH` | `main.h:71`; `power_latch.cpp:36` | Defined, no handler | ❌ |
| 4 | `DEVICE_FLAG_PWR_LATCH_DFF` | `main.h:72`; `power_latch.cpp:30` | Defined, no handler | ❌ |
| 5 | `DEVICE_FLAG_CHANNEL_SOUNDING` | **not defined** | `opendisplay_device_flags.h:11`; gates CS/RAS at `opendisplay_cs.c:19-30`, `opendisplay_config_parser.c:206` | ➕ |
| 6–7 | reserved | — | — | N-A |

### 2.7 Sensors

| Feature | Reference | nRF54 | Status | Evidence |
|---|---|---|---|---|
| SHT40 (I2C, CRC8, temp+RH → 3 MSD bytes) | `sensor_sht40.cpp:18,91,97,207-211` | `opendisplay_sensor_sht40.c:14,66,142-146` | ✅ | Same packing, same 0xFF-on-error, same `msd_data_start_byte` default 7. |
| SHT40 address auto-probe (0x44/0x45) | `sensor_sht40.cpp:127` | `opendisplay_sensor_sht40.c:93` | ✅ |
| BQ27220 fuel gauge (voltage + packed SOC byte) | `sensor_bq27220.cpp:167-187` | `opendisplay_sensor_bq27220.c:158-180` | ✅ | Both cap SOC at 100, 0xFF on failure, `soc & 0x7F` packing. |
| BQ27220-preferred battery voltage | `display_service.cpp:1418-1423` | `opendisplay_battery.c:3` + `opendisplay_ble.c:248-250` | ✅ |
| Battery via ADC/SAADC + `voltage_scaling_factor` | `display_service.cpp:1424-1445` (10 samples, `analogRead`) | `opendisplay_battery.c:98-155` (Zephyr ADC, `adc_raw_to_millivolts_dt`) | ⚠️ | **nRF54 only supports AIN on P1.00–P1.07** (`opendisplay_battery.c:112-115`); reference accepts any analog pin. Also `BATTERY_SENSE_FLAG_ENABLE_INVERTED` is **not honored** on nRF54 (`:134` comment) — matching the reference's own omission. |
| 30 s battery TTL cache | `display_service.cpp:1448-1460` | `opendisplay_battery.c:34` | ✅ |
| Chip temperature | `display_service.cpp:1462-1473` (`sd_temp_get`) | `opendisplay_ble.c:224-235` (Zephyr `nordic,nrf-temp`) | ⚠️ | **nRF54 reads it exactly once at boot** (`read_chip_temperature_once()`, called from `opendisplay_ble_init:689`) — the MSD temperature byte never updates. Reference re-reads on every `updatemsdata()`. |
| MSD temp encoding `(T+40)*2` | `display_service.cpp:1489-1492` | `opendisplay_ble.c:252-258` | ✅ |
| MSD status byte (bat-hi, reboot, connreq, loopcounter) | `display_service.cpp:1494-1497` | `opendisplay_ble.c:263-266` | ✅ |
| AXP2101 PMIC | `display_service.cpp:825-968` (`initAXP2101`), `readAXP2101Data` `:970+`; `SENSOR_TYPE_AXP2101` | Sensor type **defined** (`opendisplay_structs.h:93`) but **no driver** | ❌ |
| I2C bus scan | `display_service.cpp:758-788` | — | ❌ | Debug aid only. |
| Temperature-compensated refresh | **Neither** implements it (chip temp is reported in MSD, never fed to the panel LUT) | — | N-A |

### 2.8 Inputs

| Feature | Reference | nRF54 | Status | Evidence |
|---|---|---|---|---|
| GT911 touch (init, poll, INT, reset, addr auto) | `touch_input.cpp` (731 L) | `opendisplay_touch.c` (703 L) | ✅ |
| Touch flags INVERT_X/Y, SWAP_XY | `structs.h:282-284` | `opendisplay_touch.c:465-478` | ✅ |
| Touch `enable_pin`, `poll_interval_ms`, `display_instance`, `touch_data_start_byte` | `structs.h:293-297` | `opendisplay_touch.c:310-315, 413, 470-472, 607` | ✅ |
| Touch suspend/resume around EPD refresh | `touchSuspendForEpdRefresh` / `touchResumeAfterEpdRefresh` | `opendisplay_touch_resume_after_refresh()` (`opendisplay_touch.h:26`) | ⚠️ | nRF54 has **resume** but no explicit **suspend** — reference suspends before refresh (`display_service.cpp:1342, 1808`). |
| Button GPIO + press count + MSD byte | `device_control.cpp:517-532, 556-663` | `opendisplay_button.c:42-137` | ✅ | Same `(id&7) \| (count&0xF)<<3 \| state<<7` packing. |
| `MAX_BUTTONS` | **32** (`structs.h:408`, 4 instances × 8) | **8** (`opendisplay_button.c:11`) | ⚠️ | Configs with >8 buttons silently truncate. |
| Button interrupts | ISR-driven, edge-accurate | IRQ sets a flag; **actual detection is polled** (`opendisplay_button.c:104-106`) | ⚠️ | A fast press-release between poll passes can be missed on nRF54. |
| Long-press power-off (`power_off_flags`, `power_off_hold_sec`) | `device_control.cpp:44-81` `pollConfiguredPowerOffButtons()` | **ABSENT** (config fields not even in the struct) | ❌ |
| Hardware power button (latch boards) | `power_latch.cpp` `powerButtonPoll()` | ABSENT | ❌ |
| Wake-on-button from deep sleep | `wake_button.cpp` (272 L), `armButtonWakeSources()`, `detectButtonWake()` | ABSENT | ❌ |

### 2.9 Outputs

| Feature | Reference | nRF54 | Status | Evidence |
|---|---|---|---|---|
| LED 3-group flash sequencer (12-B program, brightness, loops, delays, repeats) | `device_control.cpp:125-412` | `opendisplay_led.c` (389 L), `opendisplay_pipe.c:1255-1288` | ✅ |
| LED invert flags (`led_flags` bits 0–3) | `device_control.cpp:170-181` | present | ✅ |
| LED software PWM / brightness | `device_control.cpp:465-514` `flashLed()` | `opendisplay_led.c` | ✅ |
| Buzzer: payload format `[inst][repeats][npatterns]{[nsteps]{[freq][dur]}*}*` | `buzzer_control.cpp` | `opendisplay_buzzer.c:304-376` | ✅ |
| Buzzer: `duty_percent`, `enable_pin`, `BUZZER_FLAG_ENABLE_ACTIVE_HIGH` | `buzzer_control.cpp:105-115` | `opendisplay_buzzer.c:84-95, 120-124` | ✅ |
| Buzzer: duration unit 5 ms, inter-pattern gap 20 ms | `buzzer_control.cpp:15-16` | `opendisplay_buzzer.c:34-35` | ✅ |
| **Buzzer: frequency index → Hz** | **Quarter-tone table**, `f = 13.75·2^(idx/24)` centi-Hz, `idx 120 = A4 = 440.00 Hz`, octave-folded into playable window [117, 234] (`buzzer_control.cpp:19-113`) | **Linear**, `400 + 11600·(idx−1)/254` Hz (`opendisplay_buzzer.c:74-82`) | ❌ | **Divergence D1** |
| **Buzzer: max total duration** | **30 000 ms** (`buzzer_control.cpp:17`) | **5 000 ms** (`opendisplay_buzzer.c:36`) | ⚠️ | **Divergence D1b** — long melodies are truncated at 5 s. |
| Buzzer power-off alert | `passiveBuzzerPowerOffAlert()` (`buzzer_control.h:96`) | ABSENT (no power-off path) | ❌ |
| Buzzer non-blocking playback | `buzzerService()` state machine | k_timer state machine | ✅ |

### 2.10 Power

| Feature | Reference | nRF54 | Status | Evidence |
|---|---|---|---|---|
| Deep sleep (timer) | `main.cpp:468+` `enterDeepSleep()`, ESP32 | ABSENT — `opendisplay_ble.c:730-733` | ❌ |
| `deep_sleep_time_seconds` config | `structs.h:56` | Field in struct (`opendisplay_structs.h:38`) but **unused** | ❌ |
| Idle/quiet-window sleep logic | `main.cpp:262-420` (`pollActivity`, `DEFAULT_IDLE_HOLD_MS`) | ABSENT — `main.c:34-59` loop never sleeps | ❌ |
| `min_wake_time_seconds` floor | `main.cpp:166-176`, `structs.h:60` | Field not in struct | ❌ |
| `sleep_timeout_ms` (MSD refresh cadence) | `main.cpp:413` | `main.c:49-51` | ✅ |
| Wake-on-button | `wake_button.cpp` | ABSENT | ❌ |
| Power latch (MOSFET, `DEVICE_FLAG_BATTERY_LATCH`) | `power_latch.cpp:36+` | ABSENT | ❌ |
| Power latch (74AHC1G79 D-FF, `DEVICE_FLAG_PWR_LATCH_DFF`) | `power_latch.cpp:30+`, released by `0x0052` | ABSENT | ❌ |
| EPD panel power session / keep-alive | `display_service.cpp:160-317` | ABSENT | ❌ |
| EPD rail power-cycle at boot | `display_service.cpp:136-150` `prepareEpdRailForBoot()` | `main.c:31` `board_nrf54_prepare_epd_rail()` | ✅ |
| GPIO parking on power-down | `display_service.cpp` (implicit) | `opendisplay_display.cpp:84-102` `opendisplay_display_park_pins()` | ➕ |
| External SPI flash deep-power-down | `powerDownExternalFlashFromConfig()` (`main.h:199`) | `opendisplay_ble.c:585-604` | ✅ |
| DC/DC + low-power mode | `ble_init.cpp:190-191` `sd_power_mode_set/dcdc_mode_set` | Zephyr defaults (no explicit call) | ⚠️ |
| Charger control (`charge_enable_pin`, `charge_state_pin`, `charger_flags`) | Fields in `structs.h:57-59` | Fields present (`opendisplay_structs.h:39-41`) but **no driver on either side** | N-A |
| Low-battery behaviour | Neither implements a cutoff | — | N-A |

### 2.11 Build / dev

| Feature | Reference | nRF54 | Status | Evidence |
|---|---|---|---|---|
| CI build | `.github/workflows/main.yaml` (PlatformIO, nRF52840 + ESP32-S3) | `.github/workflows/main.yaml` (NCS v3.3.1, XIAO nRF54L15) | ✅ |
| Release workflow | `.github/workflows/release.yml` + `release-to-discord.yml` | `.github/workflows/release.yml` | ⚠️ | No Discord notification. |
| Multi-target build matrix | 10 PlatformIO envs (`platformio.ini`) | 2 boards (L15, LM20) via `build.sh` | N-A |
| **OTA / DFU** | nRF52840: Adafruit `BLEDfu` service (`ble_init.cpp:178`) + `enterDFUMode()` bootloader jump | **NONE.** `grep -rn "MCUBOOT\|BOOTLOADER\|DFU\|SMP_" zephyr/*.conf` → only `CONFIG_BT_SMP` (pairing, unrelated) | ❌ |
| UF2 packaging | `scripts/nrf_uf2_post.py`, `tools/uf2conv.py`, `tools/uf2families.json` | ABSENT | ❌ |
| Factory provisioning tool | `tools/provision_firmware.py` | ABSENT (only `scripts/factory_config_gen.py`) | ❌ |
| Logo conversion tool | `tools/convert_logo.py`, `tools/od_logo.svg` | ABSENT (logo pre-baked in `logo_bitmap.h`) | ⚠️ |
| zlib test harness | `tools/test_zlib_stream.c` | ABSENT | ⚠️ |
| Config packet tool | `tools/config_packet.py` | `tools/config_packet.py` | ✅ |
| Logging | UART + buffered `writeSerial`/`flushLog` | RTT (`CONFIG_USE_SEGGER_RTT=y`), UART via `PROFILE=uart` (`zephyr/prj_uart.conf`) | ✅ |
| Design docs | `docs/` — 9 design/findings docs incl. `pipe-write-protocol.md`, `buzzer-protocol.md`, `epd-panel-power-session.md` | `docs/LM20_NCS.md` only (before this audit) | ⚠️ |

### 2.12 nRF54-only (➕)

| Feature | Evidence | Notes |
|---|---|---|
| **BLE Channel Sounding + RAS ranging (reflector)** | `src/opendisplay_cs.c` (210 L), `src/opendisplay_cs.h`, `zephyr/prj_cs.conf` | Compiled in; runtime-gated on `DEVICE_FLAG_CHANNEL_SOUNDING` (bit 5). Advertises `BT_UUID_RANGING_SERVICE` in scan response (`opendisplay_cs.c:37-56`). `CONFIG_BT_CTLR_SDC_CS_ROLE_REFLECTOR_ONLY=y`. Reference has nothing comparable. |
| NFC endpoint command + `nfc_config` packet `0x2A` | `opendisplay_pipe.c:1023-1176`, `opendisplay_structs.h:183-201`, `opendisplay_constants.h:30-38` | **Stub** — `opendisplay_ble_nfc_read/write` both `return false` (`opendisplay_ble.c:735-751`). Squats on opcode `0x0082`. |
| `simple_config_*` fields in manufacturer_data | `opendisplay_structs.h:19-22` | |
| GPIO pin parking on power-off | `opendisplay_display.cpp:84-102`, `nrf54_gpio.c` | |
| Deferred command processing off the BT RX thread | `opendisplay_pipe.c:76-82, 1407-1425` | Architecturally cleaner than the reference. |
| Explicit per-packet config length table | `opendisplay_config_parser.c:123-137` | Stricter validation. |
| Compact `(port<<4)\|pin` GPIO encoding | `nrf54_gpio.h`, used throughout | Config pin bytes are **not** raw Arduino pin numbers — a config authored for the reference has incompatible pin values. |

---

## 3. Prioritized gap list

| # | Gap | Impact | Reference files to port | Effort |
|---|---|---|---|---|
| **1** | **PIPE_WRITE `0x0080/0x0081/0x0082`** — sliding-window transfer with 32-deep reorder queue + QUIC-style SACK. Without it every image push falls back to the stop-and-wait `0x71` path (one ACK per frame), which is dramatically slower on a BLE link. Any client that negotiates pipe gets **no response at all** on `0x0080` and a **bogus NFC NACK** on `0x0082`. | 🔴 Critical | `display_service.cpp:2114-2600` (`handlePipeWriteStart/Data/End`, `pipeBuildAckPayload`, `sendPipeAck/Nack`, `resetPipeWriteState`), `structs.h:103-163` (`PipeWriteState`, `PipeReorderSlot`, `PIPE_*` constants), `communication.cpp:626-640`, `docs/pipe-write-protocol.md` | **L** |
| **2** | **Opcode `0x0082` collision (NFC vs PIPE END)** — must be resolved before #1 can even be added. | 🔴 Critical | `opendisplay_protocol.h:27` — move NFC to an unused opcode (e.g. `0x0083`) | **S** |
| **3** | **`0x0051` ENTER_DFU acks success but does nothing** — and there is no OTA/DFU path at all (no MCUboot). Field devices are unupdatable. | 🔴 Critical | Add MCUboot + `CONFIG_MCUMGR`/SMP-over-BLE to `zephyr/prj.conf` + sysbuild; until then make `0x0051` return `{0xFF,0x51}` | **S** (honest NACK) / **L** (real DFU) |
| **4** | **Buzzer frequency table** — every melody plays at the wrong pitch today. | 🟠 High | `buzzer_control.cpp:19-113` (`kBuzzerCentiHzTable`, `buzzer_fold_index`, `buzzer_index_to_centihz`), `buzzer_control.h:27-92` (note enum), `:17` (30 s cap) | **S** |
| **5** | **GRAY16 bpp = 1 vs 4** — a 16-gray push is sized 4× differently on the two firmwares; one of them is wrong for any given client. Decide which, then align both. | 🟠 High | `display_service.cpp:1404-1415` vs `opendisplay_display_color.c:27-39` | **S** (once the spec question is settled) |
| **6** | **`0x71` auto-complete** — reference refreshes automatically once the byte count is reached; nRF54 waits for `0x72`. A client written against the reference can hang. | 🟠 High | `display_service.cpp:1982-1983` | **S** |
| **7** | **Deep sleep + wake-on-button + sleep timers** — battery devices never sleep on nRF54. This is the single biggest *runtime* (battery-life) gap. | 🟠 High | `main.cpp:262-420, 468+` (`enterDeepSleep`, `pollActivity`, `minWakeHoldActive`), `wake_button.cpp`, `structs.h:56,60,72` → Zephyr `pm_state_force` / `sys_poweroff` + GPIO wake | **L** |
| **8** | **Power latch (MOSFET + D-FF)** — latched-battery boards cannot power themselves off; `0x0052` and long-press shutdown are dead. Requires adding `pwr_pin_2`/`pwr_pin_3` to `SystemConfig` first. | 🟠 High | `power_latch.cpp` (211 L), `power_latch.h`, `structs.h:31-32`, `device_control.cpp:44-81, 717-725` | **M** |
| **9** | **EPD keep-alive / panel power session** — every push pays a full cold bring-up (~900 ms rail + init). Requires adding `screen_timeout_seconds` to `PowerOption`. | 🟡 Medium | `display_service.cpp:160-317`, `structs.h:61-63`, `docs/epd-panel-power-session.md` | **M** |
| **10** | **`transmission_modes` bit4 (PIPE_WRITE) unknown to nRF54** — configs will advertise a capability the device can't honor. Follows from #1. | 🟡 Medium | `structs.h:100` | **S** |
| **11** | **Missing panels 0x47/0x48/0x4A/0x4C; wrong-substitute 0x49/0x4B** | 🟡 Medium | Vendor EP368/EP368_4GRAY/EP40_SPECTRA/EP27/EP27_4GRAY/EP213ZZ init sequences into `third_party/bb_epaper` | **M** |
| **12** | **Config reload does not re-init LED/buzzer/touch/button/sensors** — a pin change needs a reboot. | 🟡 Medium | `communication.cpp:27-43` pattern; call the `*_init()` functions from `opendisplay_ble_reload_config_from_nvm()` (`opendisplay_ble.c:606`) | **S** |
| **13** | **Chip temperature read once at boot** — MSD temp byte is frozen. | 🟡 Medium | `opendisplay_ble.c:224-235, 689` — move `read_chip_temperature_once()` into `update_msd_payload()` | **S** |
| **14** | **Long-press power-off buttons** (`power_off_flags`, `power_off_hold_sec`) | 🟡 Medium | `device_control.cpp:44-81`, `structs.h:273-275` | **S** (after #8) |
| **15** | **`MAX_BUTTONS` 8 vs 32; buttons polled not ISR-latched** | 🟡 Medium | `opendisplay_button.c:11, 104-106` | **S** |
| **16** | **Explicit 2M-PHY / DLE-251 request on connect** — throughput left on the table if the central doesn't ask. | 🟡 Medium | `ble_init.cpp:123-144` → `bt_conn_le_phy_update()` + `bt_conn_le_data_len_update()` | **S** |
| **17** | **Pipe msgq depth 8** (vs ESP32's 33) — image frames can be dropped under burst. | 🟡 Medium | `opendisplay_pipe.c:82` | **S** |
| **18** | **`DEVICE_FLAG_XIAOINIT` / `WS_PP_INIT` unhandled** | 🟢 Low | `config_parser.cpp:847-856`, `main.h:201-202` | **S** |
| **19** | **Reset-pin factory reset** (`SECURITY_FLAG_RESET_PIN_*`) | 🟢 Low | `main.h:231` `checkResetPin()` | **S** |
| **20** | **Partial-write 15-min watchdog** | 🟢 Low | `display_service.cpp:357-366` | **S** |
| **21** | **Seeed_GFX / IT8951 13.3" panels (`panel_ic 3000/3001`)** | 🟢 Low | `lib/Seeed_GFX/`, `display_seeed_gfx.cpp` — very large port | **L** |
| **22** | **AXP2101 PMIC** | 🟢 Low | `display_service.cpp:825-1100` | **M** |
| **23** | **Touch suspend before EPD refresh** (resume exists, suspend doesn't) | 🟢 Low | `display_service.cpp:1342, 1808` | **S** |
| **24** | **UF2 packaging / `provision_firmware.py` / `convert_logo.py`** | 🟢 Low | `scripts/nrf_uf2_post.py`, `tools/` | **S** |

---

## 4. 🚨 DIVERGENCES — both sides implement it, DIFFERENTLY

These are the dangerous ones. Each will *appear* to work and then produce wrong behaviour.

### 🔴 D1 — BUZZER FREQUENCY MAPPING IS COMPLETELY DIFFERENT

The nRF54 file's own header comment claims the wire semantics are *"byte-for-byte identical to the reference handleBuzzerActivate"* (`opendisplay_buzzer.c:12-27`). **It is not.** nRF54 was ported from a pre-music version of the reference.

- **Reference** (`buzzer_control.cpp:19-113`): a 256-entry **quarter-tone lookup table**, `centiHz[i] = round(100 · 13.75 · 2^(i/24))`. `idx 120 = A4 = 440.00 Hz` exactly. Out-of-range indices are **octave-folded** into the playable window `[117, 234]` (403 Hz … 11 840 Hz), preserving pitch class. `buzzer_control.h:27-92` exports a `BuzzerNote` enum (`nA4`, `nC5`, …) whose values *are* the wire indices.
- **nRF54** (`opendisplay_buzzer.c:74-82`): `hz = 400 + (11600 · (idx − 1)) / 254` — a **linear ramp**.

Concretely, a host sending `nA4` (index 120, intending 440 Hz):
- reference plays **440 Hz** ✅
- nRF54 plays `400 + 11600·119/254` = **~5834 Hz** ❌

**Every melody transmitted by a py-opendisplay/Home-Assistant client is unrecognisable noise on nRF54.** Additionally the total-duration cap is **30 000 ms** on the reference vs **5 000 ms** on nRF54 (`buzzer_control.cpp:17` vs `opendisplay_buzzer.c:36`), so any melody longer than 5 s is silently truncated.

### 🔴 D2 — GRAY16 (color_scheme 6) BITS-PER-PIXEL: 1 vs 4

- **Reference** `getBitsPerPixel()` (`display_service.cpp:1404-1415`) has **no case for scheme 6** — it falls through to `return 1`. Total bytes = `ceil(w/8) · h`.
- **nRF54** `opendisplay_color_bits_per_pixel()` (`opendisplay_display_color.c:35-37`) returns **4**. Total bytes = `ceil(w·4/8) · h` — **4× larger**.

The nRF54 source explicitly calls the reference "the outlier/bug" (`opendisplay_display_color.c:16-18`) and claims py-opendisplay sends 4bpp. **Whoever is right, the two firmwares cannot both be right**, and a compressed GRAY16 push will be rejected on one of them with a `zlib size != total` mismatch (`opendisplay_display.cpp:787-792` / `display_service.cpp:1819`). Resolve against the actual client encoder and align.

### 🔴 D3 — OPCODE `0x0082` COLLISION: PIPE_WRITE_END vs NFC_ENDPOINT

- **Reference**: `0x0082` = PIPE_WRITE END (`communication.cpp:637-640`, `display_service.cpp:2526`), carries `[refresh_mode:1][new_etag:4 BE]` and drives the final refresh.
- **nRF54**: `0x0082` = `CMD_NFC_ENDPOINT` (`opendisplay_protocol.h:27`, `opendisplay_pipe.c:1301-1302`).

A client that finishes a pipe transfer by sending `0x0082 [refresh][etag]` to an nRF54 device will hit `handle_nfc_endpoint()`, which reads `payload[0]` as an NFC sub-command. `refresh_mode` values 0/1/2 aren't valid NFC sub-ops (0x00/0x01/0x10/0x11/0x12) except `0x00` — which means "**NFC read**", replying `{FF, 82, FF, 02}` because the NFC backend is a stub. **The image never refreshes and the client gets a nonsense error.** This single line makes it impossible to add PIPE_WRITE to nRF54 without first relocating NFC.

### 🔴 D4 — `0x0051` ENTER_DFU CLAIMS SUCCESS, DOES NOTHING

`opendisplay_pipe.c:1241-1247` sends `{0x00, 0x51}` (success) **before** calling `opendisplay_ble_schedule_dfu()`, which is `printf("[OD] DFU not implemented on nRF54 yet")` (`opendisplay_ble.c:725-728`). The client believes the device is entering the bootloader and will start pushing a firmware image into a void.

Note the contrast with `0x0052` DEEP_SLEEP, which the port handles *correctly*: it deliberately sends **no response** (`opendisplay_pipe.c:1248-1254`, with a comment explaining it's matching the nRF52840's "don't advertise unsupported" behaviour). `0x0051` should do the same, or return `{0xFF, 0x51}`.

### 🟠 D5 — DEVICE NAME LIVES IN A DIFFERENT ADVERTISING PACKET

- **Reference nRF52840**: `Bluefruit.Advertising.addName()` (`ble_init.cpp:199`) → name is in the **ADV packet**.
- **Reference ESP32**: `advertisementData->setName(...)` + `setScanResponse(false)` (`ble_init.cpp:318, 325`) → name in **ADV packet**, no scan response at all.
- **nRF54**: name is `sd_buf[0]` → **scan response only** (`opendisplay_ble.c:320, 331, 568`).

Any host doing a **passive** scan, or filtering by name without an active scan, will see the nRF54 device as unnamed. `0x2446` service UUID placement diverges the same way (ADV on ESP32, scan response on nRF54, absent on nRF52840).

### 🟠 D6 — COMPRESSED WRITES GATED ON `transmission_modes` BIT 0 ON nRF54 ONLY

- **Reference**: `handleDirectWriteStart` (`display_service.cpp:1815`) treats `len >= 4` as "compressed" and proceeds. The ZIPXL bit is **only logged** (`config_parser.cpp:713`), never enforced.
- **nRF54**: `opendisplay_display.cpp:776-781` **hard-rejects** with `-4` unless `transmission_modes & TRANSMISSION_MODE_STREAMING_DECOMPRESSION` (bit 0).

A device config that omits bit 0 works fine on the reference and **fails every compressed image push on nRF54** with `{0xFF, 0x70}`. Same for `0x76` partial (`opendisplay_display.cpp:532-539`).

### 🟠 D7 — `0x0071` AUTO-COMPLETE

Reference `handleDirectWriteData` calls `handleDirectWriteEnd(nullptr, 0)` the moment `directWriteBytesWritten >= directWriteTotalBytes` (`display_service.cpp:1982-1983`) — the panel refreshes **without** an explicit `0x72`. nRF54 instead logs and ignores trailing bytes (`opendisplay_display.cpp:844-854`) and requires the explicit END. A client relying on auto-complete waits forever for the `{0x00,0x73}` that never comes.

### 🟡 D8 — CONFIG STRUCT FIELD DRIFT (silent misinterpretation risk)

`SystemConfig`, `PowerOption`, `BinaryInputs` and `DisplayConfig` are the **same wire size** on both sides but nRF54 maps several tail fields into `reserved[]`:

| Struct | Reference field | nRF54 |
|---|---|---|
| `SystemConfig` | `pwr_pin_2`, `pwr_pin_3` (`structs.h:31-32`) | inside `reserved[17]` (`opendisplay_structs.h:12`) |
| `PowerOption` | `min_wake_time_seconds` (u16), `screen_timeout_seconds` (u8) (`structs.h:60-63`) | inside `reserved[7]` (`opendisplay_structs.h:42`) |
| `BinaryInputs` | `power_off_flags`, `power_off_hold_sec` (`structs.h:273-275`) | inside `reserved[14]` (`opendisplay_structs.h:140`) |
| `DisplayConfig` | `full_update_mC` (u16) (`structs.h:192`) | inside `reserved[15]` (`opendisplay_structs.h:76`) |
| `ManufacturerData` | `reserved[18]` (`structs.h:40`) | `simple_config_driver_index`, `simple_config_display_index`, `simple_config_power_index`, `configured_at[6]` (`opendisplay_structs.h:19-22`) |

Round-tripping a config through nRF54 preserves the bytes (they're copied wholesale), so no data is lost — but the **features keyed off those fields are silently inert**, and the `ManufacturerData` case means the two firmwares disagree about what the *same bytes mean*.

### 🟡 D9 — GPIO PIN ENCODING

nRF54 uses a compact `(port << 4) | pin` byte throughout (`nrf54_gpio.h`, `opendisplay_structs.h:176` comment). The reference uses raw Arduino/GPIO numbers. **A config blob authored for an nRF52840/ESP32 device will drive completely wrong pins on nRF54** (and vice versa). This is arguably necessary, but it means configs are *not* portable across the two firmwares despite identical packet formats — worth documenting loudly in the toolbox.

---

## 5. nRF54-only additions

1. **BLE Channel Sounding + RAS ranging (reflector role)** — `src/opendisplay_cs.c` (210 L), `src/opendisplay_cs.h`, `zephyr/prj_cs.conf`. Compiled in unconditionally, runtime-gated on the new `DEVICE_FLAG_CHANNEL_SOUNDING` (`device_flags` bit 5, `opendisplay_device_flags.h:11`), parsed at `opendisplay_config_parser.c:206`. When enabled, advertises `BT_UUID_RANGING_SERVICE` in the scan response (`opendisplay_cs.c:37-56`) and hooks connect/disconnect (`opendisplay_ble.c:388-390, 406`). Configured as `REFLECTOR_ONLY`, 2 antenna paths, Mode-3 off. **No reference equivalent.**
2. **NFC endpoint command + `nfc_config` packet `0x2A`** — full read/write/chunked-write protocol (`opendisplay_pipe.c:1023-1176`, 32-B config struct at `opendisplay_structs.h:183-201`, record types at `opendisplay_constants.h:33-38`). Currently a **stub** (`opendisplay_ble.c:735-751` both return `false`), and it collides with PIPE_WRITE END — see D3.
3. **Deferred command processing** — GATT writes are copied into `K_MSGQ` on the BT RX thread and dispatched on the main thread (`opendisplay_pipe.c:76-82, 1376-1425`), with a connection-generation counter that drops stale frames. Architecturally superior to the reference's in-callback execution; the reference only does this on ESP32.
4. **GPIO parking on power-down** — `opendisplay_display_park_pins()` (`opendisplay_display.cpp:84-102`) tri-states every panel signal before cutting the rail, preventing back-feed through the EPD's protection diodes.
5. **Explicit per-packet config length validation table** — `opendisplay_config_parser.c:123-137`; the reference infers lengths.
6. **`simple_config_*` fields in `ManufacturerData`** — `opendisplay_structs.h:19-22` (driver/display/power preset indices + a 6-byte `configured_at` timestamp).
7. **`CONFIG_BT_SMP` / bonding compiled in** (`zephyr/prj_ncs.conf`) — unused by the GATT permissions today, but available.
8. **RTT logging by default** (`zephyr/prj.conf`), with UART as an opt-in profile — a deliberate battery-safety choice the reference doesn't make.
