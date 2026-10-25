import unittest
import asyncio
from playwright.async_api import async_playwright
from scraper import scrape_page
from interaction_engine import universal_registry, RankingHandler, StrategyType, WidgetType
from run_mode import RemoteSessionManager

HTML_QUALTRICS_MODERN_RANKING = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Qualtrics Modern Ranking Survey</title>
    <style>
        body { font-family: sans-serif; padding: 30px; }
        .QuestionOuter {
            border: 1px solid #d9534f;
            padding: 20px;
            border-radius: 6px;
            max-width: 650px;
        }
        .validation-error {
            color: #d9534f;
            font-size: 14px;
            margin-bottom: 12px;
            display: flex;
            align-items: center;
        }
        .question-text {
            font-weight: 600;
            font-size: 16px;
            margin-bottom: 20px;
        }
        .ranking-list {
            list-style: none;
            padding: 0;
            margin: 0;
            display: flex;
            flex-direction: column;
            gap: 12px;
        }
        .q-ranking-card {
            display: flex;
            align-items: center;
            justify-content: space-between;
            border: 1px solid #ccc;
            border-radius: 4px;
            padding: 10px 14px;
            background: #fff;
            user-select: none;
        }
        .card-left {
            display: flex;
            align-items: center;
            gap: 12px;
        }
        .rank-circle {
            width: 20px;
            height: 20px;
            border-radius: 50%;
            background: #bbb;
            display: inline-block;
        }
        .card-label {
            font-size: 14px;
            color: #333;
        }
        .drag-handle {
            cursor: grab;
            padding: 4px 8px;
            color: #888;
            font-size: 18px;
            letter-spacing: 2px;
        }
        .next-btn {
            margin-top: 25px;
            padding: 10px 22px;
            background: #0076d6;
            color: white;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-size: 15px;
        }
    </style>
</head>
<body>
    <div class="QuestionOuter" id="QID77">
        <div class="validation-error" id="valError">
            <span style="margin-right: 6px;">⊘</span> Response required
        </div>
        <div class="question-text">
            *Please rank your top three formats for reaching frontline workers specifically.
        </div>
        <div class="ranking-container">
            <ul class="ranking-list" role="list" id="rankUl">
                <li class="q-ranking-card" role="listitem" id="item_short_vid" data-choice-id="1">
                    <div class="card-left">
                        <span class="rank-circle"></span>
                        <span class="card-label">Short videos</span>
                    </div>
                    <div class="drag-handle" role="button" aria-label="reorder">⋮⋮</div>
                </li>
                <li class="q-ranking-card" role="listitem" id="item_interactive" data-choice-id="2">
                    <div class="card-left">
                        <span class="rank-circle"></span>
                        <span class="card-label">Interactive scenario-based modules</span>
                    </div>
                    <div class="drag-handle" role="button" aria-label="reorder">⋮⋮</div>
                </li>
                <li class="q-ranking-card" role="listitem" id="item_mobile_apps" data-choice-id="3">
                    <div class="card-left">
                        <span class="rank-circle"></span>
                        <span class="card-label">Mobile apps</span>
                    </div>
                    <div class="drag-handle" role="button" aria-label="reorder">⋮⋮</div>
                </li>
            </ul>
        </div>
    </div>
    <button class="next-btn" id="NextButton">Next page &gt;</button>

    <script>
        // Custom pointer interaction script simulating Qualtrics reordering logic
        let draggedCard = null;
        const list = document.getElementById('rankUl');
        const cards = document.querySelectorAll('.q-ranking-card');

        cards.forEach(card => {
            const handle = card.querySelector('.drag-handle');
            handle.addEventListener('mousedown', (e) => {
                draggedCard = card;
            });
            card.addEventListener('dragover', (e) => {
                e.preventDefault();
            });
            card.addEventListener('drop', (e) => {
                e.preventDefault();
                if (draggedCard && draggedCard !== card) {
                    list.insertBefore(draggedCard, card);
                    document.getElementById('valError').style.display = 'none';
                }
            });
        });

        // Pointer gestures reordering simulation
        window.reorderItems = function(sourceId, targetId) {
            const src = document.getElementById(sourceId);
            const tgt = document.getElementById(targetId);
            if (src && tgt && src !== tgt) {
                list.insertBefore(src, tgt);
                document.getElementById('valError').style.display = 'none';
                return true;
            }
            return false;
        };
    </script>
</body>
</html>
"""

class TestQualtricsModernRanking(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.page = await self.browser.new_page()
        await self.page.set_content(HTML_QUALTRICS_MODERN_RANKING)

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()

    async def test_01_qualtrics_modern_ranking_discovery(self):
        """Scraper must discover Qualtrics Simple Layout ranking container, options, and handles."""
        fields = await scrape_page(self.page)
        ranking_fields = [f for f in fields if f.get("role") == "ranking" or f.get("type") == "ranking"]
        
        self.assertEqual(len(ranking_fields), 1, "Should discover exactly 1 ranking question")
        rf = ranking_fields[0]
        self.assertIn("Please rank your top three formats", rf["label"])
        self.assertEqual(len(rf["options"]), 3)
        self.assertEqual(rf["options"][0], "Short videos")
        self.assertEqual(rf["options"][1], "Interactive scenario-based modules")
        self.assertEqual(rf["options"][2], "Mobile apps")

    async def test_02_copilot_items_inspection_detects_all_items(self):
        """Co-Pilot /items must find all 3 items with handles rather than reporting 0 items."""
        discovered = await RankingHandler.inspect_interactive_items(self.page)
        items = discovered.get("items", [])
        targets = discovered.get("targets", [])
        
        self.assertEqual(len(items), 3, "Co-Pilot /items must detect all 3 items")
        self.assertEqual(items[0]["label"], "Short videos")
        self.assertEqual(items[1]["label"], "Interactive scenario-based modules")
        self.assertEqual(items[2]["label"], "Mobile apps")
        
        # Position targets 1, 2, 3 must be available
        self.assertEqual(len(targets), 3)

    async def test_03_automatic_bot_drag_execution(self):
        """The bot itself can automatically drag/reorder without requiring manual user drag."""
        fields = await scrape_page(self.page)
        rf = fields[0]
        
        # User requested ranking: ['Interactive scenario-based modules', 'Short videos', 'Mobile apps']
        intended_order = [
            "Interactive scenario-based modules",
            "Short videos",
            "Mobile apps"
        ]
        
        fill_res = await universal_registry.dispatch_fill(self.page, rf, {"value": intended_order})
        self.assertTrue(fill_res.success, f"Bot auto-drag failed: {fill_res.diagnostics}")
        self.assertIsNotNone(fill_res.applied_strategy)

    async def test_04_copilot_remote_move_command(self):
        """Telegram /move command must successfully reorder items."""
        chat_id = "test_qualtrics_chat"
        session_id = "test_qualtrics_sess"
        
        RemoteSessionManager.register_session(
            session_id=session_id,
            page=self.page,
            chat_id=chat_id,
            browser=self.browser
        )
        
        # Move item 2 to position 1
        res = await RemoteSessionManager.dispatch_remote_action("move", payload={"item_id": "2", "position": "1"}, chat_id=chat_id)
        self.assertTrue(res.get("success"), f"Remote move failed: {res}")
        self.assertTrue(res.get("verified"))
        
        RemoteSessionManager.unregister_session(session_id=session_id, chat_id=chat_id)


if __name__ == '__main__':
    unittest.main()
