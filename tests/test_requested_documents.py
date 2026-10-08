"""The documents HQ asks the pipeline to supply (requested_documents; supply.yml next to HQ's rulings; decisions/0031).

No network: EDGAR answers come from the runner fixtures' fake transport (tests/runner_fixtures.py) plus a synthetic
other issuer, "PGR", with its submissions, filings and SEC's ticker table.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
import yaml

from pipeline import edgar, llm, registry, runner
from tests import runner_fixtures as fx

PGR_CIK = "0000080661"
PGR_NAME = "PROGRESSIVE CORP/OH/"
PGR_SUBMISSIONS_URL = f"{edgar.DATA_BASE}/submissions/CIK{PGR_CIK}.json"
PGR_FILINGS = [  # accession, form, filed, report date, items, primary document
    ("0000080661-26-000177", "10-Q", "2026-05-04", "2026-03-31", "", "pgr-20260331.htm"),
    ("0000080661-26-000250", "8-K", "2026-07-15", "2026-07-15", "2.02,9.01", "pgr-20260715.htm"),
    ("0000080661-26-000308", "10-Q", "2026-08-03", "2026-06-30", "", "pgr-20260630.htm"),
    ("0000080661-26-000400", "10-Q", "2026-11-03", "2026-09-30", "", "pgr-20260930.htm"),
]
TICKERS = {"0": {"cik_str": 80661, "ticker": "PGR", "title": "PROGRESSIVE CORP"},
           "1": {"cik_str": 1067983, "ticker": "BRK-B", "title": "BERKSHIRE HATHAWAY INC"},
           "2": {"cik_str": 1067983, "ticker": "BRK-A", "title": "BERKSHIRE HATHAWAY INC"},
           "3": {"cik_str": 1751008, "ticker": "APP", "title": "AppLovin Corp"},
           "4": {"cik_str": 111, "ticker": "DUO-A", "title": "One issuer"},
           "5": {"cik_str": 222, "ticker": "DUO-B", "title": "Another issuer"}}
Q2_LINES = ["FORM 10-Q", "The Progressive Corporation", "Revenues grew in the quarter.", "36",
            "Policies in force", "Personal auto 27,932 thousand at June 30, 2026, against 25,668 thousand.", "37",
            "Note 4. Investments", "Fixed maturities of 75,000 million.", "Note 5. Debt", "Debt of 6,900 million.",
            "38"]
Q1_LINES = ["FORM 10-Q", *[f"Line {n} of the first-quarter report, with nothing in particular." for n in range(1, 301)]]


def html(lines: list[str]) -> bytes:
    return ("<html><body>" + "".join(f"<p>{line}</p>" for line in lines) + "</body></html>").encode()


def folder(accession: str) -> str:
    return f"{edgar.ARCHIVES_BASE}/80661/{accession.replace('-', '')}"


def pgr_answers() -> dict[str, bytes]:
    keys = ("accessionNumber", "form", "filingDate", "reportDate", "items", "primaryDocument")
    recent = {key: [row[i] for row in PGR_FILINGS] for i, key in enumerate(keys)}
    submissions = {"cik": "80661", "name": PGR_NAME, "tickers": ["PGR"], "fiscalYearEnd": "1231",
                   "entityType": "operating", "filings": {"recent": recent, "files": []}}
    eight_k = folder("0000080661-26-000250")
    return {
        PGR_SUBMISSIONS_URL: json.dumps(submissions).encode(),
        edgar.COMPANY_TICKERS_URL: json.dumps(TICKERS).encode(),
        f"{folder('0000080661-26-000177')}/pgr-20260331.htm": html(Q1_LINES),
        f"{folder('0000080661-26-000308')}/pgr-20260630.htm": html(Q2_LINES),
        f"{folder('0000080661-26-000400')}/pgr-20260930.htm": html(["FORM 10-Q", "The third quarter."]),
        f"{eight_k}/index.json": json.dumps({"directory": {"item": [{"name": "pgr-20260715.htm"},
                                                                    {"name": "ex99-1.htm"}]}}).encode(),
        f"{eight_k}/pgr-20260715.htm": html(["FORM 8-K", "Item 2.02 Results of Operations."]),
        f"{eight_k}/ex99-1.htm": html(["Progressive Reports June Results", "Net premiums written grew 9%."]),
    }


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """No real .env or EDGAR cache; the network is off."""
    monkeypatch.delenv(edgar.UA_ENV, raising=False)
    monkeypatch.setenv(edgar.ENV_FILE_ENV, str(tmp_path / "missing.env"))
    monkeypatch.setenv(edgar.CACHE_ENV, str(tmp_path / "default-edgar-cache"))

    def no_network(*args, **kwargs):
        raise AssertionError("these tests never use the network")

    monkeypatch.setattr(edgar, "UrllibTransport", no_network)


@pytest.fixture
def env(tmp_path):
    env = fx.make_env(tmp_path)
    env.gateway.client._transport.responses.update(pgr_answers())
    return env


def calls(env) -> list[str]:
    return env.gateway.client._transport.calls


def context(env, step: str = "01C", *, period: str = "FY2026Q2", run_date: dt.date = dt.date(2026, 9, 30),
            round_: int = 1) -> registry.RunContext:
    return registry.RunContext(step=registry.STEPS[step], company="APP", period=period, run_date=run_date,
                               public_root=env.public, private_root=env.private, workspace_root=env.workspace,
                               call=None, formats={}, edgar=env.gateway, round=round_)


def succeeded_run(env, rel: str, manifest: dict) -> None:
    fx.write_yaml(env.private / rel / registry.MANIFEST_NAME, {"bundle": rel, **manifest})
    fx.write_yaml(env.private / rel / registry.RUN_RECORD_NAME, {"status": "succeeded"})


def build(env, supply: list | str, step: str = "01C", folder_name: str = "2026-09-29-17A-APP",
          **kwargs) -> registry.BuiltInput:
    """requested_documents of APP's FY2026Q2 build (first draft 2026-09-28) with this supply list."""
    succeeded_run(env, "runs/APP/2026-09-28-01A", {"step": "01A", "company": "APP", "period": "FY2026Q2"})
    path = env.private / "runs" / "hq" / folder_name / registry.SUPPLY_FILE
    if isinstance(supply, str):
        fx.write(path, supply)
    else:
        fx.write_yaml(path, supply)
    return registry.INPUTS["requested_documents"](context(env, step, **kwargs), "requested_documents")


def register_privately(env, *entries: dict) -> None:
    path = env.private / "companies" / "APP" / "sources.yml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    fx.write_yaml(path, {"sources": [*data["sources"], *entries]})


def edgar_sources(built: registry.BuiltInput) -> dict[str, dict]:
    return {s["tag"]: s for s in built.sources if s["kind"] == "edgar"}


# ---------------------------------------------------------------------------------------------------- wiring


@pytest.mark.parametrize("step", ["01A", "01C", "04C", "02"])
def test_the_valuation_its_review_the_revised_dossier_and_the_report_may_be_given_requested_documents(env, step):
    spec = registry.STEPS[step]
    call = llm.prompt_part(llm.load_prompt(spec.prompt_id, env.private / "prompts"), spec.part, mode=spec.mode)
    assert ("requested_documents", False) in call.inputs
    llm.check_inputs(call, llm.role_definition(call.role, fx.REPO_ROOT), [name for name, _ in call.inputs])


# ---------------------------------------------------------------------------------------------------- the window


def test_the_supply_lists_read_are_those_of_the_window_of_hqs_rulings(env):
    """One selection for both inputs (hq_build_files): the build of the step's own period, from its first draft until
    the next build's first draft; a later period without a build reads the latest build begun by the run date."""
    succeeded_run(env, "runs/APP/2026-09-28-01A", {"step": "01A", "company": "APP", "period": "FY2026Q2"})
    succeeded_run(env, "runs/APP/2026-11-05-01A", {"step": "01A", "company": "APP", "period": "FY2026Q3"})
    for name in ("2026-09-01-17A-APP", "2026-09-29-17A-APP", "2026-09-30-17A-APP-r2", "2026-11-07-17A-APP"):
        fx.write(env.private / "runs" / "hq" / name / "rulings.md", f"Rulings of {name}.\n")
        fx.write_yaml(env.private / "runs" / "hq" / name / registry.SUPPLY_FILE,
                      [{"tag": "APP-10Q-FY2026Q2", "why": f"asked in {name}"}])
    fx.write_yaml(env.private / "runs" / "hq" / "2026-09-29-17A-APPX" / registry.SUPPLY_FILE,
                  [{"tag": "APP-10K-FY2025", "why": "another company's list"}])
    cases = [(context(env, step, run_date=day), ["2026-09-29-17A-APP", "2026-09-30-17A-APP-r2"])
             for step in ("01C", "04C", "02") for day in (dt.date(2026, 9, 27), dt.date(2026, 11, 20))]
    cases += [(context(env, "01A", round_=2), ["2026-09-29-17A-APP", "2026-09-30-17A-APP-r2"]),
              (context(env, "01C", period="FY2026Q3", run_date=dt.date(2026, 11, 20)), ["2026-11-07-17A-APP"]),
              (context(env, "02", period="FY2026Q4", run_date=dt.date(2026, 11, 20)), ["2026-11-07-17A-APP"])]
    for ctx, want in cases:
        rulings = registry.INPUTS["hq_rulings"](ctx, "hq_rulings")
        requested = registry.INPUTS["requested_documents"](ctx, "requested_documents")
        assert [p.parent.name for p in registry.hq_build_files(ctx, "APP", registry.SUPPLY_FILE)] == want
        assert [s["path"].split("/")[2] for s in rulings.sources] == want
        assert [s["path"].split("/")[2] for s in requested.sources if s["kind"] == "repo_file"] == want
        assert list(edgar_sources(requested)) == ["APP-10Q-FY2026Q2"]  # a tag both lists name is supplied once
        assert f"asked for by HQ (runs/hq/{want[0]}/supply.yml): asked in {want[0]}" in requested.text


def test_the_first_draft_and_a_build_without_a_supply_list_are_given_no_requested_documents(env):
    with pytest.raises(registry.Omit, match="first draft"):
        registry.INPUTS["requested_documents"](context(env, "01A"), "requested_documents")
    succeeded_run(env, "runs/APP/2026-09-28-01A", {"step": "01A", "company": "APP", "period": "FY2026Q2"})
    fx.write(env.private / "runs" / "hq" / "2026-09-29-17A-APP" / "rulings.md", "R1.\n")
    with pytest.raises(registry.Omit, match="no supply.yml beside its rulings"):
        registry.INPUTS["requested_documents"](context(env), "requested_documents")
    with pytest.raises(registry.Omit, match="names no document: .*not a YAML list"):
        build(env, "tag: APP-10Q-FY2026Q2\n")
    with pytest.raises(registry.Omit, match="names no document: .*entry 1: it has no tag"):
        build(env, [{"why": "a request without its tag"}])
    assert calls(env) == []  # nothing was looked up


# ---------------------------------------------------------------------------------------------------- resolution


def test_a_registered_tag_is_resolved_through_the_sources_yml_another_issuers_filing_too(env):
    """BRK's sources.yml registers PGR-10Q-FY2026Q2 with its issuer's CIK and accession: no ticker lookup, and the
    period is read in the issuer's own calendar. The public sources.yml registers APP's own results 8-K."""
    register_privately(env, {"tag": "PGR-10Q-FY2026Q2", "kind": "filing", "title": "Progressive Q2 10-Q",
                             "issuer_cik": PGR_CIK, "form": "10-Q", "accession": "0000080661-26-000308"},
                       {"tag": "PGR-10Q-FY2026Q1", "kind": "filing", "title": "Progressive Q1 10-Q, by its URL",
                        "url": f"{folder('0000080661-26-000177')}/0000080661-26-000177-index.htm"})
    built = build(env, [{"tag": "PGR-10Q-FY2026Q2", "why": "policies in force for the comparison"},
                        {"tag": "APP-8K-2026-08-05#EX99.1", "why": "the results release"},
                        {"tag": "PGR-10Q-FY2026Q1", "why": "the issuer and the filing from the URL alone"}])
    assert edgar.COMPANY_TICKERS_URL not in calls(env)
    sources = edgar_sources(built)
    assert list(sources) == ["PGR-10Q-FY2026Q2", "APP-8K-2026-08-05#EX-99.1", "PGR-10Q-FY2026Q1"]
    assert (sources["PGR-10Q-FY2026Q1"]["accession"], sources["PGR-10Q-FY2026Q1"]["issuer_cik"]) == \
           ("0000080661-26-000177", PGR_CIK)
    pgr = sources["PGR-10Q-FY2026Q2"]
    assert {k: pgr[k] for k in ("form", "accession", "issuer_cik", "filed", "document", "registered", "period")} == {
        "form": "10-Q", "accession": "0000080661-26-000308", "issuer_cik": PGR_CIK, "filed": "2026-08-03",
        "document": "pgr-20260630.htm", "registered": True, "period": "FY2026Q2"}
    assert pgr["url"] == f"{folder('0000080661-26-000308')}/pgr-20260630.htm"
    assert pgr["title"] == f"{PGR_NAME} 10-Q for FY2026Q2 (period ended 2026-06-30)"
    assert (f"===== [src:PGR-10Q-FY2026Q2] {PGR_NAME} 10-Q for FY2026Q2 (period ended 2026-06-30) | accession "
            "0000080661-26-000308 | filed 2026-08-03 | pgr-20260630.htm =====") in built.text
    assert "Personal auto 27,932 thousand" in built.text and "the whole document" in pgr["note"]
    release = sources["APP-8K-2026-08-05#EX-99.1"]
    assert release["url"] == fx.EX991_URL and release["issuer_cik"] == fx.APP_CIK and release["registered"]
    assert release["title"] == "AppLovin Corp 8-K filed 2026-08-05" and "period" not in release
    assert fx.CANARY_FILING in built.text and built.note == "3 of 3 requested document(s) supplied"


def test_an_unregistered_tag_is_resolved_by_its_ticker_and_the_issuers_submissions(env):
    """Another issuer's CIK comes from SEC's company_tickers.json; a periodic report is matched by form and fiscal
    period, a current report by its filing date. The company's own tag needs no ticker lookup."""
    built = build(env, [{"tag": "APP-10Q-FY2026Q2", "why": "own"}, {"tag": "PGR-10Q-FY2026Q1", "why": "periodic"},
                        {"tag": "PGR-8K-2026-07-15#EX-99.1", "why": "current"}])
    sources = edgar_sources(built)
    assert [(s["accession"], s["issuer_cik"], s["registered"]) for s in sources.values()] == [
        ("0001751008-26-000059", fx.APP_CIK, False), ("0000080661-26-000177", PGR_CIK, False),
        ("0000080661-26-000250", PGR_CIK, False)]
    assert sources["APP-10Q-FY2026Q2"]["url"] == fx.TENQ_URL and sources["PGR-10Q-FY2026Q1"]["period"] == "FY2026Q1"
    assert sources["PGR-8K-2026-07-15#EX-99.1"]["document"] == "ex99-1.htm"
    assert "Net premiums written grew 9%." in built.text and "Item 2.02 Results" not in built.text
    assert calls(env).count(edgar.COMPANY_TICKERS_URL) == 1 and calls(env).count(PGR_SUBMISSIONS_URL) == 1


def test_a_ticker_names_its_issuer_with_or_without_its_share_class():
    class Client:
        ttl = 3600.0

        def get_json(self, url, max_age=None):
            assert url == edgar.COMPANY_TICKERS_URL and max_age == self.ttl
            return TICKERS

    assert edgar.cik_for_ticker("PGR", client=Client()) == PGR_CIK
    assert edgar.cik_for_ticker("BRK", client=Client()) == edgar.cik_for_ticker("BRK.B", client=Client()) == \
        "0001067983"
    with pytest.raises(edgar.EdgarDataError, match="lists DUO under 2 issuers"):
        edgar.cik_for_ticker("DUO", client=Client())
    with pytest.raises(edgar.EdgarDataError, match="lists no ticker ZZZZ"):
        edgar.cik_for_ticker("ZZZZ", client=Client())


# ---------------------------------------------------------------------------------------------------- the excerpt


def test_a_locator_cuts_the_document_to_the_page_or_note_it_names(env):
    built = build(env, [{"tag": "PGR-10Q-FY2026Q2#p37", "why": "policies in force"},
                        {"tag": "PGR-10Q-FY2026Q2#Note4", "why": "investments"},
                        {"tag": "PGR-10Q-FY2026Q2#p99", "why": "a page it does not have"}])
    sources = edgar_sources(built)
    assert list(sources) == ["PGR-10Q-FY2026Q2#p37", "PGR-10Q-FY2026Q2#Note4", "PGR-10Q-FY2026Q2#p99"]
    blocks = built.text.split("===== [src:")[1:]
    page, note, whole = (block.split("\n\n", 1)[1].strip() for block in blocks)
    assert page == "Policies in force\nPersonal auto 27,932 thousand at June 30, 2026, against 25,668 thousand."
    assert note == "Note 4. Investments\nFixed maturities of 75,000 million."
    assert whole.startswith("FORM 10-Q") and whole.endswith("38")
    assert "the part #p37 names (" in sources["PGR-10Q-FY2026Q2#p37"]["note"]
    assert "the whole document: #p99 was not found by its page footers or headings" in \
           sources["PGR-10Q-FY2026Q2#p99"]["note"]
    assert calls(env).count(f"{folder('0000080661-26-000308')}/pgr-20260630.htm") == 1  # fetched once


def test_a_berkshire_note_headed_by_its_number_in_parentheses_is_found():
    """Berkshire Hathaway's 10-K heads each note "(4)" on a line of its own, its title on the next, and repeats the
    heading atop each page the note continues on; a table's footnote "(1)" is followed by a sentence, not a title."""
    from pipeline import documents
    doc = "\n".join([
        "Item 2. Properties", "(1)", "The estimated increase (decrease) is after income taxes.",
        "NOTES TO CONSOLIDATED FINANCIAL STATEMENTS", "(1)", "Significant accounting policies and practices",
        "Policies text.", "K-70", "(3)", "Investments in fixed maturity securities", "Fixed text.",
        "(4)", "Investments in equity securities", "Apple 61,000.", "K-80",
        "Notes to Consolidated Financial Statements", "(4)", "Investments in equity securities",
        "American Express 151.6 million shares.",
        "(5)", "Equity method investments", "Kraft Heinz 3,500.", "K-81"])
    note4 = documents.locator_section(doc, "Note4")
    assert note4.startswith("(4)\nInvestments in equity securities\nApple 61,000.") and "Kraft Heinz" not in note4
    assert note4.endswith("American Express 151.6 million shares.")
    assert documents.locator_section(doc, "Note1").startswith("(1)\nSignificant accounting policies")


def test_each_item_and_all_items_together_stay_within_the_caps_and_a_cut_says_where(env, monkeypatch):
    """The first document is cut at the item cap, the second at what the first left of the total, which fills it: the
    third is not supplied (nor fetched). Nor is a document when less than the minimum is left."""
    monkeypatch.setattr(registry, "REQUESTED_ITEM_TOKENS", 100)
    monkeypatch.setattr(registry, "REQUESTED_TOTAL_TOKENS", 150)
    monkeypatch.setattr(registry, "REQUESTED_MIN_TOKENS", 20)
    built = build(env, [{"tag": "PGR-10Q-FY2026Q1", "why": "long"}, {"tag": "PGR-10Q-FY2026Q2", "why": "short"},
                        {"tag": "APP-10Q-FY2026Q2", "why": "after the total"}])
    first, second = edgar_sources(built).values()
    kept = "\n".join(Q1_LINES[:4])  # the lines within 100 tokens
    assert f"cut by the pipeline after line 4 of 301 (character {len(kept)} of " in first["note"]
    assert "to stay within 100 tokens; the rest is at the url above" in first["note"]
    assert f"{kept}\n[cut by the pipeline here, after line 4 of 301 (character {len(kept)} of " in built.text
    assert "cut by the pipeline after line 8 of 12 (character 184 of " in second["note"]
    assert f"to stay within {150 - registry.slicing.estimate_tokens(kept)} tokens" in second["note"]
    assert "not supplied: APP-10Q-FY2026Q2: the documents listed before it fill the input's cap of 150 tokens" in \
           built.text
    assert built.note == "2 of 3 requested document(s) supplied, 2 cut; 1 not supplied"
    assert fx.TENQ_URL not in calls(env)
    monkeypatch.setattr(registry, "REQUESTED_MIN_TOKENS", 80)  # what the first leaves is too little for a second
    built = build(env, [{"tag": "PGR-10Q-FY2026Q1"}, {"tag": "PGR-10Q-FY2026Q2"}])
    assert list(edgar_sources(built)) == ["PGR-10Q-FY2026Q1"]
    assert "not supplied: PGR-10Q-FY2026Q2: the documents listed before it fill the input's cap" in built.text


def test_a_document_is_cut_at_a_line_end_within_its_budget():
    text = "\n".join(f"line {n:03d} of the text" for n in range(100))
    cut, where = registry.cut_to_tokens(text, 50)
    assert where == f"after line 6 of 100 (character {len(cut):,} of {len(text):,})"
    assert cut.endswith("line 005 of the text") and registry.slicing.estimate_tokens(cut) <= 50
    assert registry.cut_to_tokens(text, 10_000) == (text, None)
    one_line = "head\n" + "x" * 1_000  # a line longer than the budget is cut inside: 262 characters, 100 tokens
    assert registry.cut_to_tokens(one_line, 100) == (one_line[:262], "after line 2 of 2 (character 262 of 1,005)")


# ---------------------------------------------------------------------------------------------------- not supplied


def test_what_cannot_be_supplied_is_listed_with_the_reason_and_the_rest_is_supplied(env):
    register_privately(env, {"tag": "APP-LTR-2025", "kind": "letter", "title": "Letter to shareholders",
                             "date": "2026-02-19"},
                       {"tag": "PGR-10K-FY2024", "kind": "filing", "title": "Progressive 10-K 2024",
                        "issuer_cik": PGR_CIK, "accession": "0000080661-25-999999"})
    built = build(env, [{"tag": "APP-10Q-FY2026Q2", "why": "supplied"}, {"tag": "ZZZZ-10K-FY2025"},
                        {"tag": "PGR-10Q-FY2026Q3"}, {"tag": "PGR-10K-FY2025"}, {"tag": "APP-LTR-2025"},
                        {"tag": "PGR-10K-FY2024"}, {"tag": "PGR-8K-2026-07-15#EX-99.2"}, {"tag": "not-a-tag"},
                        {"why": "no tag"}])
    header = built.text.split("=====", 1)[0]
    for line in [
        "not supplied: runs/hq/2026-09-29-17A-APP/supply.yml entry 9: it has no tag",
        "not supplied: ZZZZ-10K-FY2025: SEC's company_tickers.json lists no ticker ZZZZ",
        "not supplied: PGR-10Q-FY2026Q3: filed on 2026-11-03 (accession 0000080661-26-000400), after the run date "
        "2026-09-30",
        f"not supplied: PGR-10K-FY2025: no matching filing on EDGAR (CIK {PGR_CIK}, {PGR_NAME})",
        "not supplied: APP-LTR-2025: registered as kind: letter, not an EDGAR filing; the pipeline fetches EDGAR "
        "filings only",
        f"not supplied: PGR-10K-FY2024: EDGAR has no accession number 0000080661-25-999999 among the filings of CIK "
        f"{PGR_CIK}",
        "not supplied: PGR-8K-2026-07-15#EX-99.2: the filing 0000080661-26-000250 has no EX-99.2 on EDGAR",
        "not supplied: not-a-tag: not-a-tag is not a source tag",
    ]:
        assert line in header, line
    assert list(edgar_sources(built)) == ["APP-10Q-FY2026Q2"]
    assert built.note == "1 of 8 requested document(s) supplied; 8 not supplied"
    assert f"{folder('0000080661-26-000400')}/pgr-20260930.htm" not in calls(env)  # never fetched


def test_an_input_none_of_whose_documents_can_be_supplied_is_omitted_with_the_reasons(env):
    with pytest.raises(registry.Omit, match=r"none of the 2 document\(s\) HQ asked for could be supplied: "
                                            r"ZZZZ-10K-FY2025: SEC's company_tickers.json lists no ticker ZZZZ; "
                                            r"PGR-10K-FY2025: no matching filing"):
        build(env, [{"tag": "ZZZZ-10K-FY2025"}, {"tag": "PGR-10K-FY2025"}])


# ---------------------------------------------------------------------------------------------------- placement


def test_placement_registers_a_cited_requested_document_of_another_issuer(env):
    """The run's manifest records the requested document's source entry; when a placed file cites it, placement
    registers it in the sources.yml with its issuer's CIK, EDGAR's title and the period (runner.complete_sources)."""
    built = build(env, [{"tag": "PGR-10Q-FY2026Q2#p37", "why": "policies in force"}])
    fx.write_yaml(env.private / "runs" / "APP" / "2026-09-30-01C" / runner.MANIFEST,
                  {"bundle": "runs/APP/2026-09-30-01C", "step": "01C", "company": "APP",
                   "inputs": [{"name": "requested_documents", "sources": built.sources}]})
    valuation = runner.PlannedWrite("valuation_md", registry.PRIVATE_REPO, "private",
                                    "runs/APP/2026-09-30-01C/valuation_md.md",
                                    b"Progressive's personal auto policies grew 8.8% [src:PGR-10Q-FY2026Q2#p37].\n")
    writes, warnings = [valuation], []
    runner.complete_sources(writes, {"company": "APP"}, env.roots, warnings)
    (merged,) = [w for w in writes if w.path == "companies/APP/sources.yml"]
    assert merged.repo == registry.PRIVATE_REPO
    entry = yaml.safe_load(merged.text)["sources"][-1]
    assert entry == {"tag": "PGR-10Q-FY2026Q2", "kind": "filing",
                     "title": f"{PGR_NAME} 10-Q for FY2026Q2 (period ended 2026-06-30)", "issuer_cik": PGR_CIK,
                     "form": "10-Q", "period": "FY2026Q2", "accession": "0000080661-26-000308",
                     "filed": "2026-08-03", "url": f"{folder('0000080661-26-000308')}/pgr-20260630.htm",
                     "primary": True, "note": entry["note"]}
    assert warnings == ["registered 1 cited document(s) the pipeline had supplied: PGR-10Q-FY2026Q2"]
