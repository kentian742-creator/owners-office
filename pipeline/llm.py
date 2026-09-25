"""The single model-call entry point: the only module allowed to call models.

Hard rule: model calls live only in pipeline/llm.py and record the model name, the prompt version and the input hash.
Any other .py file that imports a model SDK is stopped by the CI check C-LLM-ENTRY.

One complete() call runs one part of one prompt under prompt set v3 (prompts/ in the private repository,
docs/decisions/0009):

1. The role table has one source only: agents/*.yml (model, effort, fallbacks, prompts, can_see, cannot_see). An
   unknown role, a prompt or part the role has not registered, or a front matter role that differs from the calling
   role is an error raised before any request is sent.
2. A prompt is found by its id as <id>-*.md in the prompts directory. The directory comes from the prompts_dir
   argument, else the environment variable OWNERS_OFFICE_PROMPTS; the default is the sibling checkout
   ../owners-office-private/prompts. The front matter's parts give each part's role, inputs and outputs. A
   multi-pass part (inputs_passN / outputs_passN; calls gives the number of passes) takes this pass's inputs and
   outputs by pass_no, and a prompt with modes takes this mode's inputs and outputs by mode; the declared lists are
   always enforced strictly. A part with role: pipeline (12V) is deterministic code and does not call a model.
   Each output's format comes from output_formats in the 00 front matter (overridden by the prompt's or part's
   formats); without a declared format no request is sent.
3. System prompt = 00 + 00D (if the front matter of the prompt or of this part has design) + the specific prompt;
   {{variables}} are filled from variables.
4. Input: one user message. Its first line, <run .../>, says which part, which pass and which mode is running; then
   comes one <input name="..."> block per input, named exactly as in the part's inputs, in front matter order. A
   missing required input, an undeclared input, or an input outside the role's can_see or inside its cannot_see
   (00 §G6) is refused before any request is sent. Page images (rendered_pages) go into their <input> block as
   base64 image blocks.
5. Output: split at <output name="...">, validated against the declared formats (pipeline/outputs.py: names,
   required outputs, YAML, thesis-ci schema, front matter). If it is invalid, the errors are appended to the
   original request, which is retried once; if the output is still invalid, LLMOutputInvalid is raised and nothing
   is handed over. generated_by is injected into the front matter of every Markdown output; placement follows 00 §F2
   (LLMResult.placements()).
6. Requests: streaming by default (client.messages.stream(...).get_final_message()); the output limit is 128000 for
   01, 02 and 11 and 64000 for the rest. Drafting and oversight models both use adaptive thinking
   (thinking={"type": "adaptive"}) and budget_tokens is never sent; depth is set with output_config.effort.
   Oversight roles go through the beta API with server-side refusal fallback turned on (docs/decisions/0003).
   The system prompt blocks of 00 (and 00D) carry a 5-minute cache breakpoint, with the specific prompt after the
   breakpoint; cache-write and cache-read tokens are charged at their own prices from CACHE_PRICES_PER_MTOK
   (docs/decisions/0015).
7. Budget guard: before every request (retries included), sum cost_usd in the log for the current UTC calendar
   month; once it reaches the monthly budget (budget.monthly_usd in constitution/decision-rights.yml), raise
   BudgetExceeded and do not send the request.
8. Logging: every request sent (retries, refusals and errors included) appends one JSON line to
   logs/llm-calls.jsonl: the model name; prompt_version / rules_version / design_version, the front matter
   versions of the prompt, 00 and 00D; *_revision, the file revision (the last git commit that changed the file;
   "sha256:<content hash>+dirty" when it is uncommitted or modified); the part id, part_id; input_sha256, the
   sha256 of the canonical JSON of {prompt_id, prompt (the full system prompt), inputs, run}; usage, which includes
   the cache-write and cache-read token counts; and cost_breakdown, split into input, output, cache write and cache
   read.

The SDK is imported lazily inside a function: without the SDK installed, this module can still be imported and
tested. The SDK reads credentials from the environment (from GitHub Secrets in CI); keys never appear in the code.
"""

from __future__ import annotations

import base64
import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import outputs as _outputs
from .outputs import ParsedOutput, Placement

REPO_ROOT = Path(__file__).resolve().parents[1]
SDK_MODULE = "anthropic"  # imported only in _default_client()

PROMPTS_ENV = "OWNERS_OFFICE_PROMPTS"  # prompts directory; default: sibling checkout ../owners-office-private/prompts
PRIVATE_REPO_NAME = "owners-office-private"
RULES_ID = "00"  # series rules, placed first in every system prompt
DESIGN_ID = "00D"  # design system, only for prompts whose front matter has design

MAX_TOKENS = 16000  # limit when not streaming
STREAM_MAX_TOKENS = 64000  # default limit for streaming calls: one v3 output often holds several complete files
LONG_OUTPUT_PROMPTS = frozenset({"01", "02", "11"})  # prompts README: their output is very long
LONG_MAX_TOKENS = 128000
LOG_ENV = "OWNERS_OFFICE_LLM_LOG"  # can point the log at a persistent location (e.g. the private repository in CI)
DEFAULT_LOG_PATH = REPO_ROOT / "logs" / "llm-calls.jsonl"
DEFAULT_MONTHLY_BUDGET_USD = 20.0  # DESIGN.md's default; decision-rights.yml takes precedence

DEFAULT_EFFORT = "high"
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

# USD per million tokens: (input, output). Checked on 2026-09-24 against Anthropic's official prices; see
# docs/decisions/0003. A model not in the table cannot be called: its cost could not be recorded, so the budget
# guard would not work.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-sonnet-5": (2.0, 10.0),
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
# Prompt caching, USD per million tokens: (5-minute cache write, cache read). The pipeline uses only the default
# 5-minute cache (CACHE_CONTROL).
# Source: Anthropic's prompt caching prices (the prompt caching section of the Claude API docs, checked 2026-09-25):
# a 5-minute cache write costs 1.25 times the input price and a cache read 0.1 times; the cache read price of
# claude-fable-5-1 is listed separately as 0.25 (i.e. 0.025 times). Except for that entry, the numbers in the table
# are computed from PRICES_PER_MTOK with these two multipliers and have not been checked model by model; when prices
# change, edit this table (tests/test_llm.py checks that it agrees with the multipliers).
# A model not in this table cannot be called either.
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.1
CACHE_PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-sonnet-5": (2.5, 0.2),
    "claude-fable-5-1": (12.5, 0.25),
    "claude-opus-5": (6.25, 0.5),
    "claude-opus-4-8": (6.25, 0.5),
    "claude-haiku-4-5": (1.25, 0.1),
}
# The system prompt blocks of 00 (and 00D) each get a cache breakpoint: they are the same for every call. The
# specific prompt goes after the breakpoints.
CACHE_CONTROL = {"type": "ephemeral"}

# Server-side refusal fallback (beta). The scalar "default" uses -2026-07-01; the array form that names models
# uses -2026-06-01.
FALLBACK_BETA_DEFAULT = "server-side-fallback-2026-07-01"
FALLBACK_BETA_ARRAY = "server-side-fallback-2026-06-01"
SERVER_FALLBACK_MODELS = frozenset({"claude-fable-5-1", "claude-opus-5"})
FALLBACKS_OFF = frozenset({"", "none", "off", "false", "no"})

# Thinking: these models are sent adaptive thinking explicitly; budget_tokens is never sent (these models reject it
# with a 400). Models outside this set (claude-haiku-4-5, used for the budget downgrade) do not support adaptive
# thinking; thinking is not sent for them.
ADAPTIVE_THINKING_MODELS = frozenset({"claude-sonnet-5", "claude-fable-5-1", "claude-opus-5", "claude-opus-4-8"})
# Models that do not accept the effort parameter.
NO_EFFORT_MODELS = frozenset({"claude-haiku-4-5"})

# Page images: only these inputs are sent as images (rendered_pages, for design review in 05, 09C, 10, 12C, 13).
IMAGE_INPUTS = frozenset({"rendered_pages"})
IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
MAX_REQUEST_BYTES = 32 * 1024 * 1024  # the API's limit for a single request

# Budget downgrade stages (this month's spend / monthly budget); see docs/decisions/0003.
DEGRADE_THRESHOLDS = (
    (1.0, "stopped"),
    (0.85, "downgrade_drafting_model"),
    (0.70, "pause_candidates"),
)

# 00 §F0: input names taken from a part carry the part id as a suffix (findings_04A, test_proposals_04B_lite);
# the suffix is removed before comparing against cannot_see, consistent with thesis-ci's C-PROMPT-ISOLATION.
PART_SUFFIX_RE = re.compile(r"_\d{2}[A-Za-z]?(?:_lite)?$")
# The "variables" of 00 §F1 and the prompts README, written {{name}}. Their names are Chinese words for company,
# ticker, status, period, date, document and subject under review.
VARIABLE_RE = re.compile(r"\{\{\s*([^{}\s]+?)\s*\}\}")
_PROMPT_ID = re.compile(r"^\d{2}[A-Z]?$")
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_INPUT_KEY = re.compile(r"^[A-Za-z0-9_.-]+$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Directory of report outputs ({doc} in outputs.PLACEMENT); 09, 10 and 19 take it from the document variable.
_DOC_OF_PROMPT = {"02": "02", "05": "02", "06": "06", "07": "07", "08": "08", "11": "11", "12": "11", "13": "11"}


class LLMError(RuntimeError):
    """Base class for model call failures."""


class PromptError(LLMError, ValueError):
    """The call does not match the prompt or the role table: an unknown role or part, an unregistered prompt, missing
    or extra inputs, a missing variable, and so on. No request was sent."""


class InputRefused(PromptError):
    """00 §G6: an input is not in the role's can_see, or is in its cannot_see. No request was sent."""


class BudgetExceeded(LLMError):
    """This month's spend has reached the budget; the call was not sent."""

    def __init__(self, spent_usd: float, budget_usd: float):
        self.spent_usd = spent_usd
        self.budget_usd = budget_usd
        super().__init__(
            f"本月模型花费 {spent_usd:.4f} USD 已达到预算 {budget_usd:.2f} USD，调用未发出。"
            "按 docs/decisions/0003 的降级顺序处理；提高预算属于资金事项，由主人决定。"
        )


class LLMRefusal(LLMError):
    """stop_reason == "refusal": the model (including the server-side fallback chain) refused the request."""

    def __init__(self, model: str, category: str | None, explanation: str | None):
        self.model = model
        self.category = category
        self.explanation = explanation
        super().__init__(
            f"模型 {model} 拒绝了请求（stop_reason=refusal，类别={category or '未知'}）。"
            f"{explanation or ''} 按流水线规则开 issue 说明原因，不要静默跳过。"
        )


class LLMTruncated(LLMError):
    """stop_reason == "max_tokens": the output was truncated. result is still usable; the caller decides whether
    to retry."""

    def __init__(self, result: LLMResult):
        self.result = result
        super().__init__(
            f"输出达到 max_tokens 被截断（{result.role} / {result.part_id or result.prompt_id}）；"
            "截断的草稿不能直接进入档案。需要时传 allow_truncated=True。"
        )


class LLMOutputInvalid(LLMError):
    """The output is still invalid after one retry (00 §F0, §F6). errors holds the last attempt's errors; result is
    the last reply."""

    def __init__(self, errors: Sequence[str], result: LLMResult):
        self.errors = list(errors)
        self.result = result
        listed = "\n".join(f"- {e}" for e in self.errors)
        super().__init__(f"{result.part_id or result.prompt_id} 的输出重试后仍不合格，没有交出任何输出：\n{listed}")


# ---------------------------------------------------------------- Data


@dataclasses.dataclass(frozen=True)
class Role:
    """One role from agents/<role>.yml. can_see, cannot_see and prompts default to None (they only have to be
    complete at call time)."""

    role: str
    path: Path
    model: Mapping[str, Any]
    prompts: tuple[str, ...] | None
    can_see: frozenset[str] | None
    cannot_see: frozenset[str] | None


@dataclasses.dataclass(frozen=True)
class PromptFile:
    id: str
    version: str  # the front matter's version
    path: Path
    text: str  # the whole file (front matter included)
    front: Mapping[str, Any]
    revision: str  # file_revision()


@dataclasses.dataclass(frozen=True)
class PromptPart:
    """The part a call runs: the role and this pass's (this mode's) inputs and outputs, as (name, required) pairs."""

    prompt_id: str
    key: str | None  # the part key in the front matter (A, B_lite, draft); None when there are no parts
    label: str  # the part id: 04A, 04B-lite, 03R, 03-draft; equals the prompt id when there are no parts
    role: str
    inputs: tuple[tuple[str, bool], ...]
    outputs: tuple[tuple[str, bool], ...]
    pass_no: int | None = None
    mode: str | None = None
    design: bool = False  # whether to load 00D (the front matter of the prompt or of this part has design)
    formats: Mapping[str, str] = dataclasses.field(default_factory=dict)  # formats overrides from the prompt or part


@dataclasses.dataclass(frozen=True)
class LLMResult:
    text: str  # full text of the last reply
    model: str  # the model that actually produced the reply (response.model)
    requested_model: str
    role: str
    prompt_id: str
    prompt_version: str  # version from the prompt's front matter
    input_sha256: str
    output_sha256: str
    usage: dict[str, int]  # summed over all requests
    cost_usd: float  # summed over all requests
    stop_reason: str | None
    effort: str | None
    fallbacks: str | None
    served_by_fallback: bool
    part: str | None = None
    part_id: str | None = None
    pass_no: int | None = None
    mode: str | None = None
    prompt_revision: str | None = None
    rules_version: str | None = None
    rules_revision: str | None = None
    design_version: str | None = None
    design_revision: str | None = None
    attempts: int = 1
    outputs: Mapping[str, ParsedOutput] = dataclasses.field(default_factory=dict)
    generated_by: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    context: Mapping[str, Any] = dataclasses.field(default_factory=dict)  # default fields for the placement templates

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def placements(self, **context: Any) -> list[Placement]:
        """Where each non-empty output goes under 00 §F2. context overrides the default fields (company, period,
        run_date, month, doc, ...)."""
        return _outputs.place_outputs(
            self.outputs, prompt_id=self.prompt_id, part_id=self.part_id or self.prompt_id, **{**self.context, **context}
        )


# ---------------------------------------------------------------- Hashes and versions


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def input_sha256(
    prompt_id: str, prompt_text: str, inputs: Mapping[str, Any], *, run: Mapping[str, Any] | None = None
) -> str:
    """The sha256 of the canonical JSON of {prompt_id, prompt, inputs[, run]}. complete() passes the full system
    prompt, all inputs (images replaced by file name and content hash) and run (part, pass, mode)."""
    payload: dict[str, Any] = {
        "prompt_id": prompt_id,
        "prompt": prompt_text,
        "inputs": {key: inputs[key] for key in sorted(inputs)},
    }
    if run:
        payload["run"] = dict(run)
    return sha256_text(canonical_json(payload))


def render_inputs(inputs: Mapping[str, str], order: Sequence[str] | None = None) -> str:
    """Render inputs as <input name="..."> blocks, in the order given by order, else sorted by key. The same inputs
    always give the same text."""
    names = [n for n in order if n in inputs] if order is not None else sorted(inputs)
    return "\n\n".join(f'<input name="{key}">\n{inputs[key]}\n</input>' for key in names)


def _git(cwd: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def file_revision(path: str | os.PathLike[str]) -> str:
    """Hash of the last commit that changed the file; "sha256:<content hash>+dirty" when the file is uncommitted or
    modified (00 §H5)."""
    path = Path(path).resolve()
    fallback = f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}+dirty"
    commit = _git(path.parent, "log", "-1", "--format=%H", "--", path.name)
    if not commit:
        return fallback
    status = _git(path.parent, "status", "--porcelain", "--", path.name)
    if status is None or status.strip():
        return fallback
    return commit


# ---------------------------------------------------------------- Role table (agents/*.yml)


def _load_yaml(path: Path) -> Any:
    import yaml  # PyYAML; imported lazily to keep the module light

    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _string_list(value: Any, what: str, path: Path) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise PromptError(f"{path.name} 的 {what} 应当是字符串列表")
    return tuple(v.strip() for v in value)


def load_roles(repo_root: str | os.PathLike[str] | None = None) -> dict[str, Role]:
    """Read agents/*.yml: role name (the role field, else the file name) → Role. A role defined twice or a missing
    model.id is an error."""
    agents = Path(repo_root or REPO_ROOT) / "agents"
    roles: dict[str, Role] = {}
    paths = sorted([*agents.glob("*.yml"), *agents.glob("*.yaml")]) if agents.is_dir() else []
    for path in paths:
        data = _load_yaml(path)
        if not isinstance(data, dict):
            raise PromptError(f"{path} 应当是一个映射")
        name = data.get("role") or path.stem
        if name in roles:
            raise PromptError(f"角色 {name} 定义了两次：{roles[name].path.name}、{path.name}")
        model = data.get("model")
        if not isinstance(model, dict) or not isinstance(model.get("id"), str) or not model["id"].strip():
            raise PromptError(f"{path.name} 没有 model.id（角色的模型只由 agents/*.yml 决定）")
        visible = {k: _string_list(data[k], k, path) if k in data else None for k in ("prompts", "can_see", "cannot_see")}
        roles[name] = Role(
            role=name,
            path=path,
            model=dict(model),
            prompts=visible["prompts"],
            can_see=frozenset(visible["can_see"]) if visible["can_see"] is not None else None,
            cannot_see=frozenset(visible["cannot_see"]) if visible["cannot_see"] is not None else None,
        )
    return roles


def role_definition(role: str, repo_root: str | os.PathLike[str] | None = None) -> Role:
    roles = load_roles(repo_root)
    if role not in roles:
        known = "、".join(sorted(roles)) or "（agents/ 下没有角色定义）"
        raise PromptError(f"未知角色 {role!r}；agents/*.yml 定义的角色：{known}")
    return roles[role]


def _resolve_fallbacks(model_id: str, configured: Any) -> str | None:
    if configured is None:
        return "default" if model_id in SERVER_FALLBACK_MODELS else None
    text = str(configured).strip()
    return None if text.lower() in FALLBACKS_OFF else text


def resolve_model(
    role: str,
    *,
    model: str | None = None,
    effort: str | None = None,
    repo_root: str | os.PathLike[str] | None = None,
) -> tuple[str, str | None, str | None]:
    """Return (model id, effort, fallbacks): arguments first, then agents/<role>.yml; an unknown role is an error."""
    return _model_choice(role_definition(role, repo_root).model, model, effort)


def _model_choice(config: Mapping[str, Any], model: str | None, effort: str | None) -> tuple[str, str | None, str | None]:
    configured_id = config["id"]
    model_id = model or configured_id
    chosen_effort = effort or config.get("effort") or DEFAULT_EFFORT
    if chosen_effort not in EFFORT_LEVELS:
        raise ValueError(f"effort 必须是 {EFFORT_LEVELS} 之一，收到 {chosen_effort!r}")
    if model_id in NO_EFFORT_MODELS:
        chosen_effort = None
    # When the caller switches models, the fallbacks configured in the agents file for the original model no
    # longer apply.
    configured_fallbacks = config.get("fallbacks") if model_id == configured_id else None
    return model_id, chosen_effort, _resolve_fallbacks(model_id, configured_fallbacks)


def _fallback_kwargs(fallbacks: str | None) -> dict[str, Any] | None:
    if fallbacks is None:
        return None
    if fallbacks == "default":
        return {"betas": [FALLBACK_BETA_DEFAULT], "fallbacks": "default"}
    models = [item.strip() for item in fallbacks.split(",") if item.strip()]
    return {"betas": [FALLBACK_BETA_ARRAY], "fallbacks": [{"model": m} for m in models]}


# ---------------------------------------------------------------- Prompts (prompts/ in the private repository)


def resolve_prompts_dir(
    prompts_dir: str | os.PathLike[str] | None = None, repo_root: str | os.PathLike[str] | None = None
) -> Path:
    """The prompts directory: the argument → the environment variable OWNERS_OFFICE_PROMPTS →
    owners-office-private/prompts next to the public repository."""
    if prompts_dir is not None:
        path = Path(prompts_dir)
    elif os.environ.get(PROMPTS_ENV):
        path = Path(os.environ[PROMPTS_ENV])
    else:
        path = Path(repo_root or REPO_ROOT).resolve().parent / PRIVATE_REPO_NAME / "prompts"
    if not path.is_dir():
        raise PromptError(
            f"找不到提示词目录 {path}；用参数 prompts_dir 或环境变量 {PROMPTS_ENV} 指定，"
            f"默认是与公开仓库并列的 {PRIVATE_REPO_NAME}/prompts"
        )
    return path


def load_prompt(prompt_id: str, prompts_dir: str | os.PathLike[str] | None = None) -> PromptFile:
    """Read <id>-*.md by prompt id and check the front matter's id and version."""
    if not isinstance(prompt_id, str) or not _PROMPT_ID.match(prompt_id):
        raise PromptError(f"提示词编号写成两位数字加可选字母（00、00D、03、17），收到 {prompt_id!r}")
    directory = resolve_prompts_dir(prompts_dir)
    matches = sorted(directory.glob(f"{prompt_id}-*.md"))
    if len(matches) != 1:
        found = "、".join(p.name for p in matches) or "没有"
        raise PromptError(f"提示词 {prompt_id} 应当在 {directory} 里恰好有一个 {prompt_id}-*.md，找到：{found}")
    path = matches[0]
    text = path.read_text(encoding="utf-8")
    parts = _outputs.split_front_matter(text)
    if parts is None:
        raise PromptError(f"{path.name} 没有 YAML front matter")
    try:
        front = _outputs.load_yaml_text(parts[0])
    except Exception as exc:  # yaml.YAMLError
        raise PromptError(f"{path.name} 的 front matter 解析失败：{exc}") from exc
    if not isinstance(front, dict):
        raise PromptError(f"{path.name} 的 front matter 应当是一个映射")
    if front.get("id") != prompt_id:
        raise PromptError(f"{path.name} 的 front matter id 是 {front.get('id')!r}，不是 {prompt_id!r}")
    version = front.get("version")
    if not isinstance(version, (str, int, float)) or isinstance(version, bool) or not str(version).strip():
        raise PromptError(f"{path.name} 的 front matter 没有 version")
    return PromptFile(prompt_id, str(version), path, text, front, file_revision(path))


def _part_label(prompt_id: str, key: str | None, block: Mapping[str, Any]) -> str:
    """04A, 04B-lite, 14Q; a name that starts with an id uses that id (03R, 03P); the rest are written like 03-draft."""
    if key is None:
        return prompt_id
    name = block.get("name")
    token = str(name).split()[0] if isinstance(name, str) and name.split() else ""
    if re.fullmatch(rf"{re.escape(prompt_id)}[A-Z][\w-]*", token):
        return token
    if key[:1].isupper():
        return prompt_id + key.replace("_", "-")
    return f"{prompt_id}-{key}"


def _names(value: Any, what: str) -> list[tuple[str, bool]]:
    if not isinstance(value, list):
        raise PromptError(f"{what} 应当是列表")
    out = []
    for item in value:
        name = str(item).strip()
        required = not name.endswith("?")
        name = name.rstrip("?").strip()
        if not _NAME.match(name):
            raise PromptError(f"{what} 里有看不懂的名字 {item!r}")
        out.append((name, required))
    return out


def _dedupe(items: Sequence[tuple[str, bool]]) -> tuple[tuple[str, bool], ...]:
    merged: dict[str, bool] = {}
    for name, required in items:
        merged[name] = merged.get(name, False) or required
    return tuple(merged.items())


def _format_overrides(value: Any, what: str) -> dict[str, str]:
    """The front matter's formats: {output name: yaml|markdown|text|file}."""
    if value is None:
        return {}
    if not isinstance(value, Mapping) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise PromptError(f"{what} 的 formats 应当是 {{输出名: 格式}} 映射")
    return dict(value)


def output_formats(rules: PromptFile, call: PromptPart) -> dict[str, str]:
    """The format of each output of this part (00 §F0): output_formats from the 00 front matter, overridden by the
    prompt's or part's formats. An output without a declared format, a name declared with two formats, or an output
    with a schema in the wrong format is an error."""
    declared = rules.front.get("output_formats")
    if not isinstance(declared, Mapping):
        raise PromptError(f"{rules.path.name} 的 front matter 没有 output_formats（§F0 的输出格式声明）")
    by_name: dict[str, str] = {}
    for fmt, names in declared.items():
        if not isinstance(names, list):
            raise PromptError(f"{rules.path.name} 的 output_formats.{fmt} 应当是输出名列表")
        for name in names:
            if name in by_name and by_name[name] != fmt:
                raise PromptError(f"{rules.path.name} 把输出 {name} 同时声明成 {by_name[name]} 和 {fmt}")
            by_name[str(name)] = str(fmt)
    by_name.update(call.formats)
    missing = [name for name, _ in call.outputs if name not in by_name]
    if missing:
        raise PromptError(f"{call.label} 的输出 {'、'.join(missing)} 没有声明格式（00 的 output_formats 或部分的 formats）")
    formats = {name: by_name[name] for name, _ in call.outputs}
    problems = _outputs.format_problems(formats)
    if problems:
        raise PromptError(f"{call.label} 的输出格式声明不对：" + "；".join(problems))
    return formats


def prompt_part(
    prompt: PromptFile, part: str | None = None, *, pass_no: int | None = None, mode: str | None = None
) -> PromptPart:
    """Resolve from the front matter the part a call runs. part may be a part key (A, B_lite, draft) or a part id
    (04A, 04B-lite, 03R)."""
    front = prompt.front
    parts = front.get("parts")
    if parts is not None and not isinstance(parts, Mapping):
        raise PromptError(f"{prompt.path.name} 的 parts 应当是映射")
    if parts:
        labels = {k: _part_label(prompt.id, k, v if isinstance(v, Mapping) else {}) for k, v in parts.items()}
        key = part if part in parts else next((k for k, lbl in labels.items() if lbl == part), None)
        if key is None:
            choices = "、".join(f"{k}（{labels[k]}）" for k in parts)
            raise PromptError(f"提示词 {prompt.id} 分部分运行，part 要写其中之一：{choices}；收到 {part!r}")
        block = parts[key]
        if not isinstance(block, Mapping):
            raise PromptError(f"{prompt.path.name} 的部分 {key} 应当是映射")
        role = block.get("role", front.get("role"))
    else:
        if part not in (None, prompt.id):
            raise PromptError(f"提示词 {prompt.id} 没有分部分，不要给 part（收到 {part!r}）")
        key, block, role = None, front, front.get("role")
    label = _part_label(prompt.id, key, block)
    if role == "pipeline":
        raise PromptError(f"{label} 是流水线的确定性步骤（role: pipeline），不调用模型")
    if not isinstance(role, str) or not role:
        raise PromptError(f"{label} 的 front matter 没有写角色")

    # Multi-pass parts (04B, 09B) give inputs and outputs per pass: inputs_passN, outputs_passN; calls gives the
    # number of passes.
    pass_keys = sorted(k for k in block if isinstance(k, str) and re.fullmatch(r"inputs_pass\d+", k))
    calls = block.get("calls")
    if calls is not None and (isinstance(calls, bool) or calls != max(1, len(pass_keys))):
        raise PromptError(f"{label} 的 calls 应当是整数 {max(1, len(pass_keys))}（与 inputs_passN 的遍数一致），收到 {calls!r}")
    if pass_keys:
        passes = [int(k[len("inputs_pass"):]) for k in pass_keys]
        if pass_no not in passes:
            raise PromptError(f"{label} 分遍调用，pass_no 要写 {'、'.join(map(str, passes))} 之一；收到 {pass_no!r}")
        if f"outputs_pass{pass_no}" not in block:
            raise PromptError(f"{label} 分遍调用，front matter 要写 outputs_pass{pass_no}")
        raw_inputs, raw_outputs = block[f"inputs_pass{pass_no}"], block[f"outputs_pass{pass_no}"]
    else:
        if pass_no is not None:
            raise PromptError(f"{label} 只调用一遍，不要给 pass_no")
        raw_inputs, raw_outputs = block.get("inputs"), block.get("outputs")

    # Prompts with modes (02): if the mode lists its own inputs or outputs, use them; otherwise use the prompt's.
    modes = front.get("modes")
    if modes:
        if not isinstance(modes, Mapping) or mode not in modes:
            choices = "、".join(map(str, modes)) if isinstance(modes, Mapping) else str(modes)
            raise PromptError(f"提示词 {prompt.id} 有多个模式，mode 要写其中之一：{choices}；收到 {mode!r}")
        spec = modes[mode] if isinstance(modes[mode], Mapping) else {}
        raw_inputs = spec.get("inputs", raw_inputs)
        raw_outputs = spec.get("outputs", raw_outputs)
    elif mode is not None:
        raise PromptError(f"提示词 {prompt.id} 没有模式，不要给 mode（收到 {mode!r}）")
    where = f"{label}" + (f" 模式 {mode}" if mode else "") + (f" 第 {pass_no} 遍" if pass_no else "")
    formats = _format_overrides(front.get("formats"), prompt.path.name)
    if block is not front:
        formats.update(_format_overrides(block.get("formats"), label))
    return PromptPart(
        prompt_id=prompt.id,
        key=key,
        label=label,
        role=role,
        inputs=_dedupe(_names(raw_inputs, f"{where} 的 inputs")),
        outputs=_dedupe(_names(raw_outputs, f"{where} 的 outputs")),
        pass_no=pass_no,
        mode=mode,
        design="design" in front or "design" in block,
        formats=formats,
    )


def fill_variables(prompt: PromptFile, variables: Mapping[str, str] | None) -> str:
    """Replace each {{variable}} in the prompt with its value from variables. A missing one is an error: no
    placeholder is handed to the model."""
    variables = dict(variables or {})
    wanted = sorted({m.group(1) for m in VARIABLE_RE.finditer(prompt.text)})
    missing = [name for name in wanted if name not in variables]
    if missing:
        raise PromptError(f"提示词 {prompt.id} 需要变量 {'、'.join(missing)}（variables）")
    for name in wanted:
        if not isinstance(variables[name], str):
            raise TypeError(f"变量 {name!r} 必须是字符串")
    return VARIABLE_RE.sub(lambda m: variables[m.group(1)], prompt.text)


# ---------------------------------------------------------------- Checking and rendering inputs


def _hidden_by(name: str, cannot_see: frozenset[str]) -> str | None:
    """If name, or name without its part-id suffix, is in cannot_see, return the matching entry (case-insensitive)."""
    lowered = {item.strip().rstrip("?").strip().lower(): item for item in cannot_see}
    low = name.lower()
    for candidate in (low, PART_SUFFIX_RE.sub("", low)):
        if candidate in lowered:
            return lowered[candidate]
    return None


def check_inputs(call: PromptPart, role: Role, provided: Sequence[str]) -> None:
    """00 §G6: give the model only inputs that the part declares and the role can see. Otherwise raise PromptError or
    InputRefused."""
    declared = [name for name, _ in call.inputs]
    problems = [
        f"{name} 不是 {call.label} 的输入（{call.label} 的输入：{'、'.join(declared)}）"
        for name in sorted(set(provided) - set(declared))
    ]
    problems += [f"缺少必需输入 {name}" for name, required in call.inputs if required and name not in provided]
    if problems:
        raise PromptError(f"{call.label} 的输入不对：" + "；".join(problems))
    refused = []
    for name in sorted(provided):
        if name not in (role.can_see or frozenset()):
            refused.append(f"{name} 不在 can_see 里")
        hidden = _hidden_by(name, role.cannot_see or frozenset())
        if hidden is not None:
            refused.append(f"{name} 在 cannot_see 里（{hidden}）")
    if refused:
        raise InputRefused(f"{call.label} 不能把这些输入交给 {role.role}（{role.path.name}，00 §G6）：" + "；".join(refused))


def _validate_inputs(inputs: Mapping[str, str], images: Mapping[str, Sequence[Any]]) -> None:
    if not isinstance(inputs, Mapping) or not isinstance(images, Mapping) or not (inputs or images):
        raise ValueError("inputs 必须是非空的 {名称: 文本} 字典（页面图像另用 images 传）")
    for key, value in inputs.items():
        if not isinstance(key, str) or not _INPUT_KEY.match(key):
            raise ValueError(f"输入名只能含字母、数字、下划线、点和连字符：{key!r}")
        if not isinstance(value, str):
            raise TypeError(f"输入 {key!r} 必须是字符串")
        if key in IMAGE_INPUTS:
            raise ValueError(f"{key} 是页面图像，用 images={{{key!r}: [图像文件, …]}} 传")
    for key, files in images.items():
        if key not in IMAGE_INPUTS:
            raise ValueError(f"只有 {'、'.join(sorted(IMAGE_INPUTS))} 以图像传，收到 {key!r}")
        if key in inputs:
            raise ValueError(f"{key} 同时出现在 inputs 和 images 里")
        if isinstance(files, (str, os.PathLike)) or not isinstance(files, Sequence) or not files:
            raise ValueError(f"images[{key!r}] 必须是非空的图像文件列表")


def _load_images(images: Mapping[str, Sequence[Any]]) -> dict[str, list[tuple[str, str, str, str]]]:
    """{input name: [(file name, media_type, base64, sha256)]}."""
    loaded: dict[str, list[tuple[str, str, str, str]]] = {}
    for key, files in images.items():
        rows = []
        for item in files:
            path = Path(item)
            media_type = IMAGE_MEDIA_TYPES.get(path.suffix.lower())
            if media_type is None:
                raise ValueError(f"{path.name}：页面图像只支持 {'、'.join(sorted(IMAGE_MEDIA_TYPES))}")
            if not path.is_file():
                raise FileNotFoundError(f"找不到页面图像：{path}")
            data = path.read_bytes()
            encoded = base64.standard_b64encode(data).decode("ascii")
            rows.append((path.name, media_type, encoded, hashlib.sha256(data).hexdigest()))
        loaded[key] = rows
    return loaded


def _run_header(call: PromptPart) -> str:
    attrs = {"prompt": call.prompt_id, "part": call.key, "label": call.label if call.key else None,
             "pass": call.pass_no, "mode": call.mode}
    return "<run " + " ".join(f'{k}="{v}"' for k, v in attrs.items() if v is not None) + "/>"


def render_user_content(
    call: PromptPart, inputs: Mapping[str, str], images: Mapping[str, list[tuple[str, str, str, str]]] | None = None
) -> str | list[dict[str, Any]]:
    """The user message: one <run .../> line, then one <input name="..."> block per input in front matter order.
    Without images it is one piece of text; with images it is a list of content blocks, with each image as a base64
    image block inside its <input> block."""
    images = images or {}
    order = [name for name, _ in call.inputs if name in inputs or name in images]
    if not images:
        return _run_header(call) + "\n\n" + render_inputs(inputs, order)
    blocks: list[dict[str, Any]] = []
    text: list[str] = [_run_header(call)]

    def flush() -> None:
        if text:
            blocks.append({"type": "text", "text": "\n\n".join(text)})
            text.clear()

    for name in order:
        if name in inputs:
            text.append(f'<input name="{name}">\n{inputs[name]}\n</input>')
            continue
        text.append(f'<input name="{name}">')
        for page, (file_name, media_type, data, _digest) in enumerate(images[name], start=1):
            text.append(f'<page n="{page}" file="{file_name}"/>')
            flush()
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}})
        text.append("</input>")
    flush()
    return blocks


def _content_bytes(content: str | list[dict[str, Any]]) -> int:
    if isinstance(content, str):
        return len(content.encode("utf-8"))
    return sum(len(b["source"]["data"]) if b["type"] == "image" else len(b["text"].encode("utf-8")) for b in content)


def _with_errors(content: str | list[dict[str, Any]], errors: Sequence[str]) -> str | list[dict[str, Any]]:
    """Retry: append the validation errors to the original request."""
    note = (
        "<validation_errors>\n上一次回答的输出没有通过流水线的校验（00 §F0、§F6）。"
        "按下面的错误改正后，按原来的要求重新交出这一部分的全部输出：\n"
        + "\n".join(f"- {e}" for e in errors)
        + "\n</validation_errors>"
    )
    if isinstance(content, str):
        return content + "\n\n" + note
    return [*content, {"type": "text", "text": note}]


# ---------------------------------------------------------------- Budget and log


def _log_path(log_path: str | os.PathLike[str] | None) -> Path:
    if log_path is not None:
        return Path(log_path)
    env = os.environ.get(LOG_ENV)
    return Path(env) if env else DEFAULT_LOG_PATH


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def monthly_budget(budget_usd: float | None = None, repo_root: str | os.PathLike[str] | None = None) -> float:
    if budget_usd is not None:
        return float(budget_usd)
    path = Path(repo_root or REPO_ROOT) / "constitution" / "decision-rights.yml"
    if path.is_file():
        data = _load_yaml(path)
        budget = (data or {}).get("budget") if isinstance(data, dict) else None
        if isinstance(budget, dict) and isinstance(budget.get("monthly_usd"), (int, float)):
            return float(budget["monthly_usd"])
    return DEFAULT_MONTHLY_BUDGET_USD


def _same_month(timestamp: str, now: dt.datetime) -> bool:
    try:
        stamp = dt.datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return isinstance(timestamp, str) and timestamp.startswith(now.strftime("%Y-%m"))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    stamp = stamp.astimezone(dt.timezone.utc)
    return (stamp.year, stamp.month) == (now.year, now.month)


def month_spend(log_path: str | os.PathLike[str] | None = None, now: dt.datetime | None = None) -> float:
    """Total spend (USD) in the log for the current UTC calendar month. Bad lines are skipped."""
    path = _log_path(log_path)
    now = (now or _utcnow()).astimezone(dt.timezone.utc)
    if not path.is_file():
        return 0.0
    total = 0.0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict) or not _same_month(record.get("timestamp", ""), now):
                continue
            cost = record.get("cost_usd")
            if isinstance(cost, (int, float)):
                total += float(cost)
    return round(total, 6)


def remaining_budget(
    *,
    log_path: str | os.PathLike[str] | None = None,
    budget_usd: float | None = None,
    repo_root: str | os.PathLike[str] | None = None,
    now: dt.datetime | None = None,
) -> float:
    budget = monthly_budget(budget_usd, repo_root)
    return round(max(0.0, budget - month_spend(log_path, now)), 6)


def budget_status(
    *,
    log_path: str | os.PathLike[str] | None = None,
    budget_usd: float | None = None,
    repo_root: str | os.PathLike[str] | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """This month's budget status and downgrade stage: normal → pause_candidates → downgrade_drafting_model →
    stopped."""
    now = (now or _utcnow()).astimezone(dt.timezone.utc)
    budget = monthly_budget(budget_usd, repo_root)
    spent = month_spend(log_path, now)
    fraction = spent / budget if budget > 0 else 1.0
    stage = next((name for threshold, name in DEGRADE_THRESHOLDS if fraction >= threshold), "normal")
    return {
        "month": now.strftime("%Y-%m"),
        "spent_usd": spent,
        "budget_usd": budget,
        "remaining_usd": round(max(0.0, budget - spent), 6),
        "fraction": round(fraction, 4),
        "stage": stage,
    }


def _append_log(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(canonical_json(record) + "\n")


_USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def _usage_dict(usage: Any) -> dict[str, int]:
    def read(name: str) -> int:
        value = usage.get(name) if isinstance(usage, Mapping) else getattr(usage, name, None)
        return int(value) if isinstance(value, (int, float)) else 0

    return {name: read(name) for name in _USAGE_FIELDS}


def _priced(model: str) -> bool:
    return model in PRICES_PER_MTOK and model in CACHE_PRICES_PER_MTOK


def cost_breakdown(model: str, usage: Mapping[str, int]) -> dict[str, float]:
    """Cost in USD, computed separately at each part's own price: uncached input, output, cache write, cache read."""
    input_price, output_price = PRICES_PER_MTOK[model]
    write_price, read_price = CACHE_PRICES_PER_MTOK[model]
    parts = {
        "input": usage.get("input_tokens", 0) * input_price,
        "output": usage.get("output_tokens", 0) * output_price,
        "cache_write": usage.get("cache_creation_input_tokens", 0) * write_price,
        "cache_read": usage.get("cache_read_input_tokens", 0) * read_price,
    }
    return {name: round(value / 1_000_000, 6) for name, value in parts.items()}


def cost_usd(model: str, usage: Mapping[str, int]) -> float:
    return round(sum(cost_breakdown(model, usage).values()), 6)


def _served_by_fallback(response: Any) -> bool:
    """A fallback_message in usage.iterations means a fallback model took over (a sticky-routed turn has no fallback
    block)."""
    iterations = getattr(getattr(response, "usage", None), "iterations", None) or []
    return any(getattr(entry, "type", None) == "fallback_message" for entry in iterations)


# ---------------------------------------------------------------- Calls


def _default_client() -> Any:
    try:
        import anthropic  # lazy import: this module still imports without the SDK installed
    except ImportError as exc:
        raise LLMError("需要 Anthropic Python SDK：pip install -r requirements.txt") from exc
    return anthropic.Anthropic()  # credentials are read from the environment, not kept in the code


def _send(client: Any, params: Mapping[str, Any], fallback_kwargs: Mapping[str, Any] | None, stream: bool) -> Any:
    """One request. With server-side fallback it goes through the beta API; streaming uses messages.stream(...) and
    its get_final_message()."""
    api = client.beta.messages if fallback_kwargs else client.messages
    kwargs = {**params, **(fallback_kwargs or {})}
    if stream:
        with api.stream(**kwargs) as response_stream:
            return response_stream.get_final_message()
    return api.create(**kwargs)


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _run_context(call: PromptPart, inputs: Mapping[str, str], variables: Mapping[str, str]) -> dict[str, Any]:
    """Default fields for the placement templates (outputs.place): company ticker, period, run date, report
    directory, subject under review."""
    run_date = inputs.get("run_date", "").strip()
    return {
        "company": variables.get("代码"),
        "period": variables.get("期间"),
        "run_date": run_date if _DATE.match(run_date) else _utcnow().date().isoformat(),
        "doc": _DOC_OF_PROMPT.get(call.prompt_id) or variables.get("文档"),
        "subject": variables.get("被审对象"),
    }


def complete(
    role: str,
    prompt_id: str,
    inputs: Mapping[str, str],
    *,
    part: str | None = None,
    pass_no: int | None = None,
    mode: str | None = None,
    variables: Mapping[str, str] | None = None,
    images: Mapping[str, Sequence[str | os.PathLike[str]]] | None = None,
    pipeline_fields: Mapping[str, Mapping[str, Any]] | None = None,
    model: str | None = None,
    effort: str | None = None,
    stream: bool = True,
    max_tokens: int | None = None,
    client: Any = None,
    log_path: str | os.PathLike[str] | None = None,
    budget_usd: float | None = None,
    repo_root: str | os.PathLike[str] | None = None,
    prompts_dir: str | os.PathLike[str] | None = None,
    schemas_dir: str | os.PathLike[str] | None = None,
    allow_truncated: bool = False,
) -> LLMResult:
    """Run one part of prompt prompt_id as role and return the validated outputs.

    - part: the part key or id (A / 04A, B_lite / 04B-lite, draft, revise / 03R); omit it when the prompt has no
      parts.
    - pass_no: which pass of a multi-pass part (04B, 09B); mode: the mode of a prompt with modes (02).
    - inputs: {input name: text}; images: {"rendered_pages": [image file, ...]}.
    - variables: the values of the prompt's {{variables}}, e.g. company "Microsoft", ticker "MSFT" and period
      "FY2027Q1" (keyed by the prompt's variable names, which are Chinese).
    - pipeline_fields: {output name: {key: value}}, fields the pipeline maintains (00 §G8, e.g. thesis's
      trust_level), written into that output before validation.
    - stream, max_tokens: streaming by default; the limit is 128000 for 01, 02 and 11 and 64000 for the rest (16000
      when not streaming).
    - prompts_dir, schemas_dir: the prompts directory and the thesis-ci schema directory; for the defaults see
      resolve_prompts_dir() and outputs.schema_validator().

    Before any request is sent, it checks the role, part, inputs and variables (PromptError, InputRefused) and the
    budget (BudgetExceeded). A refusal raises LLMRefusal; a truncation raises LLMTruncated (with allow_truncated=True
    the result is returned as usual, without validating outputs). An invalid output is retried once; if it is still
    invalid, LLMOutputInvalid is raised. Every request sent writes one log line.
    """
    root = Path(repo_root) if repo_root else REPO_ROOT
    log = _log_path(log_path)
    images = images or {}
    variables = dict(variables or {})
    _validate_inputs(inputs, images)

    spec = role_definition(role, root)
    for field in ("prompts", "can_see", "cannot_see"):
        if getattr(spec, field) is None:
            raise PromptError(f"{spec.path.name} 没有 {field}；角色要写全才能调用（thesis-ci agent.schema.json）")
    pdir = resolve_prompts_dir(prompts_dir, root)
    prompt = load_prompt(prompt_id, pdir)
    call = prompt_part(prompt, part, pass_no=pass_no, mode=mode)
    if call.role != role:
        raise PromptError(f"{call.label} 的角色是 {call.role}（{prompt.path.name} 的 front matter），不是 {role}")
    if prompt.id not in spec.prompts and call.label not in spec.prompts:
        raise PromptError(f"{spec.path.name} 的 prompts 没有登记 {call.label}（也没有整份登记 {prompt.id}）")
    rules = load_prompt(RULES_ID, pdir)
    formats = output_formats(rules, call)
    files = [name for name, required in call.outputs if required and formats[name] == _outputs.FILE]
    if files:
        raise PromptError(f"{call.label} 要交出文件（{'、'.join(files)}），需要能执行代码的环境；llm.py 只交换文本")
    check_inputs(call, spec, [*inputs, *images])

    design = load_prompt(DESIGN_ID, pdir) if call.design else None
    system_texts = [rules.text, *([design.text] if design else []), fill_variables(prompt, variables)]
    try:
        validators = _outputs.validators_for([name for name, _ in call.outputs], schemas_dir)
    except _outputs.SchemaUnavailable as exc:
        raise LLMError(f"{call.label} 的输出要按 thesis-ci schema 校验：{exc}") from exc

    model_id, chosen_effort, fallbacks = _model_choice(spec.model, model, effort)
    if not _priced(model_id):
        raise ValueError(f"模型 {model_id} 不在价格表里，花费无法记账；先在 PRICES_PER_MTOK 与 CACHE_PRICES_PER_MTOK 登记")

    loaded = _load_images(images)
    content = render_user_content(call, inputs, loaded)
    if _content_bytes(content) + sum(len(t.encode("utf-8")) for t in system_texts) > MAX_REQUEST_BYTES:
        raise ValueError(f"请求超过 {MAX_REQUEST_BYTES // (1024 * 1024)} MB 的上限（多半是页面图像太大），先压缩图像")
    hashed_inputs: dict[str, Any] = {**inputs, **{k: [f"{f}:sha256:{d}" for f, _, _, d in v] for k, v in loaded.items()}}
    run = {k: v for k, v in (("part", call.key), ("pass", call.pass_no), ("mode", call.mode)) if v is not None}
    in_hash = input_sha256(prompt.id, "\n\n".join(system_texts), hashed_inputs, run=run)

    if max_tokens is None:
        max_tokens = (LONG_MAX_TOKENS if prompt.id in LONG_OUTPUT_PROMPTS else STREAM_MAX_TOKENS) if stream else MAX_TOKENS
    system = [{"type": "text", "text": text, "cache_control": dict(CACHE_CONTROL)} for text in system_texts[:-1]]
    system.append({"type": "text", "text": system_texts[-1]})  # the specific prompt comes after the cache breakpoint
    params: dict[str, Any] = {
        "model": model_id,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": content}],
    }
    if chosen_effort is not None:
        params["output_config"] = {"effort": chosen_effort}
    if model_id in ADAPTIVE_THINKING_MODELS:
        params["thinking"] = {"type": "adaptive"}
    fallback_kwargs = _fallback_kwargs(fallbacks)
    context = _run_context(call, inputs, variables)

    versions = {
        "prompt_version": prompt.version,
        "prompt_revision": prompt.revision,
        "rules_version": rules.version,
        "rules_revision": rules.revision,
        "design_version": design.version if design else None,
        "design_revision": design.revision if design else None,
    }
    budget = monthly_budget(budget_usd, root)
    total_usage = _usage_dict(None)
    total_cost = 0.0
    errors: list[str] = []
    for attempt in (1, 2):
        spent = month_spend(log)
        if spent >= budget:
            raise BudgetExceeded(spent, budget)
        if client is None:
            client = _default_client()
        record: dict[str, Any] = {
            "timestamp": _utcnow().isoformat(timespec="seconds"),
            "role": role,
            "prompt_id": prompt.id,
            "part": call.key,
            "part_id": call.label,
            "pass": call.pass_no,
            "mode": call.mode,
            "attempt": attempt,
            "prompt_path": _relative(prompt.path, pdir.parent),
            **versions,
            "requested_model": model_id,
            "model": None,
            "effort": chosen_effort,
            "fallbacks": fallbacks,
            "served_by_fallback": False,
            "stream": stream,
            "max_tokens": max_tokens,
            "images": sum(len(v) for v in loaded.values()),
            "input_sha256": in_hash,
            "output_sha256": None,
            "usage": _usage_dict(None),
            "cost_usd": 0.0,
            "stop_reason": None,
        }
        request = params
        if attempt == 2:  # retry: the same request, with the first attempt's validation errors appended
            request = {**params, "messages": [{"role": "user", "content": _with_errors(content, errors)}]}
        try:
            response = _send(client, request, fallback_kwargs, stream)
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"[:500]
            _append_log(log, record)
            raise

        served_model = getattr(response, "model", None) or model_id
        usage = _usage_dict(getattr(response, "usage", None))
        price_model = served_model if _priced(served_model) else model_id
        record.update(
            model=served_model,
            served_by_fallback=_served_by_fallback(response),
            usage=usage,  # includes cache_creation_input_tokens and cache_read_input_tokens
            cost_usd=cost_usd(price_model, usage),
            cost_breakdown=cost_breakdown(price_model, usage),
            stop_reason=getattr(response, "stop_reason", None),
            request_id=getattr(response, "_request_id", None),
        )
        if price_model != served_model:
            record["price_basis"] = price_model
        total_usage = {k: total_usage[k] + usage[k] for k in total_usage}
        total_cost = round(total_cost + record["cost_usd"], 6)

        # Check stop_reason before reading content: on a refusal, content is empty or only a fragment.
        if record["stop_reason"] == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None)
            record["refusal_category"] = category
            _append_log(log, record)
            raise LLMRefusal(served_model, category, getattr(details, "explanation", None))

        text = "".join(block.text for block in response.content if getattr(block, "type", None) == "text")
        record["output_sha256"] = sha256_text(text)
        generated_by = {
            "model": served_model,
            "rules_version": rules.version,
            **({"design_version": design.version} if design else {}),
            "prompt": prompt.id,
            **({"part": call.label} if call.key else {}),
            **({"pass": call.pass_no} if call.pass_no is not None else {}),
            "prompt_version": prompt.version,
            "input_sha256": in_hash,
        }
        result = LLMResult(
            text=text,
            model=served_model,
            requested_model=model_id,
            role=role,
            prompt_id=prompt.id,
            prompt_version=prompt.version,
            input_sha256=in_hash,
            output_sha256=record["output_sha256"],
            usage=total_usage,
            cost_usd=total_cost,
            stop_reason=record["stop_reason"],
            effort=chosen_effort,
            fallbacks=fallbacks,
            served_by_fallback=record["served_by_fallback"],
            part=call.key,
            part_id=call.label,
            pass_no=call.pass_no,
            mode=call.mode,
            prompt_revision=prompt.revision,
            rules_version=rules.version,
            rules_revision=rules.revision,
            design_version=versions["design_version"],
            design_revision=versions["design_revision"],
            attempts=attempt,
            generated_by=generated_by,
            context=context,
        )
        if result.stop_reason == "max_tokens":
            _append_log(log, record)
            if not allow_truncated:
                raise LLMTruncated(result)
            return result  # truncated replies are not validated and yield no outputs; the caller handles text itself

        parsed, errors = _outputs.parse_reply(
            text,
            call.outputs,
            formats=formats,
            generated_by=generated_by,
            validators=validators,
            pipeline_fields=pipeline_fields,
            where=f" {call.label} ",
        )
        if errors:
            record["validation_errors"] = errors[:50]
        _append_log(log, record)
        if not errors:
            return dataclasses.replace(result, outputs=parsed)
        if attempt == 2:
            raise LLMOutputInvalid(errors, result)
    raise AssertionError("unreachable")  # pragma: no cover
