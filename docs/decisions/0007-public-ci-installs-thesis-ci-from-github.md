# 0007 The public repository's CI installs thesis-ci from GitHub

## Background

The owners-office CI has to run `thesis-ci lint`. thesis-ci is a separate public repository (see `0001`); in Phase 0 it has no release yet, and its spec is still v0.x and will change over the earnings seasons. We need to decide how the public CI gets thesis-ci; the private repository's CI needs it too.

## Options

1. Copy thesis-ci's code into owners-office.
2. Reference thesis-ci as a git submodule.
3. Install it with pip from GitHub: `requirements-lint.txt` says `thesis-ci @ git+https://github.com/kentian742-creator/thesis-ci@main`.
4. Publish it on PyPI and install it by version number.
5. Build a reusable GitHub Action in thesis-ci and call it from owners-office with `uses:`.

## Decision

Option 3.

- `requirements-lint.txt` has only this line; the public CI's lint job runs `pip install -r requirements-lint.txt` and then `thesis-ci lint .`.
- The private repository's CI also checks out this repository and installs from the same `requirements-lint.txt`, so both sides use the same version.
- Once thesis-ci has its first version tag, `@main` changes to a fixed tag (for example `@v0.1.0`), so that upgrading thesis-ci becomes a recorded commit.
- When thesis-ci reaches v1.0 in Phase 4, consider publishing it on PyPI or providing a reusable Action.

## Rationale

- There is only one copy of the code, so no copy can drift from the original.
- Anyone can reproduce the CI locally from `requirements-lint.txt`, which delivers on the promise that "anyone can install it".
- No token is needed: thesis-ci is a public repository.
- Submodules and PyPI are extra overhead at v0.x, and version tags already provide reproducibility.

## Rejected alternatives

- **Copying the code**: two copies are bound to drift, and the lint results would disagree with thesis-ci's own tests.
- **git submodule**: checking out and updating are more cumbersome and CI needs an extra step, for the same benefit as a fixed tag.
- **PyPI**: changes are frequent during v0.x, which makes releases costly; wait for v1.0.
- **Reusable Action**: can come later, but that wrapper doesn't exist yet in Phase 0; start with the simplest way to install.

## Consequences

- thesis-ci has to be pushed to GitHub before the owners-office CI can pass (the push order in STATUS to-do T3).
- Until a tag is pinned, an incompatible change on thesis-ci's main branch turns the owners-office CI red immediately; that is exactly why a tag should be set soon.

## Date

2026-09-24
