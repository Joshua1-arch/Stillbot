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
from dataclasses import dataclass
from typing import Optional, Callable, Awaitable, Dict, Any, Tuple, Union
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


def log_copilot_event(
    event: str,
    chat_id: Optional[Union[str, int]] = None,
    user_id: Optional[Union[str, int]] = None,
    session_id: Optional[str] = None,
    command: Optional[str] = None,
    action: Optional[str] = None,
    result: Optional[str] = None,
    state: Optional[str] = None,
    page_num: Optional[int] = None,
    url: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None
) -> None:
    """
    Structured Co-Pilot audit logging.
    Strictly avoids logging passwords, tokens, API keys, or sensitive respondent answers.
    """
    parts = [f"[COPILOT_AUDIT] event={event}"]
    if chat_id is not None:
        parts.append(f"chat_id={chat_id}")
    if user_id is not None:
        parts.append(f"user_id={user_id}")
    if session_id:
        parts.append(f"session_id={session_id}")
    if command:
        parts.append(f"cmd={command}")
    if action:
        parts.append(f"action={action}")
    if state:
        parts.append(f"state={state}")
    if page_num is not None:
        parts.append(f"page={page_num}")
    if result:
        parts.append(f"result={result}")
    if url:
        clean_url = url.split("?")[0] if "?" in url else url
        parts.append(f"url={clean_url}")
    if details:
        safe_details = {
            k: v for k, v in details.items()
            if not any(s in k.lower() for s in ["key", "token", "pass", "secret", "auth", "credential"])
        }
        parts.append(f"details={safe_details}")
    if error:
        parts.append(f"error={error}")
    _safe_print(" ".join(parts))


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

@dataclass
class RemoteSessionInfo:
    """Represents an active or paused remote browser session."""
    session_id: str
    chat_id: str
    page: Page
    created_at: float
    last_active: float
    user_id: Optional[str] = None
    page_num: int = 1
    state: str = "RUNNING"  # "RUNNING", "PAUSED_CO_PILOT", "COMPLETED", "TERMINATED"
    url: str = ""


class RemoteSessionManager:
    """
    Manages active cloud browser sessions and handles remote interaction
    commands dispatched from Telegram or secure API callbacks.
    """
    _sessions_by_chat: Dict[str, RemoteSessionInfo] = {}
    _sessions_by_id: Dict[str, RemoteSessionInfo] = {}
    _active_pages: Dict[str, Page] = {}
    _action_queues: Dict[str, asyncio.Queue] = {}

    @classmethod
    def register_session(
        cls,
        session_id: str,
        page: Page,
        chat_id: Optional[Union[str, int]] = None,
        user_id: Optional[Union[str, int]] = None,
        page_num: int = 1,
        state: str = "RUNNING"
    ) -> RemoteSessionInfo:
        """Registers an active browser session under its session_id and chat_id."""
        chat_key = str(chat_id) if chat_id is not None else "default"
        cur_url = ""
        try:
            if not page.is_closed():
                cur_url = page.url
        except Exception:
            pass

        now = time.time()
        info = RemoteSessionInfo(
            session_id=session_id,
            chat_id=chat_key,
            user_id=str(user_id) if user_id else None,
            page=page,
            created_at=now,
            last_active=now,
            page_num=page_num,
            state=state,
            url=cur_url
        )

        cls._sessions_by_id[session_id] = info
        cls._sessions_by_chat[chat_key] = info
        cls._active_pages[session_id] = page
        if session_id not in cls._action_queues:
            cls._action_queues[session_id] = asyncio.Queue()

        log_copilot_event(
            "SESSION_REGISTERED",
            chat_id=chat_key,
            user_id=user_id,
            session_id=session_id,
            state=state,
            page_num=page_num,
            url=cur_url
        )
        return info

    @classmethod
    def mark_session_paused(
        cls,
        chat_id: Optional[Union[str, int]] = None,
        session_id: Optional[str] = None,
        page_num: int = 1,
        reason: str = ""
    ) -> Optional[RemoteSessionInfo]:
        """Marks the session as paused for Co-Pilot human intervention."""
        info = cls.get_session_info(chat_id=chat_id, session_id=session_id)
        if info:
            info.state = "PAUSED_CO_PILOT"
            info.page_num = page_num
            info.last_active = time.time()
            try:
                if not info.page.is_closed():
                    info.url = info.page.url
            except Exception:
                pass
            log_copilot_event(
                "SESSION_PAUSED",
                chat_id=info.chat_id,
                session_id=info.session_id,
                state=info.state,
                page_num=page_num,
                url=info.url,
                details={"reason": reason}
            )
        return info

    @classmethod
    def mark_session_resumed(
        cls,
        chat_id: Optional[Union[str, int]] = None,
        session_id: Optional[str] = None,
        page_num: int = 1
    ) -> Optional[RemoteSessionInfo]:
        """Marks the session as resumed back into automated execution."""
        info = cls.get_session_info(chat_id=chat_id, session_id=session_id)
        if info:
            info.state = "RUNNING"
            info.page_num = page_num
            info.last_active = time.time()
            try:
                if not info.page.is_closed():
                    info.url = info.page.url
            except Exception:
                pass
            log_copilot_event(
                "SESSION_RESUMED",
                chat_id=info.chat_id,
                session_id=info.session_id,
                state=info.state,
                page_num=page_num,
                url=info.url
            )
        return info

    @classmethod
    def unregister_session(
        cls,
        session_id: Optional[str] = None,
        chat_id: Optional[Union[str, int]] = None,
        reason: str = "SURVEY_ENDED"
    ) -> None:
        """Removes an active session from the registry upon termination."""
        chat_key = str(chat_id) if chat_id is not None else None
        target_info = None

        if session_id and session_id in cls._sessions_by_id:
            target_info = cls._sessions_by_id.pop(session_id, None)
            if target_info and target_info.chat_id in cls._sessions_by_chat:
                cls._sessions_by_chat.pop(target_info.chat_id, None)
            cls._active_pages.pop(session_id, None)
            cls._action_queues.pop(session_id, None)
        elif chat_key and chat_key in cls._sessions_by_chat:
            target_info = cls._sessions_by_chat.pop(chat_key, None)
            if target_info:
                cls._sessions_by_id.pop(target_info.session_id, None)
                cls._active_pages.pop(target_info.session_id, None)
                cls._action_queues.pop(target_info.session_id, None)

        if target_info:
            target_info.state = "TERMINATED"
            log_copilot_event(
                "SESSION_CLEANED_UP",
                chat_id=target_info.chat_id,
                session_id=target_info.session_id,
                state="TERMINATED",
                details={"reason": reason}
            )

    @classmethod
    def get_session_info(
        cls,
        chat_id: Optional[Union[str, int]] = None,
        session_id: Optional[str] = None,
        allow_closed: bool = False
    ) -> Optional[RemoteSessionInfo]:
        """Retrieves active session info by chat_id or session_id with health check."""
        chat_key = str(chat_id) if chat_id is not None else None

        # 1. Lookup by explicit session_id
        if session_id and session_id in cls._sessions_by_id:
            info = cls._sessions_by_id[session_id]
            if allow_closed or not info.page.is_closed():
                return info
            cls.unregister_session(session_id=session_id, reason="BROWSER_CLOSED")
            return None

        # 2. Lookup by explicit chat_id
        if chat_key and chat_key in cls._sessions_by_chat:
            info = cls._sessions_by_chat[chat_key]
            if allow_closed or not info.page.is_closed():
                return info
            cls.unregister_session(chat_id=chat_key, reason="BROWSER_CLOSED")
            return None

        # 3. Fallback: single active session fallback for test suites or default lookup
        if (chat_key is None or chat_key == "default") and len(cls._sessions_by_chat) == 1:
            info = next(iter(cls._sessions_by_chat.values()))
            if allow_closed or not info.page.is_closed():
                return info
            cls.unregister_session(chat_id=info.chat_id, reason="BROWSER_CLOSED")
            return None

        # 4. Check active pages dictionary fallback
        if session_id and session_id in cls._active_pages:
            p = cls._active_pages[session_id]
            if allow_closed or not p.is_closed():
                return RemoteSessionInfo(
                    session_id=session_id,
                    chat_id="default",
                    page=p,
                    created_at=time.time(),
                    last_active=time.time(),
                    state="RUNNING"
                )

        return None

    @classmethod
    def get_session_page(cls, session_id: str = "default", chat_id: Optional[Union[str, int]] = None) -> Optional[Page]:
        """Convenience method returning the active Page object for the session."""
        info = cls.get_session_info(chat_id=chat_id, session_id=session_id)
        return info.page if info else None

    @classmethod
    async def dispatch_remote_action(
        cls,
        action_type: str,
        payload: Optional[Dict[str, Any]] = None,
        session_id: str = "default",
        chat_id: Optional[Union[str, int]] = None,
        user_id: Optional[Union[str, int]] = None
    ) -> Dict[str, Any]:
        """
        Dispatches and executes a remote action on the active cloud browser session.
        Supported actions: 'advance', 'fill', 'click', 'dismiss', 'snapshot'.
        Verifies post-action state and returns clear diagnostic responses.
        """
        payload = payload or {}
        info = cls.get_session_info(chat_id=chat_id, session_id=session_id, allow_closed=True)

        if not info:
            log_copilot_event(
                "SESSION_LOOKUP_MISSING",
                chat_id=chat_id,
                user_id=user_id,
                action=action_type,
                result="NOT_FOUND"
            )
            return {
                "success": False,
                "error": "No active remote browser session found for your chat. Start a survey with /start or check if the session already completed."
            }

        page = info.page
        if page.is_closed():
            cls.unregister_session(chat_id=info.chat_id, reason="BROWSER_CLOSED")
            log_copilot_event(
                "BROWSER_CLOSED",
                chat_id=info.chat_id,
                user_id=user_id,
                session_id=info.session_id,
                action=action_type
            )
            return {
                "success": False,
                "error": "The survey browser session was closed or expired. Please start a new survey with /start."
            }

        log_copilot_event(
            "ACTION_ATTEMPTED",
            chat_id=info.chat_id,
            user_id=user_id,
            session_id=info.session_id,
            action=action_type,
            page_num=info.page_num,
            url=page.url
        )

        try:
            if action_type in ["snapshot", "snap"]:
                shot = await page.screenshot(type="png")
                cur_url = page.url
                try:
                    title = await page.title()
                except Exception:
                    title = "Survey Page"
                info.last_active = time.time()
                info.url = cur_url
                log_copilot_event(
                    "ACTION_RESULT",
                    chat_id=info.chat_id,
                    user_id=user_id,
                    session_id=info.session_id,
                    action="snapshot",
                    result="SUCCESS",
                    url=cur_url,
                    page_num=info.page_num
                )
                return {
                    "success": True,
                    "action": "snapshot",
                    "screenshot": shot,
                    "url": cur_url,
                    "title": title,
                    "page_num": info.page_num
                }

            elif action_type in ["advance", "next", "continue", "submit"]:
                initial_url = page.url
                initial_content = ""
                try:
                    initial_content = await page.evaluate("document.body ? document.body.innerText.slice(0, 300) : ''")
                except Exception:
                    pass

                selectors = [
                    '#NextButton', 'button#NextButton', 'input#NextButton',
                    'button.NextButton', 'input.NextButton', '.NextButton', '.next-button',
                    '.cf-navigation-next', 'button.cf-navigation-next', '.cf-navigation__button',
                    'button[type="submit"]', 'input[type="submit"]',
                    'button:has-text("Next")', 'button:has-text("Continue")', 'button:has-text("Submit")',
                    'button:has-text("Proceed")', 'button:has-text("Start")',
                    '[data-action*="next" i]', '[data-action*="submit" i]'
                ]
                clicked = False
                clicked_selector = None
                for sel in selectors:
                    loc = page.locator(sel).first
                    if await loc.count() > 0:
                        try:
                            if await loc.is_visible():
                                await loc.scroll_into_view_if_needed()
                                await loc.click(timeout=3000)
                                clicked = True
                                clicked_selector = sel
                                break
                        except Exception:
                            continue

                if not clicked:
                    # Fallback enter keypress
                    await page.keyboard.press("Enter")
                    clicked_selector = "Enter_key"

                # State Verification Phase: Wait up to 3.5s for URL change, DOM change, or validation error
                transition_verified = False
                validation_error_msg = None
                poll_start = time.time()

                while time.time() - poll_start < 3.5:
                    await page.wait_for_timeout(300)
                    if page.is_closed():
                        break

                    # 1. Check for on-screen survey validation errors FIRST
                    err_text = await page.evaluate("""() => {
                        const errSelectors = [
                            '.error', '.ValidationError', '.error-message', '.cf-error',
                            '[aria-invalid="true"]', '.has-error', '.survey-error'
                        ];
                        for (const sel of errSelectors) {
                            const el = document.querySelector(sel);
                            if (el && el.offsetParent !== null) {
                                const txt = (el.innerText || el.textContent || '').trim();
                                if (txt.length > 0 && txt.length < 200) return txt;
                            }
                        }
                        return null;
                    }""")
                    if err_text:
                        validation_error_msg = err_text
                        transition_verified = False
                        break

                    # 2. Check for URL change
                    current_url = page.url
                    if current_url != initial_url:
                        transition_verified = True
                        break

                    # 3. Check for DOM transition
                    current_content = await page.evaluate("document.body ? document.body.innerText.slice(0, 300) : ''")
                    if current_content and current_content != initial_content:
                        transition_verified = True
                        break

                info.last_active = time.time()
                info.url = page.url

                if transition_verified:
                    log_copilot_event(
                        "ACTION_RESULT",
                        chat_id=info.chat_id,
                        user_id=user_id,
                        session_id=info.session_id,
                        action="advance",
                        result="TRANSITION_VERIFIED",
                        url=page.url
                    )
                    return {
                        "success": True,
                        "action": "advance",
                        "verified": True,
                        "clicked_selector": clicked_selector,
                        "url": page.url
                    }
                elif validation_error_msg:
                    log_copilot_event(
                        "ACTION_RESULT",
                        chat_id=info.chat_id,
                        user_id=user_id,
                        session_id=info.session_id,
                        action="advance",
                        result="VALIDATION_ERROR",
                        details={"error_msg": validation_error_msg}
                    )
                    return {
                        "success": False,
                        "error": f"Page validation error: '{validation_error_msg}'. Please answer required questions using /fill <answer> first."
                    }
                else:
                    log_copilot_event(
                        "ACTION_RESULT",
                        chat_id=info.chat_id,
                        user_id=user_id,
                        session_id=info.session_id,
                        action="advance",
                        result="UNVERIFIED",
                        details={"clicked_selector": clicked_selector}
                    )
                    return {
                        "success": False,
                        "error": "Clicked Next, but no page transition occurred. An unanswered question or captcha may require attention. Use /snap to inspect the page."
                    }

            elif action_type == "fill":
                value = (payload.get("value") or "").strip()
                if not value:
                    return {"success": False, "error": "Fill value cannot be empty. Usage: /fill <your answer text>"}

                # Search for target input: prioritizes invalid/required/empty inputs
                target_info = await page.evaluate("""() => {
                    const inputs = Array.from(document.querySelectorAll('input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="checkbox"]):not([type="radio"]), textarea'));
                    const visible = inputs.filter(el => {
                        const style = window.getComputedStyle(el);
                        return style && style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0' && el.offsetParent !== null && !el.disabled && !el.readOnly;
                    });

                    if (visible.length === 0) return null;

                    // 1. Element with error or invalid state
                    const invalidEl = visible.find(el => el.getAttribute('aria-invalid') === 'true' || el.classList.contains('error') || el.closest('.error, .ValidationError, .has-error'));
                    if (invalidEl) {
                        invalidEl.setAttribute('data-copilot-target', 'true');
                        return {
                            name: invalidEl.getAttribute('aria-label') || invalidEl.getAttribute('placeholder') || invalidEl.name || invalidEl.id || 'required field',
                            selector: '[data-copilot-target="true"]'
                        };
                    }

                    // 2. Element marked required and empty
                    const reqEmptyEl = visible.find(el => (el.required || el.getAttribute('aria-required') === 'true') && (!el.value || el.value.trim() === ''));
                    if (reqEmptyEl) {
                        reqEmptyEl.setAttribute('data-copilot-target', 'true');
                        return {
                            name: reqEmptyEl.getAttribute('aria-label') || reqEmptyEl.getAttribute('placeholder') || reqEmptyEl.name || reqEmptyEl.id || 'required field',
                            selector: '[data-copilot-target="true"]'
                        };
                    }

                    // 3. First empty visible element
                    const emptyEl = visible.find(el => !el.value || el.value.trim() === '');
                    if (emptyEl) {
                        emptyEl.setAttribute('data-copilot-target', 'true');
                        return {
                            name: emptyEl.getAttribute('aria-label') || emptyEl.getAttribute('placeholder') || emptyEl.name || emptyEl.id || 'text field',
                            selector: '[data-copilot-target="true"]'
                        };
                    }

                    // 4. Default to first visible element
                    visible[0].setAttribute('data-copilot-target', 'true');
                    return {
                        name: visible[0].getAttribute('aria-label') || visible[0].getAttribute('placeholder') || visible[0].name || visible[0].id || 'input field',
                        selector: '[data-copilot-target="true"]'
                    };
                }""")

                if not target_info:
                    log_copilot_event(
                        "ACTION_RESULT",
                        chat_id=info.chat_id,
                        user_id=user_id,
                        session_id=info.session_id,
                        action="fill",
                        result="NO_INPUT_FOUND"
                    )
                    return {
                        "success": False,
                        "error": "No empty or required text input field found on the current page. If this is a multiple-choice question, use /snap to inspect."
                    }

                target_sel = target_info["selector"]
                field_label = target_info["name"]

                loc = page.locator(target_sel).first
                await loc.scroll_into_view_if_needed()
                await loc.fill(value)
                await loc.dispatch_event("input")
                await loc.dispatch_event("change")

                actual_val = await loc.input_value()
                await page.evaluate("""() => {
                    const el = document.querySelector('[data-copilot-target="true"]');
                    if (el) el.removeAttribute('data-copilot-target');
                }""")

                info.last_active = time.time()
                if actual_val == value:
                    log_copilot_event(
                        "ACTION_RESULT",
                        chat_id=info.chat_id,
                        user_id=user_id,
                        session_id=info.session_id,
                        action="fill",
                        result="FILLED_VERIFIED",
                        details={"field_name": field_label}
                    )
                    return {
                        "success": True,
                        "action": "fill",
                        "field_name": field_label,
                        "verified": True
                    }
                else:
                    return {
                        "success": False,
                        "error": f"Fill attempted on '{field_label}', but actual value '{actual_val}' did not match intended input."
                    }

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
                    info.last_active = time.time()
                    return {"success": True, "action": action_type}
                return {"success": False, "error": f"Element '{selector or text}' not found."}

            elif action_type == "dismiss":
                dismiss_btn = page.locator('button:has-text("OK"), button:has-text("Dismiss"), button:has-text("Close"), .modal-close').first
                if await dismiss_btn.count() > 0:
                    await dismiss_btn.click(timeout=3000)
                    info.last_active = time.time()
                    return {"success": True, "action": action_type}
                return {"success": False, "error": "No dismiss button found."}

            return {"success": False, "error": f"Unknown action type '{action_type}'."}
        except Exception as e:
            log_copilot_event(
                "ACTION_EXCEPTION",
                chat_id=info.chat_id,
                user_id=user_id,
                session_id=info.session_id,
                action=action_type,
                error=str(e)
            )
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
        chat_id: Optional[Union[str, int]] = None,
        **kwargs
    ) -> bool:
        chat_key = str(chat_id or getattr(page, "_chat_id", None) or "default")
        session_id = f"session_{chat_key}_{page_num}"

        # 1. Register or validate the live remote session BEFORE sending Co-Pilot message
        info = RemoteSessionManager.get_session_info(chat_id=chat_key)
        if not info:
            RemoteSessionManager.register_session(
                session_id=session_id,
                page=page,
                chat_id=chat_key,
                page_num=page_num,
                state="PAUSED_CO_PILOT"
            )
        else:
            RemoteSessionManager.mark_session_paused(
                chat_id=chat_key,
                page_num=page_num,
                reason=reason
            )

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

        # 2. Send intervention prompt ONLY after session is registered and available
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
                    RemoteSessionManager.mark_session_resumed(chat_id=chat_key, page_num=page_num + 1)
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
                    RemoteSessionManager.mark_session_resumed(chat_id=chat_key, page_num=page_num)
                    if status_callback:
                        try:
                            await status_callback(f"✅ **Remote DOM transition verified!** Resuming automation...")
                        except Exception:
                            pass
                    await page.wait_for_timeout(1000)
                    return True

        finally:
            # DO NOT DELETE SESSION HERE: Session stays alive for ongoing survey execution!
            pass


def get_intervention_provider(mode: Optional[str] = None) -> HumanInterventionProvider:
    """Factory returning the appropriate human intervention provider."""
    active_mode = mode or resolve_run_mode()
    if active_mode == RunMode.CLOUD.value:
        return RemoteCloudInterventionProvider()
    return LocalDesktopInterventionProvider()
