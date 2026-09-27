# Status

- Updated: 2026-09-27
- Current phase: **Phase 1 · Run one season semi-automatically (in preparation; the Q3 earnings season starts in mid-October)**
- Portfolio: holdings APP, PDD; candidates AXP, MSFT, SPGI; BRK archived (`decisions/0008`, confirmed by the owner). The `thesis.yml` and `story.md` of all six companies are labeled accordingly.
- Next hard deadline: APP's FY2026Q3 pre-registration, to be merged by **2026-10-30** at the latest on a conservative estimate (the deadline is the end of 11-01; to be updated once APP announces its release date, to-do T11).

## Current

Phase 0 is complete: acceptance A1–A7 PASS, and the Phase 0 letter to the owner is written (`letters/2026-09.md`). The three repositories have been created on GitHub and pushed (2026-09-25), and CI passed everywhere after the push:

- thesis-ci (public), release tags `v0.2.0`, `v0.2.1`, `v0.3.0` (English first) and `v0.4.0` (the evaluation engine for quantitative tests): https://github.com/kentian742-creator/thesis-ci
- owners-office (public), CI pinned to thesis-ci `v0.4.0`: https://github.com/kentian742-creator/owners-office
- owners-office-private (private): https://github.com/kentian742-creator/owners-office-private

Owner decisions on 2026-09-25:

- **English first** (`decisions/0020`): all repositories, the prompt set and model outputs are English; Chinese versions of key documents live in `zh-CN/`. The translation is in progress (see the Phase 1 checklist).
- **Model budget $50 a month** for API calls (`decisions/0021`); candidates get quarterly updates in phase 1 as designed.
- **Model backend** (`decisions/0022`): model calls run through the Claude Code CLI on the owner's Max plan by default (`claude -p` with a replaced system prompt, no tools, chosen model and effort), still through `pipeline/llm.py`; the API is the fallback. The owner's subscription token is set up and a live call succeeded (T4, done); the API key is optional.
- **Public history** (`decisions/0023`, delegated by the owner): the history of this repository is not rewritten to remove the line the STATUS hotfix took out; the mistake stays in `mistakes.md`.
- **Prompt set v3.1** (English) confirmed by the owner.

Owner decisions on 2026-09-27:

- **Constitution v2.3:** R14 "Long-term objective and drawdowns" added (about 8–10%+ annualized compounding over time; drawdowns of 50%+ accepted while intrinsic value, fundamentals and management are not permanently impaired), until then only in the preamble; it is not a valuation parameter and not a hurdle. 00 §W gains W8 "Plain statement". To be reported in one line in the next letter (III.6).
- **One model** (`decisions/0025`): every role runs on Claude Opus 5.5 at effort high; the budget's second degrade step lowers the drafting roles' effort instead of changing model.
- **Prompt set v3.2** adopted: the audit against current Claude prompting guidance (a new opening of 00 stating what the system is for and how a call works, the owner's reasons behind §V7, §V17 and §G6, no model-computed deadlines in 15A, 15B's `reasoning` no longer asked "step by step"); front matter, roles and rule text unchanged apart from R14 and W8.
- **Series rule 00 §E10:** where there is no Form 4, use other official sources; use official data as far as possible (since 2026-03-18 foreign private issuers' insiders file Forms 3/4). Applied in the English prompt set.

## Earnings calendar (conservative estimates, 2026-09-25)

From `pipeline.edgar next-release`: three calendar days before the earliest past release of the same fiscal quarter, moved back to a business day (`placeholder: true` until the company announces the date; then rerun with `--announced`). Candidates don't pre-register; their quarterly update is due within 7 days of the filing's acceptance.

| Company | Status | Period | Expected release | Pre-registration deadline | Merge by |
| --- | --- | --- | --- | --- | --- |
| AXP | candidate | FY2026Q3 | 2026-10-14 | — | — |
| MSFT | candidate | FY2027Q1 | 2026-10-21 | — | — |
| SPGI | candidate | FY2026Q3 | 2026-10-21 | — | — |
| BRK | archive | FY2026Q3 | 2026-10-29 | — | — |
| APP | holding | FY2026Q3 | 2026-11-02 | 2026-11-01T23:59:59-05:00 | 2026-10-30T00:59:59-04:00 |
| PDD | holding | FY2026Q3 | 2026-11-13 | 2026-11-12T23:59:59-05:00 | 2026-11-09T23:59:59-05:00 |

## Phase overview

Timing and acceptance criteria are copied from the roadmap in `DESIGN.md`. Once the acceptance script passes, the next phase starts automatically.

| Phase | Timing | Deliverables | Status |
| --- | --- | --- | --- |
| 0 Skeleton, constitution and migration | End of September to mid-October | Three repositories; CLAUDE.md; schema and lint; constitution/; thesis.yml and two-minute stories for the holdings (and candidates) | Done (2026-09-25) |
| 1 Run one season semi-automatically | Mid-October to end of November 2026 | Pipeline triggered step by step; pre-registrations; the first monthly letter | In preparation |
| 2 Automation | November 2026 to January 2027 | EDGAR monitoring, automatic PRs, automerge, trust levels | Not started |
| 3 Isolation, ledgers and HQ | January–February 2027 | Audit and blind read, divergence map, say-do ledger, capital allocator, L3 memos | Not started |
| 4 Calibration and open source | From spring 2027 | thesis-ci v1.0, calibration dashboard, industry dependency graph | Not started |
| 5 Valuation configurator | From the second half of 2027 | Single-page MSFT configurator | Not started |

## Phase 1 checklist

Acceptance (`DESIGN.md` roadmap): all pre-registrations merged and timestamped before the results are released; an update merged within 7 days after each report is filed. Sorted by deadline:

- [x] T22 First fact audit of the PDD archive: 16A extracted 372 facts; 04A (six slices) found 304 accurate, 32 consistent with citation, 20 L2 only, 4 basis issues, 12 errors; HQ rulings written; revised on 2026-09-25
- [x] T18 EDGAR fetching moved into `pipeline/edgar.py` (`decisions/0017`, 64 tests). Estimates (`placeholder: true`; update with `--announced` once the company announces its date): APP FY2026Q3 release around 2026-11-02, deadline 2026-11-01T23:59:59-05:00, **merge by 2026-10-30T00:59:59-04:00 at the latest**; PDD FY2026Q3 release around 2026-11-13, deadline 2026-11-12T23:59:59-05:00, merge by 2026-11-09T23:59:59-05:00 at the latest. Expected release dates for the candidates: AXP 10-14, MSFT 10-21, SPGI 10-21 (BRK is archived)
- [x] T8 OpenTimestamps (`pipeline/timestamp.py`, `decisions/0018`): CI timestamps pre-registration files automatically once they are merged into main, and upgrades pending proofs every 6 hours; thesis-ci v0.2.1 compares hashes locally first, so an unreachable calendar server no longer causes a false error. Files in which the owner rewrites probabilities are merged at least one day early, so the Bitcoin confirmation lands before the deadline
- [x] Phase 1 acceptance script (`scripts/accept.py --phase 1`): P1 pre-registrations, P2 updates within 7 days, P3 first monthly letter, P4 phase 0 criteria; items not yet due report PENDING (exit code 3)
- [ ] English first (`decisions/0020`): thesis-ci docs and English-aware checks (release 0.3.0 with `C-LANGUAGE`); owners-office docs, constitution, agents, five archives and industries translated; the PDD archive, the private repository's documents, the prompt set (Chinese v3 kept in the private `zh-CN/prompts/`) and the pipeline's strings are done; still to do: the phase 0 acceptance report (`docs/acceptance/phase-0.md`, Chinese original to `zh-CN/docs/acceptance/`)
- [x] Model backend `claude-code` in `pipeline/llm.py` (decision 0022) and local execution in the pipeline runner; prompts at v3.1 (English, confirmed by the owner). A live call through the owner's subscription succeeded on 2026-09-25 (T4); every prompt part validated with the fake backend
- [x] Earnings calendar: the next expected release date of each of the six companies (from `release_history` and company announcements), with deadlines updated as the companies announce their dates
- [x] Post-earnings pipeline, phase A: the candidate path (`decisions/0024`): 16B metric extraction; the evaluation of the quantitative tests (`python -m pipeline.runner evaluate`, recorded like a run, on thesis-ci's `evaluate_company`); 14T with a resolver for each test's `where` and `lookback`; 14A; the 03 draft, 16A in update mode, 04A with pipeline-cut source excerpts, 03R; `place` with every §F2 action and §G9 staging (`place --publish` after HQ's review); the event chain (`python -m pipeline.runner event <TICKER> <PERIOD> --run-date D [--dry-run] [--approve ...]`) with review stops after the draft, the audit and before placement; structural checks of the YAML outputs later steps read; the claude-code backend's output limit. Dry-run end to end on AXP FY2026Q3 (a rehearsal on the FY2026Q2 filings until the event exists)
- [ ] Post-earnings pipeline, phase B: the holdings' steps 04B-lite, 14B, 15B (with the settlement file) and 17A, by 2026-10-28 (APP reports around 11-02). Also: thesis-ci's evaluation interface released and pinned (`feat/evaluate`; until then only dry runs evaluate, with a placeholder), the second audit round after 03R, and a second 03R when HQ returns an update
- [ ] T11 APP FY2026Q3 pre-registration (results around early November): freeze the question list → write the pre-registration → merge and timestamp at least 72 hours before the deadline
- [ ] T11 PDD FY2026Q3 pre-registration (results around late November, 6-K)
- [ ] Quarterly updates: merged within 7 days after each report is filed; company managers start at level 1, so updates stay in the private repository first and are published after HQ review (00 §G9)
- [ ] T12 The first monthly letter (covering October), by November 2 at the latest
- [x] T22 AXP and MSFT fact audits (2026-09-27): AXP 338 facts, 17 errors, revised (test AXP-Q8 superseded by AXP-Q17 from FY2026Q4; baselines of AXP-Q4 and AXP-Q10 corrected); MSFT 460 facts, 41 errors (one later withdrawn), revised (baselines of MSFT-Q12, MSFT-Q4's history and MSFT-L3 corrected). No threshold of a test in force changed. Both are in `mistakes.md`; the report errors are in the private errata table
- [ ] T22 SPGI (448 facts extracted; audit next) and BRK (extraction next)
- [ ] Open from the AXP and MSFT audits: AXP-Q4's data basis (XBRL tags vs reported capital returns) decided by the FY2026Q4 update at the latest; MSFT's capital-allocation grade re-examined in the FY2027Q1 update (the incremental return now reads 23.7% and 38.9%, above the report's 20% upgrade line)
- [ ] T21 Source for price references; T17 source for earnings call transcripts

## Phase 0 checklist (done)

- [x] Skeletons of the three repositories, CLAUDE.md, `pipeline/llm.py`, `scripts/accept.py`, public CI, private repository skeleton; `docs/DESIGN.md`
- [x] Prompt set v3 (00, 00D, 01–19) finalized and kept in the private repository's `prompts/` (`decisions/0009`)
- [x] `constitution/`: `owner.md` rebuilt from the full text of the owner's constitution as R1–R13 (consistent with 00 §C) + H1–H5; `rules.yml`, `decision-rights.yml` (spec 0.2) and `masters.md` updated to match
- [x] `agents/`: thirteen roles; visibility uses the prompts' input names, checked part by part
- [x] Decision records `0001`–`0014` (0002 and 0005 superseded; 0004 and 0006 partly amended)
- [x] Outdated wording in CLAUDE.md and the README, and the H4 terms in `decisions/0004`, corrected; `trust/levels.yml` created, with every role starting at level 1
- [x] **`pipeline/llm.py` upgrade** (formerly to-do T10, `decisions/0015`): roles come from `agents/*.yml`; system prompt = 00 (plus 00D for the typesetting parts) + the prompt, with 00 and 00D going through prompt caching; inputs are trimmed to the role's visibility; outputs are parsed per the format declarations of §F0, structured outputs must pass the schema first, and a failing output gets one retry carrying the errors; placements are given per §F2; the log records the model, the versions and revisions of 00 and the prompt, the input hash and cache usage. 130 tests pass; all 40 parts were run with the real prompts and a fake client.
- [x] **thesis-ci spec 0.2**: schemas, `checks.yml` (34 checks) and the check implementations, SPEC, CHANGELOG; 687 tests and selftest 34/34 pass. C-HURDLE and C-CONCENTRATION corrected to follow the owner's original text (`decisions/0014`); the price-multiple check for public files now also covers price-to-book and phrasings like "price below N times book".
- [x] **Content migration**: the six companies' `thesis.yml`, `story.md`, `sources.yml`, `ledger.yml` and the private `valuation.yml` migrated to spec 0.2; all EDGAR accession numbers backfilled; source tags renamed per SPEC §3.3 (own reports `<ticker>-RPT1-<date>`, periodic reports by fiscal period, current reports by EDGAR filing date); the grades given to prices recomputed with the §V11 mechanical scale (two companies each moved one step; details only in the private repository); the six open fixes in the APP draft and the three price-derived numbers in BRK's public files dealt with. Compared item by item: apart from the rewrites already explained (APP-L4, APP-L5, BRK-Q7, BRK-Q8), no test's threshold, direction or resolution sentence changed. lint: public repository zero errors and zero warnings; private repository zero errors and one warning (SPGI's Treasury yield is the midpoint of two dates and has no single observation date).
- [x] **APP audit**: the six items found in two trial runs were corrected during the migration; 16A extracted 237 facts and 04A checked them independently in four batches: 202 accurate, 7 errors (really two problems: debt principal and net debt copied the report's rounded figures, and the opening share count was taken from the wrong year), and the rest consistent with the report only, secondary only, basis issues or unconfirmed. Of 57 conclusions, 45 were fixed and 12 needed no change; the valuation numbers were not changed and went into T15.
- [x] Integration: `git init` and first commit in all three repositories (local, not pushed; the author email is the GitHub noreply address)
- [x] Acceptance: `scripts/accept.py --phase 0` A1–A7 all PASS (2026-09-25); the report is in `docs/acceptance/phase-0.md`
- [x] First entry in `mistakes.md` (the errors found by APP's first fact audit)
- [x] Phase 0 letter to the owner (one page at most): `letters/2026-09.md`; the private appendix is in the private repository at `letters/2026-09-private-appendix.md`

## To-dos

| # | Item | Who | Notes |
| --- | --- | --- | --- |
| T4 | Model access for pipeline runs | Owner | Done 2026-09-25: the subscription token from `claude setup-token` is in the workspace `.env` (outside every repository) as `CLAUDE_CODE_OAUTH_TOKEN`, and a live call succeeded. The pipeline runs the CLI with an empty configuration, so the account e-mail is not sent to the model. The token lasts a year; renew it with the workspace script `setup-claude-token.sh`. The API key is optional (fallback only, decision 0022); if used, it goes into the private repository's Actions secrets as `ANTHROPIC_API_KEY`. |
| T5 | Full public MSFT archive | System | Phase 1; the valuation section contains no price-derived numbers (`decisions/0004`). |
| T7 | Persisting the model-call log | System | Phase 2; point the log at the private repository with `OWNERS_OFFICE_LLM_LOG`. |
| T11 | The first pre-registrations | System | APP FY2026Q3: merge by 2026-10-30 at the latest (conservative estimate, to be updated once APP announces its release date); freeze the question list (14Q) first, then write the pre-registration (15A). PDD FY2026Q3: merge by 2026-11-09 at the latest. Timestamps, the EDGAR module, the source tag migration and the `llm.py` upgrade are all done; the pipeline runs locally on the owner's subscription (T4 done); the post-earnings chain runs for candidates, and the holdings' last four steps are phase B (`decisions/0024`). |
| T12 | Monthly letter | System (HQ) | 18 writes up the previous month on the first business day of each month; the first one (covering October) goes out by November 2 at the latest. |
| T13 | The "quality-to-premium mapping table" for Phase 5 in the design document | Owner (before Phase 5) | Conflicts with 00 §V1 "no lookup tables, no fixed tiers"; not written until the owner decides otherwise (`decisions/0012`). C-HURDLE and C-CONCENTRATION have been corrected to follow the original text (`decisions/0014`). |
| T15 | Valuation refresh pending review (04C) | System | Valuation method issues found during the migration, which by rule were not changed in the migration, are recorded in the todo of each company's private `valuation.yml`, to be handled at the next 02 valuation refresh and reviewed by 04C. BRK comes first (its return estimate is the first hurdle for every company, §V6): a possible §V3 double discount, and the premium lacks a §V1 basis. SPGI: the premium is interpolated between other companies (§V10). PDD: whether the premium contains a round-number target return, whether the starting point is discounted twice, whether the 2025 fine is normalized. APP: the premium starts from the market-wide equity premium (§V1, §V10). AXP: one edge of a valuation range is computed differently from §V7 (details in the private file). |
| T16 | Price lines of BRK-Q7 and BRK-Q8 | HQ | Under §H4 the thresholds of these two breaker lines are only in the private valuation file; resolving them needs period-end prices. Year-end closing prices are category 2 of the prices allowed by §H2, but quarter-end prices are not among the four categories, so HQ has to write a decision record. BRK is archived; low priority. |
| T17 | Source for earnings call transcripts | System | EDGAR does not carry earnings calls; MSFT's FY2027 guidance, SPGI's hyperscaler issuance volume and the like come only from calls, and are currently cited through the report pages that paraphrase them. Decide where transcripts come from (the replays and transcripts on the companies' IR websites) and register the source. |
| T19 | Candidates for spec 0.3 | System | Qualitative tests can also have `evaluate_on` (PDD-L7 needs it); APP-L5(b) is recorded as undetermined every quarter until a citable, freezable list of top publishers is found. |
| T20 | Runtime environment for typesetting (19) | System | 19 produces files (PDF and page images), so `llm.py` cannot run it directly; before Phase 2, decide where to execute it (an environment that can run code), then connect design review. |
| T21 | Source for price references | System | §H2 requires price references to be supplied by the pipeline with a date and a source. The price references in the existing six valuations come from the reports, and APP's could only be traced to third-party daily price data (secondary). Before the first valuation refresh, decide where official exchange closing prices come from; they are written only into private files. |
| T22 | First fact audit of the other five archives | System | ★ In Phase 0 only APP went through 16A → 04A → revision. PDD is a holding and goes first, in time for its pre-registration in late November; AXP, MSFT, SPGI and BRK are done before their own first pre-registration or next update. The process follows APP's: extraction, independent checking in shards, HQ rulings, revision by the company manager, recorded in the private repository's `runs/`. |
| T23 | thesis-ci backlog from the phase 1 acceptance work | System | (1) candidates have no event record: wire `accept.edgar_acceptance()` to `pipeline.edgar` or add an event file; (2) SPEC: private staging path for level-1 updates; (3) optional `period:` in update front matter; (4) require ISO 8601 with offset for `acceptance_datetime` (the compact form is read as naive); (5) record the owner file's merge time; (6) nothing checks that the Bitcoin attestation predates the release; (7) company status is read as of now, not as of the event. |

## How to pick up the work

When the owner says "continue" in a new session, read this file first and start from the first unfinished item in the "Phase 1 checklist", nearest deadline first (APP's pre-registration deadline is around early November). Tick each item here when it is done, and write new autonomous decisions into `docs/decisions/`; stop to ask the owner only about money matters and constitutional amendments, and record things only the owner can do in the to-do table. Once the Phase 1 acceptance script is written, use it to decide whether Phase 2 can start.
