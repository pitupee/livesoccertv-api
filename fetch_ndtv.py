import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

try:
    from curl_cffi import requests
    USE_CURL_CFFI = True
except ImportError:
    import requests as requests
    USE_CURL_CFFI = False

BASE_URL = "https://sports.ndtv.com/multisportsapi/"

PARAMS = {
    "methodtype": "3",
    "client": "2656770267",
    "sport": "1",
    "league": "0",
    "timezone": "0530",
    "language": "en",
    "widget": "sidefiltercricketSports",
}

GAMESTATES = {
    "live": "1",
    "upcoming": "2",
    "completed": "3",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://sports.ndtv.com/cricket",
    "Origin": "https://sports.ndtv.com",
}

# Only keep completed matches within this window
COMPLETED_WINDOW_DAYS = 7


def fetch_gamestate(gs_value):
    params = {**PARAMS, "gamestate": gs_value}
    url = BASE_URL + "?" + "&".join(f"{k}={v}" for k, v in params.items())
    if USE_CURL_CFFI:
        r = requests.get(url, impersonate="chrome124", headers=HEADERS, timeout=60)
    else:
        r = requests.get(url, headers=HEADERS, timeout=60)
    r.raise_for_status()
    return r.json()


def team_slug(name):
    """Lowercase, no spaces or punctuation — used in details_url / streaming_url."""
    if not name:
        return "unknown"
    return re.sub(r"[^a-z0-9]", "", name.strip().lower())


def country_slug(name):
    """
    Extract base country name for logo URL.
    Strips: Women, Men, Girls, Boys, Under-XX, U19, A (A-team), XI.
    e.g. 'England Women Under-19' → 'england'
         'West Indies Women'      → 'westindies'
         'South Africa A'         → 'southafrica'
    """
    if not name:
        return "unknown"
    # Strip from the first suffix keyword onwards
    cleaned = re.sub(
        r"\s+(women|men|girls|boys|under[\s\-]?\d+|u[\s\-]?\d+|xi)\b.*$",
        "", name.strip(), flags=re.IGNORECASE,
    )
    # Strip trailing standalone ' A' (A-team)
    cleaned = re.sub(r"\s+a$", "", cleaned.strip(), flags=re.IGNORECASE)
    return re.sub(r"[^a-z0-9]", "", cleaned.strip().lower())


def logo_url(name):
    return f"https://aimages.willow.tv/teamLogos/{country_slug(name)}.png"


def parse_start_utc(s):
    """
    Convert NDTV date strings to JST ISO 8601 with timezone offset (UTC+9).
    Handles: 'YYYY-MM-DDTHH:MM+05:30', 'YYYY-MM-DDTHH:MM:SS+05:30', 'YYYY-MM-DD HH:MM:SS'
    """
    if not s:
        return None
    try:
        s = str(s).strip()
        # ISO with offset: 2026-07-02T13:30+05:30 or ...T13:30:00+05:30
        m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?)\+(\d{2}):(\d{2})", s)
        if m:
            fmt = "%Y-%m-%dT%H:%M:%S" if s.count(":") >= 3 else "%Y-%m-%dT%H:%M"
            naive = datetime.strptime(m.group(1), fmt)
            offset = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
            utc_dt = naive - offset
            jst_dt = utc_dt + timedelta(hours=9)
            return jst_dt.strftime("%Y-%m-%dT%H:%M:%S+09:00")
        # Space-separated IST: 2026-07-02 08:00:00
        m2 = re.match(r"(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}(?::\d{2})?)", s)
        if m2:
            fmt = "%Y-%m-%dT%H:%M:%S" if ":" in m2.group(2)[3:] else "%Y-%m-%dT%H:%M"
            naive = datetime.strptime(f"{m2.group(1)}T{m2.group(2)}", fmt)
            utc_dt = naive - timedelta(hours=5, minutes=30)
            jst_dt = utc_dt + timedelta(hours=9)
            return jst_dt.strftime("%Y-%m-%dT%H:%M:%S+09:00")
    except Exception:
        pass
    return str(s)


def transform_match(item):
    # NDTV puts teams in a 'participants' array
    participants = item.get("participants") or []
    team1 = participants[0].get("name") if len(participants) > 0 else None
    team2 = participants[1].get("name") if len(participants) > 1 else None

    # Fallback for unknown response shapes
    if not team1:
        team1 = item.get("t1nm") or item.get("team1_name") or item.get("home_team")
    if not team2:
        team2 = item.get("t2nm") or item.get("team2_name") or item.get("away_team")

    team1_logo = item.get("t1img") or item.get("team1_logo") or logo_url(team1)
    team2_logo = item.get("t2img") or item.get("team2_logo") or logo_url(team2)

    league = (item.get("series_name") or item.get("tour_name") or
              item.get("srnm") or item.get("tournament") or "Cricket")

    start = parse_start_utc(
        item.get("start_date") or item.get("ms") or item.get("match_start") or item.get("date")
    )

    event_fmt = (item.get("event_format") or item.get("match_type") or item.get("mtp") or "").lower()
    if "test" in event_fmt:
        duration = 10   # hours
        match_format = "Test"
    elif "odi" in event_fmt or "one_day" in event_fmt or "one day" in event_fmt:
        duration = 8    # hours
        match_format = "ODI"
    else:
        duration = 5    # hours (T20 / unknown)
        match_format = "T20"

    event_id = item.get("match_id") or item.get("mid") or item.get("event_id") or item.get("id")
    try:
        event_id = int(event_id) if event_id is not None else None
    except (ValueError, TypeError):
        pass

    slug = team_slug(team1) if team1 else "unknown"

    return {
        "team1": team1 or "TBD",
        "team2": team2 or "TBD",
        "team1_logo": team1_logo,
        "team2_logo": team2_logo,
        "league": league,
        "start": start,
        "duration": duration,
        "format": match_format,
        "details_url": f"https://home.getemoji.online/?yosintv={slug}",
        "streaming_url": f"https://cdn.singhs.com.np/{slug}.json",
        "event_id": event_id,
        "cricket_data": None,
    }


def extract_matches(data, label=""):
    """Extract raw match list from a single-gamestate NDTV response."""
    if isinstance(data, dict) and "matches" in data:
        return data["matches"] or []
    if isinstance(data, list):
        return data
    return []


def main():
    out_dir = os.path.dirname(os.path.abspath(__file__))
    os.makedirs(out_dir, exist_ok=True)

    cutoff = datetime.now(timezone.utc) - timedelta(days=COMPLETED_WINDOW_DAYS)
    all_matches = []
    seen_ids = set()

    for label, gs in GAMESTATES.items():
        try:
            data = fetch_gamestate(gs)
            raw = extract_matches(data, label)
            print(f"  [{label}] {len(raw)} raw matches")

            for item in raw:
                try:
                    m = transform_match(item)

                    # For completed matches, skip anything older than the window
                    if label == "completed" and m["start"]:
                        try:
                            # Parse JST format with timezone offset
                            match_dt = datetime.strptime(m["start"], "%Y-%m-%dT%H:%M:%S%z")
                            # Convert to UTC for comparison
                            match_dt_utc = match_dt.astimezone(timezone.utc)
                            if match_dt_utc < cutoff:
                                continue
                        except Exception:
                            pass

                    eid = m["event_id"]
                    if eid is not None and eid in seen_ids:
                        continue
                    if eid is not None:
                        seen_ids.add(eid)
                    all_matches.append(m)

                except Exception as e:
                    print(f"  Warning: skipping item — {e}", file=sys.stderr)

            kept = sum(1 for m in all_matches if True)
            print(f"  [{label}] → kept (running total: {len(all_matches)})")

        except Exception as e:
            print(f"  [{label}] FAILED: {e}", file=sys.stderr)

    all_matches.sort(key=lambda m: m.get("start") or "")

    out_path = os.path.join(out_dir, "cricket-data.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"matches": all_matches}, f, ensure_ascii=False, indent=2)

    print(f"\nSaved {len(all_matches)} matches → {out_path}")


if __name__ == "__main__":
    main()
