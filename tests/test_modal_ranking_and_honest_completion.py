import unittest
import asyncio
import os
import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
import json
from unittest.mock import AsyncMock, patch, MagicMock

from run_context import RunContext, PageState, AnswerStatus, NormalizedQuestion
from scraper import scrape_page, detect_blocking_modal, check_page_has_questions
from main import (
    verify_control_state, fill_field, handle_blocking_modal,
    wait_for_human_copilot, check_is_completed, VerificationResult
)
from playwright.async_api import async_playwright

class TestModalRankingAndHonestCompletion(unittest.IsolatedAsyncioTestCase):
    
    async def asyncSetUp(self):
        self.p = await async_playwright().start()
        self.browser = await self.p.chromium.launch(headless=True)
        self.page = await self.browser.new_page()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.p.stop()

    # 1. A visible Qualtrics-style modal blocking Next
    async def test_01_qualtrics_style_modal_blocking_next(self):
        """Test detection and handling of Qualtrics portal modal intercepting pointer events."""
        html = """
        <!DOCTYPE html>
        <html>
        <head><title>Qualtrics Survey</title>
        <style>
            .portal { position: fixed; top: 0; left: 0; width: 100%; height: 100%; z-index: 9999; background: rgba(0,0,0,0.5); }
            #dialog.modal { position: absolute; top: 30%; left: 30%; width: 400px; background: white; padding: 20px; }
        </style>
        </head>
        <body>
            <div id="SurveyEngine">
                <div class="QuestionOuter"><div class="QuestionText">What is your department?</div></div>
                <button type="button" id="next-button">Next</button>
            </div>
            <div class="portal">
                <div id="dialog" class="modal" tabindex="-1" role="dialog" aria-modal="true" aria-label="modal">
                    <h3>Connection problem</h3>
                    <p>There was a connection problem communicating with the server. Please try again.</p>
                    <button type="button" class="retry-btn">Try Again</button>
                </div>
            </div>
        </body>
        </html>
        """
        await self.page.set_content(html)
        modal_info = await detect_blocking_modal(self.page)
        
        self.assertTrue(modal_info["found"])
        self.assertEqual(modal_info["type"], "network_error")
        self.assertIn("connection problem", modal_info["text"].lower())
        self.assertTrue(len(modal_info["buttons"]) > 0)
        self.assertEqual(modal_info["buttons"][0]["text"], "Try Again")

    # 2. A disabled Next button during loading
    async def test_02_disabled_next_button_during_loading(self):
        """Test disabled Next button state detection preventing premature clicks."""
        html = """
        <!DOCTYPE html>
        <html>
        <body>
            <div class="QuestionOuter"><div class="QuestionText">Loading survey content...</div></div>
            <button disabled type="button" id="next-button" aria-label="Next page" class="navigation-button">Next</button>
        </body>
        </html>
        """
        await self.page.set_content(html)
        next_btn = self.page.locator("#next-button").first
        
        is_disabled = await next_btn.is_disabled()
        self.assertTrue(is_disabled, "Next button must be identified as disabled during loading")

    # 3. A network-error dialog that persists
    async def test_03_persistent_network_error_dialog(self):
        """Test persistent connection error modal activates bounded retry then halts cleanly."""
        html = """
        <!DOCTYPE html>
        <html>
        <body>
            <div class="portal">
                <div id="dialog" class="modal" role="dialog" style="display: block; width: 350px; height: 200px;">
                    <h3>Network Error</h3>
                    <p>Failed to connect to survey host. Please try again.</p>
                    <button type="button" id="modal-retry-btn">Retry</button>
                </div>
            </div>
        </body>
        </html>
        """
        await self.page.set_content(html)
        run_ctx = RunContext(run_id="test_run", page_state=PageState.QUESTION_DISCOVERY)
        
        # Simulate that retries already reached threshold (>2)
        self.page._network_modal_retries = 2
        
        # Mock wait_for_human_copilot to simulate human timeout
        with patch("main.wait_for_human_copilot", new_callable=AsyncMock) as mock_copilot:
            mock_copilot.return_value = False
            can_proceed, msg = await handle_blocking_modal(self.page, page_num=1, run_ctx=run_ctx)
            
            self.assertFalse(can_proceed)
            self.assertEqual(run_ctx.page_state, PageState.TIMED_OUT)
            self.assertIn("persistent connection error", msg)
            mock_copilot.assert_called_once()

    # 4. A visible ranking question that semantic scraper extracts (not zero fields)
    async def test_04_visible_ranking_question_extracted(self):
        """Test Qualtrics ranking/drag-and-drop question extraction with role='ranking'."""
        html = """
        <!DOCTYPE html>
        <html>
        <body>
            <div class="QuestionOuter RO Ranking" id="QID100">
                <div class="QuestionText">From the list above, please select your top three formats for reaching frontline workers specifically.</div>
                <div class="QuestionBody">
                    <ul class="ChoiceStructure ui-sortable">
                        <li class="Selection" id="QR~QID100~1" draggable="true" style="display:block; width:200px; height:40px;">
                            <span class="handle ui-sortable-handle">:::</span>
                            <span class="Label">Short videos</span>
                        </li>
                        <li class="Selection" id="QR~QID100~2" draggable="true" style="display:block; width:200px; height:40px;">
                            <span class="handle ui-sortable-handle">:::</span>
                            <span class="Label">Interactive scenario-based modules</span>
                        </li>
                        <li class="Selection" id="QR~QID100~3" draggable="true" style="display:block; width:200px; height:40px;">
                            <span class="handle ui-sortable-handle">:::</span>
                            <span class="Label">Step-by-step written guides</span>
                        </li>
                        <li class="Selection" id="QR~QID100~4" draggable="true" style="display:block; width:200px; height:40px;">
                            <span class="handle ui-sortable-handle">:::</span>
                            <span class="Label">Podcasts</span>
                        </li>
                    </ul>
                </div>
            </div>
            <button type="button" id="next-button">Next</button>
        </body>
        </html>
        """
        await self.page.set_content(html)
        fields = await scrape_page(self.page)
        
        self.assertEqual(len(fields), 1)
        rf = fields[0]
        self.assertEqual(rf["role"], "ranking")
        self.assertIn("top three formats", rf["label"].lower())
        self.assertEqual(rf["target_count"], 3)
        self.assertEqual(len(rf["options"]), 4)
        self.assertIn("Short videos", rf["options"])
        self.assertIn("Interactive scenario-based modules", rf["options"])

    # 5. A ranking widget that supports dragging and verifies the resulting order
    async def test_05_ranking_widget_drag_and_drop_verified(self):
        """Test drag-and-drop interaction with DOM order verification."""
        html = """
        <!DOCTYPE html>
        <html>
        <head>
        <style>
            .rank-item { padding: 10px; margin: 5px; background: #eee; cursor: grab; }
            .Group.Ranked { min-height: 100px; border: 2px dashed #999; padding: 10px; }
        </style>
        </head>
        <body>
            <div class="QuestionOuter RO" id="q_rank">
                <div class="QuestionText">Rank top formats:</div>
                <div class="Items">
                    <ul id="source-list" class="ui-sortable">
                        <li class="rank-item" id="item_1" draggable="true">Short videos</li>
                        <li class="rank-item" id="item_2" draggable="true">Interactive modules</li>
                    </ul>
                </div>
                <div class="Group Ranked" id="target-box">
                    <ul id="target-list" class="ui-sortable"></ul>
                </div>
            </div>
            <script>
                // Realistic drag and drop event listeners
                const targetBox = document.getElementById('target-box');
                const targetList = document.getElementById('target-list');
                document.querySelectorAll('.rank-item').forEach(el => {
                    el.addEventListener('dragstart', (e) => {
                        e.dataTransfer.setData('text/plain', el.id);
                    });
                });
                targetBox.addEventListener('dragover', (e) => e.preventDefault());
                targetBox.addEventListener('drop', (e) => {
                    e.preventDefault();
                    const id = e.dataTransfer.getData('text/plain') || 'item_1';
                    const it = document.getElementById(id);
                    if (it) targetList.appendChild(it);
                });
            </script>
        </body>
        </html>
        """
        await self.page.set_content(html)
        fields = await scrape_page(self.page)
        self.assertEqual(len(fields), 1)
        rf = fields[0]
        
        answer = {"value": ["Short videos", "Interactive modules"]}
        await fill_field(self.page, rf, answer)
        
        # Verify DOM state
        res = await verify_control_state(self.page, rf, answer["value"])
        self.assertTrue(res.is_verified)
        self.assertEqual(res.control_type, "ranking")

    # 6. A ranking widget that cannot be manipulated reliably and fails verification
    async def test_06_unmanipulable_ranking_widget_fails_verification(self):
        """Test ranking widget that ignores drag actions fails verification rather than falsely claiming success."""
        html = """
        <!DOCTYPE html>
        <html>
        <body>
            <div class="QuestionOuter RO" id="q_locked">
                <div class="QuestionText">Rank your preferences:</div>
                <div class="Group Ranked" id="dropzone_locked">
                    <ul>
                        <li class="rank-item" id="locked_opt1">Option A</li>
                        <li class="rank-item" id="locked_opt2">Option B</li>
                    </ul>
                </div>
            </div>
        </body>
        </html>
        """
        await self.page.set_content(html)
        fields = await scrape_page(self.page)
        self.assertEqual(len(fields), 1)
        rf = fields[0]
        
        # Attempt to verify an impossible order that does not exist in DOM
        res = await verify_control_state(self.page, rf, ["Nonexistent Choice Z", "Choice X"])
        self.assertFalse(res.is_verified)
        self.assertIsNotNone(res.failure_reason)

    # 7. A stale page that does not change after navigation attempts
    async def test_07_progress_watchdog_prevents_endless_retries(self):
        """Test progress watchdog halts after max passes and does not increment page_num."""
        run_ctx = RunContext(run_id="watchdog_test", max_same_page_passes=3)
        url = "https://survey.example.com/form1"
        dom_fp = "120:False:What is your age?"
        
        # Pass 1
        is_stalled, count = run_ctx.check_progress_watchdog(url, dom_fp)
        self.assertFalse(is_stalled)
        self.assertEqual(count, 1)
        
        # Pass 2
        is_stalled, count = run_ctx.check_progress_watchdog(url, dom_fp)
        self.assertFalse(is_stalled)
        self.assertEqual(count, 2)
        
        # Pass 3
        is_stalled, count = run_ctx.check_progress_watchdog(url, dom_fp)
        self.assertTrue(is_stalled, "Watchdog must trigger on 3rd identical pass")
        self.assertEqual(count, 3)

    # 8. Co-pilot timeout producing a non-success exit status
    async def test_08_copilot_timeout_produces_timed_out_status(self):
        """Test wait_for_human_copilot timeout returns False and never reports success."""
        await self.page.set_content("<html><body><h1>Survey Question</h1></body></html>")
        
        # Call wait_for_human_copilot with 0.1s timeout
        resumed = await wait_for_human_copilot(self.page, page_num=1, reason="Test timeout", timeout_seconds=0.1)
        self.assertFalse(resumed, "Co-pilot must return False when timeout expires")

    # 9. Genuine survey completion producing success only after verification
    async def test_09_genuine_completion_verification(self):
        """Test genuine survey completion only reports COMPLETED when thank-you text is verified."""
        html = """
        <!DOCTYPE html>
        <html>
        <body>
            <h1>Thank you for taking this survey</h1>
            <p>Your response has been recorded.</p>
        </body>
        </html>
        """
        await self.page.set_content(html)
        
        # When advance button is not present and thank-you text is present
        completed, reason = await check_is_completed(self.page, has_answered=True, has_advance_btn=False)
        self.assertTrue(completed)
        self.assertIn("Completion message detected", reason)
        
        # But if a Next button is still visible, it must NOT be marked completed
        not_completed, _ = await check_is_completed(self.page, has_answered=True, has_advance_btn=True)
        self.assertFalse(not_completed, "Cannot complete while advance button remains visible")

    # 10. An answer that was attempted but never confirmed in DOM
    async def test_10_unconfirmed_answer_fails_validation(self):
        """Test answer that fails DOM verification is never marked verified in RunContext."""
        run_ctx = RunContext(run_id="audit_run")
        raw_field = {
            "id": "f_req_text",
            "role": "textbox",
            "label": "Required Security Code",
            "required": True
        }
        questions = run_ctx.register_discovered_questions([raw_field])
        q = questions[0]
        
        # Mark validation failed
        run_ctx.mark_validation_failed(q.fingerprint, "Input element did not retain value in DOM")
        
        is_complete, unverified = run_ctx.validate_completion()
        self.assertFalse(is_complete)
        self.assertEqual(len(unverified), 1)
        self.assertEqual(unverified[0].label, "Required Security Code")

if __name__ == "__main__":
    unittest.main()
