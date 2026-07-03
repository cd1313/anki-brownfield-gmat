// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

mod service;

use std::collections::HashMap;

use fsrs::FSRS;
use fsrs::FSRS5_DEFAULT_DECAY;
use rand::seq::SliceRandom;
use rand::Rng;

use crate::notetype::Notetype;
use crate::ops::Op;
use crate::prelude::*;
use crate::revlog::RevlogEntry;
use crate::revlog::RevlogReviewKind;
use crate::search::SortMode;

/// `button_chosen` value recorded for a correct MCQ attempt (mirrors "Good").
const MCQ_CORRECT_BUTTON: u8 = 3;
/// `button_chosen` value recorded for an incorrect MCQ attempt (mirrors
/// "Again").
const MCQ_INCORRECT_BUTTON: u8 = 1;

/// IRT ability grid (logit scale) for the EAP performance estimator.
const THETA_MIN: f32 = -4.0;
const THETA_MAX: f32 = 4.0;
const THETA_STEP: f32 = 0.1;
/// Logistic scaling constant that makes the 2PL/3PL logistic approximate the
/// normal-ogive IRT model (the conventional value).
const IRT_D: f32 = 1.702;

/// Adaptive practice recommender (see MODELS.md §4). The next card maximises
/// `WEAKNESS_WEIGHT*(-theta) - |b - (theta + DESIRABLE_OFFSET)| + explore`, so
/// weaker sections dominate and, within a section, items near the student's
/// ability are preferred.
const WEAKNESS_WEIGHT: f32 = 3.0;
/// Target difficulty sits slightly above ability ("desirable difficulty").
const DESIRABLE_OFFSET: f32 = 0.5;
/// Empirical-Bayes pseudo-observations pulling item difficulty toward the
/// neutral prior `b0 = 0` (few attempts -> ~0; many -> observed difficulty).
const SHRINK_K: f32 = 4.0;
/// Bonus for unattempted items so new questions still surface (and get the
/// attempts the hybrid difficulty estimate needs). Decays as `1/(1+n)`.
const EXPLORE_BONUS: f32 = 0.5;
/// Tiny random tie-break added to each candidate's score, for variety.
const RECOMMEND_JITTER: f32 = 0.02;

/// Per-topic accumulator while scanning cards.
#[derive(Default)]
struct TopicAcc {
    total_cards: u32,
    reviewed_cards: u32,
    graded_reviews: u32,
    mastered_cards: u32,
    /// Current retrievability of each card that has an FSRS memory state
    /// (i.e. the reviewed/scored cards). Length = scored cards.
    retr_values: Vec<f32>,
}

impl Collection {
    /// Read-only: aggregate per-topic memory mastery for the term cards
    /// selected by `input.search`, grouped by the section-level tag under
    /// `input.tag_prefix`. A card is "mastered" when its FSRS
    /// retrievability >= `r_threshold` AND its most recent rated review was
    /// answered within `time_budget_secs` (0 disables the gate).
    ///
    /// Returns **two** scores per section, each with its own range (the
    /// 10th–90th percentile of per-card retrievability over that score's
    /// card set):
    /// - **practiced** = mean recall over the cards you've reviewed ("what
    ///   you've studied"); range = p10–p90 across reviewed cards.
    /// - **category** = coverage-aware recall over the whole section with
    ///   not-yet-reviewed cards counted as 0 (= `practiced × coverage`), so
    ///   reviewing a few cards out of many yields a low category score, not a
    ///   misleading ~100%; range = p10–p90 over all cards (unreviewed = 0).
    ///
    /// Give-up rule: `has_score` is false (abstain) unless the topic has at
    /// least `min_reviews` graded reviews AND `min_cards` reviewed cards.
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
                // Record per-card retrievability for cards with a memory state.
                // Unreviewed cards (no memory state) contribute nothing here; the
                // category score/range treat them as 0 via total_cards below.
                if let Some(r) = retr {
                    acc.retr_values.push(r);
                }
                if mastered {
                    acc.mastered_cards += 1;
                }
            }
        }

        let mut out: Vec<_> = topics
            .into_iter()
            .map(|(topic, acc)| {
                let scored = acc.retr_values.len();
                let sum: f32 = acc.retr_values.iter().sum();
                // Practiced: mean recall over reviewed cards.
                let practiced_score = if scored > 0 { sum / scored as f32 } else { 0.0 };
                // Category: coverage-aware over the whole section (unreviewed = 0).
                let category_score = if acc.total_cards > 0 {
                    sum / acc.total_cards as f32
                } else {
                    0.0
                };
                // Practiced range: p10–p90 of per-card recall across reviewed cards.
                // Category is a single raw number (coverage-aware mean), no range.
                let practiced_low = percentile(&acc.retr_values, 10.0);
                let practiced_high = percentile(&acc.retr_values, 90.0);

                let has_score = acc.graded_reviews >= input.min_reviews
                    && acc.reviewed_cards >= input.min_cards;
                anki_proto::gmat::TopicMastery {
                    topic,
                    total_cards: acc.total_cards,
                    reviewed_cards: acc.reviewed_cards,
                    mastered_cards: acc.mastered_cards,
                    practiced_score,
                    practiced_low,
                    practiced_high,
                    category_score,
                    has_score,
                }
            })
            .collect();
        out.sort_by(|a, b| a.topic.cmp(&b.topic));

        Ok(anki_proto::gmat::TopicMasteryResponse { topics: out })
    }

    /// Objective grading for an MCQ card: compares `chosen` against the note's
    /// "Answer" field (case-insensitive, trimmed). When `input.took_millis > 0`
    /// the attempt is recorded as a non-scheduling revlog entry (see
    /// [`Collection::record_mcq_attempt`]) so the IRT performance model has
    /// response data; otherwise it issues no writes.
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
        if input.took_millis > 0 {
            let _ = self.record_mcq_attempt(cid, correct, input.took_millis)?;
        }
        Ok(anki_proto::gmat::GradeMcqResponse {
            correct,
            correct_answer,
        })
    }

    /// Record one MCQ attempt as a revlog entry (undo-aware). Correctness is
    /// encoded in `button_chosen` (3 = correct, 1 = incorrect) and latency in
    /// `taken_millis`. The entry is written as a "cramming" entry
    /// (`RevlogReviewKind::Filtered` with `ease_factor == 0`), which
    /// [`RevlogEntry::has_rating_and_affects_scheduling`] treats as
    /// non-scheduling — so it is excluded from FSRS parameter training and the
    /// stats/scheduler, and never affects the memory model. (Practice cards
    /// also live in `GMAT::Practice`, outside the `GMAT::Terms` memory query.)
    /// These entries are the input to the IRT performance model; the reader
    /// identifies them via [`RevlogEntry::is_cramming`].
    fn record_mcq_attempt(
        &mut self,
        cid: CardId,
        correct: bool,
        took_millis: u32,
    ) -> Result<anki_proto::collection::OpChanges> {
        let button_chosen = if correct {
            MCQ_CORRECT_BUTTON
        } else {
            MCQ_INCORRECT_BUTTON
        };
        self.transact(Op::UpdateCard, |col| {
            let entry = RevlogEntry {
                id: TimestampMillis::now().into(),
                cid,
                usn: col.usn()?,
                button_chosen,
                interval: 0,
                last_interval: 0,
                ease_factor: 0, // with Filtered kind => is_cramming() => non-scheduling
                taken_millis: took_millis,
                review_kind: RevlogReviewKind::Filtered,
            };
            col.add_revlog_entry_undoable(entry)?;
            Ok(())
        })
        .map(Into::into)
    }

    /// Record one graded attempt (from any grader, e.g. AI semantic grading of
    /// a typed `GMAT::Terms` answer) as a non-scheduling revlog entry, so
    /// the IRT performance model has response data. Undo-aware. Generalises
    /// the MCQ-only [`Collection::record_mcq_attempt`]; correctness/latency
    /// are encoded the same way (see its docs), so these entries flow into
    /// the same IRT reader via [`RevlogEntry::is_cramming`] and sync as
    /// ordinary revlog rows.
    pub(crate) fn record_graded_attempt_impl(
        &mut self,
        input: anki_proto::gmat::RecordGradedAttemptRequest,
    ) -> Result<anki_proto::collection::OpChanges> {
        self.record_mcq_attempt(CardId(input.card_id), input.correct, input.took_millis)
    }

    /// Practice pool ("custom review order"): recommend the next card matching
    /// `search` whose `custom_data` is not marked done for `cycle`.
    ///
    /// **Adaptive selection (MODELS.md §4):** when `tag_prefix` is set, the
    /// card is chosen to be *weakness-first, at your level* — the section
    /// with the lowest IRT ability θ (`eap_ability`) is prioritised, and
    /// within it items whose difficulty `b` sits near θ (plus a small
    /// desirable-difficulty offset) are preferred. Item difficulty is a
    /// hybrid estimate: derived from each item's own logged attempts and
    /// shrunk toward a neutral prior ([`empirical_difficulty`]), so
    /// unattempted items are neutral and new questions still surface via an
    /// exploration bonus. With no response data every score is flat, so
    /// selection degrades to an unbiased random draw.
    ///
    /// An empty `tag_prefix` disables the IRT weighting (pure random draw).
    /// Read-only — no scheduling, no FSRS, no revlog writes. `exhausted` is
    /// true when no cards remain for the cycle.
    pub(crate) fn next_practice_card_impl(
        &mut self,
        input: anki_proto::gmat::PracticePoolRequest,
    ) -> Result<anki_proto::gmat::NextPracticeCardResponse> {
        let cids = self.search_cards(input.search.as_str(), SortMode::NoOrder)?;

        // No section grouping requested -> preserve the original random draw.
        if input.tag_prefix.is_empty() {
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
            return Ok(anki_proto::gmat::NextPracticeCardResponse {
                card_id: picked.map(|c| c.0).unwrap_or(0),
                exhausted: picked.is_none(),
                remaining: remaining.len() as u32,
            });
        }

        // Pass 1: aggregate per-section responses (for ability θ) and per-item
        // attempt counts, and collect the cards still available this cycle.
        struct Candidate {
            cid: CardId,
            sections: Vec<String>,
            guessing: f32,
            attempts: u32,
            correct: u32,
        }
        let mut section_responses: HashMap<String, Vec<(bool, f32)>> = HashMap::new();
        let mut candidates: Vec<Candidate> = Vec::new();

        for cid in cids {
            let Some(card) = self.storage.get_card(cid)? else {
                continue;
            };
            let Some(note) = self.storage.get_note(card.note_id)? else {
                continue;
            };
            let sections = topics_for_tags(&note.tags, &input.tag_prefix);
            if sections.is_empty() {
                continue;
            }
            let nt = self
                .get_notetype(note.notetype_id)?
                .or_not_found(note.notetype_id)?;
            let c = item_guessing(&note, &nt);

            // MCQ attempts are logged as cramming revlog entries (see
            // record_mcq_attempt); they feed the IRT ability estimate.
            let revlog = self.storage.get_revlog_entries_for_card(cid)?;
            let mut attempts = 0u32;
            let mut correct = 0u32;
            for e in revlog.iter().filter(|e| e.is_cramming() && e.has_rating()) {
                let is_correct = e.button_chosen == MCQ_CORRECT_BUTTON;
                attempts += 1;
                if is_correct {
                    correct += 1;
                }
                for s in &sections {
                    section_responses
                        .entry(s.clone())
                        .or_default()
                        .push((is_correct, c));
                }
            }

            if practice_done_cycle(&card.custom_data) != Some(input.cycle) {
                candidates.push(Candidate {
                    cid,
                    sections,
                    guessing: c,
                    attempts,
                    correct,
                });
            }
        }

        if candidates.is_empty() {
            return Ok(anki_proto::gmat::NextPracticeCardResponse {
                card_id: 0,
                exhausted: true,
                remaining: 0,
            });
        }

        // Per-section ability. Sections with no responses fall back to the prior
        // (θ ≈ 0) via eap_ability, i.e. neutral priority.
        let section_theta: HashMap<String, f32> = section_responses
            .iter()
            .map(|(s, r)| (s.clone(), eap_ability(r).0))
            .collect();

        // Pass 2: score each available card and take the argmax (jitter breaks
        // ties and, on a cold collection where all scores are equal, randomises).
        let remaining = candidates.len() as u32;
        let mut rng = rand::rng();
        let mut best: Option<(f32, CardId)> = None;
        for cand in &candidates {
            // Representative section = the card's weakest tagged section.
            let theta = cand
                .sections
                .iter()
                .map(|s| section_theta.get(s).copied().unwrap_or(0.0))
                .fold(f32::INFINITY, f32::min);
            let theta = if theta.is_finite() { theta } else { 0.0 };
            let score = recommend_score(theta, cand.guessing, cand.attempts, cand.correct)
                + rng.random::<f32>() * RECOMMEND_JITTER;
            if best.map(|(bs, _)| score > bs).unwrap_or(true) {
                best = Some((score, cand.cid));
            }
        }

        let picked = best.map(|(_, cid)| cid);
        Ok(anki_proto::gmat::NextPracticeCardResponse {
            card_id: picked.map(|c| c.0).unwrap_or(0),
            exhausted: picked.is_none(),
            remaining,
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

    /// Read-only per-section readiness from logged MCQ responses.
    ///
    /// **Performance (this phase):** ability `theta` is estimated by EAP under
    /// an IRT model with fixed item parameters — discrimination `a = 1`,
    /// difficulty `b = 0`, guessing `c = 1/#choices` per item (item difficulty
    /// is *assumed*, not calibrated; see the model docs). `theta` is
    /// accuracy-only: response times are read for the pacing factor but never
    /// enter ability. `has_score` abstains until there are enough responses,
    /// enough coverage, and a small enough standard error.
    ///
    /// The pacing and projected-score fields are populated in a later phase.
    pub(crate) fn estimate_readiness_impl(
        &mut self,
        input: anki_proto::gmat::ReadinessRequest,
    ) -> Result<anki_proto::gmat::ReadinessResponse> {
        let cids = self.search_cards(input.search.as_str(), SortMode::NoOrder)?;
        let budget_ms = input.time_budget_secs.saturating_mul(1000);

        let mut sections: HashMap<String, SectionAcc> = HashMap::new();
        for cid in cids {
            let Some(card) = self.storage.get_card(cid)? else {
                continue;
            };
            let Some(note) = self.storage.get_note(card.note_id)? else {
                continue;
            };
            let topics = topics_for_tags(&note.tags, &input.tag_prefix);
            if topics.is_empty() {
                continue;
            }
            let nt = self
                .get_notetype(note.notetype_id)?
                .or_not_found(note.notetype_id)?;
            let c = item_guessing(&note, &nt);

            // MCQ attempts are logged as cramming revlog entries (see
            // record_mcq_attempt).
            let revlog = self.storage.get_revlog_entries_for_card(cid)?;
            let attempts: Vec<&RevlogEntry> = revlog
                .iter()
                .filter(|e| e.is_cramming() && e.has_rating())
                .collect();

            for topic in topics {
                let acc = sections.entry(topic).or_default();
                acc.items_available += 1;
                if !attempts.is_empty() {
                    acc.items_attempted += 1;
                }
                for e in &attempts {
                    let correct = e.button_chosen == MCQ_CORRECT_BUTTON;
                    acc.responses.push((correct, c));
                    if correct {
                        acc.correct += 1;
                    }
                    acc.took_millis.push(e.taken_millis);
                    if e.taken_millis <= budget_ms {
                        acc.within_budget += 1;
                    }
                }
            }
        }

        let mut out: Vec<_> = sections
            .into_iter()
            .map(|(section, acc)| {
                let (theta, theta_se) = eap_ability(&acc.responses);
                let n = acc.responses.len() as u32;
                let coverage = if acc.items_available > 0 {
                    acc.items_attempted as f32 / acc.items_available as f32
                } else {
                    0.0
                };
                let pct_correct = if n > 0 {
                    acc.correct as f32 / n as f32
                } else {
                    0.0
                };
                let within_budget_rate = if n > 0 {
                    acc.within_budget as f32 / n as f32
                } else {
                    0.0
                };
                let has_score = n >= input.min_responses
                    && coverage >= input.min_coverage
                    && theta_se <= input.max_se;

                // Pacing: project full-section time from the median per-item pace.
                let median_secs = median_millis(&acc.took_millis) as f32 / 1000.0;
                let projected_section_minutes = if n > 0 {
                    median_secs * section_questions(&section) as f32 / 60.0
                } else {
                    0.0
                };

                // Readiness score = accuracy (θ→score) minus a pacing penalty,
                // with a range from ability uncertainty. Only when not abstaining.
                let (score, score_low, score_high, confidence) = if has_score {
                    let penalty =
                        pacing_penalty(projected_section_minutes, input.section_minutes as f32);
                    let clamp =
                        |t: f32| (theta_to_section_score(t) - penalty).clamp(SCORE_MIN, SCORE_MAX);
                    (
                        clamp(theta),
                        clamp(theta - theta_se),
                        clamp(theta + theta_se),
                        confidence_label(theta_se, coverage),
                    )
                } else {
                    (0.0, 0.0, 0.0, String::new())
                };

                anki_proto::gmat::SectionReadiness {
                    section,
                    theta,
                    theta_se,
                    responses: n,
                    items_attempted: acc.items_attempted,
                    items_available: acc.items_available,
                    coverage,
                    pct_correct,
                    within_budget_rate,
                    projected_section_minutes,
                    score,
                    score_low,
                    score_high,
                    confidence,
                    has_score,
                }
            })
            .collect();
        out.sort_by(|a, b| a.section.cmp(&b.section));

        Ok(anki_proto::gmat::ReadinessResponse { sections: out })
    }
}

/// Per-section accumulator for readiness estimation.
#[derive(Default)]
struct SectionAcc {
    items_available: u32,
    items_attempted: u32,
    correct: u32,
    within_budget: u32,
    /// (correct, guessing_c) for every logged attempt in the section.
    responses: Vec<(bool, f32)>,
    /// Latency of every logged attempt (ms), for the pacing factor.
    took_millis: Vec<u32>,
}

/// 3PL probability of a correct response at ability `theta` for an item with
/// discrimination `a`, difficulty `b`, guessing `c`.
fn three_pl(theta: f32, a: f32, b: f32, c: f32) -> f32 {
    c + (1.0 - c) / (1.0 + (-IRT_D * a * (theta - b)).exp())
}

/// Guessing parameter `c = 1/(#answer options)`, from the note's non-empty
/// A–E option fields (Quant 5-way → 0.2, Verbal 4-way → 0.25, DI T/F → 0.5).
/// Falls back to 0 (no guessing) when fewer than two options are present.
fn item_guessing(note: &Note, nt: &Notetype) -> f32 {
    let options = ["A", "B", "C", "D", "E"]
        .iter()
        .filter(|name| {
            nt.fields
                .iter()
                .position(|f| f.name.eq_ignore_ascii_case(name))
                .and_then(|idx| note.fields().get(idx))
                .map(|s| !s.trim().is_empty())
                .unwrap_or(false)
        })
        .count();
    if options >= 2 {
        1.0 / options as f32
    } else {
        0.0
    }
}

/// Hybrid item difficulty `b` from an item's own logged attempts. `p_hat`
/// (observed proportion correct) is inverted through the 3PL at the responder's
/// ability `theta` — `b_obs = theta + ln((1-p)/(p-c)) / D` — then shrunk toward
/// the neutral prior `b0 = 0` by [`SHRINK_K`] pseudo-observations. `n = 0`
/// (unattempted) returns 0; `p_hat <= c` (at/below chance) clamps to a hard
/// `b`. Difficulty is *provisional*: approximated at the current section
/// ability, not a full item/person co-calibration (see MODELS.md §4).
fn empirical_difficulty(n: u32, correct: u32, c: f32, theta: f32) -> f32 {
    if n == 0 {
        return 0.0;
    }
    let p_hat = correct as f32 / n as f32;
    // Keep p strictly inside (c, 1) so the log stays finite for all-right /
    // all-wrong / at-chance items.
    let eps = 1e-3;
    let p = p_hat.clamp(c + eps, 1.0 - eps);
    let b_obs = (theta + ((1.0 - p) / (p - c)).ln() / IRT_D).clamp(THETA_MIN, THETA_MAX);
    (n as f32 * b_obs) / (n as f32 + SHRINK_K)
}

/// Recommendation score for one practice item at section ability `theta`.
/// Higher is better; weaker sections dominate, difficulty-fit refines, and
/// unattempted items get an exploration bonus. See MODELS.md §4.
fn recommend_score(theta: f32, c: f32, attempts: u32, correct: u32) -> f32 {
    let b = empirical_difficulty(attempts, correct, c, theta);
    let fit = -(b - (theta + DESIRABLE_OFFSET)).abs();
    let explore = EXPLORE_BONUS / (1.0 + attempts as f32);
    WEAKNESS_WEIGHT * (-theta) + fit + explore
}

/// GMAT Focus Edition section score bounds.
const SCORE_MIN: f32 = 60.0;
const SCORE_MAX: f32 = 90.0;

/// Number of scored questions in a full GMAT Focus section, used to project
/// total section time from the student's per-item pace.
fn section_questions(section: &str) -> u32 {
    if section.ends_with("Verbal") {
        23
    } else if section.ends_with("DataInsights") {
        20
    } else {
        // Quantitative (and default).
        21
    }
}

/// Median of a slice of millisecond latencies (0 when empty).
fn median_millis(values: &[u32]) -> u32 {
    if values.is_empty() {
        return 0;
    }
    let mut v = values.to_vec();
    v.sort_unstable();
    let mid = v.len() / 2;
    if v.len() % 2 == 1 {
        v[mid]
    } else {
        ((v[mid - 1] as u64 + v[mid] as u64) / 2) as u32
    }
}

/// Error function (Abramowitz & Stegun 7.1.26), for the normal CDF.
fn erf(x: f64) -> f64 {
    let t = 1.0 / (1.0 + 0.327_591_1 * x.abs());
    let y = 1.0
        - (((((1.061_405_429 * t - 1.453_152_027) * t) + 1.421_413_741) * t - 0.284_496_736) * t
            + 0.254_829_592)
            * t
            * (-x * x).exp();
    if x < 0.0 {
        -y
    } else {
        y
    }
}

/// Standard normal CDF Φ(x).
fn normal_cdf(x: f32) -> f32 {
    (0.5 * (1.0 + erf(x as f64 / std::f64::consts::SQRT_2))) as f32
}

/// Percentile → GMAT Focus section score (60–90), piecewise-linear over
/// anchor points.
///
/// NOTE: these anchors are an **approximate placeholder** derived from GMAC's
/// published section percentile rankings; they should be replaced with the
/// exact official table (see docs/gmat). The projected score is intentionally
/// shown with a range and confidence and is **not** validated against real
/// exam outcomes.
fn percentile_to_section_score(p: f32) -> f32 {
    const ANCHORS: &[(f32, f32)] = &[
        (0.00, 60.0),
        (0.05, 63.0),
        (0.15, 67.0),
        (0.30, 71.0),
        (0.50, 75.0),
        (0.70, 79.0),
        (0.85, 83.0),
        (0.95, 87.0),
        (1.00, 90.0),
    ];
    let p = p.clamp(0.0, 1.0);
    for w in ANCHORS.windows(2) {
        let (p0, s0) = w[0];
        let (p1, s1) = w[1];
        if p <= p1 {
            let frac = if p1 > p0 { (p - p0) / (p1 - p0) } else { 0.0 };
            return s0 + frac * (s1 - s0);
        }
    }
    SCORE_MAX
}

/// Ability θ (assumed ~N(0,1) in the population) → untimed section score.
fn theta_to_section_score(theta: f32) -> f32 {
    percentile_to_section_score(normal_cdf(theta))
}

/// Pacing penalty in section-score points, growing with projected section time
/// over the limit: 0 when within the limit, up to `MAX_PACING_PENALTY` when far
/// over. Transparent/documented — pacing is a distinct readiness factor from
/// accuracy (BrainLift Subcat 1.3).
fn pacing_penalty(projected_minutes: f32, limit_minutes: f32) -> f32 {
    const MAX_PACING_PENALTY: f32 = 12.0;
    const POINTS_PER_UNIT_OVERAGE: f32 = 20.0;
    if limit_minutes <= 0.0 || projected_minutes <= limit_minutes {
        return 0.0;
    }
    let overage = (projected_minutes - limit_minutes) / limit_minutes;
    (overage * POINTS_PER_UNIT_OVERAGE).min(MAX_PACING_PENALTY)
}

/// Confidence label from ability uncertainty and coverage.
fn confidence_label(se: f32, coverage: f32) -> String {
    if se < 0.4 && coverage >= 0.6 {
        "high".to_string()
    } else if se < 0.7 && coverage >= 0.3 {
        "medium".to_string()
    } else {
        "low".to_string()
    }
}

/// EAP ability estimate over a fixed θ grid with an N(0,1) prior. Each response
/// is `(correct, guessing_c)` for an item with `a = 1`, `b = 0`. Returns
/// `(theta, se)` where `se` is the posterior standard deviation. With no
/// responses this returns the prior (θ ≈ 0, se ≈ 1); it stays finite for
/// all-correct / all-wrong inputs (which is why EAP is used over MLE).
fn eap_ability(responses: &[(bool, f32)]) -> (f32, f32) {
    let mut nodes = Vec::new();
    let mut t = THETA_MIN;
    while t <= THETA_MAX + 1e-6 {
        nodes.push(t);
        t += THETA_STEP;
    }
    // Unnormalised N(0,1) prior at each node.
    let mut post: Vec<f64> = nodes
        .iter()
        .map(|&t| (-0.5 * (t as f64) * (t as f64)).exp())
        .collect();
    for (correct, c) in responses {
        for (i, &node) in nodes.iter().enumerate() {
            let p = three_pl(node, 1.0, 0.0, *c) as f64;
            post[i] *= if *correct { p } else { 1.0 - p };
        }
    }
    let sum: f64 = post.iter().sum();
    if sum <= 0.0 {
        return (0.0, 1.0);
    }
    let mean: f64 = nodes
        .iter()
        .zip(&post)
        .map(|(&n, &w)| n as f64 * w)
        .sum::<f64>()
        / sum;
    let var: f64 = nodes
        .iter()
        .zip(&post)
        .map(|(&n, &w)| {
            let d = n as f64 - mean;
            d * d * w
        })
        .sum::<f64>()
        / sum;
    (mean as f32, var.max(0.0).sqrt() as f32)
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

/// Linear-interpolated percentile `p` (0–100) of `values`; 0.0 for an empty
/// slice. Used for the p10–p90 memory-score ranges.
fn percentile(values: &[f32], p: f32) -> f32 {
    if values.is_empty() {
        return 0.0;
    }
    let mut v = values.to_vec();
    v.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let rank = (p / 100.0) * (v.len() - 1) as f32;
    let lo = rank.floor() as usize;
    let hi = rank.ceil() as usize;
    if lo == hi {
        v[lo]
    } else {
        v[lo] + (v[hi] - v[lo]) * (rank - lo as f32)
    }
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
        // Full coverage (1 of 1): practiced and category agree, both high.
        assert!(t.practiced_score > 0.8);
        assert!(t.category_score > 0.8);
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
        // Category is coverage-aware: mean over ALL cards (unreviewed = 0) ~ 0.5.
        assert!(
            t.category_score > 0.4 && t.category_score < 0.6,
            "category ~0.5 from 1 of 2 covered, got {}",
            t.category_score
        );
        // Practiced is over reviewed cards only ~ 1.0.
        assert!(
            t.practiced_score > 0.8,
            "practiced ~1.0 over the one reviewed card, got {}",
            t.practiced_score
        );
        Ok(())
    }

    #[test]
    fn split_separates_practiced_from_category() -> Result<()> {
        let mut col = Collection::new();
        // One well-known reviewed card ...
        add_reviewed_card(&mut col, DeckId(1), "GMAT::Quant::Algebra", 2_000);
        // ... plus four unreviewed cards in the same section => low coverage.
        let nt = col.basic_notetype();
        for _ in 0..4 {
            let mut note = nt.new_note();
            note.tags = vec!["GMAT::Quant::Geometry".to_string()];
            col.add_note(&mut note, DeckId(1)).unwrap();
        }
        let res = col.compute_topic_mastery(request("", 60, 1))?;
        let t = &res.topics[0];
        assert_eq!(t.reviewed_cards, 1);
        // Practiced stays high (what you've studied); category is diluted by the
        // four unseen cards (~0.2). The two scores are reported separately, and
        // the section is still shown (give-up is reviews+cards only, no range gate).
        assert!(
            t.practiced_score > 0.8,
            "practiced high, got {}",
            t.practiced_score
        );
        assert!(
            t.category_score < 0.3,
            "category diluted, got {}",
            t.category_score
        );
        assert!(t.practiced_score > t.category_score);
        assert!(t.has_score);
        Ok(())
    }

    #[test]
    fn percentile_interpolates() {
        assert_eq!(percentile(&[], 50.0), 0.0);
        assert_eq!(percentile(&[0.4], 10.0), 0.4);
        let v = [0.0, 0.25, 0.5, 0.75, 1.0];
        assert!((percentile(&v, 50.0) - 0.5).abs() < 1e-6);
        assert!((percentile(&v, 10.0) - 0.1).abs() < 1e-6);
        assert!((percentile(&v, 90.0) - 0.9).abs() < 1e-6);
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
            took_millis: 0,
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

    fn grade_timed(
        col: &mut Collection,
        cid: CardId,
        chosen: &str,
        took_millis: u32,
    ) -> anki_proto::gmat::GradeMcqResponse {
        col.grade_mcq_answer(anki_proto::gmat::GradeMcqRequest {
            card_id: cid.0,
            chosen: chosen.to_string(),
            took_millis,
        })
        .unwrap()
    }

    #[test]
    fn mcq_attempt_is_logged_to_revlog() {
        let mut col = Collection::new();
        let cid = add_mcq_card(&mut col, "C");
        let _ = grade_timed(&mut col, cid, "C", 4_200); // correct, 4.2s
        let _ = grade_timed(&mut col, cid, "A", 9_000); // incorrect, 9s

        let revlog = col.storage.get_revlog_entries_for_card(cid).unwrap();
        assert_eq!(revlog.len(), 2, "both attempts logged");
        // Recorded as non-scheduling ("cramming") entries so FSRS/memory are untouched.
        assert!(revlog
            .iter()
            .all(|e| e.is_cramming() && !e.has_rating_and_affects_scheduling()));
        let correct = revlog
            .iter()
            .find(|e| e.button_chosen == MCQ_CORRECT_BUTTON)
            .unwrap();
        assert_eq!(correct.taken_millis, 4_200);
        let wrong = revlog
            .iter()
            .find(|e| e.button_chosen == MCQ_INCORRECT_BUTTON)
            .unwrap();
        assert_eq!(wrong.taken_millis, 9_000);

        // took_millis == 0 must not record (e.g. previews).
        let _ = grade_timed(&mut col, cid, "C", 0);
        assert_eq!(
            col.storage.get_revlog_entries_for_card(cid).unwrap().len(),
            2,
            "took_millis=0 does not log"
        );
    }

    fn add_basic_card(col: &mut Collection) -> CardId {
        let nt = col.basic_notetype();
        let mut note = nt.new_note();
        col.add_note(&mut note, DeckId(1)).unwrap();
        col.storage.card_ids_of_notes(&[note.id]).unwrap()[0]
    }

    fn record_graded(col: &mut Collection, cid: CardId, correct: bool, took_millis: u32) {
        let _ = col
            .record_graded_attempt_impl(anki_proto::gmat::RecordGradedAttemptRequest {
                card_id: cid.0,
                correct,
                took_millis,
            })
            .unwrap();
    }

    #[test]
    fn graded_attempt_correct_logs_cramming_revlog() {
        // A generic graded attempt (e.g. an AI-graded typed term answer) records
        // a non-scheduling revlog entry the IRT reader can consume.
        let mut col = Collection::new();
        let cid = add_basic_card(&mut col);
        record_graded(&mut col, cid, true, 3_000);

        let revlog = col.storage.get_revlog_entries_for_card(cid).unwrap();
        assert_eq!(revlog.len(), 1);
        let e = &revlog[0];
        assert_eq!(e.button_chosen, MCQ_CORRECT_BUTTON);
        assert_eq!(e.taken_millis, 3_000);
        // Non-scheduling ("cramming") so FSRS/memory are untouched, yet it still
        // carries a rating the IRT performance reader picks up.
        assert!(e.is_cramming());
        assert!(e.has_rating());
        assert!(!e.has_rating_and_affects_scheduling());
    }

    #[test]
    fn graded_attempt_incorrect_uses_incorrect_button() {
        let mut col = Collection::new();
        let cid = add_basic_card(&mut col);
        record_graded(&mut col, cid, false, 5_000);

        let revlog = col.storage.get_revlog_entries_for_card(cid).unwrap();
        assert_eq!(revlog.len(), 1);
        assert_eq!(revlog[0].button_chosen, MCQ_INCORRECT_BUTTON);
        assert_eq!(revlog[0].taken_millis, 5_000);
        assert!(revlog[0].is_cramming());
    }

    #[test]
    fn graded_attempt_is_undoable() {
        let mut col = Collection::new();
        let cid = add_basic_card(&mut col);
        record_graded(&mut col, cid, true, 3_000);
        assert_eq!(
            col.storage.get_revlog_entries_for_card(cid).unwrap().len(),
            1
        );

        col.undo().unwrap();
        assert_eq!(
            col.storage.get_revlog_entries_for_card(cid).unwrap().len(),
            0,
            "undo removes the graded attempt"
        );
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
            // Empty prefix -> the original random draw (no IRT weighting).
            tag_prefix: String::new(),
        }
    }

    /// Like `pool_req` but enables the IRT-weighted recommender under "GMAT".
    fn pool_req_irt(search: &str, cycle: u32) -> anki_proto::gmat::PracticePoolRequest {
        anki_proto::gmat::PracticePoolRequest {
            search: search.to_string(),
            cycle,
            tag_prefix: "GMAT".into(),
        }
    }

    /// Creates the shared "GMAT MCQ" note type once (Answer "C" = correct).
    fn mcq_notetype(col: &mut Collection) -> Notetype {
        let mut nt = Notetype {
            name: "GMAT MCQ".into(),
            ..Default::default()
        };
        nt.add_field("Question");
        nt.add_field("Answer");
        nt.add_template("Card 1", "{{Question}}", "{{Question}}<hr>{{Answer}}");
        col.add_notetype(&mut nt, true).unwrap();
        nt
    }

    /// Adds `n` practice cards (Answer "C") tagged with `tag`, reusing `nt`.
    fn add_practice_in_section(
        col: &mut Collection,
        nt: &Notetype,
        tag: &str,
        n: usize,
    ) -> Vec<CardId> {
        let mut cids = Vec::new();
        for _ in 0..n {
            let mut note = nt.new_note();
            note.fields_mut()[0] = "Q".into();
            note.fields_mut()[1] = "C".into();
            note.tags = vec![tag.to_string()];
            col.add_note(&mut note, DeckId(1)).unwrap();
            cids.push(col.storage.card_ids_of_notes(&[note.id]).unwrap()[0]);
        }
        cids
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

    // --- IRT performance (ability) ------------------------------------------

    /// Adds `n` tagged "GMAT MCQ" practice cards (answer "C") in one section.
    fn add_tagged_practice(col: &mut Collection, tag: &str, n: usize) -> Vec<CardId> {
        let mut nt = Notetype {
            name: "GMAT MCQ".into(),
            ..Default::default()
        };
        nt.add_field("Question");
        nt.add_field("Answer");
        nt.add_template("Card 1", "{{Question}}", "{{Question}}<hr>{{Answer}}");
        col.add_notetype(&mut nt, true).unwrap();
        let mut cids = Vec::new();
        for _ in 0..n {
            let mut note = nt.new_note();
            note.fields_mut()[0] = "Q".into();
            note.fields_mut()[1] = "C".into();
            note.tags = vec![tag.to_string()];
            col.add_note(&mut note, DeckId(1)).unwrap();
            cids.push(col.storage.card_ids_of_notes(&[note.id]).unwrap()[0]);
        }
        cids
    }

    fn readiness_req(
        min_responses: u32,
        min_coverage: f32,
        max_se: f32,
    ) -> anki_proto::gmat::ReadinessRequest {
        anki_proto::gmat::ReadinessRequest {
            search: "note:\"GMAT MCQ\"".into(),
            tag_prefix: "GMAT".into(),
            time_budget_secs: 120,
            section_minutes: 45,
            min_responses,
            min_coverage,
            max_se,
        }
    }

    #[test]
    fn more_correct_yields_higher_theta() {
        let mut hi = Collection::new();
        for c in add_tagged_practice(&mut hi, "GMAT::Quant::Algebra", 10) {
            let _ = grade_timed(&mut hi, c, "C", 3_000); // correct
        }
        let hi_theta = hi
            .estimate_readiness_impl(readiness_req(1, 0.0, 10.0))
            .unwrap()
            .sections[0]
            .theta;

        let mut lo = Collection::new();
        for c in add_tagged_practice(&mut lo, "GMAT::Quant::Algebra", 10) {
            let _ = grade_timed(&mut lo, c, "A", 3_000); // wrong
        }
        let lo_theta = lo
            .estimate_readiness_impl(readiness_req(1, 0.0, 10.0))
            .unwrap()
            .sections[0]
            .theta;

        assert!(
            hi_theta > lo_theta,
            "all-correct θ {hi_theta} should exceed all-wrong θ {lo_theta}"
        );
        assert!(hi_theta > 0.0 && lo_theta < 0.0);
    }

    #[test]
    fn few_responses_abstains_on_wide_se() {
        let mut col = Collection::new();
        for c in add_tagged_practice(&mut col, "GMAT::Quant::Algebra", 2) {
            let _ = grade_timed(&mut col, c, "C", 3_000);
        }
        // min_responses/coverage satisfied, so the SE gate is what abstains.
        let s = col
            .estimate_readiness_impl(readiness_req(1, 0.0, 0.5))
            .unwrap();
        let s = &s.sections[0];
        assert!(
            s.theta_se.is_finite() && s.theta_se > 0.5,
            "few items => wide (finite) SE, got {}",
            s.theta_se
        );
        assert!(!s.has_score, "wide SE must abstain");
    }

    #[test]
    fn eap_recovers_known_theta() {
        // Responses generated at the true probability for θ*=1.0, c=0.
        let theta_true = 1.0_f32;
        let p = three_pl(theta_true, 1.0, 0.0, 0.0);
        let n = 400usize;
        let n_correct = (p * n as f32).round() as usize;
        let resp: Vec<(bool, f32)> = (0..n).map(|i| (i < n_correct, 0.0)).collect();
        let (theta, se) = eap_ability(&resp);
        assert!(
            (theta - theta_true).abs() < 0.15,
            "recovered θ {theta}, expected ~{theta_true}"
        );
        assert!(se < 0.2, "many responses => small SE, got {se}");
    }

    #[test]
    fn latency_does_not_change_theta() {
        let mut fast = Collection::new();
        for c in add_tagged_practice(&mut fast, "GMAT::Quant::Algebra", 6) {
            let _ = grade_timed(&mut fast, c, "C", 2_000); // fast + correct
        }
        let fast_theta = fast
            .estimate_readiness_impl(readiness_req(1, 0.0, 10.0))
            .unwrap()
            .sections[0]
            .theta;

        let mut slow = Collection::new();
        for c in add_tagged_practice(&mut slow, "GMAT::Quant::Algebra", 6) {
            let _ = grade_timed(&mut slow, c, "C", 90_000); // slow + correct
        }
        let slow_theta = slow
            .estimate_readiness_impl(readiness_req(1, 0.0, 10.0))
            .unwrap()
            .sections[0]
            .theta;

        assert!(
            (fast_theta - slow_theta).abs() < 1e-4,
            "θ must ignore latency: {fast_theta} vs {slow_theta}"
        );
    }

    // --- Readiness (pacing + score) -----------------------------------------

    #[test]
    fn readiness_scores_a_section_with_range() {
        let mut col = Collection::new();
        for c in add_tagged_practice(&mut col, "GMAT::Quant::Algebra", 30) {
            let _ = grade_timed(&mut col, c, "C", 3_000); // correct, well
                                                          // within budget
        }
        let res = col
            .estimate_readiness_impl(readiness_req(10, 0.5, 1.0))
            .unwrap();
        let s = &res.sections[0];
        assert!(s.has_score, "enough responses/coverage/precision to score");
        assert!(s.score >= SCORE_MIN && s.score <= SCORE_MAX);
        assert!(
            s.score_low <= s.score && s.score <= s.score_high,
            "range must bracket the point estimate"
        );
        assert!(
            (s.within_budget_rate - 1.0).abs() < 1e-6,
            "all answered fast"
        );
        assert!(!s.confidence.is_empty());
    }

    #[test]
    fn slow_pace_lowers_the_score() {
        // Same accuracy (all correct); only the pace differs.
        let mut fast = Collection::new();
        for c in add_tagged_practice(&mut fast, "GMAT::Quant::Algebra", 30) {
            let _ = grade_timed(&mut fast, c, "C", 3_000); // ~fast
        }
        let fast_s = fast
            .estimate_readiness_impl(readiness_req(10, 0.5, 1.0))
            .unwrap()
            .sections[0]
            .score;

        let mut slow = Collection::new();
        for c in add_tagged_practice(&mut slow, "GMAT::Quant::Algebra", 30) {
            let _ = grade_timed(&mut slow, c, "C", 180_000); // 3 min/item ->
                                                             // over the 45-min
                                                             // limit
        }
        let slow_section = slow
            .estimate_readiness_impl(readiness_req(10, 0.5, 1.0))
            .unwrap();
        let slow_s = &slow_section.sections[0];

        assert!(
            slow_s.projected_section_minutes > 45.0,
            "3 min/item over 21 questions should exceed the section limit"
        );
        assert!(
            slow_s.score < fast_s,
            "slower pace must lower the score ({} vs {})",
            slow_s.score,
            fast_s
        );
    }

    #[test]
    fn abstaining_section_has_no_score() {
        let mut col = Collection::new();
        // Only 2 responses -> below min_responses -> abstain.
        for c in add_tagged_practice(&mut col, "GMAT::Quant::Algebra", 2) {
            let _ = grade_timed(&mut col, c, "C", 3_000);
        }
        let res = col
            .estimate_readiness_impl(readiness_req(10, 0.5, 1.0))
            .unwrap();
        let s = &res.sections[0];
        assert!(!s.has_score);
        assert_eq!(s.score, 0.0, "no score number when abstaining");
        assert!(s.confidence.is_empty());
    }

    // --- adaptive recommendation (IRT-weighted selection) -------------------

    #[test]
    fn empirical_difficulty_shrinks_to_prior() {
        // Unattempted item -> neutral prior (b0 = 0).
        assert_eq!(empirical_difficulty(0, 0, 0.0, 0.5), 0.0);
        // All-wrong is harder than all-right; signs straddle the prior.
        let hard = empirical_difficulty(8, 0, 0.0, 0.0);
        let easy = empirical_difficulty(8, 8, 0.0, 0.0);
        assert!(
            hard > easy,
            "all-wrong harder than all-right: {hard} vs {easy}"
        );
        assert!(hard > 0.0 && easy < 0.0);
        // Shrinkage: more attempts pull `b` further from the prior.
        let few = empirical_difficulty(1, 0, 0.0, 0.0);
        let many = empirical_difficulty(50, 0, 0.0, 0.0);
        assert!(
            many > few && few.abs() < many.abs(),
            "few {few} < many {many}"
        );
    }

    #[test]
    fn recommend_score_prefers_items_near_ability() {
        // Target difficulty = θ + DESIRABLE_OFFSET. Among equally-attempted items,
        // the one near the student's level should score highest.
        let theta = 0.0;
        let easy = recommend_score(theta, 0.0, 6, 6); // all right -> too easy
        let on_level = recommend_score(theta, 0.0, 6, 3); // ~half -> b ≈ θ
        let hard = recommend_score(theta, 0.0, 6, 0); // all wrong -> too hard
        assert!(
            on_level > easy && on_level > hard,
            "on-level highest: on {on_level} easy {easy} hard {hard}"
        );
    }

    #[test]
    fn recommend_score_does_not_starve_unseen_items() {
        // A fresh item (exploration bonus) should outrank a well-attempted item
        // that is a poor difficulty fit, so new questions still get surfaced.
        let theta = 0.0;
        let fresh = recommend_score(theta, 0.0, 0, 0);
        let poor_fit = recommend_score(theta, 0.0, 20, 0); // very hard, far from θ+δ
        assert!(
            fresh > poor_fit,
            "unseen {fresh} should beat poor fit {poor_fit}"
        );
    }

    #[test]
    fn recommends_weakest_section_first() {
        let mut col = Collection::new();
        let nt = mcq_notetype(&mut col);
        let quant = add_practice_in_section(&mut col, &nt, "GMAT::Quant::Algebra", 3);
        let verbal = add_practice_in_section(&mut col, &nt, "GMAT::Verbal::CR", 3);
        // Quant answered wrong (low θ); Verbal answered right (high θ).
        for c in &quant {
            let _ = grade_timed(&mut col, *c, "A", 3_000);
        }
        for c in &verbal {
            let _ = grade_timed(&mut col, *c, "C", 3_000);
        }
        let res = col
            .next_practice_card_impl(pool_req_irt("note:\"GMAT MCQ\"", 1))
            .unwrap();
        assert!(!res.exhausted);
        assert!(
            quant.iter().any(|c| c.0 == res.card_id),
            "weakest section (Quant) should be recommended, got {}",
            res.card_id
        );
    }

    #[test]
    fn recommender_with_no_data_returns_a_valid_card() {
        let mut col = Collection::new();
        let nt = mcq_notetype(&mut col);
        let cids = add_practice_in_section(&mut col, &nt, "GMAT::Quant::Algebra", 3);
        // No attempts anywhere: scores are flat -> unbiased fallback, still valid.
        let res = col
            .next_practice_card_impl(pool_req_irt("note:\"GMAT MCQ\"", 1))
            .unwrap();
        assert!(!res.exhausted);
        assert_eq!(res.remaining, 3);
        assert!(cids.iter().any(|c| c.0 == res.card_id));
    }

    #[test]
    fn recommender_excludes_cycle_done_cards() {
        let mut col = Collection::new();
        let nt = mcq_notetype(&mut col);
        let cids = add_practice_in_section(&mut col, &nt, "GMAT::Quant::Algebra", 3);
        mark_done(&mut col, cids[0], 1);
        mark_done(&mut col, cids[1], 1);
        let res = col
            .next_practice_card_impl(pool_req_irt("note:\"GMAT MCQ\"", 1))
            .unwrap();
        assert!(!res.exhausted);
        assert_eq!(res.remaining, 1);
        assert_eq!(
            res.card_id, cids[2].0,
            "only the un-done card can be served"
        );
    }
}
