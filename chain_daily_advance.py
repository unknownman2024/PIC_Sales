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

# Day-wise files:
# Chain Daily Advance/day-wise/YYYY-MM-DD.json
DAYWISE_DIR = os.path.join(
    OUTPUT_DIR,
    "day-wise"
)

os.makedirs(
    OUTPUT_DIR,
    exist_ok=True
)

os.makedirs(
    DAYWISE_DIR,
    exist_ok=True
)

CHAIN_ORDER = [
    "PVR",
    "INOX",
    "CINEPOLIS"
]

BLOCK_RATES = {
    "PVR": 0.005,
    "CINEPOLIS": 0.0325,
    "INOX": 0.0,
}

MAX_WORKERS = 50
REQUEST_TIMEOUT = 30
RETRIES = 3

# Backfill start date (inclusive)
BACKFILL_START = datetime(
    2025,
    8,
    1
).date()

# ============================================================
# TIME / LAST UPDATED
# ============================================================

IST = pytz.timezone(
    "Asia/Kolkata"
)


def current_ist_last_updated():
    """
    Current IST time in exactly:

        10:06 PM, 19 August 2026
    """

    now = datetime.now(
        IST
    )

    return now.strftime(
        "%I:%M %p, %-d %B %Y"
    )


def source_last_updated(data):
    """
    Extract lastUpdated exactly as supplied
    by the source JSON.

    Example:
        "10:06 PM, 19 August 2026"

    No conversion or rewriting is performed.
    """

    if not isinstance(
        data,
        dict
    ):
        return None

    value = data.get(
        "lastUpdated"
    )

    if value is None:
        return None

    value = str(value).strip()

    return value or None


# ============================================================
# LOG
# ============================================================

def log(msg):
    print(
        f"➡ {msg}",
        flush=True
    )


# ============================================================
# MOVIE KEY NORMALIZATION
# ============================================================

def normalize_movie_key(raw_key):
    if not isinstance(
        raw_key,
        str
    ):
        return raw_key

    key = raw_key.strip()

    if key.endswith("]"):
        open_bracket = key.rfind(
            "["
        )

        if open_bracket != -1:
            movie_name = key[
                :open_bracket
            ].strip()

            inside = key[
                open_bracket + 1:-1
            ].strip()

            if movie_name and inside:
                parts = [
                    p.strip()
                    for p in inside.split("|")
                    if p.strip()
                ]

                if parts:
                    language = parts[-1]

                    return (
                        f"{movie_name} | {language}"
                    )

    return key


# ============================================================
# CHAIN DETECTION
# ============================================================

def detect_chain(show):
    """
    Determine chain ONLY from the venue name.

    Do NOT use the compressed JSON "chain" field.

    Matching behavior:

        venue.upper()
        -> check whether chain keyword exists in venue
        -> first matching chain wins
    """

    if not isinstance(
        show,
        dict
    ):
        return None

    venue = str(
        show.get(
            "venue",
            ""
        )
    ).strip()

    if not venue:
        return None

    venue_upper = venue.upper()

    for chain in CHAIN_ORDER:
        if chain.upper() in venue_upper:
            return chain

    return None


# ============================================================
# DISCOUNT
# ============================================================

def apply_discount(
    chain,
    sold,
    gross,
    seats
):
    rate = BLOCK_RATES.get(
        chain,
        0
    )

    if sold > 0 and rate > 0:
        avg_price = (
            gross / sold
            if sold
            else 0
        )

        blocked = seats * rate

        sold = max(
            0,
            round(
                sold - blocked
            )
        )

        gross = max(
            0,
            sold * avg_price
        )

    return sold, gross


# ============================================================
# BUILD REVERSE DICTS
# ============================================================

def build_reverse_dicts(
    dicts
):
    reverse = {}

    for name in (
        "cities",
        "states",
        "venues",
        "chains",
        "showtimes",
        "audis"
    ):
        source = dicts.get(
            name,
            {}
        )

        reverse[name] = {
            value: key
            for key, value
            in source.items()
        }

    return reverse


# ============================================================
# DECOMPRESS ONE SHOW
# ============================================================

def decompress_show(
    arr,
    reverse
):
    if (
        not isinstance(
            arr,
            list
        )
        or len(arr) < 12
    ):
        return None

    def resolve(
        name,
        index,
        default=""
    ):
        return reverse[name].get(
            arr[index],
            default
        )

    total_seats = (
        arr[6] or 0
    )

    available = (
        arr[7] or 0
    )

    sold = (
        arr[8] or 0
    )

    gross_x100 = (
        arr[9] or 0
    )

    occupancy_x100 = (
        arr[10] or 0
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
        "totalSeats": total_seats,
        "available": available,
        "sold": sold,
        "gross": (
            gross_x100 / 100.0
        ),
        "occupancy": (
            f"{occupancy_x100 / 100:.2f}%"
        ),
        "minsLeft": (
            arr[11] or 0
        ),
    }


# ============================================================
# FETCH ONE DATE
# ============================================================

def fetch_date(
    date_str,
    session
):
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
                    "User-Agent":
                        "Mozilla/5.0 "
                        "(Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 "
                        "(KHTML, like Gecko) "
                        "Chrome/151 "
                        "Safari/537.36",
                    "Accept":
                        "application/json,"
                        "text/plain,*/*",
                },
            )

            if response.status_code == 404:
                return {
                    "status": "missing",
                    "data": None,
                    "lastUpdated": None
                }

            if response.status_code in {
                429,
                500,
                502,
                503,
                504
            }:
                last_error = (
                    f"HTTP "
                    f"{response.status_code}"
                )

                if attempt < RETRIES:
                    time.sleep(
                        attempt
                    )
                    continue

                return {
                    "status": "error",
                    "data": None,
                    "lastUpdated": None,
                    "error": last_error
                }

            response.raise_for_status()

            try:
                data = response.json()

            except Exception as exc:
                return {
                    "status": "error",
                    "data": None,
                    "lastUpdated": None,
                    "error":
                        f"JSON decode error: {exc}"
                }

            # ----------------------------------------------------
            # SOURCE LAST UPDATED
            # ----------------------------------------------------

            source_updated = (
                source_last_updated(
                    data
                )
            )

            # ----------------------------------------------------
            # New compressed format
            # ----------------------------------------------------

            if (
                isinstance(
                    data,
                    dict
                )
                and isinstance(
                    data.get(
                        "dicts"
                    ),
                    dict
                )
                and isinstance(
                    data.get(
                        "movies"
                    ),
                    dict
                )
            ):
                dicts = data[
                    "dicts"
                ]

                movies = data[
                    "movies"
                ]

                reverse = (
                    build_reverse_dicts(
                        dicts
                    )
                )

                decompressed = (
                    defaultdict(list)
                )

                for (
                    raw_movie_key,
                    compressed_list
                ) in movies.items():

                    if not isinstance(
                        compressed_list,
                        list
                    ):
                        continue

                    movie_key = (
                        normalize_movie_key(
                            raw_movie_key
                        )
                    )

                    for arr in (
                        compressed_list
                    ):
                        show = (
                            decompress_show(
                                arr,
                                reverse
                            )
                        )

                        if show is not None:
                            decompressed[
                                movie_key
                            ].append(
                                show
                            )

                return {
                    "status": "success",
                    "data": dict(
                        decompressed
                    ),
                    "lastUpdated": (
                        source_updated
                    ),
                    "source_movies": len(
                        movies
                    ),
                    "parsed_movies": len(
                        decompressed
                    ),
                }

            # ----------------------------------------------------
            # Legacy fallback
            # ----------------------------------------------------

            if isinstance(
                data,
                dict
            ):
                normalized = (
                    defaultdict(list)
                )

                for (
                    raw_movie_key,
                    shows
                ) in data.items():

                    if raw_movie_key in {
                        "date",
                        "lastUpdated",
                        "dicts",
                        "movies"
                    }:
                        continue

                    if not isinstance(
                        shows,
                        list
                    ):
                        continue

                    movie_key = (
                        normalize_movie_key(
                            raw_movie_key
                        )
                    )

                    normalized[
                        movie_key
                    ].extend(
                        shows
                    )

                return {
                    "status": "success",
                    "data": dict(
                        normalized
                    ),
                    "lastUpdated": (
                        source_updated
                    ),
                    "source_movies": len(
                        normalized
                    ),
                    "parsed_movies": len(
                        normalized
                    ),
                }

            return {
                "status": "error",
                "data": None,
                "lastUpdated": None,
                "error":
                    "Unknown JSON structure"
            }

        except requests.exceptions.Timeout:
            last_error = (
                f"timeout on attempt "
                f"{attempt}/{RETRIES}"
            )

            if attempt == RETRIES:
                return {
                    "status": "error",
                    "data": None,
                    "lastUpdated": None,
                    "error": last_error
                }

        except requests.exceptions.RequestException as exc:
            last_error = str(
                exc
            )

            if attempt == RETRIES:
                return {
                    "status": "error",
                    "data": None,
                    "lastUpdated": None,
                    "error": last_error
                }

        except Exception as exc:
            return {
                "status": "error",
                "data": None,
                "lastUpdated": None,
                "error":
                    f"{type(exc).__name__}: {exc}"
            }

    return {
        "status": "error",
        "data": None,
        "lastUpdated": None,
        "error":
            last_error
            or "Unknown error"
    }


# ============================================================
# PROCESS DAY
# ============================================================

def process_day(
    shows
):
    raw = defaultdict(
        lambda: {
            "sold": 0,
            "gross": 0,
            "seats": 0,
            "shows": 0,
            "venues": set()
        }
    )

    for show in shows:

        if not isinstance(
            show,
            dict
        ):
            continue

        chain = detect_chain(
            show
        )

        if not chain:
            continue

        raw[chain][
            "shows"
        ] += 1

        raw[chain][
            "sold"
        ] += (
            show.get(
                "sold",
                0
            )
            or 0
        )

        raw[chain][
            "gross"
        ] += (
            show.get(
                "gross",
                0
            )
            or 0
        )

        raw[chain][
            "seats"
        ] += (
            show.get(
                "totalSeats",
                0
            )
            or 0
        )

        venue = str(
            show.get(
                "venue",
                ""
            )
        ).strip()

        if venue:
            raw[chain][
                "venues"
            ].add(
                venue
            )

    result = []

    for chain in CHAIN_ORDER:

        value = raw.get(
            chain
        )

        if (
            not value
            or value["seats"] == 0
        ):
            result.append(
                None
            )
            continue

        sold, gross = (
            apply_discount(
                chain,
                value["sold"],
                value["gross"],
                value["seats"]
            )
        )

        occupancy = round(
            (
                sold /
                value["seats"]
            ) * 100,
            2
        )

        result.append([
            value["shows"],
            sold,
            len(
                value["venues"]
            ),
            round(
                gross,
                2
            ),
            occupancy
        ])

    return result


# ============================================================
# MONTH HELPERS
# ============================================================

def last_day_of_month(
    year,
    month
):
    _, last = calendar.monthrange(
        year,
        month
    )

    return datetime(
        year,
        month,
        last
    ).date()


def month_range(
    year,
    month
):
    start = datetime(
        year,
        month,
        1
    ).date()

    end = last_day_of_month(
        year,
        month
    )

    while start <= end:
        yield start.strftime(
            "%Y-%m-%d"
        )

        start += timedelta(
            days=1
        )


def month_filename(
    year,
    month
):
    return (
        f"{year}-{month:02d}.json"
    )


def daywise_filename(
    date_str
):
    return (
        f"{date_str}.json"
    )


# ============================================================
# SAVE ONE DAY-WISE FILE
# ============================================================

def save_daywise(
    date_str,
    movie_data,
    source_updated
):
    """
    Output:

        Chain Daily Advance/
            day-wise/
                YYYY-MM-DD.json

    Structure:

        {
            "date": "2026-08-20",
            "lastUpdated": "10:06 PM, 19 August 2026",
            "Movie | Language": [
                [shows, sold, venues, gross, occupancy]
            ]
        }

    lastUpdated comes directly from
    the source Detailed JSON.
    """

    sorted_output = {}

    for raw_movie in sorted(
        movie_data.keys(),
        key=lambda x: x.lower()
    ):

        shows = movie_data.get(
            raw_movie
        )

        if not isinstance(
            shows,
            list
        ):
            continue

        movie = (
            normalize_movie_key(
                raw_movie
            )
        )

        stats = process_day(
            shows
        )

        if not stats:
            continue

        if not any(
            item is not None
            for item in stats
        ):
            continue

        sorted_output[
            movie
        ] = stats

    # --------------------------------------------------------
    # Metadata FIRST
    # --------------------------------------------------------

    final_output = {
        "date": date_str,
        "lastUpdated": (
            source_updated
        ),
        **sorted_output
    }

    filename = (
        daywise_filename(
            date_str
        )
    )

    path = os.path.join(
        DAYWISE_DIR,
        filename
    )

    output_json = json.dumps(
        final_output,
        ensure_ascii=False,
        separators=(",", ":")
    )

    with open(
        path,
        "w",
        encoding="utf-8"
    ) as f:
        f.write(
            output_json
        )

    log(
        f"✅ Saved day-wise "
        f"{filename} "
        f"({len(sorted_output)} movies)"
    )


# ============================================================
# MAIN
# ============================================================

def main():

    # Current date in IST
    today = datetime.now(
        IST
    ).date()

    # Current monthly file's timestamp.
    # Generate ONCE so every monthly file written
    # during this run has the same timestamp.
    monthly_last_updated = (
        current_ist_last_updated()
    )

    prev_year, prev_month = (
        (
            today.year,
            today.month - 1
        )
        if today.month > 1
        else (
            today.year - 1,
            12
        )
    )

    curr_year = today.year
    curr_month = today.month

    log(
        f"🔄 Rebuilding previous month: "
        f"{prev_year}-{prev_month:02d}"
    )

    log(
        f"🔄 Rebuilding current month:  "
        f"{curr_year}-{curr_month:02d}"
    )

    # ------------------------------------------------------------
    # Determine months to fetch
    # ------------------------------------------------------------

    months_to_fetch = set()

    # Always fetch previous month
    months_to_fetch.add(
        (
            prev_year,
            prev_month
        )
    )

    # Always fetch current month
    months_to_fetch.add(
        (
            curr_year,
            curr_month
        )
    )

    # ------------------------------------------------------------
    # Backfill missing older months
    # ------------------------------------------------------------

    current_month_start = datetime(
        curr_year,
        curr_month,
        1
    ).date()

    d = BACKFILL_START

    while d < current_month_start:

        y = d.year
        m = d.month

        if (
            y,
            m
        ) not in months_to_fetch:

            filename = month_filename(
                y,
                m
            )

            month_path = os.path.join(
                OUTPUT_DIR,
                filename
            )

            if not os.path.exists(
                month_path
            ):
                months_to_fetch.add(
                    (
                        y,
                        m
                    )
                )

                log(
                    f"📂 Missing "
                    f"{filename} "
                    f"→ will backfill"
                )

        if m == 12:
            d = datetime(
                y + 1,
                1,
                1
            ).date()
        else:
            d = datetime(
                y,
                m + 1,
                1
            ).date()

    # ------------------------------------------------------------
    # Build date list
    # ------------------------------------------------------------

    dates_to_fetch = set()

    for (
        year,
        month
    ) in months_to_fetch:

        if (
            year == curr_year
            and month == curr_month
        ):

            # Current month:
            # 1st -> today + 5 days

            start = datetime(
                year,
                month,
                1
            ).date()

            end = (
                today
                + timedelta(
                    days=5
                )
            )

            d = start

            while d <= end:

                dates_to_fetch.add(
                    d.strftime(
                        "%Y-%m-%d"
                    )
                )

                d += timedelta(
                    days=1
                )

        else:

            # Full month

            for date_str in month_range(
                year,
                month
            ):
                dates_to_fetch.add(
                    date_str
                )

    log(
        f"📅 Total dates to check: "
        f"{len(dates_to_fetch)}"
    )

    # ------------------------------------------------------------
    # Fetch concurrently
    # ------------------------------------------------------------

    # Structure:
    #
    # fetched_results[date] = {
    #     "data": {...},
    #     "lastUpdated": "..."
    # }
    #
    fetched_results = {}

    def fetch_wrapper(
        date_str
    ):
        with requests.Session() as session:
            return (
                date_str,
                fetch_date(
                    date_str,
                    session
                )
            )

    total = len(
        dates_to_fetch
    )

    completed = 0

    log(
        f"🚀 Fetching with "
        f"{MAX_WORKERS} workers..."
    )

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        futures = {
            executor.submit(
                fetch_wrapper,
                date_str
            ): date_str
            for date_str
            in dates_to_fetch
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
                        f"⚪ {date} "
                        f"– source missing"
                    )

                elif status == "success":

                    fetched_results[
                        date
                    ] = {
                        "data": result.get(
                            "data",
                            {}
                        ),
                        "lastUpdated": (
                            result.get(
                                "lastUpdated"
                            )
                        )
                    }

                    log(
                        f"[{completed}/{total}] "
                        f"✅ {date} "
                        f"– "
                        f"{result.get('parsed_movies', 0)} "
                        f"movies"
                    )

                else:

                    log(
                        f"[{completed}/{total}] "
                        f"❌ {date} "
                        f"– "
                        f"{result.get('error', 'unknown')}"
                    )

            except Exception as exc:

                log(
                    f"[{completed}/{total}] "
                    f"❌ {date_str} "
                    f"– worker error: {exc}"
                )

            if (
                completed % 10 == 0
                or completed == total
            ):

                log(
                    f"⏳ Progress: "
                    f"{completed}/{total} "
                    f"("
                    f"{100 * completed // total}%"
                    f")"
                )

    log(
        f"✅ Successfully fetched "
        f"{len(fetched_results)} dates"
    )

    # ------------------------------------------------------------
    # Group fetched data by month
    # ------------------------------------------------------------

    month_groups = (
        defaultdict(list)
    )

    for date_str in (
        fetched_results
    ):

        y, m, _ = (
            date_str.split("-")
        )

        month_groups[
            (
                int(y),
                int(m)
            )
        ].append(
            date_str
        )

    if not month_groups:

        log(
            "⚠️ No data fetched "
            "– nothing to save."
        )

        return

    # ============================================================
    # SAVE DAY-WISE FILES (fresh overwrite)
    # ============================================================

    log(
        "📁 Saving day-wise files..."
    )

    for date_str in sorted(
        fetched_results.keys()
    ):

        result = fetched_results.get(
            date_str
        )

        if not result:
            continue

        movie_data = result.get(
            "data",
            {}
        )

        if not movie_data:
            continue

        save_daywise(
            date_str,
            movie_data,
            result.get(
                "lastUpdated"
            )
        )

    # ============================================================
    # SAVE MONTHLY FILES – FORCE REBUILD (no merging)
    # ============================================================

    for (
        year,
        month
    ), date_list in month_groups.items():

        filename = month_filename(
            year,
            month
        )

        log(
            f"💾 Rebuilding {filename} from scratch..."
        )

        # --------------------------------------------------------
        # Build month data only from fetched dates
        # --------------------------------------------------------

        month_data = defaultdict(
            dict
        )

        for date_str in date_list:

            result = fetched_results.get(
                date_str
            )

            if not result:
                continue

            movie_data = result.get(
                "data",
                {}
            )

            if not movie_data:
                continue

            for (
                raw_movie,
                shows
            ) in movie_data.items():

                if not isinstance(
                    shows,
                    list
                ):
                    continue

                movie = (
                    normalize_movie_key(
                        raw_movie
                    )
                )

                stats = process_day(
                    shows
                )

                if (
                    stats
                    and any(
                        item is not None
                        for item in stats
                    )
                ):
                    month_data[
                        movie
                    ][
                        date_str
                    ] = stats

        # --------------------------------------------------------
        # Sort movies and dates
        # --------------------------------------------------------

        sorted_movies = {}

        for movie in sorted(
            month_data.keys(),
            key=lambda x: x.lower()
        ):

            sorted_movies[
                movie
            ] = {}

            for date_str in sorted(
                month_data[
                    movie
                ].keys()
            ):

                sorted_movies[
                    movie
                ][
                    date_str
                ] = month_data[
                    movie
                ][
                    date_str
                ]

        # --------------------------------------------------------
        # Monthly metadata – current IST time
        # --------------------------------------------------------

        final_output = {
            "lastUpdated":
                monthly_last_updated,
            **sorted_movies
        }

        # --------------------------------------------------------
        # Write monthly JSON
        # --------------------------------------------------------

        output_json = json.dumps(
            final_output,
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
        ) as f:

            f.write(
                output_json
            )

        log(
            f"✅ Saved {filename} "
            f"({len(sorted_movies)} movies)"
        )

    log(
        "🎉 Rebuild and backfill complete!"
    )


# ============================================================
# ENTRY
# ============================================================

if __name__ == "__main__":
    main()
