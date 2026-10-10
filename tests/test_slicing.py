"""pipeline/slicing.py: one part as several calls when one cannot take its inputs (docs/decisions/0026)."""

from __future__ import annotations

import datetime as dt
import re

import yaml

from pipeline import documents, slicing

TABLE = "===== source table (public and private sources.yml) =====\nsources:\n- tag: APP-10Q-FY2026Q2\n"


def product(*blocks: tuple[str, str]) -> str:
    body = "".join(f"===== {header} =====\n{text}\n" for header, text in blocks)
    return "The product under audit.\n\n" + body + TABLE


def test_estimates_count_ascii_at_the_calibrated_rate_and_other_characters_as_one_token_each():
    assert slicing.estimate_tokens("a" * 260) == 100
    assert slicing.estimate_tokens("a" * 26 + "é" * 5) == 15


def test_a_product_that_fits_is_not_sliced():
    assert slicing.split_product(product(("output: update (update.md)", "Installs grew.")), limit=1000) is None


def test_a_product_is_cut_between_blocks_and_every_slice_keeps_the_title_and_the_source_table():
    text = product(("output: update (update.md)", "a" * 260), ("output: pr_body (pr_body.md)", "b" * 260),
                   ("changes: thesis.yml", "c" * 260))
    parts = slicing.split_product(text, limit=150)
    assert len(parts) == 3
    for number, part in enumerate(parts, 1):
        assert part.startswith("The product under audit.\n\n[Pipeline note: this call gets slice "
                               f"{number} of 3 of the product")
        assert part.endswith(TABLE) and part.count("===== source table") == 1
    assert "===== output: pr_body (pr_body.md) =====\n" + "b" * 260 in parts[1] and "a" * 260 not in parts[1]


def test_a_block_longer_than_the_limit_is_cut_at_headings_into_numbered_parts():
    body = "".join(f"## Section {i}\n" + "x" * 200 + "\n" for i in range(6))
    parts = slicing.split_product(product(("output: update (update.md)", body)), limit=200)
    headers = [re.findall(r"^===== (?!source).* =====$", p, flags=re.M) for p in parts]
    assert len(parts) >= 3 and headers[0] == [f"===== output: update (update.md) (part 1 of {len(parts)}) ====="]
    assert all(p.split("=====\n", 1)[1].lstrip().startswith("## Section") for p in parts[1:] if "## Section" in p)
    assert "".join(re.findall(r"## Section \d", "".join(parts))) == "".join(f"## Section {i}" for i in range(6))


def test_merged_fact_tables_are_renumbered_in_slice_order_with_derived_inputs_to_match():
    first = {"as_of": dt.date(2026, 10, 20), "facts": [{"id": "F001", "value": 1}, {"id": "F002", "value": 2}],
             "generated_by": {"model": "m"}}
    second = {"as_of": dt.date(2026, 10, 20), "facts": [
        {"id": "F001", "value": 3},
        {"id": "F002", "value": 4, "derived": True, "inputs": ["F001", "APP-10Q-FY2026Q2"]}]}
    merged, maps = slicing.merge_fact_tables([first, second])
    assert [f["id"] for f in merged["facts"]] == ["F001", "F002", "F003", "F004"]
    assert merged["facts"][3]["inputs"] == ["F003", "APP-10Q-FY2026Q2"]
    assert maps == [{"F001": "F001", "F002": "F002"}, {"F001": "F003", "F002": "F004"}]
    assert merged["as_of"] == dt.date(2026, 10, 20) and merged["generated_by"] == {"model": "m"}
    assert list(merged) == ["as_of", "facts", "generated_by"]


def test_facts_are_packed_by_the_documents_they_cite():
    q, k, old = "10Q", "10K", "10K-OLD"
    tokens = {q: 100, k: 300, old: 300}
    docs = [frozenset({q})] * 5 + [frozenset({k})] * 2 + [frozenset({q, k})] + [frozenset()]
    slices = slicing.pack_facts(docs, tokens, max_facts=4, soft=150, hard=1000)
    by_docs = {tuple(sorted(s.documents)): s.facts for s in slices}
    # the fact citing both documents opens a slice; the 10-K facts join it at no cost, and so does the last 10-Q
    # fact once the first four 10-Q facts have filled a slice of their own; the fact that needs no document is left
    assert by_docs[(k, q)] == [4, 5, 6, 7] and by_docs[(q,)] == [0, 1, 2, 3] and by_docs[()] == [8]
    assert sorted(i for s in slices for i in s.facts) == list(range(9))
    assert [min(s.facts) for s in slices] == sorted(min(s.facts) for s in slices)


def test_documents_over_the_hard_limit_are_cut_to_windows_in_priority_order():
    tokens = {"new": 400, "mid": 400, "old": 400}
    (only,) = slicing.pack_facts([frozenset(tokens)], tokens, priority=["new", "mid", "old"], hard=900)
    assert only.documents == ["new", "mid"] and only.cut == ["old"]


def test_windows_keep_the_lines_around_matches_and_report_how_many():
    text = "\n".join(f"line {i}" for i in range(100)) + "\nRevenue was 12,256 million.\n" + "tail\n" * 50
    cut, hits = slicing.windows(text, documents.value_patterns("12,256 against 11,200"), lines_around=2)
    assert hits == 1 and cut.splitlines() == ["line 98", "line 99", "Revenue was 12,256 million.", "tail", "tail"]
    assert slicing.windows("nothing", documents.value_patterns(123456)) == ("", 0)


def test_a_slice_of_the_fact_table_names_itself_and_carries_the_inputs_of_its_derived_facts():
    rows = [{"id": "F001", "value": 1}, {"id": "F002", "value": 2},
            {"id": "F003", "derived": True, "inputs": ["F001", "F002"], "value": 3}]
    table = slicing.slice_fact_table({"as_of": "2026-10-20", "facts": rows}, rows, [1, 2], 2, 3)
    assert table["slice"]["number"] == 2 and table["slice"]["of"] == 3
    assert "Give a verdict for every fact under facts and for no other" in table["slice"]["note"]
    assert [f["id"] for f in table["facts"]] == ["F002", "F003"]
    assert [f["id"] for f in table["context_facts"]] == ["F001"] and table["as_of"] == "2026-10-20"
    alone = slicing.slice_fact_table({"facts": rows}, rows, [0], 1, 3)
    assert "context_facts" not in alone and "context_facts" not in alone["slice"]["note"]


def test_merged_audits_follow_the_fact_order_and_renumber_findings_and_questions_with_their_references():
    one = {"fact_verdicts": [{"id": "F003", "verdict": "accurate"}],
           "findings": [{"id": "04A-01", "group": "must fix", "evidence": "see 04A-01"}],
           "questions": [{"id": "Q1", "issue": "basis of 04A-01?"}]}
    two = {"fact_verdicts": [{"id": "F001", "verdict": "error"}, {"id": "F002", "verdict": "accurate"}],
           "findings": [{"id": "04A-01", "group": "should fix", "fix": "as in 04A-01, not 04A-010"}],
           "questions": None}
    merged, maps = slicing.merge_audits([one, two], ["F001", "F002", "F003"])
    assert [v["id"] for v in merged["fact_verdicts"]] == ["F001", "F002", "F003"]
    assert [(f["id"], f.get("evidence") or f.get("fix")) for f in merged["findings"]] == [
        ("04A-01", "see 04A-01"), ("04A-02", "as in 04A-02, not 04A-010")]
    assert merged["questions"] == [{"id": "04A-Q01", "issue": "basis of 04A-01?"}]
    assert maps == [{"04A-01": "04A-01", "Q1": "04A-Q01"}, {"04A-01": "04A-02"}]


def test_verdict_coverage_names_missing_extra_and_repeated_facts():
    verdicts = [{"id": "F001"}, {"id": "F001"}, {"id": "F009"}]
    errors = slicing.verdict_coverage(["F001", "F002"], verdicts)
    assert errors == [
        "fact_verdicts: no verdict for F002; every fact under facts in the fact_table gets exactly one verdict (04A)",
        "fact_verdicts: F009 is not a fact under facts in the fact_table; give verdicts for those facts only (04A)",
        "fact_verdicts: F001 has more than one verdict (04A)"]
    assert slicing.verdict_coverage(["F001"], [{"id": "F001"}]) == []


def test_values_are_matched_as_filings_print_them():
    assert documents.value_forms("12,256 against 11,200") == ["12,256 against 11,200", "12,256", "11,200"]
    assert documents.value_forms(dt.date(2026, 7, 24)) == ["July 24, 2026", "2026-07-24"]
    assert documents.cut_excerpt("Filed on July 24, 2026 by the company.", dt.date(2026, 7, 24)) == \
        "Filed on July 24, 2026 by the company."
    assert documents.cut_excerpt("Interest income\n12,256\n11,200\n", "12,256 against 11,200") == \
        "Interest income … 12,256"
    distinctive = documents.distinctive_patterns("about 25 (2024); 8.3 against 12,256")
    assert not any(p.search("In 2024 about 25 stores") for p in distinctive)  # years and short numbers are left out
    assert any(p.search("rose to 12,256 million") for p in distinctive) and any(p.search("8.3%") for p in distinctive)
    assert documents.distinctive_patterns(5) == [] and documents.distinctive_patterns("growth") == []
    assert yaml.safe_load(yaml.safe_dump({"v": documents.value_forms(-3)})) == {
        "v": ["(3)", "(3.0)", "-3", "-3.0", "−3", "−3.0"]}  # "$3.0 billion" too


def test_a_short_number_needs_two_shared_words_a_rare_one_or_its_rows_own_label():
    """NVIDIA's first audit (2026-10-08, 04A-Q03 to -Q21): excerpts attached on one common shared word."""
    rows = "\n".join([f"Line {i} on operating results, revenue and other matters of the year." for i in range(200)])
    doc = rows + "\nOperating leases (2) (3)\nRevenue $ 215,938 $ 130,497 Up 65%\nResearch and development 8.6 9.9\n"
    assert documents.cut_excerpt(doc, -2, words="Operating income change") is None  # "operating" alone, and common
    assert documents.cut_excerpt(doc, 65, words="Revenue growth") == "Revenue $ 215,938 $ 130,497 Up 65%"  # its label
    assert documents.cut_excerpt(doc, 8.6, words="R&D as share of revenue") == "Research and development 8.6 9.9"
    assert documents.cut_excerpt("(1) Net income divided by weighted average shares.\n", -1,
                                 words="diluted weighted shares change") is None  # a footnote marker, not -1
    whole = "Payments related to repurchases of common stock ( 39,044 ) ( 23,815 )\n"
    assert documents.cut_excerpt(whole, 39044, words="Buybacks (cash flow)") == whole.strip()  # five digits suffice


def test_a_derived_figure_is_attached_only_where_the_filing_prints_it_with_its_own_words():
    doc = "Proceeds from sales of equity securities 7,241 70\nOperating margin 62.4% in the year\n"
    assert documents.cut_excerpt(doc, 7241, words="stock-based compensation, trailing twelve months",
                                 strict=True) is None
    assert documents.cut_excerpt(doc, 7241, words="stock-based compensation, trailing twelve months") == \
        "Proceeds from sales of equity securities 7,241 70"  # not strict: a distinct number is attached as before


def test_values_given_as_text_are_searched_as_the_filing_prints_them():
    assert documents.value_forms("36") == ["36", "36.0"] and documents.value_forms("4.90") == ["4.90", "4.9"]
    assert documents.value_forms("2026-01-25") == ["January 25, 2026", "2026-01-25"]
    assert documents.value_forms(17) == ["17", "17.0"] and documents.value_forms(1234) == ["1,234", "1234"]
    assert documents.cut_excerpt("In June 2026, we issued an aggregate of $25.0 billion of senior notes.\n", "25",
                                 words="Senior notes issued") == \
        "In June 2026, we issued an aggregate of $25.0 billion of senior notes."
    assert {"researc", "develop", "repurch", "tax", "custome"} <= documents._keywords(
        "R&D, buybacks, income tax and customers")  # abbreviations, synonyms, short words, plurals, stems


def test_a_statement_locator_finds_the_statement_not_its_line_in_the_index():
    text = "\n".join(["Index to Consolidated Financial Statements", "Consolidated Statements of Income 50",
                     "Consolidated Balance Sheets 52", "Report of Independent Registered Public Accounting Firm",
                     "Consolidated Statements of Income", "Revenue 215,938", "Cost of revenue 62,475",
                     "Net income 120,067", "Consolidated Statements of Comprehensive Income", "Net income 120,067",
                     "Consolidated Balance Sheets", "Cash 11,486", "Total assets 206,803",
                     "Notes to the Consolidated Financial Statements", "Note 1 - Organization"])
    income = documents.locator_section(text, "IS")
    assert income.startswith("Consolidated Statements of Income\nRevenue 215,938") and "Comprehensive" not in income
    balance = documents.locator_section(text, "BS")
    assert balance.splitlines()[1:] == ["Cash 11,486", "Total assets 206,803"]
    assert documents.locator_section(text, "CF") is None


def test_an_excerpt_prefers_a_sentence_about_the_fact_and_skips_a_bare_coincidence():
    """MCD's audit (2026-09-28): excerpts matched bare numbers in unrelated sentences."""
    doc = "Foreign currency translation 5.7\nThe effective tax rate was 5.7 points lower.\nOperating margin was 45.6%.\n"
    assert documents.cut_excerpt(doc, 5.7, words="effective tax rate change") == \
        "The effective tax rate was 5.7 points lower."
    assert documents.cut_excerpt(doc, 5.7, words="restaurant count") is None  # a bare short number, nothing shared
    assert documents.cut_excerpt(doc, 45.6, words="operating margin") == "Operating margin was 45.6%."
