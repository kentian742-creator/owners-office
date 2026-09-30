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


def test_the_ten_year_yield_is_the_reading_of_the_day_or_the_last_business_day_before():
    csv = ('Date,"1 Mo","10 Yr","30 Yr"\n09/29/2026,4.04,5.26,5.59\n09/25/2026,4.01,5.20,5.55\n')
    reading = prices.ten_year_yield(dt.date(2026, 9, 27), fetch=lambda url: csv)
    assert (reading.date.isoformat(), reading.percent) == ("2026-09-25", 5.20) and "10-year" in reading.note


def test_every_price_and_yield_carries_the_pipelines_own_tag_and_a_private_sources_entry():
    closes = prices.history("BRK.B", dt.date(2026, 1, 5), fetch=lambda url: DATA)
    assert closes[-1].to_dict()["tag"] == "BRK-PRICES-2026-01-05#2026-01-02"
    entry = prices.source_entry(closes[-1])
    assert (entry["tag"], entry["kind"], entry["date"], entry["url"]) == \
        ("BRK-PRICES-2026-01-05", "web", "2026-01-05", closes[-1].source) and "split" in entry["note"]
    reading = prices.ten_year_yield(dt.date(2026, 9, 29), fetch=lambda url: 'Date,"10 Yr"\n09/29/2026,5.26\n')
    assert reading.to_dict()["tag"] == "UST-PARYIELD-2026-09-29" == reading.source_entry()["tag"]
