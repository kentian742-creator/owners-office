"""模型输出的解析、校验与放置（00 §F0、§F1、§F2、§F6）。本模块不调用模型。

pipeline/llm.py 收到回答后调用 parse_reply()：把回答拆成 <output name="…"> 块，逐项按声明的格式校验，
返回 {输出名: ParsedOutput} 和错误清单；有错误时 llm.py 带着这份清单重试一次。

- 格式由提示词声明（00 front matter 的 output_formats，部分可用 formats 覆盖），调用方以 formats 传入：
  yaml、markdown、text、file（00 §F0）。
- 名字：必须是该部分 outputs 里的名字，一个名字只出现一次；必需输出不能缺（没有内容写“无”）。
- yaml：不加代码围栏，能解析，没有重复键；有 thesis-ci schema 的（OUTPUT_SCHEMAS）再按 schema 校验。
  pipeline_fields 给出的流水线维护字段（00 §G8）先写进文档再校验。
- markdown：必须以 front matter 开头，至少写 company、doc、as_of、doc_status（00 §F1；股东信和它的私有附录
  不写 company）；story.md 只按 story schema 校验 front matter。
- text：不带 front matter，不校验内容；file：放不进文本块，出现即报错。
- generated_by：注入每份 markdown 输出的 front matter，以及没有 schema 限制的 yaml 映射；
  schema 不允许多余键的（thesis、ledger、story 等）不改文档，放在 ParsedOutput.generated_by 里一并返回。

放置表 PLACEMENT 把 00 §F2 写成数据（输出名 → 仓库与路径模板），place()／place_outputs() 只回答
“这份输出该放到哪里”；写文件、开 PR 由调用方负责。表里没有的输出按 §F2 最后一行放私有仓库。
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
SCHEMAS_ENV = "OWNERS_OFFICE_SCHEMAS"  # 可把 schema 目录指到别处（测试用）；默认用已安装的 thesis-ci
DEFAULT_SCHEMAS_DIR = REPO_ROOT.parent / "thesis-ci" / "spec" / "schemas"

EMPTY_MARK = "无"  # 00 §F0：没有内容写“无”，不省略
MAX_ERRORS_PER_OUTPUT = 20

MARKDOWN, YAML, TEXT, FILE = "markdown", "yaml", "text", "file"
FORMATS = (YAML, MARKDOWN, TEXT, FILE)  # 00 §F0 的四种输出格式

# 输出名 → thesis-ci schema（spec/schemas/<名>.schema.json）。只登记完整文件；story 校验的是 front matter；
# sources_additions 是若干 sources 条目，去掉 visibility 后按 {"sources": [...]} 校验。
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
# 有 schema 的输出必须声明成对应的格式，否则 schema 校验会落空。
SCHEMA_FORMATS = {name: (MARKDOWN if name == "story" else YAML) for name in OUTPUT_SCHEMAS}

# 00 §F1：markdown 输出至少写这些 front matter 键；股东信和它的私有附录不属于某一家公司，不写 company。
FRONT_MATTER_KEYS = ("company", "doc", "as_of", "doc_status")
F1_WITHOUT_COMPANY = frozenset({"letter", "private_appendix"})
SOURCE_VISIBILITIES = ("public", "private")


class SchemaUnavailable(LookupError):
    """需要按 thesis-ci schema 校验，却找不到 schema。"""


@dataclasses.dataclass(frozen=True)
class ParsedOutput:
    """一份校验过的输出。text 是交给调用方写入的文本（Markdown 已注入 generated_by）；“无”为空输出。"""

    name: str
    format: str  # 声明的格式：yaml / markdown / text / file
    text: str
    data: Any = None  # YAML 的内容；Markdown 为 front matter
    empty: bool = False
    schema: str | None = None
    generated_by: Mapping[str, Any] | None = None
    generated_by_injected: bool = False
    pipeline_fields: tuple[str, ...] = ()  # 由流水线写入的键（00 §G8）


# ---------------------------------------------------------------- YAML


def _yaml() -> Any:
    import yaml  # PyYAML；延迟导入，保持模块轻量

    return yaml


@functools.lru_cache(maxsize=None)
def _unique_key_loader() -> type:
    """SafeLoader，但重复键报错：thesis-ci 的 lint 也把重复键当错误（YAML 只保留最后一个值）。"""
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
            except TypeError:  # 不可哈希的键由 construct_mapping 自己报错
                continue
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping", node.start_mark, f"duplicate key {key!r}", key_node.start_mark
                )
        return loader.construct_mapping(node, deep=deep)

    UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping)
    return UniqueKeyLoader


def load_yaml_text(text: str) -> Any:
    """解析一段 YAML；重复键报错。出错抛出 yaml.YAMLError。"""
    return _yaml().load(text, Loader=_unique_key_loader())


def dump_yaml(data: Any) -> str:
    return _yaml().safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)


def jsonable(obj: Any) -> Any:
    """把 YAML 数据映射到 JSON 数据模型再做 schema 校验：日期变成 ISO 字符串，键变成字符串（与 thesis-ci 一致）。"""
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
    where = f"第 {mark.line + 1} 行：" if mark is not None else ""
    return f"{where}{problem}"


def split_front_matter(text: str) -> tuple[str, str] | None:
    """(front matter, 正文)；开头不是 --- 或没有结束的 --- 时返回 None。"""
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
    """thesis-ci 的 JSON Schema 校验器。

    查找顺序：参数 schemas_dir → 环境变量 OWNERS_OFFICE_SCHEMAS → 已安装的 thesis-ci（thesis_ci.contract）
    → 兄弟检出 ../thesis-ci/spec/schemas。都找不到抛出 SchemaUnavailable。
    """
    directory = schemas_dir if schemas_dir is not None else os.environ.get(SCHEMAS_ENV) or None
    if directory is None:
        try:
            from thesis_ci import contract  # 公开 CI 按 requirements.txt 安装了 thesis-ci
        except ImportError:
            contract = None
        if contract is not None:
            try:
                return contract.validator(name)
            except (OSError, ValueError) as exc:
                raise SchemaUnavailable(f"thesis-ci 里没有 {name}.schema.json：{exc}") from exc
        directory = DEFAULT_SCHEMAS_DIR
    path = Path(directory) / f"{name}.schema.json"
    if not path.is_file():
        raise SchemaUnavailable(f"找不到 {path}；装上 thesis-ci，或用 {SCHEMAS_ENV} 指到 schema 目录")
    return _validator_from_file(str(path.resolve()))


@functools.lru_cache(maxsize=None)
def _validator_from_file(path: str) -> Any:
    import json

    import jsonschema

    schema = json.loads(Path(path).read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())


def validators_for(outputs: Iterable[str], schemas_dir: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """这些输出要用到的 schema 校验器（schema 名 → 校验器）。llm.py 在发请求之前就解析好，找不到就不花钱。"""
    names = sorted({OUTPUT_SCHEMAS[o] for o in outputs if o in OUTPUT_SCHEMAS})
    return {name: schema_validator(name, schemas_dir) for name in names}


def format_problems(formats: Mapping[str, str]) -> list[str]:
    """格式声明本身的问题：不认识的格式；有 schema 的输出声明成了别的格式。"""
    problems = [f"{name} 的格式 {fmt!r} 不是 {'、'.join(FORMATS)} 之一" for name, fmt in formats.items() if fmt not in FORMATS]
    problems += [
        f"{name} 有 thesis-ci schema（{OUTPUT_SCHEMAS[name]}），格式应当是 {SCHEMA_FORMATS[name]}，声明的是 {fmt}"
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


# ---------------------------------------------------------------- 封装


_OPEN = re.compile(r"<output\s+name\s*=\s*([\"'])([^\"']*)\1\s*>")
_CLOSE = re.compile(r"</output\s*>")


def split_envelope(reply: str) -> tuple[list[tuple[str, str]], list[str]]:
    """[(输出名, 块内文本)] 与封装错误。块内文本去掉首尾的空行，非空的以一个换行结尾（便于直接写文件）。"""
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
            errors.append(f"输出 {name!r} 没有用 </output> 结束")
            pos = opening.end()
            continue
        body = reply[opening.end() : closing.start()].strip("\r\n").rstrip()
        blocks.append((name, body + "\n" if body else ""))
        pos = closing.end()
    if not blocks and not errors:
        errors.append('回答里没有 <output name="…"> 块；每个输出放进一个块（00 §F0）')
    return blocks, errors


def parse_reply(
    reply: str,
    declared: Sequence[tuple[str, bool]],
    *,
    formats: Mapping[str, str],
    generated_by: Mapping[str, Any],
    validators: Mapping[str, Any] | None = None,
    pipeline_fields: Mapping[str, Mapping[str, Any]] | None = None,
    where: str = "本部分",
) -> tuple[dict[str, ParsedOutput], list[str]]:
    """拆开并校验一次回答。declared 是该部分的 (输出名, 是否必需)，formats 是每个输出声明的格式；
    validators 为 None 时按需解析 thesis-ci schema。返回 ({输出名: ParsedOutput}, 错误清单)。"""
    names = [name for name, _ in declared]
    undeclared = [name for name in names if name not in formats]
    if undeclared:
        raise ValueError(f"这些输出没有声明格式：{'、'.join(undeclared)}（00 §F0 的 output_formats）")
    if validators is None:
        validators = validators_for(names)
    blocks, errors = split_envelope(reply)
    outputs: dict[str, ParsedOutput] = {}
    seen: set[str] = set()
    for name, raw in blocks:
        if name not in names:
            errors.append(f"输出 {name!r} 不在{where}的输出表里（可用：{'、'.join(names)}）")
            continue
        if name in seen:
            errors.append(f"输出 {name!r} 出现了不止一次")
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
            errors.append(f"缺少输出 {name!r}；没有内容时写“{EMPTY_MARK}”，不能省略（00 §F0）")
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
    """按声明的格式 fmt 校验一份输出。返回 (ParsedOutput 或 None, 错误)。有 schema 的输出必须在 validators 里有校验器。"""
    if fmt not in FORMATS:
        raise ValueError(f"{name} 的格式 {fmt!r} 不是 {'、'.join(FORMATS)} 之一")
    schema = OUTPUT_SCHEMAS.get(name)
    if schema is not None and schema not in validators:
        raise SchemaUnavailable(f"{name} 要按 {schema}.schema.json 校验，但没有拿到校验器（见 validators_for）")
    if raw.strip() == EMPTY_MARK:
        return ParsedOutput(name, fmt, "", None, empty=True, schema=schema), []
    if not raw.strip():
        return None, [f"{name}：输出是空的；没有内容时写“{EMPTY_MARK}”（00 §F0）"]
    if fmt == FILE:
        return None, [f"{name}：这是文件（PDF、页面图像），放不进 <output> 文本块"]
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
        return None, [f"{name}：YAML 不加代码围栏（00 §F0）"]
    try:
        data = load_yaml_text(raw)
    except _yaml().YAMLError as exc:
        return None, [f"{name}：YAML 解析失败：{_yaml_problem(exc)}"]
    if data is None:
        return None, [f"{name}：YAML 是空的；没有内容时写“{EMPTY_MARK}”（00 §F0）"]
    text = raw
    applied: tuple[str, ...] = ()
    if fields:
        if not isinstance(data, dict):
            return None, [f"{name}：应当是一个映射（流水线要写入 {'、'.join(fields)}）"]
        text, data = apply_fields(text, data, fields)
        applied = tuple(fields)
    errors: list[str] = []
    validator = validators.get(schema) if schema else None
    if schema == "sources":
        instance, errors = _sources_instance(name, data)
    else:
        instance = data
    if validator is not None and not errors:
        errors = [f"{name}：{e}" for e in _cap(schema_errors(validator, instance, schema))]
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
        return None, [f"{name}：Markdown 输出要以 front matter 开头（--- … ---，00 §F0、§F1）"]
    fm_text, body = parts
    try:
        front = load_yaml_text(fm_text) if fm_text.strip() else {}
    except _yaml().YAMLError as exc:
        return None, [f"{name}：front matter 解析失败：{_yaml_problem(exc)}"]
    if not isinstance(front, dict):
        return None, [f"{name}：front matter 应当是一个映射"]
    front = dict(front)
    applied: tuple[str, ...] = ()
    if fields:
        front.update(fields)
        applied = tuple(fields)
    errors = []
    if schema is None:  # story.md 是 §F1 的例外：只用 story schema 的字段
        keys = [k for k in FRONT_MATTER_KEYS if not (k == "company" and name in F1_WITHOUT_COMPANY)]
        errors += [f"{name}：front matter 缺少 {key}（00 §F1）" for key in keys if key not in front]
    validator = validators.get(schema) if schema else None
    if validator is not None:
        errors += [f"{name}：front matter {e}" for e in _cap(schema_errors(validator, front, schema))]
    if errors:
        return None, errors
    injected = not (validator is not None and rejects_extra_keys(validator))
    if injected:
        front["generated_by"] = dict(generated_by)
    text = raw if not (injected or applied) else f"---\n{dump_yaml(front)}---\n{body}"
    return ParsedOutput(name, MARKDOWN, text, front, schema=schema, generated_by=generated_by,
                        generated_by_injected=injected, pipeline_fields=applied), []


def _cap(errors: list[str]) -> list[str]:
    if len(errors) <= MAX_ERRORS_PER_OUTPUT:
        return errors
    return errors[:MAX_ERRORS_PER_OUTPUT] + [f"……另有 {len(errors) - MAX_ERRORS_PER_OUTPUT} 条同类错误"]


def _sources_instance(name: str, data: Any) -> tuple[Any, list[str]]:
    entries = data.get("sources") if isinstance(data, dict) and set(data) == {"sources"} else data
    if not isinstance(entries, list):
        return None, [f"{name}：应当是 sources 条目的列表，每条注明 visibility（00 §F2）"]
    errors = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict) or entry.get("visibility") not in SOURCE_VISIBILITIES:
            errors.append(f"{name}[{i}]：visibility 必须是 public 或 private（决定写进哪个仓库的 sources.yml）")
    return {"sources": [_without_visibility(e) for e in entries]}, errors


def _without_visibility(entry: Any) -> Any:
    return {k: v for k, v in entry.items() if k != "visibility"} if isinstance(entry, dict) else entry


def split_sources_additions(data: Any) -> dict[str, list[dict[str, Any]]]:
    """按 visibility 把 sources_additions 分给两个仓库，并去掉 visibility（sources schema 不接受这个键）。"""
    entries = data.get("sources") if isinstance(data, dict) and set(data) == {"sources"} else data
    out: dict[str, list[dict[str, Any]]] = {v: [] for v in SOURCE_VISIBILITIES}
    for entry in entries or []:
        if not isinstance(entry, dict) or entry.get("visibility") not in SOURCE_VISIBILITIES:
            raise ValueError(f"sources_additions 条目缺少 visibility：{entry!r}")
        out[entry["visibility"]].append(_without_visibility(entry))
    return out


def _append_generated_by(text: str, data: dict, generated_by: Mapping[str, Any]) -> tuple[str, dict, bool]:
    """在 YAML 映射末尾追加 generated_by。追加后解析结果不对（例如流式映射）就不改，改为一并返回。"""
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
    """把流水线维护的顶层字段（00 §G8）写进 YAML 文本：已有的键原地替换，没有的加在开头的注释之后。

    尽量保留模型写的注释与格式；改完解析结果与预期不一致时，退回整份重新输出（注释会丢）。
    """
    merged = {**data, **fields}
    lines = text.split("\n")
    missing = []
    for key, value in fields.items():
        block = dump_yaml({key: value}).rstrip("\n").split("\n")
        start = next((i for i, line in enumerate(lines) if _top_key(line) == key), None)
        if start is None:
            missing.extend(block)
            continue
        end = start + 1
        while end < len(lines) and _top_key(lines[end]) is None and lines[end].strip() not in ("---", "..."):
            end += 1
        while end - 1 > start and (not lines[end - 1].strip() or lines[end - 1].startswith("#")):
            end -= 1  # 块后的空行和顶格注释属于下一段
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


# ---------------------------------------------------------------- 放置（00 §F2）


PUBLIC, PRIVATE, SPLIT = "public", "private", "split"
REPOS = {PUBLIC: "owners-office", PRIVATE: "owners-office-private"}
PUBLIC_DOSSIERS = frozenset({"MSFT"})  # §F2：MSFT 的 dossier.md 公开（第 8 部分不含数字）
UPDATE_SUBJECTS = frozenset({"季度更新", "update"})  # 04 的 {{被审对象}} 是季度更新时，04A 的 findings 随 PR 公开


@dataclasses.dataclass(frozen=True)
class Destination:
    """放置表的一行。path 是相对仓库根的模板；None 表示不是文件（PR 正文、PR 附件）。"""

    visibility: str  # public | private | split（sources_additions：按每条的 visibility 分给两个仓库）
    path: str | None
    action: str = "write"  # write | append | patch | merge | front_matter | pr_body | pr_attachment
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
# 输出名 → 候选去处，第一条 when 全部满足的生效；都不满足时用 DEFAULT_DESTINATION。
# 模板字段：company、period、run_date、month、doc、slug、scope、part_id、output、ext（见 place()）。
PLACEMENT: dict[str, tuple[Destination, ...]] = {
    # 公开仓库（§F2 第一行；路径按 thesis-ci SPEC §2）
    "thesis": (_d(PUBLIC, "companies/{company}/thesis.yml"),),
    "story": (_d(PUBLIC, "companies/{company}/story.md"),),
    "ledger": (_d(PUBLIC, "companies/{company}/ledger.yml"),),
    "prereg": (_d(PUBLIC, "companies/{company}/prereg/{period}.yml", note="条目文件；时间戳与结算另成文件（SPEC §2.1）"),),
    "update": (_d(PUBLIC, "companies/{company}/updates/{run_date}.md"),),
    "reviewed_sections": (
        _d(PUBLIC, "companies/{company}/updates/{run_date}.md", "front_matter",
           "写进更新记录 front matter 的 reviewed_sections（SPEC §4.2）"),
    ),
    "pr_body": (_d(PUBLIC, None, "pr_body", "审计意见、分歧与反向清单由流水线追加在后面"),),
    "mistakes_entry": (_d(PUBLIC, "mistakes.md", "append"),),
    "letter": (_d(PUBLIC, "letters/{month}.md"),),
    # 档案：MSFT 公开（§F2 第二行），其余公司私有（§F2 第四行）
    "dossier": (
        _d(PUBLIC, "companies/{company}/dossier.md", company=PUBLIC_DOSSIERS),
        _d(PRIVATE, "companies/{company}/dossier.md"),
    ),
    "dossier_changes": (
        _d(PUBLIC, "companies/{company}/dossier.md", "patch", "逐部分替换档案正文", company=PUBLIC_DOSSIERS),
        _d(PRIVATE, "companies/{company}/dossier.md", "patch", "逐部分替换档案正文"),
    ),
    # 季度更新 PR 的附件：随 PR 公开，遵守 §H4（§F2 第三行）；其余情形的同名输出按默认放私有仓库
    "findings": (_d(PUBLIC, None, "pr_attachment", part_id={"04A"}, subject=UPDATE_SUBJECTS),),
    "inversion_list": (_d(PUBLIC, None, "pr_attachment", part_id={"04B-lite"}),),
    "divergence_map": (_d(PUBLIC, None, "pr_attachment", part_id={"14B"}),),
    "qualitative_verdicts": (_d(PUBLIC, None, "pr_attachment", part_id={"14T"}),),
    # 来源：按每条的 visibility 并入两边的 sources.yml，写入前去掉 visibility（split_sources_additions）
    "sources_additions": (_d(SPLIT, "companies/{company}/sources.yml", "merge", "按每条的 visibility 分开并入"),),
    # 私有仓库（§F2 第四行；路径按 SPEC §2、§7）
    "valuation_yml": (
        _d(PRIVATE, "companies/{company}/valuation.yml", note="doc_status: proposed 的待审版本经 04C 批准才生效（§V20）"),
    ),
    "valuation_md": (_d(PRIVATE, "companies/{company}/valuation.md"),),
    "escalation": (_d(PRIVATE, "escalations/{run_date}-{company}-{slug}.yml"),),
    "memo": (_d(PRIVATE, "memos/{run_date}-{company}-{slug}.yml"),),
    "ranking": (_d(PRIVATE, "hq/ranking.yml"),),
    "private_appendix": (_d(PRIVATE, "letters/{month}-private-appendix.md"),),
    # reports/：02、05、06–08、10、11、13 的正文、版面意图与连接清单，19 的 PDF 与页面图像
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
DEFAULT_DESTINATION = _d(PRIVATE, "runs/{scope}/{run_date}-{part_id}/{output}.{ext}", note="§F2：表里没有列出的输出放私有仓库")
TRUST_ROUTED_PROMPTS = frozenset({"03"})  # §G9：季度更新按信任等级分流


class PlacementError(ValueError):
    """路径模板缺少字段。"""


def place(output: str, *, prompt_id: str, part_id: str, fmt: str | None = None, **context: Any) -> list[Placement]:
    """一份输出该放到哪里（00 §F2）。sources_additions 返回两条（公开、私有），其余一条。

    fmt 是输出声明的格式，决定路径里 {ext} 的扩展名（yaml 用 yml，其余 md）；路径要用 {ext} 而没给 fmt 时报错。
    context 给模板字段：company（代码）、period（FY<年>Q<季>）、run_date（YYYY-MM-DD）、month（YYYY-MM，股东信）、
    doc（02／06／07／08／11，报告目录）、subject（04 的被审对象）、slug（备忘录与升级请求的文件名，默认取输出名）。
    模板需要的字段缺了抛出 PlacementError。
    """
    ctx = {k: v for k, v in context.items() if v is not None}
    ctx.update(output=output, part_id=part_id, prompt_id=prompt_id)
    ctx.setdefault("slug", output)
    ctx.setdefault("scope", ctx.get("company") or "hq")
    if fmt is not None:
        ctx.setdefault("ext", "yml" if fmt == YAML else "md")
    dest = next(
        (d for d in PLACEMENT.get(output, ()) if all(str(ctx.get(k)) in allowed for k, allowed in d.when.items())),
        DEFAULT_DESTINATION,
    )
    path = None
    if dest.path is not None:
        needed = {field for _, field, _, _ in string.Formatter().parse(dest.path) if field}
        missing = sorted(needed - set(ctx))
        if missing:
            raise PlacementError(f"{output} 的去处 {dest.path} 需要 {', '.join(missing)}")
        path = dest.path.format_map(ctx)
    visibilities = (PUBLIC, PRIVATE) if dest.visibility == SPLIT else (dest.visibility,)
    return [Placement(output, REPOS[v], v, path, dest.action, _note(dest.note, prompt_id, v)) for v in visibilities]


def _note(note: str, prompt_id: str, visibility: str) -> str:
    if prompt_id in TRUST_ROUTED_PROMPTS and visibility == PUBLIC:
        return "；".join(filter(None, [note, "§G9：按信任等级分流，1 级及以下先留私有仓库，经总部复核后公开"]))
    return note


def place_outputs(
    outputs: Mapping[str, ParsedOutput], *, prompt_id: str, part_id: str, **context: Any
) -> list[Placement]:
    """一次调用的全部非空输出各该放到哪里。"""
    placements: list[Placement] = []
    for name, parsed in outputs.items():
        if parsed.empty:
            continue
        placements.extend(place(name, prompt_id=prompt_id, part_id=part_id, fmt=parsed.format, **context))
    return placements
