"""
Fetch current injuries / suspensions for World Cup 2026 squads.

Primary source: API-Football (RapidAPI free tier, 100 req/day).
    secret: API_FOOTBALL_KEY

Fallback (best-effort, no key): scrape Wikipedia per-team injury sections.
This is intentionally simple — it's gated by env var WIKI_INJURIES_FALLBACK=1
since the structure is fragile.

Output: data/injuries.json
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from html import unescape
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "data" / "injuries.json"
MATCHES_FILE = ROOT / "data" / "matches.json"
ALIASES_FILE = ROOT / "data" / "team_aliases.json"
TIMEOUT = 30
RATE_DELAY = 1.0  # API-Football free is 30 req/min


def _load_aliases() -> dict[str, str]:
    if not ALIASES_FILE.exists():
        return {}
    try:
        raw = json.loads(ALIASES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    out: dict[str, str] = {}
    for canon_name, variants in raw.items():
        if str(canon_name).startswith("_"):
            continue
        out[canon_name.lower()] = canon_name
        if isinstance(variants, list):
            for v in variants:
                out[str(v).lower()] = canon_name
    return out


_ALIASES = _load_aliases()


def canon(name: str) -> str:
    if name is None:
        return ""
    s = str(name).strip()
    return _ALIASES.get(s.lower(), s)


def collect_teams() -> list[str]:
    if not MATCHES_FILE.exists():
        return []
    try:
        payload = json.loads(MATCHES_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    teams: set[str] = set()
    for m in payload.get("matches", []):
        for side in ("homeTeam", "awayTeam"):
            t = (m.get(side) or {}).get("name")
            if t:
                teams.add(canon(t))
    return sorted(teams)


# ---------------------------------------------------------------------------
# API-Football (RapidAPI tier or direct key)
# ---------------------------------------------------------------------------

API_FOOTBALL_BASE = "https://v3.football.api-sports.io"
API_FOOTBALL_RAPID = "https://api-football-v1.p.rapidapi.com/v3"


def _api_football_headers(key: str) -> dict:
    """Detect whether key is direct (api-sports.io) or RapidAPI."""
    # RapidAPI keys are typically 50 chars; api-sports.io are 32. Try direct first.
    return {"x-apisports-key": key}


def _api_football_get(path: str, key: str, params: dict | None = None) -> dict:
    url = f"{API_FOOTBALL_BASE}{path}"
    resp = requests.get(url, headers=_api_football_headers(key),
                        params=params or {}, timeout=TIMEOUT)
    if resp.status_code == 401 or resp.status_code == 403:
        # Retry as RapidAPI
        resp = requests.get(
            f"{API_FOOTBALL_RAPID}{path}",
            headers={
                "x-rapidapi-key": key,
                "x-rapidapi-host": "api-football-v1.p.rapidapi.com",
            },
            params=params or {},
            timeout=TIMEOUT,
        )
    resp.raise_for_status()
    return resp.json() or {}


def fetch_api_football(key: str, teams: list[str]) -> dict[str, list[dict]]:
    """Find each team's id then list current injuries (last 60 days)."""
    out: dict[str, list[dict]] = {t: [] for t in teams}

    # Search team ids
    team_ids: dict[str, int] = {}
    for team in teams:
        try:
            data = _api_football_get("/teams", key, {"name": team, "type": "national"})
        except requests.HTTPError as e:
            print(f"  WARN team-lookup {team}: {e}", file=sys.stderr)
            time.sleep(RATE_DELAY)
            continue
        for r in data.get("response", []) or []:
            t = (r.get("team") or {})
            if (t.get("national") is True) and t.get("id"):
                team_ids[team] = int(t["id"])
                break
        time.sleep(RATE_DELAY)

    # Pull injuries per team for current FIFA window
    season = datetime.now(timezone.utc).year
    for team, tid in team_ids.items():
        try:
            data = _api_football_get("/injuries", key,
                                     {"team": tid, "season": season})
        except requests.HTTPError as e:
            print(f"  WARN injuries {team}: {e}", file=sys.stderr)
            time.sleep(RATE_DELAY)
            continue
        items: list[dict] = []
        for r in data.get("response", []) or []:
            player = (r.get("player") or {})
            league = (r.get("league") or {})
            fixture = (r.get("fixture") or {})
            items.append({
                "player": player.get("name"),
                "type": player.get("type"),
                "reason": player.get("reason"),
                "league": league.get("name"),
                "fixtureDate": (fixture.get("date") or "")[:10],
            })
        out[team] = items
        time.sleep(RATE_DELAY)

    return out


# ---------------------------------------------------------------------------
# Wikipedia best-effort fallback (no key)
# ---------------------------------------------------------------------------

WIKI_LANGS = ("en", "es")


def _wiki_search_page(team: str) -> str | None:
    """Try to locate a 2026 World Cup squad page or fall back to team page."""
    queries = [
        f"{team} at the 2026 FIFA World Cup",
        f"{team} national football team",
    ]
    for lang in WIKI_LANGS:
        for q in queries:
            try:
                resp = requests.get(
                    f"https://{lang}.wikipedia.org/w/api.php",
                    params={
                        "action": "opensearch",
                        "search": q,
                        "limit": 1,
                        "namespace": 0,
                        "format": "json",
                    },
                    timeout=TIMEOUT,
                )
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, list) and len(data) >= 4 and data[3]:
                    return data[3][0]
            except requests.RequestException:
                continue
    return None


def fetch_wikipedia_fallback(teams: list[str]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {t: [] for t in teams}
    for team in teams:
        url = _wiki_search_page(team)
        if not url:
            continue
        try:
            page = requests.get(url, timeout=TIMEOUT,
                                headers={"User-Agent": "Mozilla/5.0"})
            page.raise_for_status()
        except requests.RequestException:
            continue
        text = page.text
        # Heuristic: capture sentences mentioning 'injury'/'lesión' near a player name.
        notes: list[dict] = []
        for sentence in re.split(r"(?<=[.!?])\s+", unescape(re.sub(r"<[^>]+>", " ", text))):
            if re.search(r"\b(injur|injuries|lesi[oó]n|withdrew|ruled out)\b", sentence, re.I):
                clean = sentence.strip()[:240]
                if clean:
                    notes.append({"note": clean})
                if len(notes) >= 5:
                    break
        out[team] = notes
        time.sleep(0.5)
    return out


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def main() -> int:
    teams = collect_teams()
    if not teams:
        print("WARN: no teams found in matches.json — run fetch_matches first.", file=sys.stderr)

    sources_tried: list[dict] = []
    used_source: str | None = None
    by_team: dict[str, list[dict]] = {t: [] for t in teams}

    api_key = os.environ.get("API_FOOTBALL_KEY", "").strip()
    wiki_fallback = os.environ.get("WIKI_INJURIES_FALLBACK", "").strip() == "1"

    if api_key and teams:
        try:
            res = fetch_api_football(api_key, teams)
            non_empty = sum(1 for v in res.values() if v)
            sources_tried.append({"source": "api-football", "ok": True,
                                  "teams_with_data": non_empty})
            if non_empty:
                by_team = res
                used_source = "api-football"
        except Exception as e:  # noqa: BLE001
            sources_tried.append({"source": "api-football", "ok": False,
                                  "error": str(e)[:200]})
    else:
        sources_tried.append({"source": "api-football", "ok": False,
                              "error": "API_FOOTBALL_KEY not set"})

    if not used_source and wiki_fallback and teams:
        try:
            res = fetch_wikipedia_fallback(teams)
            non_empty = sum(1 for v in res.values() if v)
            sources_tried.append({"source": "wikipedia", "ok": True,
                                  "teams_with_data": non_empty})
            if non_empty:
                by_team = res
                used_source = "wikipedia"
        except Exception as e:  # noqa: BLE001
            sources_tried.append({"source": "wikipedia", "ok": False,
                                  "error": str(e)[:200]})

    payload = {
        "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": used_source,
        "sourcesTried": sources_tried,
        "teamCount": len(teams),
        "teams": by_team,
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    if used_source:
        print(f"Wrote injuries from '{used_source}' to {OUTPUT}")
    else:
        print("WARNING: no injury source available; wrote empty injuries.json",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
