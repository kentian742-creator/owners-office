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

### 2026-09-25 · APP · Factual error

- Judgment at the time: the APP archive built in Phase 0 (`companies/APP/thesis.yml`) copied the rounded figures in the company report for debt principal and net debt; for the change in share count, it took the opening figure from the following year's year-end; the five-year reading for management criterion (a) mixed two sets of numbers, from before and after a restatement.
- What actually happened: the first fact audit checked 237 facts one by one against the 10-K and 10-Q filings; seven were judged errors, which were really two problems (debt principal and net debt; the opening share count), plus one basis issue. The rounded debt figure in the report came from remarks made on an earnings call and does not match the filings.
- What went wrong: when the archive was built, SEC's EDGAR was not yet reachable, so the numbers were taken from the report without going back to the primary filings one by one; and the archive did not go through a fact audit before it was merged.
- Whose mistake: the system's archive-building process (the Phase 0 engineering setup, which belongs to no role and, per `docs/decisions/0016`, does not count toward any trust level); the root cause is the rounded figures in the owner's report.
- What changed: the archive was corrected against the primary filings (commit d471ebb); the report text is left unchanged and the differences are logged in the errata table in the private repository; the archives of the other five companies get the same fact audit before their first pre-registration, and from now on every archive goes through a fact audit before it is merged.
