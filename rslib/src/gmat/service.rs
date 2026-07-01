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
}
