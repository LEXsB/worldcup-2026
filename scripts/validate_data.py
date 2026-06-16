"""
Batch validator for the WC2026 data files.

Compares the previous and current versions of:
    data/matches.json      data/odds.json
    data/team_stats.json   data/injuries.json

It checks:
- Schema sanity (expected keys present)
- Reasonable counts (matches > 0, etc.)
- Date sanity (within 2026-06-01 .. 2026-07-31)
- Odds drift > 50% on the same fixture between runs
- Source failures reported by the fetchers

Outputs:
    data/validation_report.json   (always)
    Exit code 0 always (workflow continues either way)

If anomalies exist AND a GitHub Issue with the same fingerprint is not already
open, the script creates one via the GitHub REST API. Requires GH_TOKEN +
GITHUB_REPOSITORY env vars (provided by Actions automatically).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
PREV_DIR = Path(os.environ.get("WC_PREV_DIR", "/tmp/wc/prev"))
REPORT = DATA_DIR / "validation_report.json"

WC_DATE_MIN = "2026-06-01"
WC_DATE_MAX = "2026-07-31"
ODDS_DRIFT_THRESHOLD = 0.50  # 50% relative change is suspicious


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _check_matches(curr: dict, anomalies: list[dict]) -> dict:
    matches = curr.get("matches") or []
    summary = {"count": len(matches), "out_of_window": 0, "missing_status": 0,
               "finished": 0, "scheduled": 0}
    if not matches:
        anomalies.append({"area": "matches", "level": "warn",
                          "msg": "matches.json contains 0 matches"})
        return summary
    for m in matches:
        d = (m.get("utcDate") or "")[:10]
        status = m.get("status") or ""
        if status == "FINISHED":
            summary["finished"] += 1
        elif status in ("SCHEDULED", "TIMED"):
            summary["scheduled"] += 1
        else:
            summary["missing_status"] += 1
        if d and not (WC_DATE_MIN <= d <= WC_DATE_MAX):
            summary["out_of_window"] += 1
    if summary["out_of_window"]:
        anomalies.append({
            "area": "matches", "level": "warn",
            "msg": f"{summary['out_of_window']} matches outside {WC_DATE_MIN}..{WC_DATE_MAX}",
        })
    return summary


def _check_odds(curr: dict, prev: dict, anomalies: list[dict]) -> dict:
    odds = curr.get("odds") or []
    summary = {"count": len(odds), "source": curr.get("source"),
               "sources_failed": [], "drift_warnings": 0}
    for s in curr.get("sourcesTried") or []:
        if not s.get("ok"):
            summary["sources_failed"].append(s.get("source"))
    if not odds:
        anomalies.append({"area": "odds", "level": "warn",
                          "msg": "no odds collected from any source"})
        return summary
    if curr.get("source") != prev.get("source") and prev.get("source"):
        anomalies.append({
            "area": "odds", "level": "info",
            "msg": f"odds source changed: {prev.get('source')} → {curr.get('source')}",
        })

    prev_idx = {(o["date"], o["home"], o["away"]): o for o in (prev.get("odds") or [])}
    for o in odds:
        key = (o.get("date"), o.get("home"), o.get("away"))
        p = prev_idx.get(key)
        if not p:
            continue
        for col in ("home_odds", "draw_odds", "away_odds"):
            old, new = p.get(col), o.get(col)
            if not (old and new and old > 1.0 and new > 1.0):
                continue
            drift = abs(new - old) / old
            if drift > ODDS_DRIFT_THRESHOLD:
                summary["drift_warnings"] += 1
                if summary["drift_warnings"] <= 5:
                    anomalies.append({
                        "area": "odds", "level": "warn",
                        "msg": (f"{key[0]} {key[1]} vs {key[2]}: {col} "
                                f"{old} → {new} (drift {drift*100:.0f}%)"),
                    })
    return summary


def _check_team_stats(curr: dict, anomalies: list[dict]) -> dict:
    teams = curr.get("teams") or {}
    no_form = sum(1 for t in teams.values()
                  if not (t.get("form") or {}).get("matches"))
    no_cards = sum(1 for t in teams.values()
                   if not (t.get("wc_bookings") or {}).get("matches_with_data"))
    summary = {"team_count": len(teams),
               "teams_without_form": no_form,
               "teams_without_card_data": no_cards}
    if teams and no_form == len(teams):
        anomalies.append({"area": "team_stats", "level": "warn",
                          "msg": "no team has recent-form data — check BAYESIAN_RESULTS_URL"})
    return summary


def _check_injuries(curr: dict, anomalies: list[dict]) -> dict:
    teams = curr.get("teams") or {}
    with_data = sum(1 for v in teams.values() if v)
    summary = {"team_count": len(teams), "teams_with_injuries": with_data,
               "source": curr.get("source")}
    if not curr.get("source"):
        anomalies.append({"area": "injuries", "level": "info",
                          "msg": "no injury source configured (set API_FOOTBALL_KEY)"})
    return summary


def _fingerprint(anomalies: list[dict]) -> str:
    h = hashlib.sha256()
    for a in anomalies:
        h.update((a.get("area", "") + "|" + a.get("level", "") + "|"
                  + a.get("msg", "") + "\n").encode("utf-8"))
    return h.hexdigest()[:12]


def _open_issue_if_needed(anomalies: list[dict], summary: dict) -> dict:
    """Create a GitHub Issue if any 'warn'/'error' anomalies exist."""
    severe = [a for a in anomalies if a.get("level") in ("warn", "error")]
    if not severe:
        return {"opened": False, "reason": "no severe anomalies"}

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        return {"opened": False, "reason": "no GH_TOKEN/GITHUB_REPOSITORY"}

    fp = _fingerprint(severe)
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # Skip if an open issue with this fingerprint already exists.
    try:
        existing = requests.get(
            f"https://api.github.com/repos/{repo}/issues",
            headers=headers,
            params={"state": "open", "labels": "data-anomaly"},
            timeout=30,
        )
        existing.raise_for_status()
        for issue in existing.json() or []:
            if f"fp:{fp}" in (issue.get("body") or ""):
                return {"opened": False, "reason": "duplicate (issue #" + str(issue.get("number")) + ")"}
    except requests.RequestException as e:
        return {"opened": False, "reason": f"list-issues failed: {e}"}

    body_lines = [
        "Automated data validation detected anomalies in the latest fetch.",
        "",
        "**Summary**",
        "```json",
        json.dumps(summary, indent=2, ensure_ascii=False),
        "```",
        "",
        "**Anomalies**",
    ]
    for a in severe:
        body_lines.append(f"- `{a.get('area')}` [{a.get('level')}] {a.get('msg')}")
    body_lines += ["", f"<!-- fp:{fp} -->"]

    payload = {
        "title": f"[data-anomaly] {len(severe)} issue(s) in WC2026 fetch — fp:{fp}",
        "body": "\n".join(body_lines),
        "labels": ["data-anomaly"],
    }
    try:
        resp = requests.post(
            f"https://api.github.com/repos/{repo}/issues",
            headers=headers, json=payload, timeout=30,
        )
        resp.raise_for_status()
        return {"opened": True, "number": resp.json().get("number"), "fp": fp}
    except requests.RequestException as e:
        return {"opened": False, "reason": f"create failed: {e}"}


def main() -> int:
    anomalies: list[dict] = []
    summary: dict[str, Any] = {}

    files = [
        ("matches",     "matches.json"),
        ("odds",        "odds.json"),
        ("team_stats",  "team_stats.json"),
        ("injuries",    "injuries.json"),
    ]

    curr_data: dict[str, dict] = {}
    prev_data: dict[str, dict] = {}
    for area, fname in files:
        curr_data[area] = _load(DATA_DIR / fname)
        prev_data[area] = _load(PREV_DIR / fname)

    summary["matches"]    = _check_matches(curr_data["matches"], anomalies)
    summary["odds"]       = _check_odds(curr_data["odds"], prev_data["odds"], anomalies)
    summary["team_stats"] = _check_team_stats(curr_data["team_stats"], anomalies)
    summary["injuries"]   = _check_injuries(curr_data["injuries"], anomalies)

    issue_result = _open_issue_if_needed(anomalies, summary)

    report = {
        "checkedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "anomalyCount": len(anomalies),
        "anomalies": anomalies,
        "summary": summary,
        "issue": issue_result,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                      encoding="utf-8")
    print(f"Validation report: {len(anomalies)} anomaly/anomalies. issue={issue_result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
