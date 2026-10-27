"""Shared paths, configuration, and HTTP helpers for collect-outstanding-tasks.

Every external call goes through ``latchkey curl`` (the workspace's credential
gateway), so no secret ever lives in this skill.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]

# Durable state (raw records, model verdicts, the task list). Kept in the backup:
# model verdicts are paid for and raw records are what every task links back to.
DATA_DIR = Path(
    os.environ.get("COT_DATA_DIR", REPO_ROOT / "data/.skills/collect-outstanding-tasks")
)
# Slack messages the user reacted to with the look-later emoji (default :look-later:) (records, when each was
# first seen, and the marks that predate tracking). See cot_cloud.record_look_later.
LOOK_LATER_PATH = DATA_DIR / "look_later.json"
LOOK_LATER_EMOJI = "look-later"
# Downloaded copies of the Mac's databases (~1 GB). Rebuildable, so they live on
# disk outside the home tree (never /tmp, which is a small RAM disk).
MAC_CACHE_DIR = Path(
    os.environ.get("COT_MAC_CACHE_DIR", "/var/tmp/collect-outstanding-tasks")
)

# Who the user is and which accounts to read: set by the user, never hardcoded.
SETTINGS_PATH = Path(os.environ.get("COT_SETTINGS_PATH", DATA_DIR / "settings.toml"))
EXAMPLE_SETTINGS_PATH = Path(__file__).resolve().parents[1] / "settings.example.toml"
FILE_SERVER = "http://latchkey-self.invalid/minds-api-proxy/api/v1/files"

DEFAULT_DAYS = 30
# Raw records are kept this many days past the look-back window, then evicted.
RAW_RETENTION_EXTRA_DAYS = 14

# Source key -> (task "source" label, tab). The tab rule the user confirmed.
SOURCES: dict[str, tuple[str, str]] = {
    "gmail-work": ("email", "work"),
    "slack": ("slack", "work"),
    "google-docs": ("google-docs", "work"),
    "notion": ("notion", "work"),
    "gmail-personal": ("email", "personal"),
    "imessage": ("text", "personal"),
    "whatsapp": ("whatsapp", "personal"),
}
SOURCE_LABELS = {
    "gmail-work": "Work email",
    "slack": "Slack",
    "google-docs": "Google Docs comments",
    "notion": "Notion to-do list",
    "gmail-personal": "Personal email",
    "imessage": "Texts",
    "whatsapp": "WhatsApp",
}
MAC_SOURCES = ("imessage", "whatsapp")


# Google signals per-minute quota with a 403, not a 429.
RATE_LIMITED = re.compile(
    r"rateLimitExceeded|userRateLimitExceeded|Quota exceeded|RESOURCE_EXHAUSTED"
)


class SourceError(RuntimeError):
    """A source could not be read; the run continues without it."""


class SettingsError(RuntimeError):
    """The settings file is missing or malformed; the message says how to fix it."""


# (section, key, type) for every setting the file may hold.
_SETTING_KEYS: tuple[tuple[str, str, type], ...] = (
    ("", "name", str),
    ("", "employer", str),
    ("gmail", "work_email", str),
    ("gmail", "personal_email", str),
    ("slack", "enabled", bool),
    ("slack", "look_later_emoji", str),
    ("google_docs", "account", str),
    ("notion", "account", str),
    ("notion", "tasks_data_source", str),
    ("mac", "home", str),
)


@dataclass(frozen=True)
class Settings:
    """The user's identity and accounts, from ``settings.toml``. Blank means "off"."""

    name: str = ""
    employer: str = ""
    work_email: str = ""
    personal_email: str = ""
    slack: bool = False
    look_later_emoji: str = LOOK_LATER_EMOJI
    docs_account: str = ""
    notion_account: str = ""
    notion_tasks_data_source: str = ""
    mac_home: str = ""
    path: Path = SETTINGS_PATH

    @property
    def who(self) -> str:
        return self.name or "the user"

    def off_reason(self, source_key: str) -> str | None:
        """Why a source is turned off (naming the setting to fill in), or None if it is on."""
        missing = {
            "gmail-work": [] if self.work_email else ["gmail.work_email"],
            "gmail-personal": [] if self.personal_email else ["gmail.personal_email"],
            "slack": [] if self.slack else ["slack.enabled = true"],
            "google-docs": [] if self.docs_account else ["google_docs.account"],
            "notion": [
                key
                for key, value in (
                    ("notion.account", self.notion_account),
                    ("notion.tasks_data_source", self.notion_tasks_data_source),
                )
                if not value
            ],
            "imessage": [] if self.mac_home else ["mac.home"],
            "whatsapp": [] if self.mac_home else ["mac.home"],
        }[source_key]
        if not missing:
            return None
        return f"turned off: set {' and '.join(missing)} in {self.path} to read it"


def load_settings(path: Path | None = None) -> Settings:
    """Read the settings file, refusing a missing file, a typo'd key or a wrong type."""
    path = path or SETTINGS_PATH
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SettingsError(
            f"No settings file at {path}. Copy {EXAMPLE_SETTINGS_PATH} there and fill in "
            "your name and the accounts to read (leave a source blank to turn it off)."
        ) from None
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(f"{path} is not valid TOML: {exc}") from None
    known = {(section, key): kind for section, key, kind in _SETTING_KEYS}
    values: dict[str, Any] = {}
    for top, value in raw.items():
        entries = value.items() if isinstance(value, dict) else [(None, value)]
        for key, item in entries:
            section, name = (top, key) if key is not None else ("", top)
            label = f"{section}.{name}" if section else name
            kind = known.get((section, name))
            if kind is None:
                raise SettingsError(f"{path}: unknown setting {label!r}")
            if not isinstance(item, kind):
                raise SettingsError(
                    f"{path}: {label} must be {'true or false' if kind is bool else 'text'}"
                )
            values[label] = item.strip() if isinstance(item, str) else item
    return Settings(
        name=values.get("name", ""),
        employer=values.get("employer", ""),
        work_email=values.get("gmail.work_email", ""),
        personal_email=values.get("gmail.personal_email", ""),
        slack=values.get("slack.enabled", False),
        look_later_emoji=values.get("slack.look_later_emoji", LOOK_LATER_EMOJI).strip(":"),
        docs_account=values.get("google_docs.account", ""),
        notion_account=values.get("notion.account", ""),
        notion_tasks_data_source=values.get("notion.tasks_data_source", ""),
        mac_home=values.get("mac.home", "").rstrip("/"),
        path=path,
    )


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(ts: dt.datetime) -> str:
    return (
        ts.astimezone(dt.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def parse_iso(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def cutoff(days: int, now: dt.datetime | None = None) -> dt.datetime:
    return (now or now_utc()) - dt.timedelta(days=days)


def latchkey_curl(
    url: str,
    *,
    account: str | None = None,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: str | None = None,
    extra: list[str] | None = None,
    timeout: float = 120,
) -> subprocess.CompletedProcess[bytes]:
    argv = ["latchkey"]
    if account:
        argv += ["--account", account]
    argv += ["curl", "-sS", "-X", method]
    for k, v in (headers or {}).items():
        argv += ["-H", f"{k}: {v}"]
    if data is not None:
        argv += ["--data-binary", data]
    argv += extra or []
    argv.append(url)
    try:
        return subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        # Same shape as curl's own timeout (exit 28), so callers handle one case.
        return subprocess.CompletedProcess(
            argv, 28, exc.stdout or b"", exc.stderr or b"timed out"
        )


def get_json(
    url: str,
    *,
    account: str | None = None,
    retries: int = 4,
    backoff: float = 5,
    **kw: Any,
) -> Any:
    """GET (or other method) returning parsed JSON, retrying rate limits and 5xx."""
    last = ""
    for attempt in range(retries + 1):
        proc = latchkey_curl(url, account=account, extra=["-w", "\n%{http_code}"], **kw)
        out = proc.stdout.decode("utf-8", "replace")
        body, _, code = out.rpartition("\n")
        if proc.returncode == 0 and code.isdigit():
            status = int(code)
            if (
                status == 429
                or status >= 500
                or (status == 403 and RATE_LIMITED.search(body))
            ):
                last = f"HTTP {status}: {body[:300]}"
                time.sleep(backoff * 2**attempt)
                continue
            try:
                parsed = json.loads(body)
            except ValueError as exc:
                raise SourceError(f"HTTP {status}, not JSON: {body[:300]}") from exc
            if status >= 400:
                raise SourceError(f"HTTP {status}: {json.dumps(parsed)[:300]}")
            return parsed
        last = f"curl exit {proc.returncode}: {proc.stderr.decode('utf-8', 'replace')[:300]}"
        time.sleep(backoff * 2**attempt)
    raise SourceError(last)


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def write_json(path: Path, value: Any) -> None:
    """Write atomically so a reader (the app) never sees a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=1, ensure_ascii=False))
    os.replace(tmp, path)
