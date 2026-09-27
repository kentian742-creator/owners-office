"""Tests of the claude-code backend of pipeline/llm.py (docs/decisions/0022).

A fake `claude` executable stands in for the Claude Code CLI: it records its arguments, working directory,
environment, stdin and system prompt file, and answers with a JSON result shaped like the CLI's (`--output-format
json`, or stream-json for requests with page images). Nothing touches the network, a model or the owner's login.
"""

from __future__ import annotations

import io
import json
import os
import stat
import sys
from pathlib import Path

import pytest
import yaml

from pipeline import llm, runner
from tests import runner_fixtures as rfx
from tests.llm_fixtures import (
    DRAFT_REPLY,
    DRAFT_VARIABLES,
    UPDATE_MD,
    FakeClient,
    envelope,
    make_env,
    make_response,
    write_agent,
)

DRAFT_INPUTS = {"run_date": "2026-09-24", "filings": "Revenue grew year on year.", "thesis": "Thesis summary."}
FAKE_SCRIPT = r'''
import json, os, sys, time
from pathlib import Path

HERE = Path(__file__).parent
args = sys.argv[1:]
if args == ["--version"]:
    print("9.9.9 (Claude Code)")
    sys.exit(0)
stdin = sys.stdin.read()
system_file = Path(args[args.index("--system-prompt-file") + 1])
plan = json.loads((HERE / "plan.json").read_text())
step = plan.pop(0) if len(plan) > 1 else plan[0]
(HERE / "plan.json").write_text(json.dumps(plan))
record = {"args": args, "cwd": os.getcwd(), "cwd_entries": sorted(os.listdir(".")), "env": dict(os.environ),
          "stdin": stdin, "system_prompt": system_file.read_text(encoding="utf-8"),
          "system_file": str(system_file), "config_dir_exists": os.path.isdir(os.environ.get("CLAUDE_CONFIG_DIR", "-"))}
with (HERE / "calls.jsonl").open("a") as handle:
    handle.write(json.dumps(record) + "\n")
if step.get("sleep"):
    time.sleep(step["sleep"])
if "raw" in step:
    sys.stdout.write(step["raw"])
    sys.exit(step.get("exit", 1))
model = args[args.index("--model") + 1]
served = step.get("served_model", model)
usage = {"input_tokens": 1200, "output_tokens": 300, "cache_creation_input_tokens": 50, "cache_read_input_tokens": 7000,
         "server_tool_use": {"web_search_requests": 0}}
result = {"type": "result", "subtype": "success", "is_error": step.get("is_error", False),
          "api_error_status": step.get("api_error_status"), "result": step.get("result", ""),
          "stop_reason": step.get("stop_reason", "end_turn"), "session_id": "fake-session",
          "num_turns": step.get("num_turns", 1), "total_cost_usd": 0.0421, "duration_ms": 12,
          "terminal_reason": "completed", "permission_denials": step.get("permission_denials", []), "usage": usage,
          "modelUsage": {served: {"inputTokens": 1200, "outputTokens": 300, "cacheReadInputTokens": 7000,
                                  "cacheCreationInputTokens": 50, "costUSD": 0.0421, "contextWindow": 1000000}}}
if "--input-format" in args:
    init = {"type": "system", "subtype": "init", "tools": [], "mcp_servers": [], "skills": [], "plugins": [],
            "slash_commands": [], "model": model}
    init.update(step.get("init", {}))
    print(json.dumps(init))
    print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": result["result"]}]}}))
    print(json.dumps(result))
else:
    print(json.dumps(result))
sys.exit(step.get("exit", 1 if result["is_error"] else 0))
'''


class FakeCLI:
    """The fake `claude` executable in its own directory, with a plan of answers and a record of the calls."""

    def __init__(self, directory: Path):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "fake_claude.py").write_text(FAKE_SCRIPT, encoding="utf-8")
        self.path = self.dir / "claude"
        self.path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{self.dir / "fake_claude.py"}" "$@"\n',
                             encoding="utf-8")
        self.path.chmod(self.path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        self.answer(result=DRAFT_REPLY)

    def answer(self, *steps: dict | None, **step) -> None:
        plan = [s for s in steps if s is not None] or [step]
        (self.dir / "plan.json").write_text(json.dumps(plan), encoding="utf-8")

    @property
    def calls(self) -> list[dict]:
        path = self.dir / "calls.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.is_file() else []


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv(llm.PROMPTS_ENV, raising=False)
    monkeypatch.delenv(llm.CLAUDE_TIMEOUT_ENV, raising=False)
    monkeypatch.delenv(llm.OAUTH_TOKEN_ENV, raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    # Never read the owner's real workspace .env (it may hold the subscription token): point at a missing file.
    monkeypatch.setenv("OWNERS_OFFICE_ENV_FILE", str(tmp_path / "no-such.env"))
    monkeypatch.setenv(llm.BACKEND_ENV, "claude-code")
    return make_env(tmp_path)


@pytest.fixture
def cli(tmp_path, monkeypatch):
    fake = FakeCLI(tmp_path / "fake-cli")
    monkeypatch.setenv(llm.CLAUDE_BIN_ENV, str(fake.path))
    llm._CLI_VERSIONS.clear()
    return fake


def call(env, role="company_manager", prompt_id="03", inputs=None, **kwargs):
    kwargs.setdefault("budget_usd", 20.0)
    kwargs.setdefault("schemas_dir", env.schemas)
    if prompt_id == "03":
        kwargs.setdefault("part", "draft")
        kwargs.setdefault("variables", DRAFT_VARIABLES)
    return llm.complete(role, prompt_id, DRAFT_INPUTS if inputs is None else inputs, log_path=env.log,
                        repo_root=env.repo, prompts_dir=env.prompts, **kwargs)


def log_lines(env) -> list[dict]:
    return [json.loads(line) for line in env.log.read_text(encoding="utf-8").splitlines()]


# ---------------------------------------------------------------- the request


def test_the_cli_runs_in_print_mode_with_nothing_but_the_prompt(env, cli):
    result = call(env)
    assert result.backend == "claude-code" and result.outputs["update"].data["company"] == "TEST"
    (seen,) = cli.calls
    args = seen["args"]
    assert args[:3] == ["--print", "--output-format", "json"]
    assert args[args.index("--model") + 1] == "claude-sonnet-5" and args[args.index("--effort") + 1] == "high"
    assert args[args.index("--tools") + 1] == "" and args[args.index("--setting-sources") + 1] == ""
    for flag in ("--strict-mcp-config", "--no-session-persistence", "--disable-slash-commands"):
        assert flag in args
    assert "--bare" not in args and "--mcp-config" not in args and "--continue" not in args
    assert seen["cwd_entries"] == [] and not Path(seen["cwd"]).exists()  # an empty temporary directory, removed
    assert Path(seen["system_file"]).parent != Path(seen["cwd"])


def test_system_prompt_and_user_content_are_exactly_what_the_api_path_sends(env, cli):
    call(env)
    client = FakeClient(make_response())
    call(env, backend="api", client=client)
    (request,) = client.requests
    (seen,) = cli.calls
    assert seen["system_prompt"] == "\n\n".join(block["text"] for block in request["system"])
    assert seen["stdin"] == request["messages"][0]["content"]
    assert "{{company}}" not in seen["system_prompt"] and "Test Co (TEST), FY2026Q3" in seen["system_prompt"]
    api_line, cli_line = log_lines(env)[1], log_lines(env)[0]
    assert cli_line["input_sha256"] == api_line["input_sha256"]


def test_the_environment_is_built_from_scratch(env, cli, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("SOME_HOST_SETTING", "leak")
    call(env)
    seen_env = cli.calls[0]["env"]
    assert "ANTHROPIC_API_KEY" not in seen_env and "ANTHROPIC_BASE_URL" not in seen_env
    assert "SOME_HOST_SETTING" not in seen_env and "CLAUDE_CONFIG_DIR" not in seen_env
    assert {k: seen_env[k] for k in llm.CLAUDE_ENV_FIXED} == llm.CLAUDE_ENV_FIXED
    assert seen_env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "128000" and seen_env["HOME"] == os.environ["HOME"]


def test_a_long_lived_token_gets_an_empty_configuration_directory(env, cli, monkeypatch):
    monkeypatch.setenv(llm.OAUTH_TOKEN_ENV, "test-token")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/owner/config")
    call(env)
    (seen,) = cli.calls
    assert seen["env"][llm.OAUTH_TOKEN_ENV] == "test-token" and seen["config_dir_exists"]
    assert seen["env"]["CLAUDE_CONFIG_DIR"] != "/owner/config" and not Path(seen["env"]["CLAUDE_CONFIG_DIR"]).exists()
    assert log_lines(env)[0]["cli_isolated_config"] is True


def test_every_part_gets_the_models_full_output_ceiling(env, cli):
    """The CLI's limit covers thinking and the reply: a short output still needs room to think (decision 0024)."""
    cli.answer(result=envelope(report="---\ncompany: TEST\ndoc: report_02\nas_of: 2026-09-24\ndoc_status: draft\n---\n"
                                      "Report.\n", questions="none"))
    call(env, prompt_id="02", mode="report", inputs={"run_date": "2026-09-24", "dossier": "Dossier."},
         variables=DRAFT_VARIABLES)
    cli.answer(result=DRAFT_REPLY)
    call(env)
    assert [c["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] for c in cli.calls] == ["128000", "128000"]
    write_agent(env.repo, "company_manager", model="claude-haiku-4-5")
    call(env)
    assert cli.calls[-1]["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "64000"


def test_models_without_effort_get_no_effort_flag(env, cli):
    write_agent(env.repo, "company_manager", model="claude-haiku-4-5")
    call(env)
    assert "--effort" not in cli.calls[0]["args"]


# ---------------------------------------------------------------- the log and the budget


def test_claude_code_calls_are_logged_like_api_calls_at_no_api_cost(env, cli):
    result = call(env)
    (line,) = log_lines(env)
    assert line["backend"] == "claude-code" and line["model"] == "claude-sonnet-5"
    assert line["cost_usd"] == 0.0 and line["cost_breakdown"] == {"input": 0.0, "output": 0.0, "cache_write": 0.0,
                                                                  "cache_read": 0.0}
    assert line["notional_cost_usd"] == 0.0421 and result.notional_cost_usd == 0.0421 and result.cost_usd == 0.0
    assert line["usage"] == {"input_tokens": 1200, "output_tokens": 300, "cache_creation_input_tokens": 50,
                             "cache_read_input_tokens": 7000}
    assert (line["session_id"], line["cli_version"], line["num_turns"]) == ("fake-session", "9.9.9", 1)
    for key in ("prompt_version", "prompt_revision", "rules_version", "rules_revision", "input_sha256", "part_id"):
        assert line[key]
    assert line["fallbacks"] is None and line["stream"] is None and line["max_tokens"] == 128000
    assert result.generated_by["backend"] == "claude-code"
    assert llm.month_spend(env.log) == 0.0


def test_the_api_budget_neither_counts_nor_stops_claude_code_calls(env, cli):
    stamp = llm._utcnow().isoformat(timespec="seconds")
    env.log.parent.mkdir(parents=True, exist_ok=True)
    env.log.write_text(json.dumps({"timestamp": stamp, "cost_usd": 25.0}) + "\n", encoding="utf-8")
    assert call(env, budget_usd=20.0).outputs
    with pytest.raises(llm.BudgetExceeded):
        call(env, budget_usd=20.0, backend="api", client=FakeClient(make_response()))
    assert llm.month_spend(env.log) == 25.0


def test_an_invalid_reply_is_retried_once_with_the_errors_in_english(env, cli):
    bad = envelope(update=UPDATE_MD, thesis="company: TEST\ntrust_level: high\ntests: []\n", questions="none")
    cli.answer({"result": bad}, {"result": DRAFT_REPLY})
    result = call(env)
    first, second = cli.calls
    assert result.attempts == 2 and "<validation_errors>" not in first["stdin"]
    assert second["stdin"].startswith(first["stdin"]) and "failed the pipeline's validation" in second["stdin"]
    assert "trust_level: 'high' is not of type 'integer'" in second["stdin"]
    assert [line["attempt"] for line in log_lines(env)] == [1, 2]


# ---------------------------------------------------------------- failures


@pytest.mark.parametrize("step, error, match", [
    ({"is_error": True, "result": "Not logged in · Please run /login"}, llm.ClaudeCodeUnavailable, "claude auth login"),
    ({"is_error": True, "result": "Claude AI usage limit reached|1790000000"}, llm.PlanLimitReached, "API backend"),
    ({"is_error": True, "api_error_status": 429, "result": "API Error"}, llm.PlanLimitReached, "limit"),
    ({"is_error": True, "api_error_status": 500, "result": "API Error: 500 internal"}, llm.ClaudeCodeError, "500"),
    ({"raw": "Error: unknown option '--frobnicate'\n", "exit": 1}, llm.ClaudeCodeError, "printed no result"),
    ({"result": DRAFT_REPLY, "num_turns": 3, "permission_denials": [{"tool_name": "Bash"}]}, llm.ClaudeCodeError,
     "tools"),
])
def test_cli_failures_are_logged_and_raised(env, cli, step, error, match):
    cli.answer(**step)
    with pytest.raises(error, match=match):
        call(env)
    (line,) = log_lines(env)
    assert line["backend"] == "claude-code" and line["error"].startswith(f"{error.__name__}: ")
    assert line["cost_usd"] == 0.0 and line["output_sha256"] is None and line["cli_version"] == "9.9.9"


def test_a_hung_cli_times_out(env, cli, monkeypatch):
    monkeypatch.setenv(llm.CLAUDE_TIMEOUT_ENV, "0.5")
    cli.answer(result=DRAFT_REPLY, sleep=5)
    with pytest.raises(llm.ClaudeCodeError, match="did not finish"):
        call(env)


def test_truncation_and_refusal(env, cli):
    cli.answer(result="<output name=\"update\">cut", stop_reason="max_tokens")
    with pytest.raises(llm.LLMTruncated):
        call(env)
    cli.answer(result="", stop_reason="refusal")
    with pytest.raises(llm.LLMRefusal):
        call(env)
    assert [line["stop_reason"] for line in log_lines(env)] == ["max_tokens", "refusal"]


def test_a_second_turn_without_tools_is_an_output_limit_not_tool_use(env, cli):
    """At effort xhigh the thinking can fill the output limit; the CLI then continues into a second turn. With no tool
    loaded and no permission asked, that is a truncation, reported as such (model comparison, decision 0024)."""
    cli.answer(result=DRAFT_REPLY, num_turns=2)
    with pytest.raises(llm.LLMTruncated, match="output limit of 128000 tokens") as info:
        call(env)
    assert "tools" not in str(info.value)
    (line,) = log_lines(env)
    assert line["stop_reason"] == "max_tokens" and line["cli_stop_reason"] == "end_turn" and line["num_turns"] == 2
    assert "output limit" in line["cli_output_limit"] and line["notional_cost_usd"] == 0.0421


def test_a_client_cannot_be_given_to_the_cli_backend(env, cli):
    with pytest.raises(ValueError, match="claude-code"):
        call(env, client=FakeClient(make_response()))
    assert cli.calls == [] and not env.log.exists()


# ---------------------------------------------------------------- page images


def test_page_images_go_in_as_one_stream_json_message(env, cli):
    reply = envelope(design_checks="- ok", layout_instructions="none", findings="none", questions="none")
    cli.answer(result=reply)
    result = llm.complete("design_reviewer", "09", {"document": "Document."}, part="C",
                          images={"rendered_pages": [env.page_png, env.page_jpg]}, variables={"document": "07"},
                          log_path=env.log, repo_root=env.repo, prompts_dir=env.prompts, schemas_dir=env.schemas)
    assert result.outputs["design_checks"].data == ["ok"]
    (seen,) = cli.calls
    assert seen["args"][1:6] == ["--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
    message = json.loads(seen["stdin"])
    kinds = [block["type"] for block in message["message"]["content"]]
    assert message["type"] == "user" and kinds.count("image") == 2
    assert log_lines(env)[0]["cli_loaded"] == {k: [] for k in llm.CLI_LOADED_KEYS}


def test_a_run_that_loaded_tools_is_refused(env, cli):
    cli.answer(result=envelope(design_checks="- ok", layout_instructions="none", findings="none", questions="none"),
               init={"tools": ["Bash", "Read"]})
    with pytest.raises(llm.ClaudeCodeError, match="tools"):
        llm.complete("design_reviewer", "09", {"document": "Document."}, part="C",
                     images={"rendered_pages": [env.page_png]}, variables={"document": "07"}, log_path=env.log,
                     repo_root=env.repo, prompts_dir=env.prompts, schemas_dir=env.schemas)


# ---------------------------------------------------------------- finding the CLI


def _exe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_the_binary_is_found_by_setting_then_path_then_the_desktop_app(tmp_path, monkeypatch):
    desktop = tmp_path / "desktop"
    for version in ("2.1.9", "2.1.280", "notes"):
        _exe(desktop / version / llm.DESKTOP_CLAUDE_EXE)
    monkeypatch.setattr(llm, "DESKTOP_CLAUDE_DIR", desktop)
    empty_path = str(tmp_path / "empty-bin")
    assert llm.find_claude_binary({"PATH": empty_path}) == desktop / "2.1.280" / llm.DESKTOP_CLAUDE_EXE
    on_path = _exe(tmp_path / "bin" / "claude")
    assert llm.find_claude_binary({"PATH": str(on_path.parent)}) == on_path
    chosen = _exe(tmp_path / "chosen" / "claude")
    assert llm.find_claude_binary({"PATH": str(on_path.parent), llm.CLAUDE_BIN_ENV: str(chosen)}) == chosen
    with pytest.raises(llm.ClaudeCodeUnavailable, match=llm.CLAUDE_BIN_ENV):
        llm.find_claude_binary({llm.CLAUDE_BIN_ENV: str(tmp_path / "missing")})
    monkeypatch.setattr(llm, "DESKTOP_CLAUDE_DIR", tmp_path / "no-desktop")
    with pytest.raises(llm.ClaudeCodeUnavailable, match="not found"):
        llm.find_claude_binary({"PATH": empty_path})


# ---------------------------------------------------------------- through the runner


def test_the_runner_executes_a_bundle_through_the_cli(tmp_path, cli, monkeypatch):
    for name in (llm.PROMPTS_ENV, llm.LOG_ENV, llm.BACKEND_ENV):
        monkeypatch.delenv(name, raising=False)
    renv = rfx.make_env(tmp_path / "runner")
    bundle = renv.assemble()
    manifest = yaml.safe_load((bundle / runner.MANIFEST).read_text(encoding="utf-8"))
    call_part, formats = runner.verify_prompts(manifest, renv.private)
    inputs = runner.read_inputs(bundle, manifest)
    from pipeline import fake_client

    cli.answer(result=fake_client.placeholder_reply(call_part.outputs, formats, runner._fake_context(manifest, inputs)))
    record = renv.execute(bundle, backend="claude-code")
    assert (record["status"], record["backend"], record["client"]) == ("succeeded", "claude-code", "claude-code")
    assert record["cost_usd"] == 0.0 and record["notional_cost_usd"] == 0.0421
    assert record["request_matches_manifest"] is True
    (line,) = [json.loads(x) for x in (bundle / runner.CALLS).read_text(encoding="utf-8").splitlines()]
    assert line["backend"] == "claude-code" and line["input_sha256"] == manifest["request_sha256"]
    assert "Pre-registration candidate for Q3 2026" in cli.calls[0]["stdin"]


def test_the_runner_prints_cli_failures_without_their_text(tmp_path, cli, monkeypatch):
    for name in (llm.PROMPTS_ENV, llm.LOG_ENV, llm.BACKEND_ENV):
        monkeypatch.delenv(name, raising=False)
    renv = rfx.make_env(tmp_path / "runner")
    bundle = renv.assemble()
    cli.answer(raw=f"partial reply {rfx.CANARY_OUTPUT}\n", exit=1)
    out = io.StringIO()
    record = renv.execute(bundle, backend="claude-code", out=out)
    assert record["status"] == "failed" and record["error"]["type"] == "ClaudeCodeError"
    assert "ClaudeCodeError: the Claude Code CLI failed" in out.getvalue() and rfx.CANARY_OUTPUT not in out.getvalue()
    cli.answer(is_error=True, result="Claude AI usage limit reached|1790000000")
    retried = renv.execute(bundle, backend="claude-code", retry=True, out=out)
    assert retried["error"]["type"] == "PlanLimitReached" and "--backend api" in out.getvalue()


def test_oauth_token_is_read_from_the_workspace_env_file(tmp_path, monkeypatch):
    """With no CLAUDE_CODE_OAUTH_TOKEN in the environment, the token comes from the workspace .env and is never logged."""
    from pipeline import edgar, llm

    env_file = tmp_path / ".env"
    env_file.write_text('SEC_USER_AGENT="x y@z"\nCLAUDE_CODE_OAUTH_TOKEN="tok-123"\n', encoding="utf-8")
    env = {edgar.ENV_FILE_ENV: str(env_file)}
    assert llm._workspace_env_value(llm.OAUTH_TOKEN_ENV, env) == "tok-123"
    assert llm._workspace_env_value(llm.OAUTH_TOKEN_ENV, {edgar.ENV_FILE_ENV: str(tmp_path / "missing")}) is None
