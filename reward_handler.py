import asyncio
import os
import re
from playwright.async_api import Page

REWARD_KEYWORDS = [
    "gift card", "electronic gift card", "reward", "incentive",
    "compensation", "raffle", "drawing", "sweepstakes", "honorarium",
    "payment", "voucher", "token of appreciation", "amazon", "tango",
    "virtual visa", "virtual mastercard", "provide your email",
    "enter to win", "receive a $", "$5", "$10", "$15", "$20", "$25", "$50"
]

EXCLUDE_DOMAINS_OR_PATHS = [
    "privacy", "terms", "support", "qualtrics.com/privacy",
    "psychologytoday.com", "988lifeline.org", "nami.org",
    "facebook.com/policies", "twitter.com", "instagram.com",
    "linkedin.com", "tel:"
]

async def find_reward_links(page: Page):
    """
    Scans the current page DOM for any incentive/reward/gift card links or buttons.
    Returns a list of dicts with url, anchorText, surroundingText.
    """
    js_finder = """
    () => {
        const rewardKeywords = [
            "gift card", "electronic gift card", "reward", "incentive",
            "compensation", "raffle", "drawing", "sweepstakes", "honorarium",
            "payment", "voucher", "token of appreciation", "amazon", "tango",
            "virtual visa", "virtual mastercard", "provide your email",
            "enter to win", "receive a $", "$5", "$10", "$15", "$20", "$25", "$50"
        ];
        
        const excludeList = [
            "privacy", "terms", "support", "qualtrics.com/privacy",
            "psychologytoday.com", "988lifeline.org", "nami.org",
            "facebook.com/policies", "twitter.com", "instagram.com",
            "linkedin.com", "tel:"
        ];
        
        const candidates = [];
        const seenUrls = new Set();
        
        // 1. Scan anchor tags
        const anchors = Array.from(document.querySelectorAll('a[href]'));
        for (const a of anchors) {
            const href = (a.getAttribute('href') || '').trim();
            if (!href || href.startsWith('#') || href.startsWith('javascript:')) continue;
            
            const fullUrl = a.href;
            const fullUrlLower = fullUrl.toLowerCase();
            
            // Exclude non-reward links
            if (excludeList.some(ex => fullUrlLower.includes(ex))) continue;
            if (seenUrls.has(fullUrl)) continue;
            
            const anchorText = (a.innerText || a.textContent || '').trim();
            const anchorLower = anchorText.toLowerCase();
            
            // Get surrounding container text
            const container = a.closest('p, li, div, td') || a.parentElement;
            const surroundingText = (container ? container.innerText : anchorText).trim();
            const surroundingLower = surroundingText.toLowerCase();
            
            const matchesReward = rewardKeywords.some(kw => 
                anchorLower.includes(kw) || surroundingLower.includes(kw)
            );
            
            if (matchesReward) {
                seenUrls.add(fullUrl);
                candidates.push({
                    type: "anchor",
                    url: fullUrl,
                    anchorText: anchorText,
                    surroundingText: surroundingText.replace(/\\s+/g, ' ').slice(0, 300)
                });
            }
        }
        
        // 2. Scan buttons that might be styled reward actions
        const buttons = Array.from(document.querySelectorAll('button, input[type="button"], [role="button"]'));
        for (const btn of buttons) {
            const text = (btn.innerText || btn.getAttribute('value') || btn.getAttribute('aria-label') || '').trim();
            const textLower = text.toLowerCase();
            if (rewardKeywords.some(kw => textLower.includes(kw))) {
                candidates.push({
                    type: "button",
                    url: "",
                    anchorText: text,
                    surroundingText: text
                });
            }
        }
        
        return candidates;
    }
    """
    try:
        results = await page.evaluate(js_finder)
        return results or []
    except Exception as e:
        print(f"⚠️ Error scanning for reward links: {e}")
        return []

async def fill_reward_page(reward_page: Page, user_email: str):
    """
    Fills out the opened reward page (email, confirm email, name) and submits it.
    """
    print(f"🎁 Auto-filling reward form with email: {user_email}")
    await reward_page.wait_for_timeout(3000)
    
    # 1. Fill email inputs
    js_fill = f"""
    () => {{
        let filledCount = 0;
        const inputs = Array.from(document.querySelectorAll('input:not([type="hidden"]):not([type="submit"]):not([type="button"]), textarea'));
        
        for (const input of inputs) {{
            const type = (input.type || '').toLowerCase();
            const name = (input.name || '').toLowerCase();
            const id = (input.id || '').toLowerCase();
            const placeholder = (input.placeholder || '').toLowerCase();
            const ariaLabel = (input.getAttribute('aria-label') || '').toLowerCase();
            
            // Check if label references email
            let labelText = '';
            if (input.id) {{
                const lbl = document.querySelector(`label[for="${{input.id}}"]`);
                if (lbl) labelText = lbl.innerText.toLowerCase();
            }}
            if (!labelText && input.closest('label')) {{
                labelText = input.closest('label').innerText.toLowerCase();
            }}
            
            const combined = `${{type}} ${{name}} ${{id}} ${{placeholder}} ${{ariaLabel}} ${{labelText}}`;
            
            if (combined.includes('email') || type === 'email') {{
                input.focus();
                input.value = "{user_email}";
                input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                filledCount++;
            }}
        }}
        return filledCount;
    }}
    """
    try:
        filled_count = await reward_page.evaluate(js_fill)
        print(f"✍️ Filled {filled_count} email field(s) on reward page.")
    except Exception as e:
        print(f"⚠️ Error filling email on reward page: {e}")

    await reward_page.wait_for_timeout(1000)

    # 2. Find and click Submit / Next on the reward page
    js_submit = """
    () => {
        const buttons = Array.from(document.querySelectorAll('button, input[type="submit"], input[type="button"], [role="button"], #NextButton, .NextButton'));
        for (const btn of buttons) {
            const text = (btn.innerText || btn.value || btn.getAttribute('aria-label') || '').trim().toLowerCase();
            if (text.includes('submit') || text.includes('finish') || text.includes('done') || 
                text.includes('next') || text.includes('>>') || text.includes('continue') ||
                btn.id.toLowerCase().includes('next') || btn.className.toLowerCase().includes('next')) {
                if (btn.offsetWidth > 0 || btn.offsetHeight > 0) {
                    btn.click();
                    return true;
                }
            }
        }
        return false;
    }
    """
    submitted = False
    try:
        submitted = await reward_page.evaluate(js_submit)
        if submitted:
            print("🚀 Clicked submit button on reward form.")
        else:
            # Fallback press Enter
            await reward_page.keyboard.press("Enter")
            print("⌨️ Pressed Enter on reward form.")
    except Exception as e:
        print(f"⚠️ Error submitting reward form: {e}")

    await reward_page.wait_for_timeout(4000)
    return True

async def check_and_claim_reward(page: Page, status_callback=None, user_email: str = None, run_index: int = 1):
    """
    Checks if the survey end page contains a reward/gift card link.
    If detected:
    1. Sends reward link & details to Telegram immediately.
    2. Clicks and opens the reward link.
    3. Fills in user_email and submits the form.
    4. Takes a screenshot proof of submission.
    5. Sends confirmation to Telegram.
    """
    candidates = await find_reward_links(page)
    if not candidates:
        return None

    primary = candidates[0]
    reward_url = primary.get("url")
    snippet = primary.get("surroundingText") or primary.get("anchorText")
    
    print(f"\n🎁 [REWARD DETECTED] Found reward/incentive link!")
    print(f"• URL: {reward_url}")
    print(f"• Context: {snippet}")

    # 1. Alert Telegram immediately
    if status_callback:
        try:
            msg = (
                f"🎁 **Reward / Gift Card Link Detected!**\n"
                f"• **Incentive:** {snippet}\n"
                f"• **Reward Link:** {reward_url}\n\n"
                f"⚡ Auto-filling reward form with `{user_email or 'configured email'}`..."
            )
            await status_callback(msg)
        except Exception as e:
            print(f"⚠️ Telegram alert error: {e}")

    # 2. Click / Open the reward link
    reward_page = None
    try:
        # Check if clicking spawns a new popup tab
        context = page.context
        async with context.expect_page(timeout=6000) as new_page_info:
            try:
                # Try clicking the anchor tag
                await page.locator(f'a[href="{reward_url}"]').first.click(timeout=3000)
            except Exception:
                # Try clicking by anchor text
                anchor_text = primary.get("anchorText") or "Click here"
                await page.get_by_role("link", name=anchor_text).first.click(timeout=3000)
        reward_page = await new_page_info.value
        await reward_page.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception:
        # If no popup or click failed, open reward_url directly in current or new page
        print("ℹ️ Opening reward URL directly...")
        try:
            reward_page = await page.context.new_page()
            await reward_page.goto(reward_url, timeout=25000, wait_until="domcontentloaded")
        except Exception as e:
            print(f"⚠️ Failed to navigate to reward URL: {e}")
            if reward_url:
                return {"reward_url": reward_url, "status": "Link detected but navigation failed"}

    # 3. Fill and submit reward form
    if reward_page and user_email:
        await fill_reward_page(reward_page, user_email)
        
        # 4. Screenshot proof
        os.makedirs("artifacts", exist_ok=True)
        proof_path = f"artifacts/reward_submitted_run_{run_index}.png"
        try:
            await reward_page.screenshot(path=proof_path, full_page=False, timeout=5000)
            print(f"📸 Saved reward submission screenshot to {proof_path}")
        except Exception:
            proof_path = None

        # 5. Notify Telegram of completion
        if status_callback:
            try:
                conf_msg = (
                    f"✅ **Reward Claim Submitted!**\n"
                    f"• **Claimed for:** `{user_email}`\n"
                    f"• **Link:** {reward_url}\n"
                    f"• Proof saved to screenshots."
                )
                await status_callback(conf_msg)
            except Exception:
                pass
                
        try:
            await reward_page.close()
        except Exception:
            pass

        return {
            "reward_url": reward_url,
            "status": "Claimed & Submitted",
            "proof_path": proof_path
        }

    return {"reward_url": reward_url, "status": "Link detected (No email provided)"}
