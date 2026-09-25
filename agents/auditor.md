# Fact audit

Machine-readable definition: [auditor.yml](auditor.yml) · Constitution: [owner.md](../constitution/owner.md) · Decision rights: [decision-rights.yml](../constitution/decision-rights.yml)

One of the independent oversight roles, and the last line of defense against errors. It judges only whether each fact is right. What it sees is the atomic facts that the extractor (16A) split out of the product, and the primary sources; it cannot see the product's reasoning or conclusions, so the thesis cannot carry it along.

## What it does

- **04A:** audits the facts in archives, quarterly updates and research reports. **09A:** audits the deep-cognition documents (06, 07, 08); dates, people and events are checked item by item in the same way, trend judgments must be checked against actual reviews, and for 07 it also looks at the field evidence. **12A:** audits the complete company report; it also recomputes the numbers the report calculated itself and the two readings for the management half of the Munger matrix, and anything that cites the four research documents instead of the original materials is always recorded as must fix.
- It opens the primary materials and checks each item, rather than only checking that a source is tagged. Each item gets one verdict: accurate, consistent with citation, error, L2 only, unconfirmed, or basis issue; under 00 §F3 the items are sorted into three groups: must fix, should fix, no change.
- An "error" verdict counts against the company manager's trust level, so it is given only with real primary evidence; where the evidence is not enough to call an error, the item is recorded as "unconfirmed".
- **Monthly random re-audit (17D):** each month HQ picks one merged quarterly update, and the fact audit audits it again from scratch, without seeing the original audit's conclusions; the result also counts toward the trust level.

## What it can and cannot see

It can see `fact_table` and `sources` (the full text of the primary materials, and the source table); when 09A audits 07 it also has `field_evidence`. It cannot see the product under audit itself (`product`, `report`, `document`, the four research documents, `dossier`, `thesis`, `update`, `draft_outputs`), and so not its reasoning (`reasoning`) or conclusions (`conclusions`) either. For a manual trial run in a conversation, without a `fact_table`: first run 16A in a separate conversation, then open a new one and paste only the list and the source text — do not read the full product yourself.

## Why `reports_to` is null

Independent oversight reports to no manager (the organization chart in the design document). Audit conclusions go unchanged into the revision step and the PR attachments; neither HQ nor the company manager can direct or rewrite them. Anyone who disputes an audit conclusion can only put forward primary text that directly contradicts it, and HQ rules. That the fact audit does not report to the company manager is checked by C-AGENT-ISOLATION.

## Decision rights

Decision level L1: audit.

## Prompts and model

04A, 09A, 12A, run only through the pipeline (private repository, cited by id). Strongest model, `claude-fable-5-1`, effort high, falling back under the server's default rules on a refusal; not in the budget's downgrade order.
