"""Every figure of an archive document, checked against its sources and recomputed (docs/decisions/0034).

The owner directed on 2026-10-10 that spot checks are not enough: every figure in every archive document is checked,
forward against the source its tags cite and in reverse by recomputing it, before anything is placed or published.
This module does the checking. pipeline/runner.py runs it on the archive documents a bundle would place
(`python -m pipeline.runner factcheck <bundle>`, and as a gate inside `place`), and HQ gives a verdict on each figure
the checker could not confirm, in the bundle's factcheck.yml.

Forward. A figure is looked up in the source its tags cite: in the part the locator names (a page, an item, a note, a
statement, an exhibit, a page of the owner's report), then in the whole document, then among the XBRL facts filed
with that accession. A figure without a tag of its own takes the tags of its paragraph, of its list's lead-in, of its
table row (the row's Source cell, else the whole row) and of its table's caption and footnote. Numbers match to the
figure's last digit, also across scales: billions and millions, a percent and a fraction, units and millions. The
company's valuation and dossier, which the other documents draw from, are evidence too, and so is another company's
valuation for a sentence about that company.

Reverse. Arithmetic written out ("3,804 ÷ 11,716 = 32.5%", chains "A = B ÷ C = D", brackets, percents, M and B mixed)
is evaluated, and table rows under operator headers ("− H1 FY2026", "+ H1 FY2027", "= TTM") are redone row by row.
Arithmetic that does not hold is a calc error. A figure no source confirms is tried as a sum, difference, product,
ratio, percent or growth rate of two figures: one from its own sentence or row, the other from the same place, the
same table, the forty figures before it, or the valuation's key figures.

Specific (decision 0038). A match counts only when it singles the figure out, so that a slip of its last digit (the
figure one more or one less in that digit: `slips`) would not pass the same way:
- in a cited filing, the figure stands next to a word of its own label (its column header and row label, or the words
  around it in its sentence) in the source's line or sentence, and neither slip stands there next to that label too;
- in the archive's own evidence and among XBRL facts, neither slip is there at all;
- recomputed from the figures beside it, neither slip is recomputed from them too, and a sum or difference does not
  count when one of its two figures is under ten units of the figure's last digit;
- written-out arithmetic holds when its result is the exact result of the formula as written, to the result's last
  digit. A result that holds only within the rounding of the formula's inputs is a calc error for a reviewer, and in
  a chain "A = B" whose two sides have the same shape, each figure of B is checked against its counterpart in A.
A number matches a figure only when its unit does not make it another kind of quantity: points or a multiple against a
percent, a percent against a dollar amount. Any other match leaves the figure weak, for a reviewer.

Each figure ends in one class (CLASSES):
- found: singled out (above) in the part its own tag cites or, if it is distinctive (three digits or more, or two and
  a decimal point), anywhere in the cited document;
- context: the same, through the tags of its paragraph, list or table;
- xbrl: equal to an XBRL fact filed with the cited filing, when neither slip is one;
- valuation, dossier: a distinctive number in the company's valuation (valuation.yml, or the working of the version
  in force) or in its dossier, when neither slip is in it;
- recomputed: redone from the figures beside it when neither slip is, or the result of a formula in the document
  that holds;
- weak: a short number found only somewhere in a whole document, or a match that does not single the figure out;
- skipped: a single digit (a count or an ordinal), a test's question or the system's own forecast;
- not-a-fact: a heading's number, a series rule's constant, a threshold, condition or forecast, a sensitivity case;
- unverified: none of the above.
Every weak or unverified figure and every calc error needs a reviewer's verdict (REVIEW_CLASSES, VERDICTS).

A document is never its own source or evidence: a valuation's working that cites its own tag is not confirmed by
itself. The check is deterministic: the same documents and sources give the same result. Offline, EDGAR and prices are
read from their caches only, and a tag whose document cannot be read is reported with the reason, never passed over.
"""

from __future__ import annotations

import ast
import bisect
import collections
import dataclasses
import datetime as dt
import decimal
import fnmatch
import itertools
import math
import operator
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import yaml

from . import documents, edgar, prices, registry

if TYPE_CHECKING:  # the runner imports this module
    from .runner import Roots

CLASSES = ("found", "context", "xbrl", "valuation", "dossier", "recomputed", "weak", "skipped", "not-a-fact",
           "unverified")
REVIEW_CLASSES = ("unverified", "weak")
CALC_ERROR = "calc-error"  # a review item for arithmetic written out that does not hold
# The verdicts a reviewer gives an item in factcheck.yml (owner, 2026-10-10). Placement goes ahead only when every item
# has an accepted one; `wrong` and `cannot-verify` stop it until the text is corrected and the item no longer appears.
VERDICTS = {
    "correct": "the figure is in its source (say where)",
    "derived-correct": "recomputed by hand from sourced inputs (say how)",
    "not-a-fact": "not a statement of fact: part of a name or an identifier, a threshold, an assumption or a forecast",
    "last-digit": "correct but for the last digit, within the rounding of its inputs",
    "approximation": "an approximation the text presents as one, close enough to stand",
    "wrong": "the figure is wrong; correct the text (stops placement)",
    "cannot-verify": "no available source confirms it (stops placement)",
}
ACCEPTED_VERDICTS = ("correct", "derived-correct", "not-a-fact", "last-digit", "approximation")
BLOCKING_VERDICTS = ("wrong", "cannot-verify")
REVIEW_FILE = "factcheck.yml"
REVIEW_VERSION = 1
CONTEXT_CHARS = 300  # a review item's context: this much of its sentence or table row, around the figure
ITEM_FIELDS = ("doc", "figure", "class", "tags", "context", "line", "checker")  # written by the checker on every run

# ---------------------------------------------------------------------------------------------------- figures

TAG_RE = re.compile(r"\[src:([A-Za-z0-9][A-Za-z0-9._-]*(?:#[A-Za-z0-9._–-]+)?)\]")
_TAG_TOKEN_RE = re.compile(r"\s*\[src:[^\]]*\]")
# Numbers in a text that are not figures: source tags, ISO dates, fiscal periods, form names, exhibit, item, note and
# part numbers, identifiers of tests, calculations and rulings (MCD-Q7, C9, R18), section signs, ordinals, durations
# ("5-year"), written dates, anchors, versions, split ratios and accession numbers.
NOT_FIGURES = re.compile(
    r"\[src:[^\]]*\]|\b\d{4}-\d{2}-\d{2}\b|\b(?:FY|CY)\d{4}(?:Q[1-4])?(?:[–-](?:FY\d{4})?Q?[1-4])?\b|\bQ[1-4]\b|"
    r"\b(?:10|20|40)-[KQF](?:/A)?\b|\b[86]-K\b|\bDEF ?14A\b|\b13F(?:-HR)?\b|\bS-1\b|\b424B\d\b|"
    r"\bExhibits? \d+(?:\.\d+)?(?:\s*(?:and|,)\s*\d+(?:\.\d+)?)*|\bEX-\d+(?:\.\d+)?\b|"
    r"\b[A-Z]{2,6}-[A-Z]{1,3}\d+\b|\b[A-Z]{2,6}-[A-Z0-9]+-[A-Za-z0-9#.-]+|"
    r"\b(?:Items?|Notes?|Parts?|part|parts)\s+\d+[A-Z]?\b|"
    r"§\s?[A-Z]?\d+[a-z]?|\b[A-Z]{1,2}\d{1,3}\b|\b\d+(?:st|nd|rd|th)\b|"
    r"\b\d+-(?:year|day|week|month|quarter|minute)s?\b|"
    r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2},?\s+\d{4}\b|"
    r"\b(?:Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?\s+\d{1,2}\b|#\S+|\bv\d+(?:\.\d+)*\b|\b\d+-for-\d+\b|"
    r"\b\d{10}-\d{2}-\d{6}\b")
_DIGITS = r"\d[\d,]*(?:\.\d+)?"
# A figure: an optional sign and dollar sign; a number, or a number in parentheses (an accounting negative); a unit.
# A parenthesis that does not close right after the number belongs to the prose ("(236.90 ÷ 208.88)"), not the figure.
NUMBER_RE = re.compile(
    rf"(?<![\w.])(?P<neg>[−-])?(?P<cur>\$)?(?:\((?P<paren>{_DIGITS})\)|(?P<num>{_DIGITS}))"
    r"(?P<unit>\s?(?:%|percent\b|bps?\b|x\b|×|points?\b|pp\b|[MBK]\b|bn\b|million\b|billion\b|thousand\b))?")
YEAR_RE = re.compile(r"^(?:19|20)\d\d$")
UNITS = {"percent": "%", "points": "pt", "point": "pt", "pp": "pt", "bps": "bp", "bp": "bp", "×": "x",
         "million": "M", "m": "M", "billion": "B", "bn": "B", "b": "B", "thousand": "K", "k": "K"}


class Number(NamedTuple):
    text: str  # as written, with its sign, dollar sign, parentheses and unit
    value: float  # negative for a minus sign or accounting parentheses
    unit: str  # %, pt, bp, x, M, B, K or ""
    decimals: int  # digits after the decimal point: the figure's precision


def figures_of(text: str) -> list[Number]:
    """The figures in a text, in order. A year (four digits from 1900 to 2099 with no dollar sign or unit) is not a
    figure, nor is anything NOT_FIGURES matches."""
    out = []
    for m in NUMBER_RE.finditer(NOT_FIGURES.sub(" ", text)):
        raw = (m.group("paren") or m.group("num")).rstrip(",")
        unit_text = m.group("unit") or ""
        if YEAR_RE.match(raw) and not m.group("cur") and not unit_text:
            continue
        value = float(raw.replace(",", ""))
        if m.group("neg") or m.group("paren"):
            value = -value
        number = f"({raw})" if m.group("paren") else raw
        written = f"{m.group('neg') or ''}{m.group('cur') or ''}{number}{unit_text}".strip()
        unit = unit_text.strip().lower()
        out.append(Number(written, value, UNITS.get(unit, unit), len(raw.split(".")[1]) if "." in raw else 0))
    return out


def distinctive(text: str) -> bool:
    """Three significant digits or more, or two and a decimal point: a number unlikely to match by chance."""
    digits = len(re.sub(r"\D", "", text).lstrip("0"))
    return digits >= 3 or ("." in text and digits >= 2)


def _same(a: float, b: float, decimals: int) -> bool:
    """Whether two numbers agree to the last digit of a figure with `decimals` decimals. Signs are ignored: a decline
    may be written as a positive number."""
    return abs(abs(a) - abs(b)) <= 0.5 * 10 ** (-decimals) + 1e-9


def _single_digit(fig: Figure) -> bool:
    return fig.value == int(fig.value) and abs(fig.value) < 10 and not fig.unit


# ---------------------------------------------------------------------------------------------------- units


@dataclasses.dataclass
class Unit:
    """A piece of a document whose figures are checked together: a sentence, a table row, or a YAML value."""

    line: int  # 1-based
    text: str
    own: list[str]  # tags in the sentence itself (a table row has none; each cell carries its own)
    context: list[str]  # tags of the paragraph, the list's lead-in, the row, the table's caption and footnote
    cells: list[tuple[str, list[str]]] | None = None  # a table row: (cell text, the cell's own tags)
    header: list[str] | None = None
    table: int | None = None  # which table of the document a row belongs to
    skip: str = ""  # why its figures are not facts to check (a test's question, the system's forecast)
    label: str = ""  # what a reviewer sees as its context, when that is not `text` (a YAML number with its key)


SENTENCE_RE = re.compile(r"(?<=[.;!?])\s+(?=[A-Z(\"“$−-])")
_LIST_ITEM_RE = re.compile(r"\s*(?:[-*]|\d+\.)\s")
_SOURCE_HEADER_RE = re.compile(r"(?i)sources?$")


def markdown_units(text: str) -> list[Unit]:
    """The sentences and table rows of a Markdown document, front matter left out. A sentence's context is its
    paragraph's tags and, in a list, the tags of the line that leads into the list. A table row's context is its
    Source cell's tags (else the whole row's), then the tags of the line above the table (its caption) and of the
    line below it (its footnote)."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end > 0:
            text = "\n" * text[:end + 4].count("\n") + text[end + 4:]  # front matter blanked, lines kept
    lines = text.split("\n")
    units: list[Unit] = []
    lead: list[str] = []  # tags of the latest line that is not a list item: a list below it takes them as context
    tables = 0
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if line.lstrip().startswith("|"):
            start = i
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append((i, lines[i]))
                i += 1
            caption = next((TAG_RE.findall(lines[j]) for j in range(start - 1, max(-1, start - 3), -1)
                            if lines[j].strip()), [])
            footnote = next((TAG_RE.findall(lines[j]) for j in range(i, min(len(lines), i + 3)) if lines[j].strip()),
                            [])
            cells = [[c.strip() for c in raw.strip().strip("|").split("|")] for _, raw in rows]
            header = cells[0] if cells else []
            source_col = next((k for k, h in enumerate(header) if _SOURCE_HEADER_RE.match(h)), None)
            for (n, raw), row in zip(rows[2:], cells[2:]):  # after the header and the separator
                row_tags = (TAG_RE.findall(row[source_col]) if source_col is not None and source_col < len(row)
                            else TAG_RE.findall(raw))
                units.append(Unit(n + 1, raw, [], row_tags + caption + footnote,
                                  [(c, TAG_RE.findall(c)) for c in row], header, tables))
            tables += 1
            continue
        own_line = TAG_RE.findall(line)
        if not _LIST_ITEM_RE.match(line):
            lead = own_line
        paragraph = own_line + lead
        for sentence in SENTENCE_RE.split(line):
            units.append(Unit(i + 1, sentence, TAG_RE.findall(sentence), paragraph))
        i += 1
    return units


# Keys whose values are not facts to check: identifiers, rules, thresholds, dates, statuses and bookkeeping.
YAML_SKIP_KEYS = frozenset({
    "threshold", "probability", "id", "pillar", "covers", "effective_from", "due", "made_at", "rule", "op",
    "consecutive", "period", "unit", "fail_if", "warn_if", "warn_rule", "schema_version", "trust_level", "version",
    "input_sha256", "frequency", "status", "reviewed", "as_of", "lookback", "filer", "where", "metric_def", "origin",
    "severity", "evaluate_on", "domain", "category"})
SOURCE_KEYS = ("source", "settlement_source")
NOT_FACT_YAML = "a test's threshold or the system's own forecast, not a fact"
_YAML_STR = "tag:yaml.org,2002:str"
_YAML_NUMBERS = ("tag:yaml.org,2002:int", "tag:yaml.org,2002:float")
_PLAIN_NUMBER_RE = re.compile(r"^-?\d[\d,]*(?:\.\d+)?$")


def yaml_units(text: str) -> list[Unit]:
    """The values of a YAML document: each sentence of a string and each number, with its line. A value takes the
    `source` (and `settlement_source`) of every mapping it sits in as its tags. Keys in YAML_SKIP_KEYS are not facts,
    nor is a test's `question`, nor the `statement` or `note` of the system's own forecast (side: system, or kind:
    prediction), which are skipped with that reason."""
    root = yaml.compose(text, Loader=yaml.SafeLoader)
    units: list[Unit] = []

    def scalar(node: Any, key: str) -> str | None:
        found = next((v for k, v in node.value if isinstance(k, yaml.ScalarNode) and k.value == key), None)
        return found.value if isinstance(found, yaml.ScalarNode) and found.tag == _YAML_STR else None

    def walk(node: Any, path: str, inherited: list[str], skip: str) -> None:
        if isinstance(node, yaml.MappingNode):
            tags = list(inherited) + [s.strip() for s in (scalar(node, k) for k in SOURCE_KEYS) if s is not None]
            forecast = scalar(node, "side") == "system" or scalar(node, "kind") == "prediction"
            for key_node, value in node.value:
                key = str(key_node.value) if isinstance(key_node, yaml.ScalarNode) else ""
                if key in SOURCE_KEYS or key == "generated_by" or key in YAML_SKIP_KEYS:
                    continue
                inner = NOT_FACT_YAML if key == "question" or (forecast and key in ("statement", "note")) else skip
                walk(value, f"{path}.{key}" if path else key, tags, inner)
        elif isinstance(node, yaml.SequenceNode):
            for n, item in enumerate(node.value):
                name = scalar(item, "id") if isinstance(item, yaml.MappingNode) else None
                walk(item, f"{path}[{name or n}]", inherited, skip)
        elif isinstance(node, yaml.ScalarNode) and node.tag in _YAML_NUMBERS:
            value = yaml.safe_load(node.value)
            if isinstance(value, (int, float)) and math.isfinite(value):
                written = node.value if _PLAIN_NUMBER_RE.match(node.value) else repr(value)
                units.append(Unit(node.start_mark.line + 1, written, list(inherited), list(inherited), skip=skip,
                                  label=f"{path}: {written}"))
        elif isinstance(node, yaml.ScalarNode) and node.tag == _YAML_STR:
            for sentence in SENTENCE_RE.split(node.value):
                own = TAG_RE.findall(sentence)
                units.append(Unit(node.start_mark.line + 1, sentence, own or list(inherited),
                                  TAG_RE.findall(node.value) + list(inherited), skip=skip))

    if root is not None:
        walk(root, "", [], "")
    return units


def units_of(doc: str, text: str) -> list[Unit]:
    return yaml_units(text) if doc.endswith((".yml", ".yaml")) else markdown_units(text)


# ---------------------------------------------------------------------------------------------------- sources


@dataclasses.dataclass(frozen=True)
class SourceText:
    """A document a source tag names, read whole: its text (None when it cannot be read), where it came from or why
    it cannot be read, and the XBRL facts filed with it (an EDGAR filing's; the values in USD also in millions)."""

    text: str | None
    note: str
    xbrl: frozenset[float] = frozenset()
    report: bool = False  # the owner's report text, whose locators are its page markers
    path: str | None = None  # the archive document it is (its path in its repository), if it is one


class Sources:
    """What the checker reads to confirm a figure. ArchiveSources is the pipeline's; the tests give their own."""

    def document(self, base: str, exhibit: str | None = None) -> SourceText:
        """The document a tag without its locator names (an exhibit of it when `exhibit` is EX-99.1 or the like)."""
        raise NotImplementedError

    def price(self, tag: str) -> tuple[float | None, str]:
        """A closing price or a Treasury yield a price tag names, or None and the reason it cannot be read."""
        return None, f"{tag}: no price source"


PRICE_TAG_RE = re.compile(r"^(?:[A-Z][A-Z0-9.]*-PRICES-|UST-PARYIELD-)\d{4}-\d{2}-\d{2}(?:#|$)")
_EXHIBIT_RE = re.compile(r"EX-?(\d+(?:\.\d+)?)")


def exhibit_of(locator: str) -> tuple[str | None, str]:
    """A locator's exhibit, written EX-99.1 (archives also write EX99.1), and the rest of the locator."""
    m = _EXHIBIT_RE.match(locator.upper()) if locator else None
    return (f"EX-{m.group(1)}", locator[m.end():].lstrip("-")) if m else (None, locator)


class Evidence(NamedTuple):
    """A document of the archive itself that other documents draw from: the valuation, its working, the dossier."""

    cls: str  # the class of a figure found in it: valuation or dossier
    doc: str  # its path in its repository, which names it; a document is never evidence for itself
    text: str
    company: str | None = None  # another company's valuation: counts only for a sentence that names that company


@dataclasses.dataclass
class Figure:
    doc: str
    line: int
    text: str
    value: float
    unit: str
    decimals: int
    own_tags: list[str]
    context_tags: list[str]
    unit_text: str  # the sentence or table row the figure stands in
    label: str = ""
    cls: str = ""
    how: str = ""
    names: str = ""  # a table cell's column header and row label: the words that say what the figure is


@dataclasses.dataclass(frozen=True)
class CalcError:
    doc: str
    line: int
    expression: str
    computed: float  # what the expression's left side comes to
    context: str
    tags: tuple[str, ...] = ()
    loose: bool = False  # the result holds only within the rounding of the formula's inputs, not as written


@dataclasses.dataclass
class DocumentCheck:
    """The result for one document: every figure with its class, the arithmetic that does not hold, and the tags
    whose documents could not be read (tag -> why)."""

    doc: str
    figures: list[Figure]
    calc_errors: list[CalcError]
    unreadable: dict[str, str]
    output: str | None = None  # the bundle output that writes it, when the runner checks a placement

    @property
    def classes(self) -> dict[str, int]:
        counts = collections.Counter(f.cls for f in self.figures)
        return {cls: counts[cls] for cls in CLASSES if counts[cls]}

    @property
    def to_review(self) -> list[Figure]:
        return [f for f in self.figures if f.cls in REVIEW_CLASSES]


@dataclasses.dataclass
class _Cited:
    text: str | None  # the cited part, or the whole document when the locator was not found (or there is none)
    note: str
    whole: str | None
    located: bool  # whether `text` is the part the locator names
    path: str | None = None  # SourceText.path


class _Numbers:
    """The numbers of a text with where they stand, sorted by value, for matching a figure to its last digit by
    bisection."""

    def __init__(self, text: str):
        items = []
        for m in re.finditer(r"(?<![\w.])\(?(\d[\d,]*(?:\.\d+)?)\)?", text):
            try:
                items.append((float(m.group(1).replace(",", "")), m.start(1), m.end(1)))
            except ValueError:
                pass
        items.sort()
        self.items = items
        self.values = [value for value, _, _ in items]

    def at(self, target: float, decimals: int) -> list[tuple[int, int]]:
        """Where the numbers equal to `target` to its last digit stand: (start, end) of each."""
        tolerance = 0.5 * 10 ** (-decimals) + 1e-9
        target = abs(target)
        out = []
        k = bisect.bisect_left(self.values, target - tolerance)
        while k < len(self.values) and self.values[k] <= target + tolerance:
            out.append(self.items[k][1:])
            k += 1
        return out


SCALES = ((1, 0, "rounded"), (1000, -3, "billions written as millions"), (0.001, 3, "millions written as billions"),
          (0.01, 2, "a percent written as a fraction"), (100, -2, "a fraction written as a percent"),
          (1e-6, 6, "units written as millions"))
SCALE = {"B": 1000.0, "M": 1.0, "K": 0.001}  # to millions
# A figure's label (decision 0038): the words that say what it is, as five-letter stems ("revenue" and "revenues"
# alike), without the words a filing uses everywhere. A few labels stand also for the words filings use instead.
LABEL_STOP = frozenset("""
    about above after again against also among amount amounts annual average based basis before being below between
    billion billions both cents change changed changes compared company consolidated could current decrease decreased
    declined dollars during each ended ending fell fiscal first following from full further given grew growth have
    high higher increase increased including into last less like lower made million millions more most much next number
    other over percent period periods point points prior quarter quarters rate report reported rose same second share
    shares since some such than that their them then there these they third this those three through total under until
    upon used were what when where which while with within would year years""".split())
LABEL_SYNONYMS = {
    "capex": ("payme", "purch", "capit", "expen"), "d&a": ("depre", "amort"), "buybacks": ("repur", "purch"),
    "buyback": ("repur", "purch"), "repurchases": ("purch",), "eps": ("earni",), "revenue": ("sales", "reven"),
    "revenues": ("sales", "reven"), "sales": ("reven",), "dividends": ("divid",), "debt": ("borro", "notes"),
    "payables": ("payab",), "receivables": ("recei",)}
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z&'’-]*")
WINDOW_BEFORE, WINDOW_AFTER = 250, 80  # the part of a source's line or paragraph a label must stand in, around a number
# A number followed by one of these is not a percent; a dollar amount or a count of points is not a number followed by
# a percent sign.
_NOT_PERCENT_AFTER = re.compile(r"\s*(?:points?\b|pts?\b|bps?\b|basis points\b|pp\b|million\b|billion\b|"
                                r"thousand\b|×|x\b|times\b)", re.I)
_PERCENT_AFTER = re.compile(r"\s*(?:%|percent\b|per cent\b)", re.I)
NOT_FACT_WORDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("a heading's number", re.compile(r"^\s*#{1,6}\s")),
    ("a series rule's constant", re.compile(r"margin of safety|entry bar|default tier|§V\d|series rule|"
                                           r"error band|re-run at a discount rate", re.I)),
    ("a threshold, condition or forecast, not a fact", re.compile(
        r"\bat (?:or )?(?:above|below|least|most)\b|\bbelow \d|\babove \d|\bwould\b|\bif\b|\bthreshold|"
        r"\bfail line|\bwarn line|\bprobabilit|\blikely\b|\bpre-registration|\bcandidate:|\bfalls? below|"
        r"\brises? above|\bholding at\b|\bturning positive|\bwill separate", re.I)),
    ("a sensitivity case the valuation chose", re.compile(r"\bsensitivity|points? of NOPAT|\bcase\b.*\bat\b|"
                                                         r"cash conversion k|±", re.I)),
)


class Checker:
    """Checks documents of one company against its sources (Sources) and the archive's own evidence (Evidence).
    It remembers what it has read, so the documents of one placement share the cost of reading their sources."""

    def __init__(self, sources: Sources, company: str, *, evidence: Sequence[Evidence] = (),
                 key_figures: Sequence[Number] = (), names: Mapping[str, Sequence[str]] | None = None):
        self.sources = sources
        self.company = company
        self.evidence = list(evidence)
        self.key_figures = [(n.text, n.value) for n in key_figures]
        self._names = {ticker: re.compile("|".join(rf"\b{re.escape(n)}(?![\w&])" for n in aliases))
                       for ticker, aliases in (names or {}).items() if aliases}
        self._documents: dict[tuple[str, str | None], SourceText] = {}
        self._cited: dict[str, _Cited] = {}
        self._xbrl: dict[str, list[float]] = {}
        self._prices: dict[str, tuple[float | None, str]] = {}
        self._numbers: dict[int, tuple[str, _Numbers]] = {}  # id(text) -> (text, numbers): the text is kept alive
        self._windows: dict[tuple[int, int], frozenset[str]] = {}  # (id(text), start) -> the stems around a number
        self._doc: str | None = None  # the document being checked

    # -- reading sources

    def _document(self, base: str, exhibit: str | None) -> SourceText:
        key = (base, exhibit)
        if key not in self._documents:
            self._documents[key] = self.sources.document(base, exhibit)
            if self._documents[key].text is not None and base not in self._xbrl:
                self._xbrl[base] = sorted({abs(x) for x in self._documents[key].xbrl})
        return self._documents[key]

    def _price(self, tag: str) -> tuple[float | None, str]:
        if tag not in self._prices:
            self._prices[tag] = self.sources.price(tag)
        return self._prices[tag]

    def cited(self, tag: str) -> _Cited:
        """The part of a document a tag's locator names, or the whole document when there is no locator or the part
        cannot be found."""
        if tag in self._cited:
            return self._cited[tag]
        base, _, locator = tag.partition("#")
        exhibit, locator = exhibit_of(locator)
        source = self._document(base, exhibit)
        whole = source.text
        if whole is None or not locator:
            cited = _Cited(whole, source.note, whole, False, source.path)
        else:
            if source.report:
                part = registry._report_page(whole, locator)
                part = None if part is whole else part
            else:
                part = documents.locator_section(whole, locator, "10-Q" if "-10Q-" in base else "10-K")
            cited = (_Cited(part, "the cited part", whole, True, source.path) if part else
                     _Cited(whole, "the whole document (locator not found)", whole, False, source.path))
        self._cited[tag] = cited
        return cited

    def _itself(self, cited: _Cited) -> bool:
        """Whether a tag names the document being checked: a valuation's working that cites its own tag. A document
        is never its own source."""
        return cited.path is not None and cited.path == self._doc

    def note(self, tag: str) -> str:
        """What reading a tag gave: the source's note, or the price or yield it names."""
        if PRICE_TAG_RE.match(tag):
            value, why = self._price(tag)
            return f"{value:g}" if value is not None else why
        cited = self.cited(tag)
        return "the document itself, which is not its own source" if self._itself(cited) else cited.note

    def unreadable(self, tags: Iterable[str]) -> dict[str, str]:
        """The tags (without a page locator; with an exhibit) whose document or price cannot be read, and why."""
        out: dict[str, str] = {}
        for tag in dict.fromkeys(tags):
            if PRICE_TAG_RE.match(tag):
                value, why = self._price(tag)
                if value is None:
                    out.setdefault(tag, why)
                continue
            base, _, locator = tag.partition("#")
            exhibit, _ = exhibit_of(locator)
            source = self._document(base, exhibit)
            if source.text is None:
                out.setdefault(f"{base}#{exhibit}" if exhibit else base, source.note)
        return out

    # -- matching

    def _numbers_of(self, text: str) -> _Numbers:
        entry = self._numbers.get(id(text))
        if entry is None or entry[0] is not text:
            entry = self._numbers[id(text)] = (text, _Numbers(text))
        return entry[1]

    def occurrences(self, fig: Figure, text: str) -> list[tuple[int, int, str]]:
        """Where a figure stands in a text and how: as written, or a number to the figure's last digit at one of
        SCALES, in that order. A number whose unit makes it another kind of quantity is left out (_other_unit)."""
        numbers = self._numbers_of(text)
        bare = re.sub(r"[^\d.]", "", fig.text).strip(".")
        written, scaled = [], []
        for scale, shift, why in SCALES:
            for start, end in numbers.at(abs(fig.value) * scale, fig.decimals + shift):
                if _other_unit(fig, text, end):
                    continue
                if scale == 1 and text[start:end].replace(",", "") == bare:
                    written.append((start, end, "as written"))
                else:
                    scaled.append((start, end, why))
        return written + scaled

    def found_in(self, fig: Figure, text: str) -> str | None:
        """How a figure stands in a text: as written, or a number in it to the figure's last digit at one of SCALES."""
        found = self.occurrences(fig, text)
        return found[0][2] if found else None

    def _window(self, text: str, start: int, end: int) -> frozenset[str]:
        """The word stems around a number of a text: its line, or its paragraph, cut to WINDOW_BEFORE characters before
        it and WINDOW_AFTER after it. A line that holds only numbers before this one takes its label from the line
        before it, as a filing's table row often breaks after its label."""
        key = (id(text), start)
        if key not in self._windows:
            line = text.rfind("\n", 0, start) + 1
            if line > 0 and not re.search(r"[A-Za-z]", text[line:start]):
                line = text.rfind("\n", 0, line - 1) + 1
            first = max(line, start - WINDOW_BEFORE)
            last = text.find("\n", end)
            last = min(len(text) if last < 0 else last, end + WINDOW_AFTER)
            self._windows[key] = _stems(_WORD_RE.findall(text[first:last]))
        return self._windows[key]

    def _labelled(self, fig: Figure, text: str, words: frozenset[str]) -> str | None:
        """How a figure stands in a text next to a word of its label, if it does."""
        return next((how for start, end, how in self.occurrences(fig, text) if words & self._window(text, start, end)),
                    None)

    def singled_out(self, fig: Figure, text: str) -> tuple[str | None, str]:
        """How a text names a figure (decision 0038): the figure stands next to a word of its own label, and neither
        slip of its last digit stands next to that label too. Else None and why not."""
        words = label_of(fig)
        if not words:
            return None, "nothing in its sentence or row says what it is"
        how = self._labelled(fig, text, words)
        if how is None:
            return None, "not next to its label"
        for slip in slips(fig):
            if self._labelled(slip, text, words):
                return None, f"next to its label, but so is {slip.text}, a slip of its last digit"
        return f"{how}, next to its label", ""

    def _in_xbrl(self, fig: Figure, base: str) -> bool:
        values = self._xbrl.get(base)
        if not values:
            return False
        tolerance = 0.5 * 10 ** (-fig.decimals) + 1e-9
        target = abs(fig.value)
        at = bisect.bisect_left(values, target - tolerance)
        return at < len(values) and values[at] <= target + tolerance

    def check_tags(self, fig: Figure, tags: Sequence[str]) -> tuple[str, str] | None:
        """found or xbrl through these tags, else weak when only a short number matched somewhere, else None."""
        for tag in tags:
            if PRICE_TAG_RE.match(tag):
                value, _ = self._price(tag)
                if value is not None and _same(fig.value, value, fig.decimals):
                    return "found", f"{tag}: {value:g}"
        weak = None
        for tag in tags:
            if PRICE_TAG_RE.match(tag):
                continue
            cited = self.cited(tag)
            if cited.text is None or self._itself(cited):
                continue
            how = self.found_in(fig, cited.text)
            if how and (cited.located or distinctive(fig.text)):
                named, why = self.singled_out(fig, cited.text)
                if named:
                    return "found", f"{tag}, {cited.note} ({named})"
                weak = weak or ("weak", f"{tag}, {cited.note} ({how}): {why}")
            elif how:
                weak = weak or ("weak", f"{tag}, {cited.note} ({how}): a short number")
            base = tag.partition("#")[0]
            if self._in_xbrl(fig, base):
                slip = next((s for s in slips(fig) if self._in_xbrl(s, base)), None)
                if distinctive(fig.text) and slip is None:
                    return "xbrl", f"{base} XBRL"
                # a filing's XBRL facts are hundreds of numbers: a short one among them proves no more than a short
                # number somewhere in its text, nor one whose slip is a fact too
                weak = weak or ("weak", f"{base} XBRL: " + (f"so is {slip.text}, a slip of its last digit"
                                                             if distinctive(fig.text) else
                                                             "a short number equal to a fact of the filing"))
            if cited.located and cited.whole is not None:
                how = self.found_in(fig, cited.whole)
                if how and distinctive(fig.text):
                    named, why = self.singled_out(fig, cited.whole)
                    if named:
                        return "found", f"{base}, elsewhere in the document ({named})"
                    weak = weak or ("weak", f"{base}, elsewhere in the document ({how}): {why}")
                elif how:
                    weak = weak or ("weak", f"{base}, elsewhere in the document ({how}): a short number")
        return weak

    # -- classification

    def _mentions(self, company: str, fig: Figure) -> bool:
        pattern = self._names.get(company)
        return bool(pattern and pattern.search(fig.unit_text + " " + " ".join(fig.own_tags + fig.context_tags)))

    def classify(self, fig: Figure, pool: Sequence[tuple[str, float]], evidence: Sequence[Evidence],
                 neighbours: Sequence[Figure], formulas: Sequence[tuple[float, int, str]]) -> None:
        hit = self.check_tags(fig, fig.own_tags) if fig.own_tags else None
        if hit and hit[0] != "weak":
            fig.cls, fig.how = hit
            return
        context = [t for t in fig.context_tags if t not in fig.own_tags]
        hit2 = self.check_tags(fig, context) if context else None
        if hit2 and hit2[0] != "weak":
            fig.cls, fig.how = ("context", hit2[1]) if hit2[0] == "found" else hit2
            return
        weak_evidence = None
        for item in evidence:
            if item.company and not self._mentions(item.company, fig):
                continue  # another company's valuation counts only for a sentence about that company
            how = self.found_in(fig, item.text)
            if how and distinctive(fig.text):
                slip = next((s for s in slips(fig) if self.found_in(s, item.text)), None)
                if slip is None:
                    fig.cls, fig.how = item.cls, f"{item.doc} ({how})"
                    return
                weak_evidence = weak_evidence or ("weak", f"{item.doc} ({how}): so is {slip.text}, a slip of its last "
                                                          "digit")
        own = [(f.text, f.value) for f in neighbours]
        how = recompute(fig, pool, own)
        weak_recomputed = None
        if how:
            for slip in slips(fig):  # what the figures beside it give a slip, with the slip in the figure's place
                slip_how = recompute(slip, _swapped(pool, len(own), fig, slip), _swapped(own, len(own), fig, slip))
                if slip_how:
                    weak_recomputed = ("weak", f"recomputed as {how}, but so is {slip.text}, a slip of its last digit "
                                               f"({slip_how})")
                    break
            else:
                fig.cls, fig.how = "recomputed", how
                return
        formula = next((f for v, d, f in formulas
                        if distinctive(fig.text) and _same(fig.value, v, max(d, fig.decimals))), None)
        if formula:
            fig.cls, fig.how = "recomputed", f"the result of a formula in this document that holds: {formula[:120]}"
            return
        if hit or hit2 or weak_evidence or weak_recomputed:
            fig.cls, fig.how = hit or hit2 or weak_evidence or weak_recomputed
            return
        if _single_digit(fig):
            fig.cls, fig.how = "skipped", "a single digit (a count or an ordinal)"
            return
        for reason, pattern in NOT_FACT_WORDS:
            if pattern.search(fig.unit_text):
                fig.cls, fig.how = "not-a-fact", reason
                return
        tags = fig.own_tags or context
        fig.cls = "unverified"
        fig.how = ("no tag in its sentence, row, paragraph or table" if not tags else
                   "not found in: " + "; ".join(f"{t} ({self.note(t)})" for t in dict.fromkeys(tags)))[:240]

    def check(self, doc: str, text: str, *, output: str | None = None) -> DocumentCheck:
        """Every figure of one document, classified; its calc errors; the tags it cites that cannot be read. `doc` is
        the document's path in its repository: evidence of the same path (or text) is never its own evidence."""
        self._doc = doc
        units = units_of(doc, text)
        evidence = [e for e in self.evidence if e.doc != doc and e.text != text]
        calc, formulas = calc_errors(units, doc)
        table_pool: dict[int, list[tuple[str, float]]] = {}
        for u in units:
            if u.cells is not None and u.table is not None:
                table_pool.setdefault(u.table, []).extend((n.text, n.value) for c, _ in u.cells for n in figures_of(c))
        recent: list[tuple[str, float]] = []
        figures: list[Figure] = []
        for u in units:
            if u.cells is not None:
                row_label = TAG_RE.sub("", u.cells[0][0]) if u.cells else ""
                row = [Figure(doc, u.line, n.text, n.value, n.unit, n.decimals, cell_tags, u.context, u.text, u.label,
                              names=f"{(u.header or [])[k] if k < len(u.header or []) else ''} {row_label}".strip())
                       for k, (cell, cell_tags) in enumerate(u.cells) for n in figures_of(cell)]
            else:
                row = [Figure(doc, u.line, n.text, n.value, n.unit, n.decimals, u.own, u.context, u.text, u.label)
                       for n in figures_of(u.text)]
            own = [(f.text, f.value) for f in row]
            wider = table_pool.get(u.table, []) if u.cells is not None and u.table is not None else recent[-40:]
            pool = own + [p for p in wider if p not in own][:80] + self.key_figures
            if u.cells is None:
                recent.extend(own)
            for fig in row:
                if u.skip:
                    fig.cls, fig.how = "skipped", u.skip
                    continue
                self.classify(fig, pool, evidence, row, formulas)
            figures.extend(row)
        tags = [t for u in units for t in [*u.own, *u.context, *(t for _, cell in u.cells or () for t in cell)]]
        return DocumentCheck(doc, figures, calc, self.unreadable(tags), output)


def slips(fig: Figure) -> list[Figure]:
    """The figure as a slip of its last digit would have written it: one more and one less in that digit (decision
    0038). A match that one of them would get too does not single the figure out."""
    found = list(re.finditer(r"\d[\d,]*(?:\.\d+)?", fig.text))
    if not found:
        return []
    m = found[-1]
    digits = m.group(0).rstrip(",")
    places = len(digits.split(".")[1]) if "." in digits else 0
    out = []
    for step in (1, -1):
        moved = decimal.Decimal(digits.replace(",", "")) + step * decimal.Decimal(1).scaleb(-places)
        if moved < 0:
            continue
        written = f"{moved:,.{places}f}" if "," in digits else f"{moved:.{places}f}"
        out.append(dataclasses.replace(fig, text=fig.text[:m.start()] + written + fig.text[m.start() + len(digits):],
                                       value=float(moved) * (-1 if fig.value < 0 else 1), cls="", how=""))
    return out


def _stems(words: Iterable[str]) -> frozenset[str]:
    """Words as five-letter stems, without LABEL_STOP and words under four letters."""
    out = set()
    for word in words:
        word = word.lower().strip("'’-")
        if len(word) >= 4 and word not in LABEL_STOP:
            out.add(word[:5])
    return frozenset(out)


def label_of(fig: Figure) -> frozenset[str]:
    """The stems of the words that say what a figure is: a table cell's column header and row label; else the eight
    words before it and the three after it in its sentence or YAML value, with the YAML key. A few labels stand also for
    the words filings use (LABEL_SYNONYMS)."""
    if fig.names:
        words = _WORD_RE.findall(fig.names)
    else:
        text = TAG_RE.sub(" ", fig.unit_text)
        at = text.find(fig.text)
        words = _WORD_RE.findall(text[:at] if at >= 0 else text)[-8:]
        words += _WORD_RE.findall(text[at + len(fig.text):])[:3] if at >= 0 else []
        if fig.label:
            words += _WORD_RE.findall(re.sub(r"[_.\[\]]", " ", fig.label.split(":")[0]))
    stems = set(_stems(words))
    for word in words:
        stems.update(LABEL_SYNONYMS.get(word.lower(), ()))
    return frozenset(stems)


def _other_unit(fig: Figure, text: str, end: int) -> bool:
    """Whether the number of a text that ends at `end` is another kind of quantity than the figure: points, basis
    points, a multiple or an amount against a percent; a percent against a dollar amount, millions or points."""
    after = text[end:end + 16].lstrip(")")
    if fig.unit == "%":
        return bool(_NOT_PERCENT_AFTER.match(after))
    if fig.unit in ("M", "B", "K", "pt", "bp") or "$" in fig.text:
        return bool(_PERCENT_AFTER.match(after))
    return False


def _swapped(items: Sequence[tuple[str, float]], n: int, fig: Figure, slip: Figure) -> list[tuple[str, float]]:
    """The first `n` items (a figure's own sentence or row) with the slip in the figure's place."""
    return [(slip.text, slip.value) if k < n and item == (fig.text, fig.value) else item for k, item in enumerate(items)]


def recompute(fig: Figure, pool: Sequence[tuple[str, float]], own: Sequence[tuple[str, float]] | None = None
              ) -> str | None:
    """A sum, difference, product, ratio, percent or growth rate of two figures of `pool` that gives this figure. At
    least one of the two stands in the figure's own sentence or row (`own`). A short figure is recomputed from its own
    sentence or row only, and a single digit never: coincidences are too likely. Nor is a figure recomputed from a
    copy of itself (in any scale): "1 × $2,163M" or "0.1 + $7,186M" proves nothing, and neither does a sum or difference
    with a figure under ten units of this figure's last digit ("$9,370M + 1" for $9,371M)."""
    if _single_digit(fig):
        return None
    own = list(pool) if own is None else list(own)
    if not distinctive(fig.text):
        pool = own
    d = fig.decimals
    targets = {(fig.value, d)}
    if re.search(r"[BK]\b|billion|thousand", fig.text):  # "$35.2B" against inputs in millions
        scale = SCALE["B"] if re.search(r"B\b|billion", fig.text) else SCALE["K"]
        targets.add((fig.value * scale, d - round(math.log10(scale))))

    def itself(x: float) -> bool:
        return any(_same(x, v, dd) for v, dd in targets)

    def scaled(items: Sequence[tuple[str, float]]) -> list[tuple[str, float]]:
        out = [(t, _in_millions(t, x)) for t, x in items]
        out += [(t, x) for t, x in items if re.search(r"[MBK]\b", t)]  # also as written
        return [(t, x) for t, x in out if x != 0 and not itself(x)]

    values, local = scaled(pool), set(scaled(own))
    for (ta, a), (tb, b) in itertools.permutations(values, 2):
        if (ta, a) not in local and (tb, b) not in local:
            continue
        for op, result in (("+", a + b), ("−", a - b), ("×", a * b), ("÷", a / b), ("÷ ×100", 100 * a / b),
                           ("growth %", 100 * (a - b) / abs(b)), ("× ÷100", a * b / 100)):
            for v, dd in targets:
                if _same(v, result, dd) and not (op in ("+", "−") and min(abs(a), abs(b)) < 10 * 10 ** (-dd)):
                    return f"{ta} {op} {tb}"
    return None


def _in_millions(text: str, value: float) -> float:
    unit = re.search(r"([MBK])\b|(billion|million|thousand)", text)
    if not unit:
        return value
    key = unit.group(1) or {"billion": "B", "million": "M", "thousand": "K"}[unit.group(2)]
    return value * SCALE[key]


# ---------------------------------------------------------------------------------------------------- arithmetic

ARITH_CHARS = re.compile(r"[\d\s$,.()+−×÷/^%-]|(?<=\d)[MBK](?![a-z])|(?<=[\d)\s])x(?=[\s(\d])")
_RESULT_RE = re.compile(r"=\s*(\(?[−-]?\$?\d[\d,]*(?:\.\d+)?)\s*(%|[MBK]\b|points?\b)?")
_TWO_OPERANDS_RE = re.compile(r"\d[MBK%]?\)?\s*[+−×÷/^x-]\s*\(?\$?\d")
_OPERATORS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
              ast.Pow: operator.pow}
MAX_EXPONENT = 100  # "1.08 ^ 10" is a growth factor; a larger power is not a figure's arithmetic


def _arith(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _arith(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        value = _arith(node.operand)
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPERATORS:
        left, right = _arith(node.left), _arith(node.right)
        if isinstance(node.op, ast.Pow) and (abs(right) > MAX_EXPONENT or abs(left) > 1e6):
            raise ValueError("a power too large to be a figure's arithmetic")
        return _OPERATORS[type(node.op)](left, right)
    raise ValueError("not arithmetic")


def to_python(expr: str) -> tuple[str, str | None] | None:
    """A formula as written, as Python arithmetic, and the unit of its result: $ and thousands separators dropped;
    ×, x, ÷, − and ^ as *, *, /, - and **; a percent inside as a fraction (21% is 0.21). Operands in M, B and K are
    put in millions when the formula mixes them ($3,700M + $0.5B), and the result is then in M; otherwise the unit
    is dropped, and the result is in the one unit the operands carry (or None). None when it is not arithmetic."""
    e = expr.replace("−", "-").replace("×", "*").replace("÷", "/").replace("^", "**").replace("$", "")
    e = re.sub(r"(?<=\d),(?=\d{3})", "", e)
    units = set(re.findall(r"(?<=\d)([MBK])(?![a-z])", e))
    if len(units) > 1:
        e = re.sub(r"(\d+(?:\.\d+)?)([MBK])(?![a-z])", lambda m: f"({m.group(1)}*{SCALE[m.group(2)]!r})", e)
        unit = "M"
    else:
        e = re.sub(r"(?<=\d)[MBK](?![a-z])", "", e)
        unit = next(iter(units), None)
    e = re.sub(r"(?<=[\d)\s])x(?=[\s(\d])", "*", e)
    e = re.sub(r"(\d+(?:\.\d+)?)\s*%", r"(\1/100)", e)
    e = re.sub(r"(?<![\d.])0+(?=\d)", "", e)  # "08" is 8, not a syntax error
    if re.search(r"[^\d\s.()+\-*/]", e) or not re.search(r"\d", e):
        return None
    return e, unit


def evaluate(expr: str) -> tuple[float, str | None] | None:
    """The value of a formula as written and the unit of the value (to_python), or None when it is not arithmetic
    with an operator, or cannot be evaluated (division by zero, a power too large)."""
    parsed = to_python(expr)
    if parsed is None or not re.search(r"[+\-*/]", parsed[0].strip().lstrip("-(")):
        return None
    try:
        value = _arith(ast.parse(parsed[0].strip(), mode="eval"))
    except (SyntaxError, ValueError, TypeError, ZeroDivisionError, OverflowError, RecursionError):
        return None
    return (value, parsed[1]) if isinstance(value, float) and math.isfinite(value) else None


def _common(a: float, a_unit: str | None, b: float, b_unit: str | None) -> tuple[float, float]:
    """Two values in one scale: both in millions when both carry a unit (M, B or K) and the units differ."""
    if a_unit in SCALE and b_unit in SCALE and a_unit != b_unit:
        return a * SCALE[a_unit], b * SCALE[b_unit]
    return a, b


def operand_value(token: str) -> float | None:
    number = re.sub(r"[^\d.]", "", token)
    if not number:
        return None
    token = token.strip()
    negative = token.startswith(("−", "-")) or (token.startswith("(") and token.endswith(")"))
    return -float(number) if negative else float(number)


def left_expression(text: str, end: int) -> str:
    """The formula that ends just before `end` (an "="): the longest run of figures, units, brackets and operators."""
    k = end
    while k > 0 and ARITH_CHARS.match(text, k - 1):
        k -= 1
    expr = text[k:end].strip()
    while expr.count(")") > expr.count("(") and not expr.endswith(")"):  # an unmatched ")" from the prose
        expr = expr[expr.find(")") + 1:].strip()
    while expr.count("(") > expr.count(")"):
        expr = expr[expr.find("(") + 1:].strip() if expr.startswith("(") else expr + ")"
    return expr.strip(" ,;:")


def _written(expr: str) -> set[str]:
    """The numbers a formula writes, without thousands separators."""
    return {n.replace(",", "") for n in re.findall(r"\d[\d,]*(?:\.\d+)?", expr)}


def _rounding(literal: str, written: set[str]) -> float:
    """Half a unit of the last digit of a number the formula writes with decimals, or with four digits or more (a
    rounded amount). A short whole number (a count, a power, 100, 365) and a number the conversion added are exact."""
    if literal not in written:
        return 0.0
    if "." in literal:
        return 0.5 * 10 ** -len(literal.split(".")[1])
    return 0.5 if len(literal.lstrip("0")) >= 4 else 0.0


def _bounds(node: ast.AST, py: str, written: set[str]) -> tuple[float, float]:
    """The least and the most a formula comes to when each number it writes is anywhere within its rounding."""
    if isinstance(node, ast.Expression):
        return _bounds(node.body, py, written)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        half = _rounding(ast.get_source_segment(py, node) or "", written)
        return float(node.value) - half, float(node.value) + half
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        low, high = _bounds(node.operand, py, written)
        return (-high, -low) if isinstance(node.op, ast.USub) else (low, high)
    if isinstance(node, ast.BinOp) and type(node.op) in _OPERATORS:
        (a, b), (c, d) = _bounds(node.left, py, written), _bounds(node.right, py, written)
        if isinstance(node.op, ast.Add):
            return a + c, b + d
        if isinstance(node.op, ast.Sub):
            return a - d, b - c
        if isinstance(node.op, ast.Div) and c <= 0 <= d:
            raise ValueError("a divisor whose rounding reaches zero")
        if isinstance(node.op, ast.Pow) and (a <= 0 or max(abs(c), abs(d)) > MAX_EXPONENT or b > 1e6):
            raise ValueError("a power outside a figure's arithmetic")
        corners = [_OPERATORS[type(node.op)](x, y) for x in (a, b) for y in (c, d)]
        return min(corners), max(corners)
    raise ValueError("not arithmetic")


def bounds(expr: str) -> tuple[float, float] | None:
    """What a formula as written comes to within the rounding of the numbers it writes, in the unit of evaluate(); None
    when that cannot be bounded."""
    parsed = to_python(expr)
    if parsed is None:
        return None
    py = parsed[0].strip()
    try:
        return _bounds(ast.parse(py, mode="eval"), py, _written(expr))
    except (SyntaxError, ValueError, TypeError, ZeroDivisionError, OverflowError, RecursionError):
        return None


def _within(low: float, high: float, target: float, half: float) -> bool:
    """Whether a stated result lies within a formula's bounds, give or take its own rounding. Signs are ignored, as
    for any figure: a decline may be written as a positive number."""
    small, large = sorted((abs(low), abs(high)))
    if low <= 0 <= high:
        small = 0.0
    return small - half <= abs(target) <= large + half


def _counterparts(a: ast.AST, b: ast.AST) -> list[tuple[ast.AST, ast.AST]]:
    """The parts of two formulas that stand for each other where the two have the same shape, down to each part of
    the second that is a number or of another shape."""
    if isinstance(a, ast.BinOp) and isinstance(b, ast.BinOp) and type(a.op) is type(b.op):
        return _counterparts(a.left, b.left) + _counterparts(a.right, b.right)
    if isinstance(a, ast.UnaryOp) and isinstance(b, ast.UnaryOp) and type(a.op) is type(b.op):
        return _counterparts(a.operand, b.operand)
    return [(a, b)]


def chain_status(left: str, right: str) -> str:
    """Whether a chain "left = right", whose right side writes the left side again, holds (decision 0038).

    "holds": the left side's exact value lies within the rounding of the right side's numbers, part by part where the
    two have the same shape ("1.053^10 ÷ 1.072^10 = 1.676 ÷ 2.004": 1.053^10 against 1.676, 1.072^10 against 2.004).
    "loose": it does only within the rounding of the left side's numbers too. "wrong": neither."""
    pa, pb = to_python(left), to_python(right)
    if pa is None or pb is None:
        return "holds"
    sa, sb = pa[0].strip(), pb[0].strip()
    try:
        ta, tb = ast.parse(sa, mode="eval").body, ast.parse(sb, mode="eval").body
    except SyntaxError:
        return "holds"
    mixed = pa[1] in SCALE and pb[1] in SCALE and pa[1] != pb[1]  # "$3,700M + $0.5B = $4.2B": in millions
    fa, fb = (SCALE[pa[1]], SCALE[pb[1]]) if mixed else (1.0, 1.0)
    worst = "holds"
    for na, nb in [(ta, tb)] if mixed else _counterparts(ta, tb):
        try:
            exact = fa * _arith(na)
            a_low, a_high = (fa * x for x in _bounds(na, sa, _written(left)))
            b_low, b_high = (fb * x for x in _bounds(nb, sb, _written(right)))
        except (ValueError, TypeError, ZeroDivisionError, OverflowError, RecursionError):
            try:
                same = math.isclose(fa * _arith(na), fb * _arith(nb), rel_tol=1e-9, abs_tol=1e-12)
            except (ValueError, TypeError, ZeroDivisionError, OverflowError, RecursionError):
                continue
            worst = worst if same else "wrong"
            continue
        if b_low - 1e-9 <= exact <= b_high + 1e-9:
            continue
        status = "loose" if a_low <= b_high + 1e-9 and b_low <= a_high + 1e-9 else "wrong"
        worst = "wrong" if "wrong" in (worst, status) else status
    return worst


def calc_errors(units: Sequence[Unit], doc: str) -> tuple[list[CalcError], list[tuple[float, int, str]]]:
    """The arithmetic written out in a document that does not hold, and (value, decimals, formula) of each formula
    that does: a figure equal to such a result is recomputed. A result holds when it is the formula's exact value as
    written, to the result's last digit; one that is right only within the rounding of the numbers the formula writes
    is a calc error too (`loose`), for a reviewer (decision 0038). A chain's right side is checked by chain_status."""
    errors: list[CalcError] = []
    results: list[tuple[float, int, str]] = []
    for u in units:
        tags = tuple(dict.fromkeys(u.own + u.context))
        text = TAG_RE.sub("", u.text)
        text = re.sub(r"^\s*[-*]\s+", "", text)  # a list bullet, not a minus sign
        text = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", "DATE", text)  # a date is not a subtraction
        text = re.sub(r"\b\d+-(?=year|day|week|month|quarter|point)", "N-", text)
        for m in _RESULT_RE.finditer(text):
            left = left_expression(text, m.start())
            if not left or "=" in left or not _TWO_OPERANDS_RE.search(left):
                continue  # one figure on the left: "8.99% = the Treasury + the premium" is checked by recomputing
            value = evaluate(left)
            stated = operand_value(m.group(1))
            if value is None or stated is None:
                continue
            k = m.start() + 1  # a chain "A = B ÷ C = D": the right side is a formula up to the next "="
            while k < len(text) and text[k] != "=" and ARITH_CHARS.match(text, k):
                k += 1
            right = text[m.start() + 1:k].strip(" ,;:")
            if re.search(r"\d[MBK%]?\)?\s*[+−×÷/^-]\s*\(?\$?\d", right):
                other = evaluate(right)
                if other is not None:
                    status = chain_status(left, right)
                    if status != "holds":
                        errors.append(CalcError(doc, u.line, f"{left} = {right}", round(value[0], 6),
                                                u.label or u.text, tags, loose=status == "loose"))
                    continue
            raw, unit = m.group(1), (m.group(2) or "").strip()
            decimals = len(re.sub(r"[^\d.]", "", raw).split(".")[1]) if "." in raw else 0
            half = 0.5 * 10 ** (-decimals) + 1e-9  # the stated result's own rounding
            low, high = bounds(left) or (value[0], value[0])
            factor, target = 1.0, stated
            if value[1] in SCALE and unit in SCALE and value[1] != unit:  # "$3.7B + $0.5B = $4,200M": in millions
                factor, target, half = SCALE[value[1]], stated * SCALE[unit], half * SCALE[unit]
            scales = (factor, 100 * factor) if unit == "%" or abs(value[0]) < 1 else (factor,)
            formula = f"{left} = {raw}{m.group(2) or ''}"
            if any(abs(abs(s * value[0]) - abs(target)) <= half for s in scales):
                results.append((stated, decimals, formula))
            else:
                errors.append(CalcError(doc, u.line, formula, round(value[0], 6), u.label or u.text, tags,
                                        loose=any(_within(s * low, s * high, target, half) for s in scales)))
        if u.cells and u.header and any(re.match(r"\s*[−+=-]", h) for h in u.header[1:]):
            total = None
            for (cell, _), head in zip(u.cells[1:], u.header[1:]):
                found = figures_of(cell)
                if not found:
                    total = None
                    break
                x, decimals = found[0].value, found[0].decimals
                op = head.strip()[:1]
                if total is None and op not in "−+=-":
                    total = x
                elif op in "−-" and total is not None:
                    total -= x
                elif op == "+" and total is not None:
                    total += x
                elif op == "=" and total is not None:
                    if not _same(x, total, decimals):
                        errors.append(CalcError(doc, u.line, u.text.strip()[:140], round(total, 4), u.text, tags))
                    else:
                        results.append((x, decimals, u.text.strip()[:100]))
    return errors, results


# ---------------------------------------------------------------------------------------------------- the archive


class ArchiveFiles:
    """Both repositories and the workspace as a placement will leave them: a file the placement is about to write
    reads as its new content (`planned`, keyed by repository name and path), any other file as it is on disk."""

    def __init__(self, roots: Roots, planned: Mapping[tuple[str, str], str] | None = None):
        self.roots = roots
        self.planned = dict(planned or {})

    def path(self, repo: str, rel: str) -> Path:
        return (self.roots.public if repo == registry.PUBLIC_REPO else self.roots.private) / rel

    def read(self, repo: str, rel: str) -> str | None:
        if (repo, rel) in self.planned:
            return self.planned[(repo, rel)]
        path = self.path(repo, rel)
        try:
            return path.read_text(encoding="utf-8") if path.is_file() else None
        except (OSError, UnicodeDecodeError):
            return None

    def glob(self, repo: str, pattern: str) -> list[str]:
        """Paths matching `pattern` on disk or planned, sorted."""
        root = self.path(repo, "")
        found = {p.relative_to(root).as_posix() for p in root.glob(pattern) if p.is_file()}
        found |= {rel for r, rel in self.planned if r == repo and fnmatch.fnmatchcase(rel, pattern)}
        return sorted(found)

    def load_yaml(self, repo: str, rel: str) -> Any:
        text = self.read(repo, rel)
        try:
            return yaml.safe_load(text) if text is not None else None
        except yaml.YAMLError:
            return None


def registered(files: ArchiveFiles, ticker: str) -> dict[str, dict[str, Any]]:
    """tag -> entry of a company's registered sources: its public, then its private sources.yml (the first entry of a
    tag wins, as registry.registered_sources reads them)."""
    out: dict[str, dict[str, Any]] = {}
    for repo in (registry.PUBLIC_REPO, registry.PRIVATE_REPO):
        data = files.load_yaml(repo, f"companies/{ticker}/sources.yml")
        for entry in (data.get("sources") if isinstance(data, dict) else None) or []:
            if isinstance(entry, dict) and entry.get("tag"):
                out.setdefault(str(entry["tag"]), entry)
    return out


def dossier_path(files: ArchiveFiles, ticker: str) -> tuple[str, str] | None:
    """(repository, path) of a company's dossier: private, or public for a company whose dossier is public (MSFT)."""
    rel = f"companies/{ticker}/dossier.md"
    return next(((repo, rel) for repo in (registry.PRIVATE_REPO, registry.PUBLIC_REPO)
                 if files.read(repo, rel) is not None), None)


_RPT_RE = re.compile(r"^([A-Z][A-Z0-9.]*)-RPT\d+-")
_DOSSIER_TAG_RE = re.compile(r"^([A-Z][A-Z0-9.]*)-DOSSIER-")
_PRICES_RE = re.compile(r"^([A-Z][A-Z0-9.]*)-PRICES-(\d{4}-\d{2}-\d{2})$")
_UST_RE = re.compile(r"^UST-PARYIELD-(\d{4}-\d{2}-\d{2})$")
_TEXT_SUFFIXES = (".md", ".txt", ".yml", ".yaml")
PRICES_CACHE = Path("inputs") / "prices"  # in the workspace: price histories and Treasury tables, as 01C reads them


class _Offline(Exception):
    """Raised instead of a network request in offline mode."""


def _no_fetch(url: str) -> Any:
    raise _Offline(url)


def _short(exc: BaseException) -> str:
    return " ".join(str(exc).split())[:120] or type(exc).__name__


def _locates(entry: Mapping[str, Any]) -> bool:
    """Whether a registered entry says where its document is."""
    return any(entry.get(key) for key in ("location", "accession", "url", "issuer_cik"))


class ArchiveSources(Sources):
    """The pipeline's own reading of source tags (thesis-ci SPEC 3.3), as the fact audit reads them:
    - <T>-RPT<n>-<date>: the owner's report text in the workspace (inputs/text/reports__<T>.txt), by page;
    - <T>-DOSSIER-<date>: the company's dossier; IND-...: the public industry modules;
    - a tag registered with a private location (<T>-VAL-<date>, a valuation's working): that file;
    - <T>-PRICES-<date>[#<day>]: the close on that day in the price history read on <date>; UST-PARYIELD-<date>: the
      10-year Treasury par yield of that date (pipeline/prices.py, cached in the workspace's inputs/prices);
    - any other tag: its EDGAR filing, found from the registered entry of the company (or of the tag's own issuer) as
      edgar.filing_for_entry finds it; the primary document, or the EX-99 exhibit the locator names; and the XBRL
      facts filed with that accession.
    Offline, EDGAR, prices and yields come from their caches only. Nothing is fetched from anywhere else."""

    def __init__(self, files: ArchiveFiles, company: str, *, offline: bool = False,
                 client: edgar.EdgarClient | None = None):
        self.files = files
        self.company = company
        self.offline = offline
        self._client = client
        self._issuers: dict[str, Any] = {}
        self._registers: dict[str, dict[str, dict[str, Any]]] = {}
        self._facts: dict[str, Any] = {}
        self._histories: dict[tuple[str, dt.date], list[prices.Close] | str] = {}
        self._filer: edgar.Filer | None | bool = False

    @property
    def client(self) -> edgar.EdgarClient:
        if self._client is None:
            self._client = edgar.EdgarClient(offline=self.offline)
        return self._client

    def _register(self, ticker: str) -> dict[str, dict[str, Any]]:
        if ticker not in self._registers:
            self._registers[ticker] = registered(self.files, ticker)
        return self._registers[ticker]

    def entry(self, base: str) -> dict[str, Any]:
        """The registered entry of a tag: in the company's sources, else in those of the company the tag names. An
        entry that does not say where its document is (no location, accession, URL or issuer) does not hide the
        named company's own entry: MCD's register once listed SPGI-VAL-2026-09-30 with no location."""
        own = self._register(self.company).get(base) or {}
        if not _locates(own) and (m := re.match(r"([A-Z][A-Z0-9.]*)-", base)) and m.group(1) != self.company:
            other = self._register(m.group(1)).get(base) or {}
            if _locates(other) or not own:
                return other
        return own

    def _company_filer(self) -> edgar.Filer | None:
        if self._filer is False:
            try:
                self._filer = edgar.load_filer(self.company, self.files.roots.public)
            except (ValueError, OSError, yaml.YAMLError):
                self._filer = None  # a new archive has no thesis.yml yet: its tags resolve through SEC's ticker table
        return self._filer or None

    def document(self, base: str, exhibit: str | None = None) -> SourceText:
        if m := _RPT_RE.match(base):
            path = self.files.roots.workspace / "inputs" / "text" / f"reports__{m.group(1)}.txt"
            if path.is_file():
                return SourceText(path.read_text(encoding="utf-8"), "the owner's report text", report=True)
            return SourceText(None, f"no report text {path.name} in the workspace")
        if m := _DOSSIER_TAG_RE.match(base):
            where = dossier_path(self.files, m.group(1))
            return (SourceText(self.files.read(*where), "the dossier", path=where[1]) if where else
                    SourceText(None, f"{m.group(1)} has no dossier"))
        if base.startswith("IND-"):
            modules = self.files.glob(registry.PUBLIC_REPO, "industries/*/*.md")
            text = "\n".join(self.files.read(registry.PUBLIC_REPO, rel) or "" for rel in modules)
            return SourceText(text, "the industry modules") if modules else SourceText(None, "no industry modules")
        if PRICE_TAG_RE.match(base):
            return SourceText(None, "a price or a yield, read as one")
        entry = self.entry(base)
        location = str(entry.get("location") or "")
        if location.startswith("private:"):
            rel = location[len("private:"):]
            text = self.files.read(registry.PRIVATE_REPO, rel) if rel.endswith(_TEXT_SUFFIXES) else None
            return SourceText(text, "a private working file", path=rel) if text is not None else \
                SourceText(None, f"not readable: {location}")
        try:
            fields = {k: v for k, v in entry.items() if k in ("accession", "issuer_cik", "url", "form")}
            filing, _ = edgar.filing_for_entry({"tag": base, **fields}, company=self._company_filer(),
                                               client=self.client, issuers=self._issuers)
            name = filing.primary_document
            if exhibit:
                docs = edgar.filing_documents(filing.cik, filing.accession, client=self.client, primary_document=name)
                named = [d.name for d in docs if d.exhibit == exhibit and not d.name.lower().endswith(".pdf")]
                if not named:
                    return SourceText(None, f"the filing has no {exhibit}")
                name = named[0]
            text = registry.document_text(self.client.get_bytes(f"{filing.folder_url}/{name}"), name)
        except (edgar.EdgarError, ValueError, OSError) as exc:
            return SourceText(None, ("" if entry else "registered in no sources.yml, and ") + f"unresolved on EDGAR: "
                                    f"{_short(exc)}")
        return SourceText(text, f"EDGAR {filing.form}", self._xbrl(filing))

    def _xbrl(self, filing: edgar.Filing) -> frozenset[float]:
        """The numeric XBRL facts filed with this accession (companyfacts); amounts in USD also in millions."""
        cik = edgar.normalize_cik(filing.cik)
        if cik not in self._facts:
            try:
                self._facts[cik] = self.client.get_json(f"{edgar.DATA_BASE}/api/xbrl/companyfacts/CIK{cik}.json")
            except (edgar.EdgarError, ValueError, OSError):
                self._facts[cik] = {}
        out: set[float] = set()
        for taxonomy in (self._facts[cik].get("facts") or {}).values():
            for concept in taxonomy.values():
                for unit, rows in (concept.get("units") or {}).items():
                    for row in rows:
                        value = row.get("val")
                        if row.get("accn") == filing.accession and isinstance(value, (int, float)) \
                                and not isinstance(value, bool):
                            out.add(float(value))
                            if unit == "USD":
                                out.add(float(value) / 1e6)
        return frozenset(out)

    def price(self, tag: str) -> tuple[float | None, str]:
        base, _, locator = tag.partition("#")
        cache = self.files.roots.workspace / PRICES_CACHE
        fetch = _no_fetch if self.offline else None
        if m := _PRICES_RE.match(base):
            symbol = registry.PRICE_SYMBOLS.get(m.group(1), m.group(1))
            as_of = dt.date.fromisoformat(m.group(2))
            key = (symbol, as_of)
            if key not in self._histories:
                try:
                    kwargs = {"fetch": fetch} if fetch else {}
                    self._histories[key] = prices.history(symbol, as_of, cache_dir=cache, **kwargs)
                except prices.PriceError as exc:
                    self._histories[key] = (f"offline: no cached prices of {symbol} read on {as_of}" if self.offline
                                            else _short(exc))
            history = self._histories[key]
            if isinstance(history, str):
                return None, history
            try:
                day = dt.date.fromisoformat(locator) if locator else as_of
            except ValueError:
                return None, f"{locator!r} is not a trading day YYYY-MM-DD"
            closes = [c for c in history if c.date <= day]
            return (closes[-1].close, "") if closes else (None, f"no close of {symbol} on or before {day}")
        if m := _UST_RE.match(base):
            day = dt.date.fromisoformat(m.group(1))
            try:
                reading = prices.ten_year_yield(day, cache_dir=cache, **({"fetch": fetch} if fetch else {}))
            except prices.PriceError as exc:
                return None, (f"offline: no cached Treasury table for {day}" if self.offline else _short(exc))
            if reading.date != day:  # the tag names the reading's own date (prices.Yield.tag)
                return None, f"no 10-year Treasury reading dated {day} (the last before it is {reading.date})"
            return reading.percent, ""
        return None, f"{tag} is not a price or yield tag"


def company_names(files: ArchiveFiles, ticker: str) -> list[str]:
    """How a document names a company: its ticker, its short name from thesis.yml (registry.short_name), and the first
    word of a short name of several words when it is not a word that also names other things (Berkshire for
    Berkshire Hathaway; not American for American Express, nor S&P for S&P Global, which also names the index)."""
    data = files.load_yaml(registry.PUBLIC_REPO, f"companies/{ticker}/thesis.yml")
    short = registry.short_name(data.get("name") if isinstance(data, dict) else None, ticker)
    names = [ticker, short]
    first = short.split()[0] if len(short.split()) > 1 else ""
    if first.isalpha() and first not in SHARED_FIRST_WORDS:
        names.append(first)
    return list(dict.fromkeys(n for n in names if n))


SHARED_FIRST_WORDS = frozenset({"American", "General", "United", "National", "International", "First", "New", "Bank"})


def working_of(files: ArchiveFiles, ticker: str, valuation: str) -> str | None:
    """The path of the working of the valuation in force: the file the private sources.yml registers for the
    <T>-VAL-<date> tag the valuation cites (placement points it at the working of the version in force), else the
    latest placed working (runs/<T>/*-01C*/valuation_md.md)."""
    register = registered(files, ticker)
    cited = set(re.findall(rf"\b{re.escape(ticker)}-VAL-\d{{4}}-\d{{2}}-\d{{2}}\b", valuation))
    for tag in sorted(cited, reverse=True):
        location = str(register.get(tag, {}).get("location") or "")
        if location.startswith("private:") and files.read(registry.PRIVATE_REPO, location[len("private:"):]):
            return location[len("private:"):]
    placed = files.glob(registry.PRIVATE_REPO, f"runs/{ticker}/*-01C*/valuation_md.md")
    return placed[-1] if placed else None


def archive_evidence(files: ArchiveFiles, company: str) -> tuple[list[Evidence], list[Number], dict[str, list[str]]]:
    """The archive's own documents the company's other documents draw from, in the order they are tried: its
    valuation.yml and the working of the version in force, every other company's valuation.yml (for a sentence that
    names that company), its dossier. Also the valuation's key figures (the figures in the first 4,000 characters of
    valuation.yml, which recomputation may use) and the names of the other companies."""
    evidence: list[Evidence] = []
    rel = f"companies/{company}/valuation.yml"
    valuation = files.read(registry.PRIVATE_REPO, rel)
    if valuation is not None:
        evidence.append(Evidence("valuation", rel, valuation))
        working = working_of(files, company, valuation)
        if working:
            evidence.append(Evidence("valuation", working, files.read(registry.PRIVATE_REPO, working) or ""))
    names: dict[str, list[str]] = {}
    for other in files.glob(registry.PRIVATE_REPO, "companies/*/valuation.yml"):
        ticker = other.split("/")[1]
        if ticker != company:
            evidence.append(Evidence("valuation", other, files.read(registry.PRIVATE_REPO, other) or "", ticker))
            names[ticker] = company_names(files, ticker)
    where = dossier_path(files, company)
    if where is not None:
        evidence.append(Evidence("dossier", where[1], files.read(*where) or ""))
    key_figures = figures_of(valuation[:4000]) if valuation else []
    return evidence, key_figures, names


def archive_checker(files: ArchiveFiles, company: str, *, offline: bool = False,
                    sources: Sources | None = None) -> Checker:
    """A checker for one company's documents with the pipeline's sources (or `sources`, for tests) and evidence."""
    evidence, key_figures, names = archive_evidence(files, company)
    return Checker(sources or ArchiveSources(files, company, offline=offline), company, evidence=evidence,
                   key_figures=key_figures, names=names)


# ---------------------------------------------------------------------------------------------------- review


def trimmed(text: str, figure: str, width: int = CONTEXT_CHARS) -> str:
    """The context a reviewer sees: the sentence or row without its [src:] tags (the item lists them), whitespace
    collapsed, cut at word boundaries to about `width` characters around the figure's first occurrence."""
    flat = " ".join(_TAG_TOKEN_RE.sub("", text).split())
    if len(flat) <= width:
        return flat
    at = max(flat.find(figure), 0)
    start = min(max(at + len(figure) // 2 - width // 2, 0), len(flat) - width)
    end = start + width
    if start > 0:  # begin after a space, never after the figure's start
        space = flat.find(" ", start, at)
        start = space + 1 if space >= 0 else start
    if end < len(flat):  # end at a space, never before the figure's end
        space = flat.rfind(" ", at + len(figure), end)
        end = space if space >= 0 else end
    return ("…" if start > 0 else "") + flat[start:end].strip() + ("…" if end < len(flat) else "")


def item_key(item: Mapping[str, Any]) -> tuple[str, str, str]:
    """An item is the same item again when its document, figure and context are: lines shift, and are not compared."""
    context = " ".join(_TAG_TOKEN_RE.sub("", str(item.get("context") or "")).split())
    return str(item.get("doc") or ""), str(item.get("figure") or "").strip(), context


def review_items(checks: Iterable[DocumentCheck]) -> list[dict[str, Any]]:
    """One item per figure that needs a verdict (weak, unverified) and per calc error, in document order, with
    `verdict` and `how` left for the reviewer. Figures with the same key (the same figure in the same context) are one
    item."""
    items: dict[tuple[str, str, str], dict[str, Any]] = {}
    for check in checks:
        for fig in check.to_review:
            item = {"doc": check.doc, "figure": fig.text, "class": fig.cls,
                    "tags": list(dict.fromkeys(fig.own_tags + fig.context_tags)),
                    "context": trimmed(fig.label or fig.unit_text, fig.text), "line": fig.line, "checker": fig.how,
                    "verdict": None, "how": None}
            items.setdefault(item_key(item), item)
        for error in check.calc_errors:
            item = {"doc": check.doc, "figure": error.expression, "class": CALC_ERROR, "tags": list(error.tags),
                    "context": trimmed(error.context, error.expression.split("=")[0].strip()), "line": error.line,
                    "checker": f"the left side comes to {error.computed:,.6g}" + (
                        " as written; the other side agrees only within the rounding of the figures written"
                        if error.loose else ""), "verdict": None, "how": None}
            items.setdefault(item_key(item), item)
    return list(items.values())


def merge_verdicts(items: Sequence[Mapping[str, Any]], previous: Iterable[Any]
                   ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The items with the verdict, `how` and any other field a reviewer wrote on the previous item they match, and the
    previous items with a verdict that match none (kept, so that no verdict is lost)."""
    earlier: dict[tuple[str, str, str], dict[str, Any]] = {}
    for old in previous:
        if isinstance(old, Mapping) and old.get("doc") and old.get("figure") is not None:
            earlier.setdefault(item_key(old), dict(old))
    used: set[tuple[str, str, str]] = set()
    out = []
    for item in items:
        key = item_key(item)
        old = earlier.get(key)
        if old is None:
            out.append(dict(item))
            continue
        used.add(key)
        out.append({**item, **{k: v for k, v in old.items() if k not in ITEM_FIELDS}})
    stale = [old for key, old in earlier.items() if key not in used and old.get("verdict") not in (None, "")]
    return out, stale


def carry_verdicts(items: Sequence[Mapping[str, Any]], previous: Iterable[Any], origin: str) -> list[dict[str, Any]]:
    """The items, where one still has no verdict taking the verdict (and `how` and any other field a reviewer wrote)
    of the same item in an earlier review file; `verdict_from` names that file (`origin`)."""
    earlier: dict[tuple[str, str, str], dict[str, Any]] = {}
    for old in previous:
        if isinstance(old, Mapping) and old.get("doc") and old.get("figure") is not None and verdict_of(old):
            earlier.setdefault(item_key(old), dict(old))
    out = []
    for item in items:
        old = earlier.get(item_key(item)) if verdict_of(item) is None else None
        if old is None:
            out.append(dict(item))
        else:
            out.append({**item, **{k: v for k, v in old.items() if k not in ITEM_FIELDS},
                        "verdict_from": old.get("verdict_from") or origin})
    return out


def verdict_of(item: Mapping[str, Any]) -> str | None:
    """The item's verdict in its canonical spelling ("Not a fact" is not-a-fact), or None when there is none."""
    value = item.get("verdict")
    if value is None:
        return None
    text = re.sub(r"[\s_]+", "-", str(value).strip().lower())
    return text or None


@dataclasses.dataclass
class Gate:
    """The gate's reading of the items that need review: placement goes ahead only when `passed`."""

    items: list[dict[str, Any]]
    open: list[dict[str, Any]]  # no verdict, or a word that is not a verdict
    blocked: list[dict[str, Any]]  # wrong or cannot-verify: the text must be corrected

    @property
    def passed(self) -> bool:
        return not self.open and not self.blocked

    @property
    def accepted(self) -> dict[str, int]:
        counts = collections.Counter(verdict_of(i) for i in self.items)
        return {v: counts[v] for v in ACCEPTED_VERDICTS if counts[v]}


def gate(items: Sequence[Mapping[str, Any]]) -> Gate:
    open_, blocked = [], []
    for item in items:
        verdict = verdict_of(item)
        if verdict in ACCEPTED_VERDICTS:
            continue
        (blocked if verdict in BLOCKING_VERDICTS else open_).append(dict(item))
    return Gate([dict(i) for i in items], open_, blocked)


def describe_item(item: Mapping[str, Any]) -> str:
    """One line per item, like a linter's: document:line, the figure, its class, the verdict or the checker's note."""
    verdict = verdict_of(item)
    state = (f"verdict {verdict}" if verdict in VERDICTS else
             f"verdict {item.get('verdict')!r} is not one of {', '.join(VERDICTS)}" if verdict else
             str(item.get("checker") or ""))
    return f"{item.get('doc')}:{item.get('line')}: {item.get('figure')} [{item.get('class')}] {state}"[:300]


def load_review(path: Path) -> list[dict[str, Any]]:
    """The items of a review file and the earlier items it kept (no_longer_found), or [] when there is none."""
    data = registry.load_yaml_file(path)
    if not isinstance(data, dict):
        return []
    return [i for key in ("items", "no_longer_found") for i in data.get(key) or [] if isinstance(i, dict)]


def summary(checks: Sequence[DocumentCheck], result: Gate) -> dict[str, Any]:
    """Counts for factcheck.yml and placement.yml: per document, and over all of them."""
    per_doc = {}
    for check in checks:
        per_doc[check.doc] = {**({"output": check.output} if check.output else {}), "figures": len(check.figures),
                              "classes": check.classes, "calc_errors": len(check.calc_errors),
                              "to_review": sum(1 for i in result.items if i.get("doc") == check.doc),
                              **({"unreadable_tags": dict(check.unreadable)} if check.unreadable else {})}
    return {"documents": per_doc,
            "totals": {"figures": sum(len(c.figures) for c in checks), "to_review": len(result.items),
                       "accepted": sum(result.accepted.values()), "open": len(result.open),
                       "blocking": len(result.blocked), "verdicts": result.accepted}}


def review_document(checks: Sequence[DocumentCheck], items: Sequence[Mapping[str, Any]],
                    stale: Sequence[Mapping[str, Any]], *, bundle: str | None, checked_at: str,
                    offline: bool) -> dict[str, Any]:
    """The content of factcheck.yml."""
    result = gate(items)
    data: dict[str, Any] = {
        "factcheck_version": REVIEW_VERSION,
        "bundle": bundle,
        "checked_at": checked_at,
        "offline": offline,
        "how_to_review": "Give every item a verdict and say how you checked it (`how`). Placement goes ahead only when "
                         f"every verdict is one of {', '.join(ACCEPTED_VERDICTS)}; {' and '.join(BLOCKING_VERDICTS)} "
                         "stop it until the text is corrected and the item no longer appears. Run the fact check "
                         "again after a correction: verdicts stay with their items (document, figure and context).",
        "verdicts": dict(VERDICTS),
        **summary(checks, result),
        "items": [dict(i) for i in items],
    }
    if stale:
        data["no_longer_found"] = [dict(i) for i in stale]
    return data
