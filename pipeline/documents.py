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

from . import edgar

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
SECTION_NOTE = "where `where` names a section of a filing (MD&A, risk factors, legal proceedings, a note), the whole " \
               "filing is supplied"

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
            requests.append(DocRequest(kind, clause=clause))
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
            kind: str) -> None:
        old = chosen.get(filing.accession)
        if old is not None:
            only = frozenset() if (not old.only_exhibits or not only) else old.only_exhibits | only
            primary, exhibits = primary or old.primary, exhibits or old.exhibits
            kind = old.kind
        chosen[filing.accession] = Selection(filing, primary, exhibits, only, kind)

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
                    add(f, primary=True, exhibits=False, kind=request.kind)
                    found += 1
        elif request.kind in (ANNUAL_REPORT, PROXY):
            forms = FORMS[request.kind]
            matching = [f for f in known if f.form in forms]
            in_window = [f for f in matching if (_periodic_label(f, cal) in periods if request.kind == ANNUAL_REPORT
                                                 else _in(f.filing_date, start, as_of))]
            latest = [max(matching, key=lambda f: (f.filing_date, f.accession))] if matching else []
            for f in {x.accession: x for x in [*in_window, *latest]}.values():
                add(f, primary=True, exhibits=False, kind=request.kind)
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
    """The ways a value may be written in a filing: 16698 -> 16,698; 72.229 -> 72.229; -3 -> (3); text as is."""
    if isinstance(value, bool) or value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [form for item in value for form in value_forms(item)]
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
    return [text] if len(text) >= 3 else []


def cut_excerpt(document: str, value: Any) -> str | None:
    """The sentence (or table row) of the document that contains the value, at most MAX_EXCERPT_CHARS long; None when
    the value is not found. Numbers are matched as whole numbers (16,698 does not match 116,698)."""
    for form in value_forms(value):
        if any(ch.isdigit() for ch in form):
            pattern = re.compile(r"(?<![\d.,])" + re.escape(form) + r"(?![\d]|[.,]\d)")
        else:
            pattern = re.compile(re.escape(form), re.I)
        lines = document.splitlines()
        for index, line in enumerate(lines):
            match = pattern.search(line)
            if not match:
                continue
            sentence = next((s for s in _sentences(line) if pattern.search(s)), line)
            label = _row_label(lines, index) if _is_cell(sentence) else None
            return _clip(f"{label} … {sentence.strip()}" if label else sentence, pattern)
    return None


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
