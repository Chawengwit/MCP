name: "PRP Template — MCP Data Gateway"
description: |
  Context-rich blueprint with executable validation gates. Tuned for Python + MCP server
  work. Optimized for one-pass implementation.

## Principles

1. **Context only as needed** — keep "All Needed Context" ≤ 200 lines. Cite files, don't
   restate them.
2. **Validation gates are executable** — `ruff`, `mypy`, `pytest` commands as written.
3. **Mirror, don't invent** — `src/events/` is the project's reference implementation.
4. **Follow [CLAUDE.md](../../CLAUDE.md)** — global rules take precedence over this template.

---

## Goal

[The end state, in concrete terms.]

## Why

- [Which phase of [docs/plan.md](../../docs/plan.md) this fulfils.]
- [Which downstream features depend on this.]

## What

[User-visible behaviour and technical requirements. List acceptance criteria as
checkboxes.]

### Success Criteria

- [ ] [Specific, measurable, runnable assertion]
- [ ] All existing 27 events tests still pass
- [ ] `ruff check src/ tests/` and `mypy src/ tests/` clean
- [ ] No secrets/tokens in any log line at any level

## All Needed Context

> **Hard cap: this section ≤ 200 lines.** Cite files with line ranges; never paste full
> source. The implementing agent will read referenced files directly.

### Documentation & References

```yaml
- url: [Official docs URL with section anchor]
  why: [Specific section/method needed]
  critical: [Key insight that prevents a common error]

- file: src/events/recorder.py
  why: [Pattern to mirror — name it]
  lines: [Cite specific line ranges, not the whole file]

- doc: CLAUDE.md
  section: ["Response Format Conventions" | "Activity Logging" | etc.]
  critical: [The specific rule that applies]
```

### Current Codebase tree

```
[Output of `tree src/ tests/` filtered to relevant subtrees, ≤ 30 lines]
```

### Desired Codebase tree (files to add/modify)

```
src/<module>/foo.py        — one-line responsibility
tests/<module>/test_foo.py — coverage for foo.py
```

### Known Gotchas (feature-specific)

> Project-wide gotchas live in [CLAUDE.md § Things to Watch For](../../CLAUDE.md). List
> ONLY traps specific to this feature here — library quirks, version edges, race
> conditions, ordering constraints. Cite the file and line where the trap manifests.

```python
# Example:
# CRITICAL: ${VAR} substitution in config loader is single-pass — substituted values
#   are NOT re-scanned. Prevents user data being interpreted as templates.
# CRITICAL: loop.add_signal_handler raises NotImplementedError on Windows — wrap in
#   try/except so dev on Windows still works.
```

## Implementation Blueprint

### Data models

[List Pydantic models / dataclasses to introduce. One-line description each.
Skip pseudocode unless a non-obvious decision (e.g. validator logic) needs pinning.]

### Tasks (in order)

```yaml
Task 1 — [What to build]:
  CREATE [file path]:
    - [Key responsibility, one line]
    - MIRROR pattern from: [src/events/<file>.py:<lines>]
  KEY DECISION: [if any choice needs justification, state it]

Task 2 — [...]:
  ...
```

> Skip per-task pseudocode unless a step has a subtle correctness trap. Cite reference
> files with line ranges instead.

### Integration Points

```yaml
RECORDER:
  source: src.events.Recorder
  calls: record_audit + record_usage + record_insight per tool invocation

CONFIG:
  source: src.config.load_api_configs
  env: [list MCP_* env vars introduced or consumed]

LOGGING:
  destination: stderr only
  level: MCP_LOG_LEVEL env var (default INFO)
```

## Validation Loop

### Level 1 — Lint, format, type

```bash
ruff check src/ tests/ --fix
ruff format src/ tests/
mypy src/ tests/
```

Zero errors. Do not silence with `# type: ignore` / `# noqa` without a one-line comment.

### Level 2 — Unit tests

```bash
pytest tests/<new_module>/ -v   # debug new tests in isolation
pytest tests/ -v                # full suite must pass (incl. 27 existing events tests)
```

Required test categories (adapt names per feature):

- happy path
- invalid input → specific exception class (not bare `Exception`)
- async path with start/stop in `try/finally`
- secret-omission assertion (search serialized output for token/secret/auth keys)

### Level 3 — Smoke test

```bash
# Adapt per feature. Example for server:
echo "" | timeout 2 python -m src.server 2>/tmp/mcp_stderr.log
test -z "$(echo '' | timeout 2 python -m src.server 2>/dev/null)"  # stdout empty
```

## MCP Security Checklist

The implementing agent MUST verify each item before marking the feature complete. Each
check is **grep-able** so it can be re-run on the diff.

- [ ] **No secrets in logs (any level).**
      `grep -riE 'authorization:|bearer |client_secret|access_token|api_key' logs/`
      → must return zero matches with non-`<redacted>` values.
- [ ] **All HTTP traffic logging routes through `src/events/redaction.py`** —
      `redact_headers`, `redact_body`, `redact_url`. No reimplemented redaction.
- [ ] **Credentials read/written ONLY via `keyring`** — no plaintext secret files.
- [ ] **OAuth callback server (if any)** binds to `127.0.0.1` / `localhost` only,
      and lifetime is bounded by the active flow (closes after token exchange).
- [ ] **Tool responses contain zero auth fields.**
      `grep -E '"auth"|"client_id"|"token"|"secret"' <serialized response>` → zero matches.
- [ ] **Pydantic validates every tool input.** No raw `dict[str, Any]` reaches business
      logic without a Pydantic model in between.
- [ ] **Error messages do not leak internals** — no filesystem paths, env var names,
      or stack traces in user-facing error responses.
- [ ] **No new pip dependency added without flagging it explicitly** in the PRP / commit.
- [ ] **Stdout has zero non-protocol output**; logs go to stderr only.

## Risks

> Replace this section with the 1–3 things most likely to go wrong during implementation.
> Be specific: what could fail, where, and what's the recovery.
>
> Example format:
>
> 1. **MCP SDK API surface drifted** — README excerpts may not match installed `mcp` version.
>    *Recovery:* re-verify `Server`, `stdio_server`, `InitializationOptions` against the
>    installed package before writing wiring code.
>
> 2. **mypy rejects third-party imports** — `mcp` package may not ship type stubs.
>    *Recovery:* `# type: ignore[import-not-found]` with a one-line comment, only on the
>    import line.

## Final Checklist

Project-wide style and security rules are enforced by the validation gates and
[CLAUDE.md](../../CLAUDE.md). The list below is the additional **per-feature** verification:

- [ ] `ruff check src/ tests/` clean
- [ ] `ruff format src/ tests/ --check` clean
- [ ] `mypy src/ tests/` clean
- [ ] `pytest tests/ -v` — all pass (including the existing 27 events tests)
- [ ] MCP Security Checklist above — every item verified
- [ ] Smoke test passes (if defined for this feature)
- [ ] Acceptance criteria from "Success Criteria" — all checked
