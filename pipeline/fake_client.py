"""Fake model client for dry runs of the pipeline runner (docs/decisions/0019, 0024).

It stands in for the SDK client that pipeline/llm.py would otherwise create: it answers every request with
schema-valid placeholders for the part's declared outputs and reports a rough token count, so the request
assembly, the role checks, the output validation, the cost accounting and the budget guard in llm.py all run as
they would with a key. It opens no network connection and imports no model SDK. Every placeholder says DRY-RUN,
the run record says client: fake, and the runner refuses to place dry-run outputs.

Outputs that a later step reads keep the shape that step expects, so a dry run of the post-earnings chain goes end
to end: 16B answers each metric it was asked for with not_found, 14T rules every due test undetermined, 16A gives one
untagged fact, 04A one no-change finding, and 03's thesis and ledger hand back the current files unchanged (they are
valid by construction). The placeholders are built from the request's own inputs (context["inputs"]), never from
anything else.
"""

from __future__ import annotations

import calendar
import datetime as dt
import math
import re
from collections.abc import Mapping, Sequence
from types import SimpleNamespace
from typing import Any

import yaml

from . import outputs as _outputs

DRY_RUN_NOTE = "DRY-RUN placeholder written by the fake client; not model output."
REQUEST_ID = "dry-run"
# F1 `doc` values of Markdown outputs (00 section F1); other outputs use their own name.
MARKDOWN_DOCS = {
    "dossier": "dossier",
    "update": "update",
    "letter": "letter",
    "private_appendix": "private_appendix",
    "valuation_md": "valuation_md",
    "report": "report_02",
    "revised_report": "report_02",
}


def estimate_tokens(text: str) -> int:
    """A rough token count: ASCII characters / 4 plus one token per other character (CJK text is ~1 per char)."""
    ascii_chars = len(text.encode("ascii", "ignore"))
    return math.ceil(ascii_chars / 4 + (len(text) - ascii_chars))


def request_text(params: Mapping[str, Any]) -> str:
    """All text of a Messages request: system blocks and user content (image blocks are not counted)."""
    parts: list[str] = []
    for block in params.get("system") or []:
        parts.append(str(block.get("text", "")) if isinstance(block, Mapping) else str(block))
    for message in params.get("messages") or []:
        content = message.get("content") if isinstance(message, Mapping) else None
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.extend(str(b.get("text", "")) for b in content if isinstance(b, Mapping) and b.get("type") == "text")
    return "\n".join(parts)


class FakeClient:
    """Duck-types the part of the SDK client that llm.complete() uses: messages / beta.messages, stream / create."""

    kind = "fake"

    def __init__(self, reply: str):
        self.reply = reply
        self.requests: list[dict[str, Any]] = []  # model and max_tokens only; no content is kept
        self.messages = _FakeMessages(self)
        self.beta = SimpleNamespace(messages=_FakeMessages(self))

    def respond(self, params: Mapping[str, Any]) -> SimpleNamespace:
        self.requests.append({"model": params.get("model"), "max_tokens": params.get("max_tokens")})
        usage = SimpleNamespace(
            input_tokens=estimate_tokens(request_text(params)),
            output_tokens=estimate_tokens(self.reply),
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
            iterations=None,
        )
        return SimpleNamespace(
            model=params.get("model"),
            stop_reason="end_turn",
            stop_details=None,
            content=[SimpleNamespace(type="text", text=self.reply)],
            usage=usage,
            _request_id=REQUEST_ID,
        )


class _FakeMessages:
    def __init__(self, client: FakeClient):
        self._client = client

    def create(self, **params: Any) -> SimpleNamespace:
        return self._client.respond(params)

    def stream(self, **params: Any) -> _FakeStream:
        return _FakeStream(self._client.respond(params))


class _FakeStream:
    def __init__(self, response: SimpleNamespace):
        self._response = response

    def __enter__(self) -> _FakeStream:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def get_final_message(self) -> SimpleNamespace:
        return self._response


# ---------------------------------------------------------------------------------------------------- placeholders


def _dump(data: Any) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False).rstrip("\n")


def _month_end(day: dt.date) -> dt.date:
    return dt.date(day.year, day.month, calendar.monthrange(day.year, day.month)[1])


def _add_months(day: dt.date, months: int) -> dt.date:
    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    return dt.date(year, month + 1, min(day.day, calendar.monthrange(year, month + 1)[1]))


def _prereg(context: Mapping[str, Any]) -> str:
    header = dict((context.get("pipeline_fields") or {}).get("prereg") or {})
    company = str(context.get("company") or "TEST")
    period = str(context.get("period") or "FY2000Q1")
    run_date = dt.date.fromisoformat(str(context["run_date"]))
    event = header.get("event") or {}
    try:
        release = dt.date.fromisoformat(str(event.get("expected_release")))
    except ValueError:
        release = run_date + dt.timedelta(days=30)
    common = {"criterion": DRY_RUN_NOTE, "data_source": DRY_RUN_NOTE, "domain": context.get("domain") or "other",
              "added_by": "system"}
    items = [
        {"id": f"{company}-{period}-1", "statement": DRY_RUN_NOTE, "probability": 0.5, **common,
         "horizon": "quarter", "resolves_by": _month_end(release).isoformat(), "pillar": "P1",
         "falsifies_thesis": True, "reference": DRY_RUN_NOTE},
        {"id": f"{company}-{period}-2", "statement": DRY_RUN_NOTE, "probability": 0.5, **common,
         "horizon": "18m", "resolves_by": _month_end(_add_months(run_date, 18)).isoformat(), "pillar": "P1",
         "falsifies_thesis": False, "reference": DRY_RUN_NOTE},
    ]
    return _dump({**header, "horizon": "mixed", "items": items})


def _question_list(context: Mapping[str, Any]) -> str:
    return _dump([
        {"id": "Q01", "question": DRY_RUN_NOTE, "kind": "pillar", "maps_to": ["P1"]},
        {"id": "Q02", "question": DRY_RUN_NOTE, "kind": "open", "maps_to": []},
    ])


def _questions(context: Mapping[str, Any]) -> str:
    return _dump([{"id": f"{context.get('label') or 'RUN'}-DRY-1", "issue": DRY_RUN_NOTE, "options": ["none"],
                   "interim": "none", "blocking": False}])


def _markdown(name: str, context: Mapping[str, Any]) -> str:
    front: dict[str, Any] = {}
    if name not in _outputs.F1_WITHOUT_COMPANY and context.get("company"):
        front["company"] = context["company"]
    front.update({"doc": MARKDOWN_DOCS.get(name, name), "as_of": str(context["run_date"]), "doc_status": "draft",
                  "dry_run": True})
    return f"---\n{_dump(front)}\n---\n\n{DRY_RUN_NOTE}"


def _inputs(context: Mapping[str, Any]) -> Mapping[str, str]:
    return context.get("inputs") or {}


def _yaml_input(context: Mapping[str, Any], name: str) -> Any:
    text = _inputs(context).get(name)
    try:
        return yaml.safe_load(text) if text else None
    except yaml.YAMLError:
        return None


_OUTPUT_BLOCK_RE = re.compile(r"^===== output: (\w+) \([^)]*\) =====\n(.*?)(?=^===== |\Z)", re.S | re.M)


def carried_over(context: Mapping[str, Any], name: str) -> str | None:
    """The current version of a file the part hands back complete (thesis, ledger): its input, or its block in
    draft_outputs (03R)."""
    inputs = _inputs(context)
    if inputs.get(name):
        return inputs[name]
    for match in _OUTPUT_BLOCK_RE.finditer(inputs.get("draft_outputs") or ""):
        if match.group(1) == name:
            return match.group(2).rstrip() + "\n"
    return None


def _carry(name: str) -> Any:
    def build(context: Mapping[str, Any]) -> str:
        text = carried_over(context, name)
        if text is None:
            return _dump({"dry_run": True, "output": name, "note": DRY_RUN_NOTE})
        data = yaml.safe_load(text)
        if isinstance(data, dict):
            data.pop("generated_by", None)
        return _dump(data)

    return build


def _metric_values(context: Mapping[str, Any]) -> str:
    wanted = _yaml_input(context, "metric_definitions") or {}
    metrics = [m.get("id") for m in wanted.get("metrics") or [] if isinstance(m, dict)] or ["dry_run_metric"]
    return _dump([{"metric": metric, "value": "not_found", "unit": None, "currency": None,
                   "period": context.get("period"), "basis": DRY_RUN_NOTE, "source": None, "excerpt": DRY_RUN_NOTE,
                   "confidence": "low", "note": DRY_RUN_NOTE} for metric in metrics])


def _qualitative_verdicts(context: Mapping[str, Any]) -> str:
    tests = (_yaml_input(context, "qualitative_tests") or {}).get("tests") or [{"id": "DRY-RUN"}]
    return _dump([{"test_id": t.get("id"), "restated_question": DRY_RUN_NOTE, "answer": "cannot determine",
                   "verdict": "undetermined", "conditions": [], "excerpt": DRY_RUN_NOTE, "source": [],
                   "missing": DRY_RUN_NOTE} for t in tests if isinstance(t, dict)])


def _fact_table(context: Mapping[str, Any]) -> str:
    return _dump({"as_of": str(context["run_date"]), "facts": [
        {"id": "F001", "location": "update", "subject": DRY_RUN_NOTE, "what": DRY_RUN_NOTE, "value": DRY_RUN_NOTE,
         "unit": None, "period": None, "source": None, "untagged": True, "excerpt": DRY_RUN_NOTE}]})


def _fact_verdicts(context: Mapping[str, Any]) -> str:
    facts = (_yaml_input(context, "fact_table") or {}).get("facts") or [{"id": "F001"}]
    return _dump([{"id": f.get("id"), "verdict": "unconfirmed", "source_location": None, "correct_value": None}
                  for f in facts if isinstance(f, dict)])


def _findings(context: Mapping[str, Any]) -> str:
    label = context.get("label") or "RUN"
    return _dump([{"id": f"{label}-01", "group": "no change", "type": None, "location": DRY_RUN_NOTE,
                   "quote": DRY_RUN_NOTE, "evidence": DRY_RUN_NOTE, "fix": None}])


def _question_ids(context: Mapping[str, Any], name: str) -> list[str]:
    data = _yaml_input(context, name)
    rows = data.get("questions") if isinstance(data, dict) else data
    return [str(q.get("id")) for q in rows or [] if isinstance(q, dict) and q.get("id")]


def _answers(source: str) -> Any:
    def build(context: Mapping[str, Any]) -> str:
        carried = carried_over(context, "question_answers") if source == "question_list" else None
        if carried is not None:  # a revision hands the draft's answers back unchanged
            return _carry("question_answers")(context)
        ids = _question_ids(context, source)
        if not ids:
            return _outputs.EMPTY_MARK  # a candidate has no question list, so no answers (prompt 03)
        return _dump([{"id": qid, "answer": DRY_RUN_NOTE, "excerpt": None, "source": None,
                       "confidence": "plausible but not the base case"} for qid in ids])

    return build


def _divergence_map(context: Mapping[str, Any]) -> str:
    ids = _question_ids(context, "question_list") or ["Q01"]
    return _dump([{"id": qid, "mark": "cannot tell", "company_manager": DRY_RUN_NOTE, "blind_read": DRY_RUN_NOTE,
                   "touches_pillar": False, "more_direct": None, "resolution": DRY_RUN_NOTE,
                   "to_owner_letter": False} for qid in ids])


def _settlement(input_name: str, key: str) -> Any:
    def build(context: Mapping[str, Any]) -> str:
        data = _yaml_input(context, input_name)
        rows = (data.get("items") or data.get("entries")) if isinstance(data, dict) else data
        ids = [str(r.get("id")) for r in rows or [] if isinstance(r, dict) and r.get("id")]
        if not ids:
            return _outputs.EMPTY_MARK
        return _dump([{"id": rid, key: "undetermined", "values": None, "evidence": DRY_RUN_NOTE,
                       "reasoning": DRY_RUN_NOTE} for rid in ids])

    return build


def _gate_decision(context: Mapping[str, Any]) -> str:
    return _dump({"decision": "hold", "reasons": [DRY_RUN_NOTE]})


SPECIAL = {"prereg": _prereg, "question_list": _question_list, "questions": _questions, "thesis": _carry("thesis"),
           "ledger": _carry("ledger"), "metric_values": _metric_values, "qualitative_verdicts": _qualitative_verdicts,
           "fact_table": _fact_table, "fact_verdicts": _fact_verdicts, "findings": _findings,
           "gate_decision": _gate_decision, "sources_additions": lambda context: "[]",
           "question_answers": _answers("question_list"), "blind_answers": _answers("question_list_stripped"),
           "divergence_map": _divergence_map, "prereg_settlement": _settlement("items_blind", "outcome"),
           "ledger_settlement": _settlement("ledger_due", "status"),
           "reviewed_sections": lambda context: "[]", "dossier_changes": lambda context: "[]",
           "patch_decisions": lambda context: "[]", "revision_notes": lambda context: "[]",
           "returns": lambda context: "[]"}


def placeholder_output(name: str, fmt: str, context: Mapping[str, Any]) -> str:
    if name in SPECIAL:
        return SPECIAL[name](context)
    if fmt == _outputs.YAML:
        return _dump({"dry_run": True, "output": name, "note": DRY_RUN_NOTE})
    if fmt == _outputs.MARKDOWN:
        return _markdown(name, context)
    return f"{DRY_RUN_NOTE} ({name})"


def placeholder_reply(declared: Sequence[tuple[str, bool]], formats: Mapping[str, str],
                      context: Mapping[str, Any]) -> str:
    """A reply with one <output> block per required output; optional outputs are left out, as a model may do."""
    blocks = [f'<output name="{name}">\n{placeholder_output(name, formats[name], context)}\n</output>'
              for name, required in declared if required and formats.get(name) != _outputs.FILE]
    return "\n\n".join(blocks)
