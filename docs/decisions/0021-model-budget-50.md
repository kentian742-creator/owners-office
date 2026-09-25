# 0021 Model budget raised to $50 a month

> Supersedes the budget cap in [0003](0003-model-assignment-and-budget.md); the rest of 0003 stands.

## Background

The design document caps model spending at $20 a month, and decision 0003 set that as `budget.monthly_usd` in
`constitution/decision-rights.yml`. Its own cost model put a busy month at about $24, over the cap. Phase 1 adds the
first pre-registrations, quarterly updates with their audits for the holdings and the candidates, and the first
monthly letter. A full quarterly update with its audits costs roughly $5 per company, so two holdings and three
candidates would exceed $20 in November, and the degrade order would pause the candidates first.

Raising the budget is a money matter, decided by the owner (decision level L3).

## Options

1. Keep $20 and pause the candidates' updates when the cap is reached.
2. Raise the cap to $50 a month.

## Decision

Option 2, decided by the owner on 2026-09-25.

- `budget.monthly_usd: 50` in `constitution/decision-rights.yml`. The budget guard in `pipeline/llm.py` reads it; the
  degrade order (pause candidates, then downgrade the drafting model) is unchanged and applies at the new cap.
- The cap covers calls billed to the Anthropic API. Model calls run through Claude Code on the owner's subscription are
  not billed per token; they are logged with their token usage (see decision 0022).
- Phase 1 therefore covers the candidates as the design intended: their quarterly updates are part of acceptance check
  P2.

## Rationale

- The design's own cost model already exceeded $20 in a busy month; the candidates are where the phase 1 track record
  starts.
- $50 leaves room for the audits without touching the oversight models, which the degrade order never downgrades.

## Rejected alternatives

- **Keep $20:** the candidates would be paused in the first earnings season, which defeats the purpose of tracking them.

## Date

2026-09-25
