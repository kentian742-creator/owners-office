# 0031 HQ names the documents a build or a valuation still needs; the pipeline fetches them

> Follows [0028](0028-archives-from-sec-filings.md) and [0029](0029-prices-valuation-report.md). Code: the input
> `requested_documents` and `hq_build_files` in `pipeline/registry.py`; `filing_for_entry` and `cik_for_ticker` in
> `pipeline/edgar.py`.

## Background

A valuation (01C) sees the filings the pipeline selects for it (`archive.valuation_selection`, within 450,000
estimated tokens); its model review (04C) sees no filing text at all, only the source table. On 2026-09-30 Berkshire Hathaway's valuation could
compute only two of its five backtest year-ends. The other three need Notes 4 and 5 of older 10-Ks and the
third-quarter 10-Qs published before each year-end, and the selection includes neither (HQ rulings R18 and R19).
Dossier builds also cite other companies' filings for comparison, such as PGR-10Q-FY2026Q2 and UNP-10K-FY2025. The
pipeline could not fetch those at all, so the fact audit could not check the claims that rest on them (ruling R3).
The model cannot fetch a document itself: the claude-code backend runs without tools (0022).

## Decision

1. **HQ decides what is supplied.** Next to its rulings on a build, HQ may write
   `runs/hq/<date>-17A-<TICKER>/supply.yml` in the private repository: a list of `{tag, why}`, where the tag may carry
   a locator, as in `BRK-10K-FY2022#Note4`, `BRK-10Q-FY2025Q3#Item1` or `PGR-10Q-FY2026Q2#p37`. The order of the list
   is HQ's priority. The lists that count are those in the window of the rulings: from the build's first draft until
   the next build's first draft. One function makes that selection for both `hq_rulings` and `requested_documents`.
2. **The pipeline fetches what HQ lists, and nothing else.** `requested_documents` is an optional input of 01A round
   2, 01C, 04C and 02. Each tag is resolved to its EDGAR filing:
   - through the company's registered sources first (accession, issuer CIK or URL), whatever kind they are
     registered as, as long as they carry an EDGAR accession or URL (a letter or a web page is not on EDGAR);
   - otherwise from the tag itself: the issuer's CIK that the registered sources give the tag's ticker (Berkshire's
     archive uses BHE- for Berkshire Hathaway Energy, which SEC's ticker table gives to another company), else from
     SEC's ticker table (`company_tickers.json`); then the filing
     in that issuer's submissions, matched as `check-sources` matches it: a periodic report by form and fiscal period
     in the issuer's own calendar, a current report by filing date and ordinal.

   A filing made after the run date is not supplied, because a run reads only what was public on its date; nor is a
   document that is not HTML or text (Berkshire files its annual report to shareholders as a PDF only).
3. **Only the part HQ names.** A locator cuts the primary document to a page (`p37`, `pK-86`), an item (`Item8`) or a
   note (`Note4`), or picks an exhibit (`EX-99.1`). A page is found by its footer; where the same number also stands
   in the table of contents or in a table, the footer is the one in the run of pages numbered one after another.
   When the page footers and headings do not show the part a locator names, a document that fits one item is
   supplied whole and its note says so; a longer one is not supplied, as its first pages would rarely hold the part.
   One document takes at most 50,000 estimated tokens and the whole input at most 250,000, its header lines
   included. The document that reaches the total is cut to what is
   left and the ones after it are not supplied, nor is any document once less than 2,000 tokens are left. Every cut
   says where it falls.
4. **Nothing is dropped silently.** Each request that cannot be resolved or fetched is listed at the top of the input
   as `not supplied: <tag>: <reason>`, in the pipeline's own words (an EDGAR failure is named by its kind, without
   local paths). When HQ wrote no list, or nothing on it can be supplied, the input is left out and the manifest
   records why. Without the SEC contact (User-Agent) the run fails, as with every other EDGAR input.
5. **A cited document is registered like any other the pipeline supplied.** The source record of each document
   carries its tag, form, accession, issuer CIK, filing date, URL, title and period. When a placed file cites one that
   no `sources.yml` registers, placement registers it, and another issuer's filing goes in with that issuer's CIK.

## Consequences

- Berkshire's case, assembled from the local EDGAR cache: Notes 4 and 5 of the 10-Ks for FY2021, FY2022 and FY2024
  come to about 12,000 tokens together. Finding them took one change to the note cutter. Berkshire heads each note
  "(4)" on a line of its own with the title on the next, so that form is now read as a heading, but only among the
  notes (after their title, before the next item) and in a row of three numbered one after another; a table
  footnote or an entry of the exhibit index such as "(10) Material Contracts" is not.
- The fact audit (04A) still resolves only the company's own filings (`cited_texts`): a claim in a dossier that rests
  on another issuer's filing stays unchecked there, as ruling R3 found, even once that filing is supplied here and
  registered. Giving 04A the same resolution is a follow-up.
- The four prompts gain the input in the private repository. Until they declare it, the pipeline never assembles it.
- Every request goes to the SEC's hosts only, through `pipeline/edgar.py` and its User-Agent rule (0013).

## Options not taken

- **A wider selection for every valuation.** It would spend tokens on every run for a need that arises company by
  company, and the pipeline cannot know which notes a particular backtest is missing.
- **Letting the model fetch documents.** The backend runs without tools by design (0022); every input stays the
  pipeline's, recorded and hashed.
- **Failing the assembly when one request cannot be met.** A mistyped tag or a filing not yet made would block the
  whole run. Listing it lets the run go ahead and HQ correct its list.

## Date

2026-10-07
