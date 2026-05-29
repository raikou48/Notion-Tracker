#!/usr/bin/env python3
"""
refresh_tracker.py
==================

Refreshes BOTH "Task Status Tracker" and "Task Type Tracker" toggles
on the Task section (Academics and Personal) page.

  • Task Status Tracker — counts EVERY active task by Status
      (Not started / In progress / Done)
  • Task Type Tracker   — counts ACTIVE tasks excluding Done by Type
      (Personal / Academic / Academic & Personal)

Archived and trashed tasks are filtered out from every tracker, so the
totals match what you actually see in your Notion view.

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
    LOCAL_TZ: Any = ZoneInfo("America/New_York")
except Exception:
    LOCAL_TZ = None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DATA_SOURCE_ID = "122cd9978dee4c0b83ed4722a007a841"
PARENT_PAGE_ID = "f9c27f2c8845823a837201565a531822"

TRACKERS: list[dict[str, Any]] = [
    {
        "toggle_title": "Task Status Tracker",
        "property_name": "Status",
        "property_type": "status",
        "buckets": [
            {"emoji": "🔴 ", "label": "Not started"},
            {"emoji": "🟡 ", "label": "In progress"},
            {"emoji": "🟢 ", "label": "Done"},
        ],
    },
    {
        "toggle_title": "Task Type Tracker",
        "property_name": "Type",
        "property_type": "multi_select",
        "exclude_status": ["Done"],
        "buckets": [
            {"emoji": "🟠 ", "label": "Personal"},
            {"emoji": "🟣 ", "label": "Academic"},
            {"emoji": "🟡 ", "label": "Academic & Personal"},
        ],
    },
]

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
    """Return every active (non-archived, non-trashed) task in the data source."""
    raw: list[dict[str, Any]] = []
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
        raw.extend(data["results"])
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")

    # Strip archived / trashed pages. Notion's query returns them by default.
    active = [
        t for t in raw
        if not t.get("in_trash", False) and not t.get("archived", False)
    ]
    dropped = len(raw) - len(active)
    if dropped:
        print(f"  Filtered out {dropped} archived/trashed task(s).")
    return active


def get_status_name(task: dict[str, Any]) -> str | None:
    prop = (task.get("properties") or {}).get("Status") or {}
    if prop.get("type") != "status":
        return None
    return ((prop.get("status") or {}).get("name")) or None


def count_by_property(
    tasks: list[dict[str, Any]],
    prop_name: str,
    prop_type: str,
    bucket_labels: list[str],
) -> dict[str, int]:
    counts = {label: 0 for label in bucket_labels}
    for t in tasks:
        prop = (t.get("properties") or {}).get(prop_name) or {}
        if prop.get("type") != prop_type:
            continue
        if prop_type == "status":
            name = ((prop.get("status") or {}).get("name")) or ""
            if name in counts:
                counts[name] += 1
        elif prop_type == "multi_select":
            for option in prop.get("multi_select") or []:
                name = option.get("name") or ""
                if name in counts:
                    counts[name] += 1
        elif prop_type == "select":
            name = ((prop.get("select") or {}).get("name")) or ""
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


def find_toggle(title_contains: str) -> str | None:
    for block in list_block_children(PARENT_PAGE_ID):
        if block.get("type") == "heading_3":
            h = block.get("heading_3", {})
            if h.get("is_toggleable"):
                text = "".join(
                    rt.get("plain_text", "") for rt in h.get("rich_text", [])
                )
                if title_contains in text:
                    return block["id"]
    return None


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


def build_tracker_blocks(
    counts: dict[str, int],
    buckets: list[dict[str, str]],
    exclude_note: str | None = None,
) -> list[dict[str, Any]]:
    total = sum(counts.values())

    def pct(n: int) -> str:
        return f"{round(100 * n / total)}% of total" if total else "—"

    now = datetime.datetime.now(LOCAL_TZ) if LOCAL_TZ else datetime.datetime.utcnow()
    timestamp = now.strftime("%b %d, %Y at %I:%M %p %Z").strip().rstrip()

    refresh_parts = [
        _text("🔄 "),
        _text("Auto-refreshed by GitHub Actions every 5 minutes.", bold=True),
    ]
    if exclude_note:
        refresh_parts.append(_text(f"  {exclude_note}"))
    refresh_parts.append(_text(f"  Last refresh: {timestamp}."))

    refresh_note = _quote(refresh_parts)

    column_blocks = []
    for bucket in buckets:
        emoji = bucket["emoji"]
        label = bucket["label"]
        n = counts.get(label, 0)
        column_blocks.append(
            _column([
                _quote([_text(emoji), _text(label, bold=True)]),
                _heading_1(str(n)),
                _paragraph([_text(pct(n))]),
            ])
        )

    total_line = _paragraph([
        _text(f"{total} tasks total", bold=True),
        _text(f" · Last refresh: {timestamp}"),
    ])

    return [refresh_note, _column_list(column_blocks), total_line]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def refresh_tracker(tasks: list[dict[str, Any]], config: dict[str, Any]) -> None:
    title = config["toggle_title"]
    print(f"\n→ {title}")

    exclude_status = config.get("exclude_status") or []
    if exclude_status:
        filtered = [t for t in tasks if get_status_name(t) not in exclude_status]
        excluded = len(tasks) - len(filtered)
        exclude_note = (
            "Active tasks only — excludes "
            + " or ".join(f'\"{s}\"' for s in exclude_status)
            + "."
        )
        print(f"  Filtered out {excluded} task(s) with status in {exclude_status}.")
    else:
        filtered = tasks
        exclude_note = None

    bucket_labels = [b["label"] for b in config["buckets"]]
    counts = count_by_property(
        filtered, config["property_name"], config["property_type"], bucket_labels
    )
    print(f"  Counts: {counts}")

    toggle_id = find_toggle(title)
    if not toggle_id:
        print(f"  WARN: toggle '{title}' not found on the page; skipping.")
        return

    for child in list_block_children(toggle_id):
        delete_block(child["id"])
    append_children(
        toggle_id, build_tracker_blocks(counts, config["buckets"], exclude_note)
    )
    print("  Updated. ✓")


def main() -> None:
    print("→ Querying Tasks data source…")
    tasks = query_all_tasks()
    print(f"  {len(tasks)} active task(s).")

    for config in TRACKERS:
        refresh_tracker(tasks, config)

    print("\n→ All trackers refreshed. Done. ✓")


if __name__ == "__main__":
    main()

