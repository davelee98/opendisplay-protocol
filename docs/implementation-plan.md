# Plan: NFC opcode → 0x0083 + shared self-documenting `opendisplay_protocol.h`

## Context

The OpenDisplay BLE wire protocol has a **load-bearing opcode collision**: `0x0082` means
`CMD_NFC_ENDPOINT` on the NFC firmwares (Firmware_Silabs, Firmware_NRF54) and the OD App, but
`CMD_PIPE_WRITE_END` (image-transfer terminator) on the combined `Firmware` (nRF52840+ESP32) and
py-opendisplay. The root cause is that **the opcode numbers are redeclared independently in all 7
repos with no shared source of truth** — the app even declares them twice (Swift + JS). Bumping NFC
fixes the symptom; a shared header prevents recurrence.

This change does two coordinated things:
1. **Move NFC from `0x0082` → `0x0083`** so `0x0082` is unambiguously `PIPE_WRITE_END` everywhere.
2. **Create one shared, self-documenting `opendisplay_protocol.h`** (the wire-protocol contract),
   vendored into all four firmware repos, with detailed spec/behavior comments suitable for engineer
   and **agent grounding**.

### Decisions (locked with user)
- **Canonical home:** the new spec-only repo **`davelee98/opendisplay-protocol`**, now **public**
  (https://github.com/davelee98/opendisplay-protocol), cloned locally at
  `/Users/davelee/Documents/OD/opendisplay-protocol`. Laid out with `src/` (canonical header) and
  `docs/` (this plan + the opcode support matrix). The four vendored copies + sync destinations in the
  firmware repos are identical to what they'd be anywhere; only the canonical path / push-origin lives
  here.
- **App scope:** firmware-only now; the OD App Swift change is a **tracked follow-up** (NFC has no UI
  caller yet, so nothing breaks in production; NFC stays non-functional end-to-end until the app PR).
- **Cutover:** clean — NFC responds only on `0x0083`; old `0x0082` NFC frames are rejected. No alias.
- **Combined `Firmware`:** header + `#include` only; keep the raw-hex `switch` as-is. Defer the
  literal→named refactor + `main.h` RESP dedup to a separate reviewed commit.
- **Rollout:** branch + PR per **firmware** repo (this is a wire-protocol change, not a chore). The
  canonical `opendisplay-protocol` repo itself is committed directly to `main`.

### Key grounding correction (de-risks the cutover)
The Silabs NFC handler (`Firmware_Silabs/opendisplay_pipe.c:841-994`) already builds **every** response
through the symbol `RESP_NFC_ENDPOINT` — there are **no raw `0x82` opcode-echo literals**. So bumping
the single `RESP_NFC_ENDPOINT` `#define` to `0x83u` propagates automatically. The literal `0x80/0x81/0x82`
bytes in those responses are NFC **sub-status** codes (read-data / write-committed / chunk-accepted) — a
**different field** that must **NOT** change. Verify Firmware_NRF54's handler is likewise symbolic.

---

## Part 1 — The shared header design

**File:** `opendisplay_protocol.h` — macro-only (every constant a `#define`, `u`-suffixed). Macros
have no linkage, so it is safe to `#include` from C and C++ TUs (Arduino C++ in `Firmware`, mixed C/C++
in NRF54, C in Silabs/NRF52811) with **no `extern "C"`**. Keep include guard `OPENDISPLAY_PROTOCOL_H`
(Silabs + NRF54 already use it → drop-in replacements). Authoring base = the existing NRF54 superset
header (`Firmware_NRF54/src/opendisplay_protocol.h`), cleaning up its layout wart (the `OD_ERR_*` block
is currently wedged mid-command-list at lines 17-22).

**Contents (union superset of all repos):**
- Commands: `0x000F`, `0x0040`–`0x0045`, `0x0050`–`0x0052`, `0x0070`–`0x0073`, `0x0075`–`0x0077`,
  `CMD_PIPE_WRITE_START/DATA/END 0x0080/0x0081/0x0082`, `CMD_NFC_ENDPOINT 0x0083`.
- Response bytes: the Silabs/NRF54 `RESP_*` set **plus** the combined-Firmware/NRF52811 direct-write
  family (`RESP_DIRECT_WRITE_*_ACK 0x70/71/72`, `RESP_DIRECT_WRITE_REFRESH_SUCCESS 0x73`,
  `RESP_DIRECT_WRITE_REFRESH_TIMEOUT 0x74`, `RESP_DIRECT_WRITE_ERROR 0xFF`), `RESP_NFC_ENDPOINT 0x83u`,
  and shared `RESP_ACK 0x00 / RESP_NACK 0xFF / RESP_AUTH_REQUIRED 0xFE`.
- `AUTH_STATUS_*`, `OD_ERR_*` (partial-update errors), NFC sub-commands (`0x00/0x01/0x10/0x11/0x12`)
  and rec-types (TEXT/URI/WELL_KNOWN_RAW/MIME/RAW_NDEF — **moved in** from `opendisplay_constants.h`,
  since they are wire bytes an agent must ground on), NFC sub-status codes (0x80/0x81/0x82).
- Pipe wire constants (`PIPE_VERSION 0x01`, `PIPE_FLAG_COMPRESSED 0x01`, `PIPE_FLAG_PARTIAL 0x02`,
  `PIPE_MAX_FRAME 244`, `OD_PIPE_MAX_PAYLOAD 244`), chunk/size constants (`CONFIG_CHUNK_SIZE 200`,
  `CONFIG_CHUNK_SIZE_WITH_PREFIX 202`, `MAX_CONFIG_CHUNKS 20`, `MAX_RESPONSE_DATA_SIZE 100`).
- Version macros `OD_PROTOCOL_VERSION_MAJOR 2u` / `OD_PROTOCOL_VERSION_MINOR 0u` /
  `OD_PROTOCOL_VERSION_STR "2.0"`, governed by the VERSIONING POLICY in the header banner
  (MAJOR = breaking wire change, MINOR = backward-compatible addition).

**Stays OUT** (owned by each repo's `opendisplay_structs.h` / `opendisplay_constants.h`, which diverge):
device-implementation constants (`PIPE_MAX_W`, reorder-slot sizes — per-target `#ifdef` in
`Firmware/src/structs.h`), config-TLV struct layouts, and packet-type / transmission-mode bitfields.
The shared header documents config-packet-type bytes in a **comment table only**, pointing to those files.

**Self-documenting, agent-groundable comment convention.** Top banner: purpose, canonical path,
"VENDORED COPY — DO NOT EDIT — sync via tools/sync_protocol_header.py", `OD_PROTOCOL_VERSION`, and a
universal-framing overview (2-byte **big-endian** opcode; response `[status][cmd_echo][data]` with
`0x00` ACK / `0xFF` NACK / `0xFE` auth-required; encryption-envelope summary; auth gating; chunk
budgets). Then **every opcode gets a uniform, line-anchored tagged block** so `grep '@opcode'`-style
tooling and agents can parse it:

```
@opcode: 0xNNNN   @name: CMD_FOO   @dir: host->device|device->host
@request:  byte-by-byte layout (offsets, sizes, endianness, sub-command byte)
@response: each distinct status/echo/data frame
@errors:   NACK / OD_ERR_* codes this opcode can return
@state:    sequencing/preconditions (auth-first, START→DATA→END, timeouts)
@limits:   payload/chunk size budgets
@targets:  Firmware | NRF54 | Silabs | NRF52811 — which firmwares implement it
@changed / @since:  history (NFC @changed: moved 0x0082→0x0083 in v2)
@collision: only where a byte value is reused (RESP 0x73 = LED-ack vs refresh-success, by direction)
```

Content is sourced from the extracted wire behavior (auth challenge/response, config read/write/chunk,
direct-write, pipe sliding-window, NFC sub-protocol, deep-sleep/DFU/reboot/LED/buzzer/partial). Document
explicitly: `0x0082 = PIPE_WRITE_END`; `0x0083 = NFC (moved from 0x0082 in v2)`; and that the NFC
sub-status `0x82` ("chunk accepted") is unrelated to the opcode.

---

## Part 2 — Distribution / sync

**Vendored plain copy + sync script** (matches the no-submodules convention and the existing
`tools/config_packet.py` duplication precedent across `Firmware` and `Firmware_NRF54`).

- **Canonical:** `opendisplay-protocol/src/opendisplay_protocol.h` (public spec repo) +
  `opendisplay-protocol/tools/sync_protocol_header.py` + a `README` that can embed the opcode matrix.
- **`sync_protocol_header.py`:** canonical source is `src/opendisplay_protocol.h`. `--push` copies
  canonical → each destination (below); `--check` byte-compares and exits non-zero with a diff (the CI
  hook). Keep every copy **byte-identical** so `--check` is a trivial hash compare.
- **Destination map:**
  - `Firmware/include/opendisplay_protocol.h`  (PlatformIO default include path — zero config change)
  - `Firmware_NRF54/src/opendisplay_protocol.h`  (`${SRC_DIR}` already an include dir)
  - `Firmware_Silabs/opendisplay_protocol.h`  (repo root, `"../."` already an include dir)
  - `Firmware_NRF/opendisplay_protocol.h`  (`$(PROJECT_ROOT)` already in `INC_FOLDERS`)
- **CI:** because the canonical repo is **public**, each firmware repo's CI reads the canonical
  `src/opendisplay_protocol.h` directly (a shallow `git checkout` of `opendisplay-protocol` or a fetch
  of the raw URL) and runs `sync_protocol_header.py --check` against it — **no auth / deploy key / SHA
  pinning required**. (Optional alternative: an embedded SHA-256 compare via a tiny per-repo
  `tools/check_protocol_header.sh`.)
- **Linkage:** macro-only ⇒ no `extern "C"` anywhere; banner records the rule for future edits.

---

## Part 3 — Per-repo migration (ordered; branch + PR each)

**Step 0 — Canonical.** The public `opendisplay-protocol` repo is **already initialized and its `main`
pushed**; `docs/` already holds this plan (`implementation-plan.md`) and the opcode support matrix
(`opcode-support-matrix.html`), and `src/` is scaffolded. Remaining: finish authoring
`src/opendisplay_protocol.h` (Part 1, in progress now) and create `tools/sync_protocol_header.py`
(Part 2). Unlike the firmware repos (branch + PR), the canonical `opendisplay-protocol` repo is
committed **directly to `main`** (commit identity uses the GitHub noreply email
`247393336+davelee98@users.noreply.github.com`). Confirm py-opendisplay needs no change (no NFC; its
`0x0082` stays `PIPE_WRITE_END` in `protocol/commands.py`); optionally add a check that its Python
constants match the header.

**Step 1 — Firmware_NRF54.** Replace `src/opendisplay_protocol.h` with the vendored copy (drop-in: same
guard/path). In `src/opendisplay_pipe.c` (dispatch at :1182+), `case CMD_NFC_ENDPOINT` auto-follows the
new `#define`; **grep `handle_nfc_endpoint` for any raw `0x82` opcode-echo literal** and repoint to
`RESP_NFC_ENDPOINT` (leave sub-status `0x80/0x81/0x82` untouched). If NFC sub/rec constants moved into
the shared header, delete their duplicates from `src/opendisplay_constants.h` (or have it include the
shared header). Build with `west`.

**Step 2 — Firmware_Silabs.** Replace root `opendisplay_protocol.h` with the vendored copy (drop-in).
Dispatch (`opendisplay_pipe.c:1011+`) `case CMD_NFC_ENDPOINT` auto-follows; the NFC handler is already
fully symbolic on `RESP_NFC_ENDPOINT` (verified) so the `0x82→0x83` echo propagates from the `#define`
— **no handler edits needed** beyond confirming sub-status bytes are untouched. Same constants-dedup as
Step 1 if applicable. Build via SLC + CMake.

**Step 3 — Firmware (combined).** Add vendored copy at `include/opendisplay_protocol.h`; `#include` it
from `src/communication.cpp` (and `src/main.h`). **No behavioral change** — `0x0082` stays
`PIPE_WRITE_END`, no NFC here. Do **not** refactor the raw-hex `switch` (`communication.cpp:583+`) or
dedup `main.h:50-60` RESP_* now — that is a separate follow-up commit. Build all PlatformIO envs
(nRF52840 + ESP32).

**Step 4 — Firmware_NRF.** Add vendored copy at repo root; confirm the Make include path covers root
(`INC_FOLDERS += $(PROJECT_ROOT)` already does). Edit `EPD/EPD_service.h`: `#include
"opendisplay_protocol.h"` and **delete the duplicated `CMD_/RESP_/AUTH_` block (:67-122)**. Watch:
(a) missing `u` suffixes there → shared macros are `u`-suffixed, fine in C but recheck any
`-Wsign-compare`; (b) the legacy `EPD_CMDS` enum — keep it, but ensure no enumerator NAME collides with
a shared macro (would be a compile error). Dispatch (`EPD/EPD_service.c:618+`) recompiles unchanged.
Build via `make`.

**Step 5 — Sync lock-in.** Run `sync_protocol_header.py --check` across all four; wire it into each
repo's CI / pre-commit. Commit per repo referencing protocol `v2.0` and the `0x82→0x83` move.

**Follow-up (tracked, not in this change-set):**
- **OD App** — `BLE/ODConstants.swift:40` `case nfc = 0x0083` + label at :64; grep app + `Resources/ble-common.js`
  for stray NFC `0x82`/`0x0082`/`130` literals (survey: NFC is built Swift-side). Required for
  end-to-end NFC.
- **Combined Firmware refactor** — convert `communication.cpp` switch literals to `CMD_*` names and
  replace `main.h` local RESP_* with shared ones (reconcile any name/value mismatch before deleting).

---

## Part 4 — Verification

- **Builds:** `pio run` (all `Firmware` envs), `west build` (NRF54, covers C++ inclusion), SLC+CMake
  (Silabs), `make` clean with zero-new-warnings (Firmware_NRF — catches suffix/sign/name-collision from
  the `EPD_service.h` dedup).
- **Sync:** `sync_protocol_header.py --check` green in all four repos.
- **Static wire audit:** `grep -rn '0x0082\|0x82'` each repo post-change — in `Firmware` only
  pipe-END sites; in NRF54/Silabs **zero NFC opcode-echo hits** (sub-status `0x82` bytes allowed); in
  Firmware_NRF none protocol-related.
- **NFC on 0x0083 (E2E):** flash NRF54 + Silabs; drive NFC read (`0x00`), single write (`0x01`), and
  chunked `0x10/0x11/0x12` via a raw-frame script (`bleak`/py-opendisplay, since the app has no NFC UI
  yet). Assert ACKs are `[0x00][0x83]...` and a forced error returns `[0xFF][0x83][0xFF][err]`. Negative:
  send old `[0x00,0x82,...]` NFC frame → expect NACK/unknown (proves clean cutover, no alias).
- **Pipe regression (0x0082 intact):** full pipe image transfer (`0x0080→0x0081→0x0082`) via
  py-opendisplay against combined `Firmware`; END still finalizes + refreshes.
- **NRF52811 regression:** exercise direct-write; confirm `0x70–0x74/0xFF` notifications still emit
  (proves the `EPD_service.h` dedup preserved values exactly).

## Critical files
- `opendisplay-protocol/src/opendisplay_protocol.h` + `opendisplay-protocol/tools/sync_protocol_header.py` (canonical)
- `Firmware_NRF54/src/opendisplay_protocol.h` (authoring base → replaced) + `src/opendisplay_pipe.c:1182+`
- `Firmware_Silabs/opendisplay_protocol.h` (replaced); NFC handler `opendisplay_pipe.c:841-994` (verify symbolic)
- `Firmware_NRF/EPD/EPD_service.h:67-122` (delete dup block, add include)
- `Firmware/include/opendisplay_protocol.h` (new) + `src/communication.cpp:583+` (include only)
- Follow-up: `OD App/BLE/ODConstants.swift:40`
