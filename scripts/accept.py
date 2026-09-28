#!/usr/bin/env python3
"""Phase acceptance script for Owner's Office.

Usage (from any directory):

    .venv/bin/python owners-office/scripts/accept.py --phase 0|1
        [--workspace <workspace>] [--json]
        [--write-report docs/acceptance/phase-N.md] [--allow-uncommitted] [--as-of 2026-12-08]

Workspace layout: <workspace>/ holds the sibling directories thesis-ci, owners-office and owners-office-private.
When <workspace>/inputs/DESIGN.md exists, A2 compares its sha256 with the owner's Chinese original of the design,
owners-office/zh-CN/docs/DESIGN.md (before that file exists: with owners-office/docs/DESIGN.md). The default
workspace is the parent of the repository holding this script. A relative --write-report path is relative to the
owners-office repository root.

Phase 0 (DESIGN.md: lint passes; at least 5 thesis tests per company; every constitution rule maps to an executable
check) expands into A1-A7, each printed as PASS / FAIL / SKIP with details.

Phase 1 (DESIGN.md: every pre-registration merged before the results release and timestamped; every filing's update
merged within 7 days of the filing entering EDGAR; plus the first monthly letter, STATUS T12) expands into P1-P4:

- P1 pre-registrations: every earnings event of a holding in the window has a pre-registration items file; every
  pre-registration in the window (holding or candidate) is merged into the default branch before its deadline, has
  .ots proofs next to it and a settlement file whose merged_at matches git, and its deadline precedes EDGAR
  acceptance. Single-file checks are left to thesis-ci's C-PREREG-TIMING and C-PREREG-IMMUTABLE.
- P2 updates: for every earnings filing of a holding or candidate in the window, an update record is merged as final
  into the default branch within 7 x 24 hours of EDGAR acceptance.
- P3 the first monthly letter, letters/2026-10.md, is on the default branch as final by 2026-11-02 (US Eastern).
- P4 phase 0's A2-A7 still hold.

The window is DESIGN's "mid-October to end of November 2026" (US Eastern dates, inclusive). Earnings events come from
the items and settlement files under each company's prereg/, the company list from thesis.yml's status. Merge times
are commit times on the default branch's first-parent history (GitHub creates merge, squash and rebased commits when
a pull request is merged). Items that are not due yet are PENDING, not FAIL. --as-of evaluates at another time
(default: now; commits made after it count as not made yet).

Exit codes: 0 with neither FAIL nor PENDING (with a SKIP the result is only a provisional pass and cannot be used to
advance to the next phase); 1 with a FAIL; 2 for usage or environment errors; 3 with no FAIL but items still PENDING.
Depends only on the standard library and PyYAML.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - environment problem
    print("PyYAML is required: pip install -r requirements.txt", file=sys.stderr)
    sys.exit(2)

try:  # US Eastern time: EDGAR dates, pre-registration deadlines and the letter's due date are all in it
    from zoneinfo import ZoneInfo

    NY: dt.tzinfo | None = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - no time zone database; phase 1 refuses to run
    NY = None
UTC = dt.timezone.utc

REPOS = ("thesis-ci", "owners-office", "owners-office-private")
TEST_TYPES = ("quantitative", "qualitative", "staleness")
MIN_TESTS = 5
MAX_CLAUDE_LINES = 200
MIN_AGENTS = 5
LINT_TIMEOUT = 600
TEST_TIMEOUT = 900

PASS, FAIL, SKIP, PENDING = "PASS", "FAIL", "SKIP", "PENDING"
RANK = {PASS: 0, SKIP: 1, PENDING: 2, FAIL: 3}  # a criterion takes the worst status of its lines


@dataclasses.dataclass
class Criterion:
    id: str
    title: str
    status: str = PASS
    detail: str = ""
    items: list[str] = dataclasses.field(default_factory=list)

    def fail(self, item: str) -> None:
        self.status = FAIL
        self.items.append(item)

    def record(self, status: str | None, text: str, indent: int = 0) -> None:
        """Add a detail line; a line with a status makes the criterion no better than it. None is a plain note."""
        self.items.append("  " * indent + (f"[{status}] " if status else "") + text)
        if status is not None and RANK[status] > RANK[self.status]:
            self.status = status


@dataclasses.dataclass
class Run:
    returncode: int
    stdout: str
    stderr: str

    def tail(self, lines: int = 20) -> list[str]:
        text = (self.stdout + "\n" + self.stderr).strip()
        return text.splitlines()[-lines:] if text else []


def run(cmd: list[str], cwd: Path, timeout: int, extra_path: str | None = None) -> Run:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    if extra_path:  # lets the child find tools installed next to this Python (the ots that thesis-ci calls)
        env["PATH"] = extra_path + os.pathsep + env.get("PATH", "")
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=env, check=False,
        )
    except FileNotFoundError as exc:
        return Run(127, "", f"executable not found: {exc}")
    except subprocess.TimeoutExpired:
        return Run(124, "", f"timed out after {timeout} s: {' '.join(cmd)}")
    return Run(proc.returncode, proc.stdout, proc.stderr)


def git(repo: Path, *args: str) -> str | None:
    result = run(["git", "-C", str(repo), *args], cwd=repo, timeout=60)
    return result.stdout.strip() if result.returncode == 0 else None


def tool_dir() -> str:
    """The directory of this Python (the virtual environment's bin), where thesis-ci and ots are installed."""
    return str(Path(sys.executable).parent)  # not resolved, for the reason below


def find_thesis_ci() -> str | None:
    """thesis-ci next to this Python (the virtual environment) first, then on PATH."""
    here = Path(sys.executable).parent  # not resolved: a virtual environment's python is often a symlink
    for name in ("thesis-ci", "thesis-ci.exe"):
        candidate = here / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which("thesis-ci")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def company_dirs(public_repo: Path) -> list[Path]:
    root = public_repo / "companies"
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith((".", "_")))


# ---------------------------------------------------------------- parsing thesis-ci's JSON output


def load_json_output(text: str) -> Any:
    """Parse the JSON in a command's output, tolerating log lines around it. None when it cannot be parsed."""
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    end = max(text.rfind("}"), text.rfind("]"))
    if not starts or end <= min(starts):
        return None
    try:
        return json.loads(text[min(starts) : end + 1])
    except json.JSONDecodeError:
        return None


CONTAINER_KEYS = ("findings", "issues", "violations", "problems", "diagnostics", "messages", "results", "errors", "warnings")
LEVEL_KEYS = ("level", "severity")
CHECK_KEYS = ("check", "check_id", "id", "code", "rule")
WHERE_KEYS = ("path", "file", "location", "where")
MESSAGE_KEYS = ("message", "msg", "detail", "text", "description", "title")
PASS_STATUSES = {"pass", "passed", "ok", "success"}


def _level(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.lower()
    if value.startswith("err") or value in {"fail", "failed", "failure", "fatal", "critical"}:
        return "error"
    if value.startswith("warn"):
        return "warning"
    if value in {"info", "note", "notice", "hint"}:
        return "info"
    return None


def _first(obj: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = obj.get(key)
        if isinstance(value, (str, int)) and str(value):
            return str(value)
    return None


def describe(finding: Any) -> str:
    if isinstance(finding, str):
        return finding
    if not isinstance(finding, dict):
        return json.dumps(finding, ensure_ascii=False)
    where = _first(finding, WHERE_KEYS)
    line = finding.get("line")
    if where and isinstance(line, int):
        where = f"{where}:{line}"
    parts = [_first(finding, CHECK_KEYS), where, _first(finding, MESSAGE_KEYS)]
    text = " ".join(p for p in parts if p)
    return text or json.dumps(finding, ensure_ascii=False)


def collect_findings(obj: Any, default: str | None = None, out: list | None = None) -> list[tuple[str, str]]:
    """Collect (level, description) pairs from lint JSON of unknown shape.

    Understands: a list of findings; {"findings": [...]}; {"errors": [...], "warnings": [...]}; findings grouped by
    check. A finding without a level field takes the level of the list it is in (errors / warnings).
    """
    out = [] if out is None else out
    if isinstance(obj, list):
        for item in obj:
            collect_findings(item, default, out)
        return out
    if isinstance(obj, str):
        if default:
            out.append((default, obj))
        return out
    if not isinstance(obj, dict):
        return out
    level = next((lvl for key in LEVEL_KEYS if (lvl := _level(obj.get(key)))), None) or default
    containers = [key for key in CONTAINER_KEYS if isinstance(obj.get(key), list)]
    if containers:
        for key in containers:
            child = "error" if key == "errors" else "warning" if key == "warnings" else level
            collect_findings(obj[key], child, out)
        return out
    status = obj.get("status")
    if (isinstance(status, str) and status.lower() in PASS_STATUSES) or obj.get("passed") is True:
        return out
    if level:
        out.append((level, describe(obj)))
        return out
    for value in obj.values():
        if isinstance(value, (dict, list)):
            collect_findings(value, default, out)
    return out


def _count(obj: Any, keys: tuple[str, ...]) -> int | None:
    if not isinstance(obj, dict):
        return None
    for holder in (obj, obj.get("summary"), obj.get("totals"), obj.get("counts")):
        if isinstance(holder, dict):
            for key in keys:
                if isinstance(holder.get(key), int) and not isinstance(holder.get(key), bool):
                    return holder[key]
    return None


@dataclasses.dataclass
class LintOutcome:
    errors: list[str]
    warnings: list[str]
    n_errors: int
    n_warnings: int


def parse_lint(data: Any) -> LintOutcome:
    found = collect_findings(data)
    errors = [text for level, text in found if level == "error"]
    warnings = [text for level, text in found if level == "warning"]
    n_errors = max(len(errors), _count(data, ("errors", "error_count", "n_errors")) or 0)
    n_warnings = max(len(warnings), _count(data, ("warnings", "warning_count", "n_warnings")) or 0)
    return LintOutcome(errors, warnings, n_errors, n_warnings)


@dataclasses.dataclass(frozen=True)
class LintFinding:
    """One lint finding with its file path (phase 1 attributes findings to earnings events by file)."""

    level: str  # error | warning
    check: str
    file: str  # relative to the repository root, forward slashes
    line: int | None
    message: str

    def describe(self) -> str:
        where = f"{self.file}:{self.line}" if self.file and self.line else self.file
        return " ".join(p for p in ("thesis-ci", self.check, where, self.message) if p)


def lint_findings(data: Any) -> list[LintFinding] | None:
    """`thesis-ci lint --format json` output ({"errors": [...], "warnings": [...]}, or a list of findings with a
    level) -> findings. None for a shape it does not recognise."""
    if isinstance(data, dict) and any(isinstance(data.get(key), list) for key in ("errors", "warnings")):
        pairs = [("error", f) for f in data.get("errors") or []] + [("warning", f) for f in data.get("warnings") or []]
    elif isinstance(data, list):
        pairs = [
            (next((lvl for key in LEVEL_KEYS if (lvl := _level(f.get(key)))), None), f) for f in data if isinstance(f, dict)
        ]
    else:
        return None
    out = []
    for level, finding in pairs:
        if level not in ("error", "warning"):
            continue
        if isinstance(finding, str):
            out.append(LintFinding(level, "?", "", None, finding))
        elif isinstance(finding, dict):
            line = finding.get("line")
            out.append(LintFinding(
                level,
                _first(finding, CHECK_KEYS) or "?",
                (_first(finding, WHERE_KEYS) or "").replace(os.sep, "/"),
                line if isinstance(line, int) and not isinstance(line, bool) else None,
                _first(finding, MESSAGE_KEYS) or "",
            ))
    return out


TRUE_WORDS = {"yes", "true", "ok", "pass", "passed", "implemented", "registered", "tested"}


def _flag(info: dict[str, Any], keys: tuple[str, ...]) -> bool | None:
    for key in keys:
        if key in info:
            value = info[key]
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return value > 0
            if isinstance(value, (list, dict)):
                return len(value) > 0
            if isinstance(value, str):
                return value.lower() in TRUE_WORDS
            return False
    return None


def parse_checks(data: Any) -> dict[str, dict[str, Any]] | None:
    """`thesis-ci checks --format json` -> {check id: info}. Understands a list, {"checks": [...]} and {id: {...}}."""
    items = data.get("checks") if isinstance(data, dict) and "checks" in data else data
    if isinstance(items, dict):
        items = [{"id": key, **(value if isinstance(value, dict) else {})} for key, value in items.items()]
    if not isinstance(items, list):
        return None
    table: dict[str, dict[str, Any]] = {}
    for item in items:
        if isinstance(item, str):
            table[item] = {}
        elif isinstance(item, dict):
            check_id = item.get("id") or item.get("check") or item.get("check_id")
            if isinstance(check_id, str):
                table[check_id] = item
    return table


# ---------------------------------------------------------------- acceptance criteria


class Context:
    def __init__(self, workspace: Path, allow_uncommitted: bool, now: dt.datetime | None = None):
        self.ws = workspace
        self.pub = workspace / "owners-office"
        self.priv = workspace / "owners-office-private"
        self.tci_repo = workspace / "thesis-ci"
        self.allow_uncommitted = allow_uncommitted
        self.tci = find_thesis_ci()
        self.now = now if now is not None else dt.datetime.now(UTC)  # phase 1's evaluation time (time-zone aware)
        self._selftest: Run | None = None
        self._histories: dict[Path, History] = {}
        self._companies: list[Company] | None = None
        self._prereg: tuple[list[LintFinding], str | None] | None = None
        self._updates: dict[str, list[UpdateRecord]] = {}

    def selftest(self) -> Run:
        if self._selftest is None:
            if self.tci is None:
                self._selftest = Run(127, "", "thesis-ci executable not found")
            else:
                self._selftest = run([self.tci, "selftest"], cwd=self.ws, timeout=LINT_TIMEOUT)
        return self._selftest

    @property
    def today(self) -> dt.date:
        """The evaluation time's date in US Eastern time."""
        return self.now.astimezone(NY).date()

    @property
    def window_closed(self) -> bool:
        return self.now > end_of_day(PHASE1_WINDOW[1])

    def history(self, repo: Path) -> History:
        if repo not in self._histories:
            self._histories[repo] = History(repo, self.now)
        return self._histories[repo]

    def companies(self) -> list[Company]:
        if self._companies is None:
            self._companies = load_companies(self)
        return self._companies

    def prereg_findings(self) -> tuple[list[LintFinding], str | None]:
        if self._prereg is None:
            self._prereg = prereg_lint(self)
        return self._prereg

    def update_records(self, ticker: str) -> list[UpdateRecord]:
        if ticker not in self._updates:
            self._updates[ticker] = update_records(self, ticker)
        return self._updates[ticker]


def a1_repositories(ctx: Context) -> Criterion:
    c = Criterion("A1", "The three repositories exist, each a git repository with at least one commit")
    for name in REPOS:
        repo = ctx.ws / name
        if not repo.is_dir():
            c.fail(f"{name}: directory missing")
            continue
        if ctx.allow_uncommitted:
            c.items.append(f"{name}: directory exists (--allow-uncommitted: git not checked)")
            continue
        top = git(repo, "rev-parse", "--show-toplevel")
        if top is None or Path(top).resolve() != repo.resolve():
            c.fail(f"{name}: not a git repository of its own")
            continue
        count = git(repo, "rev-list", "--count", "HEAD")
        if not count or not count.isdigit() or int(count) < 1:
            c.fail(f"{name}: no commits yet")
            continue
        c.items.append(f"{name}: {count} commits")
    if c.status == PASS and ctx.allow_uncommitted:
        c.status = SKIP
        c.detail = "git checks skipped; this result is only provisional"
    return c


def a2_design(ctx: Context) -> Criterion:
    """docs/DESIGN.md exists; the owner's Chinese original is byte-identical to inputs/DESIGN.md.

    The original is zh-CN/docs/DESIGN.md, and docs/DESIGN.md is its English translation (only its existence is
    checked). Before zh-CN/docs/DESIGN.md exists, docs/DESIGN.md is the original and is compared itself.
    """
    c = Criterion("A2", "docs/DESIGN.md exists, and the owner's original design matches inputs/DESIGN.md")
    english = ctx.pub / "docs" / "DESIGN.md"
    chinese = ctx.pub / "zh-CN" / "docs" / "DESIGN.md"
    if not english.is_file():
        c.fail("owners-office/docs/DESIGN.md does not exist")
    if chinese.is_file():
        original, label = chinese, "zh-CN/docs/DESIGN.md"
        if english.is_file():
            c.items.append("docs/DESIGN.md exists (English translation; not compared)")
    else:
        original, label = english, "docs/DESIGN.md"
        c.detail = "zh-CN/docs/DESIGN.md does not exist yet, so docs/DESIGN.md is compared as the original"
    if not original.is_file():
        return c
    source = ctx.ws / "inputs" / "DESIGN.md"
    if not source.is_file():
        c.detail = "; ".join(filter(None, [c.detail, "the workspace has no inputs/, so only existence is checked"]))
        c.items.append(f"{label} sha256 {sha256_file(original)}")
        return c
    a, b = sha256_file(source), sha256_file(original)
    if a == b:
        c.items.append(f"{label} matches inputs/DESIGN.md: sha256 {a}")
    else:
        c.fail(f"sha256 differs: inputs/DESIGN.md {a} != {label} {b}")
    return c


def a3_required_files(ctx: Context) -> Criterion:
    c = Criterion("A3", "Required files are present")
    pub, priv = ctx.pub, ctx.priv
    for rel in (
        "repo.yml",
        "CLAUDE.md",
        "docs/STATUS.md",
        "constitution/owner.md",
        "constitution/masters.md",
        "constitution/rules.yml",
        "constitution/decision-rights.yml",
        "pipeline/llm.py",
    ):
        if not (pub / rel).is_file():
            c.fail(f"owners-office/{rel} is missing")
    claude = pub / "CLAUDE.md"
    if claude.is_file():
        lines = len(claude.read_text(encoding="utf-8").splitlines())
        if lines > MAX_CLAUDE_LINES:
            c.fail(f"CLAUDE.md has {lines} lines, more than {MAX_CLAUDE_LINES}")
        else:
            c.items.append(f"CLAUDE.md: {lines} lines")
    decisions = sorted((pub / "docs" / "decisions").glob("*.md"))
    if not decisions:
        c.fail("no decision records in docs/decisions/")
    else:
        c.items.append(f"{len(decisions)} decision records")
    agents = sorted((pub / "agents").glob("*.yml"))
    if len(agents) < MIN_AGENTS:
        c.fail(f"only {len(agents)} agents/*.yml, at least {MIN_AGENTS} needed")
    else:
        c.items.append(f"{len(agents)} agents/*.yml")
    workflows = [
        p
        for p in sorted((pub / ".github" / "workflows").glob("*.y*ml"))
        if re.search(r"thesis-ci\s+lint", p.read_text(encoding="utf-8"))
    ]
    if not workflows:
        c.fail("no workflow in .github/workflows/ runs thesis-ci lint")
    else:
        c.items.append("workflows running thesis-ci lint: " + ", ".join(p.name for p in workflows))
    for repo, expected in ((pub, "public"), (priv, "private")):
        marker = repo / "repo.yml"
        if not marker.is_file():
            c.fail(f"{repo.name}/repo.yml is missing")
            continue
        data = load_yaml(marker) or {}
        if data.get("visibility") != expected:
            c.fail(f"{repo.name}/repo.yml: visibility should be {expected}")
    companies = company_dirs(pub)
    missing = [d.name for d in companies if not (priv / "companies" / d.name / "valuation.yml").is_file()]
    for name in missing:
        c.fail(f"owners-office-private/companies/{name}/valuation.yml is missing")
    if companies and not missing:
        c.items.append(f"private valuation.yml files cover all {len(companies)} public companies")
    return c


def a4_lint(ctx: Context) -> Criterion:
    c = Criterion("A4", "thesis-ci lint: zero errors in the public and private repositories")
    if ctx.tci is None:
        c.fail("thesis-ci executable not found (virtual environment or PATH)")
        return c
    runs = (
        ("owners-office", [ctx.tci, "lint", str(ctx.pub), "--format", "json"]),
        (
            "owners-office-private",
            [ctx.tci, "lint", str(ctx.priv), "--counterpart", str(ctx.pub), "--format", "json"],
        ),
    )
    for label, cmd in runs:
        result = run(cmd, cwd=ctx.ws, timeout=LINT_TIMEOUT, extra_path=tool_dir())
        data = load_json_output(result.stdout)
        if data is None:
            c.fail(f"{label}: cannot parse lint's JSON output (exit code {result.returncode})")
            c.items.extend(f"  {line}" for line in result.tail())
            continue
        outcome = parse_lint(data)
        if outcome.n_errors:
            c.fail(f"{label}: {outcome.n_errors} errors, {outcome.n_warnings} warnings")
            c.items.extend(f"  error {text}" for text in outcome.errors)
        elif result.returncode != 0:
            c.fail(f"{label}: no errors parsed, but lint exited with code {result.returncode}")
            c.items.extend(f"  {line}" for line in result.tail())
        else:
            c.items.append(f"{label}: 0 errors, {outcome.n_warnings} warnings")
        c.items.extend(f"  warning {text}" for text in outcome.warnings)
    return c


def a5_thesis_tests(ctx: Context) -> Criterion:
    c = Criterion("A5", f"Every company has at least {MIN_TESTS} thesis tests (all three types) and a story.md")
    companies = company_dirs(ctx.pub)
    if not companies:
        c.fail("no companies under owners-office/companies/")
        return c
    holdings = []
    for d in companies:
        thesis, story = d / "thesis.yml", d / "story.md"
        if not story.is_file():
            c.fail(f"{d.name}: story.md is missing")
        if not thesis.is_file():
            c.fail(f"{d.name}: thesis.yml is missing")
            continue
        try:
            data = load_yaml(thesis) or {}
        except yaml.YAMLError as exc:
            c.fail(f"{d.name}: thesis.yml is not valid YAML ({exc.__class__.__name__})")
            continue
        tests = data.get("tests") if isinstance(data, dict) else None
        tests = tests if isinstance(tests, list) else []
        types = {t.get("type") for t in tests if isinstance(t, dict)}
        missing = [t for t in TEST_TYPES if t not in types]
        status = data.get("status") if isinstance(data, dict) else None
        if status == "holding":
            holdings.append(d.name)
        summary = f"{d.name} ({status or 'no status'}): {len(tests)} tests"
        if len(tests) < MIN_TESTS or missing:
            reason = f"fewer than {MIN_TESTS}" if len(tests) < MIN_TESTS else ""
            if missing:
                reason = "; ".join(filter(None, [reason, "missing types " + ", ".join(missing)]))
            c.fail(f"{summary}, {reason}")
        else:
            c.items.append(summary + ", all three types")
    c.detail = "holdings: " + (", ".join(holdings) if holdings else "none")
    return c


def a6_constitution_checks(ctx: Context) -> Criterion:
    c = Criterion("A6", "Every constitution rule maps to checks that are implemented and pass selftest")
    rules_path = ctx.pub / "constitution" / "rules.yml"
    if not rules_path.is_file():
        c.fail("constitution/rules.yml does not exist")
        return c
    rules = (load_yaml(rules_path) or {}).get("rules") or []
    if not rules:
        c.fail("constitution/rules.yml has no rules")
        return c
    if ctx.tci is None:
        c.fail("thesis-ci executable not found")
        return c
    result = run([ctx.tci, "checks", "--format", "json"], cwd=ctx.ws, timeout=LINT_TIMEOUT)
    table = parse_checks(load_json_output(result.stdout))
    if result.returncode != 0 or table is None:
        c.fail(f"thesis-ci checks --format json failed or its output cannot be parsed (exit code {result.returncode})")
        c.items.extend(f"  {line}" for line in result.tail())
        return c
    selftest_ok = ctx.selftest().returncode == 0
    notes: set[str] = set()
    for rule in rules:
        rule_id = rule.get("id", "?") if isinstance(rule, dict) else "?"
        checks = rule.get("checks") if isinstance(rule, dict) else None
        if not checks:
            c.fail(f"{rule_id}: references no check")
            continue
        problems = []
        for check_id in checks:
            info = table.get(check_id)
            if info is None:
                problems.append(f"{check_id} is not registered in thesis-ci")
                continue
            implemented = _flag(info, ("implemented", "has_impl", "has_implementation", "registered"))
            tested = _flag(info, ("tested", "has_test", "has_tests", "unit_tests", "tests", "test_count"))
            passed = _flag(info, ("selftest", "selftest_passed", "selftest_ok", "passes_selftest"))
            if implemented is False:
                problems.append(f"{check_id} is not implemented")
            elif implemented is None:
                notes.add("the checks output has no implementation field; a listed check counts as implemented")
            if tested is False:
                problems.append(f"{check_id} has no unit test")
            if passed is False:
                problems.append(f"{check_id} fails selftest")
            elif passed is None and not selftest_ok:
                problems.append(f"{check_id}: thesis-ci selftest fails as a whole")
        if problems:
            c.fail(f"{rule_id}: " + "; ".join(problems))
        else:
            c.items.append(f"{rule_id}: " + ", ".join(checks))
    c.detail = "; ".join(sorted(notes))
    return c


def a7_tests(ctx: Context) -> Criterion:
    c = Criterion("A7", "thesis-ci selftest exits with 0; pytest passes in thesis-ci and owners-office")
    selftest = ctx.selftest()
    if selftest.returncode == 0:
        c.items.append("thesis-ci selftest: passed")
    else:
        c.fail(f"thesis-ci selftest: exit code {selftest.returncode}")
        c.items.extend(f"  {line}" for line in selftest.tail())
    for label, repo, extra in (("thesis-ci", ctx.tci_repo, []), ("owners-office", ctx.pub, ["tests"])):
        if not repo.is_dir():
            c.fail(f"{label}: directory missing")
            continue
        cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *extra]
        result = run(cmd, cwd=repo, timeout=TEST_TIMEOUT)
        last = result.tail(1)[0] if result.tail(1) else ""
        if result.returncode == 0:
            c.items.append(f"{label} pytest: {last}")
        elif result.returncode == 5:
            c.fail(f"{label} pytest: no tests collected")
        else:
            c.fail(f"{label} pytest: exit code {result.returncode}")
            c.items.extend(f"  {line}" for line in result.tail())
    return c


# ---------------------------------------------------------------- phase 1: window, due times and time handling

# DESIGN.md roadmap: phase 1 runs "mid-October to end of November 2026 (the Q3 earnings season)". Mid-October is
# taken as the 15th. US Eastern dates, both ends included.
PHASE1_WINDOW = (dt.date(2026, 10, 15), dt.date(2026, 11, 30))
UPDATE_WITHIN = dt.timedelta(hours=7 * 24)  # update merged within 7 x 24 hours of the filing's EDGAR acceptance
MERGE_LEAD_HOURS = 72  # 15A / STATUS T11: merge at least 72 hours before the deadline. A note only: DESIGN asks for "before release"
MERGE_TOLERANCE = dt.timedelta(seconds=60)  # allowed gap between the settlement's merged_at and the git commit time
FIRST_LETTER = "letters/2026-10.md"  # STATUS T12: the first monthly letter, covering October
FIRST_LETTER_DUE = dt.date(2026, 11, 2)  # at the latest November 2 (US Eastern)
TRACKED = ("holding", "candidate")  # phase 1 checks holdings and candidates, not archive
PREREG_CHECKS = ("C-PREREG-TIMING", "C-PREREG-IMMUTABLE")
PREREG_NAME = re.compile(r"^(FY\d{4}Q[1-4])(-owner|\.settlement)?\.yml$")  # the three file names of SPEC §2.1
PREREG_FILE = re.compile(r"^companies/([^/]+)/prereg/(FY\d{4}Q[1-4])(?:-owner)?\.(?:yml(?:\.ots)?|settlement\.yml)$")

Line = tuple[str | None, str]  # (status or None, detail)


def to_datetime(value: Any) -> dt.datetime | None:
    """ISO 8601 with a UTC offset (Z allowed) or EDGAR's YYYYMMDDHHMMSS (US Eastern) -> aware datetime.

    None for values without a time zone and for anything unreadable.
    """
    if isinstance(value, dt.datetime):
        moment = value
    elif isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"\d{14}", text):
            return dt.datetime.strptime(text, "%Y%m%d%H%M%S").replace(tzinfo=NY)
        if text[-1:] in ("Z", "z"):
            text = text[:-1] + "+00:00"
        try:
            moment = dt.datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    return moment if moment.utcoffset() is not None else None


def to_date(value: Any) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        return dt.date.fromisoformat(value.strip())
    return None


def end_of_day(day: dt.date) -> dt.datetime:
    """23:59:59 US Eastern on that day."""
    return dt.datetime.combine(day, dt.time(23, 59, 59), tzinfo=NY)


def ny(moment: dt.datetime) -> str:
    """ISO 8601 in US Eastern time, to the second."""
    return moment.astimezone(NY).isoformat(timespec="seconds")


def hours(delta: dt.timedelta) -> str:
    return f"{delta.total_seconds() / 3600:.1f}"


def relpath(repo: Path, path: Path) -> str:
    return path.relative_to(repo).as_posix()


def window_text(ctx: Context) -> str:
    start, end = PHASE1_WINDOW
    return f"window {start} to {end} (US Eastern dates, inclusive); evaluated at {ny(ctx.now)}"


def front_matter(text: str) -> dict[str, Any] | None:
    """The YAML mapping between the two --- lines at the top of a Markdown file; None if absent or unreadable."""
    lines = text.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for end, line in enumerate(lines[1:], 1):
        if line.strip() in ("---", "..."):
            try:
                data = yaml.safe_load("\n".join(lines[1:end]))
            except yaml.YAMLError:
                return None
            return data if isinstance(data, dict) else None
    return None


def read_mapping(path: Path) -> tuple[dict[str, Any], str | None]:
    try:
        data = load_yaml(path)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        return {}, f"{path.name} cannot be read ({exc.__class__.__name__})"
    if not isinstance(data, dict):
        return {}, f"{path.name} is not a YAML mapping"
    return data, None


# ---------------------------------------------------------------- phase 1: history of the default branch


@dataclasses.dataclass(frozen=True)
class Commit:
    sha: str
    when: dt.datetime  # committer date

    @property
    def short(self) -> str:
        return self.sha[:7]


def default_branch(repo: Path) -> str | None:
    """The default branch: where origin/HEAD points, else the first of origin/main, origin/master, main, master.

    Remote branches come first: a commit that is only local has not been merged into the default branch.
    """
    head = git(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if head:
        return head
    for ref in ("origin/main", "origin/master", "main", "master"):
        if git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"):
            return ref
    return None


class History:
    """The first-parent history of a repository's default branch.

    A merge time is a commit time on this history: a pull request's merge commit, squash commit and rebased commits
    are all created when it is merged, so earlier commits on the topic branch do not count. Commits made after
    `until` count as not made yet.
    """

    def __init__(self, repo: Path, until: dt.datetime):
        self.repo, self.until = repo, until
        self.ref: str | None = None
        self.error: str | None = None
        self._commits: dict[str, list[Commit]] = {}
        self._texts: dict[tuple[str, str], str | None] = {}
        top = git(repo, "rev-parse", "--show-toplevel") if repo.is_dir() else None
        if top is None or Path(top).resolve() != repo.resolve():
            self.error = f"{repo.name} is not a git repository of its own, so merge times cannot be checked"
        elif git(repo, "rev-parse", "--is-shallow-repository") == "true":
            self.error = (f"{repo.name} is a shallow clone: its history is incomplete, so merge times cannot be checked "
                          "(check out with fetch-depth: 0 in CI)")
        else:
            self.ref = default_branch(repo)
            if self.ref is None:
                self.error = f"{repo.name} has no default branch (origin/HEAD, origin/main, origin/master, main, master)"

    def commits(self, rel: str) -> list[Commit]:
        """The commits on the default branch that changed rel, oldest first."""
        if self.ref is None:
            return []
        if rel not in self._commits:
            out = git(self.repo, "log", "--first-parent", "--format=%H %cI", self.ref, "--", rel) or ""
            found = []
            for line in reversed(out.splitlines()):
                sha, _, stamp = line.partition(" ")
                when = to_datetime(stamp)
                if when is not None and when <= self.until:
                    found.append(Commit(sha, when))
            self._commits[rel] = found
        return self._commits[rel]

    def paths(self, rel_dir: str) -> list[str]:
        """Every path under rel_dir that has been on the default branch, including ones deleted or moved since."""
        if self.ref is None:
            return []
        out = git(self.repo, "log", "--first-parent", "--no-renames", "--format=", "--name-only", self.ref, "--", rel_dir)
        return sorted({line.strip() for line in (out or "").splitlines() if line.strip()})

    def text(self, commit: Commit, rel: str) -> str | None:
        key = (commit.sha, rel)
        if key not in self._texts:
            result = run(["git", "-C", str(self.repo), "show", f"{commit.sha}:{rel}"], cwd=self.repo, timeout=60)
            self._texts[key] = result.stdout if result.returncode == 0 else None
        return self._texts[key]

    def first_where(self, rel: str, wanted: Callable[[dict[str, Any]], bool]) -> Commit | None:
        """The first commit at which rel's front matter satisfies `wanted`."""
        for commit in self.commits(rel):
            text = self.text(commit, rel)
            if text is not None and wanted(front_matter(text) or {}):
                return commit
        return None


# ---------------------------------------------------------------- phase 1: companies and earnings events


@dataclasses.dataclass
class Event:
    """An earnings event (company x period), assembled from the prereg/ files of one period (SPEC §2.1)."""

    ticker: str
    period: str
    items: Path | None = None  # <period>.yml: the system's items file
    owner: Path | None = None  # <period>-owner.yml: the owner's items and overrides
    settlement: Path | None = None  # <period>.settlement.yml
    deadline: dt.datetime | None = None
    expected_release: dt.date | None = None
    placeholder: bool = False
    merged_at: Any = None
    acceptance: dt.datetime | None = None
    acceptance_from: str | None = None  # "settlement file" | "EDGAR"
    accession: str | None = None
    ots_proof: Any = None
    problems: list[str] = dataclasses.field(default_factory=list)  # problems with the items files
    settlement_problem: str | None = None

    @property
    def release(self) -> dt.date | None:
        """The release date (US Eastern): from the EDGAR acceptance time when known, else the expected release."""
        return self.acceptance.astimezone(NY).date() if self.acceptance is not None else self.expected_release

    def in_window(self) -> bool:
        return self.release is not None and PHASE1_WINDOW[0] <= self.release <= PHASE1_WINDOW[1]

    @property
    def settlement_name(self) -> str:
        return f"{self.period}.settlement.yml"


@dataclasses.dataclass
class Company:
    ticker: str
    status: str | None
    filer: dict[str, Any]
    events: list[Event] = dataclasses.field(default_factory=list)
    problem: str | None = None

    @property
    def label(self) -> str:
        return f"{self.ticker} ({self.status or 'no status'})"


def edgar_acceptance(ticker: str, period: str, filer: dict[str, Any]) -> tuple[dt.datetime, str | None] | None:
    """Hook: when EDGAR accepted the results filing (8-K item 2.02; a results 6-K for a foreign issuer), and its
    accession number.

    Called only when the settlement file has no acceptance_datetime. Returns (aware acceptance time, accession or
    None), or None when nothing is found; the acceptance time then counts as unknown (a FAIL once the release day has
    passed). `filer` is thesis.yml's filer mapping (cik, type, fiscal_year_end, earnings_form).

    Not wired in yet: pipeline/edgar.py (STATUS T18) is being built separately and is deliberately not imported here.
    To wire it in, replace only this function's body, for example:

        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from pipeline import edgar  # needs SEC_USER_AGENT (decisions/0013)
        estimate = edgar.next_release(filer["cik"], period, fiscal_year_end=filer.get("fiscal_year_end"),
                                      filer_type=filer.get("type"))
        if estimate.event is None:
            return None
        return estimate.event.filing.accepted_utc, estimate.event.accession
    """
    return None


def load_companies(ctx: Context) -> list[Company]:
    """Every company under owners-office/companies/: thesis.yml's status and filer, and the earnings events in prereg/."""
    out = []
    for folder in company_dirs(ctx.pub):
        thesis = folder / "thesis.yml"
        data, problem = read_mapping(thesis) if thesis.is_file() else ({}, "thesis.yml is missing")
        status = data.get("status") if isinstance(data.get("status"), str) else None
        filer = data.get("filer") if isinstance(data.get("filer"), dict) else {}
        company = Company(folder.name, status, filer, problem=problem)
        company.events = load_events(ctx, company, folder / "prereg")
        out.append(company)
    return out


def load_events(ctx: Context, company: Company, folder: Path) -> list[Event]:
    groups: dict[str, dict[str, Path]] = {}
    for path in sorted(folder.glob("*.yml")) if folder.is_dir() else ():
        match = PREREG_NAME.match(path.name)
        if match:
            kind = {None: "items", "-owner": "owner", ".settlement": "settlement"}[match.group(2)]
            groups.setdefault(match.group(1), {})[kind] = path
    events = []
    for period, files in sorted(groups.items()):
        e = Event(company.ticker, period, files.get("items"), files.get("owner"), files.get("settlement"))
        header = None
        for path in (e.items, e.owner):  # the system file's header first; C-PREREG-TIMING checks the deadlines agree
            if path is not None:
                data, problem = read_mapping(path)
                if problem:
                    e.problems.append(problem)
                elif header is None:
                    header = data
        if header is not None:
            event = header.get("event") if isinstance(header.get("event"), dict) else {}
            e.expected_release = to_date(event.get("expected_release"))
            e.placeholder = event.get("placeholder") is True
            e.deadline = to_datetime(header.get("deadline"))
            if e.deadline is None:
                e.problems.append(f"deadline {header.get('deadline')!r} is not an ISO 8601 date-time with a UTC offset")
        if e.settlement is not None:
            data, e.settlement_problem = read_mapping(e.settlement)
            e.merged_at, e.accession, e.ots_proof = data.get("merged_at"), data.get("accession"), data.get("ots_proof")
            raw = data.get("acceptance_datetime")
            if raw is not None:
                e.acceptance = to_datetime(raw)
                if e.acceptance is None:
                    e.settlement_problem = f"{e.settlement.name}: acceptance_datetime {raw!r} has no time zone"
                else:
                    e.acceptance_from = "settlement file"
        # No acceptance time in the settlement file and the release day has come (or is unknown): ask the EDGAR hook
        due = e.expected_release is None or e.expected_release <= ctx.today
        if e.acceptance is None and e.settlement_problem is None and company.status in TRACKED and due:
            found = edgar_acceptance(company.ticker, period, company.filer)
            if found is not None and to_datetime(found[0]) is not None:
                e.acceptance, e.acceptance_from = found[0], "EDGAR"
                e.accession = e.accession or found[1]
        events.append(e)
    return events


def prereg_lint(ctx: Context) -> tuple[list[LintFinding], str | None]:
    """Run only thesis-ci's C-PREREG-TIMING and C-PREREG-IMMUTABLE on the public repository, with --today set to
    the evaluation time's US Eastern date.

    Single-file judgements are left to thesis-ci rather than re-implemented here: the deadline falls before the
    release day, the owner's file shares the system file's deadline, merged_at precedes the deadline, the deadline
    precedes acceptance_datetime, and after the deadline an .ots proof exists and passes `ots verify`.
    Returns (findings, the reason it could not run).
    """
    if ctx.tci is None:
        return [], "thesis-ci executable not found (virtual environment or PATH); cannot run " + ", ".join(PREREG_CHECKS)
    cmd = [ctx.tci, "lint", str(ctx.pub), "--only", *PREREG_CHECKS, "--today", ctx.today.isoformat(), "--format", "json"]
    result = run(cmd, cwd=ctx.ws, timeout=LINT_TIMEOUT, extra_path=tool_dir())
    findings = lint_findings(load_json_output(result.stdout))
    if findings is None or result.returncode not in (0, 1):
        tail = "; ".join(result.tail(3))
        return [], (f"thesis-ci lint --only {' '.join(PREREG_CHECKS)} failed or its output cannot be parsed "
                    f"(exit code {result.returncode}) {tail}").strip()
    if result.returncode == 1 and not any(f.level == "error" for f in findings):
        return findings, "thesis-ci lint exited with code 1, but no errors were parsed"
    return findings, None


# ---------------------------------------------------------------- phase 1: detail lines


def add_event(c: Criterion, header: str, lines: list[Line]) -> None:
    """One earnings event: the header line takes the worst status of its details, which are indented one level."""
    statuses = [status for status, _ in lines if status]
    c.record(max(statuses, key=RANK.__getitem__) if statuses else PASS, header)
    for status, text in lines:
        c.record(status, text, indent=1)


def coverage(ctx: Context, company: Company, hint: str) -> Line:
    """A company with no earnings event in the window: fine if a later release is recorded; else PENDING while the
    window is open and FAIL once it has closed."""
    later = sorted(e.release for e in company.events if e.release is not None and e.release > PHASE1_WINDOW[1])
    if later:
        return PASS, f"{company.label}: no earnings event in the window; the next recorded release, {later[0]}, is after it"
    if ctx.window_closed:
        return FAIL, f"{company.label}: the window has closed and prereg/ has no earnings event released in it ({hint})"
    return PENDING, f"{company.label}: no earnings event recorded in the window yet ({hint})"


def note_other_events(c: Criterion, company: Company, shown: list[Event]) -> None:
    """Note the company's events outside this criterion: released outside the window, or with no release date."""
    others = [e for e in company.events if e not in shown]
    outside = [f"{e.period} ({e.release})" for e in others if e.release is not None and not e.in_window()]
    undated = [e.period for e in others if e.release is None]
    if outside:
        c.record(None, f"{company.ticker} events released outside the window, not part of this phase: {', '.join(outside)}")
    if undated:
        c.record(None, f"{company.ticker} events with neither an expected release nor an EDGAR acceptance time, "
                       f"so the window cannot be decided: {', '.join(undated)}")


def unknown_acceptance(ctx: Context, e: Event) -> Line:
    """The acceptance time is unknown: PENDING until the expected release day has passed, FAIL after."""
    if e.expected_release is not None and e.expected_release >= ctx.today:
        placeholder = ", a placeholder date" if e.placeholder else ""
        return PENDING, f"results not released yet (expected {e.expected_release}{placeholder})"
    where = (f"settlement file {e.settlement.name} has no acceptance_datetime" if e.settlement
             else f"there is no settlement file {e.settlement_name}")
    return FAIL, (f"EDGAR acceptance time unknown: {where}, and the EDGAR hook (edgar_acceptance in accept.py, "
                  "empty until STATUS T18 is wired in) returned nothing")


def event_header(company: Company, e: Event, *, deadline: bool) -> str:
    """An event's header: company, period, release (acceptance) time, plus the deadline (P1) or update due time (P2)."""
    parts = [f"{e.ticker} {e.period} ({company.status})"]
    if e.acceptance is not None:
        parts.append(f"EDGAR accepted {ny(e.acceptance)} ({e.acceptance_from})")
    elif e.expected_release is not None:
        parts.append(f"expected release {e.expected_release}" + (" (placeholder)" if e.placeholder else ""))
    else:
        parts.append("release date unknown")
    if deadline and e.deadline is not None:
        parts.append(f"deadline {ny(e.deadline)}")
    elif not deadline and e.acceptance is not None:
        parts.append(f"update due {ny(e.acceptance + UPDATE_WITHIN)}")
    return " · ".join(parts)


def merge_lines(ctx: Context, path: Path, deadline: dt.datetime, passed: bool) -> list[Line]:
    """An items file first appears (is merged) on the default branch before the deadline and is not changed after."""
    hist = ctx.history(ctx.pub)
    if hist.error:
        return [(FAIL, f"{path.name}: merge time cannot be checked ({hist.error})")]
    commits = hist.commits(relpath(ctx.pub, path))
    if not commits:
        if passed:
            return [(FAIL, f"{path.name} was not merged into {hist.ref} before the deadline")]
        return [(PENDING, f"{path.name} not merged into {hist.ref} yet (deadline {ny(deadline)}; "
                          f"15A asks for at least {MERGE_LEAD_HOURS} hours before it)")]
    first, last = commits[0], commits[-1]
    if first.when >= deadline:
        lines: list[Line] = [(FAIL, f"{path.name} merged into {hist.ref} only at {ny(first.when)} ({first.short}), "
                                    "not before the deadline")]
    else:
        lead = deadline - first.when
        short = ("" if lead >= dt.timedelta(hours=MERGE_LEAD_HOURS)
                 else f"; less than the {MERGE_LEAD_HOURS} hours 15A asks for (a note only)")
        lines = [(PASS, f"{path.name} merged into {hist.ref} at {ny(first.when)} ({first.short}), "
                        f"{hours(lead)} hours before the deadline{short}")]
    if last is not first and last.when >= deadline:
        lines.append((FAIL, f"{path.name} changed on {hist.ref} after the deadline ({last.short}, {ny(last.when)})"))
    return lines


def settlement_lines(ctx: Context, e: Event, passed: bool) -> list[Line]:
    """The settlement file has merged_at (required once the deadline has passed), equal to the time of the commit
    that first added the items file to the default branch."""
    assert e.items is not None
    hist = ctx.history(ctx.pub)
    commits = [] if hist.error else hist.commits(relpath(ctx.pub, e.items))
    lines: list[Line] = []
    if e.merged_at is None:
        what = (f"settlement file {e.settlement.name} has no merged_at" if e.settlement
                else f"no settlement file {e.settlement_name} yet")
        hint = f" (git: first added to {hist.ref} at {ny(commits[0].when)}, {commits[0].short})" if commits else ""
        lines.append((FAIL if passed else PENDING, what + hint))
    else:
        merged = to_datetime(e.merged_at)
        if merged is None:
            lines.append((FAIL, f"merged_at {e.merged_at!r} has no time zone"))
        elif hist.error:
            lines.append((FAIL, f"merged_at {ny(merged)} cannot be checked against git ({hist.error})"))
        elif not commits:
            lines.append((FAIL, f"the settlement file records merged_at {ny(merged)}, but {e.items.name} is not on {hist.ref}"))
        elif abs(merged - commits[0].when) > MERGE_TOLERANCE:
            lines.append((FAIL, f"merged_at {ny(merged)} does not match git: the commit that first added "
                                f"{e.items.name} to {hist.ref}, {commits[0].short}, is at {ny(commits[0].when)}"))
        else:
            lines.append((PASS, f"settlement merged_at {ny(merged)} matches git ({commits[0].short})"))
    if e.ots_proof:  # optional; when present it must name the proof of this period's system items file
        expected = (e.items.parent / (e.items.name + ".ots")).resolve()
        company_dir = e.items.parent.parent
        named = [(base / str(e.ots_proof)).resolve() for base in (company_dir, e.items.parent, ctx.pub)]
        if expected not in named:
            lines.append((FAIL, f"settlement ots_proof {e.ots_proof!r} does not name {e.items.name}.ots"))
    return lines


def acceptance_lines(ctx: Context, e: Event) -> list[Line]:
    assert e.deadline is not None
    if e.acceptance is None:
        return [] if e.settlement_problem else [unknown_acceptance(ctx, e)]
    accession = f", accession {e.accession}" if e.accession else ""
    text = f"{ny(e.acceptance)} ({e.acceptance_from}{accession})"
    if e.acceptance_from == "EDGAR":  # thesis-ci reads only the settlement file and cannot see this time: compare here
        if e.deadline < e.acceptance:
            return [(PASS, f"the deadline precedes the EDGAR acceptance time {text}")]
        return [(FAIL, f"the deadline {ny(e.deadline)} does not precede the EDGAR acceptance time {text}")]
    return [(None, f"EDGAR acceptance time {text}; C-PREREG-TIMING judges it against the deadline")]


def prereg_lines(ctx: Context, e: Event, findings: list[LintFinding], lint_ok: bool) -> list[Line]:
    lines: list[Line] = [(FAIL, problem) for problem in e.problems]
    if e.settlement_problem:
        lines.append((FAIL, e.settlement_problem))
    if e.items is None:  # a holding's earnings event with only a settlement file (or only an owner file)
        due = e.deadline or (end_of_day(e.release - dt.timedelta(days=1)) if e.release else None)
        overdue = due is not None and ctx.now > due
        lines.append((FAIL if overdue else PENDING, f"no system pre-registration items file {e.period}.yml"
                      + (f" (deadline {ny(due)})" if due else "")))
    elif e.deadline is not None:
        passed = ctx.now > e.deadline
        reported = {f.file for f in findings if f.check == "C-PREREG-IMMUTABLE"}  # it reports proofs missing after the deadline
        for path in (e.items, e.owner):
            if path is None:
                continue
            lines += merge_lines(ctx, path, e.deadline, passed)
            proof = path.with_name(path.name + ".ots")
            if proof.is_file():
                lines.append((PASS, f"timestamp proof {proof.name}"))
            elif not passed:
                lines.append((PENDING, f"no timestamp proof {proof.name} yet (stamp it before the deadline)"))
            elif relpath(ctx.pub, path) not in reported:
                lines.append((FAIL, f"timestamp proof {proof.name} is missing (the deadline has passed)"))
        lines += settlement_lines(ctx, e, passed)
        lines += acceptance_lines(ctx, e)
    lines += [(FAIL if f.level == "error" else None, f.describe()) for f in findings]
    if lint_ok and e.items is not None and not any(f.level == "error" for f in findings):
        lines.append((PASS, f"thesis-ci {', '.join(PREREG_CHECKS)}: no errors in this period's files"))
    return lines


# ---------------------------------------------------------------- phase 1: update records


@dataclasses.dataclass(frozen=True)
class UpdateRecord:
    repo: Path
    rel: str  # companies/<TICKER>/updates/<date>.md
    private: bool

    @property
    def where(self) -> str:
        return f"{'private' if self.private else 'public'} repo {self.rel}"


def update_records(ctx: Context, ticker: str) -> list[UpdateRecord]:
    """companies/<TICKER>/updates/*.md in both repositories: those in the working tree, plus those that have been on
    the default branch.

    A level-1 company manager's update is staged in the private repository and published after HQ review
    (decisions/0011, 00 §G9); the private copy may be moved out once published, hence the history.
    """
    rel_dir = f"companies/{ticker}/updates"
    out = []
    for repo, private in ((ctx.pub, False), (ctx.priv, True)):
        if not repo.is_dir():
            continue
        found = {relpath(repo, p) for p in (repo / rel_dir).glob("*.md")}
        hist = ctx.history(repo)
        if hist.error is None:
            found |= {p for p in hist.paths(rel_dir) if posixpath.dirname(p) == rel_dir and p.endswith(".md")}
        out += [UpdateRecord(repo, rel, private) for rel in sorted(found)]
    return out


def final_update(fm: dict[str, Any], period: str) -> bool:
    """A final update record (SPEC §4.2 front matter: doc: update, doc_status: final); if it declares a
    period, it must be this one."""
    return fm.get("doc") == "update" and fm.get("doc_status") == "final" and fm.get("period") in (None, period)


def unfinished_update(record: UpdateRecord, e: Event) -> bool:
    """In the working tree and apparently for this period (doc: update, file date not before the acceptance day),
    but not on the default branch as final."""
    path = record.repo / record.rel
    if e.acceptance is None or not path.is_file():
        return False
    fm = front_matter(path.read_text(encoding="utf-8")) or {}
    if fm.get("doc") != "update" or fm.get("period") not in (None, e.period):
        return False
    day = to_date(Path(record.rel).stem)
    return day is None or day >= e.acceptance.astimezone(NY).date()


def update_lines(ctx: Context, e: Event) -> list[Line]:
    if e.acceptance is None:
        return [(FAIL, e.settlement_problem)] if e.settlement_problem else [unknown_acceptance(ctx, e)]
    accepted, due = e.acceptance, e.acceptance + UPDATE_WITHIN
    lines: list[Line] = []
    on_time, late, waiting = [], [], []
    for record in ctx.update_records(e.ticker):
        hist = ctx.history(record.repo)
        if hist.error:
            lines.append((FAIL, f"{record.where}: merge time cannot be checked ({hist.error})"))
            continue
        final = hist.first_where(record.rel, lambda fm: final_update(fm, e.period))
        if final is None:
            if unfinished_update(record, e):
                waiting.append((record, hist))
        elif final.when >= accepted:  # final before the acceptance: an earlier update
            (on_time if final.when <= due else late).append((final, record, hist))
    if on_time:
        final, record, hist = min(on_time, key=lambda item: item[0].when)
        staged = ("; staged in the private repo (a level-1 update is reviewed by HQ before it is published, 00 §G9)"
                  if record.private else "")
        lines.append((PASS, f"{record.where} merged into {hist.ref} as final at {ny(final.when)} ({final.short}), "
                            f"{hours(final.when - accepted)} hours after EDGAR acceptance{staged}"))
        return lines
    if ctx.now <= due:
        lines.append((PENDING, f"no update record merged as final yet (due {ny(due)})"))
    else:
        lines.append((FAIL, f"no update record merged as final into the default branch within 7 x 24 hours of "
                            f"EDGAR acceptance (due {ny(due)})"))
    for final, record, _hist in late:
        lines.append((None, f"{record.where} merged as final only at {ny(final.when)} "
                            f"({hours(final.when - accepted)} hours after EDGAR acceptance)"))
    for record, hist in waiting:
        lines.append((None, f"{record.where} is not on {hist.ref} as final yet (front matter needs doc: update, "
                            "doc_status: final)"))
    return lines


# ---------------------------------------------------------------- phase 1: P1-P4


def p1_preregistrations(ctx: Context) -> Criterion:
    c = Criterion("P1", "Pre-registrations: every earnings event of a holding in the window has one; each is merged "
                        "into the default branch before its deadline and timestamped, its settlement merged_at matches "
                        "git, and its deadline precedes EDGAR acceptance")
    companies = ctx.companies()
    holdings = [co.ticker for co in companies if co.status == "holding"]
    c.detail = f"{window_text(ctx)}; holdings {', '.join(holdings) or 'none'}"
    hist = ctx.history(ctx.pub)
    if hist.error:
        c.record(FAIL, hist.error)
    scope = {
        co.ticker: [e for e in co.events if e.in_window() and (co.status == "holding" or e.items or e.owner)]
        for co in companies
        if co.status in TRACKED
    }
    findings: dict[tuple[str, str], list[LintFinding]] = {}
    lint_ok = False
    if any(e.items or e.owner for events in scope.values() for e in events):
        found, error = ctx.prereg_findings()
        if error:
            c.record(FAIL, error)
        lint_ok = error is None
        for finding in found:
            if Path(finding.file).is_absolute():  # thesis-ci reports relative paths; normalise an absolute one
                try:
                    rel = Path(finding.file).resolve().relative_to(ctx.pub.resolve()).as_posix()
                    finding = dataclasses.replace(finding, file=rel)
                except ValueError:
                    pass
            match = PREREG_FILE.match(finding.file)
            if match:
                findings.setdefault((match.group(1), match.group(2)), []).append(finding)
            elif finding.level == "error" and not finding.file:  # an error of thesis-ci itself
                c.record(FAIL, finding.describe())
    for co in companies:
        if co.problem:
            c.record(FAIL, f"{co.label}: {co.problem}")
            continue
        if co.status not in TRACKED:
            c.record(None, f"{co.label}: outside phase 1 acceptance (only holdings and candidates are checked)")
            continue
        events = scope[co.ticker]
        if not events:
            if co.status == "holding":
                c.record(*coverage(ctx, co, "every earnings event of a holding needs a pre-registration"))
            else:
                c.record(None, f"{co.label}: no pre-registration in the window (not required of a candidate)")
        for e in events:
            add_event(c, event_header(co, e, deadline=True), prereg_lines(ctx, e, findings.get((co.ticker, e.period), []), lint_ok))
        note_other_events(c, co, [e for e in co.events if e.in_window()])
    return c


def p2_updates(ctx: Context) -> Criterion:
    c = Criterion("P2", "Updates: for every earnings filing of a holding or candidate in the window, an update record "
                        "is merged as final into the default branch within 7 x 24 hours of EDGAR acceptance")
    companies = ctx.companies()
    tracked = [co.ticker for co in companies if co.status in TRACKED]
    c.detail = (f"{window_text(ctx)}; holdings and candidates {', '.join(tracked) or 'none'}; update records are "
                "companies/<TICKER>/updates/*.md in the public and private repos (level-1 updates are staged in the "
                "private repo first, 00 §G9)")
    for co in companies:
        if co.problem:
            c.record(FAIL, f"{co.label}: {co.problem}")
            continue
        if co.status not in TRACKED:
            c.record(None, f"{co.label}: outside phase 1 acceptance (only holdings and candidates are checked)")
            continue
        events = [e for e in co.events if e.in_window()]
        if not events:
            hint = ("every earnings event of a holding needs a pre-registration" if co.status == "holding" else
                    "a candidate has no pre-registration: record the filing's acceptance_datetime in "
                    "prereg/<period>.settlement.yml so that P2 can see it")
            c.record(*coverage(ctx, co, hint))
        for e in events:
            add_event(c, event_header(co, e, deadline=False), update_lines(ctx, e))
        note_other_events(c, co, events)
    return c


def final_letter(fm: dict[str, Any]) -> bool:
    return fm.get("doc") == "letter" and fm.get("doc_status") == "final"


def p3_first_letter(ctx: Context) -> Criterion:
    due = end_of_day(FIRST_LETTER_DUE)
    c = Criterion("P3", f"First monthly letter {FIRST_LETTER} (covering October): doc: letter, doc_status: final, "
                        f"on the default branch as final by {FIRST_LETTER_DUE} (US Eastern)")
    c.detail = (f"evaluated at {ny(ctx.now)}; due {ny(due)}; the time is that of the first commit on the default "
                "branch where the letter is final (an earlier draft commit does not count)")
    hist = ctx.history(ctx.pub)
    if hist.error:
        c.record(FAIL, hist.error)
        return c
    path = ctx.pub / FIRST_LETTER
    current = front_matter(path.read_text(encoding="utf-8")) if path.is_file() else None
    first = hist.first_where(FIRST_LETTER, final_letter)
    if first is not None and first.when <= due:
        if current is not None and final_letter(current):
            c.record(PASS, f"{FIRST_LETTER} went into {hist.ref} as final at {ny(first.when)} ({first.short})")
        else:
            now_state = "no longer exists" if not path.is_file() else "is not final (doc: letter, doc_status: final)"
            c.record(FAIL, f"{FIRST_LETTER} went into {hist.ref} as final at {ny(first.when)} ({first.short}), "
                           f"but the current file {now_state}")
        return c
    if first is not None:
        c.record(FAIL, f"{FIRST_LETTER} went into {hist.ref} as final only at {ny(first.when)} ({first.short}), "
                       "after the due date")
        return c
    if not path.is_file():
        why = f"{FIRST_LETTER} does not exist yet"
    elif current is None or not final_letter(current):
        why = f"{FIRST_LETTER} is not final yet (front matter needs doc: letter, doc_status: final)"
    else:
        why = f"the final {FIRST_LETTER} is not on {hist.ref} yet"
    c.record(FAIL if ctx.now > due else PENDING, f"{why} (due {ny(due)})")
    return c


PHASE0_RECHECK = (a2_design, a3_required_files, a4_lint, a5_thesis_tests, a6_constitution_checks, a7_tests)


def p4_phase0(ctx: Context) -> Criterion:
    c = Criterion("P4", "Phase 0 criteria still hold (A2-A7)")
    for check in PHASE0_RECHECK:
        sub = check(ctx)
        c.record(sub.status, f"{sub.id} {sub.title}")
        if sub.detail:
            c.record(None, sub.detail, indent=1)
        for item in sub.items:
            c.record(None, item, indent=1)
    return c


PHASES = {
    0: (a1_repositories, a2_design, a3_required_files, a4_lint, a5_thesis_tests, a6_constitution_checks, a7_tests),
    1: (p1_preregistrations, p2_updates, p3_first_letter, p4_phase0),
}


# ---------------------------------------------------------------- output


def overall(criteria: list[Criterion]) -> str:
    if any(c.status == FAIL for c in criteria):
        return FAIL
    if any(c.status == PENDING for c in criteria):
        return PENDING
    if any(c.status == SKIP for c in criteria):
        return "PASS_PROVISIONAL"
    return PASS


RESULT_LABEL = {
    PASS: "PASS",
    FAIL: "FAIL",
    PENDING: "PENDING (nothing has failed, but some items are not due yet; the phase is not complete)",
    "PASS_PROVISIONAL": "PASS (provisional: some checks were skipped)",
}
EXIT_CODES = {FAIL: 1, PENDING: 3}


def as_text(phase: int, stamp: str, ws: Path, criteria: list[Criterion]) -> str:
    lines = [f"Phase {phase} acceptance · {stamp}", f"Workspace: {ws}", ""]
    for c in criteria:
        lines.append(f"[{c.status}] {c.id} {c.title}")
        if c.detail:
            lines.append(f"       {c.detail}")
        lines.extend(f"       {item}" for item in c.items)
    lines += ["", f"Result: {RESULT_LABEL[overall(criteria)]}"]
    return "\n".join(lines)


def as_markdown(phase: int, stamp: str, ws: Path, criteria: list[Criterion]) -> str:
    """The report committed to the public repository: no local absolute paths; details in code blocks."""
    prefix = str(ws.resolve()) + os.sep

    def clean(text: str) -> str:
        return text.replace(prefix, "").replace(str(ws), "<workspace>")

    out = [
        f"# Phase {phase} acceptance report",
        "",
        f"- Generated: {stamp}",
        "- Generated by: `scripts/accept.py` (thesis-ci, owners-office and owners-office-private are sibling "
        "directories of the workspace)",
        f"- Result: **{RESULT_LABEL[overall(criteria)]}**",
        "",
        "| ID | Criterion | Result |",
        "| --- | --- | --- |",
    ]
    out += [f"| {c.id} | {c.title} | {c.status} |" for c in criteria]
    for c in criteria:
        out += ["", f"## {c.id} {c.title}", "", f"Result: {c.status}"]
        body = ([c.detail] if c.detail else []) + c.items
        if body:
            out += ["", "```text", *(clean(line) for line in body), "```"]
    return "\n".join(out) + "\n"


def parse_as_of(text: str) -> dt.datetime:
    """--as-of: an ISO 8601 date-time (without a UTC offset it is US Eastern); a bare date means 23:59:59 US Eastern."""
    value = text.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return end_of_day(dt.date.fromisoformat(value))
    try:
        moment = dt.datetime.fromisoformat(value[:-1] + "+00:00" if value[-1:] in ("Z", "z") else value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an ISO 8601 date or date-time: {text!r}") from None
    return moment if moment.utcoffset() is not None else moment.replace(tzinfo=NY)


def main(argv: list[str] | None = None) -> int:
    default_ws = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="Owner's Office phase acceptance")
    parser.add_argument("--phase", type=int, required=True, help="phase number (0 or 1)")
    parser.add_argument("--workspace", type=Path, default=default_ws, help=f"workspace root (default {default_ws})")
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.add_argument("--write-report", type=Path,
                        help="write the Markdown report to this path (a relative path is relative to owners-office)")
    parser.add_argument("--allow-uncommitted", action="store_true", help="skip A1's git commit check (phase 0 only)")
    parser.add_argument("--as-of", type=parse_as_of, metavar="TIME",
                        help="evaluate phase 1 at this time (ISO 8601; a bare date means the end of that day, "
                             "US Eastern; default: now)")
    args = parser.parse_args(argv)

    if args.phase not in PHASES:
        print(f"no acceptance criteria are defined for phase {args.phase}", file=sys.stderr)
        return 2
    if args.phase >= 1 and NY is None:
        print("phase 1 needs the time zone database (America/New_York): pip install tzdata", file=sys.stderr)
        return 2
    ws = args.workspace.expanduser().resolve()
    if not ws.is_dir():
        print(f"workspace does not exist: {ws}", file=sys.stderr)
        return 2

    ctx = Context(ws, args.allow_uncommitted, now=args.as_of)
    criteria = [check(ctx) for check in PHASES[args.phase]]
    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    result = overall(criteria)

    if args.json:
        payload = {
            "phase": args.phase,
            "generated_at": stamp,
            "evaluated_at": ctx.now.isoformat(timespec="seconds"),
            "workspace": str(ws),
            "result": result,
            "criteria": [dataclasses.asdict(c) for c in criteria],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(as_text(args.phase, stamp, ws, criteria))

    if args.write_report:
        report = args.write_report if args.write_report.is_absolute() else ctx.pub / args.write_report
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(as_markdown(args.phase, stamp, ws, criteria), encoding="utf-8")
        if not args.json:
            print(f"report written to {report}")

    return EXIT_CODES.get(result, 0)


if __name__ == "__main__":
    sys.exit(main())
