"""Fetchers for the cloud sources: Gmail (two accounts), Slack, Google Docs, Notion.

Each ``fetch_*`` returns a list of normalized records. Every record carries
``source_key``, ``id`` (stable across runs), ``unit`` (the conversation it
belongs to, which is what the model judges as a whole), ``date`` (ISO UTC),
``from_me``, ``url`` (back to the origin), plus every reasonable field the
call already returned.
"""

from __future__ import annotations

import base64
import datetime as dt
import html
import json
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from email.utils import parseaddr
from pathlib import Path
from typing import Any

from cot_common import (
    DATA_DIR,
    LOOK_LATER_EMOJI,
    LOOK_LATER_PATH,
    SourceError,
    cutoff,
    get_json,
    iso,
    latchkey_curl,
    now_utc,
    parse_iso,
    read_json,
    write_json,
)

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
MAX_TEXT = 20_000
MAX_HTML = 300_000

# ---------------------------------------------------------------- Gmail


def _b64(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode(
        "utf-8", "replace"
    )


def _find_part(payload: dict[str, Any], mime: str) -> str:
    if payload.get("mimeType") == mime and payload.get("body", {}).get("data"):
        return _b64(payload["body"]["data"])
    for part in payload.get("parts") or []:
        found = _find_part(part, mime)
        if found:
            return found
    return ""


def _attachments(payload: dict[str, Any]) -> list[str]:
    names = []
    for part in payload.get("parts") or []:
        if part.get("filename"):
            names.append(part["filename"])
        names += _attachments(part)
    return names


def html_to_text(markup: str) -> str:
    markup = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", markup)
    markup = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", markup)
    text = html.unescape(re.sub(r"<[^>]+>", " ", markup))
    text = re.sub(r"[ \t\xa0\u200c\u034f]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_gmail_message(
    msg: dict[str, Any], account: str, source_key: str
) -> dict[str, Any]:
    payload = msg.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    plain = _find_part(payload, "text/plain")
    markup = _find_part(payload, "text/html")
    labels = msg.get("labelIds", [])
    sender_name, sender_addr = parseaddr(headers.get("from", ""))
    return {
        "source_key": source_key,
        "id": msg["id"],
        "unit": f"{source_key}:{msg['threadId']}",
        "thread_id": msg["threadId"],
        "account": account,
        "date": iso(
            dt.datetime.fromtimestamp(int(msg["internalDate"]) / 1000, dt.timezone.utc)
        ),
        "from": headers.get("from"),
        "from_name": sender_name or sender_addr,
        "from_me": "SENT" in labels or sender_addr.lower() == account.lower(),
        "to": headers.get("to"),
        "cc": headers.get("cc"),
        "subject": headers.get("subject"),
        "header_date": headers.get("date"),
        "list_unsubscribe": headers.get("list-unsubscribe"),
        "labels": labels,
        "snippet": html.unescape(msg.get("snippet", "")),
        "body": (plain or html_to_text(markup))[:MAX_TEXT],
        "body_html": markup[:MAX_HTML] or None,
        "attachments": _attachments(payload),
        "url": f"https://mail.google.com/mail/u/?authuser={urllib.parse.quote(account)}#all/{msg['threadId']}",
    }


def gmail_query(days: int) -> str:
    return f"in:inbox newer_than:{days}d -category:promotions -category:social -category:forums"


class QuotaPacer:
    """Spend at most ``per_minute`` Gmail quota units a minute, across threads.

    Gmail meters each user per minute (messages.get and messages.list cost 5
    units); a burst over it fails every call with a 403 for the rest of the minute.
    """

    def __init__(self, per_minute: float) -> None:
        self.interval = 60.0 / per_minute
        self.next_at = time.monotonic()
        self.lock = threading.Lock()

    def spend(self, units: int) -> None:
        with self.lock:
            now = time.monotonic()
            wait = self.next_at - now
            self.next_at = max(now, self.next_at) + units * self.interval
        if wait > 0:
            time.sleep(wait)


GMAIL_UNITS_PER_MINUTE = 3000
METADATA_HEADERS = ("From", "To", "Cc", "Subject", "Date", "List-Unsubscribe")


def is_human_thread(labels: set[str], work: bool) -> bool:
    """Worth reading in full: any work thread, or a personal thread Gmail marks
    important/personal or that the user wrote in. The rest are read from metadata only."""
    return work or bool(labels & {"IMPORTANT", "CATEGORY_PERSONAL", "SENT"})


def fetch_gmail(
    account: str,
    source_key: str,
    days: int,
    known: dict[str, dict[str, Any]] | None = None,
    per_minute: float = GMAIL_UNITS_PER_MINUTE,
) -> list[dict[str, Any]]:
    """Inbox messages in the window plus the user's own replies in those threads.

    Every message is read as metadata (headers, labels, snippet); messages in
    threads worth reading are then re-read in full for the body.
    """
    pacer = QuotaPacer(per_minute)

    def get(url: str) -> Any:
        pacer.spend(5)
        return get_json(url, account=account, backoff=15)

    def list_ids(query: str) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        token = None
        while True:
            params = {"q": query, "maxResults": "500"}
            if token:
                params["pageToken"] = token
            page = get(f"{GMAIL}/messages?{urllib.parse.urlencode(params)}")
            out += page.get("messages", [])
            token = page.get("nextPageToken")
            if not token:
                return out

    inbox = list_ids(gmail_query(days))
    # The user's replies count in new threads and in ones already stored from earlier refreshes.
    threads = {m["threadId"] for m in inbox} | {
        r.get("thread_id") for r in (known or {}).values() if r.get("thread_id")
    }
    sent = [
        m for m in list_ids(f"in:sent newer_than:{days}d") if m["threadId"] in threads
    ]
    ids = list(dict.fromkeys(m["id"] for m in inbox + sent))
    in_inbox = {m["id"] for m in inbox}
    # A message's content never changes, so one stored from an earlier run is reused.
    known = known or {}
    meta_q = "format=metadata&" + "&".join(
        f"metadataHeaders={h}" for h in METADATA_HEADERS
    )
    todo = [i for i in ids if i not in known]
    with ThreadPoolExecutor(10) as pool:
        metas = {
            m["id"]: m
            for m in pool.map(lambda i: get(f"{GMAIL}/messages/{i}?{meta_q}"), todo)
        }
    labels_by_thread: dict[str, set[str]] = {}
    thread_of: dict[str, str] = {}
    for i in ids:
        tid = metas[i]["threadId"] if i in metas else known[i]["thread_id"]
        labels = (
            metas[i].get("labelIds", []) if i in metas else known[i].get("labels", [])
        )
        thread_of[i] = tid
        labels_by_thread.setdefault(tid, set()).update(labels)
    work = source_key == "gmail-work"
    full_ids = [
        i
        for i in ids
        if is_human_thread(labels_by_thread[thread_of[i]], work)
        and not (i in known and known[i].get("full"))
    ]
    with ThreadPoolExecutor(10) as pool:
        fulls = {
            m["id"]: m
            for m in pool.map(
                lambda i: get(f"{GMAIL}/messages/{i}?format=full"), full_ids
            )
        }
    start = cutoff(days)
    records = []
    for i in ids:
        if i in fulls or i in metas:
            rec = parse_gmail_message(fulls.get(i) or metas[i], account, source_key)
            rec["full"] = i in fulls
        else:
            rec = known[i]
        rec["in_inbox"] = i in in_inbox
        if parse_iso(rec["date"]) >= start:
            records.append(rec)
    return records


# ---------------------------------------------------------------- Slack


def _slack(method: str, **params: Any) -> dict[str, Any]:
    data = get_json(f"https://slack.com/api/{method}?{urllib.parse.urlencode(params)}")
    if not data.get("ok"):
        raise SourceError(f"slack {method}: {data.get('error')}")
    return data


def parse_slack_match(
    match: dict[str, Any], me: str, query_kind: str
) -> dict[str, Any]:
    channel = match.get("channel") or {}
    ts = match["ts"]
    thread_ts = None
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(match.get("permalink", "")).query)
    if qs.get("thread_ts"):
        thread_ts = qs["thread_ts"][0]
    return {
        "source_key": "slack",
        "id": f"{channel.get('id')}:{ts}",
        "unit": f"slack:{channel.get('id')}",
        "ts": ts,
        "thread_ts": thread_ts,
        "date": iso(dt.datetime.fromtimestamp(float(ts), dt.timezone.utc)),
        "user_id": match.get("user"),
        "from_name": match.get("username") or match.get("user"),
        "from_me": match.get("user") == me,
        "channel_id": channel.get("id"),
        "channel": channel.get("name"),
        "is_dm": bool(channel.get("is_im")),
        "is_group_dm": bool(channel.get("is_mpim")),
        "text": match.get("text") or "",
        "files": [f.get("name") for f in match.get("files") or []],
        "matched": [query_kind],
        "url": match.get("permalink"),
    }


def fetch_slack(days: int, look_later_emoji: str = LOOK_LATER_EMOJI) -> list[dict[str, Any]]:
    me = _slack("auth.test")["user_id"]
    after = (cutoff(days) - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    queries = {
        "mention": f"<@{me}> after:{after}",
        "to_me": f"to:me after:{after}",
        "from_me": f"from:<@{me}> after:{after}",
    }
    by_id: dict[str, dict[str, Any]] = {}
    for kind, query in queries.items():
        page = 1
        while True:
            data = _slack(
                "search.messages", query=query, count=100, page=page, sort="timestamp"
            )
            for match in data.get("messages", {}).get("matches", []):
                rec = parse_slack_match(match, me, kind)
                if rec["id"] in by_id:
                    by_id[rec["id"]]["matched"].append(kind)
                else:
                    by_id[rec["id"]] = rec
            paging = data.get("messages", {}).get("paging", {})
            if page >= paging.get("pages", 1):
                break
            page += 1
    start = iso(cutoff(days))
    records = [r for r in by_id.values() if r["date"] >= start]
    # The look-later mark is a setting (slack.look_later_emoji); blank turns it off.
    marked = fetch_look_later(me, look_later_emoji) if look_later_emoji else []
    names = slack_names(
        {r["user_id"] for r in records + marked if r.get("user_id")}
        | {m for r in records + marked for m in MENTION.findall(r["text"])}
    )
    for r in records + marked:
        r["from_name"] = names.get(r.get("user_id") or "", r["from_name"])
        r["text_display"] = MENTION.sub(
            lambda m: "@" + names.get(m.group(1), m.group(1)), r["text"]
        )
    record_look_later(marked, emoji=look_later_emoji)
    return records


def fetch_look_later(me: str, emoji: str = LOOK_LATER_EMOJI) -> list[dict[str, Any]]:
    """Every message the user reacted to with the look-later emoji, however old.

    Slack's search finds them (``hasmy::emoji:``); ``reactions.list`` refuses
    this token type. Slack does not say *when* a reaction was added."""
    out: list[dict[str, Any]] = []
    page = 1
    while True:
        data = _slack(
            "search.messages", query=f"hasmy::{emoji}:", count=100,
            page=page, sort="timestamp",
        )
        out += [
            parse_slack_match(m, me, "look_later")
            for m in data.get("messages", {}).get("matches", [])
        ]
        if page >= data.get("messages", {}).get("paging", {}).get("pages", 1):
            return out
        page += 1


def record_look_later(
    marked: list[dict[str, Any]],
    path: Path | None = None,
    now: dt.datetime | None = None,
    emoji: str = LOOK_LATER_EMOJI,
) -> dict[str, Any]:
    """Save the marked messages, stamping each with when a refresh first saw it.

    Slack keeps no time for a reaction, so the first refresh that sees a mark
    stands in for when it was made (refreshes run hourly). The very first run
    has no such history: every mark already there goes in ``baseline`` and is
    not added as a task, unless the user named it in ``include``. A message
    whose mark was removed drops out of ``records`` (and so off the list)."""
    path = path or LOOK_LATER_PATH
    state = read_json(path, {})
    stamp = iso(now or now_utc())
    seen = state.setdefault("first_seen", {})
    if state.get("baseline") is None:
        state["baseline"] = sorted(r["id"] for r in marked)
    state.setdefault("include", [])
    for r in marked:
        seen.setdefault(r["id"], stamp)
    state["records"] = marked
    state["emoji"] = emoji
    state["fetched_at"] = stamp
    write_json(path, state)
    return state


MENTION = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")


def slack_names(user_ids: set[str]) -> dict[str, str]:
    """User id -> real name, cached on disk (people rarely rename)."""
    path = DATA_DIR / "slack_users.json"
    cache = read_json(path, {})
    missing = sorted(u for u in user_ids if u not in cache)

    def one(uid: str) -> tuple[str, str | None]:
        try:
            user = _slack("users.info", user=uid)["user"]
        except SourceError:
            return uid, None
        profile = user.get("profile") or {}
        return uid, user.get("real_name") or profile.get("real_name") or profile.get(
            "display_name"
        ) or user.get("name")

    with ThreadPoolExecutor(4) as pool:
        for uid, name in pool.map(one, missing):
            if name:
                cache[uid] = name
    if missing:
        write_json(path, cache)
    return cache


# ---------------------------------------------------------------- Google Docs

DRIVE = "https://www.googleapis.com/drive/v3"
COMMENT_FIELDS = (
    "nextPageToken,comments(id,content,htmlContent,author(displayName,emailAddress,me),resolved,deleted,"
    "createdTime,modifiedTime,quotedFileContent,replies(id,content,author(displayName,emailAddress,me),"
    "createdTime,modifiedTime,action,deleted))"
)


def parse_doc_comment(
    doc: dict[str, Any], c: dict[str, Any], my_email: str
) -> dict[str, Any]:
    replies = [r for r in c.get("replies", []) if not r.get("deleted")]
    people = [c.get("author", {})] + [r.get("author", {}) for r in replies]
    text_all = " ".join(
        [c.get("content", "")] + [r.get("content", "") for r in replies]
    ).lower()
    owners = [o.get("emailAddress") for o in doc.get("owners", [])]
    involved = (
        any(p.get("me") for p in people)
        or my_email.lower() in text_all
        or my_email.lower() in [(o or "").lower() for o in owners]
    )
    last = replies[-1] if replies else c
    return {
        "source_key": "google-docs",
        "id": c["id"],
        "unit": f"google-docs:{doc['id']}:{c['id']}",
        "doc_id": doc["id"],
        "doc_name": doc.get("name"),
        "doc_url": doc.get("webViewLink"),
        "doc_owners": owners,
        "date": c.get("modifiedTime") or c.get("createdTime"),
        "created": c.get("createdTime"),
        "from_name": c.get("author", {}).get("displayName"),
        "from_me": bool(c.get("author", {}).get("me")),
        "last_from_me": bool(last.get("author", {}).get("me")),
        "involves_me": involved,
        "quoted": (c.get("quotedFileContent") or {}).get("value"),
        "content": c.get("content"),
        "replies": [
            {
                "author": r.get("author", {}).get("displayName"),
                "me": bool(r.get("author", {}).get("me")),
                "date": r.get("createdTime"),
                "content": r.get("content"),
                "action": r.get("action"),
            }
            for r in replies
        ],
        "url": f"https://docs.google.com/document/d/{doc['id']}/edit?disco={c['id']}",
    }


def fetch_docs(account: str, days: int) -> list[dict[str, Any]]:
    """Open (unresolved) comment threads on Google Docs modified in the window."""
    q = (
        "mimeType='application/vnd.google-apps.document' and trashed=false "
        f"and modifiedTime > '{iso(cutoff(days))[:19]}'"
    )
    files: list[dict[str, Any]] = []
    token = None
    while True:
        params = {
            "q": q,
            "pageSize": "100",
            "orderBy": "modifiedTime desc",
            "fields": "nextPageToken,files(id,name,modifiedTime,webViewLink,owners(emailAddress))",
            "includeItemsFromAllDrives": "true",
            "supportsAllDrives": "true",
        }
        if token:
            params["pageToken"] = token
        page = get_json(
            f"{DRIVE}/files?{urllib.parse.urlencode(params)}", account=account
        )
        files += page.get("files", [])
        token = page.get("nextPageToken")
        if not token:
            break

    def comments(doc: dict[str, Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        token = None
        while True:
            params = {"pageSize": "100", "fields": COMMENT_FIELDS}
            if token:
                params["pageToken"] = token
            page = get_json(
                f"{DRIVE}/files/{doc['id']}/comments?{urllib.parse.urlencode(params)}",
                account=account,
            )
            out += page.get("comments", [])
            token = page.get("nextPageToken")
            if not token:
                return out

    with ThreadPoolExecutor(6) as pool:
        all_comments = list(pool.map(comments, files))
    records = []
    for doc, cs in zip(files, all_comments):
        for c in cs:
            if c.get("resolved") or c.get("deleted"):
                continue
            records.append(parse_doc_comment(doc, c, account))
    return records


# ---------------------------------------------------------------- Notion

MCP_URL = "https://mcp.notion.com/mcp"
MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2025-06-18",
}


def _mcp_body(stdout: bytes) -> dict[str, Any]:
    text = stdout.decode("utf-8", "replace")
    for line in text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    return json.loads(text)


def notion_call(account: str, tool: str, arguments: dict[str, Any]) -> Any:
    """One tools/call on Notion's hosted MCP server (initialize, then call)."""
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "collect-outstanding-tasks", "version": "1"},
        },
    }
    proc = latchkey_curl(
        MCP_URL,
        account=account,
        method="POST",
        headers=MCP_HEADERS,
        data=json.dumps(init),
        extra=["-D", "-", "-o", "/dev/null"],
    )
    match = re.search(rb"(?im)^mcp-session-id:\s*(\S+)", proc.stdout)
    if not match:
        raise SourceError(
            f"notion: no MCP session ({proc.stdout[:200]!r} {proc.stderr[:200]!r})"
        )
    headers = dict(MCP_HEADERS, **{"Mcp-Session-Id": match.group(1).decode()})
    latchkey_curl(
        MCP_URL,
        account=account,
        method="POST",
        headers=headers,
        data='{"jsonrpc":"2.0","method":"notifications/initialized"}',
    )
    call = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    proc = latchkey_curl(
        MCP_URL,
        account=account,
        method="POST",
        headers=headers,
        data=json.dumps(call),
    )
    body = _mcp_body(proc.stdout)
    result = body.get("result") or {}
    text = "".join(c.get("text", "") for c in result.get("content", []))
    if result.get("isError") or "error" in body:
        raise SourceError(f"notion {tool}: {text or body.get('error')}"[:400])
    return json.loads(text)


def clean_notion_text(value: str) -> str:
    return re.sub(
        r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value or ""))
    ).strip()


def parse_notion_row(row: dict[str, Any]) -> dict[str, Any]:
    page_id = row["url"].rstrip("/").rsplit("/", 1)[-1].split("?")[0]
    return {
        "source_key": "notion",
        "id": page_id,
        "unit": f"notion:{page_id}",
        "date": row.get("Last Edited"),
        "from_me": True,
        "title": clean_notion_text(row.get("Write a task", "")),
        "notes": clean_notion_text(row.get("Notes", "")),
        "day": row.get("Day"),
        "week": row.get("Week"),
        "done": row.get(" ") == "__YES__",
        "row": row,
        "url": row["url"],
    }


def fetch_notion(account: str, data_source: str, days: int) -> list[dict[str, Any]]:
    """Unchecked rows of the user's Notion to-do database edited inside the window."""
    data = notion_call(
        account,
        "notion-query-data-sources",
        {
            "data": {
                "mode": "rows",
                "data_source_url": data_source,
                "filter": {
                    "type": "group",
                    "operator": "and",
                    "filters": [
                        {
                            "type": "property",
                            "property": " ",
                            "propertyType": "checkbox",
                            "operator": "checkbox_is",
                            "value": {"type": "exact", "value": False},
                        }
                    ],
                },
                "limit": 100,
            }
        },
    )
    return open_notion_rows(data, iso(cutoff(days)))


def open_notion_rows(data: dict[str, Any], start: str) -> list[dict[str, Any]]:
    """Unchecked, non-empty rows last edited at or after ``start`` (ISO)."""
    rows = [parse_notion_row(r) for r in data.get("results", [])]
    return [
        r for r in rows if r["title"] and not r["done"] and (r["date"] or "") >= start
    ]
