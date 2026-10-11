#!/usr/bin/env python3
"""Measures the fact-audit excerpt matcher (documents.cut_excerpt, registry.cut_source_excerpts) on a past 04A bundle.

Usage (from the owners-office directory):

    ../.venv/bin/python scripts/excerpt_eval.py <bundle> --labels <labels.yml> [--show wrong,none,unjudged,changed]

<bundle> is a 04A run directory (owners-office-private/runs/<T>/<date>-04A...), sliced or not. Its fact tables give
the facts with the excerpts the pipeline cut when the bundle was assembled ("recorded"), and its sources inputs give
the documents. A document that a slice received cut to the lines around its values is taken from a slice that
received it whole; one that no slice received whole is used as cut, and the report names it. The script cuts every
fact's excerpt again with the code in this checkout ("now") and compares both with hand labels.

The labels file is kept outside the repositories, since its facts come from a private run:

    facts:
      F161:
        group: named            # any name; the report counts each group
        right: [generally using a one-month lag]
        wrong: [Vesting periods for restricted stock]
        absent: false           # true: no line of the cited documents states the fact, so no excerpt is best

An entry matches an excerpt that contains each of its parts, separated by "…", with whitespace collapsed. An excerpt
that matches no entry is "unjudged": judge it and add it to the labels. Exit code 0, or 2 for usage errors.
"""

from __future__ import annotations

import argparse
import copy
import re
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline import registry  # noqa: E402  (the checkout's own pipeline, after the path is set)

HEADER_RE = re.compile(r"^===== \[src:([^\]]+)\] .* =====$")
CUT_NOTE = "note: cut by the pipeline"
OUTCOMES = ("right", "wrong", "none", "unjudged")


def parse_sources(text: str) -> list[tuple[str, str, bool]]:
    """(cite, text, cut) of each document in a sources input (registry.render_documents)."""
    lines = text.split("\n")
    heads = [i for i, line in enumerate(lines) if HEADER_RE.match(line)]
    found = []
    for k, start in enumerate(heads):
        end = heads[k + 1] if k + 1 < len(heads) else len(lines)
        cite = HEADER_RE.match(lines[start]).group(1)
        i = start + 1
        cut = False
        while i < end and lines[i].strip() and (lines[i].startswith("url: ") or lines[i].startswith("note: ")):
            cut = cut or lines[i].startswith(CUT_NOTE)
            i += 1
        found.append((cite, "\n".join(lines[i:end]).strip(), cut))
    return found


def input_files(bundle: Path, name: str) -> list[Path]:
    files = sorted(bundle.glob(f"slices/*/inputs/{name}")) or sorted(bundle.glob(f"inputs/{name}"))
    if not files:
        raise SystemExit(f"error: no inputs/{name} in {bundle} or its slices")
    return files


def load_documents(bundle: Path) -> tuple[dict[str, str], list[str]]:
    """{cite and tag: text} as registry.cited_texts gives it, and the cites only found cut."""
    versions: dict[str, list[tuple[str, bool]]] = {}
    for path in input_files(bundle, "sources.txt"):
        for cite, text, cut in parse_sources(path.read_text(encoding="utf-8")):
            versions.setdefault(cite, []).append((text, cut))
    texts: dict[str, str] = {}
    only_cut = []
    for cite in sorted(versions):
        whole = [text for text, cut in versions[cite] if not cut]
        if not whole:
            only_cut.append(cite)
        texts[cite] = whole[0] if whole else max((text for text, _ in versions[cite]), key=len)
        texts.setdefault(cite.partition("#")[0], texts[cite])
    return texts, only_cut


def _order(fact_id: str) -> tuple[int, str]:
    digits = re.sub(r"\D", "", fact_id)
    return (int(digits) if digits else 0, fact_id)


def load_facts(bundle: Path) -> list[dict[str, Any]]:
    """The fact rows of every slice in id order (the order of 16A's fact table), each once."""
    facts: dict[str, dict[str, Any]] = {}
    for path in input_files(bundle, "fact_table.yml"):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        for fact in (data.get("facts") if isinstance(data, dict) else data) or []:
            if isinstance(fact, dict):
                facts.setdefault(str(fact.get("id")), fact)
    return [facts[i] for i in sorted(facts, key=_order)]


def recut(facts: Sequence[Mapping[str, Any]], texts: Mapping[str, str]) -> dict[str, str | None]:
    """Each fact's excerpt as this checkout's pipeline cuts it."""
    rows = [copy.deepcopy(dict(f)) for f in facts]
    for row in rows:
        row.pop("source_excerpt", None)
        row.pop("excerpt_missing", None)
    registry.cut_source_excerpts(rows, texts)
    return {str(r.get("id")): r.get("source_excerpt") for r in rows}


def _plain(text: str) -> str:
    return " ".join(text.split())


def matches(excerpt: str, entry: str) -> bool:
    plain = _plain(excerpt)
    parts = [_plain(p) for p in entry.split("…") if p.strip()]
    return bool(parts) and all(p in plain for p in parts)


def judge(excerpt: str | None, label: Mapping[str, Any]) -> str:
    if not excerpt:
        return "none"
    right = any(matches(excerpt, e) for e in label.get("right") or [])
    wrong = any(matches(excerpt, e) for e in label.get("wrong") or [])
    if right != wrong:
        return "right" if right else "wrong"
    return "unjudged"


def table(rows: Mapping[str, Counter], absent: Mapping[str, Counter]) -> list[str]:
    out = [f"{'':10}{'facts':>6}" + "".join(f"{o:>10}" for o in OUTCOMES) + f"{'(absent)':>10}"]
    for group, counts in rows.items():
        out.append(f"{group:10}{sum(counts.values()):>6}" + "".join(f"{counts[o]:>10}" for o in OUTCOMES)
                   + f"{absent[group]['none']:>10}")
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--show", default="", help="comma-separated: right, wrong, none, unjudged, changed")
    args = parser.parse_args(argv)
    if not args.bundle.is_dir() or not args.labels.is_file():
        parser.error("the bundle must be a directory and the labels a file")
    labels = (yaml.safe_load(args.labels.read_text(encoding="utf-8")) or {}).get("facts") or {}
    facts = load_facts(args.bundle)
    texts, only_cut = load_documents(args.bundle)
    recorded = {str(f.get("id")): f.get("source_excerpt") for f in facts}
    now = recut(facts, texts)
    by_id = {str(f.get("id")): f for f in facts}
    show = {s.strip() for s in args.show.split(",") if s.strip()}

    print(f"{args.bundle}: {len(facts)} facts, {len(labels)} labelled")
    if only_cut:
        print(f"documents only found cut by the pipeline: {', '.join(only_cut)}")
    changed = [i for i in recorded if recorded[i] != now.get(i)]
    print(f"excerpts: {sum(1 for e in recorded.values() if e)} recorded, {sum(1 for e in now.values() if e)} now; "
          f"{len(changed)} differ")
    details = []
    for when, excerpts in (("recorded", recorded), ("now", now)):
        counts: dict[str, Counter] = {}
        absent: dict[str, Counter] = {}
        for fact_id, label in labels.items():
            fact_id = str(fact_id)
            if fact_id not in by_id:
                print(f"warning: {fact_id} is labelled but not in the bundle")
                continue
            outcome = judge(excerpts.get(fact_id), label)
            group = str(label.get("group") or "-")
            for g in (group, "all"):
                counts.setdefault(g, Counter())[outcome] += 1
                absent.setdefault(g, Counter())
                if outcome == "none" and label.get("absent"):
                    absent[g]["none"] += 1
            if when == "now" and outcome in show:
                details.append((fact_id, group, outcome, excerpts.get(fact_id)))
        print(f"\n{when}:")
        print("\n".join(table(dict(sorted(counts.items(), key=lambda kv: kv[0] == "all")), absent)))
    for fact_id, group, outcome, excerpt in sorted(details, key=lambda d: _order(d[0])):
        fact = by_id[fact_id]
        print(f"\n{fact_id} [{group}] {outcome}: {fact.get('subject')} | {fact.get('what')} | {fact.get('value')!r} "
              f"{fact.get('unit') or ''} | {fact.get('period')}\n  now:      {excerpt}\n  recorded: {recorded[fact_id]}")
    if "changed" in show:
        for fact_id in sorted(changed, key=_order):
            fact = by_id[fact_id]
            label = labels.get(fact_id) or labels.get(int(fact_id) if fact_id.isdigit() else fact_id)
            outcome = judge(now.get(fact_id), label) if label else "unlabelled"
            print(f"\n{fact_id} changed ({outcome}): {fact.get('subject')} | {fact.get('what')} | "
                  f"{fact.get('value')!r} {fact.get('unit') or ''} | {fact.get('period')} | {fact.get('source')}\n"
                  f"  now:      {now.get(fact_id)}\n  recorded: {recorded[fact_id]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
