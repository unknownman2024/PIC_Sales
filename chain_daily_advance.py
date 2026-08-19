#!/usr/bin/env python3

import json
import os
import requests
import pytz
import time
import calendar
from datetime import datetime, timedelta
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================
# CONFIG
# ============================================================

BASE_URL = "https://districtdata2026.pages.dev/advance"
OUTPUT_DIR = "Chain Daily Advance"

os.makedirs(OUTPUT_DIR, exist_ok=True)

CHAIN_ORDER = ["PVR", "INOX", "CINEPOLIS"]

BLOCK_RATES = {
    "PVR": 0.005,
    "CINEPOLIS": 0.0325,
    "INOX": 0.0,
}

MAX_WORKERS = 15
REQUEST_TIMEOUT = 30
RETRIES = 3

# Backfill start date (inclusive)
BACKFILL_START = datetime(2025, 8, 1).date()

# ============================================================
# LOG
# ============================================================

def log(msg):
    print(f"➡ {msg}", flush=True)

# ============================================================
# MOVIE KEY NORMALIZATION
# ============================================================

def normalize_movie_key(raw_key):
    if not isinstance(raw_key, str):
        return raw_key

    key = raw_key.strip()
    if key.endswith("]"):
        open_bracket = key.rfind("[")
        if open_bracket != -1:
            movie_name = key[:open_bracket].strip()
            inside = key[open_bracket + 1:-1].strip()
            if movie_name and inside:
                parts = [p.strip() for p in inside.split("|") if p.strip()]
                if parts:
                    language = parts[-1]
                    return f"{movie_name} | {language}"
    return key

# ============================================================
# CHAIN DETECTION
# ============================================================

def detect_chain(show):
    chain_value = str(show.get("chain", "")).strip().upper()
    if "PVR" in chain_value:
        return "PVR"
    if "INOX" in chain_value:
        return "INOX"
    if "CINEPOLIS" in chain_value:
        return "CINEPOLIS"

    venue = str(show.get("venue", "")).strip().upper()
    for chain in CHAIN_ORDER:
        if chain in venue:
            return chain
    return None

# ============================================================
# DISCOUNT
# ============================================================

def apply_discount(chain, sold, gross, seats):
    rate = BLOCK_RATES.get(chain, 0)
    if sold > 0 and rate > 0:
        avg_price = gross / sold if sold else 0
        blocked = seats * rate
        sold = max(0, round(sold - blocked))
        gross = max(0, sold * avg_price)
    return sold, gross

# ============================================================
# BUILD REVERSE DICTS
# ============================================================

def build_reverse_dicts(dicts):
    reverse = {}
    for name in ("cities", "states", "venues", "chains", "showtimes", "audis"):
        source = dicts.get(name, {})
        reverse[name] = {value: key for key, value in source.items()}
    return reverse

# ============================================================
# DECOMPRESS ONE SHOW
# ============================================================

def decompress_show(arr, reverse):
    if not isinstance(arr, list) or len(arr) < 12:
        return None

    def resolve(name, index, default=""):
        return reverse[name].get(arr[index], default)

    total_seats = arr[6] or 0
    available = arr[7] or 0
    sold = arr[8] or 0
    gross_x100 = arr[9] or 0
    occupancy_x100 = arr[10] or 0

    return {
        "city": resolve("cities", 0, "Unknown"),
        "state": resolve("states", 1, "Unknown"),
        "venue": resolve("venues", 2, "Unknown"),
        "chain": resolve("chains", 3, "Unknown"),
        "time": resolve("showtimes", 4, ""),
        "audi": resolve("audis", 5, ""),
        "totalSeats": total_seats,
        "available": available,
        "sold": sold,
        "gross": gross_x100 / 100.0,
        "occupancy": f"{occupancy_x100 / 100:.2f}%",
        "minsLeft": arr[11] or 0,
    }

# ============================================================
# FETCH ONE DATE
# ============================================================

def fetch_date(date_str, session):
    url = f"{BASE_URL}/{date_str}_Detailed.json"
    last_error = None

    for attempt in range(1, RETRIES + 1):
        try:
            response = session.get(
                url,
                timeout=REQUEST_TIMEOUT,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151 Safari/537.36",
                    "Accept": "application/json,text/plain,*/*",
                },
            )

            if response.status_code == 404:
                return {"status": "missing", "data": None}

            if response.status_code in {429, 500, 502, 503, 504}:
                last_error = f"HTTP {response.status_code}"
                if attempt < RETRIES:
                    time.sleep(attempt)
                    continue
                return {"status": "error", "data": None, "error": last_error}

            response.raise_for_status()

            try:
                data = response.json()
            except Exception as exc:
                return {"status": "error", "data": None, "error": f"JSON decode error: {exc}"}

            # New compressed format
            if (
                isinstance(data, dict)
                and isinstance(data.get("dicts"), dict)
                and isinstance(data.get("movies"), dict)
            ):
                dicts = data["dicts"]
                movies = data["movies"]
                reverse = build_reverse_dicts(dicts)
                decompressed = defaultdict(list)

                for raw_movie_key, compressed_list in movies.items():
                    if not isinstance(compressed_list, list):
                        continue
                    movie_key = normalize_movie_key(raw_movie_key)
                    for arr in compressed_list:
                        show = decompress_show(arr, reverse)
                        if show is not None:
                            decompressed[movie_key].append(show)

                return {
                    "status": "success",
                    "data": dict(decompressed),
                    "source_movies": len(movies),
                    "parsed_movies": len(decompressed),
                }

            # Legacy fallback
            if isinstance(data, dict):
                normalized = defaultdict(list)
                for raw_movie_key, shows in data.items():
                    if raw_movie_key in {"date", "lastUpdated", "dicts", "movies"}:
                        continue
                    if not isinstance(shows, list):
                        continue
                    movie_key = normalize_movie_key(raw_movie_key)
                    normalized[movie_key].extend(shows)

                return {
                    "status": "success",
                    "data": dict(normalized),
                    "source_movies": len(normalized),
                    "parsed_movies": len(normalized),
                }

            return {"status": "error", "data": None, "error": "Unknown JSON structure"}

        except requests.exceptions.Timeout as exc:
            last_error = f"timeout on attempt {attempt}/{RETRIES}"
            if attempt == RETRIES:
                return {"status": "error", "data": None, "error": last_error}

        except requests.exceptions.RequestException as exc:
            last_error = str(exc)
            if attempt == RETRIES:
                return {"status": "error", "data": None, "error": last_error}

        except Exception as exc:
            return {"status": "error", "data": None, "error": f"{type(exc).__name__}: {exc}"}

    return {"status": "error", "data": None, "error": last_error or "Unknown error"}

# ============================================================
# PROCESS DAY
# ============================================================

def process_day(shows):
    raw = defaultdict(lambda: {"sold": 0, "gross": 0, "seats": 0, "shows": 0, "venues": set()})

    for show in shows:
        if not isinstance(show, dict):
            continue
        chain = detect_chain(show)
        if not chain:
            continue

        raw[chain]["shows"] += 1
        raw[chain]["sold"] += show.get("sold", 0) or 0
        raw[chain]["gross"] += show.get("gross", 0) or 0
        raw[chain]["seats"] += show.get("totalSeats", 0) or 0

        venue = str(show.get("venue", "")).strip()
        if venue:
            raw[chain]["venues"].add(venue)

    result = []
    for chain in CHAIN_ORDER:
        value = raw.get(chain)
        if not value or value["seats"] == 0:
            result.append(None)
            continue

        sold, gross = apply_discount(chain, value["sold"], value["gross"], value["seats"])
        occupancy = round((sold / value["seats"]) * 100, 2)
        result.append([value["shows"], sold, len(value["venues"]), round(gross, 2), occupancy])

    return result

# ============================================================
# MONTH HELPERS
# ============================================================

def last_day_of_month(year, month):
    _, last = calendar.monthrange(year, month)
    return datetime(year, month, last).date()

def month_range(year, month):
    start = datetime(year, month, 1).date()
    end = last_day_of_month(year, month)
    while start <= end:
        yield start.strftime("%Y-%m-%d")
        start += timedelta(days=1)

def month_filename(year, month):
    return f"{year}-{month:02d}.json"

def load_existing_month(filename):
    path = os.path.join(OUTPUT_DIR, filename)
    if not os.path.exists(path):
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        log(f"⚠️ Could not read {filename}: {exc}")
        return {}

    result = {}
    for raw_movie, dates in data.items():
        if raw_movie == "lastUpdated":
            continue
        if not isinstance(dates, dict):
            continue
        movie = normalize_movie_key(raw_movie)
        result.setdefault(movie, {})
        for date_str, chain_data in dates.items():
            if isinstance(chain_data, list):
                result[movie][date_str] = chain_data
            elif isinstance(chain_data, dict):
                result[movie][date_str] = [chain_data.get(chain) for chain in CHAIN_ORDER]
    return result

# ============================================================
# MAIN
# ============================================================

def main():
    today = datetime.now().date()

    prev_year, prev_month = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    curr_year, curr_month = today.year, today.month

    log(f"🔄 Rebuilding previous month: {prev_year}-{prev_month:02d}")
    log(f"🔄 Rebuilding current month:  {curr_year}-{curr_month:02d}")

    # ------------------------------------------------------------------
    # Determine which months need to be fetched
    # ------------------------------------------------------------------
    months_to_fetch = set()

    # Always fetch previous and current months
    months_to_fetch.add((prev_year, prev_month))
    months_to_fetch.add((curr_year, curr_month))

    # Backfill missing older months (from BACKFILL_START to previous month, exclusive of current/previous)
    current_month_start = datetime(curr_year, curr_month, 1).date()
    d = BACKFILL_START
    while d < current_month_start:
        y, m = d.year, d.month
        # Skip if this month is the previous or current (already covered)
        if (y, m) not in months_to_fetch:
            filename = month_filename(y, m)
            if not os.path.exists(os.path.join(OUTPUT_DIR, filename)):
                months_to_fetch.add((y, m))
                log(f"📂 Missing {filename} → will backfill")
        # Advance to next month
        if m == 12:
            d = datetime(y + 1, 1, 1).date()
        else:
            d = datetime(y, m + 1, 1).date()

    # ------------------------------------------------------------------
    # Build list of date strings to fetch
    # ------------------------------------------------------------------
    dates_to_fetch = set()

    for year, month in months_to_fetch:
        if year == curr_year and month == curr_month:
            # Current month: from 1st to today + 5 days
            start = datetime(year, month, 1).date()
            end = today + timedelta(days=5)
            d = start
            while d <= end:
                dates_to_fetch.add(d.strftime("%Y-%m-%d"))
                d += timedelta(days=1)
        else:
            # Full month
            for date_str in month_range(year, month):
                dates_to_fetch.add(date_str)

    log(f"📅 Total dates to check: {len(dates_to_fetch)}")

    # ------------------------------------------------------------------
    # Fetch all dates concurrently
    # ------------------------------------------------------------------
    fetched_results = {}  # date_str -> data dict

    def fetch_wrapper(date_str):
        with requests.Session() as session:
            return date_str, fetch_date(date_str, session)

    total = len(dates_to_fetch)
    completed = 0

    log(f"🚀 Fetching with {MAX_WORKERS} workers...")
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(fetch_wrapper, date_str): date_str for date_str in dates_to_fetch}

        for future in as_completed(futures):
            date_str = futures[future]
            completed += 1

            try:
                date, result = future.result()
                status = result.get("status")

                if status == "missing":
                    log(f"[{completed}/{total}] ⚪ {date} – source missing")
                elif status == "success":
                    fetched_results[date] = result.get("data", {})
                    log(f"[{completed}/{total}] ✅ {date} – {result.get('parsed_movies', 0)} movies")
                else:
                    log(f"[{completed}/{total}] ❌ {date} – {result.get('error', 'unknown')}")
            except Exception as exc:
                log(f"[{completed}/{total}] ❌ {date_str} – worker error: {exc}")

            if completed % 10 == 0 or completed == total:
                log(f"⏳ Progress: {completed}/{total} ({100 * completed // total}%)")

    log(f"✅ Successfully fetched {len(fetched_results)} dates")

    # ------------------------------------------------------------------
    # Group fetched data by month and save each month
    # ------------------------------------------------------------------
    # Group by year-month
    month_groups = defaultdict(list)
    for date_str in fetched_results:
        y, m, _ = date_str.split("-")
        month_groups[(int(y), int(m))].append(date_str)

    if not month_groups:
        log("⚠️ No data fetched – nothing to save.")
        return

    for (year, month), date_list in month_groups.items():
        filename = month_filename(year, month)
        log(f"💾 Saving {filename}...")

        # Load existing month data (if any)
        old_data = load_existing_month(filename)

        # Build final data: keep old dates that were not fetched, overwrite fetched dates with new data
        final_data = defaultdict(dict)

        # First, copy old data for dates not in fetched_results (missing from source)
        fetched_dates_for_month = set(date_list)
        for movie, dates in old_data.items():
            for date_str, value in dates.items():
                if date_str not in fetched_dates_for_month:
                    final_data[movie][date_str] = value

        # Now add/replace with freshly fetched data
        for date_str in fetched_dates_for_month:
            movie_data = fetched_results.get(date_str)
            if not movie_data:
                continue
            for raw_movie, shows in movie_data.items():
                if not isinstance(shows, list):
                    continue
                movie = normalize_movie_key(raw_movie)
                stats = process_day(shows)
                if stats and any(item is not None for item in stats):
                    final_data[movie][date_str] = stats

        # Sort movies and dates for stable output
        sorted_output = {}
        for movie in sorted(final_data.keys(), key=lambda x: x.lower()):
            sorted_output[movie] = {}
            for date_str in sorted(final_data[movie].keys()):
                sorted_output[movie][date_str] = final_data[movie][date_str]

        # Write JSON (single-line, compact)
        output_json = json.dumps(sorted_output, ensure_ascii=False, separators=(",", ":"))
        path = os.path.join(OUTPUT_DIR, filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write(output_json)

        log(f"✅ Saved {filename} ({len(sorted_output)} movies)")

    log("🎉 Rebuild and backfill complete!")

# ============================================================
# ENTRY
# ============================================================

if __name__ == "__main__":
    main()
