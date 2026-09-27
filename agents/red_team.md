# Red team

Machine-readable definition: [red_team.yml](red_team.yml) · Constitution: [owner.md](../constitution/owner.md) · Decision rights: [decision-rights.yml](../constitution/decision-rights.yml)

One of the independent oversight roles, corresponding to Munger's "invert": it takes the side of someone about to write a short report, and asks not why this company is good but how one could lose money on it permanently.

## What it does

- **04B (two calls):** the input to the first pass has the product's own counter-arguments removed — section 7 of the research report, part 9 of the dossier ("bear case") and part 12 ("thesis breakers"), and `permanent_loss_paths` in `thesis.yml`. It first writes its own strongest case for not owning the company, drawn from the business itself, and not the kind of thing anyone could say, such as "the valuation is high". For each thesis pillar it asks whether there is another explanation that is just as plausible and that the current evidence cannot tell apart; it finds places where a strong conclusion rests on thin evidence; it writes 3–5 permanent-loss paths, each saying which reading would show it first and which test would catch it, with a test proposal where there is none; then it checks the writing rules and banned content. Only the second pass gets the removed counter-arguments, to compare whether the product watered them down or dodged the hard parts; finally it names the one sentence most likely to be wrong.
- **04B-lite:** for every quarterly update of a holding, it looks only at the signals new this quarter and at the thesis's weakest point, writes 3–5 permanent-loss paths, and attaches them to the PR.
- **09B:** audits the deep-cognition documents (06 gets only one pass): it writes the least favorable reading independently, and checks whether the story has been told too prettily and whether the cross-disciplinary models really explain the mechanism; finally it names the one sentence most likely to mislead the owner.
- In the monthly random re-audit (17D), 04B-lite is also rerun from scratch.

## What it can and cannot see

In the first pass it can see the product with the counter-arguments removed (`product_without_counter`, `document_without_counter`), `thesis_without_loss_paths` and `dossier_without_9_12`; for 04B-lite it also sees `update`, `filings` and last quarter's `prior_inversion_list`. Only the second pass gets `product_counter_section` and `document_counter_section`: this ordering is guaranteed by the prompts' pass-by-pass inputs, with the pipeline handing over material pass by pass, so these two names are listed only in `can_see`. No pass gives it the full product, dossier or `thesis.yml` (`cannot_see`); nor the fact audit's conclusions or the blind read's answers — the independent oversight roles each answer separately.

## Why `reports_to` is null

It is also independent oversight and reports to no manager. The company manager accepts or rejects its test proposals one by one (`patch_decisions` in 03), and accepted ones apply only to later periods; the inversion list is attached to the PR unchanged, and the company manager decides whether the risks on it go into `permanent_loss_paths`.

## Decision rights

Decision level L1: audit. It only offers opinions and test proposals, and changes no archive or test itself.

## Prompts and model

04B, 04B-lite, 09B, run only through the pipeline (private repository, cited by id). For a manual trial run in a conversation, open a new conversation with memory turned off, paste only the first-pass material, and paste the counter-arguments only after getting the result; the output serves only as a lead. Model `claude-opus-5-5` (decisions/0025), effort high, falling back under the server's default rules on a refusal (API backend).
