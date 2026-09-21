#!/usr/bin/env python3
"""Group Posit Package Manager's R system requirements by apt package.

Fetches the CRAN and Bioconductor system requirements for one Ubuntu release
from https://packagemanager.posit.co and prints one `apt-get -y install` line
per system package, with the R packages that need it as a trailing comment.
The output is the block pasted into recipes/ubuntu24/r-build-essential.def.

    python3 group_apt_by_rpkg.py                         # ubuntu 24.04, stdout
    python3 group_apt_by_rpkg.py --release 24.04 --bioc-version 3.23 -o block.sh

Setup pages: https://packagemanager.posit.co/client/#/repos/cran/setup
             https://packagemanager.posit.co/client/#/repos/bioconductor/setup
"""

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

API = "https://packagemanager.posit.co/__api__/repos"

# Packages to comment out (can include simple glob suffix '*')
FILTERS = [
    "chromium", "nvidia-cuda-dev", "ocl-icd-opencl-dev", "texlive",
]

HEADER = [
    "    # System requirements from Posit Package Manager, grouped by assets/r-build-essential/group_apt_by_rpkg.py",
    "    # https://packagemanager.posit.co/client/#/repos/cran/setup",
    "    # https://packagemanager.posit.co/client/#/repos/bioconductor/setup",
]


def fetch_requirements(repo, release, bioc_version=None):
    """Return the `requirements` list of one repo's sysreqs response."""
    query = {"all": "true", "distribution": "ubuntu", "release": release}
    if bioc_version:
        query["bioc_version"] = bioc_version
    url = f"{API}/{repo}/sysreqs?{urllib.parse.urlencode(query)}"
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            return json.load(response)["requirements"]
    except (urllib.error.URLError, KeyError, json.JSONDecodeError) as err:
        sys.exit(f"Could not read {url}: {err}")


def group_by_apt_package(entries):
    """Map each system package to the set of R packages that need it."""
    pkg_to_rpkgs = defaultdict(set)
    for entry in entries:
        for pkg in entry["requirements"].get("packages", []):
            pkg_to_rpkgs[pkg].add(entry["name"])
    return pkg_to_rpkgs


def is_filtered(pkg_name):
    for pat in FILTERS:
        if pat.endswith("*"):
            if pkg_name.startswith(pat[:-1]):
                return True
        elif pkg_name == pat:
            return True
    return False


def render(pkg_to_rpkgs):
    lines = list(HEADER)
    # Emit non-lib packages first, then lib* packages last
    ordered = sorted(p for p in pkg_to_rpkgs if not p.startswith("lib")) + \
        sorted(p for p in pkg_to_rpkgs if p.startswith("lib"))
    for pkg in ordered:
        rpkgs = " ".join(sorted(pkg_to_rpkgs[pkg]))
        comment_prefix = "# " if is_filtered(pkg) else ""
        lines.append(f"    {comment_prefix}apt-get -y install {pkg} # {rpkgs}")
    # Clean up apt lists to reduce image size
    lines += ["", "    apt-get clean && rm -rf /var/lib/apt/lists/*"]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--release", default="24.04", help="Ubuntu release (default: 24.04)")
    parser.add_argument("--bioc-version", default="3.23", help="Bioconductor version (default: 3.23)")
    parser.add_argument("-o", "--output", help="write here instead of stdout")
    args = parser.parse_args()

    entries = fetch_requirements("cran", args.release)
    entries += fetch_requirements("bioconductor", args.release, args.bioc_version)
    text = render(group_by_apt_package(entries))

    if args.output:
        with open(args.output, "w") as out:
            out.write(text)
        print(f"Generated: {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
