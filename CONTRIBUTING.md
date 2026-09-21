# Contributing

## Rules

**If a tool is available through conda, it does not get a recipe.** Conda
already installs it, pins it and records what it resolved to. A recipe exists
only for what conda cannot provide: an EULA-gated download, a vendor tarball, a
version conda never packaged, or data.

**One recipe per module, named by its path.** `recipes/cellranger/9.0.1` is the
module `cellranger/9.0.1`. A `.def` builds a container; anything else is a shell
recipe. Do not add a recipe whose `#TARGET:` collides with an existing module.

**Declare every dependency the base image does not provide.** A tool a script
build uses is a `#DEP:`, except the ones the base image ships: the shell and
core utilities, `tar`, `gzip`, `pigz`, `bzip2`, `xz`, `unzip`, `curl`,and `wget`.

**Do not declare resources on a download-only recipe.** Scheduler directives
send the build to a compute node, which often has no route to the internet.

**Every `#PH:` must appear in the `#TARGET:`.** A placeholder that changes what
is built without changing the module path makes two different images claim
one name.

**Never commit `index/`.** It is generated and committed by CI; a second writer
conflicts with it on every pull request. `index/README.md` is the one file in
there that is hand-written, and CI exempts it.

**Validate before pushing:**

```bash
python3 scripts/validate_recipes.py
```
