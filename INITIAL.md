# Feature Request

> One feature per file. Run `/generate-prp INITIAL.md` then review the produced PRP and
> run `/execute-prp PRPs/{...}.md`.
>
> **This file is a DELTA**: it does NOT repeat info already in [docs/plan.md](docs/plan.md)
> or [CLAUDE.md](CLAUDE.md). It captures only what's specific or deeper than the roadmap.

---

## STATUS

**Phases 3–6 PRPs already generated and split.** No active feature delta in this file.

| Phase | PRP | Status |
|-------|-----|--------|
| 3 — Authentication | [PRPs/phase3-auth.md](PRPs/phase3-auth.md) | Ready to execute |
| 4 — API Gateway | [PRPs/phase4-gateway.md](PRPs/phase4-gateway.md) | Ready to execute |
| 5 — Tools & Integration | [PRPs/phase5-tools.md](PRPs/phase5-tools.md) | Ready to execute (depends on 3 + 4) |
| 6 — Testing & Documentation | [PRPs/phase6-testing-docs.md](PRPs/phase6-testing-docs.md) | Ready to execute (depends on 3 + 4 + 5) |

To execute a phase: `/execute-prp PRPs/phase3-auth.md` (then 4 → 5 → 6).

---

## NEXT FEATURE

When starting the next feature after Phase 6, replace this file's content with a fresh
DELTA describing ONE feature (FEATURE / ACCEPTANCE CRITERIA / EDGE CASES / OUT OF SCOPE
/ REFERENCE PATTERNS). Then run `/generate-prp INITIAL.md`.
