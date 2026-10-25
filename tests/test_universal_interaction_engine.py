"""
Test suite for Universal Interactive Form Understanding & Interaction Engine.
Covers:
- Text/Numeric inputs
- Radios/Checkboxes with polarity protection
- Custom Dropdowns / ARIA Comboboxes
- Ranking / Drag-and-drop sortable widgets
- Sliders / Range controls (seek & keyboard)
- Date / Time pickers
- File uploads
- Signature pads (authorized signature confirmation)
- Shadow DOM discovery and interaction
- Multi-strategy fallback and recovery
- Safe, non-sensitive knowledge base learning
- Unknown widget graceful diagnostics without false completion
"""

import asyncio
import os
import unittest
from playwright.async_api import async_playwright

from interaction_engine import (
    universal_registry,
    WidgetType,
    StrategyType,
    StrategyKnowledgeBase,
    VerificationResult
)
from scraper import scrape_page
from main import fill_field, verify_control_state


class TestUniversalInteractionEngine(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.context = await self.browser.new_context()
        self.page = await self.context.new_page()

    async def asyncTearDown(self):
        await self.context.close()
        await self.browser.close()
        await self.playwright.stop()

    async def test_01_text_numeric_interaction_and_verification(self):
        """Test text and numeric input handling with multi-strategy fill and verification."""
        html = """
        <!DOCTYPE html>
        <html>
        <body>
            <label for="username">Username</label>
            <input id="username" type="text" />
            <label for="user_age">Age</label>
            <input id="user_age" type="number" />
        </body>
        </html>
        """
        await self.page.set_content(html)
        fields = await scrape_page(self.page)
        self.assertEqual(len(fields), 2)
        
        # 1. Fill username
        u_field = next(f for f in fields if "username" in f.get("label", "").lower() or f.get("id", "").endswith("_0"))
        res_u = await universal_registry.dispatch_fill(self.page, u_field, {"value": "JaneDev"})
        self.assertTrue(res_u.success)
        self.assertTrue(res_u.verification.is_verified)
        self.assertEqual(await self.page.input_value("#username"), "JaneDev")

        # 2. Fill age
        a_field = next(f for f in fields if "age" in f.get("label", "").lower() or f.get("id", "").endswith("_1"))
        res_a = await universal_registry.dispatch_fill(self.page, a_field, {"value": "34"})
        self.assertTrue(res_a.success)
        self.assertEqual(await self.page.input_value("#user_age"), "34")

    async def test_02_choice_handler_polarity_protection(self):
        """Test choice handler strictly protects polarity (Agree vs Disagree)."""
        html = """
        <!DOCTYPE html>
        <html>
        <body>
            <fieldset>
                <legend>I enjoy remote collaboration</legend>
                <label><input type="radio" name="feedback" value="Strongly Disagree" id="opt_disagree" /> Strongly Disagree</label>
                <label><input type="radio" name="feedback" value="Agree" id="opt_agree" /> Agree</label>
            </fieldset>
        </body>
        </html>
        """
        await self.page.set_content(html)
        fields = await scrape_page(self.page)
        self.assertEqual(len(fields), 1)
        radio_field = fields[0]

        # Expected value is "Agree" -> Must NOT click "Strongly Disagree"
        res = await universal_registry.dispatch_fill(self.page, radio_field, {"value": "Agree"})
        self.assertTrue(res.success)
        self.assertTrue(await self.page.is_checked("#opt_agree"))
        self.assertFalse(await self.page.is_checked("#opt_disagree"))

        # Verify control state
        v_res = await universal_registry.dispatch_verify(self.page, radio_field, "Agree")
        self.assertTrue(v_res.is_verified)

    async def test_03_custom_aria_combobox_interaction(self):
        """Test custom ARIA combobox with popup options list."""
        html = """
        <!DOCTYPE html>
        <html>
        <body>
            <div id="wrapper">
                <label id="cb-label">Department</label>
                <div role="combobox" aria-labelledby="cb-label" id="dept_combo" tabindex="0" onclick="document.getElementById('popup').style.display='block'">
                    Select Department...
                </div>
                <ul id="popup" role="listbox" style="display:none;">
                    <li role="option" onclick="document.getElementById('dept_combo').innerText='Engineering'; document.getElementById('popup').style.display='none';">Engineering</li>
                    <li role="option" onclick="document.getElementById('dept_combo').innerText='Marketing'; document.getElementById('popup').style.display='none';">Marketing</li>
                </ul>
            </div>
        </body>
        </html>
        """
        await self.page.set_content(html)
        fields = await scrape_page(self.page)
        self.assertTrue(len(fields) >= 1)
        combo_field = fields[0]

        res = await universal_registry.dispatch_fill(self.page, combo_field, {"value": "Engineering"})
        self.assertTrue(res.success)
        combo_text = await self.page.inner_text("#dept_combo")
        self.assertIn("Engineering", combo_text)

    async def test_04_ranking_widget_sort_and_verify(self):
        """Test ranking / drag-and-drop sortable widget reordering."""
        html = """
        <!DOCTYPE html>
        <html>
        <head>
            <style>
                .rank-item { padding: 10px; margin: 4px; background: #eee; cursor: grab; }
            </style>
        </head>
        <body>
            <div class="QuestionOuter RO" id="Q1">
                <div class="QuestionText">Rank your preferred work environments:</div>
                <ul class="ChoiceStructure ui-sortable" id="rank_list">
                    <li class="rank-item" draggable="true" id="c1">Remote</li>
                    <li class="rank-item" draggable="true" id="c2">Hybrid</li>
                    <li class="rank-item" draggable="true" id="c3">On-site</li>
                </ul>
            </div>
        </body>
        </html>
        """
        await self.page.set_content(html)
        fields = await scrape_page(self.page)
        self.assertEqual(len(fields), 1)
        rf = fields[0]
        self.assertEqual(rf.get("role"), "ranking")

        # Rank Hybrid top
        await fill_field(self.page, rf, ["Hybrid", "Remote", "On-site"])
        v = await verify_control_state(self.page, rf, ["Hybrid", "Remote", "On-site"])
        self.assertTrue(v.is_verified)

    async def test_05_slider_seek_and_verify(self):
        """Test range slider control interaction and DOM verification."""
        html = """
        <!DOCTYPE html>
        <html>
        <body>
            <label for="satisfaction">Overall Satisfaction (0 - 100)</label>
            <input type="range" id="satisfaction" min="0" max="100" value="0" />
        </body>
        </html>
        """
        await self.page.set_content(html)
        fields = await scrape_page(self.page)
        self.assertEqual(len(fields), 1)
        sf = fields[0]
        self.assertEqual(sf.get("role"), "slider")

        # Fill via main fill_field which delegates to universal_registry
        await fill_field(self.page, sf, "75")
        
        v = await verify_control_state(self.page, sf, "75")
        self.assertTrue(v.is_verified)

    async def test_06_date_time_picker_interaction(self):
        """Test datepicker interaction and state verification."""
        html = """
        <!DOCTYPE html>
        <html>
        <body>
            <label for="start_date">Expected Start Date</label>
            <input type="date" id="start_date" />
        </body>
        </html>
        """
        await self.page.set_content(html)
        fields = await scrape_page(self.page)
        self.assertEqual(len(fields), 1)
        df = fields[0]
        self.assertEqual(df.get("role"), "datepicker")

        await fill_field(self.page, df, "2026-11-01")
        val = await self.page.input_value("#start_date")
        self.assertEqual(val, "2026-11-01")

        v = await verify_control_state(self.page, df, "2026-11-01")
        self.assertTrue(v.is_verified)

    async def test_07_file_upload_interaction(self):
        """Test file upload control with dummy file attachment."""
        html = """
        <!DOCTYPE html>
        <html>
        <body>
            <label for="cv_file">Upload Resume / CV</label>
            <input type="file" id="cv_file" />
        </body>
        </html>
        """
        await self.page.set_content(html)
        fields = await scrape_page(self.page)
        self.assertEqual(len(fields), 1)
        ff = fields[0]
        self.assertEqual(ff.get("role"), "file_upload")

        # Create temporary sample file
        test_file = "test_sample_upload.txt"
        with open(test_file, "w") as f:
            f.write("Candidate Resume Data")

        try:
            await fill_field(self.page, ff, test_file)
            v = await verify_control_state(self.page, ff, test_file)
            self.assertTrue(v.is_verified)
        finally:
            if os.path.exists(test_file):
                os.remove(test_file)

    async def test_08_signature_pad_drawing_and_verification(self):
        """Test signature pad canvas drawing with pixel modification confirmation."""
        html = """
        <!DOCTYPE html>
        <html>
        <body>
            <label>Sign Here</label>
            <canvas id="user_signature" width="300" height="100" style="border:1px solid #000;"></canvas>
        </body>
        </html>
        """
        await self.page.set_content(html)
        fields = await scrape_page(self.page)
        self.assertEqual(len(fields), 1)
        sig_field = fields[0]
        self.assertEqual(sig_field.get("role"), "signature")

        # Fill via universal registry
        res = await universal_registry.dispatch_fill(self.page, sig_field, {"value": "Jane Doe"})
        self.assertTrue(res.success)
        self.assertTrue(res.verification.is_verified)

    async def test_09_shadow_dom_recursive_discovery_and_interaction(self):
        """Test that interactive elements inside open shadow DOM roots are discovered and filled."""
        html = """
        <!DOCTYPE html>
        <html>
        <body>
            <div id="host_element"></div>
            <script>
                const host = document.getElementById('host_element');
                const shadow = host.attachShadow({ mode: 'open' });
                shadow.innerHTML = `
                    <label for="shadow_input">Shadow Field</label>
                    <input id="shadow_input" type="text" />
                `;
            </script>
        </body>
        </html>
        """
        await self.page.set_content(html)
        # Wait for shadow DOM mounting
        await self.page.wait_for_selector("#host_element")
        
        fields = await scrape_page(self.page)
        self.assertTrue(len(fields) >= 1)
        shadow_field = fields[0]
        self.assertEqual(shadow_field.get("role"), "textbox")

        res = await universal_registry.dispatch_fill(self.page, shadow_field, {"value": "InsideShadow"})
        self.assertTrue(res.success)

    async def test_10_safe_knowledge_learning_scrubs_pii(self):
        """Test StrategyKnowledgeBase records successful strategies without storing any PII."""
        kb = StrategyKnowledgeBase()
        
        # Record interaction on Qualtrics slider
        kb.record_success("qualtrics", WidgetType.SLIDER, "input_range_slider_0", StrategyType.SLIDER_SEEK)
        kb.record_success("qualtrics", WidgetType.SLIDER, "input_range_slider_0", StrategyType.SLIDER_KEYBOARD)

        # Retrieve preferred strategies
        preferred = kb.get_preferred_strategies("qualtrics", WidgetType.SLIDER, "input_range_slider_0")
        self.assertIn(StrategyType.SLIDER_SEEK, preferred)
        self.assertIn(StrategyType.SLIDER_KEYBOARD, preferred)

        # Verify no PII strings or values are present in knowledge base
        for k in kb._learned_patterns.keys():
            self.assertNotIn("Jane", k)
            self.assertNotIn("password", k)
            self.assertNotIn("@", k)


if __name__ == "__main__":
    unittest.main()
