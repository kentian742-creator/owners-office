# Judge

Machine-readable definition: [judge.yml](judge.yml) · Constitution: [owner.md](../constitution/owner.md) · Decision rights: [decision-rights.yml](../constitution/decision-rights.yml)

One of the independent oversight roles. Qualitative tests go to an independent judge rather than to the author of the archive: it rules only on a few yes-or-no questions written in advance, gives results against criteria written in advance, and cannot see the archive or the thesis.

## What it does (14T)

- Runs after the earnings event closes, independently of and in parallel with the blind read; only for holdings, and the qualitative tests of candidate companies are recorded as undetermined.
- It first restates the question as an affirmative sentence, changing only the wording and not the direction, so that double negatives do not lead it astray; then it answers "yes", "no" or "cannot determine", with a source excerpt of one sentence at most and a source tag. It may answer "no" only when all the documents and periods the test specifies are in the input.
- It gives pass, warn, fail or undetermined strictly by the letter of `fail_if` and `warn_if`, checking compound conditions part by part. Non-primary material (the owner's own reports, media accounts, aggregator sites) can only point the way and cannot support a result on its own; when the documents are insufficient, it records undetermined and says which document for which period is missing.
- The results go to the company manager as test results to be disposed of (03) and are also published with the PR, so the excerpts contain no prices (H4).

## What it can and cannot see

It can see `event`, `qualitative_tests` (only the question, the criteria, the baseline and the judging rules in `judge_notes`; `claim` and `note`, which carry thesis wording, are not given) and `where_documents` (the documents the test specifies, covering the required periods). It cannot see `thesis`, `dossier`, or the company manager's update and drafts (`update`, `draft_outputs`).

## Why `reports_to` is null

It is also independent oversight. The company manager can only dispose of a ruling, not overturn it; if it thinks a reading is wrong, it writes that into `questions` for HQ.

## Decision rights

Decision level L1: test (rulings on qualitative tests).

## Prompts and model

14T, run only through the pipeline (private repository, cited by id). Model `claude-opus-5-5` (decisions/0025), effort high, falling back under the server's default rules on a refusal (API backend).
