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
# Wikipedia consolidated squads page (MediaWiki API → wikitext)
# ---------------------------------------------------------------------------

WIKI_API = "https://en.wikipedia.org/w/api.php"
WIKI_SQUADS_PAGE = "2026_FIFA_World_Cup_squads"


def fetch_wikipedia_squads_page(teams: list[str]) -> dict[str, list[dict]]:
    """Parse the consolidated squads article via MediaWiki API.

    Strategy: download raw wikitext (way more stable than HTML scraping) and
    look for replacement notes per-team. Wikipedia uses a fairly consistent
    format like:
        '''Team Name'''
        ...
        Replaces: ''Player A'' (injury) was replaced by '''Player B''' on...
    """
    out: dict[str, list[dict]] = {t: [] for t in teams}
    try:
        resp = requests.get(WIKI_API, params={
            "action": "parse",
            "page": WIKI_SQUADS_PAGE,
            "prop": "wikitext",
            "format": "json",
            "formatversion": "2",
        }, timeout=TIMEOUT, headers={"User-Agent": "wc2026-bot/1.0"})
        resp.raise_for_status()
        data = resp.json() or {}
    except (requests.RequestException, ValueError) as e:
        print(f"  WARN wiki-squads: {e}", file=sys.stderr)
        return out

    text = ((data.get("parse") or {}).get("wikitext") or "")
    if not text:
        return out

    # Build a lookup: lowercased team variants -> canonical
    variants_lookup: dict[str, str] = {}
    for canon_name in teams:
        variants_lookup[canon_name.lower()] = canon_name
    for canon_name, variants in _ALIASES.items():
        if canon_name not in teams:
            continue
        if isinstance(variants, list):
            for v in variants:
                variants_lookup[v.lower()] = canon_name

    # Section headers in this article are level-3: "===Team Name==="
    # Capture each team's chunk of wikitext.
    sections = re.split(r"\n===\s*([^=]+?)\s*===\n", text)
    # sections = [preamble, h1_name, h1_body, h2_name, h2_body, ...]
    for i in range(1, len(sections), 2):
        header = sections[i].strip()
        body = sections[i + 1] if i + 1 < len(sections) else ""
        canon_team = variants_lookup.get(header.lower())
        if not canon_team:
            continue

        # Look for explicit replacement / injury / withdrawal patterns.
        notes: list[dict] = []
        seen_notes: set[str] = set()

        def add_note(player: str | None, raw: str) -> None:
            # Clean wikitext: [[A|B]] -> B, [[A]] -> A, refs, templates, html
            clean = re.sub(r"\{\{[^}]+\}\}", "", raw)
            clean = re.sub(r"<ref[^>]*>.*?</ref>", "", clean, flags=re.S)
            clean = re.sub(r"<ref[^>]*/\s*>", "", clean)
            clean = re.sub(r"\[\[[^|\]]*\|([^\]]+)\]\]", r"\1", clean)
            clean = re.sub(r"\[\[([^\]]+)\]\]", r"\1", clean)
            clean = re.sub(r"\[https?://[^\s\]]+\s+([^\]]+)\]", r"\1", clean)
            clean = re.sub(r"\[https?://[^\s\]]+\]", "", clean)
            clean = re.sub(r"<[^>]+>", "", clean)
            clean = re.sub(r"'{2,}", "", clean)
            clean = re.sub(r"\s+", " ", clean).strip().rstrip(".,;:")
            if not (25 < len(clean) < 280):
                return
            if clean in seen_notes:
                return
            # Filter out reference-style residues
            if re.search(r"^(retrieved|archived from)", clean, re.I):
                return
            seen_notes.add(clean)
            entry: dict = {"note": clean, "source": "wikipedia-squads"}
            # Only attach player name if it looks like a real proper noun:
            # at least 2 words, capitalized, not generic English glue.
            if player:
                p = re.sub(r"\s+", " ", player).strip().rstrip(",.;:")
                # Reject common wikitext glue captured by greedy regex
                if p and not re.match(
                    r"^(and|but|the|on|in|with|by|withdrew|injured|replaced)\b",
                    p, re.I,
                ) and " " in p and len(p) < 60:
                    entry["player"] = p
            notes.append(entry)

        # Pattern 1: "FirstName LastName was replaced by ..."
        # Anchor on a name candidate that's NOT "and|but|...".
        for m in re.finditer(
            r"(?<![\w\-])([A-ZÀ-Ž][\wÀ-ž'\-]+(?:\s+[A-ZÀ-Ž][\wÀ-ž'\-]+){1,3})"
            r"\s+(?:withdrew\s+(?:injured\s+)?|was\s+(?:replaced|ruled\s+out))"
            r"[^.\n]{0,220}",
            body,
        ):
            add_note(m.group(1), m.group(0))

        # Pattern 2: standalone sentences that mention injury/withdrawal but
        # didn't match the structured pattern above.
        for sentence in re.split(r"(?<=[.\n])\s+", body):
            if re.search(r"\b(injury|injuries|withdrew|ruled\s+out|sidelined)\b",
                         sentence, re.I):
                add_note(None, sentence)
            if len(notes) >= 8:
                break

        if notes:
            out[canon_team] = notes

    return out


# ---------------------------------------------------------------------------
# OpenFootball: openfootball/world-cup community-maintained squads
# ---------------------------------------------------------------------------

OPENFOOTBALL_RAW = "https://raw.githubusercontent.com/openfootball/world-cup/master/2026/squads/wc.csv"


def fetch_openfootball_squads(teams: list[str]) -> dict[str, list[dict]]:
    """OpenFootball maintains squads in plain CSV (community-driven).

    The CSV layout varies between editions; for 2026 it commonly has
    columns like team,player,position,club,note. If the file or layout
    isn't there we just return empty (graceful fallback).
    """
    out: dict[str, list[dict]] = {t: [] for t in teams}
    try:
        resp = requests.get(OPENFOOTBALL_RAW, timeout=TIMEOUT,
                            headers={"User-Agent": "wc2026-bot/1.0"})
        if resp.status_code == 404:
            return out  # not yet published, that's fine
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  WARN openfootball: {e}", file=sys.stderr)
        return out

    import csv
    import io
    rows = list(csv.DictReader(io.StringIO(resp.text)))
    if not rows:
        return out

    notes_keys = {"note", "notes", "status", "comment"}
    for r in rows:
        team_raw = r.get("team") or r.get("Team") or ""
        canon_team = canon(team_raw)
        if canon_team not in out:
            continue
        # Find any free-text note column
        note_text = ""
        for k, v in r.items():
            if v and k.lower() in notes_keys:
                note_text = str(v).strip()
                break
        if not note_text:
            continue
        if not re.search(r"injur|withdrew|ruled out|replaced|out\s+of\b|baja",
                         note_text, re.I):
            continue
        out[canon_team].append({
            "player": r.get("player") or r.get("Player"),
            "note": note_text[:240],
            "source": "openfootball",
        })
    return out


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
    """Fetch all WC injuries with only 2 API calls (saves free-tier quota).

    Strategy:
        1. /teams?league=1&season=2026 → returns the 48 WC teams with their ids.
        2. /injuries?league=1&season=2026 → returns all injuries reported for
           those WC fixtures.

    The Wikipedia/results.csv data we already have uses football-data.org names
    (e.g. "Czechia", "Congo DR"). API-Football uses slightly different names
    ("Czech Republic", "DR Congo"), so we apply canon() in both directions.
    """
    out: dict[str, list[dict]] = {t: [] for t in teams}
    wanted_canon = {canon(t) for t in teams}

    # 1) Map team-id -> canonical name
    LEAGUE_WC = 1
    SEASON = 2026
    try:
        data = _api_football_get("/teams", key,
                                 {"league": LEAGUE_WC, "season": SEASON})
    except requests.HTTPError as e:
        print(f"  WARN teams lookup: {e}", file=sys.stderr)
        return out

    id_to_team: dict[int, str] = {}
    for r in data.get("response", []) or []:
        t = (r.get("team") or {})
        tid = t.get("id")
        api_name = t.get("name") or ""
        if not tid or not api_name:
            continue
        # Try the API name; if not in our list, try canonicalizing.
        c = canon(api_name)
        if c in wanted_canon:
            id_to_team[int(tid)] = c
        else:
            # Some API-Football names need extra normalization (e.g. "USA").
            for variant_can, variants in _ALIASES.items():
                if api_name == variants:  # safety
                    if variant_can in wanted_canon:
                        id_to_team[int(tid)] = variant_can
                        break

    if not id_to_team:
        print("  WARN: no WC teams matched between football-data and API-Football",
              file=sys.stderr)
        return out

    time.sleep(RATE_DELAY)

    # 2) All injuries for the WC season
    try:
        data = _api_football_get("/injuries", key,
                                 {"league": LEAGUE_WC, "season": SEASON})
    except requests.HTTPError as e:
        print(f"  WARN injuries: {e}", file=sys.stderr)
        return out

    for r in data.get("response", []) or []:
        team_obj = (r.get("team") or {})
        tid = team_obj.get("id")
        if tid is None:
            continue
        team_name = id_to_team.get(int(tid))
        if not team_name:
            continue
        player = (r.get("player") or {})
        fixture = (r.get("fixture") or {})
        out.setdefault(team_name, []).append({
            "player": player.get("name"),
            "type": player.get("type"),
            "reason": player.get("reason"),
            "fixtureDate": (fixture.get("date") or "")[:10],
        })

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
    used_sources: list[str] = []
    by_team: dict[str, list[dict]] = {t: [] for t in teams}

    api_key = os.environ.get("API_FOOTBALL_KEY", "").strip()
    wiki_fallback = os.environ.get("WIKI_INJURIES_FALLBACK", "").strip() == "1"

    def merge(name: str, results: dict[str, list[dict]]) -> int:
        """Add new findings without overwriting existing ones (dedup by note)."""
        added = 0
        for team, items in (results or {}).items():
            if team not in by_team:
                continue
            existing_keys = {(it.get("player"), it.get("note") or "") for it in by_team[team]}
            for it in items:
                key = (it.get("player"), it.get("note") or "")
                if key in existing_keys:
                    continue
                # Tag the source so the viewer can show provenance
                it.setdefault("source", name)
                by_team[team].append(it)
                existing_keys.add(key)
                added += 1
        return added

    # 1) API-Football (best when available — provides player + reason structured)
    if api_key and teams:
        try:
            res = fetch_api_football(api_key, teams)
            added = merge("api-football", res)
            sources_tried.append({"source": "api-football", "ok": True,
                                  "items_added": added})
            if added > 0:
                used_sources.append("api-football")
        except Exception as e:  # noqa: BLE001
            sources_tried.append({"source": "api-football", "ok": False,
                                  "error": str(e)[:200]})
    else:
        sources_tried.append({"source": "api-football", "ok": False,
                              "error": "API_FOOTBALL_KEY not set"})

    # 2) Wikipedia consolidated squads page (free, no key, all 48 teams in one fetch)
    if teams:
        try:
            res = fetch_wikipedia_squads_page(teams)
            added = merge("wikipedia-squads", res)
            sources_tried.append({"source": "wikipedia-squads", "ok": True,
                                  "items_added": added})
            if added > 0:
                used_sources.append("wikipedia-squads")
        except Exception as e:  # noqa: BLE001
            sources_tried.append({"source": "wikipedia-squads", "ok": False,
                                  "error": str(e)[:200]})

    # 3) OpenFootball community CSV (free, no key)
    if teams:
        try:
            res = fetch_openfootball_squads(teams)
            added = merge("openfootball", res)
            sources_tried.append({"source": "openfootball", "ok": True,
                                  "items_added": added})
            if added > 0:
                used_sources.append("openfootball")
        except Exception as e:  # noqa: BLE001
            sources_tried.append({"source": "openfootball", "ok": False,
                                  "error": str(e)[:200]})

    # 4) Wikipedia per-team article fallback (more aggressive, gated by env var)
    if wiki_fallback and teams:
        try:
            res = fetch_wikipedia_fallback(teams)
            added = merge("wikipedia-team-pages", res)
            sources_tried.append({"source": "wikipedia-team-pages", "ok": True,
                                  "items_added": added})
            if added > 0:
                used_sources.append("wikipedia-team-pages")
        except Exception as e:  # noqa: BLE001
            sources_tried.append({"source": "wikipedia-team-pages", "ok": False,
                                  "error": str(e)[:200]})

    used_source: str | None = "+".join(used_sources) if used_sources else None

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
