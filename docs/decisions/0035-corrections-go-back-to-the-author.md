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
