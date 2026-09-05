import asyncio
import json
import os
import pycountry
from datetime import datetime, timedelta
from curl_cffi.requests import AsyncSession

FM_BASE = "https://www.fotmob.com/api/data"

ALL_COUNTRY_CODES = [c.alpha_2 for c in pycountry.countries]

MATCH_CONCURRENCY = 10
TV_CONCURRENCY = 12
TV_TIMEOUT = 30
TV_RETRIES = 3
ENABLE_TV_CHANNELS = True
ENABLE_VENUE = True


def cleanup_old_files():
    if not os.path.exists("date"):
        os.makedirs("date")
        return

    keep_files = set()

    for offset in range(-1, 31):
        d = datetime.now() + timedelta(days=offset)

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


async def fetch_country_tv_listings(session, country_code, semaphore):
    url = f"{FM_BASE}/tvlistings?countryCode={country_code}"

    data = None
    async with semaphore:
        for attempt in range(TV_RETRIES):
            data = await fetch_json(session, url, timeout=TV_TIMEOUT)
            if data is not None:
                break
            await asyncio.sleep(1 + attempt)

    if not data:
        return country_code, {}

    per_match = {}
    for raw_match_id, entries in data.items():
        try:
            match_id = int(raw_match_id)
        except (TypeError, ValueError):
            continue

        channels = set()
        for entry in entries or []:
            station = entry.get("station") or {}
            name = (station.get("name") or station.get("callSign") or "").strip()
            if name:
                channels.add(name)

        if channels:
            per_match[match_id] = channels

    return country_code, per_match


async def build_tv_index(session):
    """Fotmob serves a whole country's TV listings in one call, keyed by match
    id and covering roughly the next ten days. Sweeping every country costs
    ~250 requests in total, instead of one request per match per country."""
    semaphore = asyncio.Semaphore(TV_CONCURRENCY)

    tasks = [
        fetch_country_tv_listings(session, cc, semaphore)
        for cc in ALL_COUNTRY_CODES
    ]
    results = await asyncio.gather(*tasks)

    tv_index = {}
    covered_countries = 0

    for country_code, per_match in results:
        if not per_match:
            continue

        covered_countries += 1
        name = country_name(country_code)

        for match_id, channels in per_match.items():
            tv_index.setdefault(match_id, []).append({
                "country": name,
                "country_code": country_code,
                "channels": sorted(channels),
            })

    for entries in tv_index.values():
        entries.sort(key=lambda x: x["country"])

    print(
        f"TV listings: {len(tv_index)} matches "
        f"from {covered_countries} countries"
    )

    return tv_index


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


async def process_day(session, days_offset, tv_index):
    target_date = datetime.now() + timedelta(days=days_offset)
    date_query = target_date.strftime("%Y-%m-%d")
    date_compact = target_date.strftime("%Y%m%d")
    file_name = date_compact + ".json"

    print(f"Processing {date_query}")

    matches = await get_fotmob_schedule(session, date_compact)
    if not matches:
        print(f"No matches found: {date_query}")
        return

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


async def main():
    cleanup_old_files()

    async with AsyncSession() as session:
        tv_index = await build_tv_index(session) if ENABLE_TV_CHANNELS else {}

        for offset in range(-1, 31):
            await process_day(session, offset, tv_index)
            await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(main())
