"""A new archive from the SEC filings: what 01A reads (docs/decisions/0028). No model, no network: registry.py fetches.

The first six archives were built from the owner's own research reports. From 2026-09-28 a company the owner adds is
built by the pipeline from primary sources (decisions/0027, 0028), and this module decides what the dossier's author
(01A) is given:

- build_selection(): the filings, in priority order: first the present (the latest annual report in full, the latest
  earnings release and quarterly report, the latest proxy statement, and the current reports of the last twelve
  months on material agreements, acquisitions and changes of officers, items 1.01, 2.01 and 5.02); then the history
  (the business section and MD&A of the annual reports five and ten fiscal years back); then what the present largely
  repeats (the other releases of the last year, earlier quarterly reports). The caller adds them in this order while
  the estimated size stays within SOURCES_TOKENS, and lists what did not fit, so the dossier can name it in its
  unknowns register instead of filling the gap from memory (00 §E3).
- xbrl_summary(): the ten-year financial summary (01A's `xbrl_facts`) from EDGAR's companyfacts: for each line item,
  the value of each fiscal year as the latest annual report states it (so restatements win), with the accession it
  comes from and the source tag of that filing.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from . import documents, edgar

SOURCES_TOKENS = 450_000  # 01A's documents, estimated; leaves room for the system prompt, xbrl_facts and the output
HISTORY_YEARS = (5, 10)  # annual reports this many fiscal years before the latest one: business section and MD&A
CURRENT_REPORT_ITEMS = frozenset({"1.01", "2.01", "5.02"})
MAX_CURRENT_REPORTS = 12
SUMMARY_YEARS = 10

# (key, label, unit, kind, concepts in order of preference); kind "duration" (a fiscal year's flow) or "instant"
# (the balance at the fiscal year end).
SUMMARY_ITEMS: tuple[tuple[str, str, str, str, tuple[str, ...]], ...] = (
    ("revenue", "Revenue", "USD", "duration",
     ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet",
      "RevenueFromContractWithCustomerIncludingAssessedTax")),
    ("operating_income", "Operating income", "USD", "duration", ("OperatingIncomeLoss",)),
    ("net_income", "Net income", "USD", "duration", ("NetIncomeLoss", "ProfitLoss")),
    ("eps_diluted", "Diluted earnings per share", "USD/shares", "duration", ("EarningsPerShareDiluted",)),
    ("diluted_shares", "Weighted diluted shares", "shares", "duration",
     ("WeightedAverageNumberOfDilutedSharesOutstanding",)),
    ("operating_cash_flow", "Cash from operations", "USD", "duration",
     ("NetCashProvidedByUsedInOperatingActivities",)),
    ("capex", "Capital expenditure", "USD", "duration",
     ("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets")),
    ("depreciation_amortization", "Depreciation and amortization", "USD", "duration",
     ("DepreciationDepletionAndAmortization", "DepreciationAndAmortization", "DepreciationAmortizationAndAccretionNet")),
    ("share_based_compensation", "Share-based compensation", "USD", "duration",
     ("ShareBasedCompensation", "AllocatedShareBasedCompensationExpense")),
    ("buybacks", "Share repurchases (cash)", "USD", "duration", ("PaymentsForRepurchaseOfCommonStock",)),
    ("dividends", "Dividends paid", "USD", "duration", ("PaymentsOfDividendsCommonStock", "PaymentsOfDividends")),
    ("interest_expense", "Interest expense", "USD", "duration",
     ("InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt")),
    ("income_tax", "Income tax expense", "USD", "duration", ("IncomeTaxExpenseBenefit",)),
    ("cash", "Cash and equivalents", "USD", "instant",
     ("CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents")),
    ("total_assets", "Total assets", "USD", "instant", ("Assets",)),
    ("long_term_debt", "Long-term debt, noncurrent", "USD", "instant", ("LongTermDebtNoncurrent", "LongTermDebt")),
    ("debt_current", "Debt due within a year", "USD", "instant", ("LongTermDebtCurrent", "DebtCurrent")),
    ("equity", "Shareholders' equity", "USD", "instant",
     ("StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest")),
)
ANNUAL_FORMS = frozenset({"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"})


@dataclasses.dataclass(frozen=True)
class Pick:
    """One filing 01A is to be given, why, and how much of it."""

    selection: documents.Selection
    reason: str


def build_selection(filings: Sequence[edgar.Filing], events: Sequence[edgar.EarningsEvent], *, as_of: dt.date,
                    cal: edgar.FiscalCalendar, foreign: bool) -> list[Pick]:
    """The filings for a new dossier, in priority order (see the module docstring)."""
    known = [f for f in filings if f.filing_date <= as_of]
    annual = sorted((f for f in known if f.form in ANNUAL_FORMS and not f.form.endswith("/A")),
                    key=lambda f: (f.report_date or f.filing_date, f.filing_date))
    picks: list[Pick] = []
    seen: set[str] = set()

    def add(filing: edgar.Filing, reason: str, *, primary: bool = True, exhibits: bool = False,
            sections: frozenset[str] | None = None, kind: str = "") -> None:
        if filing.accession in seen:
            return
        seen.add(filing.accession)
        picks.append(Pick(documents.Selection(filing, primary, exhibits, frozenset(), kind, sections), reason))

    latest = annual[-1] if annual else None
    released = sorted((e for e in events if e.filing.filing_date <= as_of),
                      key=lambda e: (e.period_end, e.filing.filing_date))[-4:]
    after = latest.filing_date if latest is not None else dt.date.min
    quarterly = sorted((f for f in known if f.form == "10-Q" and f.filing_date > after), key=lambda f: f.filing_date,
                       reverse=True)
    proxies = [f for f in known if f.form == "DEF 14A"]
    year_ago = as_of - dt.timedelta(days=365)
    current = sorted((f for f in known if f.form == ("6-K" if foreign else "8-K") and f.filing_date > year_ago
                      and set(f.items) & CURRENT_REPORT_ITEMS), key=lambda f: f.filing_date, reverse=True)

    def release(event: edgar.EarningsEvent) -> None:
        add(event.filing, f"the earnings release for {event.period}", primary=foreign, exhibits=True,
            kind=documents.EARNINGS_RELEASE)

    # 1. the present: the annual report, the latest release and quarterly report, the proxy, officer changes
    if latest is not None:
        add(latest, "the latest annual report, in full", kind=documents.ANNUAL_REPORT)
    if released:
        release(released[-1])
    for filing in quarterly[:1]:
        add(filing, "the latest quarterly report", kind=documents.QUARTERLY_REPORT)
    if proxies:
        add(max(proxies, key=lambda f: f.filing_date), "the latest proxy statement (pay, ownership, the board)",
            kind=documents.PROXY)
    for filing in current[:MAX_CURRENT_REPORTS]:
        items = ", ".join(sorted(set(filing.items) & CURRENT_REPORT_ITEMS))
        add(filing, f"a current report of the last twelve months (items {items})", kind=documents.CURRENT_REPORT)
    # 2. the history the dossier's ten-year parts need (the numbers are in xbrl_facts)
    if latest is not None and latest.report_date is not None:
        for years in HISTORY_YEARS:
            target = latest.report_date.year - years
            older = [f for f in annual if f.report_date is not None and f.report_date.year == target]
            if older:
                add(older[-1], f"the annual report {years} fiscal years back: business section and MD&A",
                    sections=frozenset({"business", "mdna"}), kind=documents.ANNUAL_REPORT)
    # 3. what the present already largely repeats: the other releases of the last year, earlier quarterly reports
    for event in reversed(released[:-1]):
        release(event)
    for filing in quarterly[1:]:
        add(filing, "an earlier quarterly report filed after the latest annual report",
            kind=documents.QUARTERLY_REPORT)
    return picks


VALUATION_SOURCES_TOKENS = 380_000  # 01C's documents: the valuation also carries the dossier and xbrl_facts
VALUATION_HISTORY_YEARS = 9  # MD&A of the annual reports this many years back: each explains its year's changes


def valuation_selection(filings: Sequence[edgar.Filing], *, as_of: dt.date) -> list[Pick]:
    """The filings 01C checks its readings against (04C: a cause given for a past decline must be established from a
    primary filing): the latest annual report and the latest quarterly report in full, then the MD&A of each earlier
    annual report, newest first, so that every year of the ten-year record has the filing that explains it."""
    known = [f for f in filings if f.filing_date <= as_of]
    annual = sorted((f for f in known if f.form in ANNUAL_FORMS and not f.form.endswith("/A")),
                    key=lambda f: (f.report_date or f.filing_date, f.filing_date))
    if not annual:
        return []
    latest = annual[-1]
    picks = [Pick(documents.Selection(latest, True, False, frozenset(), documents.ANNUAL_REPORT, None),
                  "the latest annual report, in full")]
    quarterly = sorted((f for f in known if f.form == "10-Q" and f.filing_date > latest.filing_date),
                       key=lambda f: f.filing_date)
    if quarterly:
        picks.append(Pick(documents.Selection(quarterly[-1], True, False, frozenset(), documents.QUARTERLY_REPORT, None),
                          "the latest quarterly report, in full"))
    by_year = {f.report_date.year: f for f in annual if f.report_date is not None}
    year = (latest.report_date or latest.filing_date).year
    for back in range(1, VALUATION_HISTORY_YEARS + 1):
        older = by_year.get(year - back)
        if older is not None:
            picks.append(Pick(documents.Selection(older, True, False, frozenset(), documents.ANNUAL_REPORT,
                                                  frozenset({"mdna"})),
                              f"the annual report for fiscal {year - back}: MD&A (the causes of that year's changes)"))
    return picks


def within_budget(picks: Sequence[Any], cost: Callable[[Any], int], budget: int = SOURCES_TOKENS
                  ) -> tuple[list[Any], list[Any]]:
    """The picks that fit in `budget`, taken in order (a later, smaller one may still fit), and those left out."""
    kept, left, used = [], [], 0
    for pick in picks:
        size = cost(pick)
        if used + size <= budget:
            kept.append(pick)
            used += size
        else:
            left.append(pick)
    return kept, left


def _duration_days(row: Mapping[str, Any]) -> int | None:
    try:
        return (dt.date.fromisoformat(str(row["end"])) - dt.date.fromisoformat(str(row["start"]))).days
    except (KeyError, ValueError):
        return None


def _annual_rows(facts: Mapping[str, Any], concept: str, unit: str, kind: str) -> list[Mapping[str, Any]]:
    for taxonomy in ("us-gaap", "ifrs-full"):
        units = ((facts.get(taxonomy) or {}).get(concept) or {}).get("units") or {}
        rows = units.get(unit) or []
        out = []
        for row in rows:
            if str(row.get("form", "")) not in ANNUAL_FORMS or row.get("fp") not in ("FY", None):
                continue
            if kind == "duration":
                days = _duration_days(row)
                if days is None or not 330 <= days <= 400:
                    continue
            out.append(row)
        if out:
            return out
    return []


def xbrl_summary(companyfacts: Mapping[str, Any], *, cal: edgar.FiscalCalendar,
                 tags: Callable[[str], str | None], years: int = SUMMARY_YEARS) -> dict[str, Any]:
    """The ten-year summary: {fiscal_years, items: {key: {label, unit, concept, values: {FYyyyy: {value, end,
    accession, source}}}}, missing}. `tags` maps an accession to the filing's source tag (None: not known)."""
    facts = companyfacts.get("facts") or {}
    items: dict[str, Any] = {}
    missing: list[str] = []
    all_years: set[str] = set()
    for key, label, unit, kind, concepts in SUMMARY_ITEMS:
        found: list[tuple[str, dict[str, Mapping[str, Any]]]] = []
        for concept in concepts:
            by_end: dict[str, Mapping[str, Any]] = {}
            for row in _annual_rows(facts, concept, unit, kind):
                end = str(row.get("end"))
                best = by_end.get(end)
                if best is None or str(row.get("filed", "")) > str(best.get("filed", "")):
                    by_end[end] = row
            if by_end:
                found.append((concept, by_end))
        if not found:
            missing.append(key)
            continue
        # the concept with the most recent fiscal year, then the most years; the others are shown when they differ
        concept, by_end = max(found, key=lambda c: (max(c[1]), len(c[1]), -concepts.index(c[0])))
        latest_end = max(by_end)
        alternatives = {}
        for other, rows in found:
            if other == concept or latest_end not in rows:
                continue
            value, mine = rows[latest_end].get("val"), by_end[latest_end].get("val")
            if isinstance(value, (int, float)) and isinstance(mine, (int, float)) and mine and \
                    abs(value - mine) > 0.02 * abs(mine):
                alternatives[other] = value
        values: dict[str, Any] = {}
        for end in sorted(by_end)[-years:]:
            row = by_end[end]
            try:
                fiscal_year, _ = cal.period_of(dt.date.fromisoformat(end))
            except ValueError:
                continue
            label_year = f"FY{fiscal_year}"
            accession = str(row.get("accn") or "")
            values[label_year] = {"value": row.get("val"), "end": end, "accession": accession,
                                  "source": tags(accession) or accession}
            all_years.add(label_year)
        items[key] = {"label": label, "unit": unit, "concept": concept, "values": values}
        if alternatives:
            items[key]["other_concepts_latest_year"] = alternatives
    return {"fiscal_years": sorted(all_years)[-years:], "items": items, "missing": missing}


def render_summary(summary: Mapping[str, Any], company: str) -> str:
    """xbrl_summary() as the YAML document 01A reads, with a note on how to read it."""
    import yaml

    note = (f"Ten-year financial summary of {company} compiled by the pipeline from EDGAR's XBRL companyfacts. Each "
            "value is the one the latest annual report states for that fiscal year (a restatement replaces the "
            "original), with the accession it comes from and that filing's source tag; cite the tag. A company can "
            "change the concept it reports a line under or the line's basis between years: the concept is named per "
            "line, and a change of basis must be checked in the filings before a series is compared. Where another "
            "concept gives a different value for the latest year, it is listed under other_concepts_latest_year: "
            "check which one the filing's statements use. Values are as the company tagged them (some tag share "
            "counts in millions). Lines the company does not tag are listed under missing.")
    return yaml.safe_dump({"note": note, **summary}, allow_unicode=True, sort_keys=False, width=110)


def tag_lookup(filings: Iterable[edgar.Filing], tag_of: Callable[[edgar.Filing], str]) -> Callable[[str], str | None]:
    by_accession = {f.accession: f for f in filings}

    def lookup(accession: str) -> str | None:
        filing = by_accession.get(accession)
        return tag_of(filing) if filing is not None else None

    return lookup
