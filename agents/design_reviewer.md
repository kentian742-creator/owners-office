# Design review

Machine-readable definition: [design_reviewer.yml](design_reviewer.yml) · Constitution: [owner.md](../constitution/owner.md) · Decision rights: [decision-rights.yml](../constitution/decision-rights.yml)

One of the independent oversight roles. It looks at the typeset pages as images, page by page, rather than reading only the text: the private reports must be as readable and accurate as the finished pieces the owner has approved, with nothing mixed in that does not belong there.

## What it does

- **09C (deep cognition, 06, 07, 08):** first, banned content — these three documents do no valuation, so a value range, discount rate, position percentage, ranking or share-price forecast is a must fix. Then it checks the layout page by page against 00D's deep-cognition checklist: the logo color may be used only for graphic elements such as rules, timelines, module labels and section numbers, never spread into blocks of color; serif headings and sans-serif body text; whether 06's timeline, 07's four dashboard modules, and 08's branch-path diagram and optionality fan are in place; whether any final page carries only a few lines.
- **12C (complete company report):** against 00D's research-report checklist: serif throughout, with text for different purposes distinguishable at a glance; no accent color with two meanings; the cover follows the company-name title rule and uses the official wordmark image, and the company's full English name is not repeated next to the wordmark, in the header or in the footer; the key-data strip carries only the company's own numbers; table of contents, headers, footers and charts are all present; the main visual is tied into one thread; no revision log, position ladder or precise entry point.
- Every layout problem comes with an instruction the typesetter (19) can carry out directly (`layout_instructions`: which page, which element, what it should become), passed on through 10 and 13. Without page images it checks only for banned content, and in `questions` asks for the layout to be reviewed again after typesetting.

## What it can and cannot see

It can see `document`, `visual_specs`, `report`, `cover`, `charts`, `main_visual` and `rendered_pages`. The isolation table sets no extra limits for the design review.

## Why `reports_to` is null

It is also independent oversight and reports to no manager.

## Decision rights

Decision level L1: audit.

## Prompts and model

09C, 12C (private repository, cited by id). Strongest model, `claude-fable-5-1`, effort high, falling back under the server's default rules on a refusal. `pipeline/llm.py` passes the page images (`rendered_pages`) to the model as image blocks.
