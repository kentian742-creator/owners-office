"""pipeline/holdings.py: a listed equity portfolio from the 13F, marked at the price-reference date."""

from __future__ import annotations

import datetime as dt
import json
import urllib.error

from pipeline import holdings, prices

TABLE = b"""<?xml version="1.0" encoding="UTF-8"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>COM</titleOfClass><cusip>037833100</cusip>
    <value>1000</value><shrsOrPrnAmt><sshPrnamt>10</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt></infoTable>
  <infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>COM</titleOfClass><cusip>037833100</cusip>
    <value>500</value><shrsOrPrnAmt><sshPrnamt>5</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt></infoTable>
  <infoTable><nameOfIssuer>CHUBB LIMITED</nameOfIssuer><titleOfClass>COM</titleOfClass><cusip>H1467J104</cusip>
    <value>300</value><shrsOrPrnAmt><sshPrnamt>3</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt></infoTable>
  <infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>CALL</titleOfClass><cusip>037833100</cusip>
    <value>9</value><shrsOrPrnAmt><sshPrnamt>1</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
    <putCall>Call</putCall></infoTable>
</informationTable>"""


def test_the_13f_positions_are_summed_per_security_without_options():
    positions = holdings.parse_information_table(TABLE)
    assert [(p.issuer, p.shares, p.value) for p in positions] == [("APPLE INC", 15, 1500), ("CHUBB LIMITED", 3, 300)]


def test_a_cusip_maps_to_its_us_ticker_and_a_non_us_issuer_through_its_cins(tmp_path):
    calls = []

    def post(url, jobs):
        calls.append([(j["idType"], j["idValue"]) for j in jobs])
        table = {("ID_CUSIP", "037833100"): "AAPL", ("ID_CUSIP", "526057302"): "LEN/B", ("ID_CINS", "H1467J104"): "CB"}
        return [{"data": [{"ticker": table[(j["idType"], j["idValue"])]}]} if (j["idType"], j["idValue"]) in table
                else {"warning": "No identifier found."} for j in jobs]

    cache = tmp_path / "figi.json"
    got = holdings.figi_tickers(["037833100", "H1467J104", "526057302", "000000000"], cache=cache, post=post, pause=0)
    assert got == ({"037833100": "AAPL", "H1467J104": "CB", "526057302": "LEN.B"}, {})
    assert calls[1] == [("ID_CINS", "H1467J104"), ("ID_CINS", "000000000")]
    assert holdings.figi_tickers(["037833100"], cache=cache, post=lambda *a: 1 / 0, pause=0) == (
        {"037833100": "AAPL"}, {})


def test_positions_are_marked_at_the_close_on_or_before_the_date_and_the_rest_say_why():
    positions = holdings.parse_information_table(TABLE)
    closes = [prices.Close("AAPL", dt.date(2026, 6, 30), 100.0, "u", tag="AAPL-PRICES-2026-09-30"),
              prices.Close("AAPL", dt.date(2026, 9, 29), 120.0, "u", tag="AAPL-PRICES-2026-09-30"),
              prices.Close("AAPL", dt.date(2026, 10, 1), 999.0, "u", tag="AAPL-PRICES-2026-09-30")]
    marks = holdings.mark_positions(positions, {"037833100": "AAPL"}, dt.date(2026, 6, 30), dt.date(2026, 9, 30),
                                    lambda s: closes)
    data = holdings.summary(dt.date(2026, 6, 30), dt.date(2026, 9, 30), marks)
    apple, chubb = data["positions"]
    assert apple["value_2026-09-30"] == 1800 and apple["close_date"] == "2026-09-29"
    assert apple["price_tag"] == "AAPL-PRICES-2026-09-30#2026-09-29" and "no US ticker" in chubb["not_marked"]
    assert (data["total_13f_value"], data["marked_positions_value"], data["change_on_marked_positions"],
            data["coverage"]) == (1800, 1800, 300, 0.8333)


def test_a_split_after_the_report_date_does_not_change_the_mark():
    """10 shares worth 1,000 at the report date, then a 2-for-1 split: Nasdaq's split-adjusted history has 50.0 at
    the report date and 52.0 at the mark, so the position is worth 1,040, not the 13F's 10 shares x 52."""
    position = holdings.Position("SPLIT CO", "COM", "000000001", 10, 1000)
    closes = [prices.Close("SPL", dt.date(2026, 6, 30), 50.0, "u", tag="SPL-PRICES-2026-09-30"),
              prices.Close("SPL", dt.date(2026, 9, 30), 52.0, "u", tag="SPL-PRICES-2026-09-30")]
    marks = holdings.mark_positions([position], {"000000001": "SPL"}, dt.date(2026, 6, 30), dt.date(2026, 9, 30),
                                    lambda s: closes)
    data = holdings.summary(dt.date(2026, 6, 30), dt.date(2026, 9, 30), marks)
    assert (data["marked_positions_value"], data["change_on_marked_positions"]) == (1040, 40)
    row = data["positions"][0]
    assert (row["close_2026-06-30"], row["report_close_date"], row["close_2026-09-30"]) == (50.0, "2026-06-30", 52.0)
    late = holdings.mark_positions([position], {"000000001": "SPL"}, dt.date(2026, 6, 29), dt.date(2026, 9, 30),
                                   lambda s: closes)
    assert late[0].close is None and "on or before 2026-06-29" in late[0].problem


def test_a_failed_ticker_lookup_is_reported_as_such_not_as_a_security_without_a_us_listing(tmp_path):
    def post(url, jobs):
        if jobs[0]["idType"] == "ID_CUSIP":
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)
        return [{"data": [{"ticker": "CB"}]} if j["idValue"] == "H1467J104" else {"warning": "No identifier found."}
                for j in jobs]

    cache = tmp_path / "figi.json"
    tickers, failed = holdings.figi_tickers(["037833100", "H1467J104"], cache=cache, post=post, pause=0)
    assert tickers == {"H1467J104": "CB"} and list(failed) == ["037833100"] and "OpenFIGI" in failed["037833100"]
    assert json.loads(cache.read_text(encoding="utf-8")) == {"H1467J104": "CB"}  # the failed one is asked again
    assert list(holdings.figi_tickers(["037833100"], post=lambda url, jobs: {"error": "busy"}, pause=0)[1]) == [
        "037833100"]
    positions = holdings.parse_information_table(TABLE)
    marks = holdings.mark_positions(positions, tickers, dt.date(2026, 6, 30), dt.date(2026, 9, 30), lambda s: [],
                                    failed)
    assert marks[0].problem == failed["037833100"] and "no US ticker" not in marks[0].problem
