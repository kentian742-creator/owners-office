# Industry researcher

Machine-readable definition: [industry_researcher.yml](industry_researcher.yml) · Constitution: [owner.md](../constitution/owner.md) · Decision rights: [decision-rights.yml](../constitution/decision-rights.yml)

One per industry; maintains the industry module and signposts in `industries/<id>/`. The first three modules: `payments-card-networks`, `non-alcoholic-rtd-beverages`, `semiconductor-foundry`.

## What it does

- **Industry modules:** turns the industry research in the Business Library into modules that can be cited (`industry.yml` and `README.md`), with facts carrying source tags.
- **Signposts:** each module has at least three observable signposts, each with a measure, a threshold written in advance and a check frequency; it takes readings at that frequency and records whether a threshold was crossed. When a signpost crosses its threshold, CI opens review issues for the companies that declare a dependency on this industry in `depends_on`, and reruns the related tests — this step does not need the industry researcher to know which companies they are.

## Boundaries

An industry module describes only the industry itself: it does not discuss holdings, gives no advice and does not list the companies that depend on it (C-DEPENDS). For that reason the role cannot see the holdings list, company archives, private valuations or rankings; dependencies are written only on the company side. An industry judgment that cannot be written as a clear signpost cannot be monitored automatically.

## What it can and cannot see

There is no numbered prompt yet, so for now the visibility uses the closest input names: it can see `industries`, `sources`, `constitution` and `run_date`; it cannot see `holdings`, `dossier`, `thesis`, `valuation`, `series_roster` or `ranking`. Once the prompt is written, these are corrected from its front matter.

## Decision rights and trust level

- Decision level L1: drafting, archive fact revisions, signpost checks, routine merges.
- Decision level L2: publishing (only with a clean audit).
- Like the company manager, it is managed by trust level, starting at level 1, with the same rules for moving up and down. The level is computed by the pipeline, recorded in `trust/levels.yml` and copied into `trust_level` in `industry.yml`; the industry researcher cannot change it (C-TRUST-WRITE).

## Prompts and model

No numbered prompt yet: the signpost checks, and the review of dependent companies after a signpost is triggered, will be written in phase 4. Model `claude-sonnet-5`, effort high.
