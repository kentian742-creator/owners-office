"""A new archive from the SEC filings (pipeline/archive.py, the intake list and steps 01A/01B; decisions/0028)."""

from __future__ import annotations

import datetime as dt
import io
import json
from pathlib import Path

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


def context(env, step: str, round_: int = 1, period: str = "FY2026Q2",
            run_date: dt.date = dt.date(2026, 9, 28)) -> registry.RunContext:
    return registry.RunContext(step=registry.STEPS[step], company="NEWCO", period=period,
                               run_date=run_date, public_root=env.public, private_root=env.private,
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


def succeeded_run(env, rel: str, manifest: dict) -> Path:
    """A succeeded runner bundle under the private runs/, as ctx.runs() indexes it."""
    folder = env.private / rel
    fx.write_yaml(folder / registry.MANIFEST_NAME, {"bundle": rel, **manifest})
    fx.write_yaml(folder / registry.RUN_RECORD_NAME, {"status": "succeeded"})
    return folder


def test_02_reads_the_effective_valuations_working_as_placed_not_as_the_model_wrote_it(env):
    """Placement marks the working effective and applies HQ's tag repairs to its copy; outputs/ keeps the raw text."""
    fx.write(env.private / "companies" / "NEWCO" / "valuation.yml", "company: NEWCO\ndoc_status: effective\n")
    run = succeeded_run(env, "runs/NEWCO/2026-09-30-01C-rerun2",
                        {"step": "01C", "company": "NEWCO", "period": "FY2026Q2", "rerun": 2})
    fx.write(run / "outputs" / "valuation_yml.yml", "company: NEWCO\ndoc_status: proposed\n")
    fx.write(run / "outputs" / "valuation_md.md", "---\ndoc_status: proposed\n---\nCentral value $10.\n")
    placed = fx.write(run / "valuation_md.md",
                      "---\ndoc_status: effective\n---\nCentral value $10 [src:NEWCO-VAL-2026-09-30].\n")
    built = registry.INPUTS["valuation"](context(env, "02"), "valuation")
    assert "doc_status: proposed" not in built.text and "$10 [src:NEWCO-VAL-2026-09-30]." in built.text
    assert built.sources[-1]["kind"] == "repo_file"
    assert built.sources[-1]["path"] == "runs/NEWCO/2026-09-30-01C-rerun2/valuation_md.md"
    placed.unlink()  # no placed copy: the run's own output
    built = registry.INPUTS["valuation"](context(env, "02"), "valuation")
    assert "Central value $10.\n" in built.text and built.sources[-1]["kind"] == "run_output"


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


def test_the_latest_review_decides_even_after_nine_same_day_reruns(tmp_path):
    """As text, -rerun10 sorts before -rerun9; reviews are ranked by their rerun number."""
    from pipeline import runner
    proposed = {"bundle": "runs/NEWCO/2026-09-30-01C", "company": "NEWCO"}
    for rerun, decision in ((9, "approved"), (10, "returned")):
        folder = tmp_path / "runs" / "NEWCO" / f"2026-09-30-04C-rerun{rerun}"
        fx.write_yaml(folder / "manifest.yml", {"bundle": f"runs/NEWCO/{folder.name}", "step": "04C", "rerun": rerun,
                                                "inputs": [{"name": "valuation", "sources": [
                                                    {"kind": "run_output", "run": proposed["bundle"]}]}]})
        fx.write_yaml(folder / runner.RUN_RECORD, {"status": "succeeded"})
        fx.write(folder / "outputs" / "valuation_decision.yml", f"valuation_decision: {decision}\n")
    assert runner.approving_review(tmp_path, proposed) == ("runs/NEWCO/2026-09-30-04C-rerun10", "returned")


def test_marking_a_valuation_effective_changes_its_doc_status_line_and_nothing_else():
    """A blank line after it stays, or 02 no longer finds the run that produced the effective version."""
    from pipeline import runner
    assert runner.mark_effective("company: X\ndoc_status: proposed\n\ncurrency: USD\n") == \
        "company: X\ndoc_status: effective\n\ncurrency: USD\n"
    assert runner.mark_effective("doc_status: proposed\r\n\r\ncurrency: USD\r\n") == \
        "doc_status: effective\r\n\r\ncurrency: USD\r\n"


def valuation_run(env, run_dir: str, center: int, *, status: str = "doc_status: proposed") -> Path:
    """A succeeded 01C bundle of NEWCO proposing `center` and citing its working, and a 04C run that approved it."""
    from pipeline import runner
    date, rerun = run_dir[:10], int(run_dir.partition("-rerun")[2] or 1)
    numbered = {"rerun": rerun} if rerun > 1 else {}
    bundle = env.private / "runs" / "NEWCO" / run_dir
    text = (f"company: NEWCO\nas_of: '{date}'\n{status}\n\ncenter: {center}\n"
            f"note: The center is {center} [src:NEWCO-VAL-{date}].\n")
    fx.write(bundle / "outputs" / "valuation_yml.yml", text)
    fx.write_yaml(bundle / runner.MANIFEST, {
        "manifest_version": runner.MANIFEST_VERSION, "bundle": f"runs/NEWCO/{run_dir}", "step": "01C",
        "company": "NEWCO", "scope": "NEWCO", "period": "FY2026Q2", "run_date": date, "prompt": {"id": "01"},
        **numbered, "context": {"company": "NEWCO", "run_date": date, "run_dir": run_dir}})
    fx.write_yaml(bundle / runner.RUN_RECORD, {"status": "succeeded", "outputs": {"valuation_yml": {
        "status": "written", "file": "outputs/valuation_yml.yml", "format": "yaml",
        "sha256": registry.sha256_text(text)}}})
    review = env.private / "runs" / "NEWCO" / run_dir.replace("-01C", "-04C")
    fx.write_yaml(review / runner.MANIFEST, {"bundle": f"runs/NEWCO/{review.name}", "step": "04C", "company": "NEWCO",
                                             **numbered, "inputs": [
        {"name": "valuation", "sources": [{"kind": "run_output", "run": f"runs/NEWCO/{run_dir}"}]}]})
    fx.write_yaml(review / runner.RUN_RECORD, {"status": "succeeded"})
    fx.write(review / "outputs" / "valuation_decision.yml", "valuation_decision: approved\n")
    return bundle


def place_valuation(env, bundle: Path) -> dict:
    from pipeline import runner
    return runner.place(bundle, roots=env.roots, allow_fake=True, lint=False, out=io.StringIO(),
                        now=dt.datetime(2026, 10, 2, tzinfo=dt.timezone.utc))


def effective_valuation(env) -> dict:
    return yaml.safe_load((env.private / "companies" / "NEWCO" / "valuation.yml").read_text(encoding="utf-8"))


def test_an_earlier_approved_valuation_never_replaces_a_later_one(env):
    from pipeline import runner
    earlier, later = valuation_run(env, "2026-09-29-01C", 100), valuation_run(env, "2026-09-30-01C", 200)
    place_valuation(env, later)
    with pytest.raises(runner.RunnerError, match=r"a later valuation \(runs/NEWCO/2026-09-30-01C\)"):
        place_valuation(env, earlier)  # a retry, say, after the earlier placement failed
    assert effective_valuation(env)["center"] == 200 and effective_valuation(env)["doc_status"] == "effective"


@pytest.mark.parametrize("review_dir, period, reviewed", [
    # the routine §V20 review of a stale valuation: the placed version, filed under the run that produced it
    ("2026-11-10-04C", "FY2026Q3", [
        {"kind": "repo_file", "repo": registry.PRIVATE_REPO, "path": "companies/NEWCO/valuation.yml"},
        {"kind": "run_output", "run": "runs/NEWCO/2026-09-30-01C", "output": "valuation_md"}]),
    # a 04C rerun that returns the later bundle itself
    ("2026-09-30-04C-rerun2", "FY2026Q2", [{"kind": "run_output", "run": "runs/NEWCO/2026-09-30-01C"}]),
])
def test_an_older_valuation_never_replaces_the_one_in_force_whatever_04c_said_of_it_since(env, review_dir, period,
                                                                                          reviewed):
    from pipeline import runner
    earlier, later = valuation_run(env, "2026-09-29-01C", 100), valuation_run(env, "2026-09-30-01C", 200)
    place_valuation(env, later)
    review = env.private / "runs" / "NEWCO" / review_dir
    fx.write_yaml(review / runner.MANIFEST, {"bundle": f"runs/NEWCO/{review_dir}", "step": "04C", "company": "NEWCO",
                                             "period": period, "rerun": int(review_dir.partition("-rerun")[2] or 1),
                                             "inputs": [{"name": "valuation", "sources": reviewed}]})
    fx.write_yaml(review / runner.RUN_RECORD, {"status": "succeeded"})
    fx.write(review / "outputs" / "valuation_decision.yml", "valuation_decision: returned\n")
    assert runner.approving_review(env.private, {"bundle": "runs/NEWCO/2026-09-30-01C", "company": "NEWCO"}) == \
        (f"runs/NEWCO/{review_dir}", "returned")
    with pytest.raises(runner.RunnerError, match=r"the effective valuation came from a later run "
                                                 r"\(runs/NEWCO/2026-09-30-01C\)"):
        place_valuation(env, earlier)
    assert effective_valuation(env)["center"] == 200 and effective_valuation(env)["as_of"] == "2026-09-30"


def test_a_same_day_rerun_that_replaces_the_valuation_takes_its_working_tag_with_it(env):
    place_valuation(env, valuation_run(env, "2026-09-30-01C", 100))
    report = place_valuation(env, valuation_run(env, "2026-09-30-01C-rerun2", 200))
    sources = yaml.safe_load((env.private / "companies" / "NEWCO" / "sources.yml").read_text(encoding="utf-8"))
    (working,) = [e for e in sources["sources"] if e["tag"] == "NEWCO-VAL-2026-09-30"]
    assert working["location"] == "private:runs/NEWCO/2026-09-30-01C-rerun2/valuation_md.md"
    assert effective_valuation(env)["center"] == 200 and any("NEWCO-VAL-2026-09-30 now names" in w
                                                             for w in report["warnings"])


def test_an_approved_valuation_is_placed_only_once_it_says_effective(env):
    from pipeline import runner
    place_valuation(env, valuation_run(env, "2026-09-29-01C", 100,
                                       status="doc_status: proposed  # takes effect when 04C approves"))
    assert effective_valuation(env)["doc_status"] == "effective"
    with pytest.raises(runner.RunnerError, match="no 'doc_status: proposed' line"):
        place_valuation(env, valuation_run(env, "2026-09-30-01C", 200, status="doc_status : proposed"))
    assert effective_valuation(env)["center"] == 100


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


LENNAR_13F = b"""<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable><nameOfIssuer>LENNAR CORP</nameOfIssuer><titleOfClass>CL A</titleOfClass><cusip>526057104</cusip>
    <value>10000</value><shrsOrPrnAmt><sshPrnamt>100</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
  </infoTable>
  <infoTable><nameOfIssuer>LENNAR CORP</nameOfIssuer><titleOfClass>CL B</titleOfClass><cusip>526057302</cusip>
    <value>900</value><shrsOrPrnAmt><sshPrnamt>10</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
  </infoTable>
</informationTable>"""


def test_the_holdings_marks_tag_an_unregistered_13f_by_its_filing_date_and_each_share_class_by_its_own_history(
        env, monkeypatch):
    report = filing("a-13f-q2", "13F-HR", "2026-08-14", "2026-06-30")
    subs = edgar.Submissions(cik="0000000042", name="NEWCO CORP", tickers=("NEWCO",), fiscal_year_end="1231",
                             entity_type="operating", category=None, filings=[*FILINGS, report], pages=[],
                             pages_loaded=[])
    monkeypatch.setattr(edgar, "submissions", lambda *args, **kwargs: subs)
    index = {"directory": {"item": [{"name": "primary_doc.xml"}, {"name": "infotable.xml"}]}}
    bodies = {f"{report.folder_url}/index.json": json.dumps(index).encode(),
              f"{report.folder_url}/infotable.xml": LENNAR_13F}
    monkeypatch.setattr(env.gateway.client, "get_bytes", lambda url, **kwargs: bodies[url])
    fx.write(env.workspace / registry.FIGI_CACHE, json.dumps({"526057104": "LEN", "526057302": "LEN.B"}))
    for symbol, (june, september) in {"LEN": ("100.00", "110.00"), "LEN.B": ("90.00", "99.00")}.items():
        rows = [{"date": "09/28/2026", "close": september}, {"date": "06/30/2026", "close": june}]
        fx.write(env.workspace / "inputs" / "prices" / f"{symbol}-2026-09-28.json",
                 json.dumps({"data": {"tradesTable": {"rows": rows}}}))
    built = registry.INPUTS["holdings_marks"](context(env, "01C"), "holdings_marks")
    data = yaml.safe_load(built.text)
    filed, *closes = built.sources
    assert data["source_13f"]["tag"] == filed["tag"] == "NEWCO-13FHR-2026-08-14"  # SPEC 3.3: EDGAR's filing date
    assert filed["document"] == "infotable.xml"
    assert [c["tag"] for c in closes] == ["LEN-PRICES-2026-09-28", "LEN.B-PRICES-2026-09-28"]
    assert [p["price_tag"] for p in data["positions"]] == ["LEN-PRICES-2026-09-28#2026-09-28",
                                                         "LEN.B-PRICES-2026-09-28#2026-09-28"]
    assert (data["marked_positions_value"], data["coverage"]) == (11990, 1.0)


def test_each_backtest_year_end_marks_the_last_13f_filed_by_then_at_that_year_ends_closes(env, monkeypatch):
    """§V5/V12 start a backtest year-end from what was public by then (HQ ruling R19): the Q3 13F filed in November,
    marked at the December close; a year-end before any 13F says so."""
    reports = [filing("a-13f-2024q3", "13F-HR", "2024-11-14", "2024-09-30"),
               filing("a-13f-2025q3", "13F-HR", "2025-11-14", "2025-09-30"),
               filing("a-13f-2026q2", "13F-HR", "2026-08-14", "2026-06-30")]
    subs = edgar.Submissions(cik="0000000042", name="NEWCO CORP", tickers=("NEWCO",), fiscal_year_end="1231",
                             entity_type="operating", category=None, filings=[*FILINGS, *reports], pages=[],
                             pages_loaded=[])
    monkeypatch.setattr(edgar, "submissions", lambda *args, **kwargs: subs)
    index = {"directory": {"item": [{"name": "infotable.xml"}]}}
    bodies = {}
    for report in reports:
        bodies[f"{report.folder_url}/index.json"] = json.dumps(index).encode()
        bodies[f"{report.folder_url}/infotable.xml"] = LENNAR_13F
    monkeypatch.setattr(env.gateway.client, "get_bytes", lambda url, **kwargs: bodies[url])
    fx.write(env.workspace / registry.FIGI_CACHE, json.dumps({"526057104": "LEN", "526057302": "LEN.B"}))
    for symbol in ("LEN", "LEN.B", "NEWCO"):
        rows = [{"date": f"{m}/{d}/{y}", "close": str(100 + y - 2020 + (m == 12))}
                for y in range(2020, 2027) for m, d in ((12, 31), (9, 30), (6, 30)) if (y, m) < (2026, 9)]
        rows.append({"date": "09/28/2026", "close": "107"})
        fx.write(env.workspace / "inputs" / "prices" / f"{symbol}-2026-09-28.json",
                 json.dumps({"data": {"tradesTable": {"rows": rows}}}))
    built = registry.INPUTS["backtest_holdings_marks"](context(env, "01C"), "backtest_holdings_marks")
    blocks = {b["year_end"]: b for b in yaml.safe_load(built.text)["year_ends"]}
    assert list(blocks) == ["2025-12-31", "2024-12-31", "2023-12-31", "2022-12-31", "2021-12-31"]
    assert blocks["2025-12-31"]["source_13f"]["report_date"] == "2025-09-30"  # filed 2025-11-14, before the year-end
    assert blocks["2024-12-31"]["source_13f"]["report_date"] == "2024-09-30"
    assert blocks["2023-12-31"] == {"year_end": "2023-12-31", "not_marked": "no 13F-HR was filed by this date"}
    lennar = blocks["2025-12-31"]["positions"][0]
    assert lennar["close_2025-09-30"] == 105 and lennar["close_2025-12-31"] == 106  # the year-end's own close
    assert lennar["value_2025-12-31"] == round(10000 * 106 / 105) and lennar["price_tag"] == "LEN-PRICES-2026-09-28#2025-12-31"
    tags = [s.get("tag") for s in built.sources]
    assert len(tags) == len(set(tags))  # one record per 13F and per price history
    assert built.note.startswith("2025-12-31: a-13f-2025q3, coverage 100.0%")


def test_the_valuation_checks_its_readings_against_the_latest_reports_and_each_earlier_years_mdna():
    picks = archive.valuation_selection(FILINGS, as_of=dt.date(2026, 9, 28), cal=CAL)
    assert [(p.selection.filing.accession, p.selection.sections) for p in picks] == [
        ("a-10k-2025", None), ("a-10q-q2", None),  # in full
        ("a-10k-2020", frozenset({"mdna"}))]  # nine years back at most: the 2015 report is ten
    assert archive.valuation_selection([], as_of=dt.date(2026, 9, 28), cal=CAL) == []




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
    picks = archive.valuation_selection(annual, as_of=dt.date(2026, 9, 28), cal=CAL, declines={2018, 2022})
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


def test_a_valuation_in_a_later_period_reads_the_rulings_on_the_latest_build_only(env):
    """01C may value the placed dossier, and 04C review it, in a period with no 01A run of its own."""
    fx.write(env.private / "runs" / "hq" / "2026-09-01-17A-NEWCO" / "rulings.md", "old build\n")
    fx.write(env.private / "runs" / "hq" / "2026-09-29-17A-NEWCO" / "rulings.md", "R1 continuing basis\n")
    succeeded_run(env, "runs/NEWCO/2026-09-28-01A", {"step": "01A", "company": "NEWCO", "period": "FY2026Q2"})
    for step in ("01C", "04C"):
        built = registry.INPUTS["hq_rulings"](context(env, step, period="FY2026Q3"), "hq_rulings")
        assert "R1 continuing basis" in built.text and "old build" not in built.text


def test_a_backdated_valuation_keeps_the_rulings_on_its_build_when_a_rebuild_began_after_its_run_date(env):
    """A rerun of 01C and 04C dated in the build period values the dossier HQ ruled on, not a later, unruled draft."""
    fx.write(env.private / "runs" / "hq" / "2026-09-29-17A-NEWCO" / "rulings.md", "R1 continuing basis\n")
    succeeded_run(env, "runs/NEWCO/2026-09-28-01A", {"step": "01A", "company": "NEWCO", "period": "FY2026Q2"})
    succeeded_run(env, "runs/NEWCO/2026-11-05-01A", {"step": "01A", "company": "NEWCO", "period": "FY2026Q3"})
    for step in ("01C", "04C"):
        built = registry.INPUTS["hq_rulings"](context(env, step, run_date=dt.date(2026, 9, 30)), "hq_rulings")
        assert "R1 continuing basis" in built.text
        assert [s["path"] for s in built.sources] == ["runs/hq/2026-09-29-17A-NEWCO/rulings.md"]


def test_a_valuation_reads_the_rulings_on_its_own_periods_build_and_not_on_a_later_one(env):
    """01C and 04C of FY2026Q2 value the dossier of FY2026Q2's build: dated before its first draft (MCD's 01C of
    2026-09-29 valued the build begun 2026-09-30), dated after a later rebuild began, or after HQ ruled on that rebuild."""
    fx.write(env.private / "runs" / "hq" / "2026-09-01-17A-NEWCO" / "rulings.md", "old build\n")
    fx.write(env.private / "runs" / "hq" / "2026-09-29-17A-NEWCO" / "rulings.md", "R1 continuing basis\n")
    succeeded_run(env, "runs/NEWCO/2026-09-28-01A", {"step": "01A", "company": "NEWCO", "period": "FY2026Q2"})
    succeeded_run(env, "runs/NEWCO/2026-11-05-01A", {"step": "01A", "company": "NEWCO", "period": "FY2026Q3"})
    fx.write(env.private / "runs" / "hq" / "2026-11-07-17A-NEWCO" / "rulings.md", "R1 on the rebuild\n")
    for step in ("01C", "04C"):
        for run_date in (dt.date(2026, 9, 27), dt.date(2026, 9, 30), dt.date(2026, 11, 20)):
            built = registry.INPUTS["hq_rulings"](context(env, step, run_date=run_date), "hq_rulings")
            assert [s["path"] for s in built.sources] == ["runs/hq/2026-09-29-17A-NEWCO/rulings.md"], (step, run_date)
        later = registry.INPUTS["hq_rulings"](context(env, step, period="FY2026Q3", run_date=dt.date(2026, 11, 20)),
                                              "hq_rulings")
        assert [s["path"] for s in later.sources] == ["runs/hq/2026-11-07-17A-NEWCO/rulings.md"]


def test_the_rulings_on_a_company_whose_ticker_starts_with_this_one_are_not_this_companys(env):
    succeeded_run(env, "runs/NEWCO/2026-09-28-01A", {"step": "01A", "company": "NEWCO", "period": "FY2026Q2"})
    fx.write(env.private / "runs" / "hq" / "2026-09-29-17A-NEWCOX" / "rulings.md", "another company\n")
    with pytest.raises(registry.Omit, match="has not ruled"):
        registry.INPUTS["hq_rulings"](context(env, "01A", 2), "hq_rulings")
    fx.write(env.private / "runs" / "hq" / "2026-09-30-17A-NEWCO-r2-rerun2" / "rulings.md", "R2 second round\n")
    built = registry.INPUTS["hq_rulings"](context(env, "01A", 2), "hq_rulings")
    assert "R2 second round" in built.text and "another company" not in built.text


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


FILING_FACT = "Revenue was $15,336m [src:X-10K-FY2025#p78] (Note 3).\n"


def test_a_tag_repair_never_lands_inside_another_source_tag():
    from pipeline import runner
    fixed, problems = runner.apply_tag_repairs(FILING_FACT, [{"find": "FY2025", "replace": "FY2025 [src:X-VAL-1]"}])
    assert fixed == FILING_FACT and "more than [src:] tags" in problems[0]


def test_a_tag_repair_never_removes_or_swaps_a_source_tag():
    from pipeline import runner
    for find, replace in ((" [src:X-10K-FY2025#p78] (", " ("), ("[src:X-10K-FY2025#p78]", "[src:X-VAL-1]"),
                          ("$15,336m [src:X-10K-FY2025#p78]", "$15,336m")):
        fixed, problems = runner.apply_tag_repairs(FILING_FACT, [{"find": find, "replace": replace}])
        assert fixed == FILING_FACT and "more than [src:] tags" in problems[0], replace
    fixed, problems = runner.apply_tag_repairs(FILING_FACT, [
        {"find": "#p78] (Note 3)", "replace": "#p78] [src:X-VAL-1] (Note 3 [src:X-10K-FY2025#Note3])"}])
    assert problems == [] and "[src:X-10K-FY2025#p78] [src:X-VAL-1] (Note 3 [src:X-10K-FY2025#Note3])." in fixed


def test_a_tag_repair_adds_a_tag_after_a_figure_never_inside_it_nor_with_other_whitespace():
    from pipeline import runner
    text = "Margins were 41%. Totals ($1,795m, $3,964m) give ±10% with 8.5 points.\n"
    for find, replace in (("$1,795m", "$1 [src:X],795m"), ("8.5 points", "8 [src:X].5 points"),
                          ("41%.", "41%\n\n\n[src:X]."), ("41%", "41%  [src:X]")):
        fixed, problems = runner.apply_tag_repairs(text, [{"find": find, "replace": replace}])
        assert fixed == text and "more than [src:] tags" in problems[0], replace
    fixed, problems = runner.apply_tag_repairs(text, [
        {"find": "$3,964m)", "replace": "$3,964m [src:X-8K-1#EX99.1])"},
        {"find": "±10% with", "replace": "±10% [src:X-VAL-1] with"}])
    assert problems == [] and "$3,964m [src:X-8K-1#EX99.1]) give ±10% [src:X-VAL-1] with" in fixed


def test_an_excerpt_is_looked_for_first_on_the_cited_page_or_note():
    doc = "\n".join(["Revenue 1,392 in another table.", "K-69", "Indices revenue was 1,392 this year.", "K-70",
                     "Note 9. Leases", "Rent 1,392 under leases.", "Note 10. Debt", "Debt of 1,392 matures in 2030.",
                     "Note 11. Equity", "Equity text."])
    assert documents.locator_section(doc, "pK-70").strip() == "Indices revenue was 1,392 this year."
    assert "Debt of 1,392" in documents.locator_section(doc, "Note10") and "Rent" not in documents.locator_section(doc, "Note10")
    assert documents.locator_section(doc, "p999") is None


def test_a_page_runs_from_the_previous_pages_footer_past_year_subheadings_and_one_number_cells():
    """S&P Global's 10-K, page 50: a "2025" subheading and paragraph, then a "2024" subheading and a paragraph that
    begins the same way."""
    doc = "\n".join([
        "Market Intelligence", "49",
        "Revenue by type", "Subscription", "4,423", "423",
        "2025", "Revenue increased 6% on subscription growth during the year ended December 31, 2025.",
        "2024", "Revenue increased 6% on subscription growth during the year ended December 31, 2024.",
        "50", "Ratings"])
    page = documents.locator_section(doc, "p50")
    assert page.startswith("Revenue by type") and page.endswith("December 31, 2024.")
    excerpt = documents.cut_excerpt(page, 6, words="Market Intelligence revenue increased 6% in 2025")
    assert excerpt.endswith("December 31, 2025.")
    assert documents.locator_section(doc, "p49") == "Market Intelligence"  # no page 48 footer: from the start


def test_a_note_is_found_by_its_heading_not_by_a_footnote_or_the_index_of_notes():
    """S&P Global's 10-K: footnotes ("1 Includes ...") and the index of notes ("2 Acquisitions and Divestitures") also
    begin with a note's number, and so does the list of exhibits after the notes ("2.Financial Schedule")."""
    doc = "\n".join([
        "Total — Quarter 4,700,236 $ 514.16",
        "1 Includes 0.6 million shares received from the conclusion of our ASR agreement.",
        "Item 7. Management's Discussion and Analysis",
        *[f"Results of operations, line {i}." for i in range(20)],
        "Intersegment eliminations 6 … (200) — (186) — 8% N/M",
        "Notes to the Consolidated Financial Statements", "83",
        "1 Accounting Policies", "83", "2 Acquisitions and Divestitures", "91",
        "3 Goodwill and Other Intangible Assets", "95",
        "1. Accounting Policies",
        "Non-transaction revenue also includes an intersegment revenue elimination of $ 200 million, $ 186 million "
        "and $ 177 million.",
        *[f"Accounting policies, line {i}." for i in range(5)],
        "2. Acquisitions and Divestitures", "We acquired With Intelligence.",
        "3. Goodwill and Other Intangible Assets", "Goodwill text.",
        "2 As of December 31, 2025, $ 4,914 million relates to our redeemable noncontrolling interest.",
        "Item 15. Exhibits, Financial Statement Schedules", "1.Financial Statements", "•Consolidated Balance Sheets",
        "2.Financial Schedule", *[f"Schedule II, line {i}." for i in range(20)]])
    note1 = documents.locator_section(doc, "Note1")
    assert note1.startswith("1. Accounting Policies") and note1.endswith("Accounting policies, line 4.")
    excerpt = documents.cut_excerpt(note1, 186, words="Ratings intersegment revenue elimination")
    assert excerpt.startswith("Non-transaction revenue also includes an intersegment revenue elimination")
    assert documents.locator_section(doc, "Note2") == "2. Acquisitions and Divestitures\nWe acquired With Intelligence."


def test_a_note_headed_on_a_line_of_its_own_is_found_and_the_rows_of_the_index_of_notes_are_not():
    """American Express's 10-K: the index of notes has a row per note ("Note 3 – Reserves for Credit Losses") with its
    page number on the next line, each note is headed "NOTE 3" on a line of its own with its title on the next, and
    the table of accounting policies in Note 1 refers to "Note 10" on a line of its own too."""
    titles = ["Summary of Significant Accounting Policies", "Loans and Card Member Receivables",
              "Reserves for Credit Losses", "Investment Securities", "Asset Securitizations", "Other Assets",
              "Customer Deposits", "Debt", "Other Liabilities", "Stock-Based Compensation", "Retirement Plans"]
    index = ["NOTES TO CONSOLIDATED FINANCIAL STATEMENTS", "97"]
    for n, title in enumerate(titles, 1):
        index += [f"Note {n} – {title}", str(94 + 3 * n)]
    bodies = {1: ["Significant Accounting Policy Note", "Number Note Title",
                  "Reserves for Credit Losses Note 3 Reserves for Credit Losses",
                  "Stock-Based Compensation", "Note 10", "Stock-Based Compensation"],
              3: ["Card Member loans reserve for credit losses increased for the year ended December 31, 2025.",
                  "(c)Primarily includes foreign currency translation adjustments of $ 3 million, $( 4 ) million and "
                  "$ 1 million for the years ended December 31, 2025, 2024 and 2023, respectively."]}
    notes = []
    for n, title in enumerate(titles, 1):
        notes += ["Table of Contents", f"NOTE {n}", title.upper(), *bodies.get(n, [f"{title} text."]), str(96 + 3 * n)]
    doc = "\n".join([*index, "91", "Table of Contents", "CONSOLIDATED STATEMENTS OF INCOME", *notes])
    note3 = documents.locator_section(doc, "Note3")
    assert note3.startswith("NOTE 3\nRESERVES FOR CREDIT LOSSES\n") and "NOTE 4" not in note3
    excerpt = documents.cut_excerpt(note3, 3,
                                    words="foreign currency translation adjustments to reserves for credit losses")
    assert excerpt.startswith("(c)Primarily includes foreign currency translation adjustments of $ 3 million")
    assert documents.locator_section(doc, "Note10").startswith("NOTE 10\nSTOCK-BASED COMPENSATION\n")
    # the index's last row has no next row to close it and would run on to the end of the document
    assert documents.locator_section(doc, "Note11").startswith("NOTE 11\nRETIREMENT PLANS\n")
    # rows of an index are no headings even where the notes' own headings are missing: the caller searches the filing
    assert documents.locator_section("\n".join(index), "Note3") is None
    row = "\n".join(["Index", "Note 3 – Reserves for Credit Losses ... 85", "Card Member loans reserve text."])
    assert documents.locator_section(row, "Note3") is None


def test_a_decline_year_of_a_52_53_week_fiscal_year_promotes_that_years_annual_report():
    """Years ending on the Sunday nearest December 31: fiscal 2022 ended on 2023-01-01, and xbrl_summary (so
    decline_years) names it FY2022."""
    ends = {2019: "2019-12-29", 2020: "2021-01-03", 2021: "2022-01-02", 2022: "2023-01-01", 2023: "2023-12-31",
            2024: "2024-12-29", 2025: "2025-12-28"}
    income = {2019: 130, 2020: 140, 2021: 150, 2022: 120, 2023: 160, 2024: 170, 2025: 180}
    rows = [row(income[y], end, f"{y + 1}-02-25", f"k{y}", str(dt.date.fromisoformat(end) - dt.timedelta(days=363)))
            for y, end in ends.items()]
    summary = archive.xbrl_summary({"facts": {"us-gaap": {"OperatingIncomeLoss": {"units": {"USD": rows}}}}}, cal=CAL,
                                   tags=lambda accession: None)
    declines = archive.decline_years(summary)
    assert declines == {2022}
    annual = [filing(f"k{y}", "10-K", f"{y + 1}-02-25", end) for y, end in ends.items()]
    picks = archive.valuation_selection(annual, as_of=dt.date(2026, 9, 28), cal=CAL, declines=declines)
    assert [p.selection.filing.accession for p in picks] == ["k2025", "k2022", "k2024", "k2023", "k2021", "k2020",
                                                             "k2019"]
    assert picks[1].reason.startswith("the annual report for fiscal 2022: MD&A (a decline year")


def test_a_valuations_working_is_registered_privately_by_its_tag():
    from pipeline import runner
    entries = runner.working_entries({"company": "NEWCO", "run_date": "2026-09-30", "step": "01C", "scope": "NEWCO",
                                      "context": {"run_dir": "2026-09-30-01C-rerun2"}})
    entry = entries["NEWCO-VAL-2026-09-30"]
    assert entry["location"] == "private:runs/NEWCO/2026-09-30-01C-rerun2/valuation_md.md"
    assert entry["visibility"] == "private" and runner.working_entries({"company": "X", "step": "15A"}) == {}


def test_a_named_note_is_the_note_itself_not_its_row_in_an_index_of_notes():
    """American Express's 10-K lists "Note 12 – Contingencies and Commitments ... 128" in an index and heads the note
    itself "NOTE 12" on its own line: the contingencies note came back as the two-line index row (test AXP-L8 reads
    it from FY2026Q3). Berkshire Hathaway's "(5) Equity method investments" is preferred over "(3) Investments in
    fixed maturity securities", which the broader title also matches."""
    body = "Text of the note that runs long enough to be a real part of the notes to the financial statements. " * 3
    axp = "\n".join(["Index to notes", "Note 3 – Reserves for Credit Losses", "85", "Note 12 – Contingencies and Commitments",
                     "128", "Item 8. Financial Statements and Supplementary Data",
                     "Notes to Consolidated Financial Statements", "NOTE 3", "RESERVES FOR CREDIT LOSSES", body,
                     "NOTE 12", "CONTINGENCIES AND COMMITMENTS", f"Legal proceedings. {body}", "NOTE 13",
                     "INCOME TAXES", body, "Item 9. Changes in and Disagreements with Accountants"])
    text, found, missing = documents.extract_sections(axp, "10-K", frozenset({"note:contingencies"}))
    assert found and "Legal proceedings." in text and "INCOME TAXES" not in text and "128" not in text
    brk = "\n".join(["Item 8. Financial Statements and Supplementary Data", "Notes to Consolidated Financial Statements",
                     "(1)", "Significant accounting policies", body, "(2)", "Significant business acquisitions", body,
                     "(3)", "Investments in fixed maturity securities", body * 3, "(4)", "Investments in equity securities",
                     body, "(5)", "Equity method investments", f"Kraft Heinz. {body}", "(6)", "Investment gains", body])
    text, found, _ = documents.extract_sections(brk, "10-K", frozenset({"note:equity method"}))
    assert found and "Kraft Heinz." in text and "fixed maturity" not in text


# ---- a company without an annual report yet: its offering prospectus (SpaceX listed in June 2026; decisions/0033)


def body(word: str, lines: int = 30) -> str:
    return "\n".join(f"{word} sentence {i} of the section, long enough to count as the text of its body." for i in
                     range(lines))


PROSPECTUS = "\n".join([
    "TABLE OF CONTENTS", "Page",
    "PROSPECTUS SUMMARY ......................................", "1",
    "RISK FACTORS ............................................", "9",
    "MANAGEMENT’S DISCUSSION AND ANALYSIS OF FINANCIAL CONDITION AND RESULTS", "OF OPERATIONS ......", "30",
    "BUSINESS ................................................", "60",
    "MANAGEMENT ..............................................", "90",
    "INDEX TO FINANCIAL STATEMENTS ...........................", "F-1",
    "PROSPECTUS SUMMARY", "Our Business", body("Summary"),
    "RISK FACTORS", body("Risk"),
    "USE OF PROCEEDS", body("Proceeds", 5),
    "MANAGEMENT’S DISCUSSION AND ANALYSIS OF FINANCIAL CONDITION AND RESULTS OF", "OPERATIONS", body("Revenue"),
    "BUSINESS", body("Launch"), "Management", body("Crew"),  # a subheading in title case does not end the section
    "MANAGEMENT", body("Director"),
    "EXECUTIVE COMPENSATION", body("Pay"),
    "CERTAIN RELATIONSHIPS AND RELATED PERSON TRANSACTIONS", body("Lease"),
    "SECURITY OWNERSHIP OF CERTAIN BENEFICIAL OWNERS AND MANAGEMENT", body("Holder"),
    "DESCRIPTION OF CAPITAL STOCK", body("Class-B"),
    "UNDERWRITING", body("Underwriter"),
    "INDEX TO FINANCIAL STATEMENTS",
    "Report of Independent Registered Public Accounting Firm ...................", "F-2",
    "Report of Independent Registered Public Accounting Firm", body("Opinion", 5),
    "Consolidated Balance Sheets", "(in millions)", "Total assets 100", body("Annual-note"),
    "Space Exploration Technologies Corp.", "Consolidated Balance Sheets", "(in millions)", "(unaudited)",
    "Total assets 120", body("Interim-note"),
])


def test_a_prospectus_is_cut_by_its_own_headings_and_its_interim_statements_can_be_left_out():
    text, found, missing = documents.extract_sections(PROSPECTUS, "424B4", frozenset({"mdna", "audited_statements"}))
    assert found == ["audited_statements", "mdna"] and missing == []
    assert text.startswith("--- section: MANAGEMENT'S DISCUSSION AND ANALYSIS OF FINANCIAL CONDITION AND RESULTS OF "
                           "OPERATIONS ---\nRevenue sentence 0")  # the wrapped heading is read as one
    assert "Annual-note sentence 29" in text and "Total assets 100" in text
    assert "Interim-note" not in text and "Total assets 120" not in text and "Risk sentence" not in text
    whole, found, _ = documents.extract_sections(PROSPECTUS, "424B4", frozenset({"financial_statements"}))
    assert found == ["financial_statements"] and "Interim-note sentence 29" in whole
    business, _, _ = documents.extract_sections(PROSPECTUS, "S-1/A", frozenset({"business"}))
    assert "Crew sentence 29" in business and "Director" not in business and "Summary" not in business
    governance, found, _ = documents.extract_sections(PROSPECTUS, "424B4", frozenset(
        {"management", "executive_compensation", "related_party", "principal_stockholders", "capital_stock"}))
    assert len(found) == 5 and all(f"{w} sentence 29" in governance for w in ("Director", "Pay", "Lease", "Holder"))
    assert "Underwriter" not in governance and "Launch" not in governance


def test_a_prospectus_supplies_the_sections_it_has_and_names_the_ones_it_lacks():
    without_pay = PROSPECTUS.replace("EXECUTIVE COMPENSATION", "COMPENSATION TABLES")
    text, found, missing = documents.extract_sections(without_pay, "424B4", frozenset({"business",
                                                                                      "executive_compensation"}))
    assert found == ["business"] and missing == ["executive_compensation"] and "Launch sentence 29" in text
    plain = body("Plain", 100)  # no heading at all: the whole text, as for a periodic report
    assert documents.extract_sections(plain, "424B4", frozenset({"mdna"})) == (plain, [], ["mdna"])
    families = documents.form_family("424B4"), documents.form_family("S-1/A"), documents.form_family("424B3")
    assert families == (documents.PROSPECTUS, documents.PROSPECTUS, None)  # 424B3: supplements, not offerings


IPO_FILINGS = [
    filing("n-s1", "S-1", "2026-05-20"),
    filing("n-s1a", "S-1/A", "2026-06-03"),
    filing("n-424b4", "424B4", "2026-06-12"),
    filing("n-8k-ipo", "8-K", "2026-06-15", items=("1.01", "5.02")),
    filing("n-8k-q2", "8-K", "2026-08-04", items=("2.02", "9.01")),
    filing("n-10q-q2", "10-Q", "2026-08-04", "2026-06-30"),
]
IPO_EVENTS = [event(IPO_FILINGS[4], "FY2026Q2", "2026-06-30")]
GOVERNANCE = ["capital_stock", "executive_compensation", "management", "principal_stockholders", "related_party"]


def chosen(picks: list[archive.Pick]) -> list[tuple[str, list[str]]]:
    return [(p.selection.filing.accession, sorted(p.selection.sections or [])) for p in picks]


def test_a_company_without_an_annual_report_is_read_from_its_final_prospectus_in_parts():
    picks = archive.build_selection(IPO_FILINGS, IPO_EVENTS, as_of=dt.date(2026, 10, 9), cal=CAL, foreign=False)
    assert chosen(picks) == [
        ("n-8k-q2", []), ("n-10q-q2", []),
        ("n-424b4", ["audited_statements", "mdna"]),  # the 10-Q supersedes the prospectus's interim statements
        ("n-424b4", ["business"]), ("n-424b4", GOVERNANCE),  # in place of the annual report and the proxy statement
        ("n-8k-ipo", []), ("n-424b4", ["risk_factors"])]
    assert {p.selection.kind for p in picks if p.selection.filing.form == "424B4"} == {documents.PROSPECTUS}
    before = archive.build_selection(IPO_FILINGS[:4], [], as_of=dt.date(2026, 7, 1), cal=CAL, foreign=False)
    assert chosen(before)[0] == ("n-424b4", ["financial_statements", "mdna"])  # no 10-Q yet: its interim ones stay
    registered = archive.build_selection(IPO_FILINGS[:2], [], as_of=dt.date(2026, 6, 5), cal=CAL, foreign=False)
    assert {a for a, _ in chosen(registered)} == {"n-s1a"}  # no final prospectus yet: the latest amendment
    later = [*IPO_FILINGS, filing("n-10k-2026", "10-K", "2027-02-20", "2026-12-31")]
    after = archive.build_selection(later, IPO_EVENTS, as_of=dt.date(2027, 3, 1), cal=CAL, foreign=False)
    assert "n-424b4" not in {a for a, _ in chosen(after)}  # the annual report carries the record from then on


def test_the_valuation_of_a_company_without_an_annual_report_reads_its_prospectus_record():
    picks = archive.valuation_selection(IPO_FILINGS, as_of=dt.date(2026, 10, 9), cal=CAL)
    assert chosen(picks) == [("n-10q-q2", []), ("n-424b4", ["audited_statements", "mdna"])]
    assert archive.valuation_selection(IPO_FILINGS[3:], as_of=dt.date(2026, 10, 9), cal=CAL) == []


def prospectus_documents(tmp_path: Path, text: str, sections: set[str]) -> registry.FilingText:
    """The prospectus documents the gateway supplies for a selection of `sections`, EDGAR answering with `text`."""
    prospectus = edgar.Filing(cik="0000000042", accession="0000000042-26-000007", form="424B4",
                              filing_date=dt.date(2026, 6, 12), primary_document="prospectus.txt")
    clock = fx.Clock()
    client = edgar.EdgarClient(user_agent=fx.FAKE_UA, cache_dir=tmp_path / "cache", sleep=clock.sleep, jitter=0,
                               transport=lambda url, headers, timeout: (200, {}, text.encode()),
                               limiter=edgar.RateLimiter(5, clock=clock.monotonic, sleep=clock.sleep))
    selection = documents.Selection(prospectus, True, False, frozenset(), documents.PROSPECTUS, frozenset(sections))
    subs = type("Subs", (), {"filings": [prospectus]})()
    (doc,) = registry.EdgarGateway(client).selection_documents(selection, ticker="NEWCO", cal=CAL, subs=subs,
                                                               known_tags={})
    return doc


def test_the_dossiers_source_note_names_the_prospectus_sections_kept_and_those_not_found(tmp_path):
    doc = prospectus_documents(tmp_path / "a", PROSPECTUS, {"business", "summary", "capital_stock"})
    assert doc.tag == "NEWCO-424B4-2026-06-12"
    assert doc.sections == ("business", "description of capital stock", "prospectus summary")
    assert doc.note.startswith("sections kept: business, description of capital stock, prospectus summary (")
    assert "Launch sentence 29" in doc.text and "Director" not in doc.text and "not found" not in doc.note
    renamed = PROSPECTUS.replace("CERTAIN RELATIONSHIPS AND RELATED PERSON TRANSACTIONS", "TRANSACTIONS WITH INSIDERS")
    doc = prospectus_documents(tmp_path / "b", renamed, {"business", "related_party", "capital_stock"})
    assert doc.note.endswith("; not found by heading: related-party transactions")
    assert "Launch sentence 29" in doc.text and "Class-B sentence 29" in doc.text
