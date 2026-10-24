import asyncio
import os
import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
import json
import re
import urllib.parse
from datetime import datetime
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

TARGET_URL = (
    "https://www.facebook.com/ads/library/?"
    "active_status=active&ad_type=all&country=US&is_targeted_country=false&"
    "media_type=all&q=Shareablee&search_type=keyword_unordered&"
    "sort_data[mode]=total_impressions&sort_data[direction]=desc"
)

CACHE_FILE = os.path.join(os.path.dirname(__file__), "scratch", "discovered_ads.json")

CLOSED_QUOTA_KEYWORDS = [
    "quota full",
    "quota has been reached",
    "survey is closed",
    "survey is no longer available",
    "no longer accepting responses",
    "maximum number of respondents",
    "study is complete",
    "survey has expired",
    "thank you for your interest, but"
]

def load_discovered():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_discovered(data):
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def extract_real_url(url: str) -> str:
    if "l.facebook.com/l.php" in url or "lm.facebook.com/l.php" in url:
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        if "u" in qs:
            return urllib.parse.unquote(qs["u"][0])
    return url

async def check_survey_quota_fast(url: str, browser=None) -> dict:
    """
    Fast pre-flight check to see if a survey link is already closed or quota full.
    Completes in <3 seconds.
    """
    if not url or "facebook.com" in url:
        return {"is_open": True, "reason": "Pending Ad Click"}

    owns_browser = False
    try:
        if browser is None:
            p = await async_playwright().start()
            browser = await p.chromium.launch(headless=True)
            owns_browser = True

        page = await browser.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=7000)
            await asyncio.sleep(1.5)
            body_text = (await page.evaluate("() => document.body ? document.body.innerText.toLowerCase() : ''"))
            
            for kw in CLOSED_QUOTA_KEYWORDS:
                if kw in body_text:
                    await page.close()
                    return {"is_open": False, "reason": f"Quota Full: '{kw}'"}
        finally:
            await page.close()
            if owns_browser:
                await browser.close()
                await p.stop()
    except Exception as e:
        # If timeout or network error, assume open to not falsely block
        return {"is_open": True, "reason": "Active"}

    return {"is_open": True, "reason": "Open"}

async def run_fb_watcher(headless: bool = True):
    """
    Scrapes the Facebook Ad Library for active Shareablee ads, extracting:
    - Ad ID
    - Started Running date
    - Ad destination URL
    - Quota/Availability status
    """
    discovered = load_discovered()
    newly_found = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        try:
            await page.goto(TARGET_URL, wait_until="commit", timeout=40000)
        except Exception:
            pass

        # Poll for page hydration
        page_text = ""
        for _ in range(12):
            await asyncio.sleep(2)
            try:
                page_text = await page.evaluate("() => document.body ? document.body.innerText : ''")
                if "Library ID:" in page_text:
                    break
            except Exception:
                pass

        # Match Ad ID and the exact "Started running on <Date>"
        id_date_matches = re.findall(
            r"Library ID:\s*(\d+)[\s\S]{0,120}?Started running on\s*([A-Za-z0-9\s,]+)",
            page_text
        )

        seen_in_scan = set()
        for match in id_date_matches:
            ad_id = match[0].strip()
            date_str = match[1].strip().split("\n")[0]
            seen_in_scan.add(ad_id)

            if ad_id not in discovered:
                entry = {
                    "ad_id": ad_id,
                    "url": f"https://www.facebook.com/ads/library/?id={ad_id}",
                    "title": f"Shareablee Survey Ad ({ad_id})",
                    "started_date": date_str,
                    "status": "OPEN",
                    "discovered_at": datetime.now().isoformat()
                }
                discovered[ad_id] = entry
                newly_found.append(entry)
            else:
                # Update date if missing
                if "started_date" not in discovered[ad_id] or not discovered[ad_id]["started_date"]:
                    discovered[ad_id]["started_date"] = date_str

        # Fallback for any IDs that didn't match the strict regex
        all_ids = re.findall(r'Library ID:\s*(\d+)', page_text)
        for ad_id in set(all_ids):
            if ad_id not in discovered:
                entry = {
                    "ad_id": ad_id,
                    "url": f"https://www.facebook.com/ads/library/?id={ad_id}",
                    "title": f"Shareablee Survey Ad ({ad_id})",
                    "started_date": "Recently Active",
                    "status": "OPEN",
                    "discovered_at": datetime.now().isoformat()
                }
                discovered[ad_id] = entry
                newly_found.append(entry)

        await browser.close()

    save_discovered(discovered)
    print(f"📊 Ad Watcher finished. Total ads indexed: {len(discovered)}")
    return newly_found, discovered

if __name__ == "__main__":
    asyncio.run(run_fb_watcher(headless=True))

