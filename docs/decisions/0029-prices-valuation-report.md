# 0029 Prices from Nasdaq.com; the valuation (01C) and the research report (02) join the pipeline

> Settles STATUS T21 (the source of price references). Follows [0028](0028-archives-from-sec-filings.md). Code:
> `pipeline/prices.py`, steps 01C and 02 in `pipeline/registry.py`.

## Background

A valuation needs a price with its date and source (00 §H2), five fiscal year-end closes for the backtest (§V5,
§V12), the reference anchors (§V6) and the 10-year Treasury yield behind the discount rate (§V1). The first six
valuations took these from the owner's reports. On 2026-09-28 the owner chose the exchanges' own websites as the
source, and asked to see a research report for McDonald's built by the pipeline.

## Decision

1. **Prices come from Nasdaq.com.** Its historical-quotes service covers NYSE and Nasdaq listings and ETFs for ten
   years. Every price carries its date, the URL it was read from, and what it is: the consolidated close, adjusted
   for stock splits and not for dividends. For an NYSE listing the close can differ by a few cents from NYSE's own
   closing auction. Berkshire is valued through its class B shares (BRK.B); the index anchor's price is VOO's.
2. **The 10-year yield comes from the U.S. Treasury's daily par yield curve,** with its date.
3. **Nothing personal leaves the machine.** These requests carry a generic User-Agent. The SEC contact address in
   the workspace `.env` is for EDGAR only.
4. **01C (the private valuation) runs in the pipeline.** Its inputs are:
   - `price_reference`, the close on the run date;
   - `year_end_closes`, the last five fiscal year ends;
   - `anchors`: Berkshire's current valuation reading and close, VOO's close (HQ has not yet set the index anchor's
     return, ruling S11), and the Treasury yield;
   - `series_roster`: the other companies' current valuation readings.

   Its outputs are private and proposed until 04C reviews them (§V20).
5. **02 (the research report) runs in report mode.** The model writes the report as Markdown, with cover and chart
   specifications in YAML, from the archive, the valuation, the roster, the price and the anchors. Typesetting the
   PDF (19) waits for a runtime that can run code (STATUS T20). There is no official wordmark file yet, so the input
   says so.

## Consequences

- Every price the pipeline reads is recorded in the bundle's sources with its URL and date, and goes only into
  private files (§H4).
- A split between a year end and the run date is already reflected in the year-end closes. This is stated on each
  close, so a backtest compares like with like.
- 04C (the valuation model review) is not in the pipeline yet. A valuation from 01C stays `proposed`, and the report
  says it has not been reviewed.

## Amendment (2026-10-08): a listed portfolio marked from its 13F

A company that holds a large listed equity portfolio, such as Berkshire Hathaway, is valued partly through it, and
its balance sheet carries the portfolio at the last quarter end. Two inputs give the valuation the portfolio at the
dates it needs instead (`pipeline/holdings.py`; private, like every price):

- `holdings_marks`: the positions of the latest Form 13F-HR, each marked at the price-reference date. A position's
  13F value (its market value at the report date) is carried forward by the ratio of its Nasdaq.com closes at the
  two dates, so a stock split in between does not change it. The ticker of each CUSIP comes from OpenFIGI's public
  mapping, and the request carries only the codes.
- `backtest_holdings_marks`: for each backtest year-end (§V5, §V12), the positions of the last 13F filed by that
  date, marked at the year-end's closes, so the backtest starts only from what was public then.

Values in 13Fs filed before 2023-01-03 are in thousands of dollars, and the parser converts them. What the marks
cannot see is stated with them: trades after the 13F's report date, holdings outside the 13F (non-US listings), and
positions with no US ticker or no price history, which keep their report-date value and lower the stated coverage.
The 04C model review sees both inputs.
