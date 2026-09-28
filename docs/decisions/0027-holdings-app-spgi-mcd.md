# 0027 Holdings APP, SPGI, MCD; research coverage MSFT, AXP, GOOG, AAPL, NVDA

> Supersedes the lists in [0008](0008-portfolio-app-pdd.md). Its reasons for keeping BRK as the hurdle stand.

## Background

On 2026-09-28 the owner said their positions had changed. They now hold S&P Global (SPGI), AppLovin (APP) and
McDonald's (MCD). They also want five companies followed as research and published here, because readers are
interested in them: Alphabet (GOOG), Apple (AAPL), NVIDIA (NVDA), Microsoft (MSFT) and American Express (AXP). PDD
appears on neither list.

Until now the candidates were the three highest-ranked companies outside the holdings (0008, and
`ranking.candidates: 3` in `constitution/decision-rights.yml`).

## Decision

- **Holdings (`status: holding`):** APP, SPGI, MCD. SPGI moves up from candidate. MCD has no archive yet.
- **Research coverage (`status: candidate`):** MSFT, AXP, GOOG, AAPL, NVDA. These get quarterly updates and public
  archives, like the candidates before them. GOOG, AAPL and NVDA have no archive yet. `ranking.candidates` goes from 3
  to 5. The owner chose this list, so HQ's quarterly ranking (17C) still ranks and reports, but does not rotate the
  list until the owner says otherwise.
- **Archived (`status: archive`):** BRK, still the first hurdle (§V6), and PDD, which is no longer held. PDD's archive
  is kept and refreshed once a year with its annual report, and its FY2026Q3 pre-registration is no longer due.
- **New archives start from the SEC filings.** The first six archives were built from the owner's own research
  reports. MCD, GOOG, AAPL and NVDA are built by the pipeline from primary sources: prompt 01 (dossier, system files,
  valuation) wired into the runner, then the fact audit 16A → 04A in slices (0026). MCD comes first, because it is a
  holding with results due in late October.

## Consequences

- **Pre-registration deadlines.** SPGI's FY2026Q3 pre-registration must be merged by 2026-10-17 (estimated release
  2026-10-21, deadline 2026-10-20 23:59 ET). APP's must be merged by 2026-10-30. MCD's comes after its archive: its
  last three third-quarter releases fell between October 29 and November 5, which puts its merge deadline around
  2026-10-22.
- **Cost.** Five research companies instead of three add about two quarterly updates each quarter. The budget's
  degrade order already pauses candidates first (`budget.degrade_order`).
- **Public disclosure is unchanged.** The README names the holdings but never position sizes.
- **What 0008 put in place is updated.** The `status` of SPGI and PDD in `thesis.yml` and `story.md`, the README, the
  STATUS earnings calendar and the to-do list.
