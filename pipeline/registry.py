"""Input registry for the pipeline runner (docs/decisions/0019). This module never calls a model.

A step is one prompt part run by one role: 14Q (question list, HQ), 15A (pre-registration, company manager),
18 (monthly letter, HQ). The prompt's front matter is the only list of a part's inputs; this module maps every
input name to an assembler that builds that document from the two repositories, EDGAR (through pipeline.edgar),
the thesis-ci schemas and earlier run outputs under runs/ in the private repository.

- A declared input without an assembler, or a required input whose data cannot be found, fails the assembly;
  the runner reports every missing input at once.
- A required input that legitimately has no data yet (no settled predictions, no update this month) becomes an
  explicit document that says so in one line, and the manifest marks it empty. Nothing is silently omitted.
  An optional input may be left out only by raising Omit, and the manifest records the reason.
- Stand-ins are declared: until companies/<T>/dossier.md exists, the owner's complete report text stands in for
  the dossier (substitute: dossier in the manifest).
- Assemblers read files, git history and EDGAR. They never call a model and never print what they read.

Adding a step (16B, 14A, 14T, 03, 04A, 04B-lite, 14B, 15B) means adding a StepSpec to STEPS and an assembler for
each input name that part declares and that is not registered yet; the runner, the bundle format and the
workflow stay as they are. docs/decisions/0019 lists what each of those steps still needs.
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

from . import edgar, isolation, llm
from . import outputs as _outputs

PUBLIC_REPO = "owners-office"
PRIVATE_REPO = "owners-office-private"
WORKSPACE = "workspace"  # the directory that holds both checkouts, the local .env and inputs/

MANIFEST_NAME = "manifest.yml"
RUN_RECORD_NAME = "run.yml"
OUTPUTS_DIR_NAME = "outputs"
ATTEMPTS_DIR_NAME = "attempts"  # failed attempts kept by `execute --retry`: attempts/<n>/run.yml
LLM_LOG_REL = "runs/llm-log.jsonl"  # persistent call log in the private repository (STATUS T7)

# Prompt variables (prompts README). Their names are Chinese in the prompts; they are written as escapes here so
# that this public module stays English-only.
VAR_COMPANY = "\u516c\u53f8"  # company name
VAR_TICKER = "\u4ee3\u7801"  # ticker
VAR_STATUS = "\u72b6\u6001"  # holding | candidate
VAR_PERIOD = "\u671f\u95f4"  # FY<year>Q<quarter>
VAR_DATE = "\u65e5\u671f"  # run date
# The label that marks a pre-registration candidate in thesis.yml's todo list (prompt 15A), plus its English form.
PREREG_CANDIDATE_MARKS = ("\u9884\u6ce8\u518c\u5019\u9009", "pre-registration candidate", "prereg candidate")
_LABEL_SEPARATORS = ("\uff1a", ":")  # full-width and ASCII colon

QUARTER_RE = re.compile(r"^FY\d{4}Q[1-4]$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
RUN_DIR_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(\d{2}[A-Za-z0-9-]*)$")
_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
_MISTAKE_HEADING_RE = re.compile(r"^### (\d{4}-\d{2}-\d{2})\b", re.M)
_IX_HEADER_RE = re.compile(rb"(?is)<ix:header\b.*?</ix:header\s*>")
RESOLVED = ("happened", "not_happened")


class MissingInput(LookupError):
    """A required input has no data and no defined stand-in. The message names what is missing, never content."""


class Omit(Exception):
    """An optional input is left out on purpose; the message is the reason recorded in the manifest."""


# ---------------------------------------------------------------------------------------------------- data


@dataclasses.dataclass(frozen=True)
class StepSpec:
    """One supported step. The inputs come from the prompt's front matter, not from here."""

    step: str  # the part label as the prompt names it: 14Q, 15A, 18
    prompt_id: str
    part: str | None  # front-matter part key (Q, A), or None for a prompt without parts
    scope: str  # "company" (runs/<TICKER>/...) or "hq" (runs/hq/...)
    period_kind: str  # "quarter" (FY<year>Q<quarter>) or "month" (YYYY-MM, the month the step is about)
    holdings_only: bool  # prompt scope: 14Q and 15A run for holdings only
    summary: str
    pipeline_fields: Callable[[RunContext], dict[str, dict[str, Any]]] | None = None


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

    @property
    def cite(self) -> str:
        return f"{self.tag}#{self.locator}" if self.locator else self.tag


@dataclasses.dataclass(frozen=True)
class ResultsDocuments:
    period: str
    period_end: dt.date
    release_date: dt.date
    documents: list[FilingText]
    notes: list[str]


@dataclasses.dataclass
class PriorRun:
    """A run directory under runs/<scope>/<run_date>-<part>/ in the private repository."""

    path: Path
    rel: str
    scope: str
    run_date: dt.date
    part: str
    manifest: dict[str, Any] | None
    record: dict[str, Any] | None
    attempts: list[dict[str, Any]] = dataclasses.field(default_factory=list)  # earlier failed attempts (--retry)

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
    call: Any  # llm.PromptPart
    formats: Mapping[str, str]
    edgar: Any = None  # EdgarGateway or a test double; created on first use
    schemas_dir: Path | None = None
    memo: dict[str, Any] = dataclasses.field(default_factory=dict)

    @property
    def scope(self) -> str:
        return self.company or "hq"

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
        return self.remember("runs", lambda: index_runs(self.private_root))

    def month(self) -> tuple[dt.date, dt.date]:
        return month_bounds(self.period)


# ---------------------------------------------------------------------------------------------------- steps


def _prereg_fields(ctx: RunContext) -> dict[str, dict[str, Any]]:
    """15A: the pre-registration header is the pipeline's, not the model's (edgar.prereg_header())."""
    return {"prereg": {"company": ctx.company, **ctx.estimate().prereg_header(), "author": "system"}}


STEPS: dict[str, StepSpec] = {
    "14Q": StepSpec("14Q", "14", "Q", "company", "quarter", True,
                    "neutral question list, frozen before the results event (HQ)"),
    "15A": StepSpec("15A", "15", "A", "company", "quarter", True,
                    "pre-registration of 3-5 settleable expectations (company manager)", _prereg_fields),
    "18": StepSpec("18", "18", None, "hq", "month", False,
                   "monthly letter to the owner about the previous month (HQ)"),
}


def step_spec(step: str) -> StepSpec:
    if step not in STEPS:
        raise KeyError(f"unknown step {step!r}; supported: {', '.join(STEPS)}")
    return STEPS[step]


def variables_for(ctx: RunContext) -> dict[str, str]:
    """{{variable}} values of the prompts (prompts README). Unused ones are ignored by llm.fill_variables()."""
    out = {VAR_DATE: ctx.run_date.isoformat()}
    if ctx.company:
        thesis = ctx.thesis()
        out[VAR_TICKER] = ctx.company
        out[VAR_COMPANY] = str(thesis.get("name") or ctx.company)
        out[VAR_STATUS] = str(thesis.get("status") or "")
    if ctx.step.period_kind == "quarter":
        out[VAR_PERIOD] = ctx.period
    return out


def placement_context(ctx: RunContext) -> dict[str, Any]:
    """Template fields for outputs.place(): company, period, run_date, month (monthly letter)."""
    return {
        "company": ctx.company,
        "period": ctx.period if ctx.step.period_kind == "quarter" else None,
        "run_date": ctx.run_date.isoformat(),
        "month": ctx.period if ctx.step.period_kind == "month" else None,
        "doc": None,
        "subject": None,
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

    def latest_results(self, filer: edgar.Filer, period: str, as_of: dt.date,
                       known_tags: Mapping[str, str] | None = None) -> ResultsDocuments:
        """The results release and periodic report of the last quarter reported before `period`, as of `as_of`."""
        known_tags = known_tags or {}
        fiscal_year, quarter = edgar.parse_period(period)
        if quarter is None:
            raise ValueError(f"latest filings are looked up for a quarter (FY2026Q3), got {period!r}")
        client = self.client
        subs = edgar.submissions(filer.cik, client=client, since=dt.date(fiscal_year - 2, 1, 1))
        cal = edgar.FiscalCalendar.parse(filer.fiscal_year_end or subs.fiscal_year_end)
        period_end = cal.quarter_end(fiscal_year, quarter)
        kind = filer.type or edgar.infer_filer_type(subs)
        events = edgar.earnings_events(subs.cik, cal.label, filer_type=kind, since=period_end - dt.timedelta(days=400),
                                       until=as_of, client=client, subs=subs)
        earlier = [e for e in events if e.period_end < period_end]
        if not earlier:
            raise MissingInput(f"EDGAR has no results release for a quarter before {period} as of {as_of}")
        latest = max(earlier, key=lambda e: (e.period_end, e.filing.filing_date, e.accession))
        ticker = filer.ticker or str(subs.tickers[0] if subs.tickers else filer.cik)
        documents: list[FilingText] = []
        notes: list[str] = []
        if kind == edgar.FOREIGN:
            if latest.exhibit:
                documents.append(self._document(ticker, cal, latest.filing, latest.exhibit, known_tags))
            else:
                notes.append(f"the results 6-K {latest.accession} names no press-release document")
        else:
            docs = edgar.filing_documents(subs.cik, latest.accession, client=client,
                                          primary_document=latest.filing.primary_document)
            exhibits = [d for d in docs if d.kind == "exhibit" and (d.exhibit or "").startswith("EX-99")
                        and not d.name.lower().endswith(".pdf")]
            for doc in sorted(exhibits, key=_exhibit_key):
                documents.append(self._document(ticker, cal, latest.filing, doc.name, known_tags, doc.exhibit))
            if not exhibits:
                notes.append(f"the results 8-K {latest.accession} has no EX-99 exhibit")
        closing = latest.closing
        if closing is not None and closing.filing_date <= as_of:
            documents.append(self._document(ticker, cal, closing, closing.primary_document, known_tags))
        elif kind != edgar.FOREIGN:
            notes.append(f"the periodic report for {latest.period} was not on EDGAR as of {as_of}")
        return ResultsDocuments(latest.period, latest.period_end, latest.release_date, documents, notes)

    def _document(self, ticker: str, cal: edgar.FiscalCalendar, filing: edgar.Filing, name: str,
                  known_tags: Mapping[str, str], exhibit: str | None = None) -> FilingText:
        url = f"{filing.folder_url}/{name}"
        raw = self.client.get_bytes(url)
        if exhibit is None:
            kind, exhibit = edgar.classify_document(name, filing.primary_document)
            exhibit = exhibit if kind == "exhibit" else None
        registered = filing.accession in known_tags
        tag = known_tags.get(filing.accession) or source_tag(ticker, filing, cal)
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
        "Each document below opens with a header line that names its source tag; cite it as [src:TAG] "
        "or [src:TAG#LOCATOR].",
        *[f"Note: {note}." for note in results.notes],
        "",
    ]
    sources = []
    for doc in results.documents:
        items = f" (Items {', '.join(doc.items)})" if doc.items else ""
        lines += [f"===== [src:{doc.cite}] {doc.form}{items} | accession {doc.accession} | filed {doc.filed} | "
                  f"{doc.document} =====", f"url: {doc.url}", "", doc.text.strip(), ""]
        sources.append({"kind": "edgar", "tag": doc.cite, "registered": doc.registered, "form": doc.form,
                        "accession": doc.accession, "filed": doc.filed.isoformat(), "document": doc.document,
                        "url": doc.url, "sha256": doc.raw_sha256, "text_sha256": sha256_text(doc.text)})
    return BuiltInput("\n".join(lines), "txt", sources, note="; ".join(results.notes) or None)


@assembler("event")
def _event(ctx: RunContext, name: str) -> BuiltInput:
    company = _need_company(ctx, name)
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
    label = entry
    for separator in _LABEL_SEPARATORS:
        label = label.split(separator, 1)[0]
    low = label.lower()
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
    """The question list HQ froze for this company and period: the placed output of the latest 14Q run."""
    company = _need_company(ctx, name)
    runs = [r for r in ctx.runs() if r.scope == company and r.part == "14Q" and r.period == ctx.period
            and r.placed_file("question_list") is not None]
    if not runs:
        raise MissingInput(f"no placed 14Q question list for {company} {ctx.period} (run 14Q, merge it, then place it)")
    run = runs[-1]
    path = run.placed_file("question_list")
    if path is None:  # pragma: no cover - filtered above
        raise MissingInput(f"{run.rel} has no placed question_list")
    return run, path.read_text(encoding="utf-8")


@assembler("question_list")
def _question_list(ctx: RunContext, name: str) -> BuiltInput:
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
        if data in (None, _outputs.EMPTY_MARK):
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
    if log.is_file():
        for line in log.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and str(record.get("timestamp") or "")[:7] == ctx.period:
                key = str(record.get("part_id") or record.get("prompt_id"))
                calls[key] = calls.get(key, 0) + 1
    data = {"month": ctx.period, **status, "requests": sum(calls.values()), "requests_by_part": calls,
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
