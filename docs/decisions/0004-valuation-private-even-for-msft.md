# 0004 Valuation numbers always stay in the private repository, MSFT included

> **Partly amended ([0012](0012-discount-rate-no-cross-company.md), 2026-09-24):** the phrase in this record "how the quality rating affects the discount-rate premium" no longer holds: the premium is judged from the company's own cash-flow record, not slotted by rating (00 §V1, §V10). The rest is unchanged.

## Background

Two statements in DESIGN.md need to be reconciled:

- The default for "what is public" is "of the full archives, only MSFT's is public", and the contents of owners-office include "a complete MSFT sample archive".
- The hard rules of the kickoff instructions say "Value ranges and L3 memos are stored only in the private repository; the public thesis.yml contains no value_ranges"; the design principles also require "price silence" and no buy or sell advice in public content.

The owner's 02 reports are complete reports on 12 dimensions, and their valuation part contains a reference price, a value range and its midpoint, the discount rate, the annualized return implied by the current price, and a letter grade for the price. Publishing the MSFT report as it is would bring these numbers into the public repository; thesis-ci's `C-PUBLIC-NO-VALUATION` would stop them as errors.

## Options

1. Publish MSFT's full archive as it is, valuation numbers included.
2. Publish the full archive, but have the lint make an exception for MSFT.
3. Publish MSFT's full archive, with the valuation dimension covering only method and judgment and no number derived from price; the numbers stay in the private repository.
4. Don't publish MSFT's full archive at all.

## Decision

Option 3.

- MSFT's public archive (Phase 1, 12 dimensions, Markdown, under `companies/MSFT/`) keeps the business, economics, moat, management, bear case, peers, monitoring, failure modes and the rest in full.
- In the public version the valuation dimension states only which method is used, which assumptions matter most, how the quality rating affects the discount-rate premium (rule 5 of the constitution), and how sensitive the valuation is to distant cash flows. It does not state the reference price, the value range or its midpoint, the return implied by the current price or the grade for the price, nor the result of the reverse calculation of "what the current price assumes".
- All of these numbers go into `owners-office-private/companies/MSFT/valuation.yml`, as for every other company; prices exist only as private reference data, used for one alert when a range is crossed.
- `ratings` in the public `thesis.yml` contains only the four items business, management, capital allocation and culture (section 4 of the thesis-ci spec).

## Rationale

- The hard rules are explicit constraints in the kickoff instructions, and CI enforces them; "only MSFT's full archive is public" is an adjustable default. Where the two conflict, the hard rules win.
- DESIGN.md publishes MSFT because "one complete sample is enough to prove quality". Quality is proved by the business analysis, the sources and the testable thesis, not by price numbers; removing the valuation numbers does not hurt that purpose.
- The Phase 5 valuation configurator was always planned to be closed source, as a subscription product; keeping valuation numbers in the private repository is consistent with that, and it also keeps public content from being taken as investment advice.
- The same rule applies to every company, so the lint needs no exceptions and the rule is harder to game.

## Rejected alternatives

- **Publish as it is**: directly breaks a hard rule, and CI would not pass either.
- **A lint exception for MSFT**: once there is an exception, "the public repository contains no value ranges" is no longer a rule a machine can guarantee.
- **Don't publish the MSFT archive**: gives up the DESIGN.md purpose of showing quality through one complete sample, when the conflict is only about the valuation numbers.

## Date

2026-09-24
