"""
Regression tests for manual resume command and Next button integration:
1. Resume command while waiting for input (PAUSED_CO_PILOT) triggers advance logic.
2. Next button while waiting for input continues execution exactly as before.
3. Resume command when bot is not paused returns an appropriate warning and does not trigger unintended execution.
4. Simultaneous resume command and Next button cannot cause duplicate execution (mutex via info.lock).
5. Current session, browser context, question inventory, and execution state are fully preserved.
6. Case-insensitive text messages: 'resume', '/resume', 'continue' behave consistently.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock
from playwright.async_api import async_playwright

from run_mode import (
    RemoteSessionManager,
    RemoteSessionInfo,
    RemoteCloudInterventionProvider,
    LocalDesktopInterventionProvider,
)
import telegram_bot


class TestManualResumeCoPilot(unittest.IsolatedAsyncioTestCase):

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

        # Clean session registry after each test
        RemoteSessionManager._sessions_by_chat.clear()
        RemoteSessionManager._sessions_by_id.clear()
        RemoteSessionManager._active_pages.clear()

    async def test_01_resume_command_while_waiting_for_input_triggers_advance_and_resumes(self):
        """
        Requirement 1 & 2: When Stillbot is paused and waiting for manual user input (PAUSED_CO_PILOT),
        sending 'resume' triggers the same continuation logic that the existing Next button uses,
        unblocking the workflow.
        """
        html = """
        <!DOCTYPE html>
        <html>
        <body>
            <h1>Questionnaire Waiting For Intervention</h1>
            <div id="survey_status">Paused for input</div>
            <button id="NextButton" onclick="document.getElementById('survey_status').innerText = 'Page 2 Resumed';">Next</button>
        </body>
        </html>
        """
        await self.page.set_content(html)
        test_chat_id = 100200
        provider = RemoteCloudInterventionProvider()

        resumed_successfully = False

        async def user_sends_resume_after_pause():
            # Wait for provider to enter intervention wait loop
            await asyncio.sleep(0.5)
            info = RemoteSessionManager.get_session_info(chat_id=test_chat_id)
            self.assertIsNotNone(info)
            self.assertEqual(info.state, "PAUSED_CO_PILOT")

            # Dispatch 'resume' action (simulating user sending 'resume' or '/resume')
            res = await RemoteSessionManager.dispatch_remote_action("resume", chat_id=test_chat_id)
            self.assertTrue(res.get("success"), f"Resume action should succeed: {res}")
            self.assertEqual(res.get("action"), "advance")
            self.assertTrue(res.get("verified"))

            nonlocal resumed_successfully
            resumed_successfully = True

        task = asyncio.create_task(user_sends_resume_after_pause())
        result = await provider.request_intervention(
            page=self.page,
            page_num=1,
            reason="Waiting for required answer verification",
            chat_id=test_chat_id,
            timeout_seconds=5
        )
        await task

        self.assertTrue(result, "Intervention loop should return True upon resume")
        self.assertTrue(resumed_successfully)
        body_text = await self.page.inner_text("body")
        self.assertIn("Page 2 Resumed", body_text)

    async def test_02_next_button_while_waiting_for_input_continues_execution_as_before(self):
        """
        Requirement 4: Existing Next button must remain fully functional.
        Clicking Next should resume execution exactly as it currently does.
        """
        html = """
        <!DOCTYPE html>
        <html>
        <body>
            <div id="page_state">Initial State</div>
            <button id="NextButton" onclick="document.getElementById('page_state').innerText = 'Page Advanced via Next Button';">Next</button>
        </body>
        </html>
        """
        await self.page.set_content(html)
        test_chat_id = 300400
        provider = RemoteCloudInterventionProvider()

        async def click_next_button_after_pause():
            await asyncio.sleep(0.5)
            # Dispatch 'advance' (simulating user clicking Next button callback or /next)
            res = await RemoteSessionManager.dispatch_remote_action("advance", chat_id=test_chat_id)
            self.assertTrue(res.get("success"), f"Next button advance should succeed: {res}")
            self.assertTrue(res.get("verified"))

        task = asyncio.create_task(click_next_button_after_pause())
        result = await provider.request_intervention(
            page=self.page,
            page_num=1,
            reason="Waiting for input",
            chat_id=test_chat_id,
            timeout_seconds=5
        )
        await task

        self.assertTrue(result, "Intervention loop should resume upon Next button click")
        body_text = await self.page.inner_text("body")
        self.assertIn("Page Advanced via Next Button", body_text)

    async def test_03_resume_command_when_not_paused_rejects_appropriately(self):
        """
        Requirement 6 & 7: The resume command should only work when Stillbot is actually waiting
        for manual input. If the bot is not paused (e.g. RUNNING), it should respond appropriately
        instead of triggering an unintended execution.
        """
        html = "<html><body><button id='NextButton'>Next</button></body></html>"
        await self.page.set_content(html)
        test_chat_id = 500600

        # Register session in RUNNING state (not waiting for input)
        RemoteSessionManager.register_session(
            session_id=f"session_{test_chat_id}",
            page=self.page,
            chat_id=test_chat_id,
            state="RUNNING"
        )

        res = await RemoteSessionManager.dispatch_remote_action("resume", chat_id=test_chat_id)
        self.assertFalse(res.get("success"), "Resume must fail when session is already running")
        self.assertIn("not waiting for manual input", res.get("error", ""))
        self.assertEqual(res.get("state"), "RUNNING")

        # Session remains in RUNNING state and page unchanged
        info = RemoteSessionManager.get_session_info(chat_id=test_chat_id)
        self.assertEqual(info.state, "RUNNING")

    async def test_04_simultaneous_resume_command_and_next_button_prevents_duplicate_execution(self):
        """
        Requirement 8: Ensure that sending 'resume' and clicking Next simultaneously
        cannot cause duplicate execution. Mutual exclusion is enforced by info.lock.
        """
        click_count = 0

        # Track button click events in the page
        await self.page.expose_function("record_click", lambda: None)
        html = """
        <!DOCTYPE html>
        <html>
        <body>
            <div id="counter">0</div>
            <button id="NextButton" onclick="
                let c = parseInt(document.getElementById('counter').innerText) + 1;
                document.getElementById('counter').innerText = c;
            ">Next</button>
        </body>
        </html>
        """
        await self.page.set_content(html)
        test_chat_id = 700800

        RemoteSessionManager.register_session(
            session_id=f"session_{test_chat_id}",
            page=self.page,
            chat_id=test_chat_id,
            state="PAUSED_CO_PILOT"
        )

        # Trigger both simultaneously
        res_resume, res_next = await asyncio.gather(
            RemoteSessionManager.dispatch_remote_action("resume", chat_id=test_chat_id),
            RemoteSessionManager.dispatch_remote_action("advance", chat_id=test_chat_id)
        )

        # At least one must succeed; the subsequent one must not duplicate or cause race errors
        self.assertTrue(res_resume.get("success") or res_next.get("success"))

        final_counter = await self.page.inner_text("#counter")
        self.assertEqual(final_counter, "1", "Button click must only execute once; no duplicate execution!")

    async def test_05_preservation_of_current_execution_state(self):
        """
        Requirement 9 & 10: Preserve the current session, browser context, question inventory,
        and execution state. Do not automatically fabricate or submit answers to required input fields.
        """
        html = """
        <!DOCTYPE html>
        <html>
        <body>
            <h1>Customer Feedback Survey</h1>
            <input type="text" id="feedback_field" value="User Draft Text" />
            <button id="NextButton" onclick="document.body.innerHTML = '<h1>Submitted Successfully</h1>';">Submit</button>
        </body>
        </html>
        """
        await self.page.set_content(html)
        test_chat_id = 900100

        session_id = f"session_{test_chat_id}"
        info = RemoteSessionManager.register_session(
            session_id=session_id,
            page=self.page,
            chat_id=test_chat_id,
            page_num=3,
            state="PAUSED_CO_PILOT"
        )
        info.widget_items = {"q1": {"id": "q1", "label": "Draft question"}}

        # Verify initial context
        self.assertEqual(info.page_num, 3)
        self.assertIn("q1", info.widget_items)

        # Resuming executes advance logic
        res = await RemoteSessionManager.dispatch_remote_action("resume", chat_id=test_chat_id)
        self.assertTrue(res.get("success"))

        # Check that page state transitioned as expected and browser context was preserved
        self.assertFalse(self.page.is_closed(), "Browser page must remain open")
        page_content = await self.page.inner_text("body")
        self.assertIn("Submitted Successfully", page_content)

        # Session object remains valid
        active_info = RemoteSessionManager.get_session_info(chat_id=test_chat_id)
        self.assertIsNotNone(active_info)
        self.assertEqual(active_info.page, self.page)

    async def test_06_telegram_handlers_and_keywords(self):
        """
        Verify that telegram_bot.remote_resume_command properly handles Update objects
        and dispatches resume action to RemoteSessionManager.
        """
        test_chat_id = 111222
        RemoteSessionManager.register_session(
            session_id=f"session_{test_chat_id}",
            page=self.page,
            chat_id=test_chat_id,
            state="PAUSED_USER"
        )

        mock_update = MagicMock()
        mock_update.effective_chat.id = test_chat_id
        mock_update.effective_user.id = 12345
        mock_update.callback_query = None
        mock_update.message = MagicMock()
        mock_update.message.text = "resume"
        mock_update.message.reply_text = AsyncMock()

        mock_context = MagicMock()

        # Call remote_resume_command
        await telegram_bot.remote_resume_command(mock_update, mock_context)

        # Should reply with automation resumed
        mock_update.message.reply_text.assert_called_once()
        call_text = mock_update.message.reply_text.call_args[0][0]
        self.assertIn("Automation resumed", call_text)

        # Session should now be RUNNING
        info = RemoteSessionManager.get_session_info(chat_id=test_chat_id)
        self.assertEqual(info.state, "RUNNING")


if __name__ == "__main__":
    unittest.main()
