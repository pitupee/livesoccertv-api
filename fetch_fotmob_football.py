import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

try:
    from curl_cffi import requests
    USE_CURL_CFFI = True
except ImportError:
    import requests
    USE_CURL_CFFI = False

# FotMob matches endpoint (timezone=Asia/Tokyo, matches the existing tooling)
FM_URL = "https://www.fotmob.com/api/data/matches"
TIMEZONE = "Asia%2FTokyo"
CCCODE3 = "JPN"

# Upcoming window in days (keeps matches starting today..now+7 days)
FUTURE_DAYS = 7

# Keep started-but-unfinished (live) matches started within this buffer
LIVE_GRACE_MINUTES = 180

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.fotmob.com/",
    "Origin": "https://www.fotmob.com",
}


def fetch_date(date_str):
    url = f"{FM_URL}?date={date_str}&timezone={TIMEZONE}&ccode3={CCCODE3}&includeNextDayLateNight=true"
    if USE_CURL_CFFI:
        r = requests.get(url, impersonate="chrome124", headers=HEADERS, timeout=30)
    else:
        r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def team_slug(name):
    """Lowercase, no spaces or punctuation — used in details_url / streaming_url."""
    if not name:
        return "unknown"
    return re.sub(r"[^a-z0-9]", "", name.strip().lower())


def logo_url(team_id):
    return f"https://images.fotmob.com/image_resources/logo/teamlogo/{team_id}.png"


def normalize_start(utc_time):
    """Normalize FotMob utcTime (2026-08-07T18:45:00.000Z) → 2026-08-07T18:45Z."""
    if not utc_time:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})", utc_time)
    if m:
        return m.group(1) + "Z"
    return utc_time


def transform_match(lg, m):
    team1 = (m.get("home") or {}).get("name") or "TBD"
    team2 = (m.get("away") or {}).get("name") or "TBD"
    home_id = (m.get("home") or {}).get("id")
    away_id = (m.get("away") or {}).get("id")
    league = lg.get("name") or "Football"

    start = normalize_start((m.get("status") or {}).get("utcTime") or
                            (datetime.fromtimestamp((m.get("timeTS") or 0) / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%MZ")))

    event_id = m.get("id")
    try:
        event_id = int(event_id) if event_id is not None else None
    except (ValueError, TypeError):
        pass

    slug = team_slug(team1) if team1 else "unknown"

    return {
        "team1": team1,
        "team2": team2,
        "team1_logo": logo_url(home_id) if home_id else "",
        "team2_logo": logo_url(away_id) if away_id else "",
        "league": league,
        "start": start,
        "duration": 2.2,
        "details_url": f"https://home.getemoji.online/?yosintv={slug}",
        "streaming_url": f"https://cdn.singhs.com.np/{slug}.json",
        "event_id": event_id,
        "football_data": None,
    }


def main():
    out_dir = os.path.dirname(os.path.abspath(__file__))
    os.makedirs(out_dir, exist_ok=True)

    now = datetime.now(timezone.utc)
    cutoff_start = now - timedelta(minutes=LIVE_GRACE_MINUTES)
    cutoff_end = now + timedelta(days=FUTURE_DAYS)

    all_matches = []
    seen_ids = set()

    for day in range(FUTURE_DAYS + 1):  # today + next 7 days
        d = now + timedelta(days=day)
        date_str = d.strftime("%Y%m%d")
        try:
            data = fetch_date(date_str)
        except Exception as e:
            print(f"  [{date_str}] FAILED: {e}", file=sys.stderr)
            continue

        count = 0
        for lg in (data.get("leagues") or []):
            for m in (lg.get("matches") or []):
                try:
                    status = m.get("status") or {}
                    if status.get("finished") or status.get("cancelled"):
                        continue

                    utc_time = status.get("utcTime") or datetime.fromtimestamp((m.get("timeTS") or 0) / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
                    start_dt = datetime.strptime(utc_time[:16], "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)

                    # Auto-delete past / finished matches: keep only upcoming + live
                    if start_dt < cutoff_start or start_dt > cutoff_end:
                        continue

                    tm = transform_match(lg, m)
                    eid = tm["event_id"]
                    if eid is not None:
                        if eid in seen_ids:
                            continue
                        seen_ids.add(eid)
                    all_matches.append(tm)
                    count += 1
                except Exception as e:
                    print(f"  Warning: skipping item — {e}", file=sys.stderr)

        print(f"  [{date_str}] kept {count} upcoming matches")

    all_matches.sort(key=lambda m: m.get("start") or "")

    out_path = os.path.join(out_dir, "football-data.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"matches": all_matches}, f, ensure_ascii=False, indent=2)

    print(f"\nSaved {len(all_matches)} upcoming matches → {out_path}")


if __name__ == "__main__":
    main()
