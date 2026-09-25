# 0005 Backfilling EDGAR accession numbers deferred

> **Superseded by [0013](0013-sec-edgar-access.md) (2026-09-24):** the owner has provided a contact email for the SEC User-Agent (stored only in the local `.env`), EDGAR is reachable, and the accession numbers are backfilled instead. This record is kept for history.

## Background

Section 3.5 of the thesis-ci spec requires source entries with `kind: filing` to carry the EDGAR accession number; a missing one is a warning (`C-SRC-ACCESSION`), not an error, and has to be listed among the to-dos in `docs/STATUS.md`. The SEC's fair access rules require automated requests to state contact details in the User-Agent and to limit the request rate. When Phase 0 tried to read the submissions API for the five companies, the SEC returned its "undeclared automated tool" block page, and no accession number was obtained. The owner's email is used only to identify the owner; without the owner's consent, it is not used to declare an identity to third-party services.

The five companies' CIKs are known: MSFT `0000789019`, AXP `0000004962`, PDD `0001737806` (a foreign private issuer, files 20-F / 6-K), BRK `0001067983`, SPGI `0000064040`.

## Options

1. Put the owner's email into the User-Agent without asking the owner.
2. Write a fake or placeholder contact.
3. Copy the accession numbers from third-party websites.
4. Defer: leave the accession numbers `null` for now, accept the `C-SRC-ACCESSION` warning and list it as a to-do; once the owner provides a contact email, a script backfills the accession numbers and filing dates from the submissions API.

## Decision

Option 4.

- Every source entry with `kind: filing` has `accession: null`; the lint warning stays as a reminder.
- To-do T2 (`docs/STATUS.md`): ask the owner for a contact email for the SEC User-Agent; it can be a dedicated address.
- Once it is available: the User-Agent goes into GitHub Actions variables or Secrets (an environment variable locally), never into code; the request rate stays far below the SEC's limit; the backfill script fills only `accession`, `filed` and `form`, and every change shows up in the diff.
- Timing: this has to be in place before the first earnings release of a holding in Phase 1 (AXP's 8-K, PDD's 6-K), because the timing check of pre-registrations uses EDGAR's `acceptanceDateTime`.

## Rationale

- Using the owner's email to declare an identity means giving personal information to outsiders, which needs the owner's own consent; it is neither a money matter nor an amendment to the constitution, but it is something only the owner can do, so it goes into the to-dos instead of being done on the owner's behalf.
- Fake contact details break the SEC's rules and could get the whole network range blocked, which would make the Phase 2 EDGAR monitoring impossible.
- Accession numbers copied from third parties cannot be verified, which goes against the principle "every number is traceable".
- The spec already makes a missing accession number a warning, so deferring does not block Phase 0 acceptance; the numbers cite the owner's report pages for now (see `0006`), so their sources are still traceable.

## Rejected alternatives

- **Use the owner's email directly**: gives personal information to outsiders without consent.
- **Placeholder contact details**: break the SEC's fair access rules and risk a block.
- **Third-party data**: cannot be verified, and still sidesteps the SEC's rules.

## Date

2026-09-24
