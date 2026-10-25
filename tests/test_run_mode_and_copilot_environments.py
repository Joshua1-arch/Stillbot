"""
Regression tests for Stillbot Dual Execution Environments:
Local PC Mode vs. Render Cloud Mode.
"""

import os
import unittest
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from playwright.async_api import async_playwright

from run_mode import (
    RunMode,
    resolve_run_mode,
    validate_and_log_run_mode,
    has_graphical_display,
    is_cloud_environment,
    LocalDesktopInterventionProvider,
    RemoteCloudInterventionProvider,
    RemoteSessionManager,
    get_intervention_provider
)


class TestRunModeAndCoPilotEnvironments(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.context = await self.browser.new_context()
        self.page = await self.context.new_page()

    async def asyncTearDown(self):
        await self.context.close()
        await self.browser.close()
        await self.playwright.stop()

    def test_01_auto_mode_environment_resolution(self):
        """Test Auto mode resolves cloud on Render/headless and local on desktop with display."""
        # 1. On Render cloud
        render_env = {"STILLBOT_RUN_MODE": "auto", "RENDER": "true", "RENDER_SERVICE_ID": "srv-123"}
        self.assertEqual(resolve_run_mode(render_env), RunMode.CLOUD.value)

        # 2. On Linux headless without display
        with patch("os.name", "posix"):
            headless_linux = {"STILLBOT_RUN_MODE": "auto"}
            self.assertEqual(resolve_run_mode(headless_linux), RunMode.CLOUD.value)

            # Linux with display
            desktop_linux = {"STILLBOT_RUN_MODE": "auto", "DISPLAY": ":0"}
            self.assertEqual(resolve_run_mode(desktop_linux), RunMode.LOCAL.value)

            # Linux with Wayland
            wayland_linux = {"STILLBOT_RUN_MODE": "auto", "WAYLAND_DISPLAY": "wayland-0"}
            self.assertEqual(resolve_run_mode(wayland_linux), RunMode.LOCAL.value)

    def test_02_explicit_mode_overrides_automatic_detection(self):
        """Test explicit STILLBOT_RUN_MODE overrides environment variables."""
        # Force local even on Render
        forced_local = {"STILLBOT_RUN_MODE": "local", "RENDER": "true"}
        self.assertEqual(resolve_run_mode(forced_local), RunMode.LOCAL.value)

        # Force cloud even with display available
        forced_cloud = {"STILLBOT_RUN_MODE": "cloud", "DISPLAY": ":0"}
        self.assertEqual(resolve_run_mode(forced_cloud), RunMode.CLOUD.value)

    def test_03_provider_factory_selection(self):
        """Test get_intervention_provider returns correct provider instance."""
        local_provider = get_intervention_provider(RunMode.LOCAL.value)
        self.assertIsInstance(local_provider, LocalDesktopInterventionProvider)

        cloud_provider = get_intervention_provider(RunMode.CLOUD.value)
        self.assertIsInstance(cloud_provider, RemoteCloudInterventionProvider)

    async def test_04_local_mode_direct_interaction_prompt(self):
        """Test Local mode retains existing 'Look at your Chrome screen' prompt."""
        html = """
        <!DOCTYPE html>
        <html>
        <body>
            <h1>Questionnaire Page 1</h1>
            <div id="content">Please fill out form</div>
            <button id="next-btn" onclick="document.getElementById('content').innerText = 'Page 2 Loaded';">Next</button>
        </body>
        </html>
        """
        await self.page.set_content(html)
        provider = LocalDesktopInterventionProvider()

        captured_msgs = []
        async def mock_callback(msg: str):
            captured_msgs.append(msg)

        # Background task that advances page after 500ms
        async def advance_after_delay():
            await asyncio.sleep(0.5)
            await self.page.click("#next-btn")

        adv_task = asyncio.create_task(advance_after_delay())
        res = await provider.request_intervention(
            page=self.page,
            page_num=1,
            reason="Waiting for user review",
            status_callback=mock_callback,
            timeout_seconds=5
        )
        await adv_task

        self.assertTrue(res, "Local provider must resume after DOM transition")
        self.assertTrue(len(captured_msgs) >= 1)
        self.assertIn("open Chrome browser", captured_msgs[0])
        self.assertIn("AI credit spending is paused", captured_msgs[0])

    async def test_05_cloud_mode_never_mentions_local_chrome_screen(self):
        """Test Cloud mode never instructs user to look at a local Chrome screen and provides remote controls."""
        html = """
        <!DOCTYPE html>
        <html>
        <body>
            <h1>Cloud Survey Page 2</h1>
            <div id="status_div">Awaiting input</div>
            <button id="NextButton" onclick="document.getElementById('status_div').innerText = 'Advanced to Page 3';">Next</button>
        </body>
        </html>
        """
        await self.page.set_content(html)
        provider = RemoteCloudInterventionProvider()

        captured_msgs = []
        async def mock_callback(msg: str, photo=None):
            captured_msgs.append((msg, photo))

        # Advance after delay via remote session action
        async def remote_advance_task():
            await asyncio.sleep(0.5)
            await RemoteSessionManager.dispatch_remote_action("advance")

        adv_task = asyncio.create_task(remote_advance_task())
        res = await provider.request_intervention(
            page=self.page,
            page_num=2,
            reason="Unanswerable custom question",
            status_callback=mock_callback,
            timeout_seconds=5
        )
        await adv_task

        self.assertTrue(res, "Cloud provider must resume after remote action causes transition")
        self.assertTrue(len(captured_msgs) >= 1)
        notification_text = captured_msgs[0][0]

        # Must NOT mention local Chrome screen
        self.assertNotIn("Look at your Chrome screen", notification_text)
        self.assertNotIn("open Chrome browser", notification_text)

        # Must mention Cloud Co-Pilot, paused spend, and remote commands
        self.assertIn("Cloud Co-Pilot", notification_text)
        self.assertIn("PAUSED", notification_text)
        self.assertIn("/next", notification_text)
        self.assertIn("/fill", notification_text)
        self.assertIn("/snap", notification_text)

    async def test_06_remote_session_action_dispatch(self):
        """Test RemoteSessionManager executes remote actions on the server page."""
        html = """
        <!DOCTYPE html>
        <html>
        <body>
            <label for="feedback">Your Feedback:</label>
            <input type="text" id="feedback" />
            <button id="NextButton" onclick="document.body.innerHTML = '<h1>Survey Finished</h1>';">Next</button>
        </body>
        </html>
        """
        await self.page.set_content(html)
        session_id = "test_cloud_session"
        RemoteSessionManager.register_session(session_id, self.page)

        try:
            # 1. Remote Fill
            fill_res = await RemoteSessionManager.dispatch_remote_action("fill", {"selector": "#feedback", "value": "Great experience"})
            self.assertTrue(fill_res["success"])
            val = await self.page.input_value("#feedback")
            self.assertEqual(val, "Great experience")

            # 2. Remote Snapshot
            snap_res = await RemoteSessionManager.dispatch_remote_action("snapshot")
            self.assertTrue(snap_res["success"])
            self.assertIsNotNone(snap_res.get("screenshot"))

            # 3. Remote Advance
            adv_res = await RemoteSessionManager.dispatch_remote_action("advance")
            self.assertTrue(adv_res["success"])
            body_text = await self.page.inner_text("body")
            self.assertIn("Survey Finished", body_text)
        finally:
            RemoteSessionManager.unregister_session(session_id)

    async def test_07_each_mode_resumes_only_after_verified_state_change(self):
        """Test providers do not return prematurely without a verified DOM/URL transition."""
        html = "<html><body><div>Static Page - No Action</div></body></html>"
        await self.page.set_content(html)

        local_provider = LocalDesktopInterventionProvider()
        cloud_provider = RemoteCloudInterventionProvider()

        # Both must timeout and return False when no state transition occurs
        res_local = await local_provider.request_intervention(
            page=self.page,
            page_num=1,
            reason="Testing no-op timeout",
            timeout_seconds=2
        )
        self.assertFalse(res_local, "Local provider must not resume without verified transition")

        res_cloud = await cloud_provider.request_intervention(
            page=self.page,
            page_num=1,
            reason="Testing no-op timeout",
            timeout_seconds=2
        )
        self.assertFalse(res_cloud, "Cloud provider must not resume without verified transition")

    def test_08_validate_and_log_mode_sanitization(self):
        """Test validate_and_log_run_mode sanitizes output and never exposes tokens."""
        env = {
            "STILLBOT_RUN_MODE": "cloud",
            "TELEGRAM_BOT_TOKEN": "123456:SECRET_TOKEN_ABC",
            "OPENAI_API_KEY": "sk-secret-key-12345"
        }
        info = validate_and_log_run_mode(env)
        self.assertEqual(info["resolved_mode"], "cloud")
        # Ensure returned info dictionary does not contain sensitive keys
        self.assertNotIn("TELEGRAM_BOT_TOKEN", info)
        self.assertNotIn("OPENAI_API_KEY", info)
        self.assertNotIn("SECRET_TOKEN_ABC", str(info))


if __name__ == "__main__":
    unittest.main()
