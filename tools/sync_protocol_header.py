#!/usr/bin/env python3
"""Vendor the canonical C headers into the firmware repos.

The canonical C wire headers are authored ONCE, here, and copied byte-for-byte
into every firmware repo. This tool propagates the canonical files (--push) and
verifies the vendored copies have not drifted (--check). Keeping every copy
byte-identical means --check is a trivial hash compare and cross-repo diffs stay
empty.

CANONICAL SOURCES (this repo) -- two artifacts:
    protocol : src/opendisplay_protocol.h   (opcodes / responses / errors)
    structs  : src/opendisplay_structs.h    (config + wire-payload structs/enums)

Only C headers are vendored into firmware. The generated language mirrors
(opendisplay_{protocol,structs}.{py,js,d.ts,swift}) are NOT handled here -- each
has its own drift gate (gen_python_protocol.py --check, gen_js_structs.py --check,
gen_swift_protocol.py --check, ...).

VENDORED COPIES (byte-identical)
    protocol:                                structs:
    Firmware/include/opendisplay_protocol.h  Firmware/include/opendisplay_structs.h
    Firmware_NRF54/src/opendisplay_protocol.h Firmware_NRF54/src/opendisplay_structs.h
    Firmware_Silabs/opendisplay_protocol.h   Firmware_Silabs/opendisplay_structs.h
    Firmware_NRF/opendisplay_protocol.h      Firmware_NRF/opendisplay_structs.h

    NOTE: `structs` copies land in firmware only after each repo ADOPTS the shared
    header (shared-types-plan phase 2). Until then --check reports them
    MISSING/DRIFT for that repo -- expected. Scope to the adopted header with
    `--artifact protocol` if you want a clean check pre-adoption.

WHEN TO RUN
    * After editing a canonical header: run --push, then commit each firmware
      repo (branch + PR per repo).
    * In each firmware repo's CI / pre-commit: run --check so a stale or
      hand-edited vendored copy fails the build.

USAGE -- maintainer (firmware repos are siblings of this repo on disk)
    tools/sync_protocol_header.py --push                        # both headers -> all copies
    tools/sync_protocol_header.py --check                       # verify all copies
    tools/sync_protocol_header.py --check --artifact protocol   # only opendisplay_protocol.h
    tools/sync_protocol_header.py --check --only Firmware_Silabs
    tools/sync_protocol_header.py --push  --artifact structs --only Firmware_NRF54
    tools/sync_protocol_header.py --list                        # show the copy map

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
DEFAULT_BASE = REPO_ROOT.parent                             # siblings live here (…/OD)

RAW_BASE = "https://raw.githubusercontent.com/davelee98/opendisplay-protocol/main/src/"


class Artifact:
    """A canonical C header + where it is vendored in each firmware repo."""

    def __init__(self, name: str, canonical_rel: str, repos: dict):
        self.name = name
        self.canonical_rel = Path(canonical_rel)            # within this repo
        self.raw_url = RAW_BASE + Path(canonical_rel).name
        self.repos = {k: Path(v) for k, v in repos.items()}  # repo key -> dest, rel to base


# The two canonical headers vendored into firmware. `structs` destinations mirror
# `protocol`'s per-repo layout (NRF54 already uses src/, Silabs the repo root).
ARTIFACTS = {
    "protocol": Artifact(
        "protocol", "src/opendisplay_protocol.h",
        {
            "Firmware":        "Firmware/include/opendisplay_protocol.h",
            "Firmware_NRF54":  "Firmware_NRF54/src/opendisplay_protocol.h",
            "Firmware_Silabs": "Firmware_Silabs/include/opendisplay_protocol.h",
            "Firmware_NRF":    "Firmware_NRF/opendisplay_protocol.h",
        },
    ),
    "structs": Artifact(
        "structs", "src/opendisplay_structs.h",
        {
            "Firmware":        "Firmware/include/opendisplay_structs.h",
            "Firmware_NRF54":  "Firmware_NRF54/src/opendisplay_structs.h",
            "Firmware_Silabs": "Firmware_Silabs/include/opendisplay_structs.h",
            "Firmware_NRF":    "Firmware_NRF/opendisplay_structs.h",
        },
    ),
}

ALL_REPOS = list(ARTIFACTS["protocol"].repos)


# --- helpers ----------------------------------------------------------------

def die(msg: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def selected_artifacts(args) -> list:
    if not args.artifact:
        return list(ARTIFACTS.values())
    out = []
    for key in args.artifact.split(","):
        key = key.strip()
        if key not in ARTIFACTS:
            die(f"unknown artifact '{key}'. Known: {', '.join(ARTIFACTS)}")
        out.append(ARTIFACTS[key])
    return out


def read_canonical(art, args) -> bytes:
    """Return the canonical header bytes from a URL, an explicit path, or the default."""
    if args.canonical_url:
        with urllib.request.urlopen(args.canonical_url, timeout=30) as resp:  # noqa: S310
            return resp.read()
    path = Path(args.canonical) if args.canonical else (REPO_ROOT / art.canonical_rel)
    if not path.is_file():
        die(f"canonical header not found: {path}")
    return path.read_bytes()


def resolve_targets(art, args) -> list:
    """Return the list of (repo_label, destination_path) to operate on for one artifact."""
    if args.dest:
        return [("(--dest)", Path(args.dest))]
    base = Path(args.base) if args.base else DEFAULT_BASE
    keys = args.only.split(",") if args.only else list(art.repos)
    out = []
    for key in keys:
        key = key.strip()
        if key not in art.repos:
            die(f"unknown repo '{key}'. Known: {', '.join(art.repos)}")
        out.append((key, base / art.repos[key]))
    return out


def unified_diff(canonical: bytes, current: bytes, label: str) -> str:
    a = current.decode("utf-8", "replace").splitlines(keepends=True)
    b = canonical.decode("utf-8", "replace").splitlines(keepends=True)
    return "".join(difflib.unified_diff(a, b, fromfile=f"{label} (vendored)",
                                        tofile="canonical", n=2))


# --- commands ---------------------------------------------------------------

def cmd_list(args) -> int:
    base = Path(args.base) if args.base else DEFAULT_BASE
    for art in selected_artifacts(args):
        print(f"[{art.name}] canonical : {REPO_ROOT / art.canonical_rel}")
        for key, rel in art.repos.items():
            print(f"           {key:<16} {base / rel}")
    print(f"base      : {base}")
    return 0


def cmd_push(args) -> int:
    changed = same = 0
    for art in selected_artifacts(args):
        canonical = read_canonical(art, args)
        for label, dest in resolve_targets(art, args):
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.is_file() and dest.read_bytes() == canonical:
                print(f"  up-to-date  [{art.name}] {label}  {dest}")
                same += 1
                continue
            dest.write_bytes(canonical)
            print(f"  wrote       [{art.name}] {label}  {dest}")
            changed += 1
    print(f"push: {changed} updated, {same} already current.")
    return 0


def cmd_check(args) -> int:
    ok = drift = missing = 0
    diffs: list = []
    for art in selected_artifacts(args):
        canonical = read_canonical(art, args)
        for label, dest in resolve_targets(art, args):
            if not dest.is_file():
                print(f"  MISSING     [{art.name}] {label}  {dest}")
                missing += 1
                continue
            current = dest.read_bytes()
            if current == canonical:
                print(f"  ok          [{art.name}] {label}  {dest}")
                ok += 1
            else:
                print(f"  DRIFT       [{art.name}] {label}  {dest}")
                drift += 1
                diffs.append(unified_diff(canonical, current, f"{art.name}:{dest}"))
    if diffs:
        print("\n--- drift detail (vendored vs canonical) ---")
        for d in diffs:
            print(d if d else "(binary or whitespace-only difference)")
    failed = drift + missing
    print(f"\ncheck: {ok} in sync, {drift} drifted, {missing} missing.")
    if failed:
        print("Run `sync_protocol_header.py --push` and commit the updated copies "
              "(structs copies are MISSING/DRIFT until a repo adopts the shared header).",
              file=sys.stderr)
    return 1 if failed else 0


# --- entrypoint -------------------------------------------------------------

def main(argv: list = None) -> int:
    p = argparse.ArgumentParser(
        prog="sync_protocol_header.py",
        description="Vendor / verify the canonical opendisplay_protocol.h and opendisplay_structs.h.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--push", action="store_true",
                      help="copy the canonical header(s) into every vendored copy")
    mode.add_argument("--check", action="store_true",
                      help="verify every vendored copy matches (exit 1 on drift)")
    mode.add_argument("--list", action="store_true",
                      help="print the canonical + copy paths and exit")

    p.add_argument("--artifact", metavar="NAME[,NAME...]",
                   help=f"restrict to specific headers ({', '.join(ARTIFACTS)}); default: all")
    p.add_argument("--only", metavar="NAME[,NAME...]",
                   help=f"restrict to specific repos ({', '.join(ALL_REPOS)})")
    p.add_argument("--base", metavar="DIR",
                   help="directory the firmware repos live under "
                        f"(default: {DEFAULT_BASE})")
    p.add_argument("--dest", metavar="FILE",
                   help="operate on ONE explicit vendored file (overrides --only/--base; "
                        "for a lone firmware-repo checkout; pair with --canonical-url and "
                        "a single --artifact)")
    p.add_argument("--canonical", metavar="FILE",
                   help="explicit canonical header path (default: this repo's src copy)")
    p.add_argument("--canonical-url", metavar="URL",
                   help="fetch the canonical header from a URL instead of a local file "
                        "(e.g. the GitHub raw path for the artifact being checked)")

    args = p.parse_args(argv)

    if args.dest and args.only:
        die("--dest and --only are mutually exclusive")
    if args.dest and not args.artifact and (args.canonical or args.canonical_url):
        # --dest compares one file against one explicit source; artifact selection is
        # only used to pick the default canonical, which --canonical/-url override.
        pass
    if args.dest and len(selected_artifacts(args)) > 1 and not (args.canonical or args.canonical_url):
        die("--dest needs a single --artifact (or --canonical / --canonical-url) to know which header to use")

    if args.list:
        return cmd_list(args)
    if args.push:
        return cmd_push(args)
    return cmd_check(args)


if __name__ == "__main__":
    raise SystemExit(main())
