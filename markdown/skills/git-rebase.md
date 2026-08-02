# Git Rebase Skill

You have been given this skill because the user wants to rebase a feature branch onto the
default branch and force-push the result — the "GitLab/GitHub can't rebase this, do it locally"
workflow. Drive the rebase for them. The clean path is fully automated; on a conflict you
**propose** a resolution and **wait for explicit approval** before touching anything.

You are running in `agent` mode with the `shell` tool and the read-only `git_status` /
`git_diff` tools. Every git command runs on the user's local machine where the repo lives.

## Before you start (preconditions)

1. **Working tree must be clean.** Run `git status --porcelain`. If it prints anything, STOP:
   report the dirty files and ask whether to `git stash` first. Never stash, discard, or
   commit the user's work on your own.
2. **Identify the feature branch**: `git rev-parse --abbrev-ref HEAD`. This is the branch you
   will rebase and, at the end, force-push. If it is `master`/`main`, STOP — you don't rebase
   the default branch onto itself.
3. **Identify the default branch**: try `git symbolic-ref refs/remotes/origin/HEAD` and strip
   the `refs/remotes/origin/` prefix; fall back to `master` (this repo's default). Call it
   `<default>` below and `<feature>` for the branch from step 2.

## The rebase sequence

Mirror the commands the user runs by hand:

```bash
git checkout <default>
git pull origin <default>        # must be fast-forward; see note
git checkout <feature>
git rebase origin/<default>
```

Read the `git rebase` output before doing anything else.

- If `git pull` reports anything other than *fast-forward* or *already up to date* (i.e. local
  `<default>` has diverged), STOP and report — do not try to reconcile a diverged local
  default branch.
- Equivalent shortcut that avoids switching branches: `git fetch origin <default>` then
  `git rebase origin/<default>` from `<feature>`. Either is fine; prefer the shortcut if the
  user is already on the feature branch and just wants it rebased.

### Clean path

If the rebase finishes with no conflict (`git status` shows a clean tree, no rebase in
progress), go straight to **Force-push**.

## Conflict protocol — propose, then wait for approval

When the rebase stops on a conflict:

1. List conflicted files: `git diff --name-only --diff-filter=U`.
2. For each conflicted file: read it, show the conflict hunks (the
   `<<<<<<<` / `=======` / `>>>>>>>` blocks), and propose a concrete resolution **with your
   reasoning** — which side to keep, or how to combine them. Base it on what each side
   changed, not a guess.
3. **STOP your turn here.** Present all proposals and ask the user to approve, edit, or reject
   them. Do **not** write files, do **not** `git add`, do **not** `git rebase --continue` yet.
   Leaving the repo mid-rebase is expected and fine.
4. On the next turn, once the user approves: re-run `git status` to confirm the rebase is still
   in progress, apply the approved content to each file, `git add <files>`, then
   `git rebase --continue`.
5. If `--continue` surfaces another conflict, repeat from step 1 (propose and STOP again). When
   the rebase finishes, go to **Force-push**.

Never resolve a conflict and continue without the user's explicit go-ahead. If a resolution is
ambiguous or you're unsure which side is correct, say so and ask — don't pick blindly.

## Force-push

Only after the rebase is fully finished and the working tree is clean:

```bash
git push origin <feature> --force-with-lease
```

- Always `--force-with-lease`. **Never** bare `--force`.
- **Never** force-push `master`, `main`, or a protected/default branch. Only ever the
  `<feature>` branch you just rebased — verify the current branch name matches before pushing.
- `--force-with-lease` is classified as destructive and will trigger a confirmation prompt.
  That prompt is intended — let it happen; it's the user's final gate.

## Hard safety rules

- No `git rebase --skip` (drops a commit) and no `git reset --hard` unless the user explicitly
  tells you to.
- If the repo is in an unexpected state (rebase already in progress at the start, detached
  HEAD, ongoing merge, etc.), STOP and report before doing anything.
- Rollback is always available — offer it whenever something looks wrong:
  - Mid-rebase, to bail out and restore the branch: `git rebase --abort`.
  - To recover from a bad force-push: `git reflog` to find the pre-rebase commit, then
    `git reset --hard <that-sha>` (note the pre-rebase SHA before you start, so you can hand it
    back if needed).

## Response format

Structure each turn as:

1. **Situation** — current branch, default branch, working-tree state.
2. **Plan / commands** — the exact commands you're about to run (or just ran) and their output.
3. **Conflicts** (only when they occur) — per-file proposed resolutions with reasoning, then a
   clear "approve to continue?" ask.
4. **Result** — what happened (rebased cleanly / force-pushed / waiting on you).
5. **Rollback** — the one command to undo, if the user wants out.
