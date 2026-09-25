"""按角色裁剪输入的内容一半（00 §G6）：从成品里删去审计类输入不该看到的章节。本模块不调用模型。

pipeline/llm.py 按 agents/<role>.yml 的 can_see／cannot_see 拒绝不该给的输入名；这里负责把成品本身
裁成那些输入名所指的内容：

- 反方第一遍（04B、09B）拿到 *_without_counter、dossier_without_9_12、thesis_without_loss_paths；
  第二遍才补给被删去的部分（product_counter_section、document_counter_section）。
- 盲推（14A）拿到 question_list_stripped：删去 kind 与 maps_to，打乱顺序（开放问题不动）。

要删的章节找不到时抛出 ValueError，而不是原样交出：原样交出等于把反方内容泄露给第一遍。
"""

from __future__ import annotations

import random
import re
from collections.abc import Iterable
from typing import Any

from .outputs import dump_yaml, load_yaml_text

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^[ \t]*(```|~~~)")
_NUMBER = re.compile(r"^(?:第\s*)?(\d+)\s*(?:[.、．:：)）]|部分|节|\s)")

# 06–08 各自的反方内容（09B 第一遍删去）；06 没有专门的反方一节，09B 审 06 只跑一遍。
COUNTER_TITLES = {"06": (), "07": ("竞争生态全景",), "08": ("反过来想",)}


def split_sections(markdown: str, level: int = 2) -> list[tuple[str | None, str]]:
    """按 level 级标题切块：[(标题文字；第一个标题之前的部分为 None, 块文本), …]。

    各块拼起来等于原文；代码围栏里的 # 不算标题；更深一级的标题留在所属的块里。
    """
    sections: list[tuple[str | None, str]] = []
    title: str | None = None
    chunk: list[str] = []
    fenced = False
    for line in markdown.splitlines(keepends=True):
        if _FENCE.match(line):
            fenced = not fenced
        match = None if fenced else _HEADING.match(line.rstrip("\r\n"))
        if match and len(match.group(1)) == level:
            if chunk or title is not None:
                sections.append((title, "".join(chunk)))
            title, chunk = match.group(2), [line]
        else:
            chunk.append(line)
    if chunk or title is not None:
        sections.append((title, "".join(chunk)))
    return sections


def section_number(title: str) -> int | None:
    """标题开头的编号：“9. 不买它的理由”“第 12 部分 …”→ 9、12。"""
    match = _NUMBER.match(title.strip())
    return int(match.group(1)) if match else None


def strip_sections(
    markdown: str, *, numbers: Iterable[int] = (), titles: Iterable[str] = (), level: int = 2
) -> tuple[str, str]:
    """删去编号在 numbers 里、或标题含 titles 任一词的 level 级章节。

    返回 (删后的正文, 删去的部分)。任何一个编号或标题找不到都抛出 ValueError。
    """
    numbers, titles = set(numbers), tuple(titles)
    kept: list[str] = []
    removed: list[str] = []
    found_numbers: set[int] = set()
    found_titles: set[str] = set()
    for title, text in split_sections(markdown, level):
        number = section_number(title) if title is not None else None
        hits = [t for t in titles if title is not None and t in title]
        if number in numbers or hits:
            removed.append(text)
            if number in numbers:
                found_numbers.add(number)
            found_titles.update(hits)
        else:
            kept.append(text)
    missing = [f"第 {n} 部分" for n in sorted(numbers - found_numbers)] + [f"“{t}”" for t in titles if t not in found_titles]
    if missing:
        raise ValueError(f"找不到要删去的章节：{'、'.join(missing)}（按 {'#' * level} 级标题查找）")
    return "".join(kept), "".join(removed)


def dossier_without_9_12(dossier: str) -> tuple[str, str]:
    """档案删去第 9 部分（不买它的理由）和第 12 部分（论点破坏者）。返回 (删后, 删去的部分)。"""
    return strip_sections(dossier, numbers=(9, 12))


def report_without_counter(report: str) -> tuple[str, str]:
    """研报删去第 7 节（反方最强论点）。返回 (product_without_counter, product_counter_section)。"""
    return strip_sections(report, numbers=(7,))


def document_without_counter(document: str, doc: str) -> tuple[str, str]:
    """06–08 删去各自的反方内容（COUNTER_TITLES）。返回 (document_without_counter, document_counter_section)。"""
    if doc not in COUNTER_TITLES:
        raise ValueError(f"doc 必须是 {'、'.join(COUNTER_TITLES)} 之一，收到 {doc!r}")
    titles = COUNTER_TITLES[doc]
    return strip_sections(document, titles=titles) if titles else (document, "")


def thesis_without_loss_paths(thesis_yaml: str) -> str:
    """thesis.yml 删去 thesis.permanent_loss_paths（重新输出，注释一并去掉）。"""
    data = load_yaml_text(thesis_yaml)
    if not isinstance(data, dict):
        raise ValueError("thesis.yml 应当是一个映射")
    thesis = data.get("thesis")
    if isinstance(thesis, dict) and "permanent_loss_paths" in thesis:
        data = {**data, "thesis": {k: v for k, v in thesis.items() if k != "permanent_loss_paths"}}
    return dump_yaml(data)


def question_list_stripped(question_list_yaml: str, *, seed: str) -> str:
    """14Q 的问题清单交给盲推之前：每题删去 kind 与 maps_to，非开放问题按 seed 确定地打乱，开放问题留在原位。

    题目 id 保留：14B 按 id 把盲推的回答与公司经理的回答对上。同一 seed 得到同一顺序，输入哈希可复现。
    """
    data: Any = load_yaml_text(question_list_yaml)
    if isinstance(data, dict) and isinstance(data.get("questions"), list):
        data = data["questions"]
    if not isinstance(data, list) or not all(isinstance(q, dict) for q in data):
        raise ValueError("question_list 应当是问题的列表（每题 id、question、kind、maps_to）")
    movable = [i for i, q in enumerate(data) if q.get("kind") != "open"]
    order = [data[i] for i in movable]
    random.Random(seed).shuffle(order)
    shuffled = list(data)
    for i, question in zip(movable, order):
        shuffled[i] = question
    stripped = [{k: v for k, v in q.items() if k not in ("kind", "maps_to")} for q in shuffled]
    return dump_yaml(stripped)
