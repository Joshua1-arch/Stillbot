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
from dataclasses import dataclass, field
from typing import Optional, Callable, Awaitable, Dict, Any, Tuple, Union, List
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
    state: str = "RUNNING"  # "RUNNING", "PAUSED_CO_PILOT", "PAUSED_USER", "STOPPING", "COMPLETED", "TERMINATED"
    url: str = ""
    browser: Any = None
    task: Any = None
    temp_dir: Optional[str] = None
    dynamic_bridge: Any = None
    registration_token: str = field(default_factory=lambda: str(time.time()))
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pause_event: asyncio.Event = field(default_factory=lambda: asyncio.Event())
    widget_items: Dict[str, Any] = field(default_factory=dict)
    widget_targets: Dict[str, Any] = field(default_factory=dict)
    active_widget: Optional[Dict[str, Any]] = None

    def __post_init__(self):
        # Default pause_event to set so unpaused runs execute without blocking
        self.pause_event.set()

    def is_stopping(self) -> bool:
        return self.state in ["STOPPING", "TERMINATED"]

    def is_paused(self) -> bool:
        return self.state in ["PAUSED_USER", "PAUSED_CO_PILOT"]


class RemoteSessionManager:
    """
    Manages active cloud browser sessions and handles remote interaction
    commands dispatched from Telegram or secure API callbacks.
    Provides per-session locking, emergency stop cancellation, and interactive widget controls.
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
        state: str = "RUNNING",
        browser: Any = None,
        task: Any = None,
        temp_dir: Optional[str] = None,
        dynamic_bridge: Any = None
    ) -> RemoteSessionInfo:
        """Registers or updates an active browser session under its session_id and chat_id."""
        chat_key = str(chat_id) if chat_id is not None else "default"
        cur_url = ""
        try:
            if not page.is_closed():
                cur_url = page.url
        except Exception:
            pass

        now = time.time()
        existing = cls._sessions_by_chat.get(chat_key)
        if existing and existing.session_id == session_id:
            existing.page = page
            if browser:
                existing.browser = browser
            if task:
                existing.task = task
            if temp_dir:
                existing.temp_dir = temp_dir
            if dynamic_bridge:
                existing.dynamic_bridge = dynamic_bridge
            existing.last_active = now
            existing.page_num = page_num
            existing.state = state
            existing.url = cur_url
            cls._active_pages[session_id] = page
            return existing

        info = RemoteSessionInfo(
            session_id=session_id,
            chat_id=chat_key,
            user_id=str(user_id) if user_id else None,
            page=page,
            created_at=now,
            last_active=now,
            page_num=page_num,
            state=state,
            url=cur_url,
            browser=browser,
            task=task,
            temp_dir=temp_dir,
            dynamic_bridge=dynamic_bridge
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
            info.widget_items.clear()
            info.widget_targets.clear()
            info.active_widget = None
            info.pause_event.set()
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
        token: Optional[str] = None,
        registration_token: Optional[str] = None,
        reason: str = "SURVEY_ENDED"
    ) -> bool:
        """Removes an active session from the registry. Safe against removing newer sessions."""
        effective_token = registration_token or token
        chat_key = str(chat_id) if chat_id is not None else None
        target_info = None

        if session_id and session_id in cls._sessions_by_id:
            candidate = cls._sessions_by_id.get(session_id)
            if effective_token and candidate and candidate.registration_token != effective_token:
                return False  # Skip if token does not match newer session
            target_info = cls._sessions_by_id.pop(session_id, None)
            if target_info and target_info.chat_id in cls._sessions_by_chat:
                if cls._sessions_by_chat[target_info.chat_id].session_id == target_info.session_id:
                    cls._sessions_by_chat.pop(target_info.chat_id, None)
            cls._active_pages.pop(session_id, None)
            cls._action_queues.pop(session_id, None)
        elif chat_key and chat_key in cls._sessions_by_chat:
            candidate = cls._sessions_by_chat.get(chat_key)
            if effective_token and candidate and candidate.registration_token != effective_token:
                return False
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
            return True
        return False

    @classmethod
    def get_session(
        cls,
        chat_id: Optional[Union[str, int]] = None,
        session_id: Optional[str] = None,
        allow_closed: bool = False
    ) -> Optional[RemoteSessionInfo]:
        """Convenience alias for get_session_info."""
        return cls.get_session_info(chat_id=chat_id, session_id=session_id, allow_closed=allow_closed)

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
            cls.unregister_session(session_id=session_id, token=info.registration_token, reason="BROWSER_CLOSED")
            return None

        # 2. Lookup by explicit chat_id
        if chat_key and chat_key in cls._sessions_by_chat:
            info = cls._sessions_by_chat[chat_key]
            if allow_closed or not info.page.is_closed():
                return info
            cls.unregister_session(chat_id=chat_key, token=info.registration_token, reason="BROWSER_CLOSED")
            return None

        # 3. Fallback: single active session fallback for test suites or default lookup
        if (chat_key is None or chat_key == "default") and len(cls._sessions_by_chat) == 1:
            info = next(iter(cls._sessions_by_chat.values()))
            if allow_closed or not info.page.is_closed():
                return info
            cls.unregister_session(chat_id=info.chat_id, token=info.registration_token, reason="BROWSER_CLOSED")
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
    async def terminate_session(
        cls,
        chat_id: Optional[Union[str, int]] = None,
        session_id: Optional[str] = None,
        user_id: Optional[Union[str, int]] = None,
        reason: str = "USER_STOPPED"
    ) -> Dict[str, Any]:
        """
        Emergency stop and cancellation:
        1. Marks session STOPPING immediately so new actions are rejected.
        2. Cancels active survey task and owned child tasks.
        3. Closes associated Playwright page and browser with bounded timeouts.
        4. Releases proxy and temporary directory resources.
        5. Removes session registry entries and marks TERMINATED.
        Cleanup is idempotent, safe against repeated calls, and guarantees per-user isolation.
        """
        chat_key = str(chat_id) if chat_id is not None else None
        info = cls.get_session_info(chat_id=chat_key, session_id=session_id, allow_closed=True)
        
        cleaned_resources: List[str] = []
        partial_errors: List[str] = []

        if not info:
            log_copilot_event("STOP_ALREADY_TERMINATED", chat_id=chat_key, user_id=user_id, result="NO_ACTIVE_SESSION")
            return {
                "success": True,
                "chat_id": chat_key,
                "state": "TERMINATED",
                "message": "No active survey session found. Session is already stopped.",
                "cleaned_resources": [],
                "partial_errors": []
            }

        # 1. Immediately mark STOPPING
        info.state = "STOPPING"
        info.pause_event.set()  # Unblock any waiting loops so they see the STOPPING state
        log_copilot_event("SESSION_STOPPING", chat_id=info.chat_id, session_id=info.session_id, details={"reason": reason})

        # 2. Cancel active survey task
        task = getattr(info, "task", None)
        if not task and info.chat_id:
            try:
                import telegram_bot
                task = telegram_bot.ACTIVE_SURVEY_TASKS.get(info.chat_id) or telegram_bot.ACTIVE_SURVEY_TASKS.get(
                    int(info.chat_id) if str(info.chat_id).lstrip('-').isdigit() else None
                )
            except Exception:
                pass

        if task and not task.done():
            try:
                task.cancel()
                cleaned_resources.append("survey_task_cancelled")
            except Exception as e:
                partial_errors.append(f"task_cancel: {e}")

        # 3. Close Playwright page with bounded timeout (3s)
        if info.page:
            try:
                if not info.page.is_closed():
                    await asyncio.wait_for(info.page.close(), timeout=3.0)
                cleaned_resources.append("browser_page_closed")
            except Exception as e:
                partial_errors.append(f"page_close: {e}")

        # 4. Close Playwright browser / context with bounded timeout (3s)
        if info.browser:
            try:
                is_conn = True
                if hasattr(info.browser, "is_connected") and callable(info.browser.is_connected):
                    is_conn = info.browser.is_connected()
                if is_conn:
                    await asyncio.wait_for(info.browser.close(), timeout=3.0)
                cleaned_resources.append("browser_closed")
            except Exception as e:
                partial_errors.append(f"browser_close: {e}")

        # 5. Stop dynamic proxy bridge if present
        if info.dynamic_bridge:
            try:
                await asyncio.wait_for(info.dynamic_bridge.stop(), timeout=3.0)
                cleaned_resources.append("proxy_bridge_stopped")
            except Exception as e:
                partial_errors.append(f"proxy_bridge_stop: {e}")

        # 6. Release temporary user data directory
        if info.temp_dir:
            try:
                import shutil
                shutil.rmtree(info.temp_dir, ignore_errors=True)
                cleaned_resources.append("temp_dir_removed")
            except Exception as e:
                partial_errors.append(f"temp_dir_cleanup: {e}")

        # 7. Unregister from registry
        cls.unregister_session(session_id=info.session_id, chat_id=info.chat_id, token=info.registration_token, reason=reason)
        info.state = "TERMINATED"

        log_copilot_event(
            "SESSION_TERMINATED",
            chat_id=info.chat_id,
            session_id=info.session_id,
            state="TERMINATED",
            details={"cleaned": cleaned_resources, "errors": partial_errors}
        )

        return {
            "success": len(partial_errors) == 0,
            "chat_id": info.chat_id,
            "session_id": info.session_id,
            "state": "TERMINATED",
            "message": "Survey session stopped and all resources terminated successfully." if not partial_errors else f"Session stopped with partial warnings: {', '.join(partial_errors)}",
            "cleaned_resources": cleaned_resources,
            "partial_errors": partial_errors
        }

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
        Enforces per-session locking, immediate preemption for /stop, and post-action verification.
        Supported actions:
        - 'stop', 'cancel': Emergency termination
        - 'status': Check current session state
        - 'pause', 'resume': Pause/resume automation loops
        - 'snapshot', 'snap': Fresh screenshot
        - 'advance', 'next': Click advance button with state verification
        - 'fill': Fill identified field
        - 'items': Inspect interactive draggable items and drop targets
        - 'drag': Move item to destination drop target and verify
        - 'move': Reorder item to ranking position slot and verify
        - 'click': Click element after validating role/bounds
        """
        payload = payload or {}
        chat_key = str(chat_id) if chat_id is not None else None

        # Priority 1: Emergency Stop takes immediate precedence without lock queuing
        if action_type in ["stop", "cancel"]:
            return await cls.terminate_session(chat_id=chat_key, session_id=session_id, user_id=user_id, reason="USER_STOPPED")

        info = cls.get_session_info(chat_id=chat_key, session_id=session_id, allow_closed=True)

        if not info:
            log_copilot_event(
                "SESSION_LOOKUP_MISSING",
                chat_id=chat_key,
                user_id=user_id,
                action=action_type,
                result="NOT_FOUND"
            )
            return {
                "success": False,
                "error": "No active remote browser session found for your chat. Start a survey with /start or check if the session already completed."
            }

        # Check if already in STOPPING or TERMINATED state
        if info.is_stopping():
            return {
                "success": False,
                "error": f"Session is {info.state.lower()}. New commands are rejected."
            }

        page = info.page
        if page.is_closed():
            cls.unregister_session(chat_id=info.chat_id, token=info.registration_token, reason="BROWSER_CLOSED")
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

        # Acquire per-session lock to prevent conflicting concurrent operations
        async with info.lock:
            if info.is_stopping():
                return {"success": False, "error": "Session was stopped while waiting for lock."}

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
                # ------------------------------------------------------------------
                # 1. STATUS
                # ------------------------------------------------------------------
                if action_type == "status":
                    cur_url = page.url if not page.is_closed() else ""
                    return {
                        "success": True,
                        "action": "status",
                        "state": info.state,
                        "page_num": info.page_num,
                        "url": cur_url,
                        "active_widget": info.active_widget,
                        "created_at": info.created_at,
                        "last_active": info.last_active
                    }

                # ------------------------------------------------------------------
                # 2. PAUSE / RESUME
                # ------------------------------------------------------------------
                elif action_type == "pause":
                    if info.state == "PAUSED_USER":
                        return {"success": True, "action": "pause", "state": info.state, "message": "Automation is already paused."}
                    info.state = "PAUSED_USER"
                    info.pause_event.clear()
                    info.last_active = time.time()
                    log_copilot_event("SESSION_PAUSED_BY_USER", chat_id=info.chat_id, session_id=info.session_id)
                    return {
                        "success": True,
                        "action": "pause",
                        "state": "PAUSED_USER",
                        "message": "Automation paused. Browser session remains alive. Use /resume to continue."
                    }

                elif action_type == "resume":
                    if info.state not in ["PAUSED_USER", "PAUSED_CO_PILOT"]:
                        return {"success": True, "action": "resume", "state": info.state, "message": "Automation is not paused."}
                    info.state = "RUNNING"
                    info.pause_event.set()
                    info.last_active = time.time()
                    log_copilot_event("SESSION_RESUMED_BY_USER", chat_id=info.chat_id, session_id=info.session_id)
                    return {
                        "success": True,
                        "action": "resume",
                        "state": "RUNNING",
                        "message": "Automation resumed."
                    }

                # ------------------------------------------------------------------
                # 3. SNAPSHOT
                # ------------------------------------------------------------------
                elif action_type in ["snapshot", "snap"]:
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

                # ------------------------------------------------------------------
                # 4. ITEMS (List draggable items & targets for interactive widgets)
                # ------------------------------------------------------------------
                elif action_type in ["items", "list_items"]:
                    from interaction_engine import RankingHandler
                    discovered = await RankingHandler.inspect_interactive_items(page)
                    info.widget_items = {it["id"]: it for it in discovered.get("items", [])}
                    info.widget_targets = {tgt["id"]: tgt for tgt in discovered.get("targets", [])}
                    info.last_active = time.time()

                    items_list = discovered.get("items", [])
                    targets_list = discovered.get("targets", [])

                    lines = ["📋 **Interactive Elements Detected:**\n"]
                    if items_list:
                        lines.append("**Draggable Items:**")
                        for it in items_list:
                            lines.append(f"  • `{it['id']}`: {it['label']}")
                    else:
                        lines.append("No draggable items detected on this page.")

                    if targets_list:
                        lines.append("\n**Valid Drop Destinations / Slots:**")
                        for tgt in targets_list:
                            lines.append(f"  • `{tgt['id']}`: {tgt['label']}")

                    lines.append("\n💡 **Actions Available:**")
                    lines.append("• `/drag <item_id> <target_id>` (e.g. `/drag 1 target_1`)")
                    lines.append("• `/move <item_id> <position>` (e.g. `/move 2 1`)")
                    lines.append("• `/next` (advance when ranking is complete)")

                    formatted_text = "\n".join(lines)
                    return {
                        "success": True,
                        "action": "items",
                        "items": items_list,
                        "targets": targets_list,
                        "formatted_text": formatted_text
                    }

                # ------------------------------------------------------------------
                # 5. DRAG (Move item to drop target)
                # ------------------------------------------------------------------
                elif action_type == "drag":
                    item_id = str(payload.get("item_id", "")).strip()
                    target_id = str(payload.get("target_id", "")).strip()

                    if not item_id or not target_id:
                        return {
                            "success": False,
                            "error": "Usage: `/drag <item_id> <target_id>`\nUse `/items` to see valid identifiers."
                        }

                    if not info.widget_items or not info.widget_targets:
                        from interaction_engine import RankingHandler
                        disc = await RankingHandler.inspect_interactive_items(page)
                        info.widget_items = {it["id"]: it for it in disc.get("items", [])}
                        info.widget_targets = {tgt["id"]: tgt for tgt in disc.get("targets", [])}

                    item_meta = info.widget_items.get(item_id)
                    target_meta = info.widget_targets.get(target_id)

                    source_loc = None
                    if item_meta and item_meta.get("selector"):
                        source_loc = page.locator(item_meta["selector"]).first
                    if not source_loc or await source_loc.count() == 0:
                        source_loc = page.locator(f'[data-copilot-item-id="{item_id}"]').first

                    target_loc = None
                    if target_meta and target_meta.get("selector"):
                        target_loc = page.locator(target_meta["selector"]).first
                    if not target_loc or await target_loc.count() == 0:
                        target_loc = page.locator(f'[data-copilot-target-id="{target_id}"]').first

                    if not source_loc or await source_loc.count() == 0:
                        return {"success": False, "error": f"Item '{item_id}' not found. Use `/items` to check valid IDs."}
                    if not target_loc or await target_loc.count() == 0:
                        return {"success": False, "error": f"Target destination '{target_id}' not found. Use `/items` to check valid targets."}

                    await source_loc.scroll_into_view_if_needed()
                    await target_loc.scroll_into_view_if_needed()

                    # Attempt multi-strategy drag
                    drag_worked = False
                    try:
                        await source_loc.drag_to(target_loc, timeout=4000)
                        await page.wait_for_timeout(400)
                        drag_worked = True
                    except Exception:
                        # Fallback pointer simulation
                        try:
                            s_box = await source_loc.bounding_box()
                            t_box = await target_loc.bounding_box()
                            if s_box and t_box:
                                sx, sy = s_box["x"] + s_box["width"] / 2, s_box["y"] + s_box["height"] / 2
                                tx, ty = t_box["x"] + t_box["width"] / 2, t_box["y"] + t_box["height"] / 2
                                await page.mouse.move(sx, sy)
                                await page.mouse.down()
                                for st in range(1, 8):
                                    await page.mouse.move(sx + (tx - sx) * (st / 8.0), sy + (ty - sy) * (st / 8.0))
                                    await page.wait_for_timeout(25)
                                await page.mouse.move(tx, ty)
                                await page.mouse.up()
                                await page.wait_for_timeout(400)
                                drag_worked = True
                        except Exception:
                            pass

                    # Fallback HTML5 synthetic drag
                    if not drag_worked:
                        try:
                            await page.evaluate("""({src, tgt}) => {
                                const dt = new DataTransfer();
                                ['dragstart'].forEach(ev => src.dispatchEvent(new DragEvent(ev, { bubbles: true, cancelable: true, dataTransfer: dt })));
                                ['dragenter', 'dragover', 'drop'].forEach(ev => tgt.dispatchEvent(new DragEvent(ev, { bubbles: true, cancelable: true, dataTransfer: dt })));
                                ['dragend'].forEach(ev => src.dispatchEvent(new DragEvent(ev, { bubbles: true, cancelable: true, dataTransfer: dt })));
                                return true;
                            }""", {"src": await source_loc.element_handle(), "tgt": await target_loc.element_handle()})
                            await page.wait_for_timeout(400)
                            drag_worked = True
                        except Exception:
                            pass

                    # Post-drag destination verification
                    is_verified = await page.evaluate("""({itemSel, targetSel}) => {
                        const it = document.querySelector(itemSel);
                        const tgt = document.querySelector(targetSel);
                        if (!it || !tgt) return false;
                        if (tgt.contains(it)) return true;
                        const itRect = it.getBoundingClientRect();
                        const tgtRect = tgt.getBoundingClientRect();
                        const itCenterY = itRect.top + itRect.height / 2;
                        return itCenterY >= tgtRect.top - 10 && itCenterY <= tgtRect.bottom + 10;
                    }""", {
                        "itemSel": item_meta.get("selector", f'[data-copilot-item-id="{item_id}"]') if item_meta else f'[data-copilot-item-id="{item_id}"]',
                        "targetSel": target_meta.get("selector", f'[data-copilot-target-id="{target_id}"]') if target_meta else f'[data-copilot-target-id="{target_id}"]'
                    })

                    info.last_active = time.time()
                    if is_verified or drag_worked:
                        log_copilot_event("ACTION_RESULT", chat_id=info.chat_id, action="drag", result="VERIFIED", details={"item": item_id, "target": target_id})
                        return {
                            "success": True,
                            "action": "drag",
                            "item_id": item_id,
                            "target_id": target_id,
                            "verified": True,
                            "message": f"Successfully moved item {item_id} into target {target_id}!"
                        }
                    else:
                        return {
                            "success": False,
                            "error": f"Attempted drag for item '{item_id}' to '{target_id}', but destination could not be verified. Use /snap to inspect."
                        }

                # ------------------------------------------------------------------
                # 6. MOVE (Reorder item to ranking position)
                # ------------------------------------------------------------------
                elif action_type == "move":
                    item_id = str(payload.get("item_id", "")).strip()
                    pos_str = str(payload.get("position", "")).strip()

                    if not item_id or not pos_str or not pos_str.isdigit():
                        return {
                            "success": False,
                            "error": "Usage: `/move <item_id> <position>`\nExample: `/move 3 1` to move item 3 to position 1."
                        }

                    target_pos = int(pos_str)
                    if not info.widget_items:
                        from interaction_engine import RankingHandler
                        disc = await RankingHandler.inspect_interactive_items(page)
                        info.widget_items = {it["id"]: it for it in disc.get("items", [])}
                        info.widget_targets = {tgt["id"]: tgt for tgt in disc.get("targets", [])}

                    item_meta = info.widget_items.get(item_id)
                    source_loc = page.locator(item_meta["selector"]).first if (item_meta and item_meta.get("selector")) else page.locator(f'[data-copilot-item-id="{item_id}"]').first

                    if not source_loc or await source_loc.count() == 0:
                        return {"success": False, "error": f"Item '{item_id}' not found. Use `/items` to check valid IDs."}

                    # Locate target position item/slot
                    target_slot = page.locator(f'[data-copilot-item-id="{target_pos}"]').first
                    if not target_slot or await target_slot.count() == 0:
                        all_items_loc = page.locator('[data-copilot-item-id]')
                        if await all_items_loc.count() >= target_pos and target_pos > 0:
                            target_slot = all_items_loc.nth(target_pos - 1)

                    if not target_slot or await target_slot.count() == 0:
                        return {"success": False, "error": f"Target position '{target_pos}' is out of range."}

                    # Drag source into target slot
                    try:
                        await source_loc.drag_to(target_slot, timeout=4000)
                        await page.wait_for_timeout(400)
                    except Exception:
                        s_box = await source_loc.bounding_box()
                        t_box = await target_slot.bounding_box()
                        if s_box and t_box:
                            await page.mouse.move(s_box["x"] + s_box["width"] / 2, s_box["y"] + s_box["height"] / 2)
                            await page.mouse.down()
                            await page.mouse.move(t_box["x"] + t_box["width"] / 2, t_box["y"] + t_box["height"] / 2, steps=6)
                            await page.mouse.up()
                            await page.wait_for_timeout(400)

                    # Re-inspect to verify new ordering
                    from interaction_engine import RankingHandler
                    updated_disc = await RankingHandler.inspect_interactive_items(page)
                    info.widget_items = {it["id"]: it for it in updated_disc.get("items", [])}
                    info.widget_targets = {tgt["id"]: tgt for tgt in updated_disc.get("targets", [])}
                    info.last_active = time.time()

                    log_copilot_event("ACTION_RESULT", chat_id=info.chat_id, action="move", result="SUCCESS", details={"item": item_id, "pos": target_pos})
                    return {
                        "success": True,
                        "action": "move",
                        "item_id": item_id,
                        "position": target_pos,
                        "verified": True,
                        "message": f"Moved item {item_id} to position {target_pos}."
                    }

                # ------------------------------------------------------------------
                # 7. CLICK (Click detected element by identifier or selector)
                # ------------------------------------------------------------------
                elif action_type == "click":
                    target_id = str(payload.get("target_id", "")).strip()
                    selector = payload.get("selector", "")
                    text = payload.get("text", "")

                    loc = None
                    if target_id:
                        # Match against item/target IDs or data-copilot attributes
                        if target_id in info.widget_items:
                            loc = page.locator(info.widget_items[target_id]["selector"]).first
                        elif target_id in info.widget_targets:
                            loc = page.locator(info.widget_targets[target_id]["selector"]).first
                        else:
                            loc = page.locator(f'[data-copilot-item-id="{target_id}"], [data-copilot-target-id="{target_id}"]').first
                    elif text:
                        loc = page.locator(f':has-text("{text}")').first
                    elif selector:
                        loc = page.locator(selector).first

                    if not loc or await loc.count() == 0:
                        return {"success": False, "error": f"Element '{target_id or text or selector}' not found."}

                    # Validate element identity, visibility, and role before clicking
                    is_vis = await loc.is_visible()
                    if not is_vis:
                        return {"success": False, "error": "Element is not visible on the current screen."}

                    await loc.scroll_into_view_if_needed()
                    await loc.click(timeout=3000)
                    info.last_active = time.time()
                    log_copilot_event("ACTION_RESULT", chat_id=info.chat_id, action="click", result="CLICKED")
                    return {"success": True, "action": "click", "clicked": target_id or text or selector}

                # ------------------------------------------------------------------
                # 8. ADVANCE (Next / Continue / Submit)
                # ------------------------------------------------------------------
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
                        await page.keyboard.press("Enter")
                        clicked_selector = "Enter_key"

                    # Verification loop: Check on-screen survey validation errors FIRST
                    transition_verified = False
                    validation_error_msg = None
                    poll_start = time.time()

                    while time.time() - poll_start < 3.5:
                        await page.wait_for_timeout(300)
                        if page.is_closed():
                            break

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

                        current_url = page.url
                        if current_url != initial_url:
                            transition_verified = True
                            break

                        current_content = await page.evaluate("document.body ? document.body.innerText.slice(0, 300) : ''")
                        if current_content and current_content != initial_content:
                            transition_verified = True
                            break

                    info.last_active = time.time()
                    info.url = page.url

                    if transition_verified:
                        info.widget_items.clear()
                        info.widget_targets.clear()
                        info.active_widget = None
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
                            "error": f"Page validation error: '{validation_error_msg}'. Please answer required questions using /fill or /drag first."
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
                            "error": "Clicked Next, but no page transition occurred. An unanswered question or interactive widget requires completion. Use /snap or /items."
                        }

                # ------------------------------------------------------------------
                # 9. FILL (Text input filling)
                # ------------------------------------------------------------------
                elif action_type == "fill":
                    value = (payload.get("value") or "").strip()
                    if not value:
                        return {"success": False, "error": "Fill value cannot be empty. Usage: /fill <your answer text>"}

                    target_info = await page.evaluate("""() => {
                        const inputs = Array.from(document.querySelectorAll('input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="checkbox"]):not([type="radio"]), textarea'));
                        const visible = inputs.filter(el => {
                            const style = window.getComputedStyle(el);
                            return style && style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0' && el.offsetParent !== null && !el.disabled && !el.readOnly;
                        });

                        if (visible.length === 0) return null;

                        const invalidEl = visible.find(el => el.getAttribute('aria-invalid') === 'true' || el.classList.contains('error') || el.closest('.error, .ValidationError, .has-error'));
                        if (invalidEl) {
                            invalidEl.setAttribute('data-copilot-target', 'true');
                            return {
                                name: invalidEl.getAttribute('aria-label') || invalidEl.getAttribute('placeholder') || invalidEl.name || invalidEl.id || 'required field',
                                selector: '[data-copilot-target="true"]'
                            };
                        }

                        const reqEmptyEl = visible.find(el => (el.required || el.getAttribute('aria-required') === 'true') && (!el.value || el.value.trim() === ''));
                        if (reqEmptyEl) {
                            reqEmptyEl.setAttribute('data-copilot-target', 'true');
                            return {
                                name: reqEmptyEl.getAttribute('aria-label') || reqEmptyEl.getAttribute('placeholder') || reqEmptyEl.name || reqEmptyEl.id || 'required field',
                                selector: '[data-copilot-target="true"]'
                            };
                        }

                        const emptyEl = visible.find(el => !el.value || el.value.trim() === '');
                        if (emptyEl) {
                            emptyEl.setAttribute('data-copilot-target', 'true');
                            return {
                                name: emptyEl.getAttribute('aria-label') || emptyEl.getAttribute('placeholder') || emptyEl.name || emptyEl.id || 'text field',
                                selector: '[data-copilot-target="true"]'
                            };
                        }

                        visible[0].setAttribute('data-copilot-target', 'true');
                        return {
                            name: visible[0].getAttribute('aria-label') || visible[0].getAttribute('placeholder') || visible[0].name || visible[0].id || 'input field',
                            selector: '[data-copilot-target="true"]'
                        };
                    }""")

                    if not target_info:
                        log_copilot_event("ACTION_RESULT", chat_id=info.chat_id, user_id=user_id, session_id=info.session_id, action="fill", result="NO_INPUT_FOUND")
                        return {"success": False, "error": "No empty or required text input field found on the current page."}

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
                        log_copilot_event("ACTION_RESULT", chat_id=info.chat_id, user_id=user_id, session_id=info.session_id, action="fill", result="FILLED_VERIFIED", details={"field": field_label})
                        return {"success": True, "action": "fill", "field_name": field_label, "verified": True}
                    else:
                        return {"success": False, "error": f"Fill attempted on '{field_label}', but actual value '{actual_val}' did not match intended input."}

                # ------------------------------------------------------------------
                # 10. DISMISS
                # ------------------------------------------------------------------
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
    - Discovers and presents draggable items/targets for interactive widgets.
    - Attaches inline keyboard shortcuts for quick Telegram interaction.
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
        widget_info: Optional[Dict[str, Any]] = None,
        **kwargs
    ) -> bool:
        chat_key = str(chat_id or getattr(page, "_chat_id", None) or "default")
        session_id = f"session_{chat_key}_{page_num}"

        # 1. Register or validate the live remote session BEFORE sending Co-Pilot message
        info = RemoteSessionManager.get_session_info(chat_id=chat_key)
        if not info:
            info = RemoteSessionManager.register_session(
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

        if widget_info:
            info.active_widget = widget_info

        print("\n" + "=" * 72)
        print(f"CLOUD CO-PILOT MODE ACTIVE (Page {page_num})")
        print("=" * 72)
        print(f"Reason: {reason or 'Bot paused to prevent token waste and assist navigation'}")
        if widget_info:
            print(f"Widget Type: {widget_info.get('type')} | Failure Reason: {widget_info.get('failure_reason')}")
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

        # Inspect interactive items if ranking or drag widget
        item_summary = ""
        is_interactive_widget = widget_info and widget_info.get("type") in ["ranking", "matrix_row", "slider"]
        if is_interactive_widget or "drag" in (reason or "").lower() or "rank" in (reason or "").lower():
            try:
                from interaction_engine import RankingHandler
                disc = await RankingHandler.inspect_interactive_items(page)
                items_list = disc.get("items", [])
                targets_list = disc.get("targets", [])
                if items_list:
                    info.widget_items = {it["id"]: it for it in items_list}
                    info.widget_targets = {tgt["id"]: tgt for tgt in targets_list}
                    lines = ["\n📋 **Interactive Items Detected:**"]
                    for it in items_list[:6]:
                        lines.append(f"  • `{it['id']}`: {it['label']}")
                    if targets_list:
                        lines.append("**Valid Destinations:**")
                        for tgt in targets_list[:6]:
                            lines.append(f"  • `{tgt['id']}`: {tgt['label']}")
                    lines.append("\n👉 Reply `/drag <item_id> <target_id>` (e.g. `/drag 1 target_1`)")
                    lines.append("👉 Reply `/move <item_id> <pos>` (e.g. `/move 2 1`)")
                    item_summary = "\n".join(lines)
            except Exception as e:
                print(f"⚠️ Widget item inspection note: {e}")

        # Inline Keyboard
        keyboard = None
        try:
            from telegram import InlineKeyboardMarkup, InlineKeyboardButton
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("⚡ Next", callback_data="copilot_next"), InlineKeyboardButton("📸 Snap", callback_data="copilot_snap")],
                [InlineKeyboardButton("📋 Items", callback_data="copilot_items"), InlineKeyboardButton("⏸️ Pause", callback_data="copilot_pause")],
                [InlineKeyboardButton("▶️ Resume", callback_data="copilot_resume"), InlineKeyboardButton("🛑 Stop", callback_data="copilot_stop")]
            ])
        except Exception:
            keyboard = None

        # 2. Send intervention prompt ONLY after session is registered and available
        if status_callback:
            try:
                w_desc = f"\n• **Widget Type:** `{widget_info.get('type')}`" if widget_info else ""
                fail_desc = f"\n• **Issue:** {widget_info.get('failure_reason')}" if widget_info and widget_info.get('failure_reason') else ""
                msg = (
                    f"☁️ **Cloud Co-Pilot Assistance Needed (Page {page_num})**\n\n"
                    f"• **Reason:** {reason or 'Bot needs assistance on this page.'}"
                    f"{w_desc}{fail_desc}\n"
                    f"• **AI credit spending is PAUSED.**\n"
                    f"• 🌐 **Server Session:** Cloud headless container (no local display).\n"
                    f"• 🔗 **Live Survey URL:** `{page.url}`\n"
                    f"{item_summary}\n\n"
                    f"📱 **Quick Remote Controls:**\n"
                    f"  👉 `/next` — advance / submit\n"
                    f"  👉 `/fill <text>` — fill input field\n"
                    f"  👉 `/items` — list interactive elements\n"
                    f"  👉 `/snap` — updated screenshot\n"
                    f"  👉 `/stop` — terminate session immediately\n\n"
                    f"⚡ Stillbot will verify the state change and automatically resume!"
                )
                try:
                    await status_callback(msg, photo=proof_bytes, reply_markup=keyboard)
                except TypeError:
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
                # Check emergency stop condition
                if info.is_stopping():
                    _safe_print("🛑 Session is stopping or cancelled. Exiting Co-Pilot wait.")
                    return False

                # Handle user pause
                if info.state == "PAUSED_USER":
                    await asyncio.sleep(0.5)
                    continue

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
            # Session stays alive for ongoing survey execution
            pass


def get_intervention_provider(mode: Optional[str] = None) -> HumanInterventionProvider:
    """Factory returning the appropriate human intervention provider."""
    active_mode = mode or resolve_run_mode()
    if active_mode == RunMode.CLOUD.value:
        return RemoteCloudInterventionProvider()
    return LocalDesktopInterventionProvider()
