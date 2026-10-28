#!/usr/bin/env python3
"""Print the overlays build-os.yml checks for a push, one name or pattern per line.

The environment names the push:

    BEFORE, AFTER  the commits it moved between

A changed plain recipe is its name. A changed template is a pattern over all
its values, such as `ubuntu24/r/*`: `registry missing` says which of them are
missing, so the script does not read what changed inside the file.

Exits 1, printing nothing, when the push cannot be compared, such as a new
branch or a force push. The workflow then checks every recipe.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from generate_recipe_index import parse_headers  # noqa: E402


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def name_of(path: str) -> str:
    h = parse_headers(Path(path))
    if h["ph"] and h["target"]:
        return re.sub(r"\{[^}]*\}", "*", h["target"])
    return path.removeprefix("recipes/").removesuffix(".def")


def main() -> int:
    before, after = os.environ["BEFORE"], os.environ["AFTER"]
    if set(before) == {"0"}:
        return 1
    try:
        paths = git("diff", "--name-only", "--diff-filter=AMR", before, after, "--", "recipes/*.def").split()
    except subprocess.CalledProcessError:
        return 1
    print("\n".join(sorted({name_of(p) for p in paths})))
    return 0


if __name__ == "__main__":
    sys.exit(main())
