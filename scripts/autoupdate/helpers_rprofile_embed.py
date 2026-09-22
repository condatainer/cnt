#!/usr/bin/env python3
"""Sync assets/helpers/rstudio-server/.Rprofile into the heredoc embedded in
helpers/rstudio-server.

helpers/rstudio-server writes this file into a user's directory itself, with no
runtime fetch of a companion asset, so the two copies have to stay identical.
Only the text between the BEGIN/END markers is rewritten; the source file
itself is what scripts/autoupdate/helpers_rprofile.py keeps current, so this
script always runs after it in the same autoupdate pass.

    python3 scripts/autoupdate/helpers_rprofile_embed.py [--check]
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "assets" / "helpers" / "rstudio-server" / ".Rprofile"
TARGET = ROOT / "helpers" / "rstudio-server"

BEGIN = "    # BEGIN embedded from assets/helpers/rstudio-server/.Rprofile — edit that\n"
END = "    # END embedded from assets/helpers/rstudio-server/.Rprofile\n"


def regenerate(source: str, target: str) -> str | None:
    """Return target with the embedded block refreshed from source, or None
    when nothing changed."""
    start, stop = target.find(BEGIN), target.find(END)
    if start < 0 or stop < start:
        raise SystemExit("no BEGIN/END embed markers in helpers/rstudio-server")

    block = (
        BEGIN
        + "    # file, not here, then run scripts/autoupdate/helpers_rprofile_embed.py\n"
        + '    cat > "$CNT_HELPER_CWD/.Rprofile" <<\'RPROFILE\'\n'
        + source
        + "RPROFILE\n"
    )
    updated = target[:start] + block + target[stop:]
    return updated if updated != target else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="exit 1 when the file is stale")
    args = ap.parse_args()

    label = "helpers/rstudio-server"
    try:
        source = SOURCE.read_text()
        target = TARGET.read_text()
        updated = regenerate(source, target)
    except (OSError, SystemExit) as err:
        print(f"[FAIL] {label}: {err}", file=sys.stderr)
        return 1

    if updated is None:
        print(f"[SKIP] {label}: up to date")
        return 0
    if args.check:
        print(f"[FAIL] {label}: stale, run scripts/autoupdate/helpers_rprofile_embed.py", file=sys.stderr)
        return 1
    TARGET.write_text(updated)
    print(f"[UPDATED] {label}: synced from {SOURCE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
