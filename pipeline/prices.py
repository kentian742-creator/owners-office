"""Closing prices for the private valuation: where they come from, with their date (00 §H2; STATUS T21).

The owner chose the exchanges' own websites as the source (2026-09-28). Nasdaq's site publishes ten years of daily
history for NYSE and Nasdaq listings and for ETFs alike. What its "Close/Last" column is, and so what every price the
pipeline supplies is:

- the consolidated closing price of the day (for an NYSE listing it can differ by a few cents from NYSE's own
  closing-auction price);
- adjusted for stock splits (a year-end close before a split is divided by the split ratio), not for dividends.

Each price carries its date, the URL it was read from and that note. Prices go only into private inputs and files:
public files carry no prices (§H4). This module never sends anything personal: no e-mail address, no token, a generic
User-Agent (the SEC contact address in the workspace .env is for EDGAR only).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import time
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

HISTORY_URL = ("https://api.nasdaq.com/api/quote/{symbol}/historical?assetclass={asset_class}&fromdate={start}"
               "&todate={end}&limit=9999")
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
SOURCE_NOTE = ("Nasdaq.com historical quotes, Close/Last: the consolidated closing price, adjusted for stock splits, "
               "not for dividends")
HISTORY_YEARS = 10
ETFS = frozenset({"VOO", "SPY", "IVV", "VTI"})


class PriceError(RuntimeError):
    """No price could be read (network, format, or no trading day in range)."""


def price_tag(symbol: str, as_of: dt.date) -> str:
    """The source tag of one symbol's price history read on `as_of` (a day's close is cited as <tag>#YYYY-MM-DD).
    A share class drops its suffix: BRK.B -> BRK-PRICES-<date>."""
    return f"{symbol.upper().split('.')[0].split('/')[0]}-PRICES-{as_of.isoformat()}"


@dataclasses.dataclass(frozen=True)
class Close:
    symbol: str
    date: dt.date
    close: float
    source: str
    note: str = SOURCE_NOTE
    tag: str | None = None  # the history's source tag (price_tag); None for a close built by hand

    def to_dict(self) -> dict[str, Any]:
        out = {"symbol": self.symbol, "date": self.date.isoformat(), "close": self.close, "source": self.source,
               "note": self.note}
        if self.tag:
            out["tag"] = f"{self.tag}#{self.date.isoformat()}"
        return out


def source_entry(close: Close) -> dict[str, Any]:
    """The sources.yml entry (private: prices never go public, §H4) of the history a close comes from."""
    return {"tag": close.tag, "kind": "web",
            "title": f"{close.symbol} historical daily closing prices, ten years to {close.tag.rsplit('-PRICES-', 1)[-1]}, "
                     "Nasdaq.com",
            "date": close.tag.rsplit("-PRICES-", 1)[-1], "url": close.source, "primary": False,
            "note": f"{SOURCE_NOTE}. The locator #YYYY-MM-DD names a trading day. Registered by the pipeline."}


def _fetch_json(url: str) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 (a fixed https host)
        return json.loads(response.read().decode("utf-8"))


def _number(text: Any) -> float:
    return float(str(text).replace("$", "").replace(",", "").strip())


def parse_history(data: Any, symbol: str, url: str, tag: str | None = None) -> list[Close]:
    rows = ((((data or {}).get("data") or {}).get("tradesTable") or {}).get("rows")) or []
    out = []
    for row in rows:
        try:
            day = dt.datetime.strptime(str(row["date"]), "%m/%d/%Y").date()
            out.append(Close(symbol, day, _number(row["close"]), url, tag=tag))
        except (KeyError, ValueError):
            continue
    return sorted(out, key=lambda c: c.date)


def history(symbol: str, as_of: dt.date, *, cache_dir: Path | None = None,
            fetch: Callable[[str], Any] = _fetch_json) -> list[Close]:
    """Ten years of daily closes up to `as_of`, oldest first; cached per symbol and as-of date under `cache_dir`."""
    symbol = symbol.upper()
    start = as_of.replace(year=as_of.year - HISTORY_YEARS)
    url = HISTORY_URL.format(symbol=symbol, asset_class="etf" if symbol in ETFS else "stocks",
                             start=start.isoformat(), end=as_of.isoformat())
    cache = cache_dir / f"{symbol.replace('/', '-')}-{as_of.isoformat()}.json" if cache_dir else None
    if cache is not None and cache.is_file():
        data = json.loads(cache.read_text(encoding="utf-8"))
    else:
        try:
            data = fetch(url)
        except Exception as exc:  # network or format: the caller reports what is missing
            raise PriceError(f"{symbol}: prices could not be read from Nasdaq.com ({type(exc).__name__})") from None
        if cache is not None:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(data), encoding="utf-8")
        time.sleep(0.5)  # one request at a time, politely
    closes = parse_history(data, symbol, url, price_tag(symbol, as_of))
    if not closes:
        raise PriceError(f"{symbol}: Nasdaq.com returned no closing prices up to {as_of}")
    return closes


def close_on_or_before(closes: Sequence[Close], day: dt.date) -> Close:
    """The close of `day`, or of the last trading day before it."""
    earlier = [c for c in closes if c.date <= day]
    if not earlier:
        raise PriceError(f"no close on or before {day}")
    return earlier[-1]


def year_end_closes(closes: Sequence[Close], year_end: str = "12-31", years: int = 5,
                    as_of: dt.date | None = None) -> list[Close]:
    """The close on or before each of the last `years` fiscal year ends (MM-DD) before `as_of`, newest first."""
    as_of = as_of or closes[-1].date
    month, day = (int(x) for x in year_end.split("-"))
    out = []
    year = as_of.year
    while len(out) < years and year > as_of.year - years - 2:
        try:
            end = dt.date(year, month, day)
        except ValueError:  # 02-29 in a common year
            end = dt.date(year, month, 28)
        if end < as_of:
            try:
                out.append(close_on_or_before(closes, end))
            except PriceError:
                break
        year -= 1
    return out


# ---------------------------------------------------------------------------------------------------- Treasury

TREASURY_URL = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/"
                "{year}/all?type=daily_treasury_yield_curve&field_tdr_date_value={year}&page&_format=csv")
TREASURY_NOTE = "U.S. Treasury daily par yield curve rate, 10-year constant maturity (home.treasury.gov)"


@dataclasses.dataclass(frozen=True)
class Yield:
    date: dt.date
    percent: float
    source: str
    note: str = TREASURY_NOTE

    @property
    def tag(self) -> str:
        return f"UST-PARYIELD-{self.date.isoformat()}"

    def to_dict(self) -> dict[str, Any]:
        return {"date": self.date.isoformat(), "ten_year_percent": self.percent, "source": self.source,
                "note": self.note, "tag": self.tag}

    def source_entry(self) -> dict[str, Any]:
        return {"tag": self.tag, "kind": "web",
                "title": f"U.S. Treasury daily par yield curve rates ({self.date.year}), 10-year on {self.date.isoformat()}",
                "date": self.date.isoformat(), "url": self.source, "primary": True,
                "note": f"{TREASURY_NOTE}. Registered by the pipeline."}


def _fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/csv"})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 (a fixed https host)
        return response.read().decode("utf-8")


def parse_treasury(text: str, url: str) -> list[Yield]:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    header = [h.strip().strip('"') for h in lines[0].split(",")]
    if "10 Yr" not in header:
        return []
    column = header.index("10 Yr")
    out = []
    for line in lines[1:]:
        cells = line.split(",")
        try:
            out.append(Yield(dt.datetime.strptime(cells[0].strip('"'), "%m/%d/%Y").date(), float(cells[column]), url))
        except (IndexError, ValueError):
            continue
    return sorted(out, key=lambda y: y.date)


def ten_year_yield(as_of: dt.date, *, fetch: Callable[[str], str] = _fetch_text) -> Yield:
    """The 10-year Treasury par yield of `as_of`, or of the last business day before it."""
    for year in (as_of.year, as_of.year - 1):
        url = TREASURY_URL.format(year=year)
        try:
            readings = [y for y in parse_treasury(fetch(url), url) if y.date <= as_of]
        except Exception as exc:  # network or format
            raise PriceError(f"the 10-year Treasury yield could not be read ({type(exc).__name__})") from None
        if readings:
            return readings[-1]
    raise PriceError(f"no 10-year Treasury yield on or before {as_of}")
