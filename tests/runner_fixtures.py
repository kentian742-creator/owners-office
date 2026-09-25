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

PREREG_CANDIDATE = "\u9884\u6ce8\u518c\u5019\u9009"  # the 15A todo label, as the Chinese archive writes it
FULL_WIDTH_COLON = "\uff1a"

RULES = """---
id: "00"
title: synthetic series rules (runner tests)
version: "9.0"
output_formats:
  yaml: [prereg, questions, question_list, blind_answers]
  markdown: [letter, private_appendix]
  text: [l2_report, unprompted_observations]
  file: [pdf]
---

Synthetic rules for the runner tests.
"""

PROMPT_14 = """---
id: "14"
title: synthetic question list and blind read
version: "9.1"
parts:
  Q: {name: synthetic question list, role: hq_capital_allocator, inputs: [thesis, dossier], outputs: [question_list]}
  A: {name: synthetic blind read, role: blind_reader, inputs: [question_list_stripped], outputs: [blind_answers, unprompted_observations]}
---

Synthetic prompt 14.
"""

PROMPT_15 = """---
id: "15"
title: synthetic pre-registration
version: "9.2"
parts:
  A: {name: synthetic prereg, role: company_manager, inputs: [run_date, thesis, dossier, latest_filings, prereg_candidates, calibration, event, release_history, schema], outputs: [prereg, l2_report, questions]}
---

Synthetic prompt 15.
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

PROMPTS = {"00-rules.md": RULES, "14-questions.md": PROMPT_14, "15-prereg.md": PROMPT_15, "18-letter.md": PROMPT_18}

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
        f"2026 Q3 {PREREG_CANDIDATE} (synthetic){FULL_WIDTH_COLON}buybacks at least as large as Q2 {CANARY_THESIS}",
        "Culture rating: left empty until there is first-hand evidence.",
    ],
}
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
             "<p>FORM 10-Q</p><p>Synthetic quarterly report.</p></body></html>")


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
    write(public / "companies" / "APP" / "prereg" / ".gitkeep", "")
    write_yaml(public / "companies" / "AXP" / "thesis.yml", THESIS_AXP)
    write_yaml(public / "trust" / "levels.yml", {"as_of": "2026-09-24", "companies": {"APP": 1, "AXP": 1},
                                                  "industries": {}})
    write(public / "mistakes.md", "# Mistakes\n\n## List\n\n### 2026-09-25 · APP · fact error\n\n- Synthetic.\n")
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
    write(private / "memos" / ".gitkeep", "")
    git(private, "init", "-q", "-b", "main")
    commit_all(private)

    write(workspace / "inputs" / "text" / "reports__APP.txt", f"Synthetic complete report {CANARY_REPORT}.\n")
    schemas = tmp_path / "schemas"
    write(schemas / "prereg.schema.json", json.dumps(PREREG_SCHEMA, indent=2))
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
