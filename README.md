# opendisplay-protocol

**The single source of truth for the OpenDisplay BLE wire protocol.**

Every value that travels on the OpenDisplay BLE wire — command opcodes, response
and status bytes, authentication states, NACK error codes, and the sub-protocol
constants for NFC and the sliding-window image PIPE — is defined exactly once, in
[`src/opendisplay_protocol.h`](src/opendisplay_protocol.h). That header is
self-documenting: a new engineer or an AI agent should be able to implement a
fully-correct client **from that file alone**, without reading any firmware.

The header is then propagated, unchanged, to every consumer so there is never a
second definition to drift out of sync:

```
                       src/opendisplay_protocol.h   ← edit ONLY here
                                   │
      ┌────────────────────────────┼────────────────────────────┐
 vendored byte-for-byte      generated (parsed)           generated (parsed)
 sync_protocol_header.py     gen_python_protocol.py        gen_js_protocol.py
      │                            │                             │
 Firmware/include/…          opendisplay_protocol.py       opendisplay_protocol.js
 Firmware_NRF54/src/…              │                       opendisplay_protocol.d.ts
 Firmware_Silabs/…           → py-opendisplay                    │
 Firmware_NRF/…                (Python clients)            → JS / TS clients

        both generators share ONE parser:  tools/protocol_model.py
```

C/C++ firmware can `#include` the header directly, so its copies are **byte-for-byte
identical**. Python and JavaScript cannot `#include` a C header, so the same values
are **generated** into a Python module and an ES module (`+ .d.ts` for TypeScript).
The two generators are thin renderers over one shared parser (`protocol_model.py`),
so the header is the single source of truth for the *values* and the parser is the
single source of truth for *how they're read* — a new language backend can't drift
from the others on what the header means. Every path has a `--check` mode that fails
CI if a copy has drifted from the canonical header.

## Layout

| Path | What it is |
|---|---|
| `src/opendisplay_protocol.h` | **Canonical** wire-protocol contract (macro-only, self-documenting). Edit here. |
| `src/opendisplay_protocol.py` | **Generated** Python mirror (flat `Final` constants). Do not hand-edit. |
| `src/opendisplay_protocol.js` | **Generated** ES module (flat `export const`). Do not hand-edit. |
| `src/opendisplay_protocol.d.ts` | **Generated** TypeScript declarations with exact literal types. Do not hand-edit. |
| `tools/sync_protocol_header.py` | Vendor the header into the firmware repos (`--push`) and verify (`--check`). |
| `tools/protocol_model.py` | The one shared parser: header → language-neutral constant model. |
| `tools/gen_python_protocol.py` | Render the Python module from the model (`--write` / `--check`). |
| `tools/gen_js_protocol.py` | Render the `.js` + `.d.ts` from the model (`--write` / `--check`). |
| `docs/` | Design notes, the adoption [rollout plan](docs/rollout-plan.md), and an opcode support matrix. |
| `agents/<repo>/` | Cross-repo findings / architecture notes, filed by the repo they concern. |

## The header

`opendisplay_protocol.h` is **macro-only** — every constant is a `u`-suffixed
`#define`, no typedefs / enums / structs / functions. Macros have no linkage, so
the header is safe to `#include` from C and C++ with no `extern "C"` block, and
the vendored copies are drop-in replacements.

It is organized into numbered sections (command opcodes, response bytes, auth
status, opcode-scoped NACK error namespaces, NFC, PIPE, size budgets, encryption
envelope). Each opcode carries a uniform, line-anchored tag block so tooling and
agents can parse it:

```
@opcode @name @dir   identity + direction         @errors    NACK / error codes
@request             byte-by-byte request layout   @state     sequencing, timeouts
@response            each status/echo/data frame   @limits    payload / chunk budgets
@targets             which firmwares implement it  @collision where a byte is reused
```

**Two rules make the header safe to propagate mechanically:**

1. **Values stay simple** — an integer literal (`0x0041u` / `200u`), a string
   literal, or a reference to a macro already defined above. No expressions
   (`(1u << 3)`), casts, or multi-token values. The Python generator hard-errors
   on anything else rather than silently dropping it.
2. **Byte values may be reused across names** (e.g. `0x73` is both
   `RESP_LED_ACTIVATE_ACK` and `RESP_DIRECT_WRITE_REFRESH_SUCCESS`; `0xFF` is both
   `RESP_NACK` and `RESP_DIRECT_WRITE_ERROR`). NACK error codes are **opcode-scoped**:
   `data[0]` is only meaningful once you know the echoed opcode. This is why the
   generated Python uses flat constants, not one `IntEnum` — an enum would alias
   duplicate values and erase a name.

## Versioning

`OD_PROTOCOL_VERSION` is `MAJOR.MINOR`, describes the spec in the header, and is
**not** transmitted on the wire (the negotiated `0x0080` `PIPE_VERSION` is a
separate field). Bump **MAJOR** for a breaking wire change (changing the value or
meaning of an existing opcode / status / error, a layout/endianness/framing
change, or removing/renumbering a code); bump **MINOR** for a backward-compatible
addition; **no bump** for comment-only clarifications. The full policy, the rule of
thumb, and the append-only changelog live in the header's banner — read it before
bumping.

## Making a change

Edit the canonical header, then propagate to **both** consumer worlds and verify:

```bash
# 1. Edit src/opendisplay_protocol.h. Per the header's AGENT INSTRUCTIONS, also
#    update LAST CHANGED, add a CHANGELOG bullet under "Unreleased", and bump
#    OD_PROTOCOL_VERSION if the change is breaking (MAJOR) or additive (MINOR).

# 2. Regenerate the Python and JS mirrors (same repo, commit alongside the header):
tools/gen_python_protocol.py --write
tools/gen_js_protocol.py --write
tools/gen_python_protocol.py --check      # should print "ok"
tools/gen_js_protocol.py --check          # should print "ok" (.js + .d.ts)

# 3. Push the header into the firmware repos (they are sibling checkouts):
tools/sync_protocol_header.py --push
tools/sync_protocol_header.py --check     # should be "all in sync"
tools/sync_protocol_header.py --list      # show the canonical + copy paths
```

Firmware adoption is deliberate and per-repo: each firmware repo commits its
updated vendored copy on its own branch/PR. See [docs/rollout-plan.md](docs/rollout-plan.md)
for the migration briefs and the recommended CI drift-check snippet.

## Verifying a copy in CI (no cross-repo checkout needed)

Both tools can verify a lone checkout against the canonical header fetched from
GitHub, so a firmware repo's CI needs no dependency on this one:

```bash
# firmware repo (C) — byte-for-byte compare of its vendored copy:
tools/sync_protocol_header.py --check \
  --canonical-url https://raw.githubusercontent.com/davelee98/opendisplay-protocol/main/src/opendisplay_protocol.h \
  --dest include/opendisplay_protocol.h

# Python consumer — regenerate into a buffer and diff (exit 1 on drift):
tools/gen_python_protocol.py --check --header path/to/opendisplay_protocol.h

# JS / TS consumer — verify the .js and .d.ts against the header (exit 1 on drift):
tools/gen_js_protocol.py --check --header path/to/opendisplay_protocol.h
```

All exit `0` when in sync and `1` on drift or a missing copy.

## Tools reference

All are stdlib-only, Python 3.8+, and share the same `--check` / exit-code
contract. The two generators render over one shared parser, `protocol_model.py`
(header → language-neutral model); it is imported, not run directly.

**`sync_protocol_header.py`** — vendor the header into the four firmware repos.
- `--push` — write the canonical bytes into every vendored copy
- `--check` — verify every copy is byte-identical (exit 1 on drift, prints a diff)
- `--list` — print the canonical path and the copy map
- `--only NAME[,NAME]`, `--base DIR`, `--dest FILE`, `--canonical[-url]` — scope / redirect

**`gen_python_protocol.py`** — render the Python constants module from the header.
- `--write` — (re)generate `src/opendisplay_protocol.py`
- `--check` — verify the module matches the header (exit 1 on drift, prints a diff)
- `--stdout` — print the module without writing
- `--header FILE`, `--out FILE` — explicit paths

**`gen_js_protocol.py`** — render the ES module + TypeScript declarations.
- `--write` — (re)generate `src/opendisplay_protocol.js` and `.d.ts`
- `--check` — verify both files match the header (exit 1 on drift, prints a diff)
- `--stdout` — print the `.js` without writing
- `--header FILE`, `--out-js FILE`, `--out-dts FILE` — explicit paths

Generated artifacts are deterministic (each embeds the header's SHA-256, no
timestamp). The Python module passes `ruff`, `ruff format`, and `mypy --strict`;
the `.js` is valid ESM and the `.d.ts` gives exact literal types.
