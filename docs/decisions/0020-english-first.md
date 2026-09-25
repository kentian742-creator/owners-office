# 0020 English first; Chinese in a separate zh-CN/ directory

## Background

Until 2026-09-25 the three repositories, the prompt set and the model outputs were written mostly in Chinese, and a few
documents (the README, the agents overview) mixed Chinese and English. The repositories are on GitHub now, two of
them public, and the owner wants them readable by anyone.

The owner decided on 2026-09-25:

- English is the working language everywhere: both public repositories, the private repository, the prompt set, and
  every model output from now on.
- Chinese is the second language. Chinese versions of key documents live in a separate `zh-CN/` directory at each
  repository root; if needed, Chinese versions of prompts and reports can be made there too.
- A file is in one language. Mixing languages in the same document reads badly.
- Repository descriptions say that a Chinese translation exists.

## Options

1. Keep Chinese, add English summaries.
2. Bilingual files (Chinese and English sections side by side).
3. English everywhere; Chinese versions of key documents in `zh-CN/`, maintained alongside.
4. English everywhere; a full Chinese mirror of every file.

## Decision

Option 3.

- **English files:** everything outside `zh-CN/`. The linter enforces it with a new check, `C-LANGUAGE` (thesis-ci
  0.3.0): an English file may not contain CJK text. Test data and the linter's own Chinese detection lists are exempt,
  because they exist to handle Chinese text.
- **Key documents with a Chinese version:** the README, the investment constitution and the masters' principles, the
  design document (its Chinese original, which acceptance check A2 compares byte for byte with the owner's copy), the
  letters, the two-minute stories, the mistakes list, the industry module summaries, and thesis-ci's README and
  specification. When one of them changes, its `zh-CN/` counterpart changes in the same commit.
- **Everything else is English only:** decision records, STATUS, role definitions, data files (`thesis.yml`,
  `ledger.yml`, `sources.yml`, valuations), code, workflows and commit messages.
- **Prompts:** the prompt set is translated to English and stays the version the pipeline runs; the Chinese v3 text is
  kept in the private repository's `zh-CN/prompts/`. Model outputs are English; a Chinese version of a report or letter
  is produced only when asked for.
- **Translation discipline:** translating never changes content. Every translated file is checked mechanically
  against its previous version: numbers (Chinese magnitudes normalised), dates, fiscal periods, source tags, test ids,
  accession numbers and URLs must all be preserved.
- The owner's own reports stay as they are (they are sources, in Chinese); archives cite them by page as before.

## Rationale

- English is the common language of the readers the public repositories are for, and of the filings the archives
  cite; the owner also judged English prompts to work better with the models.
- One language per file keeps both versions clean and makes translation drift visible: a key document and its
  `zh-CN/` counterpart change together or the review catches it.
- A full mirror (option 4) doubles every edit for files no Chinese reader needs, such as data files and code.

## Rejected alternatives

- **Chinese with English summaries:** the substance would stay unreadable to most readers.
- **Bilingual files:** every edit has to change two languages in the same place, and the owner found mixed files
  harder to read.
- **Full mirror:** twice the maintenance for little benefit.

## Date

2026-09-25
