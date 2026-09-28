# Owner's Office

**One investor's judgment, kept in public and put to the test.**

Owner's Office is the research archive of one private investor, called "the owner" throughout. Each company it follows
has a written thesis and 20 to 30 tests that could prove the thesis wrong. Before each earnings release, forecasts about
the results are written down with probabilities and timestamped; afterwards they are scored against criteria fixed in
advance. Mistakes are published. The research is drafted and audited by AI agents working under the owner's written
investment constitution; the owner decides only where the money goes and whether the constitution changes.

**Status on 2026-09-28:** six companies are archived, and every archive has been checked fact by fact against the
companies' filings; each error found is in [mistakes.md](mistakes.md). Four more (McDonald's, Alphabet, Apple and NVIDIA)
are being built by the pipeline straight from their SEC filings. The first forecasts, on S&P Global's results for the
third quarter of 2026, will be registered by 2026-10-17, so nothing has been scored yet. Progress is tracked in
[docs/STATUS.md](docs/STATUS.md).

> **Not investment advice.** This repository records one private investor's method and forecast record. It recommends
> no security, publishes no valuations or prices, and executes no trades. The owner holds shares of AppLovin, S&P
> Global and McDonald's, and follows other companies as research or for reference; position sizes are never published.

A Chinese version of this page is in [zh-CN/README.md](zh-CN/README.md).

## Why

Investors rarely learn how good their judgment really is. Theses live in notes that get rewritten after the fact,
forecasts are vague enough to survive any outcome, and a good year can hide a bad process. Owner's Office is built to
close those gaps:

| The usual problem | What this repository does instead |
| --- | --- |
| A thesis that cannot be wrong | Each thesis carries tests: numbers that must hold, yes-or-no questions answered by a separate model that sees only the question and the filings, and deadlines by which each part of the thesis must be re-examined. Every threshold is set before the data arrives. |
| Hindsight | Forecasts are committed and timestamped with [OpenTimestamps](https://opentimestamps.org) before results are public. Once a period's results are out, CI refuses any change to that period's thresholds. |
| Numbers nobody checked | Every fact cites a primary document, such as `[src:AXP-10K-FY2025#p45]`, and the linter rejects a number without one. Each archive and each quarterly update is audited fact by fact against the filings before it is merged. |
| Anchoring | A "blind reader" model that never sees the thesis answers the same questions. Where it disagrees with the drafting model is where review time goes. |
| Grading your own homework | Resolved forecasts are scored (Brier score, calibration by domain), so the record, not self-assessment, shows where the owner's judgment can be trusted. |
| Taking management at its word | Management's promises go into a say-do ledger and are settled like the owner's own forecasts. |

## Start here

- **A company:** AppLovin's [two-minute story](companies/APP/story.md), then its full [thesis and tests](companies/APP/thesis.yml).
  The other archives are [S&P Global](companies/SPGI/), [American Express](companies/AXP/),
  [Microsoft](companies/MSFT/), [Berkshire Hathaway](companies/BRK/) and [PDD](companies/PDD/).
- **The rules:** the [investment constitution](constitution/owner.md): fourteen rules and five hard limits, each
  mapped to a check a machine can run.
- **The mistakes:** [mistakes.md](mistakes.md), every error found so far and what changed because of it.
- **The letter:** [the monthly letter to the owner](letters/2026-09.md).
- **Where things stand:** [docs/STATUS.md](docs/STATUS.md) and the [decision records](docs/decisions/). The design is
  in [docs/DESIGN.md](docs/DESIGN.md).

## How it works

1. **Archive.** Each company has a `thesis.yml` (the thesis, its ratings and its tests), a two-minute story, a sources
   table and a say-do ledger. Valuations stay in a private repository and never appear here.
2. **Before results.** The questions for the coming quarter are frozen first; then probabilities are written, merged
   and timestamped before the company reports.
3. **After results.** When the 10-Q, 10-K or 8-K arrives, the quantitative tests are computed from the filing, the
   qualitative tests are judged by an independent model that must quote its evidence, and the quarterly update is
   audited fact by fact before it is merged.
4. **Settlement.** Forecasts are scored against the criteria written in advance, and the letter reports what went
   right, what went wrong and what the system changed.

Thirteen roles do the work: an analyst for each company (the "company manager"), an industry researcher, a
coordinator ("HQ"), an extractor and a typesetter, plus eight independent oversight roles: fact audit, valuation-model
review, red team, synthesis review, design review, blind reader, judge and settler. They all run on Anthropic's Claude
Opus 5.5; what each role may read and what it must never see are set in [agents/](agents/). A role that makes
factual errors loses autonomy until its work needs the owner's review, and a clean record wins it back. Money and
constitutional amendments always go to the owner.

The format, linter and scoring tools are published separately as [thesis-ci](https://github.com/kentian742-creator/thesis-ci),
which works for any archive built the same way.

## Who this is for

- **Investors who keep a written thesis:** [thesis-ci](https://github.com/kentian742-creator/thesis-ci) checks your
  own archive the way it checks this one: tests written before the data, a source for every number, thresholds frozen
  once results are out. `pip install thesis-ci`, then `thesis-ci init`.
- **People building LLM pipelines for research:** the role charters in [agents/](agents/) (what each role may read and
  what it must never see), the [pipeline](pipeline/) (every call recorded with the hashes of its inputs, outputs
  validated before use, oversized steps split into slices), and the [decision records](docs/decisions/) as a design
  log that includes what went wrong.
- **Readers of these companies:** each archive states the thesis, the tests that would prove it wrong, and every error
  the audits found in it.

The prompts are not published yet.

## Repository map

| Path | Contents |
| --- | --- |
| `companies/<ticker>/` | Thesis and tests, two-minute story, sources, say-do ledger, pre-registrations, updates |
| `constitution/` | The investment constitution, how principles from Buffett, Munger and Lynch become system rules, decision rights |
| `agents/` | The thirteen roles: model, inputs, isolation, charter |
| `industries/<id>/` | Industry modules and the signposts they watch |
| `forecasts/`, `letters/`, `mistakes.md` | Forecasts and the owner's overrides, letters to the owner, the mistakes list |
| `docs/` | Design, status, decision records, the acceptance report of each phase |
| `pipeline/`, `scripts/` | The single entry point for model calls, the pipeline runner, the acceptance script |
| `zh-CN/` | Chinese versions of the key documents, at the same relative paths |

A private companion repository holds what must not be public: valuations, decision memos, the decision log with
position sizes, the full research reports and the prompts.

## What it does not do

- No trading signals, no backtests, no share price predictions.
- No daily share prices. Prices exist only as private reference data for valuation; the only price a public file may
  show is a company's own disclosed average buyback price.
- No trade execution. Money is always decided and moved by the owner personally.

## Feedback

A correction of fact backed by a primary source is the most useful contribution: see [CONTRIBUTING.md](CONTRIBUTING.md).
Questions and critique of the method are welcome in
[Discussions](https://github.com/kentian742-creator/owners-office/discussions).

## License

Method documents (`docs/`, `constitution/`, `agents/` and the like) are licensed under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Research content (`companies/`, `industries/`, `forecasts/`,
`letters/`, `mistakes.md`) is all rights reserved. Code (`pipeline/`, `scripts/`, `tests/`, `.github/`) is MIT. See
[LICENSE.md](LICENSE.md).
