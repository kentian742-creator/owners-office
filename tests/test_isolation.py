"""Tests of pipeline/isolation.py: removing from finished products the sections that audit-type inputs must not see
(00 §G6). The headings are the fixed English headings of prompts 01A, 02, 07 and 08; the content is synthetic."""

from __future__ import annotations

import pytest

from pipeline import isolation, outputs

DOSSIER = """---
company: TEST
doc: dossier
---
# Dossier

## 8. Valuation
Drivers.

## 9. Bear case
The strongest argument against.

```text
## 9. A heading inside a code block does not count
```

## 10. Monitoring
Metrics.

## 11. Thesis
The thesis.

## 12. Thesis breakers
Void once it happens.

## Munger matrix
Placement.
"""

REPORT = ("## 6. Munger matrix\nA\n## 7. Strongest bear case\nB\n## 8. Peer comparison\nC\n"
          "## 12. Where this judgment is most likely wrong\nD\n")
DOCUMENT_07 = ("## Frontline view (Lynch-style)\nA\n## Competitive landscape (Munger-style)\nB\n"
               "## Management and culture: present-day evidence\nC\n## Macro and cycle position (Druckenmiller-style)\nD\n")
DOCUMENT_08 = ("## What greatness would take (Buffett-style)\nA\n## Second-order effects and optionality\nB\n"
               "## Forks in the road\nC\n## Invert (Munger-style)\nD\n")


def test_sections_round_trip():
    sections = isolation.split_sections(DOSSIER)
    assert "".join(text for _, text in sections) == DOSSIER
    assert [title for title, _ in sections] == [None, "8. Valuation", "9. Bear case", "10. Monitoring", "11. Thesis",
                                                "12. Thesis breakers", "Munger matrix"]


@pytest.mark.parametrize("title, number", [("9. Bear case", 9), ("12) Thesis breakers", 12), ("Part 9: Bear case", 9),
                                           ("section 7 - Strongest bear case", 7), ("Munger matrix", None)])
def test_section_numbers(title, number):
    assert isolation.section_number(title) == number


def test_dossier_without_parts_9_and_12():
    kept, removed = isolation.dossier_without_9_12(DOSSIER)
    assert "Bear case" not in kept and "Thesis breakers" not in kept and "inside a code block" not in kept
    assert "## 10. Monitoring" in kept and "## 11. Thesis\n" in kept and "## Munger matrix" in kept
    assert kept.startswith("---\ncompany: TEST")
    assert "The strongest argument against" in removed and "Void once it happens" in removed


def test_dossier_parts_are_found_by_title_when_unnumbered_and_in_any_case():
    renamed = DOSSIER.replace("## 9. Bear case", "## BEAR CASE").replace("## 12. Thesis breakers", "## thesis breakers")
    kept, removed = isolation.dossier_without_9_12(renamed)
    assert "argument against" in removed and "Void once" in removed and "## 11. Thesis\n" in kept


def test_missing_sections_fail_loudly_instead_of_leaking():
    with pytest.raises(ValueError, match=r'section 7 \("Strongest bear case"\)'):
        isolation.report_without_counter(DOSSIER)
    renamed = DOSSIER.replace("## 9. Bear case", "## Nine: the case against")
    with pytest.raises(ValueError, match=r'section 9 \("Bear case"\)'):
        isolation.dossier_without_9_12(renamed)


def test_report_counter_section():
    kept, counter = isolation.report_without_counter(REPORT)
    assert kept == "## 6. Munger matrix\nA\n## 8. Peer comparison\nC\n## 12. Where this judgment is most likely wrong\nD\n"
    assert counter == "## 7. Strongest bear case\nB\n"


def test_document_counter_sections_tolerate_the_munger_style_suffix():
    kept, counter = isolation.document_without_counter(DOCUMENT_07, "07")
    assert "Competitive landscape" not in kept and counter.startswith("## Competitive landscape (Munger-style)")
    kept, counter = isolation.document_without_counter(DOCUMENT_08, "08")
    assert "Invert" not in kept and counter == "## Invert (Munger-style)\nD\n" and "## Forks in the road" in kept
    numbered = DOCUMENT_08.replace("## Invert (Munger-style)", "## 4. INVERT, Munger-style")
    assert isolation.document_without_counter(numbered, "08")[1].startswith("## 4. INVERT")
    assert isolation.document_without_counter(DOCUMENT_07, "06") == (DOCUMENT_07, "")


def test_document_counter_titles_match_whole_words_only():
    assert isolation.title_matches("Invert (Munger-style)", "Invert")
    assert not isolation.title_matches("Inverted yield curves", "Invert")
    assert isolation.title_matches("2. The competitive  landscape", "Competitive landscape")
    with pytest.raises(ValueError, match='"Invert"'):
        isolation.document_without_counter(DOCUMENT_07, "08")
    with pytest.raises(ValueError, match="doc"):
        isolation.document_without_counter(DOCUMENT_07, "09")


def test_thesis_without_loss_paths():
    thesis = "company: TEST\nthesis:\n  summary: Summary\n  permanent_loss_paths:\n    - Path one\n  pillars: []\n"
    stripped = outputs.load_yaml_text(isolation.thesis_without_loss_paths(thesis))
    assert stripped == {"company": "TEST", "thesis": {"summary": "Summary", "pillars": []}}


def test_question_list_is_stripped_and_shuffled_deterministically():
    questions = "\n".join(
        f"- {{id: Q0{i}, question: Question {i}, kind: pillar, maps_to: [P{i}]}}" for i in range(1, 7)
    ) + "\n- {id: Q07, question: What else deserves attention, kind: open, maps_to: []}\n"
    stripped = outputs.load_yaml_text(isolation.question_list_stripped(questions, seed="TEST-FY2026Q3"))
    assert all(set(q) == {"id", "question"} for q in stripped)
    assert stripped[-1]["id"] == "Q07"  # the open question stays put
    assert sorted(q["id"] for q in stripped) == [f"Q0{i}" for i in range(1, 8)]
    assert isolation.question_list_stripped(questions, seed="TEST-FY2026Q3") == isolation.question_list_stripped(
        questions, seed="TEST-FY2026Q3"
    )
    orders = {tuple(q["id"] for q in outputs.load_yaml_text(isolation.question_list_stripped(questions, seed=s)))
              for s in ("a", "b", "c", "d")}
    assert len(orders) > 1  # it really shuffles
