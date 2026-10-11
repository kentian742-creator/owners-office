#!/usr/bin/env python3
"""Measures the fact-audit excerpt matcher (documents.cut_excerpt via registry.cut_source_excerpts) on a past 04A run.

Usage (from the workspace root, the folder that holds owners-office and owners-office-private):

    .venv/bin/python owners-office/scripts/excerpt_eval.py owners-office-private/runs/WMT/2026-10-09-04A-rerun2 \
        --labels work/excerpt-eval/WMT-2026-10-09-04A-rerun2.labels.yml [--show wrong,none,unjudged,changed]

The run is a 04A bundle, sliced or not. Its fact tables hold each fact with the excerpt the pipeline cut when the
bundle was assembled ("recorded"), and its sources inputs hold the documents. A document that a slice received cut to
the lines around its values is taken from a slice that received it whole; one that no slice received whole is used as
cut, and the report names it. The script cuts every fact's excerpt again with this checkout's code ("now") and
counts, for each group of labelled facts, the excerpts that are right, wrong or missing, recorded and now.

The labels are kept outside the repositories, since their facts come from a private run:

    facts:
      F161:
        group: named            # any name; the report counts each group and all together
        right: [generally using a one-month lag]
        wrong: [Vesting periods for restricted stock]
        absent: false           # true: no line of the cited documents states the fact, so no excerpt is best

An entry matches an excerpt that contains each of its parts (separated by "…"), whitespace collapsed. An excerpt that
matches no entry, or both a right and a wrong one, is "unjudged": judge it and add it to the labels.
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

from pipeline import documents, registry  # noqa: E402  (this checkout's pipeline, once the path is set)

HEADER_RE = re.compile(r"^===== \[src:([^\]]+)\] .* =====$")  # registry.render_documents
CUT_NOTE = "note: cut by the pipeline"
OUTCOMES = ("right", "wrong", "none", "unjudged")


def parse_sources(text: str) -> list[tuple[str, str, bool]]:
    """(cite, text, whether the pipeline cut it) of each document in a sources input."""
    lines = text.split("\n")
    heads = [i for i, line in enumerate(lines) if HEADER_RE.match(line)]
    found = []
    for k, start in enumerate(heads):
        end = heads[k + 1] if k + 1 < len(heads) else len(lines)
        i, cut = start + 1, False
        while i < end and (lines[i].startswith("url: ") or lines[i].startswith("note: ")):
            cut = cut or lines[i].startswith(CUT_NOTE)
            i += 1
        found.append((HEADER_RE.match(lines[start]).group(1), "\n".join(lines[i:end]).strip(), cut))
    return found


def input_files(run: Path, name: str) -> list[Path]:
    files = sorted(run.glob(f"slices/*/inputs/{name}")) or sorted(run.glob(f"inputs/{name}"))
    if not files:
        raise SystemExit(f"error: no inputs/{name} in {run} or its slices")
    return files


def load_documents(run: Path) -> tuple[dict[str, str], list[str]]:
    """{cite: text}, with each tag also mapped to its first document as registry.cited_texts maps it, and the cites
    found only cut."""
    versions: dict[str, list[tuple[str, bool]]] = {}
    for path in input_files(run, "sources.txt"):
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
    return int(digits) if digits else 0, fact_id


def load_facts(run: Path) -> list[dict[str, Any]]:
    """The fact rows of every slice, each once, in the order of their ids (16A's order)."""
    facts: dict[str, dict[str, Any]] = {}
    for path in input_files(run, "fact_table.yml"):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        for fact in (data.get("facts") if isinstance(data, dict) else data) or []:
            if isinstance(fact, dict):
                facts.setdefault(str(fact.get("id")), fact)
    return [facts[i] for i in sorted(facts, key=_order)]


def owners(run: Path) -> list[str]:
    """The words that name the company, from the run's manifest: its ticker and its short name."""
    path = run / "manifest.yml"
    manifest = (yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None) or {}
    variables = manifest.get("variables") or {}
    ticker = str(variables.get("ticker") or manifest.get("company") or "")
    return documents.owner_names(ticker, variables.get("company"), None) if ticker else []


def recut(facts: Sequence[Mapping[str, Any]], texts: Mapping[str, str], names: Sequence[str]) -> dict[str, Any]:
    """Each fact's excerpt as this checkout cuts it (None when it cuts none)."""
    rows = [copy.deepcopy(dict(fact)) for fact in facts]
    for row in rows:
        row.pop("source_excerpt", None)
        row.pop("excerpt_missing", None)
    registry.cut_source_excerpts(rows, texts, names)
    return {str(row.get("id")): row.get("source_excerpt") for row in rows}


def matches(excerpt: str, entry: str) -> bool:
    plain = " ".join(excerpt.split())
    parts = [" ".join(part.split()) for part in str(entry).split("…") if part.strip()]
    return bool(parts) and all(part in plain for part in parts)


def judge(excerpt: str | None, label: Mapping[str, Any]) -> str:
    """right, wrong, none or unjudged."""
    if not excerpt:
        return "none"
    right = any(matches(excerpt, entry) for entry in label.get("right") or [])
    wrong = any(matches(excerpt, entry) for entry in label.get("wrong") or [])
    return ("right" if right else "wrong") if right != wrong else "unjudged"


def count(excerpts: Mapping[str, Any], labels: Mapping[str, Mapping[str, Any]]) -> dict[str, Counter]:
    """group -> outcome -> facts, "all" last; "absent" counts the facts left without an excerpt that have none."""
    counts: dict[str, Counter] = {}
    for fact_id, label in labels.items():
        outcome = judge(excerpts.get(fact_id), label)
        for group in (str(label.get("group") or "-"), "all"):
            counts.setdefault(group, Counter())[outcome] += 1
            if outcome == "none" and label.get("absent"):
                counts[group]["absent"] += 1
    return dict(sorted(counts.items(), key=lambda item: item[0] == "all"))


def table(counts: Mapping[str, Counter]) -> str:
    rows = [f"{'':10}{'facts':>6}" + "".join(f"{o:>10}" for o in OUTCOMES) + f"{'(absent)':>10}"]
    for group, counter in counts.items():
        rows.append(f"{group:10}{sum(counter[o] for o in OUTCOMES):>6}" + "".join(f"{counter[o]:>10}" for o in OUTCOMES)
                    + f"{counter['absent']:>10}")
    return "\n".join(rows)


def describe(fact: Mapping[str, Any]) -> str:
    return (f"{fact.get('subject')} | {fact.get('what')} | {fact.get('value')!r} {fact.get('unit') or ''} | "
            f"{fact.get('period')} | {fact.get('source')}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("run", type=Path, help="a 04A run directory")
    parser.add_argument("--labels", type=Path, required=True, help="the hand labels (YAML)")
    parser.add_argument("--show", default="", help="list facts now right, wrong, none, unjudged, or changed")
    args = parser.parse_args(argv)
    if not args.run.is_dir() or not args.labels.is_file():
        parser.error("the run must be a directory and the labels a file")
    data = yaml.safe_load(args.labels.read_text(encoding="utf-8")) or {}
    labels = {str(k): v for k, v in (data.get("facts") or {}).items()}
    facts = load_facts(args.run)
    by_id = {str(fact.get("id")): fact for fact in facts}
    missing = sorted(set(labels) - set(by_id), key=_order)
    if missing:
        print(f"warning: labelled but not in the run: {', '.join(missing)}")
        labels = {k: v for k, v in labels.items() if k in by_id}
    texts, only_cut = load_documents(args.run)
    recorded = {fact_id: fact.get("source_excerpt") for fact_id, fact in by_id.items()}
    now = recut(facts, texts, owners(args.run))
    changed = [fact_id for fact_id in by_id if recorded[fact_id] != now[fact_id]]

    print(f"{args.run}: {len(facts)} facts, {len(labels)} labelled")
    if only_cut:
        print(f"documents found only cut by the pipeline: {', '.join(only_cut)}")
    print(f"excerpts: {sum(1 for e in recorded.values() if e)} recorded, {sum(1 for e in now.values() if e)} now, "
          f"{len(changed)} facts changed")
    for when, excerpts in (("recorded", recorded), ("now", now)):
        print(f"\n{when}:\n{table(count(excerpts, labels))}")
    show = {item.strip() for item in args.show.split(",") if item.strip()}
    for fact_id in sorted(by_id, key=_order):
        label = labels.get(fact_id)
        outcome = judge(now[fact_id], label) if label else "unlabelled"
        if outcome in show or ("changed" in show and fact_id in changed):
            print(f"\n{fact_id} [{(label or {}).get('group', '-')}] {outcome}: {describe(by_id[fact_id])}\n"
                  f"  now:      {now[fact_id]}\n  recorded: {recorded[fact_id]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
