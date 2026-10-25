"""
Regression and End-to-End lifecycle tests for Stillbot Cloud Browser Session Lifecycle:
1. Session registration before Co-Pilot prompt.
2. Shared session lookup via chat_id.
3. Reliable /snap returns screenshot bytes and metadata.
4. Reliable /next clicks advance button and verifies state transition.
5. Reliable /fill identifies active/required input, fills it, and verifies input_value().
6. Session cleanup after completion or browser closure.
7. Clear error diagnostics for missing or expired sessions.
8. Authorization isolation between distinct Telegram chats.
9. Non-blocking Telegram concurrency architecture.
10. Preservation of Local Windows Desktop mode.
"""

import os
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from playwright.async_api import async_playwright

from run_mode import (
    RunMode,
    RemoteSessionManager,
    RemoteSessionInfo,
    RemoteCloudInterventionProvider,
    LocalDesktopInterventionProvider,
    get_intervention_provider,
    resolve_run_mode
)


class TestCloudCoPilotSessionLifecycle(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.context = await self.browser.new_context()
        self.page = await self.context.new_page()

    async def asyncTearDown(self):
        try:
            if not self.page.is_closed():
                await self.page.close()
        except Exception:
            pass
        try:
            await self.context.close()
        except Exception:
            pass
        try:
            await self.browser.close()
        except Exception:
            pass
        await self.playwright.stop()
        # Clean any remaining test sessions
        RemoteSessionManager._sessions_by_chat.clear()
        RemoteSessionManager._sessions_by_id.clear()
        RemoteSessionManager._active_pages.clear()

    async def test_01_session_registration_precedes_intervention_prompt(self):
        """Verify the active cloud browser session is registered in RemoteSessionManager before Co-Pilot message is dispatched."""
        html = """
        <!DOCTYPE html>
        <html>
        <body>
            <h1>Questionnaire Page 1</h1>
            <div id="survey_status">Paused for human help</div>
            <button id="NextButton" onclick="document.getElementById('survey_status').innerText = 'Page 2 Loaded';">Next</button>
        </body>
        </html>
        """
        await self.page.set_content(html)
        provider = RemoteCloudInterventionProvider()
        test_chat_id = 998877

        session_found_at_callback_time = False

        async def status_callback(msg: str, photo=None):
            nonlocal session_found_at_callback_time
            # Check whether the session is already registered and accessible by chat_id
            info = RemoteSessionManager.get_session_info(chat_id=test_chat_id)
            if info is not None and info.page is self.page and info.state == "PAUSED_CO_PILOT":
                session_found_at_callback_time = True

        async def advance_after_delay():
            await asyncio.sleep(0.5)
            # Send /next command via RemoteSessionManager for this chat
            res = await RemoteSessionManager.dispatch_remote_action("advance", chat_id=test_chat_id)
            self.assertTrue(res.get("success"), f"Advance failed: {res}")

        adv_task = asyncio.create_task(advance_after_delay())
        res = await provider.request_intervention(
            page=self.page,
            page_num=1,
            reason="Uncertain survey question",
            status_callback=status_callback,
            timeout_seconds=5,
            chat_id=test_chat_id
        )
        await adv_task

        self.assertTrue(res, "Provider must resume after verified state change")
        self.assertTrue(session_found_at_callback_time, "Session MUST be registered and in PAUSED_CO_PILOT state when status message is sent")

    async def test_02_snap_returns_real_screenshot(self):
        """Verify /snap captures a genuine PNG screenshot from the active live browser session."""
        html = "<html><body style='background-color: rgb(240, 248, 255);'><h1>Stillbot Live Survey Test</h1></body></html>"
        await self.page.set_content(html)
        test_chat_id = 112233
        session_id = f"session_{test_chat_id}_1"

        RemoteSessionManager.register_session(session_id=session_id, page=self.page, chat_id=test_chat_id, page_num=1)

        res = await RemoteSessionManager.dispatch_remote_action("snapshot", chat_id=test_chat_id)
        self.assertTrue(res.get("success"), f"Snapshot dispatch failed: {res}")
        shot_bytes = res.get("screenshot")
        self.assertIsInstance(shot_bytes, bytes)
        # Check PNG Magic Header (\x89PNG\r\n\x1a\n)
        self.assertTrue(shot_bytes.startswith(b"\x89PNG\r\n\x1a\n"), "Captured screenshot must be valid PNG format")
        self.assertEqual(res.get("page_num"), 1)

    async def test_03_next_clicks_and_verifies_transition(self):
        """Verify /next clicks the Next button, polls for transition, and confirms page change."""
        html = """
        <html>
        <body>
            <div id="content">Page One</div>
            <button id="NextButton" onclick="document.getElementById('content').innerText = 'Page Two Loaded';">Next</button>
        </body>
        </html>
        """
        await self.page.set_content(html)
        test_chat_id = 445566
        session_id = f"session_{test_chat_id}_1"
        RemoteSessionManager.register_session(session_id=session_id, page=self.page, chat_id=test_chat_id, page_num=1)

        res = await RemoteSessionManager.dispatch_remote_action("advance", chat_id=test_chat_id)
        self.assertTrue(res.get("success"), f"Advance failed: {res}")
        self.assertTrue(res.get("verified"), "Advance must explicitly verify the transition")
        body_text = await self.page.inner_text("body")
        self.assertIn("Page Two Loaded", body_text)

    async def test_04_next_reports_validation_error_without_claiming_false_success(self):
        """Verify /next does not claim success if a validation error prevents the page from advancing."""
        html = """
        <html>
        <body>
            <div class="error" style="display:none;" id="err">This question requires an answer.</div>
            <button id="NextButton" onclick="document.getElementById('err').style.display = 'block';">Next</button>
        </body>
        </html>
        """
        await self.page.set_content(html)
        test_chat_id = 778899
        session_id = f"session_{test_chat_id}_1"
        RemoteSessionManager.register_session(session_id=session_id, page=self.page, chat_id=test_chat_id, page_num=1)

        res = await RemoteSessionManager.dispatch_remote_action("advance", chat_id=test_chat_id)
        self.assertFalse(res.get("success"), "Must NOT claim success when validation error occurs")
        self.assertIn("validation error", res.get("error", "").lower())

    async def test_05_fill_identifies_active_required_field_and_confirms(self):
        """Verify /fill identifies the required empty field, fills it, verifies DOM input_value, and reports confirmation."""
        html = """
        <html>
        <body>
            <label for="firstName">First Name (Optional):</label>
            <input type="text" id="firstName" value="John" />
            <br/>
            <label for="age">Your Age (Required):</label>
            <input type="text" id="age" required aria-required="true" value="" placeholder="Enter your age" />
        </body>
        </html>
        """
        await self.page.set_content(html)
        test_chat_id = 334455
        session_id = f"session_{test_chat_id}_1"
        RemoteSessionManager.register_session(session_id=session_id, page=self.page, chat_id=test_chat_id, page_num=1)

        res = await RemoteSessionManager.dispatch_remote_action("fill", payload={"value": "32"}, chat_id=test_chat_id)
        self.assertTrue(res.get("success"), f"Fill failed: {res}")
        self.assertTrue(res.get("verified"))

        # Verify DOM value
        actual_val = await self.page.input_value("#age")
        self.assertEqual(actual_val, "32", "The required empty age field must be filled with 32")
        # Ensure optional already-filled field was NOT overwritten
        first_name_val = await self.page.input_value("#firstName")
        self.assertEqual(first_name_val, "John")

    async def test_06_session_isolation_prevents_unauthorized_chat_access(self):
        """Verify user B cannot control or snapshot user A's survey session."""
        html = "<html><body><h1>User A Confidential Survey</h1></body></html>"
        await self.page.set_content(html)
        chat_user_a = 1001
        chat_user_b = 2002

        RemoteSessionManager.register_session(
            session_id="session_user_a",
            page=self.page,
            chat_id=chat_user_a,
            user_id="alice"
        )

        # User B attempts to snapshot User A's session
        res_b_snap = await RemoteSessionManager.dispatch_remote_action("snapshot", chat_id=chat_user_b)
        self.assertFalse(res_b_snap.get("success"))
        self.assertIn("No active remote browser session found", res_b_snap.get("error"))

        # User B attempts to advance User A's session
        res_b_adv = await RemoteSessionManager.dispatch_remote_action("advance", chat_id=chat_user_b)
        self.assertFalse(res_b_adv.get("success"))
        self.assertIn("No active remote browser session found", res_b_adv.get("error"))

        # User A succeeds
        res_a_snap = await RemoteSessionManager.dispatch_remote_action("snapshot", chat_id=chat_user_a)
        self.assertTrue(res_a_snap.get("success"))

    async def test_07_session_cleanup_after_completion(self):
        """Verify unregistering a session prevents subsequent actions with a clear diagnostic message."""
        test_chat_id = 556677
        session_id = f"session_{test_chat_id}"
        RemoteSessionManager.register_session(session_id=session_id, page=self.page, chat_id=test_chat_id)

        # Unregister (simulating survey completion or browser closure)
        RemoteSessionManager.unregister_session(session_id=session_id, chat_id=test_chat_id, reason="TEST_COMPLETE")

        res = await RemoteSessionManager.dispatch_remote_action("advance", chat_id=test_chat_id)
        self.assertFalse(res.get("success"))
        self.assertIn("No active remote browser session found", res.get("error"))

    async def test_08_closed_browser_automatically_detected_and_reported(self):
        """Verify closed browser pages are detected and reported clearly without crashing."""
        test_chat_id = 991122
        session_id = f"session_{test_chat_id}"
        RemoteSessionManager.register_session(session_id=session_id, page=self.page, chat_id=test_chat_id)

        # Close the page
        await self.page.close()

        res = await RemoteSessionManager.dispatch_remote_action("snapshot", chat_id=test_chat_id)
        self.assertFalse(res.get("success"))
        self.assertIn("closed or expired", res.get("error"))

    async def test_09_local_windows_mode_workflow_preserved(self):
        """Verify Local PC Mode continues to use direct desktop interaction and prompts user on screen."""
        html = """
        <html>
        <body>
            <div id="status">Page 1</div>
            <button id="next-btn" onclick="document.getElementById('status').innerText = 'Page 2';">Next</button>
        </body>
        </html>
        """
        await self.page.set_content(html)
        provider = LocalDesktopInterventionProvider()

        captured_msgs = []
        async def mock_callback(msg: str):
            captured_msgs.append(msg)

        async def user_clicks_next():
            await asyncio.sleep(0.4)
            await self.page.click("#next-btn")

        click_task = asyncio.create_task(user_clicks_next())
        res = await provider.request_intervention(
            page=self.page,
            page_num=1,
            reason="Local review",
            status_callback=mock_callback,
            timeout_seconds=4
        )
        await click_task

        self.assertTrue(res, "Local provider must resume upon local DOM transition")
        self.assertTrue(len(captured_msgs) >= 1)
        self.assertIn("open Chrome browser", captured_msgs[0])


if __name__ == "__main__":
    unittest.main()
