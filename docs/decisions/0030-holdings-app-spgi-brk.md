# 0030 Holdings APP, SPGI, BRK; MCD becomes research coverage

> Supersedes the lists in [0027](0027-holdings-app-spgi-mcd.md). Its other decisions stand.

## Background

On 2026-09-30 the owner said their positions had changed. They sold McDonald's (MCD), which had been their defensive
position, and bought Berkshire Hathaway (BRK.B): they judge Berkshire a better fit for them, and as a shareholder they
can attend Berkshire's annual meeting. They also added to AppLovin (APP). They no longer hold MCD, PDD, MSFT or AXP.

## Decision

- **Holdings (`status: holding`):** APP, SPGI, BRK. BRK moves up from `archive`; it also stays the first hurdle every
  other company is compared with (§V6).
- **Research coverage (`status: candidate`):** MSFT, AXP, MCD, GOOG, AAPL, NVDA. MCD moves down from holding; its
  archive, built from the SEC filings on 2026-09-29, keeps its quarterly updates. `ranking.candidates` goes from 5 to 6.
- **Archived (`status: archive`):** PDD.

## Consequences

- **Pre-registration.** BRK now pre-registers. Its FY2026Q3 release is estimated for 2026-10-29 (its last three
  third-quarter releases came on Saturdays between November 1 and 4), so the pre-registration must be merged by
  2026-10-25. MCD's is no longer due.
- **Public disclosure is unchanged.** The README names the holdings but never position sizes, costs or weights; those
  stay in the private repository.
- **What 0027 put in place is updated.** The `status` in the thesis files and stories of BRK and MCD, the README (both
  languages), `constitution/decision-rights.yml`, and the STATUS earnings calendar, which the daily watch reads.
