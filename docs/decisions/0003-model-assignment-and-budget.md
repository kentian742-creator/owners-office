# 0003 Model assignment and budget

> **Partly superseded by [0021](0021-model-budget-50.md) (2026-09-25):** the monthly cap is $50, decided by the owner.
> **Partly superseded by [0025](0025-one-model-opus-5-5.md) (2026-09-27):** every role runs on `claude-opus-5-5`; the downgrade step lowers effort instead of changing model. The rest of this record stands.

## Background

DESIGN.md already sets the defaults: a mid-tier model for drafting, the strongest model for audit and blind read; a monthly cap on the model budget, and near the cap, pause candidate companies first, then downgrade the drafting model. Phase 0 has to turn this into concrete models, a price table, a budget guard and a downgrade order, and estimate roughly how much two holdings plus three candidates cost per month. All model calls go through `pipeline/llm.py`, which picks the model from `agents/<role>.yml` and uses the defaults set here when there is no configuration.

## Options

1. The strongest model for every role.
2. A mid-tier model for drafting; the strongest model for audit, blind read and the inversion list (the DESIGN.md plan).
3. A mid-tier model for every role.
4. The second-strongest model for the oversight roles, to save money.

## Decision

Option 2.

| Role | Model | effort | Notes |
| --- | --- | --- | --- |
| Company manager `company_manager`, industry researcher `industry_researcher`, HQ `hq_capital_allocator` | `claude-sonnet-5` | `high` | Drafting, revisions, pre-registrations, letters to the owner |
| Audit `auditor`, blind read `blind_reader`, inversion list `red_team` | `claude-fable-5-1` | `high` | Uses the beta API with the server-side refusal fallback enabled: `betas=["server-side-fallback-2026-07-01"]`, `fallbacks="default"` |

- Thinking: with these models, omitting the `thinking` parameter means adaptive thinking; `budget_tokens` is not sent. Depth is set with `output_config.effort`.
- Refusals: check `stop_reason == "refusal"` before reading the content, raise a clear error and open an issue; an answer taken over by the fallback model is costed at the fallback model's price, the log sets `served_by_fallback` to true, and the PR body must say that the audit was done by the fallback model.
- Price table (USD per million tokens; matches `PRICES_PER_MTOK` in `pipeline/llm.py`; source: [Anthropic model pricing](https://platform.claude.com/docs/en/about-claude/pricing), checked 2026-09-24):

  ```text
  Model               Input   Output
  claude-sonnet-5      2      10
  claude-fable-5-1    10      50
  claude-opus-5        5      25
  claude-opus-4-8      5      25
  claude-haiku-4-5     1       5
  ```

  `pipeline/llm.py` refuses to call a model that is not in the table, because its cost could not be recorded.

- Budget: `budget.monthly_usd` in `constitution/decision-rights.yml`, defaulting to the DESIGN.md cap (`20`). `cost_usd` in `logs/llm-calls.jsonl` is summed per UTC calendar month; if the cap has already been reached before a call, `BudgetExceeded` is raised and the request is not sent. `remaining_budget()` and `budget_status()` give the remaining budget and the current stage.
- Downgrade order (the `stage` of `budget_status()`, by the share of the budget spent this month; the stage names match `budget.degrade_order` in `decision-rights.yml`):

  ```text
  Share     Stage                      Action
  < 0.70    normal                     business as usual
  ≥ 0.70    pause_candidates           pause model calls for candidate companies; their quantitative tests (XBRL, no model) go on as usual
  ≥ 0.85    downgrade_drafting_model   drafting roles switch to claude-haiku-4-5; audit, blind read and inversion list are not downgraded
  ≥ 1.00    stopped                    all model calls stop until next month; failed steps open issues and go into the monthly letter;
                                       quarterly updates fall back to the Phase 1 semi-automatic process and must not stop
  ```

  The audit is never downgraded. Raising the budget is a money matter for the owner to decide; the system only reports usage and reasons in the letter.

- Estimated monthly spend (assumptions, not facts; thinking tokens are priced as output; re-estimate from the actual logs after the first earnings season):

  ```text
  Per-call assumptions (input / output tokens → USD)
    Drafting, claude-sonnet-5
      pre-registration draft        20K / 4K   → 0.08
      quarterly update 03           60K / 10K  → 0.22
      automatic revision 05         30K / 8K   → 0.14
      monthly letter                40K / 6K   → 0.14
      annual deep refresh 06–08, 11 (each)  100K / 15K → 0.35
    Oversight, claude-fable-5-1
      audit 04                      30K / 10K  → 0.80
      blind read                    60K / 10K  → 1.10
      qualitative test judgment     40K / 4K   → 0.60
      inversion list                20K / 6K   → 0.50
      letter audit                  20K / 6K   → 0.50
      monthly random review         60K / 10K  → 1.10
      annual report audits 09, 10, 12, 13 (each)  50K / 10K → 1.00

  Per holding per quarter     0.08 + 0.22 + 0.14 + 0.80 + 1.10 + 0.60 + 0.50 = 3.44
  Per candidate per quarter   0.22 (company manager draft only)
  One round of quarterlies    2 × 3.44 + 3 × 0.22 = 7.54
  Fixed per month             0.14 + 0.50 + 1.10 = 1.74
  Annual report refresh       holdings 4 × 0.35 + 4 × 1.00 = 5.40; candidates 4 × 0.35 = 1.40
  Full year                   4 × 7.54 + 12 × 1.74 + 2 × 5.40 + 3 × 1.40 = 66.04
  Average per month           about 5.5
  Earnings month              7.54 + 1.74 = 9.28
  Worst month                 quarterlies, all annual reports and the fixed items in the same month: 9.28 + 10.80 + 4.20 = 24.28, over the cap, handled by the downgrade order
  Uncertainty                 the oversight roles' thinking tokens are the biggest variable; actual spend may be about twice the assumption
  ```

## Rationale

- The audit is the last line of defense against errors, and DESIGN.md says explicitly that it deserves the best model; the blind read has to answer independently of the drafting model, and a different, stronger model also reduces same-source bias.
- Drafts are checked item by item by the audit, so errors from a mid-tier model get caught; drafting also has the longest inputs, so switching it to a mid-tier model saves the most.
- By the estimate above, ordinary months are far below the cap and earnings months are within it; only months in which annual and quarterly reports coincide reach the cap, and the downgrade order exists for exactly those months.
- Pause the candidates first, then downgrade drafting, and leave the audit alone: this is the order in DESIGN.md. Candidates are only points of comparison, and the audit quality of the holdings must not be cut.

## Rejected alternatives

- **The strongest model everywhere**: drafting has the longest inputs; with the strongest model for everything, earnings months would go over the cap, and the audit is already the safety net.
- **A mid-tier model everywhere**: breaks the DESIGN.md default "the strongest model for audit and blind read", and weakens the independence of the blind read.
- **The second-strongest model for the oversight roles**: saves little money but weakens the last line of defense; the second-strongest model is kept as the target of the refusal fallback.
- **The cheapest model as the drafting default**: too much quality risk for thesis updates; it is only one step in the downgrade order.
- **Counting tokens before each call and blocking on a per-call cost estimate**: one more request and more latency; a monthly cumulative guard is enough at the current scale.

## Date

2026-09-24
