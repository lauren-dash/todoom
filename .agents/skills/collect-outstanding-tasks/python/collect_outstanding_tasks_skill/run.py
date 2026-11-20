"""Collect the user's outstanding tasks from every source into one task list.

Subcommands (each a step; ``all`` chains them):
  fetch   pull the last N days from each source into the raw store
  judge   have Claude pick the tasks out of each changed conversation
  build   write the task list (tasks.json) the Task Inbox app reads
  all     fetch, judge, build
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from collect_outstanding_tasks_skill.cot_common import (
    DEFAULT_DAYS,
    LOOK_LATER_PATH,
    MAC_SOURCES,
    SOURCE_LABELS,
    SOURCES,
    Settings,
    SettingsError,
    iso,
    load_settings,
    now_utc,
    read_json,
    write_json,
)
from collect_outstanding_tasks_skill.cot_store import (
    JUDGE_STATUS_PATH,
    JUDGEMENTS_PATH,
    LOOK_LATER_PREFIX,
    MERGES_PATH,
    MESSAGE_SOURCES,
    OPEN_TASKS_PATH,
    STATUS_PATH,
    TASKS_PATH,
    VOICE_PATH,
    build_task_list,
    fetch_days,
    load_all_snapshots,
    load_completed,
    load_dismissed,
    load_store,
    record_status,
    save_snapshot,
)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _fetchers(
    days: dict[str, int], contacts: dict[str, str], settings: Settings
) -> dict[str, Callable[[], list[dict[str, Any]]]]:
    from collect_outstanding_tasks_skill import cot_cloud, cot_mac

    s = settings
    return {
        "gmail-work": lambda: cot_cloud.fetch_gmail(
            s.work_email, "gmail-work", days["gmail-work"], load_store("gmail-work")
        ),
        "slack": lambda: cot_cloud.fetch_slack(days["slack"], s.look_later_emoji),
        "google-docs": lambda: cot_cloud.fetch_docs(
            s.docs_account, days["google-docs"]
        ),
        "notion": lambda: cot_cloud.fetch_notion(
            s.notion_account, s.notion_tasks_data_source, days["notion"]
        ),
        "gmail-personal": lambda: cot_cloud.fetch_gmail(
            s.personal_email,
            "gmail-personal",
            days["gmail-personal"],
            load_store("gmail-personal"),
        ),
        "imessage": lambda: cot_mac.fetch_imessage(
            s.mac_home, days["imessage"], contacts
        ),
        "whatsapp": lambda: cot_mac.fetch_whatsapp(
            s.mac_home, days["whatsapp"], contacts
        ),
    }


def _attempt(name: str, fn: Callable[[], Any]) -> tuple[Any, dict[str, Any]]:
    started = time.monotonic()
    at = iso(now_utc())
    try:
        value = fn()
    except Exception as exc:  # noqa: BLE001 -- one source must not sink the run
        log(f"  {name}: FAILED {type(exc).__name__}: {exc}")
        return None, {
            "ok": False,
            "at": at,
            "error": str(exc)[:300] or type(exc).__name__,
            "seconds": round(time.monotonic() - started, 1),
        }
    count = len(value) if hasattr(value, "__len__") else None
    log(f"  {name}: {count} in {time.monotonic() - started:.0f}s")
    return value, {
        "ok": True,
        "at": at,
        "records": count,
        "seconds": round(time.monotonic() - started, 1),
    }


def cmd_fetch(
    days: int,
    only: list[str] | None,
    settings: Settings | None = None,
    full: bool = False,
) -> dict[str, Any]:
    from collect_outstanding_tasks_skill import cot_mac

    settings = settings or load_settings()
    results: dict[str, dict[str, Any]] = {}
    wanted = []
    for s in only or list(SOURCES):
        reason = settings.off_reason(s)
        if reason is None:
            wanted.append(s)
            continue
        # Turned off: drop any tasks it gave before it was switched off.
        log(f"  {s}: {reason}")
        save_snapshot(s, [], days)
        results[s] = {"ok": False, "off": True, "at": iso(now_utc()), "error": reason}
    # Messages are read only since the last successful refresh (the first run,
    # or --full, reads the whole window); Docs and Notion are read in full.
    status = read_json(STATUS_PATH, {})
    span = {s: days if full else fetch_days(s, days, status) for s in SOURCES}
    log(
        "Fetching: "
        + (
            ", ".join(f"{SOURCE_LABELS[s]} ({span[s]}d)" for s in wanted)
            or "nothing (every source is turned off)"
        )
    )
    contacts: dict[str, str] = {}
    fetchers = _fetchers(span, contacts, settings)
    if any(s in MAC_SOURCES for s in wanted):
        home = settings.mac_home
        _, probe = _attempt("mac", lambda: cot_mac.check_reachable(home))
        if probe["ok"]:
            value, results["contacts"] = _attempt(
                "contacts", lambda: cot_mac.fetch_contacts(home)
            )
            contacts.update(value or {})
        else:
            # Mac offline: record every Mac source as failed with the probe's reason.
            for s in [s for s in wanted if s in MAC_SOURCES]:
                results[s] = dict(probe)
            wanted = [s for s in wanted if s not in MAC_SOURCES]
    # The two Mac sources share one file server; the rest are independent APIs.
    cloud = [s for s in wanted if s not in MAC_SOURCES]
    mac = [s for s in wanted if s in MAC_SOURCES]

    def run_mac() -> list[tuple[str, Any, dict[str, Any]]]:
        return [(s, *_attempt(s, fetchers[s])) for s in mac]

    with ThreadPoolExecutor(len(cloud) + 1) as pool:
        mac_future = pool.submit(run_mac)
        cloud_results = list(pool.map(lambda s: (s, *_attempt(s, fetchers[s])), cloud))
        outcomes = cloud_results + mac_future.result()
    for source, records, res in outcomes:
        if res["ok"]:
            save_snapshot(
                source,
                records,
                days,
                incremental=source in MESSAGE_SOURCES and span[source] < days,
            )
        results[source] = res
    return record_status(results)


def cmd_judge(
    model: str, workers: int, dry_run: bool, settings: Settings | None = None
) -> dict[str, Any]:
    from collect_outstanding_tasks_skill.cot_judge import (
        build_units,
        done_anchors,
        find_duplicates,
        fix_voice,
        judge_units,
        pack_batches,
    )

    units = build_units(load_all_snapshots())
    cache = read_json(JUDGEMENTS_PATH, {})
    if dry_run:
        todo = [u for u in units if cache.get(u.key, {}).get("hash") != u.hash]
        batches = pack_batches(todo)
        summary = {
            "units": len(units),
            "to_judge": len(todo),
            "batches": len(batches),
            "chars": sum(len(u.text) for u in todo),
            "by_source": {
                s: sum(1 for u in todo if u.source_key == s) for s in SOURCES
            },
        }
        print(json.dumps(summary, indent=1))
        return summary
    settings = settings or load_settings()
    dismissed = load_dismissed()
    completed = load_completed()
    new_cache, stats = judge_units(
        units,
        cache,
        model=model,
        workers=workers,
        log=log,
        dismissed=dismissed,
        settings=settings,
        completed=completed,
    )
    write_json(JUDGEMENTS_PATH, new_cache)
    # Then one call over the whole list to fold duplicates (weekly reminders,
    # the same ask by email and by text).
    unmerged = build_task_list(
        load_all_snapshots(), new_cache, {}, {}, DEFAULT_DAYS,
        look_later=read_json(LOOK_LATER_PATH, {}),
    )["tasks"]
    # Checked-off tasks join the duplicate check, so a reminder about one is folded into it.
    # A checked-off task is still in the verdicts; it goes in once, as its DONE entry.
    done = done_anchors(completed)
    done_ids = {d["id"] for d in done}
    merges, merge_cost = find_duplicates(
        [t for t in unmerged if t["id"] not in done_ids] + done,
        read_json(MERGES_PATH, {}),
        model=model,
    )
    write_json(MERGES_PATH, merges)
    stats.cost_usd += merge_cost
    if merges.get("error"):
        stats.errors.append("merging duplicates: " + merges["error"])
    log(
        f"Folded {sum(len(g) - 1 for g in merges['groups'])} duplicate tasks into {len(merges['groups'])}"
    )
    # Then reword any task that still talks about the user in the third person.
    # Look-later tasks quote the Slack message as written, so they are not reworded.
    voice, voice_cost, voice_error = fix_voice(
        [t for t in unmerged if not t["id"].startswith(LOOK_LATER_PREFIX)],
        read_json(VOICE_PATH, {}),
        model=model,
        name=settings.name,
    )
    write_json(VOICE_PATH, voice)
    stats.cost_usd += voice_cost
    if voice_error:
        stats.errors.append("rewording tasks: " + voice_error)
    status = {
        "at": iso(now_utc()),
        "model": model,
        "units": stats.units,
        "cached": stats.cached,
        "judged": stats.judged,
        "failed": stats.failed,
        "calls": stats.calls,
        "cost_usd": round(stats.cost_usd, 4),
        "seconds": stats.seconds,
        "errors": stats.errors[:5],
        "dismissals_seen": len(dismissed),
    }
    write_json(JUDGE_STATUS_PATH, status)
    log(
        f"Read {stats.judged} conversations in {stats.calls} calls, ${stats.cost_usd:.2f}, {stats.seconds:.0f}s; {stats.failed} failed"
    )
    return status


def cmd_build(days: int, out: Path) -> dict[str, Any]:
    result = build_task_list(
        load_all_snapshots(),
        read_json(JUDGEMENTS_PATH, {}),
        read_json(STATUS_PATH, {}),
        read_json(JUDGE_STATUS_PATH, {}),
        days,
        merges=read_json(MERGES_PATH, {}).get("groups", []),
        dismissed=load_dismissed(),
        completed=load_completed(),
        previous_open=read_json(OPEN_TASKS_PATH, {}).get("tasks", []),
        look_later=read_json(LOOK_LATER_PATH, {}),
    )
    from collect_outstanding_tasks_skill.cot_judge import apply_voice

    apply_voice(result["tasks"], read_json(VOICE_PATH, {}))
    write_json(out, result)
    write_json(OPEN_TASKS_PATH, {"at": result["generated"], "tasks": result["tasks"]})
    carried = sum(1 for t in result["tasks"] if t.get("carried"))
    if carried:
        log(f"Kept {carried} open tasks from conversations older than {days} days")
    tabs = {
        tab: sum(1 for t in result["tasks"] if t["tab"] == tab)
        for tab in ("work", "personal")
    }
    log(
        f"Wrote {len(result['tasks'])} tasks ({tabs['work']} work, {tabs['personal']} personal) to {out}"
    )
    for gap in result["gaps"]:
        log(f"  gap: {gap}")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(
        p: argparse.ArgumentParser,
        *,
        fetch: bool = False,
        judge: bool = False,
        build: bool = False,
    ) -> None:
        if fetch or build:
            p.add_argument(
                "--days",
                type=int,
                default=DEFAULT_DAYS,
                help="look-back window (default 30)",
            )
        if fetch:
            p.add_argument(
                "--full",
                action="store_true",
                help="re-read the whole window instead of only what is new since the last refresh",
            )
            p.add_argument(
                "--source",
                action="append",
                choices=list(SOURCES),
                help="only these sources (repeatable)",
            )
        if judge:
            from collect_outstanding_tasks_skill.cot_judge import DEFAULT_MODEL, WORKERS

            p.add_argument("--model", default=DEFAULT_MODEL)
            p.add_argument("--workers", type=int, default=WORKERS)
        if build:
            p.add_argument("--out", type=Path, default=TASKS_PATH)

    common(
        sub.add_parser("fetch", help="pull each source into the raw store"), fetch=True
    )
    p_judge = sub.add_parser(
        "judge", help="pick tasks out of changed conversations (metered Claude calls)"
    )
    common(p_judge, judge=True)
    p_judge.add_argument(
        "--dry-run", action="store_true", help="show what would be sent, make no calls"
    )
    common(sub.add_parser("build", help="write the task list"), build=True)
    common(
        sub.add_parser("all", help="fetch, judge, build"),
        fetch=True,
        judge=True,
        build=True,
    )
    args = parser.parse_args(argv)

    settings = None
    if args.cmd != "build":
        try:
            settings = load_settings()
        except SettingsError as exc:
            log(str(exc))
            return 2
    if args.cmd == "fetch":
        status = cmd_fetch(args.days, args.source, settings, args.full)
        return (
            0
            if any(status.get(s, {}).get("ok") for s in (args.source or SOURCES))
            else 1
        )
    if args.cmd == "judge":
        status = cmd_judge(args.model, args.workers, args.dry_run, settings)
        return 1 if status.get("failed") and not status.get("judged") else 0
    if args.cmd == "build":
        cmd_build(args.days, args.out)
        return 0
    status = cmd_fetch(args.days, args.source, settings, args.full)
    if not any(status.get(s, {}).get("ok") for s in (args.source or SOURCES)):
        log(
            "No source could be read (each failed or is turned off); keeping the previous task list."
        )
        return 1
    cmd_judge(args.model, args.workers, False, settings)
    cmd_build(args.days, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
