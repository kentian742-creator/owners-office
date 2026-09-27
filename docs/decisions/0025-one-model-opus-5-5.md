# 0025 Every role runs on Claude Opus 5.5

> Supersedes the model table of [0003](0003-model-assignment-and-budget.md); the rest of 0003 (refusal handling, price
> table, budget guard) stands, with the downgrade step redefined below.

## Background

Decision 0003 followed the design document: a mid-tier model for drafting (`claude-sonnet-5` for the company manager,
the industry researcher, HQ, the extractor and the typesetter) and the strongest model for oversight
(`claude-fable-5-1` for the eight oversight roles). Two things changed after it:

- **Claude Opus 5.5** became available at $4 / $20 per million input / output tokens, 40% of Fable 5.1's price
  ($10 / $50) and twice Sonnet 5's ($2 / $10), with the same 1M context and 128K output.
- The pipeline now runs by default through Claude Code on the owner's subscription (0022), where cost shows up as plan
  usage rather than dollars, and speed matters for turning a quarter around within seven days.

The split also put the weakest model on the roles whose judgment matters most: the company manager writes and revises
the archives, and HQ writes the rulings and the letters.

On 2026-09-27 all three models were confirmed callable on the owner's plan, and a comparison was run on the fact audit
(04A) through the real pipeline path: 20 AXP facts with verified verdicts (8 wrong, 12 right), about 108k tokens of
identical input for every model, one `complete()` call per run (`work/model-eval/`, outside the repositories):

| Model, effort | Runs | Verdict class right | Errors caught | False alarms | Valid on the first try | Time, all runs | Notional cost, all runs |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `claude-opus-5-5`, high | 2 | 35 of 40 | 13 of 16 | 2 of 24 | 2 of 2 | 10.4 min | $2.98 |
| `claude-fable-5-1`, high | 2 | 36 of 40 | 12 of 16 | 0 of 24 | 1 of 2 | 14.4 min | $9.40 |
| `claude-sonnet-5`, high | 1 | 15 of 20 | 4 of 8 | 1 of 12 | 1 of 1 | 4.9 min | $0.73 |
| `claude-opus-5-5`, xhigh (128k output limit) | 1 | 18 of 20 | 7 of 8 | 1 of 12 | 1 of 1 | 8.7 min | $2.06 |

Opus 5.5's two false alarms were the same fact, a precision point (the ~$230 million settlement also covers the Federal
Reserve), not a wrong number. At effort xhigh with the pipeline's default 64k output limit, two runs failed after about
ten minutes; the pipeline misread the CLI's continuation as tool use (being fixed). The comparison is small (20 facts,
one company, one or two runs per model) and covers only the fact audit.

The fact audits of the AXP, MSFT and PDD archives (2026-09-25 to 09-27), which found every error they reported with
primary evidence, were also run on Opus 5.5.

## Options

1. Keep 0003: Sonnet 5 for drafting, Fable 5.1 for oversight.
2. Opus 5.5 for drafting, Fable 5.1 for oversight.
3. Opus 5.5 for every role.

## Decision

Option 3, decided by the owner on 2026-09-27 ("Opus 5.5 is the best choice today; there is no need for Sonnet or
Fable").

- Every role in `agents/*.yml` uses `claude-opus-5-5` at effort `high`, with `fallbacks: default` (on the API backend, a
  refusal falls back under the server's default rules, and `pipeline/llm.py` records the model that answered).
  `pipeline/llm.py` supports the model since e0b458c (prices, cache rates, adaptive thinking, server fallback); thinking
  cannot be turned off on this model, and the pipeline always sends `effort` explicitly, because the model's own default
  is `medium`.
- The budget's second degrade step (`downgrade_drafting_model` in `decision-rights.yml`) now means lowering the drafting
  roles' effort from `high` to `medium` on the same model. It applies only to calls billed to the API; oversight roles
  are never downgraded.
- Effort per role stays `high` until an effort sweep shows where `medium` holds quality (the extractor and the
  typesetter are the first candidates).

## Rationale

- **Quality where it counts.** The company manager and HQ now run on a model at least as strong as the one that audits
  them; the audits of this week, run on Opus 5.5, found real errors with primary evidence.
- **Accuracy, price and speed.** On the comparison Opus 5.5 matched Fable 5.1 (35 vs 36 of 40 verdicts in the right
  class, 13 vs 12 of 16 errors caught) at about a third of the notional cost, faster and valid on the first try; Sonnet 5
  caught half the errors and is not fit for the audit. `xhigh` scored no better than `high`, so effort stays `high`.
- **One model is simpler to prompt and to maintain.** The prompt set (v3.2) is tuned once; there is one set of model
  behaviours, refusal categories and limits to track.

What is given up: the design document's second line of defense against shared bias, a different and stronger model
for oversight. The oversight roles now stay independent through isolation alone: they never see the inputs or the
conclusions they are not meant to see (00 §G6, `cannot_see`), and every call is a separate request with no shared
context. If the calibration record or the audits show errors that both drafter and auditor miss, the choice is
revisited.

## Rejected alternatives

- **Keep 0003:** the drafting roles stay on the weakest model, and Fable 5.1 costs about 2.5 times as much per token for
  the oversight roles without evidence here that it catches more.
- **Opus 5.5 for drafting, Fable 5.1 for oversight:** keeps the second model as a guard against shared bias, at the
  highest cost; the owner chose one model.

## Date

2026-09-27
