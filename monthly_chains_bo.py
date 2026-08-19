#!/usr/bin/env python3

import json
import os
import time
import requests
import pytz

from datetime import datetime, timedelta
from collections import defaultdict


# ============================================================
# CONFIG
# ============================================================

BASE_URL = "https://districtdata2026.pages.dev/boxoffice"
OUTPUT_DIR = "Chain Daily Breakdown"

os.makedirs(OUTPUT_DIR, exist_ok=True)

CHAIN_LIST = [
    "PVR",
    "INOX",
    "Cinepolis",
    "Movietime Cinemas",
    "Wave Cinemas",
    "Miraj Cinemas",
    "Rajhans Cinemas",
    "Asian Mukta",
    "MovieMax",
    "Mythri Cinemas",
    "Maxus Cinemas",
]

START_YEAR = 2025
START_MONTH = 9

REQUEST_TIMEOUT = 30
RETRIES = 3


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

    Canonical result:

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
                    x.strip()
                    for x in inside.split("|")
                    if x.strip()
                ]

                if parts:
                    language = parts[-1]
                    return f"{movie_name} | {language}"

    return key


# ============================================================
# CHAIN DETECTION
# ============================================================

def detect_chain(show):
    """
    Prefer the chain value decoded from the compressed source.
    Fall back to venue name.
    """

    chain_value = str(
        show.get("chain", "")
    ).strip().lower()

    if chain_value:

        for chain in CHAIN_LIST:
            if chain.lower() in chain_value:
                return chain

    venue = str(
        show.get("venue", "")
    ).strip()

    if not venue:
        return None

    venue_lower = venue.lower()

    for chain in CHAIN_LIST:
        if chain.lower() in venue_lower:
            return chain

    return None


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
# DECOMPRESS SHOW
# ============================================================

def decompress_show(arr, reverse):

    if not isinstance(arr, list):
        return None

    # New supercompressed format has 12 values.
    if len(arr) < 12:
        return None

    def resolve(name, index, default=""):
        return reverse[name].get(
            arr[index],
            default
        )

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

        "totalSeats": arr[6] or 0,
        "available": arr[7] or 0,
        "sold": arr[8] or 0,

        # Source stores gross × 100.
        "gross": (arr[9] or 0) / 100.0,

        # Source stores occupancy × 100.
        "occupancy": f"{(arr[10] or 0) / 100:.2f}%",

        "minsLeft": arr[11] or 0,
    }


# ============================================================
# FETCH + DECOMPRESS
# ============================================================

def fetch(date_str):

    url = (
        f"{BASE_URL}/"
        f"{date_str}_Detailed.json"
    )

    for attempt in range(1, RETRIES + 1):

        try:

            response = requests.get(
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
            # File genuinely doesn't exist.
            # ------------------------------------------------

            if response.status_code == 404:

                log(
                    f"⚪ {date_str} – "
                    f"source file does not exist"
                )

                return None

            # ------------------------------------------------
            # Retry temporary errors.
            # ------------------------------------------------

            if response.status_code in {
                429,
                500,
                502,
                503,
                504,
            }:

                if attempt < RETRIES:
                    time.sleep(attempt)
                    continue

                log(
                    f"❌ {date_str} – "
                    f"HTTP {response.status_code}"
                )

                return None

            response.raise_for_status()

            try:
                data = response.json()

            except Exception as exc:

                log(
                    f"❌ {date_str} – "
                    f"JSON decode error: {exc}"
                )

                return None

            # =================================================
            # NEW SUPERCOMPRESSED FORMAT
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

                reverse = build_reverse_dicts(
                    data["dicts"]
                )

                decompressed = defaultdict(list)

                for raw_movie_key, compressed_shows in (
                    data["movies"].items()
                ):

                    if not isinstance(
                        compressed_shows,
                        list
                    ):
                        continue

                    movie_key = normalize_movie_key(
                        raw_movie_key
                    )

                    for arr in compressed_shows:

                        show = decompress_show(
                            arr,
                            reverse
                        )

                        if show is not None:
                            decompressed[
                                movie_key
                            ].append(show)

                log(
                    f"📥 Loaded {date_str} – "
                    f"{len(decompressed)} movies"
                )

                return dict(decompressed)

            # =================================================
            # LEGACY FALLBACK
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

                log(
                    f"📥 Loaded legacy {date_str} – "
                    f"{len(normalized)} movies"
                )

                return dict(normalized)

            log(
                f"⚠️ {date_str} – "
                f"unknown JSON structure"
            )

            return None

        except requests.exceptions.Timeout:

            if attempt < RETRIES:
                time.sleep(attempt)
                continue

            log(
                f"⏱️ {date_str} – "
                f"request timeout"
            )

        except requests.exceptions.RequestException as exc:

            if attempt < RETRIES:
                time.sleep(attempt)
                continue

            log(
                f"❌ {date_str} – "
                f"request error: {exc}"
            )

        except Exception as exc:

            log(
                f"❌ {date_str} – "
                f"{type(exc).__name__}: {exc}"
            )

            return None

    return None


# ============================================================
# PROCESS MOVIE SHOWS
# ============================================================

def process(shows):

    chain_data = defaultdict(
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

        venue = str(
            show.get("venue", "")
        ).strip()

        chain = detect_chain(show)

        if not chain:
            continue

        chain_data[chain]["shows"] += 1

        chain_data[chain]["sold"] += (
            show.get("sold", 0) or 0
        )

        chain_data[chain]["gross"] += (
            show.get("gross", 0) or 0
        )

        chain_data[chain]["seats"] += (
            show.get("totalSeats", 0) or 0
        )

        if venue:
            chain_data[chain][
                "venues"
            ].add(venue)

    result = {}

    for chain, value in chain_data.items():

        seats = value["seats"]
        sold = value["sold"]
        gross = value["gross"]

        occ = (
            round(
                (sold / seats) * 100,
                2
            )
            if seats
            else 0
        )

        result[chain] = [
            value["shows"],
            sold,
            len(value["venues"]),
            round(gross, 2),
            occ,
        ]

    return result


# ============================================================
# SAVE
# ============================================================

def save(filepath, data):

    # --------------------------------------------------------
    # Minified / single-line output.
    # --------------------------------------------------------

    output = json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":")
    )

    with open(
        filepath,
        "w",
        encoding="utf-8"
    ) as f:

        f.write(output)

    log(
        f"💾 Saved → {filepath}"
    )


# ============================================================
# LAST DAY OF MONTH
# ============================================================

def get_last_day(year, month):

    if month == 12:
        next_month = datetime(
            year + 1,
            1,
            1
        ).date()
    else:
        next_month = datetime(
            year,
            month + 1,
            1
        ).date()

    return next_month - timedelta(days=1)


# ============================================================
# MONTH PROCESSOR
# ============================================================

def process_month(
    year,
    month,
    allow_update
):

    filename = (
        f"{year}-{month:02d}.json"
    )

    path = os.path.join(
        OUTPUT_DIR,
        filename
    )

    ist = pytz.timezone(
        "Asia/Kolkata"
    )

    today = datetime.now(
        ist
    ).date()

    month_start = datetime(
        year,
        month,
        1
    ).date()

    # --------------------------------------------------------
    # Future month protection.
    # --------------------------------------------------------

    if month_start > today:

        log(
            f"⛔ Blocked future month → "
            f"{filename}"
        )

        return

    # --------------------------------------------------------
    # Past month:
    # Existing file remains locked.
    # --------------------------------------------------------

    if (
        os.path.exists(path)
        and not allow_update
    ):

        log(
            f"⏭ Skipping locked month → "
            f"{filename}"
        )

        return

    # --------------------------------------------------------
    # Current month = full refresh.
    # --------------------------------------------------------

    log(
        f"🔄 Full refresh → "
        f"{filename}"
    )

    month_data = {}

    # Current month only up to today.
    # Past months get their full calendar.
    if allow_update:
        end_date = today
    else:
        end_date = get_last_day(
            year,
            month
        )

    current = month_start

    while current <= end_date:

        date_str = current.strftime(
            "%Y-%m-%d"
        )

        daily = fetch(
            date_str
        )

        if daily:

            for raw_movie, shows in (
                daily.items()
            ):

                if not isinstance(
                    shows,
                    list
                ):
                    continue

                movie = normalize_movie_key(
                    raw_movie
                )

                stats = process(
                    shows
                )

                if not stats:
                    continue

                month_data.setdefault(
                    movie,
                    {}
                )[date_str] = stats

            log(
                f"✔ Updated → "
                f"{date_str}"
            )

        current += timedelta(
            days=1
        )

    # --------------------------------------------------------
    # Stable sorting.
    # --------------------------------------------------------

    sorted_data = {}

    for movie in sorted(
        month_data.keys(),
        key=lambda x: x.lower()
    ):

        sorted_data[movie] = {}

        for date_str in sorted(
            month_data[movie].keys()
        ):

            sorted_data[movie][
                date_str
            ] = month_data[movie][
                date_str
            ]

    save(
        path,
        sorted_data
    )


# ============================================================
# MAIN
# ============================================================

def main():

    ist = pytz.timezone(
        "Asia/Kolkata"
    )

    today = datetime.now(
        ist
    ).date()

    year = START_YEAR
    month = START_MONTH

    while True:

        month_start = datetime(
            year,
            month,
            1
        ).date()

        # ----------------------------------------------------
        # Stop at future month.
        # ----------------------------------------------------

        if month_start > today:

            log(
                f"🛑 Stop at future month → "
                f"{year}-{month:02d}"
            )

            break

        is_current = (
            year == today.year
            and month == today.month
        )

        process_month(
            year,
            month,
            allow_update=is_current
        )

        # ----------------------------------------------------
        # Next month.
        # ----------------------------------------------------

        month += 1

        if month > 12:
            month = 1
            year += 1


# ============================================================
# ENTRY
# ============================================================

if __name__ == "__main__":
    main()
