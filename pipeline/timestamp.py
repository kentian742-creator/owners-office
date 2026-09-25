"""Timestamp proofs for pre-registrations: OpenTimestamps stamping, upgrading, status and verification
(docs/decisions/0018). This module does not call a model.

Specification (thesis-ci SPEC §2.1): every pre-registration items file, companies/<TICKER>/prereg/<period>.yml (the
system's) and <period>-owner.yml (the owner's), has an OpenTimestamps proof <file>.ots next to it. After the deadline,
C-PREREG-IMMUTABLE requires the proof to exist. When the client is on PATH (thesis-ci v0.2.1 and later), it first uses
`ots info` to compare locally the sha256 the proof commits to with the file's sha256; a mismatch or an unreadable
proof is an error. Once they match, it runs `ots verify -f <file> <file>.ots`: a pass is enough; it reports an error
only when the client says the proof itself is bad; anything else (no Bitcoin node, still pending, calendars
unreachable, timeout) is only a warning.

Every step is left to the official client, opentimestamps-client (command ots, version pinned in requirements.txt);
this module only calls it and parses its output:

- stamp(paths): timestamps each file and writes the proof next to it. Does nothing when a proof of the same content
  already exists. Refuses in two cases: the deadline of a pre-registration items file has passed (a proof made after
  the deadline cannot prove the content before it); or <file>.ots already exists but proves different content. The
  proof is not overwritten: when a change before the deadline is really needed (15A: once the company announces the
  release date, only event and deadline change), delete the old proof in the same PR; git history keeps it, and the
  workflow stamps again after the merge.
- upgrade(paths): gets the Bitcoin attestation from the calendars and upgrades a pending proof to a complete proof.
  The client leaves <proof>.bak in place when it upgrades, so the upgrade runs on a temporary copy and replaces the
  original proof atomically only when something changed. Still pending is not a failure; no calendar answering is a
  failure.
- status(path): missing / pending / attested / error. Reads ots info offline (the sha256 the proof commits to, the
  pending calendars, the Bitcoin block height and that block's merkle root) and compares it with the file's current
  sha256. For a proof that already has a Bitcoin attestation it also runs ots verify: with a Bitcoin node the client
  prints the block date; without a node the block time stays empty, and the block height and merkle root are given
  so they can be checked by hand on any block explorer.
- verify(path): judges exactly as C-PREREG-IMMUTABLE (thesis-ci v0.2.1) does, step for step: first ots info and a
  local sha256 comparison, then ots verify. True: passed; None: cannot be verified on this machine (still pending, no
  Bitcoin node, calendars unreachable, timeout); False: failed (does not match the file, proof unreadable or corrupt,
  no proof).

What client 0.7.2 actually does (from reading its source and testing it):

- stamp sends only sha256(sha256(file) ‖ 16 random bytes) to the calendars; neither the file nor its hash leaves this
  machine. There are four calendars by default; stamping succeeds only when at least two answer within 5 seconds,
  otherwise no proof is written. A freshly written proof is "pending": the calendars promise to write it into
  Bitcoin, and there is usually a block only a few hours later.
- verify first compares the file's sha256 locally; a mismatch gives "File does not match original!". After that, a
  pending proof has to contact the calendars; a complete proof does not contact them and gets the block header
  directly from a Bitcoin node (Bitcoin Core's RPC, using the local bitcoin.conf and .cookie; a pruned node works
  too). The client has no block-explorer fallback: without a node it only reports
  "Could not connect to Bitcoin node"; the block time is printed only when verification passes, and only as a date.
- When the calendars cannot be reached (DNS or TLS errors), the verify output for a pending proof does not contain
  "pending": thesis-ci v0.2.0's C-PREREG-IMMUTABLE therefore judged a sound proof to be an error; v0.2.1 compares the
  hash locally first and only warns. An upgraded proof does not contact the calendars when verified and carries the
  whole path to the Bitcoin block header, so proofs are still upgraded before the deadline
  (.github/workflows/timestamp.yml upgrades every 6 hours).
- The client waits for calendar answers with no timeout; here every call is limited to OTS_TIMEOUT seconds.
  --no-cache is always added: the result depends only on the proof file itself, the same as on CI (where the cache
  is always empty).
- A macOS Python installed from python.org may have no CA certificates, and connecting to a calendar then fails with
  CERTIFICATE_VERIFY_FAILED; setting SSL_CERT_FILE=/etc/ssl/cert.pem fixes it.

Command line (at the repository root):

    python -m pipeline.timestamp stamp FILE...     # or --prereg: every items file whose deadline has not passed
                                                   # (skipping those that have a proof of the same content)
    python -m pipeline.timestamp upgrade FILE...   # or --prereg: every pre-registration proof in the repository
    python -m pipeline.timestamp status FILE...    # or --prereg: every items file
    python -m pipeline.timestamp verify FILE...    # exit status: 0 all passed, 1 some failed,
                                                   # 3 some cannot be verified on this machine

FILE can be the timestamped file or its .ots. stamp, upgrade and status exit with status 1 when there is a failure or
an error.
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
OTS_TIMEOUT = 120  # seconds, the same as C-PREREG-IMMUTABLE
PROOF_SUFFIX = ".ots"
SETTLEMENT_SUFFIX = ".settlement.yml"
PREREG_ITEMS_GLOB = "companies/*/prereg/*.yml"  # minus *.settlement.yml: the items files (same as thesis-ci)
PREREG_PROOFS_GLOB = "companies/*/prereg/*.yml.ots"

EXIT_OK, EXIT_FAILED, EXIT_UNVERIFIED = 0, 1, 3

# ots info output (Timestamp.str_tree in opentimestamps 0.4)
_INFO_BITCOIN = re.compile(
    r"verify BitcoinBlockHeaderAttestation\((\d+)\)(?:[ \t]*\r?\n[ \t]*# Bitcoin block merkle root ([0-9a-f]{64}))?"
)
_INFO_PENDING = re.compile(r"verify PendingAttestation\('([^']*)'\)")
# The line ots verify prints when it passes. It prints the block date in the local time zone; run_ots sets TZ to UTC.
_VERIFY_OK = re.compile(r"Success! Bitcoin block (\d+) attests existence as of (\d{4}-\d{2}-\d{2})")
_NO_NODE = re.compile(r"Could not connect to (?:local )?Bitcoin node", re.I)
_CALENDAR_LINE = re.compile(r"^Calendar (\S+): (.+)$", re.M)
# The two regexes of thesis-ci (v0.2.1) src/thesis_ci/checks/archive.py, copied verbatim, so that verify() judges
# exactly as C-PREREG-IMMUTABLE does (tests/test_timestamp.py compares them case by case when thesis-ci is
# installed). The first takes the sha256 the proof commits to from ots info (sha256 only). The second judges the ots
# verify output once the hashes match: a match means the proof itself is bad; any failure that does not match only
# means it cannot be verified on this machine.
OTS_INFO_DIGEST_RE = re.compile(r"File sha256 hash:\s*([0-9a-f]{64})", re.I)
OTS_BAD_PROOF_RE = re.compile(r"does not match|mismatch|bad timestamp|invalid|corrupt|not a timestamp", re.I)
UPGRADE_COMPLETE = "Success! Timestamp complete"
UPGRADE_PENDING = "Failed! Timestamp not complete"
CALENDAR_PENDING = "Pending confirmation"  # the calendar's answer: received, not yet written into Bitcoin
_SSL_HINT = "本机 Python 没有 CA 证书：设 SSL_CERT_FILE=/etc/ssl/cert.pem（macOS）后重试"


class TimestampError(RuntimeError):
    """The OpenTimestamps client cannot be found or cannot run."""


@dataclasses.dataclass(frozen=True)
class StepResult:
    """The result of stamp() or upgrade() for one file.

    action: for stamp, stamped (newly stamped), already_stamped (a proof of the same content already exists),
    refused, failed (stamping did not succeed); for upgrade, upgraded (just upgraded to a Bitcoin attestation),
    complete (already a complete proof), pending (still pending), failed.
    """

    path: Path  # the timestamped file
    proof: Path  # <file>.ots
    action: str
    detail: str = ""
    block_height: int | None = None

    @property
    def ok(self) -> bool:
        return self.action not in ("refused", "failed")


@dataclasses.dataclass(frozen=True)
class ProofStatus:
    """The result of status() and verify().

    state: missing (no proof), pending, attested (has a Bitcoin attestation), error (proof corrupt, does not match the
    file, verification failed).
    verified: verify() splits it three ways like C-PREREG-IMMUTABLE: True passed, None cannot be verified on this
    machine, False failed. status() verifies only proofs that already have a Bitcoin attestation (for the block
    time); for the others it is None.
    block_time: the block date the client prints after verifying against a Bitcoin node (UTC, date only); None
    without a node.
    merkle_root: that block's merkle root (in the order block explorers display it); without a node it can be
    checked by hand on any block explorer.
    """

    path: Path
    proof: Path
    state: str
    detail: str = ""
    file_sha256: str | None = None  # the sha256 the proof commits to
    block_height: int | None = None  # the earliest Bitcoin attestation
    merkle_root: str | None = None
    block_time: str | None = None
    calendars: tuple[str, ...] = ()  # the calendars still pending
    verified: bool | None = None


class ProofInfo(NamedTuple):
    """The content parsed from the ots info output."""

    file_digest: str  # the sha256 the proof commits to
    bitcoin: tuple[tuple[int, str | None], ...]  # (block height, merkle root)
    calendars: tuple[str, ...]


# ---------------------------------------------------------------- client


def ots_executable() -> str:
    """The ots on PATH; if there is none, the one in the same directory as the running interpreter (for a virtual
    environment that is not activated)."""
    found = shutil.which("ots")
    if found:
        return found
    beside = Path(sys.executable).with_name("ots")
    if beside.is_file() and os.access(beside, os.X_OK):
        return str(beside)
    raise TimestampError("找不到 OpenTimestamps 客户端 ots：pip install -r requirements.txt")


def run_ots(args: Sequence[str], timeout: float = OTS_TIMEOUT) -> tuple[int | None, str]:
    """Run `ots args...` and return (exit status, stdout and stderr combined); the exit status is None on a timeout.

    This module runs the client only through this function (the tests replace it).
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
    """ots info output → ProofInfo; None when the sha256 the proof commits to is not found (unreadable, or not
    sha256). Like thesis-ci, looks only at the output, not at the exit status."""
    head = OTS_INFO_DIGEST_RE.search(output)
    if head is None:
        return None
    bitcoin = tuple((int(height), root or None) for height, root in _INFO_BITCOIN.findall(output))
    calendars = tuple(dict.fromkeys(_INFO_PENDING.findall(output)))
    return ProofInfo(head.group(1).lower(), bitcoin, calendars)


def classify_verify(returncode: int | None, output: str) -> bool | None:
    """The ots verify result, asked for only after the sha256 of the file and the proof matched locally. Judged as in
    C-PREREG-IMMUTABLE: exit status 0 passes; output saying the proof itself is bad (OTS_BAD_PROOF_RE) fails; anything
    else (a timeout included) cannot be verified on this machine."""
    if returncode is None:
        return None
    if returncode == 0:
        return True
    return False if OTS_BAD_PROOF_RE.search(output) else None


# ---------------------------------------------------------------- files


def proof_path(path: str | os.PathLike[str]) -> Path:
    """<file>.ots: the proof sits next to the timestamped file (SPEC §2.1)."""
    p = Path(path)
    return p.with_name(p.name + PROOF_SUFFIX)


def _pair(path: str | os.PathLike[str]) -> tuple[Path, Path]:
    """(timestamped file, proof); path can be either of the two."""
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
    """A .yml under companies/<TICKER>/prereg/ other than a settlement file: <period>.yml and <period>-owner.yml
    (the same test as in thesis-ci)."""
    p = Path(os.path.abspath(path))
    return (
        p.suffix == ".yml"
        and not p.name.endswith(SETTLEMENT_SUFFIX)
        and p.parent.name == "prereg"
        and p.parent.parent.parent.name == "companies"
    )


def prereg_deadline(path: str | os.PathLike[str]) -> dt.datetime:
    """The items file's deadline (ISO 8601 with a time zone offset). Raises ValueError when it cannot be read."""
    try:
        data = load_yaml_text(Path(path).read_text(encoding="utf-8"))  # duplicate keys are an error
    except Exception as exc:  # noqa: BLE001  (I/O, encoding, YAML syntax, duplicate keys: all treated as unreadable)
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
    """Replace the content of path atomically: write a temporary file in the same directory first, then os.replace."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------- summaries of the output


def _host(url: str) -> str:
    return urlparse(url).netloc or url


def _last_line(output: str, returncode: int | None = None) -> str:
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    return lines[-1] if lines else f"退出状态 {returncode}"


def _with_hint(text: str, output: str) -> str:
    return f"{text}（{_SSL_HINT}）" if "CERTIFICATE_VERIFY_FAILED" in output else text


def _calendar_summary(output: str) -> str:
    """Group the "Calendar <url>: <message>" lines by message:
    alice..., bob...: Pending confirmation in Bitcoin blockchain."""
    by_message: dict[str, list[str]] = {}
    for url, message in _CALENDAR_LINE.findall(output):
        by_message.setdefault(message.strip(), []).append(_host(url))
    return "；".join(f"{', '.join(hosts)}: {message}" for message, hosts in by_message.items())


def _pending_detail(calendars: Sequence[str]) -> str:
    return f"待确认：{len(calendars)} 个日历（{', '.join(_host(u) for u in calendars)}）"


def _stamp_failure(returncode: int | None, output: str) -> str:
    """The errors of each calendar when ots -v stamp fails (printed only with -v), without duplicates."""
    if returncode is None:
        return output
    lines = [
        ln.strip() for ln in output.splitlines()
        if ln.strip() and not ln.startswith(("Submitting to remote calendar", "Doing "))
    ]
    text = "; ".join(dict.fromkeys(lines)) or f"退出状态 {returncode}"
    return _with_hint(text, output)


# ---------------------------------------------------------------- status and verification


def _run_info(proof: Path) -> tuple[int | None, str]:
    return run_ots(["--no-cache", "info", os.path.abspath(proof)])


def _info(path: Path, proof: Path, run: tuple[int | None, str] | None = None) -> ProofStatus:
    """Read the proof offline (ots info): the sha256 it commits to, Bitcoin attestations, pending calendars. Does not
    compare it with the file."""
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
    """Run ots verify for st (already read by _info) and fill in verified the way C-PREREG-IMMUTABLE judges."""
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
    """The proof's status. Offline (ots info and the local hash); a proof that already has a Bitcoin attestation also
    gets an ots verify run for the block time (needs a Bitcoin node)."""
    target, proof = _pair(path)
    if not proof.is_file():
        return ProofStatus(target, proof, "missing", "没有证明")
    st = _info(target, proof)
    if st.file_sha256 is None:
        return st  # the proof cannot be read
    if not target.is_file():
        return dataclasses.replace(st, state="error", detail="被打时间戳的文件不存在")
    mismatch = _digest_mismatch(st, target)
    if mismatch:
        return mismatch
    return _run_verify(st) if st.state == "attested" else st


def _digest_mismatch(st: ProofStatus, target: Path) -> ProofStatus | None:
    """Compare locally the sha256 the proof commits to with the file's current sha256 (no network needed); on a
    mismatch, return a failed result."""
    actual = file_sha256(target)
    if actual == st.file_sha256:
        return None
    return dataclasses.replace(
        st, state="error", verified=False,
        detail=f"文件与证明不符：文件在打时间戳之后改过（证明的 sha256 {st.file_sha256}，文件现在 {actual}）",
    )


def verify(path: str | os.PathLike[str]) -> ProofStatus:
    """Step for step the same as C-PREREG-IMMUTABLE (ots_verify in thesis-ci v0.2.1):

    1. ots info: None on a timeout; False when the output has no sha256 the proof commits to (unreadable); False when
       it differs from the file's sha256.
    2. Once they match, ots verify -f <file> <proof>: None on a timeout; True for exit status 0; False when the output
       says the proof itself is bad; None otherwise (no Bitcoin node, still pending, calendars unreachable). For a
       pending proof this step contacts the calendars.
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


# ---------------------------------------------------------------- stamping and upgrading


def stamp(paths: Iterable[str | os.PathLike[str]], *, now: dt.datetime | None = None) -> list[StepResult]:
    """Timestamp each file and write the proof next to it (<file>.ots). The timestamped files are read, never written.

    A problem with one file does not affect the others: refusals and failures are recorded in each file's own result
    (see StepResult.ok); no exception is raised.
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
    too_late = None  # why an items file can no longer be stamped: the deadline has passed, or it cannot be read
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
    # An absolute path, so a file name starting with - is not taken for an option; the client writes the proof to
    # <argument>.ots
    returncode, output = run_ots(["--no-cache", "-v", "stamp", os.path.abspath(path)])
    if not proof.is_file():
        return StepResult(path, proof, "failed", f"ots stamp 没有写出证明：{_stamp_failure(returncode, output)}")
    made = _info(path, proof)
    if made.file_sha256 != digest:
        proof.unlink()  # a proof just written and not yet committed
        return StepResult(path, proof, "failed", f"新证明与文件不符（{made.detail}；打时间戳时文件变了？），已删掉，请重打")
    return StepResult(path, proof, "stamped", made.detail)


def upgrade(paths: Iterable[str | os.PathLike[str]]) -> list[StepResult]:
    """Upgrade pending proofs to proofs with a Bitcoin attestation (contacts the calendars). Each path can be the
    timestamped file or its .ots; only the proofs are changed."""
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
        # The client renames the old proof to <proof>.bak: let that stay in the temporary directory only.
        work = Path(tmp) / proof.name
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
    if CALENDAR_PENDING not in output:  # no calendar answered "received, pending": the query failed this time
        return StepResult(path, proof, "failed", _with_hint(f"没有日历答复，未能升级（{summary}）", output))
    return StepResult(path, proof, "pending", f"仍待确认（{summary}）")


# ---------------------------------------------------------------- pre-registration files in the repository


def prereg_items_files(root: str | os.PathLike[str] = REPO_ROOT) -> list[Path]:
    """The pre-registration items files in the repository: companies/*/prereg/*.yml minus *.settlement.yml."""
    return sorted(
        p for p in Path(root).glob(PREREG_ITEMS_GLOB) if p.is_file() and not p.name.endswith(SETTLEMENT_SUFFIX)
    )


def files_to_stamp(root: str | os.PathLike[str] = REPO_ROOT, now: dt.datetime | None = None) -> list[Path]:
    """What stamp --prereg covers: items files whose deadline has not passed, or whose deadline cannot be read (left to
    stamp() to refuse and report).

    Files past their deadline are not included: a missing or mismatched proof is an error reported by
    C-PREREG-IMMUTABLE, and stamping after the fact cannot fix it.
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
    """The pre-registration proofs in the repository: companies/*/prereg/*.yml.ots."""
    return sorted(p for p in Path(root).glob(PREREG_PROOFS_GLOB) if p.is_file())


# ---------------------------------------------------------------- command line

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
    """A relative path for files under the current directory, an absolute path for the rest."""
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
