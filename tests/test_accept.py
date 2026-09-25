"""scripts/accept.py 的测试：解析 thesis-ci 输出的容错，以及不依赖外部命令的几条验收标准。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "accept", Path(__file__).resolve().parents[1] / "scripts" / "accept.py"
)
accept = importlib.util.module_from_spec(SPEC)
sys.modules["accept"] = accept  # dataclasses 需要在 sys.modules 里找到模块
SPEC.loader.exec_module(accept)


# ---------------------------------------------------------------- lint 输出解析


@pytest.mark.parametrize(
    "data, errors, warnings",
    [
        ([{"check": "C-SCHEMA", "level": "error", "path": "a.yml", "message": "坏"}], 1, 0),
        ({"findings": [{"check": "C-SRC-ACCESSION", "severity": "warning", "message": "缺登记号"}]}, 0, 1),
        ({"errors": ["一个错误"], "warnings": [{"message": "一个警告"}]}, 1, 1),
        (
            {"results": [{"id": "C-SCHEMA", "level": "error", "status": "pass", "findings": []}]},
            0,
            0,
        ),
        (
            {"results": [{"id": "C-STORY", "level": "error", "findings": [{"path": "x/story.md", "message": "太长"}]}]},
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
    text = "正在检查……\n{\"findings\": []}\n完成"
    assert accept.load_json_output(text) == {"findings": []}
    assert accept.load_json_output("不是 JSON") is None


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


# ---------------------------------------------------------------- 不依赖外部命令的标准


def make_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    for name in accept.REPOS:
        (ws / name).mkdir(parents=True)
    (ws / "inputs").mkdir()
    (ws / "inputs" / "DESIGN.md").write_text("# 设计\n", encoding="utf-8")
    return ws


def test_a2_detects_mismatch(tmp_path):
    ws = make_workspace(tmp_path)
    ctx = accept.Context(ws, allow_uncommitted=True)
    assert accept.a2_design(ctx).status == accept.FAIL
    (ws / "owners-office" / "docs").mkdir()
    (ws / "owners-office" / "docs" / "DESIGN.md").write_text("# 设计\n", encoding="utf-8")
    assert accept.a2_design(ctx).status == accept.PASS
    (ws / "owners-office" / "docs" / "DESIGN.md").write_text("# 改过\n", encoding="utf-8")
    assert accept.a2_design(ctx).status == accept.FAIL


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
        (d / "story.md").write_text("---\ncompany: X\n---\n故事\n", encoding="utf-8")


def test_a5_requires_five_tests_of_three_types_and_story(tmp_path):
    ws = make_workspace(tmp_path)
    ctx = accept.Context(ws, allow_uncommitted=True)
    assert accept.a5_thesis_tests(ctx).status == accept.FAIL  # 没有公司
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
    c = accept.Criterion("A2", "t", items=[f"{ws}/owners-office/docs/DESIGN.md 缺失"])
    report = accept.as_markdown(0, "2026-09-24T00:00:00+00:00", ws, [c])
    assert str(ws) not in report
    assert "owners-office/docs/DESIGN.md 缺失" in report
