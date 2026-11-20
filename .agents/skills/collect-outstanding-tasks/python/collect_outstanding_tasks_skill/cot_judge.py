"""The model step: read each conversation and pick out what the user owes.

Records are grouped into *units* (an email thread, a Slack channel, a Docs
comment thread, a text or WhatsApp chat). Each unit is rendered to compact
text, and units are packed into batches for one Claude call each.

A unit's verdict is cached by a hash of its rendered text, so a re-run only
pays for conversations that changed. When a changed unit is re-judged, the
tasks it had before are shown to the model so it can keep their ids -- that
is what lets the app keep a checked-off task checked across refreshes.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import time
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from collect_outstanding_tasks_skill.cot_common import SOURCES, Settings, now_utc

PROMPT_VERSION = "2"
DEFAULT_MODEL = "claude-sonnet-5"
# claude -p takes the prompt as one argv string, which Linux caps at 128 KB
# (bytes, not characters: emoji and accents count 2-4 each).
MAX_BATCH_BYTES = 90_000
# Small batches run side by side: one call over 40 conversations took ~100s,
# five calls over 8 each finish in ~25s together, at about the same cost.
MAX_BATCH_UNITS = 8
MAX_UNIT_CHARS = 40_000
# The user's "Not a task" corrections ride along in the system prompt, newest and
# reasoned ones first, up to this many characters.
MAX_FEEDBACK_CHARS = 8_000
# Chats (texts, WhatsApp, Slack) are judged one calendar week at a time: a busy
# chat holds thousands of messages a month, and past weeks then stay cached
# while only the current week is re-read.
CHAT_SOURCES = ("imessage", "whatsapp", "slack")
WORKERS = 8

SYSTEM = """\
You read the user's recent messages and list the things they still owe someone \
or have to do. Who the user is comes at the end of these instructions. Messages \
marked "me" are the user's.

INCLUDE as a task:
- a request or question to the user that they have not answered or done
- something the user said they would do ("I'll send it", "will do this weekend")
- a deadline, sign-up, RSVP, form, appointment to book, bill or payment the user has to act on
- recruiter follow-ups addressed to the user personally
- an open Google Docs comment thread where someone else wrote last, replying to the \
user or asking them something: the user still has to answer or resolve it
EXCLUDE:
- anything the user already handled: they replied with the answer, said it's done, or \
the other person said it's resolved
- sales pitches and cold outreach, newsletters, marketing, receipts and shipping \
notices, verification codes, automated notifications with nothing to do
- appointment reminders that need no action, FYIs, chit-chat, jokes, reactions
- requests addressed to someone else in a group

Write each task as a short imperative title in plain words (under 12 words, name \
the person), and a one-line "why" giving the context the user needs (who asked, \
amounts, times). Both are read by the user themselves: address them directly as \
"you" / "your" (e.g. "Jordan asked you to ..."); never call the user "she", "he", \
"they", "the user" or by name. "due" is a YYYY-MM-DD date only if the messages state or clearly \
imply one. "link" is a URL copied exactly from the messages only when acting on \
the task means opening that link (a sign-up form, a payment page); otherwise null. \
"record" is the bracketed number of the message the task comes from. "sender" \
is null unless that message's sender is shown only as a phone number or short code: \
then give the business or person the text says it is from (e.g. "CVS Pharmacy").

If a unit lists PREVIOUS TASKS and one of your tasks is the same task, set \
"same_as" to that previous id (even if you word it differently); otherwise null. \
Drop a previous task only if the messages now show it is handled. A previous task \
marked DISMISSED was rejected by the user as not a task: never list it again, not \
even reworded. A previous task marked DONE is one the user already checked off: \
never list it again, not even reworded, unless the messages show a new, separate \
request made after it was done (a reminder about the same thing is not new).

One unit can give zero, one, or several tasks. For a unit with no task, give \
"skip" with a category (one of: handled, sales, recruiter-no-action, newsletter, \
receipt, code, notification, fyi, chit-chat, other) and a few-word note.

Reply with JSON only, no prose and no code fence:
{"units":[{"unit":"U1","tasks":[{"record":"3","title":"...","why":"...","due":null,\
"link":null,"sender":null,"same_as":null}],"skip":null}]}
Every unit in the input must appear exactly once in the output."""


def about_user(settings: Settings) -> str:
    """Who the user is, for the end of a system prompt (from the settings file)."""
    facts = []
    if settings.employer:
        facts.append(f"works at {settings.employer}")
    if settings.work_email:
        facts.append(f"work email {settings.work_email}")
    if settings.personal_email:
        facts.append(f"personal email {settings.personal_email}")
    who = (
        f"The user is {settings.name}"
        if settings.name
        else "The user's name is not given"
    )
    return f"ABOUT THE USER: {who}" + (f" ({'; '.join(facts)})" if facts else "") + "."


SKIP_CATEGORIES = {
    "handled",
    "sales",
    "recruiter-no-action",
    "newsletter",
    "receipt",
    "code",
    "notification",
    "fyi",
    "chit-chat",
    "other",
}
URL_RE = re.compile(r"https?://[^\s<>\"')\]|]+")
QUOTE_TAIL = re.compile(
    r"\n(On .{5,200}wrote:|-{2,} ?Original Message|From: .+\nSent: ).*", re.S
)


def _trim_email(body: str, limit: int = 2500) -> str:
    body = QUOTE_TAIL.sub("", body or "")
    body = "\n".join(line for line in body.splitlines() if not line.startswith(">"))
    body = re.sub(r"\n\s*\n+", "\n", body).strip()
    return body[:limit] + (" ..." if len(body) > limit else "")


def _short(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:limit] + (" ..." if len(text) > limit else "")


def _day(iso_date: str) -> str:
    return iso_date[:16].replace("T", " ")


@dataclass
class Unit:
    key: str
    source_key: str
    records: list[dict[str, Any]]
    text: str = ""
    aliases: dict[str, str] = field(default_factory=dict)  # "3" -> record id

    @property
    def hash(self) -> str:
        return hashlib.sha256(f"{PROMPT_VERSION}\n{self.text}".encode()).hexdigest()[
            :24
        ]

    @property
    def latest(self) -> str:
        return max(r["date"] for r in self.records)


def render_unit(unit: Unit) -> None:
    recs = sorted(unit.records, key=lambda r: r["date"])
    sk = unit.source_key
    lines: list[str] = []
    first = recs[0]
    if sk.startswith("gmail"):
        lines.append(
            f"{'work' if sk == 'gmail-work' else 'personal'} email thread: {first.get('subject')!r}"
        )
        for i, r in enumerate(recs, 1):
            unit.aliases[str(i)] = r["id"]
            who = "me" if r["from_me"] else r.get("from")
            if not r.get("full", True):  # read from metadata only (likely automated)
                lines.append(
                    f"[{i}] {_day(r['date'])} from {who}: {_short(r.get('snippet', ''), 200)}"
                )
            else:
                lines.append(
                    f"[{i}] {_day(r['date'])} from {who} to {_short(r.get('to') or '', 120)}"
                )
                lines.append(_trim_email(r.get("body") or r.get("snippet", "")))
    elif sk == "slack":
        kind = (
            "DM"
            if first.get("is_dm")
            else "group DM"
            if first.get("is_group_dm")
            else f"#{first.get('channel')}"
        )
        lines.append(
            f"Slack {kind}, week of {week_of(first['date'])} (only messages that mention the user, were sent to them, or are theirs)"
        )
        for i, r in enumerate(recs, 1):
            unit.aliases[str(i)] = r["id"]
            who = "me" if r["from_me"] else r.get("from_name")
            thread = (
                " (in thread)"
                if r.get("thread_ts") and r.get("thread_ts") != r.get("ts")
                else ""
            )
            lines.append(
                f"[{i}] {_day(r['date'])} {who}{thread}: {_short(r.get('text_display') or r['text'], 1500)}"
            )
    elif sk == "google-docs":
        r = first
        unit.aliases["1"] = r["id"]
        lines.append(f"Google Doc {r.get('doc_name')!r} open comment thread")
        if r.get("quoted"):
            lines.append(f"on text: {_short(r['quoted'], 300)!r}")
        lines.append(
            f"[1] {_day(r.get('created') or r['date'])} {'me' if r['from_me'] else r.get('from_name')}: {_short(r.get('content') or '', 1500)}"
        )
        for rep in r.get("replies", []):
            lines.append(
                f"    reply {_day(rep.get('date') or '')} {'me' if rep.get('me') else rep.get('author')}: {_short(rep.get('content') or '', 800)}"
            )
    else:  # texts / WhatsApp
        label = "Text" if sk == "imessage" else "WhatsApp"
        lines.append(
            f"{label} {'group ' if first.get('is_group') else ''}chat: {first.get('chat')}, week of {week_of(first['date'])} (later weeks are read separately)"
        )
        for i, r in enumerate(recs, 1):
            unit.aliases[str(i)] = r["id"]
            who = "me" if r["from_me"] else r.get("from_name")
            lines.append(f"[{i}] {_day(r['date'])} {who}: {_short(r['text'], 600)}")
    text = "\n".join(lines)
    if len(text) > MAX_UNIT_CHARS:
        text = lines[0] + "\n(earlier messages cut)\n" + text[-MAX_UNIT_CHARS:]
    unit.text = text


def week_of(iso_date: str) -> str:
    year, week, _ = dt.date.fromisoformat(iso_date[:10]).isocalendar()
    return f"{year}-W{week:02d}"


def unit_key_for(record: dict[str, Any]) -> str:
    if record["source_key"] in CHAT_SOURCES:
        return f"{record['unit']}#{week_of(record['date'])}"
    return record["unit"]


def base_unit(unit_key: str) -> str:
    """The conversation a (possibly week-sliced) unit key belongs to."""
    return unit_key.split("#", 1)[0]


def build_units(records: list[dict[str, Any]]) -> list[Unit]:
    """Group records into judgeable units. Notion rows are tasks already and skip the model."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        if r["source_key"] == "notion":
            continue
        if r["source_key"] == "google-docs" and not r.get("involves_me"):
            continue
        groups[unit_key_for(r)].append(r)
    units = []
    for key, recs in groups.items():
        unit = Unit(key=key, source_key=recs[0]["source_key"], records=recs)
        render_unit(unit)
        units.append(unit)
    units.sort(key=lambda u: (u.source_key, u.key))
    return units


def pack_batches(units: list[Unit]) -> list[list[Unit]]:
    batches: list[list[Unit]] = []
    current: list[Unit] = []
    size = 0
    for u in units:
        n = len(u.text.encode()) + 200
        if current and (size + n > MAX_BATCH_BYTES or len(current) >= MAX_BATCH_UNITS):
            batches.append(current)
            current, size = [], 0
        current.append(u)
        size += n
    if current:
        batches.append(current)
    return batches


def dismissed_ids(dismissed: dict[str, Any]) -> set[str]:
    """Every task id the user rejected, including the duplicates folded into one."""
    ids = set(dismissed)
    for entry in dismissed.values():
        if isinstance(entry, dict):
            ids.update(str(i) for i in entry.get("merged_ids") or [])
    return ids


def _clip(text: Any, limit: int) -> str:
    return _short(str(text or ""), limit)


def done_ids(completed: dict[str, Any]) -> set[str]:
    """Every task id the user checked off, including the duplicates folded into one."""
    ids = set(completed)
    for entry in completed.values():
        if isinstance(entry, dict):
            ids.update(str(i) for i in entry.get("merged_ids") or [])
    return ids


def done_block(completed: dict[str, Any], max_chars: int = MAX_FEEDBACK_CHARS) -> str:
    """What the user already checked off, newest first, so it is not listed again."""
    entries = [e for e in completed.values() if isinstance(e, dict) and e.get("title")]
    entries.sort(key=lambda e: str(e.get("at") or ""), reverse=True)
    if not entries:
        return ""
    header = (
        "ALREADY DONE: the user checked these off. Do not list them again, not even "
        "reworded or from another source (a reminder, a follow-up about the same "
        "thing). List one only if the messages show a new, separate request made after "
        "it was checked off."
    )
    lines: list[str] = []
    size = len(header)
    for e in entries:
        line = f'- "{_clip(e.get("title"), 100)}" ({e.get("source") or "?"}, from {_clip(e.get("from"), 40) or "?"}; done {str(e.get("at") or "")[:10]})'
        if size + len(line) + 1 > max_chars:
            lines.append(f"- ... and {len(entries) - len(lines)} older ones")
            break
        lines.append(line)
        size += len(line) + 1
    return header + "\n" + "\n".join(lines)


def feedback_block(
    dismissed: dict[str, Any], max_chars: int = MAX_FEEDBACK_CHARS
) -> str:
    """The user's "Not a task" removals as rules for the system prompt.

    Removals with a reason come first (they say *why*, so they generalise), then
    bare removals; within each, newest first. Lines stop at ``max_chars``.
    """
    entries = [
        e
        for e in dismissed.values()
        if isinstance(e, dict) and (e.get("title") or e.get("reason"))
    ]
    entries.sort(key=lambda e: str(e.get("at") or ""), reverse=True)
    entries.sort(key=lambda e: not str(e.get("reason") or "").strip())
    if not entries:
        return ""
    header = (
        "THE USER'S CORRECTIONS: on earlier lists they marked these as 'Not a task'. Do "
        "not pull tasks like them. Where they gave a reason, treat it as a rule that "
        "overrides the rules above; lean away from tasks that resemble the ones they "
        "removed without a reason."
    )
    lines: list[str] = []
    size = len(header)
    for e in entries:
        where = f"{e.get('source') or '?'}, from {_clip(e.get('from'), 40) or '?'}"
        reason = str(e.get("reason") or "").strip()
        if reason:
            line = f'- "{_clip(e.get("title"), 100)}" ({where}; {_clip(e.get("why"), 120)}) -- their reason: {_clip(reason, 300)}'
            if e.get("excerpt"):
                line += f' [message: "{_clip(e.get("excerpt"), 160)}"]'
        else:
            line = f'- "{_clip(e.get("title"), 100)}" ({where})'
        if size + len(line) + 1 > max_chars:
            lines.append(f"- ... and {len(entries) - len(lines)} older removals")
            break
        lines.append(line)
        size += len(line) + 1
    return header + "\n" + "\n".join(lines)


def build_prompt(
    batch: list[Unit],
    previous: dict[str, dict[str, Any]],
    today: dt.date,
    rejected: set[str] | frozenset[str] = frozenset(),
    done: set[str] | frozenset[str] = frozenset(),
) -> str:
    parts = [f"Today is {today.isoformat()} ({today.strftime('%A')}). Times are UTC.\n"]
    for i, u in enumerate(batch, 1):
        parts.append(f"=== UNIT U{i}")
        parts.append(u.text)
        prev = previous.get(u.key, {}).get("tasks") or []
        if prev:
            parts.append(
                "PREVIOUS TASKS: "
                + "; ".join(
                    f"[{t['id']}] {t['title']}"
                    + (" (DISMISSED by the user)" if t["id"] in rejected else "")
                    + (" (DONE: the user checked it off)" if t["id"] in done else "")
                    for t in prev
                )
            )
        parts.append("")
    return "\n".join(parts)


def parse_reply(text: str) -> dict[str, Any]:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError(f"no JSON object in reply: {text[:200]!r}")
    return json.loads(text[start : end + 1])


def task_id(source_key: str, unit_key: str, record_id: str, title: str) -> str:
    digest = hashlib.sha1(f"{unit_key}|{record_id}|{title}".encode()).hexdigest()[:12]
    return f"{SOURCES[source_key][0]}-{digest}"


def apply_verdict(
    unit: Unit, verdict: dict[str, Any], previous: dict[str, Any] | None
) -> dict[str, Any]:
    """Validate one unit's model verdict into a cache entry (ids resolved, links checked)."""
    prev_ids = {t["id"] for t in (previous or {}).get("tasks", [])}
    used: set[str] = set()
    tasks = []
    fallback = next(
        (
            r["id"]
            for r in reversed(sorted(unit.records, key=lambda r: r["date"]))
            if not r["from_me"]
        ),
        unit.records[-1]["id"],
    )
    for t in verdict.get("tasks") or []:
        title = (t.get("title") or "").strip()
        if not title:
            continue
        record_id = unit.aliases.get(str(t.get("record", "")).strip("[] "), fallback)
        link = t.get("link")
        if not (isinstance(link, str) and URL_RE.fullmatch(link) and link in unit.text):
            link = None
        due = t.get("due")
        if not (isinstance(due, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", due)):
            due = None
        same = t.get("same_as")
        tid = (
            same
            if same in prev_ids and same not in used
            else task_id(unit.source_key, unit.key, record_id, title)
        )
        while tid in used:
            tid += "x"
        used.add(tid)
        sender = (
            t.get("sender")
            if isinstance(t.get("sender"), str) and t.get("sender", "").strip()
            else None
        )
        tasks.append(
            {
                "id": tid,
                "record_id": record_id,
                "title": title,
                "why": (t.get("why") or "").strip(),
                "due": due,
                "link": link,
                "sender": sender,
            }
        )
    skip = None
    if not tasks:
        raw_skip = verdict.get("skip") or {}
        category = (
            raw_skip.get("category")
            if raw_skip.get("category") in SKIP_CATEGORIES
            else "other"
        )
        skip = {"category": category, "note": (raw_skip.get("note") or "").strip()}
    return {"hash": unit.hash, "tasks": tasks, "skip": skip}


@dataclass
class JudgeStats:
    units: int = 0
    cached: int = 0
    judged: int = 0
    failed: int = 0
    calls: int = 0
    cost_usd: float = 0.0
    seconds: float = 0.0
    errors: list[str] = field(default_factory=list)


Completer = Callable[[str, str, str], tuple[str, float]]


def claude_completer(prompt: str, system: str, model: str) -> tuple[str, float]:
    from collect_outstanding_tasks_skill.claude_p import claude_p_completion

    result = claude_p_completion(prompt, system=system, model=model)
    return result.text, result.cost_usd


# The duplicate check thought for ~23k tokens (four to five minutes) when left
# uncapped; with this cap it answers in under a minute, and with none at all it
# folds different to-dos together (two different hotel bookings).
MERGE_THINKING_TOKENS = 3000


def merge_completer(prompt: str, system: str, model: str) -> tuple[str, float]:
    from collect_outstanding_tasks_skill.claude_p import claude_p_completion

    result = claude_p_completion(
        prompt, system=system, model=model, thinking_tokens=MERGE_THINKING_TOKENS
    )
    return result.text, result.cost_usd


def judge_units(
    units: list[Unit],
    cache: dict[str, dict[str, Any]],
    *,
    model: str = DEFAULT_MODEL,
    completer: Completer = claude_completer,
    workers: int = WORKERS,
    today: dt.date | None = None,
    log: Callable[[str], None] = lambda _: None,
    dismissed: dict[str, Any] | None = None,
    settings: Settings | None = None,
    completed: dict[str, Any] | None = None,
) -> tuple[dict[str, dict[str, Any]], JudgeStats]:
    """Return the new cache (only the given units' entries) and run stats.

    A unit whose hash matches its cache entry is reused as-is. A unit whose batch
    fails keeps its previous entry (marked stale) if it had one.

    ``dismissed`` (the app's dismissed.json) is not part of the cache key: the
    user's corrections steer every conversation read from then on, and a task they
    dismissed is dropped from the list by build whatever the cache holds.
    """
    dismissed = dismissed or {}
    rejected = dismissed_ids(dismissed)
    done = done_ids(completed or {})
    feedback = feedback_block(dismissed)
    already = done_block(completed or {})
    system = SYSTEM + "\n\n" + about_user(settings or Settings())
    system += "".join("\n\n" + b for b in (feedback, already) if b)
    started = time.monotonic()
    today = today or now_utc().date()
    stats = JudgeStats(units=len(units))
    new_cache: dict[str, dict[str, Any]] = {}
    todo = []
    for u in units:
        entry = cache.get(u.key)
        if entry and entry.get("hash") == u.hash and not entry.get("stale"):
            new_cache[u.key] = entry
            stats.cached += 1
        else:
            todo.append(u)
    batches = pack_batches(todo)
    log(
        f"{stats.cached} conversations unchanged, {len(todo)} to read in {len(batches)} model calls"
    )

    def run(
        batch: list[Unit],
    ) -> tuple[list[Unit], dict[str, Any] | None, float, str | None]:
        prompt = build_prompt(batch, cache, today, rejected, done)
        cost = 0.0
        error = None
        for _attempt in range(2):
            try:
                text, c = completer(prompt, system, model)
                cost += c
                return batch, parse_reply(text), cost, None
            except Exception as exc:  # noqa: BLE001 -- one batch must not sink the run
                error = f"{type(exc).__name__}: {str(exc)[:300]}"
        return batch, None, cost, error

    with ThreadPoolExecutor(max(1, workers)) as pool:
        for batch, reply, cost, error in pool.map(run, batches):
            stats.calls += 1
            stats.cost_usd += cost
            verdicts = {}
            if reply is not None:
                for v in reply.get("units", []):
                    verdicts[str(v.get("unit", ""))] = v
            for i, u in enumerate(batch, 1):
                verdict = verdicts.get(f"U{i}")
                if verdict is None:
                    stats.failed += 1
                    if error is None:
                        error = "model reply left out some conversations"
                    if u.key in cache:
                        new_cache[u.key] = dict(cache[u.key], stale=True)
                    continue
                entry = apply_verdict(u, verdict, cache.get(u.key))
                entry["judged_at"] = now_utc().isoformat(timespec="seconds")
                entry["model"] = model
                new_cache[u.key] = entry
                stats.judged += 1
            if error:
                stats.errors.append(error)
                log(f"a model call failed: {error}")
    stats.seconds = round(time.monotonic() - started, 1)
    return new_cache, stats


# ---------------------------------------------------------------- duplicates

MERGE_SYSTEM = """\
You get the user's to-do list, gathered from email, Slack, texts, WhatsApp, Docs and \
Notion. The same to-do often appears more than once: a reminder sent every week, \
the same request by email and by text, a commitment they repeated. Group the items \
that are the same real-world to-do (same action on the same thing for the same \
person). A later message about the same deliverable from the same sender is the \
same to-do even when a detail changed (a reminder, a new deadline, "pet photos are \
fine too"). Different to-dos that merely look alike (two different bills, two \
pets' separate appointments, two separate charges) are NOT duplicates.

Items marked DONE are ones the user already checked off: put a new item in a group with \
a DONE item when it is the same to-do (a reminder or follow-up about it), so it is \
not listed again.

Reply with JSON only, no prose and no code fence: {"groups":[["id1","id2"],...]} \
listing only groups of two or more ids, each id in at most one group."""


MERGE_NEW_SYSTEM = """\
You get the user's to-do list, gathered from email, Slack, texts, WhatsApp, Docs and \
Notion, in two parts. LIST is what is already on it (items marked DONE are ones the \
user already checked off). NEW is what was just found. The same to-do often appears more \
than once: a reminder sent every week, the same request by email and by text, a \
commitment they repeated. For each NEW item, find the LIST or NEW items that are the \
same real-world to-do (same action on the same thing for the same person). Different \
to-dos that merely look alike (two different bills, two pets' separate appointments) \
are NOT duplicates. A NEW item that is the same to-do as a DONE item goes in a group \
with it, so it is not listed again.

Reply with JSON only, no prose and no code fence: {"groups":[["id1","id2"],...]} \
listing only groups of two or more ids that each contain at least one NEW id."""


def merge_key(tasks: list[dict[str, Any]]) -> str:
    lines = sorted(f"{t['id']}|{t['title']}|{t['why']}" for t in tasks)
    return hashlib.sha256(
        f"{PROMPT_VERSION}\n{MERGE_SYSTEM}\n".encode() + "\n".join(lines).encode()
    ).hexdigest()[:24]


def item_fingerprint(task: dict[str, Any]) -> str:
    return hashlib.sha256(f"{task['title']}\n{task['why']}".encode()).hexdigest()[:16]


def done_anchors(completed: dict[str, Any]) -> list[dict[str, Any]]:
    """Checked-off tasks as entries for the duplicate check, marked DONE."""
    return [
        {
            "id": i,
            "source": e.get("source") or "?",
            "from": e.get("from"),
            "date": e.get("date") or e.get("at"),
            "title": f"DONE: {e.get('title')}",
            "why": e.get("why") or "",
        }
        for i, e in completed.items()
        if isinstance(e, dict) and e.get("title")
    ]


def _merge_line(t: dict[str, Any]) -> str:
    return f"[{t['id']}] ({t['source']}, from {t.get('from') or '?'}, {(t.get('date') or '')[:10]}) {t['title']} -- {t['why']}"


def _union_groups(*group_lists: list[list[str]]) -> list[list[str]]:
    """Join groups that share an id, so each id ends up in exactly one group."""
    parent: dict[str, str] = {}

    def root(i: str) -> str:
        while parent.setdefault(i, i) != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    order: list[str] = []
    for groups in group_lists:
        for g in groups:
            for i in g:
                if i not in parent:
                    order.append(i)
                parent[root(i)] = root(g[0])
    joined: dict[str, list[str]] = defaultdict(list)
    for i in order:
        joined[root(i)].append(i)
    return [g for g in joined.values() if len(g) >= 2]


def _valid_groups(raw_groups: Any, ids: set[str], must_touch: set[str] | None = None) -> list[list[str]]:
    seen: set[str] = set()
    groups = []
    for g in raw_groups if isinstance(raw_groups, list) else []:
        if not isinstance(g, list):
            continue
        members = [
            i for i in dict.fromkeys(str(x) for x in g) if i in ids and i not in seen
        ]
        if len(members) >= 2 and (must_touch is None or must_touch & set(members)):
            seen.update(members)
            groups.append(members)
    return groups


def find_duplicates(
    tasks: list[dict[str, Any]],
    previous: dict[str, Any],
    *,
    model: str = DEFAULT_MODEL,
    completer: Completer = merge_completer,
) -> tuple[dict[str, Any], float]:
    """Groups of task ids that are the same to-do.

    Reuses ``previous`` when the list is unchanged. Otherwise only the items that
    are new (or reworded) since ``previous`` are checked against the rest of the
    list, and the answer is joined with the groups found before: re-checking the
    whole list every run is what made a refresh take minutes. With no earlier
    check to build on (``previous`` has no ``seen``) the whole list is checked.
    On a failed call the previous groups that still apply are kept, and the new items stay
    unseen so the next run checks them again.
    Checked-off tasks may be passed in as ``done_anchors`` entries."""
    key = merge_key(tasks)
    if previous.get("key") == key and "seen" in previous:
        return previous, 0.0
    ids = {t["id"] for t in tasks}
    fingerprints = {t["id"]: item_fingerprint(t) for t in tasks}
    seen_before = previous.get("seen")
    kept = [
        [i for i in g if i in ids]
        for g in previous.get("groups", [])
        if sum(i in ids for i in g) >= 2
    ]
    if isinstance(seen_before, dict):
        new = [t for t in tasks if seen_before.get(t["id"]) != fingerprints[t["id"]]]
        new_ids = {t["id"] for t in new}
        if not new:
            return {"key": key, "groups": _union_groups(kept), "seen": fingerprints}, 0.0
        listed = [t for t in tasks if t["id"] not in new_ids]
        prompt = "LIST:\n" + "\n".join(map(_merge_line, listed)) + "\n\nNEW:\n" + "\n".join(map(_merge_line, new))
        system = MERGE_NEW_SYSTEM
    else:
        new_ids = None
        prompt = "\n".join(map(_merge_line, tasks))
        system = MERGE_SYSTEM
    try:
        text, cost = completer(prompt, system, model)
        raw_groups = parse_reply(text).get("groups") or []
    except Exception as exc:  # noqa: BLE001 -- merging is a nicety; never sink the run
        return {
            "key": None,
            "groups": _union_groups(kept),
            "seen": {i: f for i, f in (seen_before or {}).items() if i in ids},
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
        }, 0.0
    found = _valid_groups(raw_groups, ids, new_ids)
    base = kept if new_ids is not None else []
    return {"key": key, "groups": _union_groups(base, found), "seen": fingerprints}, cost


# ---------------------------------------------------------------- voice

# The list is read by the user themselves, so every title and "why" speaks to them
# as "you". The judge prompt asks for that, but verdicts cached from before it (and
# any slip) are reworded here: one call per run over just the tasks that may still
# talk about the user in the third person, cached by their original wording.
_PRONOUNS = r"she|her|hers|herself|he|him|his|himself|the user|the user's"


def third_person_re(name: str = "") -> re.Pattern[str]:
    """Words that may refer to the user in the third person: pronouns and their name."""
    names = [
        re.escape(n) for n in dict.fromkeys([name.strip(), *name.split()[:1]]) if n
    ]
    words = "|".join([*(f"{n}(?:'s)?" for n in names), _PRONOUNS])
    return re.compile(rf"\b({words})\b", re.IGNORECASE)


VOICE_SYSTEM = """\
You reword items on the user's to-do list so they speak to the user directly. \
Rewrite each item's title and why in the second person ("you", "your") wherever \
they refer to the user (as "she", "he", "they", "the user" or by name; who the \
user is comes at the end of these instructions). Change nothing else: keep every \
other person's name, the facts, amounts and dates, and about the same length. \
Pronouns that refer to someone other than the user stay as they are.

Reply with JSON only, no prose and no code fence: \
{"items":[{"id":"...","title":"...","why":"..."}]} with one entry per input item."""


def voice_key(title: str, why: str) -> str:
    return hashlib.sha256(f"{VOICE_SYSTEM}\n{title}\n{why}".encode()).hexdigest()[:20]


def needs_voice(task: dict[str, Any], pattern: re.Pattern[str] | None = None) -> bool:
    pattern = pattern or third_person_re()
    return bool(pattern.search(f"{task.get('title') or ''} {task.get('why') or ''}"))


def fix_voice(
    tasks: list[dict[str, Any]],
    previous: dict[str, Any],
    *,
    model: str = DEFAULT_MODEL,
    completer: Completer = claude_completer,
    name: str = "",
) -> tuple[dict[str, Any], float, str | None]:
    """Second-person rewordings keyed by ``voice_key`` of the original wording.

    Keeps only entries for tasks still on the list; asks the model about the
    ones that need it and are not cached yet. A failed call keeps the cache and
    returns the error (the tasks then show their original wording)."""
    pattern = third_person_re(name)
    wanted = {
        voice_key(t["title"], t.get("why") or ""): t
        for t in tasks
        if needs_voice(t, pattern)
    }
    cache = {k: v for k, v in previous.items() if k in wanted}
    todo = {k: t for k, t in wanted.items() if k not in cache}
    if not todo:
        return cache, 0.0, None
    lines = [
        json.dumps(
            {"id": k, "title": t["title"], "why": t.get("why") or ""},
            ensure_ascii=False,
        )
        for k, t in todo.items()
    ]
    try:
        system = VOICE_SYSTEM + "\n\n" + about_user(Settings(name=name))
        text, cost = completer("\n".join(lines), system, model)
        items = parse_reply(text).get("items") or []
    except Exception as exc:  # noqa: BLE001 -- wording is a nicety; never sink the run
        return cache, 0.0, f"{type(exc).__name__}: {str(exc)[:200]}"
    for item in items:
        k = str(item.get("id") or "")
        if k in todo and str(item.get("title") or "").strip():
            cache[k] = {
                "title": str(item["title"]).strip(),
                "why": str(item.get("why") or "").strip(),
            }
    return cache, cost, None


def apply_voice(tasks: list[dict[str, Any]], cache: dict[str, Any]) -> None:
    """Swap in the second-person wording for every task the cache covers."""
    for t in tasks:
        hit = cache.get(voice_key(t["title"], t.get("why") or ""))
        if hit:
            t["title"], t["why"] = hit["title"], hit["why"]
