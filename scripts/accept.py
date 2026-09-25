#!/usr/bin/env python3
"""阶段验收脚本 / Phase acceptance script.

用法（在任何目录下运行）：

    .venv/bin/python owners-office/scripts/accept.py --phase 0
        [--workspace /Users/asuka/OwnersOffice] [--json]
        [--write-report docs/acceptance/phase-0.md] [--allow-uncommitted]

工作区布局：<workspace>/ 下有 thesis-ci、owners-office、owners-office-private 三个兄弟目录；
<workspace>/inputs/DESIGN.md 存在时，A2 额外核对它与 owners-office/docs/DESIGN.md 的 sha256。
默认工作区是本脚本所在仓库的上一级目录。--write-report 的相对路径以 owners-office 仓库根为准。

第 0 阶段的验收标准（DESIGN.md：lint 全部通过；每家至少 5 条 thesis tests；
宪法每条规则都对应一个可执行检查）展开为 A1–A7，每条输出 PASS / FAIL / SKIP 和明细。
退出码：没有 FAIL 为 0（有 SKIP 时结论是“临时通过”，不能作为进入下一阶段的依据）；
有 FAIL 为 1；参数或环境错误为 2。只依赖标准库和 PyYAML。
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - 环境问题
    print("需要 PyYAML：pip install -r requirements.txt", file=sys.stderr)
    sys.exit(2)

REPOS = ("thesis-ci", "owners-office", "owners-office-private")
TEST_TYPES = ("quantitative", "qualitative", "staleness")
MIN_TESTS = 5
MAX_CLAUDE_LINES = 200
MIN_AGENTS = 5
LINT_TIMEOUT = 600
TEST_TIMEOUT = 900

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


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


@dataclasses.dataclass
class Run:
    returncode: int
    stdout: str
    stderr: str

    def tail(self, lines: int = 20) -> list[str]:
        text = (self.stdout + "\n" + self.stderr).strip()
        return text.splitlines()[-lines:] if text else []


def run(cmd: list[str], cwd: Path, timeout: int) -> Run:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env, check=False
        )
    except FileNotFoundError as exc:
        return Run(127, "", f"找不到可执行文件：{exc}")
    except subprocess.TimeoutExpired:
        return Run(124, "", f"超时（{timeout} 秒）：{' '.join(cmd)}")
    return Run(proc.returncode, proc.stdout, proc.stderr)


def git(repo: Path, *args: str) -> str | None:
    result = run(["git", "-C", str(repo), *args], cwd=repo, timeout=60)
    return result.stdout.strip() if result.returncode == 0 else None


def find_thesis_ci() -> str | None:
    """先找与当前 Python 同目录的 thesis-ci（虚拟环境），再找 PATH。"""
    here = Path(sys.executable).parent  # 不 resolve：虚拟环境的 python 常是符号链接
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


# ---------------------------------------------------------------- 解析 thesis-ci 的 JSON 输出


def load_json_output(text: str) -> Any:
    """解析命令输出里的 JSON；容忍 JSON 前后夹杂的日志行。解析不了返回 None。"""
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
    """从未知形状的 lint JSON 里收集 (级别, 描述)。

    认得这些形状：发现列表；{"findings": [...]}；{"errors": [...], "warnings": [...]}；
    按检查分组、组内再列发现的结构。没有级别字段的发现按所在列表（errors / warnings）定级。
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
    """`thesis-ci checks --format json` → {检查 id: 信息}。认得列表、{"checks": [...]} 和 {id: {...}}。"""
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


# ---------------------------------------------------------------- 验收标准


class Context:
    def __init__(self, workspace: Path, allow_uncommitted: bool):
        self.ws = workspace
        self.pub = workspace / "owners-office"
        self.priv = workspace / "owners-office-private"
        self.tci_repo = workspace / "thesis-ci"
        self.allow_uncommitted = allow_uncommitted
        self.tci = find_thesis_ci()
        self._selftest: Run | None = None

    def selftest(self) -> Run:
        if self._selftest is None:
            if self.tci is None:
                self._selftest = Run(127, "", "找不到 thesis-ci 可执行文件")
            else:
                self._selftest = run([self.tci, "selftest"], cwd=self.ws, timeout=LINT_TIMEOUT)
        return self._selftest


def a1_repositories(ctx: Context) -> Criterion:
    c = Criterion("A1", "三个仓库存在，且各是至少有一次提交的 git 仓库")
    for name in REPOS:
        repo = ctx.ws / name
        if not repo.is_dir():
            c.fail(f"{name}: 目录不存在")
            continue
        if ctx.allow_uncommitted:
            c.items.append(f"{name}: 目录存在（--allow-uncommitted：未检查 git）")
            continue
        top = git(repo, "rev-parse", "--show-toplevel")
        if top is None or Path(top).resolve() != repo.resolve():
            c.fail(f"{name}: 不是独立的 git 仓库")
            continue
        count = git(repo, "rev-list", "--count", "HEAD")
        if not count or not count.isdigit() or int(count) < 1:
            c.fail(f"{name}: 还没有提交")
            continue
        c.items.append(f"{name}: {count} 个提交")
    if c.status == PASS and ctx.allow_uncommitted:
        c.status = SKIP
        c.detail = "git 检查已跳过；这次结果只是临时通过"
    return c


def a2_design(ctx: Context) -> Criterion:
    c = Criterion("A2", "owners-office/docs/DESIGN.md 存在，并与 inputs/DESIGN.md 一致")
    target = ctx.pub / "docs" / "DESIGN.md"
    if not target.is_file():
        c.fail("owners-office/docs/DESIGN.md 不存在")
        return c
    source = ctx.ws / "inputs" / "DESIGN.md"
    if not source.is_file():
        c.detail = "工作区没有 inputs/，只检查存在性"
        c.items.append(f"docs/DESIGN.md sha256 {sha256_file(target)}")
        return c
    a, b = sha256_file(source), sha256_file(target)
    if a == b:
        c.items.append(f"sha256 一致：{a}")
    else:
        c.fail(f"sha256 不一致：inputs {a} ≠ docs {b}")
    return c


def a3_required_files(ctx: Context) -> Criterion:
    c = Criterion("A3", "必需文件齐全")
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
            c.fail(f"owners-office/{rel} 缺失")
    claude = pub / "CLAUDE.md"
    if claude.is_file():
        lines = len(claude.read_text(encoding="utf-8").splitlines())
        if lines > MAX_CLAUDE_LINES:
            c.fail(f"CLAUDE.md 有 {lines} 行，超过 {MAX_CLAUDE_LINES} 行")
        else:
            c.items.append(f"CLAUDE.md {lines} 行")
    decisions = sorted((pub / "docs" / "decisions").glob("*.md"))
    if not decisions:
        c.fail("docs/decisions/ 下没有决策记录")
    else:
        c.items.append(f"决策记录 {len(decisions)} 份")
    agents = sorted((pub / "agents").glob("*.yml"))
    if len(agents) < MIN_AGENTS:
        c.fail(f"agents/*.yml 只有 {len(agents)} 个，至少需要 {MIN_AGENTS} 个")
    else:
        c.items.append(f"agents/*.yml {len(agents)} 个")
    workflows = [
        p
        for p in sorted((pub / ".github" / "workflows").glob("*.y*ml"))
        if re.search(r"thesis-ci\s+lint", p.read_text(encoding="utf-8"))
    ]
    if not workflows:
        c.fail(".github/workflows/ 下没有调用 thesis-ci lint 的 workflow")
    else:
        c.items.append("调用 thesis-ci lint 的 workflow：" + ", ".join(p.name for p in workflows))
    for repo, expected in ((pub, "public"), (priv, "private")):
        marker = repo / "repo.yml"
        if not marker.is_file():
            c.fail(f"{repo.name}/repo.yml 缺失")
            continue
        data = load_yaml(marker) or {}
        if data.get("visibility") != expected:
            c.fail(f"{repo.name}/repo.yml 的 visibility 应为 {expected}")
    companies = company_dirs(pub)
    missing = [d.name for d in companies if not (priv / "companies" / d.name / "valuation.yml").is_file()]
    for name in missing:
        c.fail(f"owners-office-private/companies/{name}/valuation.yml 缺失")
    if companies and not missing:
        c.items.append(f"私有 valuation.yml 覆盖全部 {len(companies)} 家公开公司")
    return c


def a4_lint(ctx: Context) -> Criterion:
    c = Criterion("A4", "thesis-ci lint：公开与私有仓库零错误")
    if ctx.tci is None:
        c.fail("找不到 thesis-ci 可执行文件（虚拟环境或 PATH）")
        return c
    runs = (
        ("owners-office", [ctx.tci, "lint", str(ctx.pub), "--format", "json"]),
        (
            "owners-office-private",
            [ctx.tci, "lint", str(ctx.priv), "--counterpart", str(ctx.pub), "--format", "json"],
        ),
    )
    for label, cmd in runs:
        result = run(cmd, cwd=ctx.ws, timeout=LINT_TIMEOUT)
        data = load_json_output(result.stdout)
        if data is None:
            c.fail(f"{label}: 无法解析 lint 的 JSON 输出（退出码 {result.returncode}）")
            c.items.extend(f"  {line}" for line in result.tail())
            continue
        outcome = parse_lint(data)
        if outcome.n_errors:
            c.fail(f"{label}: {outcome.n_errors} 个错误，{outcome.n_warnings} 个警告")
            c.items.extend(f"  错误 {text}" for text in outcome.errors)
        elif result.returncode != 0:
            c.fail(f"{label}: 没有解析到错误，但 lint 退出码为 {result.returncode}")
            c.items.extend(f"  {line}" for line in result.tail())
        else:
            c.items.append(f"{label}: 0 个错误，{outcome.n_warnings} 个警告")
        c.items.extend(f"  警告 {text}" for text in outcome.warnings)
    return c


def a5_thesis_tests(ctx: Context) -> Criterion:
    c = Criterion("A5", f"每家公司至少 {MIN_TESTS} 条 thesis tests（三类齐全），并有 story.md")
    companies = company_dirs(ctx.pub)
    if not companies:
        c.fail("owners-office/companies/ 下没有公司")
        return c
    holdings = []
    for d in companies:
        thesis, story = d / "thesis.yml", d / "story.md"
        if not story.is_file():
            c.fail(f"{d.name}: 缺少 story.md")
        if not thesis.is_file():
            c.fail(f"{d.name}: 缺少 thesis.yml")
            continue
        try:
            data = load_yaml(thesis) or {}
        except yaml.YAMLError as exc:
            c.fail(f"{d.name}: thesis.yml 不是合法 YAML（{exc.__class__.__name__}）")
            continue
        tests = data.get("tests") if isinstance(data, dict) else None
        tests = tests if isinstance(tests, list) else []
        types = {t.get("type") for t in tests if isinstance(t, dict)}
        missing = [t for t in TEST_TYPES if t not in types]
        status = data.get("status") if isinstance(data, dict) else None
        if status == "holding":
            holdings.append(d.name)
        summary = f"{d.name}（{status or '无状态'}）: {len(tests)} 条测试"
        if len(tests) < MIN_TESTS or missing:
            reason = f"少于 {MIN_TESTS} 条" if len(tests) < MIN_TESTS else ""
            if missing:
                reason = "；".join(filter(None, [reason, "缺少类型 " + "、".join(missing)]))
            c.fail(f"{summary}，{reason}")
        else:
            c.items.append(summary + "，三类齐全")
    c.detail = "持仓：" + ("、".join(holdings) if holdings else "无")
    return c


def a6_constitution_checks(ctx: Context) -> Criterion:
    c = Criterion("A6", "宪法每条规则都对应已实现并通过自检的检查")
    rules_path = ctx.pub / "constitution" / "rules.yml"
    if not rules_path.is_file():
        c.fail("constitution/rules.yml 不存在")
        return c
    rules = (load_yaml(rules_path) or {}).get("rules") or []
    if not rules:
        c.fail("constitution/rules.yml 没有规则")
        return c
    if ctx.tci is None:
        c.fail("找不到 thesis-ci 可执行文件")
        return c
    result = run([ctx.tci, "checks", "--format", "json"], cwd=ctx.ws, timeout=LINT_TIMEOUT)
    table = parse_checks(load_json_output(result.stdout))
    if result.returncode != 0 or table is None:
        c.fail(f"thesis-ci checks --format json 失败或输出无法解析（退出码 {result.returncode}）")
        c.items.extend(f"  {line}" for line in result.tail())
        return c
    selftest_ok = ctx.selftest().returncode == 0
    notes: set[str] = set()
    for rule in rules:
        rule_id = rule.get("id", "?") if isinstance(rule, dict) else "?"
        checks = rule.get("checks") if isinstance(rule, dict) else None
        if not checks:
            c.fail(f"{rule_id}: 没有引用任何检查")
            continue
        problems = []
        for check_id in checks:
            info = table.get(check_id)
            if info is None:
                problems.append(f"{check_id} 未在 thesis-ci 中登记")
                continue
            implemented = _flag(info, ("implemented", "has_impl", "has_implementation", "registered"))
            tested = _flag(info, ("tested", "has_test", "has_tests", "unit_tests", "tests", "test_count"))
            passed = _flag(info, ("selftest", "selftest_passed", "selftest_ok", "passes_selftest"))
            if implemented is False:
                problems.append(f"{check_id} 未实现")
            elif implemented is None:
                notes.add("checks 输出没有实现状态字段，按“已列出即已实现”处理")
            if tested is False:
                problems.append(f"{check_id} 没有单元测试")
            if passed is False:
                problems.append(f"{check_id} 自检未通过")
            elif passed is None and not selftest_ok:
                problems.append(f"{check_id}：thesis-ci selftest 整体未通过")
        if problems:
            c.fail(f"{rule_id}: " + "；".join(problems))
        else:
            c.items.append(f"{rule_id}: " + "、".join(checks))
    c.detail = "；".join(sorted(notes))
    return c


def a7_tests(ctx: Context) -> Criterion:
    c = Criterion("A7", "thesis-ci selftest 退出码为 0；thesis-ci 与 owners-office 的 pytest 通过")
    selftest = ctx.selftest()
    if selftest.returncode == 0:
        c.items.append("thesis-ci selftest: 通过")
    else:
        c.fail(f"thesis-ci selftest: 退出码 {selftest.returncode}")
        c.items.extend(f"  {line}" for line in selftest.tail())
    for label, repo, extra in (("thesis-ci", ctx.tci_repo, []), ("owners-office", ctx.pub, ["tests"])):
        if not repo.is_dir():
            c.fail(f"{label}: 目录不存在")
            continue
        cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *extra]
        result = run(cmd, cwd=repo, timeout=TEST_TIMEOUT)
        last = result.tail(1)[0] if result.tail(1) else ""
        if result.returncode == 0:
            c.items.append(f"{label} pytest: {last}")
        elif result.returncode == 5:
            c.fail(f"{label} pytest: 没有收集到测试")
        else:
            c.fail(f"{label} pytest: 退出码 {result.returncode}")
            c.items.extend(f"  {line}" for line in result.tail())
    return c


PHASES = {0: (a1_repositories, a2_design, a3_required_files, a4_lint, a5_thesis_tests, a6_constitution_checks, a7_tests)}


# ---------------------------------------------------------------- 输出


def overall(criteria: list[Criterion]) -> str:
    if any(c.status == FAIL for c in criteria):
        return FAIL
    if any(c.status == SKIP for c in criteria):
        return "PASS_PROVISIONAL"
    return PASS


RESULT_LABEL = {PASS: "PASS", FAIL: "FAIL", "PASS_PROVISIONAL": "PASS（临时：有检查被跳过）"}


def as_text(phase: int, stamp: str, ws: Path, criteria: list[Criterion]) -> str:
    lines = [f"第 {phase} 阶段验收 · {stamp}", f"工作区：{ws}", ""]
    for c in criteria:
        lines.append(f"[{c.status}] {c.id} {c.title}")
        if c.detail:
            lines.append(f"       {c.detail}")
        lines.extend(f"       {item}" for item in c.items)
    lines += ["", f"结论：{RESULT_LABEL[overall(criteria)]}"]
    return "\n".join(lines)


def as_markdown(phase: int, stamp: str, ws: Path, criteria: list[Criterion]) -> str:
    """写进公开仓库的报告：不含本机绝对路径；明细放在代码块里。"""
    prefix = str(ws.resolve()) + os.sep

    def clean(text: str) -> str:
        return text.replace(prefix, "").replace(str(ws), "<workspace>")

    out = [
        f"# 第 {phase} 阶段验收报告",
        "",
        f"- 生成时间：{stamp}",
        "- 生成方式：`scripts/accept.py`（工作区中 thesis-ci、owners-office、owners-office-private 为兄弟目录）",
        f"- 结论：**{RESULT_LABEL[overall(criteria)]}**",
        "",
        "| 编号 | 标准 | 结果 |",
        "| --- | --- | --- |",
    ]
    out += [f"| {c.id} | {c.title} | {c.status} |" for c in criteria]
    for c in criteria:
        out += ["", f"## {c.id} {c.title}", "", f"结果：{c.status}"]
        body = ([c.detail] if c.detail else []) + c.items
        if body:
            out += ["", "```text", *(clean(line) for line in body), "```"]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    default_ws = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="Owner's Office 阶段验收")
    parser.add_argument("--phase", type=int, required=True, help="阶段编号（目前只有 0）")
    parser.add_argument("--workspace", type=Path, default=default_ws, help=f"工作区根目录（默认 {default_ws}）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    parser.add_argument("--write-report", type=Path, help="把 Markdown 报告写到此路径（相对路径以 owners-office 为准）")
    parser.add_argument("--allow-uncommitted", action="store_true", help="跳过 A1 的 git 提交检查")
    args = parser.parse_args(argv)

    if args.phase not in PHASES:
        print(f"第 {args.phase} 阶段的验收标准尚未定义", file=sys.stderr)
        return 2
    ws = args.workspace.expanduser().resolve()
    if not ws.is_dir():
        print(f"工作区不存在：{ws}", file=sys.stderr)
        return 2

    ctx = Context(ws, args.allow_uncommitted)
    criteria = [check(ctx) for check in PHASES[args.phase]]
    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    result = overall(criteria)

    if args.json:
        payload = {
            "phase": args.phase,
            "generated_at": stamp,
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
            print(f"报告已写入 {report}")

    return 1 if result == FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
