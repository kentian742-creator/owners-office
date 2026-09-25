# 0012 The discount-rate check no longer compares premiums across companies

> Partly amends [0004](0004-valuation-private-even-for-msft.md). This record states no company's discount rate or premium.

## Background

In thesis-ci spec 0.1, C-DISCOUNT-RATE checked not only the arithmetic of the discount rate but also made a cross-company comparison: with companies sorted by quality rating, a higher-quality company could not have a higher premium. Rule 5 of the old `owner.md` also stated "a higher-quality company may not have a higher premium" as a rule. All four existing C-DISCOUNT-RATE errors in the private repository came from this cross-company comparison.

The owner's valuation rule 10 says the opposite: a company's premium must be derived from that company's own cash-flow record and must not be interpolated between the premiums of other companies in the series; rule 13 adds that this only forbids borrowing other companies' premiums and is no reason to cut or change the series ranking. 00 §V1 puts it this way: the premium prices only the volatility and predictability of this company's cash flows across a full cycle, with no lookup tables, no fixed tiers, and no numbers assigned by the cells of the Munger matrix or by rating; §V10 adds that when an individual company's premium does not follow the rating order, the basis is stated in `method_note`, and the premium is not adjusted to fit the order.

The original text of the owner's investment constitution does say that "more-predictable, higher-quality businesses generally warrant a lower risk premium": "generally" marks a tendency, not a hard constraint across companies.

## Options

1. Keep the cross-company comparison, as an error.
2. Keep the cross-company comparison, downgraded to a warning.
3. Compare only between companies in the same domain.
4. Drop the cross-company comparison; the check becomes `total = risk_free + premium`, a dated Treasury yield reading, and a `method_note` that states the basis of the premium judgment.

## Decision

Option 4 (C-DISCOUNT-RATE in thesis-ci spec 0.2).

- R5 of `owner.md` is rewritten per 00 §C; its interpretation keeps the owner's own wording, "generally warrant a lower risk premium", and "How the system enforces it" states that this is a tendency, not a hard constraint across companies.
- The phrase in [0004](0004-valuation-private-even-for-msft.md) "how the quality rating affects the discount-rate premium" no longer holds: the premium is judged from the company's own cash-flow record, not slotted by rating. The rest of 0004 is unchanged, with a note at its top.
- The design document's Phase 5 idea of "a quality-to-premium mapping table, written in the public constitution" conflicts with the 00 §V1 rule "no lookup tables, no fixed tiers"; until the owner decides otherwise, no such mapping table is written (see the STATUS to-dos).

## Rationale

- A check must not force a practice the owner's rules forbid. The cross-company comparison would push some company's premium to be adjusted so that "the order is right", which is exactly the interpolation and order-fitting that rule 10 and §V10 guard against.
- What the premium should explain is the volatility and predictability of this company's cash flows, while the quality rating mixes several dimensions such as business, management and capital allocation; the two are correlated, but they don't map one to one.
- The arithmetic, the date and the stated basis are the parts a machine can check reliably; whether the premium judgment holds up is examined item by item by model review (04C), including its symmetry.

## Rejected alternatives

- **Keep it as an error:** contradicts rule 10 directly; the four existing errors in the private repository are false positives it caused.
- **Downgrade to a warning:** it would fire every time and only produce noise, or quietly push people toward fitting the order.
- **Compare only within the same domain:** still constrains one company's premium by other companies' premiums, just within a smaller scope.

## Date

2026-09-24
