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
from scraper import scrape_frame
from ai_engine import generate_dynamic_schema, get_form_answers
from playwright.async_api import async_playwright

class TestStillbotRegressionSuite(unittest.TestCase):
    
    def test_01_question_fingerprint_stability(self):
        """Test that identical questions generate identical fingerprints across renders."""
        fp1 = compute_question_fingerprint(0, "radio", "What is your gender?", ["Male", "Female", "Non-binary"])
        fp2 = compute_question_fingerprint(0, "radio", "  what is your GENDER?  ", ["Female", "Non-binary", "Male"])
        self.assertEqual(fp1, fp2, "Fingerprints must be case and option-order invariant.")
        
        fp_diff = compute_question_fingerprint(0, "radio", "What is your age?", ["18-24", "25-34"])
        self.assertNotEqual(fp1, fp_diff, "Different questions must have distinct fingerprints.")

    def test_02_instruction_precedence_hierarchy(self):
        """Test strict instruction hierarchy: safety > confirmed facts > survey instructions > global prefs > default."""
        confirmed_profile = {"age": 28, "email": "test@domain.com"}
        user_instructions = "My age is 35. Set location to California."
        global_prefs = {"age": 30, "tone": "formal"}
        
        # Current run user instruction overrides profile fact
        val, src = InstructionPrecedence.resolve_fact("age", confirmed_profile, user_instructions, global_prefs)
        self.assertEqual(val, "35")
        self.assertEqual(src, "survey_specific_instruction")
        
        # Profile fact when no explicit user instruction
        val_email, src_email = InstructionPrecedence.resolve_fact("email", confirmed_profile, user_instructions, global_prefs)
        self.assertEqual(val_email, "test@domain.com")
        self.assertEqual(src_email, "confirmed_respondent_fact")

        # Global pref when neither
        val_tone, src_tone = InstructionPrecedence.resolve_fact("tone", confirmed_profile, user_instructions, global_prefs)
        self.assertEqual(val_tone, "formal")
        self.assertEqual(src_tone, "global_user_preference")

    def test_03_deterministic_fact_resolution(self):
        """Test RunContext zero-token deterministic resolution."""
        ctx = RunContext(
            run_id="test_run",
            confirmed_profile={"email": "surveyor@example.com", "zip_code": "90210", "first_name": "Alex", "gender": "Male"}
        )
        
        q_email = NormalizedQuestion(fingerprint="fp1", id="f1", label="Please enter your email address:", role="textbox")
        res_email = ctx.resolve_deterministic_answer(q_email)
        self.assertIsNotNone(res_email)
        self.assertEqual(res_email[0], "surveyor@example.com")
        
        q_gender = NormalizedQuestion(fingerprint="fp2", id="f2", label="Gender", role="radio", options=["Female", "Male", "Other"])
        res_gender = ctx.resolve_deterministic_answer(q_gender)
        self.assertIsNotNone(res_gender)
        self.assertEqual(res_gender[0], "Male")
        
        self.assertEqual(ctx.token_metrics["deterministic_hits"], 2)

    def test_04_question_inventory_and_caching(self):
        """Test question inventory registration, answer caching, and status tracking."""
        ctx = RunContext(run_id="run_test_04")
        raw_fields = [
            {"id": "stillbot_f0_0", "role": "radio", "label": "Do you own a car?", "options": ["Yes", "No"]},
            {"id": "stillbot_f0_1", "role": "textbox", "label": "Postal Code", "options": []}
        ]
        
        new_qs = ctx.register_discovered_questions(raw_fields)
        self.assertEqual(len(new_qs), 2)
        
        fp_car = new_qs[0].fingerprint
        ctx.mark_verified(fp_car, "Yes")
        self.assertEqual(ctx.question_inventory[fp_car].status, AnswerStatus.VERIFIED)
        self.assertEqual(ctx.verified_answers[fp_car], "Yes")
        
        # Second registration pass with DOM index shifted
        raw_fields_shifted = [
            {"id": "stillbot_f0_99", "role": "radio", "label": "Do you own a car?", "options": ["Yes", "No"]}
        ]
        new_qs_2 = ctx.register_discovered_questions(raw_fields_shifted)
        self.assertEqual(len(new_qs_2), 0, "Previously registered question must not be counted as newly discovered.")
        self.assertEqual(ctx.question_inventory[fp_car].id, "stillbot_f0_99", "Locator id must be updated cleanly.")
        self.assertEqual(ctx.question_inventory[fp_car].status, AnswerStatus.VERIFIED)

    def test_05_page_state_transitions(self):
        """Test PageState state machine transitions."""
        ctx = RunContext(run_id="run_state_test")
        self.assertEqual(ctx.page_state, PageState.INITIALISING)
        
        ctx.transition_state(PageState.LOADING)
        self.assertEqual(ctx.page_state, PageState.LOADING)
        
        ctx.transition_state(PageState.QUESTION_DISCOVERY)
        self.assertEqual(ctx.page_state, PageState.QUESTION_DISCOVERY)
        
        ctx.transition_state(PageState.ANSWER_GENERATION)
        self.assertEqual(ctx.page_state, PageState.ANSWER_GENERATION)
        
        ctx.transition_state(PageState.FILLING)
        self.assertEqual(ctx.page_state, PageState.FILLING)
        
        ctx.transition_state(PageState.VERIFYING)
        self.assertEqual(ctx.page_state, PageState.VERIFYING)
        
        ctx.transition_state(PageState.WAITING_FOR_PAGE_CHANGE)
        self.assertEqual(ctx.page_state, PageState.WAITING_FOR_PAGE_CHANGE)
        
        ctx.transition_state(PageState.COMPLETED)
        self.assertEqual(ctx.page_state, PageState.COMPLETED)

    def test_06_in_run_caching_skips_ai(self):
        """Test that verified questions in question_cache bypass LLM calls."""
        async def run_async():
            ctx = RunContext(run_id="run_cache_test")
            field = {
                "id": "q1",
                "role": "radio",
                "label": "Are you registered to vote?",
                "options": ["Yes", "No"],
                "fingerprint": "fp_vote_q"
            }
            # Pre-populate cache
            ctx.question_cache["fp_vote_q"] = {
                "value": "Yes",
                "confidence": 1.0,
                "source": "persona",
                "status": "answered",
                "reason": "Cached decision"
            }
            
            answers = await get_form_answers([field], {}, run_context=ctx)
            self.assertEqual(answers["q1"]["value"], "Yes")
            self.assertEqual(ctx.token_metrics["cache_hits"], 1)
            self.assertEqual(ctx.token_metrics["ai_calls"], 0, "Must not call LLM when answer is in cache.")
            
        asyncio.run(run_async())

    def test_20_logical_question_id_reordered_dom_and_duplicates(self):
        """Test question identity is stable across DOM sibling reordering and distinguishes duplicate labels."""
        # 1. Identical question reordered in DOM maintains identical logical id
        lid_1 = compute_logical_question_id(0, "fs_account", "textbox", "Username")
        lid_2 = compute_logical_question_id(0, "fs_account", "textbox", "  USERNAME  ")
        self.assertEqual(lid_1, lid_2, "Logical question ID must be case/whitespace invariant.")
        
        # 2. Identical label across different container sections produces distinct logical IDs
        lid_shipping_zip = compute_logical_question_id(0, "section_shipping", "textbox", "ZIP Code")
        lid_billing_zip = compute_logical_question_id(0, "section_billing", "textbox", "ZIP Code")
        self.assertNotEqual(lid_shipping_zip, lid_billing_zip, "Duplicate labels in different sections must have distinct logical IDs.")

    def test_21_changed_option_set_invalidates_cache(self):
        """Test that changed option sets prevent stale answer reuse."""
        ctx = RunContext(run_id="test_cache_invalidation")
        
        # Original question with options A and B
        q_v1 = NormalizedQuestion(
            fingerprint="q1_sig1",
            id="f1",
            label="Preferred Contact",
            role="radio",
            options=["Email", "Phone"]
        )
        ctx.question_inventory[q_v1.fingerprint] = q_v1
        ctx.question_cache[q_v1.fingerprint] = {"value": "Email", "status": "ANSWERED"}
        
        # Cache hit for identical options
        hit = ctx.get_cached_answer(q_v1)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["value"], "Email")
        
        # Revised question with different options (SMS added)
        q_v2 = NormalizedQuestion(
            fingerprint="q1_sig2",
            id="f1",
            label="Preferred Contact",
            role="radio",
            options=["Email", "Phone", "SMS"]
        )
        # Attempt cache lookup with new fingerprint
        miss = ctx.get_cached_answer(q_v2)
        self.assertIsNone(miss, "Changed option set must invalidate or bypass cached answer.")

    def test_22_unanswerable_personal_facts_preserve_unresolved(self):
        """Test that AI schema safely represents UNANSWERABLE/NEEDS_CLARIFICATION without fabricating values."""
        fields = [
            {"id": "fav_color", "label": "What is your favorite pet's maiden name?", "type": "text", "required": True}
        ]
        DynamicSchema = generate_dynamic_schema(fields)
        
        # Valid unresolved payload with value=None and status='UNANSWERABLE'
        unresolved_data = {
            "fav_color": {
                "value": None,
                "confidence": 0.0,
                "source": "unauthorized",
                "status": "UNANSWERABLE",
                "reason": "Respondent pet information not provided in persona."
            }
        }
        parsed = DynamicSchema(**unresolved_data).model_dump()
        self.assertEqual(parsed["fav_color"]["status"], "UNANSWERABLE")
        self.assertIsNone(parsed["fav_color"]["value"])

    def test_23_gift_card_keyword_does_not_override_selection(self):
        """Test that questions mentioning rewards or gift cards do not force Amazon selection."""
        field = {
            "id": "q_reward",
            "role": "radio",
            "label": "Which incentive or reward would you prefer for completing this study?",
            "options": ["PayPal Transfer", "Amazon Gift Card", "Charity Donation", "Virtual Visa"]
        }
        ctx = RunContext(run_id="reward_test", confirmed_profile={"first_name": "Taylor"})
        norm_q = NormalizedQuestion(
            fingerprint="fp_rew",
            id="q_reward",
            role="radio",
            label=field["label"],
            options=field["options"]
        )
        det_res = ctx.resolve_deterministic_answer(norm_q)
        if det_res:
            self.assertNotEqual(det_res[0], "Amazon Gift Card", "Reward keyword must never force Amazon gift card override.")
        else:
            self.assertIsNone(det_res)

class TestPlaywrightMockQuestionnaire(unittest.IsolatedAsyncioTestCase):
    
    async def asyncSetUp(self):
        self.p = await async_playwright().start()
        self.browser = await self.p.chromium.launch(headless=True)
        self.page = await self.browser.new_page()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.p.stop()

    async def test_07_scrape_and_group_controls(self):
        """Test that scraper finds custom buttons, groups radios and checkboxes, and detects required status."""
        html_content = """
        <!DOCTYPE html>
        <html>
        <body>
            <fieldset class="question-container">
                <legend>What devices do you own? *</legend>
                <label><input type="checkbox" name="dev" value="Laptop"> Laptop</label>
                <label><input type="checkbox" name="dev" value="Smartphone"> Smartphone</label>
                <label><input type="checkbox" name="dev" value="Tablet"> Tablet</label>
            </fieldset>

            <div class="question-container">
                <div class="question-title">Rate our service:</div>
                <div role="radiogroup" aria-label="Satisfaction">
                    <button role="radio" aria-label="Poor" data-choice="poor">1</button>
                    <button role="radio" aria-label="Average" data-choice="avg">2</button>
                    <button role="radio" aria-label="Excellent" data-choice="exc">3</button>
                </div>
            </div>

            <div class="question-container">
                <label for="comments">Additional Feedback:</label>
                <textarea id="comments" name="feedback"></textarea>
            </div>
            
            <button id="next-btn" type="submit">Next</button>
        </body>
        </html>
        """
        await self.page.set_content(html_content)
        fields = await scrape_frame(self.page.main_frame, 0)
        
        # Checkbox group must be aggregated into one question with options
        chk_groups = [f for f in fields if f.get("role") == "checkbox" and len(f.get("options", [])) > 1]
        self.assertEqual(len(chk_groups), 1, "Checkboxes sharing a parent legend must be grouped.")
        self.assertIn("Laptop", chk_groups[0]["options"])
        self.assertIn("Smartphone", chk_groups[0]["options"])
        self.assertTrue(chk_groups[0]["required"])

        # Radio group with custom buttons
        radio_groups = [f for f in fields if f.get("role") == "radio"]
        self.assertEqual(len(radio_groups), 1, "Custom button[role='radio'] must be grouped into single radio question.")
        self.assertEqual(len(radio_groups[0]["options"]), 3)

        # Textarea
        textareas = [f for f in fields if f.get("role") == "textbox"]
        self.assertEqual(len(textareas), 1)

    async def test_08_conditional_branching_detection(self):
        """Test that when an answer dynamically reveals a conditional question, it is cleanly detected."""
        html_content = """
        <!DOCTYPE html>
        <html>
        <body>
            <fieldset id="q_pets">
                <legend>Do you have any pets?</legend>
                <label><input type="radio" name="pets" id="pets_no" value="No"> No</label>
                <label><input type="radio" name="pets" id="pets_yes" value="Yes" onclick="document.getElementById('hidden_branch').style.display='block'"> Yes</label>
            </fieldset>

            <div id="hidden_branch" style="display: none;">
                <fieldset id="q_pet_types">
                    <legend>What kind of pet?</legend>
                    <label><input type="radio" name="pet_type" value="Dog"> Dog</label>
                    <label><input type="radio" name="pet_type" value="Cat"> Cat</label>
                </fieldset>
            </div>
        </body>
        </html>
        """
        await self.page.set_content(html_content)
        ctx = RunContext(run_id="branch_test")
        
        # Initial scan: only 1 question (hidden_branch is display: none)
        fields_pass1 = await scrape_frame(self.page.main_frame, 0)
        self.assertEqual(len(fields_pass1), 1)
        ctx.register_discovered_questions(fields_pass1)
        
        # Click "Yes" to trigger dynamic branch
        await self.page.click("#pets_yes")
        
        # Second scan: newly revealed question is discovered
        fields_pass2 = await scrape_frame(self.page.main_frame, 0)
        self.assertEqual(len(fields_pass2), 2)
        newly_revealed = ctx.register_discovered_questions(fields_pass2)
        self.assertEqual(len(newly_revealed), 1, "Only the newly revealed conditional question must be added.")
        self.assertIn("What kind of pet?", newly_revealed[0].label)

    async def test_09_matrix_and_likert_grid_detection(self):
        """Test matrix grid with rows and rating columns."""
        html_content = """
        <!DOCTYPE html>
        <html>
        <body>
            <table class="cf-grid-table">
                <thead>
                    <tr>
                        <th>Statement</th>
                        <th class="cf-grid-col__label">Strongly Disagree</th>
                        <th class="cf-grid-col__label">Neutral</th>
                        <th class="cf-grid-col__label">Strongly Agree</th>
                    </tr>
                </thead>
                <tbody>
                    <tr id="row_1">
                        <td class="cf-grid-row__label">The software is intuitive</td>
                        <td><input type="radio" name="r1" value="1" aria-label="Strongly Disagree"></td>
                        <td><input type="radio" name="r1" value="2" aria-label="Neutral"></td>
                        <td><input type="radio" name="r1" value="3" aria-label="Strongly Agree"></td>
                    </tr>
                    <tr id="row_2">
                        <td class="cf-grid-row__label">I encounter frequent bugs</td>
                        <td><input type="radio" name="r2" value="1" aria-label="Strongly Disagree"></td>
                        <td><input type="radio" name="r2" value="2" aria-label="Neutral"></td>
                        <td><input type="radio" name="r2" value="3" aria-label="Strongly Agree"></td>
                    </tr>
                </tbody>
            </table>
        </body>
        </html>
        """
        await self.page.set_content(html_content)
        fields = await scrape_frame(self.page.main_frame, 0)
        
        self.assertEqual(len(fields), 2, "Each matrix row must aggregate into a distinct question.")
        self.assertTrue(fields[0].get("is_matrix"))
        self.assertIn("The software is intuitive", fields[0].get("matrix_row") or fields[0].get("label"))
        self.assertEqual(len(fields[0]["options"]), 3)
        self.assertIn("Strongly Agree", fields[0]["options"])

    async def test_10_native_and_custom_dropdowns(self):
        """Test native <select> and custom ARIA combobox extraction."""
        html_content = """
        <!DOCTYPE html>
        <html>
        <body>
            <div class="question-container">
                <label for="state-select">Select your state:</label>
                <select id="state-select" name="state">
                    <option value="">-- Choose --</option>
                    <option value="NY">New York</option>
                    <option value="CA">California</option>
                    <option value="TX">Texas</option>
                </select>
            </div>
            
            <div class="question-container">
                <div id="combo-label">Industry:</div>
                <div role="combobox" aria-labelledby="combo-label">
                    <div role="option" value="tech">Technology</div>
                    <div role="option" value="health">Healthcare</div>
                    <div role="option" value="edu">Education</div>
                </div>
            </div>
        </body>
        </html>
        """
        await self.page.set_content(html_content)
        fields = await scrape_frame(self.page.main_frame, 0)
        
        native_sel = next(f for f in fields if f.get("type") == "select-one")
        self.assertIn("California", native_sel["options"])
        
        custom_combo = next(f for f in fields if f.get("role") == "combobox" and f.get("type") != "select-one")
        self.assertIn("Technology", custom_combo["options"])

    async def test_11_preflight_final_audit_fills_missed_fields(self):
        """Test pre-flight final audit identifies unselected inputs and fills them."""
        from main import verify_and_fill_unanswered_fields
        html_content = """
        <!DOCTYPE html>
        <html>
        <body>
            <fieldset id="q_satisfied">
                <legend>Are you satisfied?</legend>
                <label><input type="radio" name="sat" id="sat_yes" value="Yes" data-stillbot-id="stillbot_f0_0"> Yes</label>
                <label><input type="radio" name="sat" id="sat_no" value="No" data-stillbot-id="stillbot_f0_1"> No</label>
            </fieldset>
        </body>
        </html>
        """
        await self.page.set_content(html_content)
        
        fields = [{
            "id": "stillbot_f0_0",
            "role": "radio",
            "label": "Are you satisfied?",
            "options": ["Yes", "No"],
            "option_map": {"Yes": "stillbot_f0_0", "No": "stillbot_f0_1"}
        }]
        answers = {"stillbot_f0_0": {"value": "Yes", "confidence": 1.0, "source": "inferred"}}
        session_mem = {}
        
        resolved = await verify_and_fill_unanswered_fields(self.page, fields, answers, session_mem)
        self.assertEqual(resolved, 1, "Must resolve unselected radio before advance.")
        
        # Verify DOM is actually checked
        is_checked = await self.page.is_checked("#sat_yes")
        self.assertTrue(is_checked, "Radio element in live DOM must be checked.")

    async def test_12_distinct_questions_with_identical_labels(self):
        """Test two distinct questions with identical wording in different sections/fieldsets."""
        html_content = """
        <!DOCTYPE html>
        <html>
        <body>
            <fieldset id="sec_primary">
                <legend>Primary Applicant</legend>
                <label for="p_phone">Phone Number:</label>
                <input type="text" id="p_phone" name="primary_phone">
            </fieldset>
            <fieldset id="sec_co">
                <legend>Co-Applicant</legend>
                <label for="co_phone">Phone Number:</label>
                <input type="text" id="co_phone" name="co_phone">
            </fieldset>
        </body>
        </html>
        """
        await self.page.set_content(html_content)
        fields = await scrape_frame(self.page.main_frame, 0)
        self.assertEqual(len(fields), 2)
        
        ctx = RunContext(run_id="run_identical_labels")
        registered = ctx.register_discovered_questions(fields)
        self.assertEqual(len(registered), 2, "Questions with identical labels in distinct containers must register separately.")
        self.assertNotEqual(registered[0].logical_id, registered[1].logical_id, "Logical IDs must be distinct.")
        self.assertNotEqual(registered[0].fingerprint, registered[1].fingerprint, "Fingerprints must be distinct.")

    def test_13_options_change_invalidates_cache(self):
        """Test question whose options change on conditional branch causes cache invalidation."""
        ctx = RunContext(run_id="run_opts_change")
        q_init = [{
            "id": "q_tier",
            "role": "radio",
            "label": "Select Plan:",
            "options": ["Basic", "Pro"],
            "container_key": "sec_plan"
        }]
        registered = ctx.register_discovered_questions(q_init)
        lid = registered[0].logical_id
        
        # Manually cache an answer
        ctx.question_cache[lid] = {"value": "Pro", "confidence": 1.0, "source": "persona"}
        self.assertIsNotNone(ctx.get_cached_answer(registered[0]))
        
        # Branching changes options: "Pro" is removed or replaced by Enterprise
        q_updated = [{
            "id": "q_tier",
            "role": "radio",
            "label": "Select Plan:",
            "options": ["Basic", "Enterprise"],
            "container_key": "sec_plan"
        }]
        re_registered = ctx.register_discovered_questions(q_updated)
        self.assertEqual(len(re_registered), 1, "Question with changed options must be flagged as requiring reprocessing.")
        self.assertEqual(re_registered[0].revision, 2)
        # Verify cache was invalidated
        self.assertIsNone(ctx.get_cached_answer(re_registered[0]), "Stale cached answer must be invalidated.")

    async def test_14_live_dom_verification_failure_detected(self):
        """Test that a click failing to change the live DOM is NOT marked VERIFIED."""
        from main import verify_control_state
        html_content = """
        <!DOCTYPE html>
        <html>
        <body>
            <label><input type="radio" id="r_opt1" name="g1" value="Option A" disabled data-stillbot-id="f_opt1"> Option A</label>
        </body>
        </html>
        """
        await self.page.set_content(html_content)
        field = {
            "id": "f_opt1",
            "role": "radio",
            "label": "Choice Question",
            "options": ["Option A"],
            "option_map": {"Option A": "f_opt1"}
        }
        # Nothing is checked in DOM
        is_verified, val, err = await verify_control_state(self.page, field, "Option A")
        self.assertFalse(is_verified, "Must not verify if live DOM element was not checked.")
        self.assertIsNotNone(err)

    def test_15_run_isolation_clean_state(self):
        """Test that a fresh run starts with a clean cache and does not inherit prior runs."""
        ctx1 = RunContext(run_id="run_1")
        ctx1.question_cache["fp_shared"] = {"value": "SecretAnswer"}
        
        ctx2 = RunContext(run_id="run_2")
        self.assertEqual(len(ctx2.question_cache), 0, "Fresh RunContext must have an empty cache.")
        self.assertEqual(len(ctx2.question_inventory), 0, "Fresh RunContext must have empty inventory.")

    async def test_16_multitier_conditional_branching(self):
        """Test 3 tiers of dynamic conditional questions appearing sequentially."""
        html_content = """
        <!DOCTYPE html>
        <html>
        <body>
            <div id="q1_box">
                <label>Do you drive?</label>
                <input type="radio" id="drive_yes" name="drive" value="Yes" onclick="document.getElementById('q2_box').style.display='block'"> Yes
            </div>
            <div id="q2_box" style="display:none;">
                <label>Vehicle Engine Type:</label>
                <input type="radio" id="eng_ev" name="eng" value="Electric" onclick="document.getElementById('q3_box').style.display='block'"> Electric
            </div>
            <div id="q3_box" style="display:none;">
                <label>Charger Type:</label>
                <input type="radio" id="chg_fast" name="chg" value="DC Fast"> DC Fast
            </div>
        </body>
        </html>
        """
        await self.page.set_content(html_content)
        ctx = RunContext(run_id="run_3tier")
        
        # Tier 1
        f1 = await scrape_frame(self.page.main_frame, 0)
        self.assertEqual(len(f1), 1)
        ctx.register_discovered_questions(f1)
        
        # Trigger Tier 2
        await self.page.click("#drive_yes")
        f2 = await scrape_frame(self.page.main_frame, 0)
        self.assertEqual(len(f2), 2)
        new_q2 = ctx.register_discovered_questions(f2)
        self.assertEqual(len(new_q2), 1)
        
        # Trigger Tier 3
        await self.page.click("#eng_ev")
        f3 = await scrape_frame(self.page.main_frame, 0)
        self.assertEqual(len(f3), 3)
        new_q3 = ctx.register_discovered_questions(f3)
        self.assertEqual(len(new_q3), 1)
        self.assertIn("Charger", new_q3[0].label)

    async def test_17_question_becomes_hidden(self):
        """Test question that becomes hidden when parent answer changes is not reported visible."""
        html_content = """
        <!DOCTYPE html>
        <html>
        <body>
            <fieldset>
                <label><input type="radio" id="opt_show" name="mode" value="Show" onclick="document.getElementById('dyn_field').style.display='block'"> Show</label>
                <label><input type="radio" id="opt_hide" name="mode" value="Hide" onclick="document.getElementById('dyn_field').style.display='none'"> Hide</label>
            </fieldset>
            <div id="dyn_field" style="display:block;">
                <label for="sub_input">Details:</label>
                <input type="text" id="sub_input" name="details">
            </div>
        </body>
        </html>
        """
        await self.page.set_content(html_content)
        f_init = await scrape_frame(self.page.main_frame, 0)
        self.assertEqual(len(f_init), 2)
        
        # Hide the field
        await self.page.click("#opt_hide")
        f_hidden = await scrape_frame(self.page.main_frame, 0)
        self.assertEqual(len(f_hidden), 1, "Hidden field must not be returned by scraper.")

    async def test_18_multiselect_checkbox_group_live_verification(self):
        """Test multi-select checkbox group verified against live DOM checked set."""
        from main import verify_control_state
        html_content = """
        <!DOCTYPE html>
        <html>
        <body>
            <fieldset id="fs_skills">
                <legend>Skills</legend>
                <label><input type="checkbox" id="c1" value="Python" checked data-stillbot-id="chk_py"> Python</label>
                <label><input type="checkbox" id="c2" value="Playwright" checked data-stillbot-id="chk_pw"> Playwright</label>
                <label><input type="checkbox" id="c3" value="Rust" data-stillbot-id="chk_rs"> Rust</label>
            </fieldset>
        </body>
        </html>
        """
        await self.page.set_content(html_content)
        field = {
            "id": "chk_group_1",
            "role": "checkbox",
            "label": "Skills",
            "options": ["Python", "Playwright", "Rust"],
            "option_map": {"Python": "chk_py", "Playwright": "chk_pw", "Rust": "chk_rs"}
        }
        # Expected match: Python & Playwright
        is_ok, val, err = await verify_control_state(self.page, field, ["Python", "Playwright"])
        self.assertTrue(is_ok)
        self.assertIn("Python", val)
        self.assertIn("Playwright", val)
        
        # Expected mismatch: including Rust (which is unchecked)
        is_ok_mismatch, _, err_mismatch = await verify_control_state(self.page, field, ["Python", "Rust"])
        self.assertFalse(is_ok_mismatch)
        self.assertIsNotNone(err_mismatch)

    async def test_19_disabled_advance_button_handling(self):
        """Test advance button that is temporarily disabled during loading/calculation."""
        html_content = """
        <!DOCTYPE html>
        <html>
        <body>
            <button id="adv_btn" disabled>Next</button>
            <script>
                setTimeout(() => {
                    document.getElementById('adv_btn').disabled = false;
                }, 2000);
            </script>
        </body>
        </html>
        """
        await self.page.set_content(html_content)
        btn = self.page.locator("#adv_btn")
        self.assertTrue(await btn.is_disabled())
        
        # Wait for button to enable
        await self.page.wait_for_function("!document.getElementById('adv_btn').disabled")
        self.assertFalse(await btn.is_disabled())

    async def test_24_substring_false_positive_rejected_in_verification(self):
        """Test verify_control_state strictly rejects substring false positives (e.g. Agree vs Disagree, 1 vs 10)."""
        from main import verify_control_state
        
        # 1. Radio: Selected is "Disagree", expected is "Agree"
        html_radio = """
        <!DOCTYPE html>
        <html>
        <body>
            <fieldset id="fs_opinion">
                <legend>Feedback</legend>
                <label><input type="radio" name="op" id="r_dis" value="Disagree" checked data-stillbot-id="stillbot_r_dis"> Disagree</label>
                <label><input type="radio" name="op" id="r_agr" value="Agree" data-stillbot-id="stillbot_r_agr"> Agree</label>
            </fieldset>
        </body>
        </html>
        """
        await self.page.set_content(html_radio)
        field_radio = {
            "id": "stillbot_r_agr",
            "role": "radio",
            "label": "Feedback",
            "options": ["Disagree", "Agree"],
            "option_map": {"Disagree": "stillbot_r_dis", "Agree": "stillbot_r_agr"}
        }
        res = await verify_control_state(self.page, field_radio, "Agree")
        self.assertFalse(res.is_verified, "'Agree' must NOT verify against selected 'Disagree'")
        self.assertIn("Disagree", str(res.actual_state))

        # 2. Textbox: Actual value is "Notice", expected is "No"
        html_text = """
        <!DOCTYPE html>
        <html>
        <body>
            <label for="txt_in">Comment</label>
            <input type="text" id="txt_in" value="Notice" data-stillbot-id="stillbot_txt">
        </body>
        </html>
        """
        await self.page.set_content(html_text)
        field_text = {
            "id": "stillbot_txt",
            "role": "textbox",
            "label": "Comment"
        }
        res_text = await verify_control_state(self.page, field_text, "No")
        self.assertFalse(res_text.is_verified, "'No' must NOT verify against live text 'Notice'")

        # 3. Numeric: Actual value is "10", expected is "1"
        html_num = """
        <!DOCTYPE html>
        <html>
        <body>
            <label><input type="radio" name="num" id="r_10" value="10" checked data-stillbot-id="stillbot_10"> 10</label>
            <label><input type="radio" name="num" id="r_1" value="1" data-stillbot-id="stillbot_1"> 1</label>
        </body>
        </html>
        """
        await self.page.set_content(html_num)
        field_num = {
            "id": "stillbot_1",
            "role": "radio",
            "label": "Rating",
            "options": ["10", "1"],
            "option_map": {"10": "stillbot_10", "1": "stillbot_1"}
        }
        res_num = await verify_control_state(self.page, field_num, "1")
        self.assertFalse(res_num.is_verified, "'1' must NOT verify against selected '10'")

    async def test_25_structured_verification_result_contract(self):
        """Test VerificationResult data model, attribute access, boolean truthiness, and tuple unpacking."""
        from main import verify_control_state, VerificationResult
        
        html_content = """
        <!DOCTYPE html>
        <html>
        <body>
            <label><input type="checkbox" id="chk_test" value="Yes" checked data-stillbot-id="chk_val"> Accept Terms</label>
        </body>
        </html>
        """
        await self.page.set_content(html_content)
        field = {
            "id": "chk_val",
            "role": "checkbox",
            "label": "Accept Terms",
            "options": ["Yes"],
            "option_map": {"Yes": "chk_val"}
        }
        
        res = await verify_control_state(self.page, field, "Yes")
        self.assertIsInstance(res, VerificationResult)
        self.assertTrue(res.is_verified)
        self.assertTrue(bool(res))
        self.assertEqual(res.control_type, "checkbox")
        self.assertEqual(res.expected_state, ["Yes"])
        
        # Tuple unpacking compatibility
        is_ok, live_val, failure_err = res
        self.assertTrue(is_ok)
        self.assertIsNone(failure_err)

    async def test_26_failed_interaction_does_not_mark_verified(self):
        """Test that an unselected control is never falsely reported as verified."""
        from main import verify_control_state
        
        html_content = """
        <!DOCTYPE html>
        <html>
        <body>
            <input type="checkbox" id="locked_chk" data-stillbot-id="locked_chk" disabled>
        </body>
        </html>
        """
        await self.page.set_content(html_content)
        field = {
            "id": "locked_chk",
            "role": "checkbox",
            "label": "Locked Checkbox",
            "options": ["Check"]
        }
        # Expected checked, but input is unchecked and disabled
        res = await verify_control_state(self.page, field, True)
        self.assertFalse(res.is_verified)
        self.assertIsNotNone(res.failure_reason)

if __name__ == "__main__":
    unittest.main()

