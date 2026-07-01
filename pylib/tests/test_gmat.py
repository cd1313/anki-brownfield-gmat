# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from tests.shared import getEmptyCol


def test_gmat_topic_mastery():
    col = getEmptyCol()

    # Two term notes in different GMAT sections.
    for tag in ["GMAT::Quant::Algebra", "GMAT::Verbal::CR"]:
        note = col.newNote()
        note["Front"] = tag
        note["Back"] = "x"
        note.tags = [tag]
        col.addNote(note)

    # Review one card so its section has a graded review.
    col.sched.answerCard(col.sched.getCard(), 3)  # Good

    # Call the new backend RPC end-to-end (round-trips through rsbridge).
    topics = col._backend.get_topic_mastery(
        search="",
        tag_prefix="GMAT",
        r_threshold=0.8,
        time_budget_secs=60,
        min_reviews=1,
        min_cards=1,
    )

    by_topic = {t.topic: t for t in topics}
    # Both sections show up, aggregated one level under the prefix.
    assert set(by_topic) == {"GMAT::Quant", "GMAT::Verbal"}
    assert by_topic["GMAT::Quant"].total_cards == 1
    assert by_topic["GMAT::Verbal"].total_cards == 1

    # Exactly one card was reviewed, so exactly one section crosses the give-up
    # threshold and the other abstains.
    assert sum(t.reviewed_cards for t in topics) == 1
    assert sum(1 for t in topics if t.has_score) == 1


def test_gmat_grade_mcq():
    col = getEmptyCol()

    # A minimal "GMAT MCQ"-shaped note type: a Question and an Answer field.
    mm = col.models
    nt = mm.new("GMAT MCQ")
    for field in ["Question", "Answer"]:
        mm.add_field(nt, mm.new_field(field))
    tmpl = mm.new_template("Card 1")
    tmpl["qfmt"] = "{{Question}}"
    tmpl["afmt"] = "{{Question}}<hr>{{Answer}}"
    mm.add_template(nt, tmpl)
    mm.add(nt)

    note = col.new_note(nt)
    note["Question"] = "2 + 2 = ?"
    note["Answer"] = "C"
    col.add_note(note, deck_id=1)
    cid = note.cards()[0].id

    # Correct choice (case-insensitive) grades correct via the backend RPC.
    res = col._backend.grade_mcq(card_id=cid, chosen="c")
    assert res.correct
    assert res.correct_answer == "C"

    # Wrong choice grades incorrect but still returns the correct answer.
    res = col._backend.grade_mcq(card_id=cid, chosen="A")
    assert not res.correct
    assert res.correct_answer == "C"


def _add_mcq_notetype(col):
    mm = col.models
    nt = mm.new("GMAT MCQ")
    for field in ["Question", "Answer"]:
        mm.add_field(nt, mm.new_field(field))
    tmpl = mm.new_template("Card 1")
    tmpl["qfmt"] = "{{Question}}"
    tmpl["afmt"] = "{{Question}}<hr>{{Answer}}"
    mm.add_template(nt, tmpl)
    mm.add(nt)
    return nt


def test_gmat_practice_pool():
    col = getEmptyCol()
    nt = _add_mcq_notetype(col)
    cids = []
    for ans in ["A", "B", "C"]:
        note = col.new_note(nt)
        note["Question"] = "Q"
        note["Answer"] = ans
        col.add_note(note, deck_id=1)
        cids.append(note.cards()[0].id)

    search = 'note:"GMAT MCQ"'

    # Cycle 1: all three available; draw returns one of them.
    res = col._backend.next_practice_card(search=search, cycle=1)
    assert not res.exhausted
    assert res.remaining == 3
    assert res.card_id in cids

    # Mark all done for cycle 1 -> pool exhausted.
    for cid in cids:
        col._backend.mark_practice_done(card_id=cid, cycle=1)
    res = col._backend.next_practice_card(search=search, cycle=1)
    assert res.exhausted
    assert res.remaining == 0

    # Cycle 2 (a reset): everything is available again.
    res = col._backend.next_practice_card(search=search, cycle=2)
    assert not res.exhausted
    assert res.remaining == 3
