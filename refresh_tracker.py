#!/usr/bin/env python3
"""
refresh_tracker.py
==================

Two jobs per run:

  1. Refresh the Task Status Tracker + Task Type Tracker toggles on
     the Task section (Academics and Personal) page.

  2. Back up every task — including the notes inside it — to the
     Google Sheet via the Apps Script web app endpoint. The Sheet is
     append-only: deletions in Notion never propagate; the last-known
     state of each task is preserved.

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

SHEETS_WEB_APP_URL = os.environ.get("SHEETS_WEB_APP_URL", "")


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
# Notion fetching
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
        if not r.ok:
            sys.stderr.write(f"Query failed ({r.status_code}): {r.text}\n")
        r.raise_for_status()
        data = r.json()
        tasks.extend(data["results"])
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return tasks


def fetch_block_children(block_id: str) -> list[dict[str, Any]]:
    children: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        url = f"{API_BASE}/blocks/{block_id}/children?page_size=100"
        if cursor:
            url += f"&start_cursor={cursor}"
        r = requests.get(url, headers=_headers(), timeout=30)
        if not r.ok:
            return children
        data = r.json()
        children.extend(data["results"])
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return children


def blocks_to_text(blocks: list[dict[str, Any]], depth: int = 0) -> str:
    """Convert a block tree to plain text. Limits recursion to depth 2."""
    lines: list[str] = []
    indent = "  " * depth
    for block in blocks:
        bt = block.get("type")
        if not bt:
            continue
        data = block.get(bt, {}) or {}
        rich = data.get("rich_text") or []
        text = "".join((rt.get("plain_text") or "") for rt in rich)

        if bt == "heading_1":
            lines.append(f"{indent}# {text}")
        elif bt == "heading_2":
            lines.append(f"{indent}## {text}")
        elif bt == "heading_3":
            lines.append(f"{indent}### {text}")
        elif bt == "bulleted_list_item":
            lines.append(f"{indent}• {text}")
        elif bt == "numbered_list_item":
            lines.append(f"{indent}- {text}")
        elif bt == "to_do":
            mark = "[x]" if data.get("checked") else "[ ]"
            lines.append(f"{indent}{mark} {text}")
        elif bt == "quote":
            lines.append(f"{indent}> {text}")
        elif bt == "toggle":
            lines.append(f"{indent}▸ {text}")
        elif bt == "code":
            lines.append(f"{indent}```\n{indent}{text}\n{indent}```")
        elif bt == "callout":
            lines.append(f"{indent}💬 {text}")
        elif bt == "divider":
            lines.append(f"{indent}---")
        elif bt == "paragraph":
            lines.append(f"{indent}{text}" if text else "")
        elif text:
            lines.append(f"{indent}{text}")

        if depth < 2 and block.get("has_children"):
            try:
                child_blocks = fetch_block_children(block["id"])
                child_text = blocks_to_text(child_blocks, depth + 1)
                if child_text:
                    lines.append(child_text)
            except Exception:
                pass

    return "\n".join(line for line in lines if line is not None)


def fetch_task_notes(task_id: str) -> str:
    try:
        blocks = fetch_block_children(task_id)
        return blocks_to_text(blocks).strip()
    except Exception as e:
        return f"(error fetching notes: {e})"


# ---------------------------------------------------------------------------
# Property extraction
# ---------------------------------------------------------------------------

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


def extract_task_data(task: dict[str, Any]) -> dict[str, Any]:
    props = task.get("properties") or {}

    # Title (the "Task" property)
    title_prop = props.get("Task") or {}
    title_arr = title_prop.get("title") or []
    task_title = "".join((rt.get("plain_text") or "") for rt in title_arr)

    # Status
    status = ""
    sp = props.get("Status") or {}
    if sp.get("type") == "status":
        status = ((sp.get("status") or {}).get("name")) or ""

    # Category
    category = ""
    cp = props.get("Category") or {}
    if cp.get("type") == "select":
        category = ((cp.get("select") or {}).get("name")) or ""

    # Type (multi_select → comma-joined)
    tp = props.get("Type") or {}
    type_names: list[str] = []
    if tp.get("type") == "multi_select":
        type_names = [
            opt.get("name") for opt in (tp.get("multi_select") or [])
            if opt.get("name")
        ]
    type_value = ", ".join(type_names)

    # Due Date
    due_date = ""
    dp = props.get("Due Date") or {}
    if dp.get("type") == "date":
        due_date = ((dp.get("date") or {}).get("start")) or ""

    # Notes (page body content)
    notes = fetch_task_notes(task["id"])

    now = datetime.datetime.now(LOCAL_TZ) if LOCAL_TZ else datetime.datetime.utcnow()

    return {
        "id": task["id"],
        "task": task_title,
        "status": status,
        "category": category,
        "type": type_value,
        "due_date": due_date,
        "notes": notes,
        "last_synced": now.isoformat(),
    }


# ---------------------------------------------------------------------------
# Notion block manipulation (for tracker rewrite)
# ---------------------------------------------------------------------------

def list_block_children(block_id: str) -> list[dict[str, Any]]:
    return fetch_block_children(block_id)


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
    timestamp = now.strftime("%b %d, %Y at %I:%M %p %Z").strip()

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
        n = counts.get(bucket["label"], 0)
        column_blocks.append(
            _column([
                _quote([_text(bucket["emoji"]), _text(bucket["label"], bold=True)]),
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
# Tracker refresh
# ---------------------------------------------------------------------------

def refresh_tracker(tasks: list[dict[str, Any]], config: dict[str, Any]) -> None:
    title = config["toggle_title"]
    print(f"\n→ {title}")

    exclude_status = config.get("exclude_status") or []
    if exclude_status:
        filtered = [t for t in tasks if get_status_name(t) not in exclude_status]
        exclude_note = (
            "Active tasks only — excludes "
            + " or ".join(f"\"{s}\"" for s in exclude_status)
            + "."
        )
        print(f"  Filtered out {len(tasks) - len(filtered)} task(s) with status in {exclude_status}.")
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
        print(f"  WARN: toggle '{title}' not found; skipping.")
        return

    for child in list_block_children(toggle_id):
        delete_block(child["id"])
    append_children(
        toggle_id, build_tracker_blocks(counts, config["buckets"], exclude_note)
    )
    print("  Updated. ✓")


# ---------------------------------------------------------------------------
# Google Sheets backup
# ---------------------------------------------------------------------------

def push_to_sheet(tasks_data: list[dict[str, Any]]) -> None:
    if not SHEETS_WEB_APP_URL:
        print("  SHEETS_WEB_APP_URL not set; skipping backup.")
        return

    try:
        r = requests.post(
            SHEETS_WEB_APP_URL,
            json={"tasks": tasks_data},
            timeout=60,
            allow_redirects=True,
        )
        if r.ok:
            try:
                result = r.json()
                print(
                    f"  Sheet backup: inserted {result.get('inserted', 0)}, "
                    f"updated {result.get('updated', 0)}."
                )
            except Exception:
                print(f"  Sheet backup: HTTP {r.status_code} (non-JSON response)")
        else:
            print(f"  Sheet backup FAILED: HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"  Sheet backup error: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("→ Querying Tasks data source…")
    tasks = query_all_tasks()
    print(f"  Found {len(tasks)} tasks.")

    for config in TRACKERS:
        refresh_tracker(tasks, config)

    print("\n→ Backing up tasks (with notes) to Google Sheets…")
    print(f"  Fetching notes for {len(tasks)} task(s)…")
    tasks_data = [extract_task_data(t) for t in tasks]
    push_to_sheet(tasks_data)

    print("\n→ All done. ✓")


if __name__ == "__main__":
    main()

