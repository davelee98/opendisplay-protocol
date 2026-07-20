# Audit — opendisplay-protocol (canonical wire-protocol repo)

**Date:** 2026-07-19
**Branch/commit:** `main` @ `9e9139ef5792d85e7d4c089afa54dbd3800a39b3`
**Scope:** REPORT-ONLY review of the repo as SPEC (src/opendisplay_protocol.h) and as TOOLING (sync + mirror generators), plus docs/agents consistency and cross-repo drift.

---

## 1. Architecture / spec overview

- `src/opendisplay_protocol.h` (861 lines) is the canonical BLE wire contract, **OD_PROTOCOL_VERSION 2.1**, LAST CHANGED 2026-07-19. Macro-only, `u`-suffixed, self-documenting via line-anchored `@opcode/@request/@response/@errors/@state/@limits/@targets/@changed/@since/@collision` blocks. Sections: 1 opcodes, 2 response/status bytes, 3 auth status, 4 opcode-scoped NACK namespaces (4a partial, 4b pipe-start, 4c deep-sleep, 4d power-off), 5 NFC sub-protocol, 6 PIPE constants, 7 chunk budgets, 8 encryption envelope.
- `src/opendisplay_structs.h` (1242 lines) is the companion config/payload-struct header; vendoring of it is explicitly phase-2 (pre-adoption MISSING/DRIFT is documented as expected).
- Derived artifacts: byte-for-byte vendored C copies in the four firmware repos (`tools/sync_protocol_header.py`), plus **generated** mirrors `src/opendisplay_protocol.{py,js,d.ts,swift}` and `opendisplay_structs.{py,js,d.ts,swift}` rendered from one shared parser (`tools/protocol_model.py`, `tools/structs_model.py`), with per-language `--check` drift gates and an independent cross-checker `tools/validate_mirrors.py`.
- Recent history: 2.1 added CMD_POWER_OFF and documented the deep-sleep duration payload (commit `931f9d4`), then commit `2d4fedf` (2026-07-19) **swapped the two opcodes in place** (0x0052=POWER_OFF, 0x0053=DEEP_SLEEP) inside the already-dated 2.1 changelog entry; docs were renamed to match (`5529f57`).

### Verified-good (checked this audit)

- All six generator drift gates pass: `gen_{python,js,swift}_{protocol,structs}.py --check` → exit 0. The language mirrors are current with the header.
- `tools/validate_mirrors.py` passes: 109/109 constant names + values agree across py/js/d.ts/swift; 26/26 struct wire sizes agree. Mirror regeneration is byte-idempotent (regenerated `.py` identical to committed).
- No opcode *value* collisions among CMD_* (0x0F, 0x40–0x45, 0x50–0x53, 0x70–0x77, 0x80–0x83). Byte-value reuse in RESP_*/error namespaces is deliberate, documented, and scoped (0x73 dual meaning, 0xFF dual meaning, cross-namespace error bytes).
- The two "known issues" the audit brief carried — 0x52 `[seconds:2 BE]` payload undocumented / `@errors: none` despite firmware NACKs — are **fixed in the canonical header** (now the 0x0053 block, lines 403–452, with SECTION 4c error namespace). They remain unfixed everywhere the header has not propagated (see §5).

---

## 2. Findings by severity

### CRITICAL

**C1. The byte-for-byte vendoring invariant is broken for 3 of 4 firmware repos — `sync_protocol_header.py --check` FAILS today.**
- Evidence (run 2026-07-19): `[protocol]` Firmware **ok**; Firmware_NRF54 **DRIFT**; Firmware_Silabs **DRIFT**; Firmware_NRF **MISSING**. (`[structs]`: same pattern; structs pre-adoption drift is documented-expected, protocol drift is not.)
- `Firmware_NRF54/src/opendisplay_protocol.h` is a 59-line hand-written stub and `Firmware_Silabs/opendisplay_protocol.h` a 50-line stub — not stale canonical snapshots but independently authored files using the same include guard. They predate/bypass vendoring; only `Firmware/include/opendisplay_protocol.h` (861 lines, current) was ever synced.
- The stubs already diverge from the spec and from each other: NRF54 names 0x0076 `CMD_PARTIAL_WRITE_START` while Silabs names it `CMD_DIRECT_WRITE_PARTIAL_START`; the Silabs stub carries `CMD_NFC_ENDPOINT 0x0083` plus LED/buzzer defines, NRF54's carries a partial-error subset (`OD_ERR_ETAG_MISMATCH` etc. without the `OD_ERR_PARTIAL_` prefix used canonically).
- Failure scenario: any 2.x spec change (e.g. today's 0x52/0x53 swap) reaches only ESP32/nRF52840 firmware; NRF54/Silabs/NRF continue compiling against private constants, and the header's "implement a client from this file alone" promise silently mis-describes three targets.
- Confidence: **Confirmed** (tool output + file inspection).

**C2. Breaking wire change shipped inside a released version number — versioning policy violated by its own spec.**
- `src/opendisplay_protocol.h:70-74` (changelog) and `:396-399`, `:441-447` (`@changed`): CMD_DEEP_SLEEP moved 0x0052→0x0053, swapped with CMD_POWER_OFF, **after** 0x0052=deep-sleep (with `[seconds:2 BE]`) shipped in Firmware PR #97. The file itself classifies this as "BREAKING vs shipped firmware" yet records it as "corrected in place within the (unreleased) 2.1 line rather than a MAJOR bump (amended 2026-07-19)" — but 2.1 is not unreleased: it carries a dated heading "2.1 (2026-07-18)" and `OD_PROTOCOL_VERSION_STR "2.1"` was already the committed released marker before the swap (commit `931f9d4` vs swap commit `2d4fedf`).
- Consequence: two incompatible wire mappings both self-identify as "2.1". A client that pinned "2.1" on 2026-07-18 sends 0x0052 to deep-sleep; against post-swap firmware that is POWER_OFF — worst case a **hard rail-cut instead of a timed sleep** (device unreachable until a physical button press). The version marker no longer discriminates the peer's 0x52/0x53 meaning; per the header's own rule-of-thumb ("could a peer on the previous version misread the wire? yes → MAJOR") this required 3.0.
- Also violates changelog append-only rule (`:113`): the 2.1 entry was rewritten in place.
- Confidence: **Confirmed** (git history + header text).

### HIGH

**H1. No CI exists in this repo — every "CI runs --check" claim is aspirational.**
- The repo has no `.github/` directory and no pre-commit config. Yet `src/opendisplay_protocol.h:124` ("CI runs `sync_protocol_header.py --check` (a hash compare) in each repo"), README.md:37 ("fails CI if a copy has drifted"), and CLAUDE.md:33 state CI enforcement as fact. Nothing gates a push that breaks vendoring or mirror freshness; C1 is the direct result of relying on a manual step ("Firmware adoption is deliberate and per-repo", README.md:118) with no enforcement anywhere.
- Failure scenario: exactly what has happened — header at 2.1, three firmware repos never adopted, no red build anywhere.
- Confidence: **Confirmed**.

**H2. Deep-sleep/power-off fire-and-forget contract is internally inconsistent about who ACKs.**
- `:417-420` (0x0053 @response): "The SUCCESS path emits NO frame"; but `:439` (@targets): "Silabs (ACKs, IGNORES payload, enters EM4)" — so on Silabs the success path *does* emit `[0x00][0x53]`. Likewise 0x0052 `:373-379` says the ACK "is QUEUED on latch HW but usually dies". A client implementing "silence == success, frame == error" per the @response text will mis-handle the documented Silabs ACK; a client waiting briefly for NACKs cannot distinguish "success (silent)" from "NACK lost in teardown". The contract needs one normative rule ("any ACK is possible but MUST NOT be required; only NACK/0xFE frames are meaningful") stated once, not per-target prose.
- Confidence: **Confirmed** (textual contradiction); wire consequence Plausible.

**H3. Silabs deep-sleep "IGNORES payload" makes the one-shot duration silently lossy per-target.**
- `:439-440`: Silabs ACKs 0x0053 and ignores `[seconds:2 BE]`. A cross-target client sending a wake override gets identical positive signaling from a target that will wake at its configured cadence instead. The spec documents the divergence but defines no capability discovery (no flag, no NACK, no response field) letting a client learn the payload was dropped. Ambiguity-by-design in the canonical spec.
- Confidence: **Confirmed** (as spec text).

### MEDIUM

**M1. 0x0081 PIPE_WRITE_DATA NACK error bytes have no named constants.**
- `:590-591` documents `err: 0x02 decompress, 0x03 write/size, 0x04 protocol` in prose only; SECTION 4 deliberately gives every other handler a prefixed namespace (`OD_ERR_PIPE_START_*`, `OD_ERR_PARTIAL_*`, …) but the in-flight pipe NACK codes exist nowhere as macros, so clients/firmware hard-code magic numbers the mirrors cannot carry. Inconsistent with the repo's own "ground every wire byte in a macro" doctrine (cf. NFC record types kept as macros "for that reason", `:801-802`).
- Confidence: **Confirmed**.

**M2. Endianness of the same concepts flips between neighboring opcodes — documented, but a standing foot-gun.**
- etag: `4 BE` in 0x0072/0x0082 END (`:481`, `:602`) and 0x0076 (`:521`), but `old_etag:4 LE` in the 0x0080 partial extension (`:557`). Geometry: BE in 0x0076 (`:522-524`), LE in 0x0080 (`:557`). The header flags it (`:523-525`), yet the spec retains two byte orders for identical fields with error namespaces whose overlapping byte values (0x03/0x05/0x06/0x07) also mean different things across the same two paths. Every new implementation must get four independent traps right; the deliberate 0x0053 BE seconds adds a third endianness exception.
- Confidence: **Confirmed** (as spec risk).

**M3. `sync_protocol_header.py` writes vendored copies non-atomically.**
- `tools/sync_protocol_header.py:183`: `dest.write_bytes(canonical)` — no temp-file + `os.replace`. A crash/disk-full mid-write leaves a truncated vendored header that still passes compilation in some cases (include guard present at top) until `--check` is next run manually (and per H1, nothing runs it). Same pattern in `protocol_model.py:107` (`path.write_text`) for mirrors, mitigated there by same-repo git.
- Failure scenario: interrupted `--push` leaves a firmware repo with a half header that defines a subset of opcodes; C code referencing missing macros fails loudly (good) but a truncation past the last-used macro compiles silently (bad).
- Confidence: Confirmed (code), scenario Plausible.

**M4. `--dest` + `--canonical-url` with default artifacts double-processes one file.**
- `tools/sync_protocol_header.py:190-207` with `:128-136`: when `--dest` is given without `--artifact` but with `--canonical-url`, `selected_artifacts` returns *both* artifacts and `resolve_targets` returns the same single dest for each, so the one file is fetched/compared (or written) twice against the same URL — counts are inflated and, in the documented firmware-CI invocation (README.md:129-131, which omits `--artifact`), `--check` reports 2 ok / or 2 drift for one file. The guard at `:262-263` only fires when *no* canonical override is given; `:258-261` is a dead `pass` branch.
- Confidence: **Confirmed** (code path analysis).

**M5. Spec ambiguity: CONFIG_READ chunk framing underdefined.**
- `:262-269`: `total_len` present "ONLY on chunk 0", data per frame "<= MAX_RESPONSE_DATA_SIZE (100) minus its header" — the header size differs between chunk 0 (6 bytes) and later chunks (4 bytes), and the spec never states whether `MAX_RESPONSE_DATA_SIZE` bounds the *data* or the *frame*; the example budget arithmetic is left to the reader. Also no statement of ordering/loss semantics for the notification stream (BLE notifications are ordered per link, but the spec's "implement from this file alone" bar means it should say so).
- Confidence: Confirmed (text), impact Plausible.

**M6. CMD_CONFIG_WRITE single-vs-chunked discrimination is fragile and only prose-guarded.**
- `:276-281`: chunked mode is inferred from payload > 200 and "the first frame MUST carry a FULL 200 data bytes"; a 201-byte payload frame is ambiguous on its face (is `[total:2][199 bytes]` or `[201 config bytes]`?) — firmware disambiguates by the >200 threshold, but the header's warning that a short first chunk "makes firmware take the single-frame path and NACK the following 0x42 chunks" concedes the encoding is not self-describing. No version/flag byte distinguishes the two forms.
- Confidence: Confirmed (spec design), field failures Plausible.

### LOW

**L1. `@targets` self-contradiction on CMD_READ_MSD (0x0044).** `:322`: "@targets: NRF54 | Silabs | NRF52811 (Firmware handles it but see repo)" — Firmware is simultaneously excluded and included, with an unresolvable pointer ("see repo") in a file that promises no firmware reading is needed. Confirmed.

**L2. CMD_REBOOT auth wording ambiguous.** `:253`: "session required (except on the no-ACK path)" — undefined term; a reader cannot tell whether an unauthenticated 0x0F reboots the device (auth bypass?) or gets 0xFE. As written it can be read as a security hole. Confirmed (text).

**L3. AUTH step-1 vs step-2 success responses are value-ambiguous.** `AUTH_STATUS_SUCCESS == AUTH_STATUS_CHALLENGE == 0x00` (`:706-707`); `[0x00][0x50][0x00]…` is a challenge (23 bytes) or a step-2 success (3 bytes) — disambiguated only by length/sequence, which the spec shows but never states as the normative discriminator. Confirmed.

**L4. Chunk budgets 230/154 are prose-only magic numbers.** `:189-193`: the direct-write data budgets appear nowhere as macros (unlike CONFIG_CHUNK_SIZE / PIPE_MAX_FRAME), so mirrors cannot carry them and clients hard-code them. Confirmed.

**L5. NFC READ has no chunked path though the write buffer is 512 B.** `:624` allows chunked writes to 512 bytes and `OD_PIPE_MAX_PAYLOAD 244` (`:841`) caps a response frame; a stored record > ~236 B cannot be returned by `NFC_SUB_READ` in one frame and the spec defines no read chunking — the read contract is silently bounded below the write contract. Plausible (bounds inferred from spec constants; matches the known "read missing" gap in py-opendisplay).

**L6. Parser silently skips malformed defines.** `tools/protocol_model.py:47`: `_DEFINE_RE` requires literal `#define`; a `#  define NAME 1u` line would be ignored without the promised hard error, dropping the constant from all mirrors while the C compiler still sees it. `validate_mirrors.py` would catch a *name-count* mismatch, so risk is mitigated but the "refuses to silently drop" claim (`protocol_model.py:18`) is not fully true. Confirmed (code), occurrence Hypothetical.

**L7. README omits the Swift mirror path.** README.md:41-52 layout table and the Tools reference list only py/js/d.ts; `src/opendisplay_protocol.swift`, `src/opendisplay_structs.swift` and `tools/gen_swift_*.py` exist and are gated. Doc drift only. Confirmed.

---

## 3. Spec-ambiguity table

| # | Opcode / area | Ambiguity (canonical text) | Risk |
|---|---|---|---|
| A1 | 0x000F REBOOT | Response "none on most targets… Silabs may ACK"; auth "except on the no-ACK path" (L2) | Client timeout logic and auth expectations differ per target with no discriminator |
| A2 | 0x0040 CONFIG_READ | Per-frame data budget arithmetic and stream-ordering semantics left implicit (M5) | Off-by-header-size reassembly bugs |
| A3 | 0x0041 CONFIG_WRITE | Single vs chunked form not self-describing; 200-byte-exact first-chunk rule (M6) | Silent misparse → NACKed 0x42 chunks |
| A4 | 0x0043 FIRMWARE_VERSION | "optional git/SHA string" — encoding/termination unspecified | Cosmetic; parsers guess |
| A5 | 0x0044 READ_MSD | @targets contradicts itself re Firmware (L1); plaintext-ness specified only for Silabs | Unknown gating on other targets |
| A6 | 0x0050 AUTH | Step-1/step-2 success share status 0x00; length is the only (unstated) discriminator (L3) | Misclassification in lenient parsers |
| A7 | 0x0051 ENTER_DFU | "none on nRF… Silabs may ACK" — response optionality per-target | Same class as A1 |
| A8 | 0x0052 POWER_OFF | ACK "usually dies"; MUST-NOT-wait vs possible NACK — how long to listen for NACK is unspecified (H2) | Client can't distinguish success from lost NACK |
| A9 | 0x0053 DEEP_SLEEP | Success emits no frame, but Silabs ACKs (H2); Silabs ignores `[seconds:2 BE]` with no signal (H3); nRF "may instead stay silent and log" | Duration overrides silently dropped; unsupported-target detection unreliable |
| A10 | 0x0072 / 0x0082 END | `[new_etag:4 BE]` "presence by length"; interaction with refresh byte 2/absent (0x82) means one-byte and five-byte forms both legal — partial refresh selection by *absence* is easy to misread | Wrong refresh mode selected |
| A11 | 0x0081 NACK | err codes prose-only, no macros (M1); "further frames discarded until next START" — but no state signal to a client that missed the NACK | Stalled uploads |
| A12 | 0x0083 NFC READ | No chunked read; max readable record size implicit (L5) | Reads of large records undefined |
| A13 | Endianness | Three regimes: LE default, BE in 0x76/END etags+geometry, BE seconds in 0x53, LE in 0x80 partial (M2) | Cross-path copy/paste bugs |
| A14 | Encryption | Which commands are gated vs plaintext is listed, but NACK/0xFE behavior for *plaintext* commands under active security (e.g. 0x43 while encrypted session live) unstated | Interop edge cases |

---

## 4. Tooling audit summary

- `sync_protocol_header.py`: logic sound for the maintainer flow; findings M3 (non-atomic write), M4 (`--dest` double-processing + dead branch `:258-261`); `--check` correctly exits 1 on drift/missing and prints unified diffs; URL fetch has a 30 s timeout and HTTP errors raise (no silent bad-content vendor). Docstring correctly scopes the structs pre-adoption exemption — but there is **no equivalent exemption for protocol**, so today's failing check is a true positive, not tool noise.
- `protocol_model.py` / generators: single-parser design is genuinely drift-proof across languages; handles every macro form present (int literal with suffix, string literal, alias ref) and hard-errors on expressions. L6 is the one silent-skip hole. Generated artifacts are deterministic (embedded header SHA-256, no timestamps) — verified by clean regeneration.
- `validate_mirrors.py`: valuable independent gate (name/value parity + struct sizes vs `OD_STATIC_ASSERT`); passes today. Neither it nor the gen gates run anywhere automatically (H1).

## 5. Cross-repo observations

*(for the synthesis agent — every known/suspected divergence between the canonical spec and a consumer)*

1. **Vendored protocol header: only `Firmware` matches canonical.** `Firmware_NRF54/src/opendisplay_protocol.h` and `Firmware_Silabs/opendisplay_protocol.h` are independent hand-written stubs (59/50 lines) with divergent macro names for 0x0076 (`CMD_PARTIAL_WRITE_START` vs `CMD_DIRECT_WRITE_PARTIAL_START`) and unprefixed partial-error names; `Firmware_NRF` has no copy. `sync --check` fails today (Confirmed).
2. **0x52/0x53 meaning is per-repo right now.** Canonical (post `2d4fedf`, 2026-07-19): 0x52=POWER_OFF, 0x53=DEEP_SLEEP. Shipped firmware (Firmware PR #97) and any client tracking it: 0x52=deep-sleep with `[seconds:2 BE]`, 0x53 unassigned/power-off-draft. Until every firmware + py-opendisplay adopts the swap simultaneously, a "2.1" client cannot know which mapping a device speaks (Confirmed at spec level; per-repo adoption status in the cross-repo section below).
3. **Structs header (`opendisplay_structs.h`) adoption is phase-2 by design** — only `Firmware` carries a copy today; NRF54/Silabs DRIFT (local structs), NRF MISSING. Expected, per tool docstring/CLAUDE.md, but worth tracking because `opendisplay_structs.h` `#include`s the protocol header it will drag along.
4. **CI enforcement exists nowhere**: not in this repo (no `.github/`), and the header claims each firmware repo runs `--check` — verify per-repo; the NRF54/Silabs stubs are strong evidence none do (H1).
5. Known per-target contract divergences *documented in the spec itself* (each is a real wire difference a client must special-case): Silabs ACKs 0x53 and ignores its payload; Silabs keeps 0x44 plaintext under encryption; nRF may answer 0x53 with silence instead of the specified NACK; REBOOT/DFU ACK optionality per target; PIPE is Firmware-only; NFC is NRF54+Silabs-only; 0x45/0x76/0x77 support matrices per the legend (`:196-208`).

## 6. Addendum — `docs/` + `agents/` contradiction sweep (verified by prior audit pass, 2026-07-19)

Sweep of every doc in `docs/` and `agents/` against the v2.1 canonical header, plus firmware-reality verification of the CI claim in item 4 above.

### HIGH
**A1. `docs/opcode-support-matrix.html` is broadly stale (survey dated 2026-07-15 — predates both the v2.0 NFC move and the 2.1 swap).** Confirmed.
- NFC shown on 0x0082 with no 0x0083 row (`:588`, `:430`, `:437-444`, `:492-494`); `:537`/`:547` still frame a "load-bearing collision" that v2.0 resolved.
- 0x0052 labeled "Deep Sleep" (`:578`); no 0x0053 or POWER_OFF rows; 0x0052-as-deep-sleep marks nRF targets "yes" where the canonical header now says unsupported (`:485`).
- Cell mismatches: 0x0045 Silabs should be "yes" per canonical block `:333` (but note the canonical's own internal inconsistency — legend `:205` says NO 0x0045; both sides flagged); 0x0051 matrix marks Firmware=yes while header `@targets` omits Firmware (agents notes say Firmware does DFU via GPREGRET — the header may under-list; flag both); 0x0044 minor. Other rows match.
- Recommended regeneration: add 0x0083 row, retire 0x0082-NFC cells, relabel 0x0052→POWER_OFF, add 0x0053, fix the 0x0045-Silabs and 0x0051 cells; "22 opcodes through 0x0083, no live collision".

### MEDIUM
**A2. Superseded design docs contradict canonical.** `agents/Firmware_NRF54/DESIGN_REPO_STRUCTURE_FOUR_TARGETS.md:28,:308,:326` + `agents/Firmware_NRF54/README.md:30` state NFC on 0x0082 and propose moving it to 0x0090 (plus a 0x0046 GET_CAPS) — a road not taken (canonical chose 0x0083; no 0x0090/0x0046 exist). Deserve "superseded" banners. Confirmed.

**A3. Firmware-reality verification of the CI claim (closes item 4 above): NO repo anywhere runs `sync_protocol_header.py --check`.** Grep of every `.github/workflows/*` in Firmware, Firmware_NRF54, Firmware_Silabs, Firmware_NRF, and py-opendisplay (plus py's `.pre-commit-config.yaml`) for `sync_protocol_header`/`--check`/`opendisplay_protocol` → nothing. `--check` today: NRF54 DRIFT, Silabs DRIFT, Firmware_NRF MISSING; only Firmware ok. The drift gate is unwired ecosystem-wide, which is exactly why four consumers drifted undetected. Confirmed.

### LOW
- `deep-sleep-duration-0x53-contract.md` + `-plan.md`: fully consistent with the v2.1 header (0x0052=POWER_OFF, 0x0053=DEEP_SLEEP, BE seconds, 4c/4d namespaces, fire-and-forget); filenames already renamed `-0x53-`; no stale `-0x52-` refs. Confirmed clean.
- `nfc-capabilities-findings-and-plan.md`: wire content correct; stale line anchors (`:37` cites header lines 561/678-717; actual 651/~790-830). Confirmed.
- `BUZZER_MUSIC_PROTOCOL_REFERENCE.md`: wire content correct; stale anchors (`:95` cites 441-450/589; actual 549/689). `:186`/`:754` correctly record that the header documents a 2-byte buzzer response while firmware emits 4 — a live firmware-vs-header observation, not a doc bug. Confirmed.
- Contextual/historical (no action): `rollout-plan.md` (v2.0-consistent NFC 0x82→0x83; stale `/Users/davelee/...` paths); `implementation-plan.md` (v2.0-era "0x0050-0x0052" band); `shared-types-plan.md:22,:36` correctly flags OD App Swift nfc=0x0082 as stale; agents deep-sleep docs (`DEEP_SLEEP_AUDIT_2026-07-08.md:24`, `FINDINGS_NONBLOCKING_LOOP_2026-07-13.md:117`, `DESIGN_POWER_AND_EVENTS_ZEPHYR.md:192`) describe pre-swap 0x0052 — legitimately historical.
