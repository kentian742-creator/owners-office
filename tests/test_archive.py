"""A new archive from the SEC filings (pipeline/archive.py, the intake list and steps 01A/01B; decisions/0028)."""

from __future__ import annotations

import datetime as dt

import pytest

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
    runs["04C"] = StubRun(tmp_path, "runs/NEWCO/2026-09-30-04C", {"valuation_decision": "valuation_decision: returned\n",
                                                                   "findings": "must_fix: [double discounting]\n"})
    assert "double discounting" in registry.INPUTS["findings_04C"](context(env, "01C"), "findings_04C").text


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
