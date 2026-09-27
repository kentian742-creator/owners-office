"""The post-earnings chain of one earnings event, run step by step with stops for human review
(docs/decisions/0024; the per-event flow of docs/DESIGN.md).

    python -m pipeline.runner event AXP FY2026Q3 --run-date 2026-10-21 [--dry-run] [--approve draft|audit|placement]

Each call looks at the state of every stage and runs what is due, in order, until the chain reaches a stop, a
failure, or its end. It is resumable: a bundle that succeeded is never run again (whatever its run date), an
assembled bundle is executed, a failed one stops the chain until --retry, and a stop is passed only once someone has
approved it (--approve writes review.yml into the bundle under review). New bundles take --run-date.

Stages, from the prompts' scopes (candidates get the quantitative tests and the company manager's draft; holdings
also get the blind read, the independent judge, the red team, the divergence map, the settlement and the gate):

    16B      metric extraction          when a due test reads a metric from filing text
    ci       evaluation (no model)       ci_results: readings and results of the quantitative tests
    14T      qualitative tests           holdings, when a qualitative test is due
    14A      blind read                  holdings
    15B      settlement                  when a pre-registration item or a ledger entry is due
    03-draft quarterly update draft
    -- stop "draft": review the draft --
    16A      fact extraction (update mode)
    04A      fact audit
    04B-lite inversion list              holdings
    14B      divergence map              holdings
    -- stop "audit": review the audit --
    03R      revision after the audit
    17A      HQ review and release gate  holdings
    -- stop "placement": review the revision before anything is placed --
    place    every bundle of the chain (not the draft or ci); a quarterly update below trust level 2 is staged in the
             private repository (section G9) until `place --publish`

A dry run (--dry-run) runs the same chain with the fake backend into a directory outside both repositories, as a
rehearsal when the event has not happened yet; its stops are passed without review and its placement is a check
(nothing is written anywhere).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from . import edgar, evaluation, registry, runner

STOPS = ("draft", "audit", "placement")
REVIEW_RECORD = "review.yml"
WAITING = 3  # exit status of a chain that stopped for review (as scripts/accept.py's PENDING)


@dataclasses.dataclass(frozen=True)
class Stage:
    name: str  # a step label, "ci", "stop:<name>" or "place"
    holdings_only: bool = False
    skip: Callable[[registry.RunContext], str | None] | None = None  # returns why the stage is not due, or None
    review: str | None = None  # for a stop: the step whose bundle is reviewed

    @property
    def kind(self) -> str:
        if self.name == registry.CI_STEP:
            return "evaluate"
        if self.name.startswith("stop:"):
            return "stop"
        return "place" if self.name == "place" else "model"


def _no_text_metrics(ctx: registry.RunContext) -> str | None:
    metrics, _ = registry.metric_registry(ctx)
    definitions, _, _ = evaluation.text_metric_definitions(ctx.thesis(), ctx.period, ctx.run_date, metrics)
    return None if definitions else "no due test reads a metric from filing text"


def _no_qualitative(ctx: registry.RunContext) -> str | None:
    due, _ = registry.due_qualitative(ctx)
    return None if due else "no qualitative test is due"


def _nothing_to_settle(ctx: registry.RunContext) -> str | None:
    items = registry.prereg_items_due(ctx) if ctx.is_holding() else []
    return None if items or registry.ledger_entries_due(ctx) else "no pre-registration item or ledger entry is due"


CHAIN: tuple[Stage, ...] = (
    Stage("16B", skip=_no_text_metrics),
    Stage(registry.CI_STEP),
    Stage("14T", holdings_only=True, skip=_no_qualitative),
    Stage("14A", holdings_only=True),
    Stage("15B", skip=_nothing_to_settle),
    Stage("03-draft"),
    Stage("stop:draft", review="03-draft"),
    Stage("16A"),
    Stage("04A"),
    Stage("04B-lite", holdings_only=True),
    Stage("14B", holdings_only=True),
    Stage("stop:audit", review="04A"),
    Stage("03R"),
    Stage("17A", holdings_only=True),
    Stage("stop:placement", review="03R"),
    Stage("place"),
)
NOT_PLACED = frozenset({registry.CI_STEP, "03-draft"})


@dataclasses.dataclass
class StageState:
    stage: str
    state: str  # done | ran | failed | skipped | waiting | passed | placed | checked | not reached
    detail: str = ""
    bundle: str | None = None


def chain_for(status: str) -> list[Stage]:
    """The stages of an event for a holding or a candidate (prompt scopes)."""
    return [s for s in CHAIN if status == "holding" or not s.holdings_only]


def _operator(root: Path) -> str:
    return (registry.git_output(root, "config", "user.name") or "").strip() or "operator"


def approve(bundle_dir: Path, stop: str, *, root: Path, now: dt.datetime | None = None) -> dict[str, Any]:
    """Record that a person reviewed the bundle at a stop: review.yml in the bundle (never inside inputs/)."""
    record = {"stop": stop, "bundle": runner.load_manifest(bundle_dir)["bundle"], "approved_by": _operator(root),
              "approved_at": runner._iso(now or runner._utcnow())}
    runner.write_yaml(bundle_dir / REVIEW_RECORD, record)
    return record


def approved(bundle_dir: Path, stop: str) -> bool:
    data = registry.load_yaml_file(bundle_dir / REVIEW_RECORD)
    return isinstance(data, dict) and data.get("stop") == stop


def run_event(company: str, period: str, *, run_date: dt.date, roots: runner.Roots | None = None,
              dry_run: bool = False, out_root: str | os.PathLike[str] | None = None, backend: str | None = None,
              approve_stops: Sequence[str] = (), retry: bool = False, allow_dirty: bool = False,
              edgar_gateway: Any = None, offline: bool = False, schemas_dir: str | os.PathLike[str] | None = None,
              today: dt.date | None = None, client_factory: Callable[[Path], Any] | None = None,
              lint: bool = True, out: TextIO | None = None) -> tuple[int, list[StageState]]:
    """Run the chain of one event as far as it can go. Returns (exit status, the state of every stage): 0 when the
    chain is complete, WAITING (3) at a stop, 1 on a failure. `client_factory` injects a model test double per
    bundle; `approve_stops` approves stops before running."""
    roots = roots or runner.resolve_roots()
    out = out or sys.stdout
    today = today or dt.date.today()
    ticker = str(company).strip().upper()
    unknown = [s for s in approve_stops if s not in STOPS]
    if unknown:
        raise runner.RunnerError(f"--approve takes {', '.join(STOPS)}, got {', '.join(unknown)}")
    base = runner.dry_run_base(roots, out_root) if dry_run else roots.private
    if not dry_run and out_root:
        raise runner.RunnerError("--out is for dry runs; a real chain writes its bundles into the private repository")
    gateway = edgar_gateway if edgar_gateway is not None else registry.EdgarGateway(offline=offline)
    status = _status(roots, ticker)
    log = runner.seed_dry_run_log(roots, base) if dry_run else None
    backend = "fake" if dry_run else backend
    ctx = _context(roots, ticker, period, run_date, base, gateway, schemas_dir, dry_run)
    states: list[StageState] = []
    code = 0
    for stage in chain_for(status):
        if code:
            states.append(StageState(stage.name, "not reached"))
            continue
        try:
            state = _run_stage(stage, ctx, roots=roots, base=base, ticker=ticker, period=period, run_date=run_date,
                               dry_run=dry_run, backend=backend, log=log, approve_stops=approve_stops, retry=retry,
                               allow_dirty=allow_dirty, gateway=gateway, offline=offline, schemas_dir=schemas_dir,
                               today=today, client_factory=client_factory, lint=lint)
        except (runner.RunnerError, registry.MissingInput, edgar.EdgarError) as exc:
            state = StageState(stage.name, "failed", str(exc))
        states.append(state)
        if state.state == "failed":
            code = 1
        elif state.state == "waiting":
            code = WAITING
    _print_states(ticker, period, run_date, dry_run, states, code, out)
    return code, states


def _status(roots: runner.Roots, ticker: str) -> str:
    thesis = registry.load_yaml_file(roots.public / "companies" / ticker / "thesis.yml")
    if not isinstance(thesis, dict):
        raise runner.RunnerError(f"companies/{ticker}/thesis.yml does not exist in {registry.PUBLIC_REPO}")
    status = str(thesis.get("status") or "")
    if status not in ("holding", "candidate"):
        raise runner.RunnerError(f"{ticker} is {status!r}; quarterly updates run for holdings and candidates")
    return status


def _context(roots: runner.Roots, ticker: str, period: str, run_date: dt.date, base: Path, gateway: Any,
             schemas_dir: Any, dry_run: bool) -> registry.RunContext:
    """A context for the chain's own decisions (which stages are due); assemblers get their own."""
    return registry.RunContext(step=runner.CI_SPEC, company=ticker, period=period, run_date=run_date,
                               public_root=roots.public, private_root=roots.private, workspace_root=roots.workspace,
                               call=None, formats={}, edgar=gateway,
                               schemas_dir=Path(schemas_dir) if schemas_dir else None,
                               runs_roots=(base,) if dry_run else (), rehearsal=dry_run)


def _existing(base: Path, step: str, ticker: str, period: str) -> list[registry.PriorRun]:
    """The chain's bundles of a step for this event, oldest first (any run date), in the chain's own directory."""
    return [r for r in registry.index_runs(base) if r.manifest and r.step == step and r.company == ticker
            and r.period == period]


def _bundle_path(base: Path, step: str, ticker: str, run_date: dt.date) -> Path:
    spec = runner.CI_SPEC if step == registry.CI_STEP else registry.step_spec(step)
    return base / "runs" / spec.storage_scope(ticker) / spec.bundle_name(run_date, ticker)


def _run_stage(stage: Stage, ctx: registry.RunContext, *, roots: runner.Roots, base: Path, ticker: str, period: str,
               run_date: dt.date, dry_run: bool, backend: str | None, log: Path | None,
               approve_stops: Sequence[str], retry: bool, allow_dirty: bool, gateway: Any, offline: bool,
               schemas_dir: Any, today: dt.date, client_factory: Callable[[Path], Any] | None,
               lint: bool) -> StageState:
    if stage.kind == "stop":
        return _stop(stage, base=base, ticker=ticker, period=period, dry_run=dry_run, approve_stops=approve_stops,
                     root=roots.private)
    if stage.kind == "place":
        return _place_all(ctx, roots=roots, base=base, ticker=ticker, period=period, dry_run=dry_run,
                          gateway=gateway, schemas_dir=schemas_dir, lint=lint)
    if stage.kind == "model" and stage.name not in registry.STEPS:
        return StageState(stage.name, "failed", f"the pipeline has no step {stage.name} yet (docs/decisions/0024)")
    runs = _existing(base, stage.name, ticker, period)
    done = [r for r in runs if r.status == "succeeded"]
    if done:
        return StageState(stage.name, "done", bundle=done[-1].rel)
    if stage.skip is not None:
        why = stage.skip(ctx)
        if why:
            return StageState(stage.name, "skipped", why)
    if stage.kind == "evaluate":
        pending = runs[-1] if runs else None
        if pending is not None and pending.status == "failed" and not retry:
            return StageState(stage.name, "failed", f"{pending.rel} failed; --retry evaluates it again", pending.rel)
        bundle_dir, record = runner.evaluate(ticker, period, run_date=pending.run_date if pending else run_date,
                                             roots=roots, out_root=base if dry_run else None, edgar_gateway=gateway,
                                             offline=offline, allow_dirty=allow_dirty, schemas_dir=schemas_dir,
                                             today=today, retry=pending is not None, out=_Quiet())
        return _result(stage.name, bundle_dir, record)
    pending = runs[-1] if runs else None
    if pending is not None and pending.status == "failed" and not retry:
        return StageState(stage.name, "failed", f"{pending.rel} failed ({_error_type(pending)}); --retry runs it again",
                          pending.rel)
    if pending is not None:
        bundle_dir = pending.path
    else:
        bundle_dir = runner.assemble(stage.name, ticker, period, run_date=run_date, roots=roots,
                                     out_root=base if dry_run else None, edgar_gateway=gateway, offline=offline,
                                     allow_dirty=allow_dirty or dry_run, schemas_dir=schemas_dir, today=today,
                                     rehearsal=dry_run and registry.step_spec(stage.name).post_event)
    client = client_factory(bundle_dir) if client_factory else None
    record = runner.execute(bundle_dir, roots=roots, backend=backend, log_path=log, client=client,
                            retry=pending is not None and pending.status == "failed", schemas_dir=schemas_dir,
                            allow_unpinned=dry_run, env={} if dry_run else None, out=_Quiet())
    return _result(stage.name, bundle_dir, record)


def _result(name: str, bundle_dir: Path, record: Mapping[str, Any]) -> StageState:
    rel = "/".join(bundle_dir.parts[-3:])
    if record.get("status") == "succeeded":
        return StageState(name, "ran", _outcome(record), rel)
    return StageState(name, "failed", f"{(record.get('error') or {}).get('type')}; details in {rel}/run.yml", rel)


def _outcome(record: Mapping[str, Any]) -> str:
    if record.get("results"):
        return ", ".join(f"{k} {v}" for k, v in sorted(record["results"].items()))
    written = [k for k, v in (record.get("outputs") or {}).items() if v.get("status") == "written"]
    return f"{len(written)} output(s); {record.get('requests', 0)} request(s)"


def _error_type(run: registry.PriorRun) -> str:
    return str(((run.record or {}).get("error") or {}).get("type") or "unknown error")


def _stop(stage: Stage, *, base: Path, ticker: str, period: str, dry_run: bool, approve_stops: Sequence[str],
          root: Path) -> StageState:
    name = stage.name.split(":", 1)[1]
    runs = [r for r in _existing(base, str(stage.review), ticker, period) if r.status == "succeeded"]
    if not runs:
        return StageState(stage.name, "failed", f"no succeeded {stage.review} bundle to review")
    bundle = runs[-1]
    if dry_run:
        return StageState(stage.name, "passed", "a dry run passes its stops without review", bundle.rel)
    if approved(bundle.path, name):
        return StageState(stage.name, "passed", "approved", bundle.rel)
    if name in approve_stops:
        record = approve(bundle.path, name, root=root)
        return StageState(stage.name, "passed", f"approved now by {record['approved_by']}", bundle.rel)
    return StageState(stage.name, "waiting", f"review {bundle.rel} (inputs/, outputs/, run.yml), then run again with "
                                             f"--approve {name}", bundle.rel)


def _place_all(ctx: registry.RunContext, *, roots: runner.Roots, base: Path, ticker: str, period: str,
               dry_run: bool, gateway: Any, schemas_dir: Any, lint: bool) -> StageState:
    """Place every bundle of the chain that is not placed yet (the draft and ci are not placed). A dry run checks the
    plan only."""
    runs = [r for r in registry.index_runs(base) if r.succeeded and r.company == ticker and r.period == period
            and r.step not in NOT_PLACED]
    placed, checked = [], []
    for run in sorted(runs, key=lambda r: (r.step == "03R", r.run_date, r.rel)):  # 03R last: its PR body attaches
        if (run.path / runner.PLACEMENT_RECORD).is_file():
            continue
        report = runner.place(run.path, roots=roots, check=dry_run, allow_fake=dry_run, lint=lint and not dry_run,
                              edgar_gateway=gateway, schemas_dir=schemas_dir, out=_Quiet())
        (checked if dry_run else placed).append(f"{run.step}: {len(report['files'])} file(s)"
                                                + (f", {report['routing']['route']}" if report.get("routing") else ""))
    if dry_run:
        return StageState("place", "checked", "; ".join(checked) or "nothing to place")
    return StageState("place", "placed", "; ".join(placed) or "everything was placed already")


class _Quiet:
    """Swallows the per-command printouts; the chain prints one line per stage instead."""

    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        pass


def _print_states(ticker: str, period: str, run_date: dt.date, dry_run: bool, states: Sequence[StageState],
                  code: int, out: TextIO) -> None:
    mode = "dry run (fake backend, outside both repositories)" if dry_run else "run"
    print(f"event {ticker} {period} (run date {run_date}, {mode}):", file=out)
    for s in states:
        where = f" [{s.bundle}]" if s.bundle else ""
        print(f"  {s.stage:<15} {s.state:<11} {s.detail}{where}", file=out)
    verdict = {0: "complete", 1: "stopped on a failure", WAITING: "waiting for review"}[code]
    print(f"chain {verdict}", file=out)
