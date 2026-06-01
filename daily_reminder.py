#!/usr/bin/env python3
"""
daily_reminder.py
=================

Fires push notifications at 5 PM and 9 PM Eastern Time daily.
Each time slot has its own checkbox property in the Tasks database:

  • "Daily 5pm Reminder" → fires at 5 PM ET
  • "Daily 9pm Reminder" → fires at 9 PM ET

Only active (non-Done) tasks with the relevant checkbox ticked get
notified. Adding more time slots is just adding to the REMINDERS list.

Requires: pip install requests
"""

from __future__ import annotations

import datetime
import os
import sys
from typing import Any

import requests

try:
    from zoneinfo import ZoneInfo
    EASTERN: Any = ZoneInfo("America/New_York")
except Exception:
    EASTERN = None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DATA_SOURCE_ID = "122cd9978dee4c0b83ed4722a007a841"
API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"

# Each entry: ET hour to fire, the checkbox property to check, and a label.
# To add another slot (e.g. 8 AM), just append here and add the cron + checkbox.
REMINDERS = [
    {"hour": 11, "property": "Daily 11am Reminder", "label": "11 AM"},
    {"hour": 17, "property": "Daily 5pm Reminder", "label": "5 PM"},
    {"hour": 21, "property": "Daily 9pm Reminder", "label": "9 PM"},
]


def _headers() -> dict[str, str]:
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        sys.stderr.write("ERROR: NOTION_TOKEN is not set.\n")
        sys.exit(1)
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# Notion
# ---------------------------------------------------------------------------

def query_all_tasks() -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        body: dict[str, Any] = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        r = requests.post(
            f"{API_BASE}/data_sources/{DATA_SOURCE_ID}/query",
            headers=_headers(), json=body, timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        tasks.extend(data["results"])
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return tasks


def get_title(task: dict[str, Any]) -> str:
    prop = (task.get("properties") or {}).get("Task") or {}
    arr = prop.get("title") or []
    return "".join((rt.get("plain_text") or "") for rt in arr).strip() or "(untitled)"


def get_checkbox(task: dict[str, Any], name: str) -> bool:
    prop = (task.get("properties") or {}).get(name) or {}
    if prop.get("type") != "checkbox":
        return False
    return bool(prop.get("checkbox"))


def get_status(task: dict[str, Any]) -> str:
    prop = (task.get("properties") or {}).get("Status") or {}
    if prop.get("type") != "status":
        return ""
    return ((prop.get("status") or {}).get("name")) or ""


def get_task_url(task: dict[str, Any]) -> str:
    return task.get("url") or ""


# ---------------------------------------------------------------------------
# ntfy.sh
# ---------------------------------------------------------------------------

def send_ntfy(message: str, title: str, click_url: str | None = None) -> None:
    topic_url = os.environ.get("NTFY_TOPIC_URL", "").strip()
    if not topic_url:
        sys.stderr.write("ERROR: NTFY_TOPIC_URL is not set.\n")
        sys.exit(1)

    headers: dict[str, Any] = {
        "Title": title.encode("utf-8"),
        "Priority": "default",
        "Tags": "bell",
    }
    if click_url:
        headers["Click"] = click_url

    r = requests.post(
        topic_url,
        data=message.encode("utf-8"),
        headers=headers,
        timeout=30,
    )
    if not r.ok:
        sys.stderr.write(f"ntfy.sh failed ({r.status_code}): {r.text}\n")
        r.raise_for_status()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    now_et = datetime.datetime.now(EASTERN) if EASTERN else datetime.datetime.utcnow()
    current_hour = now_et.hour
    print(f"Current Eastern time: {now_et.strftime('%Y-%m-%d %H:%M %Z')}")

    # Find which reminder slot(s) match the current hour.
    active_slots = [r for r in REMINDERS if r["hour"] == current_hour]

    if not active_slots:
        hours = ", ".join(str(r["hour"]) + ":00" for r in REMINDERS)
        print(f"  Current hour ({current_hour}:00) doesn't match any slot ({hours}). Skipping.")
        return

    print("→ Querying tasks…")
    tasks = query_all_tasks()
    print(f"  Found {len(tasks)} tasks.")

    for slot in active_slots:
        label = slot["label"]
        prop = slot["property"]
        print(f"\n→ {label} reminder (checking '{prop}')…")

        flagged = [
            t for t in tasks
            if get_checkbox(t, prop) and get_status(t) != "Done"
        ]

        if not flagged:
            print(f"  No active tasks have '{prop}' checked. Nothing to send.")
            continue

        print(f"  {len(flagged)} task(s) flagged:")
        for t in flagged:
            print(f"   • {get_title(t)}")

        if len(flagged) == 1:
            t = flagged[0]
            send_ntfy(
                message=get_title(t),
                title=f"📌 {label} reminder",
                click_url=get_task_url(t) or None,
            )
        else:
            lines = ["You have these tasks to do:"]
            for t in flagged:
                lines.append(f"• {get_title(t)}")
            send_ntfy(
                message="\n".join(lines),
                title=f"📌 {label} reminder — {len(flagged)} tasks",
            )

        print(f"  Sent. ✓")


if __name__ == "__main__":
    main()

