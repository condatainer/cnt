#!/usr/bin/env python3
"""Reorder a recipe's or helper's header block into the conventional groups.

A recipe:

    what it is        #DESC: #URL: #TYPE: #ARCH: #TARGET:
    what may be done  #LICENSE: #REDISTRIBUTE:
    placeholders      #PH:
    maintenance       #AUTOUPDATE:
    what it needs     #DEP: #SOURCE: #INPUT:
    what it gives     #ENV:
    how it is built   #SBATCH #PBS #BSUB

A helper:

    what it is        #DESC:
    session defaults  #NCPUS: #MEM: #TIME: #GPU: #SINGLETON:
    what it loads     #REQUIRED_OVERLAYS: #IMG_PACKAGES: #POST_INSTALL_CMD: #BIND:
    parameters        each #PARAM:, then its #VALUE: and #AUTOUPDATE:

Blank lines separate the groups. Order within a group is left alone — a `#PH:`
list and its `#DEP:` lines are in the order the author chose. A helper's
parameters stay together, in the order they first appear, and a parameter with a
`#VALUE:` or `#AUTOUPDATE:` line is set off by a blank line.

Nothing depends on this: no parser looks at header order or adjacency, so it is
a reading convention and reformatting can never change what a recipe means.

    python3 scripts/format_headers.py [--check] [PATH ...]
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECIPES_DIR = ROOT / "recipes"
HELPERS_DIR = ROOT / "helpers"
SKIP_SUFFIXES = (".py", ".md")

GROUPS = [
    ["DESC", "URL", "TYPE", "ARCH", "TARGET"],
    ["LICENSE", "REDISTRIBUTE"],
    ["PH"],
    ["AUTOUPDATE"],
    ["DEP", "SOURCE", "INPUT"],
    ["ENV"],
    ["SBATCH", "PBS", "BSUB"],
]

HELPER_GROUPS = [
    ["DESC"],
    ["NCPUS", "MEM", "TIME", "GPU", "SINGLETON"],
    ["REQUIRED_OVERLAYS", "IMG_PACKAGES", "POST_INSTALL_CMD", "BIND"],
]
# The headers that describe one parameter, in the order they are written.
PARAM_HEADERS = ["PARAM", "VALUE", "AUTOUPDATE"]

HEADER_RE = re.compile(r"^#([A-Z][A-Z0-9_]*)")
PARAM_KEY_RE = re.compile(r"^#(?:PARAM|VALUE|AUTOUPDATE):\s*([A-Za-z0-9_]+)")


def split(text: str):
    """Return (leading, headers, body).

    The header block starts after the shebang and ends at the first line that
    is neither a header nor blank.
    """
    lines = text.splitlines()
    i = 1 if lines and lines[0].startswith("#!") else 0
    leading, headers = lines[:i], []
    while i < len(lines):
        if not lines[i].strip():
            i += 1
            continue
        if not HEADER_RE.match(lines[i]):
            break
        headers.append(lines[i])
        i += 1
    return leading, headers, lines[i:]


def header_key(line: str) -> str:
    """The header name, or "" for a line that is not one."""
    m = HEADER_RE.match(line)
    return m.group(1) if m else ""


def param_blocks(headers: list[str]) -> list[list[str]]:
    """Group the parameter headers by the parameter they describe.

    A block is the `#PARAM:` line, then its `#VALUE:` lines, then its
    `#AUTOUPDATE:` lines, in the order the parameters first appear. A `#VALUE:`
    or `#AUTOUPDATE:` naming no `#PARAM:` forms a block of its own.
    """
    keys: list[str] = []
    found: dict[str, dict[str, list[str]]] = {}
    for line in headers:
        m = PARAM_KEY_RE.match(line)
        if not m:
            continue
        key = m.group(1)
        if key not in found:
            keys.append(key)
            found[key] = {h: [] for h in PARAM_HEADERS}
        found[key][header_key(line)].append(line)
    return [[line for h in PARAM_HEADERS for line in found[k][h]] for k in keys]


def format_helper(text: str) -> str:
    leading, headers, body = split(text)
    if not headers:
        return text

    groups = [[h for h in headers if header_key(h) in g] for g in HELPER_GROUPS]
    known = {h for g in HELPER_GROUPS for h in g} | set(PARAM_HEADERS)
    unknown = [h for h in headers if header_key(h) not in known]

    out = list(leading)
    for group in (g for g in groups if g):
        out += group + [""]
    blocks = param_blocks(headers)
    for i, block in enumerate(blocks):
        out += block
        last = i == len(blocks) - 1
        if not last and (len(block) > 1 or len(blocks[i + 1]) > 1):
            out.append("")
    if blocks:
        out.append("")
    if unknown:
        out += unknown + [""]
    out += body
    return "\n".join(out).rstrip("\n") + "\n"


def format_file(path: Path, text: str) -> str:
    if HELPERS_DIR in path.resolve().parents:
        return format_helper(text)
    return format_text(text)


def format_text(text: str) -> str:
    leading, headers, body = split(text)
    if not headers:
        return text

    buckets = [[h for h in headers if header_key(h) in g] for g in GROUPS]
    # Anything unrecognised keeps its order and lands last, where the validator
    # will complain about it rather than this script silently dropping it.
    unknown = [h for h in headers if not any(header_key(h) in g for g in GROUPS)]
    if unknown:
        buckets.append(unknown)

    out = list(leading)
    for bucket in (b for b in buckets if b):
        out += bucket + [""]
    out += body
    return "\n".join(out).rstrip("\n") + "\n"


def targets(argv: list[str]) -> list[Path]:
    paths = [Path(a) for a in argv if not a.startswith("-")]
    if paths:
        return paths
    return [p for d in (RECIPES_DIR, HELPERS_DIR) for p in sorted(d.rglob("*"))
            if p.is_file() and p.suffix not in SKIP_SUFFIXES]


def rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def main() -> int:
    check = "--check" in sys.argv
    paths = targets(sys.argv[1:])
    changed: list[Path] = []

    for path in paths:
        text = path.read_text(encoding="utf-8")
        formatted = format_file(path, text)
        if formatted == text:
            continue
        changed.append(path)
        if not check:
            path.write_text(formatted, encoding="utf-8")

    verb = "needs formatting" if check else "formatted"
    for path in changed:
        print(f"{verb}: {rel(path)}")

    if check:
        print(f"{len(changed)} of {len(paths)} files need formatting")
        return 1 if changed else 0

    print(f"{len(changed)} reformatted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
