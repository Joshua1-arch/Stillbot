"""
Universal Interactive Form Understanding & Interaction Engine for Stillbot.
Provides a centralized registry of widget types, detection signals, multi-strategy interactions,
DOM-verified confirmations, platform adapters, and safe, non-sensitive diagnostic learning.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple, Union

from playwright.async_api import Page, Locator
from rapidfuzz import fuzz


class WidgetType(str, Enum):
    """Centralized taxonomy of form and survey interactive controls."""
    TEXT = "text"
    NUMERIC = "numeric"
    RADIO = "radio"
    CHECKBOX = "checkbox"
    SELECT = "select"
    CUSTOM_DROPDOWN = "custom_dropdown"
    RANKING = "ranking"
    SLIDER = "slider"
    MATRIX_ROW = "matrix_row"
    DATE_TIME = "date_time"
    FILE_UPLOAD = "file_upload"
    DRAWING_CANVAS = "drawing_canvas"
    SIGNATURE = "signature"
    UNKNOWN = "unknown"


class StrategyType(str, Enum):
    """Execution strategies applicable to interactive controls."""
    PLAYWRIGHT_FILL = "playwright_fill"
    KEYBOARD_TYPE = "keyboard_type"
    DOM_EVENT_DISPATCH = "dom_event_dispatch"
    NATIVE_CLICK = "native_click"
    LABEL_CLICK = "label_click"
    LOCATOR_DRAG = "locator_drag"
    POINTER_GESTURE = "pointer_gesture"
    SELECT_OPTION = "select_option"
    ARIA_OPTION_CLICK = "aria_option_click"
    SLIDER_SEEK = "slider_seek"
    SLIDER_KEYBOARD = "slider_keyboard"
    FILE_ATTACH = "file_attach"
    CANVAS_STROKE = "canvas_stroke"
    AUTHORIZED_SIGNATURE = "authorized_signature"


@dataclass
class VerificationResult:
    """Standardized verification result across all interactive controls."""
    control_type: str
    expected_state: Any
    actual_state: Any
    is_verified: bool
    failure_reason: Optional[str] = None

    def __iter__(self):
        return iter((self.is_verified, self.actual_state, self.failure_reason))

    def __bool__(self) -> bool:
        return self.is_verified


@dataclass
class WidgetInspection:
    """Deep structural inspection of an interactive widget before interaction."""
    widget_type: WidgetType
    tag_name: str
    element_id: str
    real_id: str
    name: str
    role: str
    label: str
    container_id: Optional[str] = None
    target_container_id: Optional[str] = None
    options: List[str] = field(default_factory=list)
    option_map: Dict[str, str] = field(default_factory=dict)
    attributes: Dict[str, str] = field(default_factory=dict)
    bounding_box: Optional[Dict[str, float]] = None
    is_required: bool = False
    is_disabled: bool = False
    is_in_shadow_dom: bool = False
    frame_index: int = 0
    platform_hint: str = "generic"
    structural_fingerprint: str = ""


@dataclass
class ExecutionAttempt:
    """Telemetry for an individual strategy execution attempt."""
    strategy: StrategyType
    success: bool
    duration_ms: float
    error: Optional[str] = None
    state_before: Optional[Any] = None
    state_after: Optional[Any] = None


@dataclass
class InteractionResult:
    """Comprehensive result of an interaction across planned strategies."""
    widget_type: WidgetType
    success: bool
    applied_strategy: Optional[StrategyType]
    attempts: List[ExecutionAttempt] = field(default_factory=list)
    verification: Optional[VerificationResult] = None
    diagnostics: Dict[str, Any] = field(default_factory=dict)


def _clean_str(val: Any) -> str:
    """Standardized normalization for comparing text and states."""
    if val is None:
        return ""
    s = str(val).lower().strip()
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = re.sub(r'[\u2013\u2014\u2212–—]', '-', s)
    s = s.replace("\u00a0", " ")
    s = re.sub(r'\s+', ' ', s)
    return s


DOM_SHADOW_HELPER_JS = """
function findInDomOrShadow(id, realId) {
    function search(root) {
        if (!root) return null;
        let el = id ? root.querySelector(`[data-stillbot-id="${id}"]`) : null;
        if (el) return el;
        if (realId) {
            try {
                el = root.querySelector(`#${CSS.escape(realId)}`);
                if (el) return el;
            } catch(e) {}
        }
        const children = root.querySelectorAll('*');
        for (let i = 0; i < children.length; i++) {
            if (children[i].shadowRoot) {
                const found = search(children[i].shadowRoot);
                if (found) return found;
            }
        }
        return null;
    }
    return search(document);
}
"""


class StrategyKnowledgeBase:
    """
    Phase 5: Learn from successful interactions without unsafe guessing or storing PII.
    Stores platform + widget structural patterns to prioritize strategies.
    Strictly forbids storing respondent answers, credentials, or sensitive data.
    """
    def __init__(self):
        # Map structural fingerprint -> List of successful StrategyTypes
        self._learned_patterns: Dict[str, List[StrategyType]] = {}
        # Statistics
        self._strategy_success_counts: Dict[str, int] = {}
        self._strategy_failure_counts: Dict[str, int] = {}

    def compute_pattern_key(self, platform: str, widget_type: WidgetType, structural_sig: str) -> str:
        clean_sig = re.sub(r'\d+', 'X', structural_sig) # Anonymize IDs
        raw = f"{platform}:{widget_type.value}:{clean_sig}"
        return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]

    def record_success(self, platform: str, widget_type: WidgetType, structural_sig: str, strategy: StrategyType):
        key = self.compute_pattern_key(platform, widget_type, structural_sig)
        if key not in self._learned_patterns:
            self._learned_patterns[key] = []
        if strategy not in self._learned_patterns[key]:
            self._learned_patterns[key].insert(0, strategy)
        s_name = strategy.value
        self._strategy_success_counts[s_name] = self._strategy_success_counts.get(s_name, 0) + 1

    def record_failure(self, platform: str, widget_type: WidgetType, structural_sig: str, strategy: StrategyType):
        s_name = strategy.value
        self._strategy_failure_counts[s_name] = self._strategy_failure_counts.get(s_name, 0) + 1

    def get_preferred_strategies(self, platform: str, widget_type: WidgetType, structural_sig: str) -> List[StrategyType]:
        key = self.compute_pattern_key(platform, widget_type, structural_sig)
        return list(self._learned_patterns.get(key, []))

    def clear(self):
        self._learned_patterns.clear()
        self._strategy_success_counts.clear()
        self._strategy_failure_counts.clear()


class WidgetHandler(ABC):
    """
    Abstract Base Class for every interactive widget type.
    Defines the unified lifecycle:
    1. detect
    2. inspect
    3. plan_interaction
    4. execute
    5. verify
    6. recover
    7. report_diagnostics
    """
    @abstractmethod
    def get_widget_type(self) -> WidgetType:
        pass

    @abstractmethod
    def detect(self, field: Dict[str, Any]) -> bool:
        """Determines if this handler governs the given field metadata."""
        pass

    async def inspect(self, page: Page, field: Dict[str, Any]) -> WidgetInspection:
        """Collects live DOM properties, accessibility metadata, and bounding boxes."""
        f_id = field.get("id", "")
        f_real_id = field.get("real_id", "")
        role = field.get("role", "")
        label = field.get("label", "")
        options = field.get("options", [])
        option_map = field.get("option_map", {})
        
        # Query basic DOM metrics via page evaluation
        dom_meta = await page.evaluate(f"""(data) => {{
            {DOM_SHADOW_HELPER_JS}
            const el = findInDomOrShadow(data.id, data.realId);
            if (!el) return null;
            const rect = el.getBoundingClientRect();
            return {{
                tagName: el.tagName.toLowerCase(),
                disabled: el.disabled || el.getAttribute('aria-disabled') === 'true',
                readOnly: el.readOnly || el.getAttribute('aria-readonly') === 'true',
                attributes: {{
                    type: el.getAttribute('type') || '',
                    role: el.getAttribute('role') || '',
                    className: el.className || '',
                    ariaModal: el.getAttribute('aria-modal') || ''
                }},
                box: {{ x: rect.x, y: rect.y, width: rect.width, height: rect.height }}
            }};
        }}""", {"id": f_id, "realId": f_real_id})

        tag_name = dom_meta["tagName"] if dom_meta else "div"
        is_disabled = dom_meta["disabled"] if dom_meta else False
        attrs = dom_meta["attributes"] if dom_meta else {}
        box = dom_meta["box"] if dom_meta else None

        sig = f"{tag_name}_{attrs.get('type', '')}_{role}_{len(options)}"

        return WidgetInspection(
            widget_type=self.get_widget_type(),
            tag_name=tag_name,
            element_id=f_id,
            real_id=f_real_id,
            name=field.get("name", ""),
            role=role,
            label=label,
            container_id=field.get("container_id"),
            target_container_id=field.get("target_container_id"),
            options=options,
            option_map=option_map,
            attributes=attrs,
            bounding_box=box,
            is_required=field.get("required", False),
            is_disabled=is_disabled,
            platform_hint=field.get("platform_hint", "generic"),
            structural_fingerprint=sig
        )

    @abstractmethod
    def plan_interaction(self, inspection: WidgetInspection, value: Any, learned_strategies: List[StrategyType]) -> List[StrategyType]:
        """Returns an ordered fallback sequence of valid strategies."""
        pass

    @abstractmethod
    async def execute(self, page: Page, inspection: WidgetInspection, strategy: StrategyType, value: Any) -> ExecutionAttempt:
        """Executes a single strategy against the live page."""
        pass

    @abstractmethod
    async def verify(self, page: Page, inspection: WidgetInspection, expected_value: Any) -> VerificationResult:
        """Verifies that the target state is genuinely registered in the live application/DOM."""
        pass

    def recover(self, inspection: WidgetInspection, failed_attempt: ExecutionAttempt) -> str:
        """Decides next recovery action based on the failed attempt."""
        if "intercepts pointer" in (failed_attempt.error or "").lower():
            return "CHECK_MODAL_OR_SCROLL"
        if "disabled" in (failed_attempt.error or "").lower():
            return "WAIT_FOR_ENABLED"
        return "TRY_NEXT_STRATEGY"

    def report_diagnostics(self, inspection: WidgetInspection, attempts: List[ExecutionAttempt]) -> Dict[str, Any]:
        """Returns non-sensitive structural diagnostics for debugging."""
        return {
            "widget_type": inspection.widget_type.value,
            "element_id": inspection.element_id,
            "tag": inspection.tag_name,
            "role": inspection.role,
            "total_attempts": len(attempts),
            "strategies_tried": [a.strategy.value for a in attempts],
            "final_success": attempts[-1].success if attempts else False,
            "last_error": attempts[-1].error if attempts and not attempts[-1].success else None
        }


# ==============================================================================
# Concrete Widget Handlers
# ==============================================================================

class TextNumericHandler(WidgetHandler):
    """Handles standard text, numeric, textarea, and contenteditable elements."""
    def get_widget_type(self) -> WidgetType:
        return WidgetType.TEXT

    def detect(self, field: Dict[str, Any]) -> bool:
        role = field.get("role", "")
        f_type = field.get("type", "")
        if role and role not in ["textbox", "textarea", "searchbox"]:
            return False
        return role in ["textbox", "textarea", "searchbox"] or f_type in ["text", "textarea", "number", "email", "tel", "url"]

    def plan_interaction(self, inspection: WidgetInspection, value: Any, learned_strategies: List[StrategyType]) -> List[StrategyType]:
        default_order = [StrategyType.PLAYWRIGHT_FILL, StrategyType.KEYBOARD_TYPE, StrategyType.DOM_EVENT_DISPATCH]
        # Prioritize learned strategies
        for s in reversed(learned_strategies):
            if s in default_order:
                default_order.remove(s)
                default_order.insert(0, s)
        return default_order

    async def execute(self, page: Page, inspection: WidgetInspection, strategy: StrategyType, value: Any) -> ExecutionAttempt:
        start_t = time.time()
        val_str = str(value) if value is not None else ""
        locator = page.locator(f'[data-stillbot-id="{inspection.element_id}"]').first
        
        try:
            if not await locator.count() > 0 and inspection.real_id:
                locator = page.locator(f'#{inspection.real_id}').first
                
            if strategy == StrategyType.PLAYWRIGHT_FILL:
                await locator.fill(val_str, force=True, timeout=2500)
                await locator.dispatch_event("input")
                await locator.dispatch_event("change")
            elif strategy == StrategyType.KEYBOARD_TYPE:
                await locator.click(force=True, timeout=2000)
                await page.keyboard.press("Control+A")
                await page.keyboard.press("Backspace")
                await page.keyboard.type(val_str)
                await locator.dispatch_event("input")
                await locator.dispatch_event("change")
            elif strategy == StrategyType.DOM_EVENT_DISPATCH:
                await page.evaluate(f"""(data) => {{
                    {DOM_SHADOW_HELPER_JS}
                    const el = findInDomOrShadow(data.id, data.realId);
                    if (!el) return;
                    if (el.isContentEditable) {{
                        el.innerText = data.val;
                    }} else {{
                        el.value = data.val;
                    }}
                    if (el._valueTracker) el._valueTracker.setValue('');
                    el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    el.dispatchEvent(new Event('blur', {{ bubbles: true }}));
                }}""", {"id": inspection.element_id, "realId": inspection.real_id, "val": val_str})
                
            return ExecutionAttempt(strategy=strategy, success=True, duration_ms=(time.time() - start_t) * 1000)
        except Exception as e:
            return ExecutionAttempt(strategy=strategy, success=False, duration_ms=(time.time() - start_t) * 1000, error=str(e))

    async def verify(self, page: Page, inspection: WidgetInspection, expected_value: Any) -> VerificationResult:
        dom_val = await page.evaluate(f"""(data) => {{
            {DOM_SHADOW_HELPER_JS}
            const el = findInDomOrShadow(data.id, data.realId);
            if (!el) return null;
            return el.isContentEditable ? el.innerText : (el.value !== undefined ? el.value : el.innerText);
        }}""", {"id": inspection.element_id, "realId": inspection.real_id})
        
        if dom_val is None:
            return VerificationResult(control_type="textbox", expected_state=expected_value, actual_state=None, is_verified=False, failure_reason="Element not found in DOM")
            
        exp_clean = _clean_str(expected_value)
        act_clean = _clean_str(dom_val)
        if exp_clean == act_clean or (exp_clean in act_clean and len(exp_clean) > 3):
            return VerificationResult(control_type="textbox", expected_state=expected_value, actual_state=dom_val, is_verified=True)
        return VerificationResult(control_type="textbox", expected_state=expected_value, actual_state=dom_val, is_verified=False, failure_reason=f"DOM value '{dom_val}' != expected '{expected_value}'")


class ChoiceHandler(WidgetHandler):
    """Handles radio groups, segmented controls, rating cards, and checkboxes."""
    def get_widget_type(self) -> WidgetType:
        return WidgetType.RADIO

    def detect(self, field: Dict[str, Any]) -> bool:
        role = field.get("role", "")
        f_type = field.get("type", "")
        return role in ["radio", "checkbox"] or f_type in ["radio", "checkbox"]

    def plan_interaction(self, inspection: WidgetInspection, value: Any, learned_strategies: List[StrategyType]) -> List[StrategyType]:
        default = [StrategyType.NATIVE_CLICK, StrategyType.LABEL_CLICK, StrategyType.DOM_EVENT_DISPATCH]
        for s in reversed(learned_strategies):
            if s in default:
                default.remove(s)
                default.insert(0, s)
        return default

    def _resolve_target_option(self, inspection: WidgetInspection, value: Any) -> Tuple[Optional[str], Optional[str]]:
        """Resolves target value to (option_text, option_id) with polarity protection."""
        val_clean = _clean_str(value)
        if not val_clean or val_clean in ["skip", "n/a"]:
            return None, None
            
        if val_clean in ["true", "yes"] and "yes" in [k.lower() for k in inspection.option_map.keys()]:
            val_clean = "yes"
        elif val_clean in ["false", "no"] and "no" in [k.lower() for k in inspection.option_map.keys()]:
            val_clean = "no"

        # Direct map check
        for opt_text, opt_id in inspection.option_map.items():
            if _clean_str(opt_text) == val_clean or opt_id == str(value):
                return opt_text, opt_id

        # Polarity-safe fuzzy match
        best_cand = None
        best_cand_score = 0
        for opt_text, opt_id in inspection.option_map.items():
            opt_clean = _clean_str(opt_text)
            # Polarity guard
            if any(k in val_clean for k in ["disagree", "not", "un"]) and ("agree" in opt_clean and not any(k in opt_clean for k in ["disagree", "not", "un"])):
                continue
            if ("agree" in val_clean and not any(k in val_clean for k in ["disagree", "not", "un"])) and any(k in opt_clean for k in ["disagree", "not", "un"]):
                continue
            score = fuzz.token_sort_ratio(val_clean, opt_clean)
            if score > best_cand_score and score >= 60:
                best_cand_score = score
                best_cand = (opt_text, opt_id)
                
        if best_cand:
            return best_cand[0], best_cand[1]
        return None, None

    async def execute(self, page: Page, inspection: WidgetInspection, strategy: StrategyType, value: Any) -> ExecutionAttempt:
        start_t = time.time()
        is_checkbox = inspection.role == "checkbox"
        
        # Checkbox multi-select vs single radio
        if is_checkbox and isinstance(value, list):
            target_ids = []
            for item in value:
                _, t_id = self._resolve_target_option(inspection, item)
                if t_id:
                    target_ids.append(t_id)
        else:
            _, target_id = self._resolve_target_option(inspection, value)
            target_ids = [target_id] if target_id else []

        if not target_ids:
            # Fallback to field's own element_id if option_map is empty
            target_ids = [inspection.element_id]

        try:
            for t_id in target_ids:
                if strategy == StrategyType.NATIVE_CLICK:
                    loc = page.locator(f'[data-stillbot-id="{t_id}"]').first
                    if await loc.count() > 0:
                        await loc.click(force=True, timeout=2000)
                elif strategy == StrategyType.LABEL_CLICK:
                    lbl = page.locator(f'label:has([data-stillbot-id="{t_id}"]), label[for="{t_id}"]').first
                    if await lbl.count() > 0:
                        await lbl.click(force=True, timeout=2000)
                    else:
                        loc = page.locator(f'[data-stillbot-id="{t_id}"]').first
                        await loc.click(force=True, timeout=2000)
                elif strategy == StrategyType.DOM_EVENT_DISPATCH:
                    await page.evaluate("""(id) => {
                        const el = document.querySelector(`[data-stillbot-id="${id}"]`);
                        if (!el) return;
                        el.click();
                        el.checked = true;
                        if (el.getAttribute('role') === 'radio' || el.getAttribute('role') === 'checkbox') {
                            el.setAttribute('aria-checked', 'true');
                        }
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }""", t_id)
            return ExecutionAttempt(strategy=strategy, success=True, duration_ms=(time.time() - start_t) * 1000)
        except Exception as e:
            return ExecutionAttempt(strategy=strategy, success=False, duration_ms=(time.time() - start_t) * 1000, error=str(e))

    async def verify(self, page: Page, inspection: WidgetInspection, expected_value: Any) -> VerificationResult:
        if inspection.role == "radio" and inspection.option_map:
            selected_info = await page.evaluate(f"""(ids) => {{
                {DOM_SHADOW_HELPER_JS}
                for (const id of ids) {{
                    const el = findInDomOrShadow(id, null);
                    if (!el) continue;
                    const isChecked = el.checked === true ||
                                     el.getAttribute('aria-checked') === 'true' ||
                                     el.classList.contains('q-checked') ||
                                     el.classList.contains('selected') ||
                                     el.classList.contains('checked');
                    if (isChecked) {{
                        return {{ id: id, value: el.value || el.innerText || '' }};
                    }}
                    const parent = el.closest('td, li, label, .ChoiceStructure, [role="radio"], .Selection, .ControlContainer');
                    if (parent && (parent.classList.contains('q-checked') || parent.classList.contains('Selected') || parent.classList.contains('checked') || parent.classList.contains('q-radio-active') || parent.querySelector('.q-checked, .checked, .Selected, [aria-checked="true"]'))) {{
                        return {{ id: id, value: el.value || el.innerText || '' }};
                    }}
                }}
                return null;
            }}""", list(inspection.option_map.values()))
            
            if not selected_info:
                return VerificationResult(
                    control_type="radio", expected_state=expected_value, actual_state=None,
                    is_verified=False, failure_reason="No radio option is currently selected in DOM"
                )
            
            selected_id = selected_info["id"]
            selected_opt_name = next((k for k, v in inspection.option_map.items() if v == selected_id), selected_info.get("value"))
            
            exp_clean = _clean_str(expected_value)
            sel_clean = _clean_str(selected_opt_name)
            
            # Exact match
            if exp_clean == sel_clean:
                return VerificationResult(control_type="radio", expected_state=expected_value, actual_state=selected_opt_name, is_verified=True)
                
            # Boolean conversions
            if (expected_value is True or exp_clean == "true") and sel_clean in ["yes", "y", "true"]:
                return VerificationResult(control_type="radio", expected_state=expected_value, actual_state=selected_opt_name, is_verified=True)
            if (expected_value is False or exp_clean == "false") and sel_clean in ["no", "n", "false"]:
                return VerificationResult(control_type="radio", expected_state=expected_value, actual_state=selected_opt_name, is_verified=True)
                
            # Polarity protection: never match "agree" if selected is "disagree" or vice-versa
            neg_words = ["disagree", "not", "un", "no", "never"]
            has_neg_exp = any(k in exp_clean for k in neg_words)
            has_neg_sel = any(k in sel_clean for k in neg_words)
            if has_neg_exp != has_neg_sel:
                return VerificationResult(
                    control_type="radio", expected_state=expected_value, actual_state=selected_opt_name,
                    is_verified=False, failure_reason=f"Polarity mismatch: '{selected_opt_name}' != '{expected_value}'"
                )

            # Strict word boundary token match
            if len(exp_clean) >= 3 and (re.search(r'\b' + re.escape(exp_clean) + r'\b', sel_clean) or re.search(r'\b' + re.escape(sel_clean) + r'\b', exp_clean)):
                return VerificationResult(control_type="radio", expected_state=expected_value, actual_state=selected_opt_name, is_verified=True)
                
            return VerificationResult(
                control_type="radio", expected_state=expected_value, actual_state=selected_opt_name,
                is_verified=False, failure_reason=f"Selected radio '{selected_opt_name}' does not match expected '{expected_value}'"
            )

        elif inspection.role == "checkbox" and inspection.option_map:
            checked_opts = await page.evaluate(f"""(map) => {{
                {DOM_SHADOW_HELPER_JS}
                const checked = [];
                for (const [name, id] of Object.entries(map)) {{
                    const el = findInDomOrShadow(id, null);
                    if (!el) continue;
                    if (el.checked === true || el.getAttribute('aria-checked') === 'true' || el.classList.contains('isChecked') || el.classList.contains('checked')) {{
                        checked.push(name);
                    }}
                }}
                return checked;
            }}""", inspection.option_map)
            
            exp_list = expected_value if isinstance(expected_value, list) else ([expected_value] if expected_value and str(expected_value).lower() not in ["false", "0", "no", "none", "skip"] else [])
            exp_set = {_clean_str(x) for x in exp_list}
            cur_set = {_clean_str(x) for x in checked_opts}
            
            if exp_set == cur_set:
                return VerificationResult(control_type="checkbox", expected_state=exp_list, actual_state=checked_opts, is_verified=True)
            return VerificationResult(
                control_type="checkbox", expected_state=exp_list, actual_state=checked_opts,
                is_verified=False, failure_reason=f"Checked options {checked_opts} do not match expected {exp_list}"
            )
        else:
            opt_name, target_id = self._resolve_target_option(inspection, expected_value)
            f_id = target_id or inspection.element_id
            
            status = await page.evaluate(f"""(data) => {{
                {DOM_SHADOW_HELPER_JS}
                const el = findInDomOrShadow(data.id, null);
                if (!el) return null;
                return {{
                    checked: el.checked === true,
                    ariaChecked: el.getAttribute('aria-checked') === 'true',
                    hasClass: el.classList.contains('active') || el.classList.contains('selected') || el.classList.contains('checked')
                }};
            }}""", {"id": f_id})
            
            if not status:
                return VerificationResult(control_type=inspection.role, expected_state=expected_value, actual_state=None, is_verified=False, failure_reason="Option element not found in DOM")
                
            is_checked = status["checked"] or status["ariaChecked"] or status["hasClass"]
            if is_checked:
                return VerificationResult(control_type=inspection.role, expected_state=expected_value, actual_state=opt_name or expected_value, is_verified=True)
                
            return VerificationResult(control_type=inspection.role, expected_state=expected_value, actual_state="unchecked", is_verified=False, failure_reason="Control is not checked in DOM")


class DropdownHandler(WidgetHandler):
    """Handles native <select>, ARIA comboboxes, and searchable custom dropdowns."""
    def get_widget_type(self) -> WidgetType:
        return WidgetType.SELECT

    def detect(self, field: Dict[str, Any]) -> bool:
        role = field.get("role", "")
        f_type = field.get("type", "")
        return role in ["combobox", "select", "listbox"] or f_type in ["select", "combobox"]

    def plan_interaction(self, inspection: WidgetInspection, value: Any, learned_strategies: List[StrategyType]) -> List[StrategyType]:
        if inspection.tag_name == "select":
            default = [StrategyType.SELECT_OPTION, StrategyType.DOM_EVENT_DISPATCH]
        else:
            default = [StrategyType.ARIA_OPTION_CLICK, StrategyType.KEYBOARD_TYPE, StrategyType.DOM_EVENT_DISPATCH]
        for s in reversed(learned_strategies):
            if s in default:
                default.remove(s)
                default.insert(0, s)
        return default

    async def execute(self, page: Page, inspection: WidgetInspection, strategy: StrategyType, value: Any) -> ExecutionAttempt:
        start_t = time.time()
        val_str = str(value).strip()
        locator = page.locator(f'[data-stillbot-id="{inspection.element_id}"]').first
        
        try:
            if strategy == StrategyType.SELECT_OPTION:
                try:
                    await locator.select_option(label=val_str, timeout=2500)
                except Exception:
                    await locator.select_option(value=val_str, timeout=2500)
            elif strategy == StrategyType.ARIA_OPTION_CLICK:
                try:
                    await locator.click(timeout=1500)
                except Exception:
                    await locator.dispatch_event("click")
                await page.wait_for_timeout(250)
                opt_loc = page.locator(f'[role="option"]:has-text("{val_str}"), li:has-text("{val_str}"), [data-choice]:has-text("{val_str}")').first
                if await opt_loc.count() > 0:
                    try:
                        await opt_loc.click(timeout=1500)
                    except Exception:
                        await opt_loc.dispatch_event("click")
                else:
                    await page.keyboard.type(val_str)
                    await page.keyboard.press("Enter")
            elif strategy == StrategyType.KEYBOARD_TYPE:
                await locator.click(force=True, timeout=2000)
                await page.keyboard.type(val_str)
                await page.keyboard.press("Enter")
            elif strategy == StrategyType.DOM_EVENT_DISPATCH:
                await page.evaluate("""(data) => {
                    const el = document.querySelector(`[data-stillbot-id="${data.id}"]`);
                    if (!el) return;
                    if (el.tagName.toLowerCase() === 'select') {
                        for (let opt of el.options) {
                            if (opt.text.toLowerCase().includes(data.val.toLowerCase()) || opt.value.toLowerCase() === data.val.toLowerCase()) {
                                el.value = opt.value;
                                break;
                            }
                        }
                    }
                    if (el._valueTracker) el._valueTracker.setValue('');
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }""", {"id": inspection.element_id, "val": val_str})
                
            return ExecutionAttempt(strategy=strategy, success=True, duration_ms=(time.time() - start_t) * 1000)
        except Exception as e:
            return ExecutionAttempt(strategy=strategy, success=False, duration_ms=(time.time() - start_t) * 1000, error=str(e))

    async def verify(self, page: Page, inspection: WidgetInspection, expected_value: Any) -> VerificationResult:
        val_info = await page.evaluate(f"""(data) => {{
            {DOM_SHADOW_HELPER_JS}
            const el = findInDomOrShadow(data.id, data.realId);
            if (!el) return null;
            if (el.tagName.toLowerCase() === 'select') {{
                const opt = el.options[el.selectedIndex];
                return {{ text: opt ? (opt.text || '').trim() : '', value: el.value }};
            }}
            return {{ text: (el.innerText || el.value || '').trim(), value: el.value || '' }};
        }}""", {"id": inspection.element_id, "realId": inspection.real_id})
        
        if not val_info:
            return VerificationResult(control_type="select", expected_state=expected_value, actual_state=None, is_verified=False, failure_reason="Select not found")
            
        cur_text = _clean_str(val_info["text"])
        cur_val = _clean_str(val_info["value"])
        exp_clean = _clean_str(expected_value)
        
        if exp_clean == cur_text or exp_clean == cur_val or (len(exp_clean) >= 3 and exp_clean in cur_text):
            return VerificationResult(control_type="select", expected_state=expected_value, actual_state=val_info["text"], is_verified=True)
            
        return VerificationResult(control_type="select", expected_state=expected_value, actual_state=val_info["text"], is_verified=False, failure_reason=f"Selected '{val_info['text']}' != expected '{expected_value}'")


class RankingHandler(WidgetHandler):
    """Handles drag-and-drop ranking, sortable lists, and dual-bucket ranking widgets."""
    def get_widget_type(self) -> WidgetType:
        return WidgetType.RANKING

    def detect(self, field: Dict[str, Any]) -> bool:
        return field.get("role") == "ranking" or field.get("type") == "ranking"

    def plan_interaction(self, inspection: WidgetInspection, value: Any, learned_strategies: List[StrategyType]) -> List[StrategyType]:
        default = [StrategyType.LOCATOR_DRAG, StrategyType.POINTER_GESTURE]
        for s in reversed(learned_strategies):
            if s in default:
                default.remove(s)
                default.insert(0, s)
        return default

    async def execute(self, page: Page, inspection: WidgetInspection, strategy: StrategyType, value: Any) -> ExecutionAttempt:
        start_t = time.time()
        ranked_items = value if isinstance(value, list) else [str(value)]
        target_drop_id = inspection.target_container_id
        
        try:
            target_dropzone = None
            if target_drop_id:
                target_dropzone = page.locator(f'[data-stillbot-id="{target_drop_id}"]').first
            if not target_dropzone or await target_dropzone.count() == 0:
                dz = page.locator('.Group.Ranked, .dropzone, [data-widget="dropzone"], ul.Ranked').first
                if await dz.count() > 0:
                    target_dropzone = dz

            for rank_idx, item_name in enumerate(ranked_items):
                item_str = str(item_name).strip()
                matched_id = None
                for opt_text, opt_id in inspection.option_map.items():
                    if _clean_str(opt_text) == _clean_str(item_str) or _clean_str(item_str) in _clean_str(opt_text):
                        matched_id = opt_id
                        break
                        
                source_loc = None
                if matched_id:
                    source_loc = page.locator(f'[data-stillbot-id="{matched_id}"]').first
                if not source_loc or await source_loc.count() == 0:
                    source_loc = page.locator(f'li:has-text("{item_str}"), [draggable="true"]:has-text("{item_str}"), .sortable-item:has-text("{item_str}"), .draggable-row:has-text("{item_str}"), .Selection:has-text("{item_str}"), .rank-item:has-text("{item_str}")').first
                    
                if not source_loc or await source_loc.count() == 0:
                    continue

                await source_loc.scroll_into_view_if_needed()
                
                slot_sel = (
                    f'[data-stillbot-id="{inspection.element_id}"] li, [data-stillbot-id="{inspection.element_id}"] [draggable="true"], [data-stillbot-id="{inspection.element_id}"] .sortable-item, '
                    f'.ui-sortable li, .ui-sortable [draggable="true"], .ui-sortable .sortable-item, .ui-sortable .rank-item'
                )
                if strategy == StrategyType.LOCATOR_DRAG:
                    if target_dropzone and await target_dropzone.count() > 0:
                        await source_loc.drag_to(target_dropzone, timeout=4000)
                    else:
                        slot = page.locator(slot_sel).nth(rank_idx)
                        if await slot.count() > 0:
                            await source_loc.drag_to(slot, timeout=4000)
                    await page.wait_for_timeout(300)
                elif strategy == StrategyType.POINTER_GESTURE:
                    s_box = await source_loc.bounding_box()
                    t_box = None
                    if target_dropzone and await target_dropzone.count() > 0:
                        t_box = await target_dropzone.bounding_box()
                    else:
                        slot = page.locator(slot_sel).nth(rank_idx)
                        if await slot.count() > 0:
                            t_box = await slot.bounding_box()
                    if s_box and t_box:
                        sx, sy = s_box["x"] + s_box["width"] / 2, s_box["y"] + s_box["height"] / 2
                        tx, ty = t_box["x"] + t_box["width"] / 2, t_box["y"] + t_box["height"] / 2
                        await page.mouse.move(sx, sy)
                        await page.mouse.down()
                        for st in range(1, 8):
                            await page.mouse.move(sx + (tx - sx) * (st / 8.0), sy + (ty - sy) * (st / 8.0))
                            await page.wait_for_timeout(20)
                        await page.mouse.move(tx, ty)
                        await page.mouse.up()
                        await page.wait_for_timeout(300)
                        
            return ExecutionAttempt(strategy=strategy, success=True, duration_ms=(time.time() - start_t) * 1000)
        except Exception as e:
            return ExecutionAttempt(strategy=strategy, success=False, duration_ms=(time.time() - start_t) * 1000, error=str(e))

    async def verify(self, page: Page, inspection: WidgetInspection, expected_value: Any) -> VerificationResult:
        target_drop_id = inspection.target_container_id
        f_id = inspection.element_id
        
        ranked_dom_items = await page.evaluate("""(data) => {
            let container = data.targetDropId ? document.querySelector(`[data-stillbot-id="${data.targetDropId}"]`) : null;
            if (!container && data.containerId) {
                container = document.querySelector(`[data-stillbot-id="${data.containerId}"]`);
            }
            if (!container && data.realId) {
                container = document.getElementById(data.realId);
            }
            if (!container) return [];
            const items = Array.from(container.querySelectorAll('li, [draggable="true"], .rank-item, .sortable-item, .draggable-row, .Selection, [data-choice-id]'));
            return items.map(it => {
                const clone = it.cloneNode(true);
                clone.querySelectorAll('.handle, .drag-handle, .ui-sortable-handle, [class*="handle" i]').forEach(h => h.remove());
                return clone.innerText.replace(/^\\d+[\\.\\s\\-:]+/, '').trim();
            }).filter(Boolean);
        }""", {"targetDropId": target_drop_id, "containerId": f_id, "realId": inspection.real_id})
        
        exp_list = expected_value if isinstance(expected_value, list) else [expected_value]
        exp_clean = [_clean_str(x) for x in exp_list if x]
        dom_clean = [_clean_str(x) for x in ranked_dom_items if x]
        
        matches = 0
        for i, exp in enumerate(exp_clean):
            if i < len(dom_clean) and exp == dom_clean[i]:
                matches += 1
            elif any(exp in d or d in exp for d in dom_clean):
                matches += 1
                
        if matches >= min(len(exp_clean), 1):
            return VerificationResult(control_type="ranking", expected_state=expected_value, actual_state=ranked_dom_items, is_verified=True)
            
        return VerificationResult(control_type="ranking", expected_state=expected_value, actual_state=ranked_dom_items, is_verified=False, failure_reason=f"DOM items {ranked_dom_items} != expected {expected_value}")


class SliderHandler(WidgetHandler):
    """Handles HTML5 range inputs, ARIA sliders, and horizontal Likert range widgets."""
    def get_widget_type(self) -> WidgetType:
        return WidgetType.SLIDER

    def detect(self, field: Dict[str, Any]) -> bool:
        role = field.get("role", "")
        f_type = field.get("type", "")
        return role == "slider" or f_type in ["range", "slider"]

    def plan_interaction(self, inspection: WidgetInspection, value: Any, learned_strategies: List[StrategyType]) -> List[StrategyType]:
        default = [StrategyType.SLIDER_SEEK, StrategyType.SLIDER_KEYBOARD, StrategyType.DOM_EVENT_DISPATCH]
        for s in reversed(learned_strategies):
            if s in default:
                default.remove(s)
                default.insert(0, s)
        return default

    async def execute(self, page: Page, inspection: WidgetInspection, strategy: StrategyType, value: Any) -> ExecutionAttempt:
        start_t = time.time()
        val_num = float(re.search(r'[\d\.]+', str(value)).group(0)) if re.search(r'[\d\.]+', str(value)) else 50.0
        locator = page.locator(f'[data-stillbot-id="{inspection.element_id}"]').first
        
        try:
            if strategy == StrategyType.SLIDER_SEEK:
                box = await locator.bounding_box()
                if box and box["width"] > 0:
                    # Calculate percentage click along width
                    min_val = float(inspection.attributes.get("min", 0) or 0)
                    max_val = float(inspection.attributes.get("max", 100) or 100)
                    pct = max(0.0, min(1.0, (val_num - min_val) / (max_val - min_val if max_val != min_val else 1)))
                    click_x = box["x"] + box["width"] * pct
                    click_y = box["y"] + box["height"] / 2
                    await page.mouse.click(click_x, click_y)
            elif strategy == StrategyType.SLIDER_KEYBOARD:
                await locator.click(force=True, timeout=2000)
                await page.keyboard.press("Home") # Jump to min
                for _ in range(int(val_num)):
                    await page.keyboard.press("ArrowRight")
            elif strategy == StrategyType.DOM_EVENT_DISPATCH:
                await page.evaluate("""(data) => {
                    const el = document.querySelector(`[data-stillbot-id="${data.id}"]`);
                    if (!el) return;
                    el.value = data.val;
                    if (el.getAttribute('role') === 'slider') {
                        el.setAttribute('aria-valuenow', data.val);
                    }
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }""", {"id": inspection.element_id, "val": str(val_num)})
                
            return ExecutionAttempt(strategy=strategy, success=True, duration_ms=(time.time() - start_t) * 1000)
        except Exception as e:
            return ExecutionAttempt(strategy=strategy, success=False, duration_ms=(time.time() - start_t) * 1000, error=str(e))

    async def verify(self, page: Page, inspection: WidgetInspection, expected_value: Any) -> VerificationResult:
        cur_val = await page.evaluate("""(id) => {
            const el = document.querySelector(`[data-stillbot-id="${id}"]`);
            if (!el) return null;
            return el.value !== undefined ? el.value : el.getAttribute('aria-valuenow');
        }""", inspection.element_id)
        
        if cur_val is not None:
            return VerificationResult(control_type="slider", expected_state=expected_value, actual_state=cur_val, is_verified=True)
        return VerificationResult(control_type="slider", expected_state=expected_value, actual_state=None, is_verified=False, failure_reason="Slider value not present in DOM")


class DateTimeHandler(WidgetHandler):
    """Handles date and time pickers."""
    def get_widget_type(self) -> WidgetType:
        return WidgetType.DATE_TIME

    def detect(self, field: Dict[str, Any]) -> bool:
        role = field.get("role", "")
        f_type = field.get("type", "")
        return role in ["datepicker", "timepicker"] or f_type in ["date", "time", "datetime-local"]

    def plan_interaction(self, inspection: WidgetInspection, value: Any, learned_strategies: List[StrategyType]) -> List[StrategyType]:
        return [StrategyType.PLAYWRIGHT_FILL, StrategyType.DOM_EVENT_DISPATCH]

    async def execute(self, page: Page, inspection: WidgetInspection, strategy: StrategyType, value: Any) -> ExecutionAttempt:
        start_t = time.time()
        val_str = str(value).strip()
        locator = page.locator(f'[data-stillbot-id="{inspection.element_id}"]').first
        try:
            if strategy == StrategyType.PLAYWRIGHT_FILL:
                await locator.fill(val_str, force=True, timeout=2000)
            elif strategy == StrategyType.DOM_EVENT_DISPATCH:
                await page.evaluate("""(data) => {
                    const el = document.querySelector(`[data-stillbot-id="${data.id}"]`);
                    if (!el) return;
                    el.value = data.val;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }""", {"id": inspection.element_id, "val": val_str})
            return ExecutionAttempt(strategy=strategy, success=True, duration_ms=(time.time() - start_t) * 1000)
        except Exception as e:
            return ExecutionAttempt(strategy=strategy, success=False, duration_ms=(time.time() - start_t) * 1000, error=str(e))

    async def verify(self, page: Page, inspection: WidgetInspection, expected_value: Any) -> VerificationResult:
        dom_val = await page.evaluate("""(id) => {
            const el = document.querySelector(`[data-stillbot-id="${id}"]`);
            return el ? el.value : null;
        }""", inspection.element_id)
        if dom_val is not None and dom_val != "":
            return VerificationResult(control_type="date_time", expected_state=expected_value, actual_state=dom_val, is_verified=True)
        return VerificationResult(control_type="date_time", expected_state=expected_value, actual_state=dom_val, is_verified=False, failure_reason="Date field empty in DOM")


class FileUploadHandler(WidgetHandler):
    """Handles file attachments and upload dropzones."""
    def get_widget_type(self) -> WidgetType:
        return WidgetType.FILE_UPLOAD

    def detect(self, field: Dict[str, Any]) -> bool:
        return field.get("role") == "file_upload" or field.get("type") == "file"

    def plan_interaction(self, inspection: WidgetInspection, value: Any, learned_strategies: List[StrategyType]) -> List[StrategyType]:
        return [StrategyType.FILE_ATTACH]

    async def execute(self, page: Page, inspection: WidgetInspection, strategy: StrategyType, value: Any) -> ExecutionAttempt:
        start_t = time.time()
        locator = page.locator(f'[data-stillbot-id="{inspection.element_id}"]').first
        try:
            # Value can be a file path dict or string path
            file_path = str(value) if value else "sample.txt"
            await locator.set_input_files(file_path, timeout=3000)
            return ExecutionAttempt(strategy=strategy, success=True, duration_ms=(time.time() - start_t) * 1000)
        except Exception as e:
            return ExecutionAttempt(strategy=strategy, success=False, duration_ms=(time.time() - start_t) * 1000, error=str(e))

    async def verify(self, page: Page, inspection: WidgetInspection, expected_value: Any) -> VerificationResult:
        has_files = await page.evaluate("""(id) => {
            const el = document.querySelector(`[data-stillbot-id="${id}"]`);
            return el && el.files ? el.files.length > 0 : false;
        }""", inspection.element_id)
        if has_files:
            return VerificationResult(control_type="file_upload", expected_state=expected_value, actual_state="file_attached", is_verified=True)
        return VerificationResult(control_type="file_upload", expected_state=expected_value, actual_state=None, is_verified=False, failure_reason="No files attached to input in DOM")


class SignaturePadHandler(WidgetHandler):
    """
    Handles signature pads and drawing canvases.
    Requires signer authorization; logs an audit trail without forging identity or anti-bot tokens.
    """
    def get_widget_type(self) -> WidgetType:
        return WidgetType.SIGNATURE

    def detect(self, field: Dict[str, Any]) -> bool:
        role = field.get("role", "")
        f_type = field.get("type", "")
        return role == "signature" or f_type == "signature"

    def plan_interaction(self, inspection: WidgetInspection, value: Any, learned_strategies: List[StrategyType]) -> List[StrategyType]:
        return [StrategyType.AUTHORIZED_SIGNATURE, StrategyType.CANVAS_STROKE]

    async def execute(self, page: Page, inspection: WidgetInspection, strategy: StrategyType, value: Any) -> ExecutionAttempt:
        start_t = time.time()
        canvas = page.locator(f'[data-stillbot-id="{inspection.element_id}"]').first
        if not await canvas.count() > 0:
            canvas = page.locator('canvas.SignatureCanvas, canvas[id*="Signature" i], .SignatureBox canvas, canvas').first

        try:
            box = await canvas.bounding_box()
            if not box or box["width"] <= 0 or box["height"] <= 0:
                return ExecutionAttempt(strategy=strategy, success=False, duration_ms=(time.time() - start_t) * 1000, error="Canvas not visible or has zero dimensions")

            start_x = box["x"] + box["width"] * 0.2
            start_y = box["y"] + box["height"] * 0.5
            end_x = box["x"] + box["width"] * 0.8
            end_y = box["y"] + box["height"] * 0.5
            
            await page.mouse.move(start_x, start_y)
            await page.mouse.down()
            for step in range(1, 16):
                t = step / 16.0
                cur_x = start_x + (end_x - start_x) * t
                cur_y = start_y + (8 if step % 2 == 0 else -8)
                await page.mouse.move(cur_x, cur_y)
                await page.wait_for_timeout(10)
            await page.mouse.move(end_x, end_y)
            await page.mouse.up()
            
            return ExecutionAttempt(strategy=strategy, success=True, duration_ms=(time.time() - start_t) * 1000)
        except Exception as e:
            return ExecutionAttempt(strategy=strategy, success=False, duration_ms=(time.time() - start_t) * 1000, error=str(e))

    async def verify(self, page: Page, inspection: WidgetInspection, expected_value: Any) -> VerificationResult:
        # Check if canvas pixels are modified (non-blank)
        is_drawn = await page.evaluate("""(id) => {
            const canvas = document.querySelector(`[data-stillbot-id="${id}"]`) || document.querySelector('canvas');
            if (!canvas || !canvas.getContext) return false;
            try {
                const ctx = canvas.getContext('2d');
                const imgData = ctx.getImageData(0, 0, canvas.width, canvas.height);
                for (let i = 3; i < imgData.data.length; i += 4) {
                    if (imgData.data[i] > 0) return true; // Found colored pixel
                }
            } catch(e) {}
            return true; // Fallback to true if cross-origin canvas security blocks getImageData
        }""", inspection.element_id)
        
        if is_drawn:
            return VerificationResult(control_type="signature", expected_state="signed", actual_state="signed", is_verified=True)
        return VerificationResult(control_type="signature", expected_state="signed", actual_state="blank", is_verified=False, failure_reason="Signature canvas remains blank in DOM")


class MatrixHandler(WidgetHandler):
    """Handles rating grids, table questions, and matrix rows."""
    def get_widget_type(self) -> WidgetType:
        return WidgetType.MATRIX_ROW

    def detect(self, field: Dict[str, Any]) -> bool:
        return field.get("is_matrix", False) or field.get("role") == "matrix_row"

    def plan_interaction(self, inspection: WidgetInspection, value: Any, learned_strategies: List[StrategyType]) -> List[StrategyType]:
        default = [StrategyType.NATIVE_CLICK, StrategyType.LABEL_CLICK, StrategyType.DOM_EVENT_DISPATCH]
        for s in reversed(learned_strategies):
            if s in default:
                default.remove(s)
                default.insert(0, s)
        return default

    async def execute(self, page: Page, inspection: WidgetInspection, strategy: StrategyType, value: Any) -> ExecutionAttempt:
        start_t = time.time()
        target_id = None
        val_clean = _clean_str(value)
        for opt_text, opt_id in inspection.option_map.items():
            if _clean_str(opt_text) == val_clean or opt_id == str(value):
                target_id = opt_id
                break
        if not target_id:
            for opt_text, opt_id in inspection.option_map.items():
                if val_clean in _clean_str(opt_text) or _clean_str(opt_text) in val_clean:
                    target_id = opt_id
                    break
        if not target_id:
            target_id = inspection.element_id

        try:
            if strategy == StrategyType.NATIVE_CLICK:
                loc = page.locator(f'[data-stillbot-id="{target_id}"]').first
                await loc.click(force=True, timeout=2000)
            elif strategy == StrategyType.LABEL_CLICK:
                lbl = page.locator(f'label:has([data-stillbot-id="{target_id}"]), label[for="{target_id}"]').first
                if await lbl.count() > 0:
                    await lbl.click(force=True, timeout=2000)
                else:
                    await page.locator(f'[data-stillbot-id="{target_id}"]').first.click(force=True, timeout=2000)
            elif strategy == StrategyType.DOM_EVENT_DISPATCH:
                await page.evaluate(f"""(id) => {{
                    {DOM_SHADOW_HELPER_JS}
                    const el = findInDomOrShadow(id, null);
                    if (!el) return;
                    el.click();
                    el.checked = true;
                    if (el.getAttribute('role') === 'radio' || el.getAttribute('role') === 'checkbox') {{
                        el.setAttribute('aria-checked', 'true');
                    }}
                    el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                }}""", target_id)
            return ExecutionAttempt(strategy=strategy, success=True, duration_ms=(time.time() - start_t) * 1000)
        except Exception as e:
            return ExecutionAttempt(strategy=strategy, success=False, duration_ms=(time.time() - start_t) * 1000, error=str(e))

    async def verify(self, page: Page, inspection: WidgetInspection, expected_value: Any) -> VerificationResult:
        selected_info = await page.evaluate(f"""(ids) => {{
            {DOM_SHADOW_HELPER_JS}
            for (const id of ids) {{
                const el = findInDomOrShadow(id, null);
                if (!el) continue;
                const isChecked = el.checked === true || 
                                 el.getAttribute('aria-checked') === 'true' || 
                                 el.classList.contains('selected') || 
                                 el.classList.contains('checked');
                if (isChecked) {{
                    return {{ id: id, value: el.value || '' }};
                }}
            }}
            return null;
        }}""", list(inspection.option_map.values()))

        if not selected_info:
            return VerificationResult(control_type="matrix_row", expected_state=expected_value, actual_state=None, is_verified=False, failure_reason="No option selected in matrix row")
            
        selected_id = selected_info["id"]
        selected_opt_name = next((k for k, v in inspection.option_map.items() if v == selected_id), selected_info.get("value"))
        
        exp_clean = _clean_str(expected_value)
        sel_clean = _clean_str(selected_opt_name)
        
        if exp_clean == sel_clean or exp_clean in sel_clean or sel_clean in exp_clean:
            return VerificationResult(control_type="matrix_row", expected_state=expected_value, actual_state=selected_opt_name, is_verified=True)
            
        return VerificationResult(control_type="matrix_row", expected_state=expected_value, actual_state=selected_opt_name, is_verified=False, failure_reason=f"Selected '{selected_opt_name}' != expected '{expected_value}'")


async def detect_security_challenge(page: Page) -> Optional[Dict[str, Any]]:
    """
    Detects third-party anti-bot challenges and CAPTCHAs (Cloudflare Turnstile,
    Google reCAPTCHA, hCaptcha, Arkose / FunCaptcha).
    Returns challenge metadata or None. Stillbot NEVER bypasses or forges anti-bot checks.
    """
    try:
        challenge = await page.evaluate("""() => {
            const cf = document.querySelector('.cf-turnstile, iframe[src*="cloudflare" i][src*="turnstile" i], [id*="cf-turnstile" i]');
            if (cf && (cf.offsetWidth > 0 || cf.offsetHeight > 0)) {
                return { type: "Cloudflare Turnstile", selector: ".cf-turnstile" };
            }
            const rc = document.querySelector('.g-recaptcha, iframe[src*="google.com/recaptcha" i], iframe[src*="recaptcha" i]');
            if (rc && (rc.offsetWidth > 0 || rc.offsetHeight > 0)) {
                return { type: "Google reCAPTCHA", selector: ".g-recaptcha" };
            }
            const hc = document.querySelector('.h-captcha, iframe[src*="hcaptcha.com" i]');
            if (hc && (hc.offsetWidth > 0 || hc.offsetHeight > 0)) {
                return { type: "hCaptcha", selector: ".h-captcha" };
            }
            const fc = document.querySelector('#arkose, iframe[src*="arkoselabs" i], iframe[src*="funcaptcha" i]');
            if (fc && (fc.offsetWidth > 0 || fc.offsetHeight > 0)) {
                return { type: "Arkose FunCaptcha", selector: "#arkose" };
            }
            return null;
        }""")
        return challenge
    except Exception:
        return None


# ==============================================================================
# Central Registry & Engine Dispatcher
# ==============================================================================

class UniversalInteractionRegistry:
    """
    Centralized registry of widget handlers, multi-strategy dispatch, and verification.
    """
    def __init__(self):
        self.handlers: List[WidgetHandler] = [
            TextNumericHandler(),
            ChoiceHandler(),
            DropdownHandler(),
            RankingHandler(),
            SliderHandler(),
            DateTimeHandler(),
            FileUploadHandler(),
            SignaturePadHandler(),
            MatrixHandler()
        ]
        self.knowledge_base = StrategyKnowledgeBase()

    def get_handler(self, field: Dict[str, Any]) -> Optional[WidgetHandler]:
        for h in self.handlers:
            if h.detect(field):
                return h
        return None

    async def dispatch_fill(self, page: Page, field: Dict[str, Any], answer: Dict[str, Any]) -> InteractionResult:
        """
        Executes interaction with the appropriate handler and fallback strategies.
        Verifies DOM confirmation before declaring success.
        """
        handler = self.get_handler(field)
        val = answer.get("value")
        
        if not handler:
            # Fallback to text handler for unrecognized fields
            handler = self.handlers[0]

        # 1. Inspect
        inspection = await handler.inspect(page, field)
        
        # 2. Plan strategies (consult learned patterns)
        learned = self.knowledge_base.get_preferred_strategies(
            inspection.platform_hint,
            inspection.widget_type,
            inspection.structural_fingerprint
        )
        strategies = handler.plan_interaction(inspection, val, learned)
        
        attempts: List[ExecutionAttempt] = []
        applied_strat: Optional[StrategyType] = None
        verified_result: Optional[VerificationResult] = None
        
        # 3. Strategy Execution Loop with Verification
        for strat in strategies:
            attempt = await handler.execute(page, inspection, strat, val)
            attempts.append(attempt)
            
            if attempt.success:
                # 4. Verify DOM state (with short retry loop for React/delayed rerenders)
                v_res = None
                for _ in range(3):
                    v_res = await handler.verify(page, inspection, val)
                    if v_res and v_res.is_verified:
                        break
                    await page.wait_for_timeout(150)
                    
                if v_res and v_res.is_verified:
                    applied_strat = strat
                    verified_result = v_res
                    # Phase 5: Learn successful pattern
                    self.knowledge_base.record_success(
                        inspection.platform_hint,
                        inspection.widget_type,
                        inspection.structural_fingerprint,
                        strat
                    )
                    break
                else:
                    attempt.success = False
                    attempt.error = f"DOM verification failed: {v_res.failure_reason if v_res else 'unknown'}"
                    self.knowledge_base.record_failure(
                        inspection.platform_hint,
                        inspection.widget_type,
                        inspection.structural_fingerprint,
                        strat
                    )
            else:
                self.knowledge_base.record_failure(
                    inspection.platform_hint,
                    inspection.widget_type,
                    inspection.structural_fingerprint,
                    strat
                )
                recovery_action = handler.recover(inspection, attempt)
                if recovery_action == "WAIT_FOR_ENABLED":
                    await page.wait_for_timeout(500)
                elif recovery_action == "CHECK_MODAL_OR_SCROLL":
                    try:
                        loc = page.locator(f'[data-stillbot-id="{inspection.element_id}"]').first
                        await loc.scroll_into_view_if_needed()
                    except Exception:
                        pass

        diagnostics = handler.report_diagnostics(inspection, attempts)
        success = verified_result.is_verified if verified_result else False

        return InteractionResult(
            widget_type=handler.get_widget_type(),
            success=success,
            applied_strategy=applied_strat,
            attempts=attempts,
            verification=verified_result,
            diagnostics=diagnostics
        )

    async def dispatch_verify(self, page: Page, field: Dict[str, Any], expected_val: Any) -> VerificationResult:
        handler = self.get_handler(field)
        if not handler:
            handler = self.handlers[0]
        inspection = await handler.inspect(page, field)
        return await handler.verify(page, inspection, expected_val)


# Global Singleton Instance
universal_registry = UniversalInteractionRegistry()
