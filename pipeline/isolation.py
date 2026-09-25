"""The content half of trimming inputs by role (00 §G6): removes from finished products the sections that
audit-type inputs must not see. This module calls no model.

pipeline/llm.py refuses input names that must not be given, per can_see/cannot_see in agents/<role>.yml; this module
trims the finished product itself down to what those input names refer to:

- The red team's first pass (04B, 09B) gets *_without_counter, dossier_without_9_12 and thesis_without_loss_paths;
  only the second pass adds back the removed parts (product_counter_section, document_counter_section).
- The blind read (14A) gets question_list_stripped: kind and maps_to removed, order shuffled (open questions stay put).

When a section to be removed can't be found, ValueError is raised instead of handing the text over unchanged: handing
it over unchanged would leak the bear-case content to the first pass.
"""

from __future__ import annotations

import random
import re
from collections.abc import Iterable
from typing import Any

from .outputs import dump_yaml, load_yaml_text

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^[ \t]*(```|~~~)")
_NUMBER = re.compile(r"^(?:第\s*)?(\d+)\s*(?:[.、．:：)）]|部分|节|\s)")

# The bear-case content of each of 06–08 (removed for the first pass of 09B); 06 has no dedicated bear-case
# section, so 09B runs only one pass when it reviews 06.
COUNTER_TITLES = {"06": (), "07": ("竞争生态全景",), "08": ("反过来想",)}


def split_sections(markdown: str, level: int = 2) -> list[tuple[str | None, str]]:
    """Split at the headings of the given level: [(heading text, or None for the part before the first heading,
    chunk text), ...].

    The chunks joined together equal the original text; a # inside a code fence is not a heading; deeper headings stay
    in the chunk they belong to.
    """
    sections: list[tuple[str | None, str]] = []
    title: str | None = None
    chunk: list[str] = []
    fenced = False
    for line in markdown.splitlines(keepends=True):
        if _FENCE.match(line):
            fenced = not fenced
        match = None if fenced else _HEADING.match(line.rstrip("\r\n"))
        if match and len(match.group(1)) == level:
            if chunk or title is not None:
                sections.append((title, "".join(chunk)))
            title, chunk = match.group(2), [line]
        else:
            chunk.append(line)
    if chunk or title is not None:
        sections.append((title, "".join(chunk)))
    return sections


def section_number(title: str) -> int | None:
    """The number at the start of a heading: "9. ..." -> 9, and the Chinese "part 12" form (see _NUMBER) -> 12."""
    match = _NUMBER.match(title.strip())
    return int(match.group(1)) if match else None


def strip_sections(
    markdown: str, *, numbers: Iterable[int] = (), titles: Iterable[str] = (), level: int = 2
) -> tuple[str, str]:
    """Remove the sections of the given level whose number is in numbers or whose heading contains any of titles.

    Returns (remaining text, removed part). Raises ValueError if any of the numbers or titles is not found.
    """
    numbers, titles = set(numbers), tuple(titles)
    kept: list[str] = []
    removed: list[str] = []
    found_numbers: set[int] = set()
    found_titles: set[str] = set()
    for title, text in split_sections(markdown, level):
        number = section_number(title) if title is not None else None
        hits = [t for t in titles if title is not None and t in title]
        if number in numbers or hits:
            removed.append(text)
            if number in numbers:
                found_numbers.add(number)
            found_titles.update(hits)
        else:
            kept.append(text)
    missing = [f"第 {n} 部分" for n in sorted(numbers - found_numbers)] + [f"“{t}”" for t in titles if t not in found_titles]
    if missing:
        raise ValueError(f"找不到要删去的章节：{'、'.join(missing)}（按 {'#' * level} 级标题查找）")
    return "".join(kept), "".join(removed)


def dossier_without_9_12(dossier: str) -> tuple[str, str]:
    """Remove part 9 (the bear case) and part 12 (thesis breakers) from the dossier. Returns (remaining, removed)."""
    return strip_sections(dossier, numbers=(9, 12))


def report_without_counter(report: str) -> tuple[str, str]:
    """Remove section 7 (the strongest bear-case argument) from the research report.

    Returns (product_without_counter, product_counter_section).
    """
    return strip_sections(report, numbers=(7,))


def document_without_counter(document: str, doc: str) -> tuple[str, str]:
    """Remove the bear-case content of 06–08 (COUNTER_TITLES).

    Returns (document_without_counter, document_counter_section).
    """
    if doc not in COUNTER_TITLES:
        raise ValueError(f"doc 必须是 {'、'.join(COUNTER_TITLES)} 之一，收到 {doc!r}")
    titles = COUNTER_TITLES[doc]
    return strip_sections(document, titles=titles) if titles else (document, "")


def thesis_without_loss_paths(thesis_yaml: str) -> str:
    """Remove thesis.permanent_loss_paths from thesis.yml (the YAML is dumped again, so comments are dropped too)."""
    data = load_yaml_text(thesis_yaml)
    if not isinstance(data, dict):
        raise ValueError("thesis.yml 应当是一个映射")
    thesis = data.get("thesis")
    if isinstance(thesis, dict) and "permanent_loss_paths" in thesis:
        data = {**data, "thesis": {k: v for k, v in thesis.items() if k != "permanent_loss_paths"}}
    return dump_yaml(data)


def question_list_stripped(question_list_yaml: str, *, seed: str) -> str:
    """Prepare the 14Q question list for the blind read: drop kind and maps_to from every question, shuffle the
    non-open questions deterministically by seed, and leave the open questions in place.

    Question ids are kept: 14B matches the blind read's answers to the company manager's by id. The same seed gives
    the same order, so the input hash is reproducible.
    """
    data: Any = load_yaml_text(question_list_yaml)
    if isinstance(data, dict) and isinstance(data.get("questions"), list):
        data = data["questions"]
    if not isinstance(data, list) or not all(isinstance(q, dict) for q in data):
        raise ValueError("question_list 应当是问题的列表（每题 id、question、kind、maps_to）")
    movable = [i for i, q in enumerate(data) if q.get("kind") != "open"]
    order = [data[i] for i in movable]
    random.Random(seed).shuffle(order)
    shuffled = list(data)
    for i, question in zip(movable, order):
        shuffled[i] = question
    stripped = [{k: v for k, v in q.items() if k not in ("kind", "maps_to")} for q in shuffled]
    return dump_yaml(stripped)
