"""
Stillbot Execution Environment and Human Intervention Architecture.

Supports two primary run modes:
1. Local PC Mode:
   - Visible local desktop browser (Chrome/Chromium).
   - Direct-interaction workflow with onscreen Co-Pilot cues.
   - Resumes automatically when the user advances the page.

2. Render Cloud Mode:
   - Headless cloud container execution (no local display).
   - Pauses automation and AI spending when human intervention is needed.
   - Dispatches Telegram Co-Pilot notifications with remote interaction controls.
   - Secure remote action execution (advance, fill, click, snapshot).
   - Resumes only after verified state transitions.

Configuration:
- STILLBOT_RUN_MODE=auto|local|cloud (deployment env overrides auto-detection).
"""

import os
import sys
import time
import asyncio
from abc import ABC, abstractmethod
from enum import Enum
from typing import Optional, Callable, Awaitable, Dict, Any, Tuple
from playwright.async_api import Page


def _safe_print(msg: str) -> None:
    """Safe print helper preventing Windows cp1252 encoding crashes."""
    try:
        print(msg)
    except UnicodeEncodeError:
        try:
            encoding = getattr(sys.stdout, "encoding", None) or "ascii"
            clean_msg = msg.encode(encoding, errors="replace").decode(encoding)
            print(clean_msg)
        except Exception:
            pass


class RunMode(str, Enum):
    LOCAL = "local"
    CLOUD = "cloud"
    AUTO = "auto"


def has_graphical_display(env: Optional[Dict[str, str]] = None) -> bool:
    """Checks whether a graphical desktop display server is available."""
    e = env if env is not None else os.environ
    if os.name == "nt":
        # Windows desktop session has GUI display by default
        return True
    # Linux / Unix display indicators
    return bool(e.get("DISPLAY") or e.get("WAYLAND_DISPLAY"))


def is_cloud_environment(env: Optional[Dict[str, str]] = None) -> bool:
    """Detects if running on cloud platforms (Render, Heroku, Kubernetes, etc.)."""
    e = env if env is not None else os.environ
    cloud_indicators = [
        "RENDER",
        "RENDER_SERVICE_ID",
        "RENDER_INSTANCE_ID",
        "DYNO",
        "KUBERNETES_SERVICE_HOST",
        "FLY_APP_NAME",
        "HEROKU_APP_NAME",
        "CONTAINER"
    ]
    return any(bool(e.get(k)) for k in cloud_indicators)


def resolve_run_mode(env: Optional[Dict[str, str]] = None) -> str:
    """
    Resolves the active Stillbot run mode.
    Explicit 'local' or 'cloud' setting overrides automatic detection.
    """
    e = env if env is not None else os.environ
    configured_mode = (e.get("STILLBOT_RUN_MODE") or "auto").strip().lower()

    if configured_mode == RunMode.LOCAL.value:
        return RunMode.LOCAL.value
    if configured_mode == RunMode.CLOUD.value:
        return RunMode.CLOUD.value

    # Automatic detection
    if is_cloud_environment(e):
        return RunMode.CLOUD.value
    if not has_graphical_display(e):
        return RunMode.CLOUD.value
    if (e.get("HEADLESS") or "").strip().lower() == "true" and os.name != "nt":
        return RunMode.CLOUD.value

    return RunMode.LOCAL.value


def validate_and_log_run_mode(env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """
    Validates startup configuration and logs the selected execution mode
    without exposing any passwords, tokens, or credentials.
    """
    e = env if env is not None else os.environ
    raw_mode = (e.get("STILLBOT_RUN_MODE") or "auto").strip().lower()
    resolved = resolve_run_mode(e)
    display_avail = has_graphical_display(e)
    cloud_detected = is_cloud_environment(e)
    is_headless = True if resolved == RunMode.CLOUD.value else (e.get("HEADLESS", "false").lower() == "true")

    info = {
        "configured_mode": raw_mode,
        "resolved_mode": resolved,
        "display_available": display_avail,
        "cloud_environment": cloud_detected,
        "is_headless": is_headless
    }

    print("\n" + "-" * 70)
    print(f"STILLBOT RUNTIME ENVIRONMENT: {resolved.upper()}")
    print(f"* Configured Setting: {raw_mode} (Resolved: {resolved})")
    print(f"* Graphical Display Available: {display_avail}")
    print(f"* Cloud/Container Detected: {cloud_detected}")
    print(f"* Browser Mode: {'Headless' if is_headless else 'Visible Desktop Window'}")
    print("-" * 70 + "\n")

    return info


# ==============================================================================
# SECURE REMOTE SESSION MANAGER (Cloud Mode)
# ==============================================================================

class RemoteSessionManager:
    """
    Manages active cloud browser sessions and handles remote interaction
    commands dispatched from Telegram or secure API callbacks.
    """
    _active_pages: Dict[str, Page] = {}
    _action_queues: Dict[str, asyncio.Queue] = {}

    @classmethod
    def register_session(cls, session_id: str, page: Page) -> None:
        cls._active_pages[session_id] = page
        if session_id not in cls._action_queues:
            cls._action_queues[session_id] = asyncio.Queue()

    @classmethod
    def unregister_session(cls, session_id: str) -> None:
        cls._active_pages.pop(session_id, None)
        cls._action_queues.pop(session_id, None)

    @classmethod
    def get_session_page(cls, session_id: str = "default") -> Optional[Page]:
        if session_id in cls._active_pages:
            return cls._active_pages[session_id]
        if cls._active_pages:
            return next(iter(cls._active_pages.values()))
        return None

    @classmethod
    async def dispatch_remote_action(cls, action_type: str, payload: Optional[Dict[str, Any]] = None, session_id: str = "default") -> Dict[str, Any]:
        """
        Dispatches and executes a remote action on the active browser session.
        Supported actions: 'advance', 'click', 'fill', 'dismiss', 'snapshot'.
        """
        page = cls.get_session_page(session_id)
        if not page or page.is_closed():
            return {"success": False, "error": "No active remote browser session found."}

        payload = payload or {}
        try:
            if action_type in ["advance", "next", "continue", "submit"]:
                # Attempt to find and click any standard survey advance button
                selectors = [
                    '#NextButton', 'button#NextButton', 'input#NextButton',
                    '.next-button', '.NextButton', 'button.NextButton',
                    '.cf-navigation-next', 'button.cf-navigation-next',
                    'button[type="submit"]', 'input[type="submit"]',
                    'button:has-text("Next")', 'button:has-text("Continue")', 'button:has-text("Submit")'
                ]
                clicked = False
                for sel in selectors:
                    loc = page.locator(sel).first
                    if await loc.count() > 0:
                        try:
                            await loc.scroll_into_view_if_needed()
                            await loc.click(timeout=3000)
                            clicked = True
                            break
                        except Exception:
                            continue
                if not clicked:
                    # Fallback enter keypress
                    await page.keyboard.press("Enter")
                return {"success": True, "action": action_type, "clicked": clicked}

            elif action_type == "fill":
                selector = payload.get("selector", 'input[type="text"], input:not([type]), textarea')
                value = payload.get("value", "")
                loc = page.locator(selector).first
                if await loc.count() > 0:
                    try:
                        await loc.scroll_into_view_if_needed()
                    except Exception:
                        pass
                    await loc.fill(value)
                    return {"success": True, "action": action_type, "filled": value}
                return {"success": False, "error": f"Target input '{selector}' not found."}

            elif action_type == "click":
                selector = payload.get("selector", "")
                text = payload.get("text", "")
                if text:
                    loc = page.locator(f':has-text("{text}")').first
                else:
                    loc = page.locator(selector).first
                if await loc.count() > 0:
                    try:
                        await loc.scroll_into_view_if_needed()
                    except Exception:
                        pass
                    await loc.click(timeout=3000)
                    return {"success": True, "action": action_type}
                return {"success": False, "error": f"Element '{selector or text}' not found."}

            elif action_type == "dismiss":
                dismiss_btn = page.locator('button:has-text("OK"), button:has-text("Dismiss"), button:has-text("Close"), .modal-close').first
                if await dismiss_btn.count() > 0:
                    await dismiss_btn.click(timeout=3000)
                    return {"success": True, "action": action_type}
                return {"success": False, "error": "No dismiss button found."}

            elif action_type == "snapshot":
                shot = await page.screenshot(type="png")
                return {"success": True, "action": action_type, "screenshot": shot}

            return {"success": False, "error": f"Unknown action type '{action_type}'."}
        except Exception as e:
            return {"success": False, "error": str(e)}


# ==============================================================================
# COMMON INTERVENTION INTERFACE & IMPLEMENTATIONS
# ==============================================================================

class HumanInterventionProvider(ABC):
    """Abstract Base Class for Human Intervention Co-Pilot Modes."""

    @abstractmethod
    async def request_intervention(
        self,
        page: Page,
        page_num: int,
        reason: str = "",
        status_callback: Optional[Callable[[str], Awaitable[None]]] = None,
        timeout_seconds: int = 300,
        **kwargs
    ) -> bool:
        """
        Pauses autonomous execution and requests human intervention.
        Resumes ONLY after an explicit verified page-state transition.
        """
        pass


class LocalDesktopInterventionProvider(HumanInterventionProvider):
    """
    Local PC Intervention Provider:
    - User interacts directly with the visible local Chrome/Chromium window.
    - Prompts user to look at their Chrome screen.
    - Monitors local page for URL/DOM transition.
    """

    async def request_intervention(
        self,
        page: Page,
        page_num: int,
        reason: str = "",
        status_callback: Optional[Callable[[str], Awaitable[None]]] = None,
        timeout_seconds: int = 300,
        **kwargs
    ) -> bool:
        print("\n" + "=" * 72)
        print(f"CO-PILOT MODE ACTIVE (Page {page_num})")
        print("=" * 72)
        print(f"Reason: {reason or 'Bot paused to prevent token waste and assist navigation'}")
        print("Look at your Chrome screen: Answer any missed fields or click Next!")
        print("The bot is watching live and will AUTO-RESUME as soon as you advance.")
        print("=" * 72 + "\n")

        if status_callback:
            try:
                msg = (
                    f"🤝 **Co-Pilot Assistance Needed on Page {page_num}**\n\n"
                    f"• {reason or 'Bot needs assistance on this page.'}\n"
                    f"• **AI credit spending is paused.**\n"
                    f"• 🔗 Live Survey URL: `{page.url}`\n\n"
                    f"👉 **Please click Next or answer directly in the open Chrome browser.**\n"
                    f"⚡ The bot will automatically detect when the page advances and resume!"
                )
                await status_callback(msg)
            except Exception:
                pass

        initial_url = page.url
        try:
            initial_html = await page.evaluate("document.body ? document.body.innerText.slice(0, 300) : ''")
        except Exception:
            initial_html = ""

        start_time = asyncio.get_event_loop().time()
        while True:
            elapsed = asyncio.get_event_loop().time() - start_time
            if timeout_seconds and elapsed > timeout_seconds:
                _safe_print(f"⏱️ Co-Pilot timeout ({timeout_seconds}s) reached on Page {page_num}.")
                if status_callback:
                    try:
                        await status_callback(f"⏱️ **Co-Pilot Timeout on Page {page_num}:** Exceeded {timeout_seconds}s waiting for human advance.")
                    except Exception:
                        pass
                return False

            try:
                await page.wait_for_timeout(1000)
                if page.is_closed():
                    _safe_print("⚠️ Browser was closed during Co-Pilot mode.")
                    return False

                current_url = page.url
                if current_url != initial_url:
                    _safe_print(f"🎉 URL change detected ({current_url})! User advanced the page.")
                    _safe_print("🚀 Co-Pilot: Resuming automated bot...")
                    await page.wait_for_timeout(1000)
                    return True

                current_html = await page.evaluate("document.body ? document.body.innerText.slice(0, 300) : ''")
                if current_html and current_html != initial_html:
                    _safe_print("🎉 DOM transition detected! User advanced the page.")
                    _safe_print("🚀 Co-Pilot: Resuming automated bot...")
                    await page.wait_for_timeout(1000)
                    return True
            except Exception as e:
                if "closed" in str(e).lower():
                    return False
                await page.wait_for_timeout(1000)


class RemoteCloudInterventionProvider(HumanInterventionProvider):
    """
    Render Cloud Intervention Provider:
    - Running in headless container without a local display.
    - Never instructs the user to look at a local Chrome screen.
    - Pauses automation and AI token spending.
    - Dispatches Telegram notifications with secure remote interaction instructions.
    - Registers remote browser session for interactive command execution.
    - Resumes ONLY after verifying the expected state transition.
    """

    async def request_intervention(
        self,
        page: Page,
        page_num: int,
        reason: str = "",
        status_callback: Optional[Callable[[str], Awaitable[None]]] = None,
        timeout_seconds: int = 300,
        **kwargs
    ) -> bool:
        session_id = f"session_{page_num}_{int(time.time())}"
        RemoteSessionManager.register_session(session_id, page)

        print("\n" + "=" * 72)
        print(f"CLOUD CO-PILOT MODE ACTIVE (Page {page_num})")
        print("=" * 72)
        print(f"Reason: {reason or 'Bot paused to prevent token waste and assist navigation'}")
        print("Environment: Cloud Server (No local desktop browser available)")
        print("Automation & AI credit spending are PAUSED.")
        print("Use Telegram Co-Pilot remote controls to interact with this session!")
        print("=" * 72 + "\n")

        # Capture remote proof screenshot if possible
        proof_bytes = None
        try:
            if not page.is_closed():
                proof_bytes = await page.screenshot(type="png")
        except Exception:
            pass

        if status_callback:
            try:
                msg = (
                    f"☁️ **Cloud Co-Pilot Assistance Needed (Page {page_num})**\n\n"
                    f"• **Reason:** {reason or 'Bot needs assistance on this page.'}\n"
                    f"• **AI credit spending is PAUSED.**\n"
                    f"• 🌐 **Server Session:** Cloud headless container (no local display).\n"
                    f"• 🔗 **Live Survey URL:** `{page.url}`\n\n"
                    f"📱 **Remote Interaction Controls:**\n"
                    f"  👉 Reply `/next` to click Next/Submit on the cloud browser.\n"
                    f"  👉 Reply `/fill <text>` to answer the required question.\n"
                    f"  👉 Reply `/snap` to request an updated screenshot.\n\n"
                    f"⚡ Stillbot will verify the state change and automatically resume!"
                )
                # Attempt to pass screenshot if callback accepts it, else standard message
                try:
                    await status_callback(msg, photo=proof_bytes)
                except TypeError:
                    await status_callback(msg)
            except Exception as e:
                print(f"⚠️ Cloud Co-Pilot status callback error: {e}")

        initial_url = page.url
        try:
            initial_html = await page.evaluate("document.body ? document.body.innerText.slice(0, 300) : ''")
        except Exception:
            initial_html = ""

        start_time = asyncio.get_event_loop().time()
        try:
            while True:
                elapsed = asyncio.get_event_loop().time() - start_time
                if timeout_seconds and elapsed > timeout_seconds:
                    _safe_print(f"⏱️ Cloud Co-Pilot timeout ({timeout_seconds}s) reached on Page {page_num}.")
                    if status_callback:
                        try:
                            await status_callback(f"⏱️ **Cloud Co-Pilot Timeout (Page {page_num}):** Exceeded {timeout_seconds}s waiting for remote action.")
                        except Exception:
                            pass
                    return False

                await page.wait_for_timeout(1000)
                if page.is_closed():
                    _safe_print("⚠️ Remote browser session was closed.")
                    return False

                # 1. Check for URL change
                current_url = page.url
                if current_url != initial_url:
                    _safe_print(f"🎉 Remote URL transition verified ({current_url})! Resuming cloud automation...")
                    if status_callback:
                        try:
                            await status_callback(f"✅ **Remote transition verified!** Resuming automation on Page {page_num + 1}...")
                        except Exception:
                            pass
                    await page.wait_for_timeout(1000)
                    return True

                # 2. Check for DOM transition
                current_html = await page.evaluate("document.body ? document.body.innerText.slice(0, 300) : ''")
                if current_html and current_html != initial_html:
                    _safe_print("🎉 Remote DOM transition verified! Resuming cloud automation...")
                    if status_callback:
                        try:
                            await status_callback(f"✅ **Remote DOM transition verified!** Resuming automation...")
                        except Exception:
                            pass
                    await page.wait_for_timeout(1000)
                    return True

        finally:
            RemoteSessionManager.unregister_session(session_id)


def get_intervention_provider(mode: Optional[str] = None) -> HumanInterventionProvider:
    """Factory returning the appropriate human intervention provider."""
    active_mode = mode or resolve_run_mode()
    if active_mode == RunMode.CLOUD.value:
        return RemoteCloudInterventionProvider()
    return LocalDesktopInterventionProvider()
