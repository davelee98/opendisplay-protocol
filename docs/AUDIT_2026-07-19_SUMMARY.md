# OpenDisplay toolchain audit — cross-repo SUMMARY

**Date:** 2026-07-19 (synthesis 2026-07-20) · **Scope:** end-to-end static audit of the whole OpenDisplay pipeline (ODL JSON → odl-renderer → epaper-dithering → py-opendisplay → four firmware targets), plus Home_Assistant_Integration (top consumer), opendisplay.org (website), and opendisplay-protocol (canonical wire spec).
**Method:** report-only static analysis. No builds, no test runs, no installs, no hardware. "Confirmed" = a traced code path; "Plausible" = a reasoned failure mode not fully traced to a crash.

Per-repo detail lives in the ten companion docs in this directory:
[Firmware](AUDIT_2026-07-19_Firmware.md) ·
[Firmware_NRF54](AUDIT_2026-07-19_Firmware_NRF54.md) ·
[Firmware_Silabs](AUDIT_2026-07-19_Firmware_Silabs.md) ·
[Firmware_NRF](AUDIT_2026-07-19_Firmware_NRF.md) ·
[py-opendisplay](AUDIT_2026-07-19_py-opendisplay.md) ·
[epaper-dithering](AUDIT_2026-07-19_epaper-dithering.md) ·
[odl-renderer](AUDIT_2026-07-19_odl-renderer.md) ·
[Home_Assistant_Integration](AUDIT_2026-07-19_Home_Assistant_Integration.md) ·
[opendisplay-protocol](AUDIT_2026-07-19_opendisplay-protocol.md) ·
[opendisplay.org](AUDIT_2026-07-19_opendisplay.org.md)

---

## 1. Executive summary

The toolchain is architecturally coherent and, in the parts that were built carefully, genuinely solid: py-opendisplay's PIPE_WRITE sliding-window sender was traced against the spec and found **defect-free**; epaper-dithering's colour math is correct; the HA integration's per-MAC BLE lock and executor discipline are correct; the ESP32 `Firmware` NimBLE callback-race fix (PR #108) holds. The problems are concentrated in two places: **(a)** a small number of real memory-safety / availability bugs in firmware, and **(b)** a systemic **spec-drift** failure — the canonical wire protocol advanced (v2.0 moved NFC, v2.1 swapped deep-sleep/power-off) but the change reached only *one* of six consumers, and the CI gate that was supposed to catch exactly this (`sync_protocol_header.py --check`) is **wired into no repo anywhere**.

The single most serious finding is a **remotely reachable ~64 KB out-of-bounds heap write** in the Firmware_Silabs NFC handler (the only real NFC implementation in the ecosystem). The same bug class sits latent behind a stub on Firmware_NRF54. The most *pervasive* finding is the **0x52/0x53 opcode swap**: only ESP32 `Firmware` adopted v2.1, so a py-opendisplay/HA `deep_sleep()` — which still sends 0x0052 — will **hard power-off** (button-only wake) any tag running the new ESP32 firmware, while silently doing the right thing on every other target. Nothing detects this because the drift gate is unwired, three of four firmware repos carry hand-written stub headers instead of the vendored canonical, and the fourth never vendored a header at all.

Total findings across the toolchain: **1 exploitable Critical + 1 latent Critical**, plus a cluster of High/Medium interop and availability issues detailed below.

---

## 2. Findings — descending severity, deduplicated across repos

Each row is one distinct issue; the "Repos" column lists everywhere it manifests, with links to the doc(s) that detail it.

### CRITICAL

**S1 — Silabs NFC single-shot write: uint16 length-guard overflow → remote ~64 KB heap OOB write (exploitable memory corruption).**
`opendisplay_pipe.c:950` validates `(uint16_t)(4u + text_len) > payload_len`; the sum truncates to uint16, so `text_len = 0xFFFF` yields `3`, passes the guard, and `od_nfc_write_record_raw` (`opendisplay_ble.c:1282/1294`) `memcpy`s up to 65 535 bytes into the 512-byte global `s_od_nfc_write_blocks` — overwriting adjacent `.bss` (likely a code-exec primitive), plus an OOB read of the same span. TEXT and URI single-write rec types are reachable; the chunked path is bounds-safe. Reachable unauthenticated if config has security disabled, else from any authenticated session.
Repo: **Firmware_Silabs** ([C1](AUDIT_2026-07-19_Firmware_Silabs.md)). Confidence: **Confirmed (traced end-to-end).**

**S2 — Same uint16 NFC-length overflow, latent on NRF54 behind a stub backend.**
`opendisplay_pipe.c:1060-1065` has the identical truncated guard; `text_len = 0xFFFF` passes. Defanged **only** because `opendisplay_ble_nfc_write` currently returns false (stub) — it flips to a Critical OOB **read** (~64 KB from a ≤244-byte frame) the moment a real NFC backend lands. The config half of NFC (0x2A packet) is already fully parsed/stored, so the "real front end / dead backend" asymmetry is wide.
Repo: **Firmware_NRF54** ([A3 / L1](AUDIT_2026-07-19_Firmware_NRF54.md)). Confidence: **Confirmed (latent).** *This is a shared reference-handler bug pattern — fix both, and add a spec note, since the canonical NFC sub-protocol's full-uint16 `len` field invites it.*

### HIGH

**S3 — The 0x52/0x53 deep-sleep / power-off opcode swap is an ecosystem break; only ESP32 `Firmware` adopted v2.1.**
Canonical v2.1: `0x0052 = CMD_POWER_OFF`, `0x0053 = CMD_DEEP_SLEEP (+ optional [seconds:2 BE])`. py-opendisplay still emits deep-sleep on `0x0052` (`commands.py:40`), as do NRF54, Silabs, and NRF-legacy firmware. Consequence: a py/HA `deep_sleep()` against a **new ESP32 tag hard-cuts the rail (button-only wake)** instead of sleeping; against a canonical host, a Silabs/NRF54 tag sent `0x0053` **hangs the client** (falls to default: no response). Client + three firmwares agree with each other and with *shipped* firmware, but all disagree with canonical + the ESP32 branch.
Repos: **py-opendisplay** ([H1](AUDIT_2026-07-19_py-opendisplay.md)), **Firmware** (adopter — [Cross-repo §3](AUDIT_2026-07-19_Firmware.md)), **Firmware_NRF54** ([H1/A2](AUDIT_2026-07-19_Firmware_NRF54.md)), **Firmware_Silabs** ([H1](AUDIT_2026-07-19_Firmware_Silabs.md)), **Firmware_NRF** ([L1](AUDIT_2026-07-19_Firmware_NRF.md)), **opendisplay.org** ([H1](AUDIT_2026-07-19_opendisplay.org.md)), **opendisplay-protocol** ([spec hazard, §2 obs 2](AUDIT_2026-07-19_opendisplay-protocol.md)). Confidence: **Confirmed.** *(Note: HA integration itself never issues deep-sleep — [Cross-repo obs](AUDIT_2026-07-19_Home_Assistant_Integration.md) — so it is insulated at the 7.12.0 pin.)*

**S4 — Header-vendoring drift on 3/4 firmware repos + the drift-detection CI gate is wired nowhere.**
`sync_protocol_header.py --check` FAILS today: NRF54 DRIFT (59-line hand stub), Silabs DRIFT (~50-line hand stub), Firmware_NRF **MISSING** (own header `EPD/EPD_service.h`, never vendored; the sync map's expected path does not exist); only ESP32 `Firmware` is in sync. Grep of every `.github/workflows/*` across all firmware repos and py-opendisplay (+ py's `.pre-commit-config.yaml`) finds **no invocation** of the check. This unwired gate is the direct cause of S3 and S6 going undetected. The protocol repo itself also has no `.github/`, so its own header's claim that "CI runs `--check` in each repo" is aspirational.
Repos: **opendisplay-protocol** ([C1/H1 + §6 A3](AUDIT_2026-07-19_opendisplay-protocol.md)), **Firmware_NRF54**, **Firmware_Silabs**, **Firmware_NRF**, **py-opendisplay** (hand-written constants, no mirror diff). Confidence: **Confirmed.**

**S5 — ESP32 `Firmware` config-read over BLE silently truncates configs larger than ~864 bytes.**
`handleReadConfig` (`communication.cpp:351-393`) emits up to 44 chunks synchronously into a **10-slot** response ring that is only drained by the same loop task *after* the handler returns; after ~9 chunks every further chunk logs "Response queue full, dropping." A realistically provisioned device (displays + LEDs + sensors + wifi + security + `data_extended`) exceeds the threshold → host reassembles a truncated config → parse/CRC failure. A regression introduced by the NimBLE queue-based response model; LAN transport unaffected.
Repo: **Firmware** ([H1](AUDIT_2026-07-19_Firmware.md)); host-visible to **py-opendisplay**/**HA** config read-back. Confidence: **Confirmed (traced).**

**S6 — NRF54 dispatches NFC on the retired 0x0082 opcode; canonical (v2.0+) is 0x0083.**
`opendisplay_pipe.c:1301` handles NFC on `0x0082` (= canonical `CMD_PIPE_WRITE_END`). A canonical/py host sending NFC `0x0083` hits "unknown cmd"; a host sending `PIPE_WRITE_END 0x0082` is misrouted into NFC handling. py-opendisplay's NFC (0x0083) is therefore unreachable on NRF54 regardless of py correctness. (Silabs correctly uses 0x0083.)
Repos: **Firmware_NRF54** ([H1/A2](AUDIT_2026-07-19_Firmware_NRF54.md)), **py-opendisplay** ([cross-repo](AUDIT_2026-07-19_py-opendisplay.md)), **opendisplay.org** (docs also on 0x0082 — [C1](AUDIT_2026-07-19_opendisplay.org.md)). Confidence: **Confirmed.**

**S7 — Firmware_NRF (legacy) runs multi-second blocking work in the BLE interrupt handler → two watchdog-reset / permanent-hang paths.**
`NRF_SDH_DISPATCH_MODEL = 0` means the whole GATT-write → dispatch chain runs in the SoftDevice event ISR. **H1:** `CMD_DIRECT_WRITE_END` refresh busy-waits up to ~65 s at SWI priority; the WDT feed is gated on a timestamp incremented only by a 1 Hz timer that cannot preempt the spin, so the feed never fires and a refresh longer than the 2 s reload window resets the device mid-operation → client re-drives → reboot loop. **H2:** `LED_ACTIVATE` with `ledcfg[10]==254` sets `grouprepeats==255`, the "run forever" sentinel, and `ledFlashLogic()` never returns — permanent ISR hang (then WDT reset). A short LED frame bypasses encryption entirely (see S8).
Repo: **Firmware_NRF** ([H1, H2](AUDIT_2026-07-19_Firmware_NRF.md)). Confidence: **Confirmed.**

**S8 — epaper-dithering ColorScheme value 7/8 conflict with protocol v2.0 wire contract.**
`palettes.rs:50-51` still defines `Grayscale8 = 7`; canonical `opendisplay_structs.h:550` reassigned **7 = SEVEN_COLOR** and added **8 = BWGBRY_SPLIT** (live on the E1004 panel, `Firmware/src/display_service.cpp:1585`). A `color_scheme=7` device is decoded as GRAYSCALE_8 and dithered to a gray ramp (visually wrong) then fails to encode; a `color_scheme=8` device cannot upload at all (`TryFrom<u8>` rejects it). Fix must land in epaper-dithering first (it owns the shared enum), then propagate through py-opendisplay `from_value` and the HA pin chain.
Repos: **epaper-dithering** ([H1](AUDIT_2026-07-19_epaper-dithering.md)), **py-opendisplay**/**Firmware** (consumers). Confidence: **Confirmed.**

**S9 — epaper-dithering PyO3 binding holds the GIL for the entire dither → stalls the HA event loop.**
`packages/python/src/lib.rs:61-137` never calls `py.allow_threads`; a rayon-parallel multi-hundred-ms dither of a large Spectra panel runs with the process-global GIL held, so HA's executor-offloaded `drawcustom` still freezes the event loop for the whole dither. Fix (`allow_threads`) is library-local, no consumer change.
Repos: **epaper-dithering** ([H2](AUDIT_2026-07-19_epaper-dithering.md)), **Home_Assistant_Integration** (victim). Confidence: **Confirmed.**

**S10 — Website documents NFC on 0x0082 and entirely omits (and denies) the PIPE_WRITE protocol.**
Two site Criticals: **C1** — `ble-flow.html:896` / `reference-firmware-variants.html:107` document NFC on the retired 0x0082 (an implementer emits frames firmware reads as pipe-write-end). **C2** — the shipping 0x0080–0x0082 sliding-window path is undocumented *and* `ble-flow.html:696-697` states "Reference firmware implements direct write only." Plus site Highs H2/H3 (config-read chunk size 512 vs 100; chunked-write example violates the mandatory 200-byte first chunk — copying it reproduces the failure).
Repo: **opendisplay.org** ([C1, C2, H2, H3](AUDIT_2026-07-19_opendisplay.org.md)). Confidence: **Confirmed.**

**S11 — ODL documentation vs renderer reality: three High mismatches.**
Dithering table maps algorithm 1 to Floyd-Steinberg but renderer maps 1=Burkes, FS=3, default=Burkes; `dlimg` camera/image-entity support is documented but the renderer rejects entity IDs (vaporware); the in-browser designer exports fresh multiline elements without the `offset_y` the renderer requires → `ValueError` on render.
Repos: **opendisplay.org** ([H5/H6/H7](AUDIT_2026-07-19_opendisplay.org.md)), **odl-renderer** (ground truth). Confidence: **Confirmed.**

**S12 — odl-renderer unbounded plot loops freeze the event loop (DoS).**
`H1`: a small `xlegend.interval` drives an unbounded axis/legend loop; combined with S9's GIL behaviour and HA's render path, a hostile or malformed ODL payload can hang the HA event loop. Also `H2` (non-dict element escapes the ValueError contract as raw AttributeError) and `H3` (parse_colors + align + anchor double-shifts text).
Repo: **odl-renderer** ([H1/H2/H3](AUDIT_2026-07-19_odl-renderer.md)). Confidence: **Confirmed.**

### MEDIUM

**S13 — Short-frame plaintext bypass of encryption/replay protection (shared across three firmwares).**
When a session is authenticated, frames below the length threshold are dispatched as cleartext with no CCM integrity and no replay check — and the sensitive control opcodes (REBOOT, ENTER_DFU, DEEP_SLEEP, CONFIG_READ, DIRECT_WRITE_START, short LED) are all short. Threshold: `frame_len >= 31` on Silabs (`pipe.c:1225`) and NRF-legacy (`EPD_service.c:705`); NRF54 shares the pattern. Practical actor is the already-authenticated peer (links are unpaired/SEC_OPEN), so it is a defense-in-depth / spec-conformance gap rather than a remote off-path bypass — but it is uniform across the ecosystem and the canonical spec itself lacks an in-band plaintext/ciphertext discriminator.
Repos: **Firmware_Silabs** ([M2](AUDIT_2026-07-19_Firmware_Silabs.md)), **Firmware_NRF** ([M2](AUDIT_2026-07-19_Firmware_NRF.md)), **Firmware_NRF54** (pattern). Confidence: **Confirmed (path); Plausible (impact).**

**S14 — Dishonest ACKs on unimplemented commands mislead the client into hanging/bricking flows.**
NRF54 `CMD_ENTER_DFU` (`pipe.c:1241-1247`) replies `[00][51]` success then only logs "not implemented" — the client waits for a bootloader/disconnect that never comes (worse than the honest deep-sleep silent stub). Related: NRF54 deep-sleep is a **pure no-op** with no low-power state anywhere in the app, so a battery tag commanded to sleep never reduces draw ([M3](AUDIT_2026-07-19_Firmware_NRF54.md)). (NRF-legacy DFU is honest — sets GPREGRET + reset.)
Repo: **Firmware_NRF54** ([M4/A4, M3](AUDIT_2026-07-19_Firmware_NRF54.md)). Confidence: **Confirmed.**

**S15 — py-opendisplay response-parsing conflation: FF52 refusals + generic NACK swallowing + fictional constants.**
`deep_sleep()` reads no sub-status, so `0x01 DISABLED` / `0x02 NOT_BATTERY` all report as "unsupported" (H2). `validate_ack_response` collapses every `[FF][echo]` to a generic error, discarding meaningful `OD_ERR_PARTIAL_*` sub-status so a doomed partial retries every upload (M4). The `[status][echo]`-as-BE-opcode model rests on a fictional `RESPONSE_HIGH_BIT_FLAG 0x8000` no firmware sets (M3); pipe-start err 0x02 is mis-diagnosed as "compression unsupported" (canonical: unknown-flag) driving a pointless uncompressed retry (M5). Auth-required detection only matches the 3-byte firmware shape, not the header's 2-byte form (M2).
Repo: **py-opendisplay** ([H2, M2-M5](AUDIT_2026-07-19_py-opendisplay.md)). Confidence: **Confirmed.**

**S16 — py-opendisplay mid-stream re-authentication can desync a live legacy image transfer.**
`_write` runs `_reauthenticate_if_needed` on every encrypted write; the PIPE path bypasses it but the legacy 0x71 chunk senders do not, so a slow upload crossing 90% of the session timeout transmits a full 3-frame AUTHENTICATE between two image chunks (and drains the queue), desyncing firmware reassembly.
Repo: **py-opendisplay** ([M1](AUDIT_2026-07-19_py-opendisplay.md)). Confidence: **Confirmed path; Plausible impact.**

**S17 — HA entry unload / reboot-reload can block up to ~600 s on an in-flight non-upload service call.**
`async_unload_entry` / `_async_reload_after_reboot` drain the per-MAC lock with an unbounded `async with`; only `upload_task` is cancelled first, so an in-flight `drawcustom`/LED/buzzer/OTA holds the lock until its own 600 s deadline. `drawcustom` also lacks the latest-wins/coalescing that `upload_image` has.
Repo: **Home_Assistant_Integration** ([M1, L2](AUDIT_2026-07-19_Home_Assistant_Integration.md)). Confidence: **Confirmed.**

**S18 — NRF54 Channel Sounding (device_flags bit 5): cross-thread conn use-after-free + 10 s system-workqueue starvation.**
`cs_setup_work_handler` reads/derefs `s_cs_conn` across a 10 s blocking `k_sem_take` on the shared system workqueue while `on_disconnected` (BT thread) unrefs and nulls it with no lock (M1, UB on disconnect-during-setup); for that window every other system-workqueue item is starved (M2). Only triggers when CS is enabled and a peer initiates.
Repo: **Firmware_NRF54** ([M1, M2](AUDIT_2026-07-19_Firmware_NRF54.md)). Confidence: **Plausible (M1) / Confirmed (M2).**

**S19 — ESP32 `Firmware` zlib window divergence: all envs but one reject standard 32 KB-window streams.**
Default `OPENDISPLAY_ZLIB_WINDOW_BITS = 9` (512 B); only `env:esp32-s3-E1004` builds `=15`. A host emitting a standard 32 KB-window zlib stream for a compressed 0x70/0x76/0x80 transfer fails at the header on every other env. The host must select window size per target. Contradicts the workspace note that "existing targets pin 32 KB windows." Website M3/M13 document the old behaviour, now stale.
Repos: **Firmware** ([zlib §](AUDIT_2026-07-19_Firmware.md)), **opendisplay.org** ([M3/M13](AUDIT_2026-07-19_opendisplay.org.md)), host-side **py-opendisplay**. Confidence: **Confirmed.**

**S20 — Config-wire CRC computed but not enforced; unauthenticated LAN control plane; config-write 201-byte edge bug (three firmware config-parser issues).**
Silabs computes the toolbox config CRC-16 then proceeds on mismatch (`config_parser.c:490-503`) — a corrupt SecurityConfig is accepted and applied ([M3](AUDIT_2026-07-19_Firmware_Silabs.md)). ESP32 `Firmware` WiFi-LAN feeds the entire command surface unauthenticated when security is off ([M2](AUDIT_2026-07-19_Firmware.md)). A single-frame config write of exactly 201 bytes silently drops one byte and stalls on **all three** of Silabs/NRF54/NRF-legacy/ESP32 (same class: [Silabs M4](AUDIT_2026-07-19_Firmware_Silabs.md) / [NRF54 A8](AUDIT_2026-07-19_Firmware_NRF54.md) / [NRF-legacy L3](AUDIT_2026-07-19_Firmware_NRF.md) / [Firmware L4](AUDIT_2026-07-19_Firmware.md)). Confidence: **Confirmed.**

**S21 — Website Medium cluster: stale/contradictory protocol pages.** RESP_AUTH_REQUIRED byte position (header-vs-client, header likely wrong), byte-arithmetic errors, MTU-vs-244 conflict, packet-type list stops at 39, variants page covers 2 of 4 targets, dead `../homeassistant/` link (404, 3 files), 5-month-stale nRF demo package, Firefox-Web-Bluetooth false claim, wrong Silabs repo link. Repo: **opendisplay.org** ([M1-M16, H8-H11](AUDIT_2026-07-19_opendisplay.org.md)). Confidence: **Confirmed.**

### LOW (rolled up — see per-repo docs)

- **Firmware ESP32:** power-latch MOSFET-hold-at-wake, Seeed IT8951 unbounded refresh wait, buzzer/LED cross-task race (nRF build) → possible OOB read, 60 s undocumented deep-sleep clamp, stale dispatcher log. [Firmware](AUDIT_2026-07-19_Firmware.md)
- **NRF54:** timing-unsafe auth memcmp, cross-thread `s_conn`/`s_notify` reads, `rescan_security_packet` adopts unvalidated bytes, battery active-low flag ignored, config skip-size table not enforced against struct sizes. [Firmware_NRF54](AUDIT_2026-07-19_Firmware_NRF54.md)
- **Silabs:** deep-sleep ACK-vs-EM4 flush, global session state vs MAX_CONNECTIONS=4, FW_VERSION/READ_MSD auth-gated pre-session, event-loop reboot/refresh stalls. [Firmware_Silabs](AUDIT_2026-07-19_Firmware_Silabs.md)
- **NRF-legacy:** reentrant `nrf_sdh_evts_poll` in saveConfig, deep-sleep ACK not TX-confirmed, READ_MSD declared-but-undispatched, length-heuristic fragility. [Firmware_NRF](AUDIT_2026-07-19_Firmware_NRF.md)
- **py-opendisplay:** session-clear-vs-queued-frame misparse, retransmit "1-RTT spacing" fires every ACK, unbounded notification queue. [py-opendisplay](AUDIT_2026-07-19_py-opendisplay.md)
- **HA:** config-resync no give-up cap, wall-vs-monotonic clock mismatch in sleep/expiry, field-name-based diagnostics redaction. [Home_Assistant_Integration](AUDIT_2026-07-19_Home_Assistant_Integration.md)
- **epaper-dithering:** JS palettes hand-duplicated (drift risk), JS test matrix never exercises non-default dither modes, wasm canonical-pinning sentinel misfire, RGBA cross-surface divergence, index-order invariant unenforced. [epaper-dithering](AUDIT_2026-07-19_epaper-dithering.md)
- **odl-renderer:** `dlimg` no fetch timeout/size cap + SSRF-shaped source handling, font-name path traversal, unbounded process-global caches, many uncoerced-input → wrapped-ValueError edges. [odl-renderer](AUDIT_2026-07-19_odl-renderer.md)
- **opendisplay-protocol tooling:** non-atomic vendored-header write, `--dest` double-processing + dead branch, mirror generator silent-skip hole. [opendisplay-protocol](AUDIT_2026-07-19_opendisplay-protocol.md)

---

## 3. Cross-repo consistency matrix

### Opcode numbering & NFC

| Repo | Deep-sleep | Power-off | `[seconds:2 BE]` | NFC | Vendored header |
|---|---|---|---|---|---|
| **Canonical v2.1** | 0x53 | 0x52 | yes | 0x83 | — (source of truth) |
| Firmware (ESP32) | **0x53** ✓ | **0x52** ✓ | yes ✓ (+60 s floor) | none (per @targets) | **CURRENT** (only in-sync copy) |
| Firmware_NRF54 | 0x52 ✗ | none | no | **0x82** ✗ | STALE 59-line stub (DRIFT) |
| Firmware_Silabs | 0x52 ✗ (ACK→EM4) | none | no | 0x83 ✓ | STALE ~50-line stub (DRIFT) |
| Firmware_NRF (legacy) | 0x52 ✗ (ACK→sleep) | none | no | none | **MISSING** (own header, never vendored) |
| py-opendisplay | 0x52 ✗ | absent | no | 0x83 ✓ | own hand-written constants (no mirror import) |

`sync_protocol_header.py --check` today: Firmware OK · NRF54 DRIFT · Silabs DRIFT · Firmware_NRF MISSING. **Wired into CI in zero repos.**

### Library pin versions

| Consumer | Pins | Current | Status |
|---|---|---|---|
| Home_Assistant_Integration (`manifest.json`) | py-opendisplay==**7.12.0**, odl-renderer==0.5.12 | py at 7.13.0 | Behind by one minor; every symbol HA calls exists at 7.12.0 (verified) — NFC-write (7.13.0-only) and the 0x52 deep-sleep defect are both **unreachable** from HA at this pin |
| py-opendisplay (`pyproject.toml`) | epaper-dithering==**5.0.9** | 5.0.9 | Current |

### Encryption / short-frame plaintext threshold

| Repo | Threshold | Short sensitive opcodes bypass CCM+replay? |
|---|---|---|
| Firmware_Silabs | `frame_len >= 31` | Yes (S13) |
| Firmware_NRF (legacy) | `length >= 31` | Yes (S13) |
| Firmware_NRF54 | same pattern | Yes (S13) |
| Firmware (ESP32) | (not flagged this pass) | — |

---

## 4. Unimplemented / partial features rollup

| Area | Repos | State |
|---|---|---|
| Deep-sleep `[seconds:2 BE]` timed wake | py, NRF54, Silabs, NRF-legacy | Not implemented (bare opcode; payload ignored) |
| Low-power state on NRF54 | Firmware_NRF54 | **Absent entirely** — super-loop only `k_msleep`s; deep-sleep is a log-only no-op |
| Enter-DFU / OTA | NRF54 (dishonest ACK), NRF-legacy (honest), Silabs (real AppLoader OTA), ESP32 | Uneven; NRF54 ACKs success then does nothing |
| NFC | Silabs (real, on 0x83) · NRF54 (real front end, stub backends, wrong 0x82) · Firmware/NRF-legacy (none) | Only Silabs functional; both real front-ends carry the uint16 overflow |
| PIPE_WRITE sliding window | Firmware (ESP32) only + py sender | One firmware peer; undocumented on the website |
| CMD_POWER_OFF 0x52 (v2.1) | ESP32 only | All others lack a true power-off distinct from deep-sleep |
| READ_MSD 0x44 | NRF-legacy declared-but-undispatched; others via advert | Partial |
| ColorScheme 7-color/8-BWGBRY/RGB modes | epaper-dithering | Absent; value 7 mis-assigned (S8) |
| ODL `dlimg` camera/entity, font-fallback, several sub-options | odl-renderer / docs | Documented but vaporware/ignored |
| Persistent HA delivery queue, `drawcustom` coalescing | HA integration | Memory-only; no supersede for drawcustom |
| Website coverage of v2.0/v2.1, NFC 0x83, PIPE, POWER_OFF | opendisplay.org | Wholly undocumented / stale |

---

## 5. Recommended remediation order

**Tier 0 — exploitable memory safety (do first):**
1. **S1** — fix the Silabs NFC uint16 length guard (32-bit arithmetic) and bound `data_len` in `od_nfc_write_record_raw` before any memcpy. Remotely reachable heap-write.
2. **S2** — fix the identical NRF54 guard now, before a real NFC backend lands, and add a canonical spec note so the shared reference handler stops inviting the bug.

**Tier 1 — ecosystem-breaking drift + the gate that would have caught it:**
3. **S4** — wire `sync_protocol_header.py --check` into CI/pre-commit in every firmware repo and py-opendisplay; add a py check that diffs hand-written constants against the generated mirror. (Also fix the tooling Lows: non-atomic write, `--dest` double-processing.) This is the keystone — it prevents S3/S6/S8 from recurring.
4. **S3 + S6** — execute the 0x52/0x53 (and confirm 0x82→0x83) migration as a **coordinated** cross-repo change: vendor the canonical header into NRF54/Silabs/NRF-legacy, adopt 0x53 deep-sleep / 0x52 power-off / 0x83 NFC, update py-opendisplay constants, then update the website and opcode-support-matrix. Until all ship together, gate `deep_sleep()` on a known-firmware-version check to avoid the accidental hard power-off.
5. **S8** — reassign epaper-dithering ColorScheme 7/8 to match `opendisplay_structs.h`, propagate through py `from_value` and the HA pin chain.

**Tier 2 — availability / correctness:**
6. **S7** — port NRF-legacy command execution off the SoftDevice ISR (app_scheduler/main-loop worker, as NRF54 does); fixes both WDT-reset paths.
7. **S5** — drain the ESP32 response ring between config-read chunks (or grow it / notify directly).
8. **S9** — add `py.allow_threads` around the dither (unblocks the HA event loop).
9. **S12 / S11** — bound odl-renderer plot loops; reconcile the ODL docs/designer output with renderer reality.

**Tier 3 — hardening & hygiene:**
10. **S13** — tighten the short-frame plaintext bypass across firmwares (require encryption for control opcodes when a session is encrypted); add a canonical in-band discriminator.
11. **S14 / S15 / S16 / S17** — dishonest ACKs, py response conflation, mid-stream re-auth, HA unbounded unload drain.
12. **S18–S21** and the Low rollup — CS thread-safety, config-CRC enforcement, zlib window coordination, website refresh.

---

*Prepared by the coordinating audit agent from ten verified per-repo reports. Static analysis only — no code was modified, built, or run. Severity/confidence labels on findings carried over from prior verified audit passes are preserved.*
