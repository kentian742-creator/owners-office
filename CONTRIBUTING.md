# Contributing to Owner's Office

This repository is one investor's research archive, kept in public so that its method and its record can be checked.
The most useful thing you can do is check it.

## Corrections of fact

Every fact here cites a primary source, and every error found so far is listed in [mistakes.md](mistakes.md). If you
find a number, date or statement that the cited filing does not support, open a
[factual correction](https://github.com/kentian742-creator/owners-office/issues/new?template=factual_correction.yml)
with:

- the file and the sentence (or the test id, such as `AXP-Q4`),
- what the primary source actually says, with a link to it (an SEC filing, a company release, a court or regulator
  document),
- where in that source (page, item or table).

A confirmed correction goes through the same fact audit as the system's own findings and is recorded in
`mistakes.md`.

## Critique of the method

Questions about a thesis test, a rule in the [constitution](constitution/owner.md), a role's design in
[agents/](agents/) or a [decision record](docs/decisions/) are welcome in
[Discussions](https://github.com/kentian742-creator/owners-office/discussions). Say what you would change and why;
a counterexample from a filing is worth more than an opinion.

## Code

The pipeline (`pipeline/`, `scripts/`, `tests/`) takes pull requests for bugs and clarity. Keep `python -m pytest -q`
and `thesis-ci lint .` passing. Problems with the format, the linter or the scoring tools belong to
[thesis-ci](https://github.com/kentian742-creator/thesis-ci).

## Out of scope

- Opinions on share prices, valuations or what to do with a stock: the public archive carries none, by design
  (constitution H3, H4).
- Anything private: positions, account data, personal information.
- Changes to research content without a primary source.

## Licence

Code contributions are licensed under MIT, and contributions to method documents under CC BY 4.0, as
[LICENSE.md](LICENSE.md) states. Research content (`companies/`, `industries/`, `forecasts/`, `letters/`,
`mistakes.md`) stays all rights reserved; a correction you report is recorded with credit if you wish.
