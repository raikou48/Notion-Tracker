#!/usr/bin/env python3
"""
refresh_tracker.py
==================

Refreshes the "Task Status Tracker" toggle on the Task section
(Academics and Personal) page in Notion.

Uses the Notion API version 2025-09-03 with the data sources endpoint.

Reads the "Status" property (not "Status (1)") and maps its four values
into the three tracker buckets:

    To do                  → Not started
    To do- daily/weekly    → Not started   (recurring tasks)
    In progress            → In progress
    Archive                → Done

Requires:
    pip install requests
"""

from __future__ import annotations

import datetime
import os
import sys
from typing import Any

import requests

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ: Any = ZoneInfo("America/New_York")
except Exception:
    LOCAL_TZ = None


# ---------------------------------------------------------------------------
# Configuration — these IDs are specific to your Notion workspace.
# ---------------------------------------------------------------------------

# The Task DATA SOURCE (the table inside the database container).
DATA_SOURCE_ID = "16727f2c884583a6bf398738a4305ada"

# The "Task section (Academics and Personal)" page that holds the toggle.
PARENT_PAGE_ID = "f9c27f2c8845823a837201565a531822"

# Substring used to identify the toggle to update on the parent page.
TOGGLE_TITLE_CONTAINS = "Task Status Tracker"

# Which Notion status property to read.
STATUS_PROPERTY = "Status"

# Maps the raw Notion status values into the three tracker buckets.
# Any status value not in this map is ignored entirely.
STATUS_MAPPING: dict[str, str] = {
    "To do": "Not started",
    "To do- daily/weekly": "Not started",
    "In progress": "In progress",
    "Archive": "Done",
}

# The three tracker buckets in display order.
BUCKETS = ["Not started", "In progress", "Done"]


# ---------------------------------------------------------------------------
# Notion API helpers
# ---------------------------------------------------------------------------

API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"


def _headers() -> dict[str, str]:
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        sys.stderr.write(
            "ERROR: NOTION_TOKEN environment variable is not set. "
            "Add it as a GitHub Actions secret or export it locally.\n"
        )
        sys.exit(1)
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def query_all_tasks() -> list[dict[str, Any]]:
    """Return every page in the task data source (handles pagination)."""
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
        if not r.ok:
            sys.stderr.write(f"Query failed ({r.status_code}): {r.text}\n")
        r.raise_for_status()
        data = r.json()
        tasks.extend(data["results"])
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return tasks


def count_by_status(tasks: list[dict[str, Any]]) -> dict[str, int]:
    counts = {bucket: 0 for bucket in BUCKETS}
    unknown: list[str] = []
    for t in tasks:
        prop = (t.get("properties") or {}).get(STATUS_PROPERTY) or {}
        if prop.get("type") == "status":
            status_obj = prop.get("status") or {}
            name = status_obj.get("name")
            bucket = STATUS_MAPPING.get(name) if name else None
            if bucket:
                counts[bucket] += 1
            elif name:
                unknown.append(name)
    if unknown:
        sys.stderr.write(
            f"Note: {len(unknown)} task(s) had unmapped status values: "
            f"{sorted(set(unknown))}\n"
        )
    return counts


def list_block_children(block_id: str) -> list[dict[str, Any]]:
    children: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        url = f"{API_BASE}/blocks/{block_id}/children?page_size=100"
        if cursor:
            url += f"&start_cursor={cursor}"
        r = requests.get(url, headers=_headers(), timeout=30)
        r.raise_for_status()
        data = r.json()
        children.extend(data["results"])
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return children


def delete_block(block_id: str) -> None:
    r = requests.delete(
        f"{API_BASE}/blocks/{block_id}", headers=_headers(), timeout=30
    )
    r.raise_for_status()


def append_children(
    block_id: str, children: list[dict[str, Any]]
) -> dict[str, Any]:
    r = requests.patch(
        f"{API_BASE}/blocks/{block_id}/children",
        headers=_headers(),
        json={"children": children},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def find_tracker_toggle() -> str:
    """Locate the heading_3 toggle block whose title contains the tracker name."""
    for block in list_block_children(PARENT_PAGE_ID):
        if block.get("type") == "heading_3":
            h = block.get("heading_3", {})
            if h.get("is_toggleable"):
                text = "".join(
                    rt.get("plain_text", "") for rt in h.get("rich_text", [])
                )
                if TOGGLE_TITLE_CONTAINS in text:
                    return block["id"]
    raise RuntimeError(
        f"Could not find a toggle heading containing "
        f"'{TOGGLE_TITLE_CONTAINS}' on page {PARENT_PAGE_ID}. "
        f"Has it been moved or renamed?"
    )


# ---------------------------------------------------------------------------
# Block construction helpers
# ---------------------------------------------------------------------------

def _text(content: str, bold: bool = False) -> dict[str, Any]:
    rt: dict[str, Any] = {"type": "text", "text": {"content": content}}
    if bold:
        rt["annotations"] = {"bold": True}
    return rt


def _paragraph(rich_text: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "paragraph", "paragraph": {"rich_text": rich_text}}


def _heading_1(content: str) -> dict[str, Any]:
    return {
        "type": "heading_1",
        "heading_1": {"rich_text": [_text(content)]},
    }


def _quote(rich_text: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "quote", "quote": {"rich_text": rich_text}}


def _column(children: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "column", "column": {"children": children}}


def _column_list(columns: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "column_list", "column_list": {"children": columns}}


def build_tracker_blocks(counts: dict[str, int]) -> list[dict[str, Any]]:
    not_started = counts["Not started"]
    in_progress = counts["In progress"]
    done = counts["Done"]
    total = not_started + in_progress + done

    def pct(n: int) -> str:
        return f"{round(100 * n / total)}% of total" if total else "—"

    now = (
        datetime.datetime.now(LOCAL_TZ) if LOCAL_TZ
        else datetime.datetime.utcnow()
    )
    timestamp = now.strftime("%b %d, %Y at %I:%M %p %Z").strip()
    if timestamp.endswith("AM ") or timestamp.endswith("PM "):
        timestamp = timestamp.rstrip()

    refresh_note = _quote([
        _text("🔄 "),
        _text("Auto-refreshed by GitHub Actions every hour.", bold=True),
        _text(f"  Last refresh: {timestamp}."),
    ])

    columns_data = [
        ("🔴 ", "Not started", not_started, pct(not_started)),
        ("🟡 ", "In progress", in_progress, pct(in_progress)),
        ("🟢 ", "Done", done, pct(done)),
    ]

    column_blocks = []
    for emoji, label, n, p in columns_data:
        column_blocks.append(
            _column([
                _quote([_text(emoji), _text(label, bold=True)]),
                _heading_1(str(n)),
                _paragraph([_text(p)]),
            ])
        )

    total_line = _paragraph([
        _text(f"{total} tasks total", bold=True),
        _text(f" · Last refresh: {timestamp}"),
    ])

    return [refresh_note, _column_list(column_blocks), total_line]


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

def main() -> None:
    print("→ Querying task data source…")
    tasks = query_all_tasks()
    print(f"  Found {len(tasks)} tasks.")

    counts = count_by_status(tasks)
    print(f"  Counts: {counts}")

    print("→ Locating tracker toggle…")
    toggle_id = find_tracker_toggle()
    print(f"  Toggle block id: {toggle_id}")

    print("→ Removing existing toggle contents…")
    existing = list_block_children(toggle_id)
    for child in existing:
        delete_block(child["id"])
    print(f"  Removed {len(existing)} block(s).")

    print("→ Writing new toggle contents…")
    new_blocks = build_tracker_blocks(counts)
    append_children(toggle_id, new_blocks)
    print("  Done. ✓")


if __name__ == "__main__":
    main()

