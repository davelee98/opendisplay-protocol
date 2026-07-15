#!/usr/bin/env python3
"""Vendor the canonical opendisplay_protocol.h into the firmware repos.

opendisplay_protocol.h is authored ONCE, here, and copied byte-for-byte into
every firmware repo. This tool propagates the canonical file (--push) and
verifies the vendored copies have not drifted (--check). Keeping every copy
byte-identical means --check is a trivial hash compare and cross-repo diffs of
the header stay empty.

CANONICAL SOURCE (this repo)
    opendisplay-protocol/src/opendisplay_protocol.h

VENDORED COPIES (byte-identical)
    Firmware/include/opendisplay_protocol.h
    Firmware_NRF54/src/opendisplay_protocol.h
    Firmware_Silabs/opendisplay_protocol.h
    Firmware_NRF/opendisplay_protocol.h

WHEN TO RUN
    * After editing the canonical header: run --push, then commit each firmware
      repo (branch + PR per repo).
    * In each firmware repo's CI / pre-commit: run --check so a stale or
      hand-edited vendored copy fails the build.

USAGE -- maintainer (firmware repos are siblings of this repo on disk)
    tools/sync_protocol_header.py --push                 # canonical -> all copies
    tools/sync_protocol_header.py --check                # verify all copies
    tools/sync_protocol_header.py --check --only Firmware_Silabs
    tools/sync_protocol_header.py --push  --base /path/to/OD
    tools/sync_protocol_header.py --list                 # show the copy map

USAGE -- firmware-repo CI (only that repo checked out; fetch canonical from GitHub)
    tools/sync_protocol_header.py --check \
        --canonical-url https://raw.githubusercontent.com/davelee98/opendisplay-protocol/main/src/opendisplay_protocol.h \
        --dest include/opendisplay_protocol.h

EXIT CODES
    0  success / all copies in sync
    1  drift, missing copy, or error

Stdlib only; no third-party dependencies. Python 3.8+.
"""

from __future__ import annotations

import argparse
import difflib
import sys
import urllib.request
from pathlib import Path

# --- layout -----------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent          # opendisplay-protocol/
CANONICAL_REL = Path("src") / "opendisplay_protocol.h"       # within this repo
DEFAULT_BASE = REPO_ROOT.parent                              # siblings live here (…/OD)

CANONICAL_RAW_URL = (
    "https://raw.githubusercontent.com/davelee98/"
    "opendisplay-protocol/main/src/opendisplay_protocol.h"
)

# repo key -> vendored copy path, relative to the sibling base dir
REPOS = {
    "Firmware":        Path("Firmware")        / "include" / "opendisplay_protocol.h",
    "Firmware_NRF54":  Path("Firmware_NRF54")  / "src"     / "opendisplay_protocol.h",
    "Firmware_Silabs": Path("Firmware_Silabs")            / "opendisplay_protocol.h",
    "Firmware_NRF":    Path("Firmware_NRF")               / "opendisplay_protocol.h",
}


# --- helpers ----------------------------------------------------------------

def read_canonical(args) -> bytes:
    """Return the canonical header bytes from a URL, an explicit path, or the default."""
    if args.canonical_url:
        with urllib.request.urlopen(args.canonical_url, timeout=30) as resp:  # noqa: S310
            return resp.read()
    path = Path(args.canonical) if args.canonical else (REPO_ROOT / CANONICAL_REL)
    if not path.is_file():
        die(f"canonical header not found: {path}")
    return path.read_bytes()


def resolve_targets(args) -> list[tuple[str, Path]]:
    """Return the list of (label, destination_path) to operate on."""
    if args.dest:
        return [("(--dest)", Path(args.dest))]
    base = Path(args.base) if args.base else DEFAULT_BASE
    keys = args.only.split(",") if args.only else list(REPOS)
    out: list[tuple[str, Path]] = []
    for key in keys:
        key = key.strip()
        if key not in REPOS:
            die(f"unknown repo '{key}'. Known: {', '.join(REPOS)}")
        out.append((key, base / REPOS[key]))
    return out


def unified_diff(canonical: bytes, current: bytes, label: str) -> str:
    a = current.decode("utf-8", "replace").splitlines(keepends=True)
    b = canonical.decode("utf-8", "replace").splitlines(keepends=True)
    return "".join(difflib.unified_diff(a, b, fromfile=f"{label} (vendored)",
                                        tofile="canonical", n=2))


def die(msg: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


# --- commands ---------------------------------------------------------------

def cmd_list() -> int:
    print(f"canonical : {REPO_ROOT / CANONICAL_REL}")
    print(f"base      : {DEFAULT_BASE}")
    print("copies    :")
    for key, rel in REPOS.items():
        print(f"  {key:<16} {DEFAULT_BASE / rel}")
    return 0


def cmd_push(args) -> int:
    canonical = read_canonical(args)
    changed = same = 0
    for label, dest in resolve_targets(args):
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.is_file() and dest.read_bytes() == canonical:
            print(f"  up-to-date  {label}  {dest}")
            same += 1
            continue
        dest.write_bytes(canonical)
        print(f"  wrote       {label}  {dest}")
        changed += 1
    print(f"push: {changed} updated, {same} already current, "
          f"{len(canonical)} bytes each.")
    return 0


def cmd_check(args) -> int:
    canonical = read_canonical(args)
    ok = drift = missing = 0
    diffs: list[str] = []
    for label, dest in resolve_targets(args):
        if not dest.is_file():
            print(f"  MISSING     {label}  {dest}")
            missing += 1
            continue
        current = dest.read_bytes()
        if current == canonical:
            print(f"  ok          {label}  {dest}")
            ok += 1
        else:
            print(f"  DRIFT       {label}  {dest}")
            drift += 1
            diffs.append(unified_diff(canonical, current, str(dest)))
    if diffs:
        print("\n--- drift detail (vendored vs canonical) ---")
        for d in diffs:
            print(d if d else "(binary or whitespace-only difference)")
    failed = drift + missing
    print(f"\ncheck: {ok} in sync, {drift} drifted, {missing} missing.")
    if failed:
        print("Run `sync_protocol_header.py --push` and commit the updated copies.",
              file=sys.stderr)
    return 1 if failed else 0


# --- entrypoint -------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="sync_protocol_header.py",
        description="Vendor / verify the canonical opendisplay_protocol.h.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--push", action="store_true",
                      help="copy the canonical header into every vendored copy")
    mode.add_argument("--check", action="store_true",
                      help="verify every vendored copy matches (exit 1 on drift)")
    mode.add_argument("--list", action="store_true",
                      help="print the canonical + copy paths and exit")

    p.add_argument("--only", metavar="NAME[,NAME...]",
                   help=f"restrict to specific repos ({', '.join(REPOS)})")
    p.add_argument("--base", metavar="DIR",
                   help="directory the firmware repos live under "
                        f"(default: {DEFAULT_BASE})")
    p.add_argument("--dest", metavar="FILE",
                   help="operate on ONE explicit vendored file (overrides --only/--base; "
                        "for a lone firmware-repo checkout)")
    p.add_argument("--canonical", metavar="FILE",
                   help="explicit canonical header path (default: this repo's src copy)")
    p.add_argument("--canonical-url", metavar="URL",
                   help="fetch the canonical header from a URL instead of a local file "
                        f"(e.g. {CANONICAL_RAW_URL})")

    args = p.parse_args(argv)

    if args.dest and args.only:
        die("--dest and --only are mutually exclusive")

    if args.list:
        return cmd_list()
    if args.push:
        return cmd_push(args)
    return cmd_check(args)


if __name__ == "__main__":
    raise SystemExit(main())
