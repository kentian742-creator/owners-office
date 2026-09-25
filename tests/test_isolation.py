"""pipeline/isolation.py 的测试：从成品里删去审计类输入不该看到的章节（00 §G6）。内容都是合成的。"""

from __future__ import annotations

import pytest

from pipeline import isolation, outputs

DOSSIER = """---
company: TEST
doc: dossier
---
# 档案

## 8. 内在价值
驱动因素。

## 9. 不买它的理由
最强的反方论证。

```text
## 9. 代码块里的标题不算
```

## 10. 监控仪表盘
指标。

## 12. 论点破坏者
一旦发生即作废。

## 芒格矩阵
落位。
"""


def test_sections_round_trip():
    sections = isolation.split_sections(DOSSIER)
    assert "".join(text for _, text in sections) == DOSSIER
    assert [title for title, _ in sections] == [None, "8. 内在价值", "9. 不买它的理由", "10. 监控仪表盘", "12. 论点破坏者", "芒格矩阵"]


def test_dossier_without_parts_9_and_12():
    kept, removed = isolation.dossier_without_9_12(DOSSIER)
    assert "不买它的理由" not in kept and "论点破坏者" not in kept and "代码块里的标题不算" not in kept
    assert "## 10. 监控仪表盘" in kept and "## 芒格矩阵" in kept and kept.startswith("---\ncompany: TEST")
    assert "最强的反方论证" in removed and "一旦发生即作废" in removed


def test_missing_sections_fail_loudly_instead_of_leaking():
    with pytest.raises(ValueError, match="第 7 部分"):
        isolation.report_without_counter(DOSSIER)
    renamed = DOSSIER.replace("## 9. 不买它的理由", "## 九、不买它的理由")
    with pytest.raises(ValueError, match="第 9 部分"):
        isolation.dossier_without_9_12(renamed)


def test_report_and_document_counter_sections():
    report = "## 6. 估值\n甲\n## 7. 反方最强论点\n乙\n## 8. 结论\n丙\n"
    kept, counter = isolation.report_without_counter(report)
    assert kept == "## 6. 估值\n甲\n## 8. 结论\n丙\n" and counter == "## 7. 反方最强论点\n乙\n"
    document = "## 一线认知\n甲\n## 竞争生态全景（芒格式）\n乙\n## 宏观\n丙\n"
    kept, counter = isolation.document_without_counter(document, "07")
    assert "竞争生态全景" not in kept and counter.startswith("## 竞争生态全景")
    assert isolation.document_without_counter(document, "06") == (document, "")
    with pytest.raises(ValueError, match="反过来想"):
        isolation.document_without_counter(document, "08")
    with pytest.raises(ValueError, match="doc"):
        isolation.document_without_counter(document, "09")


def test_thesis_without_loss_paths():
    thesis = "company: TEST\nthesis:\n  summary: 摘要\n  permanent_loss_paths:\n    - 路径一\n  pillars: []\n"
    stripped = outputs.load_yaml_text(isolation.thesis_without_loss_paths(thesis))
    assert stripped == {"company": "TEST", "thesis": {"summary": "摘要", "pillars": []}}


def test_question_list_is_stripped_and_shuffled_deterministically():
    questions = "\n".join(
        f"- {{id: Q0{i}, question: 问题{i}, kind: pillar, maps_to: [P{i}]}}" for i in range(1, 7)
    ) + "\n- {id: Q07, question: 还有什么值得注意, kind: open, maps_to: []}\n"
    stripped = outputs.load_yaml_text(isolation.question_list_stripped(questions, seed="TEST-FY2026Q3"))
    assert all(set(q) == {"id", "question"} for q in stripped)
    assert stripped[-1]["id"] == "Q07"  # 开放问题不动
    assert sorted(q["id"] for q in stripped) == [f"Q0{i}" for i in range(1, 8)]
    assert isolation.question_list_stripped(questions, seed="TEST-FY2026Q3") == isolation.question_list_stripped(
        questions, seed="TEST-FY2026Q3"
    )
    orders = {tuple(q["id"] for q in outputs.load_yaml_text(isolation.question_list_stripped(questions, seed=s)))
              for s in ("a", "b", "c", "d")}
    assert len(orders) > 1  # 确实打乱了
