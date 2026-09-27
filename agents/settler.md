# Settler

Machine-readable definition: [settler.yml](settler.yml) · Constitution: [owner.md](../constitution/owner.md) · Decision rights: [decision-rights.yml](../constitution/decision-rights.yml)

One of the independent oversight roles; it settles the items in pre-registrations and the say-do ledger that have come due. It is not the author of the pre-registrations, and it cannot see the probabilities of the time, the authors or the owner's overrides, so that a preset answer does not lead it; it settles only against the resolution criteria written in advance, and does not reinterpret them.

## What it does (15B)

- Runs after the earnings event closes and before the company manager drafts the quarterly update: it settles the due pre-registration items of holdings, and the due items in the ledgers of all companies. Items not yet due are not in the input.
- For each pre-registration item it gives happened, not_happened or undetermined, and writes the values and calculation it used and a source excerpt of one sentence at most, and shows how the resolution criterion applies, using nothing but the criterion. If the criterion itself is ambiguous, or the basis has changed so that it cannot be applied as written, it does not pick an interpretation itself: it records undetermined, says where the ambiguity is, and sends it to HQ for a ruling; if HQ cannot rule either, the item stays undetermined and is not scored.
- Ledger: management promises are settled in four grades — kept, partially kept, not kept, silently dropped — and it checks whether a promise not kept was acknowledged unprompted in this period's materials. Forecasts on the system's and the owner's side are recorded only as happened, not happened or undetermined — the pipeline has removed `side`, so it cannot tell who wrote a forecast.
- Settlement records only whether expectations came true, for calibration and candor statistics; it is not used to judge the decisions made at the time (R11). Brier scores are computed by the pipeline.

## What it can and cannot see

It can see `event`, `items_blind` (only the statement, resolution criterion, data source and horizon), `filings`, `metric_values`, and `ledger_due` (with `side` removed and `settle_as` marked). It cannot see `prereg` and `prereg_due` (which carry probabilities and authors; the owner's overrides are a separate file and are not given either), the full `ledger` (with `side`), `thesis`, `dossier` or `update`.

## Why `reports_to` is null

It is also independent oversight. The company manager relays the settlement results and cannot change them; they also go to the pipeline to update the calibration record.

## Decision rights

Decision level L1: rulings against criteria written in advance, filed under `test` (the decision-rights configuration has no separate "settle" action).

## Prompts and model

15B, run only through the pipeline (private repository, cited by id). Strongest model, `claude-fable-5-1`, effort high, falling back under the server's default rules on a refusal.
