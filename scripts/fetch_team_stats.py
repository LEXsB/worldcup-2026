"""
Build per-team last-10 stats for the FIFA World Cup 2026 viewer.

Goals (scored/conceded, W-D-L) come from a mirror of the bayesian_model
public results dataset (set BAYESIAN_RESULTS_URL to its raw URL).

Cards (yellow / red) come from API-Football v3 (free 100 req/day).
For each team we ask `/fixtures?team={id}&last=10` (1 call) and then
`/fixtures/statistics?fixture={fid}` per fixture (1 call each). Per-fixture
results are cached in data/team_stats_cards_cache.json so subsequent runs
only hit the API for newly-played matches — fits the free-tier budget.

Output keys exposed per team:

    form          — last-10 goals/W-D-L/streak from results.csv mirror
    cards_last10  — yellow/red counts across the same 10 most recent
                    international matches (any competition)

Output: data/team_stats.json
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "data" / "team_stats.json"
ALIASES_FILE = ROOT / "data" / "team_aliases.json"
MATCHES_FILE = ROOT / "data" / "matches.json"
CARDS_CACHE_FILE = ROOT / "data" / "team_stats_cards_cache.json"
TIMEOUT = 30
LAST_N = 10
API_FOOTBALL_BASE = "https://v3.football.api-sports.io"
API_FOOTBALL_RAPID = "https://api-football-v1.p.rapidapi.com/v3"
API_FOOTBALL_RATE_DELAY = 2.1  # 30 req/min on free tier
API_FOOTBALL_DEFAULT_BUDGET = 90  # leave headroom under 100/day


# ---------------------------------------------------------------------------
# Aliases
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
# Last-10 form from a results.csv mirror
# ---------------------------------------------------------------------------

def _empty_form(team: str) -> dict:
    return {
        "team": team,
        "matches": 0,
        "window": LAST_N,
        "gf_total": 0,
        "ga_total": 0,
        "gf_avg": None,
        "ga_avg": None,
        "wins": 0,
        "draws": 0,
        "losses": 0,
        "record": "0-0-0",
        "last_match_date": "",
        "last_matches": [],
    }


def fetch_results_csv(url: str) -> list[dict]:
    """Download results.csv (martj42-style) from a raw URL."""
    resp = requests.get(url, timeout=TIMEOUT)
    resp.raise_for_status()
    reader = csv.DictReader(io.StringIO(resp.text))
    rows: list[dict] = []
    for r in reader:
        date = (r.get("date") or "")[:10]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            continue
        try:
            hg = int(r["home_score"])
            ag = int(r["away_score"])
        except (KeyError, TypeError, ValueError):
            continue
        rows.append({
            "date": date,
            "home": canon(r.get("home_team", "")),
            "away": canon(r.get("away_team", "")),
            "hg": hg,
            "ag": ag,
            "tournament": r.get("tournament", ""),
        })
    return rows


def compute_form_from_results(results: list[dict], teams: set[str]) -> dict[str, dict]:
    today = datetime.now(timezone.utc).date().isoformat()
    events: dict[str, list[dict]] = defaultdict(list)
    for m in results:
        if m["date"] >= today:
            continue
        events[m["home"]].append({
            "date": m["date"], "opponent": m["away"],
            "gf": m["hg"], "ga": m["ag"],
            "result": "W" if m["hg"] > m["ag"] else ("D" if m["hg"] == m["ag"] else "L"),
            "tournament": m["tournament"],
            "venue": "H",
        })
        events[m["away"]].append({
            "date": m["date"], "opponent": m["home"],
            "gf": m["ag"], "ga": m["hg"],
            "result": "W" if m["ag"] > m["hg"] else ("D" if m["ag"] == m["hg"] else "L"),
            "tournament": m["tournament"],
            "venue": "A",
        })

    out: dict[str, dict] = {}
    for team in teams:
        last = sorted(events.get(team, []), key=lambda x: x["date"], reverse=True)[:LAST_N]
        if not last:
            out[team] = _empty_form(team)
            continue
        gf = sum(x["gf"] for x in last)
        ga = sum(x["ga"] for x in last)
        wins = sum(1 for x in last if x["result"] == "W")
        draws = sum(1 for x in last if x["result"] == "D")
        losses = sum(1 for x in last if x["result"] == "L")
        out[team] = {
            "team": team,
            "matches": len(last),
            "window": LAST_N,
            "gf_total": gf,
            "ga_total": ga,
            "gf_avg": round(gf / len(last), 2),
            "ga_avg": round(ga / len(last), 2),
            "wins": wins,
            "draws": draws,
            "losses": losses,
            "record": f"{wins}-{draws}-{losses}",
            "last_match_date": last[0]["date"],
            "last_matches": last,
        }
    return out


# ---------------------------------------------------------------------------
# Cards (yellow/red) for each team's last-10 international matches
# Source: API-Football v3 (free tier 100 req/day) with persistent cache
# ---------------------------------------------------------------------------

def _empty_cards(team: str) -> dict:
    return {
        "team": team,
        "window": LAST_N,
        "yellow": 0,
        "red": 0,
        "yellow_avg": None,
        "red_avg": None,
        "matches_with_data": 0,
        "matches": [],
    }


def _load_cards_cache() -> dict:
    if not CARDS_CACHE_FILE.exists():
        return {"team_ids": {}, "fixtures": {}}
    try:
        data = json.loads(CARDS_CACHE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"team_ids": {}, "fixtures": {}}
    data.setdefault("team_ids", {})
    data.setdefault("fixtures", {})
    return data


def _save_cards_cache(cache: dict) -> None:
    CARDS_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CARDS_CACHE_FILE.write_text(
        json.dumps(cache, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _api_football_get(path: str, key: str, params: dict | None = None) -> dict:
    """GET against api-sports.io; fall back to RapidAPI host if needed."""
    try:
        resp = requests.get(
            f"{API_FOOTBALL_BASE}{path}",
            headers={"x-apisports-key": key},
            params=params or {},
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        raise requests.RequestException(f"api-sports.io unreachable: {e}") from e
    if resp.status_code in (401, 403):
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


def _parse_card_stats(stats_response: list) -> dict[int, dict]:
    """Turn /fixtures/statistics response into {team_id: {yellow, red}}."""
    out: dict[int, dict] = {}
    for ts in stats_response or []:
        team_obj = ts.get("team") or {}
        tid = team_obj.get("id")
        if tid is None:
            continue
        yellow = red = 0
        for s in ts.get("statistics") or []:
            typ = (s.get("type") or "").lower()
            val = s.get("value")
            try:
                val = int(val) if val is not None else 0
            except (TypeError, ValueError):
                val = 0
            if "yellow" in typ and "card" in typ:
                yellow = val
            elif "red" in typ and "card" in typ:
                red = val
        out[int(tid)] = {"yellow": yellow, "red": red}
    return out


def _resolve_team_ids(api_key: str, teams: set[str], cache: dict) -> int:
    """Populate cache['team_ids'] for any teams missing an API-Football id.

    Returns the number of API calls consumed.
    """
    missing = [t for t in teams if t not in cache["team_ids"]]
    if not missing:
        return 0

    calls = 0
    # One bulk call covers the 48 WC participants.
    try:
        data = _api_football_get("/teams", api_key,
                                 {"league": 1, "season": 2026})
        calls += 1
    except (requests.RequestException, ValueError) as e:
        print(f"  WARN /teams lookup: {e}", file=sys.stderr)
        return calls

    for r in data.get("response", []) or []:
        tobj = r.get("team") or {}
        tid = tobj.get("id")
        api_name = tobj.get("name") or ""
        if not tid or not api_name:
            continue
        c = canon(api_name)
        if c in teams:
            cache["team_ids"][c] = int(tid)
    time.sleep(API_FOOTBALL_RATE_DELAY)
    return calls


def fetch_cards_last10(api_key: str, teams: set[str]) -> tuple[dict[str, dict], dict]:
    """For each team, sum yellow/red cards across its last-10 fixtures.

    Uses a persistent per-fixture cache so re-runs only spend quota on
    matches we haven't seen before. Returns (per_team_cards, status).
    """
    out = {t: _empty_cards(t) for t in teams}
    status = {
        "calls_used": 0,
        "budget": 0,
        "teams_refreshed": 0,
        "teams_with_data": 0,
        "budget_exhausted": False,
    }
    if not teams:
        return out, status

    budget_raw = os.environ.get("API_FOOTBALL_BUDGET", "").strip()
    try:
        budget = int(budget_raw) if budget_raw else API_FOOTBALL_DEFAULT_BUDGET
    except ValueError:
        budget = API_FOOTBALL_DEFAULT_BUDGET
    status["budget"] = budget

    cache = _load_cards_cache()
    calls = _resolve_team_ids(api_key, teams, cache)
    _save_cards_cache(cache)

    # Process teams alphabetically; persist cache after each one so
    # partial progress survives crashes / quota exhaustion.
    for team in sorted(teams):
        if calls >= budget:
            status["budget_exhausted"] = True
            break

        team_id = cache["team_ids"].get(team)
        if not team_id:
            continue

        # 1) Last-10 fixture ids for this team (any competition, finished)
        try:
            fdata = _api_football_get("/fixtures", api_key,
                                      {"team": team_id, "last": LAST_N})
            calls += 1
        except (requests.RequestException, ValueError) as e:
            print(f"  WARN fixtures {team}: {e}", file=sys.stderr)
            time.sleep(API_FOOTBALL_RATE_DELAY)
            continue
        time.sleep(API_FOOTBALL_RATE_DELAY)

        fixtures_meta: list[dict] = []
        for f in fdata.get("response", []) or []:
            fix = f.get("fixture") or {}
            fid = fix.get("id")
            if not fid:
                continue
            home = (f.get("teams") or {}).get("home") or {}
            away = (f.get("teams") or {}).get("away") or {}
            league = (f.get("league") or {}).get("name") or ""
            fixtures_meta.append({
                "fixture_id": int(fid),
                "date": (fix.get("date") or "")[:10],
                "home_id": home.get("id"),
                "home_name": home.get("name"),
                "away_id": away.get("id"),
                "away_name": away.get("name"),
                "league": league,
            })

        # 2) Per-fixture stats for any not yet cached
        for meta in fixtures_meta:
            fid_str = str(meta["fixture_id"])
            if fid_str in cache["fixtures"]:
                continue
            if calls >= budget:
                status["budget_exhausted"] = True
                break
            try:
                sdata = _api_football_get("/fixtures/statistics", api_key,
                                          {"fixture": meta["fixture_id"]})
                calls += 1
            except (requests.RequestException, ValueError) as e:
                print(f"  WARN stats {meta['fixture_id']}: {e}", file=sys.stderr)
                time.sleep(API_FOOTBALL_RATE_DELAY)
                continue
            time.sleep(API_FOOTBALL_RATE_DELAY)

            per_team = _parse_card_stats(sdata.get("response") or [])
            cache["fixtures"][fid_str] = {
                "date": meta["date"],
                "league": meta["league"],
                "home_id": meta["home_id"],
                "away_id": meta["away_id"],
                # Keep team-id keyed cards as str for JSON safety
                "cards": {str(tid): v for tid, v in per_team.items()},
            }
            _save_cards_cache(cache)

        # 3) Aggregate cached fixtures for this team
        agg = _empty_cards(team)
        for meta in fixtures_meta:
            fid_str = str(meta["fixture_id"])
            entry = cache["fixtures"].get(fid_str)
            if not entry:
                continue
            t_cards = (entry.get("cards") or {}).get(str(team_id))
            if not t_cards:
                continue
            opp_id = meta["away_id"] if meta["home_id"] == team_id else meta["home_id"]
            opp_name = meta["away_name"] if meta["home_id"] == team_id else meta["home_name"]
            agg["yellow"] += int(t_cards.get("yellow") or 0)
            agg["red"] += int(t_cards.get("red") or 0)
            agg["matches_with_data"] += 1
            agg["matches"].append({
                "date": meta["date"],
                "opponent": opp_name or "",
                "league": meta["league"],
                "yellow": int(t_cards.get("yellow") or 0),
                "red": int(t_cards.get("red") or 0),
                "fixtureId": meta["fixture_id"],
            })
        if agg["matches_with_data"]:
            agg["yellow_avg"] = round(agg["yellow"] / agg["matches_with_data"], 2)
            agg["red_avg"] = round(agg["red"] / agg["matches_with_data"], 2)
            status["teams_with_data"] += 1
        out[team] = agg
        status["teams_refreshed"] += 1

    _save_cards_cache(cache)
    status["calls_used"] = calls

    # Even if we ran out of budget mid-run, fill remaining teams from cache
    # so the viewer doesn't lose previously-collected data.
    for team in teams:
        if out[team]["matches_with_data"] > 0:
            continue
        team_id = cache["team_ids"].get(team)
        if not team_id:
            continue
        # Pull any cached fixtures involving this team (best-effort)
        agg = _empty_cards(team)
        rows = []
        for fid_str, entry in cache["fixtures"].items():
            cards = (entry.get("cards") or {}).get(str(team_id))
            if not cards:
                continue
            opp_id = entry.get("away_id") if entry.get("home_id") == team_id else entry.get("home_id")
            rows.append({
                "date": entry.get("date") or "",
                "league": entry.get("league") or "",
                "yellow": int(cards.get("yellow") or 0),
                "red": int(cards.get("red") or 0),
                "fixtureId": int(fid_str),
                "opponent_id": opp_id,
            })
        rows.sort(key=lambda r: r["date"], reverse=True)
        rows = rows[:LAST_N]
        if not rows:
            continue
        for r in rows:
            agg["yellow"] += r["yellow"]
            agg["red"] += r["red"]
            agg["matches_with_data"] += 1
            agg["matches"].append({
                "date": r["date"], "league": r["league"],
                "yellow": r["yellow"], "red": r["red"],
                "fixtureId": r["fixtureId"],
            })
        if agg["matches_with_data"]:
            agg["yellow_avg"] = round(agg["yellow"] / agg["matches_with_data"], 2)
            agg["red_avg"] = round(agg["red"] / agg["matches_with_data"], 2)
            status["teams_with_data"] += 1
        out[team] = agg

    return out, status


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def collect_teams_from_matches() -> set[str]:
    if not MATCHES_FILE.exists():
        return set()
    try:
        payload = json.loads(MATCHES_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    teams: set[str] = set()
    for m in payload.get("matches", []):
        for side in ("homeTeam", "awayTeam"):
            t = (m.get(side) or {}).get("name")
            if t:
                teams.add(canon(t))
    return teams


def main() -> int:
    teams = collect_teams_from_matches()
    if not teams:
        print("WARN: no teams found in matches.json — run fetch_matches first.", file=sys.stderr)

    sources_tried: list[dict] = []

    # 1) Goals from results.csv mirror
    form: dict[str, dict] = {}
    bayesian_url = os.environ.get("BAYESIAN_RESULTS_URL", "").strip()
    if bayesian_url and teams:
        try:
            results = fetch_results_csv(bayesian_url)
            form = compute_form_from_results(results, teams)
            sources_tried.append({"source": "bayesian-results", "ok": True, "rows": len(results)})
        except Exception as e:  # noqa: BLE001
            sources_tried.append({"source": "bayesian-results", "ok": False, "error": str(e)[:200]})
    else:
        sources_tried.append({"source": "bayesian-results", "ok": False, "error": "BAYESIAN_RESULTS_URL not set"})

    if not form:
        form = {t: _empty_form(t) for t in sorted(teams)}

    # 2) Cards (yellow/red) from each team's last-10 international fixtures
    cards: dict[str, dict] = {}
    af_key = os.environ.get("API_FOOTBALL_KEY", "").strip()
    if af_key and teams:
        try:
            cards, cards_status = fetch_cards_last10(af_key, teams)
            sources_tried.append({
                "source": "api-football-cards-last10",
                "ok": True,
                **cards_status,
            })
        except Exception as e:  # noqa: BLE001
            sources_tried.append({
                "source": "api-football-cards-last10",
                "ok": False,
                "error": str(e)[:200],
            })
    else:
        sources_tried.append({
            "source": "api-football-cards-last10",
            "ok": False,
            "error": "API_FOOTBALL_KEY not set",
        })

    teams_payload: dict[str, dict] = {}
    for team in sorted(teams):
        teams_payload[team] = {
            "team": team,
            "form": form.get(team, _empty_form(team)),
            "cards_last10": cards.get(team, _empty_cards(team)),
        }

    payload = {
        "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window": LAST_N,
        "teamCount": len(teams_payload),
        "sourcesTried": sources_tried,
        "teams": teams_payload,
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote stats for {len(teams_payload)} teams to {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
