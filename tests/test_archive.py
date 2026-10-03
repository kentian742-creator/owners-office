"""A new archive from the SEC filings (pipeline/archive.py, the intake list and steps 01A/01B; decisions/0028)."""

from __future__ import annotations

import datetime as dt

import pytest
import yaml

from pipeline import archive, documents, edgar, registry

from . import runner_fixtures as fx

CAL = edgar.FiscalCalendar.parse("12-31")


def filing(accession: str, form: str, filed: str, report: str | None = None, items: tuple[str, ...] = ()) -> edgar.Filing:
    return edgar.Filing(cik="0000000042", accession=accession, form=form, filing_date=dt.date.fromisoformat(filed),
                        report_date=dt.date.fromisoformat(report) if report else None, items=items,
                        primary_document=f"{accession}.htm")


def event(release: edgar.Filing, period: str, end: str) -> edgar.EarningsEvent:
    return edgar.EarningsEvent(cik="0000000042", period=period, period_end=dt.date.fromisoformat(end), filing=release,
                               release_date=release.filing_date, release_basis="acceptance")


FILINGS = [
    filing("a-10k-2015", "10-K", "2016-02-25", "2015-12-31"),
    filing("a-10k-2020", "10-K", "2021-02-23", "2020-12-31"),
    filing("a-10k-2025", "10-K", "2026-02-24", "2025-12-31"),
    filing("a-8k-q3", "8-K", "2025-11-05", items=("2.02", "9.01")),
    filing("a-8k-q4", "8-K", "2026-02-11", items=("2.02", "9.01")),
    filing("a-8k-q1", "8-K", "2026-05-07", items=("2.02", "9.01")),
    filing("a-10q-q1", "10-Q", "2026-05-07", "2026-03-31"),
    filing("a-proxy", "DEF 14A", "2026-04-07"),
    filing("a-8k-q2", "8-K", "2026-08-04", items=("2.02", "9.01")),
    filing("a-8k-ceo", "8-K", "2026-08-04", items=("5.02",)),
    filing("a-8k-old", "8-K", "2025-01-15", items=("5.02",)),
    filing("a-10q-q2", "10-Q", "2026-08-07", "2026-06-30"),
]
EVENTS = [event(FILINGS[3], "FY2025Q3", "2025-09-30"), event(FILINGS[4], "FY2025Q4", "2025-12-31"),
          event(FILINGS[5], "FY2026Q1", "2026-03-31"), event(FILINGS[8], "FY2026Q2", "2026-06-30")]


def test_a_new_dossier_reads_the_present_first_then_ten_years_of_history_then_what_repeats():
    picks = archive.build_selection(FILINGS, EVENTS, as_of=dt.date(2026, 9, 28), cal=CAL, foreign=False)
    assert [p.selection.filing.accession for p in picks] == [
        "a-10k-2025", "a-8k-q2", "a-10q-q2", "a-proxy", "a-8k-ceo",  # the present
        "a-10k-2020", "a-10k-2015",  # five and ten fiscal years back
        "a-8k-q1", "a-8k-q4", "a-8k-q3", "a-10q-q1"]  # what the present largely repeats
    by = {p.selection.filing.accession: p.selection for p in picks}
    assert by["a-10k-2025"].sections is None and by["a-10k-2020"].sections == frozenset({"business", "mdna"})
    assert by["a-8k-q2"].exhibits and not by["a-8k-q2"].primary  # a domestic release is its EX-99 exhibits
    assert "a-8k-old" not in by  # an officer change more than a year ago


def test_what_does_not_fit_the_budget_is_left_out_in_order_and_a_smaller_later_one_still_fits():
    kept, left = archive.within_budget([("a", 60), ("b", 50), ("c", 30)], lambda x: x[1], budget=100)
    assert kept == [("a", 60), ("c", 30)] and left == [("b", 50)]


def row(val, end, filed, accn, start=None, form="10-K"):
    out = {"val": val, "end": end, "filed": filed, "accn": accn, "form": form, "fp": "FY"}
    if start:
        out["start"] = start
    return out


def test_the_ten_year_summary_takes_the_latest_statement_of_each_year_and_names_other_concepts():
    facts = {"facts": {"us-gaap": {
        "Revenues": {"units": {"USD": [
            row(100, "2024-12-31", "2025-02-01", "k2024", "2024-01-01"),
            row(101, "2024-12-31", "2026-02-01", "k2025", "2024-01-01"),  # restated in the next 10-K: wins
            row(120, "2025-12-31", "2026-02-01", "k2025", "2025-01-01"),
            row(30, "2025-12-31", "2026-02-01", "k2025", "2025-10-01"),  # a quarter: not a fiscal year
        ]}},
        "DepreciationDepletionAndAmortization": {"units": {"USD": [
            row(5, "2025-12-31", "2026-02-01", "k2025", "2025-01-01")]}},
        "DepreciationAndAmortization": {"units": {"USD": [
            row(20, "2024-12-31", "2026-02-01", "k2025", "2024-01-01"),
            row(22, "2025-12-31", "2026-02-01", "k2025", "2025-01-01")]}},
        "Assets": {"units": {"USD": [row(900, "2025-12-31", "2026-02-01", "k2025")]}},
    }}}
    summary = archive.xbrl_summary(facts, cal=CAL, tags={"k2025": "X-10K-FY2025"}.get)
    revenue = summary["items"]["revenue"]
    assert revenue["values"] == {
        "FY2024": {"value": 101, "end": "2024-12-31", "accession": "k2025", "source": "X-10K-FY2025"},
        "FY2025": {"value": 120, "end": "2025-12-31", "accession": "k2025", "source": "X-10K-FY2025"}}
    dna = summary["items"]["depreciation_amortization"]
    assert dna["concept"] == "DepreciationAndAmortization"  # more years with the same latest year
    assert dna["other_concepts_latest_year"] == {"DepreciationDepletionAndAmortization": 5}
    assert summary["items"]["total_assets"]["values"]["FY2025"]["value"] == 900
    assert "net_income" in summary["missing"] and summary["fiscal_years"] == ["FY2024", "FY2025"]
    assert "other_concepts_latest_year" in archive.render_summary(summary, "X")


def test_a_filer_block_comes_from_edgar_for_a_company_without_thesis_yml():
    subs = edgar.Submissions(cik="0000320193", name="Apple Inc.", tickers=("AAPL",), fiscal_year_end="0926",
                             entity_type="operating", category=None, filings=[FILINGS[2]], pages=[], pages_loaded=[])
    filer = edgar.filer_from_submissions(subs)
    assert (filer.ticker, filer.fiscal_year_end, filer.type, filer.earnings_form, filer.annual_form) == \
        ("AAPL", "09-26", "domestic", "8-K", "10-K")


@pytest.fixture
def env(tmp_path, monkeypatch):
    env = fx.make_env(tmp_path)
    fx.write(env.public / registry.INTAKE_REL, "companies:\n  - ticker: NEWCO\n    name: Newco Corporation\n"
                                               "    cik: \"0000000042\"\n    status: holding\n    decided: \"2026-09-28\"\n")
    subs = edgar.Submissions(cik="0000000042", name="NEWCO CORP", tickers=("NEWCO",), fiscal_year_end="1231",
                             entity_type="operating", category=None, filings=FILINGS, pages=[], pages_loaded=[])
    monkeypatch.setattr(edgar, "submissions", lambda *args, **kwargs: subs)
    return env


def context(env, step: str, round_: int = 1) -> registry.RunContext:
    return registry.RunContext(step=registry.STEPS[step], company="NEWCO", period="FY2026Q2",
                               run_date=dt.date(2026, 9, 28), public_root=env.public, private_root=env.private,
                               workspace_root=env.workspace, call=None, formats={}, edgar=env.gateway, round=round_)


def test_a_company_on_the_intake_list_is_known_before_its_thesis_exists(env):
    ctx = context(env, "01A")
    assert ctx.thesis() == {"company": "NEWCO", "name": "Newco Corporation", "status": "holding", "intake": True}
    assert ctx.filer().cik == "0000000042" and ctx.filer().fiscal_year_end == "12-31"
    assert registry.variables_for(ctx)["status"] == "holding"
    fields = registry.STEPS["01B"].pipeline_fields(context(env, "01B"))
    assert fields["thesis"] == {"schema_version": "0.2", "company": "NEWCO", "name": "Newco Corporation",
                                "status": "holding", "trust_level": 1,
                                "filer": {"cik": "0000000042", "type": "domestic", "fiscal_year_end": "12-31",
                                          "earnings_form": "8-K", "annual_form": "10-K"}}
    assert fields["ledger"] == {"company": "NEWCO"}


def test_the_first_draft_is_not_given_findings_and_a_company_off_the_list_needs_its_thesis(env):
    with pytest.raises(registry.Omit, match="first draft"):
        registry.INPUTS["findings_04A"](context(env, "01A"), "findings_04A")
    with pytest.raises(registry.Omit, match="no dossier yet"):
        registry.INPUTS["existing_dossier"](context(env, "01A"), "existing_dossier")
    with pytest.raises(registry.Omit):
        registry.INPUTS["findings_04B"](context(env, "01A"), "findings_04B")
    other = registry.RunContext(step=registry.STEPS["01A"], company="NONE", period="FY2026Q2",
                                run_date=dt.date(2026, 9, 28), public_root=env.public, private_root=env.private,
                                workspace_root=env.workspace, call=None, formats={})
    with pytest.raises(registry.MissingInput, match="does not list NONE"):
        other.thesis()


def test_the_industry_modules_are_listed_with_their_summaries(env):
    fx.write(env.public / "industries" / "burgers" / "industry.yml",
             "id: burgers\nname: Burgers\nas_of: \"2026-09-01\"\nsummary: Beef in a bun.\nsource: X#p1\n")
    built = registry.INPUTS["industries"](context(env, "01A"), "industries")
    assert "id: burgers" in built.text and "summary: Beef in a bun." in built.text and "source:" not in built.text


def test_the_wordmark_input_names_the_official_logo_file_and_its_source(env):
    ctx = context(env, "01A")
    assert registry.INPUTS["wordmark"](ctx, "wordmark").empty
    fx.write(env.workspace / "inputs" / "logos" / "NEWCO.svg", "<svg xmlns='http://www.w3.org/2000/svg'/>")
    fx.write(env.workspace / "inputs" / "logos" / "NEWCO.source.txt", "https://example.com/brand (company media kit)")
    built = registry.INPUTS["wordmark"](ctx, "wordmark")
    assert "inputs/logos/NEWCO.svg" in built.text and "company media kit" in built.text and not built.empty


class StubRun:
    """A succeeded run with the given outputs, as ctx.latest_run returns it."""

    def __init__(self, folder, rel: str, outputs: dict[str, str]):
        self.rel = rel
        self.files = {}
        for name, text in outputs.items():
            self.files[name] = fx.write(folder / rel / "outputs" / f"{name}.yml", text)

    def output_file(self, name):
        return self.files.get(name)

    def read_output(self, name):
        path = self.files.get(name)
        return path.read_text(encoding="utf-8") if path else None

    def outputs_used(self, names):
        return [{"kind": "run_output", "run": self.rel, "output": n} for n in names if n in self.files]


def test_the_model_review_sees_the_proposed_version_and_its_findings_go_back_only_when_it_returns_it(env, tmp_path,
                                                                                                    monkeypatch):
    fx.write(env.private / "companies" / "NEWCO" / "valuation.yml", "doc_status: effective\n")
    runs = {"01C": StubRun(tmp_path, "runs/NEWCO/2026-09-28-01C", {"valuation_yml": "doc_status: proposed\n"}),
            "04C": StubRun(tmp_path, "runs/NEWCO/2026-09-29-04C", {"valuation_decision": "approved\n",
                                                                     "findings": "should_fix: []\n"})}
    monkeypatch.setattr(registry.RunContext, "latest_run", lambda self, step, **kw: runs.get(step))
    review = context(env, "04C")
    assert registry.STEPS["04C"].variables == {"subject": "proposed valuation version"}
    assert "doc_status: proposed" in registry.INPUTS["valuation"](review, "valuation").text
    assert "doc_status: effective" in registry.INPUTS["valuation"](context(env, "02"), "valuation").text
    with pytest.raises(registry.Omit):
        registry.INPUTS["report_valuation_section"](review, "report_valuation_section")
    with pytest.raises(registry.Omit, match="has not returned"):
        registry.INPUTS["findings_04C"](context(env, "01C"), "findings_04C")
    with pytest.raises(registry.Omit, match="builds it from the dossier"):
        registry.INPUTS["valuation"](context(env, "01C"), "valuation")
    runs["04C"] = StubRun(tmp_path, "runs/NEWCO/2026-09-30-04C", {"valuation_decision": "valuation_decision: returned\n",
                                                                   "findings": "must_fix: [double discounting]\n"})
    assert "double discounting" in registry.INPUTS["findings_04C"](context(env, "01C"), "findings_04C").text
    returned = registry.INPUTS["valuation"](context(env, "01C"), "valuation")
    assert "doc_status: proposed" in returned.text and "04C returned" in returned.text


def test_a_valuation_is_placed_as_effective_only_after_04c_approved_that_very_run(tmp_path):
    from pipeline import runner
    proposed = {"bundle": "runs/NEWCO/2026-09-28-01C", "company": "NEWCO"}
    assert runner.approving_review(tmp_path, proposed) == (None, "")

    def review(date: str, reviewed: str, decision: str) -> None:
        folder = tmp_path / "runs" / "NEWCO" / f"{date}-04C"
        fx.write_yaml(folder / "manifest.yml", {"bundle": f"runs/NEWCO/{date}-04C", "inputs": [
            {"name": "valuation", "sources": [{"kind": "run_output", "run": reviewed, "output": "valuation_yml"}]}]})
        fx.write_yaml(folder / runner.RUN_RECORD, {"status": "succeeded"})
        fx.write(folder / "outputs" / "valuation_decision.yml", decision)

    review("2026-09-29", "runs/NEWCO/2026-09-20-01C", "approved\n")  # another version
    assert runner.approving_review(tmp_path, proposed) == (None, "")
    review("2026-09-30", "runs/NEWCO/2026-09-28-01C", "valuation_decision: returned\n")
    assert runner.approving_review(tmp_path, proposed) == ("runs/NEWCO/2026-09-30-04C", "returned")
    review("2026-10-01", "runs/NEWCO/2026-09-28-01C", "approved\n")
    assert runner.approving_review(tmp_path, proposed) == ("runs/NEWCO/2026-10-01-04C", "approved")
    assert runner.mark_effective("---\ncompany: X\ndoc_status: proposed\n---\n") == \
        "---\ncompany: X\ndoc_status: effective\n---\n"
    assert runner.mark_effective('doc_status: "proposed"\nnote: proposed\n') == "doc_status: effective\nnote: proposed\n"


def test_a_cited_price_history_is_registered_privately_only(tmp_path):
    from pipeline import prices, runner
    close = prices.Close("MCD", dt.date(2026, 9, 29), 233.98, "https://api.nasdaq.com/x", tag="MCD-PRICES-2026-09-29")
    fx.write_yaml(tmp_path / "runs" / "MCD" / "2026-09-29-01C" / "manifest.yml", {"inputs": [
        {"name": "price_reference", "sources": [registry._price_source(close)]}]})
    supplied = runner.supplied_documents(tmp_path, "MCD")
    assert supplied["MCD-PRICES-2026-09-29"]["visibility"] == "private"
    assert supplied["MCD-PRICES-2026-09-29"]["kind"] == "web"


def test_each_year_end_close_comes_with_that_days_ten_year_treasury_yield(env, monkeypatch):
    from pipeline import prices
    closes = [prices.Close("NEWCO", dt.date(y, 12, 31), 100.0 + y - 2021, "u", tag="NEWCO-PRICES-2026-09-28")
              for y in range(2020, 2026)]
    monkeypatch.setattr(registry, "price_history", lambda ctx, symbol: closes)

    def ten_year(day):
        if day.year == 2021:
            raise prices.PriceError("no 10-year Treasury yield on or before 2021-12-31")
        return prices.Yield(day, 4.0 + (day.year - 2021) / 10, "t")

    monkeypatch.setattr(prices, "ten_year_yield", ten_year)
    built = registry.INPUTS["year_end_closes"](context(env, "01C"), "year_end_closes")
    rows = yaml.safe_load(built.text)["year_end_closes"]
    assert [r["date"] for r in rows] == ["2025-12-31", "2024-12-31", "2023-12-31", "2022-12-31", "2021-12-31"]
    assert rows[0]["treasury_10y"] == {"date": "2025-12-31", "ten_year_percent": 4.4, "source": "t",
                                       "tag": "UST-PARYIELD-2025-12-31"}
    assert "missing" in rows[-1]["treasury_10y"]
    assert {s["tag"] for s in built.sources if s["kind"] == "treasury"} == {
        "UST-PARYIELD-2025-12-31", "UST-PARYIELD-2024-12-31", "UST-PARYIELD-2023-12-31", "UST-PARYIELD-2022-12-31"}


def test_the_valuation_checks_its_readings_against_the_latest_reports_and_each_earlier_years_mdna():
    picks = archive.valuation_selection(FILINGS, as_of=dt.date(2026, 9, 28))
    assert [(p.selection.filing.accession, p.selection.sections) for p in picks] == [
        ("a-10k-2025", None), ("a-10q-q2", None),  # in full
        ("a-10k-2020", frozenset({"mdna"}))]  # nine years back at most: the 2015 report is ten
    assert archive.valuation_selection([], as_of=dt.date(2026, 9, 28)) == []




def test_mdna_of_a_10k_laid_out_with_a_cross_reference_index_is_found_by_its_title():
    body = "\n".join(f"Operating income fell 9% in 2022 because of the Russia exit charge, line {i}." for i in range(60))
    text = "\n".join([
        "Management's Discussion and Analysis of Financial Condition and Results of Operations",  # contents
        "Financial Statements and Supplementary Data",
        "MANAGEMENT'S DISCUSSION AND ANALYSIS OF FINANCIAL CONDITION AND RESULTS OF OPERATIONS",  # the body
        body,
        "Financial Statements and Supplementary Data",
        "Consolidated Statement of Income",
        "Revenues 23,183",
        "Item 7 Management’s Discussion and Analysis of Financial Condition and Results of Operations Pages 8-37",
        "Item 7A Quantitative and Qualitative Disclosures About Market Risk Pages 22-23",
    ])
    out, found, missing = documents.extract_sections(text, "10-K", frozenset({"mdna"}))
    assert found == ["mdna"] and missing == [] and "Russia exit charge, line 59." in out
    assert "Revenues 23,183" not in out and "Pages 8-37" not in out


def test_the_decline_years_come_first_in_the_valuations_filings():
    summary = {"items": {
        "operating_income": {"values": {"FY2019": {"value": 10}, "FY2020": {"value": 7}, "FY2021": {"value": 11}}},
        "operating_cash_flow": {"values": {"FY2021": {"value": 9}, "FY2022": {"value": 8}, "FY2024": {"value": 1}}}}}
    assert archive.decline_years(summary) == {2020, 2022}  # FY2024 follows a gap, so it is not compared
    annual = [filing(f"a-10k-{y}", "10-K", f"{y + 1}-02-20", f"{y}-12-31") for y in range(2016, 2026)]
    picks = archive.valuation_selection(annual, as_of=dt.date(2026, 9, 28), declines={2018, 2022})
    assert [p.selection.filing.accession for p in picks][:5] == [
        "a-10k-2025", "a-10k-2022", "a-10k-2018", "a-10k-2024", "a-10k-2023"]
    assert "decline year" in picks[1].reason


def test_the_revision_reads_hqs_rulings_on_the_audit_of_this_build(env, monkeypatch):
    with pytest.raises(registry.Omit, match="first draft"):
        registry.INPUTS["hq_rulings"](context(env, "01A"), "hq_rulings")
    with pytest.raises(registry.Omit, match="has not ruled"):
        registry.INPUTS["hq_rulings"](context(env, "01A", 2), "hq_rulings")
    fx.write(env.private / "runs" / "hq" / "2026-09-01-17A-NEWCO" / "rulings.md", "old build\n")
    fx.write(env.private / "runs" / "hq" / "2026-09-29-17A-NEWCO" / "rulings.md", "R1 continuing basis\n")
    draft = type("Draft", (), {"run_date": dt.date(2026, 9, 28)})()
    monkeypatch.setattr(registry.RunContext, "latest_run", lambda self, step, **kw: draft if step == "01A" else None)
    built = registry.INPUTS["hq_rulings"](context(env, "01A", 2), "hq_rulings")
    assert "R1 continuing basis" in built.text and "old build" not in built.text


def test_an_excerpt_comes_from_the_row_that_shares_the_most_words_with_the_fact():
    doc = "\n".join(["Revenue from external customers 1,392 1,301", "Indices segment revenue 1,392 1,201",
                     "Indices segment operating profit 965 880"])
    out = documents.cut_excerpt(doc, 1392, words="Indices segment revenue FY2023")
    assert out is not None and out.startswith("Indices segment revenue")


def test_a_private_file_citing_a_tag_only_the_public_sources_register_gets_it_mirrored(tmp_path):
    from pipeline import runner
    roots = runner.Roots(public=tmp_path / "pub", private=tmp_path / "priv", workspace=tmp_path)
    fx.write_yaml(roots.public / "companies" / "NEWCO" / "sources.yml", {"sources": [
        {"tag": "NEWCO-CALL-FY2026Q2", "kind": "transcript", "title": "Q2 call", "primary": False}]})
    fx.write_yaml(roots.private / "companies" / "NEWCO" / "sources.yml", {"sources": [
        {"tag": "NEWCO-RPT1-2026-09-01", "kind": "report", "title": "the owner's report", "primary": False}]})
    dossier = runner.PlannedWrite("dossier", registry.PRIVATE_REPO, "private", "companies/NEWCO/dossier.md",
                                  b"Guidance was raised [src:NEWCO-CALL-FY2026Q2].\n")
    writes, warnings = [dossier], []
    runner.complete_sources(writes, {"company": "NEWCO"}, roots, warnings)
    mirrored = [w for w in writes if w.repo == registry.PRIVATE_REPO and w.path.endswith("sources.yml")]
    assert mirrored and "NEWCO-CALL-FY2026Q2" in mirrored[0].text
    assert not [w for w in writes if w.repo == registry.PUBLIC_REPO]


def test_hq_tag_repairs_may_add_source_tags_and_nothing_else():
    from pipeline import runner
    text = "Central value $413.5. The buy range is $413.5 × 65%–75%.\n"
    fixed, problems = runner.apply_tag_repairs(text, [
        {"find": "× 65%–75%.", "replace": "× 65%–75% [src:X-VAL-2026-09-30]."}])
    assert problems == [] and fixed.endswith("× 65%–75% [src:X-VAL-2026-09-30].\n")
    _, problems = runner.apply_tag_repairs(text, [{"find": "× 65%–75%.", "replace": "× 60%–75% [src:X]."}])
    assert "more than [src:] tags" in problems[0]
    _, problems = runner.apply_tag_repairs(text, [{"find": "$413.5", "replace": "$413.5 [src:X]"}])
    assert "2 times" in problems[0]


def test_an_excerpt_is_looked_for_first_on_the_cited_page_or_note():
    doc = "\n".join(["Revenue 1,392 in another table.", "K-69", "Indices revenue was 1,392 this year.", "K-70",
                     "Note 9. Leases", "Rent 1,392 under leases.", "Note 10. Debt", "Debt of 1,392 matures in 2030.",
                     "Note 11. Equity", "Equity text."])
    assert documents.locator_section(doc, "pK-70").strip() == "Indices revenue was 1,392 this year."
    assert "Debt of 1,392" in documents.locator_section(doc, "Note10") and "Rent" not in documents.locator_section(doc, "Note10")
    assert documents.locator_section(doc, "p999") is None


def test_a_valuations_working_is_registered_privately_by_its_tag():
    from pipeline import runner
    entries = runner.working_entries({"company": "NEWCO", "run_date": "2026-09-30", "step": "01C", "scope": "NEWCO",
                                      "context": {"run_dir": "2026-09-30-01C-rerun2"}})
    entry = entries["NEWCO-VAL-2026-09-30"]
    assert entry["location"] == "private:runs/NEWCO/2026-09-30-01C-rerun2/valuation_md.md"
    assert entry["visibility"] == "private" and runner.working_entries({"company": "X", "step": "15A"}) == {}
