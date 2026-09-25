# 0002 Holdings and candidate list

> **Superseded by [0008](0008-portfolio-app-pdd.md) (2026-09-24):** the owner confirmed the holdings as APP and PDD; the candidates are AXP, MSFT and SPGI; BRK is archived. This record is kept for history.

> Status: **pending review**. Later on the day the decision was made, APP's company report appeared in the workspace; see "New evidence" at the end.

## Background

The Phase 0 task in DESIGN.md is "generate thesis.yml and two-minute stories for the 4 holdings", and its example uses AXP. At kickoff the owner gave the holdings as "APP" and PDD. At kickoff the workspace had five company reports (02 reports): MSFT, AXP, PDD, BRK and SPGI, and no report on a company with the ticker APP. The rule for candidate companies is "HQ ranks companies against the entry bar of rule 4 of the constitution, takes the top three and rotates them automatically each quarter", but the ranking has to wait until the capital allocator comes online in Phase 3.

We need to decide which company "APP" refers to, who the holdings and the candidates are, and which companies get archives in Phase 0.

## Options

1. Treat "APP" as another company with the ticker APP and start a separate archive for it.
2. Read "APP" as a typo for AXP: at kickoff only the AXP report matched, and the thesis.yml example in DESIGN.md is also AXP (depending on the payments and card networks industry module, whose industry research happens to be in the workspace).
3. Stop and ask the owner.

## Decision

Option 2, with a request for the owner to confirm.

- Holdings (`status: holding`): AXP, PDD.
- Candidates (`status: candidate`): MSFT, BRK, SPGI, that is, the other three company reports in the workspace. The list does not rotate until the Phase 3 capital allocator comes online; after that, HQ ranks and rotates it every quarter as DESIGN.md describes.
- In Phase 0 all five companies get thesis.yml, story.md and sources.yml; the acceptance criterion "at least 5 thesis tests per company" applies to all five. Candidate companies run only the quantitative tests and the company manager's draft (the DESIGN.md cost rule), with no pre-registration and no blind read.
- If the owner did not mean AXP: change AXP to `candidate` or `archive`, and create thesis.yml, story.md, sources.yml and the private valuation.yml for that company. The change is an ordinary commit and involves no money.

## Rationale

- At kickoff only the AXP report matched, and DESIGN.md itself uses AXP as its holding example; starting a separate APP archive with no material at hand could only mean making numbers up, which breaks "never make up numbers".
- Recording holding status is neither a money operation nor a constitutional amendment; under the scope of authority, work should not stop for it. The cost of a misunderstanding is small: changing one field.
- With archives for all five, the candidates have something to be compared with the holdings on, in line with the constitution's opportunity cost principle.

## Rejected alternatives

- **A separate APP archive** (at kickoff): with no report, the numbers could only be made up or left empty, and no thesis tests could be written.
- **Stop and ask**: this is not one of the two kinds of matters that must be escalated, and it would hold up all of Phase 0, while Phase 0 has to be finished before the first holding reports results.
- **Archives only for the two holdings**: candidate companies without archives can't be compared, and the Phase 3 ranking would have no starting point.

## Consequences

DESIGN.md's success criterion "about 80 settled system forecasts by the end of 2027" was estimated for four holdings; now only two holdings are pre-registered, so the sample is roughly halved and calibration conclusions by domain will come later.

## New evidence (2026-09-24, after the decision)

While Phase 0 was under way, `APP.pdf` appeared in the workspace's `inputs/reports/`, together with its extracted text: a complete company report on AppLovin (Nasdaq ticker APP), dated 2026-09-23, in the same format as the other five 02 reports. A copy has been put into the private repository as `reports/APP.pdf`, tagged `APP-RPT-2026-09`, and work on an archive for APP (`companies/APP/`) has begun.

This overturns the main basis for option 2 ("only the AXP report matched"). The most direct reading now is that the owner's "APP" is AppLovin.

Recommendation (for the integration step to decide, with confirmation from the owner):

- Change the holdings to APP and PDD. APP's thesis.yml gets at least 5 tests covering all three kinds, plus story.md, sources.yml and the private valuation.yml; APP's business does not belong to any of the three existing industry modules, so `depends_on` can be empty.
- Keep the AXP archive and change its status to `candidate`. Until the Phase 3 ranking goes live, the candidates are provisionally the four companies AXP, MSFT, BRK and SPGI; once it is live, take the top three per DESIGN.md.
- Once the list is settled, write a new decision record that supersedes this one and note "superseded by NNNN" at the top of this record; also update the portfolio line and to-do T9 in `docs/STATUS.md`.

## Date

2026-09-24
