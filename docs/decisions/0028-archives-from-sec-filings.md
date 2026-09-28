# 0028 New archives are built by the pipeline from the SEC filings

> Follows [0027](0027-holdings-app-spgi-mcd.md). Code: `pipeline/archive.py`, the 01A and 01B steps and their
> assemblers in `pipeline/registry.py`, `companies/intake.yml`.

## Background

The first six archives were written from the owner's own research reports, then fact-checked against the filings
(Phase 0 and the audits of September 2026). On 2026-09-28 the owner added four companies with no report behind them:
McDonald's (a holding), Alphabet, Apple and NVIDIA (research coverage). The owner also wants research reports to be a
pipeline that starts from the SEC's primary filings. Prompt 01 (company archive: dossier, system files, valuation)
existed, but the runner could not run it.

Three things had to be settled:

- **Identity.** The pipeline finds a company's CIK, fiscal year and forms in its `thesis.yml`, which a new company
  does not have until 01B writes it.
- **What 01A reads.** A dossier covers ten years: economics, capital allocation, management and the evolution of the
  moat. The filings behind it would not fit one call. McDonald's current 10-K and proxy statement alone come to about
  210,000 tokens.
- **How the new dossier is audited.** The fact audit (16A → 04A) was wired for quarterly updates.

## Decision

1. **`companies/intake.yml` lists the companies the owner has added whose archive is not built yet:** ticker, name,
   CIK, status and the date decided. Until the company's `thesis.yml` exists, the pipeline takes its name and status
   from that list, and reads its filer block (fiscal year end, domestic or foreign, forms) from EDGAR's submissions.
2. **01A is given the present first, then the history, then what repeats.** Filings are added in this order while
   they fit a budget of 450,000 estimated tokens:
   - the present: the latest annual report in full; the latest earnings release and quarterly report; the latest
     proxy statement; the current reports of the last twelve months on material agreements, acquisitions and changes
     of officers (items 1.01, 2.01, 5.02);
   - the history: the business section and MD&A of the annual reports five and ten fiscal years back;
   - what the present largely repeats: the other earnings releases of the last year, earlier quarterly reports.

   Whatever does not fit is named in the input, so the dossier can list it among its unknowns instead of filling the
   gap from memory (00 §E3).
3. **A ten-year financial summary comes from XBRL.** `xbrl_facts` holds eighteen line items per fiscal year: revenue,
   operating income, net income, diluted EPS and shares, operating cash flow, capital expenditure, depreciation,
   share-based pay, buybacks, dividends, interest, tax, cash, assets, debt and equity. Each value is the one the
   latest annual report states for that year, with its accession and source tag. When another XBRL concept gives a
   different value for the latest year, it is listed beside the chosen one, so the dossier checks which one the
   statements use.
4. **The dossier is audited like an update.** 16A and 04A run with the subject "archive" (`--subject archive`). 16A
   reads 01A's first draft and the sources it added. 04A resolves the draft's source tags through those additions and
   runs in slices when needed (0026). 01A's second round revises the draft with the audit's findings, and 01B turns
   the final dossier into `thesis.yml`, `story.md` and `ledger.yml`. The pipeline writes their header fields: the
   name and status from the intake list, the filer block from EDGAR, and trust level 1.
5. **Not yet in the pipeline:** 01C (the private valuation), which needs a source of official closing prices
   (STATUS T21), and 04B (red team and argument review of the dossier), which needs two passes in one bundle. Both
   are recorded as omitted inputs, never silently skipped.

## Consequences

- McDonald's 01A assembles from the real filings at about 485,000 estimated tokens: eight filings, ten fiscal years
  of XBRL data and the three industry modules. It is well inside the context window, and the three older earnings
  releases and one quarterly report that did not fit are named.
- Some annual reports (McDonald's among them) index their items by page instead of heading them in the text. For
  those, the section cut falls back to the whole filing and the input says so.
- The whole path (01A → 16A → 04A → 01A round 2 → 01B) runs end to end with the fake backend. 01B's outputs are
  placed in the public `companies/<TICKER>/`, the dossier in the private repository.
- The first real build is McDonald's, after the plan's weekly reset on 2026-09-30, so that its pre-registration can
  be merged by about 2026-10-22.
