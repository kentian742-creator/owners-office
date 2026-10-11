"""The source excerpts the pipeline cuts for the fact audit (documents.cut_excerpt, registry.cut_source_excerpts), and
the script that measures them on a past run (scripts/excerpt_eval.py). The cases are modelled on Walmart's fact audit
of 2026-10-09, whose questions name excerpts cut from unrelated lines (04A-Q01 to -Q27)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

from pipeline import documents, registry

SPEC = importlib.util.spec_from_file_location("excerpt_eval",
                                              Path(__file__).resolve().parents[1] / "scripts" / "excerpt_eval.py")
excerpt_eval = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(excerpt_eval)

FILLER = [f"Paragraph {i} discusses the results of operations and other matters of the year." for i in range(60)]
LAG = "International operations are consolidated using a one-month lag."
VESTING = "Restricted stock vests over periods between one month and three years."


def test_the_values_own_words_are_not_words_it_shares_with_the_fact():
    """04A-Q01 (F161): a consolidation lag of "one month" got a vesting period of one month, since "month" counted as
    a word the line shares with the fact."""
    doc = "\n".join([*FILLER, VESTING])
    assert documents.cut_excerpt(doc, "one month", "consolidation lag of one month") is None
    assert documents.cut_excerpt("\n".join([*FILLER, VESTING, LAG]), "one month", "consolidation lag",
                                 entity="International") == LAG  # "one month" is also found as "one-month"


def test_common_words_cannot_qualify_a_line_on_their_own():
    """04A-Q01 (F190): a segment's margin of 4.3% got "Walmart U.S. comparable sales increased 4.3%", which shares
    "Walmart" and "sales" with it, words in a large share of a retailer's 10-K. Two words used to suffice."""
    common = [f"Net sales at store {i} grew, and sales of groceries led." for i in range(40)]
    doc = "\n".join([*common, *FILLER, "Net sales increased 4.3% and 4.8% in the last two years."])
    assert documents.cut_excerpt(doc, 4.3, "net sales margin") is None
    margin = "The margin on net sales was 4.3% in the year."
    assert documents.cut_excerpt(f"{doc}\n{margin}", 4.3, "net sales margin") == margin  # "margin" is not common


SEGMENTS = """Walmart International Segment
Fiscal Years Ended January 31,
(Amounts in millions) 2021 2020 2019
Net sales $ 121,360 $ 120,130 $ 120,824
Operating income as a percentage of net sales 3.0 % 2.8 % 4.0 %
Net sales for the Walmart International segment increased $1.2 billion or 1.0% for fiscal 2021.
Sam's Club Segment
Fiscal Years Ended January 31,
(Amounts in millions) 2021 2020 2019
Net sales $ 63,910 $ 58,792 $ 57,839
Operating income as a percentage of net sales 3.0 % 2.8 % 2.6 %
Net sales for the Sam's Club segment increased $5.1 billion or 8.7% for fiscal 2021."""
MARGIN = "segment operating income ÷ segment net sales"


def margin_row(value: float, entity: str, period: str) -> str | None:
    return documents.cut_excerpt(SEGMENTS, value, MARGIN, entity=entity, unit="%", period=period, filing_year=2021)


def test_the_row_of_the_facts_entity_and_the_column_of_its_fiscal_year():
    """04A-Q10 (F194, F195): Sam's Club's margins of 2.8% and 3.0% got the International segment's row, which holds
    the same figures under the same label; 04A-Q05 (F533): a growth of 5.1% for fiscal 2026 got a row whose 5.1%
    stands in the column of fiscal 2024."""
    assert margin_row(2.8, "Sam's Club U.S.", "FY2020").endswith("3.0 % 2.8 % 2.6 %")
    assert margin_row(3.0, "Sam's Club U.S.", "FY2021").endswith("3.0 % 2.8 % 2.6 %")
    assert margin_row(2.8, "International", "FY2020").endswith("3.0 % 2.8 % 4.0 %")
    assert margin_row(4.0, "International", "FY2019").endswith("3.0 % 2.8 % 4.0 %")
    assert margin_row(4.0, "International", "FY2020") is None  # 4.0 stands in the column of fiscal 2019
    assert margin_row(4.0, "Mexico", "FY2019") is None  # no table or line names the entity
    row = "Net sales percentage change from comparable period 4.4 % 4.7 % 5.1 %"
    table = f"(Amounts in millions) 2026 2025 2024\n{row}"
    growth = {"words": "net sales growth", "unit": "%", "filing_year": 2026}
    assert documents.cut_excerpt(table, 5.1, period="FY2026", **growth) is None
    assert documents.cut_excerpt(table, 5.1, period="FY2024", **growth) == row
    assert documents.cut_excerpt(table, 5.1, period="FY2024", **{**growth, "filing_year": 2025}) == row  # no check


def test_a_figure_in_a_sentence_about_other_fiscal_years_is_another_years_figure():
    """A derived growth of 4.7% for fiscal 2026 got a segment's growth of 4.7% for fiscal 2025 from the year before's
    10-K: when the filing names its fiscal years as the archive does, a sentence that names only other fiscal years
    states another year's figure."""
    doc = "\n".join(["Fiscal 2026 was the company's sixtieth year.",
                     "Net sales increased $20.6 billion or 4.7% and $21.3 billion or 5.1% for fiscal 2025 and 2024.",
                     "Net sales increased $31.9 billion or 4.7% and $31.9 billion or 5.0% for fiscal 2026 and 2025."])
    growth = {"words": "net sales growth", "unit": "%", "period": "FY2026"}
    assert documents.cut_excerpt(doc, 4.7, filing_year=2026, **growth).endswith("for fiscal 2026 and 2025.")
    year_before = "\n".join(doc.replace("Fiscal 2026", "Fiscal 2025").splitlines()[:2])  # the 10-K for fiscal 2025
    assert documents.cut_excerpt(year_before, 4.7, filing_year=2025, **growth) is None


def test_an_amount_in_billions_is_found_where_the_filing_prints_millions():
    """04A-Q22 (F282): the prior year's adjusted operating income of $7.876 billion stands in the release as 7,876, in
    millions; 04A-Q10 (F386): an average price of 135.2 stands as $135.20."""
    row = "Adjusted operating income $ 9,383 $ 7,876"
    words = "prior-year adjusted operating income"
    assert documents.cut_excerpt(row, 7.876, words, unit="USD bn") == row
    assert documents.cut_excerpt(row, 7.876, words) is None  # without the unit there is nothing to scale
    assert documents.value_forms(7.876, "USD bn") == ["7.876", "7,876", "7876"]
    assert documents.value_forms(1500, "USD m") == ["1,500", "1500", "1.5", "1.50"]  # "$1.5 billion"
    assert documents.value_forms(85, "m shares") == ["85", "85.0"]  # share counts are not scaled
    assert "135.20" in documents.value_forms(135.2)
    assert any(p.search(row) for p in documents.distinctive_patterns(7.876, "USD bn"))  # slices keep the line


def test_a_value_printed_with_another_unit_is_another_figure():
    """04A-Q01, -Q25 and -Q27 (F184, F192, F597): margins of 5.0% and 3.9% got "decreased 5 basis points" and
    "increases of $3.9 billion", and a share of revenue of 0.5% got "$ 0.5 billion"; 04A-Q18 (F240): a multiple of
    1.9 got "$1.9 billion"."""
    margin = "Segment operating income as a percentage of segment net sales was 5.0% and 3.9%."
    doc = "\n".join([*FILLER, "Operating expenses as a percentage of segment net sales decreased 5 basis points.",
                     "Segment operating income increased by $3.9 billion, and net sales by $5.0 billion.", margin])
    assert documents.cut_excerpt(doc, 5.0, MARGIN, unit="%") == margin
    assert documents.cut_excerpt(doc, 3.9, MARGIN, unit="%") == margin
    assert documents.cut_excerpt(doc, 3.9, MARGIN, unit="USD bn").startswith("Segment operating income increased by")
    vizio = "We bought VIZIO for $1.9 billion, which raised payments for property and equipment."
    assert documents.cut_excerpt(f"{doc}\n{vizio}", 1.9, "capex ÷ depreciation", unit="x", strict=True) is None


def test_a_derived_figure_carries_its_inputs_excerpts_before_any_match_of_its_own():
    """04A-Q18 (F240): capital expenditure over depreciation of 1.9 times got the sentence about an acquisition for
    $1.9 billion. A derived figure takes its inputs' excerpts, a figure derived from derived ones too; only when an
    input has none is the figure looked for itself, and then strictly."""
    doc = "\n".join([*FILLER, "Payments for property and equipment ( 26,642 ) ( 23,783 )",
                     "Depreciation and amortization 14,203 12,973",
                     "We bought VIZIO for 1.9 billion, which raised payments for property and equipment."])
    source = "EX-10K-FY2026"
    facts = [
        {"id": "F4", "subject": "Example", "what": "capex ÷ D&A, doubled", "value": 3.8, "unit": "x", "source": source,
         "derived": True, "inputs": ["F3"]},
        {"id": "F3", "subject": "Example", "what": "capex ÷ depreciation and amortization", "value": 1.9, "unit": "x",
         "source": source, "derived": True, "inputs": ["F1", "F2"]},
        {"id": "F1", "subject": "Example", "what": "capex", "value": 26.642, "unit": "USD bn", "source": source},
        {"id": "F2", "subject": "Example", "what": "depreciation and amortization", "value": 14.203, "unit": "USD bn",
         "source": source},
        {"id": "F5", "subject": "Example", "what": "capex ÷ depreciation and amortization", "value": 1.9, "unit": "x",
         "source": source, "derived": True, "inputs": ["F1", "F9"]},
    ]
    assert registry.cut_source_excerpts(facts, {source: doc}, ["EX", "Example"]) == (4, 1)
    by_id = {fact["id"]: fact for fact in facts}
    assert by_id["F1"]["source_excerpt"] == "Payments for property and equipment ( 26,642 ) ( 23,783 )"
    assert by_id["F3"]["source_excerpt"].startswith("derived from F1, F2: [F1] Payments for property and equipment")
    assert by_id["F4"]["source_excerpt"].startswith("derived from F3: [F3] derived from F1, F2:")
    assert by_id["F5"]["excerpt_missing"] is True  # F9 is no fact; "1.9 billion" is no multiple


def test_the_entity_is_the_subject_without_the_companys_own_name():
    """The company's own name ties no line to a segment: "Walmart International" asks for "International", and
    "Walmart" alone for nothing."""
    doc = "\n".join([*FILLER, "Walmart U.S. operating income was $ 25,158 million.",
                     "Walmart International operating income was $ 5,103 million."])
    facts = [{"id": "F1", "subject": "Walmart International", "what": "operating income", "value": 25.158,
              "unit": "USD bn", "source": "WMT-10K-FY2026"},
             {"id": "F2", "subject": "Walmart International", "what": "operating income", "value": 5.103,
              "unit": "USD bn", "source": "WMT-10K-FY2026"},
             {"id": "F3", "subject": "Walmart", "what": "operating income", "value": 25.158, "unit": "USD bn",
              "source": "WMT-10K-FY2026"}]
    registry.cut_source_excerpts(facts, {"WMT-10K-FY2026": doc}, documents.owner_names("WMT", "Walmart Inc.", None))
    assert facts[0]["excerpt_missing"] is True
    assert facts[1]["source_excerpt"] == "Walmart International operating income was $ 5,103 million."
    assert facts[2]["source_excerpt"] == "Walmart U.S. operating income was $ 25,158 million."


def test_one_end_of_a_range_is_not_the_range_and_one_year_is_not_a_tenure():
    """04A-Q22 (F537, F539, F445): guidance of 3.5-4.5% got the quarter's growth of 3.5%, and a tenure from 2019-11 to
    2026-01 got the sentence that names 2026 alone."""
    quarter = "Net sales $125.2 $120.9 $4.3 3.5%"
    guidance = "Net sales (cc) Increase 3.5% to 4.5% Unchanged Increase 4.0% to 5.0%"
    words = "net sales growth guidance"
    assert documents.cut_excerpt(f"{quarter}\n{guidance}", "3.5-4.5", words, unit="%") == guidance
    assert documents.cut_excerpt(f"{quarter}\n{guidance}", "4.0-5.0", words, unit="%") == guidance
    assert documents.cut_excerpt(quarter, "3.5-4.5", words, unit="%") is None
    assert documents.value_forms("2024-01") == ["2024-01", "2024"]  # a year and a month, not a range
    bio = "Jane Roe President, effective February 2026. From November 2019 to February 2026, she led Example U.S."
    assert documents.cut_excerpt(bio, "2019-11 to 2026-01", "Example U.S. tenure", entity="Jane Roe") == \
        "From November 2019 to February 2026, she led Example U.S."


def test_dates_references_and_parts_of_words_are_not_the_value():
    """A tariff refund ratio of 31% got "July 31, 2026" (F278); 8 independent nominees got "Proposals 5 through 8"
    (04A-Q14, F518); a negative working capital got "negatively" (04A-Q27, F666)."""
    refunds = "During the quarter ended July 31, 2026, tariff refunds lowered quarterly operating costs."
    vote = 'The Board recommends a vote "FOR" Proposals 2, 3 and 4 and "AGAINST" Proposals 5 through 8.'
    fuel = "Lower fuel prices negatively affected working capital in the quarter."
    doc = "\n".join([*FILLER, refunds, vote, fuel])
    assert documents.cut_excerpt(doc, 31, "tariff refunds ÷ quarterly operating income", unit="%") is None
    assert documents.cut_excerpt(doc, 8, "board nominees recommended", unit="count") is None
    assert documents.cut_excerpt(doc, "negative", "working capital") is None
    assert documents.cut_excerpt(doc, "lower", "working capital fuel prices") == fuel


def test_the_evaluation_script_counts_right_wrong_and_missing_excerpts(tmp_path, capsys):
    run = tmp_path / "run"
    (run / "inputs").mkdir(parents=True)
    facts = [{"id": "F1", "subject": "Example International", "what": "consolidation lag", "value": "one month",
              "source": "EX-10K-FY2026#Note1", "source_excerpt": VESTING},
             {"id": "F2", "subject": "Example", "what": "net sales", "value": 7.876, "unit": "USD bn",
              "source": "EX-10K-FY2026", "excerpt_missing": True}]
    (run / "inputs" / "fact_table.yml").write_text(yaml.safe_dump({"facts": facts}), encoding="utf-8")
    document = "\n".join([*FILLER, VESTING, LAG, "Net sales $ 7,876 $ 7,001"])
    (run / "inputs" / "sources.txt").write_text(
        "Sources.\n\n===== [src:EX-10K-FY2026] 10-K | accession 1 | filed 2026-03-13 | ex.htm =====\nurl: https://x\n"
        f"\n{document}\n", encoding="utf-8")
    (run / "manifest.yml").write_text(yaml.safe_dump({"variables": {"ticker": "EX", "company": "Example"}}),
                                      encoding="utf-8")
    labels = tmp_path / "labels.yml"
    labels.write_text(yaml.safe_dump({"facts": {"F1": {"group": "named", "right": [LAG], "wrong": [VESTING]},
                                                "F2": {"group": "named", "right": ["Net sales … 7,876"]}}}),
                      encoding="utf-8")
    assert excerpt_eval.main([str(run), "--labels", str(labels), "--show", "right"]) == 0
    out = capsys.readouterr().out
    recorded, now = out.split("\nrecorded:\n")[1].split("\nnow:\n")
    assert recorded.splitlines()[1].split() == ["named", "2", "0", "1", "1", "0", "0"]
    assert now.splitlines()[1].split() == ["named", "2", "2", "0", "0", "0", "0"]
    assert "F1 [named] right: Example International | consolidation lag" in now and LAG in now
