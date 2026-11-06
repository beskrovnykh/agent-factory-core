---
name: code-review
description: Code review — bugs, logic errors, hardcodes, magic numbers, duplication, dead branches, style. Ревью кода, код-ревью. Use when asked to look over CODE — a diff, a branch, changed or uncommitted files, a function, a file — for defects or quality ("review my uncommitted changes", "check this function for logic errors and hardcoded values", "посмотри код на баги", "оцени качество кода: дублирование, мёртвые ветки, стиль"), or before a merge or deploy. Do NOT use when the object is not code (a PR description, wording) or when the ask is a different job: debugging failing tests, reading prod logs, explaining architecture, upgrading dependencies.
allowed-tools: Read, Grep, Glob, Bash, Task
user-invocable: true
---

# Code Review Skill

Review changed or specified files. Priority: bugs first, then quality.

## Input

`$ARGUMENTS` — file paths, directory, or empty (= review uncommitted changes via `git diff`).

## What to find (ordered by severity)

### 1. LOGIC — Wrong behavior
Code does something different from what the author intended.
- Wrong comparison: `>=` vs `<=`, `>` vs `>=`, off-by-one
- Wrong sign: PnL, direction (LONG/SHORT swap), price delta
- Wrong variable: using `entry_price` where `exit_price` was meant
- Wrong order: checks that should be A-then-B but are B-then-A
- Inverted condition: `if allowed` where `if not allowed` was meant
- Division/multiplication error: `* 100` missing or doubled, `pct / 100` forgotten
- Wrong default: fallback value that silently produces incorrect results

### 2. HARDCODE — Values that shouldn't be literals
Hardcoded values that will break when context changes.
- Magic numbers: `0.02`, `48`, `20.0` that should be from config/shared constants
- Hardcoded paths, URLs, ports, IPs
- Hardcoded timeouts, retry counts, thresholds
- Coin-specific values (`'BTC'`, `0.001`) embedded in generic logic
- Values that duplicate a constant defined elsewhere (use the import!)

### 3. SILENT_FAIL — Errors that disappear
Code that swallows errors, making debugging impossible.
- `except Exception: pass` or `except: pass`
- `return None` without logging (caller will get AttributeError later)
- Empty `except` that catches and ignores real bugs
- Missing `else` / missing error branch that silently does nothing
- `or 0` / `or {}` that masks data corruption
- API/DB call that ignores failure status

### 4. STATE — Leaked or corrupted state
State not cleaned up, leading to phantom positions or stuck resources.
- Margin reserved but never released on error path
- Position opened in tracker but not in DB (or vice versa)
- Pending entry/exit not cleared after timeout/error
- Set/dict entry added but never removed
- Lock acquired but not released in exception path
- Race condition: check-then-act without lock

### 5. PARITY — Backtest vs live divergence
Logic that differs between backtest and live, causing false confidence.
- Different formula for same calculation
- Different order of exit checks (disaster vs trail)
- Different default values or constants
- Condition present in one but missing in the other
- Different rounding or precision

### 6. CONTRACT — Interface mismatch
Function returns or expects something different from what callers assume.
- Returns `str` but caller treats as `float`
- Returns `None` on error but caller doesn't check
- Dict key present in one path but missing in another
- Async function called without `await`
- Changed function signature but not all call sites updated

### 7. COPYPASTE — Duplicated logic
Same algorithm in multiple places — bug fix in one = bug in the other.
- Same calculation in 2+ files (extract to shared)
- Functions that differ by 1-2 lines (parameterize)
- Same validation/parsing repeated

### 8. CONST — Duplicate constants
Same magic number defined in multiple places.
- Constants redefined instead of imported from config/shared
- Default values scattered across dataclasses
- Same threshold in test and production code

### 9. SUBOPTIMAL — Wasteful patterns
Code that works but is unnecessarily complex or fragile.
- `try/except Exception` that catches too broadly
- Mutable default arguments (`def f(x=[])`)
- Sync I/O in async context (blocks event loop)
- Re-querying data already available in scope
- N+1 queries / repeated API calls in loop

### 10. STYLE — Convention violations
Project conventions (CLAUDE.md) not followed.
- Missing type annotations (PEP 484 required)
- Magic defaults in dataclasses (should come from Config)
- `# type: ignore` without explanation
- Dead imports, unused variables

## Output format

Group findings by file. For each issue:

```
### <file_path>:<line>

**[CATEGORY]** Short title

\`\`\`python
# current code
\`\`\`

Problem: what's wrong and what can happen.
Fix: what to do (or show fixed code if trivial).
```

## Severity

Rate overall:
- **CRITICAL** — logic bugs, silent failures, state leaks. Stop and fix NOW
- **SERIOUS** — hardcodes, parity issues, contract mismatches. Fix before deploy
- **MODERATE** — duplication, suboptimal patterns. Fix this sprint
- **MINOR** — style only, no risk
- **CLEAN** — no issues found

## Rules

- Bug categories (1-6) are ALWAYS worth reporting
- Quality categories (7-10) — only report if clearly problematic
- Do NOT report issues in files you weren't asked to review
- Do NOT suggest adding docstrings/comments/types to unchanged code
- Do NOT report test files for missing type annotations
- Do NOT invent problems. If it's clean, say CLEAN
- When in doubt about severity, report it — false negative is worse than false positive for bugs
