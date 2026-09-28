"""Tests of the pipeline runner: pipeline/runner.py, pipeline/registry.py, pipeline/fake_client.py (decisions/0019).

No network, no API key, no model call. EDGAR answers from tests/fixtures/edgar/ plus synthetic documents, the prompts
are synthetic front matter (tests/runner_fixtures.py), and the model is the fake client or a test double. The role
table is the real agents/*.yml of this repository.
"""

from __future__ import annotations

import ast
import datetime as dt
import io
import json
import re
from pathlib import Path

import pytest
import yaml

from pipeline import edgar, fake_client, llm, outputs, registry, runner
from tests import runner_fixtures as fx

STEPS = ("14Q", "15A", "18", "16B", "14T", "14A", "15B", "03-draft", "16A", "04A", "04B-lite", "14B", "03R", "17A")
NOW = dt.datetime(2026, 10, 20, 12, 0, tzinfo=dt.timezone.utc)
RUN_DATE = dt.date.fromisoformat(fx.RUN_DATE)
REAL_PROMPTS = fx.REPO_ROOT.parent / registry.PRIVATE_REPO / "prompts"
WORKFLOW = fx.REPO_ROOT.parent / registry.PRIVATE_REPO / ".github" / "workflows" / "pipeline-step.yml"
INPUTS_15A = ["run_date", "thesis", "dossier", "latest_filings", "prereg_candidates", "calibration", "event",
              "release_history", "schema"]
INPUTS_18 = ["run_date", "month_events", "updates", "divergence_map", "owner_letter_items", "l2_report", "rulings",
             "questions_open", "trust_changes", "memos", "budget", "failures", "phase_status", "prereg_activity",
             "calibration", "mistakes"]
PREREG = "companies/APP/prereg/FY2026Q3.yml"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """No real .env, EDGAR cache, prompt, log or schema directory; the network is off."""
    monkeypatch.delenv(edgar.UA_ENV, raising=False)
    monkeypatch.setenv(edgar.ENV_FILE_ENV, str(tmp_path / "missing.env"))
    monkeypatch.setenv(edgar.CACHE_ENV, str(tmp_path / "default-edgar-cache"))
    for name in (llm.PROMPTS_ENV, llm.LOG_ENV, outputs.SCHEMAS_ENV):
        monkeypatch.delenv(name, raising=False)

    def no_network(*args, **kwargs):
        raise AssertionError("the runner tests never use the network")

    monkeypatch.setattr(edgar, "UrllibTransport", no_network)


@pytest.fixture
def env(tmp_path):
    return fx.make_env(tmp_path)


def manifest_of(bundle: Path) -> dict:
    return yaml.safe_load((bundle / runner.MANIFEST).read_text(encoding="utf-8"))


def call_of(env, bundle: Path):
    return runner.verify_prompts(manifest_of(bundle), env.private)


def valid_client(env, bundle: Path, note: str = fake_client.DRY_RUN_NOTE) -> fake_client.FakeClient:
    """A test double that answers like a model would: every declared output, valid, with `note` as its text."""
    manifest = manifest_of(bundle)
    call, formats = call_of(env, bundle)
    inputs = runner.read_inputs(bundle, manifest)
    reply = fake_client.placeholder_reply(call.outputs, formats, runner._fake_context(manifest, inputs))
    return fake_client.FakeClient(reply.replace(fake_client.DRY_RUN_NOTE, note))


def place(env, bundle: Path, **kwargs):
    kwargs.setdefault("allow_fake", True)
    kwargs.setdefault("lint", False)
    kwargs.setdefault("now", NOW)
    kwargs.setdefault("out", io.StringIO())
    kwargs.setdefault("edgar_gateway", env.gateway)
    return runner.place(bundle, roots=env.roots, schemas_dir=env.schemas, **kwargs)


def roots_args(env) -> list[str]:
    return ["--public-root", str(env.public), "--private-root", str(env.private), "--workspace-root",
            str(env.workspace), "--schemas-dir", str(env.schemas)]


def branch_of(root: Path) -> str:
    return fx.git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()


# ---------------------------------------------------------------------------------------------------- registry


def test_the_supported_steps():
    assert list(registry.STEPS) == list(STEPS)
    assert registry.STEPS["15A"].pipeline_fields is not None and registry.STEPS["03R"].pipeline_fields is not None
    assert registry.STEPS["04A"].variables == {"subject": "quarterly update"}
    assert all(registry.STEPS[s].post_event for s in STEPS[3:]) and not registry.STEPS["15A"].post_event
    assert registry.STEPS["17A"].about_company and registry.STEPS["17A"].scope == "hq"
    assert registry.STEPS["17A"].bundle_name(RUN_DATE, "APP", 2) == f"{fx.RUN_DATE}-17A-APP-r2"
    assert registry.ROUNDS == {"16A": 2, "04A": 2, "03R": 2, "17A": 2}
    assert registry.STEPS["14Q"].holdings_only and registry.STEPS["15A"].holdings_only
    assert registry.STEPS["18"].scope == "hq" and registry.STEPS["18"].period_kind == "month"


@pytest.mark.parametrize("step", STEPS)
def test_every_declared_input_has_an_assembler(env, step):
    spec = registry.STEPS[step]
    call = llm.prompt_part(llm.load_prompt(spec.prompt_id, env.private / "prompts"), spec.part)
    assert call.label == step
    assert [name for name, _ in call.inputs if name not in registry.INPUTS] == []


@pytest.mark.parametrize("step", STEPS)
def test_the_real_roles_may_see_every_input_of_their_step(env, step):
    """agents/*.yml of this repository (the only role table) allow each step's inputs (00 section G6)."""
    spec = registry.STEPS[step]
    call = llm.prompt_part(llm.load_prompt(spec.prompt_id, env.private / "prompts"), spec.part)
    role = llm.role_definition(call.role, fx.REPO_ROOT)
    llm.check_inputs(call, role, [name for name, _ in call.inputs])
    assert step in role.prompts or spec.prompt_id in role.prompts


@pytest.mark.skipif(not REAL_PROMPTS.is_dir(), reason="the private prompts are not checked out next to this repository")
@pytest.mark.parametrize("step", STEPS)
def test_the_real_prompts_are_covered_and_match_the_fixtures(env, step):
    spec = registry.STEPS[step]
    real = llm.prompt_part(llm.load_prompt(spec.prompt_id, REAL_PROMPTS), spec.part)
    fixture = llm.prompt_part(llm.load_prompt(spec.prompt_id, env.private / "prompts"), spec.part)
    assert [n for n, _ in real.inputs if n not in registry.INPUTS] == []
    assert (real.label, real.role, real.inputs, real.outputs) == (fixture.label, fixture.role, fixture.inputs,
                                                                 fixture.outputs)
    llm.check_inputs(real, llm.role_definition(real.role, fx.REPO_ROOT), [n for n, _ in real.inputs])


# ---------------------------------------------------------------------------------------------------- isolation


def seed_question_list(env, period: str = "FY2026Q3") -> Path:
    """A frozen and placed 14Q question list for APP (FY2026Q3 unless given)."""
    run = env.private / "runs" / "APP" / "2026-07-01-14Q"
    fx.write_yaml(run / runner.MANIFEST, {"manifest_version": 1, "step": "14Q", "period": period})
    fx.write_yaml(run / runner.RUN_RECORD, {"status": "succeeded"})
    fx.write_yaml(run / "question_list.yml", [
        {"id": "Q01", "question": "first", "kind": "pillar", "maps_to": ["P1"]},
        {"id": "Q02", "question": "second", "kind": "pillar", "maps_to": ["P2"]},
        {"id": "Q03", "question": "open question", "kind": "open", "maps_to": []},
    ])
    return run


def test_the_blind_read_gets_only_the_stripped_question_list(env):
    seed_question_list(env, "FY2026Q2")
    bundle = env.assemble("14A", period="FY2026Q2")
    stripped = yaml.safe_load((bundle / "inputs" / "question_list_stripped.yml").read_text(encoding="utf-8"))
    assert all("kind" not in q and "maps_to" not in q for q in stripped)
    assert {q["id"] for q in stripped} == {"Q01", "Q02", "Q03"} and stripped[-1]["question"] == "open question"
    filings, entry = manifest_of(bundle)["inputs"]
    assert entry["sources"][0]["transform"] == "isolation.question_list_stripped" and entry["sources"][0]["seed"]
    assert filings["name"] == "filings" and manifest_of(bundle)["role"] == "blind_reader"


def test_assembly_refuses_an_input_the_role_may_not_see(env):
    """A blind-read part that declared the thesis is refused before anything is written."""
    seed_question_list(env, "FY2026Q2")
    prompt = env.private / "prompts" / "14-questions.md"
    prompt.write_text(prompt.read_text(encoding="utf-8").replace(
        "inputs: [filings, question_list_stripped]", "inputs: [filings, question_list_stripped, thesis]"),
        encoding="utf-8")
    with pytest.raises(runner.RunnerError, match="thesis") as info:
        env.assemble("14A", period="FY2026Q2")
    assert "blind_reader" in str(info.value)
    assert not (env.private / "runs" / "APP" / f"{fx.RUN_DATE}-14A").exists()


# ---------------------------------------------------------------------------------------------------- assemble


def test_assemble_15a_builds_every_declared_input_with_hashes_and_sources(env):
    bundle = env.assemble()
    manifest = manifest_of(bundle)
    assert bundle == env.private / "runs" / "APP" / f"{fx.RUN_DATE}-15A"
    assert manifest["bundle"] == f"runs/APP/{fx.RUN_DATE}-15A" and manifest["role"] == "company_manager"
    assert [e["name"] for e in manifest["inputs"]] == INPUTS_15A
    for entry in manifest["inputs"]:
        data = (bundle / entry["file"]).read_bytes()
        assert registry.sha256_bytes(data) == entry["sha256"] and len(data) == entry["bytes"] and entry["sources"]
    assert sorted(p.name for p in (bundle / "inputs").iterdir()) == sorted(
        Path(e["file"]).name for e in manifest["inputs"])
    assert manifest["inputs_sha256"] == runner.inputs_digest(manifest["inputs"])
    by_name = {e["name"]: e for e in manifest["inputs"]}
    assert by_name["dossier"]["substitute"] == "dossier" and "reports__APP.txt" in by_name["dossier"]["note"]
    assert by_name["calibration"]["empty"] is True and "no settled predictions" in by_name["calibration"]["note"]
    assert manifest["pipeline_fields"]["prereg"] == {
        "company": "APP",
        "event": {"period": "FY2026Q3", "expected_release": "2026-11-02", "form": "8-K", "placeholder": True},
        "deadline": "2026-11-01T23:59:59-05:00",
        "author": "system",
    }
    assert manifest["pipeline_commit"] == fx.git(env.public, "rev-parse", "HEAD").strip()
    assert manifest["prompt"]["sha256"] == registry.sha256_bytes((env.private / "prompts/15-prereg.md").read_bytes())
    assert manifest["variables"] == {"ticker": "APP", "company": "AppLovin", "status": "holding", "period": "FY2026Q3",
                                     "date": fx.RUN_DATE}
    assert manifest["schemas"]["prereg"]["digest"] == registry.schema_digest(fx.PREREG_SCHEMA)
    assert manifest["context"]["period"] == "FY2026Q3" and manifest["context"]["run_date"] == fx.RUN_DATE
    assert re.fullmatch(r"[0-9a-f]{64}", manifest["request_sha256"])

    filings = (bundle / "inputs" / "latest_filings.txt").read_text(encoding="utf-8")
    assert "[src:APP-8K-2026-08-05#EX-99.1]" in filings and "[src:APP-10Q-FY2026Q2]" in filings
    assert "hidden contexts" not in filings  # the inline XBRL header is dropped
    tags = {s["tag"]: s["registered"] for s in by_name["latest_filings"]["sources"]}
    assert tags == {"APP-8K-2026-08-05#EX-99.1": True, "APP-10Q-FY2026Q2": False}
    candidates = yaml.safe_load((bundle / "inputs" / "prereg_candidates.yml").read_text(encoding="utf-8"))
    assert len(candidates["from_thesis_todo"]) == 1 and fx.PREREG_CANDIDATE in candidates["from_thesis_todo"][0]
    history = yaml.safe_load((bundle / "inputs" / "release_history.yml").read_text(encoding="utf-8"))
    assert [e["period"] for e in history["events"]] == ["FY2023Q3", "FY2024Q3", "FY2025Q3"]
    event = yaml.safe_load((bundle / "inputs" / "event.yml").read_text(encoding="utf-8"))
    assert event["merge_by"] == "2026-10-30T00:59:59-04:00" and event["status"] == "estimated"


def test_assembly_fails_loudly_with_every_missing_input_and_writes_nothing(env, tmp_path):
    (env.workspace / "inputs" / "text" / "reports__APP.txt").unlink()
    offline = registry.EdgarGateway(edgar.EdgarClient(offline=True, cache_dir=tmp_path / "empty-cache"))
    with pytest.raises(runner.RunnerError) as info:
        env.assemble(edgar_gateway=offline)
    message = str(info.value)
    for name in ("dossier", "latest_filings", "event", "release_history"):
        assert f"- {name}:" in message
    assert "- thesis:" not in message and "- calibration:" not in message
    assert not (env.private / "runs" / "APP").exists()


@pytest.mark.parametrize("args, match", [
    (("16Z", "APP", "FY2026Q3"), "unknown step"),
    (("15A", "APP", "2026-10"), "quarter period"),
    (("15A", "AXP", "FY2026Q3"), "holdings only"),
    (("18", "APP", "2026-10"), "HQ step"),
    (("18", "hq", "FY2026Q3"), "month period"),
])
def test_step_company_and_period_are_checked(env, args, match):
    with pytest.raises(runner.RunnerError, match=match):
        env.assemble(*args)


def test_assembly_pins_committed_code(env):
    (env.public / "pipeline").mkdir()
    (env.public / "pipeline" / "local.py").write_text("# a local change\n", encoding="utf-8")
    with pytest.raises(runner.RunnerError, match=r"uncommitted changes in 1 path\(s\): pipeline/local.py"):
        env.assemble(allow_dirty=False)
    manifest = manifest_of(env.assemble(allow_dirty=True))
    assert manifest["pipeline"]["dirty_paths"] == ["pipeline/local.py"]
    assert any(note.startswith("pin: uncommitted") for note in manifest["notes"])


def test_unpushed_commits_are_noted_not_refused(env):
    manifest = manifest_of(env.assemble(allow_dirty=False))
    assert manifest["pipeline"]["pushed"] is False and manifest["pipeline"]["dirty_paths"] == []
    assert any("no remote branch" in note for note in manifest["notes"])


def test_bundles_are_never_overwritten(env):
    env.assemble()
    with pytest.raises(runner.RunnerError, match="never overwritten"):
        env.assemble()


def test_monthly_letter_inputs_say_explicitly_when_there_is_nothing(env):
    runs = env.private / "runs" / "APP"
    fx.write_yaml(runs / "2026-10-05-04A" / "questions.yml", {"questions": [{"id": "04A-1-Q1", "issue": "synthetic"}]})
    fx.write_yaml(runs / "2026-10-20-15A" / runner.MANIFEST, {"manifest_version": 1, "period": "FY2026Q3"})
    fx.write_yaml(runs / "2026-10-20-15A" / runner.RUN_RECORD, {"status": "succeeded", "cost_usd": 0.3})
    fx.write(runs / "2026-10-20-15A" / "outputs" / "l2_report.md", "Registered three expectations.\n")
    fx.write_yaml(runs / "2026-10-10-14Q" / runner.MANIFEST, {"manifest_version": 1, "period": "FY2026Q3"})
    fx.write_yaml(runs / "2026-10-10-14Q" / "attempts" / "1" / runner.RUN_RECORD, {"status": "failed"})
    fx.write_yaml(runs / "2026-10-10-14Q" / runner.RUN_RECORD, {"status": "failed", "error": {"type": "LLMRefusal"}})
    mistakes = env.public / "mistakes.md"
    mistakes.write_text(mistakes.read_text(encoding="utf-8") + "\n### 2026-10-12 · APP · process error\n\n- Late.\n",
                        encoding="utf-8")
    bundle = env.assemble("18", "hq", "2026-10", run_date=dt.date(2026, 11, 2), today=dt.date(2026, 11, 2))
    manifest = manifest_of(bundle)
    assert manifest["bundle"] == "runs/hq/2026-11-02-18" and manifest["context"]["month"] == "2026-10"
    by_name = {e["name"]: e for e in manifest["inputs"]}
    assert list(by_name) == INPUTS_18
    for name in ("updates", "divergence_map", "owner_letter_items", "rulings", "memos", "trust_changes", "calibration"):
        doc = yaml.safe_load((bundle / by_name[name]["file"]).read_text(encoding="utf-8"))
        assert by_name[name].get("empty") is True and by_name[name]["note"] and doc["status"] == "empty", name
    for name in ("month_events", "l2_report", "questions_open", "failures", "mistakes", "budget", "phase_status",
                 "prereg_activity"):
        assert not by_name[name].get("empty"), name

    def read(name):
        return (bundle / by_name[name]["file"]).read_text(encoding="utf-8")

    assert "Registered three expectations." in read("l2_report") and "Late." in read("mistakes")
    failures = yaml.safe_load(read("failures"))
    assert failures["repeated_failures"] == [{"scope": "APP", "part": "14Q", "consecutive_failures": 2}]
    assert len(failures["failed_runs"]) == 2
    activity = yaml.safe_load(read("prereg_activity"))
    assert activity["next_month"] == "2026-11"
    assert [(row["company"], row["expected_release"]) for row in activity["upcoming_next_month"]] == [("APP", "2026-11-02")]
    assert "does not exist yet" in yaml.safe_load(read("budget"))["note"]


# ---------------------------------------------------------------------------------------------------- execute


def test_dry_run_runs_the_fake_backend_outside_both_repositories(env, tmp_path):
    out = io.StringIO()
    bundle, record = runner.dry_run("15A", "APP", "FY2026Q3", run_date=RUN_DATE, roots=env.roots,
                                    out_root=tmp_path / "dry", edgar_gateway=env.gateway, schemas_dir=env.schemas,
                                    today=RUN_DATE, out=out)
    assert bundle == (tmp_path / "dry" / "runs" / "APP" / f"{fx.RUN_DATE}-15A").resolve()
    assert not (env.private / "runs" / "APP").exists()
    assert (record["status"], record["backend"], record["client"], record["attempt"]) == ("succeeded", "fake", "fake", 1)
    assert record["request_matches_manifest"] is True and record["requests"] == 1 and record["cost_usd"] > 0
    assert sorted(p.name for p in (bundle / "outputs").iterdir()) == ["l2_report.md", "prereg.yml", "questions.yml"]
    manifest = manifest_of(bundle)
    prereg = yaml.safe_load((bundle / "outputs" / "prereg.yml").read_text(encoding="utf-8"))
    assert {k: prereg[k] for k in manifest["pipeline_fields"]["prereg"]} == manifest["pipeline_fields"]["prereg"]
    assert not list(outputs.schema_validator("prereg", env.schemas).iter_errors(outputs.jsonable(prereg)))
    log = tmp_path / "dry" / registry.LLM_LOG_REL
    assert (bundle / runner.CALLS).read_bytes() == log.read_bytes()
    assert json.loads(log.read_text(encoding="utf-8").splitlines()[0])["input_sha256"] == manifest["request_sha256"]
    rows = {(r["output"], r["repo"], r["path"]) for r in record["placements"]}
    assert rows == {("prereg", "owners-office", PREREG),
                    ("l2_report", "owners-office-private", f"runs/APP/{fx.RUN_DATE}-15A/l2_report.md"),
                    ("questions", "owners-office-private", f"runs/APP/{fx.RUN_DATE}-15A/questions.yml")}
    assert (bundle / runner.REPLY).is_file() and yaml.safe_load((bundle / runner.RUN_RECORD).read_text())["reply"]


def test_the_fake_prereg_passes_the_real_thesis_ci_schema(env, tmp_path):
    contract = pytest.importorskip("thesis_ci.contract")
    bundle, _ = runner.dry_run("15A", "APP", "FY2026Q3", run_date=RUN_DATE, roots=env.roots, out_root=tmp_path / "dry",
                               edgar_gateway=env.gateway, schemas_dir=env.schemas, today=RUN_DATE, out=io.StringIO())
    prereg = yaml.safe_load((bundle / "outputs" / "prereg.yml").read_text(encoding="utf-8"))
    assert list(contract.validator("prereg").iter_errors(outputs.jsonable(prereg))) == []


def test_placement_plans_of_14q_and_18(env, tmp_path):
    kwargs = dict(roots=env.roots, out_root=tmp_path / "dry", edgar_gateway=env.gateway, schemas_dir=env.schemas,
                  out=io.StringIO())
    _, questions = runner.dry_run("14Q", "APP", "FY2026Q3", run_date=RUN_DATE, today=RUN_DATE, **kwargs)
    assert [(r["output"], r["repo"], r["path"]) for r in questions["placements"]] == [
        ("question_list", "owners-office-private", f"runs/APP/{fx.RUN_DATE}-14Q/question_list.yml")]
    _, letter = runner.dry_run("18", "hq", "2026-10", run_date=dt.date(2026, 11, 2), today=dt.date(2026, 11, 2),
                               **kwargs)
    assert {(r["output"], r["repo"], r["path"]) for r in letter["placements"]} == {
        ("letter", "owners-office", "letters/2026-10.md"),
        ("private_appendix", "owners-office-private", "letters/2026-10-private-appendix.md")}


def test_execute_refuses_a_bundle_that_does_not_match_its_manifest(env):
    bundle = env.assemble()
    (bundle / "inputs" / "thesis.yml").write_text("changed: true\n", encoding="utf-8")
    with pytest.raises(runner.RunnerError, match="inputs/thesis.yml: sha256"):
        env.execute(bundle)
    assert not (bundle / runner.RUN_RECORD).exists()


def test_execute_refuses_unlisted_input_files(env):
    bundle = env.assemble()
    (bundle / "inputs" / "extra.txt").write_text("extra\n", encoding="utf-8")
    with pytest.raises(runner.RunnerError, match="inputs/extra.txt: not listed"):
        env.execute(bundle)


def test_execute_refuses_a_prompt_changed_after_assembly(env):
    bundle = env.assemble()
    prompt = env.private / "prompts" / "15-prereg.md"
    prompt.write_text(prompt.read_text(encoding="utf-8") + "\nChanged.\n", encoding="utf-8")
    with pytest.raises(runner.RunnerError, match="prompts changed after assembly"):
        env.execute(bundle)


def test_the_budget_guard_reads_the_call_log_and_a_retry_keeps_the_failed_attempt(env):
    bundle = env.assemble()
    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    env.log.write_text(json.dumps({"timestamp": stamp, "cost_usd": 25.0}) + "\n", encoding="utf-8")
    record = env.execute(bundle)
    assert record["status"] == "failed" and record["error"]["type"] == "BudgetExceeded" and record["requests"] == 0
    assert not (bundle / "outputs").exists() and not (bundle / runner.CALLS).exists()
    with pytest.raises(runner.RunnerError, match="--retry"):
        env.execute(bundle)
    env.log.write_text("", encoding="utf-8")
    retried = env.execute(bundle, retry=True)
    assert retried["status"] == "succeeded" and retried["attempt"] == 2
    assert yaml.safe_load((bundle / "attempts" / "1" / runner.RUN_RECORD).read_text())["status"] == "failed"
    with pytest.raises(runner.RunnerError, match="only runs a failed bundle again"):
        env.execute(bundle, retry=True)


def test_claude_code_backend_needs_the_cli_before_the_bundle_is_touched(env, monkeypatch, tmp_path):
    monkeypatch.setenv(llm.CLAUDE_BIN_ENV, str(tmp_path / "no-such-claude"))
    bundle = env.assemble()
    with pytest.raises(runner.BackendUnavailable, match=llm.CLAUDE_BIN_ENV):
        env.execute(bundle, backend="claude-code")
    assert not (bundle / runner.RUN_RECORD).exists()


def test_the_backend_is_passed_to_llm_complete(env, monkeypatch):
    real_complete = llm.complete
    seen = {}

    def complete(*args, backend=None, **kwargs):
        seen["backend"] = backend
        return real_complete(*args, backend="api", **kwargs)  # the injected test double stands in for the model

    monkeypatch.setattr(llm, "complete", complete)
    monkeypatch.setattr(llm, "find_claude_binary", lambda env=None: Path("/usr/bin/true"))
    assert [runner.backend_kwargs(b) for b in ("fake", "api", "claude-code")] == [
        {"backend": "fake"}, {"backend": "api"}, {"backend": "claude-code"}]
    bundle = env.assemble()
    record = env.execute(bundle, backend="claude-code", client=valid_client(env, bundle))
    assert seen == {"backend": "claude-code"}
    assert (record["status"], record["backend"], record["client"]) == ("succeeded", "claude-code", "injected")


def test_model_calls_never_run_in_a_public_repositorys_actions(env):
    bundle = env.assemble()
    client = valid_client(env, bundle)
    public_actions = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "someone/owners-office"}
    with pytest.raises(runner.RunnerError, match="world-readable"):
        env.execute(bundle, backend="api", client=client, env=public_actions)
    assert client.requests == [] and not (bundle / runner.RUN_RECORD).exists()
    private_actions = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "someone/owners-office-private",
                       "GITHUB_RUN_ID": "42"}
    record = env.execute(bundle, backend="api", client=client, env=private_actions)
    assert record["status"] == "succeeded" and record["github"]["run_id"] == "42"


def test_real_backends_run_only_the_pinned_code(env):
    bundle = env.assemble()
    (env.public / "pipeline").mkdir()
    (env.public / "pipeline" / "drift.py").write_text("# drift\n", encoding="utf-8")
    with pytest.raises(runner.RunnerError, match="local changes"):
        env.execute(bundle, backend="api", client=valid_client(env, bundle))
    record = env.execute(bundle, backend="api", client=valid_client(env, bundle), allow_unpinned=True)
    assert record["status"] == "succeeded" and record["pipeline"]["unpinned_allowed"] is True
    assert record["pipeline"]["matches"] is False


def test_invalid_output_is_recorded_in_the_bundle_and_never_printed(env, capsys):
    bundle = env.assemble()
    reply = (f'<output name="prereg">\nitems: []\nnote: {fx.CANARY_OUTPUT}\n</output>\n\n'
             f'<output name="l2_report">\n{fx.CANARY_OUTPUT}\n</output>\n\n'
             f'<output name="questions">\n{outputs.EMPTY_MARK}\n</output>')
    out = io.StringIO()
    record = env.execute(bundle, backend="api", client=fake_client.FakeClient(reply), out=out)
    assert record["status"] == "failed" and record["error"]["type"] == "LLMOutputInvalid"
    assert record["error"]["validation_errors"] and record["requests"] == 2
    assert (bundle / runner.REPLY).read_text(encoding="utf-8") == reply and not (bundle / "outputs").exists()
    printed = out.getvalue() + "".join(capsys.readouterr())
    assert "LLMOutputInvalid" in printed
    assert not [canary for canary in fx.CANARIES if canary in printed]


def test_execute_show_and_pr_body_print_no_input_or_output_text(env, capsys):
    bundle = env.assemble()
    out = io.StringIO()
    record = env.execute(bundle, backend="api", client=valid_client(env, bundle, f"Synthetic {fx.CANARY_OUTPUT}"),
                         out=out)
    assert record["status"] == "succeeded"
    assert fx.CANARY_OUTPUT in (bundle / "outputs" / "prereg.yml").read_text(encoding="utf-8")
    assert fx.CANARY_FILING in (bundle / "inputs" / "latest_filings.txt").read_text(encoding="utf-8")
    assert runner.main(["show", str(bundle), *roots_args(env)]) == 0
    assert runner.main(["pr-body", str(bundle), *roots_args(env)]) == 0
    printed = out.getvalue() + "".join(capsys.readouterr())
    assert "sha256" in printed and "## Pipeline step 15A" in printed
    assert not [canary for canary in fx.CANARIES if canary in printed]


def test_cli_refuses_fake_execution_inside_the_private_repository(env, capsys):
    bundle = env.assemble()
    assert runner.main(["execute", str(bundle), "--backend", "fake", *roots_args(env)]) == 1
    assert "dry-run" in capsys.readouterr().err and not (bundle / runner.RUN_RECORD).exists()


def test_cli_lists_the_steps(capsys):
    assert runner.main(["steps"]) == 0
    printed = capsys.readouterr().out
    assert all(step in printed for step in STEPS)


# ---------------------------------------------------------------------------------------------------- place


def test_dry_run_outputs_are_never_placed(env):
    bundle = env.executed()
    with pytest.raises(runner.RunnerError, match="never placed"):
        place(env, bundle, allow_fake=False)
    assert not (env.public / PREREG).exists()


def test_place_writes_public_outputs_on_a_branch_with_the_pipeline_header(env):
    bundle = env.executed()
    text = (bundle / "outputs" / "prereg.yml").read_text(encoding="utf-8")
    fx.rewrite_output(bundle, "prereg", text.replace("expected_release: '2026-11-02'", "expected_release: '2026-12-01'"))
    report = place(env, bundle)
    assert branch_of(env.public) == f"pipeline/APP-{fx.RUN_DATE}-15A" == report["public_branch"]
    placed = yaml.safe_load((env.public / PREREG).read_text(encoding="utf-8"))
    assert placed["event"]["expected_release"] == "2026-11-02" and placed["deadline"] == "2026-11-01T23:59:59-05:00"
    assert (placed["company"], placed["author"], placed["horizon"]) == ("APP", "system", "mixed")
    assert any(warning.startswith("prereg.event") for warning in report["warnings"])
    assert (env.private / "runs" / "APP" / f"{fx.RUN_DATE}-15A" / "l2_report.md").is_file()
    assert (env.private / "runs" / "APP" / f"{fx.RUN_DATE}-15A" / "questions.yml").is_file()
    record = yaml.safe_load((bundle / runner.PLACEMENT_RECORD).read_text(encoding="utf-8"))
    assert {(f["repo"], f["path"]) for f in record["files"]} == {
        ("owners-office", PREREG), ("owners-office-private", f"runs/APP/{fx.RUN_DATE}-15A/l2_report.md"),
        ("owners-office-private", f"runs/APP/{fx.RUN_DATE}-15A/questions.yml")}
    assert fx.git(env.public, "log", "--oneline", "main").count("\n") == 1  # nothing was committed anywhere


def test_check_mode_writes_nothing(env):
    bundle = env.executed()
    report = place(env, bundle, check=True)
    assert report["check_only"] and not (env.public / PREREG).exists() and branch_of(env.public) == "main"
    assert not (bundle / runner.PLACEMENT_RECORD).exists()


def test_public_outputs_with_cjk_text_are_flagged(env):
    bundle = env.executed()
    text = (bundle / "outputs" / "prereg.yml").read_text(encoding="utf-8")
    fx.rewrite_output(bundle, "prereg", text.replace("statement: DRY-RUN", "statement: \u672c\u5b63 DRY-RUN", 1))
    with pytest.raises(runner.RunnerError, match="CJK text on 1 line"):
        place(env, bundle)
    assert not (env.public / PREREG).exists() and branch_of(env.public) == "main"
    report = place(env, bundle, allow_cjk=True)
    assert report["allow_cjk"] is True and any("CJK" in warning for warning in report["warnings"])


def test_private_outputs_may_contain_cjk_text(env):
    bundle = env.executed()
    fx.rewrite_output(bundle, "l2_report", "\u767b\u8bb0\u4e86\u4e09\u6761\u9884\u671f\u3002\n")
    report = place(env, bundle)
    assert not report["warnings"] or not any("CJK" in warning for warning in report["warnings"])


def test_a_placed_prereg_only_ever_gets_a_new_header(env):
    bundle = env.executed()
    place(env, bundle)
    proof = env.public / f"{PREREG}.ots"
    proof.write_bytes(b"synthetic proof")
    fx.commit_all(env.public, "prereg")
    report = place(env, bundle, announced="2026-11-05")
    placed = yaml.safe_load((env.public / PREREG).read_text(encoding="utf-8"))
    assert placed["event"] == {"period": "FY2026Q3", "expected_release": "2026-11-05", "form": "8-K",
                               "placeholder": False}
    assert placed["deadline"] == "2026-11-04T23:59:59-05:00" and not proof.exists()
    (prereg,) = [f for f in report["files"] if f["output"] == "prereg"]
    assert prereg["status"] == "header-update" and prereg["remove"] == [f"{PREREG}.ots"]
    fx.commit_all(env.public, "header")
    text = (bundle / "outputs" / "prereg.yml").read_text(encoding="utf-8")
    fx.rewrite_output(bundle, "prereg", text.replace("probability: 0.5", "probability: 0.6", 1))
    with pytest.raises(runner.RunnerError, match="different items"):
        place(env, bundle, announced="2026-11-05")


def test_place_refuses_after_the_deadline_and_warns_after_the_merge_by_time(env):
    bundle = env.executed()
    with pytest.raises(runner.RunnerError, match="deadline"):
        place(env, bundle, now=dt.datetime(2026, 11, 2, 12, tzinfo=dt.timezone.utc))
    report = place(env, bundle, now=dt.datetime(2026, 10, 31, 12, tzinfo=dt.timezone.utc))
    assert any("merge-by" in warning for warning in report["warnings"])


def fake_lint(monkeypatch, before: list, after: list) -> None:
    """run_lint answers `before` on its first call (the baseline) and `after` on the next one."""
    results = iter([before, after])
    monkeypatch.setattr(runner, "run_lint", lambda roots, today: {"public": {"errors": next(results), "warnings": 0},
                                                                  "private": {"errors": [], "warnings": 0}})


def test_place_rolls_back_when_the_placement_brings_lint_errors(env, monkeypatch):
    bundle = env.executed()
    error = {"check": "C-TEST", "file": PREREG, "line": 1, "message": "synthetic"}
    fake_lint(monkeypatch, before=[], after=[error])
    with pytest.raises(runner.RunnerError, match="1 new error"):
        place(env, bundle, lint=True)
    assert not (env.public / PREREG).exists()
    assert not (env.private / "runs" / "APP" / f"{fx.RUN_DATE}-15A" / "l2_report.md").exists()
    assert not (bundle / runner.PLACEMENT_RECORD).exists()


def test_lint_errors_that_were_there_before_do_not_block_a_placement(env, monkeypatch):
    bundle = env.executed()
    error = {"check": "C-TEST", "file": "elsewhere.yml", "line": 3, "message": "synthetic"}
    fake_lint(monkeypatch, before=[error], after=[{**error, "line": 4}])
    report = place(env, bundle, lint=True)
    assert report["lint"]["public"] == {"errors": [], "pre_existing_errors": 1, "warnings": 0}
    assert any("already reported 1 error" in warning for warning in report["warnings"])
    assert (env.public / PREREG).is_file()


# ---------------------------------------------------------------------------------------------------- repository rules


@pytest.mark.parametrize("module", ["runner.py", "registry.py", "fake_client.py", "chain.py", "documents.py",
                                    "evaluation.py"])
def test_the_runner_modules_import_no_model_sdk(module):
    tree = ast.parse((fx.REPO_ROOT / "pipeline" / module).read_text(encoding="utf-8"))
    imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names}
    imported |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                 and node.module}
    assert not imported & {"anthropic", "openai", "claude_agent_sdk", "claude_code_sdk"}


@pytest.mark.parametrize("path", [*sorted(p.relative_to(fx.REPO_ROOT).as_posix()
                                          for p in (fx.REPO_ROOT / "pipeline").glob("*.py")),
                                  "tests/test_runner.py", "tests/runner_fixtures.py", "tests/test_claude_code.py",
                                  "tests/test_post_earnings.py", "tests/evaluate_shim.py",
                                  "docs/decisions/0019-pipeline-runner.md", "docs/decisions/0022-claude-code-backend.md",
                                  "docs/decisions/0024-post-earnings-steps.md"])
def test_the_pipeline_files_are_english_only(path):
    """Owner policy 2026-09-25: the public repositories are English-first (no CJK text in the pipeline code)."""
    target = fx.REPO_ROOT / path
    if not target.is_file():
        pytest.skip(f"{path} does not exist")
    assert runner.cjk_lines(target.read_text(encoding="utf-8")) == []


@pytest.mark.skipif(not WORKFLOW.is_file(), reason="the private repository is not checked out next to this one")
def test_the_private_workflow_is_manual_minimal_and_quiet():
    text = WORKFLOW.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    triggers = data.get("on", data.get(True))
    assert set(triggers) == {"workflow_dispatch"}
    assert data["permissions"] == {"contents": "read"}
    (job,) = data["jobs"].values()
    assert job["permissions"] == {"contents": "write", "pull-requests": "write"}
    assert set(re.findall(r"secrets\.([A-Za-z0-9_]+)", text)) == {"ANTHROPIC_API_KEY"}
    for step in job["steps"]:
        assert "${{ inputs." not in step.get("run", "") and "github.event.inputs" not in step.get("run", "")
    keyed = [step for step in job["steps"] if "ANTHROPIC_API_KEY" in (step.get("env") or {})]
    assert len(keyed) == 1 and "python -m pipeline.runner execute" in keyed[0]["run"]
    assert "--backend api" in keyed[0]["run"]
    assert runner.cjk_lines(text) == []


def test_the_workspace_root_names_the_env_file(tmp_path):
    """--workspace-root decides which .env EDGAR and the CLI backend read, unless one is already named."""
    roots = runner.resolve_roots(tmp_path / "pub", tmp_path / "priv", tmp_path / "ws")
    environ: dict[str, str] = {}
    runner.use_workspace_env_file(roots, environ)
    assert environ["OWNERS_OFFICE_ENV_FILE"] == str((tmp_path / "ws").resolve() / ".env")
    environ = {"OWNERS_OFFICE_ENV_FILE": "/elsewhere/.env"}
    runner.use_workspace_env_file(roots, environ)
    assert environ["OWNERS_OFFICE_ENV_FILE"] == "/elsewhere/.env"
