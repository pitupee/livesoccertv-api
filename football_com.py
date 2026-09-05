import json
from datetime import datetime, timezone

try:
    from curl_cffi import requests
    USE_CURL_CFFI = True
except ImportError:
    import requests
    USE_CURL_CFFI = False

URL = "https://www.football.com/api/ng/factsCenter/event/firstSearch"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.football.com/",
    "Origin": "https://www.football.com",
}


def fetch_all():
    seen = set()
    all_matches = []

    if USE_CURL_CFFI:
        resp = requests.get(
            URL,
            params={"keyword": "BestOdds", "offset": 0, "pageSize": 500},
            headers=HEADERS,
            impersonate="chrome124",
            timeout=15,
        )
    else:
        resp = requests.get(
            URL,
            params={"keyword": "BestOdds", "offset": 0, "pageSize": 500},
            headers=HEADERS,
            timeout=15,
        )
    resp.raise_for_status()
    body = resp.json()

    if body.get("bizCode") != 10000:
        raise RuntimeError(f"API error: {body.get('message')}")

    live = body["data"].get("live", [])
    pre = body["data"].get("preMatch", [])

    for m in live + pre:
        raw_id = m.get("eventId", "")
        match_id = raw_id.split(":")[-1] if ":" in raw_id else raw_id
        if match_id in seen:
            continue
        seen.add(match_id)
        ts = m.get("estimateStartTime", 0) / 1000
        all_matches.append({
            "matchId": match_id,
            "estimateStartTime": m.get("estimateStartTime"),
            "time": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "homeTeamName": m.get("homeTeamName"),
            "awayTeamName": m.get("awayTeamName"),
        })

    return all_matches


def save(matches):
    with open("today.json", "w", encoding="utf-8") as f:
        json.dump(matches, f, indent=2, ensure_ascii=False)
    print(f"[{datetime.now()}] Saved {len(matches)} matches to today.json")


if __name__ == "__main__":
    save(fetch_all())
