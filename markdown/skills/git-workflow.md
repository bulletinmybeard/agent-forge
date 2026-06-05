# Git Workflow Skill

You have been given this skill because the user's query involves Git operations, pull requests, branch management, or collaborative development workflows. Follow these guidelines when advising on Git-related topics.

## Branch Naming Conventions

Use prefixes to categorize branches:

1. **feature/** — New functionality (e.g.,, `feature/qdrant-integration`, `feature/slack-bot`)
2. **bugfix/** — Bug fixes against develop (e.g.,, `bugfix/embedding-cache-leak`)
3. **hotfix/** — Critical fixes against main (e.g.,, `hotfix/auth-bypass`)
4. **release/** — Release preparation (e.g.,, `release/v1.2.0`)
5. **docs/** — Documentation updates (e.g.,, `docs/api-reference`)
6. **refactor/** — Code improvements without feature changes (e.g.,, `refactor/query-service`)
7. **chore/** — Maintenance tasks (e.g.,, `chore/upgrade-dependencies`)

Keep names lowercase, use hyphens (not underscores), and keep under 50 characters.

## Conventional Commits

Structure commit messages as: `<type>(<scope>): <subject>`

Types:
- **feat** — New feature
- **fix** — Bug fix
- **docs** — Documentation changes
- **style** — Code style (formatting, missing semicolons, etc.)
- **refactor** — Code refactoring without feature/bug changes
- **perf** — Performance improvements
- **test** — Adding or updating tests
- **chore** — Build, dependencies, tooling (no source code change)

Examples:
```
feat(search): add semantic deduplication for chunks
fix(indexer): prevent duplicate embeddings on upsert
docs(api): clarify chunk structure in OpenAPI spec
refactor(memory): consolidate conversation history layers
chore(deps): upgrade qdrant-client to 2.1.0
```

Keep subject under 50 characters, imperative mood (not "adds", use "add").
Include detailed body (separated by blank line) for complex changes, referencing issue numbers:
```
feat(logging): implement structured JSON logs

Switches from unstructured text to JSON format for better parsing.
Includes request ID correlation for distributed tracing.

Fixes #123
Relates to #456
```

## Pull Request Best Practices

1. **Keep PRs small** — Aim for <400 lines changed. Easier to review, faster to merge, simpler to revert.
2. **Link issues** — Add "Fixes #123" or "Closes #456" in PR description for auto-closing.
3. **Draft PRs** — Use draft status for work-in-progress; convert to ready when tests pass.
4. **Descriptive title** — Should be a valid conventional commit (feat, fix, etc.).
5. **Description template** — Include:
   - What changed and why
   - Testing approach (manual steps, test cases added)
   - Screenshots/logs for UI/API changes
   - Breaking changes clearly marked
6. **No force-push after review** — Once reviewers start commenting, avoid force-push unless resolving conflicts.
7. **Address feedback** — Reply to each comment, don't silently dismiss.

## Code Review Workflow

1. **Assign reviewers** — Aim for 2 reviewers minimum on main/develop. Domain experts for complex changes.
2. **Approval criteria**:
   - Code style matches repo conventions
   - Tests added/updated for new code
   - No new linting warnings
   - No security issues (no hardcoded secrets, SQL injection, etc.)
   - Documentation updated if needed
3. **Merge strategies**:
   - **Squash merge** for feature branches (cleaner history, single commit per feature)
   - **Create merge commit** for release branches (preserves history)
   - Avoid fast-forward merges for feature tracking
4. **Delete branch after merge** to keep repo clean.

## Git Operations Reference

**Rebasing (preferred for local branches):**
```bash
git fetch origin develop
git rebase origin/develop              # Rebase current branch
git rebase -i HEAD~3                  # Interactive: squash last 3 commits
git rebase --continue                 # After resolving conflicts
```

**Merging (for shared/public branches):**
```bash
git merge --no-ff origin/develop      # Preserve merge commit history
git merge --squash feature/x           # Combine all commits into one before merge
```

**Cherry-picking (for selective commits):**
```bash
git log --oneline origin/develop      # Find commit hash
git cherry-pick abc1234               # Apply specific commit
git cherry-pick abc1234...def5678     # Range of commits (exclusive of first)
```

**Bisect (for debugging):**
```bash
git bisect start
git bisect bad HEAD                   # Mark current as bad
git bisect good v1.0.0                # Mark known-good commit
# Test the provided commit, then:
git bisect good                       # or: git bisect bad
# Repeat until Git narrows to the culprit
git bisect reset
```

**Stash management:**
```bash
git stash                             # Save uncommitted changes
git stash list                        # View all stashes
git stash apply stash@{0}             # Apply stash (keep it)
git stash pop                         # Apply and delete
git stash drop stash@{0}              # Delete without applying
```

## Conflict Resolution

1. **Merge conflicts** — Edit conflicted files, choose sections between `<<<<<<<` and `>>>>>>>`
2. **Rebase conflicts** — Resolve, then `git add .` and `git rebase --continue`
3. **Ours vs theirs**:
   ```bash
   git checkout --ours config.yaml     # Keep local version
   git checkout --theirs src/app.py    # Take remote version
   ```
4. **View conflict context**:
   ```bash
   git diff --name-only --diff-filter=U  # Conflicted files
   git log --oneline --graph --all      # Visualize branches
   ```

## Release Workflow

1. **Create release branch**:
   ```bash
   git checkout -b release/v1.2.0 develop
   ```
2. **Update version** in code (version file, pyproject.toml, package.json, etc.)
3. **Generate changelog** from commits:
   ```bash
   git log v1.1.0..release/v1.2.0 --oneline
   ```
4. **Merge to main and tag**:
   ```bash
   git checkout main
   git merge --no-ff release/v1.2.0 -m "Release v1.2.0"
   git tag -a v1.2.0 -m "Version 1.2.0"
   git push origin main --tags
   ```
5. **Back-merge to develop**:
   ```bash
   git checkout develop
   git merge --no-ff main
   git push origin develop
   ```

## Response Format

When advising on Git workflows, structure your response as:

1. **Situation** — Summarize the current state (branch, uncommitted changes, etc.)
2. **Issue** — What's problematic and why
3. **Solution** — Step-by-step Git commands with explanations
4. **Verification** — How to confirm the operation succeeded
5. **Rollback** — If something goes wrong, how to undo (use `git reflog` if needed)
