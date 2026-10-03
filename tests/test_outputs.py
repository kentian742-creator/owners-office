"""Tests of pipeline/outputs.py: output envelopes, validation, generated_by and placement (00 §F0, §F1, §F2)."""

from __future__ import annotations

import json

import pytest

from pipeline import outputs
from tests.llm_fixtures import SCHEMAS

GENERATED_BY = {"model": "claude-sonnet-5", "backend": "api", "rules_version": "9.0", "prompt": "03",
                "prompt_version": "3.1", "input_sha256": "0" * 64}
# The output names and formats these tests use (the real declaration is output_formats in the front matter of 00 in
# the private repository)
FORMATS = {
    **dict.fromkeys(["findings", "questions", "layout_instructions", "thesis", "sources_additions", "fact_verdicts",
                     "model_checks", "gate_decision"], "yaml"),
    **dict.fromkeys(["letter", "dossier", "private_appendix", "story", "update"], "markdown"),
    **dict.fromkeys(["reply", "bear_case", "notes"], "text"),
    "pdf": "file",
}


@pytest.fixture
def schemas(tmp_path):
    for name, schema in SCHEMAS.items():
        (tmp_path / f"{name}.schema.json").write_text(json.dumps(schema), encoding="utf-8")
    return tmp_path


def parse(reply, declared, schemas_dir, **kwargs):
    validators = outputs.validators_for([name for name, _ in declared], schemas_dir)
    formats = {name: FORMATS[name] for name, _ in declared}
    return outputs.parse_reply(reply, declared, formats=formats, generated_by=GENERATED_BY, validators=validators,
                               **kwargs)


# ---------------------------------------------------------------- envelope


def test_envelope_blocks_are_split_by_name():
    blocks, errors = outputs.split_envelope("Preamble\n<output name='a'>\none\n</output>\n<output name=\"b\">two</output>")
    assert blocks == [("a", "one\n"), ("b", "two\n")]
    assert errors == []


def test_unclosed_block_and_missing_envelope_are_errors():
    _, errors = outputs.split_envelope('<output name="a">\none\n<output name="b">two</output>')
    assert errors == ["output 'a' is not closed with </output>"]
    _, errors = outputs.split_envelope("Only prose")
    assert "has no <output" in errors[0]


def test_names_must_match_the_declared_outputs(schemas):
    reply = '<output name="findings">- a</output><output name="findings">- b</output><output name="extra">x</output>'
    parsed, errors = parse(reply, [("findings", True), ("questions", True), ("notes", False)], schemas)
    assert "output 'findings' appears more than once" in errors
    assert any(e.startswith("output 'extra' is not an output of this part") for e in errors)
    assert any(e.startswith("output 'questions' is missing") and '"none"' in e for e in errors)
    assert not any(e.startswith("output 'notes' is missing") for e in errors)  # optional outputs may be left out


def test_every_declared_output_needs_a_declared_format(schemas):
    with pytest.raises(ValueError, match="no declared format"):
        outputs.parse_reply('<output name="findings">- a</output>', [("findings", True)], formats={},
                            generated_by=GENERATED_BY, validators={})
    with pytest.raises(ValueError, match="format"):
        outputs.parse_output("findings", "- a", fmt="auto", generated_by=GENERATED_BY, validators={})


def test_format_declarations_must_suit_the_schemas():
    assert outputs.format_problems({"thesis": "yaml", "story": "markdown", "reply": "text"}) == []
    assert outputs.format_problems({"thesis": "text", "story": "yaml", "reply": "prose"}) == [
        "the format 'prose' of reply is not one of yaml, markdown, text, file",
        "thesis has a thesis-ci schema (thesis), so its format should be yaml, not text",
        "story has a thesis-ci schema (story), so its format should be markdown, not yaml",
    ]


@pytest.mark.parametrize("mark", ["none", "None", "NONE", "  none  "])
def test_empty_mark_means_empty_in_any_letter_case(schemas, mark):
    parsed, errors = parse(f'<output name="thesis">\n{mark}\n</output>', [("thesis", True)], schemas)
    assert errors == []
    assert parsed["thesis"].empty and parsed["thesis"].text == "" and parsed["thesis"].data is None


def test_only_the_bare_marker_is_empty():
    assert outputs.EMPTY_MARK == "none"
    assert outputs.is_empty_mark("None\n") and not outputs.is_empty_mark("none so far") and not outputs.is_empty_mark(None)


# ---------------------------------------------------------------- YAML


def test_yaml_problems_are_reported(schemas):
    declared = [("findings", True), ("questions", True), ("layout_instructions", True)]
    reply = (
        '<output name="findings">```yaml\n- a\n```</output>'
        '<output name="questions">id: 1\nid: 2</output>'
        '<output name="layout_instructions"># only a comment</output>'
    )
    _, errors = parse(reply, declared, schemas)
    assert "findings: YAML goes without code fences (00 §F0)" in errors
    assert any(e.startswith("questions: the YAML does not parse: ") and "duplicate key 'id'" in e for e in errors)
    assert any(e.startswith("layout_instructions: the YAML is empty") for e in errors)


def test_schema_errors_name_the_output_and_the_path(schemas):
    reply = '<output name="thesis">company: TEST\ntrust_level: high\ntests: []\nextra: 1</output>'
    _, errors = parse(reply, [("thesis", True)], schemas)
    assert "thesis: thesis.schema.json /: Additional properties are not allowed ('extra' was unexpected)" in errors
    assert "thesis: thesis.schema.json /trust_level: 'high' is not of type 'integer'" in errors


def test_a_schema_output_is_never_left_unvalidated():
    with pytest.raises(outputs.SchemaUnavailable):
        outputs.parse_output("thesis", "company: TEST", fmt="yaml", generated_by=GENERATED_BY, validators={})
    with pytest.raises(outputs.SchemaUnavailable):
        outputs.validators_for(["thesis"], "/nonexistent/schemas")


def test_sources_additions_need_visibility_and_match_the_sources_schema(schemas):
    good = ("- {tag: T-10K-FY2025, kind: filing, title: Annual report, visibility: public}\n"
            "- {tag: T-RPT1-2026-09-23, kind: report, title: Report, visibility: private}")
    parsed, errors = parse(f'<output name="sources_additions">{good}</output>', [("sources_additions", True)], schemas)
    assert errors == []
    split = outputs.split_sources_additions(parsed["sources_additions"].data)
    assert [e["tag"] for e in split["public"]] == ["T-10K-FY2025"]
    assert [e["tag"] for e in split["private"]] == ["T-10K-FY2025", "T-RPT1-2026-09-23"]  # public ones mirrored
    assert all("visibility" not in e for e in split["public"] + split["private"])

    bad = "- {tag: T-1, kind: filing, title: x}\n- {tag: T-2, title: y, visibility: public}"
    _, errors = parse(f'<output name="sources_additions">{bad}</output>', [("sources_additions", True)], schemas)
    assert errors == ["sources_additions[0]: visibility must be public or private (it decides which repository's "
                      "sources.yml the entry goes into)"]


def test_pipeline_fields_replace_nested_blocks_and_keep_comments():
    text = (
        "# file header comment\n"
        "schema_version: \"0.1\"\n"
        "company: TEST\n"
        "filer:\n  cik: \"0000000001\"\n  type: domestic\n"
        "\n# comment of the next section\n"
        "category: stalwart\n"
    )
    data = outputs.load_yaml_text(text)
    new_text, merged = outputs.apply_fields(text, data, {"schema_version": "0.2", "filer": {"type": "foreign"},
                                                          "trust_level": 1})
    assert merged == {"schema_version": "0.2", "company": "TEST", "filer": {"type": "foreign"}, "category": "stalwart",
                      "trust_level": 1}
    assert outputs.load_yaml_text(new_text) == merged
    assert new_text.startswith("# file header comment\ntrust_level: 1\nschema_version: \"0.2\"\n")
    assert "\n# comment of the next section\ncategory: stalwart\n" in new_text
    assert "0000000001" not in new_text


def test_pipeline_fields_keep_the_repositories_quoting_so_an_unchanged_field_shows_no_diff():
    """The rehearsal of 2026-09-27: the fields came back as cik: 0000004962 and schema_version: '0.2', so the diff
    16A reads showed changes the model had not made (and YAML 1.2 parsers read 0000004962 as a number)."""
    current = ('schema_version: "0.2"\ncompany: AXP\nstatus: candidate\nfiler:\n  cik: "0000004962"\n  type: domestic\n'
               '  fiscal_year_end: "12-31"\n  earnings_form: 8-K\n  annual_form: 10-K\n# claims\nclaim: text\n')
    data = outputs.load_yaml_text(current)
    fields = {k: data[k] for k in ("schema_version", "company", "status", "filer")}
    new_text, merged = outputs.apply_fields(current, data, fields)
    assert new_text == current and merged == data
    changed, _ = outputs.apply_fields(current, data, {"filer": {**data["filer"], "cik": "0000000042"}})
    assert '  cik: "0000000042"\n' in changed and '  fiscal_year_end: "12-31"\n' in changed
    assert outputs.dump_fields({"a": "2026-07-24", "b": "yes", "c": "8-K", "d": "12,256"}) == \
        'a: "2026-07-24"\nb: "yes"\nc: 8-K\nd: "12,256"\n'


def test_a_list_output_written_under_its_own_name_is_unwrapped():
    """MCD's 01A wrote questions:\n  - id: ... twice (2026-09-28); the rows are the same, one level down."""
    raw = "questions:\n  - id: Q1\n    issue: Which basis?\n    options: [a, b]\n    interim: a\n    blocking: false\n"
    parsed, errors = outputs._parse_yaml("questions", raw, None, {}, {"model": "m"}, None)
    assert errors == [] and parsed.data == [{"id": "Q1", "issue": "Which basis?", "options": ["a", "b"],
                                             "interim": "a", "blocking": False}]
    assert outputs.load_yaml_text(parsed.text)[0]["id"] == "Q1"


def test_pipeline_fields_fall_back_to_a_full_dump_when_text_cannot_be_patched():
    text = "{company: TEST, trust_level: 3}"
    new_text, merged = outputs.apply_fields(text, outputs.load_yaml_text(text), {"trust_level": 1})
    assert outputs.load_yaml_text(new_text) == merged == {"company": "TEST", "trust_level": 1}


# ---------------------------------------------------------------- Markdown and generated_by


def test_f1_front_matter_keys_are_required(schemas):
    letter = "---\ndoc: letter\nas_of: 2026-10-01\ndoc_status: draft\n---\nLetter.\n"
    appendix = "---\ndoc: private_appendix\nas_of: 2026-10-01\ndoc_status: draft\n---\nAppendix.\n"
    dossier = "---\ndoc: dossier\nas_of: 2026-10-01\ndoc_status: draft\n---\nDossier.\n"
    parsed, errors = parse(
        f'<output name="letter">{letter}</output><output name="private_appendix">{appendix}</output>'
        f'<output name="dossier">{dossier}</output>',
        [("letter", True), ("private_appendix", True), ("dossier", True)],
        schemas,
    )
    assert errors == ["dossier: the front matter has no company (00 §F1)"]  # letters belong to no one company
    assert parsed["letter"].data["generated_by"] == GENERATED_BY
    assert parsed["letter"].text.startswith("---\ndoc: letter\n") and parsed["letter"].text.endswith("---\nLetter.\n")
    assert parsed["private_appendix"].generated_by_injected


def test_story_front_matter_is_validated_and_left_alone(schemas):
    story = "---\ncompany: TEST\nas_of: 2026-09-24\nstatus: sold\n---\nStory.\n"
    _, errors = parse(f'<output name="story">{story}</output>', [("story", True)], schemas)
    assert errors == ["story: front matter story.schema.json /status: 'sold' is not one of "
                      "['holding', 'candidate', 'archive']"]


def test_text_outputs_are_left_alone_and_yaml_outputs_are_strict(schemas):
    reply = (
        '<output name="reply">Summary: no memo; the reasons follow.</output>'
        '<output name="bear_case">---\nprose that starts with a rule\n---\nmore prose</output>'
        '<output name="layout_instructions">Page 3 - move the chart. The reasons are in questions.</output>'
    )
    parsed, errors = parse(reply, [("reply", True), ("bear_case", True), ("layout_instructions", True)], schemas)
    assert parsed["reply"].text == "Summary: no memo; the reasons follow.\n"  # text: left alone even if it looks like YAML
    assert parsed["bear_case"].format == "text" and not parsed["bear_case"].generated_by_injected
    assert errors == []  # one sentence is a valid YAML scalar
    assert parsed["layout_instructions"].data == "Page 3 - move the chart. The reasons are in questions."
    _, errors = parse('<output name="layout_instructions">page: 3\n  change: [unclosed</output>',
                      [("layout_instructions", True)], schemas)
    assert errors and errors[0].startswith("layout_instructions: the YAML does not parse")


def test_generated_by_is_appended_to_schemaless_mappings_only_when_it_round_trips(schemas):
    mapping = "as_of: 2026-09-24\nchanges: []\n"
    flow = "{as_of: 2026-09-24, checks: []}\n...\n"
    parsed, errors = parse(
        f'<output name="layout_instructions">{mapping}</output><output name="model_checks">{flow}</output>',
        [("layout_instructions", True), ("model_checks", True)],
        schemas,
    )
    assert errors == []
    assert parsed["layout_instructions"].generated_by_injected
    assert parsed["layout_instructions"].text.startswith(mapping)
    assert not parsed["model_checks"].generated_by_injected


FACT = ("- id: F001\n  location: update 2\n  subject: TEST\n  what: revenue\n  value: 100\n  unit: USD million\n"
        "  period: FY2026Q3\n  source: TEST-10Q-FY2026Q3#Item2\n  excerpt: 'Excerpt: revenue of $100 million'\n")


@pytest.mark.parametrize("output, text, error", [
    ("fact_table", FACT, "fact_table: should be a mapping with a facts list (16A)"),
    ("fact_table", "facts:\n" + FACT.replace("\n  ", "\n    ").replace("- ", "  - ", 1),
     "fact_table: the mapping has no as_of (16A)"),
    ("fact_table", "as_of: 2026-10-21\nfacts:\n  - id: F001\n    what: revenue\n",
     "fact_table[0] (F001): has no location, value, source, excerpt (16A)"),
    ("metric_values", "revenue_yoy: 12.5\n", "metric_values: should be a list with one entry per item, not a mapping"),
    ("metric_values", "- metric: revenue_yoy\n  value: 12.5\n", "metric_values[0]: has no unit, period, source (16B)"),
    ("qualitative_verdicts", "- test_id: TEST-L1\n  answer: 'No'\n  verdict: passed\n  excerpt: x\n  source: []\n",
     "qualitative_verdicts[0] (TEST-L1): verdict 'passed' is not one of: fail, pass, undetermined, warn (14T)"),
    ("qualitative_verdicts", "- test_id: TEST-L1\n  answer: 'No'\n  verdict: pass\n  excerpt: x\n  source: []\n",
     "qualitative_verdicts[0] (TEST-L1): answer 'No' is not one of: cannot determine, no, yes (14T)"),
    ("prereg_settlement", "- id: TEST-FY2026Q3-1\n  outcome: true\n  evidence: x\n  reasoning: y\n",
     "prereg_settlement[0] (TEST-FY2026Q3-1): outcome True is not one of: happened, not_happened, undetermined (15B)"),
    ("ledger_settlement", "- id: TEST-M-1\n  result: kept\n", "ledger_settlement[0] (TEST-M-1): has none of status, "
                                                               "outcome (15B, 00 §F6)"),
    ("revision_notes", "- finding_id: 04A-01\n  action: done\n  how: x\n",
     "revision_notes[0]: action 'done' is not one of: fixed, not fixed, to HQ (00 §F4)"),
    ("dossier_changes", "- section: Moat\n  text: New text.\n",
     "dossier_changes[0]: section 'Moat' is not one of: bear_case, breakers"),
    ("reviewed_sections", "[moat, Moat]\n", "reviewed_sections: 'Moat' is not one of: bear_case"),
    ("gate_decision", "Release: the checks pass.\n",
     "gate_decision: should be a mapping whose decision is release, return or hold"),
    ("divergence_map", "- id: Q01\n  mark: disagree\n",
     "divergence_map[0] (Q01): mark 'disagree' is not one of: agree, cannot tell, diverge (14B)"),
])
def test_yaml_outputs_that_later_steps_read_are_checked_for_shape_and_words(schemas, output, text, error):
    """Decision 0024: YAML that parses but not in the part's format is refused with the rule it breaks, so the one
    retry can fix it; the words are the rules' own (00 §F3, §F4, §F8 and the prompts)."""
    FORMATS.setdefault(output, "yaml")
    _, errors = parse(f'<output name="{output}">{text}</output>', [(output, True)], schemas)
    assert any(e.startswith(error) for e in errors), errors


WELL_FORMED = {
    "fact_table": "as_of: 2026-10-21\nfacts:\n" + "".join("  " + line + "\n" for line in FACT.splitlines()),
    "metric_values": "- metric: revenue_yoy\n  value: not_found\n  unit: '%'\n  period: FY2026Q3\n  source: null\n",
    "qualitative_verdicts": ("- test_id: TEST-L1\n  answer: cannot determine\n  verdict: undetermined\n"
                             "  excerpt: null\n  source: []\n"),
    "dossier_changes": "- section: moat\n  text: New text of part 3.\n",
    "reviewed_sections": "[moat, monitoring]\n",
    "gate_decision": "decision: hold\nreasons: [the audit is open]\n",
}


def test_well_formed_structured_outputs_pass(schemas):
    for output in WELL_FORMED:
        FORMATS.setdefault(output, "yaml")
    reply = "".join(f'<output name="{name}">{text}</output>' for name, text in WELL_FORMED.items())
    parsed, errors = parse(reply, [(name, True) for name in WELL_FORMED], schemas)
    assert errors == [] and parsed["fact_table"].data["facts"][0]["id"] == "F001"


def test_file_outputs_cannot_travel_in_text(schemas):
    _, errors = parse('<output name="pdf">%PDF-1.7</output>', [("pdf", True)], schemas)
    assert errors == ["pdf: this is a file (a PDF or page images) and cannot go into an <output> text block"]


def test_real_thesis_ci_story_schema_is_used_by_default(monkeypatch):
    pytest.importorskip("thesis_ci")
    monkeypatch.delenv(outputs.SCHEMAS_ENV, raising=False)
    validators = outputs.validators_for(["story"])
    good = "---\ncompany: TEST\nas_of: 2026-09-24\nstatus: holding\n---\nStory.\n"
    parsed, errors = outputs.parse_reply(f'<output name="story">{good}</output>', [("story", True)],
                                         formats={"story": "markdown"}, generated_by=GENERATED_BY,
                                         validators=validators)
    assert errors == []
    assert parsed["story"].generated_by == GENERATED_BY and not parsed["story"].generated_by_injected


# ---------------------------------------------------------------- placement (00 §F2)


def one(output, **kwargs):
    (placement,) = outputs.place(output, **kwargs)
    return placement


def test_public_files_follow_the_spec_layout():
    ctx = dict(prompt_id="01", part_id="01B", company="APP", run_date="2026-11-05", period="FY2026Q3")
    assert one("thesis", **ctx) == outputs.Placement("thesis", "owners-office", "public", "companies/APP/thesis.yml",
                                                     "write")
    assert one("story", **ctx).path == "companies/APP/story.md"
    assert one("prereg", **{**ctx, "prompt_id": "15", "part_id": "15A"}).path == "companies/APP/prereg/FY2026Q3.yml"
    assert one("mistakes_entry", **ctx).action == "append"
    assert one("pr_body", **ctx).path is None


def test_dossier_is_public_only_for_msft():
    ctx = dict(prompt_id="01", part_id="01A", run_date="2026-11-05")
    assert one("dossier", company="MSFT", **ctx).visibility == "public"
    assert one("dossier", company="APP", **ctx).repo == "owners-office-private"


@pytest.mark.parametrize("subject", ["quarterly update", "Quarterly update", "update"])
def test_findings_on_a_quarterly_update_go_public_with_the_pr(subject):
    ctx = dict(company="APP", run_date="2026-11-05")
    assert one("findings", prompt_id="04", part_id="04A", subject=subject, **ctx).action == "pr_attachment"


def test_pr_attachments_depend_on_the_part():
    ctx = dict(company="APP", run_date="2026-11-05")
    private = one("findings", prompt_id="04", part_id="04A", subject="research report", fmt="yaml", **ctx)
    assert (private.visibility, private.path) == ("private", "runs/APP/2026-11-05-04A/findings.yml")
    assert one("inversion_list", prompt_id="04", part_id="04B-lite", **ctx).visibility == "public"
    assert one("inversion_list", prompt_id="04", part_id="04B", fmt="yaml", **ctx).visibility == "private"


def test_sources_additions_go_to_both_repositories():
    placements = outputs.place("sources_additions", prompt_id="03", part_id="03-draft", company="APP",
                               run_date="2026-11-05")
    assert [(p.repo, p.path, p.action) for p in placements] == [
        ("owners-office", "companies/APP/sources.yml", "merge"),
        ("owners-office-private", "companies/APP/sources.yml", "merge"),
    ]
    assert "§G9" in placements[0].note and "§G9" not in placements[1].note


def test_private_files_and_defaults():
    ctx = dict(company="APP", run_date="2026-11-05")
    assert one("escalation", prompt_id="03", part_id="03-draft", **ctx).path == "escalations/2026-11-05-APP-escalation.yml"
    assert one("report", prompt_id="02", part_id="02", doc="02", **ctx).path == "reports/APP/02/report.md"
    assert one("ranking", prompt_id="17", part_id="17C", run_date="2026-11-05").path == "hq/ranking.yml"
    assert (one("rulings", prompt_id="17", part_id="17A", fmt="yaml", run_date="2026-11-05").path
            == "runs/hq/2026-11-05-17A/rulings.yml")
    assert one("answers", prompt_id="12", part_id="12B", fmt="text", **ctx).path == "runs/APP/2026-11-05-12B/answers.md"
    assert one("questions", prompt_id="03", part_id="03-draft", fmt="yaml", **ctx).note.startswith("§F2")


def test_missing_template_fields_are_reported():
    with pytest.raises(outputs.PlacementError, match="month"):
        outputs.place("letter", prompt_id="18", part_id="18", run_date="2026-11-02")
    with pytest.raises(outputs.PlacementError, match="company"):
        outputs.place("thesis", prompt_id="03", part_id="03-draft", run_date="2026-11-02")
    with pytest.raises(outputs.PlacementError, match="ext"):  # without a format, no extension is guessed
        outputs.place("rulings", prompt_id="17", part_id="17A", run_date="2026-11-02")


def test_prose_needs_a_source_tag_for_every_fact_number_and_01b_cites_only_the_dossier_s_sources():
    """MCD's first build (2026-09-28): 106 untagged numbers reached placement, and the ledger cited the dossier."""
    dossier = outputs.ParsedOutput("dossier", outputs.MARKDOWN,
                                   "---\ndoc: dossier\n---\nRevenue was $26.9B [src:MCD-10K-FY2025#p40].\n"
                                   "Margin was 46.1%.\n")
    assert outputs.prose_errors({"dossier": dossier}) == [
        "dossier: fact number '46.1%' has no [src:] tag in its sentence (00 §E1): Margin was 46.1%."]
    ledger = outputs.ParsedOutput("ledger", outputs.YAML, "entries:\n- id: M1\n  source: MCD-DOSSIER-2026-09-30#s7\n"
                                                           "- id: M2\n  source: MCD-10K-FY2025#p40\n")
    errors = outputs.cited_tag_errors("01B", {"dossier": dossier.text}, {"ledger": ledger})
    assert errors == ["ledger: source MCD-DOSSIER-2026-09-30 is not a source the dossier cites; cite the filing the "
                      "dossier cites for that fact instead (the dossier is private and cannot be cited)"]


def test_a_model_review_returns_exactly_when_a_finding_is_must_fix():
    def reply(decision, *groups):
        findings = [{"id": f"04C-{i}", "group": g} for i, g in enumerate(groups, 1)]
        return {"valuation_decision": outputs.ParsedOutput("valuation_decision", outputs.YAML, "", decision),
                "findings": outputs.ParsedOutput("findings", outputs.YAML, "", findings)}

    assert outputs.decision_errors(reply({"decision": "returned"}, "must fix", "should fix")) == []
    assert outputs.decision_errors(reply("approved", "should fix", "no change")) == []
    returned = outputs.decision_errors(reply({"decision": "returned"}, "should fix"))
    assert returned and returned[0].startswith("valuation_decision:") and "'findings'" in returned[0]
    assert "04C-2" in outputs.decision_errors(reply("approved", "should fix", "Must-fix"))[0]
    assert "approved or returned" in outputs.decision_errors(reply("maybe", "should fix"))[0]
    assert outputs.coverage_errors("04C", {}, reply("returned", "should_fix")) == returned


def test_a_valuation_tags_the_numbers_in_its_notes():
    data = {"value_ranges": {"center": 413.5, "note": "The buy range is $413.5 × 65%–75%. Revenue was $15,336m "
                                                       "[src:X-10K-FY2025#p78]."},
            "method_note": "Growth is 7% [src:X-VAL-2026-09-30] for five years.", "source": "X-10K-FY2025"}
    parsed = {"valuation_yml": outputs.ParsedOutput("valuation_yml", outputs.YAML, "", data)}
    errors = outputs.valuation_text_errors(parsed)
    assert len(errors) == 1 and errors[0].startswith("valuation_yml: value_ranges.note: fact number '$4")
    assert "X-VAL" not in errors[0] and "<TICKER>-VAL-<run date>" in errors[0]


VALUATION_RAW = """company: X
method_note: >-
  The ex-release margin is about 4.1%, which would lower the center by about $7.4 a share; Berkshire's
  float grew [src:X-10K-FY2025#p3].
value_ranges:
  note: 'The buy range is Berkshire''s center × 65%–75%.'
  center: 446.9
"""


def _valuation_output(raw=VALUATION_RAW):
    return outputs.ParsedOutput("valuation_yml", outputs.YAML, raw, outputs.load_yaml_text(raw))


def test_untagged_valuation_sentences_are_listed_for_a_tag_repair():
    """Berkshire's 01C (2026-10-02): the retry wrote the whole valuation_yml again to tag one note, and the new
    version had two other untagged figures. A tag error in the notes is now repaired sentence by sentence."""
    out = _valuation_output()
    sentences = outputs.repairable_sentences(out)
    assert sentences == ["The ex-release margin is about 4.1%, which would lower the center by about $7.4 a share;",
                         "The buy range is Berkshire's center × 65%–75%."]
    errors = outputs.valuation_text_errors({"valuation_yml": out})
    assert errors and all(outputs.is_prose_error(e, "valuation_yml") for e in errors)
    assert not outputs.is_prose_error("valuation_yml: center: 'x' is not of type 'number'", "valuation_yml")


def test_a_valuation_tag_repair_adds_tags_across_folded_lines_and_quotes_and_nothing_else():
    out = _valuation_output()
    first, second = outputs.repairable_sentences(out)
    raw = ("- find: " + repr_yaml(first) + "\n  replace: "
           + repr_yaml(first.replace("4.1%", "4.1% [src:X-VAL-2026-09-30]")) + "\n"
           "- find: " + repr_yaml(second) + "\n  replace: "
           + repr_yaml(second.replace("75%.", "75% [src:X-VAL-2026-09-30].")) + "\n")
    fixed, problems = outputs.apply_repairs("valuation_yml", out, raw)
    assert problems == [] and outputs.valuation_text_errors({"valuation_yml": fixed}) == []
    assert "4.1% [src:X-VAL-2026-09-30], which" in fixed.text and "Berkshire''s center × 65%–75% [src:X-VAL" in fixed.text
    assert fixed.data["value_ranges"]["center"] == 446.9 and fixed.data["company"] == "X"

    changed = ("- find: " + repr_yaml(first) + "\n  replace: "
               + repr_yaml(first.replace("4.1%", "4.0% [src:X-VAL-2026-09-30]")) + "\n")
    _, problems = outputs.apply_repairs("valuation_yml", out, changed)
    assert problems and "only by added [src:] tags" in problems[0]
    untagged = "- find: " + repr_yaml(first) + "\n  replace: " + repr_yaml(first) + "\n"
    assert "only by added [src:] tags" in outputs.apply_repairs("valuation_yml", out, untagged)[1][0]
    twice = _valuation_output(VALUATION_RAW + "other: 'The buy range is Berkshire''s center × 65%–75%.'\n")
    _, problems = outputs.apply_repairs("valuation_yml", twice, "- find: " + repr_yaml(second) + "\n  replace: "
                                        + repr_yaml(second.replace("75%.", "75% [src:X-VAL-2026-09-30].")) + "\n")
    assert problems and "occurs 2 times" in problems[0]


def repr_yaml(text):
    import json
    return json.dumps(text, ensure_ascii=False)
