"""Tests of the post-earnings steps (docs/decisions/0024): pipeline/documents.py, pipeline/evaluation.py, the
post-event assemblers of pipeline/registry.py, the ci step and the F2 placement actions of pipeline/runner.py, and
the event chain of pipeline/chain.py.

No network, no key, no model: EDGAR answers from tests/fixtures/edgar/ plus synthetic documents (APP's FY2026Q2 event
is on EDGAR there, FY2026Q3 is not), the prompts are the real front matter over synthetic text
(tests/runner_fixtures.py), the model is the fake client or a test double, and thesis-ci's evaluation interface is
the shim in tests/evaluate_shim.py.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import re
import sys
from pathlib import Path

import pytest
import yaml

from pipeline import chain, documents, edgar, evaluation, fake_client, llm, outputs, registry, runner, slicing
from tests import evaluate_shim
from tests import runner_fixtures as fx

EVENT = "FY2026Q2"  # APP's quarter on EDGAR in the fixtures: 8-K and 10-Q filed 2026-08-05
RUN = dt.date(2026, 10, 20)
UPDATE_STEPS = ("16B", "03-draft", "16A", "04A", "03R")


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """No real .env, EDGAR cache, prompt, log or schema directory; the network is off."""
    monkeypatch.delenv(edgar.UA_ENV, raising=False)
    monkeypatch.setenv(edgar.ENV_FILE_ENV, str(tmp_path / "missing.env"))
    monkeypatch.setenv(edgar.CACHE_ENV, str(tmp_path / "default-edgar-cache"))
    for name in (llm.PROMPTS_ENV, llm.LOG_ENV, outputs.SCHEMAS_ENV):
        monkeypatch.delenv(name, raising=False)

    def no_network(*args, **kwargs):
        raise AssertionError("these tests never use the network")

    monkeypatch.setattr(edgar, "UrllibTransport", no_network)


@pytest.fixture
def env(tmp_path):
    return fx.make_env(tmp_path)


@pytest.fixture
def shim(monkeypatch):
    evaluate_shim.install(monkeypatch)


def manifest_of(bundle: Path) -> dict:
    return yaml.safe_load((bundle / runner.MANIFEST).read_text(encoding="utf-8"))


def inputs_of(bundle: Path) -> dict[str, dict]:
    return {e["name"]: e for e in manifest_of(bundle)["inputs"]}


def read_input(bundle: Path, name: str) -> str:
    return (bundle / inputs_of(bundle)[name]["file"]).read_text(encoding="utf-8")


def set_thesis(env, **changes) -> None:
    """Rewrite APP's thesis.yml (status, trust_level, ...) and commit it, as the pipeline would maintain it."""
    path = env.public / "companies" / "APP" / "thesis.yml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data.update(changes)
    fx.write_yaml(path, data)
    if fx.git(env.public, "status", "--porcelain").strip():
        fx.commit_all(env.public, "thesis")


def run(env, step: str, **kwargs) -> Path:
    """Assemble and execute one step for APP's FY2026Q2 event with the fake client."""
    bundle = env.assemble(step, period=EVENT, **kwargs)
    record = env.execute(bundle)
    assert record["status"] == "succeeded", record.get("error")
    return bundle


def evaluate(env, **kwargs):
    kwargs.setdefault("out", io.StringIO())
    return runner.evaluate("APP", EVENT, run_date=RUN, roots=env.roots, edgar_gateway=env.gateway,
                           schemas_dir=env.schemas, allow_dirty=True, today=RUN, **kwargs)


def subs(env) -> edgar.Submissions:
    return edgar.submissions(fx.APP_CIK, client=env.gateway.client)


# ---------------------------------------------------------------------------------------------------- where


def test_where_is_split_into_clauses_outside_parentheses():
    text = "Rules of AI platforms (outside EDGAR; registered when judged); risk factors of the 10-K. Press releases"
    assert documents.split_clauses(text) == ["Rules of AI platforms (outside EDGAR; registered when judged)",
                                             "risk factors of the 10-K", "Press releases"]


def test_where_names_the_companys_own_filings_and_reports_everything_else():
    plan = documents.parse_where(fx.WHERE_L1, foreign=False, owners=["APP", "AppLovin"])
    assert [(r.kind, sorted(r.items), sorted(r.exhibits)) for r in plan.requests] == [
        ("earnings_release", ["2.02"], ["EX-99.1"]), ("quarterly_report", [], []), ("annual_report", [], [])]
    (note,) = plan.not_supplied
    assert note.startswith("call materials") and "not on EDGAR" in note

    owners = documents.owner_names("AXP", "American Express Company", "American Express")
    plan = documents.parse_where("Amex's and Delta's press releases and 8-Ks; both companies' earnings call materials",
                                 foreign=False, owners=owners)
    assert [r.kind for r in plan.requests] == ["current_report"]  # 8-Ks hold the press releases (EX-99)
    assert any("of Delta outside the company's own EDGAR filings" in n for n in plan.not_supplied)
    assert any("not on EDGAR" in n for n in plan.not_supplied)

    plan = documents.parse_where("Earnings press releases (8-K Item 2.02, Exhibit 99.1); press releases, official "
                                 "blogs and exchange announcements of the top publishers", foreign=False,
                                 owners=["APP", "AppLovin"])
    assert [r.kind for r in plan.requests] == ["earnings_release"]
    assert any(n.startswith("press releases, official blogs") and "not a filing of the company" in n
               for n in plan.not_supplied)

    plan = documents.parse_where("8-K Item 5.02, Form 4, Schedule 13D/G, DEF 14A", foreign=False, owners=["APP"])
    assert [(r.kind, sorted(r.items)) for r in plan.requests] == [
        ("current_report", ["5.02"]), ("proxy", []), ("ownership", []), ("beneficial_ownership", [])]
    assert plan.not_supplied == []

    plan = documents.parse_where("This period's 6-K Exhibit 99.1 and 20-F; 6-K announcements", foreign=True,
                                 owners=["PDD"])
    assert [(r.kind, sorted(r.exhibits)) for r in plan.requests] == [
        ("earnings_release", ["EX-99.1"]), ("annual_report", []), ("current_report", [])]


def test_the_lookback_window_and_the_filings_it_selects(env):
    cal = edgar.FiscalCalendar.parse("12-31")
    assert documents.lookback_periods("FY2026Q2", 4) == ["FY2025Q3", "FY2025Q4", "FY2026Q1", "FY2026Q2"]
    assert documents.window_start("FY2026Q2", 4, cal) == dt.date(2025, 7, 1)
    assert documents.window_start("FY2026Q2", 1, cal) == dt.date(2026, 4, 1)
    assert documents.window_start("FY2027Q1", 1, edgar.FiscalCalendar.parse("06-30")) == dt.date(2026, 7, 1)
    data = subs(env)
    events = edgar.earnings_events(data.cik, "12-31", filer_type="domestic", client=env.gateway.client, subs=data)
    plan = documents.parse_where(fx.WHERE_L1, foreign=False, owners=["APP"])
    selected, notes = documents.select_filings(plan.requests, data.filings, events, period=EVENT, lookback=1,
                                               as_of=RUN, cal=cal, foreign=False)
    assert [(s.filing.accession, s.primary, s.exhibits, sorted(s.only_exhibits)) for s in selected] == [
        ("0001751008-26-000010", True, False, []),  # the 10-K in force (FY2025), not in the one-quarter window
        ("0001751008-26-000057", False, True, ["EX-99.1"]),  # the FY2026Q2 results 8-K: its EX-99.1 only
        ("0001751008-26-000059", True, False, []),  # the FY2026Q2 10-Q
    ]
    assert notes == []
    plan = documents.parse_where("Form 4", foreign=False, owners=["APP"])
    selected, _ = documents.select_filings(plan.requests, data.filings, events, period=EVENT, lookback=1,
                                           as_of=RUN, cal=cal, foreign=False)
    assert {s.filing.form for s in selected} == {"3", "4"}  # Form 4 names the ownership reports: Forms 3, 4 and 5
    assert all(s.filing.filing_date >= dt.date(2026, 4, 1) and s.kind == "ownership" for s in selected)


def test_same_day_filings_of_one_form_are_numbered_in_acceptance_order(env):
    data = subs(env)
    cal = edgar.FiscalCalendar.parse("12-31")
    same_day = [f for f in data.filings if f.form == "4" and f.filing_date == dt.date(2026, 8, 21)]
    tags = sorted(documents.tag_with_ordinal("APP", f, cal, data.filings) for f in same_day)
    assert tags == ["APP-4-2026-08-21", "APP-4-2026-08-21-2"]
    ten_q = data.get("0001751008-26-000059")
    assert documents.tag_with_ordinal("APP", ten_q, cal, data.filings) == "APP-10Q-FY2026Q2"


@pytest.mark.parametrize("value, found", [
    (1234, "Installs grew to 1,234 million in the quarter."),
    (19637, "Total revenues / FX-adjusted … $19,637"),
    (-191, "Reserve release … (191)"),
    ("Installs grew", "Installs grew to 1,234 million in the quarter."),
    (123, None),  # 1,234 does not contain the number 123
    (None, None),
])
def test_source_excerpts_are_the_sentence_or_the_table_row_that_holds_the_value(value, found):
    document = ("Quarter results\nInstalls grew to 1,234 million in the quarter. Margins were stable.\n"
                "Total revenues\nFX-adjusted\n$19,637\n$17,856\nReserve release\n(191)\n")
    assert documents.cut_excerpt(document, value) == found


# ---------------------------------------------------------------------------------------------------- evaluation


def test_due_tests_follow_effective_from_retired_at_and_first_readable():
    tests = {"tests": [
        {"id": "T-Q1", "type": "quantitative", "effective_from": "FY2026Q3"},
        {"id": "T-Q2", "type": "quantitative", "effective_from": "FY2026Q1", "retired_at": "FY2026Q3"},
        {"id": "T-Q3", "type": "quantitative", "effective_from": "FY2026Q1", "first_readable": "2026-11"},
        {"id": "T-Q4", "type": "quantitative", "effective_from": "FY2026Q1", "first_readable": "2026-10"},
    ]}
    due, skipped = evaluation.due_tests(tests, "quantitative", "FY2026Q3", dt.date(2026, 10, 21))
    assert [t["id"] for t in due] == ["T-Q1", "T-Q4"]
    assert [s["test_id"] for s in skipped] == ["T-Q2", "T-Q3"]


def test_16b_definitions_carry_no_thesis_wording_and_name_segments():
    registry_, _ = {m["id"]: m for m in fx.METRICS_YML["metrics"]}, None
    found = evaluation.text_metrics(fx.THESIS_APP, EVENT, RUN, registry_)
    ids = [d["id"] for d in found.definitions]
    assert ids == ["unit_drivers_yoy", "segment_revenue_yoy[Apps]"]
    assert fx.CANARY_TEST not in json.dumps(found.definitions)
    assert "xbrl" not in found.definitions[0]["components"]["installs"]
    assert found.definitions[1]["params"] == {"segment": "Apps", "basis": "reported"}
    assert found.skipped == [{"metric": "annual_text_metric", "reason": "first readable 2027-02"}]
    assert found.names["segment_revenue_yoy[Apps]"] == {"metric": "segment_revenue_yoy", "segment": "Apps",
                                                        "unit": "%", "components": {}}
    assert evaluation.xbrl_metric_ids(fx.THESIS_APP, registry_) == ["revenue_yoy", "unit_drivers_yoy.installs"]


def test_16b_values_become_thesis_ci_readings():
    names = evaluation.text_metrics(fx.THESIS_APP, EVENT, RUN, {m["id"]: m for m in fx.METRICS_YML["metrics"]}).names
    values = [
        {"metric": "unit_drivers_yoy", "value": "12.5%", "unit": None, "period": "FY2026 Q2",
         "source": "[src:APP-10Q-FY2026Q2#Item2]", "basis": "reported", "confidence": "high"},
        {"metric": "unit_drivers_yoy.installs", "value": "1,234", "unit": "count", "period": "FY2026Q2",
         "source": "APP-10Q-FY2026Q2, Item 2"},
        {"metric": "segment_revenue_yoy[Apps]", "value": "(3.0)", "unit": "%", "period": "FY2026Q2",
         "source": "APP-8K-2026-08-05#EX-99.1", "currency": "USD"},
        {"metric": "segment_revenue_yoy[Apps]", "value": "not_found", "period": "FY2026Q2", "source": None,
         "note": "the filing gives no segment table"},
        {"metric": "unit_drivers_yoy", "value": 3, "period": "Q2 2026", "source": "APP-10Q-FY2026Q2"},
    ]
    readings, unusable = evaluation.readings_from_metric_values(values, run="runs/APP/x-16B", names=names)
    assert [(r["metric"], r["period"], r["value"], r["unit"], r["source"]) for r in readings] == [
        ("unit_drivers_yoy", "FY2026Q2", 12.5, "%", "APP-10Q-FY2026Q2#Item2"),
        ("unit_drivers_yoy.installs", "FY2026Q2", 1234.0, "count", "APP-10Q-FY2026Q2"),
        ("segment_revenue_yoy", "FY2026Q2", -3.0, "%", "APP-8K-2026-08-05#EX-99.1"),
    ]
    assert readings[2]["segment"] == "Apps" and readings[2]["note"].startswith("currency USD")
    assert "16B (runs/APP/x-16B)" in readings[0]["basis"]
    assert [u["reason"] for u in unusable] == ["the filing gives no segment table",
                                               "period 'Q2 2026' is not a fiscal period"]


@pytest.mark.parametrize("text, number", [("12.5", 12.5), ("12.5%", 12.5), ("(3.0)", -3.0), ("-1,234", -1234.0),
                                          ("$1,234.5", 1234.5), ("not_found", None), ("about 5", None), (7, 7.0),
                                          (True, None)])
def test_values_are_read_as_numbers_only_when_they_are_numbers(text, number):
    assert evaluation.parse_number(text) == number


# ---------------------------------------------------------------------------------------------------- the event


def test_the_event_filings_and_the_event_record(env):
    bundle = env.assemble("16B", period=EVENT)
    filings = read_input(bundle, "filings")
    assert "The filings of APP's FY2026Q2 earnings event" in filings and fx.CANARY_FILING in filings
    assert "[src:APP-8K-2026-08-05#EX-99.1]" in filings and "[src:APP-10Q-FY2026Q2]" in filings
    assert "hidden contexts" not in filings and "not on EDGAR" in filings
    assert not inputs_of(bundle)["filings"].get("substitute")
    env.gateway.client.offline = True  # everything the 14T event record needs is cached by now
    ctx = registry.RunContext(step=registry.STEPS["14T"], company="APP", period=EVENT, run_date=RUN,
                              public_root=env.public, private_root=env.private, workspace_root=env.workspace,
                              call=None, formats={}, edgar=env.gateway)
    record = yaml.safe_load(registry.INPUTS["event"](ctx, "event").text)
    assert (record["period"], record["closes_on"], record["closed_by"], record["closed"]) == (
        EVENT, "2026-08-05", "10-Q", True)


def test_a_post_event_step_needs_the_event_on_edgar_and_closed(env, monkeypatch):
    with pytest.raises(runner.RunnerError, match="EDGAR has no results release for FY2026Q3 as of 2026-10-20"):
        env.assemble("16B", period="FY2026Q3")
    real = edgar.earnings_events

    def open_event(*args, **kwargs):  # as if the 10-Q were still to come: the event closes five business days later
        events = real(*args, **kwargs)
        for event in events:
            event.closing, event.closes_on, event.closed_by = None, dt.date(2026, 8, 12), "T+5"
        return events

    monkeypatch.setattr(edgar, "earnings_events", open_event)
    with pytest.raises(runner.RunnerError, match=r"released 2026-08-05\) closes on 2026-08-12 .* on or after"):
        env.assemble("16B", period=EVENT, run_date=dt.date(2026, 8, 6), today=dt.date(2026, 8, 6))
    assert not (env.private / "runs" / "APP").exists()


def test_a_dry_run_before_the_event_rehearses_with_the_last_reported_quarter(env, tmp_path):
    bundle, record = runner.dry_run("16B", "APP", "FY2026Q3", run_date=RUN, roots=env.roots, out_root=tmp_path / "dry",
                                    edgar_gateway=env.gateway, schemas_dir=env.schemas, today=RUN, out=io.StringIO())
    assert record["status"] == "succeeded" and manifest_of(bundle)["rehearsal"] is True
    entry = inputs_of(bundle)["filings"]
    assert entry["substitute"] == "filings" and "FY2026Q2 stand in (dry run only)" in entry["note"]
    with pytest.raises(runner.RunnerError, match="for dry runs only"):
        runner.assemble("16B", "APP", "FY2026Q3", run_date=RUN, roots=env.roots, edgar_gateway=env.gateway,
                        schemas_dir=env.schemas, allow_dirty=True, today=RUN, rehearsal=True)


# ---------------------------------------------------------------------------------------------------- 16B and ci


def test_16b_gets_the_filings_and_the_metric_definitions_only(env):
    bundle = env.assemble("16B", period=EVENT)
    assert list(inputs_of(bundle)) == ["filings", "metric_definitions"]
    assert manifest_of(bundle)["omitted"] == [{"name": "prior_values", "reason": "no earlier 16B run for APP: this is "
                                                                                 "the first period read from filing "
                                                                                 "text"}]
    definitions = yaml.safe_load(read_input(bundle, "metric_definitions"))
    assert [m["id"] for m in definitions["metrics"]] == ["unit_drivers_yoy", "segment_revenue_yoy[Apps]"]
    assert definitions["not_read_this_period"] == [{"metric": "annual_text_metric", "reason": "first readable 2027-02"}]
    everything = "".join((bundle / e["file"]).read_text(encoding="utf-8") for e in manifest_of(bundle)["inputs"])
    assert fx.CANARY_TEST not in everything and fx.CANARY_THESIS not in everything
    source = inputs_of(bundle)["metric_definitions"]["sources"][0]
    assert source["tests_by_metric"] == {"unit_drivers_yoy": ["APP-Q5"], "segment_revenue_yoy": ["APP-Q7"]}


def test_prior_values_are_the_previous_periods_readings(env):
    earlier = env.private / "runs" / "APP" / "2026-08-10-16B"
    fx.write_yaml(earlier / runner.MANIFEST, {"manifest_version": 1, "step": "16B", "company": "APP",
                                              "period": "FY2026Q1"})
    fx.write_yaml(earlier / runner.RUN_RECORD, {"status": "succeeded"})
    fx.write(earlier / "outputs" / "metric_values.yml", "- metric: unit_drivers_yoy\n  value: 9\n")
    entry = inputs_of(env.assemble("16B", period=EVENT))["prior_values"]
    assert entry["note"] == "metric_values of FY2026Q1 (runs/APP/2026-08-10-16B)"


def test_evaluate_records_the_ci_step_like_a_run(env, shim):
    bundle16b = run(env, "16B")
    fx.rewrite_output(bundle16b, "metric_values", yaml.safe_dump([
        {"metric": "segment_revenue_yoy[Apps]", "value": "-3%", "unit": "%", "period": EVENT,
         "source": "[src:APP-8K-2026-08-05#EX-99.1]", "basis": "reported"},
        {"metric": "unit_drivers_yoy", "value": "not_found", "unit": "%", "period": EVENT, "source": None,
         "note": "no installs table"}]))
    bundle, record = evaluate(env)
    assert bundle == env.private / "runs" / "APP" / "2026-10-20-ci"
    assert (record["status"], record["backend"], record["requests"]) == ("succeeded", "pipeline", 0)
    assert record["results"] == {"pass": 1, "fail": 1, "undetermined": 2}
    results = {r["id"]: r for r in yaml.safe_load((bundle / "outputs" / "ci_results.yml").read_text())["results"]}
    assert results["APP-Q1"]["result"] == "pass" and results["APP-Q7"]["result"] == "fail"
    readings = yaml.safe_load(read_input(bundle, "readings"))
    assert {(r["metric"], r.get("segment")) for r in readings["readings"]} == {("revenue_yoy", None),
                                                                              ("segment_revenue_yoy", "Apps")}
    assert readings["not_usable"][0]["reason"] == "no installs table"
    manifest = manifest_of(bundle)
    assert manifest["role"] == "pipeline" and manifest["prompt"] is None and manifest["evaluator"]["placeholder"] is False
    runner.verify_bundle(bundle, manifest)
    kinds = [s.get("detail") or s.get("output") for s in inputs_of(bundle)["readings"]["sources"]]
    assert kinds == ["XBRL companyfacts", "metric_values"]
    calls = {c["function"]: c for c in evaluate_shim.CALLS}
    assert calls["readings_from_companyfacts"]["metric_ids"] == ["revenue_yoy", "unit_drivers_yoy.installs"]
    assert calls["evaluate_company"]["today"] == RUN
    with pytest.raises(runner.RunnerError, match="already evaluated"):
        evaluate(env)
    with pytest.raises(runner.RunnerError, match="deterministic step"):
        env.execute(bundle)


def test_evaluate_needs_16b_when_a_due_test_reads_filing_text(env, shim):
    with pytest.raises(runner.RunnerError, match=r"2 due metric\(s\) are read from filing text and no 16B run"):
        evaluate(env)
    assert not (env.private / "runs" / "APP" / "2026-10-20-ci").exists()


def test_without_thesis_cis_evaluation_only_a_dry_run_evaluates(env, monkeypatch, tmp_path):
    evaluate_shim.remove(monkeypatch)
    run(env, "16B")
    with pytest.raises(runner.RunnerError, match="thesis-ci v0.4.0 or later"):
        evaluate(env)
    bundle, record = evaluate(env, out_root=tmp_path / "dry")
    assert record["client"] == "placeholder" and set(record["results"]) == {"undetermined"}
    assert manifest_of(bundle)["evaluator"]["placeholder"] is True


def test_a_failed_evaluation_is_recorded_and_retried(env, shim, monkeypatch):
    run(env, "16B")

    def broken(thesis, readings, period, today):
        raise ValueError("synthetic failure")

    monkeypatch.setattr(sys.modules["thesis_ci.evaluate"], "evaluate_company", broken)
    bundle, record = evaluate(env)
    assert record["status"] == "failed" and record["error"]["type"] == "ValueError" and not (bundle / "outputs").exists()
    evaluate_shim.install(monkeypatch)
    _, retried = evaluate(env, retry=True)
    assert retried["status"] == "succeeded" and retried["attempt"] == 2
    assert yaml.safe_load((bundle / "attempts" / "1" / runner.RUN_RECORD).read_text())["status"] == "failed"


# ---------------------------------------------------------------------------------------------------- 14T


def test_14t_gets_the_due_tests_without_thesis_wording_and_their_documents(env):
    bundle = env.assemble("14T", period=EVENT)
    assert manifest_of(bundle)["role"] == "judge" and list(inputs_of(bundle)) == ["event", "qualitative_tests",
                                                                                  "where_documents"]
    tests = yaml.safe_load(read_input(bundle, "qualitative_tests"))["tests"]
    assert [t["id"] for t in tests] == ["APP-L1"]  # APP-L4 is first readable in 2027
    assert tests[0]["baseline"] == {"value": "No acquisition in 2026", "as_of": "2026-09-24",
                                    "source": "APP-8K-2026-08-05#EX-99.1"}
    assert set(tests[0]) == {"id", "question", "fail_if", "warn_if", "judge_notes", "baseline"}
    documents_text = read_input(bundle, "where_documents")
    plan = yaml.safe_load(documents_text.split("\n\n", 1)[1].split("\n=====", 1)[0])["tests"][0]
    assert plan["supplied"] == ["APP-10K-FY2025", "APP-8K-2026-08-05#EX-99.1", "APP-10Q-FY2026Q2"]
    assert plan["periods"] == [EVENT] and plan["not_supplied"][0].startswith("call materials")
    assert "[src:APP-10K-FY2025] 10-K" in documents_text and fx.CANARY_FILING in documents_text
    everything = "".join((bundle / e["file"]).read_text(encoding="utf-8") for e in manifest_of(bundle)["inputs"])
    assert fx.CANARY_TEST not in everything and fx.CANARY_THESIS not in everything
    event = yaml.safe_load(read_input(bundle, "event"))
    assert event["period"] == EVENT and event["closed"] is True
    recorded = {x["tag"]: x for x in inputs_of(bundle)["where_documents"]["sources"]}
    # the synthetic 10-Q has no item headings, so the MD&A the clause names cannot be cut out: the whole filing
    assert recorded["APP-10Q-FY2026Q2"]["note"] == "the whole filing: section(s) not found by heading: MD&A"


def test_14t_runs_for_holdings_only(env):
    set_thesis(env, status="candidate")
    with pytest.raises(runner.RunnerError, match="holdings only"):
        env.assemble("14T", period=EVENT)


# ---------------------------------------------------------------------------------------------------- 03, 16A, 04A, 03R


def test_the_draft_of_a_candidate_leaves_out_what_candidates_do_not_have(env, shim):
    set_thesis(env, status="candidate")
    run(env, "16B")
    evaluate(env)
    bundle = env.assemble("03-draft", period=EVENT)
    manifest = manifest_of(bundle)
    omitted = {o["name"]: o["reason"] for o in manifest["omitted"]}
    assert set(omitted) == {"qualitative_verdicts", "prereg_settlement", "prereg_due", "question_list", "owner_notes"}
    assert "runs only for holdings" in omitted["qualitative_verdicts"] and "do not pre-register" in omitted["prereg_due"]
    entries = inputs_of(bundle)
    assert [e for e in entries if entries[e].get("empty")] == ["ledger_settlement", "pending_archive_patch",
                                                               "pending_test_proposals", "pending_ledger_entries"]
    assert entries["dossier"]["substitute"] == "dossier"
    assert entries["ci_results"]["sources"][0]["run"] == "runs/APP/2026-10-20-ci"
    assert manifest["pipeline_fields"]["thesis"] == {k: fx.THESIS_APP[k] for k in registry.PIPELINE_THESIS_FIELDS
                                                     if k in fx.THESIS_APP} | {"status": "candidate"}
    assert manifest["pipeline_fields"]["ledger"] == {"company": "APP"}
    assert manifest["variables"]["period"] == EVENT and manifest["bundle"] == "runs/APP/2026-10-20-03-draft"


def test_the_draft_needs_the_evaluation_and_a_holdings_draft_needs_the_judge(env):
    with pytest.raises(runner.RunnerError) as info:
        env.assemble("03-draft", period=EVENT)
    message = str(info.value)
    assert "- ci_results: ci_results: no succeeded ci run for APP FY2026Q2" in message
    assert "- qualitative_verdicts: qualitative_verdicts: no succeeded 14T run" in message
    assert "- question_list: no placed 14Q question list for APP FY2026Q2" in message


def draft_chain(env, shim_installed=True) -> Path:
    """A candidate's 16B, ci and 03 draft for the event, executed with the fake client."""
    set_thesis(env, status="candidate")
    run(env, "16B")
    evaluate(env)
    return run(env, "03-draft")


def test_16a_extracts_from_the_draft_and_04a_gets_facts_with_source_excerpts(env, shim):
    draft = draft_chain(env)
    fx.rewrite_output(draft, "update", "---\ncompany: APP\ndoc: update\nas_of: 2026-10-20\ndoc_status: draft\n---\n"
                                       "Installs grew to 1,234 million [src:APP-10Q-FY2026Q2].\n")
    product = env.assemble("16A", period=EVENT)
    text = read_input(product, "product")
    assert "===== output: update (update.md) =====" in text and "Installs grew to 1,234 million" in text
    assert "===== changes: thesis.yml =====" in text and "===== source table" in text
    assert "APP-RPT1-2026-09-20" in text  # the private sources.yml is part of the table
    env.execute(product)
    fx.rewrite_output(product, "fact_table", yaml.safe_dump({"as_of": "2026-10-20", "facts": [
        {"id": "F001", "location": "update 1", "subject": "APP", "what": "installs", "value": 1234,
         "unit": "million", "period": EVENT, "source": "APP-10Q-FY2026Q2", "excerpt": "Installs grew to 1,234 million"},
        {"id": "F002", "location": "update 1", "subject": "APP", "what": "installs", "value": 999, "unit": None,
         "period": None, "source": None, "untagged": True, "excerpt": "..."}]}))
    audit = env.assemble("04A", period=EVENT)
    facts = yaml.safe_load(read_input(audit, "fact_table"))["facts"]
    assert facts[0]["source_excerpt"] == "Installs grew to 1,234 million in the quarter."
    assert facts[1]["excerpt_missing"] is True and "source_excerpt" not in facts[1]
    assert inputs_of(audit)["fact_table"]["note"] == "source excerpts cut: 1; excerpt_missing: 1"
    sources = inputs_of(audit)["sources"]
    assert sources["file"] == "inputs/sources.txt" and "[src:APP-10Q-FY2026Q2]" in read_input(audit, "sources")
    assert manifest_of(audit)["variables"]["subject"] == "quarterly update"
    assert manifest_of(audit)["context"]["subject"] == "quarterly update"


def test_03r_gets_the_draft_the_findings_and_the_sources(env, shim):
    draft_chain(env)
    run(env, "16A")
    run(env, "04A")
    bundle = env.assemble("03R", period=EVENT)
    assert list(inputs_of(bundle)) == ["draft_outputs", "findings_04A", "sources"]
    omitted = {o["name"] for o in manifest_of(bundle)["omitted"]}
    assert omitted == {"test_proposals_04B_lite", "returns_17A"}
    text = read_input(bundle, "draft_outputs")
    assert "===== output: thesis (thesis.yml) =====" in text and "===== output: pr_body (pr_body.md) =====" in text
    record = env.execute(bundle)
    assert record["status"] == "succeeded"
    thesis = yaml.safe_load((bundle / "outputs" / "thesis.yml").read_text(encoding="utf-8"))
    assert thesis["status"] == "candidate" and thesis["trust_level"] == 1  # carried over, G8 fields from the pipeline


# ---------------------------------------------------------------------------------------------------- slices (0026)

FACTS = [
    {"id": "F001", "location": "update 1", "subject": "APP", "what": "installs", "value": 1234, "unit": "million",
     "period": EVENT, "source": "APP-10Q-FY2026Q2", "excerpt": "Installs grew to 1,234 million"},
    {"id": "F002", "location": "update 1", "subject": "APP", "what": "margin", "value": 999, "unit": None,
     "period": None, "source": None, "untagged": True, "excerpt": "..."},
    {"id": "F003", "location": "update 2", "subject": "APP", "what": "installs again", "value": 1234,
     "unit": "million", "period": EVENT, "source": "APP-10Q-FY2026Q2#p4", "excerpt": "1,234 million installs"},
    {"id": "F004", "location": "update 2", "subject": "APP", "what": "growth", "value": 5, "unit": "%",
     "period": EVENT, "source": "APP-10Q-FY2026Q2", "excerpt": "up 5%", "derived": True, "inputs": ["F001"],
     "formula": "1,234 / 1,175 - 1"},
]


def sliced_audit(env, monkeypatch, max_facts: int) -> Path:
    draft_chain(env)
    extraction = run(env, "16A")
    fx.rewrite_output(extraction, "fact_table", yaml.safe_dump({"as_of": "2026-10-20", "facts": FACTS}))
    monkeypatch.setattr(slicing, "MAX_FACTS", max_facts)
    return env.assemble("04A", period=EVENT)


def slice_input(bundle: Path, part: dict, name: str) -> str:
    entry = next(e for e in part["inputs"] if e["name"] == name)
    return (bundle / entry["file"]).read_text(encoding="utf-8")


def test_a_long_fact_table_is_audited_in_slices_whose_outputs_are_merged(env, shim, monkeypatch):
    """decisions/0026: each slice judges its own facts against its own documents; outputs/ holds the merged outputs,
    which the revision reads as it would one call's."""
    audit = sliced_audit(env, monkeypatch, max_facts=1)
    manifest = manifest_of(audit)
    assert manifest["inputs"] == [] and [s["id"] for s in manifest["slices"]] == ["s01", "s02", "s03", "s04"]
    assert manifest["request_sha256"] is None and manifest["estimate"]["slices"] == 4
    tables = [yaml.safe_load(slice_input(audit, s, "fact_table")) for s in manifest["slices"]]
    assert [[f["id"] for f in t["facts"]] for t in tables] == [["F001"], ["F002"], ["F003"], ["F004"]]
    assert tables[3]["slice"] == {"number": 4, "of": 4, "note": tables[3]["slice"]["note"]}
    assert [f["id"] for f in tables[3]["context_facts"]] == ["F001"] and "context_facts" not in tables[0]
    assert tables[0]["facts"][0]["source_excerpt"] == "Installs grew to 1,234 million in the quarter."
    assert "[src:APP-10Q-FY2026Q2]" in slice_input(audit, manifest["slices"][0], "sources")
    assert "slice s04" in runner.describe_bundle(audit)

    record = env.execute(audit)
    assert record["status"] == "succeeded" and [r["status"] for r in record["slices"]] == ["succeeded"] * 4
    assert record["requests"] == 4 and record["merged"]["slices"] == 4
    verdicts = yaml.safe_load((audit / "outputs" / "fact_verdicts.yml").read_text(encoding="utf-8"))
    findings = yaml.safe_load((audit / "outputs" / "findings.yml").read_text(encoding="utf-8"))
    assert [v["id"] for v in verdicts] == ["F001", "F002", "F003", "F004"]
    assert [f["id"] for f in findings] == ["04A-01", "04A-02", "04A-03", "04A-04"]
    assert record["outputs"]["findings"]["merged_from_slices"] is True
    assert all((audit / "slices" / s / "run.yml").is_file() for s in ("s01", "s02", "s03", "s04"))
    revision = env.assemble("03R", period=EVENT)
    assert "04A-04" in read_input(revision, "findings_04A")


def test_a_failed_slice_is_the_only_one_run_again(env, shim, monkeypatch):
    audit = sliced_audit(env, monkeypatch, max_facts=2)
    called: list[int] = []
    real = llm.complete

    def flaky(*args, **kwargs):
        number = yaml.safe_load(args[2]["fact_table"])["slice"]["number"]
        called.append(number)
        if number == 2 and called.count(2) == 1:
            raise RuntimeError("the connection dropped")
        return real(*args, **kwargs)

    monkeypatch.setattr(runner.llm, "complete", flaky)
    first = env.execute(audit)
    assert first["status"] == "failed" and first["error"]["type"] == "SlicesFailed"
    assert [(r["id"], r["status"]) for r in first["slices"]] == [("s01", "succeeded"), ("s02", "failed")]
    assert not (audit / "outputs").exists()
    with pytest.raises(runner.RunnerError, match="--retry"):
        env.execute(audit)
    second = env.execute(audit, retry=True)
    assert second["status"] == "succeeded" and called == [1, 2, 2]
    assert second["attempt"] == 2 and second["slices"][0]["this_run"] is False
    assert (audit / "attempts" / "1" / "run.yml").is_file()
    assert (audit / "slices" / "s02" / "attempts" / "1" / "run.yml").is_file()


def test_a_long_draft_is_extracted_in_slices_and_the_fact_ids_run_on(env, shim, monkeypatch):
    draft_chain(env)
    monkeypatch.setattr(slicing, "PRODUCT_TOKENS", 60)
    extraction = env.assemble("16A", period=EVENT)
    parts = manifest_of(extraction)["slices"]
    assert len(parts) >= 2
    first = slice_input(extraction, parts[0], "product")
    assert f"[Pipeline note: this call gets slice 1 of {len(parts)} of the product" in first
    assert all(slice_input(extraction, part, "product").count("===== source table") == 1 for part in parts)
    record = env.execute(extraction)
    assert record["status"] == "succeeded"
    table = yaml.safe_load((extraction / "outputs" / "fact_table.yml").read_text(encoding="utf-8"))
    assert [f["id"] for f in table["facts"]] == [f"F{i:03d}" for i in range(1, len(parts) + 1)]
    assert record["merged"]["fact_ids"]["s02"] == "F002–F002"
    audit = env.assemble("04A", period=EVENT)  # the merged table, in one call again
    assert [f["id"] for f in yaml.safe_load(read_input(audit, "fact_table"))["facts"]] == \
        [f"F{i:03d}" for i in range(1, len(parts) + 1)]


def test_a_request_that_cannot_fit_the_context_is_refused_before_anything_is_written(env, shim, monkeypatch):
    set_thesis(env, status="candidate")
    run(env, "16B")
    evaluate(env)
    monkeypatch.setattr(slicing, "MAX_INPUT_TOKENS", 1000)
    with pytest.raises(runner.RunnerError, match=r"03-draft: the request is estimated at [\d,]+ input tokens, more "
                                                 r"than the 1,000"):
        env.assemble("03-draft", period=EVENT)
    assert not (env.private / "runs" / "APP" / "2026-10-20-03-draft").exists()


# ---------------------------------------------------------------------------------------------------- placement

NOW = dt.datetime(2026, 10, 20, 18, 0, tzinfo=dt.timezone.utc)
DOSSIER = ("# APP dossier\n\n## 1. Business\n\nWhat it sells.\n\n## 3. Moat\n\nOld moat text.\n\n"
           "## Version history\n\n- v1 2026-09-25: first build.\n")
MISTAKE = "### 2026-10-20 · APP · Fact error\n\n- What was written: a synthetic mistake.\n"


def place(env, bundle: Path, **kwargs):
    kwargs.setdefault("allow_fake", True)
    kwargs.setdefault("lint", False)
    kwargs.setdefault("now", NOW)
    kwargs.setdefault("out", io.StringIO())
    kwargs.setdefault("edgar_gateway", env.gateway)
    return runner.place(bundle, roots=env.roots, schemas_dir=env.schemas, **kwargs)


def set_levels(env, level: int) -> None:
    set_thesis(env, trust_level=level)
    path = env.public / "trust" / "levels.yml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["companies"]["APP"] = level
    fx.write_yaml(path, data)
    if fx.git(env.public, "status", "--porcelain").strip():
        fx.commit_all(env.public, "trust")


def revision(env, level: int = 1) -> Path:
    """A candidate's executed 03R bundle whose outputs exercise every F2 action."""
    set_levels(env, level)
    draft_chain(env)
    run(env, "16A")
    audit = run(env, "04A")
    fx.rewrite_output(audit, "findings", yaml.safe_dump([
        {"id": "04A-01", "group": "must fix", "type": "fact_error", "location": "2, Installs",
         "quote": "Installs grew", "evidence": "The 10-Q says otherwise.", "fix": "Installs fell."}], sort_keys=False))
    bundle = run(env, "03R")
    fx.rewrite_output(bundle, "update", "---\ncompany: APP\ndoc: update\nas_of: 2026-10-20\ndoc_status: final\n---\n\n"
                                        "## 1. Does this change the thesis?\n\nNo.\n")
    fx.rewrite_output(bundle, "reviewed_sections", "[monitoring]\n")
    fx.rewrite_output(bundle, "mistakes_entry", MISTAKE)
    fx.rewrite_output(bundle, "sources_additions", yaml.safe_dump([
        {"tag": "APP-10Q-FY2026Q2", "kind": "filing", "title": "APP 10-Q FY2026Q2", "visibility": "public"},
        {"tag": "APP-NOTE-2026-10-20", "kind": "other", "title": "a private note", "visibility": "private"}]))
    fx.rewrite_output(bundle, "dossier_changes", yaml.safe_dump([
        {"section": "moat", "text": "New moat text."},
        {"section": "version_history", "text": "- v2 2026-10-20: the moat part rewritten."}]))
    thesis = yaml.safe_load((bundle / "outputs" / "thesis.yml").read_text(encoding="utf-8"))
    thesis["todo"] = [*thesis.get("todo", []), "Synthetic follow-up from the update."]
    fx.rewrite_output(bundle, "thesis", yaml.safe_dump(thesis, sort_keys=False))
    fx.rewrite_output(bundle, "pr_body", "Summary: nothing changes.\n")
    fx.write(env.private / "companies" / "APP" / "dossier.md", DOSSIER)
    return bundle


def test_a_quarterly_update_at_trust_level_2_is_placed_with_every_f2_action(env, shim):
    bundle = revision(env, level=2)
    report = place(env, bundle)
    assert report["public_branch"] == "pipeline/APP-2026-10-20-03R" and report["routing"]["trust_level"] == 2
    update = (env.public / "companies" / "APP" / "updates" / "2026-10-20.md").read_text(encoding="utf-8")
    assert yaml.safe_load(outputs.split_front_matter(update)[0])["reviewed_sections"] == ["monitoring"]
    mistakes = (env.public / "mistakes.md").read_text(encoding="utf-8")
    assert mistakes.index("### 2026-10-20 · APP") < mistakes.index("### 2026-09-25 · APP")  # newest first
    public_tags = [e["tag"] for e in yaml.safe_load((env.public / "companies/APP/sources.yml").read_text())["sources"]]
    private = yaml.safe_load((env.private / "companies/APP/sources.yml").read_text())["sources"]
    assert public_tags == ["APP-8K-2026-08-05", "APP-10Q-FY2026Q2"]
    assert [e["tag"] for e in private] == ["APP-RPT1-2026-09-20", "APP-10Q-FY2026Q2",
                                           "APP-NOTE-2026-10-20"]  # the run's public additions are mirrored
    assert all("visibility" not in e for e in private)
    dossier = (env.private / "companies" / "APP" / "dossier.md").read_text(encoding="utf-8")
    assert "## 3. Moat\n\nNew moat text." in dossier and "Old moat text" not in dossier
    assert dossier.rstrip().endswith("- v2 2026-10-20: the moat part rewritten.") and "## 1. Business" in dossier
    thesis = yaml.safe_load((env.public / "companies" / "APP" / "thesis.yml").read_text(encoding="utf-8"))
    assert thesis["todo"][-1] == "Synthetic follow-up from the update."
    body = (bundle / "pr_body.md").read_text(encoding="utf-8")
    assert body.startswith("Summary: nothing changes.") and "## Audit findings (04A)\n\n```yaml\n- id: 04A-01" in body
    actions = {(f["output"], f["action"], f["status"]) for f in report["files"]}
    assert {("thesis", "write", "replace"), ("mistakes_entry", "append", "update"), ("pr_body", "pr_body", "new"),
            ("update", "write+front_matter", "new"), ("dossier_changes", "patch", "update")} <= actions
    assert {("sources_additions", "merge", "update")} <= actions
    assert (bundle / runner.PLACEMENT_RECORD).is_file()


def test_below_trust_level_2_the_update_is_staged_until_hq_publishes_it(env, shim):
    bundle = revision(env, level=1)
    report = place(env, bundle)
    staged = env.private / "runs" / "APP" / "2026-10-20-03R" / "staged"
    assert report["routing"]["route"].startswith("staged in owners-office-private:runs/APP/2026-10-20-03R/staged")
    assert "public_branch" not in report and fx.git(env.public, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    assert (staged / "companies" / "APP" / "updates" / "2026-10-20.md").is_file()
    assert "### 2026-10-20 · APP" in (staged / "mistakes.md").read_text(encoding="utf-8")
    assert not (env.public / "companies" / "APP" / "updates" / "2026-10-20.md").exists()
    assert "Old moat text" not in (env.private / "companies" / "APP" / "dossier.md").read_text()  # private: placed
    fx.commit_all(env.private, "staged for review")
    published = place(env, bundle, publish=True)
    assert published["public_branch"] == "pipeline/APP-2026-10-20-03R"
    assert (env.public / "companies" / "APP" / "updates" / "2026-10-20.md").read_bytes() == \
        (staged / "companies" / "APP" / "updates" / "2026-10-20.md").read_bytes()
    assert (bundle / runner.PUBLICATION_RECORD).is_file()


def test_publishing_refuses_what_hq_did_not_review(env, shim):
    bundle = revision(env, level=1)
    with pytest.raises(runner.RunnerError, match="was not staged; place the bundle without --publish first"):
        place(env, bundle, publish=True)
    place(env, bundle)
    staged = env.private / "runs" / "APP" / "2026-10-20-03R" / "staged" / "mistakes.md"
    staged.write_text(staged.read_text(encoding="utf-8") + "\nEdited after staging.\n", encoding="utf-8")
    with pytest.raises(runner.RunnerError, match="differs from the staged copy HQ reviewed"):
        place(env, bundle, publish=True)


def test_a_replaced_file_must_be_the_one_the_update_read(env, shim):
    bundle = revision(env, level=2)
    path = env.public / "companies" / "APP" / "ledger.yml"
    path.write_text(path.read_text(encoding="utf-8") + "# edited by hand\n", encoding="utf-8")
    fx.commit_all(env.public, "hand edit")
    with pytest.raises(runner.RunnerError, match=r"companies/APP/ledger.yml changed after the update read it"):
        place(env, bundle)


def test_a_registered_source_is_never_rewritten(env, shim):
    bundle = revision(env, level=2)
    fx.rewrite_output(bundle, "sources_additions", yaml.safe_dump([
        {"tag": "APP-8K-2026-08-05", "kind": "filing", "title": "a different title", "visibility": "public"}]))
    with pytest.raises(runner.RunnerError, match="source APP-8K-2026-08-05 is registered with different fields"):
        place(env, bundle)


def test_the_pr_body_is_checked_for_h4_wording(env, shim):
    pytest.importorskip("thesis_ci.checks.public")
    bundle = revision(env, level=2)
    fx.rewrite_output(bundle, "pr_body", "Summary: the price target is unchanged.\n")
    with pytest.raises(runner.RunnerError, match="the PR body contains section H4 wording"):
        place(env, bundle)


def test_the_draft_and_the_evaluation_are_never_placed(env, shim):
    draft = draft_chain(env)
    with pytest.raises(runner.RunnerError, match="is the draft; 03R revises it"):
        place(env, draft)
    with pytest.raises(runner.RunnerError, match="deterministic step"):
        place(env, env.private / "runs" / "APP" / "2026-10-20-ci")


def test_dossier_changes_wait_for_a_dossier(env, shim):
    bundle = revision(env, level=2)
    (env.private / "companies" / "APP" / "dossier.md").unlink()
    report = place(env, bundle, check=True)
    (kept,) = [f for f in report["files"] if f["output"] == "dossier_changes"]
    assert kept["path"] == "runs/APP/2026-10-20-03R/dossier_changes.yml"
    assert any("does not exist yet" in w for w in report["warnings"])


def test_patch_add_and_merge_helpers():
    patched, problems = runner.patch_dossier(DOSSIER, [{"section": "business", "text": "## 1. Business\n\nNew."},
                                                       {"section": "ratings", "text": "A"},
                                                       {"section": "runway", "text": "x"}])
    assert "## 1. Business\n\nNew.\n\n## 3. Moat" in patched
    assert problems == ["dossier_changes: ratings is not a part of the dossier",
                        "dossier_changes: the dossier has no part 7 'Runway'"]
    assert runner.add_mistake("# M\n\n## The list\n\n### old\n", MISTAKE) == f"# M\n\n## The list\n\n{MISTAKE}\n### old\n"
    text = "# comment kept\nsources:\n  - tag: A-1\n    kind: filing\n    title: a\n"
    merged, added, problems = runner.merge_sources(text, [{"tag": "A-1", "kind": "filing", "title": "a"},
                                                          {"tag": "B-2", "kind": "web", "title": "b"}])
    assert added == 1 and problems == [] and merged.startswith("# comment kept\n")
    assert [e["tag"] for e in yaml.safe_load(merged)["sources"]] == ["A-1", "B-2"]


# ---------------------------------------------------------------------------------------------------- the event chain


def event(env, **kwargs):
    kwargs.setdefault("run_date", RUN)
    kwargs.setdefault("edgar_gateway", env.gateway)
    kwargs.setdefault("schemas_dir", env.schemas)
    kwargs.setdefault("today", RUN)
    kwargs.setdefault("out", io.StringIO())
    kwargs.setdefault("env", {})
    return chain.run_event("APP", EVENT, roots=env.roots, **kwargs)


def states(result) -> dict[str, str]:
    return {s.stage: s.state for s in result[1]}


def test_a_dry_run_of_a_candidates_event_goes_end_to_end_outside_the_repositories(env, shim, tmp_path):
    set_thesis(env, status="candidate")
    code, result = event(env, dry_run=True, out_root=tmp_path / "dry")
    assert code == 0, [(s.stage, s.state, s.detail) for s in result]
    assert [s.stage for s in result] == ["16B", "ci", "15B", "03-draft", "stop:draft", "16A", "04A", "stop:audit",
                                         "03R", "16A-r2", "04A-r2", "17A", "03R-r2", "17A-r2", "stop:placement",
                                         "place"]
    assert states((code, result)) == {"16B": "ran", "ci": "ran", "15B": "skipped", "03-draft": "ran",
                                      "stop:draft": "passed", "16A": "ran", "04A": "ran", "stop:audit": "passed",
                                      "03R": "ran", "16A-r2": "skipped", "04A-r2": "skipped", "17A": "ran",
                                      "03R-r2": "skipped", "17A-r2": "skipped", "stop:placement": "passed",
                                      "place": "checked"}
    assert next(s.detail for s in result if s.stage == "17A") == "gate decision: hold"
    assert not (env.private / "runs" / "APP").exists() and fx.git(env.public, "status", "--porcelain") == ""
    assert "staged in owners-office-private" in result[-1].detail


def test_the_chain_stops_for_review_and_resumes_without_running_anything_twice(env, shim, monkeypatch):
    set_thesis(env, status="candidate")
    executed: list[str] = []
    real_execute, real_place = runner.execute, runner.place

    def counting_execute(bundle, **kwargs):
        executed.append(Path(bundle).name)
        return real_execute(bundle, **kwargs)

    monkeypatch.setattr(runner, "execute", counting_execute)
    monkeypatch.setattr(runner, "place", lambda bundle, **kw: real_place(bundle, **{**kw, "allow_fake": True}))
    factory = valid_factory(env)
    first = event(env, backend="api", client_factory=factory, lint=False)
    assert first[0] == chain.WAITING and states(first)["stop:draft"] == "waiting"
    assert "--approve draft" in next(s.detail for s in first[1] if s.stage == "stop:draft")
    again = event(env, backend="api", client_factory=factory, lint=False)
    assert again[0] == chain.WAITING and executed == ["2026-10-20-16B", "2026-10-20-03-draft"]
    audit = event(env, backend="api", client_factory=factory, lint=False, approve_stops=["draft"])
    assert states(audit)["stop:audit"] == "waiting" and states(audit)["16A"] == "ran"
    review = yaml.safe_load((env.private / "runs/APP/2026-10-20-03-draft" / chain.REVIEW_RECORD).read_text())
    assert review["stop"] == "draft" and review["approved_at"]
    revise = event(env, backend="api", client_factory=factory, lint=False, approve_stops=["audit"])
    assert states(revise)["stop:placement"] == "waiting" and states(revise)["16A-r2"] == "skipped"
    assert next(s.detail for s in revise[1] if s.stage == "stop:placement").startswith("HQ's gate: hold; review")
    done = event(env, backend="api", client_factory=factory, lint=False, approve_stops=["placement"])
    assert done[0] == 0 and states(done)["place"] == "placed"
    assert "2026-10-20-03R: 9 file(s), staged" in done[1][-1].detail
    final = event(env, backend="api", client_factory=factory, lint=False)
    assert final[0] == 0 and states(final)["place"] == "placed" and final[1][-1].detail == "everything was placed already"
    assert executed == ["2026-10-20-16B", "2026-10-20-03-draft", "2026-10-20-16A", "2026-10-20-04A", "2026-10-20-03R",
                        "2026-10-20-17A-APP"]


def valid_factory(env):
    """A model test double for each bundle: the fake client's placeholders for exactly that bundle's call."""

    def build(bundle: Path) -> fake_client.FakeClient:
        manifest = manifest_of(bundle)
        call, formats = runner.verify_prompts(manifest, env.private)
        context = runner._fake_context(manifest, runner.read_inputs(bundle, manifest))
        return fake_client.FakeClient(fake_client.placeholder_reply(call.outputs, formats, context))

    return build


def test_a_failed_step_stops_the_chain_until_retry(env, shim):
    set_thesis(env, status="candidate")
    bad = lambda bundle: fake_client.FakeClient("no output blocks at all")  # noqa: E731
    first = event(env, backend="api", client_factory=bad, lint=False)
    assert first[0] == 1 and states(first)["16B"] == "failed" and states(first)["ci"] == "not reached"
    again = event(env, backend="api", client_factory=valid_factory(env), lint=False)
    assert states(again)["16B"] == "failed" and "--retry" in again[1][0].detail
    retried = event(env, backend="api", client_factory=valid_factory(env), lint=False, retry=True)
    assert states(retried)["16B"] == "ran" and states(retried)["stop:draft"] == "waiting"


def test_a_holdings_chain_runs_every_step_of_the_prompts_scope(env, shim, tmp_path):
    run_dir = env.private / "runs" / "APP" / "2026-07-01-14Q"
    fx.write_yaml(run_dir / runner.MANIFEST, {"manifest_version": 1, "step": "14Q", "company": "APP", "period": EVENT})
    fx.write_yaml(run_dir / runner.RUN_RECORD, {"status": "succeeded"})
    fx.write_yaml(run_dir / "question_list.yml", [{"id": "Q01", "question": "q", "kind": "pillar", "maps_to": ["P1"]},
                                                  {"id": "Q02", "question": "open", "kind": "open", "maps_to": []}])
    code, result = event(env, dry_run=True, out_root=tmp_path / "dry")
    got = states((code, result))
    assert code == 0, [(s.stage, s.state, s.detail) for s in result]
    assert (got["14T"], got["14A"], got["15B"], got["03-draft"], got["16A"], got["04A"], got["04B-lite"], got["14B"],
            got["03R"], got["17A"]) == ("ran", "ran", "skipped", "ran", "ran", "ran", "ran", "ran", "ran", "ran")
    placed = result[-1].detail
    assert "2026-10-20-14B: 2 file(s)" in placed and "2026-10-20-17A-APP: 5 file(s)" in placed
    assert "14Q" not in placed  # the question list is not a step of the chain


def test_the_event_command_prints_states_not_content(env, shim, tmp_path, monkeypatch, capsys):
    set_thesis(env, status="candidate")
    event(env, dry_run=True, out_root=tmp_path / "warm")  # fills the EDGAR cache the offline command reads
    monkeypatch.setenv(edgar.CACHE_ENV, str(env.gateway.client.cache_dir))
    code = runner.main(["event", "APP", EVENT, "--run-date", RUN.isoformat(), "--dry-run", "--offline", "--out",
                        str(tmp_path / "dry"), "--public-root", str(env.public), "--private-root", str(env.private),
                        "--workspace-root", str(env.workspace), "--schemas-dir", str(env.schemas)])
    printed = "".join(capsys.readouterr())
    assert code == 0 and "chain complete" in printed and "16B             ran" in printed
    assert not [canary for canary in (*fx.CANARIES, fx.CANARY_TEST) if canary in printed]


# ---------------------------------------------------------------------------------------------------- phase B


FORM4 = b"""<?xml version="1.0"?>
<ownershipDocument><documentType>4</documentType><periodOfReport>2026-08-20</periodOfReport>
<reportingOwner><reportingOwnerId><rptOwnerName>Founder Jane</rptOwnerName></reportingOwnerId>
<reportingOwnerRelationship><isDirector>1</isDirector><isOfficer>1</isOfficer><officerTitle>CEO</officerTitle>
</reportingOwnerRelationship></reportingOwner>
<nonDerivativeTable>
<nonDerivativeTransaction><securityTitle><value>Class A Common Stock</value></securityTitle>
<transactionDate><value>2026-08-20</value></transactionDate><transactionCoding><transactionCode>S</transactionCode>
</transactionCoding><transactionAmounts><transactionShares><value>1000</value></transactionShares>
<transactionPricePerShare><value>512.34</value><footnoteId id="F1"/></transactionPricePerShare>
<transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode></transactionAmounts>
<postTransactionAmounts><sharesOwnedFollowingTransaction><value>9000</value></sharesOwnedFollowingTransaction>
</postTransactionAmounts><ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership>
</ownershipNature></nonDerivativeTransaction>
<nonDerivativeTransaction><securityTitle><value>Class A Common Stock</value></securityTitle>
<transactionDate><value>2026-08-20</value></transactionDate><transactionCoding><transactionCode>S</transactionCode>
</transactionCoding><transactionAmounts><transactionShares><value>500</value></transactionShares>
<transactionPricePerShare><value>513.00</value><footnoteId id="F2"/></transactionPricePerShare>
<transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode></transactionAmounts>
<postTransactionAmounts><sharesOwnedFollowingTransaction><value>8500</value></sharesOwnedFollowingTransaction>
</postTransactionAmounts><ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership>
</ownershipNature></nonDerivativeTransaction></nonDerivativeTable>
<footnotes><footnote id="F1">Weighted average price; prices ranged from $511.90 to $512.80.</footnote>
<footnote id="F2">Sold under a Rule 10b5-1 trading plan adopted on 2026-03-01.</footnote></footnotes>
</ownershipDocument>"""


def test_ownership_reports_are_rendered_in_a_few_lines_without_prices():
    text = documents.render_ownership(FORM4)
    assert "Reporting owner: Founder Jane (director, officer, CEO)" in text
    assert "code S (open-market sale); 2 transaction(s), 1,500 shares disposed; owned after 8500; direct" in text
    assert "Footnote F2: Sold under a Rule 10b5-1 trading plan" in text and "1 footnote(s) stating prices left out" in text
    assert "512" not in text and "$" not in text
    assert documents.render_ownership(b"<not xml") is None


TENQ_TEXT = "\n".join([
    "PART I", "Item 1.", "Item 2.", "PART II", "Item 1.", "Item 1A.",  # the table of contents
    "PART I – FINANCIAL INFORMATION", "Item 1. Financial Statements", "Condensed balance sheet",
    "1. Description of Business", "What the company does.", "5. Commitments and Contingencies",
    "A lawsuit is pending.", "6. Subsequent Events", "A deal was signed after the quarter.",
    "ITEM 2. MANAGEMENT'S DISCUSSION AND ANALYSIS", "Revenue grew.", "Margins held.",
    "ITEM 3. MARKET RISK", "Rates.", "PART II – OTHER INFORMATION", "ITEM 1. LEGAL PROCEEDINGS", "See note 5.",
    "ITEM 1A. RISK FACTORS", "Competition is intense.", "Regulation may change.", "ITEM 6. EXHIBITS", "31.1"])


@pytest.mark.parametrize("wanted, kept, dropped", [
    ({"mdna"}, ["Revenue grew.", "Margins held."], ["A lawsuit is pending.", "Competition is intense."]),
    ({"risk_factors", "legal_proceedings"}, ["Competition is intense.", "See note 5."], ["Revenue grew."]),
    ({"mdna", "note:subsequent events"}, ["A deal was signed after the quarter.", "Revenue grew."],
     ["A lawsuit is pending."]),
    ({"note:contingencies"}, ["A lawsuit is pending."], ["Revenue grew.", "A deal was signed"]),
])
def test_sections_named_by_where_are_cut_by_the_filings_own_headings(wanted, kept, dropped):
    text, found, missing = documents.extract_sections(TENQ_TEXT, "10-Q", frozenset(wanted))
    assert missing == [] and sorted(found) == sorted(wanted)
    assert all(k in text for k in kept) and not any(d in text for d in dropped)
    assert text.startswith("--- section: ")


def test_a_section_that_cannot_be_found_keeps_the_whole_filing_and_an_absent_note_is_skipped():
    text, found, missing = documents.extract_sections(TENQ_TEXT, "10-Q", frozenset({"mdna", "business"}))
    assert text == TENQ_TEXT and missing == ["business"]
    no_note = TENQ_TEXT.replace("6. Subsequent Events\nA deal was signed after the quarter.\n", "")
    text, found, missing = documents.extract_sections(no_note, "10-Q", frozenset({"mdna", "note:subsequent events"}))
    assert missing == [] and "Revenue grew." in text and "not in this filing: note: subsequent events" in text
    assert documents.section_requests("the MD&A and subsequent-events notes of the 10-Q and 10-K") == frozenset(
        {"mdna", "note:subsequent events"})
    assert documents.section_requests("20-F Item 3.D risk factors and Item 16I") == frozenset(
        {"risk_factors", "item:3", "item:16I"})
    assert documents.section_requests("8-K Item 5.02 and the 10-K") is None


def holding_draft(env) -> Path:
    """APP (a holding) up to its 03 draft for FY2026Q2: a frozen question list, 16B, ci, 14T, 14A and the draft."""
    run_dir = env.private / "runs" / "APP" / "2026-07-01-14Q"
    fx.write_yaml(run_dir / runner.MANIFEST, {"manifest_version": 1, "step": "14Q", "company": "APP", "period": EVENT})
    fx.write_yaml(run_dir / runner.RUN_RECORD, {"status": "succeeded"})
    fx.write_yaml(run_dir / "question_list.yml", [{"id": "Q01", "question": "q", "kind": "pillar", "maps_to": ["P1"]},
                                                  {"id": "Q02", "question": "open", "kind": "open", "maps_to": []}])
    run(env, "16B")
    evaluate(env)
    run(env, "14T")
    run(env, "14A")
    return run(env, "03-draft")


def test_the_red_team_and_the_divergence_map_get_their_inputs_only(env, shim):
    holding_draft(env)
    red = env.assemble("04B-lite", period=EVENT)
    assert list(inputs_of(red)) == ["update", "filings", "thesis_without_loss_paths"]
    assert manifest_of(red)["omitted"][0]["name"] == "prior_inversion_list"
    assert "permanent_loss_paths" not in read_input(red, "thesis_without_loss_paths")
    assert manifest_of(red)["role"] == "red_team" and manifest_of(red)["variables"]["subject"] == "quarterly update"
    divergence = env.assemble("14B", period=EVENT)
    assert list(inputs_of(divergence)) == ["question_list", "question_answers", "blind_answers",
                                           "unprompted_observations", "filings", "thesis", "dossier", "update"]
    answers = yaml.safe_load(read_input(divergence, "question_answers"))
    assert [a["id"] for a in answers] == ["Q01", "Q02"]
    assert inputs_of(divergence)["blind_answers"]["sources"][0]["run"] == "runs/APP/2026-10-20-14A"


def due_predictions(env) -> None:
    """A FY2026Q2 pre-registration of APP, merged, with a system and an owner item due, and a system forecast in the
    ledger due at the end of the quarter."""
    item = {"statement": "Synthetic statement.", "probability": 0.7, "criterion": "Synthetic criterion.",
            "data_source": "The FY2026Q2 10-Q MD&A", "horizon": "quarter", "resolves_by": "2026-08-31",
            "domain": "digital_advertising", "added_by": "system"}
    fx.write_yaml(env.public / "companies" / "APP" / "prereg" / f"{EVENT}.yml", {
        "company": "APP", "event": {"period": EVENT, "expected_release": "2026-08-05", "form": "8-K",
                                    "placeholder": False},
        "deadline": "2026-08-04T23:59:59-04:00", "author": "system", "horizon": "quarter",
        "items": [{"id": f"APP-{EVENT}-1", **item},
                  {"id": f"APP-{EVENT}-2", **item, "resolves_by": "2027-12-31", "horizon": "18m"}]})
    fx.write_yaml(env.public / "companies" / "APP" / "prereg" / f"{EVENT}-owner.yml", {
        "company": "APP", "author": "owner", "items": [{"id": f"APP-{EVENT}-3", **item, "probability": 0.2,
                                                        "added_by": "owner"}]})
    ledger = yaml.safe_load((env.public / "companies" / "APP" / "ledger.yml").read_text(encoding="utf-8"))
    ledger["entries"].append({"id": "APP-S-2026-01", "side": "system", "kind": "prediction", "probability": 0.6,
                              "statement": "Synthetic forecast.", "made_at": "2026-05-06", "due": "2026-Q2",
                              "source": "APP-8K-2026-08-05#EX-99.1", "status": "pending", "note": "Criterion."})
    fx.write_yaml(env.public / "companies" / "APP" / "ledger.yml", ledger)
    fx.commit_all(env.public, "due predictions")


def test_the_settler_sees_neither_probabilities_nor_authors(env, shim):
    due_predictions(env)
    run(env, "16B")
    evaluate(env)
    bundle = env.assemble("15B", period=EVENT)
    assert list(inputs_of(bundle)) == ["event", "items_blind", "filings", "metric_values", "ledger_due"]
    items = yaml.safe_load(read_input(bundle, "items_blind"))["items"]
    assert [i["id"] for i in items] == [f"APP-{EVENT}-1", f"APP-{EVENT}-3"]  # -2 resolves in 2027
    assert all(set(i) == set(registry.BLIND_ITEM_FIELDS) for i in items)
    (entry,) = yaml.safe_load(read_input(bundle, "ledger_due"))["entries"]
    assert entry["settle_as"] == "binary" and "side" not in entry and "probability" not in entry
    everything = "".join((bundle / e["file"]).read_text(encoding="utf-8") for e in manifest_of(bundle)["inputs"])
    assert "probability" not in everything and "added_by" not in everything and "owner" not in everything.lower()
    values = yaml.safe_load(read_input(bundle, "metric_values"))
    assert values["readings"][0]["metric"] == "revenue_yoy" and "extracted_16B" in values
    filings = read_input(bundle, "filings")
    assert "The documents the due items' data_source names" in filings and "[src:APP-10Q-FY2026Q2]" in filings


def test_settlements_are_written_as_the_pipelines_settlement_files(env, shim):
    due_predictions(env)
    run(env, "16B")
    evaluate(env)
    bundle = run(env, "15B")
    fx.rewrite_output(bundle, "prereg_settlement", yaml.safe_dump([
        {"id": f"APP-{EVENT}-1", "outcome": "happened", "values": {"growth": 12}, "calculation": "12 >= 10",
         "evidence": "Revenue grew 12% [src:APP-8K-2026-08-05#EX-99.1].", "reasoning": "The criterion holds."},
        {"id": f"APP-{EVENT}-3", "outcome": "undetermined", "evidence": None, "reasoning": "The data is missing."}],
        sort_keys=False))
    report = place(env, bundle)
    path = env.public / "companies" / "APP" / "prereg" / f"{EVENT}.settlement.yml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert (data["company"], data["period"], data["accession"]) == ("APP", EVENT, "0001751008-26-000057")
    assert data["merged_at"] and data["ots_proof"] is None
    results = {r["id"]: r for r in data["results"]}
    assert results[f"APP-{EVENT}-1"] == {"id": f"APP-{EVENT}-1", "outcome": "happened", "values": {"growth": 12},
                                         "calculation": "12 >= 10", "evidence": "Revenue grew 12% "
                                         "[src:APP-8K-2026-08-05#EX-99.1].", "reasoning": "The criterion holds.",
                                         "source": "APP-8K-2026-08-05#EX-99.1", "settled_at": "2026-10-20"}
    assert report["public_branch"] == "pipeline/APP-2026-10-20-15B" and report["routing"] is None
    assert registry.settled_predictions(env.public)  # the calibration record reads the new file
    fx.commit_all(env.public, "settlement")
    again = run(env, "15B", run_date=dt.date(2026, 10, 27), today=dt.date(2026, 10, 27))
    fx.rewrite_output(again, "prereg_settlement", yaml.safe_dump([
        {"id": f"APP-{EVENT}-1", "outcome": "not_happened", "evidence": None, "reasoning": "x"}]))
    with pytest.raises(runner.RunnerError, match="was settled happened before; a settlement is never rewritten"):
        place(env, again)


def test_hq_gets_the_revision_the_audit_and_what_the_gate_checks(env, shim):
    set_thesis(env, status="candidate")
    draft_chain(env)
    run(env, "16A")
    audit = run(env, "04A")
    fx.rewrite_output(audit, "findings", yaml.safe_dump([
        {"id": "04A-01", "group": "must fix", "type": "fact_error", "location": "2", "quote": "q", "evidence": "e",
         "fix": "f"}], sort_keys=False))
    revision = run(env, "03R")
    fx.rewrite_output(revision, "revision_notes", yaml.safe_dump([{"finding_id": "04A-01", "action": "not fixed",
                                                                   "how": "the evidence is disputed"}]))
    bundle = env.assemble("17A", "APP", EVENT, run_date=RUN)
    assert bundle == env.private / "runs" / "hq" / "2026-10-20-17A-APP"
    manifest = manifest_of(bundle)
    assert (manifest["role"], manifest["company"], manifest["scope"]) == ("hq_capital_allocator", "APP", "hq")
    entries = inputs_of(bundle)
    assert entries["inversion_list"]["empty"] and entries["divergence_map"]["empty"]
    gate = yaml.safe_load(read_input(bundle, "gate_rules"))
    assert gate["gate"]["blocking"] if gate["gate"] else True
    assert gate["pipeline_checks"]["open_04A_must_fix"] == [{"finding": "04A-01", "round": 1,
                                                             "revision_notes": "not fixed"}]
    assert gate["pipeline_checks"]["trust_level"] == 1
    assert yaml.safe_load(read_input(bundle, "trust_level"))["trust_level"] == 1
    raised = yaml.safe_load(read_input(bundle, "questions"))["raised"]
    assert {r["step"] for r in raised} >= {"03-draft", "04A", "03R"}
    assert "===== output: revision_notes" in read_input(bundle, "update_outputs")
    assert manifest["context"]["run_dir"] == "2026-10-20-17A-APP"


def test_the_second_audit_round_reads_only_what_the_revision_changed(env, shim):
    set_thesis(env, status="candidate")
    draft = draft_chain(env)
    run(env, "16A")
    run(env, "04A")
    revision = run(env, "03R")
    before = (draft / "outputs" / "update.md").read_text(encoding="utf-8")
    fx.rewrite_output(revision, "update", before.replace("DRY-RUN placeholder", "Installs grew to 1,234 million. "
                                                                               "DRY-RUN placeholder"))
    product = env.assemble("16A", period=EVENT, round_=2)
    assert product.name == "2026-10-20-16A-r2" and manifest_of(product)["round"] == 2
    text = read_input(product, "product")
    assert "===== changes: update (draft -> revision) =====" in text and "+" in text
    assert "changes: thesis" not in text and "generated_by" not in text
    env.execute(product)
    audit = env.assemble("04A", period=EVENT, round_=2)
    assert inputs_of(audit)["fact_table"]["sources"][0]["run"] == "runs/APP/2026-10-20-16A-r2"
    with pytest.raises(runner.RunnerError, match="15B has no round 2"):
        env.assemble("15B", period=EVENT, round_=2)
    with pytest.raises(runner.RunnerError, match="16A has no round 3"):
        env.assemble("16A", period=EVENT, round_=3)


def scripted_factory(env, replies: dict[str, dict[str, str]]):
    """valid_factory, with some outputs of some bundles replaced (bundle directory name -> output -> text)."""
    base = valid_factory(env)

    def build(bundle: Path) -> fake_client.FakeClient:
        client = base(bundle)
        for name, text in replies.get(bundle.name, {}).items():
            client.reply = re.sub(rf'(<output name="{name}">\n).*?(\n</output>)',
                                  lambda m: m.group(1) + text.rstrip("\n") + m.group(2), client.reply, flags=re.S)
        return client

    return build


RETURN = {"gate_decision": "decision: return\nreasons: [the must-fix finding is open]\n",
          "returns": "- issue: fix finding 04A-01\n"}


def test_hq_returns_the_update_once_then_the_loop_stops(env, shim, monkeypatch):
    set_thesis(env, status="candidate")
    real_place = runner.place
    monkeypatch.setattr(runner, "place", lambda bundle, **kw: real_place(bundle, **{**kw, "allow_fake": True}))
    factory = scripted_factory(env, {"2026-10-20-17A-APP": RETURN})
    event(env, backend="api", client_factory=factory, lint=False, approve_stops=["draft"])
    code, result = event(env, backend="api", client_factory=factory, lint=False, approve_stops=["audit"])
    got = states((code, result))
    assert (got["17A"], got["03R-r2"], got["17A-r2"], got["stop:placement"]) == ("ran", "ran", "ran", "waiting")
    revision = env.private / "runs" / "APP" / "2026-10-20-03R-r2"
    assert list(inputs_of(revision)) == ["draft_outputs", "findings_04A", "returns_17A", "sources"]
    assert "the revision runs/APP/2026-10-20-03R" in read_input(revision, "draft_outputs")
    assert "fix finding 04A-01" in read_input(revision, "returns_17A")
    gate2 = env.private / "runs" / "hq" / "2026-10-20-17A-APP-r2"
    assert "runs/APP/2026-10-20-03R-r2" in read_input(gate2, "update_outputs")

    env2 = fx.make_env(env.workspace.parent / "second")
    evaluate_shim.install(monkeypatch)
    set_thesis(env2, status="candidate")
    both = scripted_factory(env2, {"2026-10-20-17A-APP": RETURN, "2026-10-20-17A-APP-r2": RETURN})
    for stop in ("draft", "audit"):
        event(env2, backend="api", client_factory=both, lint=False, approve_stops=[stop])
    code, result = event(env2, backend="api", client_factory=both, lint=False, approve_stops=["placement"])
    assert code == 1 and states((code, result))["stop:placement"] == "failed"
    assert "returned the update again in round 2" in next(s.detail for s in result if s.stage == "stop:placement")
    assert not (env2.private / "runs" / "APP" / "2026-10-20-03R-r2" / runner.PLACEMENT_RECORD).exists()


def test_the_pr_body_carries_every_audit_round(env, shim):
    bundle = revision(env, level=2)
    second = env.private / "runs" / "APP" / "2026-10-20-04A-r2"
    fx.write_yaml(second / runner.MANIFEST, {"manifest_version": 1, "step": "04A", "company": "APP", "period": EVENT,
                                             "round": 2})
    fx.write_yaml(second / runner.RUN_RECORD, {"status": "succeeded"})
    fx.write(second / "outputs" / "findings.yml", "- id: 04A-2-01\n  group: no change\n  type: null\n")
    place(env, bundle)
    body = (bundle / "pr_body.md").read_text(encoding="utf-8")
    assert "## Audit findings (04A)" in body and "## Audit findings (04A, round 2)\n\n```yaml\n- id: 04A-2-01" in body
