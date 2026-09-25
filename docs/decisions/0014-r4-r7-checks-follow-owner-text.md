# 0014 The concentration and hurdle checks follow the owner's original text

> Closes the first two items of STATUS to-do T13. This record states no company's numbers.

## Background

When the constitution was rebuilt (`owner.md` v2), two checks turned out to be stricter than the owner's original text.

- **R4 Concentration and position size.** The original text says "typically around 4–5 core holdings" and "10–20% is a qualification threshold for buying at all, not a mandatory allocation target", and the interpretation adds that businesses understood more deeply, with stronger fundamentals and more attractive risk/reward, can get larger positions. Yet C-CONCENTRATION in thesis-ci 0.2 required the target position of buy and add memos to fall within 10–20%, and treated more than 5 holdings as an error; HQ's prompt 17B also said "within the 10–20% entry bar".
- **R7 Opportunity cost.** The original text is "clears Berkshire or VOO as the comparison bar". The engineering default in 00 §V6 is that Berkshire's long-term return estimate at the current price is the first hurdle, and the index's forward return is the second reference. Yet C-HURDLE took the higher of the two as the hurdle.

## Options

1. Keep things as they are, and ask the owner to confirm at the next revision of 00.
2. Change the checks and 17B to follow the original text: below the bottom of the entry bar is an error, above the top is allowed with the reasons stated; the number of holdings only warns; the hurdles are applied in the §V6 order.
3. Downgrade both checks to warnings.

## Decision

Option 2. This brings the implementation back to the original text; it is not a new investment judgment, so it needs no separate decision by the owner; the owner can still overturn the §V6 order of the hurdles at any time.

- **C-CONCENTRATION:** a target position below 10% in a buy or add memo is an error (if a business isn't worth a meaningful position, it isn't worth owning at all); above 20% is allowed, but the memo's `weight_note` must state why this company deserves a larger position, and a missing note triggers a warning. More holdings than `max_holdings` is a warning, not an error; a `max_holdings` above 5 also only warns. `entry_band` must still equal [0.10, 0.20]: it is a bar, not a target.
- **C-HURDLE:** the Berkshire hurdle is the first hurdle; the long-term expected return in a buy or add memo must be above it, and a memo may not set its own hurdle below it, or it is an error; passing only the first hurdle and not the VOO reference is a warning, and the memo states the reasons. When there is no Berkshire number, VOO is the hurdle. A holding that falls below either line is a warning, judged by HQ in the quarterly ranking (17C); the warning itself is no reason to escalate (00 §G3).
- **17B:** the target position is "no lower than the 10% entry bar; state the reasons when above 20%".
- thesis-ci's `memo.schema.json` gains the optional field `weight_note`; the descriptions in `checks.yml` and `rules.yml` and "How the system enforces it" in `owner.md` are rewritten to match.

## Rationale

- A check has to enforce the owner's rules, neither stricter nor looser. The original text says plainly that 10–20% is a bar, not a target, and that higher-conviction businesses can be larger; a hard cap would keep decisions the text allows out of CI.
- "Around 4–5" is a description, not a hard cap; treating the appearance of a sixth company as an error would turn a question for the owner's judgment into a format error.
- "Berkshire or VOO" does not say to take the higher one. Taking the higher one would quietly raise the hurdle whenever the index's forward return is above Berkshire's.

## Rejected alternatives

- **Keep things as they are:** every day the checks disagree with the original text, they may block a decision the owner would allow.
- **Downgrade all to warnings:** falling below the entry bar and failing the first hurdle are cases the original text explicitly rules out, and they should be errors.

## Date

2026-09-24
