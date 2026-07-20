# AUDIT — opendisplay.org (project website)

**Repo:** `/home/davelee/opendisplay/opendisplay.org`
**Branch / commit audited:** `main` @ `fcd8044fca6e201d95b4dad9532ef7c42c5a412a` (tag `1.3`, 2026-07-14), working tree clean.
**Date:** 2026-07-19 · **Depth:** light (assembly of verified staged audits + link/hygiene spot-check).
**Ground truth:** `opendisplay-protocol/src/opendisplay_protocol.h` v2.1 (0x0052=CMD_POWER_OFF, 0x0053=CMD_DEEP_SLEEP swapped in 2.1; NFC=0x0083 moved from 0x0082 in v2.0); py-opendisplay; odl-renderer.

Findings from Parts A/B (website protocol + hardware/install/landing pages) and the ODL-docs-vs-renderer cross-check are **verified by prior audit passes** and incorporated below with their original severities/confidence. Part C (protocol-repo docs sweep) belongs to `AUDIT_2026-07-19_opendisplay-protocol.md` and is not re-listed here. This agent's own spot-check corroborated the load-bearing link/package findings.

---

## Architecture overview (site layout)

Static HTML site served from `httpdocs/`. No build step; deployed via FTP (`.github/workflows/deploy-ftp.yml`). Key areas:

- **Top-level pages:** `index.html` (landing), `what-hardware-to-buy.html`, `impressum.html`, `datenschutz.html` (German legal).
- **`protocol/`** — the technical documentation set: `ble-flow.html`, `basic-standard.html`, `flex-standard.html`, `display-data-format.html`, `yaml-config.html`, `reference-firmware-variants.html`, `open-display-language.html` (ODL element reference), `flex-tools.html`, `adding-displays.html`, `index.html`.
- **`firmware/`** — install/flashing guides and the web flasher: `index.html`, `install/`, `toolbox/` (ESP web-flasher with `bin/`, `config.yaml`, `simple-config-presets.json`, `firmware/NRF52840.*`), `battery/`, `config/`, `display/`, `reusing_solum_displays.html`, `seeed_display_compatibility.html`.
- **`nrf_web_tools/`** — Web-Serial/DFU nRF flasher (ships its own `NRF52840.zip` demo package).
- **`designer/`** — in-browser ODL designer (`index.html`, MDI icon font).
- **`l/`** — short-link / contact landing (`index.html`, `generateqr.html`).
- **`js/`, `css/`, `assets/`, `fonts/`** — shared static assets (all self-hosted; no external CDN/script deps found in `designer/index.html` or elsewhere).

Firmware binaries are synced from the Firmware repo release (`sync-firmware.yml`); `firmware/toolbox/bin/firmware-version.json` records the source tag.

---

## Findings by severity

### CRITICAL

**C1 — NFC documented on the retired 0x0082 opcode.** `protocol/ble-flow.html:896` ("0x0082 — NFC endpoint (BG22 only)") and `protocol/reference-firmware-variants.html:107` ("NFC 0x0082"). Canonical: 0x0082 = `CMD_PIPE_WRITE_END` (header :616); NFC = 0x0083 (:651), moved in the v2.0 clean cutover (:85-87, :645-649). *Failure:* an implementer following the site emits NFC frames on 0x0082 that firmware parses as pipe-write-end — silently wrong behavior, no NFC action. **Confirmed** (verified by prior audit pass; corroborated — 0x0082 NFC references present in both files).

**C2 — Entire PIPE_WRITE sliding-window protocol (0x0080–0x0082) undocumented AND actively contradicted.** No page mentions 0x0080/0x0081, negotiation header, SACK, the 33-slot reorder queue, window range 1..32, `PIPE_MAX_FRAME` 244, or `OD_ERR_PIPE_START_*`. Worse, `ble-flow.html:696-697` asserts "Reference firmware implements direct write only." Canonical: header :552-616, :835-841; implemented in py `device.py:2296-2439`. *Failure:* a client author cannot use (and is told not to expect) a shipping high-throughput path. **Confirmed** (verified by prior audit pass).

---

### HIGH

**H1 — Deep sleep listed as 0x0052.** `ble-flow.html:888` shows deep sleep at 0x0052; header 2.1 swapped this so 0x0053 = DEEP_SLEEP, 0x0052 = POWER_OFF. Site never mentions 0x0053 / POWER_OFF / `[seconds:2 BE]` / `OD_ERR_DEEP_SLEEP_*`. (py-opendisplay is equally behind — needs a coordinated update, not a site-only fix.) **Confirmed** (verified by prior audit pass).

**H2 — Config-read notification chunk size wrong (512 B vs 100 B).** `ble-flow.html:606` and the worked example at :970-978 (508/512/260-byte chunks); header :269/:849 sets `MAX_RESPONSE_DATA_SIZE = 100`. **Confirmed.**

**H3 — Chunked config-write example violates the mandatory full-200-byte first chunk.** `ble-flow.html:1096` shows "Chunk 1: 198 bytes"; header :277-281 requires the first frame to carry a full 200 bytes — a short first chunk makes firmware take the single-frame path and NACK the following 0x42. Copying the example reproduces the documented failure. **Confirmed.**

**H4 — 244-byte pipe frame attributed to BG22, which has no PIPE.** `reference-firmware-variants.html:122-124`. Canonical: PIPE is Firmware-only; 244 = `PIPE_MAX_FRAME`; 230 = direct-write chunk budget. The row inverts the two numbers' owners and implies a BG22 capability that does not exist. **Confirmed.**

**H5 (ODL docs cross-check) — Dithering table wrong.** Docs map algorithm 1 to Floyd-Steinberg; renderer maps 1 = Burkes, FS = 3, and the default is Burkes (not "ordered"). *Failure:* users selecting a dither index get a different algorithm than documented. **Confirmed** (verified by prior ODL audit pass).

**H6 (ODL docs cross-check) — `dlimg` camera/image-entity support is vaporware.** Renderer rejects entity IDs (`media.py:104-114`, `media_loader.py:70-78`); the integration never resolves them. Documented capability does not exist. **Confirmed.**

**H7 (ODL docs cross-check) — Designer exports fresh multiline elements without the required `offset_y`.** A newly-added multiline element lacks the offset the renderer requires → renderer raises `ValueError` on render. *Failure:* designer output is not rendarable out of the box. **Confirmed.**

**H8 — Toolbox deep link references a preset id that does not exist.** `what-hardware-to-buy.html` links `?config=xiao-75-s3-og`; that preset id is absent from `firmware/toolbox/simple-config-presets.json` (every other id linked on the page is present). *Failure:* the toolbox opens without the advertised preset. **High / Plausible** (verified by prior audit pass).

**H9 — `nrf_web_tools/NRF52840.zip` demo is ~5 months stale.** The demo package is an app-only Adafruit-DFU zip, 206,041 B, timestamped 2026-02-12, versus the current toolbox package `firmware/toolbox/firmware/NRF52840.zip` at 270,749 B (2026-07-13); `firmware/toolbox/bin/firmware-version.json` reports source tag **2.10 (published 2026-07-06)**. The demo therefore flashes firmware that predates v2 (pre-NFC-cutover, pre-pipe). *Failure:* a user flashing the demo gets an incompatible-with-current-docs image. **Confirmed** (spot-check corroborated: file sizes/dates and `firmware-version.json` tag 2.10 as stated).

**H10 — Dead `../homeassistant/index.html` link (404).** That directory does not exist under `httpdocs/`. Referenced in **three files**: `firmware/index.html`, `protocol/open-display-language.html` (:147, :1675), `firmware/reusing_solum_displays.html` (:167) — 5 occurrences total. *Failure:* every "Home Assistant" nav/CTA from these pages 404s. **Confirmed** (spot-check: `ls homeassistant` → absent; `grep` confirms the three referencing files).

---

### MEDIUM

**M1 — RESP_AUTH_REQUIRED byte position: header vs client conflict.** Site says 0xFE is the third byte (`ble-flow.html:452,:917`); header says `[0xFE][echo]` in the first byte; py `device.py:746-748` matches the **site** (3-byte form). Genuine interop hazard; the header may be the wrong party. **Confirmed conflict.**

**M2 — Encrypted payload budget implies 512 B.** `ble-flow.html:521`; header caps an encrypted chunk at 154 (:190-193). 512 is plausibly the decrypt-buffer bound but is wrong as sender guidance. **Plausible.**

**M3 — "Legacy 32 KB zlib still accepted on v1.x ESP32/nRF builds" is stale.** `ble-flow.html:742-744`, `display-data-format.html:777`, `reference-firmware-variants.html:87-89`. Firmware default is `OPENDISPLAY_ZLIB_WINDOW_BITS 9` (512 B, `uzlib.h:21-26`); larger-window streams are rejected (`od_zlib_stream.c:641`); `platformio.ini` has `=15` commented out for nRF/most ESP32 envs, active only on select envs (:148 esp32-s3/-c3). **Plausible.**

**M4 — `yaml-config.html` packet-type list stops at 39.** Missing 40 touch / 41 passive_buzzer / 42 nfc / 43 flash / 44 data_extended (header :240-244; toolbox `config.yaml:1676-1677` defines data_extended). The site's own `flex-standard.html:187-205` documents 40-43 (internal inconsistency); 44 is missing from both. **Confirmed.**

**M5 — Variants vs Flex contradiction on BG22 NFC/flash config parsing.** `reference-firmware-variants.html:69` says "parsed"; `flex-standard.html:199,204` says "in schema only — not parsed." One is wrong. **Confirmed contradiction.**

**M6 — Variants page covers only 2 of 4 firmware targets.** No NRF54 / NRF52811 columns (header :196-208 defines four distinct capability sets). **Confirmed.**

**M7 — `basic-standard.html` byte-example arithmetic errors (four).** :1012 bits `10110010` labeled 0xB6 (correct 0xB2); :1057 `10111010` labeled 0xB6 (0xBA); :1074 `00101000` labeled 0x24 (0x28); :1113-1129 scheme-3 labeled 0x93 but its own caption yields 0x4B. `display-data-format.html` has the correct values for the identical rows (:249,:297,:314,:374). **Confirmed.**

**M8 — MTU guidance conflicts with the 244-byte ceiling.** `basic-standard.html:521-524` ("MTU 512 … ~509 bytes per chunk") and `reference-firmware-variants.html:123`; canonical `PIPE_MAX_FRAME` 244 = "GATT write ceiling", py `DEFAULT_MAX_FRAME = 244` = "HA native ceiling." **Confirmed** for the reference stack.

**M9 — "BLE-only screens … no deep sleep" is stale.** `what-hardware-to-buy.html`; Firmware v2.x ships `CMD_DEEP_SLEEP`/`POWER_OFF` (`communication.cpp:660-665`). (Still true for NRF54.) **Medium / Confirmed.**

**M10 — Firefox listed as supporting Web Bluetooth + Web Serial (landing).** `index.html` claims Firefox support; it supports neither, and the site's own `nrf_web_tools/index.html:399` says Chrome/Edge only. **Confirmed.**

**M11 — Wrong Silabs firmware repo link.** `reference-firmware-variants.html` links `github.com/OpenDisplay/Firmware-silabs-bg22`; the actual repo is `Firmware_Silabs` → likely 404. **Plausible** (spot-check confirmed this is the only non-standard OpenDisplay repo link on the site; all other repo links resolve to real repo names).

**M12 — "Max pipe payload BG22 244 B" (variants) implies a BG22 pipe capability it lacks.** Same root as H4. **Med-High.**

**M13 — "legacy large-window zlib removed in firmware v2" (variants) conflicts with `platformio.ini:148`,** where the esp32-c3/s3 env still builds `WINDOW_BITS=15`. **Medium / Plausible.**

**M14 (ODL docs cross-check) — `ttl` service option silently dropped.** `services.py:166 REMOVE_EXTRA` strips it; documented but ineffective. **Confirmed.**

**M15 (ODL docs cross-check) — Font-fallback claim false.** Docs say a missing font falls back; `fonts.py:135-141` raises instead. **Confirmed.**

**M16 (ODL docs cross-check) — Several documented sub-options are ignored/unimplemented:** `multiline.spacing`; `polygon.width` (`shapes.py:171` commented out); plot top-level `size`; `progress_bar` expects `font_name` not documented `font` (`visualizations.py:725`); designer's default `dlimg` is an SVG PIL can't decode. **Confirmed (each Medium).**

---

### LOW

Website / protocol-page (verified by prior audit pass):
- **L1** "<200 bytes" vs `≤200` boundary (`ble-flow.html:669` vs header :276).
- **L2** Auth status list omits 0x02 ALREADY (`ble-flow.html:920-928` vs header :709).
- **L3** Site documents 16-byte `server_proof` in step-2 success (`ble-flow.html:502-506`); header (:344-346) omits it but py **parses** it (`responses.py:185`, `crypto.py:80-92`) → header under-documentation, fix the header not the site.
- **L4** Site omits the Silabs `READ_MSD`-plaintext exception (header :181-182).
- **L5** mDNS TXT `msd` "28 hex chars (14 bytes)" vs 16-byte MSD used everywhere else (`ble-flow.html:389`) — unexplained.
- **L6** Block-upload opcodes 0x0064/0x0065 + responses 0x00C4-0x00C8 exist nowhere in header/py — vaporware; page marks "not implemented / spec only" (`ble-flow.html:857-861,897-898,909`).
- **L7** `basic-standard.html` pull-model packet types 0x01/02/81/82/83 are a site-only WIP profile; footgun — visual collision with GATT 0x0082/0x0083; `display-data-format.html:199` propagates without note.
- **L8** `power_option` keys `min_wake_time_seconds` / `screen_timeout_seconds` verified consistent (`config.yaml:394-397` vs py `models/config.py:213-214,276-277` + `config_serializer.py:175-176`) — no on-page docs, no divergence.
- **L9** CRC spec (CRC-16/CCITT-FALSE, init 0xFFFF, poly 0x1021, length-zeroed) verified correct vs `config_serializer.py:46-63`.

Hardware/install/landing (verified by prior audit pass):
- Only nRF52840 + ESP32 boards listed vs four firmware trees (`what-hardware-to-buy.html`, and landing board list).
- Toolbox bins ship s3/c3/c6 + NRF52840 but repo also builds classic esp32 (`platformio.ini:234`).
- `nrf_web_tools` claims "supports all Adafruit-bootloader devices" but hard-codes the nRF52840 image with no chip check.
- Variants: `config.yaml` path has a spurious `web/` prefix; no deep-sleep/power-off row.
- Landing: blanket "GPL-v3" but py-opendisplay is MIT; "~80 mA idle … use USB power" stale vs deep-sleep.

ODL docs cross-check (Low):
- HW color glossed wrong (`colors.py:48-49` LIGHT_GRAY); anchor default `lt` vs `la`-on-newlines (`text.py:69-70`); rectangle `radius`/`corners` default interplay (`shapes.py:79-80`, radius defaults 10 when corners present); plot `x_end`/`y_end` defaults are a symmetric margin, not the canvas edge (`visualizations.py:614-615`); unknown colors render white with a one-time warning.

Spot-check (this pass): version-like strings scanned across top-level/firmware pages were overwhelmingly SVG path coordinates, not release identifiers — no additional stale version-string findings. No `TODO`/`FIXME`/`localhost`/`.php` leaks in page content (only a legitimate Web-Serial `localhost` note in `nrf_web_tools/index.html:269`). `l/` and `designer/` pages have no external CDN/script dependencies and no broken internal links.

---

## Unimplemented-or-partial (documented-but-vaporware / implemented-but-undocumented)

| Item | Kind | Where | Evidence |
|---|---|---|---|
| `dlimg` camera / image-entity source | Documented, not implemented | ODL docs | renderer rejects entity IDs — `media.py:104-114`, `media_loader.py:70-78`; integration never resolves |
| Block-upload 0x0064/0x0065 + 0x00C4-0x00C8 | Documented "spec only" | `ble-flow.html:857-861,897-898,909` | absent from header + py |
| `ttl` service option | Documented, silently dropped | `services.py:166` REMOVE_EXTRA | stripped before use |
| Font fallback on missing font | Documented, raises instead | `fonts.py:135-141` | throws |
| `multiline.spacing`, `polygon.width`, plot top-level `size` | Documented, ignored | `shapes.py:171` (commented out) etc. | no effect |
| `progress_bar` `font` key | Documented wrong key | `visualizations.py:725` wants `font_name` | option ignored |
| PIPE_WRITE 0x0080-0x0082 sliding window | Implemented, undocumented (and denied) | header :552-616; py `device.py:2296-2439`; `ble-flow.html:696-697` denies it | shipping but invisible to clients |
| `diagram` element | Implemented, undocumented | renderer `visualizations.py:779` | no ODL-doc entry |
| Per-element rotation/mirror/pivot transforms | Implemented, undocumented | renderer `core.py:100-101` | no ODL-doc entry |
| Service opts `refresh_type` / `tone_compression` / `measured_palette` | Implemented, undocumented | integration/renderer | no ODL-doc entry |
| ODL aliases: `align`, `y_padding`, `stroke`, color aliases, `smooth_steps`, `visible`-coercion | Implemented, undocumented | renderer | no ODL-doc entry |
| CMD_POWER_OFF 0x0052 + 0x52/0x53 swap; DEEP_SLEEP `[seconds:2 BE]` + 4c/4d codes | Header, wholly undocumented on site | header :391-392,:437-440,:485 | site still on old 0x0052=deep-sleep |
| NFC sub-protocol on 0x0083 (subs 0x00/01/10/11/12, rec types, 512 buffer, NFC_STATUS/ERR) | Header, wholly undocumented on site | header :651, :790-830 | site documents NFC on retired 0x0082 |
| NACK per-opcode scoping; encrypted budget 154; `MAX_CONFIG_CHUNKS` 20; LED `flash_config:12`; buzzer payload; NRF54/NRF52811 capability matrix | Header, undocumented on site | header :161-169,:190-193 etc. | gaps |

---

## Cross-repo observations

1. **v2.0/v2.1 protocol changes never reached the website.** The two swaps that matter most for interop — NFC 0x0082→0x0083 (v2.0) and DEEP_SLEEP/POWER_OFF 0x52↔0x53 (v2.1) — are absent from every protocol page. This is the same staleness class flagged in the protocol-repo `opcode-support-matrix.html` (Part C) and in the Silabs/py audits; a coordinated documentation refresh across site + `opendisplay-protocol/docs` + py-opendisplay is warranted.

2. **Site, header, and client disagree three ways on the same fields.** RESP_AUTH_REQUIRED byte position (M1) and `server_proof` presence (L3) are cases where the header is the outlier and py-opendisplay agrees with the site — i.e. the canonical header is under- or mis-specified, and fixes belong in the header, not the site.

3. **Firmware artifact drift.** The `nrf_web_tools` demo package (H9) is a manually-committed copy that the `sync-firmware.yml` pipeline does not update (only `firmware/toolbox/firmware/` is synced). Any doc/binary that lives outside the sync path silently rots — same failure mode as the stale opcode docs.

4. **Version-coupling note.** Aside from staged findings: the HA manifest still pins `py-opendisplay==7.12.0` while the library is at 7.13.0 (`pyproject.toml:7`) — consistent with the version-coupling caveat in the workspace CLAUDE.md; relevant to the HA-integration audit, noted here only because it surfaced while cross-checking the website's Python sample (which itself verified accurate: `OpenDisplayDevice`, `device_name` kwarg, async CM, `upload_image`).
