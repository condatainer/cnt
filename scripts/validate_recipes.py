#!/usr/bin/env python3
"""Check recipes against the rules a reviewer would otherwise have to catch.

Exits non-zero on any error. Warnings are printed but do not fail.

The load-bearing check is that every #PH: reaches the #TARGET:. A placeholder
that changes what is built without changing the module path produces two
different artifacts under one name — the collision the recipe contract exists
to prevent.
"""
import re
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# The header grammar has one definition. Re-parsing it here would be a second
# one to keep in step.
from autoupdate import (COMMON_PARAMS, OPTIONAL, PRIMARY, REQUIRED,  # noqa: E402
                        SOURCES, TAG_SOURCES, checkable_urls, decode_values,
                        natural, parse_params, pin_re)

ROOT = Path(__file__).resolve().parents[1]
RECIPES_DIR = ROOT / "recipes"

SKIP_SUFFIXES = (".py", ".md")
SKIP_NAMES = {"README.md"}

KNOWN_HEADERS = {
    "DESC", "URL", "DEP", "ENV", "TYPE", "ARCH", "PH", "TARGET",
    "INPUT", "SOURCE", "LICENSE", "REDISTRIBUTE", "AUTOUPDATE",
}
SCHEDULER_HEADERS = ("#SBATCH", "#PBS", "#BSUB")
RESERVED_PH = {"prefix"}
# What $CNT_SRC_<name> can be: it is an environment variable.
SOURCE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ARCH_VALUES = ("native", "noarch")
REDISTRIBUTE_VALUES = ("yes", "no")
# Fetched with urllib, so the scheme must be one it speaks. `git` is excluded:
# ls-remote also takes git://, ssh:// and scp-style user@host:path.
URL_SOURCES = {"listing", "json", "xml"}
TOKEN_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

errors: list[str] = []
warnings: list[str] = []


def err(rel: str, msg: str) -> None:
    errors.append(f"{rel}: {msg}")


def warn(rel: str, msg: str) -> None:
    warnings.append(f"{rel}: {msg}")


def split_recipe(path: Path) -> tuple[list[str], list[str]]:
    """Return (header_lines, body_lines).

    The header block is contiguous, starts after the shebang, and ends at the
    first line that is neither a header nor blank.
    """
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    headers, i = [], 0
    if lines and lines[0].startswith("#!"):
        i = 1
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if re.match(r"^#[A-Z][A-Z0-9_]*", line):
            headers.append(line)
            i += 1
            continue
        break
    return headers, lines[i:]


def header_key(line: str) -> str:
    m = re.match(r"^#([A-Z][A-Z0-9_]*)", line)
    return m.group(1) if m else ""


def strip_note(value: str) -> str:
    idx = value.find("##")
    return value[:idx].strip() if idx >= 0 else value.strip()


def anchored(pattern: str) -> bool:
    return pattern.startswith("^") and pattern.endswith("$")


def check_updates(rel: str, updates: list, path: Path, deps: list[str],
                  pl_names: list[str], pl_sep: dict[str, str],
                  pl_values: dict[str, list[str]]) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    module = rel[len("recipes/"):].removesuffix(".def")

    seen: set[str] = set()
    for name, source, params in updates:
        where = f"#AUTOUPDATE:{name}"

        if name in seen:
            err(rel, f"{where} appears twice — a key has one owner")
        seen.add(name)

        missing = REQUIRED.get(source, set()) - set(params)
        if missing:
            hint = PRIMARY.get(source, "")
            err(rel, f"{where} ({source}) needs {', '.join(sorted(missing))}"
                     + (f"; {hint}= may be written bare as the first token"
                        if hint in missing else ""))

        allowed = (REQUIRED.get(source, set()) | OPTIONAL.get(source, set())
                   | COMMON_PARAMS)
        for extra in sorted(set(params) - allowed):
            warn(rel, f"{where} ({source}) ignores unknown parameter {extra}=")

        # Shape only, never reachability: the validator must not need a network,
        # or a transient upstream outage fails an unrelated pull request. A
        # typo'd scheme is still worth catching here — at run time it costs a
        # day and an issue.
        if source in URL_SOURCES and "url" in params:
            parts = urllib.parse.urlsplit(params["url"])
            if parts.scheme not in ("http", "https") or not parts.netloc:
                err(rel, f"{where} url is not http(s) with a host: "
                         f"{params['url']}")

        if source == "conda" and "::" in params.get("pkg", "::"):
            pass
        elif source == "conda":
            err(rel, f"{where} pkg must be channel::name — the channel is "
                     f"required, never defaulted")

        pattern = params.get("regex")
        if pattern is not None:
            try:
                rx = re.compile(pattern)
            except re.error as e:
                err(rel, f"{where} regex does not compile: {e}")
                rx = None
            if rx is not None:
                if rx.groups == 0 and not anchored(pattern):
                    err(rel, f"{where} regex has no capture group and is not "
                             f"anchored, so a substring becomes the version")
                if source in TAG_SOURCES and not anchored(pattern):
                    warn(rel, f"{where} ({source}) matches bare version "
                              f"strings; anchor the regex with ^…$")
        elif source in ("github", "docker"):
            warn(rel, f"{where} ({source}) has no regex — tag namespaces carry "
                      f"latest, nightly and the like")

        # what it rewrites, resolved the same way the updater resolves it
        is_pl = name in pl_names
        is_dep = any(d.startswith(name + "/") for d in deps)
        var = params.get("pin")
        if var and not re.fullmatch(r"[A-Za-z_]\w*", var):
            err(rel, f"{where} pin={var} is not a shell variable name — pin "
                     f"names the variable holding the version, not a pattern")
        elif not is_pl and not is_dep:
            if var is None:
                err(rel, f"{where} matches no #PH: or #DEP:, so it is a body "
                         f"pin and needs pin=<VAR_NAME>")
            elif not pin_re(var).search(text):
                err(rel, f'{where} has no {var}="…" assignment in this recipe')
        elif var:
            err(rel, f"{where} sets pin= but {name} is a "
                     f"{'#PH:' if is_pl else '#DEP:'}, which is rewritten by "
                     f"name — pin= would do nothing")

        if params.get("verify") and is_dep:
            err(rel, f"{where} verify= does nothing on a #DEP: — there is no "
                     f"URL to check")
        elif params.get("verify"):
            # An unresolvable URL is skipped rather than failed, so a recipe
            # whose every URL interpolates ${ARCH} verifies nothing and reports
            # success. Silent, and exactly what verify= is there to prevent.
            sample = (pl_values.get(name) or ["0"])[0]
            if not checkable_urls(text.replace("{" + name + "}", sample)):
                err(rel, f"{where} verify= cannot resolve a single URL — "
                         f"branch the architecture into literal URLs instead "
                         f"of interpolating it")

        if is_pl:
            if pl_sep.get(name) == "|" and (params.get("min") or params.get("max")):
                err(rel, f"{where} has min=/max= on a | list, which is "
                         f"unordered — there is nothing to bound")
            # A bound filters the *source*, so a listed value outside it is one
            # the source will never return again — which the updater reads as an
            # upstream deletion and refuses, on every run from then on. Pruning
            # means moving the bound and the list together.
            for bound, keep in (("min", lambda v, b: natural(v) >= natural(b)),
                                ("max", lambda v, b: natural(v) <= natural(b))):
                b = params.get(bound)
                listed = pl_values.get(name) or []
                cut = [v for v in listed if not keep(v, b)] if b else []
                if cut and len(cut) == len(listed):
                    err(rel, f"{where} {bound}={b} excludes every value the "
                             f"recipe lists, so every run reports a loss")
                elif cut:
                    err(rel, f"{where} {bound}={b} excludes "
                             f"{', '.join(cut[:3])}{'…' if len(cut) > 3 else ''} "
                             f"from the #PH: list — the next run reads that as "
                             f"an upstream deletion and refuses; move the bound "
                             f"and the list together")

        if source == "dep":
            for f in params.get("from", "").split(","):
                f = f.strip()
                if not f:
                    continue
                if f == module or module.startswith(f + "/"):
                    err(rel, f"{where} derives from {f}, which is itself")
                    continue
                matching = [d for d in deps if d == f or d.startswith(f + "/")]
                if not matching:
                    err(rel, f"{where} derives from {f}, "
                             f"which is not a #DEP: of this recipe")
                elif not any("{" + name + "}" in d for d in matching):
                    err(rel, f"{where} derives from {f}, but its #DEP: pins a "
                             f"version instead of using {{{name}}}")


def check(path: Path, seen_targets: dict[str, str]) -> None:
    rel = path.relative_to(ROOT).as_posix()
    headers, body = split_recipe(path)

    pl_names: list[str] = []
    target = description = ""
    pl_open: dict[str, bool] = {}
    pl_sep: dict[str, str] = {}
    pl_values: dict[str, list[str]] = {}
    schedulers: set[str] = set()
    deps: list[str] = []
    updates: list[tuple[str, str, dict[str, str]]] = []
    sources: list[tuple[str, str]] = []    # (name, url or "ask:<prompt>")

    for line in headers:
        key = header_key(line)
        if f"#{key}" in SCHEDULER_HEADERS:
            schedulers.add(key)
            continue
        if key not in KNOWN_HEADERS:
            err(rel, f"unknown header #{key}:")
            continue
        value = strip_note(line.split(":", 1)[1]) if ":" in line else ""
        if key == "PH":
            name = value.split(":", 1)[0].strip() if ":" in value else ""
            if not name:
                err(rel, f"malformed #PH: line: {line}")
            elif name in RESERVED_PH:
                err(rel, f"#PH:{name} is reserved — {{{name}}} is substituted "
                         f"when the artifact is loaded, not at build time")
            else:
                pl_names.append(name)
                values = value.split(":", 1)[1] if ":" in value else ""
                if "," in values and "|" in values:
                    err(rel, f"#PH:{name} mixes , and | — the separator picks "
                             f"the ordering, so a list may use only one")
                # The updater's decoder, so `40-49` is the 10 versions it means
                # rather than one odd string. A second implementation here drifts.
                items, sep = decode_values(values)
                pl_open[name] = "*" in items
                pl_sep[name] = sep
                pl_values[name] = [v for v in items if v != "*"]
        elif key == "TARGET":
            target = target or value
        elif key == "DESC":
            description = description or value
        elif key == "DEP":
            deps.append(value)
        elif key == "AUTOUPDATE":
            m = re.match(r"([^:\s]+):(\w+)\s*(.*)$", value)
            if not m:
                err(rel, f"malformed #AUTOUPDATE: {line}")
                continue
            name, source = m.group(1), m.group(2)
            if source not in SOURCES and source not in ("cmd", "dep"):
                err(rel, f"#AUTOUPDATE:{name} has unknown source {source!r}")
                continue
            try:
                updates.append((name, source, parse_params(m.group(3), source)))
            except ValueError as e:
                err(rel, f"#AUTOUPDATE:{name}: {e}")
        elif key == "INPUT":
            if path.suffix == ".def":
                err(rel, "#INPUT: on a .def — only a script recipe reads "
                         "answers; a definition is built by apptainer")
            elif not value:
                err(rel, "#INPUT: needs a prompt")
        elif key == "SOURCE":
            if path.suffix == ".def":
                err(rel, "#SOURCE: on a .def — only a script recipe fetches "
                         "sources; a definition is built by apptainer")
                continue
            parts = value.split(None, 1)
            name, rest = (parts + [""])[:2]
            if not SOURCE_NAME_RE.match(name) or not rest:
                err(rel, f"malformed #SOURCE: {line} — the form is a name "
                         f"then one URL, or ask: and a prompt; the name "
                         f"holds only letters, digits and underscore")
            elif rest.startswith("ask:"):
                if not rest[len("ask:"):].strip():
                    err(rel, f"#SOURCE:{name} ask: needs a prompt")
                else:
                    sources.append((name, rest))
            elif re.search(r"\s", rest):
                err(rel, f"#SOURCE:{name} takes one URL; mirrors are not supported")
            else:
                parts_url = urllib.parse.urlsplit(rest)
                if parts_url.scheme not in ("http", "https") or not parts_url.netloc:
                    err(rel, f"#SOURCE:{name} url is not http(s) with a host: "
                             f"{rest}")
                else:
                    sources.append((name, rest))
        elif key == "TYPE":
            if value.lower() not in ("app", "data"):
                err(rel, f"#TYPE: must be app or data, got: {value}")
            elif path.suffix == ".def":
                err(rel, "#TYPE: on a .def — base/os come from the path")
        elif key == "ARCH":
            if path.suffix == ".def":
                err(rel, "#ARCH: on a .def — an os and a base are root "
                         "filesystems and are always architecture-specific")
            elif value.lower() not in ARCH_VALUES:
                err(rel, f"#ARCH: must be {' or '.join(ARCH_VALUES)}, "
                         f"got: {value}")
        elif key == "REDISTRIBUTE":
            if value.lower() not in REDISTRIBUTE_VALUES:
                err(rel, f"#REDISTRIBUTE: must be "
                         f"{' or '.join(REDISTRIBUTE_VALUES)}, got: {value}")

    if not description:
        err(rel, "missing #DESC:")

    # --- the invariant: placeholders and the target agree both ways ---
    target_tokens = set(TOKEN_RE.findall(target))
    if pl_names and not target:
        err(rel, f"#PH: declared ({', '.join(pl_names)}) but no #TARGET:")
    else:
        for name in pl_names:
            if name not in target_tokens:
                err(rel, f"#PH:{name} never appears in #TARGET: — two "
                         f"expansions would build the same module name")
    for token in sorted(target_tokens - set(pl_names)):
        err(rel, f"#TARGET: uses {{{token}}} with no matching #PH:")

    # --- #SOURCE: names and placeholders ---
    # A name is one input, and a token is substituted from a #PH:. One with no
    # #PH: behind it is left standing in the URL, which then fetches nothing.
    seen_sources: set[str] = set()
    for name, rest in sources:
        if name in seen_sources:
            err(rel, f"#SOURCE:{name} appears twice — one name is one input")
        seen_sources.add(name)
        for token in sorted(set(TOKEN_RE.findall(rest)) - set(pl_names)):
            err(rel, f"#SOURCE:{name} uses {{{token}}} with no matching #PH:")

    # Two open-ended placeholders with no literal between them cannot be matched
    # back apart: nothing says where the first value ends.
    for a, b in re.findall(r"\{([A-Za-z_]\w*)\}\{([A-Za-z_]\w*)\}", target):
        if pl_open.get(a) and pl_open.get(b):
            err(rel, f"#TARGET: has {{{a}}}{{{b}}} adjacent and both open-ended "
                     f"— a concrete name cannot be split back into them")

    # --- module name collisions ---
    module = target or (rel[len("recipes/"):].removesuffix(".def"))
    if module in seen_targets:
        err(rel, f"module {module} already built by {seen_targets[module]}")
    else:
        seen_targets[module] = rel

    # An app with no #REDISTRIBUTE: is deliberately not reported. Undeclared is
    # the safe state — a public registry refuses it — and the only way to clear
    # such a warning is to assert a redistribution right, which is the one
    # direction that cannot be taken back. Push refuses at the moment the
    # question is live, which is where it belongs.

    # --- #AUTOUPDATE: ---
    # Everything here fails at *update* time otherwise, and a failed source does
    # not fail the run, so a broken header silently stops one recipe updating
    # and nobody notices.
    check_updates(rel, updates, path, deps, pl_names, pl_sep, pl_values)

    # --- one scheduler per recipe ---
    # Directives are translated across schedulers, so a recipe declares its
    # resources once. Two families means two answers with nothing to say which
    # is authoritative, and the recipe silently gets different resources
    # depending on where it is built.
    if len(schedulers) > 1:
        err(rel, "mixes " + " and ".join(f"#{s}" for s in sorted(schedulers)) +
                 " — declare resources for one scheduler; they are translated")

    # --- headers below the block are inert and probably a mistake ---
    for line in body:
        key = header_key(line)
        if key and (key in KNOWN_HEADERS or f"#{key}" in SCHEDULER_HEADERS):
            warn(rel, f"#{key} below the header block is ignored: {line.strip()}")


def main() -> int:
    seen_targets: dict[str, str] = {}
    count = 0
    for path in sorted(RECIPES_DIR.rglob("*")):
        if not path.is_file():
            continue
        if path.name in SKIP_NAMES or path.suffix in SKIP_SUFFIXES:
            continue
        check(path, seen_targets)
        count += 1

    for w in warnings:
        print(f"WARN  {w}")
    for e in errors:
        print(f"ERROR {e}")

    print(f"\nChecked {count} recipes: {len(errors)} errors, "
          f"{len(warnings)} warnings")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
