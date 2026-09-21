#!/usr/bin/env python3
"""Check natural() against the shared version-ordering cases.

The Go side reads the same file (catalog/version_test.go), so the two
implementations are pinned to one table rather than to each other's source.

    python3 scripts/test_version_order.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from autoupdate import natural  # noqa: E402

CASES = Path(__file__).resolve().parent / "version_cases.txt"


def compare(a: str, b: str) -> int:
    ka, kb = natural(a), natural(b)
    return 1 if ka > kb else (-1 if ka < kb else 0)


def main() -> int:
    failures = 0
    checked = 0
    for lineno, line in enumerate(CASES.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        a, b, want = line.split("|")
        got = compare(a, b)
        checked += 1
        if got != int(want):
            print(f"{CASES.name}:{lineno}: {a} vs {b}: got {got}, want {want}")
            failures += 1

    if failures:
        print(f"\n{failures} of {checked} cases disagree with the shared table.")
        return 1
    print(f"{checked} cases agree.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
