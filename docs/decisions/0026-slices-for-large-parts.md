# 0026 A part too large for one call runs in slices

> Amends [0024](0024-post-earnings-steps.md) for 16A and 04A. Code: `pipeline/slicing.py`, the slicers in
> `pipeline/registry.py` and the sliced path of `runner.execute`.

## Background

On 2026-09-27 the post-earnings chain was rehearsed end to end on real filings and the real model (AXP FY2026Q2, a
copy of both repositories, nothing placed). The 03 draft succeeded on the first try. The next two steps showed limits
that every quarterly update will reach:

- **04A could not be sent.** Its `sources` input carried the full text of every document the fact table cites:
  3.3 MB, about 1.3 million tokens, over the model's 1M-token context window. Five 10-Ks (FY2014 and FY2022–FY2025)
  made up 2.9 MB of it, cited by about 30 of the 385 facts; the 10-Q and the earnings release, which carry most facts,
  were 0.3 MB. One reply would also have had to judge all 385 facts, beyond the 128k-token output limit.
- **16A nearly filled its output.** It wrote the 385 facts in one reply of 118k output tokens, against a ceiling of
  128k; a larger update (MSFT's archive has 460 facts) would be cut off.
- **The pipeline's token estimate was low by a third.** It counted four characters per token; the real calls ran at
  2.5 to 2.8 for filing text, YAML and Markdown.
- **Most source excerpts were missing.** The pipeline found the cited sentence for 79 of 385 facts: it searched for
  values such as "12,256 against 11,200" as one string, and for dates in ISO form, which filings do not print.

The hand-run fact audits of the six archives (September 2026) had already worked around the first two limits: the
facts were split into slices of about sixty, each checked by its own auditor.

## Decision

1. **Slices.** A part whose inputs one call cannot take runs as several calls in one bundle. Each slice is recorded
   under `slices/<id>/` like a bundle (inputs, outputs, run.yml, calls.jsonl, reply.txt). When every slice has
   succeeded, the pipeline merges their outputs into the bundle's `outputs/`, so the steps after it read them
   unchanged. A retry runs only the slices that failed or never ran.
2. **16A** is sliced when the product's blocks exceed 16,000 tokens. The blocks (one per draft output or diff) are
   packed in order, and a longer block is cut at headings. Every slice keeps the product's title and the full source
   table. The merged fact table is renumbered F001… in slice order, and each derived fact's `inputs` are renumbered
   to match.
3. **04A** is sliced when the table has more than 60 facts or its documents exceed 500,000 tokens:
   - Facts are grouped by the documents they cite. An untagged fact is checked against the event's own filings.
   - Groups are packed into slices of at most 60 facts. A slice's documents stay within 160,000 tokens unless one
     group alone needs more.
   - A slice keeps at most 500,000 tokens of documents in full: the event's filings first, then the newest. That is
     two annual reports, which carry about four fiscal years of comparatives between them. Older documents are cut to
     the lines around the values the slice's facts state, and so is any document that at most two facts cite and that
     is not one of the event's own filings.
   - Each slice's fact table opens with a pipeline note: which slice it is, and that it judges its own facts only. It
     carries as `context_facts` any facts from other slices that its derived facts take as inputs.
   - Merging restores the fact order and renumbers findings and questions (04A-01…, 04A-Q01…), rewriting each
     slice's references to its own ids.
4. **Every fact gets exactly one verdict.** `llm.complete` now checks a 04A reply against its fact table: no fact
   missing, none judged twice, no verdict for anything else. A reply that fails is retried once with the ids listed,
   like any other invalid output, and the merge checks the whole table again.
5. **A request that cannot fit is refused at assembly.** The estimate is now ASCII characters / 2.6 (plus one token per
   other character). Assembly refuses any request (or slice) estimated above 872,000 tokens, the context window less
   the output ceiling, before anything is written or sent.
6. **Better excerpts.** A string value is also searched for by the numbers in it, and a date as filings print it
   ("July 24, 2026").

The prompt set is unchanged: what a slice is travels in the slice's own input.

## Consequences

- On the rehearsal's data, 16A becomes three calls of about 25k input tokens, and 04A eight calls of 140k to 380k.
  That is about 1.8 million input tokens per audit, more than the one call that could not be sent, because the 10-Q
  goes into several slices. Slices run one after another; a quarter's audit takes about as long as the hand audits
  did.
- A cut document holds only lines where a value appears verbatim, so a derived or rounded figure it cannot show is
  left unconfirmed rather than confirmed or refuted. The rule keeps such cuts to older and rarely cited documents.
- The first real run of the sliced audit is the next rehearsal (after the plan's weekly reset on 2026-09-30), before
  AXP reports on 2026-10-23.

## Options not taken

- **One call with only the excerpts.** Cheap, but the auditor could no longer find the right figure when the cited
  one is wrong, and finding wrong figures is 04A's purpose.
- **Letting the auditor fetch documents with tools.** The claude-code backend runs without tools by design (0022); the
  inputs stay the pipeline's, recorded and hashed.
- **Summarizing the 10-Ks first.** A summary is not a primary source (00 §E).
