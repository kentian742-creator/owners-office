# 0038 A figure is accepted only by a match that singles it out

> Follows [0034](0034-every-figure-checked.md). Code: `slips`, `label_of`, `Checker.singled_out`, `recompute`,
> `chain_status` and `calc_errors` in `pipeline/factcheck.py`.

## Background

On 2026-10-11 HQ corrected a figure in Walmart's placed report: "1.053^10 ÷ 1.072^10 = 1.675 ÷ 2.004", where 1.053^10
is 1.676. The gate had accepted 1.675 as recomputed, because 1.053 × 159.1 ÷ 100 happens to give it. The recompute
tries every pair of the figures near a figure, and with a hundred figures to pair, some pair gives almost any number.

HQ then tested the checker itself. Each figure it had accepted on the placed documents of Walmart, Costco, NVIDIA and
McDonald's was classified again with its last digit moved by one, as a slip would write it. Most acceptances passed
the slip too:

- 699 of the 886 figures recomputed from a pair;
- 93% to 97% of the figures with one to three significant digits, whatever their class. Finding a short number in
  the cited part of a filing showed only that the number was there, not that it was the figure: a 10-K's discussion
  holds hundreds of such numbers.

The arithmetic check had the same weakness. It accepted a result within 0.4% of the formula, so 1.675 for 1.676
passed, and it compared the two sides of a chain "A = B" only as wholes.

## Decision

1. **A match counts only when it singles the figure out:** a slip of the last digit (`slips`: the figure one more and
   one less in that digit) must not pass the same way.
   - In a cited filing, the figure must stand next to a word of its own label (`label_of`: a table cell's column
     header and row label, or the words around it in its sentence) in the source's line or sentence. Neither slip may
     stand there next to that label too.
   - In the archive's own evidence (the valuation and the dossier) and among the XBRL facts of the cited filing,
     neither slip may be there at all.
   - Recomputed from the figures near it, the figure counts only if neither slip, put in its place, is recomputed too.
     A sum or difference with a figure under ten units of the figure's last digit is a near copy, not a recompute.
2. **Units must agree.** Points or a multiple are not a percent, and a percent is not a dollar amount.
3. **Arithmetic holds as written.**
   - A result must be the exact value of the formula as written, to the result's last digit.
   - A result that is right only within the rounding of the numbers the formula writes is a calc error for a reviewer,
     who usually judges it "last digit".
   - In a chain "A = B" whose two sides have the same shape, each number of B is checked against its counterpart in A.
4. **Everything else is weak, for a reviewer,** and the checker says why: the figure is not next to its label, a slip
   stands there too, or a slip is recomputed too.

## Consequences

The stricter check leaves 3,322 more items to review on the four companies' placed documents. That count is after the
verdicts already given and HQ's recomputes of the valuations. Every new archive document will carry more items to
review than before, most of them short figures in filings' discussions.

HQ reviews these items in batches by source. A verdict counts only when it can be checked:

- "correct" must quote the source verbatim;
- "derived-correct" must give a formula, which is recomputed from quoted inputs.

Where a review finds a figure wrong, HQ corrects the placed document with a ruling. The PDFs are re-issued once their
documents pass.
