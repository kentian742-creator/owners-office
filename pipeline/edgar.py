"""SEC EDGAR 访问 / SEC EDGAR access（STATUS T18；docs/decisions/0013、0017）。

只读 EDGAR 的公开数据：submissions（含 filings.files 的续页）、文件索引 index.json、主文档与附件 EX-99.x。
由此给出业绩事件、release_history、下一次发布日的估计与预注册的截止时间，以及来源表登记号的核对。

- **User-Agent：** 环境变量 SEC_USER_AGENT；没有就读 .env（默认工作区根目录，即本仓库的上一级；
  OWNERS_OFFICE_ENV_FILE 或 --env-file 可改）。两处都没有就拒绝联网。这个值不打印、不记日志、不进缓存、
  不写进任何文件，报错信息里也没有：本模块的异常与日志一律先把它换成 [SEC_USER_AGENT]（decisions/0013）。
- **频率：** 同一进程里的请求共用一个限速器，每秒不超过 5 次（SEC 的上限是 10 次）；429 与 5xx 按指数退避重试，
  尊重 Retry-After；每次请求有超时。只访问 SEC 的两个主机（data.sec.gov、www.sec.gov）。
- **TLS：** SSL_CERT_FILE > certifi > 系统默认 > 常见的系统证书文件（python.org 的 macOS 版 Python 不带 CA 证书）。
- **缓存：** 默认 ~/.cache/owners-office/edgar（三个仓库之外；OWNERS_OFFICE_EDGAR_CACHE 或 --cache-dir 可改）。
  Archives 下的文件入库后不再变，永久缓存；submissions 默认一小时后重新取。缓存里只有 SEC 的响应正文与取数时间。
- **业绩事件：** 国内发行人是带第 2.02 项的 8-K，10-Q／10-K 入库或满 5 个工作日收口（以先到者为准）；
  外国发行人（PDD）是业绩 6-K，按附件 EX-99.1 的新闻稿标题认出。期间按公司自己的财年写 FY<年>Q<季>。
- **下一次发布日：** 按提示词 15A 的规则估计（公布的日期 > 公布的窗口 > release_history > 最近一期的间隔），
  截止时间是发布日前一天 23:59:59 美东，合并时限是截止前 72 个实际经过的小时。
- 本模块不接任何行情源（C-NO-PRICE-FEED），也不调用模型（C-LLM-ENTRY）。

命令行（在仓库根目录）：

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
ENV_FILE_ENV = "OWNERS_OFFICE_ENV_FILE"  # .env 的位置；默认工作区根目录
CACHE_ENV = "OWNERS_OFFICE_EDGAR_CACHE"  # 缓存目录；默认 ~/.cache/owners-office/edgar
DEFAULT_ENV_FILE = WORKSPACE_ROOT / ".env"

DATA_BASE = "https://data.sec.gov"
ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"
ALLOWED_HOSTS = frozenset({"data.sec.gov", "www.sec.gov"})

MAX_REQUESTS_PER_SECOND = 5.0  # 本项目的上限；SEC 的公平访问上限是 10
DEFAULT_TIMEOUT = 30.0
DEFAULT_ATTEMPTS = 5
DEFAULT_BACKOFF = 1.0
MAX_BACKOFF = 60.0
SUBMISSIONS_TTL = 3600.0  # submissions 缓存一小时；Archives 下的文件永久缓存

NY = ZoneInfo("America/New_York")
UTC = dt.timezone.utc

MERGE_HOURS = 72  # 预注册至少在截止时间前 72 小时合并（15A）
ESTIMATE_LEAD_DAYS = 3  # 没有公布日期时，历年最早的月日往前数 3 个日历日（15A）
HISTORY_YEARS = 3  # release_history：过去三年同一财季

DOMESTIC = "domestic"
FOREIGN = "foreign_private_issuer"
ANNUAL_FORMS = frozenset({"10-K", "10-K/A", "10-KT", "10-KT/A", "20-F", "20-F/A", "40-F", "40-F/A"})
QUARTERLY_FORMS = frozenset({"10-Q", "10-Q/A", "10-QT", "10-QT/A"})
PERIODIC_FORMS = ANNUAL_FORMS | QUARTERLY_FORMS
CLOSING_FORMS = frozenset({"10-Q", "10-K"})  # 国内业绩事件的收口文件（不含修正版）
SYSTEM_CA_BUNDLES = (
    "/etc/ssl/cert.pem",  # macOS、Alpine
    "/etc/ssl/certs/ca-certificates.crt",  # Debian、Ubuntu
    "/etc/pki/tls/certs/ca-bundle.crt",  # RHEL、Fedora
)

ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")
PERIOD_RE = re.compile(r"^FY(\d{4})(?:Q([1-4]))?$")
_OVERFLOW_PAGE_RE = re.compile(r"^CIK\d{10}-submissions-\d{3}\.json$")
_DOC_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_R_PAGE_RE = re.compile(r"^R\d+\.htm$", re.I)  # XBRL 查看器生成的页面，不下载
_EX99_RE = re.compile(r"(?i)(?:ex|exhibit)[-_ ]?99(?:[-_.d](\d{1,2})|(\d))?(?!\d)")  # ex99-1、ex991、ex99d1 ……

LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------------------------------------------
# User-Agent：只在内存里；日志与异常里一律换成占位符
# ---------------------------------------------------------------------------------------------------------------

REDACTED = "[SEC_USER_AGENT]"
_SENSITIVE: set[str] = set()
_SENSITIVE_LOCK = threading.Lock()
_EMAIL_RE = re.compile(r"[^\s<>()\"',;]+@[^\s<>()\"',;]+")


def _remember_sensitive(value: str) -> None:
    """记住要从输出里去掉的值：整个 User-Agent，以及其中的邮箱。"""
    value = value.strip()
    if not value:
        return
    with _SENSITIVE_LOCK:
        _SENSITIVE.add(value)
        _SENSITIVE.update(m.group(0) for m in _EMAIL_RE.finditer(value))


def redact(text: str) -> str:
    """把本进程见过的 User-Agent（及其中的邮箱）换成占位符。"""
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
    """EDGAR 访问或数据出错。信息先去掉 User-Agent 再保存。"""

    def __init__(self, message: str):
        super().__init__(redact(str(message)))


class MissingUserAgent(EdgarError):
    """没有可用的 SEC_USER_AGENT：拒绝联网。"""


class EdgarHTTPError(EdgarError):
    def __init__(self, message: str, *, url: str, status: int | None = None):
        super().__init__(message)
        self.url = redact(url)
        self.status = status


class EdgarNotFound(EdgarHTTPError):
    """404：EDGAR 上没有这个地址。"""


class EdgarAccessDenied(EdgarHTTPError):
    """403 或 SEC 的拦截页：User-Agent 不合格或请求太快。"""


class EdgarOffline(EdgarError):
    """离线模式下缓存里没有需要的文件。"""


class EdgarDataError(EdgarError):
    """EDGAR 返回的内容与预期不符，或数据不够做判断。"""


def _valid_user_agent(value: str) -> bool:
    """SEC 要求写明联系方式：一行可打印的 ASCII，含邮箱。"""
    return bool(value) and value.isascii() and value.isprintable() and "@" in value and len(value) <= 300


def _read_env_value(path: Path, key: str) -> str | None:
    """从 .env 里读一个键（KEY=VALUE，可带 export、引号与行尾注释）。不回显文件内容。"""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise MissingUserAgent(f"读不了 {path}（{type(exc).__name__}）") from None
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
        value = rest  # 同一个键写了几次，以最后一次为准（与 shell 一致）
    return value or None


def load_user_agent(env: Mapping[str, str] | None = None, env_file: str | os.PathLike | None = None) -> str:
    """SEC_USER_AGENT：先看环境变量，再看 .env。都没有就抛 MissingUserAgent。"""
    env = os.environ if env is None else env
    value = (env.get(UA_ENV) or "").strip()
    where = f"环境变量 {UA_ENV}"
    if not value:
        path = Path(env_file) if env_file else Path(env.get(ENV_FILE_ENV) or DEFAULT_ENV_FILE)
        value = (_read_env_value(path, UA_ENV) or "").strip()
        where = str(path)
        if not value:
            raise MissingUserAgent(
                f"没有 SEC 的 User-Agent：设环境变量 {UA_ENV}，或在 {path} 里写一行 {UA_ENV}=<项目名 联系邮箱>"
                "（docs/decisions/0013）。没有它不访问 EDGAR。"
            )
    _remember_sensitive(value)
    if not _valid_user_agent(value):
        raise MissingUserAgent(f"{where} 的 {UA_ENV} 不能用作 User-Agent：应当是一行可打印的 ASCII 文本并含联系邮箱（值不显示）")
    return value


# ---------------------------------------------------------------------------------------------------------------
# HTTP：TLS、限速、重试、缓存
# ---------------------------------------------------------------------------------------------------------------


def tls_context() -> ssl.SSLContext:
    """证书校验始终打开。CA 证书依次取 SSL_CERT_FILE、certifi、系统默认、常见的系统证书文件。"""
    cafile = os.environ.get("SSL_CERT_FILE")
    if cafile:
        return ssl.create_default_context(cafile=cafile)
    try:
        import certifi  # requirements.txt；没装时退回系统证书
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
    """相邻两次请求至少隔 1/max_per_second 秒；任何一秒之内不超过 max_per_second 次。"""

    def __init__(self, max_per_second: float = MAX_REQUESTS_PER_SECOND, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        if not 0 < max_per_second <= MAX_REQUESTS_PER_SECOND:
            raise ValueError(f"请求频率必须在 0 到 {MAX_REQUESTS_PER_SECOND:g} 次/秒之间，收到 {max_per_second}")
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

# (url, 请求头, 超时) -> (状态码, 响应头, 正文)。网络错误抛 OSError 或 http.client.HTTPException。
Transport = Callable[[str, Mapping[str, str], float], "tuple[int, Mapping[str, str], bytes]"]


class UrllibTransport:
    """标准库 urllib 的传输层。HTTP 错误也作为 (状态码, 响应头, 正文) 返回，由客户端统一处理。"""

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
            raise EdgarAccessDenied("EDGAR 返回的是网页而不是 JSON（多半是 SEC 的拦截页：检查 User-Agent 与请求频率）",
                                    url="", status=None) from None
        raise EdgarDataError("EDGAR 返回的内容不是 JSON") from None


def _is_cert_error(exc: BaseException) -> bool:
    if isinstance(exc, ssl.SSLCertVerificationError):
        return True
    return isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, ssl.SSLCertVerificationError)


def _atomic_write(path: Path, data: bytes) -> None:
    """先写临时文件再改名；已有文件保留原来的权限，新文件 0644（mkstemp 默认是 0600）。"""
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
    """带限速、重试、超时与磁盘缓存的 EDGAR 客户端。

    联网模式下构造时就要求 User-Agent（参数 user_agent，否则环境变量或 .env），拿不到就抛 MissingUserAgent。
    离线模式（offline=True）只读缓存，不需要 User-Agent，缓存里没有就抛 EdgarOffline。
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
                raise MissingUserAgent("传入的 User-Agent 不能用：应当是一行可打印的 ASCII 文本并含联系邮箱（值不显示）")
            self._ua = user_agent.strip()
        elif not offline:
            self._ua = load_user_agent(env_file=env_file)

    def __repr__(self) -> str:
        return (f"EdgarClient(cache_dir={str(self.cache_dir)!r}, offline={self.offline}, "
                f"user_agent={'<set>' if self._ua else '<none>'})")

    # -- 读取 ------------------------------------------------------------------------------------------------------

    def get_bytes(self, url: str, *, max_age: float | None = None,
                  validate: Callable[[bytes], None] | None = None) -> bytes:
        """取一个地址的正文。max_age=None 表示内容不会变（Archives），缓存永久有效。"""
        path = self._cache_path(url)
        cached = self._cache_read(path, max_age)
        if cached is not None:
            try:
                if validate:
                    validate(cached)
                LOG.debug("缓存命中 %s", url)
                return cached
            except EdgarError:
                if self.offline:
                    raise
                LOG.warning("缓存内容无效，重新取：%s", url)
        if self.offline:
            raise EdgarOffline(f"离线模式，缓存里没有：{url}（缓存目录 {self.cache_dir}）")
        body = self._fetch(url)
        if validate:
            try:
                validate(body)
            except EdgarHTTPError as exc:
                raise type(exc)(str(exc) + f"：{url}", url=url, status=exc.status) from None
        self._cache_write(path, url, body)
        return body

    def get_json(self, url: str, *, max_age: float | None = None) -> Any:
        return json.loads(self.get_bytes(url, max_age=max_age, validate=_validate_json))

    # -- 缓存 ------------------------------------------------------------------------------------------------------

    def _cache_path(self, url: str) -> Path:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS:
            raise EdgarError(f"本模块只访问 SEC 的 {sorted(ALLOWED_HOSTS)}，收到 {url}")
        segments = [s for s in parts.path.split("/") if s]
        if not segments or any(s in (".", "..") or "\\" in s or "\x00" in s for s in segments):
            raise EdgarError(f"看不懂的 EDGAR 地址：{url}")
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
        # 只存正文、地址与取数时间；请求头（含 User-Agent）从不写盘
        _atomic_write(path, body)
        meta = {"url": url, "fetched_at": dt.datetime.fromtimestamp(self._now(), UTC).isoformat(timespec="seconds")}
        _atomic_write(self._meta_path(path), json.dumps(meta).encode())

    # -- 网络 ------------------------------------------------------------------------------------------------------

    def _backoff_delay(self, attempt: int, retry_after: float | None) -> float:
        delay = retry_after if retry_after is not None else self.backoff * (2 ** (attempt - 1))
        delay = min(MAX_BACKOFF, max(0.0, delay))
        if self.jitter:
            delay *= 1 + self.jitter * random.random()
        return delay

    def _fetch(self, url: str) -> bytes:
        if not self._ua:
            raise MissingUserAgent(f"没有 SEC 的 User-Agent，不访问 EDGAR：{url}")
        if self._transport is None:
            self._transport = UrllibTransport()
        headers = {"User-Agent": self._ua, "Accept-Encoding": "gzip, deflate"}
        last = "没有响应"
        last_status: int | None = None
        for attempt in range(1, self.max_attempts + 1):
            self.limiter.acquire()
            retry_after = None
            try:
                status, response_headers, body = self._transport(url, headers, self.timeout)
            except (OSError, http.client.HTTPException) as exc:
                if _is_cert_error(exc):
                    raise EdgarError(f"TLS 证书校验失败：{url}。装上 certifi（requirements.txt），"
                                     "或设 SSL_CERT_FILE 指向 CA 证书文件。") from None
                last = f"{type(exc).__name__}: {redact(str(exc))[:200]}"
            else:
                response_headers = {str(k).lower(): v for k, v in dict(response_headers).items()}
                last_status = status
                if status == 200:
                    LOG.info("GET %s -> 200", url)
                    try:
                        return _decode_body(body, response_headers)
                    except (OSError, EOFError, zlib.error) as exc:
                        last = f"解压失败（{type(exc).__name__}）"
                elif status == 404:
                    raise EdgarNotFound(f"EDGAR 没有这个地址（404）：{url}", url=url, status=404)
                elif status in (401, 403):
                    raise EdgarAccessDenied(
                        f"EDGAR 拒绝访问（HTTP {status}）：{url}。检查 {UA_ENV} 是否写了项目名与联系邮箱、"
                        "请求是否过快（docs/decisions/0013）", url=url, status=status)
                elif status == 429 or 500 <= status < 600:
                    last = f"HTTP {status}"
                    retry_after = _retry_after(response_headers.get("retry-after"))
                else:
                    raise EdgarHTTPError(f"EDGAR 返回 HTTP {status}：{url}", url=url, status=status)
            if attempt < self.max_attempts:
                delay = self._backoff_delay(attempt, retry_after)
                LOG.warning("EDGAR 请求没有成功（%s），%.1f 秒后重试（%d/%d）：%s", last, delay, attempt,
                            self.max_attempts, url)
                self._sleep(delay)
        raise EdgarHTTPError(f"EDGAR 请求 {self.max_attempts} 次都没有成功（最后一次：{last}）：{url}",
                             url=url, status=last_status)


@functools.lru_cache(maxsize=1)
def default_client() -> EdgarClient:
    """库函数不传 client 时用它：User-Agent 取自环境变量或工作区根目录的 .env。"""
    return EdgarClient()


# ---------------------------------------------------------------------------------------------------------------
# submissions 与 filing
# ---------------------------------------------------------------------------------------------------------------


def normalize_cik(value: Any) -> str:
    text = str(value).strip()
    if text.upper().startswith("CIK"):
        text = text[3:]
    if not text.isdigit() or len(text) > 10:
        raise ValueError(f"CIK 应当是最多 10 位数字，收到 {value!r}")
    return text.zfill(10)


def normalize_accession(value: Any) -> str:
    text = str(value).strip()
    if re.fullmatch(r"\d{18}", text):
        text = f"{text[:10]}-{text[10:12]}-{text[12:]}"
    if not ACCESSION_RE.match(text):
        raise ValueError(f"登记号应当写成 0000000000-00-000000，收到 {value!r}")
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
    """EDGAR 的接收时间 → 带时区的 UTC 时间。

    submissions 的 acceptanceDateTime 是真正的 UTC（结尾的 Z 没错，见 inputs/edgar/acceptance_timezone.md）；
    SGML 头的 YYYYMMDDHHMMSS 与索引页的 Accepted 是美东时间。
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
    """submissions 里的一行。"""

    cik: str  # 列出它的发行人（submissions 的主人），不一定是登记号前缀里的申报代理
    accession: str
    form: str
    filing_date: dt.date
    report_date: dt.date | None = None
    acceptance_datetime: str | None = None  # EDGAR 原文（UTC）
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
    """submissions 的列式数据 → 逐行字典。"""
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
    fiscal_year_end: str | None  # EDGAR 的 MMDD，如 "1231"、"0630"
    entity_type: str | None
    category: str | None
    filings: list[Filing]  # 按 filing date、接收时间排序
    pages: list[dict[str, Any]]  # filings.files 的续页说明
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
    """发行人的全部申报：recent 块加 filings.files 的续页。给了 since 时，只取结束日期不早于它的续页。"""
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
            raise EdgarDataError(f"CIK {cik10} 的 submissions 列了看不懂的续页 {name!r}")
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
    """最近几年报 10-K/10-Q 的是国内发行人；只报 20-F/40-F/6-K 的是外国私人发行人。"""
    recent = subs.filings[-600:]
    if any(f.form in ("10-K", "10-Q") for f in recent):
        return DOMESTIC
    if any(f.form in ("20-F", "40-F", "6-K") for f in recent):
        return FOREIGN
    return DOMESTIC


# ---------------------------------------------------------------------------------------------------------------
# 文件索引与文档
# ---------------------------------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Document:
    name: str
    url: str
    kind: str  # primary | exhibit | xbrl_viewer | other
    exhibit: str | None = None  # EX-99、EX-99.1 ……（只认 EX-99 系列）
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
    """一份申报的文件清单（index.json）。主文档取自 submissions 的 primaryDocument。"""
    client = client or default_client()
    cik10 = normalize_cik(cik)
    accession = normalize_accession(accession)
    folder = f"{ARCHIVES_BASE}/{int(cik10)}/{accession.replace('-', '')}"
    data = client.get_json(f"{folder}/index.json")
    items = ((data.get("directory") or {}).get("item")) or []
    docs = []
    for item in items:
        name = str(item.get("name") or "")
        if not _DOC_NAME_RE.match(name):
            continue
        kind, exhibit = classify_document(name, primary_document)
        size = item.get("size")
        size = int(size) if isinstance(size, int) or (isinstance(size, str) and size.isdigit()) else None
        docs.append(Document(name=name, url=f"{folder}/{name}", kind=kind, exhibit=exhibit, size=size))
    return docs


def download_filing(cik: Any, accession: str, dest: str | os.PathLike, *, client: EdgarClient | None = None,
                    primary_document: str | None = None, exhibit_prefixes: Sequence[str] = ("EX-99",)) -> list[Path]:
    """下载主文档与附件（默认只要 EX-99.x）到 dest/<登记号>/。XBRL 查看器页面 R<n>.htm 永远不下载。"""
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
    """HTML → 按段落分行的纯文本（只看开头 limit 个字节就够找标题和落款）。

    源码里的换行只是空白（标题常被折成两行）；段落类标签才分行；行内标签（span、font、b……）直接去掉，
    不补空格（“202<span>5</span>”是 2025）。
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
    """6-K 附件里的季度业绩新闻稿。"""

    document: str
    title: str
    quarter: int | None
    year: int | None
    dateline: dt.date | None


def find_results_title(text: str) -> ResultsRelease | None:
    """在新闻稿开头找“<公司> Announces <某季度> …… Results”一类标题和紧随其后的落款日期。

    “to/will report …”（预告发布日期的公告）不算；股东大会结果之类没有“quarter”的也不算。
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
    """这份 6-K 是不是季度业绩：看第一个 EX-99 附件（没有附件时看 6-K 正文）的新闻稿标题。"""
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
# 财年与工作日
# ---------------------------------------------------------------------------------------------------------------


def _month_end(year: int, month: int) -> dt.date:
    return dt.date(year, month, calendar.monthrange(year, month)[1])


def _snap_to_month_end(day: dt.date) -> dt.date:
    """52/53 周财年的季末落在月末前后几天：15 日以前算上个月末，其余算本月末。"""
    if day.day <= 15:
        first = day.replace(day=1)
        previous = first - dt.timedelta(days=1)
        return _month_end(previous.year, previous.month)
    return _month_end(day.year, day.month)


def parse_period(value: str) -> tuple[int, int | None]:
    m = PERIOD_RE.match(str(value).strip())
    if not m:
        raise ValueError(f"期间应当写成 FY<年>Q<季> 或 FY<年>，收到 {value!r}")
    return int(m.group(1)), (int(m.group(2)) if m.group(2) else None)


@dataclasses.dataclass(frozen=True)
class FiscalCalendar:
    """公司自己的财年：财年以结束那一年命名（MSFT 截至 2026-06-30 的财年是 FY2026），季末取月末。"""

    month: int
    day: int = 31

    @classmethod
    def parse(cls, value: Any) -> FiscalCalendar:
        m = re.fullmatch(r"-*(\d{1,2})-?(\d{2})", str(value).strip())
        if not m:
            raise ValueError(f"财年年末应当写成 MM-DD（如 12-31、06-30），收到 {value!r}")
        month, day = int(m.group(1)), int(m.group(2))
        if not (1 <= month <= 12 and 1 <= day <= 31):
            raise ValueError(f"财年年末应当写成 MM-DD（如 12-31、06-30），收到 {value!r}")
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
        """一个季末（容许 52/53 周财年的几天偏差）属于哪个财年的第几季。"""
        snapped = _snap_to_month_end(day)
        offset = (snapped.month - self.month) % 12
        if offset % 3:
            raise ValueError(f"{day} 不是财年年末为 {self.label} 的公司的季末")
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
        _observed(dt.date(year, 1, 1)),  # 元旦
        _nth_weekday(year, 1, 0, 3),  # 马丁·路德·金纪念日
        _nth_weekday(year, 2, 0, 3),  # 总统日
        _last_weekday(year, 5, 0),  # 阵亡将士纪念日
        _observed(dt.date(year, 7, 4)),  # 独立日
        _nth_weekday(year, 9, 0, 1),  # 劳工节
        _nth_weekday(year, 10, 0, 2),  # 哥伦布日
        _observed(dt.date(year, 11, 11)),  # 退伍军人节
        _nth_weekday(year, 11, 3, 4),  # 感恩节
        _observed(dt.date(year, 12, 25)),  # 圣诞节
    ]
    if year >= 2021:
        days.append(_observed(dt.date(year, 6, 19)))  # 六月节
    return days


# 行政令临时关闭联邦政府的日子（EDGAR 同样不收件）；只影响历史上的工作日计算。
EXTRA_CLOSURES = frozenset({
    dt.date(2018, 12, 5), dt.date(2018, 12, 24), dt.date(2019, 12, 24), dt.date(2020, 12, 24),
    dt.date(2024, 12, 24), dt.date(2025, 1, 9), dt.date(2025, 12, 24), dt.date(2025, 12, 26),
})


@functools.lru_cache(maxsize=64)
def us_federal_holidays(year: int) -> frozenset[dt.date]:
    """美国联邦法定假日（遇周六提前到周五、遇周日顺延到周一；次年元旦可能落在本年 12 月 31 日）。"""
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
    """发布日前一天 23:59:59 美东；偏移取那一刻实际适用的（夏令时 -04:00，标准时 -05:00）。"""
    day = release_date - dt.timedelta(days=1)
    return dt.datetime.combine(day, dt.time(23, 59, 59), tzinfo=NY)


def merge_by(deadline: dt.datetime, hours: int = MERGE_HOURS) -> dt.datetime:
    """截止前 hours 个实际经过的小时（先换成 UTC 再减，跨夏令时切换也对），以美东时间表示。"""
    if deadline.tzinfo is None:
        raise ValueError("截止时间必须带时区")
    return (deadline.astimezone(UTC) - dt.timedelta(hours=hours)).astimezone(NY)


def _shift_years(day: dt.date, years: int) -> dt.date:
    try:
        return day.replace(year=day.year + years)
    except ValueError:  # 2 月 29 日
        return day.replace(year=day.year + years, day=28)


def today_et() -> dt.date:
    return dt.datetime.now(NY).date()


# ---------------------------------------------------------------------------------------------------------------
# 业绩事件与 release_history
# ---------------------------------------------------------------------------------------------------------------


@dataclasses.dataclass
class EarningsEvent:
    """一次业绩事件：国内发行人的业绩 8-K（第 2.02 项），外国发行人的业绩 6-K。"""

    cik: str
    period: str  # 公司财年的 FY<年>Q<季>
    period_end: dt.date
    filing: Filing
    release_date: dt.date  # 首次公开的美东日期（发布日）
    release_basis: str  # acceptance | report_date | closing | dateline | filing_date
    closing: Filing | None = None  # 国内：同一期间的 10-Q/10-K
    closes_on: dt.date | None = None
    closed_by: str | None = None  # 10-Q | 10-K | T+5 | 6-K
    exhibit: str | None = None  # 6-K：新闻稿所在的文件
    title: str | None = None  # 6-K：新闻稿标题
    dateline: dt.date | None = None  # 6-K：新闻稿落款日期
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
    """业绩 8-K 的发布日：接收时间的美东日期；只有 2.02／9.01 两项时，报告日（公告日）早 1–4 天的取报告日。

    伯克希尔周六发布、下周才交 8-K，报告日就是那个周六；带别的项目（5.02、7.01……）的 8-K，报告日是最早那件事的日期，
    不是发布日（APP 2023-11-08、MSFT 2025-10-29 都是这样）。
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
            periodic.setdefault(label, f)  # 同一期间取最早入库的一份
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
            events[label].notes.append(f"同一期间另有第 2.02 项 8-K {f.accession}（{f.filing_date}）")
            continue
        event = EarningsEvent(cik=subs.cik, period=label, period_end=period_end, filing=f, release_date=release,
                              release_basis=basis)
        closing = periodic.get(label)
        closing_et = closing.accepted_et if closing else None
        if closing_et and closing_et.date() < event.release_date:
            # 10-Q/10-K 比 8-K 先入库（伯克希尔）：业绩最早在 EDGAR 上公开的是定期报告
            event.release_date, event.release_basis = closing_et.date(), "closing"
            event.notes.append(f"{closing.form} 比 8-K 先入库，发布日取它的接收日期")
        t_plus_5 = add_business_days(f.filing_date, 5)
        event.closing = closing
        if closing and closing.filing_date <= t_plus_5:
            event.closes_on, event.closed_by = max(f.filing_date, closing.filing_date), closing.form
        else:
            event.closes_on, event.closed_by = t_plus_5, "T+5"
        if closing is None:
            event.notes.append("还没有这一期的 10-Q/10-K")
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
            notes.append(f"标题写的是第 {release_info.quarter} 季度，期间按标题取，而不是发布日前最近的季末")
            period_end = day
            fiscal_year, quarter = cal.period_of(day)
        if release_info.year and cal.month == 12 and release_info.year != fiscal_year:
            notes.append(f"标题的年份 {release_info.year} 与推出的财年 FY{fiscal_year} 不一致")
        label = f"FY{fiscal_year}Q{quarter}"
        if f.form != "6-K":
            if label in events:
                events[label].amendments.append(f)
            continue
        if label in events:
            events[label].related.append(f)
            events[label].notes.append(f"同一期间另有业绩 6-K {f.accession}（{f.filing_date}）")
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
        raise EdgarDataError(f"CIK {subs.cik} 没有财年年末：请传 fiscal_year_end（thesis.yml 的 filer.fiscal_year_end）")
    cal = FiscalCalendar.parse(value)
    if fiscal_year_end and subs.fiscal_year_end:
        edgar = FiscalCalendar.parse(subs.fiscal_year_end)
        if edgar.month != cal.month:
            LOG.warning("CIK %s：传入的财年年末 %s 与 EDGAR 的 %s 不一致，按传入的算", subs.cik, cal.label, edgar.label)
    return cal


def earnings_events(cik: Any, fiscal_year_end: Any = None, *, filer_type: str | None = None,
                    since: dt.date | None = None, until: dt.date | None = None, client: EdgarClient | None = None,
                    subs: Submissions | None = None) -> list[EarningsEvent]:
    """since..until（按 EDGAR filing date，含两端）之间的业绩事件，按入库先后排列。"""
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
    """过去 years 年（EDGAR filing date 落在 as_of 之前 years 年之内）的业绩事件；quarter 只留那个财季。"""
    as_of = as_of or today_et()
    since = _shift_years(as_of, -years)
    events = earnings_events(cik, fiscal_year_end, filer_type=filer_type, since=since, until=as_of, client=client)
    if quarter is not None:
        events = [e for e in events if e.quarter == quarter]
    return events


# ---------------------------------------------------------------------------------------------------------------
# 下一次发布日与预注册截止时间（提示词 15A）
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
    history_window: tuple[dt.date, dt.date] | None = None  # 历年同一财季的发布日换到本期所在年份
    history: list[EarningsEvent] = dataclasses.field(default_factory=list)
    event: EarningsEvent | None = None  # 已经发布时
    notes: list[str] = dataclasses.field(default_factory=list)

    def prereg_header(self) -> dict[str, Any]:
        """预注册文件头里流水线负责的部分：event 与 deadline（schema: prereg.schema.json）。"""
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
    """公布的发布日：日期按美东日期理解；带时区的时刻换成美东日期（例如北京时间的早晨是美东的前一天晚上）。"""
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
    """没有公布日期时的占位发布日（15A）。返回 (日期, 依据, 历年窗口)。

    有 release_history：取历年同一财季里月日最早的一次，换到本期所在年份，往前数 3 个日历日，
    遇周末或联邦假日继续往前取工作日。没有：最近一期发布日距季末的天数套到本季，再减 3 天。
    """
    lead = dt.timedelta(days=ESTIMATE_LEAD_DAYS)
    if same_quarter:
        shifted = sorted(((_shift_years(e.release_date, fiscal_year - e.fiscal_year), e) for e in same_quarter),
                         key=lambda pair: (pair[0], pair[1].period))
        earliest, source = shifted[0]
        expected = previous_business_day(earliest - lead)
        basis = (f"release_history：同一财季历年月日最早的是 {source.period} 的 {source.release_date}，"
                 f"换到 {earliest.year} 年为 {earliest}；往前 {ESTIMATE_LEAD_DAYS} 个日历日并退到工作日 → {expected}")
        return expected, basis, (shifted[0][0], shifted[-1][0])
    if latest is None:
        raise EdgarDataError("没有同一财季的发布记录，也没有最近一期的发布日，无法估计；请用 --announced 或 --window")
    lag = (latest.release_date - latest.period_end).days
    expected = previous_business_day(period_end + dt.timedelta(days=lag) - lead)
    basis = (f"没有同一财季的发布记录：最近一期 {latest.period} 在季末后第 {lag} 天发布，"
             f"套到本季（季末 {period_end}）再往前 {ESTIMATE_LEAD_DAYS} 天并退到工作日 → {expected}")
    return expected, basis, None


def next_release(cik: Any, period: str, *, fiscal_year_end: Any = None, filer_type: str | None = None,
                 form: str | None = None, announced: Any = None,
                 window: tuple[Any, Any] | None = None, as_of: dt.date | None = None,
                 client: EdgarClient | None = None) -> ReleaseEstimate:
    """一个财季的（预计）发布日、预注册截止时间与合并时限。

    优先级：已在 EDGAR 上发布 > 公司公布的日期（announced，placeholder: false）> 公司公布的窗口里最早的工作日
    > release_history（过去三个财年的同一财季）> 最近一期的间隔。后三者 placeholder: true。
    """
    client = client or default_client()
    as_of = as_of or today_et()
    fiscal_year, quarter = parse_period(period)
    if quarter is None:
        raise ValueError(f"预注册按季度登记，期间要写到季度（FY2026Q3），收到 {period!r}")
    # 续页只要覆盖过去三个财年；再往前一年留余量，免得财年与日历年错开
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
        basis = f"已在 EDGAR 上发布：{released.form} {released.accession}（{released.release_date}）"
    elif announced is not None:
        expected, placeholder, status = parse_release_date(announced), False, "announced"
        basis = f"公司公布的发布日 {expected}"
    elif window is not None:
        start, end = parse_release_date(window[0]), parse_release_date(window[1])
        if end < start:
            raise ValueError(f"发布窗口的起点 {start} 晚于终点 {end}")
        expected = next_business_day(start)
        if expected > end:
            raise ValueError(f"公布的窗口 {start}..{end} 里没有工作日")
        placeholder, status = True, "announced_window"
        basis = f"公司公布的发布窗口 {start}..{end} 里最早的工作日"
    else:
        expected, basis, estimated_window = estimate_release(fiscal_year, period_end, history, latest)
        placeholder, status = True, "estimated"
        if len(history) < HISTORY_YEARS:
            notes.append(f"同一财季只找到 {len(history)} 次发布（应有 {HISTORY_YEARS} 次）")
    if not released and history:
        shifted = sorted(_shift_years(e.release_date, fiscal_year - e.fiscal_year) for e in history)
        estimated_window = estimated_window or (shifted[0], shifted[-1])

    deadline = prereg_deadline(expected)
    merge_time = merge_by(deadline)
    now_et = dt.datetime.now(NY) if as_of == today_et() else dt.datetime.combine(as_of, dt.time(0, 0), tzinfo=NY)
    if not released:
        if expected <= period_end:
            notes.append(f"发布日 {expected} 不晚于季末 {period_end}，请核对")
        if as_of >= expected:
            notes.append(f"{as_of} 已到（预计）发布日 {expected}，EDGAR 上还没有这一期的业绩文件")
        elif now_et > merge_time:
            notes.append(f"合并时限 {iso(merge_time)} 已过")
    return ReleaseEstimate(
        cik=subs.cik, period=f"FY{fiscal_year}Q{quarter}", period_end=period_end, form=form, status=status,
        expected_release=expected, placeholder=placeholder, basis=basis, deadline=deadline, merge_by=merge_time,
        history_window=estimated_window, history=history, event=released, notes=notes,
    )


# ---------------------------------------------------------------------------------------------------------------
# 公司 → CIK（读 thesis.yml 的 filer）
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
    """公司代码 → companies/<代码>/thesis.yml 的 filer 块；也可以直接给 10 位以内的 CIK。"""
    text = str(company).strip()
    if re.fullmatch(r"(?i)(?:CIK)?\d{1,10}", text):
        return Filer(ticker=None, cik=normalize_cik(text))
    ticker = text.upper()
    path = Path(repo_root or REPO_ROOT) / "companies" / ticker / "thesis.yml"
    if not path.is_file():
        raise ValueError(f"找不到 {path}：公司写 companies/ 下的代码，或者直接写 CIK")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    filer = data.get("filer") or {}
    if not filer.get("cik"):
        raise ValueError(f"{path} 没有 filer.cik")
    return Filer(
        ticker=str(data.get("company") or ticker),
        cik=normalize_cik(filer["cik"]),
        type=filer.get("type"),
        fiscal_year_end=filer.get("fiscal_year_end"),
        earnings_form=filer.get("earnings_form"),
        annual_form=filer.get("annual_form"),
        name=data.get("name"),
    )


# ---------------------------------------------------------------------------------------------------------------
# 来源表的登记号核对（thesis-ci SPEC §3.3、§3.5）
# ---------------------------------------------------------------------------------------------------------------

FILL_FIELDS = ("accession", "filed", "form", "period")  # --write 只补这几个字段，而且只补缺的
_FIELD_ORDER = ("tag", "kind", "title", "issuer_cik", "form", "period", "accession", "date", "filed", "url",
                "location", "primary", "note")
_TAG_RE = re.compile(r"^(?P<ticker>[A-Z0-9][A-Z0-9.]*)-(?P<form>[A-Z0-9]+)-(?P<rest>.+)$")
_TAG_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:-(\d+))?$")


def form_key(form: str) -> str:
    """表格的比较键：去掉连字符、斜杠和空格（10-Q → 10Q，DEF 14A → DEF14A，8-K/A → 8KA）；FORM3 与 3 相同。"""
    key = re.sub(r"[^A-Z0-9]", "", str(form).upper())
    key = re.sub(r"^SCHEDULE", "SC", key)
    if key.startswith("FORM") and key[4:].isdigit():
        key = key[4:]
    return key


@dataclasses.dataclass(frozen=True)
class SourceTag:
    ticker: str
    form: str  # 标签里的表格写法（10Q、8K、FORM3 ……）
    period: str | None = None
    date: dt.date | None = None
    ordinal: int = 1


def parse_source_tag(tag: str) -> SourceTag | None:
    """定期报告 <代码>-<表格>-<期间>，临时报告 <代码>-<表格>-<EDGAR filing date>[-N]（SPEC §3.3）。"""
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
    problems: list[str] = dataclasses.field(default_factory=list)  # 与 EDGAR 不一致
    fills: dict[str, Any] = dataclasses.field(default_factory=dict)  # 缺、可以补的字段
    unresolved: str | None = None  # 无法核对的原因
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
    """companies/<代码>/sources.yml 所属的公司：同目录的 thesis.yml，否则本仓库 companies/<代码>/thesis.yml。"""
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
    """EDGAR 能确定的期间：定期报告看报告日；业绩 8-K／6-K 看业绩事件；其余返回 None（不核对）。"""
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
        return None, "既没有 form，标签里也看不出表格"
    candidates = [f for f in issuer.subs.filings if form_key(f.form) == key]
    if tag and tag.date:
        same_day = sorted((f for f in candidates if f.filing_date == tag.date), key=lambda f: f.accession)
        if len(same_day) >= tag.ordinal:
            return same_day[tag.ordinal - 1], ""
        return None, f"EDGAR 上 {tag.date} 没有第 {tag.ordinal} 份 {form or tag.form}"
    if tag and tag.period:
        candidates = [f for f in candidates
                      if f.form in PERIODIC_FORMS and expected_period(f, issuer, client) == tag.period]
    elif entry.get("filed"):
        filed = to_date(entry.get("filed"))
        candidates = [f for f in candidates if f.filing_date == filed]
    else:
        return None, "标签里没有期间或日期，也没有 filed"
    if len(candidates) == 1:
        return candidates[0], ""
    if not candidates:
        return None, "EDGAR 上找不到对应的文件"
    return None, f"EDGAR 上有 {len(candidates)} 份候选：{'、'.join(f.accession for f in candidates[:5])}"


def _tag_problems(tag: SourceTag | None, filing: Filing, period: str | None, issuer: _Issuer,
                  registered: set[str]) -> list[str]:
    """标签与 EDGAR 是否一致（SPEC §3.3）。registered 是同一张来源表里已登记的登记号。

    同一天同一表格有几份时，“-2、-3”只在几份都登记时才需要（按登记号先后）；只登记了其中一份时不加后缀也对，
    按 EDGAR 上的先后写后缀也认。
    """
    if tag is None:
        return []
    problems = []
    if form_key(tag.form) != form_key(filing.form):
        problems.append(f"标签的表格 {tag.form} 与 EDGAR 的 {filing.form} 不一致")
    if filing.form in PERIODIC_FORMS:
        if tag.period and period and tag.period != period:
            problems.append(f"标签的期间 {tag.period} 与 EDGAR 推出的 {period} 不一致")
        elif tag.date:
            problems.append("定期报告的标签应当写财年期间（SPEC §3.3）")
    elif tag.date:
        if tag.date != filing.filing_date:
            problems.append(f"临时报告的标签应当用 EDGAR filing date：应为 …-{filing.filing_date}，写的是 …-{tag.date}")
        else:
            same_day = sorted(f.accession for f in issuer.subs.filings
                              if f.filing_date == filing.filing_date and form_key(f.form) == form_key(filing.form))
            on_edgar = same_day.index(filing.accession) + 1 if filing.accession in same_day else 1
            listed = [a for a in same_day if a in registered or a == filing.accession]
            in_file = listed.index(filing.accession) + 1 if filing.accession in listed else 1
            if tag.ordinal not in (on_edgar, in_file):
                want = f"…-{filing.filing_date}" + (f"-{in_file}" if in_file > 1 else "")
                problems.append(f"同一天 EDGAR 上有 {len(same_day)} 份 {filing.form}，这一份按登记号排第 {on_edgar}、"
                                f"在本表登记的几份里排第 {in_file}：标签应为 {want}")
    elif tag.period:
        problems.append("临时报告的标签应当写 EDGAR filing date（SPEC §3.3）")
    return problems


def _check_entry(entry: Mapping[str, Any], company: Filer | None, client: EdgarClient,
                 issuers: dict[str, _Issuer], registered: set[str] | None = None) -> SourceFinding:
    tag_text = str(entry.get("tag") or "")
    finding = SourceFinding(tag=tag_text)
    tag = parse_source_tag(tag_text)
    raw_cik = entry.get("issuer_cik")
    if not raw_cik and company and tag and tag.ticker == company.ticker:
        raw_cik = company.cik
        finding.notes.append("issuer_cik 没写，按标签取本公司的 CIK")
    if not raw_cik:
        finding.unresolved = "没有 issuer_cik，标签也不是本公司的，无法核对"
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
            finding.problems.append(f"EDGAR 上 CIK {cik} 的申报里没有登记号 {accession}")
            return finding
    else:
        filing, why = _resolve_from_tag(entry, tag, issuer, client)
        if filing is None:
            finding.unresolved = f"缺 accession，{why}"
            return finding
        finding.fills["accession"] = filing.accession
    period = expected_period(filing, issuer, client)
    finding.edgar = {"accession": filing.accession, "form": filing.form,
                     "filed": filing.filing_date.isoformat(), "period": period}

    form = entry.get("form")
    if form in (None, ""):
        finding.fills["form"] = filing.form
    elif form_key(str(form)) != form_key(filing.form):
        finding.problems.append(f"form 写的是 {form}，EDGAR 是 {filing.form}")

    try:
        filed = to_date(entry.get("filed"))
    except ValueError:
        finding.problems.append(f"filed 写的是 {entry.get('filed')!r}，不是日期")
        filed = filing.filing_date
    if filed is None:
        finding.fills["filed"] = filing.filing_date
    elif filed != filing.filing_date:
        finding.problems.append(f"filed 写的是 {filed}，EDGAR 的 filing date 是 {filing.filing_date}")

    entry_period = entry.get("period")
    if period:
        if entry_period in (None, ""):
            finding.fills["period"] = period
        elif str(entry_period) != period:
            finding.problems.append(f"period 写的是 {entry_period}，按 EDGAR 应为 {period}")

    finding.problems.extend(_tag_problems(tag, filing, period, issuer, registered or set()))
    url = entry.get("url")
    m = re.search(r"/Archives/edgar/data/\d+/(\d{18})(?:/|$)", str(url or ""))
    if m and m.group(1) != filing.accession.replace("-", ""):
        finding.problems.append(f"url 指向另一份文件（{m.group(1)}），不是 {filing.accession}")
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
    """标签 → (条目首行, 条目末行之后, 键的缩进)。只认“- tag: X”开头的条目。"""
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
    """只补缺的字段（缺键或值为 null）；不改任何已有的值。改完重新解析，确认除了补上的字段外一模一样。"""
    lines = text.splitlines(keepends=True)
    blocks = _entry_blocks(lines)
    inserts: list[tuple[int, list[str]]] = []
    replaces: dict[int, str] = {}
    for tag, fields in fills.items():
        if tag not in blocks:
            raise EdgarDataError(f"来源表里找不到“- tag: {tag}”这一行，没有写入")
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
                raise ValueError(f"只补 {FILL_FIELDS} 这几个字段，收到 {field}")
            rendered = f"{' ' * key_indent}{field}: {_yaml_scalar(value)}\n"
            if field in keys:
                j = keys[field]
                rest = key_re.match(lines[j].rstrip("\n")).group("rest")
                if not re.fullmatch(r"\s*(?:null|Null|NULL|~)?\s*(?:#.*)?", rest):
                    raise EdgarDataError(f"{tag} 的 {field} 已经有值，不覆盖")
                replaces[j] = rendered
                continue
            anchor = start
            for name in _FIELD_ORDER[:_FIELD_ORDER.index(field)]:
                if name in keys:
                    anchor = max(anchor, keys[name])
            k = anchor + 1  # 跳过锚点键的续行
            while k < end and (not lines[k].strip() or len(lines[k]) - len(lines[k].lstrip()) > key_indent):
                k += 1
            while k > anchor + 1 and not lines[k - 1].strip():
                k -= 1
            pending.setdefault(k, []).append((field, rendered))
        for position, new_lines in pending.items():  # 同一处插入几个字段时按常见的字段顺序排
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
        raise EdgarDataError("写入会改动 sources 以外的内容，已放弃")
    if len(old_entries) != len(new_entries):
        raise EdgarDataError("写入会改变条目数，已放弃")
    for before, after in zip(old_entries, new_entries):
        tag = before.get("tag") if isinstance(before, dict) else None
        wanted = {k: _normalized(v) for k, v in (fills.get(tag) or {}).items()}
        for key in set(before) | set(after):
            if key in wanted:
                if before.get(key) not in (None, "") or _normalized(after.get(key)) != wanted[key]:
                    raise EdgarDataError(f"{tag} 的 {key} 写入后不对，已放弃")
            elif _normalized(before.get(key)) != _normalized(after.get(key)):
                raise EdgarDataError(f"写入会改动 {tag} 的 {key}，已放弃")


def check_sources(path: str | os.PathLike, *, client: EdgarClient | None = None,
                  repo_root: str | os.PathLike | None = None, write: bool = False) -> SourcesReport:
    """核对来源表里每条 kind: filing 的 accession／filed／period（及 form、标签、url）与 EDGAR 是否一致。

    默认只读、只报告。write=True 时只给没有不一致的条目补缺的 accession、filed、form、period，不改已有的值；
    改动以 unified diff 返回（SourcesReport.diff）。
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
# 命令行
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
        raise argparse.ArgumentTypeError(f"日期写成 YYYY-MM-DD，收到 {value!r}") from None


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
                   "rule": "EDGAR filing date 落在窗口内"},
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
    print(f"{report.path}：{len(report.findings)} 条 kind: filing；与 EDGAR 一致 {counts['ok']}，"
          f"缺字段可补 {counts['missing']}，不一致 {counts['mismatch']}，无法核对 {counts['unresolved']}")
    for finding in report.findings:
        where = f"（第 {finding.line} 行）" if finding.line else ""
        for problem in finding.problems:
            print(f"  不一致  {finding.tag}{where}：{problem}")
        if finding.unresolved:
            print(f"  无法核对 {finding.tag}{where}：{finding.unresolved}")
        for field, value in finding.fills.items():
            value = value.isoformat() if isinstance(value, dt.date) else value
            print(f"  可补    {finding.tag}{where}：{field}: {value}")
        for note in finding.notes:
            print(f"  说明    {finding.tag}{where}：{note}")
    if report.written:
        print("已写入（只补缺的字段）：")
        print(report.diff.rstrip())
    elif any(f.fills for f in report.findings):
        print("只读：加 --write 补上缺的字段（不改已有的值；有不一致的条目不补）。")


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
                        help=f"读 {UA_ENV} 的 .env（默认 {ENV_FILE_ENV} 或 {DEFAULT_ENV_FILE}）")
    common.add_argument("--cache-dir", type=Path, default=None, help=f"缓存目录（默认 {CACHE_ENV} 或 ~/.cache/owners-office/edgar）")
    common.add_argument("--offline", action="store_true", help="只用缓存，不联网（不需要 User-Agent）")
    common.add_argument("--refresh", action="store_true", help="submissions 不用缓存，重新取")
    common.add_argument("--repo", type=Path, default=REPO_ROOT, help="公开仓库根目录（读 companies/<代码>/thesis.yml）")
    common.add_argument("--json", action="store_true", help="输出 JSON（默认 YAML）")
    common.add_argument("-v", "--verbose", action="store_true", help="打印每次请求（不含请求头）")

    parser = argparse.ArgumentParser(prog="python -m pipeline.edgar", description="SEC EDGAR 访问（decisions/0017）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("release-history", parents=[common], help="过去几年的业绩事件")
    p.add_argument("company", help="公司代码（companies/ 下的目录名）或 CIK")
    p.add_argument("--years", type=int, default=HISTORY_YEARS)
    p.add_argument("--as-of", type=_date_arg, default=None, help="窗口终点（默认今天，美东）")
    p.add_argument("--quarter", type=int, choices=(1, 2, 3, 4), default=None, help="只看这个财季")
    p.set_defaults(func=_cmd_release_history)

    p = sub.add_parser("next-release", parents=[common], help="一个财季的（预计）发布日、截止时间与合并时限")
    p.add_argument("company")
    p.add_argument("period", help="FY<年>Q<季>，按公司财年")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--announced", default=None,
                       help="公司公布的发布日（YYYY-MM-DD 美东日期，或带时区的时刻）；替换估计，placeholder: false")
    group.add_argument("--window", nargs=2, metavar=("START", "END"), default=None, help="公司公布的发布窗口")
    p.add_argument("--as-of", type=_date_arg, default=None, help="按哪一天判断（默认今天，美东）")
    p.set_defaults(func=_cmd_next_release)

    p = sub.add_parser("check-sources", parents=[common], help="核对来源表里 kind: filing 条目的登记号")
    p.add_argument("path", type=Path)
    p.add_argument("--write", action="store_true", help="补上缺的 accession/filed/form/period（不改已有的值）")
    p.set_defaults(func=_cmd_check_sources)

    p = sub.add_parser("documents", parents=[common], help="一份申报的文件清单；可下载主文档与 EX-99.x")
    p.add_argument("company")
    p.add_argument("accession")
    p.add_argument("--download", type=Path, default=None, help="下载到这个目录（<目录>/<登记号>/）")
    p.add_argument("--all", action="store_true", help="列出全部文件（默认只列主文档与 EX-99.x）")
    p.set_defaults(func=_cmd_documents)

    p = sub.add_parser("submissions", parents=[common], help="submissions 概况（含续页）")
    p.add_argument("company")
    p.set_defaults(func=_cmd_submissions)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    try:
        return args.func(args)
    except MissingUserAgent as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except EdgarError as exc:
        print(f"EDGAR：{exc}", file=sys.stderr)
        return 3
    except (ValueError, OSError, yaml.YAMLError) as exc:
        print(f"错误：{redact(str(exc))}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
