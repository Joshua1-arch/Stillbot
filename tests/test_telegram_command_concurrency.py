"""
End-to-End integration test for Telegram bot Co-Pilot commands:
Verifies that remote_snap_command, remote_next_command, and remote_fill_command
interact with active Playwright sessions concurrently without blocking Telegram polling.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock
from playwright.async_api import async_playwright

from run_mode import RemoteSessionManager
from telegram_bot import (
    remote_next_command,
    remote_snap_command,
    remote_fill_command,
    ACTIVE_SURVEY_TASKS,
    cancel
)


class TestTelegramCommandConcurrency(unittest.IsolatedAsyncioTestCase):

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
        RemoteSessionManager._sessions_by_chat.clear()
        RemoteSessionManager._sessions_by_id.clear()
        ACTIVE_SURVEY_TASKS.clear()

    async def test_01_telegram_commands_end_to_end(self):
        """Verify Telegram command handlers interact with the live page and reply back properly."""
        html = """
        <html>
        <body>
            <label for="favColor">Favorite Color:</label>
            <input type="text" id="favColor" value="" placeholder="Color" required />
            <button id="NextButton" onclick="document.body.innerHTML = '<h1>Survey Submitted</h1>';">Next</button>
        </body>
        </html>
        """
        await self.page.set_content(html)
        test_chat_id = 887766

        # Register live session
        RemoteSessionManager.register_session(
            session_id=f"session_{test_chat_id}",
            page=self.page,
            chat_id=test_chat_id,
            page_num=1
        )

        # Mock Telegram Update & Context for /fill
        update_fill = MagicMock()
        update_fill.effective_chat.id = test_chat_id
        update_fill.effective_user.id = 1234
        update_fill.message.reply_text = AsyncMock()

        ctx_fill = MagicMock()
        ctx_fill.args = ["Blue"]

        # 1. Execute /fill Blue
        await remote_fill_command(update_fill, ctx_fill)
        update_fill.message.reply_text.assert_called_once()
        reply_fill = update_fill.message.reply_text.call_args[0][0]
        self.assertIn("Blue", reply_fill)
        self.assertIn("Successfully filled", reply_fill)

        # Verify DOM
        val = await self.page.input_value("#favColor")
        self.assertEqual(val, "Blue")

        # 2. Execute /snap
        update_snap = MagicMock()
        update_snap.effective_chat.id = test_chat_id
        update_snap.effective_user.id = 1234
        update_snap.message.reply_photo = AsyncMock()
        update_snap.message.reply_text = AsyncMock()

        ctx_snap = MagicMock()
        await remote_snap_command(update_snap, ctx_snap)
        update_snap.message.reply_photo.assert_called_once()
        call_kwargs = update_snap.message.reply_photo.call_args[1]
        self.assertIn("photo", call_kwargs)
        self.assertTrue(isinstance(call_kwargs["photo"], bytes))
        self.assertTrue(call_kwargs["photo"].startswith(b"\x89PNG\r\n\x1a\n"))

        # 3. Execute /next
        update_next = MagicMock()
        update_next.effective_chat.id = test_chat_id
        update_next.effective_user.id = 1234
        update_next.message.reply_text = AsyncMock()

        ctx_next = MagicMock()
        await remote_next_command(update_next, ctx_next)
        update_next.message.reply_text.assert_called_once()
        reply_next = update_next.message.reply_text.call_args[0][0]
        self.assertIn("State transition verified", reply_next)

        body_text = await self.page.inner_text("body")
        self.assertIn("Survey Submitted", body_text)

    async def test_02_commands_execute_during_running_survey_without_deadlock(self):
        """Verify that while a background survey task is running, Telegram commands are processed immediately."""
        test_chat_id = 999111

        # Simulate background survey loop that pauses on Co-Pilot
        survey_active = True
        async def fake_survey_job():
            while survey_active:
                await asyncio.sleep(0.05)

        task = asyncio.create_task(fake_survey_job())
        ACTIVE_SURVEY_TASKS[test_chat_id] = task

        # Register session
        RemoteSessionManager.register_session(
            session_id=f"session_{test_chat_id}",
            page=self.page,
            chat_id=test_chat_id,
            page_num=1
        )

        update_snap = MagicMock()
        update_snap.effective_chat.id = test_chat_id
        update_snap.effective_user.id = 555
        update_snap.message.reply_photo = AsyncMock()

        # Command should execute in < 2 seconds while background survey task is running
        await asyncio.wait_for(remote_snap_command(update_snap, MagicMock()), timeout=3.0)
        update_snap.message.reply_photo.assert_called_once()

        # Stop background task
        survey_active = False
        await task


if __name__ == "__main__":
    unittest.main()
