# 0001 Three-repository layout

## Background

DESIGN.md gives each of the three repositories its own job: thesis-ci is an open-source tool anyone can install, owners-office is the public record the owner maintains with it, and owners-office-private holds the full archives and the amounts. Phase 0 has to build the skeletons of these three repositories locally; after that the owner reviews them and they are pushed to the GitHub account `kentian742-creator`. We need to decide how they are laid out locally, how the two archive repositories find each other, at which layer the public/private boundary sits, and which license the code in owners-office uses (DESIGN.md names only two categories, "method documents" and "research content").

## Options

1. One repository, with the private content in an encrypted directory or a submodule.
2. Two repositories: the tool merged into the public archive repository, plus a private repository.
3. Three sibling repositories (the DESIGN.md plan): thesis-ci, owners-office, owners-office-private.

## Decision

Option 3.

- Three sibling directories under one local workspace folder: `thesis-ci/`, `owners-office/`, `owners-office-private/`. The raw materials stay in the workspace's `inputs/` and go into no repository; copies of the PDFs go into the private repository's `reports/`.
- The root of each of the two archive repositories has a `repo.yml`: `visibility` (public / private), `owner: kentian742-creator`, `spec_version: "0.1"`, `counterpart` (the name of the other repository). thesis-ci decides which checks to run from `visibility`; the private repository's lint reads the public repository through `--counterpart ../owners-office` for the cross-repository checks.
- The public repository's CI cannot see the private repository; cross-repository checks run only in the private repository's CI, locally and in the acceptance script.
- Licenses: thesis-ci code MIT, spec CC BY 4.0 (DESIGN.md); owners-office method documents CC BY 4.0 and research content all rights reserved (DESIGN.md), and its glue code (`pipeline/`, `scripts/`, `tests/`, `.github/`) MIT, the same as thesis-ci's code; the private repository is not public. Details in `LICENSE.md`.

## Rationale

- The visibility boundary is the repository boundary. GitHub has no directory-level visibility; a boundary on repositories is the hardest to get wrong, so amounts and prices can't reach the public history through a single configuration mistake.
- Others can install and reuse the tool on its own without getting the owner's archives, and each license is clear.
- `counterpart` gives the cross-repository checks (for example, that every company in the public repository has a `valuation.yml` in the private repository) a clear entry point, while the public CI never needs permission to read the private repository.
- The code is MIT because CC BY does not suit software, and all rights reserved would keep others from reproducing the checks the way the CI runs them.

## Rejected alternatives

- **One repository with an encrypted directory or a submodule**: key management is complex, a single slip writes amounts into the public history for good, and a submodule needs an extra access token in CI.
- **The tool merged into the public archive repository**: anyone who wants the tool would have to pull down the owner's archives; the tool's MIT license and the research content's reserved rights would be mixed together, which also gets in the way of releasing thesis-ci as v1.0 in Phase 4.
- **owners-office code also all rights reserved**: the promise that "anyone can check" would be empty.

## Date

2026-09-24
