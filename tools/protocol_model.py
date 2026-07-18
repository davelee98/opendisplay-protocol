#!/usr/bin/env python3
"""Parse opendisplay_protocol.h into a language-neutral constant model.

This is the ONE parser shared by every language generator (gen_python_protocol.py,
gen_js_protocol.py, ...). The C header `src/opendisplay_protocol.h` is the single
source of truth for the wire VALUES; this module is the single source of truth for
how they are READ, so a new language backend is a thin renderer and can never drift
from the others on what the header means.

It parses only `#define NAME VALUE [/* comment */]` lines, where VALUE is one of:
  * an integer literal   (0x000Fu / 0xFFu / 2u ...; the u/l suffix is stripped) -> kind "int"
  * a string literal     ("2.0")                                                -> kind "str"
  * a reference to an already-defined macro name (AUTH_STATUS_CHALLENGE)         -> kind "ref"
The classified `value` token is valid as-is in both Python and JavaScript (a hex or
decimal literal, a double-quoted string, or an identifier). The include-guard
`#define` (no value) and every comment / `#ifndef` / `#endif` line are ignored. Any
other value shape (an expression, an unknown symbol) is a HARD ERROR -- the header
promises simple macros, so the parser refuses to silently drop what it cannot model.

Doc comments are retained: each `#define`'s preceding block comment (the
@opcode/@request/... spec blocks) and its trailing inline comment, plus each
SECTION's intro prose and any standalone closing note. Comment TEXT is cleaned of C
framing here; each renderer adds its own line-comment prefix (`#`, `//`, ...).

Stdlib only; no third-party dependencies. Python 3.8+.
"""

from __future__ import annotations

import difflib
import hashlib
import re
import sys
from pathlib import Path
from typing import List, NamedTuple, NoReturn, Optional, Set, Tuple

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
    value: str  # a token valid in Python and JS: int literal, "string", or an identifier
    kind: str  # "int" | "str" | "ref" -- lets a renderer format the type (e.g. .d.ts)
    inline: Optional[str]  # the trailing /* ... */ on the #define line
    doc: List[str]  # cleaned block-comment lines that preceded the #define


class Group(NamedTuple):
    """A section heading, its intro prose, and the constants beneath it."""

    label: str
    intro: List[str]  # cleaned body of the SECTION block comment
    consts: List[Const]
    trailing: List[str]  # a doc block with no following #define (e.g. a closing note)


def die(msg: str) -> NoReturn:
    """Print an error and exit non-zero (shared failure path for all generators)."""
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def source_sha(text: str) -> str:
    """SHA-256 of the header bytes, embedded in generated banners for provenance."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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


def reconcile(path: Path, content: str, *, write: bool, regen_cmd: str) -> Tuple[bool, str]:
    """Apply one generated artifact to disk (write) or verify it (check).

    Returns (ok, message). In write mode `ok` is always True; in check mode `ok`
    is False on drift or a missing file, with a unified diff in the message. This
    is the shared drift-gate behavior for every language generator.
    """
    if write:
        changed = not (path.is_file() and path.read_text(encoding="utf-8") == content)
        path.write_text(content, encoding="utf-8")
        return True, f"{'wrote' if changed else 'up-to-date'}: {path}"
    if not path.is_file():
        return False, f"MISSING: {path}\nRun `{regen_cmd}` and commit the generated file."
    current = path.read_text(encoding="utf-8")
    if current == content:
        return True, f"ok: {path}"
    diff = _unified_diff(current, content, str(path))
    return False, f"DRIFT: {path} is stale\n\n{diff}\nRun `{regen_cmd}` and commit the regenerated file."


def protocol_version(groups: List[Group]) -> str:
    """Return the OD_PROTOCOL_VERSION string (e.g. "2.0"), or "unknown"."""
    for group in groups:
        for const in group.consts:
            if const.name == "OD_PROTOCOL_VERSION_STR":
                return const.value.strip('"')
    return "unknown"


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


def clean_edges(body: List[str]) -> List[str]:
    """Trim leading/trailing blank lines from a doc body."""
    start, end = 0, len(body)
    while start < end and not body[start]:
        start += 1
    while end > start and not body[end - 1]:
        end -= 1
    return body[start:end]


def _split_value_comment(rest: str) -> Tuple[str, Optional[str]]:
    """Split the text after `#define NAME` into (value, comment)."""
    comment: Optional[str] = None
    idx = rest.find("/*")
    if idx != -1:
        end = rest.find("*/", idx + 2)
        comment = rest[idx + 2 : end if end != -1 else len(rest)].strip()
        rest = rest[:idx]
    return rest.strip(), (comment or None)


def _classify_value(raw: str, name: str, known: Set[str], lineno: int) -> Tuple[str, str]:
    """Return (token, kind) for a C macro value, or die if unclassifiable.

    The token is emitted verbatim by every renderer; kind is "int" | "str" | "ref".
    """
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        return raw, "str"  # string literal is valid Python and JS as-is
    m = _INT_RE.match(raw)
    if m:
        return m.group("core"), "int"  # keep the source's hex-vs-decimal form, drop the suffix
    if _IDENT_RE.match(raw):
        if raw not in known:
            die(f"{name} (line {lineno}) references undefined macro '{raw}'")
        return raw, "ref"  # reference to an earlier-defined constant (e.g. AUTH_STATUS_SUCCESS)
    die(f"cannot classify value for {name} (line {lineno}): {raw!r}")


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
                groups.append(Group(f"Section {section.group(1)}: {section.group(2)}", clean_edges(intro), [], []))
            else:
                pending_doc = body
            i += 1
            continue

        define = _DEFINE_RE.match(stripped)
        if define:
            value_raw, inline = _split_value_comment(define.group("rest"))
            if value_raw:  # skip the value-less include-guard #define
                name = define.group("name")
                value, kind = _classify_value(value_raw, name, known, i + 1)
                known.add(name)
                groups[-1].consts.append(Const(name, value, kind, inline, pending_doc))
            pending_doc = []
        i += 1

    if pending_doc:  # a trailing doc block at end of file
        groups[-1].trailing.extend(pending_doc)
    return [g for g in groups if g.consts or g.trailing]
