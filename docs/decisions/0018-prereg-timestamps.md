# 0018 Pre-registration timestamps: stamped on merge into main, upgraded every 6 hours, CI checks that the file is unchanged

## Background

thesis-ci SPEC §2.1 requires an OpenTimestamps proof `<file>.ots` next to every pre-registration items file (`<period>.yml` and `<period>-owner.yml`); the design document requires that "an OpenTimestamps timestamp is added at merge, so anyone can independently check that nothing was backdated". After the deadline, `C-PREREG-IMMUTABLE` requires the proof to exist; when the client is on the PATH (from thesis-ci v0.2.1), it first uses `ots info` to compare locally the sha256 the proof commits to with the sha256 of the file, and reports an error if they differ or the proof can't be read; once they match, it runs `ots verify -f <file> <file>.ots`: passing is enough, an error is reported only when the client says the proof itself is broken, and everything else (no Bitcoin node, pending, calendar unreachable, timeout) only warns. STATUS to-do T8 has to be wired up before APP's first pre-registration (results around early November).

Reading the source of opentimestamps-client 0.7.2 (the latest version on PyPI) and testing it showed the following points, which shaped the design:

1. **Stamping** sends only sha256(sha256(file) ‖ 16 random bytes) to the four default calendars, and writes a proof only if at least two of them answer within 5 seconds; neither the file nor its hash leaves the machine. A freshly written proof is "pending": the calendars promise to write it into Bitcoin, and there is usually a block only a few hours later.
2. **Verification** first compares the file's sha256 locally, and a mismatch gives "File does not match original!"; this step needs no network. After that it verifies the Bitcoin confirmation, which requires a Bitcoin node (Bitcoin Core's RPC; a pruned node works too). The client has no block explorer fallback: without a node it only reports "Could not connect to Bitcoin node"; `--no-bitcoin` only prints "check by hand that the merkle root of block N is X". The block time is also printed only after the node verification passes, and only to the day.
3. **Pending proofs** need to contact the calendars when they are verified. When the calendars can't be reached (seen in testing: the local Python lacks CA certificates, so TLS fails), the output does not contain "pending": thesis-ci v0.2.0's `C-PREREG-IMMUTABLE` therefore judged an intact proof an error, and even suggested that "the file was changed after it was timestamped". This false positive was fixed in v0.2.1: the hash is compared locally first, and once it matches, any network failure is only a warning. **Upgraded proofs** don't contact the calendars when they are verified.
4. **Upgrading** (`ots upgrade`), when something changes, renames the old proof to `<proof>.bak` and leaves it in place; it exits with status 1 when the proof is still pending; fetching the upgrade from the calendars has no timeout.

## Options

1. **When to stamp:** (a) the author stamps on the PR branch; (b) a workflow stamps after the merge into main; (c) stamp everything at the deadline.
2. **When to upgrade:** (a) never upgrade, and ask the calendars at verification time; (b) upgrade on a schedule and commit back to the repository.
3. **How CI verifies the Bitcoin confirmation:** (a) run a Bitcoin node in CI; (b) query a block explorer API; (c) don't verify the Bitcoin layer, only check that the file and the proof match, and leave the Bitcoin layer to anyone with a node.
4. **A proof exists but the file's content has changed:** (a) restamp automatically and overwrite; (b) refuse; when a change before the deadline is really needed, delete the old proof in the same PR.
5. **The deadline has passed:** (a) stamp anyway; (b) refuse.

## Decision

1(b), 2(b), 3(c), 4(b), 5(b).

- **`pipeline/timestamp.py`**: `stamp(paths)`, `upgrade(paths)`, `status(path)`, `verify(path)`, command line `python -m pipeline.timestamp stamp|upgrade|status|verify [file…] [--prereg]`. Every step calls the `ots` client and parses its output, without reading or writing the proof format itself; the timestamped files are only read, never written.
  - `stamp` writes the proof next to the file, and does nothing when a proof for the same content already exists. It refuses when the items file's deadline has passed (including the deadline moment itself) or can't be read (missing, without a time zone, duplicate keys), or when `<file>.ots` already exists but proves different content, or can't be read. After stamping, it reads the new proof again to confirm that it commits to exactly this content.
  - `upgrade` runs on a temporary copy and replaces the original proof atomically, only when something changed, so no `.bak` is left in the repository; a proof that is still pending is not a failure, but no calendar answering is.
  - `status` reports missing / pending / attested / error offline: whether the sha256 the proof commits to matches the file's current one, the pending calendars, the Bitcoin block height and that block's merkle root; for a confirmed proof it also runs `ots verify` once more, which adds the block date when a node is available.
  - `verify` matches `C-PREREG-IMMUTABLE` (thesis-ci v0.2.1) step for step: first `ots info` to compare the sha256 locally, then `ots verify`; the same two regular expressions (`OTS_INFO_DIGEST_RE`, `OTS_BAD_PROOF_RE`) and the same 120-second limit; when thesis-ci is installed, a test runs the same fake client through both and compares their verdicts one by one: pass, cannot be verified on this machine, fail.
  - Every call is limited to 120 seconds; `--no-cache` is always added, so the result depends only on the proof file itself, like CI's empty cache; `TZ=UTC`, so block dates are printed in UTC.
- **`.github/workflows/timestamp.yml`**: one job. Triggers: a push to main that changes `companies/*/prereg/*.yml`; every 6 hours; manual. Steps: `stamp --prereg` (all items files whose deadline has not passed, skipping those that already have a proof of the same content) → `upgrade --prereg` (all proofs) → if any `*.ots` was newly stamped or upgraded, commit it back to main. The committer is `github-actions[bot]` (GitHub's noreply address), and the last line is the fixed `Co-Authored-By` trailer.
  - Permissions: the workflow default is `contents: read`; only this job has `contents: write`, and it runs only on `refs/heads/main`, so a manual trigger on another branch can't write to main either. The repository's default token permission is read-only, and write permission is granted explicitly here. No secrets are used.
  - No loops: commits pushed with `GITHUB_TOKEN` trigger no workflow; the commits contain only `*.ots`, which doesn't match the trigger's paths; nothing is committed when nothing changed. `concurrency` queues the runs, so they don't run in parallel. The same rule also means lint doesn't run for these commits; they contain only `*.ots`, which gets checked with the next push.
  - If the push is rejected because main has moved on, the commit is dropped and redone from the new main, up to three times, so the proofs always cover the latest content on main; if the push is rejected while main has not changed (for example because branch protection is added later), the job stops with an error.
  - When there is a refusal (a proof exists but the content has changed) or a stamp did not happen (calendars unreachable), whatever can be committed is still committed, and the run ends in failure; what was not stamped is stamped in the next run (at most 6 hours later).
- **The lint job** installs the client, so that `C-PREREG-IMMUTABLE` really verifies the proofs: a file changed after the deadline is an error; no Bitcoin node, still pending or calendars unreachable only warn (from v0.2.1; v0.2.0 would misjudge "pending with the calendars unreachable" as an error).
- **Client version** pinned to `opentimestamps-client==0.7.2` in `requirements.txt`; the lint and timestamp jobs borrow this constraint with `pip install -c requirements.txt opentimestamps-client`. Before upgrading the client, check the output of `ots` against thesis-ci's two regular expressions.
- **Changing an items file before the deadline** (15A: once the company announces its release date, only `event` and `deadline` change, and the items stay word for word): delete the old proof in the same PR (the git history keeps it), and after the merge the workflow timestamps the new content. If the deletion is forgotten, the workflow refuses and ends in failure.
- **Not done this time:** `merged_at` and `ots_proof` in the settlement file are written by the settlement pipeline; nothing checks that the block time is before the deadline (`C-PREREG-IMMUTABLE` doesn't compare them, and CI can't get the block time either).

## Rationale

- **Stamp on merge.** The design document asks for it "at merge". GitHub records the merge time (`merged_at` in the settlement file), while the timestamp is independent of GitHub and proves that the content merged into main existed at that time. 15A requires merging at least 72 hours early, and Bitcoin confirmation usually takes a few hours, so it arrives before the deadline.
- **Scheduled upgrades.** Upgrading is no longer needed to avoid the false positive (thesis-ci v0.2.1 fixed it), but it is still worth finishing before the deadline: a pending proof depends on the calendar servers, and if a calendar goes offline or deletes its data, the Bitcoin layer can never be verified; an upgraded proof carries the whole path to the Bitcoin block header, verification needs only block headers, and anyone can verify it with their own node, without trusting the calendars or this system.
- **CI only checks that the file is unchanged.** What CI can do, and what matters most, is the local hash comparison: for a file changed after the deadline, `C-PREREG-IMMUTABLE` takes the sha256 the proof commits to from `ots info`, compares, and reports an error, without contacting any server. The Bitcoin layer is left to anyone with a node to verify independently; without a node, `status` gives the block height and the merkle root, which can be checked by hand on any block explorer.
- **No overwriting, no stamping after the deadline.** `C-PREREG-IMMUTABLE` only checks that the proof and the file match; it does not compare the block time with the deadline. If a file could be restamped after the deadline, a changed file with a new proof would still pass the check, and the timestamp would mean nothing. So both refusals sit in the only place that writes proofs. A legitimate change before the deadline needs a person to delete the old proof explicitly, and the change is visible in the PR.
- **The time of a proof is the block time.** For a file merged only in the last few hours before the deadline (for example, the owner's rewrite), the block may fall after the deadline, and then its existence before the deadline rests only on GitHub's `merged_at`. The owner's rewrites are best merged at least one day early.

## Rejected alternatives

- **Stamping on the PR branch:** the content still changes before the merge, so every change would need a new stamp; the proof would cover a draft, not the content merged into main.
- **Stamping everything at the deadline:** the Bitcoin block is always later than the deadline, so it can't prove the content existed before the deadline.
- **No upgrades:** verification would depend on the calendar servers forever; if a calendar goes offline or deletes its data, an un-upgraded proof is left with only the "file matches proof" layer, and the Bitcoin layer can never be verified again.
- **Running a Bitcoin node in CI:** a node first has to download and validate the whole chain (a pruned node too), which one CI run can't do.
- **Querying a block explorer:** amounts to trusting a third party; `C-PREREG-IMMUTABLE` accepts only the client's verdict, so the result would still be "cannot verify". When a check by hand is needed, the block height and merkle root from `status` are enough.
- **Overwriting proofs automatically:** a change after the deadline could quietly get a new proof; and a legitimate change before the deadline should be visible in the PR.
- **Stamping after the deadline anyway:** as above: it proves nothing, and it lets the check pass.

## Date

2026-09-25
