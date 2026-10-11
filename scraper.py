import asyncio
import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
from playwright.async_api import Page, Frame
import json
import os
import config

async def scrape_frame(frame: Frame, frame_index: int = 0):
    """
    Tier 1 Semantic Scraper.
    Extracts semantic information about interactive elements without mutating the DOM.
    Returns data needed for Playwright's get_by_role locators.
    """
    js_script = r"""
    () => {
        const results = [];
        const radioGroups = {};
        // Look for standard inputs, contenteditables, and common ARIA roles across all web platforms
        const selectors = 'input, select, textarea, [contenteditable="true"], [role="textbox"], [role="radio"], [role="checkbox"], [role="listbox"], [role="combobox"]';
        const elements = document.querySelectorAll(selectors);
        
        elements.forEach((el, index) => {
            // Skip invisible elements and hidden mobile clones (e.g. Confirmit mobile_QFAM controls)
            const isHidden = (el.offsetWidth === 0 && el.offsetHeight === 0) || window.getComputedStyle(el).display === 'none' || window.getComputedStyle(el).visibility === 'hidden';
            if (isHidden && !el.closest('label')) return;
            if (el.id && el.id.toLowerCase().startsWith('mobile_')) return;
            if (el.type === 'hidden' || el.type === 'submit' || el.type === 'button' || el.type === 'reset') return;
            
            // Skip security tokens, CSRF, and anti-bot verification inputs (e.g. Verisoul, Recaptcha, Turnstile)
            const elId = (el.id || '').toLowerCase();
            const elName = (el.name || '').toLowerCase();
            const secTokens = ['sessionid', 'verisoul', 'recaptcha', 'hcaptcha', 'turnstile', 'csrf', '_token', '__viewstate', 'fingerprint'];
            if (secTokens.some(k => elId.includes(k) || elName.includes(k))) return;
            
            // 1. Accessible Name Extraction
            let name = el.getAttribute('aria-label') || '';
            
            if (!name && el.getAttribute('aria-labelledby')) {
                const idList = el.getAttribute('aria-labelledby').split(/\s+/);
                const parts = idList.map(i => {
                    const found = document.getElementById(i);
                    return found ? found.innerText.trim() : '';
                }).filter(Boolean);
                if (parts.length > 0) name = parts.join(' ');
            }
            
            if (!name && el.id) {
                try {
                    const labelEl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
                    if (labelEl) name = labelEl.innerText;
                } catch(e) {
                    try {
                        const labelEl = document.querySelector(`label[for="${el.id}"]`);
                        if (labelEl) name = labelEl.innerText;
                    } catch(e2) {}
                }
            }
            if (!name && el.closest('label')) {
                name = el.closest('label').innerText;
            }
            if (!name) {
                // Check previous element sibling or label in immediate parent
                const prev = el.previousElementSibling;
                if (prev && (prev.tagName.toLowerCase() === 'label' || prev.innerText)) {
                    const pTxt = prev.innerText.trim();
                    if (pTxt && pTxt.length < 60) name = pTxt;
                } else if (el.parentElement) {
                    const parentText = Array.from(el.parentElement.childNodes)
                        .filter(n => n.nodeType === 3)
                        .map(n => n.textContent.trim())
                        .join(' ');
                    if (parentText && parentText.length < 60) name = parentText;
                }
            }
            if (!name && el.hasAttribute('placeholder')) {
                name = el.getAttribute('placeholder');
            }
            if (!name && el.hasAttribute('title')) {
                name = el.getAttribute('title');
            }
            // For custom ARIA widgets with direct text (exclude native <select> so we don't get option lists)
            const tag = el.tagName.toLowerCase();
            if (!name && tag !== 'select' && el.innerText && el.innerText.trim().length > 0) {
                name = el.innerText.substring(0, 100);
            }
            if (!name) {
                name = el.name || el.id || '';
            }
            name = name.trim();
            
            // Determine raw option name before adding question context
            const rawOptionName = (name || '').trim();
            
            // 2. Advanced Universal Context / Question Title Extraction
            let contextHeading = "";
            let container = el.closest('fieldset, .cf-question, .question, .question-container, .form-group, [role="radiogroup"], [role="group"], .geS5n, [jsmodel], form > div, .QuestionOuter, .survey-question, div[data-testid*="question"], .freebirdFormviewerViewItemsItemItem, .form-line');
            if (!container) {
                container = el.closest('div[class*="question" i]:not([class*="answer" i]):not([class*="choice" i])');
            }
            if (container && (container.className || '').includes('answer')) {
                container = container.closest('.cf-question, .question, fieldset') || container;
            }
            if (container) {
                let headingEl = container.querySelector('legend, h1, h2, h3, h4, h5, h6, [role="heading"], .title, .question-title, .question-title-container, .QuestionText, .exportItemTitle, [data-testid*="title"], [data-testid*="header"], .cf-question__text, .cf-question__title, [class*="question-text" i], [class*="question__text" i]');
                if (headingEl) {
                    contextHeading = headingEl.innerText.trim();
                } else {
                    let firstLine = container.innerText.split('\n')[0].trim();
                    if (firstLine.length > 3 && firstLine.length < 150) contextHeading = firstLine;
                }
            }
            if (!contextHeading) {
                const globalHeading = document.querySelector('.cf-question__text, .cf-question__title, [class*="question__text" i], [class*="question-text" i], .QuestionText, h1, h2, h3');
                if (globalHeading) contextHeading = globalHeading.innerText.trim();
            }
            
            // 3. Determine Role
            let role = el.getAttribute('role');
            if (!role) {
                if (tag === 'input') {
                    role = el.type === 'checkbox' ? 'checkbox' : (el.type === 'radio' ? 'radio' : 'textbox');
                } else if (tag === 'select') {
                    role = 'combobox';
                } else if (tag === 'textarea' || el.isContentEditable) {
                    role = 'textbox';
                }
            }
            
            // Inject stable ID for locator targeting
            const injectedId = `stillbot_f${__FRAME_IDX__}_${index}`;
            el.setAttribute('data-stillbot-id', injectedId);
            
            // Handle Radio Buttons: Aggregate into single question field with options
            if (role === 'radio') {
                const tr = el.closest('tr, [role="row"]');
                const table = el.closest('table, [role="grid"], .cf-grid, .cf-grid-table');
                let rowLabel = "";
                let colLabel = "";
                
                if (tr && table) {
                    const rowLabelEl = tr.querySelector('.cf-grid-row__label, .cf-label, th, td:first-child');
                    if (rowLabelEl && rowLabelEl !== el.closest('td')) {
                        rowLabel = rowLabelEl.innerText.trim();
                    }
                    
                    const td = el.closest('td, [role="gridcell"]');
                    if (td) {
                        const colIdx = Array.from(tr.children).indexOf(td);
                        const theadThs = table.querySelectorAll('thead th, thead td, tr:first-child th, tr:first-child td, .cf-grid-col__label');
                        if (theadThs[colIdx]) {
                            colLabel = theadThs[colIdx].innerText.trim();
                        }
                    }
                }

                const groupKey = el.name ? ('name_' + el.name) : (tr ? ('row_' + (tr.id || Array.from(document.querySelectorAll('*')).indexOf(tr))) : (container ? ('cont_' + (container.id || Array.from(document.querySelectorAll('*')).indexOf(container))) : ('single_' + index)));
                const optText = colLabel || rawOptionName || el.value || `Option ${index}`;
                const qLabel = rowLabel ? (contextHeading ? `${contextHeading} - ${rowLabel}` : rowLabel) : (contextHeading || rawOptionName || 'Select an option');
                
                if (!radioGroups[groupKey]) {
                    radioGroups[groupKey] = {
                        id: injectedId,
                        role: 'radio',
                        label: qLabel,
                        type: 'radio',
                        options: [],
                        option_map: {}
                    };
                }
                if (optText && !radioGroups[groupKey].options.includes(optText)) {
                    radioGroups[groupKey].options.push(optText);
                    radioGroups[groupKey].option_map[optText] = injectedId;
                }
                return;
            }
            
            // Clean up contextHeading for non-radio fields
            if (contextHeading) {
                if (!name || /^[\d_\[\]\s-]+$/.test(name) || name.toLowerCase().startsWith('unknown')) {
                    name = contextHeading;
                } else if (contextHeading !== name && !name.startsWith(contextHeading)) {
                    name = contextHeading + " - " + name;
                }
            }
            
            // 4. Options Extraction
            let options = [];
            if (tag === 'select') {
                options = Array.from(el.options).map(opt => (opt.text || opt.value || '').trim()).filter(Boolean);
            } else if (role === 'listbox' || role === 'combobox') {
                const opts = el.querySelectorAll('[role="option"], li');
                opts.forEach(opt => {
                    const optText = (opt.innerText || opt.getAttribute('value') || '').trim();
                    if (optText) options.push(optText);
                });
            }
            
            results.push({
                id: injectedId,
                role: role || 'textbox',
                label: name || `Unknown Field ${index}`,
                type: el.type || (tag === 'select' ? 'select' : 'text'),
                options: options
            });
        });
        
        // Append aggregated radio groups
        Object.values(radioGroups).forEach(group => {
            results.push(group);
        });
        return results;
    }
    """
    script_to_run = js_script.replace('__FRAME_IDX__', str(frame_index))
    try:
        fields = await frame.evaluate(script_to_run)
        return fields
    except Exception as e:
        print(f"Failed to scrape frame {frame_index}: {e}")
        return []

async def scrape_page(page: Page):
    """
    Recursively scrapes all frames on the page and returns all fields.
    """
    print("🕸️ Running Tier 1 Semantic Scraper...")
    all_fields = []
    
    # Wait for network idle to ensure React has finished rendering
    try:
        await page.wait_for_load_state("networkidle", timeout=3000)
    except:
        pass
        
    # Scrape main frame
    fields = await scrape_frame(page.main_frame, 0)
    all_fields.extend(fields)
    
    # Scrape child frames (iframes)
    for idx, frame in enumerate(page.main_frame.child_frames):
        try:
            fields = await scrape_frame(frame, idx + 1)
            all_fields.extend(fields)
        except Exception:
            pass
        
    print(f"✅ Found {len(all_fields)} fields.")
    return all_fields

async def prepare_vision_fallback(page: Page):
    """
    Tier 2 Vision Fallback.
    Triggered when DOM scraping fails. 
    Draws numbered bounding boxes around potential interactive elements and takes a screenshot.
    """
    if not config.ENABLE_VISION_FALLBACK:
        return 0
        
    print("⚠️ Tier 1 failed to find fields. Triggering Tier 2 Vision Fallback...")
    js_script = """
    () => {
        let counter = 1;
        const boxes = [];
        // Look for anything that resembles an input visually
        const candidates = document.querySelectorAll('div, span, p');
        
        candidates.forEach(el => {
            const style = window.getComputedStyle(el);
            // Heuristic: If it has a border, is empty/editable, and isn't tiny.
            const hasBorder = style.borderWidth && style.borderWidth !== '0px' && style.borderStyle !== 'none';
            if (el.isContentEditable || (hasBorder && el.innerText.length < 50 && el.childElementCount === 0 && el.offsetHeight > 15)) {
                if (el.offsetWidth === 0 || el.offsetHeight === 0) return;
                
                const rect = el.getBoundingClientRect();
                const overlay = document.createElement('div');
                overlay.style.position = 'absolute';
                overlay.style.top = `${rect.top + window.scrollY}px`;
                overlay.style.left = `${rect.left + window.scrollX}px`;
                overlay.style.width = `${rect.width}px`;
                overlay.style.height = `${rect.height}px`;
                overlay.style.border = '2px solid red';
                overlay.style.backgroundColor = 'rgba(255,0,0,0.2)';
                overlay.style.zIndex = '999999';
                overlay.style.pointerEvents = 'none';
                
                const label = document.createElement('div');
                label.style.position = 'absolute';
                label.style.top = '0';
                label.style.left = '0';
                label.style.backgroundColor = 'red';
                label.style.color = 'white';
                label.style.fontSize = '12px';
                label.style.padding = '2px';
                label.innerText = counter;
                
                overlay.appendChild(label);
                document.body.appendChild(overlay);
                counter++;
            }
        });
        return counter - 1;
    }
    """
    num_boxes = await page.evaluate(js_script)
    if num_boxes > 0:
        os.makedirs("artifacts", exist_ok=True)
        path = "artifacts/vision_fallback.png"
        await page.screenshot(path=path, full_page=True)
        print(f"📸 Vision Fallback: Detected {num_boxes} potential inputs. Screenshot saved to {path}")
    else:
        print("❌ Vision Fallback failed to find any inputs.")
    return num_boxes
