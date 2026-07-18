# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Canonical BLE wire-protocol spec for OpenDisplay. The entire repo exists to produce one artifact:
`src/opendisplay_protocol.h`, a macro-only, self-documenting C/C++ header that is vendored
byte-for-byte into four downstream firmware repos. There is no application code here — no build,
no test suite. "Correctness" means the header is internally consistent and every vendored copy
matches it exactly.

See the workspace-root `~/Documents/OD/CLAUDE.md` for how this repo relates to the firmware repos,
the app, and the website.

## Repo layout

- `src/opendisplay_protocol.h` — the canonical header (~740 lines). This is the only file that
  defines wire protocol values. Everything else in the repo supports or documents it.
- `tools/sync_protocol_header.py` — vendors the canonical header into sibling firmware repos and
  checks for drift. Stdlib-only, Python 3.8+.
- `docs/implementation-plan.md`, `docs/rollout-plan.md` — historical/planning docs for the v2.0
  header creation and its rollout to firmware repos. Useful for background on *why* the header is
  shaped the way it is (e.g. the NFC 0x0082→0x0083 collision fix), not living specs.
- `docs/opcode-support-matrix.html` — static reference of which firmware targets implement which
  opcodes.
- `docs/shared-types-plan.md` — **proposal** (not yet implemented) to extend this repo from the wire
  *message* contract to the wire *payload* contract: one canonical, annotated C source for the packed
  config structs + enums, codegen'd into firmware/Python/Swift/JS + `config.yaml`. The C declaration
  is the layout oracle (read via libclang); structured `@tag` comments carry the semantics C can't
  express (enum-binding, bitfields, per-field endianness). Read before doing any cross-repo struct/enum
  sharing work.

## Commands

Sync tool (run from repo root; firmware repos are expected as siblings under `~/Documents/OD/`):

```
tools/sync_protocol_header.py --list                        # show canonical + all copy paths
tools/sync_protocol_header.py --check                       # verify all vendored copies match (exit 1 on drift)
tools/sync_protocol_header.py --check --only Firmware_Silabs # scope to one repo
tools/sync_protocol_header.py --push                        # copy canonical -> all vendored copies
tools/sync_protocol_header.py --push --base /path/to/OD     # override the sibling-repos base dir
```

`--dest <file>` operates on one explicit vendored file (for use inside a lone firmware-repo
checkout, e.g. in that repo's own CI, combined with `--canonical-url` to fetch the canonical file
over HTTP instead of reading a local sibling checkout):

```
tools/sync_protocol_header.py --check \
    --canonical-url https://raw.githubusercontent.com/davelee98/opendisplay-protocol/main/src/opendisplay_protocol.h \
    --dest include/opendisplay_protocol.h
```

Syntax-check the header itself (it must compile clean as both C and C++, since it's included from
Arduino C++, mixed C/C++, and pure-C firmware):

```
cc -std=c99 -fsyntax-only src/opendisplay_protocol.h
c++ -fsyntax-only src/opendisplay_protocol.h
```

There is no linter, formatter, or test suite in this repo.

## Editing the header — required workflow

Any edit to `src/opendisplay_protocol.h` MUST follow the **AGENT INSTRUCTIONS** block near the top
of the file itself (read it before editing) — in short, every edit:

1. Updates `LAST CHANGED` to the edit date.
2. Adds a bullet under `Unreleased (since x.y)` in the CHANGELOG describing the change.
3. Classifies the change per the VERSIONING POLICY documented in the header:
   - **MAJOR** bump: any change that could make a peer built against the previous version misread
     the wire — changed/removed/renumbered opcode, response byte, auth state, error code, or a
     changed request/response layout, field size, endianness, or framing rule.
   - **MINOR** bump: backward-compatible addition only — new opcode, response byte, error code, NFC
     sub-command/record type, or a new optional trailing field old parsers ignore.
   - **No bump**: comment/spec clarifications only, or documenting that a firmware now implements an
     already-defined opcode (`@targets` update).
4. When a bump is warranted, updates `OD_PROTOCOL_VERSION_MAJOR` / `_MINOR` / `_STR`, adds a new
   `MAJOR.MINOR (YYYY-MM-DD)` heading, and moves the accumulated Unreleased entries under it.
5. Never rewrites or deletes historical changelog entries — it's append-only.

Structural rules enforced by the header's own banner:
- **Macro-only.** Every constant is a `#define`, `u`-suffixed (e.g. `200u`). No typedefs, enums,
  structs, or functions — those live per-repo in `opendisplay_structs.h` / `opendisplay_constants.h`,
  which stay out of this repo. This is what lets the header be `#include`d from C and C++ translation
  units with no `extern "C"`.
- **Every opcode gets a uniform tagged block** (`@opcode @name @dir @request @response @errors
  @state @limits @targets @changed/@since`, plus `@collision` where a byte value is reused across
  opcodes) — grep-able and meant for both engineers and agent grounding. Match this format exactly
  when adding an opcode.
- **NACK error codes are opcode-scoped, not global.** `data[0]` in a NACK is only meaningful once you
  know the echoed opcode (byte 1) — the same byte value means different things under different
  opcodes (e.g. `0x03` is `OD_ERR_PARTIAL_RECT_OOB` under a 0x76 NACK but
  `OD_ERR_PIPE_START_SIZE_MISMATCH` under a 0x80 NACK). Keep new error codes in their opcode's own
  prefixed namespace (`OD_ERR_PARTIAL_*`, `OD_ERR_PIPE_START_*`, `NFC_ERR_*`, ...) — never add to a
  shared/global enum.
- Keep the include guard `OPENDISPLAY_PROTOCOL_H` so vendored copies stay drop-in replacements.

## Propagating a change downstream

After editing and version-bumping the canonical header:

1. `tools/sync_protocol_header.py --push` from this repo to update the vendored copies in sibling
   firmware repos on disk.
2. Each firmware repo gets its own branch + PR (this repo commits directly to `main`; firmware repos
   do not). Vendored copies must stay **byte-identical** to canonical — never hand-edit a vendored
   copy or reformat it.
3. Firmware repos may locally `#define` values this header also defines (pre-existing duplication).
   When adopting/updating the header in a firmware repo, the shared header wins: delete the local
   duplicate rather than keeping both (a mismatched duplicate, e.g. `200` vs `200u`, is a
   redefinition error). Repo-specific values (struct layouts, transmission-mode bitfields,
   device-implementation constants) are intentionally NOT in this header and stay local.
4. Vendored destinations (all siblings of this repo under `~/Documents/OD/`):
   - `Firmware/include/opendisplay_protocol.h`
   - `Firmware_NRF54/src/opendisplay_protocol.h`
   - `Firmware_Silabs/opendisplay_protocol.h`
   - `Firmware_NRF/opendisplay_protocol.h`

`docs/rollout-plan.md` has detailed, self-contained per-repo migration briefs (written to be handed
to an agent) if you're doing a full adoption pass rather than a small follow-up change.
