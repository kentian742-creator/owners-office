# Owner's Office · Standing rules

This is the public repository of Owner's Office: the investment constitution, decision rights, agent definitions, each company's thesis.yml and two-minute story, pre-registrations, forecasts, say-do ledgers and letters to the owner. The system tracks the companies, and it also tracks whether the owner's (Ken's) judgment as an owner is reliable. `docs/DESIGN.md` is authoritative for the design; the sibling repository `../thesis-ci/spec/` is authoritative for file formats.

## How a new session picks up the work

When the owner says "keep going":

1. Read `docs/STATUS.md` and start with the first open item of the current phase checklist ("Phase 1 checklist");
2. Read the relevant sections of `docs/DESIGN.md` and `docs/decisions/` only when you need background;
3. After each piece of work, update `docs/STATUS.md` (progress, to-dos, blockers) so the next session can continue.

## Scope of authority (from the kickoff instructions in DESIGN.md)

- Make every engineering decision within the current phase yourself: directories, schemas, scripts, tests, CI.
- Details the design document does not cover: decide them yourself according to the principles in `constitution/`, and record them in `docs/decisions/`.
- Stop and ask the owner about only two kinds of things: operations involving money, and amendments to the investment constitution. Don't ask about anything else.
  - Money operations include buying, adding, trimming and selling, and also raising the model budget (see `docs/decisions/0003`).
  - Amending the constitution means changing the rules themselves in `constitution/`; changing only the format or adding check mappings does not count.
- Things only the owner can do go into the STATUS to-do list, and you don't do them on the owner's behalf: putting the model API key into GitHub Secrets (never paste it into a chat or into code), logging the Claude Code CLI in to the owner's subscription (`claude auth login`) or creating its long-lived token (`claude setup-token`, stored only as the private repository's Actions secret `CLAUDE_CODE_OAUTH_TOKEN`), and creating and pushing the GitHub repositories for the first time after review.
- The User-Agent for SEC access lives only in `.env` at the workspace root (`SEC_USER_AGENT`), never in any repository, log or commit (`docs/decisions/0013`).
- The system will not and cannot trade for the owner; the default option for money matters is always the status quo.

## Hard rules (as worded in the kickoff instructions; enforced by CI)

| Hard rule | Check that enforces it |
| --- | --- |
| Every number in the archive must carry a source tag; | `C-SRC-TAG`, `C-SRC-FACT` (`C-SRC-ACCESSION` warns) |
| Don't fetch or display daily stock prices; prices are used only for an alert when a value range is crossed; | `C-NO-PRICE-FEED` |
| No buy or sell advice in public content; execute no trades; | `C-PUBLIC-NO-ADVICE`, `C-NO-TRADING` |
| Value ranges and L3 memos are stored only in the private repository; the public thesis.yml contains no value_ranges; | `C-PUBLIC-NO-VALUATION`, `C-PUBLIC-NO-AMOUNTS` |
| Model calls may live only in pipeline/llm.py, which records the model name, prompt version and input hash. | `C-LLM-ENTRY`; the log fields are covered by `tests/test_llm.py` |

The detailed price rules are in 00 §H2: only four kinds of prices may appear, and all of them except a company's disclosed historical average buyback price appear only in private files. Secrets go only into GitHub Secrets, and `C-NO-SECRETS` blocks them anywhere else. The checks are defined in `../thesis-ci/spec/checks.yml`. Content that fails CI is not merged; don't change a check just to get past it; to change one, write a decision record first.

## Conventions for writing the archives

- Source tags are written `[src:TAG#LOCATOR]` and named per `../thesis-ci/spec/SPEC.md` §3.3: own reports `<ticker>-RPT<number>-<date>`, periodic reports `<ticker>-<form>-<fiscal period>` (e.g. `AXP-10Q-FY2026Q2`), current reports `<ticker>-<form>-<EDGAR filing date>`. The locator `pN` of a report is the PDF page number (`docs/decisions/0006`). Tags must be registered in the `sources.yml` in the file's directory or at the repository root.
- Never make up numbers: without a source, leave the value empty (`null`) and record a to-do. Quote at most one sentence of source text in any one place.
- The public repository contains no prices, value ranges, returns or multiples derived from prices, or position amounts; these go into `../owners-office-private/` (00 §H4, `docs/decisions/0004`).

## Language

- English first: every file outside `zh-CN/` is in English and never mixes in Chinese (the only exceptions are test data that exercises Chinese text and the linter's Chinese detection lists).
- Chinese versions of the key documents live in `zh-CN/` at the same relative path (for example `zh-CN/README.md`, `zh-CN/docs/DESIGN.md`); which documents are key is listed in `docs/decisions/0020`. Each starts with a line linking to its English file, and the English file links to it; the exception is `zh-CN/docs/DESIGN.md`, the owner's original, which stays byte-identical to `inputs/DESIGN.md` (acceptance A2) and gets no such line.
- When a key document changes, update its `zh-CN/` counterpart in the same commit.
- Commit messages are in English.

## File map

| Path | Contents |
| --- | --- |
| `docs/DESIGN.md` | Design (authoritative) |
| `docs/STATUS.md` | Progress, to-dos, how to pick up the work |
| `docs/decisions/` | Decision records: background, options, decision, rationale, rejected alternatives, date |
| `constitution/` | Investment constitution, the masters' principles, rules and their check mapping, the three decision levels |
| `agents/` | Each role's model, visibility and prompt numbers |
| `companies/<TICKER>/` | thesis.yml, story.md, sources.yml, prereg/, ledger.yml, updates/ |
| `trust/levels.yml` | Each role's trust level (maintained by the pipeline, checked by `C-TRUST-WRITE`) |
| `industries/<id>/` | Industry modules and signposts (no holdings, no recommendations) |
| `forecasts/`, `letters/`, `mistakes.md` | Forecasts and overrides, letters to the owner, mistakes list |
| `pipeline/llm.py` | The only model-call entry point; backends `claude-code` (default, the owner's subscription) and `api` (fallback, budgeted) (`docs/decisions/0022`) |
| `pipeline/runner.py`, `pipeline/registry.py` | Pipeline runner and its input registry: assemble a bundle, execute it, place the outputs (`docs/decisions/0019`, `0024`) |
| `pipeline/chain.py`, `pipeline/documents.py`, `pipeline/evaluation.py` | After an earnings event: the chain of steps with review stops, the documents a step reads, the evaluation of the quantitative tests (`docs/decisions/0024`) |
| `scripts/accept.py` | Phase acceptance script |
| `zh-CN/` | Chinese versions of the key documents, at the same relative paths |
| `../owners-office-private/` | Valuations, L3 memos, escalation requests, series ranking (`hq/`), decision log, PDF reports, prompts v3 (`prompts/`) |
| `../thesis-ci/` | Format spec, lint, selftest |

## Common commands (from the workspace root `/Users/asuka/OwnersOffice`)

```bash
.venv/bin/thesis-ci lint owners-office
.venv/bin/thesis-ci lint owners-office-private --counterpart owners-office
.venv/bin/thesis-ci checks
.venv/bin/thesis-ci selftest
.venv/bin/python -m pytest -q owners-office/tests
.venv/bin/python owners-office/scripts/accept.py --phase 0 --write-report docs/acceptance/phase-0.md
# In the owners-office directory:
../.venv/bin/python -m pipeline.edgar next-release APP FY2026Q3            # expected release date, deadline, latest merge time
../.venv/bin/python -m pipeline.edgar next-release APP FY2026Q3 --announced 2026-11-05   # after the company announces the date
../.venv/bin/python -m pipeline.edgar check-sources companies/APP/sources.yml
../.venv/bin/python -m pipeline.timestamp status companies/APP/prereg/FY2026Q3.yml
# Pipeline runner (docs/decisions/0019, 0022), one prompt part per bundle:
../.venv/bin/python -m pipeline.runner steps
../.venv/bin/python -m pipeline.runner dry-run 15A APP FY2026Q3 --run-date 2026-10-20    # fake backend, into ../work/pipeline-dry-run/
../.venv/bin/python -m pipeline.runner assemble 15A APP FY2026Q3 --run-date 2026-10-20   # bundle in ../owners-office-private/runs/
../.venv/bin/python -m pipeline.runner execute runs/APP/2026-10-20-15A                   # backend claude-code (default)
../.venv/bin/python -m pipeline.runner execute runs/APP/2026-10-20-15A --backend api     # fallback: Anthropic API, counts against the budget
../.venv/bin/python -m pipeline.runner show runs/APP/2026-10-20-15A
../.venv/bin/python -m pipeline.runner place runs/APP/2026-10-20-15A --check
# After an earnings event (docs/decisions/0024): the whole chain, with stops for review; resumable
../.venv/bin/python -m pipeline.runner event AXP FY2026Q3 --run-date 2026-10-21 --dry-run   # fake backend; a rehearsal before the event
../.venv/bin/python -m pipeline.runner event AXP FY2026Q3 --run-date 2026-10-21             # runs until the next stop
../.venv/bin/python -m pipeline.runner event AXP FY2026Q3 --run-date 2026-10-21 --approve draft   # after reviewing the draft
../.venv/bin/python -m pipeline.runner evaluate AXP FY2026Q3 --run-date 2026-10-21          # the quantitative tests alone (ci_results)
../.venv/bin/python -m pipeline.runner place runs/AXP/2026-10-21-03R --publish              # a staged update, after HQ's review (00 §G9)
```

Model backends (`docs/decisions/0022`): `claude-code` runs the Claude Code CLI in print mode on the owner's Claude subscription (no tools, settings, MCP, skills, CLAUDE.md or memory; an empty temporary directory); the CLI is `OWNERS_OFFICE_CLAUDE_BIN`, else `claude` on the PATH, else the copy the Claude desktop app installed, and it must be logged in (`claude auth login`, the owner's action). `api` is the fallback (plan limits reached, unattended runs in the private repository's Actions) and is the only spend that counts against `budget.monthly_usd`. `OWNERS_OFFICE_BACKEND` sets the default; `fake` is for dry runs and tests.

The python.org Python on this machine has no CA certificates: when access to EDGAR or the OpenTimestamps calendars fails, put `SSL_CERT_FILE=/etc/ssl/cert.pem` in front of the command. When running lint locally, `ots` must be on the PATH (use `.venv/bin`); otherwise timestamp proofs after the deadline are only reported as "cannot verify".

While a repository has no commits yet, run acceptance with `--allow-uncommitted` (the result counts only as a provisional pass). Once acceptance passes, move on to the next phase without the owner's sign-off; a failure goes into the letter.

## Commit conventions

- One commit does one thing; the subject is `<scope>: <summary>`, for example `AXP: update the 2026Q3 thesis`, `pipeline: budget guard`, `decisions: 0008 ...`.
- The last line of every commit message must be:

  ```text
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  ```

- The commit author email is the GitHub noreply address (each repository's local `git config user.email`), not a personal email: a public repository's commit history is public content too.
- PRs that change a thesis get the `thesis change` label; changing your mind counts as an achievement, not a stain.
- Files from the private repository must never be copied into this repository; run lint and the tests before pushing.

## Decision records

Things the design document doesn't cover and that you have to decide yourself are written up as `docs/decisions/NNNN-slug.md` (numbered in sequence), with a fixed structure: Background / Options / Decision / Rationale / Rejected alternatives / Date. When overturning an earlier decision, write a new record and note "Superseded by NNNN" in the old one.
