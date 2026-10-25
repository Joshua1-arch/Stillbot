import unittest
import asyncio
import os
import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
import tempfile
import json
from unittest.mock import AsyncMock, patch

from run_context import (
    PageState, AnswerStatus, NormalizedQuestion,
    compute_question_fingerprint, compute_logical_question_id,
    compute_options_signature, InstructionPrecedence, RunContext
)
from scraper import scrape_frame, scrape_page
from ai_engine import generate_dynamic_schema, get_form_answers
from main import verify_control_state, fill_field, VerificationResult
from playwright.async_api import async_playwright

class TestPhase2VerificationSuite(unittest.IsolatedAsyncioTestCase):
    
    async def asyncSetUp(self):
        self.p = await async_playwright().start()
        self.browser = await self.p.chromium.launch(headless=True)
        self.page = await self.browser.new_page()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.p.stop()

    async def test_p2_01_whitespace_and_unicode_normalization_in_verification(self):
        """Test verify_control_state normalizes unicode, smart quotes, non-breaking spaces, and trim."""
        html_content = """
        <!DOCTYPE html>
        <html>
        <body>
            <label for="txt_q">Quote & Dash:</label>
            <input type="text" id="txt_q" value="“Option—One”&nbsp;with&nbsp;space" data-stillbot-id="stillbot_txt_q">
        </body>
        </html>
        """
        await self.page.set_content(html_content)
        field = {
            "id": "stillbot_txt_q",
            "role": "textbox",
            "label": "Quote & Dash:"
        }
        # Expected contains standard ascii quotes, hyphens, and regular spaces
        res = await verify_control_state(self.page, field, '"Option-One" with space')
        self.assertTrue(res.is_verified, "Smart quotes, em-dashes, and non-breaking spaces must normalize cleanly.")
        self.assertIsNone(res.failure_reason)

    async def test_p2_02_select_element_duplicate_labels_different_values(self):
        """Test select dropdown where options share labels but have distinct values (e.g. pricing tiers)."""
        html_content = """
        <!DOCTYPE html>
        <html>
        <body>
            <label for="tier_select">Choose Plan:</label>
            <select id="tier_select" data-stillbot-id="sel_tier">
                <option value="">-- Choose --</option>
                <option value="basic_monthly" selected>Basic ($10/mo)</option>
                <option value="basic_annual">Basic ($100/yr)</option>
            </select>
        </body>
        </html>
        """
        await self.page.set_content(html_content)
        field = {
            "id": "sel_tier",
            "role": "combobox",
            "type": "select-one",
            "label": "Choose Plan:",
            "options": ["Basic ($10/mo)", "Basic ($100/yr)"]
        }
        # Expected is the monthly option which is selected
        res_correct = await verify_control_state(self.page, field, "Basic ($10/mo)")
        self.assertTrue(res_correct.is_verified)
        
        # Expected is annual, but monthly is selected
        res_wrong = await verify_control_state(self.page, field, "Basic ($100/yr)")
        self.assertFalse(res_wrong.is_verified)
        self.assertTrue(any(k in (res_wrong.failure_reason or "").lower() for k in ["mismatch", "does not match"]))

    async def test_p2_03_delayed_input_settle_verification(self):
        """Test verification handles asynchronous framework value updates."""
        html_content = """
        <!DOCTYPE html>
        <html>
        <body>
            <label for="delayed_input">Delayed Value:</label>
            <input type="text" id="delayed_input" value="" data-stillbot-id="txt_delayed">
            <script>
                setTimeout(() => {
                    document.getElementById('delayed_input').value = 'Settled Framework Value';
                }, 150);
            </script>
        </body>
        </html>
        """
        await self.page.set_content(html_content)
        field = {
            "id": "txt_delayed",
            "role": "textbox",
            "label": "Delayed Value:"
        }
        # Initially empty
        res_early = await verify_control_state(self.page, field, "Settled Framework Value")
        self.assertFalse(res_early.is_verified)
        
        # Wait for settled condition
        await self.page.wait_for_function("document.getElementById('delayed_input').value === 'Settled Framework Value'")
        res_settled = await verify_control_state(self.page, field, "Settled Framework Value")
        self.assertTrue(res_settled.is_verified)

    async def test_p2_04_detached_or_hidden_control_returns_explicit_failure(self):
        """Test that removing an element from DOM produces is_verified=False and safe failure reason without crashing."""
        html_content = """
        <!DOCTYPE html>
        <html>
        <body>
            <div id="target_container">
                <input type="text" id="temp_input" value="Active" data-stillbot-id="txt_temp">
            </div>
        </body>
        </html>
        """
        await self.page.set_content(html_content)
        field = {
            "id": "txt_temp",
            "role": "textbox",
            "label": "Temp Field"
        }
        # Detach element
        await self.page.evaluate("document.getElementById('temp_input').remove()")
        
        res = await verify_control_state(self.page, field, "Active")
        self.assertFalse(res.is_verified)
        self.assertIsNotNone(res.failure_reason)
        self.assertTrue(any(k in res.failure_reason.lower() for k in ["not found", "could not be located"]))

    async def test_p2_05_unsupported_custom_control_fails_safely(self):
        """Test an unmapped custom widget (e.g. signature canvas) produces unresolved status rather than false success."""
        html_content = """
        <!DOCTYPE html>
        <html>
        <body>
            <div class="custom-signature-container">
                <label>Please sign below:</label>
                <canvas id="sig_canvas" width="300" height="100"></canvas>
            </div>
        </body>
        </html>
        """
        await self.page.set_content(html_content)
        # Custom element without accessible role
        fields = await scrape_frame(self.page.main_frame, 0)
        # Canvas should not be converted to a falsely filled text or radio field
        canvas_fields = [f for f in fields if f.get("id") == "sig_canvas"]
        self.assertEqual(len(canvas_fields), 0, "Non-interactive canvas must not be scraped as standard form input.")

    async def test_p2_06_unanswerable_required_question_preserves_unresolved(self):
        """Test full orchestration handling of UNANSWERABLE state on required field."""
        ctx = RunContext(run_id="unanswerable_audit_test", confirmed_profile={"age": "32"})
        
        unanswerable_field = {
            "id": "sec_q1",
            "role": "textbox",
            "label": "What was the name of your elementary school mascot?",
            "required": True,
            "fingerprint": "fp_mascot"
        }
        
        # Register question
        norm_qs = ctx.register_discovered_questions([unanswerable_field])
        self.assertEqual(len(norm_qs), 1)
        norm_q = norm_qs[0]
        
        # Dynamic schema produces UNANSWERABLE
        DynamicSchema = generate_dynamic_schema([unanswerable_field])
        ai_response_payload = {
            "sec_q1": {
                "value": None,
                "confidence": 0.0,
                "source": "unauthorized",
                "status": "UNANSWERABLE",
                "reason": "Mascot name is unknown in authorized persona facts."
            }
        }
        parsed = DynamicSchema(**ai_response_payload).model_dump()
        self.assertEqual(parsed["sec_q1"]["status"], "UNANSWERABLE")
        self.assertIsNone(parsed["sec_q1"]["value"])
        
        # When unanswerable, cache must not record verified answer
        self.assertNotIn("fp_mascot", ctx.verified_answers)
        self.assertNotEqual(ctx.question_inventory[norm_q.logical_id].status, AnswerStatus.VERIFIED)

    async def test_p2_07_dynamically_loaded_options_handled_cleanly(self):
        """Test scraper and cache invalidation when options are populated asynchronously."""
        html_content = """
        <!DOCTYPE html>
        <html>
        <body>
            <label for="async_select">Select Category:</label>
            <select id="async_select" data-stillbot-id="sel_async">
                <option value="">Loading...</option>
            </select>
            <script>
                setTimeout(() => {
                    const sel = document.getElementById('async_select');
                    sel.innerHTML = '<option value="opt1">Category A</option><option value="opt2">Category B</option>';
                }, 200);
            </script>
        </body>
        </html>
        """
        await self.page.set_content(html_content)
        
        # Initial scrape: 1 option (Loading...)
        f_early = await scrape_frame(self.page.main_frame, 0)
        self.assertIn("Loading...", f_early[0]["options"])
        sig_early = compute_options_signature(f_early[0]["options"])
        
        # Wait for dynamic population
        await self.page.wait_for_function("document.getElementById('async_select').options.length > 1")
        f_late = await scrape_frame(self.page.main_frame, 0)
        self.assertIn("Category A", f_late[0]["options"])
        sig_late = compute_options_signature(f_late[0]["options"])
        
        # Signatures must differ, enforcing cache invalidation
        self.assertNotEqual(sig_early, sig_late, "Mutated options must produce distinct signatures.")

    async def test_p2_08_duplicate_labels_in_separate_questions_verified_accurately(self):
        """Test two distinct questions with identical options in separate fieldsets verify independently."""
        html_content = """
        <!DOCTYPE html>
        <html>
        <body>
            <fieldset id="fs_primary">
                <legend>Primary Contact Type</legend>
                <label><input type="radio" name="p_type" id="p_mob" value="Mobile" checked data-stillbot-id="p_mob"> Mobile</label>
                <label><input type="radio" name="p_type" id="p_land" value="Landline" data-stillbot-id="p_land"> Landline</label>
            </fieldset>

            <fieldset id="fs_secondary">
                <legend>Secondary Contact Type</legend>
                <label><input type="radio" name="s_type" id="s_mob" value="Mobile" data-stillbot-id="s_mob"> Mobile</label>
                <label><input type="radio" name="s_type" id="s_land" value="Landline" checked data-stillbot-id="s_land"> Landline</label>
            </fieldset>
        </body>
        </html>
        """
        await self.page.set_content(html_content)
        
        field_primary = {
            "id": "p_mob",
            "role": "radio",
            "label": "Primary Contact Type",
            "options": ["Mobile", "Landline"],
            "option_map": {"Mobile": "p_mob", "Landline": "p_land"}
        }
        field_secondary = {
            "id": "s_land",
            "role": "radio",
            "label": "Secondary Contact Type",
            "options": ["Mobile", "Landline"],
            "option_map": {"Mobile": "s_mob", "Landline": "s_land"}
        }
        
        # Verify primary is Mobile
        res_p = await verify_control_state(self.page, field_primary, "Mobile")
        self.assertTrue(res_p.is_verified)
        
        # Verify secondary is Landline
        res_s = await verify_control_state(self.page, field_secondary, "Landline")
        self.assertTrue(res_s.is_verified)
        
        # Cross-check: Secondary is NOT Mobile
        res_s_cross = await verify_control_state(self.page, field_secondary, "Mobile")
        self.assertFalse(res_s_cross.is_verified)

    async def test_p2_09_multiselect_checkbox_set_matching_policy(self):
        """Test checkbox group rejects partial subsets (e.g. expected 2, only 1 checked)."""
        html_content = """
        <!DOCTYPE html>
        <html>
        <body>
            <fieldset id="fs_langs">
                <legend>Languages Spoken</legend>
                <label><input type="checkbox" id="chk_en" value="English" checked data-stillbot-id="chk_en"> English</label>
                <label><input type="checkbox" id="chk_es" value="Spanish" data-stillbot-id="chk_es"> Spanish</label>
                <label><input type="checkbox" id="chk_fr" value="French" data-stillbot-id="chk_fr"> French</label>
            </fieldset>
        </body>
        </html>
        """
        await self.page.set_content(html_content)
        field = {
            "id": "chk_group",
            "role": "checkbox",
            "label": "Languages Spoken",
            "options": ["English", "Spanish", "French"],
            "option_map": {"English": "chk_en", "Spanish": "chk_es", "French": "chk_fr"}
        }
        # Expected both English and Spanish, but only English is checked
        res_partial = await verify_control_state(self.page, field, ["English", "Spanish"])
        self.assertFalse(res_partial.is_verified, "Partial selection must NOT pass verification.")
        self.assertIn("Spanish", (res_partial.failure_reason or ""))

    async def test_p2_10_stale_iframe_navigation_resilience(self):
        """Test scraper handles empty or navigating iframes safely without uncaught exceptions."""
        html_content = """
        <!DOCTYPE html>
        <html>
        <body>
            <div id="main_content">
                <label for="top_input">Top Level Input:</label>
                <input type="text" id="top_input" value="Main">
            </div>
            <iframe id="test_iframe" src="about:blank"></iframe>
        </body>
        </html>
        """
        await self.page.set_content(html_content)
        
        # Scrape page should inspect top frame and handle about:blank iframe cleanly
        fields = await scrape_page(self.page)
        self.assertTrue(len(fields) >= 1)
        self.assertEqual(fields[0].get("label"), "Top Level Input:")

if __name__ == "__main__":
    unittest.main()
