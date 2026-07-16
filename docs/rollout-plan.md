# Rollout plan — adopting the shared `opendisplay_protocol.h` (v2.0)

Agent-executable plan for the remaining follow-up work. The canonical repo
(`davelee98/opendisplay-protocol`) is complete: `src/opendisplay_protocol.h`
(v2.0), `tools/sync_protocol_header.py`, and docs are on `main`. What remains is
**adopting** the header in the four firmware repos and wiring CI.

Each task below is written as a **self-contained brief** you can paste directly
into an agent. The four firmware repos are independent git repos, so their
migrations can run **in parallel** (no shared files; they only *read* the
already-committed canonical header).

---

## Canonical facts (all briefs rely on these)

- Canonical header: `/Users/davelee/Documents/OD/opendisplay-protocol/src/opendisplay_protocol.h`
- Sync tool: `/Users/davelee/Documents/OD/opendisplay-protocol/tools/sync_protocol_header.py`
  (`--push` writes copies; `--check` verifies; `--only <repo>` scopes it)
- Raw URL (for CI): `https://raw.githubusercontent.com/davelee98/opendisplay-protocol/main/src/opendisplay_protocol.h`
- Vendored destinations:
  | Repo | Path |
  |---|---|
  | Firmware | `Firmware/include/opendisplay_protocol.h` |
  | Firmware_NRF54 | `Firmware_NRF54/src/opendisplay_protocol.h` |
  | Firmware_Silabs | `Firmware_Silabs/opendisplay_protocol.h` |
  | Firmware_NRF | `Firmware_NRF/opendisplay_protocol.h` |

### The macro-dedup rule (critical — applies to B, C, D)
Replacing/adding the shared header introduces macros that several repos **also**
define locally (without a `u` suffix) — e.g. `RESP_*`, `PIPE_*`, `CONFIG_CHUNK_SIZE`,
`MAX_RESPONSE_DATA_SIZE`, `OD_NFC_REC_*`, `OD_NFC_IC_*`, `BLE_CMD_HEADER_SIZE`,
`ENCRYPTION_NONCE_SIZE`, `ENCRYPTION_TAG_SIZE`. A duplicate `#define` with a
different token (`200` vs `200u`) is a **macro-redefinition error/warning**.
Rule: **the shared header now OWNS these; delete the local duplicates.** Keep
strictly repo-specific defines (config-packet types `CONFIG_PKT_*`, `TRANSMISSION_MODE_*`,
struct layouts, `GPIO_PIN_UNUSED`, device/charger flags, encryption *buffer* sizes
like `MAX_ENCRYPTED_*`). The shared header must NOT be edited in any firmware repo.

---

## Shared conventions (put these in EVERY firmware brief)

- **Work only in your assigned repo.** Never edit the canonical repo, the shared
  header's content, or any other firmware repo.
- **Get the header** by copying the canonical file to your destination path
  (byte-for-byte). Do not reformat or add a banner — copies must stay identical
  so `sync_protocol_header.py --check` passes.
- **Branch**, do not commit to `main` directly: `git checkout -b chore/protocol-v2-shared-header`.
- **Commit identity** (avoids GitHub's email-privacy push block): set local
  `git config user.email "247393336+davelee98@users.noreply.github.com"` and
  `git config user.name "davelee98"`.
- **Build before PR.** If the repo's real toolchain (NCS/`west`, SLC+CMake,
  `arm-none-eabi` Make) is not available in the environment, do a focused
  compile-sanity of the changed C/C++ files with the system compiler
  (`-fsyntax-only` where headers resolve) and **clearly report that the full
  firmware build could not be run** — do not claim a green build you didn't run.
- **Open a PR to the repo's own `main`; do not merge.** Include a body describing
  the change and the protocol version (v2.0) + NFC `0x82→0x83` where relevant.
- **Report back**: files added/edited/deleted, the macro dedups performed, build
  or compile-sanity result, and the PR URL.

---

## Parallelization & recommended order

- **Run B, C, D, A as parallel agents** (independent repos). Fold the CI snippet
  (Task E) into each migration PR.
- If you prefer sequencing: start with **D (Firmware_NRF)** — smallest, pure C,
  establishes the dedup pattern — then **C (Silabs)** and **B (NRF54)** which
  realize the NFC `0x83` cutover, then **A (Firmware)** which is vendor-only.
- The canonical-repo CI (Task E, part 1) is independent and can go anytime.
- **Out of scope for this batch** (tracked separately): the OD App Swift NFC
  `0x83` change (`BLE/ODConstants.swift:40`), and the combined-`Firmware`
  literal→named refactor of the `communication.cpp` switch.

---

## Task A — Firmware (combined nRF52840 + ESP32)  ·  vendor + CI only

> **Repo:** `/Users/davelee/Documents/OD/Firmware` (PlatformIO, C++/Arduino).
> **Goal:** establish the vendored header + drift CI WITHOUT changing behavior.
> This repo keeps `0x0082 = PIPE_WRITE_END` and has no NFC.
>
> **Why minimal:** `src/main.h` (`RESP_*`, `CONFIG_CHUNK_SIZE`, `MAX_RESPONSE_DATA_SIZE`)
> and `src/structs.h` (`PIPE_VERSION`, `PIPE_FLAG_*`, `PIPE_MAX_FRAME`) already
> define macros the shared header owns. `#include`-ing the shared header into
> those TUs would require de-duplicating them — that work belongs with the
> deferred `communication.cpp` literal→named refactor, NOT this batch.
>
> **Steps:**
> 1. Branch; set commit identity (see conventions).
> 2. Copy the canonical header to `include/opendisplay_protocol.h` (byte-identical).
> 3. Do **NOT** `#include` it anywhere yet, and do NOT touch `main.h`/`structs.h`/
>    `communication.cpp`. The file is vendored now so `--check` passes; the code
>    adopts the names in the later refactor PR.
> 4. Add the CI drift-check (Task E snippet), pointing at `include/opendisplay_protocol.h`.
> 5. Verify the repo still builds unchanged: `pio run` (nRF52840 + esp32 envs from
>    `platformio.ini`). If PlatformIO isn't available, report that.
> 6. Open a PR: "Vendor shared opendisplay_protocol.h v2.0 (+ drift CI); no
>    behavior change." Report per conventions.

---

## Task B — Firmware_NRF54  ·  header + NFC 0x83 cutover + dedup

> **Repo:** `/Users/davelee/Documents/OD/Firmware_NRF54` (Nordic NCS/Zephyr, mixed C/C++).
> **Goal:** replace the local protocol header with the shared v2.0 one; complete
> the NFC `0x0082 → 0x0083` cutover; adopt the opcode-scoped error namespaces;
> dedup local constants.
>
> **Steps:**
> 1. Branch; set commit identity.
> 2. Overwrite `src/opendisplay_protocol.h` with the canonical header (byte-identical).
> 3. **NFC opcode:** the dispatcher `src/opendisplay_pipe.c` (`switch` at ~:1182)
>    uses `case CMD_NFC_ENDPOINT`, which now resolves to `0x0083` automatically.
>    In the NFC handler, grep for any RAW `0x82` used as the *opcode echo* in
>    response frames and repoint to `RESP_NFC_ENDPOINT` (now `0x83`). **Do NOT
>    change** the NFC *sub-status* bytes `0x80`/`0x81`/`0x82` (read-data /
>    write-committed / chunk-accepted) — those are a different field.
> 4. **Error namespaces:** the old header defined `OD_ERR_*`; the shared header
>    renamed them to `OD_ERR_PARTIAL_*` and added `OD_ERR_PIPE_START_*`. Update
>    every `OD_ERR_ETAG_MISMATCH` / `OD_ERR_RECT_OOB` / `OD_ERR_RECT_ALIGN` (and
>    the already-`PARTIAL`-named ones) reference in the partial-write (0x76)
>    handler to `OD_ERR_PARTIAL_*`. NRF54 has no pipe START handler, so no
>    `OD_ERR_PIPE_START_*` usage.
> 5. **Dedup** (macro-redefinition rule): delete from `src/opendisplay_constants.h`
>    the now-shared defines — `CONFIG_CHUNK_SIZE`, `CONFIG_CHUNK_SIZE_WITH_PREFIX`,
>    `MAX_CONFIG_CHUNKS`, `MAX_RESPONSE_DATA_SIZE`, `OD_NFC_IC_*`, `OD_NFC_REC_*`.
>    Keep repo-specific ones (`CONFIG_PKT_*`, `TRANSMISSION_MODE_*`, `GPIO_PIN_UNUSED`,
>    `OD_BUS_TYPE_I2C`, etc.). Ensure `opendisplay_constants.h` (or its includers)
>    still `#include "opendisplay_protocol.h"` so the shared values are visible.
> 6. Build: `./build.sh` / `west build -b xiao_nrf54l15/nrf54l15/cpuapp zephyr/`
>    (needs the NCS env from `ncs-env.sh`). If NCS isn't installed here, compile-
>    sanity the changed `.c`/`.cpp` against the shared header and report that the
>    full build couldn't run.
> 7. Add the CI snippet (Task E), path `src/opendisplay_protocol.h`.
> 8. PR: "Adopt shared protocol v2.0; NFC 0x82→0x83; opcode-scoped error names."
>    Report per conventions, listing the exact dedups and NFC repoints.

---

## Task C — Firmware_Silabs  ·  header + NFC 0x83 cutover + dedup

> **Repo:** `/Users/davelee/Documents/OD/Firmware_Silabs` (SiLabs BG22, SLC+CMake, C + one C++ TU).
> **Goal:** same as B for Silabs. NFC handler here is ALREADY fully symbolic on
> `RESP_NFC_ENDPOINT`, so the `0x83` echo propagates from the `#define` alone.
>
> **Steps:**
> 1. Branch; set commit identity.
> 2. Overwrite root `opendisplay_protocol.h` with the canonical header (byte-identical).
> 3. **NFC opcode:** dispatcher `opendisplay_pipe.c` (`switch` at ~:1011) uses
>    `case CMD_NFC_ENDPOINT` → auto `0x0083`. The NFC handler (`:841-994`) already
>    builds all responses via `RESP_NFC_ENDPOINT` (verified) → no handler edits
>    for the echo. **Confirm** (grep) there are no raw `0x82` opcode-echo literals;
>    leave sub-status `0x80/0x81/0x82` untouched.
> 4. **Error namespaces:** Silabs has no partial-write / pipe START handler, so no
>    `OD_ERR_*` usage to update. (Verify with a grep; update if any exist.)
> 5. **Dedup**: delete from `opendisplay_constants.h` the now-shared defines —
>    `CONFIG_CHUNK_SIZE`, `CONFIG_CHUNK_SIZE_WITH_PREFIX`, `MAX_CONFIG_CHUNKS`,
>    `MAX_RESPONSE_DATA_SIZE`, `OD_NFC_IC_*`, `OD_NFC_REC_*`. Keep repo-specific
>    ones (`CONFIG_PKT_*`, `TRANSMISSION_MODE_*`, `GPIO_PIN_UNUSED`, `OD_BUS_TYPE_I2C`).
>    Ensure the shared header is included where those values are needed.
> 6. Build: SLC generate + CMake (`build-and-flash.sh` / the `cmake_gcc` preset,
>    arm-none-eabi). If the SLC/toolchain isn't available here, compile-sanity the
>    changed files and report.
> 7. Add the CI snippet (Task E), path `opendisplay_protocol.h`.
> 8. PR: "Adopt shared protocol v2.0; NFC 0x82→0x83 (symbolic); dedup constants."
>    Report per conventions.

---

## Task D — Firmware_NRF (nRF52811)  ·  header + delete duplicated block

> **Repo:** `/Users/davelee/Documents/OD/Firmware_NRF` (GNU Make, pure C). No NFC/pipe.
> **Goal:** introduce the shared header and delete the duplicated protocol defines
> currently inlined in `EPD/EPD_service.h`.
>
> **Steps:**
> 1. Branch; set commit identity.
> 2. Copy the canonical header to repo-root `opendisplay_protocol.h`. The Makefile
>    include path already covers root (`INC_FOLDERS += $(PROJECT_ROOT)`), so no
>    build-config edit for the header.
> 3. In `EPD/EPD_service.h`: add `#include "opendisplay_protocol.h"` near the top,
>    and **delete the duplicated block** (`CMD_*` / `AUTH_STATUS_*` / `RESP_*`,
>    ~lines 67-109) plus the now-shared size/encryption defines at ~:111-119
>    (`CONFIG_CHUNK_SIZE`, `CONFIG_CHUNK_SIZE_WITH_PREFIX`, `MAX_RESPONSE_DATA_SIZE`,
>    `BLE_CMD_HEADER_SIZE`, `ENCRYPTION_NONCE_SIZE`, `ENCRYPTION_TAG_SIZE`).
>    **Keep** repo-specific defines: `MAX_CONFIG_CHUNKS` if it differs — check;
>    the encryption *buffer* sizes (`MAX_ENCRYPTED_CIPHERTEXT_LEN`,
>    `MAX_ENCRYPTED_PACKET_SIZE`, `MAX_UNENCRYPTED_PACKET_SIZE`, `MAX_BLE_PACKET_SIZE`),
>    the legacy `EPD_CMDS` enum, and the BLE UUIDs.
> 4. **Watch:** (a) the legacy `EPD_CMDS` enumerators must not collide by NAME with
>    a shared macro (compile error if so — none expected); (b) `RESP_LED_ACTIVATE_ACK`
>    vs `RESP_DIRECT_WRITE_REFRESH_SUCCESS` both `0x73` exist in the shared header
>    exactly as before — fine.
> 5. Consumers (`main.c`, `encryption.c`, `EPD/EPD_service.c`) recompile unchanged
>    against the shared names.
> 6. Build: `make` in `build-nrf52/` (arm-none-eabi). If unavailable, compile-sanity
>    the changed files and report.
> 7. Add the CI snippet (Task E), path `opendisplay_protocol.h`.
> 8. PR: "Adopt shared protocol v2.0; drop duplicated defines from EPD_service.h."
>    Report per conventions.

---

## Task E — CI wiring

**Part 1 — canonical repo (`opendisplay-protocol`).** Add `.github/workflows/protocol.yml`
that (a) compiles the header (`cc -std=c99 -fsyntax-only src/opendisplay_protocol.h`
and a C++ pass) and (b) `python -m py_compile tools/sync_protocol_header.py`. It
cannot `--check` siblings (they aren't checked out in this repo's CI); its job is
to prove the canonical header/tool are valid. Small task; do anytime.

**Part 2 — each firmware repo.** Add a drift-check step to that repo's CI that
compares its vendored copy against canonical `main` (no Python dep, no cross-repo
checkout — the canonical repo is public):

```yaml
- name: opendisplay_protocol.h in sync with canonical
  run: |
    curl -fsSL https://raw.githubusercontent.com/davelee98/opendisplay-protocol/main/src/opendisplay_protocol.h -o /tmp/canonical.h
    diff -u <PATH_TO_VENDORED_COPY> /tmp/canonical.h
```
Replace `<PATH_TO_VENDORED_COPY>` per repo (`include/opendisplay_protocol.h`,
`src/opendisplay_protocol.h`, or `opendisplay_protocol.h`). Fold this into each
migration PR (A–D). **Decision to confirm:** pin to `main` (always-latest, a
canonical edit can retroactively fail an untouched firmware CI) vs a tagged
release (e.g. `v2.0` — firmware adopts deliberately). Recommended: pin to a
**tag** so protocol adoption is an explicit, reviewable bump per repo.

---

## Verification matrix

| Repo | Header in place | Builds | NFC on 0x0083 | 0x0082 intact | Constants deduped |
|---|---|---|---|---|---|
| Firmware | `include/` (vendored, unused) | `pio run` unchanged | n/a (no NFC) | ✅ PIPE_WRITE_END | deferred w/ refactor |
| Firmware_NRF54 | `src/` (replaced) | `west build` | ✅ read/write/chunk | n/a (no pipe) | ✅ |
| Firmware_Silabs | root (replaced) | SLC+CMake | ✅ (symbolic) | n/a (no pipe) | ✅ |
| Firmware_NRF | root (new) | `make` | n/a | n/a | ✅ (EPD_service.h) |

**End-to-end NFC proof** (after B + C merge and flash): drive NFC read (`0x00`),
single write (`0x01`), chunked (`0x10/0x11/0x12`) and assert ACKs are `[00][83]…`
and a forced error returns `[FF][83][FF][err]`; send a legacy `[00][82]` NFC frame
and expect NACK/unknown (proves the clean cutover). **Pipe regression** (Firmware):
a full `0x0080→0x0081→0x0082` transfer still finalizes + refreshes.
