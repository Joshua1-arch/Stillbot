"""
Adversarial Audit & Generalization Test Suite for Universal Interaction Engine.
Covers:
- Matrix with custom headers and row/column binding
- React-style delayed state updates & rerenders
- Scrollable containers & offscreen elements
- Custom controls whose displayed value differs from stored value
- Keyboard-only navigation & focus-based selection
- CAPTCHA / anti-bot challenge detection & safe escalation
- Unfamiliar ranking markup and custom sortable cards
- Strategy knowledge base fingerprint isolation & PII scrubbing
- Stale locator & dynamic remount recovery
- Signature pad signer authorization & audit trail
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
    VerificationResult,
    detect_security_challenge
)
from scraper import scrape_page
from main import fill_field, verify_control_state


class TestAdversarialInteractionAudit(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.context = await self.browser.new_context()
        self.page = await self.context.new_page()

    async def asyncTearDown(self):
        await self.context.close()
        await self.browser.close()
        await self.playwright.stop()

    async def test_01_matrix_reversed_headers_and_custom_radio(self):
        """Test matrix grid with table layout, custom column headers, and row binding."""
        html = """
        <!DOCTYPE html>
        <html>
        <body>
            <table role="grid" class="cf-grid-table">
                <thead>
                    <tr>
                        <th>Statements</th>
                        <th class="cf-grid-col__label">Poor</th>
                        <th class="cf-grid-col__label">Average</th>
                        <th class="cf-grid-col__label">Excellent</th>
                    </tr>
                </thead>
                <tbody>
                    <tr role="row">
                        <td class="cf-grid-row__label">Technical Support</td>
                        <td><input type="radio" name="row_tech" value="Poor" aria-label="Poor, response for Technical Support" /></td>
                        <td><input type="radio" name="row_tech" value="Average" aria-label="Average, response for Technical Support" /></td>
                        <td><input type="radio" name="row_tech" value="Excellent" aria-label="Excellent, response for Technical Support" /></td>
                    </tr>
                </tbody>
            </table>
        </body>
        </html>
        """
        await self.page.set_content(html)
        fields = await scrape_page(self.page)
        self.assertEqual(len(fields), 1)
        matrix_field = fields[0]
        self.assertTrue(matrix_field.get("is_matrix"))

        # Fill via main fill_field -> universal_registry
        await fill_field(self.page, matrix_field, {"value": "Excellent"})
        
        v = await verify_control_state(self.page, matrix_field, "Excellent")
        self.assertTrue(v.is_verified)
        self.assertIn("Excellent", str(v.actual_state))

    async def test_02_react_delayed_state_update(self):
        """Test React-style delayed state update where DOM reflects value after debounced render."""
        html = """
        <!DOCTYPE html>
        <html>
        <body>
            <label for="delayed_input">Debounced Username</label>
            <input id="delayed_input" type="text" />
            <div id="preview" style="margin-top: 10px;">Initial</div>
            <script>
                const input = document.getElementById('delayed_input');
                const preview = document.getElementById('preview');
                input.addEventListener('input', (e) => {
                    setTimeout(() => {
                        preview.innerText = e.target.value;
                    }, 200);
                });
            </script>
        </body>
        </html>
        """
        await self.page.set_content(html)
        fields = await scrape_page(self.page)
        self.assertEqual(len(fields), 1)
        
        res = await universal_registry.dispatch_fill(self.page, fields[0], {"value": "AsyncUser"})
        self.assertTrue(res.success)
        self.assertTrue(res.verification.is_verified)
        self.assertEqual(await self.page.input_value("#delayed_input"), "AsyncUser")

    async def test_03_scrollable_container_and_offscreen_control(self):
        """Test interaction with a control placed deep inside a scrollable viewport container."""
        html = """
        <!DOCTYPE html>
        <html>
        <head>
            <style>
                #scroll_pane { height: 250px; overflow-y: scroll; border: 1px solid #ccc; }
                .spacer { height: 1200px; }
            </style>
        </head>
        <body>
            <div id="scroll_pane">
                <div class="spacer">Scroll down...</div>
                <label for="deep_input">Deep Field</label>
                <input id="deep_input" type="text" />
            </div>
        </body>
        </html>
        """
        await self.page.set_content(html)
        fields = await scrape_page(self.page)
        self.assertEqual(len(fields), 1)

        res = await universal_registry.dispatch_fill(self.page, fields[0], {"value": "ScrolledValue"})
        self.assertTrue(res.success)
        self.assertEqual(await self.page.input_value("#deep_input"), "ScrolledValue")

    async def test_04_custom_control_displayed_vs_stored_value(self):
        """Test custom control where option label differs from underlying stored code."""
        html = """
        <!DOCTYPE html>
        <html>
        <body>
            <label for="country_sel">Country</label>
            <select id="country_sel">
                <option value="">Select country...</option>
                <option value="US">United States of America (USA)</option>
                <option value="CA">Canada</option>
                <option value="UK">United Kingdom</option>
            </select>
        </body>
        </html>
        """
        await self.page.set_content(html)
        fields = await scrape_page(self.page)
        self.assertEqual(len(fields), 1)

        # Target by full text
        await fill_field(self.page, fields[0], "United States of America (USA)")
        v = await verify_control_state(self.page, fields[0], "United States of America (USA)")
        self.assertTrue(v.is_verified)
        self.assertEqual(await self.page.input_value("#country_sel"), "US")

    async def test_05_keyboard_only_accessible_control(self):
        """Test custom input that primarily expects keyboard entry without native mouse clicks."""
        html = """
        <!DOCTYPE html>
        <html>
        <body>
            <label for="kb_text">Keyboard Only Input</label>
            <input id="kb_text" type="text" />
        </body>
        </html>
        """
        await self.page.set_content(html)
        fields = await scrape_page(self.page)
        self.assertEqual(len(fields), 1)

        # Execute using keyboard type strategy
        inspection = await universal_registry.handlers[0].inspect(self.page, fields[0])
        attempt = await universal_registry.handlers[0].execute(self.page, inspection, StrategyType.KEYBOARD_TYPE, "KeyboardTyping")
        self.assertTrue(attempt.success)
        self.assertEqual(await self.page.input_value("#kb_text"), "KeyboardTyping")

    async def test_06_captcha_detection_triggers_human_intervention(self):
        """Test that third-party CAPTCHA / anti-bot challenges are cleanly detected for human escalation."""
        # 1. Cloudflare Turnstile
        html_cf = """
        <!DOCTYPE html>
        <html>
        <body>
            <form>
                <div class="cf-turnstile" data-sitekey="test-key" style="width:300px; height:65px;"></div>
            </form>
        </body>
        </html>
        """
        await self.page.set_content(html_cf)
        challenge_cf = await detect_security_challenge(self.page)
        self.assertIsNotNone(challenge_cf)
        self.assertEqual(challenge_cf.get("type"), "Cloudflare Turnstile")

        # 2. Google reCAPTCHA
        html_rc = """
        <!DOCTYPE html>
        <html>
        <body>
            <div class="g-recaptcha" data-sitekey="test-key" style="width:300px; height:75px;"></div>
        </body>
        </html>
        """
        await self.page.set_content(html_rc)
        challenge_rc = await detect_security_challenge(self.page)
        self.assertIsNotNone(challenge_rc)
        self.assertEqual(challenge_rc.get("type"), "Google reCAPTCHA")

    async def test_07_unfamiliar_ranking_markup_and_reordering(self):
        """Test ranking widget with unconventional markup (draggable cards without standard Qualtrics classes)."""
        html = """
        <!DOCTYPE html>
        <html>
        <head>
            <style>
                .sortable-item { padding: 8px; margin: 4px; border: 1px solid #999; }
            </style>
        </head>
        <body>
            <div class="custom-ranking-container" id="rank_box">
                <div class="question-title">Rank your priorities:</div>
                <div class="ui-sortable" id="sort_list">
                    <div class="sortable-item" draggable="true" id="p1">Speed</div>
                    <div class="sortable-item" draggable="true" id="p2">Quality</div>
                    <div class="sortable-item" draggable="true" id="p3">Cost</div>
                </div>
            </div>
        </body>
        </html>
        """
        await self.page.set_content(html)
        fields = await scrape_page(self.page)
        self.assertEqual(len(fields), 1)
        rf = fields[0]
        self.assertEqual(rf.get("role"), "ranking")

        # Reorder Quality top
        await fill_field(self.page, rf, ["Quality", "Speed", "Cost"])
        v = await verify_control_state(self.page, rf, ["Quality", "Speed", "Cost"])
        self.assertTrue(v.is_verified)

    async def test_08_strategy_knowledge_base_anonymization_and_fingerprint_isolation(self):
        """Test strategy knowledge base enforces fingerprint isolation and strictly scrubs personal data."""
        kb = StrategyKnowledgeBase()

        # Two different platforms and fingerprints
        kb.record_success("qualtrics", WidgetType.SLIDER, "slider_horizontal_100", StrategyType.SLIDER_SEEK)
        kb.record_success("surveymonkey", WidgetType.SLIDER, "slider_matrix_50", StrategyType.SLIDER_KEYBOARD)

        # Check preferred strategies are isolated per fingerprint
        q_strat = kb.get_preferred_strategies("qualtrics", WidgetType.SLIDER, "slider_horizontal_100")
        sm_strat = kb.get_preferred_strategies("surveymonkey", WidgetType.SLIDER, "slider_matrix_50")
        
        self.assertEqual(q_strat, [StrategyType.SLIDER_SEEK])
        self.assertEqual(sm_strat, [StrategyType.SLIDER_KEYBOARD])

        # Verify no personal data or email patterns stored
        for key in kb._learned_patterns.keys():
            self.assertNotIn("joshua", key.lower())
            self.assertNotIn("@", key)
            self.assertNotIn("phone", key.lower())

    async def test_09_stale_locator_and_dynamic_remount_recovery(self):
        """Test graceful recovery when DOM rerenders or elements remount."""
        html = """
        <!DOCTYPE html>
        <html>
        <body>
            <div id="container">
                <input id="dynamic_f" type="text" />
            </div>
            <script>
                // Simulate framework replacing element on interaction
                document.getElementById('dynamic_f').addEventListener('focus', () => {
                    const old = document.getElementById('dynamic_f');
                    const clone = old.cloneNode(true);
                    old.replaceWith(clone);
                });
            </script>
        </body>
        </html>
        """
        await self.page.set_content(html)
        fields = await scrape_page(self.page)
        self.assertEqual(len(fields), 1)

        res = await universal_registry.dispatch_fill(self.page, fields[0], {"value": "RemountedValue"})
        self.assertTrue(res.success)
        self.assertEqual(await self.page.input_value("#dynamic_f"), "RemountedValue")

    async def test_10_signature_pad_signer_authorization_audit(self):
        """Test signature pad records authorized signer confirmation without forging identity."""
        html = """
        <!DOCTYPE html>
        <html>
        <body>
            <label id="sig_label">Authorized Signer Consent</label>
            <canvas id="authorized_sig" width="300" height="120" style="border: 1px solid #333;"></canvas>
        </body>
        </html>
        """
        await self.page.set_content(html)
        fields = await scrape_page(self.page)
        self.assertEqual(len(fields), 1)
        sig_field = fields[0]

        # Fill with verified respondent authorization
        res = await universal_registry.dispatch_fill(self.page, sig_field, {"value": "Authorized User"})
        self.assertTrue(res.success)
        self.assertEqual(res.applied_strategy, StrategyType.AUTHORIZED_SIGNATURE)
        self.assertTrue(res.verification.is_verified)


if __name__ == "__main__":
    unittest.main()
