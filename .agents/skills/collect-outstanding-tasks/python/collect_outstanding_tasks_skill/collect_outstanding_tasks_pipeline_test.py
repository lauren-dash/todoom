"""The model step (with a fake Claude), the raw store, the task list the app
reads, and the fetch orchestration's failure handling."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from collect_outstanding_tasks_skill import cot_common, cot_judge, cot_store, run

NOW = dt.datetime(2026, 10, 22, 21, 0, tzinfo=dt.timezone.utc)
# Every source turned on, for made-up user Alex Rivera.
ALL_ON = cot_common.Settings(
    name="Alex Rivera",
    employer="Example Co",
    work_email="alex@work.example",
    personal_email="alex@example.com",
    slack=True,
    docs_account="alex@work.example",
    notion_account="alex@work.example:Example Co",
    notion_tasks_data_source="collection://00000000-0000-0000-0000-000000000000",
    mac_home="/Users/alex",
)


def text_rec(
    i: int,
    text: str,
    *,
    me: bool = False,
    chat: str = "Sam Rivera",
    unit: str = "imessage:a",
    group: bool = False,
) -> dict[str, Any]:
    return {
        "source_key": "imessage",
        "id": f"G{i}",
        "unit": unit,
        "date": f"2026-10-12T{8 + i:02d}:00:00Z",
        "from_me": me,
        "from_name": "me" if me else chat,
        "chat": chat,
        "is_group": group,
        "text": text,
        "url": "sms:+14155550142",
    }


def email_rec(
    i: str,
    *,
    me: bool = False,
    thread: str = "t1",
    body: str = "Can you send the deck?",
) -> dict[str, Any]:
    return {
        "source_key": "gmail-work",
        "id": i,
        "unit": f"gmail-work:{thread}",
        "date": "2026-10-20T10:00:00Z",
        "from": "Me <alex@work.example>" if me else "Casey Lin <casey@work.example>",
        "from_name": "Alex Rivera" if me else "Casey Lin",
        "from_me": me,
        "to": "Casey Lin <casey@work.example>" if me else "alex@work.example",
        "subject": "Deck",
        "labels": ["INBOX"],
        "snippet": body,
        "body": body,
        "full": True,
        "url": f"https://mail.google.com/mail/u/?authuser=x#all/{thread}",
    }


class FakeClaude:
    """Answers every unit with a scripted verdict and records each prompt."""

    def __init__(self, verdict_for: Any) -> None:
        self.verdict_for = verdict_for
        self.prompts: list[str] = []

    def __call__(self, prompt: str, system: str, model: str) -> tuple[str, float]:
        self.prompts.append(prompt)
        units = []
        for block in prompt.split("=== UNIT ")[1:]:
            alias, _, body = block.partition("\n")
            units.append(
                dict(self.verdict_for(alias.strip(), body), unit=alias.strip())
            )
        return json.dumps({"units": units}), 0.01


# ---------------------------------------------------------------- judge


def test_units_skip_notion_and_uninvolved_docs_and_drop_old_chat_lines() -> None:
    records = [text_rec(i, f"m{i}") for i in range(1, 4)]
    records += [
        {
            "source_key": "notion",
            "id": "n1",
            "unit": "notion:n1",
            "date": "2026-10-20T00:00:00Z",
            "from_me": True,
        }
    ]
    records += [
        {
            "source_key": "google-docs",
            "id": "c1",
            "unit": "google-docs:d:c1",
            "date": "2026-10-20T00:00:00Z",
            "from_me": False,
            "involves_me": False,
        }
    ]
    units = cot_judge.build_units(records)
    assert [u.key for u in units] == ["imessage:a#2026-W42"]
    assert units[0].aliases == {"1": "G1", "2": "G2", "3": "G3"}
    assert "[2] 2026-10-12 10:00 Sam Rivera: m2" in units[0].text


def test_chats_are_judged_a_week_at_a_time() -> None:
    sunday = dict(text_rec(1, "sun"), date="2026-10-11T20:00:00Z")
    monday = dict(text_rec(2, "mon"), date="2026-10-12T08:00:00Z")
    units = cot_judge.build_units([sunday, monday])
    assert [(u.key, list(u.aliases.values())) for u in units] == [
        ("imessage:a#2026-W41", ["G1"]),
        ("imessage:a#2026-W42", ["G2"]),
    ]
    assert cot_judge.base_unit(units[0].key) == "imessage:a"
    assert "week of 2026-W41" in units[0].text
    (email,) = cot_judge.build_units([email_rec("e1")])
    assert email.key == "gmail-work:t1", "email threads are not sliced"


def test_oversized_unit_keeps_its_newest_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cot_judge, "MAX_UNIT_CHARS", 200)
    (unit,) = cot_judge.build_units(
        [text_rec(i, f"message number {i}") for i in range(1, 10)]
    )
    assert len(unit.text) < 300 and "(earlier messages cut)" in unit.text
    assert "message number 9" in unit.text and "message number 1:" not in unit.text


def test_metadata_only_email_is_rendered_compactly() -> None:
    rec = dict(email_rec("e1"), full=False, snippet="Your order shipped", body="")
    (unit,) = cot_judge.build_units([rec])
    assert (
        unit.text.splitlines()[1]
        == "[1] 2026-10-20 10:00 from Casey Lin <casey@work.example>: Your order shipped"
    )


def test_batches_respect_char_and_unit_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cot_judge, "MAX_BATCH_BYTES", 1000)
    monkeypatch.setattr(cot_judge, "MAX_BATCH_UNITS", 3)
    units = [
        cot_judge.Unit(key=str(i), source_key="imessage", records=[], text="x" * 300)
        for i in range(7)
    ]
    assert [len(b) for b in cot_judge.pack_batches(units)] == [2, 2, 2, 1]
    wide = [
        cot_judge.Unit(key=str(i), source_key="imessage", records=[], text="é" * 300)
        for i in range(2)
    ]
    assert [len(b) for b in cot_judge.pack_batches(wide)] == [1, 1], (
        "the cap counts UTF-8 bytes"
    )


def test_parse_reply_tolerates_code_fence() -> None:
    assert cot_judge.parse_reply('```json\n{"units": []}\n```') == {"units": []}
    with pytest.raises(ValueError):
        cot_judge.parse_reply("I could not do that")


def test_apply_verdict_validates_record_link_due_and_skip() -> None:
    (unit,) = cot_judge.build_units(
        [
            text_rec(1, "sign up at https://signup.example/x by Friday"),
            text_rec(2, "ok", me=True),
        ]
    )
    entry = cot_judge.apply_verdict(
        unit,
        {
            "tasks": [
                {
                    "record": "1",
                    "title": "Sign up",
                    "why": "w",
                    "due": "2026-10-30",
                    "link": "https://signup.example/x",
                    "sender": None,
                },
                {
                    "record": "99",
                    "title": "Invented link",
                    "why": "",
                    "due": "Friday",
                    "link": "https://evil.example/",
                    "sender": "CVS",
                },
                {"record": "1", "title": "  ", "why": "dropped"},
            ]
        },
        None,
    )
    first, second = entry["tasks"]
    assert (first["record_id"], first["due"], first["link"]) == (
        "G1",
        "2026-10-30",
        "https://signup.example/x",
    )
    assert second["record_id"] == "G1", (
        "unknown record falls back to the newest message not from the user"
    )
    assert second["link"] is None, "a link not present in the messages is dropped"
    assert second["due"] is None and second["sender"] == "CVS"
    assert first["id"].startswith("text-") and first["id"] != second["id"]
    assert entry["skip"] is None
    empty = cot_judge.apply_verdict(
        unit, {"tasks": [], "skip": {"category": "bogus", "note": "chat"}}, None
    )
    assert empty["skip"] == {"category": "other", "note": "chat"}


def test_judge_caches_unchanged_units_and_keeps_ids_when_a_unit_changes() -> None:
    def verdict(alias: str, body: str) -> dict[str, Any]:
        prev = [line for line in body.splitlines() if line.startswith("PREVIOUS TASKS")]
        same = prev[0].split("[")[1].split("]")[0] if prev else None
        if "venmo" in body:
            return {
                "tasks": [
                    {
                        "record": "1",
                        "title": "Venmo Sam $100",
                        "why": "half",
                        "due": None,
                        "link": None,
                        "same_as": same,
                    }
                ]
            }
        return {"tasks": [], "skip": {"category": "chit-chat", "note": "hi"}}

    claude = FakeClaude(verdict)
    records = [
        text_rec(1, "please venmo me $100"),
        text_rec(2, "hi", unit="imessage:b", chat="Taylor"),
    ]
    cache1, stats1 = cot_judge.judge_units(
        cot_judge.build_units(records), {}, completer=claude, workers=1
    )
    assert (stats1.judged, stats1.cached, stats1.calls) == (2, 0, 1)
    task_id = cache1["imessage:a#2026-W42"]["tasks"][0]["id"]

    cache2, stats2 = cot_judge.judge_units(
        cot_judge.build_units(records), cache1, completer=claude, workers=1
    )
    assert (stats2.judged, stats2.cached, stats2.calls) == (0, 2, 0)
    assert cache2 == cache1

    records.append(text_rec(3, "also venmo for dinner?"))
    cache3, stats3 = cot_judge.judge_units(
        cot_judge.build_units(records), cache2, completer=claude, workers=1
    )
    assert (stats3.judged, stats3.cached) == (1, 1)
    assert f"PREVIOUS TASKS: [{task_id}] Venmo Sam $100" in claude.prompts[-1]
    assert cache3["imessage:a#2026-W42"]["tasks"][0]["id"] == task_id
    assert "chat: Taylor" not in claude.prompts[-1], "the unchanged chat is not re-sent"


def test_judge_failure_keeps_previous_verdict_as_stale() -> None:
    records = [text_rec(1, "please venmo me")]
    good = FakeClaude(
        lambda a, b: {"tasks": [{"record": "1", "title": "Venmo", "why": ""}]}
    )
    cache1, _ = cot_judge.judge_units(
        cot_judge.build_units(records), {}, completer=good, workers=1
    )

    def broken(prompt: str, system: str, model: str) -> tuple[str, float]:
        raise RuntimeError("claude -p exited 1")

    records.append(text_rec(2, "and the hotel"))
    cache2, stats = cot_judge.judge_units(
        cot_judge.build_units(records), cache1, completer=broken, workers=1
    )
    assert (
        stats.failed == 1
        and stats.calls == 1
        and "claude -p exited 1" in stats.errors[0]
    )
    assert cache2["imessage:a#2026-W42"]["stale"] is True
    assert (
        cache2["imessage:a#2026-W42"]["tasks"] == cache1["imessage:a#2026-W42"]["tasks"]
    )


def test_judge_unit_missing_from_reply_counts_as_failed() -> None:
    records = [text_rec(1, "a"), text_rec(2, "b", unit="imessage:b")]
    claude = FakeClaude(lambda a, b: {"tasks": []})

    def partial(prompt: str, system: str, model: str) -> tuple[str, float]:
        text, cost = claude(prompt, system, model)
        data = json.loads(text)
        data["units"] = data["units"][:1]
        return json.dumps(data), cost

    cache, stats = cot_judge.judge_units(
        cot_judge.build_units(records), {}, completer=partial, workers=1
    )
    assert stats.judged == 1 and stats.failed == 1
    assert list(cache) == ["imessage:a#2026-W42"]


# ---------------------------------------------------------------- store


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for mod in (cot_store, run):
        for name, rel in [
            ("STATUS_PATH", "fetch_status.json"),
            ("JUDGEMENTS_PATH", "judgements.json"),
            ("JUDGE_STATUS_PATH", "judge_status.json"),
            ("TASKS_PATH", "tasks.json"),
            ("MERGES_PATH", "merges.json"),
            ("VOICE_PATH", "voice.json"),
            ("OPEN_TASKS_PATH", "open_tasks.json"),
            ("LOOK_LATER_PATH", "look_later.json"),
        ]:
            monkeypatch.setattr(mod, name, tmp_path / rel)
    monkeypatch.setattr(cot_store, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(
        cot_store, "DISMISSED_PATH", tmp_path / "app" / "dismissed.json"
    )
    monkeypatch.setattr(
        cot_store, "COMPLETED_PATH", tmp_path / "app" / "completed.json"
    )
    return tmp_path


def test_snapshot_keeps_latest_and_evicts_old_records(data_dir: Path) -> None:
    old = dict(text_rec(1, "old"), date="2026-08-01T00:00:00Z")
    recent = text_rec(2, "recent")
    cot_store.save_snapshot("imessage", [old, recent], 30, now=NOW)
    assert [r["id"] for r in cot_store.load_snapshot("imessage")] == ["G1", "G2"], (
        "the latest fetch is kept whole"
    )
    newer = text_rec(3, "newer")
    cot_store.save_snapshot("imessage", [newer], 30, now=NOW)
    store = json.loads((data_dir / "raw" / "imessage.json").read_text())
    assert sorted(store["records"]) == ["G2", "G3"], (
        "out-of-snapshot records older than window+14d are evicted"
    )
    assert store["latest_ids"] == ["G3"]
    assert [r["id"] for r in cot_store.load_snapshot("imessage")] == ["G3"]


def test_record_status_remembers_last_success(data_dir: Path) -> None:
    cot_store.record_status(
        {"imessage": {"ok": True, "at": "2026-10-21T10:00:00Z", "records": 5}}
    )
    status = cot_store.record_status(
        {"imessage": {"ok": False, "at": "2026-10-22T10:00:00Z", "error": "Mac asleep"}}
    )
    assert status["imessage"] == {
        "ok": False,
        "attempted_at": "2026-10-22T10:00:00Z",
        "error": "Mac asleep",
        "seconds": None,
        "last_ok_at": "2026-10-21T10:00:00Z",
        "records": 5,
    }


# ---------------------------------------------------------------- the task list


def judged(unit: str, record_id: str, title: str, **extra: Any) -> dict[str, Any]:
    return {
        unit: {
            "hash": "h",
            "tasks": [
                dict(
                    {
                        "id": f"id-{record_id}",
                        "record_id": record_id,
                        "title": title,
                        "why": "because",
                        "due": None,
                        "link": None,
                    },
                    **extra,
                )
            ],
            "skip": None,
        }
    }


def test_task_list_shape_for_texts(data_dir: Path) -> None:
    records = [text_rec(i, f"m{i}", me=(i == 5)) for i in range(1, 11)]
    records.append(
        dict(
            text_rec(11, "Your Rx is ready", unit="imessage:sc", chat="22395"),
            from_name="22395",
        )
    )
    judgements = {
        **judged("imessage:a", "G8", "Venmo Sam"),
        **judged("imessage:sc", "G11", "Pick up prescription", sender="CVS Pharmacy"),
    }
    judgements.update(
        judged("imessage:a", "G8", "Venmo Sam", link="https://venmo.example/pay")
    )
    out = cot_store.build_task_list(records, judgements, {}, {}, 30, now=NOW)
    t = next(t for t in out["tasks"] if t["id"] == "id-G8")
    assert (
        t["tab"] == "personal" and t["source"] == "text" and t["from"] == "Sam Rivera"
    )
    assert t["url"] == "https://venmo.example/pay", (
        "a web page from the message is the link"
    )
    assert t["date"] == "2026-10-12T16:00:00Z"
    assert t["raw"]["id"] == "G8"
    assert [c["text"] for c in t["context"]] == [f"m{i}" for i in range(2, 11)]
    assert [c["this"] for c in t["context"]].index(True) == 6
    assert t["context"][3] == {
        "who": "You",
        "date": "2026-10-12T13:00:00Z",
        "text": "m5",
        "this": False,
    }
    sc = next(t for t in out["tasks"] if t["id"] == "id-G11")
    assert sc["url"] is None, "texts never link to sms:"
    assert sc["from"] == "CVS Pharmacy", "short-code sender named by the brand"
    assert out["generated"] == "2026-10-22T21:00:00Z"


def test_task_urls_by_source() -> None:
    assert cot_store.task_url("imessage", None, {"url": "sms:+1415"}) is None
    assert cot_store.task_url("whatsapp", None, {"url": "whatsapp://"}) is None
    assert (
        cot_store.task_url("whatsapp", None, {"url": "https://wa.me/1510"})
        == "https://wa.me/1510"
    )
    assert (
        cot_store.task_url(
            "whatsapp", "https://partiful.com/e/x", {"url": "https://wa.me/1510"}
        )
        == "https://partiful.com/e/x"
    )
    assert cot_store.task_url("imessage", "whatsapp://send", {"url": "sms:1"}) is None
    assert (
        cot_store.task_url("slack", None, {"url": "https://slack/x"})
        == "https://slack/x"
    )


def test_from_names_per_source() -> None:
    mine = email_rec("e2", me=True)
    assert (
        cot_store.display_from("gmail-work", email_rec("e1"), [email_rec("e1")])
        == "Casey Lin"
    )
    assert (
        cot_store.display_from("gmail-work", mine, [email_rec("e1"), mine])
        == "Casey Lin"
    )
    assert cot_store.display_from("gmail-work", mine, [mine]) == "Casey Lin", (
        "falls back to the recipient"
    )
    assert (
        cot_store.display_from("notion", {"from_me": True}, []) == "Notion to-do list"
    )
    doc = {
        "from_name": "Alex Rivera",
        "from_me": True,
        "replies": [
            {"author": "Casey Lin", "me": False},
            {"author": "Alex Rivera", "me": True},
        ],
    }
    assert cot_store.display_from("google-docs", doc, [doc]) == "Casey Lin"
    group = text_rec(1, "x", me=True, chat="Soccer Carpool", group=True)
    assert cot_store.display_from("imessage", group, [group]) == "Soccer Carpool"
    slack_mine = {
        "from_me": True,
        "channel": "marketing",
        "is_dm": False,
        "date": "2026-10-20T00:00:00Z",
    }
    assert cot_store.display_from("slack", slack_mine, [slack_mine]) == "#marketing"


def test_task_list_notion_email_skipped_and_order(data_dir: Path) -> None:
    notion = {
        "source_key": "notion",
        "id": "p1",
        "unit": "notion:p1",
        "date": "2026-10-20T00:00:00Z",
        "from_me": True,
        "title": "Renew the vendor contract",
        "notes": "",
        "day": "Wednesday",
        "url": "https://app.notion.com/p/p1",
    }
    e1, e2 = email_rec("e1"), email_rec("e2", thread="t2", body="newsletter")
    judgements = judged("gmail-work:t1", "e1", "Send Casey the deck", due="2026-10-25")
    judgements["gmail-work:t2"] = {
        "hash": "h",
        "tasks": [],
        "skip": {"category": "newsletter", "note": "marketing"},
    }
    out = cot_store.build_task_list([notion, e1, e2], judgements, {}, {}, 30, now=NOW)
    assert [t["title"] for t in out["tasks"]] == [
        "Send Casey the deck",
        "Renew the vendor contract",
    ], "dated tasks first"
    n = out["tasks"][1]
    assert (n["id"], n["from"], n["why"], n["url"], n["source"]) == (
        "notion-p1",
        "Notion to-do list",
        "Open on your Notion to-do list (Wednesday)",
        "https://app.notion.com/p/p1",
        "notion",
    )
    assert "context" not in n
    assert out["skipped"] == [
        {
            "what": "Email: Deck",
            "category": "newsletter",
            "reason": "marketing",
            "source": "email",
            "tab": "work",
            "url": e2["url"],
        }
    ]


def test_failed_source_is_reported_and_its_tasks_kept_stale(data_dir: Path) -> None:
    status = {
        "imessage": {
            "ok": False,
            "error": "Your Mac isn't reachable",
            "last_ok_at": "2026-10-22T09:00:00Z",
            "attempted_at": "2026-10-22T21:00:00Z",
        },
        "slack": {"ok": True, "last_ok_at": "2026-10-22T21:00:00Z", "records": 3},
        "contacts": {"ok": False, "error": "x"},
    }
    judgements = judged("imessage:a", "G1", "Venmo")
    judgements["imessage:a"]["stale"] = True
    out = cot_store.build_task_list(
        [text_rec(1, "venmo")], judgements, status, {"failed": 2}, 30, now=NOW
    )
    assert out["sources"]["imessage"]["status"] == "failed"
    assert out["sources"]["imessage"]["reason"] == "Your Mac isn't reachable"
    assert (
        out["sources"]["slack"]["status"] == "ok"
        and out["sources"]["slack"]["reason"] is None
    )
    assert out["sources"]["notion"]["status"] == "not-run"
    assert out["tasks"][0]["stale"] is True
    fresh = cot_store.build_task_list(
        [text_rec(1, "venmo")],
        judged("imessage:a", "G1", "Venmo"),
        status,
        {},
        30,
        now=NOW,
    )
    assert fresh["tasks"][0]["stale"] is True, (
        "a failed source's tasks are stale even with a fresh verdict"
    )
    assert (
        out["gaps"][0]
        == "Texts: couldn't read it this time: Your Mac isn't reachable. Showing its tasks as of 12 hours ago."
    )
    assert "2 conversations couldn't be read" in out["gaps"][-1]
    assert not any(g.startswith("Contacts") for g in out["gaps"]), (
        "contacts gap only when a Mac source worked"
    )


def test_whatsapp_staleness_gap() -> None:
    wa = {
        "source_key": "whatsapp",
        "id": "S1",
        "unit": "whatsapp:j",
        "date": "2026-10-18T00:00:00Z",
        "from_me": False,
    }
    out = cot_store.build_task_list([wa], {}, {}, {}, 30, now=NOW)
    assert out["gaps"] == [
        "WhatsApp: your Mac's copy ends 2026-10-18; opening WhatsApp Desktop brings it up to date."
    ]


# ---------------------------------------------------------------- run.py


def _slack_fetcher() -> list[dict[str, Any]]:
    return [
        {
            "source_key": "slack",
            "id": "C:1",
            "unit": "slack:C",
            "date": "2026-10-20T00:00:00Z",
            "from_me": False,
        }
    ]


def test_fetch_with_mac_offline_fails_mac_sources_fast(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from collect_outstanding_tasks_skill import cot_mac

    cot_store.save_snapshot("imessage", [text_rec(1, "earlier")], 30)
    called: list[str] = []

    def offline() -> None:
        raise cot_common.SourceError(cot_mac.MAC_OFFLINE)

    def never() -> list[dict[str, Any]]:
        called.append("mac fetcher")
        return []

    monkeypatch.setattr(cot_mac, "check_reachable", lambda home: offline())
    monkeypatch.setattr(cot_mac, "fetch_contacts", lambda home: never())
    monkeypatch.setattr(
        run,
        "_fetchers",
        lambda days, contacts, settings: {
            "slack": _slack_fetcher,
            "imessage": never,
            "whatsapp": never,
        },
    )
    status = run.cmd_fetch(30, ["slack", "imessage", "whatsapp"], ALL_ON)
    assert called == [], "nothing else is tried on the Mac once the probe fails"
    assert status["slack"]["ok"] is True and status["slack"]["records"] == 1
    for key in ("imessage", "whatsapp"):
        assert (
            status[key]["ok"] is False and status[key]["error"] == cot_mac.MAC_OFFLINE
        )
    assert [r["id"] for r in cot_store.load_snapshot("imessage")] == ["G1"], (
        "the failed source keeps its last snapshot"
    )
    assert [r["id"] for r in cot_store.load_snapshot("slack")] == ["C:1"]


def test_fetch_isolates_a_failing_source(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from collect_outstanding_tasks_skill import cot_mac

    def boom() -> list[dict[str, Any]]:
        raise cot_common.SourceError("db locked")

    seen_contacts: list[dict[str, str]] = []

    def whatsapp_fetch() -> list[dict[str, Any]]:
        return []

    def fetchers(
        days: int, contacts: dict[str, str], settings: cot_common.Settings
    ) -> dict[str, Any]:
        seen_contacts.append(contacts)
        return {"slack": _slack_fetcher, "imessage": boom, "whatsapp": whatsapp_fetch}

    monkeypatch.setattr(cot_mac, "check_reachable", lambda home: None)
    monkeypatch.setattr(
        cot_mac, "fetch_contacts", lambda home: {"4155550142": "Sam Rivera"}
    )
    monkeypatch.setattr(run, "_fetchers", fetchers)
    status = run.cmd_fetch(30, ["slack", "imessage", "whatsapp"], ALL_ON)
    assert status["imessage"] == {
        "ok": False,
        "attempted_at": status["imessage"]["attempted_at"],
        "error": "db locked",
        "seconds": status["imessage"]["seconds"],
        "last_ok_at": None,
        "records": None,
    }
    assert status["whatsapp"]["ok"] is True and status["contacts"]["ok"] is True
    assert seen_contacts[0] == {"4155550142": "Sam Rivera"}, (
        "Mac fetchers see the contacts loaded after the probe"
    )


def test_build_writes_the_app_file(data_dir: Path) -> None:
    cot_store.save_snapshot("imessage", [text_rec(1, "venmo me")], 30)
    (data_dir / "judgements.json").write_text(
        json.dumps(judged("imessage:a", "G1", "Venmo Sam"))
    )
    out = data_dir / "app" / "tasks.json"
    run.cmd_build(30, out)
    written = json.loads(out.read_text())
    assert [t["title"] for t in written["tasks"]] == ["Venmo Sam"]
    assert not (data_dir / "app" / "tasks.json.tmp").exists()


def test_tasks_path_defaults_to_the_app_data_dir() -> None:
    assert (
        str(cot_store.TASKS_PATH).endswith("data/.apps/task-inbox/tasks.json")
        or "COT_TASKS_PATH" in __import__("os").environ
    )


# ---------------------------------------------------------------- duplicates


def _task(i: str, date: str, title: str, due: str | None = None) -> dict[str, Any]:
    return {
        "id": i,
        "title": title,
        "why": "w",
        "date": date,
        "due": due,
        "source": "text",
        "from": "AT&T",
        "url": None,
        "stale": False,
        "merged_ids": [],
        "also_from": [],
    }


def test_merge_keeps_oldest_id_and_newest_wording() -> None:
    tasks = [
        _task("a", "2026-10-01T00:00:00Z", "Mail phone (old)", due="2026-10-30"),
        _task("b", "2026-10-16T00:00:00Z", "Mail phone (new)"),
        _task("c", "2026-10-10T00:00:00Z", "Other"),
    ]
    out = {t["id"]: t for t in cot_store.merge_tasks(tasks, [["b", "a"], ["c", "zzz"]])}
    assert set(out) == {"a", "c"}
    assert (
        out["a"]["title"] == "Mail phone (new)"
        and out["a"]["date"] == "2026-10-16T00:00:00Z"
    )
    assert out["a"]["merged_ids"] == ["b"] and out["a"]["due"] == "2026-10-30"
    assert [x["title"] for x in out["a"]["also_from"]] == ["Mail phone (old)"]
    assert out["c"]["merged_ids"] == [], "a group with one live member is ignored"


def test_find_duplicates_validates_and_caches() -> None:
    tasks = [
        _task("a", "2026-10-01", "x"),
        _task("b", "2026-10-02", "y"),
        _task("c", "2026-10-03", "z"),
    ]
    calls: list[str] = []

    def claude(prompt: str, system: str, model: str) -> tuple[str, float]:
        calls.append(prompt)
        return '{"groups": [["a", "b", "a"], ["b", "c"], ["c", "ghost"]]}', 0.02

    merges, cost = cot_judge.find_duplicates(tasks, {}, completer=claude)
    assert merges["groups"] == [["a", "b"]], (
        "duplicate, reused and unknown ids are dropped"
    )
    assert cost == 0.02 and "[a] (text, from AT&T, 2026-10-01) x -- w" in calls[0]
    again, cost2 = cot_judge.find_duplicates(tasks, merges, completer=claude)
    assert again is merges and cost2 == 0.0 and len(calls) == 1


def test_find_duplicates_failure_keeps_applicable_previous_groups() -> None:
    def broken(prompt: str, system: str, model: str) -> tuple[str, float]:
        raise RuntimeError("down")

    tasks = [_task("a", "2026-10-01", "x"), _task("b", "2026-10-02", "y")]
    merges, _ = cot_judge.find_duplicates(
        tasks,
        {"key": "old", "groups": [["a", "b", "gone"], ["gone", "b2"]]},
        completer=broken,
    )
    assert (
        merges["groups"] == [["a", "b"]]
        and "down" in merges["error"]
        and merges["key"] is None
    )


def test_skipped_lists_a_chat_once_and_not_when_it_gave_a_task(data_dir: Path) -> None:
    w41 = dict(text_rec(1, "hi"), date="2026-10-06T10:00:00Z")
    w42 = text_rec(2, "lol")
    other = text_rec(3, "please call", unit="imessage:b", chat="Mom")
    other_old = dict(
        text_rec(4, "hey", unit="imessage:b", chat="Mom"), date="2026-10-06T10:00:00Z"
    )
    skip = lambda note: {
        "hash": "h",
        "tasks": [],
        "skip": {"category": "chit-chat", "note": note},
    }  # noqa: E731
    judgements = {
        "imessage:a#2026-W41": skip("old week"),
        "imessage:a#2026-W42": skip("new week"),
        "imessage:b#2026-W41": skip("x"),
    }
    judgements.update({"imessage:b#2026-W42": judged("x", "G3", "Call Mom")["x"]})
    out = cot_store.build_task_list(
        [w41, w42, other, other_old], judgements, {}, {}, 30, now=NOW
    )
    assert [(s["what"], s["reason"], s["url"]) for s in out["skipped"]] == [
        ("Texts with Sam Rivera", "new week", None)
    ]
    assert [t["title"] for t in out["tasks"]] == ["Call Mom"]


def test_build_leaves_the_apps_own_files_alone(data_dir: Path) -> None:
    app_dir = data_dir / "app"
    app_dir.mkdir()
    owned = {
        "checked.json": '{"a": 1}',
        "edits.json": '{"b": 2}',
        "manual.json": "[3]",
        "dismissed.json": '{"zz": {}}',
    }
    for name, body in owned.items():
        (app_dir / name).write_text(body)
    cot_store.save_snapshot("imessage", [text_rec(1, "venmo me")], 30)
    run.cmd_build(30, app_dir / "tasks.json")
    assert sorted(p.name for p in app_dir.iterdir()) == [
        "checked.json",
        "dismissed.json",
        "edits.json",
        "manual.json",
        "tasks.json",
    ]
    assert {name: (app_dir / name).read_text() for name in owned} == owned


# ---------------------------------------------------------------- "Not a task"


def _dismissal(title: str, at: str, reason: str = "", **extra: Any) -> dict[str, Any]:
    return dict(
        {
            "at": at,
            "reason": reason,
            "tab": "personal",
            "source": "text",
            "source_key": "imessage",
            "title": title,
            "why": "w",
            "from": "Expensify",
            "excerpt": "Reply with a receipt",
            "merged_ids": [],
        },
        **extra,
    )


def test_feedback_puts_reasons_first_then_newest_and_caps_size() -> None:
    dismissed = {
        "a": _dismissal("Old bare", "2026-10-01T00:00:00Z"),
        "b": _dismissal("New bare", "2026-10-20T00:00:00Z"),
        "c": _dismissal(
            "Send Expensify receipt",
            "2026-10-02T00:00:00Z",
            reason="Expensify receipts are automatic, never a task",
        ),
        "d": _dismissal("Reasoned newer", "2026-10-21T00:00:00Z", reason="not mine"),
        "junk": "not a dict",
    }
    block = cot_judge.feedback_block(dismissed)
    titles = [line.split('"')[1] for line in block.splitlines()[1:]]
    assert titles == [
        "Reasoned newer",
        "Send Expensify receipt",
        "New bare",
        "Old bare",
    ]
    assert "their reason: Expensify receipts are automatic, never a task" in block
    assert '[message: "Reply with a receipt"]' in block
    small = cot_judge.feedback_block(
        dismissed, max_chars=len(block.splitlines()[0]) + 250
    )
    assert small.splitlines()[-1].startswith("- ... and ") and "Old bare" not in small
    assert cot_judge.feedback_block({}) == ""


def test_judge_sends_corrections_and_flags_dismissed_previous_tasks() -> None:
    seen: list[tuple[str, str]] = []

    def claude(prompt: str, system: str, model: str) -> tuple[str, float]:
        seen.append((prompt, system))
        return json.dumps(
            {
                "units": [
                    {
                        "unit": "U1",
                        "tasks": [
                            {
                                "record": "1",
                                "title": "Send receipt again",
                                "same_as": "text-old",
                            }
                        ],
                    }
                ]
            }
        ), 0.01

    (unit,) = cot_judge.build_units([text_rec(1, "receipt please")])
    cache = {
        unit.key: {
            "hash": "changed",
            "tasks": [{"id": "text-old", "title": "Send Expensify receipt"}],
            "skip": None,
        }
    }
    dismissed = {
        "text-old": _dismissal(
            "Send Expensify receipt", "2026-10-21T00:00:00Z", reason="automatic"
        )
    }
    new, _ = cot_judge.judge_units(
        [unit], cache, completer=claude, workers=1, dismissed=dismissed
    )
    prompt, system = seen[0]
    assert (
        "PREVIOUS TASKS: [text-old] Send Expensify receipt (DISMISSED by the user)"
        in prompt
    )
    assert (
        system.startswith(cot_judge.SYSTEM)
        and "THE USER'S CORRECTIONS" in system
        and "their reason: automatic" in system
    )
    assert new[unit.key]["tasks"][0]["id"] == "text-old", (
        "a re-emitted dismissed task keeps its id, so build drops it"
    )


def test_new_dismissal_does_not_force_a_re_read() -> None:
    claude = FakeClaude(lambda a, b: {"tasks": []})
    units = cot_judge.build_units([text_rec(1, "hi")])
    cache, _ = cot_judge.judge_units(units, {}, completer=claude, workers=1)
    _, stats = cot_judge.judge_units(
        units,
        cache,
        completer=claude,
        workers=1,
        dismissed={"x": _dismissal("t", "2026-10-21")},
    )
    assert (stats.cached, stats.calls) == (1, 0)


def test_build_never_re_emits_dismissed_tasks(data_dir: Path) -> None:
    records = [
        text_rec(1, "a"),
        text_rec(2, "b", unit="imessage:b"),
        text_rec(3, "c", unit="imessage:c"),
        text_rec(4, "d", unit="imessage:d"),
    ]
    judgements = {
        **judged("imessage:a", "G1", "Keep me"),
        **judged("imessage:b", "G2", "Dismissed directly"),
    }
    judgements.update(
        {
            **judged("imessage:c", "G3", "Folded into a dismissed one"),
            **judged("imessage:d", "G4", "Dismissed as a merged copy"),
        }
    )
    dismissed = {
        "id-G2": _dismissal("Dismissed directly", "2026-10-21"),
        "id-OLD": _dismissal("old merged task", "2026-10-21", merged_ids=["id-G4"]),
        "id-G3": _dismissal("c", "2026-10-21"),
    }
    out = cot_store.build_task_list(
        records, judgements, {}, {}, 30, now=NOW, dismissed=dismissed
    )
    assert [t["title"] for t in out["tasks"]] == ["Keep me"]
    assert out["dismissed_hidden"] == 3
    merged = cot_store.build_task_list(
        records,
        judgements,
        {},
        {},
        30,
        now=NOW,
        merges=[["id-G1", "id-G3"]],
        dismissed={"id-G3": _dismissal("c", "2026-10-21")},
    )
    assert sorted(t["id"] for t in merged["tasks"]) == ["id-G2", "id-G4"], (
        "a task with a dismissed copy folded in is hidden"
    )


def test_load_dismissed_tolerates_missing_and_bad_files(tmp_path: Path) -> None:
    assert cot_store.load_dismissed(tmp_path / "nope.json") == {}
    (tmp_path / "bad.json").write_text("{not json")
    assert cot_store.load_dismissed(tmp_path / "bad.json") == {}
    (tmp_path / "list.json").write_text("[1]")
    assert cot_store.load_dismissed(tmp_path / "list.json") == {}
    (tmp_path / "ok.json").write_text('{"a": {"title": "t"}}')
    assert cot_store.load_dismissed(tmp_path / "ok.json") == {"a": {"title": "t"}}


def test_fix_voice_rewords_only_third_person_tasks_and_caches() -> None:
    from collect_outstanding_tasks_skill.cot_judge import apply_voice, fix_voice

    tasks = [
        {
            "id": "a",
            "title": "Reply to Jordan",
            "why": "Jordan asked her to update the page",
        },
        {"id": "b", "title": "Pay the dentist bill", "why": "You owe $42.50"},
    ]
    seen: list[str] = []

    def completer(prompt: str, system: str, model: str) -> tuple[str, float]:
        seen.append(prompt)
        key = json.loads(prompt.splitlines()[0])["id"]
        return json.dumps(
            {
                "items": [
                    {
                        "id": key,
                        "title": "Reply to Jordan",
                        "why": "Jordan asked you to update the page",
                    }
                ]
            }
        ), 0.01

    cache, cost, error = fix_voice(tasks, {}, completer=completer)
    assert error is None and cost == 0.01
    assert len(seen) == 1 and "asked her" in seen[0] and "dentist bill" not in seen[0]
    # A second run with the same wording makes no call.
    cache2, cost2, _ = fix_voice(tasks, cache, completer=completer)
    assert cost2 == 0.0 and len(seen) == 1 and cache2 == cache
    apply_voice(tasks, cache)
    assert tasks[0]["why"] == "Jordan asked you to update the page"
    assert tasks[1]["why"] == "You owe $42.50"


def test_fix_voice_failure_keeps_original_wording() -> None:
    from collect_outstanding_tasks_skill.cot_judge import apply_voice, fix_voice

    tasks = [{"id": "a", "title": "Call Alex's dentist", "why": "she asked"}]

    def broken(prompt: str, system: str, model: str) -> tuple[str, float]:
        raise RuntimeError("boom")

    cache, cost, error = fix_voice(tasks, {}, completer=broken)
    assert cache == {} and cost == 0.0 and error is not None and "boom" in error
    apply_voice(tasks, cache)
    assert tasks[0]["why"] == "she asked"


# ---------------------------------------------------------------- settings


def test_missing_settings_file_names_the_file_and_the_example(tmp_path: Path) -> None:
    path = tmp_path / "settings.toml"
    with pytest.raises(cot_common.SettingsError) as exc:
        cot_common.load_settings(path)
    assert str(path) in str(exc.value)
    assert str(cot_common.EXAMPLE_SETTINGS_PATH) in str(exc.value)


def test_settings_reject_unknown_keys_and_wrong_types(tmp_path: Path) -> None:
    path = tmp_path / "settings.toml"
    path.write_text('[gmail]\nwork_emial = "a@b.example"\n')
    with pytest.raises(
        cot_common.SettingsError, match="unknown setting 'gmail.work_emial'"
    ):
        cot_common.load_settings(path)
    path.write_text('[slack]\nenabled = "yes"\n')
    with pytest.raises(
        cot_common.SettingsError, match="slack.enabled must be true or false"
    ):
        cot_common.load_settings(path)


def test_settings_file_round_trip_and_off_reasons(tmp_path: Path) -> None:
    path = tmp_path / "settings.toml"
    path.write_text(
        'name = "Alex Rivera"\n'
        '[gmail]\nwork_email = " alex@work.example "\npersonal_email = ""\n'
        "[slack]\nenabled = true\n"
        '[notion]\naccount = "alex@work.example:Example Co"\n'
        '[mac]\nhome = "/Users/alex/"\n'
    )
    s = cot_common.load_settings(path)
    assert (s.name, s.work_email, s.mac_home, s.slack) == (
        "Alex Rivera",
        "alex@work.example",
        "/Users/alex",
        True,
    )
    on = [k for k in cot_common.SOURCES if s.off_reason(k) is None]
    assert on == ["gmail-work", "slack", "imessage", "whatsapp"]
    assert s.off_reason("notion") == (
        f"turned off: set notion.tasks_data_source in {path} to read it"
    )
    assert "gmail.personal_email" in (s.off_reason("gmail-personal") or "")


def test_shipped_example_settings_parse_with_every_source_off() -> None:
    s = cot_common.load_settings(cot_common.EXAMPLE_SETTINGS_PATH)
    assert s.who == "the user"
    assert all(s.off_reason(k) for k in cot_common.SOURCES)


def test_turned_off_source_is_not_run_not_failed(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cot_store.save_snapshot("imessage", [text_rec(1, "earlier")], 30)
    monkeypatch.setattr(
        run, "_fetchers", lambda days, contacts, settings: {"slack": _slack_fetcher}
    )
    settings = cot_common.Settings(slack=True, path=data_dir / "settings.toml")
    status = run.cmd_fetch(30, ["slack", "imessage"], settings)
    assert status["slack"]["ok"] is True
    assert (
        status["imessage"]["off"] is True and "mac.home" in status["imessage"]["error"]
    )
    assert cot_store.load_snapshot("imessage") == [], (
        "a source turned off drops the tasks it gave before"
    )
    out = cot_store.build_task_list(
        cot_store.load_all_snapshots(), {}, status, {}, 30, now=NOW
    )
    assert out["sources"]["imessage"]["status"] == "not-run"
    assert "mac.home" in out["sources"]["imessage"]["reason"]
    assert out["gaps"] == [], "a source the user turned off is not a gap"


def test_judge_prompt_says_who_the_user_is() -> None:
    seen: list[str] = []

    def claude(prompt: str, system: str, model: str) -> tuple[str, float]:
        seen.append(system)
        return json.dumps({"units": [{"unit": "U1", "tasks": []}]}), 0.0

    cot_judge.judge_units(
        cot_judge.build_units([text_rec(1, "hi")]),
        {},
        completer=claude,
        workers=1,
        settings=ALL_ON,
    )
    assert seen[0].startswith(cot_judge.SYSTEM)
    assert (
        "ABOUT THE USER: The user is Alex Rivera (works at Example Co; work email "
        "alex@work.example; personal email alex@example.com)." in seen[0]
    )
    assert '"you" / "your"' in cot_judge.SYSTEM, "tasks always address the user as you"
    assert (
        "the user's name is not given"
        in cot_judge.about_user(cot_common.Settings()).lower()
    )


def test_voice_check_uses_the_users_name() -> None:
    pattern = cot_judge.third_person_re("Alex Rivera")
    assert pattern.search("Call Alex's dentist")
    assert pattern.search("Jordan asked her to sign")
    assert not pattern.search("Jordan asked you to sign")
    assert not cot_judge.third_person_re().search("Call Alex's dentist")


def test_fetch_days_reads_messages_since_last_refresh_and_state_sources_in_full() -> (
    None
):
    import datetime as dt

    from collect_outstanding_tasks_skill.cot_store import fetch_days

    now = dt.datetime(2026, 10, 26, 17, 0, tzinfo=dt.timezone.utc)
    status = {
        "gmail-work": {"last_ok_at": "2026-10-24T09:00:00Z"},
        "notion": {"last_ok_at": "2026-10-26T07:00:00Z"},
    }
    assert (
        fetch_days("gmail-work", 30, status, now) == 4
    )  # 2.3 days, rounded up, plus a day of overlap
    assert (
        fetch_days("notion", 30, status, now) == 30
    )  # state source: always the whole window
    assert fetch_days("slack", 30, status, now) == 30  # never fetched: the whole window
    assert (
        fetch_days(
            "gmail-work",
            30,
            {"gmail-work": {"last_ok_at": "2026-10-26T16:30:00Z"}},
            now,
        )
        == 2
    )
    assert (
        fetch_days(
            "gmail-work",
            30,
            {"gmail-work": {"last_ok_at": "2026-08-01T00:00:00Z"}},
            now,
        )
        == 30
    )


def test_incremental_snapshot_adds_new_records_and_drops_ones_past_the_window(
    tmp_path, monkeypatch
) -> None:
    import datetime as dt

    from collect_outstanding_tasks_skill import cot_store

    monkeypatch.setattr(cot_store, "RAW_DIR", tmp_path)
    now = dt.datetime(2026, 10, 26, tzinfo=dt.timezone.utc)
    old = {"id": "a", "date": "2026-09-20T00:00:00Z"}
    kept = {"id": "b", "date": "2026-10-10T00:00:00Z"}
    cot_store.save_snapshot("slack", [old, kept], 30, now - dt.timedelta(days=10))
    cot_store.save_snapshot(
        "slack",
        [{"id": "c", "date": "2026-10-25T00:00:00Z"}],
        30,
        now,
        incremental=True,
    )
    assert [r["id"] for r in cot_store.load_snapshot("slack")] == ["b", "c"]


def test_open_tasks_carry_forward_until_checked_off_or_removed() -> None:
    from collect_outstanding_tasks_skill.cot_store import carried_tasks

    aged = {
        "id": "t1",
        "source_key": "gmail-work",
        "merged_ids": [],
        "raw": {"unit": "gmail-work:thread-1"},
    }
    live = {
        "id": "t2",
        "source_key": "gmail-work",
        "merged_ids": [],
        "raw": {"unit": "gmail-work:thread-2"},
    }
    notion = {
        "id": "t3",
        "source_key": "notion",
        "merged_ids": [],
        "raw": {"unit": "notion:row"},
    }
    done = {
        "id": "t4",
        "source_key": "slack",
        "merged_ids": [],
        "raw": {"unit": "slack:old"},
    }
    records = [{"unit": "gmail-work:thread-2"}]
    out = carried_tasks([aged, live, notion, done], [], records, {"t4"})
    assert [t["id"] for t in out] == ["t1"] and out[0]["carried"] is True
    # Already listed (directly or folded into another task): not carried twice.
    assert carried_tasks([aged], [{"id": "x", "merged_ids": ["t1"]}], [], set()) == []


def test_checked_off_tasks_are_marked_done_for_the_model_and_fold_new_reminders() -> (
    None
):
    import datetime as dt

    from collect_outstanding_tasks_skill.cot_judge import (
        Unit,
        build_prompt,
        done_anchors,
        done_block,
        done_ids,
    )
    from collect_outstanding_tasks_skill.cot_store import merge_tasks

    completed = {
        "old1": {
            "at": "2026-10-20T10:00:00Z",
            "title": "Mail the trade-in phone",
            "source": "text",
            "merged_ids": ["old0"],
        }
    }
    assert done_ids(completed) == {"old1", "old0"}
    assert "Mail the trade-in phone" in done_block(completed)
    assert done_anchors(completed)[0]["title"].startswith("DONE: ")

    prompt = build_prompt(
        [Unit(key="u", source_key="text", records=[], text="messages")],
        {"u": {"tasks": [{"id": "old1", "title": "Mail the phone"}]}},
        dt.date(2026, 10, 26),
        done={"old1"},
    )
    assert "(DONE: the user checked it off)" in prompt
    assert (
        merge_tasks(
            [{"id": "new", "date": "2026-10-25", "merged_ids": []}], [["new", "old1"]]
        )[0]["id"]
        == "new"
    )


def test_turning_a_source_off_forgets_its_last_fetch(data_dir: Path) -> None:
    cot_store.record_status({"slack": {"ok": True, "at": "2026-10-24T09:00:00Z"}})
    status = cot_store.record_status(
        {
            "slack": {
                "ok": False,
                "off": True,
                "at": "2026-10-25T09:00:00Z",
                "error": "off",
            }
        }
    )
    assert status["slack"]["last_ok_at"] is None
    assert cot_store.fetch_days("slack", 30, status) == 30, (
        "turned back on, it reads the whole window again"
    )


def test_judge_marks_checked_off_tasks_done_without_naming_the_publisher() -> None:
    seen: list[str] = []

    def claude(prompt: str, system: str, model: str) -> tuple[str, float]:
        seen.append(system)
        return json.dumps({"units": [{"unit": "U1", "tasks": []}]}), 0.0

    completed = {
        "old1": {
            "at": "2026-10-20T10:00:00Z",
            "title": "Mail the trade-in phone",
            "source": "text",
        }
    }
    cot_judge.judge_units(
        cot_judge.build_units([text_rec(1, "hi")]),
        {},
        completer=claude,
        workers=1,
        settings=ALL_ON,
        completed=completed,
    )
    assert "ABOUT THE USER: The user is Alex Rivera" in seen[0]
    assert "ALREADY DONE: the user checked these off." in seen[0]
    assert '"Mail the trade-in phone"' in seen[0]


def test_find_duplicates_checks_only_new_items_against_the_list() -> None:
    old = [_task("a", "2026-10-01", "Mail phone"), _task("b", "2026-10-02", "Pay bill")]
    first, _ = cot_judge.find_duplicates(
        old, {}, completer=lambda p, s, m: ('{"groups": []}', 0.01)
    )
    prompts: list[str] = []

    def claude(prompt: str, system: str, model: str) -> tuple[str, float]:
        prompts.append(prompt)
        # "a" and "b" are both old: a group without a new id is ignored.
        return '{"groups": [["c", "a"], ["a", "b"]]}', 0.02

    tasks = old + [_task("c", "2026-10-03", "Send the phone back")]
    merges, cost = cot_judge.find_duplicates(tasks, first, completer=claude)
    assert merges["groups"] == [["c", "a"]] and cost == 0.02
    assert system_is_new(prompts[0]), "the new item is asked about on its own"
    again, cost2 = cot_judge.find_duplicates(tasks, merges, completer=claude)
    assert again["groups"] == [["c", "a"]] and cost2 == 0.0 and len(prompts) == 1


def system_is_new(prompt: str) -> bool:
    listed, new = prompt.split("\n\nNEW:\n")
    return "[c]" in new and "[c]" not in listed and "[a]" in listed


def test_find_duplicates_failure_leaves_new_items_to_check_next_run() -> None:
    old = _task("a", "2026-10-01", "Send Expensify receipt for coffee")
    new = _task("b", "2026-10-05", "Send Expensify receipt for coffee")
    seen = {"key": None, "groups": [], "seen": {"a": cot_judge.item_fingerprint(old)}}

    def broken(prompt: str, system: str, model: str) -> tuple[str, float]:
        raise RuntimeError("down")

    merges, _ = cot_judge.find_duplicates([old, new], seen, completer=broken)
    assert merges["groups"] == [], "same title alone never folds (two receipts, two charges)"
    assert merges["seen"] == seen["seen"] and "down" in merges["error"]


def _marked(i: str, **extra: Any) -> dict[str, Any]:
    return {
        "source_key": "slack", "id": i, "unit": "slack:C1", "ts": "1", "date": "2026-10-01T00:00:00Z",
        "from_name": "Riley", "from_me": False, "channel": "launch", "is_dm": False,
        "text": "see <https://x.com/a|this> &amp; that", "url": f"https://slack/{i}", **extra,
    }


def test_look_later_marks_made_before_tracking_are_left_out(tmp_path: Path) -> None:
    from collect_outstanding_tasks_skill import cot_cloud

    path = tmp_path / "look_later.json"
    first = dt.datetime(2026, 10, 27, 5, 0, tzinfo=dt.timezone.utc)
    cot_cloud.record_look_later([_marked("old")], path, now=first)
    later = dt.datetime(2026, 10, 28, 18, 0, tzinfo=dt.timezone.utc)
    state = cot_cloud.record_look_later([_marked("old"), _marked("new")], path, now=later)
    assert state["baseline"] == ["old"], "only the very first run sets the baseline"
    assert state["first_seen"]["old"] == "2026-10-27T05:00:00Z"
    tasks = cot_store.look_later_tasks(state)
    assert [r["id"] for _t, r in tasks] == ["new"]
    task = tasks[0][0]
    assert task["due"] is None, "a mark is not a deadline"
    assert task["title"] == "Review: see this & that", "titled by what the message says"
    assert task["why"] == 'You marked it :look-later: in Slack: "see this & that"'
    state["include"] = ["old"]
    assert {r["id"] for _t, r in cot_store.look_later_tasks(state)} == {"old", "new"}
    gone = cot_cloud.record_look_later([_marked("new")], path, now=later)
    assert [r["id"] for _t, r in cot_store.look_later_tasks(gone)] == ["new"], (
        "removing the mark takes the task off"
    )


def test_look_later_tasks_land_on_the_work_tab_and_are_not_carried() -> None:
    state = {"baseline": [], "first_seen": {}, "records": [_marked("m", is_dm=True, from_name="Priya")]}
    out = cot_store.build_task_list([], {}, {}, {}, 30, look_later=state)["tasks"]
    assert [(t["tab"], t["title"], t["due"]) for t in out] == [("work", "Review: see this & that", None)]
    again = cot_store.build_task_list([], {}, {}, {}, 30, look_later={}, previous_open=out)
    assert again["tasks"] == [], "an unmarked message is not carried forward"


def test_a_look_later_mark_on_a_found_task_keeps_its_wording_and_adds_the_due_date() -> None:
    found = _task("slack-1", "2026-10-07T00:00:00Z", "Tell Riley to swap the team photo")
    mark = dict(_task("slack-later-1", "2026-10-07T00:00:00Z", "Look at Priya's Slack DM"), due="2026-10-27")
    (out,) = cot_store.merge_tasks([found, mark], [["slack-1", "slack-later-1"]])
    assert out["title"] == "Tell Riley to swap the team photo" and out["due"] == "2026-10-27"
    assert out["merged_ids"] == ["slack-later-1"]


def test_a_copy_torn_by_a_change_mid_download_is_downloaded_again(tmp_path, monkeypatch) -> None:
    import sqlite3

    from collect_outstanding_tasks_skill import cot_mac

    good = tmp_path / "good.db"
    conn = sqlite3.connect(good)
    conn.execute("create table t (x)")
    conn.commit()
    conn.close()
    etags = iter(["v1", "v2", "v2", "v2"])  # before, after 1st copy (changed), after 2nd copy
    copies: list[int] = []

    def download(path: str, dest, timeout: float) -> None:
        copies.append(1)
        dest.write_bytes(good.read_bytes())

    monkeypatch.setattr(cot_mac, "_etag", lambda path: next(etags))
    monkeypatch.setattr(cot_mac, "_download", download)
    monkeypatch.setattr(cot_mac, "_head", lambda path: (_ for _ in ()).throw(cot_mac.SourceError("no wal")))
    db = cot_mac.sync_sqlite("/Mac/chat.db", tmp_path / "mirror")
    assert len(copies) == 2 and json.loads((tmp_path / "mirror" / "etags.json").read_text()) == {"chat.db": "v2"}
    assert db.exists()


def test_a_damaged_saved_copy_is_fetched_afresh_once(tmp_path, monkeypatch) -> None:
    import sqlite3

    from collect_outstanding_tasks_skill import cot_mac

    calls: list[bool] = []

    def sync(mac_path, local_dir, *, timeout, force=False):
        calls.append(force)
        return tmp_path / "x.db"

    reads = iter([sqlite3.DatabaseError("database disk image is malformed"), ["ok"]])

    def read(conn):
        r = next(reads)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(cot_mac, "sync_sqlite", sync)
    monkeypatch.setattr(cot_mac, "open_db", lambda path: sqlite3.connect(":memory:"))
    assert cot_mac._read_mirrored("/Mac/chat.db", tmp_path, 10, read) == ["ok"]
    assert calls == [False, True]


def test_look_later_title_drops_links_and_falls_back_when_only_a_link() -> None:
    assert cot_store.look_later_title("Should add to our website https://x.com/a/status/1?s=46") == "Review: Should add to our website"
    assert cot_store.look_later_title("one two three four five six seven eight nine") == "Review: one two three four five six seven eight..."
    assert cot_store.look_later_title("https://x.com/a x.com/b") is None


def test_sender_names_read_like_people() -> None:
    assert cot_store.tidy_name("acme_ab12cd34_agent@rbm.goog") == "Acme"
    assert cot_store.tidy_name("alex rivera") == "Alex Rivera"
    assert cot_store.tidy_name("Sam Lee") == "Sam Lee"
    assert cot_store.tidy_name("orders@example-print.com") == "orders@example-print.com"
    assert cot_store.tidy_name("#chat-alex") == "#chat-alex"
    assert cot_store.tidy_name(None) is None


def test_look_later_emoji_is_a_setting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from collect_outstanding_tasks_skill import cot_cloud

    path = tmp_path / "settings.toml"
    path.write_text('[slack]\nenabled = true\nlook_later_emoji = ":eyes:"\n')
    assert cot_common.load_settings(path).look_later_emoji == "eyes"
    path.write_text("[slack]\nenabled = true\n")
    assert cot_common.load_settings(path).look_later_emoji == "look-later", "the default"

    queries: list[str] = []

    def fake_slack(method: str, **params: Any) -> dict[str, Any]:
        if method == "auth.test":
            return {"ok": True, "user_id": "U0ALEX"}
        queries.append(params.get("query", ""))
        return {"ok": True, "messages": {"matches": [], "paging": {"pages": 1}}}

    monkeypatch.setattr(cot_cloud, "_slack", fake_slack)
    monkeypatch.setattr(cot_cloud, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cot_cloud, "LOOK_LATER_PATH", tmp_path / "look_later.json")
    cot_cloud.fetch_slack(3, "eyes")
    assert "hasmy::eyes:" in queries
    state = json.loads((tmp_path / "look_later.json").read_text())
    assert state["emoji"] == "eyes"
    state["records"] = [_marked("m")]
    state["baseline"] = []
    ((task, _rec),) = cot_store.look_later_tasks(state)
    assert task["why"].startswith("You marked it :eyes: in Slack")

    queries.clear()
    cot_cloud.fetch_slack(3, "")
    assert not any(q.startswith("hasmy::") for q in queries), "blank turns it off"
    assert json.loads((tmp_path / "look_later.json").read_text())["records"] == []
