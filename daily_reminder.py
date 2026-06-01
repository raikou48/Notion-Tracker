#!/usr/bin/env python3
"""
daily_reminder.py
=================

Fires once a day at 5:00 PM Eastern Time. Queries the Tasks data source,
finds every task with "Daily 5pm Reminder" checked AND Status != "Done",
and sends a push notification via ntfy.sh.

Workflow notes:
  • Scheduled at both 21:00 UTC and 22:00 UTC so the script catches 5pm
    ET regardless of daylight saving. The hour-gate below ensures only
    the run that lands exactly on hour 17 ET actually sends.
  • If no tasks are flagged, no notification is sent.

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

REMINDER_PROPERTY = "Daily 5pm Reminder"
TITLE_PROPERTY = "Task"
STATUS_PROPERTY = "Status"

TARGET_HOUR_ET = 17  # 5pm Eastern


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
            headers=_headers(),
            json=body,
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        tasks.extend(data["results"])
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return tasks


def get_title(task: dict[str, Any]) -> str:
    prop = (task.get("properties") or {}).get(TITLE_PROPERTY) or {}
    arr = prop.get("title") or []
    return "".join((rt.get("plain_text") or "") for rt in arr).strip() or "(untitled task)"


def get_checkbox(task: dict[str, Any], name: str) -> bool:
    prop = (task.get("properties") or {}).get(name) or {}
    if prop.get("type") != "checkbox":
        return False
    return bool(prop.get("checkbox"))


def get_status(task: dict[str, Any]) -> str:
    prop = (task.get("properties") or {}).get(STATUS_PROPERTY) or {}
    if prop.get("type") != "status":
        return ""
    return ((prop.get("status") or {}).get("name")) or ""


def get_task_url(task: dict[str, Any]) -> str:
    return task.get("url") or ""


# ---------------------------------------------------------------------------
# ntfy.sh push
# ---------------------------------------------------------------------------

def send_ntfy(message: str, title: str, click_url: str | None = None) -> None:
    topic_url = os.environ.get("NTFY_TOPIC_URL", "").strip()
    if not topic_url:
        sys.stderr.write("ERROR: NTFY_TOPIC_URL is not set.\n")
        sys.exit(1)

    headers = {
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
    if EASTERN:
        now_et = datetime.datetime.now(EASTERN)
    else:
        now_et = datetime.datetime.utcnow()

    print(f"Current Eastern time: {now_et.strftime('%Y-%m-%d %H:%M %Z')}")

    if now_et.hour != TARGET_HOUR_ET:
        print(f"  Not the {TARGET_HOUR_ET}:00 ET hour. Skipping (this is normal — the "
              f"other scheduled run handles the active timezone offset).")
        return

    print("→ Querying tasks…")
    tasks = query_all_tasks()
    print(f"  Found {len(tasks)} tasks.")

    flagged = [
        t for t in tasks
        if get_checkbox(t, REMINDER_PROPERTY) and get_status(t) != "Done"
    ]

    if not flagged:
        print("  No active tasks have a Daily 5pm Reminder. Nothing to send.")
        return

    print(f"  {len(flagged)} task(s) flagged for reminder.")
    for t in flagged:
        print(f"   • {get_title(t)}")

    if len(flagged) == 1:
        t = flagged[0]
        send_ntfy(
            message=get_title(t),
            title="📌 Daily reminder",
            click_url=get_task_url(t) or None,
        )
    else:
        lines = ["You have these tasks to do:"]
        for t in flagged:
            lines.append(f"• {get_title(t)}")
        send_ntfy(
            message="\n".join(lines),
            title=f"📌 Daily reminder — {len(flagged)} tasks",
        )

    print("  Sent. ✓")


if __name__ == "__main__":
    main()

