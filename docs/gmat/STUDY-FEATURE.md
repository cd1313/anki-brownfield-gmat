# Study feature: peer-to-peer / reciprocal teaching (spec §8)

A short **3-user ablation** of the peer-to-peer / reciprocal-teaching feature. The hypothesis and analysis below are **pre-registered** (fixed before data collection).

## Pre-registration

**Feature.** _Correct the Peer_ (the AI presents a plausible-but-wrong solution, the student critiques it, the AI judges the critique) + AI _peer explanations_ of missed practice MCQs. Learning-science basis: the protégé effect / learning-by-teaching and self-explanation (Roediger & Karpicke 2006; Bisra et al. 2018; Roscoe & Chi 2007).

**Hypothesis.** At **equal study time**, studying with the peer feature ON yields higher accuracy on a **held-out post-test of new questions** than the same MCQ practice with the peer step OFF; both beat plain (passive) Anki review.

**Primary outcome:** post-test accuracy per arm. **Primary contrast:** **peer-ON − peer-OFF** (isolates the feature). Secondary: each arm vs plain Anki.

**Failure criterion (pre-registered):** if peer-ON does not exceed peer-OFF, the feature shows no benefit here — reported honestly. n=3 is a **pilot**: it cannot establish significance; we report descriptive results + each participant's paired difference, not a powered inference.

## Protocol (how the 3-user study is run)

- **Design:** within-subjects. Each participant does **all three arms** on three **matched, disjoint topic sets** (A/B/C, similar difficulty), so each person is their own control. **Counterbalance** which topic set maps to which arm across the 3 participants (a 3×3 Latin square) to cancel topic/order effects.
- **Equal study time:** a fixed timer per arm (recommend **12–15 min**), same for all arms and participants.
- **Arms in the app:** _Peer ON_ = AI on + peer on, study the topic's MCQ practice deck using Correct-the-Peer / peer explanations. _Peer OFF_ = **Tools → GMAT: Toggle Peer Feature** (off), same MCQ practice, no peer step. _Plain Anki_ = review that topic's term flashcards normally (no MCQ practice, no peer).
- **Post-test:** immediately after each arm, the participant answers a fixed set of **new** MCQs on that topic (not seen during study). Record correct / total.
- **Record** each (participant, arm, correct, total) in `data/gmat/study_results.csv` and run `just study-feature` (or this script) to regenerate the Results below.

## Results

Participants: **3**. Per-arm pooled post-test accuracy (95% Wilson CI):

| Arm                 | Accuracy | 95% CI     | n items |
| ------------------- | -------- | ---------- | ------- |
| Peer ON             | 76.7%    | 59.1–88.2% | 30      |
| Peer OFF (ablation) | 70.0%    | 52.1–83.3% | 30      |
| Plain Anki          | 50.0%    | 33.2–66.8% | 30      |

### Primary contrast — peer-ON − peer-OFF (per participant)

| Participant | Peer ON | Peer OFF | Plain | ON − OFF |
| ----------- | ------- | -------- | ----- | -------- |
| p1          | 80%     | 60%      | 50%   | +20 pts  |
| p2          | 80%     | 90%      | 60%   | -10 pts  |
| p3          | 70%     | 60%      | 40%   | +10 pts  |

**Mean paired difference (peer-ON − peer-OFF): +6.7 pts** across 3 participant(s); 2/3 had ON > OFF.

Directional read: **mixed / no clear benefit in this pilot**.

> **n=3 is a pilot.** With three participants there is no statistical power; these are descriptive results and per-participant differences, reported honestly. The hypothesis and analysis were pre-registered above before data collection.
