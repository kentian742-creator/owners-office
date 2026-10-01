"""Pipeline runner: one prompt part at a time, from an input bundle to placed outputs.

Phase 1 of docs/DESIGN.md (STATUS T4, T7, T11, T12); design and reasons in docs/decisions/0019 and, for the steps
after an earnings event, 0024.

    python -m pipeline.runner steps
    python -m pipeline.runner assemble 15A APP FY2026Q3 --run-date 2026-10-20
    python -m pipeline.runner execute runs/APP/2026-10-20-15A [--backend claude-code|api|fake] [--retry]
    python -m pipeline.runner show runs/APP/2026-10-20-15A
    python -m pipeline.runner place runs/APP/2026-10-20-15A [--announced 2026-11-05] [--check]
    python -m pipeline.runner dry-run 15A APP FY2026Q3 --run-date 2026-10-20
    python -m pipeline.runner pr-body runs/APP/2026-10-20-15A
    python -m pipeline.runner evaluate AXP FY2026Q3 --run-date 2026-10-21
    python -m pipeline.runner event AXP FY2026Q3 --run-date 2026-10-21 [--dry-run] [--approve draft|audit|placement]

1. assemble (local). Builds exactly the inputs the prompt part declares, through the input registry
   (pipeline/registry.py), checks them against the role's visibility (llm.check_inputs, 00 section G6) and writes a
   bundle into the private repository: runs/<TICKER|hq>/<run_date>-<part>/inputs/<name>.<md|yml|txt> plus
   manifest.yml (step, role, prompt id/part/version and hashes, variables, each input's sources and sha256, the
   public commit whose pipeline code executes it, created_at). EDGAR is read here, with the SEC User-Agent from
   the workspace .env. A missing required input fails the assembly with the full list of what is missing.
2. execute. Verifies the bundle against its manifest (input hashes, prompt hashes, the pinned public commit, the
   thesis-ci schemas), runs llm.complete() once and writes outputs/, run.yml, calls.jsonl and reply.txt next to the
   inputs (a bundle assembled in slices runs once per slice and merges the slices' outputs, docs/decisions/0026); every request is also appended to runs/llm-log.jsonl, the call log the budget guard reads (T7). Backends
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
   both repositories, so the whole path can be tested without any model access. A post-event step whose event is not
   on EDGAR yet runs as a rehearsal: the last reported quarter's filings stand in, marked as such.
5. evaluate (local; docs/decisions/0024). The quantitative tests of one event, evaluated by thesis-ci's
   evaluate_company() on readings from XBRL companyfacts and from 16B, recorded like a run (the ci step:
   runs/<TICKER>/<run_date>-ci/ with manifest.yml, inputs, outputs/ci_results.yml and run.yml). No model.
6. event (local; pipeline/chain.py). The post-earnings chain of one event, step by step, resumable, with stops for
   human review after the draft, after the audit and before placement.

place knows the actions of 00 section F2 (write, front_matter, append, merge, patch, pr_body, pr_attachment) and
routes a quarterly update by trust level (section G9): below level 2 its public files are staged in the private
repository until HQ has reviewed them, and `place --publish` writes them to the public branch.

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
from collections.abc import Mapping, MutableMapping, Sequence
from pathlib import Path
from typing import Any, TextIO

import yaml

from . import documents, edgar, evaluation, fake_client, isolation, llm, registry, slicing
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
SLICES_DIR = "slices"  # slices/s01/{inputs,outputs}/..., run.yml, calls.jsonl, reply.txt (decisions/0026)
MANIFEST_VERSION = 1
RUN_RECORD_VERSION = 1
DRY_RUN_DIR = Path("work") / "pipeline-dry-run"  # under the workspace root, outside both repositories

BACKENDS = llm.BACKENDS  # claude-code, api, fake
DEFAULT_BACKEND = llm.DEFAULT_BACKEND  # owner decision 2026-09-25: the Claude Code CLI on the owner's subscription
REAL_BACKENDS = (llm.CLAUDE_CODE, llm.API)
BUNDLE_REL_RE = re.compile(r"^runs/([A-Z][A-Z0-9.]{0,9}|hq)/(\d{4}-\d{2}-\d{2})-(\d{2}[A-Za-z0-9.-]*|[a-z][a-z0-9]*)$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# Public paths whose uncommitted changes would make the pinned commit misstate the code and roles that run.
PIN_PATHS = ("pipeline", "agents", "constitution/decision-rights.yml", "requirements.txt", "requirements-lint.txt")
DEFAULT_BRANCHES = ("main", "master")
CONTEXT_WARN_TOKENS = 600_000  # a request this large still fits (slicing.MAX_INPUT_TOKENS) but is noted
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


def use_workspace_env_file(roots: Roots, environ: MutableMapping[str, str] | None = None) -> None:
    """Make EDGAR and the Claude Code backend read the chosen workspace's .env (the SEC User-Agent, the subscription
    token) unless OWNERS_OFFICE_ENV_FILE already names one. Without this, both would read the .env next to the code,
    which is wrong when --workspace-root points elsewhere (for example, a rehearsal in a clone)."""
    environ = os.environ if environ is None else environ
    if not (environ.get(edgar.ENV_FILE_ENV) or "").strip():
        environ[edgar.ENV_FILE_ENV] = str(roots.workspace / ".env")


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


def slice_entries(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every slice's input entries, each named <slice id>/<input name> (the names inputs_sha256 covers)."""
    return [{**entry, "name": f"{part['id']}/{entry['name']}"} for part in manifest.get("slices") or []
            for entry in part.get("inputs") or []]


def verify_bundle(bundle_dir: Path, manifest: Mapping[str, Any]) -> None:
    """Every input file (of every slice) is present with the manifest's sha256, and inputs/ holds nothing else."""
    problems, listed = [], set()
    entries = (manifest.get("inputs") or []) + slice_entries(manifest)
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
    input_dirs = [bundle_dir / INPUTS_DIR, *sorted((bundle_dir / SLICES_DIR).glob(f"*/{INPUTS_DIR}"))]
    extra = sorted(p.relative_to(bundle_dir).as_posix() for d in input_dirs if d.is_dir() for p in d.rglob("*")
                   if p.is_file() and p.relative_to(bundle_dir).as_posix() not in listed)
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
        call = llm.prompt_part(prompt, record.get("part"), mode=record.get("mode"))
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


def read_inputs(bundle_dir: Path, manifest: Mapping[str, Any], entries: Sequence[Mapping[str, Any]] | None = None
                ) -> dict[str, str]:
    entries = manifest["inputs"] if entries is None else entries
    return {str(e["name"]): (bundle_dir / str(e["file"])).read_text(encoding="utf-8") for e in entries}


# ---------------------------------------------------------------------------------------------------- assemble


def _normalize_company(spec: registry.StepSpec, company: str) -> str | None:
    text = str(company).strip()
    if spec.scope == "hq" and not spec.about_company:
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
    tokens = slicing.estimate_tokens(text)
    estimate = {"input_tokens": tokens, "method": f"ASCII characters / {slicing.CHARS_PER_TOKEN} + one per other "
                "character (calibrated on real calls, pipeline/slicing.py)", "model": model}
    if model in llm.PRICES_PER_MTOK:
        estimate["input_cost_usd_uncached"] = round(tokens * llm.PRICES_PER_MTOK[model][0] / 1_000_000, 4)
    return estimate


def assemble(step: str, company: str, period: str, *, run_date: dt.date | None = None, roots: Roots | None = None,
             out_root: str | os.PathLike[str] | None = None, edgar_gateway: Any = None, offline: bool = False,
             allow_dirty: bool = False, schemas_dir: str | os.PathLike[str] | None = None,
             today: dt.date | None = None, rehearsal: bool = False, round_: int = 1,
             subject: str | None = None, rerun: bool = False) -> Path:
    """Build the input bundle of one step and return its directory. Nothing is written when anything is missing.

    Upstream runs are looked up under `out_root` first when it is not the private repository (a dry run reads its own
    chain). `rehearsal` (dry runs only) lets the last reported quarter's filings stand in for an event that has not
    happened yet. `round_` 2 assembles the second round of a step in one event (registry.ROUNDS): the bundle is
    <run_date>-<step>-r2 and its inputs are the round's. `subject` "archive" runs 16A and 04A on a new archive's
    dossier instead of a quarterly update (decisions/0028). `rerun` assembles the step again on the same run date
    when its bundle exists (after a code fix or a changed input): the bundle is <name>-rerun<n>, never a later
    date."""
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
        call = llm.prompt_part(prompt, spec.part, mode=spec.mode)
        rules = llm.load_prompt(llm.RULES_ID, prompts_dir)
        design = llm.load_prompt(llm.DESIGN_ID, prompts_dir) if call.design else None
        formats = llm.output_formats(rules, call)
        role = llm.role_definition(call.role, roots.public)
    except llm.PromptError as exc:
        raise RunnerError(f"{step}: {exc}") from None
    if call.label != spec.step:
        raise RunnerError(f"{step}: prompt {spec.prompt_id} part {spec.part} resolves to {call.label}")
    if round_ != 1 and not 1 < round_ <= registry.ROUNDS.get(step, 1):
        raise RunnerError(f"{step} has no round {round_} (rounds: {registry.ROUNDS.get(step, 1)})")
    if not role.prompts or (prompt.id not in role.prompts and call.label not in role.prompts):
        raise RunnerError(f"{step}: agents/{role.path.name} does not register {call.label} for role {role.role}")
    schemas = Path(schemas_dir) if schemas_dir else None
    base = Path(out_root).resolve() if out_root else roots.private
    if rehearsal and not (out_root and not _inside(base, roots.private) and not _inside(base, roots.public)):
        raise RunnerError("a rehearsal (the last reported quarter standing in for the event) is for dry runs only, "
                          "outside both repositories")
    ctx = registry.RunContext(
        step=spec, company=ticker, period=period, run_date=run_date, public_root=roots.public,
        private_root=roots.private, workspace_root=roots.workspace, call=call, formats=formats,
        edgar=edgar_gateway if edgar_gateway is not None else registry.EdgarGateway(offline=offline),
        schemas_dir=schemas, runs_roots=(base,) if base != roots.private else (), rehearsal=rehearsal,
        round=round_, subject=subject,
    )
    if subject is not None and (subject != registry.ARCHIVE_SUBJECT or spec.step not in ("16A", "04A")):
        raise RunnerError(f"{step}: --subject {subject} is not supported (only 16A and 04A take "
                          f"{registry.ARCHIVE_SUBJECT!r})")
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

    rel = f"runs/{ctx.scope}/{spec.bundle_name(run_date, ticker, round_)}"
    if rerun:
        while (base / rel).exists():
            ctx.rerun += 1
            rel = f"runs/{ctx.scope}/{spec.bundle_name(run_date, ticker, round_, ctx.rerun)}"
    bundle_dir = base / rel
    if bundle_dir.exists():
        raise RunnerError(f"{rel} already exists under {base}; bundles are never overwritten (a same-day rerun: "
                          "--rerun)")

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
        raise RunnerError(f"cannot assemble {step} for {ticker or ctx.scope} {period}; missing or unreadable inputs:\n- "
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
    model = role.model.get("id")
    try:
        sliced = registry.slice_inputs(ctx, built)
    except (registry.MissingInput, edgar.EdgarError) as exc:
        raise RunnerError(f"{step}: cutting the inputs into slices: {exc}") from None

    def input_entries(parts: Mapping[str, registry.BuiltInput], folder: str) -> list[dict[str, Any]]:
        entries = []
        for name, required in call.inputs:
            if name not in parts:
                continue
            b = parts[name]
            data = b.text.encode("utf-8")
            entry: dict[str, Any] = {"name": name, "file": f"{folder}{INPUTS_DIR}/{name}.{b.ext}",
                                     "required": required, "bytes": len(data), "sha256": _sha(data),
                                     "sources": b.sources}
            if b.empty:
                entry["empty"] = True
            if b.substitute:
                entry["substitute"] = b.substitute
            if b.note:
                entry["note"] = b.note
            entries.append(entry)
        return entries

    def fits(estimate: Mapping[str, Any], what: str) -> None:
        if estimate["input_tokens"] > slicing.MAX_INPUT_TOKENS:
            raise RunnerError(f"{step}: {what} is estimated at {estimate['input_tokens']:,} input tokens, more than the "
                              f"{slicing.MAX_INPUT_TOKENS:,} the context window leaves beside the output; nothing was "
                              "written (cut the inputs, or add a slicer for this part: pipeline/slicing.py)")
        if estimate["input_tokens"] > CONTEXT_WARN_TOKENS:
            notes.append(f"{what}: estimated input {estimate['input_tokens']:,} tokens")

    slices_meta: list[dict[str, Any]] = []
    files: dict[str, str] = {}
    if sliced:
        entries = []
        for number, part in enumerate(sliced, 1):
            sid = f"s{number:02d}"
            own = input_entries(part.inputs, f"{SLICES_DIR}/{sid}/")
            texts_k = {name: b.text for name, b in part.inputs.items()}
            estimate_k = _estimate(prompt, rules, design, call, variables, texts_k, model)
            fits(estimate_k, f"slice {sid}")
            slices_meta.append({"id": sid, "note": part.note, "inputs": own, "inputs_sha256": inputs_digest(own),
                                "request_sha256": _request_hash(prompt, rules, design, call, variables, texts_k),
                                "estimate": estimate_k})
            files.update({e["file"]: part.inputs[e["name"]].text for e in own})
        tokens = sum(m["estimate"]["input_tokens"] for m in slices_meta)
        estimate = {**slices_meta[0]["estimate"], "input_tokens": tokens, "slices": len(slices_meta)}
        if "input_cost_usd_uncached" in estimate:
            estimate["input_cost_usd_uncached"] = round(sum(m["estimate"]["input_cost_usd_uncached"]
                                                            for m in slices_meta), 4)
        digest, request = inputs_digest([*slice_entries({"slices": slices_meta})]), None
        notes.append(f"sliced into {len(slices_meta)} calls (decisions/0026); outputs/ holds the merged outputs")
    else:
        entries = input_entries(built, "")
        texts = {name: b.text for name, b in built.items()}
        estimate = _estimate(prompt, rules, design, call, variables, texts, model)
        fits(estimate, "the request")
        digest, request = inputs_digest(entries), _request_hash(prompt, rules, design, call, variables, texts)
        files.update({e["file"]: built[e["name"]].text for e in entries})
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
        "prompt": _prompt_record(prompt, roots.private, part=spec.part, label=call.label,
                                 **({"mode": spec.mode} if spec.mode else {})),
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
        "inputs_sha256": digest,
        "request_sha256": request,
        "estimate": estimate,
        "notes": notes,
    }
    if slices_meta:
        manifest["slices"] = slices_meta
    if round_ > 1:
        manifest["round"] = round_
    if ctx.rerun > 1:
        manifest["rerun"] = ctx.rerun
    if rehearsal:
        manifest["rehearsal"] = True
        notes.append("rehearsal: a dry run before the event; inputs marked substitute stand in for the event's")
    bundle_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".assemble-", dir=bundle_dir.parent))
    try:
        os.chmod(tmp, 0o755)
        for rel_file, text in files.items():
            _write_atomic(tmp / rel_file, text.encode("utf-8"))
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


def own_log_lines(lines: bytes, input_sha256: str | None) -> bytes:
    """The log lines of this bundle's (or slice's) own requests: another execution running at the same time appends
    to the same call log, so lines are kept by the request hash the manifest recorded."""
    if not input_sha256:
        return lines
    kept = []
    for line in lines.splitlines(keepends=True):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("input_sha256") == input_sha256:
            kept.append(line)
    return b"".join(kept)


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


def _prompt_id(manifest: Mapping[str, Any]) -> str:
    """The prompt id of a bundle; a deterministic step (ci) has none and goes by its step."""
    return str((manifest.get("prompt") or {}).get("id") or manifest["step"])


def planned_placements(manifest: Mapping[str, Any], entries: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Where each written output goes under 00 section F2 (outputs.place), as recorded in run.yml and the PR."""
    rows = []
    for name, entry in entries.items():
        if entry.get("status") != "written":
            continue
        try:
            placements = _outputs.place(name, prompt_id=_prompt_id(manifest), part_id=str(manifest["step"]),
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
            "label": manifest.get("step"), "domain": domain, "pipeline_fields": manifest.get("pipeline_fields") or {},
            "inputs": inputs}


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
    if manifest.get("role") == PIPELINE_BACKEND:
        raise RunnerError(f"{rel} is a deterministic step (no model); `evaluate` runs it")
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
    if manifest.get("slices"):
        return _execute_slices(bundle_dir, manifest, call, formats, backend=backend, backend_args=backend_args,
                               client=client, attempt=attempt, pin=pin, env=env, log_path=log_path, roots=roots,
                               schemas=schemas, out=out)
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
            part=manifest["prompt"].get("part"), mode=manifest["prompt"].get("mode"),
            variables=manifest.get("variables") or {},
            pipeline_fields=manifest.get("pipeline_fields") or None, client=client, log_path=log,
            repo_root=roots.public, prompts_dir=roots.private / "prompts", schemas_dir=schemas, **backend_args,
        )
    except Exception as exc:  # recorded in run.yml; only error_summary() is printed
        failure = exc
        record["error"] = describe_error(exc)
    reply = result.text if result is not None else getattr(getattr(failure, "result", None), "text", None)
    lines = own_log_lines(_new_log_lines(log, start), manifest.get("request_sha256"))
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


# ---------------------------------------------------------------------------------------------------- slices

# Failures after which the other slices are not tried in the same attempt: the backend itself is unavailable.
_STOPPING_ERRORS = ("ClaudeCodeUnavailable", "PlanLimitReached", "BudgetExceeded")


def _slice_order_key(fact_id: str) -> tuple[int, str]:
    digits = fact_id[1:] if fact_id[:1] == "F" else ""
    return (int(digits), fact_id) if digits.isdigit() else (10**9, fact_id)


def merge_slice_outputs(step: str, bundle_dir: Path, manifest: Mapping[str, Any]
                        ) -> tuple[dict[str, str | None], dict[str, Any]]:
    """The slices' outputs merged into the part's outputs ({name: YAML text, or None for an empty output}) and what
    the merge did (pipeline/slicing.py). Raises RunnerError when the merged outputs are not well formed."""
    parts = manifest["slices"]

    def output(part: Mapping[str, Any], name: str) -> Any:
        path = bundle_dir / SLICES_DIR / part["id"] / OUTPUTS_DIR / f"{name}.yml"
        return registry.load_yaml_file(path) if path.is_file() else None

    info: dict[str, Any] = {"slices": len(parts)}
    if step == "16A":
        tables = [output(part, "fact_table") for part in parts]
        merged, maps = slicing.merge_fact_tables(tables)
        info["fact_ids"] = {part["id"]: f"{min(m.values())}–{max(m.values())}" if m else "none"
                            for part, m in zip(parts, maps)}
        texts: dict[str, str | None] = {"fact_table": registry.dump_yaml(merged)}
        data = {"fact_table": merged}
    elif step == "04A":
        order: list[str] = []
        for part in parts:
            entry = next(e for e in part["inputs"] if e["name"] == "fact_table")
            order += slicing.fact_ids(registry.load_yaml_file(bundle_dir / entry["file"]))
        order.sort(key=_slice_order_key)
        merged_lists, maps = slicing.merge_audits(
            [{name: output(part, name) for name in ("fact_verdicts", "findings", "questions")} for part in parts],
            order)
        errors = slicing.verdict_coverage(order, merged_lists["fact_verdicts"])
        if errors:
            raise RunnerError("the merged fact_verdicts do not cover the fact table:\n- " + "\n- ".join(errors))
        info["renumbered"] = {part["id"]: m for part, m in zip(parts, maps) if m}
        texts = {name: registry.dump_yaml(rows) if rows else None for name, rows in merged_lists.items()}
        data = dict(merged_lists)
    else:
        raise RunnerError(f"{step} has no merge for slices (pipeline/slicing.py)")
    problems = [e for name, value in data.items() if value for e in _outputs.structure_errors(name, value)]
    if problems:
        raise RunnerError("the merged outputs are not well formed:\n- " + "\n- ".join(problems[:20]))
    return texts, info


def _write_merged(bundle_dir: Path, call: Any, texts: Mapping[str, str | None]) -> dict[str, dict[str, Any]]:
    out_dir = bundle_dir / OUTPUTS_DIR
    out_dir.mkdir(parents=True, exist_ok=False)
    entries: dict[str, dict[str, Any]] = {}
    for name, required in call.outputs:
        text = texts.get(name)
        if text is None:
            entries[name] = {"status": "empty", "format": _outputs.YAML}
            continue
        data = text.encode("utf-8")
        _write_atomic(out_dir / f"{name}.yml", data)
        entries[name] = {"status": "written", "file": f"{OUTPUTS_DIR}/{name}.yml", "format": _outputs.YAML,
                         "bytes": len(data), "sha256": _sha(data), "merged_from_slices": True}
    return entries


def _execute_slices(bundle_dir: Path, manifest: Mapping[str, Any], call: Any, formats: Mapping[str, str], *,
                    backend: str, backend_args: Mapping[str, Any], client: Any, attempt: int, pin: Mapping[str, Any],
                    env: Mapping[str, str], log_path: str | os.PathLike[str] | None, roots: Roots,
                    schemas: Path | None, out: TextIO) -> dict[str, Any]:
    """execute() for a sliced bundle (decisions/0026): each slice not yet succeeded runs as its own call and is
    recorded under slices/<id>/ like a bundle; once every slice has succeeded, the outputs are merged into outputs/.
    A retry runs only the slices that failed or never ran."""
    rel = manifest["bundle"]
    log = Path(log_path) if log_path else roots.private / registry.LLM_LOG_REL
    record: dict[str, Any] = {
        "run_version": RUN_RECORD_VERSION, "bundle": rel, "step": manifest["step"], "status": "failed",
        "backend": backend, "client": "injected" if client is not None else ("fake" if backend == "fake" else backend),
        "attempt": attempt, "started_at": _iso(_utcnow()), "github": github_context(env), "pipeline": dict(pin),
    }
    all_lines = b""
    rows: list[dict[str, Any]] = []
    failed: list[str] = []
    first: dict[str, Any] | None = None
    stopped = None
    for part in manifest["slices"]:
        sid = part["id"]
        sdir = bundle_dir / SLICES_DIR / sid
        previous = registry.load_yaml_file(sdir / RUN_RECORD)
        if isinstance(previous, dict) and previous.get("status") == "succeeded" and (sdir / OUTPUTS_DIR).is_dir():
            rows.append({"id": sid, "status": "succeeded", "attempt": previous.get("attempt"), "this_run": False})
            first = first or previous
            continue
        if stopped:
            rows.append({"id": sid, "status": "not run", "reason": stopped})
            failed.append(sid)
            continue
        s_attempt = _archive_failed_attempt(sdir) + 1 if (sdir / RUN_RECORD).exists() else 1
        inputs = read_inputs(bundle_dir, manifest, part["inputs"])
        s_client = client
        if backend == "fake" and client is None:
            s_client = fake_client.FakeClient(fake_client.placeholder_reply(call.outputs, formats,
                                                                            _fake_context(manifest, inputs)))
        start = log.stat().st_size if log.is_file() else 0
        srec: dict[str, Any] = {"run_version": RUN_RECORD_VERSION, "bundle": rel, "slice": sid, "note": part.get("note"),
                                "status": "failed", "attempt": s_attempt, "started_at": _iso(_utcnow())}
        result, failure = None, None
        try:
            result = llm.complete(
                str(manifest["role"]), str(manifest["prompt"]["id"]), inputs,
                part=manifest["prompt"].get("part"), mode=manifest["prompt"].get("mode"),
            variables=manifest.get("variables") or {},
                pipeline_fields=manifest.get("pipeline_fields") or None, client=s_client, log_path=log,
                repo_root=roots.public, prompts_dir=roots.private / "prompts", schemas_dir=schemas, **backend_args,
            )
        except Exception as exc:  # recorded in the slice's run.yml; only error_summary() is printed
            failure = exc
            srec["error"] = describe_error(exc)
        lines = own_log_lines(_new_log_lines(log, start), part.get("request_sha256"))
        all_lines += lines
        srec.update(summarize_calls(lines))
        if result is not None:
            entries = _write_outputs(sdir, call, result)
            srec.update(
                status="succeeded", model=result.model, requested_model=result.requested_model, effort=result.effort,
                served_by_fallback=result.served_by_fallback, attempts=result.attempts, stop_reason=result.stop_reason,
                input_sha256=result.input_sha256, request_matches_manifest=result.input_sha256 == part.get(
                    "request_sha256"),
                versions={k: getattr(result, k) for k in ("prompt_version", "prompt_revision", "rules_version",
                                                           "rules_revision", "design_version", "design_revision")},
                generated_by=dict(result.generated_by), outputs=entries)
            first = first or srec
        reply = result.text if result is not None else getattr(getattr(failure, "result", None), "text", None)
        sdir.mkdir(parents=True, exist_ok=True)
        if reply is not None:
            _write_atomic(sdir / REPLY, reply.encode("utf-8"))
            srec["reply"] = {"file": REPLY, "sha256": registry.sha256_text(reply)}
        if lines:
            _write_atomic(sdir / CALLS, lines)
            srec["calls_file"] = CALLS
        srec["finished_at"] = _iso(_utcnow())
        write_yaml(sdir / RUN_RECORD, srec)
        row = {"id": sid, "status": srec["status"], "attempt": s_attempt, "this_run": True,
               "requests": srec.get("requests", 0)}
        if srec.get("notional_cost_usd"):
            row["notional_cost_usd"] = srec["notional_cost_usd"]
        if failure is not None:
            row["error"] = srec["error"].get("type")
            failed.append(sid)
            if srec["error"].get("type") in _STOPPING_ERRORS:
                stopped = f"{srec['error'].get('type')} in {sid}"
        rows.append(row)
        print(f"  slice {sid}: {srec['status']}" + (f" ({error_summary(failure)})" if failure is not None else "")
              + f"; {srec.get('requests', 0)} request(s)", file=out)
    record.update(summarize_calls(all_lines))
    record["slices"] = rows
    failure_exc: BaseException | None = None
    if failed:
        failure_exc = RunnerError(f"{len(failed)} of {len(rows)} slice(s) did not succeed: {', '.join(failed)}; "
                                  "--retry runs only those again")
        record["error"] = {"type": "SlicesFailed", "message": str(failure_exc)}
    else:
        try:
            texts, info = merge_slice_outputs(str(manifest["step"]), bundle_dir, manifest)
        except RunnerError as exc:
            failure_exc = exc
            record["error"] = {"type": "MergeFailed", "message": str(exc)[:2000]}
        else:
            entries = _write_merged(bundle_dir, call, texts)
            first = first or {}
            record.update(
                status="succeeded", model=first.get("model"), requested_model=first.get("requested_model"),
                effort=first.get("effort"), served_by_fallback=first.get("served_by_fallback"),
                attempts=first.get("attempts"), stop_reason=first.get("stop_reason"),
                versions=first.get("versions"), generated_by=first.get("generated_by"), merged=info,
                outputs=entries, placements=planned_placements(manifest, entries))
    if all_lines:
        _write_atomic(bundle_dir / CALLS, all_lines)
        record["calls_file"] = CALLS
    record["call_log"] = (log.resolve().relative_to(roots.private.resolve()).as_posix()
                          if _inside(log, roots.private) else str(log))
    record["finished_at"] = _iso(_utcnow())
    write_yaml(bundle_dir / RUN_RECORD, record)
    _print_execution(record, failure_exc, out)
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
        if record.get("slices"):
            print(f"  {len(record['slices'])} slices merged into outputs/ (decisions/0026)", file=out)
        else:
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


# ---------------------------------------------------------------------------------------------------- evaluate (ci)

CI_OUTPUT = "ci_results"
PIPELINE_BACKEND = "pipeline"  # the backend of deterministic steps: no model is called
CI_SPEC = registry.StepSpec(registry.CI_STEP, "", None, "company", "quarter", False,
                            "evaluation of the quantitative tests: readings and results (deterministic, no model)",
                            post_event=True)


def _pin(roots: Roots, allow_dirty: bool) -> tuple[str | None, list[str], bool, list[str]]:
    """(commit, dirty paths, pushed, notes): the public commit whose code runs; refuses local changes unless allowed."""
    commit = git_head(roots.public)
    dirty = git_dirty(roots.public, PIN_PATHS)
    pushed = git_pushed(roots.public) if commit else False
    problems = []
    if commit is None:
        problems.append(f"{registry.PUBLIC_REPO} has no commit to pin (git rev-parse HEAD failed)")
    if dirty:
        problems.append(f"uncommitted changes in {len(dirty)} path(s): {_paths_summary(dirty)}")
    if problems and not allow_dirty:
        raise RunnerError("cannot pin the pipeline code:\n- " + "\n- ".join(problems))
    notes = [f"pin: {p}" for p in problems]
    if commit and not pushed:
        notes.append(f"pin: commit {commit[:12]} is on no remote branch yet")
    return commit, dirty, pushed, notes


def collect_readings(ctx: registry.RunContext, evaluator: evaluation.Evaluator) -> tuple[
        list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """(readings, not usable, sources, notes): XBRL readings from companyfacts (through thesis-ci) and filing-text
    readings from every succeeded 16B run of the company up to this period, the latest run of a period winning."""
    thesis = ctx.thesis()
    metrics, _ = registry.metric_registry(ctx)
    filer = ctx.filer()
    sources: list[dict[str, Any]] = []
    notes: list[str] = []
    groups: list[list[dict[str, Any]]] = []
    xbrl_ids = evaluation.xbrl_metric_ids(thesis, metrics)
    if xbrl_ids and not evaluator.placeholder:
        try:
            facts, source = ctx.gateway().companyfacts(filer.cik)
        except edgar.EdgarNotFound:
            facts, source = None, None
            notes.append("EDGAR has no XBRL companyfacts for this issuer; XBRL metrics have no readings")
        if facts is not None:
            readings = evaluator.xbrl_readings(facts, xbrl_ids, evaluation.reading_periods(ctx.period),
                                               filer.fiscal_year_end or "12-31", ticker=str(ctx.company), thesis=thesis)
            groups.append(readings)
            sources.append({**source, "metrics": xbrl_ids, "readings": len(readings)})
    elif xbrl_ids:
        notes.append(f"placeholder evaluator: no XBRL reading taken for {len(xbrl_ids)} metric(s)")
    now = documents.period_index(ctx.period)
    runs = sorted((r for r in ctx.runs() if r.succeeded and r.step == "16B" and r.company == ctx.company
                   and -1 < registry._period_key(r.period) <= now), key=lambda r: (registry._period_key(r.period),
                                                                                    r.run_date))
    unusable: list[dict[str, Any]] = []
    for run in runs:
        text = run.read_output("metric_values")
        if text is None:
            continue
        names = evaluation.text_metrics(thesis, str(run.period), run.run_date, metrics).names
        readings, bad = evaluation.readings_from_metric_values(yaml.safe_load(text), run=run.rel, names=names)
        groups.append(readings)
        if run.period == ctx.period:
            unusable += bad
        sources += run.outputs_used(["metric_values"])
    due_text, _, _ = evaluation.text_metric_definitions(thesis, ctx.period, ctx.run_date, metrics)
    if due_text and not any(r.period == ctx.period for r in runs):
        raise registry.MissingInput(f"{len(due_text)} due metric(s) are read from filing text and no 16B run for "
                                    f"{ctx.company} {ctx.period} succeeded (run 16B first)")
    return evaluation.latest_readings(groups), unusable, sources, notes


def evaluate(company: str, period: str, *, run_date: dt.date | None = None, roots: Roots | None = None,
             out_root: str | os.PathLike[str] | None = None, edgar_gateway: Any = None, offline: bool = False,
             allow_dirty: bool = False, schemas_dir: str | os.PathLike[str] | None = None,
             today: dt.date | None = None, retry: bool = False, evaluator: evaluation.Evaluator | None = None,
             out: TextIO | None = None) -> tuple[Path, dict[str, Any]]:
    """Evaluate the quantitative tests of one earnings event and record it like a run (the ci step):
    runs/<TICKER>/<run_date>-ci/ with inputs/thesis.yml and inputs/readings.yml, manifest.yml, outputs/ci_results.yml
    and run.yml. Deterministic, no model: thesis-ci's evaluate_company() applies each test's rule to the readings.

    In a dry run (`out_root` outside both repositories) the event may be rehearsed and, while thesis-ci lacks the
    evaluation interface, the placeholder evaluator records every test as undetermined. A succeeded bundle is final;
    `retry` evaluates a failed one again on the inputs recorded at its first attempt."""
    roots = roots or resolve_roots()
    out = out or sys.stdout
    today = today or dt.date.today()
    run_date = run_date or today
    ticker = _normalize_company(CI_SPEC, company)
    _check_period(CI_SPEC, period)
    base = Path(out_root).resolve() if out_root else roots.private
    dry = base != roots.private
    if dry:
        dry_run_base(roots, base)
    ctx = registry.RunContext(
        step=CI_SPEC, company=ticker, period=period, run_date=run_date, public_root=roots.public,
        private_root=roots.private, workspace_root=roots.workspace, call=None, formats={},
        edgar=edgar_gateway if edgar_gateway is not None else registry.EdgarGateway(offline=offline),
        schemas_dir=Path(schemas_dir) if schemas_dir else None, runs_roots=(base,) if dry else (), rehearsal=dry,
    )
    rel = f"runs/{ctx.scope}/{CI_SPEC.bundle_name(run_date, ticker)}"
    bundle_dir = base / rel
    attempt = 1
    if bundle_dir.exists():
        record = registry.load_yaml_file(bundle_dir / RUN_RECORD)
        status = record.get("status") if isinstance(record, dict) else None
        if status != "failed" or not retry:
            raise RunnerError(f"{rel} was already evaluated (status {status}); a succeeded evaluation is final, --retry "
                              "evaluates a failed one again")
        attempt = _archive_failed_attempt(bundle_dir) + 1
        manifest = load_manifest(bundle_dir)
        verify_bundle(bundle_dir, manifest)
    else:
        try:
            evaluator = evaluator or evaluation.load_evaluator(allow_placeholder=dry)
            thesis_text = ctx.thesis_path().read_text(encoding="utf-8") if ctx.thesis_path().is_file() else None
            ctx.thesis()
            readings, unusable, reading_sources, reading_notes = collect_readings(ctx, evaluator)
        except (registry.MissingInput, evaluation.EvaluatorUnavailable) as exc:
            raise RunnerError(f"cannot evaluate {ticker} {period}: {exc}") from None
        except edgar.EdgarError as exc:
            raise RunnerError(f"cannot evaluate {ticker} {period}: EDGAR: {exc}") from None
        commit, dirty, pushed, notes = _pin(roots, allow_dirty or dry)
        if run_date > today:
            notes.append(f"run_date {run_date} is later than the evaluation date {today}")
        texts = {"thesis": thesis_text or "",
                 "readings": registry.dump_yaml({"company": ticker, "period": period, "readings": readings,
                                                 "not_usable": unusable})}
        entries = [
            {"name": "thesis", "file": f"{INPUTS_DIR}/thesis.yml", "required": True,
             "bytes": len(texts["thesis"].encode("utf-8")), "sha256": registry.sha256_text(texts["thesis"]),
             "sources": [registry.repo_file_source(roots.public, ctx.thesis_path(), registry.PUBLIC_REPO)]},
            {"name": "readings", "file": f"{INPUTS_DIR}/readings.yml", "required": True,
             "bytes": len(texts["readings"].encode("utf-8")), "sha256": registry.sha256_text(texts["readings"]),
             "sources": reading_sources or [{"kind": "generated", "detail": "no reading source"}],
             "note": f"{len(readings)} reading(s); {len(unusable)} extracted value(s) not usable as a number"},
        ]
        manifest = {
            "manifest_version": MANIFEST_VERSION, "pipeline_commit": commit, "bundle": rel, "step": registry.CI_STEP,
            "summary": CI_SPEC.summary, "role": PIPELINE_BACKEND, "company": ticker, "scope": ctx.scope,
            "period": period, "run_date": run_date.isoformat(), "created_at": _iso(_utcnow()),
            "created_by": "python -m pipeline.runner evaluate", "prompt": None, "rules": None, "design": None,
            "model": None, "variables": {}, "context": registry.placement_context(ctx), "pipeline_fields": {},
            "schemas": {}, "evaluator": {"name": evaluator.name, "placeholder": evaluator.placeholder},
            "pipeline": {"repository": f"{_repo_owner(roots.public)}/{registry.PUBLIC_REPO}", "commit": commit,
                         "dirty_paths": dirty, "pushed": pushed},
            "private": {"commit": git_head(roots.private)}, "inputs": entries, "omitted": [],
            "inputs_sha256": inputs_digest(entries), "request_sha256": None,
            "notes": notes + reading_notes + (["rehearsal: a dry run"] if dry else []),
        }
        bundle_dir.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix=".evaluate-", dir=bundle_dir.parent))
        try:
            os.chmod(tmp, 0o755)
            for entry in entries:
                _write_atomic(tmp / entry["file"], texts[entry["name"]].encode("utf-8"))
            write_yaml(tmp / MANIFEST, manifest)
            tmp.rename(bundle_dir)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
    if evaluator is None:
        spec = manifest.get("evaluator") or {}
        evaluator = evaluation.load_evaluator(allow_placeholder=bool(spec.get("placeholder")) and dry)
    record = _run_evaluation(bundle_dir, manifest, evaluator, attempt, run_date)
    _print_evaluation(record, out)
    return bundle_dir, record


def _run_evaluation(bundle_dir: Path, manifest: Mapping[str, Any], evaluator: evaluation.Evaluator, attempt: int,
                    run_date: dt.date) -> dict[str, Any]:
    inputs = read_inputs(bundle_dir, manifest)
    record: dict[str, Any] = {"run_version": RUN_RECORD_VERSION, "bundle": manifest["bundle"], "step": registry.CI_STEP,
                              "status": "failed", "backend": PIPELINE_BACKEND, "client": evaluator.name,
                              "attempt": attempt, "started_at": _iso(_utcnow()), "requests": 0, "cost_usd": 0.0}
    try:
        thesis = yaml.safe_load(inputs["thesis"])
        readings = (yaml.safe_load(inputs["readings"]) or {}).get("readings") or []
        document = evaluator.evaluate_company(thesis, readings, str(manifest["period"]), run_date)
        if not isinstance(document, (Mapping, list)):
            raise TypeError(f"evaluate_company returned {type(document).__name__}, not a ci_results document")
        text = registry.dump_yaml(_outputs.jsonable(document))
        out_dir = bundle_dir / OUTPUTS_DIR
        out_dir.mkdir(parents=True, exist_ok=False)
        data = text.encode("utf-8")
        _write_atomic(out_dir / f"{CI_OUTPUT}.yml", data)
        entry = {"status": "written", "file": f"{OUTPUTS_DIR}/{CI_OUTPUT}.yml", "format": _outputs.YAML,
                 "bytes": len(data), "sha256": _sha(data)}
        record.update(status="succeeded", results=evaluation.result_counts(document), outputs={CI_OUTPUT: entry},
                      placements=planned_placements(manifest, {CI_OUTPUT: entry}))
    except Exception as exc:  # recorded in run.yml; only the error class is printed
        record["error"] = describe_error(exc)
    record["finished_at"] = _iso(_utcnow())
    write_yaml(bundle_dir / RUN_RECORD, record)
    return record


def _print_evaluation(record: Mapping[str, Any], out: TextIO) -> None:
    head = f"evaluate {record['bundle']} ({record['client']}, attempt {record['attempt']}): {record['status']}"
    if record.get("error"):
        head += f" ({record['error'].get('type')}; details in {RUN_RECORD})"
    print(head, file=out)
    counts = record.get("results") or {}
    if counts:
        print("  results: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())), file=out)
    for name, entry in (record.get("outputs") or {}).items():
        print(f"  output {name}: {entry['format']}, {entry['bytes']:,} bytes, sha256 {entry['sha256'][:12]}", file=out)


# ---------------------------------------------------------------------------------------------------- dry run


def dry_run(step: str, company: str, period: str, *, run_date: dt.date | None = None, roots: Roots | None = None,
            out_root: str | os.PathLike[str] | None = None, replace: bool = False, edgar_gateway: Any = None,
            offline: bool = False, schemas_dir: str | os.PathLike[str] | None = None,
            today: dt.date | None = None, out: TextIO | None = None) -> tuple[Path, dict[str, Any]]:
    """assemble + execute with the fake backend, into a directory outside both repositories. A post-event step whose
    event is not on EDGAR yet runs as a rehearsal: the last reported quarter's filings stand in, and say so."""
    roots = roots or resolve_roots()
    base = dry_run_base(roots, out_root)
    spec = registry.STEPS.get(step)
    if spec is not None and replace:
        ticker = None if spec.scope == "hq" and not spec.about_company else str(company).strip().upper()
        existing = base / "runs" / spec.storage_scope(ticker) / spec.bundle_name(run_date or today or dt.date.today(),
                                                                                  ticker)
        if existing.is_dir():
            shutil.rmtree(existing)
    bundle_dir = assemble(step, company, period, run_date=run_date, roots=roots, out_root=base,
                          edgar_gateway=edgar_gateway, offline=offline, allow_dirty=True, schemas_dir=schemas_dir,
                          today=today, rehearsal=bool(spec and spec.post_event))
    record = execute(bundle_dir, roots=roots, backend="fake", log_path=seed_dry_run_log(roots, base),
                     schemas_dir=schemas_dir, out=out)
    return bundle_dir, record


def dry_run_base(roots: Roots, out_root: str | os.PathLike[str] | None = None) -> Path:
    """The dry-run directory: work/pipeline-dry-run in the workspace unless given; never inside a repository."""
    base = Path(out_root).resolve() if out_root else roots.workspace / DRY_RUN_DIR
    if _inside(base, roots.public) or _inside(base, roots.private):
        raise RunnerError(f"the dry-run directory {base} must be outside both repositories")
    return base


def seed_dry_run_log(roots: Roots, base: Path) -> Path:
    """A copy of the real call log in the dry-run directory, so the budget guard sees the real spend."""
    log = base / registry.LLM_LOG_REL
    seed = roots.private / registry.LLM_LOG_REL
    _write_atomic(log, seed.read_bytes() if seed.is_file() else b"")
    return log


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
        what = f"{source.get('schema')}.schema.json" if source.get("schema") else source.get("detail")
        return f"{what} ({source.get('origin')})"
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
    if manifest.get("role") == PIPELINE_BACKEND:
        what = f"deterministic step, no model; evaluator {(manifest.get('evaluator') or {}).get('name')}"
    else:
        what = (f"prompt {prompt.get('id')} part {prompt.get('part')} v{prompt.get('version')} "
                f"(sha256 {str(prompt.get('sha256'))[:12]}); rules 00 v{rules.get('version')} (sha256 "
                f"{str(rules.get('sha256'))[:12]}); model {(manifest.get('model') or {}).get('id')}")
    lines = [
        f"{manifest['bundle']}  step {manifest['step']}  role {manifest['role']}  scope {manifest['scope']}  "
        f"period {manifest['period']}  run_date {manifest['run_date']}",
        what,
        f"pipeline commit {str(manifest.get('pipeline_commit'))[:12]}; pushed {pipeline.get('pushed')}; "
        f"uncommitted: {_paths_summary(pipeline.get('dirty_paths') or []) or 'none'}",
        f"request_sha256 {manifest.get('request_sha256')}",
        f"inputs ({len(manifest.get('inputs') or [])}" + (f"; {len(manifest['slices'])} slices" if manifest.get("slices")
                                                          else "") + "):",
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
    for part in manifest.get("slices") or []:
        estimate = part.get("estimate") or {}
        lines.append(f"  slice {part['id']}: ~{estimate.get('input_tokens', 0):,} input tokens; {part.get('note')}")
        for entry in part.get("inputs") or []:
            lines.append(f"      {entry['name']:<16} {entry['bytes']:>9,} B  sha256 {str(entry['sha256'])[:12]}")
    for item in manifest.get("omitted") or []:
        lines.append(f"  omitted {item['name']}: {item['reason']}")
    fields = manifest.get("pipeline_fields") or {}
    for output, values in fields.items():
        shown = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False, default=str)}" for k, v in values.items())
        lines.append(f"pipeline fields of {output}: {shown}")
    estimate = manifest.get("estimate") or {}
    if estimate:
        lines.append(f"estimate: ~{estimate.get('input_tokens', 0):,} input tokens ({estimate.get('method')}); "
                     f"uncached input cost ~{estimate.get('input_cost_usd_uncached')} USD on {estimate.get('model')}")
    for note in manifest.get("notes") or []:
        lines.append(f"note: {note}")
    record = registry.load_yaml_file(bundle_dir / RUN_RECORD)
    if isinstance(record, dict):
        lines.append(f"run: {record.get('status')} (backend {record.get('backend')}, client {record.get('client')}, "
                     f"attempt {record.get('attempt')}); {record.get('requests', 0)} request(s); cost "
                     f"{record.get('cost_usd', 0.0)} USD")
        for row in record.get("slices") or []:
            lines.append(f"  slice {row['id']}: {row.get('status')} (attempt {row.get('attempt')})"
                         + (f", {row['error']}" if row.get("error") else ""))
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
    status: str = "new"  # new | same | header-update | replace | update
    remove: list[str] = dataclasses.field(default_factory=list)
    cjk_lines: list[int] = dataclasses.field(default_factory=list)
    action: str = "write"  # the 00 section F2 action that produced it (write, front_matter, append, merge, patch, pr_body)
    public_bound: bool = False  # public text kept privately: the PR body, and updates staged under section G9
    staged_from: str | None = None  # the public path a staged file will be published to

    @property
    def text(self) -> str:
        return self.content.decode("utf-8")


# 03 hands back complete new versions of these public files; they replace the current ones once the file is what the
# draft read (a file changed in between is refused, never overwritten blindly).
REPLACED_OUTPUTS = frozenset({"thesis", "ledger", "story"})
QUARTERLY_UPDATE_STEPS = frozenset({"03R", "03P"})  # the placeable parts of prompt 03; the draft is revised first
STAGED_DIR = "staged"  # section G9: runs/<scope>/<run_dir>/staged/<public path> in the private repository
PR_BODY_FILE = "pr_body.md"
PUBLICATION_RECORD = "publication.yml"
# 00 section F2: the attachments of the quarterly-update pull request, appended to its body in this order.
PR_ATTACHMENTS = (("04A", "findings", "Audit findings (04A)"),
                  ("14T", "qualitative_verdicts", "Rulings on the qualitative tests (14T)"),
                  ("04B-lite", "inversion_list", "Inversion list (04B-lite)"),
                  ("14B", "divergence_map", "Divergence map (14B)"))
# 00 section F5 keys -> the dossier's fixed headings (prompt 01A): part number and title.
DOSSIER_PARTS: dict[str, tuple[int | None, str]] = {
    "business": (1, "Business"), "economics": (2, "Economics"), "moat": (3, "Moat"),
    "capital_allocation": (4, "Capital allocation"), "management": (5, "Management"), "culture": (6, "Culture"),
    "runway": (7, "Runway"), "valuation": (8, "Valuation"), "bear_case": (9, "Bear case"),
    "monitoring": (10, "Monitoring"), "thesis": (11, "Thesis"), "breakers": (12, "Thesis breakers"),
    "munger": (None, "Munger matrix"), "unknowns": (None, "Unknowns register"),
}
VERSION_HISTORY = "Version history"
MISTAKES_LIST_HEADING = "## The list"


def set_front_matter(text: str, key: str, value: Any) -> str:
    """A Markdown document with one front matter key set (reviewed_sections into the update record)."""
    parts = _outputs.split_front_matter(text)
    if parts is None:
        raise ValueError("the document has no front matter")
    front = _outputs.load_yaml_text(parts[0]) or {}
    front[key] = value
    return f"---\n{_outputs.dump_yaml(front)}---\n{parts[1]}"


def add_mistake(current: str, entry: str) -> str:
    """mistakes.md with the entry added as the newest one, right under "## The list" (the list is newest first)."""
    entry = entry.strip()
    if entry in current:
        return current
    marker = f"\n{MISTAKES_LIST_HEADING}\n"
    at = current.find(marker)
    if at < 0:
        return current.rstrip("\n") + f"\n\n{MISTAKES_LIST_HEADING}\n\n{entry}\n"
    head, tail = current[:at + len(marker)], current[at + len(marker):]
    return f"{head}\n{entry}\n\n{tail.lstrip(chr(10))}"


def merge_sources(current: str | None, entries: Sequence[Mapping[str, Any]]) -> tuple[str | None, int, list[str]]:
    """A sources.yml with new entries appended; (text, entries added, problems). An entry whose tag is already
    registered is skipped when identical and refused when it differs: placement never rewrites a source."""
    data = yaml.safe_load(current) if current else None
    if current and not (isinstance(data, dict) and isinstance(data.get("sources"), list)):
        return current, 0, ["sources.yml has no sources list"]
    old = list(data["sources"]) if data else []
    known = {str(e.get("tag")): e for e in old if isinstance(e, dict)}
    added, problems = [], []
    for entry in entries:
        tag = str(entry.get("tag"))
        if tag in known:
            if _outputs.jsonable(known[tag]) != _outputs.jsonable(entry):
                problems.append(f"source {tag} is registered with different fields; placement never rewrites a source")
            continue
        added.append(dict(entry))
        known[tag] = entry
    if not added:
        return current, 0, problems
    first = re.search(r"^( *)- ", current or "", re.M)  # the file's own indentation of the sources list
    indent = first.group(1) if first else "  "
    block = "\n".join("\n".join(indent + line for line in registry.dump_yaml([e]).rstrip("\n").split("\n"))
                      for e in added)
    text = (current.rstrip("\n") + "\n\n" + block + "\n") if current else "sources:\n" + block + "\n"
    if _outputs.jsonable(yaml.safe_load(text)) != _outputs.jsonable({**(data or {}), "sources": old + added}):
        text = registry.dump_yaml({**(data or {}), "sources": old + added})  # formatting lost, content exact
    return text, len(added), problems


def patch_dossier(text: str, changes: Sequence[Mapping[str, Any]]) -> tuple[str, list[str]]:
    """The dossier with each changed part replaced by its complete new text (03's dossier_changes; parts found by
    their fixed headings, 01A), and version_history entries added at the end of "## Version history"."""
    sections = isolation.split_sections(text, 2)
    problems: list[str] = []
    for change in changes:
        key = str(change.get("section"))
        body = next((str(change[k]).strip() for k in _outputs.DOSSIER_TEXT_KEYS if change.get(k)), "")
        if key == "version_history":
            at = next((i for i, (h, _) in enumerate(sections) if h and isolation.title_matches(h, VERSION_HISTORY)),
                      None)
            if at is None:
                sections.append((VERSION_HISTORY, f"\n## {VERSION_HISTORY}\n\n{body}\n"))
            else:
                heading, chunk = sections[at]
                sections[at] = (heading, chunk.rstrip("\n") + f"\n\n{body}\n\n")
            continue
        if key not in DOSSIER_PARTS:
            problems.append(f"dossier_changes: {key} is not a part of the dossier")
            continue
        number, title = DOSSIER_PARTS[key]
        at = next((i for i, (h, _) in enumerate(sections) if h and (
            (number is not None and isolation.section_number(h) == number) or isolation.title_matches(h, title))),
                  None)
        if at is None:
            problems.append(f"dossier_changes: the dossier has no part {number or ''} {title!r}".replace("  ", " "))
            continue
        heading = sections[at][0]
        new = body if body.startswith("## ") else f"## {heading}\n\n{body}"
        sections[at] = (heading, new.rstrip("\n") + "\n\n")
    return "".join(chunk for _, chunk in sections).rstrip("\n") + "\n", problems


def trust_level(public_root: Path, company: str) -> tuple[int | None, list[str]]:
    """The company manager's trust level (section G9): thesis.yml's trust_level, and trust/levels.yml's when they
    differ (the lower one counts, with a warning)."""
    thesis = registry.load_yaml_file(public_root / "companies" / company / "thesis.yml")
    levels = registry.load_yaml_file(public_root / "trust" / "levels.yml")
    found = [v for v in ((thesis or {}).get("trust_level") if isinstance(thesis, dict) else None,
                         ((levels or {}).get("companies") or {}).get(company) if isinstance(levels, dict) else None)
             if isinstance(v, int) and not isinstance(v, bool)]
    warnings = [] if len(set(found)) <= 1 else [f"trust levels differ (thesis.yml and trust/levels.yml: {found}); the "
                                                "lower one routes this update"]
    return (min(found) if found else None), warnings


def h4_hits(text: str) -> list[str]:
    """00 section H4 wording in public-bound text that is not a repository file (the PR body), with thesis-ci's own
    wording lists; an empty list when thesis-ci is not installed (the caller warns)."""
    try:
        from thesis_ci.checks.public import ADVICE_WORDING, VALUATION_WORDING
        from thesis_ci.textscan import wording_hits
    except ImportError:
        raise LookupError("thesis-ci is not installed") from None
    return [f"line {line}: {label}" for wording in (VALUATION_WORDING, ADVICE_WORDING)
            for line, label, _ in wording_hits(text, wording)]


def compose_pr_body(text: str, manifest: Mapping[str, Any], base: Path) -> tuple[str, list[dict[str, Any]]]:
    """03's pr_body followed by the attachments 00 section F2 puts on the quarterly-update pull request: the audit
    findings (every round), the rulings on the qualitative tests, the inversion list and the divergence map of the
    same event."""
    runs = registry.index_run_roots([base])
    parts, used = [text.rstrip()], []
    for step, output, title in PR_ATTACHMENTS:
        found = sorted((r for r in runs if r.succeeded and r.step == step and r.company == manifest.get("company")
                        and r.period == manifest.get("period")), key=lambda r: (r.round, r.run_date, r.rel))
        chosen = found if step == "04A" else found[-1:]
        for run in chosen:
            body = run.read_output(output)
            if body is None or not body.strip() or _outputs.is_empty_mark(body):
                continue
            label = title if run.round == 1 else title.replace(")", f", round {run.round})")
            parts.append(f"## {label}\n\n```yaml\n{body.rstrip()}\n```")
            used += run.outputs_used([output])
    return "\n\n".join(parts) + "\n", used


SETTLEMENT_RESULT_KEYS = ("id", "outcome", "values", "calculation", "evidence", "source", "reasoning", "settled_at",
                          "hq_ruling")
SETTLEMENT_SCHEMA = "prereg-settlement"


def _first_commit_time(root: Path, rel: str) -> str | None:
    """When a file first entered the repository's history (committer time, ISO 8601 with offset): the merged_at of a
    pre-registration items file."""
    out = registry.git_output(root, "log", "--diff-filter=A", "--format=%cI", "--", rel)
    lines = [line for line in (out or "").splitlines() if line.strip()]
    return lines[-1] if lines else None


def settlement_files(text: str, manifest: Mapping[str, Any], bundle_dir: Path, roots: Roots,
                     writes: list[PlannedWrite], schemas_dir: Path | None) -> tuple[list[tuple[str, str]], list[str]]:
    """15B's prereg_settlement as settlement files (SPEC 2.1): one companies/<T>/prereg/<period>.settlement.yml per
    pre-registration period of the items settled, with the pipeline's header (company, period, the results filing's
    acceptance time and accession, when the items file was merged, its timestamp proof) and each result with its
    settled_at. A result already settled happened or not_happened is never rewritten; an undetermined one may be.
    Returns ([(path, content)], problems)."""
    company = str(manifest["company"])
    event = registry.load_yaml_file(bundle_dir / INPUTS_DIR / "event.yml") or {}
    rows = _outputs.load_yaml_text(text)
    rows = rows if isinstance(rows, list) else []
    problems: list[str] = []
    by_period: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        match = registry.ITEM_ID_RE.match(str(row.get("id")))
        if not match or match.group("company") != company:
            problems.append(f"prereg_settlement: {row.get('id')!r} is not an item id of {company}")
            continue
        by_period.setdefault(match.group("period"), []).append(row)
    try:
        validator = _outputs.schema_validator(SETTLEMENT_SCHEMA, schemas_dir)
    except _outputs.SchemaUnavailable as exc:
        return [], problems + [f"prereg_settlement: {exc}"]
    files = []
    for period, results in sorted(by_period.items()):
        path = f"companies/{company}/prereg/{period}.settlement.yml"
        items_rel = f"companies/{company}/prereg/{period}.yml"
        current = _current_text(writes, roots, registry.PUBLIC_REPO, path)
        doc = (yaml.safe_load(current) if current else None) or {"company": company, "period": period}
        doc.setdefault("acceptance_datetime", event.get("acceptance_datetime"))
        doc.setdefault("accession", event.get("accession"))
        doc["merged_at"] = doc.get("merged_at") or _first_commit_time(roots.public, items_rel)
        proof = f"{items_rel}.ots"
        doc["ots_proof"] = proof if (roots.public / proof).is_file() else doc.get("ots_proof")
        existing = {str(r.get("id")): r for r in doc.get("results") or [] if isinstance(r, dict)}
        for row in results:
            rid = str(row.get("id"))
            old = existing.get(rid)
            if old is not None and old.get("outcome") in registry.RESOLVED:
                if old.get("outcome") != row.get("outcome"):
                    problems.append(f"{path}: {rid} was settled {old.get('outcome')} before; a settlement is never "
                                    "rewritten")
                continue
            new = {k: row[k] for k in SETTLEMENT_RESULT_KEYS if k in row and k != "settled_at"}
            if not new.get("source"):
                new["source"] = evaluation.source_tag(row.get("evidence"))
            new["settled_at"] = str(manifest["run_date"])
            existing[rid] = new
        doc["results"] = list(existing.values())
        problems += [f"{path}: {e}" for e in _outputs.schema_errors(validator, doc, SETTLEMENT_SCHEMA)]
        header = (f"# Settlement of {company}'s {period} pre-registration (15B), written by the pipeline "
                  "(SPEC 2.1; docs/decisions/0024)\n")
        files.append((path, header + registry.dump_yaml(_outputs.jsonable(doc))))
    return files, problems


def _replace_guard(write: PlannedWrite, manifest: Mapping[str, Any], base: Path, root: Path) -> str | None:
    """A replaced file (thesis, ledger, story) must still be what the update read; returns a problem or None."""
    current = registry.sha256_bytes((root / write.path).read_bytes())
    read = None
    manifests = [manifest]
    runs = registry.index_run_roots([base])
    drafts = [r for r in runs if r.succeeded and r.step == "03-draft" and r.company == manifest.get("company")
              and r.period == manifest.get("period") and r.manifest]
    manifests += [max(drafts, key=lambda r: (r.run_date, r.rel)).manifest] if drafts else []
    for m in manifests:
        entry = next((e for e in m.get("inputs") or [] if e.get("name") == write.output), None)
        source = next((x for x in (entry or {}).get("sources") or [] if x.get("kind") == "repo_file"), None)
        if source:
            read = source.get("sha256")
            break
    if read is None:
        write.note = "; ".join(filter(None, [write.note, "the update did not read this file; replaced as written"]))
        return None
    if read != current:
        return (f"{write.repo}:{write.path} changed after the update read it ({current[:12]} now, {read[:12]} then); "
                "run the update again on the current file")
    return None


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


def _planned(writes: list[PlannedWrite], repo: str, path: str) -> PlannedWrite | None:
    return next((w for w in writes if w.repo == repo and w.path == path), None)


def _current_text(writes: list[PlannedWrite], roots: Roots, repo: str, path: str) -> str | None:
    """The destination as placement will leave it so far: a write planned earlier in this placement, else the file."""
    planned = _planned(writes, repo, path)
    if planned is not None:
        return planned.text
    dest = (roots.public if repo == registry.PUBLIC_REPO else roots.private) / path
    return dest.read_text(encoding="utf-8") if dest.is_file() else None


def _upsert(writes: list[PlannedWrite], new: PlannedWrite) -> None:
    old = _planned(writes, new.repo, new.path)
    if old is None:
        writes.append(new)
    else:
        old.content, old.action = new.content, f"{old.action}+{new.action}"


def plan_actions(name: str, text: str, placements: Sequence[_outputs.Placement], manifest: Mapping[str, Any],
                 roots: Roots, base: Path, writes: list[PlannedWrite], problems: list[str], warnings: list[str],
                 attachments: list[dict[str, Any]], bundle_dir: Path | None = None,
                 schemas: Path | None = None) -> None:
    """The writes one output makes under 00 section F2: write, front_matter, append, merge, patch, pr_body and
    pr_attachment. Each planned write holds the destination's complete new content."""
    context = manifest.get("context") or {}
    run_dir = context.get("run_dir") or f"{manifest['run_date']}-{manifest['step']}"
    default = f"runs/{manifest['scope']}/{run_dir}/{name}.yml"
    for p in placements:
        if p.action == "write" and p.path is not None:
            writes.append(PlannedWrite(name, p.repo, p.visibility, p.path, text.encode("utf-8"), note=p.note))
        elif p.action == "front_matter":
            target = _planned(writes, p.repo, str(p.path))
            data = _outputs.load_yaml_text(text)
            sections = data.get(name) if isinstance(data, dict) else data
            if target is None:
                problems.append(f"{name}: no {p.path} is written by this bundle to carry it in its front matter")
                continue
            try:
                target.content = set_front_matter(target.text, name, sections).encode("utf-8")
                target.action = f"{target.action}+front_matter"
            except ValueError as exc:
                problems.append(f"{name}: {p.path}: {exc}")
        elif p.action == "append":
            current = _current_text(writes, roots, p.repo, str(p.path))
            if current is None:
                problems.append(f"{name}: {p.repo}:{p.path} does not exist")
                continue
            if not text.lstrip().startswith("### "):
                warnings.append(f"{name}: the entry does not open with a '### YYYY-MM-DD · Ticker · Category' heading "
                                "(mistakes.md format)")
            _upsert(writes, PlannedWrite(name, p.repo, p.visibility, str(p.path),
                                         add_mistake(current, text).encode("utf-8"), note=p.note, action="append"))
        elif p.action == "merge":
            entries = _outputs.split_sources_additions(_outputs.load_yaml_text(text))[p.visibility]
            if not entries:
                continue
            merged, added, trouble = merge_sources(_current_text(writes, roots, p.repo, str(p.path)), entries)
            problems += [f"{name} ({p.visibility}): {t}" for t in trouble]
            if added:
                _upsert(writes, PlannedWrite(name, p.repo, p.visibility, str(p.path), str(merged).encode("utf-8"),
                                             note=f"{added} source(s) added", action="merge"))
        elif p.action == "patch":
            current = _current_text(writes, roots, p.repo, str(p.path))
            changes = _outputs.load_yaml_text(text)
            if not changes:
                continue
            if current is None:
                warnings.append(f"{name}: {p.repo}:{p.path} does not exist yet, so the changes cannot be merged into "
                                f"it; they are kept at {default} for the archive build (01)")
                writes.append(PlannedWrite(name, registry.PRIVATE_REPO, _outputs.PRIVATE, default,
                                           text.encode("utf-8"), note="dossier changes kept until a dossier exists"))
                continue
            patched, trouble = patch_dossier(current, changes)
            problems += trouble
            _upsert(writes, PlannedWrite(name, p.repo, p.visibility, str(p.path), patched.encode("utf-8"),
                                         note=p.note, action="patch"))
        elif p.action == "pr_body":
            body, used = compose_pr_body(text, manifest, base)
            path = f"runs/{manifest['scope']}/{run_dir}/{PR_BODY_FILE}"
            writes.append(PlannedWrite(name, registry.PRIVATE_REPO, _outputs.PRIVATE, path, body.encode("utf-8"),
                                       note=f"the pull request's body, with {len(used)} attachment(s)",
                                       action="pr_body", public_bound=True))
        elif p.action == "settlement":
            files, trouble = settlement_files(text, manifest, bundle_dir, roots, writes, schemas)
            problems += trouble
            for path, content in files:
                _upsert(writes, PlannedWrite(name, p.repo, p.visibility, path, content.encode("utf-8"),
                                             note=p.note, action="settlement"))
        elif p.action == "pr_attachment":
            attachments.append({"output": name, "attached": "to the body of the quarterly update's pull request, "
                                                            "when the 03R bundle is placed"})
        else:
            problems.append(f"{name}: placement action {p.action!r} is not supported by the runner")


_CITED_RE = re.compile(r"(?m)\[src:([A-Za-z0-9][A-Za-z0-9._-]*)|^\s*(?:source|settlement_source):\s*['\"]?([A-Z][A-Za-z0-9._-]*)")


def supplied_documents(private_root: Path, company: str) -> dict[str, dict[str, Any]]:
    """tag -> a sources.yml entry for every EDGAR document the pipeline gave one of the company's runs (01A, the
    audit and its slices: their manifests record tag, form, accession, filing date and URL), for tags an archive
    cites but did not register; and for every price history and Treasury reading (01C, 02), marked private."""
    out: dict[str, dict[str, Any]] = {}
    for manifest_path in sorted((private_root / "runs" / company).glob("*/manifest.yml")):
        data = registry.load_yaml_file(manifest_path) or {}
        entries = list(data.get("inputs") or []) + [e for part in data.get("slices") or [] for e in part.get("inputs") or []]
        for entry in entries:
            for src in entry.get("sources") or []:
                tag = str(src.get("tag") or "").split("#")[0]
                if src.get("kind") in ("price", "treasury") and tag and tag not in out \
                        and isinstance(src.get("entry"), dict):
                    out[tag] = {**src["entry"], "visibility": _outputs.PRIVATE}  # prices stay private (§H4)
                if src.get("kind") != "edgar" or not tag or tag in out:
                    continue
                out[tag] = {"tag": tag, "kind": "filing", "title": f"{company} {src.get('form')} filed {src.get('filed')}",
                            "form": src.get("form"), "accession": src.get("accession"), "filed": src.get("filed"),
                            "url": src.get("url"), "primary": True,
                            "note": "registered by the pipeline: an EDGAR document supplied to one of its runs and cited in the archive"}
    return out


VALUATION_OUTPUTS = ("valuation_yml", "valuation_md")
_PROPOSED_RE = re.compile(r"(?m)^(doc_status:\s*)['\"]?proposed['\"]?\s*$")


def approving_review(private_root: Path, manifest: Mapping[str, Any]) -> tuple[str | None, str]:
    """The latest succeeded 04C run that reviewed this bundle's proposed valuation (its `valuation` input names the
    bundle), and its decision: (rel, "approved" | "returned" | ...), or (None, "") when none has (00 §V20)."""
    rel, company = str(manifest["bundle"]), str(manifest.get("company") or "")
    latest: tuple[str | None, str] = (None, "")
    for path in sorted((private_root / "runs" / company).glob("*-04C*/manifest.yml")):
        review = registry.load_yaml_file(path) or {}
        record = registry.load_yaml_file(path.parent / RUN_RECORD) or {}
        if record.get("status") != "succeeded":
            continue
        reviewed = {str(src.get("run")) for entry in review.get("inputs") or [] if entry.get("name") == "valuation"
                    for src in entry.get("sources") or [] if src.get("kind") == "run_output"}
        if rel not in reviewed:
            continue
        decision_file = next(path.parent.glob("outputs/valuation_decision.*"), None)
        data = yaml.safe_load(decision_file.read_text(encoding="utf-8")) if decision_file else None
        if isinstance(data, dict):
            data = data.get("valuation_decision", data.get("decision"))
        latest = (str(review.get("bundle") or path.parent.name), str(data or "").strip().lower())
    return latest


def mark_effective(text: str) -> str:
    """doc_status: proposed -> effective (the YAML key, or the Markdown front matter's), once 04C has approved it."""
    return _PROPOSED_RE.sub(r"\1effective", text, count=1)


def complete_sources(writes: list[PlannedWrite], manifest: Mapping[str, Any], roots: Roots,
                     warnings: list[str]) -> None:
    """Before lint: (1) a ledger management entry whose statement states a number without a tag gets its own source
    as the tag; (2) a tag the planned files cite that neither sources.yml registers, and that names a document the
    pipeline supplied to 01A, is registered in both (decisions/0028)."""
    company = manifest.get("company")
    if not company:
        return
    try:
        from thesis_ci.textscan import untagged_facts
    except ImportError:
        untagged_facts = None
    for write in writes:
        if write.path.endswith("/ledger.yml") and untagged_facts is not None:
            data = yaml.safe_load(write.text)
            changed = False
            for entry in (data.get("entries") if isinstance(data, dict) else None) or []:
                if not isinstance(entry, dict) or entry.get("side") in ("system", "owner"):
                    continue
                statement, source = entry.get("statement"), entry.get("source")
                if isinstance(statement, str) and isinstance(source, str) and untagged_facts(statement, False):
                    body = statement.rstrip()
                    end = body[-1] if body and body[-1] in ".;!?" else ""
                    entry["statement"] = (body[:-1] if end else body) + f" [src:{source}]" + end  # inside the sentence
                    changed = True
            if changed:
                write.content = registry.dump_yaml(data).encode("utf-8")
                warnings.append(f"{write.path}: management entries tagged with their own source")
    supplied = supplied_documents(roots.private, str(company))
    added: set[str] = set()
    path = f"companies/{company}/sources.yml"
    public_text = _current_text(writes, roots, registry.PUBLIC_REPO, path)
    public_entries = {str(e.get("tag")): e for e in (yaml.safe_load(public_text) or {}).get("sources") or []
                      if isinstance(e, dict) and e.get("tag")} if public_text else {}
    for repo, visibility in ((registry.PUBLIC_REPO, _outputs.PUBLIC), (registry.PRIVATE_REPO, _outputs.PRIVATE)):
        # a public file resolves tags in the public sources.yml, a private file in the private one (thesis-ci)
        cited = {m.group(1) or m.group(2) for w in writes if w.repo == repo and w.path.endswith((".md", ".yml"))
                 for m in _CITED_RE.finditer(w.text)}
        if repo == registry.PRIVATE_REPO:
            cited |= {m.group(1) or m.group(2) for w in writes if w.path.endswith((".md", ".yml"))
                      for m in _CITED_RE.finditer(w.text)}  # public entries are mirrored privately (0028)
        text = _current_text(writes, roots, repo, path)
        data = yaml.safe_load(text) if text else None
        known = {str(e.get("tag")) for e in (data or {}).get("sources") or [] if isinstance(e, dict)}
        # a private file resolves tags in the private sources.yml: a tag only the public one registers is mirrored
        mirror = public_entries if repo == registry.PRIVATE_REPO else {}
        found = {**mirror, **supplied}
        missing = sorted(t for t in cited if t and t not in known and t in found
                         and not (repo == registry.PUBLIC_REPO and found[t].get("visibility") == _outputs.PRIVATE))
        if not missing:
            continue
        merged, _, trouble = merge_sources(text, [{k: v for k, v in found[t].items() if k != "visibility"}
                                                  for t in missing])
        if trouble:
            raise ValueError("; ".join(trouble))
        _upsert(writes, PlannedWrite("sources_additions", repo, visibility, path, str(merged).encode("utf-8"),
                                     action="merge"))
        added |= set(missing)
    missing = sorted(added)
    if not missing:
        return
    warnings.append(f"registered {len(missing)} cited document(s) the pipeline had supplied: {', '.join(missing)}")


def route_by_trust(writes: list[PlannedWrite], manifest: Mapping[str, Any], roots: Roots, *, publish: bool,
                   warnings: list[str], problems: list[str]) -> dict[str, Any] | None:
    """00 section G9 for a quarterly update: at trust level 1 or below its public files are staged in the private
    repository (runs/<scope>/<run_dir>/staged/<path>) until HQ has reviewed them; `publish` then writes them to the
    public branch, provided the staged copies are exactly what would be written now. Returns the routing record."""
    if str(manifest["step"]) not in QUARTERLY_UPDATE_STEPS:
        return None
    level, notes = trust_level(roots.public, str(manifest["company"]))
    warnings += notes
    run_dir = (manifest.get("context") or {}).get("run_dir") or f"{manifest['run_date']}-{manifest['step']}"
    staging = f"runs/{manifest['scope']}/{run_dir}/{STAGED_DIR}"
    public = [w for w in writes if w.repo == registry.PUBLIC_REPO]
    routing: dict[str, Any] = {"trust_level": level, "rule": "00 section G9"}
    if level is None:
        problems.append(f"{manifest['company']} has no trust level; a quarterly update is routed by it (section G9)")
        return routing
    if level >= 2:
        routing["route"] = "public branch" + ("; HQ reviews before publication (17A)" if level == 2 else "")
        if publish:
            warnings.append(f"--publish is for staged updates; at trust level {level} they go to the branch directly")
        return routing
    if level == 0:
        warnings.append("trust level 0: autonomy is suspended; this goes into the letter under 'For your attention'")
    if not publish:
        for w in public:
            w.staged_from, w.public_bound = w.path, True
            w.repo, w.visibility, w.path = registry.PRIVATE_REPO, _outputs.PRIVATE, f"{staging}/{w.path}"
        routing["route"] = f"staged in {registry.PRIVATE_REPO}:{staging}/ until HQ has reviewed it item by item"
        return routing
    for w in public:
        staged = roots.private / staging / w.path
        if not staged.is_file():
            problems.append(f"{w.path} was not staged; place the bundle without --publish first, for HQ's review")
        elif staged.read_bytes() != w.content:
            problems.append(f"{w.path}: what would be published now differs from the staged copy HQ reviewed (the "
                            "public file changed since); place the bundle again without --publish")
    routing["route"] = "published after HQ's review of the staged copies"
    return routing


def place(bundle: str | os.PathLike[str], *, roots: Roots | None = None, branch: str | None = None,
          announced: str | None = None, window: Sequence[str] | None = None, check: bool = False,
          allow_cjk: bool = False, allow_fake: bool = False, lint: bool = True, now: dt.datetime | None = None,
          edgar_gateway: Any = None, schemas_dir: str | os.PathLike[str] | None = None, publish: bool = False,
          out: TextIO | None = None) -> dict[str, Any]:
    """Copy each output of an executed bundle to its 00 section F2 destination. Returns the placement record.

    Actions (docs/decisions/0024): write; front_matter (reviewed_sections into the update record); append (the
    mistakes entry, newest first); merge (sources_additions into each repository's sources.yml by visibility); patch
    (dossier_changes, part by part); pr_body (03's body plus the audit findings, the qualitative rulings, the inversion
    list and the divergence map, kept as pr_body.md in the run directory); pr_attachment (nothing written; attached
    through the PR body). A quarterly update is routed by trust level (section G9): at level 1 or below its public
    files are staged in the private repository, and `publish` writes them to the public branch after HQ's review."""
    roots = roots or resolve_roots()
    out = out or sys.stdout
    schemas = Path(schemas_dir) if schemas_dir else None
    now = now or _utcnow()
    bundle_dir = resolve_bundle(bundle, roots.private)
    manifest = load_manifest(bundle_dir)
    rel = manifest["bundle"]
    if manifest.get("role") == PIPELINE_BACKEND:
        raise RunnerError(f"{rel} is a deterministic step; its results are read from the bundle, nothing is placed")
    if manifest["step"] == "03-draft":
        raise RunnerError(f"{rel} is the draft; 03R revises it after the audit, and the 03R bundle is placed")
    record = load_run_record(bundle_dir)
    if record.get("status") != "succeeded":
        raise RunnerError(f"{rel} did not succeed (status {record.get('status')}); there is nothing to place")
    real = record.get("backend") in REAL_BACKENDS and record.get("client") in REAL_BACKENDS
    if not real and not allow_fake:
        raise RunnerError(f"{rel} was produced by the {record.get('client')} client (a dry run or a test); its outputs "
                          "are placeholders and are never placed")
    base = bundle_dir.parents[2]
    entries = {k: v for k, v in (record.get("outputs") or {}).items() if v.get("status") == "written"}
    header, basis = (prereg_header_for(manifest, roots, announced=announced, window=window,
                                       edgar_gateway=edgar_gateway) if "prereg" in entries else (None, None))
    writes: list[PlannedWrite] = []
    problems: list[str] = []
    warnings: list[str] = []
    attachments: list[dict[str, Any]] = []
    merged_prereg: Any = None
    order = ("write", "front_matter", "append", "merge", "patch", "settlement", "pr_body", "pr_attachment")
    planned: list[tuple[int, str, str, list[_outputs.Placement]]] = []
    review, decision = (approving_review(roots.private, manifest)
                        if any(n in entries for n in VALUATION_OUTPUTS) else (None, ""))
    if review is None and any(n in entries for n in VALUATION_OUTPUTS):
        problems.append("the valuation is a proposed version: 04C has not reviewed it, and it takes effect only when "
                        "04C approves it (00 §V20)")
    elif review is not None and decision != "approved":
        problems.append(f"04C ({review}) did not approve this valuation (decision: {decision or 'none'}); it goes back "
                        "to 01C with the findings (00 §V20)")
    for name, entry in entries.items():
        data = (bundle_dir / str(entry["file"])).read_bytes()
        if _sha(data) != entry.get("sha256"):
            problems.append(f"{entry['file']}: sha256 differs from {RUN_RECORD}")
            continue
        text = data.decode("utf-8")
        if name in VALUATION_OUTPUTS and decision == "approved":
            text = mark_effective(text)
            warnings.append(f"{name}: approved by 04C ({review}); written as doc_status: effective")
        if name == "prereg" and header is not None:
            text, merged_prereg, notes, prereg_problems = prereg_document(text, header, manifest, schemas)
            warnings += notes
            problems += [f"prereg: {p}" for p in prereg_problems]
        try:
            placements = _outputs.place(name, prompt_id=_prompt_id(manifest), part_id=str(manifest["step"]),
                                        fmt=str(entry["format"]), **(manifest.get("context") or {}))
        except _outputs.PlacementError as exc:
            problems.append(f"{name}: {exc}")
            continue
        rank = min(order.index(p.action) if p.action in order else len(order) for p in placements)
        planned.append((rank, name, text, placements))
    for _, name, text, placements in sorted(planned, key=lambda row: row[0]):
        try:
            plan_actions(name, text, placements, manifest, roots, base, writes, problems, warnings, attachments,
                         bundle_dir, schemas)
        except (ValueError, yaml.YAMLError) as exc:
            problems.append(f"{name}: {exc}")
    try:
        complete_sources(writes, manifest, roots, warnings)
    except (ValueError, yaml.YAMLError) as exc:
        problems.append(f"sources: {exc}")
    routing = route_by_trust(writes, manifest, roots, publish=publish, warnings=warnings, problems=problems)
    for write in writes:
        if write.visibility == _outputs.PUBLIC or write.public_bound:
            write.cjk_lines = cjk_lines(write.text)
        root = roots.public if write.repo == registry.PUBLIC_REPO else roots.private
        if not (root / write.path).exists():
            continue
        if write.action == "write" and write.output in REPLACED_OUTPUTS and _prompt_id(manifest) == "03" \
                and write.staged_from is None:
            if (root / write.path).read_bytes() == write.content:
                write.status = "same"
                continue
            problem = _replace_guard(write, manifest, base, root)
            if problem:
                problems.append(problem)
            write.status = "replace"
        elif write.action != "write" or write.staged_from is not None:
            write.status = "same" if (root / write.path).read_bytes() == write.content else "update"
        else:
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
    for w in (w for w in writes if w.action == "pr_body"):
        try:
            hits = h4_hits(w.text)
        except LookupError:
            warnings.append(f"{w.path}: the section H4 wording of the PR body was not checked (thesis-ci missing)")
            continue
        if hits:
            problems.append(f"{w.path}: the PR body contains section H4 wording ({'; '.join(hits[:5])}); it goes "
                            "public with the pull request")
    report: dict[str, Any] = {
        "placement_version": 1,
        "bundle": rel,
        "placed_at": _iso(now),
        "check_only": check,
        "publish": publish,
        "prereg_header": {"basis": basis, **header} if header else None,
        "routing": routing,
        "files": [{"output": w.output, "repo": w.repo, "visibility": w.visibility, "path": w.path,
                   "action": w.action, "status": w.status, "sha256": _sha(w.content), "remove": w.remove,
                   **({"staged_from": w.staged_from} if w.staged_from else {}),
                   **({"note": w.note} if w.note else {}),
                   **({"cjk_lines": len(w.cjk_lines)} if w.cjk_lines else {})} for w in writes],
        "attachments": attachments,
        "warnings": warnings,
        "allow_cjk": allow_cjk,
    }
    if problems:
        raise RunnerError(f"cannot place {rel}:\n- " + "\n- ".join(problems))
    if check:
        _print_placement(report, out)
        return report
    changed = [w for w in writes if w.status != "same"]
    public_changes = [w for w in changed if w.repo == registry.PUBLIC_REPO]
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
    write_yaml(bundle_dir / (PUBLICATION_RECORD if publish else PLACEMENT_RECORD), report)
    _print_placement(report, out)
    return report


def _print_placement(report: Mapping[str, Any], out: TextIO) -> None:
    verb = "would place" if report.get("check_only") else ("published" if report.get("publish") else "placed")
    print(f"{verb} {report['bundle']}:", file=out)
    for f in report["files"]:
        extra = f"; removes {', '.join(f['remove'])}" if f.get("remove") else ""
        extra += f"; staged for {f['staged_from']}" if f.get("staged_from") else ""
        print(f"  {f['output']:<16} -> {f['repo']}:{f['path']} ({f['visibility']}, {f['action']}, {f['status']}{extra})",
              file=out)
    for a in report.get("attachments") or []:
        print(f"  {a['output']:<16} -> attached {a['attached']}", file=out)
    if report.get("routing"):
        routing = report["routing"]
        print(f"  trust level {routing.get('trust_level')}: {routing.get('route')}", file=out)
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
              "open a pull request (its body: pr_body.md in the run directory, if the bundle wrote one); for a "
              "pre-registration the timestamp workflow stamps it after the merge", file=out)
    if not report.get("check_only"):
        record = PUBLICATION_RECORD if report.get("publish") else PLACEMENT_RECORD
        print(f"  private files: commit them in {registry.PRIVATE_REPO} together with {report['bundle']}/{record}",
              file=out)


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
    p.add_argument("--subject", choices=[registry.ARCHIVE_SUBJECT], default=None,
                   help="16A and 04A on a new archive's dossier (decisions/0028)")
    p.add_argument("--round", dest="round_", type=int, default=1, help="the round of a step (01A 2: the revision)")
    p.add_argument("--rerun", action="store_true",
                   help="assemble the step again on the same run date when its bundle exists (-rerun<n>)")
    p = sub.add_parser("dry-run", parents=[roots, step_args], help="assemble and execute with the fake backend")
    p.add_argument("--out", type=Path, default=None, help=f"output directory (default: <workspace>/{DRY_RUN_DIR})")
    p.add_argument("--replace", action="store_true", help="replace an earlier dry-run bundle of the same step and date")
    p = sub.add_parser("evaluate", parents=[roots],
                       help="evaluate the quantitative tests of an earnings event (ci_results; deterministic, no model)")
    p.add_argument("company", help="ticker (companies/<TICKER>/)")
    p.add_argument("period", help="FY<year>Q<quarter>")
    p.add_argument("--run-date", type=_date_arg, default=None, help="YYYY-MM-DD (default: today)")
    p.add_argument("--offline", action="store_true", help="EDGAR from the local cache only")
    p.add_argument("--allow-dirty", action="store_true", help="evaluate with uncommitted pipeline code (recorded)")
    p.add_argument("--retry", action="store_true", help="evaluate a failed bundle again")
    p.add_argument("--out", type=Path, default=None,
                   help="a dry-run directory outside both repositories (default: the private repository)")
    p = sub.add_parser("event", parents=[roots],
                       help="run the post-earnings chain of one event step by step, with stops for review "
                            "(pipeline/chain.py)")
    p.add_argument("company", help="ticker (companies/<TICKER>/)")
    p.add_argument("period", help="FY<year>Q<quarter>")
    p.add_argument("--run-date", type=_date_arg, default=None, help="YYYY-MM-DD for new bundles (default: today)")
    p.add_argument("--dry-run", action="store_true",
                   help="the fake backend, outside both repositories; a rehearsal when the event has not happened")
    p.add_argument("--out", type=Path, default=None, help=f"dry-run directory (default: <workspace>/{DRY_RUN_DIR})")
    p.add_argument("--approve", action="append", default=[], choices=("draft", "audit", "placement"),
                   help="record that the bundle under review at this stop was reviewed, and continue")
    p.add_argument("--retry", action="store_true", help="run a failed bundle of the chain again")
    p.add_argument("--backend", choices=REAL_BACKENDS, default=None,
                   help=f"model backend of a real run (default: {llm.BACKEND_ENV}, else {DEFAULT_BACKEND})")
    p.add_argument("--allow-dirty", action="store_true", help="assemble from uncommitted pipeline code (recorded)")
    p.add_argument("--offline", action="store_true", help="EDGAR from the local cache only")
    p.add_argument("--no-lint", action="store_true", help="skip thesis-ci lint when placing")
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
    p.add_argument("--publish", action="store_true",
                   help="after HQ's review: write a staged quarterly update (trust level 1 or below, section G9) to "
                        "the public branch")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "steps":
        for spec in registry.STEPS.values():
            part = f" part {spec.part}" if spec.part else ""
            print(f"{spec.step:<5} prompt {spec.prompt_id}{part}; {spec.scope}; period {spec.period_kind}; {spec.summary}")
        return 0
    roots = resolve_roots(args.public_root, args.private_root, args.workspace_root)
    saved_env_file = os.environ.get(edgar.ENV_FILE_ENV)
    use_workspace_env_file(roots)
    try:
        if args.command == "assemble":
            bundle_dir = assemble(args.step, args.company, args.period, run_date=args.run_date, roots=roots,
                                  offline=args.offline, allow_dirty=args.allow_dirty, schemas_dir=args.schemas_dir,
                                  subject=args.subject, round_=args.round_, rerun=args.rerun)
            print(describe_bundle(bundle_dir))
            return 0
        if args.command == "dry-run":
            bundle_dir, record = dry_run(args.step, args.company, args.period, run_date=args.run_date, roots=roots,
                                         out_root=args.out, replace=args.replace, offline=args.offline,
                                         schemas_dir=args.schemas_dir)
            print(describe_bundle(bundle_dir))
            print(f"dry-run bundle: {bundle_dir}")
            return 0 if record.get("status") == "succeeded" else 1
        if args.command == "evaluate":
            _, record = evaluate(args.company, args.period, run_date=args.run_date, roots=roots, out_root=args.out,
                                 offline=args.offline, allow_dirty=args.allow_dirty, schemas_dir=args.schemas_dir,
                                 retry=args.retry)
            return 0 if record.get("status") == "succeeded" else 1
        if args.command == "event":
            from . import chain  # the chain imports this module

            code, _ = chain.run_event(args.company, args.period, run_date=args.run_date or dt.date.today(),
                                      roots=roots, dry_run=args.dry_run, out_root=args.out, backend=args.backend,
                                      approve_stops=args.approve, retry=args.retry, allow_dirty=args.allow_dirty,
                                      offline=args.offline, schemas_dir=args.schemas_dir, lint=not args.no_lint)
            return code
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
                  check=args.check, allow_cjk=args.allow_cjk, lint=not args.no_lint, schemas_dir=args.schemas_dir,
                  publish=args.publish)
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
    finally:
        if saved_env_file is None:
            os.environ.pop(edgar.ENV_FILE_ENV, None)
        else:
            os.environ[edgar.ENV_FILE_ENV] = saved_env_file
    return 2


if __name__ == "__main__":
    sys.exit(main())
