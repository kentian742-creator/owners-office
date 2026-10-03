"""Synthetic workspace for tests/test_runner.py.

Two small git repositories laid out like the real pair (the public one carries a copy of this repository's
agents/*.yml, so the role checks are the real ones), prompts that are synthetic front matter only (no text of the
private prompts; the input names are the public ones from agents/*.yml), EDGAR answers from tests/fixtures/edgar/
plus a few synthetic documents, and a trimmed prereg schema. Canary strings mark every place where input or output
text could leak into printed output. Nothing touches the network, the real .env or a model.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

from pipeline import edgar, registry, runner

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "edgar"
APP_CIK = "0001751008"
FAKE_UA = "OwnersOffice-runner-test runner-test@example.invalid"
RUN_DATE = "2026-10-20"

CANARY_THESIS = "CANARY-THESIS-7f3a"
CANARY_REPORT = "CANARY-REPORT-2b9c"
CANARY_FILING = "CANARY-FILING-5d1e"
CANARY_OUTPUT = "CANARY-OUTPUT-9a4f"
CANARIES = (CANARY_THESIS, CANARY_REPORT, CANARY_FILING, CANARY_OUTPUT)

PREREG_CANDIDATE = "Pre-registration candidate"  # the todo label of prompts 01B and 15A ("Pre-registration candidate:")

RULES = """---
id: "00"
title: synthetic series rules (runner tests)
version: "9.0"
output_formats:
  yaml: [prereg, questions, question_list, blind_answers, unprompted_observations, qualitative_verdicts, divergence_map, question_answers, prereg_settlement, ledger_settlement, fact_table, metric_values, fact_verdicts, findings, inversion_list, test_proposals, dossier_changes, patch_decisions, thesis, ledger, sources_additions, reviewed_sections, escalation, valuation_input_notes, revision_notes, gate_decision, returns, rulings, owner_letter_items]
  markdown: [letter, private_appendix, update, story]
  text: [l2_report, pr_body, mistakes_entry]
  file: [pdf]
---

Synthetic rules for the runner tests.
"""

# The front matter of the real prompts, part by part (test_the_real_prompts_are_covered_and_match_the_fixtures
# compares them); the prompt text is synthetic.
PROMPT_01 = """---
id: "01"
title: synthetic company archive
version: "9.0"
role: company_manager
parts:
  A: {name: Dossier, inputs: [run_date, sources, xbrl_facts, industries, "existing_dossier?", "findings_04A?", "hq_rulings?", "findings_04B?"], outputs: [dossier, sources_additions, questions, "mistakes_entry?"]}
  B: {name: System files, inputs: [run_date, dossier, schema, lynch_template, metrics_registry, "existing_system_files?"], outputs: [thesis, story, ledger, questions]}
  C: {name: Valuation, inputs: [run_date, dossier, xbrl_facts, filings, price_reference, year_end_closes, anchors, series_roster, schema, "findings_04C?", "valuation?", "hq_rulings?", "holdings_marks?"], outputs: [valuation_md, valuation_yml, questions]}
---

Synthetic prompt 01 about {{company}} ({{ticker}}), status {{status}}.
"""

PROMPT_02 = """---
id: "02"
title: synthetic research report
version: "9.0"
role: company_manager
design: 00D §D1
modes:
  report:
    inputs: [run_date, dossier, thesis, valuation, series_roster, sources, price_reference, anchors, wordmark]
    outputs: [report, cover, charts, archive_patch, "valuation_input_notes?", sources_additions, questions]
inputs: [run_date, dossier, thesis, valuation, series_roster, sources, price_reference, anchors, wordmark]
outputs: [report, cover, charts, archive_patch, "valuation_input_notes?", sources_additions, questions]
---

Synthetic prompt 02 about {{company}} ({{ticker}}).
"""

PROMPT_03 = """---
id: "03"
title: synthetic quarterly update
version: "9.4"
role: company_manager
parts:
  draft: {inputs: [run_date, dossier, thesis, ledger, constitution, filings, ci_results, "qualitative_verdicts?", "prereg_settlement?", "prereg_due?", ledger_settlement, "question_list?", pending_archive_patch, pending_test_proposals, pending_ledger_entries, "owner_notes?", schema], outputs: [update, question_answers, dossier_changes, patch_decisions, thesis, ledger, "story?", sources_additions, reviewed_sections, mistakes_entry, pr_body, l2_report, "escalation?", "valuation_input_notes?", questions]}
  revise: {name: 03R synthetic revision, inputs: [draft_outputs, findings_04A, "test_proposals_04B_lite?", "returns_17A?", sources], outputs: [update, question_answers, dossier_changes, patch_decisions, thesis, ledger, "story?", sources_additions, reviewed_sections, mistakes_entry, pr_body, l2_report, "escalation?", "valuation_input_notes?", questions, revision_notes]}
---

Synthetic prompt 03 about {{company}} ({{ticker}}) for {{period}}.
"""

PROMPT_04 = """---
id: "04"
title: synthetic audit
version: "9.5"
parts:
  A: {name: synthetic fact audit, role: auditor, inputs: [fact_table, sources], outputs: [fact_verdicts, findings, questions]}
  B_lite: {name: synthetic inversion list, role: red_team, inputs: [update, filings, thesis_without_loss_paths, "prior_inversion_list?"], outputs: [inversion_list, test_proposals]}
  C: {name: synthetic model review, role: model_reviewer, inputs: [valuation, "report_valuation_section?", series_roster, price_reference, anchors, sources, "hq_rulings?", "holdings_marks?"], outputs: [model_checks, valuation_decision, findings, questions]}
---

Synthetic prompt 04: the {{subject}} of {{company}} ({{ticker}}).
"""

PROMPT_14 = """---
id: "14"
title: synthetic question list, blind read, rulings and divergence map
version: "9.1"
parts:
  Q: {name: synthetic question list, role: hq_capital_allocator, inputs: [thesis, dossier], outputs: [question_list]}
  A: {name: synthetic blind read, role: blind_reader, inputs: [filings, question_list_stripped], outputs: [blind_answers, unprompted_observations]}
  T: {name: synthetic rulings, role: judge, inputs: [event, qualitative_tests, where_documents], outputs: [qualitative_verdicts]}
  B: {name: synthetic divergence map, role: hq_capital_allocator, inputs: [question_list, question_answers, blind_answers, unprompted_observations, filings, thesis, dossier, update], outputs: [divergence_map, l2_report, questions]}
---

Synthetic prompt 14.
"""

PROMPT_15 = """---
id: "15"
title: synthetic pre-registration and settlement
version: "9.2"
parts:
  A: {name: synthetic prereg, role: company_manager, inputs: [run_date, thesis, dossier, latest_filings, prereg_candidates, calibration, event, release_history, schema], outputs: [prereg, l2_report, questions]}
  B: {name: synthetic settlement, role: settler, inputs: [event, items_blind, filings, metric_values, ledger_due], outputs: [prereg_settlement, ledger_settlement, questions]}
---

Synthetic prompt 15.
"""

PROMPT_16 = """---
id: "16"
title: synthetic extraction
version: "9.6"
parts:
  A: {name: synthetic fact extraction, role: extractor, inputs: [product], outputs: [fact_table]}
  B: {name: synthetic metric extraction, role: extractor, inputs: [filings, metric_definitions, "prior_values?"], outputs: [metric_values]}
---

Synthetic prompt 16.
"""

PROMPT_17 = """---
id: "17"
title: synthetic HQ
version: "9.7"
role: hq_capital_allocator
parts:
  A: {name: synthetic review, inputs: [run_date, update_outputs, findings_04A, inversion_list, divergence_map, ci_results, trust_level, gate_rules, questions, "valuation_input_notes?", decision_rights], outputs: [gate_decision, returns, rulings, l2_report, owner_letter_items]}
---

Synthetic prompt 17.
"""

PROMPT_18 = """---
id: "18"
title: synthetic monthly letter
version: "9.3"
role: hq_capital_allocator
inputs: [run_date, month_events, updates, divergence_map, owner_letter_items, l2_report, rulings, questions_open, trust_changes, memos, budget, failures, phase_status, prereg_activity, calibration, mistakes]
outputs: [letter, private_appendix]
---

Synthetic prompt 18.
"""

PROMPTS = {"00-rules.md": RULES, "01-archive.md": PROMPT_01, "02-report.md": PROMPT_02, "03-update.md": PROMPT_03, "04-audit.md": PROMPT_04, "14-questions.md": PROMPT_14,
           "15-prereg.md": PROMPT_15, "16-extraction.md": PROMPT_16, "17-hq.md": PROMPT_17, "18-letter.md": PROMPT_18}

THESIS_APP = {
    "schema_version": "0.2",
    "company": "APP",
    "name": "AppLovin Corporation (synthetic)",
    "status": "holding",
    "filer": {"cik": APP_CIK, "type": "domestic", "fiscal_year_end": "12-31", "earnings_form": "8-K",
              "annual_form": "10-K"},
    "domain": "digital_advertising",
    "trust_level": 1,
    "thesis": {"summary": f"Synthetic summary {CANARY_THESIS}.",
               "pillars": [{"id": "P1", "claim": "Synthetic pillar."}],
               "permanent_loss_paths": ["Synthetic loss path."]},
    "todo": [
        f"{PREREG_CANDIDATE} for Q3 2026 (synthetic): buybacks at least as large as Q2 {CANARY_THESIS}",
        "Watch: pre-registration candidates are named in the todo label only, not in the text: a mention later on",
        "Culture rating: left empty until there is first-hand evidence.",
    ],
}
CANARY_TEST = "CANARY-TESTWORDING-3c8d"  # thesis wording in tests' claims and notes: never given to 16B, 14T
WHERE_L1 = "Earnings 8-K Exhibit 99.1 press release, call materials, the MD&A of the 10-Q and 10-K"
THESIS_APP["tests"] = [
    {"id": "APP-Q1", "type": "quantitative", "claim": f"Growth holds {CANARY_TEST}", "origin": "manual",
     "severity": "watch", "covers": ["growth"], "metric": "revenue_yoy", "fail_if": "Below 10%",
     "rule": {"op": "<", "threshold": 10, "unit": "%", "consecutive": 1, "period": "quarter"}, "data": "xbrl",
     "effective_from": "FY2026Q2", "note": f"Synthetic note {CANARY_TEST}."},
    {"id": "APP-Q5", "type": "quantitative", "claim": f"Unit drivers hold {CANARY_TEST}", "origin": "manual",
     "severity": "breaker", "covers": ["unit_economics"], "metric_def": {
         "id": "unit_drivers_yoy", "description": "Installs year on year", "unit": "%", "frequency": "quarter",
         "data": "filing_text", "where": "Key metrics in the 10-Q MD&A",
         "components": {"installs": {"description": "Installs", "unit": "count", "where": "10-Q MD&A",
                                     "xbrl": ["us-gaap:NotAConcept"]}}},
     "fail_if": "Below 0 for 2 quarters", "rule": {"op": "<", "threshold": 0, "unit": "%", "consecutive": 2},
     "data": "filing_text", "effective_from": "FY2026Q2", "first_readable": "2026-08",
     "note": f"Synthetic note {CANARY_TEST}."},
    {"id": "APP-Q7", "type": "quantitative", "claim": f"The segment grows {CANARY_TEST}", "origin": "manual",
     "severity": "watch", "covers": ["growth"], "metric": "segment_revenue_yoy",
     "params": {"segment": "Apps", "basis": "reported"}, "fail_if": "Below 0",
     "rule": {"op": "<", "threshold": 0, "unit": "%", "consecutive": 1}, "data": "filing_text",
     "effective_from": "FY2026Q2"},
    {"id": "APP-Q9", "type": "quantitative", "claim": "Later reading", "origin": "manual", "severity": "watch",
     "covers": ["growth"], "metric": "annual_text_metric", "metric_def": {
         "description": "Read from the 10-K", "unit": "%", "data": "filing_text", "where": "10-K"},
     "fail_if": "Below 0", "rule": {"op": "<", "threshold": 0}, "data": "filing_text", "effective_from": "FY2026Q2",
     "first_readable": "2027-02"},
    {"id": "APP-L1", "type": "qualitative", "claim": f"No large acquisitions {CANARY_TEST}", "origin": "manual",
     "severity": "watch", "covers": ["capital_allocation"], "question": "Did the company announce an acquisition?",
     "fail_if": "Yes, above 10% of assets", "warn_if": "Yes", "judge": "independent_model", "evidence": "required",
     "where": WHERE_L1, "lookback": 1, "judge_notes": ["Count only signed agreements."],
     "baseline": {"value": "No acquisition in 2026", "as_of": "2026-09-24", "source": "APP-8K-2026-08-05#EX-99.1",
                  "note": f"Baseline note {CANARY_TEST}."},
     "effective_from": "FY2026Q2", "note": f"Synthetic note {CANARY_TEST}."},
    {"id": "APP-L4", "type": "qualitative", "claim": "Later", "origin": "manual", "severity": "breaker",
     "covers": ["management"], "question": "Did the founder leave?", "fail_if": "Yes", "judge": "independent_model",
     "evidence": "required", "where": "8-K Item 5.02, Form 4, DEF 14A", "lookback": 4,
     "effective_from": "FY2026Q2", "first_readable": "2027-07"},
]
LEDGER_APP = {"company": "APP", "entries": [
    {"id": "APP-M-2026-01", "side": "management", "kind": "numeric_target", "statement": "Synthetic target.",
     "made_at": "2026-08-05", "due": "2026", "source": "APP-8K-2026-08-05#EX-99.1", "status": "pending"}]}
METRICS_YML = {"metrics": [
    {"id": "revenue_yoy", "description": "Year-on-year growth of total revenue", "unit": "%", "frequency": "quarter",
     "data": "xbrl", "xbrl": ["us-gaap:Revenues"], "formula": "revenue_t / revenue_{t-4q} - 1"},
    {"id": "segment_revenue_yoy", "description": "Year-on-year revenue growth of a segment", "unit": "%",
     "frequency": "quarter", "data": "filing_text", "where": "segment note or earnings press release"},
]}
PERMISSIVE_SCHEMAS = ("thesis", "ledger", "story", "escalation")
# The settlement file's schema, trimmed from thesis-ci's prereg-settlement.schema.json.
SETTLEMENT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
    "required": ["company", "period", "results"],
    "properties": {
        "company": {"type": "string"}, "period": {"type": "string", "pattern": "^FY\\d{4}Q[1-4]$"},
        "acceptance_datetime": {"type": ["string", "null"]}, "accession": {"type": ["string", "null"]},
        "merged_at": {"type": ["string", "null"]}, "ots_proof": {"type": ["string", "null"]},
        "results": {"type": "array", "items": {
            "type": "object", "required": ["id", "outcome"],
            "properties": {"id": {"type": "string"}, "outcome": {"enum": ["happened", "not_happened", "undetermined"]},
                           "values": {}, "calculation": {"type": ["string", "null"]},
                           "evidence": {"type": ["string", "null"]}, "source": {"type": ["string", "null"]},
                           "reasoning": {"type": ["string", "null"]}, "settled_at": {"type": ["string", "null"]},
                           "hq_ruling": {}},
            "additionalProperties": False}},
    },
    "additionalProperties": False,
}
SOURCES_SCHEMA = {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object", "required": ["sources"],
                  "properties": {"sources": {"type": "array"}}}
THESIS_AXP = {"schema_version": "0.2", "company": "AXP", "name": "Synthetic candidate", "status": "candidate",
              "domain": "payments_financial_data"}
SOURCES_APP = {"sources": [{"tag": "APP-8K-2026-08-05", "kind": "filing", "title": "synthetic", "form": "8-K",
                            "accession": "0001751008-26-000057", "filed": "2026-08-05"}]}

PREREG_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["company", "event", "deadline", "author", "items"],
    "properties": {
        "company": {"type": "string", "pattern": "^[A-Z][A-Z0-9.]{0,9}$"},
        "event": {
            "type": "object",
            "required": ["period", "expected_release", "form", "placeholder"],
            "properties": {"period": {"type": "string", "pattern": "^FY\\d{4}Q[1-4]$"},
                           "expected_release": {"type": "string"},
                           "form": {"enum": ["8-K", "6-K", "10-Q", "10-K", "20-F"]},
                           "placeholder": {"type": "boolean"}},
            "additionalProperties": False,
        },
        "deadline": {"type": "string"},
        "author": {"enum": ["system", "owner"]},
        "horizon": {"enum": ["quarter", "18m", "mixed"]},
        "items": {
            "type": "array", "minItems": 1, "maxItems": 8,
            "items": {
                "type": "object",
                "required": ["id", "statement", "probability", "criterion", "data_source", "horizon", "resolves_by",
                             "domain", "added_by"],
                "properties": {
                    "id": {"type": "string", "pattern": "^[A-Z][A-Z0-9.]*-FY\\d{4}Q[1-4]-\\d+$"},
                    "statement": {"type": "string"}, "criterion": {"type": "string"},
                    "data_source": {"type": "string"}, "reference": {"type": "string"},
                    "probability": {"type": "number", "minimum": 0.05, "maximum": 0.95},
                    "horizon": {"enum": ["quarter", "18m"]}, "resolves_by": {"type": "string"},
                    "domain": {"type": "string"}, "added_by": {"enum": ["system", "owner"]},
                    "pillar": {"type": "string"}, "falsifies_thesis": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}

EX991_URL = f"{edgar.ARCHIVES_BASE}/1751008/000175100826000057/exhibit991-2q26earningspre.htm"
TENQ_INDEX_URL = f"{edgar.ARCHIVES_BASE}/1751008/000175100826000059/index.json"
TENQ_URL = f"{edgar.ARCHIVES_BASE}/1751008/000175100826000059/app-20260630.htm"
EX991_HTML = (f"<html><body><p>AppLovin Announces Second Quarter 2026 Financial Results</p>"
              f"<p>Synthetic press release {CANARY_FILING}.</p></body></html>")
TENQ_HTML = ("<html><body><div style='display:none'><ix:header>hidden contexts and units</ix:header></div>"
             "<p>FORM 10-Q</p><p>Synthetic quarterly report.</p><p>Installs grew to 1,234 million in the quarter.</p>"
             "</body></html>")
TENK_URL = f"{edgar.ARCHIVES_BASE}/1751008/000175100826000010/app-20251231.htm"
TENK_HTML = "<html><body><p>FORM 10-K</p><p>Synthetic annual report; risk factors.</p></body></html>"
COMPANYFACTS_URL = f"{edgar.DATA_BASE}/api/xbrl/companyfacts/CIK{APP_CIK}.json"
# The shim evaluator (tests/evaluate_shim.py) reads ready readings from this key of the synthetic companyfacts.
COMPANYFACTS = {"cik": 1751008, "entityName": "AppLovin (synthetic)", "facts": {}, "shim_readings": [
    {"metric": "revenue_yoy", "period": "FY2026Q2", "value": 12.0, "unit": "%", "source": "APP-XBRL#us-gaap:Revenues",
     "basis": "synthetic"}]}


def git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=runner-test", "-c", "user.email=runner-test@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True, text=True,
    )
    return proc.stdout


def commit_all(root: Path, message: str = "fixture") -> None:
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def write_yaml(path: Path, data: Any) -> Path:
    return write(path, yaml.safe_dump(data, allow_unicode=True, sort_keys=False))


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Transport:
    """EDGAR answers: the APP submissions and 8-K index from tests/fixtures/edgar/ plus synthetic documents."""

    def __init__(self) -> None:
        self.responses: dict[str, bytes] = {
            f"{edgar.DATA_BASE}/submissions/CIK{APP_CIK}.json": (FIXTURES / f"CIK{APP_CIK}.json").read_bytes(),
        }
        archives = json.loads((FIXTURES / "archives.json").read_text(encoding="utf-8"))["responses"]
        for url, body in archives.items():
            self.responses[url] = json.dumps(body).encode() if isinstance(body, dict) else body.encode()
        self.responses[EX991_URL] = EX991_HTML.encode()
        self.responses[TENQ_INDEX_URL] = json.dumps(
            {"directory": {"item": [{"name": "app-20260630.htm", "size": "300"}]}}).encode()
        self.responses[TENQ_URL] = TENQ_HTML.encode()
        self.responses[TENK_URL] = TENK_HTML.encode()
        self.responses[COMPANYFACTS_URL] = json.dumps(COMPANYFACTS).encode()
        self.calls: list[str] = []

    def __call__(self, url: str, headers: Any, timeout: float) -> tuple[int, dict[str, str], bytes]:
        self.calls.append(url)
        if url in self.responses:
            return 200, {}, self.responses[url]
        return 404, {}, b"<html>Not Found</html>"


def make_gateway(cache_dir: Path) -> registry.EdgarGateway:
    clock = Clock()
    client = edgar.EdgarClient(user_agent=FAKE_UA, transport=Transport(), cache_dir=cache_dir,
                               limiter=edgar.RateLimiter(5, clock=clock.monotonic, sleep=clock.sleep),
                               sleep=clock.sleep, jitter=0)
    return registry.EdgarGateway(client)


@dataclasses.dataclass
class Env:
    workspace: Path
    public: Path
    private: Path
    schemas: Path
    roots: runner.Roots
    gateway: registry.EdgarGateway
    log: Path

    def assemble(self, step: str = "15A", company: str = "APP", period: str = "FY2026Q3", **kwargs: Any) -> Path:
        import datetime as dt

        kwargs.setdefault("run_date", dt.date.fromisoformat(RUN_DATE))
        kwargs.setdefault("allow_dirty", True)
        kwargs.setdefault("today", dt.date(2026, 10, 20))
        kwargs.setdefault("edgar_gateway", self.gateway)
        return runner.assemble(step, company, period, roots=self.roots, schemas_dir=self.schemas, **kwargs)

    def execute(self, bundle: Path, **kwargs: Any) -> dict[str, Any]:
        """Execute with the fake backend by default; pass backend="api" and client=... to inject a test double."""
        import io

        kwargs.setdefault("backend", "fake")
        kwargs.setdefault("env", {})
        kwargs.setdefault("out", io.StringIO())
        return runner.execute(bundle, roots=self.roots, log_path=self.log, schemas_dir=self.schemas, **kwargs)

    def executed(self, step: str = "15A", company: str = "APP", period: str = "FY2026Q3", **kwargs: Any) -> Path:
        bundle = self.assemble(step, company, period, **kwargs)
        record = self.execute(bundle)
        assert record["status"] == "succeeded", record.get("error")
        return bundle


def make_env(tmp_path: Path) -> Env:
    workspace = tmp_path / "ws"
    public = workspace / registry.PUBLIC_REPO
    private = workspace / registry.PRIVATE_REPO
    for path in sorted((REPO_ROOT / "agents").glob("*.yml")):
        target = public / "agents" / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    write(public / "repo.yml", "visibility: public\nowner: runner-test\nspec_version: \"0.2\"\n"
                               "counterpart: owners-office-private\n")
    write(public / "constitution" / "decision-rights.yml", "budget:\n  monthly_usd: 20\n")
    write_yaml(public / "companies" / "APP" / "thesis.yml", THESIS_APP)
    write_yaml(public / "companies" / "APP" / "sources.yml", SOURCES_APP)
    write_yaml(public / "companies" / "APP" / "ledger.yml", LEDGER_APP)
    write(public / "constitution" / "owner.md", "# Owner's constitution (synthetic)\n\n- R1 Business first.\n")
    write(public / "companies" / "APP" / "prereg" / ".gitkeep", "")
    write_yaml(public / "companies" / "AXP" / "thesis.yml", THESIS_AXP)
    write_yaml(public / "trust" / "levels.yml", {"as_of": "2026-09-24", "companies": {"APP": 1, "AXP": 1},
                                                  "industries": {}})
    write(public / "mistakes.md", "# Mistakes\n\n## The list\n\n### 2026-09-25 · APP · fact error\n\n- Synthetic.\n")
    write(public / "docs" / "STATUS.md", "# Status\n\n- Current phase: synthetic phase 1\n")
    write(public / "docs" / "acceptance" / "phase-0.md", "# Phase 0 acceptance\n\nPASS\n")
    write_yaml(public / "forecasts" / "2026.yml", {"year": 2026, "forecasts": []})
    git(public, "init", "-q", "-b", "main")
    commit_all(public)

    write(private / "repo.yml", "visibility: private\nowner: runner-test\nspec_version: \"0.2\"\n"
                                "counterpart: owners-office\n")
    for name, text in PROMPTS.items():
        write(private / "prompts" / name, text)
    write(private / "runs" / ".gitkeep", "")
    write_yaml(private / "companies" / "APP" / "sources.yml", {"sources": [
        {"tag": "APP-RPT1-2026-09-20", "kind": "report", "title": "synthetic report", "primary": False}]})
    write(private / "memos" / ".gitkeep", "")
    git(private, "init", "-q", "-b", "main")
    commit_all(private)

    write(workspace / "inputs" / "text" / "reports__APP.txt", f"Synthetic complete report {CANARY_REPORT}.\n")
    schemas = tmp_path / "schemas"
    write(schemas / "prereg.schema.json", json.dumps(PREREG_SCHEMA, indent=2))
    for name in PERMISSIVE_SCHEMAS:
        write(schemas / f"{name}.schema.json", json.dumps({"$schema": PREREG_SCHEMA["$schema"], "type": "object"}))
    write(schemas / "sources.schema.json", json.dumps(SOURCES_SCHEMA))
    write(schemas / "prereg-settlement.schema.json", json.dumps(SETTLEMENT_SCHEMA))
    write_yaml(tmp_path / "metrics.yml", METRICS_YML)
    roots = runner.Roots(public=public, private=private, workspace=workspace)
    return Env(workspace=workspace, public=public, private=private, schemas=schemas, roots=roots,
               gateway=make_gateway(tmp_path / "edgar-cache"), log=tmp_path / "llm-log.jsonl")


def rewrite_output(bundle: Path, name: str, text: str) -> None:
    """Replace an executed bundle's output and keep run.yml's hash in step (as if the model had written it)."""
    record = yaml.safe_load((bundle / runner.RUN_RECORD).read_text(encoding="utf-8"))
    entry = record["outputs"][name]
    (bundle / entry["file"]).write_text(text, encoding="utf-8")
    entry["sha256"] = registry.sha256_text(text)
    entry["bytes"] = len(text.encode("utf-8"))
    runner.write_yaml(bundle / runner.RUN_RECORD, record)
