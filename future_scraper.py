import asyncio
import json
import os
import sys
import pycountry
from datetime import datetime, timedelta
from curl_cffi.requests import AsyncSession

FM_BASE = "https://www.fotmob.com/api/data"

# league_id -> country codes where that league has had TV listings before.
# Fotmob only answers TV per (match, country), so querying every country for
# every match is ~250x too many requests; this map keeps it to pairs that
# can actually return something. It is grown by every run (see below).
TV_PAIRS_FILE = "tv_pairs.json"

FIRST_OFFSET = -1
LAST_OFFSET = 30
MATCH_CONCURRENCY = 10
TV_CONCURRENCY = 12
TV_TIMEOUT = 30
TV_RETRIES = 4
# Fotmob publishes TV listings roughly ten days ahead; asking further out
# only burns requests on empty answers.
TV_HORIZON_DAYS = 10
# Discovery: each run also asks a rotating 1/EXPLORE_SLICES of the known
# countries about every match in the next EXPLORE_DAYS days, so countries that
# start showing a league get picked up within a week.
EXPLORE_DAYS = 3
EXPLORE_SLICES = 7
NEAR_HORIZON_DAYS = 7
ENABLE_TV_CHANNELS = True
ENABLE_VENUE = True


def cleanup_old_files():
    if not os.path.exists("date"):
        os.makedirs("date")
        return

    keep_files = set()

    for offset in range(FIRST_OFFSET, LAST_OFFSET + 1):
        d =datetime.now() + timedelta(days=offset)

        keep_files.add(
            os.path.join(
                d.strftime("%Y"),
                d.strftime("%Y%m%d") + ".json"
            )
        )

    for root, dirs, files in os.walk("date"):
        for file in files:
            if not file.endswith(".json"):
                continue

            rel_path = os.path.relpath(
                os.path.join(root, file),
                "date"
            )

            if rel_path not in keep_files:
                try:
                    os.remove(os.path.join(root, file))
                    print(f"Deleted old file: {rel_path}")
                except Exception as e:
                    print(f"Failed deleting {rel_path}: {e}")


async def fetch_json(session, url, timeout=15):
    try:
        r = await session.get(url, impersonate="chrome120", timeout=timeout)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


async def get_fotmob_schedule(session, date_str):
    url = (
        f"{FM_BASE}/matches"
        f"?date={date_str}"
        f"&timezone=Asia%2FTokyo"
        f"&ccode3=JPN"
        f"&includeNextDayLateNight=true"
    )

    data = await fetch_json(session, url)
    if not data:
        return []

    matches = []
    for league in data.get("leagues", []):
        league_name = league.get("name", "Unknown")

        for m in league.get("matches", []):
            utc_str = m.get("status", {}).get("utcTime") or ""
            time_ts = m.get("timeTS")

            timestamp = None
            if utc_str:
                try:
                    dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
                    timestamp = int(dt.timestamp())
                except Exception:
                    pass

            if timestamp is None and time_ts:
                timestamp = int(time_ts / 1000)

            matches.append({
                "match_id": m["id"],
                "kickoff": timestamp,
                "home_name": m["home"]["name"],
                "home_id": m["home"]["id"],
                "away_name": m["away"]["name"],
                "away_id": m["away"]["id"],
                "league": league_name,
                "league_id": league.get("primaryId") or league.get("id", 0),
            })

    return matches


async def get_fotmob_venue(session, match_id):
    url = f"{FM_BASE}/matchDetails?matchId={match_id}"
    data = await fetch_json(session, url)
    if not data:
        return "TBA"

    ib = (data.get("content") or {}).get("matchFacts", {}).get("infoBox", {})
    stadium = ib.get("Stadium", {}) if ib else {}
    return stadium.get("name", "TBA") if stadium else "TBA"


def country_name(country_code):
    country = pycountry.countries.get(alpha_2=country_code)
    if not country:
        return country_code
    return getattr(country, "common_name", None) or country.name


def load_tv_pairs():
    try:
        with open(TV_PAIRS_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        raw = {}
    return {int(k): set(v) for k, v in raw.items()}


def save_tv_pairs(pairs):
    out = {str(k): sorted(pairs[k]) for k in sorted(pairs)}
    with open(TV_PAIRS_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
        f.write("\n")


def parse_channels(data):
    """/tvlisting answers {"name": "Ch A / Ch B", "affiliates": [...]};
    an empty name means no broadcast in that country."""
    channels = set()

    for part in (data.get("name") or "").split(" / "):
        part = part.strip()
        if part:
            channels.add(part)

    for aff in data.get("affiliates") or []:
        name = aff.get("name") if isinstance(aff, dict) else aff
        if isinstance(name, str) and name.strip():
            channels.add(name.strip())

    return channels


class TvStats:
    def __init__(self):
        self.requests = 0
        self.failed = 0


async def fetch_match_country_tv(session, match_id, country_code,
                                 semaphore, stats):
    url = (f"{FM_BASE}/tvlisting"
           f"?matchId={match_id}&countryCode={country_code}")

    async with semaphore:
        for attempt in range(TV_RETRIES):
            stats.requests += 1
            try:
                r = await session.get(url, impersonate="chrome120",
                                      timeout=TV_TIMEOUT)
                if r.status_code == 200:
                    return match_id, country_code, parse_channels(r.json())
                if r.status_code not in (429, 500, 502, 503, 504):
                    break
            except Exception:
                pass
            await asyncio.sleep(2 ** attempt)

    stats.failed += 1
    return match_id, country_code, set()


async def build_tv_index(session, schedules, pairs):
    """Ask /tvlisting for each (match, country) pair worth asking about."""
    known_countries = sorted(set().union(*pairs.values())) if pairs else [
        c.alpha_2 for c in pycountry.countries
    ]

    slice_idx = datetime.now().toordinal() % EXPLORE_SLICES
    explore_countries = set(known_countries[slice_idx::EXPLORE_SLICES])

    jobs = set()
    for offset, matches in schedules.items():
        if offset > TV_HORIZON_DAYS:
            continue
        for m in matches:
            countries = pairs.get(m["league_id"])
            if not countries:
                # league never seen with TV: try every known country, but only
                # close to kickoff; hits are remembered in tv_pairs.json
                if offset > EXPLORE_DAYS:
                    continue
                countries = known_countries
            elif offset <= EXPLORE_DAYS:
                countries = countries | explore_countries
            for cc in countries:
                jobs.add((m["match_id"], cc))

    print(f"TV listings: querying {len(jobs)} match/country pairs")

    semaphore = asyncio.Semaphore(TV_CONCURRENCY)
    stats = TvStats()
    results = await asyncio.gather(*[
        fetch_match_country_tv(session, mid, cc, semaphore, stats)
        for mid, cc in jobs
    ])

    league_of = {
        m["match_id"]: m["league_id"]
        for matches in schedules.values() for m in matches
    }

    tv_index = {}
    countries_hit = set()
    new_pairs = 0

    for match_id, country_code, channels in results:
        if not channels:
            continue

        countries_hit.add(country_code)
        tv_index.setdefault(match_id, []).append({
            "country": country_name(country_code),
            "country_code": country_code,
            "channels": sorted(channels),
        })

        league_countries = pairs.setdefault(league_of[match_id], set())
        if country_code not in league_countries:
            league_countries.add(country_code)
            new_pairs += 1

    for entries in tv_index.values():
        entries.sort(key=lambda x: x["country"])

    print(
        f"TV listings: {len(tv_index)} matches from {len(countries_hit)} "
        f"countries ({stats.requests} requests, {stats.failed} failed, "
        f"{new_pairs} new league/country pairs)"
    )

    return tv_index, stats


async def process_one_match(session, m, tv_index, index, total):
    match_id = m["match_id"]
    print(f"  [{index}/{total}] Match {match_id}: {m['home_name']} vs {m['away_name']}")

    venue = await get_fotmob_venue(session, match_id) if ENABLE_VENUE else "TBA"
    tv_channels = tv_index.get(match_id, []) if ENABLE_TV_CHANNELS else []

    return {
        "match_id": match_id,
        "kickoff": m["kickoff"],
        "fixture": f"{m['home_name']} vs {m['away_name']}",
        "league_id": m["league_id"],
        "league": m["league"],
        "venue": venue,
        "tv_channels": tv_channels,
    }


def day_date(days_offset):
    return datetime.now() + timedelta(days=days_offset)


async def process_day(session, days_offset, matches, tv_index):
    target_date = day_date(days_offset)
    date_query = target_date.strftime("%Y-%m-%d")
    file_name = target_date.strftime("%Y%m%d") + ".json"

    print(f"Processing {date_query}")

    if not matches:
        print(f"No matches found: {date_query}")
        return {"offset": days_offset, "date": date_query,
                "matches": 0, "with_tv": 0}

    total = len(matches)
    print(f"Found {total} matches via Fotmob")

    final_data = []

    for i in range(0, total, MATCH_CONCURRENCY):
        batch = matches[i:i + MATCH_CONCURRENCY]
        tasks = [
            process_one_match(session, m, tv_index, i + idx + 1, total)
            for idx, m in enumerate(batch)
        ]
        results = await asyncio.gather(*tasks)
        final_data.extend(results)

    year_folder = target_date.strftime("%Y")
    save_dir = os.path.join("date", year_folder)
    os.makedirs(save_dir, exist_ok=True)

    save_path = os.path.join(save_dir, file_name)
    with open(save_path, "w", encoding="utf-8") as f:
        json.dump(final_data, f, indent=4, ensure_ascii=False)

    with_tv = sum(1 for m in final_data if m["tv_channels"])
    print(
        f"Saved {year_folder}/{file_name} "
        f"({len(final_data)} matches, {with_tv} with TV channels)"
    )

    return {"offset": days_offset, "date": date_query,
            "matches": len(final_data), "with_tv": with_tv}


def health_problems(tv_index, tv_stats, day_stats):
    """Fotmob failing does not raise: the scrapers just return nothing and the
    run writes thin files over good ones. A single empty day is normal — the
    calendar has quiet days — so only look at signals that cannot legitimately
    be zero across the whole run."""
    problems = []

    total_matches = sum(d["matches"] for d in day_stats)
    if total_matches == 0:
        problems.append(
            "schedule endpoint returned no matches for any of "
            f"{len(day_stats)} days"
        )

    if ENABLE_TV_CHANNELS:
        if not tv_index:
            problems.append("tvlisting returned nothing for any match")

        if tv_stats and tv_stats.requests and \
                tv_stats.failed > tv_stats.requests * 0.2:
            problems.append(
                f"tvlisting failed for {tv_stats.failed} of "
                f"{tv_stats.requests} requests (blocked or rate limited?)"
            )

        near = [d for d in day_stats if -1 <= d["offset"] <= NEAR_HORIZON_DAYS]
        if near and sum(d["with_tv"] for d in near) == 0 and total_matches:
            problems.append(
                "no TV channels on any fixture within the next "
                f"{NEAR_HORIZON_DAYS} days"
            )

    return problems


async def main():
    day_stats = []
    offsets = range(FIRST_OFFSET, LAST_OFFSET + 1)

    async with AsyncSession() as session:
        schedules = {}
        for offset in offsets:
            date_compact = day_date(offset).strftime("%Y%m%d")
            schedules[offset] = await get_fotmob_schedule(session, date_compact)
            await asyncio.sleep(1)

        pairs = load_tv_pairs()
        tv_index, tv_stats = (
            await build_tv_index(session, schedules, pairs)
            if ENABLE_TV_CHANNELS else ({}, None)
        )

        # Nothing is written before the TV sweep, so a run that dies there
        # leaves the previous files in place.
        cleanup_old_files()

        for offset in offsets:
            day_stats.append(
                await process_day(session, offset, schedules[offset], tv_index)
            )

    total_matches = sum(d["matches"] for d in day_stats)
    total_with_tv = sum(d["with_tv"] for d in day_stats)
    print(
        f"\nRun summary: {total_matches} matches over {len(day_stats)} days, "
        f"{total_with_tv} with TV channels"
    )

    problems = health_problems(tv_index, tv_stats, day_stats)
    if problems:
        for p in problems:
            print(f"[-] {p}", file=sys.stderr)
        print(
            "[-] Fotmob data looks broken, failing so the run is not silently "
            "green",
            file=sys.stderr,
        )
        sys.exit(1)

    if ENABLE_TV_CHANNELS:
        save_tv_pairs(pairs)


if __name__ == "__main__":
    asyncio.run(main())
