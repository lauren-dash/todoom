"""Durable state: the raw record store per source, and assembling the task list.

``raw/<source>.json`` holds every record fetched from that source, keyed by id,
plus ``latest_ids`` -- the records the most recent *successful* fetch returned.
Only those are judged, so a source's current state wins (a Notion row checked
off, a Docs thread resolved, an email archived all drop out), and a source that
fails keeps its last good snapshot instead of losing its tasks. Records outside
the latest snapshot are evicted once older than the window plus a margin.
"""

from __future__ import annotations

import datetime as dt
import html
import hashlib
import json
import math
import os
import re
from email.utils import parseaddr
from pathlib import Path
from typing import Any

from cot_common import (
    DATA_DIR,
    LOOK_LATER_EMOJI,
    LOOK_LATER_PATH,
    MAC_SOURCES,
    RAW_RETENTION_EXTRA_DAYS,
    REPO_ROOT,
    SOURCE_LABELS,
    SOURCES,
    cutoff,
    iso,
    now_utc,
    parse_iso,
    read_json,
    write_json,
)
from cot_judge import base_unit, dismissed_ids, done_ids

RAW_DIR = DATA_DIR / "raw"
STATUS_PATH = DATA_DIR / "fetch_status.json"
JUDGEMENTS_PATH = DATA_DIR / "judgements.json"
JUDGE_STATUS_PATH = DATA_DIR / "judge_status.json"
MERGES_PATH = DATA_DIR / "merges.json"
VOICE_PATH = DATA_DIR / "voice.json"  # second-person rewordings, by original wording
TASKS_PATH = Path(
    os.environ.get("COT_TASKS_PATH", REPO_ROOT / "data/.apps/task-inbox/tasks.json")
)
# Written by the Task Inbox app when the user marks a task "Not a task". Read-only here.
DISMISSED_PATH = Path(
    os.environ.get("COT_DISMISSED_PATH", TASKS_PATH.parent / "dismissed.json")
)
# Written by the TODOOM app when the user checks a task off (and removed when they
# uncheck it): what they have already done. Read-only here.
COMPLETED_PATH = Path(
    os.environ.get("COT_COMPLETED_PATH", TASKS_PATH.parent / "completed.json")
)
# Every task the last build listed. A task whose conversation has since aged out of
# the look-back window is carried forward from here, so an open task stays on the
# list until the user checks it off or removes it, however old it gets.
OPEN_TASKS_PATH = DATA_DIR / "open_tasks.json"
# Sources read as messages (new ones arrive; old ones never change), which refresh
# fetches incrementally and whose open tasks are carried forward. Google Docs and
# Notion are read as current state every time: a resolved comment or a checked
# Notion row has to disappear.
MESSAGE_SOURCES = ("gmail-work", "gmail-personal", "slack", "imessage", "whatsapp")
# How much a since-last-refresh fetch overlaps the previous one, in days.
FETCH_OVERLAP_DAYS = 1


def load_completed(path: Path | None = None) -> dict[str, Any]:
    """The app's completed.json (task id -> details), or {} if absent or unreadable."""
    try:
        data = json.loads((path or COMPLETED_PATH).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def fetch_days(
    source_key: str,
    days: int,
    fetch_status: dict[str, Any],
    now: dt.datetime | None = None,
) -> int:
    """How far back this refresh reads a source.

    Message sources read only since their last successful fetch (plus a day of
    overlap), once a full window has been read; state sources (Docs, Notion)
    always read the whole window."""
    if source_key not in MESSAGE_SOURCES:
        return days
    last = (fetch_status.get(source_key) or {}).get("last_ok_at")
    if not last:
        return days
    elapsed = ((now or now_utc()) - parse_iso(last)).total_seconds() / 86400
    return max(2, min(days, math.ceil(elapsed) + FETCH_OVERLAP_DAYS))


def load_dismissed(path: Path | None = None) -> dict[str, Any]:
    """The app's dismissed.json (task id -> details), or {} if absent or unreadable."""
    try:
        data = json.loads((path or DISMISSED_PATH).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_snapshot(
    source_key: str,
    records: list[dict[str, Any]],
    days: int,
    now: dt.datetime | None = None,
    incremental: bool = False,
) -> None:
    """Store a fetch. A full fetch replaces the snapshot; an incremental one (only
    what arrived since the last refresh) is added to it, and records that slid
    out of the look-back window leave the snapshot."""
    now = now or now_utc()
    path = RAW_DIR / f"{source_key}.json"
    store = read_json(path, {"records": {}, "latest_ids": []})
    for r in records:
        store["records"][r["id"]] = r
    latest = [r["id"] for r in records]
    if incremental:
        start = iso(cutoff(days, now))
        new = set(latest)
        latest = [
            i
            for i in store.get("latest_ids", [])
            if i not in new and (store["records"].get(i, {}).get("date") or "") >= start
        ] + latest
    keep_after = iso(now - dt.timedelta(days=days + RAW_RETENTION_EXTRA_DAYS))
    latest_set = set(latest)
    store["records"] = {
        k: v
        for k, v in store["records"].items()
        if k in latest_set or (v.get("date") or "") >= keep_after
    }
    store["latest_ids"] = latest
    store["fetched_at"] = iso(now)
    write_json(path, store)


def load_store(source_key: str) -> dict[str, dict[str, Any]]:
    """Every stored record of a source, by id (not just the latest snapshot)."""
    return read_json(RAW_DIR / f"{source_key}.json", {"records": {}})["records"]


def load_snapshot(source_key: str) -> list[dict[str, Any]]:
    store = read_json(RAW_DIR / f"{source_key}.json", {"records": {}, "latest_ids": []})
    return [store["records"][i] for i in store["latest_ids"] if i in store["records"]]


def load_all_snapshots() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for key in SOURCES:
        out += load_snapshot(key)
    return out


def record_status(results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    status = read_json(STATUS_PATH, {})
    for key, res in results.items():
        prev = status.get(key, {})
        entry = {
            "ok": res["ok"],
            "attempted_at": res["at"],
            "error": res.get("error"),
            "seconds": res.get("seconds"),
        }
        if res.get("off"):
            # Turned off in the settings: not a failure, and nothing to keep.
            entry["off"] = True
        # A source turned off loses its snapshot, so turning it back on must read
        # the whole window again rather than only what is new.
        entry["last_ok_at"] = (
            res["at"]
            if res["ok"]
            else (None if res.get("off") else prev.get("last_ok_at"))
        )
        entry["records"] = res.get("records", prev.get("records"))
        status[key] = entry
    write_json(STATUS_PATH, status)
    return status


# ---------------------------------------------------------------- the task list

CONTEXT_BEFORE = 6
CONTEXT_AFTER = 2
APP_LINK = re.compile(r"^(sms|whatsapp|messages|imessage|tel|mailto):", re.I)


def task_url(source_key: str, link: str | None, rec: dict[str, Any]) -> str | None:
    """Where the task's link goes. A web page named in the message wins.

    Texts never link to the chat, and WhatsApp only to an https://wa.me/ chat:
    the desktop app opens sms:/whatsapp: links as a blank window.
    """
    if link and link.lower().startswith(("https://", "http://")):
        return link
    url = rec.get("url")
    if source_key == "imessage":
        return None
    if source_key == "whatsapp":
        return url if url and url.startswith("https://wa.me/") else None
    return None if not url or APP_LINK.match(url) else url


def _looks_like_number(name: str | None) -> bool:
    """True for a sender shown only as a number or short code."""
    return not name or not re.search(r"[A-Za-z]", name)


def display_from(
    source_key: str,
    rec: dict[str, Any],
    unit_recs: list[dict[str, Any]],
    sender_hint: str | None = None,
) -> str | None:
    """The person (or brand) the task is with, as the app shows it."""
    if source_key == "notion":
        return "Notion to-do list"
    if source_key == "google-docs":
        people = [(rec.get("from_name"), rec.get("from_me"))] + [
            (r.get("author"), r.get("me")) for r in rec.get("replies", [])
        ]
        others = [name for name, me in people if name and not me]
        return others[-1] if others else people[-1][0]
    if source_key in MAC_SOURCES:
        if rec.get("from_me") or rec.get("is_group"):
            name = rec.get("chat")
        else:
            name = rec.get("from_name")
        if sender_hint and (
            _looks_like_number(name) or "@" in (name or "") or "_" in (name or "")
        ):
            return sender_hint
        return name
    if not rec.get("from_me"):
        return rec.get("from_name") or rec.get("from")
    # The user's own message (a commitment): name the other side of the conversation.
    others = [
        r for r in sorted(unit_recs, key=lambda r: r["date"]) if not r.get("from_me")
    ]
    if others:
        return others[-1].get("from_name")
    if source_key == "slack":
        return (
            f"#{rec.get('channel')}"
            if rec.get("channel") and not rec.get("is_dm")
            else None
        )
    to_name, to_addr = parseaddr(rec.get("to") or "")
    return to_name or to_addr or None


def chat_context(
    rec: dict[str, Any], unit_recs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The messages around a text/WhatsApp task's message (reactions are never stored)."""
    ordered = sorted(unit_recs, key=lambda r: (r["date"], str(r["id"])))
    idx = next((i for i, r in enumerate(ordered) if r["id"] == rec["id"]), None)
    if idx is None:
        return []
    window = ordered[max(0, idx - CONTEXT_BEFORE) : idx + CONTEXT_AFTER + 1]
    return [
        {
            "who": "You" if r.get("from_me") else r.get("from_name"),
            "date": r["date"],
            "text": r.get("text"),
            "this": r["id"] == rec["id"],
        }
        for r in window
    ]


def merge_tasks(
    tasks: list[dict[str, Any]], groups: list[list[str]]
) -> list[dict[str, Any]]:
    """Fold each group of duplicate tasks into one.

    The merged task keeps the id of the group's oldest task (the one the user is
    most likely to have checked off already) and lists the others in
    ``merged_ids``; its wording, date and link come from the newest one (the
    newest that is not a look-later mark, when there is one).
    """
    by_id = {t["id"]: t for t in tasks}
    drop: set[str] = set()
    merged: list[dict[str, Any]] = []
    for group in groups:
        members = [by_id[i] for i in group if i in by_id and i not in drop]
        if len(members) < 2:
            continue
        members.sort(key=lambda t: (t.get("date") or "", t["id"]))
        oldest = members[0]
        # A look-later mark on a message that already gave a task keeps that
        # task's (more specific) wording but adds the mark's next-day due date.
        found = [t for t in members if not t["id"].startswith(LOOK_LATER_PREFIX)]
        marks = [t for t in members if t["id"].startswith(LOOK_LATER_PREFIX)]
        newest = (found or members)[-1]
        others = [t for t in members if t is not oldest]
        due = min(
            (t["due"] for t in [newest, *marks] if t.get("due")), default=None
        ) or min((t["due"] for t in members if t.get("due")), default=None)
        merged.append(
            dict(
                newest,
                id=oldest["id"],
                due=due,
                stale=all(t.get("stale") for t in members),
                merged_ids=sorted(t["id"] for t in others),
                also_from=[
                    {
                        "source": t["source"],
                        "from": t["from"],
                        "date": t["date"],
                        "url": t["url"],
                        "title": t["title"],
                    }
                    for t in members
                    if t is not newest
                ],
            )
        )
        drop.update(t["id"] for t in members)
    return [t for t in tasks if t["id"] not in drop] + merged


LOOK_LATER_PREFIX = "slack-later-"
SLACK_LINK = re.compile(r"<(https?://[^|>]+)(?:\|([^>]*))?>")


def look_later_tasks(state: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """(task, record) for each Slack message the user marked with the look-later emoji.

    Due the day after the mark was first seen (local time). Marks that were
    already there when tracking began (``baseline``) are left out unless the
    user asked for them (``include``)."""
    skip = set(state.get("baseline") or []) - set(state.get("include") or [])
    seen = state.get("first_seen") or {}
    out = []
    for rec in state.get("records") or []:
        if rec["id"] in skip:
            continue
        first = parse_iso(seen.get(rec["id"]) or rec["date"])
        due = (first.astimezone().date() + dt.timedelta(days=1)).isoformat()
        who = "your" if rec.get("from_me") else f"{rec.get('from_name') or 'someone'}'s"
        where = "DM" if rec.get("is_dm") else f"message in #{rec.get('channel')}"
        text = SLACK_LINK.sub(lambda m: m.group(2) or m.group(1), rec.get("text_display") or rec.get("text") or "")
        text = html.unescape(" ".join(text.split()))
        snippet = text if len(text) <= 160 else text[:157].rstrip() + "..."
        task = {
            "id": LOOK_LATER_PREFIX + hashlib.sha256(rec["id"].encode()).hexdigest()[:12],
            "title": f"Look at {who} Slack {where}",
            "why": f"You marked it :{state.get('emoji') or LOOK_LATER_EMOJI}: in Slack" + (f": \"{snippet}\"" if snippet else "."),
            "due": due,
        }
        out.append((task, rec))
    return out


def carried_tasks(
    previous: list[dict[str, Any]],
    current: list[dict[str, Any]],
    records: list[dict[str, Any]],
    gone: set[str],
) -> list[dict[str, Any]]:
    """Open tasks from the last list whose conversation is no longer in the window.

    Only message sources carry forward. A task whose conversation is still in
    the window is judged afresh (so one the user has since handled drops off);
    one the user checked off or removed (``gone``) is not carried."""
    live_units = {r.get("unit") for r in records}
    listed = {i for t in current for i in (t["id"], *t.get("merged_ids", []))}
    out = []
    for t in previous:
        ids = {t["id"], *t.get("merged_ids", [])}
        if t.get("source_key") not in MESSAGE_SOURCES or ids & (listed | gone):
            continue
        if t["id"].startswith(LOOK_LATER_PREFIX):
            continue  # listed only while the Slack mark is on the message
        if ((t.get("raw") or {}).get("unit")) in live_units:
            continue
        out.append(dict(t, carried=True))
    return out


def _describe_unit(records: list[dict[str, Any]]) -> str:
    r = records[0]
    sk = r["source_key"]
    if sk.startswith("gmail"):
        return f"Email: {r.get('subject') or '(no subject)'}"
    if sk == "slack":
        return (
            f"Slack DM with {r.get('from_name')}"
            if r.get("is_dm")
            else f"Slack #{r.get('channel')}"
        )
    if sk == "google-docs":
        return f"Comment on {r.get('doc_name')}"
    return f"{'Texts' if sk == 'imessage' else 'WhatsApp'} with {r.get('chat')}"


def _ago(stamp: str | None, now: dt.datetime) -> str:
    if not stamp:
        return "never"
    hours = (now - parse_iso(stamp)).total_seconds() / 3600
    if hours < 1:
        return "under an hour ago"
    if hours < 48:
        return f"{int(hours)} hours ago"
    return f"{int(hours // 24)} days ago"


def build_task_list(
    records: list[dict[str, Any]],
    judgements: dict[str, dict[str, Any]],
    fetch_status: dict[str, Any],
    judge_status: dict[str, Any],
    days: int,
    now: dt.datetime | None = None,
    merges: list[list[str]] | None = None,
    dismissed: dict[str, Any] | None = None,
    completed: dict[str, Any] | None = None,
    previous_open: list[dict[str, Any]] | None = None,
    look_later: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = now or now_utc()
    by_id = {(r["source_key"], r["id"]): r for r in records}
    by_unit: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        by_unit.setdefault(r["unit"], []).append(r)

    tasks: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    def add(
        source_key: str,
        task: dict[str, Any],
        rec: dict[str, Any],
        unit_recs: list[dict[str, Any]],
    ) -> None:
        label, tab = SOURCES[source_key]
        entry = {
            "id": task["id"],
            "tab": tab,
            "source": label,
            "source_key": source_key,
            "title": task["title"],
            "why": task["why"],
            "url": task_url(source_key, task.get("link"), rec),
            "source_url": rec.get("url"),
            "due": task.get("due"),
            "date": rec.get("date"),
            "from": display_from(source_key, rec, unit_recs, task.get("sender")),
            "stale": bool(task.get("stale")),
            "merged_ids": [],
            "also_from": [],
            "raw": rec,
        }
        if source_key in MAC_SOURCES:
            entry["context"] = chat_context(rec, unit_recs)
        tasks.append(entry)

    # Notion rows are tasks as written; the page id is the stable task id.
    for r in records:
        if r["source_key"] == "notion":
            why = r.get("notes") or "Open on your Notion to-do list" + (
                f" ({r['day']})" if r.get("day") else ""
            )
            add(
                "notion",
                {"id": f"notion-{r['id']}", "title": r["title"], "why": why},
                r,
                [r],
            )

    # Slack messages the user marked :look-later: are tasks as marked.
    for task, rec in look_later_tasks(look_later or {}):
        add("slack", task, rec, [rec])

    skips: dict[str, tuple[str, dict[str, Any]]] = {}
    with_tasks: set[str] = set()
    for unit_key, entry in sorted(judgements.items()):
        base = base_unit(unit_key)
        recs = by_unit.get(base)
        if not recs:
            continue
        source_key = recs[0]["source_key"]
        for t in entry.get("tasks", []):
            rec = by_id.get((source_key, t["record_id"])) or recs[-1]
            add(source_key, dict(t, stale=entry.get("stale")), rec, recs)
            with_tasks.add(base)
        if entry.get("skip"):
            skips[base] = (
                unit_key,
                entry["skip"],
            )  # a chat's latest week wins (keys sort by week)
    # One "left out" line per conversation that gave no task at all.
    for base, (_key, skip) in skips.items():
        if base in with_tasks:
            continue
        recs = by_unit[base]
        source_key = recs[0]["source_key"]
        newest = max(recs, key=lambda r: r["date"])
        skipped.append(
            {
                "what": _describe_unit(recs),
                "category": skip["category"],
                "reason": skip["note"],
                "source": SOURCES[source_key][0],
                "tab": SOURCES[source_key][1],
                "url": task_url(source_key, None, newest),
            }
        )

    failed = {
        k
        for k, st in fetch_status.items()
        if st and not st.get("ok") and not st.get("off")
    }
    for t in tasks:
        if t["source_key"] in failed:
            t["stale"] = True
    tasks = merge_tasks(tasks, merges or [])
    # A new task the duplicate check matched to one the user already checked off
    # takes that task's id among its merged ids, so the app shows it as done.
    done = done_ids(completed or {})
    for group in merges or []:
        done_in = [i for i in group if i in done]
        if not done_in:
            continue
        for t in tasks:
            if {t["id"], *t["merged_ids"]} & set(group):
                t["merged_ids"] = sorted(set(t["merged_ids"]) | set(done_in))
    # Never re-emit what the user marked "Not a task" (nor a duplicate folded into it).
    rejected = dismissed_ids(dismissed or {})
    hidden = {
        t["id"] for t in tasks if t["id"] in rejected or rejected & set(t["merged_ids"])
    }
    tasks = [t for t in tasks if t["id"] not in hidden]
    # Carry forward open tasks whose conversation aged out of the look-back window.
    tasks += carried_tasks(previous_open or [], tasks, records, rejected | done)

    # Work first; within a tab, dated tasks soonest first, then newest first.
    tasks.sort(key=lambda t: t["date"] or "", reverse=True)
    tasks.sort(key=lambda t: (t["tab"] != "work", t["due"] is None, t["due"] or ""))
    skipped.sort(key=lambda s: (s["tab"], s["source"], s["what"]))

    gaps: list[str] = []
    sources: dict[str, Any] = {}
    for key in SOURCES:
        st = fetch_status.get(key, {})
        ok = st.get("ok")
        sources[key] = {
            "label": SOURCE_LABELS[key],
            "tab": SOURCES[key][1],
            "status": "ok"
            if ok
            else ("failed" if st and not st.get("off") else "not-run"),
            "reason": None if ok else st.get("error"),
            "last_ok_at": st.get("last_ok_at"),
            "attempted_at": st.get("attempted_at"),
            "records": st.get("records"),
        }
        if st and not ok and not st.get("off"):
            since = st.get("last_ok_at")
            kept = (
                f"Showing its tasks as of {_ago(since, now)}"
                if since
                else "No tasks from it yet"
            )
            gaps.append(
                f"{SOURCE_LABELS[key]}: couldn't read it this time: {st.get('error') or 'unknown error'}. {kept}."
            )
    contacts = fetch_status.get("contacts")
    if (
        contacts
        and not contacts.get("ok")
        and any(fetch_status.get(k, {}).get("ok") for k in MAC_SOURCES)
    ):
        gaps.append(
            "Contacts: couldn't read your Mac's contacts, so some texts show phone numbers instead of names."
        )
    wa = [r["date"] for r in records if r["source_key"] == "whatsapp"]
    if wa and (now - parse_iso(max(wa))).total_seconds() > 2 * 86400:
        gaps.append(
            f"WhatsApp: your Mac's copy ends {max(wa)[:10]}; opening WhatsApp Desktop brings it up to date."
        )
    if judge_status.get("failed"):
        gaps.append(
            f"{judge_status['failed']} conversations couldn't be read this time and will be retried next refresh."
        )

    return {
        "generated": iso(now),
        "window_days": days,
        "dismissed_hidden": len(hidden),
        "tasks": tasks,
        "skipped": skipped,
        "gaps": gaps,
        "sources": sources,
        "judge": judge_status,
    }
