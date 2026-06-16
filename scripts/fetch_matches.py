"""
Fetch FIFA World Cup 2026 matches from football-data.org and write data/matches.json.

Requires environment variable FOOTBALL_DATA_API_KEY (free tier).
Sign up at: https://www.football-data.org/client/register

Usage:
    python scripts/fetch_matches.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

API_URL = "https://api.football-data.org/v4/competitions/WC/matches"
OUTPUT_FILE = Path(__file__).resolve().parent.parent / "data" / "matches.json"
TIMEOUT = 30


def fetch_matches(api_key: str) -> dict:
    resp = requests.get(
        API_URL,
        headers={"X-Auth-Token": api_key},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def normalize(raw: dict) -> dict:
    """Reduce payload to only the fields the viewer needs."""
    matches = []
    for m in raw.get("matches", []):
        score = m.get("score", {}) or {}
        full_time = score.get("fullTime", {}) or {}
        home = m.get("homeTeam", {}) or {}
        away = m.get("awayTeam", {}) or {}
        matches.append(
            {
                "id": m.get("id"),
                "utcDate": m.get("utcDate"),
                "status": m.get("status"),
                "stage": m.get("stage"),
                "group": m.get("group"),
                "matchday": m.get("matchday"),
                "homeTeam": {
                    "name": home.get("name"),
                    "shortName": home.get("shortName"),
                    "tla": home.get("tla"),
                    "crest": home.get("crest"),
                },
                "awayTeam": {
                    "name": away.get("name"),
                    "shortName": away.get("shortName"),
                    "tla": away.get("tla"),
                    "crest": away.get("crest"),
                },
                "score": {
                    "home": full_time.get("home"),
                    "away": full_time.get("away"),
                    "winner": score.get("winner"),
                    "duration": score.get("duration"),
                },
                "venue": m.get("venue"),
            }
        )
    return {
        "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "football-data.org / competitions/WC/matches",
        "competition": (raw.get("competition") or {}).get("name", "FIFA World Cup"),
        "count": len(matches),
        "matches": matches,
    }


def main() -> int:
    api_key = os.environ.get("FOOTBALL_DATA_API_KEY")
    if not api_key:
        print("ERROR: FOOTBALL_DATA_API_KEY env var is not set.", file=sys.stderr)
        return 1

    try:
        raw = fetch_matches(api_key)
    except requests.HTTPError as e:
        print(f"HTTP error from football-data.org: {e}", file=sys.stderr)
        if e.response is not None:
            print(e.response.text, file=sys.stderr)
        return 2
    except requests.RequestException as e:
        print(f"Network error: {e}", file=sys.stderr)
        return 3

    payload = normalize(raw)

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {payload['count']} matches to {OUTPUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
