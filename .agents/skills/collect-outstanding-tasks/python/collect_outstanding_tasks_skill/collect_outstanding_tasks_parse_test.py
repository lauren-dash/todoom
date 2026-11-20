"""Fixture tests for every parser of external data: Gmail, Slack, Docs, Notion,
and the Mac's Messages / WhatsApp / Contacts databases (built here in SQLite)."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"

from collect_outstanding_tasks_skill import cot_cloud, cot_common, cot_mac


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


# ---------------------------------------------------------------- Gmail


def test_gmail_full_message_parses_body_html_and_attachments() -> None:
    rec = cot_cloud.parse_gmail_message(
        load("gmail_message_full.json"), "alex@example.com", "gmail-personal"
    )
    assert rec["id"] == "18f0a1"
    assert rec["unit"] == "gmail-personal:18f0a0"
    assert rec["date"] == "2026-10-22T08:00:00Z"
    assert rec["from_name"] == "Oak Hill Elementary"
    assert rec["from_me"] is False
    assert rec["subject"] == "*ACTION NEEDED* class photo"
    assert rec["snippet"] == "Hi Alex, please send a photo by Friday & sign up"
    assert rec["body"].startswith("Hi Alex,\nPlease email a photo by Friday.")
    assert "https://signup.example/conf" in rec["body"]
    assert rec["body_html"] == "<p>Hi Alex,</p><p>Please email a photo by Friday.</p>"
    assert rec["attachments"] == ["flyer.pdf"]
    assert rec["list_unsubscribe"] == "<mailto:unsub@oakhill.example>"
    assert (
        rec["url"]
        == "https://mail.google.com/mail/u/?authuser=alex%40example.com#all/18f0a0"
    )


def test_gmail_metadata_message_has_no_body() -> None:
    rec = cot_cloud.parse_gmail_message(
        load("gmail_message_metadata.json"), "alex@example.com", "gmail-personal"
    )
    assert rec["body"] == ""
    assert rec["body_html"] is None
    assert rec["snippet"] == "Your order has shipped"
    assert rec["labels"] == ["CATEGORY_UPDATES", "INBOX"]


def test_gmail_sent_message_is_from_me() -> None:
    msg = load("gmail_message_metadata.json")
    msg["labelIds"] = ["SENT"]
    assert (
        cot_cloud.parse_gmail_message(msg, "alex@example.com", "gmail-personal")[
            "from_me"
        ]
        is True
    )


def test_html_to_text_strips_markup_and_scripts() -> None:
    assert (
        cot_cloud.html_to_text(
            "<style>x{}</style><p>Hello&nbsp;<b>you</b></p><p>Bye</p>"
        )
        == "Hello you\nBye"
    )


@pytest.mark.parametrize(
    ("labels", "work", "expected"),
    [
        ({"CATEGORY_UPDATES"}, False, False),
        ({"CATEGORY_UPDATES", "IMPORTANT"}, False, True),
        ({"CATEGORY_PERSONAL"}, False, True),
        ({"SENT"}, False, True),
        ({"CATEGORY_UPDATES"}, True, True),
    ],
)
def test_is_human_thread(labels: set[str], work: bool, expected: bool) -> None:
    assert cot_cloud.is_human_thread(labels, work) is expected


def test_fetch_gmail_reuses_known_messages_and_reads_human_threads_in_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    full = load("gmail_message_full.json")
    meta_noise = load("gmail_message_metadata.json")
    meta_human = dict(full, payload={"headers": full["payload"]["headers"]})
    calls: list[str] = []

    def fake_get(url: str, **_kw):
        calls.append(url)
        if "/messages?" in url:
            if "in%3Asent" in url:
                return {"messages": []}
            return {
                "messages": [
                    {"id": "18f0a1", "threadId": "18f0a0"},
                    {"id": "18f0b1", "threadId": "18f0b1"},
                    {"id": "old1", "threadId": "t-old"},
                ]
            }
        if url.endswith("format=full"):
            return full
        return meta_human if "/18f0a1?" in url else meta_noise

    monkeypatch.setattr(cot_cloud, "get_json", fake_get)
    monkeypatch.setattr(
        cot_cloud, "cutoff", lambda days: cot_common.parse_iso("2026-10-01T00:00:00Z")
    )
    known = {
        "old1": {
            "id": "old1",
            "thread_id": "t-old",
            "labels": ["IMPORTANT"],
            "full": True,
            "date": "2026-10-10T00:00:00Z",
            "source_key": "gmail-personal",
        }
    }
    recs = cot_cloud.fetch_gmail(
        "alex@example.com", "gmail-personal", 30, known, per_minute=1e9
    )
    by_id = {r["id"]: r for r in recs}
    assert set(by_id) == {"18f0a1", "18f0b1", "old1"}
    assert by_id["18f0a1"]["full"] is True and "photo" in by_id["18f0a1"]["body"]
    assert by_id["18f0b1"]["full"] is False
    assert by_id["old1"] is known["old1"] and by_id["old1"]["in_inbox"] is True
    assert not any("/old1" in c for c in calls), (
        "a stored message must not be re-downloaded"
    )
    assert sum(c.endswith("format=full") for c in calls) == 1


# ---------------------------------------------------------------- Slack


def test_slack_match_parses_dm_and_thread() -> None:
    rec = cot_cloud.parse_slack_match(
        load("slack_search_match.json"), "U0ALEX", "mention"
    )
    assert rec["id"] == "D123:1792700000.000100"
    assert rec["unit"] == "slack:D123"
    assert rec["is_dm"] is True
    assert rec["from_me"] is False
    assert rec["thread_ts"] == "1792690000.000001"
    assert rec["matched"] == ["mention"]
    assert rec["date"] == "2026-10-22T20:13:20Z"
    assert rec["url"].startswith("https://example-team.slack.com/archives/D123/")


def test_slack_mentions_resolve_to_real_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cot_cloud, "DATA_DIR", tmp_path)
    match = load("slack_search_match.json")
    users = {"U0MORGAN": "Morgan Lee", "U0ALEX": "Alex Rivera"}

    def fake_slack(method: str, **params):
        if method == "auth.test":
            return {"ok": True, "user_id": "U0ALEX"}
        if method == "users.info":
            return {"ok": True, "user": {"real_name": users[params["user"]]}}
        return {
            "ok": True,
            "messages": {
                "matches": [match] if params["query"].startswith("<@") else [],
                "paging": {"pages": 1},
            },
        }

    monkeypatch.setattr(cot_cloud, "_slack", fake_slack)
    monkeypatch.setattr(
        cot_cloud, "cutoff", lambda days: cot_common.parse_iso("2026-10-01T00:00:00Z")
    )
    (rec,) = cot_cloud.fetch_slack(30)
    assert rec["from_name"] == "Morgan Lee"
    assert (
        rec["text_display"]
        == "@Alex Rivera which layout do you like? centered or bottom logo"
    )
    assert json.loads((tmp_path / "slack_users.json").read_text()) == users


# ---------------------------------------------------------------- Google Docs


def test_docs_comment_parses_thread_and_involvement() -> None:
    fx = load("docs_comment.json")
    rec = cot_cloud.parse_doc_comment(fx["file"], fx["comment"], "alex@work.example")
    assert rec["id"] == "AAAComment1"
    assert rec["involves_me"] is True
    assert rec["from_me"] is True
    assert rec["last_from_me"] is False
    assert [r["content"] for r in rec["replies"]] == ["Yes, May 4 is locked"]
    assert rec["quoted"] == "launch moves to May 4"
    assert (
        rec["url"] == "https://docs.google.com/document/d/DOC1/edit?disco=AAAComment1"
    )


def test_docs_comment_not_involving_me() -> None:
    fx = load("docs_comment.json")
    c = dict(
        fx["comment"],
        content="typo here",
        author={"displayName": "Casey", "me": False},
        replies=[],
    )
    assert (
        cot_cloud.parse_doc_comment(fx["file"], c, "alex@work.example")["involves_me"]
        is False
    )


# ---------------------------------------------------------------- Notion


def test_notion_mcp_response_and_row_selection() -> None:
    body = cot_cloud._mcp_body((FIXTURES / "notion_mcp_response.txt").read_bytes())
    data = json.loads(body["result"]["content"][0]["text"])
    rows = cot_cloud.open_notion_rows(data, "2026-09-22T00:00:00Z")
    assert [(r["id"], r["title"]) for r in rows] == [
        ("1111aaaa2222bbbb3333cccc4444dddd", "Renew the vendor contract")
    ]
    assert rows[0]["url"] == "https://app.notion.com/p/1111aaaa2222bbbb3333cccc4444dddd"
    assert rows[0]["day"] == "Wednesday"


# ---------------------------------------------------------------- HTTP retry


def test_get_json_retries_google_quota_403(monkeypatch: pytest.MonkeyPatch) -> None:
    replies = [
        b'{"error": {"message": "Quota exceeded for quota metric"}}\n403',
        b'{"ok": 1}\n200',
    ]

    def fake_curl(*_a, **_kw):
        return subprocess.CompletedProcess([], 0, replies.pop(0), b"")

    monkeypatch.setattr(cot_common, "latchkey_curl", fake_curl)
    monkeypatch.setattr(cot_common.time, "sleep", lambda _s: None)
    assert cot_common.get_json("https://x") == {"ok": 1}


def test_get_json_raises_on_plain_403(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cot_common,
        "latchkey_curl",
        lambda *_a, **_kw: subprocess.CompletedProcess(
            [], 0, b'{"error": "forbidden"}\n403', b""
        ),
    )
    with pytest.raises(cot_common.SourceError, match="HTTP 403"):
        cot_common.get_json("https://x")


# ---------------------------------------------------------------- Mac databases

APPLE = cot_mac.APPLE_EPOCH


def _apple_ns(iso_ts: str) -> int:
    return int((cot_common.parse_iso(iso_ts).timestamp() - APPLE) * 1e9)


def _attributed(text: str) -> bytes:
    raw = text.encode()
    return (
        b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+"
        + bytes([len(raw)])
        + raw
        + b"\x86"
    )


@pytest.fixture
def contacts_db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "ab.abcddb")
    conn.executescript(
        """
        create table ZABCDRECORD (Z_PK integer primary key, ZFIRSTNAME text, ZLASTNAME text, ZORGANIZATION text);
        create table ZABCDPHONENUMBER (Z_PK integer primary key, ZOWNER integer, ZFULLNUMBER text);
        create table ZABCDEMAILADDRESS (Z_PK integer primary key, ZOWNER integer, ZADDRESS text);
        insert into ZABCDRECORD values (1, 'Sam', 'Rivera', null), (2, null, null, 'Maple Street Vet'), (3, 'Priya', 'K', null);
        insert into ZABCDPHONENUMBER values (1, 1, '(415) 555-0142'), (2, 2, '+1 510-555-0187'), (3, 3, '510 555 0101');
        insert into ZABCDEMAILADDRESS values (1, 1, 'Sam@Example.com');
        """
    )
    return conn


def test_contacts_map_phone_last10_and_email(contacts_db: sqlite3.Connection) -> None:
    contacts = cot_mac.load_contacts_db(contacts_db)
    assert contacts["4155550142"] == "Sam Rivera"
    assert contacts["5105550187"] == "Maple Street Vet"
    assert contacts["sam@example.com"] == "Sam Rivera"
    assert cot_mac.contact_name("+14155550142", contacts) == "Sam Rivera"
    assert cot_mac.contact_name("14155550142@s.whatsapp.net", contacts) == "Sam Rivera"
    assert cot_mac.contact_name("SAM@example.com", contacts) == "Sam Rivera"
    assert cot_mac.contact_name("+19999999999", contacts) is None
    assert cot_mac.pretty_phone("+14155550142") == "415-555-0142"
    assert cot_mac.pretty_phone("22395") == "22395"


def test_decode_attributed_body() -> None:
    assert (
        cot_mac.decode_attributed_body(_attributed("Would need you to send the code"))
        == "Would need you to send the code"
    )
    assert cot_mac.decode_attributed_body(None) is None
    assert cot_mac.decode_attributed_body(b"no marker") is None


def test_read_imessage(
    tmp_path: Path, contacts_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cot_mac, "cutoff", lambda days: cot_common.parse_iso("2026-10-01T00:00:00Z")
    )
    contacts = cot_mac.load_contacts_db(contacts_db)
    conn = sqlite3.connect(tmp_path / "chat.db")
    conn.executescript(
        """
        create table handle (ROWID integer primary key, id text);
        create table chat (ROWID integer primary key, guid text, chat_identifier text, display_name text, style integer);
        create table chat_message_join (chat_id integer, message_id integer);
        create table chat_handle_join (chat_id integer, handle_id integer);
        create table message (ROWID integer primary key, guid text, date integer, is_from_me integer, text text,
                              attributedBody blob, handle_id integer, service text, associated_message_type integer);
        insert into handle values (1, '+14155550142'), (2, '22395'), (3, '+15105550101'), (4, '+16505550199');
        insert into chat values (10, 'any;-;+14155550142', '+14155550142', '', 45),
                                (11, 'any;-;22395', '22395', '', 45),
                                (12, 'any;+;chat99', 'chat99', '', 43);
        insert into chat_handle_join values (10, 1), (11, 2), (12, 1), (12, 3), (12, 4);
        """
    )
    rows = [
        (
            1,
            "G1",
            "2026-10-18T15:17:10Z",
            0,
            None,
            _attributed("Would need you to send the gate code"),
            1,
            10,
            0,
        ),
        (2, "G2", "2026-10-18T15:18:00Z", 1, "ok will do", None, 0, 10, 0),
        (3, "G3", "2026-10-18T15:19:00Z", 0, "Loved “ok will do”", None, 1, 10, 2001),
        (
            4,
            "G4",
            "2026-10-20T09:00:00Z",
            0,
            "Your prescription is ready at CVS",
            None,
            2,
            11,
            0,
        ),
        (
            5,
            "G5",
            "2026-10-21T09:00:00Z",
            0,
            "who's booking the hotel?",
            None,
            3,
            12,
            0,
        ),
        (6, "G6", "2026-09-01T09:00:00Z", 0, "too old", None, 1, 10, 0),
        (7, "G7", "2026-10-21T10:00:00Z", 0, "￼", None, 1, 10, 0),
    ]
    for rowid, guid, ts, me, text, body, handle, chat, assoc in rows:
        conn.execute(
            "insert into message values (?,?,?,?,?,?,?,?,?)",
            (rowid, guid, _apple_ns(ts), me, text, body, handle, "iMessage", assoc),
        )
        conn.execute("insert into chat_message_join values (?,?)", (chat, rowid))
    recs = {r["id"]: r for r in cot_mac.read_imessage(conn, 30, contacts)}
    assert sorted(recs) == ["G1", "G2", "G4", "G5"], (
        "reactions, old and attachment-only messages are dropped"
    )
    assert recs["G1"]["text"] == "Would need you to send the gate code"
    assert (
        recs["G1"]["from_name"] == "Sam Rivera" and recs["G1"]["chat"] == "Sam Rivera"
    )
    assert recs["G1"]["unit"] == recs["G2"]["unit"] == "imessage:any;-;+14155550142"
    assert recs["G2"]["from_me"] is True and recs["G2"]["from_name"] == "me"
    assert recs["G1"]["date"] == "2026-10-18T15:17:10Z"
    assert recs["G4"]["from_name"] == "22395" and recs["G4"]["is_group"] is False
    assert recs["G5"]["is_group"] is True
    assert recs["G5"]["chat"] == "Sam, Priya, 650-555-0199"
    assert recs["G5"]["url"] == "messages://"


def test_read_whatsapp(
    tmp_path: Path, contacts_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cot_mac, "cutoff", lambda days: cot_common.parse_iso("2026-10-01T00:00:00Z")
    )
    contacts = cot_mac.load_contacts_db(contacts_db)
    conn = sqlite3.connect(tmp_path / "ChatStorage.sqlite")
    conn.executescript(
        """
        create table ZWACHATSESSION (Z_PK integer primary key, ZPARTNERNAME text, ZCONTACTJID text, ZSESSIONTYPE integer);
        create table ZWAGROUPMEMBER (Z_PK integer primary key, ZCONTACTNAME text, ZMEMBERJID text);
        create table ZWAMESSAGE (Z_PK integer primary key, ZSTANZAID text, ZMESSAGEDATE real, ZISFROMME integer, ZTEXT text,
                                 ZCHATSESSION integer, ZGROUPMEMBER integer, ZPUSHNAME text, ZFROMJID text);
        insert into ZWACHATSESSION values (1, 'Jordan', '15105550123@s.whatsapp.net', 0),
                                          (2, 'Soccer Carpool', '1203@g.us', 1),
                                          (3, '‪+1 (510) 555‑0101‬', '15105550101@s.whatsapp.net', 0);
        insert into ZWAGROUPMEMBER values (1, null, '14155550142@s.whatsapp.net'), (2, 'Jo', '16505550000@s.whatsapp.net');
        """
    )

    def ts(iso_ts: str) -> float:
        return cot_common.parse_iso(iso_ts).timestamp() - APPLE

    conn.executemany(
        "insert into ZWAMESSAGE values (?,?,?,?,?,?,?,?,?)",
        [
            (
                1,
                "S1",
                ts("2026-10-15T10:00:00Z"),
                1,
                "I'll venmo you half",
                1,
                None,
                None,
                None,
            ),
            (
                2,
                "S2",
                ts("2026-10-15T11:00:00Z"),
                0,
                "Partiful invite https://partiful.com/e/x",
                2,
                1,
                None,
                "1203@g.us",
            ),
            (
                3,
                "S3",
                ts("2026-10-15T12:00:00Z"),
                0,
                "see you",
                2,
                2,
                "jojo",
                "1203@g.us",
            ),
            (4, "S4", ts("2026-10-16T12:00:00Z"), 0, "hi", 3, None, None, None),
            (5, "S5", ts("2026-10-16T12:00:00Z"), 0, "   ", 3, None, None, None),
            (6, "S6", ts("2026-09-01T12:00:00Z"), 0, "old", 3, None, None, None),
        ],
    )
    recs = {r["id"]: r for r in cot_mac.read_whatsapp(conn, 30, contacts)}
    assert sorted(recs) == ["S1", "S2", "S3", "S4"]
    assert (
        recs["S1"]["chat"] == "Jordan"
        and recs["S1"]["url"] == "https://wa.me/15105550123"
    )
    assert recs["S2"]["from_name"] == "Sam Rivera", (
        "group member resolved through Contacts"
    )
    assert recs["S2"]["url"] == "whatsapp://" and recs["S2"]["is_group"] is True
    assert recs["S3"]["from_name"] == "Jo"
    assert recs["S4"]["chat"] == "Priya K", (
        "a number-only WhatsApp name falls back to Contacts"
    )


def test_mac_unreachable_is_a_source_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cot_mac,
        "latchkey_curl",
        lambda *_a, **_kw: subprocess.CompletedProcess([], 28, b"", b"timed out"),
    )
    with pytest.raises(cot_common.SourceError, match="Mac isn't reachable"):
        cot_mac.sync_sqlite("/Users/x/chat.db", Path("/nonexistent-cot-test"))


def test_sync_sqlite_skips_unchanged_db_but_refreshes_wal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    downloads: list[str] = []
    monkeypatch.setattr(cot_mac, "_head", lambda path: {"etag": '"abc"'})

    def fake_download(path: str, dest: Path, timeout: float) -> None:
        downloads.append(path.rsplit("/", 1)[-1])
        dest.write_bytes(b"x")

    monkeypatch.setattr(cot_mac, "_download", fake_download)
    (tmp_path / "chat.db-shm").write_bytes(b"stale")
    cot_mac.sync_sqlite("/Users/x/chat.db", tmp_path)
    cot_mac.sync_sqlite("/Users/x/chat.db", tmp_path)
    assert downloads == ["chat.db", "chat.db-wal", "chat.db-wal"]
    assert not (tmp_path / "chat.db-shm").exists()
