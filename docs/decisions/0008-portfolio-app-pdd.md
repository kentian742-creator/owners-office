# 0008 Holdings and candidate list corrected: holdings APP, PDD

> Supersedes [0002](0002-holdings-and-candidates.md).

## Background

[0002](0002-holdings-and-candidates.md) read the "APP" given at kickoff as a typo for AXP and set the holdings as AXP and PDD and the candidates as MSFT, BRK and SPGI. Later that day, a complete company report on AppLovin (Nasdaq ticker APP) appeared in the workspace, and work on an archive in `companies/APP/` began; the "New evidence" section of 0002 already recommended changing the holdings to APP and PDD and asked the owner to confirm. When this record was written, the `thesis.yml` files of APP, AXP and PDD in the public repository were all marked `holding`, and those of BRK, MSFT and SPGI `candidate`, which matched neither account.

On 2026-09-24 the owner confirmed: the holdings are APP and PDD.

The candidate rule comes from the design document and from HQ's quarterly ranking (17C): the three highest-ranked companies outside the holdings are the candidates, and the rest are archived and refreshed once a year with the annual report. HQ's quarterly ranking has not run yet; the only ranking that exists now is the series ranking in the owner's series of reports (private; its numbers are not in the public repository).

## Options

1. Keep 0002: holdings AXP, PDD.
2. Holdings APP, PDD; AXP, MSFT, BRK and SPGI all candidates until HQ's first ranking decides.
3. Holdings APP, PDD; the three highest-ranked companies outside the holdings in the owner's series ranking become the candidates, and the rest are archived.

## Decision

Option 3.

- **Holdings (`status: holding`):** APP, PDD (confirmed by the owner).
- **Candidates (`status: candidate`):** AXP, MSFT, SPGI, the three highest-ranked companies outside the holdings in the owner's series ranking.
- **Archived (`status: archive`):** BRK. Its archive is kept and refreshed once a year with the annual report. It remains the first reference anchor for valuation: under the engineering default in 00 §V6, Berkshire's long-term return estimate is the first hurdle, with its numbers taken from the current version of the BRK archive together with their base date, and archiving does not affect that use.
- From now on the list rotates with HQ's quarterly ranking (17C, decision level L2, reported in the monthly letter).
- Correcting the `status` in each company's `thesis.yml` and `story.md` belongs to the content migration and is outside the changes made by this record; it is listed in [STATUS](../STATUS.md).
- 0002 is kept for history, with a note at the top that this record supersedes it.

## Rationale

- The holdings are a fact about the owner, not something the system can infer; the owner has confirmed them, and that confirmation is what counts.
- The design document sets the number of candidates at three, to control cost and to apply the opportunity cost principle: candidates must be compared side by side with the holdings. Until HQ's first ranking, the ranking in the owner's series of reports is the only existing ranking made within one framework, and taking its top three comes closest to the design document's rule.
- BRK is the hurdle itself. Leaving the candidates does not mean leaving the system: the hurdle needs the current reading from its archive, not a candidate slot.

## Rejected alternatives

- **Keep 0002:** the opposite of the holdings the owner confirmed.
- **All four as candidates:** more than the three in the design document; candidate companies get quantitative tests, ledger settlement and quarterly updates every quarter, so each extra one adds cost, and the ranking already gives an order.
- **Take BRK out of the system entirely:** it is the hurdle of R7 and the first reference anchor of §V6, so its archive has to stay up to date.

## Consequences

- Pre-registration, the question list and blind read, and the independent judging of qualitative tests run only for APP and PDD; the first pre-registrations are APP's FY2026Q3 (results expected in early November) and PDD's results for the same period (expected in late November); see [STATUS](../STATUS.md).
- Candidate companies run only a slimmed-down process: quantitative tests (including metric extraction), ledger settlement, quarterly updates, and fact extraction → fact audit → revision.
- The design document's learning target "about 80 settled system forecasts by the end of 2027" was estimated for four holdings; now there are two, so the sample is roughly halved and calibration conclusions by domain will come later (as 0002 already pointed out).

## Date

2026-09-24
