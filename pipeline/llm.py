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
   {{variables}} ({{company}}, {{ticker}}, {{period}}, ...) are filled from variables.
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
6. Backends (docs/decisions/0022). The same system prompt, user content, validation, retry and logging serve all
   three; only the transport differs:
   - claude-code (the default; OWNERS_OFFICE_BACKEND overrides it): the Claude Code CLI in print mode on the owner's
     Claude subscription, run as a subprocess in an empty temporary directory with a minimal environment: the
     system prompt from --system-prompt-file, the user content on stdin, --model and --effort from the role, no
     tools, no settings files, no MCP servers, no skills, no CLAUDE.md, no auto memory, no session saved (see
     claude_code_command() and claude_code_env()). The binary is OWNERS_OFFICE_CLAUDE_BIN, else claude on PATH,
     else the newest copy the Claude desktop app installed. Its calls cost no API money: they are logged with
     cost_usd 0 and the CLI's own estimate as notional_cost_usd. Its output limit is the model's full ceiling
     (CLAUDE_CODE_MAX_TOKENS), which covers thinking and the reply together; a run the CLI continued into a second
     turn without any tool is reported as a truncation (LLMTruncated), not as tool use.
   - api: the Anthropic API through the SDK. Requests stream by default
     (client.messages.stream(...).get_final_message()); the output limit is 128000 for 01, 02 and 11 and 64000 for
     the rest. Drafting and oversight models both use adaptive thinking (thinking={"type": "adaptive"}) and
     budget_tokens is never sent; depth is set with output_config.effort. Oversight roles go through the beta API
     with server-side refusal fallback turned on (docs/decisions/0003). The system prompt blocks of 00 (and 00D)
     carry a 5-minute cache breakpoint, with the specific prompt after the breakpoint; cache-write and cache-read
     tokens are charged at their own prices from CACHE_PRICES_PER_MTOK (docs/decisions/0015).
   - fake: the API path with pipeline/fake_client.py (or the client passed in), for dry runs and tests; no network.
7. Budget guard: before every API or fake request (retries included), sum cost_usd of the API requests in the log for
   the current UTC calendar month; once it reaches the monthly budget (budget.monthly_usd in
   constitution/decision-rights.yml), raise BudgetExceeded and do not send the request. Claude Code requests are
   not API spend: they neither count against the budget nor are stopped by it.
8. Logging: every request sent (retries, refusals and errors included) appends one JSON line to
   logs/llm-calls.jsonl: the backend; the model name; prompt_version / rules_version / design_version, the front
   matter versions of the prompt, 00 and 00D; *_revision, the file revision (the last git commit that changed the
   file; "sha256:<content hash>+dirty" when it is uncommitted or modified); the part id, part_id; input_sha256, the
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
import shutil
import subprocess
import tempfile
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
# The claude-code backend gets each model's full output ceiling for every part: the CLI's limit
# (CLAUDE_CODE_MAX_OUTPUT_TOKENS) covers thinking and the reply together, and at effort xhigh a 04A audit thought for
# ~48k tokens before a ~12k reply, so 64000 was not enough (docs/decisions/0024). An unused limit costs nothing.
CLAUDE_CODE_MAX_TOKENS = 128000
MODEL_MAX_OUTPUT_TOKENS = {"claude-haiku-4-5": 64000}  # models whose output ceiling is below 128000
LOG_ENV = "OWNERS_OFFICE_LLM_LOG"  # can point the log at a persistent location (e.g. the private repository in CI)
DEFAULT_LOG_PATH = REPO_ROOT / "logs" / "llm-calls.jsonl"
# Fallback only: constitution/decision-rights.yml (budget.monthly_usd, $50 since docs/decisions/0021) takes
# precedence; this is the design document's original default, used when that file has no budget.
DEFAULT_MONTHLY_BUDGET_USD = 20.0

DEFAULT_EFFORT = "high"
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

# Backends (docs/decisions/0022).
CLAUDE_CODE, API, FAKE = "claude-code", "api", "fake"
BACKENDS = (CLAUDE_CODE, API, FAKE)
BACKEND_ENV = "OWNERS_OFFICE_BACKEND"
DEFAULT_BACKEND = CLAUDE_CODE  # owner decision 2026-09-25: the owner's Claude subscription first, the API as fallback
# Only API spend counts against budget.monthly_usd. The rule is written as an exclusion, so that a log line with no
# backend (written before backends existed) or an unexpected one counts rather than slipping past the guard.
UNBUDGETED_BACKENDS = (CLAUDE_CODE, FAKE)

# The Claude Code CLI (backend claude-code).
CLAUDE_BIN_ENV = "OWNERS_OFFICE_CLAUDE_BIN"
CLAUDE_TIMEOUT_ENV = "OWNERS_OFFICE_CLAUDE_TIMEOUT"  # seconds; the default allows a 128000-token output
DEFAULT_CLAUDE_TIMEOUT = 3600.0
# Where the Claude desktop app installs its copy: <dir>/<version>/claude.app/Contents/MacOS/claude.
DESKTOP_CLAUDE_DIR = Path.home() / "Library" / "Application Support" / "Claude" / "claude-code"
DESKTOP_CLAUDE_EXE = Path("claude.app") / "Contents" / "MacOS" / "claude"
# The CLI's environment is built from scratch: only these variables are copied from the caller's environment. Not
# copied on purpose: ANTHROPIC_API_KEY and ANTHROPIC_AUTH_TOKEN (the CLI would bill them as API calls, outside the
# budget guard), ANTHROPIC_BASE_URL and the other provider settings, and anything a host application set.
CLAUDE_ENV_PASSTHROUGH = (
    "HOME", "USER", "LOGNAME", "PATH", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
    "CLAUDE_CONFIG_DIR",  # the owner's login lives in the default config directory unless this names another one
    "CLAUDE_CODE_OAUTH_TOKEN",  # a long-lived subscription token from `claude setup-token` (unattended runs)
)
OAUTH_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
# Fixed settings: no CLAUDE.md, no auto memory, no total-token reminders, no telemetry, auto-updates or other
# non-essential traffic. CLAUDE_CODE_MAX_OUTPUT_TOKENS is added per call.
CLAUDE_ENV_FIXED = {
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_ORG_MEMORY": "1",
    "CLAUDE_CODE_TOTAL_TOKENS_REMINDER": "off",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
}
# What a stream-json run's init event lists as loaded; every list must be empty.
CLI_LOADED_KEYS = ("tools", "mcp_servers", "skills", "plugins", "slash_commands")
_CLAUDE_VERSION_RE = re.compile(r"^\d+(?:\.\d+)*$")
_PLAN_LIMIT_RE = re.compile(r"usage limit|rate limit|limit reached|out of usage|hit your limit", re.I)
_LOGIN_RE = re.compile(r"not logged in|/login|invalid api key|authentication|oauth token", re.I)

# USD per million tokens: (input, output). Checked on 2026-09-24 against Anthropic's official prices; see
# docs/decisions/0003. A model not in the table cannot be called through the API: its cost could not be recorded, so
# the budget guard would not work.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-sonnet-5": (2.0, 10.0),
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
# Prompt caching, USD per million tokens: (5-minute cache write, cache read). The pipeline uses only the default
# 5-minute cache (CACHE_CONTROL).
# Source: Anthropic's prompt caching prices (the prompt caching section of the Claude API docs, checked 2026-09-25):
# a 5-minute cache write costs 1.25 times the input price and a cache read 0.1 times; the cache read price of
# claude-fable-5-1 is listed separately as 0.25 (i.e. 0.025 times) and that of claude-opus-5-5 as 0.20 (0.05 times;
# Claude API skill, model table of 2026-09-27). Except for those two entries, the numbers in the table
# are computed from PRICES_PER_MTOK with these two multipliers and have not been checked model by model; when prices
# change, edit this table (tests/test_llm.py checks that it agrees with the multipliers).
# A model not in this table cannot be called through the API either.
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.1
CACHE_PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-sonnet-5": (2.5, 0.2),
    "claude-fable-5-1": (12.5, 0.25),
    "claude-opus-5-5": (5.0, 0.2),
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
SERVER_FALLBACK_MODELS = frozenset({"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5"})
FALLBACKS_OFF = frozenset({"", "none", "off", "false", "no"})

# Thinking: these models are sent adaptive thinking explicitly; budget_tokens is never sent (these models reject it
# with a 400). Models outside this set (claude-haiku-4-5) do not support adaptive
# thinking; thinking is not sent for them.
# claude-opus-5-5 and claude-fable-5-1 cannot run with thinking off; effort is their only depth control, and
# claude-opus-5-5 defaults to effort medium, so the pipeline always sends effort explicitly.
ADAPTIVE_THINKING_MODELS = frozenset({"claude-sonnet-5", "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5",
                                      "claude-opus-4-8"})
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
# The variables of 00 §F1 and the prompts README, written {{name}}: company, ticker, status, period, date, document,
# subject.
VARIABLE_RE = re.compile(r"\{\{\s*([^{}\s]+?)\s*\}\}")
VAR_TICKER, VAR_PERIOD, VAR_DOCUMENT, VAR_SUBJECT = "ticker", "period", "document", "subject"
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
    """This month's API spend has reached the budget; the call was not sent."""

    def __init__(self, spent_usd: float, budget_usd: float):
        self.spent_usd = spent_usd
        self.budget_usd = budget_usd
        super().__init__(
            f"this month's API spend of {spent_usd:.4f} USD has reached the budget of {budget_usd:.2f} USD; the call "
            "was not sent. Follow the downgrade order of docs/decisions/0003; raising the budget is a money matter "
            "for the owner to decide."
        )


class LLMRefusal(LLMError):
    """stop_reason == "refusal": the model (including the server-side fallback chain) refused the request."""

    def __init__(self, model: str, category: str | None, explanation: str | None):
        self.model = model
        self.category = category
        self.explanation = explanation
        super().__init__(
            f"model {model} refused the request (stop_reason=refusal, category={category or 'unknown'}). "
            f"{explanation or ''} Per the pipeline rules, open an issue that explains it; do not skip it silently."
        )


class LLMTruncated(LLMError):
    """stop_reason == "max_tokens": the output was truncated. result is still usable; the caller decides whether
    to retry. detail says more when the limit was inferred (the Claude Code CLI continued into a second turn)."""

    def __init__(self, result: LLMResult, detail: str | None = None):
        self.result = result
        self.detail = detail
        super().__init__(
            f"the output hit max_tokens and was truncated ({result.role} / {result.part_id or result.prompt_id}); "
            + (f"{detail}; " if detail else "")
            + "a truncated draft cannot go into the archive. Pass allow_truncated=True if needed."
        )


class LLMOutputInvalid(LLMError):
    """The output is still invalid after one retry (00 §F0, §F6). errors holds the last attempt's errors; result is
    the last reply."""

    def __init__(self, errors: Sequence[str], result: LLMResult):
        self.errors = list(errors)
        self.result = result
        listed = "\n".join(f"- {e}" for e in self.errors)
        super().__init__(
            f"the output of {result.part_id or result.prompt_id} is still invalid after the retry; no output was "
            f"handed over:\n{listed}"
        )


class ClaudeCodeError(LLMError):
    """The Claude Code CLI failed: it could not start, timed out, printed no result, or reported an error. details
    goes into the log line (CLI facts such as the session id and exit status; never prompt or reply text)."""

    def __init__(self, message: str, details: Mapping[str, Any] | None = None):
        self.details = dict(details or {})
        super().__init__(message)


class ClaudeCodeUnavailable(ClaudeCodeError):
    """The CLI cannot be used here: no binary was found, or it is not logged in to a Claude subscription."""


class PlanLimitReached(ClaudeCodeError):
    """The subscription's usage limit was reached. Wait for it to reset, or run the step again with the API backend
    (the fallback; its spend counts against the monthly budget)."""


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
    cost_usd: float  # API cost, summed over all requests (0 for the claude-code backend)
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
    backend: str = API
    notional_cost_usd: float | None = None  # claude-code: the CLI's own cost estimate, summed; not API spend

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
        raise PromptError(f"{what} in {path.name} should be a list of strings")
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
            raise PromptError(f"{path} should be a mapping")
        name = data.get("role") or path.stem
        if name in roles:
            raise PromptError(f"role {name} is defined twice: {roles[name].path.name}, {path.name}")
        model = data.get("model")
        if not isinstance(model, dict) or not isinstance(model.get("id"), str) or not model["id"].strip():
            raise PromptError(f"{path.name} has no model.id (a role's model is set only by agents/*.yml)")
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
        known = ", ".join(sorted(roles)) or "(no role definitions under agents/)"
        raise PromptError(f"unknown role {role!r}; the roles defined in agents/*.yml: {known}")
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
        raise ValueError(f"effort must be one of {EFFORT_LEVELS}, got {chosen_effort!r}")
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
            f"prompts directory {path} not found; name it with the prompts_dir argument or the environment variable "
            f"{PROMPTS_ENV} (the default is {PRIVATE_REPO_NAME}/prompts next to the public repository)"
        )
    return path


def load_prompt(prompt_id: str, prompts_dir: str | os.PathLike[str] | None = None) -> PromptFile:
    """Read <id>-*.md by prompt id and check the front matter's id and version."""
    if not isinstance(prompt_id, str) or not _PROMPT_ID.match(prompt_id):
        raise PromptError(f"a prompt id is two digits and an optional letter (00, 00D, 03, 17), got {prompt_id!r}")
    directory = resolve_prompts_dir(prompts_dir)
    matches = sorted(directory.glob(f"{prompt_id}-*.md"))
    if len(matches) != 1:
        found = ", ".join(p.name for p in matches) or "none"
        raise PromptError(f"prompt {prompt_id} should have exactly one {prompt_id}-*.md in {directory}; found: {found}")
    path = matches[0]
    text = path.read_text(encoding="utf-8")
    parts = _outputs.split_front_matter(text)
    if parts is None:
        raise PromptError(f"{path.name} has no YAML front matter")
    try:
        front = _outputs.load_yaml_text(parts[0])
    except Exception as exc:  # yaml.YAMLError
        raise PromptError(f"the front matter of {path.name} does not parse: {exc}") from exc
    if not isinstance(front, dict):
        raise PromptError(f"the front matter of {path.name} should be a mapping")
    if front.get("id") != prompt_id:
        raise PromptError(f"the front matter id of {path.name} is {front.get('id')!r}, not {prompt_id!r}")
    version = front.get("version")
    if not isinstance(version, (str, int, float)) or isinstance(version, bool) or not str(version).strip():
        raise PromptError(f"the front matter of {path.name} has no version")
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
        raise PromptError(f"{what} should be a list")
    out = []
    for item in value:
        name = str(item).strip()
        required = not name.endswith("?")
        name = name.rstrip("?").strip()
        if not _NAME.match(name):
            raise PromptError(f"{what} has a name that cannot be read: {item!r}")
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
        raise PromptError(f"formats in {what} should be a mapping {{output name: format}}")
    return dict(value)


def output_formats(rules: PromptFile, call: PromptPart) -> dict[str, str]:
    """The format of each output of this part (00 §F0): output_formats from the 00 front matter, overridden by the
    prompt's or part's formats. An output without a declared format, a name declared with two formats, or an output
    with a schema in the wrong format is an error."""
    declared = rules.front.get("output_formats")
    if not isinstance(declared, Mapping):
        raise PromptError(f"the front matter of {rules.path.name} has no output_formats (the §F0 format declaration)")
    by_name: dict[str, str] = {}
    for fmt, names in declared.items():
        if not isinstance(names, list):
            raise PromptError(f"output_formats.{fmt} in {rules.path.name} should be a list of output names")
        for name in names:
            if name in by_name and by_name[name] != fmt:
                raise PromptError(f"{rules.path.name} declares output {name} as both {by_name[name]} and {fmt}")
            by_name[str(name)] = str(fmt)
    by_name.update(call.formats)
    missing = [name for name, _ in call.outputs if name not in by_name]
    if missing:
        raise PromptError(
            f"outputs {', '.join(missing)} of {call.label} have no declared format (output_formats in 00, or the "
            "part's formats)"
        )
    formats = {name: by_name[name] for name, _ in call.outputs}
    problems = _outputs.format_problems(formats)
    if problems:
        raise PromptError(f"the output formats declared for {call.label} are wrong: " + "; ".join(problems))
    return formats


def prompt_part(
    prompt: PromptFile, part: str | None = None, *, pass_no: int | None = None, mode: str | None = None
) -> PromptPart:
    """Resolve from the front matter the part a call runs. part may be a part key (A, B_lite, draft) or a part id
    (04A, 04B-lite, 03R)."""
    front = prompt.front
    parts = front.get("parts")
    if parts is not None and not isinstance(parts, Mapping):
        raise PromptError(f"parts in {prompt.path.name} should be a mapping")
    if parts:
        labels = {k: _part_label(prompt.id, k, v if isinstance(v, Mapping) else {}) for k, v in parts.items()}
        key = part if part in parts else next((k for k, lbl in labels.items() if lbl == part), None)
        if key is None:
            choices = ", ".join(f"{k} ({labels[k]})" for k in parts)
            raise PromptError(f"prompt {prompt.id} runs in parts; part must be one of {choices}; got {part!r}")
        block = parts[key]
        if not isinstance(block, Mapping):
            raise PromptError(f"part {key} in {prompt.path.name} should be a mapping")
        role = block.get("role", front.get("role"))
    else:
        if part not in (None, prompt.id):
            raise PromptError(f"prompt {prompt.id} has no parts; do not pass part (got {part!r})")
        key, block, role = None, front, front.get("role")
    label = _part_label(prompt.id, key, block)
    if role == "pipeline":
        raise PromptError(f"{label} is a deterministic pipeline step (role: pipeline) and calls no model")
    if not isinstance(role, str) or not role:
        raise PromptError(f"the front matter of {label} names no role")

    # Multi-pass parts (04B, 09B) give inputs and outputs per pass: inputs_passN, outputs_passN; calls gives the
    # number of passes.
    pass_keys = sorted(k for k in block if isinstance(k, str) and re.fullmatch(r"inputs_pass\d+", k))
    calls = block.get("calls")
    if calls is not None and (isinstance(calls, bool) or calls != max(1, len(pass_keys))):
        raise PromptError(
            f"calls of {label} should be the integer {max(1, len(pass_keys))} (the number of inputs_passN), got "
            f"{calls!r}"
        )
    if pass_keys:
        passes = [int(k[len("inputs_pass"):]) for k in pass_keys]
        if pass_no not in passes:
            raise PromptError(
                f"{label} runs in passes; pass_no must be one of {', '.join(map(str, passes))}; got {pass_no!r}"
            )
        if f"outputs_pass{pass_no}" not in block:
            raise PromptError(f"{label} runs in passes; its front matter must have outputs_pass{pass_no}")
        raw_inputs, raw_outputs = block[f"inputs_pass{pass_no}"], block[f"outputs_pass{pass_no}"]
    else:
        if pass_no is not None:
            raise PromptError(f"{label} runs in one pass; do not pass pass_no")
        raw_inputs, raw_outputs = block.get("inputs"), block.get("outputs")

    # Prompts with modes (02): if the mode lists its own inputs or outputs, use them; otherwise use the prompt's.
    modes = front.get("modes")
    if modes:
        if not isinstance(modes, Mapping) or mode not in modes:
            choices = ", ".join(map(str, modes)) if isinstance(modes, Mapping) else str(modes)
            raise PromptError(f"prompt {prompt.id} has modes; mode must be one of {choices}; got {mode!r}")
        spec = modes[mode] if isinstance(modes[mode], Mapping) else {}
        raw_inputs = spec.get("inputs", raw_inputs)
        raw_outputs = spec.get("outputs", raw_outputs)
    elif mode is not None:
        raise PromptError(f"prompt {prompt.id} has no modes; do not pass mode (got {mode!r})")
    where = f"{label}" + (f" mode {mode}" if mode else "") + (f" pass {pass_no}" if pass_no else "")
    formats = _format_overrides(front.get("formats"), prompt.path.name)
    if block is not front:
        formats.update(_format_overrides(block.get("formats"), label))
    return PromptPart(
        prompt_id=prompt.id,
        key=key,
        label=label,
        role=role,
        inputs=_dedupe(_names(raw_inputs, f"inputs of {where}")),
        outputs=_dedupe(_names(raw_outputs, f"outputs of {where}")),
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
        raise PromptError(f"prompt {prompt.id} needs the variables {', '.join(missing)} (variables)")
    for name in wanted:
        if not isinstance(variables[name], str):
            raise TypeError(f"variable {name!r} must be a string")
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
        f"{name} is not an input of {call.label} (the inputs of {call.label}: {', '.join(declared)})"
        for name in sorted(set(provided) - set(declared))
    ]
    problems += [f"required input {name} is missing" for name, required in call.inputs if required and name not in provided]
    if problems:
        raise PromptError(f"the inputs of {call.label} are wrong: " + "; ".join(problems))
    refused = []
    for name in sorted(provided):
        if name not in (role.can_see or frozenset()):
            refused.append(f"{name} is not in can_see")
        hidden = _hidden_by(name, role.cannot_see or frozenset())
        if hidden is not None:
            refused.append(f"{name} is in cannot_see ({hidden})")
    if refused:
        raise InputRefused(
            f"{call.label} cannot give these inputs to {role.role} ({role.path.name}, 00 §G6): " + "; ".join(refused)
        )


def _validate_inputs(inputs: Mapping[str, str], images: Mapping[str, Sequence[Any]]) -> None:
    if not isinstance(inputs, Mapping) or not isinstance(images, Mapping) or not (inputs or images):
        raise ValueError("inputs must be a non-empty {name: text} mapping (page images go in images)")
    for key, value in inputs.items():
        if not isinstance(key, str) or not _INPUT_KEY.match(key):
            raise ValueError(f"input names may contain only letters, digits, underscores, dots and hyphens: {key!r}")
        if not isinstance(value, str):
            raise TypeError(f"input {key!r} must be a string")
        if key in IMAGE_INPUTS:
            raise ValueError(f"{key} holds page images; pass it as images={{{key!r}: [image file, ...]}}")
    for key, files in images.items():
        if key not in IMAGE_INPUTS:
            raise ValueError(f"only {', '.join(sorted(IMAGE_INPUTS))} is passed as images, got {key!r}")
        if key in inputs:
            raise ValueError(f"{key} appears in both inputs and images")
        if isinstance(files, (str, os.PathLike)) or not isinstance(files, Sequence) or not files:
            raise ValueError(f"images[{key!r}] must be a non-empty list of image files")


def _load_images(images: Mapping[str, Sequence[Any]]) -> dict[str, list[tuple[str, str, str, str]]]:
    """{input name: [(file name, media_type, base64, sha256)]}."""
    loaded: dict[str, list[tuple[str, str, str, str]]] = {}
    for key, files in images.items():
        rows = []
        for item in files:
            path = Path(item)
            media_type = IMAGE_MEDIA_TYPES.get(path.suffix.lower())
            if media_type is None:
                raise ValueError(f"{path.name}: page images must be one of {', '.join(sorted(IMAGE_MEDIA_TYPES))}")
            if not path.is_file():
                raise FileNotFoundError(f"page image not found: {path}")
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


RETRY_MARK = "===== retry: only {} produced again; the other outputs are those of the first attempt ====="


def failed_outputs(errors: Sequence[str], names: Sequence[str]) -> set[str] | None:
    """The outputs the validation errors name; None when an error names none of them (the reply as a whole failed,
    so every output is produced again)."""
    failed: set[str] = set()
    for error in errors:
        hit = {n for n in names if error.startswith((f"{n}:", f"{n}[", f"{n} (")) or f"'{n}'" in error}
        if not hit:
            return None
        failed |= hit
    return failed


def _with_errors(content: str | list[dict[str, Any]], errors: Sequence[str],
                 redo: Sequence[str] | None = None, kept: Sequence[str] = ()) -> str | list[dict[str, Any]]:
    """Retry: append the validation errors to the original request. With `redo`, only those outputs are asked for
    again; the outputs in `kept` passed and are kept as they were."""
    if redo:
        ask = (f"The outputs {', '.join(kept)} passed and are kept as they were: do not produce them again. Produce "
               f"only {', '.join(redo)} again, complete and as originally asked, and avoid these errors:\n")
    else:
        ask = "Produce all outputs of this part again, as originally asked, and avoid these errors:\n"
    note = (
        "<validation_errors>\nAn earlier attempt at this part failed the pipeline's validation (00 §F0, §F6) with the "
        "errors below. That attempt is not shown here. " + ask
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


def _budgeted(record: Mapping[str, Any]) -> bool:
    """Whether a log line counts as API spend: every line except those of the claude-code and fake backends."""
    return record.get("backend") not in UNBUDGETED_BACKENDS


def month_spend(log_path: str | os.PathLike[str] | None = None, now: dt.datetime | None = None) -> float:
    """Total API spend (USD) in the log for the current UTC calendar month. Claude Code and fake requests are not
    API spend and are left out; bad lines are skipped."""
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
            if not isinstance(record, dict) or not _budgeted(record):
                continue
            if not _same_month(record.get("timestamp", ""), now):
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
    """This month's API budget status and downgrade stage: normal → pause_candidates → downgrade_drafting_model →
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


_NO_COST = {"input": 0.0, "output": 0.0, "cache_write": 0.0, "cache_read": 0.0}


def _served_by_fallback(response: Any) -> bool:
    """A fallback_message in usage.iterations means a fallback model took over (a sticky-routed turn has no fallback
    block)."""
    iterations = getattr(getattr(response, "usage", None), "iterations", None) or []
    return any(getattr(entry, "type", None) == "fallback_message" for entry in iterations)


# ---------------------------------------------------------------- Backends: choice and the API path


def resolve_backend(backend: str | None = None) -> str:
    """The backend argument → the environment variable OWNERS_OFFICE_BACKEND → claude-code."""
    chosen = backend if backend is not None else (os.environ.get(BACKEND_ENV) or "").strip() or DEFAULT_BACKEND
    if chosen not in BACKENDS:
        source = "backend" if backend is not None else BACKEND_ENV
        raise ValueError(f"{source} must be one of {', '.join(BACKENDS)}, got {chosen!r}")
    return chosen


def _default_client() -> Any:
    try:
        import anthropic  # lazy import: this module still imports without the SDK installed
    except ImportError as exc:
        raise LLMError("the Anthropic Python SDK is needed: pip install -r requirements.txt") from exc
    return anthropic.Anthropic()  # credentials are read from the environment, not kept in the code


def _fake_client(call: PromptPart, formats: Mapping[str, str], context: Mapping[str, Any],
                 pipeline_fields: Mapping[str, Any] | None) -> Any:
    """The dry-run client (pipeline/fake_client.py): schema-valid placeholders for the part's outputs."""
    from . import fake_client

    fake_context = {"company": context.get("company"), "period": context.get("period"),
                    "run_date": context.get("run_date"), "label": call.label, "domain": "other",
                    "pipeline_fields": dict(pipeline_fields or {})}
    return fake_client.FakeClient(fake_client.placeholder_reply(call.outputs, formats, fake_context))


def _send(client: Any, params: Mapping[str, Any], fallback_kwargs: Mapping[str, Any] | None, stream: bool) -> Any:
    """One request. With server-side fallback it goes through the beta API; streaming uses messages.stream(...) and
    its get_final_message()."""
    api = client.beta.messages if fallback_kwargs else client.messages
    kwargs = {**params, **(fallback_kwargs or {})}
    if stream:
        with api.stream(**kwargs) as response_stream:
            return response_stream.get_final_message()
    return api.create(**kwargs)


@dataclasses.dataclass
class _Reply:
    """One answer, whatever the backend: what complete() validates and logs."""

    model: str
    usage: dict[str, int]
    stop_reason: str | None
    text: str
    served_by_fallback: bool = False
    refusal_category: str | None = None
    refusal_explanation: str | None = None
    log: dict[str, Any] = dataclasses.field(default_factory=dict)  # backend-specific log fields


def _api_reply(response: Any, requested_model: str) -> _Reply:
    served = getattr(response, "model", None) or requested_model
    stop_reason = getattr(response, "stop_reason", None)
    details = getattr(response, "stop_details", None) if stop_reason == "refusal" else None
    # On a refusal, content is empty or only a fragment; the text is not used.
    text = "" if stop_reason == "refusal" else "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text")
    return _Reply(
        model=served,
        usage=_usage_dict(getattr(response, "usage", None)),  # includes the cache write and cache read token counts
        stop_reason=stop_reason,
        text=text,
        served_by_fallback=_served_by_fallback(response),
        refusal_category=getattr(details, "category", None),
        refusal_explanation=getattr(details, "explanation", None),
        log={"request_id": getattr(response, "_request_id", None)},
    )


# ---------------------------------------------------------------- Backend claude-code: the Claude Code CLI


def _version_key(name: str) -> tuple[int, ...]:
    return tuple(int(part) for part in name.split("."))


def find_claude_binary(env: Mapping[str, str] | None = None) -> Path:
    """The Claude Code CLI: OWNERS_OFFICE_CLAUDE_BIN → claude on PATH → the newest version the Claude desktop app
    installed under ~/Library/Application Support/Claude/claude-code/<version>/. Raises ClaudeCodeUnavailable."""
    env = os.environ if env is None else env
    configured = (env.get(CLAUDE_BIN_ENV) or "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not (path.is_file() and os.access(path, os.X_OK)):
            raise ClaudeCodeUnavailable(f"{CLAUDE_BIN_ENV}={path} is not an executable file")
        return path
    on_path = shutil.which("claude", path=env.get("PATH"))
    if on_path:
        return Path(on_path)
    if DESKTOP_CLAUDE_DIR.is_dir():
        versions = sorted((p for p in DESKTOP_CLAUDE_DIR.iterdir() if _CLAUDE_VERSION_RE.match(p.name)),
                          key=lambda p: _version_key(p.name), reverse=True)
        for version in versions:
            exe = version / DESKTOP_CLAUDE_EXE
            if exe.is_file() and os.access(exe, os.X_OK):
                return exe
    raise ClaudeCodeUnavailable(
        f"the Claude Code CLI was not found: set {CLAUDE_BIN_ENV}, put claude on PATH, or install the Claude desktop "
        f"app (it keeps a copy under {DESKTOP_CLAUDE_DIR}); or run with the API backend"
    )


_CLI_VERSIONS: dict[str, str | None] = {}


def claude_code_version(binary: Path, env: Mapping[str, str]) -> str | None:
    """`claude --version` (first word), cached per binary; None when it cannot be read."""
    key = str(binary)
    if key not in _CLI_VERSIONS:
        try:
            proc = subprocess.run([key, "--version"], capture_output=True, text=True, timeout=60, env=dict(env),
                                  check=False)
            words = proc.stdout.split()
            _CLI_VERSIONS[key] = words[0] if proc.returncode == 0 and words else None
        except (OSError, subprocess.SubprocessError):
            _CLI_VERSIONS[key] = None
    return _CLI_VERSIONS[key]


def claude_code_command(binary: Path, model: str, effort: str | None, system_prompt_file: Path, *,
                        images: bool = False) -> list[str]:
    """The CLI arguments. Text requests go in on stdin and come back as one JSON result; a request with page images
    goes in as one stream-json user message, which the CLI accepts only with stream-json output (and --verbose)."""
    args = [str(binary), "--print"]
    if images:
        args += ["--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
    else:
        args += ["--output-format", "json"]
    args += ["--model", model]
    if effort is not None:
        args += ["--effort", effort]
    args += [
        "--system-prompt-file", str(system_prompt_file),  # replaces Claude Code's own system prompt
        "--tools", "",  # no built-in tools
        "--setting-sources", "",  # no user, project or local settings files (hooks, permissions, env, plugins)
        "--strict-mcp-config",  # no MCP servers: none is given with --mcp-config
        "--disable-slash-commands",  # no skills or custom commands
        "--no-session-persistence",  # the conversation is not saved and cannot be resumed
    ]
    return args


def _workspace_env_value(name: str, env: Mapping[str, str]) -> str | None:
    """A value from the workspace .env (the file pipeline.edgar reads the SEC User-Agent from); never logged."""
    from . import edgar  # no model call there; only its .env reader is used

    path = Path(env.get(edgar.ENV_FILE_ENV) or edgar.DEFAULT_ENV_FILE)
    value = edgar._read_env_value(path, name) if path.is_file() else None
    return value.strip() if value and value.strip() else None


def claude_code_env(base: Mapping[str, str], *, max_tokens: int, config_dir: Path | None) -> dict[str, str]:
    """The CLI's environment, built from scratch (CLAUDE_ENV_PASSTHROUGH, CLAUDE_ENV_FIXED). When a long-lived token
    (CLAUDE_CODE_OAUTH_TOKEN) is set, config_dir replaces the owner's configuration directory with an empty one, so
    nothing cached there (such as the account's email address) reaches the model."""
    env = {name: base[name] for name in CLAUDE_ENV_PASSTHROUGH if base.get(name)}
    env.update(CLAUDE_ENV_FIXED)
    env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(max_tokens)
    if config_dir is not None and env.get(OAUTH_TOKEN_ENV):
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    return env


def _stream_json_input(content: list[dict[str, Any]]) -> str:
    return json.dumps({"type": "user", "message": {"role": "user", "content": content}}, ensure_ascii=False) + "\n"


def _parse_cli_output(stdout: str, images: bool) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """(the result object, the init event of a stream-json run)."""
    if not images:
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            return None, None
        return (data if isinstance(data, dict) else None), None
    result = init = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            result = event
        elif isinstance(event, dict) and event.get("type") == "system" and event.get("subtype") == "init":
            init = event
    return result, init


def _served_model(result: Mapping[str, Any], requested: str) -> str:
    usage = result.get("modelUsage")
    if not isinstance(usage, Mapping) or not usage:
        return requested
    if requested in usage:
        return requested

    def output_tokens(name: str) -> int:
        entry = usage.get(name)
        value = entry.get("outputTokens") if isinstance(entry, Mapping) else None
        return int(value) if isinstance(value, (int, float)) else 0

    return max(usage, key=output_tokens)


def _claude_code_reply(
    model: str,
    effort: str | None,
    system_prompt: str,
    content: str | list[dict[str, Any]],
    max_tokens: int,
    *,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> _Reply:
    """One request through the Claude Code CLI, run in an empty temporary directory."""
    base = os.environ if env is None else env
    if not (base.get(OAUTH_TOKEN_ENV) or "").strip():
        token = _workspace_env_value(OAUTH_TOKEN_ENV, base)
        if token:  # the owner keeps the subscription token in the workspace .env, like the SEC User-Agent
            base = {**base, OAUTH_TOKEN_ENV: token}
    binary = find_claude_binary(base)
    images = not isinstance(content, str)
    timeout = timeout if timeout is not None else float(base.get(CLAUDE_TIMEOUT_ENV) or DEFAULT_CLAUDE_TIMEOUT)
    with tempfile.TemporaryDirectory(prefix="owners-office-claude-") as tmp:
        root = Path(tmp)
        workdir, config = root / "cwd", root / "config"
        workdir.mkdir()
        config.mkdir()
        system_file = root / "system-prompt.txt"  # outside the working directory, which stays empty
        system_file.write_text(system_prompt, encoding="utf-8")
        cli_env = claude_code_env(base, max_tokens=max_tokens, config_dir=config)
        details: dict[str, Any] = {"cli": binary.name, "cli_version": claude_code_version(binary, cli_env),
                                   "cli_isolated_config": cli_env.get("CLAUDE_CONFIG_DIR") == str(config)}
        stdin = _stream_json_input(content) if images else content
        try:
            proc = subprocess.run(
                claude_code_command(binary, model, effort, system_file, images=images),
                input=stdin, capture_output=True, text=True, encoding="utf-8", cwd=workdir, env=cli_env,
                timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired:
            raise ClaudeCodeError(f"the Claude Code CLI did not finish within {timeout:g} seconds "
                                  f"({CLAUDE_TIMEOUT_ENV})", details) from None
        except OSError as exc:
            raise ClaudeCodeUnavailable(f"the Claude Code CLI could not start: {exc}", details) from None
    result, init = _parse_cli_output(proc.stdout, images)
    details["cli_exit"] = proc.returncode
    if result is None:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or ["(no output)"]
        raise ClaudeCodeError(f"the Claude Code CLI printed no result (exit status {proc.returncode}): {tail[0][:300]}",
                              details)
    details.update(session_id=result.get("session_id"), num_turns=result.get("num_turns"),
                   notional_cost_usd=result.get("total_cost_usd"), cli_duration_ms=result.get("duration_ms"),
                   terminal_reason=result.get("terminal_reason"))
    if init is not None:
        details["cli_loaded"] = {k: init.get(k) for k in CLI_LOADED_KEYS}
    if result.get("is_error"):
        message = str(result.get("result") or result.get("subtype") or "unknown error")[:500]
        details["api_error_status"] = result.get("api_error_status")
        if result.get("api_error_status") == 429 or _PLAN_LIMIT_RE.search(message):
            raise PlanLimitReached(f"Claude Code: {message}; wait for the plan's limit to reset, or run the step again "
                                   "with the API backend (its spend counts against the monthly budget)", details)
        if _LOGIN_RE.search(message):
            raise ClaudeCodeUnavailable(f"Claude Code: {message}; the owner logs in once with `claude auth login`, or "
                                        f"sets {OAUTH_TOKEN_ENV} from `claude setup-token`", details)
        raise ClaudeCodeError(f"Claude Code reported an error: {message}", details)
    loaded = details.get("cli_loaded") or {}
    if result.get("permission_denials") or any(loaded.get(k) for k in loaded):
        raise ClaudeCodeError("the Claude Code CLI loaded or used tools although none were allowed; the reply is "
                              "not used (check the flags against this CLI version)", details)
    stop_reason = result.get("stop_reason")
    turns = result.get("num_turns")
    if isinstance(turns, int) and turns > 1:
        # No tool was loaded or asked for, so a second turn is the CLI continuing after its output limit: thinking
        # plus reply reached CLAUDE_CODE_MAX_OUTPUT_TOKENS. It is a truncation (LLMTruncated), not tool use.
        details["cli_stop_reason"] = stop_reason
        details["cli_output_limit"] = (f"the Claude Code CLI ran {turns} turns with no tool loaded or used, so thinking "
                                       f"and reply most likely reached its output limit of {max_tokens} tokens")
        stop_reason = "max_tokens"
    usage = _usage_dict(result.get("usage") or {})
    served = _served_model(result, model)
    details["model_usage"] = {name: {k: v for k, v in entry.items() if k in ("inputTokens", "outputTokens",
                                                                            "cacheReadInputTokens",
                                                                            "cacheCreationInputTokens", "costUSD")}
                              for name, entry in (result.get("modelUsage") or {}).items() if isinstance(entry, Mapping)}
    return _Reply(
        model=served,
        usage=usage,
        stop_reason=stop_reason,
        text="" if stop_reason == "refusal" else str(result.get("result") or ""),
        served_by_fallback=served != model,
        log=details,
    )


# ---------------------------------------------------------------- Calls


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
        "company": variables.get(VAR_TICKER),
        "period": variables.get(VAR_PERIOD),
        "run_date": run_date if _DATE.match(run_date) else _utcnow().date().isoformat(),
        "doc": _DOC_OF_PROMPT.get(call.prompt_id) or variables.get(VAR_DOCUMENT),
        "subject": variables.get(VAR_SUBJECT),
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
    backend: str | None = None,
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
    - variables: the values of the prompt's {{variables}}, e.g. {"company": "Microsoft", "ticker": "MSFT",
      "period": "FY2027Q1"} (company is the common English short name, 00 §W7).
    - pipeline_fields: {output name: {key: value}}, fields the pipeline maintains (00 §G8, e.g. thesis's
      trust_level), written into that output before validation.
    - backend: claude-code, api or fake (default: the environment variable OWNERS_OFFICE_BACKEND, else claude-code;
      docs/decisions/0022). client injects an SDK client (or test double) for api and fake; with fake and no client,
      pipeline/fake_client.py answers with placeholders.
    - stream, max_tokens: the API streams by default; the limit is 128000 for 01, 02 and 11 and 64000 for the rest
      (16000 when not streaming). The claude-code backend always gets the model's full output ceiling (128000; 64000
      for claude-haiku-4-5), because its limit covers thinking and the reply together.
    - prompts_dir, schemas_dir: the prompts directory and the thesis-ci schema directory; for the defaults see
      resolve_prompts_dir() and outputs.schema_validator().

    Before any request is sent, it checks the role, part, inputs and variables (PromptError, InputRefused) and, for
    the api and fake backends, the budget (BudgetExceeded). A refusal raises LLMRefusal; a truncation raises
    LLMTruncated (with allow_truncated=True the result is returned as usual, without validating outputs). An invalid
    output is retried once; if it is still invalid, LLMOutputInvalid is raised. A failing Claude Code CLI raises
    ClaudeCodeError (ClaudeCodeUnavailable, PlanLimitReached). Every request sent writes one log line.
    """
    backend = resolve_backend(backend)
    if backend == CLAUDE_CODE and client is not None:
        raise ValueError("client is for the api and fake backends; the claude-code backend runs the Claude Code CLI")
    root = Path(repo_root) if repo_root else REPO_ROOT
    log = _log_path(log_path)
    images = images or {}
    variables = dict(variables or {})
    _validate_inputs(inputs, images)

    spec = role_definition(role, root)
    for field in ("prompts", "can_see", "cannot_see"):
        if getattr(spec, field) is None:
            raise PromptError(f"{spec.path.name} has no {field}; a role must be complete to be called "
                              "(thesis-ci agent.schema.json)")
    pdir = resolve_prompts_dir(prompts_dir, root)
    prompt = load_prompt(prompt_id, pdir)
    call = prompt_part(prompt, part, pass_no=pass_no, mode=mode)
    if call.role != role:
        raise PromptError(f"the role of {call.label} is {call.role} (front matter of {prompt.path.name}), not {role}")
    if prompt.id not in spec.prompts and call.label not in spec.prompts:
        raise PromptError(f"prompts in {spec.path.name} registers neither {call.label} nor the whole of {prompt.id}")
    rules = load_prompt(RULES_ID, pdir)
    formats = output_formats(rules, call)
    files = [name for name, required in call.outputs if required and formats[name] == _outputs.FILE]
    if files:
        raise PromptError(f"{call.label} has to hand over files ({', '.join(files)}), which needs an environment that "
                          "can run code; llm.py exchanges only text")
    check_inputs(call, spec, [*inputs, *images])

    design = load_prompt(DESIGN_ID, pdir) if call.design else None
    system_texts = [rules.text, *([design.text] if design else []), fill_variables(prompt, variables)]
    try:
        validators = _outputs.validators_for([name for name, _ in call.outputs], schemas_dir)
    except _outputs.SchemaUnavailable as exc:
        raise LLMError(f"the outputs of {call.label} are validated against thesis-ci schemas: {exc}") from exc

    model_id, chosen_effort, fallbacks = _model_choice(spec.model, model, effort)
    if backend == CLAUDE_CODE:
        fallbacks = None  # the server-side refusal fallback is an API beta; the CLI does not send it
    elif not _priced(model_id):
        raise ValueError(f"model {model_id} is not in the price table, so its cost cannot be recorded; add it to "
                         "PRICES_PER_MTOK and CACHE_PRICES_PER_MTOK first")

    loaded = _load_images(images)
    content = render_user_content(call, inputs, loaded)
    if _content_bytes(content) + sum(len(t.encode("utf-8")) for t in system_texts) > MAX_REQUEST_BYTES:
        raise ValueError(f"the request is over the {MAX_REQUEST_BYTES // (1024 * 1024)} MB limit (most likely the page "
                         "images are too large); compress the images first")
    hashed_inputs: dict[str, Any] = {**inputs, **{k: [f"{f}:sha256:{d}" for f, _, _, d in v] for k, v in loaded.items()}}
    run = {k: v for k, v in (("part", call.key), ("pass", call.pass_no), ("mode", call.mode)) if v is not None}
    in_hash = input_sha256(prompt.id, "\n\n".join(system_texts), hashed_inputs, run=run)

    if max_tokens is None and backend == CLAUDE_CODE:
        max_tokens = min(CLAUDE_CODE_MAX_TOKENS, MODEL_MAX_OUTPUT_TOKENS.get(model_id, CLAUDE_CODE_MAX_TOKENS))
    elif max_tokens is None:
        max_tokens = (LONG_MAX_TOKENS if prompt.id in LONG_OUTPUT_PROMPTS else STREAM_MAX_TOKENS) if stream \
            else MAX_TOKENS
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
    total_notional = 0.0
    errors: list[str] = []
    kept: dict[str, _outputs.ParsedOutput] = {}  # outputs that passed in the first attempt (partial retry)
    redo: list[str] | None = None
    first_text = ""
    for attempt in (1, 2):
        if backend != CLAUDE_CODE:
            spent = month_spend(log)
            if spent >= budget:
                raise BudgetExceeded(spent, budget)
            if client is None:
                client = _default_client() if backend == API else _fake_client(call, formats, context, pipeline_fields)
        record: dict[str, Any] = {
            "timestamp": _utcnow().isoformat(timespec="seconds"),
            "backend": backend,
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
            "stream": None if backend == CLAUDE_CODE else stream,
            "max_tokens": max_tokens,
            "images": sum(len(v) for v in loaded.values()),
            "input_sha256": in_hash,
            "output_sha256": None,
            "usage": _usage_dict(None),
            "cost_usd": 0.0,
            "stop_reason": None,
        }
        attempt_content = content if attempt == 1 else _with_errors(content, errors, redo, list(kept))
        try:
            if backend == CLAUDE_CODE:
                reply = _claude_code_reply(model_id, chosen_effort, "\n\n".join(system_texts), attempt_content,
                                           max_tokens)
            else:
                request = params if attempt == 1 else {**params, "messages": [{"role": "user",
                                                                               "content": attempt_content}]}
                reply = _api_reply(_send(client, request, fallback_kwargs, stream), model_id)
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"[:500]
            if isinstance(exc, ClaudeCodeError):
                record.update({k: v for k, v in exc.details.items() if k not in record})
                if isinstance(exc.details.get("notional_cost_usd"), (int, float)):
                    record["notional_cost_usd"] = exc.details["notional_cost_usd"]
            _append_log(log, record)
            raise

        record.update(model=reply.model, served_by_fallback=reply.served_by_fallback, usage=reply.usage,
                      stop_reason=reply.stop_reason)
        if backend == CLAUDE_CODE:
            notional = reply.log.pop("notional_cost_usd", None)
            record.update(cost_usd=0.0, cost_breakdown=dict(_NO_COST),
                          notional_cost_usd=round(float(notional), 6) if isinstance(notional, (int, float)) else None)
            total_notional = round(total_notional + (record["notional_cost_usd"] or 0.0), 6)
        else:
            price_model = reply.model if _priced(reply.model) else model_id
            record.update(cost_usd=cost_usd(price_model, reply.usage),
                          cost_breakdown=cost_breakdown(price_model, reply.usage))
            if price_model != reply.model:
                record["price_basis"] = price_model
        record.update({k: v for k, v in reply.log.items() if k not in record})
        total_usage = {k: total_usage[k] + reply.usage[k] for k in total_usage}
        total_cost = round(total_cost + record["cost_usd"], 6)

        if reply.stop_reason == "refusal":
            record["refusal_category"] = reply.refusal_category
            _append_log(log, record)
            raise LLMRefusal(reply.model, reply.refusal_category, reply.refusal_explanation)

        text = reply.text
        record["output_sha256"] = sha256_text(text)
        generated_by = {
            "model": reply.model,
            "backend": backend,
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
            model=reply.model,
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
            backend=backend,
            notional_cost_usd=total_notional if backend == CLAUDE_CODE else None,
        )
        if result.stop_reason == "max_tokens":
            _append_log(log, record)
            if not allow_truncated:
                raise LLMTruncated(result, record.get("cli_output_limit"))
            return result  # truncated replies are not validated and yield no outputs; the caller handles text itself

        declared = [(n, req and n not in kept) for n, req in call.outputs]  # kept outputs need not come again
        parsed, errors = _outputs.parse_reply(
            text,
            declared,
            formats=formats,
            generated_by=generated_by,
            validators=validators,
            pipeline_fields=pipeline_fields,
            where=call.label,
        )
        if kept:  # the retry asked only for the failed outputs; the kept ones stand as they passed
            errors = [e for e in errors if failed_outputs([e], list(kept)) is None]
            parsed = {**kept, **{n: o for n, o in parsed.items() if n not in kept}}
            result = dataclasses.replace(result, text=first_text + "\n\n" + RETRY_MARK.format(", ".join(redo or ()))
                                         + "\n\n" + text)
        if not errors:
            errors = _outputs.coverage_errors(call.label, inputs, parsed)
        if errors:
            record["validation_errors"] = errors[:50]
        _append_log(log, record)
        if not errors:
            return dataclasses.replace(result, outputs=parsed)
        if attempt == 2:
            raise LLMOutputInvalid(errors, result)
        names = [n for n, _ in call.outputs]
        failed = failed_outputs(errors, names)
        if failed is not None and failed != set(names):
            kept = {n: o for n, o in parsed.items() if n not in failed}
            redo = [n for n in names if n in failed]
            first_text = text
    raise AssertionError("unreachable")  # pragma: no cover
