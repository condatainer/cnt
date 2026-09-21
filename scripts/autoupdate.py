#!/usr/bin/env python3
"""Keep recipes current with their upstreams.

Implements plan/autoupdate.md. A *source* discovers versions and never touches a
file; this script is the only thing that writes recipes.

    #AUTOUPDATE:<name>:<source> [<param>=<value> ...]

<name> says what to rewrite, inferred from the recipe: a #PH: key rewrites the
value list, a #DEP: module bumps its preferred version, anything else is a body
pin located by `regex`.

    python3 scripts/autoupdate.py [--dry-run] [--only NAME]
"""
import argparse
import itertools
import json
import os
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECIPES_DIR = ROOT / "recipes"

SKIP_SUFFIXES = (".py", ".md")

# Some HPC systems have broken cert bundles; every source here is public
# read-only data, so a failed handshake is a worse outcome than an unverified
# one. Matches the behaviour of the scripts this replaces.
_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE

TIMEOUT = 20

# Params that select *which* versions rather than *where they come from*, so
# two headers differing only in these share one fetch (plan §6).
_NON_IDENTITY = {"min", "max", "verify"}

# A derived list may feed another; stop rather than spin on a cycle.
MAX_DERIVE_PASSES = 5

# A runaway guard, not a budget: pagination is followed to the end, and this
# only stops an API that never says it is done. Sized off real repos rather than
# a round number — library/node is 90 pages today and grows, so a cap anywhere
# near it would start failing a working recipe. See truncated().
MAX_PAGES = 200

# The one parameter every source obviously has, so it can be written bare.
PRIMARY = {
    "conda": "pkg", "github": "repo", "docker": "image",
    "listing": "url", "json": "url", "xml": "url", "git": "url",
    "dep": "from", "cmd": "run",
}

# Checked by validate_recipes.py, so a missing one is caught before a run.
REQUIRED = {
    "conda": {"pkg"}, "github": {"repo"}, "docker": {"image"},
    "listing": {"url", "regex"}, "json": {"url", "path"},
    "xml": {"url", "path"}, "git": {"url"},
    "dep": {"from"}, "cmd": {"run"},
}
COMMON_PARAMS = {"min", "max", "verify", "regex", "pin"}
OPTIONAL = {"json": {"path"}, "xml": {"path"}, "docker": {"filter"}}

# Named patterns, so a hand-rolled `(\d+\.\d+)` does not silently truncate.
# Not semver.org's official regex: that captures major/minor/patch separately,
# and group 1 has to be the whole version.
# Group 1 is the version, so anything not part of it has to sit outside the
# group rather than be stripped afterwards. A `v` prefix never is one, so every
# pattern takes it and none captures it — an axis that would otherwise double
# the number of names for nothing.
PATTERNS = {
    "@int": r"^v?(\d+)$",
    "@version": r"^v?(\d+(?:\.\d+)+)$",
    "@semver": r"^v?(\d+\.\d+\.\d+)$",
    "@semver-pre": r"^v?(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)$",
}

# Where a regex is matched against a bare version string, so anchoring it is
# both possible and almost always right. Elsewhere it searches a page, a file,
# or a URL pulled out of JSON.
TAG_SOURCES = {"conda", "github", "docker", "git"}


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------

def _runs(s: str) -> list:
    """Digit runs as ints, the text between them as strings."""
    return [int(c) if c.isdigit() else c for c in re.split(r"(\d+)", s)]


def natural(s: str) -> tuple:
    """Order a version. Digit runs compare numerically, and a `-` followed by a
    letter is a prerelease, ranking below the bare version it qualifies.

    `2.7.11b > 2.7.9a`, `M36 > M9`, `1.2.3.10 > 1.2.3.4` — not semver, because a
    `-` suffix is usually not a prerelease at all: RStudio's `2026.06.0-242` is a
    build number, and semver's rule would rank it below a bare version. Requiring
    a letter is what separates `-rc1` from `-242`; a build number keeps sorting on
    its digits, so `2026.06.0-242 > 2026.06.0`.

    Must stay identical to `CompareVersions` in the Go `catalog` package: the
    index this writes is consumed verbatim over HTTP, so a disagreement makes
    `ph[0]` — the default offered to users — depend on whether the collection was
    read from disk or fetched. `test_version_order.py` pins the two together.
    """
    head, dash, tail = s.partition("-")
    if not (dash and tail[:1].isalpha()):
        return (_runs(s), 1, [])
    return (_runs(head), 0, _runs(tail))


def newer(a: str, b: str) -> bool:
    """True when a sorts after b."""
    return natural(a) > natural(b)


def suspicious(existing: list[str], fresh: list[str], sep: str) -> str:
    """Why a set of new values does not look like a release. "" if it does.

    Nothing reviews these commits, so some of the judgement a reader would have
    applied has to be a check. Only the unambiguous part: a check that refuses a
    real release stops that recipe updating, and since one failed source does
    not fail the run, nobody would notice.

    Deliberately not checked: whether a value's *structure* matches its
    neighbours. A first suffixed release, a first four-part version and a switch
    to calendar versioning all look wrong by that measure, and a truncated match
    like `4.6` beside `4.5.3` is indistinguishable from a genuine `1.3` after
    `1.3.1`. Excluding prereleases is what anchoring the `regex` is for.
    """
    limit = max(10, len(existing))
    if len(fresh) > limit:
        return (f"{len(fresh)} new values at once, limit {limit} — "
                f"no upstream releases that fast")

    # A `|` list is labels, not versions — `ucsc_no_alt` beside `gencode` is
    # normal, and a derived selector adds exactly that kind of value.
    if sep == "|":
        return ""

    # `latest`, `stable`, `main`. Worth refusing beyond it being wrong: natural
    # ordering puts a bare word above every number, so it would be picked as
    # the preferred version.
    if all(any(c.isdigit() for c in v) for v in existing):
        wordy = [v for v in fresh if not any(c.isdigit() for c in v)]
        if wordy:
            return f"{', '.join(wordy[:3])} has no version number in it"

    # Both bounds are needed: `*2` alone rejects 3 -> 10 in a short list,
    # `+100` alone rejects nothing at the scale these lists run at.
    if all(v.isdigit() for v in existing + fresh):
        ceiling = max(int(v) for v in existing)
        for v in fresh:
            if int(v) > ceiling * 2 and int(v) > ceiling + 100:
                return f"{v} is far outside the existing range (max {ceiling})"
    return ""


def encode_values(values: list[str], sep: str) -> str:
    """Render a value list, re-encoding contiguous integers as a range."""
    concrete = [v for v in values if v != "*"]
    if sep == "," and concrete and all(v.isdigit() for v in concrete):
        ints = sorted(int(v) for v in concrete)
        if ints == list(range(ints[0], ints[-1] + 1)) and len(ints) > 2:
            concrete = [f"{ints[0]}-{ints[-1]}"]
    out = list(concrete)
    if "*" in values:
        out.append("*")
    return sep.join(out)


def decode_values(raw: str) -> tuple[list[str], str]:
    """Split a #PH: value list. Returns (values, separator)."""
    sep = "|" if "|" in raw else ","
    values, open_ended = [], False
    for tok in (t.strip() for t in raw.split(sep)):
        if tok == "*":
            open_ended = True
        elif tok:
            m = re.fullmatch(r"(\d+)-(\d+)", tok)
            if m:
                a, b = int(m.group(1)), int(m.group(2))
                values += [str(v) for v in range(min(a, b), max(a, b) + 1)]
            else:
                values.append(tok)
    if open_ended:
        values.append("*")
    return values, sep


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

_FETCHED: dict[str, bytes] = {}


def fetch(url: str) -> bytes:
    """GET a URL once per run.

    Deduping here rather than per header means it holds however two headers
    differ — a different `regex` over the same package, or one recipe reading a
    directory listing another one also reads. Upstreams are polled once a day
    and nothing in a run depends on a response changing midway through.
    """
    if url in _FETCHED:
        return _FETCHED[url]
    headers = {"User-Agent": "cnt-autoupdate"}
    # 60 requests/hour unauthenticated, 5000 with a token. Free in Actions;
    # a local run without one still works until it hits the limit.
    token = os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=_SSL) as resp:
        _FETCHED[url] = resp.read()
    return _FETCHED[url]


def head_ok(url: str) -> bool:
    """Whether the file exists, by HEAD then a one-byte ranged GET.

    Some hosts answer HEAD with 403 or 405 while serving GET fine. Treating
    that as a missing file would drop a real release and report it as not yet
    published — a recipe frozen behind a message saying it is current. A 404
    is conclusive, so it is not retried.
    """
    for method, extra in (("HEAD", {}), ("GET", {"Range": "bytes=0-0"})):
        try:
            req = urllib.request.Request(
                url, method=method,
                headers={"User-Agent": "cnt-autoupdate", **extra})
            with urllib.request.urlopen(req, timeout=TIMEOUT,
                                        context=_SSL) as resp:
                if resp.status in (200, 206):
                    return True
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                return False
        except Exception:
            pass
    return False


def json_path(data, path: str) -> list:
    """Walk a dotted path. A `[]` segment iterates a list."""
    values = [data]
    for seg in path.split("."):
        if not seg:
            continue
        nxt = []
        iterate = seg.endswith("[]")
        key = seg[:-2] if iterate else seg
        for v in values:
            if key:
                if not isinstance(v, dict) or key not in v:
                    continue
                v = v[key]
            if iterate:
                nxt.extend(v if isinstance(v, list) else [])
            else:
                nxt.append(v)
        values = nxt
    return [v for v in values if isinstance(v, (str, int, float))]


def truncated(kind: str, what: str) -> str:
    """Why a capped tag list must fail rather than be used.

    A truncated list is indistinguishable from an upstream that deleted its old
    releases, so extend-never-replace refuses it — and reports a loss that never
    happened, every run, forever. Returning what was fetched would be worse than
    fetching nothing.
    """
    return (f"{kind} {what} did not finish paginating in {MAX_PAGES} pages; "
            f"the list would be truncated, which reads as an upstream deletion")


def apply_regex(strings: list, pattern: str | None) -> list[str]:
    """Capture group 1 from each string. No pattern means take it whole."""
    if not pattern:
        return [str(s) for s in strings]
    rx = re.compile(pattern)
    out = []
    for s in strings:
        m = rx.search(str(s))
        if m:
            out.append(m.group(1) if m.groups() else m.group(0))
    return out


# ---------------------------------------------------------------------------
# Sources — each returns a list of version strings and writes nothing
# ---------------------------------------------------------------------------

def src_listing(p: dict[str, str]) -> list[str]:
    body = fetch(p["url"]).decode("utf-8", "replace")
    rx = re.compile(p["regex"])
    return [m.group(1) if m.groups() else m.group(0) for m in rx.finditer(body)]


def src_json(p: dict[str, str]) -> list[str]:
    data = json.loads(fetch(p["url"]))
    return apply_regex(json_path(data, p["path"]), p.get("regex"))


def src_xml(p: dict[str, str]) -> list[str]:
    root = ET.fromstring(fetch(p["url"]))
    found = [(e.text or "") for e in root.iterfind(p["path"])]
    return apply_regex(found, p.get("regex"))


def src_git(p: dict[str, str]) -> list[str]:
    out = subprocess.run(["git", "ls-remote", "--tags", "--refs", p["url"]],
                         capture_output=True, text=True, timeout=TIMEOUT)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "git ls-remote failed")
    tags = [line.split("refs/tags/")[-1] for line in out.stdout.splitlines()
            if "refs/tags/" in line]
    return apply_regex(tags, p.get("regex"))


def src_cmd(p: dict[str, str], cwd: Path) -> list[str]:
    out = subprocess.run(p["run"], shell=True, capture_output=True, text=True,
                         cwd=cwd, timeout=300)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "command failed")
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def src_conda(p: dict[str, str]) -> list[str]:
    """`pkg=channel::name`. The channel is required, never defaulted."""
    pkg = p["pkg"]
    if "::" not in pkg:
        raise RuntimeError(f"conda pkg must be channel::name, got {pkg!r}")
    channel, name = pkg.split("::", 1)
    data = json.loads(fetch(f"https://api.anaconda.org/package/{channel}/{name}"))
    return apply_regex(data.get("versions", []), p.get("regex"))


def src_github(p: dict[str, str]) -> list[str]:
    """Every tag, paged to the end. One page hides older releases."""
    tags = []
    for page in range(1, MAX_PAGES + 1):
        data = json.loads(fetch(f"https://api.github.com/repos/{p['repo']}"
                                f"/tags?per_page=100&page={page}"))
        tags += [t.get("name", "") for t in data]
        if len(data) < 100:
            return apply_regex(tags, p.get("regex"))
    raise RuntimeError(truncated("repo", p["repo"]))


def src_docker(p: dict[str, str]) -> list[str]:
    """Docker Hub tags, following `next` to the end.

    `filter=` is Docker Hub's server-side substring match — optional, and only
    ever a cost saving: posit/r-base is 16 pages unfiltered and 2 with
    `filter=noble`. Correctness never depends on it.
    """
    image = p["image"]
    if "/" not in image:
        image = f"library/{image}"
    url = f"https://hub.docker.com/v2/repositories/{image}/tags?page_size=100"
    if p.get("filter"):
        url += f"&name={urllib.parse.quote(p['filter'])}"

    tags, seen = [], set()
    for _ in range(MAX_PAGES):
        if not url:
            return apply_regex(tags, p.get("regex"))
        # A `next` that points back at a page already fetched is a loop, and
        # catching it here fails on the second request rather than the 200th.
        if url in seen:
            raise RuntimeError(f"image {image} paginates in a cycle at {url}")
        seen.add(url)
        data = json.loads(fetch(url))
        tags += [t.get("name", "") for t in data.get("results", [])]
        url = data.get("next")
    raise RuntimeError(truncated("image", image))


SOURCES = {
    "listing": src_listing,
    "json": src_json,
    "xml": src_xml,
    "git": src_git,
    "conda": src_conda,
    "github": src_github,
    "docker": src_docker,
}


# ---------------------------------------------------------------------------
# The `dep` source — upstream is the collection itself
# ---------------------------------------------------------------------------

def recipe_modules(path: Path) -> list[str]:
    """Every concrete module name a recipe publishes.

    A plain recipe publishes one; a template publishes the product of its
    placeholder values. `*` is skipped — it stands for values not enumerated.
    """
    ph, target = {}, ""
    for line in header_block(path):
        if line.startswith("#PH:"):
            rest = line[len("#PH:"):]
            if ":" not in rest:
                continue
            name, raw = rest.split(":", 1)
            values, _ = decode_values(strip_note(raw))
            ph[name.strip()] = [v for v in values if v != "*"]
        elif line.startswith("#TARGET:") and not target:
            target = strip_note(line[len("#TARGET:"):])

    rel = path.relative_to(RECIPES_DIR).as_posix()
    if not target:
        return [rel[:-len(".def")] if rel.endswith(".def") else rel]
    if not ph:
        return [target]

    keys = list(ph)
    out = []
    for combo in itertools.product(*(ph[k] for k in keys)):
        name = target
        for k, v in zip(keys, combo):
            name = name.replace("{" + k + "}", v)
        out.append(name)
    return out


def build_namespaces() -> dict[str, list[str]]:
    """Map a module namespace to the versions published under it.

    `grch38/genome` -> ["gencode", "ucsc_no_alt"] regardless of whether each
    came from a template or from its own recipe file.
    """
    ns = {}
    for path in sorted(RECIPES_DIR.rglob("*")):
        if not path.is_file() or path.suffix in SKIP_SUFFIXES:
            continue
        for module in recipe_modules(path):
            head, _, version = module.rpartition("/")
            if head and version not in ns.setdefault(head, []):
                ns[head].append(version)
    return ns


def src_dep(p: dict[str, str], namespaces: dict[str, list[str]]) -> list[str]:
    """Derive from what a dependency publishes. `from=` may list several,
    in which case the value must exist in all of them."""
    froms = [f.strip() for f in p.get("from", "").split(",") if f.strip()]
    if not froms:
        raise RuntimeError("dep source needs from=<module>")

    sets = []
    for f in froms:
        if f not in namespaces:
            raise RuntimeError(f"nothing is published under {f}")
        sets.append(namespaces[f])

    values = sets[0]
    for other in sets[1:]:
        keep = set(other)
        values = [v for v in values if v in keep]
    if not values:
        raise RuntimeError("no version common to " + ", ".join(froms))
    return values


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------

@dataclass(eq=False)     # hashed by identity: headers are used as dict keys
class Header:
    path: Path
    name: str
    source: str
    params: dict[str, str]

    @property
    def rel(self) -> str:
        return self.path.relative_to(ROOT).as_posix()

    def key(self) -> tuple:
        """Identity for fetch dedup: same upstream, ignoring value filters."""
        items = sorted((k, v) for k, v in self.params.items()
                       if k not in _NON_IDENTITY)
        return (self.source, tuple(items))

    def __str__(self) -> str:
        return f"{self.rel}: {self.name}"


def resolve_regex(pattern: str) -> str:
    """Expand an `@name` shortcut. Raises on one that does not exist."""
    if not pattern.startswith("@"):
        return pattern
    if pattern not in PATTERNS:
        raise ValueError(f"unknown pattern {pattern} "
                         f"(have {', '.join(sorted(PATTERNS))})")
    return PATTERNS[pattern]


def parse_params(rest: str, source: str = "") -> dict[str, str]:
    """`[<primary>] k=v k=v` — values may not contain spaces unless quoted.

    The first token is the source's primary parameter when it is not itself a
    `key=value`. That test rather than plain position, so a URL carrying a query
    string (`?list-type=2`) is not mistaken for one.
    """
    def unquote(v: str) -> str:
        # Only a matched surrounding pair. `.strip('"')` would also eat the
        # closing quote of a value that legitimately ends in one.
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            return v[1:-1]
        return v

    params = {}
    rest = rest.strip()

    m = re.match(r"""("[^"]*"|'[^']*'|\S+)\s*(.*)$""", rest)
    if m and not re.match(r"^\w+=", m.group(1)):
        key = PRIMARY.get(source)
        if key:
            params[key] = unquote(m.group(1))
        rest = m.group(2)

    for tok in re.findall(r"""(\w+)=("[^"]*"|'[^']*'|\S+)""", rest):
        params[tok[0]] = unquote(tok[1])
    if "regex" in params:
        params["regex"] = resolve_regex(params["regex"])
    return params


def header_block(path: Path) -> list[str]:
    """The contiguous header block: after the shebang, up to the first line
    that is neither a header nor blank."""
    out = []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    i = 1 if lines and lines[0].startswith("#!") else 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if not re.match(r"^#[A-Z][A-Z0-9_]*", line):
            break
        out.append(line)
        i += 1
    return out


def strip_note(value: str) -> str:
    idx = value.find("##")
    return value[:idx].strip() if idx >= 0 else value.strip()


def read_headers(path: Path) -> list[Header]:
    """#AUTOUPDATE: lines from the contiguous header block only."""
    out = []
    for line in header_block(path):
        if not line.startswith("#AUTOUPDATE:"):
            continue
        body = line[len("#AUTOUPDATE:"):]
        m = re.match(r"([^:\s]+):(\w+)\s*(.*)$", body)
        if not m:
            print(f"ERROR {path.name}: malformed #AUTOUPDATE: {line}", file=sys.stderr)
            continue
        try:
            params = parse_params(m.group(3), m.group(2))
        except ValueError as e:
            print(f"ERROR {path.name}: {e}", file=sys.stderr)
            continue
        out.append(Header(path, m.group(1), m.group(2), params))
    return out


# ---------------------------------------------------------------------------
# Writing — the only place a recipe is modified
# ---------------------------------------------------------------------------

@dataclass
class Result:
    status: str                     # "update" | "skip" | "fail"
    message: str
    text: str | None = None         # the rewritten recipe, when status is update


# A literal shell assignment, which is how a recipe pins a component version.
# Anything with a `$` or a command substitution on the right is skipped: this
# resolves a pin, it does not pretend to be a shell.
_ASSIGN_RE = re.compile(r'^\s*([A-Za-z_]\w*)="([^"$`]*)"\s*$', re.M)
_VAR_RE = re.compile(r'\$\{([A-Za-z_]\w*)\}|\$([A-Za-z_]\w*)')


def expand_vars(text: str) -> str:
    """Substitute `VAR="literal"` assignments. Unknown variables are left."""
    env = dict(_ASSIGN_RE.findall(text))
    return _VAR_RE.sub(
        lambda m: env.get(m.group(1) or m.group(2), m.group(0)), text)


def checkable_urls(text: str) -> list[str]:
    """URLs with nothing left to substitute, so they can actually be fetched.

    An `${ARCH}` the recipe never assigns cannot be guessed, so its URL is not
    checked. Branching the architecture into one literal URL per arch is what
    makes a recipe verifiable — and checks the arch that lags, not just amd64.
    """
    return [u for u in re.findall(r'https?://[^\s"\'\\]+', expand_vars(text))
            if "{" not in u and "$" not in u]


def downloadable(text: str, token: str, version: str) -> bool:
    """Whether every URL in the recipe resolves for this candidate version.

    A release is announced before all of its artifacts land — RStudio publishes
    arm64 to S3 after the fact — and a version in the list before its file
    exists is a build that fails for whoever picks it.
    """
    filled = text.replace("{" + token + "}", version)
    # Only the URLs this version appears in. An unrelated host being down for a
    # minute would otherwise drop a good version, and nothing would say why.
    return all(head_ok(u) for u in checkable_urls(filled) if version in u)


def write_pl(h: Header, text: str, versions: list[str]) -> Result:
    """Rewrite a #PH: list, extending never replacing."""
    rx = re.compile(rf"^#PH:{re.escape(h.name)}:(.*)$", re.M)
    m = rx.search(text)
    if m is None:
        return Result("fail", f"no #PH:{h.name}: line")
    current, sep = decode_values(m.group(1))
    keep = [v for v in current if v != "*"]

    missing = [v for v in keep if v not in versions]
    if missing:
        return Result("fail", f"upstream lost {len(missing)} value(s) "
                              f"({', '.join(missing[:3])}…) — refusing to replace")

    fresh = [v for v in versions if v not in keep]
    if keep and fresh:
        problem = suspicious(keep, fresh, sep)
        if problem:
            return Result("fail", problem)

    if h.params.get("verify") == "head" and fresh:
        fresh = [v for v in fresh if downloadable(text, h.name, v)]
        versions = keep + fresh
        if not fresh:
            return Result("skip", "up to date (new versions not published yet)")

    if sep == ",":
        merged = sorted(set(keep) | set(versions), key=natural, reverse=True)
    else:
        # author-ordered: keep it, append what is new
        merged = keep + [v for v in versions if v not in keep]
    if "*" in current:
        merged.append("*")

    new_line = f"#PH:{h.name}:{encode_values(merged, sep)}"
    if new_line == m.group(0):
        return Result("skip", "up to date")
    # A line can change without gaining a value, in two quite different ways,
    # and saying "+" with nothing after it hid both. A changed first value is
    # the one that matters: for a `,` list it is the default, so it changes what
    # gets built. Contiguous integers collapsing to a range changes nothing.
    added = [v for v in merged if v not in current]
    if added:
        what = f"+{', '.join(added)}"
    elif current and current[0] != merged[0]:
        what = f"reordered, default {current[0]} -> {merged[0]}"
    else:
        what = f"recompacted to {encode_values(merged, sep)}"
    return Result("update", what,
                  text[:m.start()] + new_line + text[m.end():])


def write_dep(h: Header, text: str, versions: list[str]) -> Result:
    """Bump a #DEP:'s preferred version. The >=min floor is the author's."""
    rx = re.compile(rf"^(#DEP:{re.escape(h.name)}/)([^\s>=<#]+)(.*)$", re.M)
    m = rx.search(text)
    if m is None:
        return Result("fail", f"no #DEP:{h.name}/ line")
    current = m.group(2)
    best = max(versions, key=natural)
    if best == current:
        return Result("skip", "up to date")
    if not newer(best, current):
        return Result("fail", f"upstream {best} is older than pinned {current}")
    new_line = f"{m.group(1)}{best}{m.group(3)}"
    return Result("update", f"{current} -> {best}",
                  text[:m.start()] + new_line + text[m.end():])


def pin_re(var: str) -> re.Pattern:
    """Match `VAR="value"`, capturing the value in group 2."""
    return re.compile(rf'^([ \t]*{re.escape(var)}=")([^"]*)(")', re.M)


def write_pin(h: Header, text: str, versions: list[str]) -> Result:
    """Rewrite the `VAR="version"` assignment named by `pin=` (plan §3.5).

    `pin=` names a shell variable, not a pattern. A pattern aimed at the URLs
    looked like it let one header serve every architecture, but one that caught
    `tool_3.3_` and missed `/download/3.3/` half-rewrote the URL, and the
    agreement check below could not see it — only one occurrence had matched.
    One assignment, interpolated everywhere, makes that unrepresentable.
    """
    var = h.params.get("pin")
    if not var:
        return Result("fail", "a body pin needs pin=<VAR_NAME>")
    rx = pin_re(var)
    matches = list(rx.finditer(text))
    if not matches:
        return Result("fail", f'no {var}="…" assignment in {h.path.name}')
    found = {m.group(2) for m in matches}
    if len(found) > 1:
        return Result("fail", f"{var} is assigned {sorted(found)} — "
                              f"a pin has one value")

    current = found.pop()
    best = max(versions, key=natural)
    if best == current:
        return Result("skip", "up to date")
    if not newer(best, current):
        return Result("fail", f"upstream {best} is older than pinned {current}")

    new_text = rx.sub(lambda m: m.group(1) + best + m.group(3), text)

    # The URLs interpolate the variable, so this checks what the recipe will
    # actually download — including every branched architecture, which is the
    # point: arm64 is the one published late.
    if h.params.get("verify") == "head":
        bad = [u for u in checkable_urls(new_text)
               if best in u and not head_ok(u)]
        if bad:
            return Result("fail", f"{best} not published yet: {bad[0]}")

    return Result("update", f"{current} -> {best}", new_text)


def apply_header(h: Header, versions: list[str], dry_run: bool) -> Result:
    text = h.path.read_text(encoding="utf-8")

    if re.search(rf"^#PH:{re.escape(h.name)}:", text, re.M):
        result = write_pl(h, text, versions)
    elif re.search(rf"^#DEP:{re.escape(h.name)}/", text, re.M):
        result = write_dep(h, text, versions)
    else:
        result = write_pin(h, text, versions)

    if result.text is not None and not dry_run:
        h.path.write_text(result.text, encoding="utf-8")
    return result


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def filtered(versions: list[str], params: dict[str, str]) -> list[str]:
    out, seen = [], set()
    for v in versions:
        if v in seen:
            continue
        if "min" in params and natural(v) < natural(params["min"]):
            continue
        if "max" in params and natural(v) > natural(params["max"]):
            continue
        seen.add(v)
        out.append(v)
    return out


def discover(h: Header, cache: dict) -> list[str]:
    ck = h.key()
    if ck not in cache:
        if h.source == "cmd":
            cache[ck] = src_cmd(h.params, ROOT / "scripts" / "sources")
        elif h.source in SOURCES:
            cache[ck] = SOURCES[h.source](h.params)
        else:
            raise RuntimeError(f"unknown source {h.source!r}")
    return cache[ck]


def process(h: Header, cache: dict, namespaces: dict[str, list[str]] | None,
            dry_run: bool) -> Result:
    try:
        if h.source == "dep":
            # Never cached: the collection changes as this run writes to it.
            if namespaces is None:
                raise RuntimeError("dep source needs the collection scanned first")
            versions = src_dep(h.params, namespaces)
        else:
            versions = discover(h, cache)
        versions = filtered(versions, h.params)
        if not versions:
            raise RuntimeError("no versions matched")
        return apply_header(h, versions, dry_run)
    except Exception as e:                        # one bad source is not fatal
        return Result("fail", str(e))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change without writing")
    ap.add_argument("--only", metavar="NAME",
                    help="limit to headers whose name or recipe path contains NAME")
    args = ap.parse_args()

    headers = []
    for path in sorted(RECIPES_DIR.rglob("*")):
        if not path.is_file() or path.suffix in SKIP_SUFFIXES:
            continue
        headers += read_headers(path)

    if args.only:
        headers = [h for h in headers if args.only in h.name or args.only in h.rel]

    cache, status = {}, {}

    # Upstreams first, then derived placeholders — a `dep` source reads what
    # the collection now publishes, so its providers must already be current.
    upstream = [h for h in headers if h.source != "dep"]
    derived = [h for h in headers if h.source == "dep"]

    for h in upstream:
        status[h] = process(h, cache, None, args.dry_run)

    # Repeat while anything changed: a derived list may itself feed another.
    for _ in range(MAX_DERIVE_PASSES):
        namespaces = build_namespaces()
        changed = False
        for h in derived:
            result = process(h, cache, namespaces, args.dry_run)
            if result.status == "update":
                changed = True
            # a header that updated in an earlier pass still counts as updated
            if not (status.get(h) and status[h].status == "update"
                    and result.status == "skip"):
                status[h] = result
        if not changed or args.dry_run:
            break
    else:
        print("WARN: derivation did not settle — check for a cycle in from=",
              file=sys.stderr)

    counts = {"update": 0, "skip": 0, "fail": 0}
    for h in headers:
        result = status[h]
        counts[result.status] += 1
        tag = {"update": "UPDATED", "skip": "SKIP", "fail": "FAIL"}[result.status]
        stream = sys.stderr if result.status == "fail" else sys.stdout
        prefix = "[DRY] " if args.dry_run and result.status == "update" else ""
        print(f"{prefix}[{tag}] {h}: {result.message}", file=stream)

    print(f"\n{len(headers)} headers, {len(cache)} fetches: "
          f"{counts['update']} updated, {counts['skip']} current, "
          f"{counts['fail']} failed")

    # Fail only when nothing worked — at this scale one bad host must not
    # discard every other result (plan §6).
    return 1 if headers and counts["fail"] == len(headers) else 0


if __name__ == "__main__":
    sys.exit(main())
