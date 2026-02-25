#!/usr/bin/env python3
import json, os, requests, pytz
from datetime import datetime, timedelta
from collections import defaultdict


BASE_URL = "https://district24.pages.dev/Daily%20Advance"
OUTPUT_DIR = "Chain Daily Advance"
os.makedirs(OUTPUT_DIR, exist_ok=True)

TARGET_CHAINS = ["PVR", "INOX", "CINEPOLIS"]

BLOCK_RATES = {
    "PVR": 0.005,
    "CINEPOLIS": 0.0325,
    "INOX": 0.0
}


def log(msg):
    print("➡", msg)


def detect_chain(venue):
    if not venue:
        return None
    venue = venue.upper()
    for chain in TARGET_CHAINS:
        if chain in venue:
            return chain
    return None


def apply_discount(chain, sold, gross, seats):
    rate = BLOCK_RATES.get(chain, 0)

    if sold > 0 and rate > 0:
        avg_price = gross / sold if sold else 0
        blocked = seats * rate
        adjusted = sold - blocked

        # 🔥 prevent negative values
        sold = max(0, round(adjusted))
        gross = max(0, sold * avg_price)

    return sold, gross


def fetch(date):
    url = f"{BASE_URL}/{date}_Detailed.json"
    try:
        r = requests.get(url, timeout=12)
        if r.status_code == 200:
            log(f"📥 Data received for {date}")
            return r.json()
    except:
        pass

    log(f"⚠ No data for {date}")
    return None


def process_day(shows):
    raw = defaultdict(lambda: {"sold": 0, "gross": 0, "seats": 0, "shows": 0, "venues": set()})

    for s in shows:
        if not isinstance(s, dict):
            continue

        chain = detect_chain(s.get("venue", ""))
        if not chain:
            continue

        raw[chain]["shows"] += 1
        raw[chain]["sold"] += s.get("sold", 0) or 0
        raw[chain]["gross"] += s.get("gross", 0) or 0
        raw[chain]["seats"] += s.get("totalSeats", 0) or 0
        raw[chain]["venues"].add(s.get("venue", "").strip())

    final = {}

    for chain, v in raw.items():
        sold, gross = apply_discount(chain, v["sold"], v["gross"], v["seats"])
        occ = round((sold / v["seats"]) * 100, 2) if v["seats"] else 0

        final[chain] = {
            "shows": v["shows"],
            "sold": sold,
            "venues": len(v["venues"]),
            "gross": round(gross, 2),
            "occ": occ
        }

    return final


def save(path, structure):
    ist = pytz.timezone("Asia/Kolkata")
    structure["lastUpdated"] = datetime.now(ist).strftime("%I:%M %p, %d %B %Y")

    with open(path, "w", encoding="utf-8") as f:
        json.dump(structure, f, indent=2, ensure_ascii=False)

    log(f"💾 Saved → {path}")


def process_month(year, month, include_future):
    filename = f"{year}-{month:02d}.json"
    path = os.path.join(OUTPUT_DIR, filename)

    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            month_json = json.load(f)
        log(f"🔁 Updating existing {filename}")
    else:
        month_json = {}
        log(f"🆕 Creating month file {filename}")

    today = datetime.now().date()

    # Compute last day of month
    month_end = (datetime(year, month, 28) + timedelta(days=5)).replace(day=1).date() - timedelta(days=1)

    # Apply future buffer only to current month
    if include_future:
        end_date = min(month_end, today + timedelta(days=5))
    else:
        end_date = month_end

    current = datetime(year, month, 1).date()

    while current <= end_date:

        # 🚨 Strict boundary: NEVER allow future dates into other month files
        if current.month != month:
            break

        d = current.strftime("%Y-%m-%d")

        # Skip old processed dates if not future tracking
        if d in month_json and not include_future:
            current += timedelta(days=1)
            continue

        # Skip past dates already saved during future runs
        if d in month_json and current < today and include_future:
            current += timedelta(days=1)
            continue

        log(f"🔎 Fetching {d}")
        data = fetch(d)

        if data:
            for movie, shows in data.items():
                if not isinstance(shows, list):
                    continue

                stats = process_day(shows)
                if stats:
                    month_json.setdefault(movie, {})[d] = {
                        c: [v["shows"], v["sold"], v["venues"], v["gross"], v["occ"]]
                        for c, v in stats.items()
                    }
                    log(f"✔ Updated {movie} → {d}")

        current += timedelta(days=1)

    save(path, month_json)


def main():
    today = datetime.now()

    # Process past months normally
    for y in range(2025, today.year + 1):
        for m in range(9, today.month):
            process_month(y, m, include_future=False)

    # Process current month with future buffer
    process_month(today.year, today.month, include_future=True)

    # Prepare next month WITHOUT buffer (it will get buffer later when it's current)
    next_month = today.replace(day=28) + timedelta(days=5)
    process_month(next_month.year, next_month.month, include_future=False)


if __name__ == "__main__":
    main()
