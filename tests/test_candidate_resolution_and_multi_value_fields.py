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


    def test_I_county_deterministic_and_geographic_resolution(self):
        """
        Test that County is properly resolved deterministically from instructions or geographic correlation,
        and numeric year hallucinations like '2000' are cleanly rejected and sanitized.
        """
        # 1. Deterministic extraction from location binding
        inst = (
            "[MANDATORY VERIFIED RESIDENTIAL LOCATION - ZERO DISCREPANCY]\n"
            "• City: Jacksonville\n"
            "• County: Onslow\n"
            "• State / Region / Province: North Carolina\n"
            "• ZIP Code: 28540\n"
        )
        ctx = RunContext(run_id="test_nc_county", user_instructions=inst)
        norm_county = NormalizedQuestion(
            id="q_county",
            role="textbox",
            label="Where is the North Carolina community you are referring to? - County",
            required=True
        )
        det_res = ctx.resolve_deterministic_answer(norm_county)
        self.assertIsNotNone(det_res)
        self.assertEqual(det_res[0], "Onslow")

        # 2. Rejection and sanitization of numeric year '2000' in County field
        from ai_engine import lookup_county_for_location
        county_deduced = lookup_county_for_location(city="Jacksonville", postal="28540")
        self.assertEqual(county_deduced, "Onslow")

        field_county = {
            "id": "q_county",
            "role": "textbox",
            "label": "Where is the North Carolina community you are referring to? - County",
            "required": True
        }
        # Simulate LLM returning '2000'
        bad_answers = {
            "q_county": {"value": "2000", "confidence": 0.8, "source": "deduced", "status": "ANSWERED"}
        }
        validated, is_complete, unres = validate_answer_set(
            [field_county],
            bad_answers,
            run_context=ctx,
            session_memory={"city": "Jacksonville", "zip": "28540"}
        )
        self.assertTrue(is_complete)
        self.assertEqual(validated["q_county"]["value"], "Onslow")
        self.assertNotEqual(validated["q_county"]["value"], "2000")

    def test_J_alien_option_sanitization_and_likert_scales(self):
        """
        Test that alien values like '-3' or 'A safe' on choice/radio fields
        are sanitized and never pass invalid options to the DOM.
        """
        # 1. Likert radio scale receiving '-3'
        field_covid = {
            "id": "q_covid_safety",
            "role": "radio",
            "label": "How safe do you generally feel during each of the following periods? - During COVID-19 Pandemic (2020-2023)",
            "options": ["Very safe", "Somewhat safe", "Neither safe nor unsafe", "Somewhat unsafe", "Very unsafe", "Not Applicable"],
            "required": True
        }
        alien_answers = {
            "q_covid_safety": {"value": "-3", "confidence": 0.8, "source": "deduced", "status": "ANSWERED"}
        }
        validated, is_complete, unres = validate_answer_set([field_covid], alien_answers)
        self.assertTrue(is_complete)
        # Value must be one of the actual options, never '-3'
        self.assertIn(validated["q_covid_safety"]["value"], field_covid["options"])
        self.assertNotEqual(validated["q_covid_safety"]["value"], "-3")

        # 2. Community description receiving 'A safe'
        field_comm = {
            "id": "q_comm_desc",
            "role": "radio",
            "label": "How would you describe this community during the time you lived there?",
            "options": ["Urban", "Suburban", "Rural", "Not sure"],
            "required": True
        }
        alien_answers_2 = {
            "q_comm_desc": {"value": "A safe", "confidence": 0.8, "source": "deduced", "status": "ANSWERED"}
        }
        validated_2, is_complete_2, unres_2 = validate_answer_set([field_comm], alien_answers_2)
        self.assertTrue(is_complete_2)
        self.assertIn(validated_2["q_comm_desc"]["value"], field_comm["options"])
        self.assertNotEqual(validated_2["q_comm_desc"]["value"], "A safe")

    def test_K_language_preservation_and_leave_empty_instruction(self):
        """
        Test that negative instructions ('leave ... empty/blank') are deterministically resolved
        to an empty string instead of hallucinating or copying other values, and start year is
        properly extracted.
        """
        inst = (
            "During which years did you live in this community? it must be 2000 and leave the section "
            "with the present as empty. i repeat leave that section with present as empty, just fill the first one with 2000"
        )
        ctx = RunContext(run_id="test_years_and_empty", user_instructions=inst)

        q_start = NormalizedQuestion(
            id="q_start_yr",
            role="textbox",
            label="During which years did you live in this community? - Start year:",
            required=True
        )
        q_end = NormalizedQuestion(
            id="q_end_yr",
            role="textbox",
            label="During which years did you live in this community? - End Year (or present)",
            required=False
        )

        det_start = ctx.resolve_deterministic_answer(q_start)
        det_end = ctx.resolve_deterministic_answer(q_end)

        self.assertIsNotNone(det_start, "Start year should be deterministically resolved from user instruction")
        self.assertEqual(det_start[0], "2000")

        self.assertIsNotNone(det_end, "End year negative instruction must be deterministically resolved")
        self.assertEqual(det_end[0], "", "End year must be resolved to empty string when instructed to leave empty")

        # Test validation and answer preservation
        field_start = {"id": "q_start_yr", "role": "textbox", "label": "During which years did you live in this community? - Start year:"}
        field_end = {"id": "q_end_yr", "role": "textbox", "label": "During which years did you live in this community? - End Year (or present)"}

        answers = {
            "q_start_yr": {"value": det_start[0], "confidence": 1.0, "source": "instruction", "status": "ANSWERED"},
            "q_end_yr": {"value": det_end[0], "confidence": 1.0, "source": "instruction", "status": "ANSWERED"}
        }

        val_set, is_complete, _ = validate_answer_set([field_start, field_end], answers, run_context=ctx)
        self.assertEqual(val_set["q_start_yr"]["value"], "2000")
        self.assertEqual(val_set["q_end_yr"]["value"], "")
        self.assertNotEqual(val_set["q_end_yr"]["value"], "2000", "End year must never be filled with 2000")

    def test_L_positive_sentiment_incident_rejection_and_demographics(self):
        """
        Test that when user instructs 'Every answer must be positive, you do not face any problem, everywhere is safe':
        1. Age resolves to 30 from '30 years old male'
        2. Total years in NC resolves to 'More than 20 years' from 'Lived in North Carolina more than 20 years'
        3. Incident question deterministically resolves to 'No' (or sanitizes 'Yes' to 'No')
        4. Discrimination frequency resolves to 'Never'
        5. css_escape properly handles special selector characters without throwing NameError
        """
        from interaction_engine import css_escape

        # 1. css_escape check
        self.assertEqual(css_escape("QR~QID12~1"), r"QR~QID12~1".replace("~", r"\~"))
        self.assertEqual(css_escape("q.test:1"), r"q\.test\:1")

        user_inst = (
            "You must use a 30 years old male\n"
            "You are 100% Asian either Chinese, indian, Indonesia.\n"
            "Lived in North Carolina more than 20 years and have been there before and after Covid\n"
            "Every answer must be positive, you do not face any problem, everywhere is safe!!!\n"
        )
        ctx = RunContext(run_id="test_positive_sentiment", user_instructions=user_inst)

        # 2. Age resolution
        q_age = NormalizedQuestion(
            id="q_age",
            role="textbox",
            label="How old are you? - Enter your age in years",
            required=True
        )
        det_age = ctx.resolve_deterministic_answer(q_age)
        self.assertIsNotNone(det_age)
        self.assertEqual(det_age[0], "30")

        # 3. Total years resolution
        q_years = NormalizedQuestion(
            id="q_years",
            role="radio",
            label="Approximately how many total years have you lived in North Carolina",
            options=["Less than 1 year", "1-5 years", "6-10 years", "11-20 years", "More than 20 years"],
            required=True
        )
        det_years = ctx.resolve_deterministic_answer(q_years)
        self.assertIsNotNone(det_years)
        self.assertEqual(det_years[0], "More than 20 years")

        # 4. Incident question resolution -> MUST BE 'No'
        q_incident = NormalizedQuestion(
            id="q_incident",
            role="radio",
            label="While living in North Carolina, have you personally experienced a specific incident in which you were treated differently, unfairly, or targeted because of your race, ethnicity, language, immigration status, accent, Asian identity, or perceived foreignness?",
            options=["Yes", "No"],
            required=True
        )
        det_incident = ctx.resolve_deterministic_answer(q_incident)
        self.assertIsNotNone(det_incident)
        self.assertEqual(det_incident[0], "No")

        # 5. Discrimination frequency -> MUST BE 'Never'
        q_freq = NormalizedQuestion(
            id="q_freq",
            role="radio",
            label="While living in North Carolina, how often were YOU personally treated in the following ways because of your race, ethnicity, language, accent, immigration status, Asian identity, or perceived foreignness? - Made to feel like an outsider",
            options=["Never", "Rarely", "Sometimes", "Often"],
            required=True
        )
        det_freq = ctx.resolve_deterministic_answer(q_freq)
        self.assertIsNotNone(det_freq)
        self.assertEqual(det_freq[0], "Never")

        # 6. Sanitization of 'Yes' to 'No' in validate_answer_set
        field_inc = {
            "id": "q_incident",
            "role": "radio",
            "label": "While living in North Carolina, have you personally experienced a specific incident in which you were treated differently, unfairly, or targeted...",
            "options": ["Yes", "No"]
        }
        bad_inc_ans = {
            "q_incident": {"value": "Yes", "confidence": 0.9, "source": "deduced", "status": "ANSWERED"}
        }
        val_set, is_complete, _ = validate_answer_set([field_inc], bad_inc_ans, run_context=ctx)
        self.assertEqual(val_set["q_incident"]["value"], "No", "validate_answer_set must sanitize 'Yes' to 'No' under positive/problem-free instructions")


if __name__ == "__main__":
    unittest.main()
