# 0016 Trust levels are scored from the pipeline's first output

## Background

The trust level rules (`trust.scoring` in `constitution/decision-rights.yml`, 00 §G9) are: one factual error drops the level by one, four consecutive outputs with zero factual errors raise it by one, and new roles start at level 1; the scored outputs are quarterly updates, archive builds and rebuilds, and research reports.

On 2026-09-25 the APP archive went through fact extraction and fact audit by the prompts for the first time (16A → 04A): of 237 facts, 7 were judged "error", which were really two problems: debt principal and net debt copied the rounded figures in the owner's report, and the opening share count was taken from the wrong year. Both came from the report itself; when the archive was built, EDGAR was not yet reachable, and the numbers were taken from the report without being checked one by one against the primary filings.

Scored literally, APP's company manager would drop from level 1 to level 0 before any pipeline run: autonomy suspended, and every output would need the owner's own review.

## Options

1. Score literally: APP's company manager drops to level 0.
2. The six archives built by hand in Phase 0 are not scored, and scoring starts with the first output of a pipeline run; the errors still go into the public mistakes list as usual.
3. Score, but defer the downgrade until Phase 1 starts.

## Decision

Option 2.

- The archives put together during the Phase 0 engineering setup (the six companies' `thesis.yml`, `story.md`, `ledger.yml`, `sources.yml` and the private `valuation.yml`) do not count toward any role's trust level.
- Scoring starts with the first scored output produced in a role's name by a run of `pipeline/llm.py`.
- The errors found by this audit are corrected as usual and recorded in `mistakes.md`: they are the system's errors, just not charged to a role that had not started working yet.

## Rationale

- A trust level measures a role's own work. When the six archives were built, the system of roles was not running yet and SEC data could not be fetched; charging errors from that period to the company manager would measure the engineering process, not this role.
- Option 1 would suspend the autonomy of a holding right at the start of the first earnings season, and the owner would have to review every output, although the errors did not come from this role's judgment.
- The public record is not watered down: the errors go into the mistakes list and are reported in the letter as they are.

## Rejected alternatives

- **Score literally:** punishes a role that has had no chance to make a mistake, and pulls the owner into routine work the system should handle.
- **Defer the downgrade:** only pushes the same problem further down the road.

## Date

2026-09-25
