// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

mod service;

use std::collections::HashMap;

use fsrs::FSRS;
use fsrs::FSRS5_DEFAULT_DECAY;
use rand::seq::SliceRandom;

use crate::prelude::*;
use crate::search::SortMode;

/// Per-topic accumulator while scanning cards.
#[derive(Default)]
struct TopicAcc {
    total_cards: u32,
    reviewed_cards: u32,
    graded_reviews: u32,
    mastered_cards: u32,
    /// Sum of current retrievability over cards that have an FSRS memory state.
    retr_sum: f32,
    /// Number of cards that contributed to `retr_sum` (i.e. have a memory
    /// state).
    scored_cards: u32,
}

impl Collection {
    /// Read-only: aggregate per-topic memory mastery for the term cards
    /// selected by `input.search`, grouped by the section-level tag under
    /// `input.tag_prefix`. A card is "mastered" when its FSRS
    /// retrievability >= `r_threshold` AND its most recent rated review was
    /// answered within `time_budget_secs` (0 disables the gate).
    ///
    /// The score is **coverage-aware**: `mean_retrievability` is taken over
    /// *all* cards in the section, with not-yet-reviewed cards counted as 0
    /// (equivalent to `mean_recall_of_reviewed × coverage`), so reviewing a few
    /// cards out of many yields a low score, not a misleading ~100%. The range
    /// brackets coverage uncertainty: `retrievability_low` is this
    /// coverage-aware estimate (treating unseen cards as unknown) and
    /// `retrievability_high` is the mean recall of reviewed cards (the
    /// ceiling if the unseen are as well known). The range widens when
    /// coverage is low and collapses to the point estimate at full
    /// coverage.
    ///
    /// Issues no writes, so undo/collection integrity are unaffected.
    pub(crate) fn compute_topic_mastery(
        &mut self,
        input: anki_proto::gmat::TopicMasteryRequest,
    ) -> Result<anki_proto::gmat::TopicMasteryResponse> {
        let cids = self.search_cards(input.search.as_str(), SortMode::NoOrder)?;
        let now = self.timing_today()?.now;

        let mut topics: HashMap<String, TopicAcc> = HashMap::new();

        for cid in cids {
            let Some(card) = self.storage.get_card(cid)? else {
                continue;
            };
            let Some(note) = self.storage.get_note(card.note_id)? else {
                continue;
            };
            let card_topics = topics_for_tags(&note.tags, &input.tag_prefix);
            if card_topics.is_empty() {
                continue;
            }

            // Review history (read-only). Entries are ordered oldest-first.
            let revlog = self.storage.get_revlog_entries_for_card(card.id)?;
            let rated: Vec<&_> = revlog.iter().filter(|e| e.has_rating()).collect();
            let graded = rated.len() as u32;

            // Current retrievability, if the card has an FSRS memory state.
            let retr = if let Some(state) = card.memory_state {
                let last = match card.last_review_time {
                    Some(t) => t,
                    None => self
                        .storage
                        .time_of_last_review(card.id)?
                        .unwrap_or_default(),
                };
                let elapsed = now.elapsed_secs_since(last) as u32;
                let decay = card.decay.unwrap_or(FSRS5_DEFAULT_DECAY);
                Some(FSRS::new(None).unwrap().current_retrievability_seconds(
                    state.into(),
                    elapsed,
                    decay,
                ))
            } else {
                None
            };

            // Time gate: most recent rated review answered within the budget.
            let within_budget = input.time_budget_secs == 0
                || rated
                    .last()
                    .map(|e| e.taken_millis <= input.time_budget_secs.saturating_mul(1000))
                    .unwrap_or(false);

            let mastered = retr.map(|r| r >= input.r_threshold).unwrap_or(false) && within_budget;

            for topic in card_topics {
                let acc = topics.entry(topic).or_default();
                acc.total_cards += 1;
                acc.graded_reviews += graded;
                if graded > 0 {
                    acc.reviewed_cards += 1;
                }
                // Coverage-aware: a card only adds to the recall sum if it has a
                // memory state; the divisor below is total_cards, so not-yet-reviewed
                // cards count as 0 recall.
                if let Some(r) = retr {
                    acc.retr_sum += r;
                    acc.scored_cards += 1;
                }
                if mastered {
                    acc.mastered_cards += 1;
                }
            }
        }

        let mut out: Vec<_> = topics
            .into_iter()
            .map(|(topic, acc)| {
                // Coverage-aware point estimate: unreviewed cards count as 0 recall.
                let coverage_aware = if acc.total_cards > 0 {
                    acc.retr_sum / acc.total_cards as f32
                } else {
                    0.0
                };
                // Optimistic ceiling: assume unreviewed cards are as well known as the
                // reviewed ones. Equals the point estimate at full coverage.
                let reviewed_mean = if acc.scored_cards > 0 {
                    acc.retr_sum / acc.scored_cards as f32
                } else {
                    0.0
                };
                let has_score = acc.graded_reviews >= input.min_reviews
                    && acc.reviewed_cards >= input.min_cards;
                anki_proto::gmat::TopicMastery {
                    topic,
                    total_cards: acc.total_cards,
                    reviewed_cards: acc.reviewed_cards,
                    mastered_cards: acc.mastered_cards,
                    mean_retrievability: coverage_aware,
                    retrievability_low: coverage_aware,
                    retrievability_high: reviewed_mean,
                    has_score,
                }
            })
            .collect();
        out.sort_by(|a, b| a.topic.cmp(&b.topic));

        Ok(anki_proto::gmat::TopicMasteryResponse { topics: out })
    }

    /// Read-only objective grading for an MCQ card: compares `chosen` against
    /// the note's "Answer" field (case-insensitive, trimmed). Recording the
    /// review is left to the caller via the normal scheduler, so this
    /// issues no writes.
    pub(crate) fn grade_mcq_answer(
        &mut self,
        input: anki_proto::gmat::GradeMcqRequest,
    ) -> Result<anki_proto::gmat::GradeMcqResponse> {
        let cid = CardId(input.card_id);
        let card = self.storage.get_card(cid)?.or_not_found(cid)?;
        let note = self
            .storage
            .get_note(card.note_id)?
            .or_not_found(card.note_id)?;
        let nt = self
            .get_notetype(note.notetype_id)?
            .or_not_found(note.notetype_id)?;
        let correct_answer = nt
            .fields
            .iter()
            .position(|f| f.name.eq_ignore_ascii_case("Answer"))
            .and_then(|idx| note.fields().get(idx))
            .map(|s| s.trim().to_string())
            .unwrap_or_default();
        let correct =
            !correct_answer.is_empty() && correct_answer.eq_ignore_ascii_case(input.chosen.trim());
        Ok(anki_proto::gmat::GradeMcqResponse {
            correct,
            correct_answer,
        })
    }

    /// Practice pool ("custom review order"): return a random card matching
    /// `search` whose `custom_data` is not marked done for `cycle`. Read-only —
    /// no scheduling, no FSRS, no revlog. `exhausted` is true when none remain.
    pub(crate) fn next_practice_card_impl(
        &mut self,
        input: anki_proto::gmat::PracticePoolRequest,
    ) -> Result<anki_proto::gmat::NextPracticeCardResponse> {
        let cids = self.search_cards(input.search.as_str(), SortMode::NoOrder)?;
        let mut remaining: Vec<CardId> = Vec::new();
        for cid in cids {
            if let Some(card) = self.storage.get_card(cid)? {
                if practice_done_cycle(&card.custom_data) != Some(input.cycle) {
                    remaining.push(cid);
                }
            }
        }
        remaining.shuffle(&mut rand::rng());
        let picked = remaining.first().copied();
        Ok(anki_proto::gmat::NextPracticeCardResponse {
            card_id: picked.map(|c| c.0).unwrap_or(0),
            exhausted: picked.is_none(),
            remaining: remaining.len() as u32,
        })
    }

    /// Mark a practice card completed for `cycle` by stamping its
    /// `custom_data`. Undo-aware; does not touch scheduling/FSRS or the
    /// revlog.
    pub(crate) fn mark_practice_done_impl(
        &mut self,
        input: anki_proto::gmat::MarkPracticeDoneRequest,
    ) -> Result<anki_proto::collection::OpChanges> {
        let cid = CardId(input.card_id);
        let cycle = input.cycle;
        self.transact(crate::ops::Op::UpdateCard, |col| {
            let orig = col.storage.get_card(cid)?.or_not_found(cid)?;
            let mut card = orig.clone();
            card.custom_data = set_practice_cycle(&card.custom_data, cycle)?;
            col.update_card_inner(&mut card, orig, col.usn()?)
        })
        .map(Into::into)
    }
}

/// The practice cycle a card is marked done for, read from its `custom_data`
/// ("gp" key). `None` if unmarked or unparseable.
fn practice_done_cycle(custom_data: &str) -> Option<u32> {
    if custom_data.is_empty() {
        return None;
    }
    serde_json::from_str::<serde_json::Value>(custom_data)
        .ok()
        .and_then(|v| v.get("gp").and_then(|g| g.as_u64()))
        .map(|n| n as u32)
}

/// Return `custom_data` with the practice cycle marker ("gp") set, preserving
/// any other keys.
fn set_practice_cycle(custom_data: &str, cycle: u32) -> Result<String> {
    let mut obj: serde_json::Map<String, serde_json::Value> = if custom_data.is_empty() {
        serde_json::Map::new()
    } else {
        serde_json::from_str(custom_data).unwrap_or_default()
    };
    obj.insert("gp".to_string(), serde_json::json!(cycle));
    Ok(serde_json::to_string(&serde_json::Value::Object(obj))?)
}

/// Section-level topics for a card's tags: each tag under `prefix`, truncated
/// to one level below it (prefix "GMAT", tag "GMAT::Quant::Algebra" ->
/// "GMAT::Quant"). An empty prefix uses the tag's top-level component.
/// Deduplicated per card.
fn topics_for_tags(tags: &[String], prefix: &str) -> Vec<String> {
    let mut topics = Vec::new();
    for tag in tags {
        let topic = if prefix.is_empty() {
            tag.split("::")
                .next()
                .filter(|s| !s.is_empty())
                .map(str::to_string)
        } else if let Some(rest) = tag.strip_prefix(&format!("{prefix}::")) {
            let seg = rest.split("::").next().unwrap_or("");
            (!seg.is_empty()).then(|| format!("{prefix}::{seg}"))
        } else {
            None
        };
        if let Some(t) = topic {
            if !topics.contains(&t) {
                topics.push(t);
            }
        }
    }
    topics
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::card::CardType;
    use crate::card::FsrsMemoryState;
    use crate::notetype::Notetype;
    use crate::revlog::RevlogEntry;
    use crate::revlog::RevlogId;
    use crate::revlog::RevlogReviewKind;

    /// Adds a note tagged `tag` to `deck`, turns its card into a reviewed FSRS
    /// card (just-reviewed + high stability -> retrievability ~1.0), and
    /// records one rated review of `taken_millis`. Returns the card id.
    fn add_reviewed_card(
        col: &mut Collection,
        deck: DeckId,
        tag: &str,
        taken_millis: u32,
    ) -> CardId {
        let nt = col.basic_notetype();
        let mut note = nt.new_note();
        note.tags = vec![tag.to_string()];
        col.add_note(&mut note, deck).unwrap();
        let cid = col.storage.card_ids_of_notes(&[note.id]).unwrap()[0];

        let mut card = col.storage.get_card(cid).unwrap().unwrap();
        card.ctype = CardType::Review;
        card.interval = 10;
        card.memory_state = Some(FsrsMemoryState {
            stability: 100.0,
            difficulty: 5.0,
        });
        card.decay = Some(FSRS5_DEFAULT_DECAY);
        card.last_review_time = Some(TimestampSecs::now());
        col.storage.update_card(&card).unwrap();

        let rev = RevlogEntry {
            id: RevlogId(TimestampSecs::now().0 * 1000),
            cid,
            button_chosen: 3, // Good
            review_kind: RevlogReviewKind::Review,
            taken_millis,
            ..Default::default()
        };
        col.storage.add_revlog_entry(&rev, false).unwrap();
        cid
    }

    fn request(
        search: &str,
        time_budget_secs: u32,
        min_reviews: u32,
    ) -> anki_proto::gmat::TopicMasteryRequest {
        anki_proto::gmat::TopicMasteryRequest {
            search: search.to_string(),
            tag_prefix: "GMAT".to_string(),
            r_threshold: 0.8,
            time_budget_secs,
            min_reviews,
            min_cards: 1,
        }
    }

    #[test]
    fn fast_recall_is_mastered() -> Result<()> {
        let mut col = Collection::new();
        add_reviewed_card(&mut col, DeckId(1), "GMAT::Quant::Algebra", 2_000);
        let res = col.compute_topic_mastery(request("", 60, 1))?;
        assert_eq!(res.topics.len(), 1);
        let t = &res.topics[0];
        assert_eq!(t.topic, "GMAT::Quant");
        assert_eq!(t.total_cards, 1);
        assert_eq!(t.reviewed_cards, 1);
        assert_eq!(t.mastered_cards, 1);
        assert!(t.has_score);
        assert!(t.mean_retrievability > 0.8);
        Ok(())
    }

    #[test]
    fn slow_recall_is_not_mastered() -> Result<()> {
        let mut col = Collection::new();
        // Correct but took 120s, over the 60s budget.
        add_reviewed_card(&mut col, DeckId(1), "GMAT::Quant::Algebra", 120_000);
        let res = col.compute_topic_mastery(request("", 60, 1))?;
        let t = &res.topics[0];
        assert_eq!(t.reviewed_cards, 1);
        assert_eq!(
            t.mastered_cards, 0,
            "slow recall must not count as mastered"
        );
        Ok(())
    }

    #[test]
    fn topic_below_give_up_threshold_abstains() -> Result<()> {
        let mut col = Collection::new();
        add_reviewed_card(&mut col, DeckId(1), "GMAT::Verbal::CR", 2_000);
        // Require 5 graded reviews; the topic has only 1.
        let res = col.compute_topic_mastery(request("", 60, 5))?;
        let t = &res.topics[0];
        assert_eq!(t.topic, "GMAT::Verbal");
        assert!(!t.has_score, "topic without enough reviews must abstain");
        Ok(())
    }

    #[test]
    fn mcq_cards_are_excluded_from_memory() -> Result<()> {
        let mut col = Collection::new();
        let terms = col.get_or_create_normal_deck("Terms")?.id;
        let practice = col.get_or_create_normal_deck("Practice")?.id;
        // A term card and an MCQ card share the same section tag...
        add_reviewed_card(&mut col, terms, "GMAT::Quant::Algebra", 2_000);
        add_reviewed_card(&mut col, practice, "GMAT::Quant::Geometry", 2_000);
        // ...but the memory query scopes to the Terms deck, excluding MCQ practice.
        let res = col.compute_topic_mastery(request("deck:Terms", 60, 1))?;
        assert_eq!(res.topics.len(), 1);
        assert_eq!(res.topics[0].topic, "GMAT::Quant");
        assert_eq!(res.topics[0].total_cards, 1, "only the term card counts");
        Ok(())
    }

    #[test]
    fn coverage_dilutes_the_score() -> Result<()> {
        let mut col = Collection::new();
        // One reviewed card (retrievability ~1.0) ...
        add_reviewed_card(&mut col, DeckId(1), "GMAT::Quant::Algebra", 2_000);
        // ... and one unreviewed card in the same section (no memory state).
        let nt = col.basic_notetype();
        let mut note = nt.new_note();
        note.tags = vec!["GMAT::Quant::Geometry".to_string()];
        col.add_note(&mut note, DeckId(1)).unwrap();

        let res = col.compute_topic_mastery(request("", 60, 1))?;
        let t = &res.topics[0];
        assert_eq!(t.topic, "GMAT::Quant");
        assert_eq!(t.total_cards, 2);
        assert_eq!(t.reviewed_cards, 1);
        // Coverage-aware: mean over ALL cards (unreviewed = 0) ~ 0.5, not ~1.0.
        assert!(
            t.mean_retrievability > 0.4 && t.mean_retrievability < 0.6,
            "expected ~0.5 from 1 of 2 covered, got {}",
            t.mean_retrievability
        );
        Ok(())
    }

    /// Adds a note with a `Question`/`Answer` note type (the shape of "GMAT
    /// MCQ") whose Answer field holds `answer`. Returns the card id.
    fn add_mcq_card(col: &mut Collection, answer: &str) -> CardId {
        let mut nt = Notetype {
            name: "GMAT MCQ".into(),
            ..Default::default()
        };
        nt.add_field("Question");
        nt.add_field("Answer");
        nt.add_template("Card 1", "{{Question}}", "{{Question}}<hr>{{Answer}}");
        col.add_notetype(&mut nt, true).unwrap();

        let mut note = nt.new_note();
        note.fields_mut()[0] = "What is 2 + 2?".into();
        note.fields_mut()[1] = answer.into();
        col.add_note(&mut note, DeckId(1)).unwrap();
        col.storage.card_ids_of_notes(&[note.id]).unwrap()[0]
    }

    fn grade(
        col: &mut Collection,
        cid: CardId,
        chosen: &str,
    ) -> anki_proto::gmat::GradeMcqResponse {
        col.grade_mcq_answer(anki_proto::gmat::GradeMcqRequest {
            card_id: cid.0,
            chosen: chosen.to_string(),
        })
        .unwrap()
    }

    #[test]
    fn mcq_correct_choice_is_graded_correct() {
        let mut col = Collection::new();
        let cid = add_mcq_card(&mut col, "C");
        // Case-insensitive match against the stored Answer field.
        let res = grade(&mut col, cid, "c");
        assert!(res.correct);
        assert_eq!(res.correct_answer, "C");
    }

    #[test]
    fn mcq_wrong_choice_is_graded_incorrect() {
        let mut col = Collection::new();
        let cid = add_mcq_card(&mut col, "C");
        let res = grade(&mut col, cid, "A");
        assert!(!res.correct);
        // The correct answer is still returned so the UI can reveal it.
        assert_eq!(res.correct_answer, "C");
    }

    #[test]
    fn mcq_without_answer_field_is_not_correct() {
        let mut col = Collection::new();
        // Basic note type has no "Answer" field.
        let nt = col.basic_notetype();
        let mut note = nt.new_note();
        col.add_note(&mut note, DeckId(1)).unwrap();
        let cid = col.storage.card_ids_of_notes(&[note.id]).unwrap()[0];
        let res = grade(&mut col, cid, "A");
        assert!(!res.correct);
        assert_eq!(res.correct_answer, "");
    }

    // --- practice pool ------------------------------------------------------

    fn add_practice_cards(col: &mut Collection, answers: &[&str]) -> Vec<CardId> {
        let mut nt = Notetype {
            name: "GMAT MCQ".into(),
            ..Default::default()
        };
        nt.add_field("Question");
        nt.add_field("Answer");
        nt.add_template("Card 1", "{{Question}}", "{{Question}}<hr>{{Answer}}");
        col.add_notetype(&mut nt, true).unwrap();
        let mut cids = Vec::new();
        for ans in answers {
            let mut note = nt.new_note();
            note.fields_mut()[0] = "Q".into();
            note.fields_mut()[1] = (*ans).into();
            col.add_note(&mut note, DeckId(1)).unwrap();
            cids.push(col.storage.card_ids_of_notes(&[note.id]).unwrap()[0]);
        }
        cids
    }

    fn pool_req(search: &str, cycle: u32) -> anki_proto::gmat::PracticePoolRequest {
        anki_proto::gmat::PracticePoolRequest {
            search: search.to_string(),
            cycle,
        }
    }

    fn mark_done(col: &mut Collection, cid: CardId, cycle: u32) {
        let _ = col
            .mark_practice_done_impl(anki_proto::gmat::MarkPracticeDoneRequest {
                card_id: cid.0,
                cycle,
            })
            .unwrap();
    }

    #[test]
    fn practice_pool_returns_a_card() {
        let mut col = Collection::new();
        let cids = add_practice_cards(&mut col, &["A", "B", "C"]);
        let res = col
            .next_practice_card_impl(pool_req("note:\"GMAT MCQ\"", 1))
            .unwrap();
        assert!(!res.exhausted);
        assert_eq!(res.remaining, 3);
        assert!(cids.iter().any(|c| c.0 == res.card_id));
    }

    #[test]
    fn practice_pool_exhausts_when_all_done() {
        let mut col = Collection::new();
        let cids = add_practice_cards(&mut col, &["A", "B", "C"]);
        for c in &cids {
            mark_done(&mut col, *c, 1);
        }
        let res = col
            .next_practice_card_impl(pool_req("note:\"GMAT MCQ\"", 1))
            .unwrap();
        assert!(res.exhausted, "all cards done this cycle");
        assert_eq!(res.remaining, 0);
    }

    #[test]
    fn practice_pool_resets_next_cycle() {
        let mut col = Collection::new();
        let cids = add_practice_cards(&mut col, &["A", "B", "C"]);
        for c in &cids {
            mark_done(&mut col, *c, 1);
        }
        // Bumping the cycle makes every card "unmarked" again (pool reset).
        let res = col
            .next_practice_card_impl(pool_req("note:\"GMAT MCQ\"", 2))
            .unwrap();
        assert!(!res.exhausted);
        assert_eq!(res.remaining, 3);
    }
}
