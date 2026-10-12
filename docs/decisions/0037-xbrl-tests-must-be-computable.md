# 0037 A test read from XBRL facts must be one thesis-ci can compute

> Follows [0034](0034-every-figure-checked.md) and [0036](0036-field-repairs-for-system-files.md). Code:
> `xbrl_metric_errors` in `pipeline/outputs.py`; thesis-ci's `metrics.computable` (SPEC 9.1, thesis-ci 0.6.0 and
> 0.6.1).

## Background

A quantitative test with `data: xbrl` takes its readings from the company's XBRL facts through thesis-ci. Until
thesis-ci 0.6.0, a test's own `metric_def` was computed only when its formula was the growth of one quantity. Every
other formula got no reading, and its test stayed undetermined at every evaluation. Nothing reported it:

- AppLovin's breaker APP-Q6 had no reading from the day it was placed;
- two of American Express's tests and four of Costco's were in the same state.

thesis-ci 0.6.0 added a formula engine, and 0.6.1 added typographic signs, `avg()` and more names. Even so, a model
can still write a formula the engine does not know. Walmart's 01B of 2026-10-11 wrote six such formulas in one
attempt, among them `avg()` before 0.6.1 had it, and the run passed.

## Decision

1. **The run checks it.** Each 01B or 03 reply that writes thesis.yml has every quantitative test read from XBRL facts
   checked with thesis-ci's own `computable()`:
   - a test's own `metric_def` must parse, and its names and functions must be ones thesis-ci knows;
   - a registry metric must be one thesis-ci computes from the XBRL facts.
2. **A test that fails is an error of the reply.** The retry writes the thesis again (0036, item 3), and the error says
   how to fix it: a formula in the names of SPEC 9.1, a registry metric, or `data: filing_text` with `where`.
3. **The pins move to thesis-ci v0.6.1,** so the check and the evaluation use the same engine.

The tests already placed are checked against the same rule and corrected through each company's next update.
AppLovin's three are redefined in its rebuild after its third-quarter 10-Q, because AppLovin no longer tags capex.
