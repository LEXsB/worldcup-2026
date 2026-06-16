"""
Build per-team last-10 stats for the FIFA World Cup 2026 viewer.

Goals (scored/conceded, W-D-L) come from a mirror of the bayesian_model
public results dataset (set BAYESIAN_RESULTS_URL to its raw URL).

Cards (yellow / red) and bookings come from football-data.org per-match
detail endpoint, but ONLY for matches in the WC competition itself, since
historical bookings require per-match queries that don't fit the free-tier
quota for ~30 teams x 10 matches each. We therefore expose:

    goals_last10  — based on results.csv mirror (avg/total/W-D-L)
    wc_bookings   — accumulated from this WC's matches (yellows, reds)

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
TIMEOUT = 30
LAST_N = 10
FOOTBALL_DATA_BASE = "https://api.football-data.org/v4"
RATE_DELAY = 6.5  # 10 req/min free tier


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
# WC bookings (yellow/red cards) from football-data.org per-match detail
# ---------------------------------------------------------------------------

def fetch_wc_bookings(api_key: str, teams: set[str]) -> dict[str, dict]:
    """Accumulate yellow/red cards per team across this WC's finished matches."""
    bookings = {t: {
        "yellow": 0, "red": 0, "second_yellow": 0,
        "yellow_avg": None, "matches_with_data": 0, "matches": [],
    } for t in teams}

    if not MATCHES_FILE.exists():
        return bookings

    try:
        matches_payload = json.loads(MATCHES_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return bookings

    finished = [m for m in matches_payload.get("matches", []) if m.get("status") == "FINISHED" and m.get("id")]
    if not finished:
        return bookings

    headers = {"X-Auth-Token": api_key}
    for m in finished:
        try:
            resp = requests.get(
                f"{FOOTBALL_DATA_BASE}/matches/{m['id']}",
                headers=headers, timeout=TIMEOUT,
            )
        except requests.RequestException as e:
            print(f"  WARN match {m['id']}: {e}", file=sys.stderr)
            time.sleep(RATE_DELAY)
            continue

        if resp.status_code == 429:
            time.sleep(60)
            continue
        if resp.status_code != 200:
            time.sleep(RATE_DELAY)
            continue

        data = resp.json() or {}
        match = data.get("match") or data
        per_match: dict[str, dict] = defaultdict(lambda: {"yellow": 0, "red": 0, "second_yellow": 0})
        for b in match.get("bookings", []) or []:
            team_name = canon(((b.get("team") or {}).get("name")) or "")
            if not team_name:
                continue
            card = (b.get("card") or "").upper()
            if card == "YELLOW_CARD":
                per_match[team_name]["yellow"] += 1
            elif card == "RED_CARD":
                per_match[team_name]["red"] += 1
            elif card == "YELLOW_RED_CARD":
                per_match[team_name]["second_yellow"] += 1
                per_match[team_name]["red"] += 1

        for team, counts in per_match.items():
            if team not in bookings:
                bookings[team] = {"yellow": 0, "red": 0, "second_yellow": 0,
                                  "yellow_avg": None, "matches_with_data": 0, "matches": []}
            bookings[team]["yellow"] += counts["yellow"]
            bookings[team]["red"] += counts["red"]
            bookings[team]["second_yellow"] += counts["second_yellow"]
            bookings[team]["matches_with_data"] += 1
            bookings[team]["matches"].append({
                "date": (m.get("utcDate") or "")[:10],
                "matchId": m.get("id"),
                **counts,
            })

        time.sleep(RATE_DELAY)

    for team, b in bookings.items():
        if b["matches_with_data"]:
            b["yellow_avg"] = round(b["yellow"] / b["matches_with_data"], 2)
    return bookings


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

    # 2) Cards from football-data.org WC matches
    bookings: dict[str, dict] = {}
    fd_key = os.environ.get("FOOTBALL_DATA_API_KEY", "").strip()
    if fd_key and teams:
        try:
            bookings = fetch_wc_bookings(fd_key, teams)
            covered = sum(1 for b in bookings.values() if b["matches_with_data"])
            sources_tried.append({"source": "fd-bookings", "ok": True, "teams_with_data": covered})
        except Exception as e:  # noqa: BLE001
            sources_tried.append({"source": "fd-bookings", "ok": False, "error": str(e)[:200]})
    else:
        sources_tried.append({"source": "fd-bookings", "ok": False, "error": "FOOTBALL_DATA_API_KEY not set"})

    teams_payload: dict[str, dict] = {}
    for team in sorted(teams):
        teams_payload[team] = {
            "team": team,
            "form": form.get(team, _empty_form(team)),
            "wc_bookings": bookings.get(team, {
                "yellow": 0, "red": 0, "second_yellow": 0,
                "yellow_avg": None, "matches_with_data": 0, "matches": [],
            }),
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
