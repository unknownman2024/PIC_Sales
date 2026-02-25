import json, os, requests, pytz
from datetime import datetime, timedelta
from collections import defaultdict

# ---------------- CONFIG ----------------
BASE_URL = "https://district24.pages.dev/Daily%20Boxoffice"
OUTPUT_DIR = "Chain Daily Breakdown"

os.makedirs(OUTPUT_DIR, exist_ok=True)

CHAIN_LIST = [
    "PVR", "INOX", "Cinepolis", "Movietime Cinemas", "Wave Cinemas",
    "Miraj Cinemas", "Rajhans Cinemas", "Asian Mukta",
    "MovieMax", "Mythri Cinemas", "Maxus Cinemas"
]

START_YEAR = 2025
START_MONTH = 9   # September 2025

# ---------------- UTILS ----------------
def log(msg):
    print(f"➡ {msg}")

def detect_chain(venue):
    venue = venue.lower()
    for chain in CHAIN_LIST:
        if chain.lower() in venue:
            return chain
    return None

def fetch(date):
    url = f"{BASE_URL}/{date}_Detailed.json"
    try:
        r = requests.get(url, timeout=15)
        if r.status_code == 200:
            log(f"📥 Loaded {date}")
            return r.json()
    except Exception as e:
        log(f"❌ Fetch failed for {date}: {e}")

    log(f"⚠ No data for {date}")
    return None

# ---------------- PROCESSING ----------------
def process(shows):
    chain_data = defaultdict(lambda: {
        "sold": 0,
        "gross": 0,
        "seats": 0,
        "shows": 0,
        "venues": set()
    })

    for s in shows:
        venue = s.get("venue", "").strip()
        chain = detect_chain(venue)
        if not chain:
            continue

        chain_data[chain]["shows"] += 1
        chain_data[chain]["sold"] += s.get("sold", 0) or 0
        chain_data[chain]["gross"] += s.get("gross", 0) or 0
        chain_data[chain]["seats"] += s.get("totalSeats", 0) or 0
        chain_data[chain]["venues"].add(venue)

    for c, v in chain_data.items():
        v["occ"] = round((v["sold"] / v["seats"] * 100), 2) if v["seats"] else 0
        v["venue_count"] = len(v["venues"])

    return chain_data

# ---------------- SAVE ----------------
def save(filepath, data):
    ist = pytz.timezone("Asia/Kolkata")
    data["lastUpdated"] = datetime.now(ist).strftime("%I:%M %p, %d %B %Y")

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    log(f"💾 Saved → {filepath}")

# ---------------- MONTH PROCESSOR (FULL REFRESH MODE) ----------------
def process_month(year, month, allow_update):
    fname = f"{year}-{month:02d}.json"
    path = os.path.join(OUTPUT_DIR, fname)

    ist = pytz.timezone("Asia/Kolkata")
    today = datetime.now(ist).date()
    month_start = datetime(year, month, 1).date()

    # ⛔ Future months block
    if month_start > today:
        log(f"⛔ Blocked future month → {fname}")
        return

    # 🔒 Past month exists → locked (no refetch)
    if os.path.exists(path) and not allow_update:
        log(f"⏭ Skipping locked month → {fname}")
        return

    # ✅ FORCE REFRESH: current month always rebuilt fresh
    log(f"🔄 Full refresh → {fname}")
    month_data = {}

    # Determine end date
    if allow_update:
        end_date = today
    else:
        end_date = (month_start.replace(day=28) + timedelta(days=5)).replace(day=1) - timedelta(days=1)

    cur = month_start
    while cur <= end_date:
        date = cur.strftime("%Y-%m-%d")

        daily = fetch(date)
        if daily:
            for movie, shows in daily.items():
                if not isinstance(shows, list):
                    continue

                stats = process(shows)
                if not stats:
                    continue

                month_data.setdefault(movie, {})[date] = {
                    c: [
                        v["shows"],
                        v["sold"],
                        v["venue_count"],
                        v["gross"],
                        v["occ"]
                    ]
                    for c, v in stats.items()
                }

            log(f"✔ Updated → {date}")

        cur += timedelta(days=1)

    save(path, month_data)

# ---------------- MAIN CONTROLLER ----------------
def main():
    ist = pytz.timezone("Asia/Kolkata")
    today = datetime.now(ist).date()

    y, m = START_YEAR, START_MONTH

    while True:
        month_start = datetime(y, m, 1).date()

        # ⛔ Stop at future month
        if month_start > today:
            log(f"🛑 Stop at future month → {y}-{m:02d}")
            break

        is_current = (y == today.year and m == today.month)

        process_month(y, m, allow_update=is_current)

        m += 1
        if m > 12:
            m = 1
            y += 1

if __name__ == "__main__":
    main()
