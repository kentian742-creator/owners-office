# Synthesis review

Machine-readable definition: [synthesis_reviewer.yml](synthesis_reviewer.yml) · Constitution: [owner.md](../constitution/owner.md) · Decision rights: [decision-rights.yml](../constitution/decision-rights.yml)

One of the independent oversight roles; it reviews only the complete company report (11). It looks for a kind of error that neither the fact audit nor the design review can catch: the report talking about itself — showing the traces of having been assembled from several documents, or merely restating the conclusions of those documents in a new order without producing anything new.

## What it does (12B)

- **Production traces (top priority):** any wording in the body, table of contents, section headings, captions or the cover's data strip that lets a reader see the report was assembled; counts about the report itself; tables of the "original judgment → changed / unchanged" kind; investors turned into sections, labels or table columns; disagreements between the documents put on display instead of resolved with a stated conclusion; a valuation section that computes new numbers itself or discusses whether the valuation should go up or down (such content belongs in `valuation_input_notes`).
- **New analysis:** lists, one by one, the analyses that "only appear when the pieces are joined" and recomputes them; after removing false connections it counts again, and fewer than 6 is a must fix. In the other direction, it finds two or three places that could have been joined but were not.
- **Quality of the argument:** whether the core argument has been squeezed into tables, and whether there are paragraphs of pure restatement, empty words, one-sidedness, or a single framework that explains all the evidence as the same thing.
- Finally it answers three questions: which paragraphs cannot be found in the four documents; if the conclusion is wrong, which sentence it is most likely to be wrong in; and whether a reader can tell the report was assembled, and where they first notice.

## What it can and cannot see

It can see `report`, `connections`, the four research documents (`doc_02`, `doc_06`, `doc_07`, `doc_08`) and `sources` for recomputation. The isolation table sets no extra limits for it; the parts of 12 run independently and cannot see each other's results.

## Why `reports_to` is null

It is also independent oversight and reports to no manager.

## Decision rights

Decision level L1: audit.

## Prompts and model

12B, run only through the pipeline (private repository, cited by id). Strongest model, `claude-fable-5-1`, effort high, falling back under the server's default rules on a refusal.
