# Owner's Office

A Chinese version is in [zh-CN/README.md](zh-CN/README.md).

> **Not investment advice.** This repository documents one private investor's method and track record. It recommends no security, produces no trading signals and executes no trades.

Owner's Office is a one-person "owner's office" run the Berkshire way. Other tools evaluate companies; this one also evaluates the owner: it keeps measuring whether the owner's judgment as a business owner is actually reliable.

- **Thesis as code.** Each company's thesis is maintained like a software project: the archive is the source code, "what would prove me wrong" becomes executable thesis tests, new filings trigger the tests, and every merged pull request is a recorded decision.
- **Write it down first, verify later.** Pre-earnings expectations carry a probability, a resolution criterion and a data source. They are registered and timestamped before the results are first made public, then settled against the criteria written in advance.
- **Two-way accountability.** Management's promises to shareholders and the system's and owner's own forecasts go into the same kind of ledger and are settled the same way.
- **Anti-anchoring.** A "blind reader" model that cannot see the thesis answers independently; where it disagrees with the drafting model is where review time goes.
- **Circle of competence, measured.** Settled forecasts are scored per domain (Brier score, calibration curves), so the circle of competence is drawn by the record rather than by self-assessment.
- **Decentralized autonomy.** Roles operate under a public investment constitution, and their autonomy grows or shrinks with their track record. Only money matters and constitutional amendments go to the owner.

## Repository map

| Repository | Visibility | Contents |
| --- | --- | --- |
| [thesis-ci](https://github.com/kentian742-creator/thesis-ci) | public | Format spec (JSON Schema), linter, pre-registration verification, Brier and calibration tools |
| owners-office (this repo) | public | Investment constitution and the masters' principles, decision rights, agent definitions, per-company thesis.yml and two-minute stories, pre-registrations, forecasts and overrides, say-do ledgers, letters to the owner, mistakes list. Documents are in English; Chinese versions of the key documents are in `zh-CN/` |
| owners-office-private | private | Valuation and price reference data, L3 memos, escalation requests, series ranking, decision log with amounts, full report PDFs, prompts |

## This repository

| Path | Contents |
| --- | --- |
| `docs/DESIGN.md` | Project framework and approach |
| `docs/STATUS.md`, `docs/decisions/` | Progress and decision records |
| `constitution/` | The investment constitution, how the masters' principles become system rules, the three decision levels |
| `agents/` | The thirteen roles (company manager, industry researcher, HQ, extractor and typesetter, plus eight independent oversight roles: fact audit, model review, red team, synthesis review, design review, blind reader, judge and settler), each with its model, what it can see and its prompt numbers |
| `companies/<ticker>/` | thesis.yml, two-minute story, sources table, pre-registrations, say-do ledger, update records |
| `trust/` | Each role's trust level (maintained by the pipeline) |
| `industries/<id>/` | Industry modules and observable signposts |
| `forecasts/`, `letters/`, `mistakes.md` | Forecast and override records, letters to the owner, mistakes list |
| `pipeline/`, `scripts/` | The model-call entry point (the only one), the phase acceptance script |
| `zh-CN/` | Chinese versions of the key documents, at the same relative paths |

## What it does not do

- No trading signals, no backtests, no stock price predictions.
- No daily stock prices; prices exist only as private reference data (the price reference used for valuation, year-end closing prices for checking the valuation model, one alert when a pre-written range is crossed). The only price a public file may show is a company's own disclosed historical average buyback price.
- No trade execution: money matters are always decided and executed by the owner personally.

## License

Method documents (`docs/`, `constitution/`, `agents/`, etc.) are licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Research content (`companies/`, `industries/`, `forecasts/`, `letters/`, `mistakes.md`) is all rights reserved. Code (`pipeline/`, `scripts/`, `tests/`, `.github/`) is MIT. See [LICENSE.md](LICENSE.md).
