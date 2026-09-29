"""Tests of pipeline/llm.py: all with test-double clients; no network, and the model SDK need not be installed.

The prompts, role definitions and schemas are synthetic files in a temporary directory (tests/llm_fixtures.py); the
private repository's prompts are not read. Only test_role_table_* read the public repository's real agents/*.yml: they
are the one source of the role table.

The Claude Code CLI backend is tested in tests/test_claude_code.py. Here an autouse fixture selects the API backend,
so the injected test-double clients take the SDK path.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline import fake_client, llm
from tests.llm_fixtures import (
    DRAFT_REPLY,
    DRAFT_VARIABLES,
    FakeClient,
    THESIS_YML,
    UPDATE_MD,
    envelope,
    make_env,
    make_response,
    write_agent,
)

NOW = dt.datetime(2026, 9, 24, 12, 0, tzinfo=dt.timezone.utc)
DRAFT_INPUTS = {"run_date": "2026-09-24", "filings": "Revenue grew year on year.", "thesis": "Thesis summary."}


@pytest.fixture(autouse=True)
def api_backend(monkeypatch):
    """Run complete() on the API backend unless a test passes backend= (the default backend is claude-code)."""
    monkeypatch.setenv(llm.BACKEND_ENV, llm.API)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv(llm.PROMPTS_ENV, raising=False)
    return make_env(tmp_path)


def call(env, client, role="company_manager", prompt_id="03", inputs=None, **kwargs):
    """Runs the draft part of the synthetic prompt 03 by default."""
    kwargs.setdefault("budget_usd", 20.0)
    kwargs.setdefault("schemas_dir", env.schemas)
    if prompt_id == "03":
        kwargs.setdefault("part", "draft")
        kwargs.setdefault("variables", DRAFT_VARIABLES)
    return llm.complete(
        role,
        prompt_id,
        DRAFT_INPUTS if inputs is None else inputs,
        client=client,
        log_path=env.log,
        repo_root=env.repo,
        prompts_dir=env.prompts,
        **kwargs,
    )


def read_log(log_path):
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]


def only_request(client):
    (kwargs,) = client.requests
    return kwargs


# ---------------------------------------------------------------- Hashes


def test_input_hash_is_deterministic_and_independent_of_key_order():
    a = llm.input_sha256("03", "prompt text", {"b": "2", "a": "1"})
    b = llm.input_sha256("03", "prompt text", {"a": "1", "b": "2"})
    assert a == b
    expected = hashlib.sha256(
        json.dumps(
            {"inputs": {"a": "1", "b": "2"}, "prompt": "prompt text", "prompt_id": "03"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert a == expected


def test_input_hash_changes_with_prompt_id_prompt_or_inputs():
    base = llm.input_sha256("03", "prompt text", {"a": "1"})
    assert llm.input_sha256("04", "prompt text", {"a": "1"}) != base
    assert llm.input_sha256("03", "prompt text.", {"a": "1"}) != base
    assert llm.input_sha256("03", "prompt text", {"a": "2"}) != base
    assert llm.input_sha256("03", "prompt text", {"a": "1", "b": ""}) != base
    assert llm.input_sha256("03", "prompt text", {"a": "1"}, run={"part": "draft"}) != base


def test_rendered_inputs_are_sorted_and_stable():
    text = llm.render_inputs({"z": "last", "a": "first"})
    assert text.index('<input name="a">') < text.index('<input name="z">')
    assert text == llm.render_inputs({"a": "first", "z": "last"})
    assert llm.render_inputs({"z": "last", "a": "first"}, order=["z", "a"]).startswith('<input name="z">')


def test_same_call_twice_logs_same_input_hash(env):
    client = FakeClient(make_response())
    first = call(env, client)
    second = call(env, client)
    assert first.input_sha256 == second.input_sha256
    assert [r["input_sha256"] for r in read_log(env.log)] == [first.input_sha256] * 2


def test_input_hash_covers_the_whole_system_prompt(env):
    first = call(env, FakeClient(make_response()))
    (env.prompts / "00-series-rules.md").write_text(
        (env.prompts / "00-series-rules.md").read_text(encoding="utf-8") + "One more rule.\n", encoding="utf-8"
    )
    second = call(env, FakeClient(make_response()))
    assert first.input_sha256 != second.input_sha256


# ---------------------------------------------------------------- Log


def test_log_line_has_required_fields_and_cost(env):
    client = FakeClient(make_response(input_tokens=1000, output_tokens=500))
    result = call(env, client)

    (record,) = read_log(env.log)
    for field in (
        "timestamp",
        "backend",
        "role",
        "prompt_id",
        "part",
        "part_id",
        "requested_model",
        "model",
        "prompt_version",
        "prompt_revision",
        "rules_version",
        "rules_revision",
        "input_sha256",
        "output_sha256",
        "usage",
        "cost_usd",
        "stop_reason",
    ):
        assert field in record, field
    assert record["backend"] == "api"
    assert record["role"] == "company_manager"
    assert record["prompt_id"] == "03"
    assert record["part"] == "draft"
    assert record["part_id"] == "03-draft"
    assert record["prompt_path"] == "prompts/03-update.md"
    assert record["requested_model"] == record["model"] == "claude-sonnet-5"
    assert record["usage"]["input_tokens"] == 1000
    assert record["usage"]["output_tokens"] == 500
    # 1000 × 2 / 1e6 + 500 × 10 / 1e6
    assert record["cost_usd"] == pytest.approx(0.007)
    assert record["stop_reason"] == "end_turn"
    assert record["output_sha256"] == hashlib.sha256(DRAFT_REPLY.encode()).hexdigest()
    assert record["attempt"] == 1
    stamp = dt.datetime.fromisoformat(record["timestamp"])
    assert stamp.utcoffset() == dt.timedelta(0)

    assert result.text == DRAFT_REPLY
    assert result.cost_usd == record["cost_usd"]
    assert result.backend == "api"
    assert result.notional_cost_usd is None  # only the claude-code backend has a notional cost
    assert result.to_dict()["input_sha256"] == record["input_sha256"]


def test_versions_of_00_and_the_prompt_are_logged_separately(env):
    call(env, FakeClient(make_response()))
    (record,) = read_log(env.log)
    # the front matter versions
    assert record["rules_version"] == "9.0"
    assert record["prompt_version"] == "3.1"
    # file revisions (not in git: content hash + dirty)
    assert record["rules_revision"] == llm.file_revision(env.prompts / "00-series-rules.md")
    assert record["prompt_revision"] == llm.file_revision(env.prompts / "03-update.md")
    # 03 lays out no pages: 00D is not loaded
    assert record["design_version"] is None and record["design_revision"] is None


def test_only_text_blocks_are_read(env):
    head, tail = DRAFT_REPLY[:40], DRAFT_REPLY[40:]
    content = [
        SimpleNamespace(type="thinking", thinking="must not appear"),
        SimpleNamespace(type="text", text=head),
        SimpleNamespace(type="fallback", text="must not appear"),
        SimpleNamespace(type="text", text=tail),
    ]
    result = call(env, FakeClient(make_response(content=content)))
    assert result.text == DRAFT_REPLY
    assert "update" in result.outputs


def test_api_error_is_logged_and_reraised(env):
    client = FakeClient(error=RuntimeError("connection reset"))
    with pytest.raises(RuntimeError):
        call(env, client)
    (record,) = read_log(env.log)
    assert record["backend"] == "api"
    assert record["cost_usd"] == 0.0
    assert record["stop_reason"] is None
    assert record["error"] == "RuntimeError: connection reset"


# ---------------------------------------------------------------- File revisions (the git hashes of 00 §H5)


def test_file_revision_falls_back_to_content_hash_outside_git(env):
    path = env.prompts / "03-update.md"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert llm.file_revision(path) == f"sha256:{digest}+dirty"


def test_file_revision_is_commit_hash_when_clean(env, monkeypatch):
    commit = "a" * 40

    def fake_git(cwd, *args):
        return {"log": commit, "status": ""}[args[0]]

    monkeypatch.setattr(llm, "_git", fake_git)
    assert llm.file_revision(env.prompts / "03-update.md") == commit


def test_file_revision_is_dirty_when_file_has_uncommitted_changes(env, monkeypatch):
    def fake_git(cwd, *args):
        return {"log": "b" * 40, "status": " M 03-update.md"}[args[0]]

    monkeypatch.setattr(llm, "_git", fake_git)
    version = llm.file_revision(env.prompts / "03-update.md")
    assert version.startswith("sha256:") and version.endswith("+dirty")


def test_file_revision_is_dirty_when_never_committed(env, monkeypatch):
    monkeypatch.setattr(llm, "_git", lambda cwd, *args: "" if args[0] == "log" else "")
    assert llm.file_revision(env.prompts / "03-update.md").endswith("+dirty")


def test_logged_prompt_revision_matches_file(env):
    call(env, FakeClient(make_response()))
    (record,) = read_log(env.log)
    assert record["prompt_revision"] == llm.file_revision(env.prompts / "03-update.md")


# ---------------------------------------------------------------- Refusals and truncation


class ExplodingContent:
    """Fails as soon as content is read: shows that a refusal is detected from stop_reason, without reading content."""

    def __iter__(self):
        raise AssertionError("content must not be read on a refusal")


def audit(env, client, **kwargs):
    kwargs.setdefault("variables", {"subject": "quarterly update"})
    return call(env, client, role="auditor", prompt_id="04", part="A",
                inputs={"fact_table": "- revenue 100", "sources": "Source text."}, **kwargs)


def test_refusal_raises_before_reading_content_and_is_logged(env):
    response = make_response(
        model="claude-fable-5-1",
        stop_reason="refusal",
        input_tokens=0,
        output_tokens=0,
        content=ExplodingContent(),
        stop_details=SimpleNamespace(category="cyber", explanation="classifier refused"),
    )
    with pytest.raises(llm.LLMRefusal) as info:
        audit(env, FakeClient(response))
    assert info.value.category == "cyber"
    assert "model claude-fable-5-1 refused the request (stop_reason=refusal, category=cyber)" in str(info.value)
    (record,) = read_log(env.log)
    assert record["stop_reason"] == "refusal"
    assert record["refusal_category"] == "cyber"
    assert record["output_sha256"] is None


def test_refusal_with_null_stop_details_still_raises(env):
    response = make_response(stop_reason="refusal", content=[], stop_details=None)
    with pytest.raises(llm.LLMRefusal, match="category=unknown"):
        call(env, FakeClient(response))


def test_truncated_output_raises_unless_allowed(env):
    response = make_response("Half an ans", stop_reason="max_tokens")
    with pytest.raises(llm.LLMTruncated, match="the output hit max_tokens and was truncated") as info:
        call(env, FakeClient(response))
    assert info.value.result.text == "Half an ans"
    result = call(env, FakeClient(response), allow_truncated=True)
    assert result.stop_reason == "max_tokens"
    assert result.outputs == {}  # a truncated reply is not validated and yields no outputs
    assert len(read_log(env.log)) == 2


# ---------------------------------------------------------------- Budget


def write_log(log_path, records, *, raw_lines=()):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r) for r in records] + list(raw_lines)
    log_path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")


def this_month(cost, backend=None):
    """A log line from this month; without backend it is an old-style line, which counts as an API call."""
    record = {"timestamp": llm._utcnow().isoformat(timespec="seconds"), "cost_usd": cost}
    if backend is not None:
        record["backend"] = backend
    return record


def test_budget_guard_blocks_call_when_month_spend_reaches_budget(env):
    write_log(env.log, [this_month(0.6), this_month(0.4, "api")], raw_lines=["{bad line", '"not an object"'])
    client = FakeClient(make_response())
    with pytest.raises(llm.BudgetExceeded) as info:
        call(env, client, budget_usd=1.0)
    assert info.value.spent_usd == pytest.approx(1.0)
    assert "this month's API spend of 1.0000 USD has reached the budget of 1.00 USD" in str(info.value)
    assert client.call_count == 0
    lines = env.log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4  # no request was sent, so no log line was appended


def test_budget_guard_allows_call_below_budget(env):
    write_log(env.log, [this_month(0.2)])
    client = FakeClient(make_response())
    call(env, client, budget_usd=1.0)
    assert client.call_count == 1


def test_budget_guard_also_stops_the_retry(env):
    bad = make_response(envelope(update=UPDATE_MD, thesis=THESIS_YML))  # questions is missing
    client = FakeClient(bad, make_response())
    with pytest.raises(llm.BudgetExceeded):
        call(env, client, budget_usd=0.005)  # the first attempt costs 0.007: the budget is spent before the retry
    assert client.call_count == 1
    assert len(read_log(env.log)) == 1


def test_budget_guard_ignores_previous_months(env):
    write_log(
        env.log,
        [
            {"timestamp": "2026-08-31T23:59:59+00:00", "cost_usd": 50.0},
            {"timestamp": "2026-09-02T08:00:00+00:00", "cost_usd": 0.25},
        ],
    )
    assert llm.month_spend(env.log, now=NOW) == pytest.approx(0.25)
    assert llm.remaining_budget(log_path=env.log, budget_usd=1.0, now=NOW) == pytest.approx(0.75)
    status = llm.budget_status(log_path=env.log, budget_usd=1.0, now=NOW)
    assert status["month"] == "2026-09"
    assert status["stage"] == "normal"


def test_month_spend_counts_only_api_lines(env):
    write_log(
        env.log,
        [
            {"timestamp": "2026-09-10T00:00:00+00:00", "cost_usd": 1.0, "backend": "api"},
            {"timestamp": "2026-09-11T00:00:00+00:00", "cost_usd": 0.5},  # no backend field: an API call
            {"timestamp": "2026-09-12T00:00:00+00:00", "cost_usd": 2.0, "backend": "claude-code"},
            {"timestamp": "2026-09-13T00:00:00+00:00", "cost_usd": 4.0, "backend": "fake"},
        ],
    )
    assert llm.month_spend(env.log, now=NOW) == pytest.approx(1.5)
    assert llm.budget_status(log_path=env.log, budget_usd=2.0, now=NOW)["spent_usd"] == pytest.approx(1.5)


def test_claude_code_and_fake_spend_do_not_stop_an_api_call(env):
    write_log(env.log, [this_month(5.0, "claude-code"), this_month(5.0, "fake"), this_month(0.2)])
    client = FakeClient(make_response())
    call(env, client, budget_usd=1.0)
    assert client.call_count == 1


def test_budget_status_stages(env):
    log_path = env.log
    write_log(log_path, [{"timestamp": "2026-09-10T00:00:00+00:00", "cost_usd": 15.0}])
    assert llm.budget_status(log_path=log_path, budget_usd=20.0, now=NOW)["stage"] == "pause_candidates"
    write_log(log_path, [{"timestamp": "2026-09-10T00:00:00+00:00", "cost_usd": 18.0}])
    assert llm.budget_status(log_path=log_path, budget_usd=20.0, now=NOW)["stage"] == "downgrade_drafting_model"
    write_log(log_path, [{"timestamp": "2026-09-10T00:00:00+00:00", "cost_usd": 20.0}])
    assert llm.budget_status(log_path=log_path, budget_usd=20.0, now=NOW)["stage"] == "stopped"


def test_budget_comes_from_decision_rights(env):
    (env.repo / "constitution").mkdir()
    (env.repo / "constitution" / "decision-rights.yml").write_text(
        "budget:\n  monthly_usd: 0.5\n  degrade_order: [pause_candidates]\n", encoding="utf-8"
    )
    assert llm.monthly_budget(repo_root=env.repo) == 0.5
    write_log(env.log, [this_month(0.5)])
    with pytest.raises(llm.BudgetExceeded):
        call(env, FakeClient(make_response()), budget_usd=None)


def test_default_budget_without_constitution(env):
    assert llm.monthly_budget(repo_root=env.repo) == llm.DEFAULT_MONTHLY_BUDGET_USD


# ---------------------------------------------------------------- Role table (agents/*.yml)


DRAFTING = ("company_manager", "industry_researcher", "hq_capital_allocator", "extractor", "typesetter")
OVERSIGHT = (
    "auditor", "model_reviewer", "red_team", "synthesis_reviewer", "design_reviewer", "blind_reader", "judge", "settler",
)


def test_role_table_has_the_thirteen_roles_from_agents_yml():
    roles = llm.load_roles()  # the public repository's real agents/*.yml
    assert set(roles) == set(DRAFTING) | set(OVERSIGHT)
    for name, role in roles.items():
        assert role.prompts is not None and role.can_see is not None and role.cannot_see is not None, name
        model_id, effort, _ = llm.resolve_model(name)
        assert model_id in llm.PRICES_PER_MTOK, name
        assert effort == "high", name


def test_role_table_models_follow_decision_0025():
    """Every role runs on Claude Opus 5.5 at effort high, with the server's default refusal fallback."""
    for name in (*DRAFTING, *OVERSIGHT):
        assert llm.resolve_model(name) == ("claude-opus-5-5", "high", "default"), name


def test_unknown_role_and_unpriced_model_are_rejected(env):
    with pytest.raises(ValueError, match="unknown role 'trader'"):
        call(env, FakeClient(make_response()), role="trader")
    client = FakeClient(make_response())
    for backend in ("api", "fake"):
        with pytest.raises(ValueError, match="model claude-unknown-9 is not in the price table"):
            call(env, client, model="claude-unknown-9", backend=backend)
    assert client.call_count == 0
    assert not env.log.exists()


def test_agent_yaml_found_by_role_field(tmp_path):
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "audit.yml").write_text(
        "role: auditor\nmodel:\n  id: claude-fable-5-1\n  effort: xhigh\n  fallbacks: default\n",
        encoding="utf-8",
    )
    assert llm.resolve_model("auditor", repo_root=tmp_path) == ("claude-fable-5-1", "xhigh", "default")


def test_explicit_arguments_beat_agent_yaml(tmp_path):
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "auditor.yml").write_text(
        "role: auditor\nmodel:\n  id: claude-fable-5-1\n  effort: high\n  fallbacks: claude-opus-4-8\n",
        encoding="utf-8",
    )
    assert llm.resolve_model("auditor", repo_root=tmp_path) == ("claude-fable-5-1", "high", "claude-opus-4-8")
    # after switching models, the fallbacks configured for the original model no longer apply
    assert llm.resolve_model("auditor", model="claude-haiku-4-5", repo_root=tmp_path) == (
        "claude-haiku-4-5",
        None,
        None,
    )


def test_role_without_model_id_or_defined_twice_fails_loudly(tmp_path):
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "judge.yml").write_text("role: judge\nmodel:\n  effort: high\n", encoding="utf-8")
    with pytest.raises(llm.PromptError, match="judge.yml has no model.id"):
        llm.load_roles(tmp_path)
    (agents / "judge.yml").write_text("role: judge\nmodel:\n  id: claude-fable-5-1\n", encoding="utf-8")
    (agents / "judge2.yml").write_text("role: judge\nmodel:\n  id: claude-fable-5-1\n", encoding="utf-8")
    with pytest.raises(llm.PromptError, match="role judge is defined twice"):
        llm.load_roles(tmp_path)


def test_role_file_must_be_complete_to_be_called(env):
    write_agent(env.repo, "company_manager", model="claude-sonnet-5", can_see=None)
    client = FakeClient(make_response())
    with pytest.raises(llm.PromptError, match="company_manager.yml has no can_see"):
        call(env, client)
    assert client.call_count == 0


# ---------------------------------------------------------------- Prompts, parts and ownership


def test_prompts_dir_comes_from_argument_env_or_sibling_checkout(env, monkeypatch):
    # default: the private repository checked out beside the public one
    assert llm.resolve_prompts_dir(repo_root=env.repo) == env.prompts
    other = env.prompts.parent.parent / "elsewhere"
    other.mkdir()
    monkeypatch.setenv(llm.PROMPTS_ENV, str(other))
    assert llm.resolve_prompts_dir(repo_root=env.repo) == other
    assert llm.resolve_prompts_dir(env.prompts, repo_root=env.repo) == env.prompts
    monkeypatch.setenv(llm.PROMPTS_ENV, str(other / "missing"))
    with pytest.raises(llm.PromptError, match=llm.PROMPTS_ENV):
        llm.resolve_prompts_dir(repo_root=env.repo)


def test_system_prompt_is_00_then_the_prompt(env):
    client = FakeClient(make_response())
    call(env, client)
    kwargs = only_request(client)
    texts = [block["text"] for block in kwargs["system"]]
    assert texts[0] == (env.prompts / "00-series-rules.md").read_text(encoding="utf-8")
    assert len(texts) == 2  # 03 lays out no pages: no 00D
    assert texts[1].startswith("---\nid: \"03\"")
    # the {{variables}} are filled
    assert "Synthetic quarterly update prompt for Test Co (TEST), FY2026Q3." in texts[1]
    assert "{{" not in texts[1]
    assert "{{date}}" in texts[0]  # 00 goes in as it is, with its variables unfilled


def test_design_prompts_also_get_00d(env):
    client = FakeClient(make_response(envelope(report=REPORT_MD, questions="none")))
    result = call(env, client, prompt_id="02", mode="report",
                  inputs={"run_date": "2026-09-24", "dossier": "Dossier."}, variables=DRAFT_VARIABLES)
    texts = [block["text"] for block in only_request(client)["system"]]
    assert len(texts) == 3
    assert texts[1] == (env.prompts / "00D-design-system.md").read_text(encoding="utf-8")
    assert result.design_version == "9.1"
    (record,) = read_log(env.log)
    assert record["design_version"] == "9.1"


def test_missing_variables_are_refused_before_the_call(env):
    client = FakeClient(make_response())
    with pytest.raises(llm.PromptError, match="prompt 03 needs the variables period"):
        call(env, client, variables={"company": "Test Co", "ticker": "TEST"})
    assert client.call_count == 0


def test_parts_are_chosen_by_key_or_label(env):
    prompt = llm.load_prompt("04", env.prompts)
    assert llm.prompt_part(prompt, "A").label == "04A"
    assert llm.prompt_part(prompt, "04A").key == "A"
    assert llm.prompt_part(prompt, "04B-lite").key == "B_lite"
    revise = llm.prompt_part(llm.load_prompt("03", env.prompts), "03R")
    assert revise.key == "revise"
    assert [name for name, _ in revise.outputs] == ["update", "thesis", "questions", "escalation", "story", "revision_notes"]
    assert dict(revise.outputs)["escalation"] is False
    with pytest.raises(llm.PromptError, match="part must be one of"):
        llm.prompt_part(prompt, "Z")
    with pytest.raises(llm.PromptError, match="part must be one of"):
        llm.prompt_part(prompt)


def test_multi_pass_part_needs_pass_no_and_declares_outputs_per_pass(env):
    prompt = llm.load_prompt("04", env.prompts)
    with pytest.raises(llm.PromptError, match="pass_no"):
        llm.prompt_part(prompt, "B")
    with pytest.raises(llm.PromptError, match="pass_no"):
        llm.prompt_part(prompt, "B", pass_no=3)
    with pytest.raises(llm.PromptError, match="pass_no"):
        llm.prompt_part(prompt, "A", pass_no=1)
    first = llm.prompt_part(prompt, "B", pass_no=1)
    second = llm.prompt_part(prompt, "B", pass_no=2)
    assert [n for n, _ in first.inputs] == ["product_without_counter", "dossier_without_9_12"]
    assert [n for n, _ in second.inputs] == ["pass1", "product_counter_section"]
    assert [n for n, _ in first.outputs] == ["bear_case", "findings", "questions"]
    assert [n for n, _ in second.outputs] == ["findings", "weakest_sentence", "questions"]


def write_prompt(env, name, front, body="Synthetic prompt.\n"):
    (env.prompts / name).write_text(f"---\n{front}---\n\n{body}", encoding="utf-8")
    return llm.load_prompt(name.split("-")[0], env.prompts)


def test_calls_and_per_pass_outputs_must_match_the_passes(env):
    parts = "  B: {role: red_team, calls: %s, inputs_pass1: [x], inputs_pass2: [y], %s}\n"
    bad_calls = write_prompt(env, "05-bad.md", 'id: "05"\nversion: "1"\nparts:\n' + parts % ('"2 (note)"', "outputs_pass1: [findings], outputs_pass2: [findings]"))
    with pytest.raises(llm.PromptError, match="calls of 05B should be the integer 2"):
        llm.prompt_part(bad_calls, "B", pass_no=1)
    (env.prompts / "05-bad.md").unlink()
    no_pass_outputs = write_prompt(env, "05-bad.md", 'id: "05"\nversion: "1"\nparts:\n' + parts % ("2", "outputs: [findings]"))
    with pytest.raises(llm.PromptError, match="outputs_pass1"):
        llm.prompt_part(no_pass_outputs, "B", pass_no=1)


FINDING = ("- id: {id}\n  group: should fix\n  type: reasoning\n  location: 3, The moat widens\n"
           "  quote: The moat widens.\n  evidence: The filing says otherwise [src:TEST-10K-FY2025#Item7].\n"
           "  fix: The moat is unchanged.\n")


def test_second_pass_of_red_team_runs_with_pass_one_outputs(env):
    reply = envelope(findings=FINDING.format(id="04B-01"), weakest_sentence='The sentence "...".',
                     questions="none")
    client = FakeClient(make_response(reply, model="claude-fable-5-1"))
    result = call(env, client, role="red_team", prompt_id="04", part="B", pass_no=2,
                  inputs={"pass1": "The first pass's outputs.", "product_counter_section": "The removed counter case."},
                  variables={"subject": "research report"})
    assert result.pass_no == 2 and result.part_id == "04B"
    assert result.outputs["weakest_sentence"].format == "text"
    assert '<run prompt="04" part="B" label="04B" pass="2"/>' in only_request(client)["messages"][0]["content"]
    (record,) = read_log(env.log)
    assert record["pass"] == 2


def test_second_pass_must_deliver_its_declared_outputs(env):
    client = FakeClient(make_response(envelope(findings=FINDING.format(id="04B-01")), model="claude-fable-5-1"))
    with pytest.raises(llm.LLMOutputInvalid) as info:
        call(env, client, role="red_team", prompt_id="04", part="B", pass_no=2,
             inputs={"pass1": "The first pass's outputs.", "product_counter_section": "The removed counter case."},
             variables={"subject": "research report"})
    assert any(e.startswith("output 'weakest_sentence' is missing") for e in info.value.errors)


def test_modes_are_required_and_bring_their_own_inputs_and_outputs(env):
    prompt = llm.load_prompt("02", env.prompts)
    with pytest.raises(llm.PromptError, match="mode must be one of"):
        llm.prompt_part(prompt)
    with pytest.raises(llm.PromptError, match="mode must be one of"):
        llm.prompt_part(prompt, mode="nonsense")
    report = llm.prompt_part(prompt, mode="report")
    refresh = llm.prompt_part(prompt, mode="valuation_refresh")
    assert [n for n, _ in report.outputs] == ["report", "questions"]
    assert refresh.inputs == (("run_date", True), ("dossier", True), ("valuation_input_notes", True))
    assert [n for n, _ in refresh.outputs] == ["valuation_md", "valuation_yml", "questions"]
    with pytest.raises(llm.PromptError, match="has no modes; do not pass mode"):
        llm.prompt_part(llm.load_prompt("03", env.prompts), "draft", mode="report")


def test_valuation_refresh_mode_is_checked_against_its_own_lists(env):
    client = FakeClient(make_response())
    with pytest.raises(llm.PromptError, match="required input valuation_input_notes is missing"):
        call(env, client, prompt_id="02", mode="valuation_refresh",
             inputs={"run_date": "2026-09-24", "dossier": "Dossier."}, variables=DRAFT_VARIABLES)
    valuation_md = "---\ncompany: TEST\ndoc: valuation_md\nas_of: 2026-09-24\ndoc_status: proposed\n---\nFormula.\n"
    reply = envelope(valuation_md=valuation_md, valuation_yml="company: TEST\ndoc_status: proposed\n", questions="none")
    result = call(env, FakeClient(make_response(reply)), prompt_id="02", mode="valuation_refresh",
                  inputs={"run_date": "2026-09-24", "dossier": "Dossier.", "valuation_input_notes": "No notes."},
                  variables=DRAFT_VARIABLES)
    assert set(result.outputs) == {"valuation_md", "valuation_yml", "questions"}
    assert result.mode == "valuation_refresh"


def test_output_formats_come_from_the_rules_front_matter(env):
    rules = llm.load_prompt("00", env.prompts)
    draft = llm.prompt_part(llm.load_prompt("03", env.prompts), "draft")
    assert llm.output_formats(rules, draft) == {
        "update": "markdown", "thesis": "yaml", "questions": "yaml", "escalation": "yaml", "story": "markdown",
    }
    overridden = llm.PromptPart("03", "draft", "03-draft", "company_manager", (), draft.outputs,
                                formats={"questions": "text"})
    assert llm.output_formats(rules, overridden)["questions"] == "text"


def test_undeclared_or_inconsistent_formats_are_refused_before_the_call(env):
    rules_path = env.prompts / "00-series-rules.md"
    original = rules_path.read_text(encoding="utf-8")
    rules_path.write_text(original.replace("escalation, ", ""), encoding="utf-8")
    client = FakeClient(make_response())
    with pytest.raises(llm.PromptError, match="outputs escalation of 03-draft have no declared format"):
        call(env, client)
    rules_path.write_text(original.replace("[thesis, ", "[").replace("text: [", "text: [thesis, "), encoding="utf-8")
    with pytest.raises(llm.PromptError,
                       match=re.escape("thesis has a thesis-ci schema (thesis), so its format should be yaml, not text")):
        call(env, client)
    assert client.call_count == 0


def test_only_parts_that_lay_out_pages_load_00d(env):
    reply = envelope(fact_verdicts="none", findings="none", questions="none")
    client = FakeClient(make_response(reply, model="claude-fable-5-1"))
    result = call(env, client, role="auditor", prompt_id="12", part="A", inputs={"fact_table": "x", "sources": "y"})
    assert len(only_request(client)["system"]) == 2  # 12A lays out no pages: 00 + 12, no 00D
    assert result.design_version is None
    assert llm.prompt_part(llm.load_prompt("12", env.prompts), "C").design


def test_part_of_another_role_is_refused(env):
    client = FakeClient(make_response())
    with pytest.raises(llm.PromptError, match="the role of 04A is auditor"):
        call(env, client, role="red_team", prompt_id="04", part="A", inputs={"fact_table": "x", "sources": "y"},
             variables={"subject": "research report"})
    with pytest.raises(llm.PromptError, match="the role of 04B-lite is red_team"):
        call(env, client, role="auditor", prompt_id="04", part="B_lite", inputs={"update": "x", "filings": "y"},
             variables={"subject": "research report"})
    assert client.call_count == 0


def test_part_not_registered_in_agents_yml_is_refused(env):
    write_agent(env.repo, "design_reviewer", model="claude-fable-5-1", prompts=["12C"],
                can_see=["document", "rendered_pages"], cannot_see=[])
    with pytest.raises(llm.PromptError, match="registers neither 09C nor the whole of 09"):
        call(env, FakeClient(make_response()), role="design_reviewer", prompt_id="09", part="C",
             inputs={"document": "Body text."}, images={"rendered_pages": [env.page_png]},
             variables={"document": "07"})


def test_pipeline_parts_and_file_outputs_are_not_model_calls(env):
    with pytest.raises(llm.PromptError, match="role: pipeline"):
        llm.prompt_part(llm.load_prompt("12", env.prompts), "V")
    client = FakeClient(make_response())
    with pytest.raises(llm.PromptError, match="has to hand over files"):
        call(env, client, role="typesetter", prompt_id="19", inputs={"content": "Body text."})
    assert client.call_count == 0


# ---------------------------------------------------------------- Inputs: envelope and per-role filtering (00 §G6)


def test_inputs_are_input_blocks_in_front_matter_order(env):
    client = FakeClient(make_response())
    call(env, client, inputs={"thesis": "Thesis summary.", "run_date": "2026-09-24",
                              "filings": "Revenue grew year on year."})
    content = only_request(client)["messages"][0]["content"]
    assert content.startswith('<run prompt="03" part="draft" label="03-draft"/>')
    positions = [content.index(f'<input name="{name}">') for name in ("run_date", "filings", "thesis")]
    assert positions == sorted(positions)  # front matter order, not alphabetical order
    assert '<input name="filings">\nRevenue grew year on year.\n</input>' in content


def test_missing_required_or_undeclared_inputs_are_refused(env):
    client = FakeClient(make_response())
    with pytest.raises(llm.PromptError, match="required input thesis is missing"):
        call(env, client, inputs={"run_date": "2026-09-24", "filings": "x"})
    with pytest.raises(llm.PromptError, match="dossier is not an input of 03-draft"):
        call(env, client, inputs={**DRAFT_INPUTS, "dossier": "Dossier."})
    call(env, client, inputs={**DRAFT_INPUTS, "owner_notes": "A lead."})  # an optional input may be given
    assert client.call_count == 1


def test_input_outside_can_see_is_refused(env):
    write_agent(env.repo, "auditor", model="claude-fable-5-1", prompts=["04A"], can_see=["fact_table"],
                cannot_see=["product", "dossier"])
    client = FakeClient(make_response())
    with pytest.raises(llm.InputRefused, match="sources is not in can_see"):
        audit(env, client)
    assert client.call_count == 0


def test_input_in_cannot_see_is_refused_even_if_declared(env):
    write_agent(env.repo, "auditor", model="claude-fable-5-1", prompts=["04A"], can_see=["fact_table", "sources"],
                cannot_see=["sources"])
    client = FakeClient(make_response())
    with pytest.raises(llm.InputRefused, match=re.escape("sources is in cannot_see (sources)")):
        audit(env, client)
    assert client.call_count == 0


def test_cannot_see_matches_inputs_without_their_part_suffix(env):
    # findings_04A is compared as findings, consistent with thesis-ci's C-PROMPT-ISOLATION
    write_agent(env.repo, "company_manager", model="claude-sonnet-5", prompts=["03"],
                can_see=["draft_outputs", "findings_04A"], cannot_see=["findings"])
    client = FakeClient(make_response())
    with pytest.raises(llm.InputRefused, match=re.escape("findings_04A is in cannot_see (findings)")):
        call(env, client, part="revise", inputs={"draft_outputs": "The draft.", "findings_04A": "The audit findings."})
    assert client.call_count == 0


def test_bad_inputs_are_rejected(env):
    common = dict(client=FakeClient(), log_path=env.log, repo_root=env.repo, prompts_dir=env.prompts, part="draft")
    with pytest.raises(ValueError, match="inputs must be a non-empty"):
        llm.complete("company_manager", "03", {}, **common)
    with pytest.raises(TypeError, match="input 'n' must be a string"):
        llm.complete("company_manager", "03", {"n": 1}, **common)


# ---------------------------------------------------------------- Page images


def layout_review(env, client, pages, **kwargs):
    return call(env, client, role="design_reviewer", prompt_id="09", part="C", inputs={"document": "Body text."},
                images={"rendered_pages": pages}, variables={"document": "07"}, **kwargs)


def test_page_images_go_in_as_base64_image_blocks(env):
    reply = envelope(design_checks="none", layout_instructions="none", findings="none", questions="none")
    client = FakeClient(make_response(reply, model="claude-fable-5-1"))
    result = layout_review(env, client, [env.page_png, env.page_jpg])
    content = only_request(client)["messages"][0]["content"]
    images = [block for block in content if block["type"] == "image"]
    assert [block["source"]["media_type"] for block in images] == ["image/png", "image/jpeg"]
    assert images[0]["source"] == {
        "type": "base64",
        "media_type": "image/png",
        "data": base64.standard_b64encode(env.page_png.read_bytes()).decode("ascii"),
    }
    text = "".join(block["text"] for block in content if block["type"] == "text")
    assert '<input name="document">\nBody text.\n</input>' in text
    assert '<input name="rendered_pages">' in text and '<page n="2" file="page-2.jpg"/>' in text
    assert text.index('<input name="document">') < text.index('<input name="rendered_pages">')
    assert result.design_version == "9.1"  # 09 lays out pages: 00D is loaded
    (record,) = read_log(env.log)
    assert record["images"] == 2


def test_image_content_is_part_of_the_input_hash(env):
    reply = envelope(design_checks="none", layout_instructions="none", findings="none", questions="none")
    first = layout_review(env, FakeClient(make_response(reply)), [env.page_png])
    env.page_png.write_bytes(env.page_png.read_bytes() + b"\x00")
    second = layout_review(env, FakeClient(make_response(reply)), [env.page_png])
    assert first.input_sha256 != second.input_sha256


def test_bad_page_images_are_refused(env, tmp_path):
    client = FakeClient(make_response())
    with pytest.raises(ValueError, match="page.bmp: page images must be one of"):
        layout_review(env, client, [tmp_path / "page.bmp"])
    with pytest.raises(ValueError, match="rendered_pages holds page images; pass it as images="):
        call(env, client, role="design_reviewer", prompt_id="09", part="C",
             inputs={"document": "Body text.", "rendered_pages": "not an image"}, variables={"document": "07"})
    with pytest.raises(ValueError, match="only rendered_pages is passed as images, got 'document'"):
        call(env, client, images={"document": [env.page_png]})
    assert client.call_count == 0


# ---------------------------------------------------------------- Request shape: model, thinking, streaming


def test_drafting_role_streams_with_adaptive_thinking(env):
    client = FakeClient(make_response())
    call(env, client, role="company_manager")
    assert client.beta.messages.stream_calls == [] and client.messages.calls == []
    (kwargs,) = client.messages.stream_calls
    assert kwargs["model"] == "claude-sonnet-5"
    assert kwargs["max_tokens"] == llm.STREAM_MAX_TOKENS == 64000
    assert kwargs["messages"][0]["role"] == "user"
    assert '<input name="filings">' in kwargs["messages"][0]["content"]
    assert kwargs["output_config"] == {"effort": "high"}
    assert kwargs["thinking"] == {"type": "adaptive"}
    assert "betas" not in kwargs and "fallbacks" not in kwargs
    assert "budget_tokens" not in json.dumps(kwargs, ensure_ascii=False)


def test_long_output_prompts_stream_with_a_larger_limit(env):
    client = FakeClient(make_response(envelope(report=REPORT_MD, questions="none")))
    call(env, client, prompt_id="02", mode="report", inputs={"run_date": "2026-09-24", "dossier": "Dossier."},
         variables=DRAFT_VARIABLES)
    (kwargs,) = client.messages.stream_calls
    assert kwargs["max_tokens"] == llm.LONG_MAX_TOKENS == 128000


def test_non_streaming_call_uses_messages_create(env):
    client = FakeClient(make_response())
    call(env, client, stream=False)
    assert client.messages.stream_calls == []
    (kwargs,) = client.messages.calls
    assert kwargs["max_tokens"] == llm.MAX_TOKENS == 16000
    (record,) = read_log(env.log)
    assert record["stream"] is False


def test_fable_uses_beta_with_default_server_side_fallbacks(env):
    auditor = FakeClient(make_response(envelope(fact_verdicts="none", findings="none", questions="none"),
                                       model="claude-fable-5-1"))
    audit(env, auditor)
    red_team = FakeClient(make_response(envelope(inversion_list="none", test_proposals="none"), model="claude-fable-5-1"))
    call(env, red_team, role="red_team", prompt_id="04", part="B_lite",
         inputs={"update": "The draft.", "filings": "The filings."}, variables={"subject": "quarterly update"})
    for client in (auditor, red_team):
        assert client.messages.calls == [] and client.messages.stream_calls == []
        (kwargs,) = client.beta.messages.stream_calls
        assert kwargs["model"] == "claude-fable-5-1"
        assert kwargs["betas"] == ["server-side-fallback-2026-07-01"]
        assert kwargs["fallbacks"] == "default"
        assert kwargs["output_config"] == {"effort": "high"}
        assert kwargs["thinking"] == {"type": "adaptive"}
        assert "budget_tokens" not in json.dumps(kwargs, ensure_ascii=False)
    assert {r["fallbacks"] for r in read_log(env.log)} == {"default"}


def test_fallback_served_response_is_priced_at_served_model(env):
    response = make_response(
        envelope(fact_verdicts="none", findings="none", questions="none"),
        model="claude-opus-4-8",
        input_tokens=1000,
        output_tokens=1000,
        iterations=[SimpleNamespace(type="message"), SimpleNamespace(type="fallback_message")],
    )
    result = audit(env, FakeClient(response))
    assert result.requested_model == "claude-fable-5-1"
    assert result.model == "claude-opus-4-8"
    assert result.served_by_fallback is True
    assert result.cost_usd == pytest.approx(0.03)  # 1000 × 5 / 1e6 + 1000 × 25 / 1e6
    assert result.generated_by["model"] == "claude-opus-4-8"


def test_agent_yaml_overrides_defaults(env):
    write_agent(env.repo, "company_manager", model="claude-opus-5", effort="medium", fallbacks="none")
    client = FakeClient(make_response(model="claude-opus-5"))
    result = call(env, client)
    (kwargs,) = client.messages.stream_calls
    assert kwargs["model"] == "claude-opus-5"
    assert kwargs["output_config"] == {"effort": "medium"}
    assert result.fallbacks is None


def test_named_fallback_uses_array_form(env):
    write_agent(env.repo, "auditor", model="claude-fable-5-1", fallbacks="claude-opus-4-8", prompts=["04A"],
                can_see=["fact_table", "sources"], cannot_see=[])
    client = FakeClient(make_response(envelope(fact_verdicts="none", findings="none", questions="none"),
                                      model="claude-fable-5-1"))
    audit(env, client)
    (kwargs,) = client.beta.messages.stream_calls
    assert kwargs["betas"] == ["server-side-fallback-2026-06-01"]
    assert kwargs["fallbacks"] == [{"model": "claude-opus-4-8"}]


def test_model_without_effort_support_gets_no_output_config(env):
    client = FakeClient(make_response(model="claude-haiku-4-5"))
    call(env, client, model="claude-haiku-4-5")
    (kwargs,) = client.messages.stream_calls
    assert "output_config" not in kwargs
    assert "thinking" not in kwargs  # haiku does not support adaptive thinking; budget_tokens is not sent either


# ---------------------------------------------------------------- Prompt caching


def test_rules_block_is_a_cache_breakpoint_and_the_prompt_comes_after_it(env):
    client = FakeClient(make_response())
    call(env, client)
    rules, prompt = only_request(client)["system"]
    assert rules["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in prompt


def test_design_system_block_is_cached_too(env):
    client = FakeClient(make_response(envelope(report=REPORT_MD, questions="none")))
    call(env, client, prompt_id="02", mode="report", inputs={"run_date": "2026-09-24", "dossier": "Dossier."},
         variables=DRAFT_VARIABLES)
    rules, design, prompt = only_request(client)["system"]
    assert rules["cache_control"] == design["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in prompt


def test_cache_tokens_are_priced_at_their_own_rates_and_logged(env):
    response = make_response(input_tokens=1000, output_tokens=500,
                             cache_creation_input_tokens=10_000, cache_read_input_tokens=20_000)
    result = call(env, FakeClient(response))
    (record,) = read_log(env.log)
    assert record["usage"]["cache_creation_input_tokens"] == 10_000
    assert record["usage"]["cache_read_input_tokens"] == 20_000
    # claude-sonnet-5: input 2, output 10, cache write 2.5, cache read 0.2 (USD per million tokens)
    assert record["cost_breakdown"] == {"input": 0.002, "output": 0.005, "cache_write": 0.025, "cache_read": 0.004}
    assert record["cost_usd"] == result.cost_usd == pytest.approx(0.036)


def test_fable_cache_reads_use_their_own_listed_rate():
    usage = {"cache_creation_input_tokens": 1_000_000, "cache_read_input_tokens": 1_000_000}
    assert llm.cost_breakdown("claude-fable-5-1", usage) == {
        "input": 0.0, "output": 0.0, "cache_write": 12.5, "cache_read": 0.25,
    }


def test_opus_5_5_prices():
    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000,
             "cache_creation_input_tokens": 1_000_000, "cache_read_input_tokens": 1_000_000}
    assert llm.cost_breakdown("claude-opus-5-5", usage) == {
        "input": 4.0, "output": 20.0, "cache_write": 5.0, "cache_read": 0.2}
    assert "claude-opus-5-5" in llm.ADAPTIVE_THINKING_MODELS and "claude-opus-5-5" in llm.SERVER_FALLBACK_MODELS


def test_cache_price_table_covers_every_model_and_follows_the_documented_multipliers():
    assert set(llm.CACHE_PRICES_PER_MTOK) == set(llm.PRICES_PER_MTOK)
    for model, (write, read) in llm.CACHE_PRICES_PER_MTOK.items():
        input_price = llm.PRICES_PER_MTOK[model][0]
        assert write == pytest.approx(input_price * llm.CACHE_WRITE_MULTIPLIER), model
        if model not in ("claude-fable-5-1", "claude-opus-5-5"):  # separately listed read prices: 0.25 and 0.20
            assert read == pytest.approx(input_price * llm.CACHE_READ_MULTIPLIER), model


def test_model_without_cache_rates_is_refused(env, monkeypatch):
    monkeypatch.delitem(llm.CACHE_PRICES_PER_MTOK, "claude-sonnet-5")
    client = FakeClient(make_response())
    with pytest.raises(ValueError, match="model claude-sonnet-5 is not in the price table.*CACHE_PRICES_PER_MTOK"):
        call(env, client)
    assert client.call_count == 0


# ---------------------------------------------------------------- Outputs: parsing, validation and retry


def test_outputs_are_parsed_by_name(env):
    result = call(env, FakeClient(make_response()))
    assert set(result.outputs) == {"update", "thesis", "questions"}
    assert result.outputs["questions"].empty is True  # "none"
    assert result.outputs["thesis"].data == {"company": "TEST", "trust_level": 1, "tests": []}
    assert result.attempts == 1


@pytest.mark.parametrize("mark", ["none", "None", "NONE"])
def test_empty_marker_is_accepted_in_any_letter_case(env, mark):
    reply = envelope(update=UPDATE_MD, thesis=THESIS_YML, questions=mark)
    client = FakeClient(make_response(reply))
    result = call(env, client)
    assert client.call_count == 1 and result.attempts == 1  # accepted at once, no retry
    assert result.outputs["questions"].empty is True
    assert result.outputs["questions"].text == ""
    assert "questions" not in {p.output for p in result.placements()}


def test_invalid_reply_is_retried_once_with_the_errors(env):
    bad = make_response(envelope(update=UPDATE_MD, thesis=THESIS_YML, surprise="extra"), input_tokens=100,
                        output_tokens=10)
    good = make_response(input_tokens=100, output_tokens=10)
    client = FakeClient(bad, good)
    result = call(env, client)
    assert result.attempts == 2
    first, second = client.messages.stream_calls
    assert first["messages"][0]["content"] == second["messages"][0]["content"].split("\n\n<validation_errors>")[0]
    retry_note = second["messages"][0]["content"]
    assert "<validation_errors>" in retry_note
    assert "- output 'surprise' is not an output of 03-draft (its outputs: " in retry_note
    assert "- output 'questions' is missing" in retry_note
    assert first["system"] == second["system"]
    records = read_log(env.log)
    assert [r["attempt"] for r in records] == [1, 2]
    assert records[0]["validation_errors"] and "validation_errors" not in records[1]
    assert records[0]["input_sha256"] == records[1]["input_sha256"]
    assert result.cost_usd == pytest.approx(records[0]["cost_usd"] + records[1]["cost_usd"])
    assert result.usage["input_tokens"] == 200


def test_retry_note_is_english_and_lists_the_errors(env):
    bad = make_response(envelope(update=UPDATE_MD, thesis=THESIS_YML))  # questions is missing
    client = FakeClient(bad, make_response())
    call(env, client)
    first, second = client.messages.stream_calls
    original = first["messages"][0]["content"]
    retried = second["messages"][0]["content"]
    assert retried.startswith(original)
    assert retried[len(original):] == (
        "\n\n<validation_errors>\n"
        "An earlier attempt at this part failed the pipeline's validation (00 §F0, §F6) with the errors below. "
        "That attempt is not shown here. The outputs update, thesis passed and are kept as they were: do not produce "
        "them again. Produce only questions again, complete and as originally asked, and avoid these errors:\n"
        "- output 'questions' is missing; with no content, write \"none\" instead of leaving it out (00 §F0)\n"
        "</validation_errors>"
    )


def test_a_retry_produces_only_the_outputs_that_failed_and_keeps_the_rest(env):
    """MCD's 01A (2026-09-28): a full retry rewrote a dossier that had passed and broke it; only the failed outputs
    are produced again, and the ones that passed stand as they were."""
    bad = make_response(envelope(update=UPDATE_MD, thesis=THESIS_YML, questions="- oops: [unclosed"))
    retry = make_response(envelope(questions="none", update="---\nbroken: [\n---\nchanged\n"))  # update resent: ignored
    result = call(env, FakeClient(bad, retry))
    assert result.attempts == 2 and result.outputs["questions"].empty
    assert "changed" not in result.outputs["update"].text  # the update that passed stands; the resent one is ignored
    assert llm.RETRY_MARK.format("questions") in result.text


def test_an_error_that_names_no_output_retries_every_output():
    assert llm.failed_outputs(["the reply has no <output> blocks (00 §F0)"], ["update", "questions"]) is None
    assert llm.failed_outputs(["questions: the YAML does not parse: line 2"], ["update", "questions"]) == {"questions"}


def test_second_invalid_reply_raises_with_the_errors(env):
    bad = make_response(envelope(update=UPDATE_MD, thesis="```yaml\ncompany: TEST\n```", questions="none"))
    client = FakeClient(bad)
    with pytest.raises(llm.LLMOutputInvalid) as info:
        call(env, client)
    assert client.call_count == 2
    assert "thesis: YAML goes without code fences (00 §F0)" in info.value.errors
    assert "the output of 03-draft is still invalid after the retry; no output was handed over" in str(info.value)
    assert info.value.result.attempts == 2
    assert len(read_log(env.log)) == 2


def test_structured_outputs_are_validated_against_the_schema(env):
    thesis_without_level = "company: TEST\ntests: []\n"
    client = FakeClient(make_response(envelope(update=UPDATE_MD, thesis=thesis_without_level, questions="none")))
    with pytest.raises(llm.LLMOutputInvalid) as info:
        call(env, client)
    assert "thesis: thesis.schema.json /: 'trust_level' is a required property" in info.value.errors


def test_markdown_outputs_need_front_matter(env):
    client = FakeClient(make_response(envelope(update="Conclusion: maintain.", thesis=THESIS_YML, questions="none")))
    with pytest.raises(llm.LLMOutputInvalid) as info:
        call(env, client)
    assert any(e.startswith("update: a Markdown output starts with front matter") for e in info.value.errors)


def test_pipeline_fields_are_written_before_validation(env):
    thesis = "# comment kept\ncompany: TEST\ntests: []\n"
    client = FakeClient(make_response(envelope(update=UPDATE_MD, thesis=thesis, questions="none")))
    result = call(env, client, pipeline_fields={"thesis": {"trust_level": 2, "company": "TEST"}})
    parsed = result.outputs["thesis"]
    assert parsed.data["trust_level"] == 2
    assert parsed.text.startswith("# comment kept\ntrust_level: 2\n")
    assert parsed.pipeline_fields == ("trust_level", "company")


def test_schema_is_resolved_before_any_money_is_spent(env, tmp_path):
    client = FakeClient(make_response())
    with pytest.raises(llm.LLMError, match="the outputs of 03-draft are validated against thesis-ci schemas"):
        call(env, client, schemas_dir=tmp_path / "no-schemas")
    assert client.call_count == 0


# ---------------------------------------------------------------- generated_by and placement


def test_generated_by_is_injected_into_markdown_front_matter(env):
    result = call(env, FakeClient(make_response()))
    update = result.outputs["update"]
    assert update.generated_by_injected
    assert update.data["generated_by"] == {
        "model": "claude-sonnet-5",
        "backend": "api",
        "rules_version": "9.0",
        "prompt": "03",
        "part": "03-draft",
        "prompt_version": "3.1",
        "input_sha256": result.input_sha256,
    }
    assert list(result.generated_by)[:3] == ["model", "backend", "rules_version"]
    assert "generated_by:\n  model: claude-sonnet-5\n  backend: api\n  rules_version: '9.0'\n" in update.text
    assert update.text.endswith("Conclusion: maintain.\n")


def test_generated_by_is_returned_alongside_when_the_schema_rejects_extra_keys(env):
    story = "---\ncompany: TEST\nas_of: 2026-09-24\nstatus: holding\n---\nTwo-minute story.\n"
    client = FakeClient(make_response(envelope(update=UPDATE_MD, thesis=THESIS_YML, questions="none", story=story)))
    result = call(env, client)
    for name, text in (("thesis", THESIS_YML), ("story", story)):
        parsed = result.outputs[name]
        assert parsed.text == text  # the document is left unchanged
        assert not parsed.generated_by_injected
        assert parsed.generated_by == result.generated_by


def test_generated_by_goes_into_schemaless_yaml_mappings(env):
    checks = "as_of: 2026-09-24\nchecks:\n  - page: 1\n    result: ok\n"
    reply = envelope(design_checks=checks, layout_instructions="none", findings=FINDING.format(id="09C-01"),
                     questions="none")
    result = layout_review(env, FakeClient(make_response(reply)), [env.page_png])
    assert result.outputs["design_checks"].generated_by_injected
    assert result.outputs["design_checks"].data["generated_by"]["part"] == "09C"
    assert result.outputs["design_checks"].data["generated_by"]["backend"] == "api"
    assert not result.outputs["findings"].generated_by_injected  # a list: returned alongside


VERDICTS = "- id: F001\n  verdict: accurate\n  source_location: Item 7, Table 1\n  correct_value: null\n"


@pytest.mark.parametrize("output, text, message", [
    ("fact_verdicts", "verdicts:\n" + VERDICTS.replace("- ", "  - ").replace("\n  ", "\n    "),
     "fact_verdicts: should be a list with one entry per item, not a mapping (04A)"),
    ("fact_verdicts", VERDICTS.replace("accurate", "basis_issue"),
     "fact_verdicts[0] (F001): verdict 'basis_issue' is not one of: L2 only, accurate, basis issue, consistent with "
     "citation, error, unconfirmed (04A)"),
    ("findings", "04A-01:\n  group: must fix\n", "findings: should be a list with one entry per item, not a mapping"),
    ("findings", FINDING.format(id="04A-01").replace("should fix", "must_fix"),
     "findings[0] (04A-01): group 'must_fix' is not one of: must fix, no change, should fix (00 §F3)"),
    ("findings", FINDING.format(id="04A-01").replace("group: should fix", "group: no change"),
     "findings[0]: a no-change finding has type null (00 §F3)"),
    ("findings", "- id: 04A-01\n  group: must fix\n", "findings[0] (04A-01): has no type, location, quote, evidence, fix"),
    ("questions", "- id: Q1\n  issue: Which basis?\n  options: [a, b]\n  interim: a\n  blocking: maybe\n",
     "questions[0] (Q1): blocking 'maybe' is not one of: False, True (00 §F8)"),
])
def test_malformed_audit_outputs_get_a_precise_error_and_one_retry(env, output, text, message):
    """Replies seen in the model comparison of 2026-09-27: YAML that parses but does not follow 04A's format is
    refused with the exact rule, and the retry carries the error (decision 0024)."""
    good = {"fact_verdicts": VERDICTS, "findings": FINDING.format(id="04A-01"), "questions": "none"}
    bad = envelope(**{**good, output: text})
    client = FakeClient(make_response(bad), make_response(envelope(**good)))
    result = audit(env, client)
    assert result.attempts == 2 and result.outputs["fact_verdicts"].data[0]["verdict"] == "accurate"
    retried = client.requests[1]["messages"][0]["content"]
    assert message in retried
    assert any(message in e for e in read_log(env.log)[0]["validation_errors"])


def test_an_audit_that_skips_a_fact_or_judges_another_is_retried_with_the_ids(env):
    """04A gives every fact under facts exactly one verdict (decisions/0026); a reply that does not is retried."""
    table = "facts:\n- id: F001\n  value: 1\n- id: F002\n  value: 2\ncontext_facts:\n- id: F009\n  value: 9\n"
    good = {"fact_verdicts": VERDICTS + VERDICTS.replace("F001", "F002"), "findings": FINDING.format(id="04A-01"),
            "questions": "none"}
    bad = envelope(**{**good, "fact_verdicts": VERDICTS + VERDICTS.replace("F001", "F009")})
    client = FakeClient(make_response(bad), make_response(envelope(**good)))
    result = call(env, client, role="auditor", prompt_id="04", part="A",
                  inputs={"fact_table": table, "sources": "Source text."}, variables={"subject": "quarterly update"})
    assert result.attempts == 2
    retried = client.requests[1]["messages"][0]["content"]
    assert "fact_verdicts: no verdict for F002" in retried and "F009 is not a fact under facts" in retried


def test_placements_follow_section_f2(env):
    result = call(env, FakeClient(make_response()))
    placed = {p.output: p for p in result.placements()}
    assert placed["thesis"].repo == "owners-office"
    assert placed["thesis"].path == "companies/TEST/thesis.yml"
    assert placed["update"].path == "companies/TEST/updates/2026-09-24.md"
    assert "§G9" in placed["thesis"].note  # quarterly updates are routed by trust level
    assert "questions" not in placed  # "none" is not placed
    audited = audit(env, FakeClient(make_response(envelope(fact_verdicts="none", findings=FINDING.format(id="04A-01"),
                                                           questions="none"))))
    (findings,) = audited.placements(company="TEST")
    assert (findings.visibility, findings.action) == ("public", "pr_attachment")


# ---------------------------------------------------------------- Backends


def test_resolve_backend_takes_the_argument_then_the_env_var_then_claude_code(monkeypatch):
    monkeypatch.setenv(llm.BACKEND_ENV, "fake")
    assert llm.resolve_backend("api") == "api"  # the argument wins
    assert llm.resolve_backend() == "fake"
    monkeypatch.setenv(llm.BACKEND_ENV, "")  # an empty variable counts as unset
    assert llm.resolve_backend() == "claude-code"
    monkeypatch.delenv(llm.BACKEND_ENV)
    assert llm.resolve_backend() == llm.DEFAULT_BACKEND == "claude-code"


def test_resolve_backend_rejects_unknown_values(monkeypatch):
    with pytest.raises(ValueError, match="^backend must be one of claude-code, api, fake, got 'sdk'$"):
        llm.resolve_backend("sdk")
    monkeypatch.setenv(llm.BACKEND_ENV, "anthropic")
    with pytest.raises(ValueError, match=f"^{llm.BACKEND_ENV} must be one of claude-code, api, fake, got 'anthropic'$"):
        llm.resolve_backend()


def test_default_backend_comes_from_the_env_var(env, monkeypatch):
    monkeypatch.setenv(llm.BACKEND_ENV, "fake")
    result = audit(env, None)  # no client and no backend argument: the fake client answers
    assert result.backend == "fake"
    (record,) = read_log(env.log)
    assert record["backend"] == "fake"


def test_fake_backend_without_a_client_answers_with_placeholders(env):
    # 04A's placeholders keep the shape 03R reads: a list of verdicts and a list of findings (00 §F3)
    result = audit(env, None, backend="fake")
    assert result.backend == "fake"
    assert result.attempts == 1
    assert set(result.outputs) == {"fact_verdicts", "findings", "questions"}
    assert fake_client.DRY_RUN_NOTE in result.text
    assert result.outputs["fact_verdicts"].data[0]["verdict"] == "unconfirmed"
    assert result.outputs["findings"].data[0]["group"] == "no change"
    assert result.generated_by["backend"] == "fake"
    assert result.notional_cost_usd is None
    (record,) = read_log(env.log)
    assert record["backend"] == "fake"
    assert record["request_id"] == fake_client.REQUEST_ID
    assert record["model"] == "claude-fable-5-1"
    assert record["cost_usd"] > 0  # the fake client's token estimate, priced like the API ...
    assert llm.month_spend(env.log) == 0.0  # ... but it is not API spend


def test_fake_backend_placeholders_do_not_satisfy_a_strict_schema(env):
    # 03-draft's thesis has a schema that rejects extra keys; the generic placeholder fails it on both attempts
    with pytest.raises(llm.LLMOutputInvalid) as info:
        call(env, None, backend="fake")
    assert any(e.startswith("thesis: thesis.schema.json") for e in info.value.errors)
    assert [r["backend"] for r in read_log(env.log)] == ["fake", "fake"]


def test_claude_code_backend_refuses_an_injected_client(env):
    client = FakeClient(make_response())
    with pytest.raises(ValueError, match="client is for the api and fake backends"):
        call(env, client, backend="claude-code")
    assert client.call_count == 0
    assert not env.log.exists()  # refused before anything was logged


# ---------------------------------------------------------------- Lazy SDK import


def test_importing_module_does_not_import_sdk():
    code = "import sys; from pipeline import llm, outputs, isolation; print(llm.SDK_MODULE in sys.modules)"
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(llm.__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert out == "False"


def test_missing_sdk_gives_clear_error(env, monkeypatch):
    monkeypatch.setitem(sys.modules, llm.SDK_MODULE, None)  # simulate the SDK not being installed
    with pytest.raises(llm.LLMError, match="pip install"):
        call(env, None)


REPORT_MD = "---\ncompany: TEST\ndoc: report_02\nas_of: 2026-09-24\ndoc_status: draft\n---\n## 1. Conclusion\nBody text.\n"
