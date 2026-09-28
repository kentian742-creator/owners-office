"""Input registry for the pipeline runner (docs/decisions/0019, 0024). This module never calls a model.

A step is one prompt part run by one role. Before an earnings event: 14Q (question list, HQ), 15A (pre-registration,
company manager); once a month: 18 (monthly letter, HQ). After an earnings event (docs/decisions/0024): 16B (metric
extraction), 03-draft (quarterly update draft, company manager), 16A (fact extraction from the draft), 04A (fact
audit), 03R (revision after the audit), 15B (settlement of what is due) and 17A (HQ review and release gate); for
holdings also 14T (qualitative tests, judge), 14A (blind read), 04B-lite (inversion list) and 14B (divergence map).
16A, 04A, 03R and 17A may run a second round in one event (ROUNDS): the audit of the revision's new and changed facts,
and the revision after HQ returns an update. The deterministic evaluation of the quantitative tests (ci_results) is
not a prompt part; the runner records it like a run (the `ci` step, pipeline/evaluation.py), and pipeline/chain.py
runs the whole chain of one event.

The prompt's front matter is the only list of a part's inputs; this module maps every input name to an assembler
that builds that document from the two repositories, EDGAR (through pipeline.edgar and pipeline/documents.py), the
thesis-ci schemas and earlier run outputs under runs/ in the private repository.

- A declared input without an assembler, or a required input whose data cannot be found, fails the assembly;
  the runner reports every missing input at once.
- A required input that legitimately has no data yet (no settled predictions, no update this month) becomes an
  explicit document that says so in one line, and the manifest marks it empty. Nothing is silently omitted.
  An optional input may be left out only by raising Omit, and the manifest records the reason.
- Stand-ins are declared: until companies/<T>/dossier.md exists, the owner's complete report text stands in for
  the dossier (substitute: dossier in the manifest). In a dry-run rehearsal before the event (RunContext.rehearsal),
  the last reported quarter's filings stand in for the event's (substitute: filings); real assemblies never do this.
- Upstream outputs are the succeeded runs of the same company and period (RunContext.latest_run); a dry run looks in
  its own directory first, so a rehearsal chain reads its own placeholders and never the private repository's runs.
- Assemblers read files, git history and EDGAR. They never call a model and never print what they read.

Adding a step means adding a StepSpec to STEPS and an assembler for each input name that part declares and that is
not registered yet; the runner, the bundle format and the workflow stay as they are.
"""

from __future__ import annotations

import calendar
import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml

from . import documents, edgar, evaluation, isolation, llm, slicing
from . import outputs as _outputs

PUBLIC_REPO = "owners-office"
PRIVATE_REPO = "owners-office-private"
WORKSPACE = "workspace"  # the directory that holds both checkouts, the local .env and inputs/

MANIFEST_NAME = "manifest.yml"
RUN_RECORD_NAME = "run.yml"
OUTPUTS_DIR_NAME = "outputs"
ATTEMPTS_DIR_NAME = "attempts"  # failed attempts kept by `execute --retry`: attempts/<n>/run.yml
LLM_LOG_REL = "runs/llm-log.jsonl"  # persistent call log in the private repository (STATUS T7)

# Prompt variables (prompts README): {{company}}, {{ticker}}, {{status}}, {{period}}, {{date}}.
VAR_COMPANY = "company"  # the company's common English short name (00 section W7), from thesis.yml's name
VAR_TICKER = llm.VAR_TICKER  # ticker
VAR_STATUS = "status"  # holding | candidate
VAR_PERIOD = llm.VAR_PERIOD  # FY<year>Q<quarter>
VAR_DATE = "date"  # run date
# The label that marks a pre-registration candidate in thesis.yml's todo list: "Pre-registration candidate: ..."
# (prompts 01B and 15A). Matched case-insensitively in the text before the first colon.
PREREG_CANDIDATE_MARKS = ("pre-registration candidate", "prereg candidate")
_LABEL_SEPARATOR = ":"
# Legal-form words dropped from thesis.yml's name to get the short name (AppLovin Corporation -> AppLovin).
_LEGAL_SUFFIX_RE = re.compile(
    r"(?:,?\s+(?:inc|incorporated|corp|corporation|company|co|ltd|limited|plc|llc|lp|l\.p|n\.v|s\.a|ag|se|sa|nv)\.?)+$",
    re.IGNORECASE)
_PARENTHETICAL_RE = re.compile(r"\s*[(\uff08][^()\uff08\uff09]*[)\uff09]")

QUARTER_RE = re.compile(r"^FY\d{4}Q[1-4]$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
# <run_date>-<part>: a prompt part (15A, 03-draft, 04B-lite; an HQ step about one company adds its ticker, 17A-AXP),
# or a deterministic pipeline step (ci).
RUN_DIR_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(\d{2}[A-Za-z0-9.-]*|[a-z][a-z0-9]*)$")
CI_STEP = "ci"  # the evaluation of the quantitative tests (ci_results), recorded like a run
_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
_MISTAKE_HEADING_RE = re.compile(r"^### (\d{4}-\d{2}-\d{2})\b", re.M)
_IX_HEADER_RE = re.compile(rb"(?is)<ix:header\b.*?</ix:header\s*>")
RESOLVED = ("happened", "not_happened")


class MissingInput(LookupError):
    """A required input has no data and no defined stand-in. The message names what is missing, never content."""


class EventNotReleased(MissingInput):
    """The period's results are not on EDGAR as of the run date, or the event has not closed yet."""


class Omit(Exception):
    """An optional input is left out on purpose; the message is the reason recorded in the manifest."""


# ---------------------------------------------------------------------------------------------------- data


@dataclasses.dataclass(frozen=True)
class StepSpec:
    """One supported step. The inputs come from the prompt's front matter, not from here."""

    step: str  # the part label as the prompt names it: 14Q, 15A, 18, 03-draft, 03R
    prompt_id: str
    part: str | None  # front-matter part key (Q, A, draft, revise), or None for a prompt without parts
    scope: str  # "company" (runs/<TICKER>/...) or "hq" (runs/hq/...)
    period_kind: str  # "quarter" (FY<year>Q<quarter>) or "month" (YYYY-MM, the month the step is about)
    holdings_only: bool  # prompt scope: 14Q, 15A, 14A, 14T, 14B and 04B-lite run for holdings only
    summary: str
    pipeline_fields: Callable[[RunContext], dict[str, dict[str, Any]]] | None = None
    variables: Mapping[str, str] = dataclasses.field(default_factory=dict)  # fixed prompt variables ({{subject}})
    post_event: bool = False  # reads an earnings event that must be on EDGAR (and closed) by the run date
    about_company: bool = False  # an HQ step about one company: runs/hq/<run_date>-<step>-<TICKER>/

    def bundle_name(self, run_date: dt.date, company: str | None, round_: int = 1) -> str:
        """The run directory's name: <run_date>-<step>, plus -<TICKER> for an HQ step about one company, plus -r<n>
        for the second and later round of a step in one event (the audit after 03R, the revision after HQ returns
        an update)."""
        suffix = f"-{company}" if self.about_company and company else ""
        suffix += f"-r{round_}" if round_ > 1 else ""
        return f"{run_date.isoformat()}-{self.step}{suffix}"

    def storage_scope(self, company: str | None) -> str:
        """runs/<scope>/: the ticker for company steps, hq for HQ steps (even when about one company)."""
        return "hq" if self.scope == "hq" else str(company)


@dataclasses.dataclass
class BuiltInput:
    """One assembled input document and where it came from."""

    text: str
    ext: str  # md | yml | txt
    sources: list[dict[str, Any]]
    note: str | None = None
    empty: bool = False  # an explicit "no data yet" document
    substitute: str | None = None  # the input this document stands in for (dossier <- the owner's report text)


@dataclasses.dataclass(frozen=True)
class FilingText:
    """One EDGAR document, converted to text, with the source tag the archive uses for it."""

    tag: str
    locator: str | None  # EX-99.1 for exhibits
    registered: bool  # the tag was taken from a sources.yml entry with the same accession
    form: str
    items: tuple[str, ...]
    accession: str
    filed: dt.date
    document: str
    url: str
    raw_sha256: str
    text: str
    sections: tuple[str, ...] = ()  # the sections kept of a periodic report (14T); empty: the whole document
    note: str | None = None  # how the text was cut or rendered

    @property
    def cite(self) -> str:
        return f"{self.tag}#{self.locator}" if self.locator else self.tag


@dataclasses.dataclass
class ResultsDocuments:
    """The documents of one earnings event: the release (8-K exhibits or the 6-K) and the periodic report."""

    period: str
    period_end: dt.date
    release_date: dt.date
    documents: list[FilingText]
    notes: list[str]
    event: edgar.EarningsEvent | None = None
    rehearsal_for: str | None = None  # dry run: the period these documents stand in for

    def event_record(self, as_of: dt.date) -> dict[str, Any]:
        """The event as post-event inputs describe it: period, release, close (00 section F: event)."""
        e = self.event
        out: dict[str, Any] = {"period": self.rehearsal_for or self.period, "period_end": self.period_end.isoformat(),
                               "release_date": self.release_date.isoformat()}
        if e is not None:
            out.update(form=e.form, accession=e.accession, filing_date=e.filing.filing_date.isoformat(),
                       acceptance_datetime=e.filing.acceptance_datetime,
                       closes_on=e.closes_on.isoformat() if e.closes_on else None, closed_by=e.closed_by,
                       closed=bool(e.closes_on and e.closes_on <= as_of))
            if e.closing is not None:
                out["periodic_report"] = {"form": e.closing.form, "accession": e.closing.accession,
                                          "filing_date": e.closing.filing_date.isoformat()}
        if self.rehearsal_for:
            out["rehearsal"] = {"stand_in_period": self.period,
                                "note": "dry-run rehearsal: the event has not happened yet; the last reported "
                                        "quarter stands in"}
        return out


@dataclasses.dataclass
class PriorRun:
    """A run directory under runs/<scope>/<run_date>-<part>/ in the private repository (or a dry-run directory)."""

    path: Path
    rel: str
    scope: str
    run_date: dt.date
    part: str
    manifest: dict[str, Any] | None
    record: dict[str, Any] | None
    attempts: list[dict[str, Any]] = dataclasses.field(default_factory=list)  # earlier failed attempts (--retry)

    @property
    def step(self) -> str:
        """The step label from the manifest (17A for runs/hq/<date>-17A-AXP), else the directory's part."""
        return str(self.manifest.get("step") or self.part) if self.manifest else self.part

    @property
    def company(self) -> str | None:
        if self.manifest and self.manifest.get("company"):
            return str(self.manifest["company"])
        return None if self.scope == "hq" else self.scope

    @property
    def statuses(self) -> list[str]:
        """Every attempt's status, oldest first: archived attempts, then the current run record."""
        current = [self.status] if self.record is not None else []
        return [str(a.get("status") or "unknown") for a in self.attempts] + current

    @property
    def status(self) -> str:
        if self.record is not None:
            return str(self.record.get("status") or "unknown")
        if self.manifest is not None:
            return "assembled"
        return "recorded"  # written by hand before the runner existed (phase 0)

    def output_file(self, name: str) -> Path | None:
        """The output file of this run: outputs/<name>.* for runner bundles, <name>.* for recorded runs."""
        if self.manifest is not None:
            if self.status != "succeeded":
                return None
            candidates = [self.path / OUTPUTS_DIR_NAME / f"{name}.{ext}" for ext in ("yml", "md")]
        else:
            candidates = [self.path / f"{name}.{ext}" for ext in ("yml", "md", "txt")]
        return next((p for p in candidates if p.is_file()), None)

    def read_output(self, name: str) -> str | None:
        path = self.output_file(name)
        return path.read_text(encoding="utf-8") if path else None

    def placed_file(self, name: str) -> Path | None:
        """The copy `place` wrote to the 00 section F2 default destination runs/<scope>/<run_date>-<part>/<name>.*."""
        return next((p for ext in ("yml", "md") if (p := self.path / f"{name}.{ext}").is_file()), None)

    @property
    def period(self) -> str | None:
        return str(self.manifest.get("period")) if self.manifest and self.manifest.get("period") else None

    @property
    def round(self) -> int:
        """The round of the step in its event (1 unless the manifest says otherwise)."""
        value = self.manifest.get("round") if self.manifest else None
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 1

    @property
    def succeeded(self) -> bool:
        return self.manifest is not None and self.status == "succeeded"

    def outputs_used(self, names: Iterable[str]) -> list[dict[str, Any]]:
        """Run-output source records for the outputs of this run that exist."""
        out = []
        for name in names:
            path = self.output_file(name)
            if path is not None:
                out.append({"kind": "run_output", "run": self.rel, "output": name,
                            "sha256": sha256_bytes(path.read_bytes())})
        return out


@dataclasses.dataclass
class RunContext:
    """Everything an assembler may read for one bundle."""

    step: StepSpec
    company: str | None  # ticker; None for HQ steps that are not about one company
    period: str
    run_date: dt.date
    public_root: Path
    private_root: Path
    workspace_root: Path
    call: Any  # llm.PromptPart (None for the ci step)
    formats: Mapping[str, str]
    edgar: Any = None  # EdgarGateway or a test double; created on first use
    schemas_dir: Path | None = None
    memo: dict[str, Any] = dataclasses.field(default_factory=dict)
    runs_roots: tuple[Path, ...] = ()  # directories searched for upstream runs before the private repository (dry runs)
    rehearsal: bool = False  # dry run before the event: the last reported quarter's filings stand in
    round: int = 1  # the round of this step in its event (2: the audit after 03R, the revision after HQ returns)
    @property
    def scope(self) -> str:
        return self.step.storage_scope(self.company) if self.company else "hq"

    def remember(self, key: str, compute: Callable[[], Any]) -> Any:
        if key not in self.memo:
            self.memo[key] = compute()
        return self.memo[key]

    def thesis_path(self) -> Path:
        return self.public_root / "companies" / str(self.company) / "thesis.yml"

    def thesis(self) -> dict[str, Any]:
        def load() -> dict[str, Any]:
            path = self.thesis_path()
            if not path.is_file():
                raise MissingInput(f"companies/{self.company}/thesis.yml does not exist in {PUBLIC_REPO}")
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise MissingInput(f"companies/{self.company}/thesis.yml is not a mapping")
            return data

        return self.remember("thesis", load)

    def filer(self) -> edgar.Filer:
        def load() -> edgar.Filer:
            try:
                return edgar.load_filer(str(self.company), self.public_root)
            except ValueError as exc:
                raise MissingInput(f"no EDGAR filer for {self.company}: {exc}") from None

        return self.remember("filer", load)

    def gateway(self) -> Any:
        if self.edgar is None:
            self.edgar = EdgarGateway()
        return self.edgar

    def estimate(self) -> edgar.ReleaseEstimate:
        """The (expected) release of this period and the pre-registration deadline, computed once per bundle."""
        return self.remember(
            "estimate", lambda: self.gateway().release_estimate(self.filer(), self.period, self.run_date)
        )

    def runs(self) -> list[PriorRun]:
        """Runs under the dry-run directories (if any) and the private repository, oldest first."""
        return self.remember("runs", lambda: index_run_roots([*self.runs_roots, self.private_root]))

    def latest_run(self, step: str, *, period: str | None = None, company: str | None = None,
                   round_: int | None = None) -> PriorRun | None:
        """The latest succeeded run of a step for this company (and period and round, when given): the highest round
        first, then the latest run date. Runs of a dry-run directory come first, so a rehearsal reads its own chain."""
        company = company if company is not None else self.company
        found = [r for r in self.runs() if r.succeeded and r.step == step and r.company == company
                 and (period is None or r.period == period) and (round_ is None or r.round == round_)]
        if not found:
            return None
        preferred = [r for r in found if any(_inside(r.path, root) for root in self.runs_roots)] or found
        return max(preferred, key=lambda r: (r.round, r.run_date, r.rel))

    def event_runs(self, step: str) -> list[PriorRun]:
        """Every succeeded run of a step for this company and period, in round order (dry-run directory first)."""
        found = [r for r in self.runs() if r.succeeded and r.step == step and r.company == self.company
                 and r.period == self.period]
        preferred = [r for r in found if any(_inside(r.path, root) for root in self.runs_roots)] or found
        return sorted(preferred, key=lambda r: (r.round, r.run_date, r.rel))

    def require_run(self, step: str, name: str, *, why: str = "") -> PriorRun:
        run = self.latest_run(step, period=self.period)
        if run is None:
            raise MissingInput(f"{name}: no succeeded {step} run for {self.company} {self.period}"
                               + (f" ({why})" if why else ""))
        return run

    def status(self) -> str:
        return str(self.thesis().get("status") or "")

    def is_holding(self) -> bool:
        return self.status() == "holding"

    def calendar(self) -> edgar.FiscalCalendar:
        return edgar.FiscalCalendar.parse(self.filer().fiscal_year_end or "12-31")

    def results(self) -> ResultsDocuments:
        """The documents of this period's earnings event, as of the run date (post-event steps). In a rehearsal the
        event is looked up as of today and, when it has not happened yet, the last reported quarter stands in."""

        def load() -> ResultsDocuments:
            company = _need_company(self, "filings")
            known = known_filing_tags((self.public_root, self.private_root), company)
            gateway = self.gateway()
            try:
                return gateway.period_results(self.filer(), self.period, self.run_date, known,
                                              require_closed=not self.rehearsal)
            except EventNotReleased:
                if not self.rehearsal:
                    raise
            stand_in = gateway.latest_results(self.filer(), self.period, dt.date.today(), known)
            stand_in.rehearsal_for = self.period
            stand_in.notes.append(f"rehearsal: {self.period} is not on EDGAR yet; the documents of {stand_in.period} "
                                  "stand in (dry run only)")
            return stand_in

        return self.remember("results", load)
    def month(self) -> tuple[dt.date, dt.date]:
        return month_bounds(self.period)


# ---------------------------------------------------------------------------------------------------- steps


def _prereg_fields(ctx: RunContext) -> dict[str, dict[str, Any]]:
    """15A: the pre-registration header is the pipeline's, not the model's (edgar.prereg_header())."""
    return {"prereg": {"company": ctx.company, **ctx.estimate().prereg_header(), "author": "system"}}


# 00 section G8: the thesis.yml fields the pipeline maintains; 03 hands back the current values unchanged.
PIPELINE_THESIS_FIELDS = ("schema_version", "company", "status", "filer", "trust_level", "domain")
QUARTERLY_UPDATE = "quarterly update"  # {{subject}} of prompt 04 when it audits a quarterly update


def _update_fields(ctx: RunContext) -> dict[str, dict[str, Any]]:
    """03 draft and 03R: the pipeline-maintained fields of thesis.yml and the ledger's company (00 section G8)."""
    thesis = ctx.thesis()
    return {"thesis": {key: thesis[key] for key in PIPELINE_THESIS_FIELDS if key in thesis},
            "ledger": {"company": ctx.company}}


STEPS: dict[str, StepSpec] = {
    "14Q": StepSpec("14Q", "14", "Q", "company", "quarter", True,
                    "neutral question list, frozen before the results event (HQ)"),
    "15A": StepSpec("15A", "15", "A", "company", "quarter", True,
                    "pre-registration of 3-5 settleable expectations (company manager)", _prereg_fields),
    "18": StepSpec("18", "18", None, "hq", "month", False,
                   "monthly letter to the owner about the previous month (HQ)"),
    # After an earnings event (docs/decisions/0024), in the order the event command runs them.
    "16B": StepSpec("16B", "16", "B", "company", "quarter", False,
                    "metric extraction from the event's filings (extractor)", post_event=True),
    "14T": StepSpec("14T", "14", "T", "company", "quarter", True,
                    "independent ruling on the qualitative tests that are due (judge)", post_event=True),
    "14A": StepSpec("14A", "14", "A", "company", "quarter", True,
                    "blind read of the event's filings against the frozen question list (blind reader)",
                    post_event=True),
    "15B": StepSpec("15B", "15", "B", "company", "quarter", False,
                    "settlement of the due pre-registration items and ledger entries (settler)", post_event=True),
    "03-draft": StepSpec("03-draft", "03", "draft", "company", "quarter", False,
                         "quarterly update draft (company manager)", _update_fields, post_event=True),
    "16A": StepSpec("16A", "16", "A", "company", "quarter", False,
                    "fact extraction from the quarterly update draft (extractor, update mode)", post_event=True),
    "04A": StepSpec("04A", "04", "A", "company", "quarter", False,
                    "fact audit of the quarterly update draft (auditor)",
                    variables={"subject": QUARTERLY_UPDATE}, post_event=True),
    "04B-lite": StepSpec("04B-lite", "04", "B_lite", "company", "quarter", True,
                         "inversion list for the quarterly update (red team)",
                         variables={"subject": QUARTERLY_UPDATE}, post_event=True),
    "14B": StepSpec("14B", "14", "B", "company", "quarter", True,
                    "divergence map between the blind read and the company manager (HQ)", post_event=True),
    "03R": StepSpec("03R", "03", "revise", "company", "quarter", False,
                    "revision of the quarterly update after the audit (company manager)", _update_fields,
                    post_event=True),
    "17A": StepSpec("17A", "17", "A", "hq", "quarter", False,
                    "review and release gate of the quarterly update (HQ)", post_event=True, about_company=True),
}
ROUNDS = {"16A": 2, "04A": 2, "03R": 2, "17A": 2}  # steps that may run a second time in one event (docs/decisions/0024)


def step_spec(step: str) -> StepSpec:
    if step not in STEPS:
        raise KeyError(f"unknown step {step!r}; supported: {', '.join(STEPS)}")
    return STEPS[step]


def short_name(name: str | None, ticker: str) -> str:
    """The company's common English short name (00 section W7) from thesis.yml's name: a parenthetical and the legal
    form are dropped (AppLovin Corporation -> AppLovin, S&P Global Inc. -> S&P Global); the ticker when nothing is
    left."""
    text = _PARENTHETICAL_RE.sub("", str(name or "")).strip()
    text = _LEGAL_SUFFIX_RE.sub("", text).strip(" ,")
    return text or ticker


def variables_for(ctx: RunContext) -> dict[str, str]:
    """{{variable}} values of the prompts (prompts README). Unused ones are ignored by llm.fill_variables()."""
    out = {VAR_DATE: ctx.run_date.isoformat()}
    if ctx.company:
        thesis = ctx.thesis()
        out[VAR_TICKER] = ctx.company
        out[VAR_COMPANY] = short_name(thesis.get("name"), ctx.company)
        out[VAR_STATUS] = str(thesis.get("status") or "")
    if ctx.step.period_kind == "quarter":
        out[VAR_PERIOD] = ctx.period
    out.update(ctx.step.variables)
    return out


def placement_context(ctx: RunContext) -> dict[str, Any]:
    """Template fields for outputs.place(): company, period, run_date, month (monthly letter), subject (04), and the
    run directory's name (runs/hq/<run_date>-17A-<TICKER> for an HQ step about one company)."""
    return {
        "company": ctx.company,
        "period": ctx.period if ctx.step.period_kind == "quarter" else None,
        "run_date": ctx.run_date.isoformat(),
        "month": ctx.period if ctx.step.period_kind == "month" else None,
        "doc": None,
        "subject": ctx.step.variables.get(llm.VAR_SUBJECT),
        "scope": ctx.scope,
        "run_dir": ctx.step.bundle_name(ctx.run_date, ctx.company, ctx.round),
    }


# ---------------------------------------------------------------------------------------------------- helpers


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def dump_yaml(data: Any) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=110, default_flow_style=False)


def load_yaml_file(path: Path) -> Any:
    """Parsed YAML, or None when the file is missing or unreadable."""
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None


def month_bounds(month: str) -> tuple[dt.date, dt.date]:
    if not MONTH_RE.match(month):
        raise ValueError(f"month must be YYYY-MM, got {month!r}")
    year, mon = int(month[:4]), int(month[5:])
    return dt.date(year, mon, 1), dt.date(year, mon, calendar.monthrange(year, mon)[1])


def month_end(day: dt.date) -> dt.date:
    return dt.date(day.year, day.month, calendar.monthrange(day.year, day.month)[1])


def _date_prefix(name: str) -> dt.date | None:
    match = _DATE_PREFIX_RE.match(name)
    if not match:
        return None
    try:
        return dt.date.fromisoformat(match.group(1))
    except ValueError:
        return None


def git_output(root: Path, *args: str) -> str | None:
    """stdout of a git command in `root`, or None when git fails or is missing."""
    try:
        proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def git_commits(root: Path, first: dt.date, last: dt.date) -> list[dict[str, str]] | None:
    """Commit dates and subjects in [first, last] (committer date), oldest first; None when git history is unavailable."""
    out = git_output(root, "log", "--reverse", f"--since={first.isoformat()} 00:00:00", f"--until={last.isoformat()} 23:59:59",
               "--format=%cs%x09%s")
    if out is None:
        return None
    rows = []
    for line in out.splitlines():
        date, _, subject = line.partition("\t")
        if date:
            rows.append({"date": date, "subject": subject})
    return rows


def git_first_added(root: Path, rel: str) -> dt.date | None:
    out = git_output(root, "log", "--diff-filter=A", "--format=%cs", "--", rel)
    lines = [line for line in (out or "").splitlines() if line.strip()]
    try:
        return dt.date.fromisoformat(lines[-1]) if lines else None
    except ValueError:
        return None


def git_file_before(root: Path, rel: str, moment: dt.date) -> str | None:
    """The file's content in the last commit before the start of `moment` (committer date), or None."""
    commit = (git_output(root, "log", "-1", f"--before={moment.isoformat()} 00:00:00", "--format=%H", "--", rel) or "").strip()
    if not commit:
        return None
    return git_output(root, "show", f"{commit}:{rel}")


def repo_file_source(root: Path, path: Path, repo: str) -> dict[str, Any]:
    data = path.read_bytes()
    return {
        "kind": "repo_file",
        "repo": repo,
        "path": path.resolve().relative_to(root.resolve()).as_posix(),
        "revision": llm.file_revision(path),
        "sha256": sha256_bytes(data),
    }


def workspace_file_source(workspace_root: Path, path: Path) -> dict[str, Any]:
    return {
        "kind": "workspace_file",
        "repo": WORKSPACE,
        "path": path.resolve().relative_to(workspace_root.resolve()).as_posix(),
        "sha256": sha256_bytes(path.read_bytes()),
    }


def empty_document(ctx: RunContext, name: str, reason: str, **extra: Any) -> BuiltInput:
    """An explicit "no data yet" document: the model is told there is nothing, and why, in one line."""
    data = {"input": name, "status": "empty", "reason": reason, "as_of": ctx.run_date.isoformat(), **extra}
    return BuiltInput(dump_yaml(data), "yml", [{"kind": "generated", "detail": "explicit empty document"}],
                      note=reason, empty=True)


def index_run_roots(roots: Iterable[Path]) -> list[PriorRun]:
    """index_runs() over several roots (dry-run directories, then the private repository), oldest first; a directory
    reached through two roots is listed once."""
    seen: set[Path] = set()
    found: list[PriorRun] = []
    for root in roots:
        for run in index_runs(root):
            key = run.path.resolve()
            if key not in seen:
                seen.add(key)
                found.append(run)
    found.sort(key=lambda r: (r.run_date, r.rel))
    return found


def index_runs(private_root: Path) -> list[PriorRun]:
    """All run directories under runs/<scope>/<run_date>-<part>/, oldest first."""
    base = private_root / "runs"
    found: list[PriorRun] = []
    if not base.is_dir():
        return found
    for scope_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        for run_dir in sorted(p for p in scope_dir.iterdir() if p.is_dir()):
            match = RUN_DIR_RE.match(run_dir.name)
            if not match:
                continue
            try:
                day = dt.date.fromisoformat(match.group(1))
            except ValueError:
                continue
            manifest = load_yaml_file(run_dir / MANIFEST_NAME) if (run_dir / MANIFEST_NAME).is_file() else None
            record = load_yaml_file(run_dir / RUN_RECORD_NAME) if (run_dir / RUN_RECORD_NAME).is_file() else None
            attempts = [load_yaml_file(p) for p in sorted((run_dir / ATTEMPTS_DIR_NAME).glob(f"*/{RUN_RECORD_NAME}"),
                                                           key=lambda p: int(p.parent.name) if p.parent.name.isdigit()
                                                           else 0)]
            found.append(PriorRun(
                path=run_dir, rel=f"runs/{scope_dir.name}/{run_dir.name}", scope=scope_dir.name, run_date=day,
                part=match.group(2), manifest=manifest if isinstance(manifest, dict) else None,
                record=record if isinstance(record, dict) else None,
                attempts=[a for a in attempts if isinstance(a, dict)],
            ))
    found.sort(key=lambda r: (r.run_date, r.rel))
    return found


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _need_company(ctx: RunContext, name: str) -> str:
    if not ctx.company:
        raise MissingInput(f"{name} is about one company, and this {ctx.step.step} bundle has none")
    return ctx.company


# ---------------------------------------------------------------------------------------------------- thesis-ci schemas


def schema_names_for(call: Any) -> list[str]:
    """thesis-ci schemas of the part's outputs (outputs.OUTPUT_SCHEMAS), e.g. ["prereg"] for 15A."""
    return sorted({_outputs.OUTPUT_SCHEMAS[name] for name, _ in call.outputs if name in _outputs.OUTPUT_SCHEMAS})


def schema_file(name: str, schemas_dir: Path | None = None) -> tuple[Path, str]:
    """(path, origin) of a thesis-ci schema, looked up in the same order as outputs.schema_validator()."""
    directory: Path | None = Path(schemas_dir) if schemas_dir else None
    origin = f"schemas directory {directory}" if directory else ""
    if directory is None and os.environ.get(_outputs.SCHEMAS_ENV):
        directory = Path(os.environ[_outputs.SCHEMAS_ENV])
        origin = f"{_outputs.SCHEMAS_ENV}={directory}"
    if directory is None:
        try:
            import thesis_ci
            from thesis_ci import contract
        except ImportError:
            contract = None
        if contract is not None:
            try:
                directory = contract.spec_dir() / "schemas"
                origin = f"thesis-ci {getattr(thesis_ci, '__version__', '?')}"
            except FileNotFoundError:
                directory = None
    if directory is None:
        directory = _outputs.DEFAULT_SCHEMAS_DIR
        origin = "sibling thesis-ci checkout"
    path = directory / f"{name}.schema.json"
    if not path.is_file():
        raise MissingInput(f"thesis-ci schema {name}.schema.json not found ({origin}); install requirements-lint.txt")
    return path, origin


def schema_digest(schema: Any) -> str:
    """sha256 of the schema's canonical JSON: the same number whether read from a file or from a validator."""
    return sha256_text(json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


# ---------------------------------------------------------------------------------------------------- EDGAR


def document_text(raw: bytes, name: str) -> str:
    """EDGAR document -> text. Inline XBRL's hidden header (contexts, units) is dropped before conversion."""
    if name.lower().endswith(".txt"):
        return raw.decode("utf-8", "replace")
    cleaned = _IX_HEADER_RE.sub(b" ", raw)
    return edgar.html_to_text(cleaned, limit=len(cleaned) + 1)


def source_tag(ticker: str, filing: edgar.Filing, cal: edgar.FiscalCalendar) -> str:
    """The archive's tag for a filing (thesis-ci SPEC 3.3): periodic reports by fiscal period, others by filing date."""
    form = edgar.form_key(filing.form)
    if filing.report_date is not None:
        try:
            if filing.form in edgar.QUARTERLY_FORMS:
                return f"{ticker}-{form}-{cal.quarter_label(filing.report_date)}"
            if filing.form in edgar.ANNUAL_FORMS:
                return f"{ticker}-{form}-{cal.annual_label(filing.report_date)}"
        except ValueError:
            pass
    return f"{ticker}-{form}-{filing.filing_date.isoformat()}"


def known_filing_tags(roots: Iterable[Path], company: str) -> dict[str, str]:
    """accession -> tag from the public and private companies/<T>/sources.yml (first one wins)."""
    tags: dict[str, str] = {}
    for root in roots:
        data = load_yaml_file(root / "companies" / company / "sources.yml")
        entries = data.get("sources") if isinstance(data, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict) and entry.get("accession") and entry.get("tag"):
                tags.setdefault(str(entry["accession"]), str(entry["tag"]))
    return tags


class EdgarGateway:
    """The EDGAR facts the assemblers need, through pipeline.edgar.

    The User-Agent comes from SEC_USER_AGENT or the workspace .env (decisions/0013); it never leaves this machine:
    assembly runs locally, and the bundle only carries the documents and their EDGAR addresses.
    """

    def __init__(self, client: edgar.EdgarClient | None = None, *, offline: bool = False):
        self._client = client
        self._offline = offline

    @property
    def client(self) -> edgar.EdgarClient:
        if self._client is None:
            self._client = edgar.EdgarClient(offline=self._offline)
        return self._client

    def release_estimate(self, filer: edgar.Filer, period: str, as_of: dt.date) -> edgar.ReleaseEstimate:
        return edgar.next_release(filer.cik, period, fiscal_year_end=filer.fiscal_year_end, filer_type=filer.type,
                                  form=filer.earnings_form, as_of=as_of, client=self.client)

    def _context(self, filer: edgar.Filer, fiscal_year: int, *, years_back: int = 2) -> tuple[
            edgar.Submissions, edgar.FiscalCalendar, str, str]:
        subs = edgar.submissions(filer.cik, client=self.client, since=dt.date(fiscal_year - years_back, 1, 1))
        cal = edgar.FiscalCalendar.parse(filer.fiscal_year_end or subs.fiscal_year_end)
        kind = filer.type or edgar.infer_filer_type(subs)
        ticker = filer.ticker or str(subs.tickers[0] if subs.tickers else filer.cik)
        return subs, cal, kind, ticker

    def latest_results(self, filer: edgar.Filer, period: str, as_of: dt.date,
                       known_tags: Mapping[str, str] | None = None) -> ResultsDocuments:
        """The results release and periodic report of the last quarter reported before `period`, as of `as_of`."""
        fiscal_year, quarter = edgar.parse_period(period)
        if quarter is None:
            raise ValueError(f"latest filings are looked up for a quarter (FY2026Q3), got {period!r}")
        subs, cal, kind, ticker = self._context(filer, fiscal_year)
        period_end = cal.quarter_end(fiscal_year, quarter)
        events = edgar.earnings_events(subs.cik, cal.label, filer_type=kind, since=period_end - dt.timedelta(days=400),
                                       until=as_of, client=self.client, subs=subs)
        earlier = [e for e in events if e.period_end < period_end]
        if not earlier:
            raise MissingInput(f"EDGAR has no results release for a quarter before {period} as of {as_of}")
        latest = max(earlier, key=lambda e: (e.period_end, e.filing.filing_date, e.accession))
        return self._results_documents(latest, kind, cal, ticker, as_of, known_tags or {})

    def period_results(self, filer: edgar.Filer, period: str, as_of: dt.date,
                       known_tags: Mapping[str, str] | None = None, *, require_closed: bool = True) -> ResultsDocuments:
        """The results release and periodic report of `period` itself, as of `as_of` (post-event steps). Raises
        EventNotReleased when EDGAR has no results filing for the period yet or, with require_closed, when the event
        has not closed (the 10-Q/10-K filed or five business days passed; prompt 03)."""
        fiscal_year, quarter = edgar.parse_period(period)
        if quarter is None:
            raise ValueError(f"an earnings event is a quarter (FY2026Q3), got {period!r}")
        subs, cal, kind, ticker = self._context(filer, fiscal_year)
        period_end = cal.quarter_end(fiscal_year, quarter)
        events = edgar.earnings_events(subs.cik, cal.label, filer_type=kind, since=period_end, until=as_of,
                                       client=self.client, subs=subs)
        event = next((e for e in events if e.period == period), None)
        if event is None:
            raise EventNotReleased(f"EDGAR has no results release for {period} as of {as_of}")
        if require_closed and (event.closes_on is None or event.closes_on > as_of):
            raise EventNotReleased(f"the {period} earnings event (released {event.release_date}) closes on "
                                   f"{event.closes_on} (the {'10-Q/10-K' if kind != edgar.FOREIGN else '6-K'} or five "
                                   f"business days); run on or after that date (prompt 03)")
        return self._results_documents(event, kind, cal, ticker, as_of, known_tags or {})

    def _results_documents(self, event: edgar.EarningsEvent, kind: str, cal: edgar.FiscalCalendar, ticker: str,
                           as_of: dt.date, known_tags: Mapping[str, str]) -> ResultsDocuments:
        documents: list[FilingText] = []
        notes: list[str] = []
        if kind == edgar.FOREIGN:
            if event.exhibit:
                documents.append(self._document(ticker, cal, event.filing, event.exhibit, known_tags))
            else:
                notes.append(f"the results 6-K {event.accession} names no press-release document")
        else:
            docs = edgar.filing_documents(event.cik, event.accession, client=self.client,
                                          primary_document=event.filing.primary_document)
            exhibits = [d for d in docs if d.kind == "exhibit" and (d.exhibit or "").startswith("EX-99")
                        and not d.name.lower().endswith(".pdf")]
            for doc in sorted(exhibits, key=_exhibit_key):
                documents.append(self._document(ticker, cal, event.filing, doc.name, known_tags, doc.exhibit))
            if not exhibits:
                notes.append(f"the results 8-K {event.accession} has no EX-99 exhibit")
        closing = event.closing
        if closing is not None and closing.filing_date <= as_of:
            documents.append(self._document(ticker, cal, closing, closing.primary_document, known_tags))
        elif kind != edgar.FOREIGN:
            notes.append(f"the periodic report for {event.period} was not on EDGAR as of {as_of}")
        return ResultsDocuments(event.period, event.period_end, event.release_date, documents, notes, event=event)

    def companyfacts(self, cik: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """EDGAR's XBRL companyfacts for one issuer and its source record (URL, sha256, size); re-fetched after the
        submissions TTL, like submissions (new filings add facts)."""
        url = f"{edgar.DATA_BASE}/api/xbrl/companyfacts/CIK{edgar.normalize_cik(cik)}.json"
        raw = self.client.get_bytes(url, max_age=self.client.ttl)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise edgar.EdgarDataError(f"companyfacts for CIK {cik} is not JSON: {exc}") from None
        return data, {"kind": "edgar", "detail": "XBRL companyfacts", "url": url, "sha256": sha256_bytes(raw),
                      "bytes": len(raw)}

    def where_filings(self, filer: edgar.Filer, period: str, as_of: dt.date, lookback: int,
                      plan: documents.WherePlan) -> tuple[list[documents.Selection], list[str], edgar.Submissions,
                                                          edgar.FiscalCalendar, str, str]:
        """The company's own filings a qualitative test's `where` names over its lookback, as of `as_of`."""
        fiscal_year, _ = edgar.parse_period(period)
        subs, cal, kind, ticker = self._context(filer, fiscal_year, years_back=max(2, lookback // 4 + 2))
        start = documents.window_start(period, lookback, cal)
        events = edgar.earnings_events(subs.cik, cal.label, filer_type=kind, since=start - dt.timedelta(days=100),
                                       until=as_of, client=self.client, subs=subs)
        selections, notes = documents.select_filings(plan.requests, subs.filings, events, period=period,
                                                     lookback=lookback, as_of=as_of, cal=cal,
                                                     foreign=kind == edgar.FOREIGN)
        return selections, notes, subs, cal, kind, ticker

    def selection_documents(self, selection: documents.Selection, *, ticker: str, cal: edgar.FiscalCalendar,
                            subs: edgar.Submissions, known_tags: Mapping[str, str]) -> list[FilingText]:
        """The documents of one selected filing: its primary document and/or its EX-99 exhibits."""
        filing = selection.filing
        tag = known_tags.get(filing.accession) or documents.tag_with_ordinal(ticker, filing, cal, subs.filings)
        out: list[FilingText] = []
        if selection.primary and filing.primary_document and selection.kind == documents.OWNERSHIP:
            rendered = self._ownership(ticker, cal, filing, known_tags, tag)
            if rendered is not None:
                return [rendered]
        if selection.primary and filing.primary_document:
            doc = self._document(ticker, cal, filing, filing.primary_document, known_tags, tag=tag)
            if selection.sections and documents.form_family(filing.form):
                text, found, missing = documents.extract_sections(doc.text, filing.form, selection.sections)
                if missing:
                    doc = dataclasses.replace(doc, note=f"the whole filing: section(s) not found by heading: "
                                                        f"{', '.join(documents.describe_sections(missing))}")
                else:
                    doc = dataclasses.replace(
                        doc, text=text, sections=tuple(documents.describe_sections(found)),
                        note=f"sections kept: {', '.join(documents.describe_sections(found))} "
                             f"({len(text):,} of {len(doc.text):,} characters)")
            out.append(doc)
        if selection.exhibits:
            docs = edgar.filing_documents(filing.cik, filing.accession, client=self.client,
                                          primary_document=filing.primary_document)
            exhibits = [d for d in docs if d.kind == "exhibit" and (d.exhibit or "").startswith("EX-99")
                        and not d.name.lower().endswith(".pdf")
                        and (not selection.only_exhibits or d.exhibit in selection.only_exhibits)]
            for doc in sorted(exhibits, key=_exhibit_key):
                out.append(self._document(ticker, cal, filing, doc.name, known_tags, doc.exhibit, tag=tag))
        return out

    def registered_documents(self, filer: edgar.Filer, accession: str, tag: str,
                             exhibit: str | None = None) -> list[FilingText]:
        """The document a registered source tag names: an EX-99 exhibit when the locator names one, else the
        filing's primary document. Empty when the accession is not among the issuer's filings."""
        subs = edgar.submissions(filer.cik, client=self.client)
        filing = subs.get(accession)
        if filing is None:
            return []
        cal = edgar.FiscalCalendar.parse(filer.fiscal_year_end or subs.fiscal_year_end)
        ticker = filer.ticker or str(filer.cik)
        known = {filing.accession: tag}
        if exhibit:
            docs = edgar.filing_documents(filing.cik, filing.accession, client=self.client,
                                          primary_document=filing.primary_document)
            wanted = [d for d in docs if d.exhibit == exhibit and not d.name.lower().endswith(".pdf")]
            return [self._document(ticker, cal, filing, d.name, known, d.exhibit) for d in wanted[:1]]
        if not filing.primary_document:
            return []
        return [self._document(ticker, cal, filing, filing.primary_document, known)]

    def _ownership(self, ticker: str, cal: edgar.FiscalCalendar, filing: edgar.Filing, known_tags: Mapping[str, str],
                   tag: str) -> FilingText | None:
        """A Form 3, 4 or 5 from its XML in a few lines (documents.render_ownership), or None to fall back to the
        rendered page. The primary document xslF345X06/<name>.xml is the rendering; <name>.xml is the data."""
        name = filing.primary_document.rsplit("/", 1)[-1]
        if not name.lower().endswith(".xml"):
            return None
        url = f"{filing.folder_url}/{name}"
        try:
            raw = self.client.get_bytes(url)
        except edgar.EdgarError:
            return None
        text = documents.render_ownership(raw)
        if text is None:
            return None
        return FilingText(tag=known_tags.get(filing.accession) or tag, locator=None,
                          registered=filing.accession in known_tags, form=filing.form, items=filing.items,
                          accession=filing.accession, filed=filing.filing_date, document=name, url=url,
                          raw_sha256=sha256_bytes(raw), text=text,
                          note="rendered from the form's XML; transaction prices left out (00 section H2)")

    def _document(self, ticker: str, cal: edgar.FiscalCalendar, filing: edgar.Filing, name: str,
                  known_tags: Mapping[str, str], exhibit: str | None = None, *, tag: str | None = None) -> FilingText:
        url = f"{filing.folder_url}/{name}"
        raw = self.client.get_bytes(url)
        if exhibit is None:
            kind, exhibit = edgar.classify_document(name, filing.primary_document)
            exhibit = exhibit if kind == "exhibit" else None
        registered = filing.accession in known_tags
        tag = known_tags.get(filing.accession) or tag or source_tag(ticker, filing, cal)
        return FilingText(tag=tag, locator=exhibit, registered=registered, form=filing.form, items=filing.items,
                          accession=filing.accession, filed=filing.filing_date, document=name, url=url,
                          raw_sha256=sha256_bytes(raw), text=document_text(raw, name))


def _exhibit_key(doc: edgar.Document) -> tuple[int, str]:
    number = (doc.exhibit or "").partition(".")[2]
    return (int(number) if number.isdigit() else 0), doc.name


# ---------------------------------------------------------------------------------------------------- calibration


def settled_predictions(public_root: Path) -> list[dict[str, Any]]:
    """Resolved predictions: forecasts/*.yml (the calibration book), then prereg settlements not already in it."""
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for path in sorted(public_root.glob("forecasts/*.yml")):
        data = load_yaml_file(path)
        for item in (data.get("forecasts") if isinstance(data, dict) else None) or []:
            if not isinstance(item, dict) or item.get("outcome") not in RESOLVED:
                continue
            book = "owner" if item.get("book") == "owner_override" else "system"
            records.append({"id": item.get("id"), "book": book, "probability": item.get("probability"),
                            "outcome": item["outcome"], "domain": item.get("domain") or "other"})
            seen.add((str(item.get("id")), book))
    for settlement in sorted(public_root.glob("companies/*/prereg/*.settlement.yml")):
        data = load_yaml_file(settlement)
        if not isinstance(data, dict):
            continue
        period = str(data.get("period") or settlement.name.split(".")[0])
        system_doc = load_yaml_file(settlement.with_name(f"{period}.yml")) or {}
        owner_doc = load_yaml_file(settlement.with_name(f"{period}-owner.yml")) or {}
        system_items = {i.get("id"): i for i in system_doc.get("items") or [] if isinstance(i, dict)}
        owner_items = {i.get("id"): i for i in owner_doc.get("items") or [] if isinstance(i, dict)}
        overrides = {o.get("id"): o.get("probability") for o in owner_doc.get("overrides") or [] if isinstance(o, dict)}
        for result in data.get("results") or []:
            if not isinstance(result, dict) or result.get("outcome") not in RESOLVED:
                continue
            rid = str(result.get("id"))
            rows = []
            if rid in system_items:
                item = system_items[rid]
                rows.append(("system", item.get("probability"), item.get("domain")))
                rows.append(("owner", overrides.get(rid, item.get("probability")), item.get("domain")))
            elif rid in owner_items:
                item = owner_items[rid]
                rows.append(("owner", item.get("probability"), item.get("domain")))
            for book, probability, domain in rows:
                if (rid, book) in seen:
                    continue
                seen.add((rid, book))
                records.append({"id": rid, "book": book, "probability": probability, "outcome": result["outcome"],
                                "domain": domain or "other"})
    return [r for r in records if isinstance(r["probability"], (int, float)) and not isinstance(r["probability"], bool)]


def _score(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pairs = [(float(r["probability"]), 1.0 if r["outcome"] == "happened" else 0.0) for r in rows]
    n = len(pairs)
    return {
        "n": n,
        "brier": round(sum((p - o) ** 2 for p, o in pairs) / n, 4),
        "mean_probability": round(sum(p for p, _ in pairs) / n, 4),
        "observed_frequency": round(sum(o for _, o in pairs) / n, 4),
    }


def calibration_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_domain: dict[str, list[dict[str, Any]]] = {}
    by_book: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_domain.setdefault(str(record["domain"]), []).append(record)
        by_book.setdefault(str(record["book"]), []).append(record)
    return {
        "overall": _score(records),
        "by_domain": {k: _score(v) for k, v in sorted(by_domain.items())},
        "by_book": {k: _score(v) for k, v in sorted(by_book.items())},
    }


# ---------------------------------------------------------------------------------------------------- assemblers

Assembler = Callable[[RunContext, str], BuiltInput]
INPUTS: dict[str, Assembler] = {}


def assembler(*names: str) -> Callable[[Assembler], Assembler]:
    def register(fn: Assembler) -> Assembler:
        for name in names:
            if name in INPUTS:
                raise ValueError(f"input {name!r} has two assemblers")
            INPUTS[name] = fn
        return fn

    return register


@assembler("run_date")
def _run_date(ctx: RunContext, name: str) -> BuiltInput:
    return BuiltInput(ctx.run_date.isoformat() + "\n", "txt", [{"kind": "parameter", "detail": "--run-date"}])


@assembler("thesis")
def _thesis(ctx: RunContext, name: str) -> BuiltInput:
    _need_company(ctx, name)
    path = ctx.thesis_path()
    if not path.is_file():
        raise MissingInput(f"companies/{ctx.company}/thesis.yml does not exist in {PUBLIC_REPO}")
    return BuiltInput(path.read_text(encoding="utf-8"), "yml", [repo_file_source(ctx.public_root, path, PUBLIC_REPO)])


@assembler("dossier")
def _dossier(ctx: RunContext, name: str) -> BuiltInput:
    company = _need_company(ctx, name)
    places = [(ctx.public_root, PUBLIC_REPO)] if company in _outputs.PUBLIC_DOSSIERS else []
    places.append((ctx.private_root, PRIVATE_REPO))
    for root, repo in places:
        path = root / "companies" / company / "dossier.md"
        if path.is_file():
            return BuiltInput(path.read_text(encoding="utf-8"), "md", [repo_file_source(root, path, repo)])
    report = ctx.workspace_root / "inputs" / "text" / f"reports__{company}.txt"
    if report.is_file():
        rel = report.resolve().relative_to(ctx.workspace_root.resolve()).as_posix()
        note = (f"companies/{company}/dossier.md does not exist yet; the owner's complete report text ({rel}) "
                "stands in for the 12-part dossier")
        return BuiltInput(report.read_text(encoding="utf-8"), "txt", [workspace_file_source(ctx.workspace_root, report)],
                          note=note, substitute="dossier")
    where = " or ".join(f"{repo}:companies/{company}/dossier.md" for _, repo in places)
    raise MissingInput(f"no dossier: neither {where} nor the owner's report text inputs/text/reports__{company}.txt exists")


@assembler("schema")
def _schema(ctx: RunContext, name: str) -> BuiltInput:
    names = schema_names_for(ctx.call)
    if not names:
        return empty_document(ctx, name, f"no output of {ctx.call.label} has a thesis-ci schema")
    chunks, sources = [], []
    for schema in names:
        path, origin = schema_file(schema, ctx.schemas_dir)
        text = path.read_text(encoding="utf-8")
        chunks.append(f"# {schema}.schema.json ({origin})\n{text.rstrip()}\n")
        sources.append({"kind": "thesis-ci", "schema": schema, "origin": origin, "sha256": sha256_text(text),
                        "digest": schema_digest(json.loads(text))})
    return BuiltInput("\n".join(chunks), "txt", sources)


CITE_NOTE = "Each document below opens with a header line that names its source tag; cite it as [src:TAG] or " \
            "[src:TAG#LOCATOR]."


def render_documents(lines: list[str], docs: Iterable[FilingText]) -> tuple[str, list[dict[str, Any]]]:
    """Header lines, then each document under a ===== [src:TAG] ... ===== line; and the documents' source records."""
    lines = list(lines)
    sources = []
    for doc in docs:
        items = f" (Items {', '.join(doc.items)})" if doc.items else ""
        lines += [f"===== [src:{doc.cite}] {doc.form}{items} | accession {doc.accession} | filed {doc.filed} | "
                  f"{doc.document} =====", f"url: {doc.url}"]
        lines += [f"note: {doc.note}"] if doc.note else []
        lines += ["", doc.text.strip(), ""]
        source = {"kind": "edgar", "tag": doc.cite, "registered": doc.registered, "form": doc.form,
                  "accession": doc.accession, "filed": doc.filed.isoformat(), "document": doc.document,
                  "url": doc.url, "sha256": doc.raw_sha256, "text_sha256": sha256_text(doc.text)}
        if doc.sections:
            source["sections"] = list(doc.sections)
        if doc.note:
            source["note"] = doc.note
        sources.append(source)
    return "\n".join(lines), sources


@assembler("latest_filings")
def _latest_filings(ctx: RunContext, name: str) -> BuiltInput:
    company = _need_company(ctx, name)
    known = known_filing_tags((ctx.public_root, ctx.private_root), company)
    results = ctx.gateway().latest_results(ctx.filer(), ctx.period, ctx.run_date, known)
    if not results.documents:
        raise MissingInput(f"EDGAR returned no readable document for {company} {results.period}")
    lines = [
        f"Latest quarter reported before {ctx.period}: {results.period} (quarter end {results.period_end}); "
        f"results first public on {results.release_date} (US Eastern).",
        CITE_NOTE,
        *[f"Note: {note}." for note in results.notes],
        "",
    ]
    text, sources = render_documents(lines, results.documents)
    return BuiltInput(text, "txt", sources, note="; ".join(results.notes) or None)


@assembler("event")
def _event(ctx: RunContext, name: str) -> BuiltInput:
    """15A: the expected release and the pre-registration deadline. After the event (14T, 15B): the period, the release
    and the date the event closed."""
    company = _need_company(ctx, name)
    if ctx.step.post_event:
        results = ctx.results()
        data = {"company": company, "as_of": ctx.run_date.isoformat(), **results.event_record(ctx.run_date)}
        event = results.event
        sources = [{"kind": "edgar", "detail": f"pipeline.edgar.earnings_events: {results.period} results "
                                               f"{event.form if event else ''} {event.accession if event else ''}".strip(),
                    "cik": event.cik if event else None, "as_of": ctx.run_date.isoformat()}]
        built = BuiltInput(dump_yaml(data), "yml", sources)
        if results.rehearsal_for:
            built.substitute, built.note = "event", results.notes[-1]
        return built
    estimate = ctx.estimate()
    data = {"company": company, "as_of": ctx.run_date.isoformat()}
    data.update({k: v for k, v in estimate.to_dict().items() if k != "history"})
    sources = [{"kind": "edgar", "detail": "pipeline.edgar.next_release", "cik": estimate.cik,
                "status": estimate.status, "as_of": ctx.run_date.isoformat()}]
    return BuiltInput(dump_yaml(data), "yml", sources)


@assembler("release_history")
def _release_history(ctx: RunContext, name: str) -> BuiltInput:
    company = _need_company(ctx, name)
    estimate = ctx.estimate()
    rows = estimate.to_dict()["history"]
    fiscal_year, quarter = edgar.parse_period(ctx.period)
    data = {
        "company": company,
        "quarter": quarter,
        "periods": [f"FY{fiscal_year - k}Q{quarter}" for k in range(edgar.HISTORY_YEARS, 0, -1)],
        "rule": "results releases of the same fiscal quarter in the last three fiscal years, from EDGAR",
        "events": rows,
    }
    sources = [{"kind": "edgar", "detail": "pipeline.edgar.next_release history", "cik": estimate.cik,
                "accessions": [row["accession"] for row in rows]}]
    if not rows:
        reason = f"EDGAR has no results release of fiscal quarter {quarter} in the last {edgar.HISTORY_YEARS} years"
        data["status"] = "empty"
        data["reason"] = reason
        return BuiltInput(dump_yaml(data), "yml", sources, note=reason, empty=True)
    return BuiltInput(dump_yaml(data), "yml", sources)


def _is_prereg_candidate(entry: Any) -> bool:
    """A todo entry whose label (the text before the first colon) names a pre-registration candidate."""
    if not isinstance(entry, str):
        return False
    low = entry.split(_LABEL_SEPARATOR, 1)[0].casefold()
    return any(mark in low for mark in PREREG_CANDIDATE_MARKS)


@assembler("prereg_candidates")
def _prereg_candidates(ctx: RunContext, name: str) -> BuiltInput:
    company = _need_company(ctx, name)
    from_runs, sources = [], []
    for run in ctx.runs():
        if run.scope != company or run.part not in ("10", "13"):
            continue
        text = run.read_output("prereg_candidates")
        if text is None:
            continue
        from_runs.append({"run": run.rel, "candidates": yaml.safe_load(text)})
        sources.append({"kind": "run_output", "run": run.rel, "output": "prereg_candidates",
                        "sha256": sha256_text(text)})
    todo = ctx.thesis().get("todo") or []
    from_todo = [entry for entry in todo if _is_prereg_candidate(entry)]
    if from_todo:
        sources.append(repo_file_source(ctx.public_root, ctx.thesis_path(), PUBLIC_REPO) | {"field": "todo"})
    if not from_runs and not from_todo:
        return empty_document(ctx, name, "no 10 or 13 run has handed over pre-registration candidates, and "
                                         "thesis.yml's todo has no pre-registration candidate entry")
    data = {
        "company": company,
        "period": ctx.period,
        "from_runs": from_runs,
        "from_thesis_todo": from_todo,
        "selection": "todo entries whose label (text before the first colon) names a pre-registration candidate; "
                     "prereg_candidates outputs of 10 and 13 runs for this company",
    }
    return BuiltInput(dump_yaml(data), "yml", sources)


def _frozen_question_list(ctx: RunContext, name: str) -> tuple[PriorRun, str]:
    """The question list HQ froze for this company and period: the placed output of the latest 14Q run. A rehearsal
    also takes the unplaced output of a 14Q dry run in its own directory (dry runs are never placed)."""
    company = _need_company(ctx, name)
    runs = [r for r in ctx.runs() if r.scope == company and r.part == "14Q" and r.period == ctx.period
            and r.placed_file("question_list") is not None]
    if runs:
        path = runs[-1].placed_file("question_list")
        return runs[-1], path.read_text(encoding="utf-8") if path else ""
    rehearsed = [r for r in ctx.runs() if ctx.rehearsal and r.succeeded and r.step == "14Q" and r.company == company
                 and r.period == ctx.period and any(_inside(r.path, root) for root in ctx.runs_roots)
                 and r.output_file("question_list") is not None]
    if rehearsed:
        return rehearsed[-1], rehearsed[-1].read_output("question_list") or ""
    raise MissingInput(f"no placed 14Q question list for {company} {ctx.period} (run 14Q, merge it, then place it)")


@assembler("question_list")
def _question_list(ctx: RunContext, name: str) -> BuiltInput:
    if ctx.step.step == "03-draft" and not ctx.is_holding():
        raise Omit("candidates have no question list (prompt 14 scope); question_answers is none")
    run, text = _frozen_question_list(ctx, name)
    return BuiltInput(text, "yml", [{"kind": "run_output", "run": run.rel, "output": "question_list",
                                     "sha256": sha256_text(text)}])


@assembler("question_list_stripped")
def _question_list_stripped(ctx: RunContext, name: str) -> BuiltInput:
    """14A (blind read): kind and maps_to removed and non-open questions shuffled (isolation.py, 00 section G6)."""
    run, text = _frozen_question_list(ctx, name)
    seed = f"{run.rel}:{ctx.run_date.isoformat()}"
    stripped = isolation.question_list_stripped(text, seed=seed)
    source = {"kind": "run_output", "run": run.rel, "output": "question_list", "sha256": sha256_text(text),
              "transform": "isolation.question_list_stripped", "seed": seed}
    return BuiltInput(stripped, "yml", [source], note="kind and maps_to removed; non-open questions shuffled")


@assembler("thesis_without_loss_paths")
def _thesis_without_loss_paths(ctx: RunContext, name: str) -> BuiltInput:
    """04B and 04B-lite (red team): thesis.permanent_loss_paths removed (isolation.py, 00 section G6)."""
    built = _thesis(ctx, name)
    stripped = isolation.thesis_without_loss_paths(built.text)
    source = {**built.sources[0], "transform": "isolation.thesis_without_loss_paths"}
    return BuiltInput(stripped, "yml", [source], note="thesis.permanent_loss_paths removed")


@assembler("calibration")
def _calibration(ctx: RunContext, name: str) -> BuiltInput:
    records = settled_predictions(ctx.public_root)
    sources = [{"kind": "repo_glob", "repo": PUBLIC_REPO,
                "paths": ["forecasts/*.yml", "companies/*/prereg/*.settlement.yml"]}]
    if not records:
        built = empty_document(ctx, name, "no settled predictions yet: no resolved forecast in forecasts/*.yml and "
                                          "no resolved result in companies/*/prereg/*.settlement.yml", settled=0)
        built.sources = sources
        return built
    data = {"as_of": ctx.run_date.isoformat(), "status": "ok", "settled": len(records), **calibration_summary(records)}
    return BuiltInput(dump_yaml(data), "yml", sources)


# ---- monthly letter (18): the month is ctx.period (YYYY-MM)


def _runs_in_month(ctx: RunContext) -> list[PriorRun]:
    first, last = ctx.month()
    return [run for run in ctx.runs() if first <= run.run_date <= last]


def _run_sources(runs: Iterable[PriorRun], output: str) -> list[dict[str, Any]]:
    out = []
    for run in runs:
        path = run.output_file(output)
        if path is not None:
            out.append({"kind": "run_output", "run": run.rel, "output": output,
                        "sha256": sha256_bytes(path.read_bytes())})
    return out


@assembler("month_events")
def _month_events(ctx: RunContext, name: str) -> BuiltInput:
    first, last = ctx.month()
    runs = [{"run": r.rel, "part": r.part, "status": r.status,
             **({"cost_usd": r.record.get("cost_usd")} if r.record and "cost_usd" in r.record else {})}
            for r in _runs_in_month(ctx)]
    public = git_commits(ctx.public_root, first, last)
    private = git_commits(ctx.private_root, first, last)
    if not runs and not public and not private:
        reason = f"no pipeline run and no commit in either repository in {ctx.period}"
        if public is None or private is None:
            reason += " (git history unavailable for at least one repository)"
        return empty_document(ctx, name, reason)
    data = {
        "month": ctx.period,
        "pipeline_runs": runs,
        "public_commits": public if public is not None else "git history unavailable",
        "private_commits": private if private is not None else "git history unavailable",
    }
    sources = [{"kind": "runs", "repo": PRIVATE_REPO, "path": "runs/"},
               {"kind": "git_log", "repo": PUBLIC_REPO, "from": first.isoformat(), "to": last.isoformat()},
               {"kind": "git_log", "repo": PRIVATE_REPO, "from": first.isoformat(), "to": last.isoformat()}]
    return BuiltInput(dump_yaml(data), "yml", sources)


@assembler("updates")
def _updates(ctx: RunContext, name: str) -> BuiltInput:
    first, last = ctx.month()
    picked = [p for p in sorted(ctx.public_root.glob("companies/*/updates/*.md"))
              if (day := _date_prefix(p.name)) is not None and first <= day <= last]
    if not picked:
        return empty_document(ctx, name, f"no quarterly update is dated in {ctx.period} (companies/*/updates/)")
    chunks = [f"<!-- {p.relative_to(ctx.public_root).as_posix()} -->\n{p.read_text(encoding='utf-8').rstrip()}\n"
              for p in picked]
    return BuiltInput("\n".join(chunks), "md", [repo_file_source(ctx.public_root, p, PUBLIC_REPO) for p in picked])


def _letter_entries(data: Any) -> list[Any] | None:
    """The per-question entries of a divergence map: a list, or the single list inside a mapping."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        lists = [v for v in data.values() if isinstance(v, list)]
        if len(lists) == 1:
            return lists[0]
    return None


@assembler("divergence_map")
def _divergence_map(ctx: RunContext, name: str) -> BuiltInput:
    """18: what the month's divergence maps flag for the letter. 17A: this event's divergence map."""
    if ctx.step.step == "17A":
        return _event_output(ctx, name, "14B", "divergence_map",
                             "the divergence map (14B) runs only for holdings' quarterly updates (prompt 14 scope)")
    runs = [r for r in _runs_in_month(ctx) if r.part == "14B"]
    maps = []
    for run in runs:
        text = run.read_output("divergence_map")
        if text is None:
            continue
        data = yaml.safe_load(text)
        entries = _letter_entries(data)
        if entries is None:
            maps.append({"run": run.rel, "divergence_map": data, "note": "structure not recognised; whole map included"})
            continue
        flagged = [e for e in entries if isinstance(e, dict) and e.get("to_owner_letter")]
        if flagged:
            maps.append({"run": run.rel, "to_owner_letter": flagged})
    if not maps:
        return empty_document(ctx, name, f"no divergence map of {ctx.period} marks anything for the owner's letter")
    return BuiltInput(dump_yaml({"month": ctx.period, "divergences": maps}), "yml",
                      _run_sources(runs, "divergence_map"))


def _collect_outputs(ctx: RunContext, name: str, output: str, parts: Iterable[str] | None, ext: str) -> BuiltInput:
    runs = [r for r in _runs_in_month(ctx) if parts is None or r.part in parts]
    chunks = []
    for run in runs:
        text = run.read_output(output)
        if text is not None and text.strip():
            chunks.append(f"<!-- {run.rel} ({run.part}, {run.run_date}) -->\n{text.rstrip()}\n")
    if not chunks:
        which = f" of {', '.join(parts)}" if parts else ""
        return empty_document(ctx, name, f"no run{which} in {ctx.period} produced {output}")
    return BuiltInput("\n".join(chunks), ext, _run_sources(runs, output))


@assembler("owner_letter_items")
def _owner_letter_items(ctx: RunContext, name: str) -> BuiltInput:
    return _collect_outputs(ctx, name, "owner_letter_items", ("17A",), "yml")


@assembler("l2_report")
def _l2_report(ctx: RunContext, name: str) -> BuiltInput:
    return _collect_outputs(ctx, name, "l2_report", None, "md")


@assembler("rulings")
def _rulings(ctx: RunContext, name: str) -> BuiltInput:
    return _collect_outputs(ctx, name, "rulings", ("17A",), "md")


@assembler("questions_open")
def _questions_open(ctx: RunContext, name: str) -> BuiltInput:
    _, last = ctx.month()
    runs = [r for r in ctx.runs() if r.run_date <= last]
    found = []
    for run in runs:
        text = run.read_output("questions")
        if text is None:
            continue
        data = yaml.safe_load(text)
        if data is None or _outputs.is_empty_mark(data):
            continue
        found.append({"run": run.rel, "questions": data})
    if not found:
        return empty_document(ctx, name, f"no run up to {last} raised a question")
    data = {
        "as_of": last.isoformat(),
        "closure": "the pipeline does not record yet which questions HQ has closed; every question raised so far "
                   "is listed, grouped by run",
        "runs": found,
    }
    return BuiltInput(dump_yaml(data), "yml", _run_sources(runs, "questions"))


@assembler("trust_changes")
def _trust_changes(ctx: RunContext, name: str) -> BuiltInput:
    first, last = ctx.month()
    rel = "trust/levels.yml"
    path = ctx.public_root / rel
    current = load_yaml_file(path) if path.is_file() else None
    if not isinstance(current, dict):
        return empty_document(ctx, name, f"{rel} does not exist in {PUBLIC_REPO}")
    before_text = git_file_before(ctx.public_root, rel, first)
    after_text = git_file_before(ctx.public_root, rel, last + dt.timedelta(days=1))
    before = yaml.safe_load(before_text) if before_text else None
    after = yaml.safe_load(after_text) if after_text else current
    levels = {group: current.get(group) for group in ("companies", "industries")}
    sources = [repo_file_source(ctx.public_root, path, PUBLIC_REPO)]
    if not isinstance(before, dict):
        return empty_document(ctx, name, f"{rel} has no committed version before {ctx.period}; no change to report",
                              current_levels=levels)
    changes = []
    for group in ("companies", "industries"):
        old, new = before.get(group) or {}, (after or {}).get(group) or {}
        for key in sorted(set(old) | set(new)):
            if old.get(key) != new.get(key):
                changes.append({"group": group, "who": key, "from": old.get(key), "to": new.get(key)})
    if not changes:
        built = empty_document(ctx, name, f"no trust level changed in {ctx.period}", current_levels=levels)
        built.sources = sources
        return built
    return BuiltInput(dump_yaml({"month": ctx.period, "changes": changes, "current_levels": levels}), "yml", sources)


@assembler("memos")
def _memos(ctx: RunContext, name: str) -> BuiltInput:
    first, last = ctx.month()
    picked = [p for p in sorted(ctx.private_root.glob("memos/*.yml"))
              if (day := _date_prefix(p.name)) is not None and first <= day <= last]
    if not picked:
        return empty_document(ctx, name, f"no L3 memo is dated in {ctx.period} (memos/ in {PRIVATE_REPO})")
    data = {"month": ctx.period, "memos": [{"file": p.relative_to(ctx.private_root).as_posix(),
                                            "memo": load_yaml_file(p)} for p in picked]}
    return BuiltInput(dump_yaml(data), "yml", [repo_file_source(ctx.private_root, p, PRIVATE_REPO) for p in picked])


@assembler("budget")
def _budget(ctx: RunContext, name: str) -> BuiltInput:
    first, last = ctx.month()
    log = ctx.private_root / LLM_LOG_REL
    moment = dt.datetime.combine(last, dt.time(23, 59, 59), tzinfo=dt.timezone.utc)
    status = llm.budget_status(log_path=log, repo_root=ctx.public_root, now=moment)
    calls: dict[str, int] = {}
    backends: dict[str, dict[str, Any]] = {}
    if log.is_file():
        for line in log.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and str(record.get("timestamp") or "")[:7] == ctx.period:
                key = str(record.get("part_id") or record.get("prompt_id"))
                calls[key] = calls.get(key, 0) + 1
                row = backends.setdefault(str(record.get("backend") or llm.API), {"requests": 0, "tokens": 0})
                row["requests"] += 1
                row["tokens"] += sum(v for v in (record.get("usage") or {}).values() if isinstance(v, int))
    data = {"month": ctx.period, **status, "requests": sum(calls.values()), "requests_by_part": calls,
            "by_backend": backends,
            "budget_rule": "only API requests count against budget_usd; claude-code requests run on the owner's "
                           "Claude subscription (decisions/0022)",
            "log": LLM_LOG_REL}
    note = None
    if not log.is_file():
        note = f"{LLM_LOG_REL} does not exist yet: no model call has gone through the pipeline"
        data["note"] = note
    sources = [{"kind": "call_log", "repo": PRIVATE_REPO, "path": LLM_LOG_REL,
                "sha256": sha256_bytes(log.read_bytes()) if log.is_file() else None}]
    rights = ctx.public_root / "constitution" / "decision-rights.yml"
    if rights.is_file():
        sources.append(repo_file_source(ctx.public_root, rights, PUBLIC_REPO))
    return BuiltInput(dump_yaml(data), "yml", sources, note=note)


@assembler("failures")
def _failures(ctx: RunContext, name: str) -> BuiltInput:
    _, last = ctx.month()
    failed = []
    for run in _runs_in_month(ctx):
        records = [*run.attempts, *([run.record] if run.record is not None else [])]
        for number, record in enumerate(records, start=1):
            if record.get("status") == "failed":
                failed.append({"run": run.rel, "part": run.part, "attempt": number,
                               "error": (record.get("error") or {}).get("type")})
    streaks = []
    by_step: dict[tuple[str, str], list[str]] = {}
    for run in ctx.runs():
        if run.run_date <= last:
            by_step.setdefault((run.scope, run.part), []).extend(run.statuses)
    for (scope, part), statuses in sorted(by_step.items()):
        streak = 0
        for status in statuses:
            streak = streak + 1 if status == "failed" else 0
        if streak >= 2:
            streaks.append({"scope": scope, "part": part, "consecutive_failures": streak})
    if not failed and not streaks:
        return empty_document(ctx, name, f"no pipeline run failed in {ctx.period}")
    return BuiltInput(dump_yaml({"month": ctx.period, "failed_runs": failed, "repeated_failures": streaks}), "yml",
                      [{"kind": "runs", "repo": PRIVATE_REPO, "path": "runs/"}])


@assembler("phase_status")
def _phase_status(ctx: RunContext, name: str) -> BuiltInput:
    status = ctx.public_root / "docs" / "STATUS.md"
    if not status.is_file():
        raise MissingInput(f"docs/STATUS.md does not exist in {PUBLIC_REPO}")
    reports = sorted((ctx.public_root / "docs" / "acceptance").glob("*.md"))
    lines = [f"<!-- docs/STATUS.md -->\n{status.read_text(encoding='utf-8').rstrip()}\n", "<!-- docs/acceptance/ -->"]
    lines += [f"- {p.relative_to(ctx.public_root).as_posix()}" for p in reports] or ["- (no acceptance report yet)"]
    sources = [repo_file_source(ctx.public_root, status, PUBLIC_REPO)]
    sources += [repo_file_source(ctx.public_root, p, PUBLIC_REPO) for p in reports]
    return BuiltInput("\n".join(lines) + "\n", "md", sources)


def _upcoming_events(ctx: RunContext, start: dt.date, end: dt.date) -> list[dict[str, Any]]:
    """Results releases expected in [start, end] for holdings and candidates, from EDGAR (placeholder dates flagged)."""
    rows = []
    for thesis_path in sorted(ctx.public_root.glob("companies/*/thesis.yml")):
        data = load_yaml_file(thesis_path)
        if not isinstance(data, dict) or data.get("status") not in ("holding", "candidate"):
            continue
        company = thesis_path.parent.name
        try:
            filer = edgar.load_filer(company, ctx.public_root)
            cal = edgar.FiscalCalendar.parse(filer.fiscal_year_end or "12-31")
        except ValueError:
            continue
        day = start - dt.timedelta(days=100)
        seen = set()
        while day <= end:
            quarter_end = month_end(day)
            day = quarter_end + dt.timedelta(days=1)
            if quarter_end > end - dt.timedelta(days=10):
                continue
            try:
                period = cal.quarter_label(quarter_end)
            except ValueError:
                continue  # not a quarter end of this company's fiscal year
            if period in seen:
                continue
            seen.add(period)
            try:
                estimate = ctx.gateway().release_estimate(filer, period, ctx.run_date)
            except edgar.MissingUserAgent:
                raise
            except edgar.EdgarError as exc:
                rows.append({"company": company, "period": period, "error": type(exc).__name__})
                continue
            if estimate.status != "released" and start <= estimate.expected_release <= end:
                rows.append({"company": company, "status": data.get("status"), "period": period,
                             "expected_release": estimate.expected_release.isoformat(),
                             "placeholder": estimate.placeholder, "prereg_deadline": edgar.iso(estimate.deadline),
                             "merge_by": edgar.iso(estimate.merge_by)})
    return rows


@assembler("prereg_activity")
def _prereg_activity(ctx: RunContext, name: str) -> BuiltInput:
    first, last = ctx.month()
    registered, settled = [], []
    for path in sorted(ctx.public_root.glob("companies/*/prereg/*.yml")):
        rel = path.relative_to(ctx.public_root).as_posix()
        data = load_yaml_file(path)
        if not isinstance(data, dict):
            continue
        if path.name.endswith(".settlement.yml"):
            results = [r for r in data.get("results") or [] if isinstance(r, dict)
                       and (day := _date_prefix(str(r.get("settled_at") or ""))) and first <= day <= last]
            if results:
                counts: dict[str, int] = {}
                for result in results:
                    counts[str(result.get("outcome"))] = counts.get(str(result.get("outcome")), 0) + 1
                settled.append({"file": rel, "settled": len(results), "outcomes": counts})
            continue
        added = git_first_added(ctx.public_root, rel)
        if added is not None and first <= added <= last:
            registered.append({"file": rel, "merged": added.isoformat(), "author": data.get("author"),
                               "items": len(data.get("items") or []), "deadline": data.get("deadline"),
                               "event": data.get("event")})
    next_first = last + dt.timedelta(days=1)
    next_last = month_end(next_first)
    upcoming: Any
    try:
        upcoming = _upcoming_events(ctx, next_first, next_last)
    except (edgar.EdgarError, OSError) as exc:
        upcoming = f"unavailable: EDGAR lookup failed ({type(exc).__name__})"
    data = {"month": ctx.period, "registered": registered, "settled": settled,
            "upcoming_next_month": upcoming, "next_month": next_first.strftime("%Y-%m")}
    sources = [{"kind": "repo_glob", "repo": PUBLIC_REPO, "paths": ["companies/*/prereg/*.yml"]},
               {"kind": "edgar", "detail": "pipeline.edgar.next_release for holdings and candidates"}]
    note = None
    if not registered and not settled:
        note = f"no pre-registration was merged or settled in {ctx.period}"
        data["note"] = note
    return BuiltInput(dump_yaml(data), "yml", sources, note=note)


@assembler("mistakes")
def _mistakes(ctx: RunContext, name: str) -> BuiltInput:
    first, last = ctx.month()
    path = ctx.public_root / "mistakes.md"
    if not path.is_file():
        raise MissingInput(f"mistakes.md does not exist in {PUBLIC_REPO}")
    text = path.read_text(encoding="utf-8")
    starts = [(m.start(), dt.date.fromisoformat(m.group(1))) for m in _MISTAKE_HEADING_RE.finditer(text)]
    sections = []
    for i, (start, day) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(text)
        if first <= day <= last:
            sections.append(text[start:end].split("\n## ", 1)[0].rstrip() + "\n")
    source = [repo_file_source(ctx.public_root, path, PUBLIC_REPO)]
    if not sections:
        latest = max((day for _, day in starts), default=None)
        built = empty_document(ctx, name, f"mistakes.md has no entry dated in {ctx.period}",
                               latest_entry=latest.isoformat() if latest else None)
        built.sources = source
        return built
    return BuiltInput("\n".join(sections), "md", source)


# ---------------------------------------------------------------------------------------------------- after the event
# docs/decisions/0024. Upstream outputs are the succeeded runs of the same company and period (RunContext.latest_run).


@assembler("filings")
def _filings(ctx: RunContext, name: str) -> BuiltInput:
    """The documents of this earnings event: the results release (8-K EX-99 exhibits, or the results 6-K) and the
    10-Q or 10-K once filed. Call transcripts are not on EDGAR (STATUS T17)."""
    company = _need_company(ctx, name)
    results = ctx.results()
    if not results.documents:
        raise MissingInput(f"EDGAR returned no readable document for {company} {results.period}")
    lines = [
        f"The filings of {company}'s {results.period} earnings event (quarter end {results.period_end}); results first "
        f"public on {results.release_date} (US Eastern).",
        CITE_NOTE,
        "Earnings call transcripts are not included: they are not on EDGAR and the pipeline has no source for them "
        "yet.",
        *[f"Note: {note}." for note in results.notes],
        "",
    ]
    documents_ = list(results.documents)
    if ctx.step.step == "15B":
        extra, rows = settlement_filings(ctx)
        have = {(d.accession, d.document) for d in documents_}
        documents_ += [d for d in extra if (d.accession, d.document) not in have]
        if rows:
            lines[-1:-1] = ["The documents the due items' data_source names follow the event's own:",
                            dump_yaml({"items": rows}).rstrip()]
    text, sources = render_documents(lines, documents_)
    built = BuiltInput(text, "txt", sources, note="; ".join(results.notes) or None)
    if results.rehearsal_for:
        built.substitute = "filings"
    return built


def _as_of(ctx: RunContext) -> dt.date:
    """The date EDGAR is read as of: the run date, or today in a rehearsal (the run date is in the future)."""
    return min(ctx.run_date, dt.date.today()) if ctx.rehearsal else ctx.run_date


def _event_period(ctx: RunContext) -> str:
    """The period whose documents are read: the event's, or in a rehearsal the quarter that stands in for it."""
    return ctx.results().period if ctx.rehearsal else ctx.period


# ---- 16B: metric extraction


def metric_registry(ctx: RunContext) -> tuple[dict[str, dict[str, Any]], str]:
    return ctx.remember("metric_registry", lambda: evaluation.load_metric_registry(ctx.schemas_dir))


@assembler("metric_definitions")
def _metric_definitions(ctx: RunContext, name: str) -> BuiltInput:
    """The definitions of the metrics the due quantitative tests read from filing text: id, description, unit,
    frequency, where, formula, components, params. No thresholds, claims or notes (the extractor sees no thesis)."""
    company = _need_company(ctx, name)
    registry, origin = metric_registry(ctx)
    definitions, skipped, by_metric = evaluation.text_metric_definitions(ctx.thesis(), ctx.period, ctx.run_date,
                                                                         registry)
    sources = [repo_file_source(ctx.public_root, ctx.thesis_path(), PUBLIC_REPO)
               | {"field": "tests", "transform": "metric definitions only (thresholds, claims and notes removed)",
                  "tests_by_metric": by_metric},
               {"kind": "thesis-ci", "detail": "metrics registry (spec/metrics.yml)", "origin": origin}]
    if not definitions:
        built = empty_document(ctx, name, f"no due test of {company} reads a metric from filing text in {ctx.period}",
                               not_read_this_period=skipped)
        built.sources = sources
        return built
    data = {"company": company, "period": ctx.period, "naming": evaluation.NAMING, "metrics": definitions}
    if skipped:
        data["not_read_this_period"] = skipped
    return BuiltInput(dump_yaml(data), "yml", sources)


def _period_key(period: str | None) -> int:
    try:
        return documents.period_index(str(period))
    except ValueError:
        return -1


@assembler("prior_values")
def _prior_values(ctx: RunContext, name: str) -> BuiltInput:
    """The latest earlier period's 16B readings, to check that the basis is consistent."""
    company = _need_company(ctx, name)
    now = documents.period_index(ctx.period)
    earlier = [r for r in ctx.runs() if r.succeeded and r.step == "16B" and r.company == company
               and -1 < _period_key(r.period) < now and r.output_file("metric_values") is not None]
    if not earlier:
        raise Omit(f"no earlier 16B run for {company}: this is the first period read from filing text")
    run = max(earlier, key=lambda r: (_period_key(r.period), r.run_date))
    text = run.read_output("metric_values") or ""
    return BuiltInput(text, "yml", run.outputs_used(["metric_values"]), note=f"metric_values of {run.period} ({run.rel})")


# ---- 14T: qualitative tests


QUALITATIVE_FIELDS = ("id", "question", "fail_if", "warn_if", "judge_notes")
BASELINE_FIELDS = ("value", "unit", "period", "as_of", "source")  # baseline.note may carry thesis wording


def due_qualitative(ctx: RunContext) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    return ctx.remember("due_qualitative",
                        lambda: evaluation.due_tests(ctx.thesis(), "qualitative", ctx.period, ctx.run_date))


@assembler("qualitative_tests")
def _qualitative_tests(ctx: RunContext, name: str) -> BuiltInput:
    """The due qualitative tests with id, question, fail_if, warn_if, judge_notes and the baseline's value and source;
    claim and note (thesis wording) are removed (prompt 14T, 00 section G6)."""
    company = _need_company(ctx, name)
    due, skipped = due_qualitative(ctx)
    source = repo_file_source(ctx.public_root, ctx.thesis_path(), PUBLIC_REPO) | {
        "field": "tests", "tests": [t.get("id") for t in due],
        "transform": "qualitative tests only; claim, note and baseline.note removed"}
    if not due:
        built = empty_document(ctx, name, f"no qualitative test of {company} is due in {ctx.period}", not_due=skipped)
        built.sources = [source]
        return built
    tests = []
    for test in due:
        row = {key: test[key] for key in QUALITATIVE_FIELDS if test.get(key) is not None}
        baseline = test.get("baseline")
        if isinstance(baseline, Mapping):
            row["baseline"] = {key: baseline[key] for key in BASELINE_FIELDS if baseline.get(key) is not None}
        tests.append(row)
    data = {"company": company, "period": ctx.period, "tests": tests}
    note = f"{len(tests)} due; claim and note removed" + (f"; {len(skipped)} not due" if skipped else "")
    return BuiltInput(dump_yaml(data), "yml", [source], note=note)


@assembler("where_documents")
def _where_documents(ctx: RunContext, name: str) -> BuiltInput:
    """For each due qualitative test, the company's own EDGAR filings its `where` names over its `lookback`, and what
    the pipeline could not supply (pipeline/documents.py); then each document once, with its source tag."""
    company = _need_company(ctx, name)
    due, _ = due_qualitative(ctx)
    if not due:
        return empty_document(ctx, name, f"no qualitative test of {company} is due in {ctx.period}")
    thesis = ctx.thesis()
    filer = ctx.filer()
    gateway = ctx.gateway()
    known = known_filing_tags((ctx.public_root, ctx.private_root), company)
    owners = documents.owner_names(company, thesis.get("name"), short_name(thesis.get("name"), company))
    foreign = filer.type == edgar.FOREIGN
    period, as_of = _event_period(ctx), _as_of(ctx)
    merged: dict[str, documents.Selection] = {}  # one selection per filing, over every test
    per_test = []
    context = None
    for test in due:
        lookback = int(test.get("lookback") or 1)
        plan = documents.parse_where(test.get("where"), foreign=foreign, owners=owners)
        selections, notes, subs, cal, _, ticker = gateway.where_filings(filer, period, as_of, lookback, plan)
        context = (subs, cal, ticker)
        for selection in selections:
            old = merged.get(selection.filing.accession)
            merged[selection.filing.accession] = old.merged(selection) if old else selection
        per_test.append((test, lookback, plan, selections, notes, cal))
    fetched: dict[str, list[FilingText]] = {}
    for accession, selection in merged.items():
        subs, cal, ticker = context  # type: ignore[misc]  # set by the loop above: due is not empty
        fetched[accession] = gateway.selection_documents(selection, ticker=ticker, cal=cal, subs=subs,
                                                         known_tags=known)
    plan_rows = []
    for test, lookback, plan, selections, notes, cal in per_test:
        supplied = list(dict.fromkeys(doc.cite for s in selections for doc in fetched[s.filing.accession]))
        plan_rows.append({"test_id": test.get("id"), "where": plan.where, "lookback": lookback,
                          "periods": documents.lookback_periods(period, lookback),
                          "window": f"{documents.window_start(period, lookback, cal)} to {as_of}",
                          "supplied": supplied, "not_supplied": [*plan.not_supplied, *notes]})
    header = [
        f"Documents for the qualitative tests of {company} due in {ctx.period}, as of {as_of}.",
        "For each test: what its `where` names, the fiscal quarters its `lookback` covers, the documents supplied below "
        "and what the pipeline could not supply. Where `where` names sections of a 10-K, 10-Q or 20-F, only those "
        "sections are supplied (each document's note says which); where they cannot be found by the filing's headings, "
        "the whole filing is.",
        CITE_NOTE,
        "",
        dump_yaml({"tests": plan_rows}).rstrip(),
        "",
    ]
    if ctx.rehearsal and ctx.results().rehearsal_for:
        header.insert(1, f"Rehearsal: the event has not happened yet; the window ends with {period} instead.")
    ordered = sorted((d for docs in fetched.values() for d in docs), key=lambda d: (d.filed, d.accession,
                                                                                    d.locator or ""))
    text, sources = render_documents(header, ordered)
    missing = sum(len(row["not_supplied"]) for row in plan_rows)
    cut = sum(1 for d in ordered if d.sections)
    return BuiltInput(text, "txt", sources, note=f"{len(ordered)} document(s) for {len(plan_rows)} test(s), {cut} cut "
                                                 f"to the sections named; {missing} part(s) of `where` not supplied",
                      substitute=name if ctx.rehearsal and ctx.results().rehearsal_for else None)


# ---- 03: quarterly update


@assembler("ledger")
def _ledger(ctx: RunContext, name: str) -> BuiltInput:
    company = _need_company(ctx, name)
    path = ctx.public_root / "companies" / company / "ledger.yml"
    if not path.is_file():
        raise MissingInput(f"companies/{company}/ledger.yml does not exist in {PUBLIC_REPO}")
    return BuiltInput(path.read_text(encoding="utf-8"), "yml", [repo_file_source(ctx.public_root, path, PUBLIC_REPO)])


@assembler("constitution")
def _constitution(ctx: RunContext, name: str) -> BuiltInput:
    path = ctx.public_root / "constitution" / "owner.md"
    if not path.is_file():
        raise MissingInput(f"constitution/owner.md does not exist in {PUBLIC_REPO}")
    return BuiltInput(path.read_text(encoding="utf-8"), "md", [repo_file_source(ctx.public_root, path, PUBLIC_REPO)])


def _run_output(ctx: RunContext, name: str, step: str, output: str, *, why: str) -> tuple[PriorRun, str]:
    run = ctx.require_run(step, name, why=why)
    text = run.read_output(output)
    if text is None:
        raise MissingInput(f"{name}: {run.rel} has no {output} output")
    return run, text


@assembler("ci_results")
def _ci_results(ctx: RunContext, name: str) -> BuiltInput:
    """The evaluation of the quantitative tests (the ci step): readings, thresholds and results, computed."""
    run, text = _run_output(ctx, name, CI_STEP, "ci_results",
                            why="run `python -m pipeline.runner evaluate` after 16B")
    return BuiltInput(text, "yml", run.outputs_used(["ci_results"]))


@assembler("qualitative_verdicts")
def _qualitative_verdicts(ctx: RunContext, name: str) -> BuiltInput:
    """14T's rulings, and the qualitative tests that were not due this period."""
    company = _need_company(ctx, name)
    if not ctx.is_holding():
        raise Omit("the independent judge (14T) runs only for holdings; a candidate records every qualitative test as "
                   "undetermined (prompt 14 scope)")
    due, skipped = due_qualitative(ctx)
    if not due:
        return empty_document(ctx, name, f"no qualitative test of {company} was due in {ctx.period}",
                              not_judged=skipped)
    run, text = _run_output(ctx, name, "14T", "qualitative_verdicts", why="14T rules before the draft")
    data = {"judged_by": "the independent judge (14T)", "run": run.rel, "verdicts": yaml.safe_load(text)}
    if skipped:
        data["not_judged_this_period"] = skipped
    return BuiltInput(dump_yaml(data), "yml", run.outputs_used(["qualitative_verdicts"]))


def _event_month_end(ctx: RunContext) -> dt.date:
    """The end of the month in which the event closes (15B: items due by then are settled)."""
    results = ctx.results()
    closes = results.event.closes_on if results.event and results.event.closes_on else ctx.run_date
    return month_end(closes)


def prereg_items_due(ctx: RunContext) -> list[dict[str, Any]]:
    """Pre-registration items (system and owner) not settled yet whose resolves_by is no later than the end of the
    month in which this event closes (15B)."""
    company = _need_company(ctx, "prereg_due")
    cutoff = _event_month_end(ctx)
    folder = ctx.public_root / "companies" / company / "prereg"
    due = []
    for path in sorted(folder.glob("*.yml")):
        if path.name.endswith(".settlement.yml"):
            continue
        data = load_yaml_file(path)
        if not isinstance(data, dict):
            continue
        period = path.name.split(".")[0].removesuffix("-owner")
        settled = load_yaml_file(folder / f"{period}.settlement.yml") or {}
        done = {str(r.get("id")) for r in settled.get("results") or [] if isinstance(r, dict)
                and r.get("outcome") in RESOLVED}
        book = "owner" if path.stem.endswith("-owner") else "system"
        for item in data.get("items") or []:
            if not isinstance(item, dict) or str(item.get("id")) in done:
                continue
            resolves_by = _date_prefix(str(item.get("resolves_by") or ""))
            if resolves_by is not None and resolves_by <= cutoff:
                due.append({**item, "book": book, "file": path.relative_to(ctx.public_root).as_posix()})
    return due


def _prereg_sources(ctx: RunContext) -> list[dict[str, Any]]:
    return [{"kind": "repo_glob", "repo": PUBLIC_REPO, "paths": [f"companies/{ctx.company}/prereg/*.yml"]}]


@assembler("prereg_due")
def _prereg_due(ctx: RunContext, name: str) -> BuiltInput:
    """03: the pre-registration items due this period, with the probabilities given at the time."""
    company = _need_company(ctx, name)
    if not ctx.is_holding():
        raise Omit("candidates do not pre-register (prompt 15 scope)")
    items = prereg_items_due(ctx)
    if not items:
        built = empty_document(ctx, name, f"no pre-registration item of {company} is due by {_event_month_end(ctx)}")
        built.sources = _prereg_sources(ctx)
        return built
    return BuiltInput(dump_yaml({"company": company, "due_by": _event_month_end(ctx).isoformat(), "items": items}),
                      "yml", _prereg_sources(ctx))


@assembler("prereg_settlement")
def _prereg_settlement(ctx: RunContext, name: str) -> BuiltInput:
    company = _need_company(ctx, name)
    if not ctx.is_holding():
        raise Omit("candidates do not pre-register (prompt 15 scope)")
    if not prereg_items_due(ctx):
        return empty_document(ctx, name, f"no pre-registration item of {company} was due this period")
    run, text = _run_output(ctx, name, "15B", "prereg_settlement", why="15B settles the due items before the draft")
    return BuiltInput(text, "yml", run.outputs_used(["prereg_settlement"]))


def _ledger_due_date(value: Any) -> dt.date | None:
    """A ledger entry's due as a date: YYYY-MM-DD; YYYY (year end); YYYY-Qn (quarter end); YYYY-Hn (half end)."""
    text = str(value or "").strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return dt.date.fromisoformat(text)
        if re.fullmatch(r"\d{4}", text):
            return dt.date(int(text), 12, 31)
        match = re.fullmatch(r"(\d{4})-([QH])([1-4])", text)
        if match:
            year, kind, number = int(match.group(1)), match.group(2), int(match.group(3))
            month = number * 3 if kind == "Q" else number * 6
            return month_end(dt.date(year, month, 1))
    except ValueError:
        return None
    return None


def ledger_entries_due(ctx: RunContext) -> list[dict[str, Any]]:
    """Pending ledger entries due by the end of this event's quarter (15B settles them; 03 relays)."""
    company = _need_company(ctx, "ledger_due")
    data = load_yaml_file(ctx.public_root / "companies" / company / "ledger.yml")
    entries = data.get("entries") if isinstance(data, dict) else None
    fiscal_year, quarter = edgar.parse_period(ctx.period)
    period_end = ctx.calendar().quarter_end(fiscal_year, quarter or 4)
    due = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or entry.get("status") != "pending":
            continue
        day = _ledger_due_date(entry.get("due"))
        if day is not None and day <= period_end:
            due.append(entry)
    return due


@assembler("ledger_settlement")
def _ledger_settlement(ctx: RunContext, name: str) -> BuiltInput:
    """15B's settlement of the ledger entries due this period, or an explicit document that none was due."""
    company = _need_company(ctx, name)
    path = ctx.public_root / "companies" / company / "ledger.yml"
    sources = [repo_file_source(ctx.public_root, path, PUBLIC_REPO)] if path.is_file() else []
    if not ledger_entries_due(ctx):
        built = empty_document(ctx, name, f"no pending ledger entry of {company} is due by the end of {ctx.period}")
        built.sources = sources
        return built
    run, text = _run_output(ctx, name, "15B", "ledger_settlement", why="15B settles the due entries before the draft")
    return BuiltInput(text, "yml", run.outputs_used(["ledger_settlement"]) + sources)


# 03's pending_* inputs: content handed back by other steps and not merged yet (00 section F7).
PENDING: dict[str, tuple[str, tuple[str, ...]]] = {
    "pending_archive_patch": ("archive_patch", ("02", "05", "10", "11", "13")),
    "pending_test_proposals": ("test_proposals", ("04B", "10", "13")),
    "pending_ledger_entries": ("ledger_entries", ("10",)),
}
MERGING_STEPS = ("03R", "03P")


def last_merge(ctx: RunContext) -> PriorRun | None:
    """The latest 03R or 03P run of this company whose outputs were placed: patches handed back before it have been
    decided (adopted or returned in its patch_decisions)."""
    placed = [r for r in ctx.runs() if r.succeeded and r.step in MERGING_STEPS and r.company == ctx.company
              and _placed(r)]
    return max(placed, key=lambda r: (r.run_date, r.rel)) if placed else None


def _placed(run: PriorRun) -> bool:
    record = load_yaml_file(run.path / "placement.yml")
    return isinstance(record, dict) and not record.get("check_only")


@assembler(*PENDING)
def _pending(ctx: RunContext, name: str) -> BuiltInput:
    company = _need_company(ctx, name)
    output, parts = PENDING[name]
    merged = last_merge(ctx)
    runs = [r for r in ctx.runs() if r.company == company and r.step in parts
            and (merged is None or r.run_date > merged.run_date)]
    chunks, used = [], []
    for run in runs:
        text = run.read_output(output)
        if text is None or not text.strip() or _outputs.is_empty_mark(text):
            continue
        chunks.append({"run": run.rel, "part": run.step, output: yaml.safe_load(text)})
        used.append(run)
    since = f" since {merged.rel} was merged" if merged else ""
    if not chunks:
        built = empty_document(ctx, name, f"nothing waits to be merged: no {output} from {', '.join(parts)}{since}")
        built.sources = [{"kind": "runs", "repo": PRIVATE_REPO, "path": f"runs/{company}/"}]
        return built
    sources = [s for run in used for s in run.outputs_used([output])]
    return BuiltInput(dump_yaml({"company": company, "pending": chunks}), "yml", sources)


@assembler("owner_notes")
def _owner_notes(ctx: RunContext, name: str) -> BuiltInput:
    company = _need_company(ctx, name)
    path = ctx.private_root / "companies" / company / "owner_notes.md"
    if not path.is_file():
        raise Omit(f"no owner notes ({PRIVATE_REPO}:companies/{company}/owner_notes.md does not exist)")
    return BuiltInput(path.read_text(encoding="utf-8"), "md", [repo_file_source(ctx.private_root, path, PRIVATE_REPO)],
                      note="the owner's notes: leads only")


# ---- 16A in update mode, 04A, 03R


# The draft's outputs that carry facts, in the order 16A reads them; thesis.yml and ledger.yml go in as diffs.
PRODUCT_OUTPUTS = ("update", "pr_body", "question_answers", "dossier_changes", "mistakes_entry", "story",
                   "sources_additions")
DIFF_OUTPUTS = {"thesis": "companies/{company}/thesis.yml", "ledger": "companies/{company}/ledger.yml"}


def run_outputs_text(run: PriorRun, names: Iterable[str], title: str) -> tuple[str, list[dict[str, Any]]]:
    """A run's outputs, each under a ===== <kind> output: <name> ===== line."""
    lines, sources = [title, ""], []
    for name in names:
        path = run.output_file(name)
        if path is None:
            continue
        text = path.read_text(encoding="utf-8")
        lines += [f"===== output: {name} ({path.name}) =====", text.rstrip(), ""]
        sources.append({"kind": "run_output", "run": run.rel, "output": name, "sha256": sha256_text(text)})
    return "\n".join(lines) + "\n", sources


def _diff(old: str, new: str, label: str) -> str:
    import difflib

    lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(), f"current/{label}", f"draft/{label}",
                                      lineterm="", n=2))
    return "\n".join(lines) if lines else f"(no change to {label})"


def draft_outputs_names(run: PriorRun) -> list[str]:
    entries = (run.record or {}).get("outputs") or {}
    return [name for name, entry in entries.items() if isinstance(entry, dict) and entry.get("status") == "written"]


def content_of(text: str | None) -> str:
    """An output's content without generated_by (which differs between any two calls), for comparing a revision with
    what it revised."""
    if not text:
        return ""
    parts = _outputs.split_front_matter(text)
    if parts is not None:
        front = yaml.safe_load(parts[0]) or {}
        if isinstance(front, dict) and "generated_by" in front:
            front.pop("generated_by")
            return f"---\n{dump_yaml(front)}---\n{parts[1]}"
        return text
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return text
    if isinstance(data, dict) and "generated_by" in data:
        data.pop("generated_by")
        return dump_yaml(data)
    return text


def changed_outputs(before: PriorRun, after: PriorRun) -> list[str]:
    """The fact-carrying outputs (and thesis, ledger) a revision changed, generated_by aside."""
    return [n for n in (*PRODUCT_OUTPUTS, *DIFF_OUTPUTS)
            if content_of(before.read_output(n)) != content_of(after.read_output(n))]


def _revision_product(ctx: RunContext, name: str) -> BuiltInput:
    """16A's second round: only what the revision (03R) changed in the draft's fact-carrying outputs, as unified diffs
    (00: "04A runs once more, on the new and changed facts only")."""
    company = _need_company(ctx, name)
    draft = ctx.require_run("03-draft", name)
    revision = ctx.latest_run("03R", period=ctx.period, round_=1)
    if revision is None:
        raise MissingInput(f"{name}: no succeeded 03R run for {company} {ctx.period} (the second audit round follows it)")
    chunks = [f"The product under audit: what the revision {revision.rel} changed in the quarterly update draft "
              f"{draft.rel} of {company} for {ctx.period}, as unified diffs. Extract the facts on the added and changed "
              "lines only; the rest was audited in the first round.", ""]
    sources: list[dict[str, Any]] = []
    for output in changed_outputs(draft, revision):
        old, new = content_of(draft.read_output(output)), content_of(revision.read_output(output))
        chunks.append(f"===== changes: {output} (draft -> revision) =====\n{_diff(old, new, output)}\n")
        sources += revision.outputs_used([output])
    if not sources:
        built = empty_document(ctx, name, f"the revision {revision.rel} changed no fact-carrying output of the draft")
        built.sources = revision.outputs_used(["update"])
        return built
    table, table_sources = source_table(ctx)
    chunks.append(f"===== source table (public and private sources.yml) =====\n{table}")
    return BuiltInput("\n".join(chunks), "txt", sources + table_sources,
                      note=f"second round: the changes of {revision.rel}")


@assembler("product")
def _product(ctx: RunContext, name: str) -> BuiltInput:
    """16A in update mode: the 03 draft's outputs that carry facts, the changes to thesis.yml and ledger.yml as diffs
    against the current files, and the source table. In the second round, the revision's changes only."""
    company = _need_company(ctx, name)
    if ctx.round > 1:
        return _revision_product(ctx, name)
    run = ctx.require_run("03-draft", name, why="16A extracts the facts of the draft before the audit")
    text, sources = run_outputs_text(
        run, [n for n in PRODUCT_OUTPUTS if run.output_file(n) is not None],
        f"The product under audit: the quarterly update draft of {company} for {ctx.period} ({run.rel}). The outputs "
        "that carry facts follow, then the changes the draft makes to thesis.yml and ledger.yml (a unified diff "
        "against the current files; extract the facts on the changed and added lines only), then the source table.")
    chunks = [text]
    for output, template in DIFF_OUTPUTS.items():
        path = run.output_file(output)
        current = ctx.public_root / template.format(company=company)
        if path is None:
            continue
        new = path.read_text(encoding="utf-8")
        old = current.read_text(encoding="utf-8") if current.is_file() else ""
        chunks.append(f"===== changes: {current.name} =====\n{_diff(old, new, current.name)}\n")
        sources.append({"kind": "run_output", "run": run.rel, "output": output, "sha256": sha256_text(new),
                        "transform": f"unified diff against {PUBLIC_REPO}:{template.format(company=company)}"})
    table, table_sources = source_table(ctx)
    chunks.append(f"===== source table (public and private sources.yml) =====\n{table}")
    return BuiltInput("\n".join(chunks), "txt", sources + table_sources,
                      note=f"update mode: the draft {run.rel}")


def source_table(ctx: RunContext) -> tuple[str, list[dict[str, Any]]]:
    """Both repositories' companies/<T>/sources.yml as one YAML list, each entry with its visibility."""
    company = _need_company(ctx, "sources")
    entries, sources = [], []
    for root, repo, visibility in ((ctx.public_root, PUBLIC_REPO, "public"), (ctx.private_root, PRIVATE_REPO, "private")):
        path = root / "companies" / company / "sources.yml"
        data = load_yaml_file(path)
        if not isinstance(data, dict):
            continue
        entries += [{**e, "visibility": visibility} for e in data.get("sources") or [] if isinstance(e, dict)]
        sources.append(repo_file_source(root, path, repo))
    return dump_yaml({"sources": entries}), sources


def _fact_rows(data: Any) -> list[Any]:
    if isinstance(data, dict) and isinstance(data.get("facts"), list):
        return data["facts"]
    return data if isinstance(data, list) else []


def _tags_of(value: Any) -> list[str]:
    items = value if isinstance(value, list) else [value]
    return [str(x).strip() for x in items if isinstance(x, str) and x.strip()]


def cited_texts(ctx: RunContext, tags: Iterable[str]) -> tuple[dict[str, str], list[FilingText], list[str]]:
    """Texts of the documents the given tags cite: {tag or tag#locator: text}, the EDGAR documents fetched, and the
    tags that could not be resolved. Resolved are this event's filings, the company's filings registered in
    sources.yml with an accession, and the owner's report (inputs/text/reports__<T>.txt, a pointer only, 00 E2)."""
    company = _need_company(ctx, "sources")
    results = ctx.results()
    by_cite: dict[str, str] = {}
    docs: dict[tuple[str, str], FilingText] = {}
    for doc in results.documents:
        docs[(doc.accession, doc.document)] = doc
    registered: dict[str, dict[str, Any]] = {}
    for root in (ctx.public_root, ctx.private_root):
        data = load_yaml_file(root / "companies" / company / "sources.yml")
        for entry in (data.get("sources") if isinstance(data, dict) else None) or []:
            if isinstance(entry, dict) and entry.get("tag"):
                registered.setdefault(str(entry["tag"]), entry)
    unresolved: list[str] = []
    gateway = ctx.gateway()
    filer = ctx.filer()
    for cite in dict.fromkeys(tags):
        tag, _, locator = cite.partition("#")
        known = [d for d in docs.values() if d.tag == tag and (not locator or d.locator in (None, locator))]
        if known:
            continue
        entry = registered.get(tag)
        if entry and entry.get("accession") and str(entry.get("issuer_cik") or filer.cik).lstrip("0") == \
                filer.cik.lstrip("0"):
            try:
                fetched = gateway.registered_documents(filer, str(entry["accession"]), tag,
                                                       locator if locator.startswith("EX-") else None)
            except (edgar.EdgarError, MissingInput):
                fetched = []
            for doc in fetched:
                docs.setdefault((doc.accession, doc.document), doc)
            if fetched:
                continue
        if re.match(rf"^{re.escape(company)}-RPT\d+-", tag):
            report = ctx.workspace_root / "inputs" / "text" / f"reports__{company}.txt"
            if report.is_file():
                by_cite[tag] = report.read_text(encoding="utf-8")
                continue
        unresolved.append(cite)
    for doc in docs.values():
        by_cite.setdefault(doc.cite, doc.text)
        by_cite.setdefault(doc.tag, doc.text)
    return by_cite, list(docs.values()), unresolved


def _report_page(text: str, locator: str) -> str:
    """Page N of the owner's report text (page markers ===== [page N] =====), or the whole text."""
    match = re.fullmatch(r"p(\d+)", locator or "")
    if not match:
        return text
    pages = re.split(r"^===== \[page (\d+)\] =====$", text, flags=re.M)
    for i in range(1, len(pages) - 1, 2):
        if pages[i] == match.group(1):
            return pages[i + 1]
    return text


@assembler("fact_table")
def _fact_table(ctx: RunContext, name: str) -> BuiltInput:
    """16A's facts, each with the source excerpt the pipeline cut from the cited document by the value
    (source_excerpt), or excerpt_missing when none was found (prompt 04A)."""
    run = ctx.latest_run("16A", period=ctx.period, round_=ctx.round)
    text = run.read_output("fact_table") if run else None
    if run is None or text is None:
        raise MissingInput(f"{name}: no succeeded 16A run (round {ctx.round}) for {ctx.company} {ctx.period}")
    data = yaml.safe_load(text)
    facts = _fact_rows(data)
    tags = [t for f in facts if isinstance(f, dict) for t in _tags_of(f.get("source"))]
    texts, _, unresolved = cited_texts(ctx, tags)
    found = missing = 0
    for fact in facts:
        if not isinstance(fact, dict) or fact.get("value") is None:
            continue
        excerpt = None
        for cite in _tags_of(fact.get("source")):
            tag, _, locator = cite.partition("#")
            document = texts.get(cite) or texts.get(tag)
            if document is None:
                continue
            if "-RPT" in tag:
                document = _report_page(document, locator)
            excerpt = documents.cut_excerpt(document, fact.get("value"))
            if excerpt:
                break
        if excerpt:
            fact["source_excerpt"] = excerpt
            found += 1
        else:
            fact["excerpt_missing"] = True
            missing += 1
    out = data if isinstance(data, dict) else {"facts": facts}
    note = f"source excerpts cut: {found}; excerpt_missing: {missing}"
    if unresolved:
        note += f"; {len(unresolved)} cited tag(s) not resolvable to a document"
    source = {**run.outputs_used(["fact_table"])[0], "transform": "source_excerpt cut by the pipeline"}
    return BuiltInput(dump_yaml(out), "yml", [source], note=note)


def _findings_tags(text: str | None) -> list[str]:
    return re.findall(r"\[src:([A-Z0-9][A-Za-z0-9._-]*(?:#[^\]\s]+)?)\]", text or "")


@assembler("sources")
def _sources(ctx: RunContext, name: str) -> BuiltInput:
    """The source table (both sources.yml, and for 03R the draft's sources_additions) and the full text of the
    primary documents: the event's filings and the documents the facts (04A) or the findings (03R) cite. Written as
    .txt: thesis-ci validates any file named sources.yml."""
    company = _need_company(ctx, name)
    table, sources = source_table(ctx)
    lines = [f"Sources for {company} {ctx.period}.", "", "===== source table (public and private sources.yml) =====",
             table.rstrip(), ""]
    tags: list[str] = []
    if ctx.step.step == "04A":
        run = ctx.latest_run("16A", period=ctx.period, round_=ctx.round)
        text = run.read_output("fact_table") if run else None
        tags = [t for f in _fact_rows(yaml.safe_load(text) if text else None) if isinstance(f, dict)
                for t in _tags_of(f.get("source"))]
    elif ctx.step.step == "03R":
        draft = revised_outputs(ctx)
        audit = ctx.latest_run("04A", period=ctx.period)
        additions = draft.read_output("sources_additions") if draft else None
        if additions and not _outputs.is_empty_mark(additions):
            lines += ["===== the draft's sources_additions =====", additions.rstrip(), ""]
            sources += draft.outputs_used(["sources_additions"])
        tags = _findings_tags(audit.read_output("findings") if audit else None)
    if not ctx.step.post_event:
        return BuiltInput("\n".join(lines) + "\n", "txt", sources)
    texts, docs, unresolved = cited_texts(ctx, tags)
    report_tags = sorted({t.partition("#")[0] for t in tags if "-RPT" in t and t.partition("#")[0] in texts})
    text, doc_sources = render_sources(ctx, lines, sorted(docs, key=_filing_order), report_tags, texts, unresolved)
    return BuiltInput(text, "txt", sources + doc_sources,
                      note=f"{len(docs)} EDGAR document(s)" + (f"; {len(unresolved)} tag(s) unresolved" if unresolved
                                                               else ""),
                      substitute=name if ctx.results().rehearsal_for else None)


def _filing_order(doc: FilingText) -> tuple[Any, ...]:
    return doc.filed, doc.accession, doc.locator or ""


def render_sources(ctx: RunContext, lines: list[str], docs: Iterable[FilingText], report_tags: Iterable[str],
                   texts: Mapping[str, str], unresolved: Iterable[str]) -> tuple[str, list[dict[str, Any]]]:
    """The body of a `sources` input after its header lines: the citation note, the tags not supplied, each EDGAR
    document, then the owner's report where it is cited. Returns the text and the documents' source records."""
    company = _need_company(ctx, "sources")
    lines = [*lines, CITE_NOTE]
    unresolved = list(unresolved)
    if unresolved:
        lines.append(f"Not supplied (not resolvable to a document of {company} on EDGAR): {', '.join(unresolved)}.")
    text, doc_sources = render_documents(lines + [""], docs)
    for tag in report_tags:
        path = ctx.workspace_root / "inputs" / "text" / f"reports__{company}.txt"
        text += f"\n===== [src:{tag}] the owner's report (non-primary; a pointer to its own sources, 00 E2) =====\n\n"
        text += texts[tag].strip() + "\n"
        doc_sources.append(workspace_file_source(ctx.workspace_root, path))
    return text, doc_sources


# ---- slices: one part as several calls when one cannot take its inputs (pipeline/slicing.py, decisions/0026)

RARE_CITES = 2  # 04A: a document at most this many facts cite, and not one of the event's, is cut to windows


@dataclasses.dataclass
class SliceInputs:
    """The inputs of one slice (every input the part declares) and a one-line description for the manifest."""

    inputs: dict[str, BuiltInput]
    note: str


def slice_inputs(ctx: RunContext, built: Mapping[str, BuiltInput]) -> list[SliceInputs] | None:
    """The part's inputs cut into slices when one call cannot take them; None when one call can (or the part is not
    sliced)."""
    slicer = SLICERS.get(ctx.step.step)
    return slicer(ctx, built) if slicer else None


def _slice_16a(ctx: RunContext, built: Mapping[str, BuiltInput]) -> list[SliceInputs] | None:
    product = built.get("product")
    pieces = slicing.split_product(product.text) if product is not None else None
    if not pieces:
        return None
    out = []
    for number, text in enumerate(pieces, 1):
        blocks = re.findall(r"^===== (?!source table)(.*) =====$", text, flags=re.M)
        note = f"slice {number} of {len(pieces)}: {'; '.join(blocks)}"
        out.append(SliceInputs({**built, "product": dataclasses.replace(product, text=text, note=note)}, note))
    return out


def _fact_documents(row: Any, docs: Iterable[FilingText], reports: Iterable[str],
                    event: Iterable[str]) -> frozenset[str]:
    """The documents one fact needs: those its source tags name (an exhibit locator picks that exhibit), the owner's
    report where it is cited; for an untagged fact, the event's own filings."""
    cites = _tags_of(row.get("source")) if isinstance(row, Mapping) else []
    if not cites:
        return frozenset(event)
    docs, reports = list(docs), set(reports)
    keys: set[str] = set()
    for cite in cites:
        tag, _, locator = cite.partition("#")
        if tag in reports:
            keys.add(tag)
            continue
        same = [d for d in docs if d.tag == tag]
        if locator.startswith("EX-"):
            same = [d for d in same if d.locator == locator] or same
        keys.update(d.cite for d in same)
    return frozenset(keys)


def _windowed(doc: FilingText, rows: Iterable[Any], why: str) -> FilingText:
    """A document cut to the lines around the values the given facts state."""
    rows = [r for r in rows if isinstance(r, Mapping)]
    patterns = [p for r in rows for p in documents.distinctive_patterns(r.get("value"))]
    text, hits = slicing.windows(doc.text, patterns)
    note = (f"cut by the pipeline to the lines around the values {len(rows)} fact(s) state ({hits} matching "
            f"line(s)), because {why}; a value missing here may still be in the full document at the url above")
    return dataclasses.replace(doc, text=text if hits else "(no line of this document states one of those values)",
                               note=f"{doc.note}; {note}" if doc.note else note)


def _slice_04a(ctx: RunContext, built: Mapping[str, BuiltInput]) -> list[SliceInputs] | None:
    table, whole = built.get("fact_table"), built.get("sources")
    if table is None or whole is None:
        return None
    data = yaml.safe_load(table.text)
    rows = _fact_rows(data)
    if len(rows) <= slicing.MAX_FACTS and slicing.estimate_tokens(whole.text) <= slicing.HARD_SOURCE_TOKENS:
        return None
    tags = [t for r in rows if isinstance(r, dict) for t in _tags_of(r.get("source"))]
    texts, docs, unresolved = cited_texts(ctx, tags)
    by_key = {d.cite: d for d in docs}
    event = [d.cite for d in ctx.results().documents]
    reports = sorted({t.partition("#")[0] for t in tags if "-RPT" in t and t.partition("#")[0] in texts})
    fact_docs = [_fact_documents(r, docs, reports, event) for r in rows]
    cited: dict[str, list[int]] = {}
    for index, keys in enumerate(fact_docs):
        for key in keys:
            cited.setdefault(key, []).append(index)
    rendered: dict[str, FilingText] = {}
    for key, indexes in cited.items():
        if key in by_key:
            doc = by_key[key]
            if len(indexes) <= RARE_CITES and key not in event:
                doc = _windowed(doc, [rows[i] for i in indexes], f"only {len(indexes)} fact(s) cite it")
            rendered[key] = doc
    doc_tokens = {key: slicing.estimate_tokens(render_documents([], [doc])[0]) for key, doc in rendered.items()}
    doc_tokens.update({key: slicing.estimate_tokens(texts[key]) for key in reports})
    older = sorted((k for k in rendered if k not in event), key=lambda k: _filing_order(rendered[k]), reverse=True)
    priority = [*event, *reports, *older]
    packed = slicing.pack_facts(fact_docs, doc_tokens, priority=priority)
    source_lines_head, table_sources = source_table(ctx)
    header = [f"Sources for {ctx.company} {ctx.period}.", "", "===== source table (public and private sources.yml) =====",
              source_lines_head.rstrip(), ""]
    out = []
    for number, part in enumerate(packed, 1):
        count = len(packed)
        chosen = [rows[i] for i in part.facts]
        sliced = slicing.slice_fact_table(data, rows, part.facts, number, count)
        mine = {t for r in chosen if isinstance(r, dict) for t in _tags_of(r.get("source"))}
        slice_docs = [rendered[k] for k in part.documents if k in rendered]
        slice_docs += [_windowed(rendered[k], [rows[i] for i in part.facts if k in fact_docs[i]],
                                 "this slice's documents are over the pipeline's limit")
                       for k in part.cut if k in rendered]
        slice_reports = [k for k in reports if k in part.all_documents()]
        text, doc_sources = render_sources(ctx, header, sorted(slice_docs, key=_filing_order), slice_reports, texts,
                                           [u for u in unresolved if u in mine])
        ids = [str(r.get("id")) for r in chosen if isinstance(r, dict)]
        note = (f"slice {number} of {count}: {len(chosen)} facts ({ids[0]}–{ids[-1]}); documents: "
                f"{', '.join(part.documents) or 'none'}" + (f"; cut to windows: {', '.join(part.cut)}" if part.cut
                                                             else ""))
        out.append(SliceInputs({
            **built,
            "fact_table": BuiltInput(dump_yaml(sliced), table.ext, table.sources, note=note),
            "sources": BuiltInput(text, whole.ext, table_sources + doc_sources, note=note,
                                  substitute=whole.substitute),
        }, note))
    return out


SLICERS: dict[str, Callable[[RunContext, Mapping[str, BuiltInput]], list[SliceInputs] | None]] = {
    "16A": _slice_16a,
    "04A": _slice_04a,
}


def revised_outputs(ctx: RunContext) -> PriorRun | None:
    """What a revision revises: the draft in the first round, the previous revision after HQ returned it."""
    if ctx.round > 1:
        return ctx.latest_run("03R", period=ctx.period, round_=ctx.round - 1)
    return ctx.latest_run("03-draft", period=ctx.period)


@assembler("draft_outputs")
def _draft_outputs(ctx: RunContext, name: str) -> BuiltInput:
    """03R: every output of the 03 draft, as written before the audit; in the second round (after HQ returned the
    update), every output of the first revision."""
    run = revised_outputs(ctx)
    if run is None:
        raise MissingInput(f"{name}: no succeeded {'03R' if ctx.round > 1 else '03-draft'} run for {ctx.company} "
                           f"{ctx.period}")
    what = "the revision" if ctx.round > 1 else "the quarterly update draft"
    text, sources = run_outputs_text(run, draft_outputs_names(run),
                                     f"The outputs of {what} {run.rel}, one block per output.")
    return BuiltInput(text, "txt", sources)


@assembler("findings_04A")
def _findings_04a(ctx: RunContext, name: str) -> BuiltInput:
    run, text = _run_output(ctx, name, "04A", "findings", why="04A audits the draft before the revision")
    return BuiltInput(text, "yml", run.outputs_used(["findings"]))


@assembler("test_proposals_04B_lite")
def _test_proposals_04b_lite(ctx: RunContext, name: str) -> BuiltInput:
    if not ctx.is_holding():
        raise Omit("04B-lite runs only for holdings' quarterly updates (prompt 04)")
    run, text = _run_output(ctx, name, "04B-lite", "test_proposals", why="the red team runs on the draft")
    return BuiltInput(text, "yml", run.outputs_used(["test_proposals"]))


@assembler("returns_17A")
def _returns_17a(ctx: RunContext, name: str) -> BuiltInput:
    run = ctx.latest_run("17A", period=ctx.period)
    text = run.read_output("returns") if run else None
    if run is None or text is None or not text.strip() or _outputs.is_empty_mark(text):
        raise Omit("HQ has not returned this update (no 17A returns for this period)")
    return BuiltInput(text, "yml", run.outputs_used(["returns"]))


# ---- holdings' steps and HQ's gate (04B-lite, 14B, 15B, 17A)


def _event_output(ctx: RunContext, name: str, step: str, output: str, candidate_reason: str | None = None) -> BuiltInput:
    """An output of an earlier step of this event; for a candidate, when the step runs only for holdings, an explicit
    document that says so."""
    company = _need_company(ctx, name)
    if candidate_reason and not ctx.is_holding():
        return empty_document(ctx, name, f"{company} is a candidate: {candidate_reason}")
    run, text = _run_output(ctx, name, step, output, why=f"{step} runs earlier in the event")
    return BuiltInput(text, "yml" if output != "update" else "md", run.outputs_used([output]))


@assembler("update")
def _update(ctx: RunContext, name: str) -> BuiltInput:
    """04B-lite and 14B: the 03 draft's update record."""
    run, text = _run_output(ctx, name, "03-draft", "update", why="the draft comes first")
    return BuiltInput(text, "md", run.outputs_used(["update"]))


@assembler("question_answers")
def _question_answers(ctx: RunContext, name: str) -> BuiltInput:
    """14B: the company manager's answers to the frozen question list, from the 03 draft."""
    return _event_output(ctx, name, "03-draft", "question_answers")


@assembler("blind_answers", "unprompted_observations")
def _blind_read(ctx: RunContext, name: str) -> BuiltInput:
    """14B: the blind read's answers and observations (14A)."""
    return _event_output(ctx, name, "14A", name)


@assembler("prior_inversion_list")
def _prior_inversion_list(ctx: RunContext, name: str) -> BuiltInput:
    """04B-lite: the previous period's inversion list, so this quarter's does not repeat it."""
    company = _need_company(ctx, name)
    now = documents.period_index(ctx.period)
    earlier = [r for r in ctx.runs() if r.succeeded and r.step == "04B-lite" and r.company == company
               and -1 < _period_key(r.period) < now and r.output_file("inversion_list") is not None]
    if not earlier:
        raise Omit(f"no earlier inversion list for {company}: this is the first 04B-lite")
    run = max(earlier, key=lambda r: (_period_key(r.period), r.round, r.run_date))
    return BuiltInput(run.read_output("inversion_list") or "", "yml", run.outputs_used(["inversion_list"]),
                      note=f"inversion list of {run.period} ({run.rel})")


# 15B: what the settler may see of a due pre-registration item (prompt 15B: no probability, no author).
BLIND_ITEM_FIELDS = ("id", "statement", "criterion", "data_source", "horizon")
# ...and of a due ledger entry: the side is replaced by how it is settled, the probability is removed.
LEDGER_HIDDEN = ("side", "probability", "acknowledged_next_letter", "acknowledged_source", "settlement_source", "status")
SETTLE_AS = {"management": "four_tier", "system": "binary", "owner": "binary"}
ITEM_ID_RE = re.compile(r"^(?P<company>[A-Z][A-Z0-9.]*)-(?P<period>FY\d{4}Q[1-4])-\d+$")


@assembler("items_blind")
def _items_blind(ctx: RunContext, name: str) -> BuiltInput:
    """15B: the pre-registration items due by the end of the month in which the event closes, with only id,
    statement, criterion, data source and horizon (the settler cannot see probabilities or authors)."""
    company = _need_company(ctx, name)
    due = prereg_items_due(ctx) if ctx.is_holding() else []
    rows = sorted({str(i["id"]): {k: i[k] for k in BLIND_ITEM_FIELDS if k in i} for i in due}.values(),
                  key=lambda row: str(row["id"]))
    sources = _prereg_sources(ctx) + [{"kind": "generated", "detail": "probability, author and book removed"}]
    if not rows:
        why = "candidates do not pre-register" if not ctx.is_holding() else \
            f"no pre-registration item is due by {_event_month_end(ctx)}"
        built = empty_document(ctx, name, f"{company}: {why}")
        built.sources = sources
        return built
    data = {"company": company, "due_by": _event_month_end(ctx).isoformat(), "items": rows}
    return BuiltInput(dump_yaml(data), "yml", sources, note=f"{len(rows)} item(s) due")


@assembler("ledger_due")
def _ledger_due(ctx: RunContext, name: str) -> BuiltInput:
    """15B: the ledger entries due this period, the side replaced by how each is settled (settle_as: four_tier for
    management's commitments, binary for the system's and the owner's forecasts) and the probability removed."""
    company = _need_company(ctx, name)
    path = ctx.public_root / "companies" / company / "ledger.yml"
    sources = [repo_file_source(ctx.public_root, path, PUBLIC_REPO) | {"transform": "side, probability and status "
                                                                                    "removed; settle_as added"}] \
        if path.is_file() else []
    rows = [{"settle_as": SETTLE_AS.get(str(e.get("side")), "four_tier"),
             **{k: v for k, v in e.items() if k not in LEDGER_HIDDEN}} for e in ledger_entries_due(ctx)]
    if not rows:
        built = empty_document(ctx, name, f"no pending ledger entry of {company} is due by the end of {ctx.period}")
        built.sources = sources
        return built
    return BuiltInput(dump_yaml({"company": company, "period": ctx.period, "entries": rows}), "yml", sources,
                      note=f"{len(rows)} entr(y/ies) due")


@assembler("metric_values")
def _metric_values(ctx: RunContext, name: str) -> BuiltInput:
    """15B: the values the pipeline fetched for this event: the readings the evaluation used (XBRL companyfacts and
    16B, each with its source) and 16B's extraction of this period as written."""
    company = _need_company(ctx, name)
    ci = ctx.require_run(CI_STEP, name, why="the evaluation runs before the settlement")
    readings_file = ci.path / "inputs" / "readings.yml"
    readings = load_yaml_file(readings_file) or {}
    sources = [{"kind": "run_output", "run": ci.rel, "output": "inputs/readings.yml",
                "sha256": sha256_bytes(readings_file.read_bytes()) if readings_file.is_file() else None}]
    data: dict[str, Any] = {"company": company, "period": ctx.period, "readings": readings.get("readings") or [],
                            "not_usable": readings.get("not_usable") or []}
    extraction = ctx.latest_run("16B", period=ctx.period)
    if extraction is not None and extraction.read_output("metric_values"):
        data["extracted_16B"] = yaml.safe_load(extraction.read_output("metric_values") or "")
        sources += extraction.outputs_used(["metric_values"])
    return BuiltInput(dump_yaml(data), "yml", sources, note=f"{len(data['readings'])} reading(s)")


def settlement_filings(ctx: RunContext) -> tuple[list[FilingText], list[dict[str, Any]]]:
    """15B: the documents each due item's data_source names, over the quarters from its registration to this event
    (at most eight), besides the event's own filings."""
    company = _need_company(ctx, "filings")
    items = prereg_items_due(ctx) if ctx.is_holding() else []
    if not items:
        return [], []
    thesis = ctx.thesis()
    filer, gateway = ctx.filer(), ctx.gateway()
    known = known_filing_tags((ctx.public_root, ctx.private_root), company)
    owners = documents.owner_names(company, thesis.get("name"), short_name(thesis.get("name"), company))
    period, as_of = _event_period(ctx), _as_of(ctx)
    merged: dict[str, documents.Selection] = {}
    rows, context = [], None
    for item in items:
        match = ITEM_ID_RE.match(str(item.get("id")))
        registered = match.group("period") if match else period
        lookback = max(1, min(8, documents.period_index(period) - documents.period_index(registered) + 1))
        plan = documents.parse_where(item.get("data_source"), foreign=filer.type == edgar.FOREIGN, owners=owners)
        selections, notes, subs, cal, _, ticker = gateway.where_filings(filer, period, as_of, lookback, plan)
        context = (subs, cal, ticker)
        for selection in selections:
            old = merged.get(selection.filing.accession)
            merged[selection.filing.accession] = old.merged(selection) if old else selection
        rows.append({"item": item.get("id"), "data_source": plan.where, "lookback": lookback,
                     "accessions": [s.filing.accession for s in selections],
                     "not_supplied": [*plan.not_supplied, *notes]})
    docs: list[FilingText] = []
    for selection in merged.values():
        subs, cal, ticker = context  # type: ignore[misc]
        docs += gateway.selection_documents(selection, ticker=ticker, cal=cal, subs=subs, known_tags=known)
    return docs, rows


@assembler("update_outputs")
def _update_outputs(ctx: RunContext, name: str) -> BuiltInput:
    """17A: every output of the latest revision (03R) of this event."""
    run = ctx.latest_run("03R", period=ctx.period)
    if run is None:
        raise MissingInput(f"{name}: no succeeded 03R run for {ctx.company} {ctx.period} (HQ reviews the revision)")
    text, sources = run_outputs_text(run, draft_outputs_names(run),
                                     f"The outputs of the quarterly update's revision {run.rel}, one block per output.")
    return BuiltInput(text, "txt", sources)


@assembler("inversion_list")
def _inversion_list(ctx: RunContext, name: str) -> BuiltInput:
    return _event_output(ctx, name, "04B-lite", "inversion_list",
                         "the inversion list (04B-lite) runs only for holdings' quarterly updates (prompt 04)")


@assembler("trust_level")
def _trust_level(ctx: RunContext, name: str) -> BuiltInput:
    """17A: the company manager's trust level, from both places the pipeline keeps it, and its route (section G9)."""
    company = _need_company(ctx, name)
    thesis_level = ctx.thesis().get("trust_level")
    levels_path = ctx.public_root / "trust" / "levels.yml"
    levels = load_yaml_file(levels_path) or {}
    listed = ((levels.get("companies") or {}) if isinstance(levels, dict) else {}).get(company)
    rights = load_yaml_file(ctx.public_root / "constitution" / "decision-rights.yml") or {}
    routing = ((rights.get("trust") or {}).get("routing") or {}) if isinstance(rights, dict) else {}
    found = [v for v in (thesis_level, listed) if isinstance(v, int) and not isinstance(v, bool)]
    level = min(found) if found else None
    data = {"company": company, "role": "company_manager", "trust_level": level,
            "thesis_yml": thesis_level, "trust_levels_yml": listed,
            "route": routing.get(str(level)) if level is not None else None, "rule": "00 section G9"}
    sources = [repo_file_source(ctx.public_root, ctx.thesis_path(), PUBLIC_REPO) | {"field": "trust_level"}]
    if levels_path.is_file():
        sources.append(repo_file_source(ctx.public_root, levels_path, PUBLIC_REPO))
    return BuiltInput(dump_yaml(data), "yml", sources)


@assembler("decision_rights")
def _decision_rights(ctx: RunContext, name: str) -> BuiltInput:
    path = ctx.public_root / "constitution" / "decision-rights.yml"
    if not path.is_file():
        raise MissingInput(f"constitution/decision-rights.yml does not exist in {PUBLIC_REPO}")
    return BuiltInput(path.read_text(encoding="utf-8"), "yml", [repo_file_source(ctx.public_root, path, PUBLIC_REPO)])


def _rows(text: str | None) -> list[dict[str, Any]]:
    data = yaml.safe_load(text) if text else None
    rows = data if isinstance(data, list) else _letter_entries(data) or []
    return [r for r in rows if isinstance(r, dict)]


@assembler("gate_rules")
def _gate_rules(ctx: RunContext, name: str) -> BuiltInput:
    """17A: the release gate (decision-rights.yml gate.blocking, trust.routing) and what the pipeline can already
    tell about each condition for this update: open must-fix findings, failed breaker tests, divergences touching a
    pillar, the trust route. Lint runs when the update is placed."""
    company = _need_company(ctx, name)
    path = ctx.public_root / "constitution" / "decision-rights.yml"
    rights = load_yaml_file(path) or {}
    audits = ctx.event_runs("04A")
    revision = ctx.latest_run("03R", period=ctx.period)
    notes = {str(r.get("finding_id")): r.get("action") for r in _rows(revision.read_output("revision_notes"))} \
        if revision else {}
    must_fix = [{"finding": r.get("id"), "round": run.round, "revision_notes": notes.get(str(r.get("id")), "none")}
                for run in audits for r in _rows(run.read_output("findings")) if r.get("group") == "must fix"]
    breakers = {str(t.get("id")) for t in ctx.thesis().get("tests") or [] if isinstance(t, dict)
                and t.get("severity") == "breaker"}
    ci = ctx.latest_run(CI_STEP, period=ctx.period)
    failed = [r.get("id") for r in evaluation.result_rows(yaml.safe_load(ci.read_output("ci_results") or "")
                                                          if ci else None)
              if r.get("result") == "fail" and str(r.get("id")) in breakers]
    judge = ctx.latest_run("14T", period=ctx.period)
    failed += [r.get("test_id") for r in _rows(judge.read_output("qualitative_verdicts") if judge else None)
               if r.get("verdict") == "fail" and str(r.get("test_id")) in breakers]
    divergence = ctx.latest_run("14B", period=ctx.period)
    pillar = [{"question": r.get("id"), "resolution": r.get("resolution"), "to_owner_letter": r.get("to_owner_letter")}
              for r in _rows(divergence.read_output("divergence_map") if divergence else None)
              if r.get("mark") == "diverge" and r.get("touches_pillar")]
    level = ctx.thesis().get("trust_level")
    data = {
        "company": company, "period": ctx.period,
        "gate": (rights.get("gate") if isinstance(rights, dict) else None) or {},
        "routing": ((rights.get("trust") or {}).get("routing") if isinstance(rights, dict) else None) or {},
        "pipeline_checks": {
            "lint_errors": "checked when the update is placed (thesis-ci lint on both repositories)",
            "open_04A_must_fix": must_fix or "none",
            "failed_breakers": sorted(str(x) for x in failed) or "none",
            "divergences_touching_a_pillar": pillar or ("none" if ctx.is_holding() else
                                                        "no divergence map: the company is a candidate"),
            "trust_level": level,
        },
    }
    sources = [repo_file_source(ctx.public_root, path, PUBLIC_REPO)] if path.is_file() else []
    for run in [*audits, revision, ci, judge, divergence]:
        if run is not None:
            sources.append({"kind": "run_output", "run": run.rel, "output": "outputs"})
    return BuiltInput(dump_yaml(data), "yml", sources)


@assembler("questions")
def _questions(ctx: RunContext, name: str) -> BuiltInput:
    """17A: every question raised in this event, by run."""
    company = _need_company(ctx, name)
    found, used = [], []
    for run in ctx.runs():
        if not (run.succeeded and run.company == company and run.period == ctx.period):
            continue
        text = run.read_output("questions")
        if text is None or not text.strip() or _outputs.is_empty_mark(text):
            continue
        found.append({"run": run.rel, "step": run.step, "questions": yaml.safe_load(text)})
        used += run.outputs_used(["questions"])
    if not found:
        return empty_document(ctx, name, f"no step of {company}'s {ctx.period} event raised a question")
    return BuiltInput(dump_yaml({"company": company, "period": ctx.period, "raised": found}), "yml", used)


@assembler("valuation_input_notes")
def _valuation_input_notes(ctx: RunContext, name: str) -> BuiltInput:
    run = ctx.latest_run("03R", period=ctx.period)
    text = run.read_output("valuation_input_notes") if run else None
    if run is None or text is None or not text.strip() or _outputs.is_empty_mark(text):
        raise Omit("the revision wrote no valuation_input_notes")
    return BuiltInput(text, "yml", run.outputs_used(["valuation_input_notes"]))
