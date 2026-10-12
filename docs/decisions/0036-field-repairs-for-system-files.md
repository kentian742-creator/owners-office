# 0036 A retry repairs the fields of thesis.yml and ledger.yml instead of writing them again

> Follows [0015](0015-llm-entry-point-v3.md) and [0028](0028-archives-from-sec-filings.md). Code: `complete` and
> `_with_errors` in `pipeline/llm.py`; `fact_fields`, `repair_fields`, `align_story` and `story_errors` in
> `pipeline/outputs.py`; `_build_fields` in `pipeline/registry.py`.

## Background

`pipeline/llm.py` validates every reply and retries once with the errors (0015). Since 2026-09-29 the retry asks for
less than the first request: an output that passed is kept, and Markdown prose whose only fault is untagged fact
numbers is repaired sentence by sentence instead of being written again; the valuation's notes have had the same
repair, with tags only, since 2026-10-02. The system files of 01B and 03 had no such repair. Since 2026-10-10 the run
checks their free text for untagged numbers, as thesis-ci's C-SRC-FACT does at placement, so one untagged number in a
note sent the whole thesis.yml or ledger.yml back to be written again.

Walmart's 01B of 2026-10-11 failed three times this way. The first reply of each attempt failed on untagged numbers in
notes and claims, often a threshold restated in a note, or on a thesis without `as_of`. The retry wrote the whole
thesis again, 30 to 40 KB each time, and the new version failed on other untagged numbers or on a missing `as_of`.
Costco's 01B needed a second run for the same reason. Walmart's fourth attempt passed the run's checks and then failed
placement: the front matter of its story said slow_grower, its thesis stalwart (thesis-ci C-STORY).

## Decision

1. **Field repairs.** When every error of thesis.yml or ledger.yml is an untagged number in a free-text field, which
   the error names by its path (`tests[3].note`), the retry does not ask for the file again. It lists each such field
   with its current text and its errors, and asks for `<output name="thesis_repairs">` (or `ledger_repairs`): a YAML
   mapping from each listed path to the field's complete new text. In the new text each fact number has its source
   tag, and a number that is not a fact, such as a threshold the test's `fail_if`, `warn_if` or `rule` already holds,
   is left out.
2. **Applied to the first attempt.** The pipeline sets each listed field in the first attempt's data and rewrites only
   that field's scalar in the text, in its old quoting where the new text fits it, so every other byte of the file
   stays as the model wrote it; when the text cannot be patched in place, the data is written out whole. The repaired
   file is then validated as a new output: its schema with the pipeline's fields, its source tags and the run's other
   checks. A path that was not listed, or a new text that is not a string, is an error.
3. **Mixed faults are written again.** A thesis or ledger with any other error, such as a schema error or a source the
   dossier does not cite, is produced again whole, as before. Its untagged numbers are now listed with its schema
   errors, so the new version is told of both.
4. **Still one retry.** A field repair is a narrower retry, not an extra one: 00 §F0 and §F6 are unchanged, a call
   sends at most two requests, and an output still invalid after the retry fails the call.
5. **`as_of` is the pipeline's.** Prompt 01 says the pipeline fills the header fields of thesis.yml, `as_of` among
   them, so the model is right to leave it out; the pipeline did not fill it, and Walmart's thesis failed its schema.
   01B now writes the run date, for a new archive and for a rebuild: the system files are current as of the day 01B
   writes them, which is also the date of every part in `reviewed`. Filling the field removes the error, so missing
   fields are not part of the field repairs.
6. **The story agrees with the thesis.** The run checks the story beside the thesis with thesis-ci's own C-STORY, as
   the placement lint will. Where the story's front matter differs from thesis.yml on company, status or category,
   the story takes the thesis's value: the company and status are the pipeline's (00 §G8), and the thesis's category
   is the one its tests were built from. The category goes the other way when the thesis's own template tests
   (`origin: template:<category>`) name the story's category, which makes the thesis's field the odd one out. Only
   that scalar is rewritten; the change is noted in the call's log line (`aligned`) and the field is listed among the
   output's pipeline fields. It needs no retry. What C-STORY still finds, such as a story over 350 words, gets the
   retry, which writes the story again on its own.

## Rationale

- A retry that rewrites a long file trades its errors for new ones: the model writes some 35 KB again to change three
  sentences, and every new sentence can carry a new untagged number. A field repair changes only the fields that
  failed, and the rest of the file keeps the version that passed every other check.
- The field is the natural unit of a YAML file: the error already names it by its path, the model's answer can change
  nothing else, and the pipeline knows exactly what changed.
- The new text is free, not tags only as in the valuation's notes: in a test's note the right repair is often to take
  out a restated threshold, which a tags-only repair cannot do.
- Filling a field that the prompt gives to the pipeline follows 00 §G8; asking the model for it would ask the model to
  go against its prompt.

## Rejected alternatives

- **A second retry.** It costs another long request and is no more likely to converge: Walmart's three failed
  attempts were six requests.
- **Field repairs for schema errors as well**, such as asking for a missing field. A missing or misplaced key usually
  shows a misread structure, which a patch of single fields cannot set right; and `as_of`, the case that occurred,
  belongs to the pipeline.
- **The whole thesis-ci lint on every reply.** Most checks need the whole repository; C-STORY is the cross-file check
  that a single reply can fail, and it runs on its own.
