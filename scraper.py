import os
import json
from datetime import datetime, timedelta
from curl_cffi.requests import Session

FM_BASE = "https://www.fotmob.com/api/data"


def fetch_json(url):
    try:
        r = Session().get(url, impersonate="chrome120", timeout=30)
        if r.status_code == 200:
            return r.json()
        print(f"[-] Failed with status {r.status_code} at {url}")
    except Exception as e:
        print(f"[-] Request error: {e}")
    return None


def run():
    tomorrow = datetime.now() + timedelta(days=1)
    date_query = tomorrow.strftime("%Y%m%d")
    file_name = f"{date_query}.json"
    folder = "date"

    if not os.path.exists(folder):
        os.makedirs(folder)

    print(f"Scraping fixtures for: {tomorrow.strftime('%Y-%m-%d')} (Fotmob)")

    url = (
        f"{FM_BASE}/matches"
        f"?date={date_query}"
        f"&timezone=Asia%2FTokyo"
        f"&ccode3=JPN"
        f"&includeNextDayLateNight=true"
    )

    data = fetch_json(url)
    if not data:
        print("[-] Fotmob API returned no data")
        return

    results = []

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

            results.append({
                "match_id": m["id"],
                "kickoff": timestamp,
                "fixture": f"{m['home']['name']} vs {m['away']['name']}",
                "league": league_name,
                "league_id": league.get("primaryId") or league.get("id", 0),
            })

    if not results:
        print("[-] No matches found")
        return

    with open(f"{folder}/{file_name}", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=4)

    print(f"Saved {folder}/{file_name} ({len(results)} matches)")


if __name__ == "__main__":
    run()
