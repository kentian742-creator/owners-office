"""A stand-in for thesis-ci's evaluation interface in the runner's tests (docs/decisions/0024).

thesis-ci owns the evaluation (thesis_ci.metrics.readings_from_companyfacts, thesis_ci.evaluate.evaluate_company);
until its release carries them, and so that these tests never depend on thesis-ci's own logic, the tests install
this shim with the same signatures and just enough behavior: companyfacts readings come ready-made from the synthetic
document's shim_readings, and a test fails when its one reading of the period meets the rule's comparison.
"""

from __future__ import annotations

import datetime as dt
import operator
import sys
import types
from collections.abc import Mapping, Sequence
from typing import Any

COMPARISONS = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge, "==": operator.eq}
CALLS: list[dict[str, Any]] = []  # what the pipeline passed (arguments' shapes only)


def readings_from_companyfacts(companyfacts: Mapping[str, Any], metric_ids: list[str], periods: list[str],
                               fiscal_year_end: str) -> list[dict[str, Any]]:
    CALLS.append({"function": "readings_from_companyfacts", "metric_ids": list(metric_ids), "periods": list(periods),
                  "fiscal_year_end": fiscal_year_end})
    return [dict(r) for r in companyfacts.get("shim_readings") or []
            if r["metric"] in metric_ids and r["period"] in periods]


def evaluate_company(thesis: Mapping[str, Any], readings: Sequence[Mapping[str, Any]], period: str,
                     today: dt.date) -> dict[str, Any]:
    CALLS.append({"function": "evaluate_company", "readings": len(readings), "period": period, "today": today})
    results = []
    for test in thesis.get("tests") or []:
        if test.get("type") != "quantitative":
            continue
        metric = test.get("metric") or (test.get("metric_def") or {}).get("id")
        segment = (test.get("params") or {}).get("segment")
        rule = test.get("rule") or {}
        reading = next((dict(r) for r in readings if r.get("metric") == metric and r.get("period") == period
                        and r.get("segment") == segment), None)
        compare = COMPARISONS.get(rule.get("op"))
        if reading is None or compare is None:
            result, reason = "undetermined", "no reading of the period" if reading is None else "rule not judged"
        else:
            result = "fail" if compare(reading["value"], rule.get("threshold")) else "pass"
            reason = f"{metric} {reading['value']} {rule.get('op')} {rule.get('threshold')}"
        results.append({"id": test.get("id"), "result": result, "reading": reading,
                        "readings": [reading] if reading else [], "threshold": rule.get("threshold"),
                        "op": rule.get("op"), "fail_if": test.get("fail_if"), "warn_if": test.get("warn_if"),
                        "consecutive_count": 1 if result == "fail" else 0, "reason": reason})
    return {"company": thesis.get("company"), "period": period, "evaluated_on": today.isoformat(), "engine": "shim",
            "results": results}


def install(monkeypatch: Any) -> None:
    """Make importlib find the shim as thesis_ci.metrics and thesis_ci.evaluate for one test."""
    CALLS.clear()
    metrics = types.ModuleType("thesis_ci.metrics")
    metrics.readings_from_companyfacts = readings_from_companyfacts
    evaluate = types.ModuleType("thesis_ci.evaluate")
    evaluate.evaluate_company = evaluate_company
    monkeypatch.setitem(sys.modules, "thesis_ci.metrics", metrics)
    monkeypatch.setitem(sys.modules, "thesis_ci.evaluate", evaluate)


def remove(monkeypatch: Any) -> None:
    """Make the interface unavailable, as in a thesis-ci release without it."""
    monkeypatch.setitem(sys.modules, "thesis_ci.metrics", None)
    monkeypatch.setitem(sys.modules, "thesis_ci.evaluate", None)
