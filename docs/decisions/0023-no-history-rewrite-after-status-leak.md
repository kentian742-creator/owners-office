# 0023 The public history is not rewritten to remove the STATUS leak

> Follows the mistakes entry of 2026-09-25 ("private information in a public file"). This record does not repeat the
> leaked detail.

## Background

From its first commit until hotfix `abcff6b` (2026-09-25), the public `docs/STATUS.md` named how the grades given to
two companies' prices changed. Those grades belong only in the private repository (hard rule H4). The hotfix removed
the line from the current files, the check that caught it now scans `docs/STATUS.md`, and the mistake is in
`mistakes.md`. The line is still in the public git history.

Removing it from history means rewriting every commit up to the hotfix (the line was there from the first commit) and
force-pushing `main`. The owner delegated the decision on 2026-09-25 ("weigh it and decide").

What was known when deciding:

- The repository had been public for less than a day, with no forks, stars or watchers.
- The leaked detail is two letter grades, one step each, with no price, value or number behind them in that line; they
  describe a single day and are already out of date as the grades are recomputed.
- Records cite commit ids from that stretch of history: `mistakes.md` (and its Chinese version) and the private
  repository's run records.
- A force-push does not purge the old commits from GitHub: they stay reachable by id until GitHub Support removes them,
  and any clone made in the meantime keeps them.

## Options

1. Rewrite the history (remove the line from every commit), force-push `main`, update every cited commit id, and ask
   GitHub Support to purge the old commits.
2. Keep the history as it is. The current files are clean, the mistake is recorded, and the check is extended.

## Decision

Option 2.

- The history of `owners-office` is not rewritten, and `main` is not force-pushed.
- If a concrete harm from the old line comes to light (for example a legal request), the owner can reopen this and
  choose option 1. That would be a new decision.
- Future leaks are handled by prevention (the public-content check, STATUS reporting private changes only as counts)
  and by the mistakes list, not by rewriting history.

## Rationale

- The public archive is useful because it is append-only: pre-registrations, errors and corrections are all there to
  be checked against what came later. A rewrite on day one would set the precedent that history can be edited when it
  is inconvenient, which costs more trust than the old line could.
- The harm is small: nobody had forked or followed the repository, and the detail is two stale letter grades with no
  numbers behind them.
- A rewrite would not fully work without GitHub Support, and it would break the commit ids that `mistakes.md` and the
  run records cite.

## Rejected alternatives

- **Rewrite now while the repository is new:** this is the cheapest time to do it, but it still breaks the cited ids,
  still leaves the old commits reachable, and still sets the precedent.
- **Delete and recreate the repository:** the same costs as a rewrite, and it also loses the CI history.

## Date

2026-09-25
