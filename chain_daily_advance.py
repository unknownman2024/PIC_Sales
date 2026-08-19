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

# New advance JSON files can be large.
MAX_WORKERS = 15
REQUEST_TIMEOUT = 30
RETRIES = 3

# Historical range to process.
START_DATE = datetime(2025, 8, 1).date()

# Include current date + 5 advance days.
ADVANCE_DAYS = 5


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
    Supports both:

        Movie [2D | Hindi]
        Movie [IMAX | Hindi]
        Movie | Hindi

    Canonical form:

        Movie | Hindi

    Format is intentionally discarded for PIC aggregation.
    """

    if not isinstance(raw_key, str):
        return raw_key

    key = raw_key.strip()

    # --------------------------------------------------------
    # Movie [Format | Language]
    # --------------------------------------------------------

    if key.endswith("]"):
        open_bracket = key.rfind("[")

        if open_bracket != -1:
            movie_name = key[:open_bracket].strip()
            inside = key[open_bracket + 1:-1].strip()

            if movie_name and inside:
                parts = [
                    part.strip()
                    for part in inside.split("|")
                    if part.strip()
                ]

                if parts:
                    language = parts[-1]
                    return f"{movie_name} | {language}"

    # --------------------------------------------------------
    # Movie | Language
    # --------------------------------------------------------

    return key


# ============================================================
# CHAIN DETECTION
# ============================================================

def detect_chain(show):
    """
    Prefer the actual compressed chain dictionary value.
    Fall back to venue name for older/legacy data.
    """

    chain_value = str(
        show.get("chain", "")
    ).strip().upper()

    if chain_value:
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
        adjusted = sold - blocked

        sold = max(
            0,
            round(adjusted)
        )

        gross = max(
            0,
            sold * avg_price
        )

    return sold, gross


# ============================================================
# BUILD REVERSE DICTIONARIES
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

    # New format is exactly 12 values.
    if len(arr) < 12:
        return None

    def resolve(name, index, default=""):
        value = arr[index]
        return reverse[name].get(
            value,
            default
        )

    # --------------------------------------------------------
    # New compressed format
    #
    # [cityId,
    #  stateId,
    #  venueId,
    #  chainId,
    #  showtimeId,
    #  audiId,
    #  totalSeats,
    #  available,
    #  sold,
    #  grossX100,
    #  occupancyX100,
    #  minsLeft]
    # --------------------------------------------------------

    total_seats = arr[6] or 0
    available = arr[7] or 0
    sold = arr[8] or 0
    gross_x100 = arr[9] or 0
    occupancy_x100 = arr[10] or 0
    mins_left = arr[11] or 0

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

        "minsLeft": mins_left,
    }


# ============================================================
# FETCH + DECOMPRESS ONE DATE
# ============================================================

def fetch_date(date, session):

    url = (
        f"{BASE_URL}/"
        f"{date}_Detailed.json"
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
            # 404 = source file genuinely does not exist.
            # This is NOT an error.
            # ------------------------------------------------

            if response.status_code == 404:
                return {
                    "status": "missing",
                    "data": None,
                }

            # ------------------------------------------------
            # Retry temporary server errors.
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
                    continue

                return {
                    "status": "error",
                    "data": None,
                    "error": last_error,
                }

            # ------------------------------------------------
            # Other HTTP errors.
            # ------------------------------------------------

            response.raise_for_status()

            # ------------------------------------------------
            # JSON decode
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
            # LEGACY / UNCOMPRESSED FALLBACK
            # =================================================

            if isinstance(data, dict):

                normalized = defaultdict(list)

                for raw_movie_key, shows in (
                    data.items()
                ):

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
                f"{attempt}/{RETRIES}: {exc}"
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

    for show in shows:

        if not isinstance(
            show,
            dict
        ):
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

        values = raw.get(chain)

        if (
            not values
            or values["seats"] == 0
        ):
            result.append(None)
            continue

        sold, gross = apply_discount(
            chain,
            values["sold"],
            values["gross"],
            values["seats"],
        )

        occupancy = round(
            (
                sold /
                values["seats"]
            ) * 100,
            2,
        )

        result.append(
            [
                values["shows"],
                sold,
                len(values["venues"]),
                round(gross, 2),
                occupancy,
            ]
        )

    return result


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
            f"⚠️ Could not read {filename}: {exc}"
        )

        return {}

    normalized = {}

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

        normalized.setdefault(
            movie,
            {}
        )

        for date_str, chain_data in (
            dates.items()
        ):

            # Already-array format
            if isinstance(
                chain_data,
                list
            ):
                normalized[movie][
                    date_str
                ] = chain_data

                continue

            # Old dictionary format
            if isinstance(
                chain_data,
                dict
            ):

                entry = []

                for chain in CHAIN_ORDER:

                    entry.append(
                        chain_data.get(
                            chain
                        )
                    )

                normalized[movie][
                    date_str
                ] = entry

    # Preserve lastUpdated separately.
    if "lastUpdated" in data:
        normalized["lastUpdated"] = (
            data["lastUpdated"]
        )

    return normalized


# ============================================================
# SAVE MONTH
# ============================================================

def save_month_file(
    filename,
    data
):

    path = os.path.join(
        OUTPUT_DIR,
        filename
    )

    ist = pytz.timezone(
        "Asia/Kolkata"
    )

    data["lastUpdated"] = (
        datetime.now(ist)
        .strftime(
            "%I:%M %p, %d %B %Y"
        )
    )

    # --------------------------------------------------------
    # Write with stable UTF-8 JSON.
    # --------------------------------------------------------

    with open(
        path,
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            data,
            file,
            indent=2,
            ensure_ascii=False
        )

    log(
        f"💾 Saved → {path}"
    )


# ============================================================
# MAIN
# ============================================================

def main():

    today = datetime.now().date()

    end_date = (
        today +
        timedelta(days=ADVANCE_DAYS)
    )

    # --------------------------------------------------------
    # Generate all dates.
    # --------------------------------------------------------

    all_dates = []

    current = START_DATE

    while current <= end_date:

        all_dates.append(
            current.strftime(
                "%Y-%m-%d"
            )
        )

        current += timedelta(days=1)

    log(
        f"📅 Date range: "
        f"{all_dates[0]} → {all_dates[-1]}"
    )

    log(
        f"📅 Total dates to check: "
        f"{len(all_dates)}"
    )

    # --------------------------------------------------------
    # Load existing monthly output files.
    # --------------------------------------------------------

    existing_data = {}

    first_year = START_DATE.year
    last_year = end_date.year

    for year in range(
        first_year,
        last_year + 1
    ):

        for month in range(
            1,
            13
        ):

            filename = (
                f"{year}-{month:02d}.json"
            )

            existing_data[
                filename
            ] = load_existing_month(
                filename
            )

    # --------------------------------------------------------
    # Find dates missing from output.
    #
    # Existing dates are NOT re-fetched.
    # This preserves your previous workflow.
    # --------------------------------------------------------

    dates_to_fetch = []

    for date_str in all_dates:

        year, month, _ = (
            date_str.split("-")
        )

        filename = (
            f"{year}-{month}.json"
        )

        month_data = existing_data.get(
            filename,
            {}
        )

        has_date = False

        for movie, dates in (
            month_data.items()
        ):

            if movie == "lastUpdated":
                continue

            if not isinstance(
                dates,
                dict
            ):
                continue

            if date_str in dates:
                has_date = True
                break

        if not has_date:
            dates_to_fetch.append(
                date_str
            )

    log(
        f"🆕 Dates to fetch: "
        f"{len(dates_to_fetch)}"
    )

    if not dates_to_fetch:

        log(
            "✅ All required dates "
            "already processed."
        )

        return

    # --------------------------------------------------------
    # FETCH
    # --------------------------------------------------------

    fetched_results = {}

    log(
        f"🚀 Fetching "
        f"{len(dates_to_fetch)} dates "
        f"with {MAX_WORKERS} workers..."
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

                # ------------------------------------------------
                # 404
                #
                # Do NOT call it an error.
                # Do NOT create blank output.
                # Do NOT delete existing data.
                # ------------------------------------------------

                if status == "missing":

                    log(
                        f"[{completed}/{total}] "
                        f"⚪ {date} – "
                        f"source file does not exist; "
                        f"keeping output unchanged"
                    )

                # ------------------------------------------------
                # Successful source
                # ------------------------------------------------

                elif status == "success":

                    data = result.get(
                        "data"
                    )

                    source_movies = result.get(
                        "source_movies",
                        0
                    )

                    parsed_movies = result.get(
                        "parsed_movies",
                        0
                    )

                    fetched_results[
                        date
                    ] = data

                    log(
                        f"[{completed}/{total}] "
                        f"✅ {date} – "
                        f"source movies: "
                        f"{source_movies}, "
                        f"parsed: "
                        f"{parsed_movies}"
                    )

                # ------------------------------------------------
                # Actual error
                # ------------------------------------------------

                else:

                    error = result.get(
                        "error",
                        "unknown error"
                    )

                    log(
                        f"[{completed}/{total}] "
                        f"❌ {date} – "
                        f"{error}"
                    )

            except Exception as exc:

                log(
                    f"[{completed}/{total}] "
                    f"❌ {date} – worker error: "
                    f"{exc}"
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
        f"{len(fetched_results)} "
        f"dates"
    )

    # --------------------------------------------------------
    # GROUP RESULTS BY MONTH
    # --------------------------------------------------------

    month_buckets = defaultdict(dict)

    for date_str, movie_data in (
        fetched_results.items()
    ):

        if not movie_data:
            # Existing output is preserved.
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

            # ----------------------------------------------------
            # Only write useful data.
            #
            # If source exists but has zero usable shows,
            # do not overwrite anything with empty data.
            # ----------------------------------------------------

            if stats and any(
                item is not None
                for item in stats
            ):

                month_buckets[
                    filename
                ].setdefault(
                    movie,
                    {}
                )[date_str] = stats

    # --------------------------------------------------------
    # MERGE + SAVE
    # --------------------------------------------------------

    files_saved = 0

    for filename, new_data in (
        month_buckets.items()
    ):

        month_data = existing_data.get(
            filename,
            {}
        )

        # Remove old timestamp before merge.
        month_data.pop(
            "lastUpdated",
            None
        )

        for movie, dates_dict in (
            new_data.items()
        ):

            month_data.setdefault(
                movie,
                {}
            )

            for date_str, stats in (
                dates_dict.items()
            ):

                month_data[
                    movie
                ][date_str] = stats

        save_month_file(
            filename,
            month_data
        )

        files_saved += 1

    log(
        f"💾 Monthly files saved: "
        f"{files_saved}"
    )

    log(
        "🎉 All done!"
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
