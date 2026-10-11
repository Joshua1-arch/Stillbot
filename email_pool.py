import os
import json

POOL_FILE = "emails_pool.json"

def load_pool():
    if not os.path.exists(POOL_FILE):
        return []
    try:
        with open(POOL_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def save_pool(pool):
    with open(POOL_FILE, "w", encoding="utf-8") as f:
        json.dump(pool, f, indent=2)

def add_emails_to_pool(email_list):
    pool = load_pool()
    existing = {item["email"].lower(): item for item in pool}
    added_count = 0
    for e in email_list:
        clean = e.strip().lower()
        if clean and "@" in clean and clean not in existing:
            new_entry = {"email": e.strip(), "used_count": 0, "last_used": None}
            pool.append(new_entry)
            existing[clean] = new_entry
            added_count += 1
    save_pool(pool)
    return added_count, len(pool)

def mark_email_used(email):
    pool = load_pool()
    clean = email.strip().lower()
    for item in pool:
        if item["email"].lower() == clean:
            item["used_count"] = item.get("used_count", 0) + 1
            import datetime
            item["last_used"] = datetime.datetime.now().isoformat()
            break
    save_pool(pool)

def get_pool_summary():
    pool = load_pool()
    total = len(pool)
    fresh = sum(1 for e in pool if e.get("used_count", 0) == 0)
    return total, fresh
