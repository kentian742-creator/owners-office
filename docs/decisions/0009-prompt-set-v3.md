# 0009 Adopt prompt set v3 (00, 00D, 01–19), kept in the private repository

## Background

The design document requires all 13 prompts to "go on the schedule", and `agents/` to hold "each role's prompts, permissions and model". At kickoff these prompts were not in the workspace (STATUS to-do T1): the owner's original 01–13 (v2) were scattered across claude.ai conversations, and the writing and valuation rules were scattered across memory files, added one at a time, with conflicting old and new entries side by side, and the pipeline could not read them. One result was that the same rule was written differently in different reports: the grading scale for price differed from report to report, and that is how it drifted.

On 2026-09-24 prompt set v3 was finalized: 00 series rules, 00D design system, 01–13 as upgraded versions of the original prompts, and 14–19 as new ones (blind read and independent judging, pre-registration and settlement, extraction, HQ, the monthly letter, typesetting). The first draft went through four mutually independent reviews (fidelity to the method, prompt engineering, system integration, and a trial run on APP's real material) and was revised on roughly 150 findings. On the same day the owner decided that the prompts stay in the private repository for now and that the role definitions in the public repository cite them only by number; whether to publish them later will be decided separately.

## Options

1. Keep using v2: each prompt carries its own rules, with the memory files filling the gaps.
2. Adopt v3 and put it into the public repository's `agents/prompts/`.
3. Adopt v3 and keep it in the private repository's `owners-office-private/prompts/`; the public `agents/` cites it only by number.
4. Decide after the first earnings season.

## Decision

Option 3.

- **The prompt set:** 00 (series rules v3.0), 00D (design system) and 01–19, kept in the private repository's `prompts/`; the original texts of the owner's rules are in `prompts/context/`. The public repository writes only the numbers (such as `03`, `04A`, `15B`) and copies no text.
- **Rules are written only once:** 00 is the rulebook shared by all prompts, with the precedence hard rules > investment constitution > the rest of 00 > the individual prompt. 00 §C matches R1–R13 of `constitution/owner.md` in numbering and wording; §H, §C, §E, §P, §V, §M, §W and 00D are the owner's clauses, and changing them is a decision at level L3; §F, §G and the rest of the prompts are process and format, and changing them is a decision at level L2 (`prompt_change`), reported in the monthly letter.
- **Roles follow the prompts:** the roles grow from six to thirteen; `prompts` in `agents/*.yml` is registered from the table in the prompts README, and `can_see`/`cannot_see` use the input names from the prompts' front matter, checked by C-PROMPT-ISOLATION. Trade-offs settled while putting this in place:
  1. Making a valuation recomputation effective (`valuation_update`) is filed under model review: the pending version takes effect only once 04C approves it, while the recomputation itself is drafting by the company manager.
  2. The action table of the decision rights has no "settle", "extract" or "typeset": settlement is filed under `test` (judging against criteria written in advance), extraction under `parse`, and typesetting under `draft`.
  3. The extractor has `reports_to: null`: its output goes unchanged to the fact audit, and it takes no direction from the authors of the finished products; the typesetter is a production step shared by all companies and reports to HQ.
  4. The isolation table says that HQ "can see everything"; its `can_see` lists the actual inputs of each part, so that C-PROMPT-ISOLATION can check them part by part.
  5. The red team does not see the product's own bear-case content on the first pass and gets it only on the second; this ordering is guaranteed by the prompt's per-pass inputs. The two "bear-case content" inputs are listed only under `can_see`, and `cannot_see` lists the complete product, the archive and `thesis.yml`, which no pass receives. That way no part's inputs intersect `cannot_see`, and C-PROMPT-ISOLATION raises zero alerts.
  6. The industry researcher has no prompt yet; its `can_see`/`cannot_see` borrow the closest input names for now and will be corrected once its prompt is written.
- **Reproducibility:** every call records the model, the version of 00, the prompt version and the input hash (H5); output from manual runs serves only as leads, and can be merged only after the pipeline has rerun it.
- **Earlier records affected:** in [0006](0006-cite-report-pages.md), the own-report tag `<ticker>-RPT-<year-month>` changes to the 00 §E1 form `<ticker>-RPT<number>-<date>` (two own reports in the same month would collide); the `#pN` page locator stays as it is. The migration must be finished before the first pre-registration is timestamped, or the old tags will be frozen into the timestamp. The assignment principle of [0003](0003-model-assignment-and-budget.md) is unchanged, and the new roles are classified by it; its monthly cost estimate was made for the old process, and the new process adds calls for blind read, independent judging, settlement, extraction and the inversion list, so the estimate will be redone from the actual logs after the first earnings season.
- **Changes that need work in the repositories and the code** (thesis-ci spec 0.2, `pipeline/llm.py`, data and content migration) are carried out per the private repository's `prompts/INTEGRATION-TODO.md`, with progress recorded in [STATUS](../STATUS.md). STATUS to-do T1 is closed.

## Rationale

- **One rulebook.** Each rule is written once, numbered and versioned, and every call knows which version it used; the same rule is no longer written out separately in several places, so drift has no source.
- **Isolation.** Self-review within the same context is essentially ineffective. v3 splits the audit into parts that cannot see each other's results: the fact audit sees only atomic facts and the source text, the red team does not see the product's own bear-case content on the first pass, qualitative tests are judged by an independent judge, the settler sees neither the probabilities nor the author, and the blind read sees only the new filings and neutral questions. Only when this is written into the prompts and the role definitions can the pipeline trim inputs by role.
- **Machine-readable output.** Inputs and outputs use fixed envelopes; structured data conforms to thesis-ci's schemas; every hand-off channel between steps has a clear producer and consumer (00 §F7). This is the precondition for automation (Phase 2).
- **Why the private repository.** It is the owner's decision. The prompts contain the owner's complete valuation method and design preferences, and they refer to private files by name; and to explain the rules, they use many of the terms that public files forbid (H4), so putting them into the public repository would itself trip the public content checks. The public repository still shows who each role is, what it can and cannot see, and which prompt it runs, so the governance structure can still be checked.

## Rejected alternatives

- **Keep using v2:** conflicting rules would remain side by side, and the pipeline can't read memory files; self-review within the same context would also remain.
- **Publish in `agents/prompts/`:** the owner decided not to publish them for now; it would also bring forbidden terms into the public repository.
- **Wait until the first earnings season is over:** the first pre-registrations (15A) and question lists (14Q) in November already need v3; waiting would keep Phase 1 from running.

## Date

2026-09-24
