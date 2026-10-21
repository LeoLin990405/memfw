# Initialize local Git history

This checkout may need a local terminal to create `.git`; the current sandbox
does not permit writing Git metadata. The helper prepares three local commits
and never configures a remote or pushes.

From the project root, inspect the proposed file groups first:

```sh
python scripts/init_local_git.py
```

The default does not change the project. It checks ignore rules in a temporary
repository that is removed on exit, then reports group counts and excluded-path
counts without printing file contents or Git identity values. Review the exclusions
and keep private inputs outside the repository. The helper refuses an existing
`.git`, symlinks in candidate trees, unexpected root paths, or missing Git
identity configuration. It excludes private input patterns from its candidate
lists while retaining the aggregate query-repair status and validation reports.
It does not set `user.name` or `user.email` for you.

After reviewing the dry run and confirming the project files are suitable,
create the local repository and commits with:

```sh
python scripts/init_local_git.py --apply
```

The commits cover package source and build files, tests and experiment
scripts, then documentation and reproducibility evidence. Reports include
Markdown, JSON, and JSONL files; archives, logs, timing outputs, and
`reports/day6_runs/` are excluded. The reviewed aggregate query-repair status
and validation count files are included. `CHANGE_MEMORY.jsonl` and
`SOURCE_MANIFEST.json` are included in the final evidence commit. The helper
uses an explicit file list, honors `.gitignore`, and never force-adds files.
Each commit disables hooks and signing for that Git command only; it does not
change global Git configuration.

If Git setup stops after initialization, the helper leaves the partial local
history visible and reports that condition. It does not reset or delete Git
metadata. Inspect `git status` and the commits before continuing manually.
