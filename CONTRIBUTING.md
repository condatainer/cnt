# Contributing

## Rules

**If a tool is available through conda, it does not get a recipe.** A recipe 
exists only for what conda cannot provide: an EULA-gated download, a vendor 
tarball, a version conda never packaged, or data.

**One recipe per module, named by its path.** `recipes/cellranger/9.0.1` is the
module `cellranger/9.0.1`. A `.def` builds a container; anything else is a shell
recipe. Do not add a recipe whose `#TARGET:` collides with an existing module.

**A template is named `<name>-template`, and only a template is.** `recipes/code-server-template`
builds `code-server/{version}`, and `code-server` alone resolves to its newest version.
`recipes/grch38/gtf-gencode-template` builds `grch38/gtf/gencode/{gencode_version}`.

**A module name is at most 103 characters as a tag.** Each `/` counts as `--`, and
a template counts the longest value of each placeholder. The registry holds the
whole collection in one repository and files each module under tags of at most
128 characters, the longest being the name plus 25 characters.

**A data recipe declares every tool its script uses as a `#DEP:`**, except the
ones the base image ships: the shell and core utilities, `tar`, `gzip`, `pigz`,
`bzip2`, `xz`, `unzip`, `curl`, and `wget`. An app or OS recipe declares no
`#DEP:`; it brings what it needs.

**A recipe that only downloads declares no resource headers** (`#NCPU:`,
`#MEM:`, `#TIME:`, `#GPU:`). They send the build to a scheduler job on a compute
node, which often has no route to the internet. A recipe that only fetches its
`#SOURCE:` files and unpacks them needs none.

**Every `#PH:` must appear in the `#TARGET:`.** A placeholder that changes what
is built without changing the module path makes two different images claim
one name.

**Never commit `index/`.** It is generated and committed by CI; a second writer
conflicts with it on every pull request. `index/README.md` is the one file in
there that is hand-written, and CI exempts it.

**Validate before pushing:**

```bash
python3 scripts/validate_recipes.py
python3 scripts/format_headers.py --check
```

If the second command lists files, run it again without `--check` to reorder
their headers.
