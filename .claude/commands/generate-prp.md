---
description: Generate a PRP (Product Requirements Prompt) from a feature file (typically INITIAL.md). Researches the codebase + docs, then writes PRPs/{feature}.md as an implementation blueprint.
argument-hint: <feature-file>
---

# Create PRP

## Feature file: $ARGUMENTS

Generate a PRP for the feature defined in the given file. The goal is **one-pass
implementation success** — pass the implementing agent everything it needs to self-
validate, but no more.

The implementing agent has access to the codebase and WebSearch. It does NOT see this
conversation. Anything you don't put in the PRP, it doesn't know.

## Research Process

1. **Read the feature file** ($ARGUMENTS) end-to-end.
2. **Codebase analysis** — `src/events/` is the project's reference implementation. Cite
   specific file paths and line ranges from it. Read [CLAUDE.md](../../CLAUDE.md) for
   global rules (response shapes, error codes, redaction, stderr-only logging).
3. **External research** — library docs (mcp, httpx, keyring, pydantic) with section
   anchors. Note version-specific gotchas.
4. **Ask the user** — only if a blocking ambiguity is found that the file doesn't resolve.

## PRP Generation

Use [PRPs/templates/prp_base.md](../../PRPs/templates/prp_base.md) as the template.

### Hard constraints

- **"All Needed Context" section ≤ 200 lines.** Cite files with line ranges; do not paste
  full source.
- **Skip per-task pseudocode** unless a step has a subtle correctness trap worth pinning.
- **No self-rating / confidence score.** Use the "Risks" section instead — list the 1–3
  concrete things most likely to fail and the recovery for each.
- **Validation gates must be executable as written**:

  ```bash
  ruff check src/ tests/ --fix
  ruff format src/ tests/
  mypy src/ tests/
  pytest tests/ -v
  ```

- **MCP Security Checklist must be present** — copy from the template, adapted to the
  feature.

### What every PRP must include

- Clear, ordered task list (what to build first, second, third).
- Cited reference files with line ranges (especially from `src/events/`).
- Standard error codes mapped to any new exception classes (see
  [CLAUDE.md "Standard Error Codes"](../../CLAUDE.md)).
- Integration points: Recorder lifecycle, config loading, env vars introduced.
- Risks section (1–3 items, each with recovery).
- Final checklist + MCP Security Checklist.

## Output

Save as: `PRPs/{feature-name}.md`. Use kebab-case derived from the feature title
(e.g. `phase2-mcp-server-bootstrap.md`).

Print one short summary line when done — just the path and the top risk:

```
Wrote PRPs/phase2-mcp-server-bootstrap.md. Top risk: MCP SDK API surface drift.
```

Do not print the full PRP body to chat — the user will open the file.
