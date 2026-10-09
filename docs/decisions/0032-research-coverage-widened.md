# 0032 Research coverage widened to twelve companies; McDonald's and AppLovin go through the pipeline again

> Adds to [0030](0030-holdings-app-spgi-brk.md). Holdings are unchanged.

## Background

On 2026-10-09 the owner added six companies to the companies waiting for the pipeline: Costco (COST), Walmart (WMT),
Coca-Cola (KO), Visa (V), Mastercard (MA) and SpaceX (Space Exploration Technologies Corp., SPCX). The owner also put
McDonald's (MCD) and AppLovin (APP) back in that queue, and left the order to the pipeline.

## Decision

- **Research coverage (`status: candidate`)** grows from six companies to twelve: MSFT, AXP, MCD, GOOG, AAPL and NVDA,
  plus COST, WMT, KO, MA, V and SPCX. `ranking.candidates` in `constitution/decision-rights.yml` goes from 6 to 12.
  The quarterly ranking (17C) ranks them but does not rotate them out.
- **The six new companies** go on `companies/intake.yml`. The pipeline builds each archive from the SEC filings, as
  it built McDonald's (0028): dossier, fact audit, HQ's rulings, revision, then the system files. A valuation and a
  research report follow for each.
- **McDonald's** gets the valuation and the research report the owner stopped on 2026-09-30. Its archive already
  exists.
- **AppLovin**, a holding whose archive was first built from the owner's own notes, is rebuilt from its filings.
- **The order** follows each company's filing calendar, so that a new dossier starts from the latest quarter instead
  of being redone a few weeks later:
  - now: NVIDIA (already under way), Costco (its FY2026 10-K was filed on 2026-10-07), Walmart (its next 10-Q is due
    in early December), and McDonald's valuation and report (a new quarterly report does not make a valuation stale,
    §V20);
  - after their third-quarter 10-Qs in late October: Coca-Cola, Mastercard, Alphabet and Apple (Apple's next filing
    is its FY2026 10-K);
  - in November: AppLovin, after its third-quarter 10-Q; Visa, after its FY2026 10-K;
  - SpaceX last. It listed in June 2026 and has filed one 10-Q and no 10-K, so the earlier years of its record are
    only in its prospectus, and the pipeline does not yet read a prospectus as a source (0033 adds it).

## Consequences

- A company joins the STATUS earnings calendar, which the daily watch reads, once its archive exists. Until then
  nothing would run on its results.
- Each archive costs about as much as McDonald's did, mostly in model calls on the owner's subscription (0025), so
  the queue runs one company at a time and can pause at the subscription's limits.
- The public files gain one archive at a time, each through a pull request. Valuations and reports stay in the
  private repository (§H4).

## Date

2026-10-09
