"""
Fetch 1X2 market odds for the FIFA World Cup 2026 with a 3-source fallback:

    1. The-Odds-API (https://the-odds-api.com)         requires THE_ODDS_API_KEY
    2. ESPN (DraftKings line, scraping)                no key required
    3. Mirror of bayesian_model/data/market_odds.csv   requires BAYESIAN_ODDS_URL

Output: data/odds.json with normalized decimal odds and no-vig implied
probabilities per match (date, home, away, h, d, a, p_h, p_d, p_a).

If a source fails or returns nothing the next is tried. The script exits 0
even when all sources fail (the workflow keeps going); failures are reported
in the JSON header so the validator can pick them up.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "data" / "odds.json"
ALIASES_FILE = ROOT / "data" / "team_aliases.json"
TIMEOUT = 30


# ---------------------------------------------------------------------------
# Aliases (loaded if present; gracefully empty otherwise)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------

def _no_vig(home: float, draw: float, away: float) -> tuple[float, float, float]:
    if not all(x and x > 1.0 for x in (home, draw, away)):
        return (0.0, 0.0, 0.0)
    p_h, p_d, p_a = 1.0 / home, 1.0 / draw, 1.0 / away
    total = p_h + p_d + p_a
    if total <= 0:
        return (0.0, 0.0, 0.0)
    return (p_h / total, p_d / total, p_a / total)


def _date_key(value: Any) -> str:
    if value is None:
        return ""
    s = str(value)[:10]
    return s if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) else ""


def _row(date: str, home: str, away: str, h: float, d: float, a: float,
         bookmaker: str, source: str) -> dict:
    p_h, p_d, p_a = _no_vig(h, d, a)
    return {
        "date": date,
        "home": canon(home),
        "away": canon(away),
        "home_odds": round(h, 4),
        "draw_odds": round(d, 4),
        "away_odds": round(a, 4),
        "p_home": round(p_h, 4),
        "p_draw": round(p_d, 4),
        "p_away": round(p_a, 4),
        "bookmaker": bookmaker,
        "source": source,
    }


# ---------------------------------------------------------------------------
# Source 1: The-Odds-API
# ---------------------------------------------------------------------------

def fetch_the_odds_api(api_key: str) -> list[dict]:
    url = "https://api.the-odds-api.com/v4/sports/soccer_fifa_world_cup/odds"
    params = {
        "regions": "eu,uk,us",
        "markets": "h2h",
        "oddsFormat": "decimal",
        "apiKey": api_key,
    }
    resp = requests.get(f"{url}?{urlencode(params)}", timeout=TIMEOUT)
    resp.raise_for_status()
    games = resp.json() or []
    rows: list[dict] = []
    for g in games:
        home_team = g.get("home_team")
        away_team = g.get("away_team")
        commence = g.get("commence_time", "")
        date = _date_key(commence)
        if not (home_team and away_team and date):
            continue
        # Average across bookmakers for stability.
        h_list, d_list, a_list, books = [], [], [], []
        for bk in g.get("bookmakers", []) or []:
            for mkt in bk.get("markets", []) or []:
                if mkt.get("key") != "h2h":
                    continue
                outcomes = {o.get("name"): o.get("price") for o in mkt.get("outcomes", []) or []}
                h = outcomes.get(home_team)
                a = outcomes.get(away_team)
                d = outcomes.get("Draw")
                if h and a and d:
                    h_list.append(float(h))
                    a_list.append(float(a))
                    d_list.append(float(d))
                    books.append(str(bk.get("title") or bk.get("key", "?")))
        if not h_list:
            continue
        rows.append(_row(
            date, home_team, away_team,
            sum(h_list) / len(h_list),
            sum(d_list) / len(d_list),
            sum(a_list) / len(a_list),
            f"avg({len(books)})",
            "the-odds-api",
        ))
    return rows


# ---------------------------------------------------------------------------
# Source 2: ESPN (DraftKings line)
# ---------------------------------------------------------------------------

def _american_to_decimal(value: Any) -> float | None:
    if value is None:
        return None
    s = str(value).replace("+", "").strip()
    try:
        n = int(s)
    except ValueError:
        return None
    if n > 0:
        return 1.0 + n / 100.0
    if n < 0:
        return 1.0 + 100.0 / abs(n)
    return None


def fetch_espn() -> list[dict]:
    """Use ESPN's hidden scoreboard JSON for the FIFA World Cup."""
    # FIFA World Cup competition slug on ESPN: fifa.world
    base = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"
    rows: list[dict] = []
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    # ESPN allows date ranges via "dates=YYYYMMDD-YYYYMMDD"
    span = f"{today}-20260720"
    resp = requests.get(f"{base}?dates={span}", timeout=TIMEOUT,
                        headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    data = resp.json() or {}
    for evt in data.get("events", []) or []:
        date = _date_key(evt.get("date"))
        comps = (evt.get("competitions") or [])
        if not comps:
            continue
        comp = comps[0]
        teams_by_id = {}
        for c in comp.get("competitors", []) or []:
            team = (c.get("team") or {})
            teams_by_id[c.get("id")] = {
                "name": team.get("displayName") or team.get("name"),
                "homeAway": c.get("homeAway"),
            }
        # Find home / away
        home_name = away_name = None
        for tid, info in teams_by_id.items():
            if info["homeAway"] == "home":
                home_name = info["name"]
            elif info["homeAway"] == "away":
                away_name = info["name"]
        if not (home_name and away_name and date):
            continue
        odds_list = comp.get("odds") or []
        if not odds_list:
            continue
        # Take the first provider available; ESPN typically returns DraftKings.
        provider = odds_list[0]
        provider_name = (provider.get("provider") or {}).get("name", "ESPN")
        h = _american_to_decimal((provider.get("homeTeamOdds") or {}).get("moneyLine"))
        a = _american_to_decimal((provider.get("awayTeamOdds") or {}).get("moneyLine"))
        d = _american_to_decimal(provider.get("drawOdds", {}).get("moneyLine") if isinstance(provider.get("drawOdds"), dict) else provider.get("drawOdds"))
        if not (h and d and a):
            # Some payloads expose just a "details" string like "Mexico -300";
            # skip rather than guess.
            continue
        rows.append(_row(date, home_name, away_name, h, d, a, provider_name, "espn"))
    return rows


# ---------------------------------------------------------------------------
# Source 3: Mirror from bayesian_model raw CSV
# ---------------------------------------------------------------------------

def fetch_bayesian_mirror(url: str) -> list[dict]:
    resp = requests.get(url, timeout=TIMEOUT)
    resp.raise_for_status()
    reader = csv.DictReader(io.StringIO(resp.text))
    rows: list[dict] = []
    for r in reader:
        try:
            h = float(r["home_odds"])
            d = float(r["draw_odds"])
            a = float(r["away_odds"])
        except (KeyError, TypeError, ValueError):
            continue
        date = _date_key(r.get("date"))
        if not date:
            continue
        rows.append(_row(
            date, r.get("home_team", ""), r.get("away_team", ""),
            h, d, a,
            r.get("bookmakers", "?"),
            "bayesian-mirror",
        ))
    return rows


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def main() -> int:
    sources_tried: list[dict] = []
    odds: list[dict] = []
    used_source = None

    api_key = os.environ.get("THE_ODDS_API_KEY", "").strip()
    bayesian_url = os.environ.get("BAYESIAN_ODDS_URL", "").strip()

    plan: list[tuple[str, callable]] = []
    if api_key:
        plan.append(("the-odds-api", lambda: fetch_the_odds_api(api_key)))
    plan.append(("espn", fetch_espn))
    if bayesian_url:
        plan.append(("bayesian-mirror", lambda: fetch_bayesian_mirror(bayesian_url)))

    for name, fn in plan:
        try:
            result = fn()
            sources_tried.append({"source": name, "ok": True, "count": len(result)})
            if result and not odds:
                odds = result
                used_source = name
        except Exception as e:  # noqa: BLE001 — keep going to next source
            sources_tried.append({"source": name, "ok": False, "error": str(e)[:200]})

    payload = {
        "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": used_source,
        "sourcesTried": sources_tried,
        "count": len(odds),
        "odds": sorted(odds, key=lambda r: (r["date"], r["home"], r["away"])),
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    if used_source:
        print(f"Wrote {len(odds)} odds rows from '{used_source}' to {OUTPUT}")
    else:
        print("WARNING: no source returned odds; wrote empty odds.json", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
