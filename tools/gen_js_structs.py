#!/usr/bin/env python3
"""Generate the JavaScript payload-structs module from opendisplay_structs.h.

The C header `src/opendisplay_structs.h` is the single source of truth for the LAYOUT
of every config-TLV and message payload on the OpenDisplay BLE wire. Firmware repos
vendor it byte-for-byte; JavaScript cannot `#include` a C header, so this tool GENERATES
the equivalent ES module and CI verifies it has not drifted -- the JS sibling of
gen_python_structs.py and the structs analog of gen_js_protocol.py. Parsing is done by
the shared `structs_model` (the SAME IR the Python emitter consumes); this file is only
the renderer.

CANONICAL SOURCE
    opendisplay-protocol/src/opendisplay_structs.h   (real enums + packed structs)

GENERATED ARTIFACTS (ESM + TypeScript declarations)
    opendisplay-protocol/src/opendisplay_structs.js   (frozen enums + packed classes)
    opendisplay-protocol/src/opendisplay_structs.d.ts  (exact-literal type declarations)

The .js is dependency-free ESM usable in BOTH the browser (consumed by ble-common.js)
and Node with no build step; the .d.ts gives TypeScript consumers the literal types.

WHAT IS EMITTED
    * loose wire consts               -> flat `export const` (protocol.js style)
    * value enums (ICType, ...)       -> frozen name->value objects (Object.freeze);
                                         every member carries its @doc as JSDoc
    * bitfield groups (@bits)         -> flat `export const NAME = 1 << B` bit constants
                                         (+ shift/mask companions); reserved bits kept
    * packed structs                  -> ES classes over a shared _PackedStruct base whose
                                         generic pack()/unpack() round-trips wire bytes via
                                         DataView, HONORING per-field endianness (default
                                         little-endian; WifiConfig.server_port and all of
                                         PartialWriteStartHeader are big-endian)

    Cross-header names (OD_NFC_IC_*, PIPE_FLAG_*) owned by opendisplay_protocol.h are
    NEVER redefined -- the field docs reference them; import them from
    opendisplay_protocol.js where needed.

USAGE
    tools/gen_js_structs.py --write          # (re)generate the .js and .d.ts
    tools/gen_js_structs.py --check           # verify both match (exit 1 on drift)
    tools/gen_js_structs.py --stdout          # print the .js module; write nothing
    tools/gen_js_structs.py --header FILE --protocol FILE --out-js FILE --out-dts FILE

EXIT CODES
    0  success / in sync
    1  drift, missing file, or a layout the parser cannot model / validate

Stdlib only; no third-party dependencies. Python 3.8+.
"""

from __future__ import annotations

import argparse
import sys
import textwrap
from pathlib import Path
from typing import List, Optional

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
DEFAULT_OUT_JS = REPO_ROOT / "src" / "opendisplay_structs.js"
DEFAULT_OUT_DTS = REPO_ROOT / "src" / "opendisplay_structs.d.ts"
REGEN_CMD = "tools/gen_js_structs.py --write"

# Per-field byte order -> the `littleEndian` boolean DataView.getUint16/32 expects.
_ENDIAN_LE = {"le": "true", "be": "false"}


# --------------------------------------------------------------------------------------
# Comment / JSDoc helpers
# --------------------------------------------------------------------------------------

def _jsdoc(text: str, indent: str = "") -> List[str]:
    """Render prose as a JSDoc block (`/** ... */`), wrapped to ~92 cols."""
    text = (text or "").strip()
    if not text:
        return []
    wrapped = textwrap.wrap(text, width=max(20, 92 - len(indent) - 4))
    if len(wrapped) <= 1:
        return [f"{indent}/** {wrapped[0] if wrapped else ''} */"]
    out = [f"{indent}/**"]
    out.extend(f"{indent} * {ln}" for ln in wrapped)
    out.append(f"{indent} */")
    return out


def _line_comment(text: str, indent: str = "") -> List[str]:
    """Render prose as one or more `// ...` line comments, wrapped to ~92 cols."""
    if not text:
        return []
    wrapped = textwrap.wrap(text, width=max(20, 92 - len(indent) - 3)) or [""]
    return [f"{indent}// {ln}" for ln in wrapped]


def _field_annotations(f: Field) -> str:
    """Compact tag summary shown alongside a struct field's doc comment."""
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
    for tag, val in (("min", f.minimum), ("max", f.maximum), ("default", f.default), ("since", f.since)):
        if val is not None:
            bits.append(f"{tag} {val}")
    return ("  [" + ", ".join(bits) + "]") if bits else ""


# --------------------------------------------------------------------------------------
# .js renderer
# --------------------------------------------------------------------------------------

def _js_banner(text: str, model: Model, header_name: str) -> List[str]:
    sha = source_sha(text)
    return [
        "// @generated by tools/gen_js_structs.py -- DO NOT EDIT BY HAND.",
        f"// Source: {header_name} (OD_STRUCTS_VERSION {model.version})",
        f"// Source SHA-256: {sha}",
        f"// Regenerate: {REGEN_CMD}   (CI drift gate: --check)",
        "//",
        "// OpenDisplay BLE payload structs, enums, and bitfields. Every layout describes bytes",
        "// that travel on the OpenDisplay BLE wire. Dependency-free ESM for browser + Node.",
        "// Byte order is LITTLE-ENDIAN by default; the big-endian exceptions",
        "// (WifiConfig.server_port and all of PartialWriteStartHeader) are honored per-field by",
        "// each struct's pack()/unpack() via DataView. Value enums are frozen name->value",
        "// objects; bitfield flags are flat `export const` (protocol.js style). Cross-header",
        "// names (OD_NFC_IC_*, PIPE_FLAG_*) live in opendisplay_protocol.js and are referenced,",
        "// never redefined here.",
        "",
    ]


def _render_consts_js(out: List[str], model: Model) -> None:
    out.append("// ===== Loose wire constants =====")
    out.append("")
    for c in model.consts:
        line = f"export const {c.name} = {c.value};"
        if c.doc:
            line = f"{line}  // {c.doc}"
        out.append(line)
    out.append("")


_RUNTIME_JS = '''// ===== Packed-struct runtime (generic wire (de)serialization) =====

/** Write an unsigned scalar of arbitrary byte `width` at `offset`, honoring `le`
 * (little-endian). Widths 1/2/4 use DataView; any other width (e.g. a 3-byte /
 * 24-bit field) uses a byte loop with safe-integer arithmetic (widths up to 6
 * bytes stay within Number.MAX_SAFE_INTEGER), for parity with the Python/Swift
 * mirrors which handle arbitrary widths. */
function _writeScalar(view, offset, width, value, le) {
  switch (width) {
    case 1: view.setUint8(offset, value & 0xff); return;
    case 2: view.setUint16(offset, value & 0xffff, le); return;
    case 4: view.setUint32(offset, value >>> 0, le); return;
    default: {
      let v = Math.trunc(value);
      for (let i = 0; i < width; i++) {
        view.setUint8(offset + (le ? i : width - 1 - i), v % 256);
        v = Math.floor(v / 256);
      }
      return;
    }
  }
}

/** Read an unsigned scalar of arbitrary byte `width` at `offset`, honoring `le`
 * (little-endian). Mirrors _writeScalar. */
function _readScalar(view, offset, width, le) {
  switch (width) {
    case 1: return view.getUint8(offset);
    case 2: return view.getUint16(offset, le);
    case 4: return view.getUint32(offset, le);
    default: {
      let v = 0;
      for (let i = 0; i < width; i++) {
        v = v * 256 + view.getUint8(offset + (le ? width - 1 - i : i));
      }
      return v;
    }
  }
}

/**
 * Base for every packed wire struct: pack()/unpack() driven by the subclass's static
 * SIZE + _FIELDS table. Each field is { name, offset, width, array, le }; array fields
 * round-trip raw Uint8Array bytes (zero-padded on pack), scalars round-trip integers
 * with per-field endianness.
 */
class _PackedStruct {
  /** Serialize to exactly SIZE wire bytes (arrays zero-padded; over-long arrays throw). */
  pack() {
    const ctor = this.constructor;
    const out = new Uint8Array(ctor.SIZE);
    const view = new DataView(out.buffer);
    for (const f of ctor._FIELDS) {
      const value = this[f.name];
      if (f.array) {
        const raw = value instanceof Uint8Array ? value : Uint8Array.from(value || []);
        if (raw.length > f.width) {
          throw new RangeError(`${ctor.name}.${f.name}: ${raw.length} bytes > ${f.width}`);
        }
        out.set(raw, f.offset); // remaining bytes stay zero (out is pre-zeroed)
      } else {
        _writeScalar(view, f.offset, f.width, Number(value) || 0, f.le);
      }
    }
    return out;
  }

  /** Deserialize SIZE wire bytes into an instance (extra trailing bytes ignored). */
  static unpack(data) {
    const bytes = data instanceof Uint8Array ? data : new Uint8Array(data);
    if (bytes.length < this.SIZE) {
      throw new RangeError(`${this.name}: need ${this.SIZE} bytes, got ${bytes.length}`);
    }
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const obj = new this();
    for (const f of this._FIELDS) {
      obj[f.name] = f.array
        ? bytes.slice(f.offset, f.offset + f.width)
        : _readScalar(view, f.offset, f.width, f.le);
    }
    return obj;
  }
}
'''


def _render_enum_js(out: List[str], e: ValueEnum) -> None:
    doc = e.doc or f"{e.name} enum."
    if e.width:
        doc = f"{doc}  (on-wire width: {e.width} byte{'s' if e.width != 1 else ''})"
    out.extend(_jsdoc(doc))
    if e.external:
        out.extend(_line_comment(f"@external {e.external}"))
    out.append(f"export const {e.name} = Object.freeze({{")
    for m in e.members:
        member_doc = m.doc or ""
        if m.changed:
            member_doc = f"{member_doc} (changed: {m.changed})".strip()
        out.extend(_jsdoc(member_doc, indent="  "))
        out.append(f"  {m.name}: {m.value},")
    out.append("});")
    out.append("")


def _render_bitfield_js(out: List[str], b: BitfieldGroup) -> None:
    bit_members = [m for m in b.members if m.kind == "bit"]
    companions = [m for m in b.members if m.kind in ("shift", "mask")]
    if not bit_members:
        # A shared-shape / all-reserved group with no concrete bit macros: document it.
        out.extend(_line_comment(f"Bitfield group {b.name}: {b.doc}"))
        out.append("")
        return
    out.extend(_line_comment(b.doc or f"{b.name} bit flags."))
    for m in bit_members:
        comment = f"  // {m.doc}" if m.doc else ""
        out.append(f"export const {m.name} = 1 << {m.value};{comment}")
    for m in companions:
        val = f"0x{m.value:02X}" if m.kind == "mask" else str(m.value)
        comment = f"  // {m.doc}" if m.doc else ""
        out.append(f"export const {m.name} = {val};{comment}")
    out.append("")


def _struct_kind_note(s: Struct) -> str:
    kind: List[str] = []
    if s.packet:
        kind.append(f"config packet {s.packet}")
    if s.message:
        kind.append(f"payload of {s.message}")
    if s.required:
        kind.append("required")
    if s.repeatable:
        kind.append(f"repeatable (max {s.repeatable_max})" if s.repeatable_max else "repeatable")
    return f"  [{'; '.join(kind)}]" if kind else ""


def _render_struct_js(out: List[str], s: Struct) -> None:
    header = s.doc or f"{s.name} packed wire struct."
    out.extend(_jsdoc(f"{header}{_struct_kind_note(s)}  ({s.size} wire bytes.)"))
    out.append(f"export class {s.name} extends _PackedStruct {{")
    for f in s.fields:
        doc = (f.doc or "").strip()
        out.extend(_jsdoc(f"{doc}{_field_annotations(f)}".strip(), indent="  "))
        if f.array_len is not None:
            out.append(f"  {f.name} = new Uint8Array();")
        else:
            out.append(f"  {f.name} = 0;")
    out.append("")
    out.append(f"  static SIZE = {s.size};")
    if s.packet and s.packet.lower().startswith("0x"):
        out.append(f"  static PACKET_ID = {s.packet};")
    if s.message:
        out.append(f'  static MESSAGE = "{s.message}";')
    out.append("  static _FIELDS = [")
    for f in s.fields:
        le = _ENDIAN_LE[f.endian]
        arr = "true" if f.array_len is not None else "false"
        out.append(
            f'    {{ name: "{f.name}", offset: {f.offset}, width: {f.width}, '
            f"array: {arr}, le: {le} }},"
        )
    out.append("  ];")
    out.append("")
    out.append("  constructor(fields) {")
    out.append("    super();")
    out.append("    if (fields) Object.assign(this, fields);")
    out.append("  }")
    out.append("}")
    out.append("")


def render_js(text: str, model: Model, header_name: str) -> str:
    out = _js_banner(text, model, header_name)

    _render_consts_js(out, model)

    out.append(_RUNTIME_JS)  # trailing "" comes from the block's final newline split below

    out.append("// ===== Value enums =====")
    out.append("")
    for e in model.enums:
        _render_enum_js(out, e)

    out.append("// ===== Bitfield groups =====")
    out.append("// Bit flags are flat `export const` (protocol.js style); reserved bits kept.")
    out.append("")
    for b in model.bitfields:
        _render_bitfield_js(out, b)

    if model.crossrefs:
        out.append("// ===== Cross-header bindings (defined in opendisplay_protocol.js) =====")
        for cr in model.crossrefs:
            members = ", ".join(f"{n}={v}" for n, v in cr.members) or "(unresolved)"
            out.extend(_line_comment(f"@{cr.kind} {cr.binding}_*: {members}"))
        out.append("")

    out.append("// ===== Packed structs =====")
    out.append("")
    for s in model.structs:
        _render_struct_js(out, s)

    # Collapse any accidental triple-blank into a clean single trailing newline.
    body = "\n".join(out)
    while "\n\n\n\n" in body:
        body = body.replace("\n\n\n\n", "\n\n\n")
    return body.rstrip("\n") + "\n"


# --------------------------------------------------------------------------------------
# .d.ts renderer
# --------------------------------------------------------------------------------------

def _dts_banner(text: str, model: Model, header_name: str) -> List[str]:
    sha = source_sha(text)
    return [
        "// @generated by tools/gen_js_structs.py -- DO NOT EDIT BY HAND.",
        f"// Source: {header_name} (OD_STRUCTS_VERSION {model.version})",
        f"// Source SHA-256: {sha}",
        f"// Regenerate: {REGEN_CMD}   (CI drift gate: --check)",
        "//",
        "// TypeScript declarations for opendisplay_structs.js. Value enums are frozen",
        "// name->value objects (exact literal member types); bitfield flags are literal-typed",
        "// consts; packed structs are classes with a DataView-backed pack()/unpack().",
        "",
    ]


def _render_enum_dts(out: List[str], e: ValueEnum) -> None:
    out.append(f"export declare const {e.name}: Readonly<{{")
    for m in e.members:
        out.append(f"  {m.name}: {m.value};")
    out.append("}>;")
    out.append("")


def _render_bitfield_dts(out: List[str], b: BitfieldGroup) -> None:
    for m in b.members:
        if m.kind == "bit":
            out.append(f"export declare const {m.name}: {1 << m.value};")
        elif m.kind == "mask":
            out.append(f"export declare const {m.name}: 0x{m.value:02X};")
        else:  # shift
            out.append(f"export declare const {m.name}: {m.value};")


def _render_struct_dts(out: List[str], s: Struct) -> None:
    out.append(f"export declare class {s.name} extends _PackedStruct {{")
    for f in s.fields:
        ts = "Uint8Array" if f.array_len is not None else "number"
        out.append(f"  {f.name}: {ts};")
    out.append(f"  static readonly SIZE: {s.size};")
    if s.packet and s.packet.lower().startswith("0x"):
        out.append(f"  static readonly PACKET_ID: {s.packet};")
    if s.message:
        out.append(f'  static readonly MESSAGE: "{s.message}";')
    out.append(f"  constructor(fields?: Partial<{s.name}>);")
    out.append("}")
    out.append("")


_RUNTIME_DTS = '''// ===== Packed-struct runtime =====

/** Input accepted by every struct's static unpack(). */
export type WireBytes = Uint8Array | ArrayLike<number> | ArrayBuffer;

/** Base for every packed wire struct: DataView-backed pack()/unpack(). */
export declare class _PackedStruct {
  /** Serialize to exactly SIZE wire bytes (arrays zero-padded; over-long arrays throw). */
  pack(): Uint8Array;
  /** Deserialize SIZE wire bytes into an instance (extra trailing bytes ignored). */
  static unpack<T extends _PackedStruct>(this: new (fields?: any) => T, data: WireBytes): T;
  static readonly SIZE: number;
}
'''


def render_dts(text: str, model: Model, header_name: str) -> str:
    out = _dts_banner(text, model, header_name)

    out.append("// ===== Loose wire constants =====")
    for c in model.consts:
        out.append(f"export declare const {c.name}: {c.value};")
    out.append("")

    out.append(_RUNTIME_DTS)

    out.append("// ===== Value enums =====")
    out.append("")
    for e in model.enums:
        _render_enum_dts(out, e)

    out.append("// ===== Bitfield groups =====")
    for b in model.bitfields:
        _render_bitfield_dts(out, b)
    out.append("")

    if model.crossrefs:
        out.append("// ===== Cross-header bindings (defined in opendisplay_protocol.js) =====")
        for cr in model.crossrefs:
            members = ", ".join(f"{n}={v}" for n, v in cr.members) or "(unresolved)"
            out.append(f"// @{cr.kind} {cr.binding}_*: {members}")
        out.append("")

    out.append("// ===== Packed structs =====")
    out.append("")
    for s in model.structs:
        _render_struct_dts(out, s)

    body = "\n".join(out)
    while "\n\n\n\n" in body:
        body = body.replace("\n\n\n\n", "\n\n\n")
    return body.rstrip("\n") + "\n"


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="gen_js_structs.py",
        description="Generate the JavaScript payload-structs module (.js + .d.ts) from opendisplay_structs.h.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="(re)generate and write the .js and .d.ts")
    mode.add_argument("--check", action="store_true", help="verify both files match (exit 1 on drift)")
    mode.add_argument("--stdout", action="store_true", help="print the .js module; write nothing")
    p.add_argument("--header", metavar="FILE", help=f"canonical header path (default: {DEFAULT_HEADER})")
    p.add_argument("--protocol", metavar="FILE", help=f"protocol header for cross-refs (default: {DEFAULT_PROTOCOL})")
    p.add_argument("--out-js", metavar="FILE", help=f"generated .js path (default: {DEFAULT_OUT_JS})")
    p.add_argument("--out-dts", metavar="FILE", help=f"generated .d.ts path (default: {DEFAULT_OUT_DTS})")
    args = p.parse_args(argv)

    header_path = Path(args.header) if args.header else DEFAULT_HEADER
    protocol_path = Path(args.protocol) if args.protocol else DEFAULT_PROTOCOL
    js_path = Path(args.out_js) if args.out_js else DEFAULT_OUT_JS
    dts_path = Path(args.out_dts) if args.out_dts else DEFAULT_OUT_DTS
    if not header_path.is_file():
        die(f"canonical header not found: {header_path}")

    text = header_path.read_text(encoding="utf-8")
    model = parse_structs(text, protocol_consts_from(protocol_path))
    if not model.structs:
        die(f"no structs parsed from {header_path}")
    js = render_js(text, model, header_path.name)
    dts = render_dts(text, model, header_path.name)

    if args.stdout:
        sys.stdout.write(js)
        return 0

    failed = False
    for path, content in ((js_path, js), (dts_path, dts)):
        ok, message = reconcile(path, content, write=args.write, regen_cmd=REGEN_CMD)
        if args.write:
            print(message)
        else:
            print(message, file=sys.stdout if ok else sys.stderr)
        failed = failed or not ok
    if args.write:
        nbits = sum(1 for b in model.bitfields if any(m.kind == "bit" for m in b.members))
        print(f"({len(model.structs)} structs, {len(model.enums)} enums, {nbits} bitfields -> .js + .d.ts)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
