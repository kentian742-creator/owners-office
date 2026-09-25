# 0010 The price grading scale, the cover title, and the one sentence on the entry bar

> This record is a public file: under H4 it states no company's result on this scale and uses none of the terms forbidden in public files; the scale itself is used only in private files.

## Background

The owner's reports give letter grades on five dimensions: business, management, capital allocation, culture and price. The first four are judgments; for the price dimension, the owner's valuation rule 11 requires the grade to come from a written, mechanical scale, not from judgment. But earlier reports each used their own scale, some with plus and minus signs: the same rule was written separately into several prompts and gradually drifted (see [0009](0009-prompt-set-v3.md)).

Prompt set v3 writes this scale into 00 §V11, once. At the same time three related things had to be decided: the main title on the research report cover had a different form in each of three periods, 09-16, 09-20 and 09-23; earlier material contained a rule "no new money inside the fair range"; and company-level deliverables don't state positions, but whether a private research report may say one sentence about the entry bar had not been settled.

On 2026-09-24 the owner confirmed the four points below.

## Options

1. The price grade is given by judgment, and reports may differ.
2. One mechanical scale, with plus and minus signs allowed as finer steps.
3. One mechanical scale, five grades, no plus or minus signs (the version in the MSFT report the owner endorsed).

For the cover, for "no new money inside the fair range" and for the one sentence on the entry bar, the two options in each case are "keep the old wording" and "standardize on the wording the owner confirmed".

## Decision

Option 3; the other three are standardized on the wording the owner confirmed.

1. **The price grade comes from the mechanical scale and looks only at "price ÷ central value":**
   - A: no higher than the top of the company's range derived from its margin of safety, where top = central value × (1 − minimum margin of safety), 0.75 by default; a price below the bottom of the range is also A;
   - B: ≤ 1.00; C: ≤ 1.08; D: ≤ 1.15; E: > 1.15.
   - No plus or minus signs, and no adjustment by judgment. The four dimensions business, management, capital allocation and culture may still use finer grades such as A− and B+.
   - This scale is used only in private files; each company's result re-graded on it is written in the private files and given at the next update. The price reference the scale uses carries a date and a source and is supplied only by the pipeline, as an input (H2).
2. **Cover:** the main title is the company name. Where the official wordmark is itself the company name, the enlarged wordmark serves as the title; where it isn't (such as Pinduoduo's heart-shaped logo), the company's Chinese name is the title and the wordmark goes at the top right. The thesis is the subtitle. Wordmarks are used only in private reports; the public repository stores no logos.
3. **Drop "no new money inside the fair range".** Whether to put in new money is decided by the owner on a one-page memo drafted by HQ (decision level L3). The side of R1 that says "a fair price is not any price; a margin of safety is still required" stays as it is.
4. **The private research report (02) and the complete company report (11) may contain one sentence on the entry bar:** whether the company qualifies for the 10–20% entry bar, and where it falls short (business, management or price). No specific position size and no position ladder.

## Rationale

- **A mechanical scale is reproducible and auditable.** The same inputs give the same letter whoever computes it; model review (04C) can check each item, and reports no longer drift apart.
- **No plus or minus signs.** Price ÷ central value already carries the error band of the valuation; finer steps would only be false precision.
- **The MSFT version.** It is the wording of the report the owner endorsed; the top of grade A follows the company's own margin of safety instead of one number for the whole series, consistent with "the margin of safety moves up or down a step with predictability" (00 §V2).
- **Cover:** each of the three periods had a different form; with the company name as the title, readers know at a glance which company it is, and the thesis goes into the subtitle.
- **Drop "no new money inside the fair range".** It is a mechanical ban on buying, which conflicts with R1's "don't wait mechanically for an extreme low price"; and whether to put in new money is a decision at level L3 anyway, which a report rule should not make for the owner in advance.
- **The one sentence on the entry bar.** The owner needs to know whether a company qualifies and where it falls short; saying only this one sentence doesn't turn the report into position advice (00 §C on position size).

## Rejected alternatives

- **Given by judgment:** violates the owner's valuation rule 11; the results can't be reproduced, and this is exactly where the drift came from.
- **Mechanical scale with plus and minus signs:** false precision; and the boundaries for the signs would need yet another set of rules.
- **Keep each period's cover wording:** that is exactly why three forms coexist.
- **Keep "no new money inside the fair range":** see above.
- **Research reports say nothing at all about positions:** the answer the owner most wants, "does it qualify", would have nowhere to go.
- **Publish each company's result:** violates H4.

## Date

2026-09-24
