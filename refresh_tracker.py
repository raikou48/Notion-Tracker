#!/usr/bin/env python3
"""
refresh_tracker.py
==================

Refreshes the "Task Status Tracker" toggle on the Task section
(Academics and Personal) page in Notion.

Reads the "Tasks" data source (the one with Status values
Not started / In progress / Done) and counts each task by status.

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
# Configuration
# ---------------------------------------------------------------------------

# The "Tasks" data source (the real one your tasks live in).
DATA_SOURCE_ID = "36427f2c88458053b5ab000b5ce37518"

# The "Task section (Academics and Personal)" page that holds the toggle.
PARENT_PAGE_ID = "f9c27f2c8845823a837201565a531822"

TOGGLE_TITLE_CONTAINS = "Task Status Tracker"

# Status property and the values to count, in display order.
STATUS_PROPERTY = "Status"
BUCKETS = ["Not started", "In progress", "Done"]

API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"


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
# Notion API helpers
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
    for t in tasks:
        prop = (t.get("properties") or {}).get(STATUS_PROPERTY) or {}
        if prop.get("type") != "status":
            continue
        status_obj = prop.get("status") or {}
        name = status_obj.get("name")
        if name in counts:
            counts[name] += 1
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
    requests.delete(
        f"{API_BASE}/blocks/{block_id}", headers=_headers(), timeout=30
    ).raise_for_status()


def append_children(block_id: str, children: list[dict[str, Any]]) -> None:
    requests.patch(
        f"{API_BASE}/blocks/{block_id}/children",
        headers=_headers(),
        json={"children": children},
        timeout=30,
    ).raise_for_status()


def find_tracker_toggle() -> str:
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
        f"Could not find toggle on page {PARENT_PAGE_ID}."
    )


# ---------------------------------------------------------------------------
# Block construction
# ---------------------------------------------------------------------------

def _text(content: str, bold: bool = False) -> dict[str, Any]:
    rt: dict[str, Any] = {"type": "text", "text": {"content": content}}
    if bold:
        rt["annotations"] = {"bold": True}
    return rt


def _paragraph(rich_text: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "paragraph", "paragraph": {"rich_text": rich_text}}


def _heading_1(content: str) -> dict[str, Any]:
    return {"type": "heading_1", "heading_1": {"rich_text": [_text(content)]}}


def _quote(rich_text: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "quote", "quote": {"rich_text": rich_text}}


def _column(children: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "column", "column": {"children": children}}


def _column_list(columns: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "column_list", "column_list": {"children": columns}}


def build_tracker_blocks(counts: dict[str, int]) -> list[dict[str, Any]]:
    ns = counts["Not started"]
    ip = counts["In progress"]
    dn = counts["Done"]
    total = ns + ip + dn

    def pct(n: int) -> str:
        return f"{round(100 * n / total)}% of total" if total else "—"

    now = datetime.datetime.now(LOCAL_TZ) if LOCAL_TZ else datetime.datetime.utcnow()
    timestamp = now.strftime("%b %d, %Y at %I:%M %p %Z").strip().rstrip()

    refresh_note = _quote([
        _text("🔄 "),
        _text("Auto-refreshed by GitHub Actions every hour.", bold=True),
        _text(f"  Last refresh: {timestamp}."),
    ])

    columns_data = [
        ("🔴 ", "Not started", ns, pct(ns)),
        ("🟡 ", "In progress", ip, pct(ip)),
        ("🟢 ", "Done", dn, pct(dn)),
    ]

    column_blocks = [
        _column([
            _quote([_text(emoji), _text(label, bold=True)]),
            _heading_1(str(n)),
            _paragraph([_text(p)]),
        ])
        for emoji, label, n, p in columns_data
    ]

    total_line = _paragraph([
        _text(f"{total} tasks total", bold=True),
        _text(f" · Last refresh: {timestamp}"),
    ])

    return [refresh_note, _column_list(column_blocks), total_line]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("→ Querying Tasks data source…")
    tasks = query_all_tasks()
    print(f"  Found {len(tasks)} tasks.")

    counts = count_by_status(tasks)
    print(f"  Counts: {counts}")

    print("→ Locating tracker toggle…")
    toggle_id = find_tracker_toggle()

    print("→ Rewriting toggle contents…")
    for child in list_block_children(toggle_id):
        delete_block(child["id"])
    append_children(toggle_id, build_tracker_blocks(counts))
    print("→ Done. ✓")


if __name__ == "__main__":
    main()

