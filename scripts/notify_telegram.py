"""
Send a Telegram message for each match that newly transitioned to FINISHED
between two snapshots of matches.json.

Usage:
    python scripts/notify_telegram.py <previous.json> <current.json>

Required env vars (if missing, the script exits 0 silently — used as no-op
when Telegram notifications aren't configured):
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests

TIMEOUT = 15


def load(path: Path) -> dict:
    if not path.exists():
        return {"matches": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"matches": []}


def by_id(payload: dict) -> dict[int, dict]:
    return {m["id"]: m for m in payload.get("matches", []) if m.get("id") is not None}


def format_message(m: dict) -> str:
    home = (m.get("homeTeam") or {}).get("name") or "TBD"
    away = (m.get("awayTeam") or {}).get("name") or "TBD"
    score = m.get("score") or {}
    h, a = score.get("home"), score.get("away")
    result = f"{h} – {a}" if h is not None and a is not None else "vs"

    parts = [f"\U0001F3C6 <b>{home}</b> {result} <b>{away}</b>"]
    stage = m.get("stage")
    group = m.get("group")
    bits: list[str] = []
    if stage:
        bits.append(stage.replace("_", " ").title())
    if group:
        bits.append(group.replace("_", " ").title())
    if m.get("venue"):
        bits.append(m["venue"])
    if bits:
        parts.append(" · ".join(bits))

    winner = score.get("winner")
    if winner == "HOME_TEAM":
        parts.append(f"\u2705 Ganó {home}")
    elif winner == "AWAY_TEAM":
        parts.append(f"\u2705 Ganó {away}")
    elif winner == "DRAW":
        parts.append("\U0001F91D Empate")

    return "\n".join(parts)


def send(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(
        url,
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        print(f"Telegram error {resp.status_code}: {resp.text}", file=sys.stderr)


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: notify_telegram.py <previous.json> <current.json>", file=sys.stderr)
        return 64

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("Telegram env vars not set — skipping notifications.")
        return 0

    prev = by_id(load(Path(sys.argv[1])))
    curr = by_id(load(Path(sys.argv[2])))

    newly_finished: list[dict] = []
    for mid, m in curr.items():
        if m.get("status") != "FINISHED":
            continue
        prev_status = (prev.get(mid) or {}).get("status")
        if prev_status != "FINISHED":
            newly_finished.append(m)

    if not newly_finished:
        print("No newly finished matches — nothing to notify.")
        return 0

    # Stable ordering by kickoff time.
    newly_finished.sort(key=lambda m: m.get("utcDate") or "")

    for m in newly_finished:
        try:
            send(token, chat_id, format_message(m))
        except requests.RequestException as e:
            print(f"Network error sending Telegram message: {e}", file=sys.stderr)

    print(f"Sent {len(newly_finished)} Telegram notification(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
