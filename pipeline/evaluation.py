"""Quantitative thesis tests: which tests are due, what the extractor reads, and the evaluation that yields ci_results
(docs/decisions/0024). This module calls no model and opens no network connection.

The evaluation itself belongs to thesis-ci (the format contract's owner), through this interface:

- thesis_ci.metrics.readings_from_companyfacts(companyfacts, metric_ids, periods, fiscal_year_end, *, ticker=None,
  definitions=None, currency=None) -> [reading] (pure; the pipeline fetches companyfacts through pipeline.edgar,
  cached);
- thesis_ci.evaluate.evaluate_company(thesis, readings, period, today) -> the ci_results document: per test its id,
  result (pass / warn / fail / undetermined / not_due), the reading {value, unit, period, source, basis}, the
  readings used, threshold, op, fail_if, warn_if, consecutive_count and reason.

A reading is {metric, period, value, unit, source, basis?, note?}. Readings of metrics that are not in XBRL come from
16B's metric_values (readings_from_metric_values()); readings from XBRL come from thesis-ci. thesis-ci provides the
interface from v0.4.0 (requirements.txt); a dry run whose thesis-ci does not provide it uses PlaceholderEvaluator, which reads nothing and records every test as
undetermined with that reason; the real `evaluate` command never does.

Here, too:
- which tests are active for a period (effective_from, retired_at) and readable by a date (first_readable);
- metric_definitions for 16B: the definitions (spec/metrics.yml or the test's metric_def) of the active tests whose
  data is read from filing text, without thresholds, claims or notes (00 section G6: the extractor sees no thesis).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import importlib
import inspect
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from . import documents

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_METRICS_FILE = REPO_ROOT.parent / "thesis-ci" / "spec" / "metrics.yml"
TEXT_DATA = frozenset({"filing_text", "mixed"})  # read by 16B (mixed: the components that come from filing text)
XBRL_DATA = frozenset({"xbrl", "mixed"})
RESULTS = ("pass", "warn", "fail", "undetermined", "not_due")
NOT_FOUND = "not_found"
_NUMBER_RE = re.compile(r"^\(?[-−+]?\$?\s*\d[\d,]*(?:\.\d+)?\)?\s*%?$")


class EvaluatorUnavailable(RuntimeError):
    """The installed thesis-ci does not provide thesis_ci.metrics / thesis_ci.evaluate (added in v0.4.0)."""


# ---------------------------------------------------------------------------------------------------- tests


def _ordinal(period: Any) -> int | None:
    try:
        return documents.period_index(str(period))
    except ValueError:
        return None


def is_active(test: Mapping[str, Any], period: str) -> bool:
    """Judged in `period`: effective_from is not later, and the test was not retired at or before it (SPEC 4.1)."""
    now = documents.period_index(period)
    start = _ordinal(test.get("effective_from"))
    if start is not None and now < start:
        return False
    retired = _ordinal(test.get("retired_at"))
    return retired is None or now < retired


def first_readable(test: Mapping[str, Any]) -> dt.date | None:
    """first_readable as a date: YYYY-MM means the first day of that month."""
    text = str(test.get("first_readable") or "").strip()
    if not text:
        return None
    try:
        return dt.date.fromisoformat(text) if len(text) == 10 else dt.date.fromisoformat(text + "-01")
    except ValueError:
        return None


def readable_by(test: Mapping[str, Any], day: dt.date) -> bool:
    """Whether the test's first reading can exist by `day` (no first_readable means it always can). A month counts
    as readable during all of it."""
    first = first_readable(test)
    if first is None:
        return True
    if len(str(test.get("first_readable")).strip()) == 7:
        return (day.year, day.month) >= (first.year, first.month)
    return day >= first


def tests_of(thesis: Mapping[str, Any], kind: str) -> list[dict[str, Any]]:
    return [t for t in thesis.get("tests") or [] if isinstance(t, dict) and t.get("type") == kind]


def due_tests(thesis: Mapping[str, Any], kind: str, period: str, day: dt.date) -> tuple[list[dict[str, Any]],
                                                                                         list[dict[str, str]]]:
    """(tests of this kind judged for `period` and readable by `day`, [{test_id, reason}] for the others)."""
    due, skipped = [], []
    for test in tests_of(thesis, kind):
        tid = str(test.get("id"))
        if not is_active(test, period):
            skipped.append({"test_id": tid, "reason": f"not active in {period} (effective_from "
                                                      f"{test.get('effective_from')}, retired_at {test.get('retired_at')})"})
        elif not readable_by(test, day):
            skipped.append({"test_id": tid, "reason": f"first readable {test.get('first_readable')}, after {day}"})
        else:
            due.append(test)
    return due, skipped


# ---------------------------------------------------------------------------------------------------- metrics


def load_metric_registry(schemas_dir: Path | None = None) -> tuple[dict[str, dict[str, Any]], str]:
    """spec/metrics.yml keyed by id, and where it was read: next to a given schemas directory, else the installed
    thesis-ci, else a sibling checkout."""
    candidates = [Path(schemas_dir).parent / "metrics.yml"] if schemas_dir else []
    for path in candidates:
        if path.is_file():
            return _metrics_from(path), f"metrics registry {path}"
    try:
        from thesis_ci import contract
    except ImportError:
        contract = None
    if contract is not None:
        try:
            import thesis_ci

            return dict(contract.metrics()), f"thesis-ci {getattr(thesis_ci, '__version__', '?')}"
        except (OSError, KeyError, TypeError):
            pass
    if DEFAULT_METRICS_FILE.is_file():
        return _metrics_from(DEFAULT_METRICS_FILE), "sibling thesis-ci checkout"
    return {}, "no metrics registry found"


def _metrics_from(path: Path) -> dict[str, dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {str(m["id"]): m for m in data.get("metrics") or [] if isinstance(m, dict) and m.get("id")}


def metric_id(test: Mapping[str, Any]) -> str | None:
    definition = test.get("metric_def") if isinstance(test.get("metric_def"), Mapping) else {}
    value = test.get("metric") or definition.get("id")
    return str(value) if value else None


def definition_of(test: Mapping[str, Any], registry: Mapping[str, Mapping[str, Any]]) -> dict[str, Any] | None:
    """The metric's definition: the test's metric_def (it takes precedence), else the registry entry."""
    definition = test.get("metric_def")
    if isinstance(definition, Mapping):
        return dict(definition)
    mid = metric_id(test)
    return dict(registry[mid]) if mid and mid in registry else None


def data_of(test: Mapping[str, Any], definition: Mapping[str, Any] | None) -> str:
    return str(test.get("data") or (definition or {}).get("data") or "")


DEFINITION_KEYS = ("description", "unit", "frequency", "where", "formula", "components")
NAMING = ("A reading is named by the id of its definition below; a component reading by <id>.<component>. Periods "
          "are written FY<year>Q<quarter> (FY<year> for a fiscal year, FY<year>H<half> for a half) in the company's "
          "fiscal year.")
_READING_NAME_RE = re.compile(r"^(?P<metric>[a-z][a-z0-9_]*)(?:\[(?P<q1>[^\]]+)\])?(?:\.(?P<component>[a-z][a-z0-9_]*))?"
                              r"(?:\[(?P<q2>[^\]]+)\])?$")


@dataclasses.dataclass
class TextMetrics:
    """What 16B reads for one period, and how its readings map back to thesis-ci readings."""

    definitions: list[dict[str, Any]]  # the metric_definitions document's entries (no thresholds, claims or notes)
    skipped: list[dict[str, Any]]  # [{metric, reason}] of text metrics not read this period
    tests_by_metric: dict[str, list[str]]
    names: dict[str, dict[str, Any]]  # reading id -> {metric, segment?, series?, unit?, components: {name: unit}}


def text_metrics(thesis: Mapping[str, Any], period: str, day: dt.date,
                 registry: Mapping[str, Mapping[str, Any]]) -> TextMetrics:
    """The metrics the due quantitative tests read from filing text (data filing_text or mixed), for 16B.

    XBRL metrics come from companyfacts. Each definition is given once, without thresholds, claims or notes (00
    section G6: the extractor sees no thesis). A test's params.segment names the segment and params.holders the
    parallel series (one definition per holder): the definition's id then carries it in brackets,
    segment_revenue_yoy[Transaction services], and readings_from_metric_values() turns it back into the reading's
    segment or series (thesis-ci readings)."""
    due, _ = due_tests(thesis, "quantitative", period, day)
    due_ids = {id(t) for t in due}
    wanted: dict[str, dict[str, Any]] = {}
    names: dict[str, dict[str, Any]] = {}
    tests_by_metric: dict[str, list[str]] = {}
    skipped: list[dict[str, Any]] = []
    for test in tests_of(thesis, "quantitative"):
        mid = metric_id(test)
        definition = definition_of(test, registry)
        if data_of(test, definition) not in TEXT_DATA or mid is None:
            continue
        if id(test) not in due_ids:
            reason = ("not active this period" if not is_active(test, period)
                      else f"first readable {test.get('first_readable')}")
            skipped.append({"metric": mid, "reason": reason})
            continue
        if definition is None:
            skipped.append({"metric": mid, "reason": "no definition in spec/metrics.yml or in the test"})
            continue
        base = {k: definition[k] for k in DEFINITION_KEYS if definition.get(k) is not None}
        if isinstance(base.get("components"), Mapping):
            base["components"] = {name: {k: v for k, v in (spec or {}).items() if k != "xbrl"}
                                  for name, spec in base["components"].items()}
        params = dict(test["params"]) if isinstance(test.get("params"), Mapping) else {}
        segment = params.get("segment") if isinstance(params.get("segment"), str) else None
        holders = [h for h in params.get("holders") or [] if isinstance(h, str)] if isinstance(
            params.get("holders"), list) else []
        variants = ([(f"{mid}[{h}]", {"series": h}) for h in holders] if holders
                    else [(f"{mid}[{segment}]", {"segment": segment})] if segment else [(mid, {})])
        for name, qualifier in variants:
            entry = {"id": name, **base}
            if params:
                entry["params"] = {k: v for k, v in params.items() if k != "holders"} | (
                    {"holder": qualifier["series"]} if "series" in qualifier else {})
            if name not in wanted:
                wanted[name] = entry
                names[name] = {"metric": mid, **qualifier, "unit": base.get("unit"),
                               "components": {c: (spec or {}).get("unit")
                                              for c, spec in (base.get("components") or {}).items()}}
            elif _canonical(wanted[name]) != _canonical(entry):
                wanted[name].setdefault("note", "more than one test uses this id with different definitions; the "
                                                "first is read")
        tests_by_metric.setdefault(mid, []).append(str(test.get("id")))
    read = {n["metric"] for n in names.values()}
    return TextMetrics(list(wanted.values()), _dedupe(s for s in skipped if s["metric"] not in read), tests_by_metric,
                       names)


def text_metric_definitions(thesis: Mapping[str, Any], period: str, day: dt.date,
                            registry: Mapping[str, Mapping[str, Any]]) -> tuple[list[dict[str, Any]],
                                                                                list[dict[str, Any]],
                                                                                dict[str, list[str]]]:
    """(definitions, skipped, tests by metric) of text_metrics()."""
    found = text_metrics(thesis, period, day, registry)
    return found.definitions, found.skipped, found.tests_by_metric


def inplace_definitions(thesis: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The metric_def of every test that gives it an id, keyed by that id (thesis-ci computes XBRL ones)."""
    out: dict[str, dict[str, Any]] = {}
    for test in tests_of(thesis, "quantitative"):
        definition = test.get("metric_def")
        if isinstance(definition, Mapping) and isinstance(definition.get("id"), str):
            out.setdefault(definition["id"], dict(definition))
    return out


def xbrl_metric_ids(thesis: Mapping[str, Any], registry: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """The metric ids (and metric.component ids) whose readings come from companyfacts."""
    ids: list[str] = []
    for test in tests_of(thesis, "quantitative"):
        mid = metric_id(test)
        definition = definition_of(test, registry) or {}
        if mid is None:
            continue
        if data_of(test, definition) == "xbrl":
            ids.append(mid)
        components = definition.get("components")
        if isinstance(components, Mapping):
            ids += [f"{mid}.{name}" for name, spec in components.items() if isinstance(spec, Mapping) and spec.get("xbrl")]
    return list(dict.fromkeys(ids))


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _dedupe(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    seen, out = set(), []
    for row in rows:
        key = _canonical(row)
        if key not in seen:
            seen.add(key)
            out.append(dict(row))
    return out


def reading_periods(period: str, quarters: int = 8, years: int = 4) -> list[str]:
    """The periods whose readings an evaluation may need: this quarter and the ones before it, and the fiscal years
    that ended by then (consecutive rules look back; year-on-year formulas are computed inside thesis-ci)."""
    fiscal_year, quarter = documents.edgar.parse_period(period)
    labels = [documents.shift_period(period, -k) for k in range(quarters)]
    last_full_year = fiscal_year if quarter == 4 else fiscal_year - 1
    labels += [f"FY{last_full_year - k}" for k in range(years)]
    return labels


# ---------------------------------------------------------------------------------------------------- readings


def parse_number(value: Any) -> float | None:
    """A reading's value as a number: 12.5, "12.5%", "(3.0)", "1,234"; None for not_found and other text."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not _NUMBER_RE.match(text):
        return None
    negative = text.startswith("(") and text.endswith(")") or text.lstrip("($ ").startswith(("-", "−"))
    digits = re.sub(r"[^\d.]", "", text)
    try:
        number = float(digits)
    except ValueError:
        return None
    return -number if negative else number


def _entries(data: Any) -> list[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, Mapping):
        for key in ("metric_values", "values", "readings", "metrics"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


_SRC_RE = re.compile(r"\[src:([A-Z0-9][A-Za-z0-9._-]*(?:#[^\]\s]+)?)\]")
_TAG_RE = re.compile(r"(?<![\w.-])([A-Z][A-Z0-9.]*-[A-Z0-9][A-Za-z0-9._-]*(?:#[^\]\s,;]+)?)")  # APP-10Q-..., FRED-X
_PERIOD_RE = re.compile(r"^FY\s*(\d{4})\s*(?:(Q[1-4]|H[12]))?$", re.I)


def source_tag(value: Any) -> str | None:
    """The first source tag in an extractor's source field ("[src:AXP-8K-2026-10-16#EX-99.2]", "AXP-10Q-FY2026Q3,
    Item 2"); None when there is none."""
    for item in value if isinstance(value, list) else [value]:
        match = _SRC_RE.search(str(item or "")) or _TAG_RE.search(str(item or ""))
        if match:
            return match.group(1)
    return None


def fiscal_period(value: Any) -> str | None:
    match = _PERIOD_RE.match(str(value or "").strip())
    return f"FY{match.group(1)}{(match.group(2) or '').upper()}" if match else None


def readings_from_metric_values(data: Any, *, run: str, names: Mapping[str, Mapping[str, Any]] | None = None
                                ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """16B's metric_values -> (thesis-ci readings, [{metric, period, reason}] not usable).

    A value that is not a number (not_found, text), a period that is not a fiscal period, or a source without a tag
    is not a reading (thesis-ci: a reading that could not be found is left out, never written as null); it is listed
    with the reason, so the evaluation's reason can say why a test is undetermined. `names` (TextMetrics.names) maps
    a bracketed id back to the metric and its segment or series, and supplies the definition's unit when the
    extractor gave none."""
    names = names or {}
    readings, unusable = [], []
    for entry in _entries(data):
        if not isinstance(entry, Mapping) or not entry.get("metric"):
            continue
        name = str(entry["metric"]).strip()
        match = _READING_NAME_RE.match(name)
        number = parse_number(entry.get("value"))
        period = fiscal_period(entry.get("period"))
        tag = source_tag(entry.get("source"))
        reason = None
        if match is None:
            reason = "not a metric id"
        elif number is None:
            reason = str(entry.get("note") or entry.get("value") or "no value")
        elif period is None:
            reason = f"period {entry.get('period')!r} is not a fiscal period"
        elif tag is None:
            reason = "no source tag"
        if reason is not None:
            unusable.append({"metric": name, "period": period or entry.get("period"), "reason": reason[:300]})
            continue
        qualifier = match.group("q1") or match.group("q2")
        base = f"{match.group('metric')}[{qualifier}]" if qualifier else match.group("metric")
        known = names.get(base) or {}
        metric = str(known.get("metric") or match.group("metric"))
        component = match.group("component")
        unit = str(entry.get("unit") or "").strip() or str(
            (known.get("components") or {}).get(component) if component else known.get("unit") or "").strip()
        reading: dict[str, Any] = {"metric": f"{metric}.{component}" if component else metric, "period": period,
                                   "value": number, "unit": unit or "number", "source": tag,
                                   "basis": "; ".join(str(x) for x in (entry.get("basis"), f"filing text, 16B ({run})")
                                                      if x)}
        if known.get("segment") or known.get("series"):
            reading.update({key: str(known[key]) for key in ("segment", "series") if known.get(key)})
        elif qualifier:
            reading["segment"] = qualifier
        notes = [f"currency {entry['currency']}"] if entry.get("currency") else []
        notes += [str(entry["note"])] if entry.get("note") else []
        if entry.get("confidence"):
            notes.append(f"extractor confidence {entry['confidence']}")
        if notes:
            reading["note"] = "; ".join(notes)
        readings.append(reading)
    return readings, unusable


def latest_readings(groups: Sequence[Sequence[Mapping[str, Any]]]) -> list[dict[str, Any]]:
    """Readings merged group by group, later groups replacing earlier readings of the same metric and period."""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for group in groups:
        for reading in group:
            merged[(str(reading.get("metric")), str(reading.get("period")))] = dict(reading)
    return list(merged.values())


# ---------------------------------------------------------------------------------------------------- evaluators


@dataclasses.dataclass(frozen=True)
class Evaluator:
    """thesis-ci's evaluation interface (or the dry-run placeholder)."""

    name: str  # thesis-ci <version> | placeholder
    readings_from_companyfacts: Any
    evaluate_company: Any
    placeholder: bool = False

    def xbrl_readings(self, companyfacts: Mapping[str, Any], metric_ids: Sequence[str], periods: Sequence[str],
                      fiscal_year_end: str, *, ticker: str, thesis: Mapping[str, Any]) -> list[dict[str, Any]]:
        """readings_from_companyfacts() with the interface's four arguments; the source tag's ticker and the tests'
        in-place metric_def are passed as well when thesis-ci accepts them (ticker=, definitions=)."""
        extra: dict[str, Any] = {}
        try:
            accepted = inspect.signature(self.readings_from_companyfacts).parameters
        except (TypeError, ValueError):
            accepted = {}
        if "ticker" in accepted:
            extra["ticker"] = ticker
        if "definitions" in accepted:
            extra["definitions"] = inplace_definitions(thesis)
        return [dict(r) for r in self.readings_from_companyfacts(companyfacts, list(metric_ids), list(periods),
                                                                 fiscal_year_end, **extra) or []]


def load_evaluator(*, allow_placeholder: bool = False) -> Evaluator:
    """thesis-ci's evaluator; with allow_placeholder (dry runs only), the placeholder when thesis-ci lacks it."""
    try:
        metrics = importlib.import_module("thesis_ci.metrics")
        evaluate = importlib.import_module("thesis_ci.evaluate")
        read = getattr(metrics, "readings_from_companyfacts")
        judge = getattr(evaluate, "evaluate_company")
    except (ImportError, AttributeError) as exc:
        if allow_placeholder:
            return placeholder_evaluator(f"{type(exc).__name__}: the installed thesis-ci has no evaluation interface")
        raise EvaluatorUnavailable(
            "the installed thesis-ci has no thesis_ci.metrics.readings_from_companyfacts / "
            "thesis_ci.evaluate.evaluate_company (thesis-ci v0.4.0 or later); install the release requirements.txt "
            "pins") from None
    try:
        import thesis_ci

        version = str(getattr(thesis_ci, "__version__", "?"))
    except ImportError:  # pragma: no cover - the submodules imported above
        version = "?"
    return Evaluator(f"thesis-ci {version}", read, judge)


PLACEHOLDER_REASON = "DRY-RUN placeholder evaluation: no reading was taken and no rule was applied"


def placeholder_evaluator(why: str) -> Evaluator:
    """Dry runs only: reads nothing from companyfacts and records every quantitative test as undetermined."""

    def read(companyfacts: Any, metric_ids: Sequence[str], periods: Sequence[str], fiscal_year_end: str) -> list:
        return []

    def judge(thesis: Mapping[str, Any], readings: Sequence[Mapping[str, Any]], period: str, today: dt.date) -> dict:
        return {
            "company": thesis.get("company"),
            "period": period,
            "as_of": today.isoformat(),
            "evaluator": "placeholder",
            "dry_run": True,
            "tests": [{"id": t.get("id"), "result": "undetermined", "reading": None, "readings": [],
                       "threshold": (t.get("rule") or {}).get("threshold"), "op": (t.get("rule") or {}).get("op"),
                       "fail_if": t.get("fail_if"), "warn_if": t.get("warn_if"), "consecutive_count": 0,
                       "reason": f"{PLACEHOLDER_REASON} ({why})"}
                      for t in tests_of(thesis, "quantitative")],
        }

    return Evaluator("placeholder", read, judge, placeholder=True)


def result_rows(document: Any) -> list[Mapping[str, Any]]:
    """The per-test rows of a ci_results document (under tests or results)."""
    if isinstance(document, Mapping):
        for key in ("tests", "results"):
            if isinstance(document.get(key), list):
                return [r for r in document[key] if isinstance(r, Mapping)]
    if isinstance(document, list):
        return [r for r in document if isinstance(r, Mapping)]
    return []


def result_counts(document: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in result_rows(document):
        key = str(row.get("result") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return counts
