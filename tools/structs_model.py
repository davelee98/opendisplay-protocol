#!/usr/bin/env python3
"""Parse opendisplay_structs.h into a language-neutral payload-layout model.

This is the structs/enums counterpart to `protocol_model.py`. Where that module
reads the macro-only `opendisplay_protocol.h` (wire *values* -- opcodes, responses,
errors), THIS module reads `src/opendisplay_structs.h` (wire *payload layout* -- the
real C `enum`s, bitfield `#define` families, and packed `struct`s that describe every
config-TLV and message payload). It is the ONE parser shared by every language
generator for the payload contract (gen_python_structs.py first; a future Swift/JS
emitter fans out from the same IR), so a new backend is a thin renderer that can never
drift from the others on what the header means.

It hard-errors on anything outside the header's documented subset rather than silently
guessing (same stance as protocol_model). The subset it models:

  * VALUE ENUM
        enum Name { OD_X = <int>, OD_Y = <int>, ... };
    preceded by a `/** @enum Name @width N @external ... @doc "..." */` block; each
    enumerator carries a trailing `/**< @doc "..." @changed "..." */`.

  * BITFIELD GROUP
        /* <Owner.field> @bits <Group> (bits M-N reserved). ... */
        #define OD_X_FLAG   (1u << B)   /* @reserved? @doc "..." */
        #define OD_X_SHIFT  Nu          /* @doc "..." */
        #define OD_X_MASK   0xNNu       /* @doc "..." */
    A group comment carrying `@bits <Group>` binds the run of `#define`s that follow it
    to that group. Members are single-bit flags ((1u << B)), or shift/mask companions
    (a bare integer). Reserved placeholder bits (OD_..._RESERVED_n) are KEPT.

  * PACKED STRUCT
        /** @struct Name @packet 0xNN|outer|single @required @repeatable max=N
         *  @message CMD_X @endian le|be @doc "..." */
        struct Name {
            <uintN_t> field;          /**< @enum X @bits G @endian be @reserved @unit u
                                           @min a @max b @default d @since v @doc "..." */
            <uintN_t> field[LEN];      /**< ... */
        } __attribute__((packed));
        OD_STATIC_ASSERT(sizeof(struct Name) == SIZE, "...");
    Every field's byte width, computed offset (running sum; packed => no padding), C
    type, array length, endianness, and parsed tags are captured.

  * LOOSE CONSTS
        #define OD_CONFIG_VERSION 1u  /* @doc "..." */
    the plain `#define NAME <int|str>` wire constants (OD_STRUCTS_VERSION_*,
    OD_CONFIG_VERSION pair, OD_CONFIG_CRC_*, OD_PIN_UNUSED). Function-like macros
    (OD_STATIC_ASSERT*) and the include guard are ignored.

VALIDATION ORACLE
    The header carries `OD_STATIC_ASSERT(sizeof(struct X) == N, ...)` on every packed
    struct precisely so a subset parser can VALIDATE its computed layout. For each
    struct this module asserts (sum of field widths) == N and HARD-ERRORS on mismatch --
    that assert is the correctness oracle a regex parser lacks natively. It also
    reconciles every hex `@packet` id against the ConfigPacketType enum.

CROSS-HEADER REFS
    A few fields bind to names owned by `opendisplay_protocol.h` (NfcConfig.nfc_ic_type
    -> OD_NFC_IC_*; pipe flags -> PIPE_FLAG_*). Those names are NEVER redefined here;
    the binding is recorded, and values are resolved via protocol_model when the
    protocol header is provided, so emitters can document (not re-declare) them.

Stdlib only; no third-party dependencies. Python 3.8+.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Dict, List, NamedTuple, NoReturn, Optional, Tuple

# Reuse the shared infrastructure from the protocol model so both generators share one
# failure path, one provenance hash, and one drift-gate implementation.
from protocol_model import (  # noqa: F401  (re-exported for the emitter's convenience)
    die,
    parse_header as _parse_protocol_header,
    reconcile,
    source_sha,
)

# Widths of the only scalar C types the header uses (it is uintN_t-only by rule).
_CTYPE_WIDTH: Dict[str, int] = {"uint8_t": 1, "uint16_t": 2, "uint32_t": 4}

# The include region opens at the guard; everything before it is banner prose.
_GUARD_RE = re.compile(r"^#\s*(?:ifndef|define)\s+OPENDISPLAY_STRUCTS_H\b")

# `enum Name {`  /  `struct Name {`  /  end-of-block  / a member line.
_ENUM_OPEN_RE = re.compile(r"^enum\s+([A-Za-z_]\w*)\s*\{")
_STRUCT_OPEN_RE = re.compile(r"^struct\s+([A-Za-z_]\w*)\s*\{")
_ENUM_MEMBER_RE = re.compile(
    r"^(OD_[A-Za-z0-9_]+)\s*=\s*([0-9xXa-fA-F]+)\s*,?\s*(?:/\*\*<(.*?)\*/)?\s*$"
)
_FIELD_RE = re.compile(
    r"^(uint(?:8|16|32)_t)\s+([A-Za-z_]\w*)(?:\[(\d+)\])?\s*;\s*(?:/\*\*<(.*?)\*/)?\s*$"
)
_STATIC_ASSERT_RE = re.compile(
    r"OD_STATIC_ASSERT\(\s*sizeof\(struct\s+([A-Za-z_]\w*)\)\s*==\s*(\d+)"
)

# `#define NAME <rest>`; a `(` immediately after NAME marks a function-like macro (the
# OD_STATIC_ASSERT family) which we skip.
_DEFINE_RE = re.compile(r"^#define\s+([A-Za-z_]\w*)(\()?(?P<rest>.*)$")
_INT_RE = re.compile(r"^(0[xX][0-9A-Fa-f]+|\d+)[uUlL]*$")
_BITSHIFT_RE = re.compile(r"^\(\s*1[uUlL]*\s*<<\s*(\d+)\s*\)$")

# Trailing tags that carry a quoted payload (may span cleaned lines).
_DOC_RE = re.compile(r'@doc\s+"((?:[^"\\]|\\.)*)"')
_CHANGED_RE = re.compile(r'@changed\s+"((?:[^"\\]|\\.)*)"')
_EXTERNAL_RE = re.compile(r"@external\s+(.+?)(?=\s+@\w|\s*$)")


class EnumMember(NamedTuple):
    """One enumerator: OD_NAME = value, with its @doc / @changed prose."""

    name: str
    value: int
    doc: str
    changed: Optional[str]


class ValueEnum(NamedTuple):
    """A C `enum` bound to a scalar field (e.g. ICType, ColorScheme)."""

    name: str
    width: Optional[int]  # @width in bytes (intended on-wire width), if tagged
    external: Optional[str]  # @external provenance note, if any
    doc: str
    members: List[EnumMember]


class BitMember(NamedTuple):
    """One member of a bitfield group: a single-bit flag, or a shift/mask companion."""

    name: str
    kind: str  # "bit" | "shift" | "mask"
    value: int  # bit position (kind=bit) or the literal integer (shift/mask)
    reserved: bool
    doc: str


class BitfieldGroup(NamedTuple):
    """A `#define OD_..._FLAG (1u << N)` family bound by an `@bits <Group>` comment."""

    name: str
    doc: str  # cleaned group-comment prose
    members: List[BitMember]


class Field(NamedTuple):
    """One struct field with computed byte geometry and parsed semantic tags."""

    name: str
    ctype: str  # uint8_t / uint16_t / uint32_t
    array_len: Optional[int]  # None for a scalar; element count for an array
    width: int  # total bytes on the wire (element width * count)
    offset: int  # byte offset from struct start (packed running sum)
    endian: str  # "le" | "be" (meaningful only for multi-byte scalars)
    enum: Optional[str]  # @enum binding (local enum name or a cross-header ref)
    bits: Optional[str]  # @bits group name; "" for an unnamed inline bitfield
    reserved: bool
    unit: Optional[str]
    minimum: Optional[str]
    maximum: Optional[str]
    default: Optional[str]
    since: Optional[str]
    doc: str


class Struct(NamedTuple):
    """A packed wire struct: a config-TLV payload, framing header, or message body."""

    name: str
    packet: Optional[str]  # "0xNN" | "outer" | "single" | None
    message: Optional[str]  # @message CMD_X, for opcode payloads
    required: bool
    repeatable: bool
    repeatable_max: Optional[int]
    endian: str  # struct-level default endianness ("le" unless @endian be)
    size: int  # the OD_STATIC_ASSERT-verified byte size
    doc: str
    fields: List[Field]


class CrossRef(NamedTuple):
    """A binding to a name owned by opendisplay_protocol.h (never redefined here)."""

    binding: str  # the tag token, e.g. "OD_NFC_IC" or "PIPE_FLAG"
    kind: str  # "enum" | "bits"
    members: List[Tuple[str, int]]  # resolved (name, value) pairs, if available


class LooseConst(NamedTuple):
    """A plain `#define NAME <int|str>` wire constant."""

    name: str
    value: str  # token valid as-is in Python/JS (int literal or "string")
    kind: str  # "int" | "str"
    doc: str


class Model(NamedTuple):
    """The whole parsed payload contract; emitters render exclusively from this."""

    version: str  # OD_STRUCTS_VERSION_STR (e.g. "2.0")
    consts: List[LooseConst]
    enums: List[ValueEnum]
    bitfields: List[BitfieldGroup]
    structs: List[Struct]
    crossrefs: List[CrossRef]


# --------------------------------------------------------------------------------------
# Comment / tag helpers
# --------------------------------------------------------------------------------------

def _clean_blob(raw: str) -> str:
    """Collapse a (possibly multi-line) C comment body into one tag-parsable string.

    Strips `/**<`, `/**`, `/*`, `*/`, and per-line leading `*`, then space-joins. The
    result is fed to the `@tag` regexes (so a `@doc` that wraps across lines still
    matches as a single value).
    """
    s = raw.strip()
    for opener in ("/**<", "/**", "/*"):
        if s.startswith(opener):
            s = s[len(opener):]
            break
    if s.endswith("*/"):
        s = s[:-2]
    lines = []
    for line in s.splitlines():
        line = line.strip()
        if line.startswith("*"):
            line = line[1:].strip()
        lines.append(line)
    return " ".join(part for part in lines if part).strip()


def _unescape(text: str) -> str:
    """Turn C string escapes (\\" and \\\\) into their literal characters."""
    return text.replace('\\"', '"').replace("\\\\", "\\")


def _tag_token(blob: str, tag: str) -> Optional[str]:
    """Return the single token following `@tag`, or None if the tag is absent."""
    m = re.search(r"@" + tag + r"\b(?:\s+(?!@)(\S+))?", blob)
    if not m:
        return None
    return m.group(1)  # may be None for a bare tag (e.g. `@bits` with no group)


def _has_tag(blob: str, tag: str) -> bool:
    return bool(re.search(r"@" + tag + r"\b", blob))


def _tag_doc(blob: str) -> str:
    m = _DOC_RE.search(blob)
    return _unescape(m.group(1)) if m else ""


def _parse_int(token: str, ctx: str, lineno: int) -> int:
    """Parse a C integer literal (hex or decimal, optional u/l suffix)."""
    m = _INT_RE.match(token.strip())
    if not m:
        die(f"{ctx} (line {lineno}): cannot parse integer literal {token!r}")
    core = m.group(1)
    return int(core, 16) if core[:2].lower() == "0x" else int(core)


# --------------------------------------------------------------------------------------
# Block parsers
# --------------------------------------------------------------------------------------

def _parse_enum(name: str, body_lines: List[Tuple[int, str]], head_blob: str) -> ValueEnum:
    """Parse an `enum Name { ... }` body plus its preceding `/** @enum ... */` block."""
    members: List[EnumMember] = []
    for lineno, line in body_lines:
        s = line.strip().rstrip("}").strip()
        if not s or s.startswith("/*") or s == "};":
            continue
        m = _ENUM_MEMBER_RE.match(s)
        if not m:
            # Tolerate the closing `};` sharing a line; otherwise it is unmodeled.
            if s in ("};", "}"):
                continue
            die(f"enum {name} (line {lineno}): unmodeled member line {s!r}")
        member_blob = _clean_blob("/*" + (m.group(3) or "") + "*/")
        changed = _CHANGED_RE.search(member_blob)
        members.append(
            EnumMember(
                name=m.group(1),
                value=_parse_int(m.group(2), f"enum {name}", lineno),
                doc=_tag_doc(member_blob),
                changed=_unescape(changed.group(1)) if changed else None,
            )
        )
    width = _tag_token(head_blob, "width")
    ext = _EXTERNAL_RE.search(head_blob)
    return ValueEnum(
        name=name,
        width=int(width) if width and width.isdigit() else None,
        external=ext.group(1).strip() if ext else None,
        doc=_tag_doc(head_blob),
        members=members,
    )


def _parse_struct(
    name: str, body_lines: List[Tuple[int, str]], head_blob: str
) -> Tuple[Struct, int]:
    """Parse a packed struct body + preceding block; return (Struct, computed_size)."""
    struct_endian = _tag_token(head_blob, "endian") or "le"
    fields: List[Field] = []
    offset = 0
    for lineno, line in body_lines:
        s = line.strip()
        if not s or s.startswith("/*") or s.startswith("}"):
            continue
        m = _FIELD_RE.match(s)
        if not m:
            die(f"struct {name} (line {lineno}): unmodeled field line {s!r}")
        ctype, fname, arr, comment = m.group(1), m.group(2), m.group(3), m.group(4) or ""
        blob = _clean_blob("/*" + comment + "*/")
        elem_w = _CTYPE_WIDTH[ctype]
        array_len = int(arr) if arr is not None else None
        width = elem_w * (array_len if array_len is not None else 1)
        fields.append(
            Field(
                name=fname,
                ctype=ctype,
                array_len=array_len,
                width=width,
                offset=offset,
                endian=_tag_token(blob, "endian") or struct_endian,
                enum=_tag_token(blob, "enum"),
                bits=(_tag_token(blob, "bits") or "") if _has_tag(blob, "bits") else None,
                reserved=_has_tag(blob, "reserved"),
                unit=_tag_token(blob, "unit"),
                minimum=_tag_token(blob, "min"),
                maximum=_tag_token(blob, "max"),
                default=_tag_token(blob, "default"),
                since=_tag_token(blob, "since"),
                doc=_tag_doc(blob),
            )
        )
        offset += width

    rep_max = _tag_token(head_blob, "repeatable")
    struct = Struct(
        name=name,
        packet=_tag_token(head_blob, "packet"),
        message=_tag_token(head_blob, "message"),
        required=_has_tag(head_blob, "required"),
        repeatable=_has_tag(head_blob, "repeatable"),
        repeatable_max=int(rep_max.split("=", 1)[1]) if rep_max and "max=" in rep_max else None,
        endian=struct_endian,
        size=offset,  # provisional; reconciled against the static assert by the caller
        doc=_tag_doc(head_blob),
        fields=fields,
    )
    return struct, offset


# --------------------------------------------------------------------------------------
# Top-level parse
# --------------------------------------------------------------------------------------

def parse_structs(text: str, protocol_consts: Optional[Dict[str, int]] = None) -> Model:
    """Parse the canonical structs header into the neutral Model.

    `protocol_consts` (name -> int, e.g. from protocol_model.parse_header) resolves the
    cross-header enum/bits bindings (OD_NFC_IC_*, PIPE_FLAG_*). It is optional -- absent
    it, the bindings are still recorded, just without resolved member values.
    """
    lines = text.splitlines()
    started = False
    pending_blob = ""  # last block comment, cleaned, awaiting the next construct
    bits_group: Optional[str] = None  # active @bits group name for following #defines

    consts: List[LooseConst] = []
    enums: List[ValueEnum] = []
    bitfields: List[BitfieldGroup] = []
    structs: List[Struct] = []
    bitfield_by_name: Dict[str, BitfieldGroup] = {}
    struct_asserts: List[Tuple[str, int, int]] = []  # (name, computed, index)

    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        stripped = raw.strip()

        if not started:
            if _GUARD_RE.match(stripped):
                started = True
            i += 1
            continue

        # ---- block comment: gather it whole; may bind a bitfield group ----
        if stripped.startswith("/*"):
            block = [raw]
            while "*/" not in lines[i] and i + 1 < n:
                i += 1
                block.append(lines[i])
            pending_blob = _clean_blob("\n".join(block))
            # A group comment binds the run of #defines that follows to @bits <Group>.
            for gname in re.findall(r"@bits\s+([A-Za-z_]\w*)", pending_blob):
                if gname not in bitfield_by_name:
                    grp = BitfieldGroup(name=gname, doc=pending_blob, members=[])
                    bitfields.append(grp)
                    bitfield_by_name[gname] = grp
            found = re.findall(r"@bits\s+([A-Za-z_]\w*)", pending_blob)
            bits_group = found[0] if found else None
            i += 1
            continue

        # ---- enum ----
        em = _ENUM_OPEN_RE.match(stripped)
        if em:
            # Gather member lines until the closing "};".
            body_lines: List[Tuple[int, str]] = []
            j = i
            # the opening line may carry a member after "{"
            first_after = stripped[stripped.find("{") + 1:]
            if first_after.strip() and "};" not in first_after:
                body_lines.append((i + 1, first_after))
            while j < n and "};" not in lines[j]:
                if j != i:
                    body_lines.append((j + 1, lines[j]))
                j += 1
            if j < n:  # the "};" line may carry a trailing member
                closing = lines[j].split("};", 1)[0]
                if closing.strip():
                    body_lines.append((j + 1, closing))
            enums.append(_parse_enum(em.group(1), body_lines, pending_blob))
            pending_blob = ""
            bits_group = None
            i = j + 1
            continue

        # ---- struct ----
        sm = _STRUCT_OPEN_RE.match(stripped)
        if sm:
            body_lines = []
            j = i + 1
            while j < n and not lines[j].strip().startswith("}"):
                body_lines.append((j + 1, lines[j]))
                j += 1
            struct, computed = _parse_struct(sm.group(1), body_lines, pending_blob)
            structs.append(struct)
            struct_asserts.append((struct.name, computed, len(structs) - 1))
            pending_blob = ""
            bits_group = None
            i = j + 1
            continue

        # ---- static assert (validation oracle) ----
        am = _STATIC_ASSERT_RE.search(stripped)
        if am:
            aname, asize = am.group(1), int(am.group(2))
            match = next((t for t in struct_asserts if t[0] == aname), None)
            if match is None:
                die(f"OD_STATIC_ASSERT for unknown struct {aname!r} (line {i + 1})")
            _, computed, idx = match
            if computed != asize:
                die(
                    f"layout mismatch for struct {aname}: computed {computed} bytes from "
                    f"fields, but OD_STATIC_ASSERT says {asize} (line {i + 1})"
                )
            structs[idx] = structs[idx]._replace(size=asize)
            i += 1
            continue

        # ---- #define: bitfield member OR loose const ----
        dm = _DEFINE_RE.match(stripped)
        if dm:
            dname, func_like, rest = dm.group(1), dm.group(2), dm.group("rest")
            if func_like or dname.startswith("OD_STATIC_ASSERT") or dname == "OPENDISPLAY_STRUCTS_H":
                i += 1
                continue
            # split value from trailing /* comment */
            cidx = rest.find("/*")
            if cidx != -1:
                comment = rest[cidx:]
                value_raw = rest[:cidx].strip()
            else:
                comment, value_raw = "", rest.strip()
            blob = _clean_blob(comment) if comment else ""

            shift = _BITSHIFT_RE.match(value_raw)
            if bits_group is not None and (shift or dname.endswith(("_SHIFT", "_MASK"))):
                grp = bitfield_by_name[bits_group]
                if shift:
                    grp.members.append(
                        BitMember(dname, "bit", int(shift.group(1)), _has_tag(blob, "reserved"), _tag_doc(blob))
                    )
                else:
                    kind = "shift" if dname.endswith("_SHIFT") else "mask"
                    grp.members.append(
                        BitMember(dname, kind, _parse_int(value_raw, dname, i + 1), False, _tag_doc(blob))
                    )
                i += 1
                continue

            # loose scalar const (keep the source's hex-vs-decimal form; drop u/l suffix)
            if value_raw:
                if len(value_raw) >= 2 and value_raw[0] == '"' and value_raw[-1] == '"':
                    consts.append(LooseConst(dname, value_raw, "str", _tag_doc(blob)))
                elif _INT_RE.match(value_raw):
                    consts.append(
                        LooseConst(dname, _INT_RE.match(value_raw).group(1), "int", _tag_doc(blob))
                    )
                else:
                    die(f"loose const {dname} (line {i + 1}): unclassifiable value {value_raw!r}")
                bits_group = None
            i += 1
            continue

        i += 1

    version = next((c.value.strip('"') for c in consts if c.name == "OD_STRUCTS_VERSION_STR"), "unknown")
    crossrefs = _resolve_crossrefs(enums, structs, bitfields, protocol_consts or {})
    _validate(structs, enums)
    return Model(
        version=version,
        consts=consts,
        enums=enums,
        bitfields=bitfields,
        structs=structs,
        crossrefs=crossrefs,
    )


def _resolve_crossrefs(
    enums: List[ValueEnum],
    structs: List[Struct],
    bitfields: List[BitfieldGroup],
    protocol_consts: Dict[str, int],
) -> List[CrossRef]:
    """Record @enum/@bits bindings that point at opendisplay_protocol.h, resolving values.

    Never redefines a protocol.h name -- only records the binding + (when the protocol
    header is available) the resolved members, so emitters can DOCUMENT the reference.
    """
    local_enums = {e.name for e in enums}
    local_bits = {b.name for b in bitfields}
    seen: Dict[Tuple[str, str], CrossRef] = {}

    def add(binding: str, kind: str) -> None:
        if not binding or (binding, kind) in seen:
            return
        members = sorted(
            ((k, v) for k, v in protocol_consts.items() if k.startswith(binding + "_") or k == binding),
            key=lambda kv: kv[1],
        )
        seen[(binding, kind)] = CrossRef(binding, kind, members)

    for st in structs:
        for f in st.fields:
            # enum bindings that are neither a local enum nor a local loose const usage
            if f.enum and f.enum not in local_enums and f.enum.startswith("OD_NFC_IC"):
                add("OD_NFC_IC", "enum")
            if f.bits and f.bits not in local_bits and f.bits.startswith("PIPE_FLAG"):
                add("PIPE_FLAG", "bits")
    return list(seen.values())


def _validate(structs: List[Struct], enums: List[ValueEnum]) -> None:
    """Cross-checks beyond the per-struct size oracle."""
    # Every hex @packet id must be a ConfigPacketType enumerator value.
    cpt = next((e for e in enums if e.name == "ConfigPacketType"), None)
    if cpt is not None:
        ids = {m.value for m in cpt.members}
        for st in structs:
            if st.packet and st.packet.lower().startswith("0x"):
                pid = int(st.packet, 16)
                if pid not in ids:
                    die(
                        f"struct {st.name} @packet {st.packet} is not a ConfigPacketType value "
                        f"(known: {sorted(hex(v) for v in ids)})"
                    )
    # Every struct must have had its size confirmed by an OD_STATIC_ASSERT.
    for st in structs:
        if st.size <= 0:
            die(f"struct {st.name} has no OD_STATIC_ASSERT size (unvalidated layout)")


def protocol_consts_from(path: Path) -> Dict[str, int]:
    """Flatten opendisplay_protocol.h into a name->int map for cross-ref resolution."""
    if not path.is_file():
        return {}
    groups = _parse_protocol_header(path.read_text(encoding="utf-8"))
    out: Dict[str, int] = {}
    for g in groups:
        for c in g.consts:
            if c.kind == "int":
                core = c.value
                out[c.name] = int(core, 16) if core[:2].lower() == "0x" else int(core)
    return out


def _self_test() -> int:
    """Parse the canonical header and print an IR coverage summary (dev convenience)."""
    root = Path(__file__).resolve().parent.parent
    text = (root / "src" / "opendisplay_structs.h").read_text(encoding="utf-8")
    model = parse_structs(text, protocol_consts_from(root / "src" / "opendisplay_protocol.h"))
    print(f"version           : {model.version}")
    print(f"loose consts      : {len(model.consts)}")
    print(f"value enums       : {len(model.enums)}")
    print(f"bitfield groups   : {len(model.bitfields)} "
          f"({sum(1 for b in model.bitfields if b.members)} with members)")
    print(f"packed structs    : {len(model.structs)}")
    print(f"cross-header refs : {[(c.binding, len(c.members)) for c in model.crossrefs]}")
    for st in model.structs:
        print(f"  {st.name:<24} size={st.size:<4} fields={len(st.fields)} "
              f"packet={st.packet} message={st.message}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
