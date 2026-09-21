#!/usr/bin/env python3
"""Scan `recipes/` and write `index/recipes.json` plus a gzip companion.

The index carries only what a client needs *before* fetching a recipe: where it
is, what type it is, what it depends on, what it is, and how to expand a
template. Everything else (#ENV:, #INPUT:, scheduler directives) is read from
the recipe once it is local.

Entry shapes:

    plain      "cellranger/9.0.1" -> {"path": ..., "type": "app",
                                      "description": ..., "deps": [...]}
    template   "grch38/star-gencode" -> {..., "is_template": true,
                                         "target_template": "...",
                                         "ph": {...}}

Templates are not expanded here; condatainer expands them at resolve time.
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from autoupdate import natural  # noqa: E402  — one ordering rule for all scripts
from indexio import write_index  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RECIPES_DIR = ROOT / "recipes"
OUT_DIR = ROOT / "index"
OUT_FILE = OUT_DIR / "recipes.json"

SKIP_SUFFIXES = (".py", ".md")
SKIP_NAMES = {"README.md"}


# ---------------------------------------------------------------------------
# JSON formatting
# ---------------------------------------------------------------------------

def format_json(value: object, level: int = 0) -> str:
    """Pretty-print mappings while keeping list values on one line."""
    if isinstance(value, dict):
        if not value:
            return "{}"
        indent = "  " * level
        child_indent = "  " * (level + 1)
        items = []
        for key in sorted(value):
            encoded_key = json.dumps(key, ensure_ascii=False)
            encoded_value = format_json(value[key], level + 1)
            items.append(f"{child_indent}{encoded_key}: {encoded_value}")
        return "{\n" + ",\n".join(items) + f"\n{indent}}}"
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False, separators=(", ", ": "))
    return json.dumps(value, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Header block parsing
# ---------------------------------------------------------------------------

def read_header_block(path: Path) -> list[str]:
    """Return the contiguous header lines after the shebang.

    Stops at the first line that is neither a header nor blank, so a #DEP: in a
    heredoc or a commented-out block is never seen.
    """
    lines = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for i, raw in enumerate(fh):
            line = raw.rstrip("\n")
            if i == 0 and line.startswith("#!"):
                continue
            if not line.strip():
                continue
            if line.startswith("#") and re.match(r"^#[A-Z][A-Z0-9_]*", line):
                lines.append(line)
                continue
            break
    return lines


def strip_note(value: str) -> str:
    """Drop an inline `## note` suffix."""
    idx = value.find("##")
    return value[:idx].strip() if idx >= 0 else value.strip()


def parse_pl_values(raw: str) -> list[str]:
    """Parse a #PH: value list. The separator sets the ordering.

    `,` sorts descending — for versions. `|` keeps the author's order — for
    labels, where "larger" means nothing. Either form takes integer ranges
    (`22-49`) and `*` for open-ended, which is always last.

    The first element is the default, so the separator is what chooses it.
    """
    sep = "|" if "|" in raw else ","
    tokens = [t.strip() for t in raw.split(sep)]
    open_ended = "*" in tokens
    tokens = [t for t in tokens if t and t != "*"]

    concrete, seen = [], set()
    range_re = re.compile(r"^(\d+)-(\d+)$")
    for tok in tokens:
        m = range_re.fullmatch(tok)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            for v in range(min(a, b), max(a, b) + 1):
                if str(v) not in seen:
                    concrete.append(str(v))
                    seen.add(str(v))
        elif tok not in seen:
            concrete.append(tok)
            seen.add(tok)

    if sep == ",":
        concrete.sort(key=natural, reverse=True)
    if open_ended:
        concrete.append("*")
    return concrete


def parse_headers(path: Path) -> dict:
    ph: dict[str, list[str]] = {}
    target = description = url = type_ = ""
    deps: list[str] = []

    for line in read_header_block(path):
        if line.startswith("#PH:"):
            rest = line[len("#PH:"):]
            idx = rest.find(":")
            if idx < 0:
                continue
            name = rest[:idx].strip()
            values = parse_pl_values(strip_note(rest[idx + 1:]))
            if name and name not in ph and values:
                ph[name] = values
        elif line.startswith("#TARGET:"):
            target = target or strip_note(line[len("#TARGET:"):])
        elif line.startswith("#DESC:"):
            description = description or strip_note(line[len("#DESC:"):])
        elif line.startswith("#URL:"):
            url = url or strip_note(line[len("#URL:"):])
        elif line.startswith("#TYPE:"):
            type_ = type_ or strip_note(line[len("#TYPE:"):]).lower()
        elif line.startswith("#DEP:"):
            dep = strip_note(line[len("#DEP:"):])
            if dep:
                deps.append(dep)

    return {
        "ph": ph,
        "target": target,
        "description": description,
        "url": url,
        "type": type_,
        "deps": deps,
    }


# ---------------------------------------------------------------------------
# Type derivation
# ---------------------------------------------------------------------------

def derive_type(name: str, target: str, is_def: bool, declared: str) -> str:
    """`<distro>/base.def` is base, any other .def is os, otherwise slash count.

    The slash count is read off the module path, which for a template is its
    #TARGET: rather than the recipe filename — `grch38/star-gencode` builds
    `grch38/star/{star_version}/...`, and only the latter says it is data.
    #TYPE: overrides only app/data; a def's type comes from its path.
    """
    if is_def:
        return "base" if name.endswith("/base") else "os"
    if declared in ("app", "data"):
        return declared
    return "data" if (target or name).count("/") >= 2 else "app"


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

def main() -> None:
    index: dict[str, dict] = {}

    for path in sorted(RECIPES_DIR.rglob("*")):
        if not path.is_file():
            continue
        if path.name in SKIP_NAMES or path.suffix in SKIP_SUFFIXES:
            continue

        rel = path.relative_to(RECIPES_DIR).as_posix()
        is_def = rel.endswith(".def")
        name = rel[:-len(".def")] if is_def else rel

        h = parse_headers(path)
        entry: dict = {
            "path": f"recipes/{rel}",
            "type": derive_type(name, h["target"], is_def, h["type"]),
            "description": h["description"],
        }
        if h["url"]:
            entry["url"] = h["url"]
        if h["deps"]:
            entry["deps"] = h["deps"]
        if h["ph"]:
            if not h["target"]:
                print(f"WARNING: {name} has #PH: but no #TARGET:")
            entry["is_template"] = True
            entry["target_template"] = h["target"]
            entry["ph"] = h["ph"]

        index[name] = entry

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    text = format_json(index) + "\n"
    written = write_index(OUT_FILE, text)

    templates = sum(1 for e in index.values() if e.get("is_template"))
    types: dict[str, int] = {}
    for e in index.values():
        types[e["type"]] = types.get(e["type"], 0) + 1
    print(f"{'Wrote' if written else 'Unchanged:'} {len(index)} entries ({templates} templates) in {OUT_FILE}")
    print("  " + ", ".join(f"{k}: {v}" for k, v in sorted(types.items())))


if __name__ == "__main__":
    main()
