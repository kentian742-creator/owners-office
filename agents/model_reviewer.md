# Model review

Machine-readable definition: [model_reviewer.yml](model_reviewer.yml) · Constitution: [owner.md](../constitution/owner.md) · Decision rights: [decision-rights.yml](../constitution/decision-rights.yml)

One of the independent oversight roles; it reviews only valuations: the private valuation files (`valuation.yml` and `valuation.md`), the valuation section of research reports, and whether they are symmetric with the readings of the other companies in the series. It decides whether a proposed valuation version can take effect.

## What it does (04C)

It checks fifteen items one by one, giving each "compliant / violation / cannot tell" and the basis. The main ones:

- Whether the discount rate is "10-year Treasury yield + a premium specific to this company", and whether the Treasury reading has a date and a source; whether the premium is based on the company's own cash-flow record, or has a personal required return mixed in, is interpolated from other companies or is read off a tier by grade.
- Whether the margin of safety is stated separately, and whether the same quality signal is counted once in the discount rate and again in the margin of safety; whether there is double discounting — listing all the conservative assumptions and estimating their combined effect when multiplied together.
- Whether ranges are given, and whether all the ranges, error bands and sensitivity tables are there; whether only one discount rate is used; whether there is an auditable master table of key assumptions; whether valuations made under the old framework have been recalculated.
- Whether the five-year backtest uses unadjusted year-end closing prices, solves back for the discount rate and compares it with the actual return; whether the two reference anchors are taken from the current versions of their own archives, with a reference date.
- Whether the grade given to the price follows the mechanical scale; whether stock-based compensation is treated as a cost; whether financial companies are treated by the rules; whether the series ranking is carried in full, with a reason on every row; the readings for the management half of the Munger matrix; whether EBITDA is used only as a supporting measure.
- Symmetry: whether the strictness applied to this company is the same as for the other companies in the series.

When it reviews a proposed valuation version, it gives a conclusion. If everything complies (or there are only should-fix items), it approves, the proposed version takes effect, and the pipeline records one line, "valuation recalculation in effect", among the reports in the monthly letter. If there is a must fix, the version goes back, with the conclusion, to the step that produced it (01C, the valuation refresh in 02, or 05).

## What it can and cannot see

It can see `valuation`, `report_valuation_section`, `series_roster`, `price_reference`, `anchors` and `sources`. The isolation table sets no extra limits for it; the parts of 04 cannot see each other's results, which the allow-list ensures.

## Why `reports_to` is null

It is also independent oversight. Valuations affect the ranking and the memos, so whoever reviews them cannot take direction from the drafter or from HQ.

## Decision rights

- Decision level L1: audit.
- Decision level L2: a recalculated valuation takes effect (`valuation_update`). The recalculation itself is the company manager's drafting; the proposed version takes effect only with this role's approval.

## Prompts and model

04C (private repository, cited by id). Strongest model, `claude-fable-5-1`, effort high, falling back under the server's default rules on a refusal.
