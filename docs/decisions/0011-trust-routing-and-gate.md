# 0011 Trust levels route updates by the design document's four levels; the release gate goes into the decision-rights configuration

## Background

The design document gives each company manager a trust level of 0–3 and states how updates are handled at each of the four levels: at level 3, updates that pass all checks are merged and published automatically; at level 2, they are merged automatically and reviewed by HQ before publication; at level 1, they stay in the private repository first and HQ reviews them item by item; at level 0, autonomy is suspended and this goes into the "For your attention" section of the letter. The level is "set by the number of audit errors in the last 8 updates and by the after-the-fact rulings on divergences".

The old `decision-rights.yml` carried this only in the description text of each level: the schema had no routing and no release rules, and it did not say how "the after-the-fact rulings on divergences" are scored, or which outputs by whom count as an "update". HQ's release step in prompt set v3 (17A) needs machine-readable `gate_rules`; the routing in 00 §G9 and 17A has to match the design document; and thesis-ci spec 0.2 accordingly added `routing` to `trust` and `gate` to the decision-rights configuration.

## Options

1. Keep things as they are: routing only in the description text, with HQ deciding releases case by case.
2. Simplify to three levels: level 2 publishes automatically, just like level 3.
3. Write the design document's four levels as machine-readable routing, plus a release gate and scoring rules.

## Decision

Option 3, written into [decision-rights.yml](../../constitution/decision-rights.yml).

- **`trust.routing`:** level 3 "merged and published automatically once all checks pass"; level 2 "merged automatically; HQ reviews before publishing"; level 1 "held in the private repository; published after HQ reviews each item"; level 0 "autonomy suspended; goes into the letter under For your attention". Pre-registrations don't go through this routing and only need to pass the format and timing checks (00 §G9): they have a deadline and can't wait for a review.
- **`trust.scoring`:** one factual error (a fact_error under 00 §F3, including those found by HQ's monthly random review) drops the level by one; a divergence map ruling that the company manager's side misread blocks any upgrade within the window of the last 8 scored outputs; the scored outputs are quarterly updates, archive builds and rebuilds, and research reports. 4 consecutive outputs with zero factual errors raise the level by one; new roles start at level 1.
- **`gate.blocking`:** a release is blocked if any of the following holds: the format checks report errors; the fact audit's must-fix items have not all been handled; a breaker has failed and has not been dealt with; a divergence touching a pillar of the thesis has been neither resolved nor flagged into the letter; the trust level is not high enough for automatic release (the review path of the routing applies). Other problems are reviewed after the merge and don't block it.
- **Who writes the levels:** levels are computed and written only by the pipeline (00 §G8): they are recorded in `trust/levels.yml`, the company manager's level is copied into `trust_level` in each company's `thesis.yml`, and the industry researcher's into `trust_level` in `industry.yml`; C-TRUST-WRITE reports any mismatch between the two places. The roles managed by trust level are the company manager and the industry researcher (`trust_managed` in `agents/*.yml`).
- `levels` and `trust` determine each role's own authority; under the amendment procedure, changing them is a decision at level L3.

## Rationale

- The four-level routing copies the design document and is a rule the owner set; written in machine-readable form, the pipeline and HQ's release step execute the same rules instead of deciding case by case.
- The release gate blocks only the kinds of problems that would let errors into the public record, and everything else is reviewed after the merge: the design document prefers the visible cost of a few bad decisions to the invisible cost of bureaucratic delay.
- Scoring that is written down is harder to game. Only factual errors cause a downgrade, because only they can be checked against primary evidence. A divergence ruled a misreading shows a problem of judgment, but divergence itself is exactly what the blind read is for, and downgrading on it would push company managers to pander; so it only blocks upgrades.
- Pre-registrations have hard deadlines, and trust routing could make them miss the deadline; their quality is ensured by the format and timing checks and by settlement afterwards.

## Rejected alternatives

- **Keep things as they are:** 17A would have no executable release checklist, and the same update could be handled differently at different times.
- **Three levels:** doesn't match the design document; level 2, "review before publication", is exactly the transition by which a new company manager moves from review to autonomy.
- **Count "no source" as an error too:** 00 §F3 counts only factual errors toward the trust level; an unsourced number is a must-fix, and it can't pass the release gate anyway until the source is added.
- **Downgrade on divergence rulings too:** see above.

## Date

2026-09-24
