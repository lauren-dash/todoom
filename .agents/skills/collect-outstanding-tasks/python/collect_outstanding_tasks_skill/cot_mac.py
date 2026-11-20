"""Texts (iMessage/SMS), WhatsApp, and Contacts, read from copies of the Mac's databases.

The Mac's files come through the Imbue Studio file server, which only answers
while the Mac is awake and the app is running; when it does not, the fetch
raises ``SourceError`` and the run carries on with the cloud sources.

A database is re-downloaded only when the Mac's copy changed (by ETag); its
write-ahead log is always fetched, since that is where new messages land first.
"""

from __future__ import annotations

import datetime as dt
import re
import shutil
import sqlite3
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from collect_outstanding_tasks_skill.cot_common import (
    FILE_SERVER,
    MAC_CACHE_DIR,
    SourceError,
    cutoff,
    iso,
    latchkey_curl,
    read_json,
    write_json,
)

APPLE_EPOCH = 978307200
MAC_OFFLINE = (
    "your Mac isn't reachable; it may be asleep, or Imbue Studio isn't running"
)
# Folders under the Mac home folder (the ``mac.home`` setting).
MESSAGES_DIR = "Library/Messages"
WHATSAPP_DIR = "Library/Group Containers/group.net.whatsapp.WhatsApp.shared"
ADDRESSBOOK_SOURCES = "Library/Application Support/AddressBook/Sources"

# ---------------------------------------------------------------- downloads


def _url(path: str) -> str:
    return FILE_SERVER + urllib.parse.quote(path)


def _head(path: str) -> dict[str, str]:
    proc = latchkey_curl(
        _url(path), method="HEAD", extra=["-I", "--max-time", "20"], timeout=40
    )
    text = proc.stdout.decode("latin-1")
    status = re.match(r"HTTP/\S+ (\d+)", text)
    if proc.returncode != 0 or not status or status.group(1) in ("502", "503", "504"):
        raise SourceError(MAC_OFFLINE)
    if status.group(1) != "200":
        raise SourceError(
            f"your Mac refused {path.rsplit('/', 1)[-1]} (HTTP {status.group(1)})"
        )
    return {
        k.strip().lower(): v.strip()
        for k, v in (
            line.split(":", 1) for line in text.splitlines()[1:] if ":" in line
        )
    }


def _download(path: str, dest: Path, timeout: float) -> None:
    tmp = dest.with_suffix(dest.suffix + ".part")
    proc = latchkey_curl(
        _url(path),
        extra=["-f", "-o", str(tmp), "--max-time", str(int(timeout))],
        timeout=timeout + 30,
    )
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise SourceError(
            f"Download of {path.rsplit('/', 1)[-1]} failed: {proc.stderr.decode('utf-8', 'replace')[:200]}"
        )
    tmp.replace(dest)


# A big database the Mac's app writes to while we copy it comes out torn (old and
# new pages mixed: "database disk image is malformed"). Copy it again this many times.
DOWNLOAD_ATTEMPTS = 3


def _etag(mac_path: str) -> str:
    head = _head(mac_path)
    return head.get("etag") or f"{head.get('last-modified')}|{head.get('content-length')}"


def _intact(db: Path) -> bool:
    """Whether a copied database (without its -wal) is internally consistent."""
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
        try:
            return conn.execute("pragma quick_check(1)").fetchone()[0] == "ok"
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return False


def sync_sqlite(
    mac_path: str, local_dir: Path, *, timeout: float = 900, force: bool = False
) -> Path:
    """Mirror a SQLite db (+ its -wal) from the Mac; return the local db path.

    The main file is re-fetched only when its ETag changed (or ``force``). A copy
    is kept only if the file did not change while it downloaded and it passes
    SQLite's quick check; otherwise it is downloaded again. The -shm index is
    never copied: SQLite rebuilds it from the -wal, and a copy taken at a
    different moment than the -wal could disagree with it.
    """
    local_dir.mkdir(parents=True, exist_ok=True)
    name = mac_path.rsplit("/", 1)[-1]
    db = local_dir / name
    state_path = local_dir / "etags.json"
    state = read_json(state_path, {})
    etag = _etag(mac_path)
    if force or not db.exists() or state.get(name) != etag:
        state.pop(name, None)
        write_json(state_path, state)
        for _attempt in range(DOWNLOAD_ATTEMPTS):
            _download(mac_path, db, timeout)
            after = _etag(mac_path)
            if after == etag and _intact(db):
                break
            etag = after  # it changed mid-copy (or came out torn): copy the new version
        else:
            db.unlink(missing_ok=True)
            raise SourceError(
                f"{name} kept changing on your Mac while it was being copied; will try again next refresh"
            )
        state[name] = etag
        write_json(state_path, state)
    (local_dir / f"{name}-shm").unlink(missing_ok=True)
    wal = local_dir / f"{name}-wal"
    try:
        _head(mac_path + "-wal")
    except SourceError:
        wal.unlink(missing_ok=True)
    else:
        _download(mac_path + "-wal", wal, timeout)
    return db


def open_db(path: Path) -> sqlite3.Connection:
    """Open a mirrored db read-only. SQLite still reads the -wal beside it (it
    rebuilds the -shm index in the writable cache dir) but never checkpoints the
    -wal into the main file, so the main file stays byte-identical to the Mac's
    and the ETag check stays truthful."""
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def check_reachable(home: str) -> None:
    """Fail fast (one ~20 s probe) when the Mac is offline, instead of per file."""
    _head(f"{home}/{MESSAGES_DIR}/chat.db")


# ---------------------------------------------------------------- contacts


def digits10(value: str | None) -> str | None:
    d = re.sub(r"\D", "", value or "")
    return d[-10:] if len(d) >= 7 else None


def load_contacts_db(conn: sqlite3.Connection) -> dict[str, str]:
    names: dict[int, str] = {}
    for pk, first, last, org in conn.execute(
        "select Z_PK, ZFIRSTNAME, ZLASTNAME, ZORGANIZATION from ZABCDRECORD"
    ):
        name = " ".join(p for p in (first, last) if p) or org
        if name:
            names[pk] = name
    out: dict[str, str] = {}
    for owner, number in conn.execute(
        "select ZOWNER, ZFULLNUMBER from ZABCDPHONENUMBER"
    ):
        key = digits10(number)
        if key and owner in names:
            out.setdefault(key, names[owner])
    for owner, addr in conn.execute("select ZOWNER, ZADDRESS from ZABCDEMAILADDRESS"):
        if addr and owner in names:
            out.setdefault(addr.strip().lower(), names[owner])
    return out


def _list_sources(home: str) -> list[str]:
    proc = latchkey_curl(
        _url(f"{home}/{ADDRESSBOOK_SOURCES}/"),
        method="PROPFIND",
        headers={"Depth": "1"},
        timeout=60,
    )
    if proc.returncode != 0:
        raise SourceError(MAC_OFFLINE)
    try:
        root = ET.fromstring(proc.stdout)
    except ET.ParseError as exc:
        raise SourceError("couldn't list your Mac's contact sources") from exc
    ids = []
    for href in root.iter("{DAV:}href"):
        part = urllib.parse.unquote(href.text or "").rstrip("/").rsplit("/", 1)[-1]
        if re.fullmatch(r"[0-9A-F-]{36}", part):
            ids.append(part)
    return ids


def fetch_contacts(home: str) -> dict[str, str]:
    """Phone (last 10 digits) / email -> name, from every Mac address book source."""
    out: dict[str, str] = {}
    for source_id in _list_sources(home):
        db = sync_sqlite(
            f"{home}/{ADDRESSBOOK_SOURCES}/{source_id}/AddressBook-v22.abcddb",
            MAC_CACHE_DIR / "contacts" / source_id,
            timeout=300,
        )
        conn = open_db(db)
        try:
            for k, v in load_contacts_db(conn).items():
                out.setdefault(k, v)
        finally:
            conn.close()
    return out


def contact_name(handle: str | None, contacts: dict[str, str]) -> str | None:
    if not handle:
        return None
    if "@" in handle and not handle.endswith("whatsapp.net"):
        return contacts.get(handle.strip().lower())
    key = digits10(handle.split("@")[0])
    return contacts.get(key) if key else None


def pretty_phone(handle: str) -> str:
    d = re.sub(r"\D", "", handle)
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    if len(d) == 10:
        return f"{d[:3]}-{d[3:6]}-{d[6:]}"
    return handle


# ---------------------------------------------------------------- iMessage


def group_name(
    handles: list[str], contacts: dict[str, str], limit: int = 4
) -> str | None:
    """'Priya, Sam, Jordan +2' for a group chat the user never named."""
    if not handles:
        return None
    names = []
    for h in handles:
        name = contact_name(h, contacts)
        names.append(name.split(" ")[0] if name else pretty_phone(h))
    extra = len(names) - limit
    return ", ".join(names[:limit]) + (f" +{extra}" if extra > 0 else "")


def decode_attributed_body(blob: bytes | None) -> str | None:
    """Plain text from an NSArchiver ``attributedBody`` (where newer macOS keeps it)."""
    if not blob:
        return None
    i = blob.find(b"NSString")
    if i < 0:
        return None
    j = blob.find(b"+", i)
    if j < 0 or j + 1 >= len(blob):
        return None
    n = blob[j + 1]
    start = j + 2
    if n == 0x81:
        n = int.from_bytes(blob[start : start + 2], "little")
        start += 2
    elif n == 0x82:
        n = int.from_bytes(blob[start : start + 4], "little")
        start += 4
    return blob[start : start + n].decode("utf-8", "replace")


IMESSAGE_SQL = """
select m.ROWID, m.guid, m.date, m.is_from_me, m.text, m.attributedBody, h.id, m.service,
       ch.guid, ch.chat_identifier, ch.display_name, ch.style, ch.ROWID
from message m
left join handle h on m.handle_id = h.ROWID
left join chat_message_join cmj on cmj.message_id = m.ROWID
left join chat ch on ch.ROWID = cmj.chat_id
where m.date > ? and m.associated_message_type = 0
order by m.date
"""


def read_imessage(
    conn: sqlite3.Connection, days: int, contacts: dict[str, str]
) -> list[dict[str, Any]]:
    since = (cutoff(days).timestamp() - APPLE_EPOCH) * 1e9
    members: dict[int, list[str]] = {}
    for chat_id, handle in conn.execute(
        "select chj.chat_id, h.id from chat_handle_join chj join handle h on h.ROWID = chj.handle_id"
    ):
        members.setdefault(chat_id, []).append(handle)
    records = []
    for (
        rowid,
        guid,
        date,
        from_me,
        text,
        body,
        handle,
        service,
        chat_guid,
        chat_ident,
        chat_name,
        style,
        chat_rowid,
    ) in conn.execute(IMESSAGE_SQL, (since,)):
        text = text or decode_attributed_body(body)
        if not text or not text.replace("￼", "").strip():
            continue
        is_group = style == 43 or (chat_ident or "").startswith("chat")
        ident = chat_ident or handle or "unknown"
        if is_group and not chat_name:
            chat_name = group_name(members.get(chat_rowid, []), contacts)
        partner = (
            None
            if is_group
            else (
                contact_name(ident, contacts)
                or (pretty_phone(ident) if "@" not in ident else ident)
            )
        )
        sender = (
            "me"
            if from_me
            else (
                contact_name(handle, contacts)
                or (pretty_phone(handle) if handle and "@" not in handle else handle)
                or "unknown"
            )
        )
        records.append(
            {
                "source_key": "imessage",
                "id": guid,
                "rowid": rowid,
                "unit": f"imessage:{chat_guid or ident}",
                "date": iso(
                    dt.datetime.fromtimestamp(date / 1e9 + APPLE_EPOCH, dt.timezone.utc)
                ),
                "from_me": bool(from_me),
                "from_name": sender,
                "handle": None if from_me else handle,
                "chat": chat_name or partner or ident,
                "chat_identifier": ident,
                "is_group": is_group,
                "service": service,
                "text": text,
                "url": f"sms:{ident}"
                if not is_group and ident != "unknown"
                else "messages://",
            }
        )
    return records


def _read_mirrored(
    mac_path: str, local_dir: Path, timeout: float, read: Any
) -> Any:
    """Read a mirrored db; if the saved copy turns out damaged, copy it afresh once."""
    for force in (False, True):
        conn = open_db(sync_sqlite(mac_path, local_dir, timeout=timeout, force=force))
        try:
            return read(conn)
        except sqlite3.DatabaseError as exc:
            if force or "malformed" not in str(exc) and "not a database" not in str(exc):
                raise
        finally:
            conn.close()
    raise AssertionError("unreachable")


def fetch_imessage(
    home: str, days: int, contacts: dict[str, str]
) -> list[dict[str, Any]]:
    return _read_mirrored(
        f"{home}/{MESSAGES_DIR}/chat.db", MAC_CACHE_DIR / "messages", 900,
        lambda conn: read_imessage(conn, days, contacts),
    )


# ---------------------------------------------------------------- WhatsApp

WHATSAPP_SQL = """
select m.Z_PK, m.ZSTANZAID, m.ZMESSAGEDATE, m.ZISFROMME, m.ZTEXT, s.ZPARTNERNAME, s.ZCONTACTJID,
       s.ZSESSIONTYPE, m.ZPUSHNAME, gm.ZCONTACTNAME, gm.ZMEMBERJID, m.ZFROMJID
from ZWAMESSAGE m join ZWACHATSESSION s on m.ZCHATSESSION = s.Z_PK
left join ZWAGROUPMEMBER gm on m.ZGROUPMEMBER = gm.Z_PK
where m.ZMESSAGEDATE > ? and m.ZTEXT is not null and trim(m.ZTEXT) != ''
order by m.ZMESSAGEDATE
"""


def read_whatsapp(
    conn: sqlite3.Connection, days: int, contacts: dict[str, str]
) -> list[dict[str, Any]]:
    since = cutoff(days).timestamp() - APPLE_EPOCH
    records = []
    for (
        pk,
        stanza,
        ts,
        from_me,
        text,
        partner,
        jid,
        stype,
        push,
        member,
        member_jid,
        from_jid,
    ) in conn.execute(WHATSAPP_SQL, (since,)):
        is_group = stype == 1
        phone = jid.split("@")[0] if jid and jid.endswith("@s.whatsapp.net") else None
        partner = (
            re.sub(r"[\u200e\u200f\u202a-\u202e]", "", partner or "")
            .replace("\xa0", " ")
            .strip()
            or None
        )
        chat = partner or jid
        if not is_group and phone:
            if partner and not re.search(r"[^\d\s()+\-‑]", partner):
                partner = None  # WhatsApp only knows the number
            chat = contact_name(phone, contacts) or partner or pretty_phone(phone)
        if from_me:
            sender = "me"
        elif is_group:
            sender_jid = member_jid or from_jid
            sender = (
                contact_name(sender_jid, contacts)
                if sender_jid and "whatsapp.net" in sender_jid
                else None
            )
            sender = sender or member or push or "unknown"
        else:
            sender = chat
        records.append(
            {
                "source_key": "whatsapp",
                "id": stanza or str(pk),
                "pk": pk,
                "unit": f"whatsapp:{jid}",
                "date": iso(
                    dt.datetime.fromtimestamp(ts + APPLE_EPOCH, dt.timezone.utc)
                ),
                "from_me": bool(from_me),
                "from_name": sender,
                "chat": chat,
                "chat_jid": jid,
                "is_group": is_group,
                "text": text,
                "url": f"https://wa.me/{phone}" if phone else "whatsapp://",
            }
        )
    return records


def fetch_whatsapp(
    home: str, days: int, contacts: dict[str, str]
) -> list[dict[str, Any]]:
    return _read_mirrored(
        f"{home}/{WHATSAPP_DIR}/ChatStorage.sqlite", MAC_CACHE_DIR / "whatsapp", 300,
        lambda conn: read_whatsapp(conn, days, contacts),
    )


def clear_cache() -> None:
    shutil.rmtree(MAC_CACHE_DIR, ignore_errors=True)
