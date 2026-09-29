"""pipeline/prices.py: closing prices with their date and source (00 §H2; STATUS T21)."""

from __future__ import annotations

import datetime as dt

import pytest

from pipeline import prices

DATA = {"data": {"tradesTable": {"rows": [
    {"date": "01/02/2026", "close": "$310.10"}, {"date": "12/31/2025", "close": "$305.63"},
    {"date": "12/30/2025", "close": "$1,304.00"}, {"date": "12/31/2024", "close": "289.89"},
    {"date": "bad", "close": "$1"}]}}}


def test_history_parses_closes_oldest_first_and_caches_without_sending_anything_personal(tmp_path):
    urls = []
    closes = prices.history("mcd", dt.date(2026, 1, 5), cache_dir=tmp_path, fetch=lambda url: urls.append(url) or DATA)
    assert [(c.date.isoformat(), c.close) for c in closes] == [
        ("2024-12-31", 289.89), ("2025-12-30", 1304.0), ("2025-12-31", 305.63), ("2026-01-02", 310.1)]
    assert urls == ["https://api.nasdaq.com/api/quote/MCD/historical?assetclass=stocks&fromdate=2016-01-05"
                    "&todate=2026-01-05&limit=9999"] and "@" not in prices.USER_AGENT
    assert prices.history("MCD", dt.date(2026, 1, 5), cache_dir=tmp_path, fetch=lambda url: 1 / 0) == closes
    assert "split" in closes[0].note and closes[0].source == urls[0]


def test_the_close_on_a_day_is_the_last_trading_day_on_or_before_it_and_year_ends_follow_the_fiscal_year():
    closes = prices.parse_history(DATA, "MCD", "u")
    assert prices.close_on_or_before(closes, dt.date(2026, 1, 1)).close == 305.63
    assert [c.date.isoformat() for c in prices.year_end_closes(closes, "12-31", 5, dt.date(2026, 1, 5))] == [
        "2025-12-31", "2024-12-31"]
    with pytest.raises(prices.PriceError):
        prices.close_on_or_before(closes, dt.date(2020, 1, 1))
    with pytest.raises(prices.PriceError, match="no closing prices"):
        prices.history("X", dt.date(2026, 1, 5), fetch=lambda url: {"data": {"tradesTable": {"rows": None}}})
