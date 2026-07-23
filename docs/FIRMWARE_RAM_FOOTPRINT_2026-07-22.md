# Firmware Static RAM Footprint vs. RAM Limits — Per Build Target

**Date:** 2026-07-22
**Repo:** `/home/davelee/opendisplay/Firmware` (branch `feat/per-mac-ble-lock`)
**Purpose:** Background for a decision to add a **WiFi/TLS streaming transport**, scoped to **ESP32-C6 and ESP32-S3 only** (C3 excluded from WiFi; nRF52840 has no WiFi). The crux is RAM headroom on the **no-PSRAM C6** target.
**Method:** Measurements are from **existing linked ELF/`.map` artifacts** already on disk in `.pio/build/` (built 2026-07-19/20). No fresh `pio run` was performed. Sizes come from the per-arch `*-size -A` tools reading each `firmware.elf`, cross-checked against the `Memory Configuration` block in each `firmware.map`. Runtime WiFi/BLE/TLS heap figures are **estimates** (clearly labeled) since they are allocated dynamically and cannot be read from a static image.

---

## Executive summary

**Does C6-N4 have room for concurrent BLE + WiFi + TLS streaming? — Marginal / high-risk with defaults; feasible only with a shrunk TLS record buffer.**

- The ESP32-C6-N4 has **no PSRAM**. Its linker gives the app a **single unified 451.6 KB SRAM pool** (`sram_seg`). The **measured** static footprint (IRAM code + DRAM data + DRAM bss) is **235.8 KB (52%)**, leaving **~211 KB** for *all* runtime heap **and** FreeRTOS task stacks at boot.
- That ~211 KB must then absorb, at runtime and concurrently: the **WiFi driver + lwIP** (~50–70 KB heap), the **NimBLE host+controller** working heap (~30–50 KB), FreeRTOS/Arduino task stacks (WiFi, BLE, tcpip, loopTask ≈ 30–50 KB combined), plus the app's own working set (String churn, response/command queues, decompression scratch).
- A **TLS session (mbedTLS/WiFiClientSecure)** adds a **peak ~25–40 KB**, dominated by the **16 KB input record buffer** (`MBEDTLS_SSL_IN_CONTENT_LEN`) plus the handshake certificate-chain parse spike.
- Net: with **default** mbedTLS buffer sizes, BLE + WiFi + TLS on C6-N4 lands in the **~40–90 KB free-heap** band with a fragmentation-sensitive handshake spike on top — **tight, and prone to intermittent OOM**. It becomes **comfortable only if** `MBEDTLS_SSL_IN/OUT_CONTENT_LEN` are cut to ~4–6 KB (with RFC 6066 Max-Fragment-Length negotiated) and BLE and TLS transfers are not both mid-flight.
- **S3 changes the picture decisively.** The S3-R8 targets carry **8 MB OPI PSRAM**. The large e-paper framebuffer already lives in PSRAM, and TLS record buffers can be pushed to PSRAM as well. S3 has **~135 KB free internal DRAM** at boot *plus* ~7.9 MB PSRAM — WiFi + BLE + TLS is **comfortable** there.

**One correction to a common assumption:** the 32 KB zlib window is **NOT** in play on C6/S3. On every ESP32 env except `esp32-s3-E1004`, `OPENDISPLAY_ZLIB_WINDOW_BITS` is commented out, so the library default of **9 bits = 512 bytes** applies. The window is a **negligible** RAM consumer on C6-N4 (512 B, heap-allocated), not 32 KB. See "Big consumers → zlib" below.

---

## Per-target RAM ceilings

| Env | Chip | Internal SRAM (app budget) | PSRAM | WiFi? | Partition | Notes |
|-----|------|---------------------------|-------|-------|-----------|-------|
| `nrf52840custom` | nRF52840 | 256 KB total; ~49 KB reserved by SoftDevice S140 → ~207 KB app | none | **No** | UF2 | BLE = Adafruit Bluefruit/SoftDevice |
| `esp32-c6-N4` | ESP32-C6 | **512 KB HP SRAM** (+16 KB LP); app `sram_seg` = **451.6 KB** unified I+D | **none** | **Yes (in scope)** | huge_app | **Crux target.** RISC-V, NimBLE |
| `esp32-s3-N16R8` | ESP32-S3 | 512 KB SRAM; DRAM budget **341.8 KB**, IRAM budget 358.1 KB | **8 MB OPI** | **Yes (in scope)** | 16 MB | Seeed_GFX sprite path enabled |
| `esp32-s3-N8R8` | ESP32-S3 | same as N16R8 | 8 MB OPI | Yes | 8 MB | same footprint |
| `esp32-s3-N32R8` | ESP32-S3 | same as N16R8 | 8 MB OPI | Yes | 32 MB | same footprint |
| `esp32-s3-N32R8-extuart` | ESP32-S3 | same | 8 MB OPI | Yes | 32 MB | Seeed_GFX **off** (bb_epaper only) |
| `esp32-s3-E1004` | ESP32-S3 | same | 8 MB OPI | Yes | 32 MB | **only env with 32 KB zlib window** |
| `esp32-s3-N16R8-extuart` | ESP32-S3 | same | 8 MB OPI | Yes | 16 MB | Seeed_GFX on + UART log |
| `esp32-c3-N4` | ESP32-C3 | ~400 KB SRAM; DRAM budget **321.8 KB** | none | Excluded | huge_app | RISC-V |
| `esp32-c3-N16` | ESP32-C3 | same as c3-N4 | none | Excluded | 16 MB | identical footprint |
| `esp32-N4` | ESP32 (classic) | 320 KB DRAM, heavily fragmented; primary `dram0_0_seg` = 124.6 KB | none | Excluded | huge_app | needs `PIPE_SMALL_DRAM_WINDOW` to link |

Chip-level notes:
- **ESP32-C6:** 512 KB HP SRAM + 16 KB LP SRAM (`lp_ram_seg` 0x3FD8 = 16.3 KB). Uses a **unified** code+data SRAM segment (`sram_seg` origin 0x40800000, length **0x6E610 = 451,600 B**); ROM/bootloader/cache reserve the balance up to 512 KB. No external PSRAM on this board.
- **ESP32-S3:** 512 KB SRAM split into `iram0_0_seg` (0x57700 = 358,144 B) and `dram0_0_seg` (0x53700 = **341,760 B**), plus `extern_ram_seg` mapping the **8 MB** OPI PSRAM. `board_upload.maximum_ram_size = 327680` in `platformio.ini` is a flash-tool DRAM hint, close to the true DRAM budget.
- **ESP32-C3:** ~400 KB SRAM; `dram0_0_seg` = 0x4E710 = **321,808 B**.
- **nRF52840:** 256 KB RAM (0x2000_0000–0x2004_0000). Measured `.bss` starts at **0x2000C00C**, i.e. SoftDevice reserves the low **~49 KB**.

---

## Measured static footprint per target

DRAM/SRAM figures are what actually consumes physical internal RAM. On S3/C3, `.dram0.dummy` is a linker padding section that **reserves DRAM address space** to align the flash-mapped `.rodata` cache window, so it is counted against the DRAM budget. On C6 the code (`.iram0.text`) shares the same physical pool as data, so it is counted too. The flat `size` "bss" column is **misleading** on these parts because it also sums non-RAM address-space reservations (`.flash_rodata_dummy`, `.ext_ram.dummy`) — those are excluded here.

| Env | IRAM/code in SRAM | DRAM .dummy | DRAM .data | DRAM .bss | **SRAM static total** | App SRAM budget | **Free at boot** |
|-----|------------------:|------------:|-----------:|----------:|----------------------:|----------------:|------------------:|
| **esp32-c6-N4** | 122,986 | — (unified) | 17,372 | 95,440 | **235,798 (52%)** | 451,600 | **~215,802 (~211 KB)** |
| **esp32-s3-N16R8** (DRAM only) | (100,671 IRAM sep.) | 85,504 | 24,299 | 93,080 | **202,883 DRAM (59%)** | 341,760 DRAM | **~138,877 DRAM (~135 KB)** + ~7.9 MB PSRAM |
| esp32-c3-N4 | 88,738 | 89,088 | 15,617 | 91,152 | 195,857 DRAM (61%) | 321,808 DRAM | ~125,951 (~123 KB) |
| esp32-N4 (classic) | 121,959 | — | 26,148 | 90,128 | 116,276 DRAM | ~124,580 primary seg | very tight (needs shrunk PIPE window) |
| nrf52840custom | 282,920 flash text | — | 908 (data) | 50,408 bss | ~50 KB bss RAM | ~207 KB post-SoftDevice | ~157 KB heap (`.heap` = 184,204 reserved) |

Notes:
- **C6-N4 free heap** (~211 KB) is measured *before* any FreeRTOS task stacks or WiFi/BLE runtime heap are carved out. Task stacks (loopTask ~8 KB, tcpip ~3 KB, WiFi/BLE tasks, event/timer tasks) come out of this pool at runtime.
- **S3** reports IRAM (`.iram0.text` 100,671 + vectors 1,028) in a *separate* segment from DRAM; only DRAM is the scarce resource for buffers, and it has 8 MB PSRAM behind it.
- All S3-R8 envs measure within a few KB of each other (bss ~93 KB, data ~24 KB); the `-extuart` variants shave a little by dropping native-USB-CDC.

---

## Big RAM consumers (with file:line evidence)

### 1. E-paper framebuffer — **two distinct paths**
- **bb_epaper (default; C6, C3, nRF, S3-extuart):** streams **straight to the panel over SPI** — there is **no full framebuffer in SRAM**. Decompressed/plane bytes are pushed in chunks via `bbepWriteData()`; see `streamGray4Bytes()` (`src/display_service.cpp:1921`) and `directWriteSinkBytes()` (`src/display_service.cpp:1941`), both of which call `bbepWriteData(&bbep, …)` on ≤2048-byte chunks. The only large buffer on this path is the **2048-byte** `decompressionChunk` (`src/main.h:106`, `OPENDISPLAY_DECOMPRESSION_CHUNK_SIZE` = 2048 in `display_service.h:7`). **This is why C6 can drive large panels at all.**
- **Seeed_GFX / TFT_eSprite (S3-only, `-DOPENDISPLAY_SEEED_GFX`):** **does** allocate a full sprite framebuffer. `EPaper::initGrayMode(16)` → `createSprite()` → `callocSprite()` (`lib/Seeed_GFX/Extensions/Sprite.cpp:153`). Frame size is `(w*h+1)/2` for 4-bpp gray (`src/display_seeed_gfx.cpp:87` `fb_byte_size`). For the ED103TC2 **1872×1404** 4-gray panel that is **1,872×1,404/2 ≈ 1.31 MB**. **Crucially, this allocation prefers PSRAM:** `callocSprite` uses `ps_calloc(...)` when `psramFound() && _psram_enable` (`Sprite.cpp:191–194`), falling back to internal `calloc` only without PSRAM. On the R8 boards it lands in the 8 MB PSRAM, **not** internal DRAM. This path is compiled out on C6/C3 (`lib_ignore = Seeed_GFX`).

### 2. WiFi/TCP receive buffer — ESP32 only
- **`uint8_t tcpReceiveBuffer[8192]`** (`src/main.h:152`, declared `extern` in `src/wifi_service.cpp:31`). **8 KB static** in `.bss`, present on every ESP32 env (inside `#ifdef TARGET_ESP32`). This is the LAN framing reassembly buffer; a TLS transport would likely need a *second* plaintext buffer of similar size on top.
- **ESP-IDF WiFi driver + lwIP:** allocated from heap at `WiFi.begin()`, **not** in the static image. Estimate **~50–70 KB** (WiFi RX/TX buffers, lwIP PCBs/pbufs, netif). Present in the linked code already (`WiFi.h`, `WiFiServer`, `ESPmDNS` are compiled in).

### 3. BLE stack
- **ESP32 (C6/S3/C3): NimBLE-Arduino** (`h2zero/NimBLE-Arduino@^2.5.0`, `platformio.ini:8`). Its host+controller code is in the measured `.iram0.text`/`.bss`; its **working heap** (connection contexts, ACL buffers, att) is runtime, estimate **~30–50 KB** when a link is active. Static callback objects avoid dynamic alloc (`MyBLEServerCallbacks staticServerCallbacks`, `src/main.h:394`).
- **nRF52840: Adafruit Bluefruit + SoftDevice S140** — the ~49 KB low-RAM reservation measured above. No WiFi coexistence concern (no WiFi).

### 4. zlib decompression window — **512 B, not 32 KB, on C6/S3**
- Window declared in `lib/uzlib/src/od_zlib_stream.c:157–160`: heap pointer when `OPENDISPLAY_ZLIB_USE_HEAP_WINDOW`, else `uint8_t window[OPENDISPLAY_ZLIB_WINDOW_SIZE]` inside a **file-scope `static` state struct** (`static od_zlib_stream_state_t s;`, line 168).
- Size = `1 << OPENDISPLAY_ZLIB_WINDOW_BITS`, default **9 → 512 B** (`uzlib.h:22`, `:29`).
- **Per-env:** all ESP32 envs set `OPENDISPLAY_ZLIB_USE_HEAP_WINDOW=1` but leave `WINDOW_BITS` **commented out** → **512 B on the heap**. Only **`esp32-s3-E1004`** sets `-DOPENDISPLAY_ZLIB_WINDOW_BITS=15` → **32 KB heap window** (`platformio.ini:148`). `nrf52840custom` uses `USE_HEAP_WINDOW=0` → the 512 B window sits in static bss. **Takeaway: on C6-N4 the zlib window costs 512 B, negligible for the WiFi decision.**

### 5. Config / command / response buffers (ESP32, mostly static)
- **PIPE_WRITE reorder queue:** `PIPE_REORDER_SLOTS × PIPE_REORDER_SLOT_SIZE` = **33 × 248 ≈ 8.0 KB** on C6/S3 (`src/structs.h:50–54`). Shrunk to 17 × 248 ≈ 4.1 KB only on classic `esp32-N4` via `PIPE_SMALL_DRAM_WINDOW` (`structs.h:45–48`).
- **Command queue:** `CommandQueueItem commandQueue[33]`, `MAX_COMMAND_SIZE = 256` → **~8.4 KB** (`src/main.h:369–370, 384`).
- **Response queue:** `ResponseQueueItem responseQueue[10]`, `MAX_RESPONSE_SIZE = 512` → **~5.1 KB** (`src/main.h:363–378`).
- **Chunked config buffer:** `chunked_write_state_t.buffer[MAX_CONFIG_SIZE]` (`src/main.h:277`), plus stack buffers `configData[4096]` (`communication.cpp:351`) and `buffer[4096]` (`communication.cpp:54`) — 4 KB stack transients on the config path.
- Misc static: `staticWhiteRow[680]` + `staticRowBuffer[960]` + `staticLineBuffer[256]` (`main.h:134–136`), `bleResponseBuffer[94]`, `configReadResponseBuffer[128]`.

### 6. Encryption state / scratch
- **`EncryptionSession`** (`src/encryption_state.h:8`): includes `replay_window[64]` (uint64 → **512 B**) plus 16-byte keys/nonces — a few hundred bytes static (`EncryptionSession encryptionSession`, `main.h:289`).
- **Secure-erase scratch `static uint8_t zeroBuffer[512]`** (`encryption.cpp:756`) — 512 B static, used only during config wipe.
- Function-static crypto scratch: `decrypted_with_length[512]`, `payload_with_length[513]` (`encryption.cpp:662, 694`); `encrypted_response[600]`, `decrypted_data[512]`, `plaintext[512]` (`communication.cpp:165, 616, 587`). These are **static** (persist), collectively ~3 KB, already in the measured bss.

---

## TLS + WiFi + BLE headroom budget

### mbedTLS cost primer
`WiFiClientSecure` on Arduino-ESP32 uses ESP-IDF's `esp-tls` → mbedTLS. Per TLS session, the dominant heap costs are:
- **Input record buffer** `MBEDTLS_SSL_IN_CONTENT_LEN` — default **16 KB** (must hold a full TLS record unless RFC 6066 Max-Fragment-Length is negotiated down).
- **Output record buffer** `MBEDTLS_SSL_OUT_CONTENT_LEN` — ESP-IDF default **~4 KB** (pure-mbedTLS default is 16 KB).
- **Handshake context + session + certificate-chain parse** — a **transient peak** of ~6–12 KB (bignum, cert DER, key exchange), released after handshake.
- Both content lengths are **tunable** via sdkconfig / build flags.

Rough per-session steady-state ≈ **20–24 KB**; handshake **peak ≈ 30–40 KB**.

### C6-N4 budget (no PSRAM) — the crux

| Item | Estimate | Running free (of ~211 KB boot) |
|------|---------:|-------------------------------:|
| Boot free heap (measured static → remaining `sram_seg`) | — | **~211 KB** |
| FreeRTOS/Arduino task stacks (loop 8K, tcpip 3K, timer/event, idle) | −25 to −40 KB | ~171–186 KB |
| NimBLE host+controller working heap (link up) | −30 to −50 KB | ~121–156 KB |
| WiFi driver + lwIP (STA associated, TCP listener) | −50 to −70 KB | ~51–106 KB |
| **Subtotal: BLE + WiFi coexistence** | | **~50–105 KB free** |
| TLS session steady-state (16K in + 4K out + ctx) | −20 to −24 KB | ~26–85 KB |
| TLS handshake cert-chain **peak** (transient) | −8 to −16 KB spike | **~10–70 KB at the spike** |

**Verdict (C6-N4): TIGHT / high-risk with defaults.** The BLE+WiFi coexistence floor alone (~50–105 KB) is already the known-tight regime for a single-bank 512 KB ESP32. Layering a default-sized TLS session on top pushes the handshake spike toward the **10–30 KB** danger zone, where heap fragmentation (not just gross free bytes) causes intermittent `mbedtls` alloc failures and dropped connections. **Feasible only with mitigations:**
- Set `MBEDTLS_SSL_IN_CONTENT_LEN` / `OUT_CONTENT_LEN` to **~4–6 KB** and negotiate RFC 6066 MFL client-side (saves ~12–20 KB).
- Avoid a *second* 8 KB plaintext buffer — reuse `tcpReceiveBuffer` or stream through the existing 2 KB `decompressionChunk`.
- Do not run a BLE bulk transfer and a TLS handshake simultaneously (serialize transport bring-up).
- Consider ChaCha20-Poly1305 ciphersuites (no bignum RAM spike vs. RSA cert verify), and validate with `esp_get_free_heap_size()` / `heap_caps_get_largest_free_block()` on real hardware — the largest-free-block metric, not total free, is the true gate.

### S3-R8 budget (8 MB PSRAM) — comfortable

| Item | Placement | Effect on internal DRAM |
|------|-----------|-------------------------|
| Boot free internal DRAM (measured) | — | **~135 KB** |
| E-paper sprite framebuffer (~1.31 MB) | **PSRAM** (`ps_calloc`) | 0 |
| WiFi + lwIP + NimBLE working heap | internal DRAM | −80 to −120 KB |
| TLS record buffers (16K+4K) | **can be forced to PSRAM** via `CONFIG_MBEDTLS_EXTERNAL_MEM_ALLOC` / SPIRAM malloc threshold | ~0 internal |
| TLS handshake context / bignum | internal DRAM (small) | −6 to −12 KB transient |

**Verdict (S3): COMFORTABLE.** With 8 MB PSRAM absorbing the framebuffer and (optionally) the TLS content buffers, the ~135 KB internal DRAM comfortably holds WiFi+BLE control structures and the handshake transient. **Caveat — be precise about PSRAM limits:** WiFi/BLE **DMA descriptors and DMA-capable buffers must be in internal DRAM** (`MALLOC_CAP_DMA`), and time-critical ISR paths cannot touch PSRAM; only the *large, non-DMA* TLS content buffers and the framebuffer belong in PSRAM. So PSRAM eases the buffer pressure but does **not** eliminate the internal-DRAM demand of the WiFi/BLE stacks themselves.

---

## Recommendations & risks

1. **C6-N4 is the gating target — do not ship WiFi+TLS on it with default mbedTLS sizes.** Prototype with `MBEDTLS_SSL_IN/OUT_CONTENT_LEN` ≈ 4–6 KB + negotiated MFL, and gate merge on a **hardware** measurement of `heap_caps_get_largest_free_block(MALLOC_CAP_8BIT)` with BLE link up, WiFi associated, and a TLS session active simultaneously. Target ≥ 32 KB largest-free-block headroom after handshake.
2. **Prefer a PSRAM-equipped S3 for the first WiFi/TLS rollout.** It removes essentially all of the RAM risk (framebuffer + TLS buffers to PSRAM), at the cost of the part not being the cheapest option.
3. **Reuse existing buffers.** The 8 KB `tcpReceiveBuffer` (`main.h:152`) and 2 KB `decompressionChunk` are already static; route TLS plaintext through them rather than adding a third large buffer, especially on C6.
4. **Watch the config path stack transients** (`configData[4096]`, `buffer[4096]` in `communication.cpp`) — 4 KB stack frames on a task whose stack must also survive a TLS handshake could overflow; verify task stack sizing if TLS runs on the same task.
5. **zlib is a non-issue on C6/S3** (512 B). Only `esp32-s3-E1004` pays 32 KB, and it has PSRAM. Don't over-account for it.
6. **Coexistence is the real constraint, not gross code size.** Flash is ample on every WiFi target (huge_app / ≥8 MB). The decision rests entirely on **internal SRAM free heap + largest-free-block** under simultaneous BLE + WiFi + TLS load — a runtime property that must be measured on the C6-N4 board, not inferred from the static image alone.

### Measurement provenance
All static numbers above are from `*-size -A firmware.elf` on the on-disk 2026-07-19/20 build artifacts and the `Memory Configuration` block of each `firmware.map`. Runtime WiFi/BLE/TLS/task-stack figures are **estimates** from ESP-IDF/mbedTLS defaults and are flagged as such; margin of error on the runtime estimates is roughly ±30%, which is precisely why item (1) mandates a hardware heap measurement before committing the C6-N4 target.
