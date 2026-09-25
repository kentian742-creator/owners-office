# 0006 Numbers cite the owner's report pages; primary filings are registered first and backfilled later

> **Partly superseded by [0009](0009-prompt-set-v3.md) (2026-09-24):** source tags for own reports switch to the 00 §E1 naming `<ticker>-RPT<number>-<date>`, and the migration must be finished before the first pre-registration is timestamped; the `#pN` page locator stays as it is. For the backfill of accession numbers, see [0013](0013-sec-edgar-access.md).

## Background

The hard rules require every number in an archive to carry a source tag, registered in `sources.yml`. The material at hand in Phase 0 was the owner's five company reports (02 reports, PDF), three Business Library industry studies, PDD's 20-F for fiscal 2025, and two Berkshire shareholder letters. The other companies' 10-Ks and 10-Qs were not in the workspace, and EDGAR could not be reached for the time being (see `0005`). The thesis-ci spec allows a tag to carry a location: `TAG#LOCATOR`.

## Options

1. Cite only primary filings (10-K, 10-Q, 20-F), and leave out for now the numbers that can't be sourced from them.
2. Cite the PDF page numbers of the owner's reports, and at the same time register the primary filings behind the reports in `sources.yml`, with the accession numbers to be backfilled.
3. Cite the section numbers of the owner's reports instead of page numbers.

## Decision

Option 2.

- The owner's reports are tagged `<TICKER>-RPT-2026-09` (all the reports are dated September 2026); industry studies are `IND-<ID>-2026-09`; PDD's annual report is `PDD-20F-FY2025`; Berkshire's shareholder letters are `BRK-LTR-<YYYY>`.
- The location is written `#p<N>`, where N is the physical page number in the PDF, that is, the page number in the `===== [page N] =====` markers of the extracted text, starting from 1; it is not the page number printed on the report. In Markdown this is written like `[src:MSFT-RPT-2026-09#p10]`, in YAML `source: MSFT-RPT-2026-09#p10`.
- The reports and industry studies are private files, so their entries say `location: private:reports/<file name>`; public readers can't open them, but the source is clear, and the audit (which can read the private repository) can check each item.
- Primary filings cited by the reports (for example a given year's 10-K) are registered as `kind: filing` entries with `accession: null`, to be backfilled once EDGAR is available (see `0005`). Later, when the audit checks the primary filings, a citation can be switched from the report page to the primary filing, with every change in the diff.

## Rationale

- The owner's reports are where the theses start from, so citing them is the most faithful answer to "where did this number come from"; the page number lets the audit find the original text in one step.
- Physical page numbers hold for both the extracted text and PDF readers, while printed page numbers are thrown off by covers and tables of contents.
- It says honestly that this is a secondary source while putting the primary filings on record, so the backfill is mechanical work that needs no new research.
- Citing only primary filings would leave most Phase 0 numbers empty, and the baselines of the thesis tests could not be written.

## Rejected alternatives

- **Primary filings only**: the primary filings can't be obtained now, so many numbers would be missing and Phase 0 could not be delivered.
- **Section numbers**: a section often spans several pages, so the audit would have to search; section numbering also differs between reports.

## Date

2026-09-24
