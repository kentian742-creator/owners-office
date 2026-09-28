"""Tests for pipeline/edgar.py: no network access, no .env.

The EDGAR data in tests/fixtures/edgar/ are excerpts cut from real responses: the submissions of APP (domestic
issuer) and PDD (foreign private issuer) from 2023-09-24 to 2026-09-24, the index.json and the start of the press
release of every PDD 6-K, and the index.json of one APP 8-K. The expected values match phase 0's
inputs/edgar/release_history.yml; when that file is in the workspace, the tests also compare it field by field.
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import logging
import os
import ssl
import stat
import sys
import traceback
import types
from pathlib import Path

import pytest
import yaml

from pipeline import edgar

FIXTURES = Path(__file__).parent / "fixtures" / "edgar"
WORKSPACE_HISTORY = Path(edgar.WORKSPACE_ROOT) / "inputs" / "edgar" / "release_history.yml"
APP_CIK, PDD_CIK = "0001751008", "0001737806"
FAKE_EMAIL = "canary-3f9c@example.invalid"
FAKE_UA = f"OwnersOffice-test {FAKE_EMAIL}"
AS_OF = dt.date(2026, 9, 24)  # end of the window of phase 0's release_history.yml


def sub_url(cik: str) -> str:
    return f"{edgar.DATA_BASE}/submissions/CIK{cik}.json"


def load_responses() -> dict[str, bytes]:
    responses = {sub_url(cik): (FIXTURES / f"CIK{cik}.json").read_bytes() for cik in (APP_CIK, PDD_CIK)}
    archives = json.loads((FIXTURES / "archives.json").read_text(encoding="utf-8"))["responses"]
    for url, body in archives.items():
        responses[url] = json.dumps(body).encode() if isinstance(body, dict) else body.encode()
    return responses


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FixtureTransport:
    """Returns the excerpted real response for each URL, and 404 for any other URL.

    Records the URL, request headers and (fake) time of every request.
    """

    def __init__(self, overrides: dict[str, bytes] | None = None, clock: FakeClock | None = None):
        self.responses = load_responses()
        self.responses.update(overrides or {})
        self.clock = clock
        self.calls: list[tuple[str, dict[str, str], float | None]] = []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, dict(headers), self.clock.now if self.clock else None))
        if url in self.responses:
            return 200, {"content-type": "application/json" if url.endswith(".json") else "text/html"}, self.responses[url]
        return 404, {}, b"<html>Not Found</html>"

    @property
    def urls(self) -> list[str]:
        return [url for url, _, _ in self.calls]


def make_client(tmp_path: Path, transport=None, clock: FakeClock | None = None, **kwargs) -> edgar.EdgarClient:
    clock = clock or FakeClock()
    kwargs.setdefault("cache_dir", tmp_path / "cache")
    return edgar.EdgarClient(user_agent=FAKE_UA, transport=transport or FixtureTransport(clock=clock),
                             limiter=edgar.RateLimiter(5, clock=clock.monotonic, sleep=clock.sleep),
                             sleep=clock.sleep, jitter=0, **kwargs)


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """Tests in this file never touch the real .env, ~/.cache or the network."""
    monkeypatch.delenv(edgar.UA_ENV, raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
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
    return make_client(tmp_path)


# ---------------------------------------------------------------------------------------------------------------
# User-Agent
# ---------------------------------------------------------------------------------------------------------------


def test_user_agent_comes_from_the_environment_then_the_env_file(tmp_path):
    env_file = tmp_path / "ws.env"
    env_file.write_text(f'# workspace settings\nOTHER=1\nexport {edgar.UA_ENV}="{FAKE_UA}"\n', encoding="utf-8")
    assert edgar.load_user_agent(env={}, env_file=env_file) == FAKE_UA
    assert edgar.load_user_agent(env={edgar.ENV_FILE_ENV: str(env_file)}) == FAKE_UA
    other = "Env Agent env-agent@example.invalid"
    assert edgar.load_user_agent(env={edgar.UA_ENV: other}, env_file=env_file) == other
    env_file.write_text(f"{edgar.UA_ENV}={FAKE_UA}  # trailing comment\n", encoding="utf-8")
    assert edgar.load_user_agent(env={}, env_file=env_file) == FAKE_UA


def test_no_user_agent_means_no_network(tmp_path):
    with pytest.raises(edgar.MissingUserAgent, match=edgar.UA_ENV):
        edgar.EdgarClient(cache_dir=tmp_path)
    with pytest.raises(edgar.MissingUserAgent):
        edgar.load_user_agent(env={}, env_file=tmp_path / "empty.env")
    offline = edgar.EdgarClient(cache_dir=tmp_path, offline=True)  # reads the cache only; needs no User-Agent
    with pytest.raises(edgar.EdgarOffline):
        offline.get_bytes(sub_url(APP_CIK))


def test_an_unusable_user_agent_is_rejected_without_echoing_it(tmp_path):
    value = "no contact given 9d2e"
    with pytest.raises(edgar.MissingUserAgent) as info:
        edgar.load_user_agent(env={edgar.UA_ENV: value})
    assert value not in str(info.value)
    with pytest.raises(edgar.MissingUserAgent) as info:
        edgar.EdgarClient(user_agent="line\nbreak@example.invalid", cache_dir=tmp_path)
    assert "break@example.invalid" not in str(info.value)


def test_user_agent_is_sent_but_never_logged_cached_or_shown(tmp_path, caplog):
    clock = FakeClock()
    body = (FIXTURES / f"CIK{APP_CIK}.json").read_bytes()
    seen: list[dict] = []

    def flaky(url, headers, timeout):
        seen.append(dict(headers))
        if len(seen) == 1:
            raise ConnectionResetError(f"connection reset while sending {headers['User-Agent']}")
        if len(seen) == 2:
            return 503, {"Retry-After": "2"}, f"<html>busy, {headers['User-Agent']}</html>".encode()
        return 200, {}, body

    client = make_client(tmp_path, transport=flaky, clock=clock)
    with caplog.at_level(logging.DEBUG, logger="pipeline.edgar"):
        subs = edgar.submissions(APP_CIK, client=client)
    assert subs.name == "AppLovin Corp"
    assert [h["User-Agent"] for h in seen] == [FAKE_UA] * 3  # the requests do carry it
    assert clock.sleeps == [1.0, 2.0]  # backoff after the network error, Retry-After after the 503
    assert FAKE_UA not in caplog.text and FAKE_EMAIL not in caplog.text
    assert edgar.REDACTED in caplog.text  # it was in the transport error; the log shows the placeholder
    for path in (tmp_path / "cache").rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert FAKE_UA.encode() not in data and FAKE_EMAIL.encode() not in data
    assert FAKE_UA not in repr(client) and FAKE_EMAIL not in repr(client)

    def failing(url, headers, timeout):
        raise OSError(f"cannot connect as {headers['User-Agent']}")

    broken = make_client(tmp_path, transport=failing, max_attempts=2)
    with pytest.raises(edgar.EdgarHTTPError) as info:
        broken.get_bytes(f"{edgar.ARCHIVES_BASE}/1751008/000175100826000057/index.json")
    shown = "".join(traceback.format_exception(info.value)) + repr(info.value)
    assert FAKE_UA not in shown and FAKE_EMAIL not in shown
    assert "failed on all 2 attempts" in str(info.value)


def test_command_line_never_prints_the_user_agent(tmp_path, monkeypatch, capsys, caplog):
    monkeypatch.setenv(edgar.UA_ENV, FAKE_UA)
    transport = FixtureTransport()
    monkeypatch.setattr(edgar, "UrllibTransport", lambda: transport)
    cache = ["--cache-dir", str(tmp_path / "cli-cache")]
    caplog.set_level(logging.INFO, logger="pipeline.edgar")
    assert edgar.main(["release-history", "APP", "--as-of", AS_OF.isoformat(), *cache, "-v"]) == 0
    out, err = capsys.readouterr()
    assert len(yaml.safe_load(out)["events"]) == 12
    assert "GET" in caplog.text  # -v logs requests, but not their headers
    assert FAKE_UA not in out + err + caplog.text and FAKE_EMAIL not in out + err + caplog.text

    monkeypatch.setattr(edgar, "UrllibTransport", lambda: lambda url, headers, timeout: (403, {}, b"<html>blocked</html>"))
    assert edgar.main(["submissions", "PDD", "--cache-dir", str(tmp_path / "other-cache")]) == 3
    out, err = capsys.readouterr()
    assert "denied access" in err and FAKE_UA not in out + err and FAKE_EMAIL not in out + err

    monkeypatch.delenv(edgar.UA_ENV)
    assert edgar.main(["release-history", "APP", "--cache-dir", str(tmp_path / "third-cache")]) == 2
    assert edgar.UA_ENV in capsys.readouterr().err


# ---------------------------------------------------------------------------------------------------------------
# Rate limiting, retries, TLS, cache
# ---------------------------------------------------------------------------------------------------------------


def test_rate_limiter_allows_at_most_five_requests_per_second():
    clock = FakeClock()
    limiter = edgar.RateLimiter(5, clock=clock.monotonic, sleep=clock.sleep)
    times = []
    for _ in range(16):
        limiter.acquire()
        times.append(clock.now)
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert min(gaps) >= 0.2 - 1e-9
    assert all(sum(1 for t in times if start <= t < start + 1.0) <= 5 for start in times)
    with pytest.raises(ValueError):
        edgar.RateLimiter(6)
    with pytest.raises(ValueError):
        edgar.RateLimiter(0)


def test_every_request_goes_through_the_limiter(tmp_path):
    clock = FakeClock()
    transport = FixtureTransport(clock=clock)
    client = make_client(tmp_path, transport=transport, clock=clock)
    edgar.release_history(PDD_CIK, "12-31", filer_type=edgar.FOREIGN, as_of=AS_OF, client=client)
    stamps = [t for _, _, t in transport.calls]
    assert len(stamps) == 41  # submissions + index.json and press release of 20 6-Ks
    assert min(b - a for a, b in zip(stamps, stamps[1:])) >= 0.2 - 1e-9


def test_retries_back_off_on_429_and_5xx_but_not_on_404_or_403(tmp_path):
    def scripted(*replies):
        calls = []

        def transport(url, headers, timeout):
            calls.append(timeout)
            reply = replies[min(len(calls), len(replies)) - 1]
            if isinstance(reply, BaseException):
                raise reply
            return reply

        transport.calls = calls
        return transport

    ok = (200, {}, b'{"ok": true}')
    url = f"{edgar.DATA_BASE}/submissions/CIK{APP_CIK}.json"

    clock = FakeClock()
    transport = scripted((503, {}, b""), (429, {"Retry-After": "7"}, b""), TimeoutError("read timed out"), ok)
    client = make_client(tmp_path, transport=transport, clock=clock, timeout=12.5)
    assert client.get_json(url) == {"ok": True}
    assert clock.sleeps == [1.0, 7.0, 4.0] and transport.calls == [12.5] * 4

    clock = FakeClock()
    transport = scripted((500, {}, b""))
    client = make_client(tmp_path / "b", transport=transport, clock=clock)
    with pytest.raises(edgar.EdgarHTTPError, match="HTTP 500"):
        client.get_json(url)
    assert clock.sleeps == [1.0, 2.0, 4.0, 8.0] and len(transport.calls) == edgar.DEFAULT_ATTEMPTS

    for status, error in ((404, edgar.EdgarNotFound), (403, edgar.EdgarAccessDenied)):
        clock = FakeClock()
        transport = scripted((status, {}, b""))
        client = make_client(tmp_path / str(status), transport=transport, clock=clock)
        with pytest.raises(error):
            client.get_json(url)
        assert len(transport.calls) == 1 and clock.sleeps == []


def test_gzip_bodies_are_decoded_and_block_pages_are_not_cached(tmp_path):
    import gzip

    payload = json.dumps({"cik": APP_CIK}).encode()
    sent: list[dict] = []

    def gzipped(url, headers, timeout):
        sent.append(dict(headers))
        return 200, {"Content-Encoding": "gzip"}, gzip.compress(payload)

    client = make_client(tmp_path, transport=gzipped)
    assert client.get_json(sub_url(APP_CIK)) == {"cik": APP_CIK}
    assert sent[0]["Accept-Encoding"] == "gzip, deflate"

    block = b"<!DOCTYPE html><html><body>Your Request Originates from an Undeclared Automated Tool</body></html>"
    blocked = make_client(tmp_path / "blocked", transport=lambda url, h, t: (200, {}, block))
    with pytest.raises(edgar.EdgarAccessDenied, match="block page"):
        blocked.get_json(sub_url(PDD_CIK))
    assert not (tmp_path / "blocked" / "cache" / "data.sec.gov" / "submissions" / f"CIK{PDD_CIK}.json").exists()


def test_only_sec_hosts_are_reachable(client):
    for url in ("https://example.com/data.json", "http://www.sec.gov/Archives/x.htm",
                "https://www.sec.gov.example.com/x.json", "https://www.sec.gov/Archives/../etc/passwd"):
        with pytest.raises(edgar.EdgarError):
            client.get_bytes(url)


def test_tls_context_uses_ssl_cert_file_then_certifi_then_the_system(monkeypatch, tmp_path):
    seen = []
    real = ssl.create_default_context

    def recording(*args, **kwargs):
        seen.append(kwargs.get("cafile"))
        return real()

    monkeypatch.setattr(edgar.ssl, "create_default_context", recording)
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "ca.pem"))
    edgar.tls_context()
    assert seen[-1] == str(tmp_path / "ca.pem")

    monkeypatch.delenv("SSL_CERT_FILE")
    fake_certifi = types.ModuleType("certifi")
    fake_certifi.where = lambda: "/opt/certifi/cacert.pem"
    monkeypatch.setitem(sys.modules, "certifi", fake_certifi)
    edgar.tls_context()
    assert seen[-1] == "/opt/certifi/cacert.pem"

    monkeypatch.setitem(sys.modules, "certifi", None)  # certifi not installed
    ctx = edgar.tls_context()
    assert seen[-1] is None
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname


def test_submissions_are_cached_for_an_hour_archives_forever(tmp_path):
    clock = FakeClock()
    transport = FixtureTransport(clock=clock)
    now = [1_800_000_000.0]
    client = make_client(tmp_path, transport=transport, clock=clock, now=lambda: now[0])
    index = f"{edgar.ARCHIVES_BASE}/1751008/000175100826000057/index.json"
    edgar.submissions(APP_CIK, client=client)
    edgar.filing_documents(APP_CIK, "0001751008-26-000057", client=client)
    edgar.submissions(APP_CIK, client=client)
    assert transport.urls == [sub_url(APP_CIK), index]
    now[0] += edgar.SUBMISSIONS_TTL + 1
    edgar.submissions(APP_CIK, client=client)
    edgar.filing_documents(APP_CIK, "0001751008-26-000057", client=client)
    assert transport.urls == [sub_url(APP_CIK), index, sub_url(APP_CIK)]

    refreshing = make_client(tmp_path, transport=transport, clock=clock, now=lambda: now[0], refresh=True)
    edgar.submissions(APP_CIK, client=refreshing)
    assert transport.urls.count(sub_url(APP_CIK)) == 3

    now[0] += 30 * 86400  # offline: an expired cache entry is still used, with no network access
    offline = edgar.EdgarClient(cache_dir=tmp_path / "cache", offline=True, now=lambda: now[0])
    assert edgar.submissions(APP_CIK, client=offline).name == "AppLovin Corp"
    with pytest.raises(edgar.EdgarOffline):
        edgar.submissions(PDD_CIK, client=offline)
    meta = json.loads((tmp_path / "cache" / "data.sec.gov" / "submissions" / f"CIK{APP_CIK}.json.meta.json").read_text())
    assert set(meta) == {"url", "fetched_at"}


def test_submissions_merge_the_overflow_pages(tmp_path):
    data = json.loads((FIXTURES / f"CIK{APP_CIK}.json").read_text())
    rec = data["filings"]["recent"]
    old = [i for i, day in enumerate(rec["filingDate"]) if day < "2025-01-01"]
    new = [i for i, day in enumerate(rec["filingDate"]) if day >= "2025-01-01"] + old[:1]  # one row is in both places
    page_name = f"CIK{APP_CIK}-submissions-001.json"
    split = dict(data)
    split["filings"] = {
        "recent": {c: [rec[c][i] for i in new] for c in rec},
        "files": [{"name": page_name, "filingCount": len(old), "filingFrom": min(rec["filingDate"][i] for i in old),
                   "filingTo": max(rec["filingDate"][i] for i in old)}],
    }
    page = {c: [rec[c][i] for i in old] for c in rec}
    overrides = {sub_url(APP_CIK): json.dumps(split).encode(),
                 f"{edgar.DATA_BASE}/submissions/{page_name}": json.dumps(page).encode()}
    client = make_client(tmp_path, transport=FixtureTransport(overrides))
    subs = edgar.submissions(APP_CIK, client=client)
    assert subs.pages_loaded == [page_name]
    assert len(subs.filings) == len(rec["accessionNumber"])
    assert [f.filing_date for f in subs.filings] == sorted(f.filing_date for f in subs.filings)
    whole = edgar.release_history(APP_CIK, "12-31", as_of=AS_OF, client=make_client(tmp_path / "whole"))
    merged = edgar.release_history(APP_CIK, "12-31", as_of=AS_OF, client=client)
    assert [e.to_dict() for e in merged] == [e.to_dict() for e in whole]
    later = edgar.submissions(APP_CIK, client=make_client(tmp_path / "later", transport=FixtureTransport(overrides)),
                              since=dt.date(2025, 6, 1))
    assert later.pages_loaded == []  # the continuation page ends before since, so it is not fetched

    bad = dict(split)
    bad["filings"] = {"recent": split["filings"]["recent"], "files": [{"name": "../../etc/passwd"}]}
    with pytest.raises(edgar.EdgarDataError):
        edgar.submissions(APP_CIK, client=make_client(tmp_path / "bad", transport=FixtureTransport(
            {sub_url(APP_CIK): json.dumps(bad).encode()})))


# ---------------------------------------------------------------------------------------------------------------
# Documents and exhibits
# ---------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name, expected", [
    ("app-20260805.htm", ("primary", None)),
    ("exhibit991-2q26earningspre.htm", ("exhibit", "EX-99.1")),
    ("tm2623874d1_ex99-1.htm", ("exhibit", "EX-99.1")),
    ("d564412dex991.htm", ("exhibit", "EX-99.1")),
    ("a21-10044_1ex99d1.htm", ("exhibit", "EX-99.1")),
    ("msft-ex99_2.htm", ("exhibit", "EX-99.2")),
    ("spgi-20231102xex99.htm", ("exhibit", "EX-99")),
    ("R1.htm", ("xbrl_viewer", None)),
    ("R117.htm", ("xbrl_viewer", None)),
    ("exhibit101applovincorporat.htm", ("other", None)),
    ("FilingSummary.xml", ("other", None)),
    ("0001751008-26-000057-xbrl.zip", ("other", None)),
])
def test_documents_are_classified_by_name(name, expected):
    assert edgar.classify_document(name, "app-20260805.htm") == expected


def test_download_takes_the_primary_document_and_ex99_but_never_xbrl_viewer_pages(tmp_path):
    folder = f"{edgar.ARCHIVES_BASE}/1751008/000175100826000057"
    transport = FixtureTransport({f"{folder}/app-20260805.htm": b"<html>8-K</html>",
                                  f"{folder}/exhibit991-2q26earningspre.htm": b"<html>EX-99.1</html>"})
    client = make_client(tmp_path, transport=transport)
    docs = {d.name: d for d in edgar.filing_documents(APP_CIK, "0001751008-26-000057", client=client,
                                                        primary_document="app-20260805.htm")}
    assert docs["R1.htm"].kind == "xbrl_viewer" and docs["exhibit991-2q26earningspre.htm"].size == 226449
    paths = edgar.download_filing(APP_CIK, "000175100826000057", tmp_path / "out", client=client)
    assert sorted(p.name for p in paths) == ["app-20260805.htm", "exhibit991-2q26earningspre.htm"]
    assert all(p.parent.name == "0001751008-26-000057" for p in paths)
    assert (tmp_path / "out" / "0001751008-26-000057" / "exhibit991-2q26earningspre.htm").read_bytes() == b"<html>EX-99.1</html>"
    assert not any(url.endswith(("R1.htm", ".xsd", ".xml", ".zip")) for url in transport.urls)
    assert stat.S_IMODE(paths[0].stat().st_mode) == 0o644


def test_an_exhibit_whose_file_name_does_not_say_ex99_is_found_through_the_filing_index(tmp_path):
    """S&P Global names its release spgi2q2026-earningsrelease.htm; the index page says it is EX-99.1."""
    folder = f"{edgar.ARCHIVES_BASE}/64040/000006404026000040"
    listing = {"directory": {"item": [{"name": n, "size": "10"} for n in (
        "spgi-20260728.htm", "spgi2q2026-earningsrelease.htm", "image.jpg", "0000064040-26-000040-index.html")]}}
    page = (b"<table><tr><td>1</td><td>8-K</td><td><a>spgi-20260728.htm</a> &nbsp;&nbsp;iXBRL</td><td>8-K</td></tr>"
            b"<tr><td>2</td><td>EX-99.1</td><td><a>spgi2q2026-earningsrelease.htm</a></td><td>EX-99.1</td></tr>"
            b"<tr><td>6</td><td>GRAPHIC</td><td>image.jpg</td><td>GRAPHIC</td></tr></table>")
    transport = FixtureTransport({f"{folder}/index.json": json.dumps(listing).encode(),
                                  f"{folder}/0000064040-26-000040-index.html": page})
    docs = {d.name: d for d in edgar.filing_documents("64040", "0000064040-26-000040",
                                                        client=make_client(tmp_path, transport=transport),
                                                        primary_document="spgi-20260728.htm")}
    assert (docs["spgi2q2026-earningsrelease.htm"].kind, docs["spgi2q2026-earningsrelease.htm"].exhibit) == \
        ("exhibit", "EX-99.1")
    assert docs["spgi-20260728.htm"].kind == "primary" and docs["image.jpg"].kind == "other"
    del transport.responses[f"{folder}/0000064040-26-000040-index.html"]  # no index page: the name rules stand alone
    docs = {d.name: d for d in edgar.filing_documents("64040", "0000064040-26-000040",
                                                        client=make_client(tmp_path / "b", transport=transport),
                                                        primary_document="spgi-20260728.htm")}
    assert docs["spgi2q2026-earningsrelease.htm"].kind == "other"


def test_results_headline_and_dateline_are_read_from_the_press_release():
    text = edgar.html_to_text(
        "<P><FONT><B>PDD Holdings\nAnnounces </B></FONT><B>Fourth Quarter 2025 and Fiscal Year 202</B><B>5 "
        "Unaudited Financial Results</B></P><P>DUBLIN and SHANGHAI, March&nbsp;25, 2026 (GLOBE NEWSWIRE) &ndash; "
        "results for the quarter ended December&nbsp;31, 2025.</P>")
    found = edgar.find_results_title(text)
    assert found.title == "PDD Holdings Announces Fourth Quarter 2025 and Fiscal Year 2025 Unaudited Financial Results"
    assert (found.quarter, found.year, found.dateline) == (4, 2025, dt.date(2026, 3, 25))
    for headline in ("PDD Holdings Announces Results of Annual General Meeting",
                     "PDD Holdings to Report Third Quarter 2026 Unaudited Financial Results on November 18, 2026",
                     "Notice of Annual General Meeting"):
        assert edgar.find_results_title(headline) is None


# ---------------------------------------------------------------------------------------------------------------
# Earnings events and release_history (matching phase 0's inputs/edgar/release_history.yml)
# ---------------------------------------------------------------------------------------------------------------

# period, accession number, EDGAR filing date, acceptanceDateTime, US Eastern time, closing 10-Q/10-K
APP_EXPECTED = [
    ("FY2023Q3", "0001751008-23-000070", "2023-11-08", "2023-11-08T21:16:43.000Z", "2023-11-08 16:16:43 EST", "0001751008-23-000072"),
    ("FY2023Q4", "0001751008-24-000004", "2024-02-14", "2024-02-14T21:07:34.000Z", "2024-02-14 16:07:34 EST", "0001751008-24-000012"),
    ("FY2024Q1", "0001751008-24-000041", "2024-05-08", "2024-05-08T20:07:38.000Z", "2024-05-08 16:07:38 EDT", "0001751008-24-000043"),
    ("FY2024Q2", "0001751008-24-000053", "2024-08-07", "2024-08-07T20:07:40.000Z", "2024-08-07 16:07:40 EDT", "0001751008-24-000055"),
    ("FY2024Q3", "0001751008-24-000059", "2024-11-06", "2024-11-06T21:08:45.000Z", "2024-11-06 16:08:45 EST", "0001751008-24-000062"),
    ("FY2024Q4", "0001751008-25-000006", "2025-02-12", "2025-02-12T21:07:44.000Z", "2025-02-12 16:07:44 EST", "0001751008-25-000018"),
    ("FY2025Q1", "0001751008-25-000051", "2025-05-07", "2025-05-07T20:19:46.000Z", "2025-05-07 16:19:46 EDT", "0001751008-25-000053"),
    ("FY2025Q2", "0001751008-25-000069", "2025-08-06", "2025-08-06T20:08:54.000Z", "2025-08-06 16:08:54 EDT", "0001751008-25-000072"),
    ("FY2025Q3", "0001751008-25-000079", "2025-11-05", "2025-11-05T21:07:48.000Z", "2025-11-05 16:07:48 EST", "0001751008-25-000081"),
    ("FY2025Q4", "0001751008-26-000005", "2026-02-11", "2026-02-11T21:07:25.000Z", "2026-02-11 16:07:25 EST", "0001751008-26-000010"),
    ("FY2026Q1", "0001751008-26-000042", "2026-05-06", "2026-05-06T20:07:59.000Z", "2026-05-06 16:07:59 EDT", "0001751008-26-000044"),
    ("FY2026Q2", "0001751008-26-000057", "2026-08-05", "2026-08-05T20:06:12.000Z", "2026-08-05 16:06:12 EDT", "0001751008-26-000059"),
]

# period, accession number, EDGAR filing date, acceptanceDateTime, US Eastern time, press-release dateline
PDD_EXPECTED = [
    ("FY2023Q3", "0001104659-23-121457", "2023-11-28", "2023-11-28T13:55:08.000Z", "2023-11-28 08:55:08 EST", "2023-11-28"),
    ("FY2023Q4", "0001104659-24-036850", "2024-03-21", "2024-03-21T01:30:13.000Z", "2024-03-20 21:30:13 EDT", "2024-03-20"),
    ("FY2024Q1", "0001104659-24-064321", "2024-05-22", "2024-05-22T21:00:30.000Z", "2024-05-22 17:00:30 EDT", "2024-05-22"),
    ("FY2024Q2", "0001104659-24-092851", "2024-08-26", "2024-08-26T20:01:51.000Z", "2024-08-26 16:01:51 EDT", "2024-08-26"),
    ("FY2024Q3", "0001104659-24-121500", "2024-11-21", "2024-11-21T21:01:16.000Z", "2024-11-21 16:01:16 EST", "2024-11-21"),
    ("FY2024Q4", "0001104659-25-026115", "2025-03-20", "2025-03-20T20:01:31.000Z", "2025-03-20 16:01:31 EDT", "2025-03-20"),
    ("FY2025Q1", "0001104659-25-053013", "2025-05-27", "2025-05-27T20:31:12.000Z", "2025-05-27 16:31:12 EDT", "2025-05-27"),
    ("FY2025Q2", "0001104659-25-082502", "2025-08-25", "2025-08-25T20:00:55.000Z", "2025-08-25 16:00:55 EDT", "2025-08-25"),
    ("FY2025Q3", "0001104659-25-113490", "2025-11-18", "2025-11-18T14:27:36.000Z", "2025-11-18 09:27:36 EST", "2025-11-18"),
    ("FY2025Q4", "0001104659-26-034813", "2026-03-26", "2026-03-26T10:09:05.000Z", "2026-03-26 06:09:05 EDT", "2026-03-25"),
    ("FY2026Q1", "0001104659-26-067186", "2026-05-28", "2026-05-28T10:20:20.000Z", "2026-05-28 06:20:20 EDT", "2026-05-27"),
    ("FY2026Q2", "0001104659-26-100534", "2026-08-25", "2026-08-25T11:20:02.000Z", "2026-08-25 07:20:02 EDT", "2026-08-24"),
]
PDD_NOT_RESULTS = {"0001104659-23-121637", "0001104659-24-123667", "0001104659-24-130517", "0001104659-25-069796",
                   "0001104659-25-113965", "0001104659-25-122765", "0001104659-25-122766", "0001104659-26-099679"}


def et_text(event: edgar.EarningsEvent) -> str:
    return event.filing.accepted_et.strftime("%Y-%m-%d %H:%M:%S %Z")


def test_app_release_history_matches_phase_0(client):
    events = edgar.release_history(APP_CIK, "12-31", filer_type=edgar.DOMESTIC, as_of=AS_OF, client=client)
    got = [(e.period, e.accession, e.filing.filing_date.isoformat(), e.filing.acceptance_datetime, et_text(e),
            e.closing.accession) for e in events]
    assert got == APP_EXPECTED
    by_period = {e.period: e for e in events}
    assert all(e.form == "8-K" and "2.02" in e.filing.items for e in events)
    # the 8-K also has Item 5.02: the report date (11-03) is the date of another event, so the release date is
    # the acceptance date
    assert by_period["FY2023Q3"].release_date == dt.date(2023, 11, 8)
    # a 10-Q accepted the same day closes the event; the 10-K came two weeks later, so the event closes after
    # 5 business days (2025-02-17 is Presidents' Day)
    assert (by_period["FY2026Q2"].closes_on, by_period["FY2026Q2"].closed_by) == (dt.date(2026, 8, 5), "10-Q")
    assert (by_period["FY2024Q4"].closes_on, by_period["FY2024Q4"].closed_by) == (dt.date(2025, 2, 20), "T+5")
    assert by_period["FY2026Q2"].to_dict()["acceptance_et"] == "2026-08-05T16:06:12-04:00"
    only_q3 = edgar.release_history(APP_CIK, "12-31", as_of=AS_OF, quarter=3, client=client)
    assert [e.period for e in only_q3] == ["FY2023Q3", "FY2024Q3", "FY2025Q3"]


def test_pdd_release_history_matches_phase_0(client):
    events = edgar.release_history(PDD_CIK, "12-31", filer_type=edgar.FOREIGN, as_of=AS_OF, client=client)
    got = [(e.period, e.accession, e.filing.filing_date.isoformat(), e.filing.acceptance_datetime, et_text(e),
            e.dateline.isoformat()) for e in events]
    assert got == PDD_EXPECTED
    assert not {e.accession for e in events} & PDD_NOT_RESULTS
    for event in events:
        assert event.form == "6-K" and event.exhibit.endswith("_ex99-1.htm")
        assert event.title.startswith("PDD Holdings Announces") and event.title.endswith("Unaudited Financial Results")
        # the release date is the earlier of the dateline and the acceptance date (US Eastern); the three 6-Ks of
        # 2026 were all accepted the next morning
        assert event.release_date == min(event.dateline, event.filing.accepted_et.date())
    assert [e.release_basis for e in events][-3:] == ["dateline"] * 3
    # accepted 2024-03-20 21:30 EDT: EDGAR's filing date rolls over to the 21st, the release date stays the 20th
    assert events[1].release_date == dt.date(2024, 3, 20) and events[1].filing.filing_date == dt.date(2024, 3, 21)


def test_filer_type_is_inferred_when_not_given(client):
    assert edgar.infer_filer_type(edgar.submissions(APP_CIK, client=client)) == edgar.DOMESTIC
    assert edgar.infer_filer_type(edgar.submissions(PDD_CIK, client=client)) == edgar.FOREIGN
    events = edgar.release_history(PDD_CIK, as_of=AS_OF, client=client)  # fiscal year end from EDGAR (1231)
    assert len(events) == 12


@pytest.mark.skipif(not WORKSPACE_HISTORY.is_file(), reason="no inputs/edgar/release_history.yml in the workspace (CI)")
def test_release_history_matches_the_workspace_file_field_by_field(client):
    phase0 = yaml.safe_load(WORKSPACE_HISTORY.read_text(encoding="utf-8"))["companies"]
    for ticker, cik, kind in (("APP", APP_CIK, edgar.DOMESTIC), ("PDD", PDD_CIK, edgar.FOREIGN)):
        events = {e.accession: e for e in edgar.release_history(cik, "12-31", filer_type=kind, as_of=AS_OF, client=client)}
        rows = phase0[ticker]["releases"]
        assert len(rows) == len(events) == 12
        for row in rows:
            event = events[row["accession"]]
            f = event.filing
            assert (event.period, event.period_end.isoformat(), f.form) == (row["fiscal_quarter"], row["period_end"], row["form"])
            assert (f.filing_date.isoformat(), f.acceptance_datetime) == (row["filing_date"], row["acceptance_datetime"])
            assert et_text(event) == row["acceptance_et"]
            assert (f.report_date.isoformat(), f.primary_document) == (row["report_date"], row["primary_document"])
            if "periodic_report" in row:
                closing = row["periodic_report"]
                assert (event.closing.form, event.closing.accession, event.closing.filing_date.isoformat(),
                        event.closing.acceptance_datetime) == (closing["form"], closing["accession"],
                                                               closing["filing_date"], closing["acceptance_datetime"])
                assert ",".join(f.items) == row["items"]
            if "exhibit_99_1" in row:
                name, title = row["exhibit_99_1"].split(": ", 1)
                assert (event.exhibit, event.title) == (name, title.strip('"'))
                assert event.dateline == edgar._first_date(row["release_dateline"])


# ---------------------------------------------------------------------------------------------------------------
# Fiscal years, business days, deadlines
# ---------------------------------------------------------------------------------------------------------------


def test_fiscal_calendar_follows_the_company_year():
    msft = edgar.FiscalCalendar.parse("06-30")
    assert msft == edgar.FiscalCalendar.parse("0630")
    assert msft.quarter_end(2027, 1) == dt.date(2026, 9, 30)
    assert msft.quarter_end(2026, 3) == dt.date(2026, 3, 31)
    assert msft.quarter_label(dt.date(2026, 9, 30)) == "FY2027Q1"  # the 15A example: MSFT's quarter ending in September
    assert msft.quarter_label(dt.date(2026, 6, 30)) == "FY2026Q4" and msft.annual_label(dt.date(2026, 6, 30)) == "FY2026"
    assert msft.quarter_label(dt.date(2026, 6, 27)) == "FY2026Q4"  # quarter end in a 52/53-week fiscal year
    assert msft.quarter_label(dt.date(2026, 1, 2)) == "FY2026Q2"
    assert msft.last_quarter_end_before(dt.date(2026, 7, 29)) == dt.date(2026, 6, 30)
    app = edgar.FiscalCalendar.parse("12-31")
    assert app.quarter_label(dt.date(2026, 9, 30)) == "FY2026Q3"
    assert app.last_quarter_end_before(dt.date(2026, 3, 31)) == dt.date(2025, 12, 31)
    with pytest.raises(ValueError):
        app.period_of(dt.date(2026, 8, 31))
    with pytest.raises(ValueError):
        edgar.FiscalCalendar.parse("June")


def test_business_days_skip_weekends_and_federal_holidays():
    closed = [dt.date(2026, 7, 3),  # Independence Day (July 4 is a Saturday)
              dt.date(2021, 12, 31),  # New Year's Day 2022 is a Saturday, observed on December 31, 2021
              dt.date(2026, 6, 19), dt.date(2026, 10, 12), dt.date(2026, 11, 11), dt.date(2026, 11, 26),
              dt.date(2025, 1, 9),  # national day of mourning for President Carter; federal government closed
              dt.date(2026, 11, 14), dt.date(2026, 11, 15)]
    assert not any(edgar.is_business_day(d) for d in closed)
    assert edgar.is_business_day(dt.date(2020, 6, 19))  # Juneteenth became a federal holiday only in 2021
    assert edgar.is_business_day(dt.date(2026, 11, 3))  # Election Day is not a federal holiday
    assert edgar.add_business_days(dt.date(2025, 2, 12), 5) == dt.date(2025, 2, 20)  # across Presidents' Day
    assert edgar.previous_business_day(dt.date(2026, 11, 15)) == dt.date(2026, 11, 13)
    assert edgar.previous_business_day(dt.date(2026, 11, 26)) == dt.date(2026, 11, 25)


@pytest.mark.parametrize("release, deadline, merge_time, crosses_dst_change", [
    # daylight saving time ends on 2026-11-01: the deadline is in standard time and the merge-by time still in
    # daylight time, 72 actual hours apart rather than exactly 3 days
    ("2026-11-02", "2026-11-01T23:59:59-05:00", "2026-10-30T00:59:59-04:00", True),
    ("2026-11-04", "2026-11-03T23:59:59-05:00", "2026-11-01T00:59:59-04:00", True),
    # daylight saving time starts on 2026-03-08: the deadline is in daylight time and the merge-by time still in
    # standard time
    ("2026-03-09", "2026-03-08T23:59:59-04:00", "2026-03-05T22:59:59-05:00", True),
    ("2026-03-08", "2026-03-07T23:59:59-05:00", "2026-03-04T23:59:59-05:00", False),
    ("2026-08-05", "2026-08-04T23:59:59-04:00", "2026-08-01T23:59:59-04:00", False),
    ("2026-02-11", "2026-02-10T23:59:59-05:00", "2026-02-07T23:59:59-05:00", False),
    ("2026-11-13", "2026-11-12T23:59:59-05:00", "2026-11-09T23:59:59-05:00", False),
])
def test_deadline_is_the_end_of_the_previous_day_and_merge_by_is_72_real_hours_earlier(release, deadline, merge_time,
                                                                                       crosses_dst_change):
    due = edgar.prereg_deadline(dt.date.fromisoformat(release))
    earlier = edgar.merge_by(due)
    assert edgar.iso(due) == deadline and edgar.iso(earlier) == merge_time
    utc = dt.timezone.utc
    assert (due.astimezone(utc) - earlier.astimezone(utc)).total_seconds() == 72 * 3600
    # arithmetic on datetimes in one ZoneInfo uses wall-clock time and is an hour off across a daylight-saving
    # change, so merge_by converts to UTC first
    assert (edgar.iso(due - dt.timedelta(hours=72)) != merge_time) is crosses_dst_change
    assert due.astimezone(edgar.NY).date() == dt.date.fromisoformat(release) - dt.timedelta(days=1)
    with pytest.raises(ValueError):
        edgar.merge_by(due.replace(tzinfo=None))


def test_acceptance_times_are_utc_and_edgar_header_times_are_eastern():
    utc = edgar.parse_acceptance("2026-08-05T20:06:12.000Z")
    assert utc == dt.datetime(2026, 8, 5, 20, 6, 12, tzinfo=dt.timezone.utc)
    # the SGML header and Accepted on the index page are US Eastern time
    assert edgar.parse_acceptance("20260805160612") == utc
    assert edgar.iso(utc.astimezone(edgar.NY)) == "2026-08-05T16:06:12-04:00"
    assert edgar.parse_acceptance("") is None


# ---------------------------------------------------------------------------------------------------------------
# Next release date (15A)
# ---------------------------------------------------------------------------------------------------------------


def synthetic_event(period: str, release: str, period_end: str) -> edgar.EarningsEvent:
    filing = edgar.Filing(cik=APP_CIK, accession="0000000000-00-000001", form="8-K",
                          filing_date=dt.date.fromisoformat(release))
    return edgar.EarningsEvent(cik=APP_CIK, period=period, period_end=dt.date.fromisoformat(period_end),
                               filing=filing, release_date=dt.date.fromisoformat(release), release_basis="acceptance")


def test_estimate_takes_the_earliest_month_day_minus_three_days_on_a_business_day():
    history = [synthetic_event("FY2024Q3", "2024-11-06", "2024-09-30"),
               synthetic_event("FY2025Q3", "2025-11-29", "2025-09-30")]
    day, basis, window = edgar.estimate_release(2026, dt.date(2026, 9, 30), history, None)
    assert day == dt.date(2026, 11, 3) and window == (dt.date(2026, 11, 6), dt.date(2026, 11, 29))
    assert "FY2024Q3" in basis
    day, _, _ = edgar.estimate_release(2026, dt.date(2026, 9, 30), history[1:], None)
    assert day == dt.date(2026, 11, 25)  # 3 days before 11-29 is Thanksgiving, so one more day back
    q4 = [synthetic_event("FY2025Q4", "2026-02-11", "2025-12-31")]  # the fourth quarter is released in the next year
    assert edgar.estimate_release(2026, dt.date(2026, 12, 31), q4, None)[0] == dt.date(2027, 2, 8)
    msft = [synthetic_event("FY2026Q2", "2026-01-28", "2025-12-31")]
    assert edgar.estimate_release(2027, dt.date(2026, 12, 31), msft, None)[0] == dt.date(2027, 1, 25)


def test_estimate_without_history_uses_the_latest_lag():
    latest = synthetic_event("FY2026Q2", "2026-08-05", "2026-06-30")  # 36 days after the quarter end
    day, basis, window = edgar.estimate_release(2026, dt.date(2026, 9, 30), [], latest)
    assert (day, window) == (dt.date(2026, 11, 2), None) and "36" in basis
    with pytest.raises(edgar.EdgarDataError):
        edgar.estimate_release(2026, dt.date(2026, 9, 30), [], None)


def test_next_release_for_app_and_pdd_fy2026q3(client):
    today = dt.date(2026, 9, 25)
    app = edgar.next_release(APP_CIK, "FY2026Q3", fiscal_year_end="12-31", filer_type=edgar.DOMESTIC, as_of=today,
                             client=client)
    assert (app.status, app.form, app.placeholder, app.period_end) == ("estimated", "8-K", True, dt.date(2026, 9, 30))
    assert [e.period for e in app.history] == ["FY2023Q3", "FY2024Q3", "FY2025Q3"]
    assert app.history_window == (dt.date(2026, 11, 5), dt.date(2026, 11, 8))
    assert app.expected_release == dt.date(2026, 11, 2)
    assert edgar.iso(app.deadline) == "2026-11-01T23:59:59-05:00"
    assert edgar.iso(app.merge_by) == "2026-10-30T00:59:59-04:00"
    assert app.prereg_header() == {"event": {"period": "FY2026Q3", "expected_release": "2026-11-02", "form": "8-K",
                                             "placeholder": True}, "deadline": "2026-11-01T23:59:59-05:00"}

    pdd = edgar.next_release(PDD_CIK, "FY2026Q3", fiscal_year_end="12-31", filer_type=edgar.FOREIGN, as_of=today,
                             client=client)
    assert (pdd.form, pdd.placeholder, pdd.history_window) == ("6-K", True, (dt.date(2026, 11, 18), dt.date(2026, 11, 28)))
    assert pdd.expected_release == dt.date(2026, 11, 13)  # 3 days before 11-18 is a Sunday, so back to Friday
    assert (edgar.iso(pdd.deadline), edgar.iso(pdd.merge_by)) == ("2026-11-12T23:59:59-05:00", "2026-11-09T23:59:59-05:00")
    assert not app.notes and not pdd.notes


def test_an_announced_date_or_window_replaces_the_estimate(client):
    today = dt.date(2026, 9, 25)
    kwargs = dict(fiscal_year_end="12-31", as_of=today, client=client)
    announced = edgar.next_release(APP_CIK, "FY2026Q3", announced="2026-11-04", **kwargs)
    assert (announced.status, announced.placeholder, announced.expected_release) == ("announced", False, dt.date(2026, 11, 4))
    assert edgar.iso(announced.deadline) == "2026-11-03T23:59:59-05:00"
    assert edgar.iso(announced.merge_by) == "2026-11-01T00:59:59-04:00"
    # the past-years window is still given, for checking
    assert announced.history_window == (dt.date(2026, 11, 5), dt.date(2026, 11, 8))
    beijing = edgar.next_release(PDD_CIK, "FY2026Q3", announced="2026-11-18T07:30:00+08:00", **kwargs)
    assert beijing.expected_release == dt.date(2026, 11, 17)  # morning in Beijing is the previous evening in US Eastern
    window = edgar.next_release(PDD_CIK, "FY2026Q3", window=("2026-11-07", "2026-11-12"), **kwargs)
    assert (window.status, window.expected_release, window.placeholder) == ("announced_window", dt.date(2026, 11, 9), True)
    with pytest.raises(ValueError):
        edgar.next_release(PDD_CIK, "FY2026Q3", window=("2026-11-14", "2026-11-15"), **kwargs)


def test_next_release_reports_a_period_that_is_already_out(client):
    done = edgar.next_release(APP_CIK, "FY2026Q2", fiscal_year_end="12-31", as_of=dt.date(2026, 9, 25), client=client)
    assert (done.status, done.expected_release, done.placeholder) == ("released", dt.date(2026, 8, 5), False)
    assert done.event.accession == "0001751008-26-000057"
    with pytest.raises(ValueError):
        edgar.next_release(APP_CIK, "FY2026", client=client)


def test_prereg_header_passes_the_thesis_ci_schema_and_timing_rule(client):
    contract = pytest.importorskip("thesis_ci.contract")
    archive = pytest.importorskip("thesis_ci.checks.archive")
    estimate = edgar.next_release(APP_CIK, "FY2026Q3", fiscal_year_end="12-31", as_of=dt.date(2026, 9, 25),
                                  client=client)
    doc = {"company": "APP", **estimate.prereg_header(), "author": "system", "horizon": "quarter", "items": [{
        "id": "APP-FY2026Q3-1", "statement": "example", "probability": 0.6, "criterion": "example",
        "data_source": "10-Q", "horizon": "quarter", "resolves_by": "2026-11-30", "domain": "digital_advertising",
        "added_by": "system"}]}
    assert not list(contract.validator("prereg").iter_errors(doc))
    deadline = dt.datetime.fromisoformat(doc["deadline"])
    assert archive.deadline_day(deadline) < estimate.expected_release


# ---------------------------------------------------------------------------------------------------------------
# Company -> CIK
# ---------------------------------------------------------------------------------------------------------------


def test_filer_comes_from_thesis_yml():
    app, pdd = edgar.load_filer("APP"), edgar.load_filer("pdd")
    assert (app.cik, app.type, app.fiscal_year_end, app.earnings_form) == (APP_CIK, "domestic", "12-31", "8-K")
    assert (pdd.cik, pdd.type, pdd.earnings_form) == (PDD_CIK, "foreign_private_issuer", "6-K")
    assert edgar.load_filer("MSFT").fiscal_year_end == "06-30"
    assert edgar.load_filer("789019").cik == "0000789019"
    with pytest.raises(ValueError):
        edgar.load_filer("NOPE")


# ---------------------------------------------------------------------------------------------------------------
# Accession-number check for sources tables
# ---------------------------------------------------------------------------------------------------------------

APP_SOURCES = """\
# sources table for tests (comments must be kept)
sources:
  - tag: APP-RPT1-2026-09-23
    kind: report
    title: "Own report"
    date: 2026-09-23

  - tag: APP-10Q-FY2026Q2
    kind: filing
    title: "10-Q: quarter ended 2026-06-30"
    issuer_cik: "0001751008"
    form: 10-Q
    period: FY2026Q2
    accession: 0001751008-26-000059
    filed: 2026-08-05
    url: https://www.sec.gov/Archives/edgar/data/1751008/000175100826000059/0001751008-26-000059-index.htm
    primary: true

  - tag: APP-8K-2026-05-06
    kind: filing
    title: "8-K: first-quarter results"
    issuer_cik: "0001751008"
    form: 8-K
    period: FY2026Q1
    accession: 0001751008-26-000042
    filed: 2026-05-07  # deliberately wrong
    primary: true

  - tag: APP-8K-2026-02-11
    kind: filing
    title: "8-K: fourth-quarter results (accession number to be filled)"
    issuer_cik: "0001751008"
    accession: null
    primary: true
    note: "a note on
      two lines"

  - tag: APP-10K-FY2024
    kind: filing
    title: "10-K: FY2024 (no issuer_cik; the tag names this company)"
    form: 10-K
    primary: true

  - tag: APP-8K-2025-02-12-2
    kind: filing
    title: "second 8-K of the same day"
    issuer_cik: "0001751008"
    form: 8-K

  - tag: APP-8K-2026-04-06
    kind: filing
    title: "the tag's date is not the EDGAR filing date"
    issuer_cik: "0001751008"
    form: 8-K
    accession: 0001751008-26-000014
    filed: 2026-04-07

  - tag: APP-10Q-FY2026Q3
    kind: filing
    title: "accession number does not exist"
    issuer_cik: "0001751008"
    form: 10-Q
    accession: 0001751008-26-999999

  - tag: U-8K-2026-08-06
    kind: filing
    title: "third-party filing, no issuer_cik"
    form: 8-K
"""

PDD_SOURCES = """\
sources:
  - tag: PDD-6K-2026-08-25
    kind: filing
    title: results 6-K
    issuer_cik: "0001737806"
    form: 6-K
    accession: 0001104659-26-100534
    filed: 2026-08-25

  - tag: PDD-6K-2025-12-19-2
    kind: filing
    title: second 6-K of the same day
    issuer_cik: "0001737806"
    form: 6-K
    period: null

  - tag: PDD-20F-FY2025
    kind: filing
    title: annual report
    issuer_cik: "0001737806"
    form: 20-F
    period: FY2025
    accession: 0001104659-26-050727
    filed: 2026-04-29
"""


def write_sources(tmp_path: Path, ticker: str, text: str) -> Path:
    path = tmp_path / "repo" / "companies" / ticker / "sources.yml"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_check_sources_reports_without_writing(tmp_path, client):
    path = write_sources(tmp_path, "APP", APP_SOURCES)
    report = edgar.check_sources(path, client=client)
    assert path.read_text(encoding="utf-8") == APP_SOURCES  # read-only
    status = {f.tag: f.status for f in report.findings}
    assert "APP-RPT1-2026-09-23" not in status
    assert status == {
        "APP-10Q-FY2026Q2": "ok", "APP-8K-2026-05-06": "mismatch", "APP-8K-2026-02-11": "missing",
        "APP-10K-FY2024": "missing", "APP-8K-2025-02-12-2": "missing", "APP-8K-2026-04-06": "mismatch",
        "APP-10Q-FY2026Q3": "mismatch", "U-8K-2026-08-06": "unresolved",
    }
    found = {f.tag: f for f in report.findings}
    assert found["APP-8K-2026-05-06"].problems == ["filed says 2026-05-07; EDGAR's filing date is 2026-05-06"]
    assert found["APP-8K-2026-02-11"].fills == {"accession": "0001751008-26-000005", "form": "8-K",
                                                "filed": dt.date(2026, 2, 11), "period": "FY2025Q4"}
    assert found["APP-10K-FY2024"].fills == {"accession": "0001751008-25-000018", "filed": dt.date(2025, 2, 27),
                                             "period": "FY2024"}
    assert found["APP-8K-2025-02-12-2"].fills == {"accession": "0001751008-25-000008", "filed": dt.date(2025, 2, 12)}
    assert "...-2026-04-07" in found["APP-8K-2026-04-06"].problems[0]
    assert "0001751008-26-999999" in found["APP-10Q-FY2026Q3"].problems[0]
    assert found["APP-8K-2026-05-06"].line == 19
    assert report.counts() == {"ok": 1, "missing": 3, "mismatch": 3, "unresolved": 1}


def test_check_sources_write_fills_only_missing_fields(tmp_path, client):
    path = write_sources(tmp_path, "APP", APP_SOURCES)
    os.chmod(path, 0o640)
    report = edgar.check_sources(path, client=client, write=True)
    text = path.read_text(encoding="utf-8")
    assert report.written and report.diff.count("\n+    ") == 9
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert "# sources table for tests (comments must be kept)" in text and "# deliberately wrong" in text
    assert "    issuer_cik: \"0001751008\"\n    form: 8-K\n    period: FY2025Q4\n    accession: 0001751008-26-000005\n" \
           "    filed: 2026-02-11\n    primary: true\n    note: \"a note on\n      two lines\"\n" in text
    assert "    form: 10-K\n    period: FY2024\n    accession: 0001751008-25-000018\n    filed: 2025-02-27\n" in text
    before, after = yaml.safe_load(APP_SOURCES)["sources"], yaml.safe_load(text)["sources"]
    for old, new in zip(before, after):
        assert {k: v for k, v in new.items() if k not in old or old[k] is None} or old == new
        assert all(new[k] == v for k, v in old.items() if v is not None)  # no existing value changed
    again = {f.tag: f.status for f in edgar.check_sources(path, client=client).findings}
    assert again["APP-8K-2026-02-11"] == again["APP-10K-FY2024"] == again["APP-8K-2025-02-12-2"] == "ok"
    assert again["APP-8K-2026-05-06"] == "mismatch"  # an entry with a mismatch is neither filled nor changed


def test_check_sources_reads_6k_periods_from_the_press_release(tmp_path, client):
    path = write_sources(tmp_path, "PDD", PDD_SOURCES)
    found = {f.tag: f for f in edgar.check_sources(path, client=client).findings}
    assert found["PDD-6K-2026-08-25"].fills == {"period": "FY2026Q2"}
    assert found["PDD-6K-2025-12-19-2"].fills == {"accession": "0001104659-25-122766", "filed": dt.date(2025, 12, 19)}
    assert found["PDD-20F-FY2025"].status == "ok"
    edgar.check_sources(path, client=client, write=True)
    parsed = {s["tag"]: s for s in yaml.safe_load(path.read_text(encoding="utf-8"))["sources"]}
    assert parsed["PDD-6K-2026-08-25"]["period"] == "FY2026Q2"
    assert parsed["PDD-6K-2025-12-19-2"]["accession"] == "0001104659-25-122766"
    assert parsed["PDD-6K-2025-12-19-2"]["period"] is None


def same_day_sources(*entries: tuple[str, str]) -> str:
    lines = ["sources:"]
    for tag, accession in entries:
        lines += [f"  - tag: {tag}", "    kind: filing", "    title: 6-K", '    issuer_cik: "0001737806"',
                  "    form: 6-K", f"    accession: {accession}", "    filed: 2025-12-19"]
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize("entries, flagged", [
    # only the second filing of the day is registered: no suffix is also correct (as in the real
    # companies/PDD/sources.yml)
    ([("PDD-6K-2025-12-19", "0001104659-25-122766")], set()),
    ([("PDD-6K-2025-12-19-2", "0001104659-25-122766")], set()),
    # both registered: in accession-number order, the later one takes -2
    ([("PDD-6K-2025-12-19", "0001104659-25-122765"), ("PDD-6K-2025-12-19-2", "0001104659-25-122766")], set()),
    ([("PDD-6K-2025-12-19-2", "0001104659-25-122765"), ("PDD-6K-2025-12-19", "0001104659-25-122766")],
     {"PDD-6K-2025-12-19-2", "PDD-6K-2025-12-19"}),
])
def test_same_day_suffix_is_needed_only_when_both_filings_are_registered(tmp_path, client, entries, flagged):
    path = write_sources(tmp_path, "PDD", same_day_sources(*entries))
    report = edgar.check_sources(path, client=client)
    assert {f.tag for f in report.findings if f.problems} == flagged
    for finding in report.findings:
        assert all("by accession number" in problem for problem in finding.problems)


def test_fill_never_overwrites_an_existing_value():
    text = "sources:\n  - tag: APP-10K-FY2025\n    kind: filing\n    accession: 0001751008-26-000010\n"
    with pytest.raises(edgar.EdgarDataError, match="not overwritten"):
        edgar.fill_missing_fields(text, {"APP-10K-FY2025": {"accession": "0001751008-26-000011"}})
    with pytest.raises(ValueError):
        edgar.fill_missing_fields(text, {"APP-10K-FY2025": {"title": "x"}})
    filled = edgar.fill_missing_fields(text, {"APP-10K-FY2025": {"form": "3"}})
    assert yaml.safe_load(filled)["sources"][0]["form"] == "3"  # an all-digit form must be quoted


@pytest.mark.parametrize("tag, expected", [
    ("APP-10K-FY2025", ("APP", "10K", "FY2025", None, 1)),
    ("AXP-10Q-FY2026Q2", ("AXP", "10Q", "FY2026Q2", None, 1)),
    ("PDD-6K-2025-12-19-2", ("PDD", "6K", None, dt.date(2025, 12, 19), 2)),
    ("APP-FORM3-2026-07-02", ("APP", "FORM3", None, dt.date(2026, 7, 2), 1)),
    ("U-8K-2026-08-06", ("U", "8K", None, dt.date(2026, 8, 6), 1)),
])
def test_source_tags_follow_spec_3_3(tag, expected):
    parsed = edgar.parse_source_tag(tag)
    assert (parsed.ticker, parsed.form, parsed.period, parsed.date, parsed.ordinal) == expected


def test_form_keys_compare_tag_and_edgar_spellings():
    assert edgar.form_key("DEF 14A") == edgar.form_key("DEF14A") == "DEF14A"
    assert edgar.form_key("8-K/A") == "8KA" and edgar.form_key("FORM3") == edgar.form_key("3") == "3"
    assert edgar.form_key("SCHEDULE 13G/A") == edgar.form_key("SC 13G/A")
    assert edgar.parse_source_tag("not a tag") is None


def test_check_sources_command_line(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(edgar.UA_ENV, FAKE_UA)
    transport = FixtureTransport()
    monkeypatch.setattr(edgar, "UrllibTransport", lambda: transport)
    path = write_sources(tmp_path, "APP", APP_SOURCES)
    cache = ["--cache-dir", str(tmp_path / "cli-cache")]
    assert edgar.main(["check-sources", str(path), *cache]) == 1
    out = capsys.readouterr().out
    assert "mismatch" in out and "fillable" in out and "--write" in out
    assert edgar.main(["check-sources", str(path), "--json", *cache]) == 1
    assert json.loads(capsys.readouterr().out)["counts"]["missing"] == 3
    assert edgar.main(["next-release", "APP", "FY2026Q3", "--as-of", "2026-09-25", *cache]) == 0
    printed = yaml.safe_load(capsys.readouterr().out)
    assert printed["prereg"]["deadline"] == "2026-11-01T23:59:59-05:00" and printed["merge_by"] == "2026-10-30T00:59:59-04:00"


# ---------------------------------------------------------------------------------------------------------------
# Hard rules
# ---------------------------------------------------------------------------------------------------------------


def test_module_imports_no_model_sdk_and_passes_the_code_checks():
    source = Path(edgar.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names}
    imported |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert imported <= {"__future__", "argparse", "calendar", "dataclasses", "datetime", "difflib", "functools", "gzip",
                        "hashlib", "html", "http", "json", "logging", "os", "random", "re", "ssl", "sys", "tempfile",
                        "threading", "time", "urllib", "zlib", "collections", "email", "pathlib", "typing", "zoneinfo",
                        "yaml", "certifi"}
    code = pytest.importorskip("thesis_ci.checks.code")
    lowered = source.lower()
    assert not [phrase for phrase in code.PRICE_FEEDS if phrase in lowered]
    assert not [name for _, name in code.python_imports(source) if code.LLM_SDK_RE.match(name)]
