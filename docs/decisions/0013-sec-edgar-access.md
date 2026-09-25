# 0013 EDGAR access: the User-Agent carries the contact email the owner provided, stored only in the local .env

> Supersedes [0005](0005-edgar-accession-deferred.md). This record does not state the email address.

## Background

The SEC's fair access rules require automated requests to state contact details in the User-Agent and to limit the request rate. Earlier in Phase 0, requests that did not declare an identity were blocked by EDGAR, so 0005 decided to defer: the accession numbers of periodic reports in the sources tables were left empty for now, the C-SRC-ACCESSION warning was accepted, and we waited for the owner to provide a contact email, because without the owner's consent the owner's email is not used to declare an identity to third parties.

On 2026-09-24 the owner provided a contact email for the SEC User-Agent. Fetching submissions data from EDGAR has worked since.

Where Phase 1 needs EDGAR: the timing check of pre-registrations uses the `acceptanceDateTime` of the earnings 8-K (Item 2.02) or 6-K; 15A needs the dates on which the earnings releases for the same fiscal quarter of the past three years were filed (`release_history`); the sources tables need their accession numbers backfilled.

## Options

1. Write the email into code or a configuration file committed with the repository.
2. Keep the email only in the workspace's local `.env`, read by the caller at run time; in CI, put it into GitHub Actions Secrets or variables.
3. Keep deferring and use a third-party data source instead.

## Decision

Option 2.

- The User-Agent of EDGAR requests states the project name and the contact email the owner provided.
- The email lives only in `.env` at the workspace root: that file is outside the three repositories, and the `.gitignore` of each of the three repositories also ignores `.env`. The email is never written into code, documents, logs, commit messages, PRs or issues. When this runs automatically in GitHub Actions in Phase 2, it goes into Secrets or variables, again not into code.
- The request rate stays far below the SEC's limit, with requests made one company at a time.
- Before the first real use, check the time zone of `acceptanceDateTime`: in the data fetched, this field has a `Z` suffix, while EDGAR's filing date is counted in US Eastern time; if the two don't agree, go by what the check finds, and only then wire it into the timing check of pre-registrations.
- The backfill script fills only `accession`, `filed` and `form` in the sources tables, with every change in the diff; once the backfill is done, the C-SRC-ACCESSION warnings go away.
- 0005 is kept for history, with a note at the top that this record supersedes it.

## Rationale

- The owner agreed to it and provided the email, so the SEC's rules are met, and the timing checks and filing dates Phase 1 needs become available.
- The public repository is public and the email is personal information: kept only in a `.env` that is never committed, even an accidental commit can't bring it into the public history.
- `.env` is outside the three repositories, so scripts in any of them can read the same configuration without each keeping its own copy.

## Rejected alternatives

- **Write it into code or configuration:** personal information would enter the public repository's history, with no way to take it back.
- **Keep deferring and use third-party data:** third-party accession numbers and times can't be verified; the timing check of pre-registrations has to rest on EDGAR's records.

## Date

2026-09-24
