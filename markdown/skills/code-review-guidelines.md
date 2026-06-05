# Code Review Guidelines Skill

You have been given this skill because the user's query involves reviewing code, analysing pull requests, or evaluating code quality. Follow this structured review methodology.

## Review Methodology

Analyse code changes in this order of priority:

### 1. Security (Critical)
- **Injection vulnerabilities** — SQL injection, XSS, command injection, SSRF
- **Authentication/Authorisation** — Missing auth checks, privilege escalation paths
- **Secrets exposure** — Hardcoded credentials, API keys, tokens in source
- **Input validation** — Untrusted data flowing to sensitive operations
- **Dependency risks** — Known CVEs in imported packages

### 2. Correctness (Critical)
- **Logic errors** — Off-by-one, wrong operator, inverted conditions
- **Edge cases** — Empty inputs, null values, boundary conditions, concurrent access
- **Error handling** — Uncaught exceptions, missing cleanup, resource leaks
- **Data integrity** — Race conditions, missing transactions, partial updates
- **Contract violations** — API response format changes, breaking schema changes

### 3. Performance (Major)
- **N+1 queries** — Database queries inside loops
- **Missing indexes** — Queries on unindexed columns
- **Memory** — Unbounded collections, large allocations, missing pagination
- **Concurrency** — Thread safety, lock contention, deadlock potential
- **Caching** — Opportunities for memoisation or result caching

### 4. Maintainability (Minor/Nit)
- **Naming** — Clear, consistent variable/function/class names
- **Documentation** — Public APIs have docstrings, complex logic is commented
- **DRY** — Duplicated logic that should be extracted
- **Complexity** — Functions doing too many things, deep nesting
- **Test coverage** — New code has corresponding tests

## Severity Classification

Rate each finding:
- **Critical** — Must fix before merge (security holes, data loss, crashes)
- **Major** — Should fix before merge (bugs, performance issues, missing error handling)
- **Minor** — Nice to fix (readability, minor improvements)
- **Nit** — Stylistic preference (formatting, naming suggestions)

## Response Format

Structure your review as:

```
## Summary
One paragraph overview of the changes and their intent.

## Findings

### Critical
1. [File:Line] Description of issue
   **Fix:** Code suggestion

### Major
1. [File:Line] Description
   **Fix:** Code suggestion

### Minor / Nit
1. [File:Line] Description

## Overall Assessment
- [ ] Ready to merge
- [ ] Needs changes (list blocking items)
- [ ] Needs discussion (architectural concerns)
```

## Guidelines
- Be specific — reference exact file paths and line numbers
- Provide fix suggestions, not just problem descriptions
- Acknowledge good patterns you notice (positive reinforcement)
- Consider backward compatibility for public APIs
- Check that tests cover the new/changed code paths
- Look at the full context, not just the diff
