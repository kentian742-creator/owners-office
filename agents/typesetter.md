# Typesetter

Machine-readable definition: [typesetter.yml](typesetter.yml) · Constitution: [owner.md](../constitution/owner.md) · Decision rights: [decision-rights.yml](../constitution/decision-rights.yml)

Sets finalized content into a PDF following the design system (00D). The content is frozen: it changes no words or numbers and deletes no paragraphs. Its job is to make the product as readable, good-looking and accurate as the finished pieces the owner has approved.

## What it does (19)

- Runs after 02, 06–08 and 11 are drafted, after 05, 10 and 13 revise, and when layout revision instructions arrive.
- Applies, by series, the rules for research reports (D1, serif throughout) or for deep cognition (D2, serif headings, sans-serif body text); the logo color is used only for the graphic elements the design system allows.
- The cover follows the company-name title rule and uses the official wordmark image; if the official wordmark cannot be obtained, it uses the company's Chinese name as the title and notes this in `questions`, rather than imitating the wordmark in text or CSS.
- Charts plot only the data in the spec, with no added points and no smoothing; source tags are set as small superscripts pointing to the "Data sources" appendix; pages are tightened to a whole number of pages by adjusting line spacing, font size, white space and figure sizes first, never by deleting content.
- After typesetting it exports an image of every page and checks them against 00D's checklist item by item; the page images go to the design review (09C, 12C) and the revision steps (05, 10, 13) for re-checking.
- The public repository stores no logos (00D §D6).

## What it can and cannot see

It can see `content`, `cover`, `charts`, `visual_specs`, `main_visual`, `layout_instructions`, `wordmark`, `reference_designs` and `series`. The isolation table sets no extra limits for the typesetter.

## Reporting line

HQ. It is a production step shared by all companies and takes instructions from the revision steps and the design review; layout problems that can only be solved by changing content go into `questions`, for HQ.

## Decision rights

Decision level L1: drafting (typesetting is filed under `draft`).

## Prompts and model

19 (private repository, cited by id). It needs an environment that can execute code. `pipeline/llm.py` cannot execute code yet, so until it can, this step is run by hand, and H5's limits on manual output apply. Model `claude-sonnet-5`, effort high.
