import asyncio
import sys
import json
import os
import urllib.parse
sys.stdout.reconfigure(encoding='utf-8')
from playwright.async_api import async_playwright
import config
from scraper import scrape_page, prepare_vision_fallback
from ai_engine import get_form_answers, get_vision_form_answers

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
            
        if role == "radio":
            val_clean = str(val).strip()
            # If answer is boolean False, 'false', 'no', 'none', 'skip', or empty, DO NOT click or select this radio!
            if val is False or val_clean.lower() in ["false", "no", "skip", "none", "0", ""]:
                print(f"⏭️ Skipping radio '{field.get('label')}' (val is {val})")
                return

            option_map = field.get("option_map", {})
            clicked = False
            target_id = None
            
            if option_map:
                if val_clean in option_map:
                    target_id = option_map[val_clean]
                else:
                    val_lower = val_clean.lower()
                    for opt_text, opt_id in option_map.items():
                        if opt_text.lower() == val_lower or val_lower in opt_text.lower() or opt_text.lower() in val_lower:
                            target_id = opt_id
                            break
                            
            if target_id:
                try:
                    lbl = page.locator(f'label[for="{target_id}"], label:has([data-stillbot-id="{target_id}"])').first
                    if await lbl.count() > 0 and await lbl.is_visible():
                        await lbl.click(force=True, timeout=2000)
                        clicked = True
                    if not clicked:
                        # Fallback: direct DOM element click and event dispatch
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
                except Exception as e:
                    print(f"⚠️ Radio target click error: {e}")
                    
            if not clicked:
                try:
                    await page.get_by_role("radio", name=val_clean, exact=False).first.click(force=True, timeout=2000)
                    clicked = True
                except Exception:
                    pass
                    
            if not clicked and locator:
                try:
                    await locator.click(force=True, timeout=2000)
                    clicked = True
                except Exception:
                    pass
                    
            if clicked:
                print(f"🎯 Selected radio option: '{val_clean}'")

        elif role == "checkbox":
            if isinstance(val, list):
                for v in val:
                    try:
                        await page.get_by_role("checkbox", name=str(v), exact=False).first.click(force=True, timeout=2000)
                    except Exception:
                        pass
            else:
                val_str = str(val).strip().lower()
                if not val_str or val_str in ["no", "false", "0", "skip", "none", "n/a"]:
                    pass
                else:
                    clicked = False
                    f_id = field.get("id", "")
                    try:
                        lbl_loc = page.locator(f'label[for="{f_id}"], label:has([data-stillbot-id="{f_id}"])').first
                        if await lbl_loc.count() > 0 and await lbl_loc.is_visible():
                            await lbl_loc.click(force=True, timeout=2000)
                            clicked = True
                    except Exception:
                        pass
                    if not clicked and locator:
                        try:
                            await locator.click(force=True, timeout=2000)
                        except Exception:
                            try:
                                await locator.check(force=True, timeout=2000)
                            except Exception:
                                pass
                    
        elif role in ["combobox", "listbox"]:
            val_clean = str(val).strip()
            
            # Check if this is a standard native <select>
            try:
                tag = await locator.evaluate("el => el ? el.tagName.toLowerCase() : ''")
            except Exception:
                tag = ""
            if tag == "select":
                # Find matching option text & value
                opt_info = await locator.evaluate(f"""
                (el) => {{
                    if (!el || !el.options) return null;
                    const target = "{val_clean}".toLowerCase();
                    for (let opt of el.options) {{
                        const oText = (opt.text || "").toLowerCase();
                        const oVal = (opt.value || "").toLowerCase();
                        if (oText === target || oVal === target || oText.includes(target) || oVal.includes(target)) {{
                            return {{ text: opt.text.trim(), value: opt.value }};
                        }}
                    }}
                    return null;
                }}
                """)
                
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
                            
                    # 2. Universal focus + keyboard type (triggers reactive frameworks like React/Vue/Angular)
                    try:
                        await locator.focus()
                        await page.keyboard.type(target_text)
                        await page.keyboard.press("Enter")
                    except Exception:
                        pass
                        
                    # 3. Native event dispatch and React valueTracker reset
                    await locator.evaluate(f"""
                    (el) => {{
                        el.value = "{target_val}";
                        if (el._valueTracker) el._valueTracker.setValue('');
                        el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        el.dispatchEvent(new Event('blur', {{ bubbles: true }}));
                        
                        const pKey = Object.keys(el).find(k => k.startsWith('__reactProps'));
                        if (pKey && el[pKey] && typeof el[pKey].onChange === 'function') {{
                            try {{
                                el[pKey].onChange({{
                                    target: el,
                                    currentTarget: el,
                                    bubbles: true
                                }});
                            }} catch (e) {{}}
                        }}
                    }}
                    """)
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

async def find_advance_button(page):
    """
    Universally finds the Next, Continue, Start, or Submit button on any survey platform.
    Excludes negative buttons like Back, Previous, Cancel, Clear.
    """
    candidate_selectors = [
        '.cf-navigation-next', '.cf-navigation__button', 'button.cf-navigation-next',
        'button[type="submit"]',
        'input[type="submit"]',
        '#next-button', '#NextButton', 'button#next-button', 'button#NextButton', 'input#NextButton',
        '#submit-button', '#SubmitButton',
        '.next-button', '.NextButton', '.navigation-button', '.btn-next', '.btn-submit',
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
    return None

async def check_is_completed(page, has_answered: bool = False, has_advance_btn: bool = False):
    """
    Universally checks if a survey or questionnaire has been completed, screened out, or closed.
    Never completes on an intro page that still has a visible Next/Start button.
    """
    url = page.url.lower()
    completion_url_keywords = [
        "/thanks", "/thank-you", "/complete", "/finish", "/submitted",
        "/formresponse", "survey-thanks", "/done", "response_received",
        "/disqualified", "/screenout", "/quota-full"
    ]
    if any(k in url for k in completion_url_keywords):
        return True, "Completion URL detected"
        
    # If there is a visible Next / Start / Submit button, we are NOT finished
    if has_advance_btn:
        return False, ""
        
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
                'Please enter a valid'
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

async def run_bot(target_url: str = None, instructions: str = None, proxy: str = None, run_index: int = 1, status_callback = None):
    print("🚀 Booting Universal Questionnaire Bot...")
    if status_callback:
        try:
            await status_callback(f"🚀 **Run {run_index} Started!**\n• Initializing stealth browser environment...")
        except Exception:
            pass
    
    # Apply runtime overrides if provided per run instance
    active_url = target_url or config.TARGET_URL
    if "l.facebook.com" in active_url:
        import urllib.parse
        parsed = urllib.parse.urlparse(active_url)
        params = urllib.parse.parse_qs(parsed.query)
        if "u" in params:
            active_url = urllib.parse.unquote(params["u"][0])
            print(f"🔗 Unwrapped direct survey URL: {active_url}")
            
    run_persona = config.USER_PERSONA
    if instructions:
        run_persona = f"{config.USER_PERSONA}\n\n[CRITICAL OVERRIDES FOR THIS RUN]\n{instructions}\nYou MUST prioritize these overrides over your default persona settings."
    
    exit_reason = ""
    async with async_playwright() as p:
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
                "--window-size=1280,800"
            ]
        }
        if not is_headless:
            # Place window offscreen/minimized so it doesn't interrupt the user's active desktop
            launch_args["args"].extend(["--window-position=2000,2000", "--start-minimized"])
        
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
            
        # Determine whether to use Google Chrome channel or bundled Chromium
        chrome_channel = "chrome" if os.name == "nt" else None
        
        try:
            kwargs = dict(launch_args)
            if chrome_channel:
                kwargs["channel"] = chrome_channel
            browser = await p.chromium.launch_persistent_context(
                user_data_dir=temp_user_data_dir,
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800},
                locale="en-US",
                **kwargs
            )
            print("🌐 Launched Chromium with human hardware profile & fresh isolated session.")
        except Exception as e:
            print(f"ℹ️ Persistent launch fallback ({e}). Using standard launch.")
            try:
                b_instance = await p.chromium.launch(**launch_args)
                browser = await b_instance.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                    viewport={"width": 1280, "height": 800},
                    locale="en-US"
                )
            except Exception as e2:
                print(f"❌ Chromium launch error: {e2}")
                raise
        
        context = browser
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)
        
        try:
            from playwright_stealth import stealth_async
        except ImportError:
            stealth_async = None
            
        page = context.pages[0] if context.pages else await context.new_page()
        if stealth_async:
            await stealth_async(page)
            print("🥷 Playwright Stealth activated.")
            
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
        for _ in range(30):
            await page.wait_for_timeout(1000)
            try:
                ready_state = await page.evaluate("document.readyState")
                is_verisoul_gatekeeper = await page.evaluate("""() => {
                    const hasNextBtn = !!document.querySelector('.cf-navigation-next, button[type="submit"], input[type="submit"], #forward, #next, .btn-next');
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

                interactive_count = await page.evaluate("""() => {
                    return document.querySelectorAll('input, select, textarea, button, [role="button"], a.btn').length;
                }""")
                b_text = await page.evaluate("document.body ? document.body.innerText.trim() : ''")
                if ready_state == "complete" and (interactive_count > 0 or len(b_text) > 40):
                    print(f"✅ Page stabilized: {page.url} ({interactive_count} interactive controls, {len(b_text)} text chars)")
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
                        print(f"🛡️ Anti-bot curtain active ({curtain_wait_count}/10). Waiting for Verisoul verification & auto-advance...")
                        await page.wait_for_timeout(2000)
                        continue
                    else:
                        print("⚠️ Anti-bot curtain wait reached limit (20s). Proceeding to survey inspection...")
                        
                # Check completion again before attempting vision
                completed, reason = await check_is_completed(page, has_answered=(len(session_memory) > 0), has_advance_btn=False)
                if completed:
                    print(f"🏁 {reason}! Questionnaire is complete!")
                    exit_reason = reason
                    break
                    
                # Tier 2 Vision Fallback
                num_boxes = await prepare_vision_fallback(page)
                if num_boxes == 0:
                    print("🏁 No fields found and no vision targets. Survey is complete or terminated.")
                    exit_reason = "No fields or advance button found (Survey closed or terminated by host)"
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
                    
                if consecutive_stuck_attempts >= max_stuck_attempts:
                    print("🛑 Maximum retries reached on this page. Stopping.")
                    try:
                        await page.screenshot(path=f"artifacts/stuck_page_{page_num}.png", full_page=True)
                        print(f"📸 Saved screenshot of stuck page to artifacts/stuck_page_{page_num}.png")
                    except Exception:
                        pass
                    break
            else:
                consecutive_stuck_attempts = 0
                current_validation_errors = []
                previous_fields_hash = current_hash
                
            # 4. Think (LLM with error correction awareness)
            answers = await get_form_answers(fields, session_memory, validation_errors=current_validation_errors, persona=run_persona)
            if not answers:
                print("🏁 LLM returned empty answers. Assuming completion.")
                break
                
            # 5. Act (Universally Fill Fields)
            filled_count = 0
            for f in fields:
                f_id = f["id"]
                if f_id in answers:
                    await fill_field(page, f, answers[f_id])
                    session_memory[f.get("label", "Unknown")] = answers[f_id]["value"]
                    filled_count += 1
                    
            if status_callback:
                try:
                    await status_callback(f"⏳ **Page {page_num} Analyzed & Answered**\n• Answered {filled_count} field(s)\n• Advancing to next page...")
                except Exception:
                    pass

            await page.wait_for_timeout(1000)
            
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
                        
                        try:
                            prev_url = page.url
                            prev_html = await page.evaluate("document.body ? document.body.innerHTML : ''")
                        except Exception:
                            prev_url = ""
                            prev_html = ""
                            
                        await target_btn.scroll_into_view_if_needed()
                        if dynamic_bridge and upstream_target and dynamic_bridge.upstream_proxy is None:
                            print(f"🔄 Two-Stage Proxy: Activating US Residential IP ({upstream_target}) for survey questions...")
                            dynamic_bridge.set_upstream(upstream_target)
                            await page.wait_for_timeout(1000)
                        await target_btn.click(force=True)
                        
                        print("⏳ Waiting for page transition...")
                        for _ in range(20):
                            await page.wait_for_timeout(500)
                            try:
                                if page.url != prev_url:
                                    print("✅ URL navigation detected!")
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
                    await page.wait_for_timeout(4000)
            except Exception as e:
                print(f"❌ Navigation failed: {e}")
                break
            page_num += 1
            
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
            "screenshot_path": screenshot_path
        }

if __name__ == "__main__":
    asyncio.run(run_bot())
