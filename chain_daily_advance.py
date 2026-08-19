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

MAX_WORKERS = 15
REQUEST_TIMEOUT = 30
RETRIES = 3

# Older months are retained, but every run REBUILDS:
#   - previous month
#   - current month
#
# If these months already exist locally, they are ignored
# during the rebuild and regenerated from source.
# ============================================================


# ============================================================
# LOG
# ============================================================

def log(msg):
    print(f"➡ {msg}", flush=True)


# ============================================================
# MOVIE KEY NORMALIZATION
# ============================================================

def normalize_movie_key(raw_key):
    """
    Supports:

        Movie [2D | Hindi]
        Movie [IMAX | Hindi]
        Movie | Hindi

    All become:

        Movie | Hindi
    """

    if not isinstance(raw_key, str):
        return raw_key

    key = raw_key.strip()

    if key.endswith("]"):

        open_bracket = key.rfind("[")

        if open_bracket != -1:

            movie_name = key[:open_bracket].strip()
            inside = key[open_bracket + 1:-1].strip()

            if movie_name and inside:

                parts = [
                    p.strip()
                    for p in inside.split("|")
                    if p.strip()
                ]

                if parts:
                    language = parts[-1]
                    return f"{movie_name} | {language}"

    return key


# ============================================================
# CHAIN DETECTION
# ============================================================

def detect_chain(show):

    chain_value = str(
        show.get("chain", "")
    ).strip().upper()

    if "PVR" in chain_value:
        return "PVR"

    if "INOX" in chain_value:
        return "INOX"

    if "CINEPOLIS" in chain_value:
        return "CINEPOLIS"

    venue = str(
        show.get("venue", "")
    ).strip().upper()

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

        avg_price = (
            gross / sold
            if sold
            else 0
        )

        blocked = seats * rate

        sold = max(
            0,
            round(sold - blocked)
        )

        gross = max(
            0,
            sold * avg_price
        )

    return sold, gross


# ============================================================
# BUILD REVERSE DICTS
# ============================================================

def build_reverse_dicts(dicts):

    reverse = {}

    for name in (
        "cities",
        "states",
        "venues",
        "chains",
        "showtimes",
        "audis",
    ):

        source = dicts.get(
            name,
            {}
        )

        reverse[name] = {
            value: key
            for key, value in source.items()
        }

    return reverse


# ============================================================
# DECOMPRESS ONE SHOW
# ============================================================

def decompress_show(arr, reverse):

    if not isinstance(arr, list):
        return None

    if len(arr) < 12:
        return None

    def resolve(name, index, default=""):

        return reverse[name].get(
            arr[index],
            default
        )

    total_seats = arr[6] or 0
    available = arr[7] or 0
    sold = arr[8] or 0

    gross_x100 = arr[9] or 0
    occupancy_x100 = arr[10] or 0

    return {
        "city": resolve(
            "cities",
            0,
            "Unknown"
        ),

        "state": resolve(
            "states",
            1,
            "Unknown"
        ),

        "venue": resolve(
            "venues",
            2,
            "Unknown"
        ),

        "chain": resolve(
            "chains",
            3,
            "Unknown"
        ),

        "time": resolve(
            "showtimes",
            4,
            ""
        ),

        "audi": resolve(
            "audis",
            5,
            ""
        ),

        "totalSeats": total_seats,
        "available": available,
        "sold": sold,

        # Source stores gross × 100
        "gross": gross_x100 / 100.0,

        # Source stores occupancy × 100
        "occupancy": (
            f"{occupancy_x100 / 100:.2f}%"
        ),

        "minsLeft": arr[11] or 0,
    }


# ============================================================
# FETCH ONE DATE
# ============================================================

def fetch_date(date_str, session):

    url = (
        f"{BASE_URL}/"
        f"{date_str}_Detailed.json"
    )

    last_error = None

    for attempt in range(
        1,
        RETRIES + 1
    ):

        try:

            response = session.get(
                url,
                timeout=REQUEST_TIMEOUT,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 "
                        "(Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 "
                        "(KHTML, like Gecko) "
                        "Chrome/151 Safari/537.36"
                    ),
                    "Accept": (
                        "application/json,text/plain,*/*"
                    ),
                },
            )

            # ------------------------------------------------
            # SOURCE FILE DOES NOT EXIST
            # ------------------------------------------------

            if response.status_code == 404:

                return {
                    "status": "missing",
                    "data": None,
                }

            # ------------------------------------------------
            # RETRY TEMPORARY ERRORS
            # ------------------------------------------------

            if response.status_code in {
                429,
                500,
                502,
                503,
                504,
            }:

                last_error = (
                    f"HTTP {response.status_code}"
                )

                if attempt < RETRIES:
                    time.sleep(attempt)
                    continue

                return {
                    "status": "error",
                    "data": None,
                    "error": last_error,
                }

            response.raise_for_status()

            # ------------------------------------------------
            # JSON
            # ------------------------------------------------

            try:
                data = response.json()

            except Exception as exc:

                return {
                    "status": "error",
                    "data": None,
                    "error": (
                        f"JSON decode error: {exc}"
                    ),
                }

            # =================================================
            # NEW COMPRESSED FORMAT
            # =================================================

            if (
                isinstance(data, dict)
                and isinstance(
                    data.get("dicts"),
                    dict
                )
                and isinstance(
                    data.get("movies"),
                    dict
                )
            ):

                dicts = data["dicts"]
                movies = data["movies"]

                reverse = build_reverse_dicts(
                    dicts
                )

                decompressed = defaultdict(list)

                for raw_movie_key, compressed_list in (
                    movies.items()
                ):

                    if not isinstance(
                        compressed_list,
                        list
                    ):
                        continue

                    movie_key = normalize_movie_key(
                        raw_movie_key
                    )

                    for arr in compressed_list:

                        show = decompress_show(
                            arr,
                            reverse
                        )

                        if show is not None:
                            decompressed[
                                movie_key
                            ].append(show)

                return {
                    "status": "success",
                    "data": dict(decompressed),
                    "source_movies": len(movies),
                    "parsed_movies": len(decompressed),
                }

            # =================================================
            # LEGACY FALLBACK
            # =================================================

            if isinstance(data, dict):

                normalized = defaultdict(list)

                for raw_movie_key, shows in data.items():

                    if raw_movie_key in {
                        "date",
                        "lastUpdated",
                        "dicts",
                        "movies",
                    }:
                        continue

                    if not isinstance(
                        shows,
                        list
                    ):
                        continue

                    movie_key = normalize_movie_key(
                        raw_movie_key
                    )

                    normalized[
                        movie_key
                    ].extend(shows)

                return {
                    "status": "success",
                    "data": dict(normalized),
                    "source_movies": len(normalized),
                    "parsed_movies": len(normalized),
                }

            return {
                "status": "error",
                "data": None,
                "error": "Unknown JSON structure",
            }

        except requests.exceptions.Timeout as exc:

            last_error = (
                f"timeout on attempt "
                f"{attempt}/{RETRIES}"
            )

            if attempt == RETRIES:

                return {
                    "status": "error",
                    "data": None,
                    "error": last_error,
                }

        except requests.exceptions.RequestException as exc:

            last_error = str(exc)

            if attempt == RETRIES:

                return {
                    "status": "error",
                    "data": None,
                    "error": last_error,
                }

        except Exception as exc:

            return {
                "status": "error",
                "data": None,
                "error": (
                    f"{type(exc).__name__}: {exc}"
                ),
            }

    return {
        "status": "error",
        "data": None,
        "error": last_error or "Unknown error",
    }


# ============================================================
# PROCESS DAY
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

    for show in shows:

        if not isinstance(show, dict):
            continue

        chain = detect_chain(show)

        if not chain:
            continue

        raw[chain]["shows"] += 1

        raw[chain]["sold"] += (
            show.get("sold", 0) or 0
        )

        raw[chain]["gross"] += (
            show.get("gross", 0) or 0
        )

        raw[chain]["seats"] += (
            show.get("totalSeats", 0) or 0
        )

        venue = str(
            show.get("venue", "")
        ).strip()

        if venue:
            raw[chain][
                "venues"
            ].add(venue)

    result = []

    for chain in CHAIN_ORDER:

        value = raw.get(chain)

        if (
            not value
            or value["seats"] == 0
        ):

            result.append(None)
            continue

        sold, gross = apply_discount(
            chain,
            value["sold"],
            value["gross"],
            value["seats"],
        )

        occupancy = round(
            (
                sold /
                value["seats"]
            ) * 100,
            2,
        )

        result.append(
            [
                value["shows"],
                sold,
                len(value["venues"]),
                round(gross, 2),
                occupancy,
            ]
        )

    return result


# ============================================================
# MONTH HELPERS
# ============================================================

def month_start(year, month):

    return datetime(
        year,
        month,
        1
    ).date()


def previous_month(year, month):

    if month == 1:
        return year - 1, 12

    return year, month - 1


# ============================================================
# LOAD EXISTING MONTH
# ============================================================

def load_existing_month(filename):

    path = os.path.join(
        OUTPUT_DIR,
        filename
    )

    if not os.path.exists(path):
        return {}

    try:

        with open(
            path,
            "r",
            encoding="utf-8"
        ) as file:
            data = json.load(file)

    except Exception as exc:

        log(
            f"⚠️ Could not read "
            f"{filename}: {exc}"
        )

        return {}

    result = {}

    for raw_movie, dates in data.items():

        if raw_movie == "lastUpdated":
            continue

        if not isinstance(
            dates,
            dict
        ):
            continue

        movie = normalize_movie_key(
            raw_movie
        )

        result.setdefault(
            movie,
            {}
        )

        for date_str, chain_data in dates.items():

            if isinstance(
                chain_data,
                list
            ):

                result[movie][
                    date_str
                ] = chain_data

            elif isinstance(
                chain_data,
                dict
            ):

                result[movie][
                    date_str
                ] = [
                    chain_data.get(
                        chain
                    )
                    for chain in CHAIN_ORDER
                ]

    return result


# ============================================================
# GET REBUILD DATES
# ============================================================

def get_rebuild_dates():

    today = datetime.now().date()

    # Current month
    current_year = today.year
    current_month = today.month

    # Previous month
    prev_year, prev_month = previous_month(
        current_year,
        current_month
    )

    dates = []

    # --------------------------------------------------------
    # Previous month: ENTIRE month
    # --------------------------------------------------------

    prev_start = month_start(
        prev_year,
        prev_month
    )

    current_start = month_start(
        current_year,
        current_month
    )

    d = prev_start

    while d < current_start:

        dates.append(
            d.strftime("%Y-%m-%d")
        )

        d += timedelta(days=1)

    # --------------------------------------------------------
    # Current month: 1st → today + 5 days
    #
    # This keeps the advance window current.
    # --------------------------------------------------------

    end_date = today + timedelta(days=5)

    d = current_start

    while d <= end_date:

        dates.append(
            d.strftime("%Y-%m-%d")
        )

        d += timedelta(days=1)

    return dates


# ============================================================
# MAIN
# ============================================================

def main():

    today = datetime.now().date()

    prev_year, prev_month = previous_month(
        today.year,
        today.month
    )

    log(
        f"🔄 Rebuilding previous month: "
        f"{prev_year}-{prev_month:02d}"
    )

    log(
        f"🔄 Rebuilding current month: "
        f"{today.year}-{today.month:02d}"
    )

    dates_to_fetch = get_rebuild_dates()

    log(
        f"📅 Total source dates to check: "
        f"{len(dates_to_fetch)}"
    )

    # --------------------------------------------------------
    # Load ONLY months that are being rebuilt.
    # Other historical months remain untouched.
    # --------------------------------------------------------

    rebuild_months = {
        f"{prev_year}-{prev_month:02d}.json",
        f"{today.year}-{today.month:02d}.json",
    }

    existing_data = {}

    for filename in rebuild_months:

        existing_data[filename] = (
            load_existing_month(
                filename
            )
        )

    # --------------------------------------------------------
    # FETCH
    # --------------------------------------------------------

    fetched_results = {}

    log(
        f"🚀 Fetching with "
        f"{MAX_WORKERS} workers..."
    )

    def fetch_wrapper(date_str):

        with requests.Session() as session:

            return (
                date_str,
                fetch_date(
                    date_str,
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
                date_str
            ): date_str

            for date_str in dates_to_fetch
        }

        for future in as_completed(
            futures
        ):

            date_str = futures[
                future
            ]

            completed += 1

            try:

                date, result = (
                    future.result()
                )

                status = result.get(
                    "status"
                )

                if status == "missing":

                    log(
                        f"[{completed}/{total}] "
                        f"⚪ {date} – "
                        f"source missing"
                    )

                    # IMPORTANT:
                    # Do NOT manufacture empty data.
                    # Missing source = no data for rebuild.

                elif status == "success":

                    fetched_results[
                        date
                    ] = result.get(
                        "data",
                        {}
                    )

                    log(
                        f"[{completed}/{total}] "
                        f"✅ {date} – "
                        f"{result.get('parsed_movies', 0)} movies"
                    )

                else:

                    log(
                        f"[{completed}/{total}] "
                        f"❌ {date} – "
                        f"{result.get('error', 'unknown')}"
                    )

            except Exception as exc:

                log(
                    f"[{completed}/{total}] "
                    f"❌ {date_str} – "
                    f"worker error: {exc}"
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
        f"✅ Successfully fetched "
        f"{len(fetched_results)} dates"
    )

    # ========================================================
    # REBUILD MONTH DATA FROM SCRATCH
    # ========================================================

    rebuilt_months = defaultdict(
        lambda: defaultdict(dict)
    )

    for date_str, movie_data in (
        fetched_results.items()
    ):

        if not movie_data:
            continue

        year, month, _ = (
            date_str.split("-")
        )

        filename = (
            f"{year}-{month}.json"
        )

        for raw_movie, shows in (
            movie_data.items()
        ):

            if not isinstance(
                shows,
                list
            ):
                continue

            movie = normalize_movie_key(
                raw_movie
            )

            stats = process_day(
                shows
            )

            if not stats:
                continue

            # Only store useful results.
            if not any(
                item is not None
                for item in stats
            ):
                continue

            rebuilt_months[
                filename
            ][movie][
                date_str
            ] = stats

    # ========================================================
    # SAVE REBUILT MONTHS
    # ========================================================

    for filename in rebuild_months:

        month_data = rebuilt_months.get(
            filename,
            {}
        )

        # ----------------------------------------------------
        # IMPORTANT:
        #
        # We rebuild the month completely from source.
        #
        # However, a missing source file must NOT cause
        # previously known date data to disappear.
        #
        # Therefore:
        # - fetched date → replace with fresh source data
        # - missing date → retain existing date
        #
        # This protects historical data when the CDN no
        # longer contains an old Detailed.json.
        # ----------------------------------------------------

        old_data = existing_data.get(
            filename,
            {}
        )

        final_data = {}

        # ----------------------------------------------------
        # First copy old dates only for dates whose source
        # was missing/unavailable.
        # ----------------------------------------------------

        fetched_dates_for_month = {
            date_str
            for date_str in fetched_results
            if date_str.startswith(
                filename[:7]
            )
        }

        missing_dates_for_month = {
            date_str
            for date_str in dates_to_fetch
            if date_str.startswith(
                filename[:7]
            )
            and date_str
            not in fetched_dates_for_month
        }

        for movie, dates in old_data.items():

            if not isinstance(
                dates,
                dict
            ):
                continue

            for date_str, value in dates.items():

                if date_str in missing_dates_for_month:

                    final_data.setdefault(
                        movie,
                        {}
                    )[date_str] = value

        # ----------------------------------------------------
        # Add fresh rebuilt source data.
        # ----------------------------------------------------

        for movie, dates in (
            month_data.items()
        ):

            final_data.setdefault(
                movie,
                {}
            )

            for date_str, value in dates.items():

                final_data[movie][
                    date_str
                ] = value

        # ----------------------------------------------------
        # Sort movies and dates for stable JSON.
        # ----------------------------------------------------

        sorted_output = {}

        for movie in sorted(
            final_data.keys(),
            key=lambda x: x.lower()
        ):

            sorted_output[movie] = {}

            for date_str in sorted(
                final_data[movie].keys()
            ):

                sorted_output[movie][
                    date_str
                ] = final_data[movie][
                    date_str
                ]

        # ----------------------------------------------------
        # SINGLE-LINE JSON
        #
        # No indent.
        # No newlines.
        # No spaces after separators.
        # ----------------------------------------------------

        output_json = json.dumps(
            sorted_output,
            ensure_ascii=False,
            separators=(",", ":")
        )

        path = os.path.join(
            OUTPUT_DIR,
            filename
        )

        with open(
            path,
            "w",
            encoding="utf-8"
        ) as file:

            file.write(
                output_json
            )

        log(
            f"💾 Rebuilt → {path} "
            f"({len(sorted_output)} movies)"
        )

    log("🎉 Rebuild complete!")


# ============================================================
# ENTRY
# ============================================================

if __name__ == "__main__":
    main()
