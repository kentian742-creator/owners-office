"""预注册的时间戳证明：OpenTimestamps 的打时间戳、升级、状态与核验（docs/decisions/0018）。本模块不调用模型。

规范（thesis-ci SPEC §2.1）：每个预注册条目文件——companies/<代码>/prereg/<期间>.yml（系统）与 <期间>-owner.yml
（所有者）——旁边放一份 OpenTimestamps 证明 <文件>.ots。截止时间过后，C-PREREG-IMMUTABLE 要求证明存在；PATH 上有
客户端时（thesis-ci v0.2.1 起）先用 `ots info` 在本地比对证明承诺的 sha256 与文件的 sha256，不符或读不出即报错；
相符之后再运行 `ots verify -f <文件> <文件>.ots`：通过即可，客户端说证明本身坏了才报错，其余（没有比特币节点、
尚待确认、日历联系不上、超时）只报警。

每一步都交给官方客户端 opentimestamps-client（命令 ots，版本固定在 requirements.txt），本模块只调用它、解析它的输出：

- stamp(paths)：给每个文件打时间戳，证明写在文件旁边。同一份内容已有证明时什么都不做。拒绝两种情况：预注册条目
  文件的截止时间已过（截止之后打的证明证明不了截止之前的内容）；<文件>.ots 已存在，证明的却是另一份内容（不覆盖
  证明。截止之前确需改动时——15A：公司公布发布日后只改 event 与 deadline——在同一个 PR 里删掉旧证明，git 历史
  保留它，合并后工作流重新打）。
- upgrade(paths)：向日历取比特币确认，把待确认的证明升级为完整证明。客户端升级时会在原地留下 <证明>.bak，所以在
  临时副本上升级，有变化才原子地替换原证明；仍待确认不算失败，没有一个日历答复算失败。
- status(path)：missing / pending / attested / error。离线读 ots info（证明承诺的 sha256、待确认的日历、比特币区块
  高度与该区块的 merkle root），与文件现在的 sha256 比对；已有比特币确认的再跑一次 ots verify：有比特币节点时
  客户端打印区块日期，没有节点时区块时间留空，并给出可以在任一区块浏览器上手工核对的区块高度与 merkle root。
- verify(path)：判法与 C-PREREG-IMMUTABLE（thesis-ci v0.2.1）一步不差：先 ots info 本地比对 sha256，再 ots verify。
  True 通过；None 无法在本机核验（尚待确认、没有比特币节点、日历联系不上、超时）；False 失败（与文件不符、证明读不出
  或损坏、没有证明）。

客户端 0.7.2 的实际行为（读源码并实测）：

- stamp 只把 sha256(sha256(文件) ‖ 16 字节随机数) 发给日历，文件和它的哈希都不离开本机。默认四个日历，至少两个
  在 5 秒内应答才算成功，否则不写证明。刚写出的证明是“待确认”：日历承诺把它写进比特币，通常几个小时后才有区块。
- verify 先在本地比对文件的 sha256，不符即 “File does not match original!”。之后，待确认的证明要联系日历；完整的
  证明不联系日历，直接向比特币节点（Bitcoin Core 的 RPC，按本机 bitcoin.conf 与 .cookie；剪枝节点也行）取区块头。
  客户端没有区块浏览器后备：没有节点只报 “Could not connect to Bitcoin node”；区块时间只在核验通过时打印，只到日期。
- 日历联系不上（DNS、TLS 出错）时，待确认证明的核验输出里没有 “pending”：thesis-ci v0.2.0 的 C-PREREG-IMMUTABLE
  因此把完好的证明判为错误，v0.2.1 先在本地比对哈希，改为只报警。升级过的证明核验时不联系日历、自带通到比特币
  区块头的全部路径，所以仍在截止之前升级完（.github/workflows/timestamp.yml 每 6 小时升级一次）。
- 客户端等日历应答没有超时，这里每次调用限 OTS_TIMEOUT 秒。一律加 --no-cache：结果只取决于证明文件本身，与 CI
  上（缓存总是空的）一致。
- python.org 安装的 macOS Python 可能没有 CA 证书，连日历会报 CERTIFICATE_VERIFY_FAILED；设
  SSL_CERT_FILE=/etc/ssl/cert.pem 即可。

命令行（在仓库根目录）：

    python -m pipeline.timestamp stamp FILE…     # 或 --prereg：截止时间未过的全部条目文件（已有同一内容证明的跳过）
    python -m pipeline.timestamp upgrade FILE…   # 或 --prereg：仓库里全部预注册证明
    python -m pipeline.timestamp status FILE…    # 或 --prereg：全部条目文件
    python -m pipeline.timestamp verify FILE…    # 退出状态：0 全部通过，1 有失败，3 有无法在本机核验的

FILE 可以是被打时间戳的文件，也可以是它的 .ots。stamp、upgrade、status 有失败或错误时退出状态为 1。
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlparse

from .outputs import load_yaml_text

REPO_ROOT = Path(__file__).resolve().parents[1]
OTS_TIMEOUT = 120  # 秒，与 C-PREREG-IMMUTABLE 相同
PROOF_SUFFIX = ".ots"
SETTLEMENT_SUFFIX = ".settlement.yml"
PREREG_ITEMS_GLOB = "companies/*/prereg/*.yml"  # 去掉 *.settlement.yml 就是条目文件（与 thesis-ci 相同）
PREREG_PROOFS_GLOB = "companies/*/prereg/*.yml.ots"

EXIT_OK, EXIT_FAILED, EXIT_UNVERIFIED = 0, 1, 3

# ots info 的输出（opentimestamps 0.4 的 Timestamp.str_tree）
_INFO_BITCOIN = re.compile(
    r"verify BitcoinBlockHeaderAttestation\((\d+)\)(?:[ \t]*\r?\n[ \t]*# Bitcoin block merkle root ([0-9a-f]{64}))?"
)
_INFO_PENDING = re.compile(r"verify PendingAttestation\('([^']*)'\)")
# ots verify 通过时的一行。区块日期按本地时区打印，run_ots 把 TZ 设为 UTC。
_VERIFY_OK = re.compile(r"Success! Bitcoin block (\d+) attests existence as of (\d{4}-\d{2}-\d{2})")
_NO_NODE = re.compile(r"Could not connect to (?:local )?Bitcoin node", re.I)
_CALENDAR_LINE = re.compile(r"^Calendar (\S+): (.+)$", re.M)
# thesis-ci（v0.2.1）src/thesis_ci/checks/archive.py 的两条正则，一字不改：verify() 与 C-PREREG-IMMUTABLE 判得一样
# （tests/test_timestamp.py 在装有 thesis-ci 时逐条比对）。第一条从 ots info 取证明承诺的 sha256（只认 sha256）；
# 第二条在哈希相符之后判 ots verify 的输出：匹配即证明本身坏了，不匹配的失败都只是无法在本机核验。
OTS_INFO_DIGEST_RE = re.compile(r"File sha256 hash:\s*([0-9a-f]{64})", re.I)
OTS_BAD_PROOF_RE = re.compile(r"does not match|mismatch|bad timestamp|invalid|corrupt|not a timestamp", re.I)
UPGRADE_COMPLETE = "Success! Timestamp complete"
UPGRADE_PENDING = "Failed! Timestamp not complete"
CALENDAR_PENDING = "Pending confirmation"  # 日历的回答：收到了，还没写进比特币
_SSL_HINT = "本机 Python 没有 CA 证书：设 SSL_CERT_FILE=/etc/ssl/cert.pem（macOS）后重试"


class TimestampError(RuntimeError):
    """OpenTimestamps 客户端找不到或无法运行。"""


@dataclasses.dataclass(frozen=True)
class StepResult:
    """stamp() 或 upgrade() 对一个文件的结果。

    action——stamp：stamped（新打）、already_stamped（同一内容已有证明）、refused（拒绝）、failed（没打成）；
    upgrade：upgraded（刚升级为比特币确认）、complete（本来就是完整证明）、pending（仍待确认）、failed。
    """

    path: Path  # 被打时间戳的文件
    proof: Path  # <文件>.ots
    action: str
    detail: str = ""
    block_height: int | None = None

    @property
    def ok(self) -> bool:
        return self.action not in ("refused", "failed")


@dataclasses.dataclass(frozen=True)
class ProofStatus:
    """status() 与 verify() 的结果。

    state：missing（没有证明）、pending（待确认）、attested（有比特币确认）、error（证明损坏、与文件不符、核验失败）。
    verified：verify() 与 C-PREREG-IMMUTABLE 一样三分——True 通过，None 无法在本机核验，False 失败；status() 只核验
    已有比特币确认的证明（为了区块时间），其余为 None。
    block_time：客户端用比特币节点核验通过后打印的区块日期（UTC，只到日期）；没有节点时为 None。
    merkle_root：该区块的 merkle root（区块浏览器的显示顺序），没有节点时可在任一区块浏览器上手工核对。
    """

    path: Path
    proof: Path
    state: str
    detail: str = ""
    file_sha256: str | None = None  # 证明所承诺的 sha256
    block_height: int | None = None  # 最早的比特币确认
    merkle_root: str | None = None
    block_time: str | None = None
    calendars: tuple[str, ...] = ()  # 待确认的日历
    verified: bool | None = None


class ProofInfo(NamedTuple):
    """ots info 解析出来的内容。"""

    file_digest: str  # 证明承诺的 sha256
    bitcoin: tuple[tuple[int, str | None], ...]  # (区块高度, merkle root)
    calendars: tuple[str, ...]


# ---------------------------------------------------------------- 客户端


def ots_executable() -> str:
    """PATH 上的 ots；没有时用与当前解释器同一目录的（没有激活的虚拟环境）。"""
    found = shutil.which("ots")
    if found:
        return found
    beside = Path(sys.executable).with_name("ots")
    if beside.is_file() and os.access(beside, os.X_OK):
        return str(beside)
    raise TimestampError("找不到 OpenTimestamps 客户端 ots：pip install -r requirements.txt")


def run_ots(args: Sequence[str], timeout: float = OTS_TIMEOUT) -> tuple[int | None, str]:
    """运行 `ots args…`，返回 (退出状态, stdout 与 stderr 合起来的输出)；超时的退出状态为 None。

    本模块只通过这个函数运行客户端（测试替换它）。
    """
    env = dict(os.environ, TZ="UTC")
    try:
        proc = subprocess.run(
            [ots_executable(), *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        command = next((a for a in args if not a.startswith("-")), "")
        return None, f"ots {command} timed out after {timeout:g} s"
    except OSError as exc:
        raise TimestampError(f"ots 无法运行：{exc}") from exc
    return proc.returncode, "\n".join(s for s in (proc.stdout, proc.stderr) if s).strip()


def parse_info(output: str) -> ProofInfo | None:
    """ots info 的输出 → ProofInfo；找不到证明承诺的 sha256（读不出，或不是 sha256）时为 None。与 thesis-ci 一样
    只看输出，不看退出状态。"""
    head = OTS_INFO_DIGEST_RE.search(output)
    if head is None:
        return None
    bitcoin = tuple((int(height), root or None) for height, root in _INFO_BITCOIN.findall(output))
    calendars = tuple(dict.fromkeys(_INFO_PENDING.findall(output)))
    return ProofInfo(head.group(1).lower(), bitcoin, calendars)


def classify_verify(returncode: int | None, output: str) -> bool | None:
    """ots verify 的结果，只在文件与证明的 sha256 已在本地比对相符之后才问；判法与 C-PREREG-IMMUTABLE 相同：
    退出状态 0 通过；输出说证明本身坏了（OTS_BAD_PROOF_RE）失败；其余（含超时）无法在本机核验。"""
    if returncode is None:
        return None
    if returncode == 0:
        return True
    return False if OTS_BAD_PROOF_RE.search(output) else None


# ---------------------------------------------------------------- 文件


def proof_path(path: str | os.PathLike[str]) -> Path:
    """<文件>.ots：证明放在被打时间戳的文件旁边（SPEC §2.1）。"""
    p = Path(path)
    return p.with_name(p.name + PROOF_SUFFIX)


def _pair(path: str | os.PathLike[str]) -> tuple[Path, Path]:
    """(被打时间戳的文件, 证明)；path 可以是两者之一。"""
    p = Path(path)
    if p.name.endswith(PROOF_SUFFIX) and len(p.name) > len(PROOF_SUFFIX):
        return p.with_name(p.name[: -len(PROOF_SUFFIX)]), p
    return p, proof_path(p)


def file_sha256(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_prereg_items_file(path: str | os.PathLike[str]) -> bool:
    """companies/<代码>/prereg/ 下除结算文件以外的 .yml：<期间>.yml 与 <期间>-owner.yml（与 thesis-ci 的判法相同）。"""
    p = Path(os.path.abspath(path))
    return (
        p.suffix == ".yml"
        and not p.name.endswith(SETTLEMENT_SUFFIX)
        and p.parent.name == "prereg"
        and p.parent.parent.parent.name == "companies"
    )


def prereg_deadline(path: str | os.PathLike[str]) -> dt.datetime:
    """条目文件的 deadline（带时区偏移的 ISO 8601）。读不出来抛出 ValueError。"""
    try:
        data = load_yaml_text(Path(path).read_text(encoding="utf-8"))  # 重复键报错
    except Exception as exc:  # noqa: BLE001  （读文件、编码、YAML 语法、重复键：一律当作读不出）
        raise ValueError(f"{Path(path).name} 读不出：{exc}") from exc
    value = data.get("deadline") if isinstance(data, dict) else None
    if value is None:
        raise ValueError("没有 deadline")
    deadline = value if isinstance(value, dt.datetime) else None
    if isinstance(value, str):
        try:
            deadline = dt.datetime.fromisoformat(value.strip())
        except ValueError:
            deadline = None
    if deadline is None or deadline.tzinfo is None or deadline.utcoffset() is None:
        raise ValueError(f"deadline {value!r} 不是带时区偏移的时间")
    return deadline


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _aware(now: dt.datetime) -> dt.datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now 必须带时区：截止时间带时区偏移，不带时区无法比较")
    return now


def _replace_bytes(path: Path, data: bytes) -> None:
    """原子地替换 path 的内容：先写同一目录里的临时文件，再 os.replace。"""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------- 输出的摘要


def _host(url: str) -> str:
    return urlparse(url).netloc or url


def _last_line(output: str, returncode: int | None = None) -> str:
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    return lines[-1] if lines else f"退出状态 {returncode}"


def _with_hint(text: str, output: str) -> str:
    return f"{text}（{_SSL_HINT}）" if "CERTIFICATE_VERIFY_FAILED" in output else text


def _calendar_summary(output: str) -> str:
    """把 “Calendar <url>: <消息>” 按消息归并：alice…, bob…: Pending confirmation in Bitcoin blockchain。"""
    by_message: dict[str, list[str]] = {}
    for url, message in _CALENDAR_LINE.findall(output):
        by_message.setdefault(message.strip(), []).append(_host(url))
    return "；".join(f"{', '.join(hosts)}: {message}" for message, hosts in by_message.items())


def _pending_detail(calendars: Sequence[str]) -> str:
    return f"待确认：{len(calendars)} 个日历（{', '.join(_host(u) for u in calendars)}）"


def _stamp_failure(returncode: int | None, output: str) -> str:
    """ots -v stamp 失败时各日历的错误（-v 才打印），去重。"""
    if returncode is None:
        return output
    lines = [
        ln.strip() for ln in output.splitlines()
        if ln.strip() and not ln.startswith(("Submitting to remote calendar", "Doing "))
    ]
    text = "; ".join(dict.fromkeys(lines)) or f"退出状态 {returncode}"
    return _with_hint(text, output)


# ---------------------------------------------------------------- 状态与核验


def _run_info(proof: Path) -> tuple[int | None, str]:
    return run_ots(["--no-cache", "info", os.path.abspath(proof)])


def _info(path: Path, proof: Path, run: tuple[int | None, str] | None = None) -> ProofStatus:
    """离线读证明（ots info）：承诺的 sha256、比特币确认、待确认的日历。不与文件比对。"""
    returncode, output = run or _run_info(proof)
    info = parse_info(output)
    if info is None:
        return ProofStatus(path, proof, "error", f"证明读不出：{_last_line(output, returncode)}")
    if info.bitcoin:
        height, root = min(info.bitcoin, key=lambda item: item[0])
        return ProofStatus(path, proof, "attested", f"比特币区块 {height}", info.file_digest, height, root,
                           calendars=info.calendars)
    if info.calendars:
        return ProofStatus(path, proof, "pending", _pending_detail(info.calendars), info.file_digest,
                           calendars=info.calendars)
    return ProofStatus(path, proof, "error", "证明里既没有比特币确认，也没有待确认的日历", info.file_digest)


def _run_verify(st: ProofStatus) -> ProofStatus:
    """对 st（已由 _info 读过）运行 ots verify，按 C-PREREG-IMMUTABLE 的判法填 verified。"""
    returncode, output = run_ots(["--no-cache", "verify", "-f", os.path.abspath(st.path), os.path.abspath(st.proof)])
    verified = classify_verify(returncode, output)
    if verified:
        match = _VERIFY_OK.search(output)
        height = int(match.group(1)) if match else st.block_height
        when = match.group(2) if match else None
        return dataclasses.replace(
            st, state="attested", verified=True, block_height=height, block_time=when,
            detail=f"比特币区块 {height}（{when or '日期未知'}，UTC）证明文件那时已存在；本机比特币节点核验通过",
        )
    if verified is None:
        if _NO_NODE.search(output):
            if st.block_height is not None:
                detail = (f"比特币区块 {st.block_height}；没有比特币节点，区块未核验（文件与证明相符）。手工核对：任一区块浏览器上"
                          f"区块 {st.block_height} 的 merkle root 应为 {st.merkle_root}")
            else:
                detail = "日历已给出比特币确认，但证明文件还没升级：先运行 upgrade；没有比特币节点，区块未核验（文件与证明相符）"
        elif returncode is None:
            detail = f"{output}；文件与证明相符"
        elif CALENDAR_PENDING in output:
            detail = f"待确认（{_calendar_summary(output)}）；文件与证明相符"
        elif _CALENDAR_LINE.search(output):
            detail = _with_hint(f"日历联系不上，比特币确认未核验（{_calendar_summary(output)}）；文件与证明相符", output)
        else:
            detail = f"无法在本机核验（{_last_line(output, returncode)}）；文件与证明相符"
        return dataclasses.replace(st, verified=None, detail=detail)
    return dataclasses.replace(st, state="error", verified=False, detail=f"证明本身有问题：{_last_line(output, returncode)}")


def status(path: str | os.PathLike[str]) -> ProofStatus:
    """证明的状态。离线（ots info 与本地哈希）；已有比特币确认的再跑 ots verify 取区块时间（需要比特币节点）。"""
    target, proof = _pair(path)
    if not proof.is_file():
        return ProofStatus(target, proof, "missing", "没有证明")
    st = _info(target, proof)
    if st.file_sha256 is None:
        return st  # 证明读不出
    if not target.is_file():
        return dataclasses.replace(st, state="error", detail="被打时间戳的文件不存在")
    mismatch = _digest_mismatch(st, target)
    if mismatch:
        return mismatch
    return _run_verify(st) if st.state == "attested" else st


def _digest_mismatch(st: ProofStatus, target: Path) -> ProofStatus | None:
    """本地比对证明承诺的 sha256 与文件现在的 sha256（不需要网络）；不符时返回失败的结果。"""
    actual = file_sha256(target)
    if actual == st.file_sha256:
        return None
    return dataclasses.replace(
        st, state="error", verified=False,
        detail=f"文件与证明不符：文件在打时间戳之后改过（证明的 sha256 {st.file_sha256}，文件现在 {actual}）",
    )


def verify(path: str | os.PathLike[str]) -> ProofStatus:
    """与 C-PREREG-IMMUTABLE（thesis-ci v0.2.1 的 ots_verify）一步不差：

    1. ots info：超时为 None；输出里找不到证明承诺的 sha256（读不出）为 False；与文件的 sha256 不符为 False。
    2. 相符之后 ots verify -f <文件> <证明>：超时为 None；退出状态 0 为 True；输出说证明本身坏了为 False；
       其余为 None（没有比特币节点、尚待确认、日历联系不上）。待确认的证明这一步会联系日历。
    """
    target, proof = _pair(path)
    if not proof.is_file():
        return ProofStatus(target, proof, "missing", "没有证明", verified=False)
    if not target.is_file():
        return ProofStatus(target, proof, "error", "被打时间戳的文件不存在", verified=False)
    returncode, output = _run_info(proof)
    if returncode is None:
        return ProofStatus(target, proof, "error", output, verified=None)
    st = _info(target, proof, (returncode, output))
    if st.file_sha256 is None:
        return dataclasses.replace(st, verified=False)
    return _digest_mismatch(st, target) or _run_verify(st)


# ---------------------------------------------------------------- 打时间戳与升级


def stamp(paths: Iterable[str | os.PathLike[str]], *, now: dt.datetime | None = None) -> list[StepResult]:
    """给每个文件打时间戳，证明写在旁边（<文件>.ots）。被打时间戳的文件只读不写。

    一个文件的问题不影响其他文件：拒绝与失败都记在各自的结果里（看 StepResult.ok），不抛异常。
    """
    now = _aware(now or _utcnow())
    return [_stamp_one(Path(p), now) for p in paths]


def _stamp_one(path: Path, now: dt.datetime) -> StepResult:
    proof = proof_path(path)
    if path.name.endswith(PROOF_SUFFIX):
        return StepResult(path, proof, "refused", "这是证明文件：给被打时间戳的文件打，不给证明打")
    if not path.is_file():
        return StepResult(path, proof, "failed", "文件不存在")
    digest = file_sha256(path)
    too_late = None  # 预注册条目文件不能再打的原因：截止时间已过，或读不出截止时间
    if is_prereg_items_file(path):
        try:
            deadline = prereg_deadline(path)
            if now >= deadline:
                too_late = f"截止时间 {deadline.isoformat()} 已过"
        except ValueError as exc:
            too_late = f"读不出截止时间（{exc}），无法确认截止时间还没过"
    if proof.exists():
        existing = _info(path, proof)
        if existing.state != "error" and existing.file_sha256 == digest:
            return StepResult(path, proof, "already_stamped", f"同一内容已有证明（{existing.detail}）",
                              existing.block_height)
        why = (f"证明的是另一份内容（证明的 sha256 {existing.file_sha256}，文件现在 {digest}）"
               if existing.file_sha256 and existing.file_sha256 != digest else f"读不出（{existing.detail}）")
        advice = ("截止之前确需改动时，在同一个 PR 里删掉旧证明（git 历史保留它），合并后重新打" if too_late is None
                  else f"{too_late}，不能重打：把文件改回打时间戳时的内容")
        return StepResult(path, proof, "refused", f"{proof.name} 已存在，{why}；不覆盖证明。{advice}")
    if too_late:
        return StepResult(path, proof, "refused", f"{too_late}：截止之后打的时间戳证明不了截止之前的内容")
    # 绝对路径：文件名以 - 开头也不会被当成选项；客户端把证明写在 <参数>.ots
    returncode, output = run_ots(["--no-cache", "-v", "stamp", os.path.abspath(path)])
    if not proof.is_file():
        return StepResult(path, proof, "failed", f"ots stamp 没有写出证明：{_stamp_failure(returncode, output)}")
    made = _info(path, proof)
    if made.file_sha256 != digest:
        proof.unlink()  # 刚写出、还没提交的证明
        return StepResult(path, proof, "failed", f"新证明与文件不符（{made.detail}；打时间戳时文件变了？），已删掉，请重打")
    return StepResult(path, proof, "stamped", made.detail)


def upgrade(paths: Iterable[str | os.PathLike[str]]) -> list[StepResult]:
    """把待确认的证明升级为比特币确认的证明（联系日历）。path 可以是被打时间戳的文件或它的 .ots；只改证明。"""
    return [_upgrade_one(*_pair(p)) for p in paths]


def _upgrade_one(path: Path, proof: Path) -> StepResult:
    if not proof.is_file():
        return StepResult(path, proof, "failed", "没有证明")
    before = _info(path, proof)
    if before.state == "error":
        return StepResult(path, proof, "failed", before.detail)
    if before.state == "attested":
        return StepResult(path, proof, "complete", before.detail, before.block_height)
    original = proof.read_bytes()
    with tempfile.TemporaryDirectory(prefix="ots-upgrade-") as tmp:
        work = Path(tmp) / proof.name  # 客户端把旧证明改名为 <证明>.bak：只让它留在临时目录里
        work.write_bytes(original)
        returncode, output = run_ots(["--no-cache", "upgrade", str(work)])
        upgraded = work.read_bytes() if work.is_file() else original
        after = _info(path, work) if upgraded != original else before
    if returncode is None or (UPGRADE_COMPLETE not in output and UPGRADE_PENDING not in output):
        return StepResult(path, proof, "failed", _with_hint(f"ots upgrade 失败：{_last_line(output, returncode)}", output))
    if upgraded != original:
        if after.state == "error" or after.file_sha256 != before.file_sha256:
            return StepResult(path, proof, "failed", f"升级后的证明不对（{after.detail}），原证明未动")
        _replace_bytes(proof, upgraded)
    if after.state == "attested":
        action = "upgraded" if upgraded != original else "complete"
        return StepResult(path, proof, action, after.detail, after.block_height)
    summary = _calendar_summary(output) or _last_line(output, returncode)
    if CALENDAR_PENDING not in output:  # 没有一个日历答复“收到了、待确认”：这次没查成
        return StepResult(path, proof, "failed", _with_hint(f"没有日历答复，未能升级（{summary}）", output))
    return StepResult(path, proof, "pending", f"仍待确认（{summary}）")


# ---------------------------------------------------------------- 仓库里的预注册文件


def prereg_items_files(root: str | os.PathLike[str] = REPO_ROOT) -> list[Path]:
    """仓库里的预注册条目文件：companies/*/prereg/*.yml，去掉 *.settlement.yml。"""
    return sorted(
        p for p in Path(root).glob(PREREG_ITEMS_GLOB) if p.is_file() and not p.name.endswith(SETTLEMENT_SUFFIX)
    )


def files_to_stamp(root: str | os.PathLike[str] = REPO_ROOT, now: dt.datetime | None = None) -> list[Path]:
    """stamp --prereg 的对象：截止时间未过、或读不出截止时间（交给 stamp() 拒绝并报出来）的条目文件。

    截止时间已过的不在其中：缺证明或证明不符由 C-PREREG-IMMUTABLE 报错，事后打时间戳补不了。
    """
    now = _aware(now or _utcnow())
    out = []
    for path in prereg_items_files(root):
        try:
            if now >= prereg_deadline(path):
                continue
        except ValueError:
            pass
        out.append(path)
    return out


def prereg_proofs(root: str | os.PathLike[str] = REPO_ROOT) -> list[Path]:
    """仓库里的预注册证明：companies/*/prereg/*.yml.ots。"""
    return sorted(p for p in Path(root).glob(PREREG_PROOFS_GLOB) if p.is_file())


# ---------------------------------------------------------------- 命令行

COMMANDS = {
    "stamp": "打时间戳，<文件>.ots 写在文件旁边；截止时间已过的预注册条目文件、已有证明却是另一份内容的，一律拒绝",
    "upgrade": "把待确认的证明升级为比特币确认的证明（仍待确认不算失败）",
    "status": "证明的状态：missing / pending / attested / error",
    "verify": "ots verify，判法与 C-PREREG-IMMUTABLE 相同；退出状态 0 全部通过，1 有失败，3 有无法在本机核验的",
}
_PREREG_HELP = {
    "stamp": "加上仓库里截止时间未过的全部预注册条目文件（已有同一内容证明的跳过）",
    "upgrade": "加上仓库里全部预注册证明（companies/*/prereg/*.yml.ots）",
    "status": "加上仓库里全部预注册条目文件",
    "verify": "加上仓库里全部预注册条目文件",
}


def _display(path: Path) -> str:
    """当前目录之下的写相对路径，其余写绝对路径。"""
    absolute = Path(os.path.abspath(path))
    try:
        return str(absolute.relative_to(Path.cwd()))
    except ValueError:
        return str(absolute)


def _format_step(result: StepResult) -> str:
    return f"{result.action:<16}{_display(result.path)}" + (f"  {result.detail}" if result.detail else "")


def _format_status(st: ProofStatus, command: str) -> str:
    label = st.state if command == "status" else {True: "verified", None: "unverified", False: "failed"}[st.verified]
    return f"{label:<16}{_display(st.path)}" + (f"  {st.detail}" if st.detail else "")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.timestamp",
        description="预注册的 OpenTimestamps 时间戳：打、升级、查看、核验（docs/decisions/0018）。",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="{stamp,upgrade,status,verify}")
    for name, help_text in COMMANDS.items():
        cmd = sub.add_parser(name, help=help_text, description=help_text)
        cmd.add_argument("files", nargs="*", metavar="FILE", help="被打时间戳的文件，或它的 .ots")
        cmd.add_argument("--prereg", action="store_true", help=_PREREG_HELP[name])
        cmd.add_argument("--root", type=Path, default=REPO_ROOT, help="--prereg 找文件的仓库根目录（默认本仓库）")
    args = parser.parse_args(argv)
    if not args.files and not args.prereg:
        parser.error(f"{args.command}：给出文件，或者用 --prereg")

    now = _utcnow()
    files = [Path(f) for f in args.files]
    if args.prereg:
        found = {
            "stamp": lambda: files_to_stamp(args.root, now),
            "upgrade": lambda: prereg_proofs(args.root),
        }.get(args.command, lambda: prereg_items_files(args.root))()
        files += [p for p in found if p not in files]
    if not files:
        print("没有要处理的预注册文件。")
        return EXIT_OK

    try:
        if args.command in ("stamp", "upgrade"):
            results = stamp(files, now=now) if args.command == "stamp" else upgrade(files)
            for result in results:
                print(_format_step(result))
            return EXIT_OK if all(r.ok for r in results) else EXIT_FAILED
        check = status if args.command == "status" else verify
        statuses = [check(f) for f in files]
    except TimestampError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_FAILED
    for st in statuses:
        print(_format_status(st, args.command))
    if args.command == "status":
        return EXIT_FAILED if any(st.state == "error" for st in statuses) else EXIT_OK
    if any(st.verified is False for st in statuses):
        return EXIT_FAILED
    return EXIT_OK if all(st.verified for st in statuses) else EXIT_UNVERIFIED


if __name__ == "__main__":
    sys.exit(main())
