"""Tests for scripts/accept.py: tolerant parsing of thesis-ci output, and the criteria that need no external command.

The phase 1 tests (P1-P4) build git repositories in temporary directories with commit times set by the test.
thesis-ci is replaced by a stand-in; a single test runs the real one and is skipped when it is not installed (the
public CI's pipeline-tests job installs only requirements.txt). P4's A2-A7 are always stand-ins: running A7 inside a
test would run the whole test suite again.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

SPEC = importlib.util.spec_from_file_location(
    "accept", Path(__file__).resolve().parents[1] / "scripts" / "accept.py"
)
accept = importlib.util.module_from_spec(SPEC)
sys.modules["accept"] = accept  # dataclasses look the module up in sys.modules
SPEC.loader.exec_module(accept)


# ---------------------------------------------------------------- parsing lint output


@pytest.mark.parametrize(
    "data, errors, warnings",
    [
        ([{"check": "C-SCHEMA", "level": "error", "path": "a.yml", "message": "bad"}], 1, 0),
        ({"findings": [{"check": "C-SRC-ACCESSION", "severity": "warning", "message": "no accession"}]}, 0, 1),
        ({"errors": ["an error"], "warnings": [{"message": "a warning"}]}, 1, 1),
        (
            {"results": [{"id": "C-SCHEMA", "level": "error", "status": "pass", "findings": []}]},
            0,
            0,
        ),
        (
            {"results": [{"id": "C-STORY", "level": "error", "findings": [{"path": "x/story.md", "message": "too long"}]}]},
            1,
            0,
        ),
        ({"summary": {"errors": 2, "warnings": 0}}, 2, 0),
        ({"errors": 0, "warnings": 3, "findings": []}, 0, 3),
    ],
)
def test_parse_lint_shapes(data, errors, warnings):
    outcome = accept.parse_lint(data)
    assert (outcome.n_errors, outcome.n_warnings) == (errors, warnings)


def test_load_json_output_tolerates_log_lines():
    text = "checking...\n{\"findings\": []}\ndone"
    assert accept.load_json_output(text) == {"findings": []}
    assert accept.load_json_output("not JSON") is None


def test_parse_checks_shapes():
    assert set(accept.parse_checks([{"id": "C-SCHEMA"}, "C-STORY"])) == {"C-SCHEMA", "C-STORY"}
    assert set(accept.parse_checks({"checks": [{"id": "C-SCHEMA", "implemented": True}]})) == {"C-SCHEMA"}
    assert set(accept.parse_checks({"C-SCHEMA": {"implemented": True}})) == {"C-SCHEMA"}
    assert accept.parse_checks("C-SCHEMA") is None


def test_flag_reads_booleans_counts_and_words():
    assert accept._flag({"implemented": True}, ("implemented",)) is True
    assert accept._flag({"tests": 0}, ("tests",)) is False
    assert accept._flag({"tests": ["test_c_schema"]}, ("tests",)) is True
    assert accept._flag({"selftest": "fail"}, ("selftest",)) is False
    assert accept._flag({}, ("selftest",)) is None


# ---------------------------------------------------------------- criteria that need no external command


def make_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    for name in accept.REPOS:
        (ws / name).mkdir(parents=True)
    (ws / "inputs").mkdir()
    (ws / "inputs" / "DESIGN.md").write_text("# 设计\n", encoding="utf-8")  # the owner's original is in Chinese
    return ws


def test_a2_detects_mismatch(tmp_path):
    """Before zh-CN/docs/DESIGN.md exists, docs/DESIGN.md is the original and is compared with inputs/."""
    ws = make_workspace(tmp_path)
    ctx = accept.Context(ws, allow_uncommitted=True)
    assert accept.a2_design(ctx).status == accept.FAIL
    (ws / "owners-office" / "docs").mkdir()
    (ws / "owners-office" / "docs" / "DESIGN.md").write_text("# 设计\n", encoding="utf-8")
    result = accept.a2_design(ctx)
    assert result.status == accept.PASS
    assert "zh-CN/docs/DESIGN.md does not exist yet" in result.detail
    (ws / "owners-office" / "docs" / "DESIGN.md").write_text("# changed\n", encoding="utf-8")
    assert accept.a2_design(ctx).status == accept.FAIL


def test_a2_compares_the_chinese_original_once_it_exists(tmp_path):
    ws = make_workspace(tmp_path)
    ctx = accept.Context(ws, allow_uncommitted=True)
    original = ws / "owners-office" / "zh-CN" / "docs" / "DESIGN.md"
    original.parent.mkdir(parents=True)
    original.write_text("# 设计\n", encoding="utf-8")
    missing_english = accept.a2_design(ctx)
    assert missing_english.status == accept.FAIL  # the English docs/DESIGN.md must exist
    assert "owners-office/docs/DESIGN.md does not exist" in missing_english.items
    (ws / "owners-office" / "docs").mkdir()
    (ws / "owners-office" / "docs" / "DESIGN.md").write_text("# Design\n", encoding="utf-8")
    result = accept.a2_design(ctx)
    assert result.status == accept.PASS  # the translation differs from inputs/ and is not compared
    assert "docs/DESIGN.md exists (English translation; not compared)" in result.items
    assert any(item.startswith("zh-CN/docs/DESIGN.md matches inputs/DESIGN.md") for item in result.items)
    original.write_text("# 改过\n", encoding="utf-8")
    changed = accept.a2_design(ctx)
    assert changed.status == accept.FAIL
    assert "!= zh-CN/docs/DESIGN.md" in changed.items[-1]


def test_a2_without_inputs_checks_existence_only(tmp_path):
    ws = tmp_path / "ws"
    (ws / "owners-office" / "zh-CN" / "docs").mkdir(parents=True)
    (ws / "owners-office" / "docs").mkdir(parents=True)
    (ws / "owners-office" / "zh-CN" / "docs" / "DESIGN.md").write_text("# 设计\n", encoding="utf-8")
    (ws / "owners-office" / "docs" / "DESIGN.md").write_text("# Design\n", encoding="utf-8")
    result = accept.a2_design(accept.Context(ws, allow_uncommitted=True))
    assert result.status == accept.PASS
    assert "the workspace has no inputs/" in result.detail


def test_a1_skips_git_only_with_flag(tmp_path):
    ws = make_workspace(tmp_path)
    assert accept.a1_repositories(accept.Context(ws, allow_uncommitted=True)).status == accept.SKIP
    assert accept.a1_repositories(accept.Context(ws, allow_uncommitted=False)).status == accept.FAIL


THESIS = """\
company: {t}
status: {status}
tests:
{tests}
"""


def write_company(ws: Path, ticker: str, types: list[str], status: str = "holding", story: bool = True):
    d = ws / "owners-office" / "companies" / ticker
    d.mkdir(parents=True)
    tests = "\n".join(f"  - id: {ticker}-Q{i}\n    type: {t}" for i, t in enumerate(types, 1))
    (d / "thesis.yml").write_text(THESIS.format(t=ticker, status=status, tests=tests), encoding="utf-8")
    if story:
        (d / "story.md").write_text("---\ncompany: X\n---\nA story\n", encoding="utf-8")


def test_a5_requires_five_tests_of_three_types_and_story(tmp_path):
    ws = make_workspace(tmp_path)
    ctx = accept.Context(ws, allow_uncommitted=True)
    assert accept.a5_thesis_tests(ctx).status == accept.FAIL  # no companies
    write_company(ws, "AXP", ["quantitative", "quantitative", "qualitative", "qualitative", "staleness"])
    result = accept.a5_thesis_tests(ctx)
    assert result.status == accept.PASS
    assert "AXP" in result.detail
    write_company(ws, "PDD", ["quantitative"] * 5)
    assert accept.a5_thesis_tests(ctx).status == accept.FAIL


def test_a5_flags_missing_story(tmp_path):
    ws = make_workspace(tmp_path)
    write_company(ws, "MSFT", ["quantitative", "qualitative", "staleness", "staleness", "staleness"], "candidate", story=False)
    result = accept.a5_thesis_tests(accept.Context(ws, allow_uncommitted=True))
    assert result.status == accept.FAIL
    assert any("story.md" in item for item in result.items)


def test_overall_result():
    ok = accept.Criterion("A1", "t")
    skipped = accept.Criterion("A2", "t", status=accept.SKIP)
    failed = accept.Criterion("A3", "t", status=accept.FAIL)
    assert accept.overall([ok]) == accept.PASS
    assert accept.overall([ok, skipped]) == "PASS_PROVISIONAL"
    assert accept.overall([ok, skipped, failed]) == accept.FAIL


def test_markdown_report_hides_workspace_path(tmp_path):
    ws = make_workspace(tmp_path)
    c = accept.Criterion("A2", "t", items=[f"{ws}/owners-office/docs/DESIGN.md is missing"])
    report = accept.as_markdown(0, "2026-09-24T00:00:00+00:00", ws, [c])
    assert str(ws) not in report
    assert "owners-office/docs/DESIGN.md is missing" in report
    assert report.startswith("# Phase 0 acceptance report\n")


# ---------------------------------------------------------------- phase 1: shared helpers

NY = ZoneInfo("America/New_York")
SEED = "2026-09-24T12:00:00-04:00"  # first commit of each repository
APP = "companies/APP/prereg/FY2026Q3"
DEADLINE = "2026-11-04T23:59:59-05:00"  # end of the day before the expected release 2026-11-05 (EST from Nov 1)
MERGED = "2026-10-30T10:00:00-04:00"  # when the items file is merged into main (about 135 hours before the deadline)
ACCEPTED = "2026-11-05T21:05:12Z"  # EDGAR acceptance, i.e. 16:05:12 US Eastern
ACCESSION = "0001751008-26-000071"
UPDATE = "companies/APP/updates/2026-11-06.md"
LETTER = "letters/2026-10.md"


def t(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text)


def git_env(**extra: str) -> dict[str, str]:
    """Ignore the user's global and system git configuration (signing, hooks and aliases cannot affect the tests)."""
    return dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", **extra)


class GitRepo:
    """A git repository for tests: default branch main, commit times given by the caller."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str, date: str | None = None) -> str:
        env = git_env(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date) if date else git_env()
        cmd = ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false", *args]
        return subprocess.run(cmd, cwd=self.root, env=env, check=True, capture_output=True, text=True).stdout.strip()

    def write(self, files: dict[str, str | None]) -> None:
        for rel, text in files.items():
            path = self.root / rel
            if text is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")

    def commit(self, date: str, files: dict[str, str | None]) -> str:
        self.write(files)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "test", date=date)
        return self.git("rev-parse", "HEAD")


def phase1_workspace(base: Path, statuses: dict[str, str] | None = None) -> tuple[Path, GitRepo, GitRepo]:
    """A workspace with public and private git repositories; each public company has only a thesis.yml (status)."""
    statuses = statuses or {"APP": "holding"}
    ws = base / "ws"
    pub, priv = GitRepo(ws / "owners-office"), GitRepo(ws / "owners-office-private")
    theses = {f"companies/{ticker}/thesis.yml": f"company: {ticker}\nstatus: {status}\n" for ticker, status in statuses.items()}
    pub.commit(SEED, {"repo.yml": "visibility: public\n", **theses})
    priv.commit(SEED, {"repo.yml": "visibility: private\n"})
    return ws, pub, priv


def items_yml(ticker="APP", period="FY2026Q3", release="2026-11-05", deadline=DEADLINE, author="system") -> str:
    return (
        f"company: {ticker}\nevent:\n  period: {period}\n  expected_release: {release}\n  form: 8-K\n"
        f"  placeholder: false\ndeadline: \"{deadline}\"\nauthor: {author}\nhorizon: quarter\nitems:\n"
        f"  - id: {ticker}-{period}-1\n    statement: Revenue growth this quarter is at least last quarter's\n"
        f"    probability: 0.6\n    criterion: Revenue in the 10-Q income statement\n"
        f"    data_source: 10-Q income statement\n    horizon: quarter\n"
        f"    resolves_by: 2026-12-31\n    domain: other\n    added_by: {author}\n"
    )


def settlement_yml(ticker="APP", period="FY2026Q3", **fields: str | None) -> str:
    lines = [f"company: {ticker}", f"period: {period}"]
    for key in ("merged_at", "acceptance_datetime", "accession", "ots_proof"):
        value = fields.get(key)
        lines.append(f"{key}: " + ("null" if value is None else f'"{value}"'))
    return "\n".join(lines + ["results: []"]) + "\n"


def complete_prereg(pub: GitRepo, acceptance: str | None = ACCEPTED) -> None:
    """APP FY2026Q3: merged 135 hours before the deadline with an .ots proof; the settlement file records merged_at,
    and after the release the acceptance time and accession."""
    pub.commit(MERGED, {f"{APP}.yml": items_yml(), f"{APP}.yml.ots": "proof\n"})
    pub.commit("2026-10-30T10:05:00-04:00", {f"{APP}.settlement.yml": settlement_yml(merged_at=MERGED)})
    if acceptance:
        settled = settlement_yml(merged_at=MERGED, acceptance_datetime=acceptance, accession=ACCESSION)
        pub.commit("2026-11-05T17:00:00-05:00", {f"{APP}.settlement.yml": settled})


def update_md(ticker="APP", status="final", period: str | None = None) -> str:
    extra = f"period: {period}\n" if period else ""
    return (f"---\ncompany: {ticker}\ndoc: update\nas_of: 2026-11-06\ndoc_status: {status}\n{extra}"
            f"reviewed_sections: [economics]\n---\n\n# {ticker}: quarterly update\n")


def letter_md(status="final") -> str:
    return f"---\ndoc: letter\nas_of: 2026-11-02\ndoc_status: {status}\n---\n\n# Letter to the owner: October\n"


def at(ws: Path, when: str) -> accept.Context:
    return accept.Context(ws, allow_uncommitted=False, now=t(when))


def text_of(c: accept.Criterion) -> str:
    return "\n".join(c.items)


class FakeLint:
    """Stand-in for `thesis-ci lint --only C-PREREG-TIMING C-PREREG-IMMUTABLE`."""

    def __init__(self):
        self.findings: list = []
        self.error: str | None = None
        self.calls: list[dt.date] = []

    def __call__(self, ctx):
        self.calls.append(ctx.today)
        return list(self.findings), self.error


@pytest.fixture
def lint(monkeypatch):
    fake = FakeLint()
    monkeypatch.setattr(accept, "prereg_lint", fake)
    return fake


# ---------------------------------------------------------------- time, history and thesis-ci output


def test_to_datetime_reads_iso_zulu_and_edgar_compact():
    assert accept.to_datetime("2026-11-05T21:05:12Z") == t("2026-11-05T16:05:12-05:00")
    assert accept.to_datetime("20261105160512") == t("2026-11-05T16:05:12-05:00")  # EDGAR's compact form is US Eastern
    assert accept.to_datetime(t("2026-11-05T16:05:12-05:00")) == t("2026-11-05T21:05:12+00:00")
    assert accept.to_datetime("2026-11-05T16:05:12") is None  # no time zone: cannot be compared
    assert accept.to_datetime("next week") is None
    assert accept.to_datetime(None) is None


def test_parse_as_of():
    assert accept.parse_as_of("2026-12-08") == dt.datetime(2026, 12, 8, 23, 59, 59, tzinfo=NY)
    assert accept.parse_as_of("2026-12-08T09:00:00Z") == t("2026-12-08T09:00:00+00:00")
    assert accept.parse_as_of("2026-12-08T09:00:00").utcoffset() == dt.timedelta(hours=-5)
    with pytest.raises(argparse.ArgumentTypeError):
        accept.parse_as_of("next week")


def test_record_keeps_the_worst_status():
    c = accept.Criterion("P1", "t")
    c.record(accept.PASS, "a")
    c.record(accept.PENDING, "b", indent=1)
    c.record(None, "a note")
    c.record(accept.PASS, "c")
    assert c.status == accept.PENDING
    assert c.items == ["[PASS] a", "  [PENDING] b", "a note", "[PASS] c"]
    c.record(accept.FAIL, "d")
    c.record(accept.PENDING, "e")
    assert c.status == accept.FAIL


def test_overall_pending_is_not_a_pass():
    ok = accept.Criterion("P1", "t")
    pending = accept.Criterion("P2", "t", status=accept.PENDING)
    skipped = accept.Criterion("P3", "t", status=accept.SKIP)
    failed = accept.Criterion("P4", "t", status=accept.FAIL)
    assert accept.overall([ok, pending]) == accept.PENDING
    assert accept.overall([ok, skipped, pending]) == accept.PENDING
    assert accept.overall([pending, failed]) == accept.FAIL


def test_lint_findings_keep_file_paths():
    data = {
        "repo": "/x/owners-office",
        "errors": [{"check": "C-PREREG-TIMING", "file": f"{APP}.settlement.yml", "line": 3, "message": "m"}],
        "warnings": [{"check": "C-PREREG-IMMUTABLE", "file": f"{APP}.yml.ots", "line": None, "message": "w"}],
        "checks_run": ["C-PREREG-TIMING", "C-PREREG-IMMUTABLE"],
    }
    found = accept.lint_findings(data)
    assert [(f.level, f.check, f.file, f.line) for f in found] == [
        ("error", "C-PREREG-TIMING", f"{APP}.settlement.yml", 3),
        ("warning", "C-PREREG-IMMUTABLE", f"{APP}.yml.ots", None),
    ]
    assert found[0].describe() == f"thesis-ci C-PREREG-TIMING {APP}.settlement.yml:3 m"
    assert accept.lint_findings([{"level": "error", "check": "C", "path": "a.yml", "message": "m"}])[0].file == "a.yml"
    assert accept.lint_findings("not JSON") is None


def test_prereg_lint_without_thesis_ci(tmp_path):
    ctx = accept.Context(tmp_path, allow_uncommitted=False)
    ctx.tci = None
    assert accept.prereg_lint(ctx) == (
        [], "thesis-ci executable not found (virtual environment or PATH); cannot run C-PREREG-TIMING, C-PREREG-IMMUTABLE"
    )


def test_default_branch_prefers_the_remote(tmp_path):
    origin = GitRepo(tmp_path / "origin")
    origin.commit(SEED, {"a.txt": "1\n"})
    assert accept.default_branch(origin.root) == "main"
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", origin.root.as_uri(), str(clone)], check=True, capture_output=True, env=git_env())
    assert accept.default_branch(clone) == "origin/main"


def test_history_refuses_shallow_clones_and_non_repositories(tmp_path):
    origin = GitRepo(tmp_path / "origin")
    origin.commit(SEED, {"a.txt": "1\n"})
    origin.commit("2026-09-25T12:00:00-04:00", {"a.txt": "2\n"})
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", "--depth", "1", origin.root.as_uri(), str(clone)], check=True,
                   capture_output=True, env=git_env())
    assert "shallow clone" in accept.History(clone, t("2026-12-01T00:00:00-05:00")).error
    plain = tmp_path / "plain"
    plain.mkdir()
    assert "not a git repository of its own" in accept.History(plain, t("2026-12-01T00:00:00-05:00")).error


def test_history_uses_first_parent_merge_time_and_ignores_later_commits(tmp_path):
    repo = GitRepo(tmp_path / "r")
    repo.commit(SEED, {"README.md": "x\n"})
    repo.git("checkout", "-q", "-b", "topic")
    repo.commit("2026-10-20T10:00:00-04:00", {"a.txt": "1\n"})
    repo.git("checkout", "-q", "main")
    repo.commit("2026-10-21T10:00:00-04:00", {"README.md": "y\n"})
    repo.git("merge", "-q", "--no-ff", "topic", "-m", "merge", date="2026-10-22T10:00:00-04:00")
    hist = accept.History(repo.root, t("2026-12-01T00:00:00-05:00"))
    assert [c.when for c in hist.commits("a.txt")] == [t("2026-10-22T10:00:00-04:00")]  # the merge, not the topic commit
    assert accept.History(repo.root, t("2026-10-21T12:00:00-04:00")).commits("a.txt") == []


# ---------------------------------------------------------------- P1 pre-registrations


def test_p1_holding_without_events_is_pending_until_the_window_closes(tmp_path, lint):
    ws, _, _ = phase1_workspace(tmp_path, {"APP": "holding", "AXP": "candidate", "BRK": "archive"})
    before = accept.p1_preregistrations(at(ws, "2026-09-25T12:00:00-04:00"))
    assert before.status == accept.PENDING
    assert "[PENDING] APP (holding): no earnings event recorded in the window yet" in text_of(before)
    assert "AXP (candidate): no pre-registration in the window (not required of a candidate)" in text_of(before)
    assert "BRK (archive): outside phase 1 acceptance" in text_of(before)
    after = accept.p1_preregistrations(at(ws, "2026-12-01T00:00:01-05:00"))
    assert after.status == accept.FAIL
    assert "[FAIL] APP (holding): the window has closed" in text_of(after)
    assert lint.calls == []  # no pre-registration in the window, so thesis-ci is not needed


def test_p1_release_after_the_window_is_not_required(tmp_path, lint):
    ws, pub, _ = phase1_workspace(tmp_path)
    q2 = "companies/APP/prereg/FY2026Q2"
    q4 = "companies/APP/prereg/FY2026Q4"
    pub.commit("2026-11-20T10:00:00-05:00", {
        f"{q2}.yml": items_yml(period="FY2026Q2", release="2026-08-05", deadline="2026-08-04T23:59:59-04:00"),
        f"{q4}.yml": items_yml(period="FY2026Q4", release="2026-12-03", deadline="2026-12-02T23:59:59-05:00"),
    })
    c = accept.p1_preregistrations(at(ws, "2026-12-01T12:00:00-05:00"))
    assert c.status == accept.PASS, text_of(c)
    assert "the next recorded release, 2026-12-03, is after it" in text_of(c)
    assert "released outside the window, not part of this phase: FY2026Q2 (2026-08-05), FY2026Q4 (2026-12-03)" in text_of(c)


def test_p1_complete_prereg_passes(tmp_path, lint):
    ws, pub, _ = phase1_workspace(tmp_path)
    complete_prereg(pub)
    c = accept.p1_preregistrations(at(ws, "2026-11-10T12:00:00-05:00"))
    text = text_of(c)
    assert c.status == accept.PASS, text
    assert ("[PASS] APP FY2026Q3 (holding) · EDGAR accepted 2026-11-05T16:05:12-05:00 (settlement file) · "
            "deadline 2026-11-04T23:59:59-05:00") in text
    assert "[PASS] FY2026Q3.yml merged into main at 2026-10-30T10:00:00-04:00" in text
    assert "[PASS] timestamp proof FY2026Q3.yml.ots" in text
    assert "[PASS] settlement merged_at 2026-10-30T10:00:00-04:00 matches git" in text
    assert f"accession {ACCESSION}" in text
    assert "[PASS] thesis-ci C-PREREG-TIMING, C-PREREG-IMMUTABLE: no errors in this period's files" in text
    assert lint.calls == [dt.date(2026, 11, 10)]  # thesis-ci's --today is the evaluation time's US Eastern date


def test_p1_before_the_deadline_missing_pieces_are_pending(tmp_path, lint):
    ws, pub, _ = phase1_workspace(tmp_path)
    pub.commit("2026-11-03T10:00:00-05:00", {f"{APP}.yml": items_yml()})
    c = accept.p1_preregistrations(at(ws, "2026-11-03T12:00:00-05:00"))
    text = text_of(c)
    assert c.status == accept.PENDING, text
    assert "[FAIL]" not in text
    assert "[PENDING] no timestamp proof FY2026Q3.yml.ots yet (stamp it before the deadline)" in text
    assert "[PENDING] no settlement file FY2026Q3.settlement.yml yet" in text
    assert "[PENDING] results not released yet (expected 2026-11-05)" in text
    assert "less than the 72 hours 15A asks for (a note only)" in text  # merged 38 hours before: a note, not a failure
    unmerged = accept.p1_preregistrations(at(ws, "2026-11-03T09:00:00-05:00"))  # before the commit
    assert "[PENDING] FY2026Q3.yml not merged into main yet" in text_of(unmerged)


def test_p1_after_the_deadline_missing_proof_and_merged_at_fail(tmp_path, lint):
    ws, pub, _ = phase1_workspace(tmp_path)
    pub.commit(MERGED, {f"{APP}.yml": items_yml()})
    c = accept.p1_preregistrations(at(ws, "2026-11-05T09:00:00-05:00"))
    text = text_of(c)
    assert c.status == accept.FAIL
    assert "[FAIL] timestamp proof FY2026Q3.yml.ots is missing (the deadline has passed)" in text
    assert ("[FAIL] no settlement file FY2026Q3.settlement.yml yet (git: first added to main at "
            "2026-10-30T10:00:00-04:00") in text
    assert "[PENDING] results not released yet (expected 2026-11-05)" in text  # not late on the release day itself
    # when thesis-ci has already reported the missing proof (C-PREREG-IMMUTABLE on the items file), no second line
    lint.findings = [accept.LintFinding("error", "C-PREREG-IMMUTABLE", f"{APP}.yml", 7, "timestamp proof is missing")]
    again = text_of(accept.p1_preregistrations(at(ws, "2026-11-05T09:00:00-05:00")))
    assert "is missing (the deadline has passed)" not in again
    assert f"[FAIL] thesis-ci C-PREREG-IMMUTABLE {APP}.yml:7 timestamp proof is missing" in again


def test_p1_merged_at_must_match_git(tmp_path, lint):
    ws, pub, _ = phase1_workspace(tmp_path)
    pub.commit(MERGED, {f"{APP}.yml": items_yml(), f"{APP}.yml.ots": "proof\n"})
    pub.commit("2026-10-30T11:00:00-04:00", {f"{APP}.settlement.yml": settlement_yml(merged_at="2026-10-29T10:00:00-04:00")})
    c = accept.p1_preregistrations(at(ws, "2026-11-01T12:00:00-05:00"))
    assert c.status == accept.FAIL
    assert ("[FAIL] merged_at 2026-10-29T10:00:00-04:00 does not match git: the commit that first added "
            "FY2026Q3.yml to main") in text_of(c)
    pub.commit("2026-10-30T11:30:00-04:00", {f"{APP}.settlement.yml": settlement_yml(merged_at="2026-10-30T14:00:30Z")})
    close = accept.p1_preregistrations(at(ws, "2026-11-01T12:00:00-05:00"))  # 30 seconds off, written in UTC
    assert "[PASS] settlement merged_at 2026-10-30T10:00:30-04:00 matches git" in text_of(close)
    pub.commit("2026-10-30T12:00:00-04:00", {f"{APP}.settlement.yml": settlement_yml(merged_at="2026-10-30")})
    bad = accept.p1_preregistrations(at(ws, "2026-11-01T12:00:00-05:00"))
    assert "[FAIL] merged_at '2026-10-30' has no time zone" in text_of(bad)


def test_p1_merged_at_without_the_file_on_the_default_branch_fails(tmp_path, lint):
    ws, pub, _ = phase1_workspace(tmp_path)
    pub.write({f"{APP}.yml": items_yml(), f"{APP}.yml.ots": "proof\n", f"{APP}.settlement.yml": settlement_yml(merged_at=MERGED)})
    c = accept.p1_preregistrations(at(ws, "2026-11-01T12:00:00-05:00"))
    assert ("[FAIL] the settlement file records merged_at 2026-10-30T10:00:00-04:00, but FY2026Q3.yml is not on main"
            in text_of(c))


def test_p1_merge_time_is_the_merge_into_the_default_branch(tmp_path, lint):
    """A pre-registration written early on a branch but merged after the deadline is late."""
    ws, pub, _ = phase1_workspace(tmp_path)
    pub.git("checkout", "-q", "-b", "prereg")
    pub.commit("2026-10-20T10:00:00-04:00", {f"{APP}.yml": items_yml(), f"{APP}.yml.ots": "proof\n"})
    pub.git("checkout", "-q", "main")
    pub.commit("2026-10-21T10:00:00-04:00", {"README.md": "x\n"})
    pub.git("merge", "-q", "--no-ff", "prereg", "-m", "merge", date="2026-11-05T08:00:00-05:00")
    c = accept.p1_preregistrations(at(ws, "2026-11-05T12:00:00-05:00"))
    assert c.status == accept.FAIL
    assert "[FAIL] FY2026Q3.yml merged into main only at 2026-11-05T08:00:00-05:00" in text_of(c)


def test_p1_prereg_changed_after_the_deadline_fails(tmp_path, lint):
    ws, pub, _ = phase1_workspace(tmp_path)
    complete_prereg(pub)
    pub.commit("2026-11-06T09:00:00-05:00", {f"{APP}.yml": items_yml().replace("probability: 0.6", "probability: 0.7")})
    c = accept.p1_preregistrations(at(ws, "2026-11-10T12:00:00-05:00"))
    assert c.status == accept.FAIL
    assert "[FAIL] FY2026Q3.yml changed on main after the deadline" in text_of(c)


def test_p1_owner_file_must_be_merged_and_timestamped_before_the_deadline(tmp_path, lint):
    ws, pub, _ = phase1_workspace(tmp_path)
    complete_prereg(pub)
    pub.commit("2026-11-05T08:00:00-05:00", {f"{APP}-owner.yml": items_yml(author="owner")})
    c = accept.p1_preregistrations(at(ws, "2026-11-10T12:00:00-05:00"))
    text = text_of(c)
    assert c.status == accept.FAIL
    assert "[FAIL] FY2026Q3-owner.yml merged into main only at 2026-11-05T08:00:00-05:00" in text
    assert "[FAIL] timestamp proof FY2026Q3-owner.yml.ots is missing (the deadline has passed)" in text


def test_p1_acceptance_time_unknown_fails_after_the_release_day(tmp_path, lint):
    ws, pub, _ = phase1_workspace(tmp_path)
    complete_prereg(pub, acceptance=None)
    on_the_day = accept.p1_preregistrations(at(ws, "2026-11-05T20:00:00-05:00"))
    assert on_the_day.status == accept.PENDING, text_of(on_the_day)
    later = accept.p1_preregistrations(at(ws, "2026-11-06T09:00:00-05:00"))
    assert later.status == accept.FAIL
    assert ("[FAIL] EDGAR acceptance time unknown: settlement file FY2026Q3.settlement.yml has no acceptance_datetime"
            in text_of(later))


def test_p1_edgar_hook_fills_in_the_acceptance_time(tmp_path, lint, monkeypatch):
    ws, pub, _ = phase1_workspace(tmp_path)
    complete_prereg(pub, acceptance=None)
    calls = []

    def hook(ticker, period, filer):
        calls.append((ticker, period))
        return t("2026-11-05T16:05:12-05:00"), ACCESSION

    monkeypatch.setattr(accept, "edgar_acceptance", hook)
    accept.p1_preregistrations(at(ws, "2026-11-04T12:00:00-05:00"))
    assert calls == []  # not asked before the expected release day
    c = accept.p1_preregistrations(at(ws, "2026-11-06T09:00:00-05:00"))
    assert c.status == accept.PASS, text_of(c)
    assert (f"[PASS] the deadline precedes the EDGAR acceptance time 2026-11-05T16:05:12-05:00 (EDGAR, accession "
            f"{ACCESSION})") in text_of(c)
    assert calls == [("APP", "FY2026Q3")]
    monkeypatch.setattr(accept, "edgar_acceptance", lambda *args: (t("2026-11-04T20:00:00-05:00"), None))
    early = accept.p1_preregistrations(at(ws, "2026-11-06T09:00:00-05:00"))
    assert early.status == accept.FAIL
    assert "does not precede the EDGAR acceptance time 2026-11-04T20:00:00-05:00" in text_of(early)


def test_p1_reports_thesis_ci_findings_of_the_event(tmp_path, lint):
    ws, pub, _ = phase1_workspace(tmp_path)
    complete_prereg(pub)
    lint.findings = [
        accept.LintFinding("error", "C-PREREG-TIMING", f"{APP}.settlement.yml", 3, "deadline must be earlier than acceptance_datetime"),
        accept.LintFinding("warning", "C-PREREG-IMMUTABLE", f"{APP}.yml.ots", None, "cannot verify FY2026Q3.yml.ots"),
        accept.LintFinding("error", "C-PREREG-TIMING", "companies/APP/prereg/FY2026Q2.settlement.yml", 3, "outside the window"),
    ]
    c = accept.p1_preregistrations(at(ws, "2026-11-10T12:00:00-05:00"))
    text = text_of(c)
    assert c.status == accept.FAIL
    assert f"  [FAIL] thesis-ci C-PREREG-TIMING {APP}.settlement.yml:3 deadline must be earlier" in text
    assert f"  thesis-ci C-PREREG-IMMUTABLE {APP}.yml.ots cannot verify" in text  # a warning is only a note
    assert "outside the window" not in text  # not a file of this event
    assert "no errors in this period's files" not in text
    lint.findings, lint.error = [], "thesis-ci executable not found"
    missing = accept.p1_preregistrations(at(ws, "2026-11-10T12:00:00-05:00"))
    assert missing.status == accept.FAIL
    assert missing.items[0] == "[FAIL] thesis-ci executable not found"


def test_p1_holding_event_without_a_prereg_fails_after_its_deadline(tmp_path, lint):
    ws, pub, _ = phase1_workspace(tmp_path)
    pub.commit("2026-11-06T09:00:00-05:00", {f"{APP}.settlement.yml": settlement_yml(acceptance_datetime=ACCEPTED)})
    c = accept.p1_preregistrations(at(ws, "2026-11-10T12:00:00-05:00"))
    assert c.status == accept.FAIL
    assert "[FAIL] no system pre-registration items file FY2026Q3.yml (deadline 2026-11-04T23:59:59-05:00)" in text_of(c)
    assert lint.calls == []


def test_p1_checks_candidate_preregs_but_not_archive(tmp_path, lint):
    ws, pub, _ = phase1_workspace(tmp_path, {"APP": "holding", "AXP": "candidate", "BRK": "archive"})
    pub.commit("2026-10-10T10:00:00-04:00", {
        "companies/AXP/prereg/FY2026Q3.yml": items_yml("AXP", release="2026-10-16", deadline="2026-10-15T23:59:59-04:00"),
        "companies/BRK/prereg/FY2026Q3.yml": items_yml("BRK", release="2026-11-07", deadline="2026-11-06T23:59:59-05:00"),
    })
    c = accept.p1_preregistrations(at(ws, "2026-10-20T12:00:00-04:00"))
    text = text_of(c)
    assert "[FAIL] AXP FY2026Q3 (candidate) · expected release 2026-10-16" in text
    assert "[FAIL] timestamp proof FY2026Q3.yml.ots is missing (the deadline has passed)" in text
    assert "BRK FY2026Q3" not in text
    assert "BRK (archive): outside phase 1 acceptance" in text


def test_p1_needs_the_git_history(tmp_path, lint):
    ws = tmp_path / "ws"
    pub = ws / "owners-office"
    (pub / APP).parent.mkdir(parents=True)
    (pub / "companies/APP/thesis.yml").write_text("company: APP\nstatus: holding\n", encoding="utf-8")
    (pub / f"{APP}.yml").write_text(items_yml(), encoding="utf-8")
    c = accept.p1_preregistrations(at(ws, "2026-11-01T12:00:00-05:00"))
    assert c.status == accept.FAIL
    assert "owners-office is not a git repository of its own" in c.items[0]


@pytest.mark.skipif(accept.find_thesis_ci() is None,
                    reason="thesis-ci is not installed (the public CI's pipeline-tests job installs only requirements.txt)")
def test_p1_with_the_real_thesis_ci(tmp_path):
    """Run the real thesis-ci: the problems it reports are attributed to the earnings event by file."""
    ws, pub, _ = phase1_workspace(tmp_path)
    late = "2026-11-05T08:00:00-05:00"
    pub.commit(late, {f"{APP}.yml": items_yml(), f"{APP}.settlement.yml": settlement_yml(merged_at=late)})
    c = accept.p1_preregistrations(at(ws, "2026-11-05T12:00:00-05:00"))
    text = text_of(c)
    assert c.status == accept.FAIL
    assert f"[FAIL] thesis-ci C-PREREG-TIMING {APP}.settlement.yml" in text  # merged_at after the deadline
    assert f"[FAIL] thesis-ci C-PREREG-IMMUTABLE {APP}.yml" in text  # deadline passed, no .ots
    assert "is missing (the deadline has passed)" not in text


# ---------------------------------------------------------------- P2 updates


def test_p2_update_merged_within_seven_days_passes(tmp_path):
    ws, pub, _ = phase1_workspace(tmp_path)
    complete_prereg(pub)
    pub.commit("2026-11-06T10:00:00-05:00", {UPDATE: update_md()})
    c = accept.p2_updates(at(ws, "2026-11-10T12:00:00-05:00"))
    text = text_of(c)
    assert c.status == accept.PASS, text
    assert ("[PASS] APP FY2026Q3 (holding) · EDGAR accepted 2026-11-05T16:05:12-05:00 (settlement file) · "
            "update due 2026-11-12T16:05:12-05:00") in text
    assert f"[PASS] public repo {UPDATE} merged into main as final at 2026-11-06T10:00:00-05:00" in text
    assert "17.9 hours after EDGAR acceptance" in text


def test_p2_update_staged_in_the_private_repo_counts(tmp_path):
    ws, pub, priv = phase1_workspace(tmp_path)
    complete_prereg(pub)
    priv.commit("2026-11-07T10:00:00-05:00", {UPDATE: update_md()})
    c = accept.p2_updates(at(ws, "2026-11-10T12:00:00-05:00"))
    assert c.status == accept.PASS, text_of(c)
    assert f"[PASS] private repo {UPDATE} merged into main as final at 2026-11-07T10:00:00-05:00" in text_of(c)
    assert "staged in the private repo" in text_of(c)
    # moved to the public repo after HQ review (more than 7 days later): the private merge time still counts
    priv.commit("2026-11-16T10:00:00-05:00", {UPDATE: None})
    pub.commit("2026-11-16T10:00:00-05:00", {UPDATE: update_md()})
    moved = accept.p2_updates(at(ws, "2026-11-20T12:00:00-05:00"))
    assert moved.status == accept.PASS, text_of(moved)
    assert f"[PASS] private repo {UPDATE} merged into main as final at 2026-11-07T10:00:00-05:00" in text_of(moved)


def test_p2_pending_before_the_due_time_and_fail_when_late(tmp_path):
    ws, pub, _ = phase1_workspace(tmp_path)
    complete_prereg(pub)
    waiting = accept.p2_updates(at(ws, "2026-11-12T12:00:00-05:00"))
    assert waiting.status == accept.PENDING
    assert "[PENDING] no update record merged as final yet (due 2026-11-12T16:05:12-05:00)" in text_of(waiting)
    pub.commit("2026-11-13T09:00:00-05:00", {"companies/APP/updates/2026-11-13.md": update_md()})
    late = accept.p2_updates(at(ws, "2026-11-14T12:00:00-05:00"))
    text = text_of(late)
    assert late.status == accept.FAIL
    assert ("[FAIL] no update record merged as final into the default branch within 7 x 24 hours of EDGAR acceptance "
            "(due 2026-11-12T16:05:12-05:00)") in text
    assert "public repo companies/APP/updates/2026-11-13.md merged as final only at 2026-11-13T09:00:00-05:00" in text


def test_p2_a_draft_counts_only_from_the_commit_that_makes_it_final(tmp_path):
    ws, pub, _ = phase1_workspace(tmp_path)
    complete_prereg(pub)
    pub.commit("2026-11-06T10:00:00-05:00", {UPDATE: update_md(status="draft")})
    draft = accept.p2_updates(at(ws, "2026-11-08T12:00:00-05:00"))
    assert draft.status == accept.PENDING
    assert f"public repo {UPDATE} is not on main as final yet" in text_of(draft)
    pub.commit("2026-11-09T10:00:00-05:00", {UPDATE: update_md()})
    final = accept.p2_updates(at(ws, "2026-11-10T12:00:00-05:00"))
    assert final.status == accept.PASS
    assert f"public repo {UPDATE} merged into main as final at 2026-11-09T10:00:00-05:00" in text_of(final)


def test_p2_update_declared_for_another_period_does_not_count(tmp_path):
    ws, pub, _ = phase1_workspace(tmp_path)
    complete_prereg(pub)
    pub.commit("2026-11-06T10:00:00-05:00", {UPDATE: update_md(period="FY2026Q2")})
    assert accept.p2_updates(at(ws, "2026-11-13T12:00:00-05:00")).status == accept.FAIL


def test_p2_acceptance_time_unknown(tmp_path):
    ws, pub, _ = phase1_workspace(tmp_path)
    complete_prereg(pub, acceptance=None)
    before = accept.p2_updates(at(ws, "2026-11-04T12:00:00-05:00"))
    assert before.status == accept.PENDING
    assert "[PENDING] results not released yet (expected 2026-11-05)" in text_of(before)
    after = accept.p2_updates(at(ws, "2026-11-07T12:00:00-05:00"))
    assert after.status == accept.FAIL
    assert "[FAIL] EDGAR acceptance time unknown" in text_of(after)


def test_p2_candidates_need_a_recorded_filing(tmp_path):
    ws, pub, _ = phase1_workspace(tmp_path, {"AXP": "candidate", "BRK": "archive"})
    during = accept.p2_updates(at(ws, "2026-10-20T12:00:00-04:00"))
    assert during.status == accept.PENDING
    assert ("[PENDING] AXP (candidate): no earnings event recorded in the window yet (a candidate has no "
            "pre-registration") in text_of(during)
    assert "BRK (archive): outside phase 1 acceptance" in text_of(during)
    assert accept.p2_updates(at(ws, "2026-12-08T12:00:00-05:00")).status == accept.FAIL
    pub.commit("2026-10-16T09:00:00-04:00", {
        "companies/AXP/prereg/FY2026Q3.settlement.yml": settlement_yml("AXP", acceptance_datetime="2026-10-16T11:00:28Z"),
    })
    pub.commit("2026-10-19T09:00:00-04:00", {"companies/AXP/updates/2026-10-19.md": update_md("AXP")})
    recorded = accept.p2_updates(at(ws, "2026-12-08T12:00:00-05:00"))
    assert recorded.status == accept.PASS, text_of(recorded)
    assert "AXP FY2026Q3 (candidate) · EDGAR accepted 2026-10-16T07:00:28-04:00" in text_of(recorded)


# ---------------------------------------------------------------- P3 first monthly letter


def test_p3_missing_letter_is_pending_until_the_due_date(tmp_path):
    ws, _, _ = phase1_workspace(tmp_path)
    before = accept.p3_first_letter(at(ws, "2026-10-30T12:00:00-04:00"))
    assert before.status == accept.PENDING
    assert "[PENDING] letters/2026-10.md does not exist yet (due 2026-11-02T23:59:59-05:00)" in text_of(before)
    assert accept.p3_first_letter(at(ws, "2026-11-03T00:00:01-05:00")).status == accept.FAIL


@pytest.mark.parametrize("committed, expected", [
    ("2026-11-02T23:30:00-05:00", accept.PASS),  # November 2 in New York, already November 3 in UTC
    ("2026-11-03T00:30:00-05:00", accept.FAIL),
])
def test_p3_due_date_is_new_york_time(tmp_path, committed, expected):
    ws, pub, _ = phase1_workspace(tmp_path)
    pub.commit(committed, {LETTER: letter_md()})
    c = accept.p3_first_letter(at(ws, "2026-11-05T12:00:00-05:00"))
    assert c.status == expected, text_of(c)


def test_p3_counts_from_the_first_final_commit(tmp_path):
    ws, pub, _ = phase1_workspace(tmp_path)
    pub.commit("2026-11-01T10:00:00-05:00", {LETTER: letter_md("draft")})
    draft = accept.p3_first_letter(at(ws, "2026-11-01T12:00:00-05:00"))
    assert draft.status == accept.PENDING
    assert "is not final yet" in text_of(draft)
    pub.commit("2026-11-04T10:00:00-05:00", {LETTER: letter_md()})
    late = accept.p3_first_letter(at(ws, "2026-11-05T12:00:00-05:00"))
    assert late.status == accept.FAIL
    assert "went into main as final only at 2026-11-04T10:00:00-05:00" in text_of(late)


def test_p3_final_letter_not_yet_merged_is_pending(tmp_path):
    ws, pub, _ = phase1_workspace(tmp_path)
    pub.write({LETTER: letter_md()})
    c = accept.p3_first_letter(at(ws, "2026-11-01T12:00:00-05:00"))
    assert c.status == accept.PENDING
    assert "the final letters/2026-10.md is not on main yet" in text_of(c)


def test_p3_letter_must_still_be_final(tmp_path):
    ws, pub, _ = phase1_workspace(tmp_path)
    pub.commit("2026-11-01T10:00:00-05:00", {LETTER: letter_md()})
    pub.commit("2026-11-03T10:00:00-05:00", {LETTER: letter_md("draft")})
    c = accept.p3_first_letter(at(ws, "2026-11-05T12:00:00-05:00"))
    assert c.status == accept.FAIL
    assert "but the current file is not final" in text_of(c)


# ---------------------------------------------------------------- P4 and the overall result


def test_p4_reuses_a2_to_a7():
    assert accept.PHASE0_RECHECK == (
        accept.a2_design, accept.a3_required_files, accept.a4_lint,
        accept.a5_thesis_tests, accept.a6_constitution_checks, accept.a7_tests,
    )


def test_p4_folds_the_phase0_results(tmp_path, monkeypatch):
    def ok(ctx):
        return accept.Criterion("A2", "design", detail="existence only", items=["sha256 ..."])

    def bad(ctx):
        return accept.Criterion("A4", "lint", status=accept.FAIL, items=["owners-office: 1 errors"])

    ctx = accept.Context(tmp_path, allow_uncommitted=False)
    monkeypatch.setattr(accept, "PHASE0_RECHECK", (ok,))
    c = accept.p4_phase0(ctx)
    assert c.status == accept.PASS
    assert c.items == ["[PASS] A2 design", "  existence only", "  sha256 ..."]
    monkeypatch.setattr(accept, "PHASE0_RECHECK", (ok, bad))
    c = accept.p4_phase0(ctx)
    assert c.status == accept.FAIL
    assert "[FAIL] A4 lint" in c.items


def test_main_phase1_exit_codes_json_and_report(tmp_path, monkeypatch, capsys, lint):
    ws, _, _ = phase1_workspace(tmp_path)
    monkeypatch.setattr(accept, "PHASE0_RECHECK", ())  # do not run A2-A7 again inside a test
    code = accept.main(["--phase", "1", "--workspace", str(ws), "--as-of", "2026-09-25T12:00:00-04:00", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 3
    assert payload["result"] == accept.PENDING
    assert payload["evaluated_at"] == "2026-09-25T12:00:00-04:00"
    assert [c["id"] for c in payload["criteria"]] == ["P1", "P2", "P3", "P4"]
    code = accept.main(["--phase", "1", "--workspace", str(ws), "--as-of", "2026-09-25",
                        "--write-report", "docs/acceptance/phase-1.md"])
    out = capsys.readouterr().out
    assert code == 3
    assert "Result: PENDING" in out
    report = (ws / "owners-office" / "docs" / "acceptance" / "phase-1.md").read_text(encoding="utf-8")
    assert "- Result: **PENDING" in report and "| P3 |" in report
    assert str(ws) not in report
    assert accept.main(["--phase", "1", "--workspace", str(ws), "--as-of", "2026-12-08"]) == 1  # window closed, no prereg
    assert accept.main(["--phase", "2", "--workspace", str(ws)]) == 2
