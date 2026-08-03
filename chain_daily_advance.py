#!/usr/bin/env python3
import json, os, requests, pytz
from datetime import datetime, timedelta
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

BASE_URL = "https://districtdata2026.pages.dev/advance"
OUTPUT_DIR = "Chain Daily Advance"
os.makedirs(OUTPUT_DIR, exist_ok=True)

TARGET_CHAINS = ["PVR", "INOX", "CINEPOLIS"]

BLOCK_RATES = {
    "PVR": 0.005,
    "CINEPOLIS": 0.0325,
    "INOX": 0.0
}

# Lock for thread-safe printing
print_lock = threading.Lock()

def log(msg):
    with print_lock:
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
    """Convert compressed array back to show dict using reverse dicts."""
    reverse = {
        k: {v: kk for kk, v in dicts[k].items()} for k in dicts
    }
    # order: [cityId, stateId, venueId, chainId, timeId, audiId, total, avail, sold, gross, occBp, minsLeft]
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

def fetch(date):
    url = f"{BASE_URL}/{date}_Detailed.json"
    try:
        r = requests.get(url, timeout=12)
        if r.status_code == 200:
            data = r.json()
            # Check for compressed format
            if "dicts" in data and "movies" in data:
                # Decompress all shows per movie
                decompressed_movies = {}
                dicts = data["dicts"]
                for movie, compressed_list in data["movies"].items():
                    shows = [decompress_show(arr, dicts) for arr in compressed_list]
                    decompressed_movies[movie] = shows
                return decompressed_movies
            else:
                # Fallback: old format (direct list of shows per movie)
                return data
        else:
            log(f"⚠ No data for {date} (HTTP {r.status_code})")
    except Exception as e:
        log(f"⚠ Error fetching {date}: {e}")
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
    month_end = (datetime(year, month, 28) + timedelta(days=5)).replace(day=1).date() - timedelta(days=1)

    if include_future:
        end_date = min(month_end, today + timedelta(days=5))
    else:
        end_date = month_end

    current = datetime(year, month, 1).date()

    # Build list of dates to fetch
    dates_to_fetch = []
    while current <= end_date:
        if current.month != month:
            break
        d = current.strftime("%Y-%m-%d")

        # Skip logic
        if d in month_json and not include_future:
            current += timedelta(days=1)
            continue
        if d in month_json and current < today and include_future:
            current += timedelta(days=1)
            continue

        dates_to_fetch.append(d)
        current += timedelta(days=1)

    if not dates_to_fetch:
        log(f"✅ No new dates to fetch for {filename}")
        return

    log(f"🚀 Fetching {len(dates_to_fetch)} dates in parallel (max 50 threads)")

    # Parallel fetch
    results = {}
    with ThreadPoolExecutor(max_workers=50) as executor:
        future_to_date = {executor.submit(fetch, d): d for d in dates_to_fetch}
        for future in as_completed(future_to_date):
            d = future_to_date[future]
            try:
                data = future.result()
                if data:
                    results[d] = data
                    log(f"✅ {d} fetched")
                else:
                    log(f"❌ {d} – no data")
            except Exception as e:
                log(f"❌ {d} – error: {e}")

    # Merge results into month_json
    for d, data in results.items():
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

    save(path, month_json)

def main():
    today = datetime.now()

    # Process past months normally
    for y in range(2025, today.year + 1):
        for m in range(1, today.month):
            process_month(y, m, include_future=False)

    # Process current month with future buffer
    process_month(today.year, today.month, include_future=True)

    # Prepare next month WITHOUT buffer (it will get buffer later when it's current)
    next_month = today.replace(day=28) + timedelta(days=5)
    process_month(next_month.year, next_month.month, include_future=False)

if __name__ == "__main__":
    main()
