"""Which documents a post-earnings step reads, and the sentence of a document that carries a fact
(docs/decisions/0024). This module calls no model and opens no network connection: it plans from the submissions
data it is given, and pipeline/registry.py fetches what it selects.

- parse_where() reads a qualitative test's free-text `where` (thesis-ci SPEC 4.2) clause by clause and turns the
  parts that name the company's own EDGAR filings into requests: earnings releases, other 8-Ks with their items and
  EX-99 exhibits, 10-Q, 10-K, 6-K, 20-F, proxy statements, ownership reports, merger filings. Everything else it
  reports as not supplied, with the reason: call transcripts (not on EDGAR, STATUS T17), other issuers' filings,
  regulators', courts' and legislatures' documents, web pages. Nothing is guessed. The judge (14T) may answer "no"
  only when every document `where` requires is in its input, so a clause that also names something the pipeline
  cannot fetch is always reported, even when the company's own filings in it are supplied.
- lookback_periods() and select_filings() pick the filings of a lookback window: periodic reports by fiscal period,
  the annual report and the proxy statement in force as of the event, current reports by filing date.
- extract_sections() keeps, of a 10-K, 10-Q or 20-F, only the sections a clause names (MD&A, risk factors, legal
  proceedings, the business section, the financial statements, a named note, a 20-F item), found by the filing's own
  item and note headings; when a named section cannot be found, the whole filing is kept, and the caller records
  which sections were cut.
- tag_with_ordinal() gives a filing its archive tag (thesis-ci SPEC 3.3), numbering current reports of one form
  filed on the same day (-2, -3, ...), as SPEC 3.3 does.
- cut_excerpt() finds the sentence of a document that contains a value: 04A's source_excerpt, cut by the pipeline
  (prompt 04, 16A).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from . import edgar, slicing

# Request kinds (what a clause of `where` asks for).
EARNINGS_RELEASE = "earnings_release"  # 8-K Item 2.02 and its EX-99 exhibits; a foreign issuer's results 6-K
CURRENT_REPORT = "current_report"  # 8-Ks (optionally with given items) or 6-Ks: primary document and EX-99 exhibits
PRESS_RELEASE = "press_release"  # the company's press releases: EX-99 exhibits of its 8-Ks, or its 6-Ks
QUARTERLY_REPORT = "quarterly_report"  # 10-Q
ANNUAL_REPORT = "annual_report"  # 10-K, 20-F, 40-F
PROXY = "proxy"  # DEF 14A
OWNERSHIP = "ownership"  # Forms 3, 4, 5
BENEFICIAL_OWNERSHIP = "beneficial_ownership"  # Schedules 13D and 13G
MERGER = "merger"  # S-4, F-4, DEFM14A, 425
REQUEST_KINDS = (EARNINGS_RELEASE, CURRENT_REPORT, PRESS_RELEASE, QUARTERLY_REPORT, ANNUAL_REPORT, PROXY, OWNERSHIP,
                 BENEFICIAL_OWNERSHIP, MERGER)

FORMS: dict[str, frozenset[str]] = {
    QUARTERLY_REPORT: frozenset({"10-Q", "10-Q/A"}),
    ANNUAL_REPORT: frozenset({"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}),
    PROXY: frozenset({"DEF 14A", "DEFA14A"}),
    OWNERSHIP: frozenset({"3", "4", "5", "3/A", "4/A", "5/A"}),
    BENEFICIAL_OWNERSHIP: frozenset({"SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A", "SCHEDULE 13D", "SCHEDULE 13D/A",
                                     "SCHEDULE 13G", "SCHEDULE 13G/A"}),
    MERGER: frozenset({"S-4", "S-4/A", "F-4", "F-4/A", "DEFM14A", "425"}),
}
MAX_FILINGS_PER_KIND = 40  # ownership reports can run to hundreds a year; the most recent ones are kept, and the cut is reported

NOT_ON_EDGAR = "earnings call materials, transcripts and investor-day materials are not on EDGAR, and the pipeline has " \
               "no source for them yet (STATUS T17)"

_SENTENCE_BREAK_RE = re.compile(r"(?:(?<=[a-z0-9)\]])|(?<=\d-[A-Z]))\.\s+(?=[A-Z])")  # "... the 10-K. Press"
_FORMS_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (QUARTERLY_REPORT, re.compile(r"(?<![\w-])10-Qs?(?![\w-])")),
    (ANNUAL_REPORT, re.compile(r"(?<![\w-])(?:10-Ks?|20-Fs?|40-F)(?![\w-])")),
    (PROXY, re.compile(r"(?<![\w-])DEF\s*14A(?![\w-])|\bproxy statements?\b", re.I)),
    (OWNERSHIP, re.compile(r"\bForms?\s+[345](?:\s*(?:,|and|/)\s*[345])*\b")),
    (BENEFICIAL_OWNERSHIP, re.compile(r"\b(?:Schedules?\s+)?13[DG](?:/[DG])?\b")),
    (MERGER, re.compile(r"(?<![\w-])(?:S-4|F-4|DEFM14A)(?![\w-])|\bmerger proxy\b", re.I)),
)
_8K_RE = re.compile(r"(?<![\w-])8-Ks?(?![\w-])")
_6K_RE = re.compile(r"(?<![\w-])6-Ks?(?![\w-])")
_ITEMS_RE = re.compile(r"\bItems?\s+(\d{1,2}\.\d{2}(?:\s*(?:,|and|&)\s*\d{1,2}\.\d{2})*)", re.I)
_EXHIBITS_RE = re.compile(r"\bExhibits?\s+(99\.\d{1,2}(?:\s*(?:,|and|&)\s*99\.\d{1,2})*)", re.I)
_EARNINGS_RE = re.compile(r"\b(?:earnings|results)\b", re.I)
_PRESS_RE = re.compile(r"\bpress releases?\b|\bannouncements?\b", re.I)
_OWN_ANNOUNCER_RE = re.compile(r"\b(?:company|company's|acquisition|M&A|merger|investor)\s+(?:press releases?|"
                               r"announcements?)\b|\b6-Ks?\s+announcements?\b", re.I)
_CALL_RE = re.compile(r"\b(?:call materials|call transcripts?|earnings calls?|transcripts?|webcasts?|replays?|"
                      r"investor day materials|investor materials|call)\b", re.I)
# Words that name documents from outside the company's own EDGAR filings: regulators, courts, legislatures, other
# parties, data providers, the web. A clause that contains one is reported as not (fully) supplied.
_THIRD_PARTY_RE = re.compile(
    r"\b(?:regulators?|regulatory authorit\w*|government\w*|agenc(?:y|ies)|courts?|Circuit|Congress\w*|legislat\w*|"
    r"laws?|bills?|executive orders?|final rules?|implementing rules|Official Journal|Commission|Department of|Bureau|"
    r"Comptroller|Justice|SAMR|SEC's|NAIC|ACER|Task Force|exchanges?|ETF issuers|gateways|platforms|publishers?|"
    r"Sensor Tower|FRED|blogs?|web(?:site)?s?|rating agenc\w*|other parties|both companies|third[- ]part\w*|"
    r"parent companies|outside EDGAR|outside the company's filings|secondary|S-1)\b", re.I)
_POSSESSIVE_RE = re.compile(r"(?<![\w&.-])([A-Z][A-Za-z0-9&.]*(?:\s+[A-Z][A-Za-z0-9&.]*)?)['’]s\b")
_NOT_OWNERS = frozenset({"sec", "the", "this", "that", "both"})


# ---------------------------------------------------------------------------------------------------- periods


def period_index(period: str) -> int:
    """FY2026Q3 -> an integer that orders quarters (FY<year> counts as its fourth quarter)."""
    year, quarter = edgar.parse_period(period)
    return year * 4 + (quarter or 4) - 1


def period_from_index(index: int) -> str:
    return f"FY{index // 4}Q{index % 4 + 1}"


def shift_period(period: str, quarters: int) -> str:
    return period_from_index(period_index(period) + quarters)


def lookback_periods(period: str, lookback: int) -> list[str]:
    """The `lookback` fiscal quarters ending with `period`, oldest first (the current one included, SPEC 4.2)."""
    count = max(1, int(lookback))
    return [shift_period(period, -k) for k in range(count - 1, -1, -1)]


# ---------------------------------------------------------------------------------------------------- where


@dataclasses.dataclass(frozen=True)
class DocRequest:
    """One kind of the company's own filings that a clause of `where` names."""

    kind: str
    items: frozenset[str] = frozenset()  # 8-K items; empty means any
    exhibits: frozenset[str] = frozenset()  # EX-99.x exhibits; empty means the primary document and every EX-99
    clause: str = ""
    sections: frozenset[str] | None = None  # periodic reports: the sections the clause names; None: the whole filing


@dataclasses.dataclass
class WherePlan:
    where: str
    requests: list[DocRequest]
    not_supplied: list[str]


def split_clauses(text: str) -> list[str]:
    """`where` split at semicolons and sentence ends outside parentheses; commas stay inside a clause (they list
    documents that share a qualifier, such as "... of the top publishers")."""
    text = " ".join(str(text).split())
    clauses, depth, start = [], 0, 0
    breaks = {m.start() for m in _SENTENCE_BREAK_RE.finditer(text)}
    for i, ch in enumerate(text):
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        elif depth == 0 and (ch == ";" or i in breaks):
            clauses.append(text[start:i])
            start = i + 1
    clauses.append(text[start:])
    return [c.strip(" ,.") for c in clauses if c.strip(" ,.")]


def _is_own(name: str, owners: Iterable[str]) -> bool:
    low = name.casefold()
    for owner in owners:
        own = owner.casefold()
        if low == own or low.startswith(own) or own.startswith(low):
            return True
        if len(low) >= 3 and len(own) >= 3 and low[:3] == own[:3]:  # Amex's for American Express
            return True
    return False


def other_issuers(clause: str, owners: Iterable[str]) -> list[str]:
    """Capitalized possessives in a clause that do not name the company itself ("Delta's", "OpenAI's")."""
    owners = [o for o in owners if o]
    found = []
    for match in _POSSESSIVE_RE.finditer(clause):
        name = match.group(1)
        words = name.split()
        if any(w.casefold() in _NOT_OWNERS for w in words[:1]):
            continue
        if not any(_is_own(w, owners) for w in words) and name not in found:
            found.append(name)
    return found


def _numbers(pattern: re.Pattern[str], text: str) -> frozenset[str]:
    out: set[str] = set()
    for match in pattern.finditer(text):
        out.update(re.findall(r"\d{1,2}\.\d{1,2}", match.group(1)))
    return frozenset(out)


def parse_clause(clause: str, *, foreign: bool, owners: Iterable[str]) -> tuple[list[DocRequest], list[str]]:
    """(requests, not-supplied notes) for one clause of `where`."""
    owners = list(owners)
    requests: list[DocRequest] = []
    notes: list[str] = []
    others = other_issuers(clause, owners)
    third_party = _THIRD_PARTY_RE.search(clause)
    own_marker = bool(_OWN_ANNOUNCER_RE.search(clause)) or any(
        _is_own(m.group(1).split()[0], owners) for m in _POSSESSIVE_RE.finditer(clause)
        if m.group(1).split()[0].casefold() not in _NOT_OWNERS)
    items = _numbers(_ITEMS_RE, clause)
    exhibits = frozenset(f"EX-{n}" for n in _numbers(_EXHIBITS_RE, clause))
    earnings = bool(_EARNINGS_RE.search(clause)) or items == {"2.02"}
    if _8K_RE.search(clause):
        if earnings and (not items or "2.02" in items):
            requests.append(DocRequest(EARNINGS_RELEASE, frozenset({"2.02"}), exhibits, clause))
            rest = items - {"2.02"}
            if rest:
                requests.append(DocRequest(CURRENT_REPORT, rest, exhibits, clause))
        else:
            requests.append(DocRequest(CURRENT_REPORT, items, exhibits, clause))
    if _6K_RE.search(clause):
        if earnings or exhibits or re.search(r"\bquarterly\s+6-Ks?\b", clause):
            requests.append(DocRequest(EARNINGS_RELEASE, frozenset(), exhibits, clause))
        else:
            requests.append(DocRequest(CURRENT_REPORT, frozenset(), exhibits, clause))
    for kind, pattern in _FORMS_RES:
        if pattern.search(clause):
            sections = section_requests(clause) if kind in PERIODIC_KINDS else None
            requests.append(DocRequest(kind, clause=clause, sections=sections))
    if _PRESS_RE.search(clause) and (own_marker or not (third_party or others)):
        if earnings and not any(r.kind == EARNINGS_RELEASE for r in requests):
            requests.append(DocRequest(EARNINGS_RELEASE, frozenset({"2.02"}) if not foreign else frozenset(),
                                       exhibits, clause))
        elif not earnings and not any(r.kind == CURRENT_REPORT for r in requests):  # current reports hold them
            requests.append(DocRequest(PRESS_RELEASE, clause=clause))
    call = _CALL_RE.search(clause)
    if call:
        notes.append(f"{call.group(0)} ({clause}): {NOT_ON_EDGAR}")
    if not requests and not call:
        notes.append(f"{clause}: not a filing of the company on EDGAR; the pipeline does not fetch it")
    elif requests and (third_party or others):
        who = f" of {', '.join(others)}" if others else ""
        notes.append(f"{clause}: also names documents{who} outside the company's own EDGAR filings, which the pipeline "
                     "does not fetch; only the company's own filings in this clause are supplied")
    return requests, notes


def parse_where(where: Any, *, foreign: bool, owners: Iterable[str]) -> WherePlan:
    """Requests for the company's own EDGAR filings named by a test's `where`, and what cannot be supplied."""
    text = " ".join(where) if isinstance(where, (list, tuple)) else str(where or "")
    owners = list(owners)
    requests: list[DocRequest] = []
    notes: list[str] = []
    for clause in split_clauses(text):
        found, missing = parse_clause(clause, foreign=foreign, owners=owners)
        requests += [r for r in found if r not in requests]
        notes += missing
    if not text.strip():
        notes.append("the test has no `where`; nothing was supplied")
    return WherePlan(text, requests, notes)


# ---------------------------------------------------------------------------------------------------- selection


@dataclasses.dataclass(frozen=True)
class Selection:
    """A filing to supply and which of its documents: the primary document and/or EX-99 exhibits."""

    filing: edgar.Filing
    primary: bool
    exhibits: bool
    only_exhibits: frozenset[str] = frozenset()  # when exhibits is True: these EX-99.x only (empty = every EX-99)
    kind: str = ""
    sections: frozenset[str] | None = None  # a periodic report's sections to keep; None: the whole filing

    def merged(self, other: Selection) -> Selection:
        """The union of two selections of one filing (another clause, another test)."""
        only = frozenset() if (not self.only_exhibits or not other.only_exhibits) else \
            self.only_exhibits | other.only_exhibits
        sections = None if self.sections is None or other.sections is None else self.sections | other.sections
        return Selection(self.filing, self.primary or other.primary, self.exhibits or other.exhibits, only,
                         self.kind, sections)


def _in(day: dt.date, start: dt.date, end: dt.date) -> bool:
    return start <= day <= end


def _periodic_label(filing: edgar.Filing, cal: edgar.FiscalCalendar) -> str | None:
    if filing.report_date is None:
        return None
    try:
        return cal.quarter_label(filing.report_date)
    except ValueError:
        return None


def window_start(period: str, lookback: int, cal: edgar.FiscalCalendar) -> dt.date:
    """The first day of the first quarter of the lookback window."""
    before = shift_period(lookback_periods(period, lookback)[0], -1)
    year, quarter = edgar.parse_period(before)
    return cal.quarter_end(year, quarter or 4) + dt.timedelta(days=1)  # the day after the quarter before it


def select_filings(requests: Sequence[DocRequest], filings: Sequence[edgar.Filing],
                   events: Sequence[edgar.EarningsEvent], *, period: str, lookback: int, as_of: dt.date,
                   cal: edgar.FiscalCalendar, foreign: bool) -> tuple[list[Selection], list[str]]:
    """The filings the requests name within the lookback window, and notes on what was cut or not found.

    Periodic reports go by fiscal period; the annual report and the proxy statement in force as of `as_of` are always
    included when asked for (a 10-Q only updates the 10-K's risk factors, and the proxy covers a year); current
    reports go by filing date, from the first day of the window to `as_of`.
    """
    periods = set(lookback_periods(period, lookback))
    start = window_start(period, lookback, cal)
    known = [f for f in filings if f.filing_date <= as_of]
    release_period = {e.accession: e.period for e in events}
    chosen: dict[str, Selection] = {}
    notes: list[str] = []

    def add(filing: edgar.Filing, *, primary: bool, exhibits: bool, only: frozenset[str] = frozenset(),
            kind: str, sections: frozenset[str] | None = None) -> None:
        new = Selection(filing, primary, exhibits, only, kind, sections)
        old = chosen.get(filing.accession)
        chosen[filing.accession] = old.merged(new) if old is not None else new

    for request in requests:
        found = 0
        if request.kind == EARNINGS_RELEASE:
            for f in known:
                if release_period.get(f.accession) in periods:
                    add(f, primary=foreign and not request.exhibits, exhibits=True, only=request.exhibits,
                        kind=request.kind)
                    found += 1
        elif request.kind in (CURRENT_REPORT, PRESS_RELEASE):
            forms = ("6-K",) if foreign else ("8-K",)
            for f in known:
                if f.form not in forms or not _in(f.filing_date, start, as_of):
                    continue
                if request.items and not (set(f.items) & request.items):
                    continue
                press = request.kind == PRESS_RELEASE
                add(f, primary=not press and not request.exhibits, exhibits=True, only=request.exhibits,
                    kind=request.kind)
                found += 1
        elif request.kind == QUARTERLY_REPORT:
            for f in known:
                if f.form in FORMS[QUARTERLY_REPORT] and _periodic_label(f, cal) in periods:
                    add(f, primary=True, exhibits=False, kind=request.kind, sections=request.sections)
                    found += 1
        elif request.kind in (ANNUAL_REPORT, PROXY):
            forms = FORMS[request.kind]
            matching = [f for f in known if f.form in forms]
            in_window = [f for f in matching if (_periodic_label(f, cal) in periods if request.kind == ANNUAL_REPORT
                                                 else _in(f.filing_date, start, as_of))]
            latest = [max(matching, key=lambda f: (f.filing_date, f.accession))] if matching else []
            for f in {x.accession: x for x in [*in_window, *latest]}.values():
                add(f, primary=True, exhibits=False, kind=request.kind,
                    sections=request.sections if request.kind == ANNUAL_REPORT else None)
                found += 1
        else:
            forms = FORMS[request.kind]
            matching = sorted((f for f in known if f.form.upper() in forms and _in(f.filing_date, start, as_of)),
                              key=lambda f: (f.filing_date, f.acceptance_datetime or "", f.accession))
            if len(matching) > MAX_FILINGS_PER_KIND:
                notes.append(f"{len(matching)} {request.kind.replace('_', ' ')} filings in the window; the latest "
                             f"{MAX_FILINGS_PER_KIND} are supplied")
                matching = matching[-MAX_FILINGS_PER_KIND:]
            for f in matching:
                add(f, primary=True, exhibits=False, kind=request.kind)
                found += 1
        if not found:
            notes.append(f"no {request.kind.replace('_', ' ')} filing of the company in the window "
                         f"({start} to {as_of}) for: {request.clause}")
    ordered = sorted(chosen.values(), key=lambda s: (s.filing.filing_date, s.filing.acceptance_datetime or "",
                                                     s.filing.accession))
    return ordered, notes


# ---------------------------------------------------------------------------------------------------- sections

PERIODIC_KINDS = frozenset({QUARTERLY_REPORT, ANNUAL_REPORT})
# What a clause of `where` calls a section of a periodic report -> the section's key.
SECTION_WORDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("mdna", re.compile(r"MD&A|management['\u2019]s discussion|operating (?:and financial )?review|Item 5 operating",
                        re.I)),
    ("risk_factors", re.compile(r"risk factors", re.I)),
    ("legal_proceedings", re.compile(r"legal proceedings|legal and regulatory", re.I)),
    ("business", re.compile(r"business overview|regulat\w+ section|supervision and regulation|legal and regulatory",
                            re.I)),
    ("financial_statements", re.compile(r"statements? of (?:income|operations|cash flows)|cash flow statement|"
                                        r"balance sheet|financial statements|notes to the", re.I)),
)
# Named notes to the financial statements: (key, how a clause names it, how the note's heading reads).
NOTE_WORDS: tuple[tuple[str, re.Pattern[str], re.Pattern[str]], ...] = (
    ("note:subsequent events", re.compile(r"subsequent[- ]events?", re.I), re.compile(r"subsequent event", re.I)),
    ("note:contingencies", re.compile(r"contingenc", re.I), re.compile(r"contingenc", re.I)),
    ("note:revenue", re.compile(r"revenue note", re.I), re.compile(r"^revenue", re.I)),
    ("note:equity method", re.compile(r"equity[- ]method", re.I), re.compile(r"equity[- ]method|investments", re.I)),
    ("note:credit losses", re.compile(r"credit losses", re.I), re.compile(r"credit loss", re.I)),
    ("note:inventory", re.compile(r"inventory note", re.I), re.compile(r"inventor", re.I)),
)
_ITEM_REF_RE = re.compile(r"\bItems?\s+(\d{1,2}[A-Z]?)(?:\.[A-Z])?(?![.\d])")  # 20-F "Item 3.D", "Item 16I"; not "5.02"
# The item that holds a section, by form: (part, item); part None means any part.
ITEM_OF: dict[str, dict[str, tuple[str | None, str]]] = {
    "10-K": {"mdna": (None, "7"), "risk_factors": (None, "1A"), "legal_proceedings": (None, "3"),
             "business": (None, "1"), "financial_statements": (None, "8")},
    "10-Q": {"mdna": ("I", "2"), "risk_factors": ("II", "1A"), "legal_proceedings": ("II", "1"),
             "financial_statements": ("I", "1")},
    "20-F": {"mdna": (None, "5"), "risk_factors": (None, "3"), "legal_proceedings": (None, "8"),
             "business": (None, "4"), "financial_statements": (None, "18")},
}
SECTION_TITLES = {"mdna": re.compile(r"management['\u2019]s discussion and analysis|operating and financial review", re.I),
                  "risk_factors": re.compile(r"^(?:item\s+\w+\W+)?risk factors", re.I),
                  "legal_proceedings": re.compile(r"^(?:item\s+\w+\W+)?legal proceedings", re.I)}
# Where a section found by its title ends, besides the next item heading: a filing laid out with a cross-reference
# index (McDonald's 10-K) has no "Item 7" / "Item 8" headings in its body, only these titles.
SECTION_ENDS = {"mdna": re.compile(r"(?:item\s*8\b.*|financial statements and supplementary data)[.:]?", re.I)}
MIN_SECTION_CHARS = 2_000  # an item "section" shorter than this is a table-of-contents or index entry, not the body
SECTION_LABELS = {"mdna": "MD&A", "risk_factors": "risk factors", "legal_proceedings": "legal proceedings",
                  "business": "business", "financial_statements": "financial statements and notes"}
_ITEM_LINE_RE = re.compile(r"^\s*ITEM\s+(\d{1,2}[A-Z]?)\s*[.:\u2013\u2014-]?\s*(.*)$", re.I)
_PART_LINE_RE = re.compile(r"^\s*PART\s+(IV|I{1,3})\b", re.I)
_NOTE_LINE_RE = re.compile(r"^\s*(?:NOTE\s+)?(\d{1,2})\s*[.:\u2013\u2014-]\s*(\S.{2,120})$", re.I)


def section_requests(clause: str) -> frozenset[str] | None:
    """The sections of a periodic report a clause names ("the MD&A and subsequent-events notes of the 10-Q"); None
    when it names none, which means the whole filing."""
    keys = {key for key, pattern in SECTION_WORDS if pattern.search(clause)}
    keys |= {key for key, pattern, _ in NOTE_WORDS if pattern.search(clause)}
    keys |= {f"item:{m.group(1).upper()}" for m in _ITEM_REF_RE.finditer(clause)}
    return frozenset(keys) or None


def form_family(form: str) -> str | None:
    key = edgar.form_key(form)
    return {"10K": "10-K", "10KA": "10-K", "10Q": "10-Q", "10QA": "10-Q", "20F": "20-F", "20FA": "20-F",
            "40F": "20-F"}.get(key)


@dataclasses.dataclass(frozen=True)
class _Heading:
    line: int
    part: str | None
    item: str
    text: str


def _headings(lines: Sequence[str]) -> list[_Heading]:
    found, part = [], None
    for i, line in enumerate(lines):
        if len(line) > 200:
            continue
        match = _PART_LINE_RE.match(line)
        if match:
            part = match.group(1).upper()
            continue
        match = _ITEM_LINE_RE.match(line)
        if match:
            found.append(_Heading(i, part, match.group(1).upper(), line.strip()))
    return found


def _longest(candidates: Sequence[tuple[int, int, str]]) -> tuple[int, int, str] | None:
    """Of (start, end, heading) candidates, the body section: the longest (a table of contents entry is short)."""
    return max(candidates, key=lambda c: c[1] - c[0]) if candidates else None


def _item_span(lines: Sequence[str], heads: Sequence[_Heading], part: str | None,
               item: str) -> tuple[int, int, str] | None:
    candidates = []
    for n, head in enumerate(heads):
        if head.item == item and (part is None or head.part == part):
            end = heads[n + 1].line if n + 1 < len(heads) else len(lines)
            candidates.append((head.line, end, head.text))
    return _longest(candidates)


def _title_span(lines: Sequence[str], heads: Sequence[_Heading], title: re.Pattern[str],
                ends: re.Pattern[str] | None = None) -> tuple[int, int, str] | None:
    starts = [i for i, line in enumerate(lines) if len(line) <= 150 and title.search(line.strip())]
    candidates = []
    for start in starts:
        end = next((h.line for h in heads if h.line > start), len(lines))
        if ends is not None:
            end = next((i for i in range(start + 1, end) if len(lines[i]) <= 150 and ends.fullmatch(lines[i].strip())),
                       end)
        candidates.append((start, end, lines[start].strip()))
    return _longest(candidates)


def _chars(lines: Sequence[str], span: tuple[int, int, str] | None) -> int:
    return sum(len(line) for line in lines[span[0] + 1:span[1]]) if span else 0


def _note_span(lines: Sequence[str], heads: Sequence[_Heading], title: re.Pattern[str]) -> tuple[int, int, str] | None:
    notes = [(i, m.group(1), m.group(2)) for i, line in enumerate(lines) if (m := _NOTE_LINE_RE.match(line))]
    candidates = []
    for n, (start, number, text) in enumerate(notes):
        if not title.search(text.strip()):
            continue
        following = [i for i, num, _ in notes[n + 1:] if num != number]
        end = min([*following[:1], *(h.line for h in heads if h.line > start), len(lines)])
        candidates.append((start, end, lines[start].strip()))
    return _longest(candidates)


def extract_sections(text: str, form: str, wanted: frozenset[str]) -> tuple[str, list[str], list[str]]:
    """(text, sections found, sections not found) of a 10-K, 10-Q or 20-F: each named section under a
    "--- section: <heading> ---" line, in document order. A named note that has no heading of its own (a filing
    without a subsequent-events note, or a heading the reader does not recognize) is replaced by the whole financial
    statements with their notes. When any other named section is not found, the whole text comes back (with the
    missing ones listed), so the reader never gets less than the clause asked for."""
    family = form_family(form)
    lines = text.split("\n")
    heads = _headings(lines)
    spans: list[tuple[int, int, str, str]] = []
    missing: list[str] = []
    absent: list[str] = []

    def span_of(key: str) -> tuple[int, int, str] | None:
        if key.startswith("item:"):
            return _item_span(lines, heads, None, key[5:])
        if key.startswith("note:"):
            title = next((t for k, _, t in NOTE_WORDS if k == key), None)
            found = _note_span(lines, heads, title) if title else None
            if found is None and title is not None and not any(title.search(line.strip()) for line in lines):
                absent.append(key)  # the filing never names it: nothing to supply
                return (0, 0, "")
            return found or span_of("financial_statements")
        where = ITEM_OF.get(family or "", {}).get(key)
        span = _item_span(lines, heads, *where) if where else None
        if _chars(lines, span) < MIN_SECTION_CHARS and key in SECTION_TITLES:
            titled = _title_span(lines, heads, SECTION_TITLES[key], SECTION_ENDS.get(key))
            span = max((c for c in (span, titled) if c), key=lambda c: _chars(lines, c), default=None)
        return span

    for key in sorted(wanted):
        span = span_of(key)
        if key in absent:
            continue
        if span is None or span[1] - span[0] < 2:
            missing.append(key)
        else:
            spans.append((span[0], span[1], span[2], key))
    if missing or not spans:
        return text, sorted({s[3] for s in spans}), missing or sorted(set(wanted) - set(absent)) or sorted(wanted)
    spans.sort()
    kept: list[tuple[int, int, str, str]] = []
    for span in spans:  # a note inside a kept item (the financial statements) is not repeated
        if kept and span[0] < kept[-1][1]:
            last = kept[-1]
            kept[-1] = (last[0], max(last[1], span[1]), last[2], last[3])
            continue
        kept.append(span)
    parts = [f"--- section: {heading} ---\n" + "\n".join(lines[start + 1:end]).strip() for start, end, heading, _ in kept]
    parts += [f"--- not in this filing: {', '.join(describe_sections(absent))} (the filing never names it) ---"
              ] if absent else []
    return "\n\n".join(parts) + "\n", sorted({s[3] for s in spans}), []


TRANSACTION_CODES = {"P": "open-market purchase", "S": "open-market sale", "A": "grant or award", "M": "option exercise",
                     "F": "shares withheld for tax", "G": "gift", "C": "conversion", "D": "disposition to the issuer",
                     "J": "other", "X": "exercise of an in-the-money derivative"}


_MONEY_RE = re.compile(r"\$\s*\d")


def render_ownership(xml: bytes) -> str | None:
    """A Form 3, 4 or 5 in a few lines, from its XML: who reported and their relationship to the issuer; the
    transactions summed by day, security, code and direction (a sale executed in many tranches is one line); holdings;
    and the footnotes that state no price. Transaction prices and price footnotes are left out (00 section H2 names
    the prices a document may show). None when the XML cannot be read."""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None

    def text(node: Any, path: str) -> str:
        found = node.find(path) if node is not None else None
        return " ".join((found.text or "").split()) if found is not None and found.text else ""

    def number(value: str) -> float | None:
        try:
            return float(value)
        except ValueError:
            return None

    footnotes = {f.get("id"): " ".join("".join(f.itertext()).split()) for f in root.findall("footnotes/footnote")}
    kept = {fid for fid, body in footnotes.items() if not _MONEY_RE.search(body)}
    lines = [f"Form {text(root, 'documentType') or '?'}; period of report {text(root, 'periodOfReport') or '?'}"]
    for owner in root.findall("reportingOwner"):
        rel = owner.find("reportingOwnerRelationship")
        roles = [label for tag, label in (("isDirector", "director"), ("isOfficer", "officer"),
                                          ("isTenPercentOwner", "10% owner"), ("isOther", "other"))
                 if text(rel, tag) in ("1", "true")]
        title = text(rel, "officerTitle") or text(rel, "otherText")
        lines.append(f"Reporting owner: {text(owner, 'reportingOwnerId/rptOwnerName')} ("
                     + ", ".join(roles + ([title] if title else [])) + ")")
    groups: dict[tuple[str, ...], dict[str, Any]] = {}
    for table, kind in (("nonDerivativeTable", "non-derivative"), ("derivativeTable", "derivative")):
        node = root.find(table)
        for row in list(node) if node is not None else []:
            security = text(row, "securityTitle/value")
            owned = text(row, "postTransactionAmounts/sharesOwnedFollowingTransaction/value")
            nature = {"D": "direct", "I": "indirect"}.get(text(row, "ownershipNature/directOrIndirectOwnership/value"),
                                                          "")
            if row.tag.endswith("Holding"):
                lines.append(f"Holding ({kind}): {security}; shares owned {owned or '?'}; {nature}")
                continue
            code = text(row, "transactionCoding/transactionCode")
            direction = "acquired" if text(row, "transactionAmounts/transactionAcquiredDisposedCode/value") == "A" \
                else "disposed"
            key = (kind, text(row, "transactionDate/value"), security, code, direction, nature)
            group = groups.setdefault(key, {"count": 0, "shares": 0.0, "unknown": False, "owned": "", "notes": []})
            shares = number(text(row, "transactionAmounts/transactionShares/value"))
            group["count"] += 1
            group["shares"] += shares or 0.0
            group["unknown"] |= shares is None
            group["owned"] = owned or group["owned"]
            group["notes"] += [f.get("id") for f in row.iter() if f.tag == "footnoteId" and f.get("id") in kept
                               and f.get("id") not in group["notes"]]
    for (kind, day, security, code, direction, nature), g in groups.items():
        shares = "?" if g["unknown"] else f"{g['shares']:,.0f}"
        lines.append(f"Transaction ({kind}) {day}: {security}; code {code} ({TRANSACTION_CODES.get(code, 'see the form')})"
                     f"; {g['count']} transaction(s), {shares} shares {direction}; owned after {g['owned'] or '?'}; "
                     f"{nature}" + (f"; footnotes {', '.join(g['notes'])}" if g["notes"] else ""))
    lines += [f"Footnote {fid}: {footnotes[fid]}" for fid in footnotes if fid in kept]
    if len(kept) < len(footnotes):
        lines.append(f"({len(footnotes) - len(kept)} footnote(s) stating prices left out)")
    return "\n".join(lines) + "\n"


def describe_sections(keys: Sequence[str]) -> list[str]:
    return [SECTION_LABELS.get(k) or (f"item {k[5:]}" if k.startswith("item:") else k.replace("note:", "note: "))
            for k in keys]


# ---------------------------------------------------------------------------------------------------- tags


def tag_with_ordinal(ticker: str, filing: edgar.Filing, cal: edgar.FiscalCalendar,
                     filings: Sequence[edgar.Filing]) -> str:
    """The archive tag of a filing (SPEC 3.3): periodic reports by fiscal period, current reports by filing date,
    with -2, -3, ... for the second and later filing of the same form on the same day (in acceptance order)."""
    form = edgar.form_key(filing.form)
    if filing.report_date is not None:
        try:
            if filing.form in edgar.QUARTERLY_FORMS:
                return f"{ticker}-{form}-{cal.quarter_label(filing.report_date)}"
            if filing.form in edgar.ANNUAL_FORMS:
                return f"{ticker}-{form}-{cal.annual_label(filing.report_date)}"
        except ValueError:
            pass
    same_day = sorted((f for f in filings if f.filing_date == filing.filing_date and edgar.form_key(f.form) == form),
                      key=lambda f: (f.acceptance_datetime or "", f.accession))
    ordinal = next((i for i, f in enumerate(same_day, start=1) if f.accession == filing.accession), 1)
    base = f"{ticker}-{form}-{filing.filing_date.isoformat()}"
    return base if ordinal == 1 else f"{base}-{ordinal}"


# ---------------------------------------------------------------------------------------------------- excerpts


_SENTENCE_END_RE = re.compile(r"(?<=[.!?;])\s+(?=[A-Z(\"'“$])")
MAX_EXCERPT_CHARS = 400


def value_forms(value: Any) -> list[str]:
    """The ways a value may be written in a filing: 16698 -> 16,698; 72.229 -> 72.229; -3 -> (3); a date ->
    July 24, 2026 and 2026-07-24; text as is, then the numbers in it ("12,256 against 11,200" -> 12,256, 11,200)."""
    if isinstance(value, bool) or value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [form for item in value for form in value_forms(item)]
    if isinstance(value, (dt.date, dt.datetime)):
        return slicing.date_forms(value)
    if isinstance(value, (int, float)):
        number = float(value)
        forms = []
        if number.is_integer():
            whole = int(abs(number))
            forms += [f"{whole:,}", str(whole)]
        else:
            text = f"{abs(number):,.10f}".rstrip("0").rstrip(".")
            forms += [text, text.replace(",", "")]
        if number < 0:
            forms = [f"({f})" for f in forms] + [f"-{f}" for f in forms] + [f"−{f}" for f in forms]
        return list(dict.fromkeys(f for f in forms if f and f not in ("0",)))
    text = " ".join(str(value).split())
    forms = [text] if len(text) >= 3 else []
    return list(dict.fromkeys(forms + [n for n in slicing.number_forms(text) if n != text]))


def value_patterns(value: Any) -> list[re.Pattern[str]]:
    """value_forms() as patterns: numbers match as whole numbers (16,698 does not match 116,698), text ignores case."""
    patterns = []
    for form in value_forms(value):
        if any(ch.isdigit() for ch in form):
            patterns.append(re.compile(r"(?<![\d.,])" + re.escape(form) + r"(?![\d]|[.,]\d)"))
        else:
            patterns.append(re.compile(re.escape(form), re.I))
    return patterns


_YEAR_RE = re.compile(r"^(19|20)\d{2}$")


def distinctive_patterns(value: Any) -> list[re.Pattern[str]]:
    """value_patterns() without the forms that match almost anywhere in a filing: years, one- and two-digit whole
    numbers, and text shorter than 15 characters (used to cut a long document to windows, pipeline/slicing.py)."""
    kept = []
    for form, pattern in zip(value_forms(value), value_patterns(value)):
        bare = form.strip("()-−")
        if any(ch.isdigit() for ch in form):
            if _YEAR_RE.match(bare) or (bare.isdigit() and len(bare) <= 2):
                continue
        elif len(form) < 15:
            continue
        kept.append(pattern)
    return kept


_STOP_WORDS = frozenset({"with", "from", "that", "this", "than", "into", "over", "under", "each", "year", "years",
                         "total", "fiscal", "period", "against", "current", "prior", "company", "million", "billion",
                         "percent", "which", "their", "were", "have", "been", "also"})


def _keywords(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", text.lower()) if w not in _STOP_WORDS}


_PAGE_LOC_RE = re.compile(r"(?i)^p(?:age)?\s*([A-Z]{0,2}-?)(\d{1,4})$")
_ITEM_LOC_RE = re.compile(r"(?i)^item\s*(\d{1,2}[A-Z]?)$")
_NOTE_LOC_RE = re.compile(r"(?i)^note\s*(\d{1,2})$")


def locator_section(document: str, locator: str, form: str = "10-K") -> str | None:
    """The part of an EDGAR document a source tag's locator points to: a page (#p49, #pK-70: the text between the
    previous page's footer and this page's), an item (#Item7) or a note (#Note10); None when it cannot be found, so
    the caller searches the whole document."""
    loc = (locator or "").strip()
    lines = document.split("\n")
    if m := _PAGE_LOC_RE.match(loc):
        prefix, number = m.group(1).upper(), m.group(2)
        footers = [i for i, line in enumerate(lines) if line.strip().upper() == prefix + number]
        if len(footers) == 1:
            end = footers[0]
            # the page starts after the previous page's footer, the nearest line above that is K-69 for K-70: not after
            # any short number, as a year subheading ("2024") or a table cell ("423") is a line of its own too
            previous = f"{prefix}{int(number) - 1}"
            start = next((i for i in range(end - 1, -1, -1) if lines[i].strip().upper() == previous), -1)
            if end - start >= 2:
                return "\n".join(lines[start + 1:end])
        return None
    if m := _ITEM_LOC_RE.match(loc):
        text, found, _missing = extract_sections(document, form, frozenset({f"item:{m.group(1).upper()}"}))
        return text if found else None
    if m := _NOTE_LOC_RE.match(loc):
        number = int(m.group(1))
        heads = _note_headings(lines)
        starts = [i for i, n in heads.items() if n == number]
        previous = [i for i, n in heads.items() if n == number - 1]
        spans = []
        for start in starts:
            end = next((i for i, n in heads.items() if i > start and n == number + 1), None)
            # notes come in order, so a span that holds the previous note's heading starts at a cross-reference
            # ("Note 10" in the table of accounting policies of American Express's Note 1), not at the note
            if any(start < i < (end or len(lines)) for i in previous):
                continue
            spans.append((end is not None, (end or len(lines)) - start, start, end or len(lines)))
        if spans:
            # the note's body: first a span the next note's heading closes (the numbered list of exhibits after the
            # notes runs on to the end of the document), then the longest (Berkshire's 10-Q repeats a note's heading
            # atop each page the note continues on)
            *_, start, end = max(spans)
            return "\n".join(lines[start:end])
    return None


# A note's heading: "Note 10. Debt", "NOTE 10 — DEBT" or "10. Debt", but not "10 Debt", which is how footnotes
# ("1 Includes ...") and S&P Global's index of notes are written; or "NOTE 10" or "(10)" alone on its line, the title
# on the next (American Express; Berkshire Hathaway's 10-K, whose table footnotes "(1)" are followed by a sentence).
_NOTE_HEADING_RE = re.compile(r"^\s*(?:(?i:note)\s+(\d{1,2})\b[\s.:—–-]*|(\d{1,2})(?:[.:]|\s+[—–-])\s*)[A-Z][A-Za-z]")
_NOTE_ALONE_RE = re.compile(r"(?i)^\s*(?:note\s+(\d{1,2})|\((\d{1,2})\))\s*$")
_NOTE_TITLE_RE = re.compile(r"^\s*[A-Z][A-Za-z]")
_PAGE_NUMBER_RE = re.compile(r"^\s*(?:[A-Z]{1,2}-)?\d{1,3}\s*$")
_ENDS_IN_PAGE_NUMBER_RE = re.compile(r"[\s.](?:[A-Z]{1,2}-)?\d{1,3}\s*$")


def _note_headings(lines: Sequence[str]) -> dict[int, int]:
    """Line index -> note number of a document's note headings, without the rows of an index of notes: a row that
    ends in its page number ("Note 3 – Reserves for Credit Losses ... 85"), and rows that follow one another with
    nothing but page numbers between ("Note 3 – Reserves for Credit Losses", "112", "Note 4 – Investment Securities"
    in American Express's 10-K, whose notes are headed "NOTE 3" on a line of their own)."""
    heads: dict[int, tuple[int, int]] = {}  # line index -> (note number, the heading's last line)
    for i, line in enumerate(lines):
        if len(line) > 150:
            continue
        if m := _NOTE_HEADING_RE.match(line):
            if not _ENDS_IN_PAGE_NUMBER_RE.search(line):
                heads[i] = (int(m.group(1) or m.group(2)), i)
        elif (m := _NOTE_ALONE_RE.match(line)) and i + 1 < len(lines) and len(lines[i + 1]) <= 150 \
                and _NOTE_TITLE_RE.match(lines[i + 1]) and not (m.group(2) and lines[i + 1].rstrip().endswith(".")):
            heads[i] = (int(m.group(1) or m.group(2)), i + 1)
    rows = set()
    for i, (number, last) in heads.items():
        after = next((j for j in range(last + 1, len(lines))
                      if lines[j].strip() and not _PAGE_NUMBER_RE.match(lines[j])), None)
        if after in heads and heads[after][0] == number + 1:
            rows |= {i, after}
    return {i: number for i, (number, _) in heads.items() if i not in rows}


def cut_excerpt(document: str, value: Any, words: str = "") -> str | None:
    """The sentence (or table row) of the document that contains the value, at most MAX_EXCERPT_CHARS long; None when
    the value is not found. Numbers are matched as whole numbers (16,698 does not match 116,698). With `words` (what
    the fact is about), a sentence that also shares one of those words is preferred, and a bare short number found
    only in sentences that share none is left out rather than attached to an unrelated sentence. Of the sentences that
    contain the value, the one sharing the most of those words wins (a number often recurs in other rows: the 04A audit
    of SPGI found excerpts cut from the wrong row by a shared word such as "revenue")."""
    lines = document.splitlines()
    wanted = _keywords(words)
    fallback = None
    best: tuple[int, str] | None = None  # (words shared with the fact, excerpt): the most shared wins, then the first
    for form, pattern in zip(value_forms(value), value_patterns(value)):
        for index, line in enumerate(lines):
            match = pattern.search(line)
            if not match:
                continue
            sentence = next((s for s in _sentences(line) if pattern.search(s)), line)
            label = _row_label(lines, index) if _is_cell(sentence) else None
            text = f"{label} … {sentence.strip()}" if label else sentence
            if not wanted:
                return _clip(text, pattern)
            shared = len(_keywords(text) & wanted)
            if shared and (best is None or shared > best[0]):
                best = (shared, _clip(text, pattern))
            weak = len(form.strip("()-−")) <= 4 and "," not in form
            if fallback is None and not weak:
                fallback = _clip(text, pattern)
    return best[1] if best is not None else fallback


_LETTERS_RE = re.compile(r"[A-Za-z]{3,}")


def _is_cell(text: str) -> bool:
    """A table cell on a line of its own (EDGAR tables become one cell per line): short, with no word in it."""
    return len(text.strip()) <= 30 and not _LETTERS_RE.search(text)


def _row_label(lines: Sequence[str], index: int, reach: int = 12) -> str | None:
    """The nearest line above a cell that carries words (the row's label), within `reach` lines; a short one (a
    sub-label such as "FX-adjusted") is joined to the label above it."""
    for back in range(index - 1, max(-1, index - reach - 1), -1):
        text = lines[back].strip()
        if _LETTERS_RE.search(text) and len(text) <= 200:
            above = lines[back - 1].strip() if back > 0 else ""
            if len(text) <= 15 and _LETTERS_RE.search(above) and len(above) <= 200:
                return f"{above} / {text}"
            return text
    return None


def _sentences(line: str) -> list[str]:
    return [s for s in _SENTENCE_END_RE.split(line) if s.strip()]


def _clip(text: str, pattern: re.Pattern[str]) -> str:
    text = " ".join(text.split())
    if len(text) <= MAX_EXCERPT_CHARS:
        return text
    match = pattern.search(text)
    centre = match.start() if match else 0
    start = max(0, min(centre - MAX_EXCERPT_CHARS // 2, len(text) - MAX_EXCERPT_CHARS))
    return ("…" if start else "") + text[start:start + MAX_EXCERPT_CHARS].strip() + \
        ("…" if start + MAX_EXCERPT_CHARS < len(text) else "")


_LEGAL_WORDS = frozenset({"inc", "inc.", "corporation", "corp", "corp.", "company", "co", "co.", "ltd", "ltd.",
                          "limited", "holdings", "group", "plc", "llc"})


def owner_names(company: str, name: str | None, short: str | None) -> list[str]:
    """Words that name the company itself in `where`: its ticker and the words of its name and short name
    (parentheticals and legal forms dropped)."""
    words = [company]
    for text in (short, name):
        plain = re.sub(r"\([^)]*\)", " ", str(text or ""))
        words += [w for w in re.split(r"[\s,]+", plain) if w and w[:1].isupper() and w.casefold() not in _LEGAL_WORDS]
    return list(dict.fromkeys(words))


def describe_selection(selection: Selection) -> Mapping[str, Any]:
    f = selection.filing
    return {"form": f.form, "accession": f.accession, "filed": f.filing_date.isoformat(), "kind": selection.kind}
