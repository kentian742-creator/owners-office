"""Synthetic material for the pipeline/llm.py tests: prompts, role definitions, schemas, page images and a test-double
client.

The prompts only imitate the structure of the v3 front matter (output_formats in 00, parts, inputs_passN /
outputs_passN, calls, each mode's own inputs / outputs, role: pipeline, design on a part); their bodies are a few
placeholder sentences with none of the private repository's prompt content. Everything is written into pytest's
temporary directory; no data files are left in the repository.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

RULES = """---
id: "00"
title: synthetic series rules
version: "9.0"
output_formats:
  yaml: [thesis, questions, escalation, revision_notes, valuation_yml, fact_verdicts, findings, inversion_list, test_proposals, design_checks, layout_instructions, sources_additions, model_checks]
  markdown: [update, story, report, valuation_md, letter, dossier, private_appendix]
  text: [bear_case, weakest_sentence, reply]
  file: [pdf, rendered_pages]
---

# Synthetic series rules

A synthetic rule that mentions the placeholder {{date}}; the rules file goes into the system prompt unfilled.
"""

DESIGN = """---
id: "00D"
title: synthetic design system
version: "9.1"
---

# Synthetic design system
"""

PROMPTS = {
    "02-report.md": """---
id: "02"
version: "3.2"
role: company_manager
design: 00D §D1
modes:
  report:
    trigger: synthetic
    inputs: [run_date, dossier]
    outputs: [report, questions]
  valuation_refresh:
    trigger: synthetic
    inputs: [run_date, dossier, valuation_input_notes]
    outputs: [valuation_md, valuation_yml, questions]
inputs: [run_date, dossier, "valuation_input_notes?"]
outputs: [report, "valuation_md?", "valuation_yml?", questions]
---

Synthetic research report prompt for {{company}} ({{ticker}}).
""",
    "03-update.md": """---
id: "03"
version: "3.1"
role: company_manager
parts:
  draft: {inputs: [run_date, filings, thesis, "owner_notes?"], outputs: [update, thesis, questions, "escalation?", "story?"]}
  revise: {name: 03R synthetic revision, inputs: [draft_outputs, findings_04A], outputs: [update, thesis, questions, "escalation?", "story?", revision_notes]}
---

Synthetic quarterly update prompt for {{company}} ({{ticker}}), {{period}}.
""",
    "04-audit.md": """---
id: "04"
version: "3.0"
parts:
  A: {name: synthetic fact audit, role: auditor, inputs: [fact_table, sources], outputs: [fact_verdicts, findings, questions]}
  B: {name: synthetic counter case, role: red_team, calls: 2, inputs_pass1: [product_without_counter, "dossier_without_9_12?"], inputs_pass2: [pass1, product_counter_section], outputs_pass1: [bear_case, findings, questions], outputs_pass2: [findings, weakest_sentence, questions]}
  B_lite: {name: synthetic inversion, role: red_team, inputs: [update, filings], outputs: [inversion_list, test_proposals]}
---

Synthetic audit prompt; subject: {{subject}}.
""",
    "09-layout.md": """---
id: "09"
version: "3.0"
parts:
  C: {name: synthetic layout review, role: design_reviewer, design: 00D §D2, inputs: [document, rendered_pages], outputs: [design_checks, layout_instructions, findings, questions]}
---

Synthetic layout review of document {{document}}.
""",
    "12-audit.md": """---
id: "12"
version: "3.0"
parts:
  V: {name: synthetic consistency check, role: pipeline, inputs: [report], outputs: [findings]}
  A: {name: synthetic fact audit, role: auditor, inputs: [fact_table, sources], outputs: [fact_verdicts, findings, questions]}
  C: {name: synthetic layout review, role: design_reviewer, design: 00D §D1, inputs: [report, rendered_pages], outputs: [design_checks, questions]}
---

Synthetic report audit prompt.
""",
    "19-typeset.md": """---
id: "19"
version: "3.0"
role: typesetter
design: 00D
inputs: [content]
outputs: [pdf, rendered_pages, questions]
---

Synthetic typesetting prompt.
""",
}

# Same shape as the public repository's agents/*.yml; registers only the prompts and inputs the tests use.
AGENTS: dict[str, dict[str, Any]] = {
    "company_manager": {
        "model": "claude-sonnet-5",
        "prompts": ["02", "03"],
        "can_see": ["run_date", "dossier", "valuation_input_notes", "filings", "thesis", "owner_notes",
                    "draft_outputs", "findings_04A"],
        "cannot_see": ["blind_answers"],
    },
    "auditor": {
        "model": "claude-fable-5-1",
        "fallbacks": "default",
        "prompts": ["04A", "12A"],
        "can_see": ["fact_table", "sources"],
        "cannot_see": ["product", "dossier", "thesis", "update", "conclusions"],
    },
    "red_team": {
        "model": "claude-fable-5-1",
        "fallbacks": "default",
        "prompts": ["04B", "04B-lite"],
        "can_see": ["product_without_counter", "dossier_without_9_12", "pass1", "product_counter_section", "update",
                    "filings"],
        "cannot_see": ["product", "dossier", "thesis", "findings_04A"],
    },
    "design_reviewer": {
        "model": "claude-fable-5-1",
        "fallbacks": "default",
        "prompts": ["09C"],
        "can_see": ["document", "rendered_pages"],
        "cannot_see": [],
    },
    "typesetter": {
        "model": "claude-sonnet-5",
        "prompts": ["19"],
        "can_see": ["content"],
        "cannot_see": [],
    },
}

# Synthetic thesis-ci schemas: only the constraints the tests need (top-level additionalProperties: false, as in the
# real schemas).
SCHEMAS: dict[str, dict[str, Any]] = {
    "thesis": {
        "type": "object",
        "required": ["company", "trust_level", "tests"],
        "properties": {"company": {"type": "string"}, "trust_level": {"type": "integer"}, "tests": {"type": "array"}},
        "additionalProperties": False,
    },
    "story": {
        "type": "object",
        "required": ["company", "as_of", "status"],
        "properties": {
            "company": {"type": "string"},
            "as_of": {"type": "string", "format": "date"},
            "status": {"enum": ["holding", "candidate", "archive"]},
        },
        "additionalProperties": False,
    },
    "escalation": {
        "type": "object",
        "required": ["id", "reason"],
        "properties": {"id": {"type": "string"}, "reason": {"enum": ["moat_permanent_impairment"]}},
        "additionalProperties": False,
    },
    "valuation": {
        "type": "object",
        "required": ["company", "doc_status"],
        "properties": {"company": {"type": "string"}, "doc_status": {"enum": ["proposed", "effective"]},
                       "method_note": {"type": "string"}},
        "additionalProperties": False,
    },
    "sources": {
        "type": "object",
        "required": ["sources"],
        "properties": {
            "sources": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["tag", "kind", "title"],
                    "properties": {"tag": {"type": "string"}, "kind": {"type": "string"}, "title": {"type": "string"}},
                    "additionalProperties": False,
                },
            }
        },
        "additionalProperties": False,
    },
}

UPDATE_MD = "---\ncompany: TEST\ndoc: update\nas_of: 2026-09-24\ndoc_status: draft\n---\nConclusion: maintain.\n"
THESIS_YML = "company: TEST\ntrust_level: 1\ntests: []\n"
DRAFT_VARIABLES = {"company": "Test Co", "ticker": "TEST", "period": "FY2026Q3"}


def envelope(**outputs: str) -> str:
    """A model reply: one <output name="..."> block per output."""
    return "\n\n".join(f'<output name="{name}">\n{text}\n</output>' for name, text in outputs.items())


DRAFT_REPLY = envelope(update=UPDATE_MD, thesis=THESIS_YML, questions="none")


def write_agent(repo: Path, role: str, *, model: str | None = None, **overrides: Any) -> Path:
    """Write agents/<role>.yml; keys whose value is None are left out (to build incomplete role definitions)."""
    spec = {**AGENTS[role], **overrides}
    if model is not None:
        spec["model"] = model
    model_block = {"id": spec.pop("model")}
    for key in ("effort", "fallbacks"):
        if spec.get(key) is not None:
            model_block[key] = spec.pop(key)
        spec.pop(key, None)
    data = {"role": role, "name": role, "model": model_block}
    data.update({k: v for k, v in spec.items() if v is not None})
    path = repo / "agents" / f"{role}.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")  # JSON is valid YAML
    return path


def make_env(tmp_path: Path) -> SimpleNamespace:
    """A temporary workspace: the public repository (agents/), the private repository's prompts directory beside it,
    a schema directory, two page images and the log path."""
    repo = tmp_path / "owners-office"
    for role in AGENTS:
        write_agent(repo, role)
    prompts = tmp_path / "owners-office-private" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "00-series-rules.md").write_text(RULES, encoding="utf-8")
    (prompts / "00D-design-system.md").write_text(DESIGN, encoding="utf-8")
    for name, text in PROMPTS.items():
        (prompts / name).write_text(text, encoding="utf-8")
    schemas = tmp_path / "schemas"
    schemas.mkdir()
    for name, schema in SCHEMAS.items():
        (schemas / f"{name}.schema.json").write_text(json.dumps(schema), encoding="utf-8")
    pages = tmp_path / "pages"
    pages.mkdir()
    page_png = pages / "page-1.png"
    page_png.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic page 1")
    page_jpg = pages / "page-2.jpg"
    page_jpg.write_bytes(b"\xff\xd8\xffsynthetic page 2")
    return SimpleNamespace(
        repo=repo, prompts=prompts, schemas=schemas, page_png=page_png, page_jpg=page_jpg,
        log=tmp_path / "logs" / "llm-calls.jsonl",
    )


# ---------------------------------------------------------------- Test-double client


class FakeStream:
    def __init__(self, response: Any):
        self.response = response

    def __enter__(self) -> FakeStream:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def get_final_message(self) -> Any:
        return self.response


class FakeMessages:
    """Returns responses in order (the last one is reused); records the arguments create and stream each receive."""

    def __init__(self, responses: tuple[Any, ...], error: Exception | None):
        self.responses = list(responses)
        self.error = error
        self.calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []

    def _next(self) -> Any:
        if self.error is not None:
            raise self.error
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._next()

    def stream(self, **kwargs: Any) -> FakeStream:
        self.stream_calls.append(kwargs)
        return FakeStream(self._next())


class FakeClient:
    """A scripted stand-in for the SDK client (not pipeline/fake_client.py's dry-run client): it returns the given
    responses, or raises error, and records every request."""

    def __init__(self, *responses: Any, error: Exception | None = None):
        self.messages = FakeMessages(responses, error)
        self.beta = SimpleNamespace(messages=FakeMessages(responses, error))

    @property
    def requests(self) -> list[dict[str, Any]]:
        return [
            *self.messages.calls, *self.messages.stream_calls, *self.beta.messages.calls, *self.beta.messages.stream_calls,
        ]

    @property
    def call_count(self) -> int:
        return len(self.requests)


def make_response(
    text: str = DRAFT_REPLY,
    *,
    model: str = "claude-sonnet-5",
    stop_reason: str = "end_turn",
    input_tokens: int = 1000,
    output_tokens: int = 500,
    cache_creation_input_tokens: int | None = None,
    cache_read_input_tokens: int | None = None,
    iterations: Any = None,
    content: Any = None,
    stop_details: Any = None,
) -> SimpleNamespace:
    if content is None:
        content = [
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=text),
        ]
    return SimpleNamespace(
        model=model,
        stop_reason=stop_reason,
        stop_details=stop_details,
        content=content,
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
            iterations=iterations,
        ),
    )
