"""SEC EDGAR access (STATUS T18; docs/decisions/0013, 0017).

Reads only public EDGAR data: submissions (including the continuation pages in filings.files), the file index
index.json, primary documents and EX-99.x exhibits, and SEC's ticker table company_tickers.json. From these it derives
earnings events, release_history, the estimated next release date and the pre-registration deadline, it checks the
accession numbers in sources tables, and it finds the filing a source tag names, of any issuer.

- **User-Agent:** the SEC_USER_AGENT environment variable; if it is not set, .env is read (by default in the
  workspace root, i.e. the parent directory of this repo; override with OWNERS_OFFICE_ENV_FILE or --env-file). If
  neither has it, network access is refused. The value is never printed, logged, cached or written to any file, and
  it never appears in error messages: this module's exceptions and logs always replace it with [SEC_USER_AGENT]
  first (decisions/0013).
- **Request rate:** all requests in one process share one rate limiter, at most 5 per second (the SEC limit is 10);
  429 and 5xx are retried with exponential backoff, honoring Retry-After; every request has a timeout. Only the two
  SEC hosts are accessed (data.sec.gov, www.sec.gov).
- **TLS:** SSL_CERT_FILE > certifi > system default > common system certificate files (the python.org macOS build of
  Python ships without CA certificates).
- **Cache:** default ~/.cache/owners-office/edgar (outside all three repos; override with OWNERS_OFFICE_EDGAR_CACHE
  or --cache-dir). Files under Archives never change once accepted, so they are cached permanently; submissions are
  re-fetched after one hour by default. The cache holds only SEC response bodies and fetch times.
- **Earnings events:** for a domestic issuer, an 8-K with Item 2.02, closed when the 10-Q/10-K is filed or after 5
  business days, whichever comes first; for a foreign issuer (PDD), an earnings 6-K, recognized by the title of the
  press release in exhibit EX-99.1. Periods are written FY<year>Q<quarter> in the company's own fiscal year.
- **Next release date:** estimated by the rules of prompt 15A (announced date > announced window >
  release_history > lag of the latest period); the deadline is 23:59:59 US Eastern on the day before the release
  date, and the merge-by time is 72 actual elapsed hours before the deadline.
- This module connects to no price feed (C-NO-PRICE-FEED) and calls no model (C-LLM-ENTRY).

Command line (from the repo root):

    python -m pipeline.edgar release-history APP
    python -m pipeline.edgar next-release APP FY2026Q3 [--announced 2026-11-05]
    python -m pipeline.edgar check-sources companies/APP/sources.yml [--write]
    python -m pipeline.edgar documents APP 0001751008-26-000057 [--download DIR]
    python -m pipeline.edgar submissions APP
"""

from __future__ import annotations

import argparse
import calendar
import dataclasses
import datetime as dt
import difflib
import functools
import gzip
import hashlib
import html
import http.client
import json
import logging
import os
import random
import re
import ssl
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections.abc import Callable, Iterator, Mapping, Sequence
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = REPO_ROOT.parent

UA_ENV = "SEC_USER_AGENT"
ENV_FILE_ENV = "OWNERS_OFFICE_ENV_FILE"  # location of .env; default: the workspace root
CACHE_ENV = "OWNERS_OFFICE_EDGAR_CACHE"  # cache directory; default: ~/.cache/owners-office/edgar
DEFAULT_ENV_FILE = WORKSPACE_ROOT / ".env"

DATA_BASE = "https://data.sec.gov"
ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"
ALLOWED_HOSTS = frozenset({"data.sec.gov", "www.sec.gov"})

MAX_REQUESTS_PER_SECOND = 5.0  # this project's limit; the SEC fair-access limit is 10
DEFAULT_TIMEOUT = 30.0
DEFAULT_ATTEMPTS = 5
DEFAULT_BACKOFF = 1.0
MAX_BACKOFF = 60.0
SUBMISSIONS_TTL = 3600.0  # submissions are cached for one hour; files under Archives are cached permanently

NY = ZoneInfo("America/New_York")
UTC = dt.timezone.utc

MERGE_HOURS = 72  # pre-registrations are merged at least 72 hours before the deadline (15A)
ESTIMATE_LEAD_DAYS = 3  # without an announced date: the earliest month-day of past years minus 3 calendar days (15A)
HISTORY_YEARS = 3  # release_history: the same fiscal quarter in the past three years

DOMESTIC = "domestic"
FOREIGN = "foreign_private_issuer"
ANNUAL_FORMS = frozenset({"10-K", "10-K/A", "10-KT", "10-KT/A", "20-F", "20-F/A", "40-F", "40-F/A"})
QUARTERLY_FORMS = frozenset({"10-Q", "10-Q/A", "10-QT", "10-QT/A"})
PERIODIC_FORMS = ANNUAL_FORMS | QUARTERLY_FORMS
CLOSING_FORMS = frozenset({"10-Q", "10-K"})  # filings that close a domestic earnings event (amendments excluded)
FINAL_PROSPECTUS_FORMS = frozenset({"424B1", "424B4"})  # an offering's final prospectus (Rule 424(b)(1), (4))
REGISTRATION_FORMS = frozenset({"S-1", "S-1/A", "F-1", "F-1/A"})  # its registration statement, before the final one
PROSPECTUS_FORMS = FINAL_PROSPECTUS_FORMS | REGISTRATION_FORMS
SYSTEM_CA_BUNDLES = (
    "/etc/ssl/cert.pem",  # macOS, Alpine
    "/etc/ssl/certs/ca-certificates.crt",  # Debian, Ubuntu
    "/etc/pki/tls/certs/ca-bundle.crt",  # RHEL, Fedora
)

ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")
PERIOD_RE = re.compile(r"^FY(\d{4})(?:Q([1-4]))?$")
_OVERFLOW_PAGE_RE = re.compile(r"^CIK\d{10}-submissions-\d{3}\.json$")
_DOC_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_R_PAGE_RE = re.compile(r"^R\d+\.htm$", re.I)  # pages generated by the XBRL viewer; not downloaded
_EX99_RE = re.compile(r"(?i)(?:ex|exhibit)[-_ ]?99(?:[-_.d](\d{1,2})|(\d))?(?!\d)")  # ex99-1, ex991, ex99d1, ...

LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------------------------------------------
# User-Agent: kept in memory only; always replaced with a placeholder in logs and exceptions
# ---------------------------------------------------------------------------------------------------------------

REDACTED = "[SEC_USER_AGENT]"
_SENSITIVE: set[str] = set()
_SENSITIVE_LOCK = threading.Lock()
_EMAIL_RE = re.compile(r"[^\s<>()\"',;]+@[^\s<>()\"',;]+")


def _remember_sensitive(value: str) -> None:
    """Remember values to remove from output: the whole User-Agent and the email addresses in it."""
    value = value.strip()
    if not value:
        return
    with _SENSITIVE_LOCK:
        _SENSITIVE.add(value)
        _SENSITIVE.update(m.group(0) for m in _EMAIL_RE.finditer(value))


def redact(text: str) -> str:
    """Replace any User-Agent this process has seen (and the email addresses in it) with the placeholder."""
    with _SENSITIVE_LOCK:
        secrets = sorted(_SENSITIVE, key=len, reverse=True)
    for secret in secrets:
        if secret in text:
            text = text.replace(secret, REDACTED)
    return text


class _RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        cleaned = redact(message)
        if cleaned != message:
            record.msg, record.args = cleaned, None
        return True


LOG.addFilter(_RedactFilter())


class EdgarError(RuntimeError):
    """EDGAR access or data error. The User-Agent is removed from the message before it is stored."""

    def __init__(self, message: str):
        super().__init__(redact(str(message)))


class MissingUserAgent(EdgarError):
    """No usable SEC_USER_AGENT: network access is refused."""


class EdgarHTTPError(EdgarError):
    def __init__(self, message: str, *, url: str, status: int | None = None):
        super().__init__(message)
        self.url = redact(url)
        self.status = status


class EdgarNotFound(EdgarHTTPError):
    """404: this URL does not exist on EDGAR."""


class EdgarAccessDenied(EdgarHTTPError):
    """403 or the SEC block page: the User-Agent is not acceptable, or requests are too fast."""


class EdgarOffline(EdgarError):
    """Offline mode, and the needed file is not in the cache."""


class EdgarDataError(EdgarError):
    """EDGAR returned something other than expected, or the data are not enough to decide."""


def _valid_user_agent(value: str) -> bool:
    """The SEC requires contact details: one line of printable ASCII that includes an email address."""
    return bool(value) and value.isascii() and value.isprintable() and "@" in value and len(value) <= 300


def _read_env_value(path: Path, key: str) -> str | None:
    """Read one key from .env (KEY=VALUE, optionally with export, quotes and a trailing comment).

    Never echoes the file contents.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise MissingUserAgent(f"cannot read {path} ({type(exc).__name__})") from None
    value: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, sep, rest = line.partition("=")
        if not sep or name.strip() != key:
            continue
        rest = rest.strip()
        if len(rest) >= 2 and rest[0] == rest[-1] and rest[0] in "\"'":
            rest = rest[1:-1]
        else:
            rest = re.split(r"\s+#", rest, maxsplit=1)[0].strip()
        value = rest  # if a key is set more than once, the last one wins (as in the shell)
    return value or None


def load_user_agent(env: Mapping[str, str] | None = None, env_file: str | os.PathLike | None = None) -> str:
    """SEC_USER_AGENT: the environment variable first, then .env. Raises MissingUserAgent if neither has it."""
    env = os.environ if env is None else env
    value = (env.get(UA_ENV) or "").strip()
    where = "the environment"
    if not value:
        path = Path(env_file) if env_file else Path(env.get(ENV_FILE_ENV) or DEFAULT_ENV_FILE)
        value = (_read_env_value(path, UA_ENV) or "").strip()
        where = str(path)
        if not value:
            raise MissingUserAgent(
                f"no SEC User-Agent: set the environment variable {UA_ENV}, or add the line "
                f"{UA_ENV}=<project name> <contact email> to {path} (docs/decisions/0013); "
                "EDGAR is not accessed without it"
            )
    _remember_sensitive(value)
    if not _valid_user_agent(value):
        raise MissingUserAgent(f"{UA_ENV} from {where} cannot be used as the User-Agent: it must be one line of "
                               "printable ASCII text that includes a contact email (value not shown)")
    return value


# ---------------------------------------------------------------------------------------------------------------
# HTTP: TLS, rate limiting, retries, cache
# ---------------------------------------------------------------------------------------------------------------


def tls_context() -> ssl.SSLContext:
    """Certificate verification is always on.

    CA certificates come from, in order: SSL_CERT_FILE, certifi, the system default, common system certificate files.
    """
    cafile = os.environ.get("SSL_CERT_FILE")
    if cafile:
        return ssl.create_default_context(cafile=cafile)
    try:
        import certifi  # in requirements.txt; falls back to the system certificates when not installed
    except ImportError:
        certifi = None
    if certifi is not None:
        return ssl.create_default_context(cafile=certifi.where())
    ctx = ssl.create_default_context()
    if not ctx.cert_store_stats().get("x509_ca"):
        capath = ssl.get_default_verify_paths().capath
        if not (capath and os.path.isdir(capath) and os.listdir(capath)):
            for bundle in SYSTEM_CA_BUNDLES:
                if os.path.isfile(bundle):
                    ctx.load_verify_locations(cafile=bundle)
                    break
    return ctx


class RateLimiter:
    """Two consecutive requests are at least 1/max_per_second seconds apart.

    No one-second span has more than max_per_second requests.
    """

    def __init__(self, max_per_second: float = MAX_REQUESTS_PER_SECOND, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        if not 0 < max_per_second <= MAX_REQUESTS_PER_SECOND:
            raise ValueError(f"the request rate must be above 0 and at most {MAX_REQUESTS_PER_SECOND:g} requests per "
                             f"second, got {max_per_second}")
        self.interval = 1.0 / max_per_second
        self._clock = clock
        self._sleep = sleep
        self._next: float | None = None
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            while True:
                now = self._clock()
                if self._next is None or now >= self._next:
                    self._next = now + self.interval
                    return
                self._sleep(self._next - now)


_SHARED_LIMITER = RateLimiter()

# (url, request headers, timeout) -> (status code, response headers, body). Network errors raise OSError or
# http.client.HTTPException.
Transport = Callable[[str, Mapping[str, str], float], "tuple[int, Mapping[str, str], bytes]"]


class UrllibTransport:
    """Transport layer on the standard library's urllib.

    HTTP errors are also returned as (status code, response headers, body), and the client handles them uniformly.
    """

    def __init__(self, context: ssl.SSLContext | None = None):
        self._opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context or tls_context()))

    def __call__(self, url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, dict[str, str], bytes]:
        request = urllib.request.Request(url, headers=dict(headers), method="GET")
        try:
            with self._opener.open(request, timeout=timeout) as response:
                return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read()
            except (OSError, http.client.HTTPException):
                body = b""
            headers_out = {k.lower(): v for k, v in exc.headers.items()} if exc.headers is not None else {}
            return exc.code, headers_out, body


def default_cache_dir() -> Path:
    if os.environ.get(CACHE_ENV):
        return Path(os.environ[CACHE_ENV]).expanduser()
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "owners-office" / "edgar"


def _decode_body(body: bytes, headers: Mapping[str, str]) -> bytes:
    encoding = (headers.get("content-encoding") or "").strip().lower()
    if encoding == "gzip":
        return gzip.decompress(body)
    if encoding == "deflate":
        try:
            return zlib.decompress(body)
        except zlib.error:
            return zlib.decompress(body, -zlib.MAX_WBITS)
    return body


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - dt.datetime.now(UTC)).total_seconds())


def _looks_like_html(body: bytes) -> bool:
    head = body[:2048].lstrip().lower()
    return head.startswith((b"<!doctype html", b"<html")) or b"<html" in head


def _validate_json(body: bytes) -> None:
    try:
        json.loads(body)
    except ValueError:
        if _looks_like_html(body):
            raise EdgarAccessDenied("EDGAR returned a web page instead of JSON (most likely the SEC block page: "
                                    "check the User-Agent and the request rate)", url="", status=None) from None
        raise EdgarDataError("EDGAR returned something that is not JSON") from None


def _is_cert_error(exc: BaseException) -> bool:
    if isinstance(exc, ssl.SSLCertVerificationError):
        return True
    return isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, ssl.SSLCertVerificationError)


def _atomic_write(path: Path, data: bytes) -> None:
    """Write a temporary file, then rename it.

    An existing file keeps its permissions; a new file gets 0644 (mkstemp defaults to 0600).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        mode = 0o644
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class EdgarClient:
    """EDGAR client with rate limiting, retries, timeouts and a disk cache.

    In online mode the constructor already requires a User-Agent (the user_agent argument, else the environment variable
    or .env) and raises MissingUserAgent if there is none. Offline mode (offline=True) reads only the cache and needs no
    User-Agent; it raises EdgarOffline when the cache does not have what is needed.
    """

    def __init__(self, *, user_agent: str | None = None, env_file: str | os.PathLike | None = None,
                 cache_dir: str | os.PathLike | None = None, offline: bool = False, refresh: bool = False,
                 ttl: float = SUBMISSIONS_TTL, timeout: float = DEFAULT_TIMEOUT, max_attempts: int = DEFAULT_ATTEMPTS,
                 backoff: float = DEFAULT_BACKOFF, jitter: float = 0.1, transport: Transport | None = None,
                 limiter: RateLimiter | None = None, sleep: Callable[[float], None] = time.sleep,
                 now: Callable[[], float] = time.time):
        self.offline = offline
        self.refresh = refresh
        self.ttl = ttl
        self.timeout = timeout
        self.max_attempts = max(1, int(max_attempts))
        self.backoff = backoff
        self.jitter = jitter
        self.cache_dir = Path(cache_dir).expanduser() if cache_dir else default_cache_dir()
        self.limiter = limiter or _SHARED_LIMITER
        self._transport = transport
        self._sleep = sleep
        self._now = now
        self._ua: str | None = None
        if user_agent is not None:
            _remember_sensitive(user_agent)
            if not _valid_user_agent(user_agent.strip()):
                raise MissingUserAgent("the User-Agent passed in cannot be used: it must be one line of printable "
                                       "ASCII text that includes a contact email (value not shown)")
            self._ua = user_agent.strip()
        elif not offline:
            self._ua = load_user_agent(env_file=env_file)

    def __repr__(self) -> str:
        return (f"EdgarClient(cache_dir={str(self.cache_dir)!r}, offline={self.offline}, "
                f"user_agent={'<set>' if self._ua else '<none>'})")

    # -- Reading -------------------------------------------------------------------------------------------------

    def get_bytes(self, url: str, *, max_age: float | None = None,
                  validate: Callable[[bytes], None] | None = None) -> bytes:
        """Fetch the body at a URL.

        max_age=None means the content never changes (Archives), so the cached copy is valid forever.
        """
        path = self._cache_path(url)
        cached = self._cache_read(path, max_age)
        if cached is not None:
            try:
                if validate:
                    validate(cached)
                LOG.debug("cache hit: %s", url)
                return cached
            except EdgarError:
                if self.offline:
                    raise
                LOG.warning("cached content is invalid, fetching again: %s", url)
        if self.offline:
            raise EdgarOffline(f"offline mode, and the cache does not have {url} (cache directory {self.cache_dir})")
        body = self._fetch(url)
        if validate:
            try:
                validate(body)
            except EdgarHTTPError as exc:
                raise type(exc)(str(exc) + f": {url}", url=url, status=exc.status) from None
        self._cache_write(path, url, body)
        return body

    def get_json(self, url: str, *, max_age: float | None = None) -> Any:
        return json.loads(self.get_bytes(url, max_age=max_age, validate=_validate_json))

    # -- Cache ---------------------------------------------------------------------------------------------------

    def _cache_path(self, url: str) -> Path:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS:
            raise EdgarError(f"this module accesses only the SEC hosts {sorted(ALLOWED_HOSTS)}, got {url}")
        segments = [s for s in parts.path.split("/") if s]
        if not segments or any(s in (".", "..") or "\\" in s or "\x00" in s for s in segments):
            raise EdgarError(f"unrecognized EDGAR URL: {url}")
        name = segments[-1]
        if parts.query:
            name += "@" + hashlib.sha256(parts.query.encode()).hexdigest()[:16]
        return self.cache_dir.joinpath(parts.hostname, *segments[:-1], name)

    @staticmethod
    def _meta_path(path: Path) -> Path:
        return path.with_name(path.name + ".meta.json")

    def _cache_read(self, path: Path, max_age: float | None) -> bytes | None:
        if not path.is_file():
            return None
        if not self.offline and max_age is not None:
            if self.refresh:
                return None
            fetched = self._fetched_at(path)
            if fetched is None or self._now() - fetched > max_age:
                return None
        return path.read_bytes()

    def _fetched_at(self, path: Path) -> float | None:
        try:
            meta = json.loads(self._meta_path(path).read_text(encoding="utf-8"))
            return dt.datetime.fromisoformat(meta["fetched_at"]).timestamp()
        except (OSError, ValueError, KeyError, TypeError):
            try:
                return path.stat().st_mtime
            except OSError:
                return None

    def _cache_write(self, path: Path, url: str, body: bytes) -> None:
        # store only the body, URL and fetch time; request headers (including the User-Agent) never go to disk
        _atomic_write(path, body)
        meta = {"url": url, "fetched_at": dt.datetime.fromtimestamp(self._now(), UTC).isoformat(timespec="seconds")}
        _atomic_write(self._meta_path(path), json.dumps(meta).encode())

    # -- Network -------------------------------------------------------------------------------------------------

    def _backoff_delay(self, attempt: int, retry_after: float | None) -> float:
        delay = retry_after if retry_after is not None else self.backoff * (2 ** (attempt - 1))
        delay = min(MAX_BACKOFF, max(0.0, delay))
        if self.jitter:
            delay *= 1 + self.jitter * random.random()
        return delay

    def _fetch(self, url: str) -> bytes:
        if not self._ua:
            raise MissingUserAgent(f"no SEC User-Agent, so EDGAR is not accessed: {url}")
        if self._transport is None:
            self._transport = UrllibTransport()
        headers = {"User-Agent": self._ua, "Accept-Encoding": "gzip, deflate"}
        last = "no response"
        last_status: int | None = None
        for attempt in range(1, self.max_attempts + 1):
            self.limiter.acquire()
            retry_after = None
            try:
                status, response_headers, body = self._transport(url, headers, self.timeout)
            except (OSError, http.client.HTTPException) as exc:
                if _is_cert_error(exc):
                    raise EdgarError(f"TLS certificate verification failed: {url}; install certifi (requirements.txt) "
                                     "or point SSL_CERT_FILE to a CA certificate file") from None
                last = f"{type(exc).__name__}: {redact(str(exc))[:200]}"
            else:
                response_headers = {str(k).lower(): v for k, v in dict(response_headers).items()}
                last_status = status
                if status == 200:
                    LOG.info("GET %s -> 200", url)
                    try:
                        return _decode_body(body, response_headers)
                    except (OSError, EOFError, zlib.error) as exc:
                        last = f"decompression failed ({type(exc).__name__})"
                elif status == 404:
                    raise EdgarNotFound(f"EDGAR has no such URL (404): {url}", url=url, status=404)
                elif status in (401, 403):
                    raise EdgarAccessDenied(
                        f"EDGAR denied access (HTTP {status}): {url}; check that {UA_ENV} gives a project name and a "
                        "contact email, and that requests are not too fast (docs/decisions/0013)",
                        url=url, status=status)
                elif status == 429 or 500 <= status < 600:
                    last = f"HTTP {status}"
                    retry_after = _retry_after(response_headers.get("retry-after"))
                else:
                    raise EdgarHTTPError(f"EDGAR returned HTTP {status}: {url}", url=url, status=status)
            if attempt < self.max_attempts:
                delay = self._backoff_delay(attempt, retry_after)
                LOG.warning("EDGAR request failed (%s); retrying in %.1f seconds (%d/%d): %s", last, delay, attempt,
                            self.max_attempts, url)
                self._sleep(delay)
        raise EdgarHTTPError(f"EDGAR request failed on all {self.max_attempts} attempts (last: {last}): {url}",
                             url=url, status=last_status)


@functools.lru_cache(maxsize=1)
def default_client() -> EdgarClient:
    """Used by library functions that are not given a client.

    The User-Agent comes from the environment variable or from .env in the workspace root.
    """
    return EdgarClient()


# ---------------------------------------------------------------------------------------------------------------
# submissions and filings
# ---------------------------------------------------------------------------------------------------------------


def normalize_cik(value: Any) -> str:
    text = str(value).strip()
    if text.upper().startswith("CIK"):
        text = text[3:]
    if not text.isdigit() or len(text) > 10:
        raise ValueError(f"a CIK must be at most 10 digits, got {value!r}")
    return text.zfill(10)


def normalize_accession(value: Any) -> str:
    text = str(value).strip()
    if re.fullmatch(r"\d{18}", text):
        text = f"{text[:10]}-{text[10:12]}-{text[12:]}"
    if not ACCESSION_RE.match(text):
        raise ValueError(f"an accession number must be written 0000000000-00-000000, got {value!r}")
    return text


def to_date(value: Any) -> dt.date | None:
    if value in (None, ""):
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value).strip()[:10])


def parse_acceptance(value: str | None) -> dt.datetime | None:
    """EDGAR acceptance time → timezone-aware UTC datetime.

    acceptanceDateTime in submissions is true UTC (the trailing Z is correct; see inputs/edgar/acceptance_timezone.md);
    the YYYYMMDDHHMMSS in the SGML header and Accepted on the index page are US Eastern time.
    """
    if not value:
        return None
    text = str(value).strip()
    if re.fullmatch(r"\d{14}", text):
        return dt.datetime.strptime(text, "%Y%m%d%H%M%S").replace(tzinfo=NY).astimezone(UTC)
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def iso(moment: dt.datetime) -> str:
    return moment.isoformat(timespec="seconds")


@dataclasses.dataclass(frozen=True)
class Filing:
    """One row of submissions."""

    cik: str  # issuer whose submissions list it; not necessarily the filing agent in the accession-number prefix
    accession: str
    form: str
    filing_date: dt.date
    report_date: dt.date | None = None
    acceptance_datetime: str | None = None  # as given by EDGAR (UTC)
    items: tuple[str, ...] = ()
    primary_document: str = ""
    primary_doc_description: str = ""
    size: int | None = None

    @property
    def accepted_utc(self) -> dt.datetime | None:
        return parse_acceptance(self.acceptance_datetime)

    @property
    def accepted_et(self) -> dt.datetime | None:
        accepted = self.accepted_utc
        return accepted.astimezone(NY) if accepted else None

    @property
    def folder_url(self) -> str:
        return f"{ARCHIVES_BASE}/{int(self.cik)}/{self.accession.replace('-', '')}"

    @property
    def index_url(self) -> str:
        return f"{self.folder_url}/{self.accession}-index.htm"

    def to_dict(self) -> dict[str, Any]:
        accepted_et = self.accepted_et
        return {
            "form": self.form,
            "accession": self.accession,
            "filing_date": self.filing_date.isoformat(),
            "acceptance_datetime": self.acceptance_datetime,
            "acceptance_et": iso(accepted_et) if accepted_et else None,
            "report_date": self.report_date.isoformat() if self.report_date else None,
        }


def _rows(block: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    """Columnar submissions data → one dict per row."""
    accessions = block.get("accessionNumber") or []
    count = len(accessions)
    columns = {k: v for k, v in block.items() if isinstance(v, list) and len(v) == count}
    for i in range(count):
        yield {k: v[i] for k, v in columns.items()}


def _filing_from_row(cik: str, row: Mapping[str, Any]) -> Filing | None:
    try:
        accession = normalize_accession(row.get("accessionNumber"))
        filed = to_date(row.get("filingDate"))
    except ValueError:
        return None
    if filed is None:
        return None
    try:
        report = to_date(row.get("reportDate"))
    except ValueError:
        report = None
    size = row.get("size")
    return Filing(
        cik=cik,
        accession=accession,
        form=str(row.get("form") or "").strip(),
        filing_date=filed,
        report_date=report,
        acceptance_datetime=row.get("acceptanceDateTime") or None,
        items=tuple(x.strip() for x in str(row.get("items") or "").split(",") if x.strip()),
        primary_document=str(row.get("primaryDocument") or ""),
        primary_doc_description=str(row.get("primaryDocDescription") or ""),
        size=size if isinstance(size, int) else None,
    )


@dataclasses.dataclass
class Submissions:
    cik: str
    name: str
    tickers: tuple[str, ...]
    fiscal_year_end: str | None  # EDGAR's MMDD, e.g. "1231", "0630"
    entity_type: str | None
    category: str | None
    filings: list[Filing]  # sorted by filing date, then acceptance time
    pages: list[dict[str, Any]]  # descriptions of the continuation pages in filings.files
    pages_loaded: list[str]

    @functools.cached_property
    def _by_accession(self) -> dict[str, Filing]:
        return {f.accession: f for f in self.filings}

    def get(self, accession: str) -> Filing | None:
        return self._by_accession.get(normalize_accession(accession))

    def forms(self, *forms: str) -> list[Filing]:
        wanted = set(forms)
        return [f for f in self.filings if f.form in wanted]


def submissions(cik: Any, *, client: EdgarClient | None = None, since: dt.date | None = None) -> Submissions:
    """All filings of an issuer: the recent block plus the continuation pages in filings.files.

    With since, only continuation pages whose end date is not before since are fetched.
    """
    client = client or default_client()
    cik10 = normalize_cik(cik)
    data = client.get_json(f"{DATA_BASE}/submissions/CIK{cik10}.json", max_age=client.ttl)
    filings_block = data.get("filings") or {}
    rows = list(_rows(filings_block.get("recent") or {}))
    pages = [dict(p) for p in (filings_block.get("files") or []) if isinstance(p, Mapping)]
    loaded = []
    for page in pages:
        name = str(page.get("name") or "")
        if not _OVERFLOW_PAGE_RE.match(name):
            raise EdgarDataError(f"the submissions of CIK {cik10} list an unrecognized continuation page {name!r}")
        if since is not None and str(page.get("filingTo") or "9999") < since.isoformat():
            continue
        rows.extend(_rows(client.get_json(f"{DATA_BASE}/submissions/{name}", max_age=client.ttl)))
        loaded.append(name)
    seen: set[str] = set()
    filings: list[Filing] = []
    for row in rows:
        filing = _filing_from_row(cik10, row)
        if filing is None or filing.accession in seen:
            continue
        seen.add(filing.accession)
        filings.append(filing)
    filings.sort(key=lambda f: (f.filing_date, f.acceptance_datetime or "", f.accession))
    return Submissions(
        cik=cik10,
        name=str(data.get("name") or ""),
        tickers=tuple(data.get("tickers") or ()),
        fiscal_year_end=data.get("fiscalYearEnd") or None,
        entity_type=data.get("entityType"),
        category=data.get("category"),
        filings=filings,
        pages=pages,
        pages_loaded=loaded,
    )


def infer_filer_type(subs: Submissions) -> str:
    """Domestic issuer if it filed 10-K/10-Q in recent years; foreign private issuer if it files only 20-F/40-F/6-K."""
    recent = subs.filings[-600:]
    if any(f.form in ("10-K", "10-Q") for f in recent):
        return DOMESTIC
    if any(f.form in ("20-F", "40-F", "6-K") for f in recent):
        return FOREIGN
    return DOMESTIC


# ---------------------------------------------------------------------------------------------------------------
# Filing index and documents
# ---------------------------------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Document:
    name: str
    url: str
    kind: str  # primary | exhibit | xbrl_viewer | other
    exhibit: str | None = None  # EX-99, EX-99.1, ... (only the EX-99 series is recognized)
    size: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "exhibit": self.exhibit, "size": self.size, "url": self.url}


def classify_document(name: str, primary_document: str | None = None) -> tuple[str, str | None]:
    if primary_document and name == primary_document:
        return "primary", None
    if _R_PAGE_RE.match(name):
        return "xbrl_viewer", None
    m = _EX99_RE.search(name)
    if m and name.lower().endswith((".htm", ".html", ".txt", ".pdf")):
        number = m.group(1) or m.group(2)
        return "exhibit", f"EX-99.{int(number)}" if number else "EX-99"
    return "other", None


def _exhibit_order(doc: Document) -> tuple[int, str]:
    number = doc.exhibit.split(".", 1)[1] if doc.exhibit and "." in doc.exhibit else "0"
    return int(number), doc.name


def filing_documents(cik: Any, accession: str, *, client: EdgarClient | None = None,
                     primary_document: str | None = None) -> list[Document]:
    """File list of one filing (index.json). The primary document comes from primaryDocument in submissions."""
    client = client or default_client()
    cik10 = normalize_cik(cik)
    accession = normalize_accession(accession)
    folder = f"{ARCHIVES_BASE}/{int(cik10)}/{accession.replace('-', '')}"
    data = client.get_json(f"{folder}/index.json")
    items = ((data.get("directory") or {}).get("item")) or []
    names = [str(item.get("name") or "") for item in items]
    docs = []
    for item in items:
        name = str(item.get("name") or "")
        if not _DOC_NAME_RE.match(name):
            continue
        kind, exhibit = classify_document(name, primary_document)
        size = item.get("size")
        size = int(size) if isinstance(size, int) or (isinstance(size, str) and size.isdigit()) else None
        docs.append(Document(name=name, url=f"{folder}/{name}", kind=kind, exhibit=exhibit, size=size))
    unnamed = [d for d in docs if d.kind == "other" and d.name.lower().endswith((".htm", ".html", ".txt"))
               and not d.name.startswith(accession)]
    if unnamed and primary_document:  # an exhibit whose name does not say EX-99: the filing index says what it is
        types = document_types(client, folder, accession, names)
        for i, doc in enumerate(docs):
            kind = types.get(doc.name, "").upper()
            if doc in unnamed and _INDEX_EX99_RE.match(kind):
                docs[i] = dataclasses.replace(doc, kind="exhibit", exhibit=kind)
    return docs


_INDEX_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_INDEX_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)
_INDEX_EX99_RE = re.compile(r"^EX-99(?:\.\d{1,2})?$")


def document_types(client: EdgarClient, folder: str, accession: str, names: Sequence[str]) -> dict[str, str]:
    """Document name -> the type the filing index page gives it (EX-99.1, GRAPHIC, 8-K ...); empty when the page is
    missing or cannot be read (the file-name rules of classify_document() then stand alone)."""
    page = next((n for n in (f"{accession}-index.html", f"{accession}-index.htm") if n in names), None)
    if page is None:
        return {}
    try:
        raw = client.get_bytes(f"{folder}/{page}", max_age=None).decode("utf-8", "replace")
    except EdgarError:
        return {}
    types: dict[str, str] = {}
    for row in _INDEX_ROW_RE.findall(raw):
        cells = [re.sub(r"<[^>]+>|&nbsp;", " ", c).split() for c in _INDEX_CELL_RE.findall(row)]
        if len(cells) >= 4 and cells[2] and cells[3]:
            types[cells[2][0]] = cells[3][0]
    return types


def download_filing(cik: Any, accession: str, dest: str | os.PathLike, *, client: EdgarClient | None = None,
                    primary_document: str | None = None, exhibit_prefixes: Sequence[str] = ("EX-99",)) -> list[Path]:
    """Download the primary document and exhibits (by default only EX-99.x) to dest/<accession>/.

    XBRL viewer pages R<n>.htm are never downloaded.
    """
    client = client or default_client()
    accession = normalize_accession(accession)
    if primary_document is None:
        filing = submissions(cik, client=client).get(accession)
        primary_document = filing.primary_document if filing else None
    docs = filing_documents(cik, accession, client=client, primary_document=primary_document)
    wanted = [d for d in docs if d.kind == "primary"
              or (d.kind == "exhibit" and d.exhibit and d.exhibit.startswith(tuple(exhibit_prefixes)))]
    target = Path(dest) / accession
    written = []
    for doc in wanted:
        path = target / doc.name
        _atomic_write(path, client.get_bytes(doc.url))
        written.append(path)
    return written


_BLOCK_TAG_RE = re.compile(r"(?i)<br\s*/?>|</?(?:p|div|tr|li|h[1-6]|table|center|title|body)\b[^>]*>")
_INLINE_TAG_RE = re.compile(r"(?i)</?(?:a|abbr|b|big|em|font|i|ins|del|s|small|span|strike|strong|sub|sup|tt|u)\b[^>]*>")


def html_to_text(raw: bytes | str, limit: int = 200_000) -> str:
    """HTML → plain text, one line per paragraph (the first limit bytes are enough to find the title and dateline).

    Line breaks in the source are only whitespace (titles are often wrapped onto two lines); only paragraph-type tags
    start a new line; inline tags (span, font, b, ...) are removed without adding a space ("202<span>5</span>" is 2025).
    """
    text = raw[:limit].decode("utf-8", "replace") if isinstance(raw, bytes) else raw[:limit]
    text = re.sub(r"(?is)<(script|style|head)\b.*?</\1\s*>", " ", text)
    text = re.sub(r"\s+", " ", text)
    text = _BLOCK_TAG_RE.sub("\n", text)
    text = _INLINE_TAG_RE.sub("", text)
    text = re.sub(r"<[^>]*>", " ", text)
    text = html.unescape(text).replace("\xa0", " ")
    lines = (re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10,
           "nov": 11, "dec": 12}
_MONTH_DATE_RE = re.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|"
    r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+(\d{1,2}),?\s+(\d{4})\b", re.I)
_QUARTER_WORDS = {"first": 1, "second": 2, "third": 3, "fourth": 4}


def _first_date(text: str) -> dt.date | None:
    for m in _MONTH_DATE_RE.finditer(text):
        try:
            return dt.date(int(m.group(3)), _MONTHS[m.group(1)[:3].lower()], int(m.group(2)))
        except ValueError:
            continue
    return None


@dataclasses.dataclass(frozen=True)
class ResultsRelease:
    """A quarterly results press release in a 6-K exhibit."""

    document: str
    title: str
    quarter: int | None
    year: int | None
    dateline: dt.date | None


def find_results_title(text: str) -> ResultsRelease | None:
    """Find a title like "<Company> Announces <quarter> ... Results" at the top of a press release, and the dateline
    right after it.

    "to/will report ..." (advance notice of a release date) does not count; nor does anything without "quarter", such as
    shareholder meeting results.
    """
    lines = text.split("\n")[:80]
    for i, line in enumerate(lines):
        low = line.lower()
        if not re.search(r"\b(?:announces|reports)\b", low):
            continue
        if not (re.search(r"\bquarter\b", low) and re.search(r"\bresults\b", low)):
            continue
        if re.search(r"\b(?:to|will)\s+(?:announce|report)\b", low):
            continue
        mq = re.search(r"\b(first|second|third|fourth)\s+quarter\b(?:\s+(?:of\s+)?(?:fiscal\s+(?:year\s+)?)?(\d{4}))?",
                       low)
        dateline = _first_date("\n".join(lines[i + 1:i + 8])[:3000])
        return ResultsRelease(
            document="",
            title=line[:300],
            quarter=_QUARTER_WORDS[mq.group(1)] if mq else None,
            year=int(mq.group(2)) if mq and mq.group(2) else None,
            dateline=dateline,
        )
    return None


def classify_6k(filing: Filing, *, client: EdgarClient | None = None) -> ResultsRelease | None:
    """Whether this 6-K reports quarterly results.

    Judged by the press-release title in the first EX-99 exhibit (or in the 6-K body when there is no exhibit).
    """
    client = client or default_client()
    docs = filing_documents(filing.cik, filing.accession, client=client, primary_document=filing.primary_document)
    exhibits = sorted((d for d in docs if d.kind == "exhibit"), key=_exhibit_order)
    candidates = exhibits[:1] or [d for d in docs if d.kind == "primary"]
    for doc in candidates:
        found = find_results_title(html_to_text(client.get_bytes(doc.url)))
        if found:
            return dataclasses.replace(found, document=doc.name)
    return None


# ---------------------------------------------------------------------------------------------------------------
# Fiscal years and business days
# ---------------------------------------------------------------------------------------------------------------


def _month_end(year: int, month: int) -> dt.date:
    return dt.date(year, month, calendar.monthrange(year, month)[1])


def _snap_to_month_end(day: dt.date) -> dt.date:
    """Quarter ends in a 52/53-week fiscal year fall a few days around a month end.

    Day 15 or earlier counts as the previous month end; later days count as the end of the same month.
    """
    if day.day <= 15:
        first = day.replace(day=1)
        previous = first - dt.timedelta(days=1)
        return _month_end(previous.year, previous.month)
    return _month_end(day.year, day.month)


def parse_period(value: str) -> tuple[int, int | None]:
    m = PERIOD_RE.match(str(value).strip())
    if not m:
        raise ValueError(f"a period must be written FY<year>Q<quarter> or FY<year>, got {value!r}")
    return int(m.group(1)), (int(m.group(2)) if m.group(2) else None)


@dataclasses.dataclass(frozen=True)
class FiscalCalendar:
    """The company's own fiscal year.

    A fiscal year is named for the year in which it ends (MSFT's fiscal year ending 2026-06-30 is FY2026); quarter ends
    are month ends.
    """

    month: int
    day: int = 31

    @classmethod
    def parse(cls, value: Any) -> FiscalCalendar:
        m = re.fullmatch(r"-*(\d{1,2})-?(\d{2})", str(value).strip())
        if not m:
            raise ValueError(f"a fiscal year end must be written MM-DD (e.g. 12-31, 06-30), got {value!r}")
        month, day = int(m.group(1)), int(m.group(2))
        if not (1 <= month <= 12 and 1 <= day <= 31):
            raise ValueError(f"a fiscal year end must be written MM-DD (e.g. 12-31, 06-30), got {value!r}")
        return cls(month, day)

    @property
    def label(self) -> str:
        return f"{self.month:02d}-{self.day:02d}"

    def quarter_end(self, fiscal_year: int, quarter: int) -> dt.date:
        month, year = self.month - 3 * (4 - quarter), fiscal_year
        while month <= 0:
            month += 12
            year -= 1
        return _month_end(year, month)

    def period_of(self, day: dt.date) -> tuple[int, int]:
        """Which fiscal year and quarter a quarter end belongs to (allowing a few days' offset for 52/53-week years)."""
        snapped = _snap_to_month_end(day)
        offset = (snapped.month - self.month) % 12
        if offset % 3:
            raise ValueError(f"{day} is not a quarter end for a company whose fiscal year ends on {self.label}")
        quarter = {3: 1, 6: 2, 9: 3, 0: 4}[offset]
        fiscal_year = snapped.year if snapped.month <= self.month else snapped.year + 1
        return fiscal_year, quarter

    def quarter_label(self, day: dt.date) -> str:
        fiscal_year, quarter = self.period_of(day)
        return f"FY{fiscal_year}Q{quarter}"

    def annual_label(self, day: dt.date) -> str:
        return f"FY{self.period_of(day)[0]}"

    def last_quarter_end_before(self, day: dt.date) -> dt.date:
        year, month = day.year, day.month
        for _ in range(16):
            if (month - self.month) % 3 == 0:
                end = _month_end(year, month)
                if end < day:
                    return end
            month -= 1
            if month == 0:
                month, year = 12, year - 1
        raise AssertionError("unreachable")


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    first = dt.date(year, month, 1)
    return first + dt.timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> dt.date:
    last = _month_end(year, month)
    return last - dt.timedelta(days=(last.weekday() - weekday) % 7)


def _observed(day: dt.date) -> dt.date:
    if day.weekday() == 5:
        return day - dt.timedelta(days=1)
    if day.weekday() == 6:
        return day + dt.timedelta(days=1)
    return day


def _fixed_and_floating_holidays(year: int) -> list[dt.date]:
    days = [
        _observed(dt.date(year, 1, 1)),  # New Year's Day
        _nth_weekday(year, 1, 0, 3),  # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),  # Presidents' Day
        _last_weekday(year, 5, 0),  # Memorial Day
        _observed(dt.date(year, 7, 4)),  # Independence Day
        _nth_weekday(year, 9, 0, 1),  # Labor Day
        _nth_weekday(year, 10, 0, 2),  # Columbus Day
        _observed(dt.date(year, 11, 11)),  # Veterans Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving Day
        _observed(dt.date(year, 12, 25)),  # Christmas Day
    ]
    if year >= 2021:
        days.append(_observed(dt.date(year, 6, 19)))  # Juneteenth
    return days


# Days the federal government was temporarily closed by executive order (EDGAR accepts no filings on them
# either); these only affect business-day calculations for past dates.
EXTRA_CLOSURES = frozenset({
    dt.date(2018, 12, 5), dt.date(2018, 12, 24), dt.date(2019, 12, 24), dt.date(2020, 12, 24),
    dt.date(2024, 12, 24), dt.date(2025, 1, 9), dt.date(2025, 12, 24), dt.date(2025, 12, 26),
})


@functools.lru_cache(maxsize=64)
def us_federal_holidays(year: int) -> frozenset[dt.date]:
    """US federal holidays.

    A holiday on a Saturday moves to Friday and one on a Sunday to Monday; next year's New Year's Day may fall on
    December 31 of this year.
    """
    days = [d for y in (year, year + 1) for d in _fixed_and_floating_holidays(y) if d.year == year]
    return frozenset(days)


def is_business_day(day: dt.date) -> bool:
    return day.weekday() < 5 and day not in us_federal_holidays(day.year) and day not in EXTRA_CLOSURES


def previous_business_day(day: dt.date, *, inclusive: bool = True) -> dt.date:
    if not inclusive:
        day -= dt.timedelta(days=1)
    while not is_business_day(day):
        day -= dt.timedelta(days=1)
    return day


def next_business_day(day: dt.date, *, inclusive: bool = True) -> dt.date:
    if not inclusive:
        day += dt.timedelta(days=1)
    while not is_business_day(day):
        day += dt.timedelta(days=1)
    return day


def add_business_days(day: dt.date, count: int) -> dt.date:
    for _ in range(count):
        day = next_business_day(day, inclusive=False)
    return day


def prereg_deadline(release_date: dt.date) -> dt.datetime:
    """23:59:59 US Eastern on the day before the release date.

    The UTC offset is the one actually in effect at that moment (daylight time -04:00, standard time -05:00).
    """
    day = release_date - dt.timedelta(days=1)
    return dt.datetime.combine(day, dt.time(23, 59, 59), tzinfo=NY)


def merge_by(deadline: dt.datetime, hours: int = MERGE_HOURS) -> dt.datetime:
    """The deadline minus the given number of actual elapsed hours, expressed in US Eastern time.

    Converts to UTC before subtracting, so it is also correct across a daylight-saving change.
    """
    if deadline.tzinfo is None:
        raise ValueError("the deadline must have a time zone")
    return (deadline.astimezone(UTC) - dt.timedelta(hours=hours)).astimezone(NY)


def _shift_years(day: dt.date, years: int) -> dt.date:
    try:
        return day.replace(year=day.year + years)
    except ValueError:  # February 29
        return day.replace(year=day.year + years, day=28)


def today_et() -> dt.date:
    return dt.datetime.now(NY).date()


# ---------------------------------------------------------------------------------------------------------------
# Earnings events and release_history
# ---------------------------------------------------------------------------------------------------------------


@dataclasses.dataclass
class EarningsEvent:
    """One earnings event: a domestic issuer's earnings 8-K (Item 2.02) or a foreign issuer's earnings 6-K."""

    cik: str
    period: str  # FY<year>Q<quarter> in the company's fiscal year
    period_end: dt.date
    filing: Filing
    release_date: dt.date  # US Eastern date the results were first made public (release date)
    release_basis: str  # acceptance | report_date | closing | dateline | filing_date
    closing: Filing | None = None  # domestic: the 10-Q/10-K for the same period
    closes_on: dt.date | None = None
    closed_by: str | None = None  # 10-Q | 10-K | T+5 | 6-K
    exhibit: str | None = None  # 6-K: the document that holds the press release
    title: str | None = None  # 6-K: press-release title
    dateline: dt.date | None = None  # 6-K: press-release dateline date
    amendments: list[Filing] = dataclasses.field(default_factory=list)
    related: list[Filing] = dataclasses.field(default_factory=list)
    notes: list[str] = dataclasses.field(default_factory=list)

    @property
    def form(self) -> str:
        return self.filing.form

    @property
    def accession(self) -> str:
        return self.filing.accession

    @property
    def fiscal_year(self) -> int:
        return parse_period(self.period)[0]

    @property
    def quarter(self) -> int:
        return parse_period(self.period)[1] or 4

    def to_dict(self) -> dict[str, Any]:
        f = self.filing
        accepted_et = f.accepted_et
        out: dict[str, Any] = {
            "period": self.period,
            "period_end": self.period_end.isoformat(),
            "form": f.form,
            "accession": f.accession,
            "filing_date": f.filing_date.isoformat(),
            "acceptance_datetime": f.acceptance_datetime,
            "acceptance_et": iso(accepted_et) if accepted_et else None,
            "release_date": self.release_date.isoformat(),
            "release_basis": self.release_basis,
            "report_date": f.report_date.isoformat() if f.report_date else None,
        }
        if f.items:
            out["items"] = list(f.items)
        out["primary_document"] = f.primary_document
        if self.exhibit:
            out["exhibit"] = self.exhibit
            out["title"] = self.title
            out["dateline"] = self.dateline.isoformat() if self.dateline else None
        if self.closing:
            out["closing"] = self.closing.to_dict()
        out["closes_on"] = self.closes_on.isoformat() if self.closes_on else None
        out["closed_by"] = self.closed_by
        if self.amendments:
            out["amendments"] = [a.accession for a in self.amendments]
        if self.related:
            out["related"] = [r.accession for r in self.related]
        if self.notes:
            out["notes"] = list(self.notes)
        return out


def _release_date_8k(filing: Filing) -> tuple[dt.date, str]:
    """Release date of an earnings 8-K: the US Eastern date of the acceptance time; if the only items are 2.02/9.01 and
    the report date (announcement date) is 1–4 days earlier, the report date.

    Berkshire releases on a Saturday and files the 8-K the following week, so the report date is that Saturday. For an
    8-K with other items (5.02, 7.01, ...), the report date is the date of the earliest event, not the release date (as
    with APP 2023-11-08 and MSFT 2025-10-29).
    """
    accepted = filing.accepted_et
    base, basis = (accepted.date(), "acceptance") if accepted else (filing.filing_date, "filing_date")
    if filing.report_date and set(filing.items) <= {"2.02", "9.01"} and 0 < (base - filing.report_date).days <= 4:
        return filing.report_date, "report_date"
    return base, basis


def _domestic_events(subs: Submissions, cal: FiscalCalendar, since: dt.date | None,
                     until: dt.date | None) -> list[EarningsEvent]:
    periodic: dict[str, Filing] = {}
    for f in subs.filings:
        if f.form in CLOSING_FORMS and f.report_date:
            try:
                label = cal.quarter_label(f.report_date)
            except ValueError:
                continue
            periodic.setdefault(label, f)  # for each period, keep the first one filed
    events: dict[str, EarningsEvent] = {}
    amendments: list[Filing] = []
    for f in subs.filings:
        if "2.02" not in f.items or not f.form.startswith("8-K"):
            continue
        if (since and f.filing_date < since) or (until and f.filing_date > until):
            continue
        if f.form != "8-K":
            amendments.append(f)
            continue
        release, basis = _release_date_8k(f)
        period_end = cal.last_quarter_end_before(release)
        label = cal.quarter_label(period_end)
        if label in events:
            events[label].related.append(f)
            events[label].notes.append(f"another Item 2.02 8-K for the same period: {f.accession} ({f.filing_date})")
            continue
        event = EarningsEvent(cik=subs.cik, period=label, period_end=period_end, filing=f, release_date=release,
                              release_basis=basis)
        closing = periodic.get(label)
        closing_et = closing.accepted_et if closing else None
        if closing_et and closing_et.date() < event.release_date:
            # the 10-Q/10-K was accepted before the 8-K (Berkshire): the results first became public on EDGAR in the
            # periodic report
            event.release_date, event.release_basis = closing_et.date(), "closing"
            event.notes.append(f"the {closing.form} was accepted before the 8-K, so the release date is the "
                                f"{closing.form}'s acceptance date")
        t_plus_5 = add_business_days(f.filing_date, 5)
        event.closing = closing
        if closing and closing.filing_date <= t_plus_5:
            event.closes_on, event.closed_by = max(f.filing_date, closing.filing_date), closing.form
        else:
            event.closes_on, event.closed_by = t_plus_5, "T+5"
        if closing is None:
            event.notes.append("no 10-Q/10-K for this period yet")
        events[label] = event
    for amendment in amendments:
        release, _ = _release_date_8k(amendment)
        label = cal.quarter_label(cal.last_quarter_end_before(release))
        if label in events:
            events[label].amendments.append(amendment)
    return sorted(events.values(), key=lambda e: (e.filing.filing_date, e.accession))


def _foreign_events(subs: Submissions, cal: FiscalCalendar, since: dt.date | None, until: dt.date | None,
                    client: EdgarClient) -> list[EarningsEvent]:
    events: dict[str, EarningsEvent] = {}
    for f in subs.filings:
        if f.form not in ("6-K", "6-K/A"):
            continue
        if (since and f.filing_date < since) or (until and f.filing_date > until):
            continue
        release_info = classify_6k(f, client=client)
        if release_info is None:
            continue
        accepted = f.accepted_et
        base = accepted.date() if accepted else f.filing_date
        if release_info.dateline and release_info.dateline < base and (base - release_info.dateline).days <= 7:
            release, basis = release_info.dateline, "dateline"
        else:
            release, basis = base, "acceptance" if accepted else "filing_date"
        notes = []
        period_end = cal.last_quarter_end_before(release)
        fiscal_year, quarter = cal.period_of(period_end)
        if release_info.quarter and release_info.quarter != quarter:
            day = period_end
            for _ in range(4):
                if cal.period_of(day)[1] == release_info.quarter:
                    break
                day = cal.last_quarter_end_before(day)
            notes.append(f"the press-release title names quarter {release_info.quarter}; the period follows the title, "
                          "not the last quarter end before the release date")
            period_end = day
            fiscal_year, quarter = cal.period_of(day)
        if release_info.year and cal.month == 12 and release_info.year != fiscal_year:
            notes.append(f"the year in the title ({release_info.year}) does not match the derived fiscal year "
                          f"FY{fiscal_year}")
        label = f"FY{fiscal_year}Q{quarter}"
        if f.form != "6-K":
            if label in events:
                events[label].amendments.append(f)
            continue
        if label in events:
            events[label].related.append(f)
            events[label].notes.append(f"another results 6-K for the same period: {f.accession} ({f.filing_date})")
            continue
        events[label] = EarningsEvent(
            cik=subs.cik, period=label, period_end=period_end, filing=f, release_date=release, release_basis=basis,
            closes_on=f.filing_date, closed_by="6-K", exhibit=release_info.document, title=release_info.title,
            dateline=release_info.dateline, notes=notes,
        )
    return sorted(events.values(), key=lambda e: (e.filing.filing_date, e.accession))


def _calendar_for(subs: Submissions, fiscal_year_end: Any = None) -> FiscalCalendar:
    value = fiscal_year_end or subs.fiscal_year_end
    if not value:
        raise EdgarDataError(f"CIK {subs.cik}: no fiscal year end given and none on EDGAR; pass fiscal_year_end "
                              "(filer.fiscal_year_end in thesis.yml)")
    cal = FiscalCalendar.parse(value)
    if fiscal_year_end and subs.fiscal_year_end:
        edgar = FiscalCalendar.parse(subs.fiscal_year_end)
        if edgar.month != cal.month:
            LOG.warning("CIK %s: the fiscal year end passed in (%s) differs from EDGAR's (%s); using the one passed in",
                        subs.cik, cal.label, edgar.label)
    return cal


def earnings_events(cik: Any, fiscal_year_end: Any = None, *, filer_type: str | None = None,
                    since: dt.date | None = None, until: dt.date | None = None, client: EdgarClient | None = None,
                    subs: Submissions | None = None) -> list[EarningsEvent]:
    """Earnings events in since..until (by EDGAR filing date, both ends inclusive), in filing order."""
    client = client or default_client()
    subs = subs or submissions(cik, client=client, since=since)
    cal = _calendar_for(subs, fiscal_year_end)
    kind = filer_type or infer_filer_type(subs)
    if kind == FOREIGN:
        return _foreign_events(subs, cal, since, until, client)
    return _domestic_events(subs, cal, since, until)


def release_history(cik: Any, fiscal_year_end: Any = None, *, filer_type: str | None = None,
                    years: int = HISTORY_YEARS, as_of: dt.date | None = None, quarter: int | None = None,
                    client: EdgarClient | None = None) -> list[EarningsEvent]:
    """Earnings events with an EDGAR filing date within the given number of years before as_of.

    If quarter is given, only that fiscal quarter is kept.
    """
    as_of = as_of or today_et()
    since = _shift_years(as_of, -years)
    events = earnings_events(cik, fiscal_year_end, filer_type=filer_type, since=since, until=as_of, client=client)
    if quarter is not None:
        events = [e for e in events if e.quarter == quarter]
    return events


# ---------------------------------------------------------------------------------------------------------------
# Next release date and pre-registration deadline (prompt 15A)
# ---------------------------------------------------------------------------------------------------------------


@dataclasses.dataclass
class ReleaseEstimate:
    cik: str
    period: str
    period_end: dt.date
    form: str
    status: str  # released | announced | announced_window | estimated
    expected_release: dt.date
    placeholder: bool
    basis: str
    deadline: dt.datetime
    merge_by: dt.datetime
    history_window: tuple[dt.date, dt.date] | None = None  # past same-quarter release dates moved to this period's year
    history: list[EarningsEvent] = dataclasses.field(default_factory=list)
    event: EarningsEvent | None = None  # when already released
    notes: list[str] = dataclasses.field(default_factory=list)

    def prereg_header(self) -> dict[str, Any]:
        """The pipeline's part of the pre-registration header: event and deadline (schema: prereg.schema.json)."""
        return {
            "event": {
                "period": self.period,
                "expected_release": self.expected_release.isoformat(),
                "form": self.form,
                "placeholder": self.placeholder,
            },
            "deadline": iso(self.deadline),
        }

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "cik": self.cik,
            "period": self.period,
            "period_end": self.period_end.isoformat(),
            "form": self.form,
            "status": self.status,
            "expected_release": self.expected_release.isoformat(),
            "placeholder": self.placeholder,
            "basis": self.basis,
            "history_window": ({"from": self.history_window[0].isoformat(), "to": self.history_window[1].isoformat()}
                               if self.history_window else None),
            "deadline": iso(self.deadline),
            "merge_by": iso(self.merge_by),
            "prereg": self.prereg_header(),
            "history": [_history_row(e) for e in self.history],
        }
        if self.event:
            out["event"] = self.event.to_dict()
        if self.notes:
            out["notes"] = list(self.notes)
        return out


def _history_row(event: EarningsEvent) -> dict[str, Any]:
    accepted = event.filing.accepted_et
    return {
        "period": event.period,
        "release_date": event.release_date.isoformat(),
        "filing_date": event.filing.filing_date.isoformat(),
        "acceptance_et": iso(accepted) if accepted else None,
        "form": event.form,
        "accession": event.accession,
    }


def parse_release_date(value: Any) -> dt.date:
    """Announced release date: a date is read as a US Eastern date; a time with a time zone is converted to its US
    Eastern date (for example, morning in Beijing time is the previous evening in US Eastern time).
    """
    if isinstance(value, dt.datetime):
        moment = value
    elif isinstance(value, dt.date):
        return value
    else:
        text = str(value).strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return dt.date.fromisoformat(text)
        moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=NY)
    return moment.astimezone(NY).date()


def estimate_release(fiscal_year: int, period_end: dt.date, same_quarter: Sequence[EarningsEvent],
                     latest: EarningsEvent | None) -> tuple[dt.date, str, tuple[dt.date, dt.date] | None]:
    """Placeholder release date when no date has been announced (15A). Returns (date, basis, history window).

    With release_history: take the earliest month-day in the same fiscal quarter across past years, move it to this
    period's year, count back 3 calendar days, and if that falls on a weekend or federal holiday keep going back to a
    business day. Without it: apply the latest period's gap from quarter end to release date to this quarter, then
    subtract 3 days.
    """
    lead = dt.timedelta(days=ESTIMATE_LEAD_DAYS)
    if same_quarter:
        shifted = sorted(((_shift_years(e.release_date, fiscal_year - e.fiscal_year), e) for e in same_quarter),
                         key=lambda pair: (pair[0], pair[1].period))
        earliest, source = shifted[0]
        expected = previous_business_day(earliest - lead)
        basis = (f"release_history: the earliest month-day among past releases of this fiscal quarter is "
                 f"{source.release_date} ({source.period}), which is {earliest} in {earliest.year}; "
                 f"{ESTIMATE_LEAD_DAYS} calendar days earlier, moved back to a business day -> {expected}")
        return expected, basis, (shifted[0][0], shifted[-1][0])
    if latest is None:
        raise EdgarDataError("cannot estimate the release date: no past release of this fiscal quarter and no "
                             "release of an earlier period; use --announced or --window")
    lag = (latest.release_date - latest.period_end).days
    expected = previous_business_day(period_end + dt.timedelta(days=lag) - lead)
    basis = (f"no past release of this fiscal quarter: the latest period, {latest.period}, was released {lag} days "
             f"after its quarter end; the same lag from this quarter end ({period_end}), then {ESTIMATE_LEAD_DAYS} "
             f"calendar days earlier, moved back to a business day -> {expected}")
    return expected, basis, None


def next_release(cik: Any, period: str, *, fiscal_year_end: Any = None, filer_type: str | None = None,
                 form: str | None = None, announced: Any = None,
                 window: tuple[Any, Any] | None = None, as_of: dt.date | None = None,
                 client: EdgarClient | None = None) -> ReleaseEstimate:
    """(Expected) release date, pre-registration deadline and merge-by time for one fiscal quarter.

    Priority: already released on EDGAR > date announced by the company (announced, placeholder: false) > earliest
    business day in a window announced by the company > release_history (the same fiscal quarter in the past three
    fiscal years) > the latest period's lag. The last three give placeholder: true.
    """
    client = client or default_client()
    as_of = as_of or today_et()
    fiscal_year, quarter = parse_period(period)
    if quarter is None:
        raise ValueError(f"pre-registration is by quarter; the period must name a quarter (FY2026Q3), got {period!r}")
    # the continuation pages need only cover the past three fiscal years; one more year back is a margin in case
    # the fiscal year and the calendar year do not line up
    subs = submissions(cik, client=client, since=dt.date(fiscal_year - HISTORY_YEARS - 2, 1, 1))
    cal = _calendar_for(subs, fiscal_year_end)
    kind = filer_type or infer_filer_type(subs)
    form = form or ("6-K" if kind == FOREIGN else "8-K")
    period_end = cal.quarter_end(fiscal_year, quarter)
    since = cal.quarter_end(fiscal_year - HISTORY_YEARS, quarter)
    events = earnings_events(subs.cik, cal.label, filer_type=kind, since=since, client=client, subs=subs)
    wanted = {f"FY{fiscal_year - k}Q{quarter}" for k in range(1, HISTORY_YEARS + 1)}
    history = sorted((e for e in events if e.period in wanted), key=lambda e: e.period)
    earlier = [e for e in events if e.period_end < period_end]
    latest = max(earlier, key=lambda e: e.period_end) if earlier else None
    notes: list[str] = []
    released = next((e for e in events if e.period == f"FY{fiscal_year}Q{quarter}"), None)

    estimated_window = None
    if released:
        expected, placeholder, status = released.release_date, False, "released"
        basis = f"already released on EDGAR: {released.form} {released.accession} ({released.release_date})"
    elif announced is not None:
        expected, placeholder, status = parse_release_date(announced), False, "announced"
        basis = f"release date announced by the company: {expected}"
    elif window is not None:
        start, end = parse_release_date(window[0]), parse_release_date(window[1])
        if end < start:
            raise ValueError(f"the release window starts ({start}) after it ends ({end})")
        expected = next_business_day(start)
        if expected > end:
            raise ValueError(f"the announced window {start}..{end} has no business day")
        placeholder, status = True, "announced_window"
        basis = f"the earliest business day in the release window announced by the company ({start}..{end})"
    else:
        expected, basis, estimated_window = estimate_release(fiscal_year, period_end, history, latest)
        placeholder, status = True, "estimated"
        if len(history) < HISTORY_YEARS:
            notes.append(f"only {len(history)} of {HISTORY_YEARS} past releases of this fiscal quarter were found")
    if not released and history:
        shifted = sorted(_shift_years(e.release_date, fiscal_year - e.fiscal_year) for e in history)
        estimated_window = estimated_window or (shifted[0], shifted[-1])

    deadline = prereg_deadline(expected)
    merge_time = merge_by(deadline)
    now_et = dt.datetime.now(NY) if as_of == today_et() else dt.datetime.combine(as_of, dt.time(0, 0), tzinfo=NY)
    if not released:
        if expected <= period_end:
            notes.append(f"the release date {expected} is not after the quarter end {period_end}; check it")
        if as_of >= expected:
            notes.append(f"{as_of} is on or after the (expected) release date {expected}, and EDGAR has no results "
                          "filing for this period yet")
        elif now_et > merge_time:
            notes.append(f"the merge-by time {iso(merge_time)} has passed")
    return ReleaseEstimate(
        cik=subs.cik, period=f"FY{fiscal_year}Q{quarter}", period_end=period_end, form=form, status=status,
        expected_release=expected, placeholder=placeholder, basis=basis, deadline=deadline, merge_by=merge_time,
        history_window=estimated_window, history=history, event=released, notes=notes,
    )


# ---------------------------------------------------------------------------------------------------------------
# Company → CIK (reads filer in thesis.yml)
# ---------------------------------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Filer:
    ticker: str | None
    cik: str
    type: str | None = None  # domestic | foreign_private_issuer
    fiscal_year_end: str | None = None  # MM-DD
    earnings_form: str | None = None
    annual_form: str | None = None
    name: str | None = None


def load_filer(company: str, repo_root: str | os.PathLike | None = None) -> Filer:
    """Ticker → filer block in companies/<ticker>/thesis.yml; a CIK of up to 10 digits may also be given directly."""
    text = str(company).strip()
    if re.fullmatch(r"(?i)(?:CIK)?\d{1,10}", text):
        return Filer(ticker=None, cik=normalize_cik(text))
    ticker = text.upper()
    path = Path(repo_root or REPO_ROOT) / "companies" / ticker / "thesis.yml"
    if not path.is_file():
        raise ValueError(f"{path} not found: give a ticker under companies/, or a CIK")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    filer = data.get("filer") or {}
    if not filer.get("cik"):
        raise ValueError(f"{path} has no filer.cik")
    return Filer(
        ticker=str(data.get("company") or ticker),
        cik=normalize_cik(filer["cik"]),
        type=filer.get("type"),
        fiscal_year_end=filer.get("fiscal_year_end"),
        earnings_form=filer.get("earnings_form"),
        annual_form=filer.get("annual_form"),
        name=data.get("name"),
    )


def filer_from_submissions(subs: Submissions, ticker: str | None = None) -> Filer:
    """The filer block of a company that has no thesis.yml yet (a new archive, decisions/0028), from its submissions:
    the fiscal year end EDGAR gives (MMDD) and the forms it files."""
    kind = infer_filer_type(subs)
    fye = str(subs.fiscal_year_end or "")
    return Filer(ticker=ticker or (subs.tickers[0] if subs.tickers else None), cik=subs.cik, type=kind,
                 fiscal_year_end=f"{fye[:2]}-{fye[2:]}" if len(fye) == 4 and fye.isdigit() else None,
                 earnings_form="6-K" if kind == FOREIGN else "8-K", annual_form="20-F" if kind == FOREIGN else "10-K",
                 name=subs.name or None)


# ---------------------------------------------------------------------------------------------------------------
# Accession-number check for sources tables (thesis-ci SPEC §3.3, §3.5)
# ---------------------------------------------------------------------------------------------------------------

FILL_FIELDS = ("accession", "filed", "form", "period")  # --write fills only these fields, and only when missing
_FIELD_ORDER = ("tag", "kind", "title", "issuer_cik", "form", "period", "accession", "date", "filed", "url",
                "location", "primary", "note")
_TAG_RE = re.compile(r"^(?P<ticker>[A-Z0-9][A-Z0-9.]*)-(?P<form>[A-Z0-9]+)-(?P<rest>.+)$")
_TAG_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:-(\d+))?$")
ARCHIVES_URL_RE = re.compile(r"/Archives/edgar/data/(\d{1,10})/(\d{18})(?:/|$)")  # a filing's folder: CIK, accession


def form_key(form: str) -> str:
    """Comparison key for a form: hyphens, slashes and spaces removed (10-Q → 10Q, DEF 14A → DEF14A, 8-K/A → 8KA).

    FORM3 is the same as 3.
    """
    key = re.sub(r"[^A-Z0-9]", "", str(form).upper())
    key = re.sub(r"^SCHEDULE", "SC", key)
    if key.startswith("FORM") and key[4:].isdigit():
        key = key[4:]
    return key


@dataclasses.dataclass(frozen=True)
class SourceTag:
    ticker: str
    form: str  # the form as written in the tag (10Q, 8K, FORM3, ...)
    period: str | None = None
    date: dt.date | None = None
    ordinal: int = 1


def parse_source_tag(tag: str) -> SourceTag | None:
    """Periodic reports <ticker>-<form>-<period>, current reports <ticker>-<form>-<EDGAR filing date>[-N]
    (SPEC §3.3).
    """
    m = _TAG_RE.match(str(tag or ""))
    if not m:
        return None
    rest = m.group("rest")
    if PERIOD_RE.match(rest):
        return SourceTag(m.group("ticker"), m.group("form"), period=rest)
    d = _TAG_DATE_RE.match(rest)
    if d:
        try:
            day = dt.date.fromisoformat(d.group(1))
        except ValueError:
            return None
        return SourceTag(m.group("ticker"), m.group("form"), date=day, ordinal=int(d.group(2) or 1))
    return SourceTag(m.group("ticker"), m.group("form"))


@dataclasses.dataclass
class SourceFinding:
    tag: str
    line: int | None = None
    issuer_cik: str | None = None
    edgar: dict[str, Any] | None = None
    problems: list[str] = dataclasses.field(default_factory=list)  # mismatches with EDGAR
    fills: dict[str, Any] = dataclasses.field(default_factory=dict)  # missing fields that can be filled
    unresolved: str | None = None  # why the entry cannot be checked
    notes: list[str] = dataclasses.field(default_factory=list)

    @property
    def status(self) -> str:
        if self.unresolved:
            return "unresolved"
        if self.problems:
            return "mismatch"
        if self.fills:
            return "missing"
        return "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "line": self.line,
            "status": self.status,
            "issuer_cik": self.issuer_cik,
            "edgar": self.edgar,
            "problems": list(self.problems),
            "fills": {k: (v.isoformat() if isinstance(v, dt.date) else v) for k, v in self.fills.items()},
            "unresolved": self.unresolved,
            "notes": list(self.notes),
        }


@dataclasses.dataclass
class SourcesReport:
    path: Path
    findings: list[SourceFinding]
    written: bool = False
    diff: str = ""

    def counts(self) -> dict[str, int]:
        out = {"ok": 0, "missing": 0, "mismatch": 0, "unresolved": 0}
        for finding in self.findings:
            out[finding.status] += 1
        return out

    @property
    def clean(self) -> bool:
        return all(f.status == "ok" for f in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {"path": str(self.path), "counts": self.counts(), "written": self.written,
                "findings": [f.to_dict() for f in self.findings]}


@dataclasses.dataclass
class _Issuer:
    subs: Submissions
    calendar: FiscalCalendar
    kind: str


def _company_for_sources(path: Path, repo_root: Path) -> Filer | None:
    """The company that companies/<ticker>/sources.yml belongs to.

    From thesis.yml in the same directory, otherwise from companies/<ticker>/thesis.yml in this repo.
    """
    if path.parent.parent.name != "companies":
        return None
    ticker = path.parent.name
    local = path.parent / "thesis.yml"
    try:
        if local.is_file():
            return load_filer(ticker, path.parent.parent.parent)
        return load_filer(ticker, repo_root)
    except ValueError:
        return None


def _issuer(cik: str, company: Filer | None, client: EdgarClient, cache: dict[str, _Issuer]) -> _Issuer:
    if cik not in cache:
        subs = submissions(cik, client=client)
        fiscal_year_end = company.fiscal_year_end if company and company.cik == cik else None
        kind = company.type if company and company.cik == cik and company.type else infer_filer_type(subs)
        cache[cik] = _Issuer(subs, _calendar_for(subs, fiscal_year_end), kind)
    return cache[cik]


def expected_period(filing: Filing, issuer: _Issuer, client: EdgarClient) -> str | None:
    """The period EDGAR can determine: periodic reports by report date; earnings 8-K/6-K by the earnings event; anything
    else returns None (not checked).
    """
    cal = issuer.calendar
    try:
        if filing.form in ANNUAL_FORMS and filing.report_date:
            return cal.annual_label(filing.report_date)
        if filing.form in QUARTERLY_FORMS and filing.report_date:
            return cal.quarter_label(filing.report_date)
    except ValueError:
        return None
    if filing.form.startswith("8-K") and "2.02" in filing.items:
        release, _ = _release_date_8k(filing)
        return cal.quarter_label(cal.last_quarter_end_before(release))
    if filing.form.startswith("6-K"):
        events = _foreign_events(dataclasses.replace(issuer.subs, filings=[filing]), cal, None, None, client)
        return events[0].period if events else None
    return None


def _resolve_from_tag(entry: Mapping[str, Any], tag: SourceTag | None, issuer: _Issuer,
                      client: EdgarClient) -> tuple[Filing | None, str]:
    form = entry.get("form")
    key = form_key(form) if form else (form_key(tag.form) if tag else "")
    if not key:
        return None, "no form given, and the tag names no form"
    candidates = [f for f in issuer.subs.filings if form_key(f.form) == key]
    if tag and tag.date:
        same_day = sorted((f for f in candidates if f.filing_date == tag.date), key=lambda f: f.accession)
        if len(same_day) >= tag.ordinal:
            return same_day[tag.ordinal - 1], ""
        return None, f"EDGAR has fewer than {tag.ordinal} {form or tag.form} filings on {tag.date}"
    if tag and tag.period:
        candidates = [f for f in candidates
                      if f.form in PERIODIC_FORMS and expected_period(f, issuer, client) == tag.period]
    elif entry.get("filed"):
        filed = to_date(entry.get("filed"))
        candidates = [f for f in candidates if f.filing_date == filed]
    else:
        return None, "the tag has no period or date, and filed is not set"
    if len(candidates) == 1:
        return candidates[0], ""
    if not candidates:
        return None, "no matching filing on EDGAR"
    return None, f"EDGAR has {len(candidates)} candidates: {', '.join(f.accession for f in candidates[:5])}"


def _tag_problems(tag: SourceTag | None, filing: Filing, period: str | None, issuer: _Issuer,
                  registered: set[str]) -> list[str]:
    """Whether the tag agrees with EDGAR (SPEC §3.3). registered holds the accession numbers already registered in the
    same sources table.

    When several filings of the same form share a date, the suffixes "-2", "-3" are required only when all of them are
    registered (in accession-number order); when only one of them is registered, no suffix is also correct, and a suffix
    that follows the order on EDGAR is accepted too.
    """
    if tag is None:
        return []
    problems = []
    if form_key(tag.form) != form_key(filing.form):
        problems.append(f"the tag's form {tag.form} does not match EDGAR's {filing.form}")
    if filing.form in PERIODIC_FORMS:
        if tag.period and period and tag.period != period:
            problems.append(f"the tag's period {tag.period} does not match the one derived from EDGAR ({period})")
        elif tag.date:
            problems.append("a periodic report's tag must give the fiscal period (SPEC §3.3)")
    elif tag.date:
        if tag.date != filing.filing_date:
            problems.append(f"a current report's tag must use the EDGAR filing date: expected "
                            f"...-{filing.filing_date}, found ...-{tag.date}")
        else:
            same_day = sorted(f.accession for f in issuer.subs.filings
                              if f.filing_date == filing.filing_date and form_key(f.form) == form_key(filing.form))
            on_edgar = same_day.index(filing.accession) + 1 if filing.accession in same_day else 1
            listed = [a for a in same_day if a in registered or a == filing.accession]
            in_file = listed.index(filing.accession) + 1 if filing.accession in listed else 1
            if tag.ordinal not in (on_edgar, in_file):
                want = f"...-{filing.filing_date}" + (f"-{in_file}" if in_file > 1 else "")
                problems.append(f"EDGAR has {len(same_day)} {filing.form} filings on this day; by accession number "
                                f"this one is number {on_edgar} on EDGAR and number {in_file} among those "
                                f"registered in this table, so the tag should be {want}")
    elif tag.period:
        problems.append("a current report's tag must give the EDGAR filing date (SPEC §3.3)")
    return problems


def _check_entry(entry: Mapping[str, Any], company: Filer | None, client: EdgarClient,
                 issuers: dict[str, _Issuer], registered: set[str] | None = None) -> SourceFinding:
    tag_text = str(entry.get("tag") or "")
    finding = SourceFinding(tag=tag_text)
    tag = parse_source_tag(tag_text)
    raw_cik = entry.get("issuer_cik")
    if not raw_cik and company and tag and tag.ticker == company.ticker:
        raw_cik = company.cik
        finding.notes.append("issuer_cik not set; the tag names this company, so its CIK is used")
    if not raw_cik:
        finding.unresolved = "no issuer_cik, and the tag does not name this company; cannot check"
        return finding
    try:
        cik = normalize_cik(raw_cik)
    except ValueError as exc:
        finding.problems.append(str(exc))
        return finding
    finding.issuer_cik = cik
    issuer = _issuer(cik, company, client, issuers)
    accession = entry.get("accession")
    if accession:
        try:
            filing = issuer.subs.get(str(accession))
        except ValueError as exc:
            finding.problems.append(str(exc))
            return finding
        if filing is None:
            finding.problems.append(f"EDGAR has no accession number {accession} among the filings of CIK {cik}")
            return finding
    else:
        filing, why = _resolve_from_tag(entry, tag, issuer, client)
        if filing is None:
            finding.unresolved = f"accession missing; {why}"
            return finding
        finding.fills["accession"] = filing.accession
    period = expected_period(filing, issuer, client)
    finding.edgar = {"accession": filing.accession, "form": filing.form,
                     "filed": filing.filing_date.isoformat(), "period": period}

    form = entry.get("form")
    if form in (None, ""):
        finding.fills["form"] = filing.form
    elif form_key(str(form)) != form_key(filing.form):
        finding.problems.append(f"form says {form}; EDGAR has {filing.form}")

    try:
        filed = to_date(entry.get("filed"))
    except ValueError:
        finding.problems.append(f"filed says {entry.get('filed')!r}, which is not a date")
        filed = filing.filing_date
    if filed is None:
        finding.fills["filed"] = filing.filing_date
    elif filed != filing.filing_date:
        finding.problems.append(f"filed says {filed}; EDGAR's filing date is {filing.filing_date}")

    entry_period = entry.get("period")
    if period:
        if entry_period in (None, ""):
            finding.fills["period"] = period
        elif str(entry_period) != period:
            finding.problems.append(f"period says {entry_period}; by EDGAR it should be {period}")

    finding.problems.extend(_tag_problems(tag, filing, period, issuer, registered or set()))
    url = entry.get("url")
    m = ARCHIVES_URL_RE.search(str(url or ""))
    if m and m.group(2) != filing.accession.replace("-", ""):
        finding.problems.append(f"url points to another filing ({m.group(2)}), not {filing.accession}")
    return finding


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, dt.date):
        return value.isoformat()
    text = str(value)
    try:
        plain_ok = yaml.safe_load(f"k: {text}") == {"k": text} and text == text.strip() and " #" not in text
    except yaml.YAMLError:
        plain_ok = False
    return text if plain_ok else json.dumps(text, ensure_ascii=False)


def _entry_blocks(lines: list[str]) -> dict[str, tuple[int, int, int]]:
    """Tag → (first line of the entry, the line after its last line, key indent).

    Only entries that start with "- tag: X" are recognized.
    """
    blocks: dict[str, tuple[int, int, int]] = {}
    starts = []
    pattern = re.compile(r"^(?P<dash>[ \t]*-[ \t]+)tag:[ \t]*(?P<q>[\"']?)(?P<tag>[^\"'#\s]+)(?P=q)[ \t]*(?:#.*)?$")
    for i, line in enumerate(lines):
        m = pattern.match(line.rstrip("\n"))
        if m:
            starts.append((i, m.group("tag"), len(m.group("dash")), len(m.group("dash")) - len(m.group("dash").lstrip())))
    for n, (start, tag, key_indent, dash_indent) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        for j in range(start + 1, end):
            stripped = lines[j].strip()
            if stripped and not stripped.startswith("#"):
                indent = len(lines[j]) - len(lines[j].lstrip())
                if indent <= dash_indent:
                    end = j
                    break
        while end - 1 > start and (not lines[end - 1].strip() or lines[end - 1].strip().startswith("#")):
            end -= 1
        blocks[tag] = (start, end, key_indent)
    return blocks


def fill_missing_fields(text: str, fills: Mapping[str, Mapping[str, Any]]) -> str:
    """Fill only missing fields (key absent or value null); never change an existing value.

    After the edit, re-parse and confirm that everything except the filled fields is identical.
    """
    lines = text.splitlines(keepends=True)
    blocks = _entry_blocks(lines)
    inserts: list[tuple[int, list[str]]] = []
    replaces: dict[int, str] = {}
    for tag, fields in fills.items():
        if tag not in blocks:
            raise EdgarDataError(f"the sources table (sources.yml) has no \"- tag: {tag}\" line; nothing written")
        start, end, key_indent = blocks[tag]
        keys: dict[str, int] = {}
        key_re = re.compile(rf"^ {{{key_indent}}}(?P<key>[A-Za-z_][A-Za-z0-9_]*):(?P<rest>.*)$")
        for j in range(start, end):
            line = lines[j].rstrip("\n")
            if j == start:
                keys["tag"] = j
                continue
            m = key_re.match(line)
            if m:
                keys[m.group("key")] = j
        pending: dict[int, list[tuple[str, str]]] = {}
        for field, value in fields.items():
            if field not in FILL_FIELDS:
                raise ValueError(f"only the fields {FILL_FIELDS} are filled, got {field}")
            rendered = f"{' ' * key_indent}{field}: {_yaml_scalar(value)}\n"
            if field in keys:
                j = keys[field]
                rest = key_re.match(lines[j].rstrip("\n")).group("rest")
                if not re.fullmatch(r"\s*(?:null|Null|NULL|~)?\s*(?:#.*)?", rest):
                    raise EdgarDataError(f"{tag}: {field} already has a value and is not overwritten")
                replaces[j] = rendered
                continue
            anchor = start
            for name in _FIELD_ORDER[:_FIELD_ORDER.index(field)]:
                if name in keys:
                    anchor = max(anchor, keys[name])
            k = anchor + 1  # skip the anchor key's continuation lines
            while k < end and (not lines[k].strip() or len(lines[k]) - len(lines[k].lstrip()) > key_indent):
                k += 1
            while k > anchor + 1 and not lines[k - 1].strip():
                k -= 1
            pending.setdefault(k, []).append((field, rendered))
        for position, new_lines in pending.items():  # fields inserted at the same spot go in the usual field order
            new_lines.sort(key=lambda pair: _FIELD_ORDER.index(pair[0]))
            inserts.append((position, [rendered for _, rendered in new_lines]))
    out = []
    by_position: dict[int, list[str]] = {}
    for position, new_lines in inserts:
        by_position.setdefault(position, []).extend(new_lines)
    for i, line in enumerate(lines):
        out.extend(by_position.get(i, []))
        out.append(replaces.get(i, line))
    out.extend(by_position.get(len(lines), []))
    new_text = "".join(out)
    _verify_fill(text, new_text, fills)
    return new_text


def _normalized(value: Any) -> Any:
    if isinstance(value, dt.date):
        return value.isoformat()
    return value


def _verify_fill(old_text: str, new_text: str, fills: Mapping[str, Mapping[str, Any]]) -> None:
    old = yaml.safe_load(old_text) or {}
    new = yaml.safe_load(new_text) or {}
    old_entries, new_entries = old.get("sources") or [], new.get("sources") or []
    if {k: v for k, v in old.items() if k != "sources"} != {k: v for k, v in new.items() if k != "sources"}:
        raise EdgarDataError("the write would change content outside sources; aborted")
    if len(old_entries) != len(new_entries):
        raise EdgarDataError("the write would change the number of entries; aborted")
    for before, after in zip(old_entries, new_entries):
        tag = before.get("tag") if isinstance(before, dict) else None
        wanted = {k: _normalized(v) for k, v in (fills.get(tag) or {}).items()}
        for key in set(before) | set(after):
            if key in wanted:
                if before.get(key) not in (None, "") or _normalized(after.get(key)) != wanted[key]:
                    raise EdgarDataError(f"{tag}: {key} is wrong after the write; aborted")
            elif _normalized(before.get(key)) != _normalized(after.get(key)):
                raise EdgarDataError(f"the write would change {key} of {tag}; aborted")


def check_sources(path: str | os.PathLike, *, client: EdgarClient | None = None,
                  repo_root: str | os.PathLike | None = None, write: bool = False) -> SourcesReport:
    """Check accession/filed/period (and form, tag, url) of each kind: filing entry in a sources table against EDGAR.

    Read-only and report-only by default. With write=True, fill the missing accession, filed, form and period only for
    entries without mismatches, never changing existing values; the changes are returned as a unified diff
    (SourcesReport.diff).
    """
    client = client or default_client()
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    company = _company_for_sources(path.resolve(), Path(repo_root or REPO_ROOT))
    issuers: dict[str, _Issuer] = {}
    tag_lines = {tag: start + 1 for tag, (start, _, _) in _entry_blocks(text.splitlines(keepends=True)).items()}
    entries = [e for e in data.get("sources") or [] if isinstance(e, dict) and e.get("kind") == "filing"]
    registered = set()
    for entry in entries:
        try:
            registered.add(normalize_accession(entry.get("accession")))
        except ValueError:
            pass
    findings = []
    for entry in entries:
        finding = _check_entry(entry, company, client, issuers, registered)
        finding.line = tag_lines.get(finding.tag)
        findings.append(finding)
    report = SourcesReport(path=path, findings=findings)
    if write:
        fills = {f.tag: f.fills for f in findings if f.fills and not f.problems and not f.unresolved}
        if fills:
            new_text = fill_missing_fields(text, fills)
            if new_text != text:
                report.diff = "".join(difflib.unified_diff(text.splitlines(keepends=True),
                                                           new_text.splitlines(keepends=True),
                                                           fromfile=str(path), tofile=str(path)))
                _atomic_write(path, new_text.encode("utf-8"))
                report.written = True
    return report


# ---------------------------------------------------------------------------------------------------------------
# A source tag's filing, of any issuer (documents HQ asks the pipeline to supply, docs/decisions/0031)
# ---------------------------------------------------------------------------------------------------------------

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"  # SEC's ticker -> CIK table of all issuers


def cik_for_ticker(ticker: str, *, client: EdgarClient | None = None) -> str:
    """The CIK of the issuer a source tag's ticker names, from SEC's company_tickers.json (cached, and re-fetched after
    the submissions TTL like submissions). SEC writes a class of shares with a hyphen (BRK-B), a tag with a point or
    without the class (BRK.B, BRK): those resolve when every class SEC lists belongs to one issuer. Raises
    EdgarDataError when SEC lists no such ticker, or several issuers under it."""
    client = client or default_client()
    data = client.get_json(COMPANY_TICKERS_URL, max_age=client.ttl)
    rows = data.values() if isinstance(data, Mapping) else data if isinstance(data, list) else []
    ciks: dict[str, set[str]] = {}
    for row in rows:
        if isinstance(row, Mapping) and row.get("ticker") and str(row.get("cik_str") or "").isdigit():
            ciks.setdefault(str(row["ticker"]).upper(), set()).add(normalize_cik(row["cik_str"]))
    want = str(ticker).strip().upper().replace(".", "-")
    found = ciks.get(want) or {cik for name, listed in ciks.items() if name.startswith(f"{want}-") for cik in listed}
    if len(found) == 1:
        return next(iter(found))
    if not found:
        raise EdgarDataError(f"SEC's company_tickers.json lists no ticker {ticker}")
    raise EdgarDataError(f"SEC's company_tickers.json lists {ticker} under {len(found)} issuers: "
                         f"{', '.join(sorted(found))}")


def filing_for_entry(entry: Mapping[str, Any], *, company: Filer | None = None, client: EdgarClient | None = None,
                     issuers: dict[str, _Issuer] | None = None) -> tuple[Filing, _Issuer]:
    """The filing a sources.yml entry, or a bare {"tag": ...}, names on EDGAR, and its issuer (submissions and fiscal
    calendar). The issuer: issuer_cik, else the CIK in url, else this company when the tag names it, else the tag's
    ticker through SEC's company_tickers.json. The filing: accession (or the one in url), else matched as
    check-sources matches it: a periodic report by form and fiscal period in the issuer's own calendar, a current
    report by filing date and ordinal. Raises EdgarDataError with the reason when none is found."""
    client = client or default_client()
    issuers = {} if issuers is None else issuers
    text = str(entry.get("tag") or "")
    tag = parse_source_tag(text)
    folder = ARCHIVES_URL_RE.search(str(entry.get("url") or ""))
    raw_cik = entry.get("issuer_cik") or (folder.group(1) if folder else None)
    if not raw_cik and company is not None and tag is not None and tag.ticker == company.ticker:
        raw_cik = company.cik
    if not raw_cik:
        if tag is None:
            raise EdgarDataError(f"{text} is not a source tag <TICKER>-<FORM>-<PERIOD or DATE> (thesis-ci SPEC 3.3)")
        raw_cik = cik_for_ticker(tag.ticker, client=client)
    issuer = _issuer(normalize_cik(raw_cik), company, client, issuers)
    accession = entry.get("accession") or (folder.group(2) if folder else None)
    if accession:
        filing = issuer.subs.get(str(accession))
        if filing is None:
            raise EdgarDataError(f"EDGAR has no accession number {accession} among the filings of CIK "
                                 f"{issuer.subs.cik}")
        return filing, issuer
    filing, why = _resolve_from_tag(entry, tag, issuer, client)
    if filing is None:
        raise EdgarDataError(f"{why} (CIK {issuer.subs.cik}, {issuer.subs.name})")
    return filing, issuer


# ---------------------------------------------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------------------------------------------


def _emit(data: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=120).rstrip())


def _client_from_args(args: argparse.Namespace) -> EdgarClient:
    return EdgarClient(env_file=args.env_file, cache_dir=args.cache_dir, offline=args.offline, refresh=args.refresh)


def _date_arg(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"a date must be written YYYY-MM-DD, got {value!r}") from None


def _company_header(filer: Filer, subs: Submissions) -> dict[str, Any]:
    return {
        "company": filer.ticker,
        "name": subs.name,
        "cik": subs.cik,
        "filer_type": filer.type or infer_filer_type(subs),
        "fiscal_year_end": FiscalCalendar.parse(filer.fiscal_year_end or subs.fiscal_year_end).label,
    }


def _cmd_release_history(args: argparse.Namespace) -> int:
    filer = load_filer(args.company, args.repo)
    client = _client_from_args(args)
    as_of = args.as_of or today_et()
    events = release_history(filer.cik, filer.fiscal_year_end, filer_type=filer.type, years=args.years, as_of=as_of,
                             quarter=args.quarter, client=client)
    subs = submissions(filer.cik, client=client, since=_shift_years(as_of, -args.years))
    out = _company_header(filer, subs)
    out.update({
        "window": {"from": _shift_years(as_of, -args.years).isoformat(), "to": as_of.isoformat(),
                   "rule": "EDGAR filing date falls in the window"},
        "quarter": args.quarter,
        "events": [e.to_dict() for e in events],
    })
    _emit(out, args.json)
    return 0


def _cmd_next_release(args: argparse.Namespace) -> int:
    filer = load_filer(args.company, args.repo)
    client = _client_from_args(args)
    estimate = next_release(filer.cik, args.period, fiscal_year_end=filer.fiscal_year_end, filer_type=filer.type,
                            form=filer.earnings_form, announced=args.announced,
                            window=tuple(args.window) if args.window else None, as_of=args.as_of, client=client)
    out = {"company": filer.ticker}
    out.update(estimate.to_dict())
    _emit(out, args.json)
    return 0


def _print_sources_report(report: SourcesReport) -> None:
    counts = report.counts()
    print(f"{report.path}: {len(report.findings)} kind: filing entries; {counts['ok']} match EDGAR, "
          f"{counts['missing']} have fillable missing fields, {counts['mismatch']} mismatch, "
          f"{counts['unresolved']} cannot be checked")
    for finding in report.findings:
        where = f" (line {finding.line})" if finding.line else ""
        for problem in finding.problems:
            print(f"  mismatch   {finding.tag}{where}: {problem}")
        if finding.unresolved:
            print(f"  unresolved {finding.tag}{where}: {finding.unresolved}")
        for field, value in finding.fills.items():
            value = value.isoformat() if isinstance(value, dt.date) else value
            print(f"  fillable   {finding.tag}{where}: {field}: {value}")
        for note in finding.notes:
            print(f"  note       {finding.tag}{where}: {note}")
    if report.written:
        print("Written (missing fields only):")
        print(report.diff.rstrip())
    elif any(f.fills for f in report.findings):
        print("Read-only: add --write to fill the missing fields (existing values are not changed; entries with "
              "mismatches are not filled).")


def _cmd_check_sources(args: argparse.Namespace) -> int:
    client = _client_from_args(args)
    report = check_sources(args.path, client=client, repo_root=args.repo, write=args.write)
    if args.json:
        out = report.to_dict()
        out["diff"] = report.diff
        _emit(out, True)
    else:
        _print_sources_report(report)
    remaining = [f for f in report.findings if f.problems or f.unresolved or (f.fills and not report.written)]
    return 1 if remaining else 0


def _cmd_documents(args: argparse.Namespace) -> int:
    filer = load_filer(args.company, args.repo)
    client = _client_from_args(args)
    filing = submissions(filer.cik, client=client).get(args.accession)
    primary = filing.primary_document if filing else None
    docs = filing_documents(filer.cik, args.accession, client=client, primary_document=primary)
    out: dict[str, Any] = {
        "cik": filer.cik,
        "accession": normalize_accession(args.accession),
        "filing": filing.to_dict() if filing else None,
        "documents": [d.to_dict() for d in docs if args.all or d.kind in ("primary", "exhibit")],
        "skipped_xbrl_viewer_pages": sum(1 for d in docs if d.kind == "xbrl_viewer"),
    }
    if args.download:
        paths = download_filing(filer.cik, args.accession, args.download, client=client, primary_document=primary)
        out["downloaded"] = [str(p) for p in paths]
    _emit(out, args.json)
    return 0


def _cmd_submissions(args: argparse.Namespace) -> int:
    filer = load_filer(args.company, args.repo)
    client = _client_from_args(args)
    subs = submissions(filer.cik, client=client)
    forms: dict[str, int] = {}
    for f in subs.filings:
        forms[f.form] = forms.get(f.form, 0) + 1
    out = {
        "cik": subs.cik,
        "name": subs.name,
        "tickers": list(subs.tickers),
        "fiscal_year_end": subs.fiscal_year_end,
        "entity_type": subs.entity_type,
        "filer_type": infer_filer_type(subs),
        "filings": len(subs.filings),
        "first": subs.filings[0].filing_date.isoformat() if subs.filings else None,
        "last": subs.filings[-1].filing_date.isoformat() if subs.filings else None,
        "overflow_pages": [p.get("name") for p in subs.pages],
        "forms": dict(sorted(forms.items(), key=lambda kv: (-kv[1], kv[0]))[:25]),
    }
    _emit(out, args.json)
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--env-file", type=Path, default=None,
                        help=f".env file to read {UA_ENV} from (default: {ENV_FILE_ENV} or {DEFAULT_ENV_FILE})")
    common.add_argument("--cache-dir", type=Path, default=None,
                        help=f"cache directory (default: {CACHE_ENV} or ~/.cache/owners-office/edgar)")
    common.add_argument("--offline", action="store_true",
                        help="use the cache only, no network access (no User-Agent needed)")
    common.add_argument("--refresh", action="store_true", help="fetch submissions again instead of using the cache")
    common.add_argument("--repo", type=Path, default=REPO_ROOT,
                        help="public repo root (reads companies/<ticker>/thesis.yml)")
    common.add_argument("--json", action="store_true", help="output JSON (default: YAML)")
    common.add_argument("-v", "--verbose", action="store_true", help="log every request (without request headers)")

    parser = argparse.ArgumentParser(prog="python -m pipeline.edgar", description="SEC EDGAR access (decisions/0017)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("release-history", parents=[common], help="earnings events of the past few years")
    p.add_argument("company", help="ticker (a directory name under companies/) or CIK")
    p.add_argument("--years", type=int, default=HISTORY_YEARS)
    p.add_argument("--as-of", type=_date_arg, default=None, help="end of the window (default: today, US Eastern)")
    p.add_argument("--quarter", type=int, choices=(1, 2, 3, 4), default=None, help="only this fiscal quarter")
    p.set_defaults(func=_cmd_release_history)

    p = sub.add_parser("next-release", parents=[common],
                       help="(expected) release date, deadline and merge-by time for one fiscal quarter")
    p.add_argument("company")
    p.add_argument("period", help="FY<year>Q<quarter>, in the company's fiscal year")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--announced", default=None,
                       help="release date announced by the company (a YYYY-MM-DD US Eastern date, or a time with a "
                            "time zone); replaces the estimate, placeholder: false")
    group.add_argument("--window", nargs=2, metavar=("START", "END"), default=None,
                       help="release window announced by the company")
    p.add_argument("--as-of", type=_date_arg, default=None, help="date to judge from (default: today, US Eastern)")
    p.set_defaults(func=_cmd_next_release)

    p = sub.add_parser("check-sources", parents=[common],
                       help="check the accession numbers of kind: filing entries in a sources table (sources.yml)")
    p.add_argument("path", type=Path)
    p.add_argument("--write", action="store_true",
                   help="fill missing accession/filed/form/period (existing values are not changed)")
    p.set_defaults(func=_cmd_check_sources)

    p = sub.add_parser("documents", parents=[common],
                       help="file list of one filing; can download the primary document and EX-99.x")
    p.add_argument("company")
    p.add_argument("accession")
    p.add_argument("--download", type=Path, default=None, help="download into this directory (<dir>/<accession>/)")
    p.add_argument("--all", action="store_true", help="list all files (default: only the primary document and EX-99.x)")
    p.set_defaults(func=_cmd_documents)

    p = sub.add_parser("submissions", parents=[common], help="submissions summary (including continuation pages)")
    p.add_argument("company")
    p.set_defaults(func=_cmd_submissions)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    try:
        return args.func(args)
    except MissingUserAgent as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except EdgarError as exc:
        print(f"EDGAR: {exc}", file=sys.stderr)
        return 3
    except (ValueError, OSError, yaml.YAMLError) as exc:
        print(f"error: {redact(str(exc))}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
