---
description: Execute a PRP — implements the feature and runs ruff + mypy + pytest until green.
argument-hint: <prp-file>
---

# Execute PRP

## PRP File: $ARGUMENTS

Implement the feature defined in the given PRP. Follow [CLAUDE.md](../../CLAUDE.md) for all
project-wide rules (code style, security, response shapes, error codes, redaction). This
file describes the **process**, not the rules.

## Process

### 1. Load context

- Read the PRP end-to-end.
- Read every file the PRP cites (especially anything under `src/events/` — the project's
  reference implementation).
- Re-read [CLAUDE.md](../../CLAUDE.md) sections relevant to the feature.
- Extend research with WebSearch / additional file reads if context is missing — do NOT
  guess at API behaviour.

### 2. Plan

- Use `TodoWrite` to break work into discrete tasks following the PRP's task list.
- Identify which patterns from `src/events/` to mirror.
- Confirm integration points listed in the PRP (Recorder, redaction helpers, config).
- Verify imports against `requirements.txt` — flag any new dependency before adding.

### 3. Implement

Follow the PRP's task order. For each task, apply the rules in
[CLAUDE.md § Development Conventions](../../CLAUDE.md) and
[CLAUDE.md § Security Rules](../../CLAUDE.md). The PRP's "Known Gotchas" section calls
out anything feature-specific.

### 4. Validate

Run each gate. Fix until green. Never weaken tests or silence warnings to pass.

```bash
ruff check src/ tests/ --fix
ruff format src/ tests/
mypy src/ tests/
pytest tests/ -v          # must include the existing 27 events tests
```

If a gate fails: read the error, find the root cause, fix the code, re-run.
Use `# type: ignore` / `# noqa` only with a one-line comment explaining why.

### 5. Verify the PRP's MCP Security Checklist

Run each grep command in the PRP's MCP Security Checklist. Each item must be confirmed
before marking the feature complete.

### 6. Complete

- All checklist items in the PRP — done.
- All validation gates — green.
- Re-read the PRP one final time and confirm nothing was missed.

If validation fails repeatedly: study the error patterns in the PRP's "Risks" section and
[CLAUDE.md § Common Failure Modes](../../CLAUDE.md) before retrying.
