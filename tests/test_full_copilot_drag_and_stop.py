import os
import sys
import asyncio
import tempfile
import shutil
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
from interaction_engine import (
    RankingHandler,
    WidgetType,
    StrategyType,
    universal_registry,
)

HTML_RANKING_LIST = """
<!DOCTYPE html>
<html>
<head>
    <style>
        .rank-item { padding: 10px; margin: 4px; border: 1px solid #ccc; background: #fafafa; cursor: grab; }
        .dropzone { min-height: 80px; border: 2px dashed #999; margin: 10px 0; padding: 10px; }
    </style>
</head>
<body>
    <h3>Rank your top preferences</h3>
    <ul id="rankable-list" class="ui-sortable" role="list">
        <li class="rank-item" id="item-apple" draggable="true" role="option" tabindex="0" data-value="Apple">Apple</li>
        <li class="rank-item" id="item-banana" draggable="true" role="option" tabindex="0" data-value="Banana">Banana</li>
        <li class="rank-item" id="item-orange" draggable="true" role="option" tabindex="0" data-value="Orange">Orange</li>
    </ul>

    <h3>Dual Bucket Classification</h3>
    <div id="source-pool" class="dropzone" data-bucket="source">
        <div class="drag-card" id="card-1" draggable="true" data-label="Task 1">Task 1</div>
        <div class="drag-card" id="card-2" draggable="true" data-label="Task 2">Task 2</div>
    </div>

    <h4>Priority Bucket</h4>
    <div id="target-bucket" class="dropzone target-bucket" data-target="target_priority" data-bucket="priority">
    </div>

    <button id="btn-next" onclick="onNextClick()">Next Question</button>
    <div id="page-status">Page 1</div>

    <script>
        function onNextClick() {
            document.getElementById('page-status').innerText = 'Page 2';
        }

        // HTML5 drag/drop implementation
        let draggedId = null;
        document.querySelectorAll('.rank-item, .drag-card').forEach(el => {
            el.addEventListener('dragstart', (e) => {
                draggedId = el.id;
                e.dataTransfer.setData('text/plain', el.id);
            });
        });

        document.querySelectorAll('.dropzone').forEach(dz => {
            dz.addEventListener('dragover', (e) => e.preventDefault());
            dz.addEventListener('drop', (e) => {
                e.preventDefault();
                const id = e.dataTransfer.getData('text/plain') || draggedId;
                if (id) {
                    const el = document.getElementById(id);
                    if (el) dz.appendChild(el);
                }
            });
        });
    </script>
</body>
</html>
"""

class TestFullCopilotDragAndStop(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.page = await self.browser.new_page()
        await self.page.set_content(HTML_RANKING_LIST)
        self.temp_dir = tempfile.mkdtemp(prefix="test_session_")

    async def asyncTearDown(self):
        try:
            if not self.page.is_closed():
                await self.page.close()
            if hasattr(self.browser, "is_connected") and self.browser.is_connected():
                await self.browser.close()
            await self.playwright.stop()
        except Exception:
            pass
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)
        RemoteSessionManager._sessions_by_chat.clear()
        RemoteSessionManager._sessions_by_id.clear()
        RemoteSessionManager._active_pages.clear()

    async def test_01_ranking_reorder_and_bucket_drag(self):
        """1. Verify ranking reordering and destination-based drag-and-drop with inspection & verify."""
        handler = RankingHandler()

        # A. Inspect interactive items & drop targets
        discovered = await handler.inspect_interactive_items(self.page)
        self.assertGreater(len(discovered["items"]), 0)
        self.assertGreater(len(discovered["targets"]), 0)

        # Verify stable item keys
        item_keys = [it["id"] for it in discovered["items"]]
        self.assertIn("1", item_keys)

        # B. Test plan_interaction returns multi-strategy list
        inspection = await handler.inspect(self.page, {
            "type": "ranking",
            "id": "rankable-list",
            "options": ["Apple", "Banana", "Orange"]
        })
        strategies = handler.plan_interaction(inspection, ["Banana", "Apple"], [])
        self.assertIn(StrategyType.LOCATOR_DRAG, strategies)
        self.assertIn(StrategyType.POINTER_GESTURE, strategies)
        self.assertIn(StrategyType.HTML5_SYNTHETIC_DRAG, strategies)
        self.assertIn(StrategyType.KEYBOARD_REORDER, strategies)

        # C. Execute interaction strategy
        attempt = await handler.execute(self.page, inspection, StrategyType.LOCATOR_DRAG, ["Banana", "Apple", "Orange"])
        self.assertTrue(attempt.success)

        # D. Verify ranking verification logic
        ver = await handler.verify(self.page, inspection, ["Apple", "Banana"])
        self.assertTrue(ver.is_verified)

    async def test_02_auto_drag_failure_triggers_copilot(self):
        """2. When auto-drag fails verification, records failure and allows Co-Pilot intervention."""
        test_chat_id = 10101
        info = RemoteSessionManager.register_session(
            session_id=f"session_{test_chat_id}",
            chat_id=test_chat_id,
            page=self.page,
            browser=self.browser,
            user_id=111,
            temp_dir=self.temp_dir
        )

        # Simulate widget failure recorded
        info.active_widget = {
            "type": "ranking",
            "field": "Task Preference",
            "failure_reason": "No valid drop target matched automatic heuristic"
        }
        info.state = "WAITING_FOR_HUMAN"

        # Check status reflects the failed widget needing assistance
        status_res = await RemoteSessionManager.dispatch_remote_action("status", chat_id=test_chat_id)
        self.assertTrue(status_res["success"])
        self.assertEqual(status_res["state"], "WAITING_FOR_HUMAN")
        self.assertEqual(status_res["active_widget"]["type"], "ranking")
        self.assertIn("heuristic", status_res["active_widget"]["failure_reason"])

    async def test_03_remote_items_drag_move_click_snap_next(self):
        """3. Test /items, /drag, /move, /click, /snap, and /next remote actions."""
        test_chat_id = 20202
        RemoteSessionManager.register_session(
            session_id=f"session_{test_chat_id}",
            chat_id=test_chat_id,
            page=self.page,
            browser=self.browser,
            user_id=222,
            temp_dir=self.temp_dir
        )

        # /items
        items_res = await RemoteSessionManager.dispatch_remote_action("items", chat_id=test_chat_id)
        self.assertTrue(items_res["success"])
        self.assertGreater(len(items_res["items"]), 0)
        self.assertIn("Draggable Items:", items_res["formatted_text"])

        # /snap
        snap_res = await RemoteSessionManager.dispatch_remote_action("snapshot", chat_id=test_chat_id)
        self.assertTrue(snap_res["success"])
        self.assertTrue(isinstance(snap_res["screenshot"], bytes))

        # /click
        click_res = await RemoteSessionManager.dispatch_remote_action(
            "click",
            payload={"target_id": "1"},
            chat_id=test_chat_id
        )
        self.assertTrue(click_res["success"])

        # /next
        next_res = await RemoteSessionManager.dispatch_remote_action("advance", chat_id=test_chat_id)
        self.assertTrue(next_res["success"])
        page_status = await self.page.inner_text("#page-status")
        self.assertEqual(page_status, "Page 2")

    async def test_04_verification_failure_reporting(self):
        """4. Invalid identifiers and missing targets report clear errors instead of failing silently."""
        test_chat_id = 30303
        RemoteSessionManager.register_session(
            session_id=f"session_{test_chat_id}",
            chat_id=test_chat_id,
            page=self.page,
            browser=self.browser,
            user_id=333,
            temp_dir=self.temp_dir
        )

        # Non-existent item
        drag_err = await RemoteSessionManager.dispatch_remote_action(
            "drag",
            payload={"item_id": "999", "target_id": "target_1"},
            chat_id=test_chat_id
        )
        self.assertFalse(drag_err["success"])
        self.assertIn("999", drag_err["error"])

        # Non-existent target
        drag_err2 = await RemoteSessionManager.dispatch_remote_action(
            "drag",
            payload={"item_id": "1", "target_id": "fake_target_999"},
            chat_id=test_chat_id
        )
        self.assertFalse(drag_err2["success"])
        self.assertIn("fake_target_999", drag_err2["error"])

    async def test_05_pause_and_resume(self):
        """5. /pause pauses automated execution while retaining session; /resume unpauses."""
        test_chat_id = 40404
        info = RemoteSessionManager.register_session(
            session_id=f"session_{test_chat_id}",
            chat_id=test_chat_id,
            page=self.page,
            browser=self.browser,
            user_id=444,
            temp_dir=self.temp_dir
        )

        self.assertTrue(info.pause_event.is_set())
        self.assertFalse(info.is_paused())

        # /pause
        pause_res = await RemoteSessionManager.dispatch_remote_action("pause", chat_id=test_chat_id)
        self.assertTrue(pause_res["success"])
        self.assertTrue(info.is_paused())
        self.assertFalse(info.pause_event.is_set())

        # /resume
        resume_res = await RemoteSessionManager.dispatch_remote_action("resume", chat_id=test_chat_id)
        self.assertTrue(resume_res["success"])
        self.assertFalse(info.is_paused())
        self.assertTrue(info.pause_event.is_set())

    async def test_06_emergency_stop_lifecycle(self):
        """6. /stop transitions to STOPPING, cancels tasks, closes browser, and marks TERMINATED."""
        test_chat_id = 50505
        dummy_task = asyncio.create_task(asyncio.sleep(10))

        info = RemoteSessionManager.register_session(
            session_id=f"session_{test_chat_id}",
            chat_id=test_chat_id,
            page=self.page,
            browser=self.browser,
            task=dummy_task,
            user_id=555,
            temp_dir=self.temp_dir
        )

        # Call terminate_session
        term_res = await RemoteSessionManager.terminate_session(chat_id=test_chat_id, user_id=555, reason="User stop")
        self.assertTrue(term_res["success"])
        self.assertEqual(info.state, "TERMINATED")
        self.assertTrue(dummy_task.cancelled())
        self.assertTrue(self.page.is_closed())
        self.assertFalse(self.browser.is_connected())

        # Session should no longer be active in registry
        self.assertIsNone(RemoteSessionManager.get_session(test_chat_id))

    async def test_07_repeated_stop_and_queued_commands(self):
        """7. Repeated /stop is safe and idempotent; commands after stop are rejected."""
        test_chat_id = 60606
        info = RemoteSessionManager.register_session(
            session_id=f"session_{test_chat_id}",
            chat_id=test_chat_id,
            page=self.page,
            browser=self.browser,
            user_id=666,
            temp_dir=self.temp_dir
        )

        # First stop
        r1 = await RemoteSessionManager.terminate_session(chat_id=test_chat_id)
        self.assertTrue(r1["success"])

        # Second stop (idempotent)
        r2 = await RemoteSessionManager.terminate_session(chat_id=test_chat_id)
        self.assertTrue(r2["success"])
        self.assertIn("No active survey session found", r2["message"])

        # Queued command after cancellation is rejected
        action_res = await RemoteSessionManager.dispatch_remote_action("advance", chat_id=test_chat_id)
        self.assertFalse(action_res["success"])
        self.assertIn("No active remote browser session found", action_res["error"])

    async def test_08_per_user_isolation(self):
        """8. Never let User A's /stop cancel User B's active run."""
        chat_a = 70701
        chat_b = 70702

        browser_b = await self.playwright.chromium.launch(headless=True)
        page_b = await browser_b.new_page()
        await page_b.set_content("<html><body>User B Page</body></html>")

        info_a = RemoteSessionManager.register_session(
            session_id=f"session_{chat_a}",
            chat_id=chat_a,
            page=self.page,
            browser=self.browser,
            user_id=701
        )
        info_b = RemoteSessionManager.register_session(
            session_id=f"session_{chat_b}",
            chat_id=chat_b,
            page=page_b,
            browser=browser_b,
            user_id=702
        )

        # User A triggers stop
        term_a = await RemoteSessionManager.terminate_session(chat_id=chat_a, user_id=701)
        self.assertTrue(term_a["success"])

        # User A's session is terminated
        self.assertEqual(info_a.state, "TERMINATED")
        self.assertIsNone(RemoteSessionManager.get_session(chat_a))

        # User B's session remains completely active and untouched
        self.assertEqual(info_b.state, "RUNNING")
        self.assertIsNotNone(RemoteSessionManager.get_session(chat_b))
        self.assertFalse(page_b.is_closed())
        self.assertTrue(browser_b.is_connected())

        await page_b.close()
        await browser_b.close()

    async def test_09_session_replacement_guard(self):
        """9. Cleanup of an older session cannot remove a newer session with a different token."""
        chat_id = 80808
        old_info = RemoteSessionManager.register_session(session_id=f"session_{chat_id}_old", chat_id=chat_id, page=self.page, user_id=888)
        old_token = old_info.registration_token

        # A new session is registered for the same chat
        new_page = await self.browser.new_page()
        new_info = RemoteSessionManager.register_session(session_id=f"session_{chat_id}_new", chat_id=chat_id, page=new_page, user_id=888)
        self.assertNotEqual(old_token, new_info.registration_token)

        # Attempt to unregister with the old token
        removed = RemoteSessionManager.unregister_session(chat_id, registration_token=old_token)
        self.assertFalse(removed)

        # Active session is still new_info!
        current = RemoteSessionManager.get_session(chat_id)
        self.assertIs(current, new_info)

        await new_page.close()

    async def test_10_local_mode_compatibility(self):
        """10. Local desktop intervention provider remains fully functional and preserves local workflow."""
        provider = LocalDesktopInterventionProvider()

        captured_msgs = []
        async def mock_callback(msg: str):
            captured_msgs.append(msg)

        async def user_clicks_next():
            await asyncio.sleep(0.3)
            await self.page.click("#btn-next")

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
