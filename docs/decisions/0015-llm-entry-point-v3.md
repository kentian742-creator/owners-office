# 0015 The model-call entry point upgraded for prompt set v3: role table, streaming, versions and revisions, placement, pending valuations, prompt caching

## Background

Prompt set v3 ([0009](0009-prompt-set-v3.md)) needs matching changes in `pipeline/llm.py` (section D of the private repository's `prompts/INTEGRATION-TODO.md`, STATUS to-do T10): the system prompt is assembled from 00, 00D and the specific prompt; inputs and outputs use fixed `<input>`/`<output>` envelopes; inputs are trimmed by role; structured outputs are validated before they are handed over; design review needs to see page images; outputs state what generated them. The old `llm.py` knew only the original six roles, with the mapping from roles to models written in the code; no call streamed, and the output limit was 16000; the log recorded only one "prompt version" (a git hash).

Putting this in place raised several questions that neither the design document nor 00 answers, which had to be decided here: where the role table comes from; which calls stream; how versions are recorded; where to put outputs for which §F2 distinguishes only public and private without giving a path; where the pending version of a valuation lives before 04C approves it; and how to handle the cost of resending 00 (about 15,000 tokens) with every call.

## Options

1. **Role table:** (a) a table in the code (the old way); (b) `agents/*.yml` as the only source.
2. **Requests:** (a) only 01, 02 and 11 stream, the rest don't, with a limit of 16000; (b) everything streams, with a default limit of 64000, and 128000 for 01, 02 and 11.
3. **Versions:** (a) record only the git hash; (b) record only the `version` in the prompt's front matter; (c) record both.
4. **Paths of private outputs:** (a) each caller decides; (b) a placement table.
5. **Pending valuation version:** (a) a separate `proposed` file on the main branch; (b) a pull request.
6. **The repeated cost of 00:** (a) do nothing; (b) put a prompt-caching breakpoint on 00 (and 00D), with a 5-minute cache; (c) a 1-hour cache.

## Decision

1(b), 2(b), 3(c), 4(b), 5(b), 6(b).

- **The role table has one source.** `llm.py` reads the model, effort, fallbacks, `prompts`, `can_see` and `cannot_see` from `agents/*.yml`. An unknown role, a missing `model.id`, a role defined twice, a prompt or part the role has not registered, or a prompt whose front matter names a different role from the one calling it all raise errors before any request is sent. An input name that is not in `can_see`, or that falls in `cannot_see` (compared after stripping part-number suffixes such as `_04A`, the same way as thesis-ci's C-PROMPT-ISOLATION), is likewise refused before the request is sent (00 §G6); removing the corresponding sections from a finished product is done by `pipeline/isolation.py`. All thirteen roles can be called, with two exceptions: the industry researcher has no prompt yet; and the typesetter's 19 has to deliver a PDF and page images, which needs an environment that can execute code, so it does not go through `llm.py` for now.
- **Everything streams.** Every call uses `client.messages.stream(...)` and takes `get_final_message()`; the default output limit is 64000, and 128000 for 01, 02 and 11. Both the drafting and the oversight models run with adaptive thinking, without sending `budget_tokens`; depth is set with `output_config.effort`. The oversight roles enable the server-side refusal fallback as in [0003](0003-model-assignment-and-budget.md).
- **Both versions and revisions are recorded.** Every request (retries included) writes one log line: `prompt_version`, `rules_version` and `design_version` are the front matter `version` of the prompt, 00 and 00D; `prompt_revision`, `rules_revision` and `design_revision` are the git commits that last changed each file, or, when a file is uncommitted or modified, its content hash plus `+dirty`; the line also records the part id `part_id` and the input hash `input_sha256` (covering the complete system prompt, all inputs and parts, the pass and the mode). `generated_by` (the model, the `version` of 00 and of the prompt, the part, the input hash) is injected into the front matter of every Markdown output; files whose schema allows no extra keys (`thesis.yml`, `story.md` and others) are not changed, and `generated_by` is returned together with the result.
- **Output formats are declared by the prompts and strictly validated.** The format of each output (yaml, markdown, text, file) is declared one by one in the front matter of 00; parts with several passes or modes each spell out the inputs and outputs of that pass or mode. `llm.py` checks names, required outputs, YAML, thesis-ci schemas and front matter against the declarations; if anything fails, it retries once with the error messages, and if it still fails, it hands over no output at all. Fields maintained by the pipeline (00 §G8, such as `trust_level`) are passed in by the caller as `pipeline_fields` and written into the output before validation.
- **Placement is written down as data** (and written into 00 §F2 at the same time). The public repository follows thesis-ci SPEC §2; update records are `companies/<ticker>/updates/<run_date>.md`, one each for 03 and 03P. Private repository: dossier `companies/<ticker>/dossier.md` (MSFT's is in the public repository); valuation `companies/<ticker>/valuation.yml` and `valuation.md`; reports `reports/<ticker>/<02|06|07|08|11>/`, where a revision overwrites the file of the same name and the old version stays in the commit history; escalation requests `escalations/<date>-<ticker>-<slug>.yml`; memos `memos/<date>-<ticker>-<slug>.yml`; series ranking `hq/ranking.yml`; the private appendix of the letter `letters/<year-month>-private-appendix.md`; all other outputs `runs/<ticker, or hq for HQ>/<run_date>-<part id>/<output name>.<yml|md>`. `llm.py` only answers where each output should go (`LLMResult.placements()`); writing the files and opening pull requests is the caller's job.
- **The pending valuation version is a pull request.** On the branch, `valuation.yml` and `valuation.md` carry `doc_status: proposed`; after 04C approves and before the merge, this becomes `effective`; what 04C sends back is not merged. The main branch holds only the effective version (00 §V20).
- **Prompt caching.** The system prompt blocks of 00 (and 00D) each carry a 5-minute cache breakpoint, and the specific prompt comes after the breakpoint. Tokens written to and read from the cache are costed at their own prices: writes at 1.25 times the input price, reads generally at 0.1 times, with the read price of `claude-fable-5-1` listed separately; this table is `CACHE_PRICES_PER_MTOK` in `pipeline/llm.py`, and apart from the one entry listed separately, its prices are all computed from these multipliers; when prices change, change the table. Each log line also records `cost_breakdown` (input, output, cache write, cache read).

## Rationale

- **Write the role table once.** The role definitions are already the public governance structure ([0009](0009-prompt-set-v3.md)); another table in the code would drift, and that is how the old `llm.py`, which knew only six roles, fell behind `agents/`. Refusing inputs by `can_see`/`cannot_see` before the call means isolation is more than a convention.
- **Everything streams.** A single v3 output often contains several complete files (03 rewrites the whole `thesis.yml` and `ledger.yml`), so a limit of 16000 is bound to truncate, and a truncated answer is thrown away entirely, and rerunning it costs money again. The limit is only a ceiling and is not what gets billed; streaming calls also avoid the timeout problem of long requests.
- **Versions and revisions each have their use.** The prompt version in §H5 is a git hash; it is exact to the file content and serves reproduction. The front matter `version` is a version number for people, which small revisions after finalization don't change; it serves to say in letters and in `generated_by` which version was used. When the workspace is not yet in git, the revision falls back to a content hash and still allows reproduction.
- **Placement written down as data.** §F2 only distinguishes public and private; if each caller chose its own paths, outputs of the same kind would land in different places, and neither the checks nor the reviews could find them.
- **Pull requests.** At any moment the main branch holds exactly one effective version, while the pending content and 04C's comments stay in the pull request; approval means merging and taking effect, rejection means closing; the history is complete, and nobody can mistakenly cite the pending version as the effective one.
- **5-minute cache.** 00 is the same for every call and is the largest fixed part of every request. Within one earnings event, calls to the same model come one after another (04A, 04B-lite, 14A, 14T and 15B all use `claude-fable-5-1`), so from the second call on this part is paid at the read price; caches are separate per model, so drafting and oversight each benefit.

## Rejected alternatives

- **Role table in the code:** see above; it would drift from `agents/`.
- **Stream only 01, 02 and 11:** the outputs of parts such as 03 are just as long, and the non-streaming limit is not enough.
- **Record only the git hash:** hard to cite in letters and documents; **record only the front matter version:** can't reproduce.
- **Store the pending valuation as a second file on the main branch:** two versions would coexist on the main branch and one could easily be cited as the effective version, which conflicts with "only one effective version".
- **1-hour cache:** its write price is 2 times the input price (the 5-minute cache's is 1.25 times), while the calls within one event mostly come one after another within 5 minutes.
- **No caching:** 00 would be charged at the full input price every time, a fixed cost that can be saved.

## Date

2026-09-25
