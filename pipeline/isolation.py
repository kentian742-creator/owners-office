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
# "9. Bear case", "9) ...", "9: ...", "Part 9. ...", "Section 7 - ..."; the number is what counts.
_NUMBER = re.compile(r"^(?:(?:part|section)\s+)?(\d+)\s*(?:[.:)]|\s|$)", re.IGNORECASE)

# The fixed headings the prompts define (01A for the dossier, 02 for the research report, 07 and 08 for the
# deep-cognition documents, which tell the model to use them verbatim). A section is found by its number or by its
# title; titles match case-insensitively as whole words anywhere in the heading, so a numbered heading
# ("## 9. Bear case") and a suffix such as "(Munger-style)" still match.
DOSSIER_COUNTER_SECTIONS = ((9, "Bear case"), (12, "Thesis breakers"))  # the dossier's parts 9 and 12
REPORT_COUNTER_SECTIONS = ((7, "Strongest bear case"),)  # the research report's section 7
# The bear-case content of each of 06-08 (removed for the first pass of 09B); 06 has no dedicated bear-case
# section, so 09B runs only one pass when it reviews 06.
COUNTER_TITLES = {"06": (), "07": ("Competitive landscape",), "08": ("Invert",)}


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
    """The number at the start of a heading: "9. Bear case" -> 9, "Part 12: Thesis breakers" -> 12."""
    match = _NUMBER.match(title.strip())
    return int(match.group(1)) if match else None


def title_matches(heading: str, title: str) -> bool:
    """Whether a heading carries the title: case-insensitive, as whole words, anywhere in the heading."""
    words = r"\s+".join(re.escape(word) for word in title.split())
    return re.search(rf"(?<![\w-]){words}(?![\w-])", heading, re.IGNORECASE) is not None


def strip_sections(
    markdown: str,
    *,
    numbers: Iterable[int] = (),
    titles: Iterable[str] = (),
    sections: Iterable[tuple[int | None, str | None]] = (),
    level: int = 2,
) -> tuple[str, str]:
    """Remove the sections of the given level that match: by number (numbers), by title (titles), or by either the
    number or the title of a (number, title) pair (sections).

    Returns (remaining text, removed part). Raises ValueError if any number, title or pair matches no section.
    """
    targets = [(n, None) for n in numbers] + [(None, t) for t in titles] + list(sections)
    kept: list[str] = []
    removed: list[str] = []
    found: set[int] = set()
    for heading, text in split_sections(markdown, level):
        number = section_number(heading) if heading is not None else None
        hits = [i for i, (n, t) in enumerate(targets) if heading is not None and (
            (n is not None and number == n) or (t is not None and title_matches(heading, t)))]
        if hits:
            removed.append(text)
            found.update(hits)
        else:
            kept.append(text)
    missing = [_describe(n, t) for i, (n, t) in enumerate(targets) if i not in found]
    if missing:
        raise ValueError(f"sections to remove not found: {', '.join(missing)} (looked for {'#' * level} headings)")
    return "".join(kept), "".join(removed)


def _describe(number: int | None, title: str | None) -> str:
    if number is not None and title is not None:
        return f'section {number} ("{title}")'
    return f"section {number}" if number is not None else f'"{title}"'


def dossier_without_9_12(dossier: str) -> tuple[str, str]:
    """Remove part 9 ("Bear case") and part 12 ("Thesis breakers") from the dossier. Returns (remaining, removed)."""
    return strip_sections(dossier, sections=DOSSIER_COUNTER_SECTIONS)


def report_without_counter(report: str) -> tuple[str, str]:
    """Remove section 7 ("Strongest bear case") from the research report.

    Returns (product_without_counter, product_counter_section).
    """
    return strip_sections(report, sections=REPORT_COUNTER_SECTIONS)


def document_without_counter(document: str, doc: str) -> tuple[str, str]:
    """Remove the bear-case content of 06-08 (COUNTER_TITLES: 07 "Competitive landscape", 08 "Invert").

    Returns (document_without_counter, document_counter_section).
    """
    if doc not in COUNTER_TITLES:
        raise ValueError(f"doc must be one of {', '.join(COUNTER_TITLES)}, got {doc!r}")
    titles = COUNTER_TITLES[doc]
    return strip_sections(document, titles=titles) if titles else (document, "")


def thesis_without_loss_paths(thesis_yaml: str) -> str:
    """Remove thesis.permanent_loss_paths from thesis.yml (the YAML is dumped again, so comments are dropped too)."""
    data = load_yaml_text(thesis_yaml)
    if not isinstance(data, dict):
        raise ValueError("thesis.yml should be a mapping")
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
        raise ValueError("question_list should be a list of questions (each with id, question, kind, maps_to)")
    movable = [i for i, q in enumerate(data) if q.get("kind") != "open"]
    order = [data[i] for i in movable]
    random.Random(seed).shuffle(order)
    shuffled = list(data)
    for i, question in zip(movable, order):
        shuffled[i] = question
    stripped = [{k: v for k, v in q.items() if k not in ("kind", "maps_to")} for q in shuffled]
    return dump_yaml(stripped)
