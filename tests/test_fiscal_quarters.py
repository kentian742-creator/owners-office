"""Quarter ends learned from an issuer's own 10-Ks and 10-Qs (pipeline/edgar.py; decisions/0017, amended 2026-10-11).

The issuer here keeps Costco's calendar: its fiscal year ends on the Sunday nearest August 31, and its quarters are 12,
12, 12 and 16 weeks long (the fourth 17 in a 53-week year), so its second and third quarters end in mid-February and
early May, two to three weeks before the month ends the month-end rule assumes. The report and release dates are
Costco's from FY2022 to FY2026; FY2022's second-quarter 10-Q is left out, and FY2025's has an amendment. EDGAR answers
from memory: no network access.
"""

from __future__ import annotations

import datetime as dt
import json
import types
from pathlib import Path

import pytest
import yaml

from pipeline import documents, edgar, registry
from tests import runner_fixtures as fx

FIXTURES = Path(__file__).parent / "fixtures" / "edgar"
APP_CIK, CLUB_CIK = "0001751008", "0000990830"
CLUB = edgar.Filer(ticker="CLUB", cik=CLUB_CIK, type=edgar.DOMESTIC, fiscal_year_end="08-30", earnings_form="8-K",
                   annual_form="10-K")
CLUB_10K = ["2021-08-29", "2022-08-28", "2023-09-03", "2024-09-01", "2025-08-31", "2026-08-30"]  # report dates
CLUB_10Q = ["2021-11-21", "2022-05-08",  # FY2022: no 10-Q for the second quarter, which ended 2022-02-13
            "2022-11-20", "2023-02-12", "2023-05-07", "2023-11-26", "2024-02-18", "2024-05-12",
            "2024-11-24", "2025-02-16", "2025-05-11", "2025-11-23", "2026-02-15", "2026-05-10"]
CLUB_RELEASES = ["2021-12-09", "2022-03-03", "2022-05-26", "2022-09-22", "2022-12-08", "2023-03-02", "2023-05-25",
                 "2023-09-26", "2023-12-14", "2024-03-07", "2024-05-30", "2024-09-26", "2024-12-12", "2025-03-06",
                 "2025-05-29", "2025-09-25", "2025-12-11", "2026-03-05", "2026-05-28", "2026-09-24"]
D = dt.date.fromisoformat


def sub_url(cik: str) -> str:
    return f"{edgar.DATA_BASE}/submissions/CIK{cik}.json"


def club_submissions() -> bytes:
    """EDGAR submissions of the issuer: each 10-K filed 38 days after its year end, each 10-Q 24 days after its quarter
    end, an amendment of FY2025's second-quarter 10-Q, and results 8-Ks (Items 2.02 and 9.01) accepted after the
    close on the release date."""
    rows = [("10-K", D(day), D(day) + dt.timedelta(days=38)) for day in CLUB_10K]
    rows += [("10-Q", D(day), D(day) + dt.timedelta(days=24)) for day in CLUB_10Q]
    rows += [("10-Q/A", D("2025-02-16"), D("2025-04-10"))]
    rows += [("8-K", D(day), D(day)) for day in CLUB_RELEASES]
    columns = ("accessionNumber", "filingDate", "reportDate", "acceptanceDateTime", "form", "items", "primaryDocument")
    recent: dict[str, list[str]] = {name: [] for name in columns}
    for n, (form, report, filed) in enumerate(sorted(rows, key=lambda row: row[2]), start=1):
        for name, value in zip(columns, (f"{CLUB_CIK}-{filed:%y}-{n:06d}", filed.isoformat(), report.isoformat(),
                                         f"{filed.isoformat()}T21:30:00.000Z", form,
                                         "2.02,9.01" if form == "8-K" else "", f"club-{report:%Y%m%d}.htm")):
            recent[name].append(value)
    return json.dumps({"cik": CLUB_CIK.lstrip("0"), "name": "WHOLESALE CLUB INC", "tickers": ["CLUB"],
                       "fiscalYearEnd": "0830", "filings": {"recent": recent, "files": []}}).encode()


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """Tests in this file never touch the real .env, ~/.cache or the network."""
    monkeypatch.delenv(edgar.UA_ENV, raising=False)
    monkeypatch.setenv(edgar.ENV_FILE_ENV, str(tmp_path / "missing.env"))
    monkeypatch.setenv(edgar.CACHE_ENV, str(tmp_path / "default-cache"))

    def no_network(*args, **kwargs):
        raise AssertionError("tests must not access the network")

    monkeypatch.setattr(edgar, "UrllibTransport", no_network)
    edgar.default_client.cache_clear()
    yield
    edgar.default_client.cache_clear()


@pytest.fixture
def client(tmp_path):
    responses = {sub_url(CLUB_CIK): club_submissions(),
                 sub_url(APP_CIK): (FIXTURES / f"CIK{APP_CIK}.json").read_bytes()}  # month-end quarters

    def transport(url, headers, timeout):
        return (200, {}, responses[url]) if url in responses else (404, {}, b"<html>Not Found</html>")

    clock = fx.Clock()
    return edgar.EdgarClient(user_agent=fx.FAKE_UA, transport=transport, cache_dir=tmp_path / "cache",
                             sleep=clock.sleep, jitter=0,
                             limiter=edgar.RateLimiter(5, clock=clock.monotonic, sleep=clock.sleep))


@pytest.fixture
def subs(client):
    return edgar.submissions(CLUB_CIK, client=client)


def test_quarter_ends_are_the_report_dates_of_the_issuer_s_own_10qs_and_10ks(client, subs):
    cal = edgar.fiscal_calendar(subs, "08-30")
    plain = edgar.FiscalCalendar.parse("08-30")
    assert cal.label == "08-30" and cal != plain
    assert [cal.quarter_end(2026, q) for q in (1, 2, 3, 4)] == [D("2025-11-23"), D("2026-02-15"), D("2026-05-10"),
                                                               D("2026-08-30")]
    assert [cal.quarter_label(day) for day in (D("2026-02-15"), D("2026-05-10"))] == ["FY2026Q2", "FY2026Q3"]
    for day in (D("2026-02-15"), D("2026-05-10")):  # the month-end rule reads these as January 31 and April 30
        with pytest.raises(ValueError):
            plain.period_of(day)
    # a third-quarter release comes out before May 31, but after the quarter's end on May 10
    assert cal.last_quarter_end_before(D("2026-05-28")) == D("2026-05-10")
    assert plain.last_quarter_end_before(D("2026-05-28")) == D("2026-02-28")
    # the amendment of FY2025's second-quarter 10-Q is not counted as a fourth 10-Q: FY2025 is learned too
    assert [cal.quarter_end(2025, q) for q in (1, 2, 3)] == [D("2024-11-24"), D("2025-02-16"), D("2025-05-11")]
    # a company whose quarters the month-end rule places keeps the month-end calendar
    app = edgar.fiscal_calendar(edgar.submissions(APP_CIK, client=client), "12-31")
    assert app == edgar.FiscalCalendar.parse("12-31") and app.known_ends == ()


def test_a_year_without_three_10qs_keeps_the_month_end_rule(client, subs):
    cal = edgar.fiscal_calendar(subs, "08-30")
    # FY2022 has two 10-Qs, so neither is taken for a quarter it might not be; its year end is still its 10-K's
    assert [cal.quarter_end(2022, q) for q in (1, 2, 3, 4)] == [D("2021-11-30"), D("2022-02-28"), D("2022-05-31"),
                                                               D("2022-08-28")]
    assert cal.quarter_label(D("2021-11-21")) == "FY2022Q1"  # placed by the month-end rule (November 30)
    with pytest.raises(ValueError):
        cal.period_of(D("2022-05-08"))
    # so FY2022's third-quarter release, of 2022-05-26, is still taken for a second release of FY2022Q2
    events = edgar.earnings_events(CLUB_CIK, "08-30", since=D("2021-09-01"), until=D("2022-08-31"), client=client,
                                   subs=subs)
    assert [e.period for e in events] == ["FY2022Q1", "FY2022Q2"]
    assert [f.filing_date for f in events[1].related] == [D("2022-05-26")]


def test_later_quarters_are_projected_52_weeks_on_and_matched_within_a_week(subs):
    cal = edgar.fiscal_calendar(subs, "08-30")
    assert [cal.quarter_end(2027, q) for q in (1, 2, 3, 4)] == [D("2026-11-22"), D("2027-02-14"), D("2027-05-09"),
                                                               D("2027-08-29")]
    assert cal.quarter_end(2028, 3) == D("2028-05-07")
    assert cal.quarter_label(D("2027-05-09")) == "FY2027Q3"
    assert cal.last_quarter_end_before(D("2027-05-27")) == D("2027-05-09")
    # FY2023 had 53 weeks, so FY2024's quarters ended a week after their projection from FY2023; as of FY2023's 10-K,
    # FY2024's third-quarter 10-Q (2024-05-12) still falls within a week of the projected end
    as_of_2023 = edgar.learn_quarter_ends(edgar.FiscalCalendar.parse("08-30"),
                                          [f for f in subs.filings if f.filing_date <= D("2023-10-31")])
    assert as_of_2023.quarter_end(2024, 3) == D("2024-05-05")
    assert as_of_2023.quarter_label(D("2024-05-12")) == "FY2024Q3"
    with pytest.raises(ValueError):
        as_of_2023.period_of(D("2024-05-13"))


def test_events_are_labelled_by_the_issuer_s_own_quarters(client, subs):
    events = edgar.earnings_events(CLUB_CIK, "08-30", since=D("2022-09-01"), client=client, subs=subs)
    assert [e.period for e in events] == ["FY2022Q4"] + [f"FY{y}Q{q}" for y in range(2023, 2027) for q in (1, 2, 3, 4)]
    for event in events:  # each closed by its own 10-Q or 10-K, with no second release
        assert event.closing.report_date == event.period_end and not event.related and not event.notes
    third = [(e.period, e.release_date, e.period_end, e.closed_by) for e in events if e.quarter == 3]
    assert third == [("FY2023Q3", D("2023-05-25"), D("2023-05-07"), "10-Q"),
                     ("FY2024Q3", D("2024-05-30"), D("2024-05-12"), "10-Q"),
                     ("FY2025Q3", D("2025-05-29"), D("2025-05-11"), "10-Q"),
                     ("FY2026Q3", D("2026-05-28"), D("2026-05-10"), "10-Q")]


def test_10q_tags_resolve_to_the_issuer_s_own_quarters(client, subs):
    accession = {(f.form, f.report_date): f.accession for f in subs.filings}
    issuers: dict = {}
    for tag, form, report in [("CLUB-10Q-FY2026Q2", "10-Q", "2026-02-15"),
                              ("CLUB-10Q-FY2026Q3", "10-Q", "2026-05-10"),
                              ("CLUB-10Q-FY2025Q2", "10-Q", "2025-02-16"),  # the original, not its amendment
                              ("CLUB-10QA-FY2025Q2", "10-Q/A", "2025-02-16")]:
        filing, issuer = edgar.filing_for_entry({"tag": tag}, company=CLUB, client=client, issuers=issuers)
        assert filing.accession == accession[(form, D(report))]
        assert edgar.expected_period(filing, issuer, client) == tag.rsplit("-", 1)[1]
    # the archive's tag for the third quarter's 10-Q (a new archive, a valuation check, a post-earnings step)
    ten_q = subs.get(accession[("10-Q", D("2026-05-10"))])
    plain = edgar.FiscalCalendar.parse("08-30")
    assert documents.tag_with_ordinal("CLUB", ten_q, issuers[CLUB_CIK].calendar, subs.filings) == "CLUB-10Q-FY2026Q3"
    # by the month-end rule the 10-Q has no fiscal period, so its tag falls back to the filing date
    assert documents.tag_with_ordinal("CLUB", ten_q, plain, subs.filings) == "CLUB-10Q-2026-06-03"


def test_the_next_third_quarter_is_estimated_from_the_past_third_quarters(client):
    estimate = edgar.next_release(CLUB_CIK, "FY2027Q3", fiscal_year_end="08-30", as_of=D("2026-10-01"), client=client)
    assert estimate.period_end == D("2027-05-09")
    assert [(e.period, e.release_date) for e in estimate.history] == [
        ("FY2024Q3", D("2024-05-30")), ("FY2025Q3", D("2025-05-29")), ("FY2026Q3", D("2026-05-28"))]
    assert (estimate.status, estimate.expected_release, estimate.history_window) == (
        "estimated", D("2027-05-25"), (D("2027-05-28"), D("2027-05-30")))
    assert not estimate.notes


def test_post_earnings_steps_and_the_monthly_outlook_find_the_third_quarter(client, tmp_path, monkeypatch):
    gateway = registry.EdgarGateway(client)
    monkeypatch.setattr(gateway, "_results_documents", lambda event, *rest: event)
    # the event is looked for from the quarter's end, May 10, not May 31: the release came on May 28
    event = gateway.period_results(CLUB, "FY2026Q3", D("2026-06-10"))
    assert (event.period, event.release_date, event.closing.report_date) == ("FY2026Q3", D("2026-05-28"),
                                                                              D("2026-05-10"))
    assert documents.window_start("FY2026Q3", 1, gateway.fiscal_calendar(CLUB)) == D("2026-02-16")
    company = tmp_path / "public" / "companies" / "CLUB"
    company.mkdir(parents=True)
    filer = {"cik": CLUB_CIK, "type": "domestic", "fiscal_year_end": "08-30", "earnings_form": "8-K"}
    (company / "thesis.yml").write_text(yaml.safe_dump({"company": "CLUB", "status": "candidate", "filer": filer}))
    ctx = types.SimpleNamespace(public_root=tmp_path / "public", run_date=D("2027-04-30"), gateway=lambda: gateway)
    # the third quarter ends on 2027-05-09, early enough to be released in May (by the month-end rule, on May 31)
    rows = registry._upcoming_events(ctx, D("2027-05-01"), D("2027-05-31"))
    assert [(r["company"], r["period"], r["expected_release"], r["placeholder"]) for r in rows] == [
        ("CLUB", "FY2027Q3", "2027-05-25", True)]
