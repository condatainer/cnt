# Generated — do not edit

`recipes.json` and `helpers.json` are built from `recipes/` and `helpers/` by CI
and committed by it. A hand edit is overwritten by the next run, and a pull
request that touches them is rejected.

They are not comments in the JSON because the files are a flat
`{module: entry}` map, and a key that is not a module would have to be skipped
by every reader.

To see what a change produces:

```bash
python3 scripts/generate_recipe_index.py
python3 scripts/generate_helper_index.py
```

Then drop the result before committing. The recipes are the source of truth; the
index only exists so a client can resolve a name, its dependencies and its
placeholder values without fetching every file.

Update, validate and reindex run in one CI job, because a `#PH:` list is index
content — an update that skipped the regeneration would leave the index offering
versions the recipes no longer have.
