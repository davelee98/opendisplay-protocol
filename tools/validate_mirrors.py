#!/usr/bin/env python3
"""Independently cross-validate the generated language mirrors against the headers.

The per-generator drift gates (`gen_*_{protocol,structs}.py --check`) only prove a
mirror is IDEMPOTENT -- that regenerating it reproduces the committed file. They do
NOT prove the generator's output is correct. This tool is the complementary check:
it parses the canonical C headers and each mirror INDEPENDENTLY (not via the shared
model modules) and asserts they agree, so a generator bug that consistently emits a
wrong value would be caught here even though `--check` stays green.

WHAT IT CHECKS
  protocol (opendisplay_protocol.{py,js,d.ts,swift} vs opendisplay_protocol.h)
    - every `#define` NAME in the header appears in each mirror (no missing/extra);
    - all four mirrors agree on every constant's VALUE (cross-language parity),
      resolving ref-aliases (incl. TypeScript `typeof X`).
  structs  (opendisplay_structs.{py,js,swift} vs opendisplay_structs.h)
    - every packed struct's declared wire size in each mirror equals its
      OD_STATIC_ASSERT(sizeof(struct X) == N) in the header (the layout oracle).

Runtime byte-level equivalence of the struct (de)serializers is validated separately
(see the cross-language validation notes); this tool is a fast, deterministic,
dependency-free gate suitable for CI alongside the `--check` gates.

USAGE
    tools/validate_mirrors.py                 # validate protocol + structs
    tools/validate_mirrors.py --protocol      # protocol mirrors only
    tools/validate_mirrors.py --structs       # structs mirrors only

EXIT CODES
    0  all mirrors consistent with the headers and each other
    1  a divergence was found (details printed)

Stdlib only; no third-party dependencies. Python 3.8+.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SRC = Path(__file__).resolve().parent.parent / "src"


# --- value normalization ----------------------------------------------------

def _norm(value: str, table: Dict[str, str]):
    """Resolve a mirror value token to an int / str / (unresolved) marker.

    Handles hex/decimal literals (with u/l suffixes), quoted strings, plain
    references to another constant, and TypeScript `typeof X` ref-aliases.
    """
    v = value.strip().rstrip(";").strip()
    if v.startswith("typeof "):
        v = v[len("typeof "):].strip()
    if re.fullmatch(r"0x[0-9a-fA-F]+[ul]*", v):
        return int(re.sub(r"[ul]+$", "", v), 16)
    if re.fullmatch(r"-?\d+[ul]*", v):
        return int(re.sub(r"[ul]+$", "", v))
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        return v[1:-1]
    if v in table:
        return _norm(table[v], table)
    return ("UNRESOLVED", v)


def _collect(text: str, pattern: str) -> Dict[str, object]:
    raw = {m.group(1): m.group(2).strip()
           for m in re.finditer(pattern, text, re.M)}
    return {k: _norm(v, raw) for k, v in raw.items()}


def _read(name: str) -> str:
    return (SRC / name).read_text()


# --- protocol ---------------------------------------------------------------

_PROTOCOL_MIRRORS = {
    "py":    (r"^(\w+)\s*:\s*Final\s*=\s*(.+?)\s*(?:#.*)?$",      "opendisplay_protocol.py"),
    "js":    (r"^export const (\w+)\s*=\s*(.+?);",                "opendisplay_protocol.js"),
    "d.ts":  (r"^export declare const (\w+)\s*:\s*(.+?);",        "opendisplay_protocol.d.ts"),
    "swift": (r"^public let (\w+)(?::\s*\w+)?\s*=\s*(.+?)\s*(?://.*)?$", "opendisplay_protocol.swift"),
}


def validate_protocol() -> bool:
    header = _read("opendisplay_protocol.h")
    names = {m.group(1) for m in re.finditer(r"^#define\s+(\w+)\b", header, re.M)}
    names.discard("OPENDISPLAY_PROTOCOL_H")  # the include guard has no value

    mirrors = {label: _collect(_read(fname), pat)
               for label, (pat, fname) in _PROTOCOL_MIRRORS.items()}

    ok = True
    print(f"[protocol] header #define constants: {len(names)}")
    for label, values in mirrors.items():
        missing = names - set(values)
        extra = set(values) - names
        good = not missing and not extra
        ok &= good
        detail = ""
        if missing:
            detail += f"  missing={sorted(missing)}"
        if extra:
            detail += f"  extra={sorted(extra)}"
        print(f"  names  {label:6} {len(values):>3}  {'OK' if good else 'DIFF'}{detail}")

    ref_label, ref = next(iter(mirrors.items()))
    for label, values in mirrors.items():
        if label == ref_label:
            continue
        mism = {k: (ref.get(k), values[k]) for k in values
                if str(ref.get(k)) != str(values[k])}
        ok &= not mism
        print(f"  values {ref_label} vs {label:6} {'OK' if not mism else 'MISMATCH ' + str(mism)}")
    return ok


# --- structs ----------------------------------------------------------------

def _sizes_by_class(text: str, class_re: str, size_re: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    cur: Optional[str] = None
    for line in text.splitlines():
        c = re.match(class_re, line)
        if c:
            cur = c.group(1)
        s = re.search(size_re, line)
        if s and cur and cur not in out:
            out[cur] = int(s.group(1))
    return out


def validate_structs() -> bool:
    header = _read("opendisplay_structs.h")
    asserts = {m.group(1): int(m.group(2)) for m in re.finditer(
        r"OD_STATIC_ASSERT\(sizeof\(struct (\w+)\)\s*==\s*(\d+)", header)}

    mirror_sizes = {
        "py":    _sizes_by_class(_read("opendisplay_structs.py"),
                                 r"class (\w+)", r"^\s+SIZE\s*:\s*ClassVar\[int\]\s*=\s*(\d+)"),
        "js":    _sizes_by_class(_read("opendisplay_structs.js"),
                                 r"export class (\w+)", r"\bSIZE\s*=\s*(\d+)"),
        "swift": _sizes_by_class(_read("opendisplay_structs.swift"),
                                 r"public struct (\w+)", r"wireSize\s*=\s*(\d+)"),
    }

    ok = True
    print(f"[structs] header packed structs (OD_STATIC_ASSERT): {len(asserts)}")
    for label, sizes in mirror_sizes.items():
        bad = [(k, asserts[k], sizes.get(k)) for k in asserts if sizes.get(k) != asserts[k]]
        good = not bad
        ok &= good
        print(f"  sizes  {label:6} {'OK ({}/{})'.format(len(asserts) - len(bad), len(asserts)) if good else 'MISMATCH ' + str(bad)}")
    return ok


# --- entrypoint -------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="validate_mirrors.py",
        description="Independently cross-validate the generated mirrors against the headers.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--protocol", action="store_true", help="validate protocol mirrors only")
    p.add_argument("--structs", action="store_true", help="validate structs mirrors only")
    args = p.parse_args(argv)

    do_protocol = args.protocol or not args.structs
    do_structs = args.structs or not args.protocol

    ok = True
    if do_protocol:
        ok &= validate_protocol()
    if do_structs:
        ok &= validate_structs()

    print("\nVERDICT:", "ALL MIRRORS CONSISTENT" if ok else "DIVERGENCE FOUND")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
