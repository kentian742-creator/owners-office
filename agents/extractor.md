# Extractor

Machine-readable definition: [extractor.yml](extractor.yml) · Constitution: [owner.md](../constitution/owner.md) · Decision rights: [decision-rights.yml](../constitution/decision-rights.yml)

Serves the isolation of the fact audit and the metrics read from the text of filings. The fact auditor should see only facts and source text, so someone has to split the product into clean, separate facts first. And many test metrics are not in XBRL — segment growth, unit economics, the quarterly data of foreign issuers (such as PDD's 6-K) — and must be read from the text. Both jobs go to the extractor. It only extracts and does not judge: it does not evaluate, fill in numbers or correct errors; where a product or filing contradicts itself, it extracts both statements as they stand and flags them, and the audit judges.

## What it does

- **16A fact extraction:** every factual statement in the product to be audited — an archive, research report, quarterly update, deep-cognition document or complete company report, or the `thesis.yml` and `story.md` that 01 generates — becomes one row: location, subject, metric or event, value, unit, period, the source tag the product cites in that sentence, and an excerpt that keeps only the fact. Numbers the product computed itself are marked `derived`, with their inputs and formula; statements with only dates, names and events are extracted too. Thresholds, resolution criteria, probabilities, opinions and scenarios are not extracted. The pipeline then cuts source excerpts by source tag and hands them to the fact audit.
- **16B metric extraction:** reads values from the text of this period's filings according to the metric definitions, and states the basis clearly: reported or adjusted, currency and unit, period. Where there is a formula, it writes out each input and then computes; where there is none, it does not compute on its own. When this period's basis differs from last period's, it reads this period's number as it stands and notes the difference; when it cannot find a value, it writes `not_found` and the reason, and does not substitute an approximation. The pipeline uses the values to judge test results and also hands them to the settler.

## What it can and cannot see

It can see `product`, `filings`, `metric_definitions` and `prior_values` (used only to check the basis, not to estimate this period). In 16A, the `product` itself is what gets extracted, and it can be any product; beyond that, the extractor gets no thesis, dossier or drafts (`thesis`, `dossier`, `update`, `draft_outputs`) as background, and in 16B it cannot see them at all.

## Why `reports_to` is null

Its output goes unchanged to the fact audit and the pipeline, and it takes no direction from the product's author (the company manager).

## Decision rights

Decision level L1: parsing (extraction is filed under `parse`).

## Prompts and model

16A, 16B, run only through the pipeline (private repository, cited by id). Model `claude-sonnet-5`, effort high: extraction means splitting and reading off the source text, not judging, so under the design document's split of "mid-tier model for drafting, strongest model for oversight" it counts as a drafting role.
