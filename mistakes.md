# Mistakes list

A Chinese version is in [zh-CN/mistakes.md](zh-CN/mistakes.md).

A public record of the mistakes made by the system and by the owner. The more public a mistake, the harder it is to admit, so mistakes get a list of their own: changing your mind counts as an achievement, not a stain. The annual "letter to the owner" is published together with this list.

## What goes in

- **Factual errors**: a number or quotation merged into an archive is wrong (wrong source, misread, miscalculated).
- **Errors of judgment**: settled afterwards against the criteria written down in advance, the conclusion was wrong, and the error lies in the reasoning, not just in luck.
- **Process errors**: a check that should have been done was not done, a pre-registration was late, a test threshold was changed after the fact, an escalation was missed.
- **Omissions**: a risk or fact that the archive did not cover and that later proved to matter.

A forecast that did not come true is not in itself a mistake; it is recorded in `forecasts/` and scored with the Brier score. Only problems in the process or the reasoning go into this list.

## Format

One subsection per entry, newest first. Factual numbers carry `[src:...]` tags as usual; no amounts, prices or positions.

```markdown
### YYYY-MM-DD · Ticker · Category

- Judgment at the time: … (link to the PR, pre-registration or update record)
- What actually happened: …
- What went wrong: …
- Whose mistake: company manager / industry researcher / HQ / audit / blind reader / owner
- What changed: rules, tests, prompts or process (link to the commit or decision record)
```

## The list

### 2026-09-27 · MSFT · Factual error

- Judgment at the time: the phase 0 MSFT archive (`companies/MSFT/`) repeated a few wrong inputs from the owner's report wherever it used them. The revenue of Microsoft 365 Commercial products and cloud services, which includes on-premises licenses, was labeled as the cloud part alone and paired with the cloud's growth rate; the series of free cash flow over non-GAAP net income mixed two definitions, and two of its years divided by GAAP net income; the OpenAI stake was given at its size before later funding rounds, and its accounting was misdescribed. Smaller errors included the metrics of the executive performance awards, "no equity issuance", segment growth and margin figures, and when Amazon funded its OpenAI investment.
- What happened: the first fact audit checked 460 facts against the 10-Ks, 10-Qs, 8-Ks, the proxy statement and the company's own call transcripts: 379 accurate, 41 judged errors, 5 basis issues, 4 unconfirmed, 14 resting only on secondary sources and 17 supported only by the report. The errors cluster: the Microsoft 365 line and the growth gap built on it (9 facts), the cash-conversion series (9), the OpenAI stake and its accounting (6), the performance awards (5), share issuance (2) and nine single facts. The 41st was withdrawn: the audit and the first HQ ruling had judged the end date of OpenAI's revenue-share payments against Microsoft's announcement of October 2025, and in the revision the company manager found that an amendment of April 2026 had replaced those terms. Under the amended agreement the report is right.
- Where it went wrong: like the other archives, this one was built from the report before the filings could be fetched, and it was merged before a fact audit; the errors came from the owner's report, were copied into the archive, and each wrong input spread to every place that used it. The withdrawn error shows a second gap, in the audit and the ruling: a fact that rests on an agreement was judged without checking whether the agreement had since been amended.
- Whose error: the system's phase 0 build; phase 0 archives are not scored for any role (`docs/decisions/0016`). The source is the owner's report, whose differences from the primary documents are logged in the private errata table. The withdrawn finding was the audit's and HQ's; it was caught before any file changed on its strength, and hand-run audits are not scored either.
- What changed: the archive was corrected against the primary documents on 2026-09-27, with each recurring error fixed wherever it appeared and 32 newly registered public sources; the report text stays as written, and no test's threshold, direction or rule changed. HQ recorded the lesson for audits and rulings: when a fact rests on an agreement, check for a later amendment before calling it wrong.

### 2026-09-27 · AXP · Factual error

- Judgment at the time: the phase 0 AXP archive (`companies/AXP/`) repeated figures and statements from the owner's report without checking them against the filings: the number of large earnings drawdowns since 1989, the yearly interest cost of deposits, the share of net income returned to shareholders (presented as a figure the company had disclosed), the number of operating segments, when the company reset its earnings target in 2015–2016, the benefit cost of each extra dollar of card fees, the terms of the January 2025 settlements with the Justice Department, and whether purchase protection for purchases made by AI agents had launched.
- What happened: the first fact audit checked 338 facts against the 10-Ks, 10-Qs, 8-Ks, the proxy statement and the Justice Department's releases: 295 accurate, 17 errors, 5 basis issues, 4 resting only on secondary sources and 17 supported only by the report. The 17 errors are eight problems, most repeated in several places: the drawdown count (4 facts; five drawdowns, not four), the deposit interest cost (4), the share of net income returned (2), the settlements (2), the benefit cost per dollar of fees (2), and the segment count, the target reset and the agent purchase protection, announced for the future rather than launched (1 each). Four of the five basis issues concern the co-brand share of spending, which the company has reported on two definitions, and a test's coefficient rested on that mix.
- Where it went wrong: as with APP and PDD, the archive was built from the report before the filings could be fetched, and it was merged before a fact audit. The errors came from the owner's report and were copied into the archive unchecked.
- Whose error: the system's phase 0 build; phase 0 archives are not scored for any role (`docs/decisions/0016`). The source is the owner's report, whose differences from the primary documents are logged in the private errata table.
- What changed: the archive was corrected against the primary documents on 2026-09-27, citing them instead of the report, with 40 newly registered public sources; the report text stays as written. Test AXP-Q8, whose coefficient mixed the two definitions, is superseded from FY2026Q4 by AXP-Q17 with a coefficient on one definition; AXP-Q8 stays as the record, and no threshold of a test in force changed.

### 2026-09-25 · PDD · Fact errors

- What was written: the phase 0 PDD archive (`companies/PDD/`) repeated several statements from the owner's report without checking them against the filings: when PDD stopped disclosing GMV and user numbers, the count of loss years in the last decade, the sources of share issuance, the 2022 merchandise-sales precedent, and FY2025 figures taken from the pre-penalty results announcement without saying so.
- What happened: the first fact audit checked 372 facts against the filings: 304 accurate, 12 errors, 4 basis issues, 20 resting only on secondary sources. Fixed against the 20-F and 6-Ks; the audited FY2025 figures now come first, with the announcement basis alongside.
- Where it went wrong: as with APP, the archive was built from the report before SEC data could be fetched, and it was merged before a fact audit.
- Whose error: the system's phase 0 build (not scored for any role, decision 0016); the source is the owner's report, whose differences are logged in the private errata table.
- What changed: every archive now goes through the fact audit before a pre-registration relies on it (AXP, MSFT, SPGI and BRK are next).

### 2026-09-25 · Process · private information in a public file

- What was written: the public progress file `docs/STATUS.md` recorded, company by company, how the grades given to two companies' prices changed under the owner's chosen scale. Those grades belong only in the private repository (hard rule H4).
- What happened: the line was in the public repository from its first commit (2026-09-25) until it was found the same day by the new English wording check; it has been reworded to say only that two companies moved one step, with the details in the private repository.
- Where it went wrong: the public-content check did not scan `docs/`, and the line was written by hand while summarising the migration.
- Whose error: the system's (phase 0 engineering), not a role's.
- What changed: the public-content check now also scans `docs/STATUS.md`; STATUS reports private changes only as counts.

### 2026-09-25 · APP · Factual error

- Judgment at the time: the APP archive built in Phase 0 (`companies/APP/thesis.yml`) copied the rounded figures in the company report for debt principal and net debt; for the change in share count, it took the opening figure from the following year's year-end; the five-year reading for management criterion (a) mixed two sets of numbers, from before and after a restatement.
- What actually happened: the first fact audit checked 237 facts one by one against the 10-K and 10-Q filings; seven were judged errors, which were really two problems (debt principal and net debt; the opening share count), plus one basis issue. The rounded debt figure in the report came from remarks made on an earnings call and does not match the filings.
- What went wrong: when the archive was built, SEC's EDGAR was not yet reachable, so the numbers were taken from the report without going back to the primary filings one by one; and the archive did not go through a fact audit before it was merged.
- Whose mistake: the system's archive-building process (the Phase 0 engineering setup, which belongs to no role and, per `docs/decisions/0016`, does not count toward any trust level); the root cause is the rounded figures in the owner's report.
- What changed: the archive was corrected against the primary filings (commit d471ebb); the report text is left unchanged and the differences are logged in the errata table in the private repository; the archives of the other five companies get the same fact audit before their first pre-registration, and from now on every archive goes through a fact audit before it is merged.
