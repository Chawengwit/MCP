# Feature Request

> One feature per file. Run `/generate-prp INITIAL.md` then review the produced PRP and
> run `/execute-prp PRPs/{...}.md`.
>
> **This file is a DELTA**: it does NOT repeat info already in [docs/plan.md](docs/plan.md)
> or [CLAUDE.md](CLAUDE.md). It captures only what's specific or deeper than the roadmap.

---

## STATUS

**All seven planned phases shipped.** No active feature delta in this file.

| Phase | PRP | Status |
|-------|-----|--------|
| 3 — Authentication | [PRPs/phase3-auth.md](PRPs/phase3-auth.md) | ✅ Done — 49 tests, full security checklist verified |
| 4 — API Gateway | [PRPs/phase4-gateway.md](PRPs/phase4-gateway.md) | ✅ Done — 61 tests, response normalization + retry + redacted logging verified |
| 5 — Tools & Integration | [PRPs/phase5-tools.md](PRPs/phase5-tools.md) | ✅ Done — 37 tests, all 5 tools registered, Recorder triple per call, secret redaction in insight events |
| 6 — Testing & Documentation | [PRPs/phase6-testing-docs.md](PRPs/phase6-testing-docs.md) | ✅ Done — 10 new tests (3 subprocess smoke + 7 example-config drift guard), README Quickstart / OAuth / Keyring per OS / Troubleshooting / Logging sections, `MCP_API_CONFIG_PATH` env var |

Phases 1, 2, and 7 (project setup, core MCP server, activity logging) were shipped
ahead of the PRP-based workflow and are tracked in [`docs/plan.md`](docs/plan.md).
Total test count: **232 passing** across `tests/auth/`, `tests/events/`,
`tests/gateway/`, `tests/tools/`, `tests/integration/`, plus the top-level config /
server / example-config tests.

---

## NEXT FEATURE

The MCP Data Gateway is feature-complete relative to the original plan. To start a
new feature:

1. Replace this file's content with a DELTA describing ONE feature
   (FEATURE / ACCEPTANCE CRITERIA / EDGE CASES / OUT OF SCOPE / REFERENCE PATTERNS).
2. Run `/generate-prp INITIAL.md` to produce a PRP under `PRPs/`.
3. Run `/execute-prp PRPs/{feature}.md` to implement + validate.

Future-scope ideas (web UI, multi-tenant credentials, persistent storage, rate
limiting, response caching) live in [`docs/plan.md` § Future Scalability](docs/plan.md).
