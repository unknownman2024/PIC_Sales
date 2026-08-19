#!/usr/bin/env python3

import json
import os
import requests
import pytz

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

MAX_WORKERS = 80
REQUEST_TIMEOUT = 10


# ============================================================
# LOG
# ============================================================

def log(msg):
    print("➡", msg)


# ============================================================
# MOVIE KEY NORMALIZATION
# ============================================================

def normalize_movie_key(raw_key):
    """
    Supports both source formats:

        Spiderman [2D | Hindi]
        Spiderman | Hindi

    Canonical output:

        Spiderman | Hindi

    Important:
    - Only removes the final [ ... ] section when it exists.
    - Language is taken from the LAST "|" component inside brackets.
    - Existing "Movie | Language" keys are preserved.
    """

    if not isinstance(raw_key, str):
        return raw_key

    key = raw_key.strip()

    # --------------------------------------------------------
    # Format:
    # Movie [2D | Hindi]
    # Movie [IMAX | English]
    # Movie [4DX | Telugu]
    # --------------------------------------------------------
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

    # --------------------------------------------------------
    # Format:
    # Movie | Hindi
    # --------------------------------------------------------
    return key


# ============================================================
# CHAIN DETECTION
# ============================================================

def detect_chain(venue):
    if not venue:
        return None

    venue = str(venue).upper()

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
        adjusted = sold - blocked

        sold = max(0, round(adjusted))
        gross = max(0, sold * avg_price)

    return sold, gross


# ============================================================
# COMPRESSED JSON → NORMAL SHOW
# ============================================================

def decompress_show(arr, dicts):
    """
    New advance JSON show format:

    [
        cityId,
        stateId,
        venueId,
        chainId,
        showtimeId,
        audiId,
        totalSeats,
        available,
        sold,
        grossInPaise,
        occupancyHundredths,
        minsLeft
    ]
    """

    # --------------------------------------------------------
    # Build reverse dictionaries safely
    # --------------------------------------------------------

    reverse = {}

    for name in (
        "cities",
        "states",
        "venues",
        "chains",
        "showtimes",
        "audis",
    ):
        source = dicts.get(name, {})

        reverse[name] = {
            value: key
            for key, value in source.items()
        }

    # --------------------------------------------------------
    # Safe getters
    # --------------------------------------------------------

    def resolve(dictionary_name, index, default=""):
        try:
            value = arr[index]
        except (IndexError, TypeError):
            return default

        return reverse[dictionary_name].get(value, default)

    def number(index, default=0):
        try:
            value = arr[index]
            return value if value is not None else default
        except (IndexError, TypeError):
            return default

    # --------------------------------------------------------
    # Decompress
    # --------------------------------------------------------

    total_seats = number(6)
    available = number(7)
    sold = number(8)
    gross_cents = number(9)
    occupancy_raw = number(10)
    mins_left = number(11)

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

        # New datamaker stores gross * 100
        "gross": gross_cents / 100.0,

        # New datamaker stores occupancy * 100
        "occupancy": f"{occupancy_raw / 100:.2f}%",

        "minsLeft": mins_left,
    }


# ============================================================
# FETCH ONE DATE
# ============================================================

def fetch_date(date, session):
    url = f"{BASE_URL}/{date}_Detailed.json"

    try:
        resp = session.get(
            url,
            timeout=30,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
            },
        )

        # ----------------------------------------------------
        # IMPORTANT:
        # Show the actual HTTP status
        # ----------------------------------------------------

        if resp.status_code == 404:
            log(f"⚪ {date} – source file does not exist")
            return None

        if resp.status_code != 200:
            log(
                f"⚠️ {date} – HTTP {resp.status_code}"
            )
            return None

        # ----------------------------------------------------
        # Parse JSON separately so JSON errors are visible
        # ----------------------------------------------------

        try:
            data = resp.json()
        except Exception as e:
            log(
                f"❌ {date} – JSON decode error: {e}"
            )
            return None

        # ====================================================
        # NEW COMPRESSED FORMAT
        # ====================================================

        if (
            isinstance(data, dict)
            and isinstance(data.get("dicts"), dict)
            and isinstance(data.get("movies"), dict)
        ):
            dicts = data["dicts"]
            movies = data["movies"]

            decompressed = defaultdict(list)

            for raw_movie_key, compressed_list in movies.items():

                if not isinstance(compressed_list, list):
                    continue

                movie_key = normalize_movie_key(
                    raw_movie_key
                )

                for arr in compressed_list:

                    if not isinstance(arr, list):
                        continue

                    try:
                        show = decompress_show(
                            arr,
                            dicts
                        )

                        decompressed[movie_key].append(
                            show
                        )

                    except Exception as e:
                        log(
                            f"⚠️ {date} – bad show "
                            f"under {movie_key}: {e}"
                        )

            log(
                f"✅ {date} – parsed "
                f"{len(decompressed)} movies"
            )

            return dict(decompressed)

        # ====================================================
        # FALLBACK OLD FORMAT
        # ====================================================

        if isinstance(data, dict):

            normalized = {}

            for raw_movie_key, shows in data.items():

                if raw_movie_key == "lastUpdated":
                    continue

                if not isinstance(shows, list):
                    continue

                movie_key = normalize_movie_key(
                    raw_movie_key
                )

                normalized.setdefault(
                    movie_key,
                    []
                ).extend(shows)

            log(
                f"✅ {date} – parsed legacy format"
            )

            return normalized

        log(
            f"⚠️ {date} – unknown JSON structure"
        )

        return None

    except requests.exceptions.Timeout:
        log(
            f"⏱️ {date} – request timeout"
        )
        return None

    except requests.exceptions.RequestException as e:
        log(
            f"🌐 {date} – request error: {e}"
        )
        return None

    except Exception as e:
        log(
            f"❌ {date} – unexpected error: "
            f"{type(e).__name__}: {e}"
        )
        return None


# ============================================================
# PROCESS ONE DAY
# ============================================================

def process_day(shows):

    raw = defaultdict(
        lambda: {
            "sold": 0,
            "gross": 0,
            "seats": 0,
            "shows": 0,
            "venues": set(),
        }
    )

    for s in shows:

        if not isinstance(s, dict):
            continue

        chain = detect_chain(
            s.get("venue", "")
        )

        if not chain:
            continue

        raw[chain]["shows"] += 1

        raw[chain]["sold"] += (
            s.get("sold", 0) or 0
        )

        raw[chain]["gross"] += (
            s.get("gross", 0) or 0
        )

        raw[chain]["seats"] += (
            s.get("totalSeats", 0) or 0
        )

        venue_name = str(
            s.get("venue", "")
        ).strip()

        if venue_name:
            raw[chain]["venues"].add(venue_name)

    result = []

    for chain in CHAIN_ORDER:

        v = raw.get(chain)

        if not v or v["seats"] == 0:
            result.append(None)
            continue

        sold, gross = apply_discount(
            chain,
            v["sold"],
            v["gross"],
            v["seats"],
        )

        occ = (
            round(
                (sold / v["seats"]) * 100,
                2,
            )
            if v["seats"]
            else 0
        )

        result.append(
            [
                v["shows"],
                sold,
                len(v["venues"]),
                round(gross, 2),
                occ,
            ]
        )

    return result


# ============================================================
# SAVE MONTH
# ============================================================

def save_month_file(year, month, data_dict):

    filename = f"{year}-{month:02d}.json"

    path = os.path.join(
        OUTPUT_DIR,
        filename
    )

    ist = pytz.timezone("Asia/Kolkata")

    data_dict["lastUpdated"] = (
        datetime.now(ist)
        .strftime("%I:%M %p, %d %B %Y")
    )

    with open(
        path,
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(
            data_dict,
            f,
            indent=2,
            ensure_ascii=False
        )

    log(f"💾 Saved → {path}")


# ============================================================
# LOAD EXISTING MONTH
# ============================================================

def load_existing_month(fname):

    path = os.path.join(
        OUTPUT_DIR,
        fname
    )

    if not os.path.exists(path):
        return {}

    with open(
        path,
        "r",
        encoding="utf-8"
    ) as f:
        data = json.load(f)

    # --------------------------------------------------------
    # Convert old dict chain format → array format
    # --------------------------------------------------------

    for movie, dates in list(data.items()):

        if movie == "lastUpdated":
            continue

        if not isinstance(dates, dict):
            continue

        for date_str, chain_data in list(
            dates.items()
        ):

            if not isinstance(chain_data, dict):
                continue

            new_entry = []

            for chain in CHAIN_ORDER:

                if chain in chain_data:
                    new_entry.append(
                        chain_data[chain]
                    )
                else:
                    new_entry.append(None)

            data[movie][date_str] = new_entry

    return data


# ============================================================
# MAIN
# ============================================================

def main():

    today = datetime.now().date()

    start_date = datetime(
        2025,
        8,
        1
    ).date()

    end_date = (
        today +
        timedelta(days=5)
    )

    # --------------------------------------------------------
    # Generate dates
    # --------------------------------------------------------

    all_dates = []

    current = start_date

    while current <= end_date:

        all_dates.append(
            current.strftime("%Y-%m-%d")
        )

        current += timedelta(days=1)

    log(
        f"📅 Total dates to check: {len(all_dates)}"
    )

    # --------------------------------------------------------
    # Load existing month files
    # --------------------------------------------------------

    existing_data = {}

    for y in range(
        start_date.year,
        end_date.year + 1
    ):
        for m in range(1, 13):

            fname = f"{y}-{m:02d}.json"

            existing_data[fname] = (
                load_existing_month(fname)
            )

    # --------------------------------------------------------
    # Determine missing dates
    # --------------------------------------------------------

    dates_to_fetch = []

    for d in all_dates:

        year, month, _ = d.split("-")

        fname = f"{year}-{month}.json"

        month_data = existing_data.get(
            fname,
            {}
        )

        has_date = False

        for movie, dates_dict in (
            month_data.items()
        ):

            if movie == "lastUpdated":
                continue

            if not isinstance(
                dates_dict,
                dict
            ):
                continue

            if d in dates_dict:
                has_date = True
                break

        if not has_date:
            dates_to_fetch.append(d)

    log(
        f"🆕 Dates to fetch: {len(dates_to_fetch)}"
    )

    if not dates_to_fetch:

        log(
            "✅ All dates already processed."
        )

        return

    # --------------------------------------------------------
    # Fetch
    # --------------------------------------------------------

    fetched_results = {}

    log(
        f"🚀 Fetching {len(dates_to_fetch)} dates "
        f"with {MAX_WORKERS} workers..."
    )

    def fetch_wrapper(date):

        with requests.Session() as session:

            return (
                date,
                fetch_date(
                    date,
                    session
                )
            )

    total = len(dates_to_fetch)

    completed = 0

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        futures = {
            executor.submit(
                fetch_wrapper,
                d
            ): d
            for d in dates_to_fetch
        }

        for future in as_completed(
            futures
        ):

            d = futures[future]

            completed += 1

            try:

                date, data = (
                    future.result()
                )

                if data:

                    fetched_results[
                        date
                    ] = data

                    log(
                        f"[{completed}/{total}] "
                        f"✅ {date} – fetched"
                    )

                else:

                    log(
                        f"[{completed}/{total}] "
                        f"❌ {date} – no data"
                    )

            except Exception as e:

                log(
                    f"[{completed}/{total}] "
                    f"⚠ {d} – error: {e}"
                )

            if (
                completed % 10 == 0
                or completed == total
            ):

                log(
                    f"⏳ Progress: "
                    f"{completed}/{total} "
                    f"({100 * completed // total}%)"
                )

    log(
        f"✅ Fetched {len(fetched_results)} "
        f"dates successfully"
    )

    # --------------------------------------------------------
    # Group by month
    # --------------------------------------------------------

    month_buckets = defaultdict(dict)

    for date_str, data in (
        fetched_results.items()
    ):

        year, month, _ = date_str.split("-")

        fname = f"{year}-{month}.json"

        for raw_movie, shows in data.items():

            if not isinstance(
                shows,
                list
            ):
                continue

            # ------------------------------------------------
            # Normalize movie key AGAIN here.
            #
            # This protects against any source variation.
            # ------------------------------------------------

            movie = normalize_movie_key(
                raw_movie
            )

            stats = process_day(
                shows
            )

            if stats and any(stats):

                month_buckets[
                    fname
                ].setdefault(
                    movie,
                    {}
                )[date_str] = stats

    # --------------------------------------------------------
    # Merge + save
    # --------------------------------------------------------

    for fname, new_data in (
        month_buckets.items()
    ):

        month_data = existing_data.get(
            fname,
            {}
        )

        last_updated = month_data.pop(
            "lastUpdated",
            None
        )

        for movie, dates_dict in (
            new_data.items()
        ):

            if movie not in month_data:
                month_data[movie] = {}

            for date_str, chain_stats in (
                dates_dict.items()
            ):

                month_data[movie][
                    date_str
                ] = chain_stats

        if last_updated:

            month_data[
                "lastUpdated"
            ] = last_updated

        save_month_file(
            int(fname[:4]),
            int(fname[5:7]),
            month_data
        )

    log("🎉 All done!")


# ============================================================
# ENTRY
# ============================================================

if __name__ == "__main__":
    main()
