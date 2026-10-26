#!/usr/bin/env python3
"""Scan `helpers/` and write `index/helpers.json` plus a gzip companion.

A helper is not a buildable module: no kind, no deps, no template expansion.
Its headers (#PARAM:, #ENV_PACKAGES:, #DEP:, #BIND:, #NCPU: ...)
are read at run time from the downloaded script. The index holds what a client
needs without downloading it: the path, the first #DESC: for searching, and the
SHA-256 of the file bytes for telling whether an added copy is current.

    {"schema_version": 1, "helpers": {"jupyterlab": {"path": "helpers/jupyterlab",
        "description": "JupyterLab", "sha256": "9f2c..."}}}

A helper with no #DESC: has no description.
"""
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from generate_recipe_index import read_header_block, strip_note  # noqa: E402
from indexio import write_index  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
HELPERS_DIR = ROOT / "helpers"
OUT_DIR = ROOT / "index"
OUT_FILE = OUT_DIR / "helpers.json"

INDEX_SCHEMA_VERSION = 1
SKIP_NAMES = {"README.md"}


def entry(path: Path) -> dict[str, str]:
    result = {"path": f"helpers/{path.name}"}
    for line in read_header_block(path):
        if line.startswith("#DESC:"):
            description = strip_note(line[len("#DESC:"):])
            if description:
                result["description"] = description
            break
    result["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def main() -> None:
    index: dict[str, dict[str, str]] = {}
    if HELPERS_DIR.is_dir():
        for path in sorted(HELPERS_DIR.iterdir()):
            if not path.is_file() or path.name in SKIP_NAMES or path.name.startswith("."):
                continue
            index[path.name] = entry(path)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    document = {"schema_version": INDEX_SCHEMA_VERSION, "helpers": index}
    text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    verb = "Wrote" if write_index(OUT_FILE, text) else "Unchanged:"
    print(f"{verb} {len(index)} helpers in {OUT_FILE}")


if __name__ == "__main__":
    main()
