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
    # took_millis=0 means "grade only, don't log the attempt".
    res = col._backend.grade_mcq(card_id=cid, chosen="c", took_millis=0)
    assert res.correct
    assert res.correct_answer == "C"

    # Wrong choice grades incorrect but still returns the correct answer.
    res = col._backend.grade_mcq(card_id=cid, chosen="A", took_millis=0)
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
    # Empty tag_prefix -> the plain random draw (no IRT weighting).

    # Cycle 1: all three available; draw returns one of them.
    res = col._backend.next_practice_card(search=search, cycle=1, tag_prefix="")
    assert not res.exhausted
    assert res.remaining == 3
    assert res.card_id in cids

    # Mark all done for cycle 1 -> pool exhausted.
    for cid in cids:
        col._backend.mark_practice_done(card_id=cid, cycle=1)
    res = col._backend.next_practice_card(search=search, cycle=1, tag_prefix="")
    assert res.exhausted
    assert res.remaining == 0

    # Cycle 2 (a reset): everything is available again.
    res = col._backend.next_practice_card(search=search, cycle=2, tag_prefix="")
    assert not res.exhausted
    assert res.remaining == 3


def test_gmat_practice_recommends_weakest_section():
    col = getEmptyCol()
    nt = _add_mcq_notetype(col)

    def add_section(tag, n):
        cids = []
        for _ in range(n):
            note = col.new_note(nt)
            note["Question"] = "Q"
            note["Answer"] = "C"
            note.tags = [tag]
            col.add_note(note, deck_id=1)
            cids.append(note.cards()[0].id)
        return cids

    quant = add_section("GMAT::Quant::Algebra", 3)
    verbal = add_section("GMAT::Verbal::CR", 3)

    # Quant answered wrong (low ability); Verbal answered right (high ability).
    for cid in quant:
        col._backend.grade_mcq(card_id=cid, chosen="A", took_millis=3000)
    for cid in verbal:
        col._backend.grade_mcq(card_id=cid, chosen="C", took_millis=3000)

    # With tag_prefix set, the IRT-weighted recommender should serve a card from
    # the weakest section (Quant) rather than a random one.
    res = col._backend.next_practice_card(
        search='note:"GMAT MCQ"', cycle=1, tag_prefix="GMAT"
    )
    assert not res.exhausted
    assert res.card_id in quant


def test_gmat_estimate_readiness():
    col = getEmptyCol()
    nt = _add_mcq_notetype(col)
    cids = []
    for _ in range(12):
        note = col.new_note(nt)
        note["Question"] = "Q"
        note["Answer"] = "C"
        note.tags = ["GMAT::Quant::Algebra"]
        col.add_note(note, deck_id=1)
        cids.append(note.cards()[0].id)

    # Log 12 correct attempts (well within budget). Passing took_millis records
    # each as a non-scheduling revlog entry — the IRT model's input.
    for cid in cids:
        res = col._backend.grade_mcq(card_id=cid, chosen="C", took_millis=3000)
        assert res.correct

    def readiness():
        return {
            s.section: s
            for s in col._backend.estimate_readiness(
                search='note:"GMAT MCQ"',
                tag_prefix="GMAT",
                time_budget_secs=120,
                section_minutes=45,
                min_responses=10,
                min_coverage=0.5,
                max_se=1.0,
            )
        }

    by = readiness()
    assert "GMAT::Quant" in by
    q = by["GMAT::Quant"]
    assert q.responses == 12
    assert q.items_attempted == 12
    assert q.items_available == 12
    assert q.coverage == 1.0
    assert q.pct_correct == 1.0
    assert q.theta > 0.0  # all correct -> positive ability
    assert q.has_score
    assert 60.0 <= q.score <= 90.0
    assert q.score_low <= q.score <= q.score_high
    assert abs(q.within_budget_rate - 1.0) < 1e-6
    assert q.confidence in ("low", "medium", "high")

    # took_millis == 0 must NOT log another response.
    col._backend.grade_mcq(card_id=cids[0], chosen="C", took_millis=0)
    assert readiness()["GMAT::Quant"].responses == 12


def test_gmat_record_graded_attempt():
    # The generalised recorder (used by AI grading of typed answers) feeds the
    # same IRT performance model as MCQ grading, via the backend RPC.
    col = getEmptyCol()
    nt = _add_mcq_notetype(col)
    cids = []
    for _ in range(12):
        note = col.new_note(nt)
        note["Question"] = "Q"
        note["Answer"] = "C"
        note.tags = ["GMAT::Quant::Algebra"]
        col.add_note(note, deck_id=1)
        cids.append(note.cards()[0].id)

    # Record attempts via the NEW RPC (not grade_mcq): all correct, within budget.
    for cid in cids:
        col._backend.record_graded_attempt(card_id=cid, correct=True, took_millis=3000)

    by = {
        s.section: s
        for s in col._backend.estimate_readiness(
            search='note:"GMAT MCQ"',
            tag_prefix="GMAT",
            time_budget_secs=120,
            section_minutes=45,
            min_responses=10,
            min_coverage=0.5,
            max_se=1.0,
        )
    }
    assert "GMAT::Quant" in by
    q = by["GMAT::Quant"]
    assert q.responses == 12  # the recorded attempts became IRT responses
    assert q.pct_correct == 1.0
    assert q.theta > 0.0
    assert q.has_score
