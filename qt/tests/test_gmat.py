# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Tests for the GMAT MCQ sub-topic (category) tagger in aqt.gmat."""

import os
import tempfile

from anki.collection import Collection
from aqt.gmat import (
    GMAT_SUBTOPIC_TAGS_VERSION,
    classify_mcq_subtopic,
    ensure_mcq_notetype,
    ensure_mcq_subtopic_tags,
)


def _empty_col() -> Collection:
    folder = tempfile.mkdtemp()
    return Collection(os.path.join(folder, "collection.anki2"))


def _add_mcq(col, question: str, tags: list[str], answer: str = "C") -> int:
    nt = col.models.by_name("GMAT MCQ")
    note = col.new_note(nt)
    note["Question"] = question
    note["Answer"] = answer
    note.tags = list(tags)
    col.add_note(note, col.decks.id("Default"))
    return note.id


def test_classify_representative_questions():
    # Quant, matched by the ordered keyword rules (most specific first).
    assert (
        classify_mcq_subtopic("Quant", "The area of a triangle with base 4")
        == "Geometry"
    )
    assert (
        classify_mcq_subtopic("Quant", "In how many ways can you arrange 3 cones")
        == "Probability"
    )
    assert (
        classify_mcq_subtopic("Quant", "Solve for x in the equation 2x+1=5")
        == "Algebra"
    )
    assert classify_mcq_subtopic("Quant", "What is 15 percent of 200?") == "Arithmetic"
    # DataInsights always defaults to the table-reading category.
    assert (
        classify_mcq_subtopic("DataInsights", "week date opponent result")
        == "TableAnalysis"
    )
    # No confident match -> None (the note stays section-only).
    assert classify_mcq_subtopic("Quant", "How old is John today?") is None
    # Unknown section -> None.
    assert classify_mcq_subtopic("Nonsense", "anything") is None


def test_tagger_adds_expected_category_tags():
    col = _empty_col()
    ensure_mcq_notetype(col)
    geo = _add_mcq(col, "What is the area of the circle?", ["GMAT::Quant"])
    di = _add_mcq(col, "driver constructor laps time grid", ["GMAT::DataInsights"])

    tagged = ensure_mcq_subtopic_tags(col)
    assert tagged == 2

    assert "GMAT::Quant::Geometry" in col.get_note(geo).tags
    assert "GMAT::DataInsights::TableAnalysis" in col.get_note(di).tags


def test_no_match_note_stays_section_only():
    col = _empty_col()
    ensure_mcq_notetype(col)
    plain = _add_mcq(col, "How old is John today?", ["GMAT::Quant"])

    ensure_mcq_subtopic_tags(col)

    tags = col.get_note(plain).tags
    assert tags == ["GMAT::Quant"]
    assert not any(len(t.split("::")) >= 3 for t in tags)


def test_already_subtagged_note_is_untouched():
    col = _empty_col()
    ensure_mcq_notetype(col)
    cr = _add_mcq(
        col,
        "The argument above assumes which of the following?",
        ["GMAT::Verbal::CriticalReasoning"],
    )

    tagged = ensure_mcq_subtopic_tags(col)

    # Nothing added: the note already carries a sub-topic tag.
    assert tagged == 0
    assert col.get_note(cr).tags == ["GMAT::Verbal::CriticalReasoning"]


def test_tagger_is_idempotent_and_version_guarded():
    col = _empty_col()
    ensure_mcq_notetype(col)
    geo = _add_mcq(col, "the perimeter of the rectangle", ["GMAT::Quant"])

    first = ensure_mcq_subtopic_tags(col)
    assert first == 1
    assert "GMAT::Quant::Geometry" in col.get_note(geo).tags

    # A second run is a no-op (guarded by the stored version), so it neither
    # re-tags nor duplicates.
    second = ensure_mcq_subtopic_tags(col)
    assert second == 0
    assert col.get_note(geo).tags.count("GMAT::Quant::Geometry") == 1
    assert (
        col.get_config("gmat_mcq_subtopic_tags_version") == GMAT_SUBTOPIC_TAGS_VERSION
    )
