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
from scraper import scrape_page, prepare_vision_fallback
from ai_engine import get_form_answers, get_vision_form_answers

# --- Organic Curved Mouse Telemetry (Anti-Bot / Biometrics Evasion) ---
_LAST_MOUSE_POS = (500, 350)

def _bezier_pt(p0, p1, p2, p3, t):
    u = 1.0 - t
    return (
        (u**3)*p0[0] + 3.0*(u**2)*t*p1[0] + 3.0*u*(t**2)*p2[0] + (t**3)*p3[0],
        (u**3)*p0[1] + 3.0*(u**2)*t*p1[1] + 3.0*u*(t**2)*p2[1] + (t**3)*p3[1]
    )

async def human_curved_mouse_move(page, target_x, target_y, steps=25):
    """
    Moves the cursor using authentic organic curved zigzag trajectories
    with cubic Bezier interpolation, micro-jitter, and non-linear ease-in-out speed.
    """
    global _LAST_MOUSE_POS
    x0, y0 = _LAST_MOUSE_POS
    x3, y3 = target_x, target_y
    dx = x3 - x0
    dy = y3 - y0
    dist = math.hypot(dx, dy)
    
    if dist < 6:
        try:
            await page.mouse.move(x3, y3)
        except Exception:
            pass
        _LAST_MOUSE_POS = (x3, y3)
        return

    # Irregular curved zigzag offsets per user specifications
    spread = max(20.0, dist * 0.22)
    p1 = (x0 + dx * 0.3 + random.uniform(-spread, spread), y0 + dy * 0.2 + random.uniform(-spread, spread))
    p2 = (x0 + dx * 0.7 + random.uniform(-spread, spread), y0 + dy * 0.8 + random.uniform(-spread, spread))
    
    for i in range(steps + 1):
        t = i / float(steps)
        t_smooth = t * t * (3.0 - 2.0 * t)  # Smooth cubic ease-in-out
        bx, by = _bezier_pt((x0, y0), p1, p2, (x3, y3), t_smooth)
        jx = bx + random.uniform(-1.2, 1.2)
        jy = by + random.uniform(-1.2, 1.2)
        try:
            await page.mouse.move(jx, jy)
        except Exception:
            break
        await asyncio.sleep(random.uniform(0.008, 0.022))

    _LAST_MOUSE_POS = (x3, y3)
    await asyncio.sleep(random.uniform(0.05, 0.15))

async def human_click_element(page, locator):
    """Moves with a curved trajectory to the element and performs a realistic human click."""
    try:
        box = await locator.bounding_box()
        if box:
            target_x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
            target_y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
            await human_curved_mouse_move(page, target_x, target_y, steps=random.randint(20, 32))
            await asyncio.sleep(random.uniform(0.08, 0.22))
            await page.mouse.click(target_x, target_y)
            return True
    except Exception:
        pass
    await locator.click()
    return True

async def fill_field(page, field, answer):
    """
    Universally fills a field across any survey platform or web framework.
    Handles native HTML, React/Vue/Angular synthetic state, hidden inputs with custom labels,
    and custom ARIA comboboxes.
    """
    role = field.get("role")
    name = field.get("label")
    val = answer.get("value")
    source = answer.get("source")
    
    if source == "gap":
        print(f"⚠️ [WARNING] Persona gap detected for '{name}'. AI failed to infer a value. Leaving empty.")
        val = ""
    
    print(f"✍️ Filling [{role}] '{name}' with: {val}")
    
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

                # 5. Mandatory Gift Card Selection Mandate (Amazon is ALWAYS the required gift card):
                if option_map:
                    is_gift_card_field = any(k in (field.get("label") or "").lower() for k in ["gift card", "reward", "compensation", "incentive", "payment", "voucher"]) or any("amazon" in opt.lower() for opt in option_map)
                    if is_gift_card_field:
                        amazon_opt = next(((opt_text, opt_id) for opt_text, opt_id in option_map.items() if "amazon" in opt_text.lower()), None)
                        if amazon_opt:
                            matched_opt_name, target_id = amazon_opt
                            print(f"🎁 Mandatory Gift Card Mandate: Selected Amazon Gift Card ('{matched_opt_name}')")

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
            if isinstance(val, list):
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

        else:
            # Standard Textbox / Contenteditable
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

async def wait_for_human_copilot(page, page_num: int, reason: str = "", status_callback = None) -> bool:
    """
    Co-Pilot Mode (Human-in-the-Loop):
    Pauses autonomous automation without closing the browser and without burning any AI credits.
    Allows the user to answer questions or click Next directly on their screen.
    Monitors the page for URL or DOM changes, and automatically resumes once the user advances the page.
    """
    print("\n" + "═" * 72)
    print(f"🤝 CO-PILOT MODE ACTIVE (Page {page_num})")
    print("═" * 72)
    print(f"💡 Reason: {reason or 'Bot paused to prevent token waste and assist navigation'}")
    print("👉 Look at your Chrome screen: Answer any missed fields or click Next!")
    print("👀 The bot is watching live and will AUTO-RESUME as soon as you advance.")
    print("═" * 72 + "\n")
    
    if status_callback:
        try:
            await status_callback(
                f"🤝 **Co-Pilot Assistance Needed on Page {page_num}**\n\n"
                f"• {reason or 'Bot needs assistance on this page.'}\n"
                f"• **AI credit spending is paused.**\n"
                f"• 🔗 Live Survey URL: `{page.url}`\n\n"
                f"👉 **Please click Next or answer directly in the open Chrome browser.**\n"
                f"⚡ The bot will automatically detect when the page advances and resume!"
            )
        except Exception:
            pass

    initial_url = page.url
    try:
        initial_html = await page.evaluate("document.body ? document.body.innerText.slice(0, 300) : ''")
    except Exception:
        initial_html = ""

    # Monitor every 1.5 seconds with ZERO AI token consumption
    while True:
        try:
            await page.wait_for_timeout(1500)
            if page.is_closed():
                print("⚠️ Browser was closed during Co-Pilot mode.")
                return False

            current_url = page.url
            if current_url != initial_url:
                print(f"🎉 URL change detected ({current_url})! User advanced the page.")
                print("🚀 Co-Pilot: Resuming automated bot...")
                await page.wait_for_timeout(2000)
                return True

            current_html = await page.evaluate("document.body ? document.body.innerText.slice(0, 300) : ''")
            if current_html and current_html != initial_html:
                print("🎉 DOM transition detected! User advanced the page.")
                print("🚀 Co-Pilot: Resuming automated bot...")
                await page.wait_for_timeout(2000)
                return True
        except Exception as e:
            if "closed" in str(e).lower():
                return False
            await page.wait_for_timeout(1500)

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
    force_agree_cd = "force agree" in inst_lower or ("bowen" in inst_lower and "nursing" in inst_lower and "agree" in inst_lower)
    
    if force_agree_cd:
        import random
        for f in fields:
            lbl = (f.get("label") or "").upper()
            if "SECTION C" in lbl or "SECTION D" in lbl or "JOB PERFORMANCE" in lbl or "QUALITY OF NURSING CARE" in lbl:
                fid = f.get("id")
                opt_map = f.get("option_map", {})
                valid_opts = [opt for opt in opt_map.keys() if opt.lower() in ["agree", "strongly agree"]]
                if not valid_opts:
                    valid_opts = ["Agree", "Strongly Agree"]
                    
                curr_val = str(answers.get(fid, {}).get("value") or "").strip()
                if not curr_val or "disagree" in curr_val.lower() or curr_val.lower() not in [o.lower() for o in valid_opts]:
                    chosen = random.choice(valid_opts)
                    answers[fid] = {
                        "value": chosen,
                        "confidence": 1.0,
                        "source": "persona"
                    }
                    print(f"🛡️ Deterministic Rule Enforced: Assigned '{chosen}' for {f.get('label')[:50]}")

async def verify_and_fill_unanswered_fields(page, fields, answers, session_memory, persona="", user_email=None):
    """
    Intelligent pre-flight verification sweep.
    Before clicking Next/Submit, inspects the page to ensure no question is left blank.
    
    CRITICAL RULES:
    1. STRICTLY NEGLECT ANSWERED QUESTIONS: If an option is already selected, DO NOT TOUCH IT!
    2. CORRELATE WITH AI INTENT: If a field was missed, use the AI's intended answer for that field.
    3. NO BLIND GUESSING: If a field wasn't evaluated yet, queries the AI engine with persona instructions.
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
                    ai_resp = await get_form_answers([f], session_memory, persona=persona)
                    if ai_resp and f_id in ai_resp:
                        chosen_val = ai_resp[f_id]
                        answers[f_id] = chosen_val
                        
                if chosen_val:
                    await fill_field(page, f, chosen_val)
                    session_memory[label] = chosen_val["value"]
                    print(f"🎯 Pre-Flight Sweep: Successfully applied AI-correlated choice '{chosen_val['value']}' for '{label}'")
                    resolved_count += 1
                else:
                    # Fallback for reward / survey continuation only
                    lbl_lower = label.lower()
                    if any(k in lbl_lower for k in ["gift card", "reward", "incentive"]):
                        amz_opt = next((o for o in opt_map.keys() if "amazon" in o.lower()), None)
                        if amz_opt:
                            await fill_field(page, f, {"value": amz_opt, "confidence": 1.0, "source": "persona"})
                            session_memory[label] = amz_opt
                            resolved_count += 1
                            
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
                if f_id in answers and answers[f_id].get("value"):
                    await fill_field(page, f, answers[f_id])
                    session_memory[label] = answers[f_id]["value"]
                    resolved_count += 1
                else:
                    ai_resp = await get_form_answers([f], session_memory, persona=persona)
                    if ai_resp and f_id in ai_resp:
                        await fill_field(page, f, ai_resp[f_id])
                        answers[f_id] = ai_resp[f_id]
                        session_memory[label] = ai_resp[f_id]["value"]
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
        inst_lower = instructions.lower()
        if any(k in inst_lower for k in ["bowen", "nursing", "hospital", "synthetic", "scale", "nurse", "section a", "sections a", "respondents", "generate diverse"]):
            run_persona = f"[MANDATORY PRIMARY INSTRUCTIONS & PERSONA]\n{instructions}\nYou MUST strictly follow all rules, demographic distributions, and section constraints described above."
        else:
            run_persona = f"{config.USER_PERSONA}\n\n[CRITICAL OVERRIDES FOR THIS RUN]\n{instructions}\nYou MUST prioritize these overrides over your default persona settings."
    
    exit_reason = ""
    async with async_playwright() as p:
        # On Linux/Render servers without physical display, force headless=True
        if os.name != "nt":
            is_headless = True
        else:
            is_headless = os.getenv("HEADLESS", "False").lower() == "true"

        import tempfile
        import shutil
        temp_user_data_dir = tempfile.mkdtemp(prefix="stillbot_chrome_")

        launch_args = {
            "headless": is_headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-infobars",
                "--disable-gpu",
                "--webrtc-ip-handling-policy=disable_non_proxied_udp",
                "--force-webrtc-ip-handling-policy",
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
            print(f"🛡️ Routing through proxy: {proxy_conf['server']} (User: {proxy_conf.get('username', 'none')})")
            launch_args["proxy"] = proxy_conf
        else:
            print("⚡ Running directly on local network (No proxy).")
            
        # Auto-align browser timezone & geolocation to proxy IP location (prevent fraud/VPN screening)
        active_timezone = timezone
        if not active_timezone and proxy_conf:
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
            print("🌐 Launched Chromium with human hardware profile & fresh isolated session.")
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
        
        session_memory = {}
        page_num = 1
        previous_fields_hash = None
        consecutive_stuck_attempts = 0
        max_stuck_attempts = config.MAX_RETRIES or 3
        current_validation_errors = []
        
        # --- The Universal Autonomous Loop ---
        while True:
            print(f"\n--- Analyzing Page {page_num} ---")
            await page.wait_for_timeout(2000)
            
            # 0. Check for any lingering validation modals or "Continue Without Answering" prompts
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
                exit_reason = reason
                break
                
            # 2. Scrape DOM fields
            fields = await scrape_page(page)
            
            if not fields:
                # If no input fields exist but an advance button exists (e.g. VoiceFive/Qualtrics intro splash page)
                if advance_btn and await advance_btn.is_visible():
                    print("ℹ️ Intro / transition page detected (0 fields with Next/Start button). Advancing into survey...")
                    try:
                        await advance_btn.scroll_into_view_if_needed()
                        if dynamic_bridge and upstream_target:
                            print(f"🔄 Two-Stage Proxy: Activating US Residential IP ({upstream_target}) for survey questions...")
                            dynamic_bridge.set_upstream(upstream_target)
                            await page.wait_for_timeout(1500)
                            
                        await advance_btn.click()
                        print("⏳ Clicked intro advance button. Waiting for Question Page to render...")
                        
                        # Wait up to 15 seconds for the next question page to render
                        for _ in range(15):
                            await page.wait_for_timeout(1000)
                            try:
                                next_fields = await scrape_page(page)
                                next_btn = await find_advance_button(page)
                                if next_fields or next_btn:
                                    print(f"✅ Question Page rendered with {len(next_fields)} fields.")
                                    break
                            except Exception:
                                pass
                                
                        page_num += 1
                        continue
                    except Exception as e:
                        print(f"⚠️ Error clicking intro button: {e}")

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
                    if curtain_wait_count <= 10:
                        print(f"🛡️ Anti-bot curtain active ({curtain_wait_count}/10). Simulating organic mouse telemetry & waiting for Verisoul...")
                        target_pt = (random.randint(250, 1000), random.randint(200, 550))
                        await human_curved_mouse_move(page, target_pt[0], target_pt[1], steps=random.randint(20, 35))
                        await page.wait_for_timeout(1200)
                        continue
                    else:
                        print("⚠️ Anti-bot curtain wait reached limit (20s). Proceeding to survey inspection...")
                        
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
                    exit_reason = reason
                    break
                    
                # Tier 2 Vision Fallback
                num_boxes = await prepare_vision_fallback(page)
                if num_boxes == 0:
                    completed, reason = await check_is_completed(page, has_answered=(len(session_memory) > 0), has_advance_btn=(advance_btn is not None))
                    if completed:
                        print(f"🏁 {reason}! Questionnaire is complete!")
                        exit_reason = reason
                        break
                    else:
                        print("⚠️ No fields detected by scraper or vision, but page is not complete. Activating Co-Pilot Mode...")
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
                            exit_reason = "Survey paused or ended by user"
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
            
            # 3. Stuck & Retry Detection (Infinite Loop Protection with Error Recovery)
            current_hash = hash(str(fields))
            if current_hash == previous_fields_hash:
                consecutive_stuck_attempts += 1
                print(f"⚠️ Page fields have not advanced (Attempt {consecutive_stuck_attempts}/{max_stuck_attempts})")
                
                # Check for on-page validation errors
                current_validation_errors = await detect_validation_errors(page)
                if current_validation_errors:
                    print(f"⚠️ Validation errors detected on page: {current_validation_errors}")
                    
                if consecutive_stuck_attempts >= 2:
                    print(f"⚠️ Page {page_num} has not advanced after {consecutive_stuck_attempts} attempts. Activating Co-Pilot Mode to protect AI tokens...")
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
                        break
            else:
                consecutive_stuck_attempts = 0
                current_validation_errors = []
                previous_fields_hash = current_hash
                
            # 4. Strict 'Other' Neglect Filter (Saves AI Credits & Eliminates 'Other' Hallucinations)
            ai_fields = []
            pre_filled_answers = {}
            for f in fields:
                raw_lbl = f.get("label") or ""
                lbl_lower = raw_lbl.lower()
                role = f.get("role")
                is_other = bool(re.search(r'\b(other|another|specify)\b', lbl_lower))
                
                # Check if this is an "Other" option or "Other" textbox when standard choices exist
                if is_other and role in ["textbox", "checkbox", "radio"]:
                    stem = re.split(r'[-–—:]\s*(?:other|another|specify)', lbl_lower, flags=re.IGNORECASE)[0].strip()
                    has_siblings = any(stem in (other_f.get("label") or "").lower() for other_f in fields if other_f["id"] != f["id"])
                    if has_siblings:
                        # Auto-neglect: zero AI tokens wasted, immediately set to False / empty!
                        val_to_set = False if role == "checkbox" else ""
                        pre_filled_answers[f["id"]] = {"value": val_to_set, "confidence": 1.0, "source": "persona"}
                        print(f"🚫 Neglecting 'Other' field '{raw_lbl}' (saved AI credits).")
                        continue
                ai_fields.append(f)

            # 4b. Query LLM with only the legitimate non-other choices!
            if ai_fields:
                answers = await get_form_answers(ai_fields, session_memory, validation_errors=current_validation_errors, persona=run_persona)
            else:
                answers = {}
            # Merge pre-filled auto-neglected answers
            answers.update(pre_filled_answers)
            
            if not answers:
                print("⚠️ AI Engine returned empty answers for this page. Activating Co-Pilot Mode...")
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
                    break
                
            # 4c. Deterministic Sanitization: Prevent 'unselected answer choice' error for Other/Specify textboxes
            sanitize_other_specify_fields(fields, answers)
            enforce_age_and_dob_consistency(fields, answers, session_memory)
            enforce_user_section_rules(fields, answers, instructions)
                
            # 5. Act (Universally Fill Fields)
            filled_count = 0
            for f in fields:
                f_id = f["id"]
                if f_id in answers:
                    await fill_field(page, f, answers[f_id])
                    session_memory[f.get("label", "Unknown")] = answers[f_id]["value"]
                    filled_count += 1
                    
            # 5b. Dynamic Branching & Unhidden Field Check:
            # When answers are selected (e.g. Yes to Zoom, opt-ins, or condition screeners),
            # survey engines (REDCap, Qualtrics) dynamically reveal conditional questions on the same page.
            for branch_pass in range(3):
                await page.wait_for_timeout(800)
                re_scraped_fields = await scrape_page(page)
                
                # Check for any truly new visible fields revealed dynamically
                unfilled_new_fields = []
                known_ids = set(answers.keys())
                for rf in re_scraped_fields:
                    r_id = rf.get("id")
                    if r_id in known_ids or rf.get("label") in session_memory:
                        continue
                    
                    # Also skip neglected 'Other' fields
                    lbl_lower = (rf.get("label") or "").lower()
                    if re.search(r'\b(other|another|specify)\b', lbl_lower) and rf.get("role") in ["checkbox", "textbox"]:
                        continue

                    val = await page.evaluate(r"""(id) => {
                        const el = document.querySelector(`[data-stillbot-id="${id}"]`);
                        if (!el) return null;
                        const role = el.getAttribute('role') || el.type;
                        if (role === 'radio') {
                            if (el.getAttribute('aria-checked') === 'true') return 'checked';
                            const name = el.name;
                            if (name) {
                                const checked = document.querySelector(`input[name="${name}"]:checked`);
                                if (checked) return checked.value || 'checked';
                            }
                            const parentGroup = el.closest('[role="radiogroup"], fieldset, .geS5n, .cf-question');
                            if (parentGroup && parentGroup.querySelector('[aria-checked="true"], input:checked')) return 'checked';
                            return '';
                        }
                        if (role === 'checkbox') {
                            if (el.getAttribute('aria-checked') === 'true' || el.checked) return 'checked';
                            return '';
                        }
                        return el.value || '';
                    }""", r_id)
                    if not val:
                        unfilled_new_fields.append(rf)

                if not unfilled_new_fields:
                    break

                print(f"🔄 Detected {len(unfilled_new_fields)} newly revealed conditional field(s). Filling them...")
                new_answers = await get_form_answers(unfilled_new_fields, session_memory, persona=run_persona)
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
                    fields.extend([nf for nf in unfilled_new_fields if nf not in fields])

            if status_callback:
                try:
                    await status_callback(f"📝 **Page {page_num} Progress:**\n• Answered {filled_count} question(s)\n• Advancing to next page...\n• 🔗 Live Survey URL: `{page.url}`")
                except Exception:
                    pass

            await page.wait_for_timeout(500)
            
            # 5c. Pre-Flight Zero-Token Verification: "Have I answered all questions on this page?"
            # Sweeps the live DOM to ensure no radio group, select dropdown, or required field was left unselected.
            unanswered_resolved = await verify_and_fill_unanswered_fields(
                page, fields, answers, session_memory, persona=run_persona, user_email=resolved_email
            )
            if unanswered_resolved > 0:
                print(f"✅ Pre-Flight Verification: Automatically resolved {unanswered_resolved} unselected field(s) before advancing.")
                filled_count += unanswered_resolved
            
            # 5d. Synchronize session_memory directly from the live DOM state right before clicking Next/Submit!
            # Eliminates ANY discrepancy between what was clicked on the screen and what is printed in the summary!
            await sync_live_dom_to_session_memory(page, session_memory)
            
            # 6. Navigate (Find and Click Next / Continue / Submit)
            print("⏭️ Searching for Next/Submit button...")
            try:
                target_btn = await find_advance_button(page)
                if target_btn and await target_btn.is_visible():
                    if config.DRY_RUN:
                        print("🛑 DRY_RUN enabled. Stopping before clicking Next/Submit.")
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

                        await target_btn.scroll_into_view_if_needed()
                        if dynamic_bridge and upstream_target and dynamic_bridge.upstream_proxy is None:
                            print(f"🔄 Two-Stage Proxy: Activating US Residential IP ({upstream_target}) for survey questions...")
                            dynamic_bridge.set_upstream(upstream_target)
                            await page.wait_for_timeout(1000)
                        await human_click_element(page, target_btn)
                        
                        print("⏳ Waiting for page transition...")
                        for _ in range(20):
                            await page.wait_for_timeout(500)
                            try:
                                if page.url != prev_url:
                                    print("✅ URL navigation detected!")
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
                                curr_html = await page.evaluate("document.body ? document.body.innerHTML : ''")
                                if curr_html != prev_html:
                                    print("✅ DOM transition detected!")
                                    break
                            except Exception:
                                pass
                                
                        await page.wait_for_timeout(2500)
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
                                page_num += 1
                                continue
                            else:
                                break
                    except Exception:
                        pass
            except Exception as e:
                print(f"❌ Navigation failed: {e}")
                break
            page_num += 1
            
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

        # Final Summary & Proof Screenshot
        print("\n==================================")
        print("✅ QUESTIONNAIRE COMPLETED")
        print("==================================")
        print("Audit Trail (What was filled):")
        print(json.dumps(session_memory, indent=2))
        
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
        return {
            "summary": session_memory,
            "exit_reason": exit_reason or "Completed",
            "screenshot_path": screenshot_path,
            "reward": reward_info,
            "final_url": final_page_url,
            "final_page": page_num
        }

if __name__ == "__main__":
    asyncio.run(run_bot())
