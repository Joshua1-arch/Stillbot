import unittest
import asyncio
import os
import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
import json
from unittest.mock import AsyncMock, patch, MagicMock

from run_context import (
    PageState, AnswerStatus, NormalizedQuestion,
    compute_question_fingerprint, compute_logical_question_id,
    InstructionPrecedence, RunContext
)
from ai_engine import (
    generate_dynamic_schema, get_form_answers, _get_form_answers_single_chunk,
    find_permitted_alternative, resolve_public_fact_or_research,
    resolve_permitted_estimate, validate_answer_set
)
from scraper import scrape_frame
from playwright.async_api import async_playwright
from main import run_bot

class TestIntelligentResolutionPipeline(unittest.IsolatedAsyncioTestCase):

    def test_01_permitted_alternative_resolution(self):
        """Test that questions with permitted alternatives (Prefer not to say, None of the above) resolve lawfully."""
        field_sensitive = {
            "id": "q_income",
            "role": "radio",
            "label": "Annual Household Income",
            "required": True,
            "options": ["Under $30,000", "$30,000-$60,000", "$60,000-$100,000", "Prefer not to say"]
        }
        alt = find_permitted_alternative(field_sensitive)
        self.assertIsNotNone(alt)
        self.assertEqual(alt[0], "Prefer not to say")

        field_brand = {
            "id": "q_brand",
            "role": "checkbox",
            "label": "Which of these luxury cars do you currently own?",
            "required": True,
            "options": ["Ferrari", "Lamborghini", "McLaren", "None of the above"]
        }
        alt_brand = find_permitted_alternative(field_brand)
        self.assertIsNotNone(alt_brand)
        self.assertEqual(alt_brand[0], "None of the above")

    def test_02_public_fact_and_geographic_research(self):
        """Test that public factual questions resolve via research without inventing personal facts."""
        field_capital = {
            "id": "q_cap",
            "role": "textbox",
            "label": "What is the capital of Illinois?",
            "required": True
        }
        res = resolve_public_fact_or_research(field_capital, {})
        self.assertIsNotNone(res)
        self.assertEqual(res["value"], "Springfield")
        self.assertEqual(res["source"], "research")
        self.assertEqual(res["status"], "ANSWERED")

        # Geographic correlation from confirmed city in session memory
        field_state = {
            "id": "q_state",
            "role": "combobox",
            "label": "State of Residence",
            "required": True,
            "options": ["California", "Illinois", "New York", "Texas"]
        }
        res_state = resolve_public_fact_or_research(field_state, {"City": "Chicago"})
        self.assertIsNotNone(res_state)
        self.assertEqual(res_state["value"], "Illinois")
        self.assertEqual(res_state["source"], "research")

    def test_03_permitted_estimate_resolution(self):
        """Test that questions explicitly permitting an estimate resolve with estimate source."""
        field_approx = {
            "id": "q_hours",
            "role": "select",
            "label": "Approximately how many hours per week do you browse the internet?",
            "required": True,
            "options": ["0-5 hours", "6-15 hours", "16-30 hours", "30+ hours"]
        }
        res = resolve_permitted_estimate(field_approx, {}, persona="")
        self.assertIsNotNone(res)
        self.assertEqual(res["source"], "estimate")
        self.assertIn(res["value"], field_approx["options"])
        self.assertEqual(res["status"], "ANSWERED")

    def test_04_validate_answer_set_rejects_empty_and_whitespace(self):
        """Test validate_answer_set catches null, empty, and whitespace-only answers on required fields."""
        fields = [
            {"id": "q1", "label": "Full Name", "required": True, "role": "textbox"},
            {"id": "q2", "label": "Company", "required": False, "role": "textbox"}
        ]
        answers = {
            "q1": {"value": "   ", "status": "ANSWERED", "source": "persona", "confidence": 1.0},
            "q2": {"value": "", "status": "ANSWERED", "source": "persona", "confidence": 1.0}
        }
        validated, is_complete, unres = validate_answer_set(fields, answers)
        # q1 is required and was whitespace-only -> must NOT be complete
        self.assertFalse(is_complete)
        self.assertEqual(len(unres), 1)
        self.assertEqual(unres[0]["id"], "q1")
        self.assertIsNone(validated["q1"]["value"])
        self.assertEqual(validated["q1"]["status"], "UNANSWERABLE")
        # q2 is optional and was empty -> gracefully marked SKIPPED
        self.assertIsNone(validated["q2"]["value"])
        self.assertEqual(validated["q2"]["status"], "SKIPPED")

    def test_05_validate_answer_set_recovers_omitted_field_with_permitted_alternative(self):
        """Test that if AI omits a required field that has a permitted alternative, it is safely resolved."""
        fields = [
            {
                "id": "q_military",
                "label": "Do you have prior military service?",
                "required": True,
                "role": "radio",
                "options": ["Active Duty", "Veteran", "Reserve", "Prefer not to say"]
            }
        ]
        # AI response omitted q_military completely
        raw_answers = {}
        validated, is_complete, unres = validate_answer_set(fields, raw_answers)
        self.assertTrue(is_complete)
        self.assertEqual(len(unres), 0)
        self.assertEqual(validated["q_military"]["value"], "Prefer not to say")
        self.assertEqual(validated["q_military"]["source"], "permitted_alternative")

    async def test_06_ai_omitted_field_resolved_or_unanswerable(self):
        """Test _get_form_answers_single_chunk ensures every field has an answer object even if omitted by LLM."""
        fields = [
            {"id": "f_supplied", "label": "Age", "role": "textbox", "required": True},
            {"id": "f_omitted_opt", "label": "Optional Feedback", "role": "textbox", "required": False},
            {"id": "f_omitted_req", "label": "Security Clearance Level", "role": "textbox", "required": True}
        ]
        
        # LLM only returns f_supplied
        mock_choice = MagicMock()
        mock_choice.message.content = json.dumps({
            "f_supplied": {"value": "29", "confidence": 1.0, "source": "persona", "status": "ANSWERED"}
        })
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        
        with patch("ai_engine.client.chat.completions.create", new_callable=AsyncMock) as mock_create:
            mock_create.return_value = mock_response
            ctx = RunContext(run_id="test_omission_repair")
            answers = await _get_form_answers_single_chunk(fields, {}, run_context=ctx)
            
            # Every field must have an entry in answers
            self.assertIn("f_supplied", answers)
            self.assertIn("f_omitted_opt", answers)
            self.assertIn("f_omitted_req", answers)
            
            # f_omitted_opt is optional -> SKIPPED
            self.assertEqual(answers["f_omitted_opt"]["status"], "SKIPPED")
            # f_omitted_req is required -> UNANSWERABLE with clear reason
            self.assertEqual(answers["f_omitted_req"]["status"], "UNANSWERABLE")
            self.assertIsNone(answers["f_omitted_req"]["value"])

    async def test_07_run_context_validate_completion(self):
        """Test RunContext.validate_completion identifies unverified required questions."""
        ctx = RunContext(run_id="comp_test")
        q1 = NormalizedQuestion(id="q1", label="Full Name", required=True, status=AnswerStatus.VERIFIED)
        q2 = NormalizedQuestion(id="q2", label="Phone Number", required=True, status=AnswerStatus.UNANSWERED)
        q3 = NormalizedQuestion(id="q3", label="Website", required=False, status=AnswerStatus.UNANSWERED)
        
        ctx.question_inventory[q1.fingerprint] = q1
        ctx.question_inventory[q2.fingerprint] = q2
        ctx.question_inventory[q3.fingerprint] = q3
        
        is_complete, unverified = ctx.validate_completion()
        self.assertFalse(is_complete)
        self.assertEqual(len(unverified), 1)
        self.assertEqual(unverified[0].label, "Phone Number")
        
        # Verify q2
        ctx.mark_verified(q2.fingerprint, "555-0199")
        is_complete_now, unverified_now = ctx.validate_completion()
        self.assertTrue(is_complete_now)
        self.assertEqual(len(unverified_now), 0)

    async def test_08_e2e_form_workflow_with_zero_unresolved_output(self):
        """Test full workflow executes with clean summary containing zero null or empty required values."""
        html_file = os.path.abspath("test_pages/standard.html")
        file_url = f"file:///{html_file.replace(os.sep, '/')}"
        
        instructions = "Full Name is Jane Doe. Age is 29. Desired Role is Frontend Developer. Will you relocate is Yes."
        
        import config
        orig_dry = getattr(config, "DRY_RUN", False)
        config.DRY_RUN = True
        try:
            res = await run_bot(
                target_url=file_url,
                instructions=instructions,
                proxy="direct",
                run_index=102
            )
            self.assertIsInstance(res, dict)
            summary = res.get("summary", {})
            # Summary must not contain None or empty string values
            for k, v in summary.items():
                self.assertIsNotNone(v, f"Field '{k}' must not have None in summary.")
                self.assertTrue(bool(str(v).strip()), f"Field '{k}' must not be empty string.")
            self.assertEqual(res["exit_reason"], "DRY_RUN_COMPLETED")
            self.assertTrue(res.get("dry_run"))
            self.assertIn("submission did not occur", res.get("dry_run_details", "").lower())
        finally:
            config.DRY_RUN = orig_dry

    def test_09_omitted_field_not_replaced_by_prefer_not_to_say(self):
        """Test that a question with supported profile facts is recovered rather than replaced by 'Prefer not to say'."""
        ctx = RunContext(
            run_id="supported_recovery_test",
            confirmed_profile={"gender": "Male", "role": "DevOps Engineer"}
        )
        fields = [
            {
                "id": "f_gender",
                "label": "Gender",
                "role": "radio",
                "options": ["Male", "Female", "Non-binary", "Prefer not to say"],
                "required": True,
                "fingerprint": "fp_gender:1"
            }
        ]
        # AI returns an empty dictionary (omitting the field)
        empty_answers = {}
        validated, is_complete, unresolved = validate_answer_set(fields, empty_answers, run_context=ctx)
        
        self.assertTrue(is_complete)
        self.assertEqual(len(unresolved), 0)
        self.assertIn("f_gender", validated)
        # MUST be 'Male' from confirmed profile, NOT 'Prefer not to say'
        self.assertEqual(validated["f_gender"]["value"], "Male")
        self.assertIn(validated["f_gender"]["source"], ["persona", "instruction"])
        self.assertNotEqual(validated["f_gender"]["value"], "Prefer not to say")

    def test_10_unfamiliar_public_facts_and_math_research(self):
        """Test that arithmetic, world capitals, and calendar facts resolve via public research."""
        fields = [
            {"id": "q_math", "label": "What is 7 + 8?", "role": "textbox", "type": "text"},
            {"id": "q_capital", "label": "Capital city of France", "role": "combobox", "options": ["Berlin", "Madrid", "Paris", "Rome"]},
            {"id": "q_days", "label": "How many days are in a week?", "role": "radio", "options": ["5", "6", "7", "8"]}
        ]
        ans_math = resolve_public_fact_or_research(fields[0])
        self.assertIsNotNone(ans_math)
        self.assertEqual(ans_math["value"], "15")
        self.assertEqual(ans_math["source"], "research")
        
        ans_cap = resolve_public_fact_or_research(fields[1])
        self.assertIsNotNone(ans_cap)
        self.assertEqual(ans_cap["value"], "Paris")
        self.assertEqual(ans_cap["source"], "research")
        
        ans_days = resolve_public_fact_or_research(fields[2])
        self.assertIsNotNone(ans_days)
        self.assertEqual(ans_days["value"], "7")
        self.assertEqual(ans_days["source"], "research")

    def test_11_unsupported_personal_facts_not_invented(self):
        """Test that unknown private personal facts (PIN, maiden name) are not invented and remain UNANSWERABLE."""
        ctx = RunContext(run_id="no_invention_test", confirmed_profile={"first_name": "Alice"})
        fields = [
            {"id": "f_pin", "label": "Enter your ATM PIN", "role": "textbox", "required": True, "fingerprint": "fp_pin:0"},
            {"id": "f_maiden", "label": "Mother's Maiden Name", "role": "textbox", "required": True, "fingerprint": "fp_maiden:0"}
        ]
        validated, is_complete, unresolved = validate_answer_set(fields, {}, run_context=ctx)
        
        self.assertFalse(is_complete)
        self.assertEqual(len(unresolved), 2)
        for fid in ["f_pin", "f_maiden"]:
            self.assertIn(fid, validated)
            self.assertIsNone(validated[fid]["value"])
            self.assertEqual(validated[fid]["status"], "UNANSWERABLE")
            self.assertEqual(validated[fid]["source"], "unauthorized")

    def test_12_failed_dom_verification_prevents_completion(self):
        """Test that unverified required DOM controls prevent completion confirmation."""
        ctx = RunContext(run_id="dom_fail_test")
        q = NormalizedQuestion(
            fingerprint="fp_req:1",
            id="f_req",
            role="textbox",
            label="Target Position",
            required=True
        )
        ctx.question_inventory[q.fingerprint] = q
        
        # Mark validation failed in DOM
        ctx.mark_validation_failed(q.fingerprint, "DOM text mismatch: element value remained empty")
        is_complete, unverified = ctx.validate_completion()
        
        self.assertFalse(is_complete)
        self.assertEqual(len(unverified), 1)
        self.assertEqual(unverified[0].id, "f_req")

    async def test_13_dry_run_reports_dry_run_completed_not_completed(self):
        """Test that DRY_RUN=True reports DRY_RUN_COMPLETED and never claims submission occurred."""
        import config
        orig_dry = getattr(config, "DRY_RUN", False)
        config.DRY_RUN = True
        test_html_path = os.path.abspath("test_pages/standard.html")
        file_url = f"file:///{test_html_path.replace(os.sep, '/')}"
        instructions = "Full Name is Jane Doe. Age is 29. Desired Role is Frontend Developer. Will you relocate is Yes."
        try:
            res = await run_bot(
                target_url=file_url,
                instructions=instructions,
                proxy="direct",
                run_index=103
            )
            self.assertEqual(res["exit_reason"], "DRY_RUN_COMPLETED")
            self.assertTrue(res.get("dry_run"))
            self.assertIn("submission did not occur", res.get("dry_run_details", "").lower())
            self.assertNotEqual(res["exit_reason"], "Completed")
        finally:
            config.DRY_RUN = orig_dry

    def test_14_no_stereotyped_education_inference(self):
        """Test that profile with job role does NOT fabricate degree or stereotype education."""
        ctx = RunContext(
            run_id="no_edu_stereotype",
            confirmed_profile={"role": "Senior executive/director"}
        )
        # 1. Question provides neutral option -> selects 'Prefer not to say'
        field_with_neutral = {
            "id": "q_edu",
            "label": "What is your highest level of education?",
            "role": "radio",
            "options": ["High School", "Bachelor's", "Master's", "Prefer not to say"],
            "required": True,
            "fingerprint": "fp_edu:1"
        }
        # Inferred stereotype from LLM (assuming Director has Master's)
        answers_with_stereotype = {
            "q_edu": {
                "value": "Master's",
                "source": "deduced",
                "status": "ANSWERED",
                "reason": "Deduced from executive job title"
            }
        }
        validated, is_complete, unresolved = validate_answer_set([field_with_neutral], answers_with_stereotype, run_context=ctx)
        self.assertTrue(is_complete)
        self.assertEqual(validated["q_edu"]["value"], "Prefer not to say")
        self.assertEqual(validated["q_edu"]["source"], "permitted_alternative")

        # 2. Question without neutral option -> marks UNANSWERABLE to request human input instead of fabricating
        field_without_neutral = {
            "id": "q_edu_no_neutral",
            "label": "Highest level of education completed",
            "role": "radio",
            "options": ["High School", "Bachelor's", "Master's", "Doctorate"],
            "required": True,
            "fingerprint": "fp_edu_noneutral:1"
        }
        validated2, is_complete2, unresolved2 = validate_answer_set([field_without_neutral], answers_with_stereotype, run_context=ctx)
        self.assertFalse(is_complete2)
        self.assertIsNone(validated2["q_edu_no_neutral"]["value"])
        self.assertEqual(validated2["q_edu_no_neutral"]["status"], "UNANSWERABLE")

    def test_15_prevent_smoking_and_experience_stereotypes(self):
        """Test that smoking frequency and years of experience are not stereotyped from age or role."""
        ctx = RunContext(
            run_id="no_lifestyle_stereotype",
            confirmed_profile={"age": "45", "role": "Senior executive/director"}
        )
        fields = [
            {
                "id": "q_smoke",
                "label": "How often do you smoke cigarettes or use tobacco products?",
                "role": "radio",
                "options": ["Daily", "Occasionally", "Never", "Prefer not to answer"],
                "required": True,
                "fingerprint": "fp_smoke:1"
            },
            {
                "id": "q_years",
                "label": "How many years of experience do you have in your field?",
                "role": "radio",
                "options": ["1-3 years", "4-7 years", "8+ years"],
                "required": True,
                "fingerprint": "fp_years:1"
            }
        ]
        guessed_answers = {
            "q_smoke": {"value": "Daily", "source": "deduced", "status": "ANSWERED"},
            "q_years": {"value": "8+ years", "source": "deduced", "status": "ANSWERED"}
        }
        validated, is_complete, unresolved = validate_answer_set(fields, guessed_answers, run_context=ctx)
        # q_smoke has neutral option -> resolves to 'Prefer not to answer'
        self.assertEqual(validated["q_smoke"]["value"], "Prefer not to answer")
        # q_years has NO neutral option and is required -> UNANSWERABLE, requests human input
        self.assertIsNone(validated["q_years"]["value"])
        self.assertEqual(validated["q_years"]["status"], "UNANSWERABLE")

    async def test_16_radiogroup_aria_labelledby_question_title_extraction(self):
        """Test modern Qualtrics JFE radiogroup extracts authentic question title instead of option 1 text."""
        html_qualtrics = """
        <!DOCTYPE html>
        <html>
        <body>
            <div class="QuestionOuter">
                <div role="radiogroup" aria-labelledby="question-display-QID1">
                    <div id="question-display-QID1" class="question-display rich-text">Which best describes your primary role?</div>
                    <div class="choice">
                        <label for="QR~QID1~1"><input type="radio" id="QR~QID1~1" name="role" value="Lead"> Frontline team leader/supervisor</label>
                    </div>
                    <div class="choice">
                        <label for="QR~QID1~2"><input type="radio" id="QR~QID1~2" name="role" value="Director"> Senior executive/director</label>
                    </div>
                </div>
            </div>
        </body>
        </html>
        """
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.set_content(html_qualtrics)
            fields = await scrape_frame(page.main_frame, 0)
            await browser.close()

            self.assertEqual(len(fields), 1)
            f = fields[0]
            self.assertEqual(f["role"], "radio")
            # CRITICAL ASSERTION: Label must be the question text, NOT option 1!
            self.assertEqual(f["label"], "Which best describes your primary role?")
            self.assertIn("Frontline team leader/supervisor", f["options"])
            self.assertIn("Senior executive/director", f["options"])

if __name__ == "__main__":
    unittest.main()
