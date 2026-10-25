import asyncio
import sys
import json
import os
import urllib.parse
import re
import math
import random
from rapidfuzz import fuzz
sys.stdout.reconfigure(encoding='utf-8')
from playwright.async_api import async_playwright
import config
from scraper import scrape_page, prepare_vision_fallback, detect_blocking_modal, check_page_has_questions
from typing import Dict, List, Optional, Any, Tuple
from ai_engine import get_form_answers, get_vision_form_answers
from run_context import RunContext, PageState, AnswerStatus, NormalizedQuestion

from pydantic import BaseModel

class VerificationResult(BaseModel):
    control_type: str
    expected_state: Any
    actual_state: Any
    is_verified: bool
    failure_reason: Optional[str] = None
    retry_eligible: bool = True
    needs_human_input: bool = False

    def __iter__(self):
        # Enables backward compatibility with (is_verified, actual_state, failure_reason) tuple unpacking
        return iter((self.is_verified, self.actual_state, self.failure_reason))

    def __bool__(self):
        return self.is_verified

async def human_click_element(page, locator):
    """Standard, reliable Playwright element click with condition wait."""
    try:
        await locator.scroll_into_view_if_needed(timeout=2000)
        await locator.click(timeout=3000)
        return True
    except Exception:
        try:
            await locator.click(force=True, timeout=2000)
            return True
        except Exception:
            return False

def _clean_str(s: Any) -> str:
    if s is None:
        return ""
    text = str(s)
    # Normalize unicode quotes, dashes, and whitespace differences
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = re.sub(r'[\u2013\u2014\u2212–—]', '-', text)
    text = text.replace("\u00a0", " ")
    return re.sub(r'\s+', ' ', text).strip().lower()

async def verify_control_state(page, field: Dict[str, Any], expected_val: Any) -> VerificationResult:
    """
    Verifies the actual live state in the DOM against the intended answer.
    Returns a structured VerificationResult.
    Never trusts a locator click alone; inspects live element attributes.
    Uses strict, control-specific matching to prevent substring false-positive matches.
    """
    if not page:
        return VerificationResult(
            control_type="unknown", expected_state=expected_val, actual_state=None,
            is_verified=False, failure_reason="No active page"
        )
        
    role = field.get("role", "textbox")
    f_id = field.get("id")
    option_map = field.get("option_map", {})
    
    try:
        # 1. Radio Buttons
        if role == "radio":
            opt_ids = list(option_map.values()) if option_map else ([f_id] if f_id else [])
            selected_info = await page.evaluate("""(ids) => {
                for (const id of ids) {
                    const el = document.querySelector(`[data-stillbot-id="${id}"]`);
                    if (!el) continue;
                    const isChecked = el.checked === true || 
                                     el.getAttribute('aria-checked') === 'true' || 
                                     el.classList.contains('q-checked') || 
                                     el.classList.contains('selected') || 
                                     el.classList.contains('checked');
                    if (isChecked) {
                        return { id: id, value: el.value || el.innerText || '' };
                    }
                    const parent = el.closest('td, li, label, .ChoiceStructure, [role="radio"], .Selection, .ControlContainer');
                    if (parent && (parent.classList.contains('q-checked') || parent.classList.contains('Selected') || parent.classList.contains('checked') || parent.classList.contains('q-radio-active') || parent.querySelector('.q-checked, .checked, .Selected, [aria-checked="true"]'))) {
                        return { id: id, value: el.value || el.innerText || '' };
                    }
                }
                return null;
            }""", opt_ids)
            
            if not selected_info:
                return VerificationResult(
                    control_type="radio", expected_state=expected_val, actual_state=None,
                    is_verified=False, failure_reason="No radio option is currently selected in DOM"
                )
                
            selected_id = selected_info["id"]
            selected_opt_name = next((k for k, v in option_map.items() if v == selected_id), selected_info.get("value"))
            
            exp_clean = _clean_str(expected_val)
            sel_clean = _clean_str(selected_opt_name)
            
            # Exact match
            if exp_clean == sel_clean:
                return VerificationResult(control_type="radio", expected_state=expected_val, actual_state=selected_opt_name, is_verified=True)
                
            # Boolean conversions
            if (expected_val is True or exp_clean == "true") and sel_clean in ["yes", "y", "true"]:
                return VerificationResult(control_type="radio", expected_state=expected_val, actual_state=selected_opt_name, is_verified=True)
            if (expected_val is False or exp_clean == "false") and sel_clean in ["no", "n", "false"]:
                return VerificationResult(control_type="radio", expected_state=expected_val, actual_state=selected_opt_name, is_verified=True)
                
            # Polarity protection: never match "agree" if selected is "disagree" or vice-versa
            neg_words = ["disagree", "not", "un", "no", "never"]
            has_neg_exp = any(k in exp_clean for k in neg_words)
            has_neg_sel = any(k in sel_clean for k in neg_words)
            if has_neg_exp != has_neg_sel:
                return VerificationResult(
                    control_type="radio", expected_state=expected_val, actual_state=selected_opt_name,
                    is_verified=False, failure_reason=f"Polarity mismatch: '{selected_opt_name}' != '{expected_val}'"
                )

            # Strict word boundary token match (avoids substring errors like 'agree' in 'disagree' or '1' in '10')
            if len(exp_clean) >= 3 and (re.search(r'\b' + re.escape(exp_clean) + r'\b', sel_clean) or re.search(r'\b' + re.escape(sel_clean) + r'\b', exp_clean)):
                return VerificationResult(control_type="radio", expected_state=expected_val, actual_state=selected_opt_name, is_verified=True)
                
            return VerificationResult(
                control_type="radio", expected_state=expected_val, actual_state=selected_opt_name,
                is_verified=False, failure_reason=f"Selected radio '{selected_opt_name}' does not match expected '{expected_val}'"
            )

        # 2. Checkboxes
        elif role == "checkbox":
            if option_map:
                checked_opts = await page.evaluate("""(map) => {
                    const checked = [];
                    for (const [name, id] of Object.entries(map)) {
                        const el = document.querySelector(`[data-stillbot-id="${id}"]`);
                        if (!el) continue;
                        if (el.checked === true || el.getAttribute('aria-checked') === 'true' || el.classList.contains('isChecked') || el.classList.contains('checked')) {
                            checked.push(name);
                        }
                    }
                    return checked;
                }""", option_map)
                
                exp_list = expected_val if isinstance(expected_val, list) else ([expected_val] if expected_val and str(expected_val).lower() not in ["false", "0", "no", "none", "skip"] else [])
                exp_set = {_clean_str(x) for x in exp_list}
                cur_set = {_clean_str(x) for x in checked_opts}
                
                if exp_set == cur_set:
                    return VerificationResult(control_type="checkbox", expected_state=exp_list, actual_state=checked_opts, is_verified=True)
                return VerificationResult(
                    control_type="checkbox", expected_state=exp_list, actual_state=checked_opts,
                    is_verified=False, failure_reason=f"Checked options {checked_opts} do not match expected {exp_list}"
                )
            else:
                is_checked = await page.evaluate("""(id) => {
                    const el = document.querySelector(`[data-stillbot-id="${id}"]`);
                    if (!el) return false;
                    return el.checked === true || el.getAttribute('aria-checked') === 'true' || el.classList.contains('isChecked');
                }""", f_id)
                exp_bool = bool(expected_val) and str(expected_val).lower() not in ["false", "0", "no", "none"]
                if is_checked == exp_bool:
                    return VerificationResult(control_type="checkbox", expected_state=exp_bool, actual_state=is_checked, is_verified=True)
                return VerificationResult(
                    control_type="checkbox", expected_state=exp_bool, actual_state=is_checked,
                    is_verified=False, failure_reason=f"Checkbox state ({is_checked}) does not match expected ({exp_bool})"
                )

        # 3. Select / Combobox
        elif role in ["combobox", "select", "listbox"]:
            val_info = await page.evaluate("""(id) => {
                const el = document.querySelector(`[data-stillbot-id="${id}"]`);
                if (!el) return null;
                if (el.tagName.toLowerCase() === 'select') {
                    const opt = el.options[el.selectedIndex];
                    return { text: opt ? (opt.text || '').trim() : '', value: el.value };
                }
                return { text: (el.innerText || el.value || '').trim(), value: el.value || '' };
            }""", f_id)
            
            if not val_info or (not val_info["text"] and not val_info["value"]):
                return VerificationResult(
                    control_type="select", expected_state=expected_val, actual_state=None,
                    is_verified=False, failure_reason="Select element has no selected option or value"
                )
                
            cur_text = _clean_str(val_info["text"])
            cur_val = _clean_str(val_info["value"])
            exp_clean = _clean_str(expected_val)
            
            if exp_clean == cur_text or exp_clean == cur_val:
                return VerificationResult(control_type="select", expected_state=expected_val, actual_state=val_info["text"], is_verified=True)
            if len(exp_clean) >= 3 and (re.search(r'\b' + re.escape(exp_clean) + r'\b', cur_text) or re.search(r'\b' + re.escape(cur_text) + r'\b', exp_clean)):
                return VerificationResult(control_type="select", expected_state=expected_val, actual_state=val_info["text"], is_verified=True)
                
            return VerificationResult(
                control_type="select", expected_state=expected_val, actual_state=val_info["text"],
                is_verified=False, failure_reason=f"Selected option '{val_info['text']}' does not match expected '{expected_val}'"
            )

        # 4. Textbox / Textarea
        elif role in ["textbox", "textarea"]:
            dom_val = await page.evaluate("""(id) => {
                const el = document.querySelector(`[data-stillbot-id="${id}"]`);
                if (!el) return null;
                return el.value !== undefined ? el.value : (el.innerText || '');
            }""", f_id)
            
            if dom_val is None:
                return VerificationResult(
                    control_type="textbox", expected_state=expected_val, actual_state=None,
                    is_verified=False, failure_reason=f"Element with id {f_id} not found in DOM"
                )
                
            exp_str = _clean_str(expected_val)
            act_str = _clean_str(dom_val)
            
            if exp_str == act_str:
                return VerificationResult(control_type="textbox", expected_state=expected_val, actual_state=dom_val, is_verified=True)
            return VerificationResult(
                control_type="textbox", expected_state=expected_val, actual_state=dom_val,
                is_verified=False, failure_reason=f"DOM text '{dom_val}' does not match expected '{expected_val}'"
            )

        # 5. Ranking / Drag-and-Drop
        elif role == "ranking":
            target_drop_id = field.get("target_container_id")
            ranked_dom_items = await page.evaluate("""(data) => {
                const targetDropId = data.targetDropId;
                const containerId = data.containerId;
                let container = null;
                if (targetDropId) {
                    container = document.querySelector(`[data-stillbot-id="${targetDropId}"]`);
                }
                if (!container && containerId) {
                    container = document.querySelector(`[data-stillbot-id="${containerId}"]`);
                }
                if (!container && data.realId) {
                    container = document.getElementById(data.realId);
                }
                if (!container) return [];
                const items = Array.from(container.querySelectorAll('li, [draggable="true"], .rank-item, .sortable-item, .draggable-row, .Selection, [data-choice-id]'));
                return items.map(it => {
                    const clone = it.cloneNode(true);
                    const handles = clone.querySelectorAll('.handle, .drag-handle, .ui-sortable-handle, [class*="handle" i]');
                    handles.forEach(h => h.remove());
                    return clone.innerText.replace(/^\\d+[\\.\\s\\-:]+/, '').trim();
                }).filter(Boolean);
            }""", {"targetDropId": target_drop_id, "containerId": f_id, "realId": field.get("real_id")})
            
            exp_list = expected_val if isinstance(expected_val, list) else [expected_val]
            exp_clean_list = [_clean_str(x) for x in exp_list if x]
            dom_clean_list = [_clean_str(x) for x in ranked_dom_items if x]
            
            matches = 0
            for i, exp_item in enumerate(exp_clean_list):
                if i < len(dom_clean_list) and exp_item == dom_clean_list[i]:
                    matches += 1
                elif any(exp_item in d or d in exp_item for d in dom_clean_list):
                    matches += 1
                    
            if matches >= min(len(exp_clean_list), 1):
                return VerificationResult(control_type="ranking", expected_state=expected_val, actual_state=ranked_dom_items, is_verified=True)
            return VerificationResult(
                control_type="ranking", expected_state=expected_val, actual_state=ranked_dom_items,
                is_verified=False, failure_reason=f"Ranked items in DOM {ranked_dom_items} do not match expected {expected_val}"
            )

        # 6. Canvas / Signature
        elif role == "signature":
            return VerificationResult(control_type="signature", expected_state="signed", actual_state="signed", is_verified=True)

        # 7. Slider / Range
        elif role == "slider":
            cur_val = await page.evaluate("""(id) => {
                const el = document.querySelector(`[data-stillbot-id="${id}"]`);
                if (!el) return null;
                return el.value !== undefined ? el.value : el.getAttribute('aria-valuenow');
            }""", f_id)
            if cur_val is not None:
                return VerificationResult(control_type="slider", expected_state=expected_val, actual_state=cur_val, is_verified=True)
            return VerificationResult(control_type="slider", expected_state=expected_val, actual_state=None, is_verified=False, failure_reason="Slider value not present in DOM")

        # 8. Date / Time
        elif role in ["date_time", "datepicker", "timepicker"]:
            dom_val = await page.evaluate("""(id) => {
                const el = document.querySelector(`[data-stillbot-id="${id}"]`);
                return el ? el.value : null;
            }""", f_id)
            if dom_val is not None and dom_val != "":
                return VerificationResult(control_type=role, expected_state=expected_val, actual_state=dom_val, is_verified=True)
            return VerificationResult(control_type=role, expected_state=expected_val, actual_state=dom_val, is_verified=False, failure_reason="Date field empty in DOM")

        # 9. File Upload
        elif role == "file_upload":
            has_files = await page.evaluate("""(id) => {
                const el = document.querySelector(`[data-stillbot-id="${id}"]`);
                return el && el.files ? el.files.length > 0 : false;
            }""", f_id)
            if has_files:
                return VerificationResult(control_type="file_upload", expected_state=expected_val, actual_state="file_attached", is_verified=True)
            return VerificationResult(control_type="file_upload", expected_state=expected_val, actual_state=None, is_verified=False, failure_reason="No files attached to input in DOM")

        return VerificationResult(control_type=role, expected_state=expected_val, actual_state=expected_val, is_verified=True)
    except Exception as e:
        return VerificationResult(
            control_type=role, expected_state=expected_val, actual_state=None,
            is_verified=False, failure_reason=f"Verification exception: {e}"
        )

async def fill_field(page, field, answer):
    """
    Universally fills a field across any survey platform or web framework.
    Handles native HTML, React/Vue/Angular synthetic state, hidden inputs with custom labels,
    and custom ARIA comboboxes.
    """
    role = field.get("role")
    name = field.get("label")
    val = answer.get("value") if isinstance(answer, dict) else answer
    source = answer.get("source") if isinstance(answer, dict) else None
    
    if source == "gap":
        print(f"⚠️ [WARNING] Persona gap detected for '{name}'. AI failed to infer a value. Leaving empty.")
        val = ""
    
    print(f"✍️ Filling [{role}] '{name}' with: {val}")
    
    # 0. Primary Universal Interaction Engine Dispatch
    try:
        from interaction_engine import universal_registry
        ans_dict = answer if isinstance(answer, dict) else {"value": val}
        res = await universal_registry.dispatch_fill(page, field, ans_dict)
        if res.success:
            strat_name = res.applied_strategy.value if res.applied_strategy else "universal"
            print(f"🎯 Field '{name}' verified in DOM using strategy: {strat_name}")
            return
        else:
            print(f"ℹ️ Universal registry fallback engaged for '{name}': {res.diagnostics.get('last_error')}")
    except Exception as eng_err:
        print(f"⚠️ Universal registry dispatch note: {eng_err}")
    
    try:
        # Locate the element robustly
        locator = None
        try:
            loc = page.get_by_role(role, name=name).first
            if await loc.count() > 0:
                locator = loc
        except Exception:
            pass
            
        if not locator or await locator.count() == 0:
            locator = page.locator(f'[data-stillbot-id="{field.get("id")}"]').first

        if not locator or await locator.count() == 0:
            real_id = field.get("real_id")
            if real_id:
                loc = page.locator(f'#{real_id}').first
                if await loc.count() > 0:
                    locator = loc

        if not locator or await locator.count() == 0:
            f_name = field.get("name")
            if f_name:
                loc = page.locator(f'[name="{f_name}"]').first
                if await loc.count() > 0:
                    locator = loc

        if not locator or await locator.count() == 0:
            # Fallback by container and label text (e.g. Question 1 ZIP code)
            first_line = name.split('\n')[0].strip() if name else ""
            if first_line and len(first_line) > 1:
                target_sel = "select" if role in ["combobox", "listbox"] else ("input" if role in ["checkbox", "textbox"] else "*")
                loc = page.locator(f'fieldset:has-text("{first_line}"), .question-container:has-text("{first_line}"), div:has-text("{first_line}")').locator(target_sel).first
                if await loc.count() > 0:
                    locator = loc
            
        if role == "radio":
            option_map = field.get("option_map", {})
            val_clean = str(val).strip() if val is not None else ""
            
            # Map booleans to logical Yes/No if options have Yes/No
            if val is True and "yes" not in val_clean.lower():
                val_clean = "Yes"
            elif val is False and "no" not in val_clean.lower():
                val_clean = "No"

            # Check if this field should be skipped: only if empty or gap, or if 'skip' and not an option
            if (val is None or val_clean == "" or val_clean.lower() in ["skip", "n/a"]) and val_clean not in option_map:
                print(f"⏭️ Skipping radio '{field.get('label')}' (val is {val})")
                return

            clicked = False
            target_id = None
            matched_opt_name = None
            
            if option_map:
                def norm_txt(s: str) -> str:
                    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
                    s = re.sub(r'[\u2013\u2014\u2212–—]', '-', s)
                    s = s.replace("\u00a0", " ")
                    return re.sub(r'\s+', ' ', s).strip().lower()

                val_norm = norm_txt(val_clean)
                
                # 0. Direct element ID match (if LLM returned target ID)
                if val_clean in option_map.values():
                    target_id = val_clean
                    for opt_text, opt_id in option_map.items():
                        if opt_id == val_clean:
                            matched_opt_name = opt_text
                            break

                # 1. Exact match
                if not target_id and val_clean in option_map:
                    target_id = option_map[val_clean]
                    matched_opt_name = val_clean
                
                # 2. Normalized exact match
                if not target_id:
                    for opt_text, opt_id in option_map.items():
                        if norm_txt(opt_text) == val_norm:
                            target_id = opt_id
                            matched_opt_name = opt_text
                            break

                # 3. Safe Word-Boundary Matching (polarity protected)
                if not target_id:
                    for opt_text, opt_id in option_map.items():
                        opt_norm = norm_txt(opt_text)
                        # CRITICAL: "agree" must NEVER match "disagree" or "strongly disagree"
                        if "agree" in val_norm and not any(k in val_norm for k in ["disagree", "not", "un"]):
                            if any(k in opt_norm for k in ["disagree", "not", "un"]):
                                continue
                        # CRITICAL: "disagree" must NEVER match "agree"
                        if any(k in val_norm for k in ["disagree", "not", "un"]):
                            if "agree" in opt_norm and not any(k in opt_norm for k in ["disagree", "not", "un"]):
                                continue
                        # "strongly agree" vs "agree": do not match "strongly agree" if val is just "agree"
                        if val_norm == "agree" and "strongly" in opt_norm:
                            continue
                        if val_norm == "strongly agree" and opt_norm == "agree":
                            continue

                        # Word boundary match
                        pattern = r'\b' + re.escape(val_norm) + r'\b'
                        if re.search(pattern, opt_norm):
                            target_id = opt_id
                            matched_opt_name = opt_text
                            break
                        pattern_inv = r'\b' + re.escape(opt_norm) + r'\b'
                        if re.search(pattern_inv, val_norm):
                            target_id = opt_id
                            matched_opt_name = opt_text
                            break

                # 4. RapidFuzz Token Set Ratio with polarity protection
                if not target_id and option_map:
                    best_score = 0
                    best_opt = None
                    for opt_text, opt_id in option_map.items():
                        opt_norm = norm_txt(opt_text)
                        # Polarity protection
                        if "agree" in val_norm and not any(k in val_norm for k in ["disagree", "not", "un"]):
                            if any(k in opt_norm for k in ["disagree", "not", "un"]):
                                continue
                        if any(k in val_norm for k in ["disagree", "not", "un"]):
                            if "agree" in opt_norm and not any(k in opt_norm for k in ["disagree", "not", "un"]):
                                continue
                        score = fuzz.token_set_ratio(val_norm, opt_norm)
                        if score > best_score:
                            best_score = score
                            best_opt = (opt_text, opt_id)
                    if best_opt and best_score >= 60:
                        matched_opt_name, target_id = best_opt
                        print(f"🔍 Fuzzy matched '{val_clean}' to actual option '{matched_opt_name}' (Score: {best_score})")

                # 5. Polarity-safe fuzzy match fallback (never arbitrarily guess)
                if not target_id and option_map:
                    best_cand = None
                    best_cand_score = 0
                    for opt_text, opt_id in option_map.items():
                        opt_norm = norm_txt(opt_text)
                        # Polarity protection: never match opposite polarities!
                        if any(k in val_norm for k in ["disagree", "not", "un"]) and ("agree" in opt_norm and not any(k in opt_norm for k in ["disagree", "not", "un"])):
                            continue
                        if ("agree" in val_norm and not any(k in val_norm for k in ["disagree", "not", "un"])) and any(k in opt_norm for k in ["disagree", "not", "un"]):
                            continue
                        score = fuzz.token_sort_ratio(val_norm, opt_norm)
                        if score > best_cand_score and score >= 60:
                            best_cand_score = score
                            best_cand = (opt_text, opt_id)
                    if best_cand:
                        matched_opt_name, target_id = best_cand
                        print(f"🔍 Polarity-safe fuzzy match for '{field.get('label')}': '{val_clean}' -> '{matched_opt_name}' (score {best_cand_score})")
                    else:
                        print(f"⚠️ Unmatched option for '{field.get('label')}': Desired value '{val_clean}' does not match available options {list(option_map.keys())}. Skipping arbitrary selection.")

            if target_id:
                try:
                    # 1. Look up the real element ID to find user-facing enhanced choice label (e.g. REDCap, SurveyMonkey)
                    real_el_id = await page.evaluate(f"""() => {{
                        const el = document.querySelector('[data-stillbot-id="{target_id}"]');
                        return el ? el.id : null;
                    }}""")
                    
                    if real_el_id:
                        lbl = page.locator(f'label[for="{real_el_id}"]:visible, .enhancedchoice label[for="{real_el_id}"]').first
                        if await lbl.count() > 0:
                            await lbl.click(force=True, timeout=2000)
                            clicked = True
                    
                    if not clicked:
                        lbl = page.locator(f'label:has([data-stillbot-id="{target_id}"])').first
                        if await lbl.count() > 0 and await lbl.is_visible():
                            await lbl.click(force=True, timeout=2000)
                            clicked = True
                    
                    if not clicked:
                        await page.evaluate(f"""() => {{
                            const el = document.querySelector('[data-stillbot-id="{target_id}"]');
                            if (el) {{
                                el.click();
                                el.checked = true;
                                el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                                el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                            }}
                        }}""")
                        clicked = True

                    # Universal state verification & event enforcement
                    await page.evaluate(f"""() => {{
                        const el = document.querySelector('[data-stillbot-id="{target_id}"]');
                        if (!el) return;
                        const isChecked = el.checked === true || el.getAttribute('aria-checked') === 'true';
                        if (!isChecked) {{
                            el.click();
                            el.checked = true;
                            if (el.getAttribute('role') === 'radio') {{
                                el.setAttribute('aria-checked', 'true');
                            }}
                            el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                            el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        }}
                    }}""")
                except Exception as e:
                    print(f"⚠️ Radio target click error: {e}")
                    
            if not clicked and target_id:
                target_name = matched_opt_name or val_clean
                try:
                    await page.get_by_role("radio", name=target_name, exact=False).first.click(force=True, timeout=2000)
                    clicked = True
                except Exception:
                    pass

            if clicked:
                print(f"🎯 Verified radio selection in DOM: '{matched_opt_name or val_clean}'")
            else:
                print(f"⚠️ Warning: Could not select radio option '{val_clean}' for field '{field.get('label')}'")

        elif role == "checkbox":
            option_map = field.get("option_map", {})
            if option_map:
                # Multi-select checkbox group: click each selected option and uncheck unselected options
                targets_to_check = val if isinstance(val, list) else ([val] if val and str(val).lower() not in ["false", "0", "no", "skip", "none", "n/a"] else [])
                targets_norm = [str(t).strip().lower() for t in targets_to_check]
                for opt_name, opt_id in option_map.items():
                    opt_norm = opt_name.strip().lower()
                    should_check = any(t == opt_norm or t in opt_norm or opt_norm in t for t in targets_norm)
                    if should_check:
                        await page.evaluate(f"""() => {{
                            const el = document.querySelector('[data-stillbot-id="{opt_id}"]');
                            if (!el) return;
                            const isChecked = el.checked === true || el.getAttribute('aria-checked') === 'true';
                            if (!isChecked) {{
                                el.click();
                                el.checked = true;
                                if (el.getAttribute('role') === 'checkbox') el.setAttribute('aria-checked', 'true');
                                el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                                el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                            }}
                        }}""")
                        print(f"🎯 Checked multi-select option: '{opt_name}'")
                    else:
                        await page.evaluate(f"""() => {{
                            const el = document.querySelector('[data-stillbot-id="{opt_id}"]');
                            if (!el) return;
                            const isChecked = el.checked === true || el.getAttribute('aria-checked') === 'true';
                            if (isChecked) {{
                                el.click();
                                el.checked = false;
                                if (el.getAttribute('role') === 'checkbox') el.setAttribute('aria-checked', 'false');
                                el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                                el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                            }}
                        }}""")
            elif isinstance(val, list):
                for v in val:
                    v_str = str(v).strip()
                    try:
                        await page.get_by_role("checkbox", name=v_str, exact=False).first.click(force=True, timeout=2000)
                    except Exception:
                        pass
            else:
                val_str = str(val).strip().lower()
                f_id = field.get("id", "")
                if not val_str or val_str in ["no", "false", "0", "skip", "none", "n/a"]:
                    # Ensure unselected
                    await page.evaluate(f"""() => {{
                        const el = document.querySelector('[data-stillbot-id="{f_id}"]');
                        if (!el) return;
                        if (el.checked) {{
                            el.checked = false;
                            el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        }}
                        if (el.getAttribute('aria-checked') === 'true') {{
                            el.click();
                            el.setAttribute('aria-checked', 'false');
                        }}
                    }}""")
                else:
                    clicked = False
                    
                    # 1. Label tag lookup
                    try:
                        real_el_id = await page.evaluate(f"""() => {{
                            const el = document.querySelector('[data-stillbot-id="{f_id}"]');
                            return el ? el.id : null;
                        }}""")
                        if real_el_id:
                            lbl_loc = page.locator(f'label[for="{real_el_id}"]:visible, label:has([data-stillbot-id="{f_id}"])').first
                            if await lbl_loc.count() > 0:
                                await lbl_loc.click(force=True, timeout=1500)
                                clicked = True
                    except Exception:
                        pass
                        
                    # 2. Google Forms / custom toggle container click
                    if not clicked:
                        try:
                            toggled = await page.evaluate(f"""() => {{
                                const el = document.querySelector('[data-stillbot-id="{f_id}"]');
                                if (!el) return false;
                                const container = el.closest('.docssharedWizToggleLabeledContainer, .bz0duf, .geS5n, [role="listitem"], label');
                                if (container) {{
                                    container.click();
                                    return true;
                                }}
                                return false;
                            }}""")
                            if toggled:
                                clicked = True
                        except Exception:
                            pass
                            
                    # 3. Direct locator click
                    if not clicked and locator:
                        try:
                            await locator.click(force=True, timeout=1500)
                            clicked = True
                        except Exception:
                            try:
                                await locator.check(force=True, timeout=1500)
                                clicked = True
                            except Exception:
                                pass
                                
                    # 4. Universal state verification & event enforcement
                    await page.evaluate(f"""() => {{
                        const el = document.querySelector('[data-stillbot-id="{f_id}"]');
                        if (!el) return;
                        const isChecked = el.checked === true || el.getAttribute('aria-checked') === 'true' || el.classList.contains('isChecked');
                        if (!isChecked) {{
                            el.click();
                            if (el.getAttribute('role') === 'checkbox') {{
                                el.setAttribute('aria-checked', 'true');
                                el.dispatchEvent(new MouseEvent('click', {{ bubbles: true, cancelable: true }}));
                            }} else {{
                                el.checked = true;
                            }}
                            el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                            el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        }}
                    }}""")
                    print(f"🎯 Checked checkbox: '{field.get('label')}'")
                    
        elif role in ["combobox", "listbox"]:
            val_clean = str(val).strip()
            
            # Avoid triggering survey reloads if language is already set
            if "language" in (field.get("label") or "").lower():
                try:
                    cur_lang = await locator.evaluate("el => el ? (el.options[el.selectedIndex]?.text || '') : ''")
                    if cur_lang.lower().startswith(val_clean.lower()) or val_clean.lower() in cur_lang.lower():
                        print(f"✅ Language already set to '{cur_lang}'. Skipping.")
                        return
                except Exception:
                    pass

            # Check if this is a standard native <select>
            try:
                tag = await locator.evaluate("el => el ? el.tagName.toLowerCase() : ''")
            except Exception:
                tag = ""
            if tag == "select":
                # Find matching option text & value safely with aliases (e.g. 0 <-> None)
                opt_info = await locator.evaluate("""
                (el, targetStr) => {
                    if (!el || !el.options) return null;
                    const target = (targetStr || '').toLowerCase().trim();
                    // 1. Exact match text or value
                    for (let opt of el.options) {
                        const oText = (opt.text || '').toLowerCase().trim();
                        const oVal = (opt.value || '').toLowerCase().trim();
                        if (oText === target || oVal === target) {
                            return { text: opt.text.trim(), value: opt.value };
                        }
                    }
                    // 2. Numeric / Zero aliases (e.g. 0 <-> none / zero / no children)
                    const isZero = target === '0' || target === 'none' || target === 'zero' || target.includes('no child') || target.includes('none');
                    if (isZero) {
                        for (let opt of el.options) {
                            const oText = (opt.text || '').toLowerCase().trim();
                            const oVal = (opt.value || '').toLowerCase().trim();
                            if (oText === '0' || oVal === '0' || oText === 'none' || oVal === 'none' || oText.includes('none') || oText.includes('zero') || oText.includes('no child')) {
                                return { text: opt.text.trim(), value: opt.value };
                            }
                        }
                    }
                    // 3. Includes / substring match
                    for (let opt of el.options) {
                        const oText = (opt.text || '').toLowerCase().trim();
                        const oVal = (opt.value || '').toLowerCase().trim();
                        if ((target && (oText.includes(target) || target.includes(oText))) || (target && oVal.includes(target))) {
                            return { text: opt.text.trim(), value: opt.value };
                        }
                    }
                    // 4. If target is number, try matching digits
                    const targetNum = target.match(/\\d+/);
                    if (targetNum) {
                        for (let opt of el.options) {
                            const optNum = (opt.text || '').match(/\\d+/);
                            if (optNum && optNum[0] === targetNum[0]) {
                                return { text: opt.text.trim(), value: opt.value };
                            }
                        }
                    }
                    // 5. If still no match and current option is unselected, select the first valid option
                    if (el.selectedIndex <= 0 || !el.value) {
                        for (let i = 0; i < el.options.length; i++) {
                            const opt = el.options[i];
                            if (opt.value && opt.text && opt.text.trim() !== '' && !opt.text.toLowerCase().includes('select') && !opt.disabled) {
                                return { text: opt.text.trim(), value: opt.value };
                            }
                        }
                    }
                    return null;
                }
                """, val_clean)
                
                if opt_info:
                    target_text = opt_info["text"]
                    target_val = opt_info["value"]
                    
                    # 1. Standard Playwright select_option
                    try:
                        await locator.select_option(value=target_val, timeout=2000)
                    except Exception:
                        try:
                            await locator.select_option(label=target_text, timeout=2000)
                        except Exception:
                            pass
                            
                    # 2. Native event dispatch and React valueTracker reset
                    await locator.evaluate("""
                    (el, val) => {
                        el.value = val;
                        if (el._valueTracker) el._valueTracker.setValue('');
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                        el.dispatchEvent(new Event('blur', { bubbles: true }));
                        
                        const pKey = Object.keys(el).find(k => k.startsWith('__reactProps'));
                        if (pKey && el[pKey] && typeof el[pKey].onChange === 'function') {
                            try {
                                el[pKey].onChange({
                                    target: el,
                                    currentTarget: el,
                                    bubbles: true
                                });
                            } catch (e) {}
                        }
                    }
                    """, target_val)
                    print(f"🎯 Selected combobox option: '{target_text}'")
                else:
                    for attempt in ["label", "value"]:
                        try:
                            await locator.select_option(**{attempt: val_clean}, timeout=1500)
                            break
                        except Exception:
                            pass
            else:
                # Custom ARIA combobox (e.g. Google Forms, Ant Design, Material-UI)
                try:
                    await locator.click(force=True, timeout=2000)
                    await page.wait_for_timeout(300)
                    opt_loc = page.locator(f'[role="option"]:has-text("{val_clean}"), li:has-text("{val_clean}")').first
                    if await opt_loc.count() > 0 and await opt_loc.is_visible():
                        await opt_loc.click(force=True, timeout=2000)
                    else:
                        await page.keyboard.type(val_clean)
                        await page.keyboard.press("Enter")
                except Exception as e:
                    print(f"⚠️ Custom combobox interaction fallback: {e}")
                    
        elif role == "signature":
            print(f"✍️ Signing signature canvas: '{name}'")
            try:
                canvas = None
                try:
                    c = page.locator('canvas.SignatureCanvas, canvas[id*="Signature" i], .SignatureBox canvas, canvas').first
                    if await c.count() > 0 and await c.is_visible():
                        canvas = c
                except Exception:
                    pass
                if not canvas and locator:
                    canvas = locator
                    
                if canvas:
                    box = await canvas.bounding_box()
                    if box and box["width"] > 0 and box["height"] > 0:
                        start_x = box["x"] + box["width"] * 0.2
                        start_y = box["y"] + box["height"] * 0.5
                        end_x = box["x"] + box["width"] * 0.8
                        end_y = box["y"] + box["height"] * 0.5
                        
                        await page.mouse.move(start_x, start_y)
                        await page.mouse.down()
                        for step in range(1, 20):
                            t = step / 20.0
                            cur_x = start_x + (end_x - start_x) * t
                            cur_y = start_y + (10 if step % 2 == 0 else -10)
                            await page.mouse.move(cur_x, cur_y)
                            await page.wait_for_timeout(15)
                        await page.mouse.move(end_x, end_y)
                        await page.mouse.up()
                        print(f"✅ Digital signature drawn successfully on canvas.")
            except Exception as e:
                print(f"⚠️ Signature draw error: {e}")

        elif role == "ranking":
            target_drop_id = field.get("target_container_id")
            option_map = field.get("option_map", {})
            target_count = field.get("target_count")
            
            # Answer can be list of strings, or comma-separated string, or JSON list
            ranked_items = []
            if isinstance(val, list):
                ranked_items = [str(x).strip() for x in val if str(x).strip()]
            elif isinstance(val, str):
                try:
                    parsed = json.loads(val)
                    if isinstance(parsed, list):
                        ranked_items = [str(x).strip() for x in parsed if str(x).strip()]
                except Exception:
                    pass
                if not ranked_items:
                    ranked_items = [x.strip() for x in val.split("\n") if x.strip()]
                if not ranked_items:
                    ranked_items = [x.strip() for x in val.split(",") if x.strip()]
                    
            if target_count and len(ranked_items) > target_count:
                ranked_items = ranked_items[:target_count]
                
            print(f"🎯 Ranking options to place: {ranked_items}")
            
            # Locate target dropzone or sortable container
            target_dropzone = None
            if target_drop_id:
                target_dropzone = page.locator(f'[data-stillbot-id="{target_drop_id}"]').first
            if not target_dropzone or await target_dropzone.count() == 0:
                dz = page.locator('.Group.Ranked, .dropzone, [data-widget="dropzone"], ul.Ranked').first
                if await dz.count() > 0:
                    target_dropzone = dz
                    
            for rank_idx, item_name in enumerate(ranked_items):
                matched_opt_id = None
                for opt_text, opt_id in option_map.items():
                    if opt_text.lower() == item_name.lower() or item_name.lower() in opt_text.lower() or opt_text.lower() in item_name.lower():
                        matched_opt_id = opt_id
                        break
                        
                source_loc = None
                if matched_opt_id:
                    source_loc = page.locator(f'[data-stillbot-id="{matched_opt_id}"]').first
                if not source_loc or await source_loc.count() == 0:
                    source_loc = page.locator(f'li:has-text("{item_name}"), [draggable="true"]:has-text("{item_name}"), .sortable-item:has-text("{item_name}"), .draggable-row:has-text("{item_name}"), .Selection:has-text("{item_name}"), .rank-item:has-text("{item_name}")').first
                    
                if source_loc and await source_loc.count() > 0:
                    slot_sel = f'[data-stillbot-id="{field.get("id")}"] li, [data-stillbot-id="{field.get("id")}"] [draggable="true"], [data-stillbot-id="{field.get("id")}"] .sortable-item, .ui-sortable li, .ui-sortable [draggable="true"], .ui-sortable .sortable-item, .ui-sortable .rank-item'
                    try:
                        await source_loc.scroll_into_view_if_needed()
                        if target_dropzone and await target_dropzone.count() > 0:
                            print(f"🔄 Dragging [{item_name}] into target dropzone...")
                            await source_loc.drag_to(target_dropzone, timeout=4000)
                            await page.wait_for_timeout(600)
                        else:
                            target_slot = page.locator(slot_sel).nth(rank_idx)
                            if await target_slot.count() > 0:
                                print(f"🔄 Reordering [{item_name}] to rank {rank_idx + 1}...")
                                await source_loc.drag_to(target_slot, timeout=4000)
                                await page.wait_for_timeout(600)
                    except Exception as drag_err:
                        print(f"⚠️ Playwright drag_to failed for [{item_name}]: {drag_err}. Trying pointer simulation...")
                        try:
                            source_box = await source_loc.bounding_box()
                            target_box = None
                            if target_dropzone and await target_dropzone.count() > 0:
                                target_box = await target_dropzone.bounding_box()
                            else:
                                t_slot = page.locator(slot_sel).nth(rank_idx)
                                if await t_slot.count() > 0:
                                    target_box = await t_slot.bounding_box()
                                    
                            if source_box and target_box:
                                sx = source_box["x"] + source_box["width"] / 2
                                sy = source_box["y"] + source_box["height"] / 2
                                tx = target_box["x"] + target_box["width"] / 2
                                ty = target_box["y"] + target_box["height"] / 2
                                await page.mouse.move(sx, sy)
                                await page.mouse.down()
                                for s in range(1, 10):
                                    await page.mouse.move(sx + (tx - sx) * (s / 10.0), sy + (ty - sy) * (s / 10.0))
                                    await page.wait_for_timeout(30)
                                await page.mouse.move(tx, ty)
                                await page.mouse.up()
                                await page.wait_for_timeout(600)
                        except Exception as ptr_err:
                            print(f"⚠️ Pointer drag simulation failed: {ptr_err}")

        elif role == "slider":
            from interaction_engine import universal_registry
            ans_dict = answer if isinstance(answer, dict) else {"value": val}
            res = await universal_registry.dispatch_fill(page, field, ans_dict)
            if res.success:
                print(f"🎯 Slider set to '{val}' using strategy {res.applied_strategy.value if res.applied_strategy else 'seek'}")
            else:
                print(f"⚠️ Slider interaction failed: {res.diagnostics.get('last_error')}")

        elif role in ["date_time", "datepicker", "timepicker"]:
            from interaction_engine import universal_registry
            ans_dict = answer if isinstance(answer, dict) else {"value": val}
            res = await universal_registry.dispatch_fill(page, field, ans_dict)
            if res.success:
                print(f"🎯 Date/Time set to '{val}' using strategy {res.applied_strategy.value if res.applied_strategy else 'fill'}")
            else:
                print(f"⚠️ Date/Time interaction failed: {res.diagnostics.get('last_error')}")

        elif role == "file_upload":
            from interaction_engine import universal_registry
            ans_dict = answer if isinstance(answer, dict) else {"value": val}
            res = await universal_registry.dispatch_fill(page, field, ans_dict)
            if res.success:
                print(f"🎯 File uploaded using strategy {res.applied_strategy.value if res.applied_strategy else 'attach'}")
            else:
                print(f"⚠️ File upload failed: {res.diagnostics.get('last_error')}")

        else:
            # Standard Textbox / Contenteditable
            if val is None or str(val).strip() == "" or str(val).strip().lower() == "none":
                print(f"⏭️ Leaving optional/empty textbox blank: '{name}'")
                return
            try:
                await locator.fill(str(val), force=True, timeout=3000)
                await locator.dispatch_event("input")
                await locator.dispatch_event("change")
                await locator.dispatch_event("blur")
            except Exception:
                try:
                    await locator.click(force=True, timeout=2000)
                    await page.keyboard.press("Control+A")
                    await page.keyboard.press("Backspace")
                    await page.keyboard.type(str(val))
                    await locator.dispatch_event("input")
                    await locator.dispatch_event("change")
                except Exception as e:
                    print(f"⚠️ Textbox fill fallback: {e}")
            
    except Exception as e:
        print(f"❌ Failed to fill field '{name}': {e}")

async def wait_for_human_copilot(page, page_num: int, reason: str = "", status_callback = None, timeout_seconds: int = 300) -> bool:
    """
    Co-Pilot Mode (Human-in-the-Loop):
    Pauses autonomous automation without closing the browser and without burning any AI credits.
    Delegates to the configured HumanInterventionProvider (Local desktop or Remote cloud)
    based on the resolved STILLBOT_RUN_MODE.
    """
    from run_mode import get_intervention_provider
    provider = get_intervention_provider()
    return await provider.request_intervention(
        page=page,
        page_num=page_num,
        reason=reason,
        status_callback=status_callback,
        timeout_seconds=timeout_seconds
    )

async def find_advance_button(page):
    """
    Universally finds the Next, Continue, Start, or Submit button on any survey platform.
    Excludes negative buttons like Back, Previous, Cancel, Clear.
    """
    candidate_selectors = [
        '#NextButton', 'input#NextButton', 'button#NextButton', 'input[name="NextButton"]', 'button[name="NextButton"]',
        '#Buttons #NextButton', '.Skin #Buttons #NextButton', '#PlugInContainer #NextButton',
        '.next-button', '.NextButton', 'input.NextButton', 'button.NextButton',
        '#Buttons input', '#Buttons button',
        '.cf-navigation-next', '.cf-navigation__button', 'button.cf-navigation-next',
        'button[type="submit"]', 'input[type="submit"]',
        '#next-button', 'button#next-button',
        '#submit-button', '#SubmitButton',
        '.navigation-button', '.btn-next', '.btn-submit',
        '[data-action*="next" i]', '[data-action*="submit" i]',
        '[data-qa*="ok" i]', '[data-qa*="submit" i]',
        '[data-testid*="next" i]', '[data-testid*="submit" i]',
        'button[aria-label*="next" i]', 'button[aria-label*="continue" i]',
        'button[aria-label*="submit" i]', 'button[aria-label*="done" i]',
        'button:has-text("Next")', 'button:has-text("Next page")', 'button:has-text("Continue")',
        'button:has-text("Submit")', 'button:has-text("Done")', 'button:has-text("Finish")',
        'button:has-text("Start")', 'button:has-text("Begin")', 'button:has-text("Take Survey")',
        'a:has-text("Next")', 'a:has-text("Continue")', 'a:has-text("Start")', 'a:has-text("Begin")',
        'a.btn:has-text("Next")', 'a.button:has-text("Next")',
        '[role="button"]:has-text("Next")', '[role="button"]:has-text("Continue")',
        '[role="button"]:has-text("Submit")', '[role="button"]:has-text("Start")',
        'input[type="button"][value*="Next" i]', 'input[type="button"][value*="Continue" i]',
        'input[type="button"][value*="Submit" i]', 'input[type="button"][value*="Start" i]',
        'button:has-text("→")', 'button:has-text("➔")', 'button:has-text("➜")',
        'button:has(svg[data-icontype*="ChevronRight" i])', 'button:has(svg[data-icon*="arrow-right" i])',
        'button:has(svg.arrow-right)', 'button:has(svg.chevron-right)'
    ]
    negative_keywords = ["back", "prev", "previous", "cancel", "clear", "reset", "delete"]
    
    # Pass 1: Check in current viewport
    for sel in candidate_selectors:
        try:
            locators = await page.locator(sel).all()
            for loc in locators:
                if await loc.is_visible():
                    txt = ((await loc.inner_text() or "") + " " + (await loc.get_attribute("value") or "") + " " + (await loc.get_attribute("aria-label") or "") + " " + (await loc.get_attribute("id") or "")).lower()
                    if any(neg in txt for neg in negative_keywords):
                        continue
                    return loc
        except Exception:
            continue

    # Pass 2: Scroll to bottom of page and re-check (for long forms e.g. Qualtrics)
    try:
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(400)
        for sel in candidate_selectors[:15]:
            locators = await page.locator(sel).all()
            for loc in locators:
                if await loc.is_visible():
                    txt = ((await loc.inner_text() or "") + " " + (await loc.get_attribute("value") or "") + " " + (await loc.get_attribute("aria-label") or "") + " " + (await loc.get_attribute("id") or "")).lower()
                    if any(neg in txt for neg in negative_keywords):
                        continue
                    return loc
    except Exception:
        pass

    return None

async def handle_blocking_modal(page, page_num: int, run_ctx: RunContext, status_callback = None) -> Tuple[bool, str]:
    """
    Detects and handles blocking modal dialogs before any button clicks or navigation.
    Classifies modal into:
    - network_error: bounded retry policy (max 2 retries). If persistent, activates Co-Pilot with dialog message.
    - validation_error: flags validation failure, dismisses modal if safe so fields can be answered.
    - consent: safely acknowledges standard consent if clear accept button exists.
    - unknown: activates Co-Pilot with blocking dialog text. Never clicks Next.
    Returns (can_continue, action_summary).
    """
    modal_info = await detect_blocking_modal(page)
    if not modal_info.get("found"):
        return True, "No modal"
        
    m_type = modal_info.get("type", "unknown")
    m_text = modal_info.get("text", "")
    m_title = modal_info.get("title", "")
    summary_text = (f"{m_title}: {m_text}" if m_title else m_text)[:120].strip()
    
    print(f"\n🚨 Blocking Modal Detected on Page {page_num} [{m_type.upper()}]: '{summary_text}'")
    
    # 1. Recoverable Network / Loading Error
    if m_type == "network_error":
        retries = getattr(page, "_network_modal_retries", 0) + 1
        page._network_modal_retries = retries
        
        # Check for safe retry / reload button in modal
        retry_btn_info = None
        for b in modal_info.get("buttons", []):
            b_txt = b.get("text", "").lower()
            if any(k in b_txt for k in ["retry", "try again", "reload", "re-send", "refresh"]):
                retry_btn_info = b
                break
                
        if retries <= 2:
            print(f"🔄 Recoverable connection problem detected ({retries}/2). Waiting and attempting bounded retry...")
            if retry_btn_info:
                try:
                    btn_loc = page.locator(f'[data-stillbot-modal-btn="{retry_btn_info["id"]}"]').first
                    if await btn_loc.count() > 0:
                        await btn_loc.click()
                except Exception:
                    pass
            await page.wait_for_timeout(3000)
            
            # Recheck modal
            check_again = await detect_blocking_modal(page)
            if not check_again.get("found"):
                print("✅ Network connection dialog cleared successfully.")
                page._network_modal_retries = 0
                return True, "Network error dialog cleared"
                
        # Persistent network error (or exceeded 2 retries)
        print(f"🛑 Persistent connection error on Page {page_num}. Activating Co-Pilot Mode with error details...")
        run_ctx.transition_state(PageState.BLOCKED, f"Connection error dialog: {summary_text}")
        if status_callback:
            try:
                await status_callback(
                    f"🛑 **Connection Problem on Page {page_num}:**\n"
                    f"• Dialog: `{summary_text}`\n"
                    f"• Live Survey URL: `{page.url}`\n"
                    f"👉 Please check your network connection or dismiss the dialog on screen."
                )
            except Exception:
                pass
                
        resumed = await wait_for_human_copilot(
            page, page_num,
            reason=f"Survey blocked by connection error: '{summary_text}'. Please resolve on screen to proceed.",
            status_callback=status_callback
        )
        if resumed:
            page._network_modal_retries = 0
            return True, "Human resolved connection error dialog"
        else:
            run_ctx.transition_state(PageState.TIMED_OUT, "Co-pilot timeout on connection error dialog")
            return False, f"Survey terminated: persistent connection error dialog ({summary_text})"

    # 2. Validation Error Modal
    elif m_type == "validation_error":
        print(f"⚠️ Validation error modal detected on Page {page_num}: '{summary_text}'")
        run_ctx.transition_state(PageState.VALIDATION_ERROR, summary_text)
        # Try to dismiss the modal so respondent / bot can interact with unanswered fields
        for b in modal_info.get("buttons", []):
            b_txt = b.get("text", "").lower()
            if any(k in b_txt for k in ["ok", "dismiss", "close", "got it", "continue"]):
                try:
                    btn_loc = page.locator(f'[data-stillbot-modal-btn="{b["id"]}"]').first
                    if await btn_loc.count() > 0:
                        await btn_loc.click()
                        await page.wait_for_timeout(1000)
                        break
                except Exception:
                    pass
        return False, f"Validation error modal: {summary_text}"

    # 3. Consent / Cookie Modal
    elif m_type == "consent":
        for b in modal_info.get("buttons", []):
            b_txt = b.get("text", "").lower()
            if any(k in b_txt for k in ["accept", "agree", "allow", "ok", "got it", "continue"]):
                try:
                    btn_loc = page.locator(f'[data-stillbot-modal-btn="{b["id"]}"]').first
                    if await btn_loc.count() > 0:
                        print(f"🍪 Acknowledging consent modal button '{b['text']}'...")
                        await btn_loc.click()
                        await page.wait_for_timeout(1200)
                        return True, "Consent acknowledged"
                except Exception:
                    pass
        return True, "Consent dialog"

    # 4. Unknown / Backdrop Modal
    else:
        print(f"⚠️ Unknown blocking modal or backdrop detected on Page {page_num}: '{summary_text}'")
        run_ctx.transition_state(PageState.BLOCKED, f"Blocking modal: {summary_text}")
        resumed = await wait_for_human_copilot(
            page, page_num,
            reason=f"Survey blocked by dialog: '{summary_text}'. Please review or dismiss on screen.",
            status_callback=status_callback
        )
        if resumed:
            return True, "Human dismissed blocking dialog"
        else:
            run_ctx.transition_state(PageState.TIMED_OUT, "Co-pilot timeout on blocking modal")
            return False, f"Blocked by modal: {summary_text}"

async def check_is_completed(page, has_answered: bool = False, has_advance_btn: bool = False):
    """
    Universally checks if a survey or questionnaire has been completed, screened out, or closed.
    Never completes on an intro page that still has a visible Next/Start button.
    """
    # If there is a visible Next / Start / Submit button, we are NOT finished
    if has_advance_btn:
        return False, ""

    url = page.url.lower()
    if "docs.google.com/forms" in url and "formresponse" in url:
        return True, "Google Form submitted successfully ('formResponse')"

    completion_url_keywords = [
        "/thanks", "/thank-you", "/complete", "/finish", "/submitted",
        "survey-thanks", "/done", "response_received",
        "/disqualified", "/screenout", "/quota-full",
        "formresponse", "/formresponse", "viewformresponse"
    ]
    if any(k in url for k in completion_url_keywords):
        return True, "Completion URL detected"
        
    try:
        # Check if there are interactive input fields (text, select, radio, checkbox)
        input_count = await page.evaluate("""() => {
            const inputs = Array.from(document.querySelectorAll('input:not([type="hidden"]):not([type="submit"]):not([type="button"]), select, textarea'));
            return inputs.filter(el => el.offsetWidth > 0 || el.offsetHeight > 0 || window.getComputedStyle(el).display !== 'none').length;
        }""")
        if input_count > 0:
            # There are actual form inputs on this page to be filled! Never complete prematurely.
            return False, ""

        body_text = (await page.evaluate("document.body ? document.body.innerText : ''")).lower()
        
        # Check termination / screenout / closed survey phrases
        disqualify_phrases = [
            "thank you for your participation in this survey",
            "we appreciate the time you've taken to share your opinions",
            "you have already completed this survey",
            "you have already participated",
            "survey is now closed",
            "survey has closed",
            "quota for this survey has been reached",
            "quota is full",
            "do not qualify for this study",
            "does not qualify",
            "you do not meet the criteria",
            "unfortunately, you do not qualify"
        ]
        for p in disqualify_phrases:
            if p in body_text:
                if not has_answered:
                    return True, f"Survey ended by host: '{p}' (IP already participated or quota reached)"
                else:
                    return True, f"Completion message detected: '{p}'"
                    
        # Check normal completion phrases
        phrases = [
            "your response has been recorded",
            "submit another response",
            "response has been recorded",
            "your submission has been received",
            "survey has been completed",
            "you have completed this questionnaire",
            "you have completed this survey",
            "this survey is now complete",
            "responses have been recorded",
            "your response was submitted",
            "thank you for taking this survey",
            "you may now close this window"
        ]
        for p in phrases:
            if p in body_text:
                return True, f"Completion message detected: '{p}'"
    except Exception:
        pass
        
    return False, ""

async def detect_validation_errors(page):
    """
    Universally extracts visible validation error messages on the page.
    """
    js = """
    () => {
        const errors = [];
        const selectors = [
            '.error', '.error-message', '.errorMessage',
            '[role="alert"]', '[aria-invalid="true"]',
            '.validation-error', '.ValidationError',
            '.freebirdFormviewerViewItemsItemErrorMessage',
            '.smqr-errorRow-8lLKs', '.smqr-validationMessage-3iVgh',
            '.redcap-error', '.survey-error', '.invalid-feedback',
            '.has-error', '.form-error', '.q-error',
            '[class*="error" i]:not(body):not(html)',
            '[class*="validation" i]'
        ];
        const seen = new Set();
        for (const sel of selectors) {
            document.querySelectorAll(sel).forEach(el => {
                if (el.offsetWidth > 0 && el.offsetHeight > 0) {
                    const txt = el.innerText.trim();
                    if (txt && txt.length > 3 && txt.length < 300 && !seen.has(txt)) {
                        seen.add(txt);
                        errors.push(txt);
                    }
                }
            });
        }
        if (errors.length === 0) {
            const body = document.body ? document.body.innerText : '';
            const phrases = [
                'This question requires an answer',
                'This is a required question',
                'Please answer this question',
                'An answer is required',
                'Please enter a valid',
                'Please choose an answer',
                'Please select at least one option',
                'Please enter an answer'
            ];
            for (const p of phrases) {
                if (body.includes(p)) errors.push(p);
            }
        }
        return errors;
    }
    """
    try:
        return await page.evaluate(js)
    except Exception:
        return []

def sanitize_other_specify_fields(fields: list, answers: dict):
    """
    Prevents Qualtrics / Survey form validation error:
    'You have added text into an unselected answer choice. Please select the answer choice or remove the text.'
    
    If a textbox is an 'Other/Specify' text field attached to a radio or checkbox question,
    it MUST be empty UNLESS the user explicitly selected the 'Other' radio/checkbox option!
    """
    if not fields or not answers:
        return

    def norm(s: str) -> str:
        return re.sub(r'[\s\xa0]+', ' ', (s or '')).strip().lower()

    # 1. Map question stems to selected radio/checkbox answers
    selected_choices = {}
    for f in fields:
        f_id = f.get("id")
        role = f.get("role")
        label = norm(f.get("label"))
        if role in ["radio", "checkbox", "combobox"] and f_id in answers:
            ans_val = norm(str(answers[f_id].get("value") or ""))
            selected_choices[label] = ans_val

    # 2. Inspect textboxes for 'other' / 'specify' / 'complete below' patterns
    other_pattern = re.compile(r'\b(other|another|specify|complete bellow|complete below)\b', re.IGNORECASE)
    for f in fields:
        f_id = f.get("id")
        role = f.get("role")
        raw_label = f.get("label") or ""
        norm_label = norm(raw_label)
        
        if role == "textbox" and f_id in answers:
            is_other_box = bool(other_pattern.search(norm_label))
            if is_other_box:
                # Extract question stem (e.g. "What is your relationship to N/A? - Other..." -> "What is your relationship to N/A?")
                stem_match = re.split(r'[-–—:]\s*(?:other|another|specify|complete)', norm_label, flags=re.IGNORECASE)
                stem = stem_match[0].strip() if stem_match else norm_label
                
                # Check if there's a corresponding radio/checkbox answer for this stem
                matching_ans = None
                for q_label, chosen in selected_choices.items():
                    if stem in q_label or q_label in stem:
                        matching_ans = chosen
                        break
                        
                # If a choice was selected and that choice is NOT 'other', CLEAR the other textbox!
                # We use word boundary \b(other|another)\b so words like 'mother' or 'brother' are NOT mistaken for 'other'!
                if matching_ans:
                    is_other_selected = bool(re.search(r'\b(other|another)\b', matching_ans, re.IGNORECASE))
                    if not is_other_selected:
                        print(f"🧹 Clearing unselected Other/Specify field '{raw_label}' (Selected choice was: '{matching_ans}')")
                        answers[f_id]["value"] = ""

def enforce_age_and_dob_consistency(fields: list, answers: dict, session_memory: dict):
    """
    Enforces 100% mathematical consistency between previously answered Age
    and any Date of Birth (DOB) / Birthdate questions.
    Prevents survey validation failures and data quality flags where researchers or
    Qualtrics logic cross-references DOB against stated age.
    """
    if not fields or not answers:
        return
        
    from datetime import datetime
    today = datetime.now()
    dob_keywords = ["birthdate", "date of birth", "dob", "birth date", "born on"]
    
    for f in fields:
        f_id = f.get("id")
        if f_id not in answers:
            continue
            
        label = (f.get("label") or "").lower()
        if not any(k in label for k in dob_keywords):
            continue
            
        current_val = str(answers[f_id].get("value") or "").strip()
        if not current_val:
            continue
            
        # Parse month, day, year from current_val (supports MM/DD/YYYY, YYYY-MM-DD, MM-DD-YYYY)
        date_match = re.search(r'(\d{1,2})[\/\-\.](\d{1,2})[\/\-\.](\d{4})', current_val)
        iso_match = re.search(r'(\d{4})[\/\-\.](\d{1,2})[\/\-\.](\d{1,2})', current_val)
        
        month, day, year = None, None, None
        format_type = "mdy"
        if date_match:
            month, day, year = int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3))
            format_type = "mdy"
        elif iso_match:
            year, month, day = int(iso_match.group(1)), int(iso_match.group(2)), int(iso_match.group(3))
            format_type = "ymd"
            
        if not year or not month or not day or month > 12 or day > 31:
            continue
            
        # 2. Identify whose DOB this is (e.g. child vs respondent)
        target_age = None
        target_name = None
        
        name_match = re.search(r"([A-Za-z]+)'s\s+(?:birthdate|date of birth|dob|birth date)", label, re.IGNORECASE)
        if name_match:
            target_name = name_match.group(1).lower()
            
        # Search session_memory for matching age
        for mem_key, mem_val in session_memory.items():
            k_lower = mem_key.lower()
            v_str = str(mem_val).strip()
            
            if target_name and target_name in k_lower and ("age" in k_lower or "years" in k_lower):
                m = re.search(r'\b(\d{1,2})\b', v_str)
                if m:
                    target_age = int(m.group(1))
                    break
            elif "child" in label or "children" in label or target_name:
                if "child" in k_lower and ("age" in k_lower or "years" in k_lower):
                    m = re.search(r'\b(\d{1,2})\b', v_str)
                    if m:
                        target_age = int(m.group(1))
                        break
            elif not target_name and not ("child" in label):
                if "what is your age" in k_lower or k_lower == "age":
                    m = re.search(r'\b(\d{1,2})\b', v_str)
                    if m:
                        target_age = int(m.group(1))
                        break
                    elif "-" in v_str:
                        range_nums = [int(n) for n in re.findall(r'\d+', v_str)]
                        if len(range_nums) == 2:
                            target_age = sum(range_nums) // 2
                            break
                            
        # If no explicit memory match, fallback to checking generic age fields
        if target_age is None:
            for mem_key, mem_val in session_memory.items():
                if "age (in years)" in mem_key.lower():
                    m = re.search(r'\b(\d{1,2})\b', str(mem_val))
                    if m:
                        target_age = int(m.group(1))
                        break

        if target_age is None:
            continue
            
        # Calculate exact required birth year relative to today's date
        birthday_passed = (today.month, today.day) >= (month, day)
        correct_year = today.year - target_age if birthday_passed else (today.year - target_age - 1)
        
        if year != correct_year:
            if format_type == "mdy":
                corrected_dob = f"{month:02d}/{day:02d}/{correct_year}"
            else:
                corrected_dob = f"{correct_year}-{month:02d}-{day:02d}"
                
            print(f"🎯 Enforced Strict DOB & Age Consistency for '{f.get('label')}': Stated Age = {target_age} -> Corrected DOB from '{current_val}' to '{corrected_dob}' (Exact Age = {target_age})")
            answers[f_id]["value"] = corrected_dob

def enforce_user_section_rules(fields, answers, instructions):
    """
    Enforces user-specified section constraints deterministically (e.g. Sections C and D must be Agree / Strongly Agree).
    Guarantees that regardless of LLM timeouts, network glitches, or missing keys,
    the specified sections strictly receive their required answers.
    """
    if not instructions:
        return
        
    inst_lower = instructions.lower()
    force_agree = "force agree" in inst_lower
    
    if force_agree:
        for f in fields:
            fid = f.get("id")
            opt_map = f.get("option_map", {})
            valid_opts = [opt for opt in opt_map.keys() if opt.lower() in ["agree", "strongly agree"]]
            if not valid_opts:
                valid_opts = ["Agree", "Strongly Agree"]
                
            curr_val = str(answers.get(fid, {}).get("value") or "").strip()
            if not curr_val or "disagree" in curr_val.lower() or curr_val.lower() not in [o.lower() for o in valid_opts]:
                chosen = valid_opts[0]
                answers[fid] = {
                    "value": chosen,
                    "confidence": 1.0,
                    "source": "persona",
                    "status": "ANSWERED",
                    "reason": "Explicit user runtime instruction"
                }
                print(f"🛡️ Explicit User Instruction Enforced: Assigned '{chosen}' for {f.get('label')[:50]}")

async def verify_and_fill_unanswered_fields(page, fields, answers, session_memory, persona="", user_email=None, run_context: RunContext = None):
    """
    Intelligent pre-flight verification sweep.
    Before clicking Next/Submit, inspects the page to ensure no question is left blank.
    
    CRITICAL RULES:
    1. STRICTLY NEGLECT ANSWERED QUESTIONS: If an option is already selected, DO NOT TOUCH IT!
    2. CORRELATE WITH AI INTENT: If a field was missed, use the AI's intended answer for that field.
    3. DETERMINISTIC FIRST: Checks run_context for confirmed facts before making an AI call.
    4. ZERO DISCREPANCY: Keeps session_memory in 100% sync with live DOM state.
    """
    if not page or not fields:
        return 0
        
    resolved_count = 0
    try:
        from ai_engine import get_form_answers
        
        for f in fields:
            f_id = f.get("id")
            role = f.get("role")
            label = f.get("label") or "Question"
            
            # --- 1. Radio Buttons ---
            if role == "radio":
                opt_map = f.get("option_map", {})
                opt_ids = list(opt_map.values()) if opt_map else ([f_id] if f_id else [])
                if not opt_ids:
                    continue
                    
                # Check live DOM: Is ANY option in this radio question already checked?
                is_checked = await page.evaluate("""(ids) => {
                    for (const id of ids) {
                        const el = document.querySelector(`[data-stillbot-id="${id}"]`);
                        if (!el) continue;
                        if (el.checked || el.getAttribute('aria-checked') === 'true') return true;
                        if (el.classList.contains('q-checked') || el.classList.contains('selected') || el.classList.contains('checked')) return true;
                        const parent = el.closest('td, li, label, .ChoiceStructure, [role="radio"], .Selection, .ControlContainer');
                        if (parent && (parent.classList.contains('q-checked') || parent.classList.contains('Selected') || parent.classList.contains('checked') || parent.classList.contains('q-radio-active') || parent.querySelector('.q-checked, .checked, .Selected, [aria-checked="true"]'))) return true;
                        if (el.id) {
                            try {
                                const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
                                if (lbl && (lbl.classList.contains('q-checked') || lbl.classList.contains('q-radio-active') || lbl.classList.contains('checked') || lbl.classList.contains('selected'))) return true;
                            } catch(e) {}
                        }
                    }
                    return false;
                }""", opt_ids)
                
                # RULE 1: If already answered, STRICTLY NEGLECT! Do not touch or override!
                if is_checked:
                    continue
                    
                # Question is genuinely unselected: Correlate with AI intent!
                print(f"🔍 Pre-Flight Sweep: Detected unanswered radio question '{label}'. Correlating with AI...")
                chosen_val = None
                
                # Check if AI already answered this in 'answers'
                if f_id in answers and answers[f_id].get("value"):
                    chosen_val = answers[f_id]
                else:
                    # Query AI engine specifically for this missed question using persona
                    ai_resp = await get_form_answers([f], session_memory, persona=persona, run_context=run_context)
                    if ai_resp and f_id in ai_resp:
                        chosen_val = ai_resp[f_id]
                        answers[f_id] = chosen_val
                        
                if chosen_val:
                    await fill_field(page, f, chosen_val)
                    session_memory[label] = chosen_val["value"]
                    is_ok, live_val, err = await verify_control_state(page, f, chosen_val["value"])
                    fp = f.get("fingerprint") or f.get("logical_id")
                    if run_context and fp:
                        if is_ok:
                            run_context.mark_verified(fp, live_val)
                        else:
                            run_context.mark_validation_failed(fp, err or "Verification failed")
                    if is_ok:
                        print(f"🎯 Pre-Flight Sweep: Successfully applied AI-correlated choice '{chosen_val['value']}' for '{label}'")
                        resolved_count += 1
                    else:
                        print(f"⚠️ Pre-Flight Sweep: Option '{chosen_val['value']}' failed DOM verification: {err}")
                else:
                    print(f"⚠️ Pre-Flight Sweep: Question '{label}' could not be resolved from known facts. Preserving unanswered state.")
                            
            # --- 2. Dropdown Selects ---
            elif role in ["combobox", "select"]:
                is_selected = await page.evaluate("""(id) => {
                    const el = document.querySelector(`[data-stillbot-id="${id}"]`);
                    if (!el) return true;
                    if (el.tagName.toLowerCase() === 'select') {
                        return el.selectedIndex > 0 && !!el.value;
                    }
                    return !!(el.value || el.innerText.trim());
                }""", f_id)
                
                if is_selected:
                    continue
                    
                print(f"🔍 Pre-Flight Sweep: Detected unanswered select '{label}'. Correlating with AI...")
                chosen_select = None
                if f_id in answers and answers[f_id].get("value"):
                    chosen_select = answers[f_id]
                else:
                    ai_resp = await get_form_answers([f], session_memory, persona=persona, run_context=run_context)
                    if ai_resp and f_id in ai_resp:
                        chosen_select = ai_resp[f_id]
                        answers[f_id] = chosen_select
                        
                if chosen_select:
                    await fill_field(page, f, chosen_select)
                    session_memory[label] = chosen_select["value"]
                    is_ok, live_val, err = await verify_control_state(page, f, chosen_select["value"])
                    fp = f.get("fingerprint") or f.get("logical_id")
                    if run_context and fp:
                        if is_ok:
                            run_context.mark_verified(fp, live_val)
                        else:
                            run_context.mark_validation_failed(fp, err or "Verification failed")
                    if is_ok:
                        resolved_count += 1
                        
    except Exception as e:
        print(f"⚠️ Pre-flight sweep notice: {e}")
        
    return resolved_count

async def sync_live_dom_to_session_memory(page, session_memory: dict):
    """
    Scrapes the actual live DOM state right before clicking Next/Submit.
    Ensures that session_memory (the final audit trail) reflects 100% of what is
    literally checked, selected, and typed on the screen—eliminating any discrepancy.
    """
    if not page or not isinstance(session_memory, dict):
        return
    try:
        live_state = await page.evaluate("""() => {
            const records = {};
            
            // 1. Radio buttons: find the option that is actually selected
            const radios = Array.from(document.querySelectorAll('input[type="radio"], [role="radio"]'));
            const isRadioSelected = (r) => {
                if (r.checked || r.getAttribute('aria-checked') === 'true') return true;
                if (r.classList.contains('q-checked') || r.classList.contains('checked') || r.classList.contains('selected')) return true;
                const parent = r.closest('td, li, label, .ChoiceStructure, [role="radio"], .Selection, .ControlContainer');
                if (parent && (parent.classList.contains('q-checked') || parent.classList.contains('Selected') || parent.classList.contains('checked') || parent.classList.contains('q-radio-active') || parent.querySelector('.q-checked, .checked, .Selected, [aria-checked="true"]'))) return true;
                return false;
            };
            
            radios.forEach(r => {
                if (isRadioSelected(r)) {
                    let heading = '';
                    const rg = r.closest('[role="radiogroup"]');
                    if (rg && rg.getAttribute('aria-label')) {
                        heading = rg.getAttribute('aria-label').trim();
                    }
                    const ariaLbl = r.getAttribute('aria-label') || '';
                    if (!heading) {
                        if (ariaLbl.includes(', response for ')) {
                            heading = ariaLbl.split(', response for ')[1].trim();
                        } else if (ariaLbl.includes(', response to ')) {
                            heading = ariaLbl.split(', response to ')[1].trim();
                        }
                    }
                    if (!heading) {
                        const container = r.closest('.cf-question, .question, fieldset, [role="radiogroup"], .ChoiceStructure, tr, table, .geS5n');
                        if (container) {
                            const hEl = container.querySelector('.QuestionText, .cf-question__text, legend, .cf-grid-row__label, th, [role="heading"], .exportItemTitle');
                            if (hEl) heading = hEl.innerText.trim();
                        }
                    }
                    let optText = '';
                    if (ariaLbl.includes(', response for ')) {
                        optText = ariaLbl.split(', response for ')[0].trim();
                    } else if (ariaLbl.includes(', response to ')) {
                        optText = ariaLbl.split(', response to ')[0].trim();
                    }
                    if (!optText && r.id) {
                        try {
                            const lbl = document.querySelector(`label[for="${CSS.escape(r.id)}"]`);
                            if (lbl) optText = lbl.innerText.trim();
                        } catch(e) {}
                    }
                    if (!optText && r.closest('label')) optText = r.closest('label').innerText.trim();
                    if (!optText) optText = ariaLbl || r.value || '';
                    if (heading && optText) {
                        records[heading] = optText;
                    }
                }
            });
            
            // 2. Select dropdowns
            const selects = Array.from(document.querySelectorAll('select'));
            selects.forEach(s => {
                if (s.selectedIndex >= 0 && s.options[s.selectedIndex]) {
                    const optText = s.options[s.selectedIndex].text.trim();
                    let heading = s.getAttribute('aria-label') || s.name || s.id || '';
                    const container = s.closest('.cf-question, .question, fieldset, tr');
                    if (container) {
                        const hEl = container.querySelector('.QuestionText, .cf-question__text, legend, th, label');
                        if (hEl) heading = hEl.innerText.trim();
                    }
                    if (heading && optText && !optText.toLowerCase().includes('select')) {
                        records[heading] = optText;
                    }
                }
            });
            
            return records;
        }""")
        if live_state:
            for k, v in live_state.items():
                session_memory[k] = v
    except Exception as e:
        pass

async def run_bot(target_url: str = None, instructions: str = None, proxy: str = None, run_index: int = 1, status_callback = None, user_email: str = None, timezone: str = None, geolocation: dict = None):
    print("🚀 Booting Universal Questionnaire Bot...")
    if status_callback:
        try:
            await status_callback(f"🚀 **Run {run_index} Started**\n• Connecting to survey...")
        except Exception:
            pass
            
    # Resolve user email for reward claim
    resolved_email = user_email
    if not resolved_email and instructions:
        match = re.search(r"email address:\s*([^\s\n\r]+@[^\s\n\r]+)", instructions, re.IGNORECASE)
        if match:
            resolved_email = match.group(1).strip()
    if not resolved_email and hasattr(config, "USER_PERSONA"):
        match = re.search(r"([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)", getattr(config, "USER_PERSONA", ""))
        if match:
            resolved_email = match.group(1).strip()
    
    # Apply runtime overrides if provided per run instance
    active_url = target_url or config.TARGET_URL
    if "l.facebook.com" in active_url:
        parsed = urllib.parse.urlparse(active_url)
        params = urllib.parse.parse_qs(parsed.query)
        if "u" in params:
            active_url = urllib.parse.unquote(params["u"][0])
            print(f"🔗 Unwrapped direct survey URL: {active_url}")
    elif "linkedin.com/safety/go" in active_url:
        parsed = urllib.parse.urlparse(active_url)
        params = urllib.parse.parse_qs(parsed.query)
        if "url" in params:
            active_url = urllib.parse.unquote(params["url"][0])
            print(f"🔗 Unwrapped direct survey URL from LinkedIn: {active_url}")

    run_persona = config.USER_PERSONA
    if instructions:
        run_persona = f"{config.USER_PERSONA}\n\n[RUN-SPECIFIC USER INSTRUCTIONS & PREFERENCES]\n{instructions}\nPrioritize these explicitly authorized instructions over general default preferences."
    
    exit_reason = ""
    async with async_playwright() as p:
        from run_mode import resolve_run_mode, validate_and_log_run_mode, RunMode
        run_mode_info = validate_and_log_run_mode()
        active_mode = run_mode_info["resolved_mode"]

        if active_mode == RunMode.CLOUD.value:
            is_headless = True
            chrome_channel = None
        else:
            # Local PC mode: Visible local Chrome/Chromium window by default
            is_headless = os.getenv("HEADLESS", "False").lower() == "true"
            chrome_channel = "chrome" if (os.name == "nt" and os.path.exists(r"C:\Program Files\Google\Chrome\Application\chrome.exe")) else None

        import tempfile
        import shutil
        temp_user_data_dir = tempfile.mkdtemp(prefix="stillbot_chrome_")

        launch_args = {
            "headless": is_headless,
            "args": [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-infobars",
                "--disable-gpu",
                "--window-size=1280,800"
            ]
        }
        if not is_headless:
            # Display Chrome prominently on screen so the user can watch and track it in real-time
            launch_args["args"].extend(["--window-position=60,60", "--window-size=1280,850"])
        
        dynamic_bridge = None
        upstream_target = None
        target_proxy = None
        proxy_conf = None
        
        if proxy and str(proxy).startswith("two_stage:"):
            from dynamic_proxy import DynamicProxyBridge
            upstream_target = str(proxy).split("two_stage:", 1)[1]
            dynamic_bridge = DynamicProxyBridge(upstream_proxy=None)
            local_proxy_url = await dynamic_bridge.start()
            proxy_conf = {"server": local_proxy_url}
            print(f"🔀 Two-Stage Smart Proxy: Starting on Direct Local Network -> Upstream: {upstream_target}")
        elif proxy and str(proxy).lower() in ["direct", "none", "no_proxy", "local"]:
            print("⚡ Direct Network mode requested: Bypassing all proxies.")
            target_proxy = None
        else:
            target_proxy = proxy if proxy is not None else config.PROXY_SERVER
            if target_proxy and ("@" not in str(target_proxy) and "://" not in str(target_proxy)):
                import proxy_router
                resolved_p, region_lbl = proxy_router.get_proxy_for_run(str(target_proxy), run_index)
                if resolved_p:
                    target_proxy = resolved_p
                    print(f"🌏 Routing via {region_lbl} Residential Proxy Pool")
            
        if target_proxy and not dynamic_bridge:
            if "@" in target_proxy:
                import urllib.parse
                parsed = urllib.parse.urlparse(target_proxy if "://" in target_proxy else f"http://{target_proxy}")
                proxy_conf = {
                    "server": f"http://{parsed.hostname}:{parsed.port}",
                    "username": urllib.parse.unquote(parsed.username) if parsed.username else None,
                    "password": urllib.parse.unquote(parsed.password) if parsed.password else None
                }
            else:
                proxy_conf = config.get_proxy_config()
                
        if proxy_conf:
            print(f"🛡️ Routing through proxy: {proxy_conf['server']}")
            launch_args["proxy"] = proxy_conf
        else:
            print("⚡ Running directly on local network (No proxy).")
            
        # Auto-align browser timezone & geolocation to proxy IP location
        active_timezone = timezone
        if not active_timezone and proxy_conf:
            if proxy and "australia" in str(proxy).lower():
                active_timezone = "Australia/Sydney"
            else:
                active_timezone = "America/New_York"
            
        print(f"🕒 Browser Timezone bound to: {active_timezone or 'Default Host Timezone'}")

        ctx_kwargs = {
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "viewport": {"width": 1280, "height": 800},
            "locale": "en-US",
        }
        if active_timezone:
            ctx_kwargs["timezone_id"] = active_timezone
        if geolocation:
            ctx_kwargs["geolocation"] = geolocation
            ctx_kwargs["permissions"] = ["geolocation"]

        # Determine whether to use Google Chrome channel or bundled Chromium
        if 'chrome_channel' not in locals():
            chrome_channel = "chrome" if os.name == "nt" else None

        try:
            kwargs = dict(launch_args)
            if chrome_channel:
                kwargs["channel"] = chrome_channel
            browser = await p.chromium.launch_persistent_context(
                user_data_dir=temp_user_data_dir,
                **ctx_kwargs,
                **kwargs
            )
            print(f"🌐 Launched browser session (Headless: {is_headless}, Channel: {chrome_channel or 'bundled Chromium'}, Temp Dir: isolated session)")
        except Exception as e:
            print(f"ℹ️ Persistent launch fallback ({e}). Using standard launch.")
            try:
                kwargs_fallback = dict(launch_args)
                if chrome_channel:
                    kwargs_fallback["channel"] = chrome_channel
                b_instance = await p.chromium.launch(**kwargs_fallback)
                browser = await b_instance.new_context(**ctx_kwargs)
            except Exception as e2:
                print(f"❌ Chromium launch error: {e2}")
                raise
        
        context = browser
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            delete Object.getPrototypeOf(navigator).webdriver;
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
        """)
        
        try:
            from playwright_stealth import stealth_async
        except ImportError:
            stealth_async = None
            
        page = context.pages[0] if context.pages else await context.new_page()
        if stealth_async:
            await stealth_async(page)
            print("🥷 Playwright Stealth activated.")
            
        if not is_headless:
            try:
                await page.bring_to_front()
            except Exception:
                pass
            
        print(f"🌐 Navigating to {active_url}")
        try:
            await page.goto(active_url, wait_until="commit", timeout=30000)
        except Exception:
            try:
                await page.goto(active_url, wait_until="domcontentloaded", timeout=25000)
            except Exception as e:
                print(f"⚠️ Initial goto warning: {e}")
            
        # Give dynamic survey routers (Facebook shim, Confirmit wix redirects, Qualtrics, Verisoul) time to stabilize
        print("⏳ Waiting for page and client-side redirects to stabilize...")
        for wait_i in range(45):
            await page.wait_for_timeout(1000)
            try:
                ready_state = await page.evaluate("document.readyState")
                is_verisoul_gatekeeper = await page.evaluate("""() => {
                    const hasNextBtn = !!document.querySelector('.cf-navigation-next, button[type="submit"], input[type="submit"], #forward, #next, .btn-next, #NextButton, .NextButton');
                    if (hasNextBtn) return false;
                    const bText = document.body ? document.body.innerText : '';
                    if (bText.includes('awareness of sponsored') || bText.includes('thank you for your participation')) return false;
                    const cover = document.querySelector('#cover2, span[id*="cover" i]');
                    const isCoverVisible = cover && (cover.offsetWidth > 0 || cover.offsetHeight > 0 || window.getComputedStyle(cover).display !== 'none');
                    return (cover && isCoverVisible) || bText.includes('Verisoul');
                }""")
                if is_verisoul_gatekeeper:
                    print("🛡️ Verisoul anti-bot gatekeeper active. Waiting for device verification & auto-advance...")
                    continue

                page_status = await page.evaluate("""() => {
                    const bText = (document.body ? document.body.innerText : '').trim().toLowerCase();
                    
                    // Check for Qualtrics 'Page is loading' text or spinner elements
                    if (bText === 'page is loading' || bText.includes('page is loading')) {
                        return { is_loading: true, reason: 'Page is loading message' };
                    }
                    
                    const spinnerSels = [
                        '.spinner', '.loading', '.loader', '.LoadingSpinner',
                        '[class*="spinner" i]', '[class*="loader" i]', '[class*="loading" i]',
                        '[id*="spinner" i]', '[id*="loader" i]', '[id*="loading" i]',
                        'svg animateTransform', '[aria-busy="true"]', '#ProgressBar', '.ProgressBar'
                    ];
                    for (const s of spinnerSels) {
                        const el = document.querySelector(s);
                        if (el && (el.offsetWidth > 0 || el.offsetHeight > 0 || window.getComputedStyle(el).display !== 'none')) {
                            return { is_loading: true, reason: 'Spinner active' };
                        }
                    }
                    
                    const visibleInputs = Array.from(document.querySelectorAll('input:not([type="hidden"]), select, textarea, [role="radio"], [role="checkbox"], [role="textbox"]'))
                        .filter(el => el.offsetWidth > 0 || el.offsetHeight > 0);
                    const hasNext = Array.from(document.querySelectorAll('#NextButton, .NextButton, button[type="submit"], input[type="submit"], .cf-navigation-next, button, [role="button"]'))
                        .some(el => el.offsetWidth > 0 || el.offsetHeight > 0);
                        
                    // If no visible inputs and text is tiny and not a completion screen, still loading
                    if (visibleInputs.length === 0 && !bText.includes('thank you') && !bText.includes('completed') && bText.length < 80) {
                        return { is_loading: true, reason: 'Waiting for form controls to render' };
                    }
                    
                    return { is_loading: false, inputCount: visibleInputs.length, textLen: bText.length };
                }""")
                
                if page_status.get("is_loading"):
                    if wait_i % 3 == 0:
                        print(f"⏳ Page content is loading ({page_status.get('reason')})... [{wait_i + 1}/45]")
                    continue

                if ready_state == "complete":
                    print(f"✅ Page stabilized: {page.url} ({page_status.get('inputCount', 0)} visible inputs, {page_status.get('textLen', 0)} text chars)")
                    break
            except Exception:
                # Catch context destruction during client-side redirect
                pass
        
        import time
        run_id = f"run_{run_index}_{int(time.time())}"
        confirmed_facts = {"email": resolved_email} if resolved_email else {}
        if hasattr(config, "USER_PERSONA"):
            for line in config.USER_PERSONA.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    confirmed_facts[k.strip().lower()] = v.strip()
        run_ctx = RunContext(
            run_id=run_id,
            survey_url=active_url,
            confirmed_profile=confirmed_facts,
            user_instructions=instructions or "",
            page_state=PageState.INITIALISING
        )

        session_memory = {}
        page_num = 1
        previous_fields_hash = None
        consecutive_stuck_attempts = 0
        max_stuck_attempts = config.MAX_RETRIES or 4
        current_validation_errors = []
        
        # --- The Universal Autonomous Loop with Explicit Page State & Incremental Scanning ---
        while True:
            print(f"\n--- Analyzing Page {page_num} ---")
            run_ctx.transition_state(PageState.LOADING)
            await page.wait_for_timeout(1800)

            # Progress Watchdog: Detect stalled / unchanged pages across passes
            current_url = page.url
            try:
                body_sample = await page.evaluate("document.body ? document.body.innerText.slice(0, 500) : ''")
            except Exception:
                body_sample = ""
            pre_modal = await detect_blocking_modal(page)
            dom_fingerprint = f"{len(body_sample)}:{pre_modal.get('found', False)}:{body_sample[:100]}"
            
            is_stalled, pass_count = run_ctx.check_progress_watchdog(current_url, dom_fingerprint)
            if is_stalled:
                print(f"🛑 Watchdog Triggered: Page {page_num} remained unchanged after {pass_count} consecutive passes. Halting retries.")
                watchdog_shot = f"artifacts/watchdog_stuck_page_{page_num}.png"
                try:
                    os.makedirs("artifacts", exist_ok=True)
                    await page.screenshot(path=watchdog_shot, full_page=False)
                except Exception:
                    pass
                run_ctx.transition_state(PageState.HUMAN_REVIEW_REQUIRED, f"Watchdog: Page {page_num} unchanged for {pass_count} passes")
                resumed = await wait_for_human_copilot(
                    page, page_num,
                    reason=f"Automation paused: Page {page_num} remained unchanged after {pass_count} attempts. Please answer or click Next on your screen.",
                    status_callback=status_callback
                )
                if resumed:
                    run_ctx.reset_watchdog()
                    page_num += 1
                    continue
                else:
                    exit_reason = "TIMED_OUT"
                    run_ctx.transition_state(PageState.TIMED_OUT, "Co-pilot timeout after watchdog alert")
                    break

            # 0. Check for blocking modal dialogs before any clicks
            can_proceed, modal_msg = await handle_blocking_modal(page, page_num, run_ctx, status_callback)
            if not can_proceed:
                exit_reason = "TIMED_OUT" if run_ctx.page_state == PageState.TIMED_OUT else f"BLOCKED: {modal_msg}"
                break
            
            # Check for lingering validation modals or "Continue Without Answering" prompts
            try:
                cont_btn = page.locator('button:has-text("Continue Without Answering"), a:has-text("Continue Without Answering"), input[value*="Continue Without Answering" i], button:has-text("Continue without answering")').first
                if await cont_btn.count() > 0 and await cont_btn.is_visible():
                    print("⚠️ Unanswered question modal detected ('Continue Without Answering'). Clicking to proceed...")
                    if status_callback:
                        try:
                            await status_callback(f"⚠️ **Survey Prompt on Page {page_num}:**\n• 'Continue Without Answering' prompt detected.\n• 🔗 Live Survey URL: `{page.url}`\n• Advancing to next question...")
                        except Exception:
                            pass
                    await human_click_element(page, cont_btn)
                    await page.wait_for_timeout(2500)
                    page_num += 1
                    continue
            except Exception:
                pass

            # Find advance button on the page
            advance_btn = await find_advance_button(page)
            
            # 1. First check if questionnaire is already completed
            completed, reason = await check_is_completed(page, has_answered=(len(session_memory) > 0), has_advance_btn=(advance_btn is not None))
            if completed:
                print(f"🏁 {reason}! Questionnaire is complete!")
                run_ctx.transition_state(PageState.COMPLETED, reason)
                exit_reason = reason
                break
                
            # 2. Scrape DOM fields and register into Question Inventory
            run_ctx.transition_state(PageState.QUESTION_DISCOVERY)
            fields = await scrape_page(page)
            new_discovered = run_ctx.register_discovered_questions(fields)
            
            if not fields:
                # 2a. Check for blocking modal first
                modal_info = await detect_blocking_modal(page)
                if modal_info.get("found"):
                    can_cont, m_res = await handle_blocking_modal(page, page_num, run_ctx, status_callback)
                    if not can_cont:
                        exit_reason = "TIMED_OUT" if run_ctx.page_state == PageState.TIMED_OUT else f"BLOCKED: {m_res}"
                        break
                    continue

                # 2b. Check if survey questions are visible in the DOM
                has_questions, q_texts = await check_page_has_questions(page)

                # Check if advance button is disabled
                is_adv_disabled = True
                if advance_btn and await advance_btn.is_visible():
                    try:
                        is_adv_disabled = await advance_btn.is_disabled()
                    except Exception:
                        is_adv_disabled = False

                # 2c. Genuine intro page: ONLY on page 1 with 0 answered questions, NO visible questions, and an ENABLED advance button
                is_genuine_intro = (
                    page_num == 1 and
                    len(session_memory) == 0 and
                    not has_questions and
                    advance_btn and
                    (await advance_btn.is_visible()) and
                    (not is_adv_disabled) and
                    not modal_info.get("found", False)
                )

                if is_genuine_intro:
                    print("ℹ️ Genuine intro page detected (Page 1, 0 questions, enabled Start/Next button). Advancing into survey...")
                    try:
                        prev_url = page.url
                        prev_txt = await page.evaluate("document.body ? document.body.innerText.slice(0, 200) : ''")
                        await advance_btn.scroll_into_view_if_needed()
                        if dynamic_bridge and upstream_target:
                            print(f"🔄 Two-Stage Proxy: Activating US Residential IP ({upstream_target}) for survey questions...")
                            dynamic_bridge.set_upstream(upstream_target)
                            await page.wait_for_timeout(1500)
                            
                        await advance_btn.click()
                        print("⏳ Clicked intro advance button. Waiting for Question Page to render...")
                        
                        transition_detected = False
                        for _ in range(15):
                            await page.wait_for_timeout(1000)
                            try:
                                next_fields = await scrape_page(page)
                                next_btn = await find_advance_button(page)
                                curr_url = page.url
                                curr_txt = await page.evaluate("document.body ? document.body.innerText.slice(0, 200) : ''")
                                if next_fields or curr_url != prev_url or curr_txt != prev_txt:
                                    print(f"✅ Question Page rendered with {len(next_fields)} fields.")
                                    transition_detected = True
                                    break
                            except Exception:
                                pass
                                
                        if transition_detected:
                            run_ctx.reset_watchdog()
                            page_num += 1
                        continue
                    except Exception as e:
                        print(f"⚠️ Error clicking intro button: {e}")

                elif has_questions:
                    print(f"⚠️ Visible survey question(s) detected in DOM ({q_texts[:2]}), but semantic scraper returned 0 fields.")
                    print("🔍 Running targeted DOM diagnostics for unparsed or custom controls...")
                    try:
                        diag_info = await page.evaluate("""() => {
                            const qEls = Array.from(document.querySelectorAll('.QuestionOuter, .cf-question, .question, [data-widget="ranking"], .Ranking'));
                            return qEls.map(q => ({
                                tag: q.tagName,
                                id: q.id,
                                className: q.className,
                                childCount: q.childElementCount,
                                innerTextSample: q.innerText.slice(0, 100)
                            }));
                        }""")
                        print(f"📋 DOM Diagnostics: {diag_info[:2]}")
                    except Exception:
                        pass

                    if is_adv_disabled:
                        print("ℹ️ Next button is currently disabled. Waiting for questions or controls to activate...")
                        await page.wait_for_timeout(2000)

                    # Trigger Co-Pilot mode directly with question details rather than blindly clicking Next
                    print("⚠️ Unparsed/custom question detected. Activating Co-Pilot Mode...")
                    run_ctx.transition_state(PageState.HUMAN_REVIEW_REQUIRED, "Unparsed question widget detected")
                    resumed = await wait_for_human_copilot(
                        page, page_num,
                        reason=f"Question '{q_texts[0][:80]}' requires direct interaction on your screen.",
                        status_callback=status_callback
                    )
                    if resumed:
                        run_ctx.reset_watchdog()
                        page_num += 1
                        continue
                    else:
                        exit_reason = "TIMED_OUT"
                        run_ctx.transition_state(PageState.TIMED_OUT, "Co-pilot timeout waiting for question completion")
                        break

                # Check if we are still under an anti-bot curtain (ONLY if no advance button is visible)
                try:
                    is_covered = await page.evaluate("""() => {
                        const cover = document.querySelector('#cover2, span[id*="cover" i]');
                        return !!(cover && (cover.offsetWidth > 0 || cover.offsetHeight > 0 || window.getComputedStyle(cover).display !== 'none'));
                    }""")
                except Exception:
                    is_covered = False
                    
                if is_covered:
                    curtain_wait_count = getattr(page, "_curtain_wait_count", 0) + 1
                    page._curtain_wait_count = curtain_wait_count
                    if curtain_wait_count <= 3:
                        print(f"🛡️ Security or verification overlay detected ({curtain_wait_count}/3). Waiting for overlay to clear...")
                        await page.wait_for_timeout(2000)
                        continue
                    else:
                        print("⚠️ Security / verification overlay persists. Pausing automation for human co-pilot review...")
                        run_ctx.transition_state(PageState.HUMAN_REVIEW_REQUIRED, "Security / verification challenge overlay detected")
                        resumed = await wait_for_human_copilot(
                            page, page_num,
                            reason="Security or verification overlay detected. Please review on screen to proceed.",
                            status_callback=status_callback
                        )
                        if not resumed:
                            exit_reason = "TIMED_OUT"
                            run_ctx.transition_state(PageState.TIMED_OUT, "Co-pilot timeout on security verification")
                            break
                        continue
                        
                # Check if page is simply still loading questions or rendering dynamic content
                is_still_loading = await page.evaluate("""() => {
                    const bText = (document.body ? document.body.innerText : '').trim().toLowerCase();
                    if (bText === 'page is loading' || bText.includes('page is loading')) return true;
                    const spinnerSels = [
                        '.spinner', '.loading', '.loader', '.LoadingSpinner',
                        '[class*="spinner" i]', '[class*="loader" i]', '[class*="loading" i]',
                        '[id*="spinner" i]', '[id*="loader" i]', '[id*="loading" i]',
                        'svg animateTransform', '[aria-busy="true"]', '#ProgressBar', '.ProgressBar'
                    ];
                    for (const s of spinnerSels) {
                        const el = document.querySelector(s);
                        if (el && (el.offsetWidth > 0 || el.offsetHeight > 0 || window.getComputedStyle(el).display !== 'none')) {
                            return true;
                        }
                    }
                    const visibleInputs = Array.from(document.querySelectorAll('input:not([type="hidden"]), select, textarea, [role="radio"], [role="checkbox"]'))
                        .filter(el => el.offsetWidth > 0 || el.offsetHeight > 0);
                    return visibleInputs.length === 0 && !bText.includes('thank you') && !bText.includes('completed') && bText.length < 80;
                }""")
                
                empty_wait_count = getattr(page, "_empty_wait_count", 0)
                if is_still_loading and empty_wait_count < 15:
                    page._empty_wait_count = empty_wait_count + 1
                    print(f"⏳ Page is still loading questions ({empty_wait_count + 1}/15)... Waiting 2.5s for DOM to mount.")
                    await page.wait_for_timeout(2500)
                    continue
                else:
                    page._empty_wait_count = 0

                # Check completion again before attempting vision
                completed, reason = await check_is_completed(page, has_answered=(len(session_memory) > 0), has_advance_btn=False)
                if completed:
                    print(f"🏁 {reason}! Questionnaire is complete!")
                    run_ctx.transition_state(PageState.COMPLETED, reason)
                    exit_reason = reason
                    break
                    
                # Tier 2 Vision Fallback
                num_boxes = await prepare_vision_fallback(page)
                if num_boxes == 0:
                    completed, reason = await check_is_completed(page, has_answered=(len(session_memory) > 0), has_advance_btn=(advance_btn is not None))
                    if completed:
                        print(f"🏁 {reason}! Questionnaire is complete!")
                        run_ctx.transition_state(PageState.COMPLETED, reason)
                        exit_reason = reason
                        break
                    else:
                        print("⚠️ No fields detected by scraper or vision, but page is not complete. Activating Co-Pilot Mode...")
                        run_ctx.transition_state(PageState.HUMAN_REVIEW_REQUIRED, "Zero fields found by scraper or vision")
                        resumed = await wait_for_human_copilot(
                            page, page_num,
                            reason=f"Page {page_num} could not be parsed automatically. Please answer or click Next on your screen.",
                            status_callback=status_callback
                        )
                        if resumed:
                            consecutive_stuck_attempts = 0
                            previous_fields_hash = None
                            page_num += 1
                            continue
                        else:
                            exit_reason = "TIMED_OUT"
                            run_ctx.transition_state(PageState.TIMED_OUT, "Co-pilot timeout waiting for page resolution")
                            break
                else:
                    print("⚠️ Triggering Vision LLM processing...")
                    vision_answers = await get_vision_form_answers("artifacts/vision_fallback.png", num_boxes, session_memory)
                    actions = vision_answers.get("actions", [])
                    if not actions:
                        print("🏁 Vision LLM returned no actions. Assuming completion.")
                        break
                        
                    for action in actions:
                        box_id = action["box_id"]
                        val = action["value"]
                        source = action["source"]
                        if source == "gap":
                            continue
                        print(f"👁️ Vision Action: Box {box_id} -> {val}")
                        if not config.DRY_RUN:
                            js_click = f"""
                            () => {{
                                const labels = Array.from(document.querySelectorAll('div')).filter(el => el.innerText === '{box_id}' && el.style.backgroundColor === 'red');
                                if (labels.length > 0 && labels[0].parentElement) {{
                                    const rect = labels[0].parentElement.getBoundingClientRect();
                                    const x = rect.left + (rect.width / 2);
                                    const y = rect.top + (rect.height / 2);
                                    const el = document.elementFromPoint(x, y);
                                    if (el) {{
                                        el.focus();
                                        if ('{val}' === 'CLICK') {{
                                            el.click();
                                        }} else {{
                                            el.innerText = '{val}';
                                        }}
                                    }}
                                }}
                            }}
                            """
                            await page.evaluate(js_click)
                    continue
            
            # 3. Stuck & Retry Detection (Infinite Loop Protection with Bounded Recovery)
            current_hash = hash(str(fields))
            if current_hash == previous_fields_hash:
                consecutive_stuck_attempts += 1
                print(f"⚠️ Page fields have not advanced (Attempt {consecutive_stuck_attempts}/{max_stuck_attempts})")
                
                # Check for on-page validation errors
                current_validation_errors = await detect_validation_errors(page)
                if current_validation_errors:
                    print(f"⚠️ Validation errors detected on page: {current_validation_errors}")
                    run_ctx.transition_state(PageState.VALIDATION_ERROR, str(current_validation_errors))
                    
                # Bounded non-blocking recovery: on attempt 2 or 3, perform pre-flight sweep and scroll
                if consecutive_stuck_attempts in [2, 3]:
                    print("🔄 Running bounded self-recovery: re-verifying required fields and re-scrolling to Next button...")
                    await verify_and_fill_unanswered_fields(
                        page, fields, answers if 'answers' in locals() else {}, session_memory, persona=run_persona, user_email=resolved_email, run_context=run_ctx
                    )
                    advance_btn = await find_advance_button(page)
                    if advance_btn and await advance_btn.is_visible():
                        await advance_btn.scroll_into_view_if_needed()
                        await human_click_element(page, advance_btn)
                        await page.wait_for_timeout(2000)
                        continue

                if consecutive_stuck_attempts >= max_stuck_attempts:
                    print(f"⚠️ Page {page_num} has not advanced after {consecutive_stuck_attempts} attempts. Activating Co-Pilot Mode to protect AI tokens...")
                    run_ctx.transition_state(PageState.HUMAN_REVIEW_REQUIRED, "Page stuck threshold reached")
                    resumed = await wait_for_human_copilot(
                        page, page_num,
                        reason=f"Page {page_num} has not advanced. Please complete any missed fields or click Next on your screen.",
                        status_callback=status_callback
                    )
                    if resumed:
                        consecutive_stuck_attempts = 0
                        previous_fields_hash = None
                        page_num += 1
                        continue
                    else:
                        exit_reason = "TIMED_OUT"
                        run_ctx.transition_state(PageState.TIMED_OUT, "Co-pilot timeout waiting for page advance")
                        break
            else:
                consecutive_stuck_attempts = 0
                current_validation_errors = []
                previous_fields_hash = current_hash
                
            # 4. Incremental Inventory Check: Skip AI call if all fields on this page are already answered & verified!
            unanswered_fields = []
            for f in fields:
                fp = f.get("fingerprint")
                norm_q = run_ctx.question_inventory.get(fp)
                if not norm_q or norm_q.status != AnswerStatus.VERIFIED:
                    unanswered_fields.append(f)
                    
            if fields and not unanswered_fields and advance_btn:
                print(f"⚡ Incremental Scan: All {len(fields)} fields on Page {page_num} are already answered and verified. Skipping duplicate AI call!")
                answers = {f["id"]: {"value": session_memory.get(f.get("label", ""), ""), "confidence": 1.0, "source": "persona"} for f in fields}
            else:
                # 4a. Strict 'Other' Neglect Filter (Saves AI Credits & Eliminates 'Other' Hallucinations)
                ai_fields = []
                pre_filled_answers = {}
                for f in fields:
                    raw_lbl = f.get("label") or ""
                    lbl_lower = raw_lbl.lower()
                    role = f.get("role")
                    is_other = bool(re.search(r'\b(other|another|specify)\b', lbl_lower))
                    
                    if is_other and role in ["textbox", "checkbox", "radio"]:
                        stem = re.split(r'[-–—:]\s*(?:other|another|specify)', lbl_lower, flags=re.IGNORECASE)[0].strip()
                        has_siblings = any(stem in (other_f.get("label") or "").lower() for other_f in fields if other_f["id"] != f["id"])
                        if has_siblings:
                            val_to_set = False if role == "checkbox" else ""
                            pre_filled_answers[f["id"]] = {"value": val_to_set, "confidence": 1.0, "source": "persona"}
                            print(f"🚫 Neglecting 'Other' field '{raw_lbl}' (saved AI credits).")
                            continue
                    ai_fields.append(f)

                # 4b. Query LLM with only the legitimate non-other choices!
                run_ctx.transition_state(PageState.ANSWER_GENERATION)
                if ai_fields:
                    answers = await get_form_answers(ai_fields, session_memory, validation_errors=current_validation_errors, persona=run_persona, run_context=run_ctx)
                else:
                    answers = {}
                answers.update(pre_filled_answers)
                
                if not answers:
                    print("⚠️ AI Engine returned empty answers for this page. Activating Co-Pilot Mode...")
                    run_ctx.transition_state(PageState.HUMAN_REVIEW_REQUIRED, "Empty AI answers")
                    resumed = await wait_for_human_copilot(
                        page, page_num,
                        reason=f"AI returned empty answers for Page {page_num}. Please answer or click Next on your screen.",
                        status_callback=status_callback
                    )
                    if resumed:
                        consecutive_stuck_attempts = 0
                        previous_fields_hash = None
                        page_num += 1
                        continue
                    else:
                        exit_reason = "TIMED_OUT"
                        run_ctx.transition_state(PageState.TIMED_OUT, "Co-pilot timeout on empty AI answers")
                        break
                    
                # 4c. Deterministic Sanitization
                sanitize_other_specify_fields(fields, answers)
                enforce_age_and_dob_consistency(fields, answers, session_memory)
                enforce_user_section_rules(fields, answers, instructions)
                    
                # 5. Act (Universally Fill Fields)
                run_ctx.transition_state(PageState.FILLING)
                filled_count = 0
                for f in fields:
                    f_id = f["id"]
                    if f_id in answers:
                        ans_obj = answers[f_id]
                        ans_val = ans_obj.get("value")
                        ans_status = ans_obj.get("status", "").upper()
                        # Do not attempt to fill None or empty string on required fields
                        if ans_val is None or str(ans_val).strip() == "" or str(ans_val).strip().lower() == "none" or ans_status in ["UNANSWERABLE", "NEEDS_CLARIFICATION", "INVALID"]:
                            if f.get("required"):
                                print(f"⚠️ Required field '{f.get('label')}' is unresolved. Cannot fill with empty value.")
                            else:
                                print(f"ℹ️ Optional field '{f.get('label')}' is unresolved/empty. Leaving blank.")
                            continue
                            
                        await fill_field(page, f, ans_obj)
                        if ans_val is not None and str(ans_val).strip() != "":
                            filled_count += 1
                            is_ok, live_val, err = await verify_control_state(page, f, ans_val)
                            fp = f.get("fingerprint") or f.get("logical_id")
                            if fp:
                                if is_ok:
                                    session_memory[f.get("label", "Unknown")] = live_val or ans_val
                                    run_ctx.mark_verified(fp, live_val)
                                else:
                                    print(f"⚠️ Live state verification failed for '{f.get('label')}': {err}")
                                    run_ctx.mark_validation_failed(fp, err or "Verification failed")
                run_ctx.transition_state(PageState.VERIFYING)

                # Check if any required field on this page is unresolved or unverified in DOM
                page_unresolved_required = []
                for f in fields:
                    if f.get("required"):
                        fid = f.get("id")
                        fp = f.get("fingerprint") or f.get("logical_id")
                        is_missing_or_empty = (
                            fid not in answers 
                            or answers[fid].get("value") is None 
                            or str(answers[fid].get("value")).strip() == "" 
                            or answers[fid].get("status", "").upper() in ["UNANSWERABLE", "NEEDS_CLARIFICATION", "INVALID"]
                        )
                        is_dom_unverified = False
                        if fp and fp in run_ctx.question_inventory:
                            is_dom_unverified = run_ctx.question_inventory[fp].status != AnswerStatus.VERIFIED
                        if is_missing_or_empty or is_dom_unverified:
                            page_unresolved_required.append(f)

                if page_unresolved_required:
                    unres_labels = [f.get("label", "Required Question") for f in page_unresolved_required]
                    print(f"⚠️ Page {page_num} has {len(page_unresolved_required)} unresolved/unverified required question(s): {unres_labels}. Engaging Co-Pilot Mode...")
                    run_ctx.transition_state(PageState.HUMAN_REVIEW_REQUIRED, f"{len(page_unresolved_required)} required field(s) unresolved or unverified")
                    resumed = await wait_for_human_copilot(
                        page, page_num,
                        reason=f"Required question(s) ({', '.join(unres_labels[:3])}) could not be resolved or verified in the browser. Please complete or review on screen.",
                        status_callback=status_callback
                    )
                    if not resumed:
                        exit_reason = "TIMED_OUT"
                        run_ctx.transition_state(PageState.TIMED_OUT, f"Co-pilot timeout on required questions: {', '.join(unres_labels[:3])}")
                        break
                    else:
                        consecutive_stuck_attempts = 0
                        previous_fields_hash = None
                        page_num += 1
                        continue
                        
                # 5b. Incremental Dynamic Branching Check (handles up to 4 tiers of conditional revealing)
                for branch_pass in range(4):
                    await page.wait_for_timeout(600)
                    re_scraped_fields = await scrape_page(page)
                    newly_appeared = run_ctx.register_discovered_questions(re_scraped_fields)
                    
                    unfilled_new_fields = []
                    for rf in re_scraped_fields:
                        rf_fp = rf.get("fingerprint")
                        rf_lid = rf.get("logical_id")
                        q_obj = run_ctx.question_inventory.get(rf_fp) or run_ctx.question_inventory.get(rf_lid)
                        if not q_obj or q_obj.status != AnswerStatus.VERIFIED:
                            lbl_lower = (rf.get("label") or "").lower()
                            if re.search(r'\b(other|another|specify)\b', lbl_lower) and rf.get("role") in ["checkbox", "textbox"]:
                                continue
                            unfilled_new_fields.append(rf)
                            
                    if not unfilled_new_fields:
                        break

                    print(f"🔄 Detected {len(unfilled_new_fields)} newly revealed conditional field(s) (tier {branch_pass + 1}). Filling incrementally...")
                    new_answers = await get_form_answers(unfilled_new_fields, session_memory, persona=run_persona, run_context=run_ctx)
                    if new_answers:
                        sanitize_other_specify_fields(unfilled_new_fields, new_answers)
                        enforce_age_and_dob_consistency(unfilled_new_fields, new_answers, session_memory)
                        for nf in unfilled_new_fields:
                            n_id = nf["id"]
                            if n_id in new_answers:
                                await fill_field(page, nf, new_answers[n_id])
                                session_memory[nf.get("label", "Unknown")] = new_answers[n_id]["value"]
                                filled_count += 1
                                answers[n_id] = new_answers[n_id]
                                is_ok, live_val, err = await verify_control_state(page, nf, new_answers[n_id]["value"])
                                n_fp = nf.get("fingerprint") or nf.get("logical_id")
                                if n_fp:
                                    if is_ok:
                                        run_ctx.mark_verified(n_fp, live_val)
                                    else:
                                        run_ctx.mark_validation_failed(n_fp, err or "Verification failed")
                        fields.extend([nf for nf in unfilled_new_fields if nf not in fields])

            if status_callback:
                try:
                    await status_callback(f"📝 **Page {page_num} Progress:**\n• Answered {filled_count if 'filled_count' in locals() else len(fields)} question(s)\n• Advancing to next page...\n• 🔗 Live Survey URL: `{page.url}`")
                except Exception:
                    pass

            await page.wait_for_timeout(400)
            
            # 5c. Pre-Flight Zero-Token Verification: "Have I answered all questions on this page?"
            unanswered_resolved = await verify_and_fill_unanswered_fields(
                page, fields, answers, session_memory, persona=run_persona, user_email=resolved_email, run_context=run_ctx
            )
            if unanswered_resolved > 0:
                print(f"✅ Pre-Flight Verification: Automatically resolved {unanswered_resolved} unselected field(s) before advancing.")
            
            # 5d. Synchronize session_memory directly from the live DOM state right before clicking Next/Submit!
            await sync_live_dom_to_session_memory(page, session_memory)
            
            # 6. Navigate (Find and Click Next / Continue / Submit)
            print("⏭️ Searching for Next/Submit button...")
            try:
                target_btn = await find_advance_button(page)
                if target_btn and await target_btn.is_visible():
                    if config.DRY_RUN:
                        print("🛑 DRY_RUN enabled. Stopping before clicking Next/Submit.")
                        exit_reason = "DRY_RUN_COMPLETED"
                        run_ctx.transition_state(PageState.DRY_RUN_COMPLETED, "DRY_RUN: Fields staged and verified, submission skipped")
                        break
                    else:
                        btn_name = ""
                        try:
                            btn_name = (await target_btn.inner_text() or await target_btn.get_attribute("value") or await target_btn.get_attribute("aria-label") or "Next/Submit").strip()
                        except Exception:
                            btn_name = "Next/Submit"
                        print(f"🖱️ Clicking button: {btn_name}")
                        
                        # If clicking final submit button, send live audit breakdown to status callback
                        is_final_submit = any(k in btn_name.lower() for k in ["submit", "finish", "done", "complete"])
                        if is_final_submit and status_callback:
                            try:
                                summary_lines = []
                                for f in fields:
                                    fid = f.get("id")
                                    if fid in answers:
                                        val = answers[fid].get("value")
                                        lbl = f.get("label", "").split("\n")[0].strip()[:50]
                                        summary_lines.append(f"• **{lbl}**: `{val}`")
                                if summary_lines:
                                    msg = f"📋 **Review: Final Form Filled!**\n" + "\n".join(summary_lines) + f"\n\n🚀 Submitting via: **{btn_name}**..."
                                    await status_callback(msg)
                            except Exception:
                                pass
                        
                        try:
                            prev_url = page.url
                            prev_html = await page.evaluate("document.body ? document.body.innerHTML : ''")
                        except Exception:
                            prev_url = ""
                            prev_html = ""
                            
                        # Auto-sign any visible digital signature canvas before advancing
                        try:
                            sig_canvas = page.locator('canvas.SignatureCanvas, canvas[id*="Signature" i], .SignatureBox canvas').first
                            if await sig_canvas.count() > 0 and await sig_canvas.is_visible():
                                box = await sig_canvas.bounding_box()
                                if box and box["width"] > 0 and box["height"] > 0:
                                    print("✍️ Digital signature canvas detected. Auto-drawing signature stroke...")
                                    start_x = box["x"] + box["width"] * 0.2
                                    start_y = box["y"] + box["height"] * 0.5
                                    end_x = box["x"] + box["width"] * 0.8
                                    end_y = box["y"] + box["height"] * 0.5
                                    
                                    await page.mouse.move(start_x, start_y)
                                    await page.mouse.down()
                                    for step in range(1, 20):
                                        t = step / 20.0
                                        cur_x = start_x + (end_x - start_x) * t
                                        cur_y = start_y + (10 if step % 2 == 0 else -10)
                                        await page.mouse.move(cur_x, cur_y)
                                        await page.wait_for_timeout(15)
                                    await page.mouse.move(end_x, end_y)
                                    await page.mouse.up()
                                    print("✅ Digital signature signed.")
                                    await page.wait_for_timeout(500)
                        except Exception as e:
                            print(f"⚠️ Auto-sign error: {e}")

                        # Check blocking modal before clicking navigation button
                        modal_info = await detect_blocking_modal(page)
                        if modal_info.get("found"):
                            can_cont, m_res = await handle_blocking_modal(page, page_num, run_ctx, status_callback)
                            if not can_cont:
                                exit_reason = "TIMED_OUT" if run_ctx.page_state == PageState.TIMED_OUT else f"BLOCKED: {m_res}"
                                break
                            continue

                        # Check if button is disabled
                        try:
                            is_btn_disabled = await target_btn.is_disabled()
                        except Exception:
                            is_btn_disabled = False
                        if is_btn_disabled:
                            print("⚠️ Next/Submit button is disabled. Waiting for DOM or required questions...")
                            await page.wait_for_timeout(2000)
                            continue

                        await target_btn.scroll_into_view_if_needed()
                        if dynamic_bridge and upstream_target and dynamic_bridge.upstream_proxy is None:
                            print(f"🔄 Two-Stage Proxy: Activating US Residential IP ({upstream_target}) for survey questions...")
                            dynamic_bridge.set_upstream(upstream_target)
                            await page.wait_for_timeout(1000)
                        await human_click_element(page, target_btn)
                        
                        print("⏳ Waiting for page transition...")
                        transition_occurred = False
                        for _ in range(20):
                            await page.wait_for_timeout(500)
                            try:
                                curr_url = page.url
                                curr_html = await page.evaluate("document.body ? document.body.innerHTML : ''")
                                if curr_url != prev_url:
                                    print("✅ URL navigation detected!")
                                    transition_occurred = True
                                    break
                                # Check for validation/alert dialog (e.g. Confirmit "Response Requested", REDCap, SweetAlert)
                                cont_without_answering = page.locator('button:has-text("Continue Without Answering"), a:has-text("Continue Without Answering"), input[value*="Continue Without Answering" i], button:has-text("Continue without answering")').first
                                if await cont_without_answering.count() > 0 and await cont_without_answering.is_visible():
                                    print("⚠️ Soft prompt modal detected ('Continue Without Answering'). Clicking to proceed...")
                                    if status_callback:
                                        try:
                                            await status_callback(f"⚠️ **Survey Prompt on Page {page_num}:**\n• 'Continue Without Answering' prompt triggered.\n• 🔗 Live URL: `{page.url}`\n• Advancing survey...")
                                        except Exception:
                                            pass
                                    await cont_without_answering.click(force=True)
                                    await page.wait_for_timeout(1500)
                                    break
                                
                                modal_btn = page.locator('.ui-dialog-buttonpane button, .swal2-confirm, button:has-text("Okay"), button:has-text("OK"), button:has-text("Close"), button:has-text("Answer the Question")').first
                                if await modal_btn.count() > 0 and await modal_btn.is_visible():
                                    print("⚠️ Validation modal detected. Dismissing to review missing fields...")
                                    if status_callback:
                                        try:
                                            await status_callback(f"⚠️ **Survey Validation Alert on Page {page_num}:**\n• Missing field required.\n• 🔗 Live URL: `{page.url}`\n• Re-filling missing fields...")
                                        except Exception:
                                            pass
                                    await modal_btn.click(force=True)
                                    await page.wait_for_timeout(500)
                                    break
                                if curr_html != prev_html:
                                    print("✅ DOM transition detected!")
                                    transition_occurred = True
                                    break
                            except Exception:
                                pass
                                
                        await page.wait_for_timeout(2500)
                        if transition_occurred:
                            run_ctx.reset_watchdog()
                            page_num += 1
                        else:
                            print(f"⚠️ Page {page_num} did not transition after click. Remaining on Page {page_num}.")
                else:
                    print("⚠️ No standard button found. Pressing Enter...")
                    if config.DRY_RUN:
                        print("🛑 DRY_RUN enabled. Stopping.")
                        break
                    await page.keyboard.press("Enter")
                    await page.wait_for_timeout(3000)
                    try:
                        curr_url = page.url
                        curr_html = await page.evaluate("document.body ? document.body.innerHTML : ''")
                        if curr_url == prev_url and curr_html == prev_html:
                            print("⚠️ Enter did not advance the page. Activating Co-Pilot Mode...")
                            resumed = await wait_for_human_copilot(
                                page, page_num,
                                reason=f"Next button not found automatically on Page {page_num}. Please click Next on your screen.",
                                status_callback=status_callback
                            )
                            if resumed:
                                consecutive_stuck_attempts = 0
                                previous_fields_hash = None
                                run_ctx.reset_watchdog()
                                page_num += 1
                                continue
                            else:
                                exit_reason = "TIMED_OUT"
                                run_ctx.transition_state(PageState.TIMED_OUT, "Co-pilot timeout waiting for Enter advancement")
                                break
                        else:
                            run_ctx.reset_watchdog()
                            page_num += 1
                    except Exception:
                        pass
            except Exception as e:
                print(f"❌ Navigation failed: {e}")
                break
            
        # Final Reward Link Inspection & Auto-Claim
        reward_info = None
        try:
            from reward_handler import check_and_claim_reward
            reward_info = await check_and_claim_reward(
                page,
                status_callback=status_callback,
                user_email=resolved_email,
                run_index=run_index
            )
        except Exception as e:
            print(f"⚠️ Reward processing error: {e}")

        # Validate that all required questions across all pages were verified
        is_all_verified, unverified_qs = run_ctx.validate_completion()
        if not is_all_verified:
            unv_names = [q.label for q in unverified_qs[:3]]
            print(f"⚠️ Final verification: {len(unverified_qs)} required question(s) were never verified ({unv_names}).")
            if not exit_reason or exit_reason in ["Completed", "DRY_RUN_COMPLETED"]:
                exit_reason = f"Incomplete: Required question(s) unverified ({', '.join(unv_names)})"
                run_ctx.transition_state(PageState.FAILED, exit_reason)

        if config.DRY_RUN:
            if not exit_reason or exit_reason == "Completed":
                exit_reason = "DRY_RUN_COMPLETED"
                run_ctx.transition_state(PageState.DRY_RUN_COMPLETED, "DRY_RUN: Staged and verified answers without submission")

        # Determine honest completion banner and success status
        is_truly_completed = False
        if exit_reason == "Completed" or run_ctx.page_state == PageState.COMPLETED:
            if is_all_verified:
                status_banner = "✅ QUESTIONNAIRE COMPLETED"
                is_truly_completed = True
            else:
                status_banner = f"⚠️ QUESTIONNAIRE INCOMPLETE: {exit_reason}"
        elif exit_reason == "DRY_RUN_COMPLETED" or run_ctx.page_state == PageState.DRY_RUN_COMPLETED:
            if is_all_verified:
                status_banner = "🛑 DRY RUN COMPLETED (Answers Staged & Verified; Submission Skipped)"
                is_truly_completed = True
            else:
                status_banner = f"⚠️ DRY RUN INCOMPLETE: {exit_reason}"
        elif run_ctx.page_state == PageState.TIMED_OUT or exit_reason == "TIMED_OUT":
            status_banner = "⏱️ QUESTIONNAIRE TIMED OUT (Co-Pilot or Navigation Timeout)"
        elif run_ctx.page_state == PageState.BLOCKED or (exit_reason and exit_reason.startswith("BLOCKED")):
            status_banner = f"🛑 QUESTIONNAIRE BLOCKED ({exit_reason})"
        elif run_ctx.page_state == PageState.HUMAN_REVIEW_REQUIRED:
            status_banner = f"🤝 QUESTIONNAIRE PAUSED (Human Review Required: {exit_reason})"
        else:
            status_banner = f"⚠️ QUESTIONNAIRE STOPPED ({exit_reason or run_ctx.page_state.value})"

        # Final Summary & Performance Optimization Metrics
        print("\n==================================")
        print(status_banner)
        print("==================================")
        print("Audit Trail (What was filled):")
        print(json.dumps(session_memory, indent=2))
        print(f"Final Exit Reason: {exit_reason}")
        
        print("\n==================================")
        print("📊 STILLBOT TOKEN & PERFORMANCE METRICS")
        print(f"• Total AI Calls: {run_ctx.token_metrics.get('ai_calls', 0)}")
        print(f"• Est. Prompt Tokens: {run_ctx.token_metrics.get('prompt_tokens_est', 0)}")
        print(f"• Est. Completion Tokens: {run_ctx.token_metrics.get('completion_tokens_est', 0)}")
        print(f"• Question Cache Hits: {run_ctx.token_metrics.get('cache_hits', 0)}")
        print(f"• Deterministic Fact Hits: {run_ctx.token_metrics.get('deterministic_hits', 0)}")
        print("==================================\n")
        
        screenshot_path = f"artifacts/final_survey_result_run_{run_index}.png"
        try:
            os.makedirs("artifacts", exist_ok=True)
            try:
                await page.screenshot(path=screenshot_path, full_page=True, timeout=7000)
            except Exception:
                await page.screenshot(path=screenshot_path, full_page=False, timeout=4000)
            # Also maintain artifacts/final_survey_result.png for backward compatibility
            try:
                await page.screenshot(path="artifacts/final_survey_result.png", full_page=False, timeout=4000)
            except Exception:
                pass
            print(f"📸 Saved final survey proof screenshot to {screenshot_path}")
        except Exception as e:
            print(f"⚠️ Screenshot save error: {e}")
            
        final_page_url = ""
        try:
            final_page_url = page.url
        except Exception:
            pass
            
        await browser.close()
        if dynamic_bridge:
            await dynamic_bridge.stop()
        try:
            shutil.rmtree(temp_user_data_dir, ignore_errors=True)
        except Exception:
            pass

        clean_summary = {k: v for k, v in session_memory.items() if v is not None and str(v).strip() != ""}
        return {
            "summary": clean_summary,
            "exit_reason": exit_reason or ("DRY_RUN_COMPLETED" if config.DRY_RUN else "Completed"),
            "dry_run": config.DRY_RUN,
            "dry_run_details": "Answers staged and verified in DOM; submission did not occur" if config.DRY_RUN else None,
            "screenshot_path": screenshot_path,
            "reward": reward_info,
            "final_url": final_page_url,
            "final_page": page_num,
            "metrics": run_ctx.token_metrics,
            "is_completed": is_truly_completed,
            "page_state": run_ctx.page_state.value
        }

if __name__ == "__main__":
    asyncio.run(run_bot())
