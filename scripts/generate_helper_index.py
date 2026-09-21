#!/usr/bin/env python3
"""Scan `helpers/` and write `index/helpers.json` plus a gzip companion.

A helper is not a buildable module: no kind, no deps, no template expansion.
Its headers (#PARAM:, #IMG_PACKAGES:, #REQUIRED_OVERLAYS:, #BIND:, #NCPUS: ...)
are read at run time from the downloaded script, so this index is a directory
listing and nothing is parsed here.

    {"jupyterlab": {"path": "helpers/jupyterlab"}}
"""
import gzip
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPERS_DIR = ROOT / "helpers"
OUT_DIR = ROOT / "index"
OUT_FILE = OUT_DIR / "helpers.json"

SKIP_NAMES = {"README.md"}


def main() -> None:
    index: dict[str, dict[str, str]] = {}
    if HELPERS_DIR.is_dir():
        for path in sorted(HELPERS_DIR.iterdir()):
            if not path.is_file() or path.name in SKIP_NAMES or path.name.startswith("."):
                continue
            index[path.name] = {"path": f"helpers/{path.name}"}

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    text = json.dumps(index, indent=2, sort_keys=True, ensure_ascii=False)
    OUT_FILE.write_text(text + "\n", encoding="utf-8")
    with gzip.open(str(OUT_FILE) + ".gz", "wt", encoding="utf-8") as fh:
        fh.write(text + "\n")

    print(f"Wrote {len(index)} helpers to {OUT_FILE}")


if __name__ == "__main__":
    main()
