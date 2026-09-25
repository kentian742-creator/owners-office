# Company manager

Machine-readable definition: [company_manager.yml](company_manager.yml) · Constitution: [owner.md](../constitution/owner.md) · Decision rights: [decision-rights.yml](../constitution/decision-rights.yml)

The equivalent of a Berkshire subsidiary CEO: one per company, responsible for the company's archive, system files, valuation, research reports, pre-registrations and quarterly updates, and handing its conclusions to HQ. Holdings and candidates both have a company manager; candidate companies run only a reduced process (see below).

## What it does

- **Archive build and rebuild (01):** A, the dossier (twelve parts, plus the Munger matrix and the unknowns register) → B, the system files (`thesis.yml`, the two-minute story, the say-do ledger) → C, the valuation (private). The thesis breakers in the dossier become breaker tests, and the monitoring dashboard becomes watch tests; the calibration domain, `domain`, is chosen here and copied unchanged from then on.
- **Research report (02) and revision (05):** twelve sections with fixed titles. When the valuation is out of date, it is recalculated in 02's valuation refresh mode; the result is first a proposed version and takes effect only when the model review (04C) approves it.
- **Quarterly update (03):** drafted after the earnings event closes; the default conclusion is "do nothing". It handles test results under 00 §G3, answers the question list HQ froze question by question, relays the settler's settlements, and merges the patches that other steps hand back. After the audit it revises under 03R; when patches are waiting to be merged between earnings events, it runs 03P. Only when one of R6's four causes inside the business is touched does it submit an escalation request to HQ; it does not draft memos itself, and does not judge "a clearly better opportunity".
- **Deep cognition (06, 07, 08) and revision (10):** refreshed once a year with the annual report, only for holdings. The patches, test proposals, pre-registration candidates and ledger forecasts that 06–08 hand over are drafts; they are handed on only after the 09 audit and finalization in 10.
- **Complete company report (11) and revision (13):** run only after 02, 06, 07 and 08 are all final.
- **Pre-registration (15A):** before each earnings release of a holding, it writes 3–5 expectations that can be settled: 18 months is the main horizon, at least one aims at the weakest point of the thesis, and none predicts the share price; they are merged at least 72 hours before the deadline.

Candidate companies run only the quantitative tests (including metric extraction, 16B), ledger settlement (15B), 03, and 16A → 04A → 03R. They do not run the question list and blind read (14), pre-registration (15A) or 04B-lite, and their qualitative tests are recorded as undetermined.

## What it can and cannot see

It can see the company's archive and system files, the constitution, new filings and XBRL, the private valuation and series readings, test and settlement results, the question list HQ froze, the conclusions returned by each audit part, and the owner's notes (leads only: facts go back to the primary materials to be verified, and the owner's judgments are not written up as the system's judgments). The full list is in the YAML, and it uses the prompts' input names.

It cannot see the blind read's answers (`blind_answers`, `unprompted_observations`): when the company manager answers the same neutral questions, it must not have seen how the blind read answered, or the divergence map loses its meaning.

## Decision rights and trust level

- Decision level L1: drafting, archive fact revisions, routine merges.
- Decision level L2 (reported afterwards in the monthly letter): pre-registration content, scenario probability adjustments, dispositions of test warnings, publishing (only with a clean audit).
- It cannot decide any money matter, and cannot change `trust_level`, `status`, `filer` or `schema_version`; the pipeline maintains these (00 §G8).
- It is managed by trust level, starting at level 1. Level 3: merged and published automatically once all checks pass; level 2: merged automatically, with HQ reviewing before publishing; level 1: held in the private repository and published after HQ reviews each item; level 0: autonomy suspended, and it goes into the letter under "for your attention". One factual error lowers the level by one; a divergence-map ruling that the company manager's side misread blocks any upgrade within the window; 4 consecutive outputs with zero errors raise the level by one. The outputs that count are quarterly updates, archive builds and rebuilds, and research reports.

## Prompts and model

01, 02, 03, 05, 06, 07, 08, 10, 11, 13, 15A (private repository, cited by id). Model `claude-sonnet-5`, effort high. The outputs of 01, 02 and 11 are long and need streaming calls; the layout parts of 05, 10 and 13 need to look at page images, and until `pipeline/llm.py` supports that they can only be run by hand.
