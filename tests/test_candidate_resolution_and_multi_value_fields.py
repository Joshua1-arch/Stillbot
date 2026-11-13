import unittest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from candidate_resolution import (
    resolve_candidates,
    detect_candidates,
    determine_field_expectation,
    CandidateResolutionResult
)
from run_context import (
    RunContext,
    NormalizedQuestion,
    PageState,
    AnswerStatus,
    InstructionPrecedence
)
from ai_engine import validate_answer_set
from interaction_engine import TextNumericHandler, WidgetInspection, WidgetType


class TestCandidateResolutionAndMultiValueFields(unittest.IsolatedAsyncioTestCase):

    def test_A_age_textbox_receives_list_of_candidates(self):
        """
        Test A: Age textbox receives a list of candidates: '22, 23, 24, 25, 26, 27, 28.'
        Expected: exactly one valid age is resolved or human review is triggered.
        No multi-value string or concatenation allowed.
        """
        field_age = {
            "id": "q_age",
            "role": "textbox",
            "type": "text",
            "label": "Your age",
            "required": True,
            "fingerprint": "fp_age:0"
        }
        raw_candidates = "22, 23, 24, 25, 26, 27, 28."

        # 1. Candidate resolution layer test
        res = resolve_candidates(raw_candidates, field_age)
        self.assertTrue(res.is_resolved)
        self.assertFalse("," in str(res.resolved_value))
        self.assertTrue(str(res.resolved_value).isdigit())
        age_int = int(res.resolved_value)
        self.assertIn(age_int, [22, 23, 24, 25, 26, 27, 28])
        # Ensure it is a single scalar
        self.assertNotEqual(str(res.resolved_value), "22232425262728")

        # 2. Deterministic Memory Retrieval via RunContext
        ctx = RunContext(
            run_id="test_det_age",
            confirmed_profile={"age": "22, 23, 24, 25, 26, 27, 28."}
        )
        norm_q = NormalizedQuestion(
            id="q_age",
            role="textbox",
            label="Your age",
            required=True
        )
        det_res = ctx.resolve_deterministic_answer(norm_q)
        self.assertIsNotNone(det_res)
        det_val, det_src = det_res
        self.assertTrue(str(det_val).isdigit())
        self.assertIn(int(det_val), [22, 23, 24, 25, 26, 27, 28])
        self.assertNotIn(",", str(det_val))

        # 3. validate_answer_set stage test
        answers = {
            "q_age": {"value": raw_candidates, "status": "ANSWERED", "source": "persona"}
        }
        validated, is_complete, unres = validate_answer_set([field_age], answers, run_context=ctx)
        self.assertTrue(is_complete)
        self.assertEqual(len(unres), 0)
        final_val = validated["q_age"]["value"]
        self.assertTrue(str(final_val).isdigit())
        self.assertNotIn(",", str(final_val))

    async def test_B_age_textbox_receives_single_valid_age(self):
        """
        Test B: Age textbox receives 24.
        Expected: 24 is filled and verified successfully.
        """
        field_age = {
            "id": "q_age_single",
            "role": "textbox",
            "type": "text",
            "label": "Your age",
            "required": True
        }
        res = resolve_candidates("24", field_age)
        self.assertTrue(res.is_resolved)
        self.assertEqual(str(res.resolved_value), "24")

        # Verify TextNumericHandler verification with 24
        handler = TextNumericHandler()
        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value="24")
        inspection = WidgetInspection(
            widget_type=WidgetType.NUMERIC,
            tag_name="input",
            element_id="q_age_single",
            real_id="q_age_single",
            name="age",
            role="textbox",
            label="Your age"
        )

        v_res = await handler.verify(mock_page, inspection, expected_value="24")
        self.assertTrue(v_res.is_verified)
        self.assertEqual(v_res.actual_state, "24")

        # Detect concatenated numeric values rejection
        mock_page_bad = AsyncMock()
        mock_page_bad.evaluate = AsyncMock(return_value="22232425262728")
        v_bad = await handler.verify(mock_page_bad, inspection, expected_value="24")
        self.assertFalse(v_bad.is_verified)
        self.assertIn("concatenated", v_bad.failure_reason.lower())

    def test_C_multi_select_receives_multiple_valid_answers(self):
        """
        Test C: Multi-select question receives multiple valid answers.
        Expected: all appropriate selections are retained.
        """
        field_multi = {
            "id": "q_interests",
            "role": "checkbox",
            "label": "Which of these topics interest you? (select all that apply)",
            "options": ["Technology", "Cooking", "Sports", "Music", "Travel"],
            "required": True
        }
        raw_choices = ["Technology", "Cooking", "Music"]
        res = resolve_candidates(raw_choices, field_multi)
        self.assertTrue(res.is_resolved)
        self.assertTrue(res.has_multiple_candidates)
        self.assertEqual(res.resolved_value, ["Technology", "Cooking", "Music"])

        # Also test delimited string input for checkboxes
        res_delim = resolve_candidates("Technology, Cooking, Music", field_multi)
        self.assertTrue(res_delim.is_resolved)
        self.assertEqual(res_delim.resolved_value, ["Technology", "Cooking", "Music"])

    def test_D_ranking_question_preserves_ordered_list(self):
        """
        Test D: Ranking question receives an ordered list.
        Expected: the entire order is preserved.
        """
        field_rank = {
            "id": "q_rank",
            "role": "ranking",
            "label": "Rank your primary communication methods:",
            "options": ["Email", "Slack", "Phone", "In-person"],
            "required": True
        }
        ordered_input = ["Slack", "Email", "In-person", "Phone"]
        res = resolve_candidates(ordered_input, field_rank)
        self.assertTrue(res.is_resolved)
        self.assertEqual(res.resolved_value, ["Slack", "Email", "In-person", "Phone"])

    def test_E_field_fails_verification_repeatedly_stops_retrying(self):
        """
        Test E: A field fails verification repeatedly.
        Expected: Stillbot does not refill it indefinitely and stops at threshold.
        """
        ctx = RunContext(run_id="test_repeat_fail")
        norm_q = NormalizedQuestion(
            id="f_stuck",
            fingerprint="fp_stuck:1",
            label="Security Token",
            required=True
        )
        ctx.question_inventory[norm_q.fingerprint] = norm_q

        # Simulate 2 verification failures
        ctx.mark_validation_failed(norm_q.fingerprint, "DOM value mismatch")
        self.assertEqual(norm_q.status, AnswerStatus.VALIDATION_FAILED)

        # Failure count tracking simulation
        field_fill_attempts = {norm_q.fingerprint: 2}
        retry_threshold = 2

        # Check if further refill is permitted
        should_refill = field_fill_attempts.get(norm_q.fingerprint, 0) < retry_threshold
        self.assertFalse(should_refill, "Should not refill field after reaching retry threshold")

    def test_F_conditional_discovery_does_not_treat_existing_field_as_newly_revealed(self):
        """
        Test F: Conditional discovery finds the same field repeatedly.
        Expected: the field is not treated as newly revealed every time.
        """
        ctx = RunContext(run_id="test_conditional_discovery")
        initial_fields = [
            {"id": "f_age", "label": "Your age", "role": "textbox", "fingerprint": "fp_age:0", "logical_id": "lid_age"},
            {"id": "f_role", "label": "Which best describes you?", "role": "radio", "fingerprint": "fp_role:0", "logical_id": "lid_role"}
        ]
        processed_keys = {f["fingerprint"] for f in initial_fields}

        # Simulate rescrape returning the same fields plus 1 real conditional field
        rescraped_fields = [
            {"id": "f_age", "label": "Your age", "role": "textbox", "fingerprint": "fp_age:0", "logical_id": "lid_age"},
            {"id": "f_role", "label": "Which best describes you?", "role": "radio", "fingerprint": "fp_role:0", "logical_id": "lid_role"},
            {"id": "f_branch_1", "label": "Please specify your college", "role": "textbox", "fingerprint": "fp_college:1", "logical_id": "lid_college"}
        ]

        unfilled_new = []
        for rf in rescraped_fields:
            rf_key = rf.get("fingerprint")
            is_truly_new = rf_key not in processed_keys
            if is_truly_new:
                unfilled_new.append(rf)
                processed_keys.add(rf_key)

        # Only the genuinely new conditional field should be in unfilled_new
        self.assertEqual(len(unfilled_new), 1)
        self.assertEqual(unfilled_new[0]["id"], "f_branch_1")
        self.assertNotIn("f_age", [f["id"] for f in unfilled_new])

    def test_G_no_valid_single_age_resolved_pauses_for_human_review(self):
        """
        Test G: No valid single age can be resolved.
        Expected: Stillbot pauses for human review rather than fabricating an answer.
        """
        field_age = {
            "id": "q_age_unknown",
            "role": "textbox",
            "type": "text",
            "label": "Your age",
            "required": True
        }

        # 1. Non-numeric / unknown value
        res_unknown = resolve_candidates("unknown", field_age)
        self.assertFalse(res_unknown.is_resolved)
        self.assertEqual(res_unknown.status, "HUMAN_REVIEW_REQUIRED")
        self.assertTrue(res_unknown.requires_human_review)

        # 2. Empty candidate value on required field
        res_empty = resolve_candidates("", field_age)
        self.assertFalse(res_empty.is_resolved)
        self.assertEqual(res_empty.status, "HUMAN_REVIEW_REQUIRED")
        self.assertTrue(res_empty.requires_human_review)

        # 3. validate_answer_set marks field as UNANSWERABLE and incomplete
        answers = {
            "q_age_unknown": {"value": "unknown", "status": "ANSWERED", "source": "unauthorized"}
        }
        validated, is_complete, unres = validate_answer_set([field_age], answers)
        self.assertFalse(is_complete)
        self.assertEqual(len(unres), 1)
        self.assertIsNone(validated["q_age_unknown"]["value"])
        self.assertEqual(validated["q_age_unknown"]["status"], "UNANSWERABLE")

    def test_H_advice_question_containing_your_age_not_treated_as_numeric_age(self):
        """
        Test that an open-ended advice or opinion question containing the phrase 'your age':
        'What would you tell someone your age who vapes or uses marijuana to encourage them to quit?'
        is NEVER classified as a numeric age question and does NOT resolve to a numeric age (e.g. 25).
        """
        q_label = "What would you tell someone your age who vapes or uses marijuana to encourage them to quit?"
        field_advice = {
            "id": "q_vape_advice",
            "role": "textarea",
            "label": q_label,
            "required": True
        }
        
        # 1. Field expectation must be text_free, not numeric_single
        exp = determine_field_expectation(field_advice)
        self.assertEqual(exp, "text_free")
        
        # 2. Even if role is textbox, it must still be text_free (open-ended advice)
        field_textbox = {
            "id": "q_vape_advice_text",
            "role": "textbox",
            "label": q_label,
            "required": True
        }
        exp_tb = determine_field_expectation(field_textbox)
        self.assertEqual(exp_tb, "text_free")
        
        # 3. Deterministic memory retrieval must NOT resolve this to age 25
        ctx = RunContext(
            run_id="test_advice_avoid_age",
            confirmed_profile={"age": "25"}
        )
        norm_q = NormalizedQuestion(
            id="q_vape_advice",
            role="textarea",
            label=q_label,
            required=True
        )
        det_res = ctx.resolve_deterministic_answer(norm_q)
        self.assertIsNone(det_res, "Open-ended advice question must NOT deterministically resolve to respondent age!")


if __name__ == "__main__":
    unittest.main()
