# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

`opendisplay-protocol` is the **single canonical source** for the OpenDisplay BLE wire protocol. It is a spec repo, not a library: the deliverable is one self-documenting C header that every firmware repo vendors byte-for-byte, plus a generated Python mirror.

This is one repo inside the larger OpenDisplay multi-repo workspace (see `../CLAUDE.md`). The end-to-end pipeline is: ODL drawing instructions → `odl-renderer` → `epaper-dithering` → `py-opendisplay` (BLE upload/framing) → firmware on the tag. This repo defines the wire contract that `py-opendisplay` and the four firmware repos must agree on.

## The one invariant that matters

`src/opendisplay_protocol.h` is the source of truth. **Two derived artifacts must never drift from it:**

1. **Byte-for-byte vendored copies** in each firmware repo (siblings on disk):
   - `Firmware/include/opendisplay_protocol.h`
   - `Firmware_NRF54/src/opendisplay_protocol.h`
   - `Firmware_Silabs/opendisplay_protocol.h`
   - `Firmware_NRF/opendisplay_protocol.h`
2. **Generated Python module** `src/opendisplay_protocol.py` (Python can't `#include` C).

**Never hand-edit a vendored copy or the generated `.py`.** Edit the canonical `.h`, then propagate and verify:

```bash
tools/sync_protocol_header.py --push                     # both canonical .h → all firmware copies
tools/sync_protocol_header.py --check                     # fail if any vendored copy drifted (CI/pre-commit)
tools/sync_protocol_header.py --check --artifact protocol # scope to one header (protocol | structs)
tools/sync_protocol_header.py --list                      # show the copy map
tools/gen_python_protocol.py --write      # regenerate src/opendisplay_protocol.py
tools/gen_python_protocol.py --check       # fail if the .py drifted (CI/pre-commit)
```

`sync_protocol_header.py` vendors **both** canonical C headers — `opendisplay_protocol.h` and `opendisplay_structs.h` — into the firmware repos (use `--artifact protocol|structs` to scope). The `structs` copies show MISSING/DRIFT until each firmware repo adopts the shared header (shared-types-plan phase 2), so pre-adoption CI can scope to `--artifact protocol`. The generated language mirrors (`opendisplay_{protocol,structs}.{py,js,d.ts,swift}`) are NOT vendored by this tool — each has its own `gen_*` drift gate. `tools/validate_mirrors.py` is a complementary CI check: the `gen_* --check` gates only prove a mirror is idempotent (regenerating reproduces it), while `validate_mirrors.py` independently parses the headers and every mirror and asserts they agree (constant name/value parity across the four protocol mirrors; struct wire sizes vs each header `OD_STATIC_ASSERT`) — catching a generator bug that consistently emits a wrong value. All tools are stdlib-only, Python 3.8+, no third-party deps. `--check` is a hash/diff compare and is what CI runs in each consumer repo. Firmware CI fetches the canonical header from GitHub raw via `--canonical-url ... --dest ...`.

## Editing the header

The header is authored under strict rules so both tools can parse it and so C/C++ firmware can drop it in unchanged:

- **MACRO-ONLY.** Every constant is a `#define`, `u`-suffixed. No typedefs, enums, structs, functions, or `extern "C"` (macros have no linkage; typedefs/enums belong in each repo's `opendisplay_structs.h` / `opendisplay_constants.h`). Keep the `OPENDISPLAY_PROTOCOL_H` include guard.
- **Macro values stay simple** — an integer literal (`0x0041u`), a string literal (`"2.0"`), or a reference to an already-defined macro. **No expressions** (`(1u << 3)`), casts, or multi-token values. `gen_python_protocol.py` hard-errors on any other shape rather than silently dropping it.
- **Duplicate byte values are intentional** (e.g. `0x73` is both `RESP_LED_ACTIVATE_ACK` and `RESP_DIRECT_WRITE_REFRESH_SUCCESS`; `0xFF` is both `RESP_NACK` and `RESP_DIRECT_WRITE_ERROR`). This is why the Python mirror uses flat `Final` constants, not an `IntEnum` — an IntEnum would silently alias the second name away.

**On every edit to the header** (enforced by the AGENT INSTRUCTIONS banner at the top of the file):
1. Update the `LAST CHANGED` date.
2. Add a bullet under `Unreleased (since x.y)` in the CHANGELOG.
3. Classify per the VERSIONING POLICY: breaking wire change → bump MAJOR (`OD_PROTOCOL_VERSION_MAJOR`, reset MINOR to 0); backward-compatible addition → bump MINOR; doc-only → no bump. Keep `_STR` in sync. The changelog is append-only.

Rule of thumb: "could a peer on the previous version misread the wire?" yes → MAJOR, new-but-compatible → MINOR, neither → no bump. The version marker describes the spec and is **not** sent on the wire (the negotiated `0x0080` PIPE_VERSION is a separate field).

After any header edit, run **all four** tool commands above (`--push`, `--check`, `--write`, `--check`) so both derived artifacts and every vendored copy stay in sync.

## Wire protocol quick map (all detail lives in the header)

- **Request framing:** `[cmd_hi][cmd_lo][payload...]` — opcode is 2 bytes **big-endian**; payload fields **little-endian** unless a block says "BE".
- **Response/notification framing:** `[status][cmd_echo][data...]` — status `0x00`=ACK, `0xFF`=NACK, `0xFE`=auth-required; `cmd_echo` is the low byte of the command.
- **NACK error codes are opcode-scoped.** `data[0]` is decoded only in the scope of the echoed opcode — there is deliberately no global error enum, so the same byte means different things under different opcodes (e.g. `0x03` = `OD_ERR_PARTIAL_RECT_OOB` under a `0x76` NACK but `OD_ERR_PIPE_START_SIZE_MISMATCH` under `0x80`).
- **Encrypted envelope:** `[cmd:2 BE][nonce:16][ciphertext][tag:12]`, AES-128-CCM, AAD = the 2 plaintext opcode bytes. `AUTHENTICATE` (0x50) and `FIRMWARE_VERSION` (0x43) are always plaintext (bootstrap); Silabs also leaves `READ_MSD` (0x44) plaintext.
- The header is organized into SECTIONS 1–8 (opcodes, response bytes, auth status, opcode-scoped NACK namespaces, NFC sub-protocol on `0x0083`, PIPE sliding-window constants `0x0080..0x0082`, chunk/size budgets, encryption sizes). Every opcode block uses a uniform, line-anchored `@opcode`/`@request`/`@response`/`@errors`/`@targets` tag convention so `grep '@opcode'` and agents can parse it.

**Four firmware targets differ in which opcodes they implement** (see the FIRMWARE TARGET LEGEND and per-opcode `@targets` in the header): `Firmware` (nRF52840+ESP32, has PIPE, no NFC), `NRF54` (has NFC, no PIPE), `Silabs` (config/auth/DFU/NFC, no PIPE/partial), `NRF52811` (minimal, no NFC/PIPE/buzzer). When changing an opcode, check its `@targets` line.

## Other contents

- `src/opendisplay_structs.h` — canonical shared header for the OpenDisplay **configuration-protocol and wire-payload** types (packed config TLV structs, framing, message/advertisement payloads, and their enums/bitfields). Payload counterpart to `opendisplay_protocol.h` (which owns opcodes/responses/errors); it `#include`s that header and never redeclares a name it defines. Authored in annotated C with structured `@tag` comments (`@packet`/`@enum`/`@bits`/`@endian`/`@reserved`/`@required`/`@repeatable`/`@message`/`@external`/…) so it is codegen-ready. Verified byte-for-byte against `config.yaml` `ble_proto` and the four firmware `structs.h` supersets. Vendoring + codegen are tracked as later phases (see the plan docs below).
- `docs/` — working design/investigation notes (not user docs): `implementation-plan.md`, `rollout-plan.md`, buzzer-protocol design/reference, `opcode-support-matrix.html`, and the shared-types work: `shared-types-plan.md` (the codegen-from-annotated-C strategy) and `opendisplay-structs-draft-plan.md` (inventory, layout reconciliations, resolved decisions, and drafting checklist for `opendisplay_structs.h`). Read the relevant one before touching that subsystem. **These docs should generally all be committed to the repo** — treat `docs/` as version-controlled project history, not scratch. Don't leave design/investigation notes untracked or gitignore them; commit new ones alongside the header changes they describe.
- `agents/<repo>/` — cross-repo `DESIGN_*` / `FINDINGS_*` / audit notes carried over from the firmware, HA-integration, and py-opendisplay repos. Useful background when a header change touches BLE throughput, deep-sleep, WiFi, or config-packet behavior.
