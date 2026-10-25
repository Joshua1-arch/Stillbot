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
    compute_options_signature, InstructionPrecedence, RunContext
)
from scraper import scrape_frame, scrape_page
from ai_engine import generate_dynamic_schema, get_form_answers, _get_form_answers_single_chunk
from main import verify_control_state, fill_field, wait_for_human_copilot, run_bot, VerificationResult
from playwright.async_api import async_playwright

class TestPhase3ControlledStagingValidation(unittest.IsolatedAsyncioTestCase):
    
    async def asyncSetUp(self):
        self.p = await async_playwright().start()
        self.browser = await self.p.chromium.launch(headless=True)
        self.page = await self.browser.new_page()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.p.stop()

    async def test_01_e2e_local_standard_form_workflow(self):
        """Test end-to-end local synthetic form workflow (extraction -> fill -> live verification -> audit)."""
        html_file = os.path.abspath("test_pages/standard.html")
        file_url = f"file:///{html_file.replace(os.sep, '/')}"
        
        status_updates = []
        async def mock_status_callback(msg: str):
            status_updates.append(msg)

        instructions = "Full Name is Jane Doe. Age is 29. Desired Role is Frontend Developer. Will you relocate is Yes."
        
        # Run bot on local synthetic form with DRY_RUN enabled (prevents accidental submission)
        import config
        original_dry_run = getattr(config, "DRY_RUN", False)
        config.DRY_RUN = True
        try:
            res = await run_bot(
                target_url=file_url,
                instructions=instructions,
                proxy="direct",
                run_index=101,
                status_callback=mock_status_callback
            )
            self.assertIsInstance(res, dict)
            self.assertIn("summary", res)
            self.assertIn("metrics", res)
            # Verify status callback received run start
            self.assertTrue(any("Run 101 Started" in s for s in status_updates))
        finally:
            config.DRY_RUN = original_dry_run

    async def test_02_ai_malformed_json_fallback(self):
        """Test AI response containing invalid or malformed JSON handles gracefully without uncaught exceptions."""
        fields = [
            {"id": "q_test", "role": "textbox", "label": "Test Field", "type": "text"}
        ]
        
        mock_choice = MagicMock()
        mock_choice.message.content = "INVALID_NON_JSON_CONTENT{{{{"
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        
        with patch("ai_engine.client.chat.completions.create", new_callable=AsyncMock) as mock_create:
            mock_create.return_value = mock_response
            ctx = RunContext(run_id="malformed_json_test")
            answers = await _get_form_answers_single_chunk(fields, {}, run_context=ctx)
            # Must return fallback dictionary without crashing
            self.assertIsInstance(answers, dict)
            # Token metrics must record attempt
            self.assertGreaterEqual(ctx.token_metrics["ai_calls"], 1)

    async def test_03_ai_missing_fields_unresolved(self):
        """Test that if AI response omits a field, it remains unresolved and is not silently filled."""
        fields = [
            {"id": "f_present", "role": "textbox", "label": "Name", "type": "text"},
            {"id": "f_missing", "role": "textbox", "label": "Secret Code", "type": "text", "required": True}
        ]
        
        # Valid JSON but only contains f_present
        mock_choice = MagicMock()
        mock_choice.message.content = json.dumps({
            "f_present": {
                "value": "Jane",
                "confidence": 1.0,
                "source": "persona",
                "status": "ANSWERED",
                "reason": "Provided in instructions"
            }
        })
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        
        with patch("ai_engine.client.chat.completions.create", new_callable=AsyncMock) as mock_create:
            mock_create.return_value = mock_response
            ctx = RunContext(run_id="missing_field_test")
            answers = await _get_form_answers_single_chunk(fields, {}, run_context=ctx)
            
            # f_present is answered
            self.assertIn("f_present", answers)
            self.assertEqual(answers["f_present"]["value"], "Jane")
            
            # f_missing was omitted by AI: must not be falsely fabricated
            if "f_missing" in answers:
                self.assertIn(answers["f_missing"].get("status"), ["unresolved", "UNANSWERABLE", "NEEDS_CLARIFICATION", None])
            else:
                self.assertNotIn("f_missing", answers)

    async def test_04_copilot_bounded_timeout_and_callback(self):
        """Test wait_for_human_copilot stops cleanly when timeout expires and triggers callback."""
        callback_alerts = []
        async def mock_callback(msg: str):
            callback_alerts.append(msg)

        html_content = "<html><body><h1>Stuck Page</h1></body></html>"
        await self.page.set_content(html_content)
        
        # Run copilot with 1-second timeout
        resumed = await wait_for_human_copilot(
            self.page,
            page_num=3,
            reason="Next button not found",
            status_callback=mock_callback,
            timeout_seconds=1
        )
        # Did not advance, must return False boundedly
        self.assertFalse(resumed)
        # Status callback must have received timeout alert
        self.assertTrue(any("Co-Pilot Timeout" in a or "Co-Pilot Assistance Needed" in a for a in callback_alerts))

    async def test_05_telegram_clarification_contract(self):
        """Test clarification request payload contains required diagnostic context."""
        callback_alerts = []
        async def mock_callback(msg: str):
            callback_alerts.append(msg)

        html_content = "<html><body><h1>Clarification Form</h1></body></html>"
        await self.page.set_content(html_content)

        await wait_for_human_copilot(
            self.page,
            page_num=2,
            reason="Required field 'Security Clearance' is unanswerable",
            status_callback=mock_callback,
            timeout_seconds=1
        )
        
        # Verify message contains structured context
        assistance_msg = next((m for m in callback_alerts if "Co-Pilot Assistance Needed" in m), "")
        self.assertIn("Page 2", assistance_msg)
        self.assertIn("Security Clearance", assistance_msg)
        self.assertIn("AI credit spending is paused", assistance_msg)

    async def test_06_unauthorized_facts_produce_unanswerable(self):
        """Test that unknown personal facts produce UNANSWERABLE without inventing values."""
        field = {
            "id": "ssn_field",
            "role": "textbox",
            "label": "Social Security Number",
            "type": "text",
            "required": True
        }
        DynamicSchema = generate_dynamic_schema([field])
        
        unanswerable_payload = {
            "ssn_field": {
                "value": None,
                "confidence": 0.0,
                "source": "unauthorized",
                "status": "UNANSWERABLE",
                "reason": "SSN is not in authorized profile facts."
            }
        }
        parsed = DynamicSchema(**unanswerable_payload).model_dump()
        self.assertEqual(parsed["ssn_field"]["status"], "UNANSWERABLE")
        self.assertIsNone(parsed["ssn_field"]["value"])

    async def test_07_restart_isolation_no_state_leak(self):
        """Test that fresh RunContext has zero contamination from prior runs."""
        # Run 1: with cached answers and verified inventory
        ctx1 = RunContext(run_id="run_101", confirmed_profile={"first_name": "Alice"})
        q1 = NormalizedQuestion(fingerprint="fp_q1", id="q1", label="Age", role="textbox")
        ctx1.question_inventory[q1.fingerprint] = q1
        ctx1.question_cache[q1.fingerprint] = {"value": "25", "status": "ANSWERED"}
        ctx1.verified_answers[q1.fingerprint] = "25"
        ctx1.token_metrics["ai_calls"] = 12
        
        # Run 2: fresh run
        ctx2 = RunContext(run_id="run_102", confirmed_profile={"first_name": "Bob"})
        self.assertEqual(len(ctx2.question_inventory), 0, "Fresh run inventory must be empty.")
        self.assertEqual(len(ctx2.question_cache), 0, "Fresh run question cache must be empty.")
        self.assertEqual(len(ctx2.verified_answers), 0, "Fresh run verified answers must be empty.")
        self.assertEqual(ctx2.token_metrics["ai_calls"], 0, "Fresh run metrics must start at zero.")
        self.assertEqual(ctx2.confirmed_profile["first_name"], "Bob")

    async def test_08_dynamic_option_mutation_invalidates_cache(self):
        """Test that mutating options changes signature and invalidates cached answer."""
        ctx = RunContext(run_id="options_mutation_test")
        
        # Initial question with 2 options
        q_initial = NormalizedQuestion(
            fingerprint="q_opts_sig_1",
            id="f_choice",
            label="Delivery Speed",
            role="radio",
            options=["Standard (3-5 days)", "Express (1-2 days)"]
        )
        ctx.question_inventory[q_initial.fingerprint] = q_initial
        ctx.question_cache[q_initial.fingerprint] = {"value": "Standard (3-5 days)", "status": "ANSWERED"}
        
        # Cache hit initially
        hit = ctx.get_cached_answer(q_initial)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["value"], "Standard (3-5 days)")
        
        # Dynamic update: Overnight shipping added
        q_mutated = NormalizedQuestion(
            fingerprint="q_opts_sig_2",
            id="f_choice",
            label="Delivery Speed",
            role="radio",
            options=["Standard (3-5 days)", "Express (1-2 days)", "Overnight (Priority)"]
        )
        miss = ctx.get_cached_answer(q_mutated)
        self.assertIsNone(miss, "Mutated option list must invalidate stale cache entry.")

    async def test_09_stale_iframe_recovery(self):
        """Test stale or destroyed iframe during scraping recovers gracefully without crash."""
        mock_frame = MagicMock()
        mock_frame.evaluate = AsyncMock(side_effect=Exception("Execution context was destroyed, most likely because of a navigation."))
        
        # Scrape frame directly on stale frame
        fields = await scrape_frame(mock_frame, frame_index=1)
        self.assertEqual(fields, [], "Stale iframe must return empty list without uncaught exception.")

    async def test_10_changed_dropdown_options(self):
        """Test changed dropdown options in DOM produce updated normalized field options and signature."""
        html = """
        <html><body>
            <select id="sel_shipping" name="shipping">
                <option value="ground">Ground</option>
                <option value="air">Air</option>
            </select>
        </body></html>
        """
        await self.page.set_content(html)
        fields1 = await scrape_page(self.page)
        sel1 = next(f for f in fields1 if f.get("id") == "sel_shipping" or f.get("name") == "shipping")
        self.assertEqual(sel1["options"], ["Ground", "Air"])
        
        # Dynamically mutate options in DOM
        await self.page.evaluate("""() => {
            const sel = document.getElementById('sel_shipping');
            const opt = document.createElement('option');
            opt.value = 'drone';
            opt.innerText = 'Drone Delivery';
            sel.appendChild(opt);
        }""")
        
        fields2 = await scrape_page(self.page)
        sel2 = next(f for f in fields2 if f.get("id") == "sel_shipping" or f.get("name") == "shipping")
        self.assertEqual(sel2["options"], ["Ground", "Air", "Drone Delivery"])
        self.assertNotEqual(sel1["fingerprint"], sel2["fingerprint"])

    async def test_11_browser_navigation_during_pending_clarification(self):
        """Test wait_for_human_copilot detects live page navigation and resumes cleanly."""
        html = "<html><body><h1>Pending Clarification Page</h1></body></html>"
        await self.page.set_content(html)
        
        async def delayed_navigation():
            await asyncio.sleep(0.5)
            await self.page.evaluate("() => { window.location.hash = '#advanced_page'; }")
            
        asyncio.create_task(delayed_navigation())
        
        resumed = await wait_for_human_copilot(
            self.page,
            page_num=1,
            reason="Waiting for user review",
            timeout_seconds=3
        )
        self.assertTrue(resumed, "Browser navigation during Co-Pilot must trigger auto-resume.")

    async def test_12_failed_dom_verification(self):
        """Test verify_control_state accurately reports verification failure when DOM does not match."""
        html = """
        <html><body>
            <input type="checkbox" id="chk_agree" name="agree" value="agree">
        </body></html>
        """
        await self.page.set_content(html)
        
        # We expect it to be checked (True), but in the DOM it is unchecked (False)
        field = {"id": "chk_agree", "role": "checkbox", "options": ["I Agree"]}
        result = await verify_control_state(self.page, field, expected_val="I Agree")
        
        self.assertFalse(result.is_verified)
        self.assertIsNotNone(result.failure_reason)

    async def test_13_safe_stopping_when_required_answer_unknown(self):
        """Test that unanswerable required answers are not cached or accepted as verified."""
        ctx = RunContext(run_id="safe_stopping_test")
        
        fields = [
            {"id": "q_secret_pin", "role": "textbox", "label": "Secret Bank PIN", "required": True, "type": "text"}
        ]
        
        # AI returns UNANSWERABLE with value None
        mock_choice = MagicMock()
        mock_choice.message.content = json.dumps({
            "q_secret_pin": {
                "value": None,
                "confidence": 0.0,
                "source": "unauthorized",
                "status": "UNANSWERABLE",
                "reason": "Secret Bank PIN is unsupported and unknown."
            }
        })
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        
        with patch("ai_engine.client.chat.completions.create", new_callable=AsyncMock) as mock_create:
            mock_create.return_value = mock_response
            answers = await _get_form_answers_single_chunk(fields, {}, run_context=ctx)
            
            # Must remain UNANSWERABLE with None value
            self.assertIn("q_secret_pin", answers)
            self.assertEqual(answers["q_secret_pin"]["status"], "UNANSWERABLE")
            self.assertIsNone(answers["q_secret_pin"]["value"])
            # Cache guard must have prevented unanswerable value from being cached
            self.assertEqual(len(ctx.question_cache), 0, "Unanswerable fields must never be stored into question cache.")

    async def test_14_answer_provenance_and_conflicting_instructions(self):
        """Test that explicit survey-specific instructions override default persona preferences without fabrication."""
        ctx = RunContext(
            run_id="provenance_test",
            confirmed_profile={"full_name": "Default Persona User", "role": "Generalist"},
            user_instructions="CRITICAL PRIORITY: Target role is Senior Systems Architect. Full Name is Alex Morgan."
        )
        
        fields = [
            {"id": "q_role", "role": "textbox", "label": "Target Role", "type": "text"},
            {"id": "q_name", "role": "textbox", "label": "Full Name", "type": "text"}
        ]
        
        # When AI processes with the specific instruction, it adheres to the explicit instruction
        mock_choice = MagicMock()
        mock_choice.message.content = json.dumps({
            "q_role": {
                "value": "Senior Systems Architect",
                "confidence": 1.0,
                "source": "instruction",
                "status": "answered",
                "reason": "Explicit survey instruction override."
            },
            "q_name": {
                "value": "Alex Morgan",
                "confidence": 1.0,
                "source": "instruction",
                "status": "answered",
                "reason": "Explicit survey instruction override."
            }
        })
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        
        with patch("ai_engine.client.chat.completions.create", new_callable=AsyncMock) as mock_create:
            mock_create.return_value = mock_response
            answers = await _get_form_answers_single_chunk(fields, {}, persona=ctx.user_instructions, run_context=ctx)
            
            self.assertEqual(answers["q_role"]["value"], "Senior Systems Architect")
            self.assertEqual(answers["q_name"]["value"], "Alex Morgan")
            self.assertEqual(answers["q_role"]["source"], "instruction")

if __name__ == "__main__":
    unittest.main()
