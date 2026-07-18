#!/usr/bin/env python3
"""Generate the Python payload-structs module from opendisplay_structs.h.

The C header `src/opendisplay_structs.h` is the single source of truth for the LAYOUT
of every config-TLV and message payload on the OpenDisplay BLE wire. Firmware repos
vendor it byte-for-byte; Python cannot `#include` a C header, so this tool GENERATES
the equivalent Python module and CI verifies it has not drifted -- the cross-language
analog of the firmware `--check`, and the sibling of gen_python_protocol.py. Parsing is
done by the shared `structs_model` (also the future backend for a Swift/JS emitter);
this file is only the renderer.

CANONICAL SOURCE
    opendisplay-protocol/src/opendisplay_structs.h   (real enums + packed structs)

GENERATED ARTIFACT
    opendisplay-protocol/src/opendisplay_structs.py  (IntEnum / IntFlag + packed dataclasses)

WHAT IS EMITTED
    * loose wire consts               -> module-level `Final` constants
    * value enums (ICType, ...)       -> `IntEnum` (every member carries its @doc)
    * bitfield groups (@bits)         -> `IntFlag` (+ shift/mask companion consts);
                                         reserved placeholder bits are kept
    * packed structs                  -> `@dataclass` + a per-field `_Spec` table driving
                                         generic pack()/unpack() that round-trips wire
                                         bytes and HONORS per-field endianness (default
                                         little-endian; WifiConfig.server_port and all of
                                         PartialWriteStartHeader are big-endian)

    Cross-header names (OD_NFC_IC_*, PIPE_FLAG_*) owned by opendisplay_protocol.h are
    NEVER redefined -- the field docs reference them; import them from
    opendisplay_protocol.py where needed.

USAGE
    tools/gen_python_structs.py --write         # (re)generate src/opendisplay_structs.py
    tools/gen_python_structs.py --check          # verify it matches (exit 1 on drift)
    tools/gen_python_structs.py --stdout         # print generated module, write nothing
    tools/gen_python_structs.py --header FILE --out FILE   # explicit paths

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
DEFAULT_OUT = REPO_ROOT / "src" / "opendisplay_structs.py"
REGEN_CMD = "tools/gen_python_structs.py --write"

_ENDIAN = {"le": "little", "be": "big"}


def _wrap_comment(text: str, indent: str = "") -> List[str]:
    """Render prose as one or more `# ...` comment lines, wrapped to ~92 cols."""
    if not text:
        return []
    lines = textwrap.wrap(text, width=92 - len(indent) - 2) or [""]
    return [f"{indent}# {ln}" for ln in lines]


def _field_annotations(f) -> str:
    """Compact tag summary shown alongside a struct field's doc comment."""
    bits = []
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


def _render_consts(out: List[str], model: Model) -> None:
    out.append("# ===== Loose wire constants =====")
    out.append("")
    for c in model.consts:
        out.extend(_wrap_comment(c.doc))
        out.append(f"{c.name}: Final = {c.value}")
    out.append("")


def _render_runtime(out: List[str]) -> None:
    out.append("# ===== Packed-struct runtime (generic wire (de)serialization) =====")
    out.append("")
    out.append("class _Spec(NamedTuple):")
    out.append('    """One field\'s wire geometry, driving generic pack/unpack."""')
    out.append("")
    out.append("    name: str")
    out.append("    width: int      # total bytes on the wire")
    out.append("    array: bool     # True => raw `bytes`; False => int scalar")
    out.append('    endian: str     # "little" | "big" (only meaningful for int scalars)')
    out.append("")
    out.append("")
    out.append("class _PackedStruct:")
    out.append('    """Mixin: pack()/unpack() driven by the subclass\'s SIZE + _FIELDS table."""')
    out.append("")
    out.append("    SIZE: ClassVar[int]")
    out.append("    _FIELDS: ClassVar[Tuple[_Spec, ...]]")
    out.append("")
    out.append("    def pack(self) -> bytes:")
    out.append('        """Serialize to exactly SIZE wire bytes (arrays zero-padded/truncated)."""')
    out.append("        out = bytearray()")
    out.append("        for spec in self._FIELDS:")
    out.append("            value = getattr(self, spec.name)")
    out.append("            if spec.array:")
    out.append("                raw = bytes(value)")
    out.append("                if len(raw) > spec.width:")
    out.append("                    raise ValueError(")
    out.append('                        f"{type(self).__name__}.{spec.name}: {len(raw)} bytes > {spec.width}")')
    out.append('                out += raw.ljust(spec.width, b"\\x00")')
    out.append("            else:")
    out.append("                out += int(value).to_bytes(spec.width, spec.endian)")
    out.append("        if len(out) != self.SIZE:")
    out.append('            raise ValueError(f"{type(self).__name__}: packed {len(out)} bytes, expected {self.SIZE}")')
    out.append("        return bytes(out)")
    out.append("")
    out.append("    @classmethod")
    out.append('    def unpack(cls, data: bytes) -> "_PackedStruct":')
    out.append('        """Deserialize SIZE wire bytes into an instance (extra trailing bytes ignored)."""')
    out.append("        if len(data) < cls.SIZE:")
    out.append('            raise ValueError(f"{cls.__name__}: need {cls.SIZE} bytes, got {len(data)}")')
    out.append("        kwargs = {}")
    out.append("        offset = 0")
    out.append("        for spec in cls._FIELDS:")
    out.append("            chunk = data[offset:offset + spec.width]")
    out.append("            offset += spec.width")
    out.append("            kwargs[spec.name] = bytes(chunk) if spec.array else int.from_bytes(chunk, spec.endian)")
    out.append("        return cls(**kwargs)")
    out.append("")
    out.append("")


def _render_enum(out: List[str], e: ValueEnum) -> None:
    out.append(f"class {e.name}(IntEnum):")
    doc = e.doc or f"{e.name} enum."
    if e.width:
        doc = f"{doc}  (on-wire width: {e.width} byte{'s' if e.width != 1 else ''})"
    wrapped = textwrap.wrap(doc, width=88) or [doc]
    if len(wrapped) == 1:
        out.append(f'    """{wrapped[0]}"""')
    else:
        out.append(f'    """{wrapped[0]}')
        out.extend(f"    {ln}" for ln in wrapped[1:])
        out.append('    """')
    if e.external:
        out.append(f"    # @external {e.external}")
    out.append("")
    for m in e.members:
        comment = f"  # {m.doc}" if m.doc else ""
        out.append(f"    {m.name} = {m.value}{comment}")
        if m.changed:
            out.extend(_wrap_comment(f"changed: {m.changed}", indent="    "))
    out.append("")
    out.append("")


def _render_bitfield(out: List[str], b: BitfieldGroup, all_names: List[str]) -> None:
    bit_members = [m for m in b.members if m.kind == "bit"]
    companions = [m for m in b.members if m.kind in ("shift", "mask")]
    if not bit_members:
        # A shared-shape / all-reserved group with no concrete bit macros: document it.
        out.extend(_wrap_comment(f"Bitfield group {b.name}: {b.doc}"))
        out.append("")
        return
    all_names.append(b.name)
    out.append(f"class {b.name}(IntFlag):")
    wrapped = textwrap.wrap(b.doc or f"{b.name} bit flags.", width=88) or [b.name]
    if len(wrapped) == 1:
        out.append(f'    """{wrapped[0]}"""')
    else:
        out.append(f'    """{wrapped[0]}')
        out.extend(f"    {ln}" for ln in wrapped[1:])
        out.append('    """')
    out.append("")
    for m in bit_members:
        comment = f"  # {m.doc}" if m.doc else ""
        out.append(f"    {m.name} = 1 << {m.value}{comment}")
        all_names.append(m.name)
    out.append("")
    for m in companions:
        val = f"0x{m.value:02X}" if m.kind == "mask" else str(m.value)
        comment = f"  # {m.doc}" if m.doc else ""
        out.append(f"{m.name}: Final = {val}{comment}")
        all_names.append(m.name)
    if companions:
        out.append("")
    out.append("")


def _render_struct(out: List[str], s: Struct, all_names: List[str]) -> None:
    out.append("@dataclass")
    out.append(f"class {s.name}(_PackedStruct):")
    # class docstring
    header = s.doc or f"{s.name} packed wire struct."
    kind = []
    if s.packet:
        kind.append(f"config packet {s.packet}")
    if s.message:
        kind.append(f"payload of {s.message}")
    if s.required:
        kind.append("required")
    if s.repeatable:
        kind.append(f"repeatable (max {s.repeatable_max})" if s.repeatable_max else "repeatable")
    tail = f"  [{'; '.join(kind)}]" if kind else ""
    full = f"{header}{tail}  ({s.size} wire bytes.)"
    wrapped = textwrap.wrap(full, width=88) or [full]
    out.append(f'    """{wrapped[0]}')
    out.extend(f"    {ln}" for ln in wrapped[1:])
    out.append('    """')
    out.append("")
    # fields
    for f in s.fields:
        doc = (f.doc or "").strip()
        out.extend(_wrap_comment(f"{doc}{_field_annotations(f)}".strip(), indent="    "))
        if f.array_len is not None:
            out.append(f"    {f.name}: bytes = b\"\"")
        else:
            out.append(f"    {f.name}: int = 0")
    out.append("")
    # class metadata
    out.append(f"    SIZE: ClassVar[int] = {s.size}")
    if s.packet and s.packet.lower().startswith("0x"):
        out.append(f"    PACKET_ID: ClassVar[int] = {s.packet}")
    if s.message:
        out.append(f'    MESSAGE: ClassVar[str] = "{s.message}"')
    out.append("    _FIELDS: ClassVar[Tuple[_Spec, ...]] = (")
    for f in s.fields:
        endian = _ENDIAN[f.endian]
        out.append(f'        _Spec("{f.name}", {f.width}, {f.array_len is not None}, "{endian}"),')
    out.append("    )")
    out.append("")
    out.append("")
    all_names.append(s.name)


def render_module(text: str, model: Model, header_name: str) -> str:
    sha = source_sha(text)
    out: List[str] = []
    out.append("# @generated by tools/gen_python_structs.py -- DO NOT EDIT BY HAND.")
    out.append(f"# Source: {header_name} (OD_STRUCTS_VERSION {model.version})")
    out.append(f"# Source SHA-256: {sha}")
    out.append(f"# Regenerate: {REGEN_CMD}   (CI drift gate: --check)")
    out.append('"""OpenDisplay BLE payload structs, enums, and bitfields.')
    out.append("")
    out.append(f"Generated from the canonical {header_name} (OD_STRUCTS_VERSION {model.version}).")
    out.append("Every layout here describes bytes that travel on the OpenDisplay BLE wire. Do not")
    out.append("hand-edit; edit the header and regenerate. Byte order is LITTLE-ENDIAN by default;")
    out.append("the big-endian exceptions (WifiConfig.server_port and all of")
    out.append("PartialWriteStartHeader) are honored per-field by each struct's pack()/unpack().")
    out.append("Cross-header names (OD_NFC_IC_*, PIPE_FLAG_*) live in opendisplay_protocol.py and")
    out.append('are referenced, never redefined here."""')
    out.append("")
    out.append("from dataclasses import dataclass")
    out.append("from enum import IntEnum, IntFlag")
    out.append("from typing import ClassVar, Final, NamedTuple, Tuple")
    out.append("")

    all_names: List[str] = [c.name for c in model.consts]
    _render_consts(out, model)
    _render_runtime(out)

    out.append("# ===== Value enums =====")
    out.append("")
    for e in model.enums:
        _render_enum(out, e)
        all_names.append(e.name)

    out.append("# ===== Bitfield groups =====")
    out.append("")
    for b in model.bitfields:
        _render_bitfield(out, b, all_names)

    if model.crossrefs:
        out.append("# ===== Cross-header bindings (defined in opendisplay_protocol.py) =====")
        for cr in model.crossrefs:
            members = ", ".join(f"{n}={v}" for n, v in cr.members) or "(unresolved)"
            out.extend(_wrap_comment(f"@{cr.kind} {cr.binding}_*: {members}"))
        out.append("")

    out.append("# ===== Packed structs =====")
    out.append("")
    for s in model.structs:
        _render_struct(out, s, all_names)

    out.append("__all__ = [")
    for name in all_names:
        out.append(f'    "{name}",')
    out.append("]")
    return "\n".join(out) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="gen_python_structs.py",
        description="Generate the Python payload-structs module from opendisplay_structs.h.",
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
