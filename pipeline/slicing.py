"""Slices: one prompt part run as several model calls when its input is too large for one (docs/decisions/0026).

The rehearsal of the post-earnings chain on real filings (2026-09-27, AXP FY2026Q2) hit two limits:
- 04A received the full text of every document the fact table cites, about 1.3 million tokens (five 10-Ks for a few
  historical facts), more than the model's context window; and one reply would have had to judge 385 facts, beyond
  the 128k-token output limit.
- 16A wrote those 385 facts in one reply of 118k output tokens, close to that limit.

So the pipeline cuts these parts into slices. Each slice is a complete call with its own inputs; the slices' outputs
are merged into the outputs one call would have produced, and the steps after it read them unchanged.

- 16A: the product's blocks (one per draft output or diff) are packed in order into slices of at most
  PRODUCT_TOKENS; a block longer than that is cut at headings. Every slice keeps the product's title and the source
  table. Merged: one fact table, ids renumbered F001... in slice order, each derived fact's inputs renumbered to match.
- 04A: the facts are grouped by the documents they cite and packed into slices of at most MAX_FACTS facts whose
  documents stay within SOFT_SOURCE_TOKENS; a group whose own documents need more gets a slice of its own. Where a
  slice's documents exceed HARD_SOURCE_TOKENS, the documents after the first that fit (newest first) are cut to the
  lines around the values its facts state. Each slice's fact table says which slice it is, and carries as
  context_facts the facts of other slices that its derived facts take as inputs. HARD_SOURCE_TOKENS keeps two annual
  reports in full, which between them carry about four fiscal years of comparatives; windows are a weak substitute,
  since derived and rounded values do not appear verbatim. A document that at most two facts cite, and that is not one
  of the event's own filings, is cut to windows as well (registry.RARE_CITES). Merged: the verdicts in fact order;
  findings and questions renumbered 04A-01... in slice order.

Token counts are estimates: ASCII characters / CHARS_PER_TOKEN plus one per other character, calibrated on the
rehearsal's real calls (filing text, YAML and Markdown ran at 2.5 to 2.8 characters per token).
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

CHARS_PER_TOKEN = 2.6
CONTEXT_TOKENS = 1_000_000  # Claude Opus 5.5's context window
OUTPUT_RESERVE_TOKENS = 128_000  # the claude-code backend's output ceiling (thinking and reply together)
MAX_INPUT_TOKENS = CONTEXT_TOKENS - OUTPUT_RESERVE_TOKENS
PRODUCT_TOKENS = 16_000  # 16A: product text per slice; the fact table runs to about 2.5 tokens per product token
MAX_FACTS = 60  # 04A: facts per slice
SOFT_SOURCE_TOKENS = 160_000  # 04A: a slice's documents, when groups share a slice
HARD_SOURCE_TOKENS = 500_000  # 04A: a slice's documents in full at most (two 10-Ks and change); older ones are cut
WINDOW_LINES = 12  # lines kept on each side of a line that states a value
WINDOW_HITS = 6  # matching lines kept per value
WINDOW_TOKENS = 30_000  # at most this much of one cut document

_BLOCK_RE = re.compile(r"^===== .* =====$", re.M)
_SOURCE_TABLE = "===== source table"
_BREAK_RE = re.compile(r"^(#{1,6} |@@ |- |[A-Za-z_][\w-]*:)")
_NUMBER_RE = re.compile(r"(?<![\w.])\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![\w.,])\d+\.\d+|(?<![\w.,])\d{3,}")
_FACT_ID_RE = re.compile(r"^F\d+$")
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December")


def estimate_tokens(text: str) -> int:
    """Input tokens of a text, estimated (see the module docstring)."""
    ascii_chars = len(text.encode("ascii", "ignore"))
    return int(ascii_chars / CHARS_PER_TOKEN) + (len(text) - ascii_chars)


# ---------------------------------------------------------------------------------------------------- 16A


@dataclasses.dataclass(frozen=True)
class _Unit:
    header: str
    body: str
    tokens: int


def _split_long(header: str, body: str, limit: int) -> list[_Unit]:
    """A block longer than `limit`, cut at line boundaries, preferring a heading, a diff hunk or a top-level YAML
    item as the first line of each piece."""
    lines = body.splitlines(keepends=True)
    pieces: list[list[str]] = [[]]
    size = 0
    for line in lines:
        cost = estimate_tokens(line)
        if pieces[-1] and size + cost > limit:
            back = next((i for i in range(len(pieces[-1]) - 1, 0, -1) if _BREAK_RE.match(pieces[-1][i])), None)
            carried = pieces[-1][back:] if back is not None and back > len(pieces[-1]) // 2 else []
            if carried:
                del pieces[-1][back:]
            pieces.append(carried)
            size = sum(estimate_tokens(x) for x in carried)
        pieces[-1].append(line)
        size += cost
    count = len(pieces)
    return [_Unit(f"{header[:-6]} (part {i} of {count}) =====" if count > 1 else header, "".join(p),
                  estimate_tokens("".join(p))) for i, p in enumerate(pieces, 1)]


def split_product(text: str, limit: int | None = None) -> list[str] | None:
    """16A's product cut into slices of whole blocks (a block longer than `limit`, by default PRODUCT_TOKENS, in
    pieces), each with the product's title and the source table; None when the blocks fit in one slice."""
    limit = PRODUCT_TOKENS if limit is None else limit
    heads = list(_BLOCK_RE.finditer(text))
    if not heads:
        return None
    preamble = text[:heads[0].start()]
    units: list[_Unit] = []
    table = ""
    for i, match in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        header, body = match.group(0), text[match.end():end]
        if header.startswith(_SOURCE_TABLE):
            table += header + body
            continue
        tokens = estimate_tokens(body)
        units += _split_long(header, body, limit) if tokens > limit else [_Unit(header, body, tokens)]
    if sum(u.tokens for u in units) <= limit:
        return None
    groups: list[list[_Unit]] = [[]]
    size = 0
    for unit in units:
        if groups[-1] and size + unit.tokens > limit:
            groups.append([])
            size = 0
        groups[-1].append(unit)
        size += unit.tokens
    slices = []
    for number, group in enumerate(groups, 1):
        names = "; ".join(u.header.strip("= ") for u in group)
        note = (f"[Pipeline note: this call gets slice {number} of {len(groups)} of the product, with these blocks: {names}. "
                "Extract the facts of these blocks only; the other blocks go to other calls, and the source table "
                "below is complete. Number the facts from F001; the pipeline renumbers them when it merges the "
                "slices.]\n\n")
        body = "".join(u.header + (u.body if u.body.startswith("\n") else "\n" + u.body) for u in group)
        slices.append(preamble + note + body.rstrip("\n") + "\n\n" + table)
    return slices


def _fact_rows(data: Any) -> list[Any]:
    if isinstance(data, Mapping) and isinstance(data.get("facts"), list):
        return list(data["facts"])
    return list(data) if isinstance(data, list) else []


def _renumber_inputs(value: Any, mapping: Mapping[str, str]) -> Any:
    if isinstance(value, list):
        return [mapping.get(str(v), v) if isinstance(v, str) else v for v in value]
    if isinstance(value, str):
        return mapping.get(value, value)
    return value


def merge_fact_tables(tables: Sequence[Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """16A: the slices' fact tables as one, ids renumbered F001... in slice order and derived facts' inputs to match.
    Returns the merged table and each slice's {old id: new id}."""
    merged: dict[str, Any] = {}
    for table in tables:
        if isinstance(table, Mapping):
            for key, value in table.items():
                if key not in ("facts", "generated_by") and merged.get(key) is None:
                    merged[key] = value
    facts: list[Any] = []
    maps: list[dict[str, str]] = []
    number = 0
    for table in tables:
        rows = _fact_rows(table)
        mapping: dict[str, str] = {}
        for row in rows:
            number += 1
            if isinstance(row, Mapping) and row.get("id") is not None:
                mapping[str(row["id"])] = f"F{number:03d}"
        position = number - len(rows)
        for row in rows:
            position += 1
            if not isinstance(row, Mapping):
                facts.append(row)
                continue
            new = {**row, "id": f"F{position:03d}"}
            if "inputs" in new:
                new["inputs"] = _renumber_inputs(new["inputs"], mapping)
            facts.append(new)
        maps.append(mapping)
    merged["facts"] = facts
    generated = next((t.get("generated_by") for t in tables if isinstance(t, Mapping) and t.get("generated_by")),
                     None)
    if generated is not None:
        merged["generated_by"] = generated
    return merged, maps


# ---------------------------------------------------------------------------------------------------- 04A


@dataclasses.dataclass
class FactSlice:
    facts: list[int]  # indexes into the fact table, in table order
    documents: list[str]  # keys of the documents supplied in full
    cut: list[str] = dataclasses.field(default_factory=list)  # keys of the documents cut to windows

    def all_documents(self) -> list[str]:
        return self.documents + self.cut


def _tokens_of(keys: Iterable[str], doc_tokens: Mapping[str, int]) -> int:
    return sum(doc_tokens.get(k, 0) for k in set(keys))


def pack_facts(fact_docs: Sequence[frozenset[str]], doc_tokens: Mapping[str, int], *,
               priority: Sequence[str] = (), max_facts: int | None = None, soft: int | None = None,
               hard: int | None = None) -> list[FactSlice]:
    """Pack facts into slices by the documents they cite (see the module docstring). `fact_docs[i]` is the set of
    document keys fact i needs; `priority` orders documents for full text when a slice is over `hard` (first kept
    first; the rest are cut to windows). The limits default to MAX_FACTS, SOFT_SOURCE_TOKENS and HARD_SOURCE_TOKENS.
    Slices come in the order of their first fact."""
    max_facts = MAX_FACTS if max_facts is None else max_facts
    soft = SOFT_SOURCE_TOKENS if soft is None else soft
    hard = HARD_SOURCE_TOKENS if hard is None else hard
    groups: dict[frozenset[str], list[int]] = {}
    for index, docs in enumerate(fact_docs):
        groups.setdefault(docs, []).append(index)
    order = sorted(groups.items(), key=lambda item: (-_tokens_of(item[0], doc_tokens), item[1][0]))
    chunks = [(docs, members[i:i + max_facts]) for docs, members in order for i in range(0, len(members), max_facts)]
    slices: list[tuple[set[str], list[int]]] = []
    for docs, members in chunks:
        best, best_added = None, None
        for position, (have, facts) in enumerate(slices):
            if len(facts) + len(members) > max_facts:
                continue
            current = _tokens_of(have, doc_tokens)
            after = _tokens_of(have | docs, doc_tokens)
            if after > soft and after != current:
                continue
            added = after - current
            if best_added is None or added < best_added:
                best, best_added = position, added
        if best is None:
            slices.append((set(docs), list(members)))
        else:
            slices[best][0].update(docs)
            slices[best][1].extend(members)
    rank = {key: i for i, key in enumerate(priority)}
    result = []
    for have, facts in sorted(slices, key=lambda s: min(s[1])):
        keys = sorted(have, key=lambda k: (rank.get(k, len(rank)), k))
        full, cut, size = [], [], 0
        for key in keys:
            if not full or size + doc_tokens.get(key, 0) <= hard:
                full.append(key)
                size += doc_tokens.get(key, 0)
            else:
                cut.append(key)
        result.append(FactSlice(sorted(facts), full, cut))
    return result


def number_forms(value: Any) -> list[str]:
    """The numbers of a string value as a filing may print them ("12,256 against 11,200" -> 12,256 and 11,200),
    ignoring one- and two-digit whole numbers, which match too much."""
    if not isinstance(value, str):
        return []
    return list(dict.fromkeys(_NUMBER_RE.findall(value)))


def date_forms(value: Any) -> list[str]:
    """A date as filings print it: July 24, 2026 and 2026-07-24."""
    try:
        return [f"{_MONTHS[value.month - 1]} {value.day}, {value.year}", value.isoformat()]
    except (AttributeError, IndexError, TypeError):
        return []


def windows(text: str, patterns: Sequence[re.Pattern[str]], *, lines_around: int = WINDOW_LINES,
            per_pattern: int = WINDOW_HITS, max_tokens: int = WINDOW_TOKENS) -> tuple[str, int]:
    """The lines of `text` around the first `per_pattern` lines that match each of `patterns`, overlapping windows
    merged, joined by "[…]" lines, at most `max_tokens`. Returns the text and the number of matching lines kept."""
    lines = text.splitlines()
    found: set[int] = set()
    for pattern in patterns:
        found.update([i for i, line in enumerate(lines) if pattern.search(line)][:per_pattern])
    hits = sorted(found)
    spans: list[list[int]] = []
    for i in hits:
        start, end = max(0, i - lines_around), min(len(lines), i + lines_around + 1)
        if spans and start <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], end)
        else:
            spans.append([start, end])
    out, size = [], 0
    for start, end in spans:
        piece = "\n".join(lines[start:end])
        cost = estimate_tokens(piece)
        if size + cost > max_tokens:
            out.append("[… further matches left out: the cut document's limit is reached]")
            break
        out.append(piece)
        size += cost
    return "\n[…]\n".join(out), len(hits)


def slice_fact_table(data: Any, rows: Sequence[Any], indexes: Sequence[int], number: int, count: int) -> dict[str, Any]:
    """04A: the fact table of one slice: which slice it is, the table's own keys, the slice's facts, and as
    context_facts the other slices' facts its derived facts take as inputs."""
    chosen = [rows[i] for i in indexes]
    ids = {str(r.get("id")) for r in chosen if isinstance(r, Mapping)}
    by_id = {str(r.get("id")): r for r in rows if isinstance(r, Mapping)}
    wanted: list[str] = []
    for row in chosen:
        if isinstance(row, Mapping):
            inputs = row.get("inputs")
            for ref in inputs if isinstance(inputs, list) else [inputs]:
                ref = str(ref) if ref is not None else ""
                if _FACT_ID_RE.match(ref) and ref not in ids and ref in by_id and ref not in wanted:
                    wanted.append(ref)
    note = (f"This call gets slice {number} of {count} of the fact table ({len(chosen)} facts); the other slices go "
            "to other calls. Give a verdict for every fact under facts and for no other."
            + (" context_facts are facts of other slices that derived facts here take as inputs, shown so you can "
               "recompute them; give them no verdict." if wanted else ""))
    table: dict[str, Any] = {"slice": {"number": number, "of": count, "note": note}}
    if isinstance(data, Mapping):
        table.update({k: v for k, v in data.items() if k not in ("facts", "slice", "context_facts")})
    table["facts"] = chosen
    if wanted:
        table["context_facts"] = [by_id[i] for i in wanted]
    return table


def _new_ids(rows: Sequence[Any], prefix: str, start: int) -> dict[str, str]:
    return {str(row["id"]): f"{prefix}{start + offset:02d}" for offset, row in enumerate(rows)
            if isinstance(row, Mapping) and row.get("id") is not None}


def _rewrite(value: Any, mapping: Mapping[str, str]) -> Any:
    """Every id of `mapping` in a value (strings, lists, mappings) replaced by its new id, in one pass."""
    if not mapping:
        return value
    if isinstance(value, str):
        keys = sorted(mapping, key=len, reverse=True)
        pattern = re.compile(r"(?<![\w-])(?:" + "|".join(re.escape(k) for k in keys) + r")(?![\w-])")
        return pattern.sub(lambda m: mapping[m.group(0)], value)
    if isinstance(value, list):
        return [_rewrite(v, mapping) for v in value]
    if isinstance(value, Mapping):
        return {k: _rewrite(v, mapping) for k, v in value.items()}
    return value


def merge_audits(slices: Sequence[Mapping[str, Any]], fact_order: Sequence[str], *,
                 finding_prefix: str = "04A-", question_prefix: str = "04A-Q") -> tuple[dict[str, list[Any]],
                                                                                        list[dict[str, str]]]:
    """04A: the slices' parsed outputs ({fact_verdicts, findings, questions}, each a list or None) as one set. The
    verdicts follow the fact table's order; findings and questions are renumbered in slice order, and each slice's
    references to its own finding and question ids are rewritten to match. Returns the merged outputs and each
    slice's {old id: new id}."""
    rank = {fact: i for i, fact in enumerate(fact_order)}
    verdicts: list[Any] = []
    findings: list[Any] = []
    questions: list[Any] = []
    maps: list[dict[str, str]] = []
    for part in slices:
        verdicts += list(part.get("fact_verdicts") or [])
        own_findings = list(part.get("findings") or [])
        own_questions = list(part.get("questions") or [])
        fmap = _new_ids(own_findings, finding_prefix, len(findings) + 1)
        qmap = _new_ids(own_questions, question_prefix, len(questions) + 1)
        mapping = {**fmap, **qmap}
        for rows, target, key_map in ((own_findings, findings, fmap), (own_questions, questions, qmap)):
            for row in rows:
                if isinstance(row, Mapping):
                    new = dict(_rewrite(dict(row), mapping))
                    if row.get("id") is not None:
                        new["id"] = key_map[str(row["id"])]
                    target.append(new)
                else:
                    target.append(row)
        maps.append(mapping)
    verdicts.sort(key=lambda v: rank.get(str(v.get("id")) if isinstance(v, Mapping) else "", len(rank)))
    return {"fact_verdicts": verdicts, "findings": findings, "questions": questions}, maps


def verdict_coverage(fact_ids: Sequence[str], verdicts: Sequence[Any], *, shown: int = 20) -> list[str]:
    """04A: every fact of the table has exactly one verdict, and no verdict is for anything else."""
    wanted = [str(i) for i in fact_ids]
    seen: dict[str, int] = {}
    for row in verdicts:
        if isinstance(row, Mapping) and row.get("id") is not None:
            seen[str(row["id"])] = seen.get(str(row["id"]), 0) + 1

    def listed(ids: list[str]) -> str:
        return ", ".join(ids[:shown]) + (f" and {len(ids) - shown} more" if len(ids) > shown else "")

    errors = []
    missing = [i for i in wanted if i not in seen]
    extra = [i for i in seen if i not in set(wanted)]
    twice = [i for i, n in seen.items() if n > 1]
    if missing:
        errors.append(f"fact_verdicts: no verdict for {listed(missing)}; every fact under facts in the fact_table "
                      "gets exactly one verdict (04A)")
    if extra:
        errors.append(f"fact_verdicts: {listed(extra)} {'is' if len(extra) == 1 else 'are'} not a fact under facts in "
                      "the fact_table; give verdicts for those facts only (04A)")
    if twice:
        errors.append(f"fact_verdicts: {listed(twice)} {'has' if len(twice) == 1 else 'have'} more than one verdict "
                      "(04A)")
    return errors


def fact_ids(table: Any) -> list[str]:
    return [str(r["id"]) for r in _fact_rows(table) if isinstance(r, Mapping) and r.get("id") is not None]
