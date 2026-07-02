// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use crate::collection::Collection;
use crate::error;

impl crate::services::GmatService for Collection {
    fn get_topic_mastery(
        &mut self,
        input: anki_proto::gmat::TopicMasteryRequest,
    ) -> error::Result<anki_proto::gmat::TopicMasteryResponse> {
        self.compute_topic_mastery(input)
    }

    fn grade_mcq(
        &mut self,
        input: anki_proto::gmat::GradeMcqRequest,
    ) -> error::Result<anki_proto::gmat::GradeMcqResponse> {
        self.grade_mcq_answer(input)
    }

    fn next_practice_card(
        &mut self,
        input: anki_proto::gmat::PracticePoolRequest,
    ) -> error::Result<anki_proto::gmat::NextPracticeCardResponse> {
        self.next_practice_card_impl(input)
    }

    fn mark_practice_done(
        &mut self,
        input: anki_proto::gmat::MarkPracticeDoneRequest,
    ) -> error::Result<anki_proto::collection::OpChanges> {
        self.mark_practice_done_impl(input)
    }

    fn estimate_readiness(
        &mut self,
        input: anki_proto::gmat::ReadinessRequest,
    ) -> error::Result<anki_proto::gmat::ReadinessResponse> {
        self.estimate_readiness_impl(input)
    }
}
