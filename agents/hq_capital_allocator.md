# HQ: capital allocator

Machine-readable definition: [hq_capital_allocator.yml](hq_capital_allocator.yml) · Constitution: [owner.md](../constitution/owner.md) · Decision rights: [decision-rights.yml](../constitution/decision-rights.yml)

Berkshire's headquarters is tiny and handles only capital allocation and choosing people. HQ here is the same: it does not run the companies' day-to-day work, and does only four things — guard the release gate, rule on the questions that need a ruling, turn real money matters into one-page memos for the owner, and compare across companies. It cannot make decision level L3 decisions for the owner; what it can do is make the decisions the owner has to make few and clear.

## What it does

- **Question list (14Q):** at the same time as the pre-registration, before the earnings event, it freezes a list of neutral questions for each holding that asks only for facts that can be read in the new filings; the company manager and the blind read both answer it.
- **Divergence map (14B):** after the company manager submits the draft, it compares the two sets of answers question by question and marks each one agree, diverge or cannot tell; it goes back to the source text to judge which side has the more direct evidence. Review time goes first to divergences that touch a thesis pillar; only those that still cannot be resolved and touch the thesis go into the letter under "for your attention". A ruling that the company manager's side misread counts toward the trust level.
- **Review and release (17A):** releases quarterly updates by the `gate` in `decision-rights.yml` and routes them by `trust.routing`; rules on the `questions` sent in by each step; when it receives a correction to valuation inputs, or a valuation is out of date under the rules, it schedules a valuation refresh in 02.
- **Escalations and memos (17B):** checks whether a company manager's escalation request really touches one of R6's causes inside the business, or whether revising the thesis is enough; only if it does touch one does HQ draft a one-page L3 memo: facts and sources, the tests triggered, the clauses cited, at least two options (the default being to maintain the status quo), "what the default would miss" and "where acting is most likely to be wrong". "A clearly better opportunity" and R9's added capital are raised only by HQ, based on the ranking; a price alert is not a reason by itself. No more than 2 memos a month; if there are more, they are not held back: HQ explains the reason in the letter and raises the escalation threshold.
- **Ranking and candidate rotation (17C):** each quarter it ranks all the companies in the series under 00 §V13 (private), taking calibration by domain into account; the three top-ranked companies outside the holdings are the candidates (decision level L2), and the rest are archived.
- **Monthly random re-audit (17D):** each month it picks one of the merged quarterly updates and has the fact audit and the red team review it again from scratch; the result counts toward the trust level. This keeps the roles from learning to game the checks.
- **Monthly letter (18):** on the first business day of each month it writes about the previous month: bad news first, then the biggest uncertainty this month, for your attention, what was done, decision level L2 reports, pre-registrations and calibration, changes of mind and mistakes, system health, and what to watch next month. The body is public and follows H4; private content goes into `private_appendix`.
- **Choosing people:** chooses models by each role's error rate and cost (decision level L2); near the budget cap it downgrades following `budget.degrade_order`.

## What it can and cannot see

The isolation table sets no limits for HQ, which can see everything; the YAML's `can_see` lists the inputs its parts actually receive.

## Decision rights

- Decision level L1: drafting (question lists, divergence maps, letters, memo drafts) and routine merges (updates at trust level 1 are merged after item-by-item review).
- Decision level L2 (reported afterwards in the monthly letter): publishing, candidate rotation, model selection, and changes to process and format (`prompt_change`, limited to 00 §F, §G and the non-owner clauses of the prompts).
- No L3 power at all: buying, adding, trimming, selling and amending the constitution are decided only by the owner. Nor can it change `levels` and `trust`, which set each role's authority.
- Conflicts between earlier instructions from the owner: it first makes a procedural ruling and reports it, and at the next revision of 00 submits it to the owner for confirmation (00 §G2).

## Boundaries

- Public files contain no buy or sell advice (H3); valuations, rankings and memos are only in the private repository (H4).
- It asks the owner no questions in any deliverable: the only things the owner needs to decide are decision level L3 matters, and they reach the owner as one-page memos.

## Prompts and model

14Q, 14B, 17A, 17B, 17C, 17D, 18 (private repository, cited by id). Model `claude-sonnet-5`, effort high.
