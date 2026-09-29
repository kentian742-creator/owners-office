"""Parsing, validation and placement of model outputs (00 §F0, §F1, §F2, §F6). This module does not call a model.

pipeline/llm.py calls parse_reply() when a reply arrives: it splits the reply into <output name="..."> blocks,
validates each one against its declared format, and returns {output name: ParsedOutput} and a list of errors; when
there are errors, llm.py retries once with that list.

- Formats are declared by the prompts (output_formats in the 00 front matter, which a part can override with
  formats) and passed in by the caller as formats: yaml, markdown, text, file (00 §F0).
- Names: must be names in the part's outputs, and each name appears only once; required outputs cannot be missing
  (with no content, write the empty marker EMPTY_MARK, "none", in any letter case).
- yaml: no code fences, must parse, no duplicate keys; outputs with a thesis-ci schema (OUTPUT_SCHEMAS) are then
  validated against the schema. The pipeline-maintained fields (00 §G8) given in pipeline_fields are written into
  the document before it is validated. YAML outputs that no schema covers but a later step reads (STRUCTURES: 04A's
  fact_verdicts and every part's findings and questions per 00 §F3 and §F8, 16A's fact_table, 16B's metric_values,
  14T's qualitative_verdicts, 15B's settlements, 03's dossier_changes, reviewed_sections and revision_notes, ...)
  are checked for their shape and their fixed words (docs/decisions/0024): a list of the right rows, the keys
  every row carries, and verdict, group and type names exactly as the rules write them.
- markdown: must start with front matter that has at least company, doc, as_of and doc_status (00 §F1; the letter
  to the owner and its private appendix have no company); for story.md the front matter is validated only against
  the story schema.
- text: no front matter, content not validated; file: cannot go into a text block, so one appearing is an error.
- generated_by: injected into the front matter of every markdown output, and into yaml mappings no schema restricts;
  where the schema allows no extra keys (thesis, ledger, story, etc.) the document is left unchanged and
  generated_by is returned alongside it in ParsedOutput.generated_by.

The placement table PLACEMENT writes 00 §F2 down as data (output name → repository and path template); place() and
place_outputs() only answer "where does this output go"; writing files and opening PRs is up to the caller. Outputs
not in the table go to the private repository, per the last row of §F2.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import functools
import os
import re
import string
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMAS_ENV = "OWNERS_OFFICE_SCHEMAS"  # overrides the schema directory (for tests); default: the installed thesis-ci
DEFAULT_SCHEMAS_DIR = REPO_ROOT.parent / "thesis-ci" / "spec" / "schemas"

EMPTY_MARK = "none"  # 00 §F0: with no content, write this mark instead of omitting the output (any letter case)
MAX_ERRORS_PER_OUTPUT = 20

MARKDOWN, YAML, TEXT, FILE = "markdown", "yaml", "text", "file"
FORMATS = (YAML, MARKDOWN, TEXT, FILE)  # the four output formats of 00 §F0

# Output name → thesis-ci schema (spec/schemas/<name>.schema.json). Only complete files are registered; for story
# the front matter is validated; sources_additions is a set of sources entries, validated as {"sources": [...]}
# after visibility is removed.
OUTPUT_SCHEMAS: dict[str, str] = {
    "thesis": "thesis",
    "ledger": "ledger",
    "prereg": "prereg",
    "valuation_yml": "valuation",
    "escalation": "escalation",
    "memo": "memo",
    "story": "story",
    "sources_additions": "sources",
}
# An output with a schema must be declared in the matching format; otherwise the schema validation would be skipped.
SCHEMA_FORMATS = {name: (MARKDOWN if name == "story" else YAML) for name in OUTPUT_SCHEMAS}

# 00 §F1: a markdown output has at least these front matter keys. The letter to the owner and its private appendix
# do not belong to any one company, so they have no company key.
FRONT_MATTER_KEYS = ("company", "doc", "as_of", "doc_status")
F1_WITHOUT_COMPANY = frozenset({"letter", "private_appendix"})
SOURCE_VISIBILITIES = ("public", "private")


class SchemaUnavailable(LookupError):
    """Validation against a thesis-ci schema is needed, but the schema cannot be found."""


@dataclasses.dataclass(frozen=True)
class ParsedOutput:
    """A validated output. text is the text handed to the caller to write (Markdown with generated_by already
    injected); an output that is just the empty marker (EMPTY_MARK) is an empty output."""

    name: str
    format: str  # the declared format: yaml / markdown / text / file
    text: str
    data: Any = None  # the YAML content; for Markdown, the front matter
    empty: bool = False
    schema: str | None = None
    generated_by: Mapping[str, Any] | None = None
    generated_by_injected: bool = False
    pipeline_fields: tuple[str, ...] = ()  # keys written by the pipeline (00 §G8)


# ---------------------------------------------------------------- YAML


def _yaml() -> Any:
    import yaml  # PyYAML; imported lazily to keep the module light

    return yaml


@functools.lru_cache(maxsize=None)
def _unique_key_loader() -> type:
    """SafeLoader, but duplicate keys are an error: thesis-ci's lint also treats duplicate keys as errors (YAML keeps
    only the last value)."""
    yaml = _yaml()

    class UniqueKeyLoader(yaml.SafeLoader):
        pass

    def construct_mapping(loader: Any, node: Any, deep: bool = False) -> Any:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                continue
            key = loader.construct_object(key_node, deep=deep)
            try:
                duplicate = key in seen
                seen.add(key)
            except TypeError:  # unhashable keys are reported by construct_mapping itself
                continue
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping", node.start_mark, f"duplicate key {key!r}", key_node.start_mark
                )
        return loader.construct_mapping(node, deep=deep)

    UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping)
    return UniqueKeyLoader


def load_yaml_text(text: str) -> Any:
    """Parse a piece of YAML; duplicate keys are an error. Raises yaml.YAMLError on errors."""
    return _yaml().load(text, Loader=_unique_key_loader())


def dump_yaml(data: Any) -> str:
    return _yaml().safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)


_NUMBER_LIKE = re.compile(r"^[-+]?[0-9][0-9_.,:/-]*$")


@functools.lru_cache(maxsize=None)
def _field_dumper() -> type:
    yaml = _yaml()

    class FieldDumper(yaml.SafeDumper):
        pass

    def represent_str(dumper: Any, value: str) -> Any:
        other_type = dumper.resolve(yaml.ScalarNode, value, (True, False)) != "tag:yaml.org,2002:str"
        style = '"' if other_type or _NUMBER_LIKE.match(value) else None
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)

    FieldDumper.add_representer(str, represent_str)
    return FieldDumper


def dump_fields(data: Any) -> str:
    """dump_yaml(), but a string that a YAML parser could read as another type ("0.2", "0000004962", "12-31") is
    double-quoted, as the repositories write it: YAML 1.2 parsers read 0000004962 as a number."""
    return _yaml().dump(data, Dumper=_field_dumper(), allow_unicode=True, sort_keys=False, default_flow_style=False)


def jsonable(obj: Any) -> Any:
    """Map YAML data onto the JSON data model before schema validation: dates become ISO strings, keys become strings
    (consistent with thesis-ci)."""
    if isinstance(obj, (dt.date, dt.datetime)):
        return obj.isoformat()
    if isinstance(obj, Mapping):
        return {_json_key(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    return obj


def _json_key(key: Any) -> str:
    if isinstance(key, str):
        return key
    if isinstance(key, bool):
        return "true" if key else "false"
    if key is None:
        return "null"
    if isinstance(key, (dt.date, dt.datetime)):
        return key.isoformat()
    return str(key)


def _yaml_problem(exc: Exception) -> str:
    mark = getattr(exc, "problem_mark", None)
    problem = getattr(exc, "problem", None) or str(exc).splitlines()[0]
    where = f"line {mark.line + 1}: " if mark is not None else ""
    return f"{where}{problem}"


def is_empty_mark(text: Any) -> bool:
    """Whether an output (or a parsed YAML value) is just the empty marker "none", in any letter case (00 §F0)."""
    return isinstance(text, str) and text.strip().casefold() == EMPTY_MARK


def split_front_matter(text: str) -> tuple[str, str] | None:
    """(front matter, body); None when the text does not start with --- or has no closing ---."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            return "".join(lines[1:i]), "".join(lines[i + 1 :])
    return None


def _fenced(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith("```") or stripped.startswith("~~~") or stripped.endswith("```")


# ---------------------------------------------------------------- schema


def schema_validator(name: str, schemas_dir: str | os.PathLike[str] | None = None) -> Any:
    """A thesis-ci JSON Schema validator.

    Lookup order: the schemas_dir argument → the environment variable OWNERS_OFFICE_SCHEMAS → the installed thesis-ci
    (thesis_ci.contract) → a sibling checkout, ../thesis-ci/spec/schemas. Raises SchemaUnavailable when none is found.
    """
    directory = schemas_dir if schemas_dir is not None else os.environ.get(SCHEMAS_ENV) or None
    if directory is None:
        try:
            from thesis_ci import contract  # the public CI installs thesis-ci from requirements.txt
        except ImportError:
            contract = None
        if contract is not None:
            try:
                return contract.validator(name)
            except (OSError, ValueError) as exc:
                raise SchemaUnavailable(f"thesis-ci has no {name}.schema.json: {exc}") from exc
        directory = DEFAULT_SCHEMAS_DIR
    path = Path(directory) / f"{name}.schema.json"
    if not path.is_file():
        raise SchemaUnavailable(f"{path} not found; install thesis-ci, or point {SCHEMAS_ENV} at the schema directory")
    return _validator_from_file(str(path.resolve()))


@functools.lru_cache(maxsize=None)
def _validator_from_file(path: str) -> Any:
    import json

    import jsonschema

    schema = json.loads(Path(path).read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())


def validators_for(outputs: Iterable[str], schemas_dir: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """The schema validators these outputs need (schema name → validator). llm.py resolves them before sending the
    request, so a schema that cannot be found costs no money."""
    names = sorted({OUTPUT_SCHEMAS[o] for o in outputs if o in OUTPUT_SCHEMAS})
    return {name: schema_validator(name, schemas_dir) for name in names}


def format_problems(formats: Mapping[str, str]) -> list[str]:
    """Problems in the format declarations themselves: unknown formats; outputs with a schema declared in another
    format."""
    problems = [f"the format {fmt!r} of {name} is not one of {', '.join(FORMATS)}" for name, fmt in formats.items()
                if fmt not in FORMATS]
    problems += [
        f"{name} has a thesis-ci schema ({OUTPUT_SCHEMAS[name]}), so its format should be {SCHEMA_FORMATS[name]}, "
        f"not {fmt}"
        for name, fmt in formats.items()
        if name in SCHEMA_FORMATS and fmt in FORMATS and fmt != SCHEMA_FORMATS[name]
    ]
    return problems


def rejects_extra_keys(validator: Any) -> bool:
    schema = getattr(validator, "schema", None)
    return isinstance(schema, Mapping) and schema.get("additionalProperties") is False


def schema_errors(validator: Any, instance: Any, schema_name: str) -> list[str]:
    from jsonschema.exceptions import best_match

    errors = sorted(validator.iter_errors(jsonable(instance)), key=lambda e: [str(p) for p in e.absolute_path])
    out = []
    for err in errors:
        leaf = best_match(err.context) if err.context else err
        where = "/" + "/".join(str(p) for p in leaf.absolute_path)
        message = leaf.message if len(leaf.message) <= 200 else leaf.message[:199] + "…"
        out.append(f"{schema_name}.schema.json {where}: {message}")
    return out


# ---------------------------------------------------------------- envelope


_OPEN = re.compile(r"<output\s+name\s*=\s*([\"'])([^\"']*)\1\s*>")
_CLOSE = re.compile(r"</output\s*>")


def split_envelope(reply: str) -> tuple[list[tuple[str, str]], list[str]]:
    """[(output name, block text)] and envelope errors. Leading and trailing blank lines are stripped from block text,
    and non-empty text ends with one newline (so it can be written straight to a file)."""
    blocks: list[tuple[str, str]] = []
    errors: list[str] = []
    pos = 0
    while True:
        opening = _OPEN.search(reply, pos)
        if opening is None:
            break
        name = opening.group(2).strip()
        closing = _CLOSE.search(reply, opening.end())
        following = _OPEN.search(reply, opening.end())
        if closing is None or (following is not None and following.start() < closing.start()):
            errors.append(f"output {name!r} is not closed with </output>")
            pos = opening.end()
            continue
        body = reply[opening.end() : closing.start()].strip("\r\n").rstrip()
        blocks.append((name, body + "\n" if body else ""))
        pos = closing.end()
    if not blocks and not errors:
        errors.append('the answer has no <output name="..."> block; put each output in a block of its own (00 §F0)')
    return blocks, errors


def parse_reply(
    reply: str,
    declared: Sequence[tuple[str, bool]],
    *,
    formats: Mapping[str, str],
    generated_by: Mapping[str, Any],
    validators: Mapping[str, Any] | None = None,
    pipeline_fields: Mapping[str, Mapping[str, Any]] | None = None,
    where: str = "this part",
) -> tuple[dict[str, ParsedOutput], list[str]]:
    """Split and validate one reply. declared is the part's (output name, required) pairs; formats is each output's
    declared format; when validators is None, thesis-ci schemas are resolved as needed. Returns
    ({output name: ParsedOutput}, list of errors)."""
    names = [name for name, _ in declared]
    undeclared = [name for name in names if name not in formats]
    if undeclared:
        raise ValueError(f"these outputs have no declared format: {', '.join(undeclared)} (output_formats, 00 §F0)")
    if validators is None:
        validators = validators_for(names)
    blocks, errors = split_envelope(reply)
    outputs: dict[str, ParsedOutput] = {}
    seen: set[str] = set()
    for name, raw in blocks:
        if name not in names:
            errors.append(f"output {name!r} is not an output of {where} (its outputs: {', '.join(names)})")
            continue
        if name in seen:
            errors.append(f"output {name!r} appears more than once")
            continue
        seen.add(name)
        parsed, problems = parse_output(
            name,
            raw,
            fmt=formats[name],
            generated_by=generated_by,
            validators=validators,
            fields=(pipeline_fields or {}).get(name),
        )
        errors.extend(problems)
        if parsed is not None:
            outputs[name] = parsed
    for name, required in declared:
        if required and name not in seen:
            errors.append(f"output {name!r} is missing; with no content, write \"{EMPTY_MARK}\" instead of leaving it "
                          "out (00 §F0)")
    return outputs, errors


def parse_output(
    name: str,
    raw: str,
    *,
    fmt: str,
    generated_by: Mapping[str, Any],
    validators: Mapping[str, Any],
    fields: Mapping[str, Any] | None = None,
) -> tuple[ParsedOutput | None, list[str]]:
    """Validate one output against its declared format fmt. Returns (ParsedOutput or None, errors). An output with a
    schema must have a validator in validators."""
    if fmt not in FORMATS:
        raise ValueError(f"the format {fmt!r} of {name} is not one of {', '.join(FORMATS)}")
    schema = OUTPUT_SCHEMAS.get(name)
    if schema is not None and schema not in validators:
        raise SchemaUnavailable(f"{name} is validated against {schema}.schema.json, but no validator was given (see "
                                "validators_for)")
    if is_empty_mark(raw):
        return ParsedOutput(name, fmt, "", None, empty=True, schema=schema), []
    if not raw.strip():
        return None, [f"{name}: the output is empty; with no content, write \"{EMPTY_MARK}\" (00 §F0)"]
    if fmt == FILE:
        return None, [f"{name}: this is a file (a PDF or page images) and cannot go into an <output> text block"]
    if fmt == YAML:
        return _parse_yaml(name, raw, schema, validators, generated_by, fields)
    if fmt == MARKDOWN:
        return _parse_markdown(name, raw, schema, validators, generated_by, fields)
    return ParsedOutput(name, TEXT, raw, None, generated_by=generated_by), []


def _parse_yaml(
    name: str,
    raw: str,
    schema: str | None,
    validators: Mapping[str, Any],
    generated_by: Mapping[str, Any],
    fields: Mapping[str, Any] | None,
) -> tuple[ParsedOutput | None, list[str]]:
    if _fenced(raw):
        return None, [f"{name}: YAML goes without code fences (00 §F0)"]
    try:
        data = load_yaml_text(raw)
    except _yaml().YAMLError as exc:
        return None, [f"{name}: the YAML does not parse: {_yaml_problem(exc)}"]
    if data is None:
        return None, [f"{name}: the YAML is empty; with no content, write \"{EMPTY_MARK}\" (00 §F0)"]
    text = raw
    structure = STRUCTURES.get(name)
    if (schema is None and structure is not None and structure.list_key is None and isinstance(data, dict)
            and set(data) == {name} and isinstance(data[name], list)):
        data = data[name]  # "questions:\n  - id: ..." for a list output: the same rows, one level down
        text = dump_yaml(data)
    applied: tuple[str, ...] = ()
    if fields:
        if not isinstance(data, dict):
            return None, [f"{name}: should be a mapping (the pipeline writes {', '.join(fields)} into it)"]
        text, data = apply_fields(text, data, fields)
        applied = tuple(fields)
    errors: list[str] = []
    validator = validators.get(schema) if schema else None
    if schema == "sources":
        instance, errors = _sources_instance(name, data)
    else:
        instance = data
    if validator is not None and not errors:
        errors = [f"{name}: {e}" for e in _cap(schema_errors(validator, instance, schema))]
    if not errors and schema is None:
        errors = _cap(structure_errors(name, data))
    if errors:
        return None, errors
    injected = False
    strict = validator is not None and rejects_extra_keys(validator)
    if isinstance(data, dict) and "generated_by" not in data and not strict:
        text, data, injected = _append_generated_by(text, data, generated_by)
    return ParsedOutput(name, YAML, text, data, schema=schema, generated_by=generated_by,
                        generated_by_injected=injected, pipeline_fields=applied), []


def _parse_markdown(
    name: str,
    raw: str,
    schema: str | None,
    validators: Mapping[str, Any],
    generated_by: Mapping[str, Any],
    fields: Mapping[str, Any] | None,
) -> tuple[ParsedOutput | None, list[str]]:
    parts = split_front_matter(raw)
    if parts is None:
        return None, [f"{name}: a Markdown output starts with front matter (--- ... ---, 00 §F0, §F1)"]
    fm_text, body = parts
    try:
        front = load_yaml_text(fm_text) if fm_text.strip() else {}
    except _yaml().YAMLError as exc:
        return None, [f"{name}: the front matter does not parse: {_yaml_problem(exc)}"]
    if not isinstance(front, dict):
        return None, [f"{name}: the front matter should be a mapping"]
    front = dict(front)
    applied: tuple[str, ...] = ()
    if fields:
        front.update(fields)
        applied = tuple(fields)
    errors = []
    if schema is None:  # story.md is the exception to §F1: it uses only the story schema's fields
        keys = [k for k in FRONT_MATTER_KEYS if not (k == "company" and name in F1_WITHOUT_COMPANY)]
        errors += [f"{name}: the front matter has no {key} (00 §F1)" for key in keys if key not in front]
    validator = validators.get(schema) if schema else None
    if validator is not None:
        errors += [f"{name}: front matter {e}" for e in _cap(schema_errors(validator, front, schema))]
    if errors:
        return None, errors
    injected = not (validator is not None and rejects_extra_keys(validator))
    if injected:
        front["generated_by"] = dict(generated_by)
    text = raw if not (injected or applied) else f"---\n{dump_yaml(front)}---\n{body}"
    return ParsedOutput(name, MARKDOWN, text, front, schema=schema, generated_by=generated_by,
                        generated_by_injected=injected, pipeline_fields=applied), []


# ---------------------------------------------------------------- output structures (docs/decisions/0024)

# 00 §F3 and prompt 04A, word for word.
FACT_VERDICTS = frozenset({"accurate", "consistent with citation", "error", "L2 only", "unconfirmed", "basis issue"})
FINDING_GROUPS = frozenset({"must fix", "should fix", "no change"})
FINDING_TYPES = frozenset({"fact_error", "unsourced", "l2_only", "unit_or_period", "model_violation", "banned_content",
                           "process_leak", "design", "reasoning", "other"})
# 00 §F5 sections (dossier_changes; version_history adds the entry at the end of the dossier) and the parts a review
# date can be set for (thesis-ci thesis.reviewed).
ARCHIVE_SECTIONS = frozenset({"business", "economics", "moat", "capital_allocation", "management", "culture", "runway",
                              "valuation", "bear_case", "monitoring", "thesis", "breakers", "munger", "unknowns",
                              "ratings"})
REVIEWED_SECTIONS = ARCHIVE_SECTIONS - {"ratings"}
DOSSIER_TEXT_KEYS = ("text", "new_text", "content", "proposed")


@dataclasses.dataclass(frozen=True)
class Structure:
    """The shape of a YAML output: a list of rows (or, with list_key, a mapping holding that list), the keys every row
    carries (a value may be null unless listed in not_null), and the words a key may take."""

    required: tuple[str, ...]
    words: Mapping[str, frozenset[Any]] = dataclasses.field(default_factory=dict)
    not_null: tuple[str, ...] = ()
    list_key: str | None = None
    mapping_required: tuple[str, ...] = ()
    one_of: tuple[str, ...] = ()  # at least one of these keys
    rule: str = ""  # where the shape is defined, quoted in the error

    def errors(self, name: str, data: Any) -> list[str]:
        rows = data
        if self.list_key is not None:
            if not isinstance(data, Mapping) or not isinstance(data.get(self.list_key), list):
                return [f"{name}: should be a mapping with a {self.list_key} list ({self.rule})"]
            missing = [k for k in self.mapping_required if k not in data]
            if missing:
                return [f"{name}: the mapping has no {', '.join(missing)} ({self.rule})"]
            rows = data[self.list_key]
        if not isinstance(rows, list):
            shape = "a mapping" if isinstance(rows, Mapping) else type(rows).__name__
            return [f"{name}: should be a list with one entry per item, not {shape} ({self.rule})"]
        out: list[str] = []
        for i, row in enumerate(rows):
            where = f"{name}[{i}]" + (f" ({row.get('id') or row.get('test_id')})" if isinstance(row, Mapping)
                                       and (row.get("id") or row.get("test_id")) else "")
            if not isinstance(row, Mapping):
                out.append(f"{where}: should be a mapping ({self.rule})")
                continue
            missing = [k for k in self.required if k not in row]
            if missing:
                out.append(f"{where}: has no {', '.join(missing)} ({self.rule})")
            if self.one_of and not any(k in row for k in self.one_of):
                out.append(f"{where}: has none of {', '.join(self.one_of)} ({self.rule})")
            out += [f"{where}: {k} is empty" for k in self.not_null if k in row and row[k] in (None, "")]
            for key, allowed in self.words.items():
                if key in row and row[key] not in allowed and not (row[key] is None and key not in self.not_null
                                                                    and None in allowed):
                    listed = ", ".join(sorted(str(a) for a in allowed if a is not None))
                    out.append(f"{where}: {key} {row[key]!r} is not one of: {listed} ({self.rule})")
        return out


STRUCTURES: dict[str, Structure] = {
    "findings": Structure(("id", "group", "type", "location", "quote", "evidence", "fix"),
                          {"group": FINDING_GROUPS, "type": FINDING_TYPES | {None}}, ("id", "group"),
                          rule="00 §F3"),
    "fact_verdicts": Structure(("id", "verdict", "source_location", "correct_value"), {"verdict": FACT_VERDICTS},
                               ("id", "verdict"), rule="04A"),
    "questions": Structure(("id", "issue", "options", "interim", "blocking"), {"blocking": frozenset({True, False})},
                           ("id", "issue"), rule="00 §F8"),
    "revision_notes": Structure(("finding_id", "action", "how"),
                                {"action": frozenset({"fixed", "not fixed", "to HQ"})}, ("finding_id", "action"),
                                rule="00 §F4"),
    "fact_table": Structure(("id", "location", "what", "value", "source", "excerpt"), not_null=("id",),
                            list_key="facts", mapping_required=("as_of",), rule="16A"),
    "metric_values": Structure(("metric", "value", "unit", "period", "source"), not_null=("metric",), rule="16B"),
    "qualitative_verdicts": Structure(
        ("test_id", "answer", "verdict", "excerpt", "source"),
        {"verdict": frozenset({"pass", "warn", "fail", "undetermined"}),
         "answer": frozenset({"yes", "no", "cannot determine"})}, ("test_id", "verdict"), rule="14T"),
    "prereg_settlement": Structure(("id", "outcome", "evidence", "reasoning"),
                                   {"outcome": frozenset({"happened", "not_happened", "undetermined"})},
                                   ("id", "outcome"), rule="15B"),
    "ledger_settlement": Structure(("id",), {"status": frozenset({"kept", "partially_kept", "not_kept",
                                                                   "silently_dropped", "undetermined"}),
                                             "outcome": frozenset({"kept", "partially_kept", "not_kept",
                                                                    "silently_dropped", "undetermined"})},
                                   ("id",), one_of=("status", "outcome"), rule="15B, 00 §F6"),
    "dossier_changes": Structure(("section",), {"section": ARCHIVE_SECTIONS | {"version_history"}}, ("section",),
                                 one_of=DOSSIER_TEXT_KEYS, rule="03, 00 §F5"),
    "divergence_map": Structure(("id", "mark"), {"mark": frozenset({"agree", "diverge", "cannot tell"})},
                                ("id", "mark"), rule="14B"),
    "blind_answers": Structure(("id", "answer", "excerpt", "source", "confidence"), not_null=("id",), rule="14A"),
    "question_answers": Structure(("id", "answer", "excerpt", "source", "confidence"), not_null=("id",), rule="03"),
}


def structure_errors(name: str, data: Any) -> list[str]:
    """Shape and fixed-word errors of a YAML output that has no thesis-ci schema (STRUCTURES)."""
    if name == "reviewed_sections":
        sections = data.get("reviewed_sections") if isinstance(data, Mapping) else data
        if not isinstance(sections, list):
            return [f"{name}: should be a list of the parts reviewed (thesis-ci SPEC 4.2)"]
        return [f"{name}: {s!r} is not one of: {', '.join(sorted(REVIEWED_SECTIONS))}" for s in sections
                if s not in REVIEWED_SECTIONS]
    if name == "gate_decision":
        decision = data.get("decision") if isinstance(data, Mapping) else None
        if decision not in ("release", "return", "hold"):
            return [f"{name}: should be a mapping whose decision is release, return or hold, with the reasons (17A)"]
        return []
    structure = STRUCTURES.get(name)
    if structure is None:
        return []
    errors = structure.errors(name, data)
    if name == "findings" and not errors:
        for i, row in enumerate(data):
            if row.get("group") == "no change" and row.get("type") is not None:
                errors.append(f"{name}[{i}]: a no-change finding has type null (00 §F3)")
            elif row.get("group") != "no change" and row.get("type") is None:
                errors.append(f"{name}[{i}]: a {row.get('group')} finding needs a type (00 §F3)")
    return errors


SOURCED_PROSE = ("dossier", "story", "update")  # Markdown outputs whose every fact number needs a [src:] tag (00 §E1)
MAX_PROSE_ERRORS = 40


def prose_errors(parsed: Mapping[str, ParsedOutput]) -> list[str]:
    """A fact number without a [src:] tag in its sentence, in the outputs thesis-ci lint will check once placed
    (C-SRC-TAG; the same scan, thesis_ci.textscan)."""
    try:
        from thesis_ci.textscan import untagged_facts
    except ImportError:  # thesis-ci is not installed: lint catches it at placement
        return []
    errors: list[str] = []
    for name in SOURCED_PROSE:
        out = parsed.get(name)
        if out is None or out.empty:
            continue
        parts = split_front_matter(out.text)
        body = parts[1] if parts else out.text
        found = untagged_facts(body, True)
        for line, sentence, number in found[:MAX_PROSE_ERRORS]:
            errors.append(f"{name}: fact number '{number}' has no [src:] tag in its sentence (00 §E1): "
                          f"{' '.join(sentence.split())[:160]}")
        if len(found) > MAX_PROSE_ERRORS:
            errors.append(f"{name}: ... and {len(found) - MAX_PROSE_ERRORS} more sentences with an untagged number")
    return errors


def untagged_sentences(text: str) -> list[str]:
    """The sentences of a Markdown output (after its front matter) that state a fact number without a [src:] tag, as
    they are written in it (so each can be found and replaced)."""
    try:
        from thesis_ci import textscan
    except ImportError:
        return []
    parts = split_front_matter(text)
    body = parts[1] if parts else text
    masked = textscan.mask(body, True)
    out = []
    for start, end in textscan.sentence_spans(masked):
        sentence = masked[start:end]
        if textscan.FACT_RE.search(sentence) and not textscan.TAG_RE.search(sentence) and not \
                getattr(textscan, "PROBABILITY_JUDGMENT_RE", re.compile("(?!)")).search(sentence):
            original = body[start:end].strip()
            if original and original not in out:
                out.append(original)
    return out


def is_prose_error(error: str, name: str) -> bool:
    return error.startswith((f"{name}: fact number ", f"{name}: ... and "))


def apply_repairs(name: str, output: ParsedOutput, raw: str) -> tuple[ParsedOutput | None, list[str]]:
    """A retry's <output name="<name>_repairs">: a YAML list of {find, replace}, each find a sentence of the output
    exactly as written. Returns the repaired output, or the errors."""
    try:
        items = load_yaml_text(raw)
    except Exception as exc:
        return None, [f"{name}_repairs: the YAML does not parse: {_yaml_problem(exc)}"]
    if not isinstance(items, list):
        return None, [f"{name}_repairs: should be a list of {{find, replace}}"]
    text, errors = output.text, []
    for i, item in enumerate(items):
        find = item.get("find") if isinstance(item, Mapping) else None
        replace = item.get("replace") if isinstance(item, Mapping) else None
        if not isinstance(find, str) or not isinstance(replace, str) or not find.strip():
            errors.append(f"{name}_repairs[{i}]: needs find and replace, both text")
        elif find not in text:
            errors.append(f"{name}_repairs[{i}]: find is not a sentence of the {name} as written: {find[:100]}")
        else:
            text = text.replace(find, replace, 1)
    if errors:
        return None, errors
    return dataclasses.replace(output, text=text), []


def cited_tag_errors(part_id: str, inputs: Mapping[str, str], parsed: Mapping[str, ParsedOutput]) -> list[str]:
    """01B turns the dossier into system files: they may cite only the sources the dossier cites (a tag for the
    dossier itself, which is private, cannot be resolved in the public files)."""
    if part_id != "01B" or "dossier" not in inputs:
        return []
    allowed = set(re.findall(r"\[src:([A-Za-z0-9][A-Za-z0-9._-]*)", inputs["dossier"]))
    errors, seen = [], set()
    for name in ("thesis", "story", "ledger"):
        out = parsed.get(name)
        if out is None or out.empty:
            continue
        cited = set(re.findall(r"\[src:([A-Za-z0-9][A-Za-z0-9._-]*)", out.text))
        cited |= set(re.findall(r"(?m)^\s*(?:source|settlement_source|acknowledged_source):\s*['\"]?([A-Z][A-Za-z0-9._-]*)",
                                out.text))
        for tag in sorted(t.split("#")[0] for t in cited):
            if tag not in allowed and tag not in seen:
                seen.add(tag)
                errors.append(f"{name}: source {tag} is not a source the dossier cites; cite the filing the dossier "
                              "cites for that fact instead (the dossier is private and cannot be cited)")
    return errors


def coverage_errors(part_id: str, inputs: Mapping[str, str], parsed: Mapping[str, ParsedOutput]) -> list[str]:
    """Checks of a reply against the inputs it answers: 04A gives every fact under facts in its fact_table exactly one
    verdict and no other (a slice's context_facts get none; pipeline/slicing.py); prose carries its source tags; 01B
    cites only what the dossier cites."""
    errors = prose_errors(parsed) + cited_tag_errors(part_id, inputs, parsed)
    if errors:
        return errors
    if part_id != "04A" or "fact_table" not in inputs or "fact_verdicts" not in parsed:
        return []
    from . import slicing

    try:
        table = load_yaml_text(inputs["fact_table"])
    except Exception:  # the input was checked when it was assembled; nothing to compare against otherwise
        return []
    ids = slicing.fact_ids(table)
    if not ids:  # no fact carries an id: nothing to compare against
        return []
    verdicts = parsed["fact_verdicts"]
    rows = verdicts.data if not verdicts.empty and isinstance(verdicts.data, list) else []
    return slicing.verdict_coverage(ids, rows)


def _cap(errors: list[str]) -> list[str]:
    if len(errors) <= MAX_ERRORS_PER_OUTPUT:
        return errors
    return errors[:MAX_ERRORS_PER_OUTPUT] + [f"... and {len(errors) - MAX_ERRORS_PER_OUTPUT} more errors of this kind"]


def _sources_instance(name: str, data: Any) -> tuple[Any, list[str]]:
    entries = data.get("sources") if isinstance(data, dict) and set(data) == {"sources"} else data
    if not isinstance(entries, list):
        return None, [f"{name}: should be a list of sources entries, each with its visibility (00 §F2)"]
    errors = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict) or entry.get("visibility") not in SOURCE_VISIBILITIES:
            errors.append(f"{name}[{i}]: visibility must be public or private (it decides which repository's "
                          "sources.yml the entry goes into)")
    return {"sources": [_without_visibility(e) for e in entries]}, errors


def _without_visibility(entry: Any) -> Any:
    return {k: v for k, v in entry.items() if k != "visibility"} if isinstance(entry, dict) else entry


def split_sources_additions(data: Any) -> dict[str, list[dict[str, Any]]]:
    """Split sources_additions between the two repositories by visibility, and remove visibility (the sources schema
    does not accept that key). A public entry goes into the private sources.yml as well: thesis-ci resolves a tag only
    within its own repository, and the private files (the dossier, the valuation) cite public sources too."""
    entries = data.get("sources") if isinstance(data, dict) and set(data) == {"sources"} else data
    out: dict[str, list[dict[str, Any]]] = {v: [] for v in SOURCE_VISIBILITIES}
    for entry in entries or []:
        if not isinstance(entry, dict) or entry.get("visibility") not in SOURCE_VISIBILITIES:
            raise ValueError(f"a sources_additions entry has no visibility: {entry!r}")
        out[entry["visibility"]].append(_without_visibility(entry))
        if entry["visibility"] == PUBLIC:
            out[PRIVATE].append(_without_visibility(entry))
    return out


def _append_generated_by(text: str, data: dict, generated_by: Mapping[str, Any]) -> tuple[str, dict, bool]:
    """Append generated_by at the end of a YAML mapping. If the result then parses wrong (a flow mapping, for
    example), leave the text unchanged and return generated_by alongside instead."""
    candidate = text.rstrip("\n") + "\n" + dump_yaml({"generated_by": dict(generated_by)})
    expected = {**data, "generated_by": dict(generated_by)}
    try:
        if jsonable(load_yaml_text(candidate)) == jsonable(expected):
            return candidate, expected, True
    except Exception:
        pass
    return text, data, False


_TOP_KEY = re.compile(r"^([A-Za-z_][\w.-]*)[ \t]*:(?:[ \t]|$)")


def apply_fields(text: str, data: dict, fields: Mapping[str, Any]) -> tuple[str, dict]:
    """Write the pipeline-maintained top-level fields (00 §G8) into YAML text: existing keys are replaced in place,
    missing ones are added after the leading comments.

    Keeps the model's comments and formatting where possible; the fields are written as the repositories write them
    (dump_fields), so a draft that keeps a field unchanged shows no change in a diff. When the edited text does not
    parse to the expected data, falls back to dumping the whole document again (the comments are lost).
    """
    merged = {**data, **fields}
    lines = text.split("\n")
    missing = []
    for key, value in fields.items():
        block = dump_fields({key: value}).rstrip("\n").split("\n")
        start = next((i for i, line in enumerate(lines) if _top_key(line) == key), None)
        if start is None:
            missing.extend(block)
            continue
        end = start + 1
        while end < len(lines) and _top_key(lines[end]) is None and lines[end].strip() not in ("---", "..."):
            end += 1
        while end - 1 > start and (not lines[end - 1].strip() or lines[end - 1].startswith("#")):
            end -= 1  # blank lines and unindented comments after the block belong to the next section
        lines[start:end] = block
    if missing:
        head = 0
        while head < len(lines) and (not lines[head].strip() or lines[head].startswith("#") or lines[head].strip() == "---"):
            head += 1
        lines[head:head] = missing
    candidate = "\n".join(lines)
    try:
        if jsonable(load_yaml_text(candidate)) == jsonable(merged):
            return candidate, merged
    except Exception:
        pass
    return dump_yaml(merged), merged


def _top_key(line: str) -> str | None:
    match = _TOP_KEY.match(line)
    return match.group(1) if match else None


# ---------------------------------------------------------------- placement (00 §F2)


PUBLIC, PRIVATE, SPLIT = "public", "private", "split"
REPOS = {PUBLIC: "owners-office", PRIVATE: "owners-office-private"}
PUBLIC_DOSSIERS = frozenset({"MSFT"})  # §F2: MSFT's dossier.md is public (part 8 contains no numbers)
# When 04's subject variable ({{subject}}) is a quarterly update, 04A's findings go public with the PR. `when`
# conditions are compared case-insensitively.
UPDATE_SUBJECTS = frozenset({"quarterly update", "update"})


@dataclasses.dataclass(frozen=True)
class Destination:
    """A row of the placement table. path is a template relative to the repository root; None means it is not a file
    (PR body, PR attachment)."""

    visibility: str  # public | private | split (sources_additions: entries split between the repos by visibility)
    path: str | None
    action: str = "write"  # write | append | patch | merge | front_matter | pr_body | pr_attachment | settlement
    when: Mapping[str, frozenset[str]] = dataclasses.field(default_factory=dict)
    note: str = ""


@dataclasses.dataclass(frozen=True)
class Placement:
    output: str
    repo: str  # owners-office | owners-office-private
    visibility: str  # public | private
    path: str | None
    action: str
    note: str = ""


def _d(visibility: str, path: str | None, action: str = "write", note: str = "", **when: Iterable[str]) -> Destination:
    return Destination(visibility, path, action, {k: frozenset(v) for k, v in when.items()}, note)


_REPORTS = "reports/{company}/{doc}"
# Output name → candidate destinations; the first one whose `when` conditions all hold takes effect; if none does,
# DEFAULT_DESTINATION is used.
# Template fields: company, period, run_date, month, doc, slug, scope, part_id, output, ext (see place()).
PLACEMENT: dict[str, tuple[Destination, ...]] = {
    # Public repository (§F2 row 1; paths per thesis-ci SPEC §2)
    "thesis": (_d(PUBLIC, "companies/{company}/thesis.yml"),),
    "story": (_d(PUBLIC, "companies/{company}/story.md"),),
    "ledger": (_d(PUBLIC, "companies/{company}/ledger.yml"),),
    "prereg": (_d(PUBLIC, "companies/{company}/prereg/{period}.yml",
                  note="the items file; the timestamp and the settlement are separate files (SPEC §2.1)"),),
    # 15B: one settlement file per pre-registration period of the items settled, <period>.settlement.yml (SPEC §2.1,
    # thesis-ci prereg-settlement.schema.json); the pipeline writes its header fields (docs/decisions/0024)
    "prereg_settlement": (_d(PUBLIC, "companies/{company}/prereg/", "settlement",
                             "one <period>.settlement.yml per pre-registration period of the items settled",
                             part_id={"15B"}),),
    "update": (_d(PUBLIC, "companies/{company}/updates/{run_date}.md"),),
    "reviewed_sections": (
        _d(PUBLIC, "companies/{company}/updates/{run_date}.md", "front_matter",
           "reviewed_sections in the front matter of the update record (SPEC §4.2)"),
    ),
    "pr_body": (_d(PUBLIC, None, "pr_body",
                   "the pipeline appends the audit findings, the divergences and the inversion list"),),
    "mistakes_entry": (_d(PUBLIC, "mistakes.md", "append"),),
    "letter": (_d(PUBLIC, "letters/{month}.md"),),
    # Dossier: public for MSFT (§F2 row 2), private for the other companies (§F2 row 4)
    "dossier": (
        _d(PUBLIC, "companies/{company}/dossier.md", company=PUBLIC_DOSSIERS),
        _d(PRIVATE, "companies/{company}/dossier.md"),
    ),
    "dossier_changes": (
        _d(PUBLIC, "companies/{company}/dossier.md", "patch", "replaces the dossier part by part",
           company=PUBLIC_DOSSIERS),
        _d(PRIVATE, "companies/{company}/dossier.md", "patch", "replaces the dossier part by part"),
    ),
    # Attachments to the quarterly update PR: public with the PR, subject to §H4 (§F2 row 3); outputs with the same
    # names in other cases go to the private repository by default
    "findings": (_d(PUBLIC, None, "pr_attachment", part_id={"04A"}, subject=UPDATE_SUBJECTS),),
    "inversion_list": (_d(PUBLIC, None, "pr_attachment", part_id={"04B-lite"}),),
    "divergence_map": (_d(PUBLIC, None, "pr_attachment", part_id={"14B"}),),
    "qualitative_verdicts": (_d(PUBLIC, None, "pr_attachment", part_id={"14T"}),),
    # Sources: merged into the sources.yml of both repositories according to each entry's visibility, which is removed
    # before writing (split_sources_additions)
    "sources_additions": (_d(SPLIT, "companies/{company}/sources.yml", "merge",
                             "merged into each repository by each entry's visibility"),),
    # Private repository (§F2 row 4; paths per SPEC §2, §7)
    "valuation_yml": (
        _d(PRIVATE, "companies/{company}/valuation.yml",
           note="a proposed version (doc_status: proposed) takes effect only after 04C approves it (§V20)"),
    ),
    "valuation_md": (_d(PRIVATE, "companies/{company}/valuation.md"),),
    "escalation": (_d(PRIVATE, "escalations/{run_date}-{company}-{slug}.yml"),),
    "memo": (_d(PRIVATE, "memos/{run_date}-{company}-{slug}.yml"),),
    "ranking": (_d(PRIVATE, "hq/ranking.yml"),),
    "private_appendix": (_d(PRIVATE, "letters/{month}-private-appendix.md"),),
    # reports/: the text, layout intent and connections list of 02, 05, 06–08, 10, 11, 13;
    # the PDF and page images of 19
    "report": (_d(PRIVATE, f"{_REPORTS}/report.md"),),
    "revised_report": (_d(PRIVATE, f"{_REPORTS}/report.md"),),
    "document": (_d(PRIVATE, f"{_REPORTS}/document.md"),),
    "revised_document": (_d(PRIVATE, f"{_REPORTS}/document.md"),),
    "cover": (_d(PRIVATE, f"{_REPORTS}/cover.{{ext}}"),),
    "charts": (_d(PRIVATE, f"{_REPORTS}/charts.{{ext}}"),),
    "main_visual": (_d(PRIVATE, f"{_REPORTS}/main_visual.{{ext}}"),),
    "visual_specs": (_d(PRIVATE, f"{_REPORTS}/visual_specs.yml"),),
    "connections": (_d(PRIVATE, f"{_REPORTS}/connections.yml"),),
    "revised_connections": (_d(PRIVATE, f"{_REPORTS}/connections.yml"),),
    "pdf": (_d(PRIVATE, f"{_REPORTS}/{{doc}}.pdf"),),
    "rendered_pages": (_d(PRIVATE, f"{_REPORTS}/pages/"),),
}
DEFAULT_DESTINATION = _d(PRIVATE, "runs/{scope}/{run_dir}/{output}.{ext}",
                         note="§F2: outputs not listed in the table go to the private repository")
TRUST_ROUTED_PROMPTS = frozenset({"03"})  # §G9: quarterly updates are routed by trust level


class PlacementError(ValueError):
    """A path template is missing a field."""


def place(output: str, *, prompt_id: str, part_id: str, fmt: str | None = None, **context: Any) -> list[Placement]:
    """Where one output goes (00 §F2). For sources_additions it returns two (public, private); for the rest, one.

    fmt is the output's declared format and decides the {ext} extension in the path (yml for yaml, md otherwise); a
    path that uses {ext} with no fmt given is an error.
    context gives the template fields: company (ticker), period (FY<year>Q<quarter>), run_date (YYYY-MM-DD), month
    (YYYY-MM, for the letter to the owner), doc (02/06/07/08/11, the report directory), subject (04's subject under
    review), slug (the file name for memos and escalation requests; defaults to the output name), run_dir (the run
    directory's name; defaults to <run_date>-<part_id>, and an HQ step about one company adds -<TICKER>).
    Raises PlacementError when a field the template needs is missing.
    """
    ctx = {k: v for k, v in context.items() if v is not None}
    ctx.update(output=output, part_id=part_id, prompt_id=prompt_id)
    ctx.setdefault("slug", output)
    ctx.setdefault("scope", ctx.get("company") or "hq")
    if "run_date" in ctx:
        ctx.setdefault("run_dir", f"{ctx['run_date']}-{part_id}")
    if fmt is not None:
        ctx.setdefault("ext", "yml" if fmt == YAML else "md")
    dest = next(
        (d for d in PLACEMENT.get(output, ()) if all(_allowed(ctx.get(k), allowed) for k, allowed in d.when.items())),
        DEFAULT_DESTINATION,
    )
    path = None
    if dest.path is not None:
        needed = {field for _, field, _, _ in string.Formatter().parse(dest.path) if field}
        missing = sorted(needed - set(ctx))
        if missing:
            raise PlacementError(f"the destination {dest.path} of {output} needs {', '.join(missing)}")
        path = dest.path.format_map(ctx)
    visibilities = (PUBLIC, PRIVATE) if dest.visibility == SPLIT else (dest.visibility,)
    return [Placement(output, REPOS[v], v, path, dest.action, _note(dest.note, prompt_id, v)) for v in visibilities]


def _allowed(value: Any, allowed: frozenset[str]) -> bool:
    return str(value).casefold() in {item.casefold() for item in allowed}


def _note(note: str, prompt_id: str, visibility: str) -> str:
    if prompt_id in TRUST_ROUTED_PROMPTS and visibility == PUBLIC:
        return "; ".join(filter(None, [note, "§G9: routed by trust level; at level 1 or below it stays in the private "
                                             "repository until HQ has reviewed it"]))
    return note


def place_outputs(
    outputs: Mapping[str, ParsedOutput], *, prompt_id: str, part_id: str, **context: Any
) -> list[Placement]:
    """Where each non-empty output of one call goes."""
    placements: list[Placement] = []
    for name, parsed in outputs.items():
        if parsed.empty:
            continue
        placements.extend(place(name, prompt_id=prompt_id, part_id=part_id, fmt=parsed.format, **context))
    return placements
