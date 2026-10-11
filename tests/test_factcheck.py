"""Every figure checked (pipeline/factcheck.py) and the fact-check gate of placement (docs/decisions/0034).

No network: the checker reads FakeSources, a few short documents by tag, and the runner tests build their bundles in
the synthetic repositories of tests/runner_fixtures.py.
"""

from __future__ import annotations

import datetime as dt
import io
import json
from pathlib import Path

import pytest
import yaml

from pipeline import chain, edgar, factcheck as fc, outputs, registry, runner
from tests import runner_fixtures as fx

NOW = dt.datetime(2026, 10, 20, 12, 0, tzinfo=dt.timezone.utc)


class FakeSources(fc.Sources):
    """Documents by tag (an exhibit as TAG#EX-99.1), XBRL facts and prices by tag; nothing else can be read."""

    def __init__(self, documents=None, xbrl=None, prices_=None, paths=None):
        self.documents = documents or {}
        self.xbrl = xbrl or {}
        self.prices = prices_ or {}
        self.paths = paths or {}  # tag -> the archive document it names

    def document(self, base, exhibit=None):
        key = f"{base}#{exhibit}" if exhibit else base
        if key not in self.documents:
            return fc.SourceText(None, "not among the test documents")
        return fc.SourceText(self.documents[key], "a test document", frozenset(self.xbrl.get(base, ())),
                             report="-RPT" in base, path=self.paths.get(base))

    def price(self, tag):
        return (self.prices[tag], "") if tag in self.prices else (None, "no test price")


TEN_K = ("Item 7. Management's discussion\nRevenues were $26,885 million, up 4%, and operating income was "
         "$12,393 million, an operating margin of 46.1%. The company had 45,356 restaurants, of which 5 were new.")
REPORT = "\n".join(f"===== [page {n}] =====\n{text}" for n, text in
                   ((1, "Cover"), (2, "Comparable sales rose 3% and margins 2 points."), (3, "Appendix 17")))


def checker(documents=None, xbrl=None, prices_=None, **kwargs) -> fc.Checker:
    return fc.Checker(FakeSources(documents, xbrl, prices_), "MCD", **kwargs)


def classes(check: fc.DocumentCheck) -> list[tuple[str, str]]:
    return [(f.text, f.cls) for f in check.figures]


# ---------------------------------------------------------------------------------------------------- figures


def test_a_figure_is_a_number_with_its_sign_dollar_sign_accounting_parentheses_and_unit():
    found = fc.figures_of("Revenue was $26,885M, up 3.1%; margin fell −0.5 points to (1,234) and 1.134× at 40 bps, "
                          "a $3.7 billion program and (0.1)% comparable sales.")
    assert [(n.text, n.value, n.unit, n.decimals) for n in found] == [
        ("$26,885M", 26885.0, "M", 0), ("3.1%", 3.1, "%", 1), ("−0.5 points", -0.5, "pt", 1),
        ("(1,234)", -1234.0, "", 0), ("1.134×", 1.134, "x", 3), ("40 bps", 40.0, "bp", 0),
        ("$3.7 billion", 3.7, "B", 1), ("(0.1)%", -0.1, "%", 1)]


@pytest.mark.parametrize("text", [
    "[src:MCD-10K-FY2025#p14]", "2026-10-09", "FY2025 and FY2026Q2", "the 10-K and the 10-Q, an 8-K, the DEF 14A",
    "Item 7, Note 10 and Part 2", "test MCD-Q7, calc C9, ruling R18, bear case B2", "§V20", "the 3rd year",
    "a 5-year plan", "October 9, 2026", "Oct 9", "prompt v3.2", "a 2-for-1 split", "accession 0000063908-26-000028",
    "in 2025", "Exhibits 99.1 and 99.2", "EX-99.1"])
def test_identifiers_dates_periods_forms_and_years_are_not_figures(text):
    assert fc.figures_of(text) == []


def test_a_parenthesis_that_does_not_close_on_the_number_belongs_to_the_prose():
    found = fc.figures_of("Price ÷ central value is 1.134 (236.90 ÷ 208.88), and $2,025 is not a year.")
    assert [(n.text, n.value) for n in found] == [("1.134", 1.134), ("236.90", 236.9), ("208.88", 208.88),
                                                  ("$2,025", 2025.0)]


def test_a_distinctive_figure_has_three_digits_or_two_and_a_decimal_point():
    assert [fc.distinctive(t) for t in ("5%", "45", "4.3%", "0.08", "145", "$0.5B")] == [
        False, False, True, False, True, False]


# ---------------------------------------------------------------------------------------------------- units


def test_markdown_units_are_sentences_and_a_list_takes_the_tags_of_its_lead_in():
    text = ("---\ncompany: MCD\nas_of: 2026-10-09\n---\n# Title 2\n\nRevenue grew 4% [src:A-10K-FY2025#p3]. Margin "
            "was 46.1%.\n\nIn FY2025 [src:B-10K-FY2025#p9]:\n- 45,356 restaurants;\n- 95% franchised [src:C-1].\n\n"
            "## Next 3\n\n- 7 items\n")
    units = fc.markdown_units(text)
    assert [(u.line, u.text) for u in units] == [
        (5, "# Title 2"), (7, "Revenue grew 4% [src:A-10K-FY2025#p3]."), (7, "Margin was 46.1%."),
        (9, "In FY2025 [src:B-10K-FY2025#p9]:"), (10, "- 45,356 restaurants;"), (11, "- 95% franchised [src:C-1]."),
        (13, "## Next 3"), (15, "- 7 items")]
    assert units[1].own == ["A-10K-FY2025#p3"] and units[2].own == [] and units[2].context[0] == "A-10K-FY2025#p3"
    assert units[4].context == ["B-10K-FY2025#p9"] and units[5].own == ["C-1"]
    assert units[5].context == ["C-1", "B-10K-FY2025#p9"]
    assert units[7].context == []  # a heading leads into the list below it, and it has no tags


def test_a_table_row_takes_its_source_cell_then_the_caption_and_the_footnote_and_each_cell_its_own_tags():
    text = ("Ten years [src:CAP-1]:\n\n| Year | Revenue | Margin | Source |\n| --- | --- | --- | --- |\n"
            "| 2025 | $26,885M [src:CELL-1] | 46.1% | [src:ROW-1] |\n| 2024 | $25,920M | 45.2% | |\n\n"
            "Footnote [src:FOOT-1].\n\n| a | b |\n| --- | --- |\n| 1.5 [src:X-1] | 2.5 |\n")
    rows = [u for u in fc.markdown_units(text) if u.cells is not None]
    assert [(u.line, u.context, u.table) for u in rows] == [
        (5, ["ROW-1", "CAP-1", "FOOT-1"], 0), (6, ["CAP-1", "FOOT-1"], 0), (12, ["X-1", "FOOT-1"], 1)]
    assert rows[0].cells[1] == ("$26,885M [src:CELL-1]", ["CELL-1"]) and rows[0].header[1] == "Revenue"


def test_yaml_units_take_the_source_of_every_mapping_above_them_carry_their_line_and_skip_what_is_no_fact():
    text = ("company: MCD\nsource: S-1\ntests:\n  - id: MCD-Q1\n    question: Is growth at least 5%?\n"
            "    threshold: 5\n    baseline: {value: 60.5, source: T-1}\n"
            "  - id: MCD-P1\n    side: system\n    statement: Sales will grow 3%.\n    note: Base 1,234 [src:U-1].\n"
            "claims:\n  - text: Sales were 1,234 [src:U-1]. Then 5.\n")
    units = fc.yaml_units(text)
    assert [(u.line, u.text, u.own, u.skip != "") for u in units] == [
        (1, "MCD", ["S-1"], False), (5, "Is growth at least 5%?", ["S-1"], True), (7, "60.5", ["S-1", "T-1"], False),
        (9, "system", ["S-1"], False), (10, "Sales will grow 3%.", ["S-1"], True),
        (11, "Base 1,234 [src:U-1].", ["U-1"], True),
        (13, "Sales were 1,234 [src:U-1].", ["U-1"], False), (13, "Then 5.", ["S-1"], False)]
    assert units[2].label == "tests[MCD-Q1].baseline.value: 60.5" and units[7].context == ["U-1", "S-1"]


# ---------------------------------------------------------------------------------------------------- matching


@pytest.mark.parametrize("figure, text, how", [
    ("$26,885M", "revenues were 26,885 million", "as written"),
    ("46.1%", "a margin of 46.12 percent", "rounded"),
    ("$35.2B", "cash of $35,187 million", "billions written as millions"),
    ("32.5%", "a ratio of 0.325", "a percent written as a fraction"),
    ("0.325", "a ratio of 32.5%", "a fraction written as a percent"),
    ("26,885,000,000", "revenues of 26,885 million", "units written as millions"),
    ("46.2%", "a margin of 46.1 percent", None),
])
def test_a_figure_matches_a_number_of_its_source_to_its_last_digit_across_scales(figure, text, how):
    (number,) = fc.figures_of(figure)
    fig = fc.Figure("d", 1, number.text, number.value, number.unit, number.decimals, [], [], "")
    assert checker().found_in(fig, text) == how


def test_found_context_weak_xbrl_and_unverified():
    check = checker({"MCD-10K-FY2025": TEN_K, "MCD-RPT1-2026-09-01": REPORT},
                    xbrl={"MCD-10K-FY2025": {12393e6 / 1e6, 46.1, 17.0}}).check("doc.md", (
        "Revenue was $26,885M [src:MCD-10K-FY2025]. Comparable sales rose 3% [src:MCD-RPT1-2026-09-01#p2].\n\n"
        "In FY2025 [src:MCD-10K-FY2025]:\n- 45,356 restaurants;\n- 4% growth.\n\n"
        "Operating income was $12,393M [src:MCD-10Q-FY2026Q2].\n\nAppendix 17 [src:MCD-10K-FY2025].\n\n"
        "Nobody cites 1,111.\n"))
    assert classes(check) == [("$26,885M", "found"), ("3%", "found"), ("45,356", "context"), ("4%", "weak"),
                              ("$12,393M", "unverified"), ("17", "weak"), ("1,111", "unverified")]
    by = {f.text: f for f in check.figures}
    assert by["3%"].how == "MCD-RPT1-2026-09-01#p2, the cited part (as written)"  # a page of the owner's report
    assert by["17"].how == "MCD-10K-FY2025 XBRL: a short number equal to a fact of the filing"
    assert by["1,111"].how == "no tag in its sentence, row, paragraph or table"
    assert check.unreadable == {"MCD-10Q-FY2026Q2": "not among the test documents"}


def test_a_distinctive_figure_equal_to_an_xbrl_fact_of_the_cited_filing_is_xbrl():
    check = checker({"MCD-10K-FY2025": "nothing"}, xbrl={"MCD-10K-FY2025": {-12393.0}}).check(
        "doc.md", "Operating income was $12,393M [src:MCD-10K-FY2025].")
    assert classes(check) == [("$12,393M", "xbrl")]


def test_prices_and_yields_are_read_as_numbers_of_their_day():
    check = checker(prices_={"MCD-PRICES-2026-10-09#2026-10-08": 236.9, "UST-PARYIELD-2026-10-09": 4.12}).check(
        "doc.md", "The close was $236.90 [src:MCD-PRICES-2026-10-09#2026-10-08] and the yield 4.12% "
                  "[src:UST-PARYIELD-2026-10-09]. Not 236.95 [src:MCD-PRICES-2026-10-09#2026-10-08].")
    assert classes(check) == [("$236.90", "found"), ("4.12%", "found"), ("236.95", "unverified")]
    assert check.figures[2].how == "not found in: MCD-PRICES-2026-10-09#2026-10-08 (236.9)"


def test_the_valuation_and_the_dossier_are_evidence_and_another_companys_valuation_only_for_a_sentence_about_it():
    evidence = [fc.Evidence("valuation", "companies/MCD/valuation.yml", "center: 208.9\n"),
                fc.Evidence("valuation", "companies/BRK/valuation.yml", "implied_return: 7.2\nother: 1,234.5\n", "BRK"),
                fc.Evidence("dossier", "companies/MCD/dossier.md", "C8 capex sum $14,077.0M")]
    check = checker(evidence=evidence, names={"BRK": ["BRK", "Berkshire Hathaway", "Berkshire"]}).check("doc.md", (
        "The central value is $208.9. The Berkshire hurdle is 7.2%. Another 1,234.5 here. Capex was $14,077.0M. "
        "Short 7."))
    assert classes(check) == [("$208.9", "valuation"), ("7.2%", "valuation"), ("1,234.5", "unverified"),
                              ("$14,077.0M", "dossier"), ("7", "skipped")]
    assert check.figures[1].how == "companies/BRK/valuation.yml (as written)"


def test_a_document_is_never_its_own_evidence():
    evidence = [fc.Evidence("dossier", "companies/MCD/dossier.md", "Capex was $14,077.0M.")]
    assert classes(checker(evidence=evidence).check("companies/MCD/dossier.md", "Capex was $14,077.0M.")) == [
        ("$14,077.0M", "unverified")]
    assert classes(checker(evidence=evidence).check("other.md", "Capex: $14,077.0M.")) == [
        ("$14,077.0M", "dossier")]


def test_a_document_that_cites_itself_is_not_its_own_source():
    working = "runs/MCD/2026-10-09-01C/valuation_md.md"
    sources = FakeSources({"MCD-VAL-2026-10-09": "Operating value is $5,462.9B."},
                          paths={"MCD-VAL-2026-10-09": working})
    text = "Operating value is $5,462.9B [src:MCD-VAL-2026-10-09]."
    itself = fc.Checker(sources, "MCD").check(working, text)
    assert [(f.cls, f.how) for f in itself.figures] == [
        ("unverified", "not found in: MCD-VAL-2026-10-09 (the document itself, which is not its own source)")]
    assert classes(fc.Checker(sources, "MCD").check("companies/MCD/valuation.yml", f"note: {text}\n")) == [
        ("$5,462.9B", "found")]


def test_another_companys_names_come_from_its_thesis_and_a_shared_first_word_is_not_one(tmp_path):
    roots = runner.Roots(tmp_path / "pub", tmp_path / "priv", tmp_path)
    for ticker, name in (("BRK", "Berkshire Hathaway Inc."), ("AXP", "American Express Company"),
                         ("SPGI", "S&P Global Inc."), ("PDD", "PDD Holdings Inc. (Pinduoduo Group)")):
        fx.write_yaml(roots.public / "companies" / ticker / "thesis.yml", {"company": ticker, "name": name})
    files = fc.ArchiveFiles(roots)
    assert [fc.company_names(files, t) for t in ("BRK", "AXP", "SPGI", "PDD", "NEW")] == [
        ["BRK", "Berkshire Hathaway", "Berkshire"], ["AXP", "American Express"], ["SPGI", "S&P Global"],
        ["PDD", "PDD Holdings"], ["NEW"]]


# ---------------------------------------------------------------------------------------------------- reverse


def test_a_figure_no_source_confirms_is_recomputed_from_the_figures_beside_it():
    check = checker().check("doc.md", (
        "Revenue of $1,200.0M less costs of $900.0M left $300.0M, a margin of 25.0%. Sales grew from 400.0 to "
        "440.0, up 10.0%. A total of $1.5B is $1,500.0M."))
    assert classes(check) == [("$1,200.0M", "recomputed"), ("$900.0M", "recomputed"), ("$300.0M", "recomputed"),
                              ("25.0%", "recomputed"), ("400.0", "unverified"), ("440.0", "unverified"),
                              ("10.0%", "recomputed"), ("$1.5B", "unverified"), ("$1,500.0M", "unverified")]
    assert check.figures[2].how == "$1,200.0M − $900.0M" and check.figures[6].how == "440.0 growth % 400.0"
    # $1.5B and $1,500.0M are one figure in two scales: neither confirms the other


def test_a_figure_is_never_recomputed_from_a_copy_of_itself():
    check = checker().check("doc.md", "The ratio is $2,163M ÷ $37,799M = 5.7%. Charges were $2,163M, 1 of them.")
    assert [(f.text, f.cls, f.how) for f in check.figures[:3]] == [
        ("$2,163M", "unverified", "no tag in its sentence, row, paragraph or table"),
        ("$37,799M", "unverified", "no tag in its sentence, row, paragraph or table"),
        ("5.7%", "recomputed", "$2,163M ÷ ×100 $37,799M")]
    assert check.figures[3].cls == "unverified"  # not 1 × $2,163M


def test_a_short_figure_is_recomputed_from_its_own_sentence_only_and_a_single_digit_never():
    check = checker().check("doc.md", "Units were 30 and 50 then. Their sum 80 is here. Of 20 and 60, 80 is the sum."
                                      " Of 2 and 4, 6.")
    assert classes(check) == [("30", "unverified"), ("50", "unverified"), ("80", "unverified"), ("20", "recomputed"),
                              ("60", "recomputed"), ("80", "recomputed"), ("2", "skipped"), ("4", "skipped"),
                              ("6", "skipped")]


# ---------------------------------------------------------------------------------------------------- arithmetic


@pytest.mark.parametrize("sentence, error", [
    ("The ratio is 3,804 ÷ 11,716 = 32.5%.", None),
    ("The ratio is 3,804 ÷ 11,716 = 33.5%.", "3,804 ÷ 11,716 = 33.5%"),
    ("Chained: 1,200 ÷ 400 = 3.0, then (1,200 − 400) × 2 = 1,600.", None),
    ("Wrong chain: 1,200 ÷ 400 = 600 ÷ 300 = 2.", "1,200 ÷ 400 = 600 ÷ 300"),
    ("After tax: $1,000M × (1 − 21%) = $790M.", None),
    ("Mixed: $3,700M + $0.5B = $4.2B, and $3.7B + $0.5B = $4,200M.", None),
    ("Market value 230.48 × 24,242M = $5,587.3B.", None),
    ("On 2026-10-09 the price was 236.90 ÷ 208.88 = 1.134.", None),
    ("A margin 12 x 3 = 36 and 1.08 ^ 2 = 1.1664.", None),
    ("Growth 1.612^(1/10) − 1 = 4.9%.", None),
    ("Growth 1.612^(1/10) − 1 = 5.9%.", "1.612^(1/10) − 1 = 5.9%"),
])
def test_written_arithmetic_is_evaluated_with_chains_brackets_percents_units_and_dates(sentence, error):
    errors, _ = fc.calc_errors(fc.markdown_units(sentence), "doc.md")
    assert [e.expression for e in errors] == ([error] if error else [])


def test_a_formula_is_never_more_than_arithmetic():
    assert fc.evaluate("10 ^ 1000") is None and fc.evaluate("2 (3)") is None and fc.evaluate("1 ÷ 0") is None
    assert fc.evaluate("$3,700M + $0.5B") == (4200.0, "M") and fc.evaluate("08 + 1") == (9.0, None)
    assert fc.evaluate("__import__('os')") is None and fc.evaluate("7") is None


def test_table_rows_under_operator_headers_are_redone_row_by_row():
    text = ("| Item | FY2025 | − H1 FY2025 | + H1 FY2026 | = TTM |\n| --- | --- | --- | --- | --- |\n"
            "| Revenue | 26,885 | 12,983 | 13,616 | 27,518 |\n| Income | 12,393 | 5,868 | 6,292 | 12,917 |\n")
    errors, results = fc.calc_errors(fc.markdown_units(text), "doc.md")
    assert [(e.line, e.computed) for e in errors] == [(4, 12817.0)] and results[0][0] == 27518.0


def test_a_distinctive_figure_equal_to_the_result_of_a_formula_that_holds_is_recomputed():
    check = checker().check("doc.md", "The ratio 3,804 ÷ 11,716 = 32.47%. Elsewhere the ratio is 32.47%.")
    assert classes(check)[-1] == ("32.47%", "recomputed")
    assert check.figures[-1].how.startswith("the result of a formula in this document that holds")


# ---------------------------------------------------------------------------------------------------- review


def review_of(text: str, doc: str = "companies/MCD/dossier.md") -> list[dict]:
    return fc.review_items([checker({"MCD-10K-FY2025": TEN_K}).check(doc, text)])


def test_each_figure_to_review_and_each_calc_error_is_an_item_with_an_empty_verdict():
    items = review_of("Line one.\n\nIt was 4% and 1,111 [src:MCD-10K-FY2025].\n\nSo 2 + 2 = 5.\n")
    assert [(i["figure"], i["class"], i["line"], i["verdict"], i["how"]) for i in items] == [
        ("4%", "weak", 3, None, None), ("1,111", "unverified", 3, None, None),
        ("2 + 2 = 5", "calc-error", 5, None, None)]
    assert items[1]["tags"] == ["MCD-10K-FY2025"] and items[1]["context"] == "It was 4% and 1,111."
    assert items[2]["checker"] == "the left side comes to 4"


def test_a_long_context_is_cut_around_its_figure_at_word_boundaries():
    text = " ".join(f"word{n}" for n in range(200)) + " the figure 1,111 here " + " ".join("x" * 5 for _ in range(200))
    context = fc.trimmed(text, "1,111", width=80)
    assert context.startswith("…") and context.endswith("…") and "1,111" in context and len(context) <= 82
    assert not context[1:].startswith(" ") and "word" in context


def test_verdicts_stay_with_their_items_when_lines_move_and_a_verdict_without_its_item_is_kept():
    first = review_of("It was 4% and 1,111 [src:MCD-10K-FY2025].\n\nThen 2,222 [src:MCD-10K-FY2025].\n")
    first[0].update(verdict="correct", how="10-K p14", reviewer="HQ")
    first[2].update(verdict="wrong", how="the filing says 2,223")
    again = review_of("A new first paragraph.\n\nIt was 4% and 1,111 [src:MCD-10K-FY2025].\n\n"
                      "Then 2,223 [src:MCD-10K-FY2025].\n")
    merged, stale = fc.merge_verdicts(again, first)
    assert [(i["figure"], i["line"], i["verdict"], i.get("reviewer")) for i in merged] == [
        ("4%", 3, "correct", "HQ"), ("1,111", 3, None, None), ("2,223", 5, None, None)]
    assert [(i["figure"], i["verdict"]) for i in stale] == [("2,222", "wrong")]


def test_a_verdict_given_in_an_earlier_run_carries_over_to_the_same_item_and_says_so():
    earlier = review_of("It was 4% and 1,111 [src:MCD-10K-FY2025].")
    earlier[1].update(verdict="cannot-verify", how="not in the 10-K")
    carried = fc.carry_verdicts(review_of("It was 4% and 1,111 [src:MCD-10K-FY2025]."), earlier,
                                "runs/MCD/2026-10-05-01A-r2/factcheck.yml")
    assert [(i["verdict"], i.get("verdict_from")) for i in carried] == [
        (None, None), ("cannot-verify", "runs/MCD/2026-10-05-01A-r2/factcheck.yml")]


def test_the_gate_passes_only_when_every_item_has_an_accepted_verdict():
    items = review_of("It was 4% and 1,111 and 2,222 [src:MCD-10K-FY2025].")
    assert not fc.gate(items).passed and len(fc.gate(items).open) == 3
    for item, verdict in zip(items, ("Correct", "not a fact", "last_digit")):
        item["verdict"] = verdict
    assert fc.gate(items).passed and fc.gate(items).accepted == {"correct": 1, "not-a-fact": 1, "last-digit": 1}
    items[1]["verdict"] = "rounding"  # not a verdict: the item stays open
    assert [i["figure"] for i in fc.gate(items).open] == ["1,111"]
    assert "is not one of correct" in fc.describe_item(fc.gate(items).open[0])
    items[1]["verdict"] = "wrong"
    result = fc.gate(items)
    assert not result.passed and not result.open and [i["figure"] for i in result.blocked] == ["1,111"]


# ---------------------------------------------------------------------------------------------------- the archive


def test_the_archive_sources_read_reports_dossiers_workings_prices_and_yields_offline(tmp_path):
    roots = runner.Roots(tmp_path / "pub", tmp_path / "priv", tmp_path / "ws")
    fx.write(roots.workspace / "inputs" / "text" / "reports__MCD.txt", REPORT)
    fx.write(roots.private / "companies" / "MCD" / "dossier.md", "dossier text")
    fx.write(roots.private / "runs" / "MCD" / "2026-10-09-01C" / "valuation_md.md", "working text")
    fx.write_yaml(roots.private / "companies" / "MCD" / "sources.yml", {"sources": [
        {"tag": "MCD-VAL-2026-10-09", "kind": "other", "location": "private:runs/MCD/2026-10-09-01C/valuation_md.md"},
        {"tag": "SPGI-VAL-2026-09-30", "kind": "other", "location": None}]})
    fx.write_yaml(roots.private / "companies" / "SPGI" / "sources.yml", {"sources": [
        {"tag": "SPGI-VAL-2026-09-30", "kind": "other", "location": "private:runs/SPGI/w.md"}]})
    fx.write(roots.private / "runs" / "SPGI" / "w.md", "spgi working")
    cache = roots.workspace / fc.PRICES_CACHE
    fx.write(cache / "MCD-2026-10-09.json", json.dumps({"data": {"tradesTable": {"rows": [
        {"date": "10/08/2026", "close": "$236.90"}, {"date": "10/07/2026", "close": "$235.00"}]}}}))
    fx.write(cache / "UST-PARYIELD-2026-2026-10-09.csv", 'Date,"10 Yr"\n10/09/2026,4.12\n10/08/2026,4.10\n')
    files = fc.ArchiveFiles(roots, {(registry.PRIVATE_REPO, "companies/MCD/dossier.md"): "planned dossier"})
    client = edgar.EdgarClient(offline=True, cache_dir=tmp_path / "edgar")
    sources = fc.ArchiveSources(files, "MCD", offline=True, client=client)
    assert sources.document("MCD-RPT1-2026-09-01") == fc.SourceText(REPORT, "the owner's report text", report=True)
    assert sources.document("MCD-DOSSIER-2026-10-09").text == "planned dossier"  # what the placement writes
    assert sources.document("MCD-VAL-2026-10-09").text == "working text"
    assert sources.document("SPGI-VAL-2026-09-30").text == "spgi working"  # MCD's stub entry does not hide it
    assert sources.price("MCD-PRICES-2026-10-09#2026-10-08") == (236.9, "")
    assert sources.price("MCD-PRICES-2026-10-09") == (236.9, "")  # the close on or before the day read
    assert sources.price("UST-PARYIELD-2026-10-09") == (4.12, "")
    assert sources.price("MCD-PRICES-2026-10-10") == (None, "offline: no cached prices of MCD read on 2026-10-10")
    assert sources.price("UST-PARYIELD-2026-10-12")[1] == "offline: no cached Treasury table for 2026-10-12"
    unread = sources.document("MCD-10K-FY2025")
    assert unread.text is None and unread.note.startswith("registered in no sources.yml, and unresolved on EDGAR: "
                                                          "offline mode")


def test_the_evidence_is_the_valuation_its_working_in_force_other_valuations_and_the_dossier(tmp_path):
    roots = runner.Roots(tmp_path / "pub", tmp_path / "priv", tmp_path / "ws")
    fx.write(roots.private / "companies" / "MCD" / "valuation.yml",
             "center: 208.9\nnote: see [src:MCD-VAL-2026-10-09]\n")
    fx.write_yaml(roots.private / "companies" / "MCD" / "sources.yml", {"sources": [
        {"tag": "MCD-VAL-2026-10-09", "location": "private:runs/MCD/2026-10-09-01C-rerun2/valuation_md.md"}]})
    fx.write(roots.private / "runs" / "MCD" / "2026-10-09-01C-rerun2" / "valuation_md.md", "in force")
    fx.write(roots.private / "runs" / "MCD" / "2026-10-10-01C" / "valuation_md.md", "returned, never placed")
    fx.write(roots.private / "companies" / "BRK" / "valuation.yml", "implied_return: 7.2\n")
    fx.write_yaml(roots.public / "companies" / "BRK" / "thesis.yml", {"name": "Berkshire Hathaway Inc."})
    fx.write(roots.private / "companies" / "MCD" / "dossier.md", "dossier")
    evidence, key_figures, names = fc.archive_evidence(fc.ArchiveFiles(roots), "MCD")
    assert [(e.cls, e.doc, e.text[:8], e.company) for e in evidence] == [
        ("valuation", "companies/MCD/valuation.yml", "center: ", None),
        ("valuation", "runs/MCD/2026-10-09-01C-rerun2/valuation_md.md", "in force", None),
        ("valuation", "companies/BRK/valuation.yml", "implied_", "BRK"),
        ("dossier", "companies/MCD/dossier.md", "dossier", None)]
    assert [n.text for n in key_figures] == ["208.9"] and names == {"BRK": ["BRK", "Berkshire Hathaway", "Berkshire"]}


# ---------------------------------------------------------------------------------------------------- the gate


def test_the_archive_documents_are_named_in_the_placement_table():
    assert outputs.ARCHIVE_DOCUMENTS <= set(outputs.PLACEMENT)
    assert not outputs.ARCHIVE_DOCUMENTS & {"findings", "fact_verdicts", "questions", "sources_additions", "prereg",
                                            "pr_body", "mistakes_entry", "letter", "archive_patch"}


REPORT_MD = ("---\ncompany: APP\ndoc: report_02\nas_of: 2026-10-20\ndoc_status: draft\n---\n\n# AppLovin\n\n"
             "Revenue was $1,234.5M [src:APP-10K-FY2025]. Margins rose 4% [src:APP-10K-FY2025].\n\n"
             "Nobody cites 7,777.\n")
SOURCES = FakeSources({"APP-10K-FY2025": "Revenue 1,234.5 million; growth 4 percent and 4% of 14 units."})


@pytest.fixture
def env(tmp_path):
    return fx.make_env(tmp_path)


def report_bundle(env, text: str = REPORT_MD, *, client: str = "claude-code") -> Path:
    """A succeeded 02 bundle of APP with a research report and its questions, as a real backend would leave it."""
    run_dir = "2026-10-20-02"
    bundle = env.private / "runs" / "APP" / run_dir
    files = {"report": ("outputs/report.md", "markdown", text), "questions": ("outputs/questions.yml", "yaml", "[]\n")}
    for _, (rel, _, content) in files.items():
        fx.write(bundle / rel, content)
    fx.write_yaml(bundle / runner.MANIFEST, {
        "manifest_version": runner.MANIFEST_VERSION, "bundle": f"runs/APP/{run_dir}", "step": "02", "company": "APP",
        "scope": "APP", "period": "FY2026Q3", "run_date": "2026-10-20", "prompt": {"id": "02"},
        "context": {"company": "APP", "run_date": "2026-10-20", "run_dir": run_dir, "scope": "APP"}})
    fx.write_yaml(bundle / runner.RUN_RECORD, {"status": "succeeded", "backend": client, "client": client, "outputs": {
        name: {"status": "written", "file": rel, "format": fmt, "sha256": registry.sha256_text(content)}
        for name, (rel, fmt, content) in files.items()}})
    return bundle


def fact_check(env, bundle: Path, **kwargs) -> dict:
    return runner.fact_check(bundle, roots=env.roots, sources=SOURCES, now=NOW, out=io.StringIO(), **kwargs)


def place(env, bundle: Path, **kwargs) -> dict:
    kwargs.setdefault("factcheck_sources", SOURCES)
    kwargs.setdefault("out", io.StringIO())
    return runner.place(bundle, roots=env.roots, lint=False, now=NOW, **kwargs)


def review_file(bundle: Path) -> dict:
    return yaml.safe_load((bundle / runner.FACTCHECK_FILE).read_text(encoding="utf-8"))


def give_verdicts(bundle: Path, *verdicts: str) -> None:
    data = review_file(bundle)
    for item, verdict in zip(data["items"], verdicts):
        item.update(verdict=verdict, how="checked by hand")
    runner.write_yaml(bundle / runner.FACTCHECK_FILE, data)


def test_factcheck_writes_the_review_file_with_counts_by_class_and_one_item_per_figure_to_review(env):
    bundle = report_bundle(env)
    data = fact_check(env, bundle)
    doc = "reports/APP/2026-10-20-02/report.md"
    assert data["bundle"] == "runs/APP/2026-10-20-02" and data["checked_at"] == "2026-10-20T12:00:00Z"
    assert data["documents"] == {doc: {"output": "report", "figures": 3, "classes": {"found": 1, "weak": 1,
                                                                                   "unverified": 1},
                                       "calc_errors": 0, "to_review": 2}}
    assert [(i["doc"], i["figure"], i["class"], i["line"], i["verdict"], i["how"]) for i in data["items"]] == [
        (doc, "4%", "weak", 10, None, None), (doc, "7,777", "unverified", 12, None, None)]
    assert data["totals"] == {"figures": 3, "to_review": 2, "accepted": 0, "open": 2, "blocking": 0, "verdicts": {}}
    assert review_file(bundle) == data and set(data["verdicts"]) == set(fc.VERDICTS)


def test_placement_stops_while_an_item_has_no_accepted_verdict_and_lists_the_items_like_lint_errors(env):
    bundle = report_bundle(env)
    with pytest.raises(runner.FactCheckOpen, match=r"(?s)2 of the 2 item\(s\) to review in 3 figures have no accepted "
                                                   r"verdict.*factcheck runs/APP/2026-10-20-02.*"
                                                   r"report.md:10: 4% \[weak\].*report.md:12: 7,777 \[unverified\]"):
        place(env, bundle)
    fact_check(env, bundle)
    give_verdicts(bundle, "correct")
    with pytest.raises(runner.FactCheckOpen, match=r"1 of the 2 item"):
        place(env, bundle, check=True)
    assert not (env.private / "reports" / "APP").exists() and not (bundle / runner.PLACEMENT_RECORD).exists()


def test_placement_goes_ahead_with_accepted_verdicts_and_records_the_fact_check(env):
    bundle = report_bundle(env)
    fact_check(env, bundle)
    give_verdicts(bundle, "correct", "Not a fact")
    out = io.StringIO()
    report = place(env, bundle, check=True, out=out)
    assert "fact check: 1 archive document(s), 3 figures; 2 item(s) to review, every one with an accepted verdict " \
           "(correct 1, not-a-fact 1)" in out.getvalue()
    report = place(env, bundle)
    record = yaml.safe_load((bundle / runner.PLACEMENT_RECORD).read_text(encoding="utf-8"))["factcheck"]
    assert record == report["factcheck"] and record["review_file"] == "runs/APP/2026-10-20-02/factcheck.yml"
    assert record["review_sha256"] == registry.sha256_bytes((bundle / runner.FACTCHECK_FILE).read_bytes())
    assert record["totals"]["verdicts"] == {"correct": 1, "not-a-fact": 1}
    assert (env.private / "reports" / "APP" / "2026-10-20-02" / "report.md").read_text(encoding="utf-8") == REPORT_MD


def test_a_wrong_figure_blocks_placement_until_the_text_is_corrected_and_the_item_is_gone(env):
    bundle = report_bundle(env)
    fact_check(env, bundle)
    give_verdicts(bundle, "correct", "wrong")
    with pytest.raises(runner.FactCheckOpen, match=r"(?s)1 item\(s\) have the verdict wrong or cannot-verify: "
                                                   r"correct the text.*7,777 \[unverified\] verdict wrong"):
        place(env, bundle)
    corrected = REPORT_MD.replace("Nobody cites 7,777.", "Revenue again: $1,234.5M [src:APP-10K-FY2025].")
    fx.rewrite_output(bundle, "report", corrected)
    data = fact_check(env, bundle)
    assert [(i["figure"], i["verdict"]) for i in data["items"]] == [("4%", "correct")]
    assert [(i["figure"], i["verdict"]) for i in data["no_longer_found"]] == [("7,777", "wrong")]
    assert place(env, bundle)["factcheck"]["totals"]["accepted"] == 1


def test_a_rerun_keeps_the_verdicts_when_the_lines_move(env):
    bundle = report_bundle(env)
    fact_check(env, bundle)
    give_verdicts(bundle, "correct", "cannot-verify")
    fx.rewrite_output(bundle, "report", REPORT_MD.replace("# AppLovin\n", "# AppLovin\n\nA new paragraph.\n"))
    data = fact_check(env, bundle)
    assert [(i["figure"], i["line"], i["verdict"], i["how"]) for i in data["items"]] == [
        ("4%", 12, "correct", "checked by hand"), ("7,777", 14, "cannot-verify", "checked by hand")]
    assert data["totals"]["blocking"] == 1 and "no_longer_found" not in data


def test_an_earlier_runs_verdict_reaches_the_same_item_in_a_new_bundle(env):
    first = report_bundle(env)
    fact_check(env, first)
    give_verdicts(first, "correct", "derived-correct")
    data = review_file(first)
    second = first.parent / "2026-10-21-02"
    for item in data["items"]:  # a document of the same path in a later run (a dossier patched by an update, say)
        item["doc"] = "reports/APP/2026-10-21-02/report.md"
    fx.write_yaml(second / runner.FACTCHECK_FILE, {"items": []})
    runner.write_yaml(first / runner.FACTCHECK_FILE, data)
    again = runner.fact_check(report_bundle_at(env, "2026-10-21-02"), roots=env.roots, sources=SOURCES, now=NOW,
                              out=io.StringIO())
    origin = "runs/APP/2026-10-20-02/factcheck.yml"
    assert [(i["verdict"], i.get("verdict_from")) for i in again["items"]] == [
        ("correct", origin), ("derived-correct", origin)]


def report_bundle_at(env, run_dir: str) -> Path:
    bundle = report_bundle(env)
    target = bundle.parent / run_dir
    for path in sorted(bundle.rglob("*")):
        if path.is_file() and path.name != runner.FACTCHECK_FILE:
            fx.write(target / path.relative_to(bundle), path.read_text(encoding="utf-8"))
    manifest = yaml.safe_load((target / runner.MANIFEST).read_text(encoding="utf-8"))
    manifest["bundle"] = f"runs/APP/{run_dir}"
    manifest["run_date"] = manifest["context"]["run_date"] = run_dir[:10]
    manifest["context"]["run_dir"] = run_dir
    runner.write_yaml(target / runner.MANIFEST, manifest)
    return target


def test_the_fake_clients_placeholders_are_not_checked_and_the_command_refuses_them(env):
    bundle = report_bundle(env, client="fake")
    report = runner.place(bundle, roots=env.roots, lint=False, now=NOW, out=io.StringIO(), allow_fake=True,
                          check=True)
    assert report["factcheck"] == {"skipped": "outputs of the fake client or a test double are placeholders, never "
                                              "placed for real"}
    with pytest.raises(runner.RunnerError, match="are placeholders and are never placed"):
        fact_check(env, bundle)


def test_the_command_line_writes_the_review_file_where_asked_and_exits_1_while_items_are_open(env, monkeypatch,
                                                                                             tmp_path):
    bundle = report_bundle(env)
    monkeypatch.setattr(fc, "ArchiveSources", lambda files, company, **kwargs: SOURCES)
    args = ["factcheck", "runs/APP/2026-10-20-02", "--public-root", str(env.public), "--private-root",
            str(env.private), "--workspace-root", str(env.workspace), "--offline", "--out", str(tmp_path / "f.yml")]
    assert runner.main(args) == 1
    data = yaml.safe_load((tmp_path / "f.yml").read_text(encoding="utf-8"))
    assert data["offline"] is True and data["totals"]["open"] == 2 and not (bundle / runner.FACTCHECK_FILE).exists()
    for item in data["items"]:
        item["verdict"] = "correct"
    runner.write_yaml(tmp_path / "f.yml", data)
    assert runner.main(args) == 0


def test_the_chain_waits_for_the_verdicts_instead_of_failing(env, monkeypatch):
    run = env.private / "runs" / "APP" / "2026-10-20-03R"
    fx.write_yaml(run / runner.MANIFEST, {"bundle": "runs/APP/2026-10-20-03R", "step": "03R", "company": "APP",
                                          "period": "FY2026Q3"})
    fx.write_yaml(run / runner.RUN_RECORD, {"status": "succeeded"})

    def blocked(bundle, **kwargs):
        raise runner.FactCheckOpen("cannot place runs/APP/2026-10-20-03R:\n- fact check: 2 of the 2 item(s)")

    monkeypatch.setattr(runner, "place", blocked)
    state = chain._place_all(None, roots=env.roots, base=env.private, ticker="APP", period="FY2026Q3", dry_run=False,
                             gateway=None, schemas_dir=None, lint=False)
    assert (state.state, state.bundle) == ("waiting", "runs/APP/2026-10-20-03R") and "fact check" in state.detail
