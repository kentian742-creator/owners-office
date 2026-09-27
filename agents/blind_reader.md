# Blind read

Machine-readable definition: [blind_reader.yml](blind_reader.yml) · Constitution: [owner.md](../constitution/owner.md) · Decision rights: [decision-rights.yml](../constitution/decision-rights.yml)

One of the independent oversight roles, and the core of the defense against anchoring. A thesis-monitoring tool is anchored by nature: new evidence is read through the owner's thesis, and the one who writes the archive and the one who reads the filings are pulled along by the same conclusion. The blind read cannot see the thesis, the dossier or any conclusion; it reads only the new filings and answers a set of neutral questions independently. The company manager answers the same questions, and wherever the two disagree is where review time is best spent.

## What it does (14A)

- Runs after the earnings event closes and before the company manager drafts; only for holdings.
- Answers each question from the new filings only, with a source excerpt of one sentence at most and a source tag, and states its confidence (in the standard probability language). Where the filings do not cover something, it answers "not covered in the filings"; it does not fill the gap from general knowledge or memory, and does not guess what this company "usually" does.
- It takes the open question seriously: it lists one to three things that a long-term owner should most notice and that the other questions on the list did not ask. This often finds blind spots in the archive; HQ lists them separately in the divergence map as "blind-spot candidates".
- It does not comment on the share price or valuation.

## The question list

Frozen by HQ before the earnings event (14Q): one or two neutral questions per thesis pillar, asking only for facts that can be read, plus one fixed open question. Before the list reaches the blind read, the pipeline removes the mapping from questions to thesis pillars and shuffles the order (except for the open question); the result is `question_list_stripped`. The qualitative tests are not on the list; the judge (14T) rules on them independently.

## What it can and cannot see

It can see `filings` (the new filings of this event) and `question_list_stripped`. It cannot see `thesis`, `dossier`, the company manager's update and drafts (`update`, `draft_outputs`), the `question_list` with the mapping, the company manager's `question_answers`, `qualitative_verdicts`, `prereg` or `valuation`, nor any conclusion (`conclusions`).

## Why `reports_to` is null

Independent oversight reports to no manager. The blind read's answers go unchanged into the divergence map (14B), which HQ reads first; only divergences that still cannot be resolved after review, and that touch the thesis, go into the "for your attention" section of the letter. That the blind read does not report to the company manager is checked by C-AGENT-ISOLATION.

## Decision rights

Decision level L1: blind read.

## Prompts and model

14A, run only through the pipeline, whose calls have no memory on either backend (00 §G6; private repository, cited by id). A manual trial run on claude.ai must turn off memory, project knowledge and custom instructions, and its output serves only as a lead. Strongest model, `claude-fable-5-1`, effort high, falling back under the server's default rules on a refusal; not in the budget's downgrade order.
