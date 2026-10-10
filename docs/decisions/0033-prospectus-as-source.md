# 0033 A company without an annual report yet is read from its offering prospectus

> Follows [0028](0028-archives-from-sec-filings.md) and [0032](0032-research-coverage-widened.md). Code:
> `latest_prospectus`, `prospectus_picks` and `PROSPECTUS_PARTS` in `pipeline/archive.py`; `prospectus_sections` in
> `pipeline/documents.py`; `PROSPECTUS_FORMS` in `pipeline/edgar.py`.

## Background

A new archive's dossier (01A) is written from the latest annual report in full and from the business sections and
MD&As of the annual reports five and ten years back (0028). SpaceX, added on 2026-10-09 (0032), listed in June 2026.
It has filed:
- its registration statement (S-1, 2026-05-20) and two amendments;
- the final prospectus (424B4, 2026-06-12);
- one 10-Q (2026-08-04);
- some 8-Ks.

It has filed no annual report. Its record before the listing, the audited statements for 2023 to 2025 and their MD&A,
is only in the prospectus. The pipeline read no prospectus, so SpaceX was put last in the queue.

A whole prospectus does not fit an input. SpaceX's comes to about 568,000 estimated tokens, against 450,000 for all of
a dossier's sources. A prospectus also has no items: its sections are found only by their headings.

## Decision

1. **Which prospectus.** For a company that has filed no annual report, the pipeline reads the final prospectus of its
   latest offering (424B1 or 424B4), else its latest registration statement (S-1, F-1 or an amendment). Once the first
   annual report is filed, that report carries the record and no prospectus is read.
2. **Sections, found by their headings.** The sections are the top-level headings that the prospectus's table of
   contents lists, such as "RISK FACTORS", "BUSINESS" and "MANAGEMENT". Each runs to the next such heading.
   - A table of contents entry, with its dot leaders and page number, is not a heading.
   - When a document sets its headings in capitals, a subheading in title case ("Management" inside the business
     section) does not end a section.
   - A heading wrapped onto a second line is read as one.
   - A section that cannot be found is named in the source note, and the sections that were found are still supplied.
     Only when none is found is the whole document kept, as for a periodic report. Since a whole prospectus is over
     the budget, it is then listed as not supplied.
3. **In parts, in this order of priority.** Each part is a selection of its own, so the input budget keeps them in this
   order:
   1. the record: MD&A and the financial statements, in place of an annual report;
   2. the business section;
   3. management, executive pay, related-party transactions, principal stockholders and the description of capital
      stock, in place of a proxy statement;
   4. the risk factors, after the current reports, since a 10-Q only updates them.

   The latest earnings release and 10-Q come first, as they do for every new dossier.
4. **Interim statements a later 10-Q replaces are left out.** When a 10-Q was filed after the prospectus, the financial
   statements stop at the first balance sheet marked unaudited after the auditor's report. That is where the interim
   statements begin, and the 10-Q's figures replace them. Before the first 10-Q, the prospectus's interim statements
   are the latest figures and stay.
5. **The valuation (01C)** of such a company is checked against its latest 10-Q in full and the prospectus's record.
6. A prospectus is cited like any current report, by its form and filing date (thesis-ci SPEC 3.3), for example
   `SPCX-424B4-2026-06-12#p74`. Page locators (`#p74`, `#pF-12`) find its pages by their footers, as in a 10-K.

## Consequences

SpaceX's sources, assembled from EDGAR on 2026-10-09:

| Part | Estimated tokens |
| --- | ---: |
| The 10-Q | about 73,000 |
| The prospectus's MD&A and audited statements (from 1.47 million characters) | about 163,000 |
| Its business section | about 124,000 |
| Management, pay, related parties, ownership and capital stock | about 50,000 |
| Its risk factors | about 78,000 |

With the release and four current reports, the input comes to about 435,000 tokens. The risk factors do not fit after
the other parts. The source note says so, and the dossier names them in its unknowns register (00 §E3). HQ can supply
pages of them to the revision (0031).

SpaceX's ten-year XBRL summary is empty: the summary takes each fiscal year from an annual report, and there is none
yet. The prospectus's audited statements carry the record until the first 10-K.

SpaceX stays last in the queue, after its third-quarter 10-Q, so that its first dossier starts from the latest quarter.

## Date

2026-10-09
