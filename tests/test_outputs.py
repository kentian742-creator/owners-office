"""pipeline/outputs.py 的测试：输出封装、校验、generated_by 与放置（00 §F0、§F1、§F2）。"""

from __future__ import annotations

import json

import pytest

from pipeline import outputs
from tests.llm_fixtures import SCHEMAS

GENERATED_BY = {"model": "claude-sonnet-5", "rules_version": "9.0", "prompt": "03", "prompt_version": "3.1",
                "input_sha256": "0" * 64}
# 测试里用到的输出名与格式（真实的声明在私有仓库 00 的 front matter：output_formats）
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


# ---------------------------------------------------------------- 封装


def test_envelope_blocks_are_split_by_name():
    blocks, errors = outputs.split_envelope("前言\n<output name='a'>\n一\n</output>\n<output name=\"b\">二</output>")
    assert blocks == [("a", "一\n"), ("b", "二\n")]
    assert errors == []


def test_unclosed_block_and_missing_envelope_are_errors():
    _, errors = outputs.split_envelope('<output name="a">\n一\n<output name="b">二</output>')
    assert errors == ["输出 'a' 没有用 </output> 结束"]
    _, errors = outputs.split_envelope("只有正文")
    assert "没有 <output" in errors[0]


def test_names_must_match_the_declared_outputs(schemas):
    reply = '<output name="findings">- a</output><output name="findings">- b</output><output name="extra">x</output>'
    parsed, errors = parse(reply, [("findings", True), ("questions", True), ("notes", False)], schemas)
    assert "输出 'findings' 出现了不止一次" in errors
    assert any(e.startswith("输出 'extra' 不在本部分的输出表里") for e in errors)
    assert any(e.startswith("缺少输出 'questions'") for e in errors)
    assert not any(e.startswith("缺少输出 'notes'") for e in errors)  # 可选输出可以省略


def test_every_declared_output_needs_a_declared_format(schemas):
    with pytest.raises(ValueError, match="没有声明格式"):
        outputs.parse_reply('<output name="findings">- a</output>', [("findings", True)], formats={},
                            generated_by=GENERATED_BY, validators={})
    with pytest.raises(ValueError, match="格式"):
        outputs.parse_output("findings", "- a", fmt="auto", generated_by=GENERATED_BY, validators={})


def test_format_declarations_must_suit_the_schemas():
    assert outputs.format_problems({"thesis": "yaml", "story": "markdown", "reply": "text"}) == []
    assert outputs.format_problems({"thesis": "text", "story": "yaml", "reply": "prose"}) == [
        "reply 的格式 'prose' 不是 yaml、markdown、text、file 之一",
        "thesis 有 thesis-ci schema（thesis），格式应当是 yaml，声明的是 text",
        "story 有 thesis-ci schema（story），格式应当是 markdown，声明的是 yaml",
    ]


def test_empty_mark_means_empty(schemas):
    parsed, errors = parse('<output name="thesis">\n无\n</output>', [("thesis", True)], schemas)
    assert errors == []
    assert parsed["thesis"].empty and parsed["thesis"].text == "" and parsed["thesis"].data is None


# ---------------------------------------------------------------- YAML


def test_yaml_problems_are_reported(schemas):
    declared = [("findings", True), ("questions", True), ("layout_instructions", True)]
    reply = (
        '<output name="findings">```yaml\n- a\n```</output>'
        '<output name="questions">id: 1\nid: 2</output>'
        '<output name="layout_instructions"># 只有注释</output>'
    )
    _, errors = parse(reply, declared, schemas)
    assert "findings：YAML 不加代码围栏（00 §F0）" in errors
    assert any(e.startswith("questions：YAML 解析失败：") and "duplicate key 'id'" in e for e in errors)
    assert any(e.startswith("layout_instructions：YAML 是空的") for e in errors)


def test_schema_errors_name_the_output_and_the_path(schemas):
    reply = '<output name="thesis">company: TEST\ntrust_level: high\ntests: []\nextra: 1</output>'
    _, errors = parse(reply, [("thesis", True)], schemas)
    assert "thesis：thesis.schema.json /: Additional properties are not allowed ('extra' was unexpected)" in errors
    assert "thesis：thesis.schema.json /trust_level: 'high' is not of type 'integer'" in errors


def test_a_schema_output_is_never_left_unvalidated():
    with pytest.raises(outputs.SchemaUnavailable):
        outputs.parse_output("thesis", "company: TEST", fmt="yaml", generated_by=GENERATED_BY, validators={})
    with pytest.raises(outputs.SchemaUnavailable):
        outputs.validators_for(["thesis"], "/nonexistent/schemas")


def test_sources_additions_need_visibility_and_match_the_sources_schema(schemas):
    good = "- {tag: T-10K-FY2025, kind: filing, title: 年报, visibility: public}\n- {tag: T-RPT1-2026-09-23, kind: report, title: 报告, visibility: private}"
    parsed, errors = parse(f'<output name="sources_additions">{good}</output>', [("sources_additions", True)], schemas)
    assert errors == []
    split = outputs.split_sources_additions(parsed["sources_additions"].data)
    assert [e["tag"] for e in split["public"]] == ["T-10K-FY2025"]
    assert [e["tag"] for e in split["private"]] == ["T-RPT1-2026-09-23"]
    assert all("visibility" not in e for e in split["public"] + split["private"])

    bad = "- {tag: T-1, kind: filing, title: x}\n- {tag: T-2, title: y, visibility: public}"
    _, errors = parse(f'<output name="sources_additions">{bad}</output>', [("sources_additions", True)], schemas)
    assert errors == ["sources_additions[0]：visibility 必须是 public 或 private（决定写进哪个仓库的 sources.yml）"]


def test_pipeline_fields_replace_nested_blocks_and_keep_comments():
    text = (
        "# 文件头注释\n"
        "schema_version: \"0.1\"\n"
        "company: TEST\n"
        "filer:\n  cik: \"0000000001\"\n  type: domestic\n"
        "\n# 下一段的注释\n"
        "category: stalwart\n"
    )
    data = outputs.load_yaml_text(text)
    new_text, merged = outputs.apply_fields(text, data, {"schema_version": "0.2", "filer": {"type": "foreign"}, "trust_level": 1})
    assert merged == {"schema_version": "0.2", "company": "TEST", "filer": {"type": "foreign"}, "category": "stalwart",
                      "trust_level": 1}
    assert outputs.load_yaml_text(new_text) == merged
    assert new_text.startswith("# 文件头注释\ntrust_level: 1\nschema_version: '0.2'\n")
    assert "\n# 下一段的注释\ncategory: stalwart\n" in new_text
    assert "0000000001" not in new_text


def test_pipeline_fields_fall_back_to_a_full_dump_when_text_cannot_be_patched():
    text = "{company: TEST, trust_level: 3}"
    new_text, merged = outputs.apply_fields(text, outputs.load_yaml_text(text), {"trust_level": 1})
    assert outputs.load_yaml_text(new_text) == merged == {"company": "TEST", "trust_level": 1}


# ---------------------------------------------------------------- Markdown 与 generated_by


def test_f1_front_matter_keys_are_required(schemas):
    letter = "---\ndoc: letter\nas_of: 2026-10-01\ndoc_status: draft\n---\n信。\n"
    appendix = "---\ndoc: private_appendix\nas_of: 2026-10-01\ndoc_status: draft\n---\n附录。\n"
    dossier = "---\ndoc: dossier\nas_of: 2026-10-01\ndoc_status: draft\n---\n档案。\n"
    parsed, errors = parse(
        f'<output name="letter">{letter}</output><output name="private_appendix">{appendix}</output>'
        f'<output name="dossier">{dossier}</output>',
        [("letter", True), ("private_appendix", True), ("dossier", True)],
        schemas,
    )
    assert errors == ["dossier：front matter 缺少 company（00 §F1）"]  # 股东信和私有附录不属于某家公司
    assert parsed["letter"].data["generated_by"] == GENERATED_BY
    assert parsed["letter"].text.startswith("---\ndoc: letter\n") and parsed["letter"].text.endswith("---\n信。\n")
    assert parsed["private_appendix"].generated_by_injected


def test_story_front_matter_is_validated_and_left_alone(schemas):
    story = "---\ncompany: TEST\nas_of: 2026-09-24\nstatus: sold\n---\n故事。\n"
    _, errors = parse(f'<output name="story">{story}</output>', [("story", True)], schemas)
    assert errors == ["story：front matter story.schema.json /status: 'sold' is not one of ['holding', 'candidate', 'archive']"]


def test_text_outputs_are_left_alone_and_yaml_outputs_are_strict(schemas):
    reply = (
        '<output name="reply">Summary: 不写备忘录，理由见下。</output>'
        '<output name="bear_case">---\n分隔线开头的正文\n---\n正文</output>'
        '<output name="gate_decision">决定：放行。理由见 questions。</output>'
    )
    parsed, errors = parse(reply, [("reply", True), ("bear_case", True), ("gate_decision", True)], schemas)
    assert parsed["reply"].text == "Summary: 不写备忘录，理由见下。\n"  # text：像 YAML 也不改
    assert parsed["bear_case"].format == "text" and not parsed["bear_case"].generated_by_injected
    assert errors == []  # 一句话也是合法的 YAML 标量
    assert parsed["gate_decision"].data == "决定：放行。理由见 questions。"
    _, errors = parse('<output name="gate_decision">decision: 放行\n  reason: [未闭合</output>',
                      [("gate_decision", True)], schemas)
    assert errors and errors[0].startswith("gate_decision：YAML 解析失败")


def test_generated_by_is_appended_to_schemaless_mappings_only_when_it_round_trips(schemas):
    mapping = "as_of: 2026-09-24\nverdicts: []\n"
    flow = "{as_of: 2026-09-24, verdicts: []}\n...\n"
    parsed, errors = parse(
        f'<output name="fact_verdicts">{mapping}</output><output name="model_checks">{flow}</output>',
        [("fact_verdicts", True), ("model_checks", True)],
        schemas,
    )
    assert errors == []
    assert parsed["fact_verdicts"].generated_by_injected
    assert parsed["fact_verdicts"].text.startswith(mapping)
    assert not parsed["model_checks"].generated_by_injected


def test_file_outputs_cannot_travel_in_text(schemas):
    _, errors = parse('<output name="pdf">%PDF-1.7</output>', [("pdf", True)], schemas)
    assert errors == ["pdf：这是文件（PDF、页面图像），放不进 <output> 文本块"]


def test_real_thesis_ci_story_schema_is_used_by_default(monkeypatch):
    pytest.importorskip("thesis_ci")
    monkeypatch.delenv(outputs.SCHEMAS_ENV, raising=False)
    validators = outputs.validators_for(["story"])
    good = "---\ncompany: TEST\nas_of: 2026-09-24\nstatus: holding\n---\n故事。\n"
    parsed, errors = outputs.parse_reply(f'<output name="story">{good}</output>', [("story", True)],
                                         formats={"story": "markdown"}, generated_by=GENERATED_BY,
                                         validators=validators)
    assert errors == []
    assert parsed["story"].generated_by == GENERATED_BY and not parsed["story"].generated_by_injected


# ---------------------------------------------------------------- 放置（00 §F2）


def one(output, **kwargs):
    (placement,) = outputs.place(output, **kwargs)
    return placement


def test_public_files_follow_the_spec_layout():
    ctx = dict(prompt_id="01", part_id="01B", company="APP", run_date="2026-11-05", period="FY2026Q3")
    assert one("thesis", **ctx) == outputs.Placement("thesis", "owners-office", "public", "companies/APP/thesis.yml", "write")
    assert one("story", **ctx).path == "companies/APP/story.md"
    assert one("prereg", **{**ctx, "prompt_id": "15", "part_id": "15A"}).path == "companies/APP/prereg/FY2026Q3.yml"
    assert one("mistakes_entry", **ctx).action == "append"
    assert one("pr_body", **ctx).path is None


def test_dossier_is_public_only_for_msft():
    ctx = dict(prompt_id="01", part_id="01A", run_date="2026-11-05")
    assert one("dossier", company="MSFT", **ctx).visibility == "public"
    assert one("dossier", company="APP", **ctx).repo == "owners-office-private"


def test_pr_attachments_depend_on_the_part():
    ctx = dict(company="APP", run_date="2026-11-05")
    assert one("findings", prompt_id="04", part_id="04A", subject="季度更新", **ctx).action == "pr_attachment"
    private = one("findings", prompt_id="04", part_id="04A", subject="研报", fmt="yaml", **ctx)
    assert (private.visibility, private.path) == ("private", "runs/APP/2026-11-05-04A/findings.yml")
    assert one("inversion_list", prompt_id="04", part_id="04B-lite", **ctx).visibility == "public"
    assert one("inversion_list", prompt_id="04", part_id="04B", fmt="yaml", **ctx).visibility == "private"


def test_sources_additions_go_to_both_repositories():
    placements = outputs.place("sources_additions", prompt_id="03", part_id="03-draft", company="APP", run_date="2026-11-05")
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
    assert one("rulings", prompt_id="17", part_id="17A", fmt="yaml", run_date="2026-11-05").path == "runs/hq/2026-11-05-17A/rulings.yml"
    assert one("answers", prompt_id="12", part_id="12B", fmt="text", **ctx).path == "runs/APP/2026-11-05-12B/answers.md"
    assert one("questions", prompt_id="03", part_id="03-draft", fmt="yaml", **ctx).note.startswith("§F2")


def test_missing_template_fields_are_reported():
    with pytest.raises(outputs.PlacementError, match="month"):
        outputs.place("letter", prompt_id="18", part_id="18", run_date="2026-11-02")
    with pytest.raises(outputs.PlacementError, match="company"):
        outputs.place("thesis", prompt_id="03", part_id="03-draft", run_date="2026-11-02")
    with pytest.raises(outputs.PlacementError, match="ext"):  # 不知道格式就不猜扩展名
        outputs.place("rulings", prompt_id="17", part_id="17A", run_date="2026-11-02")
