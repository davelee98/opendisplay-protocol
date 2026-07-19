#!/usr/bin/env python3
"""Generate the Swift payload-structs module from opendisplay_structs.h.

The C header `src/opendisplay_structs.h` is the single source of truth for the LAYOUT
of every config-TLV and message payload on the OpenDisplay BLE wire. Firmware repos
vendor it byte-for-byte; Swift (the OD App) cannot `#include` a C header, so this tool
GENERATES the equivalent Swift module and CI verifies it has not drifted -- the
cross-language analog of the firmware `--check`, and the sibling of
gen_python_structs.py (and the JS emitter). Parsing is done by the shared
`structs_model`; this file is only the renderer -- it can never drift from the Python /
JS mirrors on what the header MEANS, because they all consume the same IR.

CANONICAL SOURCE
    opendisplay-protocol/src/opendisplay_structs.h    (real enums + packed structs)

GENERATED ARTIFACT
    opendisplay-protocol/src/opendisplay_structs.swift (enum / OptionSet + packed structs)

WHAT IS EMITTED
    * loose wire consts               -> module-level `let` constants
    * value enums (ICType, ...)       -> `enum Name: UInt8|UInt16` (raw type from @width;
                                         raw values are the EXACT wire bytes). Unknown /
                                         reserved bytes are handled by Swift's synthesized
                                         failable `init?(rawValue:)` (returns nil, never
                                         traps), so a rogue byte off the wire cannot crash.
    * bitfield groups (@bits)         -> `OptionSet` structs (reserved placeholder bits
                                         kept; shift/mask companions as static constants)
    * packed structs                  -> Swift structs conforming to `ODPackedStruct`, with
                                         a memberwise init plus `init?(bytes:)` /
                                         `serialize() -> [UInt8]` that round-trip wire bytes
                                         at the exact computed offsets and HONOR per-field
                                         endianness (default little-endian; the big-endian
                                         exceptions -- WifiConfig.server_port and all of
                                         PartialWriteStartHeader -- are byte-assembled BE).
                                         Each carries `static let wireSize = N` matching the
                                         header's OD_STATIC_ASSERT.

    Cross-header names (OD_NFC_IC_*, PIPE_FLAG_*) owned by opendisplay_protocol.h are
    NEVER redefined -- the doc comments reference them.

USAGE
    tools/gen_swift_structs.py --write          # (re)generate src/opendisplay_structs.swift
    tools/gen_swift_structs.py --check          # verify it matches (exit 1 on drift)
    tools/gen_swift_structs.py --stdout         # print generated module, write nothing
    tools/gen_swift_structs.py --header FILE --out FILE   # explicit paths

EXIT CODES
    0  success / in sync
    1  drift, missing file, or a layout the parser cannot model / validate

Stdlib only; no third-party dependencies. Python 3.8+. The generated Swift depends only
on the Swift standard library (no Foundation).
"""

from __future__ import annotations

import argparse
import sys
import textwrap
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from structs_model import (
    BitfieldGroup,
    Field,
    Model,
    Struct,
    ValueEnum,
    die,
    parse_structs,
    protocol_consts_from,
    reconcile,
    source_sha,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HEADER = REPO_ROOT / "src" / "opendisplay_structs.h"
DEFAULT_PROTOCOL = REPO_ROOT / "src" / "opendisplay_protocol.h"
DEFAULT_OUT = REPO_ROOT / "src" / "opendisplay_structs.swift"
REGEN_CMD = "tools/gen_swift_structs.py --write"

# C scalar type -> the Swift fixed-width integer used for the stored property.
_SWIFT_INT = {"uint8_t": "UInt8", "uint16_t": "UInt16", "uint32_t": "UInt32"}

# Swift reserved words: any generated identifier colliding with one is backtick-escaped.
_SWIFT_KEYWORDS = {
    "associatedtype", "class", "deinit", "enum", "extension", "fileprivate", "func",
    "import", "init", "inout", "internal", "let", "open", "operator", "private",
    "protocol", "public", "rethrows", "static", "struct", "subscript", "typealias",
    "var", "break", "case", "continue", "default", "defer", "do", "else",
    "fallthrough", "for", "guard", "if", "in", "repeat", "return", "switch", "where",
    "while", "as", "false", "is", "nil", "self", "Self", "super", "throw", "throws",
    "true", "try", "catch", "any", "some",
}


# --------------------------------------------------------------------------------------
# Identifier / naming helpers
# --------------------------------------------------------------------------------------

def _escape(name: str) -> str:
    """Backtick-escape a Swift identifier that collides with a reserved word."""
    return f"`{name}`" if name in _SWIFT_KEYWORDS else name


def _camel(tokens: List[str], *, upper_first: bool = False) -> str:
    """Join `_`-split tokens into camelCase (first token lowered unless upper_first)."""
    parts: List[str] = []
    for i, tok in enumerate(tokens):
        if i == 0 and not upper_first:
            parts.append(tok.lower())
        else:
            parts.append(tok[:1].upper() + tok[1:].lower())
    return "".join(parts)


def _common_prefix_tokens(names: List[str]) -> List[str]:
    """Longest shared leading `_`-token run, always leaving >=1 token per name."""
    token_lists = [n.split("_") for n in names]
    if not token_lists:
        return []
    limit = min(len(t) for t in token_lists) - 1  # keep at least one trailing token
    common: List[str] = []
    for i in range(max(limit, 0)):
        tok = token_lists[0][i]
        if all(tl[i] == tok for tl in token_lists):
            common.append(tok)
        else:
            break
    return common


def _derive_short_names(names: List[str], ctx: str) -> Tuple[List[str], Dict[str, str]]:
    """Map OD_-prefixed macro/enumerator names to idiomatic camelCase Swift names.

    Strips the longest shared token prefix (e.g. OD_COLOR_SCHEME_ -> mono/bwr/...). If
    that would yield an invalid identifier (empty, or leading digit -- e.g.
    OD_ROTATION_0), falls back to stripping only the leading `OD_`. Hard-errors on a
    name collision so the generated enum can never have two cases sharing a spelling.
    """
    def build(common: List[str]) -> Dict[str, str]:
        return {n: _camel(n.split("_")[len(common):]) for n in names}

    common = _common_prefix_tokens(names)
    short = build(common)
    if any((not v) or not v[0].isalpha() for v in short.values()):
        common = names[0].split("_")[:1]  # strip the leading "OD" token only
        short = build(common)
    if len(set(short.values())) != len(short):
        die(f"{ctx}: camelCase name collision among {sorted(names)}")
    return common, short


def _strip_with_prefix(name: str, common: List[str]) -> str:
    """camelCase a companion macro, reusing a group's derived token prefix."""
    toks = name.split("_")
    if toks[: len(common)] == common and len(toks) > len(common):
        rem = toks[len(common):]
    else:
        rem = toks[1:] if toks and toks[0] == "OD" else toks
    return _camel(rem)


def _field_name(f: Field) -> str:
    return _escape(_camel(f.name.split("_")))


# --------------------------------------------------------------------------------------
# Doc-comment helpers
# --------------------------------------------------------------------------------------

def _doc(out: List[str], text: str, indent: str = "") -> None:
    """Emit prose as one or more `///` doc-comment lines, wrapped to ~92 cols."""
    text = (text or "").strip()
    if not text:
        return
    for ln in textwrap.wrap(text, width=92 - len(indent) - 4) or [""]:
        out.append(f"{indent}/// {ln}")


def _field_annotations(f: Field) -> str:
    """Compact tag summary appended to a struct field's doc comment (parity w/ Python)."""
    bits: List[str] = []
    if f.enum:
        bits.append(f"enum {f.enum}")
    if f.bits is not None:
        bits.append(f"bits {f.bits}" if f.bits else "bitfield")
    if f.reserved:
        bits.append("reserved")
    if f.unit:
        bits.append(f"unit {f.unit}")
    if f.width > 1 and f.array_len is None and f.endian == "be":
        bits.append("BIG-ENDIAN")
    for tag, val in (("min", f.minimum), ("max", f.maximum),
                     ("default", f.default), ("since", f.since)):
        if val is not None:
            bits.append(f"{tag} {val}")
    return ("  [" + ", ".join(bits) + "]") if bits else ""


# --------------------------------------------------------------------------------------
# Section renderers
# --------------------------------------------------------------------------------------

def _const_type(value: str, kind: str) -> str:
    if kind == "str":
        return "String"
    iv = int(value, 16) if value[:2].lower() == "0x" else int(value)
    if iv <= 0xFF:
        return "UInt8"
    if iv <= 0xFFFF:
        return "UInt16"
    return "UInt32"


def _render_consts(out: List[str], model: Model) -> None:
    out.append("// MARK: - Loose wire constants")
    out.append("")
    for c in model.consts:
        _doc(out, c.doc)
        out.append(f"public let {c.name}: {_const_type(c.value, c.kind)} = {c.value}")
    out.append("")


def _render_runtime(out: List[str]) -> None:
    out.append("// MARK: - Packed-struct runtime (wire (de)serialization)")
    out.append("")
    out.append("/// A fixed-size packed wire struct that round-trips to/from raw bytes.")
    out.append("/// Byte order is per-field (see each `serialize()` / `init?(bytes:)`).")
    out.append("public protocol ODPackedStruct {")
    out.append("    /// Exact on-wire byte count (mirrors the header's OD_STATIC_ASSERT).")
    out.append("    static var wireSize: Int { get }")
    out.append("    /// Decode exactly `wireSize` bytes; nil if fewer are supplied.")
    out.append("    init?(bytes: [UInt8])")
    out.append("    /// Encode to exactly `wireSize` bytes (arrays zero-padded / truncated).")
    out.append("    func serialize() -> [UInt8]")
    out.append("}")
    out.append("")
    out.append("@inline(__always)")
    out.append("private func odReadUInt16(_ b: [UInt8], _ o: Int, bigEndian: Bool) -> UInt16 {")
    out.append("    bigEndian")
    out.append("        ? (UInt16(b[o]) << 8) | UInt16(b[o + 1])")
    out.append("        : UInt16(b[o]) | (UInt16(b[o + 1]) << 8)")
    out.append("}")
    out.append("")
    out.append("@inline(__always)")
    out.append("private func odReadUInt32(_ b: [UInt8], _ o: Int, bigEndian: Bool) -> UInt32 {")
    out.append("    bigEndian")
    out.append("        ? (UInt32(b[o]) << 24) | (UInt32(b[o + 1]) << 16) | (UInt32(b[o + 2]) << 8) | UInt32(b[o + 3])")
    out.append("        : UInt32(b[o]) | (UInt32(b[o + 1]) << 8) | (UInt32(b[o + 2]) << 16) | (UInt32(b[o + 3]) << 24)")
    out.append("}")
    out.append("")
    out.append("@inline(__always)")
    out.append("private func odAppendUInt16(_ v: UInt16, _ out: inout [UInt8], bigEndian: Bool) {")
    out.append("    let hi = UInt8((v >> 8) & 0xFF)")
    out.append("    let lo = UInt8(v & 0xFF)")
    out.append("    if bigEndian { out.append(hi); out.append(lo) } else { out.append(lo); out.append(hi) }")
    out.append("}")
    out.append("")
    out.append("@inline(__always)")
    out.append("private func odAppendUInt32(_ v: UInt32, _ out: inout [UInt8], bigEndian: Bool) {")
    out.append("    let b0 = UInt8(v & 0xFF)")
    out.append("    let b1 = UInt8((v >> 8) & 0xFF)")
    out.append("    let b2 = UInt8((v >> 16) & 0xFF)")
    out.append("    let b3 = UInt8((v >> 24) & 0xFF)")
    out.append("    if bigEndian { out.append(contentsOf: [b3, b2, b1, b0]) }")
    out.append("    else { out.append(contentsOf: [b0, b1, b2, b3]) }")
    out.append("}")
    out.append("")
    out.append("@inline(__always)")
    out.append("private func odFixedBytes(_ v: [UInt8], _ n: Int) -> [UInt8] {")
    out.append("    if v.count >= n { return Array(v[0..<n]) }")
    out.append("    return v + [UInt8](repeating: 0, count: n - v.count)")
    out.append("}")
    out.append("")


def _render_enum(out: List[str], e: ValueEnum) -> None:
    raw = "UInt16" if e.width == 2 else "UInt8"
    doc = e.doc or f"{e.name} enum."
    if e.width:
        doc = f"{doc}  (on-wire width: {e.width} byte{'s' if e.width != 1 else ''})"
    _doc(out, doc)
    _doc(out, "Unknown wire values decode to nil via Swift's synthesized init?(rawValue:) "
              "(never a trap), so a rogue byte cannot crash the parser.")
    if e.external:
        _doc(out, f"@external {e.external}")
    out.append(f"public enum {e.name}: {raw}, CaseIterable {{")
    _, short = _derive_short_names([m.name for m in e.members], f"enum {e.name}")
    for m in e.members:
        combined = m.doc + (f" (changed: {m.changed})" if m.changed else "")
        _doc(out, combined, indent="    ")
        out.append(f"    case {_escape(short[m.name])} = {m.value}")
    out.append("}")
    out.append("")


def _render_bitfield(out: List[str], b: BitfieldGroup) -> None:
    bit_members = [m for m in b.members if m.kind == "bit"]
    companions = [m for m in b.members if m.kind in ("shift", "mask")]
    if not bit_members:
        # An all-reserved / shared-shape group with no concrete bit macros: document it.
        _doc(out, f"Bitfield group {b.name}: {b.doc}")
        out.append("")
        return
    _doc(out, b.doc or f"{b.name} bit flags.")
    out.append(f"public struct {b.name}: OptionSet {{")
    out.append("    public let rawValue: UInt8")
    out.append("    public init(rawValue: UInt8) { self.rawValue = rawValue }")
    out.append("")
    common, short = _derive_short_names([m.name for m in bit_members], f"bits {b.name}")
    for m in bit_members:
        _doc(out, m.doc, indent="    ")
        out.append(f"    public static let {_escape(short[m.name])} = {b.name}(rawValue: 1 << {m.value})")
    for m in companions:
        val = f"0x{m.value:02X}" if m.kind == "mask" else str(m.value)
        _doc(out, m.doc, indent="    ")
        out.append(f"    public static let {_escape(_strip_with_prefix(m.name, common))}: UInt8 = {val}")
    out.append("}")
    out.append("")


def _render_struct(out: List[str], s: Struct) -> None:
    header = s.doc or f"{s.name} packed wire struct."
    kind: List[str] = []
    if s.packet:
        kind.append(f"config packet {s.packet}")
    if s.message:
        kind.append(f"payload of {s.message}")
    if s.required:
        kind.append("required")
    if s.repeatable:
        kind.append(f"repeatable (max {s.repeatable_max})" if s.repeatable_max else "repeatable")
    tail = f"  [{'; '.join(kind)}]" if kind else ""
    _doc(out, f"{header}{tail}  ({s.size} wire bytes.)")
    out.append(f"public struct {s.name}: ODPackedStruct {{")
    out.append(f"    public static let wireSize = {s.size}")
    if s.packet and s.packet.lower().startswith("0x"):
        out.append(f"    public static let packetID: UInt8 = {s.packet}")
    if s.message:
        out.append(f'    public static let message = "{s.message}"')
    out.append("")

    # Stored properties (memberwise representation).
    for f in s.fields:
        doc = (f.doc or "").strip()
        _doc(out, f"{doc}{_field_annotations(f)}".strip(), indent="    ")
        ftype = "[UInt8]" if f.array_len is not None else _SWIFT_INT[f.ctype]
        out.append(f"    public var {_field_name(f)}: {ftype}")
    out.append("")

    # Memberwise initializer with defaults.
    params = []
    for f in s.fields:
        if f.array_len is not None:
            params.append(f"{_field_name(f)}: [UInt8] = []")
        else:
            params.append(f"{_field_name(f)}: {_SWIFT_INT[f.ctype]} = 0")
    out.append(f"    public init({', '.join(params)}) {{")
    for f in s.fields:
        nm = _field_name(f)
        out.append(f"        self.{nm} = {nm}")
    out.append("    }")
    out.append("")

    # init?(bytes:)
    out.append("    public init?(bytes: [UInt8]) {")
    out.append("        guard bytes.count >= Self.wireSize else { return nil }")
    for f in s.fields:
        nm = _field_name(f)
        if f.array_len is not None:
            out.append(f"        self.{nm} = Array(bytes[{f.offset}..<{f.offset + f.width}])")
        elif f.ctype == "uint8_t":
            out.append(f"        self.{nm} = bytes[{f.offset}]")
        elif f.ctype == "uint16_t":
            be = "true" if f.endian == "be" else "false"
            out.append(f"        self.{nm} = odReadUInt16(bytes, {f.offset}, bigEndian: {be})")
        else:  # uint32_t
            be = "true" if f.endian == "be" else "false"
            out.append(f"        self.{nm} = odReadUInt32(bytes, {f.offset}, bigEndian: {be})")
    out.append("    }")
    out.append("")

    # serialize()
    out.append("    public func serialize() -> [UInt8] {")
    out.append("        var out = [UInt8]()")
    out.append("        out.reserveCapacity(Self.wireSize)")
    for f in s.fields:
        nm = _field_name(f)
        if f.array_len is not None:
            out.append(f"        out.append(contentsOf: odFixedBytes({nm}, {f.width}))")
        elif f.ctype == "uint8_t":
            out.append(f"        out.append({nm})")
        elif f.ctype == "uint16_t":
            be = "true" if f.endian == "be" else "false"
            out.append(f"        odAppendUInt16({nm}, &out, bigEndian: {be})")
        else:  # uint32_t
            be = "true" if f.endian == "be" else "false"
            out.append(f"        odAppendUInt32({nm}, &out, bigEndian: {be})")
    out.append("        return out")
    out.append("    }")
    out.append("}")
    out.append("")


def render_module(text: str, model: Model, header_name: str) -> str:
    sha = source_sha(text)
    out: List[str] = []
    out.append("// @generated by tools/gen_swift_structs.py -- DO NOT EDIT BY HAND.")
    out.append(f"// Source: {header_name} (OD_STRUCTS_VERSION {model.version})")
    out.append(f"// Source SHA-256: {sha}")
    out.append(f"// Regenerate: {REGEN_CMD}   (CI drift gate: --check)")
    out.append("//")
    out.append("// OpenDisplay BLE payload structs, enums, and bitfields.")
    out.append("//")
    out.append(f"// Generated from the canonical {header_name} (OD_STRUCTS_VERSION {model.version}).")
    out.append("// Every layout here describes bytes that travel on the OpenDisplay BLE wire. Do not")
    out.append("// hand-edit; edit the header and regenerate. Byte order is LITTLE-ENDIAN by default;")
    out.append("// the big-endian exceptions (WifiConfig.server_port and all of")
    out.append("// PartialWriteStartHeader) are byte-assembled per-field in serialize()/init?(bytes:).")
    out.append("// Cross-header names (OD_NFC_IC_*, PIPE_FLAG_*) live in opendisplay_protocol.h and are")
    out.append("// referenced, never redefined here. Depends only on the Swift standard library.")
    out.append("")
    _render_consts(out, model)
    _render_runtime(out)

    out.append("// MARK: - Value enums")
    out.append("")
    for e in model.enums:
        _render_enum(out, e)

    out.append("// MARK: - Bitfield groups")
    out.append("")
    for b in model.bitfields:
        _render_bitfield(out, b)

    if model.crossrefs:
        out.append("// MARK: - Cross-header bindings (defined in opendisplay_protocol.h)")
        for cr in model.crossrefs:
            members = ", ".join(f"{n}={v}" for n, v in cr.members) or "(unresolved)"
            _doc(out, f"@{cr.kind} {cr.binding}_*: {members}")
        out.append("")

    out.append("// MARK: - Packed structs")
    out.append("")
    for s in model.structs:
        _render_struct(out, s)

    return "\n".join(out).rstrip("\n") + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="gen_swift_structs.py",
        description="Generate the Swift payload-structs module from opendisplay_structs.h.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="(re)generate and write the module")
    mode.add_argument("--check", action="store_true", help="verify the module matches (exit 1 on drift)")
    mode.add_argument("--stdout", action="store_true", help="print the generated module; write nothing")
    p.add_argument("--header", metavar="FILE", help=f"canonical header path (default: {DEFAULT_HEADER})")
    p.add_argument("--protocol", metavar="FILE", help=f"protocol header for cross-refs (default: {DEFAULT_PROTOCOL})")
    p.add_argument("--out", metavar="FILE", help=f"generated module path (default: {DEFAULT_OUT})")
    args = p.parse_args(argv)

    header_path = Path(args.header) if args.header else DEFAULT_HEADER
    protocol_path = Path(args.protocol) if args.protocol else DEFAULT_PROTOCOL
    out_path = Path(args.out) if args.out else DEFAULT_OUT
    if not header_path.is_file():
        die(f"canonical header not found: {header_path}")

    text = header_path.read_text(encoding="utf-8")
    model = parse_structs(text, protocol_consts_from(protocol_path))
    if not model.structs:
        die(f"no structs parsed from {header_path}")
    generated = render_module(text, model, header_path.name)

    if args.stdout:
        sys.stdout.write(generated)
        return 0

    ok, message = reconcile(out_path, generated, write=args.write, regen_cmd=REGEN_CMD)
    if args.write:
        print(
            f"{message} ({len(model.structs)} structs, {len(model.enums)} enums, "
            f"{sum(1 for b in model.bitfields if any(m.kind == 'bit' for m in b.members))} bitfields)"
        )
        return 0
    print(message, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
