"""pipeline/timestamp.py 的测试：预注册的 OpenTimestamps 时间戳（docs/decisions/0018）。

不联网：客户端换成 FakeOts，它按 opentimestamps-client 0.7.2 的参数、输出与退出状态作答；输出取自 2026-09-25 的
真实运行（REAL_* 与各条消息）。预注册文件都是合成的。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import subprocess
from fnmatch import fnmatch
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from pipeline import timestamp

REPO_ROOT = Path(__file__).resolve().parents[1]
UTC = dt.timezone.utc
EST = dt.timezone(dt.timedelta(hours=-5))

AGGREGATORS = (
    "https://a.pool.opentimestamps.org",
    "https://b.pool.opentimestamps.org",
    "https://a.pool.eternitywall.com",
    "https://ots.btc.catallaxy.com",
)
CALENDARS = (
    "https://finney.calendar.eternitywall.com",
    "https://bob.btc.calendar.opentimestamps.org",
    "https://alice.btc.calendar.opentimestamps.org",
    "https://btc.calendar.catallaxy.com",
)
HEIGHT = 915000
MERKLE_ROOT = "7ed564d727789259b93385dd9d1a6fe87933e33f1211de0a6bfc212428bd3ee3"

# ots info：冒烟测试里刚打出的证明（四个日历待确认）
SMOKE_SHA256 = "1d89624e1ab526963af98ca35cabaf09a2401aeb54781e719787a172f3b30df0"
REAL_INFO_PENDING = f"""File sha256 hash: {SMOKE_SHA256}
Timestamp:
append 7426798b9002757aab0afa1ee30a56c3
sha256
 -> append 92bcd65f961c75d1fa6882ed6cc4f695
    sha256
    prepend a74f89f164c84726838704e81dd127ce1035976cd54a210b56560e69d5d0dff6
    sha256
    prepend 6ab6a3d4
    append 7cfca31924cce875
    verify PendingAttestation('https://finney.calendar.eternitywall.com')
 -> append e7d86dc1eb6871cd
    sha256
    append 6d38bca88e34f4d0db44a7134d8257b0f3386c8db3055fdb0798a956c5609803
    sha256
    prepend 6ab6a3d4
    append 6d664b5d78c8ac71
    verify PendingAttestation('https://bob.btc.calendar.opentimestamps.org')
 -> append f1764cbde3eafa74
    sha256
    append 73682974ac3d8a5a7eb0a088cf16f31beb4f30982ce31fdcd15f0586a5b7a6bd
    sha256
    prepend 6ab6a3d4
    append 862d8a379b9d352a
    verify PendingAttestation('https://alice.btc.calendar.opentimestamps.org')
 -> append f565768edb5e297c356ff677fddeeaa2
    sha256
    append c1a6d528200d55d462b76b7f449218488255656211d0c1d4bf379059ee478b40
    sha256
    prepend 6ab6a3d4
    append 38d4a364f234d509
    verify PendingAttestation('https://btc.calendar.catallaxy.com')
"""
# ots info：一个日历仍待确认、另一个已写进比特币的证明
REAL_INFO_ATTESTED = f"""File sha256 hash: 8eacc3a70238ec0b6eb687a09e70e3a1d237bcaba4d612fbd9ca588d6f4b9066
Timestamp:
append 00000000000000000000000000000000
sha256
 -> append 02020202
    sha256
    verify PendingAttestation('https://bob.btc.calendar.opentimestamps.org')
 -> prepend 01010101
    sha256
    verify BitcoinBlockHeaderAttestation({HEIGHT})
    # Bitcoin block merkle root {MERKLE_ROOT}
"""
NO_NODE = (
    "Could not connect to Bitcoin node: Cookie file unusable ([Errno 2] No such file or directory: "
    "'/home/runner/.bitcoin/.cookie') and rpcpassword not specified in the configuration file: "
    "'/home/runner/.bitcoin/bitcoin.conf'"
)
PENDING_REPLIES = "\n".join(f"Calendar {url}: Pending confirmation in Bitcoin blockchain" for url in CALENDARS)
SSL_FAILURES = "\n".join(
    f"Calendar {url}: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer "
    "certificate (_ssl.c:1082)" for url in CALENDARS
)
MISMATCH = "File does not match original!"
NOT_A_PROOF = "Error! {!r} is not a timestamp file."
STAMP_FAILED = "Failed to create timestamp: need at least 2 attestations but received 0 within timeout"
DNS_FAILURE = "<urlopen error [Errno 8] nodename nor servname provided, or not known>"


def sha256_of(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fake_info(digest: str, height: int | None) -> str:
    """ots info 的输出：真实的待确认输出换上 digest；有区块时在 alice 一支加上比特币确认（与真实输出同一写法）。"""
    text = REAL_INFO_PENDING.replace(SMOKE_SHA256, digest)
    if height is not None:
        alice = "    verify PendingAttestation('https://alice.btc.calendar.opentimestamps.org')"
        text = text.replace(alice, f"{alice}\n    sha256\n    verify BitcoinBlockHeaderAttestation({height})\n"
                                   f"    # Bitcoin block merkle root {MERKLE_ROOT}")
    return text


def read_fake(proof) -> dict | None:
    try:
        return json.loads(Path(proof).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class FakeOts:
    """opentimestamps-client 0.7.2 的替身：同样的参数，同样的输出与退出状态，不联网。

    证明文件在这里是一段 JSON（真实证明是二进制）：timestamp.py 从不自己读证明，只读 ots info 的输出。
    confirmed：日历已把承诺写进比特币；node：本机有比特币节点；calendars_up：日历联系得上。
    """

    def __init__(self, *, confirmed: bool = False, node: bool = False, calendars_up: bool = True):
        self.confirmed, self.node, self.calendars_up = confirmed, node, calendars_up
        self.calls: list[list[str]] = []

    def __call__(self, args, timeout=timestamp.OTS_TIMEOUT):
        args = list(args)
        self.calls.append(args)
        i = next(i for i, a in enumerate(args) if a in ("stamp", "upgrade", "verify", "info"))
        return getattr(self, "_" + args[i])(args[i + 1:])

    def commands(self) -> list[str]:
        return [next(a for a in call if not a.startswith("-")) for call in self.calls]

    def _stamp(self, rest):
        (target,) = rest
        submitted = "\n".join(f"Submitting to remote calendar {url}" for url in AGGREGATORS)
        if not self.calendars_up:
            return 1, "\n".join(["Doing 2-of-4 request, timeout is 5 seconds", submitted, *[DNS_FAILURE] * 4, STAMP_FAILED])
        with open(target + ".ots", "x", encoding="utf-8") as fh:  # 客户端以 'xb' 打开：已存在就失败
            json.dump({"sha256": sha256_of(target), "height": None}, fh)
        return 0, f"Doing 2-of-4 request, timeout is 5 seconds\n{submitted}\n0.43 seconds elapsed"

    def _info(self, rest):
        (proof,) = rest
        data = read_fake(proof)
        if data is None:
            return 1, NOT_A_PROOF.format(proof)
        return 0, fake_info(data["sha256"], data["height"])

    def _upgrade(self, rest):
        (proof,) = rest
        data = read_fake(proof)
        if data is None:
            return 1, NOT_A_PROOF.format(proof).rstrip(".")
        if data["height"] is not None:
            return 0, timestamp.UPGRADE_COMPLETE
        if not self.calendars_up:
            return 1, f"{SSL_FAILURES}\n{timestamp.UPGRADE_PENDING}"
        if not self.confirmed:
            return 1, f"{PENDING_REPLIES}\n{timestamp.UPGRADE_PENDING}"
        os.rename(proof, proof + ".bak")  # 客户端的做法：旧证明改名为 .bak，再写新证明
        with open(proof, "x", encoding="utf-8") as fh:
            json.dump({**data, "height": HEIGHT}, fh)
        return 0, f"Got 1 attestation(s) from {CALENDARS[2]}\n{timestamp.UPGRADE_COMPLETE}"

    def _verify(self, rest):
        assert rest[0] == "-f", rest  # 与 C-PREREG-IMMUTABLE 一样显式给出被打时间戳的文件
        target, proof = rest[1], rest[2]
        data = read_fake(proof)
        if data is None:
            return 1, NOT_A_PROOF.format(proof)
        if sha256_of(target) != data["sha256"]:  # 客户端先在本地比对哈希
            return 1, MISMATCH
        height = data["height"]
        if height is None:  # 待确认：先问日历（只在内存里升级）
            if not self.calendars_up:
                return 1, SSL_FAILURES
            if not self.confirmed:
                return 1, PENDING_REPLIES
            height = HEIGHT
        if not self.node:
            return 1, NO_NODE
        return 0, f"Success! Bitcoin block {height} attests existence as of 2026-10-02 UTC"


@pytest.fixture
def ots(monkeypatch) -> FakeOts:
    client = FakeOts()
    monkeypatch.setattr(timestamp, "run_ots", client)
    return client


PREREG = """company: APP
event:
  period: FY2026Q3
  expected_release: 2026-11-05
  form: 8-K
  placeholder: true
deadline: "2026-11-04T23:59:59-05:00"
author: system
horizon: mixed
items:
  - id: APP-FY2026Q3-1
    statement: 合成的测试条目
    probability: 0.6
    criterion: 合成
    data_source: 合成
    horizon: quarter
    resolves_by: 2026-11-30
    domain: other
    added_by: system
"""
DEADLINE = dt.datetime(2026, 11, 4, 23, 59, 59, tzinfo=EST)
BEFORE = dt.datetime(2026, 10, 30, 12, 0, tzinfo=UTC)  # 截止前五天多


def put(root: Path, rel: str, text: str = PREREG) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def items_file(root: Path, name: str = "FY2026Q3.yml", text: str = PREREG) -> Path:
    return put(root, f"companies/APP/prereg/{name}", text)


def stamped(path: Path) -> timestamp.StepResult:
    (result,) = timestamp.stamp([path], now=BEFORE)
    assert result.action == "stamped", result
    return result


# ---------------------------------------------------------------- ots info 的解析


def test_parse_info_pending_real_output():
    info = timestamp.parse_info(REAL_INFO_PENDING)
    assert info == timestamp.ProofInfo(SMOKE_SHA256, (), CALENDARS)


def test_parse_info_attested_real_output():
    info = timestamp.parse_info(REAL_INFO_ATTESTED)
    assert info.bitcoin == ((HEIGHT, MERKLE_ROOT),) and info.calendars == (CALENDARS[1],)
    assert timestamp.parse_info(NOT_A_PROOF.format("x.yml.ots")) is None
    assert timestamp.parse_info("File sha1 hash: " + "ab" * 20 + "\nTimestamp:\n") is None  # 只认 sha256


# ---------------------------------------------------------------- stamp


def test_stamp_writes_the_proof_next_to_the_file_and_never_touches_the_file(tmp_path, ots):
    path = put(tmp_path, "notes.txt", "任意文件\n")
    before = path.read_bytes()
    result = stamped(path)
    assert result.proof == tmp_path / "notes.txt.ots" and result.proof.is_file() and result.ok
    assert path.read_bytes() == before
    assert "4 个日历" in result.detail and "alice.btc.calendar.opentimestamps.org" in result.detail
    assert ots.commands() == ["stamp", "info"]  # 打完再读一遍新证明，确认它证明的就是这份内容
    assert ots.calls[0][:3] == ["--no-cache", "-v", "stamp"] and ots.calls[0][3] == os.path.abspath(path)


def test_stamping_the_same_content_again_changes_nothing(tmp_path, ots):
    path = items_file(tmp_path)
    proof = stamped(path).proof
    first = proof.read_bytes()
    ots.calls.clear()
    (again,) = timestamp.stamp([path], now=BEFORE)
    assert again.action == "already_stamped" and again.ok and proof.read_bytes() == first
    assert ots.commands() == ["info"]


def test_stamp_refuses_to_replace_a_proof_of_other_content(tmp_path, ots):
    path = items_file(tmp_path)
    proof = stamped(path).proof
    first = proof.read_bytes()
    path.write_text(PREREG.replace("probability: 0.6", "probability: 0.7"), encoding="utf-8")
    ots.calls.clear()
    (result,) = timestamp.stamp([path], now=BEFORE)
    assert result.action == "refused" and not result.ok
    assert "另一份内容" in result.detail and "删掉旧证明" in result.detail
    assert proof.read_bytes() == first and "stamp" not in ots.commands()
    (late,) = timestamp.stamp([path], now=DEADLINE)  # 截止之后：删旧证明重打也不行，只能改回原内容
    assert late.action == "refused" and "已过" in late.detail and "改回" in late.detail
    assert "删掉旧证明" not in late.detail and proof.read_bytes() == first


def test_stamp_refuses_to_overwrite_an_unreadable_proof(tmp_path, ots):
    path = put(tmp_path, "notes.txt", "x\n")
    path.with_name("notes.txt.ots").write_bytes(b"not a proof")
    (result,) = timestamp.stamp([path], now=BEFORE)
    assert result.action == "refused" and "读不出" in result.detail
    assert path.with_name("notes.txt.ots").read_bytes() == b"not a proof" and "stamp" not in ots.commands()


@pytest.mark.parametrize("name", ["FY2026Q3.yml", "FY2026Q3-owner.yml"])
@pytest.mark.parametrize("now", [DEADLINE, DEADLINE.astimezone(UTC) + dt.timedelta(seconds=1),
                                 dt.datetime(2026, 11, 20, tzinfo=UTC)])
def test_stamp_refuses_an_items_file_after_its_deadline(tmp_path, ots, name, now):
    """系统文件与所有者文件都一样：截止时刻起不再打，客户端一次都不调用。"""
    path = items_file(tmp_path, name)
    (result,) = timestamp.stamp([path], now=now)
    assert result.action == "refused" and "已过" in result.detail and "2026-11-04T23:59:59-05:00" in result.detail
    assert not result.proof.exists() and ots.calls == []


def test_stamp_accepts_an_items_file_until_the_deadline(tmp_path, ots):
    path = items_file(tmp_path)
    (result,) = timestamp.stamp([path], now=DEADLINE.astimezone(UTC) - dt.timedelta(seconds=1))
    assert result.action == "stamped"


@pytest.mark.parametrize("text, why", [
    (PREREG.replace('deadline: "2026-11-04T23:59:59-05:00"\n', ""), "没有 deadline"),
    (PREREG.replace('"2026-11-04T23:59:59-05:00"', '"2026-11-04T23:59:59"'), "不是带时区偏移的时间"),
    (PREREG.replace('"2026-11-04T23:59:59-05:00"', "2026-11-04"), "不是带时区偏移的时间"),
    (PREREG + 'deadline: "2026-12-31T23:59:59-05:00"\n', "duplicate key"),
    ("company: [APP\n", "读不出"),
])
def test_stamp_refuses_an_items_file_whose_deadline_cannot_be_read(tmp_path, ots, text, why):
    (result,) = timestamp.stamp([items_file(tmp_path, text=text)], now=BEFORE)
    assert result.action == "refused" and why in result.detail and ots.calls == []


def test_a_matching_proof_after_the_deadline_is_simply_already_there(tmp_path, ots):
    path = items_file(tmp_path)
    stamped(path)
    (result,) = timestamp.stamp([path], now=DEADLINE + dt.timedelta(days=30))
    assert result.action == "already_stamped" and result.ok


def test_the_deadline_rule_covers_items_files_only(tmp_path):
    assert timestamp.is_prereg_items_file(tmp_path / "companies/APP/prereg/FY2026Q3.yml")
    assert timestamp.is_prereg_items_file(tmp_path / "companies/APP/prereg/FY2026Q3-owner.yml")
    assert not timestamp.is_prereg_items_file(tmp_path / "companies/APP/prereg/FY2026Q3.settlement.yml")
    assert not timestamp.is_prereg_items_file(tmp_path / "companies/APP/thesis.yml")
    assert not timestamp.is_prereg_items_file(tmp_path / "prereg/FY2026Q3.yml")


def test_stamp_refuses_proofs_and_naive_clocks(tmp_path, ots):
    proof = put(tmp_path, "notes.txt.ots", "{}")
    (result,) = timestamp.stamp([proof], now=BEFORE)
    assert result.action == "refused" and ots.calls == []
    with pytest.raises(ValueError, match="时区"):
        timestamp.stamp([proof], now=dt.datetime(2026, 10, 30, 12, 0))


@pytest.mark.parametrize("failure, expected", [
    (DNS_FAILURE, "nodename nor servname"),
    ("<urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed>", "SSL_CERT_FILE"),
])
def test_stamp_failure_keeps_the_calendar_errors(tmp_path, monkeypatch, failure, expected):
    client = FakeOts(calendars_up=False)

    def run(args, timeout=None):
        returncode, output = client(args)
        return returncode, output.replace(DNS_FAILURE, failure)

    monkeypatch.setattr(timestamp, "run_ots", run)
    (result,) = timestamp.stamp([put(tmp_path, "notes.txt", "x\n")], now=BEFORE)
    assert result.action == "failed" and not result.proof.exists()
    assert "need at least 2 attestations" in result.detail and expected in result.detail
    assert result.detail.count(failure) == 1  # 四个日历的同一个错误只写一次


# ---------------------------------------------------------------- upgrade


def test_upgrade_leaves_a_pending_proof_alone(tmp_path, ots):
    path = items_file(tmp_path)
    proof = stamped(path).proof
    first = proof.read_bytes()
    (result,) = timestamp.upgrade([proof])
    assert result.action == "pending" and result.ok and "Pending confirmation" in result.detail
    assert proof.read_bytes() == first
    assert sorted(p.name for p in proof.parent.iterdir()) == ["FY2026Q3.yml", "FY2026Q3.yml.ots"]  # 没有 .bak


def test_upgrade_replaces_the_proof_once_bitcoin_confirms_it(tmp_path, ots):
    path = items_file(tmp_path)
    content = path.read_bytes()
    proof = stamped(path).proof
    first = proof.read_bytes()
    ots.confirmed = True
    (result,) = timestamp.upgrade([path])  # 给被打时间戳的文件也行
    assert result.action == "upgraded" and result.block_height == HEIGHT and str(HEIGHT) in result.detail
    assert proof.read_bytes() != first and path.read_bytes() == content
    assert sorted(p.name for p in proof.parent.iterdir()) == ["FY2026Q3.yml", "FY2026Q3.yml.ots"]  # 没有 .bak、.tmp
    assert timestamp.status(path).state == "attested"
    ots.calls.clear()
    (again,) = timestamp.upgrade([proof])
    assert again.action == "complete" and ots.commands() == ["info"]  # 完整的证明不再问日历


def test_upgrade_fails_when_no_calendar_answers(tmp_path, ots):
    proof = stamped(items_file(tmp_path)).proof
    first = proof.read_bytes()
    ots.calendars_up = False
    (result,) = timestamp.upgrade([proof])
    assert result.action == "failed" and "没有日历答复" in result.detail and "SSL_CERT_FILE" in result.detail
    assert proof.read_bytes() == first


def test_upgrade_reports_missing_and_broken_proofs(tmp_path, ots):
    broken = put(tmp_path, "notes.txt.ots", "garbage")
    missing, bad = timestamp.upgrade([tmp_path / "other.txt", broken])
    assert (missing.action, bad.action) == ("failed", "failed")
    assert missing.detail == "没有证明" and "not a timestamp file" in bad.detail


# ---------------------------------------------------------------- status


def test_status_of_a_pending_proof_is_offline(tmp_path, ots):
    path = items_file(tmp_path)
    stamped(path)
    ots.calls.clear()
    st = timestamp.status(path)
    assert (st.state, st.verified, st.block_height, st.calendars) == ("pending", None, None, CALENDARS)
    assert st.file_sha256 == sha256_of(path) and ots.commands() == ["info"]


def test_status_without_a_bitcoin_node(tmp_path, ots):
    """有比特币确认、没有节点：给出区块高度与 merkle root，区块时间留空，不算失败。"""
    path = items_file(tmp_path)
    stamped(path)
    ots.confirmed = True
    timestamp.upgrade([path])
    st = timestamp.status(path.with_name("FY2026Q3.yml.ots"))
    assert (st.state, st.block_height, st.merkle_root, st.block_time, st.verified) == (
        "attested", HEIGHT, MERKLE_ROOT, None, None)
    assert "没有比特币节点" in st.detail and MERKLE_ROOT in st.detail


def test_status_with_a_bitcoin_node_gives_the_block_date(tmp_path, ots):
    path = items_file(tmp_path)
    stamped(path)
    ots.confirmed = ots.node = True
    timestamp.upgrade([path])
    st = timestamp.status(path)
    assert (st.state, st.block_height, st.block_time, st.verified) == ("attested", HEIGHT, "2026-10-02", True)


def test_status_catches_a_changed_file_without_the_network(tmp_path, ots):
    path = items_file(tmp_path)
    stamped(path)
    path.write_text(PREREG.replace("0.6", "0.65"), encoding="utf-8")
    ots.calls.clear()
    st = timestamp.status(path)
    assert (st.state, st.verified) == ("error", False) and "打时间戳之后改过" in st.detail
    assert ots.commands() == ["info"]


def test_status_missing_and_broken(tmp_path, ots):
    path = items_file(tmp_path)
    assert timestamp.status(path).state == "missing"
    path.with_name("FY2026Q3.yml.ots").write_bytes(b"\x00garbage")
    st = timestamp.status(path)
    assert st.state == "error" and "证明读不出" in st.detail


# ---------------------------------------------------------------- verify


@pytest.mark.parametrize("setup, verified, state", [
    ({}, None, "pending"),  # 日历：还没写进比特币
    ({"calendars_up": False}, None, "pending"),  # 日历联系不上：哈希已在本地比对相符，只是无法核验（v0.2.1）
    ({"confirmed": True}, None, "pending"),  # 没有比特币节点
    ({"confirmed": True, "node": True}, True, "attested"),
])
def test_verify_outcomes(tmp_path, ots, setup, verified, state):
    path = items_file(tmp_path)
    stamped(path)
    for key, value in setup.items():
        setattr(ots, key, value)
    st = timestamp.verify(path)
    assert (st.verified, st.state) == (verified, state), st
    assert ots.calls[-1] == ["--no-cache", "verify", "-f", os.path.abspath(path), os.path.abspath(st.proof)]


def test_verify_without_a_node_after_upgrade(tmp_path, ots):
    """升级后的证明核验时不问日历；没有节点只能核对到“文件与证明相符”。"""
    path = items_file(tmp_path)
    stamped(path)
    ots.confirmed = True
    timestamp.upgrade([path])
    ots.calendars_up = False  # 完整的证明用不着日历
    st = timestamp.verify(path)
    assert (st.verified, st.state, st.block_height) == (None, "attested", HEIGHT)
    assert "文件与证明相符" in st.detail and MERKLE_ROOT in st.detail


def test_verify_failures(tmp_path, ots):
    """文件改过、证明读不出都在本地判定，用不着 ots verify，也用不着网络。"""
    path = items_file(tmp_path)
    missing = timestamp.verify(path)
    assert (missing.state, missing.verified) == ("missing", False)
    stamped(path)
    path.write_text(PREREG + "# 改了一个字\n", encoding="utf-8")
    ots.calls.clear()
    changed = timestamp.verify(path)
    assert (changed.state, changed.verified) == ("error", False) and "文件与证明不符" in changed.detail
    path.with_name("FY2026Q3.yml.ots").write_bytes(b"garbage")
    unreadable = timestamp.verify(path)
    assert unreadable.verified is False and "证明读不出" in unreadable.detail
    assert ots.commands() == ["info", "info"]


@pytest.mark.parametrize("slow", ["info", "verify"])
def test_verify_timeout_cannot_verify(tmp_path, ots, monkeypatch, slow):
    path = items_file(tmp_path)
    stamped(path)
    monkeypatch.setattr(timestamp, "run_ots", lambda args, timeout=None: (
        (None, f"ots {slow} timed out after 120 s") if slow in args else ots(args)))
    st = timestamp.verify(path)
    assert st.verified is None and "timed out" in st.detail


# 真实的 ots verify 输出（0.7.2）→ 文件与证明的哈希相符时应有的判定。
VERIFY_SAMPLES = {
    "success": (0, f"Success! Bitcoin block {HEIGHT} attests existence as of 2026-10-02 UTC", True),
    "no-node": (1, NO_NODE, None),
    "no-local-node": (1, f"Got 1 attestation(s) from {CALENDARS[2]}\nCould not connect to local Bitcoin node: "
                         "[Errno 111] Connection refused", None),
    "pending": (1, PENDING_REPLIES, None),
    "calendars-unreachable-while-pending": (1, SSL_FAILURES, None),
    "height-not-found": (1, f"Bitcoin block height 999999999 not found; {HEIGHT} is highest known block", None),
    "bitcoin-disabled": (1, "Not checking Bitcoin attestation; Bitcoin disabled\nTo verify manually, check that "
                            f"Bitcoin block {HEIGHT} has merkleroot {MERKLE_ROOT}", None),
    "verify-timeout": (None, "", None),
    "mismatch": (1, MISMATCH, False),
    "bad-merkleroot": (1, "Bitcoin verification failed: Digest does not match merkleroot", False),
    "not-a-proof": (1, NOT_A_PROOF.format("FY2026Q3.yml.ots"), False),
    "invalid": (1, "Invalid timestamp file 'FY2026Q3.yml.ots': Tried to read 32 bytes but got only 7 bytes", False),
}
# ots info 的回答 → 判定（这些情况下 ots verify 说什么都不影响结果）
INFO_ANSWERS = {"digest-differs": False, "unreadable": False, "info-timeout": None}
VERDICT_CASES = [("digest-matches", sample) for sample in VERIFY_SAMPLES] + [
    (info, sample) for info in INFO_ANSWERS for sample in ("success", "no-node")]


@pytest.mark.parametrize("info, sample", VERDICT_CASES, ids=[f"{i}/{s}" for i, s in VERDICT_CASES])
def test_verify_judges_like_c_prereg_immutable(tmp_path, monkeypatch, info, sample):
    """同一个客户端（同样的 ots info 与 ots verify 回答），本模块的 verify() 与 thesis-ci 的 ots_verify 判定一致。"""
    archive = pytest.importorskip("thesis_ci.checks.archive")
    target = items_file(tmp_path)
    proof = put(tmp_path, "companies/APP/prereg/FY2026Q3.yml.ots", "proof")
    digest = sha256_of(target) if info == "digest-matches" else "0" * 64
    info_answer = {
        "unreadable": (1, NOT_A_PROOF.format(str(proof))),
        "info-timeout": (None, ""),
    }.get(info, (0, fake_info(digest, None)))
    verify_answer = VERIFY_SAMPLES[sample][:2]

    def answer(args):
        command = next(a for a in args if a in ("info", "verify"))
        return info_answer if command == "info" else verify_answer

    def run(cmd, **kw):  # thesis-ci：subprocess.run([ots, *args], …)
        returncode, output = answer(cmd[1:])
        if returncode is None:
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        return subprocess.CompletedProcess(cmd, returncode, "", output)

    def run_ots(args, timeout=None):  # 本模块
        returncode, output = answer(args)
        command = next(a for a in args if a in ("info", "verify"))
        return (None, f"ots {command} timed out after 120 s") if returncode is None else (returncode, output)

    monkeypatch.setattr(archive, "subprocess", SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired))
    monkeypatch.setattr(timestamp, "run_ots", run_ots)
    theirs, _detail = archive.ots_verify("ots", target, proof)
    ours = timestamp.verify(target).verified
    expected = VERIFY_SAMPLES[sample][2] if info == "digest-matches" else INFO_ANSWERS[info]
    assert ours is theirs is expected


def test_verify_uses_the_checks_own_patterns_and_timeout():
    archive = pytest.importorskip("thesis_ci.checks.archive")
    for name in ("OTS_INFO_DIGEST_RE", "OTS_BAD_PROOF_RE"):
        ours, theirs = getattr(timestamp, name), getattr(archive, name)
        assert (ours.pattern, ours.flags) == (theirs.pattern, theirs.flags), name
    assert timestamp.OTS_TIMEOUT == archive.OTS_TIMEOUT


# ---------------------------------------------------------------- 仓库里的预注册文件与命令行


def test_prereg_discovery(tmp_path):
    current = items_file(tmp_path)
    owner = items_file(tmp_path, "FY2026Q3-owner.yml", PREREG.replace("author: system", "author: owner"))
    items_file(tmp_path, "FY2026Q3.settlement.yml", "company: APP\nperiod: FY2026Q3\nresults: []\n")
    old = put(tmp_path, "companies/PDD/prereg/FY2026Q2.yml", PREREG.replace("2026-11-04T", "2026-08-24T"))
    broken = put(tmp_path, "companies/PDD/prereg/FY2026Q3.yml", "company: PDD\n")
    assert timestamp.prereg_items_files(tmp_path) == [owner, current, old, broken]  # 按路径排序："-owner" 在 "." 之前
    assert timestamp.files_to_stamp(tmp_path, BEFORE) == [owner, current, broken]  # 截止已过的补不了，不在其中
    put(tmp_path, "companies/APP/prereg/FY2026Q3.yml.ots", "{}")
    assert timestamp.prereg_proofs(tmp_path) == [tmp_path / "companies/APP/prereg/FY2026Q3.yml.ots"]


def test_cli_stamp_prereg_then_status(tmp_path, ots, monkeypatch, capsys):
    monkeypatch.setattr(timestamp, "_utcnow", lambda: BEFORE)
    items_file(tmp_path)
    put(tmp_path, "companies/PDD/prereg/FY2026Q2.yml", PREREG.replace("2026-11-04T", "2026-08-24T"))
    assert timestamp.main(["stamp", "--prereg", "--root", str(tmp_path)]) == timestamp.EXIT_OK
    out = capsys.readouterr().out
    assert "stamped" in out and "FY2026Q3.yml" in out and "FY2026Q2" not in out
    assert timestamp.main(["stamp", "--prereg", "--root", str(tmp_path)]) == timestamp.EXIT_OK
    assert "already_stamped" in capsys.readouterr().out
    assert timestamp.main(["status", "--prereg", "--root", str(tmp_path)]) == timestamp.EXIT_OK
    out = capsys.readouterr().out
    assert re.search(r"^pending\s+\S*FY2026Q3\.yml", out, re.M) and re.search(r"^missing\s+\S*FY2026Q2\.yml", out, re.M)


def test_cli_exit_codes(tmp_path, ots, monkeypatch, capsys):
    monkeypatch.setattr(timestamp, "_utcnow", lambda: BEFORE)
    path = items_file(tmp_path)
    assert timestamp.main(["stamp", str(path)]) == timestamp.EXIT_OK
    assert timestamp.main(["upgrade", str(path)]) == timestamp.EXIT_OK  # 仍待确认不算失败
    ots.confirmed = True
    assert timestamp.main(["upgrade", "--prereg", "--root", str(tmp_path)]) == timestamp.EXIT_OK
    assert timestamp.main(["verify", str(path)]) == timestamp.EXIT_UNVERIFIED  # 没有比特币节点
    ots.node = True
    assert timestamp.main(["verify", str(path) + ".ots"]) == timestamp.EXIT_OK
    path.write_text(PREREG + "# 截止前改了\n", encoding="utf-8")
    assert timestamp.main(["verify", str(path)]) == timestamp.EXIT_FAILED
    assert timestamp.main(["status", str(path)]) == timestamp.EXIT_FAILED
    assert timestamp.main(["stamp", str(path)]) == timestamp.EXIT_FAILED  # 不覆盖证明
    out = capsys.readouterr().out
    assert re.search(r"^refused\s", out, re.M) and re.search(r"^verified\s", out, re.M)
    with pytest.raises(SystemExit):
        timestamp.main(["status"])  # 既没有文件也没有 --prereg


def test_run_ots_passes_utc_and_reports_timeouts(tmp_path, monkeypatch):
    seen = {}

    def run(cmd, **kw):
        seen.update(cmd=cmd, **kw)
        return subprocess.CompletedProcess(cmd, 1, "out\n", "err\n")

    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setattr(timestamp, "ots_executable", lambda: "/opt/bin/ots")
    monkeypatch.setattr(timestamp, "subprocess", SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired))
    assert timestamp.run_ots(["info", "a.ots"]) == (1, "out\n\nerr")
    assert seen["cmd"] == ["/opt/bin/ots", "info", "a.ots"] and seen["env"]["TZ"] == "UTC" and seen["timeout"] == 120
    monkeypatch.setattr(timestamp, "subprocess", SimpleNamespace(run=slow, TimeoutExpired=subprocess.TimeoutExpired))
    assert timestamp.run_ots(["--no-cache", "verify", "-f", "a", "a.ots"]) == (None, "ots verify timed out after 120 s")


def test_missing_client_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(timestamp.shutil, "which", lambda name: None)
    monkeypatch.setattr(timestamp.sys, "executable", str(tmp_path / "python"))
    with pytest.raises(timestamp.TimestampError, match="requirements.txt"):
        timestamp.ots_executable()


# ---------------------------------------------------------------- 工作流


def test_timestamp_workflow_permissions_and_loop_guard():
    """只有打时间戳的 job 能写仓库，且只在 main 上；它提交的 *.ots 不匹配触发它的 paths；提交带固定尾注；
    lint 装了客户端（C-PREREG-IMMUTABLE 才能核验），版本取自 requirements.txt。"""
    workflows = REPO_ROOT / ".github" / "workflows"
    wf = yaml.safe_load((workflows / "timestamp.yml").read_text(encoding="utf-8"))
    assert wf["permissions"] == {"contents": "read"}
    ((_, job),) = wf["jobs"].items()
    assert job["permissions"] == {"contents": "write"} and "refs/heads/main" in job["if"]
    push = wf["on"]["push"]
    assert push["branches"] == ["main"] and "schedule" in wf["on"]
    assert any(fnmatch("companies/APP/prereg/FY2026Q3-owner.yml", p) for p in push["paths"])
    assert not any(fnmatch("companies/APP/prereg/FY2026Q3.yml.ots", p) for p in push["paths"])
    script = "\n".join(step.get("run", "") for step in job["steps"])
    assert "stamp --prereg" in script and "upgrade --prereg" in script
    assert "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>" in script
    lint = (workflows / "lint.yml").read_text(encoding="utf-8")
    assert "-c requirements.txt opentimestamps-client" in lint
    requirements = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert re.search(r"^opentimestamps-client==\d", requirements, re.M)
