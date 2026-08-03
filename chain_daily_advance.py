#!/usr/bin/env python3
import json, os, requests, pytz
from datetime import datetime, timedelta
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_URL = "https://districtdata2026.pages.dev/advance"
OUTPUT_DIR = "Chain Daily Advance"
os.makedirs(OUTPUT_DIR, exist_ok=True)

TARGET_CHAINS = ["PVR", "INOX", "CINEPOLIS"]
BLOCK_RATES = {"PVR": 0.005, "CINEPOLIS": 0.0325, "INOX": 0.0}

MAX_WORKERS = 80
REQUEST_TIMEOUT = 10

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
        sold = max(0, round(adjusted))
        gross = max(0, sold * avg_price)
    return sold, gross

def decompress_show(arr, dicts):
    reverse = {
        k: {v: kk for kk, v in dicts[k].items()} for k in dicts
    }
    return {
        "city": reverse["cities"].get(arr[0], "Unknown"),
        "state": reverse["states"].get(arr[1], "Unknown"),
        "venue": reverse["venues"].get(arr[2], "Unknown"),
        "chain": reverse["chains"].get(arr[3], "Unknown"),
        "time": reverse["showtimes"].get(arr[4], ""),
        "audi": reverse["audis"].get(arr[5], ""),
        "totalSeats": arr[6],
        "available": arr[7],
        "sold": arr[8],
        "gross": arr[9] / 100.0,
        "occupancy": f"{arr[10]/100:.2f}%",
        "minsLeft": arr[11]
    }

def fetch_date(date, session):
    url = f"{BASE_URL}/{date}_Detailed.json"
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        if "dicts" in data and "movies" in data:
            decompressed = {}
            dicts = data["dicts"]
            for movie, compressed_list in data["movies"].items():
                decompressed[movie] = [decompress_show(arr, dicts) for arr in compressed_list]
            return decompressed
        return data
    except Exception:
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

def save_month_file(year, month, data_dict):
    filename = f"{year}-{month:02d}.json"
    path = os.path.join(OUTPUT_DIR, filename)
    ist = pytz.timezone("Asia/Kolkata")
    data_dict["lastUpdated"] = datetime.now(ist).strftime("%I:%M %p, %d %B %Y")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data_dict, f, indent=2, ensure_ascii=False)
    log(f"💾 Saved → {path}")

def main():
    today = datetime.now().date()
    start_date = datetime(2025, 8, 1).date()
    end_date = today + timedelta(days=5)

    all_dates = []
    current = start_date
    while current <= end_date:
        all_dates.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)

    log(f"📅 Total dates to check: {len(all_dates)}")

    # Load existing month files to skip already processed dates
    existing_data = {}
    for y in range(start_date.year, end_date.year + 1):
        for m in range(1, 13):
            fname = f"{y}-{m:02d}.json"
            path = os.path.join(OUTPUT_DIR, fname)
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    existing_data[fname] = json.load(f)

    dates_to_fetch = []
    for d in all_dates:
        year, month, _ = d.split('-')
        fname = f"{year}-{month}.json"
        if fname in existing_data:
            has_date = False
            for movie, dates_dict in existing_data[fname].items():
                if movie in ("lastUpdated",):
                    continue
                if d in dates_dict:
                    has_date = True
                    break
            if has_date:
                continue
        dates_to_fetch.append(d)

    log(f"🆕 Dates to fetch: {len(dates_to_fetch)}")
    if not dates_to_fetch:
        log("✅ All dates already processed.")
        return

    fetched_results = {}
    log(f"🚀 Fetching {len(dates_to_fetch)} dates with {MAX_WORKERS} workers...")

    def fetch_wrapper(date):
        with requests.Session() as session:
            return date, fetch_date(date, session)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(fetch_wrapper, d): d for d in dates_to_fetch}
        total = len(futures)
        completed = 0
        last_log = 0

        for future in as_completed(futures):
            d = futures[future]
            completed += 1
            # Log progress every 5% or every 50 dates, whichever is smaller
            if total >= 100:
                step = max(1, int(total / 20))   # 5% increments
            else:
                step = 5
            if completed % step == 0 or completed == total:
                log(f"⏳ Progress: {completed}/{total} ({100*completed//total}%)")
            try:
                date, data = future.result()
                if data:
                    fetched_results[date] = data
            except Exception:
                pass

    log(f"✅ Fetched {len(fetched_results)} dates successfully")

    # Group results by month
    month_buckets = defaultdict(dict)
    for date_str, data in fetched_results.items():
        year, month, _ = date_str.split('-')
        fname = f"{year}-{month}.json"
        for movie, shows in data.items():
            if not isinstance(shows, list):
                continue
            stats = process_day(shows)
            if stats:
                month_buckets[fname].setdefault(movie, {})[date_str] = {
                    c: [v["shows"], v["sold"], v["venues"], v["gross"], v["occ"]]
                    for c, v in stats.items()
                }

    # Merge and save month files
    for fname, new_data in month_buckets.items():
        path = os.path.join(OUTPUT_DIR, fname)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                month_data = json.load(f)
        else:
            month_data = {}
        for movie, dates_dict in new_data.items():
            if movie not in month_data:
                month_data[movie] = {}
            for date_str, chain_stats in dates_dict.items():
                month_data[movie][date_str] = chain_stats
        save_month_file(int(fname[:4]), int(fname[5:7]), month_data)

    log("🎉 All done!")

if __name__ == "__main__":
    main()
