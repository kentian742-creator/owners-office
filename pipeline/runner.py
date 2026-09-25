"""Pipeline runner: one prompt part at a time, from an input bundle to placed outputs.

Phase 1 of docs/DESIGN.md (STATUS T4, T7, T11, T12); design and reasons in docs/decisions/0019.

    python -m pipeline.runner steps
    python -m pipeline.runner assemble 15A APP FY2026Q3 --run-date 2026-10-20
    python -m pipeline.runner execute runs/APP/2026-10-20-15A [--backend claude-code|api|fake] [--retry]
    python -m pipeline.runner show runs/APP/2026-10-20-15A
    python -m pipeline.runner place runs/APP/2026-10-20-15A [--announced 2026-11-05] [--check]
    python -m pipeline.runner dry-run 15A APP FY2026Q3 --run-date 2026-10-20
    python -m pipeline.runner pr-body runs/APP/2026-10-20-15A

1. assemble (local). Builds exactly the inputs the prompt part declares, through the input registry
   (pipeline/registry.py), checks them against the role's visibility (llm.check_inputs, 00 section G6) and writes a
   bundle into the private repository: runs/<TICKER|hq>/<run_date>-<part>/inputs/<name>.<md|yml|txt> plus
   manifest.yml (step, role, prompt id/part/version and hashes, variables, each input's sources and sha256, the
   public commit whose pipeline code executes it, created_at). EDGAR is read here, with the SEC User-Agent from
   the workspace .env. A missing required input fails the assembly with the full list of what is missing.
2. execute. Verifies the bundle against its manifest (input hashes, prompt hashes, the pinned public commit, the
   thesis-ci schemas), runs llm.complete() once and writes outputs/, run.yml, calls.jsonl and reply.txt next to the
   inputs; every request is also appended to runs/llm-log.jsonl, the call log the budget guard reads (T7). Backends
   (passed to llm.complete() as backend=; docs/decisions/0022):
   - claude-code (default): the Claude Code CLI on the owner's Claude subscription, run locally; no API spend;
   - api: the Anthropic API (the fallback), locally or in the private repository's Actions (pipeline-step.yml, the
     phase-2 route); its spend counts against the monthly budget;
   - fake: pipeline/fake_client.py, for dry runs; its outputs are never placed.
   Only counts, hashes and cost are printed; model calls refuse to run in the Actions of a public repository.
3. place (local, after review). Re-checks the outputs, applies the deterministic parts (the pre-registration header
   from edgar.prereg_header()), flags public-bound text with CJK characters (owner policy 2026-09-25: the public
   repositories are English-first), writes each output to its 00 section F2 destination (public ones on a branch of
   owners-office, never on main), runs thesis-ci lint on both repositories and rolls back on errors. Committing,
   pushing and opening pull requests stay with the operator.
4. dry-run (local). assemble + execute with the fake backend into work/pipeline-dry-run/ in the workspace, outside
   both repositories, so the whole path can be tested without any model access.

This module imports no model SDK: the only model call is llm.complete() (C-LLM-ENTRY).
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import re
import shutil
import sys
import tempfile
import traceback
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

import yaml

from . import edgar, fake_client, llm, registry
from . import outputs as _outputs

REPO_ROOT = Path(__file__).resolve().parents[1]

MANIFEST = registry.MANIFEST_NAME
RUN_RECORD = registry.RUN_RECORD_NAME
PLACEMENT_RECORD = "placement.yml"
CALLS = "calls.jsonl"
REPLY = "reply.txt"
INPUTS_DIR = "inputs"
OUTPUTS_DIR = registry.OUTPUTS_DIR_NAME
ATTEMPTS_DIR = registry.ATTEMPTS_DIR_NAME
MANIFEST_VERSION = 1
RUN_RECORD_VERSION = 1
DRY_RUN_DIR = Path("work") / "pipeline-dry-run"  # under the workspace root, outside both repositories

BACKENDS = llm.BACKENDS  # claude-code, api, fake
DEFAULT_BACKEND = llm.DEFAULT_BACKEND  # owner decision 2026-09-25: the Claude Code CLI on the owner's subscription
REAL_BACKENDS = (llm.CLAUDE_CODE, llm.API)
BUNDLE_REL_RE = re.compile(r"^runs/([A-Z][A-Z0-9.]{0,9}|hq)/(\d{4}-\d{2}-\d{2})-(\d{2}[A-Za-z0-9-]*)$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# Public paths whose uncommitted changes would make the pinned commit misstate the code and roles that run.
PIN_PATHS = ("pipeline", "agents", "constitution/decision-rights.yml", "requirements.txt", "requirements-lint.txt")
DEFAULT_BRANCHES = ("main", "master")
CONTEXT_WARN_TOKENS = 800_000  # the drafting and supervising models have a 1M-token context window
# CJK ideographs, kana, hangul, CJK punctuation and full-width forms: public outputs must be English (2026-09-25).
CJK_RE = re.compile(
    "[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff\uff00-\uffef"
    "\U00020000-\U0002fa1f]"
)
BOT_NOTE = "Lists names, sizes, hashes and cost only; the content is in the files of this pull request."


class RunnerError(RuntimeError):
    """A runner command refused or failed. Messages name steps, files, hashes and counts, never input or output text."""


class BackendUnavailable(RunnerError):
    """The chosen backend cannot be used. Raised before the bundle is touched."""


@dataclasses.dataclass(frozen=True)
class Roots:
    public: Path  # owners-office (this checkout): pipeline code, agents/, the public archive
    private: Path  # owners-office-private: prompts/, runs/, the private archive
    workspace: Path  # the directory holding both checkouts, .env and inputs/


def resolve_roots(public: str | os.PathLike[str] | None = None, private: str | os.PathLike[str] | None = None,
                  workspace: str | os.PathLike[str] | None = None) -> Roots:
    public_root = Path(public).resolve() if public else REPO_ROOT
    private_root = Path(private).resolve() if private else public_root.parent / registry.PRIVATE_REPO
    workspace_root = Path(workspace).resolve() if workspace else public_root.parent
    return Roots(public_root, private_root, workspace_root)


# ---------------------------------------------------------------------------------------------------- small helpers


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha(data: bytes) -> str:
    return registry.sha256_bytes(data)


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp, 0o644)  # mkstemp creates 0600; repository files are world-readable like any checkout
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_yaml(path: Path, data: Any) -> None:
    _write_atomic(path, registry.dump_yaml(data).encode("utf-8"))


def git_head(root: Path) -> str | None:
    out = (registry.git_output(root, "rev-parse", "HEAD") or "").strip()
    return out if SHA_RE.match(out) else None


def git_dirty(root: Path, paths: Sequence[str]) -> list[str]:
    """Paths with uncommitted changes (tracked or untracked) under `paths`; ["<git unavailable>"] without git."""
    out = registry.git_output(root, "status", "--porcelain", "--untracked-files=all", "--", *paths)
    if out is None:
        return ["<git unavailable>"]
    return [line[3:] for line in out.splitlines() if line.strip()]


def git_pushed(root: Path) -> bool:
    return bool((registry.git_output(root, "branch", "-r", "--contains", "HEAD") or "").strip())


def git_branch(root: Path) -> str | None:
    out = (registry.git_output(root, "rev-parse", "--abbrev-ref", "HEAD") or "").strip()
    return out or None


def _paths_summary(paths: Sequence[str], shown: int = 5) -> str:
    listed = ", ".join(paths[:shown])
    return listed + (f" and {len(paths) - shown} more" if len(paths) > shown else "")


def cjk_lines(text: str) -> list[int]:
    """1-based numbers of the lines that contain CJK characters."""
    return [n for n, line in enumerate(text.splitlines(), start=1) if CJK_RE.search(line)]


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------------------------------- bundles


def resolve_bundle(bundle: str | os.PathLike[str], private_root: Path) -> Path:
    """A bundle given as runs/<scope>/<name> (relative to the private repository) or as a directory path."""
    path = Path(bundle)
    if not path.is_absolute():
        candidate = private_root / path
        path = candidate if candidate.is_dir() or not path.is_dir() else path
    path = path.resolve()
    if not (path / MANIFEST).is_file():
        raise RunnerError(f"{bundle}: no {MANIFEST} (looked in {path})")
    return path


def load_manifest(bundle_dir: Path) -> dict[str, Any]:
    data = registry.load_yaml_file(bundle_dir / MANIFEST)
    if not isinstance(data, dict) or data.get("manifest_version") != MANIFEST_VERSION:
        raise RunnerError(f"{bundle_dir / MANIFEST} is not a version-{MANIFEST_VERSION} bundle manifest")
    rel = data.get("bundle")
    if not isinstance(rel, str) or not BUNDLE_REL_RE.match(rel):
        raise RunnerError(f"{bundle_dir / MANIFEST}: bundle {rel!r} is not runs/<TICKER|hq>/<YYYY-MM-DD>-<part>")
    if Path(*bundle_dir.parts[-3:]).as_posix() != rel:
        raise RunnerError(f"{bundle_dir} does not match the manifest's bundle path {rel}")
    return data


def load_run_record(bundle_dir: Path) -> dict[str, Any]:
    data = registry.load_yaml_file(bundle_dir / RUN_RECORD)
    if not isinstance(data, dict):
        raise RunnerError(f"{bundle_dir.name}: no readable {RUN_RECORD}; the bundle has not been executed")
    return data


def inputs_digest(entries: Sequence[Mapping[str, Any]]) -> str:
    return registry.sha256_text(llm.canonical_json({str(e["name"]): str(e["sha256"]) for e in entries}))


def verify_bundle(bundle_dir: Path, manifest: Mapping[str, Any]) -> None:
    """Every input file is present with the manifest's sha256, and inputs/ holds nothing else."""
    problems, listed = [], set()
    entries = manifest.get("inputs") or []
    for entry in entries:
        rel = str(entry.get("file"))
        listed.add(rel)
        path = bundle_dir / rel
        if not path.is_file():
            problems.append(f"{rel}: missing")
            continue
        actual = _sha(path.read_bytes())
        if actual != entry.get("sha256"):
            problems.append(f"{rel}: sha256 {actual[:12]} differs from the manifest's {str(entry.get('sha256'))[:12]}")
    inputs_dir = bundle_dir / INPUTS_DIR
    extra = sorted(p.relative_to(bundle_dir).as_posix() for p in inputs_dir.rglob("*")
                   if p.is_file() and p.relative_to(bundle_dir).as_posix() not in listed) if inputs_dir.is_dir() else []
    problems += [f"{rel}: not listed in the manifest" for rel in extra]
    if entries and manifest.get("inputs_sha256") != inputs_digest(entries):
        problems.append("inputs_sha256 does not match the listed inputs")
    if problems:
        raise RunnerError(f"{manifest.get('bundle')} does not match its manifest:\n- " + "\n- ".join(problems))


def verify_pin(manifest: Mapping[str, Any], public_root: Path, *, strict: bool) -> dict[str, Any]:
    """The public checkout is the pinned commit, with no local changes to the pipeline code or the roles."""
    pinned = manifest.get("pipeline_commit")
    head = git_head(public_root)
    dirty = git_dirty(public_root, PIN_PATHS)
    if strict:
        if not isinstance(pinned, str) or not SHA_RE.match(pinned):
            raise RunnerError("the manifest pins no pipeline commit; assemble it again from a clean checkout")
        if head != pinned:
            raise RunnerError(f"the public checkout is at {head[:12] if head else 'no commit'}; the bundle pins "
                              f"{pinned[:12]} (check that commit out, or assemble again)")
        if dirty:
            raise RunnerError(f"the public checkout has local changes to {len(dirty)} path(s): {_paths_summary(dirty)}")
    return {"head": head, "pinned": pinned, "matches": head == pinned and not dirty}


def verify_prompts(manifest: Mapping[str, Any], private_root: Path) -> tuple[Any, dict[str, str]]:
    """The prompt, 00 (and 00D) have the content recorded at assembly. Returns (llm.PromptPart, output formats)."""
    prompts_dir = private_root / "prompts"
    record = manifest.get("prompt") or {}
    try:
        prompt = llm.load_prompt(str(record.get("id")), prompts_dir)
        rules = llm.load_prompt(llm.RULES_ID, prompts_dir)
        call = llm.prompt_part(prompt, record.get("part"))
        design = llm.load_prompt(llm.DESIGN_ID, prompts_dir) if call.design else None
        formats = llm.output_formats(rules, call)
    except llm.PromptError as exc:
        raise RunnerError(f"{manifest.get('step')}: {exc}") from None
    problems = []
    for key, prompt_file in (("prompt", prompt), ("rules", rules), ("design", design)):
        expected = (manifest.get(key) or {}).get("sha256") if manifest.get(key) else None
        actual = _sha(prompt_file.path.read_bytes()) if prompt_file is not None else None
        if expected != actual:
            name = prompt_file.path.name if prompt_file is not None else key
            problems.append(f"{name}: sha256 {str(actual)[:12]} differs from the manifest's {str(expected)[:12]}")
    if call.label != manifest.get("step"):
        problems.append(f"the prompt part resolves to {call.label}, the manifest says {manifest.get('step')}")
    if problems:
        raise RunnerError("the prompts changed after assembly; assemble the bundle again:\n- " + "\n- ".join(problems))
    return call, formats


def verify_schemas(manifest: Mapping[str, Any], call: Any, schemas_dir: Path | None) -> None:
    """The schema validators llm.py will use are the schemas the model was given as input at assembly."""
    expected = manifest.get("schemas") or {}
    for name in registry.schema_names_for(call):
        try:
            validator = _outputs.schema_validator(name, schemas_dir)
        except _outputs.SchemaUnavailable as exc:
            raise RunnerError(f"thesis-ci schema {name}: {exc}") from None
        digest = registry.schema_digest(validator.schema)
        wanted = (expected.get(name) or {}).get("digest")
        if digest != wanted:
            raise RunnerError(f"thesis-ci schema {name}: the validator here ({digest[:12]}) is not the schema given to "
                              f"the model at assembly ({str(wanted)[:12]}); install the thesis-ci of the pinned "
                              "commit or assemble again")


def read_inputs(bundle_dir: Path, manifest: Mapping[str, Any]) -> dict[str, str]:
    return {str(e["name"]): (bundle_dir / str(e["file"])).read_text(encoding="utf-8") for e in manifest["inputs"]}


# ---------------------------------------------------------------------------------------------------- assemble


def _normalize_company(spec: registry.StepSpec, company: str) -> str | None:
    text = str(company).strip()
    if spec.scope == "hq":
        if text.lower() != "hq":
            raise RunnerError(f"{spec.step} is an HQ step: pass hq as the company")
        return None
    ticker = text.upper()
    if not registry.TICKER_RE.match(ticker):
        raise RunnerError(f"{spec.step} runs for one company: pass its ticker (companies/<TICKER>/), got {company!r}")
    return ticker


def _check_period(spec: registry.StepSpec, period: str) -> None:
    pattern, example = ((registry.QUARTER_RE, "FY2026Q3") if spec.period_kind == "quarter"
                        else (registry.MONTH_RE, "2026-10"))
    if not pattern.match(period):
        raise RunnerError(f"{spec.step} takes a {spec.period_kind} period like {example}, got {period!r}")


def _prompt_record(prompt: Any, private_root: Path, **extra: Any) -> dict[str, Any]:
    return {"id": prompt.id, **extra, "version": prompt.version, "revision": prompt.revision,
            "sha256": _sha(prompt.path.read_bytes()),
            "path": prompt.path.resolve().relative_to(private_root.resolve()).as_posix()
            if _inside(prompt.path, private_root) else prompt.path.name}


def _system_texts(prompt: Any, rules: Any, design: Any, variables: Mapping[str, str]) -> list[str]:
    """The system prompt as llm.complete() builds it: 00, 00D when the part is designed, the filled prompt."""
    return [rules.text, *([design.text] if design else []), llm.fill_variables(prompt, variables)]


def _request_hash(prompt: Any, rules: Any, design: Any, call: Any, variables: Mapping[str, str],
                  inputs: Mapping[str, str]) -> str:
    """The input_sha256 llm.complete() will log for this bundle (same formula), recorded to tie the call to it."""
    run = {k: v for k, v in (("part", call.key), ("pass", call.pass_no), ("mode", call.mode)) if v is not None}
    return llm.input_sha256(prompt.id, "\n\n".join(_system_texts(prompt, rules, design, variables)), inputs, run=run)


def _estimate(prompt: Any, rules: Any, design: Any, call: Any, variables: Mapping[str, str],
              inputs: Mapping[str, str], model: str | None) -> dict[str, Any]:
    content = llm.render_user_content(call, inputs)
    text = "\n\n".join([*_system_texts(prompt, rules, design, variables), content if isinstance(content, str) else ""])
    tokens = fake_client.estimate_tokens(text)
    estimate = {"input_tokens": tokens, "method": "rough: ASCII characters / 4 + one per other character",
                "model": model}
    if model in llm.PRICES_PER_MTOK:
        estimate["input_cost_usd_uncached"] = round(tokens * llm.PRICES_PER_MTOK[model][0] / 1_000_000, 4)
    return estimate


def assemble(step: str, company: str, period: str, *, run_date: dt.date | None = None, roots: Roots | None = None,
             out_root: str | os.PathLike[str] | None = None, edgar_gateway: Any = None, offline: bool = False,
             allow_dirty: bool = False, schemas_dir: str | os.PathLike[str] | None = None,
             today: dt.date | None = None) -> Path:
    """Build the input bundle of one step and return its directory. Nothing is written when anything is missing."""
    roots = roots or resolve_roots()
    try:
        spec = registry.step_spec(step)
    except KeyError as exc:
        raise RunnerError(str(exc.args[0])) from None
    today = today or dt.date.today()
    run_date = run_date or today
    ticker = _normalize_company(spec, company)
    _check_period(spec, period)
    prompts_dir = roots.private / "prompts"
    try:
        prompt = llm.load_prompt(spec.prompt_id, prompts_dir)
        call = llm.prompt_part(prompt, spec.part)
        rules = llm.load_prompt(llm.RULES_ID, prompts_dir)
        design = llm.load_prompt(llm.DESIGN_ID, prompts_dir) if call.design else None
        formats = llm.output_formats(rules, call)
        role = llm.role_definition(call.role, roots.public)
    except llm.PromptError as exc:
        raise RunnerError(f"{step}: {exc}") from None
    if call.label != spec.step:
        raise RunnerError(f"{step}: prompt {spec.prompt_id} part {spec.part} resolves to {call.label}")
    if not role.prompts or (prompt.id not in role.prompts and call.label not in role.prompts):
        raise RunnerError(f"{step}: agents/{role.path.name} does not register {call.label} for role {role.role}")
    schemas = Path(schemas_dir) if schemas_dir else None
    ctx = registry.RunContext(
        step=spec, company=ticker, period=period, run_date=run_date, public_root=roots.public,
        private_root=roots.private, workspace_root=roots.workspace, call=call, formats=formats,
        edgar=edgar_gateway if edgar_gateway is not None else registry.EdgarGateway(offline=offline),
        schemas_dir=schemas,
    )
    notes: list[str] = []
    if ticker:
        try:
            status = ctx.thesis().get("status")
        except registry.MissingInput as exc:
            raise RunnerError(f"{step}: {exc}") from None
        if spec.holdings_only and status != "holding":
            raise RunnerError(f"{step} runs for holdings only (prompt scope); {ticker} is {status!r}")
    if run_date > today:
        notes.append(f"run_date {run_date} is later than the assembly date {today}")

    commit = git_head(roots.public)
    dirty = git_dirty(roots.public, PIN_PATHS)
    pushed = git_pushed(roots.public) if commit else False
    pin_problems = []
    if commit is None:
        pin_problems.append(f"{registry.PUBLIC_REPO} has no commit to pin (git rev-parse HEAD failed)")
    if dirty:
        pin_problems.append(f"uncommitted changes in {len(dirty)} path(s): {_paths_summary(dirty)}")
    if pin_problems and not allow_dirty:
        raise RunnerError("cannot pin the pipeline code:\n- " + "\n- ".join(pin_problems))
    notes += [f"pin: {p}" for p in pin_problems]
    if commit and not pushed:
        notes.append(f"pin: commit {commit[:12]} is on no remote branch yet; the Actions route (pipeline-step.yml) "
                     "can only check out pushed commits")

    rel = f"runs/{ctx.scope}/{run_date.isoformat()}-{call.label}"
    base = Path(out_root).resolve() if out_root else roots.private
    bundle_dir = base / rel
    if bundle_dir.exists():
        raise RunnerError(f"{rel} already exists under {base}; bundles are never overwritten")

    built: dict[str, registry.BuiltInput] = {}
    omitted: list[dict[str, str]] = []
    problems: list[str] = []
    for name, required in call.inputs:
        build = registry.INPUTS.get(name)
        if build is None:
            problems.append(f"{name}: no assembler is registered for this input (pipeline/registry.py)")
            continue
        try:
            built[name] = build(ctx, name)
        except registry.Omit as exc:
            if required:
                problems.append(f"{name}: required, but its assembler left it out ({exc})")
            else:
                omitted.append({"name": name, "reason": str(exc)})
        except registry.MissingInput as exc:
            problems.append(f"{name}: {exc}")
        except edgar.EdgarError as exc:
            problems.append(f"{name}: EDGAR: {exc}")
    if problems:
        raise RunnerError(f"cannot assemble {step} for {ctx.scope} {period}; missing or unreadable inputs:\n- "
                          + "\n- ".join(problems))
    try:
        llm.check_inputs(call, role, list(built))  # 00 section G6, before anything is written
    except llm.PromptError as exc:
        raise RunnerError(f"{step}: {exc}") from None
    try:
        pipeline_fields = spec.pipeline_fields(ctx) if spec.pipeline_fields else {}
        variables = registry.variables_for(ctx)
    except (registry.MissingInput, edgar.EdgarError) as exc:
        raise RunnerError(f"{step}: {exc}") from None
    schema_records = {}
    for name in registry.schema_names_for(call):
        try:
            path, origin = registry.schema_file(name, schemas)
        except registry.MissingInput as exc:
            raise RunnerError(f"{step}: {exc}") from None
        schema_records[name] = {"digest": registry.schema_digest(json.loads(path.read_text(encoding="utf-8"))),
                                "origin": origin}
    texts = {name: b.text for name, b in built.items()}
    model = role.model.get("id")

    entries = []
    for name, required in call.inputs:
        if name not in built:
            continue
        b = built[name]
        data = b.text.encode("utf-8")
        entry: dict[str, Any] = {"name": name, "file": f"{INPUTS_DIR}/{name}.{b.ext}", "required": required,
                                 "bytes": len(data), "sha256": _sha(data), "sources": b.sources}
        if b.empty:
            entry["empty"] = True
        if b.substitute:
            entry["substitute"] = b.substitute
        if b.note:
            entry["note"] = b.note
        entries.append(entry)
    estimate = _estimate(prompt, rules, design, call, variables, texts, model)
    if estimate["input_tokens"] > CONTEXT_WARN_TOKENS:
        notes.append(f"estimated input {estimate['input_tokens']:,} tokens is close to the 1M context window")
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "pipeline_commit": commit,
        "bundle": rel,
        "step": call.label,
        "summary": spec.summary,
        "role": call.role,
        "company": ticker,
        "scope": ctx.scope,
        "period": period,
        "run_date": run_date.isoformat(),
        "created_at": _iso(_utcnow()),
        "created_by": "python -m pipeline.runner assemble",
        "prompt": _prompt_record(prompt, roots.private, part=spec.part, label=call.label),
        "rules": _prompt_record(rules, roots.private),
        "design": _prompt_record(design, roots.private) if design else None,
        "model": {k: role.model.get(k) for k in ("id", "effort", "fallbacks") if role.model.get(k) is not None},
        "variables": variables,
        "context": registry.placement_context(ctx),
        "pipeline_fields": pipeline_fields,
        "schemas": schema_records,
        "pipeline": {"repository": f"{_repo_owner(roots.public)}/{registry.PUBLIC_REPO}", "commit": commit,
                     "dirty_paths": dirty, "pushed": pushed},
        "private": {"commit": git_head(roots.private)},
        "inputs": entries,
        "omitted": omitted,
        "inputs_sha256": inputs_digest(entries),
        "request_sha256": _request_hash(prompt, rules, design, call, variables, texts),
        "estimate": estimate,
        "notes": notes,
    }
    bundle_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".assemble-", dir=bundle_dir.parent))
    try:
        os.chmod(tmp, 0o755)
        for entry in entries:
            _write_atomic(tmp / entry["file"], built[entry["name"]].text.encode("utf-8"))
        write_yaml(tmp / MANIFEST, manifest)
        tmp.rename(bundle_dir)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return bundle_dir


def _repo_owner(public_root: Path) -> str:
    data = registry.load_yaml_file(public_root / "repo.yml")
    return str(data.get("owner")) if isinstance(data, dict) and data.get("owner") else "unknown-owner"


# ---------------------------------------------------------------------------------------------------- execute


def backend_kwargs(backend: str) -> dict[str, Any]:
    """The keyword that selects the backend in llm.complete(). For claude-code, the CLI must be findable (checked
    before the bundle is touched)."""
    if backend not in BACKENDS:
        raise RunnerError(f"backend must be one of {', '.join(BACKENDS)}, got {backend!r}")
    if backend == llm.CLAUDE_CODE:
        try:
            llm.find_claude_binary()
        except llm.ClaudeCodeUnavailable as exc:
            raise BackendUnavailable(f"{exc}") from None
    return {"backend": backend}


def check_environment(backend: str, env: Mapping[str, str], private_root: Path) -> None:
    """Model calls run locally or in the private repository's Actions, never in a public repository's Actions:
    public run logs are world-readable. Outputs always land in a checkout that declares itself private."""
    if backend not in REAL_BACKENDS:
        return
    if env.get("GITHUB_ACTIONS") == "true":
        repository = env.get("GITHUB_REPOSITORY", "")
        if repository.rsplit("/", 1)[-1] != registry.PRIVATE_REPO:
            raise RunnerError(f"model calls never run in the Actions of {repository or 'a public repository'}: its "
                              f"logs are world-readable; run them locally or in {registry.PRIVATE_REPO}")
    repo = registry.load_yaml_file(private_root / "repo.yml")
    if not isinstance(repo, dict) or repo.get("visibility") != "private":
        raise RunnerError(f"{private_root / 'repo.yml'} does not declare visibility: private")


def github_context(env: Mapping[str, str]) -> dict[str, Any] | None:
    if env.get("GITHUB_ACTIONS") != "true":
        return None
    keys = (("repository", "GITHUB_REPOSITORY"), ("run_id", "GITHUB_RUN_ID"), ("run_attempt", "GITHUB_RUN_ATTEMPT"),
            ("workflow", "GITHUB_WORKFLOW"), ("ref", "GITHUB_REF"), ("sha", "GITHUB_SHA"),
            ("server_url", "GITHUB_SERVER_URL"))
    return {key: env.get(var) for key, var in keys}


SAFE_MESSAGE_ERRORS = (RunnerError, llm.BudgetExceeded, llm.PromptError)


def error_summary(exc: BaseException) -> str:
    """One line for logs: the error class and counts. Messages are shown only for errors that never carry content."""
    if isinstance(exc, llm.LLMOutputInvalid):
        return f"LLMOutputInvalid: {len(exc.errors)} validation error(s) after the retry; details in {RUN_RECORD}"
    if isinstance(exc, llm.LLMTruncated):
        return f"LLMTruncated: the reply hit max_tokens; details in {RUN_RECORD}"
    if isinstance(exc, llm.LLMRefusal):
        return f"LLMRefusal: category {exc.category or 'unknown'}; details in {RUN_RECORD}"
    if isinstance(exc, llm.PlanLimitReached):
        return (f"PlanLimitReached: the subscription's usage limit was reached; run again later, or with --backend api "
                f"(counts against the budget); details in {RUN_RECORD}")
    if isinstance(exc, llm.ClaudeCodeUnavailable):
        return (f"ClaudeCodeUnavailable: the Claude Code CLI is missing or not logged in (claude auth login); details "
                f"in {RUN_RECORD}")
    if isinstance(exc, llm.ClaudeCodeError):  # its message may quote the CLI's output, so it is not printed
        return f"ClaudeCodeError: the Claude Code CLI failed; details in {RUN_RECORD}"
    if isinstance(exc, SAFE_MESSAGE_ERRORS):
        return f"{type(exc).__name__}: {exc}"
    status = getattr(exc, "status_code", None)
    return f"{type(exc).__name__}{f' (HTTP {status})' if status else ''}; details in {RUN_RECORD}"


def describe_error(exc: BaseException) -> dict[str, Any]:
    """The full error for run.yml (a private file)."""
    out: dict[str, Any] = {"type": type(exc).__name__, "message": str(exc)[:4000]}
    if isinstance(exc, llm.LLMOutputInvalid):
        out["validation_errors"] = list(exc.errors)[:100]
    if isinstance(exc, llm.LLMRefusal):
        out.update(category=exc.category, explanation=exc.explanation)
    if not isinstance(exc, (*SAFE_MESSAGE_ERRORS, llm.LLMError)):
        out["traceback"] = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)[-8:])[-4000:]
    return out


def _new_log_lines(log: Path, start: int) -> bytes:
    if not log.is_file():
        return b""
    with log.open("rb") as handle:
        handle.seek(start)
        return handle.read()


def summarize_calls(lines: bytes) -> dict[str, Any]:
    """Requests, API cost and token usage of the new log lines; the Claude Code CLI's own estimate is kept apart as
    notional_cost_usd (it is not API spend, decisions/0022)."""
    usage: dict[str, int] = {}
    cost, notional, count = 0.0, 0.0, 0
    for line in lines.decode("utf-8", "replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        count += 1
        cost += float(record.get("cost_usd") or 0.0)
        notional += float(record.get("notional_cost_usd") or 0.0)
        for key, value in (record.get("usage") or {}).items():
            if isinstance(value, int):
                usage[key] = usage.get(key, 0) + value
    summary: dict[str, Any] = {"requests": count, "cost_usd": round(cost, 6), "usage": usage}
    if notional:
        summary["notional_cost_usd"] = round(notional, 6)
    return summary


def _write_outputs(bundle_dir: Path, call: Any, result: llm.LLMResult) -> dict[str, dict[str, Any]]:
    out_dir = bundle_dir / OUTPUTS_DIR
    out_dir.mkdir(parents=True, exist_ok=False)
    entries: dict[str, dict[str, Any]] = {}
    for name, required in call.outputs:
        parsed = result.outputs.get(name)
        if parsed is None:
            entries[name] = {"status": "absent", "required": required}
            continue
        if parsed.empty:
            entries[name] = {"status": "empty", "format": parsed.format}
            continue
        ext = "yml" if parsed.format == _outputs.YAML else "md"
        data = parsed.text.encode("utf-8")
        _write_atomic(out_dir / f"{name}.{ext}", data)
        entry: dict[str, Any] = {"status": "written", "file": f"{OUTPUTS_DIR}/{name}.{ext}", "format": parsed.format,
                                 "bytes": len(data), "sha256": _sha(data)}
        if parsed.schema:
            entry["schema"] = parsed.schema
        entry["generated_by_injected"] = parsed.generated_by_injected
        if parsed.pipeline_fields:
            entry["pipeline_fields"] = list(parsed.pipeline_fields)
        entries[name] = entry
    return entries


def planned_placements(manifest: Mapping[str, Any], entries: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Where each written output goes under 00 section F2 (outputs.place), as recorded in run.yml and the PR."""
    rows = []
    for name, entry in entries.items():
        if entry.get("status") != "written":
            continue
        try:
            placements = _outputs.place(name, prompt_id=str(manifest["prompt"]["id"]), part_id=str(manifest["step"]),
                                        fmt=str(entry["format"]), **(manifest.get("context") or {}))
        except _outputs.PlacementError as exc:
            rows.append({"output": name, "error": str(exc)})
            continue
        for p in placements:
            row = {"output": name, "repo": p.repo, "visibility": p.visibility, "path": p.path, "action": p.action}
            if p.note:
                row["note"] = p.note
            rows.append(row)
    return rows


def _fake_context(manifest: Mapping[str, Any], inputs: Mapping[str, str]) -> dict[str, Any]:
    domain = "other"
    if "thesis" in inputs:
        data = yaml.safe_load(inputs["thesis"])
        if isinstance(data, dict) and data.get("domain"):
            domain = str(data["domain"])
    return {"company": manifest.get("company"), "period": manifest.get("period"), "run_date": manifest["run_date"],
            "label": manifest.get("step"), "domain": domain, "pipeline_fields": manifest.get("pipeline_fields") or {}}


def _archive_failed_attempt(bundle_dir: Path) -> int:
    """--retry: move the failed attempt's run.yml, calls.jsonl and reply.txt to attempts/<n>/; return n."""
    record = load_run_record(bundle_dir)
    if record.get("status") != "failed" or (bundle_dir / OUTPUTS_DIR).exists():
        raise RunnerError(f"{bundle_dir.name}: --retry only runs a failed bundle again (this one is "
                          f"{record.get('status')})")
    base = bundle_dir / ATTEMPTS_DIR
    number = 1 + max((int(p.name) for p in base.glob("*") if p.name.isdigit()), default=0)
    target = base / str(number)
    target.mkdir(parents=True)
    for name in (RUN_RECORD, CALLS, REPLY):
        if (bundle_dir / name).exists():
            shutil.move(str(bundle_dir / name), str(target / name))
    return number


def execute(bundle: str | os.PathLike[str], *, roots: Roots | None = None, backend: str | None = None,
            log_path: str | os.PathLike[str] | None = None, client: Any = None, retry: bool = False,
            allow_unpinned: bool = False, schemas_dir: str | os.PathLike[str] | None = None,
            env: Mapping[str, str] | None = None, out: TextIO | None = None) -> dict[str, Any]:
    """Run the bundle's model call once and record it next to the inputs. Returns the run record.

    Refusals before the call (an unavailable backend, the wrong environment, a bundle that does not match its
    manifest, changed prompts or schemas, an unpinned checkout) raise RunnerError and write nothing, so the bundle
    can still be executed later. Once llm.complete() is entered, the outcome is always recorded: outputs/ on success,
    run.yml with status failed otherwise, and calls.jsonl with every request llm.py logged (the same lines go to the
    call log at `log_path`, by default runs/llm-log.jsonl of the private checkout). `client` injects a test double.
    Only counts, hashes and cost are printed. `backend` defaults to OWNERS_OFFICE_BACKEND, else claude-code.
    """
    try:
        backend = llm.resolve_backend(backend)
    except ValueError as exc:
        raise RunnerError(str(exc)) from None
    env = os.environ if env is None else env
    out = out or sys.stdout
    roots = roots or resolve_roots()
    schemas = Path(schemas_dir) if schemas_dir else None
    bundle_dir = resolve_bundle(bundle, roots.private)
    manifest = load_manifest(bundle_dir)
    rel = manifest["bundle"]
    backend_args = backend_kwargs(backend)
    check_environment(backend, env, roots.private)
    executed = (bundle_dir / RUN_RECORD).exists() or (bundle_dir / OUTPUTS_DIR).exists()
    if executed and not retry:
        raise RunnerError(f"{rel} was already executed ({RUN_RECORD} or {OUTPUTS_DIR}/ exists); --retry runs a failed "
                          "bundle again, a succeeded one is final")
    verify_bundle(bundle_dir, manifest)
    pin = verify_pin(manifest, roots.public, strict=backend in REAL_BACKENDS and not allow_unpinned)
    pin["unpinned_allowed"] = allow_unpinned
    call, formats = verify_prompts(manifest, roots.private)
    verify_schemas(manifest, call, schemas)
    inputs = read_inputs(bundle_dir, manifest)
    attempt = _archive_failed_attempt(bundle_dir) + 1 if executed else 1
    if backend == "fake" and client is None:
        client = fake_client.FakeClient(fake_client.placeholder_reply(call.outputs, formats,
                                                                      _fake_context(manifest, inputs)))
        client_kind = "fake"
    else:
        client_kind = "injected" if client is not None else backend
    log = Path(log_path) if log_path else roots.private / registry.LLM_LOG_REL
    start = log.stat().st_size if log.is_file() else 0
    record: dict[str, Any] = {
        "run_version": RUN_RECORD_VERSION,
        "bundle": rel,
        "step": manifest["step"],
        "status": "failed",
        "backend": backend,
        "client": client_kind,
        "attempt": attempt,
        "started_at": _iso(_utcnow()),
        "github": github_context(env),
        "pipeline": pin,
    }
    result = None
    failure: BaseException | None = None
    try:
        result = llm.complete(
            str(manifest["role"]), str(manifest["prompt"]["id"]), inputs,
            part=manifest["prompt"].get("part"), variables=manifest.get("variables") or {},
            pipeline_fields=manifest.get("pipeline_fields") or None, client=client, log_path=log,
            repo_root=roots.public, prompts_dir=roots.private / "prompts", schemas_dir=schemas, **backend_args,
        )
    except Exception as exc:  # recorded in run.yml; only error_summary() is printed
        failure = exc
        record["error"] = describe_error(exc)
    reply = result.text if result is not None else getattr(getattr(failure, "result", None), "text", None)
    lines = _new_log_lines(log, start)
    record.update(summarize_calls(lines))
    if result is not None:
        entries = _write_outputs(bundle_dir, call, result)
        record.update(
            status="succeeded", model=result.model, requested_model=result.requested_model, effort=result.effort,
            fallbacks=result.fallbacks, served_by_fallback=result.served_by_fallback, attempts=result.attempts,
            stop_reason=result.stop_reason, input_sha256=result.input_sha256,
            request_matches_manifest=result.input_sha256 == manifest.get("request_sha256"),
            reply_sha256=result.output_sha256,
            versions={k: getattr(result, k) for k in ("prompt_version", "prompt_revision", "rules_version",
                                                       "rules_revision", "design_version", "design_revision")},
            generated_by=dict(result.generated_by), outputs=entries,
            placements=planned_placements(manifest, entries),
        )
    if reply is not None:
        _write_atomic(bundle_dir / REPLY, reply.encode("utf-8"))
        record["reply"] = {"file": REPLY, "sha256": registry.sha256_text(reply)}
    if lines:
        _write_atomic(bundle_dir / CALLS, lines)
        record["calls_file"] = CALLS
    record["call_log"] = (log.resolve().relative_to(roots.private.resolve()).as_posix()
                          if _inside(log, roots.private) else str(log))
    record["finished_at"] = _iso(_utcnow())
    write_yaml(bundle_dir / RUN_RECORD, record)
    _print_execution(record, failure, out)
    return record


def _print_execution(record: Mapping[str, Any], failure: BaseException | None, out: TextIO) -> None:
    head = f"execute {record['bundle']} (backend {record['backend']}, attempt {record['attempt']}): {record['status']}"
    if failure is not None:
        head += f" ({error_summary(failure)})"
    print(head, file=out)
    usage = record.get("usage") or {}
    print(f"  {record.get('requests', 0)} request(s); tokens in {usage.get('input_tokens', 0):,} / out "
          f"{usage.get('output_tokens', 0):,} / cache write {usage.get('cache_creation_input_tokens', 0):,} / cache "
          f"read {usage.get('cache_read_input_tokens', 0):,}; API cost {record.get('cost_usd', 0.0):.4f} USD"
          + (f" (Claude Code's own estimate {record['notional_cost_usd']:.4f} USD, not API spend)"
             if record.get("notional_cost_usd") else ""), file=out)
    if record.get("status") == "succeeded":
        print(f"  model {record.get('model')} (requested {record.get('requested_model')}), attempts "
              f"{record.get('attempts')}, stop {record.get('stop_reason')}", file=out)
        print(f"  input_sha256 {record.get('input_sha256')} (matches the manifest: "
              f"{'yes' if record.get('request_matches_manifest') else 'NO'})", file=out)
        for name, entry in (record.get("outputs") or {}).items():
            if entry.get("status") == "written":
                print(f"  output {name}: {entry['format']}, {entry['bytes']:,} bytes, sha256 {entry['sha256'][:12]}",
                      file=out)
            else:
                print(f"  output {name}: {entry.get('status')}", file=out)
    if record.get("github") is None and record.get("backend") in REAL_BACKENDS:
        print(f"  next: commit {record['bundle']} and {registry.LLM_LOG_REL} in {registry.PRIVATE_REPO} (a branch "
              f"and pull request for review); after the merge run: python -m pipeline.runner place {record['bundle']}",
              file=out)


# ---------------------------------------------------------------------------------------------------- dry run


def dry_run(step: str, company: str, period: str, *, run_date: dt.date | None = None, roots: Roots | None = None,
            out_root: str | os.PathLike[str] | None = None, replace: bool = False, edgar_gateway: Any = None,
            offline: bool = False, schemas_dir: str | os.PathLike[str] | None = None,
            today: dt.date | None = None, out: TextIO | None = None) -> tuple[Path, dict[str, Any]]:
    """assemble + execute with the fake backend, into a directory outside both repositories."""
    roots = roots or resolve_roots()
    base = Path(out_root).resolve() if out_root else roots.workspace / DRY_RUN_DIR
    if _inside(base, roots.public) or _inside(base, roots.private):
        raise RunnerError(f"the dry-run directory {base} must be outside both repositories")
    spec = registry.STEPS.get(step)
    if spec is not None and replace:
        scope = "hq" if spec.scope == "hq" else str(company).strip().upper()
        existing = base / "runs" / scope / f"{(run_date or today or dt.date.today()).isoformat()}-{spec.step}"
        if existing.is_dir():
            shutil.rmtree(existing)
    bundle_dir = assemble(step, company, period, run_date=run_date, roots=roots, out_root=base,
                          edgar_gateway=edgar_gateway, offline=offline, allow_dirty=True, schemas_dir=schemas_dir,
                          today=today)
    log = base / registry.LLM_LOG_REL  # a copy of the real call log, so the budget guard sees the real spend
    seed = roots.private / registry.LLM_LOG_REL
    _write_atomic(log, seed.read_bytes() if seed.is_file() else b"")
    record = execute(bundle_dir, roots=roots, backend="fake", log_path=log, schemas_dir=schemas_dir, out=out)
    return bundle_dir, record


# ---------------------------------------------------------------------------------------------------- show, PR body


def _source_summary(source: Mapping[str, Any]) -> str:
    kind = source.get("kind")
    if kind == "repo_file":
        revision = str(source.get("revision") or "")
        revision = "uncommitted" if revision.startswith("sha256:") else revision[:12]
        return f"{source.get('repo')}:{source.get('path')} @ {revision}"
    if kind == "workspace_file":
        return f"workspace:{source.get('path')}"
    if kind == "edgar" and source.get("accession"):
        return f"EDGAR [src:{source.get('tag')}] {source.get('document')} ({source.get('accession')})"
    if kind == "edgar":
        return f"EDGAR {source.get('detail')}"
    if kind == "thesis-ci":
        return f"{source.get('schema')}.schema.json ({source.get('origin')})"
    if kind == "parameter":
        return str(source.get("detail"))
    if kind == "run_output":
        return f"{source.get('run')}/{source.get('output')}"
    if kind == "repo_glob":
        return f"{source.get('repo')}:{', '.join(source.get('paths') or [])}"
    if kind in ("git_log", "runs", "call_log"):
        return f"{kind} {source.get('repo')}:{source.get('path', '')}".rstrip(":")
    return str(kind)


def describe_bundle(bundle_dir: Path) -> str:
    """A summary of a bundle for people: names, sizes, hashes, sources and notes, never the content."""
    manifest = load_manifest(bundle_dir)
    prompt = manifest.get("prompt") or {}
    rules = manifest.get("rules") or {}
    pipeline = manifest.get("pipeline") or {}
    lines = [
        f"{manifest['bundle']}  step {manifest['step']}  role {manifest['role']}  scope {manifest['scope']}  "
        f"period {manifest['period']}  run_date {manifest['run_date']}",
        f"prompt {prompt.get('id')} part {prompt.get('part')} v{prompt.get('version')} "
        f"(sha256 {str(prompt.get('sha256'))[:12]}); rules 00 v{rules.get('version')} (sha256 "
        f"{str(rules.get('sha256'))[:12]}); model {(manifest.get('model') or {}).get('id')}",
        f"pipeline commit {str(manifest.get('pipeline_commit'))[:12]}; pushed {pipeline.get('pushed')}; "
        f"uncommitted: {_paths_summary(pipeline.get('dirty_paths') or []) or 'none'}",
        f"request_sha256 {manifest.get('request_sha256')}",
        f"inputs ({len(manifest.get('inputs') or [])}):",
    ]
    for entry in manifest.get("inputs") or []:
        flags = []
        if entry.get("empty"):
            flags.append("EMPTY")
        if entry.get("substitute"):
            flags.append(f"STANDS IN FOR {entry['substitute']}")
        ext = Path(str(entry["file"])).suffix.lstrip(".")
        sources = "; ".join(_source_summary(s) for s in entry.get("sources") or [])
        lines.append(f"  {entry['name']:<20} {ext:<3} {entry['bytes']:>9,} B  sha256 {str(entry['sha256'])[:12]}  "
                     f"{sources}{'  [' + ', '.join(flags) + ']' if flags else ''}")
        if entry.get("note"):
            lines.append(f"      note: {entry['note']}")
    for item in manifest.get("omitted") or []:
        lines.append(f"  omitted {item['name']}: {item['reason']}")
    fields = manifest.get("pipeline_fields") or {}
    for output, values in fields.items():
        shown = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False, default=str)}" for k, v in values.items())
        lines.append(f"pipeline fields of {output}: {shown}")
    estimate = manifest.get("estimate") or {}
    lines.append(f"estimate: ~{estimate.get('input_tokens', 0):,} input tokens ({estimate.get('method')}); "
                 f"uncached input cost ~{estimate.get('input_cost_usd_uncached')} USD on {estimate.get('model')}")
    for note in manifest.get("notes") or []:
        lines.append(f"note: {note}")
    record = registry.load_yaml_file(bundle_dir / RUN_RECORD)
    if isinstance(record, dict):
        lines.append(f"run: {record.get('status')} (backend {record.get('backend')}, client {record.get('client')}, "
                     f"attempt {record.get('attempt')}); {record.get('requests', 0)} request(s); cost "
                     f"{record.get('cost_usd', 0.0)} USD")
        for name, entry in (record.get("outputs") or {}).items():
            if entry.get("status") == "written":
                lines.append(f"  output {name:<16} {entry['format']:<8} {entry['bytes']:>8,} B  sha256 "
                             f"{entry['sha256'][:12]}")
            else:
                lines.append(f"  output {name:<16} {entry.get('status')}")
        for row in record.get("placements") or []:
            where = f"{row.get('repo')}:{row.get('path')}" if row.get("path") else row.get("error")
            lines.append(f"  place  {row['output']:<16} -> {where} ({row.get('visibility')}, {row.get('action')})")
    return "\n".join(lines)


def pr_body(bundle_dir: Path) -> str:
    """The body of the private pull request that carries an executed bundle: no input or output text."""
    manifest = load_manifest(bundle_dir)
    record = registry.load_yaml_file(bundle_dir / RUN_RECORD) or {}
    usage = record.get("usage") or {}
    status = record.get("status", "not executed")
    error = (record.get("error") or {}).get("type")
    lines = [
        f"## Pipeline step {manifest['step']} | {manifest['scope']} | {manifest['period']} | run date "
        f"{manifest['run_date']}",
        "",
        f"- Status: **{status}**" + (f" ({error}; details in `{RUN_RECORD}`)" if error else "")
        + f"; backend {record.get('backend', '-')}, attempt {record.get('attempt', '-')}",
        f"- Role `{manifest['role']}`; model {record.get('model') or (manifest.get('model') or {}).get('id')}; "
        f"requests {record.get('requests', 0)}; stop reason {record.get('stop_reason', '-')}",
        f"- Tokens: input {usage.get('input_tokens', 0):,}, output {usage.get('output_tokens', 0):,}, cache write "
        f"{usage.get('cache_creation_input_tokens', 0):,}, cache read {usage.get('cache_read_input_tokens', 0):,}; "
        f"cost {record.get('cost_usd', 0.0):.4f} USD (logged in `{registry.LLM_LOG_REL}`)",
        f"- Request hash `{record.get('input_sha256', '-')}` (matches the manifest: "
        f"{'yes' if record.get('request_matches_manifest') else 'no'})",
        f"- Pipeline code: {(manifest.get('pipeline') or {}).get('repository')}@{str(manifest.get('pipeline_commit'))[:12]}"
        f"; prompt {manifest['prompt']['id']} v{manifest['prompt']['version']}, rules 00 v{manifest['rules']['version']}",
        "",
        "### Outputs",
        "",
        "| output | status | format | bytes | sha256 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name, entry in (record.get("outputs") or {}).items():
        lines.append(f"| `{name}` | {entry.get('status')} | {entry.get('format', '')} | {entry.get('bytes', '')} | "
                     f"`{str(entry.get('sha256', ''))[:12]}` |")
    lines += ["", "### Destinations (00 section F2)", ""]
    for row in record.get("placements") or []:
        lines.append(f"- `{row['output']}` -> {row.get('repo')}:`{row.get('path')}` ({row.get('visibility')})")
    lines += ["", "### Inputs", "", "| input | bytes | sha256 | flags |", "| --- | --- | --- | --- |"]
    for entry in manifest.get("inputs") or []:
        flags = ", ".join(f for f in ("empty" if entry.get("empty") else "",
                                      f"stands in for {entry['substitute']}" if entry.get("substitute") else "") if f)
        lines.append(f"| `{entry['name']}` | {entry['bytes']} | `{str(entry['sha256'])[:12]}` | {flags} |")
    lines += [
        "",
        "### Next",
        "",
        "1. Review `inputs/`, `outputs/` and `run.yml` in this pull request, then merge it.",
        f"2. Locally: `python -m pipeline.runner place {manifest['bundle']}` validates the outputs (thesis-ci lint), "
        "writes the public ones on a branch of owners-office for a pull request and the private ones here.",
        "",
        BOT_NOTE,
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------------------------------- place


@dataclasses.dataclass
class PlannedWrite:
    output: str
    repo: str
    visibility: str
    path: str
    content: bytes
    note: str = ""
    status: str = "new"  # new | same | header-update
    remove: list[str] = dataclasses.field(default_factory=list)
    cjk_lines: list[int] = dataclasses.field(default_factory=list)


def _horizon(items: Sequence[Any]) -> str:
    horizons = {item.get("horizon") for item in items if isinstance(item, dict)}
    return horizons.pop() if len(horizons) == 1 and horizons <= {"quarter", "18m"} else "mixed"


def prereg_document(text: str, header: Mapping[str, Any], manifest: Mapping[str, Any],
                    schemas_dir: Path | None) -> tuple[str, Any, list[str], list[str]]:
    """Apply the pipeline's header (company, event, deadline, author, horizon) to a 15A output and validate it.

    Returns (text, data, notes, problems). The items are never touched (15A: the pipeline only rewrites the header).
    """
    data = _outputs.load_yaml_text(text)
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return text, data, [], ["the prereg output has no items list"]
    fields = {"company": manifest["company"], "event": dict(header["event"]), "deadline": header["deadline"],
              "author": "system", "horizon": _horizon(data["items"])}
    notes = [f"prereg.{key}: the pipeline's value replaces the model's" for key, value in fields.items()
             if key in data and _outputs.jsonable(data[key]) != _outputs.jsonable(value)]
    new_text, merged = _outputs.apply_fields(text, data, fields)
    problems = []
    try:
        validator = _outputs.schema_validator("prereg", schemas_dir)
        problems += _outputs.schema_errors(validator, merged, "prereg")
    except _outputs.SchemaUnavailable as exc:
        problems.append(f"prereg schema unavailable: {exc}")
    prefix = f"{manifest['company']}-{manifest['period']}-"
    problems += [f"item id {item.get('id')!r} does not start with {prefix}" for item in merged["items"]
                 if isinstance(item, dict) and not str(item.get("id", "")).startswith(prefix)]
    if merged["event"].get("period") != manifest["period"]:
        problems.append(f"event.period {merged['event'].get('period')} is not {manifest['period']}")
    return new_text, merged, notes, problems


def prereg_header_for(manifest: Mapping[str, Any], roots: Roots, *, announced: str | None,
                      window: Sequence[str] | None, edgar_gateway: Any) -> tuple[dict[str, Any], str]:
    """The header recorded at assembly, or a fresh one from EDGAR when the company has announced its date."""
    recorded = (manifest.get("pipeline_fields") or {}).get("prereg") or {}
    if announced is None and window is None:
        if not recorded.get("event") or not recorded.get("deadline"):
            raise RunnerError("the manifest records no pre-registration header; pass --announced or --window")
        return {"event": recorded["event"], "deadline": recorded["deadline"]}, "recorded at assembly"
    gateway = edgar_gateway if edgar_gateway is not None else registry.EdgarGateway()
    filer = edgar.load_filer(str(manifest["company"]), roots.public)
    estimate = edgar.next_release(filer.cik, str(manifest["period"]), fiscal_year_end=filer.fiscal_year_end,
                                  filer_type=filer.type, form=filer.earnings_form, announced=announced,
                                  window=tuple(window) if window else None, client=gateway.client)
    return estimate.prereg_header(), estimate.basis


def _check_existing(write: PlannedWrite, root: Path, now: dt.datetime, merged: Any) -> str | None:
    """Decide what to do with a destination that exists. Returns a problem, or None (status is updated)."""
    dest = root / write.path
    existing = dest.read_bytes()
    if existing == write.content:
        write.status = "same"
        return None
    if write.output != "prereg":
        return f"{write.repo}:{write.path} exists with different content; the runner never overwrites a file"
    try:
        old = _outputs.load_yaml_text(existing.decode("utf-8"))
    except Exception:  # noqa: BLE001 - any unreadable file is refused below
        old = None
    if not isinstance(old, dict) or _outputs.jsonable(old.get("items")) != _outputs.jsonable(merged.get("items")):
        return (f"{write.repo}:{write.path} exists with different items; pre-registered items are never rewritten "
                "(15A, decisions/0018)")
    try:
        old_deadline = dt.datetime.fromisoformat(str(old.get("deadline")).replace("Z", "+00:00"))
    except ValueError:
        return f"{write.repo}:{write.path} has an unreadable deadline"
    if old_deadline.tzinfo is None or now >= old_deadline:
        return f"{write.repo}:{write.path}: its deadline {old.get('deadline')} has passed; the file is immutable"
    write.status = "header-update"
    proof = f"{write.path}.ots"
    if (root / proof).exists():
        write.remove.append(proof)  # decisions/0018: drop the old proof in the same PR; the workflow stamps again
    return None


def ensure_public_branch(public_root: Path, wanted: str | None, default_name: str, paths: Sequence[str]) -> str:
    """Put the public checkout on a branch other than main before any public output is written."""
    current = git_branch(public_root)
    if current is None:
        raise RunnerError(f"{public_root} is not a git checkout")
    dirty = git_dirty(public_root, paths)
    if dirty:
        raise RunnerError(f"uncommitted changes to {', '.join(dirty)} in {registry.PUBLIC_REPO}; commit or stash them")
    target = wanted or (current if current not in (*DEFAULT_BRANCHES, "HEAD") else default_name)
    if target in DEFAULT_BRANCHES:
        raise RunnerError("public outputs never go straight to main; name a branch with --branch")
    if target != current:
        exists = registry.git_output(public_root, "rev-parse", "--verify", "--quiet", f"refs/heads/{target}") is not None
        if registry.git_output(public_root, "switch", *((target,) if exists else ("-c", target))) is None:
            raise RunnerError(f"git switch to {target} failed in {registry.PUBLIC_REPO}")
    return target


def run_lint(roots: Roots, today: dt.date) -> dict[str, Any]:
    """thesis-ci lint of both repositories, as in CLAUDE.md (the private one with the public as counterpart)."""
    try:
        from thesis_ci import engine
    except ImportError:
        raise RunnerError("thesis-ci is not installed (pip install -r requirements-lint.txt); --no-lint skips the "
                          "validation") from None

    def rows(report: Any) -> dict[str, Any]:
        return {"errors": [f.as_dict() for f in report.errors], "warnings": len(report.warnings)}

    return {"public": rows(engine.run(roots.public, None, today)),
            "private": rows(engine.run(roots.private, roots.public, today))}


def _lint_key(side: str, error: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return side, str(error.get("check")), str(error.get("file")), str(error.get("message"))  # lines may shift


def lint_delta(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    """The lint result after placing, split into errors the placement introduced and errors that were there before."""
    known = {_lint_key(side, e) for side in ("public", "private") for e in before[side]["errors"]}
    out: dict[str, Any] = {}
    for side in ("public", "private"):
        errors = after[side]["errors"]
        new = [e for e in errors if _lint_key(side, e) not in known]
        out[side] = {"errors": new, "pre_existing_errors": len(errors) - len(new), "warnings": after[side]["warnings"]}
    return out


def place(bundle: str | os.PathLike[str], *, roots: Roots | None = None, branch: str | None = None,
          announced: str | None = None, window: Sequence[str] | None = None, check: bool = False,
          allow_cjk: bool = False, allow_fake: bool = False, lint: bool = True, now: dt.datetime | None = None,
          edgar_gateway: Any = None, schemas_dir: str | os.PathLike[str] | None = None,
          out: TextIO | None = None) -> dict[str, Any]:
    """Copy each output of an executed bundle to its 00 section F2 destination. Returns the placement record."""
    roots = roots or resolve_roots()
    out = out or sys.stdout
    schemas = Path(schemas_dir) if schemas_dir else None
    now = now or _utcnow()
    bundle_dir = resolve_bundle(bundle, roots.private)
    manifest = load_manifest(bundle_dir)
    record = load_run_record(bundle_dir)
    rel = manifest["bundle"]
    if record.get("status") != "succeeded":
        raise RunnerError(f"{rel} did not succeed (status {record.get('status')}); there is nothing to place")
    real = record.get("backend") in REAL_BACKENDS and record.get("client") in REAL_BACKENDS
    if not real and not allow_fake:
        raise RunnerError(f"{rel} was produced by the {record.get('client')} client (a dry run or a test); its outputs "
                          "are placeholders and are never placed")
    entries = {k: v for k, v in (record.get("outputs") or {}).items() if v.get("status") == "written"}
    header, basis = (prereg_header_for(manifest, roots, announced=announced, window=window,
                                       edgar_gateway=edgar_gateway) if "prereg" in entries else (None, None))
    writes: list[PlannedWrite] = []
    problems: list[str] = []
    warnings: list[str] = []
    merged_prereg: Any = None
    for name, entry in entries.items():
        data = (bundle_dir / str(entry["file"])).read_bytes()
        if _sha(data) != entry.get("sha256"):
            problems.append(f"{entry['file']}: sha256 differs from {RUN_RECORD}")
            continue
        text = data.decode("utf-8")
        if name == "prereg" and header is not None:
            text, merged_prereg, notes, prereg_problems = prereg_document(text, header, manifest, schemas)
            warnings += notes
            problems += [f"prereg: {p}" for p in prereg_problems]
        try:
            placements = _outputs.place(name, prompt_id=str(manifest["prompt"]["id"]), part_id=str(manifest["step"]),
                                        fmt=str(entry["format"]), **(manifest.get("context") or {}))
        except _outputs.PlacementError as exc:
            problems.append(f"{name}: {exc}")
            continue
        for p in placements:
            if p.action != "write" or p.path is None:
                problems.append(f"{name}: placement action {p.action!r} is not supported by the runner yet "
                                "(decisions/0019)")
                continue
            write = PlannedWrite(name, p.repo, p.visibility, p.path, text.encode("utf-8"), note=p.note)
            if p.visibility == _outputs.PUBLIC:
                write.cjk_lines = cjk_lines(text)
            writes.append(write)
    for write in writes:
        root = roots.public if write.repo == registry.PUBLIC_REPO else roots.private
        if (root / write.path).exists():
            problem = _check_existing(write, root, now, merged_prereg or {})
            if problem:
                problems.append(problem)
    if header is not None:
        deadline = dt.datetime.fromisoformat(str(header["deadline"]).replace("Z", "+00:00"))
        if now >= deadline:
            problems.append(f"the pre-registration deadline {header['deadline']} has passed")
        elif now > edgar.merge_by(deadline):
            warnings.append(f"the merge-by time {edgar.iso(edgar.merge_by(deadline))} (72 hours before the deadline) "
                            "has passed")
    for w in (w for w in writes if w.cjk_lines):
        message = (f"{w.repo}:{w.path}: public-bound output contains CJK text on {len(w.cjk_lines)} line(s) "
                   f"(first: {', '.join(map(str, w.cjk_lines[:5]))}); the public repositories are English-first "
                   "(owner policy 2026-09-25)")
        if allow_cjk:
            warnings.append(message + "; placed anyway with --allow-cjk")
        else:
            problems.append(message + "; rerun with English output, or pass --allow-cjk")
    report: dict[str, Any] = {
        "placement_version": 1,
        "bundle": rel,
        "placed_at": _iso(now),
        "check_only": check,
        "prereg_header": {"basis": basis, **header} if header else None,
        "files": [{"output": w.output, "repo": w.repo, "visibility": w.visibility, "path": w.path,
                   "status": w.status, "sha256": _sha(w.content), "remove": w.remove,
                   **({"cjk_lines": len(w.cjk_lines)} if w.cjk_lines else {})} for w in writes],
        "warnings": warnings,
        "allow_cjk": allow_cjk,
    }
    if problems:
        raise RunnerError(f"cannot place {rel}:\n- " + "\n- ".join(problems))
    if check:
        _print_placement(report, out)
        return report
    changed = [w for w in writes if w.status != "same"]
    public_changes = [w for w in changed if w.visibility == _outputs.PUBLIC]
    if public_changes:
        default_branch = f"pipeline/{manifest['scope']}-{manifest['run_date']}-{manifest['step']}"
        report["public_branch"] = ensure_public_branch(
            roots.public, branch, default_branch, [p for w in public_changes for p in (w.path, *w.remove)])
    baseline = run_lint(roots, now.date()) if lint and changed else None
    undo: list[tuple[Path, bytes | None]] = []
    try:
        for write in changed:
            root = roots.public if write.repo == registry.PUBLIC_REPO else roots.private
            dest = root / write.path
            undo.append((dest, dest.read_bytes() if dest.exists() else None))
            _write_atomic(dest, write.content)
            for stale in write.remove:
                path = root / stale
                undo.append((path, path.read_bytes()))
                path.unlink()
        if baseline is not None:
            report["lint"] = lint_delta(baseline, run_lint(roots, now.date()))
            errors = [(side, e) for side in ("public", "private") for e in report["lint"][side]["errors"]]
            if errors:
                listed = "\n- ".join(f"{side}: {e['file']}:{e.get('line') or ''} [{e['check']}] {e['message']}"
                                     for side, e in errors[:40])
                raise RunnerError(f"thesis-ci lint found {len(errors)} new error(s); nothing was placed:\n- {listed}")
            before = sum(report["lint"][side]["pre_existing_errors"] for side in ("public", "private"))
            if before:
                warnings.append(f"thesis-ci lint already reported {before} error(s) before this placement (not caused "
                                "by it); CI still requires them fixed before the merge")
    except BaseException:
        for path, previous in reversed(undo):
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                _write_atomic(path, previous)
        raise
    write_yaml(bundle_dir / PLACEMENT_RECORD, report)
    _print_placement(report, out)
    return report


def _print_placement(report: Mapping[str, Any], out: TextIO) -> None:
    verb = "would place" if report.get("check_only") else "placed"
    print(f"{verb} {report['bundle']}:", file=out)
    for f in report["files"]:
        extra = f"; removes {', '.join(f['remove'])}" if f.get("remove") else ""
        print(f"  {f['output']:<16} -> {f['repo']}:{f['path']} ({f['visibility']}, {f['status']}{extra})", file=out)
    if report.get("prereg_header"):
        header = report["prereg_header"]
        print(f"  prereg header: expected_release {header['event'].get('expected_release')}, placeholder "
              f"{header['event'].get('placeholder')}, deadline {header['deadline']} ({header['basis']})", file=out)
    for warning in report.get("warnings") or []:
        print(f"  warning: {warning}", file=out)
    if report.get("lint"):
        lint = report["lint"]
        print("  thesis-ci lint: " + "; ".join(
            f"{side} {len(lint[side]['errors'])} new error(s) ({lint[side]['pre_existing_errors']} pre-existing), "
            f"{lint[side]['warnings']} warning(s)" for side in ("public", "private")), file=out)
    if report.get("public_branch"):
        print(f"  public files are on branch {report['public_branch']} of {registry.PUBLIC_REPO}: commit, push and "
              "open a pull request; for a pre-registration the timestamp workflow stamps it after the merge", file=out)
    if not report.get("check_only"):
        print(f"  private files: commit them in {registry.PRIVATE_REPO} together with "
              f"{report['bundle']}/{PLACEMENT_RECORD}", file=out)


# ---------------------------------------------------------------------------------------------------- CLI

ACTIONS_COMMANDS = ("execute", "pr-body")  # commands that may run in Actions: no tracebacks, no content


def _date_arg(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"dates are YYYY-MM-DD, got {value!r}") from None


def build_parser() -> argparse.ArgumentParser:
    roots = argparse.ArgumentParser(add_help=False)
    roots.add_argument("--public-root", type=Path, default=None, help="owners-office checkout (default: this one)")
    roots.add_argument("--private-root", type=Path, default=None,
                       help="owners-office-private checkout (default: next to the public one)")
    roots.add_argument("--workspace-root", type=Path, default=None,
                       help="workspace holding .env and inputs/ (default: the parent of the public checkout)")
    roots.add_argument("--schemas-dir", type=Path, default=None,
                       help="thesis-ci schema directory (default: the installed thesis-ci)")

    step_args = argparse.ArgumentParser(add_help=False)
    step_args.add_argument("step", help=f"one of {', '.join(registry.STEPS)}")
    step_args.add_argument("company", help="ticker (companies/<TICKER>/), or hq for HQ steps such as 18")
    step_args.add_argument("period", help="FY<year>Q<quarter> for company steps; YYYY-MM (the month written about) for 18")
    step_args.add_argument("--run-date", type=_date_arg, default=None, help="YYYY-MM-DD (default: today)")
    step_args.add_argument("--offline", action="store_true", help="EDGAR from the local cache only")

    parser = argparse.ArgumentParser(prog="python -m pipeline.runner",
                                     description="Pipeline runner: assemble an input bundle, execute its model call, "
                                                 "place the outputs (docs/decisions/0019).")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("steps", help="list the supported steps")
    p = sub.add_parser("assemble", parents=[roots, step_args], help="build an input bundle in the private repository")
    p.add_argument("--allow-dirty", action="store_true",
                   help="assemble from uncommitted pipeline code (recorded; execute will then need --allow-unpinned)")
    p = sub.add_parser("dry-run", parents=[roots, step_args], help="assemble and execute with the fake backend")
    p.add_argument("--out", type=Path, default=None, help=f"output directory (default: <workspace>/{DRY_RUN_DIR})")
    p.add_argument("--replace", action="store_true", help="replace an earlier dry-run bundle of the same step and date")
    p = sub.add_parser("show", parents=[roots], help="summarize a bundle: names, sizes, hashes, sources")
    p.add_argument("bundle")
    p = sub.add_parser("execute", parents=[roots], help="run a bundle's model call and record it in the bundle")
    p.add_argument("bundle")
    p.add_argument("--backend", choices=BACKENDS, default=None,
                   help=f"claude-code: the Claude Code CLI on the owner's Claude subscription; api: the Anthropic API "
                        f"(the fallback; counts against the monthly budget); fake: placeholders for dry runs "
                        f"(default: {llm.BACKEND_ENV}, else {DEFAULT_BACKEND})")
    p.add_argument("--llm-log", type=Path, default=None,
                   help=f"call log the budget guard reads (default: <private>/{registry.LLM_LOG_REL})")
    p.add_argument("--retry", action="store_true", help="run a failed bundle again; the failed attempt is kept")
    p.add_argument("--allow-unpinned", action="store_true",
                   help="run although the public checkout is not the pinned commit (recorded in run.yml)")
    p = sub.add_parser("pr-body", parents=[roots], help="print the pull-request body for an executed bundle")
    p.add_argument("bundle")
    p = sub.add_parser("place", parents=[roots], help="copy an executed bundle's outputs to their destinations")
    p.add_argument("bundle")
    p.add_argument("--branch", default=None, help="public branch (default: the current one, or a new pipeline/... one)")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--announced", default=None, help="the release date the company announced (EDGAR is re-read)")
    group.add_argument("--window", nargs=2, metavar=("START", "END"), default=None,
                       help="the release window the company announced")
    p.add_argument("--check", action="store_true", help="show the plan and the checks; write nothing")
    p.add_argument("--allow-cjk", action="store_true", help="place public-bound output that contains CJK text")
    p.add_argument("--no-lint", action="store_true", help="skip thesis-ci lint after writing")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "steps":
        for spec in registry.STEPS.values():
            part = f" part {spec.part}" if spec.part else ""
            print(f"{spec.step:<5} prompt {spec.prompt_id}{part}; {spec.scope}; period {spec.period_kind}; {spec.summary}")
        return 0
    roots = resolve_roots(args.public_root, args.private_root, args.workspace_root)
    try:
        if args.command == "assemble":
            bundle_dir = assemble(args.step, args.company, args.period, run_date=args.run_date, roots=roots,
                                  offline=args.offline, allow_dirty=args.allow_dirty, schemas_dir=args.schemas_dir)
            print(describe_bundle(bundle_dir))
            return 0
        if args.command == "dry-run":
            bundle_dir, record = dry_run(args.step, args.company, args.period, run_date=args.run_date, roots=roots,
                                         out_root=args.out, replace=args.replace, offline=args.offline,
                                         schemas_dir=args.schemas_dir)
            print(describe_bundle(bundle_dir))
            print(f"dry-run bundle: {bundle_dir}")
            return 0 if record.get("status") == "succeeded" else 1
        if args.command == "show":
            print(describe_bundle(resolve_bundle(args.bundle, roots.private)))
            return 0
        if args.command == "execute":
            try:
                backend = llm.resolve_backend(args.backend)
            except ValueError as exc:
                raise RunnerError(str(exc)) from None
            if backend == "fake" and _inside(resolve_bundle(args.bundle, roots.private), roots.private):
                raise RunnerError("fake outputs never go into the private repository; use dry-run")
            record = execute(args.bundle, roots=roots, backend=backend, log_path=args.llm_log, retry=args.retry,
                             allow_unpinned=args.allow_unpinned, schemas_dir=args.schemas_dir)
            return 0 if record.get("status") == "succeeded" else 1
        if args.command == "pr-body":
            sys.stdout.write(pr_body(resolve_bundle(args.bundle, roots.private)))
            return 0
        if args.command == "place":
            place(args.bundle, roots=roots, branch=args.branch, announced=args.announced, window=args.window,
                  check=args.check, allow_cjk=args.allow_cjk, lint=not args.no_lint, schemas_dir=args.schemas_dir)
            return 0
    except RunnerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except edgar.EdgarError as exc:
        print(f"EDGAR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        if args.command not in ACTIONS_COMMANDS:
            raise  # local commands: the traceback helps, and nothing is logged anywhere
        print(f"error: {error_summary(exc)}", file=sys.stderr)  # no traceback: it could carry content into the log
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
