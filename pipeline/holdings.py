"""A company's listed equity portfolio marked at the price-reference date (private valuation; decision 0029).

Berkshire Hathaway values a large part of itself through the stocks it holds. Its balance sheet in the latest 10-Q
carries them at the quarter-end market value, so a valuation dated later would compare an old portfolio value with
today's share price. This module gives the valuation the portfolio at the price-reference date instead:

- the positions: the latest Form 13F-HR on or before the date (US-listed holdings and their share counts at the
  quarter end; the 13F's values are the quarter-end market values, in dollars since 2023);
- each position's ticker: OpenFIGI's public mapping of the CUSIP (or, for a non-US issuer, the CINS) to its US
  listing; the request carries only the codes;
- each position's closes on or before the 13F's report date and on or before the date, from the same Nasdaq.com
  history the other prices come from (prices.py). The 13F value is carried forward by the ratio of the two closes.
  The history is split-adjusted, so a split after the report date does not change that ratio; the 13F's share
  count, from before the split, would be off by the split ratio.

What it cannot see is said with the figures: trades after the 13F's quarter end are not disclosed until the next
13F; holdings outside the 13F (non-US listings such as the Japanese trading houses) stay at the balance-sheet value;
an equity-method investee is in the 13F but carried at equity on the balance sheet.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import time
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from . import prices

FIGI_URL = "https://api.openfigi.com/v3/mapping"
FIGI_BATCH = 10  # OpenFIGI without an API key: 10 jobs per request, 25 requests a minute
FIGI_PAUSE = 2.6
FORM_13F = "13F-HR"


class HoldingsError(RuntimeError):
    """The 13F or its information table could not be read."""


@dataclasses.dataclass(frozen=True)
class Position:
    issuer: str
    title: str
    cusip: str
    shares: int
    value: int  # the 13F's value at its report date, USD

    def key(self) -> str:
        return f"{self.cusip} {self.title}"


def _strip(tag: str) -> str:
    return tag.split("}", 1)[-1]


def parse_information_table(xml: bytes) -> list[Position]:
    """The 13F information table's positions, summed per security across the filer's managers; options (rows with
    putCall) and principal amounts (debt) are left out."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise HoldingsError(f"the 13F information table is not valid XML ({exc})") from None
    sums: dict[tuple[str, str], list[Any]] = {}
    for table in root.iter():
        if _strip(table.tag) != "infoTable":
            continue
        row = {_strip(c.tag): (c.text or "").strip() for c in table.iter() if c is not table and (c.text or "").strip()}
        if row.get("putCall") or row.get("sshPrnamtType", "SH") != "SH":
            continue
        try:
            shares, value = int(row["sshPrnamt"]), int(row["value"])
        except (KeyError, ValueError):
            continue
        key = (row.get("cusip", "").upper(), row.get("titleOfClass", ""))
        entry = sums.setdefault(key, [row.get("nameOfIssuer", ""), 0, 0])
        entry[1] += shares
        entry[2] += value
    return sorted((Position(name, title, cusip, shares, value) for (cusip, title), (name, shares, value) in sums.items()),
                  key=lambda p: -p.value)


def _post_json(url: str, body: Any) -> Any:
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json", "User-Agent": prices.USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 (a fixed https host)
        return json.loads(response.read().decode("utf-8"))


def figi_tickers(codes: Sequence[str], *, cache: Path | None = None, post: Callable[[str, Any], Any] = _post_json,
                 pause: float = FIGI_PAUSE) -> tuple[dict[str, str], dict[str, str]]:
    """(CUSIP or CINS -> the ticker of its US listing, as Nasdaq.com spells it ("LEN/B" -> "LEN.B"); code -> why its
    lookup failed). A code OpenFIGI answered without a US listing is in neither. Tickers are cached in `cache` (they
    do not change); failed lookups are not, so the next run asks again."""
    known: dict[str, str] = json.loads(cache.read_text(encoding="utf-8")) if cache and cache.is_file() else {}
    failed: dict[str, str] = {}
    todo = [c for c in dict.fromkeys(codes) if c and c not in known]
    for id_type in ("ID_CUSIP", "ID_CINS"):  # a non-US issuer's code is a CINS (it starts with a letter)
        batch_codes = [c for c in todo if c not in known]
        for start in range(0, len(batch_codes), FIGI_BATCH):
            chunk = batch_codes[start:start + FIGI_BATCH]
            try:
                answers = post(FIGI_URL, [{"idType": id_type, "idValue": c, "exchCode": "US"} for c in chunk])
                if not isinstance(answers, list):
                    raise ValueError("not one answer per code")
            except Exception as exc:  # network or format: reported as such, not as a code without a US listing
                why = f"the ticker could not be read from OpenFIGI ({type(exc).__name__})"
                failed.update(dict.fromkeys(chunk, why))
                answers = []
            for code, answer in zip(chunk, answers):
                rows = (answer or {}).get("data") or []
                ticker = next((r.get("ticker") for r in rows if r.get("ticker")), None)
                if ticker:
                    known[code] = str(ticker).replace("/", ".")
            if pause and start + FIGI_BATCH < len(batch_codes):
                time.sleep(pause)
    if cache is not None:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(known, indent=1, sort_keys=True), encoding="utf-8")
    return {c: known[c] for c in codes if c in known}, {c: failed[c] for c in codes if c in failed and c not in known}


@dataclasses.dataclass(frozen=True)
class Mark:
    position: Position
    ticker: str | None
    close: prices.Close | None  # on or before the as-of date
    problem: str | None = None
    report_close: prices.Close | None = None  # on or before the 13F's report date, from the same history

    @property
    def value_marked(self) -> float | None:
        if self.close is None or self.report_close is None:
            return None
        # the 13F value times the price change; not shares x close, as the 13F's share count predates any later split
        return round(self.position.value * self.close.close / self.report_close.close, 0)


def mark_positions(positions: Sequence[Position], tickers: Mapping[str, str], report_date: dt.date, as_of: dt.date,
                   history: Callable[[str], Sequence[prices.Close]],
                   failed: Mapping[str, str] | None = None) -> list[Mark]:
    """Each position with its closes on or before the 13F's report date and on or before `as_of`; a position without
    a ticker or a price says why (`failed`: why a code's ticker could not be looked up, from figi_tickers)."""
    marks = []
    for p in positions:
        ticker = tickers.get(p.cusip)
        if ticker is None:
            marks.append(Mark(p, None, None, (failed or {}).get(p.cusip, "no US ticker found for the CUSIP")))
            continue
        try:
            closes = history(ticker)
            report_close = prices.close_on_or_before(closes, report_date)
            close = prices.close_on_or_before(closes, as_of)
        except prices.PriceError as exc:
            marks.append(Mark(p, ticker, None, str(exc)))
            continue
        marks.append(Mark(p, ticker, close, report_close=report_close))
    return marks


def summary(filing_report_date: dt.date, as_of: dt.date, marks: Sequence[Mark]) -> dict[str, Any]:
    """The input the valuation reads: per position the 13F value at the report date, its closes at the report date
    and at `as_of`, and its value at `as_of`; and the totals over the positions that could be marked."""
    rows, covered_13f, marked = [], 0, 0.0
    total_13f = sum(m.position.value for m in marks)
    for m in marks:
        row = {"issuer": m.position.issuer, "class": m.position.title, "cusip": m.position.cusip,
               "ticker": m.ticker, "shares": m.position.shares,
               f"value_{filing_report_date.isoformat()}": m.position.value}
        if m.close is not None and m.report_close is not None:
            row[f"close_{filing_report_date.isoformat()}"] = m.report_close.close
            row["report_close_date"] = m.report_close.date.isoformat()
            row[f"close_{as_of.isoformat()}"] = m.close.close
            row["close_date"] = m.close.date.isoformat()
            row[f"value_{as_of.isoformat()}"] = int(m.value_marked)
            row["price_tag"] = f"{m.close.tag}#{m.close.date.isoformat()}" if m.close.tag else None
            covered_13f += m.position.value
            marked += m.value_marked or 0
        else:
            row["not_marked"] = m.problem
        rows.append(row)
    return {
        "positions_as_of": filing_report_date.isoformat(), "marked_as_of": as_of.isoformat(),
        "total_13f_value": total_13f, "marked_positions_13f_value": covered_13f, "marked_positions_value": int(marked),
        "change_on_marked_positions": int(marked - covered_13f),
        "coverage": round(covered_13f / total_13f, 4) if total_13f else None,
        "positions": rows,
    }
