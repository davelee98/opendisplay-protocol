#!/usr/bin/env python3
"""Generate the Python protocol constants module from opendisplay_protocol.h.

The C header `src/opendisplay_protocol.h` is the single source of truth for every
value on the OpenDisplay BLE wire. Firmware repos vendor it byte-for-byte
(`sync_protocol_header.py`); Python cannot `#include` a C header, so this tool
GENERATES the equivalent Python module and CI verifies it has not drifted -- the
cross-language analog of the firmware `--check`.

CANONICAL SOURCE
    opendisplay-protocol/src/opendisplay_protocol.h   (macro-only, `u`-suffixed)

GENERATED ARTIFACT
    opendisplay-protocol/src/opendisplay_protocol.py  (flat `Final` constants)

WHY FLAT CONSTANTS, NOT ONE IntEnum
    The header deliberately reuses byte values across distinct names (0x73 is both
    RESP_LED_ACTIVATE_ACK and RESP_DIRECT_WRITE_REFRESH_SUCCESS; 0xFF is both
    RESP_NACK and RESP_DIRECT_WRITE_ERROR). In an IntEnum the second member with a
    duplicate value becomes a silent ALIAS of the first, erasing a name. Module-level
    `Final` constants preserve every name and value faithfully.

WHAT IT PARSES
    Only `#define NAME VALUE [/* comment */]` lines where VALUE is one of:
      * an integer literal   (0x000Fu / 0xFFu / 2u ...; the `u`/`l` suffix is stripped)
      * a string literal     ("2.0")
      * a reference to an already-defined macro name  (AUTH_STATUS_CHALLENGE)
    The include-guard `#define` (no value) and every comment/`#ifndef`/`#endif`
    line are ignored. Any other value shape (an expression, an unknown symbol) is
    a HARD ERROR -- the header promises simple macros, so the generator refuses to
    silently drop something it does not understand.

WHEN TO RUN
    * After editing the canonical header:  gen_python_protocol.py --write
    * In CI / pre-commit:                  gen_python_protocol.py --check  (exit 1 on drift)

USAGE
    tools/gen_python_protocol.py --write        # (re)generate src/opendisplay_protocol.py
    tools/gen_python_protocol.py --check         # verify it matches (exit 1 on drift)
    tools/gen_python_protocol.py --stdout        # print generated module, write nothing
    tools/gen_python_protocol.py --header FILE --out FILE   # explicit paths

EXIT CODES
    0  success / in sync
    1  drift, missing file, or a value the parser cannot classify

Stdlib only; no third-party dependencies. Python 3.8+.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import re
import sys
from pathlib import Path
from typing import List, NamedTuple, NoReturn, Optional, Set, Tuple

# --- layout -----------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HEADER = REPO_ROOT / "src" / "opendisplay_protocol.h"
DEFAULT_OUT = REPO_ROOT / "src" / "opendisplay_protocol.py"

# --- parsing ----------------------------------------------------------------

# A SECTION heading, as it appears once framing is stripped from a block comment,
# e.g. "SECTION 1 -- COMMAND OPCODES (16-bit, big-endian on the wire)".
_SECTION_RE = re.compile(r"^SECTION\s+(\d+)\s*--\s*(.+?)\s*$")

# The include guard opens the region we care about; everything before it (the big
# front-matter banner) is header-maintenance prose, not per-constant docs.
_GUARD_RE = re.compile(r"^#\s*(?:ifndef|define)\s+OPENDISPLAY_PROTOCOL_H\b")

# "#define NAME <rest>"  (rest may hold a value and/or a trailing /* comment */,
# or be empty for the include guard).
_DEFINE_RE = re.compile(r"^#define\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\b(?P<rest>.*)$")

# Integer literal with an optional C integer suffix (u/U/l/L, any order).
_INT_RE = re.compile(r"^(?P<core>0[xX][0-9A-Fa-f]+|\d+)[uUlL]*$")

# A bare identifier (candidate symbol reference).
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class Const(NamedTuple):
    """One emitted constant, with its preceding doc block and trailing comment."""

    name: str
    value: str  # already a valid Python expression (int literal, "str", or a name)
    inline: Optional[str]  # the trailing /* ... */ on the #define line
    doc: List[str]  # cleaned block-comment lines that preceded the #define


class Group(NamedTuple):
    """A section heading, its intro prose, and the constants beneath it."""

    label: str
    intro: List[str]  # cleaned body of the SECTION block comment
    consts: List[Const]
    trailing: List[str]  # a doc block with no following #define (e.g. a closing note)


def _clean_comment_line(raw: str) -> str:
    """Strip C block-comment framing (`/*`, `*/`, leading `*`) from one line."""
    s = raw.strip()
    if s.startswith("/*"):
        s = s[2:].strip()
    if s.endswith("*/"):
        s = s[:-2].strip()
    if s.startswith("*"):
        s = s[1:]
        if s.startswith(" "):
            s = s[1:]
    return s.rstrip()


def _is_ruler(line: str) -> bool:
    """True for a horizontal-rule line (only dashes / equals)."""
    return bool(line) and set(line) <= {"-", "="}


def _clean_block(raw_lines: List[str]) -> List[str]:
    """Clean a full block comment into content lines (rulers dropped, edges trimmed)."""
    out: List[str] = []
    for raw in raw_lines:
        line = _clean_comment_line(raw)
        if _is_ruler(line):
            continue
        # collapse runs of blank lines to a single blank
        if not line and (not out or not out[-1]):
            continue
        out.append(line)
    while out and not out[-1]:
        out.pop()
    return out


def _split_value_comment(rest: str) -> Tuple[str, Optional[str]]:
    """Split the text after `#define NAME` into (value, comment)."""
    comment: Optional[str] = None
    idx = rest.find("/*")
    if idx != -1:
        end = rest.find("*/", idx + 2)
        comment = rest[idx + 2 : end if end != -1 else len(rest)].strip()
        rest = rest[:idx]
    return rest.strip(), (comment or None)


def _to_python_value(raw: str, name: str, known: Set[str], lineno: int) -> str:
    """Return a Python expression for a C macro value, or die if unclassifiable."""
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        return raw  # string literal is already valid Python
    m = _INT_RE.match(raw)
    if m:
        return m.group("core")  # keep the source's hex-vs-decimal form, drop the suffix
    if _IDENT_RE.match(raw):
        if raw not in known:
            _die(f"{name} (line {lineno}) references undefined macro '{raw}'")
        return raw  # reference to an earlier-defined constant (e.g. AUTH_STATUS_SUCCESS)
    _die(f"cannot classify value for {name} (line {lineno}): {raw!r}")


def parse_header(text: str) -> List[Group]:
    """Parse the canonical header into ordered groups of documented constants.

    Retains each `#define`'s preceding block comment (the @opcode/@request/...
    spec blocks) and its trailing inline comment. A block comment that carries a
    SECTION heading starts a new group; its remaining prose becomes the intro.
    The front-matter banner before the include guard is skipped.
    """
    lines = text.splitlines()
    groups: List[Group] = [Group("Protocol version", [], [], [])]
    known: Set[str] = set()
    pending_doc: List[str] = []  # doc block awaiting the next #define
    started = False
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not started:
            if _GUARD_RE.match(stripped):
                started = True
            i += 1
            continue

        if stripped.startswith("/*"):  # gather the whole block comment
            block: List[str] = []
            while i < len(lines):
                block.append(lines[i])
                if "*/" in lines[i]:
                    break
                i += 1
            body = _clean_block(block)
            section = next((_SECTION_RE.match(bl) for bl in body if _SECTION_RE.match(bl)), None)
            if section:
                if pending_doc:  # a doc block that no #define consumed -> closing note
                    groups[-1].trailing.extend(pending_doc)
                    pending_doc = []
                intro = [bl for bl in body if not _SECTION_RE.match(bl)]
                groups.append(Group(f"Section {section.group(1)}: {section.group(2)}", _clean_edges(intro), [], []))
            else:
                pending_doc = body
            i += 1
            continue

        define = _DEFINE_RE.match(stripped)
        if define:
            value_raw, inline = _split_value_comment(define.group("rest"))
            if value_raw:  # skip the value-less include-guard #define
                name = define.group("name")
                py_value = _to_python_value(value_raw, name, known, i + 1)
                known.add(name)
                groups[-1].consts.append(Const(name, py_value, inline, pending_doc))
            pending_doc = []
        i += 1

    if pending_doc:  # a trailing doc block at end of file
        groups[-1].trailing.extend(pending_doc)
    return [g for g in groups if g.consts or g.trailing]


def _clean_edges(body: List[str]) -> List[str]:
    """Trim leading/trailing blank lines from a doc body."""
    start, end = 0, len(body)
    while start < end and not body[start]:
        start += 1
    while end > start and not body[end - 1]:
        end -= 1
    return body[start:end]


# --- emission ---------------------------------------------------------------

def _protocol_version(groups: List[Group]) -> str:
    for group in groups:
        for const in group.consts:
            if const.name == "OD_PROTOCOL_VERSION_STR":
                return const.value.strip('"')
    return "unknown"


def _comment_lines(doc: List[str]) -> List[str]:
    """Render cleaned doc lines as Python comments (bare `#` for blank lines)."""
    return [("#" if line == "" else f"# {line}") for line in doc]


def render_module(text: str, groups: List[Group], header_name: str) -> str:
    """Render the generated Python module as a string (deterministic, no timestamp)."""
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    version = _protocol_version(groups)
    names = [c.name for g in groups for c in g.consts]

    out: List[str] = []
    out.append("# @generated by tools/gen_python_protocol.py -- DO NOT EDIT BY HAND.")
    out.append(f"# Source: {header_name} (OD_PROTOCOL_VERSION {version})")
    out.append(f"# Source SHA-256: {sha}")
    out.append("# Regenerate: tools/gen_python_protocol.py --write   (CI drift gate: --check)")
    out.append('"""OpenDisplay BLE wire-protocol constants.')
    out.append("")
    out.append(f"Generated from the canonical {header_name} (protocol v{version}). Every")
    out.append("value here travels on the OpenDisplay BLE wire. Do not hand-edit; edit the")
    out.append("header and regenerate. Constants are flat (not an IntEnum) because the wire")
    out.append("intentionally reuses byte values across distinct names -- an enum would alias")
    out.append("and hide one. NACK error codes are opcode-SCOPED: decode data[0] only in the")
    out.append('scope of the echoed opcode (see the header for the full contract)."""')
    out.append("")
    out.append("from typing import Final")
    out.append("")

    for group in groups:
        out.append(f"# ===== {group.label} =====")
        if group.intro:
            out.extend(_comment_lines(group.intro))
        out.append("")
        for idx, const in enumerate(group.consts):
            if const.doc and idx > 0:
                out.append("")  # breathing room between documented constants
            out.extend(_comment_lines(const.doc))
            line = f"{const.name}: Final = {const.value}"
            if const.inline:
                line = f"{line}  # {const.inline}"
            out.append(line)
        if group.trailing:
            out.append("")
            out.extend(_comment_lines(_clean_edges(group.trailing)))
        out.append("")

    out.append("__all__ = [")
    for name in names:
        out.append(f'    "{name}",')
    out.append("]")
    return "\n".join(out) + "\n"


# --- helpers ----------------------------------------------------------------

def _die(msg: str) -> NoReturn:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def _unified_diff(current: str, generated: str, label: str) -> str:
    return "".join(
        difflib.unified_diff(
            current.splitlines(keepends=True),
            generated.splitlines(keepends=True),
            fromfile=f"{label} (on disk)",
            tofile="freshly generated",
            n=2,
        )
    )


# --- entrypoint -------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="gen_python_protocol.py",
        description="Generate the Python protocol constants module from opendisplay_protocol.h.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="(re)generate and write the module")
    mode.add_argument("--check", action="store_true", help="verify the module matches (exit 1 on drift)")
    mode.add_argument("--stdout", action="store_true", help="print the generated module; write nothing")
    p.add_argument("--header", metavar="FILE", help=f"canonical header path (default: {DEFAULT_HEADER})")
    p.add_argument("--out", metavar="FILE", help=f"generated module path (default: {DEFAULT_OUT})")
    args = p.parse_args(argv)

    header_path = Path(args.header) if args.header else DEFAULT_HEADER
    out_path = Path(args.out) if args.out else DEFAULT_OUT
    if not header_path.is_file():
        _die(f"canonical header not found: {header_path}")

    text = header_path.read_text(encoding="utf-8")
    groups = parse_header(text)
    if not groups:
        _die(f"no constants parsed from {header_path}")
    generated = render_module(text, groups, header_path.name)

    if args.stdout:
        sys.stdout.write(generated)
        return 0

    if args.write:
        changed = not (out_path.is_file() and out_path.read_text(encoding="utf-8") == generated)
        out_path.write_text(generated, encoding="utf-8")
        count = sum(len(g.consts) for g in groups)
        print(f"{'wrote' if changed else 'up-to-date'}: {out_path} ({count} constants)")
        return 0

    # --check
    if not out_path.is_file():
        print(f"MISSING: {out_path}", file=sys.stderr)
        print("Run `gen_python_protocol.py --write` and commit the generated module.", file=sys.stderr)
        return 1
    current = out_path.read_text(encoding="utf-8")
    if current == generated:
        print(f"ok: {out_path} in sync with {header_path.name}")
        return 0
    print(f"DRIFT: {out_path} is stale vs {header_path.name}\n", file=sys.stderr)
    sys.stderr.write(_unified_diff(current, generated, str(out_path)))
    print("\nRun `gen_python_protocol.py --write` and commit the regenerated module.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
