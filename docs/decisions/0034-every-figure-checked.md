# 0034 Every figure of an archive document is checked before it is placed

> Follows [0024](0024-post-earnings-steps.md) and [0028](0028-archives-from-sec-filings.md). Code:
> `pipeline/factcheck.py`; `fact_check`, `gate_figures` and `plan_placement` in `pipeline/runner.py`;
> `ARCHIVE_DOCUMENTS` in `pipeline/outputs.py`.

## Background

Until 2026-10-10 the archives were checked by the fact audit (04A), which judges the facts the extractor (16A) lists
for it, and by HQ's spot checks of single tables against the filings. On 2026-10-10 the owner directed that spot checks
are not acceptable: every figure in every archive document is checked, forward against its source and in reverse by
recomputing it, and this check is a gate before anything is placed or published.

The same day HQ wrote a checker outside the repositories and ran it on McDonald's and NVIDIA's archive documents,
about 6,000 figures. HQ then reviewed by hand the 400 or so figures the checker could not confirm. None was wrong; a
few were off in the last digit or approximated, and those texts were corrected.

## Decision

1. **The checker is part of the pipeline** (`pipeline/factcheck.py`). It keeps HQ's method and classes:
   - each figure is looked up in the part of the source its tag cites, then in the whole document, then among the XBRL
     facts filed with that accession, to the figure's last digit and across scales (billions and millions, a percent
     and a fraction, units and millions);
   - a figure with no tag of its own takes the tags of its paragraph, its list's lead-in, its table row, and its
     table's caption and footnote;
   - the company's valuation and dossier are evidence for the documents drawn from them;
   - arithmetic written out is evaluated, and other figures are recomputed from the figures beside them.

   Each figure ends in one class: found, context, xbrl, valuation, dossier, recomputed, weak, skipped, not-a-fact or
   unverified. Every weak or unverified figure, and every calc error (arithmetic that does not hold), is an item that
   needs a verdict.
2. **What is checked:** the archive documents a bundle writes (`outputs.ARCHIVE_DOCUMENTS`):
   - the dossier, written whole or patched by an update;
   - thesis.yml, story.md and ledger.yml;
   - the quarterly update record;
   - the valuation and its working;
   - the research reports and documents.

   Verdicts, findings, questions, sources, pre-registrations and their settlements, the pull request's body, the
   mistakes entry, letters and memos are not archive documents and are not checked.
3. **The review file.** `python -m pipeline.runner factcheck <bundle>` writes `factcheck.yml` in the run directory:
   - each document's counts by class, and the tags whose documents could not be read, with the reason;
   - one item per figure to review: `doc`, `figure`, `class`, `tags`, a trimmed `context`, `line`, the checker's note,
     and `verdict: null` and `how: null` for HQ.

   The verdicts are `correct`, `derived-correct`, `not-a-fact`, `last-digit` and `approximation` (accepted), and
   `wrong` and `cannot-verify`. An item is the same item when its document, figure and context are the same; its line
   may move and is not compared. A verdict therefore survives a rerun, and an item still without a verdict takes the
   verdict given to the same item in an earlier run of the company (`verdict_from` names that run). A verdict whose
   item no longer appears is kept under `no_longer_found`.
4. **The gate.** `place` runs the same check on the archive documents it is about to write. It checks them as it
   would write them (with HQ's tag repairs applied, the valuation marked effective, the dossier patched) and against
   the source registers and valuation as this placement leaves them. It writes nothing until every item has an
   accepted verdict:
   - an item without one is listed like a lint error;
   - `wrong` or `cannot-verify` stops placement until the text is corrected and the item no longer appears.

   No flag skips the gate, so placement goes ahead only on the verdicts in the review file. `place --check` prints the
   summary, `placement.yml` records it with the review file's hash, and the post-earnings chain waits at placement for
   the verdicts. The fake client's placeholders (dry runs and tests) are never placed for real and are not checked.
5. **Deterministic and offline.** The same documents and caches give the same result. `--offline` reads EDGAR, prices
   and Treasury yields from the local caches only; Treasury tables are now cached in `inputs/prices` as price
   histories are. A tag that cannot be read is named with the reason.
6. **What was wrong in HQ's script and is fixed here.** With these fixes switched off, the port puts every figure of
   the documents HQ checked in the class HQ's script gives it, once that script's cache is made deterministic.
   - Numbers of a text were cached by the `id()` of texts that are freed, so a figure could be "found" in the numbers
     of another page, and the results changed from run to run.
   - A short number equal to any XBRL fact of the filing passed as `xbrl`: "31" from "December 31" did. A filing's
     XBRL facts are hundreds of numbers, so such a match is now `weak`, like a short number found anywhere in a
     document. A later tag that finds the figure on its cited page still makes it `found`.
   - A figure was "recomputed" from a copy of itself ("1 × $2,163M", "0.1M + $7,186M"): figures in M or B were added
     to the operands a second time without excluding the figure. No operand may now equal the figure, in any scale.
   - A document that cites its own tag was confirmed by itself. NVIDIA's valuation working cites
     `NVDA-VAL-2026-10-09`, registered when it was placed, and 243 of its figures passed that way. A tag that names
     the document being checked now confirms nothing in it.
   - A formula that mixes M and B was read without its units, so "230.48 × 24,242M = $5,587.3B" was a calc error.
     The values are now put in one scale.
   - A register entry with no location (`SPGI-VAL-2026-09-30` in McDonald's sources.yml) hid S&P Global's own entry.
   - The page locators of the owner's report texts were ignored. A parenthesis of the prose made a negative figure
     ("(236.90 ÷ 208.88)"). Price histories were cached by symbol only, and Treasury tables were fetched again for
     each figure.
   - YAML figures had no line numbers, and formulas were evaluated with `eval()`. The new evaluator does arithmetic
     only, and keeps powers bounded.
   - Other companies' names were hard-coded; they now come from each company's thesis.yml.

## Consequences

On McDonald's and NVIDIA's current documents (about 6,000 figures, read offline):

| Document | Figures | To review | HQ's script, on the versions it checked |
| --- | ---: | ---: | ---: |
| McDonald's thesis.yml, story.md, ledger.yml | 174 | 15 | 6 |
| McDonald's research report | 952 | 79 | 41 |
| McDonald's dossier | 1,276 | 116 | 20 |
| McDonald's valuation working and valuation.yml | 1,173 | 169 | 149 (working only) |
| NVIDIA thesis.yml, story.md, ledger.yml | 153 | 9 | 7 |
| NVIDIA dossier | 1,479 | 85 | 86 |
| NVIDIA valuation working and valuation.yml | 788 | 114 | 126 |

The items rise where HQ's script passed figures it had not confirmed: 295 short XBRL matches and 89 self-copies on
the documents it checked, and NVIDIA's 243 self-confirmed figures once its working tag was registered. The first gate
run of each archive therefore needs verdicts that the earlier review did not give. Of the 77 items in McDonald's
report, 28 were reviewed on 2026-10-10.

A placement now reads its sources (EDGAR from the local cache where it can) and takes a few seconds per large
document.

Two limits of HQ's method remain. A figure can still be confirmed by coincidence, through recomputation or through
the valuation and dossier classes, and such a figure never reaches HQ. And figures are matched by value alone: what a
figure means is not compared, nor is an XBRL fact's unit compared with the figure's.

## Options not taken

- **A sample, or checks of the most important figures only:** the owner ruled them out.
- **A flag to place without the verdicts, or with some still open:** it would let unconfirmed figures be placed,
  which the owner's directive rules out.
- **Letting `place` write the review file:** `place` writes nothing until it can place everything; `factcheck` writes
  the file, and HQ fills it in.
- **A model checking the figures:** the documents being checked are the model's own. Each class the checker gives
  points to a passage of a source, an XBRL fact or a computation, and the same inputs always give the same classes.

## Date

2026-10-10
