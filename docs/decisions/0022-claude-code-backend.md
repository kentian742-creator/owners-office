# 0022 Model backend: Claude Code on the owner's Claude subscription first, the API as fallback

## Background

Until now every model call of the pipeline went to the Anthropic API through `pipeline/llm.py` ([0003](0003-model-assignment-and-budget.md), [0015](0015-llm-entry-point-v3.md)), billed per token and capped by the monthly budget ([0021](0021-model-budget-50.md): $50). The owner also holds a Claude Max plan (5x or 20x), which already pays for Claude Code work: development, translation, audits run as Claude Code agents. The owner asked on 2026-09-25 whether the pipeline can run its model calls there instead, and approved doing so, with the API kept as a fallback.

Constraints:

- **H5:** model calls go only through `pipeline/llm.py`, which records the model, the versions of 00 and the prompt, and the input hash (C-LLM-ENTRY). A call made through Claude Code must be the same call, logged the same way.
- **G6:** the pipeline's calls have no memory and see only the inputs a role may see. An interactive Claude Code session brings its own system prompt, tools, CLAUDE.md files, auto memory, settings, hooks, skills and MCP servers; none of that may reach a pipeline call.
- The Claude Code CLI on this Mac (2.1.280, installed with the Claude desktop app) runs in print mode with a replaced system prompt (`--system-prompt-file`), `--model`, `--effort`, `--tools`, `--setting-sources`, `--strict-mcp-config`, `--no-session-persistence` and JSON output. Its minimal mode (`--bare`) accepts only API keys, never a subscription login, so it cannot serve here.

## Options

1. Keep the API as the only backend.
2. Add a Claude Code backend to `pipeline/llm.py`: the CLI in print mode as a subprocess, with everything but the request turned off; make it the default and keep the API as the fallback.
3. Call Claude Code through its agent SDK from Python.
4. Run the pipeline's steps by hand in Claude Code sessions.

## Decision

Option 2.

- **Choosing the backend.** `llm.complete(..., backend=)` takes `claude-code`, `api` or `fake`; the default is the environment variable `OWNERS_OFFICE_BACKEND`, else `claude-code`. The runner passes its `--backend` through (`python -m pipeline.runner execute <bundle> --backend claude-code|api`). There is no automatic switch to the API: when a call on the subscription fails, the run fails, and the operator reruns it with `--backend api`.
- **The same call.** Both backends send the same system prompt (00, plus 00D when the part is designed, plus the filled prompt) and the same user content (the `<run/>` line and the `<input>` blocks), and have the same input hash, output validation, single retry with the errors appended, `generated_by` (which now names the backend) and placement.
- **How the CLI runs.** `claude --print --output-format json --model <id from agents/<role>.yml> --effort <the role's effort> --system-prompt-file <file> --tools "" --setting-sources "" --strict-mcp-config --disable-slash-commands --no-session-persistence`, with the user content on stdin. The system prompt replaces Claude Code's own; `--tools ""` leaves no tools; `--setting-sources ""` loads no user, project or local settings (and so no hooks or permissions from them); `--strict-mcp-config` without `--mcp-config` loads no MCP servers; `--disable-slash-commands` loads no skills. The process runs in an empty temporary directory, with the system prompt file outside it, and everything is deleted afterwards. Its environment is built from scratch: only the variables it needs to run and to reach the network are copied (home, user, path, locale, temporary directory, certificates, proxies, `CLAUDE_CONFIG_DIR`, `CLAUDE_CODE_OAUTH_TOKEN`), and it gets `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1`, `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, `CLAUDE_CODE_TOTAL_TOKENS_REMINDER=off`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` and the output limit (`CLAUDE_CODE_MAX_OUTPUT_TOKENS`: 128000 for 01, 02 and 11, 64000 otherwise). `ANTHROPIC_API_KEY` is never passed on: with it, the CLI would bill the API outside the budget guard. A request with page images goes in as one stream-json message, which this CLI accepts only with stream-json output. The binary is `OWNERS_OFFICE_CLAUDE_BIN`, else `claude` on the path, else the newest copy the Claude desktop app installed. JSON structured output (`--json-schema`) is not used: the pipeline's own validation covers every output of a reply.
- **Checks on the reply.** A reply is refused (and logged as an error) when the CLI reports more than one turn or a permission denial, or, for stream-json runs, when it lists any loaded tool, MCP server, skill, plugin or command: any of these would mean the flags no longer do what this record says.
- **Logging.** Every CLI request writes the same log line as an API request (model, versions and revisions of the prompt, 00 and 00D, part, input hash, token usage including cache writes and reads, stop reason, attempt), with `backend: claude-code`, `cost_usd: 0` and a zero `cost_breakdown`. The CLI's own cost estimate goes into a separate field, `notional_cost_usd`, with the session id, the CLI version, the number of turns and the tokens per model. Errors (not logged in, a plan limit, a timeout, no result) are logged with the CLI's exit status.
- **Budget.** Only API spend counts against `budget.monthly_usd`: the guard sums every log line except those of the `claude-code` and `fake` backends (lines written before this record carry no backend and count). Claude Code calls are neither counted nor stopped by the guard. They share the plan's five-hour and weekly usage limits with the owner's own work; when a limit is reached, the run fails with `PlanLimitReached`, and a rerun with `--backend api` is spending that counts against the budget.
- **Refusal fallback.** The API's server-side refusal fallback for the oversight roles (0003) is an API beta that the CLI does not send; on the Claude Code backend a refusal ends the run as `LLMRefusal`, as on the API when the fallback chain also refuses.
- **What still reaches the model.** Checked on 2026-09-25 by pointing the CLI at a local capture server instead of the real service: beyond the system prompt and the inputs, this CLI version adds a billing line and one sentence ("You are a Claude agent, built on Anthropic's Claude Agent SDK.") before the system prompt, and a system reminder with the working directory (the empty temporary directory), platform, shell, OS version, the model's name and knowledge cutoff, and the date. When the CLI runs on the owner's interactive login, it also adds the account's email address as context; with a long-lived token (below) the configuration directory is an empty temporary one and the email is not sent. None of this is archive content; no flag of this version removes it.
- **Logging in.** Local runs use the owner's subscription: the owner logs in once with `claude auth login` (or sets a long-lived token). On 2026-09-25 the standalone CLI on this Mac was not logged in; the Claude desktop app's own sessions do not log it in.
- **Unattended runs later.** For the private repository's Actions (phase 2), the owner can create a long-lived subscription token with `claude setup-token` and store it as the Actions secret `CLAUDE_CODE_OAUTH_TOKEN` of owners-office-private; `llm.py` passes it to the CLI and gives the CLI an empty configuration directory. Until then the Actions route runs on the API. Model calls never run in a public repository's Actions (the runner refuses).

## Rationale

- **The plan is already paid.** Running the pipeline on it leaves the API budget for the fallback and for unattended runs, and the candidates' quarterly updates no longer compete with the holdings for it.
- **The controls stay where they are.** The call still goes through `llm.py` (H5); the same prompts, inputs, isolation, validation and log apply; the flags and the environment turn off everything a Claude Code session would add, and the reply checks refuse a run in which they stop working.
- **Tokens are comparable across backends.** Usage is logged the same way on both, and the notional cost shows what a month would have cost through the API.
- **No automatic fallback.** Spending money is not automatic; the default for a money matter is the status quo.

## Rejected alternatives

- **API only:** pays per token for work the plan already covers.
- **`--bare` mode:** accepts only API keys, not the subscription.
- **The agent SDK from Python:** adds a dependency and an agent loop around a request that is a single turn; the CLI's flags already give one turn with nothing loaded.
- **Steps run by hand in Claude Code sessions:** not reproducible or logged (H5), and the session's CLAUDE.md, memory and tools would reach the model (G6).
- **Automatic fallback to the API when the plan's limit is reached:** spends money without a decision.

## Date

2026-09-25
