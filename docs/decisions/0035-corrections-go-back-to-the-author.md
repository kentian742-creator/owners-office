# 0035 A revision's corrections go back to its author

> Follows [0034](0034-every-figure-checked.md). Code: `_existing_dossier` in `pipeline/registry.py`.

## Background

The fact-check gate (0034) stops placement while any figure is judged wrong, until the text is corrected and the item
no longer appears. It did not say who corrects the text:

- Placement writes a run's outputs as the model wrote them. HQ may only add source tags (`tag_repairs.yml`), so that
  an output another role approved cannot change in substance.
- A rerun of 01A's revision (round 2) revised the first draft again. Correcting a few figures that way redoes the whole
  revision, and the new text can carry new errors elsewhere.

On 2026-10-11 the gate's review of Costco's revised dossier found six wrong figures, which were three errors each stated
twice, and a number of lines without their own source tags.

## Decision

1. **Corrections of substance go back to the role that wrote the document**, as a valuation that 04C returns goes back
   to 01C (00 §V20). HQ does not edit a run's output, at placement or before it.
2. **A rerun of a revision that has succeeded revises that revision.** For `assemble 01A <TICKER> <PERIOD> --round 2
   --rerun`, `existing_dossier` is the latest succeeded revision of the period, and its note names it. When no
   revision has succeeded, the rerun revises the first draft, as before.
3. **HQ's rulings list each correction:**
   - the passage to change;
   - the corrected text;
   - the filing that settles it.

   The ruling tells 01A to change nothing else.
4. **The gate checks the corrected dossier.** An item whose document, figure and context are unchanged keeps its
   verdict (0034, item 3), so only the corrected passages need review.

## Amendment, 2026-10-11: a valuation 04C approved

HQ recomputes every valuation from its own formulas and inputs before placing it (0034). On Costco's valuation, the
recompute of the version 04C approved found figures 04C had passed: a present value dated today that is the value at
the fiscal year-end; sensitivity multipliers on two bases that put a reversed value at $588 instead of $584; and four
products off by up to 24 in the backtest. Placement would stop on them, and nothing let the valuation go back to its
author.

- **A rerun of 01C corrects the version 04C approved,** when three conditions hold: 04C's decision names that version
  (`version` in valuation_decision), it is not yet placed, and the run is a rerun (`assemble 01C <TICKER> <PERIOD>
  --rerun`). Its `valuation` input is then that version, and the note says it is under correction. There are no 04C
  findings, and HQ's rulings list the corrections as for 01A.
- **04C reviews the corrected version** like any other, and placement still needs its approval of that very run.
- **An effective valuation,** one already placed, is corrected by HQ with a ruling, as placed documents are.
